// 任务看板前端。只读 render_readme.py 生成的 docs/data/*.json，不做任何计算逻辑，
// 分档/排序的权威实现都在 Python 侧，这里只负责显示。
'use strict';

const KIND = { work: '作业', eval: '互评', exam: '考试' };
const STATE = { pending: '待完成', submitted: '已提交', done: '已完成', unknown: '未知' };
const BUCKETS = [
  ['overdue', '🔴 已逾期'],
  ['unknown', '🔴 截止未知（可能早已逾期，点进去自己确认）'],
  ['today', '🟠 今天截止'],
  ['week', '🟡 7 天内'],
  ['later', '⚪ 更远'],
];

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"]/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

async function load(name) {
  try {
    const r = await fetch(`data/${name}.json`, { cache: 'no-store' });
    if (!r.ok) return null;
    return await r.json();
  } catch { return null; }
}

function fmtRemaining(iso) {
  if (!iso) return '—';
  const ms = new Date(iso) - new Date();
  const abs = Math.abs(ms);
  const d = Math.floor(abs / 86400000);
  const h = Math.floor((abs % 86400000) / 3600000);
  const m = Math.floor((abs % 3600000) / 60000);
  const body = d ? `${d}天${h}小时` : h ? `${h}小时${m}分` : `${m}分`;
  return ms < 0 ? `已逾期${body}` : `剩余${body}`;
}

