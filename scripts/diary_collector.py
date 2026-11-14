"""Gitee 日记挖掘（源1）。

走 Gitee API v5，不用 git clone —— 仓库里有二进制附件，全量 clone 会炸
（fatal: fetch-pack: invalid index-pack output），而 API 只取需要的 .md。

两个坑：
1. contents 接口的中文路径必须编码进 URL **路径段**，用 ?path= 查询参数会返回 []
2. 目录列表不含 content，只有元数据；要正文得再请求单个文件

增量判定用 git blob sha（列表接口就带），不依赖 git 历史，--depth=1 也能用。
state.json 记录每个文件处理时的 sha，只有 sha 变了才重新送 AI。

计划抽取是双通道：
- Obsidian 复选框 `- [ ]` / `- [x]` 直接确定性解析，置信度 1.0，状态权威
- AI 只负责挖「隐含意图」，避免它把明写的待办漏掉或改状态
"""
from __future__ import annotations

import base64
import re
from datetime import date
from urllib.parse import quote

import requests

from dedup import (archive_plans, dedupe_by_id, load_list, merge_plans,
                   merge_quotes, save_list)
from utils import (DATA_DIR, env, load_json, log, make_id, now_bj, safe_text,
                   save_json, today_bj)

DIARY_DIR = DATA_DIR / "diary"
STATE_PATH = DIARY_DIR / "state.json"
QUOTES_PATH = DIARY_DIR / "quotes.json"
PLANS_PATH = DIARY_DIR / "plans.json"
ARCHIVE_PATH = DIARY_DIR / "plans_archive.json"

API = "https://gitee.com/api/v5/repos/{repo}/contents/{path}"
MAX_DEPTH = 3
CHECKBOX_RE = re.compile(r"^\s*[-*+]\s*\[([ xX])\]\s*(.+?)\s*$", re.M)
DATE_IN_NAME_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
# 助手写回区块。里面的复选框由 diary_writeback 自己管，采集时必须整块跳过，
# 否则同一句话会被 extract_checkboxes 和 AI 各抽一遍，plans.json 里全是重复项。
BLOCK_START = "<!-- assistant:plans:start -->"
BLOCK_END = "<!-- assistant:plans:end -->"
BLOCK_RE = re.compile(re.escape(BLOCK_START) + r".*?" + re.escape(BLOCK_END), re.S)


def strip_assistant_block(text: str) -> str:
    return BLOCK_RE.sub("", text or "")


# ---------------------------------------------------------------------------
# Gitee API
# ---------------------------------------------------------------------------
class Gitee:
    def __init__(self, repo: str, branch: str, token: str) -> None:
        self.repo = repo
        self.branch = branch
        self.token = token
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "personal-assistant/1.0"})
        self.calls = 0

    def contents(self, path: str) -> list[dict] | dict | None:
        url = API.format(repo=self.repo, path=quote(path, safe="/"))
        self.calls += 1
        try:
            r = self.session.get(url, params={"access_token": self.token,
                                              "ref": self.branch}, timeout=30)
        except requests.RequestException as e:
            log(f"Gitee 请求失败 {path}: {safe_text(e, 160)}", level="ERROR")
            return None
        if r.status_code != 200:
            log(f"Gitee {path} → HTTP {r.status_code}", level="ERROR")
            return None
        try:
            return r.json()
        except ValueError:
            log(f"Gitee {path} 返回非 JSON", level="ERROR")
            return None

    def walk(self, path: str, depth: int = 0) -> list[dict]:
        """递归列出所有 .md 文件。目录本身不产出内容。"""
        if depth > MAX_DEPTH:
            return []
        data = self.contents(path)
        if data is None:
            return []
        if isinstance(data, dict):
            data = [data]
        out = []
        for e in data:
            if not isinstance(e, dict):
                continue
            if e.get("type") == "dir":
                out.extend(self.walk(e.get("path") or "", depth + 1))
            elif e.get("type") == "file" and str(e.get("name", "")).lower().endswith(".md"):
                out.append(e)
        return out

    def read_file(self, path: str) -> tuple[str, str | None]:
        """返回 (正文, sha)。

        ⚠️ 实测：Gitee 对不存在/已删除的文件返回 **HTTP 200 + `{}`**，不是 404。
        所以判断存在与否必须看 body 里有没有 sha，只看状态码会误判成「文件存在但空」。
        """
        d = self.contents(path)
        if not isinstance(d, dict) or not d.get("sha"):
            return "", None
        return decode_content(d), d.get("sha")

    def write_file(self, path: str, text: str, message: str, sha: str | None = None) -> bool:
        """新建（sha=None 走 POST）或更新（带当前 sha 走 PUT）。不带 sha 更新会被拒。"""
        url = API.format(repo=self.repo, path=quote(path, safe="/"))
        payload = {"access_token": self.token, "branch": self.branch,
                   "content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
                   "message": message}
        if sha:
            payload["sha"] = sha
        self.calls += 1
        try:
            r = (self.session.put(url, json=payload, timeout=30) if sha
                 else self.session.post(url, json=payload, timeout=30))
        except requests.RequestException as e:
            log(f"Gitee 写入失败 {path}: {safe_text(e, 160)}", level="ERROR")
            return False
        if r.status_code not in (200, 201):
            log(f"Gitee 写入 {path} → HTTP {r.status_code}", level="ERROR")
            return False
        return True


