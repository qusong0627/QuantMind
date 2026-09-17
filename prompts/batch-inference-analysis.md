---
name: batch-inference-analysis
title: 批量推理信号分析
category: 研究分析
description: 每日推理信号解读：行业轮动、个股分数区间、负分参考、市场状态判断
outputs: 信号解读报告
---

> 复制下方提示词到 QuantBot（DeepSeek Harness 控制台，http://<宿主>:8088）即可使用；`{占位符}` 处替换为你的实际内容。

请分析最新一批模型推理信号：{指定日期或批次，留空则取最新}。

请读取 skills/batch-inference-analysis/SKILL.md 并按其方法论执行：读取推理信号数据 → 分析行业分布与轮动 → 个股分数区间解读 → 负分参考。最后给我：市场状态判断、信号最集中的 3 个行业、Top 5 高分股与风险提示。
