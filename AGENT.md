# AGENT.md — 维护交接说明

**给任何接手本仓库的 AI 或人类维护者。** 本文件优先级高于你的全局默认行为。
读完这一份就能上手，不需要回溯开发历史。

> ⚠️ **动手前先读 [docs/AI运维手册.md](docs/AI运维手册.md)，再读 [docs/不足与风险.md](docs/不足与风险.md)。**
> 运维手册是**可执行的操作步骤**（怎么改、怎么验、怎么派发、验收清单）；
> 不足与风险是**逐条问题的根因与修复状态**，还列了「不是缺陷、别乱改」的 10 项，
> 防止顺手重构把对的改坏。**别把「代码写了」当成「功能验证过了」。**
> 带具体次数的实测快照只写在审计文档里并注明时间，**不要把会漂移的数字抄进这两处常驻说明**。

---

## 0.这个项目是什么

河南城建学院某本科生的个人助手。GitHub Actions 定时跑，聚合三个数据源，
把**按截止时间排序的任务清单**渲染进 `README.md`，并通过**飞书机器人**推送。

三个数据源：

| 源 | 内容 | 采集方式 | 间隔（约） |
| --- | --- | --- | --- |
| 1 | Gitee 日记里的名言 + 潜在计划 | Gitee API v5（只读 .md） | 120 分钟 |
| 2 | 学习通作业/考试 | 模拟登录四步链 | 60 分钟 |
| 3 | 校园比赛/项目公告 | 公开列表页，**不登录** | 360 分钟 |

外加 WakeUp 导出的课表 `.ics`（本地解析，不联网，每轮 finalize 都重跑）。

**这三个间隔不是 cron，是 Python 里的常量** `main.TICK_INTERVALS`。
cron 只有一条 `*/20 * * * *` → 模式 `tick`，每轮由 `due_collectors()` 查
`data/runtime.json` 的时间戳决定该跑谁。改频率改常量，**不要往 workflow 里加 cron**
（selftest 有断言守着「只有一条 cron」）。为什么这么设计见 [工作原理 §0](docs/工作原理.md)。

---

## 1. 铁律（违反会直接搞坏项目）

1. **生成物禁止手改。** 下列文件全部由脚本产出，改了会被下次运行覆盖：
   - `README.md`
   - `docs/公告.md`、`docs/潜在计划.md`（**全量版**；README 里另有精简版，两边都是生成物，模板在 `scripts/*_template.md`）、`docs/已剔除任务.md`
   - `data/study/*`、`data/notice/*`、`data/diary/*`、`data/schedule/*`
   - **`data/runtime.json`**（tick 的调度状态：`last_ok` / `last_try` / `pushed` / `errors`）
   - **`data/diary/plans_archive.json`**（归档的已完成/已取消计划）
   - `docs/data/*`、`docs/week.svg`

   想改这几份文档的**措辞/结构**，改对应的模板（`readme_template.md` /
   `notice_template.md` / `plans_template.md` / `excluded_template.md`），不要改生成物。
   新增 `{{占位符}}` 必须同时在 `render_readme.py` 里提供渲染键 ——
   selftest 有一条断言专门守这个，漏了会在页面上留下字面量 `{{X}}`。

   唯一允许手工编辑的数据文件是 **`data/sources.json`**（增删公告源、改关键词、改路径）。
   另外 `data/diary/plans.json` 里的 `state` 字段允许人工改成 `done` / `cancelled`，
   合并逻辑会保护这个人工标记不被 AI 覆盖。

   **`data/runtime.json` 可以删，但不能编。** 删掉等于「所有采集器都到期、今天还没推送过」，
   下一轮 tick 会全跑一遍 —— 这是安全的重置方式。手工塞时间戳会让调度错乱，
   而且 `pushed` 被改成今天会导致当天不再推送。要重置就整个文件删掉。

2. **凭据只存在于 GitHub Secrets。** 绝不写进代码、workflow、commit、日志。
   仓库是**公开**的，Actions 日志任何人可见，所以脱敏有两道闸：
   - Python：`utils.log()` / `utils.exc_brief()` 内置 `redact()`，抹掉令牌、口令字段、**Cookie 字段**
     （`JSESSIONID` / `_uid` / `uf` / `_d` / `vc3` / `fid` / `unam` / `route` …）。
     新增日志时不要绕过 `log()` 直接 `print`；异常一律用 `exc_brief(e)`，**不要 `traceback.format_exc()`**。
   - JS：`notify_feishu.js` 的 `redact()` 用同一套正则再过一遍 `NOTIFY_OUTPUT` 和命令行文案。
     这两条路不经过 Python 的 `log()`，AI 也可能把日记隐私原样吐出来。
   **改一边的正则必须改另一边**，selftest 有断言检查 JS 侧确实套了 `redact()`。

   **第三道闸（2026-11-14 补）：`redact()` 只认凭据形状，不认业务隐私。**
   学号、私人目录名这类值它一律放过。所以**守卫 / 诊断 / 异常详情里凡是拼进日志的匹配结果，
   都要自己掩码**：学号守卫把数字掩成 `D`，私人路径守卫只报 `私人路径#N`（命中项序号），
   **绝不报命中的原文**。原来两条守卫直接打 `m.group(0)` 和黑名单原值 ——
   一旦命中就把受保护的值广播到公开日志，**修隐私的代码自己在泄露隐私**。
   验证方式：**造一次失败去看输出**，不是读代码觉得没问题（用假黑名单测，别写真实私人名字）。

   非凭据但同样敏感的开关也走 Secrets：**`DIARY_EXCLUDE`**（逗号分隔的日记目录前缀黑名单），
   仓库里只出现名字，值不入库。见 §5.2 与 [AI 运维手册](docs/AI运维手册.md)。
   **它必须同时注入「质量门禁」和「执行」两个步骤**：只注入执行步时，
   私人路径守卫在普通 tick 里每轮都走「跳过」分支，等于平时没有守卫（实测踩过）。
   selftest 有断言锁住注入次数 == 2。

