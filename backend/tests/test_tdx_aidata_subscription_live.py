"""TdxAiData 订阅采集（T-P6-02）真机 E2E：订阅 → 帧落标准键 → 逐值对拍。

机构纪律：
- 测试独立热集键（env QM_HOT_SET_KEY）与独立 worker socket，绝不触碰生产热集；
- **逐值对拍**：Redis 快照/时序的落地值与推送帧内部一致性 + 涨跌停包络合理性；
- 限流/静默等外部态如实 skip（携带证据），绝不假绿；
- 结束清理：worker SIGTERM + 测试键 DEL + 热集键 DEL。
"""

from __future__ import annotations

import os
import signal
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

_ISOLATED_SOCKET = "/tmp/qm-tdx-aidata-sub-e2e.sock"
_CST = timezone(timedelta(hours=8))
_SYMBOLS = ["600036.SH", "000858.SZ"]


def _remote_redis():
    import redis as _redis

    from backend.shared.remote_quote_config import resolve_remote_quote_redis

    resolved = resolve_remote_quote_redis()
    if resolved is None:
        return None
    host, port, password, db = resolved
    return _redis.Redis(
        host=host, port=port, password=password, db=db,
        socket_connect_timeout=3, socket_timeout=5, decode_responses=True,
    )


def _hot_set_redis():
    """热集读取侧客户端（**部署本地 Redis**，与 worker 读取侧同源）。"""
    from backend.shared.hot_set_store import make_hot_set_client

    return make_hot_set_client()


@pytest_asyncio.fixture
async def sub_env():
    """隔离环境：独立热集键 + 订阅开关（env 由子进程继承）+ 独立 socket。"""
    from backend.shared.tdx_aidata import config
    from backend.shared.tdx_aidata.client import TdxAiDataClient

    if not config.is_enabled():
        pytest.skip("TdxAiData 已显式禁用")
    if not config.dir_ready(config.resolve_dir()):
        pytest.skip(f"安装目录不完整: {config.resolve_dir()}")

    hot_set_key = f"qm:hot_set:test-{uuid.uuid4().hex[:8]}"
    os.environ["QM_HOT_SET_KEY"] = hot_set_key
    os.environ["TDX_AIDATA_SUBSCRIBE_ENABLED"] = "1"
    os.environ["QM_HOT_SET_SYNC_S"] = "3"  # 加速测试节拍

    client = TdxAiDataClient(socket_path=_ISOLATED_SOCKET)
    try:
        yield {"client": client, "hot_set_key": hot_set_key}
    finally:
        os.environ.pop("TDX_AIDATA_SUBSCRIBE_ENABLED", None)
        os.environ.pop("QM_HOT_SET_KEY", None)
        os.environ.pop("QM_HOT_SET_SYNC_S", None)
        try:
            st = await client.status()
            pid = (st or {}).get("pid")
            if isinstance(pid, int) and pid > 1:
                os.kill(pid, signal.SIGTERM)
        except Exception:  # noqa: BLE001
            pass
        await client.close()
        r = _remote_redis()
        if r is not None:
            try:
                for sym in _SYMBOLS:
                    prefix = sym.split(".")[0]
                    r.delete(f"market:snapshot:{prefix.lower() if sym.endswith('.SH') or sym.endswith('.SZ') else prefix}")
                for sym in _SYMBOLS:
                    code, market = sym.split(".")
                    r.delete(f"market:snapshot:{market.lower()}{code}")
                    r.delete(f"market:series:{market}{code}")
                r.close()
            except Exception:  # noqa: BLE001
                pass
        hs = _hot_set_redis()
        try:
            hs.delete(hot_set_key)
        except Exception:  # noqa: BLE001
            pass
        finally:
            try:
                hs.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            os.unlink(_ISOLATED_SOCKET)
        except OSError:
            pass


