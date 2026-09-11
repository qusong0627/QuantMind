"""
大盘风控 Top-K 选股策略 (Risk Guard Top-K)
[Native] 核心逻辑：
1. 先用 features_daily 做估值、规模、波动与趋势硬过滤；
2. 再结合大盘状态自动降仓；
3. 维持 Top-K-Dropout 的低换手优势。
"""

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
