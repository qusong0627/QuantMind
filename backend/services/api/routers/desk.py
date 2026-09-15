"""今日交易台 API（T-P1-05）。

一屏闭环（设计见 docs/前端界面设计_设计方案.md §二、主文档 §12）：
  数据✓ → 推理✓ → 信号 → 执行 → 盈亏 + 系统健康卡。

设计要点：
- **与体检脚本同源**：`pipeline` 四步状态与 `health` 卡直接由
  ``backend/scripts/diagnose/health.py::CHECKS`` 的判定结果映射（不重复造判定逻辑）；
- 健康检查是同步 IO（psycopg2 + sync redis），统一 `asyncio.to_thread` 执行，
  不阻塞事件循环；`?health=false` 可跳过（纯数据快速加载）；
- **每个数字带 `source` 下钻字段**（前端下钻链路的契约，主文档 §十）。

数据口径：
- 身份：`get_current_user` → 快照/订单用归一 uid（`require_sim_user_id`），
  REAL 订单用原始 sub（orders.user_id 为字符串口径）；
- 信号：`engine_signal_scores` 最新 trade_date（rank_pct 分位口径）。

GET /api/v1/desk/today
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Query

from backend.services.api.user_app.middleware.auth import get_current_user
from backend.shared.database_manager_v2 import get_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/desk", tags=["Desk"])

# pipeline 步骤 ← 体检断言映射（同源；键=步骤，值=体检项 ID）
_PIPELINE_FROM_CHECKS: list[tuple[str, str, str]] = [
    ("data_sync", "数据同步", "C08"),
    ("inference", "推理就绪", "C02"),
    ("signals", "信号分布", "C01"),
    ("settlement", "结算/台账", "C05"),
]
_LEVEL_TO_STATUS = {"ok": "ok", "warn": "warn", "fail": "fail"}


def build_pipeline(health_items: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """体检结果 → 管线步骤（纯函数，可单测）。"""
    out: list[dict[str, Any]] = []
    for key, label, check_id in _PIPELINE_FROM_CHECKS:
        item = health_items.get(check_id)
        if item is None:
            out.append(
                {
                    "key": key,
                    "label": label,
                    "status": "unknown",
                    "detail": "健康检查未运行（?health=false）",
                    "source": f"health:{check_id}",
                }
            )
            continue
        out.append(
            {
                "key": key,
                "label": label,
                "status": _LEVEL_TO_STATUS.get(str(item.get("level")), "unknown"),
                "detail": str(item.get("detail") or ""),
                "source": f"health:{check_id}",
            }
        )
    return out


async def _run_health() -> dict[str, dict[str, Any]]:
    """在线程池跑同步体检（不阻塞事件循环）；异常降级为空表。"""

    def _sync() -> dict[str, dict[str, Any]]:
        try:
            import asyncio as _aio

            from backend.scripts.diagnose import health as health_mod

            ctx = health_mod._build_context()
            items: dict[str, dict[str, Any]] = {}
            # health.py 的检查是 async（内部只做同步 IO）——在专属事件循环里统一执行
            loop = _aio.new_event_loop()
            try:
                for cid, name, fn in health_mod.CHECKS:
                    try:
                        r = loop.run_until_complete(fn(ctx))
                        items[cid] = {
                            "id": cid,
                            "name": name,
                            "level": r.level,
                            "detail": r.detail,
                        }
                    except Exception as exc:  # noqa: BLE001
                        items[cid] = {
                            "id": cid,
                            "name": name,
                            "level": "fail",
                            "detail": f"检查执行异常: {exc}",
                        }
            finally:
                loop.close()
            return items
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Desk] health 运行失败: %s", exc)
            return {}

    return await asyncio.to_thread(_sync)


async def _collect_signals(tenant_id: str) -> dict[str, Any]:
    async with get_session(read_only=True) as session:
        from sqlalchemy import text as sa_text

        date_row = (
            await session.execute(
                sa_text("SELECT max(trade_date) AS d FROM engine_signal_scores")
            )
        ).one()
        trade_date = date_row.d
        if trade_date is None:
            return {"trade_date": None, "market": "CN", "source": "db:engine_signal_scores"}
        counts = (
            await session.execute(
                sa_text(
                    "SELECT signal_side, count(*) AS n FROM engine_signal_scores "
                    "WHERE trade_date = :d GROUP BY signal_side"
                ),
                {"d": trade_date},
            )
        ).all()
        top = (
            await session.execute(
                sa_text(
                    "SELECT symbol, signal_side, round(rank_pct::numeric, 4) AS rank_pct, "
                    "round(fusion_score::numeric, 4) AS score "
                    "FROM engine_signal_scores WHERE trade_date = :d AND signal_side = 'BUY' "
                    "ORDER BY rank_pct DESC NULLS LAST LIMIT 5"
                ),
                {"d": trade_date},
            )
        ).all()
    by_side = {str(r.signal_side): int(r.n) for r in counts}
    return {
        "trade_date": str(trade_date),
        "market": "CN",
        "buy": by_side.get("BUY", 0),
        "sell": by_side.get("SELL", 0),
        "hold": by_side.get("HOLD", 0),
        "top_buy": [
            {
                "symbol": str(r.symbol),
                "side": str(r.signal_side),
                "rank_pct": float(r.rank_pct) if r.rank_pct is not None else None,
                "score": float(r.score) if r.score is not None else None,
            }
            for r in top
        ],
        "source": "db:engine_signal_scores",
    }


async def _collect_execution(tenant_id: str, sim_uid: int, raw_user: str) -> dict[str, Any]:
    async with get_session(read_only=True) as session:
        from sqlalchemy import text as sa_text

        sim_rows = (
            await session.execute(
                sa_text(
                    "SELECT symbol, side::text AS side, quantity, status::text AS status, "
                    "price_source, client_order_id, source, remarks, created_at "
                    "FROM sim_orders WHERE tenant_id = :t AND user_id = :u "
                    "AND created_at >= date_trunc('day', now()) "
                    "ORDER BY created_at DESC LIMIT 10"
                ),
                {"t": tenant_id, "u": sim_uid},
            )
        ).all()
        real_rows = (
            await session.execute(
                sa_text(
                    "SELECT symbol, side::text AS side, quantity, status::text AS status, "
                    "price_source, client_order_id, source, remarks, created_at "
                    "FROM orders WHERE tenant_id = :t AND user_id = :u "
                    "AND created_at >= date_trunc('day', now()) "
                    "ORDER BY created_at DESC LIMIT 10"
                ),
                {"t": tenant_id, "u": raw_user},
            )
        ).all()

    items = [
        {
            "mode": "SIM",
            "symbol": str(r.symbol),
            "side": str(r.side),
            "quantity": float(r.quantity or 0),
            "status": str(r.status),
            "price_source": r.price_source,
            "client_order_id": r.client_order_id,
            "origin": r.source,
            "reason": r.remarks,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in sim_rows
    ] + [
        {
            "mode": "REAL",
            "symbol": str(r.symbol),
            "side": str(r.side),
            "quantity": float(r.quantity or 0),
            "status": str(r.status),
            "price_source": r.price_source,
            "client_order_id": r.client_order_id,
            "origin": r.source,
            "reason": r.remarks,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in real_rows
    ]
    items.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    return {
        "sim_count": len(sim_rows),
        "real_count": len(real_rows),
        "filled": sum(1 for i in items if i["status"] == "filled"),
        "rejected": sum(1 for i in items if i["status"] == "rejected"),
        "items": items[:10],
        "source": "db:sim_orders+orders",
    }


async def _collect_pnl(tenant_id: str, sim_user_id: str) -> dict[str, Any]:
    async with get_session(read_only=True) as session:
        from sqlalchemy import text as sa_text

        row = (
            await session.execute(
                sa_text(
                    "SELECT snapshot_date, total_asset, initial_capital, total_pnl, today_pnl, "
                    "market_value, updated_at FROM simulation_fund_snapshots "
                    "WHERE tenant_id = :t AND user_id = :u "
                    "ORDER BY snapshot_date DESC LIMIT 1"
                ),
                {"t": tenant_id, "u": sim_user_id},
            )
        ).first()
    if row is None:
        return {
            "available": False,
            "detail": "无资金快照（账户未初始化或权益结算未运行）",
            "source": "db:simulation_fund_snapshots",
        }
    return {
        "available": True,
        "snapshot_date": str(row.snapshot_date),
        "total_asset": float(row.total_asset or 0),
        "initial_capital": float(row.initial_capital or 0),
        "total_pnl": float(row.total_pnl or 0),
        "today_pnl": float(row.today_pnl or 0),
        "market_value": float(row.market_value or 0),
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "source": "db:simulation_fund_snapshots",
    }


@router.get("/today")
async def desk_today(
    health: bool = Query(True, description="是否运行体检（10 项断言，约 1-2s）"),
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    """今日交易台聚合：管线/信号/执行/盈亏/健康——每个数字带 source 下钻字段。"""
    from backend.services.trade_shared.simulation_manager import require_sim_user_id

    tenant_id = str(current_user.get("tenant_id") or "default")
    raw_user = str(current_user.get("user_id") or "")
    sim_uid = require_sim_user_id(raw_user, tenant_id=tenant_id)

    health_items = await _run_health() if health else {}
    signals, execution, pnl = await asyncio.gather(
        _collect_signals(tenant_id),
        _collect_execution(tenant_id, int(sim_uid), raw_user),
        _collect_pnl(tenant_id, str(sim_uid)),
    )
    health_summary = {
        "ok": sum(1 for i in health_items.values() if i.get("level") == "ok"),
        "warn": sum(1 for i in health_items.values() if i.get("level") == "warn"),
        "fail": sum(1 for i in health_items.values() if i.get("level") == "fail"),
        "items": list(health_items.values()),
        "source": "scripts/diagnose/health.py（与体检命令行同源）",
    }
    return {
        "success": True,
        "data": {
            "as_of": datetime.now(timezone.utc).isoformat(),
            "tenant_id": tenant_id,
            "user_id": raw_user,
            "sim_user_id": str(sim_uid),
            "pipeline": build_pipeline(health_items),
            "signals": signals,
            "execution": execution,
            "pnl": pnl,
            "health": health_summary,
        },
    }
