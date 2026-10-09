"""T-FB-06 集成测试：跨市场回测台账扩展 + 序列表（真 PG，全程自清理）。

钉的契约：
- 七态词表落库（含诚实降级三态），非法状态被拒；
- run_id 精确收口 + 幂等（第二次 False），绝不动已完结行；
- 序列表 JSONB 往返无损（None 保持 null——前端「—」的数据源）；
- 矩阵单元格取**最近一次任意终态**（failed/insufficient 是结论不是空白）。
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

pytestmark = pytest.mark.integration


async def _fresh_pool() -> None:
    """批跑防坑：前序测试的 asyncio.run 会把连接绑死在已关闭的 loop 上。"""
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import close_database, get_session

    try:
        async with get_session(read_only=True) as probe:
            await probe.execute(text("SELECT 1"))
        return
    except Exception:  # noqa: BLE001
        await close_database()
    async with get_session(read_only=True) as probe:
        await probe.execute(text("SELECT 1"))


async def _cleanup(factor_ids: list[str]) -> None:
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    async with get_session() as session:
        for fid in factor_ids:
            await session.execute(
                text(
                    "DELETE FROM rd_agent_factor_backtest_series WHERE factor_id = :f"
                ),
                {"f": fid},
            )
            await session.execute(
                text("DELETE FROM rd_agent_factor_backtests WHERE factor_id = :f"),
                {"f": fid},
            )


def test_ensure_tables_widens_status_and_is_idempotent():
    async def runner():
        await _fresh_pool()
        from sqlalchemy import text

        from backend.services.engine.factor_backtest import store
        from backend.shared.database_manager_v2 import get_session

        await store.ensure_tables()
        await store.ensure_tables()  # 幂等：二次执行不炸

        async with get_session(read_only=True) as session:
            row = (
                (
                    await session.execute(
                        text(
                            """
                        SELECT pg_get_constraintdef(oid) AS def
                        FROM pg_constraint
                        WHERE conname = 'rd_agent_factor_backtests_status_check'
                          AND conrelid = 'rd_agent_factor_backtests'::regclass
                        """
                        )
                    )
                )
                .mappings()
                .first()
            )
        assert row is not None, "七态 CHECK 约束必须存在"
        definition = row["def"]
        for status in ("data_unsupported", "insufficient", "unavailable", "running"):
            assert f"'{status}'" in definition

    asyncio.run(runner())


def test_run_lifecycle_finish_idempotent_and_series_roundtrip():
    fid = f"fbx-test-{uuid.uuid4().hex[:12]}"

    async def runner():
        await _fresh_pool()
        from backend.services.engine.factor_backtest import store

        try:
            run_id = await store.start_run(
                fid,
                kind="functional",
                market="hong_kong",
                universe="liquid_top500",
                params={"cost_bps": 20, "window_years": 3},
                factor_name="测试因子",
                user_id="t-fb-test",
            )
            assert run_id.startswith("fb-")

            rows = await store.list_runs(factor_id=fid)
            assert len(rows) == 1
            assert rows[0]["status"] == "running"
            assert rows[0]["kind"] == "functional"
            assert rows[0]["params"] == {"cost_bps": 20, "window_years": 3}
            assert rows[0]["has_series"] is False

            ok = await store.finish_run(
                run_id,
                "completed",
                ic_value=0.051,
                rank_ic=0.048,
                icir=0.31,
                sharpe_ratio=1.2,
                annual_return=0.15,
                max_drawdown=-0.08,
                date_range="2023-10-08~2026-10-08",
                metrics={"ic": 0.051, "ann_turnover": 5.5, "ic_nw_t": None},
            )
            assert ok is True
            assert await store.finish_run(run_id, "failed", error="late") is False

            rows = await store.list_runs(factor_id=fid, market="hong_kong")
            assert rows[0]["status"] == "completed"
            assert rows[0]["metrics"]["ic"] == 0.051
            assert rows[0]["metrics"]["ic_nw_t"] is None  # None 不被吞成 0
            assert rows[0]["error"] is None  # 迟到的 failed 未改写已完结行

            payload = {
                "dates": ["2024-01-02", "2024-01-03"],
                "ic": [0.1, None],
                "nav_long": [1.0, 1.02],
            }
            await store.save_series(
                run_id, factor_id=fid, market="hong_kong", payload=payload
            )
            got = await store.get_series(run_id)
            assert got is not None
            assert got["series"] == payload
            assert got["market"] == "hong_kong"

            rows = await store.list_runs(factor_id=fid)
            assert rows[0]["has_series"] is True

            assert await store.get_series("fb-not-exists") is None
        finally:
            await _cleanup([fid])

    asyncio.run(runner())


def test_finish_run_rejects_unknown_status_and_accepts_degraded_states():
    fid = f"fbx-test-{uuid.uuid4().hex[:12]}"

    async def runner():
        await _fresh_pool()
        from backend.services.engine.factor_backtest import store

        try:
            run_id = await store.start_run(fid, kind="functional", market="crypto")
            with pytest.raises(ValueError):
                await store.finish_run(run_id, "running")  # 非终态
            with pytest.raises(ValueError):
                await store.finish_run(run_id, "exploded")  # 词表外
            assert await store.finish_run(
                run_id, "insufficient", error="有效交易日 30 < 120"
            )
            rows = await store.list_runs(factor_id=fid, status="insufficient")
            assert len(rows) == 1
            assert rows[0]["error"] == "有效交易日 30 < 120"
        finally:
            await _cleanup([fid])

    asyncio.run(runner())


def test_batch_lifecycle_persistence_and_running_pairs():
    """批次头/台账 batch_id/孤儿收口/幂等收口（T-FB-08/09 持久化契约）。"""
    fid = f"fbx-test-{uuid.uuid4().hex[:12]}"
    batch_id = f"fbb-test-{uuid.uuid4().hex[:12]}"

    async def runner():
        await _fresh_pool()
        from sqlalchemy import text

        from backend.services.engine.factor_backtest import store
        from backend.shared.database_manager_v2 import get_session

        try:
            spec = {
                "factor_ids": [fid],
                "markets": ["us_stock"],
                "units": [{"factor_id": fid, "market": "us_stock"}],
                "kinds": {fid: "functional"},
                "skipped": [],
            }
            await store.create_batch(batch_id, user_id="t-fb-test", spec=spec)
            row = await store.get_batch(batch_id)
            assert row["status"] == "running"
            assert row["spec"]["units"][0]["factor_id"] == fid
            assert row["finished_at"] is None

            run_id = await store.start_run(
                fid, kind="functional", market="us_stock", batch_id=batch_id
            )
            runs = await store.batch_runs(batch_id)
            assert [r["run_id"] for r in runs] == [run_id]  # batch_id 已挂上

            pairs = await store.running_factor_pairs([fid, "fbx-none"])
            assert {"factor_id": fid, "market": "us_stock"} in [dict(p) for p in pairs]

            # 重启恢复：孤儿 running 行收口（恰一行），批次仍 running
            assert (
                await store.settle_orphan_running(
                    batch_id, error="engine_restarted_mid_run"
                )
                == 1
            )
            run = await store.get_run(run_id)
            assert run["status"] == "failed"
            assert run["error"] == "engine_restarted_mid_run"
            assert await store.settle_orphan_running(batch_id, error="x") == 0

            # 收口幂等：running → aborted 只生效一次
            assert (
                await store.finish_batch(batch_id, "aborted", error="circuit_breaker")
                is True
            )
            assert await store.finish_batch(batch_id, "completed") is False
            with pytest.raises(ValueError):
                await store.finish_batch(batch_id, "exploded")

            assert all(
                b["batch_id"] != batch_id for b in await store.list_running_batches()
            )
            listed = await store.list_batches(user_id="t-fb-test", limit=50)
            assert any(b["batch_id"] == batch_id for b in listed)
            assert (await store.get_batch("fbb-not-exists")) is None
        finally:
            async with get_session() as session:
                await session.execute(
                    text(
                        "DELETE FROM rd_agent_factor_backtest_series "
                        "WHERE factor_id = :f"
                    ),
                    {"f": fid},
                )
                await session.execute(
                    text("DELETE FROM rd_agent_factor_backtests WHERE factor_id = :f"),
                    {"f": fid},
                )
                await session.execute(
                    text(
                        "DELETE FROM rd_agent_factor_backtest_batches "
                        "WHERE batch_id = :b"
                    ),
                    {"b": batch_id},
                )

    asyncio.run(runner())


def test_latest_cells_takes_most_recent_even_if_degraded():
    fid = f"fbx-test-{uuid.uuid4().hex[:12]}"

    async def runner():
        await _fresh_pool()
        from backend.services.engine.factor_backtest import store

        try:
            r1 = await store.start_run(fid, kind="functional", market="us_stock")
            await store.finish_run(r1, "completed", ic_value=0.04)
            await asyncio.sleep(0.05)
            r2 = await store.start_run(fid, kind="functional", market="us_stock")
            await store.finish_run(r2, "insufficient", error="too few days")

            r3 = await store.start_run(fid, kind="functional", market="hong_kong")
            await store.finish_run(r3, "unavailable", error="provider not ready")

            cells = await store.latest_cells([fid], ["us_stock", "hong_kong"])
            by_market = {c["market"]: c for c in cells}
            assert by_market["us_stock"]["run_id"] == r2
            assert by_market["us_stock"]["status"] == "insufficient"
            assert by_market["hong_kong"]["status"] == "unavailable"

            assert await store.latest_cells([]) == []
            only_us = await store.latest_cells([fid], ["us_stock"])
            assert [c["market"] for c in only_us] == ["us_stock"]
        finally:
            await _cleanup([fid])

    asyncio.run(runner())
