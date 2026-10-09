"""批量派发端点（拆解卡片 → 逐条 start_or_queue 成任务）的契约。

用户诉求：「我给了 10 个方向、一批一批的去挖掘……或者几批一起挖」。

与 evolve 的差异是本端点的存在理由：evolve 满员即 **429 背压**（老前端
「原文上屏」）；批量派发**不拒正常提交**——满员自动排队，只有排队深度上限
（ALPHA_AGENT_MAX_QUEUED_*）才逐条失败。钉住的边：

1. **逐条独立**：一条失败（QueueFullError/HardwareLockError/意外异常）不拖垮
   整批，错误随该条目回传（HTTP 恒 200，多状态信封），已派发的照常跑。
2. **回执保真**：running/queued + 1-based 位次原样透传；顺序与请求一致（index）。
3. **请求级错误先拒**：空列表/超条数上限/任一方向为空/超长/未知市场 →
   整包 400（一条都没派，不存在半批派出去）。
4. **LLM 取值链与 evolve 同一条**：无配置 412；overrides 只算一次复用全批
   （密钥不落任何持久层，排队任务排空时由解析器重新取值）。
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

from backend.services.engine.alpha_agent.hw_lock import HardwareLockError  # noqa: E402
from backend.services.engine.alpha_agent.launcher import QueueFullError  # noqa: E402
from backend.services.engine.alpha_agent.llm_client import LLMConfig  # noqa: E402
from backend.services.engine.routers import alpha_agent as router_mod  # noqa: E402

CFG = LLMConfig(
    api_key="sk-x", base_url="https://llm.test/v1", model="m1", protocol="openai"
)


class FakeLauncher:
    """start_or_queue 替身：outcomes 按调用序注入异常/回执。"""

    def __init__(self, outcomes: dict[int, object] | None = None) -> None:
        self.calls: list[dict] = []
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


def _wire(monkeypatch, launcher: FakeLauncher, llm=CFG) -> None:
    monkeypatch.setattr(router_mod, "get_launcher", lambda: launcher)
    monkeypatch.setattr(
        router_mod, "get_authenticated_identity", lambda req: ("u-1", "t-1")
    )

    async def fake_resolve(user_id, tenant_id):
        return llm, "user_profile" if llm else "none", None

    monkeypatch.setattr(router_mod, "_resolve_effective_llm_config", fake_resolve)


async def _call(payload):
    return await router_mod.dispatch_mining_batch(
        request=SimpleNamespace(), payload=payload
    )


@pytest.mark.asyncio
async def test_batch_dispatch_happy_all_started(monkeypatch) -> None:
    launcher = FakeLauncher()
    _wire(monkeypatch, launcher)

    out = await _call(
        router_mod.MiningBatchRequest(
            directions=["方向一", "方向二", "方向三"], loop_n=7
        )
    )

    assert out["code"] == 200
    data = out["data"]
    assert data["started"] == 3 and data["queued"] == 0 and data["failed"] == 0
    assert [it["task_id"] for it in data["items"]] == ["t0", "t1", "t2"]
    assert all(it["status"] == "running" for it in data["items"])
    assert all(it["error"] is None for it in data["items"])
    # 逐条参数保真：顺序、方向、市场/池/轮数、租户、overrides
    assert [c["direction"] for c in launcher.calls] == ["方向一", "方向二", "方向三"]
    assert launcher.calls[0]["user_id"] == "u-1"
    assert launcher.calls[0]["tenant_id"] == "t-1"
    assert launcher.calls[0]["market"] == "a_share"
    assert launcher.calls[0]["universe"] == "csi300"
    assert launcher.calls[0]["loop_n"] == 7
    assert isinstance(launcher.calls[0]["llm_overrides"], dict)
    assert launcher.calls[0]["llm_overrides"] == launcher.calls[2]["llm_overrides"]


@pytest.mark.asyncio
async def test_batch_dispatch_queue_position_passthrough(monkeypatch) -> None:
    launcher = FakeLauncher(
        {
            0: SimpleNamespace(task_id="a", status="running", queue_position=None),
            1: SimpleNamespace(task_id="b", status="queued", queue_position=4),
        }
    )
    _wire(monkeypatch, launcher)

    out = await _call(router_mod.MiningBatchRequest(directions=["甲", "乙"]))
    data = out["data"]
    assert data["started"] == 1 and data["queued"] == 1
    assert data["items"][1]["status"] == "queued"
    assert data["items"][1]["queue_position"] == 4


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc",
    [
        QueueFullError("您的挖掘排队已满（20/20），请等待排队任务开始后再派发"),
        HardwareLockError("算力锁定中：另有高负载任务在跑"),
    ],
)
async def test_batch_dispatch_expected_failure_does_not_kill_batch(
    monkeypatch, exc
) -> None:
    launcher = FakeLauncher({1: exc})
    _wire(monkeypatch, launcher)

    out = await _call(router_mod.MiningBatchRequest(directions=["甲", "乙", "丙"]))
    data = out["data"]
    assert data["started"] == 2 and data["failed"] == 1
    assert data["items"][1]["task_id"] is None
    assert str(exc) in data["items"][1]["error"]
    assert data["items"][2]["task_id"] == "t2"  # 后续条目照常派发
    assert len(launcher.calls) == 3


@pytest.mark.asyncio
async def test_batch_dispatch_unexpected_error_is_itemized(monkeypatch) -> None:
    launcher = FakeLauncher({0: RuntimeError("db gone")})
    _wire(monkeypatch, launcher)

    out = await _call(router_mod.MiningBatchRequest(directions=["甲", "乙"]))
    data = out["data"]
    assert data["failed"] == 1 and data["started"] == 1
    assert "db gone" in data["items"][0]["error"]
    assert data["items"][1]["task_id"] == "t1"


@pytest.mark.asyncio
async def test_batch_dispatch_rejects_blank_direction_with_index(monkeypatch) -> None:
    launcher = FakeLauncher()
    _wire(monkeypatch, launcher)

    with pytest.raises(HTTPException) as ei:
        await _call(router_mod.MiningBatchRequest(directions=["甲", "   ", "丙"]))
    assert ei.value.status_code == 400
    assert "第 2 条" in str(ei.value.detail)
    assert launcher.calls == []  # 整包先拒，一条都没派


@pytest.mark.asyncio
async def test_batch_dispatch_rejects_overlong_direction_with_index(
    monkeypatch,
) -> None:
    launcher = FakeLauncher()
    _wire(monkeypatch, launcher)

    too_long = "字" * (router_mod.MAX_SUBMIT_DIRECTION_CHARS + 1)
    with pytest.raises(HTTPException) as ei:
        await _call(router_mod.MiningBatchRequest(directions=["甲", too_long]))
    assert ei.value.status_code == 400
    assert "第 2 条" in str(ei.value.detail)
    assert str(router_mod.MAX_SUBMIT_DIRECTION_CHARS) in str(ei.value.detail)
    assert launcher.calls == []


@pytest.mark.asyncio
async def test_batch_dispatch_rejects_too_many_items(monkeypatch) -> None:
    launcher = FakeLauncher()
    _wire(monkeypatch, launcher)

    with pytest.raises(HTTPException) as ei:
        await _call(
            router_mod.MiningBatchRequest(
                directions=["方向"] * (router_mod.MAX_BATCH_DISPATCH_ITEMS + 1)
            )
        )
    assert ei.value.status_code == 400
    assert str(router_mod.MAX_BATCH_DISPATCH_ITEMS) in str(ei.value.detail)
    assert launcher.calls == []


@pytest.mark.asyncio
async def test_batch_dispatch_412_without_llm(monkeypatch) -> None:
    launcher = FakeLauncher()
    _wire(monkeypatch, launcher, llm=None)

    with pytest.raises(HTTPException) as ei:
        await _call(router_mod.MiningBatchRequest(directions=["甲"]))
    assert ei.value.status_code == 412
    assert "未配置 LLM API Key" in str(ei.value.detail)
    assert launcher.calls == []


@pytest.mark.asyncio
async def test_batch_dispatch_unknown_market_400(monkeypatch) -> None:
    launcher = FakeLauncher()
    _wire(monkeypatch, launcher)

    with pytest.raises(HTTPException) as ei:
        await _call(
            router_mod.MiningBatchRequest(directions=["甲"], market="no_such_market")
        )
    assert ei.value.status_code == 400
    assert "no_such_market" in str(ei.value.detail)
    assert launcher.calls == []


@pytest.mark.asyncio
async def test_batch_dispatch_unknown_cn_universe_400(monkeypatch) -> None:
    launcher = FakeLauncher()
    _wire(monkeypatch, launcher)

    with pytest.raises(HTTPException) as ei:
        await _call(
            router_mod.MiningBatchRequest(directions=["甲"], universe="no_such_pool")
        )
    assert ei.value.status_code == 400
    assert launcher.calls == []


def test_batch_request_directions_required_nonempty() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        router_mod.MiningBatchRequest(directions=[])
