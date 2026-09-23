---
name: quantdb-sdk
description: "QuantDB 数据 SDK — API Key 配置、数据集目录、字段查询。在 QuantBot / Claude Code 中查询 QuantDB 数据、配置 API Key、预览/同步数据集、远程查询 K 线/财务/因子时使用。触发词：quantdb、QuantDB、数据key、API key、数据集、数据字段、远程数据、同步数据集、查询K线、查看字段"
---

> ## ⚙️ 运行环境契约（最高优先级，先于本文其余内容执行）
>
> 本技能可能运行在 **QuantBot（QwenPaw 容器）** 或**宿主机/本地 Claude Code**。执行前先探测环境（`which docker`、API 连通性），并遵守以下映射规则：
>
> 1. **后端 API 地址**：QwenPaw / 容器网络内一律用 `http://quantmind:8000`（`quantmind` 是 docker 网络别名）；仅宿主机调试用 `http://127.0.0.1:8000`。正文中出现的 `127.0.0.1:8000`、`localhost:800x`，在 QwenPaw 环境下自动替换为 `http://quantmind:8000`。
> 2. **取数脚本执行**：凡 import 了 `pandas / duckdb / psycopg2 / numpy / sqlalchemy` 等重依赖或 `backend` 包的脚本，**必须在 quantmind 容器内执行**（QwenPaw 本地 venv 无这些依赖）：
>    ```bash
>    docker cp <脚本路径> quantmind:/tmp/<脚本名> && docker exec -w /app quantmind python3 /tmp/<脚本名> <参数>
>    ```
>    脚本源三选一：宿主机 repo `skills/<name>/scripts/`、QwenPaw 工作区 `/app/working/workspaces/default/skills/<name>/scripts/`、挂载目录 `/quantmind/skills/<name>/scripts/`。纯标准库脚本（无重依赖）可在 QwenPaw 本地直接跑。
> 3. **报告落盘**：股票报告页可见的 MD/PDF 报告，直接写 `/data/reports/trading_agents/{市场或类别}/{股票名}/`（QwenPaw 对 `/app/db` 有写权限，**直接写文件，不要 docker cp**）；过程数据 facts 写 `/data/reports/<类别>/`（`/data` 可写）。
> 4. **MD → PDF 转换（按优先级降级）**：
>    ① `docker exec -w /app quantmind python3 backend/scripts/md_to_pdf_report.py <输入.md> <输出.pdf>`（研报级排版，首选）；
>    ② docker 不可用时，**改用 QwenPaw 内置 `pdf` 技能**把 MD 转成 PDF；
>    ③ 两者都不可用则只交付 MD，并明确告知用户 PDF 未能生成及原因。
> 5. 本文中的 `~/.claude`、`cp -r ... ~/.claude/skills` 等说明仅适用于本地 Claude Code 维护者，**QuantBot 不要执行**。

# QuantDB 数据 SDK 技能

QuantDB 是付费 CDN 数据源，通过 API Key 认证。提供 A 股完整数据：K 线、财务、估值、技术指标、L1/L2 因子（共 315 维 AI 因子）。

## 1. API Key 配置

### 1.1 查看配置状态（脱敏）
```bash
BASE=http://127.0.0.1:8000
TOKEN=$(curl -s -X POST $BASE/api/v1/auth/login -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"<管理员口令>","tenant_id":"default"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin).get('access_token',''))")
AUTH="Authorization: Bearer $TOKEN"

# 查看 Key 是否已配置 + 数据目录 + runtime.env 路径
curl -s -H "$AUTH" "$BASE/api/v1/admin/data-platform/quantdb/config"
# 返回: {api_key_configured, api_key_masked, data_dir, runtime_env_file}
```

### 1.2 保存/更新 API Key
```bash
curl -s -X POST -H "$AUTH" -H "Content-Type: application/json" \
  "$BASE/api/v1/admin/data-platform/quantdb/config" \
  -d '{"api_key":"qdb_your_key_here"}'
# 写入 config/runtime.env 并立即生效，当场用 get_me() 验证 Key 可用性
# 返回: {api_key_masked, verified, error}
```

### 1.3 SDK 状态
```bash
curl -s -H "$AUTH" "$BASE/api/v1/admin/data-platform/quantdb/info"
# 返回: {quantdb: {installed, api_key_configured, ...}}
```

**Key 存储**：写入 `config/runtime.env`（`QUANTDB_API_KEY=...`），环境变量 `QUANTDB_API_KEY` 优先。

## 2. 数据集目录（能获取哪些字段）

