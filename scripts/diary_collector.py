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
from datetime import date, datetime, timedelta
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
# 助手的记忆（用户 2026-11-16 要求）：每天一条，由昨天日记生成，与计划一起写回。
MEMORY_PATH = DIARY_DIR / "memory.json"
MEMORY_MIN_CHARS = 40      # 昨天日记短于这个数就不生成（没东西可记，不编造）
MEMORY_MAX_CHARS = 4000     # 送 AI 的正文上限
MEMORY_KEEP_DAYS = 60       # 记忆账本保留天数，过期清理

API = "https://gitee.com/api/v5/repos/{repo}/contents/{path}"
MAX_DEPTH = 3
CHECKBOX_RE = re.compile(r"^\s*[-*+]\s*\[([ xX])\]\s*(.+?)\s*$", re.M)
DATE_IN_NAME_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
# 助手写回区块（plans / memory / english 三个）。里面的内容由 diary_writeback 自己管，
# 采集时必须整块跳过，否则会被 extract_checkboxes 和 AI 各抽一遍——助手写的
# 待办/记忆/英语混进 plans.json 或被自己"回忆"，都是污染。
#
# v1.7.0（用户报「乱码」）：区块标记从 HTML 注释改成 Obsidian 阅读视图不显示的
# `%%注释%%` 语法，内容用 callout（`> ` 引用块）包裹——Obsidian 里干净，不再裸露
# `<!-- ... -->` 原文。**旧格式（HTML 注释）仍能被识别**：读旧日记/迁移靠它。
# 英语角（用户 2026-11-16 要求：每日一句 small talk，难度随回应程度提升）。
BLOCK_START = "%% assistant:plans:start %%"
BLOCK_END = "%% assistant:plans:end %%"
OLD_BLOCK_START = "<!-- assistant:plans:start -->"
OLD_BLOCK_END = "<!-- assistant:plans:end -->"
MEM_BLOCK_START = "%% assistant:memory:start %%"
MEM_BLOCK_END = "%% assistant:memory:end %%"
OLD_MEM_BLOCK_START = "<!-- assistant:memory:start -->"
OLD_MEM_BLOCK_END = "<!-- assistant:memory:end -->"
EN_BLOCK_START = "%% assistant:english:start %%"
EN_BLOCK_END = "%% assistant:english:end %%"
# 新旧两种 marker 的区块都剩掉（旧日记迁移期间两种格式共存）
_BLOCK_STRIP = [
    re.compile(re.escape(BLOCK_START) + r".*?" + re.escape(BLOCK_END), re.S),
    re.compile(re.escape(OLD_BLOCK_START) + r".*?" + re.escape(OLD_BLOCK_END), re.S),
    re.compile(re.escape(MEM_BLOCK_START) + r".*?" + re.escape(MEM_BLOCK_END), re.S),
    re.compile(re.escape(OLD_MEM_BLOCK_START) + r".*?" + re.escape(OLD_MEM_BLOCK_END), re.S),
    re.compile(re.escape(EN_BLOCK_START) + r".*?" + re.escape(EN_BLOCK_END), re.S),
]


def strip_assistant_block(text: str) -> str:
    for pat in _BLOCK_STRIP:
        text = pat.sub("", text or "")
    return text


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
# 助手的记忆（用户 2026-11-16：「给我的 action 的 AI 加一个记忆的功能，
# 数据来源于 obsidian 的笔记里，根据日记自己生成一段话，说什么都可以」）
# ---------------------------------------------------------------------------
def offline_memory(text: str, day: str) -> str:
    """AI 不可用时的确定性占位（诚实：只报统计，不冒充理解）。"""
    boxes = list(CHECKBOX_RE.finditer(text))
    done = sum(1 for m in boxes if m.group(1).lower() == "x")
    return (f"（离线摘要，AI 今天不在线）{day} 你写了约 {len(text)} 字的日记，"
            f"勾了 {done}/{len(boxes)} 个待办。先替你记着这一笔，明天见。")


def memory_ledger_prune(items: list[dict], today: date) -> list[dict]:
    """记忆账本只留最近 MEMORY_KEEP_DAYS 天，防慢性膨胀。"""
    cutoff = (today - timedelta(days=MEMORY_KEEP_DAYS)).isoformat()
    return [it for it in items if (it.get("date") or "") >= cutoff]


