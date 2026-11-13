"""WakeUp 课表 .ics 解析。

WakeUp 导出的特点（决定了这里的写法）：
- 每周一条 VEVENT，RRULE 全是 FREQ=WEEKLY;COUNT=1 —— 不需要展开循环规则
- DTSTART/DTEND 不带时区（floating time），必须按北京时间解释
- DESCRIPTION 是「第N周」，这是唯一的周次来源
- LOCATION 是「教室 教师」拼在一起，需要拆

产物：
  data/schedule/courses.json  按「课程+教室+星期+节次」聚合，含周次列表
  data/schedule/events.json   186 条具体上课记录，供周视图 SVG 用
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

from utils import (ASSETS_DIR, DATA_DIR, BJ_TZ, log, now_bj,
                   save_json, today_bj)

ICS_PATH = ASSETS_DIR / "WakeUp导入课表.ics"
SCHEDULE_DIR = DATA_DIR / "schedule"

# 河南城建学院作息表。用 start/end 精确匹配节次，全部 186 条都能对上。
SLOTS = [
    (1, "08:00", "08:45"),
    (2, "08:50", "09:35"),
    (3, "10:05", "10:50"),
    (4, "10:55", "11:40"),
    (5, "14:00", "14:45"),
    (6, "14:50", "15:35"),
    (7, "16:05", "16:50"),
    (8, "16:55", "17:40"),
    (9, "19:00", "19:45"),
    (10, "19:50", "20:35"),
    (11, "20:40", "21:25"),
]
_START_SLOT = {s: n for n, s, _ in SLOTS}
_END_SLOT = {e: n for n, _, e in SLOTS}

WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


# ---------------------------------------------------------------------------
# ICS 读取
# ---------------------------------------------------------------------------
def unfold(text: str) -> str:
    """ICS 折行：以空格/制表符开头的行是上一行的续行。"""
    return re.sub(r"\r?\n[ \t]", "", text)


def parse_ics(path: Path) -> list[dict]:
    if not path.exists():
        log(f"课表文件不存在：{path}", level="ERROR")
        return []
    text = unfold(path.read_text(encoding="utf-8-sig"))
    events = []
    for block in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", text, re.DOTALL):
        props: dict[str, str] = {}
        for line in block.splitlines():
            line = line.strip()
            if not line or ":" not in line:
                continue
            key, _, val = line.partition(":")
            # 去掉参数部分，如 DTSTART;TZID=Asia/Shanghai
            props[key.split(";")[0].upper()] = val.strip()
        if props.get("DTSTART"):
            events.append(props)
    return events


def parse_ics_dt(s: str) -> datetime | None:
    """20261007T100500 / 20261007T100500Z / 20261007 → 北京时间 datetime。"""
    s = (s or "").strip()
    for fmt in ("%Y%m%dT%H%M%S", "%Y%m%dT%H%M", "%Y%m%d"):
        try:
            dt = datetime.strptime(s.rstrip("Z"), fmt)
        except ValueError:
            continue
        if fmt == "%Y%m%d":
            dt = dt.replace(hour=0, minute=0)
        return dt.replace(tzinfo=BJ_TZ)
    return None


def split_location(loc: str) -> tuple[str, str]:
    """「美荟楼1#328 贾汛」→ (教室, 教师)；「胡肄军」→ ("", 教师)。"""
    loc = (loc or "").strip()
    if not loc:
        return "", ""
    parts = loc.split()
    if len(parts) >= 2:
        return " ".join(parts[:-1]), parts[-1]
    only = parts[0]
    if re.search(r"[0-9#楼室]", only):
        return only, ""
    return "", only


def clean_summary(s: str) -> tuple[str, str]:
    """拆出课程名和方括号里的专题。

    「计算思维与人工智能[Agent系统搭建]」→ (计算思维与人工智能, Agent系统搭建)
    「大学体育（一）-大学体育（一）」→ (大学体育（一）, "")
    """
    s = (s or "").strip()
    topic = ""
    m = re.search(r"[\[【]([^\]】]+)[\]】]", s)
    if m:
        topic = m.group(1).strip()
        s = s[:m.start()].strip()
    if "-" in s:
        a, _, b = s.partition("-")
        if a.strip() == b.strip():
            s = a.strip()
    return s, topic


def slot_range(start: datetime, end: datetime) -> tuple[int, int]:
    sm, em = start.strftime("%H:%M"), end.strftime("%H:%M")
    a = _START_SLOT.get(sm)
    b = _END_SLOT.get(em)
    if a is None:  # 时间不在作息表里（调课/临时活动），取最接近的一节
        a = min(SLOTS, key=lambda s: abs(_minutes(s[1]) - _minutes(sm)))[0]
    if b is None:
        b = min(SLOTS, key=lambda s: abs(_minutes(s[2]) - _minutes(em)))[0]
    return a, max(a, b)


