"""P3 §6.4 告警归因：弱区 + 2σ 内 → 降级 info；证不出就照常告警（纯函数 + 接线）。

- ``attribute_ic_anomaly``：弱区谓词与 2σ 判据复用 ``regime_buckets`` 唯一实现；
  缺任一环（无当日状态/无序列/无桶）一律原样返回——**不降级**；
- ``detect_model_anomaly(regime_context=...)``：发报前归因；无 context 行为同旧版；
- ``AnomalyEngine._regime_context_for``：按市场缓存一轮、失败记 errors、表外市场诚实 None；
- ``model_ic_monitor._series_points``：日 IC 序列 → 归因消费形状。
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from backend.services.engine.anomaly_detectors import (
    Detection,
    LEVEL_CRITICAL,
    LEVEL_INFO,
    LEVEL_WARN,
    attribute_ic_anomaly,
    detect_model_anomaly,
)


def _series(values: list[float], start: str = "2026-09-01") -> list[dict]:
    d0 = date.fromisoformat(start)
    return [
        {"date": (d0 + timedelta(days=i)).isoformat(), "value": v}
        for i, v in enumerate(values)
    ]


def _stats(ic5: float = -0.05, ic20: float = 0.03, n5: int = 6) -> dict:
    return {"ic_5": ic5, "ic_20": ic20, "n_5": n5, "n_20": 20}


def _all_states(points: list[dict], state: str = "neutral") -> dict[str, str]:
    return {p["date"]: state for p in points}


# ── attribute_ic_anomaly 纯函数 ─────────────────────────────────────


@pytest.mark.unit
def test_weak_bucket_within_2sigma_downgrades_to_info():
    points = _series([-0.05, -0.04, -0.06] * 6)  # 18 天，均值 -0.05、σ≈0.0089
    dets = detect_model_anomaly(
        "m1",
        _stats(ic5=-0.05),
        regime_context={
            "current_state": "neutral",
            "states": _all_states(points),
            "daily_ic": points,
        },
    )
    assert len(dets) == 1
    det = dets[0]
    assert det.severity == LEVEL_INFO  # 原判 critical（short<0）
    assert "regime 归因（弱区，在期望内）" in det.description
    attr = det.metrics["regime_attribution"]
    assert attr["state"] == "neutral"
    assert attr["bucket_days"] == 18
    assert attr["current_ic"] == pytest.approx(-0.05)
    assert attr["k"] == 2.0


@pytest.mark.unit
def test_weak_bucket_outside_2sigma_keeps_alarm():
    points = _series([-0.05, -0.04, -0.06] * 6)
    dets = detect_model_anomaly(
        "m1",
        _stats(ic5=-0.30),  # |−0.30+0.05| = 0.25 ≫ 2σ
        regime_context={
            "current_state": "neutral",
            "states": _all_states(points),
            "daily_ic": points,
        },
    )
    assert dets[0].severity == LEVEL_CRITICAL
    assert "regime 归因" not in dets[0].description


@pytest.mark.unit
def test_warn_severity_can_also_be_downgraded():
    """相对骤降路径（short>0 → warn）命中弱区且在期望内同样降级。"""
    points = _series([-0.05, 0.05, -0.06, 0.04] * 4)  # 16 天，均值 -0.005、σ≈0.058
    dets = detect_model_anomaly(
        "m1",
        _stats(ic5=0.01, ic20=0.10),  # 0.01 < 0.10×0.5 → 相对骤降，warn
        regime_context={
            "current_state": "neutral",
            "states": _all_states(points),
            "daily_ic": points,
        },
    )
    assert dets[0].severity == LEVEL_INFO
    # 无 context 时同一输入维持 warn（行为同旧版）
    plain = detect_model_anomaly("m1", _stats(ic5=0.01, ic20=0.10))
    assert plain[0].severity == LEVEL_WARN


@pytest.mark.unit
@pytest.mark.parametrize(
    "context, label",
    [
        (None, "无 context"),
        ({"current_state": None, "states": {"x": "neutral"}, "daily_ic": [{"date": "x", "value": 0.1}]}, "无当日状态"),
        ({"current_state": "neutral", "states": {}, "daily_ic": [{"date": "x", "value": 0.1}]}, "无时间线"),
        ({"current_state": "neutral", "states": {"2026-09-01": "neutral"}, "daily_ic": []}, "无序列"),
    ],
)
def test_missing_any_link_keeps_alarm(context, label):
    dets = detect_model_anomaly("m1", _stats(ic5=-0.05), regime_context=context)
    assert dets[0].severity == LEVEL_CRITICAL, label


@pytest.mark.unit
def test_short_history_or_positive_bucket_not_weak():
    # 14 天（<15）不下弱区结论 → 照常告警
    short_pts = _series([-0.05] * 14)
    dets = detect_model_anomaly(
        "m1",
        _stats(ic5=-0.05),
        regime_context={
            "current_state": "neutral",
            "states": _all_states(short_pts),
            "daily_ic": short_pts,
        },
    )
    assert dets[0].severity == LEVEL_CRITICAL

    # 桶均值为正（非弱区）→ 照常告警
    pos_pts = _series([0.05, 0.06, 0.04] * 6)
    dets = detect_model_anomaly(
        "m1",
        _stats(ic5=-0.05),
        regime_context={
            "current_state": "neutral",
            "states": _all_states(pos_pts),
            "daily_ic": pos_pts,
        },
    )
    assert dets[0].severity == LEVEL_CRITICAL


@pytest.mark.unit
def test_current_state_absent_from_join_is_not_weak():
    """当日状态是 bear，但序列全在 neutral 桶 → bear 桶 0 天，不构成弱区。"""
    points = _series([-0.05] * 18)
    dets = detect_model_anomaly(
        "m1",
        _stats(ic5=-0.05),
        regime_context={
            "current_state": "bear",
            "states": _all_states(points, "neutral"),
            "daily_ic": points,
        },
    )
    assert dets[0].severity == LEVEL_CRITICAL


@pytest.mark.unit
def test_attribute_only_touches_ic_drop_kind():
    price = Detection(kind="price_surge", subject="600036.SH", severity=LEVEL_WARN, title="t")
    points = _series([-0.05, -0.04, -0.06] * 6)
    out = attribute_ic_anomaly(
        price,
        current_state="neutral",
        states=_all_states(points),
        daily_ic=points,
    )
    assert out is price


# ── AnomalyEngine 接线 ──────────────────────────────────────────────


def _engine():
    from backend.services.engine.anomaly_engine import AnomalyEngine

    return AnomalyEngine(model_fetcher=lambda cfg: [], status_writer=lambda payload: None)


@pytest.mark.unit
def test_regime_context_helper_caches_per_market(monkeypatch):
    from backend.shared import regime_daily_store

    engine = _engine()
    calls: list[str] = []

    def _load(market, **kwargs):
        calls.append(market)
        return {"2026-10-09": "bear", "2026-10-08": "neutral"}

    monkeypatch.setattr(regime_daily_store, "load_states_sync", _load)
    cache: dict = {}
    row = {"daily_ic": _series([0.01, -0.02])}

    ctx1 = engine._regime_context_for("mdl_cn_train_x", row, cache, "2026-10-09")
    ctx2 = engine._regime_context_for("mdl_cn_train_y", row, cache, "2026-10-09")
    assert ctx1["current_state"] == "bear" and ctx1["states"]
    assert ctx2 is not None
    assert calls == ["CN"]  # 同市场只读一次库


@pytest.mark.unit
def test_regime_context_helper_honest_none_paths(monkeypatch):
    from backend.shared import regime_daily_store

    engine = _engine()
    monkeypatch.setattr(
        regime_daily_store, "load_states_sync", lambda market, **kw: {"2026-10-09": "bear"}
    )
    cache: dict = {}

    # 无日 IC 序列 → None（不读库）
    assert engine._regime_context_for("mdl_cn_x", {}, cache, "2026-10-09") is None
    # 表外市场（无 regime 指数口径）→ None
    assert (
        engine._regime_context_for("mdl_crypto_x", {"daily_ic": _series([0.01])}, cache, "2026-10-09")
        is None
    )
    # 当日无状态行 → current_state None（下游不降级）
    ctx = engine._regime_context_for("mdl_cn_x", {"daily_ic": _series([0.01])}, cache, "2026-01-01")
    assert ctx is not None and ctx["current_state"] is None

    # 读库失败 → None + 记 errors（不抛出，不缓存成功）
    def _boom(market, **kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(regime_daily_store, "load_states_sync", _boom)
    cache2: dict = {}
    assert (
        engine._regime_context_for("mdl_cn_x", {"daily_ic": _series([0.01])}, cache2, "2026-10-09")
        is None
    )
    assert engine.counters["errors"] >= 1
    assert "regime states CN" in engine.counters["last_error"]


# ── model_ic_monitor 序列形状 ───────────────────────────────────────


@pytest.mark.unit
def test_series_points_shape():
    import pandas as pd

    from backend.scripts.model_ic_monitor import _series_points

    ic = pd.Series(
        [0.1234567, -0.001], index=pd.to_datetime(["2026-09-01", "2026-09-02"])
    )
    assert _series_points(ic) == [
        {"date": "2026-09-01", "value": 0.123457},
        {"date": "2026-09-02", "value": -0.001},
    ]
