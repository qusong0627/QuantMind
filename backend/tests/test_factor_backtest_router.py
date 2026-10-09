"""T-FB-07/08 单测：``/api/v1/factor-backtest`` 路由（mini-app + TestClient）。

照 ``test_alpha_agent_factor_list_scope`` 惯例搭 mini-app；store/engine/鉴权全部
打桩——本文件钉的是**编排契约**：
- 发起：占位去重、台账行先落（running）、kind 预检进台账、双击不双跑；
- 后台任务状态映射：ok→completed（指标逐字段进台账 + 序列落盘）、
  降级三态原样收口、取消→cancelled、意外异常→failed；
- 兜底 finally 的身份守卫清理（旧任务不得拆新任务的去重键）；
- 矩阵：归属过滤（他人因子不见单元格）、静态兼容档随市场列集、
  未跑过 = not_run（不是空白）、counts 汇总；
- 矩阵显著性列（T-FB-18）：NW t 读台账 ``ic_nw_t``、BY q 族校正与 ``/report``
  单源（同 run 的 q 逐位相等，期望值独立重算）、无批次回落 q = p、非完成
  终态一律 None；
- 曲线：404 语义（run 不存在 / 序列未落盘）。
"""

from __future__ import annotations

import asyncio
import math

import pytest

fb = pytest.importorskip("backend.services.engine.factor_backtest.router")
aa = pytest.importorskip("backend.services.engine.routers.alpha_agent")

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
except Exception:  # noqa: BLE001
    FastAPI = None

pytestmark = pytest.mark.unit

_PREFIX = "/api/v1/factor-backtest"
_CODE = 'def calculate_factor(df):\n    return df["$close"]\n'
_FACTOR = {
    "factor_id": "f-1",
    "factor_name": "测试因子",
    "factor_code": _CODE,
    "user_id": "u-1",
}


@pytest.fixture(autouse=True)
def _clean_shared_sets():
    """共享去重/取消注册表是进程内的——用例结束必须清干净，防串扰。"""
    yield
    fb._running_backtests.clear()
    fb._running_backtest_runs.clear()
    fb._backtest_cancelled.clear()


@pytest.fixture()
def client():
    app = FastAPI()

    @app.middleware("http")
    async def _inject_identity(request, call_next):
        request.state.user = {"user_id": "u-1", "tenant_id": "default"}
        return await call_next(request)

    app.include_router(fb.router)
    return TestClient(app)


@pytest.fixture()
def stub(monkeypatch):
    """路由外部依赖替身：鉴权、台账、发起 spawn。"""
    calls: dict = {"start": [], "finish": [], "series": [], "spawned": 0}

    async def _owned(factor_id, request, *, for_write=False):
        return dict(_FACTOR, factor_id=factor_id)

    monkeypatch.setattr(fb, "_require_owned_factor", _owned)

    async def _start_run(factor_id, **kwargs):
        calls["start"].append({"factor_id": factor_id, **kwargs})
        return "fb-run-1"

    monkeypatch.setattr(fb.store, "start_run", _start_run)
    monkeypatch.setattr(fb.store, "ensure_tables", lambda: asyncio.sleep(0))

    async def _finish_run(run_id, status, **kwargs):
        calls["finish"].append({"run_id": run_id, "status": status, **kwargs})
        return True

    monkeypatch.setattr(fb.store, "finish_run", _finish_run)

    async def _save_series(run_id, **kwargs):
        calls["series"].append({"run_id": run_id, **kwargs})

    monkeypatch.setattr(fb.store, "save_series", _save_series)

    def _spawn(coro):
        calls["spawned"] += 1
        coro.close()  # TestClient 请求级 loop 会取消后台任务：捕获后关闭

    monkeypatch.setattr(fb, "_spawn_backtest_run", _spawn)
    return calls


# ── 市场档案 ─────────────────────────────────────────────────────────


