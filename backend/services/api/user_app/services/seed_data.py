"""
Seed Data Service
初始化默认数据
"""

import asyncio
import logging

from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.api.user_app.models.user import User, UserProfile
from backend.services.api.user_app.services.rbac_service import (
    RBACService,
    init_default_roles_and_permissions,
)

logger = logging.getLogger(__name__)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# 默认显示名（user_profiles.display_name，前端「用户名」位置展示的就是它）
DEFAULT_ADMIN_DISPLAY_NAME = "QuantMind"
# 旧默认显示名，命中才升级，用户自行修改过的昵称不动
LEGACY_ADMIN_DISPLAY_NAME = "System Administrator"
# 默认头像：electron/public/logo.png，构建后位于 Web 根目录，故用同源相对路径
DEFAULT_ADMIN_AVATAR_URL = "/logo.png"


async def init_admin_data(db: AsyncSession):
    """初始化管理员数据"""
    logger.info("Starting admin data initialization...")
    default_tenant_id = "default"

    # 1. 确保RBAC角色存在
    await init_default_roles_and_permissions(db)

    # 2. 检查管理员用户是否存在
    stmt = select(User).where(
        User.username == "admin",
        User.tenant_id == default_tenant_id,
    )
    result = await db.execute(stmt)
    admin_user = result.scalar_one_or_none()

    # 2.1 历史坏数据自愈：admin 规范 ID 为 10000001（不再用 admin / 00000001）。
    # 用户名登录不受影响，但 JWT sub 与全链路 user_id 口径会错。
    from backend.shared.admin_identity import ADMIN_USER_ID, fix_admin_user_id

    if admin_user is not None and admin_user.user_id != ADMIN_USER_ID:
        logger.warning(
            "检测到 admin user_id=%r，纠正为 %s", admin_user.user_id, ADMIN_USER_ID
        )
        # 必须先结束当前事务再纠正：上面的 SELECT 让本 session 持有 users 表上的锁/
        # 事务快照，而 fix_admin_user_id 会在**新连接**上执行 ALTER TABLE（ACCESS
        # EXCLUSIVE）——自己持锁、新连接等锁，同进程两连接互等，启动期直接死锁
        # （曾表现为卡在 Master database health check 后、子服务不拉起）。
        await db.commit()
        await fix_admin_user_id()
        await db.expire_all()
        result = await db.execute(stmt)
        admin_user = result.scalar_one_or_none()

    import bcrypt

    if not admin_user:
        logger.info("Creating default admin user...")
        # Use bcrypt directly to avoid passlib compatibility issues with newer bcrypt versions
        # admin123 hash
        hashed = bcrypt.hashpw(b"admin123", bcrypt.gensalt()).decode("utf-8")

        admin_user = User(
            user_id=ADMIN_USER_ID,
            tenant_id=default_tenant_id,
            username="admin",
            email="admin@quantmind.com",
            password_hash=hashed,
            is_active=True,
            is_verified=True,
            is_admin=True,
            is_deleted=False,
        )
        db.add(admin_user)
        await db.commit()
        await db.refresh(admin_user)

        # 分配管理员角色
        rbac_service = RBACService(db)
        admin_role = await rbac_service.get_role_by_code("admin")
        if admin_role:
            # Check if already has role (unlikely for new user but good practice)
            roles = await rbac_service.get_user_roles(admin_user.user_id)
            if not any(r.code == "admin" for r in roles):
                await rbac_service.add_role_to_user(admin_user.user_id, admin_role.id)

    # 3. 检查/更新管理员档案
    stmt = select(UserProfile).where(
        UserProfile.user_id == admin_user.user_id,
        UserProfile.tenant_id == default_tenant_id,
    )
    result = await db.execute(stmt)
    profile = result.scalar_one_or_none()

    default_preferences = {
        "theme": "dark",
        "language": "zh-CN",
        "dashboard_layout": "default",
    }

    default_notifications = {"email": True, "push": True, "marketing": False}

    if not profile:
        logger.info("Creating admin profile...")
        profile = UserProfile(
            user_id=admin_user.user_id,
            tenant_id=default_tenant_id,
            display_name=DEFAULT_ADMIN_DISPLAY_NAME,
            bio="QuantMind 系统管理员",
            location="Server Room",
            avatar_url=DEFAULT_ADMIN_AVATAR_URL,
            trading_experience="advanced",
            risk_tolerance="high",
            preferences=default_preferences,
            notification_settings=default_notifications,
        )
        db.add(profile)
    else:
        # Update if fields are missing
        if not profile.preferences:
            profile.preferences = default_preferences
        if not profile.notification_settings:
            profile.notification_settings = default_notifications
        # 默认显示名升级：仅命中旧默认值时替换
        if (profile.display_name or "").strip() == LEGACY_ADMIN_DISPLAY_NAME:
            profile.display_name = DEFAULT_ADMIN_DISPLAY_NAME
        # 默认头像升级：空头像或旧的 ui-avatars 外链统一换成站内 Logo 相对路径
        if not profile.avatar_url or profile.avatar_url.startswith("https://ui-avatars.com/"):
            profile.avatar_url = DEFAULT_ADMIN_AVATAR_URL

    await db.commit()
    logger.info("Admin data initialization completed.")


if __name__ == "__main__":
    # Allow running directly
    import os

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    async def main():
        # Use DATABASE_URL from environment variable
        db_url = os.getenv("DATABASE_URL")
        if not db_url:
            raise ValueError("DATABASE_URL environment variable is not set")

        engine = create_async_engine(db_url)
        async_session = async_sessionmaker(engine, expire_on_commit=False)
        async with async_session() as session:
            await init_admin_data(session)
        await engine.dispose()

    asyncio.run(main())
