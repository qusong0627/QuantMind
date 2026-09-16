"""T-FE-05 一键执行测试：三重闸门（活跃/仅模拟盘/60s 防重）+ 人工排除集 + 源守卫。

纪律：执行侧测试**绝不真触发撮合**——调度执行入口一律以桩替换；
闸门本身用真库/真 Redis 行为边界验证（无活跃策略 → 409）。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException

_BACKEND = Path(__file__).resolve().parents[1]


# ── 纯函数 ──────────────────────────────────────────────────────────


@pytest.mark.unit
def test_parse_exclude_symbols():
    from backend.services.api.routers.desk import parse_exclude_symbols

    assert parse_exclude_symbols(None) == set()
    assert parse_exclude_symbols("") == set()
    assert parse_exclude_symbols(" 600036.SH , 000001.SZ ,600036.SH ") == {"600036.SH", "000001.SZ"}
    capped = parse_exclude_symbols(",".join(f"S{i}" for i in range(80)))
    assert len(capped) == 50  # 上限截断（防大载荷）


# ── 闸门（fake 依赖注入）────────────────────────────────────────────


class _FakeRawRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.ttl_value = 60

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    def ttl(self, key):
        return self.ttl_value


class _FakeRedisWrapper:
    def __init__(self) -> None:
        self.client = _FakeRawRedis()


def _patch_redis(monkeypatch, wrapper: _FakeRedisWrapper) -> None:
    import backend.services.trade_shared.redis_client as rc

    monkeypatch.setattr(rc, "get_redis", lambda: wrapper)


@pytest.mark.asyncio
async def test_execute_requires_active_strategy(monkeypatch):
    """真环境边界：当前无活跃策略 → 409（防重锁之前就拒绝，不消耗锁）。"""
    from backend.services.api.routers import desk

    wrapper = _FakeRedisWrapper()
    _patch_redis(monkeypatch, wrapper)

    with pytest.raises(HTTPException) as exc:
        await desk.execute_plan(
            payload=None, current_user={"tenant_id": "default", "user_id": "00000001"}
        )
    assert exc.value.status_code == 409
    assert "无活跃策略" in exc.value.detail or "读取失败" in exc.value.detail
    assert wrapper.client.store == {}  # 未加锁


@pytest.mark.asyncio
async def test_execute_rejects_real_mode(monkeypatch):
    from backend.services.api.routers import desk

    _patch_redis(monkeypatch, _FakeRedisWrapper())
    monkeypatch.setattr(
        desk,
        "_resolve_active_strategy",
        lambda t, u: ({"strategy_id": "28", "mode": "REAL", "live_trade_config": {}}, None),
    )
    with pytest.raises(HTTPException) as exc:
        await desk.execute_plan(payload=None, current_user={"tenant_id": "default", "user_id": "1"})
    assert exc.value.status_code == 409
    assert "仅限模拟盘" in exc.value.detail


@pytest.mark.asyncio
async def test_execute_rate_limit_and_passthrough(monkeypatch):
    """首发成功并透传排除集；60s 内二次触发 → 429 附剩余秒数。"""
    from backend.services.api.routers import desk
    from backend.services.simulation.services import simulation_hosted_scheduler as sched

    wrapper = _FakeRedisWrapper()
    _patch_redis(monkeypatch, wrapper)
    monkeypatch.setattr(
        desk,
        "_resolve_active_strategy",
        lambda t, u: (
            {"strategy_id": "28", "strategy_name": "测试", "mode": "SIMULATION", "live_trade_config": {"pool_id": "p1"}},
            None,
        ),
    )

    captured: dict[str, Any] = {}

    async def _stub_execute(**kwargs):
        captured.update(kwargs)
        return {"status": "ok", "order_count": 2, "filled_count": 2}

    monkeypatch.setattr(sched, "execute_simulation_plan_for_active", _stub_execute)

    result = await desk.execute_plan(
        payload={"exclude_symbols": ["600036.SH", "000001.SZ"]},
        current_user={"tenant_id": "default", "user_id": "1"},
    )
    assert result["success"] is True
    assert result["data"]["report"]["order_count"] == 2
    assert result["data"]["excluded"] == ["000001.SZ", "600036.SH"]
    # 透传核验：执行入口收到同一排除集与配置
    assert captured["exclude_symbols"] == {"600036.SH", "000001.SZ"}
    assert captured["live_trade_config"] == {"pool_id": "p1"}

    with pytest.raises(HTTPException) as exc:
        await desk.execute_plan(payload=None, current_user={"tenant_id": "default", "user_id": "1"})
    assert exc.value.status_code == 429
    assert "防重" in exc.value.detail and "60" in exc.value.detail


# ── 引擎侧排除语义（真 RebalanceCalculator）─────────────────────────


@pytest.mark.asyncio
async def test_engine_exclude_filters_rebalance_signals(monkeypatch):
    """排除集命中的标的不参与调仓；未命中标的正常下单。"""
    from backend.services.simulation.engine import SimulationEngine
    from backend.services.simulation.services.rebalance_calculator import StrategyConfig
    from backend.services.simulation.services.signal_loader import SignalScore

    signals = [
        SignalScore(symbol="600036", score=0.9, trade_date=date(2026, 9, 16), run_id="r", tenant_id="default", user_id="1"),
        SignalScore(symbol="000001", score=0.8, trade_date=date(2026, 9, 16), run_id="r", tenant_id="default", user_id="1"),
    ]

    class _Loader:
        async def load_latest_signals(self, **kw):
            return list(signals)

    class _Bar:
        close = 10.0
        limit_up = 11.0
        limit_down = 9.0
        suspended = False
        pre_close = 10.0

    engine = SimulationEngine(loader=_Loader())

    async def _cfg(db, strategy_id, user_id, params_override=None, market=None):
        return StrategyConfig(topk=2, lot_size=100)

    async def _acct(user_id, tenant_id="default", market="CN"):
        return {"cash": 100000.0, "total_asset": 100000.0, "positions": {}}

    async def _bars(symbols, as_of=None, market=None):
        return {s: _Bar() for s in symbols}

    async def _none(a, b):
        return None

    monkeypatch.setattr(engine, "_load_strategy_config", _cfg)
    monkeypatch.setattr(engine.account_manager, "get_account", _acct)
    monkeypatch.setattr(engine, "_load_bars", _bars)
    monkeypatch.setattr(engine, "_load_exit_ruleset", _none)
    monkeypatch.setattr(engine, "_apply_risk_buy_locks", lambda orders, **kw: orders)

    report = await engine.run_cycle(
        tenant_id="default",
        user_id="1",
        strategy_id="99",
        dry_run=True,
        exclude_symbols={"600036"},  # 裸码/后缀都应在归一后命中
    )
    symbols = {o["symbol"] for o in report.planned_orders}
    assert symbols == {"000001.SZ"}, f"排除后只应剩 000001，实际 {symbols}"


# ── 接线与纪律守卫 ──────────────────────────────────────────────────


@pytest.mark.unit
def test_execute_wiring_source_guards():
    desk_src = (_BACKEND / "services/api/routers/desk.py").read_text(encoding="utf-8")
    assert '@router.post("/plan/execute")' in desk_src
    assert "qm:desk:plan:execute:" in desk_src  # 60s 防重锁
    assert "防重锁不可用" in desk_src and "fail-closed" in desk_src.lower() or "拒绝执行" in desk_src
    assert "仅限模拟盘" in desk_src

    sched_src = (_BACKEND / "services/simulation/services/simulation_hosted_scheduler.py").read_text(encoding="utf-8")
    # 一键执行必须复用唯一执行入口（禁止旁路）
    exec_block = sched_src.split("async def execute_simulation_plan_for_active")[1].split("def _parse_started_at")[0]
    assert "run_simulation_cycle_for_active(" in exec_block
    assert "绝不" not in exec_block  # 执行入口本身允许撮合；预演入口才要求绝不执行

    engine_src = (_BACKEND / "services/simulation/engine.py").read_text(encoding="utf-8")
    # 退出规则不可被排除的纪律写入注释（机构口径留痕）
    assert "退出规则单不受排除影响" in engine_src
