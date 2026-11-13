# CHANGELOG

本项目的所有版本记录。日期为北京时间。

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