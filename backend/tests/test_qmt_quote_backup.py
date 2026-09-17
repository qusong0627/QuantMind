"""T-P6-02 备源席（大 QMT 全推行情）测试：映射/席位判定 + 双半真链路 E2E。

覆盖：
1. U：qmt_tick_to_record（字段/五档数组/符号形态/无效票价/时间解析）与
   standby_decision 席位矩阵（absent/primary_fresh/primary_stale/backup_owns）；
2. I（**双半真链路**）：kit 真服务端订阅管理器 + 真客户端会话（WholeQuoteClientSession），
   经真 Redis pub/sub 推送 → 适配器席位写：主源新鲜跳过 / 陈旧接管（source=qmt_big +
   五档逐值 + series ZSET）/ 自持续写 / 无效票计数 / 时延打点被调；
3. D：桥不可达（subscribe 抛错）→ 退避 + last_error + bridge_ok=False，不崩循环。
"""

from __future__ import annotations

import json
import time
import uuid

import pytest

# ── 1. 纯函数 ───────────────────────────────────────────────────────


def _tick(price: float = 40.0, pre: float = 39.5, *, with_book: bool = True) -> dict:
    tick = {
        "lastPrice": price,
        "lastClose": pre,
        "open": 39.6,
        "high": price + 0.3,
        "low": price - 0.4,
        "volume": 123456,
        "amount": 4_900_000.0,
        "time": int(time.time() * 1000),
    }
    if with_book:
        tick["bidPrice"] = [price - 0.01 * i for i in range(1, 6)]
        tick["bidVol"] = [100 * i for i in range(1, 6)]
        tick["askPrice"] = [price + 0.01 * i for i in range(1, 6)]
        tick["askVol"] = [200 * i for i in range(1, 6)]
    return tick


@pytest.mark.unit
def test_qmt_tick_to_record_mapping():
    from backend.services.live_trading.services.qmt_quote_backup import (
        qmt_tick_to_record,
    )

    now = time.time()
    rec = qmt_tick_to_record("600036.SH", _tick(), now)
    assert rec is not None
    assert (
        rec["symbol"] == "SH600036"
        and rec["price"] == 40.0
        and rec["pre_close"] == 39.5
    )
    assert rec["bid1"] == pytest.approx(39.99) and rec["ask5"] == pytest.approx(40.05)
    assert rec["bid_vol1"] == 100.0 and rec["ask_vol5"] == 1000.0
    assert rec["source"] == "qmt_big" and abs(rec["ts"] - now) < 5

    # 五档缺失 → 字段缺省（不假填）
    rec2 = qmt_tick_to_record("000001.SZ", _tick(with_book=False), now)
    assert rec2 is not None and "bid1" not in rec2

    # 无效：零价/缺价 → None；非 CN 形态 → None；坏符号 → None
    assert qmt_tick_to_record("600036.SH", {**_tick(), "lastPrice": 0}, now) is None
    assert qmt_tick_to_record("600036.SH", {**_tick(), "lastClose": None}, now) is None
    assert qmt_tick_to_record("AAPL", _tick(), now) is None
    assert qmt_tick_to_record("not-a-code", _tick(), now) is None

    # 时间：毫秒→秒；离谱时间回退 now
    old_ms = int((now - 10 * 86400) * 1000)
    rec3 = qmt_tick_to_record("600036.SH", {**_tick(), "time": old_ms}, now)
    assert rec3["ts"] == pytest.approx(now, abs=1)

    # 载荷构建与主源同构（消费方契约字段）
    from backend.services.live_trading.services.qmt_quote_backup import (
        build_series_payload,
        build_snapshot_fields,
    )

    snap = build_snapshot_fields(rec)
    assert {"Now", "Open", "PreClose", "timestamp", "source", "bid1", "ask5"} <= set(
        snap
    )
    assert snap["source"] == "qmt_big" and snap["Now"] == "40.0"
    series = build_series_payload(rec)
    assert series["normalized_symbol"] == "SH600036" and series["is_stale"] is False


