"""TDX 委托回报单调合并判定。"""

from __future__ import annotations

from backend.services.live_trading.services.tdx_order_merge import (
    should_apply_order_update,
)


def test_terminal_is_not_overwritten_by_non_terminal():
    assert should_apply_order_update("filled", 100, "submitted", 100) is False
    assert should_apply_order_update("cancelled", 0, "submitted", 0) is False
    assert should_apply_order_update("rejected", 0, "pending", 0) is False


def test_filled_quantity_never_regresses():
    assert should_apply_order_update("partially_filled", 500, "partially_filled", 400) is False
    assert should_apply_order_update("filled", 1000, "filled", 900) is False


def test_status_never_regresses_before_terminal():
    assert should_apply_order_update("partially_filled", 100, "submitted", 100) is False
    assert should_apply_order_update("partially_filled", 100, "pending", 100) is False


def test_forward_progress_is_applied():
    assert should_apply_order_update("submitted", 0, "partially_filled", 100) is True
    assert should_apply_order_update("partially_filled", 100, "filled", 100) is True
    assert should_apply_order_update("pending", 0, "submitted", 0) is True


def test_late_fill_can_update_terminal_when_quantity_grows():
    assert should_apply_order_update("partial_cancelled", 100, "filled", 200) is True
    assert should_apply_order_update("filled", 100, "partially_filled", 200) is True


def test_idempotent_terminal_replay_is_allowed_but_harmless():
    assert should_apply_order_update("filled", 100, "filled", 100) is True
