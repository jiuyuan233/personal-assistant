# AI 运维手册

**这份文档是写给「下一个接手这个仓库的 AI」的，也写给人看。**
它不讲设计理由（那在 [技术路径.md](技术路径.md)），不讲字段定义（那在 [数据结构.md](数据结构.md)），
只讲**你该按什么顺序做什么、怎么确认做对了、什么绝对不能碰**。

每一步都给了可以直接执行的命令。看到「✅ 验收」就是必须亲眼确认的证据，
**不要把「代码写了」当成「功能验证过了」** —— 这个项目栽过跟头，详见 [不足与风险.md](不足与风险.md)。

---

## 0. 阅读顺序（别跳）

| 顺序 | 文档 | 为什么先读它 |
| --- | --- | --- |
| 1 | **本文件** | 知道怎么动手、怎么验 |
| 2 | [AGENT.md](../AGENT.md) | **六条铁律**，违反会直接搞坏项目 |
| 3 | [不足与风险.md](不足与风险.md) | 每条问题的根因 + 修复状态 + **「不是缺陷别乱改」10 项** |
| 4 | [开发习惯.md](开发习惯.md) | 16 条协作规则，每条附用户原话 |
| 5 | [个人画像.md](个人画像.md) | 用户是谁、说话风格的硬边界 |
| 6 | [工作原理.md](工作原理.md) | 一次运行的完整数据流 |
| 7 | [技术路径.md](技术路径.md) | 要改选型时才读，含被否决的方案 |

**用户的三条硬性协作要求**（原话，别违反）：

1. 「实现方式优先在 github 现成写好的项目，不要盲目自己开发」→ 先找现成的，别从零造轮子
2. 「不要花费大量的 token」→ 别把整个仓库读一遍再动手；按需读，别重复采集
3. 「你只是负责开发，后续的更新用别的 AI」→ **你写的每一行文档都是给下一个 AI 的接口**，
   写清楚「为什么」比写清楚「是什么」更重要

---

## 1. 30 秒理解这个仓库

```
GitHub Actions（一条 cron：*/20 * * * *）
        ↓ 模式固定 tick
scripts/main.py  ── 质量门禁：ruff + selftest（不过就红，不采集）
        ↓ do_tick() 读 data/runtime.json 的时间戳，决定这轮跑谁
   ┌────┴─────────────────────────────────┐
   │ study（60分钟）  学习通四步链 → 作业/考试 │
   │ diary（120分钟） Gitee API → 名言/计划   │  ← 联网、花 token
   │ notice（360分钟）公开公告页 → 比赛信息    │
   │ schedule（每轮） WakeUp .ics → 课表      │  ← 离线
   │ morning/noon/night（每天各一次）→ 飞书文案│
   └────┬─────────────────────────────────┘
        ↓ dedup.py 合并（公告旧值优先 / 作业新值优先）
     data/*.json
        ↓ render_readme.py + week_svg.py（★零网络、零 token）
   README.md · docs/*.md · docs/data/*.json · docs/week.svg
        ↓ git add / commit / pull --rebase / push（不 force）
        ↓ notify_feishu.js（if: always()）
     飞书：有文案发纯文本，没文案发运行状态卡片
```

**三个必须记住的结构约束：**

- **渲染层不 import 任何采集器，也不 import `ai_client`。** 这是「finalize 零网络」的结构保证，
  不是靠自觉。往 `render_readme.py` / `week_svg.py` 里塞采集逻辑会直接破坏它。
- **采集模式只提交 `data`**，README/docs 的提交权独占给 `tick`/`finalize`/`all`。
  这条边界是一次真实数据丢失事故换来的（[技术路径 §11](技术路径.md)）。
- **频率写在 Python 里，不在 cron 里。** 改频率改 `main.TICK_INTERVALS` / `PUSH_HOURS`，
  **不要往 workflow 加 cron**（selftest 有断言守着「只有一条 cron」）。

---

## 2. 环境准备

### 2.1 在 Actions 上（推荐，什么都不用装）

`Actions → 个人助手 → Run workflow → 选 mode`。默认 `tick`。
workflow 自己装 Python 3.11 + Node 20 + `requirements.txt` + ruff。

**⚠️ 派发必须串行，一次一个。** 并发组 `pa-run` 只保留一个 pending run，
一次派多个，后派的会把先派的**直接 CANCELLED**（实测两条）。等上一个 completed 再派下一个。

### 2.2 在本地（改代码时强烈建议，省掉整轮 Actions 往返）

