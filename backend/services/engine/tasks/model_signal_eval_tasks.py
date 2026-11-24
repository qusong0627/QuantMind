"""模型「测试集实盘力」日更与恶化告警。

每个交易日 16:50（盘后、日线已可读）对**正在使用**的 A 股模型做滚动 3 个月体检：
- 默认模型 + 被策略绑定的模型（真正“上线”的那批）；
- 计算走 ``model_signal_eval_service.refresh_sync``，写缓存 + 历史快照；
- 评估为 red、或长多连续 5 次跑输打分池时，给模型所有者发一条站内预警
  （按用户聚合成一条，QQ 旁路关闭，避免逐模型刷屏）。

任务幂等：同一窗口重复跑只更新快照；Redis 日键防重复告警。
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any

from sqlalchemy import text

from backend.services.api import model_signal_eval_service as svc
from backend.services.engine.qlib_app.celery_config import celery_app
from backend.shared.database_manager_v2 import get_session
from backend.shared.notification_publisher import publish_notification
from backend.shared.redis_sentinel_client import get_redis_sentinel_client

logger = logging.getLogger(__name__)

ELIGIBLE_SQL = """
SELECT DISTINCT
       m.tenant_id,
       m.user_id,
       m.model_id,
       m.storage_path,
       m.metadata_json,
       m.is_default,
       m.updated_at
FROM qm_user_models m
WHERE m.status IN ('ready', 'active')
  AND (
        m.is_default = TRUE
        OR EXISTS (
            SELECT 1
            FROM qm_strategy_model_bindings b
            WHERE b.tenant_id = m.tenant_id
              AND b.user_id = m.user_id
              AND b.model_id = m.model_id
        )
      )
ORDER BY m.updated_at DESC NULLS LAST
LIMIT :limit
"""

ALERT_STREAK = 5


def _meta_dict(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _market_horizon(meta: dict) -> tuple[str, int]:
    context = meta.get("context") if isinstance(meta.get("context"), dict) else {}
    market = str(meta.get("market") or context.get("market") or "CN").upper()
    try:
        horizon = int(meta.get("target_horizon_days") or 5)
    except (TypeError, ValueError):
        horizon = 5
    return market, horizon


async def _load_eligible(limit: int) -> list[dict]:
    async with get_session(read_only=True) as session:
        rows = (
            (await session.execute(text(ELIGIBLE_SQL), {"limit": int(limit)}))
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


def _deterioration_reasons(report: dict, history: list[dict]) -> list[str]:
    admission = report.get("admission") or {}
    if admission.get("grade") == "red":
        failed = "、".join(admission.get("reasons") or ["排序/中性化未过"])
        return [f"评估 red：{failed}"]
    tail = history[-ALERT_STREAK:]
    if len(tail) >= ALERT_STREAK and all(
        row.get("long_pass") is False and (row.get("top20_excess") or 0) < 0
        for row in tail
    ):
        return [f"长多连续 {ALERT_STREAK} 次跑输打分池"]
    return []


def _alert_once(key: str) -> bool:
    try:
        client = get_redis_sentinel_client()
        # 包装层的 set() 把 Redis 异常吞成 False，会和「键已存在」混淆；
        # 这里直连主库以便区分：异常时 fail-open（宁多一次，不漏报）。
        client._ensure_connection()
        return bool(client._master_client.set(key, b"1", ex=20 * 3600, nx=True))
    except Exception as exc:  # noqa: BLE001
        logger.warning("模型评估告警去重键失败：%s", exc)
        return True


def _run(max_models: int, window_days: int) -> dict:
    try:
        rows = asyncio.run(_load_eligible(max_models))
    except Exception as exc:  # noqa: BLE001
        logger.exception("模型实盘力日更：读取模型清单失败")
        return {"status": "error", "error": str(exc)}

    alerts: dict[tuple[str, str], list[str]] = {}
    refreshed = skipped = failed = 0
    for row in rows:
        model_id = str(row.get("model_id") or "")
        storage_path = str(row.get("storage_path") or "")
        if not model_id or not storage_path:
            continue
        meta = _meta_dict(row.get("metadata_json"))
        market, horizon = _market_horizon(meta)
        if market != "CN":
            skipped += 1
            continue
        try:
            report = svc.refresh_sync(
                model_id,
                storage_path,
                market=market,
                horizon=horizon,
                window_days=window_days,
            )
        except Exception as exc:  # noqa: BLE001 — 单个模型失败不影响其余
            failed += 1
            logger.warning("模型实盘力日更失败 model=%s: %s", model_id, exc)
            alerts.setdefault(
                (str(row.get("tenant_id") or "default"), str(row.get("user_id") or "")),
                [],
            ).append(f"{model_id}：标准化评估失败（{exc}）")
            continue
        if report is None:
            skipped += 1
            continue
        refreshed += 1
        history = svc.read_history(model_id, window_days)
        reasons = _deterioration_reasons(report, history)
        if reasons:
            key = (
                str(row.get("tenant_id") or "default"),
                str(row.get("user_id") or ""),
            )
            alerts.setdefault(key, []).append(f"{model_id}：{'；'.join(reasons)}")

    today = datetime.now().strftime("%Y%m%d")
    pushed = 0
    for (tenant_id, user_id), lines in alerts.items():
        if not user_id or not lines:
            continue
        if not _alert_once(f"qm:model_signal_eval:alert:{today}:{tenant_id}:{user_id}"):
            continue
        ok = publish_notification(
            user_id=user_id,
            tenant_id=tenant_id,
            title="模型标准化评估预警",
            content="\n".join(f"- {line}" for line in lines)
            + "\n\n到「模型详情 → 测试集实盘力」查看滚动窗口与历史趋势；"
            "未过线模型不建议继续作为默认/策略绑定模型。",
            type="model_health",
            level="warning",
            action_url="#/models",
            # 只有 red/评估失败才即时推 QQ；长多连续跑输只留站内，避免每天刷屏。
            qq_alert=any("评估 red" in line or "评估失败" in line for line in lines),
        )
        if ok:
            pushed += 1

    summary = {
        "status": "ok",
        "models": len(rows),
        "refreshed": refreshed,
        "skipped": skipped,
        "failed": failed,
        "alerts": sum(len(v) for v in alerts.values()),
        "notifications": pushed,
    }
    logger.info("模型实盘力日更完成：%s", summary)
    return summary


@celery_app.task(
    name="engine.tasks.model_signal_eval_refresh",
    bind=True,
    max_retries=0,
    time_limit=25 * 60,
)
def model_signal_eval_refresh(self, max_models: int = 8, window_days: int = 63) -> dict:
    return _run(int(max_models), int(window_days))
