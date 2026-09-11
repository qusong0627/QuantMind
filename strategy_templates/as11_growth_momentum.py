# -*- coding: utf-8 -*-
"""A股成长动量 (as11_growth_momentum)

[A股] 模型分 + 60 日动量融合，持仓 30 只，5 日调仓，捕捉景气行业的趋势主升段。

文件夹：A股策略/03_成长与景气 ｜ 策略类：FilteredMomentumStrategy（模板内自定义，继承 RedisRecordingStrategy） ｜ 市场：A股
调仓：每 5 个交易日 ｜ 持仓：30 只 ｜ 单期换手：10 只

回测记录（模型 mdl_cn_train_20260906023306_57ce74a7_d5a3faa7，2024-01-02 → 2024-12-31，A股费率/T+1/含交易成本）
    年化 50.01% ｜ 夏普 2.242 ｜ 最大回撤 -12.09% ｜ 基准 16.20% ｜ 交易 970 笔 ｜ 胜率 52.74%
    对照 standard_topk（平台内置，同模型同区间）：年化 59.26% ｜ 夏普 1.918 ｜ 最大回撤 -18.42%
    注：单一区间单一模型的成绩只用于验证链路与相对比较，不等于未来收益。

怎么用
    1. AI-IDE → 策略模板 → 文件夹「A股策略/03_成长与景气」→ 选「A股成长动量」，选好模型直接回测；
    2. 参数面板可调 topk / n_drop / rebalance_days；f_* 是 A 股硬约束（基本面/流动性），建议保留；
    3. 命令行单跑（下面就是复现本文件回测记录的命令）：
       docker exec quantmind python /app/scripts/verify_ashare_backtest.py as11_growth_momentum \
         --start 2024-01-02 --end 2024-12-31 --model-id mdl_cn_train_20260906023306_57ce74a7_d5a3faa7
    4. 实盘：策略 ID 用 sys_as11_growth_momentum（内置模板在实盘链路里需加 sys_ 前缀）。
    5. 本文件的回测记录由 scripts/run_ashare_backtest_all.py 写入 scripts/ashare_backtest_results.json，
       重跑生成器（scripts/gen_ashare_strategy_templates.py）即刷新；改参数请改生成器，不要手改模板。

由 scripts/gen_ashare_strategy_templates.py 生成；参数说明见同名 .json。
"""

import pandas as pd

from backend.services.engine.qlib_app.utils.recording_strategy import RedisRecordingStrategy

# 自定义策略类：由 CustomStrategyBuilder 从动态模块自动补全 module_path（不要手写 module_path）。
# 继承 RedisRecordingStrategy → 完整保留 f_* 基本面过滤、Redis 记录、TopK-Dropout 低换手与动态风控。
# 取行情一律走基类的 _close_matrix() / _price_frame()（首次取满回测区间并缓存，之后毫秒级切片）；
# 直接调 D.features 会在每个调仓步重复拉全市场，把一年回测拖到几十分钟。



class FilteredMomentumStrategy(RedisRecordingStrategy):
    """模型分 + 动量融合，同时保留 f_* 基本面硬过滤。

    A 股逻辑：平台内置的 RedisMomentumStrategy 走 RedisTopkStrategy 链路，
    没有 FundamentalFilterMixin，f_* 参数会被静默丢弃。本类继承 RedisRecordingStrategy，
    先融合动量、再走基本面过滤与 TopK-Dropout，做到「动量增强 + A 股硬约束」。
    融合方式与内置实现一致：score + momentum_weight * zscore(动量).clip(-1, 1)。

    注意覆写的是 ``_adjust_signal`` 而不是 ``generate_target_weight_position``：
    qlib 的 TopkDropoutStrategy 自己读 self.signal 选股、从不调用后者，
    只有前者会被基类的 generate_trade_decision 真正调用（ref_date = T-1，无前视）。
    """

    def __init__(self, *args, **kwargs):
        self.momentum_period = int(kwargs.pop("momentum_period", 20))
        self.momentum_weight = float(kwargs.pop("momentum_weight", 0.5))
        super().__init__(*args, **kwargs)

    def _momentum_factor(self, stocks, ref_date):
        span = int(self.momentum_period * 2.5) + 30
        start = pd.Timestamp(ref_date) - pd.Timedelta(days=span)
        prices = self._close_matrix(stocks, start, ref_date)
        if prices is None or len(prices) <= self.momentum_period:
            return None
        window = prices.iloc[-self.momentum_period :]
        momentum = window.iloc[-1] / window.iloc[0] - 1.0
        std = momentum.std(ddof=1)
        if std is None or float(std) != float(std) or float(std) == 0:
            return None
        return ((momentum - momentum.mean()) / std).clip(-1, 1)

    def _adjust_signal(self, score, ref_date):
        """把动量因子叠加到模型分上（ref_date = 上一交易日，无前视）。

        模型分必须先做截面标准化：pred 的量纲随模型而变（本批模型 per-date std≈0.0035），
        直接加 [-1,1] 的动量因子等于把排名完全交给动量（实测 as11 年化从 55% 掉到 10%）。
        标准化后 momentum_weight 才真正是「动量的相对权重」。
        """
        if isinstance(score, pd.DataFrame):
            score = score.iloc[:, 0]
        factor = self._momentum_factor(list(score.index), ref_date)
        if factor is None:
            return score
        std = score.std(ddof=1)
        if std is None or float(std) != float(std) or float(std) == 0:
            return score
        base = (score - score.mean()) / std
        return base.add(factor.reindex(score.index).fillna(0.0) * self.momentum_weight)


STRATEGY_CONFIG = {
    "class": "FilteredMomentumStrategy",
    "kwargs": {
        "signal": '<PRED>',
        "only_tradable": True,
        "topk": 30,
        "n_drop": 10,
        "rebalance_days": 5,
        "momentum_period": 60,
        "momentum_weight": 0.4,
        "f_total_mv_min": 3000000000.0,
        "f_amount_ma_5_min": 5000,
    },
}
