"""T-P3-01 测试：策略生命周期状态机（唯一实现 shared/strategy_lifecycle.py）。

口径（设计文档 §4.2 Strategy Spec）：DRAFT→VERIFIED→SIM→LIVE，ARCHIVED 为软删；
SIM 入口须 VERIFIED（回测证据）；LIVE 入口须 SIM（晋级必须有模拟证据，
门槛细则见 T-P3-05）；版本/参数锁 = 运行中（SIM/LIVE）策略改参数必须显式升版本。
存量词表归一：ACTIVE/REPOSITORY→VERIFIED、LIVE_TRADING→LIVE。
"""

from __future__ import annotations

import pytest

from backend.shared.strategy_lifecycle import (
    STATUS_ARCHIVED,
    STATUS_DRAFT,
    STATUS_LIVE,
    STATUS_SIM,
    STATUS_VERIFIED,
    IllegalTransitionError,
    assert_transition,
    can_start,
    can_transition,
    is_running,
    normalize_status,
    requires_version_bump,
)


# ── 词表归一（存量兼容） ─────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("DRAFT", STATUS_DRAFT),
        ("draft", STATUS_DRAFT),
        ("ACTIVE", STATUS_VERIFIED),  # 存量 88 行主词表
        ("active", STATUS_VERIFIED),
        ("repository", STATUS_VERIFIED),
        ("VERIFIED", STATUS_VERIFIED),
        ("SIM", STATUS_SIM),
        ("simulation", STATUS_SIM),
        ("LIVE", STATUS_LIVE),
        ("LIVE_TRADING", STATUS_LIVE),  # 存量旧词表
        ("live_trading", STATUS_LIVE),
        ("ARCHIVED", STATUS_ARCHIVED),
        ("archive", STATUS_ARCHIVED),
        (None, STATUS_DRAFT),
        ("", STATUS_DRAFT),
        ("wat", STATUS_DRAFT),  # 未知值保守回落 DRAFT（不炸存量写入）
    ],
)
def test_normalize_status(raw, expected):
    assert normalize_status(raw) == expected


# ── 迁移合法性 ──────────────────────────────────────────────────────


@pytest.mark.unit
def test_legal_transitions():
    assert can_transition(STATUS_DRAFT, STATUS_VERIFIED)
    assert can_transition(STATUS_DRAFT, STATUS_ARCHIVED)
    assert can_transition(STATUS_VERIFIED, STATUS_SIM)
    assert can_transition(STATUS_SIM, STATUS_LIVE)
    assert can_transition(STATUS_SIM, STATUS_VERIFIED)
    assert can_transition(STATUS_LIVE, STATUS_VERIFIED)
    assert can_transition(STATUS_ARCHIVED, STATUS_VERIFIED)
    # 幂等（同状态回写=no-op 合法）
    assert can_transition(STATUS_SIM, STATUS_SIM)


@pytest.mark.unit
def test_illegal_transitions():
    # 晋级必须逐级：跨级/回退均非法
    assert not can_transition(STATUS_DRAFT, STATUS_SIM)
    assert not can_transition(STATUS_DRAFT, STATUS_LIVE)
    assert not can_transition(STATUS_VERIFIED, STATUS_LIVE)
    assert not can_transition(STATUS_LIVE, STATUS_SIM)
    assert not can_transition(STATUS_ARCHIVED, STATUS_SIM)


@pytest.mark.unit
def test_assert_transition_raises_with_context():
    with pytest.raises(IllegalTransitionError) as exc:
        assert_transition(STATUS_DRAFT, STATUS_LIVE)
    msg = str(exc.value)
    assert "DRAFT" in msg and "LIVE" in msg
    # 合法迁移不抛
    assert_transition(STATUS_VERIFIED, STATUS_SIM)


# ── 启动门禁 ────────────────────────────────────────────────────────


@pytest.mark.unit
def test_can_start_simulation_requires_verified():
    ok, reason = can_start(STATUS_DRAFT, "SIMULATION")
    assert ok is False and "验证" in reason
    ok, _ = can_start(STATUS_VERIFIED, "SIMULATION")
    assert ok is True
    ok, _ = can_start(STATUS_SIM, "SIMULATION")  # 重复启动幂等
    assert ok is True
    ok, reason = can_start(STATUS_LIVE, "SIMULATION")
    assert ok is False and reason
    ok, _ = can_start(STATUS_ARCHIVED, "SIMULATION")
    assert ok is False


@pytest.mark.unit
def test_can_start_real_requires_sim():
    ok, reason = can_start(STATUS_SIM, "REAL")
    assert ok is True
    ok, reason = can_start(STATUS_VERIFIED, "REAL")
    assert ok is False and "模拟" in reason
    ok, reason = can_start(STATUS_DRAFT, "REAL")
    assert ok is False
    ok, _ = can_start(STATUS_LIVE, "REAL")  # 已在实盘，重复启动幂等
    assert ok is True


# ── 运行态/参数锁 ───────────────────────────────────────────────────


@pytest.mark.unit
def test_is_running_and_param_lock():
    assert not is_running(STATUS_DRAFT)
    assert not is_running(STATUS_VERIFIED)
    assert is_running(STATUS_SIM)
    assert is_running(STATUS_LIVE)
    assert not requires_version_bump(STATUS_DRAFT)
    assert not requires_version_bump(STATUS_VERIFIED)
    assert requires_version_bump(STATUS_SIM)
    assert requires_version_bump(STATUS_LIVE)
    # 存量词表同样生效
    assert requires_version_bump("LIVE_TRADING")
    assert is_running("active") is False
