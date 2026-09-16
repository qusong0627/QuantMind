"""T-P6-11 情报总线测试：schema 校验/投递消费/topic 鉴权/通知链路修复。

覆盖：
1. U：validate_event 全字段边界（未知字段/坏枚举/超限/坏 payload）与 build 默认值、
   encode/decode 回环；
2. U：authorize_intel_topic 权限矩阵（匿名/跨租户/跨用户/坏 scope/market 合法）；
3. I：真 Redis——三类事件投递 → 消费组读取（schema 一致）→ ack；畸形事件毒丸处理
   （ack 跳过 + 计数）；IntelPusher._handle_one → 广播留痕 + 消费计数落 Redis；
4. WS 鉴权：handle_message 越权 intel 订阅 → SUBSCRIPTION_FORBIDDEN 且未入订阅表；
5. I：通知链路修复——publish_notification(type=data_quality) 真库落行（notification_type
   正确）→ 清理；alert_service 默认 fan-out 走统一发布器（U 注入断言）。
"""

from __future__ import annotations

import json
import uuid

import pytest


# ── 1. schema ───────────────────────────────────────────────────────


@pytest.mark.unit
def test_validate_event_accepts_and_normalizes():
    from backend.shared.intel_events import build_event

    ev = build_event(
        type="news", market="cn", targets=["600036.SH", "", "银行"],
        level="WARN", payload={"sentiment": 0.4}, actions_hint=["veto"],
        source="news_ingest", ts=1789000000.5,
    )
    assert ev["type"] == "news" and ev["market"] == "CN" and ev["level"] == "warn"
    assert ev["targets"] == ["600036.SH", "银行"]
    assert ev["ts"] == 1789000000.5 and ev["source"] == "news_ingest"
    # ts 缺省=now
    ev2 = build_event(type="regime")
    assert ev2["ts"] > 0 and ev2["market"] == "CN" and ev2["level"] == "info"


@pytest.mark.unit
def test_validate_event_rejects_malformed():
    from backend.shared.intel_events import IntelEventError, build_event, validate_event

    base = build_event(type="anomaly", payload={"k": 1})
    with pytest.raises(IntelEventError, match="未知字段"):
        validate_event({**base, "extra": 1})
    with pytest.raises(IntelEventError, match="type 非法"):
        validate_event({**base, "type": "gossip"})
    with pytest.raises(IntelEventError, match="market 非法"):
        validate_event({**base, "market": "MARS"})
    with pytest.raises(IntelEventError, match="level 非法"):
        validate_event({**base, "level": "fatal"})
    with pytest.raises(IntelEventError, match="ts"):
        validate_event({**base, "ts": "not-a-time"})
    with pytest.raises(IntelEventError, match="targets 数量超限"):
        validate_event({**base, "targets": ["x"] * 65})
    with pytest.raises(IntelEventError, match="target 超长"):
        validate_event({**base, "targets": ["x" * 25]})
    with pytest.raises(IntelEventError, match="payload 超限"):
        validate_event({**base, "payload": {"blob": "x" * 9000}})
    with pytest.raises(IntelEventError, match="payload 必须是 dict"):
        validate_event({**base, "payload": [1, 2]})


@pytest.mark.unit
def test_encode_decode_roundtrip():
    from backend.shared.intel_events import (
        IntelEventError,
        build_event,
        decode_event,
        encode_event,
    )

    ev = build_event(type="regime", market="HK", payload={"regime": "risk_on", "breadth": 0.62})
    raw = encode_event(ev)
    assert decode_event(raw) == ev
    assert decode_event(raw.encode("utf-8")) == ev
    with pytest.raises(IntelEventError):
        decode_event("not-json")
    # 缺省字段按 schema 默认补全（type 为唯一必填——生产者必须给）
    relaxed = decode_event(json.dumps({"type": "news"}))
    assert relaxed["market"] == "CN" and relaxed["level"] == "info" and relaxed["ts"] > 0


# ── 2. topic 鉴权矩阵 ───────────────────────────────────────────────


@pytest.mark.unit
def test_authorize_intel_topic_matrix():
    from backend.shared.intel_events import authorize_intel_topic

    meta = {"authenticated": True, "tenant_id": "default", "user_id": "10000001"}
    anon = {"authenticated": False, "tenant_id": "default", "user_id": "anonymous"}

    assert authorize_intel_topic(meta, "intel.default.market.CN") is True
    assert authorize_intel_topic(meta, "intel.default.market.HK") is True
    assert authorize_intel_topic(meta, "intel.default.user.10000001") is True
    assert authorize_intel_topic(meta, "intel.default.user.99999999") is False  # 跨用户
    assert authorize_intel_topic(meta, "intel.other.market.CN") is False       # 跨租户
    assert authorize_intel_topic(anon, "intel.default.market.CN") is False     # 匿名
    assert authorize_intel_topic(meta, "intel.default.market.MARS") is False   # 非法市场
    assert authorize_intel_topic(meta, "intel.default.galaxy.CN") is False     # 坏 scope
    assert authorize_intel_topic(meta, "intel.default.market") is False        # 段数不足
    assert authorize_intel_topic(meta, "stock.600036") is False                # 非 intel


