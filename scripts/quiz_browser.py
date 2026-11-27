# -*- coding: utf-8 -*-
"""学习通浏览器采集层 v2.0（2026-11-26 本地全量验证版移植）。

四条链路（全部经本地 14 门课实证）：
① 作业/考试列表：mooc1 iframe（work/list、exam-list）多行块解析 + 分页；
② 随堂活动：stuActiveList iframe 块解析 + 签到/课件/讨论自动分类；
③ 知识点总结类状态：li[activeId] → activeRowClick → iframe 跳详情读状态词；
④ 分组任务（type=35）：getActiveInfoForMe API 选分支 →
   taskDetailStuAndAnsDetail_new 详情页提取得分/提交时间。

**关键事实**：学生 cpi 全课程共用（见 sources.json 的 xxt_cpi）；stat2
统计页对 cpi 缺失返回 30 字符空页（曾误判为「限流」）。

对外接口（study_collector 使用）：
- cookies_for_browser / cookies_from_session（v1.7.2 原样保留）
- classify_quiz / parse_quiz_time（v1.7.2 原样保留）
- collect_course(course, mid_url, cookies, ua) → 单课成品任务列表
- fetch_work_stat(course, cookies, ua, cpi) → stat2 统计页行（Playwright 渲染）
纯函数（selftest 直接测）：classify_category / parse_active_blocks /
parse_group_detail / parse_stat_rows
"""
from __future__ import annotations

import json
import re
from datetime import datetime

from utils import BJ_TZ, make_id, parse_remaining, safe_text, scrub_task_title

# ---------------------------------------------------------------------------
# cookies（v1.7.2 原样保留）
# ---------------------------------------------------------------------------
def cookies_for_browser(session) -> list[dict]:
    """requests cookie jar → playwright add_cookies。域归一化 + 按名去重。

    requests 里学习通的 cookie 多为 host-only（domain=mooc1.huuc.edu.cn 等
    具体子域），host-only cookie 只发给那个确切主机 → 注入浏览器后
    mobilelearn.huuc.edu.cn 拿不到登录态。归一化成 .huuc.edu.cn 父域 cookie
    （真实用户浏览器里就是这个形态：登录后全子域共享）。
    只收 huuc.edu.cn 域的 cookie，其他站的一律不带；
    同名 cookie（多个子域各存一份同值）按名去重，值冲突取后写者（同一用户会话）。
    """
    by_name: dict[str, dict] = {}
    for c in session.cookies:
        dom = (c.domain or "").lstrip(".")
        # v2.6：chaoxing.com 域也收（type=77 的 load-data API 在该域，
        # fanyalogin 统一登录会种全家族 cookie）
        if dom.endswith("huuc.edu.cn"):
            by_name[c.name] = {"name": c.name, "value": c.value or "",
                               "domain": ".huuc.edu.cn", "path": c.path or "/"}
        elif dom.endswith("chaoxing.com"):
            by_name[f"cx.{c.name}"] = {"name": c.name, "value": c.value or "",
                                       "domain": ".chaoxing.com",
                                       "path": c.path or "/"}
    return list(by_name.values())


def cookies_from_session(session) -> list[dict]:
    """兼容旧名（v1.5.22 接口，selftest/文档引用过）。"""
    return cookies_for_browser(session)


# ---------------------------------------------------------------------------
# 状态词表与分类（v1.7.2 词表 + 本地验证的分类规则）
# ---------------------------------------------------------------------------
QUIZ_DONE = {"已完成", "已参与", "已提交", "已结束", "已交", "已答", "已互评", "已过期",
             "已评分", "最终得分", "总分"}
QUIZ_SUBMITTED = {"待批阅", "批阅中", "待批改", "批改中"}
QUIZ_PENDING = {"待做", "未做", "未交", "未提交", "待完成", "进行中", "未参与",
                "待参与", "待作答", "未答", "已发布", "待开始"}


def classify_quiz(status: str) -> str:
    s = (status or "").strip()
    if s == "未开始":
        # v1.6.6：未开始单独判 upcoming（显示在清单、标「未开始」，
        # 不进待完成/urgent6）
        return "upcoming"
    if s in QUIZ_DONE:
        return "done"
    if s in QUIZ_SUBMITTED:
        return "submitted"
    if s in QUIZ_PENDING:
        return "pending"
    for w in QUIZ_DONE:
        if w in s and ("分" in s or w in ("最终得分", "总分", "已提交")):
            return "done"
    for w in QUIZ_SUBMITTED:
        if w in s:
            return "submitted"
    for w in QUIZ_PENDING:
        if w in s:
            return "pending"
    return "unknown"


# 分类正则（本地 14 门课实证；讨论/提问只拦整词防误伤长标题）
CLASSROOM_RE = re.compile(r"(签到|选人|投票|抢答|问卷)")
CLASSROOM_EXACT_RE = re.compile(r"^(?:主题)?讨论$|^提问$")
MATERIAL_RE = re.compile(
    r"\.(pptx?|pdf|docx?|xlsx?|zip|rar|mp4|avi|mkv|png|jpe?g)\s*$", re.I)
_GROUP_TAG = re.compile(r"(进行中|已结束|未提交|已提交)\s*[（(]\d+[)）]\s*$")
GROUP_TAG = re.compile(r"(进行中|已结束|未提交|已提交)\s*[（(]\d+[)）]\s*$")
NOISE_TITLE_RE = re.compile(r"^[\d（）()：:、\s]*$")
_TIME_LINE_RE = re.compile(r"(结束时间|开始时间|截止时间|发布时间|剩余)")
# 时间正则（v1.7.2 原样保留）：机器层「2026-11-03 19:33 / 11-03 19:33」
# 与中文显示层「11月03日 19时33分」（run 43805479954 真实样本）
_MDHM_RE = re.compile(r"(?:(\d{4})[-/年])?(\d{1,2})[-/月](\d{1,2})日?\s*(\d{1,2}):(\d{2})")
_MDHM2_RE = re.compile(r"(\d{1,2})月(\d{1,2})日\s*(\d{1,2})时(\d{1,2})分")