QuantDB 按 6 大类 28 个数据集组织，本地已全部落盘（2016-01 至 2026-08）：

### 1 K线行情
| 数据集 | 内容 | 日期范围 |
|---|---|---|
| `daily_forward` | 日线前复权（训练/回测主用） | 2016~2026 |
| `daily_backward` | 日线后复权 | 2016~2026 |
| `daily_unadjusted` | 日线不复权（注意 amount/volume 单位 20260721 切换） | 2016~2026 |
| `index_daily` | 指数日线 | 2016~2026 |
| `min5_kline` / `min1_kline` | 5分钟/1分钟线 | 按需同步 |
| `tick_data` | Tick 逐笔（流量消耗极高） | 按需 |

### 2 基础板块
| 数据集 | 内容 |
|---|---|
| `instrument_detail` | 个股详情（**152 列基本面快照**） |
| `sector_concept` | 板块概念 |
| `index_weights` | 指数权重（沪深300/中证500/1000 等） |
| `trading_calendar` | 交易日历 |
| `margin_trading` | 融资融券 |

### 3 财务数据（按 symbol 文件）
`balance`（资产负债表）/ `income`（利润表）/ `cashflow`（现金流量表）/ `capital`（股本结构）/ `pershare_index`（每股指标）/ `dividend_factors`（分红因子）/ `holder_num`（股东户数）

### 4 债券/ETF
`etf_pcf`（ETF申赎清单）/ `convertible_bond`（可转债）

### 5 技术衍生
| 数据集 | 内容 |
|---|---|
| `valuation` | 估值（**PE/PB/市值**） |
| `technical_indicators` | 技术指标（均线/RSI/KDJ/MACD/波动率） |
| `market_sentiment` | 市场情绪 |

### 6 ML数据集（因子核心）
| 数据集 | 内容 |
|---|---|
| `features_daily` | 日频特征（技术指标+估值+交易属性合并，**78 列**，2026-09 起） |
| `l1_factors` | **L1 因子（98 因子）**：动量/波动/流动性/技术/基本面/风格/行业/筹码/概念 |
| `l2_factors` | **L2 因子（216 高频微观因子）**：VPIN/资金流/价差/深度/订单/竞价/跳跃/冲击 |
| `l1_l2_factors` | L1+L2 合并 |

### 查看数据集目录统计
```bash
curl -s -H "$AUTH" "$BASE/api/v1/admin/data-platform/quantdb/catalog"
# 返回: {groups: [{dataset_count, synced_count, files, size_mb}], datasets: [...], data_dir}
```

### 远程 meta（各数据集日期范围）
```bash
curl -s -H "$AUTH" "$BASE/api/v1/admin/data-platform/quantdb/remote-meta"
# 返回 28 行: {source, dataset, dimension, min_date, max_date, row_count, file_count, file_size}
```

## 3. 数据预览

### 本地预览（零流量）
```bash
# 预览数据集样本（本地 parquet 优先）
curl -s -H "$AUTH" "$BASE/api/v1/admin/data-platform/quantdb/preview?dataset=daily_forward&limit=20"
curl -s -H "$AUTH" "$BASE/api/v1/admin/data-platform/quantdb/preview?dataset=valuation&limit=20"
curl -s -H "$AUTH" "$BASE/api/v1/admin/data-platform/quantdb/preview?dataset=l1_factors&limit=20"
# 标的层数据集可指定 symbol
curl -s -H "$AUTH" "$BASE/api/v1/admin/data-platform/quantdb/preview?dataset=income&symbol=600519.SH&limit=20"
# 强制远端 SDK 预览
curl -s -H "$AUTH" "$BASE/api/v1/admin/data-platform/quantdb/preview?dataset=valuation&remote=true&limit=20"
# 返回: {columns: [{name, dtype}], data: [...]}
```
**用 preview 看字段**：返回的 `columns` 就是该数据集全部字段名——这是"能获取到哪些字段"的直接答案。

## 4. 远程 SDK 查询（消耗流量配额）

```bash
# 远程 K 线
curl -s -X POST -H "$AUTH" -H "Content-Type: application/json" \
  "$BASE/api/v1/admin/data-platform/quantdb/query-kline" \
  -d '{"symbol":"600519.SH","adj_type":"qfq","start_date":"2026-01-01","end_date":"2026-08-07"}'
# adj_type: qfq(前复权)/hfq(后复权)/none(不复权)

# 股票列表
curl -s -H "$AUTH" "$BASE/api/v1/admin/data-platform/quantdb/stock-list?keyword=茅台&limit=20"

# 交易日历
curl -s -H "$AUTH" "$BASE/api/v1/admin/data-platform/quantdb/calendar?start_date=2026-08-01&end_date=2026-08-31"

# Tick 逐笔
curl -s -X POST -H "$AUTH" -H "Content-Type: application/json" \
  "$BASE/api/v1/admin/data-platform/quantdb/query-tick" \
  -d '{"symbol":"600519.SH","trade_date":"2026-08-07","fields":"last_price,open,high,low,volume,amount","limit":500}'
```

