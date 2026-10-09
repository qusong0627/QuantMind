# JSON 报告契约（barra_risk.py）

两种模式共享同一顶层骨架；`--demo` 为纯标准库确定性小算例，`--quantdb` 为本地数据全链。NaN/inf 一律序列化为 `null`。stdout 打印人读中文表格，`--out` 额外写 JSON 文件。

## 顶层键

| 键 | 说明 |
|---|---|
| `skill` | 固定 `"barra-risk-model"` |
| `mode` | `demo` / `quantdb` |
| `market` | `CN` / `HK` / `US`（demo 为 `DEMO`） |
| `dataset` | quantdb：数据集名（CN 默认 `features_daily`，HK/US `l1_factors`） |
| `window` | `{start, end, data_through}`；`data_through` 为行情实际覆盖的最新分区 |
| `universe` | quantdb：`{method, asof_date, n_symbols, note}`；`--symbols` 时为 `method=explicit_symbols` |
| `data_sources` | quantdb：下表 |
| `model` | 下表 |
| `styles` | 实际入模的风格列表（顺序 `SIZE,VALUE,MOMENTUM,SHORT_REV,VOLATILITY,BETA,LIQUIDITY` 的交集） |
| `style_dropped` | 未启用风格 → 原因（如 HK/US VALUE「bp/pb 列实测全 0」） |
| `factors_dropped_zero` | 全窗恒零（未识别）被剔除的因子，如 `["IND:原材料"]`（quantdb） |
| `style_picked` | 列式风格的选取：`{SIZE: {column, transform, missing_parts}, VALUE: …, LIQUIDITY: …}`（quantdb） |
| `factor_stats` | 每个因子（含全部行业）：`{n, mean_daily, ann_ret, ann_vol, t}`；t = mean/(std/√n)（日度，未做 NW 校正） |
| `factor_cov` | 下表 |
| `style_corr` | `{recent_days, top_abs_pairs}`：最近 min(252,n) 日风格收益相关，按 \|ρ\| 取前 8 对 `"SIZE~LIQUIDITY": −0.78` |
| `specific` | 下表 |
| `attribution` | 下表 |
| `min_variance` | 下表 |
| `validation` | 下表 |
| `low_sample_warning` | 回归期数 < 252 为 true |
| `caveats` | 结论使用须知（口径 + 数据缺陷 + 本次运行的去重/剔除计数），全中文 |

## `data_sources`（quantdb）

| 键 | 说明 |
|---|---|
| `kline` | 行情路径 + 复权口径（CN 前复权；HK/US 不复权价格收益） |
| `kline_last_partition` / `factors_last_partition` | 行情/因子各自最新分区（US 实测因子落后行情） |
| `factors` | 因子数据集路径 |
| `industry` | 行业映射元信息（源文件、列、类数、note） |
| `size_note` / `liquidity_note` | 该市场 SIZE / LIQUIDITY 口径说明 |
| `dup_dropped` | 读取侧剔除的重复 (symbol,date) 行数（HK 双来源期实测数万行） |
| `n_nan_close` | 收盘价为空（停牌等）剔除行数 |
| `skipped_parts` | 因子分区因缺列被跳过的数量 |
| `n_bad_ret` | \|日收益\|>50% 按数据异常剔除的样本数 |

## `model`

`n_reg_periods`（有效回归期数）、`n_symbols`（末日截面）、`n_factors`（= n_styles + n_industries）、`n_styles`、`n_industries`、`median_names`（有效截面中位标的数/日）、`days_skipped`、`skip_reasons{no_data, too_few_names}`、`weight_scheme`（WLS 权重口径，含「真实市值」或「成交额代理」标注）。

## `factor_cov`

`method`（`ledoit_wolf` / `ewma`）、`n_obs`、`kappa`（LW 收缩强度，ewma 时为 null）、`halflife`（ewma 时有效）、`annualized_cov`（K×K 年化）、`factors`（与 `annualized_cov` 行列对齐的因子名）、`min_eigval`（PSD 自检，≥0 通过）、`asset_cov_invertible`（N×N 资产协方差可逆自检）。

## `specific`

`halflife`（EWMA 半衰期，默认 60 日）、`n_symbols`、`n_filled_with_median`（样本不足用中位填充数）、`median_ann_vol` / `p10_ann_vol` / `p90_ann_vol`（年化特异波动分位）、`symbols{sym: {ann_vol}}`（逐股）。

## `attribution`

`portfolio`（组合标签：等权 / CSV 路径 / demo 固定权重）、`n_symbols`、`n_weights_dropped`、`total_vol_ann`、`factor_var` / `specific_var`（年化方差）、`pct_factor` / `pct_specific`、`factor_table[]`（按 \|方差贡献\| 降序：`{factor, exposure, var_contribution, pct_of_total_var, factor_ann_vol}`）。

## `min_variance`

Woodbury 极小方差组合验证：`total_vol_ann`、`equal_weight_vol_ann`、`vol_ratio`（= MV/EW，真 Σ⁻¹1 解必 ≤1）、`n_symbols`、`n_short`（负权重只数，无约束解允许做空）、`gross_leverage`（Σ\|w\|）、`le_equal_weight`（Cauchy–Schwarz 自检布尔）、`note`。极端数值退化时退化为 `{"error": "…"}`（stdout 同步提示，不中断报告）。

## `validation`

| 键 | 说明 |
|---|---|
| `n_sampled_days` | 抽样对拍天数（`--validate`） |
| `solver_max_dev_vs_lstsq` | 因子收益：numpy 正规方程 vs lstsq（SVD 路径）最大偏差 |
| `solver_max_dev_vs_stdlib` | 同语义两实现（numpy vs 纯标准库高斯消元）最大偏差 |
| `kappa_dev_py_vs_np` | LW κ 纯 Python vs numpy 偏差 |
| `identity_residual_rel` | \|factor+specific−total\|/total |
| `direct_quadform_residual_rel` | 显式 N×N Σ 二次型 vs 分解式相对偏差 |
| demo 独有 | `demo_exact_recovery_max_dev`（无噪声段真值还原）、`demo_noisy_half_max_dev`、`demo_closed_form_1factor_dev`（单因子闭式手算）；quantdb 的求解/κ 三键为 `null` |

## demo 与 quantdb 的字段差异

- demo 无这些顶层键：`dataset` / `universe` / `data_sources` / `style_picked` / `factors_dropped_zero` / `low_sample_warning`。
- demo 的 `specific` 无 `n_filled_with_median`；`min_variance` 无 `n_symbols` 与 `note`；`validation` 的 `solver_*`/`kappa_*` 为 null、另有三个 `demo_*` 键。
- `attribution.portfolio` 为固定非均匀权重的文字说明。

渲染与下游消费请对差异键做缺省处理（脚本内部即如此）。

## 退出码与错误语义

| 情形 | 行为 |
|---|---|
| 正常输出 | exit 0（stdout 表格 + 可选 JSON） |
| `--list-columns` | 打印列探查后 exit 0（不产报告） |
| 窗口/目录缺失、start>end | stderr 中文错误，exit 1（如「行情无 20240101~20270101 分区」） |
| 回归期数 < 60 | 拒绝输出：「回归期数过少（23 < 60），拒绝输出不可靠模型」 |
| 全部交易日均截面不足 | 「票池过小：全部 N 个交易日截面标的数 < 因子数+2…」 |
| 票池 symbol 格式错误（前缀式） | 「票池在行情窗口中无数据（检查 symbol 格式：…）」 |
