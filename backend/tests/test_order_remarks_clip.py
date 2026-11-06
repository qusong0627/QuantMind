"""orders.remarks(varchar 500) 截断：超长拒因不得撑爆列、不得毒化 Session。

2026-11-06：QMT 返回的长拒因（>500 字）触发 StringDataRightTruncationError，
整个 Session 变成 PendingRollback，订单落库全断。
"""
from __future__ import annotations

from backend.services.trade_shared.services.order_service import (
    REMARKS_MAX,
    _clip_remarks,
)


def test_short_text_untouched():
    assert _clip_remarks("ok") == "ok"
    assert _clip_remarks(None) == ""


def test_long_text_clipped_to_column_width():
    text = "x" * (REMARKS_MAX + 200)
    out = _clip_remarks(text)
    assert len(out) == REMARKS_MAX
    assert out.endswith("…")
