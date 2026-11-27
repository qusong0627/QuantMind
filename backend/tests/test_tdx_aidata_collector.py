"""TdxAiData 订阅采集（T-P6-02）——映射纯函数与引擎逻辑测试。

覆盖：
1. 帧→Redis 双写映射：标准键（series 前缀大写 / snapshot 小写）+ **消费方字段契约**
   （Now/Open/PreClose/timestamp 必写——见 stream RemoteRedisDataSource）；
2. refresh_time(HHMMSS) → epoch 秒换算（CST）+ 跨日守卫；
3. 热集差分（增量 subscribe/unsubscribe 计划 + 上限截断）；
4. 静默判定（断流重订阅心跳）；
5. 非 A 股/非法符号跳过敏感统计；
6. 夜间重放积压修复（2026-10-14 OOM 事故）：有界队列保新弃旧 / 入队时效闸门 /
   单 tick 连续排空追平。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

# 真机样本（与 test_tdx_aidata_channel._PUSH_SAMPLE 同源，单独维护避免跨文件耦合）
_RECORD = {
    "symbol": "600036.SH",
    "price": 40.92,
    "pre_close": 41.10,
    "open": 41.15,
    "high": 41.41,
    "low": 40.52,
    "refresh_time": "153053",
    "volume": 538019.0,
    "limit_up": 45.21,
    "limit_down": 36.99,
    "seal_amount": 0.0,
    "bid1": 40.91,
    "bid2": 40.90,
    "bid3": 40.89,
    "bid4": 40.88,
    "bid5": 40.87,
    "bid_vol1": 12.0,
    "bid_vol2": 160.0,
    "bid_vol3": 186.0,
    "bid_vol4": 169.0,
    "bid_vol5": 969.0,
    "ask1": 40.92,
    "ask2": 40.93,
    "ask3": 40.94,
    "ask4": 40.95,
    "ask5": 40.96,
    "ask_vol1": 182.0,
    "ask_vol2": 5.0,
    "ask_vol3": 16.0,
    "ask_vol4": 66.0,
    "ask_vol5": 27.0,
}

_CST = timezone(timedelta(hours=8))


@pytest.mark.unit
def test_record_to_redis_keys_and_consumer_contract():
    from backend.shared.tdx_aidata import collector

    now = datetime(2026, 9, 16, 15, 30, 53, tzinfo=_CST)
    out = collector.frame_to_redis(record=_RECORD, now=now)

    # 标准键：series=前缀大写，snapshot=前缀小写（与 tdx_quote_feed/stream 一致）
    assert out["series_key"] == "market:series:SH600036"
    assert out["snapshot_key"] == "market:snapshot:sh600036"
    assert out["snapshot_source_key"] == "market:snapshot:tdx:sh600036"

    snap = out["snapshot_fields"]
    # 消费方必读字段（RemoteRedisDataSource：Now/Open/PreClose/timestamp）
    assert float(snap["Now"]) == pytest.approx(40.92)
    assert float(snap["Open"]) == pytest.approx(41.15)
    assert float(snap["PreClose"]) == pytest.approx(41.10)
    assert float(snap["High"]) == pytest.approx(41.41)
    assert float(snap["Low"]) == pytest.approx(40.52)
    assert float(snap["Volume"]) == pytest.approx(538019)
    # timestamp = 该帧 refresh_time 的 epoch 秒
    assert int(snap["timestamp"]) == int(now.timestamp())
    assert snap["source"] == "tdx_aidata_sub"
    # 原始推送字段同 Hash（五档等，供 F2/前端）
    assert float(snap["bid1"]) == pytest.approx(40.91)
    assert float(snap["ask5"]) == pytest.approx(40.96)

    series = out["series_payload"]
    assert series["symbol"] == "SH600036"
    assert float(series["price"]) == pytest.approx(40.92)
    assert series["is_stale"] is False
    assert series["source"] == "tdx_aidata_sub"
    assert float(series["limit_up"]) == pytest.approx(45.21)
    assert float(series["bid1"]) == pytest.approx(40.91)
    assert series["timestamp"] == int(now.timestamp())


@pytest.mark.unit
def test_refresh_time_epoch_with_rollover_guard():
    from backend.shared.tdx_aidata import collector

    # 正常：当天 15:30:53
    now = datetime(2026, 9, 16, 20, 0, 0, tzinfo=_CST)
    ts = collector.refresh_time_to_epoch("153053", now)
    assert (
        datetime.fromtimestamp(ts, tz=_CST).strftime("%F %T") == "2026-09-16 15:30:53"
    )

    # 跨日守卫：凌晨 00:00:30 的进程收到昨日 15:30 帧（理论不会，但守卫必须显式）——
    # 解析结果若晚于 now 超过 5 分钟，回退一天
    now2 = datetime(2026, 9, 16, 0, 0, 30, tzinfo=_CST)
    ts2 = collector.refresh_time_to_epoch("235959", now2)
    assert ts2 <= now2.timestamp()

    # 非法输入返回 None（不假造时间）
    assert collector.refresh_time_to_epoch("", now) is None
    assert collector.refresh_time_to_epoch("abc", now) is None


@pytest.mark.unit
def test_hot_set_diff_and_cap():
    from backend.shared.tdx_aidata import collector

    add, remove = collector.hot_set_diff(
        current={"600036.SH", "000858.SZ"}, desired={"000858.SZ", "601318.SH"}
    )
    assert add == {"601318.SH"}
    assert remove == {"600036.SH"}
    assert collector.hot_set_diff(current=set(), desired=set()) == (set(), set())

    # 上限截断：desired 超 cap → 排序截断（确定性），被截断的不在 add 中
    desired = {f"60000{i}.SH" for i in range(10)}
    add2, _ = collector.hot_set_diff(current=set(), desired=desired, cap=5)
    assert len(add2) == 5


@pytest.mark.unit
def test_silence_detection():
    from backend.shared.tdx_aidata import collector

    now = 1000.0
    # 有帧且 10s 前 → 正常
    assert collector.is_silent(last_frame_ts=now - 10, now=now, silence_s=120) is False
    # 静默 130s → 判定断流（需重订阅）
    assert collector.is_silent(last_frame_ts=now - 130, now=now, silence_s=120) is True
    # 从未收帧（订阅刚建立）→ 不判静默（给建立期宽限由调用方控制）
    assert collector.is_silent(last_frame_ts=None, now=now, silence_s=120) is False


@pytest.mark.unit
def test_symbol_normalization_and_skip_counter():
    from backend.shared.tdx_aidata import collector

    # 返回 (原生订阅码[后缀式，真机 subscribe 实测用法], 标准键前缀[大写])
    assert collector.normalize_subscription_symbol("600036.SH") == (
        "600036.SH",
        "SH600036",
    )
    # 已带前缀（大写/小写）都能归一
    assert collector.normalize_subscription_symbol("SH600036") == (
        "600036.SH",
        "SH600036",
    )
    assert collector.normalize_subscription_symbol("000858.SZ") == (
        "000858.SZ",
        "SZ000858",
    )
    # 非法 → None（调用方计数跳过，绝不猜测）
    assert collector.normalize_subscription_symbol("") is None
    assert collector.normalize_subscription_symbol("NOT-A-SYMBOL-XYZ") is None


@pytest.mark.unit
def test_push_error_kind_classifier():
    """推送解析异常 → 稳定分类键（2026-10-14：区分服务端错误信封与本地解析坏了）。

    真机背景：约 1% 推送帧是服务端错误信封（ErrorId=10 + Error 文本携带
    RetFlag=-5397），此前只计 parse_errors 一个总数——事后无法区分「服务端
    偶发」与「本地解析器坏了」。分类键是这两种故障的判据来源。
    """
    from backend.shared.tdx_aidata import protocol

    # 真机形态：服务端错误信封（ErrorId=10 + Error 文本携带 RetFlag=-5397）
    assert (
        protocol.push_error_kind(
            "推送错误 ErrorId=10: 订阅服务器返回错误 RetFlag=-5397"
        )
        == "server_errorid_10_retflag_-5397"
    )
    # 有 ErrorId 无 RetFlag（如 ErrorId=11 连接失败）
    assert protocol.push_error_kind("推送错误 ErrorId=11: None") == "server_errorid_11"
    # Token 配额失败是负数 ErrorId——必须能归类（2026-10-20 实测 ErrorId=-2）
    assert (
        protocol.push_error_kind("推送错误 ErrorId=-2: Token Insufficient")
        == "server_errorid_-2"
    )
    # 本地解析失败两类
    assert protocol.push_error_kind("推送非 JSON: Expecting value") == "non_json"
    assert protocol.push_error_kind("推送类型不支持: bytes") == "unsupported_type"
    # 未知形态兜底（不猜）
    assert protocol.push_error_kind(None) == "other"


@pytest.mark.unit
def test_on_push_error_envelope_counted_and_kept_as_evidence():
    """on_push 解析失败：分类计数 + 出错原文留证（成功帧不覆盖）。

    旧实现只有 last_frame_preview——下一帧成功推就把它覆盖了，ErrorId=10/-5397
    这种 ~1% 的偶发信封事后无证据可查；last_parse_error_preview 是出错侧独立留证位。
    """
    import json

    from backend.shared.tdx_aidata.collector import SubscriptionEngine

    engine = SubscriptionEngine(
        sdk_subscribe=lambda codes, cb: None,
        sdk_unsubscribe=lambda: None,
        budget_gate=None,
        redis_factory=None,
    )
    raw_err = json.dumps({"ErrorId": 10, "Error": "订阅服务器返回错误 RetFlag=-5397"})
    assert engine.on_push(raw_err) == 1  # 计数跳过，帧流继续
    assert engine.counters["parse_errors"] == 1
    assert engine.counters["parse_error_codes"] == {
        "server_errorid_10_retflag_-5397": 1
    }
    assert "RetFlag=-5397" in engine.counters["last_parse_error_preview"]

    # 后续成功帧：frames 走通、出错留证不被覆盖（两个键互不干扰）
    ok = json.dumps(
        {
            "ErrorId": 0,
            "ResultSets": [
                {"ColDes": ["code", "price"], "Content": [["600036.SH", "40.92"]]}
            ],
        }
    )
    engine.on_push(ok)
    assert engine.counters["frames"] == 1
    assert engine.counters["last_frame_records"] == 1
    assert "RetFlag=-5397" in engine.counters["last_parse_error_preview"]

    # 非 JSON（本地解析坏了）单独归类，不与服务端信封混计
    engine.on_push("not-json")
    assert engine.counters["parse_error_codes"]["non_json"] == 1
    assert engine.counters["parse_errors"] == 2


# ── 6. 夜间重放积压修复（2026-10-14 OOM 事故）────────────────────────
#
# 事故链条：服务端夜间仍以 ~2.8 帧/s 重推全量快照（≈235 条/s/worker），旧实现
# 无界 SimpleQueue + 每 15s 只排一批 500 条（≈33 条/s 隐性节流）→ 队列只涨不消
# → 8 个 worker 6 小时攒 38GB → 容器 memcg OOM 杀掉同容器因子挖掘任务。


class _CapturingPipe:
    """记录 hset 键的假 pipeline（只验证「哪些键被写」；值与 TTL 不在本测范围）。"""

    def __init__(self, sink: list[str]) -> None:
        self._sink = sink

    def hset(self, key, mapping=None):
        self._sink.append(key)

    def expire(self, key, ttl):
        pass

    def zadd(self, key, mapping):
        pass

    def zremrangebyrank(self, key, start, end):
        pass

    def execute(self):
        pass


class _CapturingRedis:
    def __init__(self, sink: list[str]) -> None:
        self._sink = sink

    def pipeline(self, transaction=False):
        return _CapturingPipe(self._sink)

    def close(self):
        pass


def _frame_json(records: list[dict]) -> str:
    """记录列表 → 真机形态推送帧（ColDes/Content 列对齐）。"""
    import json

    cols = sorted({k for r in records for k in r})
    return json.dumps(
        {
            "ErrorId": 0,
            "ResultSets": [
                {
                    "ColDes": cols,
                    "Content": [[r.get(c) for c in cols] for r in records],
                }
            ],
        }
    )


def _make_engine(**overrides):
    from backend.shared.tdx_aidata.collector import SubscriptionEngine

    kwargs = {
        "sdk_subscribe": lambda codes, cb: None,
        "sdk_unsubscribe": lambda: None,
        "budget_gate": None,
        "redis_factory": None,
    }
    kwargs.update(overrides)
    return SubscriptionEngine(**kwargs)


@pytest.mark.unit
def test_queue_bounded_drops_oldest_when_full():
    """队列有界背压：满时保新弃旧（旧无界队列 6h 攒 38GB 的根治面）。"""
    engine = _make_engine(max_queue_records=3, max_record_age_s=None)
    codes = [f"60000{i}.SH" for i in range(1, 6)]
    engine.on_push(
        _frame_json([{"code": c, "price": "10.0", "pre_close": "9.9"} for c in codes])
    )

    assert len(engine._queue) == 3  # 硬上限
    assert [r["code"] for r in engine._queue] == codes[2:]  # 保新弃旧
    assert engine.counters["queue_dropped"] == 2
    snap = engine.snapshot()
    assert snap["queue_depth"] == 3
    assert snap["queue_max"] == 3


@pytest.mark.unit
def test_enqueue_age_gate_drops_stale_replay():
    """入队时效闸门（与归档同口径 300s）：陈旧重放帧不进队列，实时帧照常入队。"""
    engine = _make_engine()
    now_cst = datetime.now(_CST)
    stale_rt = (now_cst - timedelta(hours=3)).strftime("%H%M%S")  # 夜间重放形态
    fresh_rt = now_cst.strftime("%H%M%S")
    row = {"code": "600036.SH", "price": "40.92", "pre_close": "41.10"}

    engine.on_push(_frame_json([{**row, "refresh_time": stale_rt}]))
    assert len(engine._queue) == 0
    assert engine.counters["enqueue_stale_dropped"] == 1
    # 帧到达仍计数（服务端活着）——静默判定不受丢弃影响
    assert engine.counters["frames"] == 1
    assert engine.last_frame_ts is not None

    engine.on_push(_frame_json([{**row, "refresh_time": fresh_rt}]))
    assert len(engine._queue) == 1
    assert engine.counters["enqueue_stale_dropped"] == 1  # 未增加


@pytest.mark.unit
def test_drain_backlog_catches_up_across_batches():
    """连续排空：单 tick 跨多批追平积压（旧实现每 tick 一批 500 条=33 条/s 节流）。"""
    written_keys: list[str] = []
    engine = _make_engine(
        redis_factory=lambda: _CapturingRedis(written_keys),
        max_record_age_s=None,
    )
    codes = [f"60{i:04d}.SH" for i in range(1200)]
    engine.on_push(
        _frame_json([{"code": c, "price": "10.0", "pre_close": "9.9"} for c in codes])
    )
    assert engine.snapshot()["queue_depth"] == 1200  # 远超单批 500

    assert engine._drain_backlog() is True  # 一趟见底
    assert len(engine._queue) == 0
    assert engine.counters["written"] == 1200
    # V3：每 tick 写 canonical + tdx 来源键，共 2400 次 hset
    assert len(written_keys) == 2400
    canonical = [
        key for key in written_keys
        if key.startswith("market:snapshot:")
        and not key.startswith("market:snapshot:tdx:")
    ]
    source = [key for key in written_keys if key.startswith("market:snapshot:tdx:")]
    assert len(canonical) == 1200
    assert len(source) == 1200


# ── 7. 配额/鉴权退避（2026-10-20：Token Insufficient 重订风暴）──────────
#
# 事故链条：2026-10-17 起订阅推送回 ErrorId=-2: Token Insufficient，旧实现只当
# 普通解析错误计数；静默 120s 后每 60s 强制重订一次，8 分片 × 3 天累计 44,560
# 次重订，配额始终不足。修复：识别 rate_limited → 指数退避（5min→1h），退避期间
# 不读热集、不重订；收到真实数据帧即清零。


@pytest.mark.unit
def test_token_insufficient_enters_auth_backoff_and_blocks_resubscribe(monkeypatch):
    from types import SimpleNamespace

    from backend.shared.tdx_aidata import collector

    clock = {"t": 1_000_000.0}
    # 只替换 collector 模块的 time 引用（不碰全局 time 模块）
    monkeypatch.setattr(collector, "time", SimpleNamespace(time=lambda: clock["t"]))
    engine = _make_engine(hot_set_reader=lambda: {"600036.SH"})
    engine._current = {"600036.SH"}

    engine.on_push('{"ErrorId": -2, "Error": "Token Insufficient"}')

    snap = engine.snapshot()
    assert snap["auth_backoff"]["active"] is True
    assert snap["auth_backoff"]["duration_s"] == 300.0
    assert snap["auth_backoff"]["retry_in_s"] == 300.0
    assert engine.counters["auth_backoff_entries"] == 1

    # 退避期间热集同步直接跳过（不读热集、不重订）
    out = engine.sync_hot_set_once()
    assert out["reason"] == "auth_backoff"
    assert out["retry_in_s"] == 300.0
    assert engine.counters["auth_backoff_skipped"] == 1

    # 静默强制重订同样被拦下（时钟越过最小间隔也不重订）
    engine.last_frame_ts = clock["t"] - 200  # 已静默 200s > silence_s
    assert engine._should_force_resubscribe(clock["t"] + 120) is False

    # 退避到期后恢复同步
    clock["t"] += 301
    assert engine._should_force_resubscribe(clock["t"]) is True
    out = engine.sync_hot_set_once()
    assert out.get("reason") != "auth_backoff"


@pytest.mark.unit
def test_auth_backoff_doubles_to_cap_and_resets_on_data_frame(monkeypatch):
    from types import SimpleNamespace

    from backend.shared.tdx_aidata import collector

    clock = {"t": 2_000_000.0}
    monkeypatch.setattr(collector, "time", SimpleNamespace(time=lambda: clock["t"]))
    engine = _make_engine()

    err = '{"ErrorId": -2, "Error": "Token Insufficient"}'
    durations = []
    for _ in range(6):
        engine.on_push(err)
        durations.append(engine.snapshot()["auth_backoff"]["duration_s"])
        clock["t"] += engine.auth_backoff_remaining_s() + 1

    # 300 → 600 → 1200 → 2400 → 3600（封顶）→ 3600
    assert durations == [300.0, 600.0, 1200.0, 2400.0, 3600.0, 3600.0]

    # 真实数据帧到达 → 退避清零，下一轮重新从 300 起
    engine.on_push(_frame_json([{"code": "600036.SH", "price": "40.92"}]))
    snap = engine.snapshot()
    assert snap["auth_backoff"]["active"] is False
    assert snap["auth_backoff"]["duration_s"] == 0.0

    engine.on_push(err)
    assert engine.snapshot()["auth_backoff"]["duration_s"] == 300.0


@pytest.mark.unit
def test_resubscribe_rate_limited_enters_backoff_without_budget_gate(monkeypatch):
    """订阅调用本身报 Token Insufficient → 立即退避；budget_gate=None 不得炸。"""
    from types import SimpleNamespace

    from backend.shared.tdx_aidata import collector

    monkeypatch.setattr(collector, "time", SimpleNamespace(time=lambda: 3_000_000.0))

    def _raise(codes, cb):
        raise RuntimeError("Code=13 Insufficient token")

    engine = _make_engine(sdk_subscribe=_raise, budget_gate=None)

    assert engine.resubscribe({"600036.SH"}) is False
    assert engine.counters["auth_backoff_entries"] == 1
    assert engine.snapshot()["auth_backoff"]["active"] is True
