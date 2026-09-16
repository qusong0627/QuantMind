# -*- coding: utf-8 -*-
"""美股加权核心 (US Weighted Core)

[美股] 时间 9:30–16:00 美东（连续无午休）、T+0 回转、无涨跌停、美元计价 ｜ 信号 <PRED> 为平台美股模型预测分（T 日收盘生成、T+1 生效）。

核心逻辑：按分数加权建仓 Top 25、单票上限 8%，兼顾集中与分散。
调仓：每 5 个交易日 ｜ 持仓：25 只 / 单票 ≤8% ｜ 建议调仓时点：收盘前 15:50 卖出 / 15:58 买入（美东钟，json live_defaults 预填）。
参数：见同名 .json（topk / min_score / max_weight / rebalance_days）。
使用：AI-IDE → 策略模板 → 美股 选择本模板绑定美股模型回测；实盘策略 ID 用 sys_us_weighted_core。
风险提示：跨境资金涉及汇率波动；美股无涨跌停且个股波动大，建议结合止损模板；日内高频交易需注意券商 PDT 规则（保证金账户日内交易限制）。"""
STRATEGY_CONFIG = {
    "class": "RedisWeightStrategy",
    "kwargs": {
        "signal": "<PRED>",
        "topk": 25,
        "min_score": 0.0,
        "max_weight": 0.08,
        "rebalance_days": 5
    }
}