## 5. 数据同步

```bash
# 按数据集同步（异步任务）
curl -s -X POST -H "$AUTH" -H "Content-Type: application/json" \
  "$BASE/api/v1/admin/data-platform/quantdb/sync-datasets" \
  -d '{"datasets":["l1_factors","l2_factors"],"with_pg":true,"with_qlib":true}'
# with_pg: 同步后填充 PG stock_daily_latest
# with_qlib: 同步后重建 Qlib 缓存

# 同步任务状态
curl -s -H "$AUTH" "$BASE/api/v1/admin/data-platform/quantdb/sync-jobs"
curl -s -H "$AUTH" "$BASE/api/v1/admin/data-platform/quantdb/sync-jobs/{job_id}"
# 取消任务
curl -s -X POST -H "$AUTH" "$BASE/api/v1/admin/data-platform/quantdb/sync-jobs/{job_id}/cancel"
```

## 5.1 数据流转（QuantDB → 本地库）

```
QuantDB SDK（付费 CDN）
  ↓ sync_dataset() / 增量拉取
本地 parquet（data/quantdb/，V2 分区 dt=YYYYMMDD/data.parquet）
  ↓ DuckDB 视图（quantdb_hub）+ fill_pg_from_parquet
PostgreSQL stock_daily_latest（快照表）
  ↓ qlib_data_builder.py
Qlib 二进制缓存（data/quantdb/.qlib_cache/cn_data）
  ↓ generate_feature_snapshots.py
特征快照（db/feature_snapshots/model_features_{year}.parquet）
```

**Redis DB 分配**：0=general、1=auth、2=trade、3=market、4=backtest、5=cache
**PG 表**：`stock_daily_latest`（A股快照，含行情/基本面/技术/资金流）、`feature_snapshots`（152维深度特征）

## 6. 字段详情速查（关键数据集列）

### valuation（估值，18 列）
`symbol, time, close, total_capital, circulating_capital, total_mv, float_mv, net_profit_ttm, revenue_ttm, equity, annual_net_profit, pe_ttm, pe_static, pb, ps_ttm, dividend_rate`

### technical_indicators（技术指标，37 列）
`close, ma5, ma10, ma20, ma60, ma_gap_5/10/20, rsi_6, rsi_14, kdj_k/d/j, macd_dif/dea/hist, vol_std_5/20/60, vol_atr_14, vol_to_ma5/20, volume_ma_3, amount_ma_5, volume_trend_3d, return_1d/3d/5d/10d/20d/60d, pct_change, beta_20`

### market_sentiment（市场情绪，19 列）
`price_range, upper_shadow, lower_shadow, body_ratio, amount_per_trade, liquidity_score, intraday_vol, gap_up_down, buy_pressure, sell_pressure, momentum_1d/3d, am_pm_trend, volume_concentration`

### features_daily（日频特征合并表，**78 列**，2026-09 起）
= technical_indicators + valuation 的 46 个数值列（`close/ma*/rsi*/kdj*/macd*/vol_*/return_*/pct_change/beta_20/total_capital/circulating_capital/total_mv/float_mv/net_profit_ttm/revenue_ttm/equity/annual_net_profit/pe_ttm/pe_static/pb/ps_ttm/dividend_rate`）**↑ 新增 30 列**（全部以**字符串**存储，用前需转数值）：

- **标记(0/1 字符串)**：`in_hs300, is_hsgt, is_margin, is_kcb_creatable, is_st, is_quit_risk, is_hk`
- **分类**：`industry_code/industry_name`（128 细分行业）、`sector_code`、`region_area_code/region_area_name`（32 地区板块）、`main_business`（自由文本）、`list_date`（`YYYYMMDD` 字符串）
- **市值/股本**：`total_cap_yi / float_mv_yi`（**亿元**，= `total_mv/float_mv`÷1e8）、`free_float_shares`（**万股**，自由流通股本）
- **价格**：`ipo_price`、`zt_price`（涨停价，元）、`dt_price`（跌停价，元）——基于**不复权**价
- **交易行为**：`hs_turnover`（换手率 %）、`seal_strength`（封板强度）、`zaf`（当日涨跌幅 %，≈`pct_change`）、`ever_zt_count`、`year_zt_days`
- **估值/风险**：`beta_now`、`dyna_pe`（动态PE，含负哨兵）、`static_pe_ttm`、`div_yield`（股息率 **%**）、`pb_mrq`（市净率 MRQ）