def classify_category(title: str) -> str:
    """活动条目分类：quiz（作业）/ classroom（课堂互动）/ material（课件）。

    用户定论：签到/选人/投票/抢答/问卷/整词「讨论/提问」是课堂互动不是作业；
    .pptx 等课件是老师发的材料；其余默认随堂作业。
    """
    t = (title or "").strip()
    if re.fullmatch(r"(?:主题)?讨论|提问", t):
        return "classroom"
    if CLASSROOM_RE.search(t):
        return "classroom"
    if MATERIAL_RE.search(t):
        return "material"
    return "quiz"


def _time_like(l: str) -> bool:
    return bool(re.search(r"(结束时间|开始时间|截止时间|发布时间|\d{1,2}月\d{1,2}日|\d{1,2}-\d{1,2}\s+\d{1,2}:\d{1,2})", l))


def parse_quiz_time(text: str, now: datetime) -> tuple[str | None, bool]:
    """随堂行的时间文本 → (deadline, 是否精确)。v1.7.2 原样保留。

    三种形状（真实样本）：「剩余2天3小时」倒计时、「11月03日 19时33分」
    中文显示层、「2026-11-03 19:33」机器层（带冒号）。不带年份的按距
    now 最近的一年补（学习通详情页同款规则）；带年份的视为精确。
    都不是 → (None, False)：诚实留空，绝不冒充「很宽裕」。
    """
    t = (text or "").strip()
    if not t:
        return None, False
    rem = parse_remaining(t)
    if rem is not None:
        return (now + rem).isoformat(timespec="seconds"), False

    def _nearest(mo: str, d: str, h: str, mi: str) -> tuple[str | None, bool]:
        best: datetime | None = None
        for year in (now.year, now.year - 1, now.year + 1):
            try:
                cand = datetime(year, int(mo), int(d), int(h), int(mi),
                                tzinfo=now.tzinfo)
            except ValueError:
                continue
            if best is None or abs((cand - now).total_seconds()) < \
                    abs((best - now).total_seconds()):
                best = cand
        if best is None:
            return None, False
        return best.isoformat(timespec="seconds"), False

    m2 = _MDHM2_RE.search(t)
    if m2:
        return _nearest(m2.group(1), m2.group(2), m2.group(3), m2.group(4))
    m = _MDHM_RE.search(t)
    if not m:
        return None, False
    y, mo, d, h, mi = m.groups()
    if y:
        try:
            return datetime(int(y), int(mo), int(d), int(h), int(mi),
                            tzinfo=now.tzinfo).isoformat(timespec="seconds"), True
        except ValueError:
            return None, False
    return _nearest(mo, d, h, mi)


# ---------------------------------------------------------------------------
# JS 提取脚本（本地验证版）
# ---------------------------------------------------------------------------
LIST_ITEM_JS = """
() => {
  const out = [];
  for (const el of document.querySelectorAll('li, .listItem, .item, [onclick]')) {
    const t = (el.innerText || '').trim();
    if (t && t.length > 5 && t.length < 500 && t.includes('\\n')) out.push(t);
  }
  return [...new Set(out)];
}
"""

HOME_CARD_JS = """
() => {
  const out = [];
  const seen = new Set();
  for (const el of document.querySelectorAll('span[class*="color-"], [class*="icon-"]')) {
    const txt = (el.innerText || '').trim();
    if (!txt || txt.length > 40 || seen.has(txt)) continue;
    seen.add(txt);
    let card = el.parentElement, ctx = '';
    for (let i = 0; i < 6 && card; i++) {
      const t = (card.innerText || '').trim();
      if (t && t.length > txt.length + 10) { ctx = t; break; }
      card = card.parentElement;
    }
    out.push({status: txt.slice(0, 40), ctx: ctx.slice(0, 200)});
  }
  return out;
}
"""

CLICK_NEXT_JS = """
() => {
  const els = [...document.querySelectorAll('a, button, li, span, i')];
  const usable = el => {
    if (el.disabled) return false;
    const cl = String(el.className || '');
    if (/disabled|gray|grey/i.test(cl)) return false;
    if (el.getAttribute('aria-disabled') === 'true') return false;
    if (el.offsetParent === null && getComputedStyle(el).display === 'none') return false;
    const t = (el.innerText || '').trim();
    return /^(>|下一页|下页)/i.test(t) || /next|pager-next/i.test(cl);
  };
  const hit = els.filter(usable);
  if (!hit.length) return false;
  hit[hit.length - 1].click();
  return true;
}
"""

ROWCLICK_HOOK_JS = """
() => {
  if (typeof activeRowClick !== 'function') return null;
  window.__origOpen = window.open;
  window.__opened = [];
  window.open = function(u, n, f) {
    window.__opened.push(String(u));
    try { return window.__origOpen.call(window, u, n, f); } catch (e) { return null; }
  };
  return true;
}
"""

STAT_PAGE_JS = "() => (document.body.innerText || '')"


# ---------------------------------------------------------------------------
# 纯函数：块解析 / 分类 / 分组任务详情 / 统计页行（selftest 直接测）
# ---------------------------------------------------------------------------
def _bad_title(title: str) -> bool:
    t = (title or "").strip()
    return (not t or NOISE_TITLE_RE.fullmatch(t) or GROUP_TAG.fullmatch(t)
            or re.fullmatch(r"^《[^《》]{4,}》$", t) or len(t) > 50)


