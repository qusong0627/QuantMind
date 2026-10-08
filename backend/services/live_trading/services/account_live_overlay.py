"""实盘账户概览的盘中实时口径覆盖（tdx_bridge 专用，读侧）。

背景（2026-10-08 用户报障「实况图合计赚两万多，顶部资产概览才 1,287」）：
TDX 桥 ``/api/v1/account/query`` **不回现价** → 落库同步
（``tdx_push_service.sync_account_to_pg``）用 QuantDB 最近收盘价补价——
那是为日终账本设计的收盘口径。后果：节后首日整个交易日，概览卡的
浮动盈亏恒 +1,287.40（09-30 收盘基准），而持仓 tab 走桥实时价（+1,629）。
同一座账户两个浮盈并存，页面看起来「没在动」。

本模块在**读侧**把概览口径对齐到盘中实时：按桥 ``get_market_snapshot``
逐持仓取实时价，重算 市值/总资产/浮盈/今日/累计/收益率，其余字段原样。
快照表与账本（结算/对账/日终）**口径不动**——收盘基准是它们的正确落点。

失败纪律：桥不可达 / 一只价都没取到 → **原样返回**（绝不抛、绝不拿半份
数字重算成四不像）；取得部分价 → 重算并如实标注 ``quote_overlay`` 覆盖度。
"""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timezone
from typing import Any
from collections.abc import Awaitable, Callable

from backend.shared.logging_config import get_logger

logger = get_logger(__name__)

#: 桥实时价进程内缓存秒数。概览前端轮询 ~3s，逐次打桥会把 4 只持仓放大成
#: 桥的秒级负载；10s 缓存下每只每分钟 ~6 次，远低于桥 600/min 限流。
_DEFAULT_PRICE_TTL_S = 10.0

_price_cache: dict[str, tuple[float, float]] = {}
_price_cache_lock = threading.Lock()


def _enabled() -> bool:
    return str(
        os.getenv("QM_ACCOUNT_LIVE_OVERLAY_ENABLED", "true")
    ).strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _price_ttl_s() -> float:
    try:
        ttl = float(
            os.getenv("QM_ACCOUNT_LIVE_PRICE_TTL_S", "") or _DEFAULT_PRICE_TTL_S
        )
    except (TypeError, ValueError):
        ttl = _DEFAULT_PRICE_TTL_S
    return ttl if ttl > 0 else _DEFAULT_PRICE_TTL_S


