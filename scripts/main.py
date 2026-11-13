"""统一入口。GitHub Actions 只调这一个文件，用模式参数决定干什么。

模式设计的核心约束：**finalize 必须零网络、零 token、40 秒内跑完**，
因为它每 20 分钟跑一次。登录类采集各自独立频率，互不拖累。

  python scripts/main.py finalize   # 只渲染 + 离线重算课表，不联网（每 20 分钟）
  python scripts/main.py diary      # Gitee 日记 → 渲染
  python scripts/main.py study      # 学习通 → 渲染
  python scripts/main.py notice     # 校园公告 → 渲染
  python scripts/main.py schedule   # 课表 → 渲染
  python scripts/main.py all        # 全部采集 → 渲染
  python scripts/main.py probe      # 海外 IP 连通性探测
  python scripts/main.py selftest   # 解析器自检（离线，不需要凭据）
"""
from __future__ import annotations

import sys
import traceback

from utils import log, now_bj

COLLECT_MODES = ("diary", "study", "notice", "schedule")


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
    """从 Actions  runner 探测各站点可达性。

    这是整个架构唯一的致命未知：所有实测成功都来自国内出口 IP，
    GitHub Actions 是美国 Azure IP，学校站点可能直接拒连。
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
        ("Gitee", "https://gitee.com/api/v5/user"),
        ("AI 中转", "https://api.chatanywhere.org/v1/models"),
        ("飞书", "https://open.feishu.cn/open-apis/bot/v2/hook/0"),
    ]
    ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36")
    try:
        ip = requests.get("https://api.ipify.org?format=json", timeout=15).json().get("ip")
        log(f"Actions 出口 IP：{ip}")
    except Exception as e:
        log(f"出口 IP 查询失败：{e}", level="WARN")

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
    return reachable > 0


def do_selftest() -> bool:
    """离线自检：用内置样本验证解析器，不需要凭据也不需要网络。"""
    from datetime import datetime

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

    work_html = '''<h2>陈蒙26级高等数学A（一）</h2>
    <li onclick="goTask(this);" data="https://mooc1.huuc.edu.cn/mooc-ans/mooc2/work/task?courseId=219232606&classId=153566712&cpi=577967959&workId=58370553&answerId=55746672&enc=abc123" tabindex="0" aria-label="3-5函数的极值与最值 ; 待批阅" role="link">
      <div class="right-content"><p class="overHidden2 fl">3-5函数的极值与最值</p>
      <i class="label fl">互评</i><p class="status fl">待批阅</p></div>
      <div class="time notOver" tabindex="0"><img src="x.png" />剩余1小时12分钟</div>
    </li>
    <li onclick="goTask(this);" data="https://mooc1.huuc.edu.cn/mooc-ans/mooc2/work/eval-list?courseId=219232606&classId=153566712&cpi=577967959&workId=58263400&answerId=55727436&enc=def456" aria-label="3.3泰勒公式 ; 待互评">
      <div class="right-content"><p class="overHidden2 fl">3.3泰勒公式</p>
      <i class="label fl">互评</i><p class="status fl">待互评</p></div>
    </li>'''
    course = {"course_id": "219232606", "class_id": "153566712",
              "cpi": "577967959", "name": "陈蒙26级高等数学A（一）"}
    rows = study_collector.parse_work_rows(work_html, course, now)
    check("作业行数=2", len(rows) == 2, f"实际 {len(rows)}")
    if len(rows) == 2:
        check("互评待批阅升级为 pending", rows[0]["state"] == "pending", rows[0]["state"])
        check("倒计时换算截止时间", rows[0]["deadline"] == "2026-11-12T23:12:00+08:00",
              str(rows[0]["deadline"]))
        check("eval-list 识别为互评", rows[1]["kind"] == "eval", rows[1]["kind"])
        check("待互评为 pending", rows[1]["state"] == "pending", rows[1]["state"])
        check("ID 不含 enc", "abc123" not in rows[0]["id"] and "def456" not in rows[1]["id"])

    exam_html = '''<div class="bottomList"><ul><li>
    <div onclick="viewExamAnswer('10959519','170844300')">
    <div class="right-content"><p class="overHidden2 fl">第二章测试题</p>
    <p class="status fl">已完成</p></div></div></li></ul></div>'''
    erows = study_collector.parse_exam_rows(exam_html, course, now)
    check("考试行数=1", len(erows) == 1, f"实际 {len(erows)}")
    if erows:
        check("考试状态 done", erows[0]["state"] == "done", erows[0]["state"])
        check("考试标题", erows[0]["title"] == "第二章测试题", erows[0]["title"])

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

    ics = schedule_parser.parse_ics(schedule_parser.ICS_PATH)
    check("课表 VEVENT 解析", len(ics) > 100, f"实际 {len(ics)}")
    if ics:
        ev = schedule_parser.build_events(ics)
        check("课表节次映射", all(1 <= e["slot_start"] <= e["slot_end"] <= 11 for e in ev))
        check("课表周次存在", sum(1 for e in ev if e.get("week")) > 100)
        ss = schedule_parser.guess_semester_start(ev)
        check("学期起点推断", ss == "2026-09-07", str(ss))

    from dedup import merge_tasks, merge_notices
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

    log(f"[selftest] 失败 {len(failures)} 项")
    return not failures


HANDLERS = {
    "finalize": do_finalize,
    "schedule": do_schedule,
    "diary": do_diary,
    "study": do_study,
    "notice": do_notice,
    "probe": do_probe,
    "selftest": do_selftest,
}


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "finalize").strip().lower()
    log(f"===== 个人助手启动 · 模式 {mode} · {now_bj().strftime('%Y-%m-%d %H:%M:%S')} =====")

    if mode == "all":
        results = {}
        for m in COLLECT_MODES:
            try:
                results[m] = HANDLERS[m]()
            except Exception:
                log(f"模式 {m} 异常：\n{traceback.format_exc()}", level="ERROR")
                results[m] = False
        try:
            results["finalize"] = do_finalize()
        except Exception:
            log(f"渲染异常：\n{traceback.format_exc()}", level="ERROR")
            results["finalize"] = False
        log(f"全部完成：{results}")
        return 0 if results.get("finalize") else 1

    handler = HANDLERS.get(mode)
    if handler is None:
        log(f"未知模式 {mode}，可用：{', '.join(list(HANDLERS) + ['all'])}", level="ERROR")
        return 2

    try:
        ok = handler()
    except Exception:
        log(f"模式 {mode} 异常：\n{traceback.format_exc()}", level="ERROR")
        return 1

    # 采集模式跑完顺手渲染，README 立刻反映最新数据
    if ok and mode in COLLECT_MODES:
        try:
            do_finalize()
        except Exception:
            log(f"渲染异常：\n{traceback.format_exc()}", level="ERROR")
            return 1

    log(f"===== 模式 {mode} 结束：{'成功' if ok else '失败'} =====")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())