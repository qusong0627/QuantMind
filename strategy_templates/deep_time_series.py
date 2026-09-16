# -*- coding: utf-8 -*-
"""深度学习时序预测策略 (Time-Series GRU/LSTM)

[Native · A股] 核心逻辑：直接消费时序模型（GRU/LSTM，.pkl 产物）按日输出的预测信号，
Top 30 选股、每期替换 6 只；不做截面排序后处理，保留时序模型的时间结构。

调仓：每 3 个交易日 ｜ 持仓 30 只 ｜ 单期替换 6 只。
参数：见同名 .json；切换模型后建议先回测验证信号分布（时序模型输出尺度与树模型不同）。
使用：AI-IDE → 策略模板 选择本模板并绑定**时序类**模型回测；实盘策略 ID 用 sys_deep_time_series。
风险提示：时序模型对 regime 切换敏感，建议叠加 stop_loss 并定期滚动再训练。"""
STRATEGY_CONFIG = {
    "class": "RedisRecordingStrategy",
    "kwargs": {
        "signal": "<PRED>",
        "topk": 30,
        "n_drop": 6,
    }
}
