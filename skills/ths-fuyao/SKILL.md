---
name: ths-fuyao
description: "同花顺金融数据（Fuyao REST）— A股实时行情快照、历史K线、估值快照、财务指标、指数成分与权重、龙虎榜、涨停池/跌停池/连板天梯、热股榜、异动分析、集合竞价快照、交易日历。用户说「同花顺」「龙虎榜」「涨停池」「连板天梯」「热股榜」「今天的涨停」「异动分析」「指数成分」「估值快照」时使用。触发词：同花顺、龙虎榜、涨停池、跌停池、连板、天梯、热股、异动、集合竞价、指数成分、估值快照、财务指标"
---

> ## ⚙️ 运行环境契约（最高优先级，先于本文其余内容执行）
>
> 1. **鉴权**：请求头 `X-api-key: <THS_FUYAO_KEY>`（key 在宿主 `.env`，**禁止**把 key
>    写进日志/对话/Markdown/Git）。脚本自动从环境变量 → `/quantmind/.env` → 仓库 `.env` 解析。
> 2. **Base URL**：`https://fuyao.aicubes.cn`；统一信封 `{code, message, request_id, data}`，
>    `code=0` 成功；`2001` 未认证、`4001` 频率超限、`3002` 数据未就绪——按 code 如实降级。
> 3. **标的格式**：完整 thscode（`600519.SH`），不接受纯 `600519`。
> 4. **执行位置**：纯标准库脚本，宿主机/dsh 容器均可直跑（需外网可达 fuyao.aicubes.cn）。
> 5. **交叉验证纪律**：本源价格与通达信桥价格冲突时，以桥为交易口径、本源为校验口径，
>    两个都标注时间戳；QPS 受限（4001）时串行 + 1 秒间隔。

# ths-fuyao — 同花顺金融数据技能

## 能力地图（脚本子命令 → 端点）

| 子命令 | 端点 | 用途 |
|---|---|---|
| `snapshot` | `/api/a-share/prices/snapshot` | A股实时行情快照（支持多 thscode 逗号分隔） |
| `kline` | `/api/a-share/prices/historical` | 历史K线（thscode + start/end/limit） |
| `valuation` | `/api/a-share/valuations/snapshot` | 估值快照（PE/PB 等） |
| `fin` | `/api/a-share/financials/indicators` | 财务指标 |
| `index-snap` | `/api/a-share-index/prices/snapshot` | 指数实时快照 |
| `index-cons` | `/api/a-share-index/constituents/ths-stock-list` | 指数成分股 |
| `lhb` | `/api/a-share/special-data/dragon-tiger-list` | 龙虎榜 |
| `ztpool` | `/api/a-share/special-data/limit-up-pool` | **涨停池（盘中情绪温度核心源）** |
| `dtpool` | `/api/a-share/special-data/limit-down-pool` | 跌停池 |
| `ladder` | `/api/a-share/special-data/limit-up-ladder` | 连板天梯 |
| `hot` | `/api/a-share/special-data/hot-stock-list` | 热股榜 |
| `anomaly` | `/api/a-share/special-data/anomaly-analysis-list` | 异动分析 |
| `auction` | `/api/a-share/auction/snapshot` | 集合竞价快照（盘前 9:15-9:25 观察） |
| `calendar` | `/api/a-share/calendar/trading-days` | 交易日历 |
| `raw` | 任意 `/api/**` | 通用透传（路径 + query JSON） |

## 与本系统其他数据源的分工

- **涨停池/连板天梯**：补全「盘中情绪温度」缺的全市场涨跌停口径——
  `ztpool` 数量 + `ladder` 高度分布 = 情绪周期位置（启动/发酵/高潮/退潮）。
  market-sentiment-dashboard 报告中引用本源时标注「同花顺 Fuyao」。
- **行情快照**：与通达信桥互为校验（桥为交易口径）。
- **龙虎榜/热股/异动**：盘后复盘与次日预案的主力行为证据。

## 参数与响应

- 查询参数以各端点文档为准（`/tmp/fuyao_llms.txt` 为全量文档快照；
  也可 `raw` 透传探索）。常用：`thscode`、`start`、`end`、`limit`、`trade_date`。
- 响应业务数据在 `data.item`（数组）或 `data` 字段；时间戳毫秒（上海时区）。

## 纪律

- Key 不落日志/对话/报告；脚本打印时自动脱敏。
- 涨停池等池类数据有盘中时点性，引用必须带抓取时间。
- 付费额度有限：单轮分析只拉当前任务所需端点，不做全市场枚举。