def _f(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _position_code(pos: dict[str, Any]) -> str:
    return str(pos.get("symbol") or pos.get("stock_code") or "").strip()


async def _cached_bridge_quick_prices(codes: list[str]) -> dict[str, float]:
    """桥实时价（逐只 ``get_market_snapshot``），带进程内 TTL 缓存。

    复用 ``live_family._bridge_quick_prices``（持仓 tab 同一条取价链）——两处
    取价语义必须一致，否则概览与持仓条又会差出一次行情。
    """
    now = time.monotonic()
    ttl = _price_ttl_s()
    out: dict[str, float] = {}
    stale: list[str] = []
    with _price_cache_lock:
        for code in codes:
            hit = _price_cache.get(code)
            if hit is not None and now - hit[1] <= ttl:
                out[code] = hit[0]
            else:
                stale.append(code)
    if stale:
        from backend.services.agent_arena.live_family import _bridge_quick_prices

        fresh = await _bridge_quick_prices(stale)
        with _price_cache_lock:
            for code, px in (fresh or {}).items():
                if px and px > 0:
                    _price_cache[code] = (float(px), time.monotonic())
                    out[code] = float(px)
    return out


async def overlay_account_live_prices(
    account_info: dict[str, Any],
    *,
    quote_fetcher: Callable[[list[str]], Awaitable[dict[str, float]]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """tdx_bridge 账户按桥实时价重算；其余源/取价失败原样返回（新 dict，不改入参）。

    ``quote_fetcher`` 仅测试注入用；生产走 ``_cached_bridge_quick_prices``。
    """
    if not _enabled() or not isinstance(account_info, dict):
        return account_info
    if str(account_info.get("account_source") or "") != "tdx_bridge":
        return account_info
    positions = account_info.get("positions")
    if not isinstance(positions, list) or not positions:
        return account_info
    codes = [_position_code(p) for p in positions if isinstance(p, dict)]
    codes = [c for c in codes if c]
    if not codes:
        return account_info

    fetcher = quote_fetcher or _cached_bridge_quick_prices
    try:
        prices = await fetcher(codes)
    except Exception as exc:  # noqa: BLE001 - 取价失败=保持快照口径，绝不抛
        logger.warning("账户实时价覆盖取价失败，保持快照口径: %s", exc)
        return account_info
    if not isinstance(prices, dict) or not prices:
        return account_info

    new_positions: list[Any] = []
    mv_new = 0.0
    floating_new = 0.0
    covered = 0
    uncovered: list[str] = []
    for pos in positions:
        if not isinstance(pos, dict):
            new_positions.append(pos)
            continue
        copy = dict(pos)
        code = _position_code(pos)
        raw_vol = (
            pos.get("volume")
            if pos.get("volume") is not None
            else pos.get("total_volume")
        )
        vol = _f(raw_vol)
        cost = _f(pos.get("cost_price"))
        px_live = _f(prices.get(code))
        if px_live > 0:
            copy["price"] = px_live
            copy["market_value"] = round(px_live * vol, 2)
            price_eff = px_live
            covered += 1
        else:
            if code:
                uncovered.append(code)
            price_eff = _f(pos.get("price"))
            if price_eff <= 0 and vol > 0:
                mv = _f(pos.get("market_value"))
                price_eff = mv / vol if mv > 0 else 0.0
        mv_new += price_eff * vol
        if price_eff > 0 and cost > 0:
            floating_new += (price_eff - cost) * vol
        new_positions.append(copy)
    if covered <= 0:
        return account_info

    floating_new = round(floating_new, 2)
    mv_old = _f(account_info.get("market_value"))
    ta_old = _f(account_info.get("total_asset"))
    ta_new = ta_old + (mv_new - mv_old)
    delta = ta_new - ta_old

    baseline = account_info.get("baseline")
    baseline = baseline if isinstance(baseline, dict) else {}
    initial = _f(account_info.get("initial_equity")) or _f(
        baseline.get("initial_equity")
    )
    day_open = _f(account_info.get("day_open_equity")) or _f(
        baseline.get("day_open_equity")
    )
    month_open = _f(account_info.get("month_open_equity")) or _f(
        baseline.get("month_open_equity")
    )

    daily = (
        (ta_new - day_open)
        if day_open > 0
        else _f(account_info.get("today_pnl")) + delta
    )
    monthly = (
        (ta_new - month_open)
        if month_open > 0
        else _f(account_info.get("monthly_pnl")) + delta
    )
    cumulative = (
        (ta_new - initial)
        if initial > 0
        else _f(account_info.get("cumulative_pnl")) + delta
    )
    realized = round(cumulative - floating_new, 2)
    daily_pct = (
        (daily / day_open * 100.0)
        if day_open > 0
        else _f(account_info.get("daily_return_pct"))
    )
    total_pct = (
        (cumulative / initial * 100.0)
        if initial > 0
        else _f(account_info.get("total_return_pct"))
    )

    overlay = {
        "source": "tdx_bridge_live",
        "covered": covered,
        "total": len(codes),
        "as_of": (now or datetime.now(timezone.utc)).isoformat(),
        "ttl_s": _price_ttl_s(),
    }
    if uncovered:
        overlay["uncovered"] = uncovered

    updated = dict(account_info)
    updated.update(
        {
            "positions": new_positions,
            "position_count": len(new_positions),
            "market_value": mv_new,
            "total_asset": ta_new,
            "floating_pnl": floating_new,
            "floating_pnl_raw": floating_new,
            "realized_pnl": realized,
            "cumulative_pnl": cumulative,
            "total_pnl": cumulative,
            "total_pnl_raw": cumulative,
            "today_pnl": daily,
            "daily_pnl": daily,
            "monthly_pnl": monthly,
            "daily_return": daily_pct,
            "daily_return_pct": daily_pct,
            "daily_return_ratio": daily_pct / 100.0,
            "total_return": total_pct,
            "total_return_pct": total_pct,
            "total_return_ratio": total_pct / 100.0,
            "quote_overlay": overlay,
        }
    )
    return updated
