# JSON 报告契约（brinson.py）

报告 = 家族信封（status/input_summary/assumptions/metrics/findings/limitations/next_actions/domain_result），`domain_result` 为 Brinson 域结果（源自源技能输出契约）。

## 顶层

| 字段 | 语义 |
|---|---|
| `status` | `pass` / `warning`（非残差门禁失败或 high 级 findings）/ `fail`（残差 >1bp）/ `insufficient-evidence`（输入校验失败，未做任何计算） |
| `input_summary` | 模式、市场、方法、期列表、组合/基准/全集标的数、行业源与列名（quantdb 模式） |
| `assumptions` | 方法定义、容差、残差容差、期序规则；quantdb 另含基准构造、收益口径、归一化说明 |
| `metrics` | headline 数值：三收益、三效应、残差、HHI、质量分、期数（多期为 Carino 链接值） |
| `findings[]` | `{id, severity, evidence, impact, recommended_fix}`；severity：`info`（常规剔除/去重/归一化）、`medium`（口径约定，如组合行业不在基准）、`high`（组合标的被剔除） |
| `limitations[]` | 方法边界 + 市场口径限制（不复权不计分红、行业快照时效等） |
| `next_actions[]` | 仅在有残差失败/门禁失败/严重剔除时给出 |
| `domain_result` | 见下；`insufficient-evidence` 时为 `{"analysis_skipped": true}` |

## domain_result（单期）

`method`（`fachler`/`bhb`）、`portfolio_return`、`benchmark_return`、`active_return`、`allocation`、`selection`、`interaction`、`residual`、`herfindahl_portfolio`、`herfindahl_benchmark`、`top_contributors`（`["行业:效应", …]` 至多 3 条）、`verdict`（`ALLOCATION`/`SELECTION`/`INTERACTION`）、`score`（门禁通过率）、`gates`（见 methodology）、`sectors[]`（逐行业：w_p/w_b/r_p/r_b/allocation/selection/interaction/total/weight_active）、`linked`（单期 null）、`periods`（单期 null）、`notes[]`。

## domain_result（多期，`method="{method}+carino"`）

headline 收益与效应 = Carino 链接后的几何值；`gates` 增加 `abs_residual_linked<1bp`；新增：

- `linked`：`portfolio_return_geometric`、`benchmark_return_geometric`、`active_return_geometric`、`active_return_arithmetic_sum`、`carino_factors[]`、`{allocation,selection,interaction}_linked`、`residual_linked`。
- `periods[]`：逐期 `{period, portfolio_return, benchmark_return, active_return, allocation, selection, interaction, residual, carino_factor}`。
- `period_count`；`sectors`/HHI 为最后一期快照（源契约）。

## findings id 表

| id | 场景 |
|---|---|
| `quantdb-portfolio-weights-normalized` | 组合权重和在容差内不为 1，已归一化（info，仅报一次） |
| `quantdb-benchmark-universe-dropped` | 行业映射内有标的窗口无行情，未进等权基准（info，含数量/样例） |
| `quantdb-{portfolio,benchmark}-{no_industry,no_window_return,invalid_weight}` | 组合/基准标的剔除（组合=high，基准=medium/info） |
| `quantdb-portfolio-sector-absent-in-benchmark` | 组合行业不在基准，r_b 取总收益中性约定（medium） |
| `quantdb-duplicate-kline-rows` | HK 双来源重复行已去重（info） |
| `quantdb-level-ignored` | 非 US 市场传了 `--level`，已忽略（info） |
| `insufficient-evidence-*` | 输入校验（缺列/重复行业/权重和/空输入/文件不可读/空权重文件/重复标的…）；`evidence.reason` 见脚本 ISSUE_TEXT |

`--out` 未给时 JSON 打到 stdout；给了则写文件并打印一行摘要（status/method/residual/findings/路径）。`--text` 额外打印人读摘要（gates、Carino 段、逐期表、行业表）。