@pytest.mark.unit
def test_standby_decision_matrix():
    from backend.services.live_trading.services.qmt_quote_backup import standby_decision

    now = 1000.0
    assert standby_decision(None, now=now, stale_after_s=30) == (True, "absent")
    assert standby_decision({}, now=now, stale_after_s=30) == (True, "absent")
    assert standby_decision(
        {"source": "tdx_aidata_sub", "timestamp": "990"}, now=now, stale_after_s=30
    ) == (False, "primary_fresh")
    assert standby_decision(
        {"source": "tdx_aidata_sub", "timestamp": "900"}, now=now, stale_after_s=30
    ) == (True, "primary_stale")
    assert standby_decision(
        {"source": "qmt_big", "timestamp": "999"}, now=now, stale_after_s=30
    ) == (True, "backup_owns")  # 自家写的不拦自家
    assert standby_decision(
        {"source": "", "timestamp": "999"}, now=now, stale_after_s=30
    ) == (False, "primary_fresh")  # 无源名按时间判
    assert standby_decision({"source": "x"}, now=now, stale_after_s=30) == (
        True,
        "primary_stale",
    )


class _FakeLatency:
    def __init__(self):
        self.observed: list[float] = []
        self.flushes = 0

    def observe(self, value):
        self.observed.append(value)

    def maybe_flush(self):
        self.flushes += 1


# ── 2. 双半真链路 ───────────────────────────────────────────────────


def _redis(db: int = 9, *, decode: bool = True):
    import redis as redis_lib

    return redis_lib.Redis(
        host="redis",
        port=6379,
        db=db,
        decode_responses=decode,
        socket_connect_timeout=3,
        socket_timeout=5,
    )


