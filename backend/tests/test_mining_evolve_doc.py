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
        # 直调必须显式传全部 Query 参数：缺省值是 Query() 对象不是 1
        "num_directions": 1,
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


@pytest.mark.asyncio
async def test_evolve_records_effective_direction_mode(monkeypatch) -> None:
    """方向历史（T-MV-02）：类别方向实际被选中时，生效模式随任务落档。"""
    launcher = FakeLauncher()
    _wire(monkeypatch, launcher)

    await _call(directions=["方向A", "方向B"], direction_mode="selected")

    assert launcher.started["direction"] == "方向A"
    assert launcher.started["direction_mode"] == "selected"
    assert launcher.started["direction_meta"] is None, (
        "selected 是确定性取第一条——没有抽样，就没有抽样证据（NULL 不是空 JSON）"
    )


@pytest.mark.asyncio
async def test_evolve_random_mode_is_recorded(monkeypatch) -> None:
    """random 模式抽取的方向、模式与抽样证据一起落档（单条候选排除随机抖动）。

    计数用假 store：本文件是纯路由单测，不碰真库——asyncpg 池按 loop 绑定，
    单测里开的池会毒化后续真库用例的探活（表现为静默 skip）。
    """
    import json

    from backend.services.engine.alpha_agent import task_store as task_store_mod

    class _FakeStore:
        async def count_by_direction(self, *, user_id, market, directions):
            return {}

    monkeypatch.setattr(task_store_mod, "get_mining_task_store", lambda: _FakeStore())

    launcher = FakeLauncher()
    _wire(monkeypatch, launcher)

    await _call(directions=["唯一方向"], direction_mode="random")

    assert launcher.started["direction"] == "唯一方向"
    assert launcher.started["direction_mode"] == "random"
    meta = json.loads(launcher.started["direction_meta"])
    assert meta["mode"] == "random" and meta["picked"] == "唯一方向"
    assert isinstance(meta["seed"], int)
    assert [c["direction"] for c in meta["candidates"]] == ["唯一方向"]


@pytest.mark.asyncio
async def test_evolve_random_meta_reflects_mining_history_weights(monkeypatch) -> None:
    """加权抽样（T-MV-03）：权重的数据源 = 本用户×本市场的方向挖掘史次数。

    用假 store 固定计数（真库有残留行会让权重不可测），断言 meta 里逐候选的
    attempts/weight 与 w=1/(1+n) 完全一致、且命中可重放。
    """
    import json
    import random

    from backend.services.engine.alpha_agent import task_store as task_store_mod

    class _FakeStore:
        async def count_by_direction(self, *, user_id, market, directions):
            assert user_id == "u-1" and market == "a_share"
            return {"方向A": 9}

    monkeypatch.setattr(task_store_mod, "get_mining_task_store", lambda: _FakeStore())

    launcher = FakeLauncher()
    _wire(monkeypatch, launcher)

    await _call(directions=["方向A", "方向B"], direction_mode="random")

    meta = json.loads(launcher.started["direction_meta"])
    by_dir = {c["direction"]: c for c in meta["candidates"]}
    assert by_dir["方向A"]["attempts"] == 9
    assert by_dir["方向A"]["weight"] == pytest.approx(0.1)
    assert by_dir["方向B"]["attempts"] == 0 and by_dir["方向B"]["weight"] == 1.0
    assert (
        meta["weighting"] == "blankness"
        and meta["picked"] == launcher.started["direction"]
    )
    replayed = random.Random(meta["seed"]).choices(
        [c["direction"] for c in meta["candidates"]],
        weights=[c["weight"] for c in meta["candidates"]],
        k=1,
    )[0]
    assert replayed == meta["picked"], "落档证据可复现（验收条款的机器化表达）"


@pytest.mark.asyncio
async def test_evolve_sampler_failure_falls_back_to_plain_choice(monkeypatch) -> None:
    """抽样模块整体炸了也不许拦任务创建：退普通均匀 choice，meta NULL（诚实留白）。"""
    launcher = FakeLauncher()
    _wire(monkeypatch, launcher)

    async def _boom(*a, **kw):
        raise RuntimeError("import exploded")

    monkeypatch.setattr(router_mod, "sample_weighted_direction", _boom)

    await _call(directions=["方向A", "方向B"], direction_mode="random")

    assert launcher.started["direction"] in {"方向A", "方向B"}
    assert launcher.started["direction_mode"] == "random"
    assert launcher.started["direction_meta"] is None


@pytest.mark.asyncio
async def test_evolve_free_text_records_no_mode(monkeypatch) -> None:
    """自由文本方向：类别选择没参与 → 模式 NULL，不伪记 query 的默认 selected。"""
    launcher = FakeLauncher()
    _wire(monkeypatch, launcher)

    # _call 默认 direction_mode="selected"——但它不该被记进任务
    await _call(direction="自由文本方向")

    assert launcher.started["direction"] == "自由文本方向"
    assert launcher.started["direction_mode"] is None
    assert launcher.started["direction_meta"] is None
