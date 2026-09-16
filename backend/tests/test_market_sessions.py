"""T-P3-07 测试：市场时段唯一实现（shared/market_sessions.py）——"缺口"解锁件。

口径（设计决策 2026-09-16）：**live_trade_config 的时间一律为策略市场本地时钟**——
A股"14:45"=北京时间，美股"15:50"=美东时间。本地钟点不随夏令时漂移，
校验/DST 复杂度归零；调度器按市场时区换算"现在"。

覆盖：市场键归一（CN/a_share/A、US/us_stock…）、各市场时段表、时区与交易日历映射、
本地时段表"不跨午夜"不变量、未知市场保守回落 A 股。
"""

from __future__ import annotations

import pytest

from backend.shared.market_sessions import (
    MARKET_SESSIONS,
    market_calendar,
    market_timezone,
    normalize_market_key,
    session_ranges_local,
)


# ── 市场键归一 ──────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("CN", "CN"),
        ("a_share", "CN"),
        ("A", "CN"),
        ("sse", "CN"),
        ("US", "US"),
        ("us_stock", "US"),
        ("NASDAQ", "US"),
        ("HK", "HK"),
        ("hong_kong", "HK"),
        ("CRYPTO", "CRYPTO"),
        ("FUTURES", "FUTURES"),
        ("", "CN"),  # 空/未知保守回落 A 股（存量兼容）
        ("wat", "CN"),
        (None, "CN"),
    ],
)
def test_normalize_market_key(raw, expected):
    assert normalize_market_key(raw) == expected


# ── 时段表（本地时钟）──────────────────────────────────────────────


@pytest.mark.unit
def test_cn_sessions_local_clock():
    r = session_ranges_local("CN")
    assert r["AM"] == ("09:30", "11:30")
    assert r["PM"] == ("13:00", "15:00")
    assert r["AFTER_HOURS"] == ("15:05", "15:30")  # 盘后固定价格（2026-07-06 新规）


@pytest.mark.unit
def test_us_sessions_local_clock():
    r = session_ranges_local("US")
    assert r["AM"] == ("09:30", "16:00")  # 常规时段（美东本地钟点）
    assert r["AFTER_HOURS"] == ("16:00", "20:00")  # 盘后延长时段
    assert "PM" not in r  # 美股连续交易，无午休分段


@pytest.mark.unit
def test_hk_sessions_local_clock():
    r = session_ranges_local("HK")
    assert r["AM"] == ("09:30", "12:00")
    assert r["PM"] == ("13:00", "16:00")


@pytest.mark.unit
def test_crypto_sessions_24x7():
    r = session_ranges_local("CRYPTO")
    assert r["AM"] == ("00:00", "23:59")


@pytest.mark.unit
def test_futures_sessions_day_plus_night():
    r = session_ranges_local("FUTURES")
    assert r["AM"] == ("09:00", "15:00")  # 日盘（简化口径：商品差异化时段见文档）
    assert r["NIGHT"] == ("21:00", "02:30")  # 夜盘（跨午夜，评估需 wrap 语义）


@pytest.mark.unit
def test_unknown_market_falls_back_to_cn():
    assert session_ranges_local("wat") == session_ranges_local("CN")
    assert session_ranges_local(None) == session_ranges_local("CN")


@pytest.mark.unit
def test_session_tables_are_wellformed():
    """不变量：非 NIGHT 时段不跨午夜；HH:MM 格式；每市场至少一个时段。"""
    import re

    pat = re.compile(r"^\d{2}:\d{2}$")
    for market, sessions in MARKET_SESSIONS.items():
        assert sessions, f"{market} 无时段"
        for name, (start, end) in sessions.items():
            assert pat.match(start) and pat.match(end), f"{market}.{name} 格式错"
            if name != "NIGHT":
                assert start < end, f"{market}.{name} 跨午夜（非 NIGHT 不允许）"


@pytest.mark.unit
def test_cn_after_hours_matches_market_rules_constants():
    """防双源漂移：CN 盘后固定价格窗口必须等于 market_rules 的常量（T-P2-07 唯一事实源）。"""
    from backend.services.simulation.services.market_rules import (
        AFTER_HOURS_FIXED_END,
        AFTER_HOURS_FIXED_START,
    )

    r = session_ranges_local("CN")
    assert r["AFTER_HOURS"] == (
        AFTER_HOURS_FIXED_START.strftime("%H:%M"),
        AFTER_HOURS_FIXED_END.strftime("%H:%M"),
    )


@pytest.mark.unit
def test_in_session_hhmm_wraparound():
    from backend.shared.market_sessions import in_session_hhmm

    assert in_session_hhmm("10:00", "09:30", "16:00") is True
    assert in_session_hhmm("08:00", "09:30", "16:00") is False
    # 夜盘跨午夜
    assert in_session_hhmm("23:00", "21:00", "02:30") is True
    assert in_session_hhmm("01:00", "21:00", "02:30") is True
    assert in_session_hhmm("03:00", "21:00", "02:30") is False
    assert in_session_hhmm("20:00", "21:00", "02:30") is False


