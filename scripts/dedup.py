"""合并与排序。

两类数据的合并策略是相反的，这是本文件存在的唯一理由：

- 公告（notice）：一旦发布就不会变，**旧值优先**。AI 每天重新抽的描述会来回抖动，
  必须锁住第一次的结果，否则 README 天天有无意义的 diff。
- 作业（study）：状态和倒计时每次抓取都在变，**新值优先**。旧值只在「精确截止时间」
  这一项上胜出，因为列表页只给相对倒计时，详情页给的绝对时间才是真值。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from utils import load_json, log, now_bj, parse_datetime, save_json


# ---------------------------------------------------------------------------
# 通用
# ---------------------------------------------------------------------------
def dedupe_by_id(items: list[dict]) -> list[dict]:
    """同一批内部按 id 去重，保留第一个。"""
    seen: set[str] = set()
    out = []
    for it in items:
        iid = it.get("id")
        if not iid or iid in seen:
            continue
        seen.add(iid)
        out.append(it)
    return out


def _carry(old: dict, new: dict, keys: tuple[str, ...]) -> None:
    """old 里为空的字段，用 new 补上。"""
    for k in keys:
        if not old.get(k) and new.get(k):
            old[k] = new[k]


# ---------------------------------------------------------------------------
# 公告：旧值优先
# ---------------------------------------------------------------------------
NOTICE_KEEP_DAYS = 120


def merge_notices(old_list: list[dict], new_list: list[dict], today: date) -> list[dict]:
    by_id = {n["id"]: n for n in old_list}
    added = 0
    for new in new_list:
        nid = new["id"]
        if nid in by_id:
            old = by_id[nid]
            _carry(old, new, ("title", "url", "source", "publish_date",
                              "description", "deadline", "tags"))
            # 相关度只升不降：第一次判成「值得看」就不要被后来的模型改回去
            if new.get("score", 0) > old.get("score", 0):
                old["score"] = new["score"]
                if new.get("reason"):
                    old["reason"] = new["reason"]
        else:
            by_id[nid] = new
            added += 1
        by_id[nid]["last_seen"] = today.isoformat()

    result = prune_notices(list(by_id.values()), today)
    result.sort(key=lambda n: n.get("publish_date") or "", reverse=True)
    result.sort(key=lambda n: -(n.get("score") or 0))
    log(f"公告合并：新增 {added}，保留 {len(result)}")
    return result


def prune_notices(items: list[dict], today: date, keep_days: int = NOTICE_KEEP_DAYS) -> list[dict]:
    """丢掉太久没再出现的公告，避免仓库无限膨胀。"""
    cutoff = today - timedelta(days=keep_days)
    out = []
    for n in items:
        seen = n.get("last_seen") or n.get("first_seen") or ""
        try:
            d = date.fromisoformat(seen[:10])
        except ValueError:
            out.append(n)
            continue
        if d >= cutoff:
            out.append(n)
    return out


# ---------------------------------------------------------------------------
# 作业：新值优先
# ---------------------------------------------------------------------------
def merge_tasks(old_list: list[dict], new_list: list[dict], now: datetime) -> list[dict]:
    """新抓到的列表就是权威列表。

    旧记录只贡献两样东西：first_seen（用于「新出现」标记）和精确截止时间。
    没出现在新列表里的作业直接丢弃 —— 学习通列表页本身会淘汰过期/已完成项。
    """
    old_by_id = {t["id"]: t for t in old_list}
    stamp = now.isoformat(timespec="seconds")
    out = []
    fresh = 0
    for new in new_list:
        old = old_by_id.get(new["id"])
        if old:
            new["first_seen"] = old.get("first_seen") or stamp
            # 详情页拿到的绝对时间比列表页倒计时推算的准
            if old.get("deadline_exact") and not new.get("deadline_exact"):
                new["deadline"] = old["deadline"]
                new["deadline_exact"] = True
            if not new.get("description") and old.get("description"):
                new["description"] = old["description"]
        else:
            new["first_seen"] = stamp
            fresh += 1
        new["last_seen"] = stamp
        out.append(new)
    log(f"作业合并：本次 {len(out)} 条，其中新出现 {fresh} 条")
    return sort_by_deadline(out)


def sort_by_deadline(tasks: list[dict]) -> list[dict]:
    """截止时间近的在前；没有截止时间的排最后。"""
    far = datetime.max.replace(tzinfo=None)

    def key(t: dict):
        dt = parse_datetime(t.get("deadline"))
        if dt is None:
            return (1, far)
        return (0, dt.replace(tzinfo=None))

    return sorted(tasks, key=key)


# ---------------------------------------------------------------------------
# 日记产物：名言 / 计划
# ---------------------------------------------------------------------------
def merge_quotes(old_list: list[dict], new_list: list[dict], today: date) -> list[dict]:
    """名言按正文去重（不是按 id），同一句话换个日记文件也不该重复收录。"""
    by_text = {q["text"].strip(): q for q in old_list if q.get("text")}
    added = 0
    for new in new_list:
        text = (new.get("text") or "").strip()
        if not text:
            continue
        if text in by_text:
            _carry(by_text[text], new, ("source_file", "source_date", "context"))
        else:
            new.setdefault("first_seen", today.isoformat())
            by_text[text] = new
            added += 1
    result = list(by_text.values())
    result.sort(key=lambda q: q.get("source_date") or q.get("first_seen") or "", reverse=True)
    log(f"名言合并：新增 {added}，累计 {len(result)}")
    return result


def merge_plans(old_list: list[dict], new_list: list[dict], today: date) -> list[dict]:
    """计划按 id 合并，人工标记的 done/cancelled 永不被 AI 覆盖。"""
    by_id = {p["id"]: p for p in old_list}
    added = 0
    for new in new_list:
        pid = new["id"]
        if pid in by_id:
            old = by_id[pid]
            if old.get("state") == "cancelled":
                old["last_seen"] = today.isoformat()
                continue
            # 复选框是权威来源：作者把 [x] 改回 [ ] 也要跟着变
            if new.get("origin") == "checkbox":
                old["state"] = new.get("state") or old.get("state")
                old["last_seen"] = today.isoformat()
                continue
            if old.get("state") == "done":
                old["last_seen"] = today.isoformat()
                continue
            _carry(old, new, ("text", "source_file", "source_date", "due_date", "tags"))
            if new.get("confidence", 0) > old.get("confidence", 0):
                old["confidence"] = new["confidence"]
            old["state"] = new.get("state") or old.get("state") or "open"
        else:
            new.setdefault("state", "open")
            new.setdefault("first_seen", today.isoformat())
            by_id[pid] = new
            added += 1
        by_id[pid]["last_seen"] = today.isoformat()

    result = list(by_id.values())
    result.sort(key=lambda p: (0 if p.get("state") == "open" else 1,
                               p.get("due_date") or "9999-99-99",
                               p.get("first_seen") or ""))
    log(f"计划合并：新增 {added}，累计 {len(result)}")
    return result


# ---------------------------------------------------------------------------
# 文件级读写
# ---------------------------------------------------------------------------
def load_list(path) -> list[dict]:
    data = load_json(path)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ("items", "tasks", "notices", "quotes", "plans"):
            if isinstance(data.get(k), list):
                return data[k]
    return []


def save_list(path, items: list[dict], **meta) -> None:
    save_json(path, {"updated_at": now_bj().isoformat(timespec="seconds"),
                     "count": len(items), **meta, "items": items})