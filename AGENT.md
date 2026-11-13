# AGENT.md — 维护交接说明

**给任何接手本仓库的 AI 或人类维护者。** 本文件优先级高于你的全局默认行为。
读完这一份就能上手，不需要回溯开发历史。

---

## 0.这个项目是什么

河南城建学院某本科生的个人助手。GitHub Actions 定时跑，聚合三个数据源，
把**按截止时间排序的任务清单**渲染进 `README.md`，并通过**飞书机器人**推送。

三个数据源：

| 源 | 内容 | 采集方式 | 频率 |
| --- | --- | --- | --- |
| 1 | Gitee 日记里的名言 + 潜在计划 | Gitee API v5（只读 .md） | 每天 4 次增量 |
| 2 | 学习通作业/考试 | 模拟登录四步链 | 每小时 |
| 3 | 校园比赛/项目公告 | 公开列表页，**不登录** | 每天 3 次 |

外加 WakeUp 导出的课表 `.ics`（本地解析，不联网）。

---

## 1. 铁律（违反会直接搞坏项目）

1. **生成物禁止手改。** 下列文件全部由脚本产出，改了会被下次运行覆盖：
   - `README.md`
   - `data/study/*`、`data/notice/*`、`data/diary/*`、`data/schedule/*`
   - `docs/data/*`、`docs/week.svg`

   唯一允许手工编辑的数据文件是 **`data/sources.json`**（增删公告源、改关键词、改路径）。
   另外 `data/diary/plans.json` 里的 `state` 字段允许人工改成 `done` / `cancelled`，
   合并逻辑会保护这个人工标记不被 AI 覆盖。

2. **凭据只存在于 GitHub Secrets。** 绝不写进代码、workflow、commit、日志。
   `utils.log()` 已内置 `redact()`，会抹掉 `access_token=` / `Bearer` / `sk-` / `ghp_` / `password=`。
   新增日志时不要绕过 `log()` 直接 `print`。

3. **不要 force push。** 提交步骤用的是 `git pull --rebase` + 普通 `push`。
   推不上去就让步骤失败、飞书报红、人来处理。这是刻意的设计，别"优化"成 `--force-with-lease`。

4. **不要修改这两个参考仓库**（只读参考，不属于本项目）：
   - `jiuyuan233/wool-gathering` —— 目录结构模板
   - `jiuyuan233/Lottery-BiliBiliToolB-Web233` —— 飞书推送模板

5. **finalize 模式必须保持零网络、零 token。** 它每 20 分钟跑一次。
   任何采集逻辑都不许塞进 `render_readme.py` / `week_svg.py`。

---

## 2. 目录与职责

```
scripts/
  main.py             唯一入口，模式分发
  utils.py            北京时间/倒计时解析/ID/JSON/日志+脱敏
  ai_client.py        OpenAI 兼容客户端（限流+重试+JSON 抽取）
  dedup.py            合并策略：公告旧值优先，作业新值优先
  prompts.py          全部 AI 提示词 + 用户画像
  diary_collector.py  源1
  study_collector.py  源2
  notice_collector.py 源3
  schedule_parser.py  课表 .ics
  render_readme.py    README + docs/data
  week_svg.py         本周课表 SVG
  notify_feishu.js    飞书卡片推送
  readme_template.md  README 模板（{{占位符}}）
  requirements.txt    只有 requests + pycryptodome
```

详细结构见 [docs/仓库结构.md](docs/仓库结构.md)，字段定义见 [docs/数据结构.md](docs/数据结构.md)。

---

## 3. 怎么跑

```bash
pip install -r scripts/requirements.txt

python scripts/main.py selftest   # 离线自检，不需要凭据、不需要网络 ← 改完代码先跑这个
python scripts/main.py probe      # 探测各站点从当前出口 IP 是否可达
python scripts/main.py finalize   # 渲染 + 离线重算课表（不联网、不花 token）
python scripts/main.py study      # 采集学习通 + 渲染
python scripts/main.py all        # 全部
```

Actions 上：`Actions → 个人助手 → Run workflow → 选 mode`。

**改任何解析器后，务必同步更新 `main.py` 里的 `do_selftest()` 断言。**
自检是唯一能在不消耗凭据、不依赖网络的情况下验证解析逻辑的手段。

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
所以 `deadline = 抓取时刻 + 剩余时长`，`deadline_exact=False`，README 里显示 `≈`。
只有详情页补全的时间才是 `deadline_exact=True`，且合并时旧精确值优先于新估算值。

**状态语义：** `待批阅` 通常表示已提交、等老师批阅，但**互评模式下「待批阅」+ 仍在倒计时 = 用户还得去互评**，
`parse_work_rows()` 会把它升级成 `pending`。改这段前先看清楚。

---

## 5. 已知限制 / 待办

