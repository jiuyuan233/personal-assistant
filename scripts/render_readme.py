"""README 渲染。

纯离线：只读 data/ 下的 JSON，不发任何网络请求、不花任何 token。
这是它能每 20 分钟跑一次的前提。

任务清单按截止时间分四档（逾期 / 今天 / 7天内 / 更远），档内升序。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from dedup import load_list, sort_by_deadline
from utils import (DATA_DIR, DOCS_DATA_DIR, REPO_ROOT, fmt_remaining, load_json,
                   log, now_bj, parse_datetime, save_json)

TEMPLATE = REPO_ROOT / "scripts" / "readme_template.md"
README = REPO_ROOT / "README.md"

KIND_CN = {"work": "作业", "eval": "互评", "exam": "考试"}
STATE_CN = {"pending": "待完成", "submitted": "已提交", "done": "已完成", "unknown": "未知"}
WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def esc(s: str) -> str:
    """Markdown 表格里的竖线和换行会破表。"""
    return str(s or "").replace("|", "\\|").replace("\n", " ").strip()


def md_link(text: str, url: str) -> str:
    return f"[{esc(text)}]({url})" if url else esc(text)


# ---------------------------------------------------------------------------
# 任务清单
# ---------------------------------------------------------------------------
def bucket_tasks(tasks: list[dict], now: datetime) -> dict[str, list[dict]]:
    today = now.date()
    buckets: dict[str, list[dict]] = {"overdue": [], "today": [], "week": [], "later": []}
    for t in sort_by_deadline(tasks):
        dt = parse_datetime(t.get("deadline"))
        if dt is None:
            buckets["later"].append(t)
        elif dt < now:
            buckets["overdue"].append(t)
        elif dt.date() == today:
            buckets["today"].append(t)
        elif dt - now <= timedelta(days=7):
            buckets["week"].append(t)
        else:
            buckets["later"].append(t)
    return buckets


def task_table(tasks: list[dict], now: datetime) -> str:
    if not tasks:
        return "_无_\n"
    lines = ["| 截止时间 | 课程 | 任务 | 类型 | 状态 | 倒计时 |",
             "| --- | --- | --- | --- | --- | --- |"]
    for t in tasks:
        dt = parse_datetime(t.get("deadline"))
        when = dt.strftime("%m-%d %H:%M") if dt else "—"
        if dt and not t.get("deadline_exact"):
            when += " ≈"
        lines.append("| {} | {} | {} | {} | {} | {} |".format(
            when,
            esc(t.get("course")),
            md_link(t.get("title"), t.get("url")),
            KIND_CN.get(t.get("kind"), t.get("kind") or ""),
            STATE_CN.get(t.get("state"), t.get("status") or ""),
            fmt_remaining(dt, now=now),
        ))
    return "\n".join(lines) + "\n"


def render_tasks(tasks: list[dict], now: datetime) -> tuple[str, str, dict]:
    pending = [t for t in tasks if t.get("state") == "pending"]
    buckets = bucket_tasks(pending, now)
    counts = {k: len(v) for k, v in buckets.items()}

    parts = []
    headings = [
        ("overdue", f"### 🔴 已逾期（{counts['overdue']}）"),
        ("today", f"### 🟠 今天截止（{counts['today']}）"),
        ("week", f"### 🟡 7 天内（{counts['week']}）"),
        ("later", f"### ⚪ 更远 / 无截止时间（{counts['later']}）"),
    ]
    for key, title in headings:
        if not buckets[key]:
            continue
        parts.append(title + "\n\n" + task_table(buckets[key], now))
    body = "\n".join(parts) if parts else "_当前没有待完成任务。_\n"

    others = [t for t in tasks if t.get("state") in ("submitted", "done")]
    others = sort_by_deadline(others)[-20:]
    done_block = ""
    if others:
        done_block = ("<details>\n<summary>已提交 / 已完成（最近 {} 项，点击展开）</summary>\n\n".format(len(others))
                      + task_table(others, now) + "\n</details>\n")

    suffix = ""
    if counts["overdue"]:
        suffix = f"，其中 **{counts['overdue']} 项已逾期**"
    elif counts["today"]:
        suffix = f"，其中 **{counts['today']} 项今天截止**"
    return body, done_block, {"pending": len(pending), **counts, "suffix": suffix}


def render_alert(stats: dict, meta: dict) -> str:
    if stats["overdue"]:
        return (f"> ## 🔴 有 {stats['overdue']} 项作业已逾期\n"
                f"> 立刻处理，别等下次提醒。\n")
    if stats["today"]:
        return (f"> ## 🟠 今天有 {stats['today']} 项截止\n"
                f"> 见下方「今天截止」分区。\n")
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
def render_notices(limit: int = 12) -> str:
    items = [n for n in load_list(DATA_DIR / "notice" / "notices.json")
             if (n.get("score") or 0) >= 3]
    items.sort(key=lambda n: n.get("publish_date") or "", reverse=True)
    items.sort(key=lambda n: -(n.get("score") or 0))
    items = items[:limit]
    if not items:
        return "_暂无高相关公告。_\n"
    lines = ["| 分 | 日期 | 公告 | 来源 | 截止 | 说明 |",
             "| --- | --- | --- | --- | --- | --- |"]
    for n in items:
        lines.append("| {} | {} | {} | {} | {} | {} |".format(
            n.get("score") or 0, n.get("publish_date") or "—",
            md_link(n.get("title"), n.get("url")), esc(n.get("source")),
            n.get("deadline") or "—", esc(n.get("reason")) or "—"))
    return "\n".join(lines) + "\n"


def render_plans(limit: int = 15) -> str:
    items = [p for p in load_list(DATA_DIR / "diary" / "plans.json")
             if p.get("state") == "open"]
    items.sort(key=lambda p: (-(p.get("confidence") or 0), p.get("due_date") or "9999"))
    items = items[:limit]
    if not items:
        return "_暂无未完成计划。_\n"
    lines = ["| 计划 | 置信度 | 截止 | 来源 | 标签 |",
             "| --- | --- | --- | --- | --- |"]
    for p in items:
        lines.append("| {} | {} | {} | {} | {} |".format(
            esc(p.get("text")), f"{(p.get('confidence') or 0):.1f}",
            p.get("due_date") or "—", esc(p.get("source_date") or p.get("source_file")),
            esc(" / ".join(p.get("tags") or [])) or "—"))
    return "\n".join(lines) + "\n"


def render_quotes(limit: int = 8) -> str:
    items = load_list(DATA_DIR / "diary" / "quotes.json")[:limit]
    if not items:
        return "_暂无摘录。_\n"
    out = []
    for q in items:
        date_part = f" — {q['source_date']}" if q.get("source_date") else ""
        out.append(f"> {esc(q.get('text'))}\n>\n> <sub>{esc(q.get('context'))}{date_part}</sub>\n")
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
# 主流程
# ---------------------------------------------------------------------------
def run() -> bool:
    now = now_bj()
    tasks = load_list(DATA_DIR / "study" / "tasks.json")
    body, done_block, stats = render_tasks(tasks, now)
    week_info, sched_table, week_detail = render_schedule(now)
    sources, meta = render_sources(now)
    alert = render_alert(stats, meta)

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
        "{{DONE_BLOCK}}": done_block,
        "{{SCHEDULE_TABLE}}": sched_table,
        "{{NOTICES}}": render_notices(),
        "{{PLANS}}": render_plans(),
        "{{QUOTES}}": render_quotes(),
        "{{SOURCE_STATUS}}": sources,
    }
    for k, v in repl.items():
        text = text.replace(k, v)
    README.write_text(text, encoding="utf-8")

    # 网页版数据（docs/ 是 GitHub Pages 目录，前端只能读到这里面的文件）
    save_json(DOCS_DATA_DIR / "tasks.json", {"updated_at": now.isoformat(timespec="seconds"),
                                             "stats": {k: v for k, v in stats.items() if k != "suffix"},
                                             "items": tasks})
    save_json(DOCS_DATA_DIR / "meta.json", {"updated_at": now.isoformat(timespec="seconds"),
                                            "week": week_info.replace("**", ""), "stats": stats,
                                            "sources": meta})
    for name in ("notices", "plans", "quotes"):
        src = {"notices": DATA_DIR / "notice" / "notices.json",
               "plans": DATA_DIR / "diary" / "plans.json",
               "quotes": DATA_DIR / "diary" / "quotes.json"}[name]
        save_json(DOCS_DATA_DIR / f"{name}.json", load_json(src) or {"items": []})
    log(f"任务清单：{stats['pending']} 条（逾期 {stats['overdue']} / 今天 {stats['today']} / "
        f"7天内 {stats['week']} / 更远 {stats['later']}）")
    log("README 已生成")
    return True


if __name__ == "__main__":
    run()