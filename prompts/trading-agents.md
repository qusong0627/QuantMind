---
name: trading-agents
title: 个股投研分析（智能体自主版）
category: 研究分析
description: 本地数据 → 多空子代理辩论 → 综合研判 → PDF 报告，不依赖容器投研管线
outputs: data/reports/trading_agents/ PDF 报告
---

> 复制下方提示词到 QuantBot（DeepSeek Harness 控制台，http://<宿主>:8088）即可使用；`{占位符}` 处替换为你的实际内容。

请用智能体自主模式深度分析 {股票名称及代码}。

请读取 skills/trading-agents/SKILL.md 并按其流程执行：拉取本地数据（特征/风险评分/推理分数/新闻）→ 组织多空子代理辩论 → 综合研判 → 生成 MD 报告 → 转 PDF 落盘 data/reports/trading_agents/{市场}/{股票名}/。最后给我投资论点摘要和主要风险。