@pytest.mark.integration
@pytest.mark.asyncio
async def test_subscription_writes_standard_keys_with_value_crosscheck(sub_env):
    import asyncio
    import json

    from backend.shared.tdx_aidata import collector

    client = sub_env["client"]
    hot_set_key = sub_env["hot_set_key"]

    r = _remote_redis()
    assert r is not None, "远端行情 Redis 不可解析（订阅写侧依赖）"
    hs = _hot_set_redis()
    try:
        # 1) 播种热集（隔离键；**部署本地 Redis**——worker 读取侧同源）
        hs.sadd(hot_set_key, *_SYMBOLS)

        # 2) 拉起 worker（订阅引擎随启动挂载）
        assert await client.ensure_worker(), "worker 拉起失败"
        st = await client.status()
        assert st.get("sdk_ready") is True, st

        # 3) 等待订阅建立（引擎首轮同步 ≤3s；订阅受预算闸门约束，可能需等窗口）
        subscribed = False
        for _ in range(40):  # 最多 ~80s
            await asyncio.sleep(2)
            sub = await client.subscription_status()
            if sub.get("enabled") and int(sub.get("subscribed") or 0) >= len(_SYMBOLS):
                subscribed = True
                break
        if not subscribed:
            sub = await client.subscription_status()
            gates = (await client.status()).get("gate") or {}
            if (sub.get("counters") or {}).get("resubscribes", 0) == 0 and (
                gates.get("cooldown_active")
            ):
                pytest.skip(f"订阅被配额窗口推迟（如实跳过）: {sub} gate={gates}")
            pytest.fail(f"订阅未建立: {sub}")

        # 4) 等**数据帧**（ColDes+Content 带行；元帧/ACK 不算）并等待落 Redis
        frames = 0
        for _ in range(60):  # 最多 ~180s
            await asyncio.sleep(3)
            sub = await client.subscription_status()
            counters = sub.get("counters") or {}
            frames = int(counters.get("frames_data") or 0)
            if frames > 0:
                break
        if frames == 0:
            sub = await client.subscription_status()
            pytest.skip(
                f"观察窗内仅有元帧/无数据帧（晚间节拍稀疏，如实跳过）: {sub.get('counters')}"
            )
        # 写侧 drain 节奏
        await asyncio.sleep(4)

        # 5) 标准键逐值对拍
        checked = 0
        for sym in _SYMBOLS:
            code, market = sym.split(".")
            prefix = f"{market}{code}"
            snap_key = f"market:snapshot:{prefix.lower()}"
            series_key = f"market:series:{prefix}"

            snap = r.hgetall(snap_key)
            if not snap:
                continue  # 该标的本批未推（如实跳过，不造假）
            # 消费方契约字段
            assert float(snap["Now"]) > 0
            assert float(snap["Open"]) > 0
            assert float(snap["PreClose"]) > 0
            ts = int(snap["timestamp"])
            age = datetime.now(tz=_CST).timestamp() - ts
            assert -60 <= age <= 300, f"{sym} 快照时间戳异常 age={age:.0f}s"
            assert snap.get("source") == "tdx_aidata_sub"
            # 五档全景入 Hash（T-P6-03 覆盖口径：bid1-5/ask1-5 价与量全 20 字段；
            # Inside/Outside 内外盘 TDX 推送载荷不提供——并入总线路由时另行裁定）
            for f in (
                *[f"bid{i}" for i in range(1, 6)],
                *[f"bid_vol{i}" for i in range(1, 6)],
                *[f"ask{i}" for i in range(1, 6)],
                *[f"ask_vol{i}" for i in range(1, 6)],
            ):
                assert f in snap and float(snap[f]) >= 0, (sym, f, snap.get(f))

            # 时序点与快照内部一致（逐值对拍）
            zset = r.zrange(series_key, -1, -1, withscores=True)
            assert zset, f"{sym} series 无数据"
            member, score = zset[-1]
            payload = json.loads(member)
            assert int(score) == ts
            assert float(payload["price"]) == pytest.approx(float(snap["Now"]))
            assert payload["source"] == "tdx_aidata_sub"
            assert payload["is_stale"] is False

            # 涨跌停包络合理性（机构口径 sanity）
            lu, ld = float(payload["limit_up"]), float(payload["limit_down"])
            now_p = float(payload["price"])
            assert ld <= now_p <= lu, (sym, ld, now_p, lu)

            # 帧→键映射函数与真实键一致（防键口径漂移）
            rec = {
                "symbol": sym,
                "price": float(snap["Now"]),
                "pre_close": float(snap["PreClose"]),
                "open": float(snap["Open"]),
                "refresh_time": datetime.fromtimestamp(ts, tz=_CST).strftime("%H%M%S"),
            }
            mapped = collector.frame_to_redis(rec, datetime.now(tz=_CST))
            assert mapped["snapshot_key"] == snap_key
            assert mapped["series_key"] == series_key
            checked += 1

        assert checked >= 1, "无任何标的落地（订阅帧可能全被跳过）"
    finally:
        r.close()
        hs.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_hot_set_change_triggers_resubscribe(sub_env):
    """热集差分：新增符号 → 引擎重订阅（resubscribes 计数增长），订阅集合含新符号。"""
    import asyncio

    client = sub_env["client"]
    hot_set_key = sub_env["hot_set_key"]
    hs = _hot_set_redis()
    try:
        hs.sadd(hot_set_key, "600036.SH")
        assert await client.ensure_worker()
        base = None
        for _ in range(30):
            await asyncio.sleep(2)
            sub = await client.subscription_status()
            if sub.get("enabled") and int(sub.get("subscribed") or 0) >= 1:
                base = sub
                break
        assert base is not None, "基线订阅未建立"
        resub_before = int((base.get("counters") or {}).get("resubscribes") or 0)

        hs.sadd(hot_set_key, "000858.SZ")
        got = False
        for _ in range(30):
            await asyncio.sleep(2)
            sub = await client.subscription_status()
            if int(sub.get("subscribed") or 0) >= 2 and int(
                (sub.get("counters") or {}).get("resubscribes") or 0
            ) > resub_before:
                got = True
                break
        assert got, f"热集变更未触发重订阅: {sub}"
    finally:
        hs.close()
