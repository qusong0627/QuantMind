# 🏗️ 多空策略系统架构图

## 系统总览

```
┌──────────────────────────────────────────────────────────────────────┐
│                    Quantmind 多空策略回测系统架构                      │
└──────────────────────────────────────────────────────────────────────┘

                              ┌─────────────────────┐
                              │   输入: 预测分数    │
                              │  scores = [-0.8,   │
                              │           0.5, ...]│
                              └──────────┬──────────┘
                                         │
                 ┌───────────────────────┴───────────────────────┐
                 │                                               │
                 ▼                                               ▼
    ┌──────────────────────────┐                  ┌──────────────────────────┐
    │  信号生成层               │                  │  策略配置层              │
    │  (pipeline_service.py)   │                  │  (strategy_templates/)  │
    ├──────────────────────────┤                  ├──────────────────────────┤
    │ • enable_short_selling   │                  │ • long_short_topk.json  │
    │ • score -> signal        │                  │ • long_short_topk.py    │
    │ • position_side mapping  │                  │ • 参数配置              │
    │ • is_margin_trade flag   │                  └──────────────────────────┘
    └──────────────┬───────────┘
                   │
                   ▼
    ┌──────────────────────────────────────────────────────────┐
    │          回测请求构建 (QlibBacktestRequest)              │
    ├──────────────────────────────────────────────────────────┤
    │ • strategy_type: "long_short_topk"                       │
    │ • topk: 50          # 做多数量                           │
    │ • short_topk: 50    # 做空数量 ⭐                        │
    │ • enable_short_selling: True ⭐                          │
    │ • long_exposure: 1.0 / short_exposure: 1.0 ⭐          │
    │ • max_leverage: 1.0 ⭐                                   │
    │ • 交易成本、利率等                                      │
    └──────────────┬───────────────────────────────────────────┘
                   │
                   ▼
    ┌──────────────────────────────────────────────────────────┐
    │        策略构建器 (strategy_builder.py)                  │
    ├──────────────────────────────────────────────────────────┤
    │ LongShortTopkBuilder:                                    │
    │   ├─ 读取参数                                            │
    │   ├─ 注册 RedisLongShortTopkStrategy                     │
    │   └─ 返回策略配置 JSON                                  │
    └──────────────┬───────────────────────────────────────────┘
                   │
                   ▼
    ┌──────────────────────────────────────────────────────────┐
    │        Qlib 回测引擎 (backtest_service.py)               │
    ├──────────────────────────────────────────────────────────┤
    │ 1. 初始化:                                               │
    │    └─ ensure_margin_backtest_support()                   │
    │       (注册 MarginPosition/MarginAccount)                │
    │                                                           │
    │ 2. 交易日循环:                                           │
    │    └─ for each trading_day:                              │
    │       ├─ 加载今日信号                                    │
    │       ├─ 调用 RedisLongShortTopkStrategy                │
    │       │  ├─ 分离 long/short 评分                        │
    │       │  ├─ 计算权重 (respect exposure limits)          │
    │       │  └─ 生成 trading decision                       │
    │       ├─ 执行订单                                        │
    │       └─ 更新账户状态                                    │
    │                                                           │
    │ 3. 账户管理 (MarginAccount):                             │
    │    ├─ 融券卖出 → 冻结现金                               │
    │    ├─ 每日计息 (6% p.a.)                               │
    │    └─ 平仓结算盈亏                                      │
    └──────────────┬───────────────────────────────────────────┘
                   │
                   ▼
    ┌──────────────────────────────────────────────────────────┐
    │          回测结果分析 (trade_stats_service.py)            │
    ├──────────────────────────────────────────────────────────┤
    │ • 交易统计 (total_trades, avg_holding_days)             │
    │ • 胜率/利润因子 (win_rate, profit_loss_ratio)           │
    │ • 盈亏分布 (PnL histogram)                              │
    │ • 交易频率 (trade_frequency_series)                     │
    │ • 风险指标 (Sharpe, Sortino 等)                         │
    └──────────────┬───────────────────────────────────────────┘
                   │
                   ▼
              ┌─────────────┐
              │ 回测结果    │
              │ • 收益率    │
              │ • 最大回撤  │
              │ • 夏普率    │
              │ • 交易明细  │
              └─────────────┘
```

---

## 详细流程: 做空交易全生命周期

