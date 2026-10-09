# 输出契约（JSON / Markdown）

移植自源技能的报告契约，本地化为 QuantDB 直读 + 离线复核两种模式。核心纪律：**「已证实的问题」与「缺失证据」分开报告**；缺失一律显式降级，不补写估算值。

## JSON 报告（脚本 `--out`）

顶层字段：

| 字段 | 含义 |
|---|---|
| `skill` | 固定 `cross-listing-parity` |
| `mode` | `offline`（`--demo`/`--input`）或 `quantdb` |
| `status` | `pass` / `warning` / `fail` / `insufficient-evidence`（语义见下） |
| `asof_date` | 报告数据日（绝对日期） |
| `generated_at` | 生成时间（UTC） |
| `fx` | `{hkd_cny, source, direction}`；source 记录「数据集分区 / user:--fx / input」 |
| `universe` | （quantdb）配对表行数、去重后配对数、内重行数、数据集末日、本地行情最新共同日 |
| `data_sources` | 各字段实际读取路径 |
| `formula` | 固定公式字符串 |
| `summary` | 截面统计：计算配对数、中位/均值/P5/P95、A>H 与 A<H 计数、>100pp 极端计数 |
| `pairs` | 目标日全配对明细（见下） |
| `top` / `bottom` | 溢价最高 / 折价最深各 N 条（按 `premium_pct` 排序） |
| `crosscheck` | 交叉校验块（见下） |
| `quality_findings` | 数据质量与降级清单（findings 数组） |
| `limitations` | 本次运行的已知限制（固定 + 运行态） |
| `disclaimer` | 免责声明原文 |

`pairs[]` 明细字段：`a_symbol, h_symbol, name, a_close, h_close, h_release, fx_hkd_cny, ratio, premium_pct, premium_pct_dataset, diff_pp, window_days, pct_rank_window, series_stats{n,mean,std,min,max}`。

- `premium_pct` 一律为**原始价重算**值；`premium_pct_dataset`/`diff_pp` 只在数据集覆盖该日时出现（缺失即字段缺省，不是 0）。
- `h_release` 记录该 H 收盘价来自哪个 release（`akshare` / 其他），便于回溯双来源问题。
- `pct_rank_window` / `series_stats` 基于重算序列的窗口；`window_days < window` 说明历史不足（见 `short_history_coverage`）。

`crosscheck` 块：

| 字段 | 含义 |
|---|---|
| `asof_pairs_total` / `asof_pairs_computed` | 配对表总对数 / 目标日可计算对数 |
| `dataset_pairs_on_target` | 数据集目标日分区内的配对数（0=该日无分区） |
| `rebased_vs_dataset_n` | 目标日逐对可比条数 |
| `rebased_vs_dataset_max_abs_diff_pp` | 目标日重算 vs 数据集最大 |Δ|（基期当日应为 0.0） |
| `rebased_vs_dataset_over_tol` | 超过 0.05pp 容忍的配对数 |
| `dataset_formula_max_abs_diff_pp` | 数据集列间公式复现最大差（应恒 0.0） |
| `window_rebased_vs_dataset_mean_abs_diff_pp` | 窗口内（剔除基期）单元格加权平均 |Δ|——量化 A 侧前复权效应 |
| `window` | `{days, start, end}` 实际窗口 |

离线模式的 `crosscheck`：`rows_used` / `asof_rows` / `input_premium_max_abs_diff_pp`。

## findings（`quality_findings[]`）

字段：`id`（稳定语义标识，如 `price_missing`）、`severity`、`kind`（分类：`mapping`/`dataset`/`fx`/`market`/`coverage`/`crosscheck`/`input`/`formula`）、`detail`、`evidence`（可选）、`recommended_fix`（可选）。

| severity | 含义 | 处置 |
|---|---|---|
| `high` | 已证实的数据/口径错误（如数据集公式不自洽） | 必须人工核查后才能引用结论 |
| `medium` | 影响解读的口径差/缺失（价格缺失、前复权偏差、双来源分歧） | 报告中必须披露 |
| `low` | 可自动处理的清理项（重复行、配对表内重、历史不足） | 记入数据说明 |
| `info` | 降级/来源声明（用户汇率、数据集滞后、缺汇率） | 记入数据说明 |

id 清单（quantdb）：`membership_duplicates`、`premium_row_duplicates`、`dataset_formula_mismatch`、`fx_user_supplied`、`fx_missing`、`price_missing`、`short_history_coverage`、`rebased_vs_dataset_divergence`、`a_side_adjustment_bias`、`h_source_disagreement`、`dataset_stale`。
离线：`input_empty`、`missing_columns`、`input_rows_skipped`、`no_valid_rows`、`input_premium_mismatch`。

evidence 形态举例：`price_missing` → `{missing_a_symbols, missing_h_symbols}`（缺失代码清单）；`h_source_disagreement` → `[{dt, symbol, akshare, paid_hk, rel_diff}]`；`a_side_adjustment_bias` → 窗口内 |Δ| 最大的一批 `{dt, h_symbol, a_symbol, premium_pct, rebased_pct, delta_pp}`。

## status 与退出码

- `fail`：存在 `high` finding。
- `insufficient-evidence`：无可用汇率（`fx_missing`），未计算溢价。
- `warning`：存在 `medium` finding（本地 QuantDB 常态——数据集口径差与价格缺失会命中）。
- `pass`：其余。
- 退出码：`pass`/`warning` → 0；`fail`/`insufficient-evidence` → 1。统计/脚本编排据此判定。

## Markdown 报告（`--md`）

必需章节（`scripts/validate_report.py` 强制）：

1. `## 摘要` — 数据日、汇率、可计算配对数、截面概况。
2. `## A/H 溢价排行` — 溢价最高与折价最深两个榜（含窗口分位）。
3. `## 极值与历史分位` — 极端值与分位口径说明。
4. `## 数据说明` — 数据来源、数据日、汇率来源、ratio/映射表版本、T+1/snapshot 声明、不可计算配对清单。
5. `## 免责声明`。

校验器同时要求全文含：数据来源说明、数据日、汇率来源、snapshot/快照/T+1 声明、ratio/股数比/映射表版本、非投资建议声明；并**禁用操作性表达**（建仓/减仓/加仓/止盈/止损/目标价数字/建议买卖/推荐买卖）。

校验器输出 `OK`（退出 0）或 `FAIL` + 逐条问题（退出 1；文件不存在退出 2）。只校验结构与声明，不判断结论对错——数字仍需人工核对数据日、汇率与配对表版本。

## 纪律

- 缺失显示为字段缺省/null 或 finding，**绝不写成 0**（0 是有含义的溢价值）。
- 报告写绝对日期，不写「今天/昨天」。
- 数据日、汇率日期、映射表版本三者任一变化时，跨报告比较必须重新声明。
