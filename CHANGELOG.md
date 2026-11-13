# CHANGELOG

本项目的所有版本记录。日期为北京时间。

## v1.1.2 — 2026-11-13

README 瘦身：公告与潜在计划挪进独立文档，目录区只留链接。

### 变更

- **README 移除 `## 📢 值得关注的公告` 与 `## 🎯 潜在计划` 两节**，
  改为渲染成两份独立生成物：
  - [`docs/公告.md`](docs/公告.md) ← 模板 `scripts/notice_template.md`
  - [`docs/潜在计划.md`](docs/潜在计划.md) ← 模板 `scripts/plans_template.md`
- README `## 🧭 目录` 区挂上两份文档的链接，**并带实时条数**
  （`{{NOTICE_COUNT}}` / `{{PLAN_COUNT}}`），不点进去也知道有没有新东西。
- 两份文档各自补了「相关」区：全量 JSON、网页看板、对应技术路径章节、FAQ 分组。
  潜在计划文档额外写清了**怎么标记完成**与 **T+1/T+2 写回闭环**。
- `render_notices()` / `render_plans()` 改为返回 `(文本, 条数)` 二元组；
  条数上限提为模块常量 `NOTICE_LIMIT = 12` / `PLAN_LIMIT = 15`，
  并作为 `{{NOTICE_LIMIT}}` / `{{PLAN_LIMIT}}` 注入文档，改上限时文案自动跟着变。
- 新增 `render_doc()`：按模板渲染独立文档，**缺模板只报错不抛异常**，
  但会让 `finalize` 返回 False → 飞书报红，不会静默产出半份 README。

### 修复

- `docs/竞赛日历.md` 里指向 `README.md#-值得关注的公告` 的锚点已失效，改指 `公告.md`。
- `docs/AI提示词.md` 的「README 公告表」措辞同步改为 `docs/公告.md`。

### 自检

新增 8 条断言（共 **72 项**），其中一条是结构性守卫：
**三份模板里的全部 `{{占位符}}` 必须在 `render_readme.py` 里有对应渲染键** ——
漏一个就会在页面上留下字面量 `{{X}}`，这类错误以前只能靠肉眼看渲染结果发现。

### 边界

两份新文档落在 `docs/` 下，因此自动沿用既有提交边界：
`finalize`/`all` 用 `git add data docs README.md` 提交，
采集模式 `git checkout -- README.md docs` 还原。**渲染层独占派生产物提交权**这条不变。

## v1.1.1 — 2026-11-13

把用户上传的四个原始文件脱敏后入库，README 目录区补超链接。

### 新增

- **`docs/原始文件.md`**：四个文件的来源、逐项脱敏口径、审计结果、换文件流程、`.gitignore` 例外规则。
  README 只放链接，细节全在这份文档里（遵循「README 只是目录」）。
- **`assets/抓包-学习通-脱敏.har`**（67 条请求 / 410 KB）与
  **`assets/抓包-空间页-脱敏.har`**（4 条请求 / 31 KB）：
  令牌与身份参数值、32 位 hex、请求体一律 `REDACTED`；
  **方法、路径、参数名、状态码、时序全部保留**，可直接导入 DevTools 复看接口结构。
- README 模板目录区新增「📎 原始文件（已脱敏）」一组超链接，指向四个 asset 与说明文档。

### 变更

- **`assets/WakeUp导入课表.ics` 剥离全部教师名**：172 条「教室+教师」只留教室，
  14 条纯教师名清空。完整性对照通过：`VEVENT`/`SUMMARY`/`DTSTART` 均 186=186，
  总行数 1681=1681，**变动行恰好 186 且全是 `LOCATION`**，行尾保持 LF。
  - **行为后果**：`split_location()` 现在恒定返回 `teacher=""`，课表教师列会一直空着。
    这是脱敏的代价，不是解析 bug；拆分逻辑保留，换回未脱敏原件即生效。