def decode_content(entry: dict) -> str:
    raw = entry.get("content") or ""
    if not raw:
        return ""
    try:
        return base64.b64decode(raw).decode("utf-8", errors="replace")
    except Exception:
        return ""


def source_date_of(entry: dict) -> str:
    m = DATE_IN_NAME_RE.search(entry.get("name") or "")
    if m:
        return m.group(0)
    m = DATE_IN_NAME_RE.search(entry.get("path") or "")
    return m.group(0) if m else ""


# ---------------------------------------------------------------------------
# 私人路径黑名单（审计 §2.2 / §7.3）
# ---------------------------------------------------------------------------
def excluded_prefixes() -> list[str]:
    """从环境变量 DIARY_EXCLUDE 读私人路径前缀，逗号/分号/换行分隔。

    **黑名单本身不能进仓库**：路径名就暴露了用户把哪些主题标成私人。
    所以值只放 GitHub Secret `DIARY_EXCLUDE`，仓库里只出现这个名字。
    """
    raw = env("DIARY_EXCLUDE")
    return [s.strip().strip("/") for s in re.split(r"[,;\n]", raw) if s.strip()]


def is_excluded(rel: str, prefixes: list[str]) -> bool:
    rel = (rel or "").strip("/")
    return any(rel == p or rel.startswith(p + "/") for p in prefixes)


def drop_excluded(items: list[dict], prefixes: list[str]) -> tuple[list[dict], int]:
    """把已经入库的私人条目清掉。

    只挡新采集不够 —— 黑名单生效前采到的计划还在 plans.json 里公开着。
    """
    if not prefixes:
        return items, 0
    kept = [it for it in items if not is_excluded(it.get("source_file") or "", prefixes)]
    return kept, len(items) - len(kept)


# ---------------------------------------------------------------------------
# 确定性抽取：Obsidian 复选框
# ---------------------------------------------------------------------------
def extract_checkboxes(text: str, entry: dict, today: date) -> list[dict]:
    out = []
    sdate = source_date_of(entry)
    rel = entry.get("path") or entry.get("name") or ""
    for m in CHECKBOX_RE.finditer(strip_assistant_block(text)):
        done = m.group(1).lower() == "x"
        raw = m.group(2).strip()
        # 去掉行内标签和尾部的 Obsidian 链接别名，保留可读文本
        raw = re.sub(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]", r"\1", raw)
        raw = re.sub(r"#\S+", "", raw).strip()
        if len(raw) < 2:
            continue
        out.append({
            "id": make_id(raw, rel),
            "text": safe_text(raw, 120),
            "due_date": None,
            "confidence": 1.0,
            "state": "done" if done else "open",
            "tags": ["待办"],
            "source_file": rel,
            "source_date": sdate,
            "origin": "checkbox",
            "first_seen": today.isoformat(),
            "last_seen": today.isoformat(),
        })
    return out