```
┌────────────────────────────────────────────────────────────────┐
│                    做空交易完整流程                             │
└────────────────────────────────────────────────────────────────┘

═══════════════════════════════════════════════════════════════════
阶段1: 信号生成
═══════════════════════════════════════════════════════════════════

输入: 预测分数 score = -0.8 (负数 = 看空)
      
      ┌──────────────────────────────────────────┐
      │ pipeline_service.py (Line 468-486)       │
      ├──────────────────────────────────────────┤
      │ if enable_short_selling and score < 0:  │
      │    signal = {                            │
      │      "side": "SELL",       # ⭐ 卖出    │
      │      "position_side": "short",           │
      │      "trade_action": "sell_to_open",     │
      │      "is_margin_trade": True             │
      │    }                                      │
      └──────────────────────────────────────────┘
      
输出: 做空信号 → 交易队列

═══════════════════════════════════════════════════════════════════
阶段2: 权重计算
═══════════════════════════════════════════════════════════════════

      ┌──────────────────────────────────────────┐
      │ extended_strategies.py (Line 210-322)    │
      │ RedisLongShortTopkStrategy               │
      │ .generate_target_weight_position()       │
      ├──────────────────────────────────────────┤
      │ short_scores = score[score < -threshold] │
      │ short_scores = short_scores.nsmallest(   │
      │   self.short_topk)                       │
      │ → 取分数最低的 short_topk 只             │
      │                                           │
      │ short_weights = _build_side_weights(     │
      │   short_scores,                          │
      │   actual_short_exposure)                 │
      │ → 权重归一化, 单票不超 max_weight        │
      └──────────────────────────────────────────┘

权重示例:
  SH600000: -0.05  (权重 5%)
  SZ000001: -0.03  (权重 3%)
  ...
  总敞口: 1.0 (100%)

═══════════════════════════════════════════════════════════════════
阶段3: 融券开空 (Sell to Open)
═══════════════════════════════════════════════════════════════════

交易执行:
  SELL 100 shares @ 10.0 = 1000 元
  
      ┌──────────────────────────────────────────┐
      │ margin_position.py (Line 106-139)        │
      │ MarginPosition._sell_stock()             │
      ├──────────────────────────────────────────┤
      │ if old_amount <= 0:  # 开空              │
      │   # 卖出所得现金被冻结                   │
      │   cash -= commission  # 只扣手续费       │
      │   short_proceeds += 1000  # 冻结         │
      │                                           │
      │   # 创建空头头寸                         │
      │   position["SH600000"] = {                │
      │     "amount": -100,  # 负数=空头        │
      │     "price": 10.0    # 开仓价           │
      │   }                                      │
      └──────────────────────────────────────────┘

账户状态 (开仓后):
  ├─ cash: 998 (1000 - 2元手续费)
  ├─ short_proceeds: 1000 (冻结现金)
  ├─ position["SH600000"]: {amount: -100, price: 10.0}
  └─ total_assets: 初始资金 + 盈亏 (此时为 0)

═══════════════════════════════════════════════════════════════════
阶段4: 持仓期间 - 每日计息
═══════════════════════════════════════════════════════════════════

每个交易日 (margin_position.py Line 176-220):
  
  ┌──────────────────────────────────────────┐
  │ MarginAccount._apply_daily_interest()    │
  ├──────────────────────────────────────────┤
  │ 计算空头负债 (融券费用)                   │
  │ short_debt_value = |amount| * close_price│
  │                  = 100 * 9.8 = 980 元   │
  │                                           │
  │ 融券年化费率 8% → 日费率 8%/365           │
  │ daily_interest = 980 * 0.08/365 = 0.215元│
  │                                           │
  │ cash -= 0.215  # 从现金扣除利息           │
  └──────────────────────────────────────────┘

日利息累计 (示例 30 天持仓):
  总利息 ≈ 980 * 0.08/365 * 30 ≈ 6.4 元

═══════════════════════════════════════════════════════════════════
阶段5: 平仓 (Buy to Close)
═══════════════════════════════════════════════════════════════════

平仓信号: score >= threshold (或止损/止盈触发)
  BUY 100 shares @ 9.0 = 900 元 (低买 = 盈利)
  
      ┌──────────────────────────────────────────┐
      │ margin_position.py (Line 71-104)         │
      │ MarginPosition._buy_stock()              │
      ├──────────────────────────────────────────┤
      │ if old_amount < 0:  # 存在空头           │
      │   cover_amount = 100  # 平仓数量         │
      │                                           │
      │   # 计算实现盈亏                         │
      │   realized_pnl = (entry_price - 平仓价) │
      │               * cover_amount - 手续费   │
      │               = (10.0 - 9.0) * 100 - 2 │
      │               = 100 - 2 = 98 元 ✓      │
      │                                           │
      │   cash += realized_pnl  # 98元加入现金  │
      │                                           │
      │   # 扣减冻结资金                         │
      │   short_proceeds -= (10.0 * 100)        │
      │                  = 1000 - 1000 = 0      │
      │                                           │
      │   # 删除头寸 (已平)                      │
      │   del position["SH600000"]               │
      └──────────────────────────────────────────┘

账户状态 (平仓后):
  ├─ cash: 1096 (初始1000 + 盈利98 - 总利息6)
  ├─ short_proceeds: 0 (冻结资金已释放)
  ├─ position: {} (空)
  └─ total_assets: 1096 (全现金)

═══════════════════════════════════════════════════════════════════
```

