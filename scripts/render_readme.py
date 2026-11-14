"""README 渲染。

纯离线：只读 data/ 下的 JSON，不发任何网络请求、不花任何 token。
这是它能每 20 分钟跑一次的前提。

任务清单只列「能做的」：待完成、未逾期。
全部课程都出现，按活跃度（作业发表数量）降序，没任务的写「无」。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from dedup import load_list, sort_by_deadline
from utils import (DATA_DIR, DOCS_DATA_DIR, REPO_ROOT, load_json,
                   log, now_bj, parse_datetime, save_json_stable)

TEMPLATE = REPO_ROOT / "scripts" / "readme_template.md"
README = REPO_ROOT / "README.md"

# 公告与潜在计划**同时**出现在 README（精简版）和独立文档（全量版）。
# 用户先要求移出 README 瘦身，后又要求加回来 —— 两处都留，README 用更小的上限。
# 两者都是生成物，由 finalize 独占提交权（采集模式会 git checkout -- docs 还原）。
NOTICE_TEMPLATE = REPO_ROOT / "scripts" / "notice_template.md"
PLANS_TEMPLATE = REPO_ROOT / "scripts" / "plans_template.md"
NOTICE_DOC = REPO_ROOT / "docs" / "公告.md"
PLANS_DOC = REPO_ROOT / "docs" / "潜在计划.md"
# 被清单剔除的任务单独成文，README 任务清单底部只留链接
EXCLUDED_TEMPLATE = REPO_ROOT / "scripts" / "excluded_template.md"
EXCLUDED_DOC = REPO_ROOT / "docs" / "已剔除任务.md"

KIND_CN = {"work": "作业", "eval": "互评", "exam": "考试"}
REASON_CN = {"overdue": "已逾期", "not_pending": "非待完成"}
REASON_ORDER = ("overdue", "not_pending")
WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
NOTICE_LIMIT = 12
PLAN_LIMIT = 15
# README 里的精简版上限：用户既要「加回主 README」又要过「别太长」，所以只放前几条，
# 截断行链到全量数据，节标题下再链到独立文档。改这两个数不影响 docs/ 下的全量版。
README_NOTICE_LIMIT = 5
README_PLAN_LIMIT = 8


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


def drop_reason(t: dict, now: datetime) -> str | None:
    """剔除原因键；None = 可做。判定顺序即优先级：非待完成 → 逾期。

    清单与「已剔除任务」文档共用这一个判定，避免两处口径漂移。
    """
    if t.get("state") != "pending":
        return "not_pending"
    dt = parse_datetime(t.get("deadline"))
    if dt and dt < now:
        return "overdue"
    return None


def actionable(tasks: list[dict], now: datetime) -> tuple[list[dict], dict]:
    """只留能做的：待完成、未逾期。已完成/已批阅的一律不进清单。"""
    kept: list[dict] = []
    dropped = {"not_pending": 0, "overdue": 0}
    for t in tasks:
        reason = drop_reason(t, now)
        if reason:
            dropped[reason] += 1
        else:
            kept.append(t)
    return kept, dropped


def course_key(d: dict) -> tuple[str, str]:
    """课程身份 = course_id + class_id。

    实测同一门英语课挂在两个教学班下：course_id 相同、class_id 不同。
    只按 course_id 归组会让两个班显示同一批任务。
    （具体 ID 不写进源码 —— 那也是可定位信息，见 AGENT.md 铁律 6。）
    """
    return (d.get("course_id") or "", d.get("class_id") or "")


def ordered_courses(courses: list[dict]) -> list[dict]:
    """活跃度（作业发表数量）降序，同数按待完成数、再按名字。

    任务清单与「已剔除任务」文档共用这一个排序，两处课程顺序不会漂。
    """
    return sorted(courses, key=lambda c: (-(c.get("task_count") or 0),
                                          -(c.get("pending_count") or 0),
                                          c.get("name") or ""))


def course_rows(courses: list[dict], kept: list[dict]) -> list[str]:
    """全部课程都列出来，按活跃度（作业发表数量）降序；没任务的写「无」。"""
    by_course: dict[tuple[str, str], list[dict]] = {}
    for t in kept:
        by_course.setdefault(course_key(t), []).append(t)
    ordered = ordered_courses(courses)
    lines = ["| 课程 | 作业数 | 待完成 | 任务（截止近 → 远） |",
             "| --- | --- | --- | --- |"]
    for c in ordered:
        items = sort_by_deadline(by_course.get(course_key(c), []))
        cells = []
        for t in items:
            dt = parse_datetime(t.get("deadline"))
            # 「抓不到截止」不等于「很久以后」。原来渲染成「无截止」并被算进「更远」，
            # 用户看到的是一条其实早已逾期的作业（审计 §1.1 / 用户报的 bug）。
            when = dt.strftime("%m-%d %H:%M") if dt else "⚠️截止未知"
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
    counts = {"today": 0, "week": 0, "later": 0, "unknown": 0}
    for t in kept:
        dt = parse_datetime(t.get("deadline"))
        if dt is None:
            # 单独一类，不能混进「更远」—— 那会让逾期作业看起来还很宽裕
            counts["unknown"] += 1
        elif dt.date() == today:
            counts["today"] += 1
        elif dt - now <= timedelta(days=7):
            counts["week"] += 1
        else:
            counts["later"] += 1

    note = ("> 已剔除：逾期 {} 项 · 非待完成 {} 项。"
            "课程按活跃度（作业发表数量）排序，共 {} 门。\n"
            "> 📄 [逐项查看已剔除任务与原因](docs/已剔除任务.md)\n").format(
                dropped["overdue"], dropped["not_pending"], len(courses))
    suffix = f"，其中 **{counts['today']} 项今天截止**" if counts["today"] else ""
    if counts["unknown"]:
        suffix += f"，**{counts['unknown']} 项截止未知（可能已逾期，请点进去确认）**"
    stats = {"pending": len(kept), "overdue": 0, **counts,
             "dropped": dropped, "courses": len(courses), "suffix": suffix}
    return body, note, stats


def reason_detail(reason: str, t: dict, now: datetime,
                  dt: datetime | None, st: datetime | None) -> str:
    """把剔除原因写成能看懂的一句话，带上具体天数/状态，而不是只给个分类名。"""
    if reason == "overdue" and dt:
        return f"**已逾期**：截止 {dt:%Y-%m-%d %H:%M}，已过 {(now - dt).days} 天，做不了了"
    return "**非待完成**：状态「{}」".format(esc(t.get("status") or t.get("state") or "未知"))


def excluded_row(t: dict, reason: str, now: datetime) -> str:
    dt = parse_datetime(t.get("deadline"))
    st = task_start(t)
    when = dt.strftime("%m-%d %H:%M") if dt else "⚠️截止未知"
    if dt and not t.get("deadline_exact"):
        when += " ≈"
    start = st.strftime("%m-%d %H:%M") if st else "未知"
    if st and not t.get("start_time"):
        start += "（首次发现）"
    return "| {} | {} | {} | {} | {} |".format(
        md_link(t.get("title"), t.get("url")),
        KIND_CN.get(t.get("kind"), t.get("kind") or ""),
        when, start, reason_detail(reason, t, now, dt, st))


def render_excluded(tasks: list[dict], now: datetime,
                    courses: list[dict] | None = None) -> tuple[str, int]:
    """被清单剔除的任务，**按课程分组** + 逐项原因。

    判定与 actionable() 共用 drop_reason()，课程顺序与任务清单共用 ordered_courses()，
    组内排序与任务清单共用 sort_by_deadline()。
    courses 省略时读 courses.json；selftest 传入固定表以断言分组与顺序。
    """
    by_reason = {k: 0 for k in REASON_ORDER}
    by_course: dict[tuple[str, str], list[dict]] = {}
    total = 0
    for t in tasks:
        reason = drop_reason(t, now)
        if not reason:
            continue
        total += 1
        by_reason[reason] += 1
        by_course.setdefault(course_key(t), []).append(t)
    if not total:
        return "_本次没有被剔除的任务。_\n", 0

    if courses is None:
        courses = load_list(DATA_DIR / "study" / "courses.json")
    ordered = ordered_courses(courses)
    known = {course_key(c) for c in ordered}
    # courses.json 里没有的课（理论上不该有）排在最后，按名字兜底，不能凭空丢掉
    extra = sorted((k for k in by_course if k not in known),
                   key=lambda k: (by_course[k][0].get("course") or ""))

    summary = " · ".join(f"{REASON_CN[k]} {by_reason[k]} 项"
                         for k in REASON_ORDER if by_reason[k])
    lines = [f"共 **{total}** 项：{summary}，分布在 **{len(by_course)}** 门课。", ""]
    header = ["| 任务 | 类型 | 截止 | 开始 | 剔除原因 |",
              "| --- | --- | --- | --- | --- |"]

    def section(name: str, items: list[dict]) -> None:
        lines.append(f"### {esc(name)}（{len(items)} 项）")
        lines.append("")
        lines.extend(header)
        for t in sort_by_deadline(items):
            lines.append(excluded_row(t, drop_reason(t, now), now))
        lines.append("")

    for c in ordered:
        items = by_course.get(course_key(c))
        if items:
            section(c.get("name") or "未命名课程", items)
    for k in extra:
        section(by_course[k][0].get("course") or "未命名课程", by_course[k])
    return "\n".join(lines).rstrip() + "\n", total


DATA_STALE_HOURS = 3


def study_age_hours(study: dict, now: datetime) -> float | None:
    """数据年龄（小时）。取 last_success，没有就退回 last_attempt；都没有返回 None。"""
    dt = parse_datetime(study.get("last_success") or study.get("last_attempt"))
    return (now - dt).total_seconds() / 3600 if dt else None


def render_alert(stats: dict, meta: dict, now: datetime | None = None) -> str:
    """顶部告警，**按优先级只出一个**。

    优先级（与 docs/工作原理.md 的表格必须一致，selftest 有守卫）：
      🔴 采集失败 → 🔴 截止未知 → 🟠 今天有截止 → 🟠 有逾期被移出
      → ⚠️ 部分降级 → ⚠️ 数据过期 → ⚠️ 未同步
    审计 §3.8：原来没有「数据已经 N 小时没更新」这一档，数据悄悄变旧无人发现。
    """
    now = now or now_bj()
    study = meta.get("study") or {}
    dropped = stats.get("dropped") or {}
    unknown = stats.get("unknown") or 0
    status = study.get("status") or ("ok" if study.get("ok") else "failed")

    if study and status == "failed":
        return ("> ## 🔴 学习通采集失败\n"
                f"> 原因：`{esc(study.get('reason') or '未知')}`。下方清单是上一次成功的数据，"
                "**可能已经过期**，别照着它安排今天。\n")
    if unknown:
        return (f"> ## 🔴 有 {unknown} 项待完成抓不到截止时间\n"
                "> 学习通没给倒计时、详情页也没解析出时间窗，**这些任务可能早已逾期**。"
                "请点进任务自己确认，不要当成「还很宽裕」。\n")
    if stats.get("today"):
        return (f"> ## 🟠 今天有 {stats['today']} 项截止\n"
                "> 就在下方清单里，别拖到明天。\n")
    if dropped.get("overdue"):
        return (f"> ## 🟠 有 {dropped['overdue']} 项已逾期，已移出清单\n"
                "> 做不了了，但值得知道错过了什么。逐项见 "
                "[已剔除任务](docs/已剔除任务.md)。\n")
    if study and status == "partial":
        why = "；".join(study.get("anomalies") or []) or "部分课程采集失败"
        return ("> ## ⚠️ 学习通数据不完整\n"
                f"> {esc(why)}。清单可能缺课或缺任务。\n")
    age = study_age_hours(study, now)
    if age is not None and age >= DATA_STALE_HOURS:
        return (f"> ## ⚠️ 学习通数据已 {int(age)} 小时未更新\n"
                f"> 超过 {DATA_STALE_HOURS} 小时阈值。可能是定时触发漏了或采集一直失败，"
                "见 [不足与风险](docs/不足与风险.md) §4.1。\n")
    if not study.get("ok"):
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
    detail = f"当前是 {info}，本周 {len(this_week)} 节课。"
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
def render_notices(limit: int = NOTICE_LIMIT, link_prefix: str = "../") -> tuple[str, int]:
    """返回 (正文, **符合条件的总条数**)。

    审计 §3.4：原来只返回被截断后的条数，用户看不到「还有 N 条没显示」，
    会以为公告就这么多。现在总数照实给，截断处在正文里写明。

    `link_prefix` 是截断行里 data/ 的相对前缀：产物在 docs/ 下用默认 `"../"`，
    渲染进根目录的 README 必须传 `""`，否则链接逃出仓库根（审计 §5.1 同型坑）。
    """
    items = [n for n in load_list(DATA_DIR / "notice" / "notices.json")
             if (n.get("score") or 0) >= 3]
    items.sort(key=lambda n: n.get("publish_date") or "", reverse=True)
    items.sort(key=lambda n: -(n.get("score") or 0))
    total = len(items)
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
    if total > len(items):
        lines.append("")
        lines.append(f"_另有 **{total - len(items)}** 条相关公告未显示（共 {total} 条），"
                     f"完整数据见 [notices.json]({link_prefix}data/notice/notices.json)。_")
    return "\n".join(lines) + "\n", total


def render_plans(limit: int = PLAN_LIMIT, link_prefix: str = "../") -> tuple[str, int]:
    """返回 (正文, **未完成计划总数**)。截断处写明还有多少条，见 §3.4。
    `link_prefix` 同 render_notices：README 在仓库根，必须传 `""`。"""
    items = [p for p in load_list(DATA_DIR / "diary" / "plans.json")
             if p.get("state") == "open"]
    items.sort(key=lambda p: (-(p.get("confidence") or 0), p.get("due_date") or "9999"))
    total = len(items)
    items = items[:limit]
    if not items:
        return "_暂无未完成计划。_\n", 0
    lines = ["| 计划 | 置信度 | 截止 | 来源 | 标签 |",
             "| --- | --- | --- | --- | --- |"]
    for p in items:
        lines.append("| {} | {} | {} | {} | {} |".format(
            esc(p.get("text")), f"{(p.get('confidence') or 0):.1f}",
            p.get("due_date") or "—", esc(p.get("source_date")) or "—",
            esc(" / ".join(p.get("tags") or [])) or "—"))
    if total > len(items):
        lines.append("")
        lines.append(f"_另有 **{total - len(items)}** 条未完成计划未显示（共 {total} 条），"
                     f"完整数据见 [plans.json]({link_prefix}data/diary/plans.json)。_")
    return "\n".join(lines) + "\n", total


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

    def study_state(s: dict) -> str:
        st = s.get("status") or ("ok" if s.get("ok") else "failed")
        return {"ok": "✅ 正常", "partial": "🟠 不完整"}.get(st, "❌ 失败")

    rows = [
        ("学习通作业", study_state(study),
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
    alert = render_alert(stats, meta, now)
    notices, notice_count = render_notices()
    plans, plan_count = render_plans()
    # README 内嵌的是精简版，且链接基准是仓库根（不是 docs/），所以单独渲染一次
    readme_notices, _ = render_notices(README_NOTICE_LIMIT, link_prefix="")
    readme_plans, _ = render_plans(README_PLAN_LIMIT, link_prefix="")

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
        "{{NOTICES}}": readme_notices,
        "{{PLANS}}": readme_plans,
        "{{README_NOTICE_LIMIT}}": str(README_NOTICE_LIMIT),
        "{{README_PLAN_LIMIT}}": str(README_PLAN_LIMIT),
    }
    for k, v in repl.items():
        text = text.replace(k, v)
    README.write_text(text, encoding="utf-8")

    # 独立文档仍是全量版（README 内嵌的是精简版），两边都要生成，缺模板让 finalize 报红
    ok_n = render_doc(NOTICE_TEMPLATE, NOTICE_DOC,
                      {"{{NOTICES}}": notices, "{{NOTICE_LIMIT}}": str(NOTICE_LIMIT)}, now)
    # 写回窗口以 sources.json 为准，默认值只在 diary_writeback 里定义一份，避免两处漂移
    from diary_writeback import DEFAULT_WINDOW
    wb_window = ((load_json(DATA_DIR / "sources.json") or {}).get("diary") or {}).get(
        "writeback_window") or DEFAULT_WINDOW
    ok_p = render_doc(PLANS_TEMPLATE, PLANS_DOC,
                      {"{{PLANS}}": plans, "{{PLAN_LIMIT}}": str(PLAN_LIMIT),
                       "{{WRITEBACK_WINDOW}}": wb_window}, now)
    # 被剔除的任务逐项留档，README 清单底部那句「已剔除：…」直接链到这里
    excluded_body, excluded_count = render_excluded(tasks, now)
    ok_e = render_doc(EXCLUDED_TEMPLATE, EXCLUDED_DOC,
                      {"{{EXCLUDED}}": excluded_body,
                       "{{EXCLUDED_COUNT}}": str(excluded_count)}, now)

    # 网页版数据（docs/ 是 GitHub Pages 目录，前端只能读到这里面的文件）
    # 前端自己按 state 分桶，所以这里必须只喂可做的任务，否则网页会显示出 README 已剔除的逾期项
    kept, _ = actionable(tasks, now)
    save_json_stable(DOCS_DATA_DIR / "tasks.json", {"updated_at": now.isoformat(timespec="seconds"),
                                                    "stats": {k: v for k, v in stats.items() if k != "suffix"},
                                                    "items": kept,
                                                    "courses": load_list(DATA_DIR / "study" / "courses.json")})
    save_json_stable(DOCS_DATA_DIR / "meta.json", {"updated_at": now.isoformat(timespec="seconds"),
                                                   "week": week_info.replace("**", ""), "stats": stats,
                                                   "sources": meta})
    for name in ("notices", "plans", "quotes"):
        src = {"notices": DATA_DIR / "notice" / "notices.json",
               "plans": DATA_DIR / "diary" / "plans.json",
               "quotes": DATA_DIR / "diary" / "quotes.json"}[name]
        payload = load_json(src) or {"items": []}
        if name in ("plans", "quotes"):
            # 网页版不需要知道这条计划出自私有仓库的哪个文件（审计 §2.2）
            payload = {**payload, "items": [{k: v for k, v in it.items() if k != "source_file"}
                                            for it in payload.get("items") or []]}
        save_json_stable(DOCS_DATA_DIR / f"{name}.json", payload)
    d = stats["dropped"]
    log(f"任务清单：{stats['pending']} 条可做 / {stats['courses']} 门课"
        f"（今天 {stats['today']} / 7天内 {stats['week']} / 更远 {stats['later']}"
        f" / 截止未知 {stats['unknown']}）")
    log(f"已剔除：逾期 {d['overdue']} / 非待完成 {d['not_pending']}")
    log(f"独立文档：公告 {notice_count} 条 / 潜在计划 {plan_count} 项 / 已剔除 {excluded_count} 项")
    log("README 已生成")
    return ok_n and ok_p and ok_e


if __name__ == "__main__":
    run()