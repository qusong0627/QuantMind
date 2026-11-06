"""每日日结（智能体台账 + 执行事实 + 真实资金锚）。

数据源（用户 2026-11-06 明确：**以智能体台账为准**）：

* ``qm_agent_ledger_fill``      —— 分账成交明细（每 agent 每笔买卖，trade_date）
* ``qm_agent_ledger_roundtrip`` —— 平仓回合（已实现盈亏）
* ``qm_agent_ledger_position``  —— 当前持仓（仅「当日」是 EOD 口径；补历史日不含）
* ``qm_agent_ledger_account``   —— 虚拟现金（同上）
* ``orders`` / ``trades``       —— 执行事实（委托状态、成交额、费用、执行券商 broker）
* ``real_account_snapshots``    —— 真实账户现金/市值/总资产（资金锚，勾稽用）

日结单按 ``(tenant_id, user_id, trade_date)`` 唯一；重跑幂等 upsert。
金额口径：成交额取 ``trades.trade_value``；费用取 ``trades`` 三费；现金流水
``卖出 − 买入 − 费用``。真实资金锚与账本现金的差异单独记进 ``recon_json``，
**不自动改账**。
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from sqlalchemy import text

from backend.services.trade_shared.models.enums import (
    OrderStatus,
    OrderSide,
    TradingMode,
)

logger = logging.getLogger(__name__)

SETTLEMENT_TABLE = "qm_daily_settlements"

DDL = f"""
CREATE TABLE IF NOT EXISTS {SETTLEMENT_TABLE} (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       VARCHAR(64) NOT NULL DEFAULT 'default',
    user_id         VARCHAR(64) NOT NULL,
    trade_date      DATE NOT NULL,
    orders_total    INTEGER NOT NULL DEFAULT 0,
    orders_filled   INTEGER NOT NULL DEFAULT 0,
    orders_partial  INTEGER NOT NULL DEFAULT 0,
    orders_cancelled INTEGER NOT NULL DEFAULT 0,
    orders_rejected INTEGER NOT NULL DEFAULT 0,
    orders_pending  INTEGER NOT NULL DEFAULT 0,
    filled_value    DOUBLE PRECISION NOT NULL DEFAULT 0,
    buy_value       DOUBLE PRECISION NOT NULL DEFAULT 0,
    sell_value      DOUBLE PRECISION NOT NULL DEFAULT 0,
    commission      DOUBLE PRECISION NOT NULL DEFAULT 0,
    stamp_duty      DOUBLE PRECISION NOT NULL DEFAULT 0,
    transfer_fee    DOUBLE PRECISION NOT NULL DEFAULT 0,
    total_fee       DOUBLE PRECISION NOT NULL DEFAULT 0,
    net_cash_flow   DOUBLE PRECISION NOT NULL DEFAULT 0,
    realized_pnl    DOUBLE PRECISION NOT NULL DEFAULT 0,
    cash            DOUBLE PRECISION,
    market_value    DOUBLE PRECISION,
    total_asset     DOUBLE PRECISION,
    status          VARCHAR(16) NOT NULL DEFAULT 'open',
    agents_json     JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    brokers_json    JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    positions_json  JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    recon_json      JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_daily_settlement_day UNIQUE (tenant_id, user_id, trade_date)
);
"""

_ensured = False


async def ensure_table_async(session) -> None:
    """幂等建表（进程内只跑一次；失败不抛出，下次重试）。"""
    global _ensured
    if _ensured:
        return
    try:
        await session.execute(text("SET LOCAL lock_timeout = '3s'"))
        await session.execute(text(DDL))
        await session.commit()
        _ensured = True
    except Exception as exc:  # noqa: BLE001
        logger.warning("[DailySettlement] 建表失败（不阻断记账重试）: %s", exc)


def day_window(day: date) -> tuple[datetime, datetime]:
    """北京时间的交易日窗口（orders/trades 的 naive 时间列都是上海墙钟）。"""
    start = datetime.combine(day, time.min)
    return start, start + timedelta(days=1)


#: 分账台账的 ``sell_ts/filled_at`` 是 TIMESTAMPTZ；与 naive 参数比较会被 PG 按会话
#: 时区（容器 UTC）解释，整体偏 8 小时——必须显式传北京时间的 aware 值。
TZ_CN = timezone(timedelta(hours=8))


def aware_day_window(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=TZ_CN)
    return start, start + timedelta(days=1)


async def compute_day(
    session, *, tenant_id: str, user_id: str, day: date
) -> dict[str, Any]:
    """汇总一个交易日的账（纯读；调用方决定是否落库）。"""
    start, end = day_window(day)
    aware_start, aware_end = aware_day_window(day)
    payload: dict[str, Any] = {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "trade_date": day,
    }

    # ── 1. 执行事实：委托 ───────────────────────────────────────────
    order_row = (
        (
            await session.execute(
                text(
                    """
                SELECT
                  COUNT(*) AS total,
                  COUNT(*) FILTER (WHERE status = 'filled') AS filled,
                  COUNT(*) FILTER (WHERE status = 'partially_filled') AS partial,
                  COUNT(*) FILTER (WHERE status = 'cancelled') AS cancelled,
                  COUNT(*) FILTER (WHERE status = 'rejected') AS rejected,
                  COUNT(*) FILTER (
                    WHERE status IN ('pending', 'submitted', 'partially_filled')
                  ) AS pending,
                  COALESCE(SUM(filled_value), 0) AS filled_value
                FROM orders
                WHERE tenant_id = :t AND user_id = :u AND trading_mode = 'REAL'
                  AND created_at >= :start AND created_at < :end
                """
                ),
                {"t": tenant_id, "u": user_id, "start": start, "end": end},
            )
        )
        .mappings()
        .one()
    )
    payload.update(
        {
            "orders_total": int(order_row["total"]),
            "orders_filled": int(order_row["filled"]),
            "orders_partial": int(order_row["partial"]),
            "orders_cancelled": int(order_row["cancelled"]),
            "orders_rejected": int(order_row["rejected"]),
            "orders_pending": int(order_row["pending"]),
            "filled_value": float(order_row["filled_value"] or 0),
        }
    )

    broker_rows = (
        (
            await session.execute(
                text(
                    """
                SELECT COALESCE(broker, '') AS broker, COUNT(*) AS n,
                       COALESCE(SUM(filled_value), 0) AS v
                FROM orders
                WHERE tenant_id = :t AND user_id = :u AND trading_mode = 'REAL'
                  AND created_at >= :start AND created_at < :end
                GROUP BY 1 ORDER BY 2 DESC
                """
                ),
                {"t": tenant_id, "u": user_id, "start": start, "end": end},
            )
        )
        .mappings()
        .all()
    )
    payload["brokers_json"] = {
        str(r["broker"] or "(未标记)"): {
            "orders": int(r["n"]),
            "filled_value": round(float(r["v"] or 0), 2),
        }
        for r in broker_rows
    }

    # ── 2. 执行事实：成交与费用 ─────────────────────────────────────
    trade_row = (
        (
            await session.execute(
                text(
                    """
                SELECT
                  COALESCE(SUM(trade_value), 0) AS value,
                  COALESCE(SUM(trade_value) FILTER (WHERE side = 'buy'), 0) AS buy_value,
                  COALESCE(SUM(trade_value) FILTER (WHERE side = 'sell'), 0) AS sell_value,
                  COALESCE(SUM(commission), 0) AS commission,
                  COALESCE(SUM(stamp_duty), 0) AS stamp_duty,
                  COALESCE(SUM(transfer_fee), 0) AS transfer_fee
                FROM trades
                WHERE tenant_id = :t AND user_id = :u AND trading_mode = 'REAL'
                  AND executed_at >= :start AND executed_at < :end
                """
                ),
                {"t": tenant_id, "u": user_id, "start": start, "end": end},
            )
        )
        .mappings()
        .one()
    )
    buy_value = float(trade_row["buy_value"] or 0)
    sell_value = float(trade_row["sell_value"] or 0)
    total_fee = (
        float(trade_row["commission"] or 0)
        + float(trade_row["stamp_duty"] or 0)
        + float(trade_row["transfer_fee"] or 0)
    )
    payload.update(
        {
            "buy_value": round(buy_value, 2),
            "sell_value": round(sell_value, 2),
            "commission": round(float(trade_row["commission"] or 0), 2),
            "stamp_duty": round(float(trade_row["stamp_duty"] or 0), 2),
            "transfer_fee": round(float(trade_row["transfer_fee"] or 0), 2),
            "total_fee": round(total_fee, 2),
            "net_cash_flow": round(sell_value - buy_value - total_fee, 2),
        }
    )

    # ── 3. 智能体台账：分账成交 / 平仓盈亏 / 当前持仓 ───────────────
    fill_rows = (
        (
            await session.execute(
                text(
                    """
                SELECT agent,
                  COUNT(*) AS fills,
                  COALESCE(SUM(volume) FILTER (WHERE side = 'buy'), 0) AS buy_vol,
                  COALESCE(SUM(volume) FILTER (WHERE side = 'sell'), 0) AS sell_vol,
                  COALESCE(SUM(volume * price) FILTER (WHERE side = 'buy'), 0) AS buy_amt,
                  COALESCE(SUM(volume * price) FILTER (WHERE side = 'sell'), 0) AS sell_amt
                FROM qm_agent_ledger_fill
                WHERE tenant_id = :t AND user_id = :u AND trade_date = :d
                GROUP BY agent ORDER BY agent
                """
                ),
                {"t": tenant_id, "u": user_id, "d": day},
            )
        )
        .mappings()
        .all()
    )
    agents: dict[str, Any] = {
        str(r["agent"]): {
            "fills": int(r["fills"]),
            "buy_volume": float(r["buy_vol"] or 0),
            "sell_volume": float(r["sell_vol"] or 0),
            "buy_amount": round(float(r["buy_amt"] or 0), 2),
            "sell_amount": round(float(r["sell_amt"] or 0), 2),
        }
        for r in fill_rows
    }

    rt_rows = (
        (
            await session.execute(
                text(
                    """
                SELECT agent, COUNT(*) AS n, COALESCE(SUM(realized_pnl), 0) AS pnl
                FROM qm_agent_ledger_roundtrip
                WHERE tenant_id = :t AND user_id = :u
                  AND sell_ts >= :start AND sell_ts < :end
                GROUP BY agent ORDER BY agent
                """
                ),
                {"t": tenant_id, "u": user_id, "start": aware_start, "end": aware_end},
            )
        )
        .mappings()
        .all()
    )
    for r in rt_rows:
        entry = agents.setdefault(
            str(r["agent"]),
            {
                "fills": 0,
                "buy_volume": 0.0,
                "sell_volume": 0.0,
                "buy_amount": 0.0,
                "sell_amount": 0.0,
            },
        )
        entry["roundtrips"] = int(r["n"])
        entry["realized_pnl"] = round(float(r["pnl"] or 0), 2)
    payload["agents_json"] = agents
    payload["realized_pnl"] = round(
        sum(float(v.get("realized_pnl") or 0) for v in agents.values()), 2
    )

    pos_rows = (
        (
            await session.execute(
                text(
                    """
                SELECT agent, code, volume, cost_price, updated_at
                FROM qm_agent_ledger_position
                WHERE tenant_id = :t AND user_id = :u AND volume > 0
                ORDER BY agent, code
                """
                ),
                {"t": tenant_id, "u": user_id},
            )
        )
        .mappings()
        .all()
    )
    payload["positions_json"] = [
        {
            "agent": str(r["agent"]),
            "code": str(r["code"]),
            "volume": float(r["volume"]),
            "cost_price": float(r["cost_price"]),
            "updated_at": str(r["updated_at"]),
        }
        for r in pos_rows
    ]

    # ── 4. 真实资金锚（当日最后一条快照；没有则留空，不拿旧值冒充） ──
    snap = (
        (
            await session.execute(
                text(
                    """
                SELECT cash, market_value, total_asset, source, snapshot_at
                FROM real_account_snapshots
                WHERE tenant_id = :t AND user_id = :u AND snapshot_date = :d
                ORDER BY snapshot_at DESC LIMIT 1
                """
                ),
                {"t": tenant_id, "u": user_id, "d": day},
            )
        )
        .mappings()
        .first()
    )
    if snap:
        payload.update(
            {
                "cash": round(float(snap["cash"] or 0), 2),
                "market_value": round(float(snap["market_value"] or 0), 2),
                "total_asset": round(float(snap["total_asset"] or 0), 2),
                "status": "closed",
            }
        )
        payload["recon_json"] = {
            "snapshot_source": str(snap["source"]),
            "snapshot_at": str(snap["snapshot_at"]),
        }
    else:
        payload.update(
            {
                "cash": None,
                "market_value": None,
                "total_asset": None,
                "status": "no_snapshot",
            }
        )
        payload["recon_json"] = {}

    payload["has_activity"] = bool(
        payload["orders_total"] or payload["buy_value"] or payload["sell_value"]
    )
    return payload


_UPSERT_SQL = text(
    f"""
    INSERT INTO {SETTLEMENT_TABLE} (
      tenant_id, user_id, trade_date,
      orders_total, orders_filled, orders_partial, orders_cancelled,
      orders_rejected, orders_pending, filled_value,
      buy_value, sell_value, commission, stamp_duty, transfer_fee, total_fee,
      net_cash_flow, realized_pnl, cash, market_value, total_asset, status,
      agents_json, brokers_json, positions_json, recon_json, updated_at
    ) VALUES (
      :tenant_id, :user_id, :trade_date,
      :orders_total, :orders_filled, :orders_partial, :orders_cancelled,
      :orders_rejected, :orders_pending, :filled_value,
      :buy_value, :sell_value, :commission, :stamp_duty, :transfer_fee, :total_fee,
      :net_cash_flow, :realized_pnl, :cash, :market_value, :total_asset, :status,
      CAST(:agents_json AS jsonb), CAST(:brokers_json AS jsonb),
      CAST(:positions_json AS jsonb), CAST(:recon_json AS jsonb), NOW()
    )
    ON CONFLICT (tenant_id, user_id, trade_date) DO UPDATE SET
      orders_total = EXCLUDED.orders_total,
      orders_filled = EXCLUDED.orders_filled,
      orders_partial = EXCLUDED.orders_partial,
      orders_cancelled = EXCLUDED.orders_cancelled,
      orders_rejected = EXCLUDED.orders_rejected,
      orders_pending = EXCLUDED.orders_pending,
      filled_value = EXCLUDED.filled_value,
      buy_value = EXCLUDED.buy_value,
      sell_value = EXCLUDED.sell_value,
      commission = EXCLUDED.commission,
      stamp_duty = EXCLUDED.stamp_duty,
      transfer_fee = EXCLUDED.transfer_fee,
      total_fee = EXCLUDED.total_fee,
      net_cash_flow = EXCLUDED.net_cash_flow,
      realized_pnl = EXCLUDED.realized_pnl,
      cash = EXCLUDED.cash,
      market_value = EXCLUDED.market_value,
      total_asset = EXCLUDED.total_asset,
      status = EXCLUDED.status,
      agents_json = EXCLUDED.agents_json,
      brokers_json = EXCLUDED.brokers_json,
      positions_json = EXCLUDED.positions_json,
      recon_json = EXCLUDED.recon_json,
      updated_at = NOW()
    """
)


async def run_day(
    session, *, tenant_id: str, user_id: str, day: date
) -> dict[str, Any]:
    """计算并落库单日日结（幂等）。"""
    import json

    await ensure_table_async(session)
    payload = await compute_day(session, tenant_id=tenant_id, user_id=user_id, day=day)
    params = dict(payload)
    for key in ("agents_json", "brokers_json", "positions_json", "recon_json"):
        params[key] = json.dumps(params.get(key) or {}, ensure_ascii=False, default=str)
    await session.execute(_UPSERT_SQL, params)
    await session.commit()
    return payload


async def list_days(
    session, *, tenant_id: str, user_id: str, start: date, end: date
) -> list[dict[str, Any]]:
    rows = (
        (
            await session.execute(
                text(
                    f"""
                SELECT * FROM {SETTLEMENT_TABLE}
                WHERE tenant_id = :t AND user_id = :u
                  AND trade_date >= :start AND trade_date <= :end
                ORDER BY trade_date DESC
                """
                ),
                {"t": tenant_id, "u": user_id, "start": start, "end": end},
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


async def trading_days_between(start: date, end: date) -> list[date]:
    """区间内的交易日（日历不可用时退化为工作日）。"""
    try:
        from backend.shared.trading_calendar import calendar_service

        days = []
        cursor = start
        while cursor <= end:
            if await calendar_service.is_trading_day(
                market="SSE", trade_date=cursor, tenant_id="default", user_id="0"
            ):
                days.append(cursor)
            cursor += timedelta(days=1)
        return days
    except Exception:  # noqa: BLE001
        days = []
        cursor = start
        while cursor <= end:
            if cursor.weekday() < 5:
                days.append(cursor)
            cursor += timedelta(days=1)
        return days


__all__ = [
    "SETTLEMENT_TABLE",
    "compute_day",
    "day_window",
    "ensure_table_async",
    "list_days",
    "run_day",
    "trading_days_between",
]

# 供测试与调用方复用的枚举（避免重复定义数值口径）
STATUS_FILLED = OrderStatus.FILLED.value
SIDE_BUY = OrderSide.BUY.value
MODE_REAL = TradingMode.REAL.value
