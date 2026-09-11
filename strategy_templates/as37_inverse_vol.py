# -*- coding: utf-8 -*-
"""A股逆波动加权 (as37_inverse_vol)

[A股] 自定义类：选股沿用模型分 TopK，权重按 1/波动率分配并设单票上限，风险更均衡。

文件夹：A股策略/08_风险控制与仓位 ｜ 策略类：InverseVolWeightStrategy（模板内自定义，继承 RedisWeightStrategy） ｜ 市场：A股
调仓：每 5 个交易日 ｜ 持仓：30 只

回测记录（模型 mdl_cn_train_20260906023306_57ce74a7_d5a3faa7，2024-01-02 → 2024-12-31，A股费率/T+1/含交易成本）
    年化 71.15% ｜ 夏普 2.857 ｜ 最大回撤 -13.16% ｜ 基准 16.20% ｜ 交易 2136 笔 ｜ 胜率 53.65%
    对照 standard_topk（平台内置，同模型同区间）：年化 59.26% ｜ 夏普 1.918 ｜ 最大回撤 -18.42%
    注：单一区间单一模型的成绩只用于验证链路与相对比较，不等于未来收益。

怎么用
    1. AI-IDE → 策略模板 → 文件夹「A股策略/08_风险控制与仓位」→ 选「A股逆波动加权」，选好模型直接回测；
    2. 参数面板可调 topk / rebalance_days；f_* 是 A 股硬约束（基本面/流动性），建议保留；
    3. 命令行单跑（下面就是复现本文件回测记录的命令）：
       docker exec quantmind python /app/scripts/verify_ashare_backtest.py as37_inverse_vol \
         --start 2024-01-02 --end 2024-12-31 --model-id mdl_cn_train_20260906023306_57ce74a7_d5a3faa7
    4. 实盘：策略 ID 用 sys_as37_inverse_vol（内置模板在实盘链路里需加 sys_ 前缀）。
    5. 本文件的回测记录由 scripts/run_ashare_backtest_all.py 写入 scripts/ashare_backtest_results.json，
       重跑生成器（scripts/gen_ashare_strategy_templates.py）即刷新；改参数请改生成器，不要手改模板。

由 scripts/gen_ashare_strategy_templates.py 生成；参数说明见同名 .json。
"""

import pandas as pd

from backend.services.engine.qlib_app.utils.recording_strategy import (
    FundamentalFilterMixin,
    RedisWeightStrategy,
)

# 自定义策略类：由 CustomStrategyBuilder 从动态模块自动补全 module_path（不要手写 module_path）。
# 继承 RedisWeightStrategy（WeightStrategyBase 链路）→ 权重分配会被 qlib 真正执行，
# 并保留 Redis 交易记录、涨停/停牌过滤与调仓周期控制；f_* 由本类自己接 FundamentalFilterMixin。
# 取行情一律走基类的 _close_matrix() / _price_frame()（首次取满回测区间并缓存，之后毫秒级切片）；
# 直接调 D.features 会在每个调仓步重复拉全市场，把一年回测拖到几十分钟。



class InverseVolWeightStrategy(FundamentalFilterMixin, RedisWeightStrategy):
    """逆波动率加权：选股仍是模型分 TopK，权重按 1/σ 分配（风险平价近似）。

    A 股逻辑：等权会让高波动小票主导组合风险。逆波动加权在不改变选股的前提下
    压低高波动标的的权重，回撤更平滑；对涨跌停造成的权重漂移也更耐受。

    基类用 RedisWeightStrategy（WeightStrategyBase 链路）而不是 TopkDropout：
    只有前者会真正调用 ``generate_target_weight_position`` 并把返回的权重
    交给下单器，TopkDropout 是按现金等额下单、无法表达单票权重差异。
    f_* 过滤由本类自己接 FundamentalFilterMixin，取上一交易日快照，无前视。
    """

    def __init__(self, *args, **kwargs):
        self.vol_window = int(kwargs.pop("vol_window", 20))
        self.weight_cap = float(kwargs.pop("weight_cap", 0.08))
        # f_* 必须在 super().__init__ 之前 pop，否则会被 strip_unsupported_kwargs 丢掉
        self.init_fundamental_filter(kwargs)
        super().__init__(*args, **kwargs)

    def _prev_trade_date(self):
        """上一交易日；取不到返回 None（按「不调整权重」处理）。"""
        try:
            step = self.trade_calendar.get_trade_step()
            prev, _ = self.trade_calendar.get_step_time(step, shift=1)
            return pd.Timestamp(prev)
        except Exception:
            return None

    def _vol_map(self, stocks, ref_date):
        span = int(self.vol_window * 2.5) + 20
        start = pd.Timestamp(ref_date) - pd.Timedelta(days=span)
        prices = self._close_matrix(stocks, start, ref_date)
        if prices is None or prices.empty:
            return None
        vol = prices.pct_change().iloc[-self.vol_window :].std(ddof=1)
        vol = vol[vol > 0]
        return vol if not vol.empty else None

    def generate_target_weight_position(self, score, current=None, trade_exchange=None, *args, **kwargs):
        ref_date = self._prev_trade_date()
        if ref_date is not None and self.use_fundamental_filter:
            score = self.apply_fundamental_filter(score, ref_date)
        weights = super().generate_target_weight_position(score, current, trade_exchange, *args, **kwargs)
        if not weights or ref_date is None:
            return weights
        vol = self._vol_map(list(weights.keys()), ref_date)
        if vol is None:
            return weights
        inverse = (1.0 / vol.clip(lower=1e-4)).reindex(list(weights.keys())).dropna()
        if inverse.empty:
            return weights
        total = float(sum(weights.values()))
        scaled = inverse / inverse.sum() * total
        if 0 < self.weight_cap < 1.0:
            scaled = scaled.clip(upper=self.weight_cap)
            if scaled.sum() > 0:
                scaled = scaled / scaled.sum() * total
        return {key: float(value) for key, value in scaled.items()}


STRATEGY_CONFIG = {
    "class": "InverseVolWeightStrategy",
    "kwargs": {
        "signal": '<PRED>',
        "topk": 30,
        "rebalance_days": 5,
        "vol_window": 20,
        "weight_cap": 0.08,
        "f_total_mv_min": 3000000000.0,
        "f_amount_ma_5_min": 5000,
    },
}
