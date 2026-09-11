# -*- coding: utf-8 -*-
"""A股涨停规避 (as43_limit_up_guard)

[A股] 自定义类：剔除近 10 日内出现过涨停的标的，把名额让给同样高分但可成交的股票。

文件夹：A股策略/09_事件与择时 ｜ 策略类：LimitUpGuardStrategy（模板内自定义，继承 RedisRecordingStrategy） ｜ 市场：A股
调仓：每 3 个交易日 ｜ 持仓：30 只 ｜ 单期换手：10 只

回测记录（模型 mdl_cn_train_20260906023306_57ce74a7_d5a3faa7，2024-01-02 → 2024-12-31，A股费率/T+1/含交易成本）
    年化 69.79% ｜ 夏普 2.500 ｜ 最大回撤 -14.51% ｜ 基准 16.20% ｜ 交易 1488 笔 ｜ 胜率 55.65%
    对照 standard_topk（平台内置，同模型同区间）：年化 59.26% ｜ 夏普 1.918 ｜ 最大回撤 -18.42%
    注：单一区间单一模型的成绩只用于验证链路与相对比较，不等于未来收益。

怎么用
    1. AI-IDE → 策略模板 → 文件夹「A股策略/09_事件与择时」→ 选「A股涨停规避」，选好模型直接回测；
    2. 参数面板可调 topk / n_drop / rebalance_days；f_* 是 A 股硬约束（基本面/流动性），建议保留；
    3. 命令行单跑（下面就是复现本文件回测记录的命令）：
       docker exec quantmind python /app/scripts/verify_ashare_backtest.py as43_limit_up_guard \
         --start 2024-01-02 --end 2024-12-31 --model-id mdl_cn_train_20260906023306_57ce74a7_d5a3faa7
    4. 实盘：策略 ID 用 sys_as43_limit_up_guard（内置模板在实盘链路里需加 sys_ 前缀）。
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



class LimitUpGuardStrategy(RedisRecordingStrategy):
    """涨停规避：剔除近 N 日内出现涨停的标的。

    A 股逻辑：涨停股次日大概率高开、难以按模型目标价成交，且开板后常有回吐。
    与其在涨停板上排队，不如把名额让给同样高分但可成交的标的。
    涨跌幅阈值按板块区分：主板 10%、创业板/科创板 20%、北交所 30%。
    覆写 ``_adjust_signal``（基类的 generate_trade_decision 真正调用的钩子）。
    """

    def __init__(self, *args, **kwargs):
        self.lookback_days = int(kwargs.pop("lookback_days", 10))
        self.max_limit_ups = int(kwargs.pop("max_limit_ups", 0))
        super().__init__(*args, **kwargs)

    @staticmethod
    def _limit_threshold(symbol) -> float:
        code = str(symbol)[-6:]
        if code.startswith(("300", "301", "688", "689")):
            return 0.195
        if code.startswith(("4", "8", "9")):
            return 0.295
        return 0.095

    def _limit_up_counts(self, stocks, ref_date):
        span = int(self.lookback_days * 2.5) + 20
        start = pd.Timestamp(ref_date) - pd.Timedelta(days=span)
        prices = self._close_matrix(stocks, start, ref_date)
        if prices is None or prices.empty:
            return None
        returns = prices.pct_change().iloc[-self.lookback_days :]
        counts = {}
        for symbol in returns.columns:
            threshold = self._limit_threshold(symbol)
            counts[symbol] = int((returns[symbol] >= threshold).sum())
        return counts

    def _adjust_signal(self, score, ref_date):
        """剔除近 N 日出现过涨停的标的（ref_date = 上一交易日，无前视）。"""
        if isinstance(score, pd.DataFrame):
            score = score.iloc[:, 0]
        counts = self._limit_up_counts(list(score.index), ref_date)
        if counts is None:
            return score
        keep = [s for s in score.index if counts.get(s, 0) <= self.max_limit_ups]
        if not keep:
            return score
        return score.loc[keep]


STRATEGY_CONFIG = {
    "class": "LimitUpGuardStrategy",
    "kwargs": {
        "signal": '<PRED>',
        "only_tradable": True,
        "topk": 30,
        "n_drop": 10,
        "rebalance_days": 3,
        "lookback_days": 10,
        "max_limit_ups": 0,
        "f_total_mv_min": 3000000000.0,
        "f_amount_ma_5_min": 5000,
    },
}
