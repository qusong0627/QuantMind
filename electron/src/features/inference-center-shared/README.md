# 推理中心共享层（Inference Center Shared）

## 为什么有这个目录

推理中心原本是**两版 fork**：A 股页与港股页各约 1010 行，diff 291 行（≈30%），
而且已经双向漂移 ——

| 功能 | A 股版 | 港股版 |
|---|---|---|
| 基准日回退后同步输入框 + 提示 | 有 | 无（后端字段名不同） |
| 个股 K 线含基准日之后的**真实走势** | 有（窗口 `基准日-100天`） | 无（只拉 60 天） |
| `forecast_warning` 告警条 | 有 | 丢弃 |
| 多模型分数曲线 `ModelScoreCurveGrid` | 有 | 无 |
| 评级徽标 | 无 | 有 |
| SHAP 因子归因 `FeatureDriversPanel` | 无 | 有 |
| 共识矩阵 `ModelConsensusPanel` | 无 | 有 |

同时 `App.tsx` 的分发是二分支 —— **切到「美股」时渲染的是 A 股页**（默认标的 SH600519、货币 ¥、A 股联想表、SSE 日历）。

现在收敛为：**一个共享实现 + 三份约 50 行的适配器**。

## 目录职责

```
inference-center-shared/
├── adapter.tsx                        ★ InferenceCenterProvider / useInferenceCenter
├── pages/InferenceCenterShell.tsx     ★ 顶栏 + 两个顶层 Tab 的完整状态机（约 1000 行）
└── components/                        市场无关的展示组件
    ├── StockForecastChart.tsx         K 线 + P10/P50/P90 预测扇形（currencySymbol 参数化）
    ├── ModelScoreCurveGrid.tsx        多模型分数曲线小卡
    ├── FeatureDriversPanel.tsx        SHAP / 启发式因子归因
    └── ModelConsensusPanel.tsx        多模型共识矩阵
```

## 适配器要提供什么

| 字段 | 说明 | CN | HK | US |
|---|---|---|---|---|
| `market` | 市场码，传给 `/models`、`/research/models`、`predict-stock`、模型过滤 | `CN` | `HK` | `US` |
| `marketLabel` | 顶栏市场 Tag | A股市场 | 港股市场 | 美股市场 |
| `calendar` | 交易日历（`/market-calendar/*`） | `SSE` | `HKEX` | `NYSE` |
| `currencySymbol` | 基准价/图表货币符号 | `¥` | `HK$` | `$` |
| `defaultSymbol` | 个股预测默认标的 | `SH600519` | `0700.HK` | `AAPL` |
| `searchPlaceholder` | 输入框占位提示 | 600519 或 茅台 | 00700 或 腾讯 | AAPL 或 苹果 |
| `preload/search/toSymbol/normalize` | 代码联想与归一 | 本地静态表 + 前缀式 | 名称接口 + 4/5 位互配 | 标的池 + 裸 ticker |
| `fetchKline` | 个股 K 线 | `/research/kline` | `/research/kline` | `/stock-terminal-us/kline` |
| `toSuffixSymbol` | 多模型曲线需要的代码形式 | `600519.SH` | `0700.HK` | 原样 ticker |

**共享组件里不允许出现 `if (market === 'US')`**；市场差异一律进适配器。

## 美股为什么走自己的端点

- **K 线**：`/research/kline/{symbol}` 只分了 `is_hk` 与 A 股两条分支，对美股**静默返回空 K 线**
  （腾讯兜底还会把 ticker 当 A 股拼 `ts_code`）。走 `/stock-terminal-us/kline`（读本地 QuantUS parquet）。
- **联想**：`/research/stock-names?market=US` 会落到 A 股 `instrument` 表。走 `/stock-terminal-us/list`。
- **代码归一**：**不做** A 股式前缀补全 —— ticker 是裸码，误用 SH/SZ 规则会把 `SHOP` 改坏。

## 加第 N 个市场

1. 新建 `pages/InferenceCenter<Market>Page.tsx`：写一个 `InferenceCenterAdapter`，
   用 `<InferenceCenterProvider adapter={...}><InferenceCenterShell /></InferenceCenterProvider>` 包起来
2. `App.tsx` 的 `InferenceCenterByMarket` 加一个分支
3. `electron/tests/inference-center.e2e.mjs` 的 `MARKETS` 加一项

菜单 / 路由 / 权限 / i18n 都不用动（nav id `inference-center` 三市场共用）。

## 已知限制（改代码前必读）

- **个股预测的预测扇形（P10/P50/P90）依赖模型的分位推理能力**，目前只有部分模型支持；
  不支持时 `forecast_curve` 为空、`p10/p90` 为 null，UI 显示「该模型未启用分位推理」。
  这是模型能力问题，不是前端可以补的。
- **14 个美股模型里只有部分算法能真正跑推理** —— 见 `model-registry` 的拆分实现与方案文档；
  推理中心展示的是后端返回的模型列表，模型自身能否加载由模型层决定。
- 美股的 `pred.parquet` 逐算法是否已独立，取决于模型拆分是否已修（见方案文档的实施状态）。