def parse_active_blocks(blocks: list[str], course: dict, now: datetime) -> list[dict]:
    """iframe 多行块 → 任务列表（本地 xxt_collect 实证版）。

    每个块是 li/卡片的 innerText（标题/时间/状态多行）。解析出 title/status/
    time_hint，按 classify_quiz 归状态、classify_category 归类。
    输出字段与 merge_quiz 兼容（id/kind/course/title/status/state/deadline/url）。
    """
    tasks: list[dict] = []
    seen: set[str] = set()
    for b in blocks or []:
        lines = [l.strip() for l in (b or "").splitlines() if l.strip()]
        if not lines or len(lines) > 12:
            continue
        title = lines[0]
        if _bad_title(title):
            continue
        status, time_hint = "", ""
        for ln in lines[1:]:
            if not time_hint and (_TIME_LINE_RE.search(ln)
                                  or _MDHM_RE.search(ln) or _MDHM2_RE.search(ln)):
                time_hint = ln
            elif not status and len(ln) < 30 and not GROUP_TAG.fullmatch(ln) \
                    and classify_quiz(ln) != "unknown":
                status = ln
        if not status and not time_hint:
            continue  # 导航/按钮块
        # v2.0：非作业条目（签到/课件/整词讨论）在采集层直接跳过——
        # 不进待办、不进状态未知（用户定论：讨论不算作业）
        if classify_category(title) != "quiz":
            continue
        key = re.sub(r"\s+", "", title)
        if key in seen:
            continue
        seen.add(key)
        deadline, exact = parse_quiz_time(time_hint, now)
        tasks.append({
            "id": make_id(course.get("course_id") or "", key),
            "kind": "quiz",
            "category": "quiz",
            "course": course.get("name") or "",
            "course_id": course.get("course_id") or "",
            "title": scrub_task_title(title),
            "status": status,
            "state": classify_quiz(status) if status else "unknown",
            "deadline": deadline,
            "deadline_exact": exact,
            "time_hint": time_hint,
            "url": "",
            "first_seen": now.isoformat(timespec="seconds"),
        })
    return tasks


def merge_home_cards(tasks: list[dict], cards: list[dict]) -> int:
    """主页活动卡片状态回填：标题是卡片上下文子串则填空状态。"""
    filled = 0
    good = [(c["status"], re.sub(r"\s+", "", c.get("ctx") or ""))
            for c in cards or []
            if c.get("status") and classify_quiz(c["status"]) != "unknown"
            and c.get("ctx")]
    for t in tasks:
        if t.get("status"):
            continue
        nt = re.sub(r"\s+", "", t.get("title") or "")
        for s, nctx in good:
            if nctx and nt and nt in nctx:
                t["status"] = s
                t["state"] = classify_quiz(s)
                filled += 1
                break
    return filled


def parse_group_detail(text: str) -> str:
    """分组任务详情页文本 → 状态串（本地实证形态）。

    「个人最终得分」前一行是分数；「提交时间: …」是提交时间；
    两者都没有且页面「已结束」→ 未提交。
    """
    t = text or ""
    if "个人最终得分" not in t and "提交时间" not in t:
        return ""
    m = re.search(r"([\d.]+)\s*分\s*\n?\s*个人最终得分", t)
    sub = re.search(r"提交时间[:：]\s*(\S+)", t)
    if m:
        s = f"已提交，个人最终得分 {m.group(1)} 分"
        if sub:
            s += f"（提交时间 {sub.group(1)}）"
        return s
    if "提交时间" not in t and "已结束" in t:
        return "已结束，未提交"
    return "已结束（任务详情页）"


def parse_stat_rows(text: str) -> list[dict]:
    """stat2 统计页 body innerText（\t 分隔行）→ 作业行列表。"""
    items = []
    in_table = False
    for ln in (text or "").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        if ln.startswith("作业名称") and "\t" in ln:
            in_table = True
            continue
        if not in_table:
            continue
        if "\t" not in ln:
            in_table = "作业名称" in ln
            continue
        cells = [c.strip() for c in ln.split("\t") if c.strip()]
        if len(cells) < 3 or len(cells[0]) > 50 or _bad_title(cells[0]):
            continue
        m = re.search(r"([\d.]+)分", " ".join(cells))
        items.append({"title": scrub_task_title(cells[0]), "status": cells[-1],
                      "state": classify_quiz(cells[-1]), "score": m.group(1) if m else "",
                      "start": cells[1] if len(cells) > 1 else "",
                      "end": cells[2] if len(cells) > 2 else ""})
    return items


def active_id_map(bodies: list[dict]) -> dict[str, dict]:
    """activelist XHR 响应体 → 标题归一化 → {id, type}。"""
    m: dict[str, dict] = {}
    for b in bodies or []:
        try:
            import json as _j
            obj = _j.loads(b["body"])
            for it in ((obj.get("data") or {}).get("activeList")) or []:
                name = re.sub(r"\s+", "", str(it.get("nameOne") or ""))
                if name and it.get("id"):
                    m.setdefault(name, {"id": str(it["id"]), "type": it.get("type")})
        except Exception:
            pass
    return m