- **海外 IP 可达性：已实测通过（2026-11-13）。** Actions 出口 IP `20.118.38.104`（美国 Azure），
  `probe` 模式 10 个目标 **全部可达，不可达 0**：学习通登录/作业/课程/空间、校网公告、教务处、
  校团委、飞书均 HTTP 200；Gitee 与 AI 中转 HTTP 401（可达，只是未带凭据）。
  **不需要 self-hosted runner，也不需要国内定时任务兜底。**
  学校日后若封海外 IP，重跑 `probe` 即可确认，退路见 DEV_GUIDE 4.3。
- **Secrets 已配置完成（2026-11-13）**：`FS_WEBHOOK`、`AI_API_KEY`、`GITEE_TOKEN`、
  `CX_USERNAME`、`CX_PASSWORD` 共 5 个，Variables `AI_BASE_URL`、`AI_MODEL` 共 2 个，全部 HTTP 201。
  **能用 API 写，不必走网页**（轮换凭据时照这个做）：
  GET `/actions/secrets/public-key` 取 base64 公钥与 `key_id` → 用 libsodium `crypto_box_seal`
  加密 → PUT `/actions/secrets/{name}`，body 为 `{encrypted_value, key_id}`。
  本机 PATH 里没有 node/npm/python/gh，但搜到一个可用运行时：
  `C:/Program Files/Adobe/Adobe Creative Cloud Experience/libs/node.exe`（Node v24，自带 fetch）。
  加密库从 npm 官方 registry 取 `libsodium-wrappers`，**它依赖 `libsodium`，要一并下载**；
  解压到仓库外的临时目录充当 `node_modules`。两个坑：GNU tar 会把 `C:/...` 的冒号当远程主机，
  必须加 `--force-local`；脚本要写成真实文件再跑，`node -e` 里 `process.chdir` 不改变模块解析基准。
  明文值走环境变量传入、不落盘不进 git，用完删掉临时目录。
  已验证成功的模式：`selftest`（**34 项断言、失败 0 项**）、`probe`、`finalize`（含 git 写回）、`notice`、`study`、`diary`。
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
- **详情页精确截止时间未经样本验证。** `DETAIL_DT_RE` 是容错猜测，
  失败时静默退回倒计时估算，不会报错。日志里会打「详情页补全截止时间」，没打就是没匹配上。
- **招生就业处 `hncj.goworkla.cn` 默认关闭**（第三方建站，列表结构不同）。需要时再适配。
- **Actions 额度**：本仓库已设为 **public**，额度无限，所以保留每 20 分钟的 finalize。
  注意计费是「每个 job 向上取整到分钟」：20 分钟一次 = 2160 分钟/月，
  **私有仓库的 2000 分钟免费额度装不下**。若日后转 private，必须改用
  DEV_GUIDE 第 2 节的省额度 cron 组合（约 1500 分钟/月），否则会中途被停用。
- **Actions 60 天无 push 会自动停用 schedule。** 本项目每小时都有数据变更，正常不会触发。
  长期放假无课时可能触发，去 Actions 页面手动重新启用即可。

---

## 6. 改东西时的检查清单

- [ ] 改了日志文案 → 同步改 `notify_feishu.js` 的 `TASK_TEMPLATES` 正则（指标靠正则从日志里抠）
- [ ] 改了数据字段 → 同步改 `docs/数据结构.md` 和 `docs/js/app.js`
- [ ] 改了解析器 → 同步改 `main.py` 的 `do_selftest()`，并在 Actions 上跑一次 selftest
- [ ] 改了提交步骤 → 确认采集模式仍只 `git add data`，派生产物只由 `finalize`/`all` 提交（见第 5 节事故）
- [ ] 换了 AI 中转站或模型 → 先 `GET /v1/models` 核目录，`AI_MODEL` 照抄返回里的名字，别凭记忆写
- [ ] 遇到 `unknown` 作业状态 → dump 状态原文再补 `PENDING`/`SUBMITTED`/`DONE` 词表，不要猜
- [ ] 加了新公告源 → 只改 `data/sources.json`，先确认列表页是静态 HTML（不是 JS 渲染）
- [ ] 加了新依赖 → 更新 `requirements.txt`，并在 DEV_GUIDE 里说明为什么需要
- [ ] 任何情况下都不要把凭据写进文件

---

## 7. 文档索引

| 文件 | 内容 |
| --- | --- |
| [DEV_GUIDE.md](DEV_GUIDE.md) | 从零部署：建仓、配 Secrets、首次运行 |
| [docs/技术路径.md](docs/技术路径.md) | 每个技术选型的理由、被否决的方案、实测证据 |
| [docs/工作原理.md](docs/工作原理.md) | 一次运行的完整数据流 |
| [docs/数据结构.md](docs/数据结构.md) | 所有 JSON 字段定义 |
| [docs/仓库结构.md](docs/仓库结构.md) | 目录树 + 每个文件的职责 |
| [docs/AI提示词.md](docs/AI提示词.md) | 提示词设计说明与调参建议 |
| [docs/常见问题.md](docs/常见问题.md) | 故障排查 |
| [CHANGELOG.md](CHANGELOG.md) | 版本记录 |