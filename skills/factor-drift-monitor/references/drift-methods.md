# 漂移检查方法说明（drift-methods）

本技能只做**已存在因子面板的结构性与统计性体检**：断更/覆盖、缺失/常量、列集增删、数值分布漂移，外加标签列的独立监控。不构造因子、不回测组合、不修复数据、不输出买卖指令。

方法移植自 quantskills/skill-factor-drift-monitor（GPL-3.0-only），数据层改为本地 QuantDB parquet 逐分区直读（见 `source-boundary.md`）；检查定义与阈值出处如下，"本地修正"标记的为移植时新增/调整项。

## 0. 窗口与基线口径

- **分区**：一个交易日一个 Hive 分区 `<market_root>/6_ml_datasets/<dataset>/dt=YYYYMMDD/data.parquet`。
- **基线段** = 窗口**前** `baseline_partitions`（默认 20）个分区，应取稳定期；**近期段** = 窗口**后** `recent_partitions`（默认 5）个分区。
- 源技能按"窗口"比较；本地为逐分区扫描，比较口径不变（早段 vs 尾段），但分布统计在分区内抽样（见 §4）。
- 所有分区按 dt 升序处理；分区目录缺失=**缺分区**，目录在但 0 行/无文件=**空分区**（critical），两者分开报告。

## 1. 断更与覆盖

| 检查 | 触发条件 | 严重度 | 出处 |
|---|---|---|---|
| 断更（分区滞后） | 因子最新分区落后**同市场行情分区** ≥2 个分区 → warning；≥5 → critical | warning/critical | 本地（源技能无；以行情为参照避免长假误报） |
| 断更兜底 | 无行情参照时，最新分区距今 >10 自然日 → warning | warning | 本地 |
| 缺分区 | 行情有分区、因子无该日分区 → warning（列日期）；两侧均无的工作日 → info「疑为节假日」 | warning/info | 本地 |
| 覆盖骤降 | 近段某分区标的数较基线中位数下降 ≥5% → warning；≥20% → critical；绝对下降 <20 只不报 | warning/critical | 本地（阈值由 CN 09-21 实测 −5.4% 标定） |
| 重复主键 | 同一分区内 (symbol) 重复行 >0 | warning | 源技能 quality_summary 同级检查 |
| 日期列不一致 | 行内日期列值 ≠ 分区名 dt | warning | 源技能（order 检查） |
| 非有限值 | 近段任一分区任一数值列含 inf | warning | 本地（CN pe 类列实测有 inf） |

## 2. 缺失与常量

| 检查 | 触发条件 | 严重度 | 出处 |
|---|---|---|---|
| 缺失率绝对线 | 近段中位缺失率 ≥0.20 | warning | 源技能默认（≥20%） |
| 缺失率跳升 | 近段中位较基线中位上升 ≥0.10 (10pp) | warning | 源技能默认 |
| 整列停填 | 近段 ≥90% 且基线 ≤10%（缺失率） | critical | 本地（源为 warning，本项目曾整月停填故提级） |
| 单分区尖峰 | 最新分区缺失率 − 基线中位 ≥0.30 | critical | 本地（应对"末日整列缺失"） |
| 结构性稀疏 | 基线中位缺失率 ≥50% 且未较基线恶化 ≥10pp | info（聚合一条） | 本地修正（HK ind_* 列基线即缺 86%~100%，避免每次刷屏） |
| 新列未填充 | 基线窗口无此列、近段缺失率 ≥50%（非标签） | info | 本地 |
| 新常量列 | 近段所有分区 nunique≤1 且基线非全程常量 | critical（标签列 warning） | 源技能（constant column 检查，本地按标签降级） |
| 基线常量列 | 基线与近段均 nunique≤1 | info（聚合一条，记录用） | 本地 |
| 常量解冻 | 基线常量、近段出现变化 | info | 本地 |
| 全 0 列 | 由 `zero` 计数与 nunique 联合判读（nunique=1 且值=0） | 同上 | 源技能 |

标签列（`label_return`/`return_{n}d`/`future_return_{n}d`）命中缺失/常量时严重度**封顶 warning**（`_label_severity`）：标签变化改变训练口径，不影响实时信号。

## 3. 列集漂移

在**全部相邻分区**上检测（不只基线 vs 最新）：

