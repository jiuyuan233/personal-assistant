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
import json
import re
import time
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse

import requests

from dedup import (dedupe_by_identity, load_list, merge_quiz, merge_tasks,
                    save_list, sort_by_deadline)
from utils import (DATA_DIR, clean_html_text, env, exc_brief, fmt_remaining,
                   load_json, log, make_id, now_bj, parse_datetime,
                   parse_remaining, safe_text, save_json, scrub_course_name,
                   scrub_task_title, strip_personal_fields)

STUDY_DIR = DATA_DIR / "study"
TASKS_PATH = STUDY_DIR / "tasks.json"
COURSES_PATH = STUDY_DIR / "courses.json"
META_PATH = STUDY_DIR / "meta.json"

# UA 用用户真实浏览器指纹（Chrome/157，用户 2026-11-15 提供并要求）——降低反爬识别面
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36")
AES_KEY = b"u2oh6Vu^HWe4_AES"

PENDING = {"待做", "未做", "待提交", "未提交", "未交", "待完成", "进行中", "待互评",
           "待考试", "待作答", "待批阅互评", "已退回"}
SUBMITTED = {"待批阅", "已提交", "批阅中", "待批改", "批改中"}
DONE = {"已完成", "已互评", "已批阅", "已结束", "已交", "已评分"}