# ---------------------------------------------------------------------------
# AI 抽取
# ---------------------------------------------------------------------------
def ai_extract(client, text: str, entry: dict, today: date) -> tuple[list[dict], list[dict], bool]:
    """返回 (名言, 计划, 是否成功)。

    第三个值是给增量判定用的（审计 §1.4）：AI 挂了和「这篇日记本来就没东西可抽」
    都返回空列表，但前者必须重试、后者不该重试。只看列表空不空分不开这两种情况。
    """
    from prompts import DIARY_SCHEMA, diary_messages

    rel = entry.get("path") or entry.get("name") or ""
    sdate = source_date_of(entry)
    try:
        data = client.chat_json(diary_messages(text, rel, sdate),
                                schema_hint=DIARY_SCHEMA,
                                temperature=0.2, max_tokens=2000)
    except Exception as e:
        log(f"日记 AI 抽取失败 {rel}: {safe_text(e, 200)}", level="ERROR")
        return [], [], False
    if not isinstance(data, dict):
        log(f"日记 AI 返回结构异常 {rel}", level="WARN")
        return [], [], False

    quotes = []
    for q in data.get("quotes") or []:
        if not isinstance(q, dict):
            continue
        t = safe_text(q.get("text"), 300).strip()
        if len(t) < 4:
            continue
        quotes.append({
            "id": make_id(t),
            "text": t,
            "context": safe_text(q.get("context"), 60),
            "comment": safe_text(q.get("comment"), 120),
            "source_file": rel,
            "source_date": sdate,
            "first_seen": today.isoformat(),
        })

    plans = []
    for p in data.get("plans") or []:
        if not isinstance(p, dict):
            continue
        t = safe_text(p.get("text"), 120).strip()
        if len(t) < 3:
            continue
        due = p.get("due_date")
        try:
            conf = max(0.0, min(1.0, float(p.get("confidence") or 0.5)))
        except (TypeError, ValueError):
            conf = 0.5
        plans.append({
            "id": make_id(t, rel),
            "text": t,
            "due_date": due if isinstance(due, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", due) else None,
            "confidence": conf,
            "state": "open",
            "tags": [safe_text(x, 12) for x in (p.get("tags") or [])][:3],
            "source_file": rel,
            "source_date": sdate,
            "origin": "ai",
            "first_seen": today.isoformat(),
            "last_seen": today.isoformat(),
        })
    return quotes, plans, True


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def run() -> dict:
    cfg = load_json(DATA_DIR / "sources.json") or {}
    dcfg = cfg.get("diary") or {}
    repo = dcfg.get("repo") or "jiuyuan233/obsidion-gitee"
    branch = dcfg.get("branch") or "master"
    root = dcfg.get("path") or ""
    max_files = int(dcfg.get("max_files_per_run") or 12)
    max_chars = int(dcfg.get("max_chars_per_file") or 6000)

    token = env("GITEE_TOKEN")
    if not token:
        log("未配置 GITEE_TOKEN，跳过日记采集", level="WARN")
        return {"ok": False, "skipped": "no_token"}

    today = today_bj()
    g = Gitee(repo, branch, token)
    entries = g.walk(root)
    log(f"Gitee 目录扫描：{len(entries)} 个 md 文件（API 调用 {g.calls} 次）")
    if not entries:
        return {"ok": False, "files": 0}

    # 私人目录不采、也不公开（审计 §2.2）。日志只报条数，不报路径 —— 路径名本身是隐私。
    prefixes = excluded_prefixes()
    if prefixes:
        before = len(entries)
        entries = [e for e in entries if not is_excluded(e.get("path") or "", prefixes)]
        log(f"私人路径黑名单：跳过 {before - len(entries)} 个文件（{len(prefixes)} 条前缀）")
    else:
        log("未配置 DIARY_EXCLUDE，全部日记文件都会公开采集", level="WARN")

    state = load_json(STATE_PATH) or {}
    known = state.get("files") or {}
    changed = [e for e in entries
               if known.get(e.get("path") or "", {}).get("sha") != e.get("sha")]
    # 文件名带日期，倒序即「最新日记优先」
    changed.sort(key=lambda e: e.get("name") or "", reverse=True)
    batch = changed[:max_files]
    log(f"变更文件：{len(changed)}，本次处理 {len(batch)}")

    client = None
    if batch:
        from ai_client import AIClient
        try:
            client = AIClient(timeout=90)
        except (SystemExit, RuntimeError):
            log("未配置 AI_API_KEY，日记只做复选框解析", level="WARN")

    new_quotes: list[dict] = []
    new_plans: list[dict] = []
    processed = 0
    retry = 0
    for e in batch:
        rel = e.get("path") or ""
        full = g.contents(rel)
        if not isinstance(full, dict):
            continue
        text = decode_content(full)
        if not text.strip():
            continue
        new_plans.extend(extract_checkboxes(text, e, today))
        ai_ok = True
        if client:
            q, p, ai_ok = ai_extract(client, strip_assistant_block(text)[:max_chars], e, today)
            new_quotes.extend(q)
            new_plans.extend(p)
        if ai_ok:
            known[rel] = {"sha": e.get("sha"), "size": e.get("size"),
                          "processed_at": now_bj().isoformat(timespec="seconds")}
        else:
            # 审计 §1.4：AI 失败时**不推进 sha**，否则这篇日记永久漏采。
            # sha 记 None → 下轮 changed 判定必然命中 → 自动重试。
            known[rel] = {"sha": None, "failed": True, "size": e.get("size"),
                          "processed_at": now_bj().isoformat(timespec="seconds")}
            retry += 1
            log(f"AI 抽取失败，不推进 sha，下轮重试：{rel}", level="WARN")
        processed += 1
    if retry:
        log(f"本次 {retry} 个文件 AI 失败，已标记下轮重试", level="WARN")

    old_quotes = load_list(QUOTES_PATH)
    old_plans = load_list(PLANS_PATH)
    if prefixes:
        old_quotes, dq = drop_excluded(old_quotes, prefixes)
        old_plans, dp = drop_excluded(old_plans, prefixes)
        old_arc = load_list(ARCHIVE_PATH)
        old_arc, da = drop_excluded(old_arc, prefixes)
        if da:
            save_list(ARCHIVE_PATH, old_arc, repo=repo)
        if dq or dp or da:
            log(f"清理黑名单生效前入库的私人条目：名言 {dq} 条、计划 {dp} 条、归档 {da} 条")
        # state.json 的键**就是文件路径**。只清 plans/quotes 不够 ——
        # 私人目录名会从 sha 映射里泄露出去（2026-11-14 全仓库扫描实测发现）。
        bad_keys = [k for k in known if is_excluded(k, prefixes)]
        for k in bad_keys:
            del known[k]
        if bad_keys:
            log(f"清理 state.json 里的私人路径：{len(bad_keys)} 条")
    quotes = merge_quotes(old_quotes, dedupe_by_id(new_quotes), today)
    plans = merge_plans(old_plans, dedupe_by_id(new_plans), today)
    plans, archived = archive_plans(plans, today)
    if archived:
        save_list(ARCHIVE_PATH, dedupe_by_id(load_list(ARCHIVE_PATH) + archived), repo=repo)
    save_list(QUOTES_PATH, quotes, repo=repo)
    save_list(PLANS_PATH, plans, repo=repo)
    save_json(STATE_PATH, {"last_run": now_bj().isoformat(timespec="seconds"),
                           "repo": repo, "branch": branch, "root": root,
                           "file_count": len(entries), "files": known})

    open_plans = [p for p in plans if p.get("state") == "open"]
    log(f"日记完成：处理 {processed} 个文件，名言 {len(quotes)} 条，"
        f"计划 {len(plans)} 条（未完成 {len(open_plans)}）")

    # 写回：昨天挖到的潜在计划 → 今天日记里的复选框；上次没勾完的滚过来。
    # 写回失败只告警，不影响采集结果 —— 日记数据已经落盘了。
    wb: dict = {}
    try:
        import diary_writeback
        wb = diary_writeback.run(today=today, g=g, plans=plans, cfg=dcfg)
    except Exception as e:
        log(f"日记写回失败：{safe_text(e, 200)}", level="WARN")
    done = set(wb.get("done") or [])
    undone = set(wb.get("undone") or [])
    picked = set(wb.get("picked") or [])
    if done or undone or picked:
        for p in plans:
            pid = p.get("id")
            if pid in done:
                p["state"] = "done"
                # 审计 §3.2：复选框已成为权威来源。不改 origin 的话
                # merge_plans 的 done 短路会让「勾了又取消」永远回不到待办。
                p["origin"] = "checkbox"
            elif pid in undone:
                p["state"] = "open"
                p["origin"] = "checkbox"
            if pid in picked:
                p["written_once"] = True
                p["last_written"] = today.isoformat()
        save_list(PLANS_PATH, plans, repo=repo)
        log(f"写回复查：完成 {len(done)} 条、取消勾选复原 {len(undone)} 条、"
            f"本次写入 {len(picked)} 条")

    # 新入库的名言当场打分，README 的「AI 挑 3 条」不用等 backfill。
    # 失败只告警：采集结果已经落盘，打分缺了下一轮或 backfill 还能补。
    try:
        score_quotes()
    except Exception as e:
        log(f"名言价值打分异常：{safe_text(e, 160)}", level="WARN")

    return {"ok": True, "files": len(entries), "changed": len(changed),
            "processed": processed, "quotes": len(quotes),
            "plans": len(plans), "open_plans": len(open_plans),
            "writeback": wb.get("written", 0)}


def backfill_comments(batch: int = 8) -> dict:
    """给 comment 为空的名言补 AI 点评，并连带补价值分（审计 §3.3 + 用户「AI 挑 3 条」）。

    为什么需要单独一个模式：增量采集只处理 sha 变化的文件，老日记不再重跑 AI，
    而 merge_quotes 的 _carry 只补空字段 → 空 comment 永远空着。
    实测 15 条名言只有 8 条有点评，用户要求的「名言加 AI 点评」只做了一半。

    零采集、只读 quotes.json + 调 AI，跑一次即可（`main.py backfill`）。
    点评每批 8 条，价值分每批 10 条（`score_quotes`），两个出口都会调打分。
    """
    from ai_client import AIClient
    from prompts import COMMENT_SCHEMA, comment_messages

    quotes = load_list(QUOTES_PATH)
    todo = [q for q in quotes if not (q.get("comment") or "").strip()]
    if not todo:
        log(f"名言点评回填：{len(quotes)} 条全部已有评论，无需回填")
        sv = score_quotes()
        return {"ok": True, "filled": 0, "pending": 0, "scored": sv.get("scored", 0)}
    try:
        client = AIClient(timeout=90)
    except (SystemExit, RuntimeError) as e:
        log(f"名言点评回填：AI 不可用（{type(e).__name__}）", level="ERROR")
        return {"ok": False, "filled": 0, "pending": len(todo)}

    filled = 0
    for i in range(0, len(todo), batch):
        chunk = todo[i:i + batch]
        try:
            data = client.chat_json(comment_messages(chunk), schema_hint=COMMENT_SCHEMA,
                                    temperature=0.7, max_tokens=2000)
        except Exception as e:
            log(f"名言点评回填失败（第 {i // batch + 1} 批）：{safe_text(e, 160)}", level="ERROR")
            continue
        comments = (data or {}).get("comments") if isinstance(data, dict) else None
        if not isinstance(comments, list):
            log(f"名言点评回填：第 {i // batch + 1} 批返回结构异常", level="WARN")
            continue
        for q, c in zip(chunk, comments):
            c = safe_text(c, 120).strip()
            if c:
                q["comment"] = c
                filled += 1
    if filled:
        save_list(QUOTES_PATH, quotes, repo=(load_json(DATA_DIR / "sources.json") or {})
                  .get("diary", {}).get("repo", ""))
    log(f"名言点评回填：补上 {filled} 条，仍空 {len(todo) - filled} 条（共 {len(quotes)} 条）")
    sv = score_quotes()
    # 「全都补过了」也是成功：否则存量名言补齐之后再跑 backfill 会假报失败。
    return {"ok": filled > 0 or sv.get("ok", False), "filled": filled,
            "pending": len(todo) - filled, "scored": sv.get("scored", 0)}


def score_quotes(batch: int = 10) -> dict:
    """给 value 缺失的名言打 AI 价值分（0-10）。README 只展示分最高的 3 条。

    用户要求「名言就显示3条，让 AI 挑选出有价值的放上去」。挑选 = 打分 + 排序：
    打分在这里联网做（diary / backfill 模式），结果存进 quotes.json 的 value 字段；
    finalize 零网络零 token，只按 value 取前 3（AGENT.md 铁律 5）。

    与 backfill_comments 同型的坑：增量采集不重跑老文件，老名言的 value 也要能补，
    所以 backfill 模式会连带调这里。打分失败不抛异常 —— 缺 value 的名言渲染时按 0 分排，
    README 不会开天窗。
    """
    from ai_client import AIClient
    from prompts import QUOTE_VALUE_SCHEMA, quote_value_messages

    quotes = load_list(QUOTES_PATH)
    todo = [q for q in quotes if q.get("value") is None]
    if not todo:
        log(f"名言价值打分：{len(quotes)} 条全部已有分数，无需打分")
        return {"ok": True, "scored": 0, "pending": 0}
    try:
        client = AIClient(timeout=90)
    except (SystemExit, RuntimeError) as e:
        log(f"名言价值打分：AI 不可用（{type(e).__name__}）", level="ERROR")
        return {"ok": False, "scored": 0, "pending": len(todo)}

    scored = 0
    for i in range(0, len(todo), batch):
        chunk = todo[i:i + batch]
        try:
            data = client.chat_json(quote_value_messages(chunk),
                                    schema_hint=QUOTE_VALUE_SCHEMA,
                                    temperature=0.3, max_tokens=2000)
        except Exception as e:
            log(f"名言价值打分失败（第 {i // batch + 1} 批）：{safe_text(e, 160)}", level="ERROR")
            continue
        values = (data or {}).get("values") if isinstance(data, dict) else None
        if not isinstance(values, list):
            log(f"名言价值打分：第 {i // batch + 1} 批返回结构异常", level="WARN")
            continue
        for q, v in zip(chunk, values):
            if not isinstance(v, dict):
                continue
            try:
                n = int(v.get("value"))
            except (TypeError, ValueError):
                continue
            q["value"] = max(0, min(10, n))
            q["value_why"] = safe_text(v.get("why"), 40).strip()
            scored += 1
    if scored:
        save_list(QUOTES_PATH, quotes, repo=(load_json(DATA_DIR / "sources.json") or {})
                  .get("diary", {}).get("repo", ""))
    log(f"名言价值打分：补上 {scored} 条，仍缺 {len(todo) - scored} 条（共 {len(quotes)} 条）")
    return {"ok": scored > 0, "scored": scored, "pending": len(todo) - scored}


if __name__ == "__main__":
    run()