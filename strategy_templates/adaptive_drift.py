"""
自适应动态调仓策略 (Adaptive Concept Drift)
[Native] 核心逻辑：模型信号 Top-K 选股（宽 topk + 高 n_drop，偏灵活换手）。
"""
STRATEGY_CONFIG = {
    "class": "RedisRecordingStrategy",
    "kwargs": {
        "signal": "<PRED>",
        "topk": 50,
        "n_drop": 10
    }
}
