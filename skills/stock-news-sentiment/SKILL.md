---
name: stock-news-sentiment
description: "个股实时新闻与情绪分析 — 拉取目标股票（或自选列表）最近新闻（QuantDB PG news_article_enrichment 新闻库 / RSS 实时源），输出时间线 + 每条情感标注（正面/负面/中性，含置信度） + 当日情绪聚合与消息面结论。用户问「XX 有什么消息」「今天 XX 的新闻」「这票消息面怎么样」「新闻情绪」时使用。触发词：个股新闻、新闻情绪、消息面、最新消息、舆情、利好利空、新闻时间线"
---

> ## ⚙️ 运行环境契约（最高优先级，先于本文其余内容执行）
>
> 1. **新闻库**：QuantDB PG `news_article_enrichment`（symbol 前缀格式如 `SH601138`；含发布时间/标题/正文/情感字段需先查列）。PG 凭据在 quantmind 容器 runtime 配置（QM_PG_*）。
> 2. **执行位置**：带 pandas/duckdb/psycopg2 的查询在 quantmind 容器内跑（`docker cp` + `docker exec -w /app quantmind python3`）。
> 3. **实时 RSS**：新闻有滞后（库为批处理入库）时补充实时源：盘中关注“实时行情”类去重——新闻里出现价格一律以 [realtime-quotes-tdx](realtime-quotes-tdx) 验证，**禁止用新闻标题里的数字当现价**。
> 4. **情绪引擎**：quantmind 已有 FinBERT 工具链（`backend/scripts/download_finbert.py`、`import_sentiment_lexicon.py`）；不可用时由 AI 对每条新闻做结构化标注（正/负/中性+一句依据），并注明「AI 标注」。
> 5. 报告落盘 `/data/reports/trading_agents/新闻情绪/{股票名}/`；纯闲聊不需要落盘。

# stock-news-sentiment — 个股实时新闻与情绪

## 1. 取数路径

1. **QuantDB 新闻表**（首选）：
   ```sql
   SELECT * FROM news_article_enrichment
   WHERE symbol LIKE '%601138%' AND publish_time >= now() - interval '7 days'
   ORDER BY publish_time DESC LIMIT 50;
   ```
   （列名以 `\d news_article_enrichment` 实测为准；若无 sentiment 列，用第 2 步打标。）
2. **情绪标注**：
   - 容器内有 FinBERT → `docker exec -w /app quantmind python3 backend/scripts/finbert_sentiment.py <文本文件>`（先确认脚本名；没有就用 download_finbert.py 初始化模型再标）；
   - 否则 AI 逐条标注：`[正面|负面|中性] 置信度(高/中/低) 依据一句话`。
3. **实时补源**（库滞后时）：先问用户是否需要实时抓取；需要则按环境可用的 RSS/搜索通道抓当日标题，并标记「实时源，未入库」。

## 2. 输出结构

```
# {股票名} 近7日新闻情绪  （数据截至 北京 {时间}，来源：QuantDB/{实时源}，标注：FinBERT/AI）
| 时间 | 标题(截断) | 情绪 | 置信 | 依据 |
|---|---|---|---|---|
情绪聚合：正面 X 条 / 负面 Y 条 / 中性 Z 条；近3日情绪变化：↗/→/↘
消息面结论：2-4 句，只基于上述新闻事实，禁止编造未列出的消息。
```

## 3. 纪律

- 新闻里的价格/涨跌幅不是实时价：涉及价格判断前调用 realtime-quotes-tdx 验证并注明当时价。
- 情感标注必须可回溯（依据=新闻里哪句话），不给空泛情绪。
- 聚合结论要区分「利空出尽 vs 持续利空」这类程度判断并给出你的推断理由，但标注为推断。

## 4. 脚本

`scripts/stock_news.py <CODE.SH> [天数]`：复制进 quantmind 容器运行——自动探测 news table 存在性与列结构，输出最近 N 天新闻原始行（JSON）；情绪标注由上文流程完成。无法连库时脚本打印结构化空结果并提示实时源路径。