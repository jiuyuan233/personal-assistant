"""本周课表 SVG。

7 列（周一~周日）× 11 行（节次）的网格，课程块按 slot_start~slot_end 跨行。
纯字符串拼接，不依赖任何绘图库 —— Actions 上装 cairo/pillow 既慢又容易失败。
"""
from __future__ import annotations

import hashlib
from datetime import date, timedelta

from utils import DATA_DIR, DOCS_DIR, load_json, log, now_bj, save_json, today_bj

OUT = DOCS_DIR / "week.svg"

WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
SLOTS = [
    (1, "08:00"), (2, "08:50"), (3, "10:05"), (4, "10:55"), (5, "14:00"),
    (6, "14:50"), (7, "16:05"), (8, "16:55"), (9, "19:00"), (10, "19:50"), (11, "20:40"),
]
GUTTER = 62
COL_W = 132
ROW_H = 54
HEAD_H = 46
PAD = 12
WIDTH = GUTTER + COL_W * 7 + PAD * 2
HEIGHT = HEAD_H + ROW_H * len(SLOTS) + PAD * 2 + 26

PALETTE = ["#4C6EF5", "#0CA678", "#E8590C", "#9C36B5", "#1098AD",
           "#D6336C", "#5C940D", "#E67700", "#3B5BDB", "#0B7285"]

CLASS_MINUTES = 45


def time_y(minutes: float, body_top: float) -> float:
    """当天第几分钟 → SVG 的 y 坐标。

    每节课 45 分钟正好占满一行；课间线停在两行交界，不拉伸 ——
    否则大课间（08:50→10:05 有 75 分钟）会让线看着像还在上课。
    早于第一节课钳到顶，晚于最后一节钳到底。
    """
    anchors: list[tuple[float, float]] = []
    for r, (_, hhmm) in enumerate(SLOTS):
        h, m = map(int, hhmm.split(":"))
        start = h * 60 + m
        y = body_top + r * ROW_H
        anchors.append((start, y))
        anchors.append((start + CLASS_MINUTES, y + ROW_H))
    if minutes <= anchors[0][0]:
        return anchors[0][1]
    if minutes >= anchors[-1][0]:
        return anchors[-1][1]
    for (t0, y0), (t1, y1) in zip(anchors, anchors[1:]):
        if t0 <= minutes <= t1:
            return y0 if t1 == t0 else y0 + (y1 - y0) * (minutes - t0) / (t1 - t0)
    return anchors[-1][1]


def esc(s: str) -> str:
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def color_of(name: str) -> str:
    h = int(hashlib.md5((name or "").encode("utf-8")).hexdigest()[:6], 16)
    return PALETTE[h % len(PALETTE)]


def wrap(text: str, per_line: int) -> list[str]:
    """中文按字符数硬折行，SVG 不会自动换行。"""
    text = str(text or "")
    return [text[i:i + per_line] for i in range(0, len(text), per_line)] or [""]


