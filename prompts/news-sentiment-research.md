---
name: news-sentiment-research
title: 新闻情绪研究
category: 研究分析
description: RSS 历史新闻情绪研究：事件研究、七维深度分析、融合规律优化回测、研报输出
outputs: 研报 MD + PDF
---

> 复制下方提示词到 QuantBot（DeepSeek Harness 控制台，http://<宿主>:8088）即可使用；`{占位符}` 处替换为你的实际内容。

我想研究新闻情绪对股价的规律：{研究主题，如：利好新闻后 5 日收益分布 / 情绪强度与后续涨幅关系}。

请读取 skills/news-sentiment-research/SKILL.md 并按方法论执行（数据源为 Huntly RSS 历史新闻 + FinBERT 情绪），跑对应 backtest_news_*.py 脚本，输出研报级 MD + PDF。结论必须基于数据，样本量不足时明确说明。
