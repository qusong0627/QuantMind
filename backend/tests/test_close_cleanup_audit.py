"""收盘核对：跨日残留收口 + 直连 QMT 台账纳入核对。

2026-10-30 实测两类缺口：
- `orders` 里 10-29 的 ``mir-lld-...`` 委托至今仍是 submitted：A股委托当日有效，
  柜台随收盘作废，永远不会再有回报——没有任何任务收口；
- ``_qmt_channel_clause`` 只认 mir-/sltp-/flat-/trim- 前缀，直连单
  （``source='qmt_direct'``，cid 由调用方随意命名）根本不进收盘核对。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.dialects import postgresql

from backend.services.trade.services import close_cleanup_audit_task as audit


def _sql(clause) -> str:
    return str(
        clause.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    ).replace("%%", "%")


def test_cross_session_orphan_detects_previous_day_open() -> None:
    day_start = datetime(2026, 10, 30)
    assert (
        audit.is_cross_session_orphan(
            "submitted", datetime(2026, 10, 29, 10, 5), day_start
        )
        is True
    )
    assert (
        audit.is_cross_session_orphan(
            "partially_filled", datetime(2026, 10, 29, 14, 0), day_start
        )
        is True
    )


def test_cross_session_orphan_ignores_same_day_and_terminal() -> None:
    day_start = datetime(2026, 10, 30)
    assert (
        audit.is_cross_session_orphan(
            "submitted", datetime(2026, 10, 30, 9, 31), day_start
        )
        is False
    )
    assert (
        audit.is_cross_session_orphan(
            "filled", datetime(2026, 10, 29, 10, 5), day_start
        )
        is False
    )
    assert audit.is_cross_session_orphan("submitted", None, day_start) is False


def test_qmt_channel_clause_includes_direct_source() -> None:
    sql = _sql(audit._qmt_channel_clause())
    assert "source = 'qmt_direct'" in sql
    assert "client_order_id LIKE 'mir-%'" in sql


def test_classify_close_state_flags_missing_counter_order() -> None:
    result = audit.classify_close_state(
        counter_orders=[],
        local_orders=[{"status": "submitted", "exchange_order_id": "10236"}],
    )
    assert result["ok"] is False
    assert result["local_stale"][0]["reason"] == "counter_order_missing"


def test_classify_close_state_flags_counter_terminal() -> None:
    result = audit.classify_close_state(
        counter_orders=[{"order_id": "10236", "status": "CANCELLED"}],
        local_orders=[{"status": "submitted", "exchange_order_id": "10236"}],
    )
    assert result["local_stale"][0]["reason"] == "counter_terminal"
