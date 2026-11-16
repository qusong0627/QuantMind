"""止损守护三态退出：失败不得永久关闭规则。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.executor.exit_result import (  # noqa: E402
    KIND_ERROR,
    KIND_NO_POSITION,
    KIND_REJECTED,
    KIND_SUCCESS,
    KIND_UNKNOWN,
    ExitResult,
    apply_exit_outcome,
    classify_order_result,
    retry_delay_s,
)
from src.executor.stop_loss_daemon import StopLossDaemon  # noqa: E402
from src.tdx.client import TdxError, TdxTimeoutError  # noqa: E402


class FakeTdx:
    def __init__(self, *, price=9.0, positions=None, result=None,
                 order_exc=None, position_exc=None):
        self.price = price
        self.positions = positions if positions is not None else [
            {"Code": "600036.SH", "CanUseVol": 100}
        ]
        self.result = result if result is not None else {"Value": "2", "Wtbh": "W1"}
        self.order_exc = order_exc
        self.position_exc = position_exc
        self.orders = []

    def get_latest_price(self, code):
        return self.price

    def query_stock_positions(self, account_id):
        if self.position_exc:
            raise self.position_exc
        return list(self.positions)

    def order_stock(self, account_id, code, side, volume, price_type, price,
                    notify=0):
        self.orders.append((code, side, volume, price))
        if self.order_exc:
            raise self.order_exc
        return dict(self.result)


def _daemon(tmp_path, tdx, records=None):
    return StopLossDaemon(
        tdx, str(tmp_path / "stop_loss_state.json"), account_id=7,
        poll_interval=0.01, trade_log=(records.append if records is not None else None),
    )


def _item():
    return {
        "stock_code": "600036.SH", "entry_price": 10.0, "volume": 100,
        "highest_price_since_entry": 10.0, "stop_loss_pct": 0.05,
        "take_profit_pct": None, "trailing_stop_pct": None, "enabled": True,
    }


def test_classify_order_result_three_states():
    assert classify_order_result("2").kind == KIND_SUCCESS
    assert classify_order_result("1").kind == KIND_UNKNOWN
    assert classify_order_result("0", "no money").kind == KIND_REJECTED
    assert classify_order_result(None).kind == KIND_REJECTED


def test_retry_delay_backs_off_and_caps():
    assert retry_delay_s(1) == 5.0
    assert retry_delay_s(2) == 10.0
    assert retry_delay_s(99) == 300.0


def test_apply_outcome_only_success_disables():
    now = 1000.0
    success = apply_exit_outcome(
        _item(), ExitResult(KIND_SUCCESS, "submitted", "W1"), now)
    assert success["enabled"] is False
    assert success["order_id"] == "W1"

    for result in (
        classify_order_result("1"),
        classify_order_result("0", "rejected"),
    ):
        updated = apply_exit_outcome(_item(), result, now)
        assert updated["enabled"] is True
        assert updated["retry_after_ts"] > now
        assert updated["attempts"] == 1


def test_no_position_keeps_rule_enabled(tmp_path):
    records = []
    tdx = FakeTdx(positions=[])
    daemon = _daemon(tmp_path, tdx, records)
    daemon.register_items([_item()])
    daemon._tick()
    item = daemon.state_items()[0]
    assert item["enabled"] is True
    assert item["last_result"] == KIND_NO_POSITION
    assert tdx.orders == []
    assert records and records[-1]["event"] == "sltp_exit_failed"


def test_timeout_keeps_rule_enabled(tmp_path):
    tdx = FakeTdx(order_exc=TdxTimeoutError("timeout"))
    daemon = _daemon(tmp_path, tdx)
    daemon.register_items([_item()])
    daemon._tick()
    item = daemon.state_items()[0]
    assert item["enabled"] is True
    assert item["last_result"] == KIND_UNKNOWN
    assert item["retry_after_ts"] > item["last_attempt_at"]


def test_broker_error_keeps_rule_enabled(tmp_path):
    tdx = FakeTdx(order_exc=TdxError("boom"))
    daemon = _daemon(tmp_path, tdx)
    daemon.register_items([_item()])
    daemon._tick()
    item = daemon.state_items()[0]
    assert item["enabled"] is True
    assert item["last_result"] == KIND_ERROR


def test_rejected_keeps_rule_enabled(tmp_path):
    tdx = FakeTdx(result={"Value": "0", "Msg": "no money"})
    daemon = _daemon(tmp_path, tdx)
    daemon.register_items([_item()])
    daemon._tick()
    item = daemon.state_items()[0]
    assert item["enabled"] is True
    assert item["last_result"] == KIND_REJECTED


def test_success_disables_rule(tmp_path):
    tdx = FakeTdx(result={"Value": "2", "Wtbh": "W9"})
    daemon = _daemon(tmp_path, tdx)
    daemon.register_items([_item()])
    daemon._tick()
    item = daemon.state_items()[0]
    assert item["enabled"] is False
    assert item["order_id"] == "W9"
    assert len(tdx.orders) == 1


def test_retry_window_prevents_immediate_second_sell(tmp_path):
    tdx = FakeTdx(order_exc=TdxTimeoutError("timeout"))
    daemon = _daemon(tmp_path, tdx)
    daemon.register_items([_item()])
    daemon._tick()
    daemon._tick()
    assert len(tdx.orders) == 1
