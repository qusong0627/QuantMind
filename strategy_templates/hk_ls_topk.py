# -*- coding: utf-8 -*-
"""港股多空 TopK (HK Long-Short TopK)

[港股] 时间 9:30–16:00（午休 12:00–13:00）、T+0 回转、无涨跌停 ｜ 信号 <PRED> 为平台港股模型预测分（T 日收盘生成、T+1 生效）。

核心逻辑：多空各 50 只的市场中性对照；港股支持卖空，但融券成本与券源须实盘核实。
调仓：每 5 个交易日 ｜ 持仓：多空各 50 只 / 单票 ≤5% ｜ 建议调仓时点：尾盘 15:50 卖出 / 15:58 买入（json live_defaults 预填）。
参数：见同名 .json（topk / short_topk / exposures）。
使用：AI-IDE → 策略模板 → 港股 选择本模板绑定港股模型回测；实盘策略 ID 用 sys_hk_ls_topk。
风险提示：港股无涨跌停、波动大于 A 股，建议结合止损模板并关注汇率与流动性风险。"""
STRATEGY_CONFIG = {
    "class": "RedisLongShortTopkStrategy",
    "module_path": "backend.services.engine.qlib_app.utils.extended_strategies",
    "kwargs": {"signal": "<PRED>", "topk": 50, "short_topk": 50, "min_score": 0.0, "max_weight": 0.05, "long_exposure": 1.0, "short_exposure": 1.0, "rebalance_days": 5, "enable_short_selling": true},
}
