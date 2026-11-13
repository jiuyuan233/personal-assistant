# DEV_GUIDE.md — 从零部署

按顺序做，全程约 15 分钟。**第 4 步之前不要开启定时触发**，先手动验证。

---

## 1. 前置条件

- 一个 GitHub 账号（本项目用 `jiuyuan233`）
- 飞书群 + 自定义机器人 webhook
- ChatAnyWhere（或任意 OpenAI 协议中转站）的 API Key
- Gitee 私人令牌（只读 `obsidion-gitee` 仓库即可）
- 学习通账号密码（学校统一认证的那套，手机号登录）

本地**不需要**装 Python 或 Node —— 所有执行都在 Actions 上。
想本地调试再装 Python 3.11+ 和 Node 20+。

---

## 2. 建仓

```bash
cd personal-assistant
git init -b main
git add .
git commit -m "feat: 个人助手首版"
```

在 GitHub 上新建空仓库 `personal-assistant`（**不要**勾选生成 README），然后：

```bash
git remote add origin https://github.com/jiuyuan233/personal-assistant.git
git push -u origin main
```

> **公开还是私有？先算清额度，这是个硬约束。**
>
> Actions 按 **每个 job 向上取整到分钟** 计费。一次 finalize 就算只跑 30 秒，也计 1 分钟。
> 每 20 分钟一次 = 72 次/天 = **2160 次/月 = 至少 2160 分钟**，
> 而私有仓库免费额度只有 **2000 分钟/月**。也就是说：
> **private + GitHub 托管 runner + 20 分钟节奏，数学上不可能不超额。**
>
> | 方案 | 额度 | 20 分钟节奏 | 隐私 | 额外条件 |
> | --- | --- | --- | --- | --- |
> | **public 仓库** | 无限 | ✅ 保留 | ❌ 日记摘录/作业标题公开 | 无 |
> | **private + self-hosted runner** | 无限 | ✅ 保留 | ✅ | 一台常开的国内机器（**顺带解决海外 IP 被封的问题**） |
> | private + GitHub 托管 | 2000 分钟 | ❌ 必须放宽 | ✅ | 用下面的省额度 cron |
>
> 默认配置按「public 或 self-hosted」设计。README 里有课程名、作业标题、日记摘录和名言，
> 公开前请自己权衡。**飞书是主要查看入口，README 只是存档**，所以 private 也不影响日常体验。
>
> **省额度做法（private + GitHub 托管）**：现在只有**一条** cron，所以省额度只有两个旋钮，
> **不要再往 yml 里堆 cron**（selftest 有断言守着「只有一条 cron」，加了会红）：
>
> **旋钮 1 —— 拉长巡检间隔**，改 `assistant.yml` 里那一行：
>
> ```yaml
> - cron: '*/30 * * * *'   # 48 轮/天，每轮约 1 分钟 ≈ 1440 分钟/月
> ```
>
> 注意 `main.TICK_SLACK_MIN = 5` 是给 Actions 延迟留的余量；间隔拉到 30 分钟以上时
> 各采集器的实际节奏会被巡检粒度取整（study 设 60 分钟 → 实际 60 或 90 分钟）。
>
> **旋钮 2 —— 拉长采集间隔**，改 `scripts/main.py`：
>
> ```python
> TICK_INTERVALS = {"study": 180, "diary": 360, "notice": 720}   # 分钟
> ```
>
> 这只减少**联网轮数**，不减少 job 数 —— 每轮 tick 仍要跑渲染，所以省额度主要靠旋钮 1。
> 旋钮 2 的价值是省 AI token 和降低账号风控概率。
>
> **三时段推送不用单独保**：它们由 `due_pushes()` 在 tick 里判断，
> `PUSH_CATCHUP_HOURS = 3` 意味着巡检间隔只要 ≤ 3 小时就一定能补上。
> 间隔拉到 `*/30` 完全不影响早/中/晚消息。
>
> **凌晨写回也不用单独保**：`do_tick()` 发现「在窗口内且今天还没写回」就强制加跑 diary，
> 窗口 `00:00-06:00` 有 6 小时宽。只要 cron 间隔 ≤ 6 小时，写回就不会被饿死。
> **但间隔一旦超过窗口宽度（例如 `0 */8 * * *`），写回可能整天错过** —— 这是唯一的硬下限。
>
> 放宽节奏的代价：**README 每轮 tick 都刷新**，所以巡检间隔就是 README 的新鲜度上限。
> 真正的损失是倒计时数字最多旧一个巡检周期，以及「24 小时内截止」的提醒粒度变粗。
> 查实际用量：Settings → Billing → Actions。

---

