# 组合优化方法说明（methodology）

本技能把「信号/观点 + 协方差 + 约束」变成目标权重与一份事前（ex-ante）诊断：权重、风险分解（逐资产 RC + 逐因子）、换手、约束绑定清单。**不做回测、不预测收益、不输出交易指令**；所有数字都是最优化问题的解，不是收益承诺。

方法移植自 quantskills/skill-portfolio-optimize（GPL-3.0-only），数据层由 PandaData SDK 换为本地 QuantDB 直读；求解器原为实现细节（scipy SLSQP），本版扩为「scipy SLSQP + 纯标准库投影梯度」双后端（见 §5）。本地边界见 SKILL.md「来源与许可」。

## 1. 五个目标

统一记号：w 为权重向量，Σ 为协方差矩阵（年化与原样口径由输入方负责），μ 为预期收益向量，σ = diag(Σ)^½ 为逐资产波动率，定义组合方差 var(w) = wᵀΣw、波动 vol(w) = √var(w)。

| 目标 | 数学形式 | 需要 μ | 备注 |
|---|---|---|---|
| `min_variance` | min wᵀΣw | 否 | 有闭式解可独立校验（§6） |
| `mean_variance` | max μᵀw − (λ/2)·wᵀΣw | 是 | λ=`--risk-aversion`（默认 5） |
| `max_sharpe` | max μᵀw / vol(w) | 是 | 切点组合；对 μ 误差最敏感 |
| `risk_parity` | min Σᵢ (rcᵢ − var(w)/n)² | 否 | ERC；rcᵢ = wᵢ·(Σw)ᵢ |
| `max_diversification` | max σᵀw / vol(w) | 否 | Choueifaty 分散化比率 |

- 全部目标提供**解析梯度**（无自动微分依赖），SLSQP 与投影梯度共用。
- `risk_parity` 的目标函数在最优附近非常平坦（RC 是 w 的二次型之差），对求解器的停止判据要求高，见 §5 与贴坑 1。
- `max_diversification`、`risk_parity`、`min_variance` 不需要 μ——在 μ 没有可靠来源时优先用它们。
- `min_variance`/`max_diversification` 在 `budget=0`（美元中性）下会塌缩到零组合（方差 0 可行且最优），脚本直接拒绝并提示改用带收益项的目标。

## 2. 协方差估计

收益面板 → Σ 的链路（quantdb 模式；CSV/JSON 模式矩阵原样使用，口径由提供方负责）：

1. **收益**：`close` 宽表按日 `pct_change(fill_method=None)`——**绝不能**用 pandas 默认的 `pad`（把缺失收盘顶替为前收，伪造 0 收益，系统性压低波动；实测同一数据 17.83% → 16.32%）。
2. **估计**（`--cov-method auto`，默认）：
   - `ledoit_wolf`：sklearn `LedoitWolf`，**complete-case**（含任何缺失的行整行丢弃，丢弃行数写进报告）；sklearn 把估计除以 T（MLE），本脚本乘 T/(T−1) 对齐到样本口径。有效行 < max(10, n/2) 时不可用。
   - 回退 `sample`：**成对完整样本**（pairwise-complete）样本协方差，逐对用自己的有效观测数；缺失多时比 complete-case 少丢信息，但矩阵可能不满秩（交给 §2.3）。
3. **PSD 修复**（`ensure_pd`，估值前必过）：
   - 非对称超 1e-10·scale → 对称化并记 note；
   - 对角元为负 → 直接拒绝（输入非法）；全零 → 拒绝；
   - 最小特征值 λmin < −0.5·均方差 → 拒绝（多半是手工矩阵笔误/单位混乱）；
   - −0.5·均方差 ≤ λmin < 1e-8·均方差 → 特征值截断（λ←max(λ, 1e-8·均方差)）修复，记 note（含条件数）。
4. **年化**：`× --periods`（默认 252）。quantdb 模式窗口取 `--cov-window` 个交易日（默认 120）或 `--start`。

## 3. 约束模型与投影

`Constraints` 统一携带：预算等式 `Σw = budget`、箱约束 `lo ≤ w ≤ hi`（多头 lo=0；多空 lo=−weight_cap）、行业等式（`Σ_{i∈g} wᵢ = t_g`）、暴露等式（`Xᵀw = 0`）、换手上限 `‖w − w_prev‖₁ ≤ τ`。

| CLI/JSON 输入 | 施加的约束 | 语义 |
|---|---|---|
| `--budget`（默认 1） | 等式 | 美元中性用 0（配带收益项目标） |
| `--weight-cap` / `--long-short` | 箱 | 多头 [0, cap]；多空 [−cap, cap] |
| `--sectors` 只有映射 | 无（展示） | 行业暴露出现在报告里 |
| `--sector-targets S=V,...` / `--sector-neutral` | 行业等式 | 目标和 0 只在 budget=0 下可行 |
| `--exposures` 只有暴露 | 无（展示+因子分解） | 因子暴露与风险拆分出现在报告里 |
| `--exposure-neutral` | 暴露等式 Xᵀw=0 | 如 size/vol 中性 |
| `--prev-weights` 只有权重 | 无（换手诊断） | 报 L1 换手 |
| `--turnover-limit τ` | L1 球 | ‖w−w_prev‖₁ ≤ τ（单边 = L1/2） |

