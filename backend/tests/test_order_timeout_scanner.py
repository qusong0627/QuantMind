"""悬挂订单扫描器：托管委托识别（Phase 2.4）。

镜像真单（``mirror:`` 备注 / ``mir-`` client_order_id）与通达信桥委托的真实状态由
桥/QMT 轮询器回报，本地超时启发式不得越权判死（否则「柜台还挂着、本地已 EXPIRED」）。
"""

from __future__ import annotations

from sqlalchemy.dialects import postgresql

from backend.services.trade.services import order_timeout_scanner as scanner


def _sql(clause) -> str:
    text = str(
        clause.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    # psycopg 参数风格把 LIKE 的 % 转义成 %%，这里还原成可读口径
    return text.replace("%%", "%")


class TestBrokerManagedPredicate:
    def test_mirror_remark_prefix(self) -> None:
        assert scanner.is_broker_managed("mirror:600036.SH", None) is True

    def test_tdx_bridge_remark(self) -> None:
        assert scanner.is_broker_managed("通达信桥委托 12345", None) is True
        assert (
            scanner.is_broker_managed("[AWAITING_BRIDGE_ACK] 通达信桥委托", "x") is True
        )

    def test_mirror_cid_prefix_fallback(self) -> None:
        # 备注被成交回报覆盖后，仍按 client_order_id 识别
        assert scanner.is_broker_managed("部分成交 100 股", "mir-abc123") is True

    def test_ordinary_order_not_managed(self) -> None:
        assert scanner.is_broker_managed("普通委托", "cid-1") is False
        assert scanner.is_broker_managed(None, None) is False
        assert scanner.is_broker_managed("", "") is False

    def test_not_fooled_by_middle_text(self) -> None:
        # 仅前缀/包含口径命中，普通备注里的 "mirror" 字样不误伤
        assert scanner.is_broker_managed("user mirror test", "cid-2") is False


class TestSqlClauses:
    def test_managed_clause_covers_remark_and_cid(self) -> None:
        sql = _sql(scanner._broker_managed_clause())
        assert "remarks LIKE 'mirror:%'" in sql
        assert "client_order_id LIKE 'mir-%'" in sql

    def test_not_managed_clause_is_null_safe(self) -> None:
        sql = _sql(scanner._not_broker_managed_clause()[0])
        assert "remarks IS NULL" in sql
        assert (
            "NOT (remarks LIKE 'mirror:%')" in sql
            or "remarks NOT LIKE 'mirror:%'" in sql
        )

    def test_not_managed_cid_clause_null_safe(self) -> None:
        clauses = scanner._not_broker_managed_clause()
        cid_sql = next(_sql(c) for c in clauses if "client_order_id" in _sql(c))
        assert "client_order_id IS NULL" in cid_sql
        assert "mir-%" in cid_sql


class TestDirectQmtSourceManaged:
    """直连 QMT 台账（source='qmt_direct'）必须算托管，否则挂单超 30 分钟被本地判死。"""

    def test_direct_source_is_broker_managed(self) -> None:
        assert scanner.is_broker_managed(None, None, source="qmt_direct") is True
        assert (
            scanner.is_broker_managed("qm-a1b2c3", "restore4-123", source="qmt_direct")
            is True
        )

    def test_other_source_is_not_managed(self) -> None:
        assert scanner.is_broker_managed("普通委托", "cid-1", source="tdx") is False
        assert scanner.is_broker_managed(None, None, source=None) is False

    def test_managed_clause_covers_source(self) -> None:
        sql = _sql(scanner._broker_managed_clause())
        assert "source IN ('qmt_direct')" in sql

    def test_not_managed_clause_excludes_source(self) -> None:
        joined = " ".join(_sql(c) for c in scanner._not_broker_managed_clause())
        assert "source IS NULL" in joined
        assert "qmt_direct" in joined
