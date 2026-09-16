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


# ── 人工改量（T-FE-05 v2）：载荷解析 fail-fast ───────────────────────


@pytest.mark.unit
def test_parse_quantity_overrides():
    from backend.services.api.routers.desk import parse_quantity_overrides

    assert parse_quantity_overrides(None) == {}
    assert parse_quantity_overrides([]) == {}
    parsed = parse_quantity_overrides(
        [
            {"symbol": "600036.SH", "side": "buy", "quantity": 500},
            {"symbol": "000001.SZ", "side": "SELL", "quantity": 1200},
        ]
    )
    assert parsed == {("600036.SH", "BUY"): 500, ("000001.SZ", "SELL"): 1200}
    # 数量为整数字符串（JSON 宽松来源）也接受
    assert parse_quantity_overrides([{"symbol": "X", "side": "BUY", "quantity": "300"}]) == {
        ("X", "BUY"): 300
    }
    # 整数值浮点（300.0）接受——前端 number input 的常见形态
    assert parse_quantity_overrides([{"symbol": "X", "side": "BUY", "quantity": 300.0}]) == {
        ("X", "BUY"): 300
    }


@pytest.mark.unit
@pytest.mark.parametrize(
    "bad",
    [
        "not-a-list",
        [42],
        [{}],
        [{"side": "BUY", "quantity": 100}],  # 缺 symbol
        [{"symbol": "X", "side": "HOLD", "quantity": 100}],  # 非法方向
        [{"symbol": "X", "side": "BUY"}],  # 缺数量
        [{"symbol": "X", "side": "BUY", "quantity": 0}],  # 非正
        [{"symbol": "X", "side": "BUY", "quantity": -5}],
        [{"symbol": "X", "side": "BUY", "quantity": 1.5}],
        [{"symbol": "X", "side": "BUY", "quantity": True}],  # bool 是 int 子类，显式拒绝
        [  # 重复键
            {"symbol": "X", "side": "BUY", "quantity": 100},
            {"symbol": "X", "side": "BUY", "quantity": 200},
        ],
    ],
)
def test_parse_quantity_overrides_fail_fast(bad):
    """资金相关的人工调整不做静默降级：任何一条不合法整体 400（由端点转）。"""
    from backend.services.api.routers.desk import parse_quantity_overrides

    with pytest.raises(ValueError):
        parse_quantity_overrides(bad)


@pytest.mark.unit
def test_parse_quantity_overrides_cap():
    from backend.services.api.routers.desk import parse_quantity_overrides

    over = [{"symbol": f"S{i}", "side": "BUY", "quantity": 100} for i in range(51)]
    with pytest.raises(ValueError):
        parse_quantity_overrides(over)


# ── 人工改量：引擎侧唯一实现（纯函数）────────────────────────────────


def _mk_orders():
    from backend.services.simulation.services.rebalance_calculator import Order

    return [
        Order(symbol="600519.SH", side="SELL", quantity=300, price=1500.0, reason="止损"),
        Order(symbol="600036.SH", side="BUY", quantity=1000, price=40.0, reason="调仓"),
        Order(symbol="688111.SH", side="BUY", quantity=200, price=100.0, reason="调仓"),
    ]


@pytest.mark.unit
def test_apply_overrides_normalize_and_immutability():
    """主板上限整手向下取整（1250→1200）；不改价格/方向/理由；原列表不被就地修改。"""
    from backend.services.simulation.engine import apply_quantity_overrides

    orders = _mk_orders()
    adjusted, records = apply_quantity_overrides(
        orders, {("600036.SH", "BUY"): 1250}, exit_order_count=1
    )
    assert adjusted[1].quantity == 1200
    assert adjusted[1].price == orders[1].price and adjusted[1].side == "BUY"
    assert adjusted[1].reason == "调仓"
    assert orders[1].quantity == 1000  # 原对象不可变（dataclasses.replace）
    assert records == [
        {"symbol": "600036.SH", "side": "BUY", "requested": 1250, "from": 1000, "to": 1200, "applied": True}
    ]


