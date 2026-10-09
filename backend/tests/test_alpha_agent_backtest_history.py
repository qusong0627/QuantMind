"""单因子回测历史台账：一次运行一行，供后续对比（用户原话「后面好对比啊」）。

背景（2026-10-09）：``rd_agent_factors`` 每因子只有一行，``update_factor_metrics``
直接覆盖指标列——每跑一次回测，上一次结果就被抹掉。新表
``rd_agent_factor_backtests`` 记录每次运行。

本文件钉死的边：
1. **一次运行一行**：start 插 running 行，finish **按 run_id 精确收口自己那次
   运行**——取消→立即重跑后，旧任务延迟收尾不得收错新任务的行（真结果永久
   丢失）；已完结行不可被二次收口改写（取消端点与后台任务先后收口天然幂等）；
2. **崩溃对账**：陈旧 running 行（引擎崩溃 / 超时）由 recover 收口为
   failed/timeout_or_engine_crash，与 ``recover_stuck_factors`` 同口径；
3. **端点契约**：``GET /factors/{id}/backtests`` 走归属校验、透传 limit、
   越界 422；cancel 端点按注册表 run_id 收口、**不拆去重标记**（标记归任务
   finally 以身份守卫独占清理，否则旧任务会拆掉新任务的标记）；
4. **接线**：``_run_factor_backtest`` 失败/取消两条路径都写台账；台账写失败
   绝不拖挂回测（增益层纪律，照 pool_service 先例）；两个完成点都接线
   （源码邻接守卫，照 test_alpha_agent_quality_gate 先例）。

真库测试的纪律（照 ``test_pool_service`` 先例）：一次性 factor_id
（``t-hist-<rand>``），finally 连根清；每个异步测试 finally
``await close_database()``（连接绑定创建它的 loop）。
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text

try:  # pragma: no cover - 环境相关
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from backend.services.engine.qlib_app.services.rd_agent_persistence import (
        RDAgentFactorPersistence,
    )
    from backend.services.engine.routers import alpha_agent as aa
except Exception as _exc:  # noqa: BLE001
    aa = None
    _IMPORT_ERR = _exc

pytestmark = pytest.mark.skipif(aa is None, reason="依赖不可用（需容器环境）")

ROOT = Path(__file__).resolve().parents[2]
ALPHA_AGENT_PY = ROOT / "backend/services/engine/routers/alpha_agent.py"
TABLE = "rd_agent_factor_backtests"
_PREFIX = "/api/v1/alpha-agent"


def _fresh_id() -> str:
    return f"t-hist-{uuid4().hex[:10]}"


async def _skip_if_no_db() -> None:
    from backend.shared.database_manager_v2 import get_session

    try:
        async with get_session(read_only=True) as probe:
            await probe.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"DB 不可用: {exc}")


async def _cleanup(factor_ids: list[str]) -> None:
    from backend.shared.database_manager_v2 import close_database, get_session

    async with get_session() as session:
        await session.execute(
            text(f"DELETE FROM {TABLE} WHERE factor_id = ANY(:ids)"),
            {"ids": factor_ids},
        )
    await close_database()


async def _backdate_run(run_id: str, minutes: int) -> None:
    """把一行 created_at 拨老，模拟陈旧未收口运行。"""
    from backend.shared.database_manager_v2 import get_session

    async with get_session() as session:
        await session.execute(
            text(
                f"UPDATE {TABLE} SET created_at = now() - (:m || ' minutes')::interval "
                "WHERE run_id = :run_id"
            ),
            {"m": str(minutes), "run_id": run_id},
        )


# ========================== 真库：持久化语义 ==========================


@pytest.mark.asyncio
async def test_start_finish_list_roundtrip() -> None:
    """start→running 行；finish→终态 + 全指标；list 新→旧且 metadata 解包。"""
    await _skip_if_no_db()
    fid = _fresh_id()
    p = RDAgentFactorPersistence()
    try:
        await p.ensure_tables()  # 新表 DDL 幂等（升级部署同路径）

        run_id = await p.start_backtest_run(
            fid,
            factor_name="测试因子",
            user_id=fid,
            market="a_share",
            universe="csi300",
            data_source="qlib_bin",
        )
        assert run_id

        runs = await p.list_backtest_runs(fid)
        assert len(runs) == 1
        assert runs[0]["run_id"] == run_id
        assert runs[0]["status"] == "running"
        assert runs[0]["finished_at"] is None
        assert runs[0]["metadata"] == {}  # 未收口时无指标

        ok = await p.finish_backtest_run(
            run_id,
            "completed",
            ic_value=0.031,
            rank_ic=0.042,
            icir=0.51,
            rank_icir=0.62,
            sharpe_ratio=1.23,
            annual_return=0.11,
            max_drawdown=0.22,
            universe="csi300",
            data_source="qlib_bin",
            date_range="2024-01-01~2024-12-31",
            metrics={
                "data_source": "qlib_bin",
                "icir": 0.51,
                "n_obs": 240,
                "quality": {"pfs": 0.95},
            },
        )
        assert ok is True

        runs = await p.list_backtest_runs(fid)
        r = runs[0]
        assert r["status"] == "completed"
        assert r["ic_value"] == pytest.approx(0.031)
        assert r["rank_ic"] == pytest.approx(0.042)
        assert r["icir"] == pytest.approx(0.51)
        assert r["rank_icir"] == pytest.approx(0.62)
        assert r["sharpe_ratio"] == pytest.approx(1.23)
        assert r["annual_return"] == pytest.approx(0.11)
        assert r["max_drawdown"] == pytest.approx(0.22)
        assert r["date_range"] == "2024-01-01~2024-12-31"
        assert r["metadata"]["n_obs"] == 240
        assert r["metadata"]["quality"]["pfs"] == pytest.approx(0.95)
        assert r["error"] is None
        assert r["finished_at"] is not None
    finally:
        await _cleanup([fid])


@pytest.mark.asyncio
async def test_finish_is_idempotent_per_run() -> None:
    """同一 run 重复收口幂等（第二次 False）；已完结行不再被改写。"""
    await _skip_if_no_db()
    fid = _fresh_id()
    p = RDAgentFactorPersistence()
    try:
        # 第一轮：已完成
        r1 = await p.start_backtest_run(fid, user_id=fid)
        assert await p.finish_backtest_run(r1, "completed", ic_value=0.01) is True
        # 二次收口同一 run（取消端点晚于后台任务收口）：行已非 running → 幂等 False
        assert (
            await p.finish_backtest_run(r1, "cancelled", error="cancelled_by_user")
            is False
        )

        runs = await p.list_backtest_runs(fid)
        assert len(runs) == 1
        assert runs[0]["status"] == "completed"
        assert runs[0]["error"] is None

        # 第二轮：新 run 独立收口，历史行原样保留
        r2 = await p.start_backtest_run(fid, user_id=fid)
        assert await p.finish_backtest_run(r2, "failed", error="boom") is True

        runs = await p.list_backtest_runs(fid)
        assert len(runs) == 2
        assert runs[0]["run_id"] == r2  # 新→旧
        assert runs[0]["status"] == "failed"
        assert runs[0]["error"] == "boom"
        assert runs[1]["run_id"] == r1
        assert runs[1]["status"] == "completed"
    finally:
        await _cleanup([fid])


@pytest.mark.asyncio
async def test_finish_targets_its_own_run_when_multiple_open() -> None:
    """同因子两行未完结（取消→立即重跑的异常路径）时，finish(old_run) 只收旧行。

    这是 HIGH-1 的回归钉：旧实现在同场景收口「最新未完结行」，会把**新任务**
    的行收成 cancelled——新任务随后跑出的真结果无处落账、永久丢失。
    """
    await _skip_if_no_db()
    fid = _fresh_id()
    p = RDAgentFactorPersistence()
    try:
        old_run = await p.start_backtest_run(fid, user_id=fid)
        new_run = await p.start_backtest_run(fid, user_id=fid)
        assert old_run != new_run

        # 旧任务延迟收尾（取消路径）：只关自己的行
        assert (
            await p.finish_backtest_run(old_run, "cancelled", error="cancelled_by_user")
            is True
        )
        runs = {r["run_id"]: r for r in await p.list_backtest_runs(fid)}
        assert runs[old_run]["status"] == "cancelled"
        assert runs[new_run]["status"] == "running"  # 新行原样，绝不误收
        assert runs[new_run]["finished_at"] is None

        # 新任务随后正常收口，真结果落进自己的行
        assert await p.finish_backtest_run(new_run, "completed", ic_value=0.02) is True
        runs = {r["run_id"]: r for r in await p.list_backtest_runs(fid)}
        assert runs[new_run]["status"] == "completed"
        assert runs[new_run]["ic_value"] == pytest.approx(0.02)
    finally:
        await _cleanup([fid])


@pytest.mark.asyncio
async def test_finish_rejects_unknown_status() -> None:
    """非终态/未知状态直接 ValueError——不落 SQL（status 词表由代码+CHECK 双闸）。"""
    p = RDAgentFactorPersistence()
    for bad in ("running", "timeout", ""):
        with pytest.raises(ValueError):
            await p.finish_backtest_run("run-x", bad)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_recover_stuck_backtest_runs() -> None:
    """陈旧未收口行收口为 failed/timeout_or_engine_crash；新鲜行不动。"""
    await _skip_if_no_db()
    fid = _fresh_id()
    p = RDAgentFactorPersistence()
    try:
        stale = await p.start_backtest_run(fid, user_id=fid)
        fresh = await p.start_backtest_run(fid, user_id=fid)
        await _backdate_run(stale, minutes=40)

        count = await p.recover_stuck_backtest_runs(max_age_min=15)
        assert count >= 1

        runs = {r["run_id"]: r for r in await p.list_backtest_runs(fid)}
        assert runs[stale]["status"] == "failed"
        assert runs[stale]["error"] == "timeout_or_engine_crash"
        assert runs[stale]["finished_at"] is not None
        assert runs[fresh]["status"] == "running"
        assert runs[fresh]["finished_at"] is None
    finally:
        await _cleanup([fid])


# ========================== 端点契约（TestClient mini-app） ==========================


@pytest.fixture()
def client():
    app = FastAPI()

    @app.middleware("http")
    async def _inject_identity(request, call_next):
        request.state.user = {"user_id": "u1", "tenant_id": "default"}
        return await call_next(request)

    app.include_router(aa.router)
    return TestClient(app)


def _stub_persistence(monkeypatch, *, owner="u1", runs=None, raises=None):
    calls: list[dict] = []

    async def _get_factor(factor_id):
        return {"factor_id": factor_id, "user_id": owner, "status": "completed"}

    async def _list_backtest_runs(factor_id, limit=20):
        calls.append({"factor_id": factor_id, "limit": limit})
        if raises is not None:
            raise raises
        return list(runs or [])

    monkeypatch.setattr(
        aa,
        "persistence",
        SimpleNamespace(get_factor=_get_factor, list_backtest_runs=_list_backtest_runs),
    )
    return calls


@pytest.mark.unit
def test_list_backtests_passthrough(client, monkeypatch):
    runs = [
        {"run_id": "r2", "factor_id": "f1", "status": "completed", "ic_value": 0.02},
        {"run_id": "r1", "factor_id": "f1", "status": "failed", "error": "boom"},
    ]
    calls = _stub_persistence(monkeypatch, runs=runs)

    r = client.get(f"{_PREFIX}/factors/f1/backtests", params={"limit": 50})
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == 200
    assert body["data"]["factor_id"] == "f1"
    assert [x["run_id"] for x in body["data"]["runs"]] == ["r2", "r1"]
    assert calls[-1] == {"factor_id": "f1", "limit": 50}


@pytest.mark.unit
def test_list_backtests_limit_bounds(client, monkeypatch):
    _stub_persistence(monkeypatch)
    # 越界在路由层 422，不落进 SQL（与 /factors 的 le 纪律一致）
    assert client.get(f"{_PREFIX}/factors/f1/backtests?limit=0").status_code == 422
    assert client.get(f"{_PREFIX}/factors/f1/backtests?limit=101").status_code == 422


@pytest.mark.unit
def test_list_backtests_rejects_other_owner(client, monkeypatch):
    _stub_persistence(monkeypatch, owner="someone-else")
    r = client.get(f"{_PREFIX}/factors/f1/backtests")
    assert r.status_code == 404  # 跨用户不泄露存在性


# ========================== 接线：终态写台账，且不拖挂回测 ==========================


class _RecordingPersistence:
    def __init__(self):
        self.finishes: list[dict] = []
        self.starts: list[dict] = []
        self.factor_updates: list[dict] = []
        self.raise_on_finish = False
        self.raise_on_start = False

    async def update_factor_metrics(self, factor_id, **kwargs):
        self.factor_updates.append({"factor_id": factor_id, **kwargs})
        return None

    async def start_backtest_run(self, factor_id, **kwargs):
        if self.raise_on_start:
            raise RuntimeError("台账登记炸了")
        self.starts.append({"factor_id": factor_id, **kwargs})
        return "run-1"

    async def finish_backtest_run(self, run_id, status, **kwargs):
        if self.raise_on_finish:
            raise RuntimeError("台账收口炸了")
        self.finishes.append({"run_id": run_id, "status": status, **kwargs})
        return True


def _install_recorder(monkeypatch):
    rec = _RecordingPersistence()
    monkeypatch.setattr(aa, "persistence", rec)
    return rec


def _valid_factor_code() -> str:
    return "class DummyFactor:\n    def __init__(self):\n        pass\n"


@pytest.mark.asyncio
async def test_run_factor_backtest_failure_records_history(monkeypatch):
    rec = _install_recorder(monkeypatch)
    fid = _fresh_id()

    async def _boom(*args, **kwargs):
        raise RuntimeError("因子炸了")

    monkeypatch.setattr(aa, "_backtest_via_qlib", _boom)
    try:
        await aa._run_factor_backtest(
            fid,
            _valid_factor_code(),
            market="a_share",
            data_source="qlib_bin",
            start_date="2024-01-01",
            end_date="2024-12-31",
            run_id="r-1",
        )
        assert len(rec.finishes) == 1
        fin = rec.finishes[0]
        assert fin["run_id"] == "r-1"  # 收口的是本次运行的行身份
        assert fin["status"] == "failed"
        assert "因子炸了" in fin["error"]
        assert fin["date_range"] == "2024-01-01~2024-12-31"  # 失败也记有效窗口
        assert fid not in aa._running_backtests
    finally:
        aa._running_backtests.discard(fid)


@pytest.mark.asyncio
async def test_run_factor_backtest_cancel_records_history(monkeypatch):
    rec = _install_recorder(monkeypatch)
    fid = _fresh_id()

    async def _cancelled(*args, **kwargs):
        raise aa.FactorBacktestCancelled("回测已被用户取消")

    monkeypatch.setattr(aa, "_backtest_via_qlib", _cancelled)
    try:
        await aa._run_factor_backtest(
            fid,
            _valid_factor_code(),
            market="a_share",
            data_source="qlib_bin",
            start_date="2024-01-01",
            end_date="2024-12-31",
            run_id="r-1",
        )
        assert len(rec.finishes) == 1
        assert rec.finishes[0]["run_id"] == "r-1"
        assert rec.finishes[0]["status"] == "cancelled"
        assert rec.finishes[0]["error"] == "cancelled_by_user"
    finally:
        aa._running_backtests.discard(fid)


@pytest.mark.asyncio
async def test_stale_task_cleanup_keeps_new_generation(monkeypatch):
    """取消→立即重跑：旧任务延迟收尾不得清掉新任务的登记（HIGH-1 内存侧）。

    旧任务（run_id=r-1）收尾时，注册表已被新任务（r-2）覆盖——去重标记、
    取消标记、注册表条目都必须原样保留，否则新任务失去防重/取消保护。
    """
    rec = _install_recorder(monkeypatch)
    fid = _fresh_id()
    aa._running_backtest_runs[fid] = "r-2"  # 新任务已登记
    aa._running_backtests.add(fid)
    aa._backtest_cancelled.add(fid)

    async def _boom(*args, **kwargs):
        raise RuntimeError("旧任务炸了")

    monkeypatch.setattr(aa, "_backtest_via_qlib", _boom)
    try:
        await aa._run_factor_backtest(
            fid,
            _valid_factor_code(),
            market="a_share",
            data_source="qlib_bin",
            start_date="2024-01-01",
            end_date="2024-12-31",
            run_id="r-1",
        )
        # 旧任务仍收口自己的行（r-1），但这不动新任务的内存登记
        assert rec.finishes[0]["run_id"] == "r-1"
        assert aa._running_backtest_runs.get(fid) == "r-2"
        assert fid in aa._running_backtests
        assert fid in aa._backtest_cancelled
    finally:
        aa._running_backtest_runs.pop(fid, None)
        aa._running_backtests.discard(fid)
        aa._backtest_cancelled.discard(fid)


@pytest.mark.asyncio
async def test_history_write_failure_never_breaks_backtest(monkeypatch):
    """台账是增益层：登记/收口炸了，回测主链路照常收尾（照 pool_service 纪律）。"""
    rec = _install_recorder(monkeypatch)
    rec.raise_on_finish = True
    rec.raise_on_start = True
    fid = _fresh_id()

    async def _boom(*args, **kwargs):
        raise RuntimeError("因子炸了")

    monkeypatch.setattr(aa, "_backtest_via_qlib", _boom)
    try:
        # 不抛异常即为通过（failed 路径的 update_factor_metrics 走 stub）
        await aa._run_factor_backtest(
            fid,
            _valid_factor_code(),
            market="a_share",
            data_source="qlib_bin",
            start_date="2024-01-01",
            end_date="2024-12-31",
            run_id="r-1",
        )
        assert fid not in aa._running_backtests
        # 台账炸了，因子行终态照写（失败原因可见），主链路不受台账影响
        terminal = [u for u in rec.factor_updates if u.get("status") == "failed"]
        assert len(terminal) == 1
        assert "因子炸了" in terminal[0]["metadata"]["backtest_error"]
    finally:
        aa._running_backtests.discard(fid)


@pytest.mark.asyncio
async def test_backtest_endpoint_records_start(monkeypatch):
    """发起回测即登记 running 行（配置原样带上：市场/池/数据源）。"""
    rec = _install_recorder(monkeypatch)

    async def _get_factor(factor_id):
        return {
            "factor_id": factor_id,
            "user_id": "u1",
            "factor_name": "动量因子",
            "market": "a_share",
            "factor_code": _valid_factor_code(),
            "status": "pending",
        }

    monkeypatch.setattr(rec, "get_factor", _get_factor, raising=False)
    dispatched: list[tuple] = []

    async def _noop_run(*args, **kwargs):
        dispatched.append((args, kwargs))

    monkeypatch.setattr(aa, "_run_factor_backtest", _noop_run)

    app = FastAPI()

    @app.middleware("http")
    async def _inject_identity(request, call_next):
        request.state.user = {"user_id": "u1", "tenant_id": "default"}
        return await call_next(request)

    app.include_router(aa.router)
    client = TestClient(app)
    fid = _fresh_id()
    try:
        r = client.post(
            f"{_PREFIX}/factors/{fid}/backtest?universe=csi500&data_source=qlib_bin"
        )
        assert r.status_code == 200, r.text
        assert len(rec.starts) == 1
        start = rec.starts[0]
        assert start["factor_id"] == fid
        assert start["factor_name"] == "动量因子"
        assert start["user_id"] == "u1"
        assert start["market"] == "a_share"
        assert start["universe"] == "csi500"
        assert start["data_source"] == "qlib_bin"
        # 登记返回的 run_id 进注册表，供终态/取消按行身份收口
        assert aa._running_backtest_runs.get(fid) == "run-1"
    finally:
        aa._running_backtest_runs.pop(fid, None)
        aa._running_backtests.discard(fid)


@pytest.mark.unit
def test_cancel_endpoint_finalizes_registry_run(monkeypatch):
    """cancel 按注册表 run_id 收口，且**不拆去重标记**（标记归任务 finally 清理）。

    旧行为里 cancel 立刻 discard 标记，紧接的重跑能放行、随后旧任务 finally
    会把新任务的标记一并拆掉（可并发双跑同因子）。
    """
    fid = _fresh_id()
    finishes: list[dict] = []

    async def _get_factor(factor_id):
        return {"factor_id": factor_id, "user_id": "u1", "status": "backtesting"}

    async def _update_factor_metrics(factor_id, **kwargs):
        return None

    async def _finish_backtest_run(run_id, status, **kwargs):
        finishes.append({"run_id": run_id, "status": status, **kwargs})
        return True

    monkeypatch.setattr(
        aa,
        "persistence",
        SimpleNamespace(
            get_factor=_get_factor,
            update_factor_metrics=_update_factor_metrics,
            finish_backtest_run=_finish_backtest_run,
        ),
    )
    aa._running_backtests.add(fid)
    aa._running_backtest_runs[fid] = "run-77"

    app = FastAPI()

    @app.middleware("http")
    async def _inject_identity(request, call_next):
        request.state.user = {"user_id": "u1", "tenant_id": "default"}
        return await call_next(request)

    app.include_router(aa.router)
    client = TestClient(app)
    try:
        r = client.post(f"{_PREFIX}/factors/{fid}/cancel")
        assert r.status_code == 200, r.text
        assert finishes == [
            {"run_id": "run-77", "status": "cancelled", "error": "cancelled_by_user"}
        ]
        # 标记与注册表条目留给任务 finally——cancel 不越权拆
        assert fid in aa._running_backtests
        assert aa._running_backtest_runs.get(fid) == "run-77"
    finally:
        aa._running_backtest_runs.pop(fid, None)
        aa._running_backtests.discard(fid)
        aa._backtest_cancelled.discard(fid)


# ========================== 完成点接线（源码邻接守卫，照 quality_gate 先例） ==========================


@pytest.mark.unit
def test_both_completion_sites_record_history() -> None:
    """qlib 与 h5 两条完成路径都必须写历史台账。

    调用点被 ruff 折行为多行，按空白容忍匹配
    `_record_backtest_finish(run_id, "completed"` 形状——收口一律按 run_id。
    """
    src = ALPHA_AGENT_PY.read_text(encoding="utf-8")
    completed = re.findall(r'_record_backtest_finish\(\s*run_id,\s*"completed"', src)
    assert len(completed) == 2, "qlib/h5 两个完成点都必须接线"
    assert len(re.findall(r'_record_backtest_finish\(\s*run_id,\s*"failed"', src)) >= 2
    assert (
        len(re.findall(r'_record_backtest_finish\(\s*run_id,\s*"cancelled"', src)) >= 2
    )
    # 发起端登记 running 行
    assert len(re.findall(r"_record_backtest_start\(", src)) >= 2  # 定义 + 调用
    # 无接线终态路径的死代码不得回流（旧 _run_lightweight_backtest 曾直接翻
    # 因子行终态却不写台账——删于 2026-10-09）
    assert "_run_lightweight_backtest" not in src
    # 按 run_id 收口的身份注册表必须存在（finish 不再按 factor「找最新」）
    assert "_running_backtest_runs" in src


@pytest.mark.unit
def test_startup_recovery_covers_backtest_runs() -> None:
    """启动恢复钩子同时收口因子行与历史台账（引擎崩溃后两边都不留 running）。"""
    src = ALPHA_AGENT_PY.read_text(encoding="utf-8")
    assert "recover_stuck_backtest_runs(" in src
    # 恢复前先建表（与 lifespan 建表并发时，缺表会让本次恢复静默落空）
    recover_idx = src.index("recover_stuck_backtest_runs(")
    assert src.index("await persistence.ensure_tables()") < recover_idx