- **`.gitignore` 开窄例外** `!assets/*-脱敏.har`。
  `*.har` 的总禁令**保留不动** —— 文件名不带 `-脱敏` 后缀的抓包仍然提交不上去。
- 源码与文档里的真实教师名、真实 `cpi` 全部换成占位符
  （`scripts/main.py` 自测夹具、`scripts/schedule_parser.py` docstring、
  `docs/技术路径.md` §5、`docs/常见问题.md` E4）。

### 审计

| 文件 | 身份字面量残留 | 32 位 hex 残留 | 未脱敏令牌参数 |
| --- | --- | --- | --- |
| 抓包-学习通-脱敏.har | 0 | 0 | 0 |
| 抓包-空间页-脱敏.har | 0 | 0 | 0 |

PDF 竞赛目录**一个字未改**（学校公开发布，本身无个人信息）：
手机号/学号/邮箱/身份证形态各 0。

全仓库扫描确认：**无 GitHub PAT、无 AI 密钥、无 Gitee 令牌、无飞书 webhook、
无手机号/学号/密码**明文入库。

### 已知暴露（待用户决策，未擅自改动）

- `data/study/*.json` 与 README 的任务 URL 里含真实 `cpi` 和每条作业的 `enc` 令牌。
  `enc` 是**功能性必需**（去掉链接就打不开），但公开仓库等于把作业页访问能力公开。
- 部分**课程名本身带教师名**（如「××26级高等数学A（一）」），来自学习通实时抓取，
  属生成物，不能手改；要去除得在采集层加过滤。

## v1.1.0 — 2026-11-13

按用户六项要求改造：全课程清单、日记写回闭环、课表时间线、名言点评、三时段人格推送、交接文档。

### 新增

**三时段飞书推送（`daily_push.py`）**
- `morning` 07:02 / `noon` 12:02 / `night` 22:02 三条 cron，**分钟错开 2 分**避免与 `*/20` finalize 撞车
- 事实来源**只有日记**（`plans.json` / `quotes.json`），**不带课表**（用户明确要求）
- 一言接入用户指定的 `https://v1.hitokoto.cn/`，失败只 WARN 不阻断推送
- 人格 `PERSONA` 集中在 `prompts.py`，傲娇雌小鬼：先损一句再给实事；名言点评与推送共用
- AI 失败 → `fallback_message()` 确定性降级，用显式 `ai_ok` 布尔判断
- 文案写 `/tmp/push-message.txt`，飞书步骤有文案发纯文本、无文案发状态卡片；**推送模式不提交任何文件**
- concurrency 拆成 `pa-push` / `pa-collect` 两个 group，采集排队不会挤掉定时推送
- 实测 run 43382244174：`morning 文案 134 字，一言有，文案来源：AI`，飞书推送成功

**日记写回闭环（`diary_writeback.py`）**
- T+1 把昨天挖到的潜在计划写进今天日记的 md 复选框，T+2 复查，没勾的滚到今天
- **只动 `<!-- assistant:plans:start/end -->` 区块**，区块外用户正文零改动；每行带 `<!-- pid:xxx -->`
- 已勾选状态优先保住，同日重复写回幂等；无内容且文件不存在时**不创建空日记**
- 复查读 `writeback.json` 的 `last_date`/`last_path`，跨天漏跑也能对齐
- `diary_collector` 新增 Gitee 写入：新建走 POST、更新走 PUT 且必须带当前 sha
- 抽取前一律 `strip_assistant_block()`，否则助手自己写的计划会被反复重抽（自我复制）
- selftest 6 条断言守着：区块往返、正文保留、幂等替换、勾选可读、选取规则、不重复抽取

**课表当前时间线（`week_svg.py` + `docs/js/app.js`）**
- SVG 内红线 + 圆点 + 时刻标签，`finalize` 每 20 分钟重画（**20 分钟精度**）
- 导出 `docs/data/schedule_geo.json` 几何参数，网页 JS 每 30 秒实时移动覆盖线（真·实时）
- `time_y()`：每节课 45 分钟正好占满一行，**课间线停在两行交界不拉伸**，
  否则大课间（08:50→10:05 有 75 分钟）会让线看着像还在上课；两端钳位
