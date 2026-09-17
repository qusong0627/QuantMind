"""哨兵告警 API（T-P6-15）：留痕查询 / 人工标注 / 误报率报表（`sentinel_alerts` 唯一读面）。

- `GET  /api/v1/sentinel/alerts`              列表（筛选+分页；含 T+1 回填结果与标注）
- `POST /api/v1/sentinel/alerts/{id}/annotate` 人工标注（true_positive/false_positive + 备注）
- `GET  /api/v1/sentinel/report`              误报率报表（口径见下，纯函数可测）
- `GET  /api/v1/sentinel/status`              消费服务计数（Redis 镜像）

**误报率口径（细案 §六.4，首版）**：分母 = 已兑现（outcome_status='filled' 且有方向）的
告警；单条有效命中 = 人工标注优先（true_positive→1 / false_positive→0），否则用自动回填
的 hit；误报率 = 1 − mean(有效命中)。pending/no_data/not_scorable 不进分母（如实单列）。
达标线建议 ≤30%（`threshold_ok` 随报表返回；先跑数再收紧）。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text

from backend.services.api.user_app.middleware.auth import get_current_user
from backend.shared.database_manager_v2 import get_session

logger = logging.getLogger(__name__)
_CST = timezone(timedelta(hours=8))

router = APIRouter(prefix="/api/v1/sentinel", tags=["Sentinel"])

MISS_RATE_TARGET = 0.30  # §六.4 首版建议线（≤30%）
ANNOTATIONS = ("true_positive", "false_positive")


class AnnotateRequest(BaseModel):
    annotation: str = Field(..., description="true_positive | false_positive")
    note: str = ""


def effective_hit(hit: bool | None, annotation: str | None) -> bool | None:
    """单条有效命中：人工标注优先，其次自动回填。"""
    if annotation == "true_positive":
        return True
    if annotation == "false_positive":
        return False
    return hit


def build_report(rows: list[dict[str, Any]], *, days: int) -> dict[str, Any]:
    """误报率报表（纯函数）：整体 + 按类型分组；分母=已兑现且有方向。"""
    def _group_stats(group: list[dict[str, Any]]) -> dict[str, Any]:
        filled = [r for r in group if r["outcome_status"] == "filled" and r["hit"] is not None]
        annotated_tp = sum(1 for r in group if r["annotation"] == "true_positive")
        annotated_fp = sum(1 for r in group if r["annotation"] == "false_positive")
        effective = [effective_hit(r["hit"], r["annotation"]) for r in filled]
        effective = [e for e in effective if e is not None]
        hits = sum(1 for e in effective if e)
        miss_rate = (1.0 - hits / len(effective)) if effective else None
        return {
            "total": len(group),
            "pushed": sum(1 for r in group if r["pushed"]),
            "filled": len(filled),
            "pending": sum(1 for r in group if r["outcome_status"] == "pending"),
            "no_data": sum(1 for r in group if r["outcome_status"] == "no_data"),
            "not_scorable": sum(1 for r in group if r["outcome_status"] == "not_scorable"),
            "hit": hits,
            "miss": len(effective) - hits,
            "miss_rate": round(miss_rate, 4) if miss_rate is not None else None,
            "annotated_true_positive": annotated_tp,
            "annotated_false_positive": annotated_fp,
            "threshold_ok": (miss_rate is not None and miss_rate <= MISS_RATE_TARGET),
        }

    overall = _group_stats(rows)
    by_type: dict[str, dict[str, Any]] = {}
    for row in rows:
        by_type.setdefault(str(row["alert_type"]), []).append(row)
    return {
        "window_days": days,
        "generated_at": datetime.now(_CST).isoformat(),
        "miss_rate_target": MISS_RATE_TARGET,
        "caliber": (
            "误报率 = 1 − mean(有效命中)；有效命中=人工标注优先(true_positive/false_positive)，"
            "否则自动 T+1 回填 hit；分母=outcome_status='filled' 且有方向的告警"
        ),
        "overall": overall,
        "by_type": {k: _group_stats(v) for k, v in sorted(by_type.items())},
    }


@router.get("/alerts")
async def list_alerts(
    days: int = Query(14, ge=1, le=180),
    alert_type: str = Query("", description="如 news:risk_event / anomaly:volume_surge"),
    severity: str = Query(""),
    market: str = Query(""),
    symbol: str = Query(""),
    outcome_status: str = Query(""),
    annotated: str = Query("", description="true=有标注 / false=无标注 / ''=全部"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    current_user: dict = Depends(get_current_user),
):
    """告警列表（按时间倒序；全量留痕可查）。"""
    since = (datetime.now(_CST) - timedelta(days=days)).date()
    filters = ["trade_date >= :since"]
    params: dict[str, Any] = {"since": since, "lim": limit, "off": offset}
    for col, value in (("alert_type", alert_type), ("severity", severity),
                       ("market", market), ("outcome_status", outcome_status)):
        if value.strip():
            filters.append(f"{col} = :{col}")
            params[col] = value.strip()
    if symbol.strip():
        filters.append("(symbol = :sym OR targets @> CAST(:tg AS JSONB))")
        params["sym"] = symbol.strip().upper()
        params["tg"] = f'["{symbol.strip().upper()}"]'
    if annotated.strip().lower() in {"true", "1"}:
        filters.append("annotation IS NOT NULL")
    elif annotated.strip().lower() in {"false", "0"}:
        filters.append("annotation IS NULL")
    where = " AND ".join(filters)
    async with get_session(read_only=True) as session:
        total = (
            await session.execute(
                text(f"SELECT count(*) FROM sentinel_alerts WHERE {where}"), params
            )
        ).scalar()
        rows = (
            await session.execute(
                text(
                    "SELECT alert_id::text, ts::text, trade_date::text, market, symbol, targets, "
                    "       alert_type, severity, source, title, pushed, push_reason, direction, "
                    "       outcome_status, realized_return, benchmark_return, excess_return, hit, "
                    "       annotation, annotated_at::text, annotation_note "
                    f"FROM sentinel_alerts WHERE {where} "
                    "ORDER BY ts DESC LIMIT :lim OFFSET :off"
                ),
                params,
            )
        ).fetchall()
    items = [
        {
            "alert_id": r[0], "ts": r[1], "trade_date": r[2], "market": r[3], "symbol": r[4],
            "targets": list(r[5] or []), "alert_type": r[6], "severity": r[7], "source": r[8],
            "title": r[9], "pushed": bool(r[10]), "push_reason": r[11], "direction": r[12],
            "outcome_status": r[13], "realized_return": r[14], "benchmark_return": r[15],
            "excess_return": r[16], "hit": r[17], "annotation": r[18],
            "annotated_at": r[19], "annotation_note": r[20],
        }
        for r in rows
    ]
    return {"success": True, "data": {"items": items, "total": int(total or 0),
                                      "limit": limit, "offset": offset}}


@router.post("/alerts/{alert_id}/annotate")
async def annotate_alert(
    alert_id: str,
    payload: AnnotateRequest,
    current_user: dict = Depends(get_current_user),
):
    """人工标注（误判纠正入口；标注即参与误报率口径）。"""
    annotation = payload.annotation.strip().lower()
    if annotation not in ANNOTATIONS:
        raise HTTPException(status_code=400, detail=f"annotation 必须为 {ANNOTATIONS}")
    user_id = current_user.get("user_id") or current_user.get("id") or 0
    try:
        annotated_by = int(str(user_id))
    except (TypeError, ValueError):
        annotated_by = 0
    async with get_session(read_only=False) as session:
        result = await session.execute(
            text(
                "UPDATE sentinel_alerts SET annotation=:a, annotated_by=:u, annotated_at=NOW(), "
                "annotation_note=:n WHERE alert_id = CAST(:aid AS UUID)"
            ),
            {"a": annotation, "u": annotated_by, "n": payload.note[:1000], "aid": alert_id},
        )
        await session.commit()
    if not result.rowcount:
        raise HTTPException(status_code=404, detail="告警不存在")
    return {"success": True, "data": {"alert_id": alert_id, "annotation": annotation}}


@router.get("/report")
async def sentinel_report(
    days: int = Query(30, ge=1, le=365),
    current_user: dict = Depends(get_current_user),
):
    """误报率报表（口径见模块 docstring；分母=已兑现且有方向）。"""
    since = (datetime.now(_CST) - timedelta(days=days)).date()
    async with get_session(read_only=True) as session:
        rows = (
            await session.execute(
                text(
                    "SELECT alert_type, severity, pushed, outcome_status, hit, annotation "
                    "FROM sentinel_alerts WHERE trade_date >= :since"
                ),
                {"since": since},
            )
        ).fetchall()
    data = build_report(
        [
            {"alert_type": r[0], "severity": r[1], "pushed": bool(r[2]),
             "outcome_status": r[3], "hit": r[4], "annotation": r[5]}
            for r in rows
        ],
        days=days,
    )
    return {"success": True, "data": data}


@router.get("/status")
async def sentinel_status(current_user: dict = Depends(get_current_user)):
    """消费服务计数（Redis 镜像；服务内部计数器）。"""
    import json

    try:
        import os

        import redis as _redis

        client = _redis.Redis(
            host=os.getenv("REDIS_HOST") or "redis",
            port=int(os.getenv("REDIS_PORT", "6379")),
            password=os.getenv("REDIS_PASSWORD") or None,
            db=int(os.getenv("REDIS_DB_GENERAL", "0")),
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=3,
        )
        try:
            raw = client.hgetall("qm:sentinel:status") or {}
        finally:
            client.close()
        counters = json.loads(raw.get("counters") or "{}")
        return {"success": True, "data": {"last_build_at": raw.get("last_build_at"), "counters": counters}}
    except Exception as exc:  # noqa: BLE001
        return {"success": True, "data": {"readable": False, "error": str(exc)[:200]}}
