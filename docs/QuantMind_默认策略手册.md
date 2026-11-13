# QuantMind 默认策略手册

> 版本：2026-08
> 适用范围：AI-IDE 策略模板 / 回测中心默认策略
> 说明：本文档整理 QuantMind 内置的 13 个默认策略，按专业投研产品手册标准编写，覆盖策略逻辑、参数含义、适用场景与风险提示。

---

## 一、策略总览

| # | 策略 | 类名 | 信号源 | Top-K | 核心特征 |
|---|------|------|--------|-------|---------|
| 1 | 标准 Top-K 选股 | `RedisTopkStrategy` | 模型预测 | 50 | Top-K + 零换手约束 |
| 2 | 多空 Top-K 对冲 | `RedisLongShortTopkStrategy` | 模型预测 | 50/50 | 多空双向 + 5 日调仓 |
| 3 | 分数加权组合 | `RedisWeightStrategy` | 模型预测 | 50 | 按分数权重 + 单票上限 |
| 4 | 动量增强选股 | `RedisMomentumStrategy` | 模型预测 | 30 | 动量因子加权 |
| 5 | 动态风控 TOPK | `DynamicRiskTopkStrategy` | 模型预测 + 沪深300 | 50 | 均分择时 + 换池 + 急跌过滤 |
| 6 | 价值成长精选 | `RedisRecordingStrategy` | 模型预测 | 30 | 估值+成长双约束 |
| 7 | 自适应漂移 | `RedisRecordingStrategy` | 模型预测 | 50 | 动态仓位 |
| 8 | 全 Alpha 截面 | `RedisFullAlphaStrategy` | 模型预测 | 50 | 每日调仓 |
| 9 | 深度时序 | `RedisRecordingStrategy` | 模型预测 | 30 | 时序信号 |
| 10 | 止损止盈 | `RedisStopLossStrategy` | 模型预测 | 30 | 止损 -10% / 止盈 +20% |
| 11 | 波动率加权 | `RedisVolatilityWeightedStrategy` | 模型预测 | 50 | 波动率反比权重 |
| 12 | Alpha 截面（简化） | `RedisWeightStrategy` | 模型预测 | 50 | 截面选股简化版 |
| 13 | 指数增强 | `RedisFullAlphaStrategy` | 模型预测 | 50 | 成分股增强 |

---

## 二、策略详解

### 1. 标准 Top-K 选股（Standard Top-K）

**文件**：`strategy_templates/standard_topk.py`
**策略类**：`RedisTopkStrategy`（继承 `TopkDropoutStrategy` + 动态风险 Mixin）

**逻辑**：
- 每日按模型预测分数（`<PRED>`）对全市场排序，取**前 50 名**作为持仓
- 采用 **零换手强制约束**（TopkDropout 机制）：只有当新入选股票分数显著高于现有持仓末位时才会换出，降低交易成本
- 每次最多替换 **10 只**（`n_drop`），控制换手率

**参数**：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `signal` | `<PRED>` | 模型预测分数信号 |
| `topk` | 50 | 持仓股票数量 |
| `n_drop` | 10 | 每期最多替换数量 |

**适用场景**：作为基准选股策略，适合大多数量化组合的基线配置。换手温和、容量适中。

**风险提示**：Top-K 集中于高分股票，若模型信号失效可能集中于少数板块，建议配合行业分散。

---

### 2. 多空 Top-K 对冲（Long-Short Top-K）

**文件**：`strategy_templates/long_short_topk.py`
**策略类**：`RedisLongShortTopkStrategy`（继承 `WeightStrategyBase`）

**逻辑**：
- 做多：取分数最高的 **50 只**（`long_topk`）
- 做空：取分数最低的 **50 只**（`short_topk`），支持融券做空（`enable_short_selling=True`）
- **多空双向对冲**：做多 100% 仓位（`long_exposure=1.0`），做空 100% 仓位（`short_exposure=1.0`），剥离市场 Beta，赚取 Alpha
- **5 个交易日调仓**（`rebalance_days=5`），降低交易频率

