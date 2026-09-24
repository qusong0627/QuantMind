"""契约：券商拒单必须报成失败，不得报成「已提交」（2026-09-24 核实）。

``TradingEngine.submit_order`` 有**两条**把订单置为 ``REJECTED`` 的路径，此前都
无条件返回 ``success: True``：

  1. ``_execute_via_broker`` 收到 ``result.success=False`` —— 券商**不抛异常**直接拒，
     与 2026-09-21 那 42 笔越界价废单同一条路：置 REJECTED、发「订单被拒绝」通知；
  2. ``_execute_via_broker`` 内部的 ``except Exception``（下单执行崩溃）：
     置 REJECTED、发「订单执行失败」通知。

两条都在 ``_execute_via_broker`` 里被吞掉（不 re-raise），外层 ``submit_order``
只按「有没有异常抛到我这里」判成败 —— 于是**拒单被报成 success=True**。

后果放大在止损链上：``sltp_executor`` 的「dispatch 失败 → ST_FAILED + 错误告警」
本身是对的、也有测试（``test_qmt_sltp_executor.py::test_dispatch_failure_marks_failed``），
但信封说 success，它就走不到那条分支 —— 记 ``ST_SUBMITTED``、推「触发卖出已提交」、
当日规则进终态：**不重试、无失败告警，保护是假的**。

判据必须是**终态**（``order.status == REJECTED``）而不是「有没有异常」：超时时
状态未知、单可能已在柜台，那时的 ``success: True`` + ``status="submitted"`` 是
**有意**行为（``_execute_via_broker`` 里「超时绝对不能直接标记为失败」）。本文件
同时钉住它不被误伤 —— 见 ``test_timeout_is_not_reclassified_as_failure``。

本文件用**真的** ``OrderService.transition_order_status``（只把 db/redis 换成桩）：
要钉的正是「终态 REJECTED」这个判据与其备注格式，把状态机也换成假的就等于没测。
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from backend.services.live_trading.services import trading_engine as te
from backend.services.live_trading.services.trading_engine import TradingEngine
from backend.services.trade_shared.models.order import OrderStatus, TradingMode


class _FakeRedis:
    def delete(self, *_args, **_kwargs):
        return None

    def delete_pattern(self, *_args, **_kwargs):
        return None


class _FakeDb:
    async def commit(self):
        return None

    async def refresh(self, *_args, **_kwargs):
        return None


class _Broker:
    """按剧本回执的券商桩：要么给回执，要么抛。"""

    def __init__(self, *, result=None, raises=None):
        self._result = result
        self._raises = raises

    async def place_order(self, **_kwargs):
        if self._raises is not None:
            raise self._raises
        return self._result


def _order(**over) -> SimpleNamespace:
    base = {
        "order_id": uuid4(),
        "tenant_id": "default",
        "user_id": 79311845,
        "portfolio_id": None,
        "symbol": "600036.SH",
        "quantity": 100,
        "price": 90.0,
        "side": SimpleNamespace(value="sell"),
        "order_type": SimpleNamespace(value="limit"),
        "trade_action": None,
        "position_side": None,
        "is_margin_trade": False,
        "trading_mode": TradingMode.REAL,
        "client_order_id": "cid-sltp-1",
        "remarks": "",
        "status": OrderStatus.PENDING,
    }
    base.update(over)
    return SimpleNamespace(**base)


def _engine(monkeypatch: pytest.MonkeyPatch, broker: _Broker) -> TradingEngine:
    engine = TradingEngine(db=_FakeDb(), redis=_FakeRedis())
    monkeypatch.setattr(engine, "_get_stock_broker", lambda *_a, **_k: broker)
    return engine


@pytest.fixture
def notices(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """接管通知出口（`_safe_schedule_notification` 走 asyncio.create_task）。"""
    mock = AsyncMock()
    monkeypatch.setattr(te, "publish_notification_async", mock)
    return mock


@pytest.mark.asyncio
async def test_broker_reject_is_reported_as_failure(
    monkeypatch: pytest.MonkeyPatch, notices: AsyncMock
) -> None:
    """券商不抛异常直接拒（=越界价废单的形态）→ success=False / status="rejected"。"""
    engine = _engine(
        monkeypatch,
        _Broker(
            result=SimpleNamespace(
                success=False,
                message="废单：委托价格超出涨跌幅限制",
                exchange_order_id=None,
                filled_quantity=0,
                filled_price=0,
            )
        ),
    )
    order = _order()

    result = await engine.submit_order(order, tenant_id="default")
    await asyncio.sleep(0)  # 通知是 create_task 起的，让出一次让它跑完

    assert order.status == OrderStatus.REJECTED, "前提：拒单必须落终态（状态机为真）"
    assert result["success"] is False, (
        "券商拒单被报成了提交成功 —— 止损链会记 ST_SUBMITTED 且当日不再重试、无告警"
    )
    assert result["status"] == "rejected"
    assert "废单" in result["message"], "拒因必须能自查：运维看不到券商原话就只能猜"
    assert "[REJECTED: Broker拒绝: 废单" in order.remarks, "拒因须随订单留痕"
    titles = [c.kwargs.get("title") for c in notices.await_args_list]
    assert "订单被拒绝" in titles, "拒单通知此前就有，修契约时不得弄丢"


@pytest.mark.asyncio
async def test_broker_exception_is_reported_as_failure(
    monkeypatch: pytest.MonkeyPatch, notices: AsyncMock
) -> None:
    """第二条路径：下单执行崩溃被 `_execute_via_broker` 吞掉并置 REJECTED，同样不得报成功。"""
    engine = _engine(monkeypatch, _Broker(raises=RuntimeError("柜台连接中断")))
    order = _order()

    result = await engine.submit_order(order, tenant_id="default")
    await asyncio.sleep(0)

    assert order.status == OrderStatus.REJECTED
    assert result["success"] is False, "执行异常路径同样被外层报成了成功"
    assert result["status"] == "rejected"
    assert "柜台连接中断" in result["message"]
    titles = [c.kwargs.get("title") for c in notices.await_args_list]
    assert "订单执行失败" in titles


@pytest.mark.asyncio
async def test_bridge_ack_still_reports_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """不误伤：桥模式「已派发待回报」仍是成功（`success=True` / `submitted`）。

    这条同时是反向对照：同样是「没成交」，拒单与待回报必须给出**相反**的答案，
    否则修法就退化成了「只要没立即成交就算失败」。
    """
    engine = _engine(
        monkeypatch,
        _Broker(
            result=SimpleNamespace(
                success=True,
                message="已派发",
                exchange_order_id="EX-1",
                filled_quantity=0,
                filled_price=0,
                commission=0,
            )
        ),
    )
    monkeypatch.setattr(engine, "_sync_account_to_redis", AsyncMock())  # 非本用例主体
    order = _order()

    result = await engine.submit_order(order, tenant_id="default")

    assert result["success"] is True
    assert result["status"] == "submitted"
    assert "[AWAITING_BRIDGE_ACK]" in order.remarks, "待回报标记是超时扫描器的兜底依据"


@pytest.mark.asyncio
async def test_timeout_is_not_reclassified_as_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """不误伤：超时时单可能已在柜台（状态未知），契约层保持 success=True / submitted。

    判别依据只能是终态 REJECTED，**不能**是「执行时出过异常」—— 超时就是一个异常，
    但把它判成失败会让一笔可能已成交的卖单被重试（超卖）。
    """
    engine = _engine(monkeypatch, _Broker(raises=asyncio.TimeoutError()))
    monkeypatch.setattr(engine, "_sync_account_to_redis", AsyncMock())
    order = _order()

    result = await engine.submit_order(order, tenant_id="default")

    assert result["success"] is True
    assert result["status"] == "submitted"
    assert "[BRIDGE_ACK_TIMEOUT_PENDING_REVIEW]" in order.remarks
