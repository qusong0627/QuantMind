"""TdxQuoteFeed 实时行情 Feed 单元测试。

纯函数级测试：交易时段门控、快照字段映射、止损止盈触发、配置存取。
"""
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from backend.services.live_trading.services.tdx_quote_feed import (
    TZ,
    build_tick_row,
    check_sltp_trigger,
    is_trading_time,
    load_sltp_config,
    map_snapshot,
    reconcile_sessions,
    save_sltp_config,
    _normalize_prefix,
    _to_suffix,
)


def _sh(year, month, day, hour, minute, weekday):
    """构造 Asia/Shanghai 时间（weekday 仅用于断言正确性，datetime 自动推算）。"""
    dt = datetime(year, month, day, hour, minute, tzinfo=TZ)
    assert dt.weekday() == weekday, f"{dt} 不是周{weekday}"
    return dt


class TestIsTradingTime:
    def test_morning_session(self):
        # 09:30 开盘后（周一）
        assert is_trading_time(_sh(2026, 8, 24, 9, 30, 0))

    def test_pre_open_call_auction(self):
        # 09:15 集合竞价开始
        assert is_trading_time(_sh(2026, 8, 24, 9, 15, 0))

    def test_lunch_break_not_trading(self):
        # 11:35-12:55 午休
        assert not is_trading_time(_sh(2026, 8, 24, 12, 0, 0))

    def test_afternoon_session(self):
        assert is_trading_time(_sh(2026, 8, 24, 13, 0, 0))

    def test_after_close_not_trading(self):
        # 15:05 收盘后
        assert not is_trading_time(_sh(2026, 8, 24, 15, 10, 0))

    def test_weekend_not_trading(self):
        # 2026-08-22 是周六
        assert not is_trading_time(_sh(2026, 8, 22, 10, 0, 5))

    def test_none_uses_now(self):
        # 不传参也能运行（不抛异常）
        assert isinstance(is_trading_time(), bool)


class TestMapSnapshot:
    def test_full_mapping(self):
        result = map_snapshot({
            "Now": 10.5, "Open": 10.0, "Max": 10.8, "Min": 9.9,  # fidelity: allow-limit-threshold — 非阈值：TDX 快照夹具的 Min
            "LastClose": 10.2, "Volume": 12345, "Amount": 12345678.0,
        })
        assert result["Now"] == 10.5
        assert result["Open"] == 10.0
        assert result["High"] == 10.8
        assert result["Low"] == 9.9  # fidelity: allow-limit-threshold — 非阈值：断言 Min 原样映射（值即夹具）
        assert result["PreClose"] == 10.2
        assert result["Volume"] == 12345
        assert result["Amount"] == 12345678.0
        assert isinstance(result["timestamp"], int)

    def test_nan_required_price_invalidates_snapshot(self):
        # Now 为 NaN 视为无有效快照（必填字段缺失）
        result = map_snapshot({
            "Now": float("nan"), "Open": 10.0, "LastClose": 10.2,
            "Max": 10.8, "Min": 9.9, "Volume": 1, "Amount": 0,  # fidelity: allow-limit-threshold — 非阈值：TDX 快照夹具的 Min
        })
        assert result is None

    def test_nan_optional_fields_mapped_to_none(self):
        # 仅 High(Max) 为 NaN 时其余字段保留
        result = map_snapshot({
            "Now": 10.5, "Open": 10.0, "LastClose": 10.2,
            "Max": float("nan"), "Min": 9.9, "Volume": 1, "Amount": 0,  # fidelity: allow-limit-threshold — 非阈值：TDX 快照夹具的 Min
        })
        assert result["Now"] == 10.5
        assert result["High"] is None

    def test_missing_required_fields_returns_none(self):
        assert map_snapshot({"Now": 10.5}) is None
        assert map_snapshot({}) is None

    def test_non_dict_returns_none(self):
        assert map_snapshot(None) is None
        assert map_snapshot("x") is None

    def test_volume_non_numeric_defaults_zero(self):
        result = map_snapshot({
            "Now": 10.5, "Open": 10.0, "LastClose": 10.2,
            "Volume": "N/A", "Amount": None,
        })
        assert result["Volume"] == 0
        assert result["Amount"] is None


class TestCheckSltpTrigger:
    def test_stop_loss_trigger(self):
        triggered, reason = check_sltp_trigger(9.2, 10.0, {"stop_loss_pct": 0.08})
        assert triggered
        assert "止损" in reason

    def test_stop_loss_not_triggered(self):
        triggered, _ = check_sltp_trigger(9.3, 10.0, {"stop_loss_pct": 0.08})
        assert not triggered

    def test_take_profit_trigger(self):
        triggered, reason = check_sltp_trigger(11.0, 10.0, {"take_profit_pct": 0.10})
        assert triggered
        assert "止盈" in reason

    def test_trailing_stop_from_highest(self):
        # 最高 11.0，回撤 5% 线 10.45
        triggered, reason = check_sltp_trigger(
            10.4, 10.0, {"trailing_stop_pct": 0.05, "highest_price": 11.0}
        )
        assert triggered
        assert "移动止损" in reason

    def test_trailing_stop_above_line(self):
        triggered, _ = check_sltp_trigger(
            10.5, 10.0, {"trailing_stop_pct": 0.05, "highest_price": 11.0}
        )
        assert not triggered

    def test_no_config_no_trigger(self):
        triggered, _ = check_sltp_trigger(1.0, 10.0, {})
        assert not triggered

    def test_invalid_prices_no_trigger(self):
        triggered, _ = check_sltp_trigger(0, 10.0, {"stop_loss_pct": 0.08})
        assert not triggered
        triggered, _ = check_sltp_trigger(9.0, 0, {"stop_loss_pct": 0.08})
        assert not triggered


