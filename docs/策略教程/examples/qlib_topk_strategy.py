# -*- coding: utf-8 -*-
"""示例 1：Qlib 声明式标准 Top-K 选股。

使用位置：回测中心「专家模式」/ 策略管理 / 模板。
注意：signal="<PRED>" 需要回测请求传 model_id；否则会提示无信号。
"""

STRATEGY_CONFIG = {
    "class": "RedisTopkStrategy",
    "kwargs": {
        "signal": "<PRED>",
        "topk": 50,
        "n_drop": 10,
        "rebalance_days": 3,
    },
}