MIDDLE_RE = re.compile(r"stucoursemiddle\?([^\"'\s>]+)")
# v1.7.0：课件/文件类标题（.pptx/.pdf 等后缀）不是作业——采集层与存量清理共用同一形状
quiz_browser_FILE_RE = re.compile(
    r"\.(pptx?|docx?|pdf|zip|rar|7z|xlsx?|csv|txt|mp[34]|flv|mkv|avi|png|jpe?g|gif)\s*$", re.I)
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
# v1.5.16 用户报「考试没收录」：待考考试的行是 goTest(...)，与已完成的 viewExamAnswer
# 完全不同（2026-11-15 studydiag run 43680922922 实测 dump，数字打码形状）：
#   onclick="goTest('DDDDDDDDD',DDDDDDDD,DDDDDDDDD,'DDDD-DD-DD DD:DD:DD.D',DDDDDDDDD,
#                   false,'DeeDDDddcDDDcDcDDDbDcDcbDDDDdDed', 'D');"
# 参数：examId（带引号）、两个无引号数字、**开始时间字符串（自带年份+毫秒）**、
# 一个数字、布尔、一个混合字母数字的考试码、一个标志位。注意 D 打码只掩数字不掩字母。
GOTEST_RE = re.compile(
    r"goTest\('(\d+)',\s*(\d+),\s*(\d+),\s*'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(?:\.\d+)?',"
    r"\s*(\d+),\s*(?:true|false),\s*'([A-Za-z0-9]+)',\s*'(\d+)'\)")
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
    # v1.6.5→v1.6.6（用户报 bug 后澄清定稿）：「未开始」单独判 upcoming，
    # 与「未完成」区分开——**仍在清单里显示**（标「未开始」、不计待完成数、
    # 不进 urgent6），老师开放作答后学习通状态会变（待做/进行中…），
    # 下一轮采集自动回到普通待完成。
    # 实测样本：建环概论三条「未开始」新作业（无截止时间）曾被当成待完成。
    if s == "未开始":
        return "upcoming"
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
        # v2.9：学生 cpi 全课程共用（courses.json 无此字段）——work/list
        # 对 cpi=None 返回降级 HTML（data 属性简版无 answerId）→ 飞书链接 403
        self.cpi = str(cfg.get("xxt_cpi") or "577967959")
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
            # v2.6：huuc fanyalogin 的 cookie 只种 huuc 域——type=77 的
            # load-data API 在 chaoxing.com 域，需要同账号在该域再登录一次
            self._login_chaoxing_domain()
            return True
        # 只打 msg，绝不回显 uname/password
        log(f"学习通登录失败：{safe_text(data.get('msg') or data, 160)}", level="ERROR")
        return False

    def _login_chaoxing_domain(self) -> None:
        """同账号密码在 passport2.chaoxing.com 登录（种 chaoxing.com 域 cookie）。

        type=77 的 load-data API 在 chaoxing.com 域——huuc cookie 不覆盖。
        失败只 WARN（77 类将进未知清单，不影响主采集）。"""
        try:
            r = self.s.post(
                "https://passport2.chaoxing.com/fanyalogin",
                data={
                    "fid": self.cfg.get("fid", "12"),
                    "uname": aes_encrypt(self.username),
                    "password": aes_encrypt(self.password),
                    "t": "true",
                    "refer": "https://i.chaoxing.com",
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
            if data.get("status") is True:
                log("chaoxing.com 域登录成功（77 类状态 API 可用）")
            else:
                log(f"chaoxing.com 域登录失败：{safe_text(data.get('msg') or data, 120)}",
                    level="WARN")
        except Exception as e:
            log(f"chaoxing.com 域登录异常：{safe_text(e, 160)}", level="WARN")

    def ensure_chaoxing_login(self) -> bool:
        """确认 chaoxing.com 域登录态（type=77 的 load-data API 在该域）。

        fanyalogin 统一登录会种全家族 cookie（含 .chaoxing.com）——
        访问 i.chaoxing.com 若直达个人空间即确认；跳登录页则 77 类状态
        将进未知清单（Actions runner 无法人工补登）。"""
        try:
            r = self.s.get("https://i.chaoxing.com/", timeout=40,
                           allow_redirects=True)
            if r.url and "passport2" not in r.url:
                log("chaoxing.com 域登录态确认（SSO 直达个人空间）")
                return True
            log("chaoxing.com 未登录（跳转登录页）——77 类状态将进未知清单",
                level="WARN")
            return False
        except Exception as e:
            log(f"chaoxing SSO 检测异常：{safe_text(e, 160)}", level="WARN")
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
            "cpi": course.get("cpi") or self.cpi,
            "ismooc2": course.get("ismooc2", "1"),
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
                                "cpi": course.get("cpi") or self.cpi, "ut": "s",
                                "stuenc": tokens["enc"], "enc": tokens["workEnc"]})

    def exam_html(self, course: dict, tokens: dict) -> str | None:
        if not tokens.get("examEnc"):
            return None
        url = self.cfg.get("exam_list",
                           "https://mooc1.huuc.edu.cn/exam-ans/mooc2/exam/exam-list")
        # 考试模块用小写参数名，和作业模块不一样
        return self.get(url, params={"courseid": course["course_id"], "clazzid": course["class_id"],
                                     "cpi": course.get("cpi") or self.cpi, "ut": "s",
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
        # v2.8：data 属性里的 & 写作 &amp;——解码成 & 才能打开
        # （否则飞书通知的链接 403/缺参数）
        import html as _html
        url = _html.unescape(um.group(1))
        # v2.9：未交任务 data 属性可能缺 cpi（无 answerId 可补）——
        # 补上课程 cpi，task 链接才打得开
        if "cpi=" not in url:
            url += f"&cpi={course.get('cpi')}"
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
        # v1.7.0（用户 2026-11-16 定稿）：待批阅 = 已交等老师批，**不需要用户动手**，
        # 不算待办（原来互评升级规则已按用户澄清移除——他明确说"待批改不需要
        # 我操作"；漏互评的风险由「待互评」状态单独扛，交完互评后状态就是待互评）。
        # 需要动手的：待完成/未交/进行中/待互评/已退回（退回要重做）/待考试。
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
    """考试列表解析，两种行形状（diag 实测）：

    - 已完成：onclick="viewExamAnswer('examId','relationId')"，两参数带引号纯数字
    - **待考**：onclick="goTest('examId',…,'开始时间',…,false,'考试码',…)"（v1.5.16），
      goTest 第 4 参是**自带年份的精确截止时间**（v1.5.16 实测修正：参数顺序
      (courseId, examId, classId, '结束时间', cpi, …)，deadline 直接用它并标 exact）；
      深链用 examnotes 页（courseId/classId/examId，均考试级参数，无每生一份的 enc）。
    """
    out = []
    for m in re.finditer(r"<li\b[^>]*>(.*?)</li>", html, re.S):
        chunk = m.group(1)
        om = EXAM_ONCLICK_RE.search(chunk)
        gm = GOTEST_RE.search(chunk)
        tm = TITLE_RE.search(chunk)
        if not tm or not (om or gm):
            continue
        title = scrub_task_title(clean_html_text(tm.group(1)))
        if not title:
            continue
        sm = STATUS_RE.search(chunk)
        status = clean_html_text(sm.group(1)) if sm else ""
        deadline, exact, rem_text, _over = _row_deadline(chunk, now)
        if gm:
            # v1.5.16 修正：实测（run 43681528635 落盘对比）goTest 参数顺序是
            #   (courseId, examId, classId, '结束时间', cpi, false, 考试码, flag)
            # —— 参数 4 是页面给的**精确截止时间**（秒级，比列表倒计时的分钟级推算准），
            # 直接覆盖倒计时估算并标 deadline_exact=True；参数里没有开始时间。
            exam_id, relation_id = gm.group(2), ""
            end_dt = parse_datetime(gm.group(4))
            if end_dt:
                deadline = end_dt.isoformat(timespec="seconds")
                exact = True
            # examnotes 页深链（用户实测登录态下可进）；三个参数都是考试/课程级，
            # 不含每生一份的 enc——cpi 虽在用户原链接里出现，但按 §7.2 白名单口径不落盘
            url = (f"https://mooc1.huuc.edu.cn/exam-ans/exam/test/examcode/examnotes"
                   f"?courseId={course['course_id']}&classId={course['class_id']}"
                   f"&examId={exam_id}")
        else:
            exam_id, relation_id = om.group(2), om.group(3)
            # §3.7 已完成考试的列表深链：带现铸 token（enc 每次变，落盘前被 public_url 剥掉）
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
    """写 meta.json，**保留上一次的 last_success 与其他既有键**。

    审计 §3.8：原来三个失败分支都用全新 dict 覆盖，把 last_success 抹掉了，
    事后无法判断数据有多旧。失败时只更新 last_attempt 与失败原因。
    v1.5.22 修正：old 的其他键（如 quiz_cursor 轮换记账）也必须保留——
    之前的写法会把它们全部抹掉。
    v1.6.4 修正：成功轮（不带 reason 的调用）要清掉上一轮的 `reason` 残留，
    否则「失败→成功」后 meta 会同时显示 ok=True 与 reason=no_courses，
    误导看板和读文档的人（2026-11-16 17:34 真机实证过这个混淆）。
    """
    old = load_json(META_PATH) or {}
    if "reason" not in kw:
        old.pop("reason", None)
    meta = {**{k: v for k, v in old.items() if k != "last_attempt"},
            "last_attempt": now.isoformat(timespec="seconds"), **kw}
    save_json(META_PATH, meta)
    return meta


def urgent_alerts(soon: list[dict], prev_sig: str | None) -> tuple[str, list[str]]:
    """算出本轮紧急告警：返回 (签名, 要打的日志行)。纯函数，selftest 直接喂数据。

    [urgent] 是飞书发状态卡的唯一「重要」触发器，所以规则必须又准又省：
    - 只认 24h 内截止（调用方已筛好）。逾期任务做不了、且长期常驻（实测 2 条），
      拿它当紧急会让每小时一轮的 study 永远发卡 —— 正是用户抱怨的噪音。
    - 同一批任务不重复告警：签名存 meta.json 跨轮比对。
      签名**只用任务 ID，不含 deadline**：deadline_exact=False 的截止时间是
      「抓取时刻 + 列表页倒计时」每轮重算的，带进去签名每小时都变，去重就废了。
    """
    sig = ",".join(sorted(t["id"] for t in soon))
    if not soon:
        return sig, []
    if sig == prev_sig:
        return sig, [f"[urgent-same] 24h 内截止的 {len(soon)} 条与上轮相同，不重复告警"]
    return sig, [f"[urgent] {t['course']} · {t['title']} · "
                 f"{fmt_remaining(parse_datetime(t['deadline']))}" for t in soon[:5]]


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

    # v2.0 随堂采集（2026-11-26 本地 14 门课验证版移植）：块解析 + 三类
    # 分类（quiz/classroom/material）+ 状态补抓（activeRowClick 跳详情 /
    # 分组任务 API）。classroom/material 只记日志不进待办（用户定论：
    # 讨论不算作业、课件不是作业）。任何失败都不影响主采集。
    _res_by_course: dict = {}  # (course_id, class_id) → collect_course 结果
    quiz_new: list[dict] = []
    quiz_covered: set = set()
    xxt_cpi = str(cfg.get("xxt_cpi") or "577967959")
    # v2.7：旧 quiz 列表提前拆出（合并用——骨架轮 API 只回部分活动，
    # 并集合并需要旧清单做底册）
    old_all = load_list(TASKS_PATH)
    old_quiz = [t for t in old_all if t.get("kind") == "quiz"]
    if cfg.get("quiz_browser", True):
        try:
            import quiz_browser as qb
            meta_q = load_json(META_PATH) or {}
            cursor = int(meta_q.get("quiz_cursor") or 0) % max(1, len(courses))
            per_round = max(1, int(cfg.get("quiz_per_round") or 4))
            picks = courses[cursor:cursor + per_round] or courses[:1]
            log(f"[quiz] 本轮随堂采集 {len(picks)} 门（cursor={cursor}）："
                f"{', '.join((c.get('name') or '?')[:12] for c in picks)}")
            # v2.6：确认 chaoxing.com 域登录态（type=77 的 load-data API 在该域）
            cx.ensure_chaoxing_login()
            q_cookies = qb.cookies_for_browser(cx.s)
            for c in picks:
                time.sleep(2)
                mid_url = (f"https://mooc1.huuc.edu.cn/visit/stucoursemiddle"
                           f"?courseid={c['course_id']}&clazzid={c['class_id']}"
                           f"&cpi={c.get('cpi') or xxt_cpi}"
                           f"&ismooc2={c.get('ismooc2', '1')}")
                res = qb.collect_course(c, mid_url, q_cookies, UA, now,
                                        cpi=xxt_cpi)
                _res_by_course[(str(c.get("course_id")),
                                str(c.get("class_id")))] = res
                cname = c.get("name") or "?"
                if not res.get("ok"):
                    log(f"[quiz] {cname} 随堂采集失败：{res.get('error', '')[:120]}",
                        level="WARN")
                    continue
                # v2.4c：只收随堂作业（kind=quiz）——浏览器块的 work/exam
                # 与 requests 主链双源重复（同作业两条入库，用户报的重复根因）；
                # work/exam 以 requests 主链为准，浏览器块仅记 diag
                # v2.7：**合并而非替换**——本轮新抓的条目按 (course_id,
                # 归一标题) 覆盖旧条目，旧条目中不在本轮清单的保留
                #（activelist 骨架轮 API 只回当前 tab 的部分活动——
                # 本地实证：思修全量 8 条、骨架轮只回 3 条）
                import re as _re
                _key = lambda t: (str(t.get("course_id")),
                                  _re.sub(r"\s+", "", t.get("title") or ""))
                _new_by_key = {_key(t): t for t in
                               (res.get("tasks") or []) if t.get("kind") == "quiz"}
                # 并集合并限本轮 picks 课程（避免未轮到课程的任务混进
                # 本轮日志/清单——数据 course_id 各自正确，但日志按课程
                # 打印会错乱误导）
                _pick_ids = {str(c.get("course_id")) for c in picks}
                # v2.7：课堂类（type 数据）旧条目在本轮覆盖时清除——
                # type=5 讨论/11 选人/14 问卷等不是作业（用户定论），
                # 不允许以 unknown 残留。type 映射来自 res（collect_course
                # 返回的 api_type_map，key=(归一标题, aid)）
                _tmap = res.get("api_type_map") or {}
                _old_pick = []
                for t in old_quiz:
                    if str(t.get("course_id")) not in _pick_ids:
                        continue
                    nt = _re.sub(r"\s+", "", t.get("title") or "")
                    at = _tmap.get(f"{nt}|{t.get('_aid') or ''}") or                         _tmap.get(f"{nt}|")
                    if at is not None and not qb._quiz_type(at):
                        continue  # 课堂类旧条目清除
                    _old_pick.append(t)
                _old_keys = {_key(t) for t in _old_pick}
                merged_q: list[dict] = []
                for t in _old_pick:
                    nk = _key(t)
                    if nk in _new_by_key:
                        merged_q.append(_new_by_key[nk])  # 新值覆盖
                    else:
                        merged_q.append(t)  # 本轮清单没有 → 保留旧值
                for nk, t in _new_by_key.items():
                    if nk not in _old_keys:
                        merged_q.append(t)  # 新出现的活动
                tasks_q = merged_q
                diag = "; ".join(str(d) for d in res.get("diag") or [])
                # 「有任一解析结果」才算覆盖这门课；全 0 保留旧数据（旧数据保命）。
                # v2.8 例外：activelist 全部是课堂类（如体育课 6 条通知）→
                # 也是权威采集（这门课确实没有作业），清掉旧条目（否则
                # 通知类以 unknown+逾期 永久残留）
                # activelist 全部是课堂类（如体育课 6 条通知）→ 也是权威
                # 采集（这门课确实没有作业），清掉旧条目
                _all_classroom = (bool(_tmap)
                                  and all(not qb._quiz_type(at)
                                          for at in _tmap.values()))
                if tasks_q or res.get("work_blocks") or res.get("exam_blocks")                         or _all_classroom:
                    quiz_covered.add((c["course_id"], c["class_id"]))
                    quiz_new.extend(tasks_q)
                    log(f"[quiz] {cname} 随堂作业 {len(tasks_q)} 条"
                        f"（diag {diag}）")
                    for t in tasks_q[:10]:
                        log(f"[quiz]   · {t['title']}｜{t['status'] or '无状态'}｜"
                            f"{t.get('deadline') or '无截止'}")
                else:
                    log(f"[quiz] {cname} 0 候选（diag {diag}），保留旧数据",
                        level="WARN")
            new_cursor = (cursor + len(picks)) % max(1, len(courses))
            meta_now = load_json(META_PATH) or {}
            meta_now["quiz_cursor"] = new_cursor
            save_json(META_PATH, meta_now)
            log(f"[quiz] cursor → {new_cursor}（本轮随堂新增 {len(quiz_new)} 条）")
        except Exception as e:
            log(f"[quiz] 随堂采集异常（不影响主采集）：{exc_brief(e)}", level="WARN")

    # 合并分两路：work/eval/exam 全量重抓走 merge_tasks（新列表即权威）；
    # quiz 轮换制走 merge_quiz（覆盖到的课新值权威、没覆盖的保旧、
    # 整轮失败全保旧——绝不让轮换把已收录的随堂任务清掉）。
    old_all = load_list(TASKS_PATH)
    old_quiz = [t for t in old_all if t.get("kind") == "quiz"]
    old_main = [t for t in old_all if t.get("kind") != "quiz"]
    merged = merge_tasks(old_main, all_tasks, now)
    merged = sort_by_deadline(merged + merge_quiz(old_quiz, quiz_new, quiz_covered, now))
    # v2.9c：浏览器块的 work/exam 链接覆盖 requests 简版——
    # requests 抓的 work/list 源码里 data 属性是简版（JS 动态补全
    # answerId/enc），飞书链接 403；浏览器 DOM 里 data 属性完整。
    # 按 (course_id, class_id, 归一标题前20) 匹配覆盖。
    _cov_n = 0
    for (rcid, rclz), res in _res_by_course.items():
        _bl_n = 0
        for bl in (res.get("work_blocks") or []) + (res.get("exam_blocks") or []):
            _bl_n += 1
            try:
                _obj = json.loads(bl)
                _lines = (_obj.get("text") or "").splitlines()
                _nt = _re.sub(r"\s+", "", _lines[0] if _lines else "")
                _href = _obj.get("href") or ""
            except Exception as e:
                log(f"[quiz] 链接覆盖解析异常 {safe_text(e, 60)}", level="WARN")
                continue
            if not _href or "answerId" not in _href:
                continue
            _hit = False
            for t in merged:
                if (t.get("kind") in ("work", "exam")
                        and str(t.get("course_id")) == rcid
                        and str(t.get("class_id")) == rclz):
                    _tt = _re.sub(r"\s+", "", t.get("title") or "")
                    # 双向包含（块首行可能带题号前缀/后缀）
                    if _nt and (_nt in _tt or _tt in _nt):
                        t["url"] = _href
                        _cov_n += 1
                        _hit = True
                        break
            if not _hit:
                log(f"[quiz] 链接覆盖未命中: {safe_text(_nt, 30)}", level="WARN")
        log(f"[quiz] {rcid[:10]} 链接覆盖: 块 {_bl_n} 命中 {_cov_n}（累计）")
    log(f"[quiz] 链接覆盖总计 {_cov_n} 条")
    log(f"[quiz] _res_by_course 课程数 {len(_res_by_course)}")
    # v2.4（用户报「一堆重复+无状态作业」）：同作业不同 id 的历史条目
    # 分桶去重——unknown 旧版本在有状态版本存在时丢弃；不同状态的桶
    # 各自保留（「人造空间」同名已完成+未交两条都得在）
    merged, _dedup_removed = dedupe_by_identity(merged)
    if _dedup_removed:
        log(f"同作业去重：清掉历史堆积 {_dedup_removed} 条")
    # v1.7.0（用户报的 bug）：老师发的课件（.pptx/.pdf 等文件后缀标题）不是作业。
    # 采集层已过滤新条目；这一步清掉历史入库的存量（一次性迁移，下轮采集起全链路干净）。
    _before_files = len(merged)
    merged = [t for t in merged
              if not quiz_browser_FILE_RE.search((t.get("title") or "").strip())]
    if len(merged) < _before_files:
        log(f"清理课件类条目（.pptx 等文件后缀，非作业）：{_before_files - len(merged)} 条")
    # v2.0 统计页分数补充（本地 14 门课验证版）：stat2 每门课独立 URL，
    # 每轮只轮换 stat_per_round 门（防频率风控）；**cpi 必须显式**——
    # stat2 对 cpi 缺失返回 30 字符空页（本地实证，曾误判为「限流」）。
    # 只补分数不覆盖状态（作业状态已由 work/exam tab 全量覆盖），按
    # 课程+标题归一匹配，score 为空才填。任何失败不影响主采集。
    stat_per = max(0, int(cfg.get("stat_per_round") or 2))
    if stat_per and cfg.get("quiz_browser", True):
        try:
            import quiz_browser as qb
            meta_s = load_json(META_PATH) or {}
            scur = int(meta_s.get("stat_cursor") or 0) % max(1, len(courses))
            spicks = courses[scur:scur + stat_per] or courses[:1]
            q_cookies = qb.cookies_for_browser(cx.s)
            _n_score = 0
            for c in spicks:
                rows_s = qb.fetch_work_stat(c, q_cookies, UA, xxt_cpi)
                log(f"[stat] {c.get('name')}: 统计页 {len(rows_s)} 条")
                for r in rows_s:
                    if not r.get("score"):
                        continue
                    rk = re.sub(r"\s+", "", r["title"])[:20]
                    for t in merged:
                        if t.get("kind") not in ("work", "exam") or t.get("score"):
                            continue
                        if (t.get("course_id") == str(c.get("course_id"))
                                and re.sub(r"\s+", "", t.get("title") or "")[:20] == rk):
                            t["score"] = r["score"]
                            _n_score += 1
                            break
                time.sleep(2)
            nscur = (scur + len(spicks)) % max(1, len(courses))
            meta_s["stat_cursor"] = nscur
            save_json(META_PATH, meta_s)
            log(f"[stat] 本轮分数回填 {_n_score} 条（cursor → {nscur}）")
        except Exception as e:
            log(f"[stat] 统计页采集异常（不影响主采集）：{exc_brief(e)}", level="WARN")
    # 审计 §2.5 / §7.2：data/ 是公开仓库的一部分，不是本地缓存。
    # cpi / answerId / enc 每生一份，落盘前必须抹掉；完整深链只在本轮内存里用于抓详情页。
    link_mode = cfg.get("public_links") or "course"
    merged = [strip_personal_fields(t, link_mode) for t in merged]
    save_list(TASKS_PATH, merged, course_count=len(courses))
    save_list(COURSES_PATH, [{k: v for k, v in c.items() if k != "cpi"} for c in courses])

    pending = [t for t in merged if t["state"] == "pending"]
    far = now + timedelta(days=3650)
    overdue = [t for t in pending if (parse_datetime(t["deadline"]) or far) < now]
    soon = sort_by_deadline([t for t in pending
                             if timedelta(0) <= (parse_datetime(t["deadline"]) or far) - now
                             <= timedelta(hours=24)])
    log(f"待完成：{len(pending)}")
    log(f"24小时内截止：{len(soon)}")
    log(f"已逾期：{len(overdue)}")
    # 降噪规则见 urgent_alerts()：只认 24h 内截止，且同一批不重复告警。
    sig, urgent_lines = urgent_alerts(soon, (load_json(META_PATH) or {}).get("urgent_sig"))
    for line in urgent_lines:
        log(line)

    # §1.1 ok 不再无条件为 True：有失败课程 / 可疑清空 / 课程数偏离 / 截止未知 → partial
    ok = not failed and not anomalies
    _save_meta(now, ok=ok, status="ok" if ok else "partial",
               last_success=now.isoformat(timespec="seconds"),
               courses=len(courses), courses_ok=ok_courses, courses_failed=failed,
               tasks=len(merged), pending=len(pending),
               unknown_deadline=len(still_unknown), anomalies=anomalies[:12],
               urgent_sig=sig, urgent_count=len(soon))
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
        # v1.5.21 用户报「随堂作业没收录」：老师把作业/测验挂在**课程章节页**里，
        # 不在作业/考试 tab。抓课程学习页 dump 全部入口链接形状（数字打码），
        # 找到随堂作业的列表入口——不许猜 URL。
        stu_url = (f"{cfg.get('stu_page', 'https://mooc2-ans.huuc.edu.cn/mooc2-ans/mycourse/stu')}"
                   f"?courseid={c['course_id']}&clazzid={c['class_id']}&cpi={c['cpi']}"
                   f"&enc={tokens.get('enc', '')}&pageHeader=0&v=2&hideHead=0")
        shtml = cx.get(stu_url)
        if shtml and not is_tip_page(shtml):
            log(f"[diag] {c.get('name')} 课程页 {len(shtml)} 字节 | "
                f"knowledge×{shtml.count('knowledge')} chapter×{shtml.count('chapter')} "
                f"随堂×{shtml.count('随堂')}")
        # v1.5.21 续：反爬实测——连续快速访问 14 门课，第 9 门起触发
        # antispiderShowVerify（run 43721882012：202+0B，后续详情页全部 0 字节）。
        # **诊断/采集都必须放缓**。这里只对高等数学做章节探测（用户点名的缺失样本），
        # 且 302 链每跳间隔 2 秒、课程间 3 秒——宁可慢，不能触发风控。
        if "高等数学" in (c.get("name") or ""):
            time.sleep(3)
            try:
                mid_url = (f"https://mooc1.huuc.edu.cn/visit/stucoursemiddle"
                           f"?courseid={c['course_id']}&clazzid={c['class_id']}&cpi={c['cpi']}"
                           f"&ismooc2={c.get('ismooc2', '1')}")
                cur = mid_url
                hops: list[str] = []
                page = None
                for _ in range(5):
                    resp_h = cx.s.get(cur, timeout=30, allow_redirects=False)
                    time.sleep(2)
                    if resp_h.status_code in (301, 302, 303, 307, 308):
                        from urllib.parse import urljoin as _uj
                        cur = _uj(cur, resp_h.headers.get("Location") or "")
                        hops.append(_shape(cur)[:180])
                        continue
                    page = resp_h.text if resp_h.status_code == 200 else None
                    hops.append(f"final HTTP {resp_h.status_code} ({len(page or '')}B)")
                    break
                log(f"[diag] 高数 302 链：{' → '.join(hops[:3])}")
                if page:
                    js_n = len(re.findall(r'src="([^"]*\.js)', page))
                    log(f"[diag]   真实课程页 {len(page)}B | knowledge×{page.count('knowledge')} "
                        f"随堂×{page.count('随堂')} js×{js_n}")
                    seen2: set[str] = set()
                    for m in re.finditer(r"""(?:src|href|data|url)=["']([^"']*(?:knowledge|worklist|quiz|datalist|chapter|module|\.js)[^"']*)["']""", page):
                        u = _shape(m.group(1))[:200]
                        if u not in seen2:
                            seen2.add(u)
                            log(f"[diag]   高数入口：{u}")
                    # v1.5.22 收官：Playwright 打开 stuActiveList（Vue 渲染）拿任务行——
                    # v1.5.21 核心：tab href /mooc2-ans/coursedata/stu-datalist 无 query——
                    # 参数要么沿用课程页 query，要么在 contentLoader.js 里拼
                    time.sleep(2)
                    from urllib.parse import urlparse as _up2
                    dl_url2 = (f"https://mooc2-ans.huuc.edu.cn/mooc2-ans/coursedata/stu-datalist"
                               f"?{_up2(cur).query}")
                    resp_dl2 = cx.get(dl_url2)
                    size2 = len(resp_dl2 or "")
                    log(f"[diag]   datalist+课程页同query → {size2}B")
                    if size2 > 1000:
                        log(f"[diag]   随堂×{resp_dl2.count('随堂')} "
                            f"知识总结×{resp_dl2.count('知识总结')} "
                            f"test×{resp_dl2.count('test')}")
                        log(f"[diag]   前 500B：{_shape(clean_html_text(resp_dl2[:500]))[:400]}")
                        # v1.5.21：逐行 dump 含关键词的内容行（打码）+ 知识总结的 HTML 片段
                        hits_d = 0
                        for m in re.finditer(r"[^\n<>]{0,30}(?:随堂|知识总结|test)[^\n<]{0,90}", resp_dl2):
                            s = _shape(clean_html_text(m.group(0))).strip()
                            if len(s) > 12:
                                log(f"[diag]   内容行：{s}")
                            hits_d += 1
                            if hits_d >= 15:
                                break
                        for m in re.finditer(r".{0,260}知识总结.{0,340}", resp_dl2):
                            log(f"[diag]   知识总结 HTML 片段：{_shape(m.group(0))[:500]}")
                            break
                        # v1.5.21 最后一层：datalist 页的任务数据由前端 AJAX 再请求——
                        # dump 它的 AJAX 端点与 data-* 属性（打码）
                        hits_a = 0
                        for m in re.finditer(r"""(?:url|action)\s*[:=]\s*["']([^"']*(?:\.ac\?|\.ac"|/work/|/exam/|/api/|/mooc/|List|list)[^"']{0,100})["']""", resp_dl2):
                            u = _shape(m.group(1))[:180]
                            if u not in seen2:
                                seen2.add(u)
                                log(f"[diag]   datalist AJAX 端点：{u}")
                            hits_a += 1
                            if hits_a >= 10:
                                break
                        for m in re.finditer(r"""data-(?:url|api|src|page)=["']([^"']{10,150})["']""", resp_dl2):
                            log(f"[diag]   data 属性：{_shape(m.group(1))[:160]}")
                        hits_d = 0
                        for m in re.finditer(r"<tr\b.*?</tr>", resp_dl2, re.S):
                            tr = _shape(_mask_title(re.sub(r"\s+", " ", m.group(0)))[:360])
                            if "test" in tr or "随堂" in tr or "知识总结" in tr:
                                log(f"[diag]   tr 行：{tr}")
                                hits_d += 1
                                if hits_d >= 8:
                                    break
                    for jsname in ("contentLoader.js", "course-stu.js"):
                        js_m = re.search(r'src="([^"]*' + jsname + r'[^"]*)"', page)
                        if not js_m:
                            continue
                        js_u = _uj(cur, js_m.group(1))
                        time.sleep(2)
                        jt = cx.get(js_u)
                        if not jt:
                            continue
                        log(f"[diag]   {jsname} {len(jt)}B | datalist×{jt.count('datalist')}")
                        hits_js = 0
                        for m in re.finditer(r".{120}datalist.{240}", jt):
                            log(f"[diag]   {jsname} datalist 片段：{_shape(m.group(0))[:300]}")
                            hits_js += 1
                            if hits_js >= 3:
                                break
            except Exception as e:
                log(f"[diag] 高数 302 链失败：{type(e).__name__}", level="WARN")
            # v1.5.22 突破：随堂活动列表页 stuActiveList（mobilelearn 域）——
            # iframe 结构实测（v1.5.22 诊断）：课程页 4 个 frame，其一即
            # stuActiveList?courseid&clazzid&cpi&ut&s&t&stuenc——全部可构造（requests 可抓）
            time.sleep(3)
            try:
                from urllib.parse import urlparse as _up2
                _enc302 = ""
                from urllib.parse import parse_qs as _pq
                try:
                    _enc302 = (_pq(_up2(cur).query).get("enc") or [""])[0]
                except Exception:
                    _enc302 = ""
                import time as _t
                active_url = (f"https://mobilelearn.huuc.edu.cn/page/active/stuActiveList"
                              f"?courseid={c['course_id']}&clazzid={c['class_id']}"
                              f"&cpi={c['cpi']}&ut=s&t={int(_t.time() * 1000)}"
                              f"&stuenc={_enc302}")
                resp_act = cx.get(active_url)
                size_a = len(resp_act or "")
                log(f"[diag]   stuActiveList → {size_a}B")
                if resp_act and size_a > 1000:
                    log(f"[diag]   随堂×{resp_act.count('随堂')} "
                        f"知识总结×{resp_act.count('知识总结')} "
                        f"test×{resp_act.count('test')} active×{resp_act.count('active')}")
                    hits_a = 0
                    for m in re.finditer(r"[^\n<>]{0,30}(?:随堂|知识总结|测试|测验)[^\n<]{0,90}", resp_act):
                        s = _shape(clean_html_text(m.group(0))).strip()
                        if len(s) > 12:
                            log(f"[diag]   内容行：{s}")
                        hits_a += 1
                        if hits_a >= 12:
                            break
                    # v1.5.22 核心：随堂练习行的完整 HTML 结构——按 aria-label 分段取上下文
                    _segs = resp_act.split('aria-label="随堂练习"')
                    for _si, _seg in enumerate(_segs[1:3], 1):
                        log(f"[diag]   随堂练习段 {_si}（前 500B）：{_shape(_seg[:500])}")
                        log(f"[diag]   随堂练习段 {_si}（后 300B）：{_shape(_seg[-300:])}")
                    for m in re.finditer(r"onclick=\"(\w+)\(", resp_act):
                        log(f"[diag]   onclick 函数：{m.group(1)}")
                        break
            except Exception as e:
                log(f"[diag] stuActiveList 失败：{type(e).__name__}", level="WARN")
            time.sleep(3)
            seen: set[str] = set()
            for m in re.finditer(r"""(?:href|src|data|url)=["']([^"']*(?:knowledge|worklist|examlist|quiz|chapter|module|datalist)[^"']*)["']""", shtml):
                u = _shape(m.group(1))[:180]
                if u not in seen:
                    seen.add(u)
                    log(f"[diag]   入口：{u}")
        else:
            log(f"[diag] {c.get('name')} 课程页不可用（{len(shtml or '')} 字节）")
        ehtml = cx.exam_html(c, tokens)
        if ehtml and not is_tip_page(ehtml):
            for r in parse_exam_rows(ehtml, c, now):
                if r["state"] == "pending" and not r.get("deadline"):
                    problems.append({"course": c.get("name"), "chunk": None, "row": r})
            # v1.5.16 用户报「考试没收录」：逐行对比 EXAM_ONCLICK_RE 匹配情况——
            # 没匹配上的行（待考/考试码类考试）原样 dump（数字打码、标题掩码），不许猜形状
            li_marks = list(re.finditer(r"<li\b[^>]*>.*?</li>", ehtml, re.S | re.I))
            matched = sum(1 for li in li_marks if EXAM_ONCLICK_RE.search(li.group(0)))
            log(f"[diag] {c.get('name')} 考试列表：{len(li_marks)} 行，onclick 匹配 {matched} 行")
            for li in li_marks:
                chunk = li.group(0)
                if EXAM_ONCLICK_RE.search(chunk):
                    continue
                flat = re.sub(r"\s+", " ", chunk)
                if len(flat) < 40:
                    continue
                log(f"[diag] 考试行无 onclick 匹配（{len(flat)} 字节）：")
                log(f"[diag]     {_shape(_mask_title(flat))[:600]}")
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
<p class="status fl">已完成</p></div></div></li><li>
<div style="float: left; width: 90%;" tabindex="0" role="option" onclick="goTest('219232606', 11087147, 153566712, '2026-11-15 14:00:00.0', 577967959, false, 'a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6', '1');">
<div class="tag icon-exam icon-exam"></div>
<div class="right-content"><p class="overHidden2 fl">期末考试</p>
<div class="clear"></div><p class="status fl">待做</p></div></div>
<div class="time notOver"><img src="x.png" />剩余5小时30分钟</div></li></ul></div>'''
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