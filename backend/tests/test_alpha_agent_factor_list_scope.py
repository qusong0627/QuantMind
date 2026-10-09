"""``GET /factors`` 的任务级收口与分页上限契约（WS1）。

背景（2026-10-08）：因子挖掘结果区「挖到多少显示多少」被双层截断挡住——
后端 ``GET /tasks/{task_id}`` 载荷硬限 20 条（2s 轮询要小，保留），前端只
取 10 条。修复方式：结果区权威清单改走 ``GET /factors?task_id=…&limit=500``。
本文件钉死：

1. ``task_id`` / ``limit`` / ``offset`` 原样透传到 ``persistence.list_factors``；
2. 上限 500：``limit=501`` 在路由层 422，不落进 SQL；``offset`` 负数同拒；
3. 响应如实带 ``limit`` / ``offset`` 字段（界面据此显示「已达上限」与翻页进度）；
4. ``total`` / ``quality_counts`` 走 ``factor_scope_stats`` 的**全量口径**，
   与列表窗口（最新 limit 条）长度解耦、且共用同一组过滤参数——界面
   「越挖、中等因子越少」的根因就是拿窗口当全量（2026-10-09）；
5. ``/tasks/{task_id}`` 载荷内嵌因子**仍是 20 条**（刻意保留，别顺手改大）；
6. ``user_id`` 查询参数只做防伪，与认证身份不符 403（身份一律取自 JWT）。

用 ``TestClient`` mini-app（照 ``test_us_stock_terminal.py`` 惯例）：Query
参数校验（422）只有过真实路由栈才作数；身份经 http middleware 注入
``request.state.user``（alpha_agent 不用 Depends，直接读 state）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

try:  # pragma: no cover - 环境相关
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from backend.services.engine.routers import alpha_agent as aa
except Exception:  # noqa: BLE001
    aa = None

pytestmark = pytest.mark.skipif(aa is None, reason="依赖不可用（需容器环境）")

_PREFIX = "/api/v1/alpha-agent"


@pytest.fixture()
def client():
    app = FastAPI()

    @app.middleware("http")
    async def _inject_identity(request, call_next):
        request.state.user = {"user_id": "u1", "tenant_id": "default"}
        return await call_next(request)

    app.include_router(aa.router)
    return TestClient(app)


@pytest.fixture()
def record_factors(monkeypatch):
    """persistence.list_factors / factor_scope_stats 录参替身：不碰真库。

    ``stats`` 缺省按「回放行数=总数」构造（旧断言兼容）；显式传入可模拟
    「窗口外还有更多」——总数与窗口长度解耦正是本轮修复的契约。
    """
    calls: list[dict] = []
    stats_calls: list[dict] = []

    def _apply(rows=None, *, raises: Exception | None = None, stats: dict | None = None):
        async def _list_factors(**kwargs):
            calls.append(kwargs)
            if raises is not None:
                raise raises
            return list(rows or [])

        scope = stats if stats is not None else {
            "total": len(rows or []),
            "high": 0,
            "medium": 0,
            "low": 0,
            "unknown": 0,
        }

        async def _factor_scope_stats(**kwargs):
            stats_calls.append(kwargs)
            return dict(scope)

        monkeypatch.setattr(
            aa,
            "persistence",
            SimpleNamespace(
                list_factors=_list_factors, factor_scope_stats=_factor_scope_stats
            ),
        )

    return SimpleNamespace(apply=_apply, calls=calls, stats_calls=stats_calls)


# ── /factors 透传与响应 ────────────────────────────────────────────────


@pytest.mark.unit
def test_task_id_and_limit_passthrough(client, record_factors):
    record_factors.apply(
        [
            {"factor_id": "f1", "factor_name": "A"},
            {"factor_id": "f2", "factor_name": "B"},
        ]
    )
    r = client.get(f"{_PREFIX}/factors", params={"task_id": "task-1", "limit": 500})
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == 200
    assert body["data"]["total"] == 2
    assert body["data"]["limit"] == 500
    assert [f["factor_id"] for f in body["data"]["factors"]] == ["f1", "f2"]

    kwargs = record_factors.calls[-1]
    assert kwargs["user_id"] == "u1"  # 身份取认证态，不取 query
    assert kwargs["task_id"] == "task-1"
    assert kwargs["limit"] == 500
    # 未传的过滤条件保持 None（不被 query 默认值污染）
    assert kwargs["status"] is None and kwargs["market"] is None
    assert kwargs["universe"] is None


@pytest.mark.unit
def test_defaults_are_50(client, record_factors):
    record_factors.apply([])
    r = client.get(f"{_PREFIX}/factors")
    assert r.status_code == 200
    assert record_factors.calls[-1]["limit"] == 50
    assert record_factors.calls[-1]["task_id"] is None


@pytest.mark.unit
def test_offset_passthrough_and_echo(client, record_factors):
    """offset 原样落到 SQL 分页参数并回显（翻更早的因子，勿把窗口当全量）。"""
    record_factors.apply([{"factor_id": "f1"}])
    r = client.get(f"{_PREFIX}/factors", params={"offset": 200, "limit": 100})
    assert r.status_code == 200
    body = r.json()["data"]
    assert body["offset"] == 200
    assert record_factors.calls[-1]["offset"] == 200
    assert record_factors.calls[-1]["limit"] == 100


@pytest.mark.unit
def test_offset_negative_422(client, record_factors):
    record_factors.apply([])
    r = client.get(f"{_PREFIX}/factors", params={"offset": -1})
    assert r.status_code == 422
    assert not record_factors.calls


@pytest.mark.unit
def test_total_and_quality_counts_are_full_scope(client, record_factors):
    """total/quality_counts 用**全量统计**，不是窗口长度（「越挖、中等越少」根因）。

    回放 3 行、全量统计 291——响应 total 必须是 291 且带四档计数；
    统计与列表收到同一组过滤参数（否则瓦片与列表口径分叉）。
    """
    record_factors.apply(
        [{"factor_id": f"f{i}"} for i in range(3)],
        stats={"total": 291, "high": 0, "medium": 47, "low": 199, "unknown": 45},
    )
    r = client.get(
        f"{_PREFIX}/factors", params={"task_id": "t-9", "market": "a_share"}
    )
    assert r.status_code == 200
    body = r.json()["data"]
    assert body["total"] == 291  # 不是 3（窗口长度）
    assert body["quality_counts"] == {
        "high": 0,
        "medium": 47,
        "low": 199,
        "unknown": 45,
    }
    assert len(body["factors"]) == 3

    stats_kwargs = record_factors.stats_calls[-1]
    list_kwargs = record_factors.calls[-1]
    for key in ("user_id", "status", "market", "universe", "task_id"):
        assert stats_kwargs[key] == list_kwargs[key], f"{key} 口径分叉"


@pytest.mark.unit
@pytest.mark.parametrize("bad", [501, 0, -1])
def test_limit_out_of_range_422(client, record_factors, bad):
    """501 在路由层就被拒（旧上限 200 的界面问题即由此类静默截断放大）。"""
    record_factors.apply([])
    r = client.get(f"{_PREFIX}/factors", params={"limit": bad})
    assert r.status_code == 422
    assert not record_factors.calls  # 未触达 persistence


@pytest.mark.unit
def test_user_id_spoof_403(client, record_factors):
    """query 的 user_id 只做防伪：与 JWT 身份不符 403（不静默忽略）。"""
    record_factors.apply([])
    r = client.get(f"{_PREFIX}/factors", params={"user_id": "u2"})
    assert r.status_code == 403
    r_same = client.get(f"{_PREFIX}/factors", params={"user_id": "u1"})
    assert r_same.status_code == 200


# ── /tasks/{task_id} 内嵌载荷仍限 20（刻意保留） ───────────────────────


@pytest.mark.unit
def test_task_status_payload_still_capped_20(client, monkeypatch, record_factors):
    """2s 轮询端点载荷不许长大：内嵌 factors 恒 20 条。

    结果区权威清单已改走 /factors?task_id=…（本测试是「别顺手改大」的反向
    护栏）；顺带钉死响应仍带 task_id 供前端关联。
    """

    async def _fake_require(task_id, request):
        return {"task_id": task_id, "status": "running", "user_id": "u1"}

    monkeypatch.setattr(aa, "_require_owned_task", _fake_require)
    record_factors.apply([{"factor_id": f"f{i}"} for i in range(20)])

    r = client.get(f"{_PREFIX}/tasks/task-9")
    assert r.status_code == 200
    body = r.json()["data"]
    assert body["task_id"] == "task-9"
    assert len(body["factors"]) == 20
    assert record_factors.calls[-1]["limit"] == 20
    assert record_factors.calls[-1]["task_id"] == "task-9"


@pytest.mark.unit
def test_task_status_factor_error_degrades_to_empty(
    client, monkeypatch, record_factors
):
    """/tasks 的内嵌清单失败降级为空列表（轮询不能因附加载荷 500）。"""
    record_factors.apply(raises=RuntimeError("db down"))

    async def _fake_require(task_id, request):
        return {"task_id": task_id, "status": "running", "user_id": "u1"}

    monkeypatch.setattr(aa, "_require_owned_task", _fake_require)
    r = client.get(f"{_PREFIX}/tasks/task-9")
    assert r.status_code == 200
    assert r.json()["data"]["factors"] == []


# ── 路由注册顺序（结果区/物化端点的真实路由栈回归） ───────────────────


@pytest.mark.unit
def test_materialize_status_route_not_shadowed_by_factor_detail(client, monkeypatch):
    """``/factors/materialize/status`` 必须命中物化端点而非 ``/factors/{id}``。

    两个路由段数相同（4 vs 3）不冲突，但注册顺序是隐性契约：若日后把
    ``GET /factors/{factor_id}`` 挪到前面，FastAPI 会先匹配 detail 端点、
    factor_id="materialize" —— 直调端点的测试抓不到，只有真实栈能抓。
    """

    async def _empty_candidates(**kwargs):
        return []

    monkeypatch.setattr(
        "backend.scripts.rd_mined_materialize._query_candidates",
        _empty_candidates,
    )
    r = client.get(f"{_PREFIX}/factors/materialize/status", params={"factor_ids": "f1"})
    assert r.status_code == 200
    assert "factors" in r.json()["data"]  # detail 端点会 404，不会长这样


@pytest.mark.unit
def test_materialize_status_empty_ids_400(client):
    """不带 factor_ids 的状态查询 = 调用方 bug（400），不静默返回全空。"""
    r = client.get(f"{_PREFIX}/factors/materialize/status")
    assert r.status_code == 400
