# -*- coding: utf-8 -*-
"""A股个股止损 (as42_stop_loss_guard)

[A股] 平台内置止损类：个股回撤 8% 止损、盈利 15% 止盈，配合模型 TopK 选股。

文件夹：A股策略/09_事件与择时 ｜ 策略类：RedisStopLossStrategy ｜ 市场：A股
调仓：每 3 个交易日 ｜ 持仓：30 只 ｜ 单期换手：10 只

回测记录（模型 mdl_cn_train_20260906023306_57ce74a7_d5a3faa7，2024-01-02 → 2024-12-31，A股费率/T+1/含交易成本）
    年化 76.93% ｜ 夏普 2.480 ｜ 最大回撤 -18.93% ｜ 基准 16.20% ｜ 交易 4032 笔 ｜ 胜率 53.78%
    对照 standard_topk（平台内置，同模型同区间）：年化 59.26% ｜ 夏普 1.918 ｜ 最大回撤 -18.42%
    注：单一区间单一模型的成绩只用于验证链路与相对比较，不等于未来收益。

怎么用
    1. AI-IDE → 策略模板 → 文件夹「A股策略/09_事件与择时」→ 选「A股个股止损」，选好模型直接回测；
    2. 参数面板可调 topk / n_drop / rebalance_days（本模板靠内置类固有行为，无 f_* 过滤）；
    3. 命令行单跑（下面就是复现本文件回测记录的命令）：
       docker exec quantmind python /app/scripts/verify_ashare_backtest.py as42_stop_loss_guard \
         --start 2024-01-02 --end 2024-12-31 --model-id mdl_cn_train_20260906023306_57ce74a7_d5a3faa7
    4. 实盘：策略 ID 用 sys_as42_stop_loss_guard（内置模板在实盘链路里需加 sys_ 前缀）。
    5. 本文件的回测记录由 scripts/run_ashare_backtest_all.py 写入 scripts/ashare_backtest_results.json，
       重跑生成器（scripts/gen_ashare_strategy_templates.py）即刷新；改参数请改生成器，不要手改模板。

由 scripts/gen_ashare_strategy_templates.py 生成；参数说明见同名 .json。
"""

STRATEGY_CONFIG = {
    "class": "RedisStopLossStrategy",
    "module_path": "backend.services.engine.qlib_app.utils.extended_strategies",
    "kwargs": {
        "signal": '<PRED>',
        "only_tradable": True,
        "topk": 30,
        "n_drop": 10,
        "rebalance_days": 3,
        "stop_loss": -0.08,
        "take_profit": 0.15,
    },
}
