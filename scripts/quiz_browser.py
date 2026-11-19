"""Playwright 随堂作业采集（v1.6.0 收录版）。

四层逆向结论（v1.5.21/22 + 2026-11-16 真机日志证据）：
1. 课程学习页 mycourse/stu 的 tab 内容嵌在 iframe 里；
2. 随堂活动列表 = mobilelearn.huuc.edu.cn/page/active/stuActiveList
   ?courseid&clazzid&cpi&ut=s&t&stuenc —— 由课程页自己嵌成 frame#2
   （2026-11-16 run 43793959922/43800209131 日志，4 个 frame 中的第 2 个）；
3. **单独 goto 这个 URL（新开页面）渲染 0 字**（v1.5.22 的死路）；
   **课程页内的 iframe 却渲染出真实内容**（日志证据：高数 1443 字、
   思修 316 字含「小组视频作业。」行）→ 正确路径是打开课程页后
   **直接从它的 frame 里提取**，不另开页面；
4. 行结构（run 43805479954 首轮真实样本）：同一可视行被渲染成多个
   **并列碎片元素**（计数徽标「（0）」/标题/中文时间行/机器时间行），
   不是嵌在同一个候选元素里 → 提取时带 getBoundingClientRect 的
   **Y 坐标，按视觉位置聚类成行**（嵌套元素靠子串去重）；
   课堂互动类（签到/选人/投票/抢答）不是作业，过滤掉。

设计约束（v1.5.21 反爬实测教训，沿用）：
- **失败绝不影响主采集**：任何异常返回空结果，调用方只记 WARN；
- 每轮只轮换抓少数课程（study.quiz_per_round，默认 4），课程间 sleep 2s；
- cookie 复用登录链会话，**域归一化到 .huuc.edu.cn**（requests 存的
  host-only cookie 不会发给兄弟子域 —— v1.5.22「单独 goto 渲染 0 字」
  的疑似根因；真实浏览器里这些 cookie 本来就是父域形态）；
- UA 用用户真实浏览器指纹（Chrome/157）。

日志安全：frame 只落**行级结构化数据**（标题/状态/时间行）；课程页主
frame（页面菜单含用户姓名，v1.5.22 泄露过一次已删 run）全文绝不 dump；
诊断 dump 一律数字打码再落公开日志。
"""
from __future__ import annotations

import re
from datetime import datetime

from utils import make_id, parse_remaining, safe_text, scrub_task_title

# ---------------------------------------------------------------------------
# 纯函数（selftest 离线可测）
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
        if not dom.endswith("huuc.edu.cn"):
            continue
        by_name[c.name] = {"name": c.name, "value": c.value or "",
                           "domain": ".huuc.edu.cn", "path": c.path or "/"}
    return list(by_name.values())


# 随堂状态词表。未收录的词 classify 返回 unknown，调用方 WARN 出来下一轮补，
# 不许静默吞掉（学习通「未交」漏词教训，v1.2.3）。
# v1.6.5：「未开始」单独判 upcoming——活动还没开放，不算待办（与 study_collector.classify 同口径）。
QUIZ_DONE = {"已完成", "已参与", "已提交", "已结束", "已交", "已答", "已互评", "已过期", "已评分"}
QUIZ_SUBMITTED = {"待批阅", "批阅中", "待批改", "批改中"}
QUIZ_PENDING = {"待做", "未做", "未交", "未提交", "待完成", "进行中", "未参与", "待参与",
                "待作答", "未答", "已发布", "待开始"}


def classify_quiz(status: str) -> str:
    s = (status or "").strip()
    if s == "未开始":        # v1.6.6：未开始单独判 upcoming（显示在清单、标「未开始」，不进待完成/urgent6）
        return "upcoming"
    if s in QUIZ_DONE:
        return "done"
    if s in QUIZ_SUBMITTED:
        return "submitted"
    if s in QUIZ_PENDING:
        return "pending"
    return "unknown"


# v1.8.1（用户 2026-11-19 提供 DOM 实证）：随堂状态徽标是
# `<span class="color-green"><i class="icon-finish"></i>已提交，待教师公布正确答案</span>`
# ——状态是**文字**但常常超过 12 字且不是词表整词，旧逻辑整串精确匹配全漏。
# 子串扫描按词长降序（长词优先，防止「未交」抢先命中「未提交」这类前缀包含）。
_QUIZ_ALL_WORDS = sorted(QUIZ_DONE | QUIZ_SUBMITTED | QUIZ_PENDING,
                         key=len, reverse=True)
