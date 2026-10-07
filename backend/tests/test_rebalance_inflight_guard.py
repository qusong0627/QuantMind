"""调仓规划的**在途委托**闸门。

背景（2026-09-21 实测）：托管模拟盘在 09-20 21:30、09-20 22:02、09-21 09:18
跑了三个周期，每个周期都对同一只票下了买单——SH600023 目标 4100 股，三轮
分别挂了 2900 / 4000 / 4100，累计 11000 股。原因：`_generate_orders` 只读
``account.positions``，而前一轮的委托还挂在 ``pending``（盘后不撮合），持仓
自然没变，于是下一轮把**同一笔差额**又算了一遍。

幂等键 ``sim-{run}-{sym}-{side}`` 只保证**同一个 run 重试**不重复落单，跨 run
完全不可见——所以必须在**规划阶段**把在途量算进已承诺仓位。

本文件的基线断言（无在途时行为不变）不能省：扣减逻辑若写成无条件扣，会把
正常调仓也一起吃掉。
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.services.simulation.services.rebalance_calculator import (
    Order,
    Quote,
    RebalanceCalculator,
    aggregate_inflight,
)


@dataclass
class _SimOrder:
    """``SimOrder`` 的最小子集（避免测试依赖 DB 模型）。"""

    symbol: str
    side: str
    quantity: float
    filled_quantity: float = 0.0


def _quote(symbol: str, price: float = 10.0) -> Quote:
    return Quote(symbol=symbol, current_price=price)


def _positions(**volumes: int) -> dict[str, dict[str, object]]:
    return {symbol: {"volume": qty} for symbol, qty in volumes.items()}


CALC = RebalanceCalculator()


def _generate(current, target, *, inflight=None):
    return CALC._generate_orders(
        current_positions=current,
        target_positions=target,
        quotes={s: _quote(s) for s in set(current) | set(target)},
        inflight=inflight,
    )


# ---------------------------------------------------------------------------
# 基线：无在途时行为必须与改前一致（防止扣减逻辑写成无条件扣）
# ---------------------------------------------------------------------------


def test_无在途时照常下单() -> None:
    orders = _generate({}, {"SH600000": 1000})
    assert [(o.side, o.quantity) for o in orders] == [("BUY", 1000)]


def test_无在途时卖单照常() -> None:
    orders = _generate(_positions(SH600000=1000), {})
    assert [(o.side, o.quantity) for o in orders] == [("SELL", 1000)]


# ---------------------------------------------------------------------------
# 在途买单：必须从买入差额里扣掉
# ---------------------------------------------------------------------------


def test_在途买单全额覆盖目标则不再下单() -> None:
    """这就是 3 倍超额建仓的那个形态：目标 1000，第一轮已挂 1000。"""
    orders = _generate({}, {"SH600000": 1000}, inflight={"SH600000": {"BUY": 1000}})
    assert orders == []


def test_在途买单只补差额() -> None:
    orders = _generate({}, {"SH600000": 1000}, inflight={"SH600000": {"BUY": 600}})
    assert [(o.side, o.quantity) for o in orders] == [("BUY", 400)]


def test_在途买单超过目标不产生负数量() -> None:
    """在途 1500 > 目标 1000：只能不下单，绝不能出负数（会变成反向单）。"""
    orders = _generate({}, {"SH600000": 1000}, inflight={"SH600000": {"BUY": 1500}})
    assert orders == []


def test_在途买单与持仓叠加计算() -> None:
    """持仓 300 + 在途 500，目标 1000 → 还差 200。"""
    orders = _generate(
        _positions(SH600000=300),
        {"SH600000": 1000},
        inflight={"SH600000": {"BUY": 500}},
    )
    assert [(o.side, o.quantity) for o in orders] == [("BUY", 200)]


# ---------------------------------------------------------------------------
# 在途卖单：对称地扣
# ---------------------------------------------------------------------------


def test_在途卖单全额覆盖差额则不再下单() -> None:
    orders = _generate(
        _positions(SH600000=1000), {}, inflight={"SH600000": {"SELL": 1000}}
    )
    assert orders == []


def test_在途卖单只补差额() -> None:
    orders = _generate(
        _positions(SH600000=1000), {}, inflight={"SH600000": {"SELL": 400}}
    )
    assert [(o.side, o.quantity) for o in orders] == [("SELL", 600)]


def test_买卖方向互不干扰() -> None:
    """同标的的在途买单不该抵消卖单差额（方向必须分开记账）。"""
    orders = _generate(
        _positions(SH600000=1000), {}, inflight={"SH600000": {"BUY": 1000}}
    )
    assert [(o.side, o.quantity) for o in orders] == [("SELL", 1000)]


def test_在途量不遮挡其他标的() -> None:
    orders = _generate(
        {},
        {"SH600000": 1000, "SZ000001": 500},
        inflight={"SH600000": {"BUY": 1000}},
    )
    assert [(o.symbol, o.side, o.quantity) for o in orders] == [("SZ000001", "BUY", 500)]


def test_在途卖单仍受可卖量钳制() -> None:
    """T+1 钳制不能被在途扣减绕过：可卖 200 时最多卖 200。"""
    current = {"SH600000": {"volume": 1000, "available_volume": 200}}
    orders = _generate(current, {}, inflight={"SH600000": {"SELL": 0}})
    assert [(o.side, o.quantity) for o in orders] == [("SELL", 200)]


# ---------------------------------------------------------------------------
# aggregate_inflight：把订单行汇总成「标的 → 方向 → 未成交量」
# ---------------------------------------------------------------------------


def test_汇总在途量按标的与方向分区() -> None:
    result = aggregate_inflight(
        [
            _SimOrder("SH600000", "buy", 1000),
            _SimOrder("SH600000", "buy", 500),
            _SimOrder("SH600000", "sell", 300),
            _SimOrder("SZ000001", "buy", 200),
        ]
    )
    assert result == {
        "SH600000": {"BUY": 1500, "SELL": 300},
        "SZ000001": {"BUY": 200},
    }


def test_汇总时扣除已成交部分() -> None:
    """部分成交的单只剩未成交那部分还占额度。"""
    result = aggregate_inflight([_SimOrder("SH600000", "buy", 1000, filled_quantity=400)])
    assert result == {"SH600000": {"BUY": 600}}


def test_汇总时已全部成交的订单不占额度() -> None:
    result = aggregate_inflight([_SimOrder("SH600000", "buy", 1000, filled_quantity=1000)])
    assert result == {}


def test_汇总空列表返回空字典() -> None:
    assert aggregate_inflight([]) == {}


def test_汇总忽略零与负数量() -> None:
    result = aggregate_inflight(
        [_SimOrder("SH600000", "buy", 0), _SimOrder("SH600001", "buy", -5)]
    )
    assert result == {}


def test_汇总大小写与空白归一() -> None:
    """``SimOrder.side`` 是枚举，取出来可能是 ``buy`` 也可能是 ``BUY``。"""
    result = aggregate_inflight(
        [_SimOrder("SH600000", "BUY", 100), _SimOrder("SH600000", "  buy ", 100)]
    )
    assert result == {"SH600000": {"BUY": 200}}


# ---------------------------------------------------------------------------
# 接线闸门：扣减逻辑必须是**活的**
# ---------------------------------------------------------------------------
#
# 本仓有"只有定义、无调用方"的前科（见 test_worker_registration_source.py 顶部）。
# 扣减逻辑若没被引擎真的接上，测试全绿而线上照旧重复下单——最坏的一种绿。
import re  # noqa: E402
from pathlib import Path  # noqa: E402

_ENGINE = (
    Path(__file__).resolve().parents[1] / "services" / "simulation" / "engine.py"
)


def test_引擎把在途量接进调仓计算() -> None:
    src = _ENGINE.read_text(encoding="utf-8")
    assert "aggregate_inflight" in src, "引擎未汇总在途委托 —— 扣减逻辑是死代码"
    assert "list_inflight_orders" in src, "引擎未查在途委托"
    # 必须真的传进 calculate(，不能只算不用
    assert re.search(r"calculate\([^)]*inflight\s*=", src, re.S), (
        "算出了在途量却没传给 calculate —— 等于没接"
    )


def test_引擎不按策略过滤在途量() -> None:
    """账户才是被承诺的资源；按 strategy_id 过滤会漏掉别的策略占住的额度。

    这条是**反向**闸门：写法一旦被"顺手加上过滤"改回去，重复下单就复发了。
    """
    src = _ENGINE.read_text(encoding="utf-8")
    call = re.search(r"list_inflight_orders\((.*?)\)", src, re.S)
    assert call is not None, "找不到 list_inflight_orders 调用点"
    assert "strategy_id" not in call.group(1), (
        "在途量被按策略过滤了 —— 别的策略挂的单同样占着这笔钱和这只票"
    )
