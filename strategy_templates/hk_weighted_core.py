"""
港股核心加权（港股模板）
[Market] Hong Kong — 港股交易时段 9:30-16:00（午休 12:00-13:00），T+0，无涨跌停。
信号 <PRED> 为平台港股模型预测分（T 日收盘生成、T+1 生效）。
"""
STRATEGY_CONFIG = {
    "class": "RedisWeightStrategy",
    "module_path": "backend.services.engine.qlib_app.utils.extended_strategies",
    "kwargs": {"signal": "<PRED>", "topk": 25, "min_score": 0.0, "max_weight": 0.08, "rebalance_days": 5},
}
