"""风控接入测试（T-RC-02）：上下文构建 / 影子-强制-故障三态 / 唯一入口接线 / 全撤。

口径：影子=判定留痕不拦单；强制=REJECT/HALT 拒单；配置不可读/判定异常=fail-closed 拒单。
"""

from __future__ import annotations

import json
import logging
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
        "tenant_id": "default",
        "user_id": 1,
        "symbol": "600036.SH",
        "side": "buy",
        "quantity": 100,
        "order_type": "limit",
        "price": 40.0,
        "source": "manual",
        "client_order_id": "",
        "strategy_id": None,
        "portfolio_id": 0,
        "remarks": None,
        "position_side": "long",
        "is_margin_trade": False,
        "bar": None,
        "run_id": "",
        "mirror": False,
        "mirror_source": "",
        "strict_market": None,
    }
    base.update(over)
    return SimpleNamespace(**base)


def _cfg(**over) -> dict:
    import json

    base = {
        "enabled": "true",
        "shadow": "true",
        "version": "1",
        "rules": json.dumps(rgs.DEFAULT_RULES, ensure_ascii=False),
    }
    base.update(over)
    return base


# ── 上下文构建 ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_build_context_fields(monkeypatch):
    monkeypatch.setattr(
        rgs,
        "_quote_snapshot",
        lambda sym: {
            "Now": "40.60",
            "timestamp": "9999999999",
        },  # 未来戳 → age 为负，仅验字段
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
                "positions": {
                    "SH600036": {"available_volume": 300, "market_value": 12180.0}
                },
            }

    monkeypatch.setattr(
        "backend.services.trade_shared.simulation_manager.SimulationAccountManager",
        _Mgr,
    )
    ctx = await rgs.build_context(
        _req(order_type="market", price=None, remarks="sltp:600036"),
        db=None,
        redis=FakeRedis(),
    )
    assert ctx.side == "BUY" and ctx.order_type == "MARKET"
    assert ctx.forced_exit is True  # sltp: 前缀识别
    assert ctx.amount == pytest.approx(40.60 * 100)  # 市价单按最新价估额
    assert ctx.available_cash == 12345.0
    assert ctx.sellable_volume == 300  # 前缀式持仓键容错命中
    assert ctx.position_pct == pytest.approx(12180.0 / 100000.0)
    assert ctx.last_price == pytest.approx(40.60)
    assert ctx.kill_switch is False

    # 持仓键为后缀式也要能命中
    class _Mgr2(_Mgr):
        async def get_account(self, uid, tenant, market="CN"):
            return {
                "cash": 1.0,
                "total_asset": 2.0,
                "positions": {"600036.SH": {"available_volume": 7}},
            }

    monkeypatch.setattr(
        "backend.services.trade_shared.simulation_manager.SimulationAccountManager",
        _Mgr2,
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

        return RiskContext(
            market="CN",
            symbol="600036.SH",
            side="BUY",
            quantity=100,
            now_ts=0.0,
            kill_switch=True,
        )  # 急停 → HALT 判定

    import backend.services.trade.services.risk_gate_service as mod

    monkey = pytest.MonkeyPatch()
    monkey.setattr(mod, "build_context", _ctx)
    try:
        check = await rgs.check_order(_req(), db=None, redis=redis)
    finally:
        monkey.undo()
    assert check.passed and not check.enforced  # 影子不拦
    rec = redis.xadds[-1][1]
    assert rec["verdict"] == "halt" and rec["enforced"] == "false"
    assert redis.hincr.get("halted") == 1


@pytest.mark.asyncio
async def test_check_order_enforce_rejects(monkeypatch):
    redis = FakeRedis(config=_cfg(shadow="false"))

    async def _ctx(req, *, db, redis, need_counts=False):
        from backend.shared.risk import RiskContext

        return RiskContext(
            market="CN",
            symbol="600036.SH",
            side="BUY",
            quantity=100,
            now_ts=0.0,
            kill_switch=True,
        )

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
        mod,
        "build_context",
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
        return rgs.RiskCheck(
            passed=False, enforced=True, rule_id="l3.lot_size", reason="买入数量非整手"
        )

    monkeypatch.setattr(orouter, "_submit_immediate", _fake_immediate)
    monkeypatch.setattr(rgs, "check_order", _fake_check)
    # submit_order 内部按名导入 check_order —— 直接打补丁到其导入源
    import backend.services.trade.services.risk_gate_service as mod

    monkeypatch.setattr(mod, "check_order", _fake_check)

    out = await orouter.submit_order(None, FakeRedis(), _req())
    assert (
        not out.success and "风控拒单" in out.message and "l3.lot_size" in out.message
    )
    assert called["submitted"] is False  # 拒单在链前，无任何建单副作用


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
            return (
                [_Order("o1"), _Order("o2")] if status == "pending" else [_Order("o3")]
            )

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
    src = (root / "backend/services/simulation/services/order_router.py").read_text(
        encoding="utf-8"
    )
    assert src.count("_risk_check(req, db=db, redis=redis)") == 1
    assert "risk_gate_service" in src


# ── T-RC-02b：直连路径（TDX 滚动/L2）接线 + 盘后入队语义（影子实测修复）──────


class _FakeSessionCtx:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *a):
        return False


