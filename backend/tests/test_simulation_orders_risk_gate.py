"""手动模拟下单端点（POST /api/v1/simulation/orders）的风控卡点。

缺陷（2026-09-21 核实）：同一笔人工委托，**开市与否决定它过不过风控**——

  * 收市提交 → `assess_execution_window` 判不可执行 → `queue_order` 转挂单 →
    次日 `pending_order_worker` 派发时走 `risk_gate_service.check_order`（P0-2 派发复检）✅
  * 开市提交 → 本端点自己手搓了「建单 → 会话判定 → execute_order → apply_filled」，
    唯独**没有**风控这一跳 ❌

本端点是 `OrderRouter`（五路径唯一入口）之外的第六条链，风控卡点（T-RC-02）合入时
漏接。其余路径（托管引擎 / 沙箱 / TDX 滚动 / 保证金平仓 / 推送下单 / 内部策略派发）
都经 Router 或各自通道过了闸。

修复口径与挂单路径**逐字对齐**：闸放在「即将执行」那一刻，而不是提交时——
挂单路径的注释写明「入队时刻的时段/时效约束已按语义降级为告警，此处才是真正的
'申报前闸'——行情按当前 fresh 口径判定」。若把闸提到会话判定之前，一笔本可顺延到
次日成交的单会在提交瞬间被拒，与挂单路径行为分叉。

本文件钉住：开市路径过闸、拒因带规则号（可解释）、**转挂单路径不过闸**（口径对齐）、
放行路径照常成交、闸门 plumbing 抖动不静默放行、端点返回契约（201 + 订单行）不变。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.services.simulation.routers import simulation_orders
from backend.services.simulation.schemas.order import SimOrderCreate

_TENANT = "default"
_USER = "1"
_ORDER_ID = "11111111-1111-1111-1111-111111111111"


class _OrderStub:
    """`SimOrder` 台账行桩：列形状与真实 ORM 一致（字段取值**参与**断言）。

    必须真实：闸门入参是从这行上取字段拼出来的，`getattr` 取空会静默降级成缺省值，
    而 `order.side` 这类直取缺失即抛 → 落进 fail-closed 分支被拒单。桩少给一个列，
    用例就变成「测 fail-closed」而不是「测放行」——实测踩过（`'_OrderStub' object has
    no attribute 'side'`）。故此处照 `services/simulation/models/order.py` 的列逐一给出，
    并且 `side`/`order_type` 用**真枚举**（而非裸字符串），否则钉不住下面那条枚举陷阱。
    """

    def __init__(self, **kw):
        from backend.services.simulation.models.order import (
            OrderSide,
            OrderType,
            TradingMode,
        )

        self.order_id = _ORDER_ID
        self.tenant_id = _TENANT
        self.user_id = 1
        self.symbol = "SH600036"
        self.side = OrderSide.BUY
        self.order_type = OrderType.LIMIT
        # 真实列（`sim_orders.trading_mode`），且是**瘦身版** TradingMode（只剩 SIMULATION）：
        # 桩必须有它，否则「直传 `order.trading_mode`」这种改法会以 AttributeError 落进
        # fail-closed —— 用例照样红，但红在桩缺列上，枚举陷阱本身没被验证到（实测踩过）。
        self.trading_mode = TradingMode.SIMULATION
        self.quantity = 100.0
        self.price = 10.5
        self.status = "submitted"
        self.source = "manual"
        self.remarks = None
        self.client_order_id = "cid-1"
        self.strategy_id = None
        self.__dict__.update(kw)


def _auth():
    from backend.services.trade_shared.deps import AuthContext

    return AuthContext(
        user_id=_USER, tenant_id=_TENANT, raw_sub=_USER, roles=["user"], is_admin=False
    )


class _AsyncCM:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


class _FakeOrderService:
    created: list = []
    queued: list = []

    def __init__(self, db):
        self.db = db
        self.order = _OrderStub(order_id=_ORDER_ID)

    async def create_order(self, tenant_id, user_id, data, **kw):  # noqa: ARG002
        type(self).created.append(data)
        return self.order

    async def queue_order(self, order, message="", trading_session_date=None):
        type(self).queued.append((order, message, trading_session_date))

    async def sync_order_projection(self, order, **kw):  # noqa: ARG002
        return None


class _FakeEngine:
    """类级记录器：handler 自己 `SimulationExecutionEngine(db, mgr)` 造实例，
    测试拿不到那个实例，只能从类上取。"""

    calls: list = []

    def __init__(self, db, manager):  # noqa: ARG002
        pass

    async def assess_execution_window(self, order, now=None):  # noqa: ARG002
        return SimpleNamespace(
            can_execute=True,
            target_trade_date=None,
            final_state=None,
            retryable=False,
            message="ok",
        )

    async def execute_order(self, order, **kw):  # noqa: ARG002
        type(self).calls.append("execute_order")
        return SimpleNamespace(success=True, message="filled", commission=1.0)

    async def mark_rejected(self, order, message):
        type(self).calls.append(("mark_rejected", str(message)))

    async def apply_filled(self, order, result):  # noqa: ARG002
        type(self).calls.append("apply_filled")


class _ClosedEngine(_FakeEngine):
    calls: list = []

    async def assess_execution_window(self, order, now=None):  # noqa: ARG002
        return SimpleNamespace(
            can_execute=False,
            target_trade_date="2026-09-22",
            final_state=None,
            retryable=True,
            message="queued for next valid session",
        )


def _rejections(engine_cls=_FakeEngine):
    return [c for c in engine_cls.calls if isinstance(c, tuple)]


class _Decision:
    def __init__(self, passed, rule_id=None, reason=""):
        self.passed = passed
        self.rule_id = rule_id
        self.reason = reason


@pytest.fixture(autouse=True)
def _reset_recorders():
    _FakeEngine.calls = []
    _ClosedEngine.calls = []
    _FakeOrderService.created = []
    _FakeOrderService.queued = []
    yield


@pytest.fixture
def wire(monkeypatch):
    """装配端点依赖，返回 (risk_mock, order_service)。"""

    def _build(engine_cls=_FakeEngine, decision=None):
        monkeypatch.setattr(simulation_orders, "SimOrderService", _FakeOrderService)
        monkeypatch.setattr(simulation_orders, "SimulationExecutionEngine", engine_cls)
        monkeypatch.setattr(
            simulation_orders.SimulationAccountManager,
            "locked_execution",
            staticmethod(lambda *a, **k: _AsyncCM()),
        )
        risk = AsyncMock(return_value=decision or _Decision(True))
        monkeypatch.setattr(
            "backend.services.trade.services.risk_gate_service.check_order", risk
        )
        return risk

    return _build


def _payload(**kw):
    base = {
        "trading_mode": "SIMULATION",
        "symbol": "SH600036",
        "side": "buy",
        "order_type": "limit",
        "quantity": 100,
        "price": 10.5,
    }
    base.update(kw)
    return SimOrderCreate(**base)


async def _call(payload):
    # db 用 AsyncMock：端点会 await db.commit()/db.refresh()，MagicMock 不可 await
    return await simulation_orders.create_order(
        data=payload, auth=_auth(), db=AsyncMock(), redis=MagicMock()
    )


# ── 1. 开市路径：申报前必须过闸 ──────────────────────────────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_open_session_order_is_risk_checked_before_execution(wire):
    risk = wire()

    await _call(_payload())

    risk.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_gate_receives_normalized_context_not_raw_enums(wire):
    """喂给闸门的必须是**规范化后的标量**，不能是 ORM 上的 str-mixin 枚举。

    `OrderSide`/`OrderType`/`TradingMode` 都是 `(str, Enum)`，且 `_CaseInsensitiveEnum`
    **没有**覆盖 `__str__` —— Python 3.10 下 `str(TradingMode.SIMULATION)` 得到的是
    `'TradingMode.SIMULATION'` 而不是 `'SIMULATION'`（`.value` 才是）。直传枚举时
    `build_context` 的 `str(...).upper()` 会把它拼成 `'TRADINGMODE.SIMULATION'`：
    不报错、不抛异常，只是**静默地**判定为非模拟模式，进而按另一套账户快照取持仓。
    这类错法没有任何症状，只能靠钉住入参形状来防。
    """
    risk = wire()

    await _call(_payload())

    ctx = risk.await_args.args[0]
    # **必须用 `type(...) is str`，不能用 `==`**：`OrderSide` 是 `(str, Enum)`，`str` 子类
    # 直接用 `str.__eq__`，于是 `OrderSide.BUY == "buy"` 恒真 —— 拿 `==` 断言等于什么都没测
    # （实测：把入参换回裸枚举，`==` 版断言全绿通过）。真正的断裂在闸门内部：
    # `build_context` 走 `str(x).strip().upper()`，3.10 下 `str(OrderSide.BUY)` 是
    # `'OrderSide.BUY'` → 归一成 `'ORDERSIDE.BUY'`，静默不匹配。
    # `type(...) is str` 则跨 Python 版本稳定（3.11 改了 mixin 枚举的 `__str__`，
    # 若改用 `str(x).upper()` 断言，3.11+ 会重新变空转）。
    assert type(ctx.side) is str, f"side 是枚举不是标量：{ctx.side!r}"
    assert ctx.side == "buy", f"side 未规范化：{ctx.side!r}"
    assert type(ctx.order_type) is str, f"order_type 是枚举不是标量：{ctx.order_type!r}"
    assert ctx.order_type == "limit", f"order_type 未规范化：{ctx.order_type!r}"
    # 模拟端点的单恒为模拟模式（本端点 schema 也只剩 SIMULATION），须逐字给出字符串。
    assert type(ctx.trading_mode) is str, (
        f"trading_mode 是枚举不是标量：{ctx.trading_mode!r}"
    )
    assert ctx.trading_mode == "SIMULATION", (
        f"trading_mode 未规范化：{ctx.trading_mode!r}"
    )
    # 闸门实际看到的东西（复刻 build_context 的归一），这才是判断依据本身。
    assert str(ctx.side).upper() == "BUY"
    assert str(ctx.trading_mode).upper() == "SIMULATION"
    assert ctx.tenant_id == _TENANT
    assert ctx.symbol == "SH600036"
    assert ctx.quantity == 100.0
    assert ctx.price == 10.5


@pytest.mark.unit
@pytest.mark.asyncio
async def test_risk_rejection_blocks_execution_and_is_recorded(wire):
    """拒单必须**拦住成交**并把拒因落到台账行上（可解释），而不是静默放行。"""
    risk = wire(
        decision=_Decision(False, rule_id="l1.position_limit", reason="单票持仓超限")
    )

    await _call(_payload())

    risk.assert_awaited_once()
    assert "execute_order" not in _FakeEngine.calls, "风控拒单后仍成交了"
    assert "apply_filled" not in _FakeEngine.calls

    rejections = _rejections()
    assert len(rejections) == 1, "拒单未落台账"
    message = rejections[0][1]
    assert "l1.position_limit" in message, "拒因须带规则号，否则用户无法自查"
    assert "单票持仓超限" in message
    assert "风控拒单" in message


@pytest.mark.unit
@pytest.mark.asyncio
async def test_risk_pass_flows_to_execution(wire):
    risk = wire()

    await _call(_payload())

    risk.assert_awaited_once()
    assert "execute_order" in _FakeEngine.calls
    assert "apply_filled" in _FakeEngine.calls
    assert _rejections() == []


# ── 2. 转挂单路径：提交时**不**过闸（与挂单路径逐字对齐） ────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_queued_order_is_not_risk_checked_at_submit(wire):
    """收市转挂单时不过闸——闸在派发环节做（挂单路径的既有口径）。

    若在这里过闸，一笔本可顺延到次日成交的单会在提交瞬间被拒，与挂单路径行为分叉；
    而挂单路径的注释明确写了入队时刻只做语义降级、派发才是「真正的申报前闸」。
    """
    risk = wire(engine_cls=_ClosedEngine)

    await _call(_payload())

    assert len(_FakeOrderService.queued) == 1, "收市单应转挂单"
    risk.assert_not_awaited()
    assert _ClosedEngine.calls == []


# ── 3. 端点契约不变 ─────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_endpoint_still_returns_order_row(wire):
    """返回契约（201 + 订单行）不变——本端点在开源仓是公开 API，形状不能悄悄改。"""
    wire()

    returned = await _call(_payload())

    assert str(getattr(returned, "order_id", "")) == _ORDER_ID


@pytest.mark.unit
def test_non_simulation_mode_is_rejected_by_schema(wire):
    """非模拟模式在**入口 schema** 就被拒（422），handler 里那句 `!= SIMULATION` 到不了。

    仓库里有两个同名 `TradingMode`：
      * `services/trade_shared/models/enums.py` —— REAL/SHADOW/SIMULATION/BACKTEST（全的）；
      * `services/simulation/models/order.py` —— **只剩 SIMULATION**（本 schema 用的是这个）。
    故 `if data.trading_mode != TradingMode.SIMULATION` 是**不可达的冗余防线**（任何非
    SIMULATION 取值在建对象时就抛 ValidationError）。此处钉住真实契约，免得后人以为
    那道 handler 守卫在起作用而删掉 schema 约束。
    """
    import pydantic

    wire()

    with pytest.raises(pydantic.ValidationError):
        _payload(trading_mode="REAL")


# ── 4. 风控 plumbing 抖动不得误杀，也不得静默放行 ───────────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_risk_gate_plumbing_error_does_not_silently_fill(wire):
    """闸门本身抛异常时**不得**静默放行成交。

    与挂单路径的取舍不同是有意的：挂单路径可「推迟本轮」（单子还在，下轮再扫），
    本端点没有重试容器，静默放行 = 一笔未经风控的成交直接落账且无人知晓。
    故按 fail-closed 处理：拒单并留痕。
    """
    risk = wire()
    risk.side_effect = RuntimeError("redis down")

    await _call(_payload())

    assert "execute_order" not in _FakeEngine.calls, "闸门异常时静默放行成交"
    assert len(_rejections()) == 1, "fail-closed 拒单必须留痕"
