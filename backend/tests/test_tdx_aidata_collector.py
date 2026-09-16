"""TdxAiData 订阅采集（T-P6-02）——映射纯函数与引擎逻辑测试。

覆盖：
1. 帧→Redis 双写映射：标准键（series 前缀大写 / snapshot 小写）+ **消费方字段契约**
   （Now/Open/PreClose/timestamp 必写——见 stream RemoteRedisDataSource）；
2. refresh_time(HHMMSS) → epoch 秒换算（CST）+ 跨日守卫；
3. 热集差分（增量 subscribe/unsubscribe 计划 + 上限截断）；
4. 静默判定（断流重订阅心跳）；
5. 非 A 股/非法符号跳过敏感统计。
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
    "bid1": 40.91, "bid2": 40.90, "bid3": 40.89, "bid4": 40.88, "bid5": 40.87,
    "bid_vol1": 12.0, "bid_vol2": 160.0, "bid_vol3": 186.0, "bid_vol4": 169.0, "bid_vol5": 969.0,
    "ask1": 40.92, "ask2": 40.93, "ask3": 40.94, "ask4": 40.95, "ask5": 40.96,
    "ask_vol1": 182.0, "ask_vol2": 5.0, "ask_vol3": 16.0, "ask_vol4": 66.0, "ask_vol5": 27.0,
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
    assert datetime.fromtimestamp(ts, tz=_CST).strftime("%F %T") == "2026-09-16 15:30:53"

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
    assert collector.normalize_subscription_symbol("600036.SH") == ("600036.SH", "SH600036")
    # 已带前缀（大写/小写）都能归一
    assert collector.normalize_subscription_symbol("SH600036") == ("600036.SH", "SH600036")
    assert collector.normalize_subscription_symbol("000858.SZ") == ("000858.SZ", "SZ000858")
    # 非法 → None（调用方计数跳过，绝不猜测）
    assert collector.normalize_subscription_symbol("") is None
    assert collector.normalize_subscription_symbol("NOT-A-SYMBOL-XYZ") is None