class TestSymbolNormalization:
    def test_suffix_to_prefix(self):
        assert _normalize_prefix("600036.SH") == "SH600036"

    def test_prefix_passthrough(self):
        assert _normalize_prefix("SH600036") == "SH600036"

    def test_lowercase_prefix_upper(self):
        assert _normalize_prefix("sz000001") == "SZ000001"

    def test_prefix_to_suffix(self):
        assert _to_suffix("SH600036") == "600036.SH"


class TestSltpConfig:
    GET_REDIS_PATH = "backend.services.trade_shared.redis_client.get_redis"

    def test_load_default_when_redis_empty(self):
        mock_redis = MagicMock()
        mock_redis.get.return_value = None
        with patch(self.GET_REDIS_PATH, return_value=mock_redis):
            cfg = load_sltp_config("default", "00000001")
        assert cfg["stop_loss_pct"] == 0.08
        assert cfg["take_profit_pct"] is None
        assert cfg["enabled"] is True

    def test_load_merged_from_redis(self):
        mock_redis = MagicMock()
        mock_redis.get.return_value = {"stop_loss_pct": 0.05, "take_profit_pct": 0.2}
        with patch(self.GET_REDIS_PATH, return_value=mock_redis):
            cfg = load_sltp_config("default", "00000001")
        assert cfg["stop_loss_pct"] == 0.05
        assert cfg["take_profit_pct"] == 0.2
        assert cfg["enabled"] is True

    def test_save_normalizes_and_persists(self):
        mock_redis = MagicMock()
        with patch(self.GET_REDIS_PATH, return_value=mock_redis):
            saved = save_sltp_config("default", "00000001", {
                "stop_loss_pct": 0.05, "take_profit_pct": 0.2,
                "trailing_stop_pct": None, "enabled": False,
            })
        assert saved["stop_loss_pct"] == 0.05
        assert saved["take_profit_pct"] == 0.2
        assert saved["trailing_stop_pct"] is None
        assert saved["enabled"] is False
        mock_redis.set.assert_called_once()
        key = mock_redis.set.call_args.args[0]
        assert key == "trade:sltp_config:default:00000001"

    def test_save_empty_take_profit_falls_back(self):
        mock_redis = MagicMock()
        with patch(self.GET_REDIS_PATH, return_value=mock_redis):
            saved = save_sltp_config("default", "00000001", {"stop_loss_pct": 0.0})
        # 兜底默认止损
        assert saved["stop_loss_pct"] == 0.08
        assert saved["take_profit_pct"] is None


class TestReconcileSessions:
    def test_new_position_opens_session(self):
        now = datetime(2026, 8, 25, 9, 30, 0)
        new, kept, opened, closed = reconcile_sessions(
            {"SH600036"}, {}, now, "default", "00000001"
        )
        assert opened == ["SH600036"]
        assert new["SH600036"] == "tdx:default:00000001:SH600036:20260825093000"
        assert kept == {}
        assert closed == []

    def test_kept_positions_stay(self):
        now = datetime(2026, 8, 25, 9, 30, 0)
        current = {"SH600036": "tdx:default:00000001:SH600036:20260824090000"}
        new, kept, opened, closed = reconcile_sessions(
            {"SH600036", "SZ000001"}, current, now, "default", "00000001"
        )
        assert kept == current
        assert opened == ["SZ000001"]
        assert closed == []

    def test_sold_position_closes(self):
        now = datetime(2026, 8, 25, 9, 30, 0)
        current = {"SH600036": "sid-1", "SZ000001": "sid-2"}
        new, kept, opened, closed = reconcile_sessions(
            {"SH600036"}, current, now, "default", "00000001"
        )
        assert kept == {"SH600036": "sid-1"}
        assert closed == ["SZ000001"]
        assert opened == []
        assert new == {}

    def test_no_held_no_change(self):
        now = datetime(2026, 8, 25, 9, 30, 0)
        new, kept, opened, closed = reconcile_sessions(
            set(), {}, now, "default", "00000001"
        )
        assert new == {} and kept == {} and opened == [] and closed == []


class TestBuildTickRow:
    def test_builds_row(self):
        now = datetime(2026, 8, 25, 9, 30, 5)
        row = build_tick_row(
            tenant_id="default",
            user_id="00000001",
            symbol="SH600036",
            session_id="tdx:default:00000001:SH600036:20260825090000",
            snap={
                "Now": 10.5, "Open": 10.0, "High": 10.8, "Low": 9.9,  # fidelity: allow-limit-threshold — 非阈值：TDX 快照夹具的 Min
                "Volume": 1000, "Amount": 10500.0,
            },
            now=now,
        )
        assert row["tenant_id"] == "default"
        assert row["user_id"] == "00000001"
        assert row["symbol"] == "SH600036"
        assert row["session_id"] == "tdx:default:00000001:SH600036:20260825090000"
        assert row["price"] == 10.5
        assert row["high"] == 10.8
        assert row["volume"] == 1000
        assert row["source"] == "tdx_bridge"
        assert row["tick_time"] == now

    def test_missing_fields_none(self):
        row = build_tick_row(
            tenant_id="default",
            user_id="00000001",
            symbol="SZ000001",
            session_id="s",
            snap={},
            now=datetime(2026, 8, 25, 9, 30, 5),
        )
        assert row["price"] is None
        assert row["volume"] is None
        assert row["is_stale"] is False
