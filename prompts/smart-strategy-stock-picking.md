---
name: smart-strategy-stock-picking
title: 条件选股
category: 研究分析
description: QuantDB 条件选股：自然语言/结构化/DSL 三种方式，可选登记为全局股票池
outputs: 股票池列表
---

> 复制下方提示词到 QuantBot（DeepSeek Harness 控制台，http://<宿主>:8088）即可使用；`{占位符}` 处替换为你的实际内容。

请帮我选股，条件：{自然语言条件，如：市值 100-500 亿、PE < 30、近 20 日主力资金净流入、行业为半导体}。

请读取 skills/smart-strategy-stock-picking/SKILL.md：优先 parse-text 或 query-pool 执行 DSL 筛选。结果按市值/涨跌幅排序给表格，注明单位与数据截止日期；超出 50 只只展示前 50。若用户要求持久化股票池，提示在管理后台「全局股票池」保存，或通过 legacy 保存桥接到 v2（pool:code）供回测/训练/推理使用。
