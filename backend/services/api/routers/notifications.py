"""
Notification API Routes
通知中心API
"""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, field_validator

from backend.services.api.user_app.middleware.auth import get_current_user, require_admin
from backend.services.api.user_app.services import NotificationService
from backend.shared.database_manager_v2 import get_session

router = APIRouter(prefix="/notifications", tags=["通知中心"])


class NotificationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: str
    tenant_id: str
    title: str
    content: str
    type: str
    level: str
    action_url: str | None = None
    is_read: bool = False
    read_at: datetime | None = None
    created_at: datetime
    expires_at: datetime | None = None
    market: str | None = None

    @field_validator("is_read", mode="before")
    @classmethod
    def default_is_read(cls, value):
        return False if value is None else value


class NotificationListData(BaseModel):
    items: list[NotificationResponse]
    total: int
    unread_count: int
    type_counts: dict[str, int]
    has_more: bool


class NotificationListEnvelope(BaseModel):
    code: int
    message: str = "ok"
    data: NotificationListData


class SystemAnnouncementRequest(BaseModel):
    title: str
    content: str
    level: str = "info"
    type: str = "system"
    action_url: str | None = None
    user_id: str
    tenant_id: str = "default"
    # 缺省全局公告；指定市场仅该市场可见
    market: str | None = None


class ClearNotificationsRequest(BaseModel):
    days: int | None = None


@router.get("", response_model=NotificationListEnvelope)
async def list_notifications(
    is_read: bool | None = Query(None),
    days: int | None = Query(None, ge=1, le=30),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    market: str | None = Query(
        None,
        description="按市场取数（含全局通知）：CN/HK/US/FUTURES/CRYPTO/CUSTOM",
    ),
    current_user: dict = Depends(get_current_user),
):
    """
    获取通知列表
    """
    async with get_session(read_only=True) as session:
        service = NotificationService(session)
        notifications, total, unread_count, type_counts = await service.get_user_notifications(
            user_id=current_user["user_id"],
            tenant_id=current_user["tenant_id"],
            is_read=is_read,
            days=days,
            limit=limit,
            offset=offset,
            market=market,
        )
        items = [NotificationResponse.model_validate(n) for n in notifications]
        return NotificationListEnvelope(
            code=200,
            data=NotificationListData(
                items=items,
                total=total,
                unread_count=unread_count,
                type_counts=type_counts,
                has_more=(offset + len(items)) < total,
            ),
        )


@router.post("/{notification_id}/read")
async def mark_read(
    notification_id: int,
    current_user: dict = Depends(get_current_user),
):
    """
    标记通知为已读
    """
    async with get_session(read_only=False) as session:
        service = NotificationService(session)
        await service.mark_as_read(
            notification_id=notification_id,
            user_id=current_user["user_id"],
            tenant_id=current_user["tenant_id"],
        )

    return {"code": 200, "message": "已标记"}


@router.post("/read-all")
async def mark_all_read(
    current_user: dict = Depends(get_current_user),
):
    """
    全部标记为已读
    """
    async with get_session(read_only=False) as session:
        service = NotificationService(session)
        count = await service.mark_all_as_read(
            user_id=current_user["user_id"],
            tenant_id=current_user["tenant_id"],
        )

    return {"code": 200, "message": f"已标记 {count} 条通知", "data": {"count": count}}


@router.post("/clear")
async def clear_notifications(
    payload: ClearNotificationsRequest,
    current_user: dict = Depends(get_current_user),
):
    """
    清空通知（可按最近 N 天窗口）
    """
    days = payload.days
    if days is not None:
        if days < 1 or days > 30:
            raise HTTPException(status_code=400, detail="days 必须在 1-30 之间")

    async with get_session(read_only=False) as session:
        service = NotificationService(session)
        count = await service.clear_notifications(
            user_id=current_user["user_id"],
            tenant_id=current_user["tenant_id"],
            days=days,
        )

    if days is None:
        return {"code": 200, "message": f"已清除 {count} 条通知", "data": {"count": count}}
    return {"code": 200, "message": f"已清除最近 {days} 天内的 {count} 条通知", "data": {"count": count}}


@router.post("/system-announcement")
async def create_system_announcement(
    payload: SystemAnnouncementRequest,
    _: dict = Depends(require_admin),
):
    """
    管理员手工发布系统公告（前端本期不曝光）。
    """
    async with get_session(read_only=False) as session:
        service = NotificationService(session)
        created = await service.create_notification(
            user_id=payload.user_id,
            tenant_id=payload.tenant_id,
            title=payload.title,
            content=payload.content,
            type=payload.type,
            level=payload.level,
            action_url=payload.action_url,
            market=payload.market,
        )

    return {"code": 200, "message": "公告已发布", "data": {"id": created.id}}
