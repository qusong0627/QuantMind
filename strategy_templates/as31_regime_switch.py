# -*- coding: utf-8 -*-
"""A股动量反转状态切换 (as31_regime_switch)

[A股] 自定义类：沪深 300 均线判定市场状态，上行市加动量、下行市加反转，并同步调仓。

文件夹：A股策略/07_行业与主题轮动 ｜ 策略类：RegimeSwitchStrategy（模板内自定义，继承 RedisRecordingStrategy） ｜ 市场：A股
调仓：每 3 个交易日 ｜ 持仓：30 只 ｜ 单期换手：10 只

回测记录（模型 mdl_cn_train_20260906023306_57ce74a7_d5a3faa7，2024-01-02 → 2024-12-31，A股费率/T+1/含交易成本）
    年化 41.64% ｜ 夏普 1.466 ｜ 最大回撤 -16.03% ｜ 基准 16.20% ｜ 交易 1580 笔 ｜ 胜率 52.72%
    对照 standard_topk（平台内置，同模型同区间）：年化 59.26% ｜ 夏普 1.918 ｜ 最大回撤 -18.42%
    注：单一区间单一模型的成绩只用于验证链路与相对比较，不等于未来收益。

怎么用
    1. AI-IDE → 策略模板 → 文件夹「A股策略/07_行业与主题轮动」→ 选「A股动量反转状态切换」，选好模型直接回测；
    2. 参数面板可调 topk / n_drop / rebalance_days；f_* 是 A 股硬约束（基本面/流动性），建议保留；
    3. 命令行单跑（下面就是复现本文件回测记录的命令）：
       docker exec quantmind python /app/scripts/verify_ashare_backtest.py as31_regime_switch \
         --start 2024-01-02 --end 2024-12-31 --model-id mdl_cn_train_20260906023306_57ce74a7_d5a3faa7
    4. 实盘：策略 ID 用 sys_as31_regime_switch（内置模板在实盘链路里需加 sys_ 前缀）。
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



class RegimeSwitchStrategy(RedisRecordingStrategy):
    """市场状态切换：上行市加动量、下行市加反转，并同步调整总仓位。

    A 股逻辑：动量因子在趋势市有效、反转因子在震荡/下跌市有效。
    用沪深 300 的 20/60 日均线关系判定状态，动态选择增强方向，
    避免"一套因子打天下"在不同市场环境下失效。
    同时覆写 ``_adjust_signal``（因子方向）与 ``_dynamic_risk_degree``（仓位）。
    """

    def __init__(self, *args, **kwargs):
        self.regime_symbol = str(kwargs.pop("regime_symbol", "SH000300"))
        self.fast_window = int(kwargs.pop("fast_window", 20))
        self.slow_window = int(kwargs.pop("slow_window", 60))
        self.boost_weight = float(kwargs.pop("boost_weight", 0.5))
        self.momentum_window = int(kwargs.pop("momentum_window", 20))
        self.uptrend_position = float(kwargs.pop("uptrend_position", 1.0))
        self.neutral_position = float(kwargs.pop("neutral_position", 0.8))
        self.downtrend_position = float(kwargs.pop("downtrend_position", 0.5))
        super().__init__(*args, **kwargs)

    def _prices(self, symbols, ref_date, span_days):
        start = pd.Timestamp(ref_date) - pd.Timedelta(days=span_days)
        return self._close_matrix(symbols, start, ref_date)

    def _regime(self, ref_date):
        span = int(self.slow_window * 2.5) + 30
        prices = self._prices([self.regime_symbol], ref_date, span)
        if prices is None or prices.empty:
            return "neutral"
        close = prices.iloc[:, 0].dropna()
        if len(close) < self.slow_window + 1:
            return "neutral"
        fast = close.rolling(self.fast_window, min_periods=max(3, self.fast_window // 2)).mean().iloc[-1]
        slow = close.rolling(self.slow_window, min_periods=max(5, self.slow_window // 2)).mean().iloc[-1]
        last = float(close.iloc[-1])
        if last > fast > slow:
            return "up"
        if last < fast < slow:
            return "down"
        return "neutral"

    def _dynamic_risk_degree(self, base, ref_date):
        """按市场状态缩放仓位（ref_date = 上一交易日，无前视）。"""
        state = self._regime(ref_date)
        ratio = {
            "up": self.uptrend_position,
            "neutral": self.neutral_position,
            "down": self.downtrend_position,
        }[state]
        return max(0.0, min(1.0, base * ratio))

    def _adjust_signal(self, score, ref_date):
        """上行市加动量、下行市加反转（ref_date = 上一交易日，无前视）。"""
        if isinstance(score, pd.DataFrame):
            score = score.iloc[:, 0]
        state = self._regime(ref_date)
        if state == "neutral":
            return score
        span = int(self.momentum_window * 2.5) + 30
        prices = self._prices(list(score.index), ref_date, span)
        if prices is None or len(prices) <= self.momentum_window:
            return score
        window = prices.iloc[-self.momentum_window :]
        momentum = window.iloc[-1] / window.iloc[0] - 1.0
        std = momentum.std(ddof=1)
        if not std or float(std) != float(std) or float(std) <= 0:
            return score
        factor = ((momentum - momentum.mean()) / std).clip(-1, 1).reindex(score.index).fillna(0.0)
        direction = 1.0 if state == "up" else -1.0
        # 模型分先截面标准化，boost_weight 才是「相对模型分」的增强强度
        score_std = score.std(ddof=1)
        if not score_std or float(score_std) != float(score_std) or float(score_std) <= 0:
            return score
        base = (score - score.mean()) / score_std
        return base.add(factor * self.boost_weight * direction)


STRATEGY_CONFIG = {
    "class": "RegimeSwitchStrategy",
    "kwargs": {
        "signal": '<PRED>',
        "only_tradable": True,
        "topk": 30,
        "n_drop": 10,
        "rebalance_days": 3,
        "fast_window": 20,
        "slow_window": 60,
        "boost_weight": 0.5,
        "momentum_window": 20,
        "uptrend_position": 1.0,
        "neutral_position": 0.8,
        "downtrend_position": 0.5,
        "f_total_mv_min": 3000000000.0,
    },
}
