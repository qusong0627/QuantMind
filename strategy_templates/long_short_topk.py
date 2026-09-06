"""
多空 TopK 策略 (Long-Short TopK)
[Native] 核心逻辑：做多最高分 TopK + 做空最低分 TopK。
"""
STRATEGY_CONFIG = {
    "class": "RedisLongShortTopkStrategy",
    "kwargs": {
        "signal": "<PRED>",
        "topk": 50,
        "short_topk": 50,
        "min_score": 0.0,
        "max_weight": 0.05,
        "long_exposure": 1.0,
        "short_exposure": 1.0,
        "rebalance_days": 5
    }
}
