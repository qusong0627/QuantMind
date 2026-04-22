"""
Unified notification publisher (best-effort).

用于在 trade / engine 等服务内快速写入用户通知，不阻塞主流程。
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, ProgrammingError

from backend.shared.notification_metrics import (
    inc_counter,
    notification_event_push_total,
    notification_publish_total,
)

try:
    from backend.shared.database_pool import get_db
except Exception:  # pragma: no cover
    get_db = None  # type: ignore

try:
    import redis
except Exception:  # pragma: no cover
    redis = None  # type: ignore

logger = logging.getLogger(__name__)

_ALLOWED_TYPES = {"system", "trading", "market", "strategy"}
_ALLOWED_LEVELS = {"info", "warning", "error", "success"}
NOTIFICATION_EVENTS_STREAM = "notification_events"


def _build_sync_redis_client():
    if redis is None:
        return None

    try:
        use_sentinel = str(os.getenv("REDIS_USE_SENTINEL", "")).strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        password = os.getenv("REDIS_PASSWORD") or None
        if use_sentinel:
            from redis.sentinel import Sentinel

            sentinels_raw = os.getenv("REDIS_SENTINELS", "")
            sentinels = []
            for item in sentinels_raw.split(","):
                host_port = item.strip()
                if not host_port:
                    continue
                host, _, port = host_port.partition(":")
                sentinels.append((host.strip(), int(port or "26379")))
            if sentinels:
                sentinel = Sentinel(
                    sentinels,
                    socket_timeout=0.5,
                    password=password,
                )
                return sentinel.master_for(
                    os.getenv("REDIS_MASTER_NAME", "mymaster"),
                    socket_timeout=1.0,
                    password=password,
                    db=0,
                    decode_responses=True,
                )

        return redis.Redis(
            host=os.getenv("REDIS_HOST", "localhost"),
            port=int(os.getenv("REDIS_PORT", "6379")),
            password=password,
            db=0,
            decode_responses=True,
            socket_connect_timeout=3,
            socket_timeout=5,
        )
    except Exception as exc:
        logger.warning("notification event redis client init failed: %s", exc)
        return None


def _push_notification_event(payload: dict) -> bool:
    client = _build_sync_redis_client()
    if client is None:
        inc_counter(notification_event_push_total, "skipped")
        return False

    try:
        normalized = {key: "" if value is None else str(value) for key, value in payload.items()}
        client.xadd(NOTIFICATION_EVENTS_STREAM, normalized, maxlen=10000, approximate=True)
        inc_counter(notification_event_push_total, "success")
        return True
    except Exception as exc:
        inc_counter(notification_event_push_total, "failed")
        logger.warning(
            "notification event push failed: user_id=%s tenant_id=%s type=%s title=%s error=%s",
            payload.get("user_id"),
            payload.get("tenant_id"),
            payload.get("type"),
            payload.get("title"),
            exc,
        )
        return False
    finally:
        try:
            client.close()
        except Exception:
            pass


def _safe_type(value: str) -> str:
    text_value = str(value or "").strip().lower()
    return text_value if text_value in _ALLOWED_TYPES else "system"


def _safe_level(value: str) -> str:
    text_value = str(value or "").strip().lower()
    return text_value if text_value in _ALLOWED_LEVELS else "info"


def _looks_like_missing_table_error(error: ProgrammingError) -> bool:
    msg = str(error).lower()
    return "notifications" in msg and ("undefinedtable" in msg or "does not exist" in msg or "relation" in msg)


def _looks_like_user_fk_violation(error: IntegrityError) -> bool:
    msg = str(error).lower()
    return "notifications_user_id_fkey" in msg or ("foreign key" in msg and "notifications" in msg and "user_id" in msg)


def publish_notification(
    *,
    user_id: str,
    tenant_id: str,
    title: str,
    content: str,
    type: str = "system",
    level: str = "info",
    action_url: str | None = None,
    expire_days: int | None = None,
) -> bool:
    """
    同步发布通知。失败时返回 False，不抛出异常阻断主业务。
    """
    if get_db is None:
        logger.warning("notification publish skipped: database pool unavailable")
        inc_counter(notification_publish_total, "skipped")
        return False

    uid = str(user_id or "").strip()
    tid = str(tenant_id or "default").strip() or "default"
    if not uid:
        logger.warning("notification publish skipped: empty user_id")
        inc_counter(notification_publish_total, "skipped")
        return False

    now = datetime.now(timezone.utc)
    expires_at = None
    if isinstance(expire_days, int) and expire_days > 0:
        expires_at = now + timedelta(days=expire_days)

    params = {
        "user_id": uid,
        "tenant_id": tid,
        "title": str(title or "系统通知")[:128],
        "content": str(content or "").strip()[:4000],
        "type": _safe_type(type),
        "level": _safe_level(level),
        "action_url": str(action_url).strip()[:512] if action_url else None,
        "expires_at": expires_at,
    }

    sql = text("""
        INSERT INTO notifications (
            user_id, tenant_id, title, content, notification_type, level, action_url, expires_at
        )
        VALUES (
            :user_id, :tenant_id, :title, :content, :type, :level, :action_url, :expires_at
        )
        RETURNING id, created_at
        """)
    try:
        with get_db() as session:
            inserted = session.execute(sql, params).mappings().first()
            session.commit()
        notification_id = inserted["id"] if inserted else None
        created_at = inserted["created_at"] if inserted else now
        logger.info(
            "notification published: tenant_id=%s user_id=%s type=%s level=%s title=%s",
            tid,
            uid,
            params["type"],
            params["level"],
            params["title"],
        )
        inc_counter(notification_publish_total, "success")
        _push_notification_event(
            {
                "notification_id": notification_id,
                "user_id": uid,
                "tenant_id": tid,
                "title": params["title"],
                "content": params["content"],
                "type": params["type"],
                "level": params["level"],
                "action_url": params["action_url"],
                "created_at": (
                    created_at.isoformat() if hasattr(created_at, "isoformat") else str(created_at or now.isoformat())
                ),
            }
        )
        return True
    except ProgrammingError as exc:
        if _looks_like_missing_table_error(exc):
            logger.warning("notification table missing, publish skipped")
            inc_counter(notification_publish_total, "skipped")
            return False
        inc_counter(notification_publish_total, "failed")
        logger.warning(
            "notification publish ProgrammingError: tenant_id=%s user_id=%s type=%s title=%s error=%s",
            tid,
            uid,
            params["type"],
            params["title"],
            exc,
        )
        return False
    except IntegrityError as exc:
        if _looks_like_user_fk_violation(exc):
            logger.warning(
                "notification publish skipped: user not found for FK. tenant_id=%s user_id=%s title=%s",
                tid,
                uid,
                params["title"],
            )
            inc_counter(notification_publish_total, "skipped")
            return False
        inc_counter(notification_publish_total, "failed")
        logger.warning(
            "notification publish IntegrityError: tenant_id=%s user_id=%s type=%s title=%s error=%s",
            tid,
            uid,
            params["type"],
            params["title"],
            exc,
        )
        return False
    except Exception as exc:
        inc_counter(notification_publish_total, "failed")
        logger.warning(
            "notification publish failed: tenant_id=%s user_id=%s type=%s title=%s error=%s",
            tid,
            uid,
            params["type"],
            params["title"],
            exc,
        )
        return False


async def publish_notification_async(**kwargs) -> bool:
    """
    异步包装：将同步写库放入线程池。
    """
    return await asyncio.to_thread(publish_notification, **kwargs)