```bash
pip install -r scripts/requirements.txt
pip install ruff

ruff check scripts --select E9,F63,F7,F82,F821,F401
python scripts/main.py selftest
```

**这两条就是 workflow 的「质量门禁」步骤，本地跑绿了再推。**

#### Windows 本地的三个坑（都实测踩过）

| 症状 | 根因 | 解法 |
| --- | --- | --- |
| `UnicodeEncodeError: 'gbk' codec can't encode '✅'` | 控制台默认 GBK | `utils.py` 已在 import 时把 stdout/stderr 重设成 UTF-8。若仍报错，用 `python -X utf8` |
| `ModuleNotFoundError: No module named 'utils'` | **embeddable 版 Python** 带 `python311._pth`，处于 isolated 模式：既不加脚本目录进 `sys.path`，也**忽略 `PYTHONPATH`** | 用标准安装版 Python；或写个包装器 `sys.path.insert(0, scripts目录)` + `runpy.run_path(...)` |
| `ZoneInfoNotFoundError: Asia/Shanghai` | 缺 tzdata | `utils.py` 已 fallback 到固定 `UTC+8`（中国 1991 年起无夏令时，等价）。要精确就 `pip install tzdata` |

**本地跑 `finalize` 是安全的**：零网络、零 token，只读 `data/` 和 `assets/`，重写生成物。
**本地跑 `study`/`diary`/`notice` 会真的联网、真的花 token、`diary` 在凌晨窗口内会真的写用户的 Gitee 仓库** ——
不想动真实数据就只跑 `selftest` / `finalize` / `probe`。

---

## 3. 改动流程（五步，别跳步）

### 第 1 步：定位

用 grep 找关键字，**别把整个仓库读一遍**（用户明确要求省 token）。
不知道从哪找时看 [仓库结构.md](仓库结构.md) 的「加一个新数据源要动哪些文件」。

### 第 2 步：改代码

- 改**措辞/结构** → 改 `scripts/*_template.md`，**不要改生成物**（README.md、docs/公告.md 等）
- 改**行为** → 改对应的 `scripts/*.py`
- 改**配置** → 只改 `data/sources.json`（唯一常规手改文件）
- 改**频率** → 改 `main.TICK_INTERVALS` / `PUSH_HOURS` / `PUSH_CATCHUP_HOURS`
- 改**人格** → 只改 `prompts.py` 的 `PERSONA`（全仓库唯一一份）

### 第 3 步：同步自检

**改任何解析器，必须同步改 `main.do_selftest()` 的断言。**
自检是唯一不消耗凭据、不依赖网络就能验证解析逻辑的手段。

学习通页面改版时还要更新 `study_collector.py` 的 `SAMPLE_*` 常量 ——
`parse_smoke()` 用它们跑一遍全解析链，`probe` 模式也会调它，这是最早的报警。

### 第 4 步：本地验证

```bash
ruff check scripts --select E9,F63,F7,F82,F821,F401   # 必须 All checks passed
python scripts/main.py selftest                      # 必须 失败 0 项
python scripts/main.py finalize                      # 改了渲染/课表时跑，看生成物对不对
```

**F821（未定义名字）这一条曾经抓住过 `BJ_TZ` 忘了 import 的事故** —— 别把它从选择器里删掉。

### 第 5 步：推送 + Actions 验证

```bash
git add <具体文件>          # 别 git add -A，容易带进敏感文件
git commit -m "..."
git pull --rebase origin main
git push origin main       # ★ 绝不 --force
```

然后**串行**派发对应模式，**读日志确认**（不是看绿勾就算完）：

```bash
# 用 GitHub API 读日志（gh CLI 不一定装了）
# GET /repos/{owner}/{repo}/actions/runs?per_page=5      → 找 run id
# GET /repos/{owner}/{repo}/actions/runs/{id}/jobs       → 找 job id
# GET /repos/{owner}/{repo}/actions/jobs/{id}/logs       → 302 到 Azure blob
```

**⚠️ 两个实测坑：**
- 日志接口 302 跳到 Azure blob 时**必须把 Authorization 头去掉**，否则 403
- API 返回的 `head_sha` 是**完整 40 位**，和你手里的短 sha 比要用 `startsWith`

---

## 4. 十四个模式速查表