def _wait_for(cond, timeout: float = 8.0, interval: float = 0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(interval)
    return False


@pytest.mark.integration
def test_backup_standby_write_via_real_pubsub():
    """kit 真服务端管理器 + 真客户端会话 → 真 Redis pub/sub → 席位写断言。"""
    import redis as redis_lib

    from bigqmt_signal_trader.quote_push_channel import RedisQuotePushChannel
    from bigqmt_signal_trader.quote_subscription_manager import QuoteSubscriptionManager
    from bigqmt_signal_trader.whole_quote_session import WholeQuoteClientSession

    from backend.services.live_trading.services.qmt_quote_backup import (
        BackupConfig,
        QmtQuoteBackupService,
    )

    account = f"pytest-qmt-{uuid.uuid4().hex[:6]}"
    try:
        store = _redis(9)
        store.ping()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Redis 不可达: {exc}")

    # ── 服务端半：真管理器 + 桩源（记录订阅/可手动 fire）──
    class _StubSource:
        def __init__(self):
            self.on_push = None
            self.codes: list[str] = []

        def subscribe(self, codes, on_push):
            self.codes = list(codes)
            self.on_push = on_push
            return f"handle-{uuid.uuid4().hex[:4]}"

        def unsubscribe(self, handle):
            self.on_push = None

    source = _StubSource()
    # 推送负载是 msgpack 二进制——通道客户端必须 decode_responses=False（kit 同约定）
    server_channel = RedisQuotePushChannel(_redis(0, decode=False), account_id=account)
    manager = QuoteSubscriptionManager(
        source, heartbeat_timeout_seconds=30.0, on_push_publisher=server_channel.publish
    )

    # ── 客户端半：真会话（rpc 桩直连管理器）──
    def rpc_call(method, params):
        if method == "subscribe_whole_quote":
            return manager.subscribe(
                params["client_id"], params["sub_id"], params["codes"]
            )
        if method == "unsubscribe_whole_quote":
            manager.unsubscribe(params["client_id"], params["sub_id"])
            return {}
        if method == "quote_keepalive":
            manager.keepalive(params["client_id"], params["sub_id"])
            return {}
        return {}

    client_channel = RedisQuotePushChannel(_redis(0, decode=False), account_id=account)
    session = WholeQuoteClientSession(
        rpc_call=rpc_call,
        push_channel=client_channel,
        client_id="pytest-c1",
        heartbeat_interval_seconds=0.5,
    )
    session.start()

    latency = _FakeLatency()
    svc = QmtQuoteBackupService(
        config_loader=lambda: BackupConfig(
            enabled=True, stale_after_s=30.0, symbols_refresh_s=9999.0
        ),
        hot_set_fetcher=lambda: ["600036.SH", "000001.SZ"],
        subscribe_fn=session.subscribe_whole_quote,
        unsubscribe_fn=session.unsubscribe_quote,
        writer_client_factory=lambda: _redis(9),
        latency_recorder=latency,
    )
    snap_key = "market:snapshot:sh600036"
    series_key = "market:series:SH600036"
    try:
        # 主源新鲜 → 跳过
        store.hset(
            snap_key,
            mapping={
                "Now": "40.0",
                "PreClose": "39.5",
                "timestamp": str(int(time.time())),
                "source": "tdx_aidata_sub",
            },
        )
        svc.sync_subscription()
        assert svc.status()["bridge_ok"] is True

        assert _wait_for(lambda: source.on_push is not None), "订阅未到达服务端"
        # 客户端订阅线程就绪（pub/sub 为 fire-and-forget，未就绪时首发会丢）——就绪后重发
        assert _wait_for(lambda: getattr(session, "_subscriber_active", False)), (
            "订阅通道未就绪"
        )
        time.sleep(0.4)
        for _ in range(6):
            source.on_push({"600036.SH": _tick(price=41.0)})
            if _wait_for(lambda: svc.counters["batches"] >= 1, timeout=1.5):
                break
        assert svc.counters["batches"] >= 1
        assert svc.counters["written"] == 0 and svc.counters["skipped_fresh"] == 1
        assert store.hget(snap_key, "source") == "tdx_aidata_sub"  # 未被覆盖

        # 主源陈旧 → 接管写
        store.hset(snap_key, "timestamp", str(int(time.time()) - 120))
        source.on_push({"600036.SH": _tick(price=41.2), "000001.SZ": _tick(price=10.5)})
        assert _wait_for(lambda: svc.counters["written"] >= 2)
        assert store.hget(snap_key, "source") == "qmt_big"
        assert float(store.hget(snap_key, "Now")) == pytest.approx(41.2)
        assert float(store.hget(snap_key, "bid1")) == pytest.approx(41.19)
        zset = store.zrange(series_key, -1, -1, withscores=True)
        assert zset and json.loads(zset[0][0])["source"] == "qmt_big"
        assert float(store.hget("market:snapshot:sz000001", "Now")) == pytest.approx(
            10.5
        )

        # 自持续写（backup_owns）：本方上笔写不挡下一笔
        source.on_push({"600036.SH": _tick(price=41.5)})
        assert _wait_for(
            lambda: float(store.hget(snap_key, "Now")) == pytest.approx(41.5)
        )

        # 无效票计数（零价）
        invalid_before = svc.counters["records_invalid"]
        source.on_push({"600036.SH": {**_tick(), "lastPrice": 0}})
        assert _wait_for(lambda: svc.counters["records_invalid"] > invalid_before)

        # 时延打点被调（写=3 笔以上；含跳过批不 observe）
        assert len(latency.observed) >= 3 and latency.flushes >= 1
    finally:
        session.stop()
        client_channel.stop()
        server_channel.stop()
        store.delete(
            snap_key, series_key, "market:snapshot:sz000001", "market:series:SZ000001"
        )
        store.close()


# ── 3. 降级：桥不可达 ───────────────────────────────────────────────


@pytest.mark.unit
def test_bridge_offline_backoff_and_status():
    from backend.services.live_trading.services.qmt_quote_backup import (
        BackupConfig,
        QmtQuoteBackupService,
    )

    def _boom(codes, callback):
        raise ConnectionError("bridge offline")

    svc = QmtQuoteBackupService(
        config_loader=lambda: BackupConfig(enabled=True),
        hot_set_fetcher=lambda: ["600036.SH"],
        subscribe_fn=_boom,
        writer_client_factory=lambda: None,
        latency_recorder=_FakeLatency(),
    )
    svc.sync_subscription()
    st = svc.status()
    assert st["bridge_ok"] is False
    assert "bridge offline" in (st["last_error"] or "")
    # 退避生效：立即重调不发第二次（处于 backoff 窗口）
    calls = {"n": 0}

    def _count(codes, callback):
        calls["n"] += 1
        raise ConnectionError("bridge offline")

    svc2 = QmtQuoteBackupService(
        config_loader=lambda: BackupConfig(enabled=True, symbols_refresh_s=0.0),
        hot_set_fetcher=lambda: ["600036.SH"],
        subscribe_fn=_count,
    )
    svc2.sync_subscription()
    svc2._last_symbols_refresh = 0  # 允许立即再试一次 → 应被 backoff 拦下
    svc2.sync_subscription()
    assert calls["n"] == 1, "退避窗口内不得重试风暴"
