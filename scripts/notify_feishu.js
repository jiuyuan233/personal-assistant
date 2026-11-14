/**
 * =============================================================================
 * notify_feishu.js — 飞书自定义机器人推送
 * =============================================================================
 * 结构与 env 契约沿用 Lottery-BiliBiliToolB-Web233/lottery/notify-feishu.js，
 * 只把「B站抽奖任务模板」换成「个人助手模式模板」，把中奖检测换成紧急度检测。
 *
 * 用法：
 *   node scripts/notify_feishu.js                 # 状态卡片（读环境变量；常规成功静默）
 *   node scripts/notify_feishu.js "自定义消息"     # 三时段推送（有图就发带图卡片）
 *
 * 环境变量：
 *   FS_WEBHOOK     — 飞书 webhook（未设置则静默退出 0）
 *   NOTIFY_TITLE   — 通知大类，如「个人助手」
 *   NOTIFY_TASK    — 模式名，逗号分隔：study / notice / diary / finalize / probe
 *   NOTIFY_STATUS  — success / failure / cancelled
 *   NOTIFY_KIND    — morning / noon / night，决定推送卡片标题
 *   NOTIFY_OUTPUT  — 任务日志（tail -200），用于抽指标
 *   PUSH_IMAGE_PATH — 图片信息 JSON（默认 /tmp/push-image.json，anime_image.py 写）
 *   GITHUB_REPOSITORY / GITHUB_RUN_ID — Actions 自动注入，用于「查看日志」按钮
 *
 * ⚠️ 降噪（用户 2026-11-14：「正常运行的不用给我汇报，飞书信息太多了」）：
 *   状态卡片只在「重要」时发 —— 失败/取消，或日志里出现 [urgent]（24h 内截止的新任务）。
 *   常规成功一律静默退出 0，日志留一行痕。三时段推送不受此限制，它们本来就是用户要的。
 * =============================================================================
 */

const https = require('https');
const fs = require('fs');

