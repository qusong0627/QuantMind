---
name: tdx-live-trading
title: TDX 实盘监控与交易
category: 交易
description: 通达信实盘链路：实时推理、自动买卖、挂单/撤单、交易记录、持仓、桥健康巡检
outputs: 链路状态/交易结果
---

> 复制下方提示词到 QuantBot（DeepSeek Harness 控制台，http://<宿主>:8088）即可使用；`{占位符}` 处替换为你的实际内容。

我需要处理 TDX 实盘链路：{查看链路健康状态 / 查今日交易记录与持仓 / 配置实时推理 / 下单、撤单操作}。

请读取 skills/tdx-live-trading/SKILL.md：先跑 tdx_live_status.py 状态快照并按异常判定表巡检；涉及实盘下单/撤单的操作必须先列出订单要素经我确认。实盘资金安全第一，任何异常先停止操作并报告。
