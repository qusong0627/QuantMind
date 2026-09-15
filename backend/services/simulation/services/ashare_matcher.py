"""A股模拟撮合器 —— 涨跌停、整手、费用、滑点。

由 SimulationExecutionEngine 调用，不把规则堆在原类里。
所有价格均为不复权实际价格（与 LocalMarketData 的 DailyBar 口径一致）。
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import date

from backend.services.simulation.services.local_market_data import DailyBar
from backend.services.simulation.services.market_rules import lot_size_for_symbol

logger = logging.getLogger(__name__)

# ── 费用常量 ──────────────────────────────────────────────────────────
_COMMISSION_RATE = 0.0003  # 佣金费率（双向）
_COMMISSION_MIN = 5.0  # 佣金最低 5 元
_STAMP_DUTY_RATE = 0.0005  # 印花税（仅卖出，2023-08-28 降为 0.05%）
_TRANSFER_FEE_RATE = 0.00001  # 过户费（沪深双向，0.001%）
_LOT_SIZE = 100  # A股 1 手 = 100 股


@dataclass(frozen=True)
class MatchConfig:
    """撮合参数（可由前端 SimulationSettings 编辑）。"""

    price_mode: str = "close"  # open / close / vwap
    slippage_bps: float = 5.0  # 滑点（基点）
    commission_rate: float = _COMMISSION_RATE
    commission_min: float = _COMMISSION_MIN
    stamp_duty_rate: float = _STAMP_DUTY_RATE
    transfer_fee_rate: float = _TRANSFER_FEE_RATE
    lot_size: int = _LOT_SIZE
    # T-P2-03 取价契约：外部解析价（托管路径经执行引擎取价链解析后喂入；
    # 设置时优先于 bar 的 price_mode 取价——滑点/涨跌停钳制逻辑不变）
    external_price: float | None = None


@dataclass
class MatchResult:
    """撮合结果。"""

    success: bool
    fill_price: float = 0.0
    fill_quantity: int = 0
    commission: float = 0.0
    stamp_duty: float = 0.0
    transfer_fee: float = 0.0
    total_fee: float = 0.0
    reason: str = ""


def _pick_price(bar: DailyBar, mode: str, external_price: float | None = None) -> float:
    # T-P2-03：外部解析价优先（经执行引擎取价链）；否则按 bar 的 price_mode
    if external_price is not None and float(external_price) > 0:
        return float(external_price)
    if mode == "vwap" and bar.vwap > 0:
        return bar.vwap
    if mode == "open" and bar.open > 0:
        return bar.open
    return bar.close


def _floor_to_lot(shares: float, lot_size: int) -> int:
    if shares <= 0:
        return 0
    return int(shares // lot_size) * lot_size


def compute_fees(
    quantity: int,
    price: float,
    side: str,
    cfg: MatchConfig,
) -> tuple[float, float, float, float]:
    """计算 A 股三项费用。返回 (commission, stamp_duty, transfer_fee, total_fee)。"""
    gross = quantity * price
    commission = max(gross * cfg.commission_rate, cfg.commission_min)
    stamp_duty = gross * cfg.stamp_duty_rate if side == "sell" else 0.0
    transfer_fee = gross * cfg.transfer_fee_rate
    total_fee = commission + stamp_duty + transfer_fee
    return commission, stamp_duty, transfer_fee, total_fee


def match_order(
    side: str,
    quantity: int,
    bar: DailyBar,
    cfg: MatchConfig,
    available_volume: float | None = None,
) -> MatchResult:
    """对单笔订单执行 A 股撮合规则。

    Args:
        side: "buy" / "sell"
        quantity: 委托数量（股）
        bar: 当日行情（不复权）
        cfg: 撮合参数
        available_volume: T+1 可卖量（仅 sell 时需要）
    """
    # ── 停牌 ──
    if bar.suspended:
        return MatchResult(success=False, reason="SUSPENDED")

    # ── 涨跌停 ──
    if side == "buy" and bar.close >= bar.limit_up:
        return MatchResult(success=False, reason="LIMIT_UP")
    if side == "sell" and bar.close <= bar.limit_down:
        return MatchResult(success=False, reason="LIMIT_DOWN")

    # ── T+1 可卖量 ──
    if side == "sell" and available_volume is not None:
        if quantity > available_volume:
            return MatchResult(
                success=False,
                reason=f"INSUFFICIENT_AVAILABLE_VOLUME:{available_volume:.0f}",
            )

    # ── 整手 ──
    lot_size = max(1, int(lot_size_for_symbol(bar.symbol) or cfg.lot_size or _LOT_SIZE))
    if side == "buy":
        fill_qty = _floor_to_lot(quantity, lot_size)
        if fill_qty <= 0:
            return MatchResult(success=False, reason="BELOW_LOT_SIZE")
    else:
        # 卖出允许清仓零头（不满一手也可以卖完）
        fill_qty = quantity

    # ── 成交价 + 滑点 ──
    base_price = _pick_price(bar, cfg.price_mode, cfg.external_price)
    if base_price <= 0:
        return MatchResult(success=False, reason="INVALID_PRICE")

    slippage = cfg.slippage_bps / 10000
    direction = 1 if side == "buy" else -1
    fill_price = round(base_price * (1 + direction * slippage), 4)

    # 涨跌停价格钳制
    if math.isfinite(bar.limit_up) and fill_price > bar.limit_up:
        fill_price = bar.limit_up
    if bar.limit_down > 0 and fill_price < bar.limit_down:
        fill_price = bar.limit_down

    # ── 费用 ──
    commission, stamp_duty, transfer_fee, total_fee = compute_fees(
        fill_qty, fill_price, side, cfg
    )

    return MatchResult(
        success=True,
        fill_price=fill_price,
        fill_quantity=fill_qty,
        commission=commission,
        stamp_duty=stamp_duty,
        transfer_fee=transfer_fee,
        total_fee=total_fee,
    )
