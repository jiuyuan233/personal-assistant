"""通用工具：北京时间、倒计时解析、ID、JSON 读写、日志。

复用自 wool-gathering/scripts/utils.py，去掉活动专属逻辑，
新增学习通「剩余X天X小时X分钟」的解析与截止时间换算。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    from zoneinfo import ZoneInfo
    BJ_TZ = ZoneInfo("Asia/Shanghai")
except Exception:
    # Windows 本地缺 tzdata 时走这里。中国自 1991 年起无夏令时，固定偏移等价。
    BJ_TZ = timezone(timedelta(hours=8))


REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
DOCS_DIR = REPO_ROOT / "docs"
DOCS_DATA_DIR = DOCS_DIR / "data"
ASSETS_DIR = REPO_ROOT / "assets"

# Actions 的 /tmp 日志，飞书通知从这里读摘要
TASK_LOG = Path(os.environ.get("TASK_LOG_PATH", "/tmp/task-output.log"))

# Windows 控制台默认 GBK，日志里的 ✅/⚠️ 会让 print 抛 UnicodeEncodeError，
# 整个模式崩在第一条日志上（本地实测过）。Actions 是 UTF-8，这里等于无操作。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def log(msg: str, *, level: str = "INFO") -> None:
    ts = now_bj().strftime("%Y-%m-%d %H:%M:%S")
    line = redact(f"[{ts}] [{level}] {msg}")
    print(line, flush=True)
    try:
        TASK_LOG.parent.mkdir(parents=True, exist_ok=True)
        with TASK_LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def now_bj() -> datetime:
    return datetime.now(BJ_TZ)


def today_bj() -> date:
    return now_bj().date()


def parse_date(s: str | None) -> date | None:
    if not s:
        return None
    s = str(s).strip()
    for p in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y年%m月%d日",
              "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s, p).date()
        except ValueError:
            continue
    m = re.search(r"(\d{4})\D(\d{1,2})\D(\d{1,2})", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


def parse_datetime(s: str | None) -> datetime | None:
    """解析带时区的 ISO 字符串；无时区的一律按北京时间解释。"""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s))
    except ValueError:
        d = parse_date(s)
        return datetime(d.year, d.month, d.day, tzinfo=BJ_TZ) if d else None
    return dt if dt.tzinfo else dt.replace(tzinfo=BJ_TZ)


def date_to_str(d: date | None) -> str | None:
    return d.isoformat() if d else None


def dt_to_str(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") if dt else None


_REMAIN_RE = re.compile(
    r"剩余\s*(?:(\d+)\s*天)?\s*(?:(\d+)\s*小时)?\s*(?:(\d+)\s*分(?:钟)?)?"
)


def parse_remaining(text: str | None) -> timedelta | None:
    """把「剩余1天2小时30分钟」解析成 timedelta。

    学习通列表页只给相对倒计时，不给绝对时间戳，所以截止时间只能由
    「抓取时刻 + 剩余时长」推算。三个单位全缺时返回 None（避免误判成 0）。
    """
    if not text:
        return None
    m = _REMAIN_RE.search(text)
    if not m or not any(m.groups()):
        return None
    d, h, mi = (int(x) if x else 0 for x in m.groups())
    return timedelta(days=d, hours=h, minutes=mi)


def fmt_remaining(dt: datetime | None, *, now: datetime | None = None) -> str:
    """把截止时间渲染成「剩余2小时5分」/「已逾期3天」。"""
    if dt is None:
        return "—"
    now = now or now_bj()
    delta = dt - now
    total = int(delta.total_seconds())
    if total < 0:
        days, rem = divmod(-total, 86400)
        hours = rem // 3600
        return f"已逾期{days}天{hours}小时" if days else f"已逾期{hours}小时"
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"剩余{days}天{hours}小时"
    if hours:
        return f"剩余{hours}小时{minutes}分"
    return f"剩余{minutes}分"


def make_id(*parts: str) -> str:
    raw = "|".join(str(p) for p in parts if p).lower()
    h = hashlib.md5(raw.encode("utf-8")).hexdigest()[:10]
    prefix = re.sub(r"[^a-z0-9]+", "-", str(parts[0] or "item").lower())[:30].strip("-")
    return f"{prefix}-{h}"


def load_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def save_json(path: Path, data: Any, *, indent: int = 2) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=indent), encoding="utf-8")


def save_json_stable(path: Path, data: dict, *, stamp_key: str = "updated_at") -> None:
    """内容没变就不动时间戳（审计 §4.2）。

    finalize 每 20 分钟重跑课表解析，186 条记录一个字节都没变，但 updated_at
    每次都写「现在」→ workflow 的「无更新跳过提交」分支永远不命中 →
    每天 72 个纯时间戳 commit，把真正的变更淹掉。
    """
    old = load_json(path)
    if isinstance(old, dict) and isinstance(data, dict):
        if ({k: v for k, v in old.items() if k != stamp_key}
                == {k: v for k, v in data.items() if k != stamp_key}):
            data = {**data, stamp_key: old.get(stamp_key) or data.get(stamp_key)}
    save_json(path, data)


def env(key: str, default: str | None = None, required: bool = False) -> str:
    v = os.environ.get(key, default)
    if required and not v:
        log(f"环境变量 {key} 未设置，无法继续", level="ERROR")
        sys.exit(1)
    return v or ""


def safe_text(s: Any, max_len: int = 500) -> str:
    if s is None:
        return ""
    s = str(s).strip()
    return s if len(s) <= max_len else s[: max_len - 3] + "..."


def clean_html_text(s: str) -> str:
    """去标签 + 反转义 + 压缩空白。公告站标题里常有 &nbsp; 和多余空格。"""
    import html
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


# 学号是 8 位以上数字；课程名里的年级/学期（2026、2026-2027）只有 4 位，不会被误伤。
_STUDENT_ID = re.compile(r"\d{7,}[,，、]?")
_CLASS_NO = re.compile(r"[（(]\s*\d+\s*[)）]")


def scrub_course_name(s: str) -> str:
    """去掉课程名里嵌的同学学号与教学班编号。

    形状：`计算思维与人工智能<学号>,<学号>,<学号>,<学号>(111)`。
    学号 + 学校域名足以定位到具体的人，公开仓库里不能留。
    示例一律用占位符，**不要把真实学号抄进源码或文档**（踩过两次，见 docs/开发习惯.md 第 8 条）。
    """
    s = _CLASS_NO.sub("", _STUDENT_ID.sub("", s or ""))
    return re.sub(r"\s+", " ", re.sub(r"[,，、\s]+$", "", s)).strip()


# 审计 §2.1：考试/作业标题以教师真实姓名开头。实测形状是
#   <姓名><学年>-<学年>（<学期>）<课程名><任务名>
# 姓名 2-4 个汉字，紧跟四位数学年区间。**用结构规则，不用姓名黑名单** ——
# 黑名单要把真实姓名写进仓库，那本身就是二次泄露（学号已经踩过三次）。
_TEACHER_PREFIX = re.compile(r"^[一-鿿]{2,4}(?=\d{4}\s*[-—–~]\s*\d{4})")


def scrub_task_title(s: str) -> str:
    """剥掉任务标题开头的教师姓名。

    必须要求「学年-学年」这个形状，不能只写「汉字开头 + 四位数字」：
    实测有合法标题 `作业20261022`，宽松规则会把它削成 `1022`。
    正常课程名（高等数学A / 军事理论（同济大学版））不含这个形状，不会被误伤。
    """
    return _TEACHER_PREFIX.sub("", s or "").strip()


# 审计 §2.5 / §7.2：cpi / enc / stuenc / answerId 都是每生一份的，能定位到用户本人，
# 所以用白名单而不是黑名单 —— 学习通以后新增什么个人参数都不会漏出去。
_PUBLIC_KEEP = {"courseid", "classid", "clazzid", "workid", "ut", "ismooc2"}


def public_url(url: str, mode: str = "course") -> str:
    """公开产物里的链接去掉可定位到个人的参数。

    mode="course"（默认）：只留课程级参数，cpi/enc/answerId 一律去掉。
    mode="full"：原样返回。用户在 data/sources.json 的 study.public_links 里自行取舍 ——
    这是产品取舍不是 bug，所以做成一行配置，而不是写死。
    """
    if mode == "full" or not url:
        return url
    from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
    p = urlparse(url)
    keep = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
            if k.lower() in _PUBLIC_KEEP]
    return urlunparse(p._replace(query=urlencode(keep)))


def strip_personal_fields(task: dict, mode: str = "course") -> dict:
    """落盘前抹掉每生一份的字段。data/ 是公开仓库的一部分，不是本地缓存。"""
    out = {k: v for k, v in task.items() if k not in ("cpi", "answer_id")}
    if task.get("url"):
        out["url"] = public_url(task["url"], mode)
    return out


_REDACT_PATTERNS = [
    re.compile(r"(access_token=)[^&\s\"']+"),
    re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]+"),
    re.compile(r"(sk-)[A-Za-z0-9]{8,}"),
    re.compile(r"(ghp_)[A-Za-z0-9]{8,}"),
    re.compile(r"((?:password|passwd|pwd|api_key|apikey|token|secret)[\"']?\s*[:=]\s*[\"']?)[^\s&\"',}]+", re.I),
    # 审计 §2.6：学习通整条链路靠 Cookie，requests 的异常字符串会带完整请求头。
    # 仓库是 public → Actions 日志也是 public，漏一个就是明文公开。
    re.compile(r"((?:Cookie|Set-Cookie|JSESSIONID|_uid|UID|uf|_d|vc3|fid|unam|route)[\"']?\s*[:=]\s*[\"']?)[^\s&\"',}]+", re.I),
    re.compile(r"(?i)(cookie[\"']?\s*[:=]\s*[\"']?)[^\n\"']+"),
]


def redact(s: Any) -> str:
    """抹掉日志里的凭据。

    requests 的异常字符串会带完整 URL（含 access_token），AI 客户端的异常会带
    Authorization 头。这些日志会进 Actions 公开日志和飞书卡片，必须先洗一遍。
    """
    text = str(s)
    for p in _REDACT_PATTERNS:
        text = p.sub(lambda m: m.group(1) + "***", text)
    return text


def exc_brief(exc: BaseException) -> str:
    """异常摘要：类型 + 出错位置 + 消息。**不发完整栈**（审计 §2.6）。

    完整 traceback 会把局部变量里的 URL、请求头、cookie 一起带进公开日志。
    排查需要的信息（哪一行、什么错）这里都有，够了。
    """
    import traceback
    frames = traceback.extract_tb(exc.__traceback__)
    where = f"{Path(frames[-1].filename).name}:{frames[-1].lineno}" if frames else "?"
    # 先脱敏再截断：反过来会把长凭据切断，切断后的残段匹配不上脱敏正则。
    return redact(f"{type(exc).__name__} @ {where}: {exc}")[:300]