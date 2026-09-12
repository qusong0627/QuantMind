# 美股市场分析模块 (Market Analysis US)

## 模块说明

美股多维市场分析：**大盘脉搏、市场宽度、板块轮动、财报季、分析师动向、资金与筹码、估值主题**。
与「港股市场分析」（`features/market-analysis-hk/`）平级，同属**独立市场目录 + 跨市场共享层**架构。

数据全部来自本地 QuantUS parquet（`QM_QUANTUS_DATA_DIR`，默认 `data/quantus/`），
**无外部实时行情依赖**。入口：顶部市场切换器选「美股」→ 侧边栏「市场分析」。

## ⚠️ 数据口径与限制（改代码前必读）

以下均为**实测结论**，直接决定哪些面板能做、怎么做。详见
`docs/美股市场分析模块_设计方案.md` 与记忆 `quantus-data-limits`。

| 项 | 事实 | 对模块的影响 |
|----|------|-------------|
| 标的池 | 标普500 + 纳指补充共 **约 517 只**，非全市场 | 页面顶部标注，所有"全市场"统计实为池内统计 |
| 价格 | yfinance `auto_adjust=False` 的**原始未复权价** | 跨期收益在拆股日会跳变 → 板块/宽度聚合一律用**中位数**，个股级用 ±100% 裁剪 |
| 成交额 | `amount` 是**美元原始值**，无换算系数（与 A 股「股/万元」口径不同） | 展示时须标 US$ |
| 指数 | 仅 `SPX/NDX/IXIC/DJI/SOX.US` 五个；**无 VIX、无任何 ETF** | 恐慌度用已实现波动率 + 宽度代理，不承诺 VIX |
| 指数成交额 | `index_daily.amount` **恒为 0**，`SOX.US` 的 `volume` 也是 0 | 指数卡不显示成交额；volume 缺失时**隐藏而非渲染 0** |
| 指数时效 | 指数分区**通常滞后个股若干天** | 各面板分别标注 `trade_date` / `index_date`；同步已修复（见下） |
| 期权 | 只有 call、每股单一到期日、仅单快照 | **不做期权面板**（PCR / IV Rank 无法计算） |
| 财务 | 只有年报，无季报 | 不做季度趋势 / TTM 滚动 |
| 基本面因子 | `l1_factors` 的 pe/pb/roe/mv 等列被显式填 0（设计如此） | 估值/基本面一律走 `f10` 快照 |
| 快照陈旧 | 部分标的（被收购/更名的壳）基本面快照不更新但股价仍在交易 | 估值榜施加健全性门槛（见下） |

### 快照健全性门槛（针对僵尸标的）

`f10` 是单点快照，PARA（并购残留）、SBNY（倒闭残留）这类标的会产出
PE 0.06、PB 0.01 之类的失真值并霸占「低估值榜」。因此 `valuation.py` 内置门槛：

- 市值 ≥ 20 亿美元、PE ≥ 3、PB ≥ 0.3、股息率 ≥ 0.5%
- 目标价隐含空间超过 ±200% 的记录剔除
- 未分类行业归入「未分类」而非丢弃（29 只）

## 目录结构

```
backend/services/api/market_analysis_us/     # 后端：独立市场目录
├── router.py            # ~430 行：27 个端点（REST + SSE 刷新），只做转发
├── feed/                # 数据层按域拆分（每个 Tab ↔ 一个模块）
│   ├── base.py          # 基座：目录解析 / 显式分区读取 / 截面快照 / 名称·行业映射 / 状态
│   ├── indices.py       # Tab1 指数脉搏 + 指数间相对强弱
│   ├── breadth.py       # Tab1 温度计 + Tab2 宽度（MA50/200、52周高低、A-D 线）
│   ├── sectors.py       # Tab1 热力图 + Tab3 轮动 / 板块内宽度 / 板块估值
│   ├── earnings.py      # Tab4 财报日历 / 超预期 / 预期修正
│   ├── analysts.py      # Tab5 评级升降级 / 目标价 / 评级分布
│   ├── holdings.py      # Tab6 内部人 / 机构持仓 / 除息日历 / 拆股
│   └── valuation.py     # Tab7 估值榜 / 市值分层 / 估值概览
└── README.md

electron/src/features/market-analysis-us/    # 前端：独立 feature 目录
├── pages/MarketAnalysisUsPage.tsx           # 主页面（7 Tab，蓝色主题）
├── components/Us*.tsx                       # 各面板
├── services/api.ts                          # /api/v1/market-analysis-us 封装
└── types.ts                                 # 响应类型（与后端字段严格对齐）

electron/src/features/market-analysis-shared/  # 跨市场共享（三市场共用）
├── SectorHeatmapChart.tsx                   # 热力矩形图（原 ShenwanHeatmapChart）
└── ui.tsx                                   # PctText/NumText/SectionCard/RankRow/...
```

共享层复用（**零改动**）：`backend/services/api/market_analysis_shared/`
（`caching` TTL 缓存 / `market_days` 分区推交易日 / `display` 数值口径 / `names` 名称映射）。

