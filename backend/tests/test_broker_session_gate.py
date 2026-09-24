"""真单时段闸门：盘外不下单（2026-09-24 补）。

**背景**：闸门此前只装在 ``TdxPushService.place_order``（滚动单 / L2 主单 /
L2 在途重挂三条路的汇合点），而 ``broker_client`` 的五个 A 股真券商通道
**一道闸都没有**。它们走的是同一座 Windows 桥（TDX 客户端 / QMT Agent），
失败形态与那处注释里写明的完全一样：盘外提交的委托要么被柜台拒，要么被客户端
**挂成次日单**——一笔"现在"的决定变成明天的无主委托（价格、仓位、风控前提
全部过期）。而经 ``TradingEngine.submit_order`` 的每条真单路径
（``POST /orders/``、内部策略派发、止损执行器、执行流消费者、真单镜像尾段）
都汇到这五个通道里的某一个。

本文件钉三件事：

1. **盘外一律拒，且拒在传输层之前**——用"走到传输层才算数"的探头证明，
   不是断言某句文案：文案也会被"另一个更早的早退分支"命中，那时用例是假绿的。
2. **时段内照样下发**（反向对照）：防把闸门做成"永远拒"，那是另一种事故。
3. **纸面券商不设闸**（防闸门过宽）：``SIMULATION`` 的盘后演练正需要它。

时段事实必须**注入**（``monkeypatch.setattr(broker_client,
"real_order_session_refusal", ...)``）：不注入的话本文件白天绿、收盘后红，
测的是墙上钟而不是被测代码（``test_broker_plan_id_uniqueness.py`` 里
``tdx_pusher`` 那条用例的注释已经踩过同一个坑）。
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from typing import Any

import pytest

from backend.services.live_trading.services import broker_client, trading_session
from backend.services.live_trading.services.trading_session import (
    OUT_OF_SESSION_MESSAGE,
)

#: 统一的下单参数（各券商的签名取交集后的最小集）
_BASE_KWARGS: dict[str, Any] = {
    "user_id": 1,
    "symbol": "600036.SH",
    "side": "SELL",
    "quantity": 100,
    "order_type": "LIMIT",
    "price": 10.0,
    "tenant_id": "default",
}


class _Response:
    status_code = 200
    text = "{}"

    def json(self) -> dict:
        # 五个通道的解析器各取所需（TDX 看 orders[0].status、QMT-Bridge 看 ok、
        # QMTBroker 看 success/order_id）——一个信封同时喂饱它们，省掉五份夹具。
        return {
            "ok": True,
            "success": True,
            "status": "executed",
            "order_id": "EX-1",
            "orders": [{"status": "submitted", "order_id": "EX-1", "message": "已派发"}],
        }


class _Transport:
    """传输层探头：**被碰到就记一笔**。

    这是本文件的判据核心——"盘外没下发"必须由"传输层一次都没被碰"来证明。
    只看返回值会假绿：任何一个更早的校验早退（未配置、缺 client_order_id…）
    同样返回 success=False。
    """

    #: QmtExecBroker 构造时会读这个属性
    configured = True

    def __init__(self) -> None:
        self.calls = 0

    async def post(self, *_a: Any, **_k: Any) -> _Response:
        self.calls += 1
        return _Response()

    async def get(self, *_a: Any, **_k: Any) -> _Response:
        self.calls += 1
        return _Response()

    def xadd(self, *_a: Any, **_k: Any) -> str:
        self.calls += 1
        return "1-1"

    async def submit_order(self, **_k: Any) -> dict:
        self.calls += 1
        return {"order_id": "EX-1"}


def _tdx() -> tuple[Any, _Transport, dict]:
    broker = broker_client.TdxBroker(
        bridge_url="http://bridge.invalid:8550", bridge_token="tok"
    )
    probe = _Transport()
    broker._client = probe  # 不建真实 httpx 连接、不触网
    return broker, probe, {}


def _qmt() -> tuple[Any, _Transport, dict]:
    broker = broker_client.QMTBroker(qmt_host="127.0.0.1", qmt_port=1)
    probe = _Transport()
    broker._session = probe
    return broker, probe, {}


def _qmt_bridge() -> tuple[Any, _Transport, dict]:
    broker = broker_client.QMTBridgeBroker(stream_base_url="http://stream.invalid:8003")
    probe = _Transport()
    broker._session = probe
    # 桥模式强制要求幂等键（缺了它同样是 success=False 的早退 —— 正是本用例
    # 要与之区分的那种"假绿"）
    return broker, probe, {"client_order_id": "cid-bridge-1"}


def _redis() -> tuple[Any, _Transport, dict]:
    broker = broker_client.RedisBroker(
        redis_host="127.0.0.1", redis_port=1, redis_password=""
    )
    probe = _Transport()
    broker._redis = probe  # StrictRedis 是懒连接，这里直接换掉
    return broker, probe, {"client_order_id": "cid-redis-1"}


def _qmt_exec() -> tuple[Any, _Transport, dict]:
    probe = _Transport()  # 自带 configured=True，构造时那次自检走的就是它
    # client 走形参注入：QmtExecBroker.__init__ 只在 client 为 None 时才去读
    # 全局单例（那会把用例连到本机的 QMT 配置上）。
    broker = broker_client.QmtExecBroker(client=probe)
    return broker, probe, {"client_order_id": "cid-exec-1"}


REAL_BROKERS = [
    ("TdxBroker", _tdx),
    ("QMTBroker", _qmt),
    ("QMTBridgeBroker", _qmt_bridge),
    ("RedisBroker", _redis),
    ("QmtExecBroker", _qmt_exec),
]


def _place(broker: Any, extra: dict) -> Any:
    return asyncio.run(broker.place_order(**{**_BASE_KWARGS, **extra}))


def _in_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """打在**谓词所在模块**上：闸门在调用时才查这个名字，所以它跟着一起被接管。

    这也让用例不必认识闸门函数叫什么——换个闸门实现照样有效。
    """
    monkeypatch.setattr(trading_session, "is_trading_time", lambda now=None: True)


def _out_of_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(trading_session, "is_trading_time", lambda now=None: False)


@pytest.mark.parametrize(("name", "make"), REAL_BROKERS, ids=[n for n, _ in REAL_BROKERS])
def test_out_of_session_is_refused_before_the_transport(
    monkeypatch: pytest.MonkeyPatch, name: str, make: Any
) -> None:
    """盘外：拒单 + **传输层一次都没被碰**。

    "没被碰"是这条用例的全部价值：盘外下单最坏的形态不是失败，是"看起来成功了
    而委托躺在客户端里等明天开盘"。
    """
    _out_of_session(monkeypatch)
    broker, probe, extra = make()

    result = _place(broker, extra)

    assert probe.calls == 0, (
        f"{name} 盘外仍走到了传输层（{probe.calls} 次）——"
        "委托会被客户端挂成次日单，而调用方以为只是「已提交」"
    )
    assert result.success is False, f"{name} 盘外下单被报成了成功"
    assert result.message == OUT_OF_SESSION_MESSAGE, (
        f"{name} 的拒因不是统一的时段文案：{result.message!r}——"
        "用户从不同入口下的单会看到不同原因"
    )


@pytest.mark.parametrize(("name", "make"), REAL_BROKERS, ids=[n for n, _ in REAL_BROKERS])
def test_in_session_still_reaches_the_transport(
    monkeypatch: pytest.MonkeyPatch, name: str, make: Any
) -> None:
    """反向对照：时段内必须真的下发。

    没有这条，上面那条用「永远拒」也能全绿——而"永远拒"会让所有真单静默失效，
    比原来的缺口更坏。
    """
    _in_session(monkeypatch)
    broker, probe, extra = make()

    result = _place(broker, extra)

    assert probe.calls == 1, (
        f"{name} 时段内没有走到传输层——闸门被做成了「永远拒」（或更早的校验挡住了）"
    )
    assert result.success is True, f"{name} 时段内下发失败：{result.message}"


def test_paper_broker_is_not_gated(monkeypatch: pytest.MonkeyPatch) -> None:
    """纸面券商不受此闸：``SIMULATION`` 的盘后演练正需要它。

    闸门挂在券商（而不是 ``TradingEngine``）就是为了保住这条——按引擎的模式判会
    连演练一起拦掉。判据是"闸门函数一次都没被调用"：直接把闸门换成会炸的替身，
    比断言返回值更硬（返回值还可能被别的早退分支命中）。
    """
    _out_of_session(monkeypatch)

    def _boom() -> Any:
        raise AssertionError("纸面券商不该走真单时段闸门")

    monkeypatch.setattr(broker_client, "_session_refusal", _boom)
    broker = broker_client.PaperTradingBroker.__new__(broker_client.PaperTradingBroker)

    async def _no_quote(_symbol: str) -> Any:
        # 行情取不到 → place_order 走它自己的拒单分支（真实行为，不伪造成交）
        return broker_client.MarketQuoteSnapshot(price=0.0)

    monkeypatch.setattr(broker, "_get_market_snapshot", _no_quote)
    result = asyncio.run(broker.place_order(**_BASE_KWARGS))

    assert "无法获取" in result.message, (
        f"纸面券商的拒单分支没被走到，本用例没有真的进 place_order：{result.message!r}"
    )


# ---------------------------------------------------------------------------
# 结构面：新增真券商不得再漏装闸门
# ---------------------------------------------------------------------------

SOURCE = Path(broker_client.__file__).read_text(encoding="utf-8")

#: 不该有闸的两个类：抽象基类（无实现）+ 纸面券商（本地撮合，见上）
_EXEMPT = {"BaseBroker", "PaperTradingBroker"}


def _classes_with_place_order() -> dict[str, ast.ClassDef]:
    tree = ast.parse(SOURCE)
    out: dict[str, ast.ClassDef] = {}
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and any(
            isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef)
            and item.name == "place_order"
            for item in node.body
        ):
            out[node.name] = node
    return out


def _calls_session_refusal(node: ast.AST) -> bool:
    return any(
        isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id == "_session_refusal"
        for call in ast.walk(node)
    )


def test_every_real_broker_place_order_has_the_gate() -> None:
    """枚举而不是样板：新增第 6 个真券商时，这条会点名要它装闸。

    用 AST 而非正则：正则会被注释/字符串里的同名字样骗过（本文件上面就有一堆）。
    """
    offenders = sorted(
        name
        for name, node in _classes_with_place_order().items()
        if name not in _EXEMPT and not _calls_session_refusal(node)
    )
    assert not offenders, (
        f"这些券商通道的 place_order 没有真单时段闸门：{offenders}——"
        "盘外提交的委托会被客户端挂成次日单"
    )


def test_the_exempt_list_has_not_silently_grown() -> None:
    """豁免面必须**看得见**：只有抽象基类与纸面券商。

    防的是"新加一个真券商，顺手把它塞进 _EXEMPT 让上面那条变绿"。
    """
    assert set(_classes_with_place_order()) - _EXEMPT == {
        "TdxBroker",
        "QMTBroker",
        "QMTBridgeBroker",
        "RedisBroker",
        "QmtExecBroker",
    }, "券商清单变了：新增/移除真券商必须一并更新本清单与上面的用例矩阵"


def test_refusal_text_lives_in_exactly_one_place() -> None:
    """拒因文案单源：两个物理出口各写一份必然漂移。

    （``TdxPushService`` 与五个券商通道共用 ``trading_session`` 的那一句。）
    """
    live_dir = Path(broker_client.__file__).parent
    holders = [
        path.name
        for path in sorted(live_dir.rglob("*.py"))
        if OUT_OF_SESSION_MESSAGE in path.read_text(encoding="utf-8")
    ]
    assert holders == ["trading_session.py"], (
        f"时段拒因文案出现在多处：{holders}——"
        "用户在委托备注里看到的原因会取决于他从哪条路进来"
    )