- Python `time_y()` 与 JS `nowLineY()` 共用同一套锚点算法，selftest 6 条断言守着

**名言 AI 点评**
- `DIARY_SCHEMA` quotes 增加 `comment`：≤40 字、针对这句话本身、给具体动作、走 PERSONA
- `merge_quotes` 的 `_carry` 元组加入 `comment`；README 输出 `> 💬 …`，前端输出 `💬 …`
- 实测 run 43381658396：名言 15 条，**新增 8 条全部带点评**；存量 7 条无（`_carry` 只填空字段，需清库重抽）

### 修复

- **★同一门课挂两个教学班时任务串班**：`course_rows()` 原先只按 `course_id` 归组，
  实测英语课 `course_id=201780805` 有 `class_id` 156400069 / 156290484 两条，
  导致 README 里两行都显示同样 2 条任务（一行作业数 3、另一行 0）。
  改为 `course_key() = (course_id, class_id)` 复合键，新增 2 条 selftest 断言
- **逾期互评任务一直挂在清单里**：`work/task` 详情页只有空标签 `作答时间:`（值由 JS 填），
  但 `eval-list` 详情页有真实窗口 `互评时间：11-06 07:01 至 11-10 23:01`。
  新增 `detail_eval_window()` 解析（不带年份，算出来在半年以后就回退一年），窗口结束即截止。
  实测 run 43379475423：清单 4→3 可做，`已剔除：逾期 1`
- 新增 `detail_time_hints()` 诊断：正则失配时 dump 页面含「时间/截止/开始」的片段，不靠猜

### 任务清单口径（用户要求）

- **14 门课全部列出**，以课程列表为全集，没可做任务的写「无」
- 按**活跃度 = 作业发表数量**降序，组内按截止近 → 远
- `STALE_DAYS=7` 剔除开始超一周的；逾期、已完成/已批阅一律剔除
- 剔除数量写进 `{{FILTER_NOTE}}`；前端 `tasks.json` 只喂过滤后的 `kept` + `courses`
- **已知限制**：一周窗口用的是 `first_seen`（助手首次发现时间），**不是真实发布时间** ——
  普通作业的发布时间在静态 HTML 里取不到，只有互评任务能拿到真实开始时间

### 文档

- `AGENT.md` 新增第 5 节（四块新功能详解）、4.1 节（清单口径与时间字段限制），
  章节重编号至 8 节，检查清单补 7 条，修正过期的断言计数
- `docs/仓库结构.md`、`docs/数据结构.md`、`docs/AI提示词.md`、`docs/工作原理.md` 同步本轮改动
- 纠正 `数据结构.md` 里「课程名来自 middle 页 `<h2>`」的错误描述（那是已修掉的 bug 来源）

### 实测记录

| commit | 模式 | 结果 |
|---|---|---|
| `24ccc40` | selftest | **62 项断言、失败 0** |
| `24ccc40` | diary | 名言 15（8 带点评）、计划 63（重复 0）、写回守卫正确不建空日记 |
| `95a58c3` | finalize | README 14 门课全表、SVG 时间线已画、`schedule_geo.json` 已导出 |
| `8a752cc` | morning | 文案 134 字、一言有、来源 AI、飞书成功 |

### 尚未验证

- `noon` / `night` 未实跑（代码路径与 morning 相同，事实集与 brief 不同）
- 日记写回的**真实 Gitee 新建/更新**未触发：全部计划 `first_seen=2026-11-13`，
  首次自然写回落在 **11-14** 的日记
- 网页实时时间线未在浏览器验证：**GitHub Pages 尚未开启**（`has_pages=false`），需用户点头
- Actions `schedule` 定时触发仍未观察到首次自然 firing（手动派发全部正常）
- 前端分桶视图尚未改成 README 那样的「按课程分组 + 活跃度排序」

