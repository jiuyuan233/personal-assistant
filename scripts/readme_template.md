# 🎓 个人助手

> **本文件由 GitHub Actions 自动生成，请勿手工编辑**（改了也会被下次运行覆盖）。
> 最后更新：`{{UPDATED_AT}}` · {{WEEK_INFO}}
> Actions 每 20 分钟巡检一次（`tick`）：渲染每轮都做，学习通约每小时、日记约每 2 小时、公告约每 6 小时，早/中/晚推送各每天一次。
> 实际节奏看 [data/runtime.json](data/runtime.json)，调度逻辑见 [工作原理 §0](docs/工作原理.md)。

{{ALERT}}

---

## 🧭 目录

README 只放每天要看的动态，**所有说明性文档都挂在下面**：

- 🏆 **[学科竞赛日历 2026–2027](docs/竞赛日历.md)** — 269 项校级竞赛目录**按开始时间排序**、逐项标注来源与依据 + 已核实的报名/比赛时间、大一资格门槛、建环专业长期目标。**手工维护，不会被 Actions 覆盖。**
- 🌐 **[网页版看板](https://jiuyuan233.github.io/personal-assistant/)**（实时时间线在这里，README 里的 SVG 是静态的） · 📢 [公告全量](docs/公告.md)（{{NOTICE_COUNT}} 条，下面有精简版） · 🎯 [计划全量](docs/潜在计划.md)（{{PLAN_COUNT}} 项，下面有精简版） · 🗑️ [已剔除任务](docs/已剔除任务.md) · 📔 [名言全量](data/diary/quotes.json)
- 👤 **了解我**：[个人画像](docs/个人画像.md)（他是谁 / 关注什么 / 说话风格） · [开发习惯](docs/开发习惯.md)（16 条协作规则，接手 AI 必读）
- 🔧 **技术文档**：[技术路径](docs/技术路径.md) · [工作原理](docs/工作原理.md) · [数据结构](docs/数据结构.md) · [AI 提示词](docs/AI提示词.md) · [仓库结构](docs/仓库结构.md) · [常见问题](docs/常见问题.md)
- 🤖 **[AI 运维手册](docs/AI运维手册.md)** — **接手 AI 从这里开始**：怎么读这个仓库、怎么改、怎么验证、怎么派发、什么绝对不能碰，含逐条可执行的验收清单
- 🤝 **交接与运维**：[维护交接 AGENT.md](AGENT.md)（六条铁律） · [开发文档 DEV_GUIDE.md](DEV_GUIDE.md) · [变更日志 CHANGELOG.md](CHANGELOG.md)
- ⚠️ **[不足与风险](docs/不足与风险.md)** — 诚实的自我审计：逐条给根因位置、实测证据与**修复状态**，另列「不是缺陷别乱改」与「等用户决定」。**接手 AI 先读这份，再读技术文档。**
- 📎 **原始文件（已脱敏）**：[课表 ICS](assets/WakeUp导入课表.ics) · [竞赛目录 PDF](assets/河南城建学院2024年学科竞赛目录.pdf) — 脱敏口径与审计见 [原始文件说明](docs/原始文件.md)（抓包 HAR 已永久移除，不再收录）

---

## ⏰ 任务清单

**只列能做的任务**，共 {{PENDING_COUNT}} 项{{STATS_SUFFIX}}。全部课程按活跃度（作业发表数量）排序，组内按截止时间近 → 远；没任务的课写「无」。

{{TASKS}}

{{FILTER_NOTE}}

---

## 📅 本周课表

{{WEEK_INFO_DETAIL}}

![本周课表](docs/week.svg)

{{SCHEDULE_TABLE}}

---

## 📢 值得关注的公告

按相关度排序，只列 3 分以上，这里最多显示 {{README_NOTICE_LIMIT}} 条。
**全量 {{NOTICE_COUNT}} 条见 [docs/公告.md](docs/公告.md)** · 原始数据 [notices.json](data/notice/notices.json)。

{{NOTICES}}

---

## 🎯 潜在计划

从日记里挖出来的、还没做完的事。复选框待办由程序直接解析，隐含意图由 AI 抽取。
这里最多显示 {{README_PLAN_LIMIT}} 条，**全量 {{PLAN_COUNT}} 项见 [docs/潜在计划.md](docs/潜在计划.md)**。
标记完成请改 [plans.json](data/diary/plans.json) 里的 `state` 为 `done`（或 `cancelled`）。

{{PLANS}}

---

## 📔 我的名言

从自己写的日记里摘录的原话，一字未改。

{{QUOTES}}

---

## 📊 数据源状态

{{SOURCE_STATUS}}

---

<sub>
技术路径见 [docs/技术路径.md](docs/技术路径.md) · 维护交接见 [AGENT.md](AGENT.md) · 开发文档见 [DEV_GUIDE.md](DEV_GUIDE.md)<br/>
结构参考 <a href="https://github.com/jiuyuan233/wool-gathering">wool-gathering</a> · 推送方案参考 <a href="https://github.com/jiuyuan233/Lottery-BiliBiliToolB-Web233">Lottery-BiliBiliToolB-Web233</a>
</sub>