@pytest.mark.unit
def test_apply_overrides_star_market_rules():
    """科创板：201 合法（200 起 1 股递增）；150 低于最小申报 → 拒改并记录。"""
    from backend.services.simulation.engine import apply_quantity_overrides

    adjusted, records = apply_quantity_overrides(
        _mk_orders(), {("688111.SH", "BUY"): 201}, exit_order_count=1
    )
    assert adjusted[2].quantity == 201
    adjusted2, records2 = apply_quantity_overrides(
        _mk_orders(), {("688111.SH", "BUY"): 150}, exit_order_count=1
    )
    assert adjusted2[2].quantity == 200  # 未生效
    assert records2[0]["applied"] is None and "最小申报" in records2[0]["reason"]


@pytest.mark.unit
def test_apply_overrides_exit_order_rejected():
    """退出规则单不可改量（风控动作不绕过）——拒改且理由留痕。"""
    from backend.services.simulation.engine import apply_quantity_overrides

    adjusted, records = apply_quantity_overrides(
        _mk_orders(), {("600519.SH", "SELL"): 100}, exit_order_count=1
    )
    assert adjusted[0].quantity == 300
    assert records[0]["applied"] is None
    assert "退出规则单不可改量" in records[0]["reason"]


@pytest.mark.unit
def test_apply_overrides_unmatched_key_recorded():
    """未命中当前计划的改量（两轮之间行情变化致计划消失）如实记录，不猜测不补单。"""
    from backend.services.simulation.engine import apply_quantity_overrides

    adjusted, records = apply_quantity_overrides(
        _mk_orders(), {("NOTEXIST.SH", "BUY"): 100}, exit_order_count=1
    )
    assert len(adjusted) == 3  # 不新增单
    assert records[0]["applied"] is None
    assert "不存在同标的同方向单" in records[0]["reason"]


@pytest.mark.unit
def test_apply_overrides_empty_noop():
    from backend.services.simulation.engine import apply_quantity_overrides

    orders = _mk_orders()
    adjusted, records = apply_quantity_overrides(orders, None, exit_order_count=1)
    assert adjusted is not orders and len(adjusted) == 3
    assert records == []


# ── 人工改量：引擎全链（真 RebalanceCalculator + 退出单）─────────────


@pytest.mark.asyncio
async def test_engine_quantity_overrides_full_chain(monkeypatch):
    """预演链上改量：调仓单数量被改写并归一；退出单不可改（同轮并存验证）。

    真数据流：SignalLoader(桩) → RebalanceCalculator(真) → 退出单(桩) →
    _apply_risk_buy_locks(桩旁路) → apply_quantity_overrides(真)。
    """
    from backend.services.simulation.engine import SimulationEngine
    from backend.services.simulation.services.rebalance_calculator import Order, StrategyConfig
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
    # 退出单桩（T-P2-04b 起评估为 async）：一只持仓触发止损（kind=exit，名单首位）
    async def _exit_stub(account, quotes, rules, **kwargs):
        return [
            Order(symbol="600519.SH", side="SELL", quantity=300, price=1500.0, reason="止损")
        ]

    monkeypatch.setattr(engine, "_evaluate_position_exits", _exit_stub)

    report = await engine.run_cycle(
        tenant_id="default",
        user_id="1",
        strategy_id="99",
        dry_run=True,
        quantity_overrides={
            ("600036.SH", "BUY"): 1234,  # 引擎把裸码后缀化，键须用后缀式；归一为 1200（整手）
            ("600519.SH", "SELL"): 100,  # 退出单 → 拒改
            ("NOPE.SH", "BUY"): 100,  # 未命中 → 记录
        },
    )

    by_key = {(o["symbol"], o["side"]): o for o in report.planned_orders}
    # 调仓单：命中的那只被改量并归一（600036 → 600036.SZ 后缀化）
    rebalance_buy = [o for o in report.planned_orders if o["side"] == "BUY" and o["kind"] == "rebalance"]
    assert rebalance_buy, "应有调仓买单"
    changed = [o for o in rebalance_buy if o["symbol"].startswith("600036")]
    assert changed and changed[0]["quantity"] == 1200, f"改量未生效: {report.planned_orders}"
    # 退出单原量
    exit_order = by_key[("600519.SH", "SELL")]
    assert exit_order["quantity"] == 300 and exit_order["kind"] == "exit"
    # 裁定记录：1 应用 + 1 退出拒改 + 1 未命中
    r = report.quantity_adjustments
    assert any(x.get("applied") is True for x in r)
    assert any(x["applied"] is None and "退出规则单" in x["reason"] for x in r)
    assert any(x["applied"] is None and "不存在同标的同方向单" in x["reason"] for x in r)