细节与护栏：

- **冗余等式消除**：行业目标和之和恰等于预算时（如全市场多头按基准行业权重配目标），最后一行行业等式是预算等式的推论——去掉以保持等式行满秩，否则 SLSQP 的 Jacobian 秩亏、步长退化（源技能同款坑）。
- **换手几何下界预检**：即使每只资产只移动到最近的箱边界，L1 距离也有下界（上期权重越界部分必须动）；下界 > τ 时报错给出该下界，不白跑求解。
- **上限可行性预检**：n·cap < budget（多头）时直接报错。
- **相位一可行性探测**（`diagnose_feasibility`）：结果违反约束超容差时，用投影梯度在箱约束内最小化全部约束违背后的平方（3 个起点：等权/下界/上界）。残余 > 1e-5 → 判定「约束联合不可行」，报告残余最大的 3~4 条矛盾行；否则判定「可行集非空但求解器未收敛」，建议换后端/加迭代。

## 4. 风险分解

- **逐资产 RC**：rcᵢ = wᵢ·(Σw)ᵢ，份额 = rcᵢ/var(w)（可能为负——多空组合的正常现象）。`risk_parity` 目标即让份额趋于 1/n。
- **因子分解（GLS）**：给定暴露矩阵 X（n×k，列=因子），令 e = Xᵀw 为组合因子暴露，
  F = (XᵀΣ⁻¹X)⁻¹（因子协方差，GLS），因子方差 = eᵀF e，特质方差 = 总方差 − 因子方差。
  逐因子分解 πⱼ = eⱼ·(F e)ⱼ。恒等式残差 `identity_gap` 一并报告（应为 ~0）。
  仅当暴露矩阵可逆（n ≥ k 且 X 满列秩）时适用；不适用时报告说明原因。
- 分解是**事前口径**（用当期 Σ），不是已实现风险归因——与 brinson 之类的已实现归因不是一回事。

## 5. 求解器设计

| 后端 | 实现 | 何时用 |
|---|---|---|
| `scipy-slsqp` | scipy SLSQP，解析 Jacobian，±预算等多起点（1~3） | `--solver scipy`；`auto` 下除 risk_parity 之外默认 |
| `stdlib-pg` | 纯标准库投影梯度（Armijo 回溯线搜索），多起点 | 无 scipy 环境（宿主机/dsh）；`auto` 下 risk_parity 强制走它 |

- **`auto` 路由**：`risk_parity` 一律走 `stdlib-pg`。实测同一问题（CN top-60、120 日窗口）RC max−min 相对差：SLSQP（20000 迭代）6.6e-03，投影梯度 4.5e-08——SLSQP 在平坦目标上以「收敛」状态早停。其余目标在 scipy 可用时用 SLSQP，不可用回退投影梯度。
- **投影梯度**：每步取投影 proj(v − η∇f)。投影按约束拼装（Dykstra 循环处理多个非平凡约束集）：
  - 箱 ∩ 预算：对 θ 二分，w = clip(v − θ, lo, hi) 使 Σw = budget（每步 O(n log n) 级）；
  - 仿射组（行业/暴露等式、箱 ∩ 预算）：w = v − Aᵀ(AAᵀ)⁻¹(Av − b)（A 为等式行，小矩阵显式求逆）；
  - L1 球 ‖w−w_prev‖₁ ≤ τ：平移后排序 + 软阈值闭式投影。
  - 收敛判据：投影梯度的无穷范数 < 1e-10 或 4000 迭代；未达时报告「约束满足但未完整收敛」而非失败。
- **对拍**（`--cross-check`）：显式跑另一后端，报告 max|Δw| 与目标值差。实测 scipy vs stdlib（CN top-60 min-var）max|Δw| = 6.3e-07、目标差 2.96e-13——两后端在数值容差内一致。

## 6. 独立验证（每次运行自动输出）

1. **最小方差闭式解**：对照问题 = 仅预算等式、无箱约束（允许做空）的 min-var，闭式 w = Σ⁻¹1/(1ᵀΣ⁻¹1)。闭式解用两种独立实现互验：纯标准库高斯-约当求逆 vs numpy 求逆；再与实际求解后端的结果比 max|Δw|。实测偏差 1.4e-08 ~ 1.6e-07（SLSQP/PG 数值级），两实现互差 ≤ 4.1e-16。
2. **风险平价 RC 等分**：目标为 risk_parity 时报告 RC 份额 max−min 相对差（相对 1/n）。实测投影梯度 4.5e-08（收敛）；有单票上限绑定时 RC 不等分属预期（报告加注）。
3. **demo 金样**（纯标准库可复现）：2 资产 Σ=[[0.04,0.006],[0.006,0.09]]（σ=20%/30%, ρ=0.1）、μ=(8%,12%) 年化——min_variance 手算 (42/59, 17/59)，risk_parity 逆波动 (0.6, 0.4)，max_sharpe 切点 (0.6, 0.4)，全部与脚本输出一致到 1e-11 内；闭式解公式本身与手算常量偏差 0。

