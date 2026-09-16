# -*- coding: utf-8 -*-
"""默认 Top-K 选股策略 (Standard Top-K Strategy)

[Native · A股] 平台**对照基准模板**：仅按模型预测分取 Top 50、每期替换 10 只，
不做任何风格/基本面过滤——其他模板的改进效果应相对本模板衡量。

调仓：跟随 live_trade_config（默认每 3 个交易日）｜ 持仓 50 只 ｜ 单期替换 10 只。
参数：见同名 .json（topk / n_drop）。
使用：AI-IDE → 策略模板 选择本模板绑定 A 股模型回测；实盘策略 ID 用 sys_standard_topk。
风险提示：无风格约束，模型失效期回撤直接暴露；建议搭配止损或风控增强模板。"""
STRATEGY_CONFIG = {
    "class": "RedisTopkStrategy",
    "kwargs": {
        "signal": "<PRED>",
        "topk": 50,
        "n_drop": 10,
    }
}
