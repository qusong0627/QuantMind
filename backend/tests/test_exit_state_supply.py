"""T-P2-04b 测试：退出规则状态供给（开仓日 / 高水位 / 持有交易日）。

覆盖：
1. 纯函数：高水位折叠（复用 update_highest_price）、键归一、hw_key 双口径收敛；
2. 真库：resolve_open_dates（lots 最早 → trades 回退）、批量日线 high 与直查逐值一致；
3. 服务集成（假 Redis + 真库）：load_symbol_exit_states 装配、持久化回升只升不降、
   同标的二次建仓丢弃旧高点、无开仓日如实进 missing；
4. 引擎：规则集装载含 trailing（两种键）、run_cycle 真实触发 trailing/time_stop；
5. 源守卫：唯一实现复用与接线锚点。
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest

_BACKEND = Path(__file__).resolve().parents[1]


# ── 纯函数 ──────────────────────────────────────────────────────────


@pytest.mark.unit
def test_compute_high_water_fold_semantics():
    from backend.services.simulation.services.exit_state_service import (
        compute_high_water,
    )

    # 只升不降：bars=[10,12,11] + 当前价 9 → 12；再喂更低序列不变
    assert compute_high_water(None, [10.0, 12.0, 11.0], 9.0) == 12.0
    assert compute_high_water(12.0, [8.0, 9.0], 9.0) == 12.0
    # 当前价计入（盘中新高）
    assert compute_high_water(10.0, None, 10.5) == 10.5
    # 脏值忽略；全空 → 0（调用方转为 None 语义）
    assert compute_high_water(None, [0.0, -3.0, None], 0.0) == 0.0  # type: ignore[list-item]
    assert compute_high_water(None, [], None) == 0.0


@pytest.mark.unit
def test_symbol_and_key_normalization():
    from backend.services.simulation.services.exit_state_service import (
        _norm_symbol,
        hw_key,
    )

    assert _norm_symbol("SH600983") == "600983.SH"  # 账户键双形态归一
    assert _norm_symbol("600036.SH") == "600036.SH"
    assert _norm_symbol(" sz000001 ") == "000001.SZ"
    # 引擎（"1"）与 reset（"00000001"）两口径收敛同一键；市场维度入键
    assert hw_key("default", "1", "CN") == hw_key("default", "00000001", "cn")
    assert hw_key("default", "1", "HK").endswith(":HK")


# ── 假 Redis（hash 存储）────────────────────────────────────────────


class _FakeRawRedisHash:
    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str]] = {}

    def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.hashes.get(key, {}))

    def hset(self, key: str, mapping: dict[str, str]) -> int:
        bucket = self.hashes.setdefault(key, {})
        bucket.update(mapping)
        return len(mapping)

    def delete(self, key: str) -> int:
        return 1 if self.hashes.pop(key, None) is not None else 0


class _FakeRedisWrapper:
    def __init__(self) -> None:
        self.client = _FakeRawRedisHash()


async def _ensure_db_pool():
    from sqlalchemy import text as _t

    from backend.shared.database_manager_v2 import close_database, get_session

    try:
        async with get_session(read_only=True) as probe:
            await probe.execute(_t("SELECT 1"))
        return
    except Exception:  # noqa: BLE001
        await close_database()
    async with get_session(read_only=True) as probe:
        await probe.execute(_t("SELECT 1"))


# ── 真库：开仓日解析 ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_open_dates_lots_then_trades_real_db():
    """lots 最早未平行 → trades 最早 BUY 回退；归一后缀式键。"""
    import uuid as _uuid

    await _ensure_db_pool()
    from sqlalchemy import text as sa_text

    from backend.services.simulation.services.exit_state_service import (
        resolve_open_dates,
    )
    from backend.shared.database_manager_v2 import close_database, get_session

    user = 990700 + (_uuid.uuid4().int % 1000)
    _trade_order_id = _uuid.uuid4()
    d1, d2 = date(2026, 8, 1), date(2026, 8, 20)
    try:
        async with get_session(read_only=False) as session:
            # 两只 lot（不同开仓日）→ 取最早；status 字段只约束 remaining>0
            for d in (d1, d2):
                await session.execute(
                    sa_text(
                        "INSERT INTO simulation_position_lots "
                        "(tenant_id, user_id, account_id, symbol, position_side, open_date, "
                        " quantity_open, quantity_remaining, cost_price, cost_amount, status) "
                        "VALUES ('default', :u, :acct, '600036.SH', 'long', :d, 100, 100, 10, 1000, 'open')"
                    ),
                    {"u": str(user), "d": d, "acct": f"e2e:{user}"},
                )
            # 无 lots 的标的：trades 回退（side 枚举小写 buy；order_id 有 FK → 先建单）
            await session.execute(
                sa_text(
                    "INSERT INTO sim_orders (order_id, tenant_id, user_id, portfolio_id, "
                    " symbol, side, order_type, trading_mode, status, quantity, price) "
                    "VALUES (:oid, 'default', :u, 0, '000001.SZ', 'buy', 'limit', "
                    " 'SIMULATION', 'filled', 100, 10)"
                ),
                {"u": int(user), "oid": _trade_order_id},
            )
            await session.execute(
                sa_text(
                    "INSERT INTO sim_trades (trade_id, order_id, tenant_id, user_id, portfolio_id, "
                    " symbol, side, trading_mode, quantity, price, executed_at) "
                    "VALUES (gen_random_uuid(), :oid, 'default', :u, 0, "
                    " '000001.SZ', 'buy', 'SIMULATION', 100, 10, :ts)"
                ),
                {"u": int(user), "oid": _trade_order_id, "ts": datetime(2026, 8, 5, 10, 0, 0)},
            )
        got = await resolve_open_dates("default", str(user), ["SH600036", "000001.SZ", "999999.SZ"])
        assert got.get("600036.SH") == d1  # 最早 lot 胜
        assert got.get("000001.SZ") == date(2026, 8, 5)  # trades 回退
        assert "999999.SZ" not in got  # 无来源 → 缺省（不猜）
    finally:
        async with get_session(read_only=False) as session:
            await session.execute(
                sa_text("DELETE FROM simulation_position_lots WHERE tenant_id='default' AND CAST(user_id AS varchar)=:u"),
                {"u": str(user)},
            )
            await session.execute(
                sa_text("DELETE FROM sim_trades WHERE tenant_id='default' AND CAST(user_id AS varchar)=:u"),
                {"u": str(user)},
            )
            await session.execute(
                sa_text("DELETE FROM sim_orders WHERE tenant_id='default' AND CAST(user_id AS varchar)=:u"),
                {"u": str(user)},
            )
        await close_database()


@pytest.mark.asyncio
async def test_daily_highs_batch_matches_direct_query_real_db():
    """批量日线 high（不复权、按开仓日分窗）与 hub 直查逐值一致。"""
    await _ensure_db_pool()
    from backend.services.simulation.services.exit_state_service import (
        load_daily_highs_batch,
    )
    from backend.services.simulation.services.local_market_data import LocalMarketData
    from backend.services.simulation.services.market_rules import Market, normalize_market
    from backend.shared.backtest_health import load_index_closes_between  # noqa: F401 (导入连通性)
    from backend.shared.database_manager_v2 import close_database

    open_d = date(2026, 8, 1)
    as_of = date(2026, 9, 12)
    try:
        got = load_daily_highs_batch("CN", {"600036.SH": open_d}, as_of)
        assert got.get("600036.SH"), "真库应有 600036 的日线 high"
        hub = LocalMarketData._resolve_hub(normalize_market("CN"))
        direct = hub.fetch_daily_kline_batch(["600036.SH"], open_d, as_of, adjust="none")
        direct_highs = [
            float(r.high)
            for r in direct.itertuples(index=False)
            if str(r.symbol) == "600036.SH"
            and (r.trade_date.date() if hasattr(r.trade_date, "date") else r.trade_date) >= open_d
        ]
        assert sorted(got["600036.SH"]) == sorted(direct_highs), "批量与直查必须逐值一致"
        # 早于开仓日的 high 不得混入
        early = load_daily_highs_batch("CN", {"600036.SH": date(2026, 9, 1)}, as_of)
        assert max(early["600036.SH"]) <= max(got["600036.SH"])
    finally:
        await close_database()


# ── 服务集成（假 Redis + 真库）─────────────────────────────────────


@pytest.mark.asyncio
async def test_load_symbol_exit_states_assembly_and_store(monkeypatch):
    """装配：有 lot 的标的拿到 open_date/hold_days/high_water；无来源进 missing；
    持久化只升不降；同标的二次建仓（open_date 晚于持久化日期）丢弃旧高点。"""
    import uuid as _uuid

    await _ensure_db_pool()
    from sqlalchemy import text as sa_text

    from backend.services.simulation.services import exit_state_service as svc
    from backend.shared.database_manager_v2 import close_database, get_session

    user = 990800 + (_uuid.uuid4().int % 1000)
    open_d = date(2026, 9, 1)
    try:
        async with get_session(read_only=False) as session:
            await session.execute(
                sa_text(
                    "INSERT INTO simulation_position_lots "
                    "(tenant_id, user_id, account_id, symbol, position_side, open_date, "
                    " quantity_open, quantity_remaining, cost_price, cost_amount, status) "
                    "VALUES ('default', :u, :acct, '600036.SH', 'long', :d, 100, 100, 10, 1000, 'open')"
                ),
                {"u": str(user), "d": open_d, "acct": f"e2e:{user}"},
            )
        fake = _FakeRedisWrapper()
        positions = {"SH600036": {"volume": 100, "cost": 30.0}, "300649.SZ": {"volume": 200, "cost": 20.0}}
        prices = {"600036.SH": 32.0, "300649.SZ": 21.0}
        states, missing = await svc.load_symbol_exit_states(
            redis_like=fake,
            tenant_id="default",
            user_id=str(user),
            market="CN",
            positions=positions,
            last_prices=prices,
            as_of=date(2026, 9, 12),
        )
        st = states["600036.SH"]
        assert st.open_date == open_d
        assert st.hold_days is not None and st.hold_days >= 5  # 交易日历
        assert st.high_water is not None and st.high_water >= 32.0  # ≥ 当前价
        # 无开仓日来源 → 如实 missing，high_water 仍按当前价维护（优于 entry 近似）
        assert "300649.SZ" in missing
        assert states["300649.SZ"].hold_days is None
        assert states["300649.SZ"].high_water >= 21.0

        # 持久化：当前价回落时水位不回退（只升不降）
        key = svc.hw_key("default", str(user), "CN")
        assert key in fake.client.hashes
        states2, _ = await svc.load_symbol_exit_states(
            redis_like=fake,
            tenant_id="default",
            user_id=str(user),
            market="CN",
            positions={"SH600036": {"volume": 100, "cost": 30.0}},
            last_prices={"600036.SH": 10.0},
            as_of=date(2026, 9, 13),
        )
        assert states2["600036.SH"].high_water >= st.high_water

        # 同标的二次建仓：open_date 晚于持久化日期 → 丢弃旧高点（不继承）
        fake.client.hashes[key]["600036.SH"] = '{"hw": 999.0, "d": "2026-08-01"}'
        states3, _ = await svc.load_symbol_exit_states(
            redis_like=fake,
            tenant_id="default",
            user_id=str(user),
            market="CN",
            positions={"600036.SH": {"volume": 100, "cost": 30.0}},
            last_prices={"600036.SH": 32.0},
            as_of=date(2026, 9, 13),
        )
        assert states3["600036.SH"].high_water < 999.0, "旧高点必须被丢弃"

        # 清理哈希：reset 口径（user 双形态同键）
        cleared = svc.clear_high_water(fake, "default", f"0000{user}", "CN")
        assert cleared == 1
        assert key not in fake.client.hashes
    finally:
        async with get_session(read_only=False) as session:
            await session.execute(
                sa_text("DELETE FROM simulation_position_lots WHERE tenant_id='default' AND CAST(user_id AS varchar)=:u"),
                {"u": str(user)},
            )
        await close_database()


# ── 规则集装载与引擎全链 ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_exit_ruleset_loader_reads_trailing(monkeypatch):
    """装载器读 trailing（策略短键与 sltp 长键两种口径）。"""
    from backend.services.simulation.engine import SimulationEngine

    engine = SimulationEngine.__new__(SimulationEngine)

    class _Storage:
        def __init__(self, cfg):
            self.cfg = cfg

        async def get(self, strategy_id, user_id):
            return {"parameters": {"execution_config": self.cfg}}

    import backend.services.simulation.engine as eng_mod

    for cfg, expect_trail in (
        ({"trailing_stop": 0.12}, 0.12),
        ({"trailing_stop_pct": -0.08}, 0.08),
        ({"stop_loss": -0.05, "max_hold_days": 10}, None),
    ):
        monkeypatch.setattr(eng_mod, "get_strategy_storage_service", lambda c=cfg: _Storage(c))
        rules = await engine._load_exit_ruleset("99", "1")
        if expect_trail is None:
            assert rules.trailing_stop_pct is None
            assert rules.hard_stop_pct == 0.05 and rules.max_hold_days == 10
        else:
            assert rules.trailing_stop_pct == pytest.approx(expect_trail)


@pytest.mark.asyncio
async def test_engine_run_cycle_trailing_and_time_stop_real_rules(monkeypatch):
    """引擎全链：状态供给（桩）→ 真实规则判定 → trailing/time_stop 退出单。"""
    from backend.services.simulation.engine import SimulationEngine
    from backend.services.simulation.services.rebalance_calculator import StrategyConfig
    from backend.services.simulation.services.signal_loader import SignalScore

    # 提供一条信号让整轮成立（既有设计：无信号日整轮早退，含退出评估——体检 C01 兜底）
    signals: list[Any] = [
        SignalScore(
            symbol="000001",
            score=0.5,
            trade_date=date(2026, 9, 16),
            run_id="r",
            tenant_id="default",
            user_id="1",
        )
    ]

    class _Loader:
        async def load_latest_signals(self, **kw):
            return list(signals)

    class _Bar:
        close = 9.0
        limit_up = 11.0
        limit_down = 8.1
        suspended = False
        pre_close = 9.0

    engine = SimulationEngine(loader=_Loader())

    async def _cfg(db, strategy_id, user_id, params_override=None, market=None):
        return StrategyConfig(topk=1, lot_size=100)

    async def _acct(user_id, tenant_id="default", market="CN"):
        return {
            "cash": 1000.0,
            "total_asset": 1000.0,
            "positions": {"600519.SH": {"volume": 100, "cost": 10.0, "available_volume": 100}},
        }

    async def _bars(symbols, as_of=None, market=None):
        return {s: _Bar() for s in symbols}

    async def _none(a, b):
        return None

    from backend.shared.exit_rules import ExitRuleSet as _ERS

    async def _rules(strategy_id, uid):
        return _ERS(hard_stop_pct=None, take_profit_pct=None, trailing_stop_pct=0.05, max_hold_days=5)

    monkeypatch.setattr(engine, "_load_strategy_config", _cfg)
    monkeypatch.setattr(engine.account_manager, "get_account", _acct)
    monkeypatch.setattr(engine, "_load_bars", _bars)
    monkeypatch.setattr(engine, "_load_exit_ruleset", _rules)
    monkeypatch.setattr(engine, "_apply_risk_buy_locks", lambda orders, **kw: orders)

    # 状态供给桩：成本 10、现价 9（-10%）、高水位 12（回撤 25% > 5% 阈值）、持有 7 日 > 5
    async def _stub_states(**kwargs):
        from backend.services.simulation.services.exit_state_service import SymbolExitState

        return (
            {
                "600519.SH": SymbolExitState(
                    symbol="600519.SH",
                    open_date=date(2026, 9, 1),
                    high_water=12.0,
                    hold_days=7,
                    source="lots/trades+bars",
                )
            },
            [],
        )

    import backend.services.simulation.services.exit_state_service as st_mod

    monkeypatch.setattr(st_mod, "load_symbol_exit_states", _stub_states)

    report = await engine.run_cycle(
        tenant_id="default", user_id="1", strategy_id="99", dry_run=True
    )
    exits = [o for o in report.planned_orders if o["kind"] == "exit"]
    assert exits, f"应触发退出（trailing 优先），实际 {report.planned_orders}"
    assert "[trailing_stop]" in exits[0]["reason"]
    assert "最高 12" in exits[0]["reason"]  # 高水位真实参与

    # 只配 max_hold_days：同一持仓按时间止损退出
    async def _rules_time(strategy_id, uid):
        return _ERS(max_hold_days=5)

    monkeypatch.setattr(engine, "_load_exit_ruleset", _rules_time)
    report2 = await engine.run_cycle(
        tenant_id="default", user_id="1", strategy_id="99", dry_run=True
    )
    exits2 = [o for o in report2.planned_orders if o["kind"] == "exit"]
    assert exits2 and "[time_stop]" in exits2[0]["reason"]
    assert "持有 7 日" in exits2[0]["reason"]


# ── 源守卫 ──────────────────────────────────────────────────────────


@pytest.mark.unit
def test_tp204b_wiring_source_guards():
    svc = (
        _BACKEND / "services/simulation/services/exit_state_service.py"
    ).read_text(encoding="utf-8")
    # 唯一实现复用（不得重造折叠/日历）
    assert "update_highest_price" in svc
    assert "trading_days_between" in svc
    assert "adjust=\"none\"" in svc  # 不复权（cost 为成交原价）

    engine = (_BACKEND / "services/simulation/engine.py").read_text(encoding="utf-8")
    assert "load_symbol_exit_states(" in engine
    assert "hold_days=(st.hold_days if st is not None else None)" in engine
    assert "trailing_stop_pct=abs(float(trail)) if trail else None" in engine
    assert "无开仓日历史" in engine  # 缺省点名（不静默）

    router = (
        _BACKEND / "services/simulation/routers/simulation.py"
    ).read_text(encoding="utf-8")
    assert router.count("clear_high_water(") >= 2  # reset + OCR 同步
