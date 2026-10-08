"""组合优化作业（mining_combo_optimize）——argv/配置/锁协议 + 端到端作业（真库）。

纪律：
- 作业是**子进程**（与池刷新同架构：pandas/scipy 重算绝不能跑在引擎事件
  循环里）；组合行就是作业的请求与结果载体（pending→running→done/failed）。
- 锁正在跑时报 409 由端点侧探活；子进程取锁是竞态的最终兜底——拿不到锁
  的作业把自己的行标 failed（不静默留 pending）。
- 集成测试用 t-combo-* 唯一 id 直插行 + tmp 面板目录，结束时连根拔。
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from uuid import uuid4

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:  # pragma: no cover - 环境相关
    from backend.scripts import mining_combo_optimize as mco
    from backend.services.engine.mining_plugins import pool_panels
    from backend.services.engine.mining_plugins.combo_optimizer import (
        DEFAULT_CONFIG,
        ComboConfig,
    )
    from backend.services.engine.routers import alpha_agent as aa
except Exception as _exc:  # noqa: BLE001
    mco = None
    aa = None
    _IMPORT_ERR = _exc

from fastapi import HTTPException

pytestmark = pytest.mark.skipif(
    mco is None or aa is None, reason="依赖不可用（需容器环境）"
)

MARKET = "a_share"


def _run_id() -> str:
    return f"t-combo-{uuid4().hex[:10]}"


def _as_json(value):
    """asyncpg 直读 JSONB 可能给 str（text() 查询无类型信息），统一解析。"""
    return json.loads(value) if isinstance(value, str) else value


@pytest.fixture()
def isolated_combo_env(tmp_path, monkeypatch):
    lock = tmp_path / "combo.lock"
    log_dir = tmp_path / "logs"
    monkeypatch.setenv("QM_COMBO_LOCK", str(lock))
    monkeypatch.setenv("QM_COMBO_LOG_DIR", str(log_dir))
    monkeypatch.setenv("QM_FACTOR_POOL_PANEL_DIR", str(tmp_path / "panels"))
    return types.SimpleNamespace(lock=lock, log_dir=log_dir, tmp=tmp_path)


class TestBuildRunCommand:
    def test_fixed_module_invocation(self):
        cmd = mco.build_run_command("abc123def456")
        assert cmd == [
            sys.executable,
            "-m",
            "backend.scripts.mining_combo_optimize",
            "--combo-id",
            "abc123def456",
        ]

    @pytest.mark.parametrize("bad", ["", "a b", "--force", "x;rm", "x\n", "x" * 200])
    def test_rejects_unsafe_combo_id(self, bad):
        with pytest.raises(ValueError, match="combo_id"):
            mco.build_run_command(bad)


class TestParseConfig:
    def test_roundtrip_full_dict(self):
        from dataclasses import asdict

        payload = asdict(DEFAULT_CONFIG)
        assert mco.parse_config(payload) == DEFAULT_CONFIG

    def test_missing_keys_fall_back_to_defaults(self):
        assert mco.parse_config({}) == DEFAULT_CONFIG
        cfg = mco.parse_config({"seed": 7, "maxiter": 20, "cost_rate": 0.01})
        assert isinstance(cfg, ComboConfig)
        assert cfg.seed == 7 and cfg.maxiter == 20 and cfg.cost_rate == 0.01
        assert cfg.popsize == DEFAULT_CONFIG.popsize

    def test_none_seed_means_default(self):
        assert mco.parse_config({"seed": None}).seed == DEFAULT_CONFIG.seed

    def test_unknown_keys_ignored(self):
        assert mco.parse_config({"whatever": 1, "seed": 3}).seed == 3

    @pytest.mark.parametrize("key", ["maxiter", "popsize", "max_days", "seed"])
    def test_bad_type_raises_with_key_name(self, key):
        with pytest.raises(ValueError, match=key):
            mco.parse_config({key: "不是数字"})

    def test_float_coercion_for_numeric(self):
        cfg = mco.parse_config({"train_ratio": "0.6", "time_budget_s": "30"})
        assert cfg.train_ratio == pytest.approx(0.6)
        assert cfg.time_budget_s == pytest.approx(30.0)

    @pytest.mark.parametrize("bad", [-1, 2**32, 2**53 - 1])
    def test_seed_out_of_range_rejected(self, bad):
        # scipy differential_evolution 的 seed 上限是 2³²−1；越界值必须在建行
        # 前 400，而不是建行 → 占全局锁 → 跑到 DE 才炸（评审发现 3 / 安全 M-3）。
        with pytest.raises(ValueError, match="seed"):
            mco.parse_config({"seed": bad})

    def test_seed_upper_bound_accepted(self):
        assert mco.parse_config({"seed": 2**32 - 1}).seed == 2**32 - 1


class TestComboLock:
    def test_exclusive_and_env_isolated(self, isolated_combo_env):
        assert mco.probe_combo_lock() is False
        handle = mco._acquire_combo_lock()
        assert handle is not None
        try:
            assert mco.probe_combo_lock() is True
            assert mco._acquire_combo_lock() is None  # 第二次拿不到
        finally:
            handle.close()
        assert mco.probe_combo_lock() is False

    def test_main_busy_exits_three(self, isolated_combo_env, monkeypatch):
        monkeypatch.setattr(mco, "_LOCK_WAIT_S", 0.05)  # 测试不等生产重试窗
        handle = mco._acquire_combo_lock()
        assert handle is not None
        try:
            # 锁被占：不启动作业、直接 3（标失败是尽力而为，DB 不可用也照退 3）
            assert mco.main(["--combo-id", "deadbeef"]) == 3
        finally:
            handle.close()

    def test_acquire_waits_out_transient_probe(self, isolated_combo_env):
        # 父进程探活会短暂持有 LOCK_EX（µs 级、每 200ms 一次）：子进程取锁必须
        # 带重试窗口，否则免费机器上会偶发「起不来/退出码 3」（评审发现 7）。
        import threading

        holder = mco._acquire_combo_lock()
        assert holder is not None
        threading.Timer(0.2, holder.close).start()
        got = mco._acquire_combo_lock(timeout_s=2.0)
        assert got is not None
        got.close()

    def test_symlink_lock_is_not_followed(self, isolated_combo_env):
        # 世界可写的 /tmp 里锁路径被换成符号链接时：绝不许 open("w") 跟随并
        # 截断目标文件（安全 L-3）；按「拿不到锁」处理。
        victim = isolated_combo_env.tmp / "victim.txt"
        victim.write_text("precious")
        isolated_combo_env.lock.symlink_to(victim)

        assert mco._acquire_combo_lock() is None
        assert mco.probe_combo_lock() is False
        assert victim.read_text() == "precious"


class TestValidateFactorIds:
    def test_dedupes_preserving_order(self):
        assert mco.validate_factor_ids(["b", "a", "b"]) == ["b", "a"]

    def test_limits(self):
        with pytest.raises(ValueError, match="至少"):
            mco.validate_factor_ids(["only-one"])
        with pytest.raises(ValueError, match="上限"):
            mco.validate_factor_ids([f"f{i}" for i in range(mco.MAX_FACTORS + 1)])

    def test_raw_length_gate_counts_before_dedupe(self):
        # 去重后只剩 2 个（合法），但原始长度超限必须直接拒绝：旧实现逐元素
        # ``fid not in order`` 是 O(N²)，一个请求就能把引擎事件循环钉死
        # （安全评审 H-1）——闸门必须落在任何逐元素扫描之前。
        ids = ["f1", "f2"] + ["f1"] * (mco.MAX_FACTORS + 8)
        with pytest.raises(ValueError, match="上限"):
            mco.validate_factor_ids(ids)

    def test_huge_input_rejected_without_quadratic_scan(self):
        import time

        ids = [f"f{i}" for i in range(200_000)]
        t0 = time.perf_counter()
        with pytest.raises(ValueError, match="上限"):
            mco.validate_factor_ids(ids)
        # 二次扫描是分钟级；线性/常数级必须在 1s 内拒绝
        assert time.perf_counter() - t0 < 1.0

    def test_unsafe_token_rejected(self):
        with pytest.raises(ValueError, match="非法"):
            mco.validate_factor_ids(["ok", "--flag"])


class TestSpawnCombo:
    """spawn_combo 启动面（评审测试缺口：符号链接拒绝 / 原子换名 / 确认取锁从未被测）。"""

    @pytest.mark.asyncio
    async def test_busy_lock_raises_busy(self, isolated_combo_env):
        holder = mco._acquire_combo_lock()
        assert holder is not None
        try:
            with pytest.raises(mco.ComboBusyError, match="运行"):
                await mco.spawn_combo("f" * 32)
        finally:
            holder.close()

    @pytest.mark.asyncio
    async def test_symlinked_log_path_rejected_without_touching_target(
        self, isolated_combo_env
    ):
        cid = uuid4().hex
        isolated_combo_env.log_dir.mkdir(parents=True, exist_ok=True)
        victim = isolated_combo_env.tmp / "victim.txt"
        victim.write_text("precious")
        (isolated_combo_env.log_dir / f"{cid}.log").symlink_to(victim)

        with pytest.raises(mco.ComboStartError, match="符号链接"):
            await mco.spawn_combo(cid)
        assert victim.read_text() == "precious"

    @pytest.mark.asyncio
    async def test_happy_path_confirms_lock_then_reaps(
        self, isolated_combo_env, monkeypatch
    ):
        import os
        import signal

        cid = uuid4().hex
        # 替身作业：抢锁 → 睡住；父进程必须「确认子进程持锁」后才回 started
        child_code = (
            "import fcntl, os, time;"
            "f = open(os.environ['QM_COMBO_LOCK'], 'a+');"
            "fcntl.flock(f, fcntl.LOCK_EX);"
            "time.sleep(30)"
        )
        monkeypatch.setattr(
            mco,
            "build_run_command",
            lambda combo_id: [sys.executable, "-c", child_code],
        )
        info = await mco.spawn_combo(cid)
        try:
            assert info["started"] is True
            assert info["confirmed"] is True
            assert info["pid"] > 0
            assert (isolated_combo_env.log_dir / f"{cid}.log").exists()
            assert not list(isolated_combo_env.log_dir.glob("*.tmp"))  # 换名后无残留
        finally:
            os.kill(info["pid"], signal.SIGTERM)


async def _skip_if_no_db() -> None:
    from backend.shared.database_manager_v2 import get_session

    try:
        async with get_session(read_only=True) as probe:
            await probe.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"DB 不可用: {exc}")


async def _seed_combo(session, *, combo_id: str, user_id: str, factor_ids, config=None):
    await session.execute(
        text(
            "INSERT INTO rd_agent_factor_combos "
            "(combo_id, user_id, market, universe, name, factor_ids, weights, "
            " train_metrics, status) "
            "VALUES (:cid, :u, :m, '', '', CAST(:fids AS JSONB), '{}'::jsonb, "
            " CAST(:cfg AS JSONB), 'pending')"
        ),
        {
            "cid": combo_id,
            "u": user_id,
            "m": MARKET,
            "fids": json.dumps(factor_ids),
            # 与 create_combo_row 同形：请求配置嵌在 train_metrics.config
            "cfg": json.dumps({"config": config} if config else {}),
        },
    )


async def _seed_factor(
    session, *, factor_id: str, user_id: str, formula: str = "close/mean(close,20)"
):
    """rd_agent_factors 直插（列集与 test_pool_service 同款）。

    注意：``panel_ref`` 在**池表** rd_agent_factor_pool（非本表）——面板 +
    池行由 ``pool_service.record_backtested_factor`` 生产（真实链路）。
    """
    await session.execute(
        text(
            "INSERT INTO rd_agent_factors "
            "(factor_id, factor_name, factor_code, status, user_id, metadata_json, "
            " market, universe, factor_formulation, ic_value, rank_ic) "
            "VALUES (:fid, :name, '-', 'completed', :u, '{}'::jsonb, "
            " :m, '', :formula, 0.03, 0.04)"
        ),
        {
            "fid": factor_id,
            "name": f"name-{factor_id}",
            "u": user_id,
            "m": MARKET,
            "formula": formula,
        },
    )


async def _cleanup(combo_ids, factor_ids) -> None:
    from backend.shared.database_manager_v2 import get_session

    async with get_session() as session:
        if combo_ids:
            await session.execute(
                text("DELETE FROM rd_agent_factor_combos WHERE combo_id = ANY(:ids)"),
                {"ids": combo_ids},
            )
        if factor_ids:
            await session.execute(
                text(
                    "DELETE FROM rd_agent_factor_edges "
                    "WHERE src_factor_id = ANY(:ids) OR dst_factor_id = ANY(:ids)"
                ),
                {"ids": factor_ids},
            )
            await session.execute(
                text("DELETE FROM rd_agent_factor_pool WHERE factor_id = ANY(:ids)"),
                {"ids": factor_ids},
            )
            await session.execute(
                text("DELETE FROM rd_agent_factors WHERE factor_id = ANY(:ids)"),
                {"ids": factor_ids},
            )


def _series_from_arrays(z: np.ndarray, fret: np.ndarray):
    """(天数, 标的数) 数组 → (因子值, 前瞻收益) 两条 MultiIndex Series。"""
    days = pd.date_range("2025-01-06", periods=z.shape[0], freq="B")
    symbols = [f"SZ{i:06d}" for i in range(1, z.shape[1] + 1)]
    idx = pd.MultiIndex.from_product([days, symbols], names=["datetime", "instrument"])
    return pd.Series(z.ravel(), index=idx), pd.Series(fret.ravel(), index=idx)


def _write_panel(fid: str, z: np.ndarray, fret: np.ndarray) -> None:
    """合成面板直接落盘：z/fret 形如 (天数, 标的数)。"""
    days = pd.date_range("2025-01-06", periods=z.shape[0], freq="B")
    symbols = [f"SZ{i:06d}" for i in range(1, z.shape[1] + 1)]
    idx = pd.MultiIndex.from_product([days, symbols], names=["datetime", "instrument"])
    values = pd.Series(z.ravel(), index=idx)
    ret = pd.Series(fret.ravel(), index=idx)
    ref = pool_panels.write_panel(MARKET, fid, values, forward_return=ret)
    assert ref is not None


def _two_panel_shapes(n_days=30, n_sym=8, seed=7):
    rng = np.random.default_rng(seed)
    t1 = rng.normal(size=(n_days, n_sym))
    t2 = rng.normal(size=(n_days, n_sym))
    fret = 0.05 * t1 - 0.03 * t2
    return t1, t2, fret


class TestJobEndToEnd:
    @pytest.mark.asyncio
    async def test_run_job_writes_weights_metrics_and_curve(self, isolated_combo_env):
        from backend.shared.database_manager_v2 import close_database, get_session

        await _skip_if_no_db()
        run = _run_id()
        combo_id = f"{run}-c1"
        f1, f2 = f"{run}_f1", f"{run}_f2"
        try:
            t1, t2, fret = _two_panel_shapes()
            _write_panel(f1, t1, fret)
            _write_panel(f2, t2, fret)
            async with get_session() as session:
                await _seed_combo(
                    session,
                    combo_id=combo_id,
                    user_id=run,
                    factor_ids=[f1, f2],
                    config={
                        "seed": 1,
                        "maxiter": 8,
                        "popsize": 4,
                        "time_budget_s": 60.0,
                    },
                )

            summary = await mco.run_job(combo_id)
            assert summary.get("status") == "done"

            async with get_session(read_only=True) as session:
                row = (
                    (
                        await session.execute(
                            text(
                                "SELECT status, weights, train_window, "
                                "train_metrics, valid_metrics "
                                "FROM rd_agent_factor_combos WHERE combo_id = :cid"
                            ),
                            {"cid": combo_id},
                        )
                    )
                    .mappings()
                    .first()
                )
            assert row is not None and row["status"] == "done"
            weights = _as_json(row["weights"])
            assert set(weights) == {f1, f2}
            assert sum(abs(v) for v in weights.values()) == pytest.approx(1.0, abs=1e-9)
            assert "train" in (row["train_window"] or "")
            assert _as_json(row["train_metrics"])["config"]["seed"] == 1
            valid = _as_json(row["valid_metrics"])
            assert valid["mean_rank_ic"] is not None
            assert valid["curve"]["dates"]

            # 已完成的行再次触发 → 跳过（幂等护栏，不重算不覆盖）
            again = await mco.run_job(combo_id)
            assert again.get("skipped") == "done"

            # 读面：详情（属主）齐全 / 非属主 None；列表带汇总与两窗 IC
            detail = await mco.get_combo(combo_id, user_id=run)
            assert detail and detail["status"] == "done"
            assert isinstance(detail["weights"], dict) and detail["weights"]
            assert detail["valid_metrics"]["curve"]["dates"]
            assert await mco.get_combo(combo_id, user_id=f"{run}-x") is None
            listing = await mco.list_combos(user_id=run, market=MARKET)
            assert listing["total"] >= 1
            item = next(x for x in listing["items"] if x["combo_id"] == combo_id)
            assert item["n_factors"] == 2
            assert item["valid_mean_rank_ic"] == pytest.approx(valid["mean_rank_ic"])
        finally:
            await _cleanup([combo_id], [f1, f2])
            await close_database()

    @pytest.mark.asyncio
    async def test_run_job_marks_failed_on_missing_panels(self, isolated_combo_env):
        from backend.shared.database_manager_v2 import close_database, get_session

        await _skip_if_no_db()
        run = _run_id()
        combo_id = f"{run}-c2"
        f1, f2 = f"{run}_f1", f"{run}_f2"  # 面板刻意不写
        try:
            async with get_session() as session:
                await _seed_combo(
                    session,
                    combo_id=combo_id,
                    user_id=run,
                    factor_ids=[f1, f2],
                    config={"maxiter": 5, "popsize": 4},
                )
            with pytest.raises(ValueError):
                await mco.run_job(combo_id)
            async with get_session(read_only=True) as session:
                row = (
                    await session.execute(
                        text(
                            "SELECT status, error FROM rd_agent_factor_combos "
                            "WHERE combo_id = :cid"
                        ),
                        {"cid": combo_id},
                    )
                ).first()
            assert row is not None
            assert row[0] == "failed" and row[1], "失败必须留因（不许静默 pending）"
        finally:
            await _cleanup([combo_id], [f1, f2])
            await close_database()


class TestCreateComboRow:
    @pytest.mark.asyncio
    async def test_ownership_and_panel_validation(self, isolated_combo_env):
        from backend.services.engine.mining_plugins import pool_service
        from backend.shared.database_manager_v2 import close_database, get_session

        await _skip_if_no_db()
        run = _run_id()
        owner = f"{run}-owner"
        other_user = f"{run}-other"
        f_ok, f_nopanel, f_other = f"{run}_ok", f"{run}_nopanel", f"{run}_other"
        combo_id = None
        try:
            async with get_session() as session:
                await _seed_factor(session, factor_id=f_ok, user_id=owner)
                await _seed_factor(session, factor_id=f_nopanel, user_id=owner)
                await _seed_factor(session, factor_id=f_other, user_id=other_user)
            # 真实链路生产「面板文件 + 池行 panel_ref」：write_panel 直写不经池表，
            # 校验读的是池表
            t1, t2, fret = _two_panel_shapes(n_days=25)
            v1, r1 = _series_from_arrays(t1, fret)
            assert await pool_service.record_backtested_factor(
                f_ok, market=MARKET, values=v1, forward_return=r1
            )

            with pytest.raises(ValueError, match="不存在或不属于"):
                await mco.create_combo_row(
                    user_id=owner,
                    market=MARKET,
                    universe="",
                    factor_ids=[f_ok, f_other],
                    name="",
                    seed=1,
                )
            with pytest.raises(ValueError, match="面板"):
                await mco.create_combo_row(
                    user_id=owner,
                    market=MARKET,
                    universe="",
                    factor_ids=[f_ok, f_nopanel],
                    name="",
                    seed=1,
                )

            # 合法路径：补齐 f_nopanel 的面板 + 池行后再建（重复因子应去重）
            v2, r2 = _series_from_arrays(t2, fret)
            assert await pool_service.record_backtested_factor(
                f_nopanel, market=MARKET, values=v2, forward_return=r2
            )
            combo_id = await mco.create_combo_row(
                user_id=owner,
                market=MARKET,
                universe="",
                factor_ids=[f_ok, f_ok, f_nopanel],
                name="组合A",
                seed=9,
            )
            assert combo_id and len(combo_id) >= 16
            async with get_session(read_only=True) as session:
                row = (
                    (
                        await session.execute(
                            text(
                                "SELECT user_id, factor_ids, status, train_metrics, name "
                                "FROM rd_agent_factor_combos WHERE combo_id = :cid"
                            ),
                            {"cid": combo_id},
                        )
                    )
                    .mappings()
                    .first()
                )
            assert row["user_id"] == owner and row["status"] == "pending"
            assert _as_json(row["factor_ids"]) == [f_ok, f_nopanel], "重复因子应去重"
            assert _as_json(row["train_metrics"])["config"]["seed"] == 9
            assert row["name"] == "组合A"
        finally:
            await _cleanup([combo_id] if combo_id else [], [f_ok, f_nopanel, f_other])
            await close_database()

    @pytest.mark.asyncio
    async def test_cross_market_factor_rejected_at_create(self, isolated_combo_env):
        # 面板按市场分目录（panel_ref = "{market}/{factor_id}.parquet"）：因子在
        # a_share 有面板、以 hong_kong 提交时必须在建行前 400（评审发现 4：旧
        # 实现无 market 谓词，200 建行后子进程读不到 HK 面板才失败，报错还指向
        # 错误的修复动作「重跑 rebuild --panels」）。
        from backend.services.engine.mining_plugins import pool_service
        from backend.shared.database_manager_v2 import close_database, get_session

        await _skip_if_no_db()
        run = _run_id()
        f1, f2 = f"{run}_cm1", f"{run}_cm2"
        combo_id = None
        try:
            async with get_session() as session:
                await _seed_factor(session, factor_id=f1, user_id="u-cm")
                await _seed_factor(session, factor_id=f2, user_id="u-cm")
            t1, t2, fret = _two_panel_shapes(n_days=25)
            v1, r1 = _series_from_arrays(t1, fret)
            assert await pool_service.record_backtested_factor(
                f1, market=MARKET, values=v1, forward_return=r1
            )
            v2, r2 = _series_from_arrays(t2, fret)
            assert await pool_service.record_backtested_factor(
                f2, market=MARKET, values=v2, forward_return=r2
            )

            with pytest.raises(ValueError, match="面板"):
                await mco.create_combo_row(
                    user_id="u-cm",
                    market="hong_kong",
                    universe="",
                    factor_ids=[f1, f2],
                    name="",
                    seed=1,
                )
            # 同市场照常建行（防止谓词写死到错误方向）
            combo_id = await mco.create_combo_row(
                user_id="u-cm",
                market=MARKET,
                universe="",
                factor_ids=[f1, f2],
                name="",
                seed=1,
            )
            assert combo_id
        finally:
            await _cleanup([combo_id] if combo_id else [], [f1, f2])
            await close_database()


class TestStatusLifecycleGuards:
    """状态收敛防线：终态不许被翻、死亡进程的 running/pending 必须收敛（评审 6/2）。"""

    @pytest.mark.asyncio
    async def test_mark_failed_never_clobbers_done(self, isolated_combo_env):
        from backend.shared.database_manager_v2 import close_database, get_session

        await _skip_if_no_db()
        cid_done, cid_pending = _run_id(), _run_id()
        try:
            async with get_session() as session:
                await _seed_combo(
                    session, combo_id=cid_done, user_id="u", factor_ids=["a", "b"]
                )
                await _seed_combo(
                    session, combo_id=cid_pending, user_id="u", factor_ids=["a", "b"]
                )
                await session.execute(
                    text(
                        "UPDATE rd_agent_factor_combos SET status='done' "
                        "WHERE combo_id=:c"
                    ),
                    {"c": cid_done},
                )
            # 对已 done 的行补标失败（重放/锁被占路径）必须无效
            await mco.mark_failed_row(cid_done, "已有组合作业在运行")
            await mco.mark_failed_row(cid_pending, "真实失败")
            async with get_session(read_only=True) as session:
                rows = (
                    (
                        await session.execute(
                            text(
                                "SELECT combo_id, status, error FROM "
                                "rd_agent_factor_combos WHERE combo_id = ANY(:ids)"
                            ),
                            {"ids": [cid_done, cid_pending]},
                        )
                    )
                    .mappings()
                    .all()
                )
            by_id = {r["combo_id"]: r for r in rows}
            assert by_id[cid_done]["status"] == "done", "终态不许被 failed 覆盖"
            assert by_id[cid_done]["error"] is None
            assert by_id[cid_pending]["status"] == "failed"
        finally:
            await _cleanup([cid_done, cid_pending], [])
            await close_database()

    @pytest.mark.asyncio
    async def test_reconcile_marks_orphaned_running_failed(self, isolated_combo_env):
        # 子进程被 OOM/SIGKILL/容器重启卷走：status 停在 running、锁空闲、
        # updated_at 陈旧 → 惰性收敛为 failed（模块 docstring 的「不许静默
        # leave pending/running」对进程死亡路径的兑现）。
        from backend.shared.database_manager_v2 import close_database, get_session

        await _skip_if_no_db()
        cid = _run_id()
        try:
            async with get_session() as session:
                await _seed_combo(
                    session, combo_id=cid, user_id="u1", factor_ids=["a", "b"]
                )
                await session.execute(
                    text(
                        "UPDATE rd_agent_factor_combos SET status='running', "
                        "updated_at = NOW() - INTERVAL '10 minutes' "
                        "WHERE combo_id=:c"
                    ),
                    {"c": cid},
                )
            assert await mco.reconcile_stale_row(cid, user_id="u1") is True
            async with get_session(read_only=True) as session:
                status = (
                    await session.execute(
                        text(
                            "SELECT status FROM rd_agent_factor_combos "
                            "WHERE combo_id=:c"
                        ),
                        {"c": cid},
                    )
                ).scalar()
            assert status == "failed"
        finally:
            await _cleanup([cid], [])
            await close_database()

    @pytest.mark.asyncio
    async def test_reconcile_respects_fresh_rows_and_held_lock(
        self, isolated_combo_env
    ):
        from backend.shared.database_manager_v2 import close_database, get_session

        await _skip_if_no_db()
        fresh, locked = _run_id(), _run_id()
        holder = mco._acquire_combo_lock()
        assert holder is not None
        try:
            async with get_session() as session:
                await _seed_combo(
                    session, combo_id=fresh, user_id="u1", factor_ids=["a", "b"]
                )
                await session.execute(
                    text(
                        "UPDATE rd_agent_factor_combos SET status='running' "
                        "WHERE combo_id=:c"
                    ),
                    {"c": fresh},
                )
                await _seed_combo(
                    session, combo_id=locked, user_id="u1", factor_ids=["a", "b"]
                )
                await session.execute(
                    text(
                        "UPDATE rd_agent_factor_combos SET status='running', "
                        "updated_at = NOW() - INTERVAL '10 minutes' "
                        "WHERE combo_id=:c"
                    ),
                    {"c": locked},
                )
            # 刚置 running（宽限期内）：宽限窗口给足，不动
            assert await mco.reconcile_stale_row(fresh, user_id="u1") is False
            # updated_at 陈旧但锁被持有（作业还活着）：不动
            assert await mco.reconcile_stale_row(locked, user_id="u1") is False
        finally:
            holder.close()
            await _cleanup([fresh, locked], [])
            await close_database()


def _fake_request(user_id: str = "u1", tenant_id: str = "default"):
    return types.SimpleNamespace(
        state=types.SimpleNamespace(user={"user_id": user_id, "tenant_id": tenant_id})
    )


class TestComboEndpoints:
    """端点契约：owner 恒为鉴权身份；建行→spawn 顺序；400/409/500 映射；
    启动失败的行当场标 failed；列表/详情 user-scoped（详情非属主 404）。"""

    @pytest.mark.asyncio
    async def test_optimize_creates_row_then_spawns(
        self, monkeypatch, isolated_combo_env
    ):
        calls: dict = {}

        async def fake_create(**kwargs):
            calls["create"] = kwargs
            return "cid-1"

        async def fake_spawn(combo_id):
            calls["spawn"] = combo_id
            return {
                "started": True,
                "pid": 1,
                "log_path": "/tmp/x.log",
                "confirmed": True,
            }

        monkeypatch.setattr(mco, "create_combo_row", fake_create)
        monkeypatch.setattr(mco, "spawn_combo", fake_spawn)
        resp = await aa.post_combo_optimize(
            _fake_request("u1"),
            body=aa.ComboOptimizeRequest(
                market="a_share",
                universe=" csi300 ",
                factor_ids=["a", "b"],
                name="X",
                seed=7,
            ),
        )
        assert resp["data"]["combo_id"] == "cid-1"
        assert resp["data"]["status"] == "pending"
        assert resp["data"]["started"] is True
        assert calls["create"] == {
            "user_id": "u1",
            "market": "a_share",
            "universe": "csi300",
            "factor_ids": ["a", "b"],
            "name": "X",
            "seed": 7,
        }
        assert calls["spawn"] == "cid-1"

    @pytest.mark.asyncio
    async def test_bad_market_400_before_create(self, monkeypatch):
        async def boom(**kwargs):  # pragma: no cover - 不应被调用
            raise AssertionError("create 不应被调用")

        monkeypatch.setattr(mco, "create_combo_row", boom)
        with pytest.raises(HTTPException) as err:
            await aa.post_combo_optimize(
                _fake_request(),
                body=aa.ComboOptimizeRequest(market="nasdaq", factor_ids=["a", "b"]),
            )
        assert err.value.status_code == 400

    @pytest.mark.asyncio
    async def test_validation_error_400_no_spawn(self, monkeypatch, isolated_combo_env):
        async def fake_create(**kwargs):
            raise ValueError("因子不存在或不属于当前用户：x")

        async def boom(combo_id):  # pragma: no cover - 不应被调用
            raise AssertionError("spawn 不应被调用")

        monkeypatch.setattr(mco, "create_combo_row", fake_create)
        monkeypatch.setattr(mco, "spawn_combo", boom)
        with pytest.raises(HTTPException) as err:
            await aa.post_combo_optimize(
                _fake_request(),
                body=aa.ComboOptimizeRequest(factor_ids=["a", "b"]),
            )
        assert err.value.status_code == 400
        assert "不存在或不属于" in err.value.detail

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("exc", "expected"),
        [(mco.ComboBusyError("忙"), 409), (mco.ComboStartError("起不来"), 500)],
    )
    async def test_spawn_failure_marks_row_and_maps_status(
        self, monkeypatch, isolated_combo_env, exc, expected
    ):
        async def fake_create(**kwargs):
            return "cid-2"

        async def fake_spawn(combo_id):
            raise exc

        marked: list = []

        async def fake_mark(combo_id, error):
            marked.append((combo_id, error))

        monkeypatch.setattr(mco, "create_combo_row", fake_create)
        monkeypatch.setattr(mco, "spawn_combo", fake_spawn)
        monkeypatch.setattr(mco, "mark_failed_row", fake_mark)
        with pytest.raises(HTTPException) as err:
            await aa.post_combo_optimize(
                _fake_request(),
                body=aa.ComboOptimizeRequest(factor_ids=["a", "b"]),
            )
        assert err.value.status_code == expected
        assert marked and marked[0][0] == "cid-2" and str(exc) in marked[0][1]

    @pytest.mark.asyncio
    async def test_list_passes_scope_and_market_none_for_all(self, monkeypatch):
        calls: dict = {}

        async def fake_list(**kwargs):
            calls.update(kwargs)
            return {"total": 0, "items": []}

        monkeypatch.setattr(mco, "list_combos", fake_list)
        await aa.get_combos(_fake_request("u9"), market="a_share", limit=10, offset=5)
        assert calls == {
            "user_id": "u9",
            "market": "a_share",
            "limit": 10,
            "offset": 5,
        }
        await aa.get_combos(_fake_request("u9"), market="", limit=20, offset=0)
        assert calls["market"] is None

    @pytest.mark.asyncio
    async def test_list_bad_market_400(self):
        with pytest.raises(HTTPException) as err:
            await aa.get_combos(_fake_request(), market="nasdaq", limit=20, offset=0)
        assert err.value.status_code == 400

    @pytest.mark.asyncio
    async def test_detail_owner_scoped_404(self, monkeypatch):
        async def fake_get(combo_id, *, user_id):
            assert combo_id == "c1" and user_id == "u1"
            return None

        monkeypatch.setattr(mco, "get_combo", fake_get)
        with pytest.raises(HTTPException) as err:
            await aa.get_combo_detail(_fake_request("u1"), "c1")
        assert err.value.status_code == 404

    @pytest.mark.asyncio
    async def test_detail_returns_row(self, monkeypatch):
        async def fake_get(combo_id, *, user_id):
            return {"combo_id": combo_id, "status": "done", "weights": {"a": 1.0}}

        monkeypatch.setattr(mco, "get_combo", fake_get)
        resp = await aa.get_combo_detail(_fake_request("u1"), "c1")
        assert resp["data"]["status"] == "done"
        assert resp["data"]["weights"] == {"a": 1.0}
        assert "user_id" not in resp["data"]  # 详情不回显属主 ID（安全 L-4）

    @pytest.mark.asyncio
    async def test_busy_probe_409_without_creating_row(
        self, monkeypatch, isolated_combo_env
    ):
        # 锁被占时探活前置 409：连行都不建（安全 M-1：此前每次被拒都留 failed 行）
        async def boom(**kwargs):  # pragma: no cover - 不应被调用
            raise AssertionError("create 不应被调用")

        monkeypatch.setattr(mco, "probe_combo_lock", lambda: True)
        monkeypatch.setattr(mco, "create_combo_row", boom)
        with pytest.raises(HTTPException) as err:
            await aa.post_combo_optimize(
                _fake_request(),
                body=aa.ComboOptimizeRequest(factor_ids=["a", "b"]),
            )
        assert err.value.status_code == 409

    @pytest.mark.asyncio
    async def test_spawn_unexpected_error_marks_failed_and_500(
        self, monkeypatch, isolated_combo_env
    ):
        # spawn 抛非托管异常（评审 8）：行不许静默 pending，兜底标 failed + 500
        async def fake_create(**kwargs):
            return "cid-3"

        async def boom(combo_id):
            raise RuntimeError("fd 用尽风格故障")

        marked: list = []

        async def fake_mark(combo_id, error):
            marked.append((combo_id, error))

        monkeypatch.setattr(mco, "create_combo_row", fake_create)
        monkeypatch.setattr(mco, "spawn_combo", boom)
        monkeypatch.setattr(mco, "mark_failed_row", fake_mark)
        with pytest.raises(HTTPException) as err:
            await aa.post_combo_optimize(
                _fake_request(),
                body=aa.ComboOptimizeRequest(factor_ids=["a", "b"]),
            )
        assert err.value.status_code == 500
        assert marked and marked[0][0] == "cid-3"

    @pytest.mark.asyncio
    async def test_detail_reconciles_orphaned_running(self, monkeypatch):
        # pending/running 的详情请求触发惰性收敛：死行先标 failed 再回读（评审 2）
        rows = [
            {"combo_id": "c1", "status": "running", "weights": {}},
            {
                "combo_id": "c1",
                "status": "failed",
                "error": "作业进程已消失",
                "weights": {},
            },
        ]
        calls = {"get": 0, "recon": 0}

        async def fake_get(combo_id, *, user_id):
            calls["get"] += 1
            return rows[min(calls["get"], len(rows)) - 1]

        async def fake_recon(combo_id, *, user_id):
            calls["recon"] += 1
            return True

        monkeypatch.setattr(mco, "get_combo", fake_get)
        monkeypatch.setattr(mco, "reconcile_stale_row", fake_recon)
        resp = await aa.get_combo_detail(_fake_request("u1"), "c1")
        assert resp["data"]["status"] == "failed"
        assert calls["recon"] == 1
        assert calls["get"] == 2

    @pytest.mark.asyncio
    async def test_detail_skips_reconcile_for_terminal(self, monkeypatch):
        async def fake_get(combo_id, *, user_id):
            return {"combo_id": combo_id, "status": "failed", "weights": {}}

        async def boom(combo_id, *, user_id):  # pragma: no cover - 不应被调用
            raise AssertionError("终态不应触发收敛")

        monkeypatch.setattr(mco, "get_combo", fake_get)
        monkeypatch.setattr(mco, "reconcile_stale_row", boom)
        resp = await aa.get_combo_detail(_fake_request("u1"), "c1")
        assert resp["data"]["status"] == "failed"

    def test_universe_length_capped(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            aa.ComboOptimizeRequest(
                market="a_share", universe="x" * 65, factor_ids=["a", "b"]
            )
