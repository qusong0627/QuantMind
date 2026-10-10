---
name: news-sentiment-research
title: 新闻情绪研究
category: 研究分析
description: RSS 历史新闻情绪研究：事件研究、七维深度分析、融合规律优化回测、研报输出
outputs: 研报 MD + PDF
---

> 复制下方提示词到 QuantBot（DeepSeek Harness 控制台，http://<宿主>:8088）即可使用；`{占位符}` 处替换为你的实际内容。

我想研究新闻情绪对股价的规律：{研究主题，如：利好新闻后 5 日收益分布 / 情绪强度与后续涨幅关系}。

请读取 skills/news-sentiment-research/SKILL.md 并按方法论执行（数据源为 Huntly RSS 历史新闻；情绪默认走金融词典法，启用 FinBERT 后为「0.6 字典 + 0.4 FinBERT（置信度≥0.55）」融合），跑对应 backtest_news_*.py 脚本，输出研报级 MD + PDF。注意：SKILL.md 中的实测数字全部基于 2026-08-20 数据快照（98 个交易日、2026-03~08 下行/震荡窗口），是样本内结果——复用前先核对你当前的数据窗口并复测；结论必须基于数据，样本量不足时明确说明。