3. **不要 force push。** 提交步骤用的是 `git pull --rebase` + 普通 `push`。
   推不上去就让步骤失败、飞书报红、人来处理。这是刻意的设计，别"优化"成 `--force-with-lease`。

4. **不要修改这两个参考仓库**（只读参考，不属于本项目）：
   - `jiuyuan233/wool-gathering` —— 目录结构模板
   - `jiuyuan233/Lottery-BiliBiliToolB-Web233` —— 飞书推送模板

5. **finalize 模式必须保持零网络、零 token。** 它每 20 分钟跑一次。
   任何采集逻辑都不许塞进 `render_readme.py` / `week_svg.py`。

6. **抓包永远不进仓库，脱敏版也不行。** `.gitignore` 拒绝一切 `*.har`，**没有任何例外**。
   曾经给 `assets/*-脱敏.har` 开过窄口子，结果 `clazzPersonStr` 只遮了 `_` 后的个人令牌、
   **班级 ID 前缀全是明文**，`courseid`/`clazzid`/`appId` 也被误判为无害值保留 ——
   足以定位到用户本人。两份文件已删除并**从全部 git 历史中抹除**。
   **不要为了图省事删掉 `*.har` 那条规则，也不要再加 `!` 例外。**
   接口结构用文字写进 [docs/技术路径.md](docs/技术路径.md)；需要一手证据时在本地看原件，看完即删。
   `assets/` 下两个文件的脱敏口径、审计结果、换文件流程全在 [docs/原始文件.md](docs/原始文件.md)。

   **⚠️ 同一条铁律也管「采集回来的字段」，不只管上传的文件。**
   学习通把同学学号写进了课程名（形如 `计算思维与人工智能<学号>,<学号>,…(111)`），
   采集器原样存盘、原样渲染进**公开** README —— 泄露的是**别人**的学号。
   现在由 `utils.scrub_course_name()` 在**采集层**清洗，selftest 会**全仓库**扫学号形状的残留。
   **⚠️ 写修复文档时不要把真实学号当例子抄进去 —— 那等于二次泄露，本项目已犯过一次。
   示例一律用 `<学号>` 占位符，selftest 里的测试数据用代码合成，不写数字字面量。**
   **新增任何「抓回来又要公开渲染」的字段（课程名、公告标题、教师名…），
   都要先问「这个值能不能定位到人」，并把清洗放在采集器里，不要手改生成物。**
   详见 [docs/开发习惯.md](docs/开发习惯.md) 第 8 条与 CHANGELOG v1.2.3。

   **已落地的四道清洗，都在采集层/渲染层，不在生成物里：**

   | 函数 | 清什么 | 为什么用结构规则而不是黑名单 |
   | --- | --- | --- |
   | `utils.scrub_course_name()` | 课程名里嵌的同学学号、教学班编号 | 黑名单要写真实学号进仓库 = 二次泄露 |
   | `utils.scrub_task_title()` | 任务标题开头的教师姓名（`姓名2024-2025…` 形状） | 同上；且必须要求「学年-学年」形状，否则会把合法标题 `作业20261022` 削成 `1022` |
   | `utils.public_url()` | 深链里的 `cpi` / `enc` / `answerId` 等每生一份的参数 | 白名单 `_PUBLIC_KEEP` 只留课程/作业级 ID |
   | `utils.strip_personal_fields()` | task 里的 `cpi` / `answer_id` 字段 | `data/` 是公开仓库的一部分，不是本地缓存 |

   `study.public_links` 在 `data/sources.json` 里可设 `"course"`（默认，去个人参数）
   或 `"full"`（原样深链）。**这是产品取舍不是 bug** —— 用户要在「点开就能做作业」和
   「链接不暴露身份」之间选，做成一行配置而不是写死。

---

## 2. 目录与职责

```
scripts/
  main.py             唯一入口，模式分发
  utils.py            北京时间/倒计时解析/ID/JSON/日志+脱敏
  ai_client.py        OpenAI 兼容客户端（限流+重试+JSON 抽取）
  dedup.py            合并策略：公告旧值优先，作业新值优先
  prompts.py          全部 AI 提示词 + 用户画像
  diary_collector.py  源1（含 Gitee 读写 + 名言点评）
  diary_writeback.py  源1 的写回：潜在计划 → 日记复选框，T+1 写 / T+2 复查
  study_collector.py  源2
  notice_collector.py 源3
  schedule_parser.py  课表 .ics
  render_readme.py    README + docs/公告.md + docs/潜在计划.md + docs/已剔除任务.md + docs/data
  week_svg.py         本周课表 SVG + 当前时间线 + 几何导出
  daily_push.py       早/中/晚三时段飞书文案（日记事实 + 一言 + AI 人格）
  notify_feishu.js    飞书推送（有文案发纯文本，无文案发运行状态卡片）
  readme_template.md  README 模板（{{占位符}}）
  notice_template.md  docs/公告.md 模板
  plans_template.md   docs/潜在计划.md 模板
  excluded_template.md docs/已剔除任务.md 模板
  requirements.txt    只有 requests + pycryptodome
```

详细结构见 [docs/仓库结构.md](docs/仓库结构.md)，字段定义见 [docs/数据结构.md](docs/数据结构.md)。

---

## 3. 怎么跑

