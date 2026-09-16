# -*- coding: utf-8 -*-
"""截面 Alpha 预测策略 (Cross-Sectional Alpha)

[Native · A股] 核心逻辑：按模型预测分做截面加权的 Top 50 组合——高分股权重更高，
单票权重上限 5%，权重与分数正相关但不做激进集中。

调仓：跟随 live_trade_config ｜ 持仓 50 只 ｜ 单票 ≤5%。
参数：见同名 .json（topk / min_score / max_weight）。
使用：AI-IDE → 策略模板 选择本模板绑定 A 股模型回测；实盘策略 ID 用 sys_alpha_cross_section。
风险提示：加权组合在极端行情下头部风险敞口高于等权，注意单票上限与行业集中度。"""
STRATEGY_CONFIG = {
    "class": "RedisWeightStrategy",
    "kwargs": {
        "signal": "<PRED>",
        "topk": 50,
        "min_score": 0.0,
        "max_weight": 0.05,
    }
}