| 事件 | 触发条件 | 严重度 | 出处 |
|---|---|---|---|
| 列移除 | 相邻分区列集减小（非标签列） | critical | 源技能（schema 变更）+ 本地提级（按列位置读取会静默错位） |
| 标签列移除/改名 | 标签列整体改名（如实测 `return_Nd → future_return_Nd`） | warning（label 类别） | 本地 |
| 仅列序变化 | 集合不变、顺序变化 | info | 本地（实测 CN 09-15 命中；按位置对齐的管线会错位） |
| 列新增 | 集合变大 | info（>0 条时单列 inform） | 源技能（新增列无基线样本，不做分布对比） |
| 白名单命中 | 列在 `--allow-removed/--allow-added` | 降级为 info（不再消失） | 本地 |

## 4. 分布漂移（数值列）

- 取每分区每列**等距抽样**（`sample_per_partition` 默认 800），基线段与近期段分别合并后再等距降采样到 `max_sample`（默认 20000）；样本 <20 的列跳过。
- PSI 用**基线分位点**做分箱（numpy linear 分位，首末边 → ±∞），两段占比在 1e-6 处截断；KS 为两样本统计量（单遍双指针合并）。
- σ 比 = 近期标准差 / 基线标准差（各分区 std 的中位数再取比）。

| 指标 | 阈值 | 严重度 | 出处 |
|---|---|---|---|
| PSI | ≥0.10 → info；≥0.25 → warning | info/warning | 评分卡行业惯例 0.1/0.25，与源技能 watch/warning 线一致 |
| KS | ≥0.20 → warning | warning | 源技能默认 |
| 均值平移 | \|Δmean\| ≥1.0σ（σ=基线 std） | warning | 源技能默认（≥1 sd） |
| 低置信度 | 任一段样本 < `min_sample`（默认 200） | 命中即降为 info | 源技能 low-confidence 降级（<100 行；本地取 200 点） |
| 输出上限 | 详情表最多 `max_dist_alerts`（默认 15）列，其余进 JSON | — | 本地 |

**解释纪律**：分布漂移不区分「数据故障」与「真实市场状态变化」；价格水平/动量类在趋势行情中必然漂移（报告 caveats 内置）。跨列集断点直接对比会产生巨量假漂移（实测 CN 09-14 断点后 30 列新列无基线），先加白名单/取同代窗口。

## 5. 标签列独立监控

- 时间线：每个标签列的出现区间、出现分区数、中位填充率、min/max 填充。
- **回填前沿**：末次填充率 ≥50% 的分区日期；若 < 最后出现分区（或从未 ≥50%）→ info「标签回填前沿」告警。前沿停滞 = 标签重算任务疑似停跑（实测 CN：`return_1d` 末次填充 2026-09-16 … `return_60d` 2026-06-11，随 horizon 逐级前移）。
- 标签类告警一律不提级为 critical（训练口径问题 ≠ 实时信号故障），但必须可见。

## 6. 严重度语义

| 级别 | 含义 | 处置 |
|---|---|---|
| critical | 面板结构性损坏：断更（≥5 分区）、空分区、整列停填、非标签列移除、单分区整列缺失、新常量列 | 当日排查数据管线 |
| warning | 显著漂移/退化：缺失跳升、覆盖骤降、标签改名、断更 2~4 分区、分布超阈 | 人工判读（数据故障 vs 真实行情） |
| info | 记录性：白名单命中、结构性稀疏、基线常量、仅列序变化、假日缺口、低置信度 | 建立基线认知，无需动作 |

## 7. JSON 契约

顶层键：`skill, generated_at, mode, market, dataset, data_root, factor_path, kline_path, window, config, profile, staleness, partition_gaps, coverage, schema, constants, labels, missing_columns, distribution, distribution_not_shown, alerts, summary, caveats`。

- `profile`：`partitions, empty_partitions, rows_min/max, symbols_min/max, dup_symbol_rows, inf_cells`。
- `alerts[]`：`severity(critical|warning|info)`、`category(staleness|coverage|integrity|missing|constant|column-set|label|distribution)`、`object`（列名/分区/切换对）、`baseline`、`current`（含实测数字）、`hint`（可能原因提示）、`detail`（结构化补充，如 PSI、被移列表）。
- `distribution[]`：逐列 `psi, ks, mean_shift_sigma, std_ratio, n_base, n_cur, low_confidence, severity`；超输出上限的在 `distribution_not_shown` 只记数量。
- `config` 回写本次全部阈值，报告自含口径。