function fmtWhen(iso, exact) {
  if (!iso) return '—';
  const d = new Date(iso);
  const p = (n) => String(n).padStart(2, '0');
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}${exact ? '' : ' ≈'}`;
}

function bucketOf(t, now) {
  // 没有截止时间不是「还很宽裕」，是「不知道」。Python 侧同样单独分一档。
  if (!t.deadline) return 'unknown';
  const d = new Date(t.deadline);
  if (d < now) return 'overdue';
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const that = new Date(d.getFullYear(), d.getMonth(), d.getDate());
  if (that.getTime() === today.getTime()) return 'today';
  if (d - now <= 7 * 86400000) return 'week';
  return 'later';
}

function taskTable(items) {
  if (!items.length) return '<p class="empty">无</p>';
  const rows = items.map((t) => `<tr>
    <td class="when">${esc(fmtWhen(t.deadline, t.deadline_exact))}</td>
    <td>${esc(t.course)}</td>
    <td>${t.url ? `<a href="${esc(t.url)}" target="_blank" rel="noopener">${esc(t.title)}</a>` : esc(t.title)}</td>
    <td><span class="tag">${esc(KIND[t.kind] || t.kind)}</span></td>
    <td>${esc(STATE[t.state] || t.status)}</td>
    <td class="when">${esc(fmtRemaining(t.deadline))}</td>
  </tr>`).join('');
  return `<table><thead><tr><th>截止</th><th>课程</th><th>任务</th><th>类型</th><th>状态</th><th>倒计时</th></tr></thead><tbody>${rows}</tbody></table>`;
}

function renderTasks(data) {
  const items = (data && data.items) || [];
  const now = new Date();
  const pending = items.filter((t) => t.state === 'pending');
  const groups = { overdue: [], unknown: [], today: [], week: [], later: [] };
  pending.forEach((t) => groups[bucketOf(t, now)].push(t));
  const byDeadline = (a, b) => (a.deadline ? new Date(a.deadline) : Infinity) - (b.deadline ? new Date(b.deadline) : Infinity);
  Object.values(groups).forEach((g) => g.sort(byDeadline));

  $('stats').innerHTML = [
    ['red', `逾期 ${groups.overdue.length}`],
    ['red', `截止未知 ${groups.unknown.length}`],
    ['orange', `今天 ${groups.today.length}`],
    ['yellow', `7天内 ${groups.week.length}`],
    ['', `待完成 ${pending.length}`],
    ['', `总任务 ${items.length}`],
  ].map(([c, s]) => `<span class="chip ${c}">${esc(s)}</span>`).join('');

  $('tasks').innerHTML = BUCKETS
    .filter(([k]) => groups[k].length)
    .map(([k, label]) => `<div class="bucket ${k}"><h3>${label}（${groups[k].length}）</h3>${taskTable(groups[k])}</div>`)
    .join('') || '<p class="empty">当前没有待完成任务。</p>';
}

function renderNotices(data) {
  const items = ((data && data.items) || []).filter((n) => (n.score || 0) >= 3).slice(0, 15);
  if (!items.length) { $('notices').innerHTML = '<p class="empty">暂无高相关公告。</p>'; return; }
  $('notices').innerHTML = `<table><thead><tr><th>分</th><th>日期</th><th>公告</th><th>来源</th><th>截止</th></tr></thead><tbody>${
    items.map((n) => `<tr><td>${esc(n.score)}</td><td class="when">${esc(n.publish_date || '—')}</td>
      <td><a href="${esc(n.url)}" target="_blank" rel="noopener">${esc(n.title)}</a></td>
      <td>${esc(n.source)}</td><td class="when">${esc(n.deadline || '—')}</td></tr>`).join('')
  }</tbody></table>`;
}

function renderPlans(data) {
  const items = ((data && data.items) || []).filter((p) => p.state === 'open').slice(0, 20);
  if (!items.length) { $('plans').innerHTML = '<p class="empty">暂无未完成计划。</p>'; return; }
  // 只显示日期，不显示 source_file：那是私有日记仓库的目录结构（审计 §2.2）
  $('plans').innerHTML = `<table><thead><tr><th>计划</th><th>置信度</th><th>截止</th><th>记录于</th></tr></thead><tbody>${
    items.map((p) => `<tr><td>${esc(p.text)}</td><td>${esc((p.confidence || 0).toFixed(1))}</td>
      <td class="when">${esc(p.due_date || '—')}</td><td class="when">${esc(p.source_date || '—')}</td></tr>`).join('')
  }</tbody></table>`;
}

function renderQuotes(data) {
  // 与 README 的 pick_quotes() 同一口径：AI 价值降序，同分新的在前，没打分按 0 排最后。
  // 看板是全量视图所以留 10 条，README 只留 3 条（QUOTE_LIMIT）。
  const items = ((data && data.items) || []).slice().sort((a, b) => {
    const d = (b.value || 0) - (a.value || 0);
    return d !== 0 ? d : String(b.first_seen || '').localeCompare(String(a.first_seen || ''));
  }).slice(0, 10);
  if (!items.length) { $('quotes').innerHTML = '<p class="empty">暂无摘录。</p>'; return; }
  $('quotes').innerHTML = items.map((q) =>
    `<blockquote>${esc(q.text)}<span class="ctx">${esc(q.context || '')}${q.source_date ? ' · ' + esc(q.source_date) : ''}${
      q.value != null ? ' · AI 价值 ' + esc(q.value) + '/10' : ''}</span>${
      q.comment ? `<span class="ctx">💬 ${esc(q.comment)}</span>` : ''}</blockquote>`
  ).join('');
}

// 实时时间线。几何参数由 week_svg.py 导出，和 SVG 里那条静态线共用一套算法，
// 区别只是这条由浏览器每 30 秒重算，不用等 20 分钟一次的 finalize。
function nowLineY(minutes, geo) {
  const anchors = [];
  geo.slots.forEach((hhmm, r) => {
    const [h, m] = hhmm.split(':').map(Number);
    const start = h * 60 + m;
    const y = geo.body_top + r * geo.row_h;
    anchors.push([start, y], [start + geo.class_minutes, y + geo.row_h]);
  });
  if (minutes <= anchors[0][0]) return anchors[0][1];
  if (minutes >= anchors[anchors.length - 1][0]) return anchors[anchors.length - 1][1];
  for (let i = 0; i < anchors.length - 1; i++) {
    const [t0, y0] = anchors[i], [t1, y1] = anchors[i + 1];
    if (minutes >= t0 && minutes <= t1) return t1 === t0 ? y0 : y0 + (y1 - y0) * (minutes - t0) / (t1 - t0);
  }
  return anchors[anchors.length - 1][1];
}

async function startNowLine() {
  const geo = await load('schedule_geo');
  const line = $('nowline');
  if (!geo || !line || !Array.isArray(geo.slots)) return;

  const tick = () => {
    // 用北京时间算，和 SVG 一致；浏览器时区不一定是 UTC+8
    const bj = new Date(Date.now() + (8 * 60 + new Date().getTimezoneOffset()) * 60000);
    const iso = `${bj.getFullYear()}-${String(bj.getMonth() + 1).padStart(2, '0')}-${String(bj.getDate()).padStart(2, '0')}`;
    // 图是某一周的快照，跨周后 today_col 就不对了，宁可隐藏也不画歪
    const drift = Math.round((new Date(iso) - new Date(geo.date)) / 86400000);
    const col = geo.today_col + drift;
    if (col < 0 || col > 6) { line.hidden = true; return; }

    const y = nowLineY(bj.getHours() * 60 + bj.getMinutes(), geo);
    const x = geo.pad + geo.gutter + col * geo.col_w;
    line.hidden = false;
    line.style.top = (y / geo.height * 100) + '%';
    line.style.left = (x / geo.width * 100) + '%';
    line.style.width = (geo.col_w / geo.width * 100) + '%';
    $('nowlabel').textContent = `${String(bj.getHours()).padStart(2, '0')}:${String(bj.getMinutes()).padStart(2, '0')}`;
  };

  tick();
  setInterval(tick, 30000);
}

(async function main() {
  const [tasks, meta, notices, plans, quotes] = await Promise.all(
    ['tasks', 'meta', 'notices', 'plans', 'quotes'].map(load));
  $('meta').textContent = meta
    ? `最后更新 ${meta.updated_at || '未知'} · ${meta.week || ''}`
    : '数据尚未生成，先跑一次 Actions 的 finalize 模式。';
  renderTasks(tasks);
  renderNotices(notices);
  renderPlans(plans);
  renderQuotes(quotes);
  startNowLine();
})();