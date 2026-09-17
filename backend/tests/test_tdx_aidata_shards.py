"""TdxAiData 订阅分片（SDK 单进程上限 100）测试。

背景（2026-09-17 夜实测，见 docs/P6实时轨_实施细案.md T-P6-02）：
- 单次 subscribe ≤100 只：正常（元帧流动）；
- ≥101 只：**整批拒绝**——SDK 打印「[错误码 2] 股票代码错误」、零帧到达（仅打印不抛）；
- 多 worker 分片订阅共存可行（各片独立 socket/SDK 连接，同一标准键天然合并）。

覆盖：
1. U：shard_symbols 纯函数（完备/不交/确定/均衡）；
2. U：引擎分片过滤 + 超限截断显式计数 + 空片不订阅；
3. U：merge_subscriptions 聚合口径（求和/最差分片/保守静默）；
4. G：SDK 上限常量 + l05 分片文件名防撞；
5. I 真机：2 分片 E2E——120 只真实热集切 2 片，双侧元帧>0（活体证明）+ 聚合状态。
"""

from __future__ import annotations

import os
import signal
import time
import uuid
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[1]


# ── 1. 分片纯函数 ───────────────────────────────────────────────────


@pytest.mark.unit
def test_shard_symbols_partition_properties():
    from backend.shared.tdx_aidata.collector import shard_symbols

    symbols = {f"{600000 + i}.SH" for i in range(1000)}
    shards = [shard_symbols(symbols, i, 6) for i in range(6)]
    # 完备：并集 == 全集
    assert set().union(*[set(s) for s in shards]) == symbols
    # 不交：两两无交集
    for a in range(6):
        for b in range(a + 1, 6):
            assert not set(shards[a]) & set(shards[b])
    # 确定：同输入同输出（跨调用稳定，热集增删不重排其它标的）
    assert shard_symbols(symbols, 2, 6) == shards[2]
    subset = set(list(symbols)[:500])
    assert shard_symbols(subset, 2, 6) == [s for s in shards[2] if s in subset]
    # 均衡：hash 分布近均匀（1000/6≈167，容差宽放）
    assert all(120 <= len(s) <= 220 for s in shards), [len(s) for s in shards]
    # 有序 & 单分片退化
    assert shards[0] == sorted(shards[0])
    assert shard_symbols(symbols, 0, 1) == sorted(symbols)


# ── 2. 引擎分片与上限 ───────────────────────────────────────────────


class _FakeRedis:
    def __init__(self, members: set[str]):
        self._members = members

    def smembers(self, key):
        return set(self._members)

    def close(self):
        pass


class _FakeBudget:
    def check(self):
        return None

    def consume(self):
        pass

    def note_rate_limited(self):
        return 60.0

    def note_success(self):
        pass


def _engine(members: set[str], **kwargs):
    from backend.shared.tdx_aidata.collector import SubscriptionEngine

    calls = {"subscribe": [], "unsubscribe": 0}

    def _sdk_subscribe(codes, cb):
        calls["subscribe"].append(list(codes))

    def _sdk_unsubscribe():
        calls["unsubscribe"] += 1

    engine = SubscriptionEngine(
        sdk_subscribe=_sdk_subscribe,
        sdk_unsubscribe=_sdk_unsubscribe,
        budget_gate=_FakeBudget(),
        redis_factory=lambda: _FakeRedis(members),
        **kwargs,
    )
    return engine, calls


@pytest.mark.unit
def test_engine_shard_filter_and_sync():
    from backend.shared.tdx_aidata.collector import shard_symbols

    members = {f"{600000 + i}.SH" for i in range(120)}
    engine, calls = _engine(members, shard_id=1, shard_count=2)
    result = engine.sync_hot_set_once()
    assert result["changed"] is True
    expected = set(shard_symbols(members, 1, 2))
    assert engine._current == expected
    assert calls["subscribe"] and set(calls["subscribe"][-1]) == expected
    assert engine.counters["hot_set_size"] == 120
    assert engine.counters["over_cap"] is False
    snap = engine.snapshot()
    assert snap["shard"] == {"id": 1, "count": 2}
    assert snap["subscribed"] == len(expected)
    # 再次同步：无变化不重复订阅
    result2 = engine.sync_hot_set_once()
    assert result2["changed"] is False
    assert len(calls["subscribe"]) == 1


