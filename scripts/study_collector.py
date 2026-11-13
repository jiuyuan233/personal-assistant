"""学习通作业/考试采集（源2）。

四步链（已实测打通，缺一步都会拿到「提示」页）：

  1. POST passport2.huuc.edu.cn/fanyalogin
     账号密码都要 AES-128-CBC 加密，key = iv = b"u2oh6Vu^HWe4_AES"，PKCS7，base64
  2. GET  mooc2-ans.huuc.edu.cn/visit/courses/list  → 全部课程的 stucoursemiddle 链接
  3. GET  mooc1.huuc.edu.cn/visit/stucoursemiddle   → 302 跳到 stu 页，
     页面里现铸三个 token：#enc（ stuenc）、#workEnc（作业）、#examEnc（考试）
  4. GET  作业列表 /mooc2/work/list?courseId=&classId=&cpi=&ut=s&stuenc=<enc>&enc=<workEnc>
     GET  考试列表 /exam-ans/mooc2/exam/exam-list?courseid=&clazzid=&cpi=&ut=s&stuenc=<enc>&enc=<examEnc>

三个必须记住的坑：
- `ut=s` 不能省，否则「无权限的操作！」
- 作业用驼峰 courseId/classId，**考试用小写 courseid/clazzid**，混用会「参数传递不正确！」
- stuenc 和 enc 是两个不同的 token，都要带；enc 每次访问都变，所以不能进任务 ID

列表页只给相对倒计时（「剩余1小时12分钟」），没有绝对时间戳，
所以 deadline = 抓取时刻 + 剩余时长，标记 deadline_exact=False。
"""
from __future__ import annotations

import base64
import re
import time
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse

import requests

from dedup import load_list, merge_tasks, save_list, sort_by_deadline
from utils import (DATA_DIR, clean_html_text, env, fmt_remaining, load_json, log,
                   make_id, now_bj, parse_datetime, parse_remaining, safe_text,
                   save_json, scrub_course_name, scrub_task_title,
                   strip_personal_fields)

STUDY_DIR = DATA_DIR / "study"
TASKS_PATH = STUDY_DIR / "tasks.json"
COURSES_PATH = STUDY_DIR / "courses.json"
META_PATH = STUDY_DIR / "meta.json"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36")
AES_KEY = b"u2oh6Vu^HWe4_AES"

PENDING = {"待做", "未做", "待提交", "未提交", "未交", "待完成", "进行中", "待互评",
           "待考试", "未开始", "待作答", "待批阅互评"}
SUBMITTED = {"待批阅", "已提交", "批阅中", "待批改", "批改中"}
DONE = {"已完成", "已互评", "已批阅", "已结束", "已交", "已评分"}

MIDDLE_RE = re.compile(r"stucoursemiddle\?([^\"'\s>]+)")
HIDDEN_RE = r'id="{k}"[^>]*value="([a-f0-9]+)"'
# 课程名在列表页每个 li 内的 course-name span 上；middle 页的首个 h2 是全站横幅
# 「在线学习诚信承诺书」，不能用，只有 <title> 才是课程名。
COURSE_NAME_RE = re.compile(r'class="course-name[^"]*"[^>]*title="([^"]*)"')
TITLE_TAG_RE = re.compile(r"<title>(.*?)</title>", re.I | re.S)
NAME_LOOKAHEAD = 2500
WORK_ROW_RE = re.compile(r'<li\b[^>]*onclick="goTask\(this\);"[^>]*>', re.I)
DATA_URL_RE = re.compile(r'data="(https?://[^"]+)"', re.I)
ARIA_RE = re.compile(r'aria-label="([^"]*)"', re.I)
STATUS_RE = re.compile(r'<p class="status[^"]*">\s*([^<]*?)\s*</p>', re.I)
LABEL_RE = re.compile(r'<i class="label[^"]*">\s*([^<]*?)\s*</i>', re.I)
TITLE_RE = re.compile(r'<p class="overHidden2[^"]*">\s*([^<]*?)\s*</p>', re.I)
TIME_RE = re.compile(r'<div class="time\s+(\w+)"[^>]*>(.*?)</div>', re.I | re.S)
EXAM_ONCLICK_RE = re.compile("onclick=\"(\\w+)\\('(\\d+)','(\\d+)'\\)\"")
DETAIL_DT_RE = re.compile(
    r"(?:截止时间|结束时间|作答时间|截止|结束)[^0-9]{0,40}(\d{4}[-/年]\d{1,2}[-/月]\d{1,2}[日]?\s*\d{1,2}:\d{2})")
