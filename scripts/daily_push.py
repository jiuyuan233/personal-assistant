"""飞书三时段推送：早 7 点作业+公告+一言+二次元图、中午问好鼓励、晚上日记总结。

用户的要求（2026-11-14 第二次细化）：
- 只传达重要信息，常规早中晚 3 条；**正常运行不汇报**（降噪在 notify_feishu.js 做）。
- 早上：待完成的作业 + 值得关注的公告 + 一言（https://v1.hitokoto.cn/）+ 二次元图 + 中文问好。
- 晚上：对当天日记做总结。中午：问好 + 鼓励。
- **不带课表**（「不用给我发要上什么课」）。
- 内容不限于他点名的，但必须包含他点名的。

结构上把「人话」和「清单」分开，这是本轮最重要的设计决定：
  AI 只写人设 prose（问好/调侃/鼓励/总结），**清单由程序确定性拼在 prose 后面**。
整条消息交给 AI 复述事实时它会漏条目、改日期；用户要的是「作业和公告一条不漏」，
所以清单不经过 AI。AI 挂了也有降级 prose，清单照样在，消息不会断。

数据口径复用 render_readme.actionable / dedup.sort_by_deadline，
和 README 任务清单同一套判定，避免「飞书说能做、README 说已逾期」这种两处漂移。
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

from dedup import load_list, sort_by_deadline
from utils import DATA_DIR, log, now_bj, parse_datetime, safe_text

DIARY_DIR = DATA_DIR / "diary"
# workflow 的飞书步骤读这个文件；存在就发推送，不存在就走运行状态卡片（且只报重要信息）。
# PUSH_MESSAGE_PATH 只为本地 Windows 调试留的口子，线上不设，默认仍是 /tmp。
MESSAGE_PATH = Path(os.environ.get("PUSH_MESSAGE_PATH", "/tmp/push-message.txt"))
HITOKOTO_URL = "https://v1.hitokoto.cn/"
KINDS = ("morning", "noon", "night")
WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
MORNING_TASK_LIMIT = 6
MORNING_NOTICE_LIMIT = 3
NOTICE_MIN_SCORE = 3


def hitokoto() -> str:
    """取一条一言。失败返回空串，让推送照常发出去。"""
    try:
        j = requests.get(HITOKOTO_URL, timeout=15).json()
        text = safe_text(j.get("hitokoto"), 120).strip()
        src = safe_text(j.get("from"), 40).strip()
        if not text:
            return ""
        return f"{text}（{src}）" if src else text
    except Exception as e:
        log(f"一言获取失败：{type(e).__name__}", level="WARN")
        return ""


def _when(t: dict) -> str:
    dt = parse_datetime(t.get("deadline"))
    if not dt:
        return "⚠️截止未知"
    s = dt.strftime("%m-%d %H:%M")
    return s if t.get("deadline_exact") else s + "≈"


def study_facts(now: datetime) -> tuple[str, list[dict]]:
    """待完成作业。与 README 任务清单共用 actionable()，口径不会漂。

    链接紧跟在每条作业下面（不是集中堆在末尾）：飞书里一眼能对上是哪条作业的入口。
    """
    import render_readme as rr

    tasks = load_list(DATA_DIR / "study" / "tasks.json")
    kept, _ = rr.actionable(tasks, now)
    kept = sort_by_deadline(kept)
    lines = [f"待完成作业 {len(kept)} 条（只算能做的：待完成且未逾期）。"]
    for t in kept[:MORNING_TASK_LIMIT]:
        lines.append(f"- {t.get('course')} · {t.get('title')} · 截止 {_when(t)}")
        if t.get("url"):
            lines.append(f"  ↳ {t['url']}")
    if len(kept) > MORNING_TASK_LIMIT:
        lines.append(f"（还有 {len(kept) - MORNING_TASK_LIMIT} 条，全量在 README 任务清单）")
    return "\n".join(lines), kept


def notice_facts() -> tuple[str, list[dict]]:
    """值得关注的公告：相关度 >= 3，按分降序。与 README 公告节同一门槛。"""
    items = load_list(DATA_DIR / "notice" / "notices.json")
    hot = sorted([n for n in items if (n.get("score") or 0) >= NOTICE_MIN_SCORE],
                 key=lambda n: -(n.get("score") or 0))
    lines = [f"值得关注的公告 {len(hot)} 条（相关度 {NOTICE_MIN_SCORE} 分以上）。"]
    for n in hot[:MORNING_NOTICE_LIMIT]:
        due = f"截止 {n['deadline']}" if n.get("deadline") else "无明确截止"
        why = n.get("reason") or n.get("category") or ""
        lines.append(f"- {n.get('title')} · {n.get('source')} · {due}"
                     + (f" · {why}" if why else ""))
        if n.get("url"):
            lines.append(f"  ↳ {n['url']}")
    return "\n".join(lines), hot


def diary_facts(kind: str, today: date) -> str:
    """把日记数据整理成确定性事实清单。AI 只准用这里给出的信息，不许编造。

    ⚠️ 2026-11-14 用户要求：「这个随口写的不要发到飞书里，这个我有其他解决方案」——
    潜在计划（plans.json）不再播报进任何飞书消息。计划数据仍进 README 任务区与
    docs/潜在计划.md（网页/仓库里自己看），日记写回闭环也不受影响。
    这里只剩「今天的名言」一类内容。
    """
    quotes = load_list(DIARY_DIR / "quotes.json")
    iso = today.isoformat()
    quotes_today = [q for q in quotes if q.get("first_seen") == iso]

    lines = [f"今天是 {iso} {WEEKDAY_CN[today.weekday()]}。"]
    if quotes_today:
        lines.append("今天从你日记里摘出来的话：")
        for q in quotes_today[:3]:
            lines.append(f"- 「{q.get('text')}」")
            if q.get("comment"):
                lines.append(f"  点评：{q['comment']}")
    return "\n".join(lines)


def _task_line(t: dict) -> list[str]:
    """一条作业两行：条目 + 可点链接（与 study_facts 的排版一致）。"""
    out = [f"- {t.get('course')} · {t.get('title')} · 截止 {_when(t)}"]
    if t.get("url"):
        out.append(f"  ↳ {t['url']}")
    return out


def _actionable_sorted(now: datetime) -> list[dict]:
    """能做的作业（待完成且未逾期），按截止近→远。与 README 任务清单同口径。"""
    import render_readme as rr
    tasks = load_list(DATA_DIR / "study" / "tasks.json")
    kept, _ = rr.actionable(tasks, now)
    return sort_by_deadline(kept)


def info_block(kind: str, now: datetime, hit: str) -> str:
    """确定性清单，拼在 AI prose 后面。**不经过 AI**，保证用户点名的内容一条不漏。

    午/晚的清单在 v1.5.6 加厚（用户 2026-11-14：「中午和晚上的信息太少了」）：
    午 = 今日新发作业 + 7 天内截止（≤3）+ 今日新公告（≤2）；
    晚 = 48h 内截止未交（≤3）+ 本周作业盘点 + 明日截止预告（≤2）。
    数据全部来自仓库现成 JSON，零新增数据源；仍然不碰课表（铁律）。
    """
    parts: list[str] = []
    today_iso = now.date().isoformat()
    if kind == "morning":
        sf, _ = study_facts(now)
        parts.append("📚 待完成作业\n" + sf)
        nf, _ = notice_facts()
        parts.append("📢 值得关注的公告\n" + nf)
    elif kind == "noon":
        kept = _actionable_sorted(now)
        tasks = load_list(DATA_DIR / "study" / "tasks.json")
        fresh = [t for t in tasks
                 if t.get("first_seen") == today_iso and t.get("state") == "pending"]
        if fresh:
            lines = [f"🆕 今天新发的作业 {len(fresh)} 条："]
            for t in fresh[:3]:
                lines += _task_line(t)
            if len(fresh) > 3:
                lines.append(f"（还有 {len(fresh) - 3} 条，全量在 README）")
            parts.append("\n".join(lines))
        week = [t for t in kept
                if t.get("deadline")
                and (parse_datetime(t["deadline"]) or now) <= now + timedelta(days=7)]
        if week:
            lines = [f"⏳ 7 天内截止 {len(week)} 条："]
            for t in week[:3]:
                lines += _task_line(t)
            if len(week) > 3:
                lines.append(f"（还有 {len(week) - 3} 条，全量在 README）")
            parts.append("\n".join(lines))
        notices = load_list(DATA_DIR / "notice" / "notices.json")
        hot_new = [n for n in notices
                   if n.get("first_seen") == today_iso
                   and (n.get("score") or 0) >= NOTICE_MIN_SCORE]
        if hot_new:
            hot_new.sort(key=lambda n: -(n.get("score") or 0))
            lines = ["📢 今天新出的值得关注公告："]
            for n in hot_new[:2]:
                lines.append(f"- {n.get('title')}")
                if n.get("url"):
                    lines.append(f"  ↳ {n['url']}")
            parts.append("\n".join(lines))
    else:
        kept = _actionable_sorted(now)
        soon = [t for t in kept
                if t.get("deadline")
                and (parse_datetime(t["deadline"]) or now) <= now + timedelta(hours=48)]
        if soon:
            lines = [f"🔥 48 小时内截止还没交的 {len(soon)} 条："]
            for t in soon[:3]:
                lines += _task_line(t)
            if len(soon) > 3:
                lines.append(f"（还有 {len(soon) - 3} 条，全量在 README）")
            parts.append("\n".join(lines))
        tasks = load_list(DATA_DIR / "study" / "tasks.json")
        monday_iso = (now.date() - timedelta(days=now.date().weekday())).isoformat()
        this_week = [t for t in tasks if (t.get("first_seen") or "") >= monday_iso]
        done_w = [t for t in this_week if t.get("state") != "pending"]
        open_w = [t for t in this_week if t.get("state") == "pending"]
        parts.append(f"📊 本周新发作业 {len(this_week)} 条：已交/处理 {len(done_w)} 条，"
                     f"还压着 {len(open_w)} 条。")
        tomorrow_iso = (now.date() + timedelta(days=1)).isoformat()
        due_tmr = [t for t in kept
                   if t.get("deadline")
                   and (parse_datetime(t["deadline"]) or now).date().isoformat() == tomorrow_iso]
        if due_tmr:
            lines = [f"📅 明天截止 {len(due_tmr)} 条："]
            for t in due_tmr[:2]:
                lines.append(f"- {t.get('course')} · {t.get('title')}")
            parts.append("\n".join(lines))
    if hit:
        parts.append(f"💬 今日一言：{hit}")
    return "\n\n".join(parts)


def fallback_message(kind: str, facts: str, hit: str) -> str:
    """AI 挂了也要发。语气保持人设，但内容全部来自确定性事实。"""
    head = {
        "morning": "早。哼，别以为我不说话你就能赖床。今天的事都在下面，自己看。",
        "noon": "中午了。吃饭没？别又拿零食糊弄自己。下午该动的就动，别等我晚上再骂你。",
        "night": "今天就这样了。做完的算你的，没做的明天别想赖掉。啧，我可不是在担心你。",
    }[kind]
    parts = [head, "", facts]
    if hit:
        parts += ["", f"今日一言：{hit}"]
    return "\n".join(parts)


def run(kind: str) -> dict:
    if kind not in KINDS:
        log(f"未知推送时段：{kind}", level="ERROR")
        return {"ok": False}

    now = now_bj()
    today = now.date()
    facts = diary_facts(kind, today)
    hit = hitokoto()

    # 三时段都带图（用户 2026-11-14 推翻了「只有早上带图」的旧约定：
    # 「发送格式全部都按照早上的格式发，图片改成早中晚都发」）。
    # 取图失败只 WARN 不阻断推送；JS 侧有图 JSON 就发卡片，格式自动与早报一致。
    image = None
    try:
        import anime_image
        image = anime_image.run()
    except Exception as e:
        log(f"取图异常：{type(e).__name__}，本次不带图", level="WARN")

    prose = ""
    ai_ok = False
    try:
        from ai_client import AIClient
        from prompts import push_messages
        client = AIClient(timeout=90)
        prose = safe_text(client.chat_text(push_messages(kind, facts, hit),
                                           temperature=0.8, max_tokens=2000), 600).strip()
        ai_ok = bool(prose)
    except Exception as e:
        log(f"推送文案 AI 生成失败：{type(e).__name__}", level="WARN")

    if not ai_ok:
        # 一言不在这里给：info_block() 末尾已经拼了「💬 今日一言」，
        # 再传一次会让降级消息里出现两条一模一样的话。
        prose = fallback_message(kind, facts, "")
        log("AI 无输出，改用确定性降级文案", level="WARN")

    block = info_block(kind, now, hit)
    text = prose + ("\n\n" + block if block else "")
    MESSAGE_PATH.write_text(text, encoding="utf-8")
    log(f"[push] {kind} 文案 {len(text)} 字，一言{'有' if hit else '无'}，"
        f"图{'有' if image else '无'}，文案来源：{'AI' if ai_ok else '降级'}")
    return {"ok": True, "kind": kind, "chars": len(text),
            "hitokoto": bool(hit), "ai": ai_ok, "image": bool(image)}


if __name__ == "__main__":
    import sys
    run(sys.argv[1] if len(sys.argv) > 1 else "morning")