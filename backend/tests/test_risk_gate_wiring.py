"""风控接入测试（T-RC-02）：上下文构建 / 影子-强制-故障三态 / 唯一入口接线 / 全撤。

口径：影子=判定留痕不拦单；强制=REJECT/HALT 拒单；配置不可读/判定异常=fail-closed 拒单。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.services.trade.services import risk_gate_service as rgs


class FakeRedis:
    """最小 Redis 假体（记录 xadd/hincrby/hset；支持 pipeline 链）。"""

    def __init__(self, config: dict | None = None, *, fail_hgetall: bool = False):
        self.config = {**(config or {})}
        self.fail_hgetall = fail_hgetall
        self.xadds: list[tuple] = []
        self.hincr: dict[str, int] = {}
        self.hset_calls: list[dict] = []

    def hgetall(self, key):
        if self.fail_hgetall:
            raise ConnectionError("redis down")
        return dict(self.config) if key == rgs.CONFIG_KEY else {}

    def hset(self, key, mapping=None, **kw):
        self.hset_calls.append(dict(mapping or {}))
        self.config.update(mapping or {})

    def hincrby(self, key, field, n=1):
        self.hincr[field] = self.hincr.get(field, 0) + int(n)

    def xadd(self, key, fields, maxlen=None, approximate=None):
        self.xadds.append((key, dict(fields)))

    def expire(self, key, ttl):
        pass

    def pipeline(self, transaction=False):
        return self

    def execute(self):
        return []


def _req(**over) -> SimpleNamespace:
    base = {
        "tenant_id": "default", "user_id": 1, "symbol": "600036.SH", "side": "buy",
        "quantity": 100, "order_type": "limit", "price": 40.0, "source": "manual",
        "client_order_id": "", "strategy_id": None, "portfolio_id": 0, "remarks": None,
        "position_side": "long", "is_margin_trade": False, "bar": None, "run_id": "",
        "mirror": False, "mirror_source": "", "strict_market": None,
    }
    base.update(over)
    return SimpleNamespace(**base)


def _cfg(**over) -> dict:
    import json

    base = {"enabled": "true", "shadow": "true", "version": "1",
            "rules": json.dumps(rgs.DEFAULT_RULES, ensure_ascii=False)}
    base.update(over)
    return base


# ── 上下文构建 ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_build_context_fields(monkeypatch):
    monkeypatch.setattr(
        rgs, "_quote_snapshot",
        lambda sym: {"Now": "40.60", "timestamp": "9999999999"},  # 未来戳 → age 为负，仅验字段
    )
    monkeypatch.setattr(
        "backend.services.live_trading.services.real_mirror_service.kill_switch_on",
        lambda redis: False,
    )

    class _Mgr:
        def __init__(self, redis):
            pass

        async def get_account(self, uid, tenant, market="CN"):
            return {
                "cash": 12345.0,
                "total_asset": 100000.0,
                "positions": {"SH600036": {"available_volume": 300, "market_value": 12180.0}},
            }

    monkeypatch.setattr(
        "backend.services.trade_shared.simulation_manager.SimulationAccountManager", _Mgr
    )
    ctx = await rgs.build_context(
        _req(order_type="market", price=None, remarks="sltp:600036"), db=None, redis=FakeRedis()
    )
    assert ctx.side == "BUY" and ctx.order_type == "MARKET"
    assert ctx.forced_exit is True                      # sltp: 前缀识别
    assert ctx.amount == pytest.approx(40.60 * 100)     # 市价单按最新价估额
    assert ctx.available_cash == 12345.0
    assert ctx.sellable_volume == 300                   # 前缀式持仓键容错命中
    assert ctx.position_pct == pytest.approx(12180.0 / 100000.0)
    assert ctx.last_price == pytest.approx(40.60)
    assert ctx.kill_switch is False
    # 持仓键为后缀式也要能命中
    class _Mgr2(_Mgr):
        async def get_account(self, uid, tenant, market="CN"):
            return {"cash": 1.0, "total_asset": 2.0,
                    "positions": {"600036.SH": {"available_volume": 7}}}

    monkeypatch.setattr(
        "backend.services.trade_shared.simulation_manager.SimulationAccountManager", _Mgr2
    )
    ctx2 = await rgs.build_context(_req(), db=None, redis=FakeRedis())
    assert ctx2.sellable_volume == 7


# ── 三态：未配置 / 影子 / 强制 / 故障 ────────────────────────────────


@pytest.mark.asyncio
async def test_check_order_unconfigured_passes():
    redis = FakeRedis(config={})
    check = await rgs.check_order(_req(), db=None, redis=redis)
    assert check.passed and not check.enforced
    assert redis.xadds and redis.xadds[-1][1]["verdict"] == "disabled"


@pytest.mark.asyncio
async def test_check_order_shadow_records_but_passes():
    redis = FakeRedis(config=_cfg())

    async def _ctx(req, *, db, redis, need_counts=False):
        from backend.shared.risk import RiskContext

        return RiskContext(market="CN", symbol="600036.SH", side="BUY", quantity=100,
                           now_ts=0.0, kill_switch=True)   # 急停 → HALT 判定

    import backend.services.trade.services.risk_gate_service as mod
    monkey = pytest.MonkeyPatch()
    monkey.setattr(mod, "build_context", _ctx)
    try:
        check = await rgs.check_order(_req(), db=None, redis=redis)
    finally:
        monkey.undo()
    assert check.passed and not check.enforced          # 影子不拦
    rec = redis.xadds[-1][1]
    assert rec["verdict"] == "halt" and rec["enforced"] == "false"
    assert redis.hincr.get("halted") == 1


@pytest.mark.asyncio
async def test_check_order_enforce_rejects(monkeypatch):
    redis = FakeRedis(config=_cfg(shadow="false"))

    async def _ctx(req, *, db, redis, need_counts=False):
        from backend.shared.risk import RiskContext

        return RiskContext(market="CN", symbol="600036.SH", side="BUY", quantity=100,
                           now_ts=0.0, kill_switch=True)

    import backend.services.trade.services.risk_gate_service as mod
    monkeypatch.setattr(mod, "build_context", _ctx)
    check = await rgs.check_order(_req(), db=None, redis=redis)
    assert not check.passed and check.enforced
    assert check.rule_id == "l0.kill_switch"
    assert redis.xadds[-1][1]["enforced"] == "true"


@pytest.mark.asyncio
async def test_check_order_fail_closed_paths(monkeypatch):
    # ① 配置不可读 → 拒
    check = await rgs.check_order(_req(), db=None, redis=FakeRedis(fail_hgetall=True))
    assert not check.passed and check.rule_id == "l0.config"

    # ② 判定异常 → 拒
    import backend.services.trade.services.risk_gate_service as mod
    monkeypatch.setattr(
        mod, "build_context",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    check2 = await rgs.check_order(_req(), db=None, redis=FakeRedis(config=_cfg()))
    assert not check2.passed and check2.rule_id == "l0.evaluate"


# ── 唯一入口接线 ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_submit_order_blocked_by_gate(monkeypatch):
    from backend.services.simulation.services import order_router as orouter

    called = {"submitted": False}

    async def _fake_immediate(db, manager, req):
        called["submitted"] = True
        return orouter.RouterOutcome(success=True)

    async def _fake_check(req, *, db, redis):
        return rgs.RiskCheck(passed=False, enforced=True, rule_id="l3.lot_size", reason="买入数量非整手")

    monkeypatch.setattr(orouter, "_submit_immediate", _fake_immediate)
    monkeypatch.setattr(rgs, "check_order", _fake_check)
    # submit_order 内部按名导入 check_order —— 直接打补丁到其导入源
    import backend.services.trade.services.risk_gate_service as mod
    monkeypatch.setattr(mod, "check_order", _fake_check)

    out = await orouter.submit_order(None, FakeRedis(), _req())
    assert not out.success and "风控拒单" in out.message and "l3.lot_size" in out.message
    assert called["submitted"] is False                  # 拒单在链前，无任何建单副作用


@pytest.mark.asyncio
async def test_submit_order_passes_through_when_gate_ok(monkeypatch):
    from backend.services.simulation.services import order_router as orouter

    async def _fake_immediate(db, manager, req):
        return orouter.RouterOutcome(success=True, order_id="o1")

    monkeypatch.setattr(orouter, "_submit_immediate", _fake_immediate)
    import backend.services.trade.services.risk_gate_service as mod
    monkeypatch.setattr(mod, "check_order", lambda req, *, db, redis: _ok())

    async def _ok():
        return rgs.RiskCheck(passed=True)

    out = await orouter.submit_order(None, FakeRedis(), _req())
    assert out.success and out.order_id == "o1"


# ── 全撤 cancel_all ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_all_counts_and_isolates_failures(monkeypatch):
    from backend.services.simulation.services import order_service as osvc
    from backend.services.simulation.services import order_router as orouter

    class _Order:
        def __init__(self, oid):
            self.order_id = oid

    class _Svc:
        def __init__(self, db):
            pass

        async def list_orders(self, tenant_id, user_id, *, status=None, limit=50):
            return [_Order("o1"), _Order("o2")] if status == "pending" else [_Order("o3")]

        async def cancel_order(self, order, reason=None):
            if order.order_id == "o2":
                raise ValueError("Cannot cancel order in status: filled")
            return order

    monkeypatch.setattr(osvc, "SimOrderService", _Svc)
    result = await orouter.cancel_all(None, FakeRedis(), tenant_id="default", user_id=1)
    assert result["cancelled"] == 2 and result["failed"] == 1
    assert result["errors"][0]["order_id"] == "o2"


@pytest.mark.unit
def test_single_chokepoint_source_guard():
    """G：下单唯一入口必须恰好一处调用风控卡点（防旁路回潮）。"""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    src = (root / "backend/services/simulation/services/order_router.py").read_text(encoding="utf-8")
    assert src.count("_risk_check(req, db=db, redis=redis)") == 1
    assert "risk_gate_service" in src