```bash
pip install -r scripts/requirements.txt
pip install ruff                    # 质量门禁用，和 workflow 里一致

ruff check scripts --select E9,F63,F7,F82,F821,F401   # ← 先跑这个，语法/未定义名/未用导入
python scripts/main.py selftest   # 离线自检，不需要凭据、不需要网络 ← 改完代码必跑
python scripts/main.py probe      # 探测各站点从当前出口 IP 是否可达 + 解析冒烟
python scripts/main.py tick       # ★ cron 的唯一入口：按 runtime.json 决定跑谁，最后必渲染
python scripts/main.py finalize   # 只渲染 + 离线重算课表（不联网、不花 token）
python scripts/main.py study      # 采集学习通 + 渲染
python scripts/main.py diary      # 采集日记 + 写回 Gitee（写回只在凌晨窗口内发生）
python scripts/main.py notice     # 采集公告
python scripts/main.py schedule   # 只重算课表
python scripts/main.py backfill   # 给历史名言补 AI 点评（每批 8 条）
python scripts/main.py studydiag  # dump 学习通页面形状（数字全打码，可进公开日志）
python scripts/main.py morning    # 早 7 点飞书文案（不提交任何文件）
python scripts/main.py noon       # 中午问好
python scripts/main.py night      # 晚间总结
python scripts/main.py all        # 全部采集 + 渲染
```

**不带参数 = `tick`。** 上面两条（ruff + selftest）就是 workflow 里的「质量门禁」步骤，
在**执行步骤之前**跑，任何一条失败整个 job 就红，不会带着坏代码去动数据。
本地改完先跑这两条，能省掉一整轮 Actions 往返。

Actions 上：`Actions → 个人助手 → Run workflow → 选 mode`（默认 `tick`）。

**⚠️ 派发必须串行，一次一个。** concurrency group 是单一的 `pa-run`，`cancel-in-progress: false`，
但 GitHub 对同一个 group **只保留一个 pending run** —— 一次性派发多个模式，
后派发的会把先派发的**直接 cancel 掉**（实测两条 CANCELLED）。
等上一个 run 变 completed 再派下一个。job 有 `timeout-minutes: 25` 兜底，卡死不会占住 group 一整天。

**改任何解析器后，务必同步更新 `main.py` 里的 `do_selftest()` 断言。**
自检是唯一能在不消耗凭据、不依赖网络的情况下验证解析逻辑的手段。
`study_collector.parse_smoke()` 用内置 HTML 样本跑一遍解析链，`probe` 也会调它 ——
页面改版时这是最早的报警。

---

## 4. 学习通四步链（最容易坏的地方）

```
1  POST https://passport2.huuc.edu.cn/fanyalogin
     uname / password 都要 AES-128-CBC 加密
     key = iv = b"u2oh6Vu^HWe4_AES"，PKCS7，base64
     先 GET /login?fid=12 种 cookie
2  GET  https://mooc2-ans.huuc.edu.cn/visit/courses/list?v=1&rss=1&start=0&size=100
     → 解析 stucoursemiddle?courseid=&clazzid=&cpi=&ismooc2=1
3  GET  https://mooc1.huuc.edu.cn/visit/stucoursemiddle?...（跟随 302）
     → stu 页里现铸三个 token：#enc、#workEnc、#examEnc
4  GET  作业 /mooc2/work/list?courseId=&classId=&cpi=&ut=s&stuenc=<enc>&enc=<workEnc>
     GET  考试 /exam-ans/mooc2/exam/exam-list?courseid=&clazzid=&cpi=&ut=s&stuenc=<enc>&enc=<examEnc>
```

**六个必记的坑：**

- `ut=s` 不能省 → 否则「无权限的操作！」
- `stuenc` 和 `enc` 是**两个不同 token**，都要带
- 作业用驼峰 `courseId/classId`，**考试用小写 `courseid/clazzid`** → 混用会「参数传递不正确！」
- `enc` 每次访问都变 → **绝不能进任务 ID**，否则每次抓取都变成"新任务"
- **课程名别取 middle 页的 `<h2>`** → 那是全站横幅「在线学习诚信承诺书」，会让 14 门课名字全错。
  正确来源：课程列表页 `course-name` span 的 `title`（链接后 2500 字符窗口内），middle 页 `<title>` 兜底
- **待交状态词是 `未交`** → 词表里只有 `未提交`/`待提交` 会漏掉真待交作业（实测漏了 3 条）。
  再遇到 `unknown` 状态，先 dump 原文再补词表，不要猜

**列表页只给相对倒计时**（「剩余1小时12分钟」），没有绝对时间戳。
所以列表页解析出的 `deadline = 抓取时刻 + 剩余时长`，`deadline_exact=False`，README 里显示 `≈`。

**⚠️ 但「列表页没有倒计时」不等于「没有截止时间」。** 实测逾期作业的行**根本不含 `time` div**，
早先的代码把这类任务的 deadline 留成 `null`，于是 `drop_reason()` 判不出逾期，
**早已截止的作业被当成「更远」显示给用户** —— 这是用户报的核心 bug。
现在 `detail_time_window()` 从详情页取绝对时间：

| 页面 | 实测形状 | 解析函数 |
| --- | --- | --- |
| 作业详情 | `<p>作答时间:<em>MM-DD HH:MM</em>至<em>MM-DD HH:MM</em></p>` | `detail_work_window()` |
| 互评详情 | `<p class="hpInfo">互评时间：MM-DD HH:MM 至 MM-DD HH:MM</p>` | `detail_eval_window()` |

**页面不给年份**，所以按「距抓取时刻最近的那一年」补：候选超过 180 天就回退一年，
跨年时结束时间自动 +1 年。待完成且 deadline 未知的任务**一律去抓详情页**（受 `max_detail` 预算限制）。
只有详情页补全的时间才是 `deadline_exact=True`，且合并时旧精确值优先于新估算值。

**抓不到就诚实地说抓不到**：`deadline` 仍为 `null` 的任务进**独立的「⚠️截止未知」类**，
README 顶部出 🔴 告警，网页看板单列一桶 —— **绝不混进「更远」**，那会让逾期作业看起来还很宽裕。

