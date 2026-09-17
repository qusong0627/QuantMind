---
name: stock-picks
title: 每日股票推荐
category: 研究分析
description: 复盘后多维打分选股：L2 微观结构 + 模型融合分 + 仓位信号 + 板块强度 + 新闻情绪
outputs: data/reports/stock_picks/ + PDF 报告
---

> 复制下方提示词到 QuantBot（DeepSeek Harness 控制台，http://<宿主>:8088）即可使用；`{占位符}` 处替换为你的实际内容。

请基于最新复盘数据给我做今日股票推荐。

请读取 skills/stock-picks/SKILL.md 并按其流程执行：先确认 data/reports/daily_review/ 有当日 stats（没有先跑复盘取数）→ 跑 pick_candidates.py 多维打分 → Top N 个股深度分析 → 综合报告 → PDF 落盘 data/reports/stock_picks/。最后给我候选榜和每只股票的一句入选理由。
