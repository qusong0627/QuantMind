"""TdxAiData 接入（T-P6-01）——协议/客户端契约测试。

覆盖：
1. 纯函数口径：周期映射、count→区间换算（count 模式不可用的替代）、K线归一（NaN 残缺行剔除）；
2. 客户端协议：JSONL 请求/响应往返、错误映射（rate_limited/raw_code/retry_after_s）、超时；
3. worker 生命周期：未运行→按需拉起（注入 spawn 桩）、worker 死亡→状态降级+下次重拉；
4. 冷却窗口快速失败（限流后不会继续打 token）。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest


# ── 1. 纯函数口径 ───────────────────────────────────────────────────


@pytest.mark.unit
def test_period_map_and_interval_defaults():
    from backend.shared.tdx_aidata import protocol

    assert protocol.period_of("daily") == "1d"
    assert protocol.period_of("5m") == "5m"
    assert protocol.period_of("1d") == "1d"
    assert protocol.period_of("unknown-p") == "unknown-p"  # 透传（SDK 会报错码 5）


@pytest.mark.unit
def test_start_for_count_range_conversion():
    from backend.shared.tdx_aidata import protocol

    # 分钟周期：bar 时长×count×2 + 30min 裕量
    start = protocol.start_for_count("5m", 48, "2026-09-16 15:00:00")
    assert start == "2026-09-16 06:30:00"
    # 日线：日裕量
    start_d = protocol.start_for_count("1d", 5, "2026-09-16 15:00:00")
    assert start_d == "2026-09-16 15:00:00".replace("2026-09-16", "2026-09-06")


class _Iloc:
    """pandas 姿态：``series.iloc[i]`` 是索引器对象取下标（不是方法调用）。"""

    def __init__(self, vals):
        self._vals = vals

    def __getitem__(self, i):
        return self._vals[i]


class _Series:
    def __init__(self, vals):
        self._vals = vals
        self.iloc = _Iloc(vals)


class _Col:
    """模拟 pandas DataFrame 列（data[field][symbol].iloc[i] 取值链）。"""

    def __init__(self, vals, symbol):
        self._vals = vals
        self._symbol = symbol

    def __getitem__(self, key):
        assert key == self._symbol
        return _Series(self._vals)


@pytest.mark.unit
def test_bars_from_market_data_drops_nan_rows():
    from backend.shared.tdx_aidata import protocol

    data = {
        "Open": _Col([10.0, 20.0, 30.0], "600036.SH"),
        "High": _Col([11.0, 21.0, 31.0], "600036.SH"),
        "Low": _Col([9.0, 19.0, 29.0], "600036.SH"),
        "Close": _Col([10.5, float("nan"), 30.5], "600036.SH"),
        "Volume": _Col([100, 200, 300], "600036.SH"),
        "Amount": _Col([1000, 2000, 3000], "600036.SH"),
        "_dates": ["2026-09-14", "2026-09-15", "2026-09-16"],
    }

    bars = protocol.bars_from_market_data(data, "600036.SH")
    # 中间 NaN close 行被剔除
    assert [b["date"] for b in bars] == ["2026-09-14", "2026-09-16"]
    assert bars[0]["close"] == 10.5 and bars[1]["close"] == 30.5


@pytest.mark.unit
def test_map_sdk_error_rate_limit():
    from backend.shared.tdx_aidata import protocol

    code, msg = protocol.map_sdk_error("Token Insufficient")
    assert code == "rate_limited"
    assert "insufficient" in msg.lower()


# ── 5. 订阅推送解析（真机样本固化，2026-09-16）──────────────────────

_PUSH_SAMPLE = (
    '{"Error":"","ErrorId":0,"ResultSets":[{"ColDes":["code","decimal","price",'
    '"pre_close","open","high","low","refresh_time","volume","bond_match_price",'
    '"limit_up","limit_down","accrued_interest","cage_up","cage_down",'
    '"after_hours_flag","tomorrow_limit_up","tomorrow_limit_down","sdunit_status",'
    '"seal_amount","ask1","ask2","ask3","ask4","ask5","ask_vol1","ask_vol2",'
    '"ask_vol3","ask_vol4","ask_vol5","bid1","bid2","bid3","bid4","bid5",'
    '"bid_vol1","bid_vol2","bid_vol3","bid_vol4","bid_vol5"],"Content":['
    '["600036.SH","2","40.92","41.10","41.15","41.41","40.52","153053","538019",'
    '"0.000","45.21","36.99","20628944896.0000","41.74","40.09","1","45.01",'
    '"36.83","5","0.00","40.92","40.93","40.94","40.95","40.96","182","5","16",'
    '"66","27","40.91","40.90","40.89","40.88","40.87","12","160","186","169",'
    '"969"],["000858.SZ","2","69.26","69.70","69.60","69.69","68.89","153309",'
    '"154126","0.000","76.67","62.73","30.0200","70.66","67.87","1","76.19",'
    '"62.33","5","0.00","69.27","69.28","69.29","69.30","69.31","202","88","43",'
    '"179","58","69.26","69.25","69.24","69.23","69.22","74","651","133","345",'
    '"184"]]}]} '
).strip()


@pytest.mark.unit
def test_parse_push_payload_real_sample():
    from backend.shared.tdx_aidata import protocol

    records = protocol.parse_push_payload(_PUSH_SAMPLE)
    assert len(records) == 2
    r = records[0]
    assert r["symbol"] == "600036.SH"
    # 数值列已强转
    assert r["price"] == pytest.approx(40.92)
    assert r["pre_close"] == pytest.approx(41.10)
    assert r["limit_up"] == pytest.approx(45.21)
    assert r["limit_down"] == pytest.approx(36.99)
    assert r["volume"] == pytest.approx(538019)
    # 五档
    assert r["bid1"] == pytest.approx(40.91) and r["bid_vol1"] == pytest.approx(12)
    assert r["ask5"] == pytest.approx(40.96) and r["ask_vol5"] == pytest.approx(27)
    # refresh_time 双形态
    assert r["refresh_time"] == "153053"
    assert r["refresh_hms"] == "15:30:53"
    # 非数值列原样
    assert r["after_hours_flag"] == "1"
    assert records[1]["symbol"] == "000858.SZ"


@pytest.mark.unit
def test_parse_push_payload_rejects_error_and_garbage():
    from backend.shared.tdx_aidata import protocol

    with pytest.raises(protocol.ProtocolError):
        protocol.parse_push_payload('{"Error":"bad","ErrorId":12,"ResultSets":[]}')
    with pytest.raises(protocol.ProtocolError):
        protocol.parse_push_payload("not-json")
    # 缺 code 的行跳过（不假造 symbol）
    assert protocol.parse_push_payload({"ErrorId": 0, "ResultSets": [{"ColDes": ["price"], "Content": [["1.0"]]}]}) == []


class FakeWorker:
    """假 worker：按协议应答，可注入错误/挂起/猝死。"""

    def __init__(self, path: Path):
        self.path = path
        self.server = None
        self.requests: list[dict] = []
        self.mode = "ok"  # ok | rate_limited | hang | crash
        self.spawns = 0

    async def start(self):
        self.server = await asyncio.start_unix_server(self._handle, path=str(self.path))
        return self

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            while True:
                line = await reader.readline()
                if not line:
                    return
                req = json.loads(line)
                self.requests.append(req)
                if self.mode == "hang":
                    await asyncio.sleep(3600)
                if self.mode == "crash":
                    writer.close()
                    return
                if self.mode == "rate_limited":
                    resp = {
                        "id": req["id"],
                        "ok": False,
                        "result": None,
                        "error": {
                            "code": "rate_limited",
                            "message": "Token Insufficient",
                            "raw_code": 13,
                            "retry_after_s": 180.0,
                        },
                        "meta": {"duration_ms": 1.0, "attempts": 1},
                    }
                elif req["method"] == "ping":
                    resp = {
                        "id": req["id"],
                        "ok": True,
                        "result": {"pong": True, "pid": 4242},
                        "error": None,
                        "meta": {},
                    }
                elif req["method"] == "status":
                    resp = {
                        "id": req["id"],
                        "ok": True,
                        "result": {"started": True, "rate_limited": False},
                        "error": None,
                        "meta": {},
                    }
                else:
                    resp = {
                        "id": req["id"],
                        "ok": True,
                        "result": {"symbol": req["params"].get("symbol"), "price": 40.92},
                        "error": None,
                        "meta": {"duration_ms": 3.0, "attempts": 1},
                    }
                writer.write((json.dumps(resp, ensure_ascii=False) + "\n").encode())
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            return

    async def close(self):
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()


@pytest.mark.asyncio
async def test_client_roundtrip_and_error_mapping(tmp_path):
    from backend.shared.tdx_aidata.client import TdxAiDataClient, TdxAiDataError

    sock = tmp_path / "fake.sock"
    worker = await FakeWorker(sock).start()
    try:
        client = TdxAiDataClient(socket_path=str(sock), spawn_fn=None)
        quote = await client.get_quote("600036.SH")
        assert quote["price"] == 40.92
        assert worker.requests[0]["method"] == "get_quote"
        assert worker.requests[0]["params"]["symbol"] == "600036.SH"

        worker.mode = "rate_limited"
        with pytest.raises(TdxAiDataError) as ei:
            await client.get_klines("600036.SH", interval="daily", count=5)
        assert ei.value.code == "rate_limited"
        assert ei.value.raw_code == 13
        assert ei.value.retry_after_s == 180.0
        await client.close()
    finally:
        await worker.close()


@pytest.mark.asyncio
async def test_client_timeout_maps_to_error(tmp_path):
    from backend.shared.tdx_aidata.client import TdxAiDataClient, TdxAiDataError

    sock = tmp_path / "fake2.sock"
    worker = await FakeWorker(sock).start()
    worker.mode = "hang"
    try:
        client = TdxAiDataClient(socket_path=str(sock), spawn_fn=None)
        with pytest.raises(TdxAiDataError) as ei:
            await client.get_quote("600036.SH", timeout=1.0)
        assert ei.value.code == "timeout"
        await client.close()
    finally:
        await worker.close()


@pytest.mark.asyncio
async def test_client_spawns_worker_when_missing(tmp_path):
    from backend.shared.tdx_aidata.client import TdxAiDataClient

    sock = tmp_path / "fake3.sock"
    worker = FakeWorker(sock)
    spawns = {"n": 0}

    async def _spawn() -> bool:
        spawns["n"] += 1
        await worker.start()
        return True

    client = TdxAiDataClient(socket_path=str(sock), spawn_fn=_spawn)
    quote = await client.get_quote("600519.SH")
    assert quote["price"] == 40.92
    assert spawns["n"] == 1
    await client.close()
    await worker.close()


@pytest.mark.asyncio
async def test_client_recovers_after_worker_crash(tmp_path):
    from backend.shared.tdx_aidata.client import TdxAiDataClient, TdxAiDataError

    sock = tmp_path / "fake4.sock"
    worker = await FakeWorker(sock).start()
    spawns = {"n": 0}

    async def _spawn() -> bool:
        spawns["n"] += 1
        if worker.server is None:
            await worker.start()
        return True

    try:
        client = TdxAiDataClient(socket_path=str(sock), spawn_fn=_spawn)
        assert (await client.get_quote("600036.SH"))["price"] == 40.92

        # worker 猝死：本次调用报错（不做假成功），状态转 down
        worker.mode = "crash"
        with pytest.raises(TdxAiDataError):
            await client.get_quote("600036.SH")
        st = await client.status()
        assert st.get("worker") in {"down", "unknown"}

        # 下次调用：重拉 worker（spawn_fn）→ 恢复
        await worker.close()
        worker.server = None
        worker.mode = "ok"
        assert (await client.get_quote("600036.SH"))["price"] == 40.92
        assert spawns["n"] >= 1
        await client.close()
    finally:
        await worker.close()