# ---------------------------------------------------------------------------
# Playwright 采集
# ---------------------------------------------------------------------------
def _new_ctx(pw, cookies: list[dict], ua: str,
             profile: str | None = None, proxy: str | None = None):
    """浏览器上下文：Actions 用 cookies 注入；本地开发用持久 profile 登录态；
    限流时段可走代理出口换 IP。profile 模式加 stealth 参数（学习通对
    HeadlessChrome 特征拒绝渲染 Vue 页——本地实证白屏）。"""
    if profile:
        # 本地验证可传 proxy（persistent 整体走代理：huuc 偶发超时由
        # collect_course 的 attempt 重试兜底；chaoxing.com 直连极慢必须走）；
        # Actions 不传 proxy（runner 直连全通）。加 stealth 参数——学习通
        # 对 HeadlessChrome 特征拒绝渲染 Vue 页（本地实证白屏）。
        kw = dict(headless=True, user_agent=ua, locale="zh-CN",
                  args=["--disable-blink-features=AutomationControlled"])
        if proxy:
            kw["proxy"] = {"server": proxy}
        ctx = pw.chromium.launch_persistent_context(profile, **kw)
        return None, ctx
    browser = pw.chromium.launch(headless=True)
    ctx = browser.new_context(user_agent=ua, locale="zh-CN")
    if cookies:
        ctx.add_cookies(cookies)
    return browser, ctx


def _find_active_frame(page):
    for fr in page.frames:
        if fr is page.main_frame:
            continue
        u = (fr.url or "").lower()
        if "stuactivelist" in u or "mobilelearn" in u:
            return fr
    return None


def _wait_text(target, timeout_s: int = 15) -> int:
    n = 0
    for _ in range(timeout_s):
        try:
            n = target.evaluate(
                "() => document.body ? (document.body.innerText || '').trim().length : 0")
        except Exception:
            n = 0
        if isinstance(n, int) and n > 0:
            return n
        try:
            target.wait_for_timeout(1000)
        except Exception:
            break
    return n


def _find_frame_by_url(page, *snippets):
    for f in page.frames:
        if f is page.main_frame:
            continue
        if any(s in (f.url or "") for s in snippets):
            return f
    return None


def _grab_blocks(target) -> list[str]:
    try:
        return target.evaluate(LIST_ITEM_JS) or []
    except Exception:
        return []


def _collect_list_frame(page, snippets, seen_titles: dict) -> list[str]:
    """进 tab 页 → 找列表 iframe → 块提取 + 翻页去重。"""
    fr = _find_frame_by_url(page, *snippets)
    blocks: list[str] = []
    if fr is None:
        return blocks
    _wait_text(fr, 8)
    blocks = _grab_blocks(fr)
    for b in blocks:
        ls = b.splitlines()
        if ls:
            seen_titles.setdefault(re.sub(r"\s+", "", ls[0]), b)
    for _ in range(5):
        try:
            moved = bool(fr.evaluate(CLICK_NEXT_JS))
        except Exception:
            moved = False
        if not moved:
            break
        page.wait_for_timeout(2500)
        nb = _grab_blocks(fr)
        new = [b for b in nb if b.splitlines()
               and re.sub(r"\s+", "", b.splitlines()[0]) not in seen_titles]
        if not new:
            break
        blocks.extend(new)
        for b in new:
            seen_titles[re.sub(r"\s+", "", b.splitlines()[0])] = b
    return blocks


# activelist API 的活动类型 → 分类（本地 14 门课实证；未知 type 宁多勿漏）
KNOWN_CLASSROOM_TYPES = {2, 44, 11, 43, 14, 4, 80, 59, 49, 23, 46,
                         82, 56, 17, 45, 83, 13, 5, 50}


def replay_activelist(target, api_url: str) -> list[dict]:
    """iframe 上下文重放 activelist API → 活动清单（绕过页面渲染波动）。

    本地实证：列表页 body 一轮 1524 字、一轮 13 字（随机骨架），但 API
    重放稳定返回全部活动（id/nameOne/type/startTime/endTime/status）。
    注意：JSON 里 status/userStatus 对签到/课件/作业无区分（v1.8.4d 实证），
    用户完成状态必须走详情页。"""
    js = """
async (args) => {
  const r = await fetch(args.url, {credentials: 'include',
      headers: {'Accept': 'application/json'}});
  return await r.text();
}
"""
    try:
        txt = target.evaluate(js, {"url": api_url})
        import json as _j
        obj = _j.loads(txt)
        return (obj.get("data") or {}).get("activeList") or []
    except Exception:
        return []


def _quiz_type(atype) -> bool:
    """activelist type → 是否作业候选：已知课堂类排除，未知 type 宁多勿漏。"""
    try:
        at = int(atype)
    except (TypeError, ValueError):
        return False
    if at in KNOWN_CLASSROOM_TYPES:
        return False
    return True


def tasks_from_activelist(items: list[dict], course: dict,
                          now: datetime) -> list[dict]:
    """activelist API 活动清单 → 作业任务（本地 14 门课实证）。

    仅收作业候选 type（42 测验/35 分组任务/未知 type 宁多勿漏）；签到(2)、
    课件(44)、选人(11)、投票(43)、讨论(5/50) 等已知课堂类直接排除。
    deadline 用 endTime 毫秒时间戳（精确，比解析文本可靠）。"""
    tasks: list[dict] = []
    seen: set[str] = set()
    for it in items or []:
        try:
            at = int(it.get("type"))
        except (TypeError, ValueError):
            continue
        if not _quiz_type(at):
            continue
        title = str(it.get("nameOne") or "").strip()
        if _bad_title(title):
            continue
        key = re.sub(r"\s+", "", title)
        if key in seen:
            continue
        seen.add(key)
        deadline, exact = None, False
        try:
            ets = int(it.get("endTime") or 0)
            if ets > 0:
                deadline = datetime.fromtimestamp(
                    ets / 1000, tz=BJ_TZ).isoformat(timespec="seconds")
                exact = True
        except (TypeError, ValueError, OSError):
            pass

        # type=77：content.stuUrl 是学生入口重定向链接（activeRowClick
        # default 分支点击用的就是它），redirect 落地后拿参数调
        # ai-evaluate load-data API；teaUrl 是教师预览链接（学生态被拒）
        stu_url, uuid, lib_uuid = "", "", ""
        if at == 77:
            try:
                content = json.loads(
                    str(it.get("content") or "").replace("'", '"'))
                stu_url = str(content.get("stuUrl") or "")
                sc = json.loads(str(content.get("sourceConfig") or ""))
                uuid = str(sc.get("publishRelationUuid") or "")
                lib_uuid = str(sc.get("libraryUuid") or "")
            except Exception:
                pass
        tasks.append({
            "id": make_id(course.get("course_id") or "", key),
            "kind": "quiz",
            "category": "quiz",
            "_aid": str(it.get("id") or ""),
            "_stu_url": stu_url,
            "_uuid": uuid,
            "_lib_uuid": lib_uuid,
            "atype": at,
            "course": course.get("name") or "",
            "course_id": course.get("course_id") or "",
            "title": scrub_task_title(title),
            "status": "",
            "state": "unknown",
            "deadline": deadline,
            "deadline_exact": exact,
            "time_hint": "",
            "url": "",
            "first_seen": now.isoformat(timespec="seconds"),
        })
    return tasks


