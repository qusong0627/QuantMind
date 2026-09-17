---
name: stock-universal-analysis
description: "个股全维度数据分析（QuantDB 直读）— 任意 A 股代码，一次拉齐：历史日线（前/后复权）、板块归属与指数权重、财务三表（资产负债表/利润表/现金流）、估值与技术指标、L1/L2 因子。用户说「分析下某股票数据」「任意股票的历史/财报/板块」「XX股基本面数据」「XX 行业地位」时使用。触发词：个股数据、任意股票分析、财报、板块归属、历史数据、基本面数据、股东户数、股息率"
---

> ## ⚙️ 运行环境契约（最高优先级，先于本文其余内容执行）
>
> 1. **QuantDB 数据目录**：宿主机 `/home/zbox/projects/quantmind/data/quantdb` ↔ 容器内 `/data/quantdb`（quantmind 容器）↔ dsh 挂载 `/quantmind/data/quantdb`。**先探测哪个路径存在**再取数。
> 2. **重依赖（pandas/duckdb/psycopg2）一律在 quantmind 容器内跑**：
>    ```bash
>    docker cp <脚本路径> quantmind:/tmp/ && docker exec -w /app quantmind python3 /tmp/<脚本> <CODE> <参数>
>    ```
>    纯标准库脚本可在宿主机/dsh 直接跑。
> 3. **报告落盘**：`/data/reports/trading_agents/个股/{代码}/`（容器内写文件，不要 docker cp）；过程 facts 写 `/data/reports/个股/`。
> 4. **symbol 格式**：个股 = 后缀 `601138.SH`；到 PG 表则是前缀 `SH601138`（两套别混）。

# stock-universal-analysis — 个股全维度数据分析

对**任意 A 股代码**做一次完整的数据体检，输出结构化 facts（JSON），禁止编造数字——查不到就写 null 并说明。

## 1. 必查数据集（QuantDB 目录映射）

| 维度 | 路径（parquet） | 关键字段/单位 |
|---|---|---|
| 历史日线 | `1_kline_data/daily_forward/`、`daily_backward`（后复权）、`daily_unadjusted` | volume=**股**、amount=**万元**；forward=前复权 |
| 板块/市值 | `2_base_sector/instrument_detail/` | `Symbol` 后缀格式；`J_zgb`=**万股**、`Zsz/Ltsz`=**亿元**、`J_yysy`=**万元**；`HqDate` 可能停滞，先看日期 |
| 指数权重 | `2_base_sector/index_weights/` | 文件名 `000300.SH.parquet`；`Weight` = **%** |
| 财务三表 | `3_financial_data/balance|income|cashflow/` | 科目均为**元**（与板块 J_* 万元差 1e4，别混） |
| 股本/股东/分红 | `3_financial_data/capital|holder_num|dividend_factors/` | `dividend_factors.interest`=每10股派息（**/10 才是每股**） |
| 估值 | `5_technical_derived/valuation/` | pe/pb/股息率用 `dividend_rate`（板块 DYRatio 不可靠） |
| 技术指标 | `5_technical_derived/technical_indicators/` | ma/波动/量比等 |
| 因子 | `6_ml_datasets/features_daily/`、`l1_factors/`、`l2_factors/` | l2 flow 类字段注意金额单位 |
| 最新快照 | PG `stock_daily_latest`（symbol 前缀 `SH601138`） | 全 A 日线+估值+均线+行业 |

## 2. 标准流程（每只股都这么走）

1. **日线**：`daily_forward` 最后 60 根 + `daily_unadjusted` 最后 5 根（对账除权）；输出 近5日 OHLCV、60 日涨跌幅、区间高低。
2. **板块归属**：`instrument_detail` 该代码行 → 行业/市值/流通占比；`index_weights` 若在权重股索引里列出占比。
3. **财报**：`income` 最新报告期营收/净利同比，`balance` 总资产/负债率，`cashflow` 经营现金流；`holder_num` 股东户数变化。
4. **估值与因子**：`valuation` pe_ttm/pb/股息率；`technical_indicators` 均线位置；`l2_factors` 近 5 日关键因子。
5. 汇总为 facts 表（字段：代码/名称/行业/市值/PE/PB/股息率/近1月涨跌/最新报告期/营收与净利增速/股东户数/关键因子值），并给出一句数据质量注记（停滞日期等）。

## 3. 常见坑（实测字段手册）

- `min1/min5` 分钟线**停滞在 2026-07-24**，用前必查最新日期，别当实时。
- 板块 `HqDate` 停在 20260720，市值/估值滞后于日线。
- 北向 `hsgt_north` 停更 2024-08（改季度披露），别再查。
- 股息率只信 `valuation.dividend_rate`。
- 报告 PDF：`docker exec -w /app quantmind python3 backend/scripts/md_to_pdf_report.py <md> <pdf>`，不可用则只交付 MD 并说明。

## 4. 脚本

`scripts/stock_dive.py <CODE> [限定维度]`：输入后缀代码（如 `601138.SH`），自动探测数据目录并在 quantmind 容器内执行（脚本内自动 `docker cp` 自身），输出 JSON facts。无容器时脚本内部退化为 pyarrow 不可用提示。