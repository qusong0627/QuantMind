"""盘后固定价格交易时段的**执行闸**口径 —— T-P2-07 漏掉的第四处会话判定。

背景（2026-09-21 实测）：A 股盘后固定价格交易（15:05–15:30 按当日收盘价成交，
2026-07-06 新规）在系统里有三处会话判定，且三处都认：

  1. 调度会话门 `simulation_hosted_scheduler._is_enabled_session`（时段表）→ 认；
  2. 撮合会话推导 `execution_engine._resolve_match_session`（MatchConfig.session）→ 认；
  3. 风控 L0 时段校验（复用 `market_rules` 唯一谓词）→ 认。

唯独**执行闸** `assess_execution_window` 只认上午/下午连续竞价：15:05–15:30 一律判
`can_execute=False` → `"queued for next valid session"`。

后果（实跑证实，2026-09-16 周三）：

    时刻     调度门(AFTER_HOURS 开)   执行闸
    15:10    True                    can_execute=False → 排队到 09-17
    15:30    True                    can_execute=False → 排队到 09-17

托管周期若配置在该时段触发（调度器明确支持、界面可选），调度器按时放行、系统算出
调仓单，执行闸却把**每一单**排队到下一交易日 —— 盘后固定价格制度完全没生效，
撮合器精心实现的 `_CAP_AFTER_HOURS` 根本轮不到。手动单同理：15:10 下单被排队到明天，
而真实市场当时可按收盘价成交。

本文件钉住四件事：
1. 执行闸认盘后固定价格时段（CN）、边界含 15:05 与 15:30；
2. 15:00–15:05 的空档与 15:31 之后仍是排队（不扩大到全天）；
3. 该时段是 **A 股专属**，非 CN 市场不得套用（与 `_resolve_match_session` 同款约束，
   避免港股/美股被误用收盘价固定成交）；
4. `bar` 不再切换执行链 —— 只影响取价。`_submit_from_bar` 是即时链的平行实现且
   **没有会话闸**，让它可路由等于开一个绕开交易时段的口子；bar 的降级语义已由
   `_resolve_fill_price(order, bar, ...)` 单点实现，即时链传入即可。
"""

from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

_SH = ZoneInfo("Asia/Shanghai")
_HK = ZoneInfo("Asia/Hong_Kong")
_NY = ZoneInfo("America/New_York")

# 2026-09-16 是周三（交易日），09-17 是周四 —— 排队目标日可直接断言成具体日期
_TRADING_DAY = date(2026, 9, 16)
_NEXT_TRADING_DAY = date(2026, 9, 17)


def _cn(hhmm: str) -> datetime:
    hour, minute = (int(part) for part in hhmm.split(":"))
    return datetime(2026, 9, 16, hour, minute, tzinfo=_SH)


async def _window(symbol: str, when: datetime):
    """只用到 self 的静态知识，故绕过 __init__ 直接构造（与既有测试同姿势）。"""
    from backend.services.simulation.services.execution_engine import (
        SimulationExecutionEngine,
    )

    engine = SimulationExecutionEngine.__new__(SimulationExecutionEngine)
    return await engine.assess_execution_window(
        SimpleNamespace(symbol=symbol), now=when
    )


# ── 1. 盘后固定价格时段可执行（CN） ──────────────────────────────────


@pytest.mark.unit
@pytest.mark.parametrize("hhmm", ["15:05", "15:10", "15:30"])
@pytest.mark.asyncio
async def test_after_hours_fixed_window_is_executable_for_cn(hhmm):
    decision = await _window("600036.SH", _cn(hhmm))

    assert decision.can_execute is True, (
        f"CN {hhmm} 处于盘后固定价格交易时段（15:05–15:30 按收盘价成交），"
        f"执行闸却判为不可执行：{decision.message}"
    )
    assert decision.target_trade_date == _TRADING_DAY
    assert decision.retryable is False


# ── 2. 边界与空档：不扩大到全天 ──────────────────────────────────────


@pytest.mark.unit
@pytest.mark.parametrize("hhmm", ["15:04", "15:31"])
@pytest.mark.asyncio
async def test_outside_after_hours_fixed_window_still_queues(hhmm):
    """15:00–15:05 的空档与 15:31 之后，A 股确实没有可执行的会话。

    这两点是本修复的**反向钉子**：改会话判定最容易的翻车方式是把窗口开大
    （比如整个下午都放行），这两条会立刻红。
    """
    decision = await _window("600036.SH", _cn(hhmm))

    assert decision.can_execute is False
    assert decision.retryable is True
    assert decision.message == "queued for next valid session"
    assert decision.target_trade_date == _NEXT_TRADING_DAY


@pytest.mark.unit
@pytest.mark.asyncio
async def test_continuous_session_unchanged():
    decision = await _window("600036.SH", _cn("14:00"))

    assert decision.can_execute is True
    assert decision.target_trade_date == _TRADING_DAY


# ── 3. A 股专属：非 CN 市场不得套用 ──────────────────────────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_after_hours_fixed_not_applied_to_non_cn_markets(monkeypatch):
    """盘后固定价格是 A 股专属制度（`_resolve_match_session` 同款约束：不误用收盘价固定成交）。

    判别手法：把共用谓词打成**恒真**，再看两个常规时段已收盘的非 CN 市场。若执行闸
    无条件套用该时段，港股 17:00 会被误判为可执行 —— 这是时间参数化测不出来的
    （15:05–15:30 本来就落在港股下午盘与美股盘中，两者天然为真）。
    """
    from backend.services.simulation.services import market_rules

    monkeypatch.setattr(market_rules, "is_after_hours_fixed_session", lambda _ts: True)

    hk = await _window("0001.HK", datetime(2026, 9, 16, 17, 0, tzinfo=_HK))
    assert hk.can_execute is False, "港股 17:00（收盘后）被误判为可执行"

    us = await _window("AAPL", datetime(2026, 9, 16, 17, 0, tzinfo=_NY))
    assert us.can_execute is False, "美股 17:00（收盘后）被误判为可执行"