## 3. 配置 Secrets 和 Variables

仓库 → **Settings → Secrets and variables → Actions**。

> ✅ **本仓库的 Secrets 与 Variables 已于 2026-11-13 走 API 配置完成。**
> 下面保留清单与做法，供日后轮换凭据或重建仓库时使用。
> **⚠️ 开发期在聊天里贴过的凭据一律视为已泄露，请全部轮换后再填进 Secrets。**

### Secrets（敏感，必须）

| 名称 | 值 | 说明 |
| --- | --- | --- |
| `FS_WEBHOOK` | `https://open.feishu.cn/open-apis/bot/v2/hook/xxxx` | 飞书机器人完整 webhook。不配则静默跳过推送，不影响主流程 |
| `AI_API_KEY` | `sk-...` | 中转站 key。不配则公告退回关键词打分、日记只走复选框通道、推送走确定性降级 |
| `GITEE_TOKEN` | Gitee 私人令牌 | **需要 `projects` 读写权限** —— 潜在计划要写回日记仓库。只给只读则写回失败（只 WARN，采集照常） |
| `CX_USERNAME` | 学习通手机号 | 不配则跳过学习通采集，README 出 ⚠️ 未同步告警 |
| `CX_PASSWORD` | 学习通密码 | |
| `DIARY_EXCLUDE` | 逗号分隔的目录前缀 | **可选但强烈建议**。日记里不想被采集/公开的目录，例如 `私人,情绪/崩溃`。前缀匹配 `Tasks` 下的相对路径。仓库里只出现名字，值不入库 |

**`DIARY_EXCLUDE` 的两段作用**（`diary_collector.excluded_prefixes()` / `is_excluded()` / `drop_excluded()`）：

1. **采集时跳过**：命中的文件根本不读、不喂 AI，所以隐私内容不会进入 `data/`
2. **入库后清理**：黑名单生效**之前**已经采进来的名言/计划/归档条目，下一轮 diary 会被删掉
   （日志打「清理黑名单生效前入库的私人条目：名言 N 条、计划 N 条、归档 N 条」）

**它清不掉 git 历史。** 已经提交过的隐私内容仍在历史里，要彻底抹除得改写历史（见 [不足与风险 §7.1](docs/不足与风险.md)）。
所以**先配黑名单，再跑第一次 diary**，顺序反了就要多做一次历史清理。

### Variables（非敏感，可选）

| 名称 | 默认值 | 说明 |
| --- | --- | --- |
| `AI_BASE_URL` | `https://api.chatanywhere.org/v1` | 换中转站时改这里 |
| `AI_MODEL` | `deepseek-v3.2` | 换模型时改这里，名字必须照抄该站 `/v1/models` 目录（写成 `deepseek-v3-2` 会每次 403） |

不配 Variables 就用 workflow 里写死的默认值。

### 用 API 轮换（已实测可行，不必开网页）

1. `GET /repos/{owner}/{repo}/actions/secrets/public-key` → 取 `key`（base64 公钥）与 `key_id`
2. 用 libsodium 的 `crypto_box_seal(明文, 公钥)` 加密，结果转 base64
3. `PUT /repos/{owner}/{repo}/actions/secrets/{名称}`，body 为 `{"encrypted_value": "...", "key_id": "..."}`

Variables 不用加密：`POST /actions/variables`，已存在则 `PATCH /actions/variables/{名称}`。

本机 PATH 里没有 node/npm/python/gh 时的可行路径，以及 GNU tar 冒号、`node -e` 模块解析这两个坑，
见 [AGENT.md 第 5 节](AGENT.md)。**明文值只走环境变量，不要写进仓库里的任何文件。**

> ⚠️ **开发期间在聊天里贴过的所有凭据都应视为已泄露，请全部轮换后再填进 Secrets。**
> 包括 Gitee 令牌、GitHub PAT、两个学校密码、两个 AI key。
> GitHub PAT 建议换成 fine-grained token，只授权这一个仓库的 contents 读写。

---

## 4. 手动验证（按这个顺序，别跳）

仓库 → **Actions** 标签页。

### 4.1 飞书通路

左侧选 **飞书推送测试** → Run workflow → 默认参数 → Run。
飞书群应收到一张绿色卡片。收不到就检查 `FS_WEBHOOK`。

### 4.2 解析器自检

左侧选 **个人助手** → Run workflow → mode 选 **`selftest`** → Run。
这一步**不联网、不用凭据**，只验证解析逻辑。日志里应全是 ✅。
有 ❌ 就先修代码，别往下走。

### 4.3 海外连通性探测 ← 关键

mode 选 **`probe`** → Run。看日志里的出口 IP 和每个站点的可达性。

