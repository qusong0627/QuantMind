"""TradePlan 持久化幂等：重启不重放、账号解析失败 fail-closed。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.trade_plan import Order, TradePlan  # noqa: E402
from src.executor.plan_executor import PlanExecutor  # noqa: E402
from src.executor.plan_state import (  # noqa: E402
    DECISION_UNKNOWN,
    PlanStateStore,
)
from src.tdx.client import TdxError  # noqa: E402


class FakeTdx:
    def __init__(self, *, account_handle=0, order_value="2"):
        self.account_handle = account_handle
        self.order_value = order_value
        self.orders = []
        self.positions = []

    def stock_account(self, account, account_type):
        return self.account_handle

    def query_stock_positions(self, account_id):
        return list(self.positions)

    def order_stock(self, account_id, stock, side, volume, price_type, price,
                    notify=0):
        self.orders.append((stock, side, volume, price))
        return {"Value": self.order_value, "Wtbh": "W1", "Msg": ""}

    def send_message(self, msg):
        return None


def _plan(plan_id="p1", account=""):
    return TradePlan(
        plan_id=plan_id,
        account=account,
        orders=[Order(stock_code="600036.SH", side="buy", volume=100,
                      price=10.0)],
    )


def _executor(tmp_path, tdx):
    return PlanExecutor(tdx, shared_report_dir=str(tmp_path),
                        state_dir=str(tmp_path))


def test_explicit_account_resolution_failure_is_fail_closed(tmp_path):
    tdx = FakeTdx(account_handle=-1)
    report = _executor(tmp_path, tdx).execute_plan(_plan(account="bad"))
    assert report["status"] == "rejected"
    assert "账户解析失败" in report["message"]
    assert tdx.orders == []


def test_explicit_account_resolution_exception_is_fail_closed(tmp_path):
    class Boom(FakeTdx):
        def stock_account(self, account, account_type):
            raise TdxError("account not found")

    tdx = Boom()
    report = _executor(tmp_path, tdx).execute_plan(_plan(account="bad"))
    assert report["status"] == "rejected"
    assert tdx.orders == []


def test_same_plan_is_duplicate_after_restart(tmp_path):
    tdx = FakeTdx()
    first = _executor(tmp_path, tdx).execute_plan(_plan())
    assert first["status"] == "executed"
    assert len(tdx.orders) == 1

    # 新实例 = 桥重启；状态从文件加载，不得重放
    second = _executor(tmp_path, tdx).execute_plan(_plan())
    assert second["status"] == "duplicate"
    assert len(tdx.orders) == 1


def test_started_plan_returns_unknown_and_does_not_replay(tmp_path):
    store = PlanStateStore(str(tmp_path))
    store.mark("p-started", "started")
    tdx = FakeTdx()
    report = _executor(tmp_path, tdx).execute_plan(_plan("p-started"))
    assert report["status"] == "unknown"
    assert tdx.orders == []


def test_state_file_is_written_atomically(tmp_path):
    tdx = FakeTdx()
    _executor(tmp_path, tdx).execute_plan(_plan())
    path = tmp_path / "executed_plans.json"
    assert path.exists()
    import json
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["plans"]["p1"]["status"] == "executed"


def test_corrupt_state_fails_closed(tmp_path):
    (tmp_path / "executed_plans.json").write_text("{bad json", encoding="utf-8")
    store = PlanStateStore(str(tmp_path))
    assert store.classify("p1") == DECISION_UNKNOWN
    with pytest.raises(OSError):
        store.mark("p1", "started")