| 模式 | 干什么 | 联网 | 花 token | 提交文件 | 怎么验 |
| --- | --- | --- | --- | --- | --- |
| **`tick`** | ★cron 唯一入口，按 runtime 决定跑谁，最后必渲染 | 看跑谁 | 看跑谁 | `data docs README.md` | 日志第一行 `[tick] 本轮采集 […] · 推送 […]` |
| `finalize` | 只渲染 + 离线重算课表 | ❌ | ❌ | `data docs README.md` | `README 已生成` + `本周课表 SVG 已生成` |
| `study` | 学习通采集 + 渲染 | ✅ | ❌ | 只 `data` | `课程总数：14`、`扫描课程：14，成功：14` |
| `diary` | Gitee 采集 + 凌晨写回 | ✅ | ✅ | 只 `data`（+ 用户日记仓库） | `变更文件：N`、`名言合并：新增 N` |
| `notice` | 公告采集 + AI 打分 | ✅ | ✅ | 只 `data` | `抓取站点：N，成功：N` |
| `schedule` | 只重算课表 | ❌ | ❌ | 只 `data` | `课表解析：186 条上课记录` |
| `all` | 逐个跑全部采集 + 渲染 | ✅ | ✅ | `data docs README.md` | 单个失败不影响其他 |
| `backfill` | 给存量名言补 AI 点评（每批 8 条） | ✅ | ✅ | 只 `data` | `名言点评回填：补上 N 条，仍空 M 条` |
| `probe` | 13 个目标连通性 + 解析冒烟 | ✅ | ❌ | **不提交** | `可达：N`、`解析冒烟：{...}` |
| `studydiag` | dump 学习通页面形状（**数字全打码**） | ✅ | ❌ | **不提交** | 输出里数字应是 `D`、标题应是 `TITLE` |
| `selftest` | 164 项离线断言 | ❌ | ❌ | **不提交** | `失败 0 项` |
| `morning` | 早 7 点飞书文案 | ✅ | ✅ | **不提交** | `文案来源：AI`（或 `降级`） |
| `noon` | 中午问好 | ✅ | ✅ | **不提交** | 同上 |
| `night` | 晚间总结 | ✅ | ✅ | **不提交** | 同上 |

**不带参数 = `tick`。**

**「日志就是接口」**：飞书卡片的指标是用正则从日志里抠的。
改了 `scripts/*.py` 的日志文案，**必须同步改 `notify_feishu.js` 的 `TASK_TEMPLATES`**，
否则卡片上全是「—」。selftest 守不住这条（它不解析日志），只能靠人。

---

## 5. 逐功能验收清单

用户的六大需求 + 审计修复项，每条给**怎么验**和**看什么证据**。
**接手时先按这张表过一遍，确认哪些是真的能用、哪些只是代码写了。**

### 5.1 学习通：14 门课全覆盖、按活跃度排序、只显示能做的

| 要验的点 | 怎么验 | 合格证据 |
| --- | --- | --- |
| 全部课程都出现 | 派发 `study`，看 README 任务清单表格行数 | **14 行**，没作业的课写「**无**」 |
| 按活跃度排序 | 看表格「作业数」列 | 降序；同数按待完成数、再按名字（`ordered_courses()`） |
| 同一门课不串班 | 搜有没有重复课程名 | 课程身份 = `(course_id, class_id)`，两个教学班各自一行 |
| 已完成的被剔除 | 看 [已剔除任务.md](已剔除任务.md) | 按课程分组，每条写明原因（逾期 / 非待完成） |
| 逾期的被剔除 | 同上 | 「**已逾期**：截止 …，已过 N 天，做不了了」 |
| **「超一周即剔除」规则已废除** | 搜代码和文档 | 找不到这条规则；老作业只要没逾期就还在清单里 |

### 5.2 ★截止时间：用户报的核心 bug

**症状**：明明有截止时间，程序找不到，于是把早已截止的作业显示成「更远」。

**根因**：逾期作业的行**根本不含 `time` div**，旧代码把 deadline 留成 `null`，
`drop_reason()` 判不出逾期 → 混进「更远」。实测 38 条任务里 33 条 deadline 为 null。

| 要验的点 | 怎么验 | 合格证据 |
| --- | --- | --- |
| 详情页能解析出绝对时间 | `selftest` | ✅ 作业详情页解析出真实截止 / 解析出开始时间 / 跨年窗口止时间进到下一年 |
| 待完成且无截止的任务会去抓详情页 | 派发 `study`，搜日志 | `详情页补全截止时间：N 条` |
| 抓不到的**单列一类** | 看 README | `⚠️截止未知`，**不在「更远」里**；顶部出 🔴 告警 |
| 网页看板同样单列 | 打开 `docs/index.html` | 有「🔴 截止未知（可能早已逾期，点进去自己确认）」这一桶 |
| 精确值不被估算值覆盖 | `selftest` | ✅ 精确截止时间不被倒计时覆盖 |