@pytest.mark.asyncio
async def test_check_direct_order_builds_real_req_and_shadow_pass(monkeypatch):
    captured = {}

    async def _fake_check(req, *, db, redis):
        captured["req"] = req
        return rgs.RiskCheck(passed=True, enforced=False, version=1)

    monkeypatch.setattr(rgs, "check_order", _fake_check)
    monkeypatch.setattr(
        "backend.shared.database_manager_v2.get_session",
        lambda read_only=True: _FakeSessionCtx(),
    )
    check = await rgs.check_direct_order(
        tenant_id="default",
        user_id="10000001",
        symbol="600036.SH",
        side="sell",
        quantity=100,
        price=None,
        order_type="market",
        source="tdx_rolling",
        remarks="rolling_x_600036.SH_sell",
        redis_client=FakeRedis(),
    )
    assert check.passed
    req = captured["req"]
    assert isinstance(req, rgs.DirectOrderReq)
    assert req.trading_mode == "REAL" and req.source == "tdx_rolling"
    assert req.user_id == 10000001 and req.side == "sell"
    assert req.remarks == "rolling_x_600036.SH_sell"


@pytest.mark.unit
def test_l0_session_queued_intent_downgrades():
    from datetime import datetime

    from backend.shared.risk import RiskContext
    from backend.shared.risk.builtin_rules import l0_session

    ts = datetime(2026, 9, 18, 15, 34, tzinfo=rgs.CST).timestamp()  # 交易日盘后
    queued = l0_session(RiskContext(side="BUY", queued_intent=True, now_ts=ts), {})
    assert queued is not None and queued.action == "WARN"
    plain = l0_session(RiskContext(side="BUY", queued_intent=False, now_ts=ts), {})
    assert plain is not None and plain.action == "REJECT"


@pytest.mark.unit
def test_l3_stale_quote_queued_intent_downgrades():
    from backend.shared.risk import RiskContext
    from backend.shared.risk.builtin_rules import l3_stale_quote

    # 时刻不可得 + 有市场价 + 入队 → 告警（注意用 last_price 而非委托限价 price）
    queued = l3_stale_quote(
        RiskContext(
            quote_age_s=None,
            last_price=7.5,
            price=None,
            queued_intent=True,
            price_source="fallback_close",
        ),
        {},
    )
    assert queued is not None and queued.action == "WARN"
    # 陈旧但已知 age（现场实测：盘后快照 age≈87min）+ 入队 → 同样降级告警
    stale_queued = l3_stale_quote(
        RiskContext(
            quote_age_s=5220.0,
            last_price=7.57,
            queued_intent=True,
            price_source="snapshot",
        ),
        {},
    )
    assert stale_queued is not None and stale_queued.action == "WARN"
    # 非入队 + 陈旧 → 拒
    strict = l3_stale_quote(
        RiskContext(quote_age_s=5220.0, last_price=7.5, queued_intent=False), {}
    )
    assert strict is not None and strict.action == "REJECT"
    none_price = l3_stale_quote(
        RiskContext(quote_age_s=None, last_price=None, queued_intent=True), {}
    )
    assert none_price is not None and none_price.action == "REJECT"


