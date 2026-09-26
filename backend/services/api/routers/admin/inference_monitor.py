"""Admin view: automatic inference schedule success / failure."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text

from backend.services.api.user_app.middleware.auth import require_admin
from backend.services.live_trading.services.admin_inference_view import (
    ENSURE_DISPATCH_LOG_SQL,
    auto_inference_enabled,
    isoformat_date,
    isoformat_dt,
    next_weekday_auto_inference,
    reason_label,
)
from backend.shared.database_manager_v2 import get_session

router = APIRouter(dependencies=[Depends(require_admin)])
_SH_TZ = ZoneInfo("Asia/Shanghai")


def _ok(data, message: str = "success"):
    return {"success": True, "code": 200, "message": message, "data": data}


def _row_item(row: Any) -> dict[str, Any]:
    mapping = row._mapping
    status = str(mapping.get("status") or "")
    reason_code = mapping.get("reason_code")
    return {
        "id": str(mapping.get("id")),
        "trigger_source": mapping.get("trigger_source"),
        "tenant_id": mapping.get("tenant_id"),
        "user_id": str(mapping.get("user_id") or ""),
        "strategy_id": mapping.get("strategy_id"),
        "model_id": mapping.get("model_id"),
        "data_trade_date": isoformat_date(mapping.get("data_trade_date")),
        "prediction_trade_date": isoformat_date(mapping.get("prediction_trade_date")),
        "status": status,
        "reason_code": reason_code,
        "reason_label": reason_label(reason_code),
        "reason_detail": mapping.get("reason_detail"),
        "run_id": mapping.get("run_id"),
        "created_at": isoformat_dt(mapping.get("created_at")),
    }


async def _ensure_table() -> None:
    async with get_session() as db:
        await db.execute(text(ENSURE_DISPATCH_LOG_SQL))
        await db.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_qm_inf_dispatch_owner_created "
                "ON qm_model_inference_dispatch_logs (tenant_id, user_id, created_at DESC)"
            )
        )
        await db.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_qm_inf_dispatch_status_created "
                "ON qm_model_inference_dispatch_logs (status, created_at DESC)"
            )
        )


@router.get("/inference/monitor")
async def get_inference_monitor(
    status: str | None = Query(None, description="success / failed / skipped"),
    user_id: str | None = Query(None),
    model_id: str | None = Query(None),
    limit: int = Query(80, ge=1, le=300),
    current_user: dict = Depends(require_admin),
):
    status_filter = str(status or "").strip().lower() or None
    if status_filter and status_filter not in {"success", "failed", "skipped"}:
        status_filter = None
    user_filter = str(user_id or "").strip() or None
    model_filter = str(model_id or "").strip() or None

    schedule = {
        "enabled": auto_inference_enabled(),
        "cron": "工作日 06:30",
        "timezone": "Asia/Shanghai",
        "next_run_at": isoformat_dt(next_weekday_auto_inference()),
        "task": "engine.tasks.backfill_default_inference",
        "description": "默认模型推理缺口补全（含历史空洞，同「一键补全至最新」）",
    }

    empty = {
        "schedule": schedule,
        "summary": {
            "total": 0,
            "success": 0,
            "failed": 0,
            "skipped": 0,
            "running": 0,
            "today_success": 0,
            "today_failed": 0,
            "today_skipped": 0,
            "today_running": 0,
            "latest_at": None,
        },
        "settings": [],
        "items": [],
    }

    try:
        await _ensure_table()
    except Exception:
        return _ok(empty)

    today = datetime.now(_SH_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    items: list[dict[str, Any]] = []
    summary = dict(empty["summary"])
    settings: list[dict[str, Any]] = []

    async with get_session(read_only=True) as db:
        summary_row = (
            await db.execute(
                text(
                    """
                    SELECT
                      COUNT(*) AS total,
                      COUNT(*) FILTER (WHERE status = 'success') AS success,
                      COUNT(*) FILTER (WHERE status = 'failed') AS failed,
                      COUNT(*) FILTER (WHERE status = 'skipped') AS skipped,
                      COUNT(*) FILTER (WHERE status = 'running') AS running,
                      COUNT(*) FILTER (WHERE created_at >= :today AND status = 'success') AS today_success,
                      COUNT(*) FILTER (WHERE created_at >= :today AND status = 'failed') AS today_failed,
                      COUNT(*) FILTER (WHERE created_at >= :today AND status = 'skipped') AS today_skipped,
                      COUNT(*) FILTER (WHERE created_at >= :today AND status = 'running') AS today_running,
                      MAX(created_at) AS latest_at
                    FROM qm_model_inference_dispatch_logs
                    """
                ),
                {"today": today},
            )
        ).one()
        summary = {
            "total": int(summary_row.total or 0),
            "success": int(summary_row.success or 0),
            "failed": int(summary_row.failed or 0),
            "skipped": int(summary_row.skipped or 0),
            "running": int(summary_row.running or 0),
            "today_success": int(summary_row.today_success or 0),
            "today_failed": int(summary_row.today_failed or 0),
            "today_skipped": int(summary_row.today_skipped or 0),
            "today_running": int(summary_row.today_running or 0),
            "latest_at": isoformat_dt(summary_row.latest_at),
        }

        where = ["1=1"]
        params: dict[str, Any] = {"limit": limit}
        if status_filter:
            where.append("status = :status")
            params["status"] = status_filter
        if user_filter:
            where.append("user_id = :user_id")
            params["user_id"] = user_filter
        if model_filter:
            where.append("model_id = :model_id")
            params["model_id"] = model_filter

        rows = (
            await db.execute(
                text(
                    f"""
                    SELECT id, trigger_source, tenant_id, user_id, strategy_id, model_id,
                           data_trade_date, prediction_trade_date, status, reason_code,
                           reason_detail, run_id, created_at
                    FROM qm_model_inference_dispatch_logs
                    WHERE {' AND '.join(where)}
                    ORDER BY created_at DESC
                    LIMIT :limit
                    """
                ),
                params,
            )
        ).all()
        items = [_row_item(row) for row in rows]

        settings: list[dict] = []  # 用户态「自动推理」开关已下线，不再展示 enabled 设置

    return _ok(
        {
            "schedule": schedule,
            "summary": summary,
            "settings": settings,
            "items": items,
        }
    )