**⚠️ 残留风险**：详情页**不给年份**，年份是按「距抓取时刻最近的一年」推算的。
**学期交界（12 月底/1 月初）和暑假前后可能系统性错一年**，这是唯一需要人工核对的地方。

### 5.3 日记：潜在计划写回 + 复查滚动

用户要的闭环：**13 号挖到 → 写进 14 号日记（md 复选框）→ 15 号复查，没勾的滚到 15 号**。

| 要验的点 | 怎么验 | 合格证据 |
| --- | --- | --- |
| 只在凌晨写 | 白天派发 `diary`，搜日志 | `不在凌晨窗口`，**零提交** |
| 凌晨真的写进去了 | 窗口内派发 `diary`，去 Gitee 看当天日记 | 出现 `🤖 助手待办` 区块 + `- [ ]` 复选框 |
| 用户正文零改动 | `selftest` | ✅ 写入保留用户正文 / 重试保住用户并发编辑 |
| 勾选后不再滚 | `selftest` | ✅ 已完成计划不再滚进新一天 |
| 老计划不被饿死 | 看 `pick_plans()` 排序键 | `first_seen` **升序**（老的先写） |
| 不无限自我复制 | `selftest` | ✅ 助手区块被剥掉后才抽取 |
| tick 会强制补跑 | 窗口内搜日志 | `凌晨写回窗口 … 内今天还没写回，强制加跑 diary` |

**⚠️ 这一条曾经从未真实执行过**（审计 §4.6）。接手时**必须亲自去 Gitee 仓库看一眼**，
不能只看 selftest 绿 —— selftest 用的是 FakeGitee，证明不了真实 API 的权限和路径对不对。

**真实验证步骤**：
1. 确认 `GITEE_TOKEN` 有 **写权限**（只读会 403）
2. 确认 `data/sources.json` 的 `diary.daily_dir` 和真实目录一致（当前是 `日`）
3. 在凌晨窗口内（北京 00:00–06:00）派发 `diary`
4. 搜日志 `写回：` → 应看到 `YYYY-MM-DD 写入 N 条 → Tasks/日/YYYY-MM-DD.md（新建/更新）`
5. **去 Gitee 网页打开那个文件**，确认区块在、复选框在、正文没被动
6. 第二天在 Obsidian 里勾掉一条，再跑 `diary`，确认 `state` 变 `done` 且不再滚

### 5.4 课表：SVG + 实时时间线

| 要验的点 | 怎么验 | 合格证据 |
| --- | --- | --- |
| 周数每天前进 | 派发 `finalize`，看 README | `当前第 N 周`，N 随日期增加 |
| 课程名没被污染 | `selftest` | ✅ 时钟前缀污染全部修好 / 修复后没有空课程名 |
| SVG 里的线是**浅灰** | 打开 `docs/week.svg` 搜颜色 | 不含 `#FA5252`；selftest ✅ SVG 源码不再烘焙红色时间线 |
| 网页红线实时移动 | **浏览器打开 `docs/index.html`**，等 30 秒 | 红线移动；selftest ✅ 网页端仍有实时时间线 |
| 几何两边一致 | 改 `ROW_H`/`SLOTS` 后 | Python `time_y()` 和 JS `nowLineY()` 必须同步改 |

**⚠️ 在 README 里看 SVG 是看不到实时线的** —— GitHub 把它当静态图片渲染，不执行 JS。
实时线只在网页看板里存在。**别跟用户说「README 里的线会动」。**

**教师列恒为空是预期行为**：`assets/WakeUp导入课表.ics` 已脱敏剥离教师名。
**不要去"修"它**，换回未脱敏原件即生效。

### 5.5 名言 + AI 点评

| 要验的点 | 怎么验 | 合格证据 |
| --- | --- | --- |
| 新名言带点评 | 派发 `diary`，看 README | `> 💬 …` |
| 存量名言能补 | 派发 `backfill` | `名言点评回填：补上 N 条，仍空 M 条（共 K 条）`，N > 0 |
| 点评不覆盖已有的 | 重复跑 `backfill` | 第二次 `N 条全部已有评论，无需回填`（`_carry` 只填空字段） |
| 逐字引用不改字 | 抽查几条对照原日记 | 一字未改 |

