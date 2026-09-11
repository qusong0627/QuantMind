# -*- coding: utf-8 -*-
"""A股低频轮动 (as35_rotation_low_turnover)

[A股] 15 日调仓、50 只持仓、单行业上限 25% 的低频轮动，适合长期持有与成本敏感账户。

文件夹：A股策略/07_行业与主题轮动 ｜ 策略类：RedisRiskGuardTopkStrategy ｜ 市场：A股
调仓：每 15 个交易日 ｜ 持仓：50 只 ｜ 单期换手：8 只

回测记录（模型 mdl_cn_train_20260906023306_57ce74a7_d5a3faa7，2024-01-02 → 2024-12-31，A股费率/T+1/含交易成本）
    年化 36.02% ｜ 夏普 1.688 ｜ 最大回撤 -11.24% ｜ 基准 16.20% ｜ 交易 290 笔 ｜ 胜率 55.51%
    对照 standard_topk（平台内置，同模型同区间）：年化 59.26% ｜ 夏普 1.918 ｜ 最大回撤 -18.42%
    注：单一区间单一模型的成绩只用于验证链路与相对比较，不等于未来收益。

怎么用
    1. AI-IDE → 策略模板 → 文件夹「A股策略/07_行业与主题轮动」→ 选「A股低频轮动」，选好模型直接回测；
    2. 参数面板可调 topk / n_drop / rebalance_days；f_* 是 A 股硬约束（基本面/流动性），建议保留；
    3. 命令行单跑（下面就是复现本文件回测记录的命令）：
       docker exec quantmind python /app/scripts/verify_ashare_backtest.py as35_rotation_low_turnover \
         --start 2024-01-02 --end 2024-12-31 --model-id mdl_cn_train_20260906023306_57ce74a7_d5a3faa7
    4. 实盘：策略 ID 用 sys_as35_rotation_low_turnover（内置模板在实盘链路里需加 sys_ 前缀）。
    5. 本文件的回测记录由 scripts/run_ashare_backtest_all.py 写入 scripts/ashare_backtest_results.json，
       重跑生成器（scripts/gen_ashare_strategy_templates.py）即刷新；改参数请改生成器，不要手改模板。

由 scripts/gen_ashare_strategy_templates.py 生成；参数说明见同名 .json。
"""

STRATEGY_CONFIG = {
    "class": "RedisRiskGuardTopkStrategy",
    "module_path": "backend.services.engine.qlib_app.utils.extended_strategies",
    "kwargs": {
        "signal": '<PRED>',
        "only_tradable": True,
        "topk": 50,
        "n_drop": 8,
        "rebalance_days": 15,
        "industry_cap_ratio": 0.25,
        "market_state_window": 20,
        "f_total_mv_min": 5000000000.0,
        "f_amount_ma_5_min": 5000,
        "f_vol_std_20_max": 4.0,
    },
}
