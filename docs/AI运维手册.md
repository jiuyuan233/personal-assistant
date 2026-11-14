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

**✅ 已在真实仓库上验证过一次（2026-11-14 00:08:14，审计 §4.6 已从「未验」改判「已验」）**：

| 项 | 实测值 |
| --- | --- |
| 触发 | 北京时间 00:08:14 的 `diary` 运行，落在 `00:00-06:00` 窗口内 |
| `data/diary/writeback.json` | `last_date=2026-11-14`、`written=20`、`done=[]` |
| 去 Gitee GET 目标文件 | **HTTP 200**，`sha=647a1e8bdf4f096589c2416e662db8423795af24`，1340 字节 |
| 文件里实际有什么 | `assistant:plans` 块内 **20 行 `- [ ]` + 20 个 `<!-- pid:… -->`**，与 `written:20` 对得上 |

**但只验证了 PUT 更新这一条分支。** 下面两条仍然只有离线断言，**它们自然触发时你要照同样办法去核对**：

- **POST 新建**：当天日记还不存在时走创建路径（Gitee 的坑是「文件不存在返回 **200 + `{}`**」而不是 404，
  所以存在性靠**有没有 `sha` 字段**判断，不是靠状态码）。
- **sha 冲突重试**：用户同时在 Obsidian 里编辑，PUT 会因 sha 过期失败，需要重新 GET 再 PUT。

**⚠️ 为什么不能只看 selftest 绿**：selftest 用的是 FakeGitee，证明不了真实 API 的权限和路径对不对。
**也不能只看 `writeback.json`** —— 那是代码写完自己记的，只能证明「它认为自己成功了」。

**真实验证步骤**（每次写路径有改动都重跑一遍）：
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
| 网页红线实时移动 | 浏览器打开 **`https://jiuyuan233.github.io/personal-assistant/`**，等 30 秒 | 红线移动；selftest ✅ 网页端仍有实时时间线 |
| 几何两边一致 | 改 `ROW_H`/`SLOTS` 后 | Python `time_y()` 和 JS `nowLineY()` 必须同步改 |

**⚠️ 在 README 里看 SVG 是看不到实时线的** —— GitHub 把它当静态图片渲染，不执行 JS。
实时线只在网页看板里存在。**别跟用户说「README 里的线会动」。**

**验红线时的两个坑（本轮都踩过，各浪费一次查询）**：
1. **红线是 DOM 覆盖层，不是 SVG 元素** —— `docs/index.html` 里的 `<div class="now-line" id="nowline">`。
   去 SVG 里搜 `stroke`/`#FA5252` 会得到空结果，然后误判「线没画出来」。
2. **红色在 `border-top` 上，不在 `background` 上** —— 读 `getComputedStyle(el).background`
   得到 `rgba(0,0,0,0)`，看着像透明的。要读 `borderTopColor`（实测 `rgb(250, 82, 82)`），
   圆点读 `::before` 的 background，时刻标签读 `span` 的 background。

**已验到的数值证据（2026-11-14 09:2x）**：`top 27.4783% / left 72.6733% / width 13.0693%`，
按 SVG 自然尺寸 1010×690 反算 = y 189.6 / x 734，与 `nowLineY(573, geo)` 完全一致；
9 个采样点单调递增（7 个不同 y 值），跨周 `col>6` 时正确 `hidden`。
**没验到的是像素外观**：Qoder 内置浏览器 `viewport=0×0`，截图工具报
`NATIVE_BROWSER_VIEWPORT_UNAVAILABLE`。**别把「数值对」写成「看着对」。**

**教师列恒为空是预期行为**：`assets/WakeUp导入课表.ics` 已脱敏剥离教师名。
**不要去"修"它**，换回未脱敏原件即生效。

### 5.5 名言 + AI 点评

| 要验的点 | 怎么验 | 合格证据 |
| --- | --- | --- |
| 新名言带点评 | 派发 `diary`，看 README | `> 💬 …` |
| 存量名言能补 | 派发 `backfill` | `名言点评回填：补上 N 条，仍空 M 条（共 K 条）`，N > 0 |
| 点评不覆盖已有的 | 重复跑 `backfill` | 第二次 `N 条全部已有评论，无需回填`（`_carry` 只填空字段） |
| 逐字引用不改字 | 抽查几条对照原日记 | 一字未改 |

