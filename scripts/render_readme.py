"""README 渲染。

纯离线：只读 data/ 下的 JSON，不发任何网络请求、不花任何 token。
这是它能每 20 分钟跑一次的前提。

任务清单只列「能做的」：待完成、未逾期、开始不超过一周。
全部课程都出现，按活跃度（作业发表数量）降序，没任务的写「无」。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from dedup import load_list, sort_by_deadline
from utils import (DATA_DIR, DOCS_DATA_DIR, REPO_ROOT, load_json,
                   log, now_bj, parse_datetime, save_json)

TEMPLATE = REPO_ROOT / "scripts" / "readme_template.md"
README = REPO_ROOT / "README.md"

# 公告与潜在计划从 README 挪进独立文档，README 只留目录链接。
# 两者都是生成物，由 finalize 独占提交权（采集模式会 git checkout -- docs 还原）。
NOTICE_TEMPLATE = REPO_ROOT / "scripts" / "notice_template.md"
PLANS_TEMPLATE = REPO_ROOT / "scripts" / "plans_template.md"
NOTICE_DOC = REPO_ROOT / "docs" / "公告.md"
PLANS_DOC = REPO_ROOT / "docs" / "潜在计划.md"

KIND_CN = {"work": "作业", "eval": "互评", "exam": "考试"}
WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
# 开始时间距今超过这么多天的任务视为陈旧，不再显示（用户做不动了）
STALE_DAYS = 7
NOTICE_LIMIT = 12
PLAN_LIMIT = 15


def esc(s: str) -> str:
    """Markdown 表格里的竖线和换行会破表。"""
    return str(s or "").replace("|", "\\|").replace("\n", " ").strip()


def md_link(text: str, url: str) -> str:
    return f"[{esc(text)}]({url})" if url else esc(text)


# ---------------------------------------------------------------------------
# 任务清单
# ---------------------------------------------------------------------------
def task_start(t: dict) -> datetime | None:
    """开始时间：优先详情页取到的真实值，取不到退回首次发现时间（列表页不给发布时间）。"""
    return parse_datetime(t.get("start_time")) or parse_datetime(t.get("first_seen"))


def actionable(tasks: list[dict], now: datetime) -> tuple[list[dict], dict]:
    """只留能做的：待完成、未逾期、开始不超过一周。已完成/已批阅的一律不进清单。"""
    kept: list[dict] = []
    dropped = {"not_pending": 0, "overdue": 0, "stale": 0}
    cutoff = now - timedelta(days=STALE_DAYS)
    for t in tasks:
        if t.get("state") != "pending":
            dropped["not_pending"] += 1
            continue
        dt = parse_datetime(t.get("deadline"))
        if dt and dt < now:
            dropped["overdue"] += 1
            continue
        st = task_start(t)
        if st and st < cutoff:
            dropped["stale"] += 1
            continue
        kept.append(t)
    return kept, dropped


def course_key(d: dict) -> tuple[str, str]:
    """课程身份 = course_id + class_id。

    同一门课可能挂在两个教学班下（实测英语课 course_id 201780805 有
    class_id 156400069 / 156290484 两条），只按 course_id 归组会让两个班
    显示同一批任务。
    """
    return (d.get("course_id") or "", d.get("class_id") or "")


def course_rows(courses: list[dict], kept: list[dict]) -> list[str]:
    """全部课程都列出来，按活跃度（作业发表数量）降序；没任务的写「无」。"""
    by_course: dict[tuple[str, str], list[dict]] = {}
    for t in kept:
        by_course.setdefault(course_key(t), []).append(t)
    ordered = sorted(courses, key=lambda c: (-(c.get("task_count") or 0),
                                             -(c.get("pending_count") or 0),
                                             c.get("name") or ""))
    lines = ["| 课程 | 作业数 | 待完成 | 任务（截止近 → 远） |",
             "| --- | --- | --- | --- |"]
    for c in ordered:
        items = sort_by_deadline(by_course.get(course_key(c), []))
        cells = []
        for t in items:
            dt = parse_datetime(t.get("deadline"))
            when = dt.strftime("%m-%d %H:%M") if dt else "无截止"
            if dt and not t.get("deadline_exact"):
                when += " ≈"
            cells.append("{}（{}·{}）".format(
                md_link(t.get("title"), t.get("url")),
                KIND_CN.get(t.get("kind"), t.get("kind") or ""), when))
        lines.append("| {} | {} | {} | {} |".format(
            esc(c.get("name")), c.get("task_count") or 0,
            len(items) if items else "**无**",
            "<br>".join(cells) if cells else "—"))
    return lines


def render_tasks(tasks: list[dict], now: datetime) -> tuple[str, str, dict]:
    kept, dropped = actionable(tasks, now)
    courses = load_list(DATA_DIR / "study" / "courses.json")
    body = "\n".join(course_rows(courses, kept)) + "\n"

    today = now.date()
    counts = {"today": 0, "week": 0, "later": 0}
    for t in kept:
        dt = parse_datetime(t.get("deadline"))
        if dt is None:
            counts["later"] += 1
        elif dt.date() == today:
            counts["today"] += 1
        elif dt - now <= timedelta(days=7):
            counts["week"] += 1
        else:
            counts["later"] += 1

    note = ("> 已剔除：逾期 {} 项 · 开始超过 {} 天 {} 项 · 非待完成 {} 项。"
            "课程按活跃度（作业发表数量）排序，共 {} 门。\n").format(
                dropped["overdue"], STALE_DAYS, dropped["stale"],
                dropped["not_pending"], len(courses))
    suffix = f"，其中 **{counts['today']} 项今天截止**" if counts["today"] else ""
    stats = {"pending": len(kept), "overdue": 0, **counts,
             "dropped": dropped, "courses": len(courses), "suffix": suffix}
    return body, note, stats


def render_alert(stats: dict, meta: dict) -> str:
    if stats["today"]:
        return (f"> ## 🟠 今天有 {stats['today']} 项截止\n"
                f"> 就在下方清单里，别拖到明天。\n")
    if not meta.get("study", {}).get("ok"):
        return ("> ## ⚠️ 学习通数据未同步\n"
                "> 最近一次采集失败或未配置凭据，下方清单可能不是最新的。详见「数据源状态」。\n")
    return ""


# ---------------------------------------------------------------------------
# 课表
# ---------------------------------------------------------------------------
def render_schedule(now: datetime) -> tuple[str, str, str]:
    courses = load_json(DATA_DIR / "schedule" / "courses.json") or {}
    this_week = load_list(DATA_DIR / "schedule" / "this_week.json")
    cw = courses.get("current_week")
    total = courses.get("total_weeks")
    if cw is None:
        return "课表尚未解析。", "_无_", "课表未解析"
    info = f"第 **{cw}** 周" + (f" / 共 {total} 周" if total else "")
    detail = f"当前是 **{info}**，本周 {len(this_week)} 节课。"
    if not this_week:
        return info, "_本周没有课。_\n", detail

    lines = ["| 星期 | 节次 | 时间 | 课程 | 教室 | 教师 |",
             "| --- | --- | --- | --- | --- | --- |"]
    for e in sorted(this_week, key=lambda x: (x["weekday"], x["slot_start"])):
        name = e["course"] + (f"（{e['topic']}）" if e.get("topic") else "")
        lines.append("| {} | {} | {}–{} | {} | {} | {} |".format(
            e["weekday_cn"], e["slots"], e["start"], e["end"],
            esc(name), esc(e.get("room")) or "—", esc(e.get("teacher")) or "—"))
    return info, "\n".join(lines) + "\n", detail


# ---------------------------------------------------------------------------
# 公告 / 计划 / 名言
# ---------------------------------------------------------------------------
def render_notices(limit: int = NOTICE_LIMIT) -> tuple[str, int]:
    items = [n for n in load_list(DATA_DIR / "notice" / "notices.json")
             if (n.get("score") or 0) >= 3]
    items.sort(key=lambda n: n.get("publish_date") or "", reverse=True)
    items.sort(key=lambda n: -(n.get("score") or 0))
    items = items[:limit]
    if not items:
        return "_暂无高相关公告。_\n", 0
    lines = ["| 分 | 日期 | 公告 | 来源 | 截止 | 说明 |",
             "| --- | --- | --- | --- | --- | --- |"]
    for n in items:
        lines.append("| {} | {} | {} | {} | {} | {} |".format(
            n.get("score") or 0, n.get("publish_date") or "—",
            md_link(n.get("title"), n.get("url")), esc(n.get("source")),
            n.get("deadline") or "—", esc(n.get("description") or n.get("reason")) or "—"))
    return "\n".join(lines) + "\n", len(items)


def render_plans(limit: int = PLAN_LIMIT) -> tuple[str, int]:
    items = [p for p in load_list(DATA_DIR / "diary" / "plans.json")
             if p.get("state") == "open"]
    items.sort(key=lambda p: (-(p.get("confidence") or 0), p.get("due_date") or "9999"))
    items = items[:limit]
    if not items:
        return "_暂无未完成计划。_\n", 0
    lines = ["| 计划 | 置信度 | 截止 | 来源 | 标签 |",
             "| --- | --- | --- | --- | --- |"]
    for p in items:
        lines.append("| {} | {} | {} | {} | {} |".format(
            esc(p.get("text")), f"{(p.get('confidence') or 0):.1f}",
            p.get("due_date") or "—", esc(p.get("source_date") or p.get("source_file")),
            esc(" / ".join(p.get("tags") or [])) or "—"))
    return "\n".join(lines) + "\n", len(items)


def render_quotes(limit: int = 8) -> str:
    items = load_list(DATA_DIR / "diary" / "quotes.json")[:limit]
    if not items:
        return "_暂无摘录。_\n"
    out = []
    for q in items:
        date_part = f" — {q['source_date']}" if q.get("source_date") else ""
        block = f"> {esc(q.get('text'))}\n>\n> <sub>{esc(q.get('context'))}{date_part}</sub>\n"
        if q.get("comment"):
            block += f">\n> 💬 {esc(q['comment'])}\n"
        out.append(block)
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 数据源状态
# ---------------------------------------------------------------------------
def render_sources(now: datetime) -> tuple[str, dict]:
    study = load_json(DATA_DIR / "study" / "meta.json") or {}
    notice = load_json(DATA_DIR / "notice" / "notices.json") or {}
    diary = load_json(DATA_DIR / "diary" / "state.json") or {}
    sched = load_json(DATA_DIR / "schedule" / "courses.json") or {}

    def ago(iso: str | None) -> str:
        dt = parse_datetime(iso)
        if not dt:
            return "从未"
        mins = int((now - dt).total_seconds() // 60)
        if mins < 1:
            return "刚刚"
        if mins < 60:
            return f"{mins} 分钟前"
        if mins < 1440:
            return f"{mins // 60} 小时前"
        return f"{mins // 1440} 天前"

    rows = [
        ("学习通作业", "✅ 正常" if study.get("ok") else "❌ 失败",
         ago(study.get("last_success") or study.get("last_attempt")),
         f"{study.get('courses_ok', 0)}/{study.get('courses', 0)} 门课，{study.get('tasks', 0)} 条任务"),
        ("校园公告", "✅ 有数据" if notice.get("count") else "⚪ 未采集",
         ago(notice.get("updated_at")), f"{notice.get('count', 0)} 条入库"),
        ("Gitee 日记", "✅ 有数据" if diary.get("last_run") else "⚪ 未采集",
         ago(diary.get("last_run")), f"{diary.get('file_count', 0)} 个 md 文件"),
        ("WakeUp 课表", "✅ 已解析" if sched.get("current_week") else "⚪ 未解析",
         ago(sched.get("updated_at")), f"{sched.get('event_count', 0)} 条上课记录"),
    ]
    lines = ["| 数据源 | 状态 | 最近成功 | 规模 |", "| --- | --- | --- | --- |"]
    for r in rows:
        lines.append("| " + " | ".join(esc(x) for x in r) + " |")
    return "\n".join(lines) + "\n", {"study": study}


# ---------------------------------------------------------------------------
# 独立文档（公告 / 潜在计划）
# ---------------------------------------------------------------------------
def render_doc(template, out, repl: dict, now) -> bool:
    """按模板渲染一份独立文档。缺模板只报错不抛异常，避免拖垮整个 finalize。"""
    if not template.exists():
        log(f"模板缺失：{template}", level="ERROR")
        return False
    text = template.read_text(encoding="utf-8")
    full = {"{{UPDATED_AT}}": now.strftime("%Y-%m-%d %H:%M:%S (UTC+8)"), **repl}
    for k, v in full.items():
        text = text.replace(k, v)
    out.write_text(text, encoding="utf-8")
    return True


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def run() -> bool:
    now = now_bj()
    tasks = load_list(DATA_DIR / "study" / "tasks.json")
    body, filter_note, stats = render_tasks(tasks, now)
    week_info, sched_table, week_detail = render_schedule(now)
    sources, meta = render_sources(now)
    alert = render_alert(stats, meta)
    notices, notice_count = render_notices()
    plans, plan_count = render_plans()

    if not TEMPLATE.exists():
        log(f"模板缺失：{TEMPLATE}", level="ERROR")
        return False
    text = TEMPLATE.read_text(encoding="utf-8")
    repl = {
        "{{UPDATED_AT}}": now.strftime("%Y-%m-%d %H:%M:%S (UTC+8)"),
        "{{WEEK_INFO}}": week_info,
        "{{WEEK_INFO_DETAIL}}": week_detail,
        "{{ALERT}}": alert,
        "{{PENDING_COUNT}}": str(stats["pending"]),
        "{{STATS_SUFFIX}}": stats["suffix"],
        "{{TASKS}}": body,
        "{{FILTER_NOTE}}": filter_note,
        "{{SCHEDULE_TABLE}}": sched_table,
        "{{QUOTES}}": render_quotes(),
        "{{SOURCE_STATUS}}": sources,
        "{{NOTICE_COUNT}}": str(notice_count),
        "{{PLAN_COUNT}}": str(plan_count),
    }
    for k, v in repl.items():
        text = text.replace(k, v)
    README.write_text(text, encoding="utf-8")

    # 公告与潜在计划不再占 README 篇幅，各自渲染成独立文档，README 目录区只留链接
    ok_n = render_doc(NOTICE_TEMPLATE, NOTICE_DOC,
                      {"{{NOTICES}}": notices, "{{NOTICE_LIMIT}}": str(NOTICE_LIMIT)}, now)
    ok_p = render_doc(PLANS_TEMPLATE, PLANS_DOC,
                      {"{{PLANS}}": plans, "{{PLAN_LIMIT}}": str(PLAN_LIMIT)}, now)

    # 网页版数据（docs/ 是 GitHub Pages 目录，前端只能读到这里面的文件）
    # 前端自己按 state 分桶，所以这里必须只喂可做的任务，否则网页会显示出 README 已剔除的逾期项
    kept, _ = actionable(tasks, now)
    save_json(DOCS_DATA_DIR / "tasks.json", {"updated_at": now.isoformat(timespec="seconds"),
                                             "stats": {k: v for k, v in stats.items() if k != "suffix"},
                                             "items": kept,
                                             "courses": load_list(DATA_DIR / "study" / "courses.json")})
    save_json(DOCS_DATA_DIR / "meta.json", {"updated_at": now.isoformat(timespec="seconds"),
                                            "week": week_info.replace("**", ""), "stats": stats,
                                            "sources": meta})
    for name in ("notices", "plans", "quotes"):
        src = {"notices": DATA_DIR / "notice" / "notices.json",
               "plans": DATA_DIR / "diary" / "plans.json",
               "quotes": DATA_DIR / "diary" / "quotes.json"}[name]
        save_json(DOCS_DATA_DIR / f"{name}.json", load_json(src) or {"items": []})
    d = stats["dropped"]
    log(f"任务清单：{stats['pending']} 条可做 / {stats['courses']} 门课"
        f"（今天 {stats['today']} / 7天内 {stats['week']} / 更远 {stats['later']}）")
    log(f"已剔除：逾期 {d['overdue']} / 超过{STALE_DAYS}天 {d['stale']} / 非待完成 {d['not_pending']}")
    log(f"独立文档：公告 {notice_count} 条 / 潜在计划 {plan_count} 项")
    log("README 已生成")
    return ok_n and ok_p


if __name__ == "__main__":
    run()