# ── 3. 真 Redis：投递 → 消费 → 留痕 ─────────────────────────────────


@pytest.mark.integration
def test_bus_publish_consume_ack_real_redis():
    """同步 SDK 真链路：投递 → 消费组读取（含毒丸）→ ack 清零 PENDING。"""
    import redis as redis_lib

    from backend.shared import intel_events as ie

    client = redis_lib.Redis(
        host="redis", port=6379, db=0, decode_responses=True,
        socket_connect_timeout=3, socket_timeout=5,
    )
    try:
        client.ping()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Redis 不可达: {exc}")

    suffix = uuid.uuid4().hex[:8]
    key = f"intel:events:test-{suffix}"
    group = f"intel:test-{suffix}"
    try:
        for etype in ("news", "regime", "anomaly"):
            ie.publish_event(
                client, ie.build_event(type=etype, payload={"n": etype}, source="pytest"), key=key
            )
        client.xadd(key, {"data": "{not json"}, maxlen=1000)          # 毒丸①坏 JSON
        client.xadd(key, {"data": json.dumps({"type": "gossip"})}, maxlen=1000)  # 毒丸②坏 schema

        ie.ensure_group(client, key=key, group=group)
        got = ie.read_events(client, key=key, group=group, consumer="t1", block_ms=100)
        types = [ev.get("type") for _id, ev in got if "_malformed" not in ev]
        assert types[:3] == ["news", "regime", "anomaly"], got
        malformed = [ev for _id, ev in got if "_malformed" in ev]
        assert len(malformed) == 2

        # ack 全部（含毒丸由消费方决定是否 ack——SDK 提供 ack_event）
        for msg_id, _ev in got:
            assert ie.ack_event(client, msg_id, key=key, group=group) == 1
        pending = client.xpending(key, group)
        assert int(pending.get("pending") or 0) == 0
    finally:
        client.delete(key)
        client.close()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_intel_pusher_handle_one_real_redis(monkeypatch):
    """异步消费端：IntelPusher._handle_one → 广播留痕 + 消费计数 + ack。"""
    import redis.asyncio as aioredis

    from backend.shared import intel_events as ie
    from backend.services.stream.ws_core.intel_pusher import IntelPusher

    client = aioredis.Redis(
        host="redis", port=6379, db=0, decode_responses=True,
        socket_connect_timeout=3, socket_timeout=5,
    )
    try:
        await client.ping()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Redis 不可达: {exc}")

    suffix = uuid.uuid4().hex[:8]
    key = f"intel:events:putest-{suffix}"
    group = f"intel:putest-{suffix}"
    try:
        ev = ie.build_event(type="news", payload={"n": 1}, source="pytest")
        msg_id = await client.xadd(key, {"data": ie.encode_event(ev)}, maxlen=1000)
        await client.xgroup_create(key, group, id="0", mkstream=True)

        broadcast: list[tuple[str, dict]] = []

        async def _fake_publish(topic, message):
            broadcast.append((topic, message))
            return 1

        import backend.services.stream.ws_core.intel_pusher as ip

        monkeypatch.setattr(ip.manager, "publish", _fake_publish)
        monkeypatch.setattr(ip, "CONSUMER_GROUP", group, raising=False)
        monkeypatch.setattr(ip, "STREAM_KEY", key, raising=False)
        pusher = IntelPusher(consumer="t2")
        pusher._redis = client
        raw = await client.xrange(key, msg_id, msg_id)
        await pusher._handle_one(msg_id, raw[0][1])

        assert broadcast and broadcast[0][0] == "intel.default.market.CN"
        assert broadcast[0][1]["type"] == "intel_event"
        assert broadcast[0][1]["event"]["type"] == "news"
        consumed = await client.get(f"{ip.CONSUMED_COUNTER_PREFIX}:news")
        assert int(consumed or 0) >= 1
        pending = await client.xpending(key, group)
        assert int(pending.get("pending") or 0) == 0  # 已 ack

        # 毒丸：坏数据 → ack 跳过 + malformed 计数（防卡组）
        bad_id = await client.xadd(key, {"data": "oops"}, maxlen=1000)
        raw = await client.xrange(key, bad_id, bad_id)
        await pusher._handle_one(bad_id, raw[0][1])
        assert pusher.counters["malformed"] == 1
        pending = await client.xpending(key, group)
        assert int(pending.get("pending") or 0) == 0
    finally:
        await client.delete(key)
        await client.delete("intel:consumed:news")
        await client.aclose()


