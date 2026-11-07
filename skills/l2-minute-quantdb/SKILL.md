---
name: l2-minute-quantdb
description: "L2 原始 parquet → QuantDB 分钟表（l2_min1）的落库、清洗、质检与 14:50 盘中特征全流程。单日原始 3.36 亿行（snapshot 2520万 / trades 1.24亿 / orders 1.87亿），DuckDB 聚合约 100-250 秒/天、122MB/天，64 列（OHLCV + 10 档盘口 + 逐笔成交/委托聚合）。凡需要把 NAS 上的 L2 数据落库/回补/重跑、检查分钟表完整性、与日线对数、或从分钟表构建盘中特征（14:50 快照、T+1 模型）时使用。触发词：L2 分钟、分钟表、l2_min1、分钟落库、L2 回补、分钟数据清洗、14:50 特征、盘中特征、分钟模型数据"
---

# l2-minute-quantdb — L2 分钟表落库与盘中特征

> 入口：`backend/scripts/minute_model/`。本技能只讲「怎么跑、怎么验、坑在哪」；
> 数据结构通则另见 `quantdb-data-structure`，字段口径另见 `quantdb-fields`。

## 0. 环境契约（先确认再动手）

| 项 | 值 |
|---|---|
| 执行位置 | **宿主机**仓库根 `/home/zbox/projects/quantmind`（容器里看不到 NAS 原始仓库） |
| Python | 仓库 venv：`.venv/bin/python`（pandas 3 / pyarrow / duckdb 1.5） |
| 原始仓库 | `/media/zbox/nas-toshiba/l2-warehouse/parquet`（2023-01~2024-10，348 天）<br>`/media/zbox/nas-wd/l2-warehouse/parquet`（2024-04~2025-11，159 天）<br>`/media/zbox/sata/l2-warehouse/parquet`（2024-11~2025-12，147 天） |
| 输出 | `data/quantdb/1_kline_data/l2_min1/dt=YYYYMMDD/data.parquet`（`data/` 是符号链接，git 忽略） |
| 覆盖 | 合并 **654 个交易日**：2023 年 169 天（缺 4/8 月）、2024 年 242 天、2025 年 243 天 |

**开工前必查**：`mount | grep nas-` 确认三个 CIFS 挂载在；NAS 掉线时读取会卡住而不是报错。

## 1. 数据源与覆盖

| 源 | 路径 | 覆盖 | 每天规模 |
|---|---|---|---|
| 7z 原始包 | `/media/zbox/nas-NSF/L2/{年}/{月或YYYYMM}/{YYYYMMDD}.7z` | 2017-01 ~ 2022-12（**1,311 天**；2022 仅 9-12 月） | 解压 7.8~16 GB、3,000~4,500 只 × 4 个 CSV |
| parquet 仓库 nas-toshiba | `/media/zbox/nas-toshiba/l2-warehouse/parquet` | 2023-01-03 ~ 2024-10-31（348 天，2023 缺 4/8 月） | snapshot 2520万 / trades 1.24亿 / orders 1.87亿行 |
| parquet 仓库 nas-wd | `/media/zbox/nas-wd/l2-warehouse/parquet` | 2024-04-01 ~ 2025-11-28（159 天，稀疏） | 同上 |
| parquet 仓库 sata | `/media/zbox/sata/l2-warehouse/parquet` | 2024-11-01 ~ 2025-12-31（147 天） | 同上 |


## 2. 命令（一分钟跑通）

```bash
cd /home/zbox/projects/quantmind

# 单日探针（写 _probe/，打印统计 + 量纲自检）
.venv/bin/python -m backend.scripts.minute_model.build_min1_l2 --probe 20240402

# 全量（守护脚本：2 进程 × DuckDB 5GB、断点续跑、崩溃自动重启、内存闸）
bash scripts/build_l2_min1.sh 20240101 20251231   # 先跑连续完整段
bash scripts/build_l2_min1.sh 20230101 20231231   # 再补 2023

# 2017-2022：7z 原始包（解压 + GBK CSV 直读，约 25-45 秒/天）
MIN1_BUILDER=7z MIN1_WORKERS=1 bash scripts/build_l2_min1.sh 20170101 20221231

# 进度 / 已落库天数 / 占用
bash scripts/build_l2_min1.sh --status
tail -f logs/minute_model/build_min1_20240101_20251231.log

# 逐日质检（与 daily_unadjusted 逐股对数）
.venv/bin/python -m backend.scripts.minute_model.qc_min1_l2 --all

# 14:50 盘中特征（只读 time<=14:50，无后视；每股每日一行）
.venv/bin/python -m backend.scripts.minute_model.build_intraday_features --probe 20240402
.venv/bin/python -m backend.scripts.minute_model.build_intraday_features --all
```

实测速率：parquet 版单进程约 100 秒/天、2 进程并行 200–250 秒/天（互抢 NAS）；
7z 版（解压+CSV 直读）约 25–45 秒/天，2017 约 25 秒、2020-2021 约 40-50 秒，
654 天约 **8–10 小时**，输出约 **80 GB**。

## 3. 表结构与单位（必须背下来）

`l2_min1` 每股每分钟一行，最多 **241 根**：09:25 集合竞价 + 09:31~11:30 + 13:01~15:00。
**时间 = 分钟收盘标记**（09:31 表示 09:30:00~09:30:59；15:00 含收盘竞价）。

| 组 | 列 | 单位 |
|---|---|---|
| 量价 | `open/high/low/close` | **元** |
| | `volume` | 股 |
| | `amount` | **万元**（与 `daily_*` 一致） |
| 盘口 | `bid1..bid10 / ask1..ask10` | **元** |
| | `bidv1..bidv10 / askv1..askv10` | 股 |
| | `wavg_bid / wavg_ask / total_bidv / total_askv` | 元 / 股（10 档自算） |
| 逐笔成交 | `buy_volume/sell_volume`（股）、`buy_amount/sell_amount`（万元） | |
| | `large_*`（单笔≥100万）、`xlarge_*`（≥500万） | 万元 |
| | `trade_n` | 笔 |
| 逐笔委托 | `order_n / big_order_n`（价×量≥50万）/ `cancel_n` | 笔 |

## 4. 清洗规则（写死在构建 SQL 里）

1. 只留 `prod='stock'` 的**沪深股票**，剔除 B 股（SH 900xxx / SZ 200xxx）；ETF/可转债/基金/债券不进本表；**北交所不在 L2 源内**。
2. 时段只留 09:15~09:25、09:30~11:30、13:00~15:00；午休与盘前 09:26~09:29 丢弃。
3. 逐笔成交只取 `is_valid=true AND dir IN ('B','S')`——`is_valid=false` 的行价格/成交额全为 0，是**撤单记录**，不是成交。
4. `成交价<=0 / NaN / Inf` 不参与 OHLC；申买/申卖价 `<=0` 视为无挂单（NULL）。
5. 分钟量额 = 当日累计差分，**负差截断为 0**。
6. **弃用 Wind 的 `加权平均叫买价/叫卖价`**（数值异常，曾见 10.24/11.01 而收盘 10.52），`wavg_*` 由 10 档价格×量自算。

## 5. 验收标准（对数不过就是没落成）

与同日 `1_kline_data/daily_unadjusted/dt=DATE/data.parquet` 逐股对比：

| 指标 | 合格线（实测） |
|---|---|
| 成交量（分钟求和 vs 日线） | 中位 **0**，p95 < 1e-7 |
| 成交额 | 中位 ~2e-7 |
| 收盘价（最后一根 vs 日线） | 中位 ~2e-8 |
| 每股分钟数 | 中位 **241**，最小可到 88（停牌/次新） |
| 重复 (symbol,time) | **0** |
| high<low / 负 volume | **0** |
| 盘口价 vs close 偏差 | 中位 ~0.03%，p99 ~0.5% |

覆盖差异：日线有、分钟无的约 245–247 只/天，**全部是北交所**（预期内）。

## 6. 已知缺陷（训练前必读）

1. **撤单字段基本为空**：`orders.is_cancel` 全表恒 False，`otype='1'` <0.5%。`cancel_n` 保留但**撤单类特征不可靠**，训练时剔除或降级为描述性。
2. **2023 年缺 4 月与 8 月**（169/242 天）。
3. 停牌股仍有快照（价格重复、量 0），分钟表原样保留；用时按 `volume=0` 或日线成交量为 0 过滤。
4. B 股剔除、北交所无 L2；指数不在 L2 源内（用 `index_daily`）。

## 7. 坑（都踩过）

- **单位 1e4**：快照的价格字段（成交价、申买/申卖价）都是**万分之一元**。只除成交价、漏除盘口价，会得到 `bid1=56600` 而 `close=5.66`——整套盘口特征全废。构建器已在落库前做量纲自检（bid1/wavg 相对 close 偏差 >0.2 直接拒绝落盘）。
- **pandas 全天读入必炸**：单日 3.36 亿行，单进程 10GB+；4 进程并行曾把 60G 主机打成全局 OOM、机器冻结（2026-11-07）。**只用 DuckDB**，内存上限 5GB/进程。
- **HHMM 不是十进制**：分钟右移到 bar 标签时 10:59→11:00、13:59→14:00，`minute+1` 会得到 1060/1360 非法时间。
- **`is_valid=false` 不是成交**（价格与成交额为 0）。
- **多进程崩溃后重启即可续跑**：按 `dt=*/data.parquet` 是否存在跳过；半成品 `.data.parquet.tmp` 会在下次开工时删掉。
- **内存闸**：开工前检查 `MemAvailable`，低于 14GB 就等待；不要去掉（生产机长期 oom-kill）。
- **「先拷后算」已证伪（2026-11-07 实测）**：把当天 6.2GB 整拷到本地 NVMe 只有 **~33MB/s**（130 秒拷 4.3GB），
  全天拷贝约 190 秒，再加本地聚合 ~80 秒——比原地读所需列（~2.1GB/天、240 秒含聚合）更慢。
  NAS 上的外置 USB 硬盘物理上限约 33MB/s，列裁剪已经比整拷省三分之二字节，**不要再走这条优化**。

## 8. 排障

| 症状 | 处理 |
|---|---|
| `未找到任何 L2 parquet 仓库日期` | NAS 没挂上：`mount \| grep nas-`，必要时重新挂载 |
| 某天 `rows=0` / `empty` | 源目录存在但无快照：查仓库该日文件数，缺数据就换仓库 |
| 某天 `fail:...量纲不一致` | 源数据单位变了：先 `--probe` 人工核对，别强行跳过 |
| 守护脚本连续 3 次无进展退出码 2 | 有坏日反复失败：看日志定位该日，单日 `--probe` 复现 |
| 构建很慢（>400 秒/天） | NAS 慢或并发抢占：把 `MIN1_WORKERS` 调回 1 |

## 9. 下游

- 14:50 盘中特征：`build_intraday_features.py` → `data/minute_model/daily/date=*/data.parquet`（约 45 列）。
- 训练面板还需叠加：日线上下文（昨收/均线/前 N 日收益）、截面分位、标签（T+1/T+2/T+3 收益分位）。
- 复权：分钟表是**未复权**价，与 `daily_unadjusted` 同口径；做标签或跨日收益时用 `daily_forward` 的复权因子换算。
