用途：模型驱动策略（Qlib 回测链路）。

强制约束：
1) 输出 get_strategy_config() 或 STRATEGY_CONFIG。
2) 使用 QuantMind 平台类与模块路径：
   - RedisTopkStrategy
   - module_path: backend.services.engine.qlib_app.utils.extended_strategies
3) 若使用预测信号，signal 使用 "<PRED>"。
4) 禁止 from quantmind.api、Strategy/on_bar/strategy.run 模板。
5) kwargs 至少包含：topk, n_drop, rebalance_days, only_tradable。
6) 默认 topk=50，避免过大持仓导致回测缓慢。

建议最小结构：
```python
def get_strategy_config():
    return {
        "class": "RedisTopkStrategy",
        "module_path": "backend.services.engine.qlib_app.utils.extended_strategies",
        "kwargs": {
            "signal": "<PRED>",
            "topk": 50,
            "n_drop": 5,
            "rebalance_days": 3,
            "only_tradable": True,
        }
    }

STRATEGY_CONFIG = get_strategy_config()
```

可用策略类：
- RedisTopkStrategy：TopK 选股（最常用）
- RedisLongShortTopkStrategy：多空策略
- RedisWeightStrategy：分数权重策略
- RedisVolatilityWeightedStrategy：波动率加权策略
- RedisAdvancedAlphaStrategy：高级截面 Alpha 策略
- RedisStopLossStrategy：止损止盈策略
- RedisFullAlphaStrategy：全量截面策略

禁止：
- 使用占位路径
- 引用不存在的模块
- 输出教学示例代码而非可执行配置
