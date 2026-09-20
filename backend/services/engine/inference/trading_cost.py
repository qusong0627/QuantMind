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

# 涨跌幅限制，按 listing_market 区分
_LIMIT_BY_MARKET: dict[str, float] = {
    "沪市主板": 0.10,
    "深市主板": 0.10,
    "创业板": 0.20,
    "科创板": 0.20,
    "北交所": 0.30,
}
_DEFAULT_PRICE_LIMIT = 0.10

# 判定为触及涨跌停的取整容差不再在本模块持有数值：唯一事实源 =
# ``local_market_data.LIMIT_TOLERANCE``（0.5pp），用时在函数内导入。
# 数据源 pctchange 有舍入误差，涨跌停价本身又按分取整；0.5pp 是实测订正过的
# 上界（2026-09-20：0.2pp 会漏判 4/2488 条真封板，0.5pp 漏判 0 条）。


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


def price_limit_for_market(listing_market: object) -> float:
    """按上市板块返回涨跌幅限制。未知板块回退到主板 10%。"""
    return _LIMIT_BY_MARKET.get(str(listing_market or "").strip(), _DEFAULT_PRICE_LIMIT)


def limit_threshold(listing_market: object) -> float:
    """触及涨跌停的判定阈值（含容差）。

    容差唯一事实源 = ``local_market_data.LIMIT_TOLERANCE``。**惰性导入**：该模块
    是行情栈的重模块（实测多花 3.5s，且会拉起 LiteLLM 的远程模型价目表抓取），
    而本函数被 ``data_loader`` 逐行调用 —— 不能在导入期付这个代价。
    ``sys.modules`` 缓存后，每次调用只是一个字典查找。
    """
    from backend.services.simulation.services.local_market_data import LIMIT_TOLERANCE

    return price_limit_for_market(listing_market) - LIMIT_TOLERANCE
