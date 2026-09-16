# -*- coding: utf-8 -*-
"""美股动量先锋 (US Momentum)

[美股] 时间 9:30–16:00 美东（连续无午休）、T+0 回转、无涨跌停、美元计价 ｜ 信号 <PRED> 为平台美股模型预测分（T 日收盘生成、T+1 生效）。

核心逻辑：模型分叠加 20 日动量（权重 30%）的顺势配置，吃趋势主升段。
调仓：每 3 个交易日 ｜ 持仓：30 只 / 替换 6 只 ｜ 建议调仓时点：收盘前 15:50 卖出 / 15:58 买入（美东钟，json live_defaults 预填）。
参数：见同名 .json（topk / momentum_period / momentum_weight / rebalance_days）。
使用：AI-IDE → 策略模板 → 美股 选择本模板绑定美股模型回测；实盘策略 ID 用 sys_us_momentum。
风险提示：跨境资金涉及汇率波动；美股无涨跌停且个股波动大，建议结合止损模板；日内高频交易需注意券商 PDT 规则（保证金账户日内交易限制）。"""
STRATEGY_CONFIG = {
    "class": "RedisMomentumStrategy",
    "kwargs": {
        "signal": "<PRED>",
        "topk": 30,
        "n_drop": 6,
        "momentum_period": 20,
        "momentum_weight": 0.3,
        "rebalance_days": 3
    },
    "module_path": "backend.services.engine.qlib_app.utils.extended_strategies"
}