## 7. JSON 互通协议（与 barra-risk-model 等技能对接）

**输入**（`--input problem.json`）：

```json
{
  "symbols": ["000001.SZ", "..."],
  "cov": {"000001.SZ": {"000001.SZ": 0.04, "...": 0.0}, "..." : {}},
  "mu": {"000001.SZ": 0.08},
  "sectors": {"000001.SZ": "银行"},
  "sector_targets": {"银行": 0.2},
  "exposures": {"000001.SZ": {"size": 1.2, "beta": 0.9}},
  "exposure_neutral": ["size"],
  "w_prev": {"000001.SZ": 0.05},
  "objective": "mean_variance",
  "risk_aversion": 5.0,
  "budget": 1.0,
  "long_only": true,
  "weight_cap": 0.05,
  "turnover_limit": 0.3,
  "notes": []
}
```

- `cov` 可换行主序二维数组（则 `symbols` 必填）；`exposure_neutral=true` 表示**全部提供的暴露列**同时中性（要选择性中性就在 `exposures` 里只放需要的列）；`sectors`/`exposures` 不加相应目标字段时缺省即「只展示不约束」。
- 矩阵与暴露**原样使用**：年化口径、是否中心化、单位由提供方负责（barra 技能的 Σ 输出可直接喂进来）。
- 缺省值：objective=mean_variance、risk_aversion=5、budget=1、long_only=true；`sector_targets` 缺省=None（仅展示行业，不约束）。

**输出**（`--out report.json`；NaN/inf 序列化为 null 之外一律数值）：

| 键 | 内容 |
|---|---|
| `weights` | {symbol: 权重}，按资产序插入 |
| `solver` | backend / n_starts / iterations / converged / message |
| `diagnostics` | 持仓数、Σw、gross、最大/最小权重、HHI、有效持仓数、年化波动、(μ 口径)预期收益与夏普、逐资产 RC 份额、RC max−min 相对差、换手 L1/单边、因子风险拆分（factor_share/specific_share/per_factor/identity_gap）、`binding_constraints` 清单 |
| `verification` | minvar_closed_form{max_abs_gap, rms_gap, closed_form_two_impl_gap, backend}、risk_parity_rc{max_min_gap_rel}、cross_check |
| `problem` | cov_source / mu_source / 约束摘要（budget、箱、行业数、暴露名、换手上限、等式行数） |
| `caveats` | 数据口径与估计器说明、求解器提示 |
| `report_text` | 中文渲染（compact 模式给 demo 表格用） |

## 8. 标定记录（2026-10-08，quantmind 容器）

CN = 20251231 成交额前 60、窗口 120 交易日（2025-07-09~2025-12-31）、cap 0.10、Σ=Ledoit-Wolf（complete-case 丢 12 行）；HK/US/两阶段见 SKILL.md 实测校准表。

| 运行 | 后端 | 年化波动 | 持仓 | 最大权重 | 关键绑定 | 校验 |
|---|---|---|---|---|---|---|
| min_variance | scipy | 16.32% | 33 | 0.1000 | 上限 | 闭式 1.44e-08 |
| risk_parity | auto→stdlib | 19.56% | 60 | 0.0767 | 无（未绑） | RC 4.51e-08 |
| risk_parity | scipy（对照） | 19.56% | 60 | 0.0767 | 无 | RC 6.6e-03（早停） |
| max_sharpe（μ=历史均值） | scipy | 20.96% | 29 | 0.1000 | 上限 | 闭式 1.44e-08 |
| max_sharpe 无上限（对照） | scipy | 21.43% | 25 | 0.1456 | — | 同上 |

结论：① SLSQP 不适合 RP 目标（早停），auto 强制 stdlib；② μ 噪声的可视化证据——同样 μ=历史均值，无上限时 max|w| 达 14.6%，上限 10% 时被压住且更分散（effN 17.5 vs 15.2）；③ 约束参与度随目标差异大：同为 cap 0.10，min-var 被上限绑死（33 只中多只 0.1），RP 则完全够分散（最大 7.7%，cap 未绑）。

## 9. 本技能不做的事

- **不回测**：不评估换手成本后净值、不产生历史曲线——那属于回测/归因类技能。
- **不预测 μ**：`--mu-from-mean` 只是把历史均值当 μ 的对照开关（明确标注噪声警告）；正式 μ 应由上游信号/研究流程提供（如 --mu-file）。
- **不建模交易成本**：换手只作约束与诊断（L1 口径），成本/冲击/涨跌停不可交易性均未建模。
- **不建模融券**：多空书只放松下界；A 股融券成本/券源/卖空约束的可实现性由使用方另加约束。
- **不输出指令**：权重是研究产物；是否下单、怎么切单是执行面的责任。
