---
name: stock-research
title: 个股深度研究（多Agent）
category: 研究分析
description: 5 分析师并行（技术/新闻/资金情绪/基本面/市场）→ 多空辩论 → 研究经理汇总 → 研报 PDF
outputs: data/reports/stock_research/ + data/reports/trading_agents/ PDF
---

> 复制下方提示词到 QuantBot（DeepSeek Harness 控制台，http://<宿主>:8088）即可使用；`{占位符}` 处替换为你的实际内容。

请对 {股票名称及代码，如：贵州茅台 600519} 做一次个股深度研究。

请读取 skills/stock-research/SKILL.md 并严格按多 Agent 流程执行：跑 research_data.py 取数 → 5 个分析师并行（用 prompts/ 下的角色提示词）→ 多空辩论 → 研究经理汇总 → MD 转 PDF → 落盘 data/reports/trading_agents/{市场}/{股票名}/（平台股票报告页可见）。最后给我结论速览：核心逻辑、多空关键分歧、风险点。