_STATUS_LINE_MAX = 30  # 状态行长度上限：12 → 30（真实样本 14 字）；再长的是标题


def classify_quiz_line(text: str) -> str:
    """一行文本的状态分类：整词精确优先，再按词表子串扫描。

    「已提交，待教师公布正确答案」→ 命中「已提交」→ done。
    标题等长文本不该走这里（调用方先限长度）——「已结束的补交通道」这种
    标题含状态词会被误判，所以只对 ≤30 字的短行开放子串匹配。
    """
    s = (text or "").strip()
    if not s:
        return "unknown"
    exact = classify_quiz(s)
    if exact != "unknown":
        return exact
    for w in _QUIZ_ALL_WORDS:
        if w in s:
            if w in QUIZ_DONE:
                return "done"
            if w in QUIZ_SUBMITTED:
                return "submitted"
            return "pending"
    return "unknown"


_MDHM_RE = re.compile(r"(?:(\d{4})[-/年])?(\d{1,2})[-/月](\d{1,2})日?\s*(\d{1,2}):(\d{2})")
# v1.6.1：中文显示层时间「11月03日 19时33分」（真实样本 run 43805479954）
_MDHM2_RE = re.compile(r"(\d{1,2})月(\d{1,2})日\s*(\d{1,2})时(\d{1,2})分")
# 纯计数/纯符号行（「（0）」「（2）」计数徽标碎片）
_NOISE_TITLE_RE = re.compile(r"^[\d（）()：:、\s]*$")
# 课堂互动类活动不是作业（签到/选人/投票/抢答/问卷），不进任务清单
_CLASSROOM_RE = re.compile(r"(签到|选人|投票|抢答|问卷)")
# v1.8.0（用户 2026-11-19 定论）：「主题讨论」「提问」这类**整词短标题**是老师
# 当堂发起的活动容器名，不是要动手写的作业（思修/国安「主题讨论」、高数「提问」
# 实测全是被误判"逾期"的课堂活动）。只拦整词——含「讨论」的长标题（如真实
# 讨论课作业）不拦，交给判定层的 quiz_ended 规则兜底。
# 公开名：study_collector 的存量清理与 selftest 复用同一形状，不许两处漂移。
CLASSROOM_EXACT_RE = re.compile(r"^(?:主题)?讨论$|^提问$")
# v1.8.0：标题退化 = 标题就是课程名本身（带书名号）。真实样本：体育课签到行
# 《…星期一下午七八体育课》、英语《新世纪大学英语综合教程（一）》——活动
# 类型在采集层就丢了、标题拿课程名兜底。这种行是签到/课次容器，不是作业。
_BOOK_WRAP_RE = re.compile(r"^《[^《》]{4,}》$")


def title_is_course_name(title: str, course_name: str) -> bool:
    """标题是否退化成课程名（书名号包裹或裸同文）。全角/半角括号归一后比对。"""
    t = (title or "").strip()
    c = (course_name or "").strip().strip("《》")
    if _BOOK_WRAP_RE.fullmatch(t):
        t = t[1:-1]
    t = t.replace("（", "(").replace("）", ")")
    c = c.replace("（", "(").replace("）", ")")
    return bool(t) and bool(c) and len(t) >= 4 and t == c
# v1.7.0（用户报的 bug）：老师发的课件/文件不是作业——「D1-2数列的极限.pptx」
# 这种带文件后缀的标题一律不收（用户原话：任务名称后面有文件后缀的就是文件，
# 不是要动手写的作业；旧版把文件当作业还标了逾期）
_FILE_SUFFIX_RE = re.compile(
    r"\.(pptx?|docx?|pdf|zip|rar|7z|xlsx?|csv|txt|mp[34]|flv|mkv|avi|png|jpe?g|gif)\s*$", re.I)


def _time_like(l: str) -> bool:
    """这一行是不是时间形状（倒计时/月日时分/ISO 带冒号）。"""
    return (parse_remaining(l) is not None or _MDHM_RE.search(l) is not None
            or _MDHM2_RE.search(l) is not None)


