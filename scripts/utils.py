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


_REDACT_PATTERNS = [
    re.compile(r"(access_token=)[^&\s\"']+"),
    re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]+"),
    re.compile(r"(sk-)[A-Za-z0-9]{8,}"),
    re.compile(r"(ghp_)[A-Za-z0-9]{8,}"),
    re.compile(r"((?:password|passwd|pwd|api_key|apikey|token|secret)[\"']?\s*[:=]\s*[\"']?)[^\s&\"',}]+", re.I),
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