**参数**：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `signal` | `<PRED>` | 模型预测分数 |
| `topk` | 50 | 多头股票数量 |
| `short_topk` | 50 | 空头股票数量 |
| `min_score` | 0.0 | 做多最低分数门槛 |
| `max_weight` | 0.05 | 单票最大权重（5%） |
| `long_exposure` | 1.0 | 多头总仓位 |
| `short_exposure` | 1.0 | 空头总仓位 |
| `rebalance_days` | 5 | 调仓周期（交易日） |
| `enable_short_selling` | True | 是否启用做空 |

**适用场景**：市场中性策略，适合对模型选股能力有较强信心、希望剥离大盘风险的投资组合。

**风险提示**：做空需融券渠道；多空双向放大交易成本；空头端模型预测易受小盘股流动性影响。

---

### 3. 分数加权组合（Score-Weighted）

**文件**：`strategy_templates/score_weighted.py`
**策略类**：`RedisWeightStrategy`

**逻辑**：
- 取分数前 **50 名**，按预测分数**比例分配权重**（分数越高权重越大）
- 单票权重上限 **5%**（`max_weight`），防止过度集中
- 分数为负的股票不参与（`min_score=0.0`）

**参数**：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `signal` | `<PRED>` | 模型预测分数 |
| `topk` | 50 | 持仓数量 |
| `min_score` | 0.0 | 最低参与分数 |
| `max_weight` | 0.05 | 单票最大权重 |

**适用场景**：风险偏好中性，希望权重随信号强度调整的增强指数型策略。

---

### 4. 动量增强选股（Momentum-Enhanced）

**文件**：`strategy_templates/momentum.py`
**策略类**：`RedisMomentumStrategy`（继承 `RedisTopkStrategy`）

**逻辑**：
- 在 Top-K 基础上叠加**动量因子**：结合过去 **20 日**（`momentum_period`）动量
- 动量权重 **0.3**（`momentum_weight`），即 30% 动量 + 70% 模型信号
- Top-K 30 只，每期最多替换 6 只

**参数**：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `signal` | `<PRED>` | 模型预测分数 |
| `topk` | 30 | 持仓数量 |
| `n_drop` | 6 | 每期最多替换 |
| `momentum_period` | 20 | 动量回看周期（日） |
| `momentum_weight` | 0.3 | 动量因子权重 |

**适用场景**：趋势市占优，适合对短期动量有一定偏好的进攻型配置。

---

### 5. 动态风控 TOPK（Dynamic Risk Top-K）

**文件**：`strategy_templates/dynamic_risk_topk.py`
**策略类**：`DynamicRiskTopkStrategy`

**逻辑**：在 Top-K 选股上叠加三层风控：
- **均分择时**：当日全市场预测均分 ≥ 0 → 全市场池 + 100% 仓位；< 0 → 沪深300池 + 50% 仓位
- **快速崩盘过滤**：沪深300 近 10 日跌幅 ≤ -6% 或近 5 日 ≤ -4% 时，次日降到 **30%** 仓位，维持 3 个交易日
- **每日仓位检查**：风险档位变化当天强制调整目标仓位，选股仍按 3 日低换手轮动
- 严格目标仓位执行：超配卖出、低配补仓，避免 `risk_degree` 只限买入导致仓位漂移

**参数**：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `topk` | 50 | 持仓数量 |
| `n_drop` | 10 | 每期最多替换数量 |
| `rebalance_days` | 3 | 选股调仓周期（交易日） |
| `score_threshold` | 0.0 | 均分多空分界 |
| `pos_up` / `pos_down` | 1.0 / 0.5 | 正分 / 负分目标仓位 |
| `crash_exposure` | 0.3 | 急跌触发后的目标仓位 |
| `crash_ret10` / `crash_ret5` | -0.06 / -0.04 | 10 日 / 5 日指数跌幅阈值 |
| `crash_hold_days` | 3 | 急跌状态维持交易日数 |
| `defensive_universe` | csi300 | 负分/急跌时的防御池 |

**参考回测**（默认模型，2025-11-13 → 2026-11-12，3 日调仓）：
年化 **+15.04%**、夏普 **1.161**、最大回撤 **-4.31%**；同期沪深300 为 -13.32%。

