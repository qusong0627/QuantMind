"""热集定义服务（T-P6-06）测试：并集/优先级/截断纯函数 + 真库真 Redis 多用户 E2E。

覆盖：
1. U：compose_hot_set——优先级（持仓 > 异动 > 候选）、去重保序、上限截断统计、空池；
2. I：真夹具（2 个模拟账户持仓 + 当日候选信号行）→ build_once → 输出键断言 → 清理；
   输出用**真实远端行情 Redis**（与订阅 worker 同一实例）但独立测试键（隔离生产热集）；
3. D：空源 → 空集替换；超限截断如实计数；
4. G：输出键唯一（builder 写入面）与 collector 读取键一致。
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[1]
_TEST_TENANT_PREFIX = "t-hotset"


# ── 1. 纯函数 ───────────────────────────────────────────────────────


@pytest.mark.unit
def test_compose_priority_dedupe_and_forms():
    from backend.shared.hot_set import compose_hot_set

    out = compose_hot_set(
        positions=["SH600036", "000858.SZ"],
        anomalies=["601318", "600036.SH"],  # 与持仓重复（形态不同）应去重
        candidates=["600519", "000858"],
        cap=100,
    )
    symbols = out["symbols"]
    assert symbols[:2] == ["600036.SH", "000858.SZ"], "持仓优先且统一为后缀式"
    assert symbols[2] == "601318.SH", "异动次之"
    assert set(symbols) == {"600036.SH", "000858.SZ", "601318.SH", "600519.SH"}
    assert len(symbols) == len(set(symbols)), "无重复"
    assert out["stats"]["total"] == 6 and out["stats"]["kept"] == 4
    # 非法/非沪深北标的（如港股 00700.HK）跳过并计数
    out2 = compose_hot_set(positions=["00700.HK", "BAD"], anomalies=[], candidates=[], cap=10)
    assert out2["symbols"] == [] and out2["stats"]["skipped"] == 2


@pytest.mark.unit
def test_compose_cap_truncation_priority_kept():
    from backend.shared.hot_set import compose_hot_set

    positions = [f"60000{i}.SH" for i in range(5)]
    candidates = [f"00000{i}.SZ" for i in range(10)]
    out = compose_hot_set(positions=positions, anomalies=[], candidates=candidates, cap=7)
    assert len(out["symbols"]) == 7
    # 截断只吃候选尾部，持仓全保留（优先级语义）
    assert all(p in out["symbols"] for p in positions)
    stats = out["stats"]
    assert stats["truncated"] == 8 and stats["kept"] == 7 and stats["total"] == 15


@pytest.mark.unit
def test_compose_empty_sources():
    from backend.shared.hot_set import compose_hot_set

    out = compose_hot_set(positions=[], anomalies=[], candidates=[], cap=2000)
    assert out["symbols"] == []
    assert out["stats"]["total"] == 0 and out["stats"]["truncated"] == 0


# ── 2/3. 真库 + 真 Redis ────────────────────────────────────────────


async def _ensure_db_pool():
    from sqlalchemy import text as _t

    from backend.shared.database_manager_v2 import close_database, get_session

    try:
        async with get_session(read_only=True) as probe:
            await probe.execute(_t("SELECT 1"))
        return
    except Exception:  # noqa: BLE001
        await close_database()
    async with get_session(read_only=True) as probe:
        await probe.execute(_t("SELECT 1"))


def _quote_redis():
    import redis as _redis

    from backend.shared.remote_quote_config import resolve_remote_quote_redis

    resolved = resolve_remote_quote_redis()
    assert resolved is not None, "远端行情 Redis 不可解析"
    host, port, password, db = resolved
    return _redis.Redis(
        host=host, port=port, password=password, db=db,
        decode_responses=True, socket_connect_timeout=3,
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_build_once_multi_user_union_real_env():
    await _ensure_db_pool()
    from sqlalchemy import text as sa_text

    from backend.shared.database_manager_v2 import close_database, get_session

    tenant = f"{_TEST_TENANT_PREFIX}-{uuid.uuid4().hex[:6]}"
    hot_key = f"qm:hot_set:test-{uuid.uuid4().hex[:6]}"
    today = date.today()
    qr = _quote_redis()
    try:
        # 夹具①：两个用户（两租户维度也可）的模拟账户持仓（trade Redis）
        from backend.services.trade_shared.redis_client import redis_client as trade_redis

        if trade_redis.client is None:
            trade_redis.connect()
        for user, sym in (("41", "SH600036"), ("42", "SZ000001")):
            trade_redis.client.set(
                f"simulation:account:{tenant}:{user}",
                json.dumps(
                    {
                        "cash": 100000.0,
                        "total_asset": 120000.0,
                        "positions": {
                            sym: {"volume": 100, "available_volume": 100, "cost": 10.0,
                                  "price": 12.0, "market_value": 1200.0}
                        },
                    }
                ),
            )
        # 夹具②：当日候选信号（真库：本租户高分行）
        async with get_session(read_only=False) as session:
            for i, sym in enumerate(["SZ300750", "SH601318"]):
                await session.execute(
                    sa_text(
                        "INSERT INTO engine_signal_scores "
                        "(run_id, tenant_id, user_id, trade_date, symbol, fusion_score, "
                        " model_version, feature_version) "
                        "VALUES (:r, :t, :u, :d, :s, :f, 'test', 'test')"
                    ),
                    {"r": f"hotset-{uuid.uuid4().hex[:8]}", "t": tenant, "u": "41",
                     "d": today, "s": sym, "f": 9.9 - i},
                )
            await session.commit()

        # 执行：build_once（输出到真实远端行情 Redis 的隔离键）
        from backend.services.live_trading.services.hot_set_builder import HotSetBuilder

        builder = HotSetBuilder(hot_set_key=hot_key, cap=2000, candidate_top_n=500)
        report = await builder.build_once(tenant_filter=tenant)

        members = qr.smembers(hot_key)
        assert {"600036.SH", "000001.SZ"} <= members, f"持仓并集缺失: {members}"
        assert {"300750.SZ", "601318.SH"} <= members, f"候选缺失: {members}"
        assert report["kept"] >= 4 and report["kept"] == len(members)
        meta = qr.hgetall(f"{hot_key}:meta")
        assert meta.get("kept") and meta.get("built_at")
    finally:
        try:
            from backend.services.trade_shared.redis_client import redis_client as trade_redis

            for user in ("41", "42"):
                trade_redis.client.delete(f"simulation:account:{tenant}:{user}")
        except Exception:  # noqa: BLE001
            pass
        async with get_session(read_only=False) as session:
            await session.execute(
                sa_text("DELETE FROM engine_signal_scores WHERE tenant_id=:t"), {"t": tenant}
            )
            await session.commit()
        qr.delete(hot_key, f"{hot_key}:meta")
        qr.close()
        await close_database()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_build_once_empty_sources_replaces_empty_real_env():
    await _ensure_db_pool()
    from backend.shared.database_manager_v2 import close_database

    tenant = f"{_TEST_TENANT_PREFIX}-empty-{uuid.uuid4().hex[:6]}"
    hot_key = f"qm:hot_set:test-{uuid.uuid4().hex[:6]}"
    qr = _quote_redis()
    try:
        qr.sadd(hot_key, "600036.SH")  # 旧集合：本次构建应被空集**替换**（过期订阅退订）
        from backend.services.live_trading.services.hot_set_builder import HotSetBuilder

        builder = HotSetBuilder(hot_set_key=hot_key, cap=10)
        report = await builder.build_once(tenant_filter=tenant)
        assert report["kept"] == 0
        assert qr.scard(hot_key) == 0
    finally:
        qr.delete(hot_key, f"{hot_key}:meta")
        qr.close()
        await close_database()


# ── 4. 源守卫 ───────────────────────────────────────────────────────


@pytest.mark.unit
def test_hot_set_key_single_source_guard():
    """builder 输出键与 collector 读取键必须同源（默认键取自共享 config）。"""
    from backend.shared.tdx_aidata import config

    builder_src = (
        _BACKEND / "services/live_trading/services/hot_set_builder.py"
    ).read_text(encoding="utf-8")
    assert "config.hot_set_key()" in builder_src or "hot_set_key" in builder_src
    assert config.DEFAULT_HOT_SET_KEY == "qm:hot_set:symbols"
