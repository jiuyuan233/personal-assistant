"""潜在计划写回 Gitee 日记：T 日挖到的计划 → T+1 日日记里的复选框 → T+2 日复查。

用户要的效果：13 号挖出来的潜在任务写进 14 号的日记，用 `- [ ]` 让他勾；
15 号复查，勾了的算完成，没勾的滚进 15 号的日记。

2026-11-16 追加（用户要求「根据日记自己生成一段话」）：同一窗口里还写一段
**助手的记忆**（`assistant:memory` 区块，由 diary_collector.ensure_day_memory
用昨天的日记生成）——和计划区块并列、互不干扰，也只动自己的区块。

安全边界（这是往用户自己的日记仓库里写东西，必须守住）：
- **只在凌晨窗口内写**（默认北京时间 00:00–06:00，`sources.json` 的 `writeback_window` 可改）。
  用户白天在 Obsidian 里写日记，白天写回会和他的编辑撞车。窗口外只复查勾选、绝不提交。
  **窗口宽度必须 ≥ cron 间隔（20 分钟）**，否则窗口里可能一次都不触发（审计 §4.1）。
- 只动 `%% assistant:plans:start/end %%`、`%% assistant:memory:start/end %%`、
  `%% assistant:english:start/end %%` 三个区块内部，区块外的正文一个字节都不碰。
  （v1.7.0 前用 HTML 注释 `<!-- ... -->` 做 marker，Obsidian 里会裸露成「乱码」——
  用户 2026-11-16 报过后改成 Obsidian 阅读视图不显示的 `%%...%%` 语法 + callout 包裹；
  **旧格式仍能被识别**，读到旧区块会自动替换成新格式。）
- 计划区块已存在就按 pid 合并，保留用户已经勾上的 [x]，不重置；
  记忆/英语区块整段替换（每天各一条，不累积）
- pid 用行内 `%%pid:xxx%%` 注释（Obsidian 阅读视图不显示，能精确对回 plans.json；
  旧日记里的 `<!-- pid:xxx -->` 同样能读）
- 文件不存在才新建，新建时用用户日记的三段式骨架
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from dedup import load_list
from diary_collector import (BLOCK_END, BLOCK_START, EN_BLOCK_END,
                             EN_BLOCK_START, MEM_BLOCK_END, MEM_BLOCK_START,
                             OLD_BLOCK_END, OLD_BLOCK_START,
                             OLD_MEM_BLOCK_END, OLD_MEM_BLOCK_START,
                             PLANS_PATH, Gitee)
from utils import DATA_DIR, load_json, log, now_bj, safe_text, save_json

WB_PATH = DATA_DIR / "diary" / "writeback.json"
# v1.7.0：pid 同时认两种写法（新 %%…%% 与旧 HTML 注释），复查旧日记不丢勾选
PID_LINE_RE = re.compile(
    r"^\s*(?:>\s*)?[-*+]\s*\[([ xX])\]\s*(.+?)\s*(?:%%\s*pid:(\S+?)\s*%%|<!--\s*pid:(\S+?)\s*-->)\s*$", re.M)
WINDOW_RE = re.compile(r"\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*")
DEFAULT_MAX = 20
# 兕底值必须与 sources.json 一致。原来是 05:00，审计 §4.1 实测 cron 延迟可达 54 分钟，
# 04:07 那班会落到窗口外；放宽到 06:00 后窗口内有 18 次 tick 机会。
DEFAULT_WINDOW = "00:00-06:00"
NEW_FILE_TEMPLATE = "# 今日记录\n\n# 随笔 / 感想\n\n# 明日计划\n\n"


def parse_window(spec: str) -> tuple[int, int] | None:
    """"00:00-05:00" → (0, 300)，单位分钟。格式不对返回 None。"""
    m = WINDOW_RE.fullmatch(spec or "")
    if not m:
        return None
    a, b = int(m[1]) * 60 + int(m[2]), int(m[3]) * 60 + int(m[4])
    return (a, b) if 0 <= a < 1440 and 0 <= b < 1440 else None


def in_window(now: datetime, spec: str) -> bool:
    """含头不含尾，支持跨零点（"23:00-02:00"）。

    配置写错时**失败关闭**（返回 False 不写），宁可当天不写回也不能在用户编辑时提交。
    """
    w = parse_window(spec)
    if w is None:
        log(f"写回时间窗格式非法：{spec!r}，本次不写入（应为 HH:MM-HH:MM）", level="ERROR")
        return False
    cur, a, b = now.hour * 60 + now.minute, w[0], w[1]
    return a <= cur < b if a <= b else (cur >= a or cur < b)


def render_block(day: str, entries: list[tuple[str, str, bool]]) -> str:
    """entries = [(pid, 文本, 是否已完成)]。v1.7.0：Obsidian callout 包裹，无 HTML 标签。"""
    lines = [BLOCK_START, f"> [!todo]- 🤖 助手待办 · {day}"]
    for pid, text, done in entries:
        mark = "x" if done else " "
        lines.append(f"> - [{mark}] {safe_text(text, 100)} %%pid:{pid}%%")
    if not entries:
        lines.append("> - 今天没有要滚动的计划。哼，难得。")
    lines += ["> 勾选表示完成；没勾的明天会自动滚进新日记。区块外随便写，助手不碰。",
              BLOCK_END]
    return "\n".join(lines)


def render_memory_block(yesterday: str, text: str) -> str:
    """助手的记忆区块：一段自由文本（AI/离线生成），每天整段替换，不勾选、不滚动。"""
    body = "\n".join("> " + ln for ln in (text or "").strip().splitlines())
    lines = [MEM_BLOCK_START, f"> [!quote]- 💭 助手记得 · {yesterday}", body,
              "> 根据你 " + yesterday + " 的日记生成，当个念想，不一定对。",
              MEM_BLOCK_END]
    return "\n".join(lines)


def render_english_block(level: int, sentence: str) -> str:
    """英语角（v1.7.0）：每日一句 small talk，难度随用户回应程度提升。"""
    lines = [EN_BLOCK_START, f"> [!tip]- 🗣 每日英语 · Lv {level}",
              "> " + (sentence or "").strip(),
              "> 在明天的日记里用英语回它——回得动难度就升。",
              EN_BLOCK_END]
    return "\n".join(lines)


def _find_span(text: str, start_marker: str, end_marker: str,
              alt_start: str | None = None, alt_end: str | None = None) -> tuple[int, int] | None:
    """找区块的 (start, end) 字符位置。新格式优先，旧格式兑底（迁移期间共存）。"""
    text = text or ""
    start, end = text.find(start_marker), text.find(end_marker)
    if start >= 0 and end > start:
        return start, end + len(end_marker)
    if alt_start and alt_end:
        start, end = text.find(alt_start), text.find(alt_end)
        if start >= 0 and end > start:
            return start, end + len(alt_end)
    return None


def parse_block(text: str) -> list[tuple[str, bool]]:
    """从日记正文里读出助手区块的 (pid, 是否勾选)。新/旧格式都认，没有返回空列表。"""
    span = _find_span(text or "", BLOCK_START, BLOCK_END, OLD_BLOCK_START, OLD_BLOCK_END)
    if span is None:
        return []
    seg = (text or "")[span[0]:span[1]]
    out = []
    for m in PID_LINE_RE.finditer(seg):
        pid = m.group(3) or m.group(4)
        if pid:
            out.append((pid, m.group(1).lower() == "x"))
    return out


def _upsert_any(text: str, block: str, start_marker: str, end_marker: str,
                alt_start: str | None = None, alt_end: str | None = None) -> str:
    """区块通用替换/追加：新格式优先，旧格式命中也替换成新内容（自动迁移）。"""
    text = text or ""
    span = _find_span(text, start_marker, end_marker, alt_start, alt_end)
    if span is not None:
        return text[:span[0]] + block + text[span[1]:]
    sep = "" if not text or text.endswith("\n") else "\n"
    return text + sep + "\n" + block + "\n"


def upsert_block(text: str, block: str) -> str:
    """计划区块：整块替换或追加。区块外内容原样保留；旧 HTML 注释区块会迁移成新格式。"""
    return _upsert_any(text, block, BLOCK_START, BLOCK_END, OLD_BLOCK_START, OLD_BLOCK_END)


def upsert_memory(text: str, block: str) -> str:
    """记忆区块同款：整块替换或追加。与计划区块互不干扰。"""
    return _upsert_any(text, block, MEM_BLOCK_START, MEM_BLOCK_END,
                       OLD_MEM_BLOCK_START, OLD_MEM_BLOCK_END)


def upsert_english(text: str, block: str) -> str:
    """英语角同款：整块替换或追加（每天一句，不累积）。"""
    return _upsert_any(text, block, EN_BLOCK_START, EN_BLOCK_END)


def pick_plans(plans: list[dict], rolling: set[str], limit: int) -> list[dict]:
    """入选条件：state=open 且（**从未写回过** 或 昨天没勾完滚过来）。

    审计 §1.5：原来是 `first_seen == yesterday or pid in rolling`。first_seen 是
    采集日期，一条计划只在「被采到的次日」有资格；凌晨三次窗口全被挤掉 →
    这条计划再也不会进任何一天的日记。改成与日期无关的 written_once 标记后，
    错过窗口只是延后，不会永久丢。

    排序（审计 §3.4 ③）：昨天没勾完的优先 → 有截止日的近的优先 →
    **老计划先轮到**（原来按 due_date 排，多数计划没 due_date，实际按 first_seen
    倒序，新计划永远挤掉老计划）→ 用户自己的确定程度。
    """
    chosen = [p for p in plans
              if p.get("id") and p.get("state") == "open"
              and (not p.get("written_once") or p["id"] in rolling)]

    def rank(p: dict):
        return (0 if p.get("id") in rolling else 1,
                p.get("due_date") or "9999-12-31",
                p.get("first_seen") or "9999-12-31",
                -(p.get("confidence") or 0))

    return sorted(chosen, key=rank)[:limit]


def run(today: date | None = None, g: Gitee | None = None,
        plans: list[dict] | None = None, cfg: dict | None = None,
        now: datetime | None = None,
        memory: str | None = None,
        english: str | None = None,
        english_level: int = 1) -> dict:
    now = now or now_bj()
    today = today or now.date()
    cfg = cfg if cfg is not None else ((load_json(DATA_DIR / "sources.json") or {}).get("diary") or {})
    if g is None:
        log("写回需要已登录的 Gitee 客户端，跳过", level="WARN")
        return {"ok": False, "skipped": "no_client"}
    plans = plans if plans is not None else load_list(PLANS_PATH)

    root = cfg.get("path") or ""
    daily_dir = cfg.get("daily_dir") or "日"
    limit = int(cfg.get("max_writeback") or DEFAULT_MAX)
    iso = today.isoformat()
    path = f"{root}/{daily_dir}/{iso}.md"

    state = load_json(WB_PATH) or {}
    prev_date = state.get("last_date")
    prev_path = state.get("last_path")

    # 1) 复查上一次写进去的那天：用户勾了的 → 标完成
    done_ids: set[str] = set()
    rolling: set[str] = set()
    if prev_date and prev_path and prev_date < iso:
        prev_text, _ = g.read_file(prev_path)
        for pid, done in parse_block(prev_text):
            (done_ids if done else rolling).add(pid)
        log(f"复查 {prev_date}：勾选 {len(done_ids)} 条，未完成滚动 {len(rolling)} 条")

    # 2) 今天可能已经写过（一天跑多次），保留用户已勾的状态
    text, sha = g.read_file(path)
    existing = dict(parse_block(text))
    done_ids |= {pid for pid, done in existing.items() if done}
    rolling |= {pid for pid, done in existing.items() if not done}

    # 审计 §3.2：用户把昨天勾上的 [x] 又改回 [ ]，这条计划必须回到待办。
    # 复选框是权威来源，plans.json 里的 done 只是它的缓存。
    state_by_id = {p.get("id"): p.get("state") for p in plans}
    undone = {pid for pid in rolling if state_by_id.get(pid) == "done"}
    if undone:
        log(f"复查：{len(undone)} 条被取消勾选，复原为未完成")

    # 3) 凌晨窗口守卫：窗口外只复查（上面两步都是只读），绝不往用户仓库提交。
    #    不推进 last_date，等下一个凌晨窗口再写，滚动链条不会断。
    window = cfg.get("writeback_window") or DEFAULT_WINDOW
    if not in_window(now, window):
        log(f"写回：北京时间 {now:%H:%M} 不在凌晨窗口 {window} 内，只复查不写入")
        return {"ok": True, "skipped": "outside_window", "written": 0,
                "done": sorted(done_ids), "undone": sorted(undone), "path": path}

    # 4) 选出今天要写的。已勾选完成的不再写进新一天 —— 它们的 state 要等
    #    diary_collector 拿到本函数返回值之后才改成 done，这里必须自己排掉。
    candidates = [p for p in plans if p.get("id") not in done_ids]
    picked = pick_plans(candidates, rolling, limit)
    entries = [(p["id"], safe_text(p.get("text"), 100), p["id"] in done_ids) for p in picked]
    picked_ids = [p["id"] for p in picked]

    # memory = 昨天的日记生成的「助手记忆」段落（diary_collector.ensure_day_memory）。
    # english = 今日一句 small talk（diary_collector.ensure_daily_english）。
    # 两个都是 None 时区块干脆不写。

    if not entries and not text and not memory and not english:
        log(f"写回：{iso} 没有可写的计划，也不新建空日记")
        save_json(WB_PATH, {"updated_at": now_bj().isoformat(timespec="seconds"),
                            "last_date": prev_date, "last_path": prev_path})
        return {"ok": True, "written": 0, "done": sorted(done_ids),
                "undone": sorted(undone), "path": path}

    block = render_block(iso, entries)
    mem_block = (render_memory_block((today - timedelta(days=1)).isoformat(), memory)
                 if memory else "")
    en_block = render_english_block(english_level, english) if english else ""

    def _compose(base: str) -> str:
        t = upsert_block(base or NEW_FILE_TEMPLATE, block)
        if mem_block:
            t = upsert_memory(t, mem_block)
        if en_block:
            t = upsert_english(t, en_block)
        return t

    new_text = _compose(text)
    if new_text == text:
        log(f"写回：{iso} 内容无变化，跳过提交")
    else:
        msg = f"chore(diary): 助手区块 {iso}（待办 {len(entries)} 条"
        if memory:
            msg += "，含记忆"
        if english:
            msg += "，含英语角"
        msg += "）"
        ok = g.write_file(path, new_text, msg, sha=sha)
        if not ok and sha:
            # sha 被拒 = 用户/同步刚好改过这个文件。重读最新正文再合并一次，
            # 区块外以他的最新内容为准，只重试一次，不覆盖他的编辑。
            log(f"写回：{iso} 提交被拒（文件刚被改过），重读后重试一次", level="WARN")
            text, sha = g.read_file(path)
            new_text = _compose(text)
            if new_text == text:
                ok = True
            elif sha:
                ok = g.write_file(path, new_text, msg, sha=sha)
        if not ok:
            return {"ok": False, "written": 0, "done": sorted(done_ids),
                    "undone": sorted(undone), "path": path}
        log(f"写回：{iso} 写入 {len(entries)} 条 → {path}（{'更新' if sha else '新建'}）")

    save_json(WB_PATH, {"updated_at": now_bj().isoformat(timespec="seconds"),
                        "last_date": iso, "last_path": path,
                        "written": len(entries), "done": sorted(done_ids)})
    return {"ok": True, "written": len(entries), "done": sorted(done_ids),
            "undone": sorted(undone), "picked": picked_ids, "path": path}


if __name__ == "__main__":
    run()