### 5.6 飞书三时段推送

| 要验的点 | 怎么验 | 合格证据 |
| --- | --- | --- |
| 早 7 点收到 | 派发 `morning`，看飞书 | **纯文本**（不是卡片），含今天任务 + 一言 |
| 中午/晚上同上 | 派发 `noon` / `night` | 问好鼓励 / 一天总结 |
| 人格对 | 读消息 | 傲娇雌小鬼：先损一句再落到实事；**不是客服腔** |
| **不含课表** | 读消息 | 没有任何上课时间/教室（用户原话「课表的提醒我不用你」） |
| 不编造 | 对照 `data/diary/plans.json` | 消息里的任务都能找到出处 |
| AI 挂了会降级 | 临时清空 `AI_API_KEY` 跑一次 | 日志 `文案来源：降级`，仍收到含事实和一言的消息 |
| 一天最多一条 | 看 `data/runtime.json` 的 `pushed` | 值是 `YYYY-MM-DD`，同一天重复 tick 不再发 |
| 失败报红 | 看采集失败时的卡片 | 红色 ❌（用的是 `job.status`，不是 `steps.execute.outcome`） |

### 5.7 公告与竞赛

| 要验的点 | 怎么验 | 合格证据 |
| --- | --- | --- |
| 源全成功 | 派发 `notice` | `抓取站点：N，成功：N` |
| 截止日不幻觉 | `selftest` | ✅ 截止日有正文依据才采纳 / 阶段结束日不算报名截止 |
| 竞赛日历可溯源 | 打开 [竞赛日历.md](竞赛日历.md) | 每项标注来源链接，能点到官网 |
| 269 项按开始时间排序 | 同上 | 附录逐项标注开始时间 |

**⚠️ 不要相信 AI 给的截止日**，`deadline_supported()` 是唯一防线。
实测收紧提示词后重抽**仍返回同一个错值**，所以硬约束必须在代码里。
`ai_detail()` 只在 deadline 为空时写值，**存量错值不会自动纠正**，要手工清 `data/notice/notices.json`。

### 5.8 隐私（这是公开仓库）

| 要验的点 | 怎么验 | 合格证据 |
| --- | --- | --- |
| 课程名不含同学学号 | `selftest` 全仓库扫描 | ✅ 无学号形状残留 |
| 任务标题不含教师姓名 | `selftest` | ✅ 教师姓名前缀被剥掉 / 合法数字标题不被误伤 |
| 公开链接不含个人参数 | `selftest` | ✅ 公开链接去掉每生一份的参数 |
| 日志不含 cookie/token | `selftest` | ✅ 异常摘要脱敏 cookie |
| 飞书侧也脱敏 | 看 `notify_feishu.js` | `redact()` 套在 `NOTIFY_OUTPUT` 和命令行文案上 |
| 网页不暴露私有路径 | 看 `docs/data/plans.json` | **没有 `source_file` 字段** |
| 私人目录不被采集 | 配 `DIARY_EXCLUDE` 后派发 `diary` | 日志 `清理黑名单生效前入库的私人条目：…` |
| 抓包文件不在仓库 | `git ls-files \| grep -i har` | **空**；`.gitignore` 拒绝一切 `*.har`，无例外 |

**⚠️ 写修复文档时不要把真实学号/姓名当例子抄进去 —— 那等于二次泄露，本项目已犯过三次。**
示例一律用 `<学号>` / `<姓名>` 占位符，selftest 的测试数据用代码合成。

---

## 6. 常见改动 cookbook

### 6.1 加一个公告源

**只改 `data/sources.json`**，不改代码：

```jsonc
{
  "name": "某某处通知公告",
  "url": "https://xxx.huuc.edu.cn/index/tzgg.htm",
  "enabled": true,
  "gate": ["竞赛", "报名", "项目"],     // 命中才进候选
  "block": ["放假", "调课", "党建"]      // 命中直接丢
}
```

**先确认列表页是静态 HTML**（不是 JS 渲染）：`curl` 一下看有没有 `<a href>`。
然后派发 `notice`，看 `抓取站点：N，成功：N` 里 N 有没有 +1。

日期抽取有个坑：日期常在 `<a>` **外面**（前一个兄弟节点，也可能在后一个），
`_find_date()` 会回看/前看 200 字符，并取**最靠近链接的那个匹配**（`m.end()` 最大），
不是第一个匹配 —— 否则会串到上一条公告的日期。

### 6.2 改人格 / 说话风格

