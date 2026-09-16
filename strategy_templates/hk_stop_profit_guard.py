# -*- coding: utf-8 -*-
"""港股止盈保护 (HK Stop-Profit Guard)

[港股] 时间 9:30–16:00（午休 12:00–13:00）、T+0 回转、无涨跌停 ｜ 信号 <PRED> 为平台港股模型预测分（T 日收盘生成、T+1 生效）。

核心逻辑：Top 25 + 止损 -8% / 止盈 +15%（每 2 日复核），快速兑现浮盈。
调仓：每 2 个交易日 ｜ 持仓：25 只 / 止盈 +15% ｜ 建议调仓时点：尾盘 15:50 卖出 / 15:58 买入（json live_defaults 预填）。
参数：见同名 .json（stop_loss / take_profit）。
使用：AI-IDE → 策略模板 → 港股 选择本模板绑定港股模型回测；实盘策略 ID 用 sys_hk_stop_profit_guard。
风险提示：港股无涨跌停、波动大于 A 股，建议结合止损模板并关注汇率与流动性风险。"""
STRATEGY_CONFIG = {
    "class": "RedisStopLossStrategy",
    "module_path": "backend.services.engine.qlib_app.utils.extended_strategies",
    "kwargs": {"signal": "<PRED>", "topk": 25, "n_drop": 8, "stop_loss": -0.08, "take_profit": 0.15, "rebalance_days": 2},
}
