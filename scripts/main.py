"""统一入口。GitHub Actions 只调这一个文件，用模式参数决定干什么。

模式设计的核心约束：**finalize 必须零网络、零 token、40 秒内跑完**，
因为它每 20 分钟跑一次。登录类采集各自独立频率，互不拖累。

  python scripts/main.py tick       # ★唯一的定时入口：按 runtime.json 决定这轮干什么
  python scripts/main.py finalize   # 只渲染 + 离线重算课表，不联网
  python scripts/main.py diary      # Gitee 日记 → 渲染
  python scripts/main.py study      # 学习通 → 渲染
  python scripts/main.py notice     # 校园公告 → 渲染
  python scripts/main.py schedule   # 课表 → 渲染
  python scripts/main.py all        # 全部采集 → 渲染
  python scripts/main.py backfill   # 给老名言补 AI 点评 + 价值打分（一次性，联网）
  python scripts/main.py probe      # 海外 IP 连通性 + 解析冒烟
  python scripts/main.py selftest   # 解析器自检（离线，不需要凭据）
  python scripts/main.py morning    # 早 7 点飞书：作业 + 公告 + 一言 + 二次元图 + 中文问好
  python scripts/main.py noon       # 中午飞书：问好 + 鼓励 + 一言 + 二次元图
  python scripts/main.py night      # 晚上飞书：日记总结 + 一言 + 二次元图（不带课表）

为什么只有一个 cron（审计 §4.1）：原来 8 条 cron 各自映射一个模式，实测出现过
连续 1 小时 33 分钟一次都没触发。GitHub 对同一 workflow 的 schedule 有节流，
条数越多越容易被丢；而并发组只保留一个 pending，手动派发会把定时班挤掉。
现在改成一条 `7-59/20 * * * *`（每小时 :07/:27/:47，错开整点高峰）→ tick，
频率由 data/runtime.json 里的时间戳决定，
调度逻辑在 Python 里，可离线自检，也能在日志里看到「这轮为什么跑/不跑」。

三个推送模式只读 data/（diary + study + notice），不写数据、不渲染 README。
文案落到 /tmp/push-message.txt，图的信息落到 /tmp/push-image.json，由 workflow 的
飞书步骤读出来发卡片（无图/卡片被拒收时退回纯文本）。AI 或一言或图失败都降级推送，
不会静默不发。**常规采集成功不再发状态卡片**（用户 2026-11-14：信息太多了），
降噪闸门在 scripts/notify_feishu.js：只有失败/取消或 [urgent] 才发状态卡。
"""
from __future__ import annotations

import sys

from utils import DATA_DIR, exc_brief, load_json, log, now_bj, parse_datetime, save_json

COLLECT_MODES = ("diary", "study", "notice", "schedule")

# tick 的调度参数。单位分钟；判定留 5 分钟宽限，因为 Actions 的 cron 经常晚几分钟。
TICK_INTERVALS = {"study": 60, "diary": 120, "notice": 360}
TICK_SLACK_MIN = 5
# 飞书三时段（北京时间）。错过整点后在补发窗口内仍发，超过就不发了。
PUSH_HOURS = {"morning": 7, "noon": 12, "dusk": 18, "night": 22}
# 补发窗口按时段给，不能全局一个数：实测 GitHub 两次 schedule 触发间隔达 280 分钟
# （2026-11-13T20:20Z → 11-14T01:00Z），全局 3 小时会让整个窗口落在间隔里、推送被静默丢掉。
# morning 5h 正好补到 noon 时段；noon 5h 到 17:00 仍是下午；night 2h 不吵醒用户。
# target 取「今天的整点」，所以 night 实际只覆盖 22:00–24:00，跨零点不补。
PUSH_CATCHUP_HOURS = {"morning": 5, "noon": 5, "dusk": 2, "night": 2}
RUNTIME_PATH = DATA_DIR / "runtime.json"


def due_collectors(runtime: dict, now, intervals: dict | None = None) -> list[str]:
    """这轮该跑哪些采集器。纯函数，selftest 直接喂时间戳验证。

    同时看 last_ok 和 last_try：失败也按同样的间隔重试，不会每 20 分钟
    重跑一次登录链把 AI 额度和账号风控一起烧掉。
    """
    last_ok = runtime.get("last_ok") or {}
    last_try = runtime.get("last_try") or {}
    out = []
    for mode, mins in (intervals or TICK_INTERVALS).items():
        threshold = mins * 60 - TICK_SLACK_MIN * 60
        due = True
        for stamp in (last_ok.get(mode), last_try.get(mode)):
            dt = parse_datetime(stamp)
            if dt is not None and (now - dt).total_seconds() < threshold:
                due = False
                break
        if due:
            out.append(mode)
    return out


def due_pushes(runtime: dict, now) -> list[str]:
    """这轮该发哪些飞书推送。每种每天最多一次，按日期记账而不是按小时。"""
    today = now.date().isoformat()
    sent = runtime.get("pushed") or {}
    out = []
    for kind, hour in PUSH_HOURS.items():
        if sent.get(kind) == today:
            continue
        target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        hours = (now - target).total_seconds() / 3600
        if 0 <= hours <= PUSH_CATCHUP_HOURS[kind]:
            out.append(kind)
    return out


def do_tick() -> bool:
    """定时总入口：到点的采集 → 到点的推送 → finalize → 记账。

    任何一步失败都不中断后面的步骤，最后只要 finalize 成功就算这轮成功
    （README 是用户唯一看得见的产物）。失败详情写进 runtime.json 供下一轮和排查用。
    """
    import diary_writeback

    now = now_bj()
    rt = load_json(RUNTIME_PATH) or {}
    rt.setdefault("last_ok", {})
    rt.setdefault("last_try", {})
    rt.setdefault("pushed", {})
    rt.setdefault("errors", {})

    cfg = (load_json(DATA_DIR / "sources.json") or {}).get("diary") or {}
    window = cfg.get("writeback_window") or diary_writeback.DEFAULT_WINDOW
    todo = due_collectors(rt, now)
    # 凌晨写回窗口内必须跑一次 diary，否则「昨天计划写进今天日记」会整天空转。
    # 120 分钟的常规间隔不保证落在 6 小时窗口里，所以这里强制加一次。
    if (diary_writeback.in_window(now, window)
            and rt.get("writeback_date") != now.date().isoformat()
            and "diary" not in todo):
        todo.append("diary")
        log(f"[tick] 凌晨写回窗口 {window} 内今天还没写回，强制加跑 diary")

    pushes = due_pushes(rt, now)
    log(f"[tick] 本轮采集 {todo or '无'} · 推送 {pushes or '无'}")

    results = {}
    for m in todo:
        rt["last_try"][m] = now.isoformat(timespec="seconds")
        try:
            results[m] = bool(HANDLERS[m]())
        except Exception as e:
            log(f"[tick] {m} 异常：{exc_brief(e)}", level="ERROR")
            results[m] = False
            rt["errors"][m] = exc_brief(e)
        if results[m]:
            rt["last_ok"][m] = now.isoformat(timespec="seconds")
            rt["errors"].pop(m, None)
            if m == "diary" and diary_writeback.in_window(now, window):
                rt["writeback_date"] = now.date().isoformat()

    for kind in pushes:
        try:
            ok = do_push(kind)
        except Exception as e:
            log(f"[tick] 推送 {kind} 异常：{exc_brief(e)}", level="ERROR")
            ok = False
        results[kind] = ok
        if ok:
            rt["pushed"][kind] = now.date().isoformat()

    # 6 小时紧急截止提醒（用户 2026-11-14）：独立于三时段「每天一次」的通道，
    # 每条作业只提醒一次（runtime.urgent6 记账 task_id → 日期）。写 /tmp/push-urgent.txt，
    # workflow 的飞书步骤看到该文件就发，常规成功静默轮也不例外。
    try:
        import daily_push as dp
        msg, new_ledger = dp.urgent6_pick(now, rt.get("urgent6") or {})
        if msg:
            dp.URGENT_MESSAGE_PATH.write_text(msg, encoding="utf-8")
            rt["urgent6"] = new_ledger
            results["urgent6"] = True
            log(f"[urgent6] {msg.splitlines()[0]}")
    except Exception as e:
        log(f"[tick] 紧急提醒检查异常：{exc_brief(e)}", level="ERROR")
        results["urgent6"] = False

    # 新作业发布提醒（用户 2026-11-16：「首次检测到有新的作业发布的时候给我发
    # 一个消息，这个独立与其他的消息」+「发送的风格要和之前早中晚常规的信息
    # 保持一样，卡片类似的，每日英语，一言和图片」）：第三条独立通道，
    # 每条任务只通知一次（runtime.notified_new 记账 task_id → 日期）。
    # run_newtask_notice 把清单包装成与四时段同款的卡片文案（AI 人话 + 清单 +
    # 每日英语 + 一言，图片由 anime_image 落盘、飞书步骤拼卡片），
    # 常规成功静默轮也必达（带文本参数的调用不走降噪闸门）。
    try:
        msg_new, new_ledger2 = dp.new_task_pick(now, rt.get("notified_new") or {})
        if msg_new:
            card_new = dp.run_newtask_notice(msg_new, now)
            dp.NEW_MESSAGE_PATH.write_text(card_new, encoding="utf-8")
            results["newtask"] = True
            log(f"[newtask] {msg_new.splitlines()[0]}")
        rt["notified_new"] = new_ledger2
    except Exception as e:
        log(f"[tick] 新作业提醒检查异常：{exc_brief(e)}", level="ERROR")
        results["newtask"] = False

    try:
        results["finalize"] = do_finalize()
    except Exception as e:
        log(f"[tick] 渲染异常：{exc_brief(e)}", level="ERROR")
        results["finalize"] = False

    rt["last_tick"] = now.isoformat(timespec="seconds")
    rt["last_results"] = results
    save_json(RUNTIME_PATH, rt)
    log(f"[tick] 结果：{results}")
    return bool(results.get("finalize"))


def do_finalize() -> bool:
    import render_readme
    import week_svg
    # 课表解析是纯离线的，放在这里重跑一遍，教学周才会每天自动前进。
    # 否则 current_week / this_week 会停在最后一次手动跑 schedule 的那一周。
    do_schedule()
    ok = render_readme.run()
    week_svg.run()
    return ok


def do_schedule() -> bool:
    import schedule_parser
    return bool(schedule_parser.run().get("ok"))


def do_diary() -> bool:
    import diary_collector
    return bool(diary_collector.run().get("ok"))


def do_study() -> bool:
    import study_collector
    return bool(study_collector.run().get("ok"))


def do_notice() -> bool:
    import notice_collector
    return bool(notice_collector.run().get("ok"))


def do_probe() -> bool:
    """从 Actions runner 探测各站点可达性 + 解析器冒烟。

    这是整个架构唯一的致命未知：所有实测成功都来自国内出口 IP，
    GitHub Actions 是美国 Azure IP，学校站点可能直接拒连。

    审计 §4.4：原来只测连通性，站点返回 200 但页面改版照样「probe 全绿」。
    所以末尾加一段离线解析冒烟，用合成样本验证正则还能不能抽出东西。
    """
    import time
    import requests

    targets = [
        ("学习通登录", "https://passport2.huuc.edu.cn/login?fid=12"),
        ("学习通作业", "https://mooc1.huuc.edu.cn/mooc2/work/list"),
        ("学习通课程", "https://mooc2-ans.huuc.edu.cn/visit/courses/list"),
        ("学习通空间", "https://dzi.huuc.edu.cn/space/index?ws=2"),
        ("校网公告", "https://www.huuc.edu.cn/index/tzgg.htm"),
        ("教务处", "https://jwc.huuc.edu.cn/index.htm"),
        ("校团委", "https://tw.huuc.edu.cn/tzgg.htm"),
        ("科研处", "https://kyc.huuc.edu.cn/index/tzgg.htm"),
        ("研究生处", "https://yjsc.huuc.edu.cn/xkjs/tzgg.htm"),
        ("Gitee", "https://gitee.com/api/v5/user"),
        ("AI 中转", "https://api.chatanywhere.org/v1/models"),
        ("飞书", "https://open.feishu.cn/"),
        ("一言", "https://v1.hitokoto.cn/"),
        # 早报二次元图：nekos.best 是选定的 SFW 源（选型实测见 anime_image.py 文档串）。
        # 图床 i.pixiv.re 与作品页 www.pixiv.net 分开测：前者要能下载，后者只要用户点得开。
        ("二次元图 API", "https://nekos.best/api/v2/neko?amount=1"),
        ("Pixiv 图床", "https://i.pixiv.re/"),
        # v1.5.7 新数据源：午安每日英语（金山词霸）、晚安明日天气（open-meteo，均免费无 key）
        ("每日英语", "https://open.iciba.com/dsapi/"),
        ("明日天气", "https://api.open-meteo.com/v1/forecast?latitude=33.77&longitude=113.19"
                     "&daily=temperature_2m_max&timezone=Asia/Shanghai&forecast_days=1"),
        # v1.5.19 傍晚档科技热点：IT之家 RSS（中文，免费无 key；v1.5.19 曾用 HN 后换源）
        ("科技热点", "https://www.ithome.com/rss/"),
    ]
    ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36")
    try:
        ip = requests.get("https://api.ipify.org?format=json", timeout=15).json().get("ip")
        log(f"Actions 出口 IP：{ip}")
    except Exception as e:
        log(f"出口 IP 查询失败：{exc_brief(e)}", level="WARN")

    reachable = 0
    for name, url in targets:
        t0 = time.time()
        try:
            r = requests.get(url, headers={"User-Agent": ua}, timeout=20,
                             allow_redirects=True)
            ms = int((time.time() - t0) * 1000)
            # 401/403 也算「网络可达」，只是需要凭据；连不上才是真问题
            ok = r.status_code < 500
            reachable += 1 if ok else 0
            log(f"[probe] {name} → HTTP {r.status_code} ({ms}ms) {'可达' if ok else '不可达'}")
        except Exception as e:
            ms = int((time.time() - t0) * 1000)
            log(f"[probe] {name} → 失败 ({ms}ms): {type(e).__name__}", level="ERROR")
    log(f"可达：{reachable}")
    log(f"不可达：{len(targets) - reachable}")

    import study_collector
    smoke = study_collector.parse_smoke()
    bad = [k for k, v in smoke.items() if not v]
    log(f"[probe] 解析冒烟：{smoke}")
    if bad:
        log(f"[probe] 解析冒烟失败项：{bad}（页面结构可能已改版）", level="ERROR")
    return reachable > 0 and not bad