@pytest.mark.asyncio
async def test_build_context_after_hours_fallback_price_and_queued(monkeypatch):
    """盘后入队：快照缺失 → 最近收盘兜底价 + queued_intent（时段/时效校验延后）。"""
    from datetime import datetime as _dt

    class _FakeDT(_dt):
        @classmethod
        def now(cls, tz=None):
            base = _dt(2026, 9, 18, 15, 34, tzinfo=rgs.CST)
            return base.astimezone(tz) if tz else base.replace(tzinfo=None)

    monkeypatch.setattr(rgs, "datetime", _FakeDT)
    monkeypatch.setattr(rgs, "_quote_snapshot", lambda sym: {})  # 盘后快照缺失
    monkeypatch.setattr(rgs, "_last_close_fallback", lambda sym: 7.57)
    monkeypatch.setattr(
        "backend.services.live_trading.services.real_mirror_service.kill_switch_on",
        lambda redis: False,
    )

    class _Mgr:
        def __init__(self, redis):
            pass

        async def get_account(self, uid, tenant, market="CN"):
            return None

    monkeypatch.setattr(
        "backend.services.trade_shared.simulation_manager.SimulationAccountManager",
        _Mgr,
    )
    ctx = await rgs.build_context(
        _req(order_type="market", price=None, source="co_pilot"),
        db=None,
        redis=FakeRedis(),
    )
    assert ctx.queued_intent is True
    assert ctx.price_source == "fallback_close"
    assert ctx.last_price == pytest.approx(7.57)
    assert ctx.amount == pytest.approx(7.57 * 100)
    assert ctx.quote_age_s is None


@pytest.mark.unit
def test_direct_paths_source_guard():
    """G：TDX 直连下单路径必须过闸（滚动/L2 共用 place_rolling_orders + L2 重挂各一处）。"""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    rolling = (
        root / "backend/services/live_trading/services/tdx_rolling_trade_service.py"
    ).read_text(encoding="utf-8")
    l2 = (root / "backend/services/live_trading/services/tdx_l2_realtime.py").read_text(
        encoding="utf-8"
    )
    assert "check_direct_order" in rolling
    assert "check_direct_order" in l2


# ── 预检（推送确认面板逐笔跑的那条路）────────────────────────────────


@pytest.mark.asyncio
async def test_preflight_returns_full_verdict_without_any_trace(monkeypatch):
    """预检：判定全貌照给，**留痕一条不写**。

    这是本次推送功能的关键不变式。`check_order` 每次调用都 `hincrby evaluated`，
    若预检复用它，一次「选 10 只点推送」就等于往当日 metrics 灌 10 次判定 ——
    影子报告会显示「今天拦了 10 单」，而那 10 单**一次都没发出去**。
    """
    # Arrange：急停触发 HALT
    redis = FakeRedis(config=_cfg())

    async def _ctx(req, *, db, redis, need_counts=False):
        from backend.shared.risk import RiskContext

        return RiskContext(
            market="CN",
            symbol="600036.SH",
            side="BUY",
            quantity=100,
            now_ts=0.0,
            kill_switch=True,
        )

    import backend.services.trade.services.risk_gate_service as mod

    monkeypatch.setattr(mod, "build_context", _ctx)

    # Act
    verdict = await rgs.preflight_order(_req(), db=None, redis=redis)

    # Assert：裁定可见。**要的是 decisions 全表而不是一条主因** —— 同一笔单会同时踩中
    # 多条（急停 + 时段 + 陈旧行情），确认面板要按规则前缀分组呈现环境闸门与标的级原因。
    assert verdict.verdict == "halt"
    assert verdict.shadow is True  # 影子期：会拦但不拦
    assert verdict.passed is True
    rule_ids = [d["rule_id"] for d in verdict.decisions]
    assert "l0.kill_switch" in rule_ids and len(rule_ids) > 1
    assert all(
        {"rule_id", "level", "action", "reason", "evidence"} <= set(d)
        for d in verdict.decisions
    )
    # 但一条留痕都没有（决策流 + 计数双向为空）
    assert redis.xadds == []
    assert redis.hincr == {}