> 单位陷阱详见 [[quantdb-fields]]：`div_yield` 是 %（5.14=5.14%），与本表 `dividend_rate`（小数，÷100）差 100 倍；`total_cap_yi/float_mv_yi` 是亿元，与本表 `total_mv/float_mv`（元）差 1e8。

### l1_factors（98 因子）
动量(22): `mom_ret_1d~120d, mom_ma_gap_5/10/20, mom_ema_gap_12/26, mom_macd_*, mom_rsi_*, mom_kdj_*`
波动(11): `vol_std_5/10/20, vol_true_range, vol_atr_20/14, vol_parkinson_10/20, vol_gk_20`
流动性(12): `liq_volume/amount, liq_volume_ma_5/20, liq_volume_ratio_5/20, liq_amount_ma_5/20, liq_obv_20, liq_mfi_14`
基本面(12): `fun_turnover_1/5/20, fun_mv/total_mv, fun_pe/pb/bp/ep, fun_mv_rank, fun_value_zscore, fun_roe, fun_peg`
风格(9): `style_beta_20/60/120, style_idio_vol_20/60, style_residual_ret_20, style_size_20, style_value_20`
行业(14): `ind_ret_5/10/20, ind_rotation_speed_20, ind_strength_20/60, ind_dispersion_20, ind_breadth_up_20, ind_volume_ratio_20, ind_crowding_20, ind_netflow_rank_20, ind_relative_pe, ind_concentration, ind_relative_momentum_20`
筹码(9): `chip_profit_ratio_20/60/120, chip_concentration_20, chip_peak_distance, chip_floating_ratio, chip_cost_90_width, chip_profit_delta_5`
概念(11): `concept_hot_score, concept_momentum_top3, concept_exposure_top1, concept_rotation_score, concept_crowding_max, concept_diversity, concept_flow_rank, concept_leader_score, concept_cross_sector, concept_volume_ratio`

### l2_factors（216 高频微观因子）
VPIN/价差/深度/订单/资金流/已实现波动/竞价/跳跃/冲击等，如 `micro_vpin_8/20/50, micro_pin, micro_order_flow_toxicity, micro_realized_spread, micro_kyle_lambda, micro_amihud_illiquidity, micro_depth_imbalance_1, flow_money_flow_index, vol_realized_rv/rrv/rskew, micro_jump_count_1pct, micro_trade_buy/sell_pressure`

## 6.1 字段单位速查 → [[quantdb-fields]]

> ⚠️ **用数据前必查单位**。各数据集单位不一（个股 volume=股、指数 volume=手、amount=万元、dividend_rate 是 % 百分数值不是小数），搞错差 1e4~1e6 倍。全部实测验证的单位表见 **[[quantdb-fields]]** 技能：
> - 个股 kline：volume=**股**、amount=**万元**；指数 index_daily：volume=**手**、amount=**万元**
> - technical_indicators close=**后复权**；valuation close=**不复权**
> - vol_std：technical_indicators 是 **%**，l1 是**小数**（差 100 倍）
> - dividend_rate=0.148 表示 **0.148%**，不是 14.8%
> - PG `stock_daily_latest`：symbol 是**前缀** SH601138，amount 也是**万元**

## 7. 实战应用

- **数据落盘校验**：`catalog` + `remote-meta` 看覆盖，`preview` 看字段
- **当日数据补全**：本地缺当日时用 `query-kline` 远程拉
- **因子挖掘原料**：`sync-datasets` 同步 l1/l2_factors，喂给 [[rd-agent-factor-mining]]
- **选股字段**：valuation/l1_factors 的字段是 [[smart-strategy-stock-picking]] 的数据源
- **训练特征**：features_daily/l1_factors 是 [[quantmind-operations]] 模型训练的特征来源

## 8. 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| API Key 未配置 | 缺 QUANTDB_API_KEY | `/config` POST 保存 |
| verified=false | Key 无效/过期 | 重新获取 Key |
| preview 空 | 本地未同步该数据集 | 用 `remote=true` 或先 sync |
| 流量消耗高 | 频繁远程查询 | 优先本地 parquet，`preview` 默认零流量 |
| min1/tick 无数据 | 未同步 | 按需 `sync-datasets` |
