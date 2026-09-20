"""Tests for runner.worker — drives the full pipeline against InMemoryProvider.

We patch ``worker._TEST_PROVIDER`` so the worker doesn't try to qlib.init.
Redis is mocked through fake_redis on store_result/Publisher.
"""

from __future__ import annotations

import pandas as pd
import pytest

from backend.services.engine.strategy_lab.engine.data_provider import InMemoryProvider
from backend.services.engine.strategy_lab.runner import worker as worker_mod
from backend.services.engine.strategy_lab.runner.result_collector import RunResult, _sanitize


class FakeRedis:
    def __init__(self) -> None:
        self.kv: dict[str, bytes] = {}
        self.lists: dict[str, list[bytes]] = {}
        self.hashes: dict[str, dict[bytes, bytes]] = {}

    def set(self, k, v, ex=None):
        if isinstance(v, str):
            v = v.encode()
        self.kv[k] = v

    def get(self, k):
        return self.kv.get(k)

    def rpush(self, k, v):
        if isinstance(v, str):
            v = v.encode()
        self.lists.setdefault(k, []).append(v)

    def lrange(self, k, start, end):
        arr = self.lists.get(k, [])
        end = len(arr) if end == -1 else end + 1
        return arr[start:end]

    def hset(self, k, *, mapping):
        h = self.hashes.setdefault(k, {})
        for kk, vv in mapping.items():
            kb = kk.encode() if isinstance(kk, str) else kk
            vb = vv.encode() if isinstance(vv, str) else (str(vv).encode() if not isinstance(vv, bytes) else vv)
            h[kb] = vb

    def hgetall(self, k):
        return dict(self.hashes.get(k, {}))

    def expire(self, *a, **k):
        pass


def _make_provider() -> InMemoryProvider:
    idx = pd.date_range("2025-01-02", periods=8, freq="B")
    a = pd.DataFrame(
        {
            "open": [10 + i * 0.1 for i in range(8)],
            "high": [10.5 + i * 0.1 for i in range(8)],
            "low": [9.5 + i * 0.1 for i in range(8)],  # fidelity: allow-limit-threshold — 非阈值：K 线夹具的 low 序列
            "close": [10 + i * 0.1 for i in range(8)],
            "volume": [1000.0] * 8,
            "adj_close": [10 + i * 0.1 for i in range(8)],
        },
        index=idx,
    )
    return InMemoryProvider({"A": a})


@pytest.fixture
def fake_redis(monkeypatch):
    fake = FakeRedis()
    import backend.shared.redis_sentinel_client as rc
    import backend.services.engine.strategy_lab.runner.progress as prog
    import backend.services.engine.strategy_lab.runner.result_collector as rcoll
    import backend.services.engine.strategy_lab.runner.subprocess_runner as sr
    monkeypatch.setattr(rc, "get_redis_sentinel_client", lambda: fake)
    monkeypatch.setattr(prog, "get_redis_sentinel_client", lambda: fake)
    monkeypatch.setattr(rcoll, "get_redis_sentinel_client", lambda: fake)
    return fake


@pytest.fixture
def patched_provider(monkeypatch):
    provider = _make_provider()
    monkeypatch.setattr(worker_mod, "_TEST_PROVIDER", provider, raising=False)
    yield provider
    monkeypatch.setattr(worker_mod, "_TEST_PROVIDER", None, raising=False)


def test_worker_run_request_success(fake_redis, patched_provider):
    code = """
def setup(ctx):
    ctx.universe = ["A"]
    ctx.start = "2025-01-02"
    ctx.end = "2025-01-13"
    ctx.cash = 1_000_000
    ctx.commission = 0.0
    ctx.slippage = 0.0

def on_bar(ctx, bar):
    if ctx.position(bar.symbol).qty == 0:
        ctx.buy(bar.symbol, weight=0.5, reason="enter")
"""
    rc = worker_mod.run_request({"run_id": "r1", "code": code, "params": {}})
    assert rc == 0
    raw = fake_redis.kv["qm:lab:result:r1"]
    import json
    payload = json.loads(raw.decode())
    assert payload["status"] == "success"
    assert payload["metrics"]["n_trades"] >= 1
    assert payload["script_sha"]
    assert payload["run_id"] == "r1"


def test_worker_rejects_banned_import(fake_redis, patched_provider):
    code = """
import os

def setup(ctx):
    ctx.universe = ["A"]
    ctx.start = "2025-01-02"
    ctx.end = "2025-01-10"
    ctx.cash = 1_000_000
"""
    rc = worker_mod.run_request({"run_id": "r2", "code": code, "params": {}})
    assert rc == 1
    import json
    payload = json.loads(fake_redis.kv["qm:lab:result:r2"].decode())
    assert payload["status"] == "failed"
    assert "import" in (payload.get("error") or "").lower()


def test_worker_handles_user_runtime_error(fake_redis, patched_provider):
    code = """
def setup(ctx):
    ctx.universe = ["A"]
    ctx.start = "2025-01-02"
    ctx.end = "2025-01-10"
    ctx.cash = 1_000_000

def on_bar(ctx, bar):
    raise ValueError("boom")
"""
    rc = worker_mod.run_request({"run_id": "r3", "code": code, "params": {}})
    # Errors inside on_bar are swallowed and logged; the run still succeeds
    assert rc == 0
    import json
    payload = json.loads(fake_redis.kv["qm:lab:result:r3"].decode())
    assert payload["status"] == "success"
    msgs = [l.get("msg", "") for l in payload.get("logs", [])]
    assert any("boom" in m for m in msgs)


def test_worker_handles_setup_failure(fake_redis, patched_provider):
    code = """
def setup(ctx):
    ctx.universe = ["A"]
    # missing required fields -> assert_ready will raise
"""
    rc = worker_mod.run_request({"run_id": "r4", "code": code, "params": {}})
    assert rc == 1
    import json
    payload = json.loads(fake_redis.kv["qm:lab:result:r4"].decode())
    assert payload["status"] == "failed"


def test_worker_main_empty_stdin(monkeypatch):
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert worker_mod.main() == 2


def test_worker_main_bad_json(monkeypatch):
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert worker_mod.main() == 2
