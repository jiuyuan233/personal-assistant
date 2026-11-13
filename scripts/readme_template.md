# 🎓 个人助手

> **本文件由 GitHub Actions 自动生成，请勿手工编辑**（改了也会被下次运行覆盖）。
> 最后更新：`{{UPDATED_AT}}` · {{WEEK_INFO}}
> 渲染每 20 分钟一次，学习通每小时一次，公告每天 3 次，日记每 20 分钟增量检查。

{{ALERT}}

---

## 🧭 目录

README 只放每天要看的动态，其余内容都在独立文档里：

- 🏆 **[学科竞赛日历 2026–2027](docs/竞赛日历.md)** — 269 项校级竞赛目录 + 已核实的报名/比赛时间、大一资格门槛、建环专业长期目标。**手工维护，不会被 Actions 覆盖。**
- 🌐 [网页版看板](docs/index.html) · 📢 [公告全量数据](data/notice/notices.json) · 🎯 [潜在计划](data/diary/plans.json) · 📔 [名言](data/diary/quotes.json)
- 🔧 [技术路径](docs/技术路径.md) · [工作原理](docs/工作原理.md) · [数据结构](docs/数据结构.md) · [AI 提示词](docs/AI提示词.md) · [常见问题](docs/常见问题.md) · [仓库结构](docs/仓库结构.md)
- 🤝 [维护交接 AGENT.md](AGENT.md) · [开发文档 DEV_GUIDE.md](DEV_GUIDE.md) · [变更日志 CHANGELOG.md](CHANGELOG.md)
- 📎 **原始文件（已脱敏）**：[课表 ICS](assets/WakeUp导入课表.ics) · [竞赛目录 PDF](assets/河南城建学院2024年学科竞赛目录.pdf) · [学习通抓包 HAR](assets/抓包-学习通-脱敏.har) · [空间页抓包 HAR](assets/抓包-空间页-脱敏.har) — 脱敏口径见 [原始文件说明](docs/原始文件.md)

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

按相关度排序，只列 3 分以上。完整列表见 [data/notice/notices.json](data/notice/notices.json)。

{{NOTICES}}

---

## 🎯 潜在计划

从日记里挖出来的、还没做完的事。复选框待办由程序直接解析，隐含意图由 AI 抽取。
标记完成请改 [data/diary/plans.json](data/diary/plans.json) 里的 `state` 为 `done`。

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