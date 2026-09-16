"""T-P4-01 测试：Scanner SPI（shared/scanner_spi.py）——机会对象/合并去重/注册表。

口径（设计《行情扫描与机会发现》§二/§三）：
- 扫描器**注册即生效**、独立开关、纯函数式（不碰账本不下单）；
- 机会合并：同标的并 sources、多源共振加分；同源 N 日冷却不重复报；过期出池；
- score 0..100 跨扫描器可比；strength 0..1 扫描器内强度。
"""

from __future__ import annotations

import pytest

from backend.shared.scanner_spi import (
    MAX_SCORE,
    Opportunity,
    RESONANCE_BONUS,
    SCANNERS,
    is_expired,
    merge_opportunities,
    scanner_spec,
    scanner_switch_enabled,
)


def _opp(
    symbol="600036.SH",
    source="model_signal",
    score=80,
    ts="2026-09-16T15:30:00+08:00",
    **kw,
):
    return Opportunity(
        symbol=symbol,
        sources=(source,),
        strength=kw.pop("strength", 0.9),
        score=score,
        ts=ts,
        evidence=kw.pop("evidence", {}),
        **kw,
    )


# ── 机会对象 ────────────────────────────────────────────────────────


@pytest.mark.unit
def test_opportunity_defaults_and_frozen():
    from dataclasses import FrozenInstanceError

    o = Opportunity(symbol="600036.SH")
    assert o.market == "CN" and o.state == "watch" and o.score == 0
    with pytest.raises(FrozenInstanceError):
        o.symbol = "x"  # frozen


# ── 合并/去重/共振 ──────────────────────────────────────────────────


@pytest.mark.unit
def test_merge_single_source_passthrough_and_score_clamp():
    merged = merge_opportunities([_opp(score=80)])
    assert len(merged) == 1
    assert merged[0].sources == ("model_signal",)
    assert merged[0].score == 80
    assert merge_opportunities([_opp(score=MAX_SCORE)])[0].score == MAX_SCORE


@pytest.mark.unit
def test_merge_resonance_bonus_two_sources():
    items = [
        _opp(symbol="600036.SH", source="model_signal", score=80),
        _opp(symbol="600036.SH", source="pattern:duofangpao", score=70, strength=0.5),
    ]
    merged = merge_opportunities(items)
    assert len(merged) == 1
    o = merged[0]
    assert set(o.sources) == {"model_signal", "pattern:duofangpao"}
    assert o.score == min(MAX_SCORE, 80 + RESONANCE_BONUS)  # max 分 + 共振加分
    assert o.strength == pytest.approx(0.9)  # 取最强源
    assert "resonance" in o.evidence


@pytest.mark.unit
def test_merge_resonance_bonus_capped_at_100():
    items = [
        _opp(source="a", score=98),
        _opp(source="b", score=60),
        _opp(source="c", score=50),
    ]
    merged = merge_opportunities(items)
    assert merged[0].score == MAX_SCORE


@pytest.mark.unit
def test_merge_cooldown_suppresses_recent_same_source():
    prior = [
        _opp(symbol="600036.SH", source="model_signal", ts="2026-09-16T15:30:00+08:00")
    ]
    today = [
        _opp(symbol="600036.SH", source="model_signal", ts="2026-09-17T15:30:00+08:00")
    ]
    merged = merge_opportunities(
        today, prior=prior, cooldown_days=2, as_of="2026-09-17T16:00:00+08:00"
    )
    assert merged == []  # 同源冷却中

    # 冷却外（3 天前）→ 正常报
    prior_old = [
        _opp(symbol="600036.SH", source="model_signal", ts="2026-09-13T15:30:00+08:00")
    ]
    merged2 = merge_opportunities(
        today, prior=prior_old, cooldown_days=2, as_of="2026-09-17T16:00:00+08:00"
    )
    assert len(merged2) == 1


@pytest.mark.unit
def test_merge_cooldown_only_suppresses_that_source():
    prior = [
        _opp(symbol="600036.SH", source="model_signal", ts="2026-09-16T15:30:00+08:00")
    ]
    today = [
        _opp(
            symbol="600036.SH",
            source="model_signal",
            ts="2026-09-17T15:30:00+08:00",
            score=80,
        ),
        _opp(
            symbol="600036.SH",
            source="pattern:duofangpao",
            ts="2026-09-17T15:30:00+08:00",
            score=70,
        ),
    ]
    merged = merge_opportunities(
        today, prior=prior, cooldown_days=2, as_of="2026-09-17T16:00:00+08:00"
    )
    assert len(merged) == 1
    assert merged[0].sources == ("pattern:duofangpao",)  # 仅冷却源被剔除
    assert merged[0].score == 70


@pytest.mark.unit
def test_merge_sorts_by_score_desc():
    merged = merge_opportunities(
        [
            _opp(symbol="000001.SZ", source="a", score=60),
            _opp(symbol="600036.SH", source="a", score=90),
        ]
    )
    assert [o.symbol for o in merged] == ["600036.SH", "000001.SZ"]


# ── 过期 ────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_is_expired():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Asia/Shanghai")
    o = _opp(expiry="2026-09-17T15:30:00+08:00")
    assert is_expired(o, datetime(2026, 9, 17, 15, 0, tzinfo=tz)) is False
    assert is_expired(o, datetime(2026, 9, 17, 16, 0, tzinfo=tz)) is True
    # 无 expiry 视为不过期（由调用方决定生命周期）
    assert is_expired(_opp(expiry=""), datetime(2026, 9, 17, 16, 0, tzinfo=tz)) is False


# ── 注册表 ──────────────────────────────────────────────────────────


@pytest.mark.unit
def test_scanner_registry_invariants():
    ids = [s.id for s in SCANNERS]
    assert len(ids) == len(set(ids)), "扫描器 id 必须唯一"
    assert "model_signal" in ids, "v1 必须含模型信号扫描器"
    for spec in SCANNERS:
        assert spec.name and spec.frequency and spec.scope


@pytest.mark.unit
def test_scanner_switch_env():
    spec = scanner_spec("model_signal")
    assert spec is not None
    assert scanner_switch_enabled(spec, env={}) is spec.enabled_default
    if spec.switch_env:
        assert scanner_switch_enabled(spec, env={spec.switch_env: "0"}) is False
        assert scanner_switch_enabled(spec, env={spec.switch_env: "true"}) is True