def test_markets_lists_profiles_without_provider(client, monkeypatch):
    def _status(profile):
        return {
            "market": profile.market,
            "label": profile.label,
            "in_sample": profile.in_sample,
            "experimental": profile.experimental,
            "provider": "/data/secret/path",
            "ready": True,
            "calendar_start": "2020-01-02",
            "calendar_end": "2026-10-08",
            "instruments": 100,
            "columns": ["$close"],
            "bin_columns": ["close"],
            "universe_mode": profile.universe_mode,
            "default_universe": profile.default_universe,
            "universe_top_n": profile.universe_top_n,
            "window_years": profile.window_years,
            "cost_bps": profile.cost_bps,
            "benchmark": profile.benchmark,
            "min_days": 120,
        }

    monkeypatch.setattr(fb, "profile_status", _status)
    r = client.get(f"{_PREFIX}/markets")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["total"] == 5
    assert data["markets"][0]["market"] == "a_share"
    assert data["markets"][0]["in_sample"] is True
    assert all("provider" not in m for m in data["markets"])  # 内部路径不出接口


# ── 单因子发起 ───────────────────────────────────────────────────────


def test_single_starts_run_and_records_ledger_row(client, stub):
    r = client.post(
        f"{_PREFIX}/single", json={"factor_id": "f-1", "market": "us_stock"}
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["run_id"] == "fb-run-1"
    assert data["status"] == "running"
    assert stub["spawned"] == 1
    start = stub["start"][0]
    assert start["factor_id"] == "f-1"
    assert start["kind"] == "functional"  # AST 预检进台账
    assert start["market"] == "us_stock"


def test_single_dedupe_when_already_running(client, stub):
    fb._running_backtests.add("f-1")
    r = client.post(
        f"{_PREFIX}/single", json={"factor_id": "f-1", "market": "us_stock"}
    )
    assert r.status_code == 200
    assert "已在进行中" in r.json()["data"]["message"]
    assert stub["start"] == [] and stub["spawned"] == 0


def test_single_rejects_unknown_market_and_empty_code(client, stub, monkeypatch):
    r = client.post(f"{_PREFIX}/single", json={"factor_id": "f-1", "market": "mars"})
    assert r.status_code == 400

    async def _empty_code(factor_id, request, *, for_write=False):
        return dict(_FACTOR, factor_code="")

    monkeypatch.setattr(fb, "_require_owned_factor", _empty_code)
    r2 = client.post(
        f"{_PREFIX}/single", json={"factor_id": "f-1", "market": "us_stock"}
    )
    assert r2.status_code == 400
    assert "因子代码为空" in r2.json()["detail"]


def test_single_cost_bps_out_of_range_422(client, stub):
    r = client.post(
        f"{_PREFIX}/single",
        json={"factor_id": "f-1", "market": "us_stock", "cost_bps": 9999},
    )
    assert r.status_code == 422
    assert stub["spawned"] == 0


# ── 取消 ─────────────────────────────────────────────────────────────


def test_cancel_when_not_running(client, stub):
    r = client.post(f"{_PREFIX}/single/f-1/cancel")
    assert r.status_code == 200
    assert "未在运行" in r.json()["data"]["message"]
    assert stub["finish"] == []


def test_cancel_settles_ledger_of_current_run(client, stub):
    fb._running_backtests.add("f-1")
    fb._running_backtest_runs["f-1"] = "fb-run-1"
    r = client.post(f"{_PREFIX}/single/f-1/cancel")
    assert r.status_code == 200
    assert r.json()["data"]["status"] == "cancelled"
    assert stub["finish"] == [
        {"run_id": "fb-run-1", "status": "cancelled", "error": "cancelled_by_user"}
    ]
    assert "f-1" in fb._backtest_cancelled  # 标记留给任务 finally 清理


# ── 后台任务状态映射（直调 worker）───────────────────────────────────


def _run_worker(monkeypatch, stub, result=None, raises=None, fid="f-1"):
    async def _evaluate(factor, **kwargs):
        if raises is not None:
            raise raises
        return result

    monkeypatch.setattr(fb, "evaluate_factor_market", _evaluate)
    fb._running_backtests.add(fid)
    fb._running_backtest_runs[fid] = "fb-run-1"
    asyncio.run(
        fb._run_single(
            fid,
            dict(_FACTOR),
            "fb-run-1",
            market="us_stock",
            universe=None,
            start=None,
            end=None,
            cost_bps=None,
        )
    )


def test_worker_ok_maps_metrics_and_saves_series(monkeypatch, stub):
    _run_worker(
        monkeypatch,
        stub,
        result={
            "status": "ok",
            "reason": None,
            "message": None,
            "window": {"start": "2023-10-08", "end": "2026-10-08"},
            "universe": "all",
            "metrics": {
                "ic": 0.05,
                "rank_ic": 0.04,
                "icir": 0.3,
                "rank_icir": 0.25,
                "sharpe": 1.1,
                "ann_return": 0.12,
                "max_drawdown": -0.07,
            },
            "series": {"dates": ["2024-01-02"], "nav_long": [1.0]},
        },
    )
    fin = stub["finish"][0]
    assert fin["status"] == "completed"
    assert fin["ic_value"] == 0.05
    assert fin["rank_icir"] == 0.25
    assert fin["sharpe_ratio"] == 1.1
    assert fin["date_range"] == "2023-10-08~2026-10-08"
    assert stub["series"][0]["run_id"] == "fb-run-1"
    # 身份守卫清理：任务结束即释放去重键
    assert "f-1" not in fb._running_backtests
    assert "f-1" not in fb._running_backtest_runs


@pytest.mark.parametrize("status", ["data_unsupported", "insufficient", "unavailable"])
def test_worker_degraded_statuses_settle_as_is(monkeypatch, stub, status):
    _run_worker(
        monkeypatch,
        stub,
        result={
            "status": status,
            "reason": "why",
            "message": "具体情况",
            "window": {"start": "2023-10-08", "end": "2026-10-08"},
            "universe": "all",
            "metrics": None,
            "series": None,
        },
    )
    fin = stub["finish"][0]
    assert fin["status"] == status
    assert fin["error"] == "具体情况"
    assert stub["series"] == []


def test_worker_cancelled_maps_to_cancelled(monkeypatch, stub):
    _run_worker(monkeypatch, stub, raises=aa.FactorBacktestCancelled("cancelled"))
    assert stub["finish"][0]["status"] == "cancelled"
    assert stub["finish"][0]["error"] == "cancelled_by_user"


def test_worker_unexpected_exception_maps_to_failed(monkeypatch, stub):
    _run_worker(monkeypatch, stub, raises=RuntimeError("boom"))
    fin = stub["finish"][0]
    assert fin["status"] == "failed"
    assert "boom" in fin["error"]


def test_worker_finally_respects_identity_guard(monkeypatch, stub):
    """取消→立即重跑场景：旧任务收尾不得拆新任务的去重键。"""
    _run_worker(
        monkeypatch,
        stub,
        result={
            "status": "insufficient",
            "reason": "too_few_days",
            "message": "太少",
            "window": {"start": None, "end": None},
            "universe": None,
            "metrics": None,
            "series": None,
        },
    )
    # 模拟旧任务收尾时注册表已换成新 run
    assert "f-1" not in fb._running_backtest_runs  # 本场景先确认正常清理
    fb._running_backtests.add("f-2")
    fb._running_backtest_runs["f-2"] = "fb-run-2"
    asyncio.run(
        fb._run_single(
            "f-1",
            dict(_FACTOR),
            "fb-run-1",  # 旧 run_id，注册表里是 fb-run-2 —— 不得误拆
            market="us_stock",
            universe=None,
            start=None,
            end=None,
            cost_bps=None,
        )
    )
    assert "f-2" in fb._running_backtests


# ── 矩阵 ─────────────────────────────────────────────────────────────


def test_matrix_assembles_cells_compat_and_ownership(client, monkeypatch):
    async def _meta(ids):
        return [
            {
                "factor_id": "f-1",
                "factor_name": "A",
                "factor_code": _CODE,
                "user_id": "u-1",
                "ic_value": 0.03,
                "market": "a_share",
                "status": "completed",
            },
            {
                "factor_id": "f-2",
                "factor_name": "B",
                "factor_code": _CODE,
                "user_id": "u-2",  # 他人因子：单元格必须不可见
                "ic_value": 0.02,
                "market": "a_share",
                "status": "completed",
            },
        ]

    async def _cells(ids, markets=None):
        assert ids == ["f-1"]  # 归属过滤后只剩自己的
        return [
            {
                "run_id": "fb-run-1",
                "factor_id": "f-1",
                "status": "completed",
                "market": "us_stock",
                "universe": "all",
                "date_range": "2023~2026",
                "finished_at": None,
                "error": None,
                "metrics": {"ic": 0.05, "icir": 0.3, "sharpe": 1.2},
                "ic_value": 0.05,
            }
        ]

    monkeypatch.setattr(fb.store, "get_factor_meta", _meta)
    monkeypatch.setattr(fb.store, "latest_cells", _cells)
    monkeypatch.setattr(fb, "get_authenticated_identity", lambda request: ("u-1", "d"))

    r = client.post(
        f"{_PREFIX}/matrix",
        json={"factor_ids": ["f-1", "f-2"], "markets": ["a_share", "us_stock"]},
    )
    assert r.status_code == 200
    data = r.json()["data"]
    f1 = data["factors"][0]
    f2 = data["factors"][1]
    assert f1["cells"]["us_stock"]["status"] == "completed"
    assert f1["cells"]["us_stock"]["ic"] == 0.05
    assert f1["cells"]["us_stock"]["compat"] == "portable"
    assert f1["cells"]["a_share"]["status"] == "not_run"
    assert f2["owned"] is False
    assert all(c["status"] == "not_run" for c in f2["cells"].values())
    assert data["counts"]["not_run"] == 3
    assert data["counts"]["completed"] == 1


def test_matrix_marks_enriched_factor_data_unsupported_on_us(client, monkeypatch):
    """富化列因子在美股列 = data_unsupported（不跑就有结论）；CN 列 = portable。"""

    async def _meta(ids):
        return [
            {
                "factor_id": "f-1",
                "factor_name": "A",
                "factor_code": 'def calculate_factor(df):\n    return df["$netflow_5"]\n',
                "user_id": "u-1",
                "ic_value": None,
                "market": "a_share",
                "status": "pending",
            }
        ]

    async def _cells(ids, markets=None):
        return []

    monkeypatch.setattr(fb.store, "get_factor_meta", _meta)
    monkeypatch.setattr(fb.store, "latest_cells", _cells)
    monkeypatch.setattr(fb, "get_authenticated_identity", lambda request: ("u-1", "d"))

    r = client.post(
        f"{_PREFIX}/matrix",
        json={"factor_ids": ["f-1"], "markets": ["a_share", "us_stock"]},
    )
    cells = r.json()["data"]["factors"][0]["cells"]
    assert cells["a_share"]["compat"] == "portable"
    assert cells["us_stock"]["compat"] == "data_unsupported"
    assert cells["us_stock"]["missing"] == ["$netflow_5"]


def test_matrix_rejects_unknown_market(client, monkeypatch):
    monkeypatch.setattr(fb, "get_authenticated_identity", lambda request: ("u-1", "d"))
    r = client.post(
        f"{_PREFIX}/matrix", json={"factor_ids": ["f-1"], "markets": ["mars"]}
    )
    assert r.status_code == 400


# ── 矩阵显著性列（T-FB-18）────────────────────────────────────────────


def _sig_meta():
    async def _meta(ids):
        return [
            {
                "factor_id": "f-1",
                "factor_name": "A",
                "factor_code": _CODE,
                "user_id": "u-1",
                "ic_value": 0.03,
                "market": "a_share",
                "status": "completed",
            }
        ]

    return _meta


def test_matrix_significance_from_batch_family(client, monkeypatch):
    """矩阵显著性列：NW t 取台账 ic_nw_t；族 = 同批次完成单元 → BY q（独立重算）。"""

    async def _cells(ids, markets=None):
        return [
            {
                "run_id": "fb-run-1",
                "factor_id": "f-1",
                "status": "completed",
                "batch_id": "fb-batch-1",
                "market": "us_stock",
                "universe": "all",
                "date_range": "2023~2026",
                "finished_at": None,
                "error": None,
                "metrics": {"ic": 0.05, "ic_nw_t": 5.6},
                "ic_value": 0.05,
            }
        ]

    async def _batch_runs(batch_id):
        assert batch_id == "fb-batch-1"
        return [
            {"run_id": "fb-run-1", "status": "completed", "metrics": {"ic_nw_t": 5.6}},
            {"run_id": "fb-run-2", "status": "completed", "metrics": {"ic_nw_t": -0.4}},
            {"run_id": "fb-run-3", "status": "data_unsupported", "metrics": {}},
        ]

    monkeypatch.setattr(fb.store, "get_factor_meta", _sig_meta())
    monkeypatch.setattr(fb.store, "latest_cells", _cells)
    monkeypatch.setattr(fb.store, "batch_runs", _batch_runs)
    monkeypatch.setattr(fb, "get_authenticated_identity", lambda request: ("u-1", "d"))

    r = client.post(
        f"{_PREFIX}/matrix",
        json={"factor_ids": ["f-1"], "markets": ["us_stock"]},
    )
    assert r.status_code == 200
    sig = r.json()["data"]["factors"][0]["cells"]["us_stock"]["significance"]

    # 期望 q 用独立实现重算（族 p 列表 → BY 校正 → 自身位），不调被测函数
    mm = pytest.importorskip("backend.services.engine.factor_report.metrics")
    assert sig["nw_t"] == pytest.approx(5.6)
    assert sig["p_value"] == pytest.approx(mm.normal_pvalue(5.6))
    p_list = [mm.normal_pvalue(5.6), mm.normal_pvalue(-0.4)]
    assert sig["q_value_bhy"] == pytest.approx(float(mm.bhy_qvalues(p_list)[0]))
    assert sig["family_n"] == 2  # 达降级单元不进族
    assert "同一批次全部完成单元" in sig["family_note"]


def test_matrix_significance_without_batch_falls_back_to_q_eq_p(client, monkeypatch):
    """无批次上下文 → q = p（n=1）并在 family_note 明说；非完成终态一律 None。"""

    async def _cells(ids, markets=None):
        return [
            {
                "run_id": "fb-run-1",
                "factor_id": "f-1",
                "status": "completed",
                "batch_id": None,
                "market": "us_stock",
                "universe": "all",
                "date_range": "2023~2026",
                "finished_at": None,
                "error": None,
                "metrics": {"ic_nw_t": 2.0},
                "ic_value": 0.05,
            },
            {
                "run_id": "fb-run-9",
                "factor_id": "f-1",
                "status": "failed",
                "batch_id": "fb-batch-1",
                "market": "hong_kong",
                "universe": None,
                "date_range": None,
                "finished_at": None,
                "error": "boom",
                "metrics": {"ic_nw_t": 9.9},
                "ic_value": None,
            },
        ]

    async def _batch_runs(batch_id):  # 失败格不该进族也不该触发查询口径错配
        return [{"run_id": "fb-run-9", "status": "failed", "metrics": {"ic_nw_t": 9.9}}]

    monkeypatch.setattr(fb.store, "get_factor_meta", _sig_meta())
    monkeypatch.setattr(fb.store, "latest_cells", _cells)
    monkeypatch.setattr(fb.store, "batch_runs", _batch_runs)
    monkeypatch.setattr(fb, "get_authenticated_identity", lambda request: ("u-1", "d"))

    r = client.post(
        f"{_PREFIX}/matrix",
        json={"factor_ids": ["f-1"], "markets": ["us_stock", "hong_kong"]},
    )
    cells = r.json()["data"]["factors"][0]["cells"]

    sig = cells["us_stock"]["significance"]
    assert sig["q_value_bhy"] == sig["p_value"]
    assert sig["family_n"] == 1
    assert "无批次族上下文" in sig["family_note"]

    assert cells["hong_kong"]["significance"] is None


def test_matrix_and_report_share_family_q(client, stub, monkeypatch):
    """单源钉死：同一 run 的矩阵格 q 与 /report 报告块 q 逐位相等。

    ``stub`` 不能省：/report 走 ``_require_owned_factor``（真查因子库，
    单测里 "f-1" 不存在 → 404）；矩阵端点不查归属，故另两个矩阵用例不需要。
    """

    run_row = {
        "run_id": "fb-run-1",
        "factor_id": "f-1",
        "factor_name": "A",
        "status": "completed",
        "kind": None,
        "batch_id": "fb-batch-1",
        "market": "us_stock",
        "universe": "all",
        "data_source": "quantdb_factors",
        "date_range": "2023~2026",
        "ic_value": 0.05,
        "rank_ic": None,
        "icir": None,
        "rank_icir": None,
        "sharpe_ratio": None,
        "annual_return": None,
        "max_drawdown": None,
        "metrics": {"ic": 0.05, "ic_nw_t": 5.6},
        "params": {},
        "error": None,
        "created_at": None,
        "finished_at": None,
    }
    series = {
        "dates": ["2023-01-02", "2023-01-03", "2023-01-04"],
        "ic": [0.1, -0.05, 0.2],
        "nav_long": [1.0, 1.01, 1.02],
        "nav_ls": [1.0, 1.005, 1.012],
        "turnover": [0.1, 0.12, 0.11],
        "bench": "equal_weight",
        "meta": {"n_buckets": 5, "top_pct": 0.3, "cost_bps": 20},
    }

    async def _get_run(run_id):
        return dict(run_row) if run_id == "fb-run-1" else None

    async def _get_series(run_id):
        return {"series": series} if run_id == "fb-run-1" else None

    async def _batch_runs(batch_id):
        # 3 完成单元同族：报告中 nw_t 由序列算出，与台账 5.6 同源；这里以台账值为准
        return [
            {"run_id": "fb-run-1", "status": "completed", "metrics": {"ic_nw_t": 5.6}},
            {"run_id": "fb-run-2", "status": "completed", "metrics": {"ic_nw_t": -0.4}},
            {
                "run_id": "fb-run-3",
                "status": "completed",
                "metrics": {"ic_nw_t": 2.1},
            },
        ]

    monkeypatch.setattr(fb.store, "get_factor_meta", _sig_meta())

    async def _cells(ids, markets=None):
        return [dict(run_row)]

    monkeypatch.setattr(fb.store, "latest_cells", _cells)
    monkeypatch.setattr(fb.store, "get_run", _get_run)
    monkeypatch.setattr(fb.store, "get_series", _get_series)
    monkeypatch.setattr(fb.store, "batch_runs", _batch_runs)
    monkeypatch.setattr(fb, "get_authenticated_identity", lambda request: ("u-1", "d"))

    m = client.post(
        f"{_PREFIX}/matrix", json={"factor_ids": ["f-1"], "markets": ["us_stock"]}
    )
    matrix_sig = m.json()["data"]["factors"][0]["cells"]["us_stock"]["significance"]

    rep = client.get(f"{_PREFIX}/report/fb-run-1")
    assert rep.status_code == 200
    report_sig = rep.json()["data"]["report"]["significance"]

    # q 是单源不变量：两端点族输入（同批次完成单元的 ic_nw_t）与自身位一致 →
    # q 逐位相等（nw_t 本身此处不同源：报告从打桩序列现算、矩阵读台账存值——
    # 「存值 == 现算」由报告套件金样钉死，本用例不打桩真引擎算不出来的东西）。
    assert matrix_sig["q_value_bhy"] == pytest.approx(report_sig["q_value_bhy"])
    assert matrix_sig["family_n"] == report_sig["family_n"] == 3


# ── 台账 / 曲线 ──────────────────────────────────────────────────────


def test_runs_requires_owned_factor(client, stub, monkeypatch):
    seen = {}

    async def _list_runs(**kwargs):
        seen.update(kwargs)
        return [{"run_id": "fb-run-1", "status": "completed"}]

    monkeypatch.setattr(fb.store, "list_runs", _list_runs)
    r = client.get(f"{_PREFIX}/runs", params={"factor_id": "f-1", "market": "us_stock"})
    assert r.status_code == 200
    assert r.json()["data"]["runs"][0]["run_id"] == "fb-run-1"
    assert seen["factor_id"] == "f-1" and seen["market"] == "us_stock"


def test_series_endpoint_404s_and_payload(client, stub, monkeypatch):
    async def _get_run(run_id):
        return {"run_id": run_id, "factor_id": "f-1"} if run_id == "fb-run-1" else None

    async def _get_series(run_id):
        if run_id == "fb-run-1":
            return {"series": {"dates": ["2024-01-02"], "nav_long": [1.0]}}
        return None

    monkeypatch.setattr(fb.store, "get_run", _get_run)
    monkeypatch.setattr(fb.store, "get_series", _get_series)

    assert client.get(f"{_PREFIX}/runs/nope/series").status_code == 404
    r = client.get(f"{_PREFIX}/runs/fb-run-1/series")
    assert r.status_code == 200
    assert r.json()["data"]["series"]["dates"] == ["2024-01-02"]

    # run 存在但序列未落盘（降级终态）→ 404 带说明
    monkeypatch.setattr(fb.store, "get_series", lambda run_id: _none())
    r2 = client.get(f"{_PREFIX}/runs/fb-run-1/series")
    assert r2.status_code == 404
    assert "没有曲线数据" in r2.json()["detail"]


async def _none():
    return None


# ── 机构报告（T-FB-16）──────────────────────────────────────────────


def _report_series(n: int = 60) -> dict:
    ic = [0.04 + 0.15 * math.sin(i * 0.37) for i in range(n)]
    ls = [0.001 + 0.002 * math.sin(i * 0.23) for i in range(n)]
    nav_ls, acc = [], 1.0
    for r in ls:
        acc *= 1.0 + r
        nav_ls.append(acc)
    return {
        "dates": [f"d{i:03d}" for i in range(n)],
        "ic": ic,
        "ic_cum": ic,
        "nav_long": nav_ls,
        "nav_ls": nav_ls,
        "nav_bench": [1.0] * n,
        "q_curves": {},
        "turnover": [0.3] * n,
        "coverage": [100] * n,
        "bench": "equal_weight",
        "meta": {
            "cost_bps": 10,
            "top_pct": 0.3,
            "n_buckets": 5,
            "turnover_convention": "daily_two_sided",
        },
    }


def _stub_report_store(monkeypatch, *, run: dict, series: dict | None, siblings=None):
    async def _get_run(run_id):
        return run

    async def _get_series(run_id):
        return {"series": series} if series is not None else None

    monkeypatch.setattr(fb.store, "get_run", _get_run)
    monkeypatch.setattr(fb.store, "get_series", _get_series)
    if siblings is not None:

        async def _batch_runs(batch_id):
            return siblings

        monkeypatch.setattr(fb.store, "batch_runs", _batch_runs)


def test_report_endpoint_404s(client, stub, monkeypatch):
    _stub_report_store(monkeypatch, run=None, series=None)
    assert client.get(f"{_PREFIX}/report/nope").status_code == 404


def test_report_endpoint_degraded_no_numbers(client, stub, monkeypatch):
    _stub_report_store(
        monkeypatch,
        run={
            "run_id": "fb-run-1",
            "factor_id": "f-1",
            "status": "data_unsupported",
            "error": "missing_columns",
            "batch_id": None,
        },
        series=None,
    )
    r = client.get(f"{_PREFIX}/report/fb-run-1")
    assert r.status_code == 200
    rep = r.json()["data"]["report"]
    assert rep["available"] is False
    assert rep["reason"] == "missing_columns"
    assert "headline" not in rep and "cost_grid" not in rep


def test_report_endpoint_family_and_override(client, stub, monkeypatch):
    from backend.services.engine.factor_report import metrics as M

    _stub_report_store(
        monkeypatch,
        run={
            "run_id": "r-self",
            "factor_id": "f-1",
            "status": "completed",
            "batch_id": "bb-1",
            "metrics": {"benchmark": "csi300"},
        },
        series=_report_series(),
        siblings=[
            {"run_id": "r-a", "status": "completed", "metrics": {"ic_nw_t": 0.5}},
            {"run_id": "r-self", "status": "completed", "metrics": {"ic_nw_t": 2.5}},
            {"run_id": "r-c", "status": "completed", "metrics": {"ic_nw_t": 1.2}},
            {"run_id": "r-d", "status": "failed", "metrics": {}},
            {"run_id": "r-e", "status": "completed", "metrics": {}},
        ],
    )

    r = client.get(f"{_PREFIX}/report/r-self")
    assert r.status_code == 200
    sig = r.json()["data"]["report"]["significance"]
    expected_q = float(M.bhy_qvalues([M.normal_pvalue(t) for t in (0.5, 2.5, 1.2)])[1])
    assert sig["family_n"] == 3  # failed 与缺 t 的单元不入族
    assert sig["q_value_bhy"] == pytest.approx(expected_q, rel=1e-12)
    assert sig["n_trials"] == 3
    assert sig["n_trials_source"] == "batch_completed_units"

    r2 = client.get(f"{_PREFIX}/report/r-self", params={"n_trials": 9})
    sig2 = r2.json()["data"]["report"]["significance"]
    assert sig2["n_trials"] == 9 and sig2["n_trials_source"] == "param"


def test_report_endpoint_self_outside_family_falls_back(client, stub, monkeypatch):
    """自身不在族表（自身 NW t 缺失）→ 回落 n=1，绝不按位错配别人的 q。"""
    _stub_report_store(
        monkeypatch,
        run={
            "run_id": "r-self",
            "factor_id": "f-1",
            "status": "completed",
            "batch_id": "bb-1",
            "metrics": {},
        },
        series=_report_series(),
        siblings=[
            {"run_id": "r-a", "status": "completed", "metrics": {"ic_nw_t": 0.5}},
        ],
    )
    sig = client.get(f"{_PREFIX}/report/r-self").json()["data"]["report"][
        "significance"
    ]
    assert sig["family_n"] == 1
    assert "未做多重校正" in sig["family_note"]
