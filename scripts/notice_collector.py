"""校园公告采集（源3）。

绕开 CAS 登录，只抓公开列表页 —— 零封号风险，竞赛信息九成本来就公开发布。

三个站的列表结构不同，但都能用同一套「锚点 + 日期窗口」规则解析：
  www.huuc.edu.cn  <a href="../info/1061/27179.htm" title="…"><div class="date"><b>09</b><span>2026-11</span></div>…
  jwc.huuc.edu.cn  <li><span>10-28</span><a href="info/1018/5561.htm" title="…">…</a></li>
  tw.huuc.edu.cn   <li><a href="info/1007/10986.htm" title="…"><span>2026-11-06</span>…</a></li>
所以日期先在 <a>…</a> 内部找，找不到再往前找 200 字符。

分页是倒序的：tzgg.htm 是最新一页，「下页」指向 tzgg/220.htm（更旧）。
跟着「下页」链接走，不要自己算页码。
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from urllib.parse import urljoin

import requests

from dedup import dedupe_by_id, load_list, merge_notices, save_list
from utils import (DATA_DIR, clean_html_text, load_json, log, make_id,
                   safe_text, today_bj)

SOURCES_PATH = DATA_DIR / "sources.json"
NOTICE_PATH = DATA_DIR / "notice" / "notices.json"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36")

ANCHOR_RE = re.compile(r'<a\b[^>]*href="([^"]*info/\d+/\d+\.htm)"[^>]*>', re.I | re.S)
TITLE_RE = re.compile(r'title="([^"]*)"', re.I | re.S)
NEXT_RE = re.compile(r'<a[^>]*href="([^"]+)"[^>]*>\s*(?:下页|下一页|&gt;|»)\s*</a>', re.I)

DATE_PATTERNS = [
    (re.compile(r"<b>\s*(\d{1,2})\s*</b>\s*<span>\s*(\d{4})-(\d{1,2})\s*</span>", re.I), "dym"),
    (re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})"), "ymd"),
    (re.compile(r"(\d{4})/(\d{1,2})/(\d{1,2})"), "ymd"),
    (re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日"), "ymd"),
    (re.compile(r"<span[^>]*>\s*(\d{1,2})-(\d{1,2})\s*</span>", re.I), "md"),
    (re.compile(r">\s*(\d{1,2})-(\d{1,2})\s*<"), "md"),
]

AI_BATCH = 10
AI_MAX_CANDIDATES = 30
DETAIL_MAX = 5
# 博达 CMS 正文容器，www 与 jwc 两站实测一致
BODY_CONTAINER = "v_news_content"
BODY_CHARS = 6000


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
def load_sources() -> dict:
    cfg = load_json(SOURCES_PATH)
    if not isinstance(cfg, dict):
        log(f"sources.json 缺失或损坏：{SOURCES_PATH}", level="ERROR")
        return {}
    return cfg


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"})
    return s


def fetch(session: requests.Session, url: str, *, retries: int = 2) -> str | None:
    for attempt in range(1, retries + 1):
        try:
            r = session.get(url, timeout=30)
            if r.status_code == 200:
                r.encoding = r.apparent_encoding or "utf-8"
                return r.text
            log(f"抓取 {url} → HTTP {r.status_code}", level="WARN")
        except requests.RequestException as e:
            log(f"抓取 {url} 失败 ({attempt}/{retries}): {safe_text(e, 160)}", level="WARN")
    return None


# ---------------------------------------------------------------------------
# 列表解析
# ---------------------------------------------------------------------------
def _match_date(scope: str, today: date, *, nearest: bool = False) -> str | None:
    """在一段 HTML 里找日期。

    nearest=True 时取「离锚点最近」的那个匹配，而不是按模式优先级取第一个。
    往前看的窗口里可能混进上一条公告的日期，必须靠位置区分。
    """
    best = None
    for pat, kind in DATE_PATTERNS:
        for m in pat.finditer(scope):
            if nearest:
                if best is None or m.end() > best[0].end():
                    best = (m, kind)
            else:
                best = (m, kind)
                break
        if best and not nearest:
            break
    if best is None:
        return None
    m, kind = best
    g = m.groups()
    try:
        if kind == "dym":
            d, y, mo = int(g[0]), int(g[1]), int(g[2])
        elif kind == "ymd":
            y, mo, d = int(g[0]), int(g[1]), int(g[2])
        else:
            mo, d = int(g[0]), int(g[1])
            y = today.year
            # 12 月的公告在 1 月抓到时会算成「未来」，回退一年
            if date(y, mo, d) > today + timedelta(days=60):
                y -= 1
        return date(y, mo, d).isoformat()
    except ValueError:
        return None


def _find_date(inner: str, before: str, today: date) -> str | None:
    return _match_date(inner, today) or _match_date(before, today, nearest=True)


def parse_list(html: str, page_url: str, source: str, today: date) -> list[dict]:
    out = []
    for m in ANCHOR_RE.finditer(html):
        href = m.group(1)
        tag = m.group(0)
        close = html.find("</a>", m.end())
        inner = html[m.end():close] if close != -1 else html[m.end():m.end() + 400]
        before = html[max(0, m.start() - 200):m.start()]

        tm = TITLE_RE.search(tag)
        title = clean_html_text(tm.group(1)) if tm else clean_html_text(inner)
        if not title or len(title) < 4:
            continue  # 纯图片链接

        url = urljoin(page_url, href)
        out.append({
            "id": make_id(title, url),
            "title": title,
            "url": url,
            "source": source,
            "publish_date": _find_date(inner, before, today),
            "description": "",
            "deadline": None,
            "category": "",
            "reason": "",
            "tags": [],
            "score": 0,
            "first_seen": today.isoformat(),
            "last_seen": today.isoformat(),
        })
    return dedupe_by_id(out)


def next_page(html: str, page_url: str) -> str | None:
    m = NEXT_RE.search(html)
    if not m:
        return None
    nxt = urljoin(page_url, m.group(1))
    return nxt if nxt != page_url else None


def collect_source(session: requests.Session, src: dict, today: date) -> list[dict]:
    url = src["url"]
    pages = max(1, int(src.get("pages") or 1))
    items: list[dict] = []
    seen_urls = {url}
    for i in range(pages):
        html = fetch(session, url)
        if not html:
            break
        got = parse_list(html, url, src["name"], today)
        items.extend(got)
        log(f"  {src['name']} 第{i + 1}页：{len(got)} 条")
        nxt = next_page(html, url)
        if not nxt or nxt in seen_urls:
            break
        seen_urls.add(nxt)
        url = nxt
    return dedupe_by_id(items)


# ---------------------------------------------------------------------------
# 相关度
# ---------------------------------------------------------------------------
def keyword_gate(title: str, kw: dict) -> bool:
    if any(b in title for b in kw.get("block", [])):
        return False
    return any(k in title for k in kw.get("gate", []))


def keyword_score(title: str, kw: dict) -> int:
    if any(b in title for b in kw.get("block", [])):
        return 0
    if any(b in title for b in kw.get("boost", [])):
        return 3
    if any(k in title for k in kw.get("gate", [])):
        return 2
    return 0


def ai_score(items: list[dict], kw: dict) -> None:
    """就地给 items 填 score/category/reason/deadline/tags。失败则退回关键词打分。"""
    from ai_client import AIClient
    from prompts import NOTICE_SCHEMA, notice_messages

    try:
        client = AIClient(timeout=90)
    except SystemExit:
        log("未配置 AI_API_KEY，公告相关度退回关键词打分", level="WARN")
        for n in items:
            n["score"] = keyword_score(n["title"], kw)
        return

    for i in range(0, len(items), AI_BATCH):
        batch = items[i:i + AI_BATCH]
        try:
            data = client.chat_json(notice_messages(batch), schema_hint=NOTICE_SCHEMA,
                                    temperature=0.1, max_tokens=2000)
        except Exception as e:
            log(f"公告 AI 评分失败：{safe_text(e, 200)}", level="ERROR")
            data = None
        if not isinstance(data, list):
            for n in batch:
                n["score"] = keyword_score(n["title"], kw)
            continue
        by_index = {int(d.get("index", -1)): d for d in data if isinstance(d, dict)}
        for j, n in enumerate(batch):
            d = by_index.get(j)
            if not d:
                continue
            try:
                n["score"] = max(0, min(5, int(d.get("score") or 0)))
            except (TypeError, ValueError):
                n["score"] = 0
            n["category"] = safe_text(d.get("category"), 20)
            n["reason"] = safe_text(d.get("reason"), 60)
            n["tags"] = [safe_text(t, 12) for t in (d.get("tags") or [])][:3]
            dl = d.get("deadline")
            n["deadline"] = dl if isinstance(dl, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", dl) else None


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def body_text(html: str) -> str:
    i = html.find(BODY_CONTAINER)
    chunk = html[i:i + 30000] if i >= 0 else html
    return safe_text(clean_html_text(chunk), BODY_CHARS)


def ai_detail(session: requests.Session, items: list[dict]) -> None:
    """就地给高分公告补 description/deadline。只看标题拿不到截止日，必须读正文。"""
    from ai_client import AIClient
    from prompts import DETAIL_SCHEMA, detail_messages

    try:
        client = AIClient(timeout=90)
    except SystemExit:
        return
    done = 0
    for n in items:
        html = fetch(session, n["url"])
        if not html:
            log(f"正文抓取失败：{n['url']}", level="WARN")
            continue
        text = body_text(html)
        if len(text) < 80:
            log(f"正文过短（{len(text)} 字），跳过：{n['url']}", level="WARN")
            continue
        try:
            d = client.chat_json(detail_messages(n["title"], text),
                                 schema_hint=DETAIL_SCHEMA, temperature=0.1, max_tokens=2000)
        except Exception as e:
            log(f"公告正文摘要失败：{safe_text(e, 200)}", level="ERROR")
            continue
        if not isinstance(d, dict):
            log(f"正文摘要返回非对象（{type(d).__name__}）：{safe_text(d, 120)}", level="WARN")
            continue
        n["description"] = safe_text(d.get("summary"), 120)
        dl = d.get("deadline")
        if not n.get("deadline") and isinstance(dl, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", dl):
            n["deadline"] = dl
        done += 1
    log(f"正文摘要补全：{done}/{len(items)} 条")


def run() -> dict:
    cfg = load_sources()
    sources = [s for s in cfg.get("notice", []) if s.get("enabled")]
    kw = cfg.get("keywords", {})
    today = today_bj()
    session = make_session()

    all_new: list[dict] = []
    ok = 0
    for src in sources:
        try:
            got = collect_source(session, src, today)
        except Exception as e:
            log(f"源 {src.get('name')} 异常：{safe_text(e, 200)}", level="ERROR")
            continue
        if got:
            ok += 1
        all_new.extend(got)
    all_new = dedupe_by_id(all_new)
    log(f"抓取站点：{len(sources)}，成功：{ok}")

    old = load_list(NOTICE_PATH)
    old_ids = {n["id"] for n in old}
    fresh = [n for n in all_new if n["id"] not in old_ids]

    candidates = [n for n in fresh if keyword_gate(n["title"], kw)][:AI_MAX_CANDIDATES]
    cand_ids = {n["id"] for n in candidates}
    for n in fresh:
        if n["id"] not in cand_ids:
            n["score"] = keyword_score(n["title"], kw)
    if candidates:
        log(f"送 AI 评分：{len(candidates)} 条（本次新增 {len(fresh)} 条）")
        ai_score(candidates, kw)

    merged = merge_notices(old, all_new, today)
    high = [n for n in merged if (n.get("score") or 0) >= 4]
    # 只看标题拿不到报名截止日，高分公告必须读正文。已有摘要的不重复抓。
    targets = [n for n in high if not n.get("description")][:DETAIL_MAX]
    if targets:
        log(f"抓正文补摘要：{len(targets)} 条")
        ai_detail(session, targets)
    save_list(NOTICE_PATH, merged, sources=[s["name"] for s in sources])

    log(f"高相关：{len(high)}")
    for n in high[:5]:
        log(f"[notice] {n['score']}分 {n['title']} ({n.get('publish_date') or '?'}) {n['url']}")
    return {"ok": ok > 0, "sites": len(sources), "sites_ok": ok,
            "fetched": len(all_new), "new": len(fresh), "high": len(high),
            "total": len(merged)}


if __name__ == "__main__":
    run()