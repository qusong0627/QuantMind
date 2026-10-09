"""P3 §6.3 分桶评估：纯函数分桶统计 + 模型卡「状态依赖」区块接线。

- 单元（无 DB）：桶统计/弱区谓词（唯一实现）/月间统计/覆盖计数/诚实缺省；
  ``_regime_dependency`` 的三条降级路径（无指数口径 / 无序列 / 时间线不可读）；
  ``score_model`` 注入：区块进 ``inputs_version.regime_dependency``、侧车载荷
  带 ``regime`` 键（save_series 打桩捕获）；
- 集成（真库）：``load_states_sync`` 直读 + 真实状态 join 覆盖计数。
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from backend.shared.regime_buckets import (
    MIN_BUCKET_DAYS,
    bucket_of,
    compute_regime_block,
    is_weak_bucket,
    monthly_summary,
    within_expectation,
)


def _pts(pairs: list[tuple[str, float]]) -> list[dict]:
    return [{"date": d, "value": v} for d, v in pairs]


def _days(start: str, n: int) -> list[str]:
    d0 = date.fromisoformat(start)
    return [(d0 + timedelta(days=i)).isoformat() for i in range(n)]


# ── 纯函数 ──────────────────────────────────────────────────────────


@pytest.mark.unit
def test_bucket_stats_and_weak_predicate():
    days = _days("2026-01-05", MIN_BUCKET_DAYS)
    states = dict.fromkeys(days, "neutral")
    ic = _pts([(d, -0.05 if i % 2 == 0 else -0.01) for i, d in enumerate(days)])

    stats = bucket_of(ic, states, "neutral")
    assert stats["n_days"] == MIN_BUCKET_DAYS
    assert stats["mean_ic"] is not None and stats["mean_ic"] < 0
    assert stats["hit_rate"] == 0.0
    assert stats["weak"] is True  # IC≤0 且满 15 天

    # 差一天不够 15 → 不算弱区（样本不足不下结论）
    assert is_weak_bucket({"n_days": MIN_BUCKET_DAYS - 1, "mean_ic": -0.05}) is False
    # 均值为正 → 不是弱区
    assert is_weak_bucket({"n_days": 30, "mean_ic": 0.01}) is False
    # 恰好 0 → 弱区（≤0）
    assert is_weak_bucket({"n_days": 15, "mean_ic": 0.0}) is True
    # 无值 → 不是弱区
    assert is_weak_bucket({"n_days": 30, "mean_ic": None}) is False


@pytest.mark.unit
def test_bucket_hit_rate_and_empty_bucket():
    days = _days("2026-02-02", 4)
    states = {days[0]: "bull", days[1]: "bull", days[2]: "bear", days[3]: "bull"}
    ic = _pts([(days[0], 0.1), (days[1], -0.1), (days[2], -0.2), (days[3], 0.2)])

    bull = bucket_of(ic, states, "bull")
    assert bull["n_days"] == 3 and bull["hit_rate"] == pytest.approx(2 / 3, abs=1e-4)
    bear = bucket_of(ic, states, "bear")
    assert bear["n_days"] == 1 and bear["hit_rate"] == 0.0
    # 未 join 任何桶的日期不计入
    assert bucket_of(ic, {}, "bull")["n_days"] == 0
    assert bucket_of(ic, {}, "bull")["mean_ic"] is None


@pytest.mark.unit
def test_monthly_summary_worst_and_inter_month_std():
    ic = _pts(
        [("2026-01-05", 0.10), ("2026-01-06", 0.20)]  # 一月均值 0.15
        + [("2026-02-03", -0.30), ("2026-02-04", -0.10)]  # 二月均值 -0.20（最差）
        + [("2026-03-02", 0.05)]  # 三月均值 0.05
    )
    monthly = monthly_summary(ic)
    assert [m["month"] for m in monthly["months"]] == ["2026-01", "2026-02", "2026-03"]
    assert monthly["worst_month"] == {"month": "2026-02", "mean_ic": -0.2, "n_days": 2}
    # 样本 std([0.15, -0.2, 0.05], ddof=1) = sqrt(0.0325) ≈ 0.180277
    assert monthly["month_std"] == pytest.approx(0.180278, abs=1e-6)
    assert monthly["n_months"] == 3
    # 单月 → std 不可算（None，不是 0）
    assert monthly_summary(_pts([("2026-01-05", 0.1)]))["month_std"] is None


@pytest.mark.unit
def test_compute_regime_block_coverage_and_notes():
    days = _days("2026-01-05", 20)
    states = dict.fromkeys(days[:16], "neutral")  # 16 天有状态，4 天缺
    ic = _pts([(d, -0.02) for d in days])
    block = compute_regime_block(ic, states, market="CN", index="000300.SH")

    assert block["coverage"] == {
        "ic_days": 20,
        "joined_days": 16,
        "missing_regime_days": 4,
        "regime_rows": 16,
        "first_ic_date": days[0],
        "last_ic_date": days[-1],
    }
    assert block["weak_buckets"] == ["neutral"]
    assert block["buckets"]["bull"]["n_days"] == 0
    assert any("无 regime 行" in n for n in block["notes"])
    assert any("空桶" in n for n in block["notes"])
    assert any("弱区标注" in n for n in block["notes"])
    assert "最差月与月间 std 优先" in block["display_note"]


@pytest.mark.unit
def test_within_expectation_needs_distribution():
    stats = {"n_days": 30, "mean_ic": -0.05, "ic_std": 0.02}
    assert within_expectation(-0.06, stats) is True  # |−0.06+0.05|=0.01 ≤ 0.04
    assert within_expectation(-0.10, stats) is False  # 0.05 > 0.04
    # 样本不足：证不出「在期望内」→ False（不降级）
    assert within_expectation(-0.06, {"n_days": 30, "mean_ic": -0.05, "ic_std": None}) is False
    assert within_expectation(None, stats) is False


# ── _regime_dependency 降级路径与注入 ───────────────────────────────


@pytest.mark.unit
def test_regime_dependency_unsupported_market_and_no_series():
    from backend.scripts.eval import model_card

    crypto = model_card._regime_dependency("mdl_crypto_x", {"market": "CRYPTO"}, None)
    assert crypto["available"] is False and "无 regime 指数口径" in crypto["reason"]

    # CN 但无序列（非 pred 一级证据）→ 缺省且不碰 DB（若碰了会因无 DB 而异常）
    none_series = model_card._regime_dependency("m1", {}, None)
    assert none_series["available"] is False
    assert "无逐日 IC 序列" in none_series["reason"]


@pytest.mark.unit
def test_regime_dependency_with_states(monkeypatch):
    from backend.scripts.eval import model_card
    from backend.shared import regime_daily_store

    days = _days("2026-01-05", 18)
    states = dict.fromkeys(days, "bull")
    monkeypatch.setattr(regime_daily_store, "load_states_sync", lambda market, **kw: states)
    payload = {"series": {"daily_ic": _pts([(d, 0.05) for d in days])}}

    block = model_card._regime_dependency("m1", {}, payload)
    assert block["available"] is True
    assert block["market"] == "CN" and block["index"] == "000300.SH"
    assert block["buckets"]["bull"]["n_days"] == 18

    # 时间线不可读 → 缺省（评分本身不受影响）
    def _boom(market, **kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(regime_daily_store, "load_states_sync", _boom)
    broken = model_card._regime_dependency("m1", {}, payload)
    assert broken["available"] is False and "不可读" in broken["reason"]


@pytest.mark.unit
def test_score_model_attaches_block_and_sidecar(tmp_path, monkeypatch):
    """接线：区块进 inputs_version、侧车载荷带 regime 键（打桩捕获写盘）。"""
    import json as _json

    from backend.scripts.eval import model_card
    from backend.scripts.eval.model_realized import DIM_ORDER
    from backend.shared import regime_daily_store
    from backend.shared.eval_scoring import DimensionScore

    monkeypatch.setattr(model_card, "PRODUCTION_DIR", tmp_path)
    (tmp_path / "m1").mkdir()
    (tmp_path / "m1" / "metadata.json").write_text(
        _json.dumps({"metrics": {"test_rank_ic": 0.08, "test_rank_icir": 1.0}}),
        encoding="utf-8",
    )
    days = _days("2026-01-05", 16)
    states = dict.fromkeys(days, "bear")
    monkeypatch.setattr(regime_daily_store, "load_states_sync", lambda market, **kw: states)

    stub_dims = {
        key: DimensionScore(key, key, 1.0, None, False, {"insufficient": True})
        for key in DIM_ORDER
    }
    series_payload = {
        "series": {"daily_ic": _pts([(d, -0.03) for d in days])},
        "scalars": {},
        "notes": {},
    }
    monkeypatch.setattr(
        model_card,
        "resolve_dims",
        lambda *a, **kw: (stub_dims, {"tier": "pred", "series": series_payload}),
    )
    captured: dict = {}

    def _fake_save(object_type, object_id, payload):
        captured["payload"] = payload
        return {"written": True, "bytes": 1, "note": ""}

    monkeypatch.setattr(model_card, "save_series", _fake_save)

    result = model_card.score_model("m1")
    block = result["inputs_version"]["regime_dependency"]
    assert block["available"] is True
    assert block["weak_buckets"] == ["bear"]  # 16 天 IC<0
    assert captured["payload"]["regime"] == block  # 侧车载荷同块
    # 渲染：最差月/月间 std/弱区行都在
    text = model_card.render_card(result)
    assert "状态依赖" in text and "月间 std" in text and "弱区" in text


# ── 集成：真库直读 + join ───────────────────────────────────────────


@pytest.mark.integration
def test_load_states_sync_joins_real_timeline():
    from backend.shared.regime_daily_store import load_states_sync
    from backend.shared.regime_buckets import compute_regime_block

    states = load_states_sync("CN")
    assert len(states) > 1000, "CN 时间线应已回填（regime_backfill.py --market CN --from 2018-01-01）"
    real_days = sorted(states)[-40:]
    ic = _pts([(d, 0.01 if i % 2 == 0 else -0.01) for i, d in enumerate(real_days)])
    block = compute_regime_block(ic, states, market="CN", index="000300.SH")
    assert block["coverage"]["joined_days"] == 40
    assert block["coverage"]["missing_regime_days"] == 0
    assert sum(b["n_days"] for b in block["buckets"].values()) == 40