def _quiz42_status(ctx, mid_url: str, aid: str) -> str:
    """测验/知识点总结（type=42）：直开 answerQuestion2 → 轮询读状态词。

    本地实证：已提交态显示「已提交，待教师公布正确答案」；页面可能再
    302 到 quizStudentQuestion，轮询所有 frame 读。精确状态优先于徽标。"""
    page = ctx.new_page()
    try:
        page.goto(mid_url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(6000)
        frame = _find_active_frame(page)
        if frame is None:
            return ""
        vals = frame.evaluate(
            "() => ({fid: String(window.fid||''), courseId: String(window.courseId||''),"
            " classId: String(window.classId||''), timetable: String(window.timetable||'')})")
        url = (f"https://mobilelearn.huuc.edu.cn/page/quiz/stu/answerQuestion2"
               f"?courseId={vals['courseId']}&classId={vals['classId']}"
               f"&activeId={aid}&fid={vals['fid']}&timetable={vals['timetable']}")
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        best = ""
        # 实测该域页面挂载极慢（situationalDialogue 51s 才出 innerText）——
        # 轮询 60s，出现即返回
        for _ in range(60):
            page.wait_for_timeout(1000)
            for f in page.frames:
                try:
                    txt = f.evaluate(STAT_PAGE_JS)
                except Exception:
                    continue
                if "个人最终得分" in txt:
                    continue  # 误入分组任务详情，跳过
                for ln in txt.splitlines():
                    ln2 = ln.strip()
                    if ln2 and len(ln2) < 60 and not GROUP_TAG.fullmatch(ln2)                             and classify_quiz(ln2) != "unknown":
                        if any(w in ln2 for w in ("已提交", "待批阅", "已评分",
                                                  "已互评", "得分")):
                            return ln2
                        if not best:
                            best = ln2
        return best
    except Exception:
        return ""
    finally:
        try:
            page.close()
        except Exception:
            pass


def _tea_url_status(ctx, course: dict, uuid: str, lib_uuid: str,
                    cpi: str) -> str:
    """type=77（老师发布的外部 AI 互动活动）状态：**直调 load-data API**。

    本地实证（思修「怎样创造有意义的人生？」）：
    GET /mooc2-ans/ai-evaluate/v2/answer/load-data?courseid=&cpi=&clazzid=&
    publishRelationUuid=[&libraryUuid=] → JSON 含 answerScore=85.0/
    answerRecords（submitTime）/taskEndedFlag——**响应 3KB，一次请求，
    不经 51s 页面渲染**（此前 teaUrl 页面方案在 Actions 失败的根因是
    runner→chaoxing.com 跨洋访问慢页面）。libraryType=6（aiInteractStory）
    需带 libraryUuid；libraryType=0（situationalDialogue）不带同样有效。
    在 chaoxing 域轻页面上下文里 fetch（cookie 同域）。"""
    page = ctx.new_page()
    try:
        page.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        # 开一个 chaoxing 域轻上下文（根路径 404 页也行，fetch 同域即可）
        page.goto("https://mooc2-ans.chaoxing.com/", wait_until="commit",
                  timeout=45000)
        page.wait_for_timeout(3000)
        cid = course.get("course_id") or ""
        clz = course.get("class_id") or ""
        js = """
async (args) => {
  const r = await fetch('/mooc2-ans/ai-evaluate/v2/answer/load-data?courseid='
      + args.cid + '&cpi=' + args.cpi + '&clazzid=' + args.clz
      + '&publishRelationUuid=' + args.uuid
      + (args.lib ? '&libraryUuid=' + args.lib : ''),
      {credentials: 'include', headers: {'Accept': 'application/json'}});
  return await r.text();
}
"""
        txt = page.evaluate(js, {"cid": cid, "cpi": cpi, "clz": clz,
                                 "uuid": uuid, "lib": lib_uuid})
        data = (json.loads(txt).get("data")) or {}
        score = data.get("answerScore")
        recs = data.get("answerRecords") or []
        if score:
            return f"已提交，得分 {score} 分"
        if recs:
            sub = str(recs[0].get("submitTime") or "")[:16]
            return f"已提交（提交时间 {sub}）"
        try:
            ended = int(data.get("taskEndedFlag") or 0)
        except (TypeError, ValueError):
            ended = 0
        if ended == 1:
            return "已结束，未提交"
        return ""
    except Exception:
        return ""
    finally:
        try:
            page.close()
        except Exception:
            pass


def _fill_quiz_status(ctx, course: dict, mid_url: str,
                      items: list[dict], cpi: str) -> tuple[int, list[str]]:
    """状态补抓：按活动 type 分发——35 分组任务 API、42 测验直开、
    77 外部互动学生页面、其余 activeRowClick 兜底。返回 (成功数, 失败原因)。"""
    got = 0
    reasons: list[str] = []
    for it in items:
        title = it.get("title") or ""
        tag = title[:12]
        aid = it.get("_aid") or ""
        atype = it.get("atype")
        status = ""
        if not aid:
            reasons.append(f"{tag}:无id")
        elif atype == 35:
            status, to_ans_flag = _group_task_status(ctx, mid_url, aid)
            if not status:
                # toAnswerPage=1 = 跳答题页 = 未作答
                # （用户 2026-11-27 定论：待作答就是明确状态，进待办提醒）
                if to_ans_flag == 1:
                    status = "未提交（待作答）"
                else:
                    reasons.append(f"{tag}:分组任务API空")
        elif atype == 77:
            uuid = it.get("_uuid") or ""
            lib_uuid = it.get("_lib_uuid") or ""
            status = _tea_url_status(ctx, course, uuid, lib_uuid, cpi) \
                if uuid else ""
            if not status:
                reasons.append(f"{tag}:load-data无状态")
        elif atype == 42:
            status = _quiz42_status(ctx, mid_url, aid)
            if not status:
                # 直开兜底失败（详情页形态多变）→ 回退列表点击路径
                status, reason = _rowclick_status(ctx, mid_url, title)
                if not status:
                    reasons.append(f"{tag}:测验直开无状态/{reason}")
        else:
            status, reason = _rowclick_status(ctx, mid_url, title)
            if not status:
                reasons.append(f"{tag}:{reason}")
        if status:
            it["status"] = status
            it["state"] = classify_quiz(status)
            got += 1
        page_wait(ctx, 2000)
    return got, reasons


def page_wait(ctx, ms: int) -> None:
    """补抓函数各自开 page，等待统一走 ctx 已有 page 或退化 sleep。"""
    try:
        pages = ctx.pages
        if pages:
            pages[0].wait_for_timeout(ms)
            return
    except Exception:
        pass
    import time as _t
    _t.sleep(ms / 1000)


def collect_course(course: dict, mid_url: str, cookies: list[dict],
                   ua: str, now: datetime, cpi: str = "577967959",
                   profile: str | None = None,
                   proxy: str | None = None) -> dict:
    """单课采集（一次浏览器会话完成列表 + 状态补抓）。

    登录态二选一：cookies（Actions，cx 登录链注入）或 profile（本地开发，
    持久化登录目录）。
    """
    result: dict = {"ok": False, "tasks": [], "work_blocks": [],
                    "exam_blocks": [], "diag": [], "error": ""}
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser, ctx = _new_ctx(pw, cookies, ua, profile, proxy)
            page = ctx.new_page()
            xhr_bodies: list[dict] = []

            def _on_response(resp):
                try:
                    u = resp.url or ""
                    # 只认 v2 JSON API（页面 URL stuActiveList 会误匹配 activelist）；
                    # 不查 content-type——实测该 API 响应头不保证 application/json
                    if "/v2/apis/" not in u or "activelist" not in u.lower():
                        return
                    if not any(x.get("url") == u for x in xhr_bodies):
                        xhr_bodies.append({"url": u, "body": resp.text()[:200000]})
                except Exception:
                    pass

            page.on("response", _on_response)
            # ① 主页任务 tab：随堂清单 API 优先。骨架轮（body 只有课程名）
            # 时 activelist XHR 根本不发——重试 goto 至捕获 API（实测第 2 轮即中）
            api_url = ""
            frame = None
            real = ""
            cards: list = []
            for attempt in range(3):
                frame = None  # 重新 goto 后旧 frame 引用必 detached，必须重置
                try:
                    # 代理模式下 huuc 域绕远可能超时——超时不放弃，重试下轮
                    page.goto(mid_url, wait_until="domcontentloaded",
                              timeout=60000)
                except Exception as e:
                    result["diag"].append(
                        f"attempt {attempt + 1} goto超时 {safe_text(str(e), 50)}")
                    continue
                page.wait_for_timeout(9000)
                real = page.url
                for b in xhr_bodies:
                    u = b.get("url") or ""
                    if "/v2/apis/" in u and "activelist" in u.lower():
                        api_url = u
                        break
                if frame is None:
                    frame = _find_active_frame(page)
                if frame is None:
                    try:
                        page.get_by_text("任务", exact=True).first.click(timeout=5000)
                    except Exception:
                        pass
                    for _ in range(8):
                        page.wait_for_timeout(1000)
                        frame = _find_active_frame(page)
                        if frame is not None:
                            break
                if api_url:
                    try:
                        cards = page.evaluate(HOME_CARD_JS) or []
                    except Exception:
                        cards = []
                    break
                result["diag"].append(
                    f"attempt {attempt + 1} 无 activelist API（骨架轮）")
            # ①a 随堂清单：API 优先（重放 activelist，绕过渲染波动——
            # 实测同课一轮 body 1524 字、一轮 13 字），块解析兜底
            api_url = ""
            for b in xhr_bodies:
                u = b.get("url") or ""
                if "/v2/apis/" in u and "activelist" in u.lower():
                    api_url = u
                    break
            api_items: list[dict] = []
            if api_url and frame is not None:
                api_items = replay_activelist(frame, api_url)
            tasks: list[dict] = tasks_from_activelist(api_items, course, now)
            if api_items:
                result["diag"].append(f"API活动 {len(api_items)}")
            if not tasks:
                # 块解析兜底（API 未捕获/空时）
                blocks: list[str] = []
                if frame is not None:
                    try:
                        frame.wait_for_load_state("domcontentloaded", timeout=15000)
                    except Exception:
                        pass
                    _wait_text(frame, 15)
                    blocks = _grab_blocks(frame)
                    if not blocks:
                        frame.wait_for_timeout(2000)
                        blocks = _grab_blocks(frame)
                    if not blocks:
                        # 渲染波动兜底（实测同课一轮 48 块、一轮 0 块）：整页重进一次
                        page.goto(mid_url, wait_until="domcontentloaded", timeout=60000)
                        page.wait_for_timeout(8000)
                        frame = _find_active_frame(page)
                        if frame is None:
                            try:
                                page.get_by_text("任务", exact=True).first.click(timeout=5000)
                            except Exception:
                                pass
                            for _ in range(10):
                                page.wait_for_timeout(1000)
                                frame = _find_active_frame(page)
                                if frame is not None:
                                    break
                        if frame is not None:
                            _wait_text(frame, 15)
                            blocks = _grab_blocks(frame)
                    result["diag"].append(f"随堂块 {len(blocks)}（API 兜底）")
                else:
                    result["diag"].append("no_stuactivelist_frame（API 兜底）")
                tasks = parse_active_blocks(blocks, course, now)
                merge_home_cards(tasks, cards)
                # v2.4：兜底块解析的任务用 API type 数据过滤课堂类
                #（国安「1.作为青年大学生…谈一谈」标题像作业、type=5 是讨论）
                if api_items:
                    tmap = {re.sub(r"\s+", "", str(it.get("nameOne") or "")):
                            it.get("type") for it in api_items}
                    tasks = [t for t in tasks
                             if _quiz_type(tmap.get(re.sub(r"\s+", "", t.get("title") or "")))]
                result["diag"].append(f"过滤后 {len(tasks)}")

            # ②③ 作业/考试 tab（iframe 块 + 翻页）
            seen_w: dict = {}
            seen_e: dict = {}
            for tab, key, snip in ((8, "work", ("work/list", "content-zy")),
                                   (9, "exam", ("exam-list", "content-ks"))):
                url = re.sub(r"pageHeader=\d+", f"pageHeader={tab}", real) \
                    if "pageHeader=" in real else real + f"&pageHeader={tab}"
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(5000)
                bl = _collect_list_frame(page, snip,
                                         seen_w if key == "work" else seen_e)
                result["work_blocks" if key == "work" else "exam_blocks"] = bl
                result["diag"].append(f"{key}块 {len(bl)}")
                _merge_work_exam(tasks, bl, key, course, now)

            # ④ 状态补抓：随堂作业类且无状态的条目（按 atype 分发：
            # 35 分组任务 API、42 测验直开 answerQuestion2、其余 rowclick）
            need = [t for t in tasks
                    if t.get("kind") == "quiz" and t.get("state") == "unknown"]
            filled = 0
            if need:
                filled, _reasons = _fill_quiz_status(ctx, course, mid_url,
                                                     need, cpi)
                result["diag"].extend(_reasons)
            result["diag"].append(f"补状态 {filled}/{len(need)}")
            # 审计红线（§2.5）：_aid/atype 是每生活动 ID，不落盘
            for t in tasks:
                t.pop("_aid", None)
                t.pop("atype", None)
                t.pop("_stu_url", None)
                t.pop("_uuid", None)
                t.pop("_lib_uuid", None)
            result["ok"] = True
            result["tasks"] = tasks
            ctx.close()
            if browser:
                browser.close()
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {safe_text(str(e), 200)}"
    return result


def _merge_work_exam(tasks: list[dict], blocks: list[str], kind: str,
                     course: dict, now: datetime) -> None:
    """作业/考试块 → 任务（kind=work/exam，带状态）。"""
    seen: set[str] = set()
    for t in tasks:
        if t.get("kind") == kind:
            seen.add(re.sub(r"\s+", "", t.get("title") or ""))
    for b in blocks or []:
        lines = [l.strip() for l in (b or "").splitlines() if l.strip()]
        if not lines or len(lines) > 12:
            continue
        title = lines[0]
        if _bad_title(title):
            continue
        status, time_hint = "", ""
        for ln in lines[1:]:
            if not time_hint and (_TIME_LINE_RE.search(ln)
                                  or _MDHM_RE.search(ln) or _MDHM2_RE.search(ln)):
                time_hint = ln
            elif not status and len(ln) < 30 and not GROUP_TAG.fullmatch(ln) \
                    and classify_quiz(ln) != "unknown":
                status = ln
        if not status:
            continue
        key = re.sub(r"\s+", "", title)
        if key in seen:
            continue
        seen.add(key)
        deadline, exact = parse_quiz_time(time_hint, now)
        tasks.append({
            "id": make_id(course.get("course_id") or "", key),
            "kind": kind,
            "category": "work" if kind == "work" else "exam",
            "course": course.get("name") or "",
            "course_id": course.get("course_id") or "",
            "title": scrub_task_title(title),
            "status": status,
            "state": classify_quiz(status),
            "deadline": deadline,
            "deadline_exact": exact,
            "time_hint": time_hint,
            "url": "",
            "first_seen": now.isoformat(timespec="seconds"),
        })



def _rowclick_status(ctx, mid_url: str, title: str) -> tuple[str, str]:
    """进列表页 → 找 li[activeId] → activeRowClick 使 iframe 跳详情 → 读状态词。

    列表渲染有随机波动（实测同课一轮 48 块、一轮 0 块），整条重试 2 次；
    详情页加载慢，点完轮询 8s 读状态词。返回 (状态串, 失败原因)。
    自开 page（主采集 page 已停在考试 tab，不互扰）。"""
    page = ctx.new_page()
    try:
        hint = "未尝试"
        for attempt in range(3):
            page.goto(mid_url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(6000)
            frame = _find_active_frame(page)
            if frame is None:
                hint = "无frame"
                continue
            try:
                li = frame.query_selector(f'li[activeId]:has-text("{title[:20]}")')
            except Exception:
                li = None
            if li is None:
                hint = "无li"
                continue
            try:
                frame.evaluate("el => { try { activeRowClick(el); } catch (e) {} }", li)
            except Exception as e:
                hint = f"rowclick异常 {str(e)[:40]}"
                continue
            nt = re.sub(r"\s+", "", title)[:10]
            # 精确状态优先：「已提交，待教师公布正确答案」比页面徽标「已结束」
            # 信息量更大（本地实证详情页两者并存，顺序不定）
            best = ""
            for _ in range(8):
                page.wait_for_timeout(1000)
                for target in list(page.frames):
                    try:
                        txt = target.evaluate(STAT_PAGE_JS)
                    except Exception:
                        continue
                    if not txt or nt not in re.sub(r"\s+", "", txt):
                        continue
                    for ln in txt.splitlines():
                        ln2 = ln.strip()
                        if ln2 and len(ln2) < 60 and not GROUP_TAG.fullmatch(ln2) \
                                and classify_quiz(ln2) != "unknown":
                            if any(w in ln2 for w in ("已提交", "待批阅",
                                                      "已评分", "已互评", "得分")):
                                return ln2, ""
                            if not best:
                                best = ln2
            hint = "详情无状态词"
        return "", hint
    finally:
        try:
            page.close()
        except Exception:
            pass


def _group_task_status(ctx, mid_url: str, aid: str) -> tuple[str, int]:
    """分组任务（type=35）：getActiveInfoForMe 选分支 → 详情页 parse_group_detail。

    本地实证：详情页文本「个人最终得分 58.4 分（提交时间 11-04）」；
    直接构造详情 URL（4 分支模板），不经页面点击（popup 会停在 about:blank）。
    返回 (状态串, toAnswerPage)——to_ans=1 表示未作答（跳答题页），
    调用方据此给「未提交（待作答）」明确状态（用户 2026-11-27 定论）。"""
    page = ctx.new_page()
    try:
        page.goto(mid_url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(6000)
        frame = _find_active_frame(page)
        if frame is None:
            return "", -1
        vals = frame.evaluate(
            "() => ({fid: String(window.fid||''), courseId: String(window.courseId||''),"
            " classId: String(window.classId||''), timetable: String(window.timetable||'')})")
        info = frame.evaluate(
            """
async (args) => {
  const r = await fetch('/v2/apis/taskStu/getActiveInfoForMe?fid=' + args.fid
      + '&courseId=' + args.cid + '&activeId=' + args.aid,
      {credentials: 'include', headers: {'Accept': 'application/json'}});
  return await r.json();
}
""", {"fid": vals["fid"], "cid": vals["courseId"], "aid": aid})
        data = (info or {}).get("data") or {}
        # API 返回字符串/数字不定（本地实证两种都有）——统一转 int 比较
        def _n(v):
            try:
                return int(v)
            except (TypeError, ValueError):
                return -1
        is_norm, to_ans = _n(data.get("isNorm")), _n(data.get("toAnswerPage"))
        use_note = _n(data.get("useNoteEditor"))
        q = (f"courseId={vals['courseId']}&classId={vals['classId']}"
             f"&activeId={aid}&fid={vals['fid']}&timetable={vals['timetable']}")
        if is_norm == 1 and use_note == 1 and to_ans == 1:
            url = f"/page/renwuNew/stu/taskDetailStuForAnswer_new?activeId={aid}&{q}"
        elif is_norm == 1 and use_note == 1:
            url = f"/page/renwuNew/stu/taskDetailStuAndAnsDetail_new?{q}"
        elif is_norm == 1:
            url = f"/page/renwu/stu/taskDetailStuAndAnsDetail?{q}"
        else:
            url = f"/page/renwuNew/notGroup/taskDetailStuAndAnsDetailNotGroup_new?{q}"
        page.goto("https://mobilelearn.huuc.edu.cn" + url,
                  wait_until="commit", timeout=60000)
        # 页面挂载极慢（本地实证 51s+），固定 8s 必读空——轮询 60s 出现即返回
        for _ in range(60):
            page.wait_for_timeout(1000)
            for f in page.frames:
                try:
                    txt = f.evaluate(STAT_PAGE_JS)
                except Exception:
                    continue
                if "个人最终得分" in txt or "提交时间" in txt or "已结束" in txt:
                    return parse_group_detail(txt), to_ans
        return "", to_ans
    except Exception:
        return "", -1
    finally:
        try:
            page.close()
        except Exception:
            pass


def fetch_work_stat(course: dict, cookies: list[dict], ua: str,
                    cpi: str, timeout_ms: int = 45000,
                    profile: str | None = None) -> list[dict]:
    """stat2 统计页（Playwright 渲染 + 轮询 \t 数据行 + cpi 必须显式）。

    v1.7.2 移植版：requests 拿到的是骨架，真实表格 JS 渲染后出现；
    cpi 缺失时页面返回 30 字符空页（本地实证），调用方必须传对学生 cpi。
    """
    cid, clz = course.get("course_id"), course.get("class_id")
    surl = (f"https://stat2-ans.huuc.edu.cn/stat2/work-stastics/stu-index"
            f"?courseid={cid}&cpi={cpi}&clazzid={clz}&ut=s")
    out: list[dict] = []
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser, ctx = _new_ctx(pw, cookies, ua, profile)
            page = ctx.new_page()
            page.goto(surl, wait_until="domcontentloaded", timeout=timeout_ms)
            body = ""
            for _ in range(12):
                page.wait_for_timeout(1000)
                try:
                    body = page.evaluate(STAT_PAGE_JS)
                except Exception:
                    body = ""
                if body.count("\t") > 6:
                    break
            out = parse_stat_rows(body)
            ctx.close()
            if browser:
                browser.close()
    except Exception:
        return out
    return out
