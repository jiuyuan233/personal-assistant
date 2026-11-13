# 🎓 个人助手

> **本文件由 GitHub Actions 自动生成，请勿手工编辑**（改了也会被下次运行覆盖）。
> 最后更新：`{{UPDATED_AT}}` · {{WEEK_INFO}}
> 渲染每 20 分钟一次，学习通每小时一次，公告每天 3 次，日记每 20 分钟增量检查。

{{ALERT}}

---

## 🧭 目录

README 只放每天要看的动态，**所有说明性文档都挂在下面**：

- 🏆 **[学科竞赛日历 2026–2027](docs/竞赛日历.md)** — 269 项校级竞赛目录**按开始时间排序**、逐项标注来源与依据 + 已核实的报名/比赛时间、大一资格门槛、建环专业长期目标。**手工维护，不会被 Actions 覆盖。**
- 🌐 [网页版看板](docs/index.html) · 📢 [值得关注的公告](docs/公告.md)（{{NOTICE_COUNT}} 条） · 🎯 [潜在计划](docs/潜在计划.md)（{{PLAN_COUNT}} 项） · 🗑️ [已剔除任务](docs/已剔除任务.md) · 📔 [名言全量](data/diary/quotes.json)
- 👤 **了解我**：[个人画像](docs/个人画像.md)（他是谁 / 关注什么 / 说话风格） · [开发习惯](docs/开发习惯.md)（16 条协作规则，接手 AI 必读）
- 🔧 **技术文档**：[技术路径](docs/技术路径.md) · [工作原理](docs/工作原理.md) · [数据结构](docs/数据结构.md) · [AI 提示词](docs/AI提示词.md) · [仓库结构](docs/仓库结构.md) · [常见问题](docs/常见问题.md)
- 🤝 **交接与运维**：[维护交接 AGENT.md](AGENT.md) · [开发文档 DEV_GUIDE.md](DEV_GUIDE.md) · [变更日志 CHANGELOG.md](CHANGELOG.md)
- ⚠️ **[不足与风险](docs/不足与风险.md)** — 诚实的自我审计：31 条已知问题（含学习通「假成功」、公开仓库里的隐私欠账、**定时触发极不稳定，schedule 只零星命中**），逐条给根因位置与实测证据，另列「不是缺陷别乱改」与「等用户决定」。**接手 AI 先读这份，再读技术文档。**
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