def ensure_day_memory(g: Gitee, dcfg: dict, prefixes: list[str] | None = None,
                       now: datetime | None = None, client=None) -> str | None:
    """为**昨天**生成一段「助手记忆」，存 data/diary/memory.json，返回文本。

    时机与 plans 写回同款：只在凌晨写回窗口内做（白天绝不读外不写）；
    窗口内有多次 tick，已生成 ai 版就复用；offline 版下个 tick 若 AI 恢复会重生成。
    跳过条件（都返回 None，不生成也不占账）：窗口外 / 昨天日记不存在 /
    正文过短（MEMORY_MIN_CHARS）/ 昨天日记路径在私人黑名单里。
    """
    import diary_writeback  # 懒导入避免环：writeback 也 import 本模块的常量

    now = now or now_bj()
    if not dcfg.get("memory_enabled", True):
        return None
    window = dcfg.get("writeback_window") or diary_writeback.DEFAULT_WINDOW
    if not diary_writeback.in_window(now, window):
        return None

    day = (now.date() - timedelta(days=1)).isoformat()
    root = dcfg.get("path") or ""
    daily_dir = dcfg.get("daily_dir") or "日"
    rel = f"{root}/{daily_dir}/{day}.md".lstrip("/")
    if prefixes and is_excluded(rel, prefixes):
        log("[memory] 昨天的日记在私人黑名单里，不生成记忆")
        return None

    items = (load_json(MEMORY_PATH) or {}).get("items") or []
    by_date = {it.get("date"): it for it in items if isinstance(it, dict)}
    prev = by_date.get(day)
    if prev and prev.get("source") == "ai":
        return safe_text(prev.get("text"), 600) or None

    text, _sha = g.read_file(rel)
    text = strip_assistant_block(text).strip()
    if len(text) < MEMORY_MIN_CHARS:
        if not prev:
            log(f"[memory] {day} 的日记太短（{len(text)} 字），今天不写记忆")
        return prev and prev.get("text") or None

    source, out = "ai", ""
    if client is None:
        try:
            from ai_client import AIClient
            client = AIClient(timeout=90)
        except (SystemExit, RuntimeError):
            client = None
    if client is not None:
        try:
            from prompts import memory_messages
            out = safe_text(client.chat_text(
                memory_messages(text[:MEMORY_MAX_CHARS], day),
                temperature=0.8, max_tokens=500), 600).strip()
        except Exception as e:
            log(f"[memory] AI 生成失败，用离线摘要：{safe_text(e, 160)}", level="WARN")
    if not out:
        source, out = "offline", offline_memory(text, day)

    by_date[day] = {"date": day, "text": out, "source": source}
    items = memory_ledger_prune(list(by_date.values()), now.date())
    items.sort(key=lambda it: it.get("date") or "")
    save_json(MEMORY_PATH, {"updated_at": now.isoformat(timespec="seconds"),
                            "items": items})
    log(f"[memory] 记住 {day}（{source}）：{out[:80]}")
    return out


# ---------------------------------------------------------------------------
# 每日英语（用户 2026-11-16：「每日再给我一句英语句子，可以是复杂句，就是日常
# 的 small talk，难度由我回答的程度逐步提升」）——写进今天日记的英语角区块。
# ---------------------------------------------------------------------------
ENGLISH_PATH = DIARY_DIR / "english.json"
ENGLISH_MAX_LEVEL = 5          # 难度 1-5（1 简单日常 → 5 复杂句/地道表达）
# 离线降级句库（AI 挂了也要有句子；每级两句，轮换用 index 挑）
ENGLISH_FALLBACK = {
    1: ["Long time no see! How have you been?",
        "Did you sleep well last night?"],
    2: ["What did you get up to this weekend?",
        "Have you been keeping up with any shows lately?"],
    3: ["If you could pick up one new habit this month, what would it be?",
        "How do you usually unwind after a long day of classes?"],
    4: ["Do you ever feel like your best ideas show up right before you sleep?",
        "What's something you've been putting off that keeps lingering on your mind?"],
    5: ["Had you known how much effort it would take, would you still have started?",
        "If you had to describe your ideal weekend in three words, what would they be?"],
}


def offline_english(level: int, idx: int) -> str:
    """AI 不可用时的降级句子（诚实固定句库，按难度取，idx 轮换）。"""
    lv = max(1, min(ENGLISH_MAX_LEVEL, level))
    return ENGLISH_FALLBACK[lv][idx % len(ENGLISH_FALLBACK[lv])]


