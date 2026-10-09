# IC 方法说明（ic-methods）

本技能只做**截面预测力**诊断：日度 Spearman IC、ICIR、Newey-West 显著性、滚动/分段稳定性、多周期衰减与半衰期。不构造因子、不回测组合、不输出买卖指令。

方法移植自 quantskills/skill-factor-ic-decay（GPL-3.0-only），数据层改为本地 QuantDB 直读（见 `source-boundary.md`）。

## 1. 日度截面 Spearman IC

对每个交易日 t，在当日截面上对 `(factor, fwd_ret)` 做 Spearman 秩相关：

```
IC_t = Corr( rank(f_i,t), rank(r_i,t→t+h) )
```

- 用秩相关降低极端值敏感度（相对 Pearson）；并列取平均秩。
- 当日有效截面标的数 < 3 则跳过该日（两种模式一致）。
- 数值上强制 IC_t ∈ [−1,1]。

**本地收益定义（quantdb 模式）**：`ret_h(t) = close(t+h)/close(t) − 1`，close 取 `1_kline_data/daily_forward`（CN 前复权；HK/US 不复权），h 为同一标的序列的后续第 h 个交易日（等价于按 symbol 分组 `close.shift(-h)`）。读取范围向窗口末端之后多取 40 个交易日，保证窗口末段 h≤20 的收益可算。

## 2. 汇总指标

对日度 IC 序列 {IC_t}：

| 指标 | 定义 |
|------|------|
| 均值 IC | mean(IC_t) |
| 标准差 | std(IC_t)，样本标准差（ddof=1） |
| ICIR (raw) | mean / std |
| ICIR (年化) | mean / std · √252（日度序列） |
| 命中率 | P(IC_t > 0) |
| Newey-West t | 检验 mean(IC) ≠ 0，默认 lag=5 |

年化 ICIR 假设日度 IC 近似独立；实际 IC 有自相关，故同时报告 NW-t，不要只看 ICIR。

## 3. Newey-West t（Bartlett 核）

对 demeaned IC 残差 u_t = IC_t − mean(IC)：

```
σ²_NW = γ0 + 2·Σ_{j=1..L} (1 − j/(L+1))·γ_j ，  γ_j = (1/n)·Σ u_t·u_{t−j}
t_NW  = mean(IC) / √(σ²_NW / n)
```

默认 L=5。重叠持有期收益会抬高自相关，NW 校正方向正确，但 lag 选择仍是经验值。小样本下 σ²_NW 可能为负（截断为 NA）。

## 4. 滚动与分段稳定性

- **滚动**（默认窗口 W=60 交易日，min_periods=max(10, W/3)）：滚动均值 IC 与滚动年化 ICIR，用于看预测力是否阶段性塌陷，而不是只报一个全样本均值。
- **分段**（本技能新增，非重叠）：每 W 个 IC 观测切一段，报告分段均值 IC 与胜率——比滚动序列更适合一眼看「哪些时段起效/失效」。

## 5. 衰减曲线与半衰期

对每个周期 h 算全样本均值 IC，得到曲线 IC(h)。对**均值 IC > 0** 的周期点做 log-linear OLS：

```
log IC(h) = log A − h/τ   ⇒   IC(h) = A·exp(−h/τ)
```

**半衰期**（IC 衰减到一半）：`t½ = τ·ln 2`。

- 斜率 ≥ 0（未衰减或递增）：不报告正半衰期。
- 正均值点 < 2：无法拟合（**负 IC 因子因此天然无半衰期——预期行为，不是失败**）。
- 曲线非单调（如峰在 H5 后回落）时拟合 R² 会低，报告须注明 R² 与拟合点数。
- 仅单周期输入：报告该周期 IC，半衰期标注为不可估。

## 6. 样本护栏

| 条件 | 行为 |
|------|------|
| IC 观测 < 60 | 抛错拒绝输出（CSV 模式终止；quantdb 模式记入 `failed_factors` 继续跑其余因子） |
| 60 ≤ n < 252 | 允许，`low_sample_warning=true` 并加警告 |
| 因子列常数（nunique≤1） | 报「无截面变化（常数/全空列）」专用错误，提示 `--list-columns` 查填充度 |

## 7. 常见陷阱

1. **未来函数**：标签列（`return_{n}d` / `future_return_{n}d` / `label_return`）已由脚本硬拒绝；因子列本身的时点正确性由数据生产方保证（本脚本不取因子值，只读列）。
2. **重叠周期**：`ret_20` 相邻日高度重叠 → IC 序列自相关强、衰减曲线更平滑，半衰期可能被高估稳定性。
3. **截面过窄**：每日标的中位数见报告 `cross_section`；HK/US 因子列填充不均时，某些日只有个位数标的（<3 跳过）。
4. **把半衰期当保质期**：t½ 是历史衰减形状的拟合参数，不是「还能用 N 天」的承诺。
5. **只看均值 IC**：忽略 ICIR、NW-t、分段塌陷与跨周期衰减，容易把噪声因子当有效。
6. **事后票池**：quantdb 模式按窗口末日成交额取 top-N，是当前活跃池的近似，含轻微选择偏差（已写入报告 caveats）。

## 8. 强度标签（非交易信号）

脚本给出的 `strength` 仅作证据摘要：

- `strong`：|mean IC| ≥ 0.03 且 |NW-t| ≥ 2
- `moderate`：|mean IC| ≥ 0.01 且 |NW-t| ≥ 1.5
- `weak_or_noise`：其余

阈值是启发式，用于报告可读性，**不是**下单门槛。

## 9. JSON 报告契约

顶层（wrapper）：

| 键 | 说明 |
|---|---|
| `mode` | `csv` / `quantdb` |
| `market` / `dataset` / `dataset_path` / `data_root` | quantdb 模式的数据定位 |
| `window` / `data_through` | 请求窗口与行情实际覆盖到的最新分区 |
| `returns` | 收益源路径、复权口径、收益定义 |
| `universe` | 票池口径（method / asof_date / n_symbols / note） |
| `horizons` | 前视周期列表 |
| `factors` | 每因子报告数组（见下） |
| `failed_factors` | 数据不足未产出诊断的因子（含 reason 与 n_rows） |

每因子报告键：`name / n_ic / date_start / date_end / low_sample_warning / window / nw_lag / primary_horizon / summary{n, mean_ic, std_ic, icir_raw, icir_ann, hit_rate, nw_t} / strength / cross_section{min_names, median_names, days_skipped} / segments[] / rolling[] / decay_curve[] / half_life_fit{A, tau, half_life, r_squared, n_points, fitted, note} / caveats[]`（quantdb 模式另有 `column / dataset / n_symbols`）。NaN/inf 一律序列化为 `null`。
