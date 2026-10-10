"""evolve 并行方向数（T-MV-04）—— 设置页 N 方向 → 实际派发 N 任务的路由契约。

钉住的边：

1. **N>1 只属于类别路径**：directions 非空、doc_id 为空、num_directions>1 才
   走并行派发（start_or_queue 批量语义：满额排队而非 429）；自由文本/文档血统
   路径 N 不适用——单方向是它们的事实，行为与旧版一字不变。
2. **抽取可复现**：random 模式的每条任务各带独立 meta，逐步不放回（第 k 条
   meta 的 candidates = 该步剩余集合），任何一条都能单独重放复现。
3. **selected 不抽样**：按序取前 N，meta=NULL——没抽签就没有抽签凭证。
4. **一条不拖垮整批**：QueueFullError/意外异常 → 该条目 failed，其余照派。
5. **请求级问题整包 400**：任一命中方向超长，一条都不派（与 /mining/batch 同纪律）。

纪律：计数用假 store 注入——本文件是纯路由单测，不碰真库（asyncpg 池按 loop
绑定，单测里开的池会毒化后续真库用例的探活，表现为静默 skip）。
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.services.engine.alpha_agent.doc_gate import ENV_KEY  # noqa: E402
from backend.services.engine.alpha_agent.launcher import QueueFullError  # noqa: E402
from backend.services.engine.alpha_agent.llm_client import LLMConfig  # noqa: E402
from backend.services.engine.routers import alpha_agent as router_mod  # noqa: E402

CFG = LLMConfig(
    api_key="sk-x", base_url="https://llm.test/v1", model="m1", protocol="openai"
)


class FakeLauncher:
    """start_or_queue（批量环）+ start_evolution（单条路径）双替身。"""

    def __init__(self, outcomes: dict[int, object] | None = None) -> None:
        self.calls: list[dict] = []
        self.evolve_calls: list[dict] = []
        self.outcomes = outcomes or {}

    async def start_or_queue(self, user_id, **kw):
        idx = len(self.calls)
        self.calls.append({"user_id": user_id, **kw})
        outcome = self.outcomes.get(idx)
        if isinstance(outcome, Exception):
            raise outcome
        if outcome is not None:
            return outcome
        return SimpleNamespace(task_id=f"t{idx}", status="running", queue_position=None)

    async def start_evolution(self, user_id, **kw):
        self.evolve_calls.append({"user_id": user_id, **kw})
        return "task-single"

    def count_running(self):
        return {"global": 0, "by_user": {}}

    def running_capacity(self):
        from backend.services.engine.alpha_agent.launcher import AlphaAgentLauncher

        return AlphaAgentLauncher.running_capacity(self)


class FakeStore:
    def __init__(self, counts: dict[str, int] | None = None) -> None:
        self.counts = counts or {}
        self.scopes: list[dict] = []

    async def count_by_direction(self, *, user_id, market, directions):
        self.scopes.append(
            {"user_id": user_id, "market": market, "directions": list(directions)}
        )
        return {d: self.counts.get(d, 0) for d in directions if self.counts.get(d, 0)}


class FakeDocStore:
    def __init__(self, row: dict | None = None) -> None:
        self.row = row
        self.updated: list[tuple[str, dict]] = []

    async def get_doc(self, doc_id, *, user_id=None):
        return dict(self.row) if self.row else None

    async def update_doc(self, doc_id, **fields):
        self.updated.append((doc_id, fields))


def _wire(
    monkeypatch,
    launcher: FakeLauncher,
    *,
    store: FakeStore | None = None,
    doc_store: FakeDocStore | None = None,
):
    from backend.services.engine.alpha_agent import task_store as task_store_mod

    fake_store = store or FakeStore()
    monkeypatch.setattr(task_store_mod, "get_mining_task_store", lambda: fake_store)
    monkeypatch.setattr(router_mod, "get_launcher", lambda: launcher)
    monkeypatch.setattr(
        router_mod, "get_doc_store", lambda: doc_store or FakeDocStore()
    )
    monkeypatch.setattr(
        router_mod, "get_authenticated_identity", lambda req: ("u-1", "t-1")
    )
    monkeypatch.setattr(router_mod, "assert_identity_not_spoofed", lambda **kw: None)
    calls: dict = {}

    async def fake_llm(user_id, tenant_id):
        calls["llm"] = (user_id, tenant_id)
        return CFG, "user_profile", None

    monkeypatch.setattr(router_mod, "_resolve_effective_llm_config", fake_llm)
    return calls, fake_store


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
        "quality_gate_mode": "",
        "data_source": "",
    }
    kwargs.update(over)
    return router_mod.start_evolution(payload=payload, **kwargs)


def _replay(meta: dict) -> str:
    return random.Random(meta["seed"]).choices(
        [c["direction"] for c in meta["candidates"]],
        weights=[c["weight"] for c in meta["candidates"]],
        k=1,
    )[0]


@pytest.mark.asyncio
async def test_random_n_of_m_dispatches_distinct_with_replayable_metas(
    monkeypatch,
) -> None:
    """random N=2/3：两条互不相同的任务，各自 meta 独立可重放，权重按空白度。"""
    launcher = FakeLauncher()
    _calls, store = _wire(monkeypatch, launcher, store=FakeStore({"方向A": 9}))

    out = await _call(
        directions=["方向A", "方向B", "方向C"],
        direction_mode="random",
        num_directions=2,
    )

    data = out["data"]
    assert data["started"] == 2 and data["queued"] == 0 and data["failed"] == 0
    dirs = [it["direction"] for it in data["items"]]
    assert len(dirs) == 2 and len(set(dirs)) == 2, "并行方向必须互不相同"
    assert data["direction_mode"] == "random"
    # 计数口径：本用户 × 本市场 × 候选集
    assert store.scopes == [
        {
            "user_id": "u-1",
            "market": "a_share",
            "directions": ["方向A", "方向B", "方向C"],
        }
    ]
    # 派发环收到的模式/证据与响应条目一致
    assert [c["direction_mode"] for c in launcher.calls] == ["random", "random"]
    assert [c["direction"] for c in launcher.calls] == dirs

    seen_remaining = [{"方向A", "方向B", "方向C"}]
    for it, call in zip(data["items"], launcher.calls, strict=True):
        assert it["direction_meta"] == call["direction_meta"]
        meta = json.loads(it["direction_meta"])
        assert meta["mode"] == "random" and meta["weighting"] == "blankness"
        assert meta["picked"] == it["direction"]
        assert _replay(meta) == meta["picked"], "落档证据可独立重放（逐步快照）"
        by_dir = {c["direction"]: c for c in meta["candidates"]}
        if "方向A" in by_dir:
            assert by_dir["方向A"]["attempts"] == 9
            assert by_dir["方向A"]["weight"] == pytest.approx(0.1)
        # 逐步不放回：第 2 条的候选集 = 第 1 条候选去掉已抽中的
        assert set(by_dir) == seen_remaining[-1]
        seen_remaining.append(seen_remaining[-1] - {it["direction"]})
    assert len(seen_remaining[-1]) == 1


@pytest.mark.asyncio
async def test_selected_takes_first_n_without_evidence(monkeypatch) -> None:
    """selected N=2：按序取前两条——确定性，不抽样就不落抽样凭证。"""
    launcher = FakeLauncher()
    _calls, store = _wire(monkeypatch, launcher)

    out = await _call(
        directions=["方向一", "方向二", "方向三"],
        direction_mode="selected",
        num_directions=2,
    )

    data = out["data"]
    assert [it["direction"] for it in data["items"]] == ["方向一", "方向二"]
    assert all(it["direction_meta"] is None for it in data["items"])
    assert all(c["direction_meta"] is None for c in launcher.calls)
    assert all(c["direction_mode"] == "selected" for c in launcher.calls)
    assert store.scopes == [], "selected 是确定性取用，不该读挖掘史"


@pytest.mark.asyncio
async def test_n_not_smaller_than_candidates_dispatches_all_without_evidence(
    monkeypatch,
) -> None:
    """N >= 候选数：全集直派（去重保序）、meta=NULL——没抽签就没有抽签凭证。"""
    launcher = FakeLauncher()
    _calls, store = _wire(monkeypatch, launcher)

    out = await _call(
        directions=["方向一", "方向二", "方向一"],
        direction_mode="random",
        num_directions=5,
    )

    data = out["data"]
    assert [it["direction"] for it in data["items"]] == ["方向一", "方向二"]
    assert all(it["direction_meta"] is None for it in data["items"])
    assert store.scopes == [], "全集直派不抽签，不读史"


@pytest.mark.asyncio
async def test_free_text_path_ignores_num_directions(monkeypatch) -> None:
    """自由文本路径 N 不适用：仍走单条 start_evolution，行为零变化。"""
    launcher = FakeLauncher()
    _calls, _store = _wire(monkeypatch, launcher)

    out = await _call(direction="自由文本方向", num_directions=3)

    assert len(launcher.evolve_calls) == 1
    assert launcher.evolve_calls[0]["user_id"] == "u-1"
    assert launcher.evolve_calls[0]["direction"] == "自由文本方向"
    assert launcher.calls == [], "不得走并行派发环"
    assert out["data"]["task_id"] == "task-single" and "items" not in out["data"]


@pytest.mark.asyncio
async def test_doc_path_forces_single_even_with_num_directions(monkeypatch) -> None:
    """文档血统单列 task_id：N>1 被收敛为 1，血统回写照常。"""
    monkeypatch.setenv(ENV_KEY, "true")
    launcher = FakeLauncher()
    doc_store = FakeDocStore(row={"doc_id": "d1", "user_id": "u-1", "status": "parsed"})
    _calls, _store = _wire(monkeypatch, launcher, doc_store=doc_store)

    out = await _call(
        payload=router_mod.EvolveRequest(
            doc_id="d1", direction="复现方向", num_directions=2
        )
    )

    assert len(launcher.evolve_calls) == 1 and launcher.calls == []
    assert launcher.evolve_calls[0]["doc_id"] == "d1"
    assert doc_store.updated == [("d1", {"task_id": "task-single"})]
    assert out["data"]["source"] == "doc"


@pytest.mark.asyncio
async def test_num_directions_body_overrides_query(monkeypatch) -> None:
    """JSON body 变体：num_directions 非 None 覆盖 query。"""
    launcher = FakeLauncher()
    _calls, _store = _wire(monkeypatch, launcher)

    await _call(
        payload=router_mod.EvolveRequest(
            directions=["方向一", "方向二"], direction_mode="selected", num_directions=2
        ),
        directions=["方向一", "方向二"],
        direction_mode="selected",
        num_directions=1,
    )

    assert [it["direction"] for it in launcher.calls] == ["方向一", "方向二"]


@pytest.mark.asyncio
async def test_one_item_failure_does_not_sink_the_batch(monkeypatch) -> None:
    """第 2 条排队满：该条 failed 带原因，其余照派，HTTP 恒 200。"""
    launcher = FakeLauncher(outcomes={1: QueueFullError("队列已满（上限 8）")})
    _calls, _store = _wire(monkeypatch, launcher)

    out = await _call(
        directions=["方向一", "方向二", "方向三"],
        direction_mode="selected",
        num_directions=3,
    )

    data = out["data"]
    assert data["started"] == 2 and data["failed"] == 1
    assert data["items"][1]["task_id"] is None
    assert "队列已满" in data["items"][1]["error"]
    assert data["items"][2]["task_id"] == "t2"
    assert data["task_id"] == "t0", "顶层 task_id = 首条已派发任务"


@pytest.mark.asyncio
async def test_overlong_pick_rejects_whole_request(monkeypatch) -> None:
    """命中方向超长：整包 400（第 N 条点名），一条都不派、不烧 LLM。"""
    launcher = FakeLauncher()
    calls, _store = _wire(monkeypatch, launcher)

    with pytest.raises(HTTPException) as ei:
        await _call(
            directions=["正常方向", "长" * (router_mod.MAX_SUBMIT_DIRECTION_CHARS + 1)],
            direction_mode="selected",
            num_directions=2,
        )

    assert ei.value.status_code == 400
    assert "第 2 条" in str(ei.value.detail)
    assert launcher.calls == [] and launcher.evolve_calls == []
    assert "llm" not in calls, "长度闸在 LLM 解析之前"


@pytest.mark.asyncio
async def test_sampler_failure_falls_back_to_plain_sample(monkeypatch) -> None:
    """抽样模块整体炸了：退普通不放回随机（互不相同）+ meta NULL，绝不拦派发。"""
    launcher = FakeLauncher()
    _calls, _store = _wire(monkeypatch, launcher)

    async def _boom(*a, **kw):
        raise RuntimeError("import exploded")

    monkeypatch.setattr(router_mod, "sample_weighted_directions_n", _boom)

    out = await _call(
        directions=["方向一", "方向二", "方向三"],
        direction_mode="random",
        num_directions=2,
    )

    data = out["data"]
    assert len(data["items"]) == 2 and data["failed"] == 0
    assert all(it["direction_meta"] is None for it in data["items"])
    assert len({it["direction"] for it in data["items"]}) == 2
