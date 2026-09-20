"""T-P2-07 测试：盘后固定价格交易窗口（2026-07-06 新规）。

口径：15:05–15:30 按收盘价成交、无滑点；涨跌停/停牌/可卖量/申报数量闸门
与连续竞价同向（保守）；会话由墙钟唯一推导（session_for_time），
L0 时段校验复用同一谓词；SIM 保存放行、REAL 显式拒绝（通道待核实）。
"""

from __future__ import annotations

import re
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException

from backend.services.live_trading.routers.real_trading_utils import (
    _default_live_trade_config,
    _normalize_live_trade_config,
)
from backend.services.simulation.services.ashare_matcher import (
    MatchConfig,
    match_order,
)
from backend.services.simulation.services.execution_engine import (
    _resolve_match_session,
)
from backend.services.simulation.services.local_market_data import DailyBar
from backend.services.simulation.services.market_rules import (
    AFTER_HOURS_FIXED_END,
    AFTER_HOURS_FIXED_START,
    SESSION_AFTER_HOURS_FIXED,
    SESSION_CONTINUOUS,
    is_after_hours_fixed_session,
    session_for_time,
)
from backend.services.simulation.services.simulation_hosted_scheduler import (
    _is_enabled_session,
    _normalize_live_trade_config as _normalize_sim_cfg,
    _should_trigger,
)

_BACKEND = Path(__file__).resolve().parents[1]


def _bar(
    close: float = 10.0,
    pre_close: float = 9.5,  # fidelity: allow-limit-threshold — 非阈值：日线夹具的 pre_close
    limit_up: float = 10.45,
    limit_down: float = 8.55,
    suspended: bool = False,
    symbol: str = "600036.SH",
) -> DailyBar:
    return DailyBar(
        symbol=symbol,
        trade_date=date(2026, 9, 16),
        open=9.6,
        high=close * 1.001,
        low=close * 0.999,
        close=close,
        volume=1_000_000,
        amount=10_000_000,
        vwap=close,
        pre_close=pre_close,
        limit_up=limit_up,
        limit_down=limit_down,
        is_st=False,  # fidelity: allow-limit-threshold — 夹具字段：_bar 固定构造非 ST 日线（本文件不测 ST 分支）
        suspended=suspended,
    )


_CFG_CONTINUOUS = MatchConfig()
_CFG_AFTER_HOURS = MatchConfig(session=SESSION_AFTER_HOURS_FIXED)


# ── 时段谓词（L0 唯一口径） ─────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.parametrize(
    ("hour", "minute", "second", "expected"),
    [
        (14, 59, 59, False),
        (15, 4, 59, False),
        (15, 5, 0, True),
        (15, 10, 30, True),
        (15, 30, 0, True),
        (15, 30, 1, False),
    ],
)
def test_is_after_hours_fixed_session_boundaries(hour, minute, second, expected):
    assert (
        is_after_hours_fixed_session(datetime(2026, 9, 16, hour, minute, second))
        is expected
    )


@pytest.mark.unit
def test_is_after_hours_fixed_session_accepts_time_object():
    assert is_after_hours_fixed_session(time(15, 10)) is True
    assert is_after_hours_fixed_session(time(14, 0)) is False


@pytest.mark.unit
def test_session_constants_match_regulation_window():
    assert AFTER_HOURS_FIXED_START.strftime("%H:%M") == "15:05"
    assert AFTER_HOURS_FIXED_END.strftime("%H:%M") == "15:30"


@pytest.mark.unit
def test_session_for_time_maps_wallclock_to_session():
    assert session_for_time(datetime(2026, 9, 16, 15, 10)) == SESSION_AFTER_HOURS_FIXED
    assert session_for_time(datetime(2026, 9, 16, 15, 4, 59)) == SESSION_CONTINUOUS
    assert session_for_time(datetime(2026, 9, 16, 15, 31)) == SESSION_CONTINUOUS
    assert session_for_time(datetime(2026, 9, 16, 10, 0)) == SESSION_CONTINUOUS


# ── 撮合器：盘后固定价格（成交价 = 取价结果、无滑点） ────────────────


@pytest.mark.unit
def test_continuous_session_applies_slippage_buy():
    r = match_order("buy", 100, _bar(), _CFG_CONTINUOUS)
    assert r.success
    assert r.fill_price == round(10.0 * (1 + 5.0 / 10000), 4)  # 10.005


@pytest.mark.unit
def test_after_hours_buy_fills_at_close_without_slippage():
    r = match_order("buy", 100, _bar(), _CFG_AFTER_HOURS)
    assert r.success
    assert r.fill_price == 10.0