@pytest.mark.unit
def test_gate_reuses_shared_predicate_not_inline_time_compare():
    """口径唯一：执行闸必须复用 `market_rules` 的唯一谓词，不自行比较时分。

    该谓词的文档自称是「风控 L0 时段校验 / 撮合会话推导 / 调度会话门**三处共用**」——
    执行闸是第四个消费者。各写各的时间比较正是这次口径分裂的成因。
    """
    from pathlib import Path

    src = (
        Path(__file__).resolve().parents[1]
        / "services/simulation/services/execution_engine.py"
    ).read_text(encoding="utf-8")
    gate_src = src.split("async def assess_execution_window", 1)[1]
    gate_src = gate_src.split("\n    async def ", 1)[0]

    assert "is_after_hours_fixed_session" in gate_src


# ── 4. bar 只影响取价，不切换执行链 ──────────────────────────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_bar_no_longer_switches_execution_chain(monkeypatch):
    """传了 bar 也必须走即时链（会话闸/申报数量/涨跌停/T+1 全在其内）。

    `_submit_from_bar` 是即时链的平行实现，且**不调用 `assess_execution_window`** ——
    让它可路由，等于让托管的每张调仓单在周末/深夜/任意时刻都能按日线 bar 成交。
    取价降级不需要第二条链：`_resolve_fill_price` 早已把 bar 分支单点实现。
    """
    from backend.services.simulation.services import order_router
    from backend.services.trade.services import risk_gate_service

    seen: dict[str, object] = {}

    class _Passed:
        passed = True
        rule_id = None
        reason = ""

    async def _risk_ok(req, db=None, redis=None):  # noqa: ARG001
        return _Passed()

    async def _fake_immediate(db, manager, req):
        seen["immediate"] = req.bar
        return order_router.RouterOutcome(success=True, message="filled")

    async def _fake_from_bar(db, manager, req):
        seen["from_bar"] = req.bar
        return order_router.RouterOutcome(success=True, message="filled")

    monkeypatch.setattr(risk_gate_service, "check_order", _risk_ok)
    monkeypatch.setattr(order_router, "_submit_immediate", _fake_immediate)
    monkeypatch.setattr(order_router, "_submit_from_bar", _fake_from_bar)

    bar = SimpleNamespace(trade_date=_TRADING_DAY, close=10.0)
    outcome = await order_router.submit_order(
        db=object(),
        redis=object(),
        req=order_router.OrderRequest(
            tenant_id="t",
            user_id=7,
            symbol="600036.SH",
            side="buy",
            quantity=100,
            bar=bar,
        ),
    )

    assert outcome.success is True
    assert "from_bar" not in seen, "bar 不该把订单路由到无会话闸的平行链"
    assert seen.get("immediate") is bar, "bar 必须原样送进即时链（影响取价）"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_hosted_engine_threads_bar_into_order_request(monkeypatch):
    """托管引擎把当日 bar 接进 OrderRouter —— 被掐断的那根线接回来。

    断言 `resolved_strict() is False` 是核心：`OrderRequest.resolved_strict` 的注释写
    「即时默认 strict（P0-5）；**托管允许如实降级**」，但引擎从不传 bar，于是
    `resolved_strict()` 恒为 True —— 注释描述的是一个不存在的行为，
    `_submit_from_bar` 也因此成了生产死支路（全仓无一处传 bar=）。
    """
    from backend.services.simulation import engine as engine_module
    from backend.services.simulation.services import order_router

    captured: dict[str, object] = {}

    async def _capture(db, redis, req):  # noqa: ARG001
        captured["req"] = req
        return order_router.RouterOutcome(success=True, price_source="today_bar_close")

    monkeypatch.setattr(order_router, "submit_order", _capture)

    engine = engine_module.SimulationEngine.__new__(engine_module.SimulationEngine)
    engine.redis = object()

    bar = SimpleNamespace(trade_date=_TRADING_DAY, close=10.0)
    order = SimpleNamespace(
        symbol="600036.SH",
        side="buy",
        quantity=100,
        price=None,
        reason="",
        source=None,
    )
    await engine._execute_order(
        db=None,
        exec_engine=None,
        order=order,
        tenant_id="t",
        user_id="7",
        strategy_id="",
        market=None,
        run_id="run-1",
        bar=bar,
    )

    req = captured["req"]
    assert req.bar is bar
    assert req.resolved_strict() is False, "托管路径应允许如实降级，而非按即时单拒单"


@pytest.mark.unit
def test_engine_loads_bars_for_hosted_fallback_and_passes_them():
    """源守卫：引擎在无实时行情回落日线时，把 bars 一路传到 _execute_order。

    这条钉的是「接线」而非行为——行为由上面两条覆盖；此处防的是未来重构时
    有人把 `bar=` 那行删掉而无人察觉（正是它被静默掐断的方式）。
    """
    from pathlib import Path

    src = (
        Path(__file__).resolve().parents[1] / "services/simulation/engine.py"
    ).read_text(encoding="utf-8")

    assert "_bar_for_symbol(bars, order.symbol)" in src
    assert "bar=self._bar_for_symbol(bars, order.symbol)" in src
