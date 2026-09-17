"""副驾驶测试（T-P6-16）：动作校验（U）+ 建议卡→OrderRouter 真执行留痕（I 真机）。

验收口径：
- 一键执行走 OrderRouter（source=co_pilot 落 sim_orders；幂等键 cop-* 防重复下单）；
- 建议卡状态机（pending → executed/failed；重复执行 409；reject 留痕）；
- 上下文端点（/context）块级可用性真实（无 mock）。
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest

_CST = timezone(timedelta(hours=8))


@pytest.mark.unit
def test_validate_actions_rules():
    from backend.services.api.routers.copilot import AdviceAction, validate_actions

    ok = validate_actions([
        AdviceAction(symbol="600036", side="BUY", quantity=100),
        AdviceAction(symbol="SH600000", side="sell", quantity=200, order_type="limit", price=10.5),
    ])
    assert ok[0]["symbol"] == "600036.SH" and ok[0]["side"] == "buy"
    assert ok[1]["symbol"] == "600000.SH" and ok[1]["price"] == 10.5

    with pytest.raises(ValueError):
        validate_actions([AdviceAction(symbol="600036.SH", side="hold", quantity=1)])
    with pytest.raises(ValueError):
        validate_actions([AdviceAction(symbol="600036.SH", side="buy", quantity=1,
                                       order_type="limit")])  # 限价缺价
    with pytest.raises(ValueError):
        validate_actions([
            AdviceAction(symbol="600036.SH", side="buy", quantity=1),
            AdviceAction(symbol="600036", side="buy", quantity=2),  # 归一后重复
        ])
    with pytest.raises(ValueError):
        validate_actions([AdviceAction(symbol="??", side="buy", quantity=1)])


@pytest.mark.unit
def test_copilot_client_order_id_stable_and_scoped():
    from backend.shared.order_contract import build_copilot_client_order_id

    k1 = build_copilot_client_order_id("ab12cd34-0000-0000-0000-000000000000", "600036.SH", "buy")
    assert k1 == build_copilot_client_order_id("ab12cd34", "600036.SH", "BUY")
    assert k1.startswith("cop-ab12cd34-600036.SH-buy")
    assert k1 != build_copilot_client_order_id("ab12cd34", "600036.SH", "sell")


@pytest.mark.integration
def test_advice_execute_via_router_with_audit_and_cleanup():
    from sqlalchemy import text as sql_text

    from backend.services.api.routers.copilot import (
        AdviceAction,
        AdviceCreate,
        RejectRequest,
        _collect_context,
        create_advice,
        execute_advice,
        list_advice,
        reject_advice,
    )
    from backend.shared.copilot_contract import ensure_copilot_advice_table
    from backend.shared.sync_db import sync_session

    assert ensure_copilot_advice_table() is True
    tag = uuid.uuid4().hex[:6]
    user = {"user_id": "10000001", "tenant_id": "default"}
    created_advice: list[str] = []

    from fastapi import HTTPException

    async def _flow():
        """单一事件循环内跑完整链路（asyncpg 池绑定 loop，跨 asyncio.run 会炸）。"""
        out: dict = {}
        # ① 创建建议卡（900 股 600036.SH 买入——金额小、可控）
        payload = AdviceCreate(
            title=f"[itest-{tag}] 副驾驶建议：买入招行",
            rationale="测试用建议（评分高 + 新闻中性）",
            actions=[AdviceAction(symbol="600036.SH", side="buy", quantity=900)],
            context_refs={"signal": {"trade_date": "2026-09-15", "symbol": "600036.SH"},
                          "alert_id": "itest"},
        )
        created = await create_advice(payload, current_user=user)
        advice_id = created["data"]["advice_id"]
        created_advice.append(advice_id)
        assert created["data"]["status"] == "pending"

        # ② 一键执行（真 OrderRouter）——无论成交或拒单，都必须留下 source=co_pilot 的委托记录
        result = await execute_advice(advice_id, current_user=user)
        assert result["success"] is True
        data = result["data"]
        assert data["total"] == 1
        assert data["status"] in {"executed", "failed"}  # 行情不可用时可失败，但链路必须走通
        row = data["results"][0]
        with sync_session() as session:
            order = session.execute(
                sql_text(
                    "SELECT source, client_order_id, status::text, symbol FROM sim_orders "
                    "WHERE client_order_id = :cid"
                ),
                {"cid": f"cop-{advice_id.replace('-', '')[:8]}-600036.SH-buy"},
            ).fetchone()
        assert order is not None, f"OrderRouter 未落委托（result={row}）"
        from backend.shared.stock_utils import StockCodeUtil

        assert order[0] == "co_pilot"
        # sim_orders.symbol 存储形态由 Router 归一（前缀式）——断言按语义比对
        assert StockCodeUtil.to_suffix(order[3]) == "600036.SH"

        # ③ 状态机：重复执行 → 409；reject 已定局 → 409
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc1:
            await execute_advice(advice_id, current_user=user)
        assert exc1.value.status_code == 409
        with pytest.raises(HTTPException) as exc2:
            await reject_advice(advice_id, RejectRequest(reason="later"), current_user=user)
        assert exc2.value.status_code == 409

        # ④ 另一张卡：reject 流 + 列表可见
        payload2 = AdviceCreate(title=f"[itest-{tag}] 拒绝样本",
                                actions=[AdviceAction(symbol="000001.SZ", side="sell", quantity=100)])
        created2 = await create_advice(payload2, current_user=user)
        created_advice.append(created2["data"]["advice_id"])
        rejected = await reject_advice(created2["data"]["advice_id"],
                                       RejectRequest(reason=f"itest-{tag}"), current_user=user)
        assert rejected["data"]["status"] == "rejected"
        listing = await list_advice(status="", limit=50, current_user=user)
        titles = [i["title"] for i in listing["data"]["items"]]
        assert any(f"[itest-{tag}]" in t for t in titles)

        # ⑤ 上下文端点：块级可用性真实（positions/signals 必须可用，否则如实 unavailable）
        ctx = await _collect_context("default", "10000001")
        assert ctx["positions"]["available"] is True
        assert ctx["signals"]["available"] is True and ctx["signals"]["trade_date"]
        assert "source" in ctx["positions"] and "source" in ctx["signals"]
        return out

    try:
        asyncio.run(_flow())
    finally:
        # 清理：委托/成交（cop- 前缀 + 测试建议）/ 建议卡
        try:
            with sync_session() as session:
                for aid in created_advice:
                    cid = f"cop-{aid.replace('-', '')[:8]}-"
                    session.execute(
                        sql_text("DELETE FROM sim_trades WHERE order_id IN "
                                 "(SELECT order_id FROM sim_orders WHERE client_order_id LIKE :c)"),
                        {"c": f"{cid}%"},
                    )
                    session.execute(sql_text("DELETE FROM sim_orders WHERE client_order_id LIKE :c"),
                                    {"c": f"{cid}%"})
                session.execute(sql_text("DELETE FROM copilot_advice WHERE title LIKE :t"),
                                {"t": f"%[itest-{tag}]%"})
                session.commit()
        except Exception:  # noqa: BLE001
            pass