## v1.0.0 — 2026-11-12

首个正式版。四个数据源全部打通，README 任务清单 + 飞书推送 + 网页看板可用。

### 新增

**采集**
- `study_collector.py`：学习通四步链（AES 登录 → 课程列表 → 逐课铸 token → 作业/互评/考试列表）。
  真实 runner 实测覆盖 **14 门课、失败 0**，作业与考试两个接口都验证通过
- `diary_collector.py`：Gitee API v5 增量读日记，git blob sha 判变更。
  双通道抽取：Obsidian 复选框（确定性）+ AI 挖名言/潜在计划
- `notice_collector.py`：4 个公开公告站（校网/教务处/教务处活动/校团委），零登录。
  翻页 + 三种日期格式抽取 + 关键词门 + AI 相关度评分
- `schedule_parser.py`：WakeUp `.ics` 解析，186 条上课记录，反推学期首日 `2026-09-07`，
  11 节作息表精确匹配，输出事件/课程聚合/本周课表

**渲染与推送**
- `render_readme.py`：零网络零 token。任务按截止时间四桶分类（逾期/今天/7天内/更远），
  顶部三级告警，已完成折叠块，数据源状态表
- `week_svg.py`：手写 SVG 周课表，无图形库依赖，今日列高亮
- `notify_feishu.js`：互动卡片，5 种模式模板，逾期/紧急自动改色，`[urgent]` 置顶
- `docs/`：静态任务看板（index.html + app.js + style.css），GitHub Pages 可直接部署

**调度**
- `assistant.yml`：单 workflow + 4 条 cron + 模式分发。
  用 `github.event.schedule` 原文判模式，规避 Actions 延迟导致的误判。
  diary 定为每天 4 次（日记一天最多改一次，高频扫描纯属浪费额度）
- `feishu-test.yml`：独立测飞书，不碰数据源
- `main.py`：8 个模式，含 `probe`（海外 IP 连通性探测）和 `selftest`（离线解析器自检）

**基础设施**
- `utils.py`：北京时间（zoneinfo + 固定偏移兜底）、倒计时解析、稳定 ID、JSON 读写、
  **统一日志脱敏闸口**
- `dedup.py`：两种相反的合并策略——公告旧值优先（score 只升不降），任务新值优先（保留精确截止时间）
- `ai_client.py`：OpenAI 兼容客户端，滑动窗口限流 + 3 次重试 + 三级 JSON 抽取
- `prompts.py`：用户画像 + 公告评分 + 日记挖掘 + 公告正文摘要（预留）

**文档**
- `AGENT.md`（维护交接铁律）、`DEV_GUIDE.md`（从零部署）、
  `docs/技术路径.md`（选型理由与实测证据）、`docs/工作原理.md`、`docs/数据结构.md`、
  `docs/仓库结构.md`、`docs/AI提示词.md`、`docs/常见问题.md`
- `docs/竞赛日历.md`：**手工维护，不由 Actions 生成**。以 `assets/河南城建学院2024年学科竞赛目录.pdf`
  （269 个编号）为底表，联网核实 2026–2027 赛季真实报名/比赛时间，标注大一资格门槛与建环专业长期目标。
  README 改成目录式导航（`scripts/readme_template.md` 新增 `## 🧭 目录`），长内容一律外链，不堆在 README 里

### 开发期修掉的问题

