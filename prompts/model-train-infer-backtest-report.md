---
name: model-train-infer-backtest-report
title: 训练-推理-回测-报告全流程
category: 策略·因子·模型·回测
description: T+N 周期模型训练（13 种模型）→ 批量推理 → 组合回测（阈值+大盘MA+止损）→ 研报输出
outputs: 研报 MD + PDF
---

> 复制下方提示词到 QuantBot（DeepSeek Harness 控制台，http://<宿主>:8088）即可使用；`{占位符}` 处替换为你的实际内容。

请走完「训练 → 推理 → 回测 → 报告」全流程：模型类型 {lightgbm/xgboost/lstm/transformer 等 13 选 1}，周期 T+{N}，市场 {CN/HK/US/CRYPTO/FUTURES}。

请读取 skills/model-train-infer-backtest-report/SKILL.md 并按流程执行：提交训练 → 等待完成 → 批量推理 → 自定义组合回测（分数阈值 + 大盘 MA 过滤{+止损}）→ 导出研报 MD+PDF。训练耗时长，分段汇报；最后给我 T+N 周期对比与效益分析结论。