@pytest.mark.unit
def test_engine_over_cap_truncates_and_counts():
    from backend.shared.tdx_aidata.collector import SDK_SUBSCRIBE_MAX

    assert SDK_SUBSCRIBE_MAX == 100, (
        "SDK 实测上限=100（101 起整批拒绝，见模块 docstring）"
    )
    members = {f"{600000 + i}.SH" for i in range(150)}
    engine, calls = _engine(members, shard_id=0, shard_count=1)
    engine.sync_hot_set_once()
    assert len(engine._current) == SDK_SUBSCRIBE_MAX
    assert engine.counters["over_cap"] is True
    assert calls["subscribe"] and len(calls["subscribe"][-1]) == SDK_SUBSCRIBE_MAX


@pytest.mark.unit
def test_engine_empty_shard_skips_subscribe():
    members = {"600036.SH"}
    # 分 3 片：必然有空片（hash 落点唯一）
    empties = []
    for sid in range(3):
        engine, calls = _engine(members, shard_id=sid, shard_count=3)
        engine.sync_hot_set_once()
        if not engine._current:
            empties.append(sid)
            assert calls["subscribe"] == [], "空片不得调用 SDK subscribe"
    assert empties, "1 只标的切 3 片必有空片"


# ── 3. 聚合口径 ─────────────────────────────────────────────────────


@pytest.mark.unit
def test_merge_subscriptions_aggregate():
    from backend.shared.tdx_aidata.client import merge_subscriptions

    snaps = [
        {
            "enabled": True,
            "hot_set_key": "qm:hot_set:symbols",
            "shard": {"id": 0, "count": 2},
            "subscribed": 88,
            "last_frame_age_s": 3.0,
            "silent": False,
            "counters": {
                "written": 100,
                "redis_errors": 0,
                "last_error": None,
                "over_cap": False,
                "hot_set_size": 527,  # 分片视角同值：聚合必须取 max（求和会报成 3162）
            },
            "archiver": {
                "pending_rows": 5,
                "rows": 1000,
                "flushes": 10,
                "flush_errors": 0,
                "base_dir": "/d",
            },
            "latency": {"p95_ms": 400.0, "observed": 1000, "flushes": 10},
        },
        {
            "enabled": True,
            "hot_set_key": "qm:hot_set:symbols",
            "shard": {"id": 1, "count": 2},
            "subscribed": 90,
            "last_frame_age_s": 9.0,
            "silent": True,
            "counters": {
                "written": 90,
                "redis_errors": 1,
                "last_error": "boom",
                "over_cap": True,
                "hot_set_size": 527,
            },
            "archiver": {
                "pending_rows": 3,
                "rows": 900,
                "flushes": 9,
                "flush_errors": 1,
                "base_dir": "/d",
            },
            "latency": {"p95_ms": 800.0, "observed": 900, "flushes": 9},
        },
    ]
    merged = merge_subscriptions(snaps)
    assert merged["subscribed"] == 178
    assert merged["last_frame_age_s"] == 3.0  # 集群新鲜度=最新分片
    assert merged["silent"] is True and merged["silent_shards"] == [1]  # 保守口径点名
    assert merged["counters"]["written"] == 190
    assert merged["counters"]["redis_errors"] == 1
    assert merged["counters"]["last_error"] == "boom"  # 字符串取首个非空
    assert merged["counters"]["over_cap"] is True  # bool 逐片或
    assert merged["counters"]["hot_set_size"] == 527  # 同值取 max 而非求和
    assert (
        merged["archiver"]["rows"] == 1900 and merged["archiver"]["flush_errors"] == 1
    )
    assert merged["latency"]["p95_ms"] == 800.0  # 最差分片
    assert merged["latency"]["observed"] == 1900
    assert len(merged["shards"]) == 2
    assert merge_subscriptions([])["enabled"] is False


