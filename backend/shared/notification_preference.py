"""
Notification Preference Service
通知偏好服务

检查用户的通知偏好设置，决定是否发送特定类型的通知。
"""

import logging
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

DEFAULT_PREFERENCES = {
    "strategy_alerts": True,
    "trading_alerts": True,
    "system_announcements": True,
    "portfolio_updates": True,
}


async def get_user_notification_preferences(
    session: AsyncSession,
    user_id: str,
    tenant_id: str = "default",
) -> dict:
    """
    获取用户通知偏好设置

    返回默认偏好 + 用户自定义偏好的合并结果
    """
    try:
        from backend.services.api.user_app.models.user import User

        stmt = select(User).where(
            User.id == int(user_id),
            User.tenant_id == tenant_id,
        )
        result = await session.execute(stmt)
        user = result.scalar_one_or_none()

        if user and hasattr(user, "notification_settings"):
            user_settings = user.notification_settings or {}
            return {**DEFAULT_PREFERENCES, **user_settings}
    except Exception as e:
        logger.warning(
            "Failed to fetch user notification preferences for user=%s: %s",
            user_id,
            e,
        )

    return DEFAULT_PREFERENCES.copy()


async def should_send_notification(
    session: AsyncSession,
    user_id: str,
    tenant_id: str,
    notification_type: str,
) -> bool:
    """
    检查是否应该发送特定类型的通知

    Args:
        session: 数据库会话
        user_id: 用户ID
        tenant_id: 租户ID
        notification_type: 通知类型 (strategy, trading, system, portfolio)

    Returns:
        bool: 是否应该发送通知
    """
    type_to_preference = {
        "strategy": "strategy_alerts",
        "trading": "trading_alerts",
        "system": "system_announcements",
        "portfolio": "portfolio_updates",
        "market": "portfolio_updates",
        # 体检复检告警归策略提醒开关（T-P4-06 通知接线）
        "health": "strategy_alerts",
    }

    preference_key = type_to_preference.get(notification_type)
    if not preference_key:
        return True

    try:
        prefs = await get_user_notification_preferences(session, user_id, tenant_id)
        return bool(prefs.get(preference_key, True))
    except Exception as e:
        logger.warning(
            "Failed to check notification preference for user=%s type=%s: %s",
            user_id,
            notification_type,
            e,
        )
        return True


def should_send_notification_sync(
    user_id: str,
    tenant_id: str,
    notification_type: str,
) -> bool:
    """
    同步版本的通知偏好检查（用于非异步上下文）

    默认返回 True，避免阻塞主流程
    """
    return True
