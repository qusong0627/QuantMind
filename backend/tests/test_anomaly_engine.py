"""识别引擎服务测试（T-P6-14）：门控/动作接线/节流/计数（依赖全桩，无 IO）。"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def _engine(**overrides):
    from backend.services.engine.anomaly_engine import AnomalyConfig, AnomalyEngine

    calls = {"published": [], "recorded": [], "denied": [], "reduced": [], "recent": []}
    cfg_holder = {"cfg": overrides.pop("cfg", AnomalyConfig(enabled=True))}

    engine = AnomalyEngine(
        config_loader=lambda: cfg_holder["cfg"],
        market_fetcher=overrides.pop("market_fetcher", lambda cfg: {}),
        account_fetcher=overrides.pop("account_fetcher", lambda cfg: []),
        data_fetcher=overrides.pop("data_fetcher", lambda cfg: []),
        model_fetcher=overrides.pop("model_fetcher", lambda cfg: []),
        publisher=lambda d: calls["published"].append(d),
        recorder=lambda d: calls["recorded"].append(d),
        denier=lambda d: (calls["denied"].append(d) or {"locked": ["u1"]}),
        reducer=lambda d: (calls["reduced"].append(d) or {"suggested": True}),
        recent_marker=lambda ds: calls["recent"].append(list(ds)),
        deduper=lambda ds, cfg: (list(ds), 0),  # 单测不经 Redis；去重行为由集成测试覆盖
        status_writer=lambda payload: None,
        now_fn=lambda: 1000.0,
    )
    return engine, calls, cfg_holder


def test_disabled_engine_is_noop():
    engine, calls, holder = _engine()
    holder["cfg"] = type(holder["cfg"])(enabled=False)
    result = engine.build_once()
    assert result == {"enabled": False}
    assert calls["published"] == [] and calls["recorded"] == []


def test_market_detection_publishes_records_and_denies_only_critical():
    from backend.services.engine.anomaly_engine import AnomalyConfig

    quotes = {
        "600036.SH": {"price": 40.0, "pct_chg": 0.01, "now_volume": 100_000.0,
                      "avg_daily_volume": 1_000.0},  # 巨量 → critical
        "000001.SZ": {"price": 12.0, "pct_chg": -0.06},  # 大幅下行 → warn
    }
    engine, calls, holder = _engine(
        cfg=AnomalyConfig(enabled=True, volume_ratio_min=3.0, price_pct_min=0.05),
        market_fetcher=lambda cfg: quotes,
    )
    result = engine.build_once()
    assert result["enabled"] is True
    assert result["detections"] >= 2
    assert len(calls["published"]) == result["detections"]
    assert len(calls["recorded"]) == result["detections"]
    # 仅 critical（巨量）走否决；warn 不动
    assert len(calls["denied"]) == 1
    assert calls["denied"][0].severity == "critical"
    assert calls["recent"] and len(calls["recent"][0]) >= 1


def test_deny_can_be_disabled_by_config():
    from backend.services.engine.anomaly_engine import AnomalyConfig

    quotes = {"600036.SH": {"price": 40.0, "pct_chg": 0.01, "now_volume": 100_000.0,
                            "avg_daily_volume": 1_000.0}}
    engine, calls, holder = _engine(
        cfg=AnomalyConfig(enabled=True, deny_enabled=False),
        market_fetcher=lambda cfg: quotes,
    )
    engine.build_once()
    assert calls["denied"] == []


def test_reduce_gated_off_by_default_on_when_enabled():
    from backend.services.engine.anomaly_engine import AnomalyConfig

    quotes = {"600036.SH": {"price": 40.0, "pct_chg": 0.01, "now_volume": 100_000.0,
                            "avg_daily_volume": 1_000.0}}
    engine, calls, holder = _engine(
        cfg=AnomalyConfig(enabled=True, reduce_enabled=False),
        market_fetcher=lambda cfg: quotes,
    )
    engine.build_once()
    assert calls["reduced"] == []

    engine2, calls2, holder2 = _engine(
        cfg=AnomalyConfig(enabled=True, reduce_enabled=True),
        market_fetcher=lambda cfg: quotes,
    )
    engine2.build_once()
    assert len(calls2["reduced"]) == 1


def test_data_and_model_fetchers_respect_cadence():
    from backend.services.engine.anomaly_engine import AnomalyConfig

    counts = {"data": 0, "model": 0}

    def data_fetcher(cfg):
        counts["data"] += 1
        return []

    def model_fetcher(cfg):
        counts["model"] += 1
        return []

    times = {"now": 1000.0}
    engine, calls, holder = _engine(
        cfg=AnomalyConfig(enabled=True, data_every_s=1800, model_every_s=3600),
        data_fetcher=data_fetcher,
        model_fetcher=model_fetcher,
    )
    engine._now = lambda: times["now"]
    engine.build_once()
    engine.build_once()  # 同一时刻第二轮：低频取数不应再跑
    assert counts == {"data": 1, "model": 1}
    times["now"] = 1000.0 + 2000
    engine.build_once()
    assert counts["data"] == 2 and counts["model"] == 1
    times["now"] = 1000.0 + 4000
    engine.build_once()
    assert counts["data"] == 3 and counts["model"] == 2


def test_fetch_errors_counted_not_fatal():
    from backend.services.engine.anomaly_engine import AnomalyConfig

    def boom(cfg):
        raise RuntimeError("redis down")

    engine, calls, holder = _engine(
        cfg=AnomalyConfig(enabled=True),
        market_fetcher=boom,
        account_fetcher=boom,
        data_fetcher=boom,
        model_fetcher=boom,
    )
    engine.build_once()
    assert engine.counters["errors"] >= 4
    assert "redis down" in (engine.counters["last_error"] or "")


def test_account_detection_flows_through_actions():
    from backend.services.engine.anomaly_engine import AnomalyConfig

    accounts = [{
        "user_id": "42",
        "orders": [{"status": "cancelled"}] * 9 + [{"status": "filled"}],
        "positions": [{"symbol": "600036.SH", "market_value": 1_000_000}],
    }]
    engine, calls, holder = _engine(
        cfg=AnomalyConfig(enabled=True, cancel_ratio_min=0.6, min_orders=5,
                          concentration_max=0.5),
        account_fetcher=lambda cfg: accounts,
    )
    result = engine.build_once()
    kinds = {d.kind for d in calls["published"]}
    assert "account_cancel_ratio" in kinds and "account_concentration" in kinds
    assert result["detections"] == 2
    # 撤单率 90% → critical → 否决（fail-closed 锁账户）；集中度 warn 不动
    assert len(calls["denied"]) == 1
    assert calls["denied"][0].kind == "account_cancel_ratio"


def test_trading_elapsed_fraction_bounds():
    from datetime import datetime

    from backend.services.engine.anomaly_engine import trading_elapsed_fraction

    def at(h, m):
        return datetime(2026, 9, 17, h, m, tzinfo=__import__("zoneinfo").ZoneInfo("Asia/Shanghai"))

    assert trading_elapsed_fraction(at(9, 0)) == pytest.approx(0.05)  # 盘前下限
    assert trading_elapsed_fraction(at(10, 30)) == pytest.approx(0.25)
    assert trading_elapsed_fraction(at(12, 0)) == pytest.approx(0.5)
    assert trading_elapsed_fraction(at(15, 30)) == pytest.approx(1.0)
