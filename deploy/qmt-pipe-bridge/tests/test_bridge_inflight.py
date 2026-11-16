"""伴生桥订单意图/回报持久化的故障注入测试（无 Windows/真 Redis）。

覆盖审计 High：
* BLMOVE 取件后进程挂 -> processing 保留、inflight=unknown、重启不重放；
* 发送超时 -> 同一 remark 第二次直接 unknown，不再发第二笔；
* 同 remark 不同参数 -> conflict；
* XADD 失败 -> seen 不更新，下一轮重投。
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

_BASE = Path(__file__).resolve().parents[1]
_MODULE_PATH = _BASE / "pipe_redis_bridge.py"


class FakeRedis:
    def __init__(self, *args, queue=None, **kwargs):
        self.lists = {"q": list(queue or [])}
        self.hashes = {}
        self.kv = {}
        self.xadd_calls = 0
        self.xadd_fail_once = False

    # -- lists ---------------------------------------------------------
    def blmove(self, first, second, timeout, src="LEFT", dest="RIGHT"):
        source = self.lists.setdefault(first, [])
        if not source:
            return None
        value = source.pop(0)
        target = self.lists.setdefault(second, [])
        if dest == "LEFT":
            target.insert(0, value)
        else:
            target.append(value)
        return value

    def lrange(self, key, start, end):
        items = list(self.lists.get(key, []))
        if end == -1:
            return items[start:]
        return items[start:end + 1]

    def lrem(self, key, count, value):
        items = self.lists.get(key, [])
        out = []
        removed = 0
        for item in items:
            if item == value and (count == 0 or removed < count):
                removed += 1
                continue
            out.append(item)
        self.lists[key] = out
        return removed

    def llen(self, key):
        return len(self.lists.get(key, []))

    def rpush(self, key, value):
        self.lists.setdefault(key, []).append(value)
        return len(self.lists[key])

    def lpush(self, key, value):
        self.lists.setdefault(key, []).insert(0, value)
        return len(self.lists[key])

    # -- hashes --------------------------------------------------------
    def hset(self, key, field, value):
        self.hashes.setdefault(key, {})[field] = value
        return 1

    def hdel(self, key, field):
        return 1 if self.hashes.setdefault(key, {}).pop(field, None) is not None else 0

    def hvals(self, key):
        return list(self.hashes.get(key, {}).values())

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    # -- kv ------------------------------------------------------------
    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.kv:
            return None
        self.kv[key] = value
        return True

    def get(self, key):
        return self.kv.get(key)

    def expire(self, key, ttl):
        return 1

    # -- streams -------------------------------------------------------
    def xadd(self, key, fields, maxlen=None, approximate=True):
        self.xadd_calls += 1
        if self.xadd_fail_once:
            self.xadd_fail_once = False
            raise RuntimeError("xadd down")
        return "1-0"


class FakeTransport:
    def __init__(self, response=None, exc=None):
        self.response = response or {"request_id": "r", "ok": True, "data": {"accepted": True}}
        self.exc = exc
        self.calls = []

    def send_request(self, req, timeout_seconds):
        self.calls.append(req)
        if self.exc is not None:
            raise self.exc
        return dict(self.response)


def _load_bridge(monkeypatch):
    redis_mod = types.ModuleType("redis")
    redis_mod.Redis = FakeRedis
    monkeypatch.setitem(sys.modules, "redis", redis_mod)

    pkg = types.ModuleType("bigqmt_signal_trader")
    pkg.__path__ = []
    transports = types.ModuleType("bigqmt_signal_trader.transports")
    transports.__path__ = []
    pipe_mod = types.ModuleType("bigqmt_signal_trader.transports.pipe_transport")

    class _FakePipeTransport:
        def __init__(self, *args, **kwargs):
            pass

    pipe_mod.NamedPipeTransport = _FakePipeTransport
    rpc_mod = types.ModuleType("bigqmt_signal_trader.redis_rpc")
    rpc_mod.decode_rpc_request_payload = lambda payload: payload
    monkeypatch.setitem(sys.modules, "bigqmt_signal_trader", pkg)
    monkeypatch.setitem(sys.modules, "bigqmt_signal_trader.transports", transports)
    monkeypatch.setitem(
        sys.modules, "bigqmt_signal_trader.transports.pipe_transport", pipe_mod
    )
    monkeypatch.setitem(sys.modules, "bigqmt_signal_trader.redis_rpc", rpc_mod)

    spec = importlib.util.spec_from_file_location("_qmt_pipe_bridge_inflight", _MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.CFG._data["account_id"] = "40327478"
    module.CFG._data["risk"]["enabled"] = False
    return module


def _req(request_id="r1", price=38.0):
    return {
        "request_id": request_id,
        "method": "order_stock",
        "params": {
            "stock_code": "600036.SH",
            "order_volume": 100,
            "price": price,
            "order_type": 23,
            "order_remark": "sltp-600036.SH-20261116-g1",
        },
    }


@pytest.fixture()
def bridge(monkeypatch):
    return _load_bridge(monkeypatch)


def test_success_clears_processing_and_inflight(bridge):
    raw = json.dumps(_req(), ensure_ascii=False)
    r = FakeRedis(queue=[raw])
    bridge.R = r
    bridge.TR = FakeTransport()
    replies = []

    item = bridge._pop_request("q")
    assert item == raw
    assert r.llen("q") == 0
    assert r.lrange(bridge.processing_key(), 0, -1) == [raw]

    req = json.loads(item)
    resp, remove = bridge._send_order_intent(
        req, raw=item, reply_fn=lambda _req, _resp: replies.append(_resp)
    )
    assert remove is True
    assert resp["ok"] is True
    assert replies and replies[0]["ok"] is True
    if remove:
        bridge._remove_processing(item)
    assert r.lrange(bridge.processing_key(), 0, -1) == []
    assert r.hvals(bridge.inflight_key()) == []
    assert len(bridge.TR.calls) == 1


def test_timeout_keeps_processing_and_blocks_second_send(bridge):
    raw = json.dumps(_req(), ensure_ascii=False)
    r = FakeRedis(queue=[raw])
    bridge.R = r
    bridge.TR = FakeTransport(exc=TimeoutError("pipe timeout"))
    replies = []

    item = bridge._pop_request("q")
    req = json.loads(item)
    first, remove = bridge._send_order_intent(
        req, raw=item, reply_fn=lambda _req, _resp: replies.append(_resp)
    )
    assert first["code"] == "ORDER_RESULT_UNKNOWN"
    assert first["retriable"] is False
    assert remove is False
    assert r.lrange(bridge.processing_key(), 0, -1) == [raw]
    inflight = [json.loads(v) for v in r.hvals(bridge.inflight_key())]
    assert inflight and inflight[0]["state"] == "unknown"

    # 同一 remark 的重试：直接 unknown，不再发第二笔
    second, remove2 = bridge._send_order_intent(
        req, raw=item, reply_fn=lambda _req, _resp: replies.append(_resp)
    )
    assert second["code"] == "ORDER_RESULT_UNKNOWN"
    assert remove2 is True
    assert len(bridge.TR.calls) == 1


def test_same_remark_different_params_conflicts_without_send(bridge):
    raw = json.dumps(_req(), ensure_ascii=False)
    r = FakeRedis(queue=[raw])
    bridge.R = r
    bridge.TR = FakeTransport()
    req = json.loads(raw)
    first, remove = bridge._send_order_intent(req, raw=raw)
    assert first["ok"] is True and remove is True

    changed = _req(request_id="r2", price=99.0)
    second, remove2 = bridge._send_order_intent(changed, raw=raw)
    assert second["code"] == "IDEMPOTENCY_CONFLICT"
    assert second["retriable"] is False
    assert remove2 is True
    assert len(bridge.TR.calls) == 1


def test_intent_store_failure_never_sends(bridge):
    class _NoHset(FakeRedis):
        def hset(self, *args, **kwargs):
            raise RuntimeError("redis write down")

    bridge.R = _NoHset()
    bridge.TR = FakeTransport()
    resp, remove = bridge._send_order_intent(_req(), raw="raw")
    assert resp["code"] == "ORDER_INTENT_STORE_FAILED"
    assert resp["retriable"] is False
    assert remove is False
    assert bridge.TR.calls == []


def test_xadd_failure_does_not_mark_seen(bridge):
    r = FakeRedis()
    bridge.R = r
    seen = {}
    rows = [{"order_id": "1", "status": "FILLED"}]
    r.xadd_fail_once = True
    with pytest.raises(RuntimeError):
        bridge._emit_events_once("order", rows, seen, bridge._order_fingerprint,
                                 "stream", "40327478")
    assert seen == {}

    wrote = bridge._emit_events_once(
        "order", rows, seen, bridge._order_fingerprint, "stream", "40327478")
    assert wrote == 1
    assert seen
    # 第三次同样的回报：不再写
    assert bridge._emit_events_once(
        "order", rows, seen, bridge._order_fingerprint, "stream", "40327478") == 0


def test_recover_processing_marks_order_unknown_without_replay(bridge):
    raw = json.dumps(_req(), ensure_ascii=False)
    read_raw = json.dumps({"request_id": "q1", "method": "get_asset", "params": {}})
    r = FakeRedis()
    bridge.R = r
    r.lists[bridge.processing_key()] = [raw, read_raw]
    unknown = bridge.recover_processing()
    assert unknown == 1
    assert r.lrange(bridge.processing_key(), 0, -1) == [raw]
    assert r.llen("bigqmt:rpc:queue:40327478") == 1
    inflight = [json.loads(v) for v in r.hvals(bridge.inflight_key())]
    assert inflight and inflight[0]["state"] == "unknown"