@pytest.mark.unit
def test_after_hours_sell_fills_at_close_without_slippage():
    r = match_order("sell", 100, _bar(), _CFG_AFTER_HOURS, available_volume=100)
    assert r.success
    assert r.fill_price == 10.0


@pytest.mark.unit
def test_after_hours_respects_external_price_without_slippage():
    cfg = MatchConfig(session=SESSION_AFTER_HOURS_FIXED, external_price=10.12)
    r = match_order("buy", 100, _bar(), cfg)
    assert r.success
    assert r.fill_price == 10.12


@pytest.mark.unit
def test_after_hours_keeps_limit_up_buy_rejection():
    bar = _bar(close=10.45, limit_up=10.45)
    r = match_order("buy", 100, bar, _CFG_AFTER_HOURS)
    assert not r.success
    assert r.reason == "LIMIT_UP"


@pytest.mark.unit
def test_after_hours_keeps_limit_down_sell_rejection():
    bar = _bar(close=8.55, limit_down=8.55)
    r = match_order("sell", 100, bar, _CFG_AFTER_HOURS, available_volume=100)
    assert not r.success
    assert r.reason == "LIMIT_DOWN"


@pytest.mark.unit
def test_after_hours_suspended_rejected():
    r = match_order("buy", 100, _bar(suspended=True), _CFG_AFTER_HOURS)
    assert not r.success
    assert r.reason == "SUSPENDED"


@pytest.mark.unit
def test_after_hours_keeps_t_plus_1_available_volume_gate():
    r = match_order("sell", 200, _bar(), _CFG_AFTER_HOURS, available_volume=100)
    assert not r.success
    assert r.reason.startswith("INSUFFICIENT_AVAILABLE_VOLUME")


@pytest.mark.unit
def test_after_hours_keeps_star_board_quantity_rules():
    star = _bar(symbol="688001.SH")
    ok = match_order("buy", 201, star, _CFG_AFTER_HOURS)
    assert ok.success and ok.fill_quantity == 201
    bad = match_order("buy", 150, star, _CFG_AFTER_HOURS)
    assert not bad.success
    assert bad.reason == "BELOW_LOT_SIZE"


# ── 执行引擎：墙钟会话推导 + 两路径接线 ─────────────────────────────


@pytest.mark.unit
def test_resolve_match_session_by_wallclock():
    assert _resolve_match_session(datetime(2026, 9, 16, 15, 10)) == (
        SESSION_AFTER_HOURS_FIXED
    )
    assert _resolve_match_session(datetime(2026, 9, 16, 14, 0)) == SESSION_CONTINUOUS
    assert _resolve_match_session(datetime(2026, 9, 16, 9, 40)) == SESSION_CONTINUOUS


@pytest.mark.unit
def test_execute_from_bar_passes_session_to_matcher():
    src = (_BACKEND / "services/simulation/services/execution_engine.py").read_text(
        encoding="utf-8"
    )
    assert "session=_resolve_match_session(market=rules.market.value)" in src


@pytest.mark.unit
def test_execute_order_market_branch_handles_after_hours():
    src = (_BACKEND / "services/simulation/services/execution_engine.py").read_text(
        encoding="utf-8"
    )
    # 盘后分支存在且 MARKET 分支内不再无条件加滑点
    assert "SESSION_AFTER_HOURS_FIXED" in src
    market_block = src.split("if order.order_type == OrderType.MARKET:", 1)[1]
    market_block = market_block.split("elif order.order_type == OrderType.LIMIT:", 1)[0]
    assert "after_hours" in market_block


# ── 调度器：AFTER_HOURS 会话门（模拟托管） ──────────────────────────


@pytest.mark.unit
@pytest.mark.parametrize(
    ("now_hhmm", "expected"),
    [
        ("15:04", False),
        ("15:05", True),
        ("15:10", True),
        ("15:30", True),
        ("15:31", False),
    ],
)
def test_scheduler_after_hours_session_gate(now_hhmm, expected):
    cfg = {"enabled_sessions": ["AFTER_HOURS"]}
    assert _is_enabled_session(now_hhmm, cfg) is expected


@pytest.mark.unit
def test_scheduler_pm_session_unaffected():
    cfg = {"enabled_sessions": ["PM"]}
    assert _is_enabled_session("14:50", cfg) is True
    assert _is_enabled_session("15:10", cfg) is False


