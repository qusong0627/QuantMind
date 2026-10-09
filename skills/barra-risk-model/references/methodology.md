# 方法论与口径（Barra 式多因子风险模型）

方法论移植自 [quantskills/skill-risk-model](https://github.com/quantskills/skill-risk-model)（GPL-3.0-only）：截面回归、Ledoit-Wolf 闭式收缩、特异风险 EWMA、风险分解与极小方差验证沿用其公式；数据层由 PandaData SDK 整体替换为 QuantDB 本地 parquet 直读（`scripts/barra_risk.py` 无任何外部 SDK 依赖）。行业映射复用本仓 Brinson 技能的既有口径（`instrument_detail.rs_hyname` / `akshare_profile.所属行业` / `sector.sector`）。

## 1. 模型结构

对每个交易日 t 做一次截面加权最小二乘（Fama-MacBeth 结构）：

```
r_{i,t+1} = Σ_k X_{i,k,t} · f_{k,t} + u_{i,t}
```

- `X_{i,k,t}`：标的 i 在 t 日的因子暴露（风格 z 值 + 行业哑变量）；
- `f_{k,t}`：当日因子收益（回归系数），可解释为「单位暴露的次日收益」；
- `u_{i,t}`：特异收益。
- `y` 用**次日**收益（`ret.shift(-1)`），暴露与 y 严格错开一日，无前视。
- 组合协方差：`Σ = X F X' + Δ`，F 为因子协方差、Δ 为对角特异方差。

## 2. 因子与暴露

### 2.1 风格暴露（7 个）

| 风格 | 定义 | CN | HK / US |
|---|---|---|---|
| SIZE | 规模 | `ln(total_mv)`（真实总市值） | `ln(liq_amount_ma_20 或当日 amount)`（市值列实测全 0，成交额代理） |
| VALUE | 账面市值比 | `bp`；缺列时 `1/pb` | 不可用（bp/pb 列全 0 占位） |
| MOMENTUM | 12-1 动量 | `exp(cum_logret.shift(21) − shift(252)) − 1` | 同左（价格收益） |
| SHORT_REV | 21 日反转 | `exp(Δcum_logret_21) − 1` | 同左 |
| VOLATILITY | 60 日波动 | `std(logret, ddof=0)×√252` | 同左 |
| BETA | 120 日贝塔 | 对市值加权市场收益回归（最小 90 日） | 对成交额加权市场收益 |
| LIQUIDITY | 流动性 | 新列集 `ln(1+hs_turnover)`；旧列集兜底 `ln(amount_ma_5)` | `ln(1+liq_turnover_os)`（兜底 Amihud 为不流动口径、方向相反） |

- 市场收益的权重取**前一日**（`shift(1)`，源技能口径），仅正权重参与。
- 列选择按 `STYLE_SPEC` 候选顺序取「首个在全部抽样分区存在且末分区非常量（nunique>1）的列」——自动适配 CN features_daily 列集断点（旧列集无 `hs_turnover` 时自动落到 `amount_ma_5`）。`--list-columns` 可先探查。
- **标签列护栏**：`return_{n}d` / `future_return_{n}d` / `label_return` 是收益标签（现代为未来收益），仅出现在 `--list-columns` 的警示清单中，任何情况下不作为暴露候选。

### 2.2 行业

- 映射源：CN `quantdb/2_base_sector/instrument_detail/instrument_detail.parquet`（`rs_hyname`，128 类，静态快照）；HK `quanthk/2_base_sector/akshare_profile`（`所属行业`，31 类）；US `quantus/2_base_sector/sector`（`sector`，11 类）。
- 保留规则：票池覆盖 ≥ `--min-industry-names`（默认 3）且按覆盖排序取前 `--max-industries`（默认 30），其余归「其他」。
- **逐日折叠**：某行业**当日**有效截面内 < min-industry-names 只时，其成分股当日归「其他」。防止单票行业哑变量过拟合该票（残差≈0 → 特异方差退化 → Σ 近奇异，HK 实测踩过）。
- **恒零因子剔除**：全窗口系数恒 0 的行业（成分股从未进入有效截面）从模型与 F 中剔除并在报告与 caveats 中列名，否则 F 行/列全零、不可求逆。

### 2.3 标准化

逐日截面：MAD 温莎化（中位数 ± 3×1.4826×MAD，源技能口径）后 z-score（ddof=0）。行业哑变量不标准化。

## 3. 截面回归（WLS）

- 权重：`--weight-by sqrt_mcap`（默认）w=√市值——CN 用真实 `total_mv`；HK/US 无真实市值列，用 √20 日均成交额（或当日成交额）代理，报告的 `model.weight_scheme` 如实写明；`equal` 为等权。
- 求解：加权正规方程 + **相对岭** `RIDGE_REL=1e-8 × tr(X'X)/K`（源为绝对值 1e-8，本地相对化以适配不同量纲）。
- 当日有效标的数须 ≥ K+2（源口径 `MIN_NAMES_OVER_FACTORS=2`），否则跳过该日并计入 `skip_reasons`。
- 容器内用 numpy 正规方程（`_wls_np`）；同语义的纯标准库高斯消元（`wls_fit`）用于独立对拍。

## 4. 协方差估计

### 4.1 因子协方差 F

- 默认 **Ledoit-Wolf 对角目标闭式收缩**（无需特征分解）：

```
S = (1/T)·XᵀX（中心化后，X 为 T×K 因子收益）
π̂_ij = (1/T)Σ_t (x_it·x_jt − s_ij)²        （对角外）
κ* = Σ_{i≠j} π̂_ij / ( T · Σ_{i≠j} s_ij² )  （截断至 [0,1]）
F = (1−κ)·S + κ·diag(S)                     （凸组合，天然 PSD）
```

- κ→1 表示样本量不足以分辨非对角结构（如 demo T=47 实测 κ=1.0，属真实现象而非缺陷）；CN 两年 T=485 实测 κ=0.0198。
- `--cov-method ewma` 为替代：EWMA 半衰期 `--factor-halflife`（默认 90 日），Gram 形式保证 PSD。
- 年化 ×252；报告给出 F 最小特征值（PSD 自检）。

### 4.2 特异方差 Δ

- 逐股对特异收益序列做 EWMA 方差，半衰期 `--specific-halflife`（默认 60 日），至少 5 个有效观测（源口径）。
- 样本不足的标的以全体中位特异方差填充，数量记入 `specific.n_filled_with_median`。

## 5. 风险分解与组合工具

- 组合暴露 `b = X'w`；因子风险 `b'Fb`；特异风险 `Σ_i w_i²Δ_i`。
- 因子方差贡献（Euler/CCTR）`CCTR_k = b_k·(Fb)_k`，占比 = CCTR_k / ΣΣ；剔除重名后归一。
- **恒等式**：`factor_var + specific_var = w'Σw`，报告残差 `|diff|/total`（要求 ≈0）。
- **显式二次型复核**：另用 numpy 显式构造 N×N `Σ = XFX' + Δ` 与组合权重直接算 `w'Σw`，与分解式对拍（两条计算路径）。
- **极小方差组合**（验证 Σ 可求逆、可作优化输入）：`w ∝ Σ⁻¹1`，Woodbury 展开只对 K×K 求逆：

```
Σ⁻¹1 = Δ⁻¹1 − Δ⁻¹X (F⁻¹ + X'Δ⁻¹X)⁻¹ X'Δ⁻¹1
```

  无约束解析解**不做权重截断**（负权重是真空头；截断后已不是极小方差解）；报告空头数与总杠杆。自检 `le_equal_weight`：由 Cauchy–Schwarz，真解波动必 ≤ 等权——违反即求逆侧与评估侧 Σ 不一致（程序缺陷），报告显式标警。

## 6. 独立校验（每次运行输出）

| 项 | 方法 | CN 校准实测（2024-01~2025-12，30 日抽样） |
|---|---|---|
| 因子收益求解 | numpy 正规方程 vs `np.linalg.lstsq`（SVD 路径） | 最大偏差 5.29e-08 |
| 因子收益求解 | numpy vs 纯标准库高斯消元（同语义两实现） | 最大偏差 7.01e-16 |
| LW 收缩强度 κ | 纯 Python 闭式 vs numpy 独立复算 | 0.0 |
| 风险恒等式 | \|factor+specific−total\|/total | 0.0 |
| 显式 Σ 二次型 | N×N 全矩阵 vs 分解式 | 2.86e-16 |

demo 小算例另有：无噪声段因子真值还原偏差 2.59e-11、含噪声段 5.01e-03、单因子闭式手算偏差 1.53e-12。

## 7. 校准记录（2026-10-08 实测）

- **CN 全窗**（`features_daily`，2024-01-01~2025-12-31，top-300@20251231，n=485 期，中位 294 只/日，末截面 294 只）：风格年化波动 SIZE 10.66% / VALUE 4.01% / MOMENTUM 6.52% / SHORT_REV 7.65% / VOLATILITY 8.96% / BETA 8.92% / LIQUIDITY 8.82%；特异波动中位 44.73%；等权组合年化波动 31.16% = 因子 99.2% + 特异 0.8%（无显式市场因子，行业因子承担主要共同波动）；极小方差比 0.767；κ=0.0198，F 最小特征值 9.07e-04。强共线实测 SIZE~LIQUIDITY −0.78（amount_ma_5 兜底口径所致，报告已提示）。
- **HK 冒烟**（`l1_factors`，2025 全年，top-200，n=243 期）：双来源重复行剔除 43884 行；5 个行业全窗未识别被剔除；特异波动中位 25.23%；等权波动 22.07%，极小方差比 0.821（修复单票哑变量过拟合后 ≤1）。
- **US 冒烟**（2025-07-01~2026-09-30，top-150，n=303 期）：因子分区止于 2026-09-17，其后的 9 个交易日因暴露缺失被跳过（`skip_reasons.too_few_names`），报告 `data_through` 与末日截面日期如实呈现；等权波动 13.15%。
- **demo 金样**（`--demo`，纯标准库）：3 因子正交设计小算例，前 24 日特异收益为 0 时 WLS 应精确还原因子真值——实测最大偏差 2.59e-11；κ=1.0（T=47 分辨不出非对角，属真实现象）；极小方差比 0.649。
- 运行时长：CN 两年全窗（含 30 日对拍）约 40 秒（容器内，与某后台任务并行）。

## 8. 边界与不做的事

- 不预测收益、不产出配置建议；风险模型只描述历史协方差结构。
- 无显式市场/国家因子：行业因子吸收市场共同波动，组合分散度低时因子风险占比会被行业维度放大（报告 caveats 固定声明）。
- 行业分类是静态/快照口径，期间调类不反映。
- 票池是「窗口末日成交额」事后选池（含轻微选择偏差，非逐日动态池）。
- 特异风险为对角假设（个股特异收益不相关）；同行业/同风格剩余的横截面相关只被行业维度部分吸收。
- HK/US `daily_forward` 不复权（价格收益）；CN 为前复权。

## 参考

- 源技能：[quantskills/skill-risk-model](https://github.com/quantskills/skill-risk-model)（GPL-3.0-only）。
- Ledoit & Wolf (2004), "Honey, I Shrunk the Sample Covariance Matrix"（对角目标闭式解）。
- Fama & MacBeth (1973), "Risk, Return, and Equilibrium: Empirical Tests"。
