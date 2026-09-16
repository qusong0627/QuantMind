# -*- coding: utf-8 -*-
"""自适应动态调仓策略 (Adaptive Concept Drift)

[Native · A股] 核心逻辑：宽 topk（50）+ 高 n_drop（10）的主动换手配置，用更快的
组合更新速度对抗概念漂移；适合信号半衰期较短的模型。

调仓：每 3 个交易日 ｜ 持仓 50 只 ｜ 单期替换 10 只（换手显著高于标准模板）。
参数：见同名 .json（topk / n_drop / rebalance_days）。
使用：AI-IDE → 策略模板 选择本模板绑定 A 股模型回测；实盘策略 ID 用 sys_adaptive_drift。
风险提示：高换手放大交易成本（印花税+佣金+滑点），请关注成本占比指标。"""
STRATEGY_CONFIG = {
    "class": "RedisRecordingStrategy",
    "kwargs": {
        "signal": "<PRED>",
        "topk": 50,
        "n_drop": 10
    }
}
