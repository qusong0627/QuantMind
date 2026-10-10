"""T4-4（审计 H14）回归：行情读写分裂（split-brain）收敛。

背景（2026-10-10 审计实测）：
- 写侧（tdx_hot_set_feed / qmt_quote_backup / tdx_aidata）统一经
  ``backend/shared/remote_quote_config.resolve_remote_quote_redis()`` 落公网
  行情 Redis（快照 Hash 自带 ``source`` 字段：tdx_bridge / qmt_big / ...）；
- 三个读侧此前各读各的：stream WS 快照源与手工执行裸读 env（容器里回落本机
  → 永远查空，只靠 QuantDB 日线兜底）；arena 实况补价用本地 sentinel 客户端
  （decode_responses=False，bytes 键永远 miss——隐性第二坑）。全链零提示。
- quote_pusher 把 QuantDB 日线兜底（timestamp=当日零点、is_stale 恒 False）
  当实时点回写 ``market:series``，在本机 db3 留下 source=quantdb 的伪时序点。

本测试锁定 T4-4 修正后的契约：
1. 三个读侧一律走 resolve_remote_quote_redis()；仅 REMOTE_QUOTE_DISABLED
   显式停用才回落部署内 Redis；
2. arena 客户端必须来自 make_sync_client（decode_responses=True，str 键可读）；
3. quote_pusher 不再回写 market:series（兜底数据只落库/推送并显式标注
   data_source）——「本机键空时不产伪实时」为验收条款；fetch_quotes 透传
   快照自带 source；WS push 载荷携带 data_source（可观测）。
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from backend.shared import remote_quote_config as rqc

_ENV_KEYS = [
    "REMOTE_QUOTE_REDIS_HOST",
    "REMOTE_QUOTE_REDIS_PORT",
    "REMOTE_QUOTE_REDIS_PASSWORD",
    "REMOTE_QUOTE_REDIS_DB",
    "REMOTE_QUOTE_DISABLED",
]


def _isolate(monkeypatch):
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(rqc, "_root_env_cache", {})  # 隔离真实项目根 .env


# ---------- 1/2/3. 三个读侧统一解析 ----------


def test_stream_source_uses_shared_remote_config(monkeypatch):
    """stream WS 快照源：env 覆盖必须生效（写侧同源解析）。"""
    _isolate(monkeypatch)
    monkeypatch.setenv("REMOTE_QUOTE_REDIS_HOST", "quote.test")
    monkeypatch.setenv("REMOTE_QUOTE_REDIS_PORT", "6390")
    monkeypatch.setenv("REMOTE_QUOTE_REDIS_PASSWORD", "s3cret")
    monkeypatch.setenv("REMOTE_QUOTE_REDIS_DB", "7")

    from backend.services.stream.market_app.services.remote_redis_source import (
        RemoteRedisDataSource,
    )

    src = RemoteRedisDataSource()
    assert (src._host, src._port, src._password, src._db) == (
        "quote.test",
        6390,
        "s3cret",
        7,
    )


def test_stream_source_disabled_falls_back_local(monkeypatch):
    """显式停用远端（REMOTE_QUOTE_DISABLED）才回落部署内 Redis。"""
    _isolate(monkeypatch)
    monkeypatch.setenv("REMOTE_QUOTE_DISABLED", "true")
    monkeypatch.setenv("REDIS_HOST", "dep-local")
    monkeypatch.setenv("REDIS_PORT", "6399")
    monkeypatch.setenv("REDIS_PASSWORD", "lpw")
    monkeypatch.setenv("REDIS_DB_MARKET", "3")

    from backend.services.stream.market_app.services.remote_redis_source import (
        RemoteRedisDataSource,
    )

    src = RemoteRedisDataSource()
    assert (src._host, src._port, src._password, src._db) == (
        "dep-local",
        6399,
        "lpw",
        3,
    )


class _FakeSyncRedis:
    last_kwargs: dict[str, Any] = {}

    def __init__(self, **kwargs):
        type(self).last_kwargs = dict(kwargs)

    def ping(self):
        return True


def test_manual_execution_uses_shared_remote_config(monkeypatch):
    """手工执行取价：与写侧统一走 resolve_remote_quote_redis()。"""
    _isolate(monkeypatch)
    monkeypatch.setenv("REMOTE_QUOTE_REDIS_HOST", "quote.test")
    monkeypatch.setenv("REMOTE_QUOTE_REDIS_PORT", "6390")
    monkeypatch.setenv("REMOTE_QUOTE_REDIS_PASSWORD", "s3cret")
    monkeypatch.setenv("REMOTE_QUOTE_REDIS_DB", "7")

    from backend.services.live_trading.services import manual_execution_service as me

    monkeypatch.setattr(me, "_quote_redis", None)
    monkeypatch.setattr(me.redis_lib, "Redis", _FakeSyncRedis)

    client = me._get_quote_redis()
    assert isinstance(client, _FakeSyncRedis)
    assert _FakeSyncRedis.last_kwargs["host"] == "quote.test"
    assert _FakeSyncRedis.last_kwargs["port"] == 6390
    assert _FakeSyncRedis.last_kwargs["db"] == 7
    assert _FakeSyncRedis.last_kwargs["password"] == "s3cret"
    assert _FakeSyncRedis.last_kwargs["decode_responses"] is True


def test_manual_execution_disabled_falls_back_local(monkeypatch):
    _isolate(monkeypatch)
    monkeypatch.setenv("REMOTE_QUOTE_DISABLED", "true")
    monkeypatch.setenv("REDIS_HOST", "dep-local")
    monkeypatch.setenv("REDIS_PORT", "6399")
    monkeypatch.setenv("REDIS_PASSWORD", "lpw")
    monkeypatch.setenv("REDIS_DB_MARKET", "3")

    from backend.services.live_trading.services import manual_execution_service as me

    monkeypatch.setattr(me, "_quote_redis", None)
    monkeypatch.setattr(me.redis_lib, "Redis", _FakeSyncRedis)

    client = me._get_quote_redis()
    assert isinstance(client, _FakeSyncRedis)
    assert _FakeSyncRedis.last_kwargs["host"] == "dep-local"
    assert _FakeSyncRedis.last_kwargs["db"] == 3


def test_arena_reader_uses_make_sync_client(monkeypatch):
    """arena 实况补价：客户端来自 make_sync_client（远端 + str 键），进程级缓存。

    arena 数据面为本地部署包（.gitignore:317，公开仓无此包）→ 缺包时跳过。
    """
    router_lf = pytest.importorskip(
        "backend.services.api.routers.agent_arena.live_family"
    )

    sentinel = object()
    calls = {"n": 0}

    def _fake_make_sync_client():
        calls["n"] += 1
        return sentinel

    monkeypatch.setattr(router_lf, "make_sync_client", _fake_make_sync_client)
    monkeypatch.setattr(router_lf, "_quote_redis_client", None)

    assert router_lf._redis_or_none() is sentinel
    assert router_lf._redis_or_none() is sentinel
    assert calls["n"] == 1  # 第二次命中缓存，不重复构造


def test_arena_reader_none_when_remote_unavailable(monkeypatch):
    router_lf = pytest.importorskip(
        "backend.services.api.routers.agent_arena.live_family"
    )

    monkeypatch.setattr(router_lf, "make_sync_client", lambda: None)
    monkeypatch.setattr(router_lf, "_quote_redis_client", None)

    assert router_lf._redis_or_none() is None


# ---------- 4. fetch_quotes 来源标注透传 ----------


class _FakeAsyncPipeline:
    def __init__(self, store: dict[str, dict]):
        self._store = store
        self._keys: list[str] = []

    def hgetall(self, key: str):
        self._keys.append(key)

    async def execute(self):
        return [self._store.get(k, {}) for k in self._keys]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeAsyncClient:
    def __init__(self, store: dict[str, dict]):
        self._store = store

    def pipeline(self, transaction: bool = False):
        return _FakeAsyncPipeline(self._store)


def _snapshot_hash(now_ts: int, source: str | None) -> dict[str, str]:
    snap = {
        "Now": "10.5",
        "Open": "10.0",
        "PreClose": "10.2",
        "timestamp": str(now_ts),
    }
    if source:
        snap["source"] = source
    return snap


@pytest.mark.asyncio
async def test_fetch_quotes_propagates_snapshot_source(monkeypatch):
    """快照 Hash 自带 source（席位写侧写入）→ 透传为 data_source。"""
    from backend.services.stream.market_app.services.remote_redis_source import (
        RemoteRedisDataSource,
    )

    now_ts = int(time.time())

    src = RemoteRedisDataSource()
    monkeypatch.setattr(
        src,
        "_get_client",
        lambda: _FakeAsyncClient(
            {"market:snapshot:sh600036": _snapshot_hash(now_ts, "tdx_bridge")}
        ),
    )
    quotes = await src.fetch_quotes(["600036.SH"])
    assert quotes and quotes[0]["data_source"] == "tdx_bridge"

    src2 = RemoteRedisDataSource()
    monkeypatch.setattr(
        src2,
        "_get_client",
        lambda: _FakeAsyncClient(
            {"market:snapshot:sh600036": _snapshot_hash(now_ts, None)}
        ),
    )
    quotes2 = await src2.fetch_quotes(["600036.SH"])
    assert quotes2 and quotes2[0]["data_source"] == "remote_redis"


# ---------- 5. quote_pusher：兜底不产伪实时 ----------


class _FakeManager:
    def __init__(self):
        self.published: list[tuple[str, dict]] = []

    async def publish(self, topic: str, message: dict):
        self.published.append((topic, message))
        return 1


class _FakeQuoteSource:
    def __init__(self, quotes: list[dict[str, Any]]):
        self.quotes = quotes
        self.calls = 0

    async def fetch_quotes(self, symbols: list[str]):
        self.calls += 1
        return [dict(q) for q in self.quotes]


def _quote(symbol: str, source: str) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "current_price": 10.0,
        "open_price": 9.9,
        "high_price": 10.1,
        "low_price": 9.8,
        "close_price": 9.85,
        "volume": 1000,
        "amount": 1.0e4,
        "timestamp": datetime.now(timezone.utc),
        "is_stale": False,
        "data_source": source,
    }


def _install_pusher_fakes(monkeypatch, qp_mod, redis_src, qdb_src):
    fake_mgr = _FakeManager()
    monkeypatch.setattr(qp_mod, "get_remote_redis_source", lambda: redis_src)
    monkeypatch.setattr(qp_mod, "get_quantdb_source", lambda: qdb_src)
    monkeypatch.setattr(qp_mod, "manager", fake_mgr)
    return fake_mgr


@pytest.mark.asyncio
async def test_pusher_with_empty_keys_produces_no_series_points(monkeypatch):
    """验收条款：本机/远端快照键空 → quantdb 兜底只落库、只推送（带 source
    标注），绝不回写 market:series 伪实时点。"""
    from backend.services.stream.market_app.services.remote_redis_source import (
        RemoteRedisDataSource,
    )
    from backend.services.stream.ws_core import quote_pusher as qp_mod

    real_src = RemoteRedisDataSource()
    touch_calls: list[str] = []

    def _spy_get_client():
        touch_calls.append("_get_client")
        return None  # 真被用到会 AttributeError → 测试失败

    async def _empty_fetch(symbols):
        return []  # 行情键全空

    monkeypatch.setattr(real_src, "_get_client", _spy_get_client)
    monkeypatch.setattr(real_src, "fetch_quotes", _empty_fetch)

    qdb_src = _FakeQuoteSource([_quote("SZ000001", "quantdb")])
    fake_mgr = _install_pusher_fakes(monkeypatch, qp_mod, real_src, qdb_src)

    pusher = qp_mod.QuotePusher()
    pusher.subscribed_stocks = {"SZ000001"}

    persisted: list[dict[str, Any]] = []

    async def _rec(quotes):
        persisted.extend(quotes)

    async def _noop_stat(count):
        return None

    monkeypatch.setattr(pusher, "_persist_quotes", _rec)
    monkeypatch.setattr(pusher, "_report_persist_stats", _noop_stat)

    await pusher._push_once()

    assert touch_calls == []  # 未触碰行情 Redis：无伪实时回写
    assert len(persisted) == 1 and persisted[0]["data_source"] == "quantdb"
    assert fake_mgr.published, "兜底数据仍应推送，但必须带来源标注"
    topic, message = fake_mgr.published[0]
    assert topic == "stock.SZ000001"
    assert message["data"]["data_source"] == "quantdb"


@pytest.mark.asyncio
async def test_pusher_pushes_true_source_from_snapshot(monkeypatch):
    """真实席位快照命中时：不调 quantdb；推送载荷带真实 source。"""
    from backend.services.stream.ws_core import quote_pusher as qp_mod

    redis_src = _FakeQuoteSource([_quote("SH600036", "tdx_bridge")])
    qdb_src = _FakeQuoteSource([])
    fake_mgr = _install_pusher_fakes(monkeypatch, qp_mod, redis_src, qdb_src)

    pusher = qp_mod.QuotePusher()
    pusher.subscribed_stocks = {"SH600036"}

    async def _rec(quotes):
        return None

    async def _noop_stat(count):
        return None

    monkeypatch.setattr(pusher, "_persist_quotes", _rec)
    monkeypatch.setattr(pusher, "_report_persist_stats", _noop_stat)

    await pusher._push_once()

    assert qdb_src.calls == 0
    assert fake_mgr.published
    assert fake_mgr.published[0][1]["data"]["data_source"] == "tdx_bridge"


# ---------- 6. 防回潮源码守卫 ----------


def test_no_series_writeback_source_guard():
    """伪实时回写路径已移除，不得被重新引入（T4-4 防回潮）。"""
    backend = Path(__file__).resolve().parents[1]

    pusher_src = (backend / "services/stream/ws_core/quote_pusher.py").read_text(
        encoding="utf-8"
    )
    assert "append_series_point" not in pusher_src
    assert "write_series" not in pusher_src

    redis_source_src = (
        backend / "services/stream/market_app/services/remote_redis_source.py"
    ).read_text(encoding="utf-8")
    assert "def append_series_point" not in redis_source_src

    # arena 数据面为本地部署包（.gitignore:317；公开仓无此文件，main.py 注册时
    # 静默跳过）→ 文件缺失时守卫自然免跑。
    arena_router = backend / "services/api/routers/agent_arena/live_family.py"
    if arena_router.is_file():
        arena_router_src = arena_router.read_text(encoding="utf-8")
        assert "get_redis_sentinel_client" not in arena_router_src
        assert "make_sync_client" in arena_router_src

    manual_src = (
        backend / "services/live_trading/services/manual_execution_service.py"
    ).read_text(encoding="utf-8")
    assert "resolve_remote_quote_redis" in manual_src

    stream_src = (
        backend / "services/stream/market_app/services/remote_redis_source.py"
    ).read_text(encoding="utf-8")
    assert "resolve_remote_quote_redis" in stream_src
