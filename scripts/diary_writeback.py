"""潜在计划写回 Gitee 日记：T 日挖到的计划 → T+1 日日记里的复选框 → T+2 日复查。

用户要的效果：13 号挖出来的潜在任务写进 14 号的日记，用 `- [ ]` 让他勾；
15 号复查，勾了的算完成，没勾的滚进 15 号的日记。

安全边界（这是往用户自己的日记仓库里写东西，必须守住）：
- 只动 `<!-- assistant:plans:start/end -->` 之间的内容，区块外的正文一个字节都不碰
- 区块已存在就按 pid 合并，保留用户已经勾上的 [x]，不重置
- 每行尾部带 `<!-- pid:xxx -->`，Obsidian 预览里不显示，但能精确对回 plans.json
- 文件不存在才新建，新建时用用户日记的三段式骨架
"""
from __future__ import annotations

import re
from datetime import date, timedelta

from dedup import load_list
from diary_collector import BLOCK_END, BLOCK_START, PLANS_PATH, Gitee, strip_assistant_block
from utils import DATA_DIR, load_json, log, now_bj, safe_text, save_json, today_bj

WB_PATH = DATA_DIR / "diary" / "writeback.json"
PID_LINE_RE = re.compile(r"^\s*[-*+]\s*\[([ xX])\]\s*(.+?)\s*<!--\s*pid:(\S+?)\s*-->\s*$", re.M)
DEFAULT_MAX = 20
NEW_FILE_TEMPLATE = "# 今日记录\n\n# 随笔 / 感想\n\n# 明日计划\n\n"


def render_block(day: str, entries: list[tuple[str, str, bool]]) -> str:
    """entries = [(pid, 文本, 是否已完成)]"""
    lines = [BLOCK_START, f"## 🤖 助手待办 · {day}", ""]
    for pid, text, done in entries:
        mark = "x" if done else " "
        lines.append(f"- [{mark}] {text} <!-- pid:{pid} -->")
    if not entries:
        lines.append("- 今天没有要滚动的计划。哼，难得。")
    lines += ["", "<sub>勾选表示完成；没勾的明天会自动滚进新日记。区块外随便写，助手不碰。</sub>",
              BLOCK_END]
    return "\n".join(lines)


def parse_block(text: str) -> list[tuple[str, bool]]:
    """从日记正文里读出助手区块的 (pid, 是否勾选)。没有区块返回空列表。"""
    start = (text or "").find(BLOCK_START)
    end = (text or "").find(BLOCK_END)
    if start < 0 or end < start:
        return []
    return [(m.group(3), m.group(1).lower() == "x")
            for m in PID_LINE_RE.finditer(text[start:end])]


def upsert_block(text: str, block: str) -> str:
    """有区块就整块替换，没有就追加到末尾。区块外的内容原样保留。"""
    text = text or ""
    start = text.find(BLOCK_START)
    end = text.find(BLOCK_END)
    if start >= 0 and end > start:
        return text[:start] + block + text[end + len(BLOCK_END):]
    sep = "" if not text or text.endswith("\n") else "\n"
    return text + sep + "\n" + block + "\n"


def pick_plans(plans: list[dict], yesterday: str, rolling: set[str],
               limit: int) -> list[dict]:
    """昨天新挖到的 + 上次没勾完滚过来的，都只要 state=open 的。"""
    by_id = {p.get("id"): p for p in plans if p.get("id")}
    chosen: dict[str, dict] = {}
    for p in plans:
        pid = p.get("id")
        if not pid or p.get("state") != "open":
            continue
        if p.get("first_seen") == yesterday or pid in rolling:
            chosen[pid] = p
    # 有截止日的排前面（近的优先），没截止日的按用户自己的确定程度排
    ordered = sorted(chosen.values(),
                     key=lambda p: (p.get("due_date") or "9999-12-31",
                                    -(p.get("confidence") or 0)))
    return ordered[:limit]


def run(today: date | None = None, g: Gitee | None = None,
        plans: list[dict] | None = None, cfg: dict | None = None) -> dict:
    today = today or today_bj()
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

    # 3) 选出今天要写的
    yesterday = (today - timedelta(days=1)).isoformat()
    picked = pick_plans(plans, yesterday, rolling, limit)
    entries = [(p["id"], safe_text(p.get("text"), 100), p["id"] in done_ids) for p in picked]

    if not entries and not text:
        log(f"写回：{iso} 没有可写的计划，也不新建空日记")
        save_json(WB_PATH, {"updated_at": now_bj().isoformat(timespec="seconds"),
                            "last_date": prev_date, "last_path": prev_path})
        return {"ok": True, "written": 0, "done": sorted(done_ids), "path": path}

    block = render_block(iso, entries)
    new_text = upsert_block(text or NEW_FILE_TEMPLATE, block)
    if new_text == text:
        log(f"写回：{iso} 内容无变化，跳过提交")
    else:
        msg = f"chore(diary): 助手待办 {iso}（{len(entries)} 条）"
        if not g.write_file(path, new_text, msg, sha=sha):
            return {"ok": False, "written": 0, "done": sorted(done_ids), "path": path}
        log(f"写回：{iso} 写入 {len(entries)} 条 → {path}（{'更新' if sha else '新建'}）")

    save_json(WB_PATH, {"updated_at": now_bj().isoformat(timespec="seconds"),
                        "last_date": iso, "last_path": path,
                        "written": len(entries), "done": sorted(done_ids)})
    return {"ok": True, "written": len(entries), "done": sorted(done_ids), "path": path}


if __name__ == "__main__":
    run()