---

## 数据流向图

```
┌─────────────────────────────────────────────────────────────┐
│          数据层级与责任划分                                  │
└─────────────────────────────────────────────────────────────┘

交易层 (Trade Service)
├─ Order Model (数据库)
│  ├─ symbol, side (BUY/SELL)
│  ├─ position_side (LONG/SHORT) ⭐
│  ├─ trade_action (SELL_TO_OPEN/BUY_TO_CLOSE) ⭐
│  ├─ is_margin_trade (融资融券标记) ⭐
│  └─ quantity, price, commission
│
│
策略层 (Strategy Service)
├─ Pipeline Request
│  ├─ enable_short_selling: bool ⭐
│  ├─ max_short_exposure: float ⭐
│  ├─ max_leverage: float ⭐
│  └─ [其他多空参数]
│
├─ Signal Event
│  ├─ symbol, score
│  ├─ position_side (from score)
│  ├─ is_margin_trade (from score)
│  └─ trade_action
│
│
回测层 (Backtest Service)
├─ QlibBacktestRequest
│  ├─ strategy_type: "long_short_topk" ⭐
│  ├─ topk, short_topk ⭐
│  ├─ long_exposure, short_exposure ⭐
│  ├─ enable_short_selling ⭐
│  └─ [成本、利率等]
│
├─ Strategy Config (To Qlib)
│  ├─ class: "RedisLongShortTopkStrategy" ⭐
│  ├─ kwargs: {topk, short_topk, exposures, ...}
│  └─ [市场状态、风险控制等]
│
│
持仓层 (Position Management)
├─ MarginPosition
│  ├─ position[symbol] = {amount, price}
│  │  └─ amount > 0: 多头, amount < 0: 空头 ⭐
│  ├─ short_proceeds: 冻结现金 ⭐
│  └─ _buy_stock(), _sell_stock()
│
├─ MarginAccount
│  ├─ 每日计息 (_apply_daily_interest) ⭐
│  ├─ 盈亏结算 (_update_state_from_order) ⭐
│  └─ 杠杆约束 (check_account_stop_loss)
│
│
分析层 (Analysis Service)
└─ TradeStatsService
   ├─ win_rate, profit_factor
   ├─ pnl_distribution
   └─ [其他风险指标]
```

---

## 关键约束关系

```
┌──────────────────────────────────────────────────────┐
│ 多空策略中的约束方程                                 │
└──────────────────────────────────────────────────────┘

1️⃣ 敞口约束 (Exposure Constraints)
   ─────────────────────────────────────
   
   long_exposure + short_exposure ≤ max_leverage
   
   示例: long=1.0 + short=1.0 = 2.0 (允许 2:1 杠杆)
        如果 max_leverage=1.5, 则按比例缩放
   
   ┌─────────────────────────────────┐
   │ if actual_long + actual_short >  │
   │    max_total_leverage:           │
   │   scale = max_total_leverage /   │
   │           (long + short)         │
   │   actual_long *= scale           │
   │   actual_short *= scale          │
   └─────────────────────────────────┘


2️⃣ 权重约束 (Weight Constraints)
   ─────────────────────────────────────
   
   for each position:
     |weight[i]| ≤ max_weight
     
   sum(long_weights) ≤ long_exposure
   sum(short_weights) ≤ short_exposure
   
   ┌─────────────────────────────────┐
   │ cap = min(max_weight,           │
   │          target_exposure)       │
   │ weights[i] = scaled[i]          │
   │   (clipped to cap, reallocate   │
   │    remainder iteratively)       │
   └─────────────────────────────────┘


3️⃣ 动态授信约束 (Dynamic Credit Constraint)
   ─────────────────────────────────────
   
   credit_ratio = current_equity / initial_capital
   
   actual_short = min(req_short, credit_ratio)
   remaining = max(0, credit_ratio - actual_short)
   actual_long = min(req_long, 1.0 + remaining)
   
   示例:
   ├─ 初始资金 = 1000, 要求 long=1.5, short=1.0
   ├─ 当前净值 = 900 (亏损10%)
   ├─ credit_ratio = 900/1000 = 0.9
   ├─ actual_short = min(1.0, 0.9) = 0.9 ✓ (缩水)
   ├─ remaining = 0.9 - 0.9 = 0
   └─ actual_long = min(1.5, 1.0) = 1.0 (无额度) ✗


4️⃣ 爆仓保护 (Liquidation Protection)
   ─────────────────────────────────────
   
   if current_equity < initial_capital * (1 - account_stop_loss):
     ├─ 强制平仓所有头寸
     ├─ 停止所有交易
     └─ 账户进入锁定状态
   
   示例 (account_stop_loss=0.2):
   ├─ 初始 = 1000
   ├─ 止损线 = 1000 * (1 - 0.2) = 800
   └─ 当净值 < 800 时触发


5️⃣ 融券成本 (Margin Interest)
   ─────────────────────────────────────
   
   daily_interest = short_debt_value * daily_rate * days
   
   其中:
   - short_debt_value = sum(|amount[i]| * price[i])
   - daily_rate = annual_rate / 365
   - days = 交易日数差
   
   示例 (年化6%, 30天, 空头市值1000):
   ├─ daily_rate = 0.06 / 365 = 0.000164
   ├─ interest = 1000 * 0.000164 * 30 = 4.93 元
   └─ 从 cash 中扣除
```

