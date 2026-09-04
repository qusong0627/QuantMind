"""「最近事件」系统运行事件统一写入器。

把 QuantMind 各服务的运行活动（启停、健康迁移、节点阈值告警、数据同步结果）
收敛写入一张持久化表 ``system_events``（由 ``data/upgrade_v1.0.2.sql`` 建表），
供管理后台 ``/api/v1/admin/system-events`` 查询成一条可回查的「最近事件」时间线。

写入走共享同步 PG 池（``backend/shared/database_pool``），低频操作：
- FastAPI/异步上下文经 :func:`record_system_event_async`（asyncio.to_thread）；
- 同步脚本/Celery 直接调 :func:`record_system_event`。

事件记录非关键路径：任何异常只记日志、绝不抛出，避免 DB 抖动反噬业务主流程。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from sqlalchemy import text

from backend.shared.database_pool import get_db

logger = logging.getLogger(__name__)

# 注册的 event_type 说明（约定，供查询/展示用）
EVENT_TYPES = {
    "service_lifecycle": "服务启停",
    "health_transition": "健康状态迁移",
    "node_alert": "节点性能告警",
    "data_sync": "数据同步",
    "error": "错误",
}
LEVELS = {"info", "warning", "error", "critical"}


def record_system_event(
    event_type: str,
    level: str = "info",
    source: str = "quantmind-api",
    title: str = "",
    message: str | None = None,
    meta: dict[str, Any] | None = None,
) -> None:
    """同步写入一条系统运行事件。失败仅记日志，不抛出。

    :param event_type: 事件类型（见 EVENT_TYPES）
    :param level: 等级 info/warning/error/critical
    :param source: 来源服务/组件（如 quantmind-api）
    :param title: 事件标题
    :param message: 事件详情（可选）
    :param meta: 附加结构化信息（可选，序列化为 JSONB）
    """
    if level not in LEVELS:
        level = "info"
    sql = text(
        """INSERT INTO system_events (event_type, level, source, title, message, meta)
           VALUES (:event_type, :level, :source, :title, :message, CAST(:meta AS jsonb))"""
    )
    params: dict[str, Any] = {
        "event_type": event_type,
        "level": level,
        "source": source,
        "title": title[:2000],
        "message": message[:10000] if message else None,
        "meta": json.dumps(meta or {}, ensure_ascii=False),
    }
    try:
        with get_db() as session:
            session.execute(sql, params)
            session.commit()
    except Exception:  # noqa: BLE001 - 事件记录非关键路径
        logger.warning(
            "记录系统事件失败 (type=%s, level=%s, source=%s, title=%s)",
            event_type,
            level,
            source,
            title,
            exc_info=True,
        )


async def record_system_event_async(
    event_type: str,
    level: str = "info",
    source: str = "quantmind-api",
    title: str = "",
    message: str | None = None,
    meta: dict[str, Any] | None = None,
) -> None:
    """异步写入一条系统运行事件（FastAPI/异步上下文用）。"""
    await asyncio.to_thread(
        record_system_event,
        event_type,
        level,
        source,
        title,
        message,
        meta,
    )
