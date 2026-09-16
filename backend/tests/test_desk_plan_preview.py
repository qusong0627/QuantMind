"""T-FE-05 后端测试：调仓计划预演（dry-run）。

覆盖：
1. **dry-run 语义**（真 RebalanceCalculator 单实现）：产出计划条目（reason/kind/预估金额），
   但绝不执行——不撮合、不落单、不写快照（_sync_snapshot 不被调用）；
2. 退出规则卖单与调仓单同源合并（kind=exit / rebalance 分类正确）；
3. desk `_collect_plan` 无活跃策略时的如实降级形态；
4. 接线源守卫（desk→preview 入口、engine 的 dry_run 分支位置在撮合之前）。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest

_BACKEND = Path(__file__).resolve().parents[1]


class _FakeBar:
    def __init__(self, close: float, limit_up: float = 0.0, limit_down: float = 0.0) -> None:
        self.close = close
        self.limit_up = limit_up
        self.limit_down = limit_down
        self.suspended = False
        self.pre_close = close * 0.99


class _FakeLoader:
    def __init__(self, signals: list[Any]) -> None:
        self._signals = signals

    async def load_latest_signals(self, **kwargs: Any) -> list[Any]:
        return list(self._signals)


def _signals() -> list[Any]:
    from backend.services.simulation.services.signal_loader import SignalScore

    # P0 回归：信号表 symbol 为**纯数字**（DB 契约）——引擎边界必须归一为后缀式，
    # 否则与行情键失配被全部过滤（托管周期自 9/13 起静默零订单的真实场景）
    return [
        SignalScore(
            symbol="600036", score=0.9, trade_date=date(2026, 9, 16),
            run_id="r1", tenant_id="default", user_id="1",
        ),
        SignalScore(
            symbol="000001", score=0.8, trade_date=date(2026, 9, 16),
            run_id="r1", tenant_id="default", user_id="1",
        ),
    ]


@pytest.mark.asyncio
async def test_engine_dry_run_plans_without_side_effects(monkeypatch):
    from backend.services.simulation.engine import SimulationEngine
    from backend.services.simulation.services.rebalance_calculator import StrategyConfig

    engine = SimulationEngine(loader=_FakeLoader(_signals()))

    async def _fake_strategy_config(db, strategy_id, user_id, params_override=None, market=None):
        return StrategyConfig(topk=2, lot_size=100)

    async def _fake_account(user_id, tenant_id="default", market="CN"):
        return {"cash": 100000.0, "total_asset": 100000.0, "positions": {}}

    async def _fake_bars(symbols, as_of=None, market=None):
        return {sym: _FakeBar(close=10.0, limit_up=11.0, limit_down=9.0) for sym in symbols}

    async def _no_exit_rules(strategy_id, user_id):
        return None

    sync_calls: list[tuple] = []

    async def _sync_spy(tenant_id, user_id, market=None):
        sync_calls.append((tenant_id, user_id))

    monkeypatch.setattr(engine, "_load_strategy_config", _fake_strategy_config)
    monkeypatch.setattr(engine.account_manager, "get_account", _fake_account)
    monkeypatch.setattr(engine, "_load_bars", _fake_bars)
    monkeypatch.setattr(engine, "_load_exit_ruleset", _no_exit_rules)
    monkeypatch.setattr(engine, "_apply_risk_buy_locks", lambda orders, **kw: orders)
    monkeypatch.setattr(engine, "_sync_snapshot", _sync_spy)

    report = await engine.run_cycle(
        tenant_id="default", user_id="1", strategy_id="99", dry_run=True
    )

    # 计划产出（同源计算：两张等权买入）
    assert report.dry_run is True
    assert report.error is None
    assert report.signal_count == 2
    assert report.order_count == len(report.planned_orders) == 2
    for item in report.planned_orders:
        assert item["side"] == "BUY" and item["quantity"] % 100 == 0
        assert item["reason"], "计划条目必须携带理由"
        assert item["kind"] == "rebalance"
        assert item["estimated_amount"] > 0
    # 纯数字信号在引擎边界已归一为后缀式（与行情/账户/撮合同口径）
    assert {i["symbol"] for i in report.planned_orders} == {"600036.SH", "000001.SZ"}

    # 零副作用：不撮合（orders 为空）、不落账、不写快照
    assert report.orders == []
    assert report.filled_count == 0
    assert sync_calls == []


@pytest.mark.asyncio
async def test_engine_dry_run_includes_exit_orders(monkeypatch):
    from backend.services.simulation.engine import SimulationEngine
    from backend.services.simulation.services.rebalance_calculator import StrategyConfig

    engine = SimulationEngine(loader=_FakeLoader(_signals()))

    async def _fake_strategy_config(db, strategy_id, user_id, params_override=None, market=None):
        return StrategyConfig(topk=2, lot_size=100)

    async def _fake_account(user_id, tenant_id="default", market="CN"):
        # 持仓一只已跌出目标（不在信号里的 688596.SH）→ 退出规则产生卖单
        return {
            "cash": 90000.0,
            "total_asset": 100000.0,
            "positions": {
                "688596.SH": {"volume": 300, "available_volume": 300, "cost": 70.0, "price": 60.0, "market_value": 18000.0}
            },
        }

    async def _fake_bars(symbols, as_of=None, market=None):
        return {sym: _FakeBar(close=60.0 if sym.startswith("688") else 10.0, limit_up=66.0, limit_down=54.0) for sym in symbols}

    def _fake_build_account(data):
        from backend.services.simulation.services.rebalance_calculator import SimulationAccount

        positions = data.get("positions") or {}
        return SimulationAccount(
            cash=float(data.get("cash") or 0),
            total_asset=float(data.get("total_asset") or 0),
            # 退出评估依赖点位市值/成本；原引擎 _build_account 会做同款归一
            positions={
                sym: {
                    **pos,
                    "market_price": pos.get("price"),
                    "price": pos.get("price"),
                }
                for sym, pos in positions.items()
            },
        )

    from backend.shared.exit_rules import ExitRuleSet

    async def _exit_rules(strategy_id, user_id):
        return ExitRuleSet(hard_stop_pct=0.05, take_profit_pct=None, max_hold_days=None)

    monkeypatch.setattr(engine, "_load_strategy_config", _fake_strategy_config)
    monkeypatch.setattr(engine.account_manager, "get_account", _fake_account)
    monkeypatch.setattr(engine, "_load_bars", _fake_bars)
    monkeypatch.setattr(engine, "_build_account", _fake_build_account)
    monkeypatch.setattr(engine, "_load_exit_ruleset", _exit_rules)
    monkeypatch.setattr(engine, "_apply_risk_buy_locks", lambda orders, **kw: orders)

    report = await engine.run_cycle(
        tenant_id="default", user_id="1", strategy_id="99", dry_run=True
    )

    kinds = [item["kind"] for item in report.planned_orders]
    assert "exit" in kinds, f"退出规则卖单应进入计划（kind=exit），实际 {kinds}"
    exit_items = [i for i in report.planned_orders if i["kind"] == "exit"]
    assert exit_items[0]["side"] == "SELL"
    assert exit_items[0]["reason"]
    # 计划顺序 = 先退出后调仓（与执行顺序一致）
    first_exit_idx = kinds.index("exit")
    first_rebalance_idx = kinds.index("rebalance") if "rebalance" in kinds else len(kinds)
    assert first_exit_idx < first_rebalance_idx


@pytest.mark.asyncio
async def test_desk_collect_plan_without_active_strategy():
    """无活跃策略 → 如实降级（不伪造计划）。"""
    try:
        from backend.services.api.routers.desk import _collect_plan
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"依赖不可用: {exc}")

    block = await _collect_plan("default", "00000001")
    # 本机当前无活跃策略；若未来有，则至少应返回可用结构（两种形态都不允许异常）
    assert "available" in block and "source" in block
    if not block["available"]:
        assert "无活跃策略" in block["reason"] or "读取失败" in block["reason"] or "预演失败" in block["reason"]


@pytest.mark.unit
def test_plan_preview_wiring_source_guards():
    engine_src = (_BACKEND / "services/simulation/engine.py").read_text(encoding="utf-8")
    # dry-run 分支必须在撮合循环之前（防未来重构把它挪到执行后面）
    dry_idx = engine_src.index("if dry_run:")
    exec_idx = engine_src.index("exec_engine = SimulationExecutionEngine")
    assert dry_idx < exec_idx, "dry_run 分支必须位于撮合之前"
    assert "report.planned_orders" in engine_src
    assert "def _plan_to_dict" in engine_src

    sched_src = (_BACKEND / "services/simulation/services/simulation_hosted_scheduler.py").read_text(encoding="utf-8")
    assert "async def preview_simulation_plan_for_active" in sched_src
    assert "dry_run=True" in sched_src

    desk_src = (_BACKEND / "services/api/routers/desk.py").read_text(encoding="utf-8")
    assert "preview_simulation_plan_for_active" in desk_src
    assert '"plan": plan_block' in desk_src
    assert "plan: bool = Query(True" in desk_src  # 可跳过（快速加载）

    # P0 守卫（当日实机修复的两处静默断链，防回归）：
    # ① DatabaseManager 无 session() API——全仓不得再出现该调用
    import subprocess

    hits = subprocess.run(
        ["grep", "-rn", "--include=*.py", "db_manager.session()", str(_BACKEND)],
        capture_output=True, text=True,
    ).stdout
    prod_hits = [
        line
        for line in hits.splitlines()
        if "/tests/" not in line and "test_" not in line.split(":")[0]
    ]
    assert prod_hits == [], f"db_manager.session() 非法 API 复活:\n" + "\n".join(prod_hits)
    # ② 引擎默认 redis 必须引用共享单例（新建未连接实例会让账户/风控/快照全链路拿不到数据）
    assert "redis or redis_client" in engine_src
    assert "from backend.services.trade_shared.redis_client import RedisClient, redis_client" in engine_src
    # ③ CN 信号裸码在引擎边界归一（行情键/账户/撮合同口径）
    assert "StockCodeUtil.to_suffix(_sig.symbol)" in engine_src