**状态语义：** `待批阅` 通常表示已提交、等老师批阅，但**互评模式下「待批阅」+ 仍在倒计时 = 用户还得去互评**，
`parse_work_rows()` 会把它升级成 `pending`。改这段前先看清楚。

### 4.1 任务清单的过滤与排序规则（`render_readme.py`）

用户要求：**14 门课全都要出现在 README 里，没作业的写「无」**；按活跃度排序；只显示还能做的。

- `course_rows()`：以**课程列表**为准遍历，不是以作业为准。某门课没有可做任务就输出「无」。
  少一门课就是 bug，别用「有作业的课」当全集。
- **活跃度 = 该课作业发表数量**，多的排前面。`ordered_courses()` 是唯一排序入口，
  任务清单与「已剔除任务」文档共用，两处课程顺序不会漂。
- `actionable()`：**只剔两类** —— 非待完成（`state != "pending"`）、已逾期。只留能做的。
- **⛔ 已废除 `STALE_DAYS`（开始超过一周就剔除）。** 用户起初提过这条，
  后来明确要求去掉（v1.3.0）。**不要再加回来**，也不要用 `first_seen` 做任何过滤判定。
  现在只要没逾期、状态还是待完成，发布多久都留在清单里。
- `render_excluded()`：**按课程分组**（每课一个 `### 课名（n 项）` 小节），
  组内按截止时间近 → 远，顶部给按原因的汇总。判定共用 `drop_reason()`。
- 剔除数量要写进 `{{FILTER_NOTE}}`，让用户知道少了几条、为什么少。
- 前端 `docs/data/tasks.json` **只喂 `kept`**（过滤后的），另带 `courses` 供分组视图。

**⚠️ `start_time` 拿不到真实发布时间 —— 这是实测出来的硬限制。**
`work/task` 详情页 HTML 里只有空标签 `作答时间:`，**值由 JS 注入，静态 HTML 取不到**。
所以普通作业的 `start_time` 只能退回 `first_seen`（本助手首次发现时间）。
**一周规则废除后，这个字段只用于「开始」列的显示（标「（首次发现）」），不参与任何判定。**
**只有互评任务能拿到真实开始时间**：`eval-list` 详情页有真实窗口
`互评时间：11-06 07:01 至 11-10 23:01`（不带年份），由 `detail_eval_window()` 解析，
窗口结束即截止时间。年份缺失时按「算出来在半年以后就是去年的」回退一年。

排查时间字段失配时用 `detail_time_hints(html)` 把页面里含「时间/截止/开始」的片段 dump 出来，
**不要靠猜正则**。日志里「详情页无时间字段」那行就带这些片段。

实测（commit `a17f2d8`，run 43379475423）：`互评窗口截止：3.1微分中值定理 → 2026-11-10T23:01:00+08:00`，
`详情页取到开始时间：1/4 条`，`任务清单：3 条可做 / 14 门课`，`已剔除：逾期 1 / 超过7天 0 / 非待完成 34`。
（**这是 v1.2.x 的日志格式**；一周规则废除后不再有「超过7天」这一项。）
那条逾期互评在修之前一直挂在清单里 —— **窗口结束就不能再互评，必须剔除**。

---

## 5. 本轮新增的四块功能

### 5.1 三时段飞书推送（`daily_push.py`）

用户要的**不是**运行状态卡片，是有人格的每日陪伴消息。三个时段：

| 模式 | 触发时刻（北京） | 内容 |
| --- | --- | --- |
| `morning` | 07:00 起 | 今天该做的事，**来源于日记**，附一言 |
| `noon` | 12:00 起 | 问好 + 鼓励 |
| `night` | 22:00 起 | 一天的总结，数据来源于日记 |

**没有推送专用 cron。** 三个时刻写在 `main.PUSH_HOURS`，由 `due_pushes()` 在每轮 tick 里判断：
`now` 落在 `[目标时刻, 目标时刻 + PUSH_CATCHUP_HOURS[kind]]` 且 `runtime.pushed[kind] != 今天` 才发。

- **按日期记账，不是按小时**：`pushed[kind]` 存 `YYYY-MM-DD`，所以**一天最多一条**，
  补发窗口内 tick 跑再多次也只发第一次。
- **补发窗口按时段给**：`{"morning": 5, "noon": 5, "night": 2}`。morning 5h 正好补到 noon 时段；
  noon 5h 到 17:00 仍是下午；night 2h —— 迟到的「晚安总结」拖到凌晨毫无意义，还会吵醒用户。
  `target` 取的是「今天的整点」，所以 night 实际只覆盖 22:00–24:00，**跨零点不补**。
- **为什么不是全局一个数**：2026-11-14 实测 GitHub 两次 schedule 触发间隔达 **280 分钟**
  （11-13T20:20Z → 11-14T01:00Z）。原来的全局 3 小时比这个间隔还窄，
  整个补发窗口会落在间隔里、那个时段的推送被**静默丢掉**。详见 [docs/不足与风险.md](docs/不足与风险.md) §4.1。
- 想改时刻改 `PUSH_HOURS`，想改补发宽度改 `PUSH_CATCHUP_HOURS`。**不要往 workflow 加 cron。**

**人格只有一份，在 `prompts.py` 的 `PERSONA`。** 傲娇雌小鬼：先损一句，再落到实事上。
名言点评和三个时段共用它 —— **改人格只改这一处**。
硬边界写在 PERSONA 里：不许人格羞辱、不许拿外貌/家庭/智力/成绩开刀、不许色情、不许鼓励自伤。
调侃对象是「拖延这件事」，不是用户这个人。改这段前先想清楚边界还在不在。

**绝对不许带课表。** 用户原话「课表的提醒我不用你」。
`diary_facts()` 只读 `data/diary/plans.json` 和 `quotes.json`，**不碰任何 schedule 数据**。
提示词里也写死了这条。加事实来源时别顺手把课表塞进去。

