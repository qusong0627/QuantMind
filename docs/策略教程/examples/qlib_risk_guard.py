# -*- coding: utf-8 -*-
"""示例 2：Qlib 风控 + 基本面过滤。

过滤条件会使用上一交易日的基本面快照，避免前视偏差。
参数单位：市值=元，成交额=万元，ROE=比率（0.12 = 12%）。
"""

STRATEGY_CONFIG = {
    "class": "RedisRecordingStrategy",
    "kwargs": {
        "signal": "<PRED>",
        "topk": 30,
        "n_drop": 6,
        "rebalance_days": 5,
        # 基本面硬过滤
        "f_is_st_not": 1,
        "f_total_mv_min": 3_000_000_000,
        "f_total_mv_max": 50_000_000_000,
        "f_amount_ma_5_min": 5000,
        "f_pe_ttm_min": 0,
        "f_pe_ttm_max": 60,
        "f_roe_min": 0.10,
        "f_listed_days_min": 365,
        # 账户级风控
        "account_stop_loss": 0.80,
        "max_leverage": 1.0,
    },
}
