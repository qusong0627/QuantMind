用途：模型驱动策略（Qlib 回测链路）。

强制约束：
1) 输出 get_strategy_config() 或 STRATEGY_CONFIG。
2) 使用 QuantMind 平台类与模块路径：
   - 优先：RedisRecordingStrategy（支持 f_ 参数化基本面硬过滤）
   - module_path: backend.services.engine.qlib_app.utils.recording_strategy
   - 若用户明确要求多空，再使用 RedisLongShortTopkStrategy（module_path: backend.services.engine.qlib_app.utils.extended_strategies）
3) 若使用预测信号，signal 使用 "<PRED>"。
4) 禁止 from quantmind.api、Strategy/on_bar/strategy.run 模板。
5) kwargs 至少包含：topk, n_drop, rebalance_days, only_tradable。
6) 当用户提到因子/基本面过滤时，必须优先使用 `f_` 前缀参数（如 `f_pe_ttm_max`, `f_roe_min`, `f_total_mv_min`, `f_is_st_not`），不要把这类静态过滤写在 `generate_target_weight_position` 里。
7) 禁止输出不存在字段；字段命名按 `fundamental_aligned.parquet` / 平台文档口径。
8) 默认 topk=50，避免过大持仓导致回测缓慢。

建议最小结构：
```python
def get_strategy_config():
    return {
        "class": "RedisRecordingStrategy",
        "module_path": "backend.services.engine.qlib_app.utils.recording_strategy",
        "kwargs": {
            "signal": "<PRED>",
            "topk": 50,
            "n_drop": 5,
            "rebalance_days": 3,
            "only_tradable": True,
            "f_is_st_not": 1,
            "f_listed_days_min": 120,
            "f_pe_ttm_max": 30,
        }
    }

STRATEGY_CONFIG = get_strategy_config()
```

可用策略类：
- RedisRecordingStrategy：模型 TopK + f_ 因子硬过滤（默认首选）
- RedisTopkStrategy：TopK 选股（最常用）
- RedisLongShortTopkStrategy：多空策略
- RedisWeightStrategy：分数权重策略
- RedisAdvancedAlphaStrategy：高级截面 Alpha 策略
- RedisStopLossStrategy：止损止盈策略

禁止：
- 使用占位路径
- 引用不存在的模块
- 输出教学示例代码而非可执行配置
- 在静态因子过滤场景忽略 `f_` 前缀而改写自定义 Python 过滤逻辑