**只改 `prompts.py` 的 `PERSONA`。** 名言点评和三时段推送共用它。

硬边界必须还在：不许人格羞辱、不许拿外貌/家庭/智力/成绩开刀、不许色情、不许鼓励自伤。
**调侃对象是「拖延这件事」，不是用户这个人。** 改完先想清楚边界还在不在。

### 6.3 改采集频率 / 推送时刻

```python
# scripts/main.py
TICK_INTERVALS = {"study": 60, "diary": 120, "notice": 360}   # 分钟
PUSH_HOURS = {"morning": 7, "noon": 12, "night": 22}          # 北京时间
PUSH_CATCHUP_HOURS = 3                                        # 错过后补发窗口
TICK_SLACK_MIN = 5                                            # 给 cron 延迟留的余量
```

**改完必须同步改文档里的频率描述**：AGENT.md §0 表格、`scripts/readme_template.md` 顶部、
[工作原理.md](工作原理.md) §0、[数据结构.md](数据结构.md) 的 runtime.json 说明。

**硬下限**：`diary.writeback_window` 的宽度必须 ≥ cron 间隔，否则写回可能整天错过。

### 6.4 换 AI 中转站或模型

1. **先 `GET {base}/v1/models` 看目录**（实测 200、31 个模型）
2. `AI_MODEL` **照抄返回里的名字**，别凭记忆写
3. 改 Variables（不用改代码）：`AI_BASE_URL` / `AI_MODEL`

**⚠️ 实测坑：模型名必须是 `deepseek-v3.2`（点号），不是 `deepseek-v3-2`。**
写错时每次调用都 403「免费API仅可使用免费模型目录中的模型」，
极易误判成 key 失效或海外 IP 被封。**排查顺序：先核模型名，再查 key，最后才怀疑 IP。**

**`max_tokens` 必须 ≥2000**：给 1000 时 `content` 返回空 → `_extract_json` 得 None → 静默跳过。

换 key 或改提示词后公告要重灌：清空 `data/notice/notices.json`，
临时调大 `AI_MAX_CANDIDATES` 覆盖存量，跑一次 `notice`，再改回 30。

### 6.5 加一个会公开渲染的字段

**先问「这个值能不能定位到人」。** 能的话：

1. 清洗函数放 `utils.py`，在**采集层**调用（不要手改生成物）
2. 用**结构规则**，不用黑名单 —— 黑名单要把真实值写进仓库，那本身就是二次泄露
3. 加 selftest 断言，测试数据用代码合成
4. 同步改 [数据结构.md](数据结构.md) 和 `docs/js/app.js`
5. 往 `docs/data/` 拷的时候记得裁掉私有字段（参考 plans/quotes 剥 `source_file` 的写法）

### 6.6 改课表几何

`ROW_H` / `SLOTS` / `CLASS_MINUTES` 改了之后：

- Python `week_svg.time_y()` 和 JS `app.js` 的 `nowLineY()` **必须同步改**
- `schedule_geo.json` 的导出字段要跟着加
- 重跑 selftest（有 6 条断言守几何）

**关键取舍别改**：每节课 45 分钟正好占满一行，**课间线停在两行交界不拉伸**。
否则大课间（08:50→10:05 有 75 分钟）会让线看着像还在上课。

### 6.7 重置调度状态

**删掉 `data/runtime.json` 整个文件。** 下一轮 tick 会全跑一遍、当天推送重新计。

**不要手工塞时间戳** —— 把 `pushed` 改成今天会导致当天不再推送。
只想重置写回就删 `writeback_date` 这一个键。

---

## 7. 绝对不能碰的东西

