"""滚动买卖/策略提醒的 rank_pct 口径与信号模型解析（2026-11-20）。

背景：旧默认阈值 2.2 是「0-10 绝对分」时代的产物，当前模型 fusion_score 在
[-0.33, +0.17]，导致 `score ≤ 2.2` 恒真（全部持仓卖出提醒）、`> 2.2` 恒假
（永不买入）。本文件锁定修复口径：
1. 阈值默认走截面分位 rank_pct（卖 ≤ 0.10 / 买 ≥ 0.90），跨模型量纲一致；
2. 缺分位/缺分数的持仓跳过，不按 0 卖出；
3. 信号模型 = 显式 > 策略绑定 > 用户默认模型（不再取最后跑完的 run）；
4. 旧配置（无 threshold_mode）自动迁移到 rank_pct，显式 score 模式保留。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.services.live_trading.services.tdx_rolling_trade_service import (
    RollingSettings,
    SignalModel,
    TdxRollingTradeService,
    load_rolling_settings,
    resolve_signal_model,
    save_rolling_config,
)

ROLLING_MODULE = "backend.services.live_trading.services.tdx_rolling_trade_service"
GET_REDIS_PATH = "backend.services.trade_shared.redis_client.get_redis"


def _pos(symbol, volume=1000, available_volume=None, name=""):
    return {
        "symbol": symbol,
        "name": name or symbol,
        "volume": volume,
        "available_volume": available_volume if available_volume is not None else volume,
        "cost_price": 10.0,
        "market_value": 10000.0,
    }


def _redis_holding(saved: dict | None):
    mock_redis = MagicMock()
    mock_redis.get.return_value = saved
    return mock_redis


@pytest.fixture
def svc() -> TdxRollingTradeService:
    return TdxRollingTradeService()


class TestRankPctMode:
    def test_rank_mode_buys_top_decile(self, svc):
        result = svc.compute_rolling_signals(
            score_map={"000001.SZ": 0.01, "000002.SZ": 0.02},
            rank_map={"000001.SZ": 0.95, "000002.SZ": 0.80},
            positions=[],
            index_above_ma20=True,
            threshold_mode="rank_pct",
            rank_pct_threshold=0.10,
        )
        assert [b["symbol"] for b in result["buys"]] == ["000001.SZ"]
        assert result["threshold_mode"] == "rank_pct"

    def test_rank_mode_sells_bottom_decile(self, svc):
        result = svc.compute_rolling_signals(
            score_map={"600519.SH": 0.05},
            rank_map={"600519.SH": 0.049},
            positions=[_pos("600519.SH")],
            index_above_ma20=True,
            threshold_mode="rank_pct",
            rank_pct_threshold=0.10,
        )
        assert [s["symbol"] for s in result["sells"]] == ["600519.SH"]
        assert "分位" in result["sells"][0]["reason"]

    def test_rank_mode_missing_rank_is_skipped_not_sold(self, svc):
        result = svc.compute_rolling_signals(
            score_map={"600519.SH": 0.05},
            rank_map={},
            positions=[_pos("600519.SH")],
            index_above_ma20=True,
            threshold_mode="rank_pct",
            rank_pct_threshold=0.10,
        )
        assert result["sells"] == []
        assert [u["symbol"] for u in result["unscored"]] == ["600519.SH"]

    def test_rank_mode_ignores_absolute_score(self, svc):
        result = svc.compute_rolling_signals(
            score_map={"000001.SZ": 2.8},
            rank_map={"000001.SZ": 0.5},
            positions=[],
            index_above_ma20=True,
            threshold_mode="rank_pct",
            rank_pct_threshold=0.10,
        )
        assert result["buys"] == []

    def test_score_mode_keeps_legacy_behavior(self, svc):
        result = svc.compute_rolling_signals(
            score_map={"600519.SH": 1.9},
            positions=[_pos("600519.SH")],
            index_above_ma20=True,
            threshold_mode="score",
            score_threshold=2.2,
        )
        assert [s["symbol"] for s in result["sells"]] == ["600519.SH"]


class TestRollingSettingsMigration:
    def test_legacy_config_without_mode_migrates_to_rank(self):
        with patch(
            GET_REDIS_PATH,
            return_value=_redis_holding(
                {"score_threshold": 2.2, "fixed_buy_amount": 10000.0}
            ),
        ):
            settings = load_rolling_settings("default", "u")
        assert settings.threshold_mode == "rank_pct"
        assert settings.rank_pct_threshold == pytest.approx(0.10)

    def test_explicit_score_mode_is_preserved(self):
        with patch(
            GET_REDIS_PATH,
            return_value=_redis_holding(
                {"threshold_mode": "score", "score_threshold": 2.2}
            ),
        ):
            settings = load_rolling_settings("default", "u")
        assert settings.threshold_mode == "score"

    def test_rank_fields_round_trip(self):
        mock_redis = MagicMock()
        with patch(GET_REDIS_PATH, return_value=mock_redis):
            save_rolling_config(
                "default",
                "u",
                score_threshold=2.2,
                fixed_buy_amount=10000,
                execute_mode="off",
                threshold_mode="rank_pct",
                rank_pct_threshold=0.05,
                signal_model_id="mdl_x",
            )
        saved = mock_redis.set.call_args.args[1]
        assert saved["threshold_mode"] == "rank_pct"
        assert saved["rank_pct_threshold"] == pytest.approx(0.05)
        assert saved["signal_model_id"] == "mdl_x"
        with patch(GET_REDIS_PATH, return_value=_redis_holding(saved)):
            settings = load_rolling_settings("default", "u")
        assert settings.rank_pct_threshold == pytest.approx(0.05)
        assert settings.signal_model_id == "mdl_x"


class _Rows:
    def __init__(self, rows):
        self._rows = list(rows)

    def mappings(self):
        return self

    def first(self):
        return self._rows[0] if self._rows else None


class _FakeSession:
    def __init__(self, rows):
        self._rows = rows

    async def execute(self, statement, params=None):
        sql = str(statement)
        if "qm_strategy_model_bindings" in sql:
            return _Rows(self._rows.get("binding", []))
        if "is_default" in sql:
            return _Rows(self._rows.get("default", []))
        if "model_id = :model_id" in sql:
            return _Rows(self._rows.get("explicit", []))
        return _Rows([])

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class TestSignalModelResolution:
    @pytest.mark.asyncio
    async def test_explicit_beats_binding_and_default(self, monkeypatch):
        fake = _FakeSession(
            {
                "explicit": [{"model_id": "explicit", "display_name": "E"}],
                "binding": [{"model_id": "binding", "display_name": "B"}],
                "default": [{"model_id": "default", "display_name": "D"}],
            }
        )
        monkeypatch.setattr(f"{ROLLING_MODULE}.get_session", lambda **kw: fake)
        out = await resolve_signal_model("default", "u", "explicit")
        assert out.model_id == "explicit"
        assert out.source == "explicit"

    @pytest.mark.asyncio
    async def test_binding_beats_default(self, monkeypatch):
        fake = _FakeSession(
            {
                "explicit": [],
                "binding": [{"model_id": "binding", "display_name": "B"}],
                "default": [{"model_id": "default", "display_name": "D"}],
            }
        )
        monkeypatch.setattr(f"{ROLLING_MODULE}.get_session", lambda **kw: fake)
        out = await resolve_signal_model("default", "u")
        assert out.model_id == "binding"
        assert out.source == "strategy_binding"

    @pytest.mark.asyncio
    async def test_default_when_no_binding(self, monkeypatch):
        fake = _FakeSession(
            {
                "explicit": [],
                "binding": [],
                "default": [{"model_id": "default", "display_name": "D"}],
            }
        )
        monkeypatch.setattr(f"{ROLLING_MODULE}.get_session", lambda **kw: fake)
        out = await resolve_signal_model("default", "u")
        assert out.model_id == "default"
        assert out.source == "default"

    @pytest.mark.asyncio
    async def test_unresolved(self, monkeypatch):
        fake = _FakeSession({"explicit": [], "binding": [], "default": []})
        monkeypatch.setattr(f"{ROLLING_MODULE}.get_session", lambda **kw: fake)
        out = await resolve_signal_model("default", "u")
        assert out.model_id == ""
        assert out.source == "unresolved"


def test_strategy_alert_decision_rank_mode():
    from backend.services.live_trading.services.tdx_quote_feed import (
        strategy_alert_decision,
    )

    ok, reason = strategy_alert_decision(
        score=0.004,
        rank_pct=0.049,
        threshold_mode="rank_pct",
        score_threshold=2.2,
        rank_pct_threshold=0.10,
    )
    assert ok is True
    assert "分位 0.049" in reason


def test_strategy_alert_decision_rank_mode_skips_missing():
    from backend.services.live_trading.services.tdx_quote_feed import (
        strategy_alert_decision,
    )

    ok, _ = strategy_alert_decision(
        score=0.004,
        rank_pct=None,
        threshold_mode="rank_pct",
        score_threshold=2.2,
        rank_pct_threshold=0.10,
    )
    assert ok is False


def test_strategy_alert_decision_rank_mode_holds_top():
    from backend.services.live_trading.services.tdx_quote_feed import (
        strategy_alert_decision,
    )

    ok, _ = strategy_alert_decision(
        score=0.058,
        rank_pct=0.906,
        threshold_mode="rank_pct",
        score_threshold=2.2,
        rank_pct_threshold=0.10,
    )
    assert ok is False


def test_strategy_alert_decision_score_mode_skips_missing():
    from backend.services.live_trading.services.tdx_quote_feed import (
        strategy_alert_decision,
    )

    ok, _ = strategy_alert_decision(
        score=None,
        rank_pct=0.05,
        threshold_mode="score",
        score_threshold=2.2,
        rank_pct_threshold=0.10,
    )
    assert ok is False


def test_strategy_digest_line_rank_mode():
    from backend.services.live_trading.services.tdx_quote_feed import (
        format_strategy_digest_line,
    )

    line = format_strategy_digest_line(
        symbol="SH601658",
        name="邮储银行",
        score=0.004,
        threshold=2.2,
        rank_pct=0.049,
        threshold_mode="rank_pct",
        rank_pct_threshold=0.10,
        model_name="ML7 solo",
    )
    assert line == "⚠️ 邮储银行(SH601658) 位置靠后（分位 0.049 ≤ 0.100） ｜ ML7 solo"
