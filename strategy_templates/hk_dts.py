"""
港股深度时序增强（港股模板）
[Market] Hong Kong — 港股交易时段 9:30-16:00（午休 12:00-13:00），T+0，无涨跌停。
信号 <PRED> 为平台港股模型预测分（T 日收盘生成、T+1 生效）。
"""
STRATEGY_CONFIG = {
    "class": "RedisRecordingStrategy",
    "module_path": "backend.services.engine.qlib_app.utils.extended_strategies",
    "kwargs": {"signal": "<PRED>", "topk": 30, "n_drop": 6, "rebalance_days": 5},
}
