# TCA 方法说明（methodology）

本技能做**执行后复盘**：给定订单/成交明细与分钟级行情，把实施缺口（Implementation Shortfall, IS）拆成五项、给出基准对标与参与率-滑点校准。不预测、不喊单、不下单。

方法移植自 [quantskills/skill-transaction-cost-analysis](https://github.com/quantskills/skill-transaction-cost-analysis)（GPL-3.0-only），并合并 [quantskills/skill-transaction-cost-calibration](https://github.com/quantskills/skill-transaction-cost-calibration)（GPL-3.0-only）的参与率校准；数据层由 PandaData SDK 改为本地 QuantDB（见 SKILL.md「来源与许可」）。IS 五分解可溯源到 Perold (1988)。

## 1. 符号与 IS 五分解

符号：D=决策价，A=到达价，E=Σ(p·q)/Σq 成交均价，P=区间末价，Q=订单量，q=已成交量，
f=q/Q，dir=buy:+1 / sell:−1，κ=费用 bps（按成交名义额），σ_day=日内尺度波动率，ADV=日均量。

全部折算为「**订单名义额@决策价**」bps（正=对本方不利）：

```
delay 延迟 = dir·q·(A−D)/(Q·D)×1e4
exec 执行  = dir·q·(E−A)/(Q·D)×1e4 = impact 冲击 + timing 择时
  impact  = f·k·σ_day·√(Q/ADV)×1e4        （square-root 模型估计，非盘口反演）
  timing  = exec − impact                   （残差项；无 σ/ADV 时 impact=0 并记 degraded）
opp 机会   = dir·(Q−q)·(P−D)/(Q·D)×1e4     （未成交部分 vs 期末价）
fees 费用  = Σ(κ_i·p_i·q_i)/(Q·D)×1e4
total = delay + exec + opp + fees
```

- 分母用**订单名义额@决策价**（非成交额）：五项之和对买入恒等于 `(E−D)/D·1e4`（全成交情形），
  可手工复核；脚本另用一条独立算术路径（direct）对账，`residual_bps` 正常恒为 0。
- 全成交流程（Q=q）机会成本=0；缺 `order_qty` 视为全成交并记 degraded。
- 缺价降级链：无到达价→以决策价代理（延迟=0）；无决策价→以到达价代理（延迟成本=0）；
  有未成交部分但无期末价→机会成本=0。所有降级都会写进报告 degraded[]。
- 择时（timing）在未提供分段数据时包含「到达后价格漂移」与冲击模型误差，
  是五项里最不可解释的一项；不要把它当执行员水平指标单独引用。

## 2. 费用口径与常量

| 项 | 值 | 适用 |
|---|---|---|
| 佣金 broker | 2.5 bps | 真单券商假设（默认 `--fee-profile broker`） |
| 佣金 matching | 3.0 bps | 回测撮合口径，刻意保守（`--fee-profile matching`） |
| 印花税 | 5.0 bps | **仅 CN 卖出** |
| 过户费 | 0.1 bps | CN 双向 |
| 自定义 | `--commission-bps` | 覆盖佣金（含规费），其余项不变；`none` = 全 0 |

费用按「成交价×成交量」名义额逐笔计（`fees_ccy`），再折算回订单名义额 bps。费用字段可
逐笔用 `fees_bps` 列覆盖（单笔成交时披露 explicit_fees_bps）。

**费率是常量假设，不是从本地数据测出来的**：broker 与 matching 两档并存是因为「真单实际假设（万2.5）」
与「回测撮合（万3，偏保守）」是两个使用场景；量化平台的费用恒等式指的是公式一致（费用都按成交名义额
折算），不是两档费率同值。引用报告时须带上所用 profile 与覆盖值。

## 3. 基准约定

| 基准 | 定义 | 备注 |
|---|---|---|
| 区间 VWAP | Σamount/Σvolume（缺 amount 回退 Σclose×vol） | 默认；成交时刻 ±window/2（默认 ±15 分钟）连续竞价 bar；窗口空取最近一根 |
| 区间 TWAP | 典型价 (H+L+C)/3 等权平均 | 源技能口径（非 close 均值） |
| arrival 到达价 | 成交时刻前最后一根连续 bar 收盘 | 源技能口径；`--quantdb` 另给 09:25 开盘竞价价作「到达价（09:25 竞价）」 |
| 逐笔覆盖 | `benchmark_price` 列 | `--input`：有该列则不做区间计算（自备基准） |

- 逐笔滑点 `slip = dir·(p_fill − bench)/bench×1e4`（**正=劣于基准**，负=优于基准，即价格改善）。
- **超越基准率** = 滑点 ≤ 0 的成交额占比（按成交额加权）。两者都由 `_benchmark_compare` 计算，
  `--benchmark vwap|twap|arrival` 可切换。
- 「超越基准率」高不一定代表执行好：若成交时段行情顺向，任何基准都会显得「被超越」。

## 4. square-root 冲击与参与率校准

- σ_day = 分钟对数收益 std(ddof=1) × √240（日内尺度；年化 ×√244，源技能口径）。
- participation = Q/ADV。ADV 口径：`--input` 用**当日对账窗总量**（含两段集合竞价）；
  `--quantdb` 用**前 20 交易日**日线量均值（不足 20 日时用可得天数；完全不足退回当日量）。
- 默认 k=0.1（源技能默认）。`--calibrate-k`（仅 `--input`）：对每笔
  `(√participation, |滑点 vs 基准|)` 做一元 OLS：`|slip| = a + b·√part`，
  报告 a、b、R²、n、参与率区间，并给 `implied k = b/(σ_day×1e4)`。
- 纪律（源校准技能）：
  - **勿外推**到观测参与率区间之外；
  - 样本 < 5 笔不拟合；参与率无方差（全同量样本）判「拟合退化」拒绝输出斜率；
  - R² 低时 implied k 仅是数量级参考——逐笔「基准滑点」混合了行情漂移与冲击，
    例如本技能 TWAP 回放样本 R²=0.03，斜率不可解释为冲击系数；
  - implied k 只在**专用校准样本**（同一执行算法不同规模、剔除行情方向影响）上才有意义。

## 5. 会话窗口与日总量对账（CN）

标准交易日 239 根分钟 bar：

```
09:25 集合竞价 1 根 + 09:31–11:30 连续 120 根 + 13:01–14:57 连续 117 根 + 15:00 收盘竞价 1 根
```

- 对账窗 canon = auction_open + continuous + auction_close。**14:58/14:59**（深市重复 bar）与
  **15:06–15:30**（沪市盘后固定价格交易，未进入日线）列为 anomalous：展示、计 0、不进对账。
- 恒等式：Σcanon 分钟 volume == `daily_unadjusted.volume`；Σcanon 分钟 amount == 日线 amount。
  5 个标的日实测偏差 ≤ 6.2e-06 %（浮点舍入量级，见 SKILL.md 实测校准表）。
- 单位：分钟 volume=**股**、amount=**万元**（装载时 ×1e4 归一到元）；日线 amount 同为万元。
- 分钟价格为**不复权原始价**（与 `daily_unadjusted` 一致；`daily_forward` 是前复权，混用会静默算错）。
  本技能的核对项 `vwap_in_low_high` 用 `daily_unadjusted` 的 low/high 区间。

## 6. 独立校验记录（2026-10-08）

- **demo 手算**：合成明细经 Fraction 精确算术独立复算，12/12 项一致；两份订单残差均为 0。
- **--input 回放**：另写 pandas 独立实现（不复用 tca.py 任何函数）重算 16 项关键数字 + 6 笔逐笔滑点
  与工具输出逐项对账，全部一致（容差 ≤ 输出契约舍入位）。
- **--quantdb**：5 个标的日（2 只票 × 3 个交易日）VWAP 均落在当日 low~high 内，
  与日线 amount/volume 对账偏差 ≤ 6.2e-06 %。
- **点差代理校准**：600036.SH 2026-07-20 真实盘口快照（4740 条）中位报价点差 2.588 bps，
  其半价差 1.294 bps；分钟 H-L 半幅代理中位 6.473 bps → **代理 ≈ 5.0×**，只作相对比较。

## 7. 已知局限

- **分钟级近似**：本地无逐笔全量盘口，冲击为模型估计、点差为 H-L 半幅代理（≈5× 高估）；
  两者都不能替代盘口级执行分析。
- **数据覆盖**：tick 快照仅 20260511 / 20260720 两个日期；min5 仅 2026-07-20 ~ 07-24 五天；
  **HK/US 无分钟数据**，`--quantdb` 仅支持 CN，港美股只能用 `--input` 自备明细。
- **半日市/停牌**：分钟 bar 缺失时 arrival/end 代理与 ADV 外推会失真（脚本按覆盖比例外推并告警）；
  长期停牌标的的「当日」分钟文件可能为空。
- **费用常量**：仅内置 A 股结构（印花税仅 CN 卖出）；港美股费用结构请 `--commission-bps` 自行给。
- **单位**：订单量按「股」。A 股 1 手=100 股，输入用「手」须自行 ×100——脚本不猜单位。
- **口径**：IS 为单期（订单级）口径，不含融券利息、借券费用、冲击的永久部分等跨日效应。

## 8. 参考文献

- Perold, A. F. (1988). *The Implementation Shortfall: Paper versus Reality*. JPM.
- Almgren, R., Thum, C., Hauptmann, E., Li, H. (2005). *Direct Estimation of Equity Market Impact*. Risk.
- Kissell, R. (2013). *The Science of Algorithmic Trading and Portfolio Management*. Academic Press.
- 源技能：`quantskills/skill-transaction-cost-analysis`、`quantskills/skill-transaction-cost-calibration`（均 GPL-3.0-only）。
