"""快照级撮合内核（T-P6-17，F2）：盘口五档深度约束 + 涨跌停排队 + 部分成交（纯函数为主）。

**单位（2026-09-17 实锤）**：TDX 帧（含五档 bid_vol/ask_vol 与 volume）为**手**——
`000090.SZ` 早盘 10:18 帧 `volume=49,745 / bid_vol1=2,629`，当日 QuantDB 收盘
`volume=16,430,867 股` → 帧值 ×100 = 股 ✓。本模块统一换算为**股**后再参与撮合
（QuantDB 日线 volume=股，两者仅在换算后可比）。

**撮合语义（v1）**：

- 买单吃 **ask** 档（价低→高），卖单吃 **bid** 档（价高→低）；逐档消耗，量尽转下一档；
- 限价单：成交价不越过委托价（买：档价 ≤ 委托价才吃；卖：档价 ≥ 委托价）；
- 涨跌停带内钳制：成交价钳制在 [limit_down, limit_up]；**封板排队**——涨停（ask1 无量且
  现价贴涨停）买单不可成交、跌停（bid1 无量且现价贴跌停）卖单不可成交（排队中）；
- 部分成交：返回 (fill_price 加权均价, fill_qty 股取整到整手, unfilled, levels)；
  余量由既有 pending 机制顺延（execution_engine.apply_filled + pending worker），本层不造状态。
- 盘口不完整（<2 档有效）或陈旧 → 调用方回退日频核（DailyCore），**绝不猜深度**。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any
from collections.abc import Mapping

logger = logging.getLogger(__name__)

FRESH_WITHIN_S = float(os.getenv("QM_QUOTE_FRESH_WITHIN_S", "60") or 60)
STALE_WITHIN_S = float(os.getenv("QM_QUOTE_STALE_WITHIN_S", "300") or 300)
UNIT_MULTIPLIER = 100.0  # TDX/桥帧量纲=手 → 股（见模块 docstring 实测）
MIN_VALID_LEVELS = 2  # 少于两档有效 → 不算可用盘口
LOT_SIZE = 100


@dataclass(frozen=True)
class BookLevel:
    price: float
    volume: float  # 股（已乘 UNIT_MULTIPLIER）


@dataclass(frozen=True)
class Book:
    bids: tuple[BookLevel, ...]  # 降序（best first）
    asks: tuple[BookLevel, ...]  # 升序（best first）
    price: float
    pre_close: float
    limit_up: float | None
    limit_down: float | None
    ts: float | None
    source: str
    symbol: str
    completeness: int = 0  # 有效档位数（价>0 且量≥0 计 1）


@dataclass(frozen=True)
class BookFill:
    fill_price: float
    fill_qty: float  # 股，整手
    unfilled: float
    levels_consumed: int
    notes: tuple[str, ...] = field(default_factory=tuple)


def _f(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out:
        return None
    return out


def book_from_snapshot(snap: Mapping[str, Any], *, symbol: str = "", source: str = "snapshot") -> Book | None:
    """行情 Hash（collector/桥 写侧契约字段）→ Book；无有效盘口返回 None。"""

    def _levels(prefix: str, *, reverse: bool) -> tuple[BookLevel, ...]:
        out: list[BookLevel] = []
        for i in range(1, 6):
            price = _f(snap.get(f"{prefix}{i}"))
            vol = _f(snap.get(f"{prefix}_vol{i}"))
            if price is None or price <= 0 or vol is None or vol < 0:
                continue
            out.append(BookLevel(price=price, volume=vol * UNIT_MULTIPLIER))
        out.sort(key=lambda lv: lv.price, reverse=reverse)
        return tuple(out)

    price = _f(snap.get("Now")) or _f(snap.get("price"))
    pre_close = _f(snap.get("PreClose")) or _f(snap.get("pre_close"))
    if price is None or price <= 0:
        return None
    bids = _levels("bid", reverse=True)
    asks = _levels("ask", reverse=False)
    completeness = len(bids) + len(asks)
    if completeness < MIN_VALID_LEVELS:
        return None
    ts = _f(snap.get("timestamp")) or _f(snap.get("ts"))
    if ts and ts > 1e12:
        ts /= 1000.0
    return Book(
        bids=bids,
        asks=asks,
        price=price,
        pre_close=pre_close or 0.0,
        limit_up=_f(snap.get("LimitUp")) or _f(snap.get("limit_up")),
        limit_down=_f(snap.get("LimitDown")) or _f(snap.get("limit_down")),
        ts=ts,
        source=source,
        symbol=symbol or str(snap.get("symbol") or ""),
        completeness=completeness,
    )


def is_fresh(book: Book, *, now: float) -> bool:
    """盘口新鲜度：ts 缺失按不可判定 → 不新鲜（宁可回退日频，不猜）。"""
    if not book.ts:
        return False
    age = now - book.ts
    return -30.0 <= age <= STALE_WITHIN_S


def _limit_band_tolerance(limit_band: float) -> float:
    return max(0.005, abs(limit_band) * 0.005)


def _round_cent(value: float) -> float:
    """四舍五入到分（交易所口径；浮点 round() 的半分不稳不采用）。"""
    from decimal import ROUND_HALF_UP, Decimal

    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def walk_book(
    side: str,
    quantity: float,
    book: Book,
    *,
    order_type: str = "market",
    limit_price: float | None = None,
    lot_size: int = LOT_SIZE,
) -> BookFill | None:
    """盘口穿档撮合。返回 None = 该方向不可成交（封板排队/无有效对手盘）。

    成交价 = 吃掉档位的量加权均价（钳制在涨跌停带内，四舍五入到分）；
    数量 = 整手向下取整；限价单不越过委托价。
    """
    side_l = str(side or "").lower()
    if side_l not in {"buy", "sell"}:
        return None
    qty = float(quantity or 0.0)
    if qty <= 0:
        return None
    levels = book.asks if side_l == "buy" else book.bids
    if not levels:
        return None
    # 封板排队：涨停（买）无卖盘 / 跌停（卖）无买盘 → 排队不可成交
    best = levels[0]
    if side_l == "buy" and book.limit_up and best.price >= book.limit_up - _limit_band_tolerance(book.limit_up):
        # 卖一即涨停价：若卖一有量可成交（封板被砸开），无量则排队
        if best.volume <= 0:
            return None
    if side_l == "sell" and book.limit_down and best.price <= book.limit_down + _limit_band_tolerance(book.limit_down):
        if best.volume <= 0:
            return None

    remaining = qty
    consumed_value = 0.0
    consumed_qty = 0.0
    levels_consumed = 0
    notes: list[str] = []
    for level in levels:
        if remaining <= 0:
            break
        price = level.price
        if order_type == "limit" and limit_price:
            if side_l == "buy" and price > limit_price + 1e-9:
                break
            if side_l == "sell" and price < limit_price - 1e-9:
                break
        if book.limit_up and price > book.limit_up:
            price = book.limit_up
            notes.append("clamp_limit_up")
        if book.limit_down and price < book.limit_down:
            price = book.limit_down
            notes.append("clamp_limit_down")
        take = min(remaining, level.volume)
        if take <= 0:
            continue
        consumed_value += take * price
        consumed_qty += take
        remaining -= take
        levels_consumed += 1

    if consumed_qty <= 0:
        return None
    fill_qty = (int(consumed_qty // max(1, int(lot_size)))) * max(1, int(lot_size))
    if fill_qty <= 0:
        return None
    fill_price = _round_cent(consumed_value / consumed_qty)
    if book.limit_up and fill_price > book.limit_up:
        fill_price = _round_cent(book.limit_up)
    if book.limit_down and fill_price < book.limit_down:
        fill_price = _round_cent(book.limit_down)
    return BookFill(
        fill_price=fill_price,
        fill_qty=float(fill_qty),
        unfilled=max(0.0, qty - fill_qty),
        levels_consumed=levels_consumed,
        notes=tuple(sorted(set(notes))),
    )


def fetch_book(symbol: str, *, now: float | None = None, client: Any | None = None) -> Book | None:
    """读远端行情服标准键（小写前缀优先、大写兼容）→ Book；缺失/陈旧返回 None。"""
    import time

    from backend.shared.remote_quote_config import make_sync_client
    from backend.shared.stock_utils import StockCodeUtil

    suffix = StockCodeUtil.to_suffix(str(symbol or "").strip()) or str(symbol or "")
    if not suffix or "." not in suffix:
        return None
    code, market = suffix.split(".", 1)
    keys = [f"market:snapshot:{market.lower()}{code}", f"market:snapshot:{market.upper()}{code}"]
    own_client = client is None
    rc = client or make_sync_client()
    if rc is None:
        return None
    try:
        snap: dict[str, Any] = {}
        source = "snapshot"
        for key in keys:
            data = rc.hgetall(key) or {}
            if data:
                snap = data
                source = str(data.get("source") or "snapshot")
                break
        if not snap:
            return None
        book = book_from_snapshot(snap, symbol=suffix, source=source)
        if book is None:
            return None
        if not is_fresh(book, now=now if now is not None else time.time()):
            return None
        return book
    except Exception as exc:  # noqa: BLE001 - 取数失败一律回退日频核
        logger.debug("[snapshot_book] %s 读取失败: %s", symbol, exc)
        return None
    finally:
        if own_client:
            try:
                rc.close()
            except Exception:  # noqa: BLE001
                pass
