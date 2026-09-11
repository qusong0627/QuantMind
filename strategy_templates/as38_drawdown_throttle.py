# -*- coding: utf-8 -*-
"""A股回撤阶梯降仓 (as38_drawdown_throttle)

[A股] 自定义类：按沪深 300 距 250 日高点的回撤分四档降仓，回撤越深仓位越低。

文件夹：A股策略/08_风险控制与仓位 ｜ 策略类：DrawdownThrottleStrategy（模板内自定义，继承 RedisRecordingStrategy） ｜ 市场：A股
调仓：每 5 个交易日 ｜ 持仓：40 只 ｜ 单期换手：10 只

回测记录（模型 mdl_cn_train_20260906023306_57ce74a7_d5a3faa7，2024-01-02 → 2024-12-31，A股费率/T+1/含交易成本）
    年化 28.78% ｜ 夏普 1.358 ｜ 最大回撤 -13.64% ｜ 基准 16.20% ｜ 交易 972 笔 ｜ 胜率 54.85%
    对照 standard_topk（平台内置，同模型同区间）：年化 59.26% ｜ 夏普 1.918 ｜ 最大回撤 -18.42%
    注：单一区间单一模型的成绩只用于验证链路与相对比较，不等于未来收益。

怎么用
    1. AI-IDE → 策略模板 → 文件夹「A股策略/08_风险控制与仓位」→ 选「A股回撤阶梯降仓」，选好模型直接回测；
    2. 参数面板可调 topk / n_drop / rebalance_days；f_* 是 A 股硬约束（基本面/流动性），建议保留；
    3. 命令行单跑（下面就是复现本文件回测记录的命令）：
       docker exec quantmind python /app/scripts/verify_ashare_backtest.py as38_drawdown_throttle \
         --start 2024-01-02 --end 2024-12-31 --model-id mdl_cn_train_20260906023306_57ce74a7_d5a3faa7
    4. 实盘：策略 ID 用 sys_as38_drawdown_throttle（内置模板在实盘链路里需加 sys_ 前缀）。
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



class DrawdownThrottleStrategy(RedisRecordingStrategy):
    """指数回撤阶梯降仓：按沪深 300 距 250 日高点的回撤分档降仓位。

    A 股逻辑：单边下跌时模型信号会持续给出"便宜"的标的，但趋势性下跌里
    越买越亏。用指数回撤做硬闸门，回撤越深仓位越低，保住本金等右侧。
    覆写 ``_dynamic_risk_degree``：基类会在下单前把 self.risk_degree 换成这里的返回值。
    """

    def __init__(self, *args, **kwargs):
        self.regime_symbol = str(kwargs.pop("regime_symbol", "SH000300"))
        self.dd_lookback = int(kwargs.pop("dd_lookback", 250))
        self.dd_l1 = float(kwargs.pop("dd_l1", 0.05))
        self.dd_l2 = float(kwargs.pop("dd_l2", 0.10))
        self.dd_l3 = float(kwargs.pop("dd_l3", 0.15))
        self.pos_l1 = float(kwargs.pop("pos_l1", 1.0))
        self.pos_l2 = float(kwargs.pop("pos_l2", 0.8))
        self.pos_l3 = float(kwargs.pop("pos_l3", 0.6))
        self.pos_l4 = float(kwargs.pop("pos_l4", 0.4))
        super().__init__(*args, **kwargs)

    def _drawdown(self, ref_date):
        span = int(self.dd_lookback * 1.8) + 30
        start = pd.Timestamp(ref_date) - pd.Timedelta(days=span)
        prices = self._close_matrix([self.regime_symbol], start, ref_date)
        if prices is None or prices.empty:
            return None
        series = prices.iloc[:, 0].dropna().iloc[-self.dd_lookback :]
        if series.empty:
            return None
        peak = float(series.cummax().iloc[-1])
        last = float(series.iloc[-1])
        if peak <= 0:
            return None
        return last / peak - 1.0

    def _dynamic_risk_degree(self, base, ref_date):
        """按指数距 250 日高点的回撤分档降仓（ref_date = 上一交易日）。"""
        drawdown = self._drawdown(ref_date)
        if drawdown is None:
            return base
        depth = -drawdown
        if depth < self.dd_l1:
            ratio = self.pos_l1
        elif depth < self.dd_l2:
            ratio = self.pos_l2
        elif depth < self.dd_l3:
            ratio = self.pos_l3
        else:
            ratio = self.pos_l4
        return max(0.0, min(1.0, base * ratio))


STRATEGY_CONFIG = {
    "class": "DrawdownThrottleStrategy",
    "kwargs": {
        "signal": '<PRED>',
        "only_tradable": True,
        "topk": 40,
        "n_drop": 10,
        "rebalance_days": 5,
        "dd_lookback": 250,
        "dd_l1": 0.05,
        "dd_l2": 0.1,
        "dd_l3": 0.15,
        "pos_l1": 1.0,
        "pos_l2": 0.8,
        "pos_l3": 0.6,
        "pos_l4": 0.4,
        "f_total_mv_min": 3000000000.0,
    },
}