def _minutes(hhmm: str) -> int:
    h, _, m = hhmm.partition(":")
    return int(h) * 60 + int(m)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def build_events(raw: list[dict]) -> list[dict]:
    out = []
    for p in raw:
        start = parse_ics_dt(p.get("DTSTART", ""))
        end = parse_ics_dt(p.get("DTEND", "")) or start
        if not start:
            continue
        week = None
        m = re.search(r"第\s*(\d+)\s*周", p.get("DESCRIPTION", ""))
        if m:
            week = int(m.group(1))
        course, topic = clean_summary(p.get("SUMMARY", ""))
        room, teacher = split_location(p.get("LOCATION", ""))
        s1, s2 = slot_range(start, end)
        out.append({
            "uid": p.get("UID", ""),
            "date": start.date().isoformat(),
            "weekday": start.weekday(),
            "weekday_cn": WEEKDAY_CN[start.weekday()],
            "start": start.strftime("%H:%M"),
            "end": end.strftime("%H:%M"),
            "slot_start": s1,
            "slot_end": s2,
            "slots": f"{s1}-{s2}" if s2 > s1 else str(s1),
            "course": course,
            "topic": topic,
            "room": room,
            "teacher": teacher,
            "week": week,
        })
    out.sort(key=lambda e: (e["date"], e["start"], e["course"]))
    return out


def guess_semester_start(events: list[dict]) -> str | None:
    """用「第N周」反推第1周周一，取众数（个别周次写错也不影响）。"""
    votes = Counter()
    for e in events:
        if not e.get("week"):
            continue
        d = date.fromisoformat(e["date"])
        monday = d - timedelta(days=d.weekday(), weeks=e["week"] - 1)
        votes[monday.isoformat()] += 1
    if not votes:
        return None
    return votes.most_common(1)[0][0]


def aggregate_courses(events: list[dict]) -> list[dict]:
    groups: dict[tuple, dict] = {}
    for e in events:
        key = (e["course"], e["topic"], e["room"], e["teacher"],
               e["weekday"], e["start"], e["end"])
        g = groups.get(key)
        if g is None:
            g = {
                "course": e["course"], "topic": e["topic"],
                "room": e["room"], "teacher": e["teacher"],
                "weekday": e["weekday"], "weekday_cn": e["weekday_cn"],
                "start": e["start"], "end": e["end"],
                "slots": e["slots"], "slot_start": e["slot_start"], "slot_end": e["slot_end"],
                "weeks": [], "count": 0,
            }
            groups[key] = g
        if e.get("week") and e["week"] not in g["weeks"]:
            g["weeks"].append(e["week"])
        g["count"] += 1
    out = list(groups.values())
    for g in out:
        g["weeks"].sort()
    out.sort(key=lambda g: (g["weekday"], g["slot_start"], g["course"]))
    return out


def current_week(semester_start: str | None, today: date | None = None) -> int | None:
    if not semester_start:
        return None
    today = today or today_bj()
    d0 = date.fromisoformat(semester_start)
    return (today - d0).days // 7 + 1


def week_events(events: list[dict], week: int | None) -> list[dict]:
    if week is None:
        return []
    return [e for e in events if e.get("week") == week]


def run(today: date | None = None) -> dict:
    raw = parse_ics(ICS_PATH)
    if not raw:
        return {"ok": False, "events": 0}
    events = build_events(raw)
    semester_start = guess_semester_start(events)
    courses = aggregate_courses(events)
    cw = current_week(semester_start, today)
    weeks = [e["week"] for e in events if e.get("week")]
    this_week = week_events(events, cw)

    meta = {
        "semester_start": semester_start,
        "total_weeks": max(weeks) if weeks else None,
        "current_week": cw,
        "today": (today or today_bj()).isoformat(),
        "event_count": len(events),
        "course_count": len(courses),
        "this_week_count": len(this_week),
        "date_range": [events[0]["date"], events[-1]["date"]] if events else [],
    }
    save_json(SCHEDULE_DIR / "events.json", {"updated_at": now_bj().isoformat(timespec="seconds"),
                                             **meta, "items": events})
    save_json(SCHEDULE_DIR / "courses.json", {"updated_at": now_bj().isoformat(timespec="seconds"),
                                              **meta, "items": courses})
    save_json(SCHEDULE_DIR / "this_week.json", {"updated_at": now_bj().isoformat(timespec="seconds"),
                                                **meta, "items": this_week})
    log(f"课表解析：{len(events)} 条上课记录，{len(courses)} 个课程条目，"
        f"第1周周一={semester_start}，当前第 {cw} 周，本周课时：{len(this_week)}")
    return {"ok": True, **meta}


if __name__ == "__main__":
    run()