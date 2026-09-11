# -*- coding: utf-8 -*-
"""A股波动率目标仓位 (as36_vol_target)

[A股] 自定义类：仓位 = min(1, 目标波动/沪深 300 已实现波动)，高波动期自动降仓。

文件夹：A股策略/08_风险控制与仓位 ｜ 策略类：VolTargetPositionStrategy（模板内自定义，继承 RedisRecordingStrategy） ｜ 市场：A股
调仓：每 5 个交易日 ｜ 持仓：40 只 ｜ 单期换手：10 只

回测记录（模型 mdl_cn_train_20260906023306_57ce74a7_d5a3faa7，2024-01-02 → 2024-12-31，A股费率/T+1/含交易成本）
    年化 42.45% ｜ 夏普 1.988 ｜ 最大回撤 -12.12% ｜ 基准 16.20% ｜ 交易 976 笔 ｜ 胜率 54.85%
    对照 standard_topk（平台内置，同模型同区间）：年化 59.26% ｜ 夏普 1.918 ｜ 最大回撤 -18.42%
    注：单一区间单一模型的成绩只用于验证链路与相对比较，不等于未来收益。

怎么用
    1. AI-IDE → 策略模板 → 文件夹「A股策略/08_风险控制与仓位」→ 选「A股波动率目标仓位」，选好模型直接回测；
    2. 参数面板可调 topk / n_drop / rebalance_days；f_* 是 A 股硬约束（基本面/流动性），建议保留；
    3. 命令行单跑（下面就是复现本文件回测记录的命令）：
       docker exec quantmind python /app/scripts/verify_ashare_backtest.py as36_vol_target \
         --start 2024-01-02 --end 2024-12-31 --model-id mdl_cn_train_20260906023306_57ce74a7_d5a3faa7
    4. 实盘：策略 ID 用 sys_as36_vol_target（内置模板在实盘链路里需加 sys_ 前缀）。
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



class VolTargetPositionStrategy(RedisRecordingStrategy):
    """波动率目标仓位：仓位 = min(1, 目标波动 / 指数已实现波动)。

    A 股逻辑：单一仓位做多时，组合波动几乎等于市场波动。
    用沪深 300 的 20 日已实现年化波动做分母，高波动期自动降仓、低波动期满仓，
    比"拍脑袋定仓位"更稳，也避免在急跌段满仓硬扛。
    覆写 ``_dynamic_risk_degree``：基类会在下单前把 self.risk_degree 换成这里的返回值。
    """

    def __init__(self, *args, **kwargs):
        self.target_vol = float(kwargs.pop("target_vol", 0.15))
        self.vol_window = int(kwargs.pop("vol_window", 20))
        self.min_position = float(kwargs.pop("min_position", 0.2))
        self.max_position = float(kwargs.pop("max_position", 1.0))
        self.vol_symbol = str(kwargs.pop("vol_symbol", "SH000300"))
        super().__init__(*args, **kwargs)

    def _realized_vol(self, ref_date):
        span = int(self.vol_window * 2.5) + 20
        start = pd.Timestamp(ref_date) - pd.Timedelta(days=span)
        prices = self._close_matrix([self.vol_symbol], start, ref_date)
        if prices is None or prices.empty:
            return None
        series = prices.iloc[:, 0].dropna()
        returns = series.pct_change().dropna().iloc[-self.vol_window :]
        if len(returns) < 5:
            return None
        return float(returns.std(ddof=1) * (252 ** 0.5))

    def _dynamic_risk_degree(self, base, ref_date):
        """仓位系数 = min(1, 目标波动 / 已实现波动)，ref_date = 上一交易日。"""
        realized = self._realized_vol(ref_date)
        if not realized or realized <= 1e-6:
            return base
        scale = min(1.0, self.target_vol / realized)
        return max(self.min_position, min(self.max_position, base * scale))


STRATEGY_CONFIG = {
    "class": "VolTargetPositionStrategy",
    "kwargs": {
        "signal": '<PRED>',
        "only_tradable": True,
        "topk": 40,
        "n_drop": 10,
        "rebalance_days": 5,
        "target_vol": 0.15,
        "vol_window": 20,
        "min_position": 0.2,
        "max_position": 1.0,
        "f_total_mv_min": 3000000000.0,
        "f_amount_ma_5_min": 5000,
    },
}