# ── API 透传与 400 ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_execute_passes_quantity_overrides(monkeypatch):
    from backend.services.api.routers import desk
    from backend.services.simulation.services import simulation_hosted_scheduler as sched

    _patch_redis(monkeypatch, _FakeRedisWrapper())
    monkeypatch.setattr(
        desk,
        "_resolve_active_strategy",
        lambda t, u: ({"strategy_id": "28", "mode": "SIMULATION", "live_trade_config": {}}, None),
    )

    captured: dict[str, Any] = {}

    async def _stub_execute(**kwargs):
        captured.update(kwargs)
        return {"status": "ok", "quantity_adjustments": [{"symbol": "600036.SH", "side": "BUY", "applied": True}]}

    monkeypatch.setattr(sched, "execute_simulation_plan_for_active", _stub_execute)

    result = await desk.execute_plan(
        payload={"quantity_overrides": [{"symbol": "600036.SH", "side": "BUY", "quantity": 500}]},
        current_user={"tenant_id": "default", "user_id": "1"},
    )
    assert result["success"] is True
    assert captured["quantity_overrides"] == {("600036.SH", "BUY"): 500}
    assert result["data"]["quantity_overrides"] == [
        {"symbol": "600036.SH", "side": "BUY", "quantity": 500}
    ]
    assert result["data"]["report"]["quantity_adjustments"][0]["applied"] is True


@pytest.mark.asyncio
async def test_execute_rejects_malformed_overrides_400(monkeypatch):
    from backend.services.api.routers import desk

    wrapper = _FakeRedisWrapper()
    _patch_redis(monkeypatch, wrapper)
    monkeypatch.setattr(
        desk,
        "_resolve_active_strategy",
        lambda t, u: ({"strategy_id": "28", "mode": "SIMULATION"}, None),
    )
    with pytest.raises(HTTPException) as exc:
        await desk.execute_plan(
            payload={"quantity_overrides": [{"symbol": "X", "side": "BUY", "quantity": 0}]},
            current_user={"tenant_id": "default", "user_id": "1"},
        )
    assert exc.value.status_code == 400
    assert "人工改量载荷不合法" in exc.value.detail
    assert wrapper.client.store == {}  # 载荷不合法在加锁前拒绝


@pytest.mark.unit
def test_quantity_override_wiring_source_guards():
    """接线守卫：改量必须走唯一实现，退出单纪律写入引擎源码（机构口径留痕）。"""
    engine_src = (_BACKEND / "services/simulation/engine.py").read_text(encoding="utf-8")
    assert "def apply_quantity_overrides(" in engine_src
    assert "退出规则单不受改量影响" in engine_src  # run_cycle docstring
    assert "normalize_order_quantity(requested, order.symbol" in engine_src  # 申报单位唯一实现
    assert "不改价格、不改方向、不新增单" in engine_src

    desk_src = (_BACKEND / "services/api/routers/desk.py").read_text(encoding="utf-8")
    assert "def parse_quantity_overrides(" in desk_src
    assert "quantity_overrides=quantity_overrides or None" in desk_src

    sched_src = (_BACKEND / "services/simulation/services/simulation_hosted_scheduler.py").read_text(encoding="utf-8")
    exec_block = sched_src.split("async def execute_simulation_plan_for_active")[1].split("def _parse_started_at")[0]
    assert "quantity_overrides=quantity_overrides" in exec_block
