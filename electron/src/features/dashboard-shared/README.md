# 首页六宫格共享层（Dashboard Shared）

## 为什么有这个目录

首页是**六宫格**（`components/layout/ModuleGrid.tsx`）：市场概览 / 资金概览 / 实时交易记录 / 策略监控 / 智能图表 / 信息通知。

问题现场：顶部市场切换器切到港股后，只有「市场概览」跟着切，其余 5 格仍显示 A 股数据 ——
标题写着「资金概览 (港股/模拟)」，数字却是 A 股模拟账户的 ￥1,035,249.58；「实时交易记录 (港股)」
列的是招商银行、正帆科技等 A 股成交；「策略监控」显示 89 条（后端 `?market=HK` 实际只有 15 条）。
**标题改了、口径没改，比不切换更误导。**

本目录把「某个市场该显示什么」收敛成一份注册表 + 一组统一展示件，六宫格只读规格、不写市场分支。

## 目录职责

```
features/dashboard-shared/
├── types.ts                  BoxId / BoxContent / MarketContent 契约
├── marketContent.ts      ★   内容分层唯一声明处（5 市场 × 6 框）
├── notificationScope.ts      站内通知的市场分流（通知表没有 market 列，按内容里的代码分）
├── components/
│   ├── MarketChip.tsx        卡头市场徽标（+ 数据源标注）
│   └── BoxPlaceholder.tsx    数据态占位：加载 / 失败 / 未开通（带开通按钮）/ 空
└── hooks/useMarketBoxes.ts   useMarketContent / useBoxContent / useOpenSimAccount
```

配套的公共工具：`electron/src/utils/marketInfer.ts`
（代码 → 市场推断，与后端 `market_rules.infer_market` 同口径，交易历史页与首页共用）。

## 数据来源与市场维度现状

| 数据 | 端点 | 市场维度 |
|---|---|---|
| 行情概览 | `/api/v1/market/overview?market=` | ✅ 后端已支持 |
| 模拟账户 | `/api/v1/simulation/account?market=` | ✅ 已支持；未开通返回 `account_not_initialized` |
| 策略列表 | `/api/v1/strategies?market=` | ✅ 已支持（`parameters.market`，历史 NULL 按 A 股计） |
| 模拟成交 | `/api/v1/simulation/trades?market=` | ✅ 已支持（按 symbol 形态过滤，见后端 `market_symbol_sql_regex`） |
| 模拟成交统计 | `/api/v1/simulation/trades/stats/summary?market=` | ✅ 已支持 |
| 模拟盘日快照 | `/api/v1/simulation/snapshots/daily` | ❌ 无市场维度 → 非 A 股在规格里关闭该面板 |
| 组合绩效 / 持仓分布 | `/api/v1/portfolios/performance`、`/portfolios/distribution` | ❌ 无市场维度 → 同上 |
| 站内通知 | `/api/v1/notifications` | ❌ 表无 market 列 → 端上按代码分流 + 「全局」兜底 |

## 加一个市场要动什么

1. `marketContent.ts` 的 `SEEDS` 加一份（label / accent / currency / source / defaultSeedCash）
2. 检查该市场的账户与成交端点是否带 `market` 参数；没有就在后端补（参照 `simulation_history.py`）
3. 后端没有市场维度的子面板，在规格里用 `panels: { ... : false }` 关掉并写 `panelNote`
4. 六宫格卡片**不用改**（标题、空态、徽标都从规格来）
5. `electron/tests/dashboard-market.e2e.mjs` 的 `MARKETS` 加一项

## 加一个框要动什么

1. `types.ts` 的 `BoxId` 加枚举 + `BOX_IDS`
2. `marketContent.ts` 的 `defaultBoxes()` 补该框的通用文案（市场差异写进各市场的 `boxes`）
3. 写卡片组件：数据取数走带 `market` 的 hook，空态用 `BoxPlaceholder`，卡头用 `MarketChip`
4. `components/layout/ModuleGrid.tsx` 的 `moduleComponents` 注册

## 三条铁律

1. **市场差异只写在 `marketContent.ts`**：卡片组件里禁止 `if (market === 'HK')`
2. **空就是空**：任何格子缺数据都走 `BoxPlaceholder`，**不许**回落到其它市场的数据，
   也不许把 0 元假账户/默认初始资金当真实数据展示（`getFundOverview` 的默认兜底已为
   `account_not_initialized` 让路）
3. **数值出口挡 null/NaN**：历史事故 `null.toFixed` 整页白屏

## 已知边界

- **实盘模式**：实盘账户是**账户级**（`/api/v1/account` 无 market，单一账户），
  切换市场不会换账户；资金卡会显式标注「实盘账户（账户级）」。模拟账户才是市场级。
- **通知分流**是「宁可少分、不可错分」：只有通知里出现**明确代码形态**才归市场，
  其余一律进「全局」。等后端补 `notifications.market` 后，本文件退化为兜底。
