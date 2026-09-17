---
name: simulation-trading
title: 模拟交易
category: 交易
description: 模拟盘下单买卖、持仓管理、成交查询、资金快照
outputs: 交易结果
---

> 复制下方提示词到 QuantBot（DeepSeek Harness 控制台，http://<宿主>:8088）即可使用；`{占位符}` 处替换为你的实际内容。

请帮我操作模拟交易：{买入/卖出 某股票及数量 / 查持仓 / 查账户与资金 / 查成交记录}。

请读取 skills/simulation-trading/SKILL.md，通过 /api/v1/simulation/* 接口执行。下单前把订单要素（代码、方向、数量、价格）列给我确认后再提交；完成后返回成交结果与最新持仓。
