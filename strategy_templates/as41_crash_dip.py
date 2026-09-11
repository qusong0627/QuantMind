# -*- coding: utf-8 -*-
"""A股指数暴跌抄底 (as41_crash_dip)

[A股] 平台内置抄底类：沪深 300 单日暴跌 2.5%（或 100 点）触发后，挑超跌且趋势未破的标的，等其企稳（前一交易日不再大跌或留长下影）于次日开盘买入，持有 5 日，止盈 8% 止损 5%。

文件夹：A股策略/09_事件与择时 ｜ 策略类：RedisCrashBuyDipStrategy ｜ 市场：A股
调仓：每 1 个交易日 ｜ 持仓：5 只

回测记录（模型 mdl_cn_train_20260906023306_57ce74a7_d5a3faa7，2024-01-02 → 2024-12-31，A股费率/T+1/含交易成本）
    年化 21.08% ｜ 夏普 1.278 ｜ 最大回撤 -5.18% ｜ 基准 16.20% ｜ 交易 49 笔 ｜ 胜率 57.14%
    对照 standard_topk（平台内置，同模型同区间）：年化 59.26% ｜ 夏普 1.918 ｜ 最大回撤 -18.42%
    注：单一区间单一模型的成绩只用于验证链路与相对比较，不等于未来收益。

怎么用
    1. AI-IDE → 策略模板 → 文件夹「A股策略/09_事件与择时」→ 选「A股指数暴跌抄底」，选好模型直接回测；
    2. 参数面板可调 topk / rebalance_days（本模板靠内置类固有行为，无 f_* 过滤）；
    3. 命令行单跑（下面就是复现本文件回测记录的命令）：
       docker exec quantmind python /app/scripts/verify_ashare_backtest.py as41_crash_dip \
         --start 2024-01-02 --end 2024-12-31 --model-id mdl_cn_train_20260906023306_57ce74a7_d5a3faa7
    4. 实盘：策略 ID 用 sys_as41_crash_dip（内置模板在实盘链路里需加 sys_ 前缀）。
    5. 本文件的回测记录由 scripts/run_ashare_backtest_all.py 写入 scripts/ashare_backtest_results.json，
       重跑生成器（scripts/gen_ashare_strategy_templates.py）即刷新；改参数请改生成器，不要手改模板。

由 scripts/gen_ashare_strategy_templates.py 生成；参数说明见同名 .json。
"""

STRATEGY_CONFIG = {
    "class": "RedisCrashBuyDipStrategy",
    "module_path": "backend.services.engine.qlib_app.utils.extended_strategies",
    "kwargs": {
        "signal": '<PRED>',
        "top_k": 5,
        "hold_days": 5,
        "take_profit": 0.08,
        "stop_loss": -0.05,
        "crash_threshold_pct": 0.025,
        "crash_threshold_points": 100,
        "benchmark": 'SH000300',
        "max_wait_days": 3,
        "min_oversold_margin": 0.01,
        "trend_window": 20,
        "ma_fast": 5,
        "ma_slow": 20,
        "vol_lookback": 10,
        "rebalance_days": 1,
    },
}
