"""Playwright 无头浏览器：抓课程章节里的随堂作业/测验（v1.5.22）。

为什么需要浏览器：随堂作业挂在课程章节页，页面是 JS 壳（tab 走 AJAX），
requests 四层逆向（v1.5.21）确认拿不到——只有真实浏览器执行 JS 后才能看到。

设计约束（v1.5.21 反爬实测教训，改前必读）：
- **失败绝不影响主采集**：任何异常都返回空结果，调用方只记 WARN
- **频率控制**：每轮 study 只抓 cursor 指向的少数课程（轮换），302 链每跳间隔 2 秒
- **cookie 复用**：注入 requests 登录态的 cookie，浏览器不做登录
- **UA 指纹**：与采集器一致（用户真实浏览器 Chrome/157）

产出（第一版为渲染诊断 + 通用解析）：
- 打开 302 链课程页 → 等待 JS 渲染 → 拦截全部 XHR URL（打码落日志）
- 解析渲染后 DOM 中的任务行（标题含「测试/测验/随堂/知识总结」或 tab 内任务列表）
- 结构未知的部分落日志（打码），供下一轮迭代——不许猜
"""
from __future__ import annotations

from utils import safe_text


def cookies_from_session(session) -> list[dict]:
    """requests cookie jar → playwright add_cookies 格式。纯函数，selftest 可测。"""
    out = []
    for c in session.cookies:
        out.append({
            "name": c.name,
            "value": c.value or "",
            "domain": c.domain or ".huuc.edu.cn",
            "path": c.path or "/",
        })
    return out


def collect_course_page(course: dict, stu_url: str, cookies: list[dict],
                        ua: str, click_tab: str | None = None) -> dict:
    """无头浏览器打开课程页（JS 渲染），返回诊断与渲染结果。

    click_tab 非空时点击该 tab（如「任务」）再取渲染文本——v1.5.22 第二版：
    课程页 tab 内容点击后才加载，「任务」tab 最可能含随堂作业。
    **安全**：返回的 dom_text 只含页面文本；调用方 dump 时必须只打任务相关行
    （页面菜单含用户姓名，全文进公开日志=泄露，v1.5.22 实测并删过 run）。
    返回 {"ok": bool, "xhr": [...], "dom_text": "...", "html_len": int, "error": str}
    任何失败都不抛异常（主采集保护）。
    """
    result: dict = {"ok": False, "xhr": [], "dom_text": "", "html_len": 0, "error": ""}
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            ctx = browser.new_context(user_agent=ua)
            if cookies:
                ctx.add_cookies(cookies)
            page = ctx.new_page()
            seen: set[str] = set()

            def _on_response(resp) -> None:
                u = resp.url
                if any(k in u for k in ("knowledge", "quiz", "work", "test",
                                        "datalist", "chapter", "module")) and u not in seen:
                    seen.add(u)
                    xhr_seen.append(u[:220])

            xhr_seen: list[str] = []
            page.on("response", _on_response)
            page.goto(stu_url, wait_until="domcontentloaded", timeout=60000)
            # 等 tab 的 AJAX 完成：networkidle 在学习通长轮询下可能永不空闲，用固定等待
            page.wait_for_timeout(8000)
            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass
            page.wait_for_timeout(2000)

            if click_tab:
                try:
                    page.get_by_text(click_tab, exact=True).first.click()
                    page.wait_for_timeout(7000)
                except Exception as e:
                    result["tab_error"] = f"{type(e).__name__}: {safe_text(str(e), 120)}"

            dom_text = safe_text(page.evaluate(
                "() => document.body ? document.body.innerText : ''"), 12000)
            html_len = page.evaluate("() => document.documentElement.outerHTML.length")
            # 学习通 tab 内容嵌在 iframe 里（外层 evaluate 拿不到）——遍历全部 frame
            frames_out: list[dict] = []
            try:
                for fr in page.frames:
                    try:
                        ft = fr.evaluate("() => document.body ? document.body.innerText : ''")
                        if ft and len(ft.strip()) > 200:
                            frames_out.append({"url": fr.url[:180],
                                               "len": len(ft),
                                               "text": safe_text(ft, 8000)})
                    except Exception:
                        continue
            except Exception:
                pass
            result["frames"] = frames_out
            result["frame_count"] = len(page.frames)
            result["frame_urls"] = [f.url[:150] for f in page.frames]
            result.update({"ok": True, "xhr": xhr_seen[:25], "dom_text": dom_text,
                           "html_len": html_len})
            ctx.close()
            browser.close()
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {safe_text(str(e), 200)}"
    return result


def extract_quiz_rows(dom_text: str) -> list[dict]:
    """从渲染后的页面文本里提取随堂任务行。纯函数（DOM innerText），selftest 可测。

    学习通课程页文本里任务行通常带状态词（待做/未交/已完成）与时间——
    第一版基于文本行启发式，拿到真实渲染样本后收紧为精确解析。
    """
    rows: list[dict] = []
    state_words = ("待做", "未交", "待提交", "未完成", "进行中", "待互评", "待批阅")
    for line in dom_text.splitlines():
        s = line.strip()
        if not s or len(s) < 4:
            continue
        if any(w in s for w in state_words) or "知识总结" in s or "随堂" in s:
            rows.append({"line": safe_text(s, 160)})
    return rows
