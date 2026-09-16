"""T-P6-05 新鲜度契约统一 + 端到端时延度量 测试。

覆盖：
1. U：classify_age 边界（fresh/stale/unavailable / None / 未来戳容差）；
2. U：quote_policy env 优先级（新名 > 旧别名 > 默认）与非法值收口；
3. G：源守卫——三处旧阈值零残留（services 层不得出现旧 env / 硬编码 60/300），
   唯一实现只在 shared/freshness.py；
4. U：LatencyRecorder 分位/计数/flush 阈值/失败不抛（假 Redis 客户端）；
5. I：preflight 门禁（check_stream_series_freshness）三级语义 + 降级回退（假 Redis）；
6. I：真 Redis 打点回环（写入→read_latency→清理；Redis 不可达时如实 skip）；
7. I：SubscriptionEngine 打点 sink 接线（真推送样本 → observe 被调用）。
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

import pytest

from backend.shared.freshness import (
    FRESH,
    STALE,
    UNAVAILABLE,
    FreshnessPolicy,
    classify_age,
    quote_policy,
)

_BACKEND = Path(__file__).resolve().parents[1]


# ── 1. 分级谓词边界 ─────────────────────────────────────────────────


@pytest.mark.unit
def test_classify_age_boundaries():
    p = FreshnessPolicy(fresh_within_s=60, stale_within_s=300)
    assert p.classify(0) == FRESH
    assert p.classify(60) == FRESH, "边界含 fresh_within"
    assert p.classify(60.001) == STALE
    assert p.classify(300) == STALE, "边界含 stale_within"
    assert p.classify(300.001) == UNAVAILABLE
    assert p.classify(None) == UNAVAILABLE
    assert p.classify(float("nan")) == UNAVAILABLE
    assert p.classify(float("inf")) == UNAVAILABLE
    assert p.classify("bad") == UNAVAILABLE  # type: ignore[arg-type]
    # 未来戳：容差内视为 fresh（时钟偏斜），超出判 unavailable（防未来分数霸榜 ZSET）
    assert p.classify(-1.0) == FRESH
    assert p.classify(-5.0) == FRESH
    assert p.classify(-5.001) == UNAVAILABLE
    # classify_ts：ts 非正/非数 → unavailable
    assert p.classify_ts(1_000_000, 1_000_010) == FRESH
    assert p.classify_ts(0, 1_000_010) == UNAVAILABLE
    assert p.classify_ts(None, 1_000_010) == UNAVAILABLE
    assert p.is_usable(120) is True and p.is_usable(400) is False
    assert classify_age(120, fresh_within_s=60, stale_within_s=300) == STALE


@pytest.mark.unit
def test_quote_policy_env_precedence_and_legacy_alias(monkeypatch):
    for name in (
        "QM_QUOTE_FRESH_WITHIN_S",
        "QM_QUOTE_STALE_WITHIN_S",
        "SIM_REDIS_QUOTE_MAX_AGE_SEC",
        "PREFLIGHT_SERIES_STALE_THRESHOLD_SEC",
    ):
        monkeypatch.delenv(name, raising=False)
    # 默认：60/300（与历史三处口径一致）
    p = quote_policy()
    assert (p.fresh_within_s, p.stale_within_s) == (60.0, 300.0)
    # 旧别名（simulation 口径）→ stale 窗口
    p = quote_policy(env={"SIM_REDIS_QUOTE_MAX_AGE_SEC": "120"})
    assert p.stale_within_s == 120.0 and p.fresh_within_s == 60.0
    # 旧别名（preflight 口径）→ stale 窗口
    p = quote_policy(env={"PREFLIGHT_SERIES_STALE_THRESHOLD_SEC": "180"})
    assert p.stale_within_s == 180.0
    # 新名优先于旧别名
    p = quote_policy(env={"QM_QUOTE_STALE_WITHIN_S": "90", "SIM_REDIS_QUOTE_MAX_AGE_SEC": "999"})
    assert p.stale_within_s == 90.0
    # 非法值忽略回落默认；stale<fresh 抬齐（且有告警不抛）
    p = quote_policy(env={"QM_QUOTE_FRESH_WITHIN_S": "abc", "QM_QUOTE_STALE_WITHIN_S": "10"})
    assert p.fresh_within_s == 60.0 and p.stale_within_s == 60.0


# ── 2. 源守卫：三处旧阈值零残留 ─────────────────────────────────────


@pytest.mark.unit
def test_source_guard_no_legacy_thresholds():
    legacy_envs = ("SIM_REDIS_QUOTE_MAX_AGE_SEC", "PREFLIGHT_SERIES_STALE_THRESHOLD_SEC")
    offenders: list[str] = []
    for path in (_BACKEND / "services").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        if any(name in source for name in legacy_envs):
            offenders.append(str(path.relative_to(_BACKEND)))
    assert offenders == [], f"旧阈值 env 只允许存在于 shared/freshness.py，残留: {offenders}"

    # 唯一实现：三处消费方必须走 shared.freshness
    consumers = [
        "services/stream/market_app/services/remote_redis_source.py",
        "services/simulation/services/redis_series_quote.py",
        "services/simulation/services/execution_engine.py",
        "services/live_trading/routers/real_trading_utils.py",
    ]
    for rel in consumers:
        source = (_BACKEND / rel).read_text(encoding="utf-8")
        assert "backend.shared.freshness" in source or "shared.freshness" in source, rel

    # stream 旧硬编码 60/300 分级必须消失
    stream_src = (
        _BACKEND / "services/stream/market_app/services/remote_redis_source.py"
    ).read_text(encoding="utf-8")
    assert "age > 60" not in stream_src and "age > 300" not in stream_src

    # freshness 模块是本仓库唯一读取旧别名的位置
    for path in (_BACKEND / "shared").glob("*.py"):
        source = path.read_text(encoding="utf-8")
        if any(name in source for name in legacy_envs):
            assert path.name == "freshness.py", f"旧别名读取散点: {path.name}"


# ── 3. LatencyRecorder（纯逻辑 + 假 Redis）──────────────────────────


class _FakePipe:
    def __init__(self, sink: list):
        self.sink = sink
        self.ops: list = []

    def hset(self, key, mapping=None):
        self.ops.append(("hset", key, mapping))
        return self

    def zadd(self, key, mapping):
        self.ops.append(("zadd", key, mapping))
        return self

    def zremrangebyrank(self, key, start, stop):
        self.ops.append(("zremrangebyrank", key, start, stop))
        return self

    def expire(self, key, ttl):
        self.ops.append(("expire", key, ttl))
        return self

    def execute(self):
        self.sink.append(self.ops)
        return [1] * len(self.ops)


class _FakeClient:
    def __init__(self, fail: bool = False):
        self.executed: list = []
        self.fail = fail
        self.closed = False

    def pipeline(self, transaction=False):
        if self.fail:
            raise ConnectionError("boom")
        return _FakePipe(self.executed)

    def close(self):
        self.closed = True


@pytest.mark.unit
def test_latency_recorder_percentiles_and_counters():
    from backend.shared.latency_metrics import LatencyRecorder

    rec = LatencyRecorder("unit-stage", window_size=1000)
    for i in range(1, 101):
        rec.observe(float(i))
    snap = rec.snapshot()
    assert snap["samples"] == 100
    assert snap["p50_ms"] == 50.0 and snap["p95_ms"] == 95.0
    assert snap["max_ms"] == 100.0 and snap["min_ms"] == 1.0
    assert snap["pending"] == 100
    # 计数：拒绝非数/NaN；负值计 future 但照常入窗
    rec.observe("x")  # type: ignore[arg-type]
    rec.observe(float("nan"))
    rec.observe(-3.5)
    snap = rec.snapshot()
    assert snap["rejected"] == 2 and snap["future"] == 1


@pytest.mark.unit
def test_latency_recorder_flush_threshold_and_payload():
    from backend.shared.latency_metrics import LatencyRecorder

    fake = _FakeClient()
    rec = LatencyRecorder(
        "unit-flush", redis_client=fake, window_size=10, flush_seconds=30, flush_samples=5
    )
    for i in range(4):
        rec.observe(10.0 + i)
    assert rec.maybe_flush() is False, "样本数未到阈值不落盘"
    rec.observe(99.0)
    assert rec.maybe_flush() is True
    assert len(fake.executed) == 1
    ops = fake.executed[0]
    hset = next(op for op in ops if op[0] == "hset")
    assert hset[1] == "intel:latency:unit-flush"
    mapping = hset[2]
    assert mapping["p95_ms"] == "99.0" and mapping["samples"] == "5"
    assert "updated_at" in mapping and mapping["total_count"] == "5"
    assert any(op[0] == "zadd" and op[1].endswith(":series") for op in ops)
    assert any(op[0] == "expire" for op in ops)
    assert rec.maybe_flush() is False, "flush 后 pending 清零，不重复落盘"


@pytest.mark.unit
def test_latency_recorder_flush_failure_never_raises():
    from backend.shared.latency_metrics import LatencyRecorder

    rec = LatencyRecorder("unit-fail", redis_client=_FakeClient(fail=True))
    rec.observe(12.0)
    assert rec.flush() is False
    assert rec.counters["flush_errors"] == 1
    assert rec.counters["last_error"]


@pytest.mark.unit
def test_latency_recorder_fresh_guard_split():
    """新鲜档拆分：陈旧重放帧（>guard）不入验收窗口；flush 双档落键。"""
    from backend.shared.latency_metrics import LatencyRecorder

    fake = _FakeClient()
    rec = LatencyRecorder(
        "unit-guard", redis_client=fake, window_size=100, fresh_guard_ms=1000.0
    )
    rec.observe(100.0)   # 新鲜
    rec.observe(500.0)   # 新鲜
    rec.observe(5000.0)  # 陈旧（>guard）
    rec.observe(-5.0)    # 未来戳：入全量、不计陈旧
    assert rec.counters["stale"] == 1 and rec.counters["future"] == 1
    assert rec.flush() is True
    ops = fake.executed[0]
    hs = {op[1]: op[2] for op in ops if op[0] == "hset"}
    assert hs["intel:latency:unit-guard"]["samples"] == "4"
    assert hs["intel:latency:unit-guard"]["stale_count"] == "1"
    assert hs["intel:latency:unit-guard_fresh"]["samples"] == "2"
    assert hs["intel:latency:unit-guard_fresh"]["p95_ms"] == "500.0"
    # 关闭 guard（None）→ 不落新鲜档键
    fake2 = _FakeClient()
    rec2 = LatencyRecorder("unit-noguard", redis_client=fake2, fresh_guard_ms=None)
    rec2.observe(123.0)
    rec2.flush()
    keys = {op[1] for op in fake2.executed[0] if op[0] == "hset"}
    assert keys == {"intel:latency:unit-noguard"}


# ── 4. preflight 门禁语义（假 Redis）────────────────────────────────


class _FakeRedisLike:
    def __init__(self, score: float | None, *, ping_ok: bool = True):
        self.score = score
        self.ping_ok = ping_ok

    def ping(self):
        if not self.ping_ok:
            raise ConnectionError("remote down")
        return True

    def zrevrange(self, key, start, stop, withscores=True):
        if self.score is None:
            return []
        return [(json.dumps({"price": 40.0}), self.score)]


@pytest.mark.integration
def test_preflight_freshness_three_levels(monkeypatch):
    from backend.services.live_trading.routers import real_trading_utils as rtu

    monkeypatch.setattr(rtu, "_resolve_preflight_symbols", lambda: ["600036.SH"])

    def _with_score(age_s: float | None):
        score = None if age_s is None else time.time() - age_s
        monkeypatch.setattr(
            rtu, "_get_stream_series_redis_client",
            lambda: (_FakeRedisLike(score), "fakehost", 6379),
        )
        return rtu.check_stream_series_freshness()

    res = _with_score(10)
    assert res["ok"] and res["details"]["level"] == "fresh"

    res = _with_score(120)
    assert res["ok"] and res["details"]["level"] == "stale"
    assert "陈旧" in res["message"]

    res = _with_score(400)
    assert not res["ok"] and res["details"]["level"] == "unavailable"
    assert res["details"]["age_seconds"] == 400

    res = _with_score(None)
    assert not res["ok"] and res["message"] == "未发现可用行情序列"


@pytest.mark.integration
def test_preflight_freshness_fallback_to_trade_redis(monkeypatch):
    from backend.services.live_trading.routers import real_trading_utils as rtu

    monkeypatch.setattr(rtu, "_resolve_preflight_symbols", lambda: ["600036.SH"])
    monkeypatch.setattr(
        rtu,
        "_get_stream_series_redis_client",
        lambda: (_FakeRedisLike(None, ping_ok=False), "fakehost", 6379),
    )
    fallback = _FakeRedisLike(time.time() - 5)
    res = rtu.check_stream_series_freshness(redis_client=fallback)
    assert res["ok"] and res["details"]["used_fallback"] is True
    assert res["details"]["level"] == "fresh"


# ── 5. 真 Redis 打点回环 ────────────────────────────────────────────


@pytest.mark.integration
def test_latency_real_redis_roundtrip():
    from backend.shared.latency_metrics import (
        LatencyRecorder,
        _default_client,
        read_all,
        read_latency,
        read_series,
    )

    try:
        client = _default_client()
        client.ping()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Redis 不可达（容器外/未配置）: {exc}")

    stage = f"test-{uuid.uuid4().hex[:8]}"
    hash_key = f"intel:latency:{stage}"
    series_key = f"{hash_key}:series"
    try:
        rec = LatencyRecorder(stage, redis_client=client, flush_seconds=0.01)
        for ms in (10.0, 20.0, 30.0, 40.0):
            rec.observe(ms)
        assert rec.flush() is True
        stats = read_latency(stage)
        assert stats is not None
        assert stats["samples"] == 4 and stats["p95_ms"] == 40.0 and stats["max_ms"] == 40.0
        assert stats["total_count"] == 4
        assert stage in read_all(), "read_all 应枚举到测试 stage"
        series = read_series(stage, limit=5)
        assert len(series) == 1 and series[0]["p95_ms"] == 40.0
    finally:
        client.delete(hash_key, series_key)


# ── 6. SubscriptionEngine 打点 sink 接线 ────────────────────────────


@pytest.mark.integration
def test_engine_latency_sink_observes_frames():
    from backend.shared.latency_metrics import LatencyRecorder
    from backend.shared.tdx_aidata.collector import SubscriptionEngine
    from backend.tests.test_l05_store import _PUSH_SAMPLE

    class _FakeBudget:
        def check(self):
            return None

        def consume(self):
            pass

        def note_rate_limited(self):
            return 60.0

        def note_success(self):
            pass

    recorder = LatencyRecorder("engine-sink-unit")  # 无 client：只观察不落盘
    engine = SubscriptionEngine(
        sdk_subscribe=lambda codes, cb: None,
        sdk_unsubscribe=lambda: None,
        budget_gate=_FakeBudget(),
        redis_factory=None,
        latency=recorder,
    )
    engine.on_push(_PUSH_SAMPLE)
    engine._drain_and_write()
    assert recorder.counters["observed"] == 1
    snap = engine.snapshot()
    assert snap["latency"]["stage"] == "engine-sink-unit"
    assert "p95_ms" in snap["latency"]
