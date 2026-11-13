"""飞书三时段推送：早 7 点今日任务、中午问好鼓励、晚上当日总结。

数据只来自日记（plans.json / quotes.json），**不带课表** —— 用户明确说课表提醒不用管。
文案由 AI 按 prompts.PERSONA（傲娇雌小鬼）生成，一言取自 https://v1.hitokoto.cn/。

关键约束：AI 失败或一言失败都必须降级推送，不能静默不发。
用户要的是每天固定有人跟他说话，宁可文案糙一点，也不能断。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import requests

from dedup import load_list
from utils import DATA_DIR, log, now_bj, safe_text

DIARY_DIR = DATA_DIR / "diary"
# workflow 的飞书步骤读这个文件；存在就发纯文本，不存在就走原来的运行状态卡片
MESSAGE_PATH = Path("/tmp/push-message.txt")
HITOKOTO_URL = "https://v1.hitokoto.cn/"
KINDS = ("morning", "noon", "night")
WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


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

    today = now_bj().date()
    facts = diary_facts(kind, today)
    hit = hitokoto()

    text = ""
    ai_ok = False
    try:
        from ai_client import AIClient
        from prompts import push_messages
        client = AIClient(timeout=90)
        text = safe_text(client.chat_text(push_messages(kind, facts, hit),
                                          temperature=0.8, max_tokens=2000), 1500).strip()
        ai_ok = bool(text)
    except Exception as e:
        log(f"推送文案 AI 生成失败：{type(e).__name__}", level="WARN")

    if not ai_ok:
        text = fallback_message(kind, facts, hit)
        log("AI 无输出，改用确定性降级文案", level="WARN")

    MESSAGE_PATH.write_text(text, encoding="utf-8")
    log(f"[push] {kind} 文案 {len(text)} 字，一言{'有' if hit else '无'}，"
        f"文案来源：{'AI' if ai_ok else '降级'}")
    return {"ok": True, "kind": kind, "chars": len(text),
            "hitokoto": bool(hit), "ai": ai_ok}


if __name__ == "__main__":
    import sys
    run(sys.argv[1] if len(sys.argv) > 1 else "morning")