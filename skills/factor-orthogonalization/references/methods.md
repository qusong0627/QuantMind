# 正交化方法定义、诊断口径与 JSON 契约

本文件是 `scripts/orthogonalize.py` 的方法学参考：三种正交化的数学定义、预处理口径、
诊断指标公式、接受标准与反模式（承自源技能 quantskills/skill-factor-orthogonalize 的
methods.md / anti-patterns.md，GPL-3.0-only），以及本地 JSON 报告的字段契约。

## 概念区分（先定义「剥离什么」，再动手）

| 名称 | 目标 | 常见控制/基准 |
|---|---|---|
| 行业中性化 | 去掉行业配置影响 | 行业哑变量（本工具按市场给：CN rs_hyname 128 类 / HK 所属行业 31 类 / US sector 11 类） |
| 市值中性化 | 去掉大/小盘暴露 | CN ln(float_mv)、US ln(ln_mv_total)、HK 无可用市值列时退回 ln(20 日均成交额)（规模+流动性混合代理） |
| 风格中性化 | 去掉常见风格风险 | 逐日 winsorize+zscore 后的 beta / 波动率等（`--controls`） |
| 因子库正交化 | 去掉与其它因子的重复信息 | 同截面因子矩阵（sym / gs）；也用于「新因子是不是旧因子变体」的确认 |

## 三种方法：数学定义与适用场景

### 1) sym — 对称正交（Löwdin）

设 Z 为逐日截面稳健标准化后的 k 个因子列（n×k），S = corr(Z)（k×k）：

```
S = V · diag(λ) · Vᵀ          （Jacobi 对称特征分解）
W = Z · S^(−1/2) = Z · V · diag(λ^(−1/2)) · Vᵀ
```

性质与口径：
- **corr(W) = I**（恒等相关矩阵）：WᵀW ∝ I 就是定义，逐日实测 max|非对角| 见 SKILL.md 标定。
- **对输入顺序完全不敏感**；在所有「输出为正交列的线性混合」中，W 是 Frobenius 范数意义下
  最接近原始 Z 的那个（故又称最小二乘正交化）。
- **代价是解释性**：每个输出列都是全部输入因子的线性混合，不再对应单个原始因子；
  因子名沿用仅为对齐，语义已改变。
- 数值护栏：S 的最小特征值 < `1e-10` 判奇异（完全共线/常量列）跳过该日并计数；
  < `1e-2` 记「病态」警告日数（近共线时混合系数放大，输出对噪声敏感）。
- 适用：因子库去重（一组因子互相都想保留，只是不想让它们相关）；不做因子库精选时的批量预处理。

### 2) gs — 改进 Gram-Schmidt（顺序正交）

顺序投影（Modified GS + 二次正交化，数值更稳）：

```
q_1 = z_1 / ‖z_1‖
v_j = z_j − Σ_{i<j} (⟨z_j, q_i⟩/⟨q_i, q_i⟩) · q_i   （做两遍投影）
q_j = v_j / ‖v_j‖
```

性质与口径：
- **第一个因子原样保留**（只做标准化），后续因子只剔除与「前面所有因子」的重叠。
- **结果依赖输入顺序**：`--gs-order reverse` 可跑反向序做敏感性检查；不同顺序下保留的
  信息不同（谁在前谁完整保留）。
- 近共线（两个因子几乎相同）时，后到因子的剩余范数趋 0，单位化放大浮点噪声；
  实测个别日 max|非对角| 可达 ~0.09（CN 485 日），日均 ~0.004——这不是 bug，
  是 GS 在病态输入下的固有行为，横向比较时要用 sym 或先剔除近共线因子。
- 适用：「已有主力因子 + 新因子做增量」：主因子放第一个，新因子只保留增量信息；
  或明确知道优先级的信息拆分。

### 3) resid — 基准暴露残差化（逐日截面回归）

对每个交易日、每个因子独立做：

```
y_t = D_t · δ + C_t · γ + ε_t        （D=行业全哑变量，C=规模+风格控制）
residual = ε_t                        （残差才是「新因子」）
```

实现走 Frisch–Waugh–Lovell 等价式（纯标准库、快且数值友好）：

1. 对 y 和各控制列**按行业组内去均值**（消除哑变量维度）；
2. 对去均值后的 y ~ 去均值后的控制列做小规模 OLS（无截距，列主元高斯消元）；
3. 残差 = 去均值 y − 拟合值；数学上与「全哑变量 + 控制的完整 OLS 残差」恒等。

