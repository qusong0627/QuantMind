# -*- coding: utf-8 -*-
"""美股盘后延长时段 (US Extended Hours)

[美股] 时间 9:30–16:00 美东（连续无午休）、T+0 回转、无涨跌停、美元计价 ｜ 信号 <PRED> 为平台美股模型预测分（T 日收盘生成、T+1 生效）。

核心逻辑：标准 Top 30 选股，但调仓落在**盘后延长时段**（16:00–20:00 美东）——展示会话参数化能力：对收盘后发布的财报/新闻，可在盘后先于次日开盘完成调仓。
调仓：每 3 个交易日（盘后 19:50/19:58 美东） ｜ 持仓：30 只 / 替换 6 只 ｜ 建议调仓时点：盘后 19:50 卖出 / 19:58 买入（美东钟，json live_defaults 预填）。
参数：见同名 .json（topk / n_drop / rebalance_days）。
使用：AI-IDE → 策略模板 → 美股 选择本模板绑定美股模型回测；实盘策略 ID 用 sys_us_extended_hours。
风险提示：跨境资金涉及汇率波动；美股无涨跌停且个股波动大，建议结合止损模板；日内高频交易需注意券商 PDT 规则（保证金账户日内交易限制）。"""
STRATEGY_CONFIG = {
    "class": "RedisTopkStrategy",
    "kwargs": {
        "signal": "<PRED>",
        "topk": 30,
        "n_drop": 6,
        "rebalance_days": 3
    }
}