def do_selftest() -> bool:
    """离线自检：用内置样本验证解析器，不需要凭据也不需要网络。"""
    from datetime import datetime, timedelta

    from utils import BJ_TZ, parse_remaining, fmt_remaining
    import schedule_parser
    import study_collector
    import notice_collector

    failures = []

    def check(name, cond, detail=""):
        if cond:
            log(f"[selftest] ✅ {name}")
        else:
            log(f"[selftest] ❌ {name} {detail}", level="ERROR")
            failures.append(name)

    now = datetime(2026, 11, 12, 22, 0, tzinfo=BJ_TZ)

    rem = parse_remaining("剩余1小时12分钟")
    check("倒计时解析", rem is not None and rem.total_seconds() == 4320, str(rem))
    check("倒计时缺失返回 None", parse_remaining("已结束") is None)
    check("倒计时渲染", "剩余" in fmt_remaining(now.replace(hour=23), now=now))

    # 样本一律取自 study_collector.SAMPLE_*（合成 ID）。
    # 源码里写死真实 course_id/class_id 本身就是可定位信息泄露，而且会被下面的全仓库扫描扫到。
    rows = study_collector.parse_work_rows(study_collector.SAMPLE_WORK_HTML,
                                           study_collector.SAMPLE_COURSE, now)
    course = study_collector.SAMPLE_COURSE
    check("作业行数=2", len(rows) == 2, f"实际 {len(rows)}")
    if len(rows) == 2:
        # v1.7.0（用户 2026-11-16 定稿）：待批阅 = 已交等老师批，**不算待办**
        # （旧「互评模式下待批阅升级为 pending」规则已按用户澄清移除；
        # 漏互评提醒由「待互评」状态单独承担，交完互评后状态就是待互评）。
        check("互评待批阅不再升级为 pending（用户定稿：待批改不需动手）",
              rows[0]["state"] == "submitted", rows[0]["state"])
        check("倒计时换算截止时间", rows[0]["deadline"] == "2026-11-12T23:12:00+08:00",
              str(rows[0]["deadline"]))
        check("eval-list 识别为互评", rows[1]["kind"] == "eval", rows[1]["kind"])
        check("待互评为 pending", rows[1]["state"] == "pending", rows[1]["state"])
        check("ID 不含 enc", "abc123" not in rows[0]["id"] and "def456" not in rows[1]["id"])
    # v1.7.0：需要动手的状态集合 —— 待完成/待互评/已退回/待考试；
    # 不需动手的：已完成/待批改(submitted)/未开始(upcoming)
    check("已退回 归为 pending（退回要重做）",
          study_collector.classify("已退回") == "pending",
          study_collector.classify("已退回"))
    check("待批阅 归为 submitted（不需动手）",
          study_collector.classify("待批阅") == "submitted",
          study_collector.classify("待批阅"))

    erows = study_collector.parse_exam_rows(study_collector.SAMPLE_EXAM_HTML, course, now)
    check("考试行数=2（已完成 viewExamAnswer + 待考 goTest）", len(erows) == 2, f"实际 {len(erows)}")
    if len(erows) == 2:
        done_row = next(r for r in erows if r["state"] == "done")
        todo_row = next(r for r in erows if r["state"] == "pending")
        check("已完成考试状态与标题", done_row["title"] == "第二章测试题", done_row["title"])
        # v1.5.16 用户报「考试没收录」：待考行是 goTest(...) 形状，必须收录为 pending
        check("待考 goTest 行收录为 pending", todo_row["title"] == "期末考试"
              and todo_row["state"] == "pending", f"{todo_row['title']}/{todo_row['state']}")
        # v1.5.16 实测：goTest 参数 4 是页面给的精确截止时间（秒级），deadline 直接用它
        check("goTest 第 4 参是精确截止时间（deadline_exact=True）",
              todo_row["deadline"].startswith("2026-11-15T14:00")
              and todo_row["deadline_exact"] is True,
              f"{todo_row['deadline']}/{todo_row['deadline_exact']}")
        check("待考深链是 examnotes 且带 examId",
              "examnotes" in todo_row["url"] and "examId=11087147" in todo_row["url"],
              todo_row["url"])

    check("未交 归为 pending", study_collector.classify("未交") == "pending",
          study_collector.classify("未交"))
    # v1.6.5→v1.6.6（用户澄清定稿）：「未开始」单独成类 upcoming，
    # **留在清单里显示**（标「未开始」），不是剔除对象。
    check("未开始 归为 upcoming（与待完成区分）",
          study_collector.classify("未开始") == "upcoming",
          study_collector.classify("未开始"))
    check("待考试 仍是 pending（考试可备考，不受未开始误伤）",
          study_collector.classify("待考试") == "pending")

    # 清单保留能做的和未开始的；剔除只有两类：非待完成、已逾期。
    # 「开始超过一周就剔除」已按用户要求废除（v1.3.0），下面有专门的回归断言。
    import render_readme as rr

    def mk(**kw):
        return dict({"state": "pending", "course_id": "c1", "title": "t",
                     "kind": "work", "url": "u"}, **kw)

    iso = lambda d: d.isoformat(timespec="seconds")
    sample = [
        mk(),                                                  # 无截止无开始 → 留
        mk(state="done", first_seen=iso(now - timedelta(days=9))),  # 已完成 → 剔（开始列退化）
        mk(state="submitted"),                                 # 已批阅 → 剔
        mk(state="upcoming"),                                   # 未开始 → 留（v1.6.6，状态区分显示）
        mk(deadline=iso(now - timedelta(hours=1))),             # 已逾期 → 剔
        mk(start_time=iso(now - timedelta(days=8))),            # 开始超一周 → 现在留
        mk(start_time=iso(now - timedelta(days=2))),            # 开始两天前 → 留
        mk(first_seen=iso(now - timedelta(days=9))),            # 只有 first_seen → 现在留
    ]
    kept, dropped = rr.actionable(sample, now)
    check("可做过滤：留 5 剔 3（未开始也留）", len(kept) == 5 and sum(dropped.values()) == 3,
          f"kept={len(kept)} dropped={dropped}")
    check("逾期任务不进清单", dropped["overdue"] == 1, str(dropped))
    check("已完成/已批阅不进清单", dropped["not_pending"] == 2, str(dropped))
    check("未开始留在清单（不算剔除，drop_reason=None）",
          rr.drop_reason(mk(state="upcoming"), now) is None
          and "not_started" not in dropped)
    check("一周规则已废除：开始 8 天前仍留在清单",
          "stale" not in dropped
          and rr.drop_reason(mk(start_time=iso(now - timedelta(days=8))), now) is None)
    check("一周规则已废除：first_seen 9 天前仍留在清单",
          rr.drop_reason(mk(first_seen=iso(now - timedelta(days=9))), now) is None)
    check("剔除原因两类（逾期/非待完成；未开始是显示分类不是剔除）",
          rr.REASON_ORDER == ("overdue", "not_pending"),
          str(rr.REASON_ORDER))
    # 未开始在清单里的显示：单元格标「未开始」、计数另附、不计「截止未知」
    _uc_rows = rr.course_rows(
        [{"course_id": "c1", "class_id": "", "name": "示例课", "task_count": 2}],
        [mk(title="能做的"), mk(state="upcoming", title="未开始的那条")])
    _uc_line = next(r for r in _uc_rows if "未开始的那条" in r)
    check("未开始单元格标「未开始」且不标截止未知",
          "[未开始的那条](u)（作业·未开始）" in _uc_line
          and "[未开始的那条](u)（作业·⚠️" not in _uc_line,
          _uc_line[:160])
    check("未开始计数另附在待完成列",
          any("1（另 1 项未开始）" in r for r in _uc_rows), str(_uc_rows))
    _uc_stats = rr.render_tasks(sample, now)[2]
    check("未开始计入独立统计并提示自动进待办",
          _uc_stats["upcoming"] == 1
          and "1 项未开始（老师开放后自动进入待办）" in _uc_stats["suffix"],
          str(_uc_stats))

    # 被剔除的任务要逐项留档：判定与清单共用 drop_reason()，条数必须对得上
    ex_body, ex_n = rr.render_excluded(sample, now)
    check("已剔除条数与过滤统计一致", ex_n == sum(dropped.values()), f"{ex_n} vs {dropped}")
    check("已剔除逐项写明两类原因（未开始不在剔除文档里）",
          all(k in ex_body for k in ("已逾期", "非待完成")) and "未开始" not in ex_body,
          ex_body[:120])
    check("已剔除不再出现开始过久", "开始过久" not in ex_body)
    check("开始时间退化标注首次发现", "（首次发现）" in ex_body)
    check("无剔除时给说明不给空表",
          rr.render_excluded([], now) == ("_本次没有被剔除的任务。_\n", 0))
    note = rr.render_tasks(sample, now)[1]
    check("已剔除文档链接挂在清单最下面",
          note.rstrip().splitlines()[-1].endswith("(docs/已剔除任务.md)"), note)
    check("清单底部计数不再提一周规则", "开始超过" not in note, note)

    # 已剔除文档按课程分组：课程顺序必须与任务清单的活跃度排序一致
    gc = [{"course_id": "c1", "class_id": "", "name": "高等数学A", "task_count": 14},
          {"course_id": "c2", "class_id": "", "name": "军事理论", "task_count": 1}]
    gsample = [
        mk(state="done", course="高等数学A", title="高数已交"),
        mk(state="submitted", course="高等数学A", title="高数已批"),
        mk(course="军事理论", course_id="c2", title="军理逾期",
           deadline=iso(now - timedelta(hours=1))),
    ]
    gb, gn = rr.render_excluded(gsample, now, gc)
    check("已剔除按课程分节", "### 高等数学A（2 项）" in gb and "### 军事理论（1 项）" in gb, gb)
    check("已剔除课程顺序随活跃度", gb.index("### 高等数学A") < gb.index("### 军事理论"))
    check("已剔除表头去掉课程列",
          "| 任务 | 类型 | 截止 | 开始 | 剔除原因 |" in gb and "| 课程 | 任务 |" not in gb)
    check("已剔除分节数等于有剔除的课数", gb.count("### ") == 2 and gn == 3, f"{gn}")
    check("courses.json 里没有的课也不丢",
          "### 孤儿课（1 项）" in rr.render_excluded(
              [mk(state="done", course="孤儿课", course_id="zz")], now, gc)[0])

    crows = rr.course_rows([{"course_id": "c1", "name": "高等数学A", "task_count": 14},
                            {"course_id": "c2", "name": "军事理论", "task_count": 1}], kept)
    joined = "\n".join(crows)
    check("无任务的课程也列出", len(crows) == 4 and "军事理论" in joined, f"rows={len(crows)}")
    check("无任务写「无」", "**无**" in joined)
    check("无 latest 时退回活跃度排序", joined.index("高等数学A") < joined.index("军事理论"))

    # v1.7.0（用户定稿）：课程按「最后一次发布作业的时间」降序——谁的消息
    # 最新谁靠前。含已剔除任务（问的是课最近活不活跃，不问还有没有能做的）。
    late = {("c1", ""): "2026-11-12T10:00:00", ("c2", ""): "2026-11-11T08:00:00"}
    _late_rows = rr.course_rows(
        [{"course_id": "c1", "name": "A课", "task_count": 1},
         {"course_id": "c2", "name": "B课", "task_count": 9}], [], late)
    check("有 latest 时按最新发布降序",
          "\n".join(_late_rows).index("A课") < "\n".join(_late_rows).index("B课"),
          "\n".join(_late_rows)[:120])
    check("latest_by_course 取每课最大 start 锚（含已剔除任务）",
          rr.latest_by_course([
              mk(course_id="c1", title="旧作业", first_seen=iso(now - timedelta(days=5))),
              mk(course_id="c1", title="新作业", first_seen=iso(now - timedelta(hours=1))),
              mk(course_id="c1", state="done", title="新但已完成",
                 first_seen=iso(now))])[("c1", "")] == iso(now))

    # 实测：同一门英语课挂在两个教学班下（course_id 相同、class_id 不同），
    # 只按 course_id 归组会让两个班显示同一批任务
    twin = [{"course_id": "e1", "class_id": "k1", "name": "英语一班", "task_count": 0},
            {"course_id": "e1", "class_id": "k2", "name": "英语二班", "task_count": 3}]
    trows = rr.course_rows(twin, [mk(course_id="e1", class_id="k2", title="翻译作业")])
    check("同 course_id 不同班只归到本班",
          "翻译作业" in trows[2] and "翻译作业" not in trows[3], "\n".join(trows))
    check("兄弟班显示无", "**无**" in trows[3])

    # 用户先要求把公告/计划移出 README 瘦身，后又要求加回来。现在**两边都有**：
    # README 内嵌精简版（上限更小、链接基准是仓库根），docs/ 下仍是全量版。
    # 这两条守卫的方向随用户要求翻转过一次，别照抄旧结论。
    tpl = rr.TEMPLATE.read_text(encoding="utf-8")
    check("README 内嵌公告节", "## 📢 值得关注的公告" in tpl)
    check("README 内嵌计划节", "## 🎯 潜在计划" in tpl)
    check("README 精简版上限小于全量版",
          rr.README_NOTICE_LIMIT < rr.NOTICE_LIMIT and rr.README_PLAN_LIMIT < rr.PLAN_LIMIT,
          f"{rr.README_NOTICE_LIMIT}/{rr.NOTICE_LIMIT} {rr.README_PLAN_LIMIT}/{rr.PLAN_LIMIT}")
    # README 在仓库根，截断行不能带 ../（那是 docs/ 下产物的基准）。审计 §5.1 同型坑。
    _rn, _ = rr.render_notices(rr.README_NOTICE_LIMIT, link_prefix="")
    _rp, _ = rr.render_plans(rr.README_PLAN_LIMIT, link_prefix="")
    check("README 版公告截断行不带 ../", "../data/" not in _rn)
    check("README 版计划截断行不带 ../", "../data/" not in _rp)
    # 默认值必须是 docs/ 基准。查签名而不是查渲染结果 —— 后者依赖「数据至少 2 条」，
    # 数据一变守卫就假红，selftest 必须离线确定性。
    import inspect as _insp
    check("两个渲染器默认 link_prefix 仍是 ../",
          _insp.signature(rr.render_notices).parameters["link_prefix"].default == "../"
          and _insp.signature(rr.render_plans).parameters["link_prefix"].default == "../")
    check("README 目录区链接到三份独立文档",
          "docs/公告.md" in tpl and "docs/潜在计划.md" in tpl
          and "docs/已剔除任务.md" in tpl)
    check("三份独立文档模板存在",
          rr.NOTICE_TEMPLATE.exists() and rr.PLANS_TEMPLATE.exists()
          and rr.EXCLUDED_TEMPLATE.exists())

    # 抓包永久禁令：脱敏版曾漏出班级 ID，已删文件并抹除历史，不许再放进来
    hars = [p.name for p in rr.REPO_ROOT.rglob("*.har")]
    check("仓库内无抓包文件", not hars, str(hars))
    gi = (rr.REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    check("gitignore 拒绝 har 且不开例外",
          "*.har" in gi and "!assets/*-脱敏.har" not in gi)

    # 课程名里嵌过同学学号（8 位数字串），已渲染进公开 README。
    # 采集层 scrub_course_name() 负责清洗，这里兜底断言清洗结果与全仓库残留。
    # 测试数据用代码合成：源码里写死真实学号本身就是泄露，而且会被下面的扫描扫到。
    import re as _re2
    from utils import scrub_course_name
    ids = ",".join(str(9 * 10**7 + i) for i in range(4))
    leaked = f"计算思维与人工智能{ids}(111)"
    check("课程名去掉学号与教学班号",
          scrub_course_name(leaked) == "计算思维与人工智能", scrub_course_name(leaked))
    keep = ["高等数学A", "2026级-校规校纪", "新世纪大学英语综合教程(一)",
            "2026-2027第一学期星期一下午七八体育课", "军事理论（同济大学版）"]
    check("正常课程名不被误伤",
          all(scrub_course_name(n) == n for n in keep),
          str([n for n in keep if scrub_course_name(n) != n]))
    names = []
    for f in ("courses.json", "tasks.json"):
        p = rr.REPO_ROOT / "data" / "study" / f
        if p.exists():
            names += _re2.findall(r'"(?:name|course)"\s*:\s*"([^"]*)"',
                                  p.read_text(encoding="utf-8"))
    bad_names = [n for n in names if _re2.search(r"\d{7,}", n)]
    check("学习通落盘课程名无学号", not bad_names, str(bad_names))

    # 全仓库扫学号形状：修 bug 时把真实学号抄进文档同样是泄露，只扫 data/ 扫不到。
    # 三种形状：① 逗号串起的长数字串 ② 汉字紧跟 7 位以上数字 ③ ID 标签 + 可选空白 + 长数字。
    # ② 必须排掉 20YYMMDD 日期 —— 实测任务标题「作业20261022」是合法的，不能误报。
    # 也不能扫「任意 7 位以上数字」：course_id / cpi / ICS UID 全是长数字，会全库飘红。
    pats = [_re2.compile(r"\d{7,}[,，、]\d{7,}"),
            _re2.compile(r"[\u4e00-\u9fff]"
                         r"(?!20\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01]))\d{7,}"),
            # ③ 2026-11-14 补的洞：②要求汉字与数字**紧挨**，「学号 + 空格 + 数字」能溜过去。
            #    只认 ID 标签而不是任意汉字，否则「分别是 <class_id>」这种合法教学班号会误报。
            #    这里不写具体数字示例：写了会被本守卫自己扫到（实测过，红了一条）。
            _re2.compile(r"(?:学号|工号|学籍号|身份证号|考生号)\s*[:：]?\s*\d{7,}")]
    exts = {".md", ".py", ".json", ".yml", ".yaml", ".js", ".html", ".svg", ".ics", ".txt"}
    hits = []
    for p in sorted(rr.REPO_ROOT.rglob("*")):
        if ".git" in p.parts or "__pycache__" in p.parts or not p.is_file():
            continue
        if p.suffix.lower() not in exts:
            continue
        try:
            txt = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for pat in pats:
            # 失败详情会进**公开** Actions 日志，所以只报形状不报值：数字一律掩成 D。
            # 定位靠文件名 + 形状就够了，把真实学号打出来等于守卫自己在泄露。
            hits += [f"{p.relative_to(rr.REPO_ROOT)}:"
                     f"{_re2.sub(r'[0-9]', 'D', m.group(0)[:20])}"
                     for m in pat.finditer(txt)]
    check("全仓库无学号形状残留", not hits, str(hits[:8]))

    # 私人路径黑名单的**反向守卫**：黑名单的值只在 Secret 里，所以这里不能写死路径名
    # （写死本身就是泄露）。改成运行时读 DIARY_EXCLUDE，有值就断言全仓库不含这些前缀。
    # 2026-11-14 实测抓到过两个漏点：state.json 的键就是文件路径、技术路径.md 抄了真实目录名。
    # 本地没设这个环境变量时跳过（不是失败）。Actions 里之所以每轮都有，是因为
    # **质量门禁与执行两个步骤都注入了它** —— 只注入执行步时这条守卫在普通 tick 里
    # 永远跳过（实测踩过，见审计 §9.6 缺陷 A）。下面有断言锁住注入次数 == 2，别删。
    import diary_collector as _dc
    _prefixes = _dc.excluded_prefixes()
    if _prefixes:
        # 既查完整前缀，也查**最后一段目录名**：实测的漏点是文档里只写了叶子名，
        # 完整前缀匹配抓不到它。目录名本身就是隐私，所以这里也不写出真实名字。
        _needles = []
        for pref in _prefixes:
            _needles.append(pref)
            leaf = pref.rstrip("/").rsplit("/", 1)[-1]
            if leaf and leaf not in _needles:
                _needles.append(leaf)
        leak = []
        for p in sorted(rr.REPO_ROOT.rglob("*")):
            if ".git" in p.parts or "__pycache__" in p.parts or not p.is_file():
                continue
            if p.suffix.lower() not in exts:
                continue
            try:
                txt = p.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            # 只报「第几个黑名单项命中」，**绝不报命中内容**：失败详情会进公开
            # Actions 日志，把私人目录名打出来就是守卫自己在泄露（与学号守卫同理）。
            leak += [f"{p.relative_to(rr.REPO_ROOT)}:私人路径#{i}"
                     for i, n in enumerate(_needles) if n in txt]
        check("私人路径不出现在仓库任何文件里", not leak, str(leak[:8]))
    else:
        log("[selftest] ⏭️ 跳过私人路径守卫（本次进程未设 DIARY_EXCLUDE；带 Secret 的那次运行会检查）")

    # 用户硬要求：所有说明性文档都必须能在 README 目录里找到。
    # 新增文档忘了挂链接，这条会直接红，不用等人肉发现。
    docs = sorted((rr.REPO_ROOT / "docs").glob("*.md"))
    docs += [rr.REPO_ROOT / n for n in ("AGENT.md", "DEV_GUIDE.md", "CHANGELOG.md")]
    missing = [p.name for p in docs
               if f"docs/{p.name}" not in tpl and f"]({p.name})" not in tpl]
    check("所有说明性文档都挂进 README 目录", not missing, str(missing))

    # 占位符漏一个就会在页面上留下字面量 {{X}}，这里做结构性守卫
    import inspect
    import re as _re
    provided = set(_re.findall(r'"(\{\{[A-Z_]+\}\})"', inspect.getsource(rr)))
    wanted = set()
    for t in (rr.TEMPLATE, rr.NOTICE_TEMPLATE, rr.PLANS_TEMPLATE, rr.EXCLUDED_TEMPLATE):
        wanted |= set(_re.findall(r"\{\{[A-Z_]+\}\}", t.read_text(encoding="utf-8")))
    check("模板占位符全部有渲染键", not (wanted - provided), str(sorted(wanted - provided)))

    nret, pret = rr.render_notices(), rr.render_plans()
    check("公告渲染返回(文本,条数)",
          isinstance(nret, tuple) and len(nret) == 2 and isinstance(nret[1], int), str(nret)[:80])
    check("计划渲染返回(文本,条数)",
          isinstance(pret, tuple) and len(pret) == 2 and isinstance(pret[1], int), str(pret)[:80])

    # 实测样本：互评详情页给真实窗口，work 详情页只有空标签「作答时间:」（值由 JS 填）
    ws, we = study_collector.detail_eval_window("<p>互评时间：11-06 07:01 至 11-10 23:01</p>", now)
    check("互评窗口开始时间", ws is not None and (ws.month, ws.day, ws.hour) == (11, 6, 7), str(ws))
    check("互评窗口结束即截止", we is not None and (we.day, we.hour) == (10, 23), str(we))
    check("窗口已结束的互评会被剔除", we is not None and we < now)
    check("空标签不产生开始时间", study_collector.detail_start("<p>作答时间:</p>") is None)

    # 三时段推送：事实清单和降级文案都是确定性的，离线可测（一言和 AI 不测，要联网）
    import daily_push as dp

    facts = dp.diary_facts("morning", now.date())
    # 用户 2026-11-14：「随口写的（潜在计划）不要发到飞书里」→ 事实清单里不许再有计划播报
    check("飞书事实不含计划播报（用户 2026-11-14 要求）",
          "未完成计划" not in facts and "今天到期" not in facts and "已逾期" not in facts)
    check("早报不带课表", "课表" not in facts and "上课" not in facts)
    for k in dp.KINDS:
        fb = dp.fallback_message(k, facts, "测试一言（来源）")
        check(f"{k} 降级文案含事实与一言", len(fb) > 20 and "测试一言" in fb)

    # 6h 紧急截止提醒（用户 2026-11-14）：纯函数，离线可测。注入合成任务。
    u_now = now_bj()
    from datetime import timedelta as _td
    def _mk_u(tid: str, hours: float, state: str = "pending") -> dict:
        return {"id": tid, "course": "测试课", "title": f"作业{tid}", "state": state,
                "deadline": (u_now + _td(hours=hours)).isoformat(timespec="seconds"),
                "url": f"https://example.com/{tid}", "first_seen": u_now.date().isoformat()}
    msg, led = dp.urgent6_pick(u_now, {}, [
        _mk_u("u1", 3), _mk_u("u2", 9), _mk_u("u3", 3, state="done"), _mk_u("u4", -1)])
    check("紧急提醒只挑 6h 内待完成（3h 选中 / 9h、已完成、逾期不选）",
          "作业u1" in msg and "作业u2" not in msg and "作业u3" not in msg and "作业u4" not in msg)
    msg2, led2 = dp.urgent6_pick(u_now, led, [
        _mk_u("u1", 3), _mk_u("u5", 1)])
    check("已提醒过的作业不再提醒（同一截止时间只提醒一次）",
          "作业u1" not in msg2 and "作业u5" in msg2
          and any(k.startswith("u1@") for k in led2) and any(k.startswith("u5@") for k in led2))
    # v1.5.12 修的漏洞：老师把截止时间往后改，同一作业进入新 6h 窗口必须重新提醒
    msg3, _led3 = dp.urgent6_pick(u_now, led2, [_mk_u("u1", 5)])
    check("截止时间被改（延期）后重新提醒",
          "作业u1" in msg3, msg3[:80])
    msg0, led0 = dp.urgent6_pick(u_now, {"old@2026-01-01T00:00:00+08:00": "2026-01-01"},
                                 [_mk_u("u9", 2)])
    check("超过 30 天的旧账自动清理（账本不无限膨胀）",
          "old@2026-01-01T00:00:00+08:00" not in led0 and "作业u9" in msg0)
    check("urgent6 断言不看 deadline 字段漂移（签名去重在 urgent_alerts，这里按 id@deadline 记账）",
          isinstance(led2, dict))

    # 新作业发布提醒（用户 2026-11-16）：第三条独立通道。纯函数，离线可测。
    # 用固定 now（NEW_SINCE 之后）保证确定性，不跟运行日期漂移。
    n_now = datetime(2026, 11, 16, 18, 0, tzinfo=BJ_TZ)
    def _mk_n(tid: str, fs_hour: float, state: str = "pending",
              deadline: str | None = "2026-11-20T10:00:00+08:00") -> dict:
        fs = n_now - _td(hours=fs_hour)
        return {"id": tid, "course": "测试课", "title": f"作业{tid}", "state": state,
                "label": "随堂", "deadline": deadline,
                "url": f"https://example.com/{tid}",
                "first_seen": fs.isoformat(timespec="seconds")}
    n_msg, n_led = dp.new_task_pick(n_now, {}, [
        _mk_n("n1", 0.5),                                   # 上线后 30 分钟 → 新
        _mk_n("n2", 0.5, state="done"),                    # 已完成 → 噪音
        _mk_n("n3", 1.5),                                   # 上线后 90 分钟 → 新
        {"id": "n4", "state": "pending", "title": "旧任务",
         "first_seen": "2026-11-16T13:23:00+08:00"},        # NEW_SINCE 之前 → 存量不追溯
    ])
    check("新作业只挑上线后首次发现的待完成（done/存量不选）",
          "作业n1" in n_msg and "作业n3" in n_msg
          and "作业n2" not in n_msg and "旧任务" not in n_msg, n_msg[:120])
    check("新作业消息带课程/标签/截止/链接",
          "测试课 · 作业n1 · 随堂" in n_msg and "截止 11-20 10:00" in n_msg
          and "https://example.com/n1" in n_msg, n_msg[:160])
    check("新作业记账按 task_id（每条只通知一次）",
          n_led.get("n1") == "2026-11-16" and n_led.get("n3") == "2026-11-16"
          and "n2" not in n_led, str(n_led))
    n_msg2, n_led2 = dp.new_task_pick(n_now, n_led, [
        _mk_n("n1", 0.5), _mk_n("n5", 0.2)])
    check("已通知过的新作业不再通知（记账去重）",
          "作业n1" not in n_msg2 and "作业n5" in n_msg2, n_msg2[:80])
    # 48h 窗口要用「上线多天后的 now」才能触发（上线当天的历史不足以超窗）
    n_late = datetime(2026, 11, 20, 12, 0, tzinfo=BJ_TZ)
    # 48h 窗口要用「上线多天后的 now」才能触发（上线当天的历史不足以超窗）。
    # fs 手写内联：_mk_n 的时间基点是 n_now，这里必须相对 n_late 算。
    n_msg3, _ = dp.new_task_pick(n_late, {}, [
        {"id": "n6", "state": "pending", "title": "作业n6",
         "first_seen": "2026-11-17T12:00:00+08:00"},          # n_late 前 3 天 → 超窗口
        {"id": "n7", "state": "pending", "title": "作业n7",
         "first_seen": "2026-11-19T20:00:00+08:00"},          # n_late 前 16h → 窗口内
    ])
    check("首次发现超过 48h 不再当「新」（停摆补报上限）",
          "作业n6" not in n_msg3 and "作业n7" in n_msg3, n_msg3[:80])
    n_msg4, n_led4 = dp.new_task_pick(
        n_now, {"old": "2025-10-01"}, [_mk_n("n9", 1)])
    check("新作业账本 30 天旧账自动清理", "old" not in n_led4 and "作业n9" in n_msg4)
    n_msg5, _ = dp.new_task_pick(n_now, {}, [_mk_n("n8", 1, deadline=None)])
    check("无截止的新作业如实标「截止未知」", "截止未知" in n_msg5, n_msg5[:80])
    n_msg9, _ = dp.new_task_pick(n_now, {}, [_mk_n("n10", 0.5, state="upcoming",
                                                   deadline="2026-11-25T10:00:00+08:00")])
    check("新发布但未开始的也通知一次（如实标「未开始」）",
          "作业n10" in n_msg9 and "未开始（11-25 10:00）" in n_msg9, n_msg9[:120])
    # v1.6.6（用户报的 bug）：体育课三条历史课次（同 id、截止已过）被当成
    # 「新发布」连报三遍。两条闸：已逾期不报 + 同 id 只报一次。
    n_msg10, _ = dp.new_task_pick(n_now, {}, [
        _mk_n("n11", 0.5, deadline=(n_now - _td(hours=1)).isoformat(timespec="seconds")),
        _mk_n("n12", 0.5),
        _mk_n("n12", 0.5, deadline="2026-11-21T10:00:00+08:00"),  # 同 id 重复行
    ])
    check("新作业：首次发现已逾期的不报（历史课次不是新发布）",
          "作业n11" not in n_msg10, n_msg10[:120])
    check("新作业：同 id 的重复行只报一次",
          n_msg10.count("作业n12") == 1
          and n_msg10.count("https://example.com/n12") == 1, n_msg10[:120])
    n_msg6, _ = dp.new_task_pick(n_now, {},
                                 [_mk_n(f"m{i}", 1) for i in range(10)])
    check("新作业超过 8 条截断并计数（另有 N 条）",
          "另有 2 条" in n_msg6 and "m8" not in n_msg6 and "m9" not in n_msg6,
          n_msg6[:60])
    check("新作业通道接线在 do_tick 与飞书步骤",
          "new_task_pick" in inspect.getsource(do_tick)
          and "newtask" in inspect.getsource(do_tick))
    check("新作业推送走卡片化包装（与早中晚同款：AI+清单+英语+一言+图）",
          "run_newtask_notice" in inspect.getsource(do_tick))

    # v1.6.6：run_newtask_notice 卡片化包装（用户要求风格与四时段一致）
    class FakeNewAI:
        def __init__(self, text):
            self.text = text
            self.calls = 0

        def chat_text(self, messages, **kw):
            self.calls += 1
            return self.text

    _nt_plain = ("🆕 新作业发布：首次检测到 1 条\n"
                 "- 测试课 · 作业X · 随堂 · 截止 11-20 10:00\n"
                 "  ↳ https://example.com/x")
    _nt_ai = FakeNewAI("哟，来新活了。")
    _nt_card = dp.run_newtask_notice(_nt_plain, ai_client=_nt_ai,
                                     fetch_extras=False,
                                     hit="测试一言（来源）", en="test line\n测试翻译")
    check("新作业卡片：AI 人话开头 + 清单 + 每日英语 + 一言",
          _nt_card.startswith("哟，来新活了。") and _nt_plain in _nt_card
          and "今日英语一句\ntest line" in _nt_card
          and "今日一言：测试一言（来源）" in _nt_card and _nt_ai.calls == 1,
          _nt_card[:160])
    _nt_fb = dp.run_newtask_notice(_nt_plain, fetch_extras=False)
    check("新作业卡片：AI 挂了走人设固定开头，清单仍在",
          _nt_fb.startswith(dp.NEWTASK_FALLBACK) and _nt_plain in _nt_fb,
          _nt_fb[:120])

    # urgent6 不催未开始：6h 窗口内的 upcoming 不入选
    msg_up, _ = dp.urgent6_pick(u_now, {}, [
        _mk_u("up1", 3, state="upcoming")])
    check("紧急提醒不催未开始（做不了的不催）", "作业up1" not in msg_up, msg_up[:80])

    # 早报清单：未开始的单独标注、不计待完成数
    _sf_txt, _sf_kept = dp.study_facts(
        now, [dict(state="pending", course="测试课", title="常规作业",
                   deadline=iso(now + timedelta(days=2)), deadline_exact=True,
                   url="https://example.com/a"),
              dict(state="upcoming", course="测试课", title="未开始的作业", url="u2")])
    check("早报清单：待完成 1 条 + 另有 1 条未开始",
          "待完成作业 1 条" in _sf_txt and "另有 1 条未开始（老师开放后自动进入待办）" in _sf_txt
          and "未开始的作业 · 未开始（暂不可做）" in _sf_txt and "常规作业" in _sf_txt,
          _sf_txt[:160])

    # 公告时效过滤（用户 2026-11-14：「时间都截至了还给我发，改掉」）
    from utils import notice_stale as _stale
    check("公告截止已过 → 过时效",
          _stale({"deadline": "2026-01-01", "publish_date": "2025-12-01"}, now.date()))
    check("公告截止未到 → 有效（发布再老也留）",
          not _stale({"deadline": (now + _td(days=30)).date().isoformat(),
                      "publish_date": "2025-06-01"}, now.date()))
    check("无截止且发布超 90 天 → 过时效",
          _stale({"deadline": None, "publish_date": (now - _td(days=91)).date().isoformat()},
                 now.date()))
    check("无截止但发布 89 天 → 有效",
          not _stale({"deadline": None, "publish_date": (now - _td(days=89)).date().isoformat()},
                     now.date()))
    check("deadline 为 'None' 字符串不误判",
          not _stale({"deadline": "None", "publish_date": (now - _td(days=1)).date().isoformat()},
                     now.date()))
    check("README 公告节与飞书早报共用 notice_stale 判定",
          "notice_stale" in inspect.getsource(rr) and "notice_stale" in inspect.getsource(dp))
    # 看板（docs/data/notices.json）也必须同口径：导出层过滤，前端零计算（app.js 头注释的约定）
    check("看板公告导出也过时效过滤（与 README 同口径）",
          "notice_stale" in inspect.getsource(rr.run))

    # 名言只放 3 条、由 AI 的 value 分挑（用户 2026-11-14）。挑选是纯函数，离线可验。
    check("名言 README 上限为 3", rr.QUOTE_LIMIT == 3, str(rr.QUOTE_LIMIT))
    qs = [{"id": "a", "first_seen": "2026-11-14", "value": 2},
          {"id": "b", "first_seen": "2026-11-01", "value": 9},
          {"id": "c", "first_seen": "2026-11-10", "value": 6},
          {"id": "d", "first_seen": "2026-11-13"},
          {"id": "e", "first_seen": "2026-11-12", "value": 9}]
    got = [q["id"] for q in rr.pick_quotes(qs)]
    check("名言按 AI 价值降序取前 3（同分新的在前）", got == ["e", "b", "c"], str(got))
    check("没打分的名言排最后", rr.pick_quotes(qs, 5)[-1]["id"] == "d")
    check("名言不足 3 条时不开天窗", len(rr.pick_quotes(qs[:2])) == 2)
    check("一条名言都没有时返回空列表", rr.pick_quotes([]) == [])
    # 打分只在联网模式里做，finalize 不许碰网络（铁律 5）。这两条接线断了
    # README 就会永远显示 0 分排序，而且不报错 —— 所以锁住源码里的调用关系。
    _bf_src = inspect.getsource(_dc.backfill_comments)
    check("backfill 模式连带补价值分", _bf_src.count("score_quotes()") == 2,
          str(_bf_src.count("score_quotes()")))
    check("diary 采集当场给新名言打分",
          "score_quotes()" in inspect.getsource(_dc.run))
    check("backfill 入口接到补点评函数",
          "backfill_comments" in inspect.getsource(HANDLERS["backfill"]))

    # 早报清单：用户点名的作业 + 公告 + 一言必须在，且清单不经过 AI（prose/清单分离）。
    blk = dp.info_block("morning", now, "测试一言（来源）")
    check("早报清单含待完成作业", "待完成作业" in blk)
    check("早报清单含值得关注的公告", "值得关注的公告" in blk)
    # 用户 2026-11-15：发布时间直接当开始时间；缺日期的也要明说「未知」
    check("早报公告行带开始时间（即发布日期）",
          bool(_re.search(r"开始 \d{4}-\d{2}-\d{2}", blk)) or "开始时间未知" in blk, blk[:200])
    check("早报清单含一言", "今日一言" in blk and "测试一言" in blk)
    check("四个时段的清单都不带课表",
          all("课表" not in dp.info_block(k, now, "", tech_provider=lambda: "")
              and "上课" not in dp.info_block(k, now, "", tech_provider=lambda: "")
              for k in dp.KINDS))
    merged_fb = dp.fallback_message("morning", facts, "") + "\n\n" + blk
    check("降级路径里一言只出现一次", merged_fb.count("今日一言") == 1,
          str(merged_fb.count("今日一言")))
    from dedup import load_list as _ll, sort_by_deadline as _sbd
    push_kept = dp.study_facts(now)[1]
    readme_kept, _ = rr.actionable(_ll(rr.DATA_DIR / "study" / "tasks.json"), now)
    check("早报作业与 README 任务清单同一口径",
          [t["id"] for t in push_kept] == [t["id"] for t in _sbd(readme_kept)])

    # 紧急告警降噪：只认 24h 内截止，同一批不重复发卡。
    soon_sample = [{"id": "t1", "course": "高等数学A", "title": "作业",
                    "deadline": now.isoformat(timespec="seconds")},
                   {"id": "t2", "course": "大学英语", "title": "测验",
                    "deadline": now.isoformat(timespec="seconds")}]
    sig1, lines1 = study_collector.urgent_alerts(soon_sample, None)
    check("新出现的紧急任务会告警",
          len(lines1) == 2 and all(x.startswith("[urgent]") for x in lines1), str(lines1)[:80])
    sig2, lines2 = study_collector.urgent_alerts(soon_sample, sig1)
    check("同一批紧急任务不重复告警",
          sig2 == sig1 and not any(x.startswith("[urgent]") for x in lines2), str(lines2)[:80])
    drifted = [dict(t, deadline=(now + timedelta(minutes=7)).isoformat(timespec="seconds"))
               for t in soon_sample]
    check("倒计时漂移不会让签名变化（否则每小时重复发卡）",
          study_collector.urgent_alerts(drifted, sig1)[0] == sig1)
    check("没有紧急任务就不告警", study_collector.urgent_alerts([], sig1) == ("", []))
    check("紧急告警最多 5 条", len(study_collector.urgent_alerts(soon_sample * 4, None)[1]) == 5)

    # 二次元图：选图与「无凭证不上传」都是离线可验的；取图/上传要联网，不在自检里跑。
    import os as _os

    import anime_image as aimg
    check("图源判定认 Pixiv", aimg.is_pixiv({"source_url": "https://www.pixiv.net/artworks/1"}))
    check("非 Pixiv 图源如实标注", not aimg.is_pixiv({"source_url": "https://example.com/a"}))
    cand = [{"url": "https://cdn/1.png", "source_url": "https://example.com/a"},
            {"url": "https://cdn/2.png", "source_url": "https://www.pixiv.net/artworks/2"}]
    check("优先挑 Pixiv 图源的候选", aimg.pick(cand)["url"].endswith("2.png"))
    check("没有 Pixiv 候选时退回任意一张", aimg.pick(cand[:1])["url"].endswith("1.png"))
    check("空候选不抛异常", aimg.pick([]) is None and aimg.pick([{}]) is None)
    _saved_env = {k: _os.environ.pop(k, None) for k in ("FS_APP_ID", "FS_APP_SECRET")}
    try:
        check("没配飞书应用凭证时不上传（走链接模式）",
              aimg.upload("https://cdn/1.png") == "")
    finally:
        for k, v in _saved_env.items():
            if v is not None:
                _os.environ[k] = v

    # 飞书降噪与图片双路径写在 JS 里，Python 侧只能做源码守卫（node 不在门禁里跑）。
    js = (rr.REPO_ROOT / "scripts" / "notify_feishu.js").read_text(encoding="utf-8")
    check("常规成功静默闸门存在",
          "常规成功且无紧急任务" in js and "status === 'success' && urgent.length === 0" in js)
    check("逾期不再作为发卡理由", "detectOverdue" not in js)
    check("卡片被拒收时退回纯文本", "pushFallbackText" in js and "退回纯文本重发" in js)
    check("有 image_key 才内嵌真图，否则给按钮",
          "image.image_key" in js and "点开今天的图" in js)
    # 飞书卡片 schema 的 img 必填属性名是 img_key；写错会被拒卡 11246 退回纯文本（v1.5.5 真机踩过）
    check("img 元素属性名用飞书 schema 的 img_key",
          "img_key: image.image_key" in js and "image_key: image.image_key" not in js)
    check("推送卡片标题按早中晚区分", all(k in js for k in ("morning", "noon", "night")))

    # 时间线映射：节课内按真实比例走，课间停在两行交界，超出当天范围钳住
    import week_svg as ws

    bt = 100.0
    check("第一节课开始对在行顶", ws.time_y(8 * 60, bt) == bt)
    check("第一节课结束对在行底", ws.time_y(8 * 60 + 45, bt) == bt + ws.ROW_H)
    check("小课间停在行交界", ws.time_y(8 * 60 + 47, bt) == bt + ws.ROW_H)
    check("大课间不拉伸", ws.time_y(9 * 60 + 50, bt) == bt + ws.ROW_H * 2)
    check("凌晨钳到顶", ws.time_y(0, bt) == bt)
    check("深夜钳到底", ws.time_y(23 * 60, bt) == bt + ws.ROW_H * len(ws.SLOTS))

    # 日记写回：只动助手区块，区块外一个字节都不碰
    import diary_collector as dc
    import diary_writeback as dw
    import quiz_browser as qb
    import prompts as prompts_module

    blk = dw.render_block("2026-11-14", [("p1", "背单词", False), ("p2", "跑步", True)])
    check("写回区块往返解析", dict(dw.parse_block(blk)) == {"p1": False, "p2": True})
    # v1.7.0（用户报「乱码」）：区块用 Obsidian 阅读视图不显示的 %%…%% marker +
    # callout 包裹，不再裸露 <!-- … --> / <sub> / 行尾 HTML 注释 pid
    check("写回区块 Obsidian 友好（%%marker + callout，无 HTML 标签）",
          dw.BLOCK_START.startswith("%%") and "> [!todo]-" in blk
          and "<!--" not in blk and "<sub>" not in blk
          and "%%pid:p1%%" in blk, blk[:120])
    old_style = ("<!-- assistant:plans:start -->\n- [x] 旧勾选 <!-- pid:q1 -->\n"
                 "- [ ] 旧未勾 <!-- pid:q2 -->\n<!-- assistant:plans:end -->\n不动我")
    check("旧格式（HTML 注释）仍能解析勾选（迁移期兼容）",
          dict(dw.parse_block(old_style)) == {"q1": True, "q2": False},
          str(dw.parse_block(old_style)))
    migrated = dw.upsert_block("# 我的日记\n\n" + old_style, blk)
    check("旧格式区块被替换成新格式（自动迁移）且正文保留",
          "<!-- assistant:plans" not in migrated and dw.BLOCK_START in migrated
          and "# 我的日记" in migrated, migrated[:120])

    user_text = "# 今日记录\n\n我自己写的话。\n"
    merged = dw.upsert_block(user_text, blk)
    check("区块外正文保留", merged.startswith(user_text))
    again = dw.upsert_block(merged, dw.render_block("2026-11-14", [("p1", "背单词", True)]))
    check("重复写回只替换区块", again.count(dw.BLOCK_START) == 1 and "我自己写的话。" in again)
    check("复查能读到勾选", dict(dw.parse_block(again)).get("p1") is True)

    # v1.7.0：英语角区块 + 独立 upsert，与计划/记忆互不干扰
    en_blk = dw.render_english_block(3, "What did you get up to this weekend?")
    with_en = dw.upsert_english(again, en_blk)
    check("英语角区块 Obsidian 友好且可替换",
          dw.EN_BLOCK_START.startswith("%%") and "> [!tip]-" in en_blk
          and with_en.count(dw.EN_BLOCK_START) == 1
          and dw.BLOCK_START in with_en
          and dw.upsert_english(with_en, dw.render_english_block(4, "New one?"))
          .count(dw.EN_BLOCK_START) == 1)
    check("采集时三个助手区块都被剥离",
          "What did you get up" not in dc.strip_assistant_block(with_en))

    sample_plans = [
        {"id": "a", "text": "昨天的", "state": "open", "first_seen": "2026-11-12"},
        {"id": "b", "text": "滚动的", "state": "open", "first_seen": "2026-11-01",
         "written_once": True},
        {"id": "c", "text": "已完成", "state": "done", "first_seen": "2026-11-12"},
        {"id": "d", "text": "今天的", "state": "open", "first_seen": "2026-11-13"},
        {"id": "e", "text": "写过的", "state": "open", "first_seen": "2026-11-05",
         "written_once": True},
    ]
    picked = dw.pick_plans(sample_plans, {"b"}, 20)
    check("写回选未写过的+滚动未完成，滚动的排最前",
          [p["id"] for p in picked] == ["b", "a", "d"], str([p["id"] for p in picked]))
    check("写过又没滚动的不再重复写", "e" not in [p["id"] for p in picked])
    check("已完成的不写回", "c" not in [p["id"] for p in picked])

    cb = dc.extract_checkboxes("- [ ] 用户自己写的\n" + blk,
                               {"path": "日/2026-11-14.md", "name": "2026-11-14.md"}, now.date())
    check("助手区块不被重复抽取", [c["text"] for c in cb] == ["用户自己写的"], str(cb))

    # v1.7.0（用户报的 bug）：.pptx 等课件文件不是作业，采集层过滤 + 历史清理共用形状
    check("课件后缀标题被识别为文件（不是作业）",
          bool(study_collector.quiz_browser_FILE_RE.search("D1-2数列的极限.pptx"))
          and bool(study_collector.quiz_browser_FILE_RE.search("讲义.zip"))
          and not study_collector.quiz_browser_FILE_RE.search("第三章知识点总结"))
    _pptx_rows, _pptx_unmap = qb.parse_active_rows(
        [{"text": "D1-2数列的极限.pptx", "y": 100,
          "onclick": "openCurrentUrl('https://mobilelearn.huuc.edu.cn/page/active/"
                     "stuActiveDetail?courseid=219232606&clazzid=153566712&activeId=111111111', 2)",
          "aria": ""},
         {"text": "随堂小测", "y": 200,
          "onclick": "openCurrentUrl('https://mobilelearn.huuc.edu.cn/page/active/"
                     "stuActiveDetail?courseid=219232606&clazzid=153566712&activeId=222222222', 2)",
          "aria": ""}],
        {"course_id": "219232606", "class_id": "153566712", "name": "高等数学A"}, now)
    check("随堂解析跳过课件文件、只收真作业",
          [t["title"] for t in _pptx_rows] == ["随堂小测"], str(_pptx_rows))

    # v1.7.0：每日英语（AI 不可用走离线句库；难度限幅 1-5；当天幂等）
    class FakeEnAI:
        def __init__(self, ret):
            self.ret, self.calls = ret, 0

        def chat_json(self, messages, **kw):
            self.calls += 1
            return self.ret

    _saved_en_path = dc.ENGLISH_PATH
    try:
        import tempfile
        from pathlib import Path as _PPath
        with tempfile.TemporaryDirectory() as _tde:
            dc.ENGLISH_PATH = _PPath(_tde) / "english.json"
            _en_ai = FakeEnAI({"sentence": "How was your day so far?", "level": 3})
            s1 = dc.ensure_daily_english(
                now=datetime(2026, 11, 17, 0, 30, tzinfo=BJ_TZ),
                yesterday_text="I'm fine, just tired.", client=_en_ai)
            check("英语：AI 出句 + level 落账本",
                  s1 == "How was your day so far?" and _en_ai.calls == 1
                  and (dc.load_json(dc.ENGLISH_PATH) or {}).get("level") == 3, s1)
            s2 = dc.ensure_daily_english(
                now=datetime(2026, 11, 17, 2, 0, tzinfo=BJ_TZ), client=_en_ai)
            check("英语：同一天幂等（不重复烧 AI）", s2 == s1 and _en_ai.calls == 1)
            dc.save_json(dc.ENGLISH_PATH, {"date": "2026-11-16", "sentence": "old",
                                           "level": 9})
            s3 = dc.ensure_daily_english(
                now=datetime(2026, 11, 17, 3, 0, tzinfo=BJ_TZ),
                yesterday_text="", client=None)
            check("英语：AI 挂了走离线句库 + level 限幅到 5",
                  s3 in dc.ENGLISH_FALLBACK[5]
                  and (dc.load_json(dc.ENGLISH_PATH) or {}).get("level") == 5, s3)
            _pr = prompts_module.english_messages(2, "用户日记", "上一句")
            check("英语：提示词带 level/上一句/昨天日记",
                  "当前难度 level：2" in _pr[0]["content"]
                  and "上一句" in _pr[0]["content"]
                  and "用户昨天的日记" in _pr[0]["content"])
    finally:
        dc.ENGLISH_PATH = _saved_en_path

    # v1.7.0：飞书公告只推本月起的（README/看板不受此闸）
    check("notice_facts 源码含本月闸（publish_date >= 本月 1 号）",
          "month_start" in inspect.getsource(dp.notice_facts))
    # v1.7.0：天气文案写河南城建学院（用户指定地名）
    check("天气文案用河南城建学院",
          "河南城建学院" in inspect.getsource(dp.weather_tomorrow))

    # 用户硬要求：往他的 Obsidian 仓库写东西只能在凌晨，避开他白天编辑。
    check("窗口解析成分钟", dw.parse_window("00:00-05:00") == (0, 300))
    check("窗口含头", dw.in_window(datetime(2026, 11, 14, 0, 0, tzinfo=BJ_TZ), "00:00-05:00"))
    check("窗口不含尾", not dw.in_window(datetime(2026, 11, 14, 5, 0, tzinfo=BJ_TZ), "00:00-05:00"))
    check("白天不在凌晨窗口", not dw.in_window(datetime(2026, 11, 14, 15, 45, tzinfo=BJ_TZ), "00:00-05:00"))
    cross = "23:00-02:00"
    check("跨零点窗口", dw.in_window(datetime(2026, 11, 14, 23, 30, tzinfo=BJ_TZ), cross)
          and dw.in_window(datetime(2026, 11, 14, 1, 0, tzinfo=BJ_TZ), cross)
          and not dw.in_window(datetime(2026, 11, 14, 2, 0, tzinfo=BJ_TZ), cross))
    check("窗口格式非法失败关闭",
          not dw.in_window(datetime(2026, 11, 14, 1, 0, tzinfo=BJ_TZ), "白天")
          and dw.parse_window("25:00-26:00") is None)

    class FakeGitee:
        """只实现写回用到的两个方法，attempts 记录每次提交尝试（含被拒的）。"""

        def __init__(self, files, reject_first=False):
            self.files = dict(files)
            self.attempts = []
            self.reject_first = reject_first

        def read_file(self, path):
            f = self.files.get(path)
            return (f[0], f[1]) if f else ("", None)

        def write_file(self, path, text, message, sha=None):
            self.attempts.append(sha)
            if self.reject_first and len(self.attempts) == 1:
                # 模拟用户此刻正好保存了自己的编辑：sha 变了，正文多了一句
                old = self.files.get(path, ("", None))[0]
                self.files[path] = (old + "\n用户凌晨补的一句话\n", "sha-new")
                return False
            self.files[path] = (text, f"sha{len(self.attempts)}")
            return True

    import tempfile
    from pathlib import Path

    prev_blk = dw.render_block("2026-11-13", [("a", "昨天的", True), ("b", "滚动的", False)])
    fake_files = {"Tasks/日/2026-11-13.md": ("# 昨天我自己写的\n" + prev_blk, "s0"),
                  "Tasks/日/2026-11-14.md": ("# 今天我自己写的\n", "s1")}
    wb_plans = [{"id": "a", "text": "昨天的", "state": "open", "first_seen": "2026-11-12"},
                {"id": "b", "text": "滚动的", "state": "open", "first_seen": "2026-11-01"},
                {"id": "c", "text": "新挖到的", "state": "open", "first_seen": "2026-11-13"}]
    wb_cfg = {"path": "Tasks", "daily_dir": "日", "writeback_window": "00:00-05:00"}
    saved_wb = dw.WB_PATH
    try:
        with tempfile.TemporaryDirectory() as td:
            dw.WB_PATH = Path(td) / "writeback.json"
            dw.save_json(dw.WB_PATH, {"last_date": "2026-11-13",
                                      "last_path": "Tasks/日/2026-11-13.md"})

            fg = FakeGitee(fake_files)
            r = dw.run(g=fg, plans=wb_plans, cfg=wb_cfg,
                       now=datetime(2026, 11, 14, 12, 7, tzinfo=BJ_TZ))
            check("窗口外一次都不提交", not fg.attempts and r.get("skipped") == "outside_window", str(r))
            check("窗口外仍然复查勾选", r.get("done") == ["a"], str(r.get("done")))
            check("窗口外不推进 last_date",
                  (dw.load_json(dw.WB_PATH) or {}).get("last_date") == "2026-11-13")

            fg2 = FakeGitee(fake_files)
            r2 = dw.run(g=fg2, plans=wb_plans, cfg=wb_cfg,
                        now=datetime(2026, 11, 14, 0, 7, tzinfo=BJ_TZ))
            check("凌晨窗口内正常写入", len(fg2.attempts) == 1 and r2.get("written") == 2, str(r2))
            body = fg2.files["Tasks/日/2026-11-14.md"][0]
            check("写入保留用户正文", body.startswith("# 今天我自己写的"))
            check("已完成计划不再滚进新一天", "pid:a" not in body and "pid:b" in body and "pid:c" in body)

            fg3 = FakeGitee(fake_files, reject_first=True)
            r3 = dw.run(g=fg3, plans=wb_plans, cfg=wb_cfg,
                        now=datetime(2026, 11, 14, 2, 7, tzinfo=BJ_TZ))
            check("sha 冲突重读后重试", len(fg3.attempts) == 2 and r3.get("ok"), str(r3))
            check("重试保住用户并发编辑",
                  "用户凌晨补的一句话" in fg3.files["Tasks/日/2026-11-14.md"][0])
    finally:
        dw.WB_PATH = saved_wb

    # 助手的记忆（用户 2026-11-16）：区块往返 + 生成逻辑 + 写回集成，全部离线注入
    mem_blk = dw.render_memory_block("2026-11-13", "昨天你搞定了三章总结。哼。")
    check("记忆区块独立于计划区块", dw.MEM_BLOCK_START not in dw.render_block("2026-11-14", []))
    merged_m = dw.upsert_memory(merged, mem_blk)
    check("记忆区块追加且区块外正文保留",
          merged_m.count(dw.MEM_BLOCK_START) == 1 and "我自己写的话。" in merged_m
          and "昨天你搞定了三章总结" in merged_m)
    again_m = dw.upsert_memory(merged_m, dw.render_memory_block("2026-11-13", "换了说法"))
    check("重复写回只替换记忆区块（计划区块不动）",
          again_m.count(dw.MEM_BLOCK_START) == 1 and "换了说法" in again_m
          and "昨天你搞定了三章总结" not in again_m and dw.BLOCK_START in again_m)
    stripped_m = dc.strip_assistant_block(blk + "\n" + mem_blk + "\n- [ ] 用户自己的\n")
    check("采集时两个助手区块都被剥离",
          "助手待办" not in stripped_m and "助手记得" not in stripped_m
          and "用户自己的" in stripped_m)

    class FakeAI:
        def __init__(self, text, fail=False):
            self.text, self.fail, self.calls = text, fail, 0

        def chat_text(self, messages, **kwargs):
            self.calls += 1
            if self.fail:
                raise RuntimeError("fake ai down")
            return self.text

    mem_cfg = {"path": "Tasks", "daily_dir": "日", "writeback_window": "00:00-05:00"}
    _mem_dt = datetime(2026, 11, 14, 0, 30, tzinfo=BJ_TZ)
    saved_mem = dc.MEMORY_PATH
    try:
        with tempfile.TemporaryDirectory() as td:
            dc.MEMORY_PATH = Path(td) / "memory.json"
            fg_m = FakeGitee({"Tasks/日/2026-11-13.md": ("充实的一天，写了好多字 " * 5, "m0")})
            out_m = dc.ensure_day_memory(fg_m, mem_cfg, [],
                                         now=datetime(2026, 11, 14, 12, 7, tzinfo=BJ_TZ))
            check("记忆：窗口外不生成", out_m is None
                  and not (dc.load_json(dc.MEMORY_PATH) or {}).get("items"))

            fg_m2 = FakeGitee({"Tasks/日/2026-11-13.md":
                               ("# 充实的一天\n\n- [x] 搞定三章总结\n- [ ] 背单词\n晚上刷了两小时题。", "m1")})
            ai_m = FakeAI("昨天你把三章总结拿下了，还顺手把单词挂在明天。哼，别飘。")
            out_m2 = dc.ensure_day_memory(fg_m2, mem_cfg, [], now=_mem_dt, client=ai_m)
            check("记忆：窗口内生成 AI 版", out_m2 == ai_m.text and ai_m.calls == 1, str(out_m2))
            led_m = (dc.load_json(dc.MEMORY_PATH) or {}).get("items") or []
            check("记忆：账本入库（date/source/text）",
                  len(led_m) == 1 and led_m[0]["date"] == "2026-11-13"
                  and led_m[0]["source"] == "ai" and led_m[0]["text"] == ai_m.text)
            out_m3 = dc.ensure_day_memory(fg_m2, mem_cfg, [], now=_mem_dt,
                                           client=FakeAI("不该被调到"))
            check("记忆：ai 版当天只生成一次（复用账本）", out_m3 == ai_m.text)

            # 换一批场景前清空账本，避免上一步的 2026-11-13 ai 条目串场
            dc.save_json(dc.MEMORY_PATH, {"items": []})

            fg_m3 = FakeGitee({"Tasks/日/2026-11-13.md": ("短", "m2")})
            out_m4 = dc.ensure_day_memory(fg_m3, mem_cfg, [], now=_mem_dt, client=FakeAI("x"))
            out_m5 = dc.ensure_day_memory(FakeGitee({}), mem_cfg, [], now=_mem_dt, client=FakeAI("x"))
            fg_m5 = FakeGitee({"Tasks/日/2026-11-13.md": ("足够长的日记内容 " * 8, "m3")})
            out_m6 = dc.ensure_day_memory(fg_m5, mem_cfg, ["Tasks/日"], now=_mem_dt,
                                           client=FakeAI("x"))
            check("记忆：太短/不存在/私人路径都不生成",
                  out_m4 is None and out_m5 is None and out_m6 is None)

            dc.save_json(dc.MEMORY_PATH, {"items": []})
            fg_m6 = FakeGitee({"Tasks/日/2026-11-13.md": ("足够长的日记内容 " * 8, "m4")})
            out_m7 = dc.ensure_day_memory(fg_m6, mem_cfg, [], now=_mem_dt,
                                           client=FakeAI("", fail=True))
            check("记忆：AI 失败用离线摘要占位", out_m7 and "离线摘要" in out_m7, str(out_m7))
            ai_m2 = FakeAI("恢复后重写的记忆。")
            out_m8 = dc.ensure_day_memory(fg_m6, mem_cfg, [], now=_mem_dt, client=ai_m2)
            check("记忆：offline 后 AI 恢复会重生成",
                  out_m8 == ai_m2.text and ai_m2.calls == 1, str(out_m8))
            check("记忆：账本 60 天自动清理",
                  dc.memory_ledger_prune([{"date": "2026-09-01", "text": "旧"},
                                          {"date": "2026-11-13", "text": "新"}],
                                         datetime(2026, 11, 14, tzinfo=BJ_TZ).date())
                  == [{"date": "2026-11-13", "text": "新"}])

            # 写回集成用例也必须隔离：dw.run 会写 writeback.json，
            # 不換路径会把测试状态写进真实数据（v1.6.3 提交时实测踩过）
            saved_wb2 = dw.WB_PATH
            dw.WB_PATH = Path(td) / "writeback_test.json"
            try:
                fg_w = FakeGitee({"Tasks/日/2026-11-14.md": ("# 今天\n", "w1")})
                r_w = dw.run(g=fg_w, plans=[], cfg=wb_cfg, memory="记得你昨天熬夜了。哼。",
                             now=datetime(2026, 11, 14, 3, 7, tzinfo=BJ_TZ))
                body_w = fg_w.files["Tasks/日/2026-11-14.md"][0]
                check("写回：记忆段落随计划一起写入（标昨天的日期）",
                      dw.MEM_BLOCK_START in body_w and "记得你昨天熬夜了" in body_w
                      and "助手记得 · 2026-11-13" in body_w and r_w.get("ok"), body_w[:120])
                fg_w2 = FakeGitee({})
                r_w2 = dw.run(g=fg_w2, plans=[], cfg=wb_cfg, memory="只有记忆也要写。",
                              now=datetime(2026, 11, 14, 4, 7, tzinfo=BJ_TZ))
                check("写回：无计划无正文但有记忆也写入（新建骨架）",
                      "只有记忆也要写" in (fg_w2.files.get("Tasks/日/2026-11-14.md") or ("",))[0]
                      and r_w2.get("ok"))
            finally:
                dw.WB_PATH = saved_wb2
    finally:
        dc.MEMORY_PATH = saved_mem

    yml = (rr.REPO_ROOT / ".github" / "workflows" / "assistant.yml").read_text(encoding="utf-8")
    check("只有一条 cron（多条会被 GitHub 节流丢掉）",
          yml.count("- cron:") == 1 and "'7-59/20 * * * *'" in yml, str(yml.count("- cron:")))
    check("定时触发映射到 tick", "MODE=tick" in yml)
    check("质量门禁在执行之前",
          yml.index("ruff check scripts") < yml.index("python scripts/main.py \"$MODE\""))
    check("门禁含未定义名字检查 F821", "F821" in yml)
    check("只有一个并发组", "group: pa-run" in yml and "pa-push" not in yml and "pa-collect" not in yml)
    check("私人黑名单只引用 Secret 名",
          "DIARY_EXCLUDE: ${{ secrets.DIARY_EXCLUDE }}" in yml)
    # 门禁与执行**两步都要注入**。只有执行步注入时，私人路径守卫在普通 tick 里
    # 永远走「跳过」分支 —— 实测 backfill 那轮就是这样，等于平时没有守卫。
    _n_inject = yml.count("DIARY_EXCLUDE: ${{ secrets.DIARY_EXCLUDE }}")
    check("质量门禁也注入黑名单（守卫每轮都跑）", _n_inject == 2, str(_n_inject))
    # 图片双路径：凭证只引用 Secret 名；没配也能推送（upload 返回空 → 按钮链接）。
    check("飞书应用凭证只引用 Secret 名",
          "FS_APP_ID: ${{ secrets.FS_APP_ID }}" in yml
          and "FS_APP_SECRET: ${{ secrets.FS_APP_SECRET }}" in yml)
    check("推送时段传给飞书脚本（决定卡片标题）",
          "NOTIFY_KIND: ${{ steps.mode.outputs.mode }}" in yml)
    check("每轮清掉上一轮的文案与图片残留",
          "rm -f /tmp/push-message.txt /tmp/push-image.json /tmp/push-urgent.txt" in yml
          and "/tmp/push-new.txt" in yml)
    # 紧急提醒与三时段同轮时两条都必须发出（elif 会吞掉 urgent 且记账已写=永久丢失）
    check("紧急提醒走独立 if 分支（elif 会吞掉它）",
          "if [ -s /tmp/push-urgent.txt ]" in yml and "elif [ -s /tmp/push-urgent.txt ]" not in yml)
    # v1.6.2 新作业发布提醒：第三条独立通道，同轮互不挤占（elif 会吞掉新作业且记账已写=永久丢）
    check("新作业提醒也走独立 if 分支且清残留",
          "if [ -s /tmp/push-new.txt ]" in yml
          and "elif [ -s /tmp/push-new.txt ]" not in yml
          and "NOTIFY_KIND=newtask" in yml
          and "notified_new" in inspect.getsource(do_tick))
    # v1.5.17 仓库内自续链：relay.yml 接力器（用户要求定时不能外包、仓库里也要一套）
    ryml = (rr.REPO_ROOT / ".github" / "workflows" / "relay.yml").read_text(encoding="utf-8")
    check("接力器独立并发组且 cancel-in-progress（链恒 ≤1 条）",
          "group: pa-relay" in ryml and "cancel-in-progress: true" in ryml)
    check("接力器会触发下一轮 tick 和下一棒自己",
          "assistant.yml/dispatches" in ryml and "relay.yml/dispatches" in ryml
          and '"mode":"tick"' in ryml)
    check("接力器用 GITHUB_TOKEN（workflow_dispatch 官方例外，不需要 PAT）",
          "${{ github.token }}" in ryml)
    check("assistant.yml 每轮结束都接力（第三道保险）",
          "relay.yml/dispatches" in yml)
    # v1.5.22 随堂作业 Playwright 采集：依赖/浏览器安装/轮换记账的接线守卫
    check("Playwright 在依赖与安装步骤里（随堂作业抓取）",
          "playwright" in (rr.REPO_ROOT / "scripts" / "requirements.txt").read_text(encoding="utf-8")
          and "playwright install chromium" in yml)
    check("study 集成随堂探测且失败不影响主采集",
          "quiz_browser" in inspect.getsource(study_collector.run)
          and "不影响主采集" in inspect.getsource(study_collector.run))
    check("随堂轮换 cursor 记账在 meta.json",
          "quiz_cursor" in inspect.getsource(study_collector.run))

    # v1.6.0 随堂作业收录：解析/合并/标签/记账的接线与纯函数守卫
    import types as _types
    import quiz_browser as qb
    import utils as _u
    from dedup import merge_quiz as _mq
    from dedup import merge_tasks
    fake = _types.SimpleNamespace(cookies=[
        _types.SimpleNamespace(name="UID", value="v1", domain="mooc1.huuc.edu.cn", path="/"),
        _types.SimpleNamespace(name="UID", value="v1", domain="passport2.huuc.edu.cn", path="/"),
        _types.SimpleNamespace(name="fid", value="12", domain=".huuc.edu.cn", path="/"),
        _types.SimpleNamespace(name="other", value="x", domain="www.baidu.com", path="/"),
    ])
    cc = qb.cookies_for_browser(fake)
    check("随堂 cookie 域归一化（huuc 限内、按名去重、父域化）",
          len(cc) == 2 and all(c["domain"] == ".huuc.edu.cn" for c in cc)
          and {c["name"] for c in cc} == {"UID", "fid"}, str(cc))
    check("随堂状态词分类", qb.classify_quiz("进行中") == "pending"
          and qb.classify_quiz("已参与") == "done"
          and qb.classify_quiz("未开始") == "upcoming"
          and qb.classify_quiz("某个没见过的词") == "unknown")
    qdl, qex = qb.parse_quiz_time("剩余2天3小时", now)
    check("随堂倒计时推算（非精确）",
          qdl == "2026-11-15T01:00:00+08:00" and qex is False, str(qdl))
    qdl2, qex2 = qb.parse_quiz_time("12-05 10:00", now)
    check("随堂绝对时间就近补年", qdl2 == "2026-12-05T10:00:00+08:00" and qex2 is False, str(qdl2))
    qdl3, qex3 = qb.parse_quiz_time("2026-11-15 09:00", now)
    check("随堂带年份视为精确", qdl3 == "2026-11-15T09:00:00+08:00" and qex3 is True, str(qdl3))
    qrows, qunmap = qb.parse_active_rows(
        [{"text": "第三章知识总结\n进行中\n剩余2天3小时", "cls": "a",
          "onclick": "openCurrentUrl('https://mobilelearn.huuc.edu.cn/page/active/"
                     "stuActiveDetail?courseid=219232606&clazzid=153566712&activeId=123456789', 2)",
          "aria": "第三章知识总结 ; 进行中"},
         {"text": "已完成", "cls": "b", "onclick": "", "aria": ""}],
        {"course_id": "219232606", "class_id": "153566712", "name": "高等数学A"}, now)
    check("随堂行解析出 1 条（纯状态行跳过）",
          len(qrows) == 1 and not qunmap, f"{len(qrows)}/{qunmap}")
    if len(qrows) == 1:
        t0 = qrows[0]
        check("随堂行字段齐（kind/label/标题/状态/截止/深链）",
              t0["kind"] == "quiz" and t0["label"] == "随堂"
              and t0["title"] == "第三章知识总结"
              and t0["state"] == "pending" and t0["status"] == "进行中"
              and t0["deadline"] == qdl and t0["deadline_exact"] is False
              and "courseid=219232606" in t0["url"] and "123456789" in t0["url"],
              str(t0))
    # v1.6.1 真实样本结构（run 43805479954）：同一可视行是并列碎片元素，
    # 靠 Y 坐标聚类合并；计数徽标与课堂互动（签到）过滤；中文时分格式；
    # ID 提取认 activeId 而非顺位第一的 courseid（撞车会吞任务）。
    qdl4, qex4 = qb.parse_quiz_time("11月05日 15时30分", now)
    check("随堂中文时分就近补年", qdl4 == "2026-11-05T15:30:00+08:00" and qex4 is False, str(qdl4))
    frag_rows = [
        {"text": "（3）", "y": 100,
         "onclick": "openCurrentUrl('https://mobilelearn.huuc.edu.cn/page/active/"
                    "stuActiveDetail?courseid=219232606&clazzid=153566712&activeId=123456789', 2)",
         "aria": ""},
        {"text": "第三章知识总结", "y": 102, "onclick": "", "aria": "第三章知识总结 ; 进行中"},
        {"text": "11月05日 15时30分\n2026-11-05 15:30", "y": 104, "onclick": "", "aria": ""},
        {"text": "2026-2027第一学期第一次签到", "y": 200, "onclick": "", "aria": ""},
        {"text": "11月03日 19时33分", "y": 202, "onclick": "", "aria": ""},
        {"text": "3-5函数的极值与最值", "y": 300,
         "onclick": "openCurrentUrl('https://mobilelearn.huuc.edu.cn/page/active/"
                    "stuActiveDetail?courseid=219232606&clazzid=153566712&activeId=987654321', 2)",
         "aria": ""},
    ]
    frows, funmap = qb.parse_active_rows(
        frag_rows, {"course_id": "219232606", "class_id": "153566712", "name": "高等数学A"}, now)
    check("碎片按视觉聚类：2 条任务（签到行被过滤）",
          len(frows) == 2 and not funmap, f"{len(frows)}/{funmap}")
    if len(frows) == 2:
        f0, f1 = frows
        check("聚类行的字段（标题/状态/机器层精确截止/深链）",
              f0["title"] == "第三章知识总结" and f0["state"] == "pending"
              and f0["status"] == "进行中"
              and f0["deadline"] == "2026-11-05T15:30:00+08:00" and f0["deadline_exact"] is True
              and "courseid=219232606" in f0["url"] and "123456789" in f0["url"],
              str(f0))
        check("ID 提取认 activeId 不撞车（两行同课不同 ID）",
              f1["title"] == "3-5函数的极值与最值" and f0["id"] != f1["id"],
              f"{f0['id']}/{f1['id']}")
    masked = qb.masked_diag({"firstLines": ["学号12345 某某 undisclosed"]}, [])
    check("随堂诊断数字打码", masked and "12345" not in masked[0] and "D" in masked[0], str(masked))
    old_q = [{"id": "a", "kind": "quiz", "course_id": "1", "class_id": "1", "title": "A", "state": "pending"},
             {"id": "b", "kind": "quiz", "course_id": "2", "class_id": "2", "title": "B", "state": "pending"}]
    m1 = _mq(old_q, [{"id": "a2", "kind": "quiz", "course_id": "1", "class_id": "1",
                      "title": "A2", "state": "pending"}], {("1", "1")}, now)
    check("随堂合并：覆盖课新值权威、未覆盖保旧", {t["id"] for t in m1} == {"a2", "b"}
          and "first_seen" in m1[0], str([t["id"] for t in m1]))
    m2 = _mq(old_q, [], {("1", "1"), ("2", "2")}, now)
    check("随堂合并：覆盖且为空 = 真没随堂，全删", m2 == [], str(len(m2)))
    m3 = _mq(old_q, [], set(), now)
    check("随堂合并：整轮失败全保旧（不清空）", {t["id"] for t in m3} == {"a", "b"}, str(len(m3)))
    m4 = _mq([{"id": "a", "kind": "quiz", "course_id": "1", "class_id": "1", "title": "A",
               "state": "pending", "deadline": "2026-11-20T10:00:00+08:00", "deadline_exact": True}],
             [{"id": "a", "kind": "quiz", "course_id": "1", "class_id": "1",
               "title": "A", "state": "pending"}], {("1", "1")}, now)
    check("随堂合并：精确截止旧值优先（防精度倒退）",
          m4[0]["deadline"] == "2026-11-20T10:00:00+08:00" and m4[0]["deadline_exact"] is True,
          str(m4[0].get("deadline")))
    # v1.6.6（用户报的 bug）：批内同 id 重复行只留一条（体育课三行同 id 实测）
    m5 = _mq([], [{"id": "x", "kind": "quiz", "course_id": "1", "class_id": "1",
                   "title": "体育课", "state": "pending",
                   "deadline": "2026-10-20T13:52:00+08:00"},
                  {"id": "x", "kind": "quiz", "course_id": "1", "class_id": "1",
                   "title": "体育课", "state": "pending",
                   "deadline": "2026-10-27T14:59:00+08:00"}], {("1", "1")}, now)
    check("随堂合并：批内同 id 重复行只留一条",
          sum(1 for t in m5 if t["id"] == "x") == 1, str(m5))
    m6 = merge_tasks([], [{"id": "y", "kind": "work", "state": "pending", "title": "A"},
                          {"id": "y", "kind": "work", "state": "pending", "title": "A"}], now)
    check("作业合并：批内同 id 重复行只留一条",
          sum(1 for t in m6 if t["id"] == "y") == 1, str(m6))
    check("README 与网页看板都有随堂标签",
          rr.KIND_CN.get("quiz") == "随堂"
          and "quiz: '随堂'" in (rr.REPO_ROOT / "docs" / "js" / "app.js").read_text(encoding="utf-8"))
    check("随堂深链白名单含 activity 级 ID",
          "activeid" in _u._PUBLIC_KEEP and "knowledgeid" in _u._PUBLIC_KEEP)
    check("推送模式也提交 runtime.json 记账（防重复推送）",
          "push != 'true'" not in yml and "morning|noon|dusk|night)" in yml)
    check("跨触发源推送防重在 do_push 记账",
          "记账防重" in inspect.getsource(do_push)
          and "RUNTIME_PATH" in inspect.getsource(do_push))
    wcfg = ((dw.load_json(dw.DATA_DIR / "sources.json") or {}).get("diary") or {}).get("writeback_window")
    check("sources.json 的写回窗口可解析", dw.parse_window(wcfg or dw.DEFAULT_WINDOW) is not None, str(wcfg))

    # 真实样本结构：课程名在 li 内的 course-name span，middle 页首个 h2 是全站横幅
    list_html = '''<ul class="course-list"><li class="course clearfix" id="c_100000001">
      <div class="course-cover"><a href="https://mooc1.huuc.edu.cn/visit/stucoursemiddle?courseid=100000001&clazzid=100000002&cpi=100000003&ismooc2=1"><img src="x.jpg"></a></div>
      <div class="course-info"><h3 class="inlineBlock"><a class="color1" href="https://mooc1.huuc.edu.cn/visit/stucoursemiddle?courseid=100000001&clazzid=100000002&cpi=100000003&ismooc2=1">
      <span class="course-name overHidden2" title="高等数学A">高等数学A</span></a></h3></div></li></ul>'''
    cx = study_collector.Chaoxing({}, "u", "p")
    cx.get = lambda *a, **k: list_html
    cs = cx.list_courses()
    check("课程列表去重后 1 门", len(cs) == 1, f"实际 {len(cs)}")
    check("课程名取自列表页 span", bool(cs) and cs[0]["name"] == "高等数学A",
          cs[0]["name"] if cs else "无")

    cx.get = lambda *a, **k: ('<title>\n  大学生职业生涯与发展规划\n  </title>'
                              '<h2>在线学习诚信承诺书</h2>'
                              '<input id="enc" value="aa11"><input id="workEnc" value="bb22">')
    c2 = {"course_id": "1", "class_id": "2", "cpi": "3", "name": ""}
    cx.mint(c2)
    check("middle 页用 title 兜底课程名", c2["name"] == "大学生职业生涯与发展规划", c2["name"])

    notice_html = '''<li><a href="../info/1061/27179.htm" target="_blank" title="关于举办第十三期校友创新大讲堂的通知">
      <div class="date"><b>09</b><span>2026-11</span></div><p>关于举办第十三期校友创新大讲堂的通知</p></a></li>
      <li><span>10-28</span><a href="info/1018/5561.htm" target="_blank" title="我校18篇学士学位论文获评河南省优秀">我校18篇...</a></li>
      <li id="line_u8_0"><a href="info/1007/10986.htm" target="_blank" title="2026年特色品牌社团立项公示"><span>2026-11-06</span>2026年特色品牌社团立项公示</a></li>'''
    ns = notice_collector.parse_list(notice_html, "https://www.huuc.edu.cn/index/tzgg.htm",
                                     "校网通知公告", now.date())
    check("公告行数=3", len(ns) == 3, f"实际 {len(ns)}")
    if len(ns) == 3:
        check("www 日期拼接", ns[0]["publish_date"] == "2026-11-09", str(ns[0]["publish_date"]))
        check("jwc 短日期补年", ns[1]["publish_date"] == "2026-10-28", str(ns[1]["publish_date"]))
        check("tw 完整日期", ns[2]["publish_date"] == "2026-11-06", str(ns[2]["publish_date"]))
        check("相对链接转绝对", ns[0]["url"] == "https://www.huuc.edu.cn/info/1061/27179.htm",
              ns[0]["url"])
        check("标题取 title 属性", ns[1]["title"] == "我校18篇学士学位论文获评河南省优秀", ns[1]["title"])

    body_html = ('<div class="nav">首页 通知公告 学院概况</div>'
                 '<div class="v_news_content"><p>全校各学院：</p>'
                 '<p>定于2026年10月至12月举办第二十一届大学生职业规划大赛。</p>'
                 '<p>报名截止：2026年11月30日</p></div>'
                 '<div class="footer">版权所有 河南城建学院</div>')
    bt = notice_collector.body_text(body_html)
    check("正文取 v_news_content 容器", "职业规划大赛" in bt and "报名截止" in bt, bt[:60])
    check("正文不含导航与页脚", "学院概况" not in bt and "版权所有" not in bt, bt[:60])
    check("截止日有正文依据才采纳", notice_collector.deadline_supported(bt, "2026-11-30"))

    phase_html = ('<div class="v_news_content"><p>学院初赛阶段：2026年10月1日—10月30日。</p>'
                  '<p>复赛阶段：2026年11月1日—11月20日。</p></div>')
    pt = notice_collector.body_text(phase_html)
    check("阶段结束日不算报名截止", not notice_collector.deadline_supported(pt, "2026-10-30"))

    ics = schedule_parser.parse_ics(schedule_parser.ICS_PATH)
    check("课表 VEVENT 解析", len(ics) > 100, f"实际 {len(ics)}")
    if ics:
        ev = schedule_parser.build_events(ics)
        check("课表节次映射", all(1 <= e["slot_start"] <= e["slot_end"] <= 11 for e in ev))
        check("课表周次存在", sum(1 for e in ev if e.get("week")) > 100)
        ss = schedule_parser.guess_semester_start(ev)
        check("学期起点推断", ss == "2026-09-07", str(ss))

    from dedup import (archive_plans, merge_notices, merge_plans, merge_tasks,
                       save_list)
    t_old = [{"id": "a", "deadline": "2026-11-13T10:00:00+08:00", "deadline_exact": True,
              "first_seen": "2026-11-01T00:00:00+08:00", "state": "pending"}]
    t_new = [{"id": "a", "deadline": "2026-11-13T09:00:00+08:00", "deadline_exact": False,
              "state": "pending"}]
    mt = merge_tasks(t_old, t_new, now)
    check("精确截止时间不被倒计时覆盖", mt[0]["deadline"] == "2026-11-13T10:00:00+08:00",
          mt[0]["deadline"])
    check("first_seen 保留", mt[0]["first_seen"] == "2026-11-01T00:00:00+08:00")

    n_old = [{"id": "n1", "title": "旧标题", "score": 4, "description": "已有描述",
              "publish_date": "2026-11-01", "last_seen": "2026-11-10"}]
    n_new = [{"id": "n1", "title": "新标题", "score": 2, "description": "",
              "publish_date": "2026-11-02"}]
    mn = merge_notices(n_old, n_new, now.date())
    check("公告旧值优先", mn[0]["description"] == "已有描述" and mn[0]["title"] == "旧标题")
    check("公告相关度只升不降", mn[0]["score"] == 4, str(mn[0]["score"]))

    # ------------------------------------------------------------------
    # 以下是本轮审计修复的回归断言。每一条都对应 docs/不足与风险.md 的一个编号，
    # 改坏了会在这里直接红，不用等用户发现。
    # ------------------------------------------------------------------
    import importlib
    import inspect
    import json

    from utils import exc_brief, public_url, scrub_task_title, strip_personal_fields

    # §2.1 教师姓名混在任务标题开头。规则必须要求「学年-学年」形状，
    # 否则合法标题「作业20261022」会被削成「1022」。
    check("教师姓名前缀被剥掉",
          scrub_task_title("张三2026-2027学年第一学期期末") == "2026-2027学年第一学期期末",
          scrub_task_title("张三2026-2027学年第一学期期末"))
    check("合法数字标题不被误伤", scrub_task_title("作业20261022") == "作业20261022",
          scrub_task_title("作业20261022"))
    check("无学年形状的课程名不动", scrub_task_title("高等数学A") == "高等数学A")

    # §7.2 公开链接只保留课程级参数，每生一份的 cpi/enc/answerId 一律抹掉
    deep = ("https://mooc1.huuc.edu.cn/mooc-ans/mooc2/work/task"
            "?courseId=1&classId=2&cpi=3&workId=4&answerId=5&enc=abc")
    check("公开链接去掉每生一份的参数",
          public_url(deep) == "https://mooc1.huuc.edu.cn/mooc-ans/mooc2/work/task"
                              "?courseId=1&classId=2&workId=4", public_url(deep))
    check("full 模式保留深链（只给飞书用）", public_url(deep, "full") == deep)
    stripped = strip_personal_fields({"url": deep, "cpi": "3", "answer_id": "5", "title": "t"})
    check("落盘前抹掉 cpi/answer_id 字段",
          "cpi" not in stripped and "answer_id" not in stripped and stripped["title"] == "t",
          str(stripped))

    # §2.6 异常摘要：公开仓库的 Actions 日志谁都能看，不能发完整栈
    try:
        raise ValueError("Cookie: JSESSIONID=ABCDEF123456; _uid=999")
    except ValueError as e:
        brief = exc_brief(e)
    check("异常摘要脱敏 cookie", "ABCDEF123456" not in brief, brief)
    check("异常摘要带出错位置", "main.py:" in brief, brief)
    check("异常摘要不是完整栈", "Traceback" not in brief and "\n" not in brief, brief)

    # 用户报的核心 bug：明明有截止时间却抓不到，逾期任务被显示进 README。
    # 真实截止在详情页「作答时间:<em>MM-DD HH:MM</em>至<em>MM-DD HH:MM</em>」里。
    ds, de = study_collector.detail_work_window(study_collector.SAMPLE_DETAIL_HTML, now)
    check("作业详情页解析出真实截止",
          de is not None and (de.month, de.day, de.hour, de.minute) == (11, 10, 23, 59), str(de))
    check("作业详情页解析出开始时间", ds is not None and (ds.month, ds.day) == (11, 5), str(ds))
    check("解析出的窗口能判定逾期", de is not None and de < now)
    cst, cen = study_collector.detail_time_window(
        "<p>作答时间:<em>12-28 08:00</em>至<em>01-05 23:59</em></p>", now, "作答时间")
    check("跨年窗口止时间进到下一年",
          cst is not None and cen is not None and cst.year == 2026 and cen.year == 2027,
          f"{cst} ~ {cen}")
    smoke = study_collector.parse_smoke(now)
    check("解析冒烟全通道有产出", all(smoke.values()), str(smoke))

    # §1.2 采集全空 ≠ 作业全没了，必须保住历史
    hist = [mk(title="历史任务")]
    check("新列表为空时拒绝合并", len(merge_tasks(hist, [], now)) == 1, str(hist))

    # §4.2 内容没变就不动 updated_at，否则每天 24 个纯时间戳 commit
    with tempfile.TemporaryDirectory() as td:
        sp = Path(td) / "x.json"
        save_list(sp, [{"id": "a"}])
        s1 = json.loads(sp.read_text(encoding="utf-8"))["updated_at"]
        save_list(sp, [{"id": "a"}])
        s2 = json.loads(sp.read_text(encoding="utf-8"))["updated_at"]
        check("内容不变则时间戳不变", s1 == s2, f"{s1} vs {s2}")
        d = json.loads(sp.read_text(encoding="utf-8"))
        d["updated_at"] = "2020-01-01T00:00:00+08:00"
        sp.write_text(json.dumps(d), encoding="utf-8")
        save_list(sp, [{"id": "b"}])
        check("内容变了时间戳才刷新",
              json.loads(sp.read_text(encoding="utf-8"))["updated_at"] != "2020-01-01T00:00:00+08:00")

    # §3.4 归档只动「已完成/取消且久未出现」，open 计划一条都不删
    ap = [{"id": "o", "state": "open", "last_seen": "2026-01-01"},
          {"id": "d", "state": "done", "last_seen": "2026-01-01"},
          {"id": "n", "state": "done", "last_seen": "2026-11-10"}]
    kept_p, arc_p = archive_plans(ap, now.date())
    check("归档只动已完成且久未出现",
          [p["id"] for p in arc_p] == ["d"] and [p["id"] for p in kept_p] == ["o", "n"],
          f"kept={kept_p} arc={arc_p}")

    # §2.3 置信度门槛。注意量纲是 0.0–1.0（审计原文写的 >=2 是把量纲搞反了）
    check("低置信度 AI 计划不入库",
          merge_plans([], [{"id": "x", "text": "吐槽", "origin": "ai",
                            "confidence": 0.2, "state": "open"}], now.date()) == [])
    check("正常置信度入库",
          len(merge_plans([], [{"id": "y", "text": "计划", "origin": "ai",
                                "confidence": 0.7, "state": "open"}], now.date())) == 1)
    check("复选框计划不受门槛影响",
          len(merge_plans([], [{"id": "z", "text": "待办", "origin": "checkbox",
                                "confidence": 1.0, "state": "open"}], now.date())) == 1)

    # §3.9 研究生处把日期放在 </a> 之后，只看锚点内和锚点前会永远取不到
    after_html = ('<li><a href="info/1/2.htm" title="学术报告通知">学术报告通知</a>'
                  '<span>2026-11-08</span></li>')
    na = notice_collector.parse_list(after_html, "https://yjsc.huuc.edu.cn/xkjs/tzgg.htm",
                                     "研究生处学科建设", now.date())
    check("日期在锚点之后也能取到",
          len(na) == 1 and na[0]["publish_date"] == "2026-11-08", str(na))

    # §3.1 标题通道的截止日必须有字面依据，且不能永久压住后来校验过的正文日期
    n_old2 = [{"id": "n2", "title": "比赛通知", "deadline": "2026-12-01",
               "deadline_source": "title", "score": 3}]
    n_new2 = [{"id": "n2", "title": "比赛通知", "deadline": "2026-11-30",
               "deadline_source": "body", "score": 1}]
    mn2 = merge_notices(n_old2, n_new2, now.date())
    check("正文校验过的截止日覆盖标题猜测",
          mn2[0]["deadline"] == "2026-11-30" and mn2[0]["deadline_source"] == "body", str(mn2[0]))

    # §5.3 ICS 里 10 条 SUMMARY 被时钟前缀污染，用干净记录的课程名反修
    raw_ics = schedule_parser.parse_ics(schedule_parser.ICS_PATH)
    polluted = sum(1 for p in raw_ics
                   if schedule_parser.CLOCK_PREFIX_RE.match((p.get("SUMMARY") or "").strip()))
    fixed_n = schedule_parser.repair_summaries(raw_ics)
    left = sum(1 for p in raw_ics
               if schedule_parser.CLOCK_PREFIX_RE.match((p.get("SUMMARY") or "").strip()))
    check("时钟前缀污染全部修好", fixed_n == polluted and left == 0,
          f"fixed={fixed_n} polluted={polluted} left={left}")
    check("修复后没有空课程名",
          all((p.get("SUMMARY") or "").strip() for p in raw_ics))

    # §5.1 静态 SVG 不再烘焙红色时间线，红色那条只由网页 JS 画
    check("SVG 源码不再烘焙红色时间线", "#FA5252" not in inspect.getsource(ws.run))
    check("网页端仍有实时时间线",
          "nowline" in (rr.REPO_ROOT / "docs" / "js" / "app.js").read_text(encoding="utf-8"))
    # 看板与 README 的名言口径必须一致，否则用户在看板上看到的排序和 README 不一样。
    _appjs = (rr.REPO_ROOT / "docs" / "js" / "app.js").read_text(encoding="utf-8")
    check("看板名言也按 AI 价值排序", "(b.value || 0) - (a.value || 0)" in _appjs)
    check("看板名言显示 AI 价值分", "AI 价值 " in _appjs)
    check("看板状态含未开始（v1.6.5，与 Python 分类同口径）",
          "upcoming: '未开始'" in _appjs)
    check("看板把未开始纳入待办并单列一桶（v1.6.6：显示但不计待完成）",
          "t.state === 'upcoming'" in _appjs
          and "return 'upcoming'" in _appjs
          and "未开始（老师开放后自动进入待办）" in _appjs
          and "未开始 ${groups.upcoming.length}" in _appjs)
    # 用户 2026-11-16：看板「立即更新」按钮**免密钥**——不再弹框要 Token。
    # GitHub 不允许匿名 dispatch，公开页面嵌 Token 违反铁律 2（凭据只进 Secrets），
    # 所以按钮跳转 GitHub workflow 页，用用户自己的登录态点 Run workflow。
    _idx = (rr.REPO_ROOT / "docs" / "index.html").read_text(encoding="utf-8")
    check("看板有立即更新按钮（免密钥，跳转 GitHub Run workflow 页）",
          "refreshBtn" in _idx and "triggerRefresh" in _appjs
          and "actions/workflows/assistant.yml" in _appjs
          and "localStorage" not in _appjs and "prompt(" not in _appjs)
    # v1.5.19 傍晚档科技热点：RSS 解析是纯函数（离线可测），抓取是联网不测
    _syn_xml = (b'<?xml version="1.0"?><rss version="2.0"><channel>'
                b'<item><title>\xe6\x96\xb0\xe9\x97\xbb\xe4\xb8\x80</title>'
                b'<link>https://example.com/1</link>'
                b'<pubDate>Mon, 10 Nov 2026 08:00:00 GMT</pubDate></item>'
                b'<item><title>\xe6\x96\xb0\xe9\x97\xbb\xe4\xba\x8c</title>'
                b'<link>https://example.com/2</link>'
                b'<pubDate>Mon, 10 Nov 2026 09:00:00 GMT</pubDate></item>'
                b'</channel></rss>')
    _syn_parsed = dp.parse_ithome_rss(
        _syn_xml, datetime(2026, 11, 10, 12, 0, tzinfo=BJ_TZ))
    check("IT之家 RSS 解析：标题/链接/顺序",
          len(_syn_parsed) == 2 and _syn_parsed[0]["title"] == "新闻一"
          and _syn_parsed[0]["url"] == "https://example.com/1", str(_syn_parsed))
    check("超过 24 小时的旧资讯被过滤",
          len(dp.parse_ithome_rss(_syn_xml, datetime(2026, 11, 12, 12, 0, tzinfo=BJ_TZ))) == 0)
    tn_fmt = dp.format_tech_news([
        {"title": "合成新闻一", "url": "https://example.com/1"},
        {"title": "合成新闻二", "url": ""}])
    check("科技热点格式化", "合成新闻一" in tn_fmt and "合成新闻二" in tn_fmt, tn_fmt[:120])
    # dusk 的科技热点 provider 可注入：selftest 离线确定性（不真的联 IT之家）
    blk_dusk = dp.info_block("dusk", now, "", tech_provider=lambda: "1. 合成热点\n   ↳ https://example.com/t")
    check("傍晚档含科技热点节（provider 注入）", "今日科技热点" in blk_dusk and "合成热点" in blk_dusk)
    check("科技热点节只出现在傍晚档",
          "科技热点" not in dp.info_block("morning", now, "")
          and "科技热点" not in dp.info_block("night", now, ""))
    # v1.7.1（用户 2026-11-17 报）：傍晚没有作业时必须明说并夸——空清单不是跳过
    _empty_dusk = dp.info_block("dusk", now, "", tech_provider=lambda: "",
                                tasks=[{"state": "done", "title": "已交", "course": "X",
                                        "kind": "work", "deadline": "", "url": ""}])
    check("傍晚无作业时明说全部搞定（确定性，AI 挂了也在）",
          "全部搞定" in _empty_dusk and "0 条要赶" in _empty_dusk, _empty_dusk[:120])
    # task_overview：AI 事实注入的作业概览（清零 / 未清零两种口径）
    _ov_empty = dp.task_overview(now, [{"state": "done", "title": "x", "course": "c",
                                        "kind": "work", "deadline": None, "url": ""}])
    check("作业概览：全做完时如实说「已全部做完」",
          "作业已全部做完" in _ov_empty and "0 条" in _ov_empty, _ov_empty)
    _ov_some = dp.task_overview(now, [
        {"state": "pending", "title": "周内", "course": "c", "kind": "work",
         "deadline": (now + timedelta(days=2)).isoformat(timespec="seconds"), "url": ""},
        {"state": "upcoming", "title": "未开", "course": "c2", "kind": "work",
         "deadline": None, "url": ""}])
    check("作业概览：有条数/今天截止口径/未开始另计",
          "还需要动手的 1 条" in _ov_some and "今天截止的已全部完成" in _ov_some
          and "另有 1 条未开始" in _ov_some, _ov_some)
    check("AI 的 facts 注入了作业概览（run 源码守卫）",
          "task_overview(now)" in inspect.getsource(dp.run))
    import prompts as _pm
    check("dusk/night 文案指令含「全部做完时必须夸」",
          "这必须夸" in _pm.PUSH_KIND_BRIEF["dusk"]
          and "今天作业全清" in _pm.PUSH_KIND_BRIEF["night"])
    # v1.5.20 用户要求：早报作业全部完成时，推送潜在任务（来自 Obsidian 笔记）
    pb = dp.plans_block([
        {"text": "学会 Rust", "state": "open", "confidence": 0.9},
        {"text": "读完一本书", "state": "open", "confidence": 0.7, "due_date": "2026-12-01"},
        {"text": "已取消的", "state": "cancelled"},
        {"text": "已完成的", "state": "done"},
    ])
    check("潜在任务块：只取 open、置信度降序、截止日标注",
          "学会 Rust" in pb and "读完一本书（截止 2026-12-01）" in pb
          and "已取消" not in pb and "已完成的" not in pb, pb[:120])
    check("潜在任务块：无 open 计划时为空", dp.plans_block([{"text": "x", "state": "done"}]) == "")
    all_done = [{"id": "d1", "state": "done", "course_id": "c1", "title": "已交的作业",
                 "kind": "work", "url": "u"}]
    blk_full = dp.info_block("morning", now, "", tech_provider=lambda: "", tasks=all_done)
    check("早报作业全清空时改推潜在任务（用户 2026-11-15 要求）",
          "全部清空" in blk_full and "潜在任务" in blk_full, blk_full[:150])
    blk_has = dp.info_block("morning", now, "", tech_provider=lambda: "",
                            tasks=[{"id": "p1", "state": "pending", "course_id": "c1",
                                    "title": "还没做的", "kind": "work", "url": "u"}])
    check("早报有作业时不推潜在任务",
          "潜在任务" not in blk_has and "待完成作业" in blk_has, blk_has[:150])

    # §5.3 嵌套加粗：**第 **10** 周** 在 Markdown 里渲染成字面星号
    _, _, sched_detail = rr.render_schedule(now)
    check("周次不再嵌套加粗", "**第 **" not in sched_detail, sched_detail)

    # §3.8 顶部告警按优先级只出一个，🔴 档必须真的存在
    check("采集失败出红卡", "🔴" in rr.render_alert({}, {"study": {"status": "failed"}}, now))
    check("截止未知出红卡", "🔴" in rr.render_alert({"unknown": 2}, {"study": {"ok": True}}, now))
    check("今天有截止出橙卡",
          "🟠" in rr.render_alert({"today": 1}, {"study": {"ok": True}}, now))
    check("数据过期出警告",
          "小时未更新" in rr.render_alert({}, {"study": {"ok": True, "last_success":
                                                        (now - timedelta(hours=9)).isoformat()}}, now))
    check("一切正常不出告警",
          rr.render_alert({}, {"study": {"ok": True, "last_success": now.isoformat()}}, now) == "")

    # §4.1 tick 调度：纯函数，离线就能验证「这轮该跑什么」
    base = now
    check("首轮全部到点", set(due_collectors({}, base)) == set(TICK_INTERVALS),
          str(due_collectors({}, base)))
    fresh = {k: (base - timedelta(minutes=10)).isoformat() for k in TICK_INTERVALS}
    check("刚跑过的不再跑", due_collectors({"last_ok": fresh}, base) == [])
    hourly = dict(fresh, study=(base - timedelta(minutes=60)).isoformat())
    check("study 满 60 分钟才跑", due_collectors({"last_ok": hourly}, base) == ["study"],
          str(due_collectors({"last_ok": hourly}, base)))
    check("失败也按间隔重试，不每 20 分钟烧一次登录链",
          due_collectors({"last_try": fresh}, base) == [])
    day = base.date().isoformat()
    sent_m = {"pushed": {"morning": day}}
    sent_mn = {"pushed": {"morning": day, "noon": day}}
    check("到点发早报", due_pushes({}, base.replace(hour=7)) == ["morning"])
    check("早报错过 2 小时仍补发", due_pushes({}, base.replace(hour=9)) == ["morning"])
    check("早报错过 4 小时仍补发（实测 cron 间隔可达 4h40m）",
          due_pushes({}, base.replace(hour=11)) == ["morning"])
    check("早报补发止于 noon 时段", due_pushes({}, base.replace(hour=13)) == ["noon"])
    check("中午发问好", due_pushes(sent_m, base.replace(hour=12)) == ["noon"])
    check("中午错过 5 小时仍补发", due_pushes(sent_m, base.replace(hour=17)) == ["noon"])
    check("中午错过 6 小时不再补发（18 点起是傍晚档的地盘）",
          "noon" not in due_pushes(sent_m, base.replace(hour=18)))
    check("傍晚档不与午/晚重复", due_pushes({}, base.replace(hour=19)) == ["dusk"])
    check("晚上发总结", due_pushes(sent_mn, base.replace(hour=22)) == ["night"])
    check("夜间总结补到当天 23:59", due_pushes(sent_mn, base.replace(hour=23, minute=59)) == ["night"])
    check("凌晨不补发昨晚的总结",
          due_pushes(sent_mn, (base + timedelta(days=1)).replace(hour=0, minute=30)) == [])
    check("同一天不重复发", due_pushes(sent_m, base.replace(hour=7)) == [])
    # v1.5.19 傍晚冲刺档（用户新增）：18:30 前后触发，补发到 20:30，不挤晚间档
    check("傍晚 18 点发冲刺档", due_pushes({}, base.replace(hour=18)) == ["dusk"])
    check("傍晚档补发到 20 点", due_pushes({}, base.replace(hour=20)) == ["dusk"])
    check("傍晚档 21 点不再补发（不挤晚间档）", due_pushes({}, base.replace(hour=21)) == [])
    check("四个时段齐全", set(dp.KINDS) == {"morning", "noon", "dusk", "night"}, str(dp.KINDS))

    # §2.2 私人路径黑名单：值来自 Secret，判定按路径前缀，已入库的也要清掉
    check("黑名单按前缀命中", dc.is_excluded("Tasks/私人/长期计划.md", ["Tasks/私人"]))
    check("黑名单不误伤同前缀兄弟目录",
          not dc.is_excluded("Tasks/私人笔记公开版/a.md", ["Tasks/私人"]))
    kept_x, drop_n = dc.drop_excluded(
        [{"source_file": "Tasks/私人/a.md"}, {"source_file": "Tasks/日/b.md"}], ["Tasks/私人"])
    check("已入库的私人条目会被清掉", drop_n == 1 and len(kept_x) == 1, str(kept_x))
    check("无黑名单时不动数据", dc.drop_excluded([{"source_file": "x"}], []) == ([{"source_file": "x"}], 0))

    # §5.2 文档是交接材料：引用了不存在的函数，下一个 AI 会去找、找不到、然后自己造一个
    mods = ("render_readme", "study_collector", "diary_collector", "diary_writeback",
            "notice_collector", "schedule_parser", "week_svg", "dedup", "utils",
            "main", "daily_push", "ai_client", "prompts", "anime_image")
    # 排除文件扩展名：`prompts.py` 是文件名不是函数引用，早先的写法会把 .py 当函数名
    exts = ("py", "md", "json", "js", "yml", "yaml", "ics", "svg", "txt", "html", "css")
    ref_re = _re.compile(r"`(" + "|".join(mods) + r")\.([A-Za-z_][A-Za-z0-9_]*)`")
    # 只扫「常驻说明」：CHANGELOG 与不足与风险是**历史记录**，
    # 它们引用旧函数名/旧频率是在举证「当时错在哪」，不是给下一个 AI 的指令。
    living = sorted((rr.REPO_ROOT / "docs").glob("*.md"))
    living = [p for p in living if p.name not in ("不足与风险.md",)]
    living += sorted((rr.REPO_ROOT / "scripts").glob("*_template.md"))
    living += [rr.REPO_ROOT / n for n in ("README.md", "AGENT.md", "DEV_GUIDE.md")]
    bad_refs, stale_words = [], []
    # 只禁「当成现状陈述就是错的」措辞。`pa-push` / `github.event.schedule` 这类**设计名词**
    # 不进这个名单：文档里「原来用 X，因为 Y 改成了 Z」是有价值的决策记录，禁掉会逼着人删掉理由。
    # 现状由上面那几条 yml 断言守（只有一条 cron、只有一个 group、MODE=tick），那才是权威。
    forbidden = ("bucket_tasks", "日记每 20 分钟", "每天 4 次增量")
    for p in living:
        if not p.exists():
            continue
        txt = p.read_text(encoding="utf-8")
        for mod, fn in ref_re.findall(txt):
            if fn in exts:
                continue
            try:
                m = importlib.import_module(mod)
            except Exception:
                bad_refs.append(f"{p.name}:{mod}(导入失败)")
                continue
            if not hasattr(m, fn):
                bad_refs.append(f"{p.name}:{mod}.{fn}")
        stale_words += [f"{p.name}:{w}" for w in forbidden if w in txt]
    check("文档引用的函数都真实存在", not bad_refs, str(bad_refs[:10]))
    check("文档不再写着过时的函数名与频率", not stale_words, str(stale_words[:10]))

    # AI 运维手册把「日志里搜什么」当成验收证据。日志文案一改，手册就变成假指令，
    # 下一个 AI 会搜不到、然后以为功能坏了。这里把手册承诺的关键字锁住。
    src_all = "\n".join(p.read_text(encoding="utf-8")
                        for p in sorted((rr.REPO_ROOT / "scripts").glob("*.py")))
    promised = ("详情页补全截止时间", "名言点评回填", "抓取站点", "课程总数", "扫描课程",
                "变更文件", "名言合并", "计划合并", "公告合并", "文案来源", "解析冒烟",
                "课表解析", "本周课表 SVG 已生成", "README 已生成", "任务清单",
                "凌晨写回窗口", "不在凌晨窗口", "写回：", "低置信度丢弃", "计划归档",
                "清理黑名单生效前入库的私人条目", "课表源文件污染修复",
                "名言价值打分", "[urgent-same]", "[image] 选中", "[push]", "[newtask]", "[memory]")
    missing = [s for s in promised if s not in src_all]
    check("运维手册承诺的日志关键字都真实存在", not missing, str(missing))

    # 渲染产物里的相对链接必须真的能点开。实测踩过两次：render_plans/render_notices
    # 的截断行少写 ../（产物在 docs/ 下），以及 index.html 页脚写 ../README.md
    # 逃出了 Pages 根（Pages 源就是 docs/）。scripts/*_template.md 不算 ——
    # 它们的链接是相对「渲染后的位置」，不是相对模板自己。
    from urllib.parse import unquote

    link_re = _re.compile(r"\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
    html_re = _re.compile(r"(?:href|src)=\"([^\"]+)\"")
    code_re = _re.compile(r"`[^`]*`")
    bad_links = []
    escaped = []
    pages = [rr.REPO_ROOT / "docs" / "index.html"]
    pages += sorted(rr.REPO_ROOT.glob("*.md"))
    pages += sorted((rr.REPO_ROOT / "docs").glob("*.md"))
    docs_root = (rr.REPO_ROOT / "docs").resolve()
    for p in pages:
        if not p.exists():
            continue
        txt = p.read_text(encoding="utf-8")
        if p.suffix == ".md":
            # 代码块与行内代码里的链接是「在讲这个链接」，不是真链接。审计文档必须
            # 能原样引用坏链接（§9.7 引了两条），否则守卫会逼着文档把证据改掉。
            kept, fence = [], False
            for ln in txt.splitlines():
                if ln.lstrip().startswith(("```", "~~~")):
                    fence = not fence
                elif not fence:
                    kept.append(code_re.sub("", ln))
            txt = "\n".join(kept)
        targets = link_re.findall(txt) + (html_re.findall(txt) if p.suffix == ".html" else [])
        for t in targets:
            if t.startswith(("http://", "https://", "mailto:", "#", "data:")):
                continue
            rel = unquote(t.split("#")[0])
            if not rel:
                continue
            dest = (p.parent / rel).resolve()
            if not dest.exists():
                bad_links.append(f"{p.name}:{t}")
            # Pages 源是 main 的 /docs，站点根就是 docs/。文件在磁盘上存在不代表
            # 线上点得开：../README.md 会解析成 jiuyuan233.github.io/README.md → 404。
            if p.name == "index.html" and docs_root not in dest.parents:
                escaped.append(t)
    check("文档与看板的相对链接全部可达", not bad_links, str(bad_links[:10]))
    check("看板链接不逃出 Pages 根目录", not escaped, str(escaped))

    # v1.6.4 交接守卫：AI日常维护手册是给弱 AI 的操作指令，它指向的每个常量/文件
    # 都必须真实存在——手册变假指令 = 弱 AI 照做即报错/白干。锁住关键锚点。
    _maint = (rr.REPO_ROOT / "docs" / "AI日常维护手册.md").read_text(encoding="utf-8")
    _anchors = {
        "PERSONA": "PERSONA" in _maint and hasattr(__import__("prompts"), "PERSONA"),
        "PUSH_KIND_BRIEF": "PUSH_KIND_BRIEF" in _maint
                            and hasattr(__import__("prompts"), "PUSH_KIND_BRIEF"),
        "PUSH_RULES": "PUSH_RULES" in _maint and hasattr(__import__("prompts"), "PUSH_RULES"),
        "MEMORY_INSTRUCTIONS": "MEMORY_INSTRUCTIONS" in _maint
                               and hasattr(__import__("prompts"), "MEMORY_INSTRUCTIONS"),
        "MORNING_TASK_LIMIT": "MORNING_TASK_LIMIT" in _maint
                               and hasattr(__import__("daily_push"), "MORNING_TASK_LIMIT"),
        "TICK_INTERVALS": "TICK_INTERVALS" in _maint and "TICK_INTERVALS" in globals(),
        "PUSH_HOURS": "PUSH_HOURS" in _maint and "PUSH_HOURS" in globals(),
        "quiz_browser 开关": "quiz_browser" in _maint,
        "memory_enabled 开关": "memory_enabled" in _maint,
        "pushed 手改说明": "pushed" in _maint,
        "竞赛日历": "竞赛日历" in _maint
                   and (rr.REPO_ROOT / "docs" / "竞赛日历.md").exists(),
        "readme_template 模板指引": "readme_template" in _maint,
        "白名单文件都存在": all((rr.REPO_ROOT / f).exists() for f in (
            "scripts/prompts.py", "scripts/daily_push.py", "scripts/anime_image.py",
            "data/sources.json", "data/runtime.json", "data/diary/plans.json",
            "scripts/readme_template.md")),
    }
    check("日常维护手册指向的锚点全部真实存在（手册不是假指令）",
          all(_anchors.values()),
          str([k for k, v in _anchors.items() if not v]))
    check("日常维护手册已挂进 README 目录区（模板）",
          "AI日常维护手册" in (rr.REPO_ROOT / "scripts" / "readme_template.md")
          .read_text(encoding="utf-8"))

    log(f"[selftest] 失败 {len(failures)} 项")
    return not failures


def do_push(kind: str) -> bool:
    """三时段推送（v1.6.0 加记账防重）。

    用户硬要求「cron-job.org 定时 + GitHub 自带定时并存，但不能重复发」。
    三路触发源（cron-job.org → tick / GitHub cron → tick / 接力链 → tick）的
    防重在 do_tick 的 due_pushes（pushed[kind] 按日期记账）；这里再兜一层：
    **手动直派 morning/noon/dusk/night 也先查记账**，今天发过的直接跳过，
    并把成功结果写回 runtime.json（提交步骤会带上它，下一轮任何触发源都看得到）。
    """
    import daily_push
    rt = load_json(RUNTIME_PATH) or {}
    today = now_bj().date().isoformat()
    if (rt.get("pushed") or {}).get(kind) == today:
        log(f"[push] {kind} 今天已推送过（记账防重，跳过重复触发）")
        return True
    ok = bool(daily_push.run(kind).get("ok"))
    if ok:
        rt.setdefault("pushed", {})[kind] = today
        save_json(RUNTIME_PATH, rt)
        log(f"[push] {kind} 推送成功并记账（pushed.{kind} = {today}）")
    return ok


HANDLERS = {
    "tick": do_tick,
    "finalize": do_finalize,
    "schedule": do_schedule,
    "diary": do_diary,
    "study": do_study,
    "notice": do_notice,
    "backfill": lambda: __import__("diary_collector").backfill_comments().get("ok", False),
    "probe": do_probe,
    "studydiag": lambda: __import__("study_collector").diagnose().get("ok", False),
    "selftest": do_selftest,
    "morning": lambda: do_push("morning"),
    "noon": lambda: do_push("noon"),
    "dusk": lambda: do_push("dusk"),
    "night": lambda: do_push("night"),
}


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "tick").strip().lower()
    log(f"===== 个人助手启动 · 模式 {mode} · {now_bj().strftime('%Y-%m-%d %H:%M:%S')} =====")

    if mode == "all":
        results = {}
        for m in COLLECT_MODES:
            try:
                results[m] = HANDLERS[m]()
            except Exception as e:
                log(f"模式 {m} 异常：{exc_brief(e)}", level="ERROR")
                results[m] = False
        try:
            results["finalize"] = do_finalize()
        except Exception as e:
            log(f"渲染异常：{exc_brief(e)}", level="ERROR")
            results["finalize"] = False
        log(f"全部完成：{results}")
        return 0 if results.get("finalize") else 1

    handler = HANDLERS.get(mode)
    if handler is None:
        log(f"未知模式 {mode}，可用：{', '.join(list(HANDLERS) + ['all'])}", level="ERROR")
        return 2

    try:
        ok = handler()
    except Exception as e:
        # 只发异常摘要，不发完整栈：栈里的局部变量可能带 cookie / 密码（审计 §2.6）
        log(f"模式 {mode} 异常：{exc_brief(e)}", level="ERROR")
        return 1

    # 采集模式跑完顺手渲染，README 立刻反映最新数据
    if ok and mode in COLLECT_MODES:
        try:
            do_finalize()
        except Exception as e:
            log(f"渲染异常：{exc_brief(e)}", level="ERROR")
            return 1

    log(f"===== 模式 {mode} 结束：{'成功' if ok else '失败'} =====")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())