# 实测（2026-11-13 studydiag，run 43448758305）：作业详情页给的是
#   <p>作答时间:<em>11-05 23:59</em>至<em>11-10 23:59</em></p>
# 月-日 时:分、**不带年份**、值包在 <em> 里。列表页对已逾期的作业不给倒计时，
# 所以真实截止时间只有这里能拿到。漏了它 → deadline=null → 被当成「无限远」→
# 已逾期的作业照样显示进 README（用户报的 bug）。
DETAIL_START_RE = re.compile(
    r"(?:开始时间|发布时间|布置时间|起始时间|开始)[^0-9]{0,40}(\d{4}[-/年]\d{1,2}[-/月]\d{1,2}[日]?(?:\s*\d{1,2}:\d{2})?)")
# 互评页：「互评时间：11-06 07:01 至 11-10 23:01」；作业页：「作答时间:…至…」
WINDOW_LABELS = ("互评时间", "作答时间", "开始时间", "截止时间")
MD_HM_RE = re.compile(r"(?:(\d{4})[-/年])?(\d{1,2})[-/月](\d{1,2})日?\s*(\d{1,2}):(\d{2})")


# ---------------------------------------------------------------------------
# 加密
# ---------------------------------------------------------------------------
def aes_encrypt(plain: str) -> str:
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import pad
    cipher = AES.new(AES_KEY, AES.MODE_CBC, AES_KEY)
    return base64.b64encode(cipher.encrypt(pad(plain.encode("utf-8"), 16))).decode()