**2026-11-13 已在真实 runner 上跑过，结果：可达 10 / 不可达 0。**
出口 IP `20.118.38.104`（美国 Azure），学习通登录/作业/课程/空间四个端点全部 HTTP 200，
校网公告、教务处、校团委、飞书 200，Gitee 与 AI 中转 401（可达，只是没带凭据）。
**架构级风险已排除，这一步现在只是回归检查**，直接继续 4.4 即可。

- **学习通三个域名都可达** → 继续 4.4
- **学习通不可达（超时/连接重置）** → 说明学校后来改了策略，见下方「4.3 失败怎么办」
- 公告站和 Gitee 可达 → 源 1、源 3 没问题

### 4.4 单源采集

依次手动跑 `schedule` → `notice` → `diary` → `study`，每跑一次看日志和 README 变化。
`study` 最容易出问题，重点看：

```
学习通登录成功
课程总数：28
扫描课程：28，失败：0
待完成：N
```

如果「失败」数很高，多半是 token 或参数问题，对照 [AGENT.md 第 4 节](AGENT.md)。

### 4.5 开启定时

全部验证通过后，定时触发会自动生效（workflow 里的 `schedule` 不需要额外开关）。
第一次 cron 触发可能要等几分钟到几十分钟 —— GitHub 的 schedule 不保证准时。

---

## 4.3 失败怎么办（学习通连不上）

> **当前不需要这一节。** 2026-11-13 实测 Actions 出口 IP `20.118.38.104`（美国 Azure）
> 对学习通四个端点全部 HTTP 200。以下内容是**日后学校封境外 IP 时的预案**，先留着。

开发期的实测都来自国内出口 IP，所以当初担心学校对境外 IP 限流或拒连。
真遇到时三条路：

1. **国内 self-hosted runner**（推荐）
   找一台国内常开的机器（云服务器/宿舍电脑），装 GitHub self-hosted runner，
   把 `assistant.yml` 的 `runs-on: ubuntu-latest` 改成 `runs-on: self-hosted`。
   注意 self-hosted runner 跑私有仓库有安全考量，别在不受信的机器上用。
   本仓库是 public，**公开仓库上挂 self-hosted runner 更危险**（任何人提 PR 都能在你机器上跑代码），
   要用就先把仓库转 private，并关掉 fork PR 触发。

2. **拆分**：`study` 模式挪到国内的定时任务（青龙面板/云函数），
   结果写回本仓库的 `data/study/tasks.json`；Actions 只跑 finalize/notice/diary。

3. **放弃自动登录**：改成手动导出作业列表。不推荐，违背项目初衷。

**源 1（Gitee）和源 3（公开公告）不受这个问题影响**，可以先让它们跑起来。

---

## 5. 启用网页看板（可选）

仓库 → Settings → Pages → Source 选 **Deploy from a branch** →
Branch 选 `main`，目录选 **`/docs`** → Save。

几分钟后访问 `https://jiuyuan233.github.io/personal-assistant/`。
私有仓库的 Pages 需要 GitHub Pro；没有就只看 README 和飞书。

---

## 6. 日常维护

| 想做什么 | 改哪里 |
| --- | --- |
| 加/删公告源 | `data/sources.json` 的 `notice` 数组 |
| 调公告筛选词 | `data/sources.json` 的 `keywords` |
| 标记某个计划已完成 | `data/diary/plans.json` 里对应项的 `state` 改成 `done` |
| 换课表 | 替换 `assets/WakeUp导入课表.ics`，跑一次 `schedule` |
| 换 AI 模型 | 仓库 Variables 的 `AI_MODEL` |
| 改推送频率 | `.github/workflows/assistant.yml` 的 `cron` |
| 改 README 版式 | `scripts/readme_template.md` |

**改完任何代码，先跑 `selftest` 模式。**

---

## 7. 本地调试（可选）

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r scripts/requirements.txt

export AI_API_KEY=... GITEE_TOKEN=... CX_USERNAME=... CX_PASSWORD=...
export TASK_LOG_PATH=./task-output.log

python scripts/main.py selftest
python scripts/main.py study
```

Windows 上 `zoneinfo` 可能缺 tzdata，`utils.py` 会自动退回固定 +08:00 偏移
（中国自 1991 年起无夏令时，等价）。不需要额外装包。

---

## 8. 卸载 / 暂停

- **临时暂停**：Actions → 个人助手 → 右上 `...` → Disable workflow
- **彻底删除**：删仓库即可。本项目不写任何外部系统，只读三个数据源，
  唯一的副作用是往自己仓库提交和往自己飞书群发消息。