**适用场景**：希望保留 Top-K 选股 alpha，同时控制单边下跌回撤的稳健型组合。

---

### 6. 价值成长精选（Value-Growth）

**文件**：`strategy_templates/value_growth.py`
**策略类**：`RedisRecordingStrategy`

**逻辑**：
- 结合**估值 + 成长**双维度筛选：
  - 排除 ST
  - 市值 10 亿-500 亿（`f_total_mv_min/max`）
  - PE(TTM) ≤ 25（`f_pe_ttm_max`）
  - ROE ≥ 12%（`f_roe_min`）
  - 上市 ≥ 365 天
- Top-K 30 只，每期替换 5 只

**参数**：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `topk` | 30 | 持仓数量 |
| `n_drop` | 5 | 每期替换 |
| `f_total_mv_min/max` | 10亿 / 500亿 | 市值区间 |
| `f_pe_ttm_max` | 25 | 市盈率上限 |
| `f_roe_min` | 0.12 | ROE 下限 |
| `f_listed_days_min` | 365 | 上市天数下限 |

**适用场景**：偏好估值合理 + 盈利能力强的价值成长风格。

---

### 7. 自适应漂移（Adaptive Drift）

**文件**：`strategy_templates/adaptive_drift.py`
**策略类**：`RedisRecordingStrategy`

**逻辑**：
- Top-K 50 只，每期替换 10 只
- **动态仓位**（`dynamic_position=True`）：根据市场状态自动调整总仓位，市场不佳时降仓防守

**参数**：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `topk` | 50 | 持仓数量 |
| `n_drop` | 10 | 每期替换 |
| `dynamic_position` | True | 是否动态调仓 |

**适用场景**：择时能力较强，希望根据市场状态灵活调整仓位的策略。

---

### 8. 全 Alpha 截面（Full Alpha Cross-Section）

**文件**：`strategy_templates/full_alpha_cross_section.py`
**策略类**：`RedisFullAlphaStrategy`

**逻辑**：
- 每日（`rebalance_days=1`）按全市场 Alpha 截面选股，取前 50
- 单票权重上限 5%
- **高频调仓**捕捉 Alpha

**参数**：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `topk` | 50 | 持仓数量 |
| `max_weight` | 0.05 | 单票上限 |
| `rebalance_days` | 1 | 每日调仓 |

**适用场景**：模型信号日频有效、能覆盖交易成本的进攻型策略。

---

### 9. 深度时序（Deep Time-Series）

**文件**：`strategy_templates/deep_time_series.py`
**策略类**：`RedisRecordingStrategy`

**逻辑**：
- Top-K 30 只，每期替换 6 只
- 面向深度时序模型（LSTM/GRU/Transformer）产出的预测信号

**适用场景**：使用深度学习时序模型作为信号源的策略。

---

### 10. 止损止盈（Stop-Loss / Take-Profit）

**文件**：`strategy_templates/StopLoss.py`
**策略类**：`RedisStopLossStrategy`

**逻辑**：
- Top-K 30 只，每期替换 6 只
- **止损线 -10%**（`stop_loss=-0.10`）：持仓亏损达 10% 强制卖出
- **止盈线 +20%**（`take_profit=0.20`）：盈利达 20% 锁定利润

**参数**：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `topk` | 30 | 持仓数量 |
| `stop_loss` | -0.10 | 止损阈值（-10%） |
| `take_profit` | 0.20 | 止盈阈值（+20%） |

**适用场景**：对回撤敏感、需要明确风险预算的投资者。

---

### 11. 波动率加权（Volatility-Weighted）

**文件**：`strategy_templates/VolatilityWeighted.py`
**策略类**：`RedisVolatilityWeightedStrategy`（继承 `RedisWeightStrategy`）

**逻辑**：
- Top-K 50 只，按预测分数权重
- **波动率反比加权**：回看 **20 日**（`vol_lookback`）波动率，低波动股票获更高权重（风险平价思想）
- 单票权重上限 8%（`max_weight`）

