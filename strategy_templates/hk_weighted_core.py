# -*- coding: utf-8 -*-
"""港股加权核心 (HK Weighted Core)

[港股] 时间 9:30–16:00（午休 12:00–13:00）、T+0 回转、无涨跌停 ｜ 信号 <PRED> 为平台港股模型预测分（T 日收盘生成、T+1 生效）。

核心逻辑：按分数加权建仓 Top 25、单票上限 8%，兼顾集中与分散。
调仓：每 5 个交易日 ｜ 持仓：25 只 / 单票 ≤8% ｜ 建议调仓时点：尾盘 15:50 卖出 / 15:58 买入（json live_defaults 预填）。
参数：见同名 .json（topk / min_score / max_weight）。
使用：AI-IDE → 策略模板 → 港股 选择本模板绑定港股模型回测；实盘策略 ID 用 sys_hk_weighted_core。
风险提示：港股无涨跌停、波动大于 A 股，建议结合止损模板并关注汇率与流动性风险。"""
STRATEGY_CONFIG = {
    "class": "RedisWeightStrategy",
    "module_path": "backend.services.engine.qlib_app.utils.extended_strategies",
    "kwargs": {"signal": "<PRED>", "topk": 25, "min_score": 0.0, "max_weight": 0.08, "rebalance_days": 5},
}
