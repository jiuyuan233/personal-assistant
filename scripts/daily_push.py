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
from datetime import date, datetime
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


def _plan_line(p: dict) -> str:
    due = p.get("due_date")
    tags = "/".join(p.get("tags") or [])
    tail = f"［{due}］" if due else "［无截止］"
    if tags:
        tail += f"（{tags}）"
    return f"- {p.get('text')}{tail}"


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
    """把日记数据整理成确定性事实清单。AI 只准用这里给出的信息，不许编造。"""
    plans = load_list(DIARY_DIR / "plans.json")
    quotes = load_list(DIARY_DIR / "quotes.json")
    iso = today.isoformat()

    opened = [p for p in plans if p.get("state") == "open"]
    done = [p for p in plans if p.get("state") == "done"]
    due_today = [p for p in opened if p.get("due_date") == iso]
    overdue = sorted([p for p in opened if p.get("due_date") and p["due_date"] < iso],
                     key=lambda p: p["due_date"])
    new_today = [p for p in plans if p.get("first_seen") == iso]
    quotes_today = [q for q in quotes if q.get("first_seen") == iso]
    no_due = sorted([p for p in opened if not p.get("due_date")],
                    key=lambda p: -(p.get("confidence") or 0))

    lines = [f"今天是 {iso} {WEEKDAY_CN[today.weekday()]}。",
             f"日记里未完成计划 {len(opened)} 条，已完成 {len(done)} 条。"]

    if kind == "morning":
        lines.append(f"今天到期：{len(due_today)} 条。")
        lines += [_plan_line(p) for p in due_today[:6]]
        lines.append(f"已逾期还没做：{len(overdue)} 条。")
        lines += [_plan_line(p) for p in overdue[:5]]
        lines.append("没写截止日、但你自己提过想做的（按你自己的确定程度排）：")
        lines += [_plan_line(p) for p in no_due[:5]]
    elif kind == "noon":
        lines.append(f"今天到期：{len(due_today)} 条，已逾期还没做：{len(overdue)} 条。")
        pick = (due_today or overdue or no_due)[:2]
        if pick:
            lines.append("下午最该动的一件事：")
            lines += [_plan_line(p) for p in pick]
    else:
        lines.append(f"今天日记里新冒出来的计划：{len(new_today)} 条。")
        lines += [_plan_line(p) for p in new_today[:6]]
        lines.append(f"今天到期：{len(due_today)} 条，其中仍未完成 "
                     f"{sum(1 for p in due_today if p.get('state') == 'open')} 条。")
        lines.append(f"已逾期还没做：{len(overdue)} 条。")
        lines += [_plan_line(p) for p in overdue[:5]]
        if quotes_today:
            lines.append("今天从你日记里摘出来的话：")
            for q in quotes_today[:3]:
                lines.append(f"- 「{q.get('text')}」")
                if q.get("comment"):
                    lines.append(f"  点评：{q['comment']}")

    if not opened and not new_today and not quotes_today:
        lines.append("日记里今天没有任何可播报的内容。")
    return "\n".join(lines)


def info_block(kind: str, now: datetime, hit: str) -> str:
    """确定性清单，拼在 AI prose 后面。**不经过 AI**，保证用户点名的内容一条不漏。"""
    parts: list[str] = []
    if kind == "morning":
        sf, _ = study_facts(now)
        parts.append("📚 待完成作业\n" + sf)
        nf, _ = notice_facts()
        parts.append("📢 值得关注的公告\n" + nf)
    elif kind == "noon":
        _, kept = study_facts(now)
        if kept:
            t = kept[0]
            parts.append(f"下午最该动的一件事：{t.get('course')} · {t.get('title')}"
                         f"（截止 {_when(t)}）")
    else:
        tasks = load_list(DATA_DIR / "study" / "tasks.json")
        iso = now.date().isoformat()
        due_open = [t for t in tasks
                    if t.get("state") == "pending"
                    and (parse_datetime(t.get("deadline")) or now).date().isoformat() == iso]
        parts.append(f"今天到期的作业里还有 {len(due_open)} 条没做完。")
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