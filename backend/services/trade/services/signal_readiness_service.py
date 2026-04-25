import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.trade.services.manual_execution_service import (
    manual_execution_service,
)

logger = logging.getLogger(__name__)


TRADING_PERMISSION_TRADE_ENABLED = "trade_enabled"
TRADING_PERMISSION_OBSERVE_ONLY = "observe_only"
TRADING_PERMISSION_BLOCKED = "blocked"


class SignalReadinessService:
    """统一判定默认模型最新信号是否可用于自动交易。"""

    async def evaluate(
        self,
        db: AsyncSession,
        *,
        redis_client: Any,
        tenant_id: str,
        user_id: str,
        mode: str,
    ) -> dict[str, Any]:
        normalized_mode = str(mode or "REAL").strip().upper()
        tenant = (tenant_id or "").strip() or "default"
        uid = str(user_id or "").strip()

        hosted_status = await manual_execution_service.get_default_model_hosted_status(
            tenant_id=tenant,
            user_id=uid,
        )
        result = {
            "available": bool(hosted_status.get("available")),
            "status": str(hosted_status.get("reason_code") or "unknown"),
            "message": str(hosted_status.get("message") or ""),
            "mode": normalized_mode,
            "latest_run_id": hosted_status.get("latest_run_id"),
            "data_trade_date": hosted_status.get("data_trade_date"),
            "prediction_trade_date": hosted_status.get("prediction_trade_date"),
            "execution_window_start": hosted_status.get("execution_window_start"),
            "execution_window_end": hosted_status.get("execution_window_end"),
            "fallback_used": bool(hosted_status.get("fallback_used")),
            "model_source": hosted_status.get("model_source")
            or hosted_status.get("source"),
            "signal_count": 0,
            "redis_latest_run_id": None,
            "blocking": False,
            "trading_permission": TRADING_PERMISSION_TRADE_ENABLED,
        }

        if not result["available"]:
            return self._apply_mode_policy(result)

        latest_run_id = str(result.get("latest_run_id") or "").strip()
        if not latest_run_id:
            result.update(
                {
                    "available": False,
                    "status": "missing_latest_run",
                    "message": "默认模型推理状态可用，但缺少 latest_run_id",
                }
            )
            return self._apply_mode_policy(result)

        redis_key = f"qm:signal:latest:{tenant}:{uid}"
        try:
            redis_latest_run_id = str(redis_client.get(redis_key) or "").strip()
        except Exception as exc:
            logger.warning("读取最新信号 Redis key 失败: %s", exc)
            redis_latest_run_id = ""
        result["redis_latest_run_id"] = redis_latest_run_id or None

        if redis_latest_run_id and redis_latest_run_id != latest_run_id:
            result.update(
                {
                    "available": False,
                    "status": "redis_mismatch",
                    "message": (
                        "Redis 最新信号批次与默认模型最新完成推理不一致: "
                        f"redis={redis_latest_run_id}, db={latest_run_id}"
                    ),
                }
            )
            return self._apply_mode_policy(result)

        if not redis_latest_run_id:
            result.update(
                {
                    "available": False,
                    "status": "missing_redis_latest_run",
                    "message": f"未检测到最新信号 Redis 标记 {redis_key}",
                }
            )
            return self._apply_mode_policy(result)

        signal_count = await self._count_signal_rows(
            db,
            tenant_id=tenant,
            user_id=uid,
            run_id=latest_run_id,
        )
        result["signal_count"] = signal_count
        if signal_count <= 0:
            result.update(
                {
                    "available": False,
                    "status": "empty_signal",
                    "message": "最新推理批次没有可执行的 engine_signal_scores 信号",
                }
            )
            return self._apply_mode_policy(result)

        result.update(
            {
                "available": True,
                "status": "ready",
                "message": (
                    "默认模型最新推理信号可用于自动交易"
                    f"（run_id={latest_run_id}, signal_count={signal_count}）"
                ),
            }
        )
        return self._apply_mode_policy(result)

    async def _count_signal_rows(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
    ) -> int:
        row = (
            (
                await db.execute(
                    text(
                        """
                        SELECT COUNT(*) AS cnt
                        FROM engine_signal_scores
                        WHERE tenant_id = :tenant_id
                          AND user_id = :user_id
                          AND run_id = :run_id
                        """
                    ),
                    {
                        "tenant_id": tenant_id,
                        "user_id": user_id,
                        "run_id": run_id,
                    },
                )
            )
            .mappings()
            .first()
        )
        return int((row or {}).get("cnt") or 0)

    def _apply_mode_policy(self, result: dict[str, Any]) -> dict[str, Any]:
        mode = str(result.get("mode") or "REAL").upper()
        if result.get("available"):
            result["blocking"] = False
            result["trading_permission"] = TRADING_PERMISSION_TRADE_ENABLED
            return result

        if mode == "REAL":
            result["blocking"] = True
            result["trading_permission"] = TRADING_PERMISSION_BLOCKED
        else:
            result["blocking"] = False
            result["trading_permission"] = TRADING_PERMISSION_OBSERVE_ONLY
        return result


signal_readiness_service = SignalReadinessService()
