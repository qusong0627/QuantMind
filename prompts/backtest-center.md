---
name: backtest-center
title: 回测中心
category: 策略·因子·模型·回测
description: Qlib 回测：快速/专家模式、向量化极速回测、策略对比、参数优化、全局股票池 pool_id
outputs: 回测结果报告
---

> 复制下方提示词到 QuantBot（DeepSeek Harness 控制台，http://<宿主>:8088）即可使用；`{占位符}` 处替换为你的实际内容。

我需要回测：{快速回测 / 专家模式 / 对比策略 / 参数优化 / 查历史}，市场 {CN/HK/US/CRYPTO/FUTURES}，股票池 {csi300 / pool:自定义池code / 留空全市场}。

请读取 skills/backtest-center/SKILL.md 按对应模式操作。选股范围优先用 pool_id（如 pool:csi1000），与前端全局股票池一致；纯 TopK 策略可试 use_vectorized=true 极速引擎（不安全策略会自动退回 step 模式）。按市场切换 provider_uri/基准。结果给我年化收益、最大回撤、夏普比率，并说明结论是否稳健。
