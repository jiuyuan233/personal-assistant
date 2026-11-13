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

from dedup import (dedupe_by_id, load_list, merge_plans, merge_quotes, save_list)
from utils import (DATA_DIR, env, load_json, log, make_id, now_bj, safe_text,
                   save_json, today_bj)

DIARY_DIR = DATA_DIR / "diary"
STATE_PATH = DIARY_DIR / "state.json"
QUOTES_PATH = DIARY_DIR / "quotes.json"
PLANS_PATH = DIARY_DIR / "plans.json"

API = "https://gitee.com/api/v5/repos/{repo}/contents/{path}"
MAX_DEPTH = 3
CHECKBOX_RE = re.compile(r"^\s*[-*+]\s*\[([ xX])\]\s*(.+?)\s*$", re.M)
DATE_IN_NAME_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


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
# 确定性抽取：Obsidian 复选框
# ---------------------------------------------------------------------------
def extract_checkboxes(text: str, entry: dict, today: date) -> list[dict]:
    out = []
    sdate = source_date_of(entry)
    rel = entry.get("path") or entry.get("name") or ""
    for m in CHECKBOX_RE.finditer(text):
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
def ai_extract(client, text: str, entry: dict, today: date) -> tuple[list[dict], list[dict]]:
    from prompts import DIARY_SCHEMA, diary_messages

    rel = entry.get("path") or entry.get("name") or ""
    sdate = source_date_of(entry)
    try:
        data = client.chat_json(diary_messages(text, rel, sdate),
                                schema_hint=DIARY_SCHEMA,
                                temperature=0.2, max_tokens=2000)
    except Exception as e:
        log(f"日记 AI 抽取失败 {rel}: {safe_text(e, 200)}", level="ERROR")
        return [], []
    if not isinstance(data, dict):
        log(f"日记 AI 返回结构异常 {rel}", level="WARN")
        return [], []

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
    return quotes, plans


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
        except SystemExit:
            log("未配置 AI_API_KEY，日记只做复选框解析", level="WARN")

    new_quotes: list[dict] = []
    new_plans: list[dict] = []
    processed = 0
    for e in batch:
        rel = e.get("path") or ""
        full = g.contents(rel)
        if not isinstance(full, dict):
            continue
        text = decode_content(full)
        if not text.strip():
            continue
        new_plans.extend(extract_checkboxes(text, e, today))
        if client:
            q, p = ai_extract(client, text[:max_chars], e, today)
            new_quotes.extend(q)
            new_plans.extend(p)
        known[rel] = {"sha": e.get("sha"), "size": e.get("size"),
                      "processed_at": now_bj().isoformat(timespec="seconds")}
        processed += 1

    quotes = merge_quotes(load_list(QUOTES_PATH), dedupe_by_id(new_quotes), today)
    plans = merge_plans(load_list(PLANS_PATH), dedupe_by_id(new_plans), today)
    save_list(QUOTES_PATH, quotes, repo=repo)
    save_list(PLANS_PATH, plans, repo=repo)
    save_json(STATE_PATH, {"last_run": now_bj().isoformat(timespec="seconds"),
                           "repo": repo, "branch": branch, "root": root,
                           "file_count": len(entries), "files": known})

    open_plans = [p for p in plans if p.get("state") == "open"]
    log(f"日记完成：处理 {processed} 个文件，名言 {len(quotes)} 条，"
        f"计划 {len(plans)} 条（未完成 {len(open_plans)}）")
    return {"ok": True, "files": len(entries), "changed": len(changed),
            "processed": processed, "quotes": len(quotes),
            "plans": len(plans), "open_plans": len(open_plans)}


if __name__ == "__main__":
    run()