**runner 实测证据（2026-11-14 09:55，run `43543336759`）**：
`名言点评回填：补上 6 条，仍空 0 条（共 13 条）` → 提交 `6a12eaa..eec43bb` → `[feishu] ✅ 推送成功`。
拉回后本地复核 `data/diary/quotes.json`：**13/13 有 comment**，例
「哼，光说不想被牵着走可不够，你先把那几个天天刷的推送关掉再说。」——人格对。
**`backfill` 是一次性模式**，补完就没事做了；再跑会打 `全部已有评论，无需回填`。
**别把它加进 cron**，那会每 20 分钟烧一次 token。

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
| **错过整点仍补发** | 看 `PUSH_CATCHUP_HOURS` + selftest | morning/noon 各 5 小时、night 2 小时；11 条断言守着边界 |
| 失败报红 | 看采集失败时的卡片 | 红色 ❌（用的是 `job.status`，不是 `steps.execute.outcome`） |

**补发窗口为什么按时段给而不是全局一个数**：实测 GitHub 两次 schedule 触发最大间隔 **280 分钟**，
全局 3 小时（180 分钟）会让整个时段落在间隔里被静默丢掉。
morning 5h 正好补到 noon 时段、noon 5h 到 17:00 仍是下午、night 2h 不吵醒用户。
**`target` 取「今天的整点」，所以 night 实际只覆盖 22:00–24:00，跨零点不补** —— 这是有意的，
凌晨给用户发昨晚的总结比不发更糟。改这几个数前先读 §9 的投递率实测。

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
| **私人路径守卫每轮都跑** | 看门禁步骤的 env | `DIARY_EXCLUDE` 在**质量门禁与执行两处**都注入；日志是 ✅ 而不是 ⏭️ 跳过 |
| **守卫命中时不泄露值** | 故意造一次失败 | 学号守卫输出 `学号: DDDDDDDDDD`、私人路径守卫输出 `私人路径#N`，**都不含真实值** |
| 抓包文件不在仓库 | `git ls-files \| grep -i har` | **空**；`.gitignore` 拒绝一切 `*.har`，无例外 |

**⚠️ 写修复文档时不要把真实学号/姓名当例子抄进去 —— 那等于二次泄露，本项目已犯过三次。**
示例一律用 `<学号>` / `<姓名>` 占位符，selftest 的测试数据用代码合成。

**⚠️ 第四个泄露面：守卫/诊断的失败详情本身（2026-11-14 09:58 修）。**
`utils.log()` 只脱敏**凭据形状**（cookie/token），不认「学号」「私人目录名」这类业务隐私。
两条隐私守卫原来把 `m.group(0)` 和黑名单原值拼进失败详情 —— **仓库公开 ⇒ Actions 日志公开 ⇒
一旦命中就把受保护的值广播出去**。现在学号掩成 `D`、私人路径只报命中项序号。
**规则：凡是把匹配结果/异常内容拼进日志的地方，都要单独问「这行进了公开日志会不会泄露」。**
验证方法是**制造一次失败去看输出**，不是读代码觉得没问题（用假黑名单测，别写真实私人名字）。

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
PUSH_CATCHUP_HOURS = {"morning": 5, "noon": 5, "night": 2}    # 错过后补发窗口（小时，按时段）
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

### 6.8 创建 / 轮换 GitHub Secret（不用开网页）

**为什么需要这一节**：用户明确说过「这些密钥都你给我填，别麻烦我」。
网页点点点做不到自动化，但 GitHub 有 API。**难点是 Secret 值必须先用仓库公钥加密**，
明文传过去会被拒（`422`）。加密算法是 **libsodium sealed box**（X25519 + XSalsa20-Poly1305）。

**四步**：

```bash
# 1) 取仓库公钥（key_id 必须一起回传，否则 GitHub 不知道用哪把私钥解）
curl -sS -H "Authorization: Bearer $GH_PAT" \
  https://api.github.com/repos/jiuyuan233/personal-assistant/actions/secrets/public-key
# → {"key_id":"0123456789abcdef","key":"<base64 的 32 字节 X25519 公钥>"}

# 2) 用 Python + PyNaCl 加密（值只从环境变量读，绝不写进文件）
python -c "import os,base64,json,nacl.bindings as b; \
pk=json.load(open('pk.json')); \
print(base64.b64encode(b.crypto_box_seal(os.environ['SECRET_VALUE'].encode(), \
base64.b64decode(pk['key']))).decode())"

# 3) PUT 上去（名字大写，值就是上一步的 base64）
curl -sS -X PUT -H "Authorization: Bearer $GH_PAT" -H "Content-Type: application/json" \
  -d '{"encrypted_value":"<base64>","key_id":"<key_id>"}' \
  https://api.github.com/repos/jiuyuan233/personal-assistant/actions/secrets/DIARY_EXCLUDE
# → 201 = 新建成功，204 = 更新成功。**这两个状态码本身就是加密正确的证明。**

# 4) 复核：列出所有 Secret 的**名字**（API 永远不返回值）
curl -sS -H "Authorization: Bearer $GH_PAT" \
  https://api.github.com/repos/jiuyuan233/personal-assistant/actions/secrets
```