- 考试接口「参数传递不正确！」→ 考试用全小写 `courseid`/`clazzid`，作业用驼峰 `courseId`/`classId`
- 公告日期串到上一条 → 回看窗口取「最靠近链接的匹配」而非第一个匹配
- 公告排序变成最旧优先 → 改为两次稳定排序（日期倒序，再按分数倒序）
- `soon` 与 `overdue` 重叠导致 urgent 重复 → `soon` 要求 `0 <= delta <= 24h`
- 互评作业「待批阅」被当成没事可做 → 仍在倒计时时提升为 `pending`
- `_row_deadline` 缺 `over` 返回值导致 `over_flag` 未定义 → 改为返回四元组
- 两处 workflow 脚本注入（`${{ inputs.* }}` 直接插进 shell）→ 全部改走环境变量
- `docs/data/meta.json` 的 `week` 带 `**` 而前端用 `textContent` 渲染 → 写 JSON 时剥掉 Markdown 标记
- 教学周会停在最后一次手动跑 `schedule` 的那一周 → `finalize` 每次离线重算课表
- README 里「标记完成请改 `data/diary/plans.json`」在 diary 首次成功前是 404
  → 预置空封装 `{updated_at, count: 0, items: []}`，与 `save_list` 格式一致，`load_list` 读作 `[]`

### 首次真实采集暴露并修掉的问题（2026-11-13）

- **AI 每次调用 403** → 不是 key 失效也不是海外 IP 被封：`/v1/models` 返回 200、31 个模型，
  但配置里的 `deepseek-v3-2` 不在目录中，真实名字是 **`deepseek-v3.2`**（点号）。
  免费档只允许目录内模型，所以报 403 而非 404，极具误导性。
  已改仓库 Variable（PATCH 204）+ workflow 默认值。实测 `deepseek-v3.2` 与 `gpt-4o-mini` 均 200
- **3 条真待交作业漏出清单** → 真实状态词是 `未交`，而 `PENDING` 只有 `未提交`/`待提交`
  （`DONE` 里反倒有 `已交`），被判 `unknown`。补词后待完成 4 → **7**，
  两条带真实截止日（2026-11-19、2026-11-20）进入清单
- **14 门课名字全是「在线学习诚信承诺书」** → `mint()` 取了 `stucoursemiddle` 落地页的首个 `<h2>`，
  那是全站横幅。改为从课程列表页 `course-name` span 的 `title` 取（向后 2500 字符窗口），
  middle 页 `<title>` 兜底。真实 HTML 验证 **14/14 命中**
- **diary 抽出的 24 条计划全部丢失** → 提交步骤 `CONFLICT (content): Merge conflict in README.md`。
  两个叠加根因：① `actions/checkout` 默认检出派发时的 `github.sha`，排队期间别的 run 推了新提交，
  本 run 仍在旧树上渲染；② 每个模式都提交派生产物 README/docs，冲突面被放大。
  修法：checkout 显式 `ref: ${{ github.ref_name }}`；采集模式只 `git add data` 并
  `git checkout -- README.md docs`，派生产物提交权独占给 `finalize`/`all`。
  详见 `docs/技术路径.md` 第 11 节
- selftest 增加 4 条断言锁住上述解析修复（`未交` 归类、列表页课程名、去重、title 兜底），
  断言总数 26 → **30**，真实 runner 上全过
- **公告正文把页脚喂给了 AI** → `body_text()` 原来从容器名出现位置起取固定 30000 字符，
  连带「上一篇/下一篇」和版权页脚一起进提示词。改成按 `div` 嵌套深度找到匹配的闭合标签。
  真实页面实测正文 2756 → 2387 字符，selftest 加 2 条断言（取到容器内容 / 不含导航页脚）
- **AI 给的截止日三个里两个是错的** → 模型把「学院初赛阶段 10月1日—10月30日」的结束日当报名截止。
  先收紧 `DETAIL_INSTRUCTIONS`（明确排除赛程阶段日、发文日、举办日），清空重抽后
  **仍返回同一个错值** —— 提示词约束不可靠，必须代码兜底。
  新增 `deadline_supported()`：正文要有报名截止语义（截止/逾期/不予受理/前报送/报名…前/提交…前）
  **且**该日期能在正文里找到（兼容 `2026-05-31`／`2026年5月31日`／`5月31日`／`5.31` 等写法），
  否则丢弃并 WARN。逐条复核：`2026-10-30` 正文无「截止」字样 → 运行时已拦掉；
  `2026-09-18` 正文里根本没这个日期（模型编的）→ 已清空；
  `2026-05-31` 正文原话「2026年5月31日报名截止」→ 保留。
  断言总数 32 → **34**，真实 runner 上失败 0 项