@pytest.mark.unit
def test_l05_shard_tag_filename_guard(tmp_path):
    """分片归档文件名互不撞车（同日同毫秒不同 tag → 不同文件）。"""
    from datetime import datetime, timedelta, timezone

    from backend.shared.l05_store import SnapshotArchiver

    base = tmp_path / "l05"
    ts = int(
        datetime(2026, 9, 17, 10, 0, 0, tzinfo=timezone(timedelta(hours=8))).timestamp()
    )
    record = {
        "symbol": "600036.SH",
        "ts": ts,
        "price": 40.0,
        "pre_close": 39.9,
        "open": 40.0,
    }

    a = SnapshotArchiver(base_dir=str(base), tag="s0")
    b = SnapshotArchiver(base_dir=str(base), tag="s1")
    a.append(dict(record))
    b.append(dict(record))
    ra = a.flush()
    rb = b.flush()
    files = [Path(p).name for p in (ra["files"] + rb["files"])]
    assert len(files) == 2 and len(set(files)) == 2, files
    assert "-s0-" in files[0] and "-s1-" in files[1]
    from backend.shared.l05_store import read_day
    from datetime import date

    df = read_day(date(2026, 9, 17), base_dir=str(base), symbols=["600036.SH"])
    assert len(df) == 2  # 两片各一行，读取侧无感


# ── 4. 真机：2 分片 E2E ─────────────────────────────────────────────


@pytest.mark.integration
@pytest.mark.asyncio
async def test_two_shard_cluster_e2e():
    """2 分片订阅 120 只真实热集标的：双侧活体（元帧>0）+ 聚合；结束全清理。"""
    import asyncio

    from backend.shared.hot_set_store import hot_set_key, make_hot_set_client
    from backend.shared.tdx_aidata import config
    from backend.shared.tdx_aidata.client import TdxAiDataCluster

    if not config.dir_ready(config.resolve_dir()):
        pytest.skip(f"TdxAiData 安装目录不完整: {config.resolve_dir()}")
    # 热集读取侧 = **部署本地 Redis**（hot_set_store 单一事实源，2026-09-17 起）
    r = make_hot_set_client(socket_connect_timeout=5, socket_timeout=10)
    try:
        prod = sorted(r.smembers(hot_set_key()))
    except Exception as exc:  # noqa: BLE001
        r.close()
        pytest.skip(f"本地热集不可读: {exc}")
    if len(prod) < 4:
        r.close()
        pytest.skip("本地热集样本不足")

    symbols = prod[:120] if len(prod) >= 120 else prod
    base_socket = "/tmp/qm-tdx-shard-e2e.sock"
    hot_key = f"qm:hot_set:test-{uuid.uuid4().hex[:8]}"
    os.environ["QM_HOT_SET_KEY"] = hot_key
    os.environ["TDX_AIDATA_SUBSCRIBE_ENABLED"] = "1"
    # 测试 worker 关闭落盘/打点 sink：绝不写生产 L0.5 目录与 intel:latency
    # （2026-09-17 实测：E2E 曾把夜间重放帧写进 /data/l05_snapshots 与生产时延窗口）
    os.environ["QM_L05_ENABLED"] = "0"
    os.environ["QM_LATENCY_ENABLED"] = "0"
    os.environ["QM_HOT_SET_SYNC_S"] = "2"
    cluster = TdxAiDataCluster(shard_count=2, base_socket=base_socket)
    try:
        r.sadd(hot_key, *symbols)
        started = await cluster.ensure_all()
        assert all(started.values()), f"分片拉起失败: {started}"
        # 等两片订阅建立
        deadline = time.time() + 90
        sub = {}
        while time.time() < deadline:
            await asyncio.sleep(3)
            sub = await cluster.subscription_status()
            if int(sub.get("subscribed") or 0) >= len(symbols):
                break
        assert int(sub.get("subscribed") or 0) == len(symbols), sub
        per_shard = {s["shard"]["id"]: s["subscribed"] for s in sub["shards"]}
        assert all(v > 0 for v in per_shard.values()), per_shard
        assert sum(per_shard.values()) == len(symbols)

        # 活体证明：等待元帧（夜盘亦有；数据帧仅盘中）
        deadline = time.time() + 75
        metas = {0: 0, 1: 0}
        while time.time() < deadline:
            await asyncio.sleep(5)
            sub = await cluster.subscription_status()
            metas = {
                s["shard"]["id"]: int((s.get("counters") or {}).get("frames_meta") or 0)
                for s in sub["shards"]
            }
            if all(v > 0 for v in metas.values()):
                break
        assert all(v > 0 for v in metas.values()), f"存在无帧分片: {metas}"
        # 全链聚合计数可用
        assert sub["counters"]["frames_meta"] >= sum(metas.values())
    finally:
        for client in cluster.clients:
            try:
                st = await client.status()
                pid = st.get("pid")
                if isinstance(pid, int) and pid > 1:
                    os.kill(pid, signal.SIGTERM)
            except Exception:  # noqa: BLE001
                pass
        await cluster.close()
        os.environ.pop("QM_HOT_SET_KEY", None)
        os.environ.pop("TDX_AIDATA_SUBSCRIBE_ENABLED", None)
        os.environ.pop("QM_HOT_SET_SYNC_S", None)
        os.environ.pop("QM_L05_ENABLED", None)
        os.environ.pop("QM_LATENCY_ENABLED", None)
        try:
            r.delete(hot_key)
            r.close()
        except Exception:  # noqa: BLE001
            pass
        for path in (base_socket, f"{base_socket}.s1"):
            try:
                os.unlink(path)
            except OSError:
                pass


