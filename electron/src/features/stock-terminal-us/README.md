# 美股个股终端（Stock Terminal US）

顶部搜索 → 左侧 K 线（含推理分底部副图）→ 右侧 9 个详情面板。
入口：顶部市场切换器选「美股」→ 侧边栏「个股终端」。

页面骨架、K 线卡、搜索框都来自 `features/stock-terminal-shared/`；
本目录只放**美股特有**的东西：主题、数据源配置、详情体。改共享组件一处，三市场同时生效。

## 目录

```
pages/StockTerminalPage.tsx          页面（Provider + Shell + 详情体，约 40 行）
components/UsDetailBody.tsx          9 个详情面板
services/stockTerminalService.ts     服务子类：端点前缀 /detail / 列表与 profile 字段映射 / 拆股标记
types.ts                             与后端 /detail 响应严格对齐的类型
```

## ⚠️ 口径（改代码前必读）

| 项 | 事实 | 对界面的影响 |
|----|------|-------------|
| 价格 | yfinance `auto_adjust=False` 的**原始未复权价** | 复权切换**只有「不复权」**；拆股日用橙色竖线标注，跳变是正常的 |
| 成交额 | `amount` 是**美元原始值**（不是 A 股的「股/万元」） | 展示按美元口径格式化 |
| 标的池 | 标普 500 + 纳指补充，**约 500 只，非全市场** | 页面标注；含少量退市/并购残留（`has_quote=false`） |
| 估值 | `f10` 快照；**没有 PS / EV / Forward PE**；约 420/484 只有 PE | 估值面板只展示库里真有的字段 |
| 财务 | **只有年报**（每标的 5 个财年），无季报 | 不做季度趋势 / TTM |
| 机构持仓 | 13F 口径，披露滞后约一季 | 面板已标注披露日 |
| 内部人 | SEC Form 4；`Transaction` 列恒空，类型由 `Text` 前缀解析 | 后端已解析成 buy/sell/other |
| 资讯 | PG `news_article_enrichment` 按 ticker 匹配（带情绪），Huntly 标题匹配兜底 | 中文资讯 |
| 推理分 | 取决于是否训练过美股模型 | 无分数时安静降级，不报错 |

**不做的面板**：L2 微观因子（A 股独有）、CCASS/南向（港股独有）、分钟线（QuantUS 无）、
期权 / VIX（只有 call + 单快照）、指数均线卡（后端接口是 A 股专用）。

## 服务子类为什么存在

后端 `/list`、`/profile` 返回的是美股口径的扁平字段（`display_name` / `cn_name` / `market_cap_yi`），
而共享的搜索框与页面骨架吃的是通用 `StockListItem` / `StockProfile`。
映射放在**服务边界**（`UsStockTerminalService`），共享组件因此不需要任何市场分支 ——
这是三市场能共用一套组件的关键约定。

## 验证

```bash
cd electron && QM_MARKETS=US node tests/stock-terminal.e2e.mjs
```

后端：`docker exec -w /app/backend quantmind python -m pytest tests/test_us_stock_terminal.py -q --no-cov`

设计与口径详情见 `docs/美股个股终端_设计方案.md` 与
`backend/services/api/stock_terminal_us/README.md`。
