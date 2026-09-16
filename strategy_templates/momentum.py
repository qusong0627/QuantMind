# -*- coding: utf-8 -*-
"""趋势动量策略 (Momentum Strategy)

[Native · A股] 核心逻辑：在模型分基础上叠加 20 日动量（权重 30%）选 Top 30——
强者恒强，吃趋势主升段。

调仓：每 3 个交易日 ｜ 持仓 30 只 ｜ 单期替换 6 只。
参数：见同名 .json（momentum_period / momentum_weight 控制动量强度）。
使用：AI-IDE → 策略模板 选择本模板绑定 A 股模型回测；实盘策略 ID 用 sys_momentum。
风险提示：动量在风格反转期回撤放大，建议结合大盘状态过滤与 stop_loss。"""
STRATEGY_CONFIG = {
    "class": "RedisMomentumStrategy",
    "module_path": "backend.services.engine.qlib_app.utils.extended_strategies",
    "kwargs": {
        "signal": "<PRED>",
        "topk": 30,
        "n_drop": 6,
        "momentum_period": 20,
        "momentum_weight": 0.3,
    }
}
