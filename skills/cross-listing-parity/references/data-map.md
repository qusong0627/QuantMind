# 本地数据映射与实测标定（QuantDB）

全部数字为 2026-10-07/08 在本机 QuantDB 上实测（容器内 pandas/duckdb，窗口读取显式文件列表 `hive_partitioning=false`，避免 dt 列遮蔽分区裁剪）。

## 1. A/H 配对表 `quanthk/2_base_sector/ah_membership.parquet`

- 列：`h_symbol, a_symbol, 名称, source, updated_at`。
- 规模：159 行 / 149 对；10 组 (h,a) 键重复（各多 1 行），脚本按 (h_symbol,a_symbol) 去重并报 `membership_duplicates`。
- 生成：`backend/scripts/quanthk_extra_datasets.py` 用 akshare `stock_zh_ah_name` 名称匹配 A/H 代码。
- **已知缺口（名称匹配失败，不在表内）**：
  - 比亚迪 `002594.SZ` / `1211.HK`
  - 中国石油 `601857.SH` / `0857.HK`
  - 中国石化 `600028.SH` / `0386.HK`
  报告对这三家如实注明「不在配对表内」，不要手工补对（补对后公式仍成立，但配对表版本声明要改）。
- 验证方法：`pd.read_parquet(...)` 后 `groupby(["h_symbol","a_symbol"]).size()` 看重复。

## 2. 溢价数据集 `quanthk/2_base_sector/ah_premium/dt=YYYYMMDD/data.parquet`

- 列：`h_symbol, a_symbol, a_close, h_close, fx_hkd_cny, premium_pct`（+分区列 `dt`）；2018-01 分区列名已一致。
- 分区：2039 个交易日分区，2018-01-02 → 2026-08-27（构建时点）；**日期 = 两市同日交易且汇率存在**，H 假期缺分区。
- `premium_pct` 公式核对：`(a_close/(h_close*fx_hkd_cny)-1)*100`，**ratio 隐含=1**；全窗口 max diff = 0.0（脚本每次运行复验该列间公式）。
- **`a_close` 是构建时点前复权价（基期=最后一个分区日 2026-08-27）**——这是本数据集最大的口径坑：
  - 基期当日与原始价重算**逐对完全一致**（149/149，max diff 0.0pp）。
  - 越往回偏差越大（分红累计效应）：250 交易日窗口（2025-08-07~2026-08-27，剔除基期）平均 |Δ|=1.92pp；60 交易日窗口至 2025-01-15 平均 |Δ|=7.58pp，当日 88/101 对 >0.05pp、最大 25.52pp（如 2238.HK 当日重算 205.0362 vs 数据集 204.3235）。
  - 结论：**历史分位只从原始价重算序列取**；数据集 `premium_pct` 仅用于（a）交叉校验、（b）汇率源、（c）重复行证据。
  - 验证方法：同窗口对 `daily_unadjusted` 原始 A 收盘价重算，按数据日看 |Δ| 随距基期时间递减（脚本 `a_side_adjustment_bias` finding 输出 top 分歧）。
- **重复行**：(日期,配对) 级重复，最近 250 分区共 12523 组，**组内取值全部相同（0 组分歧）→ 按值去重安全**。分布：2025-08~12 约 90~110 组/日；2026-01 起固定 ≈10 组/日，且这 10 组键集合与配对表 10 组内重**逐一相同**（构建器按配对表展开所致）。脚本每次运行统计并报 `premium_row_duplicates`。

## 3. A 侧原始价 `quantdb/1_kline_data/daily_unadjusted/dt=YYYYMMDD/`

- 列含 `symbol`（后缀式 `601318.SH`）、`close` 等；分区至 2026-09-30（本地行情侧）。
- 本技能只取 `symbol, close`；读侧按 (dt,symbol) 去重。

## 4. H 侧价格 `quanthk/1_kline_data/daily_forward/dt=YYYYMMDD/`

- 列：`symbol`（四位+.HK）、`close`、`release_id`、`volume` 等；**该表是不复权原始价**（本地 H 股没有可用的复权序列，`daily_backward` 不可用）。
- **双来源重复行：2024-09-02 ~ 2026-05-08**，同一 (日期,标的) 有 `akshare` 与 `paid_hk` 两个 release 的行。标定结论：
  - `paid_hk` 对部分标的施加过复权/缩放，**不是原始成交价**：1211.HK 86.266663 vs akshare 258.799990（≈1/3）、0788.HK 11.0 vs 1.1（=1/10）、3939/9900/2038 同类（volume×price 量级自洽，非脏数据，是口径差异）。
  - **去重优先 akshare**；两来源相对差 >0.5% 的 (日期,标的) 单列 `h_source_disagreement`（2025-01-15 的 60 日窗口内 20 例）。
  - 验证方法：按 (dt,symbol) pivot akshare vs paid_hk 算相对差；对可疑标的核对成交额量级。
- H 侧价格缺失：2026-09-30 的 2899.HK；2025-01-15 窗口 H 侧缺 48 只（当时尚未上市）。缺失一律进 `price_missing`，不插值。

## 5. 汇率

- 本地**没有独立的汇率数据源**（`data/` 未检索到 usdcny/hkd 序列，QuantUS macro 数据不含 HKD/CNY 日频）。
- 唯一来源 = 数据集列 `fx_hkd_cny`：构建时 `ak.currency_boc_sina(symbol="港币")["中行折算价"]/100`，每天一个值（中行折算价），方向 1 HKD = fx CNY。
- 数据集止于 2026-08-27：更晚日期必须 `--fx`（用户提供并声明来源/日期），否则 `fx_missing` 降级（只见原币价格，不算溢价）。
- 验证方法：`prem.groupby("dt")["fx_hkd_cny"].nunique()` 每分区恒 1 个值；与报告声称的方向对照数量级（0.8~1.0 之间）。

## 6. 日期与缺口常识

- 数据集有分区 ≠ 本地行情有分区：本地 CN 行情到 2026-09-30、数据集到 2026-08-27；跑更晚日期会同时触发 `dataset_stale`（提示命令）与 `fx_user_supplied` / `fx_missing`。
- 缺分区日（H 假期等）不插值：目标日无分区时 `--date` 仍可用于本地行情重算（需 `--fx`），但数据集交叉校验自然为空（`dataset_pairs_on_target=0`）。
- 容器时钟比宿主慢约 1 小时；报告 `generated_at` 以容器时间为准，数据日以分区日为准，两者不相干。
