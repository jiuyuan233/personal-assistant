"""合并与排序。

两类数据的合并策略是相反的，这是本文件存在的唯一理由：

- 公告（notice）：一旦发布就不会变，**旧值优先**。AI 每天重新抽的描述会来回抖动，
  必须锁住第一次的结果，否则 README 天天有无意义的 diff。
- 作业（study）：状态和倒计时每次抓取都在变，**新值优先**。旧值只在「精确截止时间」
  这一项上胜出，因为列表页只给相对倒计时，详情页给的绝对时间才是真值。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from utils import BJ_TZ, load_json, log, now_bj, parse_datetime, save_json


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
                              "description", "deadline", "deadline_source", "tags"))
            # 相关度只升不降：第一次判成「值得看」就不要被后来的模型改回去
            if new.get("score", 0) > old.get("score", 0):
                old["score"] = new["score"]
                if new.get("reason"):
                    old["reason"] = new["reason"]
            # 审计 §3.1：正文通道有字面依据，标题通道没有。「旧值优先」不能
            # 让一个未经校验的标题日期永久压住后来校验过的正文日期。
            if (new.get("deadline_source") == "body" and new.get("deadline")
                    and old.get("deadline_source") != "body"):
                old["deadline"] = new["deadline"]
                old["deadline_source"] = "body"
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

    **但「新列表为空」是例外**（审计 §1.2）：那通常意味着这次全都没抓到，
    而不是作业真的全没了。此时拒绝合并，保住历史数据。
    """
    if not new_list and old_list:
        log(f"新任务列表为空而历史有 {len(old_list)} 条，拒绝合并（疑似采集失败）", level="WARN")
        return sort_by_deadline(old_list)
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
    """截止时间近的在前；没有截止时间的排最后。

    审计 §5.5：原来用 replace(tzinfo=None) **剥掉**时区而不是换算。
    当前全是 +08:00 所以看不出问题，一旦混进 UTC 字符串就会静默差 8 小时。
    """
    far = datetime.max.replace(tzinfo=BJ_TZ)

    def key(t: dict):
        dt = parse_datetime(t.get("deadline"))
        if dt is None:
            return (1, far)
        return (0, dt.astimezone(BJ_TZ))

    return sorted(tasks, key=key)


# ---------------------------------------------------------------------------
# 日记产物：名言 / 计划
# ---------------------------------------------------------------------------
# 名言只增不减是刻意的：那是用户自己写下的话，删掉等于替他遗忘。
# 计划则把「已完成/已取消且长期不再出现」的挪进归档表，主表只留在办的事。
PLAN_MIN_CONFIDENCE = 0.4
PLAN_ARCHIVE_DAYS = 30


def merge_quotes(old_list: list[dict], new_list: list[dict], today: date) -> list[dict]:
    """名言按正文去重（不是按 id），同一句话换个日记文件也不该重复收录。"""
    by_text = {q["text"].strip(): q for q in old_list if q.get("text")}
    added = 0
    for new in new_list:
        text = (new.get("text") or "").strip()
        if not text:
            continue
        if text in by_text:
            _carry(by_text[text], new, ("source_file", "source_date", "context", "comment"))
        else:
            new.setdefault("first_seen", today.isoformat())
            by_text[text] = new
            added += 1
    result = list(by_text.values())
    result.sort(key=lambda q: q.get("source_date") or q.get("first_seen") or "", reverse=True)
    log(f"名言合并：新增 {added}，累计 {len(result)}")
    return result


def merge_plans(old_list: list[dict], new_list: list[dict], today: date) -> list[dict]:
    """计划按 id 合并，人工标记的 done/cancelled 永不被 AI 覆盖。

    新入库的 AI 计划要过置信度门槛（审计 §2.3）。注意审计原文写的是
    「confidence = 1 是最低、门槛设 >= 2」——**那是把量纲搞反了**：
    prompts.DIARY_INSTRUCTIONS 规定 confidence 是 0.0–1.0，1.0 是最高。
    所以门槛取 0.4，即「模型自己都认为比模糊念头还弱」的才丢。
    复选框计划 confidence 恒为 1.0，是用户亲手写的，永远不受门槛影响。
    """
    by_id = {p["id"]: p for p in old_list}
    added = 0
    weak = 0
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
            if (new.get("origin") == "ai"
                    and float(new.get("confidence") or 0) < PLAN_MIN_CONFIDENCE):
                weak += 1
                continue
            new.setdefault("state", "open")
            new.setdefault("first_seen", today.isoformat())
            by_id[pid] = new
            added += 1
        by_id[pid]["last_seen"] = today.isoformat()

    result = list(by_id.values())
    result.sort(key=lambda p: (0 if p.get("state") == "open" else 1,
                               p.get("due_date") or "9999-99-99",
                               p.get("first_seen") or ""))
    log(f"计划合并：新增 {added}，低置信度丢弃 {weak}，累计 {len(result)}")
    return result


def archive_plans(items: list[dict], today: date,
                  keep_days: int = PLAN_ARCHIVE_DAYS) -> tuple[list[dict], list[dict]]:
    """把长期不再出现的已完成/已取消计划挪出主表（审计 §3.4）。

    **open 计划一条都不归档**：那是用户自己的意图，宁可列表长也不能替他丢。
    老计划轮不到写回的问题靠 pick_plans 的 first_seen 升序解决，不靠删数据。
    返回 (留在主表的, 该进归档表的)。
    """
    cutoff = (today - timedelta(days=keep_days)).isoformat()
    kept, archived = [], []
    for p in items:
        stale = (p.get("last_seen") or p.get("first_seen") or "")[:10] < cutoff
        if p.get("state") in ("done", "cancelled") and stale:
            archived.append(p)
        else:
            kept.append(p)
    if archived:
        log(f"计划归档：{len(archived)} 条已完成/取消且 {keep_days} 天未再出现，主表剩 {len(kept)}")
    return kept, archived


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
    """内容没变就不动 updated_at（审计 §4.2）。

    原来每次都写「现在」，于是 study 每小时跑一次，即使一条任务都没变，
    tasks.json 也必然产生一行 diff → 每天 24 个纯时间戳 commit。
    Actions 免费额度是按 commit 触发的 run 数算的，噪声 commit 白烧额度。
    """
    old = load_json(path)
    old_items = old.get("items") if isinstance(old, dict) else None
    stamp = now_bj().isoformat(timespec="seconds")
    if isinstance(old_items, list) and old_items == items:
        stamp = old.get("updated_at") or stamp
    save_json(path, {"updated_at": stamp, "count": len(items), **meta, "items": items})