// =============================================================================
// 二次脱敏（审计 §2.5）
// Python 侧 utils.redact() 只洗它自己 log() 出去的东西。这里的 NOTIFY_OUTPUT 是
// shell `tail -200` 抓的原始日志，里面可能混进 tee 之前的裸输出、node 报错、
// 或者未来某个忘了走 log() 的 print。飞书卡片同样会被人看到，所以再洗一遍。
// 规则必须与 scripts/utils.py 的 _REDACT_PATTERNS 保持一致，改一边就要改另一边。
// =============================================================================
const REDACT_PATTERNS = [
    /(access_token=)[^&\s"']+/g,
    /(Bearer\s+)[A-Za-z0-9._\-]+/g,
    /(sk-)[A-Za-z0-9]{8,}/g,
    /(ghp_)[A-Za-z0-9]{8,}/g,
    /((?:password|passwd|pwd|api_key|apikey|token|secret)["']?\s*[:=]\s*["']?)[^\s&"',}]+/gi,
    /((?:Cookie|Set-Cookie|JSESSIONID|_uid|UID|uf|_d|vc3|fid|unam|route)["']?\s*[:=]\s*["']?)[^\s&"',}]+/gi,
    /(cookie["']?\s*[:=]\s*["']?)[^\n"']+/gi,
];

function redact(s) {
    let t = String(s == null ? '' : s);
    for (const re of REDACT_PATTERNS) t = t.replace(re, '$1***');
    return t;
}

const webhook = process.env.FS_WEBHOOK;
const title   = process.env.NOTIFY_TITLE  || '个人助手';
const task    = process.env.NOTIFY_TASK   || '';
const status  = process.env.NOTIFY_STATUS || '';
const kind    = process.env.NOTIFY_KIND   || '';
const output  = redact(process.env.NOTIFY_OUTPUT || '');
const repo    = process.env.GITHUB_REPOSITORY || '';
const runId   = process.env.GITHUB_RUN_ID     || '';

// 图片信息（anime_image.py 落盘）。读不到/解析失败都当没有图，绝不阻断推送。
let image = null;
try {
    const p = process.env.PUSH_IMAGE_PATH || '/tmp/push-image.json';
    if (fs.existsSync(p)) image = JSON.parse(fs.readFileSync(p, 'utf8'));
} catch (e) {
    console.log('[feishu] 图片信息读取失败，按无图处理:', e.message);
    image = null;
}

if (!webhook) {
    console.log('[feishu] 未配置 FS_WEBHOOK，跳过推送');
    process.exit(0);
}

// =============================================================================
// 模式模板 — 图标、名称、指标正则
// 正则匹配 scripts/*.py 里 log() 打出的行，改日志格式时同步改这里
// =============================================================================
const TASK_TEMPLATES = {
    'tick': {
        icon: '🔄',
        name: '定时巡检',
        metrics: [
            { label: '课程数',    patterns: [/课程总数[：:]\s*(\d+)/, /扫描课程[：:]\s*(\d+)/] },
            { label: '待完成',    patterns: [/待完成[：:]\s*(\d+)/] },
            { label: '截止未知',  patterns: [/截止未知\s*(\d+)/] },
            { label: '已逾期',    patterns: [/已逾期[：:]\s*(\d+)/] },
            { label: '新增名言',  patterns: [/名言合并[：:]\s*新增\s*(\d+)/] },
            { label: '新增计划',  patterns: [/计划合并[：:]\s*新增\s*(\d+)/] },
            { label: '新增公告',  patterns: [/公告合并[：:]\s*新增\s*(\d+)/] },
        ]
    },
    'study': {
        icon: '📚',
        name: '学习通作业',
        metrics: [
            { label: '课程数',    patterns: [/课程总数[：:]\s*(\d+)/, /扫描课程[：:]\s*(\d+)/] },
            { label: '待完成',    patterns: [/待完成[：:]\s*(\d+)/] },
            { label: '24h内截止', patterns: [/24小时内截止[：:]\s*(\d+)/] },
            { label: '已逾期',    patterns: [/已逾期[：:]\s*(\d+)/] },
            { label: '新出现',    patterns: [/新出现\s*(\d+)\s*条/] },
        ]
    },
    'notice': {
        icon: '📢',
        name: '校园公告',
        metrics: [
            { label: '站点成功', patterns: [/抓取站点[：:]\s*\d+[，,]\s*成功[：:]\s*(\d+)/] },
            { label: '新增公告', patterns: [/公告合并[：:]\s*新增\s*(\d+)/] },
            { label: '高相关',   patterns: [/高相关[：:]\s*(\d+)/] },
        ]
    },
    'diary': {
        icon: '📔',
        name: '日记挖掘',
        metrics: [
            { label: '变更文件', patterns: [/变更文件[：:]\s*(\d+)/] },
            { label: '新增名言', patterns: [/名言合并[：:]\s*新增\s*(\d+)/] },
            { label: '新增计划', patterns: [/计划合并[：:]\s*新增\s*(\d+)/] },
        ]
    },
    'finalize': {
        icon: '📝',
        name: '渲染发布',
        metrics: [
            { label: '任务清单', patterns: [/任务清单[：:]\s*(\d+)\s*条/] },
            { label: '本周课时', patterns: [/本周课时[：:]\s*(\d+)/] },
        ]
    },
    'probe': {
        icon: '🛰️',
        name: '海外连通性探测',
        metrics: [
            { label: '可达',   patterns: [/可达[：:]\s*(\d+)/] },
            { label: '不可达', patterns: [/不可达[：:]\s*(\d+)/] },
        ]
    },
};

function extractMetric(patterns, log) {
    for (const p of patterns) {
        const match = log.match(p);
        if (match && match[1]) return match[1];
    }
    return null;
}

// =============================================================================
// 紧急度检测 — 替代原脚本的中奖检测
// 采集器把最急的任务打成 `[urgent] …` 行，这里搬到卡片顶部
// =============================================================================
function extractUrgent(log) {
    const out = [];
    const re = /\[urgent\]\s*(.+)/g;
    let m;
    while ((m = re.exec(log)) !== null) {
        const text = m[1] && m[1].trim();
        if (text && !out.includes(text)) out.push(text);
    }
    return out.slice(0, 5);
}

// 已逾期**不再**作为发卡理由：逾期任务做不了，README 已把它们剔到「已剔除任务」文档，
// 而且逾期数是常驻的（实测长期 2 项），拿它当「重要」会让每轮都发卡 —— 正是用户抱怨的噪音。
// 「重要」只认两种：运行失败/取消，以及 [urgent]（24h 内截止、且采集器判定集合有变化才打）。

// =============================================================================
// 状态映射
// =============================================================================
const STATUS_MAP = {
    success:   { emoji: '✅', label: '成功',   color: 'green' },
    failure:   { emoji: '❌', label: '失败',   color: 'red'   },
    cancelled: { emoji: '⏹️', label: '已取消', color: 'grey'  },
};
const st = STATUS_MAP[status] || { emoji: '📋', label: status || '完成', color: 'blue' };

const now = new Date();
const bjTime = new Date(now.getTime() + 8 * 3600 * 1000);
const timeStr = bjTime.toISOString().replace('T', ' ').substring(0, 19) + ' (UTC+8)';

// =============================================================================
// 构造消息
// =============================================================================
const PUSH_HEADER = {
    morning: { emoji: '🌅', name: '早安', color: 'orange' },
    noon:    { emoji: '☀️', name: '午安', color: 'blue' },
    night:   { emoji: '🌙', name: '晚安', color: 'indigo' },
    urgent:  { emoji: '⏰', name: '紧急截止提醒', color: 'red' },
};

function imageNote() {
    if (!image) return '';
    const src = image.pixiv ? 'P站' : (image.source_url ? '网络' : '未标注');
    return `🖼 今日一图：${image.artist || '佚名'}（图源 ${src}）`;
}

// 三时段推送。有图就发卡片：配了飞书应用凭证时 image_key 内嵌真图，
// 没配就发「点开看图」按钮（webhook 卡片按钮是本项目已验证可用的元素）。
// 无图时保持纯文本，和以前一样。
function buildPushPayload(text) {
    if (!image) return { msg_type: 'text', content: { text } };
    const h = PUSH_HEADER[kind] || { emoji: '💌', name: '个人助手', color: 'blue' };
    const elements = [{ tag: 'div', text: { tag: 'lark_md', content: text } }];
    if (image.image_key) {
        // ⚠️ 飞书卡片 schema 的必填属性名是 img_key（不是上传接口返回的 image_key）。
        // 属性名写错飞书会拒卡 11246「img element must contain img_key」——v1.5.5 真机实测踩过。
        elements.push({
            tag: 'img',
            img_key: image.image_key,
            alt: { tag: 'plain_text', content: '今日二次元图' }
        });
    }
    const note = imageNote();
    if (note) elements.push({ tag: 'div', text: { tag: 'lark_md', content: note } });
    const actions = [];
    if (!image.image_key && image.url) {
        actions.push({
            tag: 'button',
            text: { tag: 'plain_text', content: '🖼 点开今天的图' },
            url: image.url,
            type: 'primary'
        });
    }
    if (image.source_url) {
        actions.push({
            tag: 'button',
            text: { tag: 'plain_text', content: '🔗 图源作品页' },
            url: image.source_url,
            type: 'default'
        });
    }
    if (actions.length) elements.push({ tag: 'action', actions });
    return {
        msg_type: 'interactive',
        card: {
            config: { wide_screen_mode: true },
            header: {
                title: { tag: 'plain_text', content: `${h.emoji} ${h.name} · ${title}` },
                template: h.color
            },
            elements: elements
        }
    };
}

// 卡片发失败时的兜底纯文本：把图的信息以链接形式补进去，保证用户至少点得到。
function pushFallbackText(text) {
    if (!image) return text;
    const lines = [text, '', imageNote()];
    if (image.url) lines.push(image.url);
    if (image.source_url) lines.push(`图源：${image.source_url}`);
    return lines.join('\n');
}

let payload;
let fallbackPayload = null;

if (process.argv[2]) {
    const text = redact(process.argv[2]);
    payload = buildPushPayload(text);
    if (payload.msg_type === 'interactive') {
        fallbackPayload = { msg_type: 'text', content: { text: pushFallbackText(text) } };
    }
} else {
    const subTasks = task.split(',').map(t => t.trim()).filter(Boolean);
    const elements = [];
    const urgent = extractUrgent(output);

    // 降噪闸门：常规成功且没有紧急任务 → 静默。失败/取消永远报红。
    if (status === 'success' && urgent.length === 0) {
        console.log('[feishu] 常规成功且无紧急任务，按用户要求静默（不发卡）');
        process.exit(0);
    }

    let headerText = title;
    if (subTasks.length === 1) {
        const tmpl = TASK_TEMPLATES[subTasks[0]];
        if (tmpl) headerText = `${title} — ${tmpl.icon} ${tmpl.name}`;
    } else if (subTasks.length > 1) {
        headerText = `${title}（${subTasks.length} 个任务）`;
    }

    // 紧急度覆盖颜色（失败永远红色，不被覆盖）
    if (status !== 'failure' && urgent.length > 0) {
        headerText = `⏰ ${headerText} — 有任务即将截止`;
        st.color = 'orange';
        st.emoji = '⏰';
    }

    let content = '';

    if (urgent.length > 0) {
        content += '\n# ⏰ 即将截止\n';
        for (const u of urgent) content += `  🔥 **${u}**\n`;
        content += '\n---\n\n';
    }

    content += `${st.emoji} **状态：${st.label}**`;
    content += `\n🕐 **时间：** ${timeStr}`;
    if (repo) content += `\n📦 **仓库：** ${repo}`;

    for (const subTask of subTasks) {
        const tmpl = TASK_TEMPLATES[subTask];
        if (tmpl) {
            content += `\n\n── **${tmpl.icon} ${tmpl.name}** ──`;
            if (tmpl.metrics.length > 0) {
                const lines = tmpl.metrics.map(m => {
                    const val = extractMetric(m.patterns, output);
                    return `  ${m.label}：**${val || '—'}**`;
                });
                content += '\n' + lines.join('\n');
            } else {
                content += '\n  （详见日志）';
            }
        } else {
            content += `\n\n── **${subTask}** ──\n  （详见日志）`;
        }
    }

    if (output) {
        const lastLines = output.split('\n').filter(l => l.trim()).slice(-5);
        if (lastLines.length > 0) {
            content += '\n\n📝 **日志摘要：**';
            content += '\n```\n' + lastLines.join('\n') + '\n```';
        }
    }

    elements.push({ tag: 'div', text: { tag: 'lark_md', content: content } });

    if (repo && runId) {
        elements.push({
            tag: 'action',
            actions: [{
                tag: 'button',
                text: { tag: 'plain_text', content: '📋 查看完整运行日志' },
                url: `https://github.com/${repo}/actions/runs/${runId}`,
                type: 'primary'
            }]
        });
    }

    payload = {
        msg_type: 'interactive',
        card: {
            config: { wide_screen_mode: true },
            header: {
                title: { tag: 'plain_text', content: `${headerText} ${st.emoji}` },
                template: st.color
            },
            elements: elements
        }
    };
}

// =============================================================================
// 发送（卡片失败时退回纯文本，推送不能因为卡片 schema 问题整个丢掉）
// =============================================================================
function post(p, done) {
    const data = JSON.stringify(p);
    const parsed = new URL(webhook);
    const req = https.request({
        hostname: parsed.hostname,
        path: parsed.pathname + parsed.search,
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(data) }
    }, (res) => {
        let body = '';
        res.on('data', (chunk) => body += chunk);
        res.on('end', () => {
            let ok = false;
            if (res.statusCode === 200) {
                try {
                    const result = JSON.parse(body);
                    ok = (result.code === 0 || result.StatusCode === 0);
                } catch { ok = false; }
            }
            if (ok) {
                console.log('[feishu] ✅ 推送成功');
            } else {
                console.log(`[feishu] ⚠️ 推送返回 HTTP ${res.statusCode}:`, body);
            }
            done(ok);
        });
    });
    req.on('error', (e) => {
        console.log('[feishu] ❌ 请求错误:', e.message);
        done(false);
    });
    req.write(data);
    req.end();
}

post(payload, (ok) => {
    if (!ok && fallbackPayload) {
        console.log('[feishu] 卡片发送失败，退回纯文本重发');
        post(fallbackPayload, () => {});
    }
});