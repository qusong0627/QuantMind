---
name: daily-review
title: A股每日复盘
category: 研究分析
description: 盘后专业复盘：指数、涨停梯队、行业轮动、资金面、L2 微观结构、推理信号复盘、次日方向研判
outputs: data/reports/daily_review/ + PDF 报告
---

> 复制下方提示词到 QuantBot（DeepSeek Harness 控制台，http://<宿主>:8088）即可使用；`{占位符}` 处替换为你的实际内容。

今天是 {日期，如 2026-08-29}，请给我做 A 股每日复盘。

请读取 skills/daily-review/SKILL.md 并严格按照其固定流程执行（取数脚本 → 按模板写复盘 → 转 PDF → 落盘）：报告以 facts 数据为准，facts 里没有的数字不要写。最后给我 200 字以内的速览：市场方向、最强板块、明日关键位。
