"""
Profile Service - 用户档案服务
"""

import logging
import os
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse

from sqlalchemy import select, text, update

from backend.services.api.user_app.config import settings
from backend.services.api.user_app.models.user import UserProfile
from backend.services.api.user_app.schemas.user import UserProfileUpdate
from backend.shared.database_manager_v2 import get_session
from backend.shared.redis_sentinel_client import get_redis_sentinel_client

logger = logging.getLogger(__name__)

# 默认头像路径
DEFAULT_AVATAR_URL = "/uploads/default_avatar.png"


class ProfileService:
    """用户档案服务"""

    _profile_table_checked = False
    _profile_columns_checked = False

    def __init__(self):
        self.redis_client = get_redis_sentinel_client()

    @staticmethod
    def _extract_candidate_file_key(value: str) -> str:
        """从输入中提取可能的文件 key（用于补全无路径 URL）。"""
        raw = str(value or "").strip()
        if not raw:
            return ""

        local_prefix = "/api/v1/files/local/"
        idx = raw.find(local_prefix)
        if idx >= 0:
            return raw[idx + len(local_prefix) :].lstrip("/")

        if raw.startswith("http://") or raw.startswith("https://"):
            try:
                parsed = urlparse(raw)
                if parsed.path and parsed.path != "/":
                    return parsed.path.lstrip("/")
                if parsed.query:
                    for chunk in parsed.query.split("&"):
                        if "=" not in chunk:
                            continue
                        k, v = chunk.split("=", 1)
                        if k in ("file_key", "key", "path") and v.strip():
                            return v.strip().lstrip("/")
            except Exception:
                return ""
            return ""

        return raw.lstrip("/")

    def _normalize_avatar_url(self, avatar_url: str | None) -> str | None:
        """统一头像URL到自定义COS域名，避免前端展示域名不一致。"""
        if avatar_url is None:
            return None

        value = str(avatar_url).strip()
        if value == "":
            return value

        cos_base = os.getenv("TENCENT_COS_URL", "").strip().rstrip("/")
        if not cos_base:
            return value

        # 明确 key/path（非 URL）时直接补全到自定义域名
        if not value.startswith("http://") and not value.startswith("https://"):
            file_key = self._extract_candidate_file_key(value)
            if file_key:
                return f"{cos_base}/{file_key}"

        local_prefix = "/api/v1/files/local/"
        idx = value.find(local_prefix)
        if idx >= 0:
            file_key = self._extract_candidate_file_key(value)
            return f"{cos_base}/{file_key}"

        try:
            parsed = urlparse(value)
            # 例如 quantmind-xxx.cos.ap-guangzhou.myqcloud.com
            if parsed.netloc and ".cos." in parsed.netloc and parsed.netloc.endswith(".myqcloud.com"):
                parsed_base = urlparse(cos_base)
                if parsed_base.scheme and parsed_base.netloc:
                    normalized_path = parsed.path or ""
                    if not normalized_path or normalized_path == "/":
                        file_key = self._extract_candidate_file_key(value)
                        normalized_path = f"/{file_key}" if file_key else ""
                    normalized_query = f"?{parsed.query}" if parsed.query else ""
                    if not normalized_path:
                        logger.warning("avatar_url missing object key, skip rewrite: %s", value)
                        return value
                    return f"{parsed_base.scheme}://{parsed_base.netloc}{normalized_path}{normalized_query}"
        except Exception:
            logger.warning("Invalid avatar_url format, keep raw value: %s", value)

        return value

    async def _ensure_profile_table(self, session) -> None:
        """
        确保 user_profiles 表存在（不存在时创建）
        """
        if not (settings.DEBUG or settings.AUTO_CREATE_PROFILE_TABLE):
            return

        if ProfileService._profile_table_checked:
            return

        try:
            # Use session connection directly to run sync DDL
            await session.connection().run_sync(
                lambda sync_conn: UserProfile.__table__.create(bind=sync_conn, checkfirst=True)
            )
            ProfileService._profile_table_checked = True

            if not ProfileService._profile_columns_checked:
                # 幂等补列：兼容历史库没有 ai_ide_api_key 字段的场景
                await session.execute(text("ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS ai_ide_api_key TEXT"))
                ProfileService._profile_columns_checked = True
        except Exception:
            logger.exception("Failed to ensure user_profiles table")
            # Don't raise, just log error to allow flow to continue if table exists
            # or if it's a connectivity issue that will be caught later
            pass

    async def get_profile(self, user_id: str, tenant_id: str, use_cache: bool = True) -> UserProfile | None:
        """获取用户档案"""
        # 从数据库查询（主库，必要时可创建表）
        async with get_session(read_only=False) as session:
            await self._ensure_profile_table(session)
            # 联查 User 表以获取用户名作为兜底
            from backend.services.api.user_app.models.user import User

            stmt = (
                select(UserProfile, User.username)
                .outerjoin(User, User.user_id == UserProfile.user_id)
                .where(
                    UserProfile.user_id == user_id,
                    UserProfile.tenant_id == tenant_id,
                )
            )
            result = await session.execute(stmt)
            row = result.first()

            if not row:
                return None

            profile, username = row
            # 动态附加 username 供 API 使用（Schema 会处理显示）
            # 虽然 UserProfile 没这个字段，但我们可以手动挂载，Pydantic from_orm 会尝试读取
            profile.username_at_runtime = username

            if profile:
                # 如果没有头像，使用默认头像
                if not profile.avatar_url:
                    profile.avatar_url = DEFAULT_AVATAR_URL
                else:
                    profile.avatar_url = self._normalize_avatar_url(profile.avatar_url)
                if use_cache:
                    # 缓存档案
                    cache_key = f"profile:{tenant_id}:{user_id}"
                    try:
                        self.redis_client.setex(
                            cache_key,
                            settings.CACHE_TTL_USER_PROFILE,
                            b"cached",  # 简化
                        )
                    except Exception:
                        logger.warning("Profile cache set failed", exc_info=True)

            return profile

    async def create_profile(self, user_id: str, tenant_id: str) -> UserProfile:
        """创建用户档案"""
        async with get_session(read_only=False) as session:
            await self._ensure_profile_table(session)
            # 显式设置默认值，确保 Pydantic 校验通过
            profile = UserProfile(
                user_id=user_id, tenant_id=tenant_id, trading_experience="intermediate", risk_tolerance="medium"
            )
            session.add(profile)
            await session.commit()
            await session.refresh(profile)

            logger.info(f"Profile created: {user_id}")
            return profile

    async def update_profile(
        self, user_id: str, tenant_id: str, profile_data: UserProfileUpdate
    ) -> UserProfile | None:
        """更新用户档案"""
        async with get_session(read_only=False) as session:
            await self._ensure_profile_table(session)
            result = await session.execute(
                select(UserProfile).where(
                    UserProfile.user_id == user_id,
                    UserProfile.tenant_id == tenant_id,
                )
            )
            profile = result.scalar_one_or_none()

            if not profile:
                # 如果档案不存在，创建一个
                profile = UserProfile(user_id=user_id, tenant_id=tenant_id)
                session.add(profile)

            # 更新字段
            update_data = profile_data.dict(exclude_unset=True)
            if "avatar_url" in update_data:
                update_data["avatar_url"] = self._normalize_avatar_url(update_data.get("avatar_url"))
            for key, value in update_data.items():
                if hasattr(profile, key):
                    setattr(profile, key, value)

            profile.updated_at = datetime.now()
            await session.commit()
            await session.refresh(profile)

            # 清除缓存
            cache_key = f"profile:{tenant_id}:{user_id}"
            try:
                self.redis_client.delete(cache_key)
            except Exception:
                logger.warning("Profile cache delete failed", exc_info=True)

            logger.info(f"Profile updated: {user_id}")
            return profile

    async def update_avatar(self, user_id: str, tenant_id: str, avatar_url: str) -> UserProfile | None:
        """更新头像"""
        normalized_avatar_url = self._normalize_avatar_url(avatar_url)
        async with get_session(read_only=False) as session:
            await self._ensure_profile_table(session)
            result = await session.execute(
                update(UserProfile)
                .where(
                    UserProfile.user_id == user_id,
                    UserProfile.tenant_id == tenant_id,
                )
                .values(avatar_url=normalized_avatar_url, updated_at=datetime.now())
                .returning(UserProfile)
            )
            await session.commit()
            profile = result.scalar_one_or_none()

            if profile:
                # 清除缓存
                cache_key = f"profile:{tenant_id}:{user_id}"
                try:
                    self.redis_client.delete(cache_key)
                except Exception:
                    logger.warning("Profile cache delete failed", exc_info=True)

                logger.info(f"Avatar updated: {user_id}")

            return profile

    async def update_trading_preferences(
        self,
        user_id: str,
        tenant_id: str,
        trading_experience: str | None = None,
        risk_tolerance: str | None = None,
        investment_goal: str | None = None,
    ) -> UserProfile | None:
        """更新交易偏好"""
        async with get_session(read_only=False) as session:
            await self._ensure_profile_table(session)
            result = await session.execute(
                select(UserProfile).where(
                    UserProfile.user_id == user_id,
                    UserProfile.tenant_id == tenant_id,
                )
            )
            profile = result.scalar_one_or_none()

            if not profile:
                return None

            if trading_experience:
                profile.trading_experience = trading_experience
            if risk_tolerance:
                profile.risk_tolerance = risk_tolerance
            if investment_goal:
                profile.investment_goal = investment_goal

            profile.updated_at = datetime.now()
            await session.commit()
            await session.refresh(profile)

            # 清除缓存
            cache_key = f"profile:{tenant_id}:{user_id}"
            try:
                self.redis_client.delete(cache_key)
            except Exception:
                logger.warning("Profile cache delete failed", exc_info=True)

            logger.info(f"Trading preferences updated: {user_id}")
            return profile
