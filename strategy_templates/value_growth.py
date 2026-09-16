# -*- coding: utf-8 -*-
"""价值成长策略 (Value Growth)

[Native · A股] 核心逻辑：Top 30 选股叠加三段硬过滤——估值（PE-TTM ≤25、PB ≤3.5、
PS-TTM ≤6）、规模（总市值 10 亿~500 亿、流通市值 ≥5 亿）、经营质量（净利润 TTM ≥0.5 亿、
营收 TTM ≥5 亿）。

调仓：每 5 个交易日 ｜ 持仓 30 只 ｜ 单期替换 5 只。
参数：见同名 .json（f_* 为 A 股基本面硬约束，建议保留）。
使用：AI-IDE → 策略模板 选择本模板绑定 A 股模型回测；实盘策略 ID 用 sys_value_growth。
风险提示：价值风格持股周期长、容忍回撤换取均值回归，止损宜放宽（json 默认 -10%）。"""
STRATEGY_CONFIG = {
    "class": "RedisRecordingStrategy",
    "kwargs": {
        "signal": "<PRED>",
        "topk": 30,
        "n_drop": 5,
        "f_total_mv_min": 1e9,
        "f_total_mv_max": 5e10,
        "f_float_mv_min": 5e8,
        "f_pe_ttm_min": 0.0,
        "f_pe_ttm_max": 25,
        "f_pb_max": 3.5,
        "f_ps_ttm_max": 6.0,
        "f_net_profit_ttm_min": 5e7,
        "f_revenue_ttm_min": 5e8,
    },
}
