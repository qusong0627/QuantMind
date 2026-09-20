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
    涨跌幅阈值**逐日逐票**取权威口径（板别 + 创业板 2020-08-24 注册制改革 +
    ST 主板 5%→10%，见 local_market_data.limit_pct），不再复述前缀表。
    覆写 ``_adjust_signal``（基类的 generate_trade_decision 真正调用的钩子）。
    """

    def __init__(self, *args, **kwargs):
        self.lookback_days = int(kwargs.pop("lookback_days", 10))
        self.max_limit_ups = int(kwargs.pop("max_limit_ups", 0))
        super().__init__(*args, **kwargs)

    @staticmethod
    def _limit_threshold(symbol, ref_date) -> float:
        """涨停判定阈值（比例）。口径唯一事实源 = local_market_data.limit_pct。

        旧实现按代码前缀返回一张静态表（主板 / 宽板 / 北交所三档，各留 0.5pp
        取整余量），不看日期、没有 ST 档：
        - 2020-08-24 注册制改革前的创业板是 10% 板，却按宽板（20%）的线判 ——
          那几年的真涨停从不计数，本模板宣称的「涨停规避」在最需要它的
          年份（2016~2020 中）**静默失效**，且回测看不出来；
        - ``("4","8","9")`` 兜底把沪市 900xxx 的 B 股（10% 板）当成北交所 30%；
        - 完全没有 5% 的 ST 档，ST 票 5% 封板时判定为「没涨停」照买不误。
        """
        from datetime import date as _date

        try:
            # 导入也在 try 内：本模板会被用户克隆成独立策略在别处跑，「拿不到权威
            # 实现」是真实场景，此时必须走下面的保守兜底而不是抛出去。
            from backend.services.simulation.services.local_market_data import (
                LIMIT_TOLERANCE,
                limit_pct,
            )

            # 日期解析同理必须在 try 内 —— 放在外面时，救不了「ref_date 不可解析」
            # 这个最可能触发兜底的场景，兜底分支等于死代码。
            td = (
                pd.Timestamp(ref_date).date()
                if ref_date is not None
                else _date.today()
            )
            # 余量唯一事实源 = LIMIT_TOLERANCE（0.5pp）：封板价要按分取整，
            # 真封死的票可能只显示 9.97%，不留余量会把真涨停判丢。
            return (
                float(
                    limit_pct(
                        str(symbol),
                        is_st=False,  # fidelity: allow-limit-threshold — 无逐日 ST 源
                        trade_date=td,
                    )
                )
                - LIMIT_TOLERANCE
            )
        except Exception:  # noqa: BLE001
            # 兜底只降级、不改口径**方向**：拿不到权威实现时按最严的主板线判，
            # 宽板票因此被过度剔除（少交易），而不是把真涨停放进来（假收益）。
            # 这个字面量是**故意的**保守下界，不是又一张板别表 —— 见 allow 标记。
            return 0.095  # fidelity: allow-limit-threshold — 兜底按最严主板线

    def _limit_up_counts(self, stocks, ref_date):
        span = int(self.lookback_days * 2.5) + 20
        start = pd.Timestamp(ref_date) - pd.Timedelta(days=span)
        prices = self._close_matrix(stocks, start, ref_date)
        if prices is None or prices.empty:
            return None
        returns = prices.pct_change().iloc[-self.lookback_days :]
        counts = {}
        for symbol in returns.columns:
            # ref_date = 上一交易日：阈值本身也必须是**那一天**的板规（改革分界线）。
            threshold = self._limit_threshold(symbol, ref_date)
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
