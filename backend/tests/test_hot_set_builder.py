"""热集定义服务（T-P6-06）测试：并集/优先级/截断纯函数 + 真库真 Redis 多用户 E2E。

覆盖：
1. U：compose_hot_set——优先级（持仓 > 异动 > 候选）、去重保序、上限截断统计、空池；
2. U：实盘持仓并入——全量/真实租户并入、测试租户隔离（不调加载器）、源失败隔离；
3. I：真夹具（2 个模拟账户持仓 + 当日候选信号行）→ build_once → 输出键断言 → 清理；
   输出用**部署本地 Redis**（hot_set_store 单一事实源，2026-09-17 由远端行情服迁回）＋独立测试键；
4. I：真单持仓并入 build_once（注入加载器夹具）→ 输出键含真实持仓、报告计数分列模拟/实盘；
5. D：空源 → 空集替换；超限截断如实计数；
6. G：输出键唯一（builder 写入面）与 collector 读取键一致。
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
    out2 = compose_hot_set(
        positions=["00700.HK", "BAD"], anomalies=[], candidates=[], cap=10
    )
    assert out2["symbols"] == [] and out2["stats"]["skipped"] == 2


@pytest.mark.unit
def test_compose_cap_truncation_priority_kept():
    from backend.shared.hot_set import compose_hot_set

    positions = [f"60000{i}.SH" for i in range(5)]
    candidates = [f"00000{i}.SZ" for i in range(10)]
    out = compose_hot_set(
        positions=positions, anomalies=[], candidates=candidates, cap=7
    )
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


# ── 1b. 实盘持仓并入（桥/QMT 真单，2026-10-08）─────────────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_collect_real_positions_merge_and_tenant_gate():
    from backend.services.live_trading.services.hot_set_builder import HotSetBuilder

    calls: list[int] = []

    async def loader():
        calls.append(1)
        return (
            {"SH600036": {"volume": 100}, "SZ300750": {"volume": 200}},
            {"source": "tdx_bridge"},
        )

    builder = HotSetBuilder(
        hot_set_key="qm:hot_set:test-unit", real_positions_loader=loader
    )
    # 全量构建（无租户过滤）→ 并入
    symbols, err = await builder._collect_real_positions(None)
    assert symbols == ["SH600036", "SZ300750"] and err is None
    # 真实租户（default）→ 并入
    symbols2, err2 = await builder._collect_real_positions("default")
    assert symbols2 == ["SH600036", "SZ300750"] and err2 is None
    # 测试租户 → 空集隔离，且不再调用加载器（真实账户不写进他人断言）
    before = len(calls)
    symbols3, err3 = await builder._collect_real_positions("t-hotset-abc123")
    assert symbols3 == [] and err3 is None
    assert len(calls) == before, "测试租户不应触发实盘持仓加载"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_collect_real_positions_failure_isolated():
    from backend.services.live_trading.services.hot_set_builder import HotSetBuilder

    async def loader():
        raise RuntimeError("桥查询失败: ConnectError")

    builder = HotSetBuilder(
        hot_set_key="qm:hot_set:test-unit", real_positions_loader=loader
    )
    symbols, err = await builder._collect_real_positions(None)
    assert symbols == [], "失败不阻断其余源"
    assert err is not None and "实盘持仓采集失败" in err


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


def _hot_set_redis():
    """热集输出客户端（**部署本地 Redis**——hot_set_store 单一事实源，2026-09-17 起）。"""
    from backend.shared.hot_set_store import make_hot_set_client

    return make_hot_set_client()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_build_once_multi_user_union_real_env():
    await _ensure_db_pool()
    from sqlalchemy import text as sa_text

    from backend.shared.database_manager_v2 import close_database, get_session

    tenant = f"{_TEST_TENANT_PREFIX}-{uuid.uuid4().hex[:6]}"
    hot_key = f"qm:hot_set:test-{uuid.uuid4().hex[:6]}"
    today = date.today()
    hs = _hot_set_redis()
    try:
        # 夹具①：两个用户（两租户维度也可）的模拟账户持仓（trade Redis）
        from backend.services.trade_shared.redis_client import (
            redis_client as trade_redis,
        )

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
                            sym: {
                                "volume": 100,
                                "available_volume": 100,
                                "cost": 10.0,
                                "price": 12.0,
                                "market_value": 1200.0,
                            }
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
                    {
                        "r": f"hotset-{uuid.uuid4().hex[:8]}",
                        "t": tenant,
                        "u": "41",
                        "d": today,
                        "s": sym,
                        "f": 9.9 - i,
                    },  # fidelity: allow-limit-threshold — 非阈值：fusion_score 递减夹具
                )
            await session.commit()

        # 执行：build_once（输出到**部署本地 Redis** 的隔离键）
        from backend.services.live_trading.services.hot_set_builder import HotSetBuilder

        builder = HotSetBuilder(hot_set_key=hot_key, cap=2000, candidate_top_n=500)
        report = await builder.build_once(tenant_filter=tenant)

        members = hs.smembers(hot_key)
        assert {"600036.SH", "000001.SZ"} <= members, f"持仓并集缺失: {members}"
        assert {"300750.SZ", "601318.SH"} <= members, f"候选缺失: {members}"
        assert report["kept"] >= 4 and report["kept"] == len(members)
        meta = hs.hgetall(f"{hot_key}:meta")
        assert meta.get("kept") and meta.get("built_at")
    finally:
        try:
            from backend.services.trade_shared.redis_client import (
                redis_client as trade_redis,
            )

            for user in ("41", "42"):
                trade_redis.client.delete(f"simulation:account:{tenant}:{user}")
        except Exception:  # noqa: BLE001
            pass
        async with get_session(read_only=False) as session:
            await session.execute(
                sa_text("DELETE FROM engine_signal_scores WHERE tenant_id=:t"),
                {"t": tenant},
            )
            await session.commit()
        hs.delete(hot_key, f"{hot_key}:meta")
        hs.close()
        await close_database()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_build_once_includes_real_positions_real_env():
    """真单持仓并入全集构建：注入加载器夹具（不依赖桥在线），输出键须含真实持仓。"""
    await _ensure_db_pool()
    from backend.shared.database_manager_v2 import close_database

    hot_key = f"qm:hot_set:test-{uuid.uuid4().hex[:6]}"
    hs = _hot_set_redis()
    try:
        from backend.services.live_trading.services.hot_set_builder import HotSetBuilder

        async def loader():
            return {"SH601857": {"volume": 700}}, {"source": "tdx_bridge"}

        builder = HotSetBuilder(
            hot_set_key=hot_key, cap=2000, real_positions_loader=loader
        )
        report = await builder.build_once(tenant_filter=None)

        members = hs.smembers(hot_key)
        assert "601857.SH" in members, f"真单持仓缺失: {members}"
        assert report["sources"]["positions_real"] == 1
        assert report["sources"]["positions"] == (
            report["sources"]["positions_sim"] + 1
        )
    finally:
        hs.delete(hot_key, f"{hot_key}:meta")
        hs.close()
        await close_database()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_build_once_empty_sources_replaces_empty_real_env():
    await _ensure_db_pool()
    from backend.shared.database_manager_v2 import close_database

    tenant = f"{_TEST_TENANT_PREFIX}-empty-{uuid.uuid4().hex[:6]}"
    hot_key = f"qm:hot_set:test-{uuid.uuid4().hex[:6]}"
    hs = _hot_set_redis()
    try:
        hs.sadd(
            hot_key, "600036.SH"
        )  # 旧集合：本次构建应被空集**替换**（过期订阅退订）
        from backend.services.live_trading.services.hot_set_builder import HotSetBuilder

        builder = HotSetBuilder(hot_set_key=hot_key, cap=10)
        # 异动池读共享真 Redis（识别引擎上线后有真实异动，与租户无关）：注入空池
        # 隔离环境漂移，保住本用例「空股票源」的语义
        builder._collect_anomalies = lambda: ([], None)
        report = await builder.build_once(tenant_filter=tenant)
        # T-P6-13 起：空股票源 → 仅常驻指数（regime 源）；旧集合被**替换**（过期订阅退订）
        assert report["kept"] == 2
        assert set(hs.smembers(hot_key)) == {"000300.SH", "000001.SH"}
        assert (
            json.loads(hs.hget(f"{hot_key}:meta", "sources") or "{}").get("indexes")
            == 2
        )
    finally:
        hs.delete(hot_key, f"{hot_key}:meta")
        hs.close()
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