@pytest.mark.unit
def test_scheduler_should_trigger_in_after_hours_window():
    cfg = _normalize_sim_cfg(
        {
            "enabled_sessions": ["AFTER_HOURS"],
            "sell_time": "15:05",
            "buy_time": "15:10",
            "schedule_type": "interval",
            "rebalance_days": 1,
            "sell_first": False,
        }
    )
    decision = _should_trigger(
        now=datetime(
            2026, 9, 16, 15, 10, 20, tzinfo=ZoneInfo("Asia/Shanghai")
        ),  # 周三交易日
        live_trade_config=cfg,
        started_day=None,
    )
    assert decision.should_trigger is True
    assert decision.phase == "BUY"
    assert decision.reason == "matched"


@pytest.mark.unit
def test_scheduler_idle_outside_after_hours_window():
    cfg = _normalize_sim_cfg(
        {
            "enabled_sessions": ["AFTER_HOURS"],
            "sell_time": "15:05",
            "buy_time": "15:10",
            "schedule_type": "interval",
            "rebalance_days": 1,
            "sell_first": False,
        }
    )
    decision = _should_trigger(
        now=datetime(2026, 9, 16, 15, 35, tzinfo=ZoneInfo("Asia/Shanghai")),
        live_trade_config=cfg,
        started_day=None,
    )
    assert decision.should_trigger is False
    assert decision.reason == "outside_session"


# ── 配置校验：SIM 放行 / 超窗拒 / REAL 拒（通道待核实） ─────────────


def _after_hours_payload():
    return {
        "schedule_type": "interval",
        "rebalance_days": 3,
        "enabled_sessions": ["AFTER_HOURS"],
        "sell_time": "15:05",
        "buy_time": "15:10",
        "sell_first": True,
        "order_type": "LIMIT",
        "max_price_deviation": 0.02,
        "max_orders_per_cycle": 20,
    }


@pytest.mark.unit
def test_normalize_accepts_after_hours_for_simulation():
    cfg = _normalize_live_trade_config(
        _after_hours_payload(),
        _default_live_trade_config(),
        allow_after_hours=True,
    )
    assert cfg["enabled_sessions"] == ["AFTER_HOURS"]
    assert cfg["buy_time"] == "15:10"


@pytest.mark.unit
def test_normalize_rejects_buy_time_outside_after_hours_window():
    payload = _after_hours_payload()
    payload["buy_time"] = "15:40"
    with pytest.raises(HTTPException):
        _normalize_live_trade_config(
            payload, _default_live_trade_config(), allow_after_hours=True
        )


@pytest.mark.unit
def test_normalize_rejects_after_hours_for_real_mode():
    with pytest.raises(HTTPException) as exc:
        _normalize_live_trade_config(
            _after_hours_payload(),
            _default_live_trade_config(),
            allow_after_hours=False,
        )
    assert "盘后" in str(exc.value.detail)


@pytest.mark.unit
def test_normalize_am_pm_unchanged_with_after_hours_allowed():
    cfg = _normalize_live_trade_config(
        {
            "enabled_sessions": ["PM"],
            "sell_time": "14:30",
            "buy_time": "14:45",
        },
        _default_live_trade_config(),
        allow_after_hours=True,
    )
    assert cfg["enabled_sessions"] == ["PM"]


@pytest.mark.unit
def test_mixed_pm_and_after_hours_sessions_allowed_for_simulation():
    cfg = _normalize_live_trade_config(
        {
            "enabled_sessions": ["PM", "AFTER_HOURS"],
            "sell_time": "14:45",
            "buy_time": "15:10",
        },
        _default_live_trade_config(),
        allow_after_hours=True,
    )
    assert cfg["enabled_sessions"] == ["PM", "AFTER_HOURS"]


@pytest.mark.unit
def test_after_hours_rule_source_has_single_definition():
    """防第二实现（T-P2-07 + T-P3-07）：时段常量只在 market_rules 定义。

    消费链：撮合会话推导经 market_rules；调度会话门与实盘校验经 shared/market_sessions
    的时段表（其 CN AFTER_HOURS 由 test_market_sessions 的同步断言守护）。
    """
    engine_src = (
        _BACKEND / "services/simulation/services/execution_engine.py"
    ).read_text(encoding="utf-8")
    scheduler_src = (
        _BACKEND / "services/simulation/services/simulation_hosted_scheduler.py"
    ).read_text(encoding="utf-8")
    utils_src = (
        _BACKEND / "services/live_trading/routers/real_trading_utils.py"
    ).read_text(encoding="utf-8")
    for src in (engine_src, scheduler_src, utils_src):
        assert not re.search(r"15,\s*5\)|15,\s*30\)", src), "出现盘后时段常量副本"
    assert "market_rules" in engine_src
    assert "market_sessions" in scheduler_src
    assert "market_sessions" in utils_src
