# 输出契约（output-contract）— schema `quantmind-tca/1`

stdout 是中文表格（人读）；**JSON 是消费面**（Agent/脚本读）。JSON 仅在给定 `--out` 时落盘，父目录自动创建；
`generated_at`（本地时间字符串）与 `schema: "quantmind-tca/1"` 在写盘时注入，不参与计算。

## 数值与符号约定

- **正负**：五分解 bps **正=对本方不利**；逐笔滑点正=劣于基准（负=价格改善）；卖出方向已含方向符号。
- **舍入**：bps 类 4 位小数（`r4`，消除 `-0.0`）；参与率 8 位；价格均值/基准价 6 位；σ 6 位；比率 4 位。
- **空值**：NaN/Inf → `null`（`json_safe`）；`Path` → 字符串。
- **单位**：价格 元/股；量 股；费 bps；金额字段保留原表单位并在字段名标注（如 `minute_amount_wan` 万元）。

## 质量字段语义（三种模式共有或各自的）

| 字段 | 语义 | 消费方式 |
|---|---|---|
| `warnings[]` | 数据可疑（如逐笔滑点 |x|>500 bps 的量纲/口径告警） | 出现即先核对数据再采信结论 |
| `degraded[]` | 代理口径说明（到达价/期末价/σ 缺省回填、ADV 外推等） | 结论旁必须注明 |
| `caveats[]` | 静态口径提醒（分钟近似、点差代理 ≈5× 高估等） | 原样带入报告 |
| `handcheck` | demo 手算对账 12 项；`all_ok=false` 时脚本直接非零退出 | — |

## `orders[]`（三种模式同构）

```
symbol, side, date, fills_n,
prices:  {decision, arrival, avg_fill, end}          # 元/股；avg_fill=Σp·q/Σq
qty:     {order, filled, fill_ratio}                 # 股
market:  {adv, sigma_day, participation, spread_proxy_bps}
five_way_bps: {delay, impact, timing, opportunity, fees, total,
               residual_bps, five_sum}               # 订单名义额@决策价 bps
fee_detail:   {commission_bps, stamp_duty_bps, transfer_fee_bps}
degraded:     [str, ...]
```

`residual_bps`=独立算术路径（direct）− total，正常恒为 0（对账项）；`five_sum`=五项直接相加。

## 各模式顶层

### `--demo`

```
mode:"demo", bars_n,
benchmarks: {vwap_all, vwap_continuous, twap_continuous, arrival_open,
             spread_proxy_bps:{mean_bps, median_bps}},
orders[], benchmark_compare{...}, handcheck:{all_ok, items:[{order,item,expected,actual,ok}]}
```

### `--input`

```
mode:"input", input(绝对路径), benchmark, fee_profile,
params: {k, window_minutes, commission_bps},
orders[], aggregate: {delay, impact, timing, opportunity, fees, total},   # 按订单名义额加权
benchmark_compare: {kind, window_minutes,
                    per_order:[{symbol, side, mean_slippage_bps, beat_rate,
                                per_fill:[{datetime, price, qty, benchmark_price, slippage_bps}]}]},
calibration: null
           | {status:"insufficient", n_fills, note}          # 样本<5 或 参与率无方差
           | {status:"ok", n_fills, slope_bps_per_sqrt_part, intercept_bps, r2,
              participation_min, participation_max,          # 观测区间，勿外推
              implied_k?, sigma_day_used?, note},
warnings[], degraded[], caveats[]
```

### `--quantdb`

```
mode:"quantdb", symbol, date, freq, benchmark, fee_profile,
params: {k, side, qty, adv_days},
day_profile: {
  benchmarks: {vwap_all, vwap_continuous, twap_continuous, arrival_open,
               spread_proxy_bps:{mean_bps, median_bps},
               sigma_day_today, sigma_day_prev20, adv_prev20},
  buckets: [{bucket, n_bars, volume, volume_share, vwap}],   # 半小时桶
  uniform_exec_scenario: null | {side, exec_price_assumption, drift_vs_vwap_all_bps,
      fees_bps, fee_detail, spread_proxy_median_bps?,
      qty?, adv_use?, participation?, sigma_use?, sigma_source?, impact_bps?,
      total_est_bps_ex_spread?, total_est_bps_incl_spread_proxy?}},   # 给定 --qty 才有冲击段
reconciliation: {day,
  bars: {total, canon, continuous, anomalous:[{hhmm, session, volume}]},
  vs_daily_unadjusted: null | {minute_volume, daily_volume, volume_dev_pct,
      minute_amount_wan, daily_amount_wan, amount_dev_pct,
      vwap_minute, vwap_daily, vwap_in_low_high, daily_low, daily_high},
  degraded?},
tick_check: null | {tick:{n_ticks, median_bps, mean_bps, p25_bps, p75_bps},
                    minute_proxy_median_bps, proxy_over_half_spread_x},
caveats[]
```

- `reconciliation.vs_daily_unadjusted=null` 且带 `degraded`：该日 `daily_unadjusted` 无此标的（跳过对账）。
- `tick_check` 仅在 `--tick-check` 且该标的日有 tick 快照时非空。

## 退出码

- `0`：正常完成（可含 warnings/degraded）。
- 非 0（`SystemExit`）：市场无分钟数据（HK/US）、日期无覆盖、文件缺失、demo 手算校验失败（错误信息内嵌逐项 JSON）等。

## 报告命名建议

容器内 `/data/reports/transaction-cost-analysis/<market小写>_<SYMBOL>_<date>[_标记].json`，
如 `cn_600036.SH_2026-08-25.json`；重跑保留前后两份以对比（本目录已有 2026-10-08 标定样例）。