口径与护栏：
- **逐日截面，从不 pooled**（pooled 会把跨日截面结构混进 beta，源技能列为反模式）。
- 控制列先做 `winsorize_zscore`（5×1.4826×MAD 截尾后 z-score，ddof=1），
  组内无变化的控制列自动剔除并计数。
- 自由度护栏：`N − 组数 − 控制数 ≥ 5`，不足跳过该日并计数；当日 `N < 30` 跳过。
- 常量因子当日跳过（「当日因子常量」计数）。
- **最终标准化**：默认纯仿射 z-score（`affine`），严格保持「残差与基准零相关」到 1e-15；
  `--resid-final-scale winsorize_zscore` 为源技能口径，截尾是非线性操作，实测会在
  个别交易日把 ≤1e-1 量级的暴露重新引入最终产出（见 SKILL.md 实测校准），
  需要逐位复刻源实现时才用。
- 数值验证：脚本在 quantdb 模式把首个处理日的 FWL 残差与 `numpy.linalg.lstsq`
  （显式哑变量矩阵、SVD）对拍，实测 max|diff| ≤ 6.3e-13（三市场），写入报告 `self_checks`。
- 适用：所有「让因子不带行业/规模/风格暴露」的场景；组合层面的中性化在因子层面先做。

### 方法可组合

因子库去重 + 基准中性化可以串联：先对每个因子 resid（保住各自语义），再对残差矩阵做
sym（去掉残差之间的残余相关）。`sym` / `gs` 直接作用在原始因子上时不受基准影响——
两类目标互不替代。

## 预处理：winsorize_zscore（逐日截面）

```
med = median(x)；mad = median(|x − med|)
clip: x ← min(max(x, med − 5×1.4826×mad), med + 5×1.4826×mad)   （mad=0 时不截尾）
z = (clip − mean(clip)) / std(clip)                              （样本标准差 ddof=1）
```

- 5×1.4826×MAD 对正态数据约等于 ±5σ，几乎不触发截尾；对厚尾分布才会生效。
- 常量列（std=0）返回全 NaN，调用方按「控制列退化/当日因子常量」跳过并计数。

## 诊断指标定义（报告口径）

| 指标 | 定义 | 期望 |
|---|---|---|
| 相关矩阵 max\|非对角\| | 逐日 corr 矩阵的最大非对角绝对值；报告全窗口逐日最大与日均 | sym ≈1e-14；gs 个别病态日可到 ~0.09 |
| 规模 corr（前/后） | 标准化 y 与 winsorize-zscore 后规模控制的 Pearson 相关，逐日均值 | 前=真实暴露；后≈0 |
| 行业 R²（前/后） | y 对行业全哑变量+截距回归的**中心化** R²（组间平方和/总平方和） | 后≈0；前值含「哑变量个数/截面宽度」基数效应（K−1 个哑变量在 N 只股票上的期望 R²≈(K−1)/(N−1)），只看前后差 |
| 行业 max\|z 组均值\|（前/后） | 稳健标准化后 max_g \|mean_g(·)\|，单位=σ（源技能报告口径） | 后≈0 |
| 风格 corr（前/后） | 同规模 corr，对 `--controls` 每个风格列 | 后≈0 |
| IC 保真 | 逐日 Spearman(factor, ret_h)，**原始与各方法在完全相同的一天、同一批行上各算一次** | 残差 IC 通常下降；保留率 = 残差 mean IC / 原始 mean IC（同池同行） |
| IC 保留率 | 仅当 \|原始 mean IC\| ≥ 0.005 时给出（分母稳定门槛；低于此的比值是噪声，标 `retention: null`） | 源技能验收建议 >50% |
| 逐日处理计数 | 方法处理天数 + 跳过原因分类计数 + 每日截面中位数 | 跳过原因要能解释 |

补充口径：
- **行业 R² 的基数效应**是实测教训：不中心化的公式会把因子均值（如 RSI 的 ~50）算进
  「解释力」，产生 0.98 的假 R²；本工具已修正为中心化版本，但读报告时仍需用
  (K−1)/(N−1) 做基线对照（CN 128 类/300 只 → 基线 ≈0.43）。
- 行业分类为**静态快照**（见 SKILL.md 数据映射），历史重分类不回填，存在前视偏差的
  已知局限——与 brinson-performance-attribution 同口径。
- 本工具不产出组合层指标（Sharpe / MDD / 组合换手）：需要时把残差因子交给回测类技能。