**参数**：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `topk` | 50 | 持仓数量 |
| `vol_lookback` | 20 | 波动率回看周期 |
| `max_weight` | 0.08 | 单票上限 |
| `min_score` | 0.0 | 最低分数 |

**适用场景**：希望控制组合波动、偏好低波动股票的稳健策略。

---

### 12. Alpha 截面（简化版）

**文件**：`strategy_templates/alpha_cross_section.py`
**策略类**：`RedisWeightStrategy`

**逻辑**：与"分数加权组合"相同的配置（Top-50、分数权重、单票上限 5%），作为 Alpha 截面的简化起点。

---

### 13. 指数增强（Index-Enhanced）

**文件**：`strategy_templates/StopLoss.py`（复用）
**策略类**：`RedisFullAlphaStrategy`

**逻辑**：每日在全市场或成分股内做 Alpha 截面增强，单票上限 5%，追求超越基准（如沪深 300）。

---

## 三、策略参数速查表

| 策略 | topk | n_drop | 调仓周期 | 单票上限 | 风控要点 |
|------|------|--------|---------|---------|---------|
| 标准 Top-K | 50 | 10 | 信号日 | - | 零换手约束 |
| 多空对冲 | 50/50 | - | 5日 | 5% | 多空双向 |
| 分数加权 | 50 | - | 信号日 | 5% | 分数门槛 |
| 动量增强 | 30 | 6 | 信号日 | - | 动量 20日 |
| 动态风控 | 50 | 10 | 3日 | 均分100/50 + 急跌30 | 沪深300换池 |
| 价值成长 | 30 | 5 | 信号日 | - | 估值+ROE |
| 自适应漂移 | 50 | 10 | 信号日 | - | 动态仓位 |
| 全 Alpha | 50 | - | 1日 | 5% | 高频 |
| 深度时序 | 30 | 6 | 信号日 | - | 时序信号 |
| 止损止盈 | 30 | 6 | 信号日 | - | -10%/+20% |
| 波动率加权 | 50 | - | 信号日 | 8% | 低波动偏好 |
| Alpha 截面 | 50 | - | 信号日 | 5% | 分数权重 |
| 指数增强 | 50 | - | 1日 | 5% | 基准增强 |

---

## 四、使用说明

### 4.1 在 AI-IDE 中使用
1. 进入 AI-IDE 页面，编辑器左侧选择「策略模板」
2. 选择任一模板（如"标准 Top-K 选股"）
3. 编辑器加载模板代码，`<PRED>` 会被自动替换为当前所选模型的预测信号
4. 可直接运行回测或调整 `STRATEGY_CONFIG` 参数

### 4.2 参数调整建议
- **`topk`**：持仓数量，越大越分散、越小越集中。50 适合中等规模，30 更集中
- **`n_drop`**：换手控制，越大换手越高、信号响应越快。10-20 较常见
- **`rebalance_days`**：调仓频率，1=日频（高 Alpha 捕捉）、5=周频（低换手）
- **`max_weight`**：单票集中度，5% 较分散，10% 以上更集中
- **风控档位**（动态风控）：默认均分正 100%、负 50%、指数急跌 30%，不建议关闭

### 4.3 信号源说明
- 所有策略的 `signal` 默认为 `<PRED>`，即所选模型的每日预测分数
- 模型信号来自 QuantMind 推理引擎（`engine_signal_scores`），每个交易日更新
- 可选模型包括：LGB/XGB/CatBoost 树模型、深度学习时序模型、融合模型

---

## 五、风险提示（免责声明）

1. **策略仅供研究学习**，不构成投资建议
2. 历史回测不代表未来收益，模型信号存在失效风险
3. 做空策略（多空对冲）需确认券商融券可用性
4. 高频调仓（全 Alpha、指数增强）需评估交易成本
5. 止损止盈在极端行情（跳空/跌停）下可能无法按预设价位成交
6. 波动率加权在低波动持续期可能过度集中低波动股票
7. 使用前请充分理解策略逻辑，并根据自身风险承受能力调整参数

---

*本文档由 QuantMind 自动生成，覆盖 AI-IDE 内置全部 13 个默认策略。*
