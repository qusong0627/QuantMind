"""QuantDB 同步预算闸（C2）测试。"""

from __future__ import annotations

from backend.services.engine.data_platform import quantdb_budget as b

GB = 1024**3


def test_zero_bytes_is_ok():
    r = b.evaluate_budget(new_bytes=0, remaining_gb=0, budget_gb=1)
    assert r["ok"] is True
    assert r["estimated_gb"] == 0


def test_daily_budget_blocks_oversize():
    r = b.evaluate_budget(new_bytes=2 * GB, remaining_gb=100, budget_gb=1)
    assert r["ok"] is False
    assert any("日预算" in reason for reason in r["reasons"])


def test_remaining_quota_blocks():
    r = b.evaluate_budget(new_bytes=2 * GB, remaining_gb=1.5, budget_gb=0)
    assert r["ok"] is False
    assert any("账户剩余" in reason for reason in r["reasons"])


def test_force_bypasses_with_audit_flag():
    r = b.evaluate_budget(
        new_bytes=2 * GB, remaining_gb=1.5, budget_gb=1, force=True
    )
    assert r["ok"] is True
    assert r["forced"] is True
    assert r["reasons"]


def test_safety_ratio_inflates_estimate():
    r = b.evaluate_budget(
        new_bytes=1 * GB, remaining_gb=None, budget_gb=0, safety_ratio=1.10
    )
    assert r["raw_gb"] == 1.0
    assert r["estimated_gb"] == 1.1
    assert r["ok"] is True


def test_unknown_remaining_only_checks_budget():
    r = b.evaluate_budget(new_bytes=2 * GB, remaining_gb=None, budget_gb=3)
    assert r["ok"] is True
