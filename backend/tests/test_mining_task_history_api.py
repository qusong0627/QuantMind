"""挖掘历史端点（GET /alpha-agent/tasks/history，机构级 P0 / T-FM-03）。

三个静默故障点，逐条钉住：

1. **路由顺序**：FastAPI 按注册顺序匹配。`/tasks/history` 若注册在
   `/tasks/{task_id}` 之后，会被当作 task_id="history" 吞掉——表现为
   历史页永远 404「Task history not found」，而路由表看起来一切正常。
2. **身份收口**：列表按认证用户过滤，query 里的 user_id 只是防伪校验字段；
   把别人的历史混进来是数据面越权（同类接口的既有纪律）。
3. **未知状态 → 400**：store 的 ValueError 必须映射成 400 而不是 500，
   也不是静默空列表（静默空 = 「我挖的怎么没了」无从定位）。
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.services.engine.routers import alpha_agent as router_mod  # noqa: E402


def _fake_request() -> SimpleNamespace:
    """端点身份经 get_authenticated_identity(request) 读取（测试里被替换）。"""
    return SimpleNamespace()


def _auth_as(monkeypatch, user_id: str) -> None:
    monkeypatch.setattr(
        router_mod, "get_authenticated_identity", lambda req: (user_id, "t-1")
    )
    monkeypatch.setattr(router_mod, "assert_identity_not_spoofed", lambda **kw: None)


def test_history_route_registered_before_task_id_route() -> None:
    """路由顺序回归：history 必须先于 {task_id} 注册，否则被参数路由吞掉。"""
    paths = [r.path for r in router_mod.router.routes]
    history_idx = paths.index("/api/v1/alpha-agent/tasks/history")
    task_id_idx = paths.index("/api/v1/alpha-agent/tasks/{task_id}")
    assert history_idx < task_id_idx, (
        "把 /tasks/history 移到 /tasks/{task_id} 后注册会把历史页整个吞掉"
    )


@pytest.mark.asyncio
async def test_history_endpoint_scopes_to_auth_user(monkeypatch) -> None:
    calls: dict = {}
    count_calls: dict = {}

    class _Store:
        async def list_history(self, **kwargs):
            calls.update(kwargs)
            return [{"task_id": "t-x", "direction": "动量"}]

        async def count_history(self, **kwargs):
            count_calls.update(kwargs)
            return 137

    monkeypatch.setattr(router_mod, "get_mining_task_store", lambda: _Store())
    _auth_as(monkeypatch, "u-42")

    out = await router_mod.mining_task_history(
        request=_fake_request(),
        user_id=None,
        market="a_share",
        status="completed",
        limit=20,
        offset=5,
    )

    assert calls["user_id"] == "u-42", "过滤身份必须来自认证，不是 query"
    assert calls["market"] == "a_share" and calls["status"] == "completed"
    assert calls["limit"] == 20 and calls["offset"] == 5
    # total 必须是全量 COUNT（分页「共 N 条」）——本页行数会让它永远 ≤ limit
    assert count_calls["user_id"] == "u-42"
    assert out["code"] == 200
    assert out["data"]["tasks"][0]["task_id"] == "t-x"
    assert out["data"]["total"] == 137
    assert out["data"]["limit"] == 20 and out["data"]["offset"] == 5


@pytest.mark.asyncio
async def test_history_endpoint_maps_unknown_status_to_400(monkeypatch) -> None:
    class _Store:
        async def list_history(self, **kwargs):
            raise ValueError("unknown task status: 'backtesting'")

    monkeypatch.setattr(router_mod, "get_mining_task_store", lambda: _Store())
    _auth_as(monkeypatch, "u-42")

    with pytest.raises(HTTPException) as ei:
        await router_mod.mining_task_history(
            request=_fake_request(),
            user_id=None,
            market=None,
            status="backtesting",
            limit=50,
            offset=0,
        )
    assert ei.value.status_code == 400
    assert "backtesting" in str(ei.value.detail)


# ── 真库端到端（走端点函数体，不打假 store） ─────────────────────────


def _scope() -> str:
    return f"t-mining-api-{uuid.uuid4().hex[:10]}"


@pytest.mark.asyncio
async def test_history_endpoint_real_db_returns_own_rows_only(monkeypatch) -> None:
    from sqlalchemy import text

    from backend.services.engine.alpha_agent.task_store import get_mining_task_store
    from backend.shared.database_manager_v2 import close_database, get_session

    try:
        async with get_session(read_only=True) as probe:
            await probe.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"DB 不可用: {exc}")

    user = _scope()
    other = _scope()
    try:
        store = get_mining_task_store()
        await store.ensure_tables()
        await store.create_task(task_id="t-api-1", user_id=user, direction="我的方向")
        await store.create_task(
            task_id="t-api-2", user_id=other, direction="别人的方向"
        )

        monkeypatch.setattr(router_mod, "get_mining_task_store", lambda: store)
        _auth_as(monkeypatch, user)

        out = await router_mod.mining_task_history(
            request=_fake_request(),
            user_id=None,
            market=None,
            status=None,
            limit=50,
            offset=0,
        )

        ids = [t["task_id"] for t in out["data"]["tasks"]]
        assert ids == ["t-api-1"], "别人的历史不得出现"
        assert out["data"]["tasks"][0]["direction"] == "我的方向"
        assert out["data"]["tasks"][0]["created_at"].endswith("Z")
    finally:
        async with get_session() as session:
            await session.execute(
                text("DELETE FROM rd_agent_mining_tasks WHERE user_id IN (:u, :o)"),
                {"u": user, "o": other},
            )
        await close_database()
