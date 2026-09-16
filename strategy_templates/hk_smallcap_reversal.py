# -*- coding: utf-8 -*-
"""港股小盘反转 (HK Small-Cap Reversal)

[港股] 时间 9:30–16:00（午休 12:00–13:00）、T+0 回转、无涨跌停 ｜ 信号 <PRED> 为平台港股模型预测分（T 日收盘生成、T+1 生效）。

核心逻辑：高分散小盘反转（Top 60、每 2 日替换 20 只），捕捉超跌反弹。
调仓：每 2 个交易日 ｜ 持仓：60 只 / 替换 20 只 ｜ 建议调仓时点：尾盘 15:50 卖出 / 15:58 买入（json live_defaults 预填）。
参数：见同名 .json（topk / n_drop / rebalance_days）。
使用：AI-IDE → 策略模板 → 港股 选择本模板绑定港股模型回测；实盘策略 ID 用 sys_hk_smallcap_reversal。
风险提示：港股无涨跌停、波动大于 A 股，建议结合止损模板并关注汇率与流动性风险。"""
STRATEGY_CONFIG = {
    "class": "RedisTopkStrategy",
    "module_path": "backend.services.engine.qlib_app.utils.extended_strategies",
    "kwargs": {"signal": "<PRED>", "topk": 60, "n_drop": 20, "rebalance_days": 2},
}
