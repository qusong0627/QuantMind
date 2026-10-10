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


async def _ensure_fresh_db_pool() -> None:
    """批跑防坑：前序测试的 asyncio.run 会把 asyncpg 连接绑死在已关闭的 loop 上——
    刷新池（与 test_hot_set_builder._ensure_db_pool 同范式）。"""
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
def test_advice_create_allows_advisory_only_cards():
    """纯建议卡（actions 空）= 观察/纪律类建议（2026-09-18 契约扩展）；校验空表恒等。"""
    from backend.services.api.routers.copilot import AdviceCreate, validate_actions

    payload = AdviceCreate(title="不追热点：新闻单透镜不构成入场依据", rationale="…")
    assert payload.actions == []
    assert payload.source == "quantbot"
    assert validate_actions([]) == []


@pytest.mark.unit
def test_copilot_client_order_id_stable_and_scoped():
    from backend.shared.order_contract import build_copilot_client_order_id

    k1 = build_copilot_client_order_id("ab12cd34-0000-0000-0000-000000000000", "600036.SH", "buy")
    assert k1 == build_copilot_client_order_id("ab12cd34", "600036.SH", "BUY")
    assert k1.startswith("cop-ab12cd34-600036.SH-buy")
    assert k1 != build_copilot_client_order_id("ab12cd34", "600036.SH", "sell")


@pytest.mark.unit
def test_panel_as_of_is_data_moment_not_response_time(monkeypatch):
    """审计 H15：面板 as_of 必须是**数据时刻**（窗口内最新告警 ts），不是响应时刻。

    旧实现 as_of=datetime.now()：哨兵/总线停摆数小时，面板仍写「截至 <现在>」，
    看着新鲜。修复后口径：有事件 → ts DESC 首行（max ts）；无事件 → None（如实）；
    DB 挂 → None + events.available=False。事件 ts 一律 ISO-8601 + Z（前端按 UTC 解析年龄）。
    """
    from backend.services.api.routers import copilot as mod

    ts_max = datetime(2026, 10, 10, 3, 20, tzinfo=timezone.utc)
    ts_old = datetime(2026, 10, 10, 1, 5, tzinfo=timezone.utc)

    def row(ts: datetime, aid: str) -> tuple:
        return (
            aid,
            ts,
            "news:risk_event",
            "warn",
            "CN",
            "600036.SH",
            "标题",
            [],
            True,
            "filled",
            True,
            None,
        )

    class _Res:
        def __init__(self, rows):
            self._rows = rows

        def fetchall(self):
            return self._rows

    class _Sess:
        """双查询假会话：事件流按注入行返回；误报率块（30d 汇总）恒空。"""

        def __init__(self, rows, *, raise_on_events):
            self._rows = rows
            self._raise = raise_on_events

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def execute(self, stmt, params=None):
            sql = str(stmt)
            if "trade_date >= CURRENT_DATE - 30" in sql:
                return _Res([])
            if self._raise:
                raise RuntimeError("db down")
            return _Res(self._rows)

    def run(rows, *, raise_on_events=False):
        monkeypatch.setattr(
            mod,
            "get_session",
            lambda **kw: _Sess(rows, raise_on_events=raise_on_events),
        )
        return asyncio.run(
            mod.copilot_panel(
                hours=24, current_user={"user_id": "1", "tenant_id": "default"}
            )
        )

    data = run([row(ts_max, "a2"), row(ts_old, "a1")])["data"]
    assert data["as_of"] == "2026-10-10T03:20:00Z", (
        "as_of 必须是数据时刻（首行=max ts），不是响应时刻"
    )
    assert data["events"]["items"][0]["ts"] == "2026-10-10T03:20:00Z", (
        "事件 ts 必须 ISO-8601 + Z"
    )

    empty = run([])["data"]
    assert empty["as_of"] is None, "窗口内无事件必须如实 null——不得冒充新鲜"

    down = run([], raise_on_events=True)["data"]
    assert down["as_of"] is None
    assert down["events"]["available"] is False


@pytest.mark.integration
def test_advice_execute_via_router_with_audit_and_cleanup():
    import asyncio as _asyncio_early  # noqa: F401
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
        await _ensure_fresh_db_pool()
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

        # ② 模拟执行（OrderRouter=模拟盘撮合，不触真单）——无论成交或拒单，都必须留下
        #    source=co_pilot 的委托记录
        result = await execute_advice(advice_id, current_user=user)
        assert result["success"] is True
        data = result["data"]
        assert data["mode"] == "simulation", "执行信封必须标注 mode=simulation（审计 C4）"
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