def classify(status: str) -> str:
    s = (status or "").strip()
    if s in DONE:
        return "done"
    if s in SUBMITTED:
        return "submitted"
    if s in PENDING:
        return "pending"
    return "unknown"


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------
class Chaoxing:
    def __init__(self, cfg: dict, username: str, password: str) -> None:
        self.cfg = cfg
        self.username = username
        self.password = password
        self.passport = cfg.get("passport", "https://passport2.huuc.edu.cn").rstrip("/")
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"})
        self.logged_in = False

    def login(self) -> bool:
        try:
            self.s.get(f"{self.passport}/login", params={"fid": self.cfg.get("fid", "12")},
                       timeout=30)
            r = self.s.post(
                f"{self.passport}/fanyalogin",
                data={
                    "fid": self.cfg.get("fid", "12"),
                    "uname": aes_encrypt(self.username),
                    "password": aes_encrypt(self.password),
                    "t": "true",
                    "refer": self.cfg.get("refer", ""),
                    "forbidotherlogin": "0",
                    "validate": "",
                    "doubleFactorLogin": "0",
                    "independentId": "0",
                    "independentNameId": "0",
                },
                headers={"X-Requested-With": "XMLHttpRequest",
                         "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
                timeout=30,
            )
            data = r.json()
        except Exception as e:
            log(f"学习通登录异常：{safe_text(e, 200)}", level="ERROR")
            return False
        if data.get("status") is True:
            self.logged_in = True
            log("学习通登录成功")
            return True
        # 只打 msg，绝不回显 uname/password
        log(f"学习通登录失败：{safe_text(data.get('msg') or data, 160)}", level="ERROR")
        return False

    def get(self, url: str, *, params: dict | None = None, follow: bool = True) -> str | None:
        try:
            r = self.s.get(url, params=params, timeout=40, allow_redirects=follow)
        except requests.RequestException as e:
            log(f"请求失败 {urlparse(url).path}: {safe_text(e, 160)}", level="WARN")
            return None
        if r.status_code != 200:
            log(f"请求 {urlparse(url).path} → HTTP {r.status_code}", level="WARN")
            return None
        r.encoding = r.apparent_encoding or "utf-8"
        return r.text

    def list_courses(self) -> list[dict]:
        url = self.cfg.get("course_list", "https://mooc2-ans.huuc.edu.cn/visit/courses/list")
        html = self.get(url, params={"v": "1", "rss": "1", "start": "0", "size": "100"})
        if not html:
            return []
        out, seen = [], set()
        for m in MIDDLE_RE.finditer(html):
            q = {k: v[0] for k, v in parse_qs(m.group(1)).items()}
            cid, clazz, cpi = q.get("courseid"), q.get("clazzid"), q.get("cpi")
            if not (cid and clazz and cpi):
                continue
            key = (cid, clazz)
            if key in seen:
                continue
            seen.add(key)
            nm = COURSE_NAME_RE.search(html[m.end():m.end() + NAME_LOOKAHEAD])
            out.append({"course_id": cid, "class_id": clazz, "cpi": cpi,
                        "ismooc2": q.get("ismooc2", "1"),
                        "name": scrub_course_name(clean_html_text(nm.group(1))) if nm else ""})
        return out

    def mint(self, course: dict) -> dict | None:
        """访问 stucoursemiddle，跟随 302 拿到现铸的三个 token。"""
        base = self.cfg.get("middle", "https://mooc1.huuc.edu.cn/visit/stucoursemiddle")
        html = self.get(base, params={
            "courseid": course["course_id"], "clazzid": course["class_id"],
            "cpi": course["cpi"], "ismooc2": course.get("ismooc2", "1"),
        })
        if not html:
            return None
        tokens = {}
        for k in ("enc", "workEnc", "examEnc"):
            m = re.search(HIDDEN_RE.format(k=k), html)
            if m:
                tokens[k] = m.group(1)
        if not tokens.get("enc") or not tokens.get("workEnc"):
            log(f"课程 {course['course_id']} 未拿到 token（可能已结课或无权限）", level="WARN")
            return None
        if not course.get("name"):
            tm = TITLE_TAG_RE.search(html)
            name = scrub_course_name(clean_html_text(tm.group(1))) if tm else ""
            course["name"] = name if name and name != "提示" else f"课程{course['course_id']}"
        return tokens

    def work_html(self, course: dict, tokens: dict) -> str | None:
        return self.get(self.cfg.get("work_list", "https://mooc1.huuc.edu.cn/mooc2/work/list"),
                        params={"courseId": course["course_id"], "classId": course["class_id"],
                                "cpi": course["cpi"], "ut": "s",
                                "stuenc": tokens["enc"], "enc": tokens["workEnc"]})

    def exam_html(self, course: dict, tokens: dict) -> str | None:
        if not tokens.get("examEnc"):
            return None
        url = self.cfg.get("exam_list",
                           "https://mooc1.huuc.edu.cn/exam-ans/mooc2/exam/exam-list")
        # 考试模块用小写参数名，和作业模块不一样
        return self.get(url, params={"courseid": course["course_id"], "clazzid": course["class_id"],
                                     "cpi": course["cpi"], "ut": "s",
                                     "stuenc": tokens["enc"], "enc": tokens["examEnc"]})


# 会话中途失效时超星返回的是 **HTTP 200 的登录页**，不是「提示」页。
# 只认那三个字符串会把登录页当正常页面解析 → rows=[] → README 显示「无作业」+「✅ 正常」。
# 这是审计 §1.1 的「假成功」，比崩溃更危险：用户以为真没作业，从而错过截止。
LOGIN_MARKERS = ("fanyalogin", 'name="uname"', 'id="loginName"', "<title>登录</title>")


def is_tip_page(html: str | None) -> bool:
    """页面不可用（提示页 / 权限页 / 登录页）都算 True。"""
    if not html:
        return True
    if "<title>提示</title>" in html or "参数传递不正确" in html or "无权限的操作" in html:
        return True
    head = html[:6000]
    return any(k in head for k in LOGIN_MARKERS)


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------
def _row_deadline(chunk: str, now: datetime) -> tuple[str | None, bool, str, bool]:
    """返回 (deadline, 是否精确, 倒计时原文, 是否已过期)。"""
    m = TIME_RE.search(chunk)
    if not m:
        return None, False, "", False
    over = m.group(1).lower() == "over"
    text = clean_html_text(m.group(2))
    rem = parse_remaining(text)
    if rem is None:
        return None, False, text, over
    delta = -rem if over else rem
    return (now + delta).isoformat(timespec="seconds"), False, text, over


def parse_work_rows(html: str, course: dict, now: datetime) -> list[dict]:
    out = []
    marks = list(WORK_ROW_RE.finditer(html))
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else min(len(html), m.start() + 2500)
        chunk = html[m.start():end]
        um = DATA_URL_RE.search(chunk)
        if not um:
            continue
        url = um.group(1)
        q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        work_id, answer_id = q.get("workId", ""), q.get("answerId", "")
        path = urlparse(url).path
        kind = "eval" if "eval-list" in path else "work"

        aria = ARIA_RE.search(m.group(0))
        title, status = "", ""
        if aria:
            parts = [p.strip() for p in aria.group(1).split(";")]
            title = parts[0]
            status = parts[1] if len(parts) > 1 else ""
        if not title:
            tm = TITLE_RE.search(chunk)
            title = clean_html_text(tm.group(1)) if tm else ""
        if not status:
            sm = STATUS_RE.search(chunk)
            status = clean_html_text(sm.group(1)) if sm else ""
        if not title:
            continue
        title = scrub_task_title(title)
        lm = LABEL_RE.search(chunk)
        deadline, exact, rem_text, over = _row_deadline(chunk, now)
        label = clean_html_text(lm.group(1)) if lm else ""
        state = classify(status)
        # 互评模式下「待批阅」+ 仍在倒计时 = 用户还得去互评，不是真的没事可做
        if state == "submitted" and label == "互评" and deadline and not over:
            state = "pending"
        out.append({
            "id": make_id(course["course_id"], course["class_id"], kind, work_id, answer_id),
            "kind": kind,
            "course": course.get("name") or "",
            "course_id": course["course_id"],
            "class_id": course["class_id"],
            "cpi": course["cpi"],
            "title": title,
            "work_id": work_id,
            "answer_id": answer_id,
            "label": label,
            "status": status,
            "state": state,
            "deadline": deadline,
            "deadline_exact": exact,
            "remaining_text": rem_text,
            "url": url,
        })
    return out


def parse_exam_rows(html: str, course: dict, now: datetime,
                    tokens: dict | None = None) -> list[dict]:
    out = []
    # 考试行是 <li> 里嵌 onclick="viewExamAnswer('id','rel')" / enterExam(...)
    for m in re.finditer(r"<li>(.*?)</li>", html, re.S):
        chunk = m.group(1)
        om = EXAM_ONCLICK_RE.search(chunk)
        tm = TITLE_RE.search(chunk)
        if not om or not tm:
            continue
        title = clean_html_text(tm.group(1))
        if not title:
            continue
        title = scrub_task_title(title)
        sm = STATUS_RE.search(chunk)
        status = clean_html_text(sm.group(1)) if sm else ""
        exam_id, relation_id = om.group(2), om.group(3)
        deadline, exact, rem_text, _over = _row_deadline(chunk, now)
        # §3.7 原来这个 URL 没带 stuenc/enc，按四步链的实测结论必然「参数传递不正确」。
        # 补上现铸 token，至少课程级考试列表能打开。单场考试的深链需要 viewExamAnswer
        # 的 JS 实现，未实测前不猜（见 diagnose 的 JS dump）。
        url = (f"https://mooc1.huuc.edu.cn/exam-ans/mooc2/exam/exam-list"
               f"?courseid={course['course_id']}&clazzid={course['class_id']}"
               f"&cpi={course['cpi']}&ut=s")
        if tokens:
            url += f"&stuenc={tokens.get('enc', '')}&enc={tokens.get('examEnc', '')}"
        out.append({
            "id": make_id(course["course_id"], course["class_id"], "exam", exam_id, relation_id),
            "kind": "exam",
            "course": course.get("name") or "",
            "course_id": course["course_id"],
            "class_id": course["class_id"],
            "cpi": course["cpi"],
            "title": title,
            "work_id": exam_id,
            "answer_id": relation_id,
            "label": "考试",
            "status": status,
            "state": classify(status),
            "deadline": deadline,
            "deadline_exact": exact,
            "remaining_text": rem_text,
            "url": url,
        })
    return out


def detail_deadline(html: str) -> datetime | None:
    m = DETAIL_DT_RE.search(html)
    if not m:
        return None
    return parse_datetime(m.group(1).replace("年", "-").replace("月", "-").replace("日", ""))


def detail_start(html: str) -> datetime | None:
    m = DETAIL_START_RE.search(html)
    if not m:
        return None
    return parse_datetime(m.group(1).replace("年", "-").replace("月", "-").replace("日", ""))


def detail_time_hints(html: str, limit: int = 5) -> list[str]:
    """详情页里含时间字样的文本片段。正则失配时用它看清页面到底给了什么。"""
    out: list[str] = []
    for m in re.finditer(r"[^<>]{0,40}(?:时间|截止|开始)[^<>]{0,60}", html):
        s = clean_html_text(m.group(0)).strip()
        if s and s not in out:
            out.append(s)
        if len(out) >= limit:
            break
    return out


def detail_time_window(html: str, now: datetime, label: str) -> tuple[datetime | None, datetime | None]:
    """详情页里「<label>：起 至 止」形式的真实时间窗 → (开始, 截止)。

    实测两种标签都给这个形状：互评时间（互评页）、作答时间（作业页）。
    值可能包在 <em> 里，所以先剥标签再解析。页面不给年份，按当前年推断。
    """
    m = re.search(re.escape(label) + r"\s*[：:]\s*(.{0,140})", html or "", re.S)
    if not m:
        return None, None
    parts = MD_HM_RE.findall(clean_html_text(m.group(1)))
    if len(parts) < 2:
        return None, None

    def build(p: tuple) -> datetime | None:
        y, mo, d, h, mi = p
        year = int(y) if y else now.year
        try:
            dt = datetime(year, int(mo), int(d), int(h), int(mi), tzinfo=now.tzinfo)
        except ValueError:
            return None
        # 页面不给年份：算出来在半年以后，说明其实是去年的窗口
        if not y and dt - now > timedelta(days=180):
            dt = dt.replace(year=year - 1)
        return dt

    start, end = build(parts[0]), build(parts[1])
    # 跨年窗口（12-28 至 01-05）：止早于起说明止其实是下一年
    if start and end and end < start and not parts[1][0]:
        end = end.replace(year=end.year + 1)
    return start, end


def detail_eval_window(html: str, now: datetime) -> tuple[datetime | None, datetime | None]:
    """互评窗口。窗口一结束就不能再互评，所以结束时间即截止时间。"""
    return detail_time_window(html, now, "互评时间")


def detail_work_window(html: str, now: datetime) -> tuple[datetime | None, datetime | None]:
    """作业「作答时间」窗口。这是逾期作业唯一的真实截止时间来源。"""
    return detail_time_window(html, now, "作答时间")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def _save_meta(now: datetime, **kw) -> dict:
    """写 meta.json，**保留上一次的 last_success**。

    审计 §3.8：原来三个失败分支都用全新 dict 覆盖，把 last_success 抹掉了，
    事后无法判断数据有多旧。失败时只更新 last_attempt 与失败原因。
    """
    old = load_json(META_PATH) or {}
    meta = {"last_attempt": now.isoformat(timespec="seconds"),
            "last_success": old.get("last_success"), **kw}
    save_json(META_PATH, meta)
    return meta


def run() -> dict:
    cfg = (load_json(DATA_DIR / "sources.json") or {}).get("study") or {}
    username, password = env("CX_USERNAME"), env("CX_PASSWORD")
    if not username or not password:
        log("未配置 CX_USERNAME / CX_PASSWORD，跳过学习通采集", level="WARN")
        _save_meta(now_bj(), ok=False, status="failed", reason="no_credentials")
        return {"ok": False, "skipped": "no_credentials"}

    now = now_bj()
    cx = Chaoxing(cfg, username, password)
    if not cx.login():
        _save_meta(now, ok=False, status="failed", reason="login_failed")
        return {"ok": False, "reason": "login_failed"}

    courses = cx.list_courses()[:int(cfg.get("max_courses") or 40)]
    log(f"课程总数：{len(courses)}")
    if not courses:
        _save_meta(now, ok=False, status="failed", reason="no_courses")
        return {"ok": False, "reason": "no_courses"}

    # §3.6 用户明确说「我一共 14 个课」。抓到 3 门也算成功的话，页面改版导致漏抓无人发现。
    # 不是硬失败：选课会变，所以只告警 + 降级为 partial。
    expected = int(cfg.get("expected_courses") or 0)
    anomalies: list[str] = []
    if expected and len(courses) != expected:
        anomalies.append(f"课程数 {len(courses)} ≠ 期望 {expected}")
        log(f"课程数偏离期望：{len(courses)} ≠ {expected}", level="WARN")

    prev_counts = {(c.get("course_id"), c.get("class_id")): (c.get("task_count") or 0)
                   for c in load_list(COURSES_PATH)}
    all_tasks: list[dict] = []
    ok_courses, failed = 0, 0
    detail_budget = int(cfg.get("max_detail") or 8) if cfg.get("fetch_detail") else 0

    for i, c in enumerate(courses):
        # §5.4 每门课用「当次请求时刻」反推倒计时，而不是循环开始前的 now。
        # 14 门课几十秒，越靠后的课反推结果越早。
        req_now = now_bj()
        tokens = cx.mint(c)
        if not tokens:
            failed += 1
            continue
        whtml = cx.work_html(c, tokens)
        if is_tip_page(whtml):
            log(f"课程 {c.get('name')} 作业列表被拒（token/权限/登录态失效）", level="WARN")
            failed += 1
            continue
        rows = parse_work_rows(whtml, c, req_now)
        ehtml = cx.exam_html(c, tokens)
        if ehtml and not is_tip_page(ehtml):
            rows.extend(parse_exam_rows(ehtml, c, req_now, tokens))
        # §1.1 从「有任务」变成「0 行」要判可疑，不能当成「真的没作业」
        prev = prev_counts.get((c.get("course_id"), c.get("class_id")))
        if prev and not rows:
            anomalies.append(f"{c.get('name')} 上次 {prev} 条本次 0 条")
            log(f"可疑：{c.get('name')} 上次 {prev} 条任务，本次解析出 0 条", level="WARN")
        all_tasks.extend(rows)
        ok_courses += 1
        c["task_count"] = len(rows)
        c["pending_count"] = sum(1 for r in rows if r["state"] == "pending")
        if i < len(courses) - 1:
            time.sleep(0.3)  # 别把学校站点打疼

    log(f"扫描课程：{ok_courses}，失败：{failed}")

    # §1.2 一门都没抓到就**不落盘**。merge_tasks 的语义是「新列表即权威列表」，
    # 空列表会把历史任务全部清空，而 meta.ok 还是 True —— 最危险的静默失败。
    if ok_courses == 0:
        _save_meta(now, ok=False, status="failed", reason="all_courses_failed",
                   courses=len(courses), courses_ok=0, courses_failed=failed)
        log("全部课程采集失败，保留上一次的任务数据不覆盖", level="ERROR")
        return {"ok": False, "reason": "all_courses_failed", "courses": len(courses)}

    # 详情页：补真实截止时间 + 开始时间。
    # §3.5 原来按课程遍历序取前 N 条，排在后面的课里「1 小时后截止」的作业可能拿不到精确时间。
    # 现在：① 待完成且**无截止**的一律抓（不受预算限制，那正是用户报的 bug）
    #       ② 其余按反推截止时间从近到远排序，紧急的优先。
    if detail_budget > 0:
        pending = [t for t in all_tasks if t["state"] == "pending"]
        unknown = [t for t in pending if not t.get("deadline")]
        known = sorted((t for t in pending if t.get("deadline")),
                       key=lambda t: parse_datetime(t["deadline"]) or now)
        need = unknown + known[:max(0, detail_budget - len(unknown))]
        log(f"详情页预算 {detail_budget}，无截止待完成 {len(unknown)} 条优先，实际抓 {len(need)} 条")
        got_start = got_dl = 0
        for t in need:
            req_now = now_bj()
            html = cx.get(t["url"])
            if not html or is_tip_page(html):
                log(f"详情页不可用（{len(html or '')} 字节）：{t['title']}", level="WARN")
                continue
            dt = detail_deadline(html)
            ws, we = detail_time_window(html, req_now, "互评时间")
            if not we:
                ws, we = detail_work_window(html, req_now)
            if dt and not t.get("deadline_exact"):
                t["deadline"] = dt.isoformat(timespec="seconds")
                t["deadline_exact"] = True
                got_dl += 1
                log(f"详情页补全截止时间：{t['title']} → {t['deadline']}")
            elif we and not t.get("deadline_exact"):
                t["deadline"] = we.isoformat(timespec="seconds")
                t["deadline_exact"] = True
                got_dl += 1
                log(f"时间窗补全截止时间：{t['title']} → {t['deadline']}")
            st = ws or detail_start(html)
            if st:
                t["start_time"] = st.isoformat(timespec="seconds")
                got_start += 1
            if not dt and not st and not we:
                log(f"详情页无时间字段（{len(html)} 字节）：{t['title']} "
                    f"片段={detail_time_hints(html)}", level="WARN")
        log(f"详情页补全截止 {got_dl} 条 / 开始时间 {got_start} 条（共 {len(need)} 条）")

    still_unknown = [t for t in all_tasks
                     if t["state"] == "pending" and not t.get("deadline")]
    for t in still_unknown:
        anomalies.append(f"待完成但抓不到截止：{t.get('course')} · {t.get('title')}")
        log(f"⚠️ 待完成却无截止时间，可能已逾期：{t.get('course')} · {t.get('title')}", level="WARN")

    merged = merge_tasks(load_list(TASKS_PATH), all_tasks, now)
    # 审计 §2.5 / §7.2：data/ 是公开仓库的一部分，不是本地缓存。
    # cpi / answerId / enc 每生一份，落盘前必须抹掉；完整深链只在本轮内存里用于抓详情页。
    link_mode = cfg.get("public_links") or "course"
    merged = [strip_personal_fields(t, link_mode) for t in merged]
    save_list(TASKS_PATH, merged, course_count=len(courses))
    save_list(COURSES_PATH, [{k: v for k, v in c.items() if k != "cpi"} for c in courses])

    pending = [t for t in merged if t["state"] == "pending"]
    far = now + timedelta(days=3650)
    overdue = [t for t in pending if (parse_datetime(t["deadline"]) or far) < now]
    soon = [t for t in pending
            if timedelta(0) <= (parse_datetime(t["deadline"]) or far) - now <= timedelta(hours=24)]
    log(f"待完成：{len(pending)}")
    log(f"24小时内截止：{len(soon)}")
    log(f"已逾期：{len(overdue)}")
    for t in sort_by_deadline(overdue + soon)[:5]:
        log(f"[urgent] {t['course']} · {t['title']} · {fmt_remaining(parse_datetime(t['deadline']))}")

    # §1.1 ok 不再无条件为 True：有失败课程 / 可疑清空 / 课程数偏离 / 截止未知 → partial
    ok = not failed and not anomalies
    _save_meta(now, ok=ok, status="ok" if ok else "partial",
               last_success=now.isoformat(timespec="seconds"),
               courses=len(courses), courses_ok=ok_courses, courses_failed=failed,
               tasks=len(merged), pending=len(pending),
               unknown_deadline=len(still_unknown), anomalies=anomalies[:12])
    return {"ok": ok, "status": "ok" if ok else "partial",
            "courses": len(courses), "courses_ok": ok_courses,
            "tasks": len(merged), "pending": len(pending),
            "soon": len(soon), "overdue": len(overdue),
            "unknown_deadline": len(still_unknown), "anomalies": anomalies[:12]}


def _shape(s: str) -> str:
    """数字一律换成 D，只保留标点与标签结构。

    Actions 日志是公开的：要看见「页面给的是什么格式」，但不能把真实日期、
    学号、ID 写进日志。形状足够写正则，值不需要。
    """
    return re.sub(r"\d", "D", s or "")


def _mask_title(chunk: str) -> str:
    """标题里可能带教师姓名，dump 前换成占位符。"""
    chunk = re.sub(r'aria-label="[^"]*"', 'aria-label="TITLE"', chunk)
    return re.sub(r'(<p class="overHidden2[^"]*">)[^<]*(</p>)', r"\1TITLE\2", chunk)


def diagnose(max_courses: int = 40, max_dump: int = 4) -> dict:
    """实测：为什么有的任务抓不到截止时间。

    用户报「明明有截止时间却找不到，已截止的任务被显示进 README」。
    落盘数据里确实有 pending 且 deadline=null 的任务。
    这个模式把那些行的**真实 HTML 形状**打出来，用来写正则 —— 不许猜。
    """
    cfg = (load_json(DATA_DIR / "sources.json") or {}).get("study") or {}
    username, password = env("CX_USERNAME"), env("CX_PASSWORD")
    if not username or not password:
        log("未配置凭据，无法诊断", level="ERROR")
        return {"ok": False}
    now = now_bj()
    cx = Chaoxing(cfg, username, password)
    if not cx.login():
        return {"ok": False, "reason": "login_failed"}
    courses = cx.list_courses()[:max_courses]
    log(f"[diag] 课程 {len(courses)} 门")

    time_shapes: dict[str, int] = {}
    problems: list[dict] = []
    for c in courses:
        tokens = cx.mint(c)
        if not tokens:
            continue
        whtml = cx.work_html(c, tokens)
        if is_tip_page(whtml):
            log(f"[diag] 作业列表被拒：{c.get('name')}", level="WARN")
            continue
        marks = list(WORK_ROW_RE.finditer(whtml))
        rows = parse_work_rows(whtml, c, now)
        for i, m in enumerate(marks):
            end = marks[i + 1].start() if i + 1 < len(marks) else min(len(whtml), m.start() + 2500)
            chunk = whtml[m.start():end]
            for tm in TIME_RE.finditer(chunk):
                key = f'class="time {tm.group(1)}" → {_shape(clean_html_text(tm.group(2)))[:60]}'
                time_shapes[key] = time_shapes.get(key, 0) + 1
            # 没有 time div 的行：整行形状都要看
            if not TIME_RE.search(chunk):
                problems.append({"course": c.get("name"), "chunk": chunk, "row": None})
        for r in rows:
            if r["state"] == "pending" and not r.get("deadline"):
                problems.append({"course": c.get("name"), "chunk": None, "row": r})
        ehtml = cx.exam_html(c, tokens)
        if ehtml and not is_tip_page(ehtml):
            for r in parse_exam_rows(ehtml, c, now):
                if r["state"] == "pending" and not r.get("deadline"):
                    problems.append({"course": c.get("name"), "chunk": None, "row": r})
            # §3.7 单场考试深链：把 JS 函数体打出来看它到底跳哪个 URL，不许猜
            flat_e = re.sub(r"\s+", " ", ehtml)
            for fm in re.finditer(r"function\s+(viewExamAnswer|enterExam|goExam|toExam)\s*\([^)]*\)\s*\{.{0,600}", flat_e):
                log(f"[diag] 考试 JS：{_shape(fm.group(0))[:600]}")
            for om in EXAM_ONCLICK_RE.finditer(ehtml):
                log(f"[diag] 考试 onclick 函数名：{om.group(1)}")
                break
        time.sleep(0.3)

    log(f"[diag] time div 形状共 {len(time_shapes)} 种：")
    for k, v in sorted(time_shapes.items(), key=lambda x: -x[1]):
        log(f"[diag]   ×{v} {k}")

    log(f"[diag] 抓不到截止时间的待完成/无time行：{len(problems)} 条")
    # 待完成且无截止的排最前 —— 那才是用户报的 bug；已完成的行没有 time div 是正常的
    problems.sort(key=lambda p: 0 if p.get("row") else 1)
    for p in problems[:max_dump]:
        r = p.get("row")
        if r:
            log(f"[diag] --- pending 无截止：kind={r['kind']} status={r['status']} "
                f"label={r['label']} rem={_shape(r['remaining_text'])}")
            html = cx.get(r["url"])
            if not html or is_tip_page(html):
                log(f"[diag]     详情页不可用（{len(html or '')} 字节）")
                continue
            flat = re.sub(r"\s+", " ", html)
            log(f"[diag]     详情页 {len(html)} 字节，含日期形状的文本节点：")
            seen = set()
            for m in re.finditer(r"[^<>]{0,70}(?:\d{4}[-/年]|\d{1,2}[-/月]\d{1,2})[^<>]{0,70}", flat):
                s = _shape(clean_html_text(m.group(0)))
                if s and s not in seen:
                    seen.add(s)
                    log(f"[diag]       {s[:160]}")
                if len(seen) >= 16:
                    break
            log("[diag]     时间标签附近的原始 HTML（保留标签）：")
            for m in re.finditer(r"(?:截止时间|结束时间|作答时间|开始时间|发布时间|互评时间|剩余|时间)", flat):
                seg = flat[max(0, m.start() - 100):m.start() + 240]
                log(f"[diag]       {_shape(_mask_title(seg))[:340]}")
            log("[diag]     JS 里与时间/截止有关的赋值：")
            js_seen = set()
            for m in re.finditer(r"[\w.$]*(?:[Tt]ime|[Dd]eadline|[Ee]nd[Tt]ime|[Ss]tart)[\w.$]*\s*[:=]\s*[^,;}]{0,90}", flat):
                s = _shape(m.group(0))[:150]
                if s not in js_seen:
                    js_seen.add(s)
                    log(f"[diag]       {s}")
                if len(js_seen) >= 20:
                    break
            log("[diag]     页面里的 ajax/接口路径：")
            api_seen = set()
            for m in re.finditer(r"""["'](/[^"'\s]{4,90}|https?://[^"'\s]{4,90})["']""", flat):
                u = m.group(1)
                if re.search(r"work|exam|eval|task|detail|time", u, re.I) and not re.search(r"\.(?:js|css|png|jpg|gif|ico)", u, re.I):
                    s = _shape(u)[:130]
                    if s not in api_seen:
                        api_seen.add(s)
                        log(f"[diag]       {s}")
                if len(api_seen) >= 18:
                    break
        else:
            log(f"[diag] --- 无 time div 的行（课程 {p['course']}）：")
            flat_chunk = re.sub(r"\s+", " ", p["chunk"])
            log(f"[diag]     {_shape(_mask_title(flat_chunk))[:1000]}")
    return {"ok": True, "shapes": len(time_shapes), "problems": len(problems)}


# ---------------------------------------------------------------------------
# 离线解析样本（selftest 与 probe 共用，审计 §4.4 ②）
# ---------------------------------------------------------------------------
# ID 一律用合成值。真实 course_id / class_id 也是可定位信息，不进源码。
SAMPLE_COURSE = {"course_id": "100000001", "class_id": "100000002",
                 "cpi": "100000003", "name": "示例课程26级高等数学A（一）"}
SAMPLE_WORK_HTML = '''<h2>示例课程26级高等数学A（一）</h2>
<li onclick="goTask(this);" data="https://mooc1.huuc.edu.cn/mooc-ans/mooc2/work/task?courseId=100000001&classId=100000002&cpi=100000003&workId=58370553&answerId=55746672&enc=abc123" tabindex="0" aria-label="3-5函数的极值与最值 ; 待批阅" role="link">
  <div class="right-content"><p class="overHidden2 fl">3-5函数的极值与最值</p>
  <i class="label fl">互评</i><p class="status fl">待批阅</p></div>
  <div class="time notOver" tabindex="0"><img src="x.png" />剩余1小时12分钟</div>
</li>
<li onclick="goTask(this);" data="https://mooc1.huuc.edu.cn/mooc-ans/mooc2/work/eval-list?courseId=100000001&classId=100000002&cpi=100000003&workId=58263400&answerId=55727436&enc=def456" aria-label="3.3泰勒公式 ; 待互评">
  <div class="right-content"><p class="overHidden2 fl">3.3泰勒公式</p>
  <i class="label fl">互评</i><p class="status fl">待互评</p></div>
</li>'''
SAMPLE_EXAM_HTML = '''<div class="bottomList"><ul><li>
<div onclick="viewExamAnswer('10959519','170844300')">
<div class="right-content"><p class="overHidden2 fl">第二章测试题</p>
<p class="status fl">已完成</p></div></div></li></ul></div>'''
# 实测形状（run 43448758305）：作业详情页的「作答时间」，值在 <em> 里、不带年份
SAMPLE_DETAIL_HTML = "<p>作答时间:<em>11-05 23:59</em>至<em>11-10 23:59</em></p>"
SAMPLE_EVAL_HTML = '<p class="hpInfo">互评时间：11-06 07:01 至 11-10 23:01</p>'


def parse_smoke(now: datetime | None = None) -> dict:
    """用内置样本跑一遍解析链，返回各通道产出行数。

    审计 §4.4 ②：probe 原来只测连通性。站点改版导致正则失配时 probe 全绿、
    采集静默返回空（正是 §1.1 的入口）。这里给 probe 一个「解析得出东西吗」的断言。
    """
    now = now or now_bj()
    work = parse_work_rows(SAMPLE_WORK_HTML, SAMPLE_COURSE, now)
    exam = parse_exam_rows(SAMPLE_EXAM_HTML, SAMPLE_COURSE, now)
    ws, we = detail_time_window(SAMPLE_EVAL_HTML, now, "互评时间")
    ds, de = detail_time_window(SAMPLE_DETAIL_HTML, now, "作答时间")
    return {"work": len(work), "exam": len(exam),
            "pending": sum(1 for t in work if t["state"] == "pending"),
            "eval_window": bool(ws and we), "work_window": bool(ds and de),
            "deadline": bool(work and work[0].get("deadline"))}


if __name__ == "__main__":
    run()