def ensure_daily_english(now: datetime | None = None, yesterday_text: str = "",
                         client=None) -> str:
    """生成今天的一句英语（small talk），返回句子文本；已生成当天则复用。

    - 时机与 memory 同款：只在凌晨写回窗口内由调用方触发（窗口判断在调用方）；
    - 难度自适应（用户：「难度由我回答的程度逐步提升」）：AI 读**昨天日记**里
      用户对上一句的回应——有英文回应且通顺 → level+1（封顶 5）；没回应 → 不变；
      AI 判断回应吃力 → 可 -1（下限 1）。AI 挂 → 离线句库按当前 level。
    - 账本 data/diary/english.json（生成物）：{level, sentence, date, comment}，
      comment = 对用户上一句回应的中文短评（v1.9.9：写进每日英语区块，
      不串到助手记忆里——用户 2026-11-21 明确两个区块职责要分开）。
      今天已生成直接返回（窗口内幂等）。
    """
    now = now or now_bj()
    iso = now.date().isoformat()
    state = load_json(ENGLISH_PATH) or {}
    if state.get("date") == iso and state.get("sentence"):
        return safe_text(state.get("sentence"), 200)

    level = int(state.get("level") or 1)
    level = max(1, min(ENGLISH_MAX_LEVEL, level))
    sentence = ""
    comment = ""

    if client is None:
        try:
            from ai_client import AIClient
            client = AIClient(timeout=90)
        except (SystemExit, RuntimeError):
            client = None
    if client is not None:
        try:
            from prompts import english_messages
            data = client.chat_json(english_messages(level, yesterday_text,
                                                      state.get("sentence") or ""),
                                    temperature=0.7, max_tokens=400)
            if isinstance(data, dict):
                sentence = safe_text(data.get("sentence"), 200).strip()
                comment = safe_text(data.get("comment"), 120).strip()
                try:
                    new_lv = int(data.get("level") or level)
                except (TypeError, ValueError):
                    new_lv = level
                level = max(1, min(ENGLISH_MAX_LEVEL, new_lv))
        except Exception as e:
            log(f"[english] AI 生成失败，用离线句库：{safe_text(e, 160)}", level="WARN")

    if not sentence:
        sentence = offline_english(level, int(now.strftime("%d")))

    save_json(ENGLISH_PATH, {"updated_at": now.isoformat(timespec="seconds"),
                             "date": iso, "sentence": sentence, "level": level,
                             "comment": comment})
    log(f"[english] 今日一句（Lv{level}）：{sentence[:80]}")
    return sentence


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

    # 助手的记忆（用户 2026-11-16）：凌晨窗口内为昨天生成一段记忆段落，
    # 与潜在计划一起写进今天的日记。失败只告警——采集数据已落盘，记忆明天再补。
    memory_text: str | None = None
    try:
        memory_text = ensure_day_memory(g, dcfg, prefixes)
    except Exception as e:
        log(f"记忆生成失败：{safe_text(e, 160)}", level="WARN")

    # 每日英语（用户 2026-11-16）：同一凌晨窗口生成今日一句 small talk；
    # 自适应难度要读昨天日记（用户在日记里回应上一句），一并送 AI 判断。
    english_text: str | None = None
    try:
        import diary_writeback
        if diary_writeback.in_window(now_bj(), dcfg.get("writeback_window")
                                     or diary_writeback.DEFAULT_WINDOW):
            yesterday_rel = (f"{dcfg.get('path') or ''}/"
                             f"{dcfg.get('daily_dir') or '日'}/"
                             f"{(today_bj() - timedelta(days=1)).isoformat()}.md").lstrip("/")
            y_text, _sha = g.read_file(yesterday_rel)
            english_text = ensure_daily_english(yesterday_text=strip_assistant_block(y_text or ""))
    except Exception as e:
        log(f"每日英语生成失败：{safe_text(e, 160)}", level="WARN")
    _en_state = load_json(ENGLISH_PATH) or {}
    _en_level = int(_en_state.get("level") or 1)
    # v1.9.9：对上一句英语回应的评价属于每日英语区块（用户明确两个区块职责分开）
    _en_comment = safe_text(_en_state.get("comment"), 120)

    # 写回：昨天挖到的潜在计划 → 今天日记里的复选框；上次没勾完的滚过来。
    # 写回失败只告警，不影响采集结果 —— 日记数据已经落盘了。
    wb: dict = {}
    try:
        import diary_writeback
        wb = diary_writeback.run(today=today, g=g, plans=plans, cfg=dcfg,
                                memory=memory_text, english=english_text,
                                english_level=_en_level,
                                english_comment=_en_comment)
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