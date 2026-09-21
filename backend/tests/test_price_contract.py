"""T-P2-03 测试：取价契约（唯一实现 _resolve_fill_price + matcher external_price）。

背景：托管路径（execute_from_bar）此前完全不走取价链、永远按 bar（盘中=昨收）
成交且谎报来源 local_close；本契约统一两路径——实时链优先、降级如实标注、
陈旧价守卫单实现（strict=手动即时单保持 P0-5 语义）。
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.services.simulation.models.order import OrderType
from backend.services.simulation.services.ashare_matcher import _pick_price
from backend.services.simulation.services.execution_engine import (
    ResolvedFill,
    SimulationExecutionEngine,
)

_BACKEND = Path(__file__).resolve().parents[1]


# --- matcher external_price ------------------------------------------------


def _bar(close=10.0, open_=9.5, vwap=9.8, trade_date=None):  # fidelity: allow-limit-threshold — 非阈值：日线夹具的 open/vwap
    import datetime as _dt

    return SimpleNamespace(
        close=close,
        open=open_,
        vwap=vwap,
        trade_date=trade_date or _dt.date(2026, 9, 15),
    )


def test_pick_price_external_override():
    bar = _bar(close=10.0)
    assert _pick_price(bar, "close") == 10.0
    assert _pick_price(bar, "close", 12.34) == 12.34
    # 无效外部价（0/负）回退 bar
    assert _pick_price(bar, "close", 0.0) == 10.0
    assert _pick_price(bar, "close", None) == 10.0


# --- resolver 六态 ----------------------------------------------------------


def _engine_with_snapshot(price: float, source: str) -> SimulationExecutionEngine:
    eng = SimulationExecutionEngine.__new__(SimulationExecutionEngine)  # 不走 __init__

    async def _fake_latest_price(symbol, *, user_id=None, tenant_id=None):
        return SimpleNamespace(price=price, price_source=source)

    eng._latest_price = _fake_latest_price  # type: ignore[method-assign]
    return eng


def _order(order_type=OrderType.MARKET, symbol="600036.SH"):
    return SimpleNamespace(
        symbol=symbol, user_id=1, tenant_id="default", order_type=order_type, order_id=None
    )


@pytest.mark.asyncio
async def test_fresh_realtime_source_used():
    eng = _engine_with_snapshot(11.11, "redis_series")
    r = await eng._resolve_fill_price(_order(), _bar(), strict_market=False)
    assert r.ok and r.price == 11.11 and r.source == "redis_series" and not r.degraded


@pytest.mark.asyncio
async def test_strict_market_rejects_stale():
    eng = _engine_with_snapshot(10.0, "db_fallback")
    r = await eng._resolve_fill_price(_order(), _bar(), strict_market=True)
    assert not r.ok and "非实时行情" in r.message


@pytest.mark.asyncio
async def test_strict_market_rejects_unavailable():
    eng = _engine_with_snapshot(0.0, "unavailable")
    r = await eng._resolve_fill_price(_order(), _bar(), strict_market=True)
    assert not r.ok and "无法获取" in r.message


@pytest.mark.asyncio
async def test_strict_limit_degrades_to_snapshot():
    eng = _engine_with_snapshot(9.99, "local_daily_close")
    r = await eng._resolve_fill_price(_order(OrderType.LIMIT), None, strict_market=True)
    assert r.ok and r.price == 9.99 and r.degraded and r.source == "local_daily_close"


@pytest.mark.asyncio
async def test_hosted_stale_falls_back_to_prev_close_bar():
    import datetime as _dt

    eng = _engine_with_snapshot(10.0, "db_fallback")
    bar = _bar(close=9.87, trade_date=_dt.date(2026, 9, 15))  # 非今日=昨收
    r = await eng._resolve_fill_price(_order(), bar, strict_market=False)
    assert r.ok and r.price == 9.87 and r.source == "prev_close_bar" and r.degraded


@pytest.mark.asyncio
async def test_hosted_today_bar_labeled_fresh():
    import datetime as _dt

    eng = _engine_with_snapshot(0.0, "unavailable")
    bar = _bar(close=9.9, trade_date=_dt.datetime.now().date())  # fidelity: allow-limit-threshold — 非阈值：日线夹具的收盘价
    r = await eng._resolve_fill_price(_order(), bar, strict_market=False)
    assert r.ok and r.source == "today_bar_close"


@pytest.mark.asyncio
async def test_all_missing_rejects():
    eng = _engine_with_snapshot(0.0, "unavailable")
    r = await eng._resolve_fill_price(_order(), None, strict_market=False)
    assert not r.ok and "无法获取" in r.message


# --- 两路径接线源断言 --------------------------------------------------------


def test_both_paths_wired_to_resolver():
    src = (_BACKEND / "services/simulation/services/execution_engine.py").read_text(
        encoding="utf-8"
    )
    # 托管路径：非 strict（允许降级，但如实标注）
    assert "_resolve_fill_price(order, bar, strict_market=False)" in src
    assert "external_price=resolved.price" in src
    assert "price_source=resolved.source" in src
    # 手动即时路径：strict（保持 P0-5 语义；strict_market 参数化于 T-P2-01；
    # snapshot 透传为上游合入的执行增强参数）
    assert "strict_market=strict_market" in src
    # bar 由上游传入（2026-09-21 接回），不再硬编码 None。
    #
    # 此断言原先钉的是 `order, None, ...` —— 即「即时链永远拿不到 bar」。那正是缺陷
    # 本身：托管引擎从不传 bar → `resolved_strict()` 恒为 True → `_submit_from_bar` 与
    # `execute_from_bar` 成生产死支路，`resolved_strict` 注释里的「托管允许如实降级」
    # 描述的是一个不存在的行为。断言把 bug 当契约钉住了，故随之更正。
    #
    # **P0-5 语义不变**：手动单没有任何调用方传 bar（恒为 None），strict_market 仍为
    # True，取价结果与改动前逐位一致。变化只对「传了 bar 且 strict=False」的托管路径
    # 生效——而那正是设计意图。反向钉子见
    # `test_after_hours_fixed_execution_gate.py::test_hosted_engine_threads_bar_into_order_request`。
    assert (
        "_resolve_fill_price(\n            order, bar, strict_market=strict_market"
        in src
    )
    assert (
        "_resolve_fill_price(\n            order, None, strict_market=strict_market"
        not in src
    )
    # 不再有旧的双份守卫/谎报来源
    assert 'price_source=f"local_{cfg.price_mode}"' not in src
    assert "RULE:PRICE-STALE" in src


def test_resolved_fill_shape():
    r = ResolvedFill(ok=True, price=1.0, source="prev_close_bar", degraded=True)
    assert r.ok and r.degraded and r.message == ""