## 接受标准（源技能口径 + 本地可核对项）

正交化不是分数越高越好，而是「更独立且仍有收益」：

- 行业 / 规模 / 风格暴露显著下降（本工具的直接产出，本条是硬验收）；
- 与旧因子最大相关 < 0.6（严格库 < 0.4）——本工具用相关矩阵 max|非对角| 提供数据；
- rank IC 保留率优先 > 50%——**下降本身不是失败**：剥离的暴露若含收益预测成分，
  残差 IC 必降；独立性与保真是两个维度；
- coverage 无显著下降（逐日计数可核对）；
- 组合层 Sharpe / MDD / turnover 无恶化——本工具不产出，需接回测技能。

危险信号（先暂停排查，不要直接接受残差因子）：
- 残差 IC 比原始高很多 → 排查泄漏、mask 错位、回归实现；
- 暴露没降但 score 变高；覆盖下降 >10pp；换手翻倍；与旧因子相关性仍 >0.8；
- 训练/验证/测试三段表现几乎一样好。

## 反模式清单（源技能移植；本工具已内置的护栏标注 ✓）

| 反模式 | 修复 | 本工具状态 |
|---|---|---|
| 把 forward return / label 放进控制变量 | 控制变量只能是 T 日已知暴露 | ✓ 标签列黑名单（return_{n}d / future_return_{n}d / label_return）拒绝当因子与控制 |
| 用全样本 pooled 回归 | 必须逐日截面 | ✓ 全流程逐日 |
| 用未来行业分类回填历史 | 用当时点分类 | ⚠ 行业源为静态快照，报告 caveat 注明 |
| 正交后不重新标准化 | 每日残差重新标准化 | ✓ 默认仿射 z-score（均值 0、标准差 1） |
| 只看正交后 score | 同时看暴露下降与 alpha 保真 | ✓ 暴露清零 + IC 保真同表 |
| 控制变量过多 | 与样本数匹配 | ✓ 自由度护栏 N−K−m ≥ 5 |
| 与旧因子相关性仍 >0.8 | 纳入旧因子矩阵或拒绝 | 数据在相关矩阵里，判定留给使用者 |
| 用 test 段挑正交方案 | train/val 调参，test 不可见 | 流程纪律，工具不替代 |
| 忽略 coverage 变化 | 报告前后 coverage | ✓ 逐日处理计数 |
| 忽略 turnover | 报告 turnover delta | ✗ 本工具不产出（交回测技能） |

## JSON 报告契约（--out）

顶层键：

```
mode                 "quantdb" | "demo"
market/dataset       市场与因子数据集（demo 无）
window               {start, end, trading_days}
data_through         行情数据实际覆盖到的最后一个分区（分区间进度差异可核对）
returns              {source, adjustment, definition}（收益源与复权口径）
universe             {method, asof_date, n_symbols, note}
benchmark            {industry{source,column,n_categories,coverage_pct,note}, size{kind,column,transform},
                      styles[]}
methods_run / gs_order / resid_final_scale / factors / horizons
corr_matrix          {sym/gs: {before_mean_daily, after_mean_daily, after_offdiag_abs_max,
                      after_offdiag_abs_mean_daily_mean, ill_conditioned_days}}
exposure             逐因子 {size_corr_before/after, industry_r2_before/after,
                      industry_max_abs_group_mean_z_before/after, style_corr_before/after}
                     （每项 {mean, n_days}）
ic                   {horizons, by_method{raw, raw_on_method_rows, sym, gs, resid}
                       {factor}{Hn}{n, mean_ic, std_ic, hit_rate},
                      retention{method}{factor}{Hn}{mean_ic_raw_same_rows, mean_ic_method, retention}}
day_counts           {sym/gs: days_used, median_names_per_day, skipped{}；
                      resid: 逐因子 days_used/median_names_per_day/skipped{}}
self_checks          {resid_ctrl_corr_pre_zscore_abs_max, resid_ctrl_corr_final_abs_max/mean,
                      fwl_vs_numpy_lstsq{status,date,factor,max_abs_diff_vs_numpy_lstsq,passed}}
caveats              本次运行的注意事项（票池口径、去重、缺行业等）
golden_checks        demo 模式：手算金样逐条 PASS/FAIL
```

读法建议：先看 `self_checks`（数值正确性）与 `day_counts`（覆盖），再看 `exposure`
（暴露是否清零），最后看 `ic.retention`（保真）——顺序颠倒容易被 IC 数字带偏。