def parse_quiz_time(text: str, now: datetime) -> tuple[str | None, bool]:
    """随堂行的时间文本 → (deadline, 是否精确)。

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


def cluster_rows(rows_raw: list[dict], tol: int = 25) -> list[dict]:
    """按视觉位置把碎片元素聚成行（v1.6.1 真实样本结构）。

    同一可视行的标题/计数/时间是**并列元素**（不在同一候选里），靠
    getBoundingClientRect 的 Y 中心聚类合并；同簇内子串去重，消灭
    嵌套元素（li 全文 vs 子节点碎片）与碎片本身。
    rows_raw 每项：{text, y, onclick, aria}（y 缺失时按单独一行处理）。
    返回每项 {texts: [行内全部文本], onclick, aria}。
    """
    items = [r for r in rows_raw if (r.get("text") or "").strip()]
    with_y = [r for r in items if isinstance(r.get("y"), (int, float))]
    without_y = [r for r in items if not isinstance(r.get("y"), (int, float))]
    with_y.sort(key=lambda r: r["y"])
    clusters: list[list[dict]] = []
    for r in with_y:
        if clusters and abs(r["y"] - clusters[-1][-1]["y"]) <= tol:
            clusters[-1].append(r)
        else:
            clusters.append([r])
    out = []
    for cl in clusters + [[r] for r in without_y]:
        texts = [r["text"].strip() for r in cl]
        # 子串去重：嵌套/碎片文本被全文包含的丢掉
        kept = [t for t in texts
                if not any(t != o and t in o for o in texts)]
        if not kept:
            kept = texts
        onclick = next((r.get("onclick") for r in cl if r.get("onclick")), "")
        aria = next((r.get("aria") for r in cl if r.get("aria")), "")
        out.append({"texts": kept, "onclick": onclick, "aria": aria})
    return out


def parse_active_rows(rows_raw: list[dict], course: dict, now: datetime,
                      frame_url: str = "") -> tuple[list[dict], list[str]]:
    """frame 里提出的碎片元素 → 任务 dict 列表。纯函数。

    rows_raw 每项是 EXTRACT_JS 给的 {text, y, onclick, aria}，
    先 cluster_rows() 按视觉位置聚成行，再对每行做启发式：
    - 状态行 = 短行且能 classify（待做/进行中/已完成…）
    - 时间行 = 倒计时/月日时分/带冒号 ISO 形状
    - 标题 = 剩余行里最长的一条；aria-label「标题 ; 状态」结构优先补状态
    - 纯计数行（「（0）」）与课堂互动（签到/选人…）不是任务，跳过
    无状态时默认 pending（诚实显示，§1.1 教训：没吃透宁可让用户看见）；
    未收录状态词收集进 unmapped（调用方 WARN），下一轮补词表。
    """
    tasks: list[dict] = []
    unmapped: list[str] = []
    for cl in cluster_rows(rows_raw):
        lines = [l for t in cl["texts"] for l in t.splitlines() if l.strip()]
        if not lines:
            continue
        status, time_line = "", ""
        title_candidates: list[str] = []
        time_lines: list[str] = []
        for l in lines:
            # v1.8.1：长度 12→30 + 子串扫描（classify_quiz_line）。真实 DOM：
            # 「已提交，待教师公布正确答案」14 字，旧逻辑 len<=12 整串匹配双杀全漏
            if not status and len(l) <= _STATUS_LINE_MAX and classify_quiz_line(l) != "unknown":
                status = l
                continue
            if _time_like(l):
                time_lines.append(l)
                continue
            title_candidates.append(l)
        aria = (cl.get("aria") or "").strip()
        if not status and ";" in aria:
            parts = [p.strip() for p in aria.split(";")]
            if len(parts) >= 2 and classify_quiz_line(parts[1]) != "unknown":
                status = parts[1]
        if not title_candidates and aria:
            title_candidates = [aria.split(";")[0].strip()]
        title = max(title_candidates, key=len) if title_candidates else ""
        title = scrub_task_title(title)
        if not title or _NOISE_TITLE_RE.fullmatch(title):
            continue
        if _CLASSROOM_RE.search(title):
            continue  # 签到/选人等课堂互动，不是作业
        if CLASSROOM_EXACT_RE.fullmatch(title):
            continue  # v1.8.0：主题讨论/提问等整词短标题 = 课堂活动容器
        if title_is_course_name(title, course.get("name") or ""):
            continue  # v1.8.0：标题退化成课程名 = 签到/课次容器，不是作业
        if _FILE_SUFFIX_RE.search(title):
            continue  # v1.7.0：.pptx 等课件文件，老师发的材料不是作业
        # 零信息行（页头/标签条）：无时间无状态无深链，不是任务
        if not (time_lines or status or re.search(r"https?://", cl.get("onclick") or "")):
            continue
        # v1.8.1：state 用子串版（长状态文本「已提交，待教师公布正确答案」
        # 整串匹配是 unknown，会被强制 pending —— 正是状态丢失的另一半根因）
        state = classify_quiz_line(status) if status else "pending"
        if state == "unknown" and status:
            unmapped.append(status)
            state = "pending"
        deadline, exact = (None, False)
        if time_lines:
            # 真实样本一行里两种时间：显示层「11月05日 15时30分」+机器层
            # 「2026-11-05 15:30」（带年份）。优先机器层（精确）。
            for tl in time_lines:
                d2, e2 = parse_quiz_time(tl, now)
                if e2 and d2:
                    deadline, exact, time_line = d2, e2, tl
                    break
            if deadline is None:
                time_line = time_lines[0]
                deadline, exact = parse_quiz_time(time_line, now)
        # 行身份：优先 onclick 里的对象级命名 ID（activeId/knowledgeId…），
        # 拿不到退到最后一段 6 位以上数字串（courseid 在前、对象 ID 靠后）。
        # **ID 必须再加标题做盐**：只靠数字串时同一门课的行会撞车（v1.6.0
        # 曾因此只留一条），撞了会被去重吞掉。
        key = ""
        oc = cl.get("onclick") or ""
        km = re.search(r"(?:activeId|knowledgeId|activityId|objectId|workId|exerciseId)\W+(\d{4,})",
                       oc, re.I)
        if km:
            key = km.group(1)
        else:
            nums = re.findall(r"\d{6,}", oc)
            key = nums[-1] if nums else ""
        if not key:
            tm = re.search(r"(\d{4,})", title)
            key = tm.group(1) if tm else title
        url = ""
        um = re.search(r"https?://[^\s'\"]+", cl.get("onclick") or "")
        if um:
            url = um.group(0)
        elif frame_url:
            url = frame_url
        tasks.append({
            "id": make_id(course["course_id"], course["class_id"], "quiz", key, title),
            "kind": "quiz",
            "course": course.get("name") or "",
            "course_id": course["course_id"],
            "class_id": course["class_id"],
            "title": title,
            "label": "随堂",
            "status": status,
            "state": state,
            "deadline": deadline,
            "deadline_exact": exact,
            "remaining_text": time_line or "",
            "url": url,
        })
    return tasks, unmapped


def masked_diag(diag: dict, rows_raw: list[dict] | None = None) -> list[str]:
    """结构化诊断 → 数字打码的行列表（只进公开日志的安全形状）。

    解析失败时用：把 frame 的行级内容打码后落日志，供下一轮收紧解析。
    数字全部掩成 D（学号/日期/ID 一律不落明文）；每行截 90 字。
    """
    out: list[str] = []
    for s in (diag.get("firstLines") or [])[:10]:
        out.append(safe_text(re.sub(r"\d", "D", str(s)), 90))
    for r in (rows_raw or [])[:8]:
        t = re.sub(r"\d", "D", (r.get("text") or ""))
        out.append("row：" + safe_text(t.replace("\n", " / "), 120))
    return out


# ---------------------------------------------------------------------------
# 浏览器采集（Playwright 懒导入：没装也能 import 本模块跑 selftest）
# ---------------------------------------------------------------------------
_EXTRACT_JS = """
() => {
  const out = {lineCount: 0, rows: [], firstLines: []};
  const body = document.body;
  if (!body) return out;
  const lines = (body.innerText || '').split('\\n')
      .map(s => s.trim()).filter(s => s.length > 0);
  out.lineCount = lines.length;
  out.firstLines = lines.slice(0, 12).map(s => s.length > 50 ? s.slice(0, 50) : s);
  const seen = new Set();
  const els = [];
  for (const el of document.querySelectorAll('li, [onclick], [aria-label]')) {
    const t = (el.innerText || '').trim();
    if (!t || t.length > 400 || seen.has(t)) continue;
    seen.add(t);
    let r = null;
    try { r = el.getBoundingClientRect(); } catch (e) { continue; }
    if (r.width === 0 && r.height === 0) continue;
    els.push({text: t.slice(0, 220),
              y: Math.round(r.y + r.height / 2),
              onclick: (el.getAttribute('onclick') || '').slice(0, 220),
              aria: (el.getAttribute('aria-label') || '').slice(0, 160),
              cls: String(el.className || '').slice(0, 60)});
    if (els.length >= 80) break;
  }
  out.rows = els;
  return out;
}
"""


def _find_active_frame(page):
    for fr in page.frames:
        if "stuactivelist" in (fr.url or "").lower():
            return fr
    return None


def collect_quiz(course: dict, mid_url: str, cookies: list[dict], ua: str) -> dict:
    """一门课的随堂采集：打开课程页 → 等 stuActiveList iframe 渲染 → 提取行。

    返回 {"ok", "rows_raw", "diag", "frame_url", "error", "xhr"}：
    - ok=True 且找到 frame；**内容轮询最长 20s**（Vue 数据是慢 XHR：v1.5.22
      的 27s 固定等待下能渲染、13s 下时有时无，run 43807375956 实测 思修
      一轮 316 字、一轮 0 字——必须等 innerText 出现而不是赌固定时长）；
    - rows_raw 是 EXTRACT_JS 的行级结构化数据，解析在调用方（纯函数）；
    - xhr 是 v1.8.0 的**纯观测**：记录页面加载期间所有 active 相关 JSON
      XHR 的 URL 路径 + 字节大小（不含 query，不落值）。为下一步「XHR JSON
      结构化拦截」收集真实接口形状——先 dump 再动手，不许猜（v1.2.3 教训）。
      观测本身零风险：不改变任何解析行为，回调异常全吞。
    - 任何异常只写 error，**不抛**（主采集保护）。
    """
    result: dict = {"ok": False, "rows_raw": [], "diag": {}, "frame_url": "",
                    "error": "", "xhr": []}
    xhr_seen: list[str] = []

    def _on_response(resp) -> None:
        try:
            url = resp.url or ""
            if "active" not in url.lower():
                return
            ct = (resp.headers or {}).get("content-type") or ""
            if "json" not in ct.lower():
                return
            # 只记路径与状态码：query 串里有每生一份的参数，一个字都不落
            path = re.sub(r"^[a-z]+://[^/]+/", "", url.split("?")[0])[:80]
            xhr_seen.append(f"{path} {resp.status}")
        except Exception:
            pass

    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            ctx = browser.new_context(user_agent=ua)
            if cookies:
                ctx.add_cookies(cookies)
            page = ctx.new_page()
            page.on("response", _on_response)
            # stucoursemiddle 302 → mycourse/stu 课程页（frame 的宿主）
            page.goto(mid_url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(6000)
            frame = _find_active_frame(page)
            if frame is None:
                # 「任务」tab 点击后才建 frame 的形态（v1.5.22 实测）：点一下再等
                try:
                    page.get_by_text("任务", exact=True).first.click(timeout=5000)
                except Exception:
                    pass
                for _ in range(10):
                    page.wait_for_timeout(1000)
                    frame = _find_active_frame(page)
                    if frame is not None:
                        break
            if frame is None:
                result["error"] = "no_stuactivelist_frame"
            else:
                try:
                    frame.wait_for_load_state("domcontentloaded", timeout=20000)
                except Exception:
                    pass
                # 内容轮询：等 innerText 真正出现（最长 20s），而不是赌固定等待
                text_len = 0
                for _ in range(20):
                    try:
                        text_len = frame.evaluate(
                            "() => document.body ? (document.body.innerText || '').trim().length : 0")
                    except Exception:
                        text_len = 0
                    if isinstance(text_len, int) and text_len > 0:
                        break
                    frame.wait_for_timeout(1000)
                if text_len:
                    frame.wait_for_timeout(1500)  # 出内容后再等一拍，列表尾行补齐
                data = frame.evaluate(_EXTRACT_JS)
                rows = data.get("rows") or []
                if not rows and text_len:  # 有内容但没抓到候选：再等一轮
                    frame.wait_for_timeout(2000)
                    data = frame.evaluate(_EXTRACT_JS)
                    rows = data.get("rows") or []
                result.update({"ok": True, "rows_raw": rows, "diag": data,
                               "frame_url": frame.url})
            result["xhr"] = xhr_seen[:20]
            ctx.close()
            browser.close()
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {safe_text(str(e), 200)}"
    return result


# 兼容旧名（v1.5.22 的接口，selftest/文档引用过 cookies_from_session）
def cookies_from_session(session) -> list[dict]:
    """v1.6.0：等价 cookies_for_browser（域归一化版）。"""
    return cookies_for_browser(session)
