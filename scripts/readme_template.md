# 🎓 个人助手

> **本文件由 GitHub Actions 自动生成，请勿手工编辑**（改了也会被下次运行覆盖）。
> 最后更新：`{{UPDATED_AT}}` · {{WEEK_INFO}}
> 渲染每 20 分钟一次，学习通每小时一次，公告每天 3 次，日记每 20 分钟增量检查。

{{ALERT}}

---

## ⏰ 任务清单

**按截止时间从近到远排序。** 共 {{PENDING_COUNT}} 项待完成{{STATS_SUFFIX}}。

{{TASKS}}

{{DONE_BLOCK}}

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