**三个实测坑**：

| 坑 | 现象 | 解法 |
| --- | --- | --- |
| `crypto_box_seal` 传了 `PublicKey` 对象 | `TypeError: public key must be bytes` | 传 **`base64.b64decode(pk["key"])` 的原始 bytes** |
| 装 PyNaCl 1.6.2 | 依赖 `cffi>=2.0.0` 拉不到，安装失败 | **退到 1.5.0**（只需 `cffi>=1.4.1`），另装 cffi + pycparser |
| wheel 文件名不规范 | pip 报 `from versions: none` | pip **只认规范名** `pynacl-1.5.0-cp36-abi3-win_amd64.whl`，`pynacl.whl` 不行 |

**当前仓库的 6 个 Secret**（名字可以写进仓库，**值永远不行**）：

| 名字 | 用途 | 缺了会怎样 |
| --- | --- | --- |
| `CX_USERNAME` / `CX_PASSWORD` | 学习通登录 | study 采集失败，清单不更新 |
| `GITEE_TOKEN` | 读日记 + **写回** | diary 采集失败；**只读 token 会让写回 403** |
| `AI_API_KEY` | 抽取 / 点评 / 人格推送 | 抛 `MissingCredential`，推送降级但不崩（§4.5） |
| `FS_WEBHOOK` | 飞书机器人 | 推送静默跳过 |
| `DIARY_EXCLUDE` | **私人日记目录黑名单**（逗号分隔路径前缀） | **私人计划会被公开采集** —— 这是隐私开关，不是可选优化 |

**⚠️ `DIARY_EXCLUDE` 的值为什么不能进仓库**：路径名本身就暴露了用户把哪些主题标成私人。
所以仓库里只出现这个名字，selftest 在运行时读它做反向守卫（见 §5.8）。

### 6.9 `github.com:443` 不通时怎么推送

**实测现象**：`api.github.com` 0.4 秒返回 200，`github.com` 20 秒超时；
`git fetch` 有时成功、`git push` 有时失败。**这是间歇性的，不是配置错误** ——
别去改 git config、别设代理、别换成什么临时镜像地址。

**先重试一次普通 push**（网络可能刚好通了）。真不行再走 **Git Data API**，四步全是普通快进：

| 步 | 调用 | 关键点 |
| --- | --- | --- |
| 1 | `POST /repos/{o}/{r}/git/blobs` | `{content: <base64>, encoding: "base64"}` |
| 2 | `POST /repos/{o}/{r}/git/trees` | `{base_tree: <父提交 tree sha>, tree: [...]}`；**删文件用 `sha: null`** |
| 3 | `POST /repos/{o}/{r}/git/commits` | `{message, tree, parents, author, committer}` |
| 4 | `PATCH /repos/{o}/{r}/git/refs/heads/main` | `{sha, force: false}` |

**⚠️ 两个会让内容静默变质的坑（都实测踩过）**：

1. **`core.autocrlf=true` 时不能读工作区文件当 blob。**
   本机工作区是 **CRLF**、仓库对象是 **LF**（同一个文件：blob 里 0 个 `\r`，工作区 478 个）。
   用 `fs.readFileSync` 上传 → 全仓库行尾被改写。
   **必须用 `git cat-file blob <commit>:<path>` 取字节。**
2. **commit message 的尾换行不要削。** `git show --format=%B` 自带一个尾 `\n`，
   commit 对象里存的也是带 `\n` 的版本。`replace(/\n$/,'')` 会让 sha 对不上。

**怎么发现自己踩坑了**：比对**本地 HEAD sha** 与 **API 返回的 commit sha**。
不相等 = 内容有差异。**「API 返回 201」不等于「推上去的东西和本地一样」，sha 一致才是。**

### 6.10 抹掉 git 历史里的隐私（**需要用户明确授权，不得自行发起**）

**这是本项目做过两次的高危操作**（第一次抹 HAR，第二次抹学号 + 私人计划）。
[AGENT.md](../AGENT.md) 铁律 3 禁止 force push，**只有用户点名授权时才是例外**。
**下一个 AI 不得援引历史先例自行 force push。**

**授权后的标准流程**（2026-11-14 实际执行并验证过）：

1. **仓库外全量备份**：`git bundle create <仓库外路径>/backup.bundle --all`，
   然后 `git bundle verify` 必须报 **「records a complete history」**。没有这一步不许往下走。
2. **先量再改**：遍历 `git rev-list --all` 的每个提交的每个 blob，
   用隐私正则 + 私人目录名扫一遍，**列出受影响路径**。本次量到 **98 提交 / 626 blob / 15 个路径**。
3. **在镜像上试，不碰主仓库**：`git clone --mirror <主仓库> <临时目录>`，在镜像上重写。
4. **用 `git-filter-repo`，不是 `reset --hard`**（后者会丢掉全部开发历史）：
   ```bash
   python git_filter_repo.py --force --blob-callback <回调文件.py>
   ```
   回调是**函数体**（`blob` 在作用域里），改 `blob.data` 即可；也可以传文件路径。
   本次三条清洗规则：JSON 按 `source_file` 结构化删条目 → 文本按私人原文**整行删除** →
   私人目录叶子名替换成占位符。
5. **复扫 + 比对，两个都要过**：
   - 复扫重写后的历史 → **命中必须为 0**；
   - `diff <(git ls-tree -r 旧HEAD) <(git ls-tree -r 新HEAD)` → **当前文件必须逐字节不变**；
   - `diff` 两边的 `git log --format=%s` → **提交信息必须一条不差**。
6. **推送**：`git push --force <url> refs/heads/main:refs/heads/main`，再用 API 复核 remote head。
7. **同步本地**：`git fetch <镜像> refs/heads/main` 后 `git update-ref refs/heads/main <新sha>`。
   **树相同就不必 `reset --hard`**，`update-ref` 不动工作区，更安全。

**⚠️ 清洗正则宁可漏一点，也不能吞掉分隔符。** 本次第一版写成
`(课程名)[\d,，、()（）\s]+`，其中 `\s` **跨行**、`、` 是正常顿号，结果：
**把 ICS 里 `SUMMARY` 与 `LOCATION` 之间的换行吃掉了**（1680 行 → 1670 行，课表源文件结构被破坏），
还吃掉 README 表格空格、文档顿号、代码注释里的逗号。
改成 **`\d[\d,，()（）]*`（必须以数字开头、不含空白与顿号）** 后，HEAD 树逐字节不变。
**漏了可以复扫补，吞了是静默损坏 —— 所以第 5 步的「树必须不变」是硬门禁，不是可选项。**

**残留**：GitHub 服务端可能仍缓存旧对象（fork / PR ref / Pages 构建缓存）。
本仓库无 fork、无 PR，**开 Pages 必须在抹历史之后**，否则旧内容会通过构建缓存再暴露一次。

---

## 7. 绝对不能碰的东西