def run(today: date | None = None) -> bool:
    today = today or today_bj()
    data = load_json(DATA_DIR / "schedule" / "this_week.json") or {}
    events = data.get("items") or []
    week = data.get("current_week")
    now = now_bj()
    today_col = today.weekday()

    p: list[str] = []
    p.append(f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" '
             f'viewBox="0 0 {WIDTH} {HEIGHT}" font-family="-apple-system,BlinkMacSystemFont,'
             f'\'Segoe UI\',\'PingFang SC\',\'Microsoft YaHei\',sans-serif">')
    p.append(f'<rect width="{WIDTH}" height="{HEIGHT}" fill="#FFFFFF"/>')
    title = f"第 {week} 周课表" if week else "本周课表"
    p.append(f'<text x="{PAD}" y="{PAD + 14}" font-size="15" font-weight="600" fill="#1A1A1A">'
             f'{esc(title)} · {today.isoformat()}</text>')

    top = PAD + 26
    # 表头
    for i, cn in enumerate(WEEKDAY_CN):
        x = PAD + GUTTER + i * COL_W
        is_today = i == today_col
        if is_today:
            p.append(f'<rect x="{x}" y="{top}" width="{COL_W}" height="{HEAD_H}" fill="#EDF2FF"/>')
        p.append(f'<text x="{x + COL_W / 2}" y="{top + 20}" font-size="13" '
                 f'font-weight="{600 if is_today else 400}" text-anchor="middle" '
                 f'fill="{"#3B5BDB" if is_today else "#495057"}">{cn}</text>')
        d = today - timedelta(days=today_col - i)
        p.append(f'<text x="{x + COL_W / 2}" y="{top + 36}" font-size="10" text-anchor="middle" '
                 f'fill="#ADB5BD">{d.strftime("%m-%d")}</text>')

    body_top = top + HEAD_H
    # 网格
    for r, (num, hhmm) in enumerate(SLOTS):
        y = body_top + r * ROW_H
        p.append(f'<rect x="{PAD}" y="{y}" width="{GUTTER}" height="{ROW_H}" fill="#F8F9FA"/>')
        p.append(f'<text x="{PAD + GUTTER / 2}" y="{y + 22}" font-size="12" text-anchor="middle" '
                 f'fill="#495057">{num}</text>')
        p.append(f'<text x="{PAD + GUTTER / 2}" y="{y + 38}" font-size="9" text-anchor="middle" '
                 f'fill="#ADB5BD">{hhmm}</text>')
        p.append(f'<line x1="{PAD}" y1="{y}" x2="{WIDTH - PAD}" y2="{y}" '
                 f'stroke="#E9ECEF" stroke-width="1"/>')
    for i in range(8):
        x = PAD + GUTTER + i * COL_W
        p.append(f'<line x1="{x}" y1="{top}" x2="{x}" y2="{body_top + ROW_H * len(SLOTS)}" '
                 f'stroke="#E9ECEF" stroke-width="1"/>')
    p.append(f'<line x1="{PAD}" y1="{body_top + ROW_H * len(SLOTS)}" x2="{WIDTH - PAD}" '
             f'y2="{body_top + ROW_H * len(SLOTS)}" stroke="#E9ECEF" stroke-width="1"/>')

    # 课程块
    for e in events:
        col = e.get("weekday", 0)
        s1 = max(1, int(e.get("slot_start") or 1))
        s2 = min(len(SLOTS), int(e.get("slot_end") or s1))
        x = PAD + GUTTER + col * COL_W + 3
        y = body_top + (s1 - 1) * ROW_H + 3
        w = COL_W - 6
        h = (s2 - s1 + 1) * ROW_H - 6
        c = color_of(e.get("course"))
        p.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="6" fill="{c}" opacity="0.92"/>')
        lines = wrap(e.get("course"), 9)[:3]
        if e.get("topic"):
            lines += wrap(e["topic"], 9)[:1]
        ty = y + 16
        for ln in lines:
            p.append(f'<text x="{x + 7}" y="{ty}" font-size="11" fill="#FFFFFF">{esc(ln)}</text>')
            ty += 13
        room = e.get("room") or ""
        if room and ty < y + h - 4:
            p.append(f'<text x="{x + 7}" y="{ty}" font-size="9" fill="#FFFFFF" opacity="0.85">'
                     f'{esc(wrap(room, 12)[0])}</text>')

    if not events:
        p.append(f'<text x="{WIDTH / 2}" y="{body_top + 120}" font-size="14" text-anchor="middle" '
                 f'fill="#ADB5BD">本周没有课</text>')

    # 当前时间线（日历样式），画在课程块之上才看得见。
    # SVG 是静态的，finalize 每 20 分钟重画一次，所以这条线是「20 分钟精度」；
    # 网页端另有一条 JS 实时移动的同款线，几何参数从 schedule_geo.json 读。
    minutes = now.hour * 60 + now.minute
    ny = time_y(minutes, body_top)
    nx = PAD + GUTTER + today_col * COL_W
    p.append(f'<line x1="{nx}" y1="{ny:.1f}" x2="{nx + COL_W}" y2="{ny:.1f}" '
             f'stroke="#FA5252" stroke-width="2"/>')
    p.append(f'<circle cx="{nx}" cy="{ny:.1f}" r="4" fill="#FA5252"/>')
    p.append(f'<rect x="{PAD + 2}" y="{ny - 9:.1f}" width="{GUTTER - 6}" height="16" rx="3" fill="#FA5252"/>')
    p.append(f'<text x="{PAD + GUTTER / 2}" y="{ny + 3:.1f}" font-size="10" text-anchor="middle" '
             f'fill="#FFFFFF">{now.strftime("%H:%M")}</text>')

    p.append(f'<text x="{PAD}" y="{HEIGHT - 8}" font-size="9" fill="#CED4DA">'
             f'自动生成于 {now.strftime("%Y-%m-%d %H:%M")} (UTC+8) · 共 {len(events)} 节课</text>')
    p.append("</svg>")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(p), encoding="utf-8")

    # 前端实时线要用同一套几何，避免两处各算一遍算歪
    save_json(DOCS_DIR / "data" / "schedule_geo.json", {
        "width": WIDTH, "height": HEIGHT, "pad": PAD, "gutter": GUTTER,
        "col_w": COL_W, "row_h": ROW_H, "body_top": body_top,
        "today_col": today_col, "date": today.isoformat(),
        "class_minutes": CLASS_MINUTES,
        "slots": [hhmm for _, hhmm in SLOTS],
        "generated_at": now.isoformat(timespec="seconds"),
    })
    log(f"本周课表 SVG 已生成：{len(events)} 节课，时间线 {now.strftime('%H:%M')}")
    return True


if __name__ == "__main__":
    run()