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
4. 行 DOM 结构未最终吃透 → parse_active_rows 基于行文本启发式 +
   结构化诊断落日志（数字打码），拿到真实样本后收紧，**不许猜**。

设计约束（v1.5.21 反爬实测教训，沿用）：
- **失败绝不影响主采集**：任何异常返回空结果，调用方只记 WARN；
- 每轮只轮换抓少数课程（study.quiz_per_round，默认 2），课程间 sleep 2s；
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
QUIZ_DONE = {"已完成", "已参与", "已提交", "已结束", "已交", "已答", "已互评", "已过期", "已评分"}
QUIZ_SUBMITTED = {"待批阅", "批阅中", "待批改", "批改中"}
QUIZ_PENDING = {"待做", "未做", "未交", "未提交", "待完成", "进行中", "未参与", "待参与",
                "未开始", "待作答", "未答", "已发布", "待开始"}


def classify_quiz(status: str) -> str:
    s = (status or "").strip()
    if s in QUIZ_DONE:
        return "done"
    if s in QUIZ_SUBMITTED:
        return "submitted"
    if s in QUIZ_PENDING:
        return "pending"
    return "unknown"


_MDHM_RE = re.compile(r"(?:(\d{4})[-/年])?(\d{1,2})[-/月](\d{1,2})日?\s*(\d{1,2}):(\d{2})")


def parse_quiz_time(text: str, now: datetime) -> tuple[str | None, bool]:
    """随堂行的时间文本 → (deadline, 是否精确)。

    两种形状（与 work 列表页同源）：
    - 相对倒计时「剩余2天3小时」→ now + delta，非精确（≈）
    - 绝对时间「12-05 10:00」/「2026-12-05 10:00」→ 按距 now 最近的一年补年
      （学习通详情页同款规则；带年份的视为精确）
    都不是 → (None, False)：诚实留空，绝不冒充「很宽裕」。
    """
    t = (text or "").strip()
    if not t:
        return None, False
    rem = parse_remaining(t)
    if rem is not None:
        return (now + rem).isoformat(timespec="seconds"), False
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
    # 不带年份：候选当年与上一年，取距 now 最近的那个（跨年作业会落对年）
    best: datetime | None = None
    for year in (now.year, now.year - 1, now.year + 1):
        try:
            cand = datetime(year, int(mo), int(d), int(h), int(mi), tzinfo=now.tzinfo)
        except ValueError:
            continue
        if best is None or abs((cand - now).total_seconds()) < abs((best - now).total_seconds()):
            best = cand
    if best is None:
        return None, False
    return best.isoformat(timespec="seconds"), False


def parse_active_rows(rows_raw: list[dict], course: dict, now: datetime,
                      frame_url: str = "") -> tuple[list[dict], list[str]]:
    """frame 里提出的原始行 → 任务 dict 列表。纯函数。

    rows_raw 每项是 EXTRACT_JS 给的 {text, cls, onclick, aria}。
    行文本启发式（结构未吃透前的第一版，据诊断日志迭代）：
    - 状态行 = 短行且能 classify（待做/进行中/已完成…）
    - 时间行 = 含「剩余」或月-日 时:分形状
    - 标题 = 剩余行里最长的一条；aria-label「标题 ; 状态」结构优先补状态
    未收录状态词 → 收集进 unmapped（调用方 WARN），state 诚实判 pending：
    没吃透的状态宁可让用户看见，也不要静默吞掉（§1.1 假成功教训）。
    """
    tasks: list[dict] = []
    unmapped: list[str] = []
    for raw in rows_raw:
        lines = [l.strip() for l in ((raw.get("text") or "").splitlines()) if l.strip()]
        if not lines:
            continue
        status, time_line = "", ""
        title_candidates: list[str] = []
        for l in lines:
            if not status and len(l) <= 12 and classify_quiz(l) != "unknown":
                status = l
                continue
            if not time_line and (parse_remaining(l) is not None or _MDHM_RE.search(l)):
                time_line = l
                continue
            title_candidates.append(l)
        aria = (raw.get("aria") or "").strip()
        if not status and ";" in aria:
            parts = [p.strip() for p in aria.split(";")]
            if len(parts) >= 2 and classify_quiz(parts[1]) != "unknown":
                status = parts[1]
        if not title_candidates and aria:
            # aria-label 本身常就是「标题 ; 状态」
            title_candidates = [aria.split(";")[0].strip()]
        title = scrub_task_title(max(title_candidates, key=len) if title_candidates else "")
        if not title:
            continue
        state = classify_quiz(status)
        if state == "unknown" and status:
            unmapped.append(status)
            state = "pending"
        deadline, exact = (None, False)
        if time_line:
            deadline, exact = parse_quiz_time(time_line, now)
        # 行身份：onclick 里的长数字串（activeId/knowledgeId），拿不到用标题兜底
        key = ""
        om = re.search(r"(\d{6,})", raw.get("onclick") or "")
        if om:
            key = om.group(1)
        else:
            km = re.search(r"(\d{4,})", title)
            key = km.group(1) if km else title
        url = ""
        um = re.search(r"https?://[^\s'\"]+", raw.get("onclick") or "")
        if um:
            url = um.group(0)
        elif frame_url:
            url = frame_url
        tasks.append({
            "id": make_id(course["course_id"], course["class_id"], "quiz", key),
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
  const cands = document.querySelectorAll('li, [onclick], [aria-label]');
  for (const el of cands) {
    const t = (el.innerText || '').trim();
    if (t.length < 2 || t.length > 400 || seen.has(t)) continue;
    seen.add(t);
    out.rows.push({text: t.slice(0, 220),
                   cls: String(el.className || '').slice(0, 80),
                   onclick: (el.getAttribute('onclick') || '').slice(0, 220),
                   aria: (el.getAttribute('aria-label') || '').slice(0, 160)});
    if (out.rows.length >= 30) break;
  }
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

    返回 {"ok", "rows_raw", "diag", "frame_url", "error"}：
    - ok=True 且找到 frame（行可为空 = 这门课真没随堂）；
    - rows_raw 是 EXTRACT_JS 的行级结构化数据，解析在调用方（纯函数）；
    - 任何异常只写 error，**不抛**（主采集保护）。
    """
    result: dict = {"ok": False, "rows_raw": [], "diag": {}, "frame_url": "", "error": ""}
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            ctx = browser.new_context(user_agent=ua)
            if cookies:
                ctx.add_cookies(cookies)
            page = ctx.new_page()
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
                frame.wait_for_timeout(4000)  # Vue 渲染
                data = frame.evaluate(_EXTRACT_JS)
                rows = data.get("rows") or []
                if not rows:  # 再等一轮，列表可能慢加载
                    frame.wait_for_timeout(3000)
                    data = frame.evaluate(_EXTRACT_JS)
                    rows = data.get("rows") or []
                result.update({"ok": True, "rows_raw": rows, "diag": data,
                               "frame_url": frame.url})
            ctx.close()
            browser.close()
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {safe_text(str(e), 200)}"
    return result


# 兼容旧名（v1.5.22 的接口，selftest/文档引用过 cookies_from_session）
def cookies_from_session(session) -> list[dict]:
    """v1.6.0：等价 cookies_for_browser（域归一化版）。"""
    return cookies_for_browser(session)