| 禁止 | 为什么 |
| --- | --- |
| **手改生成物** | README.md / docs/公告.md / docs/潜在计划.md / docs/已剔除任务.md / data/study/* / data/notice/* / data/diary/* / data/schedule/* / **data/runtime.json** / **data/diary/plans_archive.json** / docs/data/* / docs/week.svg —— 改了会被下次运行覆盖。改模板 |
| **force push** | 提交步骤是 `git pull --rebase` + 普通 push。推不上去就让它失败、飞书报红、人来处理。**别"优化"成 `--force-with-lease`**。**唯一例外**：用户点名授权抹历史（§6.10），已用过两次，**不得援引先例自行发起** |
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
# ⚠️ 必须带上 DIARY_EXCLUDE，否则私人路径守卫会跳过，等于没验
DIARY_EXCLUDE='<Secret 里的值>' python scripts/main.py selftest   # 174 项断言，失败 0 项
python scripts/main.py finalize                      # 成功，README + SVG + docs/data 全部生成
```

**断言条数随环境变**：带 `DIARY_EXCLUDE` 是 **174**，不带是 **173**（那条守卫打 ⏭️ 跳过后不计入）。
**质量门禁与执行两步都注入了这个 Secret，所以线上每轮都是 174。**
本地不带变量跑绿了，不代表隐私守卫过了。
条数演进：107 → 166 → 173 → **174**（+5 推送补发窗口、+2 链接守卫、+1 门禁注入黑名单）。
**改完断言必须真跑一遍**：本轮有一次新守卫用了 `re.compile`，而 `do_selftest()` 里只有
函数内的 `import re as _re` —— **`ruff --select F821` 没报**，跑起来才 `NameError`。静态门禁不是兜底。

`finalize` 本地实测输出（2026-11-14 09:48）：课表 186 条上课记录、24 个课程条目、第 10 周、本周 17 节课；
任务清单 **3 条可做 / 14 门课（今天 0 / 7天内 3 / 更远 0 / 截止未知 0）**；已剔除逾期 2 / 非待完成 33；
独立文档 公告 8 / 潜在计划 35（黑名单清洗前 58）/ 已剔除 35。
**「截止未知」从 1 变 0**：详情页时间窗补齐后那一档清空了，这正是用户报的 bug 被修掉的可见结果。

### 已在 Actions 上真实跑通过（历史证据，需复核）

`selftest`、`probe`、`finalize`（含 git 写回）、`notice`、`study`、`diary`、`schedule`、
`morning`、`noon`、`night`。海外 IP 可达性实测通过（Actions 出口在美国 Azure，
学习通/校网/飞书全 200，Gitee 与 AI 中转 401 = 可达只是未带凭据）。

### 尚未真实验证 —— 接手后优先补

| 项 | 为什么还没验 | 怎么补 |
| --- | --- | --- |
| **写回的 POST 新建 / sha 冲突重试** | 11-14 00:08 那次只走了 **PUT 更新**分支 | 按 §5.3，等它们自然触发时**去 Gitee 网页亲眼看** |
| **待考考试行解析 + exam 深链能否打开** | 样本里两场考试都是「已完成」 | 第一次遇到待考时核对 `onclick` 函数名和倒计时结构，并点一次深链 |
| **详情页年份推算** | 学期交界/暑假前后可能错一年 | 12 月底和 1 月初人工核对一次 |
| **Pages 的像素级外观** | 数值全对，但 Qoder 内置浏览器 `viewport=0×0`，`take_screenshot` 报 `NATIVE_BROWSER_VIEWPORT_UNAVAILABLE` | 用真浏览器截一张图，确认红线没有遮住课程名 |

**已验过的别再列进这张表**：
- 日记写回的 PUT 分支已于 **2026-11-14 00:08:14** 真实验证
  （Gitee sha `647a1e8b…`、20 个复选框 + 20 个 pid 标签），证据在 §5.3。
- **名言点评 AI 回填已于 2026-11-14 09:55 在 runner 上验证**：run `43543336759`（head `6a12eaa`），
  日志 `名言点评回填：补上 6 条，仍空 0 条（共 13 条）`，提交 `6a12eaa..eec43bb`，
  拉回复核 `data/diary/quotes.json` **13 条全部有 comment**、人格正确。证据在 §5.5。
- **网页实时时间线已于 2026-11-14 09:2x 在真实浏览器里验证**：Pages `built`，
  6 个 `docs/data/*.json` 全 200，任务 3 / 公告 8 / 计划 20 / 名言 10 行都渲染出来了；
  红线 `border-top: 2px solid rgb(250,82,82)`，`top/left/width` 与 `schedule_geo.json`
  算出的几何**吻合到小数点后 4 位**，跨 9 个采样点单调移动，跨周正确隐藏。证据在 §5.4。
  **注意红线是 DOM 覆盖层不是 SVG 元素**，去查 `stroke` 或 `background` 会得出「线不见了」的错误结论。

**过时的「未验证」和过时的「已完成」一样会误导接手的人。**

### 定时触发：内部调度已验证，平台投递率只有 ~8%（改不掉）

**这两件事必须分开说，混在一起就会得出错误的完成度。**

| 层 | 状态 | 证据 |
| --- | --- | --- |
| **仓库内部调度**（谁该跑、推送该不该发、写回窗口） | ✅ **已验证正确** | run #81/#82 日志 + `data/runtime.json`：到点选中 study/diary/notice，07:00 早报在 09:00 补发成功，`errors {}` |
| **GitHub 把 cron 投递过来** | ⚠️ **实测 ~8%，平台行为** | 单 cron 架构上线后 ~8.5 小时只自然触发 **2 次**（应有 ~25 次），最大间隔 **280 分钟** |

**由此修掉的一个真缺陷**：原来的补发窗口是全局 3 小时（180 分钟），**比实测最大间隔 280 分钟还窄**
→ 整个推送时段会落在间隔里被静默丢掉。现在按时段给：
`PUSH_CATCHUP_HOURS = {"morning": 5, "noon": 5, "night": 2}`。

**别再做这两件事**：
- **别再「观察 24 小时就好了」**。v1.3.3 曾假设「手动派发挤掉了 schedule 的 pending」，
  本轮**证伪**：run #80（17:04:54Z）之后没有任何手动派发，8.5 小时自然观测仍只有 2 次。
- **别再加 cron 条数**。8 条 → 1 条正是为了绕开节流，加回去是倒退，且违反 AGENT.md。

要准点只有两条路，**都需要用户明确授权**：外部调度器定时 `workflow_dispatch`（要把 PAT 交给第三方），
或 self-hosted runner。**未经授权不要自己选一条做。**
另外：统计触发次数时**必须按 workflow 名 `个人助手` 过滤** —— API 里的 `dynamic` 事件是
「pages build and deployment」，不是助手运行；**归因架构要看每个 run 的 `head_sha`**，
不能看提交日期（本轮就因此一度把 8-cron 时期的 6 次触发算到新架构头上）。

### 已知未决 → 已全部决定（2026-11-14）

用户说「这是我最后一次回答你了」，所以 [不足与风险.md §7](不足与风险.md) 的四项**不再等用户**，
按「代价最小、可逆、不泄露」自行决定并把理由写进了文档：

- **§7.1 git 历史里的隐私**：✅ **已于 2026-11-14 00:58 执行完毕**。
  98 提交 / 626 blob 重写后复扫 **0 命中**，当前文件逐字节未变，remote main = `424e845`。
  流程与两个坑见 **§6.10**。**这是用户点名授权的一次性例外，下一个 AI 不得自行 force push。**
- **§7.2 深链参数**：**白名单** `_PUBLIC_KEEP`，去掉 `cpi`/`enc`/`stuenc`/`answerId`。
  `study.public_links` 这个开关保留，但默认值已是安全的那一档。
- **§7.3 私人计划**：Secret **`DIARY_EXCLUDE` 已创建**，仓库里只出现名字。
  本地数据已按它清洗（plans 63→40、quotes 15→13、state.json 去 5 个私人键）。
- **§7.4 Pages**：✅ **已开、已构建、已在浏览器里验证**（`https://jiuyuan233.github.io/personal-assistant/`，
  源 = `main` 的 `/docs`）。顺序按要求先做了 §7.1 再开，避免历史学号经构建缓存二次暴露。
  **站点根就是 `docs/`** —— 这条几何事实坑过一次：页脚写 `../README.md` 会 404，
  文件在磁盘上存在 ≠ 线上点得开。selftest 现有两条守卫（链接可达 + 不逃出 Pages 根）。

---

## 10. 每次收工前的自检

- [ ] `ruff check scripts --select E9,F63,F7,F82,F821,F401` → All checks passed
- [ ] `python scripts/main.py selftest` → 失败 0 项，**且带上 `DIARY_EXCLUDE` 跑过一次**（否则隐私守卫被跳过）
- [ ] 新增的注释 / 示例 / 修复说明里**没有真实私人目录名、学号、凭据** —— 脱敏范围不只是数据文件
- [ ] **新增的日志/失败详情里没有真实隐私值** —— 把匹配结果拼进日志前问一句「这行进了公开 Actions 日志会不会泄露」；
      验证方式是**造一次失败看输出**，不是读代码（见 §5.8）
- [ ] 改了生成物的**措辞** → 改的是模板，不是生成物
- [ ] 改了解析器 → selftest 断言和 `SAMPLE_*` 都跟着改了
- [ ] 改了日志文案 → `notify_feishu.js` 的 `TASK_TEMPLATES` 跟着改了
- [ ] 改了脱敏正则 → Python 和 JS **两边**都改了
- [ ] 改了频率 / 推送窗口 → **七处**都同步了：`AGENT.md`、`DEV_GUIDE.md`、`docs/AI提示词.md`、
      `docs/工作原理.md`、`docs/技术路径.md`、`docs/数据结构.md`、`docs/AI运维手册.md`
- [ ] 新增/改动了 md 或 `index.html` 里的**相对链接** → selftest 两条守卫会扫
      （「相对链接全部可达」+「不逃出 Pages 根」）。**代码块与行内代码里的链接不算**，
      所以审计文档可以原样引用坏链接当证据
- [ ] 改了 Pages 页脚 → 记住**站点根就是 `docs/`**，跨出仓库的文件要写 github.com 绝对地址
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