"""
数据质量告警写入器 + 站内通知发送器。

调用方：
- DataCleaner 在严重违规时 → write_alert(severity='warning', alert_type='range_violation', ...)
- HealthMonitor 在 error_rate 超阈值时 → write_alert(severity='error', alert_type='source_down', ...)
- FieldAggregator 在 fallback 触发时 → write_alert(severity='info', alert_type='fallback_triggered', ...)
- 共识投票偏离过大时 → write_alert(severity='warning', alert_type='consensus_break', ...)

写入路径：
1. 直接 INSERT 到 PostgreSQL data_quality_alerts
2. 调用 NotificationService.create_notification 给所有 is_admin=true 的用户
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)


# 严重程度 -> 站内通知 level
_SEVERITY_TO_LEVEL = {
    "info": "info",
    "warning": "warning",
    "error": "error",
    "critical": "error",
}


class DataAlertService:
    """
    同步实现：data_quality_alerts 写入 + 站内通知 fan-out 给 admins。
    设计为非异步，方便在 cron / 适配器同步上下文里直接调用。
    """

    def __init__(
        self,
        *,
        db_url: str | None = None,
        notification_writer=None,  # 可注入自定义 writer 便于测试
    ) -> None:
        self.db_url = db_url or _resolve_db_url()
        self.notification_writer = notification_writer

    def write_alert(
        self,
        *,
        alert_type: str,
        severity: str = "warning",
        message: str,
        market: str | None = None,
        field: str | None = None,
        source: str | None = None,
        symbol: str | None = None,
        trade_date: date | None = None,
        details: dict[str, Any] | None = None,
        notify_admins: bool = True,
    ) -> int | None:
        """写入告警 + 可选 fan-out 站内通知。返回 alert id（失败返回 None）。"""
        alert_id = self._insert_db(
            alert_type=alert_type, severity=severity, message=message,
            market=market, field=field, source=source,
            symbol=symbol, trade_date=trade_date, details=details,
        )

        if notify_admins:
            try:
                self._notify_admins(
                    title=f"[{severity.upper()}] {alert_type}: {market or '-'}/{field or '-'}",
                    body=message,
                    level=_SEVERITY_TO_LEVEL.get(severity, "info"),
                    extra={
                        "alert_id": alert_id,
                        "alert_type": alert_type,
                        "market": market,
                        "field": field,
                        "source": source,
                        "symbol": symbol,
                    },
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("admin notify failed: %s", exc)

        return alert_id

    # -------- internals --------
    def _insert_db(
        self,
        *,
        alert_type, severity, message,
        market, field, source, symbol, trade_date, details,
    ) -> int | None:
        try:
            from sqlalchemy import create_engine, text as sql_text
            engine = create_engine(self.db_url, pool_pre_ping=True)
            with engine.begin() as conn:
                row = conn.execute(
                    sql_text(
                        """
                        INSERT INTO data_quality_alerts
                            (alert_type, severity, market, field, source, symbol,
                             trade_date, message, details, created_at)
                        VALUES
                            (:alert_type, :severity, :market, :field, :source, :symbol,
                             :trade_date, :message, CAST(:details AS JSONB), NOW())
                        RETURNING id
                        """
                    ),
                    {
                        "alert_type": alert_type,
                        "severity": severity,
                        "market": market,
                        "field": field,
                        "source": source,
                        "symbol": symbol,
                        "trade_date": trade_date,
                        "message": message[:4000] if message else "",
                        "details": json.dumps(details or {}, default=str),
                    },
                ).fetchone()
                return int(row[0]) if row else None
        except Exception as exc:  # noqa: BLE001
            logger.error("insert data_quality_alerts failed: %s", exc)
            return None

    def _notify_admins(self, *, title: str, body: str, level: str, extra: dict[str, Any]) -> None:
        if self.notification_writer is not None:
            self.notification_writer(title=title, body=body, level=level, extra=extra)
            return

        # 默认实现：统一通知发布器（PG notifications + Redis Stream → WS 实时送达）。
        # 修复（T-P6-11，2026-09-17）：旧版自拼 INSERT 三处错——①id 列被 gen_random_uuid()
        # 写崩（SERIAL 整型）②列名 type≠notification_type（UndefinedColumn）③user_id 取了
        # users.id（主键空间=1）而 notifications.user_id **FK 指向 users.user_id**（业务 8 位
        # 空间）——三错叠加被 except 吞成 warning，admin 永远收不到数据质量告警。
        try:
            from sqlalchemy import create_engine, text as sql_text

            from backend.shared.notification_publisher import publish_notification

            engine = create_engine(self.db_url, pool_pre_ping=True)
            with engine.begin() as conn:
                admin_ids = conn.execute(
                    sql_text(
                        "SELECT user_id, COALESCE(tenant_id, 'default') AS tid "
                        "FROM users WHERE is_admin = true"
                    )
                ).fetchall()
            if not admin_ids:
                logger.warning("no admin users; skip data-alert fanout")
                return
            content = (body + "\n" + json.dumps(extra, default=str))[:4000]
            for row in admin_ids:
                published = publish_notification(
                    user_id=str(row[0]),
                    tenant_id=str(row[1]),
                    title=title[:128],
                    content=content,
                    type="data_quality",
                    level=level,
                )
                if not published:
                    logger.warning(
                        "data-alert 通知发布未成功（publisher 返回 False）: user=%s title=%s",
                        row[0], title,
                    )
        except Exception as exc:  # noqa: BLE001
            logger.warning("fanout data-alert to admins failed: %s", exc)


# ---------------------------------------------------------------------------
def _resolve_db_url() -> str:
    """同步 URL：委托 ``backend.shared.sync_db`` 唯一事实源（2026-09-17 收敛）。"""
    from backend.shared.sync_db import resolve_sync_db_url

    return resolve_sync_db_url()


# ---------------------------------------------------------------------------
_singleton: DataAlertService | None = None


def get_alert_service() -> DataAlertService:
    global _singleton
    if _singleton is None:
        _singleton = DataAlertService()
    return _singleton
