# -*- coding: utf-8 -*-
"""A股跌破均线后企稳低吸 (as23_deep_dip)

[A股] 只买「跌破 20 日线 0~8%、但已站回 5 日线」的企稳票（RSI<60、成交额 5000 万以上），赚短期超跌后的修复，仓位 70%。

文件夹：A股策略/05_反转与均值回归 ｜ 策略类：RedisRecordingStrategy ｜ 市场：A股
调仓：每 3 个交易日 ｜ 持仓：15 只 ｜ 单期换手：10 只

回测记录（模型 mdl_cn_train_20260906023306_57ce74a7_d5a3faa7，2024-01-02 → 2024-12-31，A股费率/T+1/含交易成本）
    年化 33.61% ｜ 夏普 1.453 ｜ 最大回撤 -11.65% ｜ 基准 16.20% ｜ 交易 1543 笔 ｜ 胜率 54.43%
    对照 standard_topk（平台内置，同模型同区间）：年化 59.26% ｜ 夏普 1.918 ｜ 最大回撤 -18.42%
    注：单一区间单一模型的成绩只用于验证链路与相对比较，不等于未来收益。

怎么用
    1. AI-IDE → 策略模板 → 文件夹「A股策略/05_反转与均值回归」→ 选「A股跌破均线后企稳低吸」，选好模型直接回测；
    2. 参数面板可调 topk / n_drop / rebalance_days；f_* 是 A 股硬约束（基本面/流动性），建议保留；
    3. 命令行单跑（下面就是复现本文件回测记录的命令）：
       docker exec quantmind python /app/scripts/verify_ashare_backtest.py as23_deep_dip \
         --start 2024-01-02 --end 2024-12-31 --model-id mdl_cn_train_20260906023306_57ce74a7_d5a3faa7
    4. 实盘：策略 ID 用 sys_as23_deep_dip（内置模板在实盘链路里需加 sys_ 前缀）。
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
        "topk": 15,
        "n_drop": 10,
        "rebalance_days": 3,
        "risk_degree": 0.7,
        "f_ma_gap_20_min": -8.0,
        "f_ma_gap_20_max": 0.0,
        "f_ma_gap_5_min": 0.0,
        "f_rsi_14_max": 60,
        "f_amount_ma_5_min": 5000,
    },
}
