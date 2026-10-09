"""evolve 的 JSON body 变体 + doc_id 血统（T-FM-10）。

四条纪律，逐条钉住：

1. **老调用形态不破**：只带 query 的老前端（payload=None）行为一字不变。
2. **doc_id 走闸门**：ENABLE_DOC_MINING 关时带 doc_id 提交一律 403——文档链
   没开的地方不存在「合法的 doc_id」。
3. **归属 + 状态**：他人 doc_id → 404（不多说一个字）；未解析完 → 409。
4. **血统回写不拦主链**：task_id 写回 rd_agent_docs 失败只告警，挖掘照常返回
   （回写是审计面，不是主链）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.services.engine.alpha_agent.doc_gate import ENV_KEY  # noqa: E402
from backend.services.engine.alpha_agent.llm_client import LLMConfig  # noqa: E402
from backend.services.engine.routers import alpha_agent as router_mod  # noqa: E402


class FakeLauncher:
    def __init__(self) -> None:
        self.started: dict | None = None
        self.count_running_calls = 0

    def count_running(self):
        self.count_running_calls += 1
        return {"global": 0, "by_user": {}}

    def running_capacity(self):
        """委托真实现（容量口径的唯一读取点）——本文件不测容量，但合约形状
        必须与真 launcher 一致，否则路由的 429 判定会在双替身上抛 AttributeError。"""
        from backend.services.engine.alpha_agent.launcher import AlphaAgentLauncher

        return AlphaAgentLauncher.running_capacity(self)

    async def start_evolution(self, user_id, **kw):
        self.started = {"user_id": user_id, **kw}
        return "task-9"


class FakeDocStore:
    def __init__(
        self, row: dict | None = None, *, update_error: Exception | None = None
    ):
        self.row = row
        self.update_error = update_error
        self.updated: list[tuple[str, dict]] = []
        self.get_calls: list[dict] = []

    async def get_doc(self, doc_id, *, user_id=None):
        self.get_calls.append({"doc_id": doc_id, "user_id": user_id})
        if self.row is None:
            return None
        if user_id is not None and self.row.get("user_id") != user_id:
            return None
        return dict(self.row)

    async def update_doc(self, doc_id, **fields):
        if self.update_error is not None:
            raise self.update_error
        self.updated.append((doc_id, fields))


def _wire(
    monkeypatch, launcher: FakeLauncher, store: FakeDocStore | None = None
) -> None:
    monkeypatch.setattr(router_mod, "get_launcher", lambda: launcher)
    monkeypatch.setattr(router_mod, "get_doc_store", lambda: store or FakeDocStore())
    monkeypatch.setattr(
        router_mod, "get_authenticated_identity", lambda req: ("u-1", "t-1")
    )
    monkeypatch.setattr(router_mod, "assert_identity_not_spoofed", lambda **kw: None)
    calls: dict = {}
    cfg = LLMConfig(
        api_key="sk-x", base_url="https://llm.test/v1", model="m1", protocol="openai"
    )

    async def fake_llm(user_id, tenant_id):
        calls["llm"] = (user_id, tenant_id)
        return cfg, "user_profile", None

    monkeypatch.setattr(router_mod, "_resolve_effective_llm_config", fake_llm)
    return calls


def _call(payload=None, **over):
    kwargs = {
        "request": SimpleNamespace(),
        "user_id": None,
        "market": "a_share",
        "universe": "csi300",
        "loop_n": 5,
        "direction": "",
        "directions": [],
        "direction_mode": "selected",
        "data_source": "",
    }
    kwargs.update(over)
    return router_mod.start_evolution(payload=payload, **kwargs)


@pytest.mark.asyncio
async def test_evolve_query_only_form_unchanged(monkeypatch) -> None:
    launcher = FakeLauncher()
    calls = _wire(monkeypatch, launcher)

    out = await _call(payload=None, direction="动量方向")

    assert launcher.started["source"] == "text"
    assert launcher.started["doc_id"] is None
    assert launcher.started["direction"] == "动量方向"
    assert out["data"]["task_id"] == "task-9"
    assert out["data"]["source"] == "text" and out["data"]["doc_id"] is None
    assert calls["llm"] == ("u-1", "t-1")


@pytest.mark.asyncio
async def test_evolve_json_body_overrides_query(monkeypatch) -> None:
    launcher = FakeLauncher()
    _wire(monkeypatch, launcher)

    payload = router_mod.EvolveRequest(
        loop_n=7, direction="body 方向", data_source="pg", direction_mode="random"
    )
    await _call(payload=payload, loop_n=5, direction="query 方向")

    assert launcher.started["loop_n"] == 7
    assert launcher.started["direction"] == "body 方向"
    assert launcher.started["data_source"] == "pg"


@pytest.mark.asyncio
async def test_evolve_doc_id_requires_gate(monkeypatch) -> None:
    monkeypatch.delenv(ENV_KEY, raising=False)
    launcher = FakeLauncher()
    _wire(monkeypatch, launcher)

    with pytest.raises(HTTPException) as ei:
        await _call(payload=router_mod.EvolveRequest(doc_id="d1"))

    assert ei.value.status_code == 403
    assert ei.value.detail == "doc_mining_disabled"
    assert launcher.started is None


@pytest.mark.asyncio
async def test_evolve_doc_id_not_owned_404(monkeypatch) -> None:
    monkeypatch.setenv(ENV_KEY, "true")
    launcher = FakeLauncher()
    store = FakeDocStore(
        row={"doc_id": "d1", "user_id": "someone-else", "status": "parsed"}
    )
    _wire(monkeypatch, launcher, store)

    with pytest.raises(HTTPException) as ei:
        await _call(payload=router_mod.EvolveRequest(doc_id="d1"))

    assert ei.value.status_code == 404
    assert store.get_calls == [{"doc_id": "d1", "user_id": "u-1"}]
    assert launcher.started is None


@pytest.mark.asyncio
async def test_evolve_doc_id_not_parsed_409(monkeypatch) -> None:
    monkeypatch.setenv(ENV_KEY, "true")
    launcher = FakeLauncher()
    store = FakeDocStore(row={"doc_id": "d1", "user_id": "u-1", "status": "parsing"})
    _wire(monkeypatch, launcher, store)

    with pytest.raises(HTTPException) as ei:
        await _call(payload=router_mod.EvolveRequest(doc_id="d1"))

    assert ei.value.status_code == 409
    assert "parsing" in str(ei.value.detail)
    assert launcher.started is None


@pytest.mark.asyncio
async def test_evolve_doc_id_happy_writes_lineage(monkeypatch) -> None:
    monkeypatch.setenv(ENV_KEY, "true")
    launcher = FakeLauncher()
    store = FakeDocStore(row={"doc_id": "d1", "user_id": "u-1", "status": "organized"})
    _wire(monkeypatch, launcher, store)

    out = await _call(
        payload=router_mod.EvolveRequest(doc_id="d1", direction="复现方向")
    )

    assert launcher.started["source"] == "doc"
    assert launcher.started["doc_id"] == "d1"
    assert launcher.started["direction"] == "复现方向"
    assert store.updated == [("d1", {"task_id": "task-9"})]
    assert out["data"]["source"] == "doc" and out["data"]["doc_id"] == "d1"


@pytest.mark.asyncio
async def test_evolve_lineage_writeback_failure_still_succeeds(monkeypatch) -> None:
    monkeypatch.setenv(ENV_KEY, "true")
    launcher = FakeLauncher()
    store = FakeDocStore(
        row={"doc_id": "d1", "user_id": "u-1", "status": "parsed"},
        update_error=RuntimeError("pg down"),
    )
    _wire(monkeypatch, launcher, store)

    out = await _call(payload=router_mod.EvolveRequest(doc_id="d1"))
    assert out["data"]["task_id"] == "task-9"
    assert launcher.started["doc_id"] == "d1"


@pytest.mark.asyncio
async def test_evolve_rejects_overlong_direction_before_llm(monkeypatch) -> None:
    launcher = FakeLauncher()
    calls = _wire(monkeypatch, launcher)

    with pytest.raises(HTTPException) as ei:
        await _call(direction="字" * (router_mod.MAX_SUBMIT_DIRECTION_CHARS + 1))

    assert ei.value.status_code == 400
    assert str(router_mod.MAX_SUBMIT_DIRECTION_CHARS) in str(ei.value.detail)
    assert "llm" not in calls and launcher.started is None


@pytest.mark.asyncio
async def test_evolve_directions_selection_then_cap_applies(monkeypatch) -> None:
    """多选类别选中超长的一条 → 同样在提交前被 8k 闸拦下（选中之后才判长）。"""
    launcher = FakeLauncher()
    _wire(monkeypatch, launcher)

    with pytest.raises(HTTPException) as ei:
        await _call(
            directions=["长" * (router_mod.MAX_SUBMIT_DIRECTION_CHARS + 1), "短方向"]
        )

    assert ei.value.status_code == 400
    assert launcher.started is None