| 禁止 | 为什么 |
| --- | --- |
| **手改生成物** | README.md / docs/公告.md / docs/潜在计划.md / docs/已剔除任务.md / data/study/* / data/notice/* / data/diary/* / data/schedule/* / **data/runtime.json** / **data/diary/plans_archive.json** / docs/data/* / docs/week.svg —— 改了会被下次运行覆盖。改模板 |
| **force push** | 提交步骤是 `git pull --rebase` + 普通 push。推不上去就让它失败、飞书报红、人来处理。**别"优化"成 `--force-with-lease`** |
| **修改两个参考仓库** | `jiuyuan233/wool-gathering`（结构模板）、`jiuyuan233/Lottery-BiliBiliToolB-Web233`（推送模板）—— 只读参考 |
| **往 render_readme.py / week_svg.py 塞采集逻辑** | 破坏「finalize 零网络零 token」的结构保证 |
| **提交任何 `*.har`** | `.gitignore` 拒绝一切 HAR，**没有任何例外**。脱敏版也不行（实测班级 ID 前缀全是明文） |
| **把真实学号/姓名写进文档当例子** | 二次泄露，已犯过三次。用 `<学号>` 占位符 |
| **绕过 `utils.log()` 直接 print** | 公开日志会带出完整 URL 和 token |
| **用 `traceback.format_exc()`** | 完整栈会把局部变量里的 cookie 带进公开日志。用 `exc_brief(e)` |
| **往 workflow 加 cron** | selftest 断言「只有一条 cron」。频率改 Python 常量 |
| **把「超一周即剔除」规则加回来** | 用户明确废除了这条 |
| **把 `unknown` 截止并进「更远」** | 那会让逾期作业看起来还很宽裕，是用户报过的核心 bug |
| **把 SVG 时间线改回红色** | 会和网页实时线叠在一起，用户看到 20 分钟前的位置还以为是真的 |
| **给推送塞课表** | 用户原话「课表的提醒我不用你」 |
| **把凭据写进任何文件** | 只走 GitHub Secrets + 环境变量。开发期在聊天里贴过的一律视为已泄露，必须轮换 |

**唯一允许手改的数据文件**：`data/sources.json`。
另外 `data/diary/plans.json` 的 `state` 字段允许人工改成 `done` / `cancelled`，
合并层会保护这个人工标记不被 AI 覆盖（**人工否决优先于一切**）。

---

## 8. 踩坑速查表

| 症状 | 根因 | 动作 |
| --- | --- | --- |
| 派发多个模式，有的 CANCELLED | 同 group 只留一个 pending | 串行派发 |
| 日志显示 `tick` 但我选了 study | mode 输入没生效 | 检查表单下拉 |
| tick 跑了但某采集器没跑 | 间隔没到（**正常**） | 看 `[tick] 本轮采集 […]`；要强制就单独派发那个模式 |
| `课程总数：0` | 登录态失效或页面改版 | 派发 `studydiag` 看形状；**代码已拒绝把 0 门课当成功** |
| 所有课 `作业列表被拒` | enc 令牌问题 | enc 是一次性的，每门课都要重做 step 3 |
| `参数传递不正确！` | 作业/考试参数大小写混用 | 作业 camelCase `courseId/classId`，考试小写 `courseid/clazzid` |
| `无权限的操作！` | 少了 `ut=s` | 补上 |
| 14 门课名字全错 | 取了 middle 页的 `<h2>` | 那是全站横幅；正确来源是列表页 `course-name` span 的 `title` |
| 真待交作业漏了 | 状态词表缺 `未交` | dump 原文再补词表，**不要猜** |
| 每次抓取全是「新任务」 | `enc` 进了任务 ID | ID 绝不含 enc |
| 同一门课出现两行 | 只按 course_id 归组 | 课程身份 = `(course_id, class_id)` |
| Gitee 读文件 200 但内容空 | **不存在/已删除的文件返回 200 + `{}`，不是 404** | 看 body 里有没有 `sha` |
| Gitee 写入 400/409 | 更新没带当前 sha | 新建 POST，更新 PUT + sha；被拒会自动重读重试一次 |
| 日记改了但没被处理 | sha 没变 / AI 失败不推进 sha / 命中黑名单 / 超过 `max_files_per_run` | 见 [常见问题 C3](常见问题.md) |
| 计划条数暴涨 | 助手区块被当成用户新写的再抽 | 确认 `strip_assistant_block()` 在抽取前调用 |
| 公告日期全是 null | 日期在 `<a>` 外面 | `_find_date()` 回看/前看 200 字符 |
| 日期串到上一条 | 取了第一个匹配 | 取 `m.end()` 最大的那个 |
| 飞书卡片指标全是「—」 | 日志文案改了但正则没改 | 同步 `TASK_TEMPLATES` |
| 收到绿卡但数据没上去 | 用了 `steps.execute.outcome` | 必须用 `job.status`（push 在后面的步骤） |
| AI 每次 403 | 模型名写错 | 先 `GET /v1/models` 核目录 |
| AI 返回空 content | `max_tokens` 太小 | ≥2000 |
| 本地 `ModuleNotFoundError: utils` | embeddable Python 的 isolated 模式 | 见 §2.2 |
| 本地 `UnicodeEncodeError: gbk` | Windows 控制台编码 | 见 §2.2 |

**完整故障排查见 [常见问题.md](常见问题.md)（A–I 九大类）。**

---

## 9. 交接状态快照

**⚠️ 快照有保质期。** 下面是 2026-11-14 的状态，接手时**先自己验一遍再相信它**。
带具体次数的实测证据只写在 [不足与风险.md](不足与风险.md) 里并注明时间。

### 已在本地验证（可复现）

```bash
ruff check scripts --select E9,F63,F7,F82,F821,F401   # All checks passed
python scripts/main.py selftest                      # 164 项断言，失败 0 项
python scripts/main.py finalize                      # 成功，README + SVG + docs/data 全部生成
```

`finalize` 本地实测输出：课表 186 条上课记录、24 个课程条目、第 10 周、本周 17 节课；
任务清单 4 条可做 / 14 门课（今天 0 / 7天内 3 / 更远 0 / **截止未知 1**）；已剔除逾期 1 / 非待完成 33。

### 已在 Actions 上真实跑通过（历史证据，需复核）

`selftest`、`probe`、`finalize`（含 git 写回）、`notice`、`study`、`diary`、`schedule`、
`morning`、`noon`、`night`。海外 IP 可达性实测通过（Actions 出口在美国 Azure，
学习通/校网/飞书全 200，Gitee 与 AI 中转 401 = 可达只是未带凭据）。

### 尚未真实验证 —— 接手后优先补

| 项 | 为什么还没验 | 怎么补 |
| --- | --- | --- |
| **日记写回真实执行** | selftest 用 FakeGitee，证明不了真实 API 权限/路径 | 按 §5.3 的六步走，**去 Gitee 网页亲眼看** |
| **网页实时时间线** | 需要在浏览器里打开页面 | 按 §5.4，开 Pages 或用本地 HTTP 服务器 |
| **待考考试行解析** | 样本里两场考试都是「已完成」 | 第一次遇到待考时核对 `onclick` 函数名和倒计时结构 |
| **详情页年份推算** | 学期交界/暑假前后可能错一年 | 12 月底和 1 月初人工核对一次 |
| **tick 的长期稳定性** | 单 cron 设计是新改的 | 连续观察几天 `data/runtime.json` 的 `last_ok`，确认三个采集器都在按间隔前进 |

### 已知未决（等用户决定，别擅自动）

见 [不足与风险.md §7](不足与风险.md)。核心几条：

- **git 历史里的隐私**：学号、私人日记计划仍在历史提交里。彻底抹除要改写历史 + force push，
  **这是需要用户明确授权的动作**
- **`study.public_links`**：`"course"`（默认，链接只到课程页，安全）vs `"full"`（一键直达作业，但暴露 cpi/enc）。
  **这是产品取舍不是 bug**
- **GitHub Pages 开不开**：开了网页看板才能用，但 `docs/data/*.json` 会公开可访问

---

## 10. 每次收工前的自检

- [ ] `ruff check scripts --select E9,F63,F7,F82,F821,F401` → All checks passed
- [ ] `python scripts/main.py selftest` → 失败 0 项
- [ ] 改了生成物的**措辞** → 改的是模板，不是生成物
- [ ] 改了解析器 → selftest 断言和 `SAMPLE_*` 都跟着改了
- [ ] 改了日志文案 → `notify_feishu.js` 的 `TASK_TEMPLATES` 跟着改了
- [ ] 改了脱敏正则 → Python 和 JS **两边**都改了
- [ ] 改了频率 → 四处文档描述都同步了
- [ ] 新增了公开字段 → 问过「能不能定位到人」，清洗在采集层
- [ ] `git status` 看一遍要提交的文件 → 没有凭据、没有 HAR、没有临时探测目录
- [ ] 提交信息说清楚**为什么**改，不只是改了什么
- [ ] 推送用普通 push，**没有 --force**
- [ ] Actions 派发**串行**，并且**读了日志**确认，不是只看绿勾
- [ ] 开发期产生的临时文件（cookie、密码、探测 HTML、抓包）**已删除**
- [ ] 文档里引用的函数名真实存在（selftest 会扫，但新增文档要自己注意）
- [ ] 在 [CHANGELOG.md](../CHANGELOG.md) 记了这次改动，在 [不足与风险.md](不足与风险.md) 更新了对应条目的状态

**最后一条最重要：如果你没能真实验证某个功能，就在文档里明说「未验证」，
不要写成「已完成」。** 这个项目的审计文档之所以有价值，就是因为它诚实。