---
name: market-analysis
title: 市场分析报告
category: 研究分析
description: 大盘快照版市场分析：核心指数、广度情绪、行业热力图、板块资金流、个股主力 Top20
outputs: data/reports/market_analysis/ + PDF 报告
---

> 复制下方提示词到 QuantBot（DeepSeek Harness 控制台，http://<宿主>:8088）即可使用；`{占位符}` 处替换为你的实际内容。

请给我做一份今日市场分析报告（大盘快照版）。

请读取 skills/market-analysis/SKILL.md 并按其流程执行：跑取数脚本（market_analysis.py）→ 基于 facts 撰写解读 → Markdown → PDF（研报风）→ 落盘 data/reports/market_analysis/。最后给我核心结论速览：大盘状态、资金主线、值得关注的 3 个板块。
