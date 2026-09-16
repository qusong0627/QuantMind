# -*- coding: utf-8 -*-
"""大盘风控 Top-K 选股策略 (Risk Guard Top-K)

[Native · A股] 核心逻辑（三层）：
1. 硬过滤——市值/波动/趋势/估值（beta ≤1.5、vol_std ≤6、MA 乖离 ≥-12%、PE ≤80）；
2. 大盘状态降仓——20 日窗口判断市场状态自动降低敞口；
3. 行业约束——单行业 ≤30% 权重上限，保持 Top-K-Dropout 低换手。

调仓：每 3 个交易日 ｜ 持仓 50 只 ｜ 单期替换 10 只。
参数：见同名 .json（industry_cap_ratio / market_state_window / f_*）。
使用：AI-IDE → 策略模板 选择本模板绑定 A 股模型回测；实盘策略 ID 用 sys_risk_guard_topk。
风险提示：降仓逻辑基于历史状态分布，极端单边下跌中仍可能滞后；属风险缓释而非保本。"""

STRATEGY_CONFIG = {
    "class": "RedisRiskGuardTopkStrategy",
    "module_path": "backend.services.engine.qlib_app.utils.extended_strategies",
    "kwargs": {
        "signal": "<PRED>",
        "topk": 50,
        "n_drop": 10,
        "rebalance_days": 3,
        "max_industry_count": 0,
        "industry_cap_ratio": 0.30,
        "market_state_window": 20,
        "f_total_mv_min": 2000000000.0,
        "f_beta_20_max": 1.5,
        "f_float_mv_min": 500000000.0,
        "f_vol_std_20_max": 6.0,
        "f_ma_gap_20_min": -12.0,
        "f_pe_ttm_min": 0.0,
        "f_pe_ttm_max": 80.0,
    }
}
