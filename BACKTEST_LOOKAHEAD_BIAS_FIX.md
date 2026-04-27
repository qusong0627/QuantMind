# 回测未来函数修复说明

更新时间：2026-04-28

## 背景

本次修复解决了 Qlib 回测链路存在的前视偏差（Look-ahead Bias）风险。核心问题是"信号日期"和"交易执行日期"没有明确错位。

在旧逻辑中，若某个信号在 T 日使用了 T 日收盘价、成交量或收盘后预测结果生成，回测引擎仍可能在 T 日使用该信号下单成交。这相当于"先看到当天答案，再在当天交易"，会系统性高估策略表现。

## 修复内容

### 1. Schema 新增防护参数

文件：`backend/services/engine/qlib_app/schemas/backtest.py`

新增字段：

```python
signal_lag_days: int = Field(
    1,
    description="信号生效滞后交易日数；默认 T 日信号在 T+1 生效，避免同日收盘信号同日成交",
    ge=0,
    le=5,
)

allow_feature_signal_fallback: bool = Field(
    False,
    description="是否允许预测信号缺失时回退到行情特征信号；默认禁止静默回退到 $close",
)
```

### 2. SimpleSignal 支持信号滞后

文件：`backend/services/engine/qlib_app/utils/simple_signal.py`

核心变化：

- `__init__()` 新增 `signal_lag_days` 参数
- 新增 `_lag_series_by_trading_days()` 方法，按交易日序列后移信号
- `_load_pred_series()` 加载 pred 后调用滞后方法
- `get_signal()` 中对 feature 信号也应用滞后，并向前多取一段窗口

### 3. 动态仓位下一交易日生效

文件：`backend/services/engine/qlib_app/services/market_state_service.py`

旧逻辑：

```text
用 T 日 close/volume 计算状态 -> 写入 T 日 risk_degree
```

新逻辑：

```text
用 T 日 close/volume 计算状态 -> 写入 T+1 日 risk_degree
```

### 4. 回测运行时统一应用信号滞后

文件：`backend/services/engine/qlib_app/services/backtest_service_runtime.py`

核心变化：

- `_enforce_signal_quality()` 增加 `request` 参数，检查 `allow_feature_signal_fallback`
- `_load_pred_pkl()` 传递 `signal_lag_days` 给 `SimpleSignal`
- 新增 `_lag_signal_frame()` 静态方法
- `_build_signal_data()` 中对 feature 信号应用滞后
- 新增 `_feature_fallback_allowed()` 静态方法

### 5. 向量化回测日期对齐增强

文件：`backend/shared/vectorized_backtest/engine.py`

核心变化：

- 在计算收益前取信号日期和价格日期交集
- 要求至少有两个有效日期，否则报错

## 默认安全行为

默认配置下：

```json
{
  "signal_lag_days": 1,
  "allow_feature_signal_fallback": false
}
```

效果：

- 预测信号默认 T+1 生效
- 找不到预测信号时不会自动退回 `$close`
- 可减少"模型信号缺失但回测仍显示高收益"的误判

## 显式允许 feature fallback

如果确实需要使用 `$close` 或其他行情特征做演示/诊断，可以显式开启：

```json
{
  "allow_feature_signal_fallback": true
}
```

也可以通过环境变量开启：

```bash
QLIB_ALLOW_FEATURE_SIGNAL_FALLBACK=true
```

## 已有 pred 文件的日期语义

如果 `pred.pkl` 日期已经表示"可交易生效日"，可以传：

```json
{
  "signal_lag_days": 0
}
```

但只有在数据生产链路明确保证这一点时才建议关闭滞后。
