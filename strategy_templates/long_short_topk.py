# -*- coding: utf-8 -*-
"""多空 TopK 策略 (Long-Short TopK)

[Native · A股] 核心逻辑：做多最高分 Top 50、同时做空最低分 Top 50 的市场中性对照配置，
多空敞口各 1.0。

调仓：每 5 个交易日 ｜ 多空各 50 只 ｜ 单票 ≤5%。
参数：见同名 .json（topk / short_topk / long_exposure / short_exposure）。
使用：AI-IDE → 策略模板 选择本模板绑定 A 股模型回测（做空分支用于研究对照）；
**实盘仅做多**——A 股现货无裸卖空，实盘请使用系统提示忽略 short 分支。
风险提示：A 股融券成本高、券源不稳定，实盘不可按回测多空口径直接复现。"""
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
