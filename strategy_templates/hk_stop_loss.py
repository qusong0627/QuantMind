"""
港股止损止盈（港股模板）
[Market] Hong Kong — 港股交易时段 9:30-16:00（午休 12:00-13:00），T+0，无涨跌停。
信号 <PRED> 为平台港股模型预测分（T 日收盘生成、T+1 生效）。
"""
STRATEGY_CONFIG = {
    "class": "RedisStopLossStrategy",
    "module_path": "backend.services.engine.qlib_app.utils.extended_strategies",
    "kwargs": {"signal": "<PRED>", "topk": 30, "n_drop": 6, "stop_loss": -0.12, "take_profit": 0.25, "rebalance_days": 3},
}
