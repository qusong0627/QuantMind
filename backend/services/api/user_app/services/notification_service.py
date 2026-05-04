"""
Notification Service
通知服务
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from sqlalchemy import delete, desc, func, or_, select, text, update
from sqlalchemy.exc import ProgrammingError

from backend.services.api.user_app.models.notification import Notification
from backend.shared.notification_metrics import (
    notification_unread_query_duration_seconds,
    timer,
)

logger = logging.getLogger(__name__)


class NotificationService:
    _indexes_ensured = False

    def __init__(self, session):
        self.session = session

    async def create_notification(
        self,
        user_id: str,
        tenant_id: str,
        title: str,
        content: str,
        type: str = "system",
        level: str = "info",
        action_url: str | None = None,
    ) -> Notification:
        """创建通知"""
        await self._ensure_notification_indexes()
        notification = Notification(
            user_id=user_id,
            tenant_id=tenant_id,
            title=title,
            content=content,
            type=type,
            level=level,
            action_url=action_url,
        )
        self.session.add(notification)
        await self.session.commit()
        return notification

    async def get_user_notifications(
        self,
        user_id: str,
        tenant_id: str,
        is_read: bool | None = None,
        days: int | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Notification], int, int, dict[str, int]]:
        """获取用户通知"""
        active_filter = (Notification.expires_at.is_(None)) | (Notification.expires_at > func.now())
        filters = [
            Notification.user_id == user_id,
            Notification.tenant_id == tenant_id,
            active_filter,
        ]
        unread_filter = or_(
            Notification.is_read.is_(False),
            Notification.is_read.is_(None),
        )
        unread_filters = [
            Notification.user_id == user_id,
            Notification.tenant_id == tenant_id,
            unread_filter,
            active_filter,
        ]

        if days is not None:
            start_at = datetime.now(timezone.utc) - timedelta(days=days)
            filters.append(Notification.created_at >= start_at)
            unread_filters.append(Notification.created_at >= start_at)

        if is_read is not None:
            filters.append(Notification.is_read.is_(True) if is_read else unread_filter)

        stmt = select(Notification).where(*filters).order_by(desc(Notification.created_at)).limit(limit).offset(offset)
        total_stmt = select(func.count(Notification.id)).where(*filters)
        unread_stmt = select(func.count(Notification.id)).where(*unread_filters)

        # 统计未读分类分布 (确保分类图标总和 = 未读总数)
        stats_stmt = select(Notification.type, func.count(Notification.id)).where(*unread_filters).group_by(Notification.type)

        with timer(notification_unread_query_duration_seconds):
            try:
                result = await self.session.execute(stmt)
                notifications = result.scalars().all()
                total = int((await self.session.execute(total_stmt)).scalar() or 0)
                unread_count = int((await self.session.execute(unread_stmt)).scalar() or 0)

                # 获取未读分类汇总结果
                stats_res = await self.session.execute(stats_stmt)
                unread_type_counts = {str(row[0]): int(row[1]) for row in stats_res.all()}

            except ProgrammingError as e:
                # 兼容开发环境未执行通知表 migration 的场景，避免前端 500 风暴
                if self._is_missing_notifications_table_error(e):
                    logger.warning("notifications table missing, return empty list as fallback")
                    return [], 0, 0, {}
                raise

        return notifications, total, unread_count, unread_type_counts

    async def mark_as_read(self, notification_id: int, user_id: str, tenant_id: str) -> bool:
        """标记已读"""
        unread_filter = or_(
            Notification.is_read.is_(False),
            Notification.is_read.is_(None),
        )
        stmt = (
            update(Notification)
            .where(
                Notification.id == notification_id,
                Notification.user_id == user_id,
                Notification.tenant_id == tenant_id,
                unread_filter,
            )
            .values(is_read=True, read_at=datetime.now())
        )
        try:
            await self._ensure_notification_indexes()
            result = await self.session.execute(stmt)
            await self.session.commit()
            return result.rowcount > 0
        except ProgrammingError as e:
            if self._is_missing_notifications_table_error(e):
                logger.warning("notifications table missing, skip mark_as_read")
                return False
            raise

    async def _ensure_notification_indexes(self) -> None:
        if NotificationService._indexes_ensured:
            return
        try:
            await self.session.execute(text("""
                    CREATE INDEX IF NOT EXISTS idx_notifications_tenant_user_created_at
                    ON notifications (tenant_id, user_id, created_at DESC)
                    """))
            await self.session.execute(text("""
                    CREATE INDEX IF NOT EXISTS idx_notifications_tenant_user_read_created_at
                    ON notifications (tenant_id, user_id, is_read, created_at DESC)
                    """))
            await self.session.commit()
            NotificationService._indexes_ensured = True
        except ProgrammingError as e:
            await self.session.rollback()
            if self._is_missing_notifications_table_error(e):
                logger.warning("notifications table missing, skip ensure indexes")
                return
            raise
        except Exception:
            await self.session.rollback()
            raise

    async def mark_all_as_read(self, user_id: str, tenant_id: str) -> int:
        """全部已读"""
        stmt = (
            update(Notification)
            .where(
                Notification.user_id == user_id,
                Notification.tenant_id == tenant_id,
                or_(
                    Notification.is_read.is_(False),
                    Notification.is_read.is_(None),
                ),
            )
            .values(is_read=True, read_at=datetime.now())
        )
        try:
            await self._ensure_notification_indexes()
            result = await self.session.execute(stmt)
            await self.session.commit()
            return result.rowcount
        except ProgrammingError as e:
            if self._is_missing_notifications_table_error(e):
                logger.warning("notifications table missing, skip mark_all_as_read")
                return 0
            raise

    async def clear_notifications(
        self,
        user_id: str,
        tenant_id: str,
        days: int | None = None,
    ) -> int:
        """删除通知（支持按天数窗口删除）"""
        active_filter = (Notification.expires_at.is_(None)) | (Notification.expires_at > func.now())
        filters = [
            Notification.user_id == user_id,
            Notification.tenant_id == tenant_id,
            active_filter,
        ]

        if days is not None:
            start_at = datetime.now(timezone.utc) - timedelta(days=days)
            filters.append(Notification.created_at >= start_at)

        stmt = delete(Notification).where(*filters)
        try:
            await self._ensure_notification_indexes()
            result = await self.session.execute(stmt)
            await self.session.commit()
            return int(result.rowcount or 0)
        except ProgrammingError as e:
            if self._is_missing_notifications_table_error(e):
                logger.warning("notifications table missing, skip clear_notifications")
                return 0
            raise

    @staticmethod
    def _is_missing_notifications_table_error(error: ProgrammingError) -> bool:
        msg = str(error).lower()
        return "notifications" in msg and ("undefinedtable" in msg or "does not exist" in msg or "relation" in msg)
