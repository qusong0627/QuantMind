"""委托列表补股票名称（用户反馈「交易记录写的很简单啊、名称都没有」）。

根因（2026-09-20 实测）：`orders.symbol_name` 列存在但 199 行里 114 行为 NULL，
通达信桥的真实写入路径（tdx_push_service 的裸 INSERT）**根本不写这一列**；
而 `/api/v1/orders/` 是 `response_model=list[OrderResponse]`，不做补全 →
前端「股票」列只剩 `600176.SH` 这种代码。

补名必须**不改 ORM 实例**：`symbol_name` 在 `orders` 表上是**真实映射列**，
在 GET 里 `o.symbol_name = …` 会把持久化对象标脏 → 会话收尾 flush 时写出 UPDATE，
读接口顺手写库（只读副本上直接报错）。所以补在 Pydantic 响应对象上。
"""

from __future__ import annotations

import uuid
from datetime import datetime

import pytest

from backend.services.trade_shared.schemas.order import OrderResponse


def _order_row(*, symbol: str, symbol_name: str | None = None) -> OrderResponse:
    """构造一条真实形态的委托响应（列缺省值对齐 orders 表 DDL）。"""
    now = datetime(2026, 9, 20, 14, 30, 0)
    return OrderResponse(
        id=1,
        order_id=uuid.uuid4(),
        tenant_id="default",
        user_id=10000001,
        portfolio_id=0,
        strategy_id=None,
        symbol=symbol,
        symbol_name=symbol_name,
        side="buy",
        order_type="limit",
        quantity=500,
        price=18.21,
        stop_price=None,
        trade_action="open",
        position_side="long",
        is_margin_trade=False,
        client_order_id=None,
        remarks="通达信桥委托",
        trading_mode="REAL",
        status="filled",
        filled_quantity=500,
        average_price=18.18,
        order_value=9105.0,
        filled_value=9090.0,
        commission=5.02,
        submitted_at=now,
        filled_at=now,
        cancelled_at=None,
        expired_at=None,
        exchange_order_id="tdx-320385",
        created_at=now,
        updated_at=now,
    )


@pytest.mark.unit
def test_fills_name_for_suffix_symbol():
    """后缀式（orders 表真实口径）必须补出中文名。"""
    from backend.services.trade.routers.trading_orders import fill_order_names

    row = _order_row(symbol="600036.SH")
    fill_order_names([row], resolver=lambda s: {"600036.SH": "招商银行"}.get(s, ""))

    assert row.symbol_name == "招商银行"


@pytest.mark.unit
def test_resolver_receives_raw_symbol_untouched():
    """代码原样交给 resolver——归一（前缀/后缀/裸码）是 resolver 的责任，不是调用方的。

    实盘 symbol 是后缀式、模拟是前缀式，补名点若自己 `.upper()` 或拼后缀，
    就会在另一层口径上静默查空。锁定「不加工」这条契约。
    """
    from backend.services.trade.routers.trading_orders import fill_order_names

    seen: list[str] = []

    def spy(symbol: str) -> str:
        seen.append(symbol)
        return "招商银行"

    rows = [_order_row(symbol="600036.SH"), _order_row(symbol="SH600036")]
    fill_order_names(rows, resolver=spy)

    assert seen == ["600036.SH", "SH600036"]
    assert all(r.symbol_name == "招商银行" for r in rows)


@pytest.mark.unit
def test_missing_name_stays_none_so_frontend_can_fall_back():
    """查不到 = None（不是空串、不是代码），前端据此回落显示代码。"""
    from backend.services.trade.routers.trading_orders import fill_order_names

    row = _order_row(symbol="920950.BJ")  # 北交所：名称表不收录
    fill_order_names([row], resolver=lambda s: "")

    assert row.symbol_name is None


@pytest.mark.unit
def test_does_not_overwrite_existing_name():
    """库里已写好的名字优先级最高（人工修正/历史正确数据不能被覆盖）。"""
    from backend.services.trade.routers.trading_orders import fill_order_names

    row = _order_row(symbol="600036.SH", symbol_name="招商银行(已核对)")
    fill_order_names([row], resolver=lambda s: "错误的名字")

    assert row.symbol_name == "招商银行(已核对)"


@pytest.mark.unit
def test_uses_real_resolver_for_real_symbols():
    """接真 resolver（本地映射表）跑一遍后缀式代码——空表环境下如实为 None，不抛错。"""
    from backend.shared.stock_name_mapper import resolve_name
    from backend.services.trade.routers.trading_orders import fill_order_names

    row = _order_row(symbol="600036.SH")
    fill_order_names([row], resolver=resolve_name)

    # 表里有就补上、没有就 None，两种都算通过；关键是不得抛异常、不得塞代码
    assert row.symbol_name in (None, "招商银行")


@pytest.mark.unit
def test_empty_list_is_noop():
    from backend.services.trade.routers.trading_orders import fill_order_names

    assert fill_order_names([], resolver=lambda s: "x") == []