# ── 7. 回归：tqs 累积账本必须每订先清（2026-09-17 盘中静默事故）─────────


@pytest.mark.unit
def test_sdk_subscribe_resets_tqs_accumulator():
    """tqs.subscribe 内部会重发历史并集——worker 包装必须每次先清账本，
    否则并集单调膨胀越过 SDK 单批 100 上限 → 整批拒绝 → 全片静默（事故实锤）。"""
    import threading

    from backend.shared.tdx_aidata.worker import AidataWorker

    class _FakeTqs:
        _sub_codes: list = []
        _sub_callbacks: dict = {}
        _sub_lock = threading.RLock()

        def __init__(self):
            self.sent: list[list] = []
            type(self)._sub_codes = []
            type(self)._sub_callbacks = {}

        def subscribe(self, stock_list, callback):
            with self._sub_lock:
                type(self)._sub_codes = list(
                    dict.fromkeys(self._sub_codes + list(stock_list))
                )
                self.sent.append(list(type(self)._sub_codes))

    worker = AidataWorker.__new__(AidataWorker)
    worker.tqs = _FakeTqs()
    cb = lambda text: 1  # noqa: E731
    first = [f"60{i:04d}.SH" for i in range(90)]
    second = [f"00{i:04d}.SZ" for i in range(80)]
    worker._sdk_subscribe(first, cb)
    worker._sdk_subscribe(second, cb)
    assert len(worker.tqs.sent[0]) == 90
    assert len(worker.tqs.sent[1]) == 80, (
        f"第二次订阅须为纯新集（80），实际 {len(worker.tqs.sent[1])}——账本未复位将复现静默事故"
    )


# ── 8. 回归：订阅不占请求预算 + 差分最小间隔（2026-09-17 盘中饿死事故）──


@pytest.mark.unit
def test_resubscribe_not_charged_to_request_budget_and_min_gap():
    """订阅通道零配额（实证）——重订不得走「3 次/窗口」请求闸门；
    热集差分重订须受最小间隔约束（防 churn），间隔内推迟并计数。"""
    import time as _time

    from backend.shared.tdx_aidata.collector import SubscriptionEngine

    class _CountingGate:
        def __init__(self):
            self.consumed = 0

        def check(self):
            return None

        def consume(self):
            self.consumed += 1

        def note_rate_limited(self):
            return 60.0

        def note_success(self):
            pass

    members = {f"{600000 + i}.SH" for i in range(10)}
    gate = _CountingGate()
    subs: list[list[str]] = []
    engine = SubscriptionEngine(
        sdk_subscribe=lambda codes, cb: subs.append(list(codes)),
        sdk_unsubscribe=lambda: None,
        budget_gate=gate,
        redis_factory=lambda: type("R", (), {"smembers": lambda self, k: set(members), "close": lambda self: None})(),
        resubscribe_min_gap_s=60.0,
    )
    engine.sync_hot_set_once()
    assert subs and engine.counters["resubscribes"] == 1
    assert gate.consumed == 0, "订阅调用不得消耗请求预算（零配额实证；旧版饿死根因）"

    # 热集变化但在最小间隔内 → 推迟计数，不发第二次订阅
    for extra in range(1, 3):
        members.add(f"{300000 + extra}.SZ")
    engine.sync_hot_set_once()
    assert len(subs) == 1 and engine.counters["resubscribe_deferred_gap"] == 1

    # 越过最小间隔 → 正常重订
    engine._last_resubscribe_ts = _time.time() - 61
    engine.sync_hot_set_once()
    assert engine.counters["resubscribes"] == 2 and len(subs) == 2


