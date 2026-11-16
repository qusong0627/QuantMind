"""收盘核对：跨日残留收口 + 直连 QMT 台账纳入核对。

2026-10-30 实测两类缺口：
- `orders` 里 10-29 的 ``mir-lld-...`` 委托至今仍是 submitted：A股委托当日有效，
  柜台随收盘作废，永远不会再有回报——没有任何任务收口；
- ``_qmt_channel_clause`` 只认 mir-/sltp-/flat-/trim- 前缀，直连单
  （``source='qmt_direct'``，cid 由调用方随意命名）根本不进收盘核对。
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace

from sqlalchemy.dialects import postgresql

from backend.services.trade.services import close_cleanup_audit_task as audit
from backend.services.trade_shared.models.enums import OrderStatus


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


def test_cross_session_unverified_never_mutates() -> None:
    action, target = audit.classify_cross_session_order(
        status="submitted",
        exchange_order_id="10236",
        counter_orders=[],
        counter_ok=False,
    )
    assert action == "unverified" and target is None


def test_cross_session_counter_terminal_maps_local_status() -> None:
    action, target = audit.classify_cross_session_order(
        status="submitted",
        exchange_order_id="10236",
        counter_orders=[{"order_id": "10236", "status": "FILLED"}],
        counter_ok=True,
    )
    assert action == "terminal" and target == "filled"


def test_cross_session_counter_missing_only_then_expires() -> None:
    action, target = audit.classify_cross_session_order(
        status="submitted",
        exchange_order_id="10236",
        counter_orders=[],
        counter_ok=True,
    )
    assert action == "expire" and target is None


def test_cross_session_counter_open_is_kept() -> None:
    action, target = audit.classify_cross_session_order(
        status="submitted",
        exchange_order_id="10236",
        counter_orders=[{"order_id": "10236", "status": "SUBMITTED"}],
        counter_ok=True,
    )
    assert action == "keep_open" and target == "SUBMITTED"


class _Result:
    def __init__(self, rows):
        self._rows = list(rows)

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _Session:
    def __init__(self, rows):
        self.rows = list(rows)
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, _stmt):
        return _Result(self.rows)

    async def commit(self):
        self.commits += 1


def _order(**over):
    base = {
        "status": OrderStatus.SUBMITTED,
        "submitted_at": datetime(2026, 10, 29, 10, 0),
        "exchange_order_id": "10236",
        "client_order_id": "mir-1",
        "symbol": "600036.SH",
        "remarks": "",
    }
    base.update(over)
    return SimpleNamespace(**base)


def test_expire_does_not_mutate_when_counter_unavailable(monkeypatch) -> None:
    order = _order()
    session = _Session([order])

    async def _run():
        from backend.shared import database_manager_v2
        monkeypatch.setattr(database_manager_v2, "get_session",
                            lambda **kwargs: session)
        return await audit.expire_cross_session_orphans(
            "20261030", counter_orders=[], counter_ok=False)

    result = asyncio.run(_run())
    assert result["unverified"]
    assert order.status == OrderStatus.SUBMITTED
    assert session.commits == 0


def test_expire_marks_only_counter_missing(monkeypatch) -> None:
    order = _order()
    session = _Session([order])

    async def _run():
        from backend.shared import database_manager_v2
        monkeypatch.setattr(database_manager_v2, "get_session",
                            lambda **kwargs: session)
        return await audit.expire_cross_session_orphans(
            "20261030", counter_orders=[], counter_ok=True)

    result = asyncio.run(_run())
    assert result["expired"]
    assert order.status == OrderStatus.EXPIRED
    assert session.commits == 1


def test_expire_follows_counter_terminal(monkeypatch) -> None:
    order = _order()
    session = _Session([order])

    async def _run():
        from backend.shared import database_manager_v2
        monkeypatch.setattr(database_manager_v2, "get_session",
                            lambda **kwargs: session)
        return await audit.expire_cross_session_orphans(
            "20261030",
            counter_orders=[{"order_id": "10236", "status": "FILLED"}],
            counter_ok=True,
        )

    result = asyncio.run(_run())
    assert result["resolved"][0]["status"] == "filled"
    assert order.status == OrderStatus.FILLED
    assert session.commits == 1
