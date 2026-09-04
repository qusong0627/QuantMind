"""管理员 - 最近事件（系统运行事件）查询。

读取 ``system_events`` 表（由 backend/shared/system_events.py 写入），
提供分页/过滤查询与统计，供管理后台「最近事件」时间线使用。
仅 admin 可访问（router 级认证兜底，同 node_history.py 模式）。
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import text

from backend.services.api.user_app.middleware.auth import require_admin
from backend.shared.database_manager_v2 import get_session

router = APIRouter(dependencies=[Depends(require_admin)])


class SystemEvent(BaseModel):
    id: int
    event_type: str
    level: str
    source: str
    title: str
    message: str | None = None
    meta: dict | None = Field(default_factory=dict)
    created_at: datetime


@router.get("", response_model=list[SystemEvent])
async def list_system_events(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    event_type: str | None = Query(default=None, description="按事件类型过滤"),
    level: str | None = Query(default=None, description="按等级过滤 info/warning/error/critical"),
    source: str | None = Query(default=None, description="按来源服务过滤"),
    start_time: datetime | None = Query(default=None, description="起始时间(含)"),
    end_time: datetime | None = Query(default=None, description="结束时间(含)"),
):
    """返回系统运行事件，新到旧分页。"""
    conds: list[str] = []
    params: dict = {"limit": limit, "offset": offset}
    if event_type:
        conds.append("event_type = :event_type")
        params["event_type"] = event_type
    if level:
        conds.append("level = :level")
        params["level"] = level
    if source:
        conds.append("source = :source")
        params["source"] = source
    if start_time:
        conds.append("created_at >= :start_time")
        params["start_time"] = start_time
    if end_time:
        conds.append("created_at <= :end_time")
        params["end_time"] = end_time
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    sql = text(
        f"""SELECT id, event_type, level, source, title, message, meta, created_at
            FROM system_events
            {where}
            ORDER BY created_at DESC, id DESC
            LIMIT :limit OFFSET :offset"""
    )
    async with get_session(read_only=True) as session:
        rows = (await session.execute(sql, params)).mappings().all()
    return [
        SystemEvent(
            id=r["id"],
            event_type=r["event_type"],
            level=r["level"],
            source=r["source"],
            title=r["title"],
            message=r["message"],
            meta=r["meta"] or {},
            created_at=r["created_at"],
        )
        for r in rows
    ]


@router.get("/stats")
async def system_events_stats(
    hours: int = Query(default=24, ge=1, le=24 * 30, description="统计最近 N 小时"),
    group_by: str = Query(default="level", pattern="^(level|event_type)$"),
):
    """按等级/事件类型统计最近 N 小时事件数，供概览计数。"""
    col = "level" if group_by == "level" else "event_type"
    sql = text(
        f"""SELECT {col} AS bucket, count(*) AS total
            FROM system_events
            WHERE created_at >= now() - make_interval(hours => :hours)
            GROUP BY {col}
            ORDER BY total DESC"""
    )
    async with get_session(read_only=True) as session:
        rows = (await session.execute(sql, {"hours": hours})).mappings().all()
    return [
        {"bucket": r["bucket"], "total": r["total"]}
        for r in rows
        if r["bucket"] is not None
    ]