港股与 A 股的原组件文件已改为 re-export 指向共享目录，**调用方零改动**。

## 性能关键：按日期取数必须走显式分区

`daily_forward` 每个 parquet 文件**内部自带 `dt` 列**，会遮蔽 DuckDB 的 hive 分区列，
导致 `read_parquet('**/*.parquet') WHERE dt IN (...)` 无法裁剪分区、耗时随库内总分区数增长。

**正确做法**（`base._read_partitioned`）：拼显式分区文件列表 + 列裁剪。

实测（本机 data/quantus，6571 个分区）：

| 方式 | 窗口 | 耗时 |
|------|------|------|
| 全 glob + `WHERE dt BETWEEN` | 250 日 | 1.73 s |
| 显式文件列表 + 3 列 | 250 日 | **0.05 s** |
| 显式文件列表 + `SELECT *`（11 列） | 310 日 | 1.62 s |
| 显式文件列表 + 3 列 | 310 日 | **0.05 s** |

结论：**既要文件列表，也要裁剪列**。列裁剪的收益和分区裁剪同一量级（32 倍）——
宽度历史（310 个分区、`SELECT *` + 全量滚动）原本 9.7 s，改成 3 列后 0.24 s。

## API 端点（前缀 `/api/v1/market-analysis-us`）

| 分组 | 端点 |
|------|------|
| 诊断 | `GET /status` |
| Tab1 | `/indices/overview`、`/indices/spread`、`/breadth`、`/heatmap`、`/profit-leaders` |
| Tab2 | `/breadth/history`、`/breadth/highlights` |
| Tab3 | `/sector-rotation`、`/sector-valuation` |
| Tab4 | `/earnings/calendar`、`/earnings/surprises`、`/earnings/revisions` |
| Tab5 | `/analysts/upgrades`、`/analysts/targets`、`/analysts/ratings` |
| Tab6 | `/insiders/movers`、`/holdings/institutional`、`/corporate-actions/{dividends,splits,dividend-history}` |
| Tab7 | `/valuation/rankings`、`/valuation/size-tiers`、`/valuation/overview` |
| 刷新 | `POST /refresh`、`POST /refresh/stream`（SSE） |

端点模式与港股一致：`Depends(get_current_user)` + `asyncio.to_thread(feed.xxx)` + `except → 500`。

## 数据源要点

- **内部人交易**：`insider_transactions.Transaction` 列**恒为空**，类型必须从 `Text` 前缀解析
  （`Sale at price...` / `Purchase at price...`）。只有 Purchase 与 Sale 是择时信号，
  授予/行权属薪酬事件已排除。全样本（2014 起）仅 982 笔买入 —— 内部人买入是强信号。
- **机构持仓**：`major_holders` 是 4 行**无标签**宽表，顺序固定为
  内部人占比 / 机构占比 / 机构流通股占比 / 机构家数（已对 487 只全样本校验）。
  13F 口径，披露日滞后约一个季度，面板需标注。
- **分析师升降级**：`upgrades_downgrades` 每股约 377 条、含目标价前后值，是最有信息量的一块。
  评级词无统一枚举，`analysts._GRADE_KEYWORDS` 按「先具体后宽泛」顺序归一为四档
  （3 看多 / 2 中性 / 1 看空 / 0 强看空）。注意 `market perform` 必须用完整短语，
  单独用 `perform` 会误命中 `outperform`。
- **超预期**：会出现 200%+ 的极端值（如 NKE 实际 0.72 vs 预估 0.13），
  已用 `earnings_history` 与 `earnings_dates` **两张独立来源交叉验证一致**，
  是源数据的预估列本身偏低，**不是本模块的计算口径问题，不要"修正"**。
- **派息日历**：`calendar` 表含 `Ex-Dividend Date` / `Dividend Date`（404/489 只覆盖）。

## 测试

```bash
# 在容器内（后端依赖在容器里）
docker exec -w /app/backend quantmind python -m pytest tests/test_quantus_market_analysis.py -q --no-cov
```

`backend/tests/test_quantus_market_analysis.py`：**零 mock** 直连本地 parquet，
断言取向是量级与健康度（防停更 / 防排序回归 / 防口径失真），而非精确数值。

覆盖点：分区时效、标的池与行业覆盖、截面裁剪、五个指数与 None 语义、
温度计自洽性、宽度序列与 52 周极端值自洽、板块排序、财报窗口、评级方向纯函数、
内部人类型解析（含 None）、估值门槛、市值分层守恒、缓存往返、缺失分区容错。

## 相关

- 设计方案：`docs/美股市场分析模块_设计方案.md`
- 港股对标实现：`backend/services/api/market_analysis_hk/`、`electron/src/features/market-analysis-hk/`
- 数据平台：`backend/services/engine/data_platform/quantus_hub.py`
- 同步：`backend/scripts/quantus_daily_sync.py`（已修复「全量分支漏同步指数」）