**一言**：`https://v1.hitokoto.cn/`，用户指定的源。取 `hitokoto` + `from`，拼成「text（from）」。
请求失败只 WARN 并返回空串，**不影响推送**。提示词要求「不许改它的字」。

**事实驱动，不许编造。** `diary_facts(kind, today)` 按 kind 给不同事实集：
早上给 due_today / overdue / no_due，中午给计数 + 两条优先项，晚上给今天新增 / 今日到期仍未完成 / 逾期 / 今天的名言（带点评）。
全空时明确输出「日记里今天没有任何可播报的内容」，让 AI 直说没有，而不是硬凑。

**降级路径**：AI 失败或返回空 → `fallback_message()` 出确定性文案（仍含事实和一言）。
判断用显式 `ai_ok` 布尔，**不要改回 `text.startswith(...)` 那种脆判断**。
日志会打「文案来源：AI / 降级」，看这行就知道有没有真的用上 AI。

**交付方式**：文案写进 `/tmp/push-message.txt`，workflow 的飞书步骤
`if [ -s ... ]` 就发纯文本，否则发原来的状态卡片。执行步骤开头有 `rm -f` 清残留。
**推送模式不提交任何文件**（`steps.mode.outputs.push == 'true'` 时跳过提交步骤）。

### 5.2 潜在计划写回日记（`diary_writeback.py`）

用户要的闭环：**13 号挖到的潜在计划 → 写进 14 号日记（md 复选框）→ 15 号复查，没勾的滚到 15 号**。

- **只在凌晨写（用户硬要求）**：他白天在 Obsidian 里编辑，白天提交会撞车。
  守卫在 `in_window()`，窗口取 `data/sources.json` → `diary.writeback_window`
  （当前 `00:00-06:00`，代码默认 `00:00-05:00`，北京时间，含头不含尾，支持跨零点）。
  **窗口外只复查勾选、一次都不提交**，返回 `skipped=outside_window`，且**不推进 `last_date`**，
  所以滚动链条不会断，下一个凌晨窗口接着写。格式写错时**失败关闭**（当天不写），不会退化成随时写。
  **守卫在代码里而不是 cron 里**，手动派发 `diary` 或 Actions 延迟到白天也写不进去。
- **tick 会强制补跑**：`do_tick()` 发现「现在在窗口内」且 `runtime.writeback_date != 今天`，
  就把 `diary` 加进本轮待跑列表，**不管它 120 分钟的间隔到没到**。
  写回成功后才记 `writeback_date`。这是修「错过凌晨窗口 → 计划永久不再进入待办」（审计 §1.5）的关键：
  窗口有 6 小时宽、每 20 分钟一次机会，只要 Actions 在这 6 小时里跑成过一轮就写得上。
- 落点：`{root}/{daily_dir}/{YYYY-MM-DD}.md`，`daily_dir` 在 `data/sources.json` 里配（当前是 `日`）。
- **只动助手区块**，边界是 `<!-- assistant:plans:start -->` / `<!-- assistant:plans:end -->`。
  区块外是用户正文，**一个字节都不许碰**。`upsert_block()` 有 markers 就替换区间，没有就追加。
- 每行带 `<!-- pid:xxx -->`，复查靠 pid 对齐，不靠文本匹配（用户可能改字）。
- **已勾选的状态必须保住**：合并时 `[x]` 优先，重复写回同一天是幂等的。
- 复查读**上一次写回的那天**（`writeback.json` 的 `last_date` / `last_path`），
  勾了的 → 进 `done_ids`，没勾的 → 进 rolling 滚到今天。
- **`done_ids` 里的计划先从候选里剔掉再选**（`candidates = [p for p in plans if p["id"] not in done_ids]`）。
  这一步不能省：`state` 要等 `diary_collector` 拿到本函数返回值之后才改成 `done`，
  传进来的 `plans` 里它们还是 `open`，不自己排掉就会把刚勾完的待办又写进新一天。
- 选取规则 `pick_plans(plans, rolling, limit)`：`state=="open"` 且（**没写过** `written_once` 或 pid 在 rolling 里），
  排序键 `(rolling 优先, due_date, first_seen 升序, -confidence)`，上限 `max_writeback`（默认 20）。
  **`first_seen` 升序是刻意的**：老计划先写，否则新计划天天挤掉老计划，老的永远轮不到（审计 §3.4 的饥饿问题）。
  写过的计划会带 `written_once=True` / `last_written`，只有 rolling 里的才允许再写一次。
- **没有内容且文件不存在时不创建空日记。** 别给用户造垃圾文件。
- 新建文件用用户的三段骨架 `NEW_FILE_TEMPLATE`（今日记录 / 随笔感想 / 明日计划）。

**采集侧必须剥掉助手区块**：`diary_collector.strip_assistant_block()` 在
`extract_checkboxes()` 和 `ai_extract()` 之前都要调。
不剥的话助手自己写进去的计划会被当成用户新写的再抽一遍，**无限自我复制**。selftest 有断言守着。

**⚠️ Gitee API v5 的坑：不存在/已删除的文件 GET 返回 HTTP 200 + `{}`，不是 404。**
所以 `read_file()` 判断存在与否**看 body 里有没有 `sha`**，只看状态码会误判成「文件存在但空」，
接着不带 sha 去 PUT 就会被拒。写入：新建走 POST，更新走 PUT **且必须带当前 sha**。

**sha 被拒会自动重试一次**：重读最新正文 → 重新 `upsert_block()` → 再提交。
区块外以用户最新内容为准，所以他凌晨临时补的一句话不会被覆盖。只重试一次，不无限循环。

写回失败只 WARN，不影响采集结果 —— 日记数据已经落盘了。

### 5.3 课表当前时间线（`week_svg.py` + `docs/js/app.js`）