# ── 4. WS 越权订阅拒绝 ──────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.integration
async def test_ws_intel_subscription_forbidden(monkeypatch):
    from backend.services.stream.ws_core import server as ws_server

    sent: list[dict] = []

    async def _capture(connection_id, payload, use_queue=True):
        sent.append(payload)
        return True

    monkeypatch.setattr(ws_server.manager, "send_message", _capture)
    monkeypatch.setattr(
        ws_server.manager,
        "connection_metadata",
        {"cid-1": {"authenticated": True, "tenant_id": "default", "user_id": "10000001"}},
        raising=False,
    )
    # manager.subscribe 要求连接已登记——放一个哑连接
    monkeypatch.setattr(
        ws_server.manager, "active_connections", {"cid-1": object()}, raising=False
    )
    # 越权：跨用户 user topic
    await ws_server.handle_message(
        "cid-1", {"type": "subscribe", "topic": "intel.default.user.99999999"}
    )
    assert sent and sent[-1].get("error_code") == "SUBSCRIPTION_FORBIDDEN"

    # 合法：market topic 放行（订阅表登记）
    sent.clear()
    await ws_server.handle_message(
        "cid-1", {"type": "subscribe", "topic": "intel.default.market.CN"}
    )
    assert sent and sent[-1].get("type") == "subscribed"
    assert "cid-1" in ws_server.manager.subscriptions.get("intel.default.market.CN", set())
    await ws_server.manager.unsubscribe("cid-1", "intel.default.market.CN")


# ── 5. 通知链路修复 ─────────────────────────────────────────────────


@pytest.mark.unit
def test_data_quality_type_allowed_and_alert_service_uses_publisher(monkeypatch):
    from backend.shared import notification_publisher as npub
    from backend.services.engine.data_platform import alert_service as asvc

    assert npub._safe_type("data_quality") == "data_quality"

    calls: list[dict] = []

    def _fake_publish_notification(**kwargs):
        calls.append(kwargs)
        return True

    monkeypatch.setattr(npub, "publish_notification", _fake_publish_notification)
    svc = asvc.DataAlertService(db_url="postgresql+psycopg2://x")

    class _Row(tuple):
        pass

    class _Engine:
        def begin(self):
            class _Ctx:
                def __enter__(_self):
                    class _Conn:
                        def execute(_c, sql, params=None):
                            class _R:
                                def fetchall(_r):
                                    return [_Row(("10000001", "default"))]
                            return _R()
                    return _Conn()

                def __exit__(_self, *a):
                    return False
            return _Ctx()

    import sqlalchemy

    monkeypatch.setattr(sqlalchemy, "create_engine", lambda *a, **k: _Engine(), raising=False)
    svc._notify_admins(title="t", body="b", level="warning", extra={"alert_id": 1})
    assert calls and calls[0]["type"] == "data_quality" and calls[0]["user_id"] == "10000001"


@pytest.mark.integration
def test_data_quality_notification_real_db_row():
    """真库：publish_notification(type=data_quality) 落行（notification_type 正确）→ 清理。"""
    from sqlalchemy import create_engine, text

    from backend.shared.notification_publisher import publish_notification

    db_url = "postgresql+psycopg2://quantmind:quantmind2026@db:5432/quantmind"
    engine = create_engine(db_url, pool_pre_ping=True)
    try:
        with engine.connect() as conn:
            admin = conn.execute(
                text(
                    "SELECT user_id, COALESCE(tenant_id,'default') FROM users "
                    "WHERE is_admin = true LIMIT 1"
                )
            ).first()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"DB 不可达: {exc}")
    if admin is None:
        pytest.skip("无 admin 用户")
    title = f"pytest data_quality {uuid.uuid4().hex[:8]}"
    ok = publish_notification(
        user_id=str(admin[0]), tenant_id=str(admin[1]),
        title=title, content="pytest", type="data_quality", level="warning",
    )
    assert ok is True
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT notification_type, level FROM notifications "
                    "WHERE title=:t ORDER BY id DESC LIMIT 1"
                ),
                {"t": title},
            ).first()
        assert row is not None and row[0] == "data_quality" and row[1] == "warning"
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM notifications WHERE title=:t"), {"t": title})
