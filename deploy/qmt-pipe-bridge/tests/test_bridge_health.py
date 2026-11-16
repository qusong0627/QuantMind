"""Windows 侧 bridge_health 的纯判定/退出码测试。

覆盖审计项：显式 DB 0、pos_age=-1 在交易时段判 stale、inflight/unknown
返回退出码 4（看门狗只告警不重启）。
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path

_BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_BASE))

_MODULE_PATH = _BASE / "bridge_health.py"
_spec = importlib.util.spec_from_file_location("bridge_health", _MODULE_PATH)
health = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = health
_spec.loader.exec_module(health)


class FakeClient:
    def __init__(self, *, heartbeat=None, metrics=None, inflight=None, processing=None):
        self.heartbeat = str(time.time()) if heartbeat is None else heartbeat
        self.metrics = dict(metrics or {"pos_age_s": "1", "asset_age_s": "1"})
        self.inflight = list(inflight or [])
        self.processing = list(processing or [])

    def get(self, key):
        return self.heartbeat

    def hget(self, key, field):
        return self.metrics.get(field)

    def hvals(self, key):
        return self.inflight

    def lrange(self, key, start, end):
        return self.processing


def test_resolve_db_keeps_explicit_zero():
    assert health._resolve_db(None, 0) == 0
    assert health._resolve_db("", 0) == 0
    assert health._resolve_db("2", 0) == 2
    assert health._resolve_db(None, None) == 5


def _use(monkeypatch, client, *, account="40327478"):
    monkeypatch.setattr(health, "_BRIDGE_CFG", {"account_id": account})
    redis_mod = type(sys)("redis")
    redis_mod.Redis = lambda *a, **k: client
    monkeypatch.setitem(sys.modules, "redis", redis_mod)
    monkeypatch.setattr(health, "redis", redis_mod)


def test_inflight_unknown_returns_exit_4(monkeypatch):
    record = json.dumps({"state": "unknown", "method": "order_stock"})
    _use(monkeypatch, FakeClient(inflight=[record]))
    assert health.main() == 4


def test_processing_order_returns_exit_4(monkeypatch):
    raw = json.dumps({"request_id": "1", "method": "order_stock", "params": {}})
    _use(monkeypatch, FakeClient(processing=[raw]))
    assert health.main() == 4


def test_processing_read_is_not_order(monkeypatch):
    raw = json.dumps({"request_id": "1", "method": "get_asset", "params": {}})
    _use(monkeypatch, FakeClient(processing=[raw]))
    assert health.main() == 0


def test_negative_pos_age_is_stale_in_trading(monkeypatch):
    _use(monkeypatch, FakeClient(metrics={"pos_age_s": "-1", "asset_age_s": "1"}))
    monkeypatch.setattr(health, "_in_trading_window", lambda now: True)
    assert health.main() == 3


def test_negative_pos_age_is_ok_off_hours(monkeypatch):
    _use(monkeypatch, FakeClient(metrics={"pos_age_s": "-1", "asset_age_s": "1"}))
    monkeypatch.setattr(health, "_in_trading_window", lambda now: False)
    assert health.main() == 0


def test_asset_stale_returns_exit_3(monkeypatch):
    _use(monkeypatch, FakeClient(metrics={"pos_age_s": "1", "asset_age_s": "130"}))
    monkeypatch.setattr(health, "_in_trading_window", lambda now: True)
    assert health.main() == 3


def test_fresh_metrics_ok(monkeypatch):
    _use(monkeypatch, FakeClient(metrics={"pos_age_s": "1", "asset_age_s": "2"}))
    monkeypatch.setattr(health, "_in_trading_window", lambda now: True)
    assert health.main() == 0