两层，**颜色和精度都不同，别混为一谈**：

1. **SVG 里的浅灰虚线**：`finalize` 每 20 分钟重画一次，所以是 **20 分钟精度**。
   它只是「生成时刻的参考位置」，**刻意画成浅色 `#CED4DA` + 灰色时间标签**。
   早先这里是红线，和网页覆盖线叠在一起，用户看到 20 分钟前的位置还以为是真的（审计 §5.1）。
   **不要把它改回红色** —— selftest 有断言检查 SVG 源码里不含 `#FA5252`。
2. **网页上的红色覆盖线**：`docs/js/app.js` 的 `startNowLine()` 每 30 秒按浏览器时间实时移动，
   这才是真·实时。几何参数由 `week_svg.py` 导出到 `docs/data/schedule_geo.json`
   （width/height/pad/gutter/col_w/row_h/body_top/today_col/slots/class_minutes/date）。
   **JS 的 `nowLineY()` 必须和 Python 的 `time_y()` 用同一套锚点算法**，改一边就要改另一边。

`time_y()` 的关键设计：**每节课 45 分钟正好占满一行，课间线停在两行交界不拉伸**。
否则大课间（08:50→10:05 有 75 分钟）会让线看着像还在上课。
早于第一节课钳到顶，晚于最后一节钳到底。selftest 有 6 条断言守着这个几何。

JS 侧按 `geo.date` 算日期漂移来平移 `today_col`，超出 0..6 就隐藏线。
时区换算成北京时间用的是 `Date.now() + (8*60 + getTimezoneOffset())*60000`。
覆盖线用**百分比定位**（`.svg-wrap` 相对定位 + `.now-line` 绝对定位），所以图片缩放也不会歪。

**在 README 里看 SVG 是看不到实时线的** —— GitHub 会把它当静态图片渲染，不执行 JS。
实时线只在网页看板（`docs/index.html`）里存在。这一点必须跟用户说清楚，别让他以为 README 里的线会动。

### 5.4 名言 AI 点评（`prompts.py` + `diary_collector.py`）

`DIARY_SCHEMA` 的 quotes 现在是 `[{"text","context","comment"}]`。
`comment` 要求：40 字以内、**针对这句话本身**（体现了什么 / 哪里在自我欺骗 / 下一步具体做什么），
不许空泛夸奖、不许复述原句，没话说给空串。风格走 `PERSONA`。

链路：`ai_extract()` → `safe_text(q.get("comment"), 120)` → `merge_quotes` 的 `_carry` 元组里
**必须含 `comment`**（否则旧条目永远补不上点评）→ `render_readme.render_quotes()` 输出 `> 💬 ...`
→ 前端 `renderQuotes` 输出 `<span class="ctx">💬 ...</span>`。

**存量名言靠 `backfill` 模式补点评**：`diary_collector.backfill_comments(batch=8)`
挑出 `comment` 为空的名言，每批 8 条调 `prompts.comment_messages()`，成功才写回。
`_carry` 只填空字段、不覆盖已有点评，所以重复跑是幂等的。
Actions 上手动派发 `backfill`；本地 `python scripts/main.py backfill`。
**不要靠清 `quotes.json` 重抽来补** —— 那会连带重抽计划，人工 `state` 标记有丢失风险。

---

## 6. 已知限制 / 待办

- **海外 IP 可达性：已实测通过（2026-11-13）。** Actions 出口 IP `20.118.38.104`（美国 Azure），
  `probe` 模式 10 个目标 **全部可达，不可达 0**：学习通登录/作业/课程/空间、校网公告、教务处、
  校团委、飞书均 HTTP 200；Gitee 与 AI 中转 HTTP 401（可达，只是未带凭据）。
  **不需要 self-hosted runner，也不需要国内定时任务兜底。**
  学校日后若封海外 IP，重跑 `probe` 即可确认，退路见 DEV_GUIDE 4.3。
- **Secrets 已配置（2026-11-13）**：`FS_WEBHOOK`、`AI_API_KEY`、`GITEE_TOKEN`、
  `CX_USERNAME`、`CX_PASSWORD`、`DIARY_EXCLUDE`，Variables `AI_BASE_URL`、`AI_MODEL`。
  **能用 API 写，不必走网页**（轮换凭据时照这个做）：
  GET `/actions/secrets/public-key` 取 base64 公钥与 `key_id` → 用 libsodium `crypto_box_seal`
  加密 → PUT `/actions/secrets/{name}`，body 为 `{encrypted_value, key_id}`。
  **GitHub 返回 204 本身就是加密正确的证明**（解不开会 422）。
  本机 PATH 里没有 node/npm/gh，但有两个可用运行时：
  `C:/Program Files/Adobe/Adobe Creative Cloud Experience/libs/node.exe`（Node v24，自带 fetch）、
  `C:/Users/XOS/AppData/Local/Temp/py311/python.exe`（3.11.9 embeddable，本地跑 selftest/ruff 用）。
  加密库从 npm 官方 registry 取 `libsodium-wrappers`，**它依赖 `libsodium`，要一并下载**；
  解压到仓库外的临时目录充当 `node_modules`。两个坑：GNU tar 会把 `C:/...` 的冒号当远程主机，
  必须加 `--force-local`；脚本要写成真实文件再跑，`node -e` 里 `process.chdir` 不改变模块解析基准。
  明文值走环境变量传入、不落盘不进 git，用完删掉临时目录。
  **⚠️ 开发期在聊天里贴过的凭据一律视为已泄露，必须轮换后再填进 Secrets。**
