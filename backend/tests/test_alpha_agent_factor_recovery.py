"""「补码评估」批次契约（POST /factors/recovery + worker）。

背景（2026-10-09）：「待评估」的 45 条因子 factor_code 为空串（旧提取器落的
半成品），行内「回测」直接 400。新增补码评估批次：LLM 按公式补码 → 写回
factor_code（metadata 标注 code_recovered）→ 自动标准回测补 IC。本文件钉死：

1. 互斥：批次在跑时重复提交返回快照（不起第二个批次）；411/422 语义——
   无 LLM 配置 412 且**不认领**互斥位（否则失败后批次锁永占）；
2. 无候选时 total=0 明示「没有待评估的因子」，不静默空转；
3. worker 口径：成败按**行终态**判（回测把失败写进 status 而不抛异常），
   有码的不重复补码、正被手动回测占用的跳过、连败 3 条熔断且已完成的保留；
4. 路由注册顺序：GET /factors/recovery/status 不被 /factors/{id} 遮蔽。

用 TestClient mini-app（照 test_alpha_agent_factor_list_scope 惯例）；
worker 直调 asyncio.run（不经 create_task——TestClient 每请求独立 portal
loop，后台任务会在响应结束后被取消）。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

try:  # pragma: no cover - 环境相关
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from backend.services.engine.alpha_agent import factor_codegen as fc
    from backend.services.engine.routers import alpha_agent as aa
except Exception:  # noqa: BLE001
    aa = None
    fc = None

pytestmark = pytest.mark.skipif(aa is None, reason="依赖不可用（需容器环境）")

_PREFIX = "/api/v1/alpha-agent"

_IDLE_STATE = {
    "running": False,
    "user_id": None,
    "total": 0,
    "done": 0,
    "failed": 0,
    "skipped": 0,
    "current_factor_id": None,
    "current_factor_name": None,
    "message": None,
    "started_at": None,
    "finished_at": None,
}


@pytest.fixture(autouse=True)
def _isolate_module_state():
    """模块级状态（互斥位/回测去重集合）逐测试复位，互不串味。"""
    aa._recovery_state.update(_IDLE_STATE)
    aa._running_backtests.clear()
    aa._running_backtest_runs.clear()
    aa._backtest_cancelled.clear()
    yield
    aa._recovery_state.update(_IDLE_STATE)
    aa._running_backtests.clear()
    aa._running_backtest_runs.clear()
    aa._backtest_cancelled.clear()


class _StubPersistence:
    """persistence 替身：录参 + 可控终态，不碰真库。"""

    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.recovery_calls: list[dict] = []
        self.saved: list[dict] = []
        self.metrics: list[dict] = []
        self.status_map: dict[str, object] = {}
        self.finished: list[dict] = []
        self._run_seq = 0

    async def list_factors_needing_recovery(self, user_id=None, limit=200):
        self.recovery_calls.append({"user_id": user_id, "limit": limit})
        return list(self.rows)

    async def save_factor(self, factor_id, factor_name, factor_code, **kwargs):
        self.saved.append(
            {
                "factor_id": factor_id,
                "factor_name": factor_name,
                "factor_code": factor_code,
                **kwargs,
            }
        )

    async def update_factor_metrics(self, factor_id, status=None, metadata=None, **kw):
        self.metrics.append(
            {"factor_id": factor_id, "status": status, "metadata": metadata}
        )

    async def get_factor(self, factor_id):
        seq = self.status_map.get(factor_id, "completed")
        if isinstance(seq, list):
            return {"factor_id": factor_id, "status": seq.pop(0)}
        return {"factor_id": factor_id, "status": seq}

    async def start_backtest_run(self, factor_id, **kwargs):
        self._run_seq += 1
        return f"run-{self._run_seq}"

    async def finish_backtest_run(self, run_id, status, **kwargs):
        self.finished.append({"run_id": run_id, "status": status, **kwargs})


@pytest.fixture()
def stub(monkeypatch):
    stub = _StubPersistence()
    monkeypatch.setattr(aa, "persistence", stub)
    return stub


@pytest.fixture()
def client():
    app = FastAPI()

    @app.middleware("http")
    async def _inject_identity(request, call_next):
        request.state.user = {"user_id": "u1", "tenant_id": "default"}
        return await call_next(request)

    app.include_router(aa.router)
    return TestClient(app)


def _factor(fid: str, *, code: str = "", name: str | None = None) -> dict:
    return {
        "factor_id": fid,
        "factor_name": name or f"factor-{fid}",
        "factor_code": code,
        "market": "a_share",
        "universe": "csi300",
        "metadata": {"task_id": "t-old"},
    }


# ── 路由层 ────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_recovery_busy_returns_snapshot_without_second_batch(client, stub):
    aa._recovery_state.update({"running": True, "user_id": "u1", "total": 5, "done": 2})
    r = client.post(f"{_PREFIX}/factors/recovery")
    assert r.status_code == 200
    body = r.json()["data"]
    assert body["running"] is True
    assert body["done"] == 2
    assert "已在进行中" in body["message"]
    assert not stub.recovery_calls  # 未触达候选查询


@pytest.mark.unit
def test_recovery_no_candidates(client, stub):
    stub.rows = []
    r = client.post(f"{_PREFIX}/factors/recovery")
    assert r.status_code == 200
    body = r.json()["data"]
    assert body["total"] == 0
    assert body["running"] is False
    assert "没有待评估" in body["message"]
    assert stub.recovery_calls[-1]["user_id"] == "u1"


@pytest.mark.unit
def test_recovery_without_llm_config_412_and_not_claimed(client, stub, monkeypatch):
    """无 LLM 配置 412，且互斥位不被认领（否则用户补 Key 后批次锁永占）。"""
    stub.rows = [_factor("f1")]

    async def _no_llm(user_id, tenant_id):
        return None, "none", {}

    monkeypatch.setattr(aa, "_resolve_effective_llm_config", _no_llm)
    r = client.post(f"{_PREFIX}/factors/recovery")
    assert r.status_code == 412
    assert "未配置 LLM API Key" in r.json()["detail"]
    assert aa._recovery_state["running"] is False


@pytest.mark.unit
def test_recovery_starts_batch_with_candidates(client, stub, monkeypatch):
    stub.rows = [_factor("f1"), _factor("f2")]

    async def _llm(user_id, tenant_id):
        return object(), "env", {}

    spawned: list = []
    monkeypatch.setattr(aa, "_resolve_effective_llm_config", _llm)
    monkeypatch.setattr(
        aa,
        "_spawn_recovery_batch",
        lambda coro: (spawned.append(coro), coro.close()),
    )
    r = client.post(f"{_PREFIX}/factors/recovery")
    assert r.status_code == 200
    body = r.json()["data"]
    assert body["running"] is True
    assert body["total"] == 2
    assert "user_id" not in body  # 快照对外不带 user_id 字段
    assert len(spawned) == 1
    assert aa._recovery_state["started_at"] is not None


@pytest.mark.unit
@pytest.mark.parametrize("bad", [0, 501])
def test_recovery_limit_out_of_range_422(client, stub, bad):
    r = client.post(f"{_PREFIX}/factors/recovery", params={"limit": bad})
    assert r.status_code == 422
    assert not stub.recovery_calls


@pytest.mark.unit
def test_recovery_status_route_not_shadowed(client, stub):
    """GET /factors/recovery/status 必须命中恢复端点而非 /factors/{id}。"""
    r = client.get(f"{_PREFIX}/factors/recovery/status")
    assert r.status_code == 200
    data = r.json()["data"]
    assert "running" in data and "total" in data

    aa._recovery_state.update({"running": True, "total": 3, "done": 1})
    r2 = client.get(f"{_PREFIX}/factors/recovery/status")
    assert r2.json()["data"]["done"] == 1


@pytest.mark.unit
def test_recovery_status_redacts_current_factor_for_others(client, stub):
    """非发起人轮询：计数可见（知道引擎忙），当前因子身份隐去（跨租户不回吐）。"""
    aa._recovery_state.update(
        {
            "running": True,
            "user_id": "someone-else",
            "total": 9,
            "done": 3,
            "current_factor_id": "f-secret",
            "current_factor_name": "SecretAlpha",
        }
    )
    data = client.get(f"{_PREFIX}/factors/recovery/status").json()["data"]
    assert data["done"] == 3
    assert data["current_factor_id"] is None
    assert data["current_factor_name"] is None


# ── 入口检测（补码实测暴露的缺口：自执行式被判 unknown） ──────────────


@pytest.mark.unit
@pytest.mark.parametrize(
    ("code", "expected"),
    [
        # calculate_* 函数式
        ("def calculate_x():\n    return 1\n", "functional"),
        # 自执行式：main() + __main__ 守卫（2026-10-09 补码实测踩中的形状）
        (
            "import numpy as np\n\n\ndef main():\n    pass\n\n\n"
            "if __name__ == '__main__':\n    main()\n",
            "functional",
        ),
        # Qlib Factor 类（即使带自测守卫也仍是 factor_class，不得进错执行器）
        (
            "import numpy as np\nimport pandas as pd\n\n\n"
            "class AlphaFactor:\n    name = 'x'\n\n\n"
            "if __name__ == '__main__':\n    print(1)\n",
            "factor_class",
        ),
        ("X = 1\n", "unknown"),
    ],
)
def test_detect_factor_kind_covers_self_exec_style(code, expected):
    assert aa._detect_factor_kind(code) == expected


@pytest.mark.unit
def test_detect_factor_kind_syntax_error_raises():
    with pytest.raises(RuntimeError, match="语法错误"):
        aa._detect_factor_kind("def main(:\n")


# ── worker ───────────────────────────────────────────────────────────


@pytest.fixture()
def fake_backtest(monkeypatch):
    calls: list[dict] = []

    async def _run(factor_id, factor_code, **kwargs):
        calls.append({"factor_id": factor_id, "factor_code": factor_code, **kwargs})

    monkeypatch.setattr(aa, "_run_factor_backtest", _run)
    return calls


def _run_worker(factors, *, llm_config=object(), user_id="u1"):
    asyncio.run(aa._run_factor_recovery(factors, llm_config, user_id))


@pytest.mark.unit
def test_worker_codegen_writeback_then_backtest_success(
    stub, fake_backtest, monkeypatch
):
    """无码因子：补码 → 写回带 code_recovered 标注 → 回测 → 终态回读计成功。"""
    codegen_calls: list[dict] = []

    async def _gen(factor, *, config, **kwargs):
        codegen_calls.append(factor)
        return "def calculate_x():\n    return 1\n"

    monkeypatch.setattr(fc, "generate_factor_code", _gen)
    stub.status_map["f1"] = "completed"

    _run_worker([_factor("f1")])

    assert len(codegen_calls) == 1
    assert stub.saved[0]["factor_code"].startswith("def calculate_x")
    assert stub.saved[0]["user_id"] == "u1"
    meta_writes = [m for m in stub.metrics if m["metadata"]]
    assert meta_writes[0]["metadata"]["code_recovered"] == "llm_codegen"
    assert fake_backtest[0]["factor_code"].startswith("def calculate_x")
    assert fake_backtest[0]["run_id"] == "run-1"
    assert fake_backtest[0]["universe"] == "csi300"
    state = aa._recovery_state
    assert state["running"] is False
    assert state["done"] == 1 and state["failed"] == 0
    assert "成功 1" in state["message"]


@pytest.mark.unit
def test_worker_existing_code_skips_codegen(stub, fake_backtest, monkeypatch):
    async def _gen(factor, **kwargs):  # pragma: no cover - 不应被调
        raise AssertionError("有码因子不得重复补码")

    monkeypatch.setattr(fc, "generate_factor_code", _gen)
    stub.status_map["f1"] = "completed"

    _run_worker([_factor("f1", code="def calculate_x():\n    return 2\n")])

    assert stub.saved == []
    assert fake_backtest[0]["factor_code"].startswith("def calculate_x")


@pytest.mark.unit
def test_worker_failed_terminal_status_counts_failed(stub, fake_backtest, monkeypatch):
    """回测失败不抛异常、只写行终态 failed——worker 必须按终态回读计失败。"""
    monkeypatch.setattr(fc, "generate_factor_code", _noop_gen)
    stub.status_map["f1"] = "failed"

    _run_worker([_factor("f1", code="def calculate_x():\n    return 1\n")])

    assert aa._recovery_state["failed"] == 1
    assert aa._recovery_state["done"] == 0


@pytest.mark.unit
def test_worker_skips_factor_with_running_backtest(stub, fake_backtest, monkeypatch):
    monkeypatch.setattr(fc, "generate_factor_code", _noop_gen)
    aa._running_backtests.add("f1")

    _run_worker([_factor("f1", code="def calculate_x():\n    return 1\n")])

    assert aa._recovery_state["skipped"] == 1
    assert fake_backtest == []
    assert stub.saved == []


@pytest.mark.unit
def test_worker_codegen_error_recorded_and_counted(stub, fake_backtest, monkeypatch):
    async def _gen(factor, **kwargs):
        raise fc.FactorCodegenError("代码校验未通过: 缺少入口")

    monkeypatch.setattr(fc, "generate_factor_code", _gen)

    _run_worker([_factor("f1")])

    assert aa._recovery_state["failed"] == 1
    assert fake_backtest == []
    recover_meta = [
        m for m in stub.metrics if m["metadata"] and "recover_error" in m["metadata"]
    ]
    assert "缺少入口" in recover_meta[0]["metadata"]["recover_error"]


@pytest.mark.unit
def test_worker_circuit_breaks_after_three_consecutive_failures(
    stub, fake_backtest, monkeypatch
):
    """连败 3 条熔断：第 4 条不再处理，已完成的保留、message 说明可重发。"""
    monkeypatch.setattr(fc, "generate_factor_code", _noop_gen)
    for fid in ("f1", "f2", "f3", "f4"):
        stub.status_map[fid] = "failed"

    _run_worker(
        [
            _factor(f, code="def calculate_x():\n    return 1\n")
            for f in ("f1", "f2", "f3", "f4")
        ]
    )

    assert len(fake_backtest) == 3  # f4 未处理
    state = aa._recovery_state
    assert state["failed"] == 3
    assert "提前中止" in state["message"]
    assert state["running"] is False


@pytest.mark.unit
def test_worker_consecutive_counter_resets_on_success(stub, fake_backtest, monkeypatch):
    """失败-失败-成功-失败-失败：连败计数被成功重置，不误触发熔断。"""
    monkeypatch.setattr(fc, "generate_factor_code", _noop_gen)
    stub.status_map["f1"] = "failed"
    stub.status_map["f2"] = "failed"
    stub.status_map["f3"] = "completed"
    stub.status_map["f4"] = "failed"
    stub.status_map["f5"] = "failed"

    _run_worker(
        [
            _factor(f, code="def calculate_x():\n    return 1\n")
            for f in ("f1", "f2", "f3", "f4", "f5")
        ]
    )

    assert len(fake_backtest) == 5
    state = aa._recovery_state
    assert state["failed"] == 4 and state["done"] == 1
    assert "提前中止" not in (state["message"] or "")


async def _noop_gen(factor, **kwargs):
    return "def calculate_x():\n    return 1\n"
