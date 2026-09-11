# -*- coding: utf-8 -*-
"""A股趋势闸门 (as18_trend_gate)

[A股] 自定义类：只在收盘价站上长期均线且均线向上时买入，被挡掉的名额让给次优候选。

文件夹：A股策略/04_动量与趋势 ｜ 策略类：TrendGateTopkStrategy（模板内自定义，继承 RedisRecordingStrategy） ｜ 市场：A股
调仓：每 5 个交易日 ｜ 持仓：40 只 ｜ 单期换手：10 只

回测记录（模型 mdl_cn_train_20260906023306_57ce74a7_d5a3faa7，2024-01-02 → 2024-12-31，A股费率/T+1/含交易成本）
    年化 28.73% ｜ 夏普 1.368 ｜ 最大回撤 -10.33% ｜ 基准 16.20% ｜ 交易 974 笔 ｜ 胜率 55.70%
    对照 standard_topk（平台内置，同模型同区间）：年化 59.26% ｜ 夏普 1.918 ｜ 最大回撤 -18.42%
    注：单一区间单一模型的成绩只用于验证链路与相对比较，不等于未来收益。

怎么用
    1. AI-IDE → 策略模板 → 文件夹「A股策略/04_动量与趋势」→ 选「A股趋势闸门」，选好模型直接回测；
    2. 参数面板可调 topk / n_drop / rebalance_days；f_* 是 A 股硬约束（基本面/流动性），建议保留；
    3. 命令行单跑（下面就是复现本文件回测记录的命令）：
       docker exec quantmind python /app/scripts/verify_ashare_backtest.py as18_trend_gate \
         --start 2024-01-02 --end 2024-12-31 --model-id mdl_cn_train_20260906023306_57ce74a7_d5a3faa7
    4. 实盘：策略 ID 用 sys_as18_trend_gate（内置模板在实盘链路里需加 sys_ 前缀）。
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



class TrendGateTopkStrategy(RedisRecordingStrategy):
    """趋势闸门：只在「收盘价站上长期均线且均线仍向上」的标的中选 TopK。

    A 股逻辑：T+1 下买错方向当天无法纠错，用长期均线做一次事前过滤，
    把仓位留给趋势仍在的标的；被挡掉的标的会把名额让给次优候选，而不是留空。
    覆写 ``_adjust_signal``（基类的 generate_trade_decision 真正调用的钩子）。
    """

    def __init__(self, *args, **kwargs):
        self.trend_ma = int(kwargs.pop("trend_ma", 60))
        self.trend_slope_days = int(kwargs.pop("trend_slope_days", 10))
        super().__init__(*args, **kwargs)

    def _trend_ok(self, stocks, ref_date):
        """返回布尔 Series：价格在均线上方 且 均线较 N 日前抬升。"""
        span = int(self.trend_ma * 2.5) + self.trend_slope_days + 30
        start = pd.Timestamp(ref_date) - pd.Timedelta(days=span)
        prices = self._close_matrix(stocks, start, ref_date)
        if prices is None or prices.empty:
            return None
        min_periods = max(5, self.trend_ma // 2)
        ma = prices.rolling(self.trend_ma, min_periods=min_periods).mean()
        if len(ma) <= self.trend_slope_days:
            return None
        latest = ma.iloc[-1]
        base = ma.iloc[-1 - self.trend_slope_days]
        return (prices.iloc[-1] > latest) & (latest > base)

    def _adjust_signal(self, score, ref_date):
        """只保留「站上长期均线且均线向上」的标的（ref_date = 上一交易日）。"""
        if isinstance(score, pd.DataFrame):
            score = score.iloc[:, 0]
        mask = self._trend_ok(list(score.index), ref_date)
        if mask is None:
            return score
        keep = [s for s in score.index if bool(mask.get(s, False))]
        if not keep:
            # 极端行情下若全部不达标就不启用闸门：空信号会让 TopkDropout 按
            # 「分数全为 NaN」的原始顺序卖出 n_drop 只，等于被动清仓，不是本意。
            return score
        return score.loc[keep]


STRATEGY_CONFIG = {
    "class": "TrendGateTopkStrategy",
    "kwargs": {
        "signal": '<PRED>',
        "only_tradable": True,
        "topk": 40,
        "n_drop": 10,
        "rebalance_days": 5,
        "trend_ma": 60,
        "trend_slope_days": 10,
        "f_total_mv_min": 3000000000.0,
        "f_amount_ma_5_min": 5000,
    },
}
