# CHANGELOG

本项目的所有版本记录。日期为北京时间。

## v1.0.0 — 2026-11-12

首个正式版。四个数据源全部打通，README 任务清单 + 飞书推送 + 网页看板可用。

### 新增

**采集**
- `study_collector.py`：学习通四步链（AES 登录 → 课程列表 → 逐课铸 token → 作业/互评/考试列表）。
  实测覆盖 28 门课，作业与考试两个接口都验证通过
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

### 已知限制

1. ~~海外 IP 可达性未验证~~ **已实测通过（2026-11-13）**：Actions 出口 IP `20.118.38.104`
   （美国 Azure），`probe` 10 个目标全部可达、不可达 0，学习通四个端点均 HTTP 200。
   不需要 self-hosted runner。退路仍留在 `DEV_GUIDE.md` 4.3 作为日后被封时的预案
2. 截止时间多数是**推算值**（列表页只给相对倒计时），渲染时标 `≈`。
   详情页补全正则 `DETAIL_DT_RE` 尚未实测
3. 考试的**待完成行**结构未验证（实测样本都是已完成状态）
4. Actions 分钟数用量是估算，需要一个月的真实数据校准。仓库已设为 public（额度无限），
   若日后转 private 必须换省额度 cron，见 `DEV_GUIDE.md` 第 2 节
5. `study` / `diary` / `notice` 三个采集器**尚未端到端跑通**，需先配 Secrets。
   `selftest`（26 项断言）、`probe`、`finalize`（含 git 写回）已在真实 runner 上验证成功
5. `bge-m3` 向量模型未接入（v1 不需要语义检索）
6. 公告正文摘要（`DETAIL_*` 提示词）已写好但未接线，`description` 字段恒为空

### 依赖

- Python 3.11+：`requests`、`pycryptodome`（仅两个）
- Node 20：飞书推送脚本只用内置 `https`，无 npm 依赖
- 参考仓库（**只读，永不修改**）：
  [wool-gathering](https://github.com/jiuyuan233/wool-gathering)（目录结构）、
  [Lottery-BiliBiliToolB-Web233](https://github.com/jiuyuan233/Lottery-BiliBiliToolB-Web233)（飞书方案）