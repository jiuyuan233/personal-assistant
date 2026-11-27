"""飞书三时段推送：早 7 点作业+公告+一言+二次元图、中午问好鼓励、晚上日记总结。

用户的要求（2026-11-14 第二次细化）：
- 只传达重要信息，常规早中晚 3 条；**正常运行不汇报**（降噪在 notify_feishu.js 做）。
- 早上：待完成的作业 + 值得关注的公告 + 一言（https://v1.hitokoto.cn/）+ 二次元图 + 中文问好。
- 晚上：对当天日记做总结。中午：问好 + 鼓励。
- **不带课表**（「不用给我发要上什么课」）。
- 内容不限于他点名的，但必须包含他点名的。

结构上把「人话」和「清单」分开，这是本轮最重要的设计决定：
  AI 只写人设 prose（问好/调侃/鼓励/总结），**清单由程序确定性拼在 prose 后面**。
整条消息交给 AI 复述事实时它会漏条目、改日期；用户要的是「作业和公告一条不漏」，
所以清单不经过 AI。AI 挂了也有降级 prose，清单照样在，消息不会断。

数据口径复用 render_readme.actionable / dedup.sort_by_deadline，
和 README 任务清单同一套判定，避免「飞书说能做、README 说已逾期」这种两处漂移。
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

from dedup import load_list, sort_by_deadline
from utils import (BJ_TZ, DATA_DIR, clean_html_text, log, now_bj,
                   parse_datetime, safe_text)

DIARY_DIR = DATA_DIR / "diary"
# workflow 的飞书步骤读这个文件；存在就发推送，不存在就走运行状态卡片（且只报重要信息）。
# PUSH_MESSAGE_PATH 只为本地 Windows 调试留的口子，线上不设，默认仍是 /tmp。
MESSAGE_PATH = Path(os.environ.get("PUSH_MESSAGE_PATH", "/tmp/push-message.txt"))
# 6 小时紧急截止提醒（用户 2026-11-14：「距离截止时间还剩6小时的可以跳出这个
# 每天3条的规则，直接通知我」）——独立于三时段，每条作业只提醒一次，记账在 runtime.urgent6。
URGENT_MESSAGE_PATH = Path(os.environ.get("PUSH_URGENT_PATH", "/tmp/push-urgent.txt"))
URGENT_HOURS = 6
# 新作业发布提醒（用户 2026-11-16：「首次检测到有新的作业发布的时候给我发一个
# 消息，这个独立与其他的消息」）——第三条独立通道，每条任务只通知一次，
# 记账在 runtime.notified_new。与三时段/urgent6 互不挤占、互不依赖。
NEW_MESSAGE_PATH = Path(os.environ.get("PUSH_NEW_PATH", "/tmp/push-new.txt"))
# 上线时刻：这之前首次发现的存量任务**不追溯**（上线当天数据里已有 76 条任务、
# 其中 37 条随堂是当天刚收录的，全推一遍就是轰炸）。首次真实通知发生在
# 上线之后第一个真正新发布的任务上。
NEW_SINCE = datetime(2026, 11, 16, 16, 0, tzinfo=BJ_TZ)
# 首次发现超过这个小时数的不再当「新」（系统停摆恢复后的补报上限，
# 超过的属于旧闻，README 里自己看）
NEW_WINDOW_HOURS = 48
NEW_MAX_PER_MSG = 8   # 单条消息最多列 8 条，其余计数并指向 README
NEW_LEDGER_DAYS = 30  # 记账保留天数，与 urgent6 同款
HITOKOTO_URL = "https://v1.hitokoto.cn/"
# 免费数据源（v1.5.7 新增，用户勾选）：金山词霸每日一句、open-meteo 天气（无需 key）。
ICIBA_URL = "https://open.iciba.com/dsapi/"
# 坐标 = 河南城建学院（平顶山，城市级精度足够天气参考；用户 2026-11-16 要求
# 文案里直接写学校名）。
WEATHER_URL = ("https://api.open-meteo.com/v1/forecast?latitude=33.77&longitude=113.19"
               "&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max"
               "&timezone=Asia/Shanghai&forecast_days=2")
KINDS = ("morning", "noon", "dusk", "night")
WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
MORNING_TASK_LIMIT = 6
MORNING_NOTICE_LIMIT = 3
NOTICE_MIN_SCORE = 3
# v1.5.19 傍晚冲刺档（用户 2026-11-15 新增）：18:30，今天截止的最后一次"还来得及"提醒
# + 中文科技热点（IT之家 RSS；v1.5.19 曾用 Hacker News + AI 翻译，用户定调要中文后换源）。
# 内容与其他三档错开：科技热点只在此档出现。
ITHOME_RSS = "https://www.ithome.com/rss/"
TECH_NEWS_HOURS = 24   # 科技快讯时效：超过 24 小时的不算「今日热点」
HN_TOP_LIMIT = 6       # 候选条数（沿用的常量名，含义=科技热点候选上限）
HN_STORY_COUNT = 3     # 最终展示条数


def hitokoto() -> str:
    """取一条一言。失败返回空串，让推送照常发出去。"""
    try:
        j = requests.get(HITOKOTO_URL, timeout=15).json()
        text = safe_text(j.get("hitokoto"), 120).strip()
        src = safe_text(j.get("from"), 40).strip()
        if not text:
            return ""
        return f"{text}（{src}）" if src else text
    except Exception as e:
        log(f"一言获取失败：{type(e).__name__}", level="WARN")
        return ""


def _when(t: dict) -> str:
    dt = parse_datetime(t.get("deadline"))
    if not dt:
        return "⚠️截止未知"
    s = dt.strftime("%m-%d %H:%M")
    return s if t.get("deadline_exact") else s + "≈"


def study_facts(now: datetime, tasks: list[dict] | None = None) -> tuple[str, list[dict]]:
    """待完成作业（+ 未开始的单独标注）。与 README 任务清单共用 actionable()，口径不会漂。

    链接紧跟在每条作业下面（不是集中堆在末尾）：飞书里一眼能对上是哪条作业的入口。
    v1.6.6：未开始的作业也列出来（用户澄清：要显示，只是状态区分）——标「未开始」，
    不计入待完成条数，也永远不会标「截止未知」（不是抓不到，是还没开放）。
    tasks 可注入（selftest 离线测「全部完成」场景），缺省读 data/study/tasks.json。
    """
    import render_readme as rr

    if tasks is None:
        tasks = load_list(DATA_DIR / "study" / "tasks.json")
    kept, _ = rr.actionable(tasks, now)
    kept = sort_by_deadline(kept)
    act = [t for t in kept if t.get("state") != "upcoming"]
    upc = [t for t in kept if t.get("state") == "upcoming"]
    header = f"待完成作业 {len(act)} 条（只算能做的：待完成且未逾期）"
    if upc:
        header += f"；另有 {len(upc)} 条未开始（老师开放后自动进入待办）"
    lines = [header + "。"]
    for t in act[:MORNING_TASK_LIMIT]:
        lines.append(f"- {t.get('course')} · {t.get('title')} ·「{_state_tag(t)}」· 截止 {_when(t)}")
        if t.get("url"):
            lines.append(f"  ↳ {t['url']}")
    for t in upc[:MORNING_TASK_LIMIT]:
        lines.append(f"- {t.get('course')} · {t.get('title')} ·「{_state_tag(t)}」· 暂不可做（未开始）")
        if t.get("url"):
            lines.append(f"  ↳ {t['url']}")
    if len(act) > MORNING_TASK_LIMIT:
        lines.append(f"（还有 {len(act) - MORNING_TASK_LIMIT} 条，全量在 README 任务清单）")
    return "\n".join(lines), kept


def notice_facts() -> tuple[str, list[dict]]:
    """值得关注的公告：相关度 >= 3，按分降序。与 README 公告节同一门槛。

    时效过滤（用户 2026-11-14：「时间都截至了还给我发」）：已过截止或无截止但
    发布超 90 天的不再推送。唯一判定在 `utils.notice_stale()`，与 README 共用。
    **飞书侧再加一道「本月起」闸（v1.7.0 用户 2026-11-16：「推送的都是过期的公告，
    改成只推送本月开始的公告」）**：publish_date 在本月 1 号之前的旧公告一律
    不进飞书推送（README/看板不受此闸，仍按自己的时效规则展示）。
    开始时间（用户 2026-11-15：「就把发布时间当作开始时间」）：每条直接以发布日期
    作为开始时间标注 —— 公告正文里几乎从不写「活动从哪天开始」，发布日就是
    学生能拿到的最早时间锚点，README 公告表的「日期」列同源（publish_date）。
    """
    from utils import notice_stale
    items = load_list(DATA_DIR / "notice" / "notices.json")
    month_start = now_bj().date().replace(day=1).isoformat()  # v1.7.0：飞书只推本月起的
    hot = [n for n in items
           if (n.get("score") or 0) >= NOTICE_MIN_SCORE
           and not notice_stale(n, now_bj().date())
           and (n.get("publish_date") or "9999-12-31") >= month_start]
    hot.sort(key=lambda n: -(n.get("score") or 0))
    lines = [f"值得关注的公告 {len(hot)} 条（相关度 {NOTICE_MIN_SCORE} 分以上、仍在有效期内）。"]
    for n in hot[:MORNING_NOTICE_LIMIT]:
        start = f"开始 {n['publish_date']}" if n.get("publish_date") else "开始时间未知"
        due = f"截止 {n['deadline']}" if n.get("deadline") else "暂无截止信息"
        why = n.get("reason") or n.get("category") or ""
        lines.append(f"- {n.get('title')} · {n.get('source')} · {start} · {due}"
                     + (f" · {why}" if why else ""))
        if n.get("url"):
            lines.append(f"  ↳ {n['url']}")
    return "\n".join(lines), hot


def daily_english() -> str:
    """金山词霸每日一句（免费无 key）。失败返回空串，不影响推送。

    每天全网就一条，重复请求返回同一条，所以不需要缓存也不会在同一天内重复打扰。
    """
    try:
        j = requests.get(ICIBA_URL, timeout=15).json()
        en = safe_text(j.get("content"), 200).strip()
        zh = safe_text(j.get("note"), 200).strip()
        if not en:
            return ""
        return f"{en}\n{zh}" if zh else en
    except Exception as e:
        log(f"每日英语获取失败：{type(e).__name__}", level="WARN")
        return ""


def weather_tomorrow(now: datetime) -> str:
    """open-meteo 明日天气（免费无 key）。失败返回空串，不影响推送。

    forecast_days=2 时 daily 数组是 [今天, 明天]，取第 2 个。
    """
    try:
        j = requests.get(WEATHER_URL, timeout=15).json()
        daily = j.get("daily") or {}
        tmax = (daily.get("temperature_2m_max") or [None, None])[1]
        tmin = (daily.get("temperature_2m_min") or [None, None])[1]
        rain = (daily.get("precipitation_probability_max") or [None, None])[1]
        if tmax is None or tmin is None:
            return ""
        out = f"河南城建学院 明天 {round(tmin)}~{round(tmax)}°C"
        if rain is not None:
            out += f"，降水概率 {rain}%"
            if isinstance(rain, (int, float)) and rain >= 60:
                out += "（记得带伞）"
        return out
    except Exception as e:
        log(f"明日天气获取失败：{type(e).__name__}", level="WARN")
        return ""


def quote_recall(today: date) -> str:
    """历史名言轮换（午安专用）：排除今天的，按天轮换取一条带 AI 点评。

    确定性：同一天同数据永远同一条（day-of-year 取模），重跑不漂移。
    晚安只报「今天新写的」，早报不报名言——三处互不重复（用户 2026-11-14）。
    """
    quotes = load_list(DIARY_DIR / "quotes.json")
    iso = today.isoformat()
    pool = [q for q in quotes if q.get("first_seen") != iso and q.get("text")]
    if not pool:
        return ""
    pool.sort(key=lambda q: (q.get("first_seen") or "", q.get("id") or ""))
    q = pool[(today.timetuple().tm_yday) % len(pool)]
    out = f"「{q.get('text')}」"
    if q.get("comment"):
        out += f"\n  点评：{q['comment']}"
    if q.get("first_seen"):
        out += f"\n  ——你 {q['first_seen']} 写下的"
    return out


def parse_ithome_rss(xml_bytes: bytes, now: datetime,
                     limit: int = HN_TOP_LIMIT) -> list[dict]:
    """解析 IT之家 RSS（标准 RSS 2.0，UTF-8）。纯函数，selftest 用合成 XML 离线测。

    只留 TECH_NEWS_HOURS 小时内发布的条目（科技快讯要新鲜，旧的不算「今日热点」）。
    """
    import xml.etree.ElementTree as ET
    from email.utils import parsedate_to_datetime
    root = ET.fromstring(xml_bytes)
    out = []
    for item in root.iter("item"):
        title = clean_html_text(item.findtext("title") or "")
        link = (item.findtext("link") or "").strip()
        pub_raw = item.findtext("pubDate") or ""
        pub = None
        try:
            pub = parsedate_to_datetime(pub_raw.strip())
        except (TypeError, ValueError):
            pub = None
        if not title or not link:
            continue
        if pub is not None:
            pub = pub.astimezone(BJ_TZ)
            if (now - pub).total_seconds() > TECH_NEWS_HOURS * 3600:
                continue  # 超过 24 小时的旧资讯不算「今日热点」
        out.append({"title": safe_text(title, 120).strip(), "url": link, "pub": pub})
        if len(out) >= limit:
            break
    return out


def format_tech_news(items: list[dict]) -> str:
    """格式化科技热点节。纯函数，selftest 可离线测。"""
    if not items:
        return ""
    lines = []
    for i, p in enumerate(items[:HN_STORY_COUNT], 1):
        lines.append(f"{i}. {p.get('title') or ''}")
        if p.get("url"):
            lines.append(f"   ↳ {p['url']}")
    return "\n".join(lines)


def tech_news() -> str:
    """傍晚档科技热点：IT之家 RSS 最新 3 条（中文，24h 内）。失败返回空串，不阻断推送。

    用户 2026-11-15 定调要中文科技报；IT之家标题即中文，**零 AI 依赖**（比 HN 翻译
    少一层失败面）。换源记录：HN（英文+AI 翻译）只在 v1.5.19 存活半天即被替换。
    """
    try:
        r = requests.get(ITHOME_RSS, timeout=15,
                         headers={"User-Agent": "Mozilla/5.0 (compatible; personal-assistant)"})
        r.raise_for_status()
        items = parse_ithome_rss(r.content, now_bj())
    except Exception as e:
        log(f"IT之家 RSS 获取失败：{type(e).__name__}", level="WARN")
        return ""
    out = format_tech_news(items)
    if out:
        log(f"[tech] 科技热点 {min(len(items), HN_STORY_COUNT)} 条（IT之家，24h 内）")
    return out


PLANS_MORNING_LIMIT = 5


def plans_block(plans: list[dict], limit: int = PLANS_MORNING_LIMIT) -> str:
    """潜在任务块（用户 2026-11-15：「作业全部完成时，就推送我的潜在任务，来自
    我的 obsidian 笔记」）。纯函数，selftest 可离线测。

    口径与 README「潜在计划」节一致：state=open，按置信度降序、截止日升序。
    计划的常规播报仍然撤除（v1.5.5 的约定），这个块**只在作业全部完成时**作为
    「没事干？干这个」的填充出现——且只显示 text/confidence，不播报详情细节。
    """
    open_plans = [p for p in plans if p.get("state") == "open" and p.get("text")]
    if not open_plans:
        return ""
    open_plans.sort(key=lambda p: (-(p.get("confidence") or 0),
                                   p.get("due_date") or "9999-12-31"))
    lines = []
    for p in open_plans[:limit]:
        due = f"（截止 {p['due_date']}）" if p.get("due_date") else ""
        lines.append(f"- {p.get('text')}{due}")
    lines.append("（全量见 README「潜在计划」节）")
    return "\n".join(lines)


def urgent6_pick(now: datetime, ledger: dict, tasks: list | None = None) -> tuple[str, dict]:
    """6 小时紧急截止提醒（用户 2026-11-14 授权的独立通道）。

    - 只挑「待完成且未逾期、剩余 < URGENT_HOURS」的作业（复用 README 同口径 actionable）；
    - 记账 ledger = {f"{task_id}@{deadline}": 提醒日期}，**同一截止时间只提醒一次**，
      不被三时段的「每天一次」规则限制，也不占用它们的清单名额；
      键里带 deadline 是 v1.5.12 修的漏洞：老师把截止时间往后改（延期）时，
      同一 task_id 进入新的 6h 窗口必须**重新提醒一次**——旧键只记 id 会把它吞掉；
    - ledger 里提醒日期超过 30 天的旧记录顺手清掉（tasks.json 本身有淘汰，
      不清的话账本只增不减，慢性膨胀）；
    - 返回 (通知文案, 新记账)。没有要提醒的就返回 ("", 清理后的记账)。
    tasks 可注入（selftest 离线测），缺省读 data/study/tasks.json。
    """
    if tasks is None:
        tasks = load_list(DATA_DIR / "study" / "tasks.json")
    import render_readme as rr
    kept, _ = rr.actionable(tasks, now)
    kept = sort_by_deadline(kept)
    iso = now.date().isoformat()
    # 清理 30 天前的旧账：值是提醒日期，早就不可能再有同一个 6h 窗口
    cutoff = (now - timedelta(days=30)).date().isoformat()
    ledger = {k: v for k, v in ledger.items() if isinstance(v, str) and v >= cutoff}
    hits = []
    for t in kept:
        # v1.6.6：未开始（upcoming）做不了，不崔——紧急提醒只认能马上动手的 pending
        if t.get("state") != "pending":
            continue
        key = f"{t.get('id')}@{t.get('deadline')}"
        if key in ledger:
            continue
        dl = parse_datetime(t.get("deadline"))
        if not dl:
            continue
        remain_h = (dl - now).total_seconds() / 3600
        if 0 < remain_h <= URGENT_HOURS:
            hits.append((t, remain_h, key))
    if not hits:
        return "", ledger
    hits.sort(key=lambda x: x[1])
    lines = [f"⏰ 紧急：{len(hits)} 条作业 {URGENT_HOURS} 小时内截止，还没交："]
    for t, remain_h, _key in hits:
        m = max(1, int(remain_h * 60))
        lines.append(f"- {t.get('course')} · {t.get('title')} · "
                     f"剩约 {m // 60} 小时 {m % 60} 分钟")
        if t.get("url"):
            lines.append(f"  ↳ {t['url']}")
    return "\n".join(lines), {**ledger, **{key: iso for _t, _r, key in hits}}


def new_task_pick(now: datetime, ledger: dict, tasks: list | None = None) -> tuple[str, dict]:
    """新作业发布提醒（用户 2026-11-16 授权的第三条独立通道）。

    - 「新发布」= 任务首次出现在数据里（first_seen 是 merge 层打的，每条任务
      整个生命周期只会打一次）且 state==pending——已完成/已逾期的旧任务是噪音；
    - 双重闸门：NEW_SINCE 之前发现的存量不追溯（上线当天不轰炸）；
      首次发现超过 NEW_WINDOW_HOURS 的不再当「新」（停摆恢复后的补报上限）；
    - 记账 ledger = {task_id: 通知日期}，**每条任务只通知一次**，独立于
      三时段的「每天一次」与 urgent6 的 6h 窗口——三条通道互不挤占；
    - ledger 里通知日期超过 30 天的旧记录顺手清掉（tasks.json 本身有淘汰，
      不清的话账本只增不减，慢性膨胀）；
    - 返回 (通知文案, 新记账)。没有新任务就返回 ("", 清理后的记账)。
    tasks 可注入（selftest 离线测），缺省读 data/study/tasks.json。
    """
    if tasks is None:
        tasks = load_list(DATA_DIR / "study" / "tasks.json")
    iso = now.date().isoformat()
    cutoff = (now - timedelta(days=NEW_LEDGER_DAYS)).date().isoformat()
    ledger = {k: v for k, v in ledger.items() if isinstance(v, str) and v >= cutoff}
    hits: list[dict] = []
    seen_tids: set[str] = set()
    for t in tasks:
        tid = t.get("id")
        if not tid or tid in ledger or tid in seen_tids:
            continue
        if t.get("state") not in ("pending", "upcoming"):
            continue
        fs = parse_datetime(t.get("first_seen"))
        if not fs or fs < NEW_SINCE:
            continue
        if (now - fs).total_seconds() > NEW_WINDOW_HOURS * 3600:
            continue
        # v1.6.6：首次发现就已逾期 → 历史课次/旧任务，不是「新发布」，不报
        dl = parse_datetime(t.get("deadline"))
        if dl and dl < now:
            continue
        seen_tids.add(tid)
        hits.append(t)
    if not hits:
        return "", ledger
    # 新的排前面（一条消息里最新鲜的在最上方）
    hits.sort(key=lambda t: parse_datetime(t.get("first_seen")) or now, reverse=True)
    lines = [f"🆕 新作业发布：首次检测到 {len(hits)} 条"]
    for t in hits[:NEW_MAX_PER_MSG]:
        dl = parse_datetime(t.get("deadline"))
        when = dl.strftime("%m-%d %H:%M") if dl else "截止未知"
        if t.get("state") == "upcoming":
            when = f"未开始（{when}）" if dl else "未开始"
        label = t.get("label") or ""
        label_part = f" · {label}" if label else ""
        lines.append(f"- {t.get('course')} · {t.get('title')}{label_part} · 截止 {when}")
        if t.get("url"):
            lines.append(f"  ↳ {t['url']}")
    if len(hits) > NEW_MAX_PER_MSG:
        lines.append(f"（另有 {len(hits) - NEW_MAX_PER_MSG} 条未列出，见 README 任务清单）")
    return "\n".join(lines), {**ledger, **{t["id"]: iso for t in hits}}


# v1.6.6：新作业推送的降级开头（AI 挂了也要发，人设不变）
NEWTASK_FALLBACK = ("新作业来了。别装看不见——清单我列在下面了，"
                    "截止时间和链接自己点。哼，早动手早轻松。")


def run_newtask_notice(plain_msg: str, now: datetime | None = None, *,
                       ai_client=None, fetch_extras: bool = True,
                       hit: str | None = None, en: str | None = None) -> str:
    """把 new_task_pick 的清单包装成与早中晚同款的推送文案。

    用户 2026-11-16 要求：「发送的风格要和之前早中晚常规的信息保持一样，
    卡片类似的，每日英语，一言和图片」。

    - AI 只写开头人话（复用四时段的 PUSH_RULES 人设），清单由 new_task_pick
      确定性拼在后面——**人话与清单分离**的分工原则与四时段完全一致；
    - 图片：anime_image.run() 落盘 /tmp/push-image.json，飞书步骤读到后自动
      拼成与四时段同款的卡片（🆕 标题 + image_key 内嵌真图/按钮）；
    - 所有联网环节失败都降级：AI 挂→固定人设开头；英语/一言/图挂→缺哪跳哪，
      消息一定发得出去；
    - ai_client / hit / en 可注入、fetch_extras=False 跳过全部外网（selftest 用）。
    """
    now = now or now_bj()
    if fetch_extras:
        if hit is None:
            hit = hitokoto()
        if en is None:
            en = daily_english()
        try:
            import anime_image
            anime_image.run()
        except Exception as e:
            log(f"新作业推送取图异常：{type(e).__name__}，本次不带图", level="WARN")
    hit, en = hit or "", en or ""

    prose = ""
    client = ai_client
    if client is None and fetch_extras:
        try:
            from ai_client import AIClient
            client = AIClient(timeout=90)
        except Exception as e:
            log(f"新作业推送 AI 不可用：{type(e).__name__}", level="WARN")
    if client is not None:
        try:
            from prompts import newtask_messages
            prose = safe_text(client.chat_text(newtask_messages(plain_msg, hit),
                                               temperature=0.8, max_tokens=800),
                              400).strip()
        except Exception as e:
            log(f"新作业推送文案 AI 生成失败：{type(e).__name__}", level="WARN")
    if not prose:
        prose = NEWTASK_FALLBACK
        log("新作业推送：AI 无输出，用固定人设开头", level="WARN")

    parts = [prose, "", plain_msg]
    if en:
        parts += ["", "🇬🇧 今日英语一句\n" + en]
    if hit:
        parts += ["", f"💬 今日一言：{hit}"]
    text = "\n\n".join(parts)
    log(f"[newtask] 卡片文案 {len(text)} 字："
        f"AI{'有' if prose != NEWTASK_FALLBACK else '降级'}，"
        f"英语{'有' if en else '无'}，一言{'有' if hit else '无'}")
    return text


def diary_facts(kind: str, today: date) -> str:
    """把日记数据整理成确定性事实清单。AI 只准用这里给出的信息，不许编造。

    ⚠️ 2026-11-14 用户要求：「这个随口写的不要发到飞书里，这个我有其他解决方案」——
    潜在计划（plans.json）不再播报进任何飞书消息。计划数据仍进 README 任务区与
    docs/潜在计划.md（网页/仓库里自己看），日记写回闭环也不受影响。
    这里只剩「今天的名言」一类内容。
    """
    quotes = load_list(DIARY_DIR / "quotes.json")
    iso = today.isoformat()
    quotes_today = [q for q in quotes if q.get("first_seen") == iso]

    lines = [f"今天是 {iso} {WEEKDAY_CN[today.weekday()]}。"]
    # 今日名言只在晚（night）档——三路不重复原则（v1.5.19 起 dusk 也排除）
    if kind == "night" and quotes_today:
        lines.append("今天从你日记里摘出来的话：")
        for q in quotes_today[:12]:
            lines.append(f"- 「{q.get('text')}」")
            if q.get("comment"):
                lines.append(f"  点评：{q['comment']}")
    return "\n".join(lines)


def task_overview(now: datetime, tasks: list[dict] | None = None) -> str:
    """给 AI 的一行作业概览事实（v1.7.1，用户 2026-11-17 报）。

    之前四时段的 AI prose 只拿到日记事实，**根本不知道作业做没做完**——
    傍晚档作业清零时它无话可说、也不夸。现在把确定性概览注入 facts：
    「还剩 X 条要交，今天截止 Y 条（已全部完成）/未开始 W」，
    AI 才有依据表态/夸奖。纯函数，selftest 可注入 tasks 离线测。
    """
    import render_readme as rr

    if tasks is None:
        tasks = load_list(DATA_DIR / "study" / "tasks.json")
    kept, _ = rr.actionable(tasks, now)
    act = [t for t in kept if t.get("state") != "upcoming"]
    upc = len(kept) - len(act)
    day_end = now.replace(hour=23, minute=59, second=59)
    today_n = sum(1 for t in act
                  if t.get("deadline")
                  and now <= (parse_datetime(t["deadline"]) or now) <= day_end)
    week_n = sum(1 for t in act
                 if t.get("deadline")
                 and day_end < (parse_datetime(t["deadline"]) or now) <= now + timedelta(days=7))
    parts = [f"作业概况：还需要动手的 {len(act)} 条"]
    if today_n:
        parts.append(f"其中今天截止 {today_n} 条")
    elif not act:
        parts.append("作业已全部做完（待完成 0 条）")
    else:
        parts.append("今天截止的已全部完成")
    if week_n:
        parts.append(f"7 天内还有 {week_n} 条")
    if upc:
        parts.append(f"另有 {upc} 条未开始（暂不可做）")
    head = "，".join(parts) + "。"
    # v2.4（用户 2026-11-27：通知里必须列出具体作业条目+链接，只有数字没法行动）
    lines = [head]
    if act:
        lines.append("📋 待做清单：")
        for t in act:
            lines += _task_line(t)
    if upc:
        upc_ts = [t for t in kept if t.get("state") == "upcoming"]
        if upc_ts:
            lines.append("⏳ 未开始：")
            for t in upc_ts[:12]:
                lines += _task_line(t)
    return "\n".join(lines)


# v1.7.2：飞书清单里每条作业的状态标注（用户 2026-11-17 要求：「12 点那一条的
# 作业截止提醒，需要告诉我作业是什么状态，比如待完成、待互改」）。
# 用学习通原文 status（待做/待互评/未开始/已退回…）——比 state 枚举翻译更准；
# 没有原文时按 state 映射兑底。
_STATE_FALLBACK = {"pending": "待完成", "submitted": "待批阅", "done": "已完成",
                  "upcoming": "未开始", "unknown": "状态未知"}


def _state_tag(t: dict) -> str:
    """一条作业的状态标签（飞书清单行用）。优先学习通原文 status。"""
    s = (t.get("status") or "").strip()
    return s or _STATE_FALLBACK.get(t.get("state") or "", "状态未知")


def _task_line(t: dict) -> list[str]:
    """一条作业两行：条目（含状态）+ 可点链接。与 study_facts 的排版一致。

    v1.7.2：条目里加状态标注（如「待互评」/「待做」/「未开始」）——用户明确
    要在截止提醒里看到每条作业的状态，区分哪些要写哪些要去互评。
    """
    out = [f"- {t.get('course')} · {t.get('title')} ·「{_state_tag(t)}」· 截止 {_when(t)}"]
    if t.get("url"):
        out.append(f"  ↳ {t['url']}")
    return out


def _actionable_sorted(now: datetime, tasks: list | None = None) -> list[dict]:
    """能做的作业（待完成且未逾期），按截止近→远。与 README 任务清单同口径。"""
    import render_readme as rr
    if tasks is None:
        tasks = load_list(DATA_DIR / "study" / "tasks.json")
    kept, _ = rr.actionable(tasks, now)
    return sort_by_deadline(kept)


def info_block(kind: str, now: datetime, hit: str, tech_provider=None,
               tasks: list[dict] | None = None) -> str:
    """确定性清单，拼在 AI prose 后面。**不经过 AI**，保证用户点名的内容一条不漏。

    分工（用户 2026-11-14：「作业部分可以重复」+ 三时段增值）：
    早 = 作业总览 + 公告总览（公告只在早报）；
    午 = 今日新发作业 + 7 天内截止 + 每日英语 + 名言回顾；
    晚 = 48h 截止未交 + 本周盘点 + 明日预告 + 明早天气 + 当天日记名言（在 diary_facts）。
    另有独立的 6h 紧急提醒通道（urgent6_pick），不受三时段「每天一次」限制。
    """
    parts: list[str] = []
    today_iso = now.date().isoformat()
    if kind == "morning":
        sf, kept_m = study_facts(now, tasks)
        if kept_m:
            parts.append("📚 待完成作业\n" + sf)
        else:
            # 用户 2026-11-15：「全部都完成了的话，就推送我的潜在任务，来自我的
            # obsidian 笔记」——没作业可做的日子，用他自己的长期计划填满这一格
            block = plans_block(load_list(DIARY_DIR / "plans.json"))
            if block:
                parts.append("🎉 待完成作业全部清空！来干点长期的事：\n"
                             "🎯 潜在任务（来自你的 Obsidian 笔记）\n" + block)
        nf, _ = notice_facts()
        parts.append("📢 值得关注的公告\n" + nf)
    elif kind == "noon":
        kept = _actionable_sorted(now, tasks)
        all_tasks = tasks if tasks is not None else load_list(DATA_DIR / "study" / "tasks.json")
        fresh = [t for t in all_tasks
                 if t.get("first_seen") == today_iso and t.get("state") == "pending"]
        if fresh:
            lines = [f"🆕 今天新发的作业 {len(fresh)} 条："]
            for t in fresh[:12]:
                lines += _task_line(t)
            if len(fresh) > 3:
                lines.append(f"（还有 {len(fresh) - 3} 条，全量在 README）")
            parts.append("\n".join(lines))
        week = [t for t in kept
                if t.get("deadline")
                and (parse_datetime(t["deadline"]) or now) <= now + timedelta(days=7)]
        if week:
            lines = [f"⏳ 7 天内截止 {len(week)} 条："]
            for t in week[:12]:
                lines += _task_line(t)
            if len(week) > 3:
                lines.append(f"（还有 {len(week) - 3} 条，全量在 README）")
            parts.append("\n".join(lines))
        en = daily_english()
        if en:
            parts.append("🇬🇧 今日英语一句\n" + en)
        recall = quote_recall(now.date())
        if recall:
            parts.append("📖 名言回顾（你日记里的话）\n" + recall)
    elif kind == "dusk":
        # v1.5.19 傍晚冲刺：今天 24:00 前截止的作业（全天最后一次「还来得及」）
        # + IT之家科技热点（本档独有）。不含英语/名言回顾（午安专属）与天气（晚安专属）。
        # v1.7.1（用户 2026-11-17 报）：没有作业时也要明说并夸他——空清单不是
        # 直接跳过，而是确定性写「全部搞定」，AI 挂了这条也在。
        kept = _actionable_sorted(now, tasks)
        day_end = now.replace(hour=23, minute=59, second=59)
        today = [t for t in kept
                 if t.get("deadline")
                 and now <= (parse_datetime(t["deadline"]) or now) <= day_end]
        if today:
            lines = [f"🔥 今天 24:00 前截止还没交的 {len(today)} 条："]
            for t in today[:12]:
                lines += _task_line(t)
            if len(today) > 3:
                lines.append(f"（还有 {len(today) - 3} 条，全量在 README）")
            parts.append("\n".join(lines))
        else:
            parts.append("🎉 今天截止的作业：**全部搞定，0 条要赶**。\n"
                         "该交的都交了，今晚不用再惦记作业——哼，千得漂亮。")
        # tech_provider 可注入（selftest 离线确定性）；缺省联 IT之家 RSS
        tn = (tech_provider or tech_news)()
        if tn:
            parts.append("📰 今日科技热点\n" + tn)
    else:
        kept = _actionable_sorted(now, tasks)
        soon = [t for t in kept
                if t.get("deadline")
                and (parse_datetime(t["deadline"]) or now) <= now + timedelta(hours=48)]
        if soon:
            lines = [f"🔥 48 小时内截止还没交的 {len(soon)} 条："]
            for t in soon[:12]:
                lines += _task_line(t)
            if len(soon) > 3:
                lines.append(f"（还有 {len(soon) - 3} 条，全量在 README）")
            parts.append("\n".join(lines))
        all_tasks = tasks if tasks is not None else load_list(DATA_DIR / "study" / "tasks.json")
        monday_iso = (now.date() - timedelta(days=now.date().weekday())).isoformat()
        this_week = [t for t in all_tasks if (t.get("first_seen") or "") >= monday_iso]
        done_w = [t for t in this_week if t.get("state") != "pending"]
        open_w = [t for t in this_week if t.get("state") == "pending"]
        parts.append(f"📊 本周新发作业 {len(this_week)} 条：已交/处理 {len(done_w)} 条，"
                     f"还压着 {len(open_w)} 条。")
        tomorrow_iso = (now.date() + timedelta(days=1)).isoformat()
        due_tmr = [t for t in kept
                   if t.get("deadline")
                   and (parse_datetime(t["deadline"]) or now).date().isoformat() == tomorrow_iso]
        if due_tmr:
            lines = [f"📅 明天截止 {len(due_tmr)} 条："]
            for t in due_tmr[:2]:
                lines.append(f"- {t.get('course')} · {t.get('title')}")
            parts.append("\n".join(lines))
        w = weather_tomorrow(now)
        if w:
            parts.append(f"🌤 明早天气：{w}")
    if hit:
        parts.append(f"💬 今日一言：{hit}")
    return "\n\n".join(parts)


def fallback_message(kind: str, facts: str, hit: str) -> str:
    """AI 挂了也要发。语气保持人设，但内容全部来自确定性事实。"""
    head = {
        "morning": "早。哼，别以为我不说话你就能赖床。今天的事都在下面，自己看。",
        "noon": "中午了。吃饭没？别又拿零食糊弄自己。下午该动的就动，别等我晚上再骂你。",
        "dusk": "傍晚了。今天还剩几小时，来得及的事现在动手，别等我晚上来数落你。",
        "night": "今天就这样了。做完的算你的，没做的明天别想赖掉。啧，我可不是在担心你。",
    }[kind]
    parts = [head, "", facts]
    if hit:
        parts += ["", f"今日一言：{hit}"]
    return "\n".join(parts)


def run(kind: str) -> dict:
    if kind not in KINDS:
        log(f"未知推送时段：{kind}", level="ERROR")
        return {"ok": False}

    now = now_bj()
    today = now.date()
    facts = diary_facts(kind, today)
    hit = hitokoto()
    # v1.7.1：作业概览注入 AI 事实——之前的 prose 只有日记事实，作业清零时 AI
    # 不知道也不夸（用户 2026-11-17 报）。失败只告警，不给旧 facts 开天窗。
    try:
        facts = facts + "\n" + task_overview(now)
    except Exception as e:
        log(f"作业概览注入失败（AI 将拿不到作业事实）：{type(e).__name__}", level="WARN")

    # 三时段都带图（用户 2026-11-14 推翻了「只有早上带图」的旧约定：
    # 「发送格式全部都按照早上的格式发，图片改成早中晚都发」）。
    # 取图失败只 WARN 不阻断推送；JS 侧有图 JSON 就发卡片，格式自动与早报一致。
    image = None
    try:
        import anime_image
        image = anime_image.run()
    except Exception as e:
        log(f"取图异常：{type(e).__name__}，本次不带图", level="WARN")

    prose = ""
    ai_ok = False
    try:
        from ai_client import AIClient
        from prompts import push_messages
        client = AIClient(timeout=90)
        prose = safe_text(client.chat_text(push_messages(kind, facts, hit),
                                           temperature=0.8, max_tokens=2000), 600).strip()
        ai_ok = bool(prose)
    except Exception as e:
        log(f"推送文案 AI 生成失败：{type(e).__name__}", level="WARN")

    if not ai_ok:
        # 一言不在这里给：info_block() 末尾已经拼了「💬 今日一言」，
        # 再传一次会让降级消息里出现两条一模一样的话。
        prose = fallback_message(kind, facts, "")
        log("AI 无输出，改用确定性降级文案", level="WARN")

    block = info_block(kind, now, hit)
    text = prose + ("\n\n" + block if block else "")
    MESSAGE_PATH.write_text(text, encoding="utf-8")
    log(f"[push] {kind} 文案 {len(text)} 字，一言{'有' if hit else '无'}，"
        f"图{'有' if image else '无'}，文案来源：{'AI' if ai_ok else '降级'}")
    return {"ok": True, "kind": kind, "chars": len(text),
            "hitokoto": bool(hit), "ai": ai_ok, "image": bool(image)}


if __name__ == "__main__":
    import sys
    run(sys.argv[1] if len(sys.argv) > 1 else "morning")