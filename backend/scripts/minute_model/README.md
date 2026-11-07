# L2 分钟表（l2_min1）数据管线

> 2026-11-07 建立。目标：把 NAS 上的 L2 原始 parquet 落成 QuantDB 统一口径的分钟表，
> 供盘中/T+1 模型训练使用。

## 1. 数据源

| 仓库 | 路径 | 覆盖 |
|---|---|---|
| nas-toshiba | `/media/zbox/nas-toshiba/l2-warehouse/parquet` | 2023-01-03 ~ 2024-10-31（348 天，2023 年缺 4/8 月） |
| nas-wd | `/media/zbox/nas-wd/l2-warehouse/parquet` | 2024-04-01 ~ 2025-11-28（159 天，稀疏） |
| sata | `/media/zbox/sata/l2-warehouse/parquet` | 2024-11-01 ~ 2025-12-31（147 天） |

合并后 **654 个交易日**：2023 年 169 天（有缺口）、2024 年 242 天、2025 年 243 天
（2024-01 ~ 2025-12 连续完整）。

单日原始规模（16 个 shard）：
- `snapshot` 2,520 万行 × 72 列（10 档盘口 + 成交 + 累计量额）
- `trades` 1.24 亿行（逐笔成交，含 `is_valid=false` 的撤单记录）
- `orders` 1.87 亿行（逐笔委托）
- 压缩 parquet 合计约 6.2 GB/天

## 2. 事故与教训（2026-11-07）

第一版用 pandas 整天读入、4 进程并行，15:02 启动，15:03 触发**全局 OOM**：
4 个进程各占 2.7~4.0 GB 且持续增长，叠加生产栈打穿 60G 内存，机器冻结，
15:20 才重启。**一天都没落库。**

现版本全部改为 **DuckDB 列裁剪 + 哈希聚合 + 磁盘溢写**，并加了两道闸：
1. 每进程 `memory_limit`（默认 5GB）；
2. 开工前检查 `/proc/meminfo` 的 `MemAvailable`，低于阈值（默认 14GB）就等待。

单日实测：**约 112 秒、峰值 5.96GB（6GB 上限）、输出 124MB**。

## 3. 表结构与口径

输出：`data/quantdb/1_kline_data/l2_min1/dt=YYYYMMDD/data.parquet`

- 粒度：1 分钟一根，每股每日最多 **241 根**
  （09:25 集合竞价 + 09:31~11:30 + 13:01~15:00）
- 行数：约 123 万行/天，5,112 只股票（`prod='stock'`，剔除 B 股）
- 列数：**64 列**
  - OHLCV + amount（amount 单位**万元**）
  - `bid1..bid10` / `ask1..ask10`（元）
  - `bidv1..bidv10` / `askv1..askv10`（股）
  - `wavg_bid` / `wavg_ask` / `total_bidv` / `total_askv`（自行按档位加权，**不用 Wind 原始列**）
  - 逐笔成交分钟聚合：`buy_volume` / `sell_volume` / `buy_amount` / `sell_amount` /
    `large_*`（≥100 万）/ `xlarge_*`（≥500 万）/ `trade_n`
  - 逐笔委托分钟聚合：`order_n` / `big_order_n`（≥50 万）/ `cancel_n`
- 时间：分钟**收盘标记**（09:31 表示 09:30:00~09:30:59；15:00 含收盘集合竞价）
- 类型：除 symbol/time 外全部 float32，zstd 压缩

## 4. 清洗规则

1. 只留 `prod='stock'` 的沪深股票，剔除 B 股（SH 900xxx / SZ 200xxx）；
   ETF/可转债/基金/债券不进本表；北交所不在 L2 源内。
2. 时段只留 09:15~09:25、09:30~11:30、13:00~15:00；午休与盘前 09:26~09:29 丢弃。
3. 逐笔成交只取 `is_valid=true AND dir IN ('B','S')`；`is_valid=false` 的行
   价格/成交额全为 0，是撤单记录，不是成交。
4. `成交价<=0 / NaN / Inf` 不参与 OHLC；整分钟无有效价则 OHLC 为 NULL。
5. 申买/申卖价 `<=0` 视为无挂单（NULL）。
6. 分钟量额 = 当日累计差分，负差截断为 0。
7. Wind 的 `加权平均叫买价/叫卖价` 数值异常（曾见 10.24/11.01 而收盘 10.52），弃用，
   改由 10 档价格×量自行加权。

## 5. 验证（2024-04-02 实测，与 daily_unadjusted 逐股对数）

| 指标 | 中位相对误差 | p95 |
|---|---:|---:|
| 成交量（分钟求和 vs 日线） | **0** | 4.3e-8 |
| 成交额 | 2.3e-7 | 1.5e-6 |
| 收盘价（最后一根 vs 日线） | 2.0e-8 | 4.5e-8 |

覆盖：5,112 只中 5,111 只能对上日线；缺的 245 只是北交所（L2 源本身没有 BSE）。

## 6. 已知数据缺陷

1. **撤单字段基本为空**：`orders.is_cancel` 全表扫描恒为 False，`otype='1'` 占比
   <0.5%。`cancel_n` 保留但**撤单类特征不可靠**，训练时应剔除或单独标注。
2. **2023 年缺 4 月与 8 月**（169 天 / 应有 242 天）。
3. **B 股被剔除**，北交所无 L2。
4. 停牌股仍有快照（价格重复、量 0），分钟表原样保留，由使用方按 `volume=0`
   与日线成交量为 0 过滤。

## 7. 命令

```bash
# 单日探针（写到 _probe/，打印统计）
.venv/bin/python -m backend.scripts.minute_model.build_min1_l2 --probe 20240402

# 全量 / 区间（可断点续跑，已完成日期自动跳过）
bash scripts/build_l2_min1.sh 20240101 20251231
bash scripts/build_l2_min1.sh 20230101 20231231
bash scripts/build_l2_min1.sh --status

# 质检（逐日与日线对数，写 _qc/*.json）
.venv/bin/python -m backend.scripts.minute_model.qc_min1_l2 --all

# 14:50 盘中特征（每股每日一行，供训练）
.venv/bin/python -m backend.scripts.minute_model.build_intraday_features --all
```

预估：654 天 × 112 秒 ÷ 2 进程 ≈ **10 小时**；输出约 **80 GB**（1.1TB 空闲盘，够）。
