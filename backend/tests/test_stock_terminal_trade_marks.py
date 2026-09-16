"""T-FE-08 后端测试：模拟成交标记（个股 K 线买卖点）/trade-marks。

覆盖：三形代码归一命中（suffix/prefix/裸码）、理由（remarks）透传、用户级隔离、
非法代码 400；真库 E2E（合成用户行 → 查询 → 清理，不触碰真实数据）。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException

_BACKEND = Path(__file__).resolve().parents[1]


@pytest.mark.asyncio
async def test_trade_marks_real_db_three_symbol_forms_and_isolation():
    try:
        from sqlalchemy import text

        from backend.shared.database_manager_v2 import get_session
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"依赖不可用: {exc}")
    try:
        async with get_session(read_only=True) as probe:
            await probe.execute(text("SELECT 1"))
    except Exception:
        from backend.shared.database_manager_v2 import close_database

        await close_database()
        try:
            async with get_session(read_only=True) as probe:
                await probe.execute(text("SELECT 1"))
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"DB 连接抖动: {exc}")

    from backend.services.api.routers.stock_terminal import stock_trade_marks

    uid = 987_650_000 + (uuid.uuid4().int % 9_000)
    other_uid = uid + 1
    order_id = str(uuid.uuid4())
    trade_id = str(uuid.uuid4())
    executed = datetime.now(timezone.utc) - timedelta(days=1)
    tenant = "default"

    try:
        async with get_session() as session:
            await session.execute(
                text(
                    "INSERT INTO sim_orders (order_id, tenant_id, user_id, portfolio_id, symbol, "
                    "side, order_type, trading_mode, status, quantity, order_value, execution_model, "
                    "total_fee, version, created_at, updated_at, remarks, client_order_id) VALUES "
                    "(:oid, :t, :u, 0, '600036.SH', 'buy', 'market', 'SIMULATION', 'filled', 100, 4110, "
                    "'ashare_matcher', 5.0, 1, now(), now(), '调仓买入: 当前0 → 目标100（测试）', :cid)"
                ),
                {"oid": order_id, "t": tenant, "u": uid, "cid": f"pytest-{order_id[:8]}"},
            )
            await session.execute(
                text(
                    "INSERT INTO sim_trades (trade_id, order_id, tenant_id, user_id, portfolio_id, "
                    "symbol, side, trading_mode, quantity, price, trade_value, commission, stamp_duty, "
                    "transfer_fee, total_fee, executed_at, created_at, updated_at) VALUES "
                    "(:tid, :oid, :t, :u, 0, '600036.SH', 'buy', 'SIMULATION', 100, 41.1, 4110, 5.0, 0, 0, "
                    "5.0, :ts, now(), now())"
                ),
                {"tid": trade_id, "oid": order_id, "t": tenant, "u": uid, "ts": executed},
            )
            await session.commit()

        user = {"tenant_id": tenant, "user_id": str(uid)}

        # 三形归一：suffix / prefix / 裸码 均应命中同一笔
        for form in ("600036.SH", "SH600036", "600036"):
            resp = await stock_trade_marks(symbol=form, days=30, current_user=user)
            items = resp["data"]["items"]
            assert len(items) == 1, f"{form} 应命中 1 笔，实际 {len(items)}"
            item = items[0]
            assert item["side"] == "buy"
            assert abs(item["price"] - 41.1) < 1e-9
            assert item["shares"] == 100
            assert abs(item["amount"] - 4110.0) < 1e-6
            assert "调仓买入" in item["reason"]  # remarks 透传（下钻理由）
            assert item["order_id"] == order_id

        # 用户隔离：另一个用户看不到这笔
        other = await stock_trade_marks(
            symbol="600036.SH", days=30, current_user={"tenant_id": tenant, "user_id": str(other_uid)}
        )
        assert other["data"]["items"] == []
    finally:
        from backend.shared.database_manager_v2 import get_session as _gs

        async with _gs() as session:
            await session.execute(text("DELETE FROM sim_trades WHERE trade_id = :t"), {"t": trade_id})
            await session.execute(text("DELETE FROM sim_orders WHERE order_id = :o"), {"o": order_id})
            await session.commit()
        from backend.shared.database_manager_v2 import close_database

        await close_database()

    _ = date  # 保持导入语义（窗口过滤以 executed_at 为准）


@pytest.mark.unit
@pytest.mark.asyncio
async def test_trade_marks_rejects_illegal_symbol():
    from backend.services.api.routers.stock_terminal import stock_trade_marks

    with pytest.raises(HTTPException) as exc:
        await stock_trade_marks(
            symbol=";;DROP", days=30, current_user={"tenant_id": "default", "user_id": "1"}
        )
    assert exc.value.status_code == 400


@pytest.mark.unit
def test_trade_marks_wiring_source_guards():
    src = (_BACKEND / "services/api/routers/stock_terminal.py").read_text(encoding="utf-8")
    assert '@router.get("/trade-marks")' in src
    # 三形归一 + 理由透传 + 用户级
    assert "StockCodeUtil.to_suffix(raw)" in src and "StockCodeUtil.to_prefix(sym)" in src
    assert "o.remarks" in src
    assert "require_sim_user_id" in src
