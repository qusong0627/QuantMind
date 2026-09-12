# 个股终端共享层（Stock Terminal Shared）

## 为什么有这个目录

个股终端原本是**整目录复制**：港股终端是 A 股终端的一次性复制（commit `ec2b2871`，6289 行）。
复制之后两边各自继续改，很快出现**双向漂移**：

| 漂移项 | A 股版 | 港股版 |
|---|---|---|
| `services/stockTerminalService.ts` | 有 `resolveWebSafeServiceBase`（Web 端相对路径修复） | 缺这个修复 |
| `InferenceScoreChart` | 有 `compact` / `endDate`（被 inference-center 复用） | 旧实现 |
| `StockSidebar` | 信号列无 | 有信号列与仓位语义 |
| `KlineChart` 缩放条几何 | 预留 52px / slider 20px | 预留 26px / slider 16px |

事实是：两份各约 300 行的服务文件，**真实差异只有 16 行赋值**。
所以共享层是**机械提取**，不是重写 —— 差异全部收敛成主题参数与一处配置。

## 目录职责

```
stock-terminal-shared/
├── types.ts                    跨市场类型（StockListItem / StockProfile / KlineBar / KlineMarker …）
├── utils.ts                    代码格式归一（toPrefix）与数值格式化
├── service.ts                  ★ 取数唯一实现：createStockTerminalService 由配置驱动
├── adapter.tsx                 ★ StockTerminalProvider / useStockTerminal：主题 + 服务 + 自选格式
├── engine/indicators.ts        纯前端指标（sma/ema/boll/macd/rsi/kdj/volMa）
├── components/
│   ├── KlineChart.tsx          ECharts K 线主图（副图/叠加/分数轴/事件竖线）
│   ├── StockSearchBar.tsx      顶部搜索联想（不预加载全量）
│   └── InferenceScoreChart.tsx 推理分折线（compact / endDate 能力取自 A 股版超集）
└── page/StockTerminalShell.tsx ★ 页面骨架：顶栏 + K 线卡 + 右侧详情卡（详情体由各市场注入）
```

## 三个市场如何接入

每个市场目录只保留**市场特有**的东西，其余全部复用：

| 市场 | 页面 | 主题差异 | 数据源配置 | 详情体 |
|---|---|---|---|---|
| A 股 `stock-terminal/` | `pages/StockTerminalPage.tsx`（薄） | 紫蓝渐变、复权三选项、参考线 | `{klineMarket:'A', quoteMarket:'CN'}` | `CnDetailBody`（9 Tab） |
| 港股 `stock-terminal-hk/` | 同上 | 「黄金线」、紧凑缩放条 | `{..., listMarket:'HK', indexMaSymbol:'HSI.HK'}` | `HkDetailBody`（7 Tab） |
| 美股 `stock-terminal-us/` | 同上 | 蓝色、只有「不复权」+ 拆股竖线 | `paths` 全指向 `/stock-terminal-us/*` | `UsDetailBody`（9 Tab） |

页面写法固定为：`<StockTerminalProvider …><StockTerminalShell renderDetail={…} /></StockTerminalProvider>`。

## 加一个市场要动什么

1. 新建 `features/stock-terminal-<market>/{pages,components,services}/`
2. `services/stockTerminalService.ts`：`new StockTerminalService(市场配置)`（需要额外端点就继承一层，美股是范例）
3. 页面：声明 `TerminalTheme` + Provider + Shell + 自己的详情体
4. `App.tsx` 的 `StockTerminalByMarket` 加一个分支
5. `electron/tests/stock-terminal.e2e.mjs` 的 `MARKETS` 加一项

菜单、路由、权限、i18n 都不用动（nav id `stock-terminal` 三市场共用）。

## 兼容约定（改这里前必读）

- **原路径 re-export**：`stock-terminal/types.ts`、`stock-terminal/components/InferenceScoreChart.tsx`
  仍是转发文件 —— 因为 `features/inference-center/components/ModelScoreCurveGrid.tsx` 直接引用后者。
  改动共享层时不要删这两个转发文件。
- **主题即差异**：市场差异一律加进 `TerminalTheme` / `TerminalMarketConfig`，
  **不要**在共享组件里写 `if (market === 'US')`。
- **null 安全**：所有数值出口都要挡 null/NaN（历史事故：`null.toFixed` 整页白屏）。

## 已删除的重复实现

2026-09-12 统一时删除的孤儿文件（两套目录各一份，全仓库无引用 —— 终端改成搜索驱动布局后遗留）：

`StockSearchBar` / `kline/KlineChart` / `engine/indicators` / `components/InferenceScoreChart`（A 股与港股各自副本）
与 `kline/KlineWorkspace` / `kline/KlineReplay` / `ChartBacktestPanel` / `RankingPanel` / `IndexMaCard` / `TagStrip`。

内容都在 git 历史里，共享层是它们的唯一后续版本。
