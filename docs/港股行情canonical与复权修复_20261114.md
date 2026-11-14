# 港股行情 canonical 收口与复权链修复（2026-11-14）

> 范围：`quanthk/1_kline_data/daily_forward`、`daily_backward`、
> `2_base_sector/adjust_factors`。目标：一个股票日只有一行行情；复权序列无
> `inf`、无伪拆并股跳变；所有改动可回滚。

## 1. 问题一：daily_forward release 重叠

2024-09-02~2026-05-08 的 411 个分区里，同一 `(symbol, time)` 同时存在两批
release：

| release | 行数 | 覆盖 | published_at 示例 |
|---|---:|---|---|
| `paid_hk`（付费历史，原始不复权） | 9,992,551 | 1980-01-02~2026-05-08 | 2026-08-09 |
| `akshare`（补充/新数据） | 1,149,338 | 2024-09-02~2026-11-13 | 2026-08-24 |

重叠 688,942 个股票日。**不是纯重复**：akshare 历史对拆并股做了回溯调整，
与付费原始价不同（0755.HK 2024-12-06 paid=1.3、akshare=0.013，差 100 倍；
0501/0100 在 2026-05-08 的伪 3400×/113× step 也源于此）。直接
`read_parquet` 会让成交量/成交额翻倍，`keep="last"` 又会随机选中 akshare 的
1/100 价格。

### 收口规则

`backend/shared/kline_canonical.py`：

- 每个 `(symbol, time)` 只留一行；
- release 优先级：`paid_hk` > `akshare` > 未知；
- 同一 release 内按 `published_at` 最新；
- 选择 paid 优先是**最小破坏**：付费历史是既有研究序列，akshare 只补
  paid 未覆盖的日期/标的。

### 执行结果

`backend/scripts/quanthk_canonicalize_kline.py --apply`：

- 变更 411 个分区，移除 688,942 行（全部 `akshare` 重叠行）；
- `daily_forward`：**10,452,947 行 = 10,452,947 个唯一个股票日，重复 0**；
- release 余量：paid 9,992,551 + akshare 460,396；
- 11-13 仍为 2,165 只，未改最新数据。

备份/审计：

- `1_kline_data/daily_forward_backup_20261114/`（全量原始分区）
- `1_kline_data/daily_forward_release_backup/dt=*`（被移除的 akshare 行）
- `1_kline_data/daily_forward_canonical_manifest.jsonl`
- `1_kline_data/daily_forward_canonical_summary.json`
- 回滚：`python backend/scripts/quanthk_canonicalize_kline.py --restore --apply`

### 写入端护栏

以下脚本合并分区时统一走 `canonicalize()`，paid 历史不会被后续 akshare
增量覆盖：

- `backend/scripts/akshare_kline.py`
- `backend/scripts/quanthk_akshare_kline.py`
- `backend/scripts/quanthk_import_paid_hk.py`

## 2. 问题二：复权因子 inf / 伪 step

实测 `adjust_factors` 有 **836,916 行 `adj_factor=inf`**，
`daily_backward` 有 **817,621 行 `close=inf`**。根因：

- 源侧某些日期给出 inf/0 step（2018-08-08、2020-03-03、2020-04-21 等
  多只股票同日出现，属 Yahoo/付费源的数据毛刺），cumprod 后整段污染；
- 0501.HK 2026-05-08 step=3400，但 canonical 收盘仅 89.8→87.6（-2.4%），
  step 与行情不一致；
- 0755.HK 2020-04-21 step=100 且 canonical 也是 100 倍，属真实合股，应保留。

### 修复规则

`backend/shared/adjust_factor_repair.py`：

- step 非有限/≤0 → 1.0；
- step 极端（>10 或 <0.1）时，必须与 **canonical 前收比**一致（0.2~5 倍内），
  否则视为源噪声 → 1.0；
- 真实拆并股保留；
- 以原因子首个有限值为基准重算 cumprod，防止量级漂移。

### 执行结果

`backend/scripts/quanthk_adjust_factor_repair.py --apply`：

- 修复 617 只因子文件、50,784 个 step；
- `adj_factor` inf：836,916 → **0**；
- 重建 11,643 个 `daily_backward` 分区（10,414,703 行）；
- `close` inf：817,621 → **0**；null 0；
- 0501.HK 2026-05-08 backward 80.4（前一日 82.4，连续）；
- 0100.HK 2026-05-08 backward 403.3（前一日 445.7，连续）；
- 剩余 7,321 行 `close<=0` 全部 ≤2020-04-20，是停牌/无成交源数据 0 价，
  研究池（`close_raw≥0.5`）不会选中。

备份/回滚：

- `2_base_sector/adjust_factors_backup_20261114/`
- `1_kline_data/daily_backward_backup_20261114/`
- 回滚：`python backend/scripts/quanthk_adjust_factor_repair.py --restore --apply`

## 3. 下游读取端护栏

- `build_ml_signal_datasets._read_kline_day`：先 canonicalize 再按股票聚合，
  防止成交额/成交量翻倍；
- `ccass_factor_panel._read_kline`：先 canonicalize 再取单行；
- `quanthk_adjust_factor._rebuild_day`：先 canonicalize 再乘因子，避免
  `keep="last"` 选中 akshare 的 1/100 价格。

## 4. 验收命令

```bash
# 1) 一个股票日一行
python - <<'PY'
import duckdb
p="/data/quanthk/1_kline_data/daily_forward/dt=*/data.parquet"
con=duckdb.connect()
print(con.execute(f"select count(*), count(distinct symbol||'|'||cast(dt as varchar)) from read_parquet('{p}', hive_partitioning=1)").fetchone())
print(con.execute(f"select count(*) from (select symbol,time,count(*) c from read_parquet('{p}', hive_partitioning=1) group by 1,2 having c>1)").fetchone())
PY

# 2) 复权无 inf
python - <<'PY'
import duckdb
p="/data/quanthk/1_kline_data/daily_backward/dt=*/data.parquet"
con=duckdb.connect()
print(con.execute(f"select sum(case when isinf(close) then 1 else 0 end) from read_parquet('{p}', hive_partitioning=1)").fetchone())
PY
```

## 5. 已知边界

- akshare 港股的“历史不复权”实际上会对拆并股回溯调整；本收口只在 paid
  未覆盖的日期使用 akshare，并把 paid 重叠段固定为原始价。
- 2026-05-09 之后的 akshare 数据按日冻结，不再回溯重写；若之后发生拆并股，
  新的增量日会带源侧调整，需在下一个因子修复周期复核。
- 恒生指数成分股名单未落库；因子回测的“大市值池”用逐日市值 Top100 代理。
- 港股通名单是当前快照（`hsgt_membership.parquet`），不是 PIT；南向池结果
  只用于横截面稳健性参照，不当作严格 PIT 回测。