# ── 9. 回归：残留 socket 文件不得阻塞按需拉起（2026-09-17 次生故障）──────


@pytest.mark.unit
def test_stale_socket_does_not_block_spawn(tmp_path, monkeypatch):
    """SIGKILL 残留的 socket 文件必须被存活探测识破并清理，否则拉起永久跳过→全片静默。"""
    import socket as _socket
    from pathlib import Path as _Path

    from backend.shared.tdx_aidata.client import TdxAiDataClient

    sock_path = str(tmp_path / "stale.sock")
    # 制造“文件存在但无监听”的残留：bind 后立即关闭（unix socket 文件留存）
    s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    s.bind(sock_path)
    s.close()
    assert _Path(sock_path).exists()

    spawned = {"n": 0}

    class _FakePopen:
        def __init__(self, *a, **k):
            spawned["n"] += 1

    import backend.shared.tdx_aidata.client as client_mod

    monkeypatch.setattr(client_mod.subprocess, "Popen", _FakePopen)
    client = TdxAiDataClient(socket_path=sock_path)
    ok = client._spawn_worker_proc()
    assert ok is True and spawned["n"] == 1, "残留 socket 必须被剔除并重新拉起"
    assert not _Path(sock_path).exists()  # 残留文件已清理（被 Popen 桩替代，不会重建）

    # 对照：真有监听者 → 不重复拉起
    s2 = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    live_path = str(tmp_path / "live.sock")
    s2.bind(live_path)
    s2.listen(1)
    try:
        c2 = TdxAiDataClient(socket_path=live_path)
        before = spawned["n"]
        assert c2._spawn_worker_proc() is True and spawned["n"] == before
    finally:
        s2.close()


@pytest.mark.unit
def test_engine_reads_hot_set_via_local_reader_not_write_client():
    """热集读取走独立 reader（部署本地 Redis），**不**经写侧客户端（远端行情服）。

    回归 2026-09-17 事故：写侧（快照落远端）与读侧（热集）同客户端时，热集被迫放
    公共服全局键上、多实例互覆。本测试锁定两侧解耦：reader 提供什么就读什么，
    写侧客户端里的成员绝不参与订阅。
    """
    from backend.shared.tdx_aidata.collector import SubscriptionEngine

    write_side_members = {"999999.SH"}  # 写侧客户端可见成员（不应被读）
    read_side_members = {"600036.SH", "000001.SZ"}
    calls = {"subscribe": []}

    engine = SubscriptionEngine(
        sdk_subscribe=lambda codes, cb: calls["subscribe"].append(list(codes)),
        sdk_unsubscribe=lambda: None,
        budget_gate=_FakeBudget(),
        redis_factory=lambda: _FakeRedis(write_side_members),
        hot_set_reader=lambda: set(read_side_members),
        shard_id=0,
        shard_count=1,
    )
    result = engine.sync_hot_set_once()
    assert result["changed"] is True
    assert engine._current == {"600036.SH", "000001.SZ"}
    assert "999999.SH" not in engine._current
    assert calls["subscribe"] and set(calls["subscribe"][-1]) == {"600036.SH", "000001.SZ"}


@pytest.mark.unit
def test_engine_hot_set_reader_failure_is_visible_and_non_fatal():
    """reader 抛错：如实记 last_error（进计数器）、不改订阅集合、不崩循环。"""
    from backend.shared.tdx_aidata.collector import SubscriptionEngine

    calls = {"subscribe": []}

    def _boom():
        raise TimeoutError("Timeout connecting to server")

    engine = SubscriptionEngine(
        sdk_subscribe=lambda codes, cb: calls["subscribe"].append(list(codes)),
        sdk_unsubscribe=lambda: None,
        budget_gate=_FakeBudget(),
        redis_factory=lambda: _FakeRedis({"600036.SH"}),
        hot_set_reader=_boom,
        shard_id=0,
        shard_count=1,
    )
    result = engine.sync_hot_set_once()
    assert result["changed"] is False
    assert result["reason"] == "hot_set_unreadable"
    assert "hot_set read" in (engine.counters.get("last_error") or "")
    assert not calls["subscribe"]
