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
                   save_json, scrub_course_name)

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
    r"(?:截止时间|结束时间|截止|结束)[^0-9]{0,40}(\d{4}[-/年]\d{1,2}[-/月]\d{1,2}[日]?\s*\d{1,2}:\d{2})")
# 实测（2026-11-13）：work/task 详情页只有空标签「作答时间:」，值由 JS 填，HTML 里取不到发布时间。
# 所以作业的「开始时间」只能退回 first_seen（本助手首次发现时间），不是真实发布时间。
DETAIL_START_RE = re.compile(
    r"(?:开始时间|发布时间|布置时间|起始时间|开始)[^0-9]{0,40}(\d{4}[-/年]\d{1,2}[-/月]\d{1,2}[日]?(?:\s*\d{1,2}:\d{2})?)")
# 互评详情页给的是真实窗口，形如「互评时间：11-06 07:01 至 11-10 23:01」，不带年份
EVAL_WINDOW_RE = re.compile(r"互评时间[：:]\s*([^<]{4,80})")
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


def is_tip_page(html: str | None) -> bool:
    if not html:
        return True
    return "<title>提示</title>" in html or "参数传递不正确" in html or "无权限的操作" in html


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


def parse_exam_rows(html: str, course: dict, now: datetime) -> list[dict]:
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
        sm = STATUS_RE.search(chunk)
        status = clean_html_text(sm.group(1)) if sm else ""
        exam_id, relation_id = om.group(2), om.group(3)
        deadline, exact, rem_text, _over = _row_deadline(chunk, now)
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
            "url": (f"https://mooc1.huuc.edu.cn/exam-ans/mooc2/exam/exam-list"
                    f"?courseid={course['course_id']}&clazzid={course['class_id']}"
                    f"&cpi={course['cpi']}&ut=s"),
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


def detail_eval_window(html: str, now: datetime) -> tuple[datetime | None, datetime | None]:
    """互评页的真实时间窗 → (开始, 截止)。窗口一结束就不能再互评，所以结束时间即截止时间。"""
    m = EVAL_WINDOW_RE.search(html)
    if not m:
        return None, None
    parts = MD_HM_RE.findall(m.group(1))
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

    return build(parts[0]), build(parts[1])


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def run() -> dict:
    cfg = (load_json(DATA_DIR / "sources.json") or {}).get("study") or {}
    username, password = env("CX_USERNAME"), env("CX_PASSWORD")
    if not username or not password:
        log("未配置 CX_USERNAME / CX_PASSWORD，跳过学习通采集", level="WARN")
        save_json(META_PATH, {"last_attempt": now_bj().isoformat(timespec="seconds"),
                              "ok": False, "reason": "no_credentials"})
        return {"ok": False, "skipped": "no_credentials"}

    now = now_bj()
    cx = Chaoxing(cfg, username, password)
    if not cx.login():
        save_json(META_PATH, {"last_attempt": now.isoformat(timespec="seconds"),
                              "ok": False, "reason": "login_failed"})
        return {"ok": False, "reason": "login_failed"}

    courses = cx.list_courses()[:int(cfg.get("max_courses") or 40)]
    log(f"课程总数：{len(courses)}")
    if not courses:
        save_json(META_PATH, {"last_attempt": now.isoformat(timespec="seconds"),
                              "ok": False, "reason": "no_courses"})
        return {"ok": False, "reason": "no_courses"}

    all_tasks: list[dict] = []
    ok_courses, failed = 0, 0
    detail_budget = int(cfg.get("max_detail") or 8) if cfg.get("fetch_detail") else 0

    for i, c in enumerate(courses):
        tokens = cx.mint(c)
        if not tokens:
            failed += 1
            continue
        whtml = cx.work_html(c, tokens)
        if is_tip_page(whtml):
            log(f"课程 {c.get('name')} 作业列表被拒（token 或权限问题）", level="WARN")
            failed += 1
            continue
        rows = parse_work_rows(whtml, c, now)
        ehtml = cx.exam_html(c, tokens)
        if ehtml and not is_tip_page(ehtml):
            rows.extend(parse_exam_rows(ehtml, c, now))
        all_tasks.extend(rows)
        ok_courses += 1
        c["task_count"] = len(rows)
        c["pending_count"] = sum(1 for r in rows if r["state"] == "pending")
        if i < len(courses) - 1:
            time.sleep(0.3)  # 别把学校站点打疼

    log(f"扫描课程：{ok_courses}，失败：{failed}")

    # 待完成任务抓详情页：补精确截止时间 + 取开始时间。
    # 列表页只有一个倒计时 div，没有发布/开始时间，「超过一周就剔除」只能靠详情页。
    if detail_budget > 0:
        need = [t for t in all_tasks if t["state"] == "pending"][:detail_budget]
        got_start = 0
        for t in need:
            html = cx.get(t["url"])
            if not html or is_tip_page(html):
                log(f"详情页不可用（{len(html or '')} 字节）：{t['title']}", level="WARN")
                continue
            dt = detail_deadline(html)
            if dt and not t.get("deadline_exact"):
                t["deadline"] = dt.isoformat(timespec="seconds")
                t["deadline_exact"] = True
                log(f"详情页补全截止时间：{t['title']} → {t['deadline']}")
            st = detail_start(html)
            ws, we = detail_eval_window(html, now)
            if ws:
                st = ws
            if we and not t.get("deadline_exact"):
                t["deadline"] = we.isoformat(timespec="seconds")
                t["deadline_exact"] = True
                log(f"互评窗口截止：{t['title']} → {t['deadline']}")
            if st:
                t["start_time"] = st.isoformat(timespec="seconds")
                got_start += 1
            if not dt and not st and not we:
                log(f"详情页无时间字段（{len(html)} 字节）：{t['title']} "
                    f"片段={detail_time_hints(html)}")
        log(f"详情页取到开始时间：{got_start}/{len(need)} 条")

    merged = merge_tasks(load_list(TASKS_PATH), all_tasks, now)
    save_list(TASKS_PATH, merged, course_count=len(courses))
    save_list(COURSES_PATH, courses)

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

    save_json(META_PATH, {"last_attempt": now.isoformat(timespec="seconds"),
                          "last_success": now.isoformat(timespec="seconds"),
                          "ok": True, "courses": len(courses),
                          "courses_ok": ok_courses, "courses_failed": failed,
                          "tasks": len(merged), "pending": len(pending)})
    return {"ok": True, "courses": len(courses), "courses_ok": ok_courses,
            "tasks": len(merged), "pending": len(pending),
            "soon": len(soon), "overdue": len(overdue)}


if __name__ == "__main__":
    run()