"""双轨对账 + 收盘核对 + 镜像跳过记录 单测（无真库/真机）。

覆盖 Phase 3.1/3.3：
* ``dual_book_reconciliation_task.build_reconciliation_report``（纯函数）
* ``close_cleanup_audit_task.classify_close_state``（纯函数）
* ``real_mirror_service.record_skip / load_skips``（Redis 哈希）
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from backend.services.live_trading.services import real_mirror_service as mirror
from backend.services.trade.services import (
    close_cleanup_audit_task as audit,
    dual_book_reconciliation_task as reconcile,
)


# --------------------------------------------------------------------------
# 替身
# --------------------------------------------------------------------------
class FakePipeline:
    def __init__(self, client: FakeRedisClient) -> None:
        self.client = client
        self.ops: list[tuple] = []

    def hincrby(self, key, field, amount):
        self.ops.append(("hincrby", key, field, amount))
        return self

    def hset(self, key, field, value):
        self.ops.append(("hset", key, field, value))
        return self

    def expire(self, key, ttl):
        self.ops.append(("expire", key, ttl))
        return self

    def execute(self):
        for op in self.ops:
            if op[0] == "hincrby":
                _, key, field, amount = op
                store = self.client.hashes.setdefault(key, {})
                store[field] = int(store.get(field, 0)) + int(amount)
            elif op[0] == "hset":
                _, key, field, value = op
                self.client.hashes.setdefault(key, {})[field] = value
        self.ops = []
        return []


class FakeRedisClient:
    def __init__(self) -> None:
        self.hashes: dict[str, dict] = {}
        self.ttls: dict[str, int] = {}

    def pipeline(self) -> FakePipeline:
        return FakePipeline(self)

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))


class FakeRedis:
    def __init__(self) -> None:
        self.client = FakeRedisClient()


# --------------------------------------------------------------------------
# 镜像跳过记录
# --------------------------------------------------------------------------
class TestSkipRecording:
    def test_record_and_load_roundtrip(self) -> None:
        redis = FakeRedis()
        mirror.record_skip(
            redis, symbol="600036.SH", side="SELL", quantity=100, reason="price_drift", source="t"
        )
        mirror.record_skip(
            redis, symbol="600036.SH", side="SELL", quantity=100, reason="price_drift", source="t"
        )
        mirror.record_skip(
            redis, symbol="000001.SZ", side="BUY", quantity=200, reason="whitelist", source="t"
        )
        counts = mirror.load_skips(redis, "20260911")
        assert counts["600036.SH:price_drift"] == 2
        assert counts["000001.SZ:whitelist"] == 1
        # detail 字段不参与计数
        assert all(not key.endswith(":detail") for key in counts)

    def test_record_skip_tolerates_broken_client(self) -> None:
        class Broken:
            class client:  # noqa: N801
                @staticmethod
                def pipeline():
                    raise RuntimeError("redis down")

        mirror.record_skip(
            Broken, symbol="600036.SH", side="SELL", quantity=1, reason="x", source="t"
        )  # 不抛异常
        assert mirror.load_skips(FakeRedis(), "20260911") == {}


# --------------------------------------------------------------------------
# 双轨对账
# --------------------------------------------------------------------------
class TestReconciliationReport:
    def test_matched_books_ok(self) -> None:
        report = reconcile.build_reconciliation_report(
            date_str="20260911",
            sim_rows=[("600036.SH", "BUY", 100), ("000001.SZ", "SELL", 200)],
            real_rows=[("600036.SH", "BUY", 100), ("000001.SZ", "SELL", 200)],
            skips={},
        )
        assert report["ok"] is True
        assert report["diffs"] == []

    def test_shortfall_explained_by_skip(self) -> None:
        report = reconcile.build_reconciliation_report(
            date_str="20260911",
            sim_rows=[("600036.SH", "SELL", 1000)],
            real_rows=[],
            skips={"600036.SH:price_drift": 1},
        )
        assert report["ok"] is True
        diff = report["diffs"][0]
        assert diff["kind"] == "shortfall"
        assert diff["explained"] is True
        assert diff["delta"] == -1000
        assert diff["skip_reasons"] == {"price_drift": 1}

    def test_shortfall_without_skip_is_unexplained(self) -> None:
        report = reconcile.build_reconciliation_report(
            date_str="20260911",
            sim_rows=[("600036.SH", "SELL", 1000)],
            real_rows=[("600036.SH", "SELL", 400)],
            skips={},
        )
        assert report["ok"] is False
        assert report["unexplained"][0]["delta"] == -600

    def test_excess_always_unexplained(self) -> None:
        report = reconcile.build_reconciliation_report(
            date_str="20260911",
            sim_rows=[("600036.SH", "BUY", 100)],
            real_rows=[("600036.SH", "BUY", 300)],
            skips={"600036.SH:whitelist": 2},
        )
        assert report["ok"] is False
        diff = report["diffs"][0]
        assert diff["kind"] == "excess"
        assert diff["explained"] is False

    def test_real_only_symbol_counted(self) -> None:
        report = reconcile.build_reconciliation_report(
            date_str="20260911",
            sim_rows=[],
            real_rows=[("600036.SH", "SELL", 100)],
            skips={},
        )
        assert report["diffs"][0]["kind"] == "excess"
        assert report["sim_symbols"] == 0
        assert report["real_symbols"] == 1


class TestReconcileDayWindow:
    def test_cst_window_bounds(self) -> None:
        start, end = reconcile.cst_day_window("20260911")
        assert start.hour == 0 and start.minute == 0
        assert (end - start).total_seconds() == 86400
        assert start.tzinfo is not None


# --------------------------------------------------------------------------
# 收盘核对
# --------------------------------------------------------------------------
class TestCloseClassification:
    @staticmethod
    def _counter(order_id: str, status: str, **over) -> dict:
        item = {"order_id": order_id, "status": status, "symbol": "600036.SH", "side": "SELL"}
        item.update(over)
        return item

    def test_all_terminal_ok(self) -> None:
        result = audit.classify_close_state(
            counter_orders=[self._counter("C1", "FILLED"), self._counter("C2", "CANCELLED")],
            local_orders=[{"order_id": "L1", "status": "filled", "exchange_order_id": "C1"}],
        )
        assert result["ok"] is True

    def test_counter_open_flagged(self) -> None:
        result = audit.classify_close_state(
            counter_orders=[self._counter("C1", "SUBMITTED")], local_orders=[]
        )
        assert result["ok"] is False
        assert result["counter_open"][0]["order_id"] == "C1"

    def test_local_stale_when_counter_terminal(self) -> None:
        result = audit.classify_close_state(
            counter_orders=[self._counter("C1", "FILLED")],
            local_orders=[{"order_id": "L1", "status": "submitted", "exchange_order_id": "C1"}],
        )
        assert result["local_stale"][0]["reason"] == "counter_terminal"
        assert result["local_stale"][0]["counter_status"] == "FILLED"

    def test_local_stale_when_counter_missing(self) -> None:
        result = audit.classify_close_state(
            counter_orders=[],
            local_orders=[{"order_id": "L1", "status": "submitted", "exchange_order_id": "CX"}],
        )
        assert result["local_stale"][0]["reason"] == "counter_order_missing"

    def test_local_pending_without_exchange_id(self) -> None:
        result = audit.classify_close_state(
            counter_orders=[],
            local_orders=[{"order_id": "L1", "status": "pending", "exchange_order_id": ""}],
        )
        assert result["local_stale"][0]["reason"] == "no_exchange_order_id"

    def test_local_terminal_not_flagged(self) -> None:
        result = audit.classify_close_state(
            counter_orders=[],
            local_orders=[{"order_id": "L1", "status": "filled", "exchange_order_id": "C1"}],
        )
        assert result["ok"] is True

    def test_parse_audit_time_fallback(self) -> None:
        assert audit.parse_audit_time("15:05") == (15, 5)
        assert audit.parse_audit_time("bad") == (15, 5)
        assert reconcile.parse_reconcile_time("15:10") == (15, 10)
        assert reconcile.parse_reconcile_time("") == (15, 10)

    def test_local_orders_restricted_to_qmt_channel(self) -> None:
        """本地残留只对照 QMT 通道单：桥单拿 QMT 柜台比对全是假残留。"""
        clause = str(
            audit._qmt_channel_clause().compile(compile_kwargs={"literal_binds": True})
        )
        for prefix in audit._QMT_CHANNEL_CID_PREFIXES:
            assert f"'{prefix}%'" in clause
        assert "'mirror:%'" in clause

    def test_run_audit_skips_when_channel_unconfigured(self) -> None:
        class FakeClient:
            configured = False

            async def refresh_settings(self) -> dict:
                return {}

        with patch(
            "backend.services.live_trading.services.qmt_exec_client.get_qmt_exec_client",
            return_value=FakeClient(),
        ):
            report = asyncio.run(audit.run_close_audit(FakeRedis(), "20260911"))
        assert report["skipped"] == "qmt_not_configured"
