# -*- coding: utf-8 -*-
"""得分加权组合策略 (Score-Weighted)

[Native · A股] 核心逻辑：权重 = Score / ΣScores 的软最大化分配，单票上限 5%——
分数差距被按比例体现，避免等权稀释强信号。

调仓：跟随 live_trade_config ｜ 持仓 50 只 ｜ 单票 ≤5%。
参数：见同名 .json（topk / min_score / max_weight）。
使用：AI-IDE → 策略模板 选择本模板绑定 A 股模型回测；实盘策略 ID 用 sys_score_weighted。
风险提示：分数分布的头部跳变会直接放大到权重，建议先在回测中检查权重分布。"""
STRATEGY_CONFIG = {
    "class": "RedisWeightStrategy",
    "kwargs": {
        "signal": "<PRED>",
        "topk": 50,
        "min_score": 0.0,
        "max_weight": 0.05,
    }
}