- **`study` / `diary` 已端到端跑通（2026-11-13）**，Secrets 配好后首次真实采集暴露了四个 bug，均已修：
  - `study`：登录成功、**14 门课全扫描失败 0**、38 条作业、**待完成 7**（修 `未交` 前只有 4）、
    两条真实截止日 2026-11-19 / 2026-11-20 已进 README 清单，飞书推送成功。
  - `diary`：Gitee 扫到 23 个 md、处理 12 个、**名言 7 条、计划 39 条**，飞书推送成功。
  - **AI 模型名必须是 `deepseek-v3.2`（点号），不是 `deepseek-v3-2`。** 写错时每次调用都 403
    「免费API仅可使用免费模型目录中的模型」，极易误判成 key 失效或海外 IP 被封。
    排查顺序：先 `GET /v1/models` 看目录（实测 200、31 个模型），再核模型名，最后才怀疑 key/IP。
    换中转站时 `AI_MODEL` 必须从该站 `/models` 返回里复制。
- **采集模式只提交 `data`，派生产物提交权独占给 `finalize`/`all`。** 这条边界是数据丢失事故换来的：
  原先每个模式都 `git add data docs README.md`，加上 checkout 默认用派发时的 `github.sha`，
  排队中的 `diary` 在旧树上渲染 README，rebase 冲突直接吞掉已抽出的 24 条计划。
  现在 checkout 显式 `ref: ${{ github.ref_name }}`，采集模式 `git add data` 后
  `git checkout -- README.md docs`。**改提交步骤前必读 `docs/技术路径.md` 第 11 节。**
- **`notice` 已端到端验证（2026-11-13）**：4 个源全成功，存量重灌 **78 条零丢失**、**高相关 3 条**，
  正文摘要 **3/3** 补全。截止日只有 `2026-05-31` 一条经正文复核成立（原话「2026年5月31日报名截止」）。
  两个必须记住的机制：**关键词打分封顶 3 分**（`keyword_score`：boost=3 / gate=2 / block=0），
  没配 `AI_API_KEY` 时「高相关」恒为 0；**只有新增公告送 AI**（`AI_MAX_CANDIDATES=30`/次），
  存量不会被重打分。换 key 或改提示词后要重灌：清空 `data/notice/notices.json`，
  临时调大 `AI_MAX_CANDIDATES` 覆盖存量，跑一次 `notice`，再改回 30。详见 CHANGELOG 第 6 条。
- **正文摘要 `max_tokens` 必须 ≥2000。** 给 1000 时 `content` 返回空 → `_extract_json` 得 None
  → 静默跳过，日志只有「正文摘要补全：0/3」而没有任何报错。三个跳过分支现在都打 WARN。
- **不要相信 AI 给的截止日，`deadline_supported()` 是唯一防线。** 模型会把「初赛阶段10月1日—10月30日」
  的结束日当报名截止，也会凭空编日期（`2026-09-18` 在正文里根本不存在）。
  **收紧提示词后重抽仍返回同一个错值**，所以提示词只是第一层，代码校验才是硬约束：
  正文必须出现报名截止语义（`DEADLINE_CUE_RE`：截止/逾期/不予受理/前报送/报名…前/提交…前）
  **且**该日期能在正文里找到，否则丢弃并打 WARN。改提示词或换模型后，
  要拿真实公告复核一遍，别只看「补全 N/N」就当成功。
  另注意 `ai_detail()` 只在 `deadline` 为空时写值，**存量错值不会被自动纠正**，要手工清 `data/notice/notices.json`。
- **正文只取 `v_news_content` 容器内部。** `body_text()` 按 `div` 嵌套深度找匹配闭合标签，
  不要改回「从容器名起取固定 N 字符」——那样会把页脚和「上一篇/下一篇」喂给 AI。selftest 有 2 条断言守着。
- **考试列表解析未经真实待考数据验证。** 目前样本里两场考试都是「已完成」，
  待考行的 `onclick` 函数名和倒计时结构可能与作业不同。第一次遇到待考时要核对。
- **详情页绝对时间已实测拿到形状（2026-11-13），但年份是推算的。**
  作业详情 `<p>作答时间:<em>MM-DD HH:MM</em>至<em>MM-DD HH:MM</em></p>`、
  互评详情 `<p class="hpInfo">互评时间：MM-DD HH:MM 至 MM-DD HH:MM</p>`，**都不带年份**。
  `detail_time_window()` 按「距抓取时刻最近的一年」补，超 180 天回退一年、跨年结束 +1 年。
  **学期交界（12 月底 / 1 月初）和暑假前后要人工核对一次**，这是唯一可能系统性错一年的地方。
  解析失败时退回倒计时估算并打 WARN，不会静默；日志里没有「详情页补全截止时间」就是没匹配上。
- **招生就业处 `hncj.goworkla.cn` 默认关闭**（第三方建站，列表结构不同）。需要时再适配。
- **Actions 额度**：本仓库已设为 **public**，额度无限。
  注意计费是「每个 job 向上取整到分钟」：`*/20` 一条 cron = 72 轮/天，
  每轮平均 1–2 分钟 ≈ **2200–4400 分钟/月**，**私有仓库的 2000 分钟免费额度装不下**。
  若日后转 private，把 `TICK_INTERVALS` 整体调大（例如 study 180 / diary 360 / notice 720），
  或把 cron 改成 `*/30`，**不要靠删 cron 条数来省** —— 现在只有一条，删了就全停。
- **Actions 60 天无 push 会自动停用 schedule。** 本项目每轮 tick 都可能产生数据变更，正常不会触发。
  长期放假无课时可能触发，去 Actions 页面手动重新启用即可。
- **课表教师列恒为空。** `assets/WakeUp导入课表.ics` 已脱敏剥离教师名，
  `split_location()` 因此恒定返回 `teacher=""`。**这是预期行为，不要去"修"它。**
  拆分逻辑保留着，换回未脱敏原件即生效。详见 [docs/原始文件.md](docs/原始文件.md)。
