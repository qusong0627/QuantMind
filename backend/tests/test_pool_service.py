"""因子池服务（pool_service）——真库集成 + env 旋钮纯函数。

每条断言都在防御一种静默故障：
1. **回测钩子绝不拖挂回测**：因子不存在 / 无 user_id / 面板写失败 → 返回
   False + 告警，绝不上抛（池是增益层，一次回测不能被池登记拖挂）。
2. **面板与池行同源**：record 后面板文件落盘、池行在、同任务链边与公式边在。
3. **novelty 口径**：完全相同的两个因子 → novelty≈0 且连 ``correlated_with``
   边；``dry_run`` 绝不写库（否则「预演」变成真写）。
4. **注入单通道 + 疲劳纪律**：digest 只含池内 completed 因子；``exclude_task_id``
   排掉本任务；``mark_retrieved`` 只给真进文本的因子 +1。
5. **隔离硬约束**：user A 的读接口看不见 user B 的池行（跨用户泄漏 = 把甲的
   挖掘成果注进乙的 prompt）。
6. **幂等**：refresh 两遍边数不翻倍（唯一键 + 先删后插）。

真库测试的纪律（照 ``test_agent_ledger_store`` 先例）：
* 一次性 user_id（``t-pool-<rand>``），finally 里连根清（memory: 集成
  测试污染真账）；
* 每个异步测试 finally ``await close_database()``——asyncpg 连接绑定创建
  它的 loop，跨 ``asyncio.run`` / 跨测试复用池化连接必报
  「attached to a different loop」（memory 里的老坑，本文件初版就踩了）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import text

from backend.services.engine.mining_plugins import pool_panels, pool_service
from backend.shared.factor_pool_contract import EDGES_TABLE, POOL_TABLE

MARKET = "a_share"
UNIVERSE = ""


def _run_id() -> str:
    return f"t-pool-{uuid4().hex[:10]}"


def _values(days: int = 6, symbols: int = 30, seed: int = 0) -> pd.Series:
    """合成因子值（MultiIndex 日×股；seed 相同 → 两份面板完全一致）。"""
    rng = np.random.default_rng(seed)
    index = pd.MultiIndex.from_product(
        [
            pd.date_range("2026-01-05", periods=days, freq="B"),
            [f"SZ{i:06d}" for i in range(1, symbols + 1)],
        ],
        names=["datetime", "instrument"],
    )
    return pd.Series(rng.normal(size=len(index)), index=index)


async def _skip_if_no_db() -> None:
    from backend.shared.database_manager_v2 import get_session

    try:
        async with get_session(read_only=True) as probe:
            await probe.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"DB 不可用: {exc}")


async def _seed_factor(
    session,
    *,
    factor_id: str,
    user_id: str | None,
    formula: str = "close/mean(close,20)",
    icir: float | None = 0.5,
    task_id: str | None = None,
    ic: float | None = 0.03,
    description: str | None = None,
) -> None:
    import json

    meta = {"task_id": task_id, "quality": {"pfs": 0.95}}
    if icir is not None:
        meta["icir"] = icir
    if description is not None:
        meta["description"] = description
    await session.execute(
        text("""
            INSERT INTO rd_agent_factors
                (factor_id, factor_name, factor_code, status, user_id, metadata_json,
                 market, universe, factor_formulation, ic_value, rank_ic)
            VALUES
                (:factor_id, :name, '-', 'completed', :user_id, CAST(:meta AS JSONB),
                 :market, :universe, :formula, :ic, 0.04)
        """),
        {
            "factor_id": factor_id,
            "name": f"name-{factor_id}",
            "user_id": user_id,
            "meta": json.dumps(meta, ensure_ascii=False),
            "market": MARKET,
            "universe": UNIVERSE,
            "formula": formula,
            "ic": ic,
        },
    )


async def _seed_pool_row(session, *, factor_id: str, user_id: str) -> None:
    """直插池行（不经回测钩子）：分位查询只依赖池行 + 因子 IC，测试免建面板。"""
    await session.execute(
        text(
            f"INSERT INTO {POOL_TABLE} (factor_id, user_id, market, universe) "
            "VALUES (:factor_id, :user_id, :market, :universe) "
            "ON CONFLICT (factor_id) DO NOTHING"
        ),
        {
            "factor_id": factor_id,
            "user_id": user_id,
            "market": MARKET,
            "universe": UNIVERSE,
        },
    )


async def _cleanup(factor_ids: list[str], users: list[str]) -> None:
    """连根拔：边 → 池行 → 因子（按测试 run 的 id/用户清，绝不扫描全表删）。"""
    from backend.shared.database_manager_v2 import get_session

    async with get_session() as session:
        if factor_ids:
            await session.execute(
                text(
                    f"DELETE FROM {EDGES_TABLE} WHERE src_factor_id = ANY(:ids) "
                    "OR dst_factor_id = ANY(:ids)"
                ),
                {"ids": factor_ids},
            )
            await session.execute(
                text(f"DELETE FROM {POOL_TABLE} WHERE factor_id = ANY(:ids)"),
                {"ids": factor_ids},
            )
            await session.execute(
                text("DELETE FROM rd_agent_factors WHERE factor_id = ANY(:ids)"),
                {"ids": factor_ids},
            )
        for user in users:
            await session.execute(
                text(f"DELETE FROM {EDGES_TABLE} WHERE user_id = :u"), {"u": user}
            )
            await session.execute(
                text(f"DELETE FROM {POOL_TABLE} WHERE user_id = :u"), {"u": user}
            )


class TestEnvKnobs:
    def test_injection_enabled_default_on(self, monkeypatch):
        monkeypatch.delenv("QM_FACTOR_POOL_INJECT_DISABLED", raising=False)
        assert pool_service.injection_enabled() is True

    @pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
    def test_injection_disabled_values(self, monkeypatch, value):
        monkeypatch.setenv("QM_FACTOR_POOL_INJECT_DISABLED", value)
        assert pool_service.injection_enabled() is False

    def test_injection_zero_means_not_disabled(self, monkeypatch):
        monkeypatch.setenv("QM_FACTOR_POOL_INJECT_DISABLED", "0")
        assert pool_service.injection_enabled() is True

    def test_inject_k_default_and_override(self, monkeypatch):
        monkeypatch.delenv("QM_FACTOR_POOL_INJECT_K", raising=False)
        assert pool_service.inject_k() == pool_service.DEFAULT_INJECT_K
        monkeypatch.setenv("QM_FACTOR_POOL_INJECT_K", "3")
        assert pool_service.inject_k() == 3
        monkeypatch.setenv("QM_FACTOR_POOL_INJECT_K", "abc")
        assert pool_service.inject_k() == pool_service.DEFAULT_INJECT_K
        monkeypatch.setenv("QM_FACTOR_POOL_INJECT_K", "-2")
        assert pool_service.inject_k() == 0


class TestHelpers:
    def test_age_days_naive_input_treated_as_utc(self):
        """naive 时间按 UTC 读（TIMESTAMPTZ 列在驱动层可能丢 tzinfo）。"""
        naive = datetime.now(timezone.utc).replace(tzinfo=None)
        age = pool_service._age_days(naive)
        assert age is not None and age < 1.0

    def test_age_days_non_datetime_is_none(self):
        assert pool_service._age_days("2026-01-01") is None
        assert pool_service._age_days(None) is None

    def test_as_float_never_raises(self):
        assert pool_service._as_float("0.5") == 0.5
        assert pool_service._as_float(None) is None
        assert pool_service._as_float("N/A") is None
        assert pool_service._as_float("") is None

    def test_canonical_edge_orders_lexicographically(self):
        assert pool_service._canonical_edge("b", "a") == ("a", "b")
        assert pool_service._canonical_edge("a", "b") == ("a", "b")


# ── 真库集成 ─────────────────────────────────────────────────────────


class TestRecordHook:
    @pytest.mark.asyncio
    async def test_record_writes_panel_pool_and_edges(self, tmp_path, monkeypatch):
        from backend.shared.database_manager_v2 import close_database, get_session

        await _skip_if_no_db()
        monkeypatch.setenv("QM_FACTOR_POOL_PANEL_DIR", str(tmp_path / "panels"))
        run = _run_id()
        user = run
        task = f"{run}-task"
        f1, f2, orphan = f"{run}_f1", f"{run}_f2", f"{run}_nouser"
        try:
            async with get_session() as session:
                await _seed_factor(session, factor_id=f1, user_id=user, task_id=task)
                await _seed_factor(session, factor_id=f2, user_id=user, task_id=task)
                await _seed_factor(session, factor_id=orphan, user_id=None)

            ok1 = await pool_service.record_backtested_factor(
                f1, market=MARKET, values=_values(seed=1)
            )
            ok2 = await pool_service.record_backtested_factor(
                f2, market=MARKET, values=_values(seed=1)
            )
            # 无 user_id 的因子绝不登记（隔离硬约束）
            no_user = await pool_service.record_backtested_factor(orphan, market=MARKET)
            # 不存在的因子 → False，绝不上抛
            missing = await pool_service.record_backtested_factor(
                f"{run}_ghost", market=MARKET
            )

            async with get_session(read_only=True) as session:
                pool_ids = [
                    str(r[0])
                    for r in (
                        await session.execute(
                            text(
                                f"SELECT factor_id FROM {POOL_TABLE} "
                                "WHERE factor_id = ANY(:ids)"
                            ),
                            {"ids": [f1, f2, orphan]},
                        )
                    ).all()
                ]
                edges = (
                    await session.execute(
                        text(
                            f"SELECT src_factor_id, dst_factor_id, relation, method "
                            f"FROM {EDGES_TABLE} WHERE user_id = :u"
                        ),
                        {"u": user},
                    )
                ).all()

            assert ok1 is True and ok2 is True
            assert no_user is False, "无 user_id 的因子被登记了 —— 隔离硬约束失效"
            assert missing is False
            assert sorted(pool_ids) == sorted([f1, f2]), "无 user_id 因子不该有池行"
            assert pool_panels.panel_path(MARKET, f1).is_file(), "面板未落盘"
            edge_set = {(str(e[2]), str(e[3])) for e in edges}
            assert ("same_task", "task_round") in edge_set, "同任务链边缺失"
            assert ("similar_to", "formula") in edge_set, "同构公式边缺失"
        finally:
            await _cleanup([f1, f2, orphan, f"{run}_ghost"], [user])
            await close_database()


class TestRefresh:
    @pytest.mark.asyncio
    async def test_refresh_novelty_edges_and_idempotency(self, tmp_path, monkeypatch):
        from backend.shared.database_manager_v2 import close_database, get_session

        await _skip_if_no_db()
        monkeypatch.setenv("QM_FACTOR_POOL_PANEL_DIR", str(tmp_path / "panels"))
        run = _run_id()
        user = run
        f1, f2, f3 = f"{run}_f1", f"{run}_f2", f"{run}_f3"
        try:
            async with get_session() as session:
                await _seed_factor(session, factor_id=f1, user_id=user, icir=0.5)
                await _seed_factor(session, factor_id=f2, user_id=user, icir=0.4)
                await _seed_factor(
                    session,
                    factor_id=f3,
                    user_id=user,
                    icir=0.3,
                    formula="volume/std(volume,10)",
                )
            await pool_service.record_backtested_factor(
                f1, market=MARKET, values=_values(seed=1)
            )
            await pool_service.record_backtested_factor(
                f2,
                market=MARKET,
                values=_values(seed=1),  # 与 f1 完全相同
            )
            await pool_service.record_backtested_factor(
                f3, market=MARKET, values=_values(seed=99)
            )

            dry = await pool_service.refresh_pool(
                user_id=user, market=MARKET, universe=UNIVERSE, dry_run=True
            )
            async with get_session(read_only=True) as session:
                score_after_dry = (
                    await session.execute(
                        text(
                            f"SELECT pool_score FROM {POOL_TABLE} WHERE factor_id = :f"
                        ),
                        {"f": f1},
                    )
                ).scalar()

            stats = await pool_service.refresh_pool(
                user_id=user, market=MARKET, universe=UNIVERSE
            )
            async with get_session(read_only=True) as session:
                rows = {
                    str(r["factor_id"]): r
                    for r in (
                        await session.execute(
                            text(
                                f"SELECT factor_id, novelty, max_pool_corr, "
                                f"max_pool_corr_with, pool_score FROM {POOL_TABLE} "
                                "WHERE factor_id = ANY(:ids)"
                            ),
                            {"ids": [f1, f2, f3]},
                        )
                    ).mappings()
                }
                edges1 = (
                    await session.execute(
                        text(f"SELECT COUNT(*) FROM {EDGES_TABLE} WHERE user_id = :u"),
                        {"u": user},
                    )
                ).scalar()
                corr_edge = (
                    await session.execute(
                        text(
                            f"SELECT weight FROM {EDGES_TABLE} WHERE user_id = :u "
                            "AND relation = 'correlated_with'"
                        ),
                        {"u": user},
                    )
                ).scalar()
            stats2 = await pool_service.refresh_pool(
                user_id=user, market=MARKET, universe=UNIVERSE
            )
            async with get_session(read_only=True) as session:
                edges2 = (
                    await session.execute(
                        text(f"SELECT COUNT(*) FROM {EDGES_TABLE} WHERE user_id = :u"),
                        {"u": user},
                    )
                ).scalar()

            assert dry["factors"] == 3 and dry["panels"] == 3 and dry["pairs"] == 3
            assert score_after_dry is None, "dry_run 把 pool_score 真写进去了"
            assert stats["corr_edges"] == 1, f"应只有 f1-f2 一对相关边: {stats}"
            # 完全相同的两个因子：最大 |ρ|≈1 → novelty≈0
            assert rows[f1]["novelty"] == pytest.approx(0.0, abs=1e-6)
            assert abs(rows[f1]["max_pool_corr"]) == pytest.approx(1.0, abs=1e-6)
            assert rows[f1]["max_pool_corr_with"] == f2
            assert rows[f1]["pool_score"] is not None
            assert rows[f3]["novelty"] is not None
            assert abs(corr_edge) >= 0.99
            assert stats2["corr_edges"] == 1
            assert edges1 == edges2, "refresh 重跑边数翻倍 —— 幂等键/先删后插失效"
        finally:
            await _cleanup([f1, f2, f3], [user])
            await close_database()


class TestInjection:
    @pytest.mark.asyncio
    async def test_digest_exclude_and_fatigue(self, tmp_path, monkeypatch):
        from backend.shared.database_manager_v2 import close_database, get_session

        await _skip_if_no_db()
        run = _run_id()
        user = run
        f1, f2 = f"{run}_f1", f"{run}_f2"
        t1, t2 = f"{run}-t1", f"{run}-t2"
        try:
            async with get_session() as session:
                await _seed_factor(
                    session, factor_id=f1, user_id=user, icir=0.6, task_id=t1
                )
                await _seed_factor(
                    session, factor_id=f2, user_id=user, icir=0.4, task_id=t2
                )
            await pool_service.record_backtested_factor(f1, market=MARKET)
            await pool_service.record_backtested_factor(f2, market=MARKET)

            digest, ids = await pool_service.build_injection_digest(
                user_id=user, market=MARKET, universe=UNIVERSE
            )
            only2, ids2 = await pool_service.build_injection_digest(
                user_id=user, market=MARKET, universe=UNIVERSE, exclude_task_id=t1
            )
            inj = await pool_service.prepare_injection(
                user_id=user,
                market=MARKET,
                universe=UNIVERSE,
                task_id=None,
                log_dir=tmp_path,
            )
            marked = await pool_service.mark_retrieved(inj.factor_ids)
            async with get_session(read_only=True) as session:
                times = {
                    str(r[0]): int(r[1])
                    for r in (
                        await session.execute(
                            text(
                                f"SELECT factor_id, times_retrieved FROM {POOL_TABLE} "
                                "WHERE factor_id = ANY(:ids)"
                            ),
                            {"ids": [f1, f2]},
                        )
                    ).all()
                }
            monkeypatch.setenv("QM_FACTOR_POOL_INJECT_DISABLED", "1")
            disabled = await pool_service.prepare_injection(
                user_id=user,
                market=MARKET,
                universe=UNIVERSE,
                task_id=None,
                log_dir=tmp_path,
            )
            monkeypatch.delenv("QM_FACTOR_POOL_INJECT_DISABLED")

            assert "历史挖掘记忆" in digest
            assert "池内共 2 条" in digest, "SOTA 标杆线未接入摘要"
            assert set(ids) == {f1, f2}
            assert ids2 == (f2,), "exclude_task_id 没排掉本任务的因子"
            assert f1 not in only2
            assert inj.path is not None and inj.path.is_file()
            assert inj.path.name == "pool_context.md"
            assert set(inj.factor_ids) == {f1, f2}
            assert marked == 2
            assert times[f1] == 1 and times[f2] == 1
            assert disabled.path is None, "env 关闭后仍生成了注入文件"
        finally:
            await _cleanup([f1, f2], [user])
            await close_database()

    @pytest.mark.asyncio
    async def test_digest_empty_pool_returns_blank(self):
        from backend.shared.database_manager_v2 import close_database

        await _skip_if_no_db()
        run = _run_id()
        try:
            digest, ids = await pool_service.build_injection_digest(
                user_id=run, market=MARKET, universe=UNIVERSE
            )
            assert digest == "" and ids == ()
        finally:
            await close_database()


class TestReadEndpointsScopeIsolation:
    @pytest.mark.asyncio
    async def test_reads_are_user_scoped(self, tmp_path, monkeypatch):
        from backend.shared.database_manager_v2 import close_database, get_session

        await _skip_if_no_db()
        monkeypatch.setenv("QM_FACTOR_POOL_PANEL_DIR", str(tmp_path / "panels"))
        run = _run_id()
        user_a, user_b = f"{run}-a", f"{run}-b"
        fa, fb = f"{run}_a1", f"{run}_b1"
        try:
            async with get_session() as session:
                await _seed_factor(session, factor_id=fa, user_id=user_a, icir=0.5)
                await _seed_factor(session, factor_id=fb, user_id=user_b, icir=0.6)
            await pool_service.record_backtested_factor(
                fa, market=MARKET, values=_values(seed=1)
            )
            await pool_service.record_backtested_factor(
                fb, market=MARKET, values=_values(seed=2)
            )
            await pool_service.refresh_pool(
                user_id=user_a, market=MARKET, universe=UNIVERSE
            )
            overview_a = await pool_service.pool_overview(
                user_id=user_a, market=MARKET, universe=UNIVERSE
            )
            listing_a = await pool_service.list_pool_factors(
                user_id=user_a, market=MARKET, universe=UNIVERSE
            )
            graph_a = await pool_service.pool_graph(
                user_id=user_a, market=MARKET, universe=UNIVERSE
            )
            overview_b = await pool_service.pool_overview(
                user_id=user_b, market=MARKET, universe=UNIVERSE
            )
            async with get_session(read_only=True) as session:
                score_b = (
                    await session.execute(
                        text(
                            f"SELECT pool_score FROM {POOL_TABLE} WHERE factor_id = :f"
                        ),
                        {"f": fb},
                    )
                ).scalar()

            assert overview_a["total"] == 1
            assert overview_a["avg_icir"] == pytest.approx(0.5, abs=1e-9)
            assert [i["factor_id"] for i in listing_a["items"]] == [fa]
            assert listing_a["total"] == 1
            assert listing_a["items"][0]["has_panel"] is True
            assert [n["factor_id"] for n in graph_a["nodes"]] == [fa]
            assert overview_b["total"] == 1, "B 看不到自己的池行"
            assert score_b is None, "refresh(user_a) 越界改了 user_b 的池行"
        finally:
            await _cleanup([fa, fb], [user_a, user_b])
            await close_database()


class TestIcPoolPercentile:
    @pytest.mark.asyncio
    async def test_percentile_ordering_and_none_semantics(self):
        """分位 = 严格小于者 / (N−1)；缺 IC / 不在池 / 池太小 → None（不按 0 判）。"""
        from backend.shared.database_manager_v2 import close_database, get_session

        await _skip_if_no_db()
        run = _run_id()
        user = f"{run}-u"
        f_low, f_mid, f_high = f"{run}_l", f"{run}_m", f"{run}_h"
        f_nul = f"{run}_n"  # IC 缺失（在池不计数）
        f_out = f"{run}_o"  # 不在池中
        user_solo = f"{run}-s"
        f_solo = f"{run}_s"
        try:
            async with get_session() as session:
                await _seed_factor(session, factor_id=f_low, user_id=user, ic=0.01)
                await _seed_factor(session, factor_id=f_mid, user_id=user, ic=0.02)
                await _seed_factor(session, factor_id=f_high, user_id=user, ic=0.03)
                await _seed_factor(session, factor_id=f_nul, user_id=user, ic=None)
                await _seed_factor(session, factor_id=f_out, user_id=user, ic=0.5)
                await _seed_factor(
                    session, factor_id=f_solo, user_id=user_solo, ic=0.02
                )
                for fid, uid in (
                    (f_low, user),
                    (f_mid, user),
                    (f_high, user),
                    (f_nul, user),
                    (f_solo, user_solo),
                ):
                    await _seed_pool_row(session, factor_id=fid, user_id=uid)

            args = {"market": MARKET, "universe": UNIVERSE}
            assert await pool_service.ic_pool_percentile(
                user_id=user, factor_id=f_low, **args
            ) == pytest.approx(0.0)
            assert await pool_service.ic_pool_percentile(
                user_id=user, factor_id=f_mid, **args
            ) == pytest.approx(0.5)
            assert await pool_service.ic_pool_percentile(
                user_id=user, factor_id=f_high, **args
            ) == pytest.approx(1.0)
            assert (
                await pool_service.ic_pool_percentile(
                    user_id=user, factor_id=f_nul, **args
                )
                is None
            ), "IC 缺失的因子不参与分位，自身查分位也应是 None"
            assert (
                await pool_service.ic_pool_percentile(
                    user_id=user, factor_id=f_out, **args
                )
                is None
            ), "不在池中的因子没有分位"
            assert (
                await pool_service.ic_pool_percentile(
                    user_id=user_solo, factor_id=f_solo, **args
                )
                is None
            ), "池内有效 IC 少于 2 个 → 不可得"
            assert (
                await pool_service.ic_pool_percentile(
                    user_id=f"{run}-x", factor_id=f_high, **args
                )
                is None
            ), "分位查询必须按 user 隔离"
        finally:
            await _cleanup(
                [f_low, f_mid, f_high, f_nul, f_out, f_solo], [user, user_solo]
            )
            await close_database()


class TestCleanupCriteriaConfig:
    def test_yaml_cleanup_section_overrides_defaults(self, tmp_path, monkeypatch):
        cfg = tmp_path / "plugins.yaml"
        cfg.write_text(
            "cleanup:\n  corr_dup: 0.85\n  weak_icir_quantile: 0.1\n  min_icir_sample: 8\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("QM_MINING_PLUGINS_CONFIG", str(cfg))
        criteria = pool_service._cleanup_criteria()
        assert criteria.corr_dup == pytest.approx(0.85)
        assert criteria.weak_icir_quantile == pytest.approx(0.1)
        assert criteria.min_icir_sample == 8

    def test_invalid_values_fall_back_to_defaults(self, tmp_path, monkeypatch):
        cfg = tmp_path / "plugins.yaml"
        cfg.write_text(
            "cleanup:\n  corr_dup: abc\n  min_icir_sample: x\n", encoding="utf-8"
        )
        monkeypatch.setenv("QM_MINING_PLUGINS_CONFIG", str(cfg))
        criteria = pool_service._cleanup_criteria()
        assert criteria.corr_dup == pytest.approx(0.9)
        assert criteria.min_icir_sample == 5


class TestArchiveCleanup:
    """归档链路：只打时间戳（非删除），默认退出注入/列表/图/总览，可恢复。

    防的静默故障：归档后某个读路径漏了 ``archived_at`` 过滤——归档因子
    继续被注入进 prompt（用户已判定它不值得参考）、或列表里消失但总览
    计数没变（对不上账）。
    """

    @pytest.mark.asyncio
    async def test_archive_roundtrip_across_all_read_paths(self):
        from backend.shared.database_manager_v2 import close_database, get_session

        await _skip_if_no_db()
        run = _run_id()
        user = run
        weak = f"{run}_weak"
        strong = [f"{run}_s{i}" for i in range(4)]
        try:
            async with get_session() as session:
                await _seed_factor(session, factor_id=weak, user_id=user, icir=0.01)
                for i, fid in enumerate(strong):
                    await _seed_factor(
                        session, factor_id=fid, user_id=user, icir=0.9 - 0.1 * i
                    )
            for fid in [weak, *strong]:
                assert await pool_service.record_backtested_factor(fid, market=MARKET)

            # 判据面：5 个样本里 0.01 垫底 → weak_icir 命中
            report = await pool_service.cleanup_suggestions(
                user_id=user, market=MARKET, universe=UNIVERSE
            )
            assert report["pool_size"] == 5
            assert report["archived_count"] == 0
            by_id = {it["factor_id"]: it for it in report["items"]}
            assert weak in by_id, "垫底因子未进清理建议"
            codes = {r["code"] for r in by_id[weak]["reasons"]}
            assert "weak_icir" in codes
            assert by_id[weak]["severity"] == "medium"
            assert report["summary"].get("weak_icir", 0) >= 1

            # 归档（非删除）：返回条数 + 行仍在（时间戳形式）
            result = await pool_service.archive_factors(user_id=user, factor_ids=[weak])
            assert result == {"archived": 1, "archived_ids": [weak], "skipped": []}
            again = await pool_service.archive_factors(user_id=user, factor_ids=[weak])
            assert again["archived"] == 0 and again["skipped"] == [weak]

            # 读路径一：注入摘要排除
            digest, ids = await pool_service.build_injection_digest(
                user_id=user, market=MARKET, universe=UNIVERSE
            )
            assert weak not in ids, "已归档因子仍被注入"
            assert "池内共 4 条" in digest, "SOTA 标杆仍把归档因子算进去"

            # 读路径二：清建议排除 + 归档计数
            report2 = await pool_service.cleanup_suggestions(
                user_id=user, market=MARKET, universe=UNIVERSE
            )
            assert report2["pool_size"] == 4
            assert report2["archived_count"] == 1
            assert weak not in {it["factor_id"] for it in report2["items"]}

            # 读路径三：列表默认排除；include_archived 才可见且带时间戳
            default_list = await pool_service.list_pool_factors(
                user_id=user, market=MARKET, universe=UNIVERSE, limit=50
            )
            assert weak not in {r["factor_id"] for r in default_list["items"]}
            with_archived = await pool_service.list_pool_factors(
                user_id=user,
                market=MARKET,
                universe=UNIVERSE,
                limit=50,
                include_archived=True,
            )
            row = next(r for r in with_archived["items"] if r["factor_id"] == weak)
            assert row["archived_at"] is not None

            # 读路径四：谱系图
            graph = await pool_service.pool_graph(
                user_id=user, market=MARKET, universe=UNIVERSE, max_nodes=50
            )
            assert weak not in {n["factor_id"] for n in graph["nodes"]}
            graph_all = await pool_service.pool_graph(
                user_id=user,
                market=MARKET,
                universe=UNIVERSE,
                max_nodes=50,
                include_archived=True,
            )
            assert weak in {n["factor_id"] for n in graph_all["nodes"]}

            # 读路径五：总览聚合只算活跃 + 归档计数
            overview = await pool_service.pool_overview(
                user_id=user, market=MARKET, universe=UNIVERSE
            )
            assert overview["total"] == 4
            assert overview["archived_count"] == 1

            # 恢复：回到注入与总览
            restored = await pool_service.unarchive_factors(
                user_id=user, factor_ids=[weak]
            )
            assert restored == {"restored": 1, "restored_ids": [weak], "skipped": []}
            _, ids3 = await pool_service.build_injection_digest(
                user_id=user, market=MARKET, universe=UNIVERSE
            )
            assert weak in ids3
            overview2 = await pool_service.pool_overview(
                user_id=user, market=MARKET, universe=UNIVERSE
            )
            assert overview2["total"] == 5
            assert overview2["archived_count"] == 0
        finally:
            await _cleanup([weak, *strong], [user])
            await close_database()

    @pytest.mark.asyncio
    async def test_archive_and_restore_are_user_scoped(self):
        from backend.shared.database_manager_v2 import close_database, get_session

        await _skip_if_no_db()
        run = _run_id()
        owner = run
        intruder = f"{run}-other"
        fid = f"{run}_f"
        try:
            async with get_session() as session:
                await _seed_factor(session, factor_id=fid, user_id=owner, icir=0.3)
            assert await pool_service.record_backtested_factor(fid, market=MARKET)

            stolen = await pool_service.archive_factors(
                user_id=intruder, factor_ids=[fid]
            )
            assert stolen["archived"] == 0 and stolen["skipped"] == [fid]
            async with get_session(read_only=True) as session:
                still = (
                    await session.execute(
                        text(
                            f"SELECT archived_at FROM {POOL_TABLE} "
                            "WHERE factor_id = :fid"
                        ),
                        {"fid": fid},
                    )
                ).scalar()
            assert still is None, "跨用户归档动了别人的池行"

            assert await pool_service.archive_factors(
                user_id=owner, factor_ids=[fid]
            ) == {"archived": 1, "archived_ids": [fid], "skipped": []}
            stolen_restore = await pool_service.unarchive_factors(
                user_id=intruder, factor_ids=[fid]
            )
            assert stolen_restore["restored"] == 0
            assert stolen_restore["skipped"] == [fid]
        finally:
            await _cleanup([fid], [owner, intruder])
            await close_database()


class TestFactorCategory:
    """因子分类：总览分类区块 + 列表分类过滤（归类单源 factor_classify）。"""

    def test_category_breakdown_aggregates_and_ranks(self):
        """纯函数聚合：计数/占比/有值均值/覆盖率/代表因子/other 垫底。"""
        rows = [
            # 动量族 ×2（一个无 IC 无池评分——缺失不按 0 计）
            {
                "factor_name": "Momentum_5D",
                "description": "[动量因子] 5日动量",
                "ic_value": 0.02,
                "icir": 0.5,
                "pool_score": 0.8,
                "novelty": 0.9,
            },
            {
                "factor_name": "mom_x",
                "description": "[动量因子] 变体",
                "ic_value": None,
                "icir": None,
                "pool_score": None,
                "novelty": None,
            },
            # 隔夜族 ×1
            {
                "factor_name": "OvernightReturn",
                "description": "[隔夜信息因子] 隔夜收益",
                "ic_value": 0.01,
                "icir": 0.2,
                "pool_score": 0.5,
                "novelty": 0.4,
            },
            # 前缀未命中规则 → other（如实呈现，不猜）
            {
                "factor_name": "mystery",
                "description": "[某种全新因子] x",
                "ic_value": 0.03,
                "icir": None,
                "pool_score": 0.9,
                "novelty": 0.1,
            },
        ]
        out = pool_service._category_breakdown(rows, total=4)
        assert [d["category"] for d in out] == ["momentum", "overnight", "other"]
        mom = out[0]
        assert mom["label"] == "动量与趋势"
        assert mom["count"] == 2 and mom["share"] == pytest.approx(0.5)
        assert mom["avg_ic"] == pytest.approx(0.02) and mom["n_ic"] == 1
        assert mom["n_icir"] == 1
        assert mom["avg_pool_score"] == pytest.approx(0.8)
        # 代表因子按池评分降序（None 垫底）
        assert mom["top_factors"] == ["Momentum_5D", "mom_x"]
        other = out[-1]
        assert other["category"] == "other" and other["label"] == "其他"
        assert other["top_factors"] == ["mystery"]

    def test_category_breakdown_median_and_saturation(self):
        """供给面（T-MV-02）：中位 IC 取有值样本；饱和度=计数÷最满真实类；other 恒 None。

        饱和度是「相对最满真实类别的填充度」（T-MV-13 配额制到来自会换成
        count/quota）；「其他」不是挖掘方向，不参与饱和度语义。
        """
        rows = [
            {
                "factor_name": "Momentum_5D",
                "description": "[动量因子] a",
                "ic_value": 0.01,
                "icir": None,
                "pool_score": 0.8,
                "novelty": None,
            },
            {
                "factor_name": "mom_x",
                "description": "[动量因子] b",
                "ic_value": 0.02,
                "icir": None,
                "pool_score": 0.7,
                "novelty": None,
            },
            {
                "factor_name": "mom_y",
                "description": "[动量因子] c",
                "ic_value": 0.30,
                "icir": None,
                "pool_score": 0.6,
                "novelty": None,
            },
            {
                "factor_name": "OvernightReturn",
                "description": "[隔夜信息因子] d",
                "ic_value": 0.05,
                "icir": None,
                "pool_score": 0.5,
                "novelty": None,
            },
            {
                "factor_name": "mystery",
                "description": "[某种全新因子] x",
                "ic_value": None,
                "icir": None,
                "pool_score": None,
                "novelty": None,
            },
        ]
        out = pool_service._category_breakdown(rows, total=5)
        by = {d["category"]: d for d in out}

        mom = by["momentum"]
        assert mom["median_ic"] == pytest.approx(0.02), "中位≠均值（0.11）——分布中心必须用中位"
        assert mom["saturation"] == pytest.approx(1.0), "最满的真实类饱和度=1"
        assert by["overnight"]["saturation"] == pytest.approx(1 / 3)
        # 单样本类：中位=该样本；无 IC 样本 → None（缺失绝不当 0）
        assert by["overnight"]["median_ic"] == pytest.approx(0.05)
        other = by["other"]
        assert other["saturation"] is None, "「其他」不是挖掘方向，不给饱和度"
        assert other["median_ic"] is None, "无 IC 样本 → None"

    def test_category_breakdown_saturation_none_when_no_real_category(self):
        """整池全落 other：没有真实类做分母 → 饱和度一律 None（不伪造 1.0）。"""
        rows = [
            {
                "factor_name": "mystery",
                "description": "[某种全新因子] x",
                "ic_value": 0.03,
                "icir": None,
                "pool_score": None,
                "novelty": None,
            }
        ]
        out = pool_service._category_breakdown(rows, total=1)
        assert out[0]["category"] == "other"
        assert out[0]["saturation"] is None
        assert out[0]["median_ic"] == pytest.approx(0.03)

    @pytest.mark.asyncio
    async def test_overview_breakdown_and_list_filter(self, tmp_path, monkeypatch):
        """真库：overview 带分类分布；列表按类过滤且行带类标签；类别隔离。"""
        from backend.shared.database_manager_v2 import close_database, get_session

        await _skip_if_no_db()
        monkeypatch.setenv("QM_FACTOR_POOL_PANEL_DIR", str(tmp_path / "panels"))
        run = _run_id()
        user = f"{run}-u"
        f_mom, f_ovn = f"{run}_m1", f"{run}_o1"
        try:
            async with get_session() as session:
                await _seed_factor(
                    session,
                    factor_id=f_mom,
                    user_id=user,
                    description="[动量因子] 5日动量",
                    ic=0.02,
                )
                await _seed_factor(
                    session,
                    factor_id=f_ovn,
                    user_id=user,
                    description="[隔夜信息因子] 隔夜收益",
                    ic=0.01,
                )
                await _seed_pool_row(session, factor_id=f_mom, user_id=user)
                await _seed_pool_row(session, factor_id=f_ovn, user_id=user)

            overview = await pool_service.pool_overview(
                user_id=user, market=MARKET, universe=UNIVERSE
            )
            cats = {d["category"]: d for d in overview["category_breakdown"]}
            assert cats["momentum"]["count"] == 1
            assert cats["overnight"]["count"] == 1
            assert sum(d["count"] for d in overview["category_breakdown"]) == (
                overview["total"]
            )

            only_ovn = await pool_service.list_pool_factors(
                user_id=user, market=MARKET, universe=UNIVERSE, category="overnight"
            )
            assert [i["factor_id"] for i in only_ovn["items"]] == [f_ovn]
            assert only_ovn["total"] == 1
            assert only_ovn["items"][0]["category"] == "overnight"
            assert only_ovn["items"][0]["category_label"] == "隔夜与跳空"
            assert only_ovn["items"][0]["raw_category_label"] == "隔夜信息因子"

            only_mom = await pool_service.list_pool_factors(
                user_id=user, market=MARKET, universe=UNIVERSE, category="momentum"
            )
            assert [i["factor_id"] for i in only_mom["items"]] == [f_mom]

            # 空类：合法类名但池内没有该类因子 → 空列表（不抛错）
            empty = await pool_service.list_pool_factors(
                user_id=user, market=MARKET, universe=UNIVERSE, category="valuation"
            )
            assert empty["total"] == 0 and empty["items"] == []
        finally:
            await _cleanup([f_mom, f_ovn], [user])
            await close_database()


class TestSeedDigest:
    """种子（父本）摘要（T-MV-01）：保序 used、如实 dropped、scope 与池一致。"""

    @pytest.mark.asyncio
    async def test_seed_digest_orders_used_and_reports_dropped(self):
        from backend.shared.database_manager_v2 import close_database, get_session

        await _skip_if_no_db()
        run = _run_id()
        user = run
        f1, f2 = f"{run}_f1", f"{run}_f2"
        ghost = f"{run}_ghost"
        try:
            async with get_session() as session:
                await _seed_factor(
                    session,
                    factor_id=f1,
                    user_id=user,
                    formula="rank(close/ref(close,20)-1)",
                    ic=0.05,
                    icir=0.7,
                )
                await _seed_factor(session, factor_id=f2, user_id=user)
                await _seed_pool_row(session, factor_id=f1, user_id=user)
                await _seed_pool_row(session, factor_id=f2, user_id=user)

            digest, used, dropped = await pool_service.build_seed_digest(
                [f2, ghost, f1], user_id=user, market=MARKET, universe=UNIVERSE
            )

            assert used == (f2, f1), "used 必须保序 = 请求顺序（卡片血统要能对照）"
            assert dropped == (ghost,)
            assert f"name-{f2}" in digest and f"name-{f1}" in digest
            assert "rank(close/ref(close,20)-1)" in digest, "父本公式要进摘要"
            assert ghost not in digest
        finally:
            await _cleanup([f1, f2], [user])
            await close_database()

    @pytest.mark.asyncio
    async def test_seed_digest_scope_isolation_and_archive(self):
        """跨用户 / 已归档的因子绝不进别人（或自己）的种子摘要。"""
        from backend.shared.database_manager_v2 import close_database, get_session

        await _skip_if_no_db()
        run = _run_id()
        user, other = f"{run}_u", f"{run}_u2"
        f_mine, f_other, f_arch = f"{run}_m", f"{run}_o", f"{run}_a"
        try:
            async with get_session() as session:
                await _seed_factor(session, factor_id=f_mine, user_id=user)
                await _seed_factor(session, factor_id=f_other, user_id=other)
                await _seed_factor(session, factor_id=f_arch, user_id=user)
                await _seed_pool_row(session, factor_id=f_mine, user_id=user)
                await _seed_pool_row(session, factor_id=f_other, user_id=other)
                await _seed_pool_row(session, factor_id=f_arch, user_id=user)
                await session.execute(
                    text(
                        f"UPDATE {POOL_TABLE} SET archived_at = now() "
                        "WHERE factor_id = :f"
                    ),
                    {"f": f_arch},
                )

            digest, used, dropped = await pool_service.build_seed_digest(
                [f_mine, f_other, f_arch],
                user_id=user,
                market=MARKET,
                universe=UNIVERSE,
            )

            assert used == (f_mine,)
            assert dropped == (f_other, f_arch)
            assert f"name-{f_other}" not in digest and f"name-{f_arch}" not in digest
        finally:
            await _cleanup([f_mine, f_other, f_arch], [user, other])
            await close_database()

    @pytest.mark.asyncio
    async def test_seed_digest_empty_request_returns_blank(self):
        assert await pool_service.build_seed_digest(
            [], user_id="u", market=MARKET, universe=UNIVERSE
        ) == ("", (), ())
