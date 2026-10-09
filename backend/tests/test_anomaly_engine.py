"""识别引擎服务测试（T-P6-14）：门控/动作接线/节流/计数（依赖全桩，无 IO）。"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

pytestmark = pytest.mark.unit

# 市场族只在连续竞价时段取数（见 anomaly_engine.in_market_session）：本文件的市场类
# 用例必须把时钟钉在时段内，否则随测试运行时钟点漂移（2026-10-08 前是 1000.0=08:16）。
_SESSION_EPOCH = datetime(2026, 10, 8, 10, 30, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()


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
        now_fn=lambda: _SESSION_EPOCH,
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


# ── §6.5-L2：弱区降险建议接线（默认关；只建议不执行）──────────────────────

_L2_MODEL_ID = "mdl_l2_train_20261008000000_aaaaaaaabbbbbbbb_cccccccc"  # 市场由 monkeypatch 定


def _wire_l2_regime(monkeypatch, *, weak: bool):
    """[§6.5-L2] 桩掉 regime 取数：模型属 CN、状态表含 today（now_fn 钉在 2026-10-08）。"""
    days = [f"2026-09-{d:02d}" for d in range(1, 17)]
    values = [-0.01, -0.03] * 8 if weak else [0.01, 0.03] * 8
    daily_ic = [{"date": day, "value": v} for day, v in zip(days, values, strict=True)]
    states = dict.fromkeys(days, "neutral")
    states["2026-10-08"] = "neutral"  # _SESSION_EPOCH 的 SH 日期 = today_iso

    monkeypatch.setattr(
        "backend.shared.model_registry._model_market_of", lambda m: "CN"
    )
    monkeypatch.setattr(
        "backend.shared.regime_daily_store.load_states_sync", lambda m: states
    )
    row = {
        "model_id": _L2_MODEL_ID,
        "ic_stats": {"ic_5": -0.02 if weak else 0.01, "ic_20": 0.03,
                     "n_5": 6, "n_20": 20},
        "daily_ic": daily_ic,
    }
    return row


def test_l2_weak_regime_attaches_advice_even_when_l1_downgraded_to_info(monkeypatch):
    """弱区命中的模型检测：L1 归因降级 info 不削减 L2 建议；建议只落建议面，不执行。"""
    from backend.services.engine.anomaly_engine import AnomalyConfig

    row = _wire_l2_regime(monkeypatch, weak=True)
    engine, calls, holder = _engine(
        cfg=AnomalyConfig(enabled=True, reduce_enabled=True),
        model_fetcher=lambda cfg: [row],
    )
    engine.build_once()

    assert len(calls["published"]) == 1
    d = calls["published"][0]
    assert d.kind == "model_ic_drop"
    assert d.severity == "info"  # L1：弱区 + 2σ 内 → 归因降级
    advice = d.metrics["position_advice"]
    assert advice["state"] == "neutral"
    assert advice["from_factor"] == 1.0 and advice["to_factor"] == 0.7
    assert "降险建议" in d.description and "仅建议不执行" in d.description

    # info 级别也进降险通道（v1 闸门只看 critical；L2 认弱区标记）
    assert [x.kind for x in calls["reduced"]] == ["model_ic_drop"]

    # 审计文案含阶梯前后系数（拦 _audit：单测不碰真库）
    captured: dict = {}
    engine._audit = lambda detection, **kw: captured.update(kw)
    out = engine._default_reduce(d)
    assert out == {"reduced": False, "suggested": True}
    assert captured["action"] == "reduce_suggested" and captured["status"] == "pending"
    assert "仓位系数 1→0.7" in captured["message"]


def test_l2_off_by_default_keeps_detection_face_identical(monkeypatch):
    """级关（默认）：弱区检测面与旧版逐位一致（无建议键、无降险动作）。"""
    from backend.services.engine.anomaly_engine import AnomalyConfig

    row = _wire_l2_regime(monkeypatch, weak=True)
    engine, calls, holder = _engine(
        cfg=AnomalyConfig(enabled=True, reduce_enabled=False),
        model_fetcher=lambda cfg: [row],
    )
    engine.build_once()
    d = calls["published"][0]
    assert "position_advice" not in (d.metrics or {})
    assert "降险建议" not in d.description
    assert calls["reduced"] == []


def test_l2_not_weak_no_advice(monkeypatch):
    """级开但非弱区：不出建议；warn（IC 为正的回撤）不进降险通道。"""
    from backend.services.engine.anomaly_engine import AnomalyConfig

    row = _wire_l2_regime(monkeypatch, weak=False)
    engine, calls, holder = _engine(
        cfg=AnomalyConfig(enabled=True, reduce_enabled=True),
        model_fetcher=lambda cfg: [row],
    )
    engine.build_once()
    d = calls["published"][0]
    assert d.severity == "warn"
    assert "position_advice" not in (d.metrics or {})
    assert calls["reduced"] == []


def test_l2_no_regime_context_no_advice(monkeypatch):
    """取不到 regime（无 daily_ic）→ 上下文 None → 照常告警，无建议、无降险。"""
    from backend.services.engine.anomaly_engine import AnomalyConfig

    row = {"model_id": _L2_MODEL_ID,
           "ic_stats": {"ic_5": -0.05, "ic_20": 0.03, "n_5": 6, "n_20": 20}}
    engine, calls, holder = _engine(
        cfg=AnomalyConfig(enabled=True, reduce_enabled=True),
        model_fetcher=lambda cfg: [row],
    )
    engine.build_once()
    d = calls["published"][0]
    assert d.severity == "critical"  # short < 0 且无归因
    assert "position_advice" not in (d.metrics or {})
    # v1 语义保留：critical 仍进降险通道（无建议文案）
    assert len(calls["reduced"]) == 1