- **任务标题里的教师姓名已在采集层剥掉**（`utils.scrub_task_title()`，结构规则不用黑名单）。
  **公开链接默认已去掉 `cpi` / `enc` / `answerId`**（`study.public_links="course"`）。
  代价：README 里的链接只到课程页，用户要多点两下才能到作业。
  想要一键直达就把 `public_links` 改成 `"full"` —— **这是用户的产品取舍，不是 bug，别擅自改回去**。

---

## 7. 改东西时的检查清单

**每次动手前后都过一遍。完整的可执行版本在 [docs/AI运维手册.md](docs/AI运维手册.md)。**

- [ ] **先跑 `ruff check scripts --select E9,F63,F7,F82,F821,F401` + `python scripts/main.py selftest`**，两条都绿再谈别的
- [ ] 改了日志文案 → 同步改 `notify_feishu.js` 的 `TASK_TEMPLATES` 正则（指标靠正则从日志里抠）
- [ ] 改了脱敏正则 → **Python `_REDACT_PATTERNS` 和 JS `REDACT_PATTERNS` 两边都改**
- [ ] 改了数据字段 → 同步改 `docs/数据结构.md` 和 `docs/js/app.js`
- [ ] 改了解析器 → 同步改 `main.py` 的 `do_selftest()` 和 `study_collector` 的 `SAMPLE_*` / `parse_smoke()`
- [ ] 改了调度频率 → 改 `main.TICK_INTERVALS` / `PUSH_HOURS`，**不要往 workflow 加 cron**；同步改 AGENT/README 模板/工作原理里的频率描述
- [ ] 改了提交步骤 → 确认采集模式仍只 `git add data`，派生产物只由 `tick`/`finalize`/`all` 提交（见第 6 节事故）；推送模式一个文件都不提交
- [ ] 改了人格/说话风格 → 只改 `prompts.py` 的 `PERSONA`，别在各处复制一份；确认硬边界还在
- [ ] 改了推送事实来源 → 确认仍然**不含课表**，且只用 `data/diary/*`
- [ ] 改了日记写回 → 确认 `strip_assistant_block()` 仍在抽取前调用，区块外正文零改动，`[x]` 不被覆盖
- [ ] 改了日记写回 → 确认凌晨窗口守卫还在（`in_window()`），**白天一次都不提交**，窗口外不推进 `last_date`
- [ ] 改了 `pick_plans()` → 确认 `done_ids` 仍在选取前被剔掉，`first_seen` 仍升序（老计划不被饿死）
- [ ] 改了课表几何（ROW_H/SLOTS/45分钟）→ Python `time_y()` 和 JS `nowLineY()` 必须同步改，并重跑 selftest
- [ ] 改了 SVG 时间线 → 确认生成线仍是**浅灰**，红色只由 JS 画
- [ ] 改了名言字段 → `dedup.merge_quotes` 的 `_carry` 元组、README 渲染、前端 `renderQuotes` 三处都要跟
- [ ] 改了截止时间解析 → 确认「抓不到」仍进**独立的 unknown 类**，绝不混进「更远」
- [ ] 新增了会公开渲染的字段 → 先问「这个值能不能定位到人」，清洗放采集层，示例用占位符
- [ ] 派发 Actions → **串行，一次一个**，等上一个 completed 再派下一个（同 group 只留一个 pending）
- [ ] 换了 AI 中转站或模型 → 先 `GET /v1/models` 核目录，`AI_MODEL` 照抄返回里的名字，别凭记忆写
- [ ] 遇到 `unknown` 作业状态 → dump 状态原文再补 `PENDING`/`SUBMITTED`/`DONE` 词表，不要猜
- [ ] 加了新公告源 → 只改 `data/sources.json`，先确认列表页是静态 HTML（不是 JS 渲染）
- [ ] 加了新依赖 → 更新 `requirements.txt`，并在 DEV_GUIDE 里说明为什么需要
- [ ] 改了文档里引用的函数名 → selftest 有断言扫全仓库 md，引用不存在的函数会红
- [ ] 任何情况下都不要把凭据写进文件；开发期贴过的凭据一律轮换

---

## 8. 文档索引

| 文件 | 内容 |
| --- | --- |
| **[docs/AI运维手册.md](docs/AI运维手册.md)** | **接手第一份**：可执行的改动流程、验证方法、派发方式、逐功能验收清单、踩坑速查 |
| [DEV_GUIDE.md](DEV_GUIDE.md) | 从零部署：建仓、配 Secrets、首次运行 |
| [docs/技术路径.md](docs/技术路径.md) | 每个技术选型的理由、被否决的方案、实测证据 |
| [docs/工作原理.md](docs/工作原理.md) | 一次运行的完整数据流 |
| [docs/数据结构.md](docs/数据结构.md) | 所有 JSON 字段定义 |
| [docs/仓库结构.md](docs/仓库结构.md) | 目录树 + 每个文件的职责 |
| [docs/AI提示词.md](docs/AI提示词.md) | 提示词设计说明与调参建议 |
| [docs/常见问题.md](docs/常见问题.md) | 故障排查 |
| [docs/个人画像.md](docs/个人画像.md) | 用户是谁 / 关注什么 / 说话风格硬边界（**public，不含凭据**） |
| [docs/开发习惯.md](docs/开发习惯.md) | 16 条协作规则，每条附用户原话 —— **接手前必读** |
| [docs/不足与风险.md](docs/不足与风险.md) | 逐条问题的诚实审计：根因位置 + 实测证据 + **修复状态**，另列「不是缺陷别乱改」「等用户决定」—— **接手前必读** |
| [docs/原始文件.md](docs/原始文件.md) | `assets/` 两个上传文件的脱敏口径、审计结果、换文件流程 |
| [docs/竞赛日历.md](docs/竞赛日历.md) | 269 项校级竞赛目录 + 已核实时间（**手工维护，Actions 不覆盖**） |
| [CHANGELOG.md](CHANGELOG.md) | 版本记录 |