@pytest.mark.asyncio
async def test_preflight_matches_check_order_verdict(monkeypatch):
    """同数据下预检与真实判定的裁定必须逐字一致 —— 否则「预检说能过、下单被拒」。"""

    # Arrange
    def _ctx_factory():
        async def _ctx(req, *, db, redis, need_counts=False):
            from backend.shared.risk import RiskContext

            return RiskContext(
                market="CN",
                symbol="600036.SH",
                side="BUY",
                quantity=100,
                now_ts=0.0,
                kill_switch=True,
            )

        return _ctx

    import backend.services.trade.services.risk_gate_service as mod

    monkeypatch.setattr(mod, "build_context", _ctx_factory())

    # Act：强制模式（会真拒），两条路各跑一次
    pre_redis = FakeRedis(config=_cfg(shadow="false"))
    pre = await rgs.preflight_order(_req(), db=None, redis=pre_redis)
    real_redis = FakeRedis(config=_cfg(shadow="false"))
    real = await rgs.check_order(_req(), db=None, redis=real_redis)

    # Assert
    assert pre.passed is False and real.passed is False
    assert pre.rule_id == real.rule_id == "l0.kill_switch"
    assert pre.reason == real.reason
    # 真实那条留痕、预检那条不留 —— 差值恰好是一次
    assert real_redis.hincr.get("halted") == 1
    assert pre_redis.hincr == {}


@pytest.mark.asyncio
async def test_preflight_fail_closed_without_trace():
    """配置不可读时预检同样 fail-closed，且不写 errors 计数。"""
    # Arrange
    redis = FakeRedis(fail_hgetall=True)

    # Act
    verdict = await rgs.preflight_order(_req(), db=None, redis=redis)

    # Assert
    assert verdict.passed is False and verdict.rule_id == "l0.config"
    assert redis.xadds == [] and redis.hincr == {}


@pytest.mark.asyncio
async def test_preflight_disabled_gate_passes_quietly():
    """风控未启用：预检放行（与 check_order 的 disabled 分支同判），且不留痕。"""
    # Arrange
    redis = FakeRedis(config={})

    # Act
    verdict = await rgs.preflight_order(_req(), db=None, redis=redis)

    # Assert
    assert verdict.passed is True and verdict.verdict == "disabled"
    assert redis.xadds == [] and redis.hincr == {}


# ── 高频交易阈值护栏（程序化交易报告义务）──────────────────────────────
#
# 撞线不违法，所以**只告警不拦截**：配置照常加载、照常生效。这一组测的就是
# 「加载不被改坏」+「该响的时候响」，两者缺一不可——只测告警会漏掉前者。


def test_order_rate_reaching_hft_warns_but_still_loads(caplog):
    """max_per_minute ≥18000 时告警，但配置原样生效（不改值、不拒绝加载）。"""
    # Arrange
    rules = json.dumps({"l3.order_frequency": {"max_per_minute": 18000}})
    redis = FakeRedis(config={"enabled": "true", "rules": rules, "version": "3"})

    # Act
    with caplog.at_level(logging.WARNING):
        cfg = rgs.load_config(redis)

    # Assert
    assert [r.getMessage() for r in caplog.records if "高频交易" in r.getMessage()]
    assert cfg is not None and cfg.enabled is True and cfg.version == 3
    assert cfg.rules["l3.order_frequency"]["max_per_minute"] == 18000


def test_order_rate_below_hft_is_quiet(caplog):
    """默认 60 笔/分（=1 笔/秒）不该刷告警——天天响的告警等于没有告警。"""
    # Arrange
    rules = json.dumps({"l3.order_frequency": {"max_per_minute": 60}})
    redis = FakeRedis(config={"enabled": "true", "rules": rules})

    # Act
    with caplog.at_level(logging.WARNING):
        rgs.load_config(redis)

    # Assert
    assert not [r for r in caplog.records if "高频交易" in r.getMessage()]


def test_order_rate_rule_absent_is_quiet(caplog):
    """规则没配 order_frequency 时不能报「你可能被认定为高频」。"""
    # Arrange
    redis = FakeRedis(
        config={"enabled": "true", "rules": json.dumps({"l1.available_cash": {}})}
    )

    # Act
    with caplog.at_level(logging.WARNING):
        rgs.load_config(redis)

    # Assert
    assert not [r for r in caplog.records if "高频交易" in r.getMessage()]