---

## 测试覆盖矩阵

```
┌────────────────────────────────────────────────────────────┐
│ 多空策略测试覆盖矩阵                                       │
└────────────────────────────────────────────────────────────┘

层级        │ 已有测试                  │ 关键验证点
────────────┼──────────────────────────┼─────────────────
信号生成    │ ✅ test_pipeline_service │ 负分 → 做空信号
            │ _marks_negative_scores  │ position_side=SHORT
            │                         │ is_margin_trade=True
────────────┼──────────────────────────┼─────────────────
持仓管理    │ ✅ test_simulation_      │ 开空: short_proceeds
            │ manager_supports_margin │ 平空: realized_pnl
            │ _short_open_and_close   │ 盈亏计算正确
────────────┼──────────────────────────┼─────────────────
股票池      │ ✅ test_margin_stock_   │ 融资融券股票池
            │ pool_service_loads_and_ │ 符号标准化
            │ normalizes_symbols      │ (SH600000等)
────────────┼──────────────────────────┼─────────────────
权重计算    │ ⏳ TODO                  │ 敞口约束
            │                         │ 权重上限
            │                         │ 多空去重
────────────┼──────────────────────────┼─────────────────
每日计息    │ ⏳ TODO                  │ 融券成本计算
            │                         │ 现金扣除逻辑
────────────┼──────────────────────────┼─────────────────
风险管理    │ ⏳ TODO                  │ 爆仓止损
            │                         │ 动态授信
────────────┼──────────────────────────┼─────────────────
回测执行    │ ⏳ TODO                  │ 端对端回测
            │                         │ 结果正确性
────────────┴──────────────────────────┴─────────────────

✅ = 已覆盖  ⏳ = 建议补充
```

---

## 配置示例

```yaml
# 保守型多空策略配置
conservative_long_short:
  strategy_type: "long_short_topk"
  topk: 30
  short_topk: 30
  long_exposure: 0.6     # 60% 做多
  short_exposure: 0.4    # 40% 做空
  max_leverage: 1.0      # 无杠杆
  account_stop_loss: 0.15 # 净值 < 85% 时止损
  rebalance_days: 20
  enable_short_selling: true

# 中等风险多空策略配置
moderate_long_short:
  strategy_type: "long_short_topk"
  topk: 50
  short_topk: 50
  long_exposure: 1.0
  short_exposure: 1.0
  max_leverage: 1.5      # 1.5:1 杠杆
  account_stop_loss: 0.20
  rebalance_days: 10
  enable_short_selling: true

# 激进型多空策略配置
aggressive_long_short:
  strategy_type: "long_short_topk"
  topk: 100
  short_topk: 100
  long_exposure: 1.5
  short_exposure: 1.5
  max_leverage: 2.0      # 2:1 杠杆 ⚠️
  account_stop_loss: 0.30
  rebalance_days: 5
  enable_short_selling: true
  financing_rate: 0.10   # 融资成本 10% 📈
  borrow_rate: 0.10      # 融券成本 10% 📈
```