@pytest.mark.unit
def test_is_market_session_active_us_conversion():
    """美股 15:50（美东）活跃：夏天=北京 03:50、冬天=北京 04:50——DST 由时区换算吸收。"""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from backend.shared.market_sessions import is_market_session_active

    summer = datetime(2026, 7, 15, 3, 50, tzinfo=ZoneInfo("Asia/Shanghai"))  # EDT 15:50
    winter = datetime(2026, 1, 15, 4, 50, tzinfo=ZoneInfo("Asia/Shanghai"))  # EST 15:50
    assert is_market_session_active("US", "AM", summer) is True
    assert is_market_session_active("US", "AM", winter) is True
    assert (
        is_market_session_active(
            "US", "AM", datetime(2026, 7, 15, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        )
        is False
    )


# ── 时区与交易日历 ──────────────────────────────────────────────────


@pytest.mark.unit
def test_market_timezone_mapping():
    assert str(market_timezone("CN")) == "Asia/Shanghai"
    assert str(market_timezone("HK")) == "Asia/Hong_Kong"
    assert str(market_timezone("US")) == "America/New_York"
    assert str(market_timezone("a_share")) == "Asia/Shanghai"


@pytest.mark.unit
def test_market_calendar_mapping():
    assert market_calendar("CN") == "XSHG"
    assert market_calendar("HK") == "XHKG"
    assert market_calendar("US") == "XNYS"
    assert market_calendar("CRYPTO") is None  # 7×24，无日历


# ── 消费方接线（调度门 / 实盘配置校验）─────────────────────────────


@pytest.mark.unit
def test_scheduler_gate_uses_market_session_table():
    from backend.services.simulation.services.simulation_hosted_scheduler import (
        _is_enabled_session as gate,
    )

    # 美股本地钟：15:10 ∈ 常规时段（09:30–16:00）
    assert gate("15:10", {"enabled_sessions": ["AM"], "market": "US"}) is True
    assert gate("08:00", {"enabled_sessions": ["AM"], "market": "US"}) is False
    assert gate("19:50", {"enabled_sessions": ["AFTER_HOURS"], "market": "US"}) is True
    # 期货夜盘跨午夜
    assert gate("23:30", {"enabled_sessions": ["NIGHT"], "market": "FUTURES"}) is True
    assert gate("01:30", {"enabled_sessions": ["NIGHT"], "market": "FUTURES"}) is True
    assert gate("03:00", {"enabled_sessions": ["NIGHT"], "market": "FUTURES"}) is False
    # 缺省市场保持 A 股存量语义
    assert gate("14:50", {"enabled_sessions": ["PM"]}) is True


@pytest.mark.unit
def test_scheduler_trigger_us_market_timezone():
    """美股触发按美东钟：北京 03:50（夏令时）= 美东 15:50 → BUY 命中。"""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from backend.services.simulation.services.simulation_hosted_scheduler import (
        _normalize_live_trade_config as _norm,
    )
    from backend.services.simulation.services.simulation_hosted_scheduler import (
        _should_trigger,
    )

    cfg = _norm(
        {
            "market": "US",
            "enabled_sessions": ["AM"],
            "sell_time": "15:45",
            "buy_time": "15:50",
            "schedule_type": "interval",
            "rebalance_days": 1,
            "sell_first": False,
        }
    )
    # 2026-07-15（周三，XNYS 交易日）北京 03:50:10 = 美东 15:50:10
    now = datetime(2026, 7, 15, 3, 50, 10, tzinfo=ZoneInfo("Asia/Shanghai"))
    decision = _should_trigger(now=now, live_trade_config=cfg, started_day=None)
    assert decision.should_trigger is True
    assert decision.phase == "BUY"


@pytest.mark.unit
def test_live_config_validation_us_market_local_clock():
    from fastapi import HTTPException

    from backend.services.live_trading.routers.real_trading_utils import (
        _default_live_trade_config,
        _normalize_live_trade_config,
    )

    # 美股尾盘 15:50（美东钟）落在常规时段内
    cfg = _normalize_live_trade_config(
        {
            "market": "US",
            "enabled_sessions": ["AM"],
            "sell_time": "15:45",
            "buy_time": "15:50",
        },
        _default_live_trade_config(),
    )
    assert cfg["enabled_sessions"] == ["AM"]

    # 20:30 超出美股盘后时段（16:00–20:00）→ 拒绝（模拟路径 allow_after_hours）
    with pytest.raises(HTTPException):
        _normalize_live_trade_config(
            {
                "market": "US",
                "enabled_sessions": ["AFTER_HOURS"],
                "sell_time": "19:50",
                "buy_time": "20:30",
            },
            _default_live_trade_config(),
            allow_after_hours=True,
        )

    # 19:50 在盘后时段内 → 放行（仅模拟）
    cfg2 = _normalize_live_trade_config(
        {
            "market": "US",
            "enabled_sessions": ["AFTER_HOURS"],
            "sell_time": "19:45",
            "buy_time": "19:50",
        },
        _default_live_trade_config(),
        allow_after_hours=True,
    )
    assert cfg2["enabled_sessions"] == ["AFTER_HOURS"]
