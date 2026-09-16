# -*- coding: utf-8 -*-
"""止损止盈策略 (Stop-Loss / Take-Profit Strategy)

[Native · A股] 核心逻辑：Top 30 选股基础上叠加硬止损止盈——持仓浮亏超过 -10% 或
浮盈超过 +20% 即强制平仓并从当期选股池剔除。

调仓：每 3 个交易日 ｜ 持仓 30 只 ｜ 单期替换 6 只 ｜ 止损 -10% / 止盈 +20%。
参数：见同名 .json（stop_loss / take_profit 可按风格调整，平台允许 -20%~-3%）。
使用：AI-IDE → 策略模板 选择本模板绑定 A 股模型回测；实盘策略 ID 用 sys_StopLoss。
风险提示：止盈会截断趋势利润、止损在跳空行情可能滑价成交；两者是风格取舍而非免费午餐。"""
STRATEGY_CONFIG = {
    "class": "RedisStopLossStrategy",
    "kwargs": {
        "signal": "<PRED>",
        "topk": 30,
        "n_drop": 6,
        "stop_loss": -0.10,
        "take_profit": 0.20,
    }
}
