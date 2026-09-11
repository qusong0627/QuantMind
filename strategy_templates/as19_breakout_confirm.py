# -*- coding: utf-8 -*-
"""A股突破确认 (as19_breakout_confirm)

[A股] 要求股价站上 20 日线 3% 以上、5 日线向上、量能放大，用突破形态确认模型信号。

文件夹：A股策略/04_动量与趋势 ｜ 策略类：RedisRecordingStrategy ｜ 市场：A股
调仓：每 3 个交易日 ｜ 持仓：30 只 ｜ 单期换手：10 只

回测记录（模型 mdl_cn_train_20260906023306_57ce74a7_d5a3faa7，2024-01-02 → 2024-12-31，A股费率/T+1/含交易成本）
    年化 23.91% ｜ 夏普 0.927 ｜ 最大回撤 -14.58% ｜ 基准 16.20% ｜ 交易 1610 笔 ｜ 胜率 49.79%
    对照 standard_topk（平台内置，同模型同区间）：年化 59.26% ｜ 夏普 1.918 ｜ 最大回撤 -18.42%
    注：单一区间单一模型的成绩只用于验证链路与相对比较，不等于未来收益。

怎么用
    1. AI-IDE → 策略模板 → 文件夹「A股策略/04_动量与趋势」→ 选「A股突破确认」，选好模型直接回测；
    2. 参数面板可调 topk / n_drop / rebalance_days；f_* 是 A 股硬约束（基本面/流动性），建议保留；
    3. 命令行单跑（下面就是复现本文件回测记录的命令）：
       docker exec quantmind python /app/scripts/verify_ashare_backtest.py as19_breakout_confirm \
         --start 2024-01-02 --end 2024-12-31 --model-id mdl_cn_train_20260906023306_57ce74a7_d5a3faa7
    4. 实盘：策略 ID 用 sys_as19_breakout_confirm（内置模板在实盘链路里需加 sys_ 前缀）。
    5. 本文件的回测记录由 scripts/run_ashare_backtest_all.py 写入 scripts/ashare_backtest_results.json，
       重跑生成器（scripts/gen_ashare_strategy_templates.py）即刷新；改参数请改生成器，不要手改模板。

由 scripts/gen_ashare_strategy_templates.py 生成；参数说明见同名 .json。
"""

STRATEGY_CONFIG = {
    "class": "RedisRecordingStrategy",
    "module_path": "backend.services.engine.qlib_app.utils.recording_strategy",
    "kwargs": {
        "signal": '<PRED>',
        "only_tradable": True,
        "topk": 30,
        "n_drop": 10,
        "rebalance_days": 3,
        "f_ma_gap_20_min": 3.0,
        "f_ma_gap_5_min": 0.0,
        "f_vol_to_ma20_min": 1.0,
        "f_rsi_14_max": 75,
        "f_amount_ma_5_min": 8000,
    },
}
