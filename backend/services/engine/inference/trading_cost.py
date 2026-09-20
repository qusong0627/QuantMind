"""A 股交易成本模型（回测评估用）。

费率口径与 backend/services/engine/qlib_app/utils/cn_exchange.py 保持一致，
但不继承 qlib.backtest.Exchange —— 回测评估只需要组合层面的成本率，
不需要逐笔撮合。
"""
from __future__ import annotations

from dataclasses import dataclass, replace

# A 股标准费率（2026 年口径）
_DEFAULT_COMMISSION_RATE = 0.00025  # 佣金，买卖各收
_DEFAULT_MIN_COMMISSION = 5.0  # 单笔最低佣金（元）
_DEFAULT_STAMP_DUTY = 0.001  # 印花税，仅卖出
_DEFAULT_TRANSFER_FEE = 0.00001  # 过户费，仅沪市
_DEFAULT_SLIPPAGE = 0.001  # 滑点，买卖各计

# 涨跌停阈值**不在本模块**：唯一事实源 = ``local_market_data`` 的
# ``limit_pct`` / ``compute_limits``（按 symbol + is_st + trade_date 判定）。
#
# 本模块曾持有一张「按 listing_market 中文板名查表」的 _LIMIT_BY_MARKET
# （沪市主板/创业板/北交所…，未命中落 0.10），2026-09-20 已删除。删因是实测：
# 写进取数面的 listing_market 实际取值是 ``SH``/``SZ``/``BJ``/``"None"``
# （model_features_2026.parquet 实测），中文键**一个都命不中** → 全市场一律
# 走 10% 默认线。后果不是「略偏」而是系统性误剔：22,185 行有涨跌幅的样本里，
# 旧表剔 436 行、按 symbol 的权威口径应剔 304 行，多剔 132 行（20% 板 115、
# 北交所 17），被误剔的 |涨幅| 中位数 11.4% —— 剔掉的恰是高动量样本。
#
# 按板名查表这个做法本身也不成立：``SZ`` 同时覆盖深市主板（10%）与创业板
# （20%），标签不足以决定阈值；能决定阈值的只有 symbol。


@dataclass(frozen=True)
class CostModel:
    """A 股双边交易成本。所有费率为小数（0.001 = 0.1%）。"""

    commission_rate: float = _DEFAULT_COMMISSION_RATE
    min_commission: float = _DEFAULT_MIN_COMMISSION
    stamp_duty: float = _DEFAULT_STAMP_DUTY
    transfer_fee: float = _DEFAULT_TRANSFER_FEE
    slippage: float = _DEFAULT_SLIPPAGE

    @classmethod
    def resolve(
        cls,
        meta: dict | None = None,
        override: dict | None = None,
    ) -> CostModel:
        """按 A 股默认 → 模型 metadata.context → 调用方 override 逐级覆盖。"""
        model = cls()

        ctx = (meta or {}).get("context")
        if isinstance(ctx, dict):
            model = model._apply(ctx)

        if override:
            model = model._apply(override)

        return model

    def _apply(self, source: dict) -> CostModel:
        updates: dict[str, float] = {}
        for field in (
            "commission_rate",
            "min_commission",
            "stamp_duty",
            "transfer_fee",
            "slippage",
        ):
            raw = source.get(field)
            if raw is None:
                continue
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            if value < 0:
                continue
            updates[field] = value
        return replace(self, **updates) if updates else self

    def round_trip_cost(self, is_sh: bool = False) -> float:
        """一次完整买入+卖出的成本率。

        min_commission 未计入：组合层面按等权分摊时，只有单标的仓位
        低于约 2 万元才会触及 5 元下限，回测的组合规模假设下可忽略。
        """
        commission = self.commission_rate * 2
        slippage = self.slippage * 2
        transfer = (self.transfer_fee * 2) if is_sh else 0.0
        return commission + slippage + transfer + self.stamp_duty

    def as_dict(self) -> dict[str, float]:
        return {
            "commission_rate": self.commission_rate,
            "min_commission": self.min_commission,
            "stamp_duty": self.stamp_duty,
            "transfer_fee": self.transfer_fee,
            "slippage": self.slippage,
            "round_trip_cost": self.round_trip_cost(),
            "round_trip_cost_sh": self.round_trip_cost(is_sh=True),
        }


