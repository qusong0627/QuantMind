# 输出契约 — stdout 裁决卡与 JSON 报告

## stdout 裁决卡（渲染顺序固定）

```
================================================================
因子质量裁决卡 — <factor>（<market>/<dataset 或 csv/demo>）
================================================================
窗口：<start> → <end> ｜ 交易日 N ｜ IC 观测 M
----------------------------------------------------------------
① 票池构造检查   [ok|partial]
   • 截面宽度 / 覆盖 / 停牌零成交行 / ST / 上市天数 / findings
② 时点对齐检查
   • 同日口径 IC / 次日执行口径 IC / 效力保留（或「同日 IC 近零，保留率略」）
③ IC/IR（简版，H=<h>）
   • 均值 IC / ICIR年化 / NW-t / 方向命中率 ｜ 分段 x3
④ 换手与成本（日频调仓，单边 <c> bp）
   • 可执行端（高|低分位）：毛 / 换手 / 净（N 个交易日）；理论多空净
⑤ 中性化残差（行业+规模）
   • 行业方差解释 / 与规模相关 ｜ 残差 IC / 保留率
----------------------------------------------------------------
裁决：<裁决中文>（方向：正|负向）｜ 置信：高|中|低
判据：<reason>
   • evidence 逐条
保留意见 / 反证：
   - caveats 逐条
（研究证据描述，不构成投资建议。）
```

## 裁决枚举

| label | 中文 | 触发（先到先得） | 置信度 |
|---|---|---|---|
| `leakage` | 泄漏（疑似未来函数） | 名称规则命中标签黑名单，或 \|IC\| ≥ `leak_ic` | 恒「高」 |
| `sample_illusion` | 样本幻觉（与噪声不可区分） | 显著性未过线或 \|IC\| < `weak_ic` | n≥252 且 \|t\|<1.0 → 高；否则中 |
| `industry_exposure` | 行业暴露（信号主要是行业/规模下注） | 中性化保留率 < `neutral_retention` | 保留率 <阈值一半 → 高；否则中 |
| `alpha` | alpha（截面选股信号） | 其余（过五道检查） | 从「高」按降档规则累减 |

## JSON 报告字段（`--out`）

顶层：

| 字段 | 说明 |
|---|---|
| `mode` | `quantdb` / `csv` / `demo`（demo 为 `{"mode":"demo","reports":[...]}` 四份合集） |
| `market` / `dataset` | 如 `CN` / `CN/features_daily` |
| `name` / `factor_column` | 因子显示名 / 实际列名（裁决的黑名单匹配用后者） |
| `n_days` / `date_start` / `date_end` | 面板天数与窗口 |
| `window` / `horizon` | 请求窗口与 IC 主口径前视周期 |
| `returns` | `source`、`adjustment`（前复权/不复权说明）、`definition`（两口径公式） |
| `size_column` | 规模中性化实际用的列（未用则 null） |
| `universe` | 票池元信息（= `checks.universe` 的拷贝） |
| `checks` | 五道检查明细，见下 |
| `verdict` | 裁决块，见下 |
| `caveats` | 保留意见列表（固定项 + 各检查降级项） |

`checks` 子块：

| 路径 | 关键字段 |
|---|---|
| `checks.universe` | `method`（top_N_by_amount/explicit_symbols/csv/synthetic_demo）、`asof_date`、`n_symbols`、`cross_section{median_names,min_names,days_total}`、`coverage_pct`、`suspended_like{rows,pct}`、`st_filter{...}`、`listing_age{available,note}`、`findings`、`status` |
| `checks.pit` | `ic_same_day`、`ic_next_day_start`、`retention`（近零时为 null）、`retention_note`、`n` |
| `checks.ic` | `n`、`mean_ic`、`std_ic`、`icir_raw`、`icir_ann`、`hit_rate`、`nw_t`、`low_sample_warning`、`segments[{start,end,n,mean_ic}]` |
| `checks.turnover_cost` | `n_days`、`gross_side_bp`、`cost_side_bp`、`net_side_bp`、`gross_ls_bp`、`cost_ls_bp`、`net_ls_bp`、`turnover_side_pct`、`turnover_ls_pct`、`breakeven_side_one_side_bp`、`net_side_ann_pct`、`round_trip_note` |
| `checks.neutralization` | `enabled`、`industry_used`、`size_used`、`industry_r2_median`、`size_corr_median`、`summary{...}`（同 checks.ic 结构）、`retention` |

`verdict` 子块：`label`、`label_zh`、`confidence`（高/中/低）、`direction`（正向/负向）、`direction_note`（可执行多头端提示）、`reason`、`criteria`（如显著性判据说明）、`evidence`（证据数字列表）、`thresholds`（本次生效的全部阈值快照，含 `leak_ic`/`sig_t`/`neutral_retention`/`quantile`/`cost_bp`/`nw_lag` 等）。

## demo 断言契约（`--demo` 必须全部通过，否则退出码 1）

| 断言 | 面板构造 | 期望裁决 |
|---|---|---|
| `mystery_signal` | 因子 = 未来收益本身（把标签当因子传入） | `leakage` |
| `random_noise` | 纯随机噪声 | `sample_illusion` |
| `sector_beta_proxy` | 收益只由行业位移驱动 | `industry_exposure` |
| `sector_beta_proxy#retention` | 同上，中性化保留率检查 | < 0.30 |
| `idio_alpha_demo` | 行业内生、可扣费的真信号 | `alpha` |
| `idio_alpha_demo#cost` | 同上，扣费后仍为正 | 净 > 0 |

合成面板由固定种子（`seed=20261008`）生成，结果逐位可复现。

## 退出码

| 码 | 含义 |
|---|---|
| 0 | 正常（或 demo 全部断言通过） |
| 1 | demo 存在失败断言 |
| 2 | 拒绝裁决（因子列 60 天 IC 观测不足 / 常量列 / 面板为空），stderr 给诊断 |
| SystemExit 非零 | 数据路径/分区/列名等环境错误（消息含修复提示） |

## 使用约束

报告中所有数字是「现有窗口 + 现有票池」下的历史证据描述：不是交易信号，不预测未来，不构成投资建议。窗口/票池变更后必须重跑保留两份对比。