### 已知限制

1. ~~海外 IP 可达性未验证~~ **已实测通过（2026-11-13）**：Actions 出口 IP `20.118.38.104`
   （美国 Azure），`probe` 10 个目标全部可达、不可达 0，学习通四个端点均 HTTP 200。
   不需要 self-hosted runner。退路仍留在 `DEV_GUIDE.md` 4.3 作为日后被封时的预案
2. 截止时间多数是**推算值**（列表页只给相对倒计时），渲染时标 `≈`。
   详情页补全正则 `DETAIL_DT_RE` 尚未实测
3. 考试的**待完成行**结构未验证（实测样本都是已完成状态）
4. Actions 分钟数用量是估算，需要一个月的真实数据校准。仓库已设为 public（额度无限），
   若日后转 private 必须换省额度 cron，见 `DEV_GUIDE.md` 第 2 节
5. ~~`study` / `diary` 尚未端到端跑通~~ **已跑通（2026-11-13）**：
   `study` 登录成功、14 门课失败 0、38 条作业、待完成 7（两条真实截止日 11-19 / 11-20 进清单）；
   `diary` 处理 12 个 md、名言 7 条、计划 39 条；两者飞书均推送成功。
   已在真实 runner 上验证成功：`selftest`（**34 项断言、失败 0 项**）、`probe`、`finalize`（含 git 写回）、
   `notice`（4 源全成功、78 条存量重灌零丢失、高相关 3）、`study`、`diary`。
   **唯一仍未验证的是 `schedule` 定时触发**：截至 2026-11-13T06:03Z，workflow `state=active`、
   Actions `enabled=true`、`allowed_actions=all`、仓库 public 非归档、默认分支 main，
   远端 blob 里 `on.schedule` 四条 cron 确认存在且表达式合法，但 `event=schedule` 的 run 数仍为 0，
   全部运行都是 `workflow_dispatch`。配置与 YAML 问题已排除，需继续观测首次自动触发
6. ~~**关键词打分上限只有 3 分**，存量公告不会被重打分~~ **存量已重灌（2026-11-13）**：
   `keyword_score` 封顶 3 分（boost=3 / gate=2 / block=0），没配 `AI_API_KEY` 时「高相关」恒为 0。
   配好 key 后已按预案执行：清空 `data/notice/notices.json`、临时把 `AI_MAX_CANDIDATES` 调到 120
   跑一次 `notice` —— **78 条存量全部找回、零丢失**，51 条送 AI，**高相关 0 → 3**，随后改回 30。
   打分分布 `{0:33, 1:25, 2:13, 3:4, 4:3}`。日常运行仍只有**新增**公告送 AI（30 条/次）
7. `bge-m3` 向量模型未接入（v1 不需要语义检索）
8. ~~公告正文摘要（`DETAIL_*`）未接线，`description` 恒为空~~ **已接线并验证（2026-11-13）**：
   `ai_detail()` 给高分且无摘要的公告抓正文（博达 CMS 容器 `v_news_content`），
   每轮上限 `DETAIL_MAX=5`，实测摘要 **3/3 补全**，README 公告表「说明」列是真实摘要。
   「截止」列不再无条件相信 AI，见上面「首次真实采集暴露并修掉的问题」里的 `deadline_supported()`。
   踩过的坑：`max_tokens=1000` 时 `content` 返回空 → 静默跳过「0/3」且无任何报错，必须 ≥2000

### 依赖

- Python 3.11+：`requests`、`pycryptodome`（仅两个）
- Node 20：飞书推送脚本只用内置 `https`，无 npm 依赖
- 参考仓库（**只读，永不修改**）：
  [wool-gathering](https://github.com/jiuyuan233/wool-gathering)（目录结构）、
  [Lottery-BiliBiliToolB-Web233](https://github.com/jiuyuan233/Lottery-BiliBiliToolB-Web233)（飞书方案）