"""订单意图/幂等纯函数单测。

钉住四态：new / replay / conflict / in_progress / unknown。
任何"结果未知"都不能被当成失败后重发；同一幂等键不同载荷必须显式冲突。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_BASE))

_MODULE_PATH = _BASE / "bridge_order_intent.py"
_spec = importlib.util.spec_from_file_location("bridge_order_intent", _MODULE_PATH)
_intent = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _intent
_spec.loader.exec_module(_intent)


def _params(**over):
    base = {
        "stock_code": "600036.SH",
        "order_volume": 100,
        "price": 38.0,
        "order_type": 23,
        "remark": "sltp-600036.SH-20261116-g1",
    }
    base.update(over)
    return base


def test_canonical_method_covers_write_aliases():
    assert _intent.canonical_method("order_stock") == "submit_order"
    assert _intent.canonical_method("order_stock_async") == "submit_order"
    assert _intent.canonical_method("order_stock_batch") == "submit_orders_batch"
    assert _intent.canonical_method("cancel_order_stock_sysid") == "cancel_order"
    assert _intent.is_order_method("ORDER_STOCK_ASYNC") is True
    assert _intent.is_order_method("query_stock_orders") is False


def test_fingerprint_is_stable_and_ignores_routing_fields():
    a = _intent.order_fingerprint(
        "order_stock", _params(request_id="r1", reply_key="k1", ttl_seconds=60)
    )
    b = _intent.order_fingerprint(
        "order_stock_async", _params(request_id="r2", reply_key="k2", ttl_seconds=30)
    )
    assert a == b


def test_fingerprint_changes_when_order_changes():
    a = _intent.order_fingerprint("order_stock", _params(price=38.0))
    b = _intent.order_fingerprint("order_stock", _params(price=38.1))
    assert a != b


def test_idempotency_key_uses_all_known_key_fields():
    assert _intent.idempotency_key("40327478", "order_stock", _params()) == (
        "qm:qmt:pipe:idem:40327478:sltp-600036.SH-20261116-g1"
    )
    assert _intent.idempotency_key(
        "40327478", "submit_order", _params(remark="", client_order_id="cid-1")
    ) == "qm:qmt:pipe:idem:40327478:cid-1"
    assert _intent.idempotency_key("40327478", "get_asset", _params()) is None


def test_classify_new_replay_conflict_in_progress_unknown():
    fp = _intent.order_fingerprint("order_stock", _params())
    assert _intent.classify_intent(None, fp) == _intent.DECISION_NEW

    done = _intent.make_intent_record(
        account_id="1", method="order_stock", params=_params(), request_id="r1",
        fingerprint=fp, now=1000.0,
    )
    done = _intent.mark_intent(done, _intent.STATE_DONE, response={"ok": True})
    assert _intent.classify_intent(done, fp) == _intent.DECISION_REPLAY

    other = _intent.order_fingerprint("order_stock", _params(price=1.0))
    assert _intent.classify_intent(done, other) == _intent.DECISION_CONFLICT

    inflight = _intent.make_intent_record(
        account_id="1", method="order_stock", params=_params(), request_id="r2",
        fingerprint=fp, now=1000.0,
    )
    assert _intent.classify_intent(inflight, fp) == _intent.DECISION_IN_PROGRESS

    inflight = _intent.mark_intent(
        inflight, _intent.STATE_UNKNOWN, error="timeout"
    )
    assert _intent.classify_intent(inflight, fp) == _intent.DECISION_UNKNOWN


def test_legacy_cache_record_is_treated_as_replay():
    """旧版本存的是裸响应；升级后不能把它当冲突或重发。"""
    legacy = {"ok": True, "data": {"order_id": "1"}}
    fp = _intent.order_fingerprint("order_stock", _params())
    assert _intent.classify_intent(legacy, fp) == _intent.DECISION_REPLAY


def test_unresolved_scan():
    records = [
        {"state": _intent.STATE_DONE},
        {"state": _intent.STATE_INFLIGHT},
        {"state": _intent.STATE_UNKNOWN},
    ]
    assert _intent.has_unresolved(records) is True
    assert _intent.has_unresolved([records[0]]) is False
