import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.live_trading.services.manual_execution_service import (
    manual_execution_service,
)

logger = logging.getLogger(__name__)


TRADING_PERMISSION_TRADE_ENABLED = "trade_enabled"
TRADING_PERMISSION_OBSERVE_ONLY = "observe_only"
TRADING_PERMISSION_BLOCKED = "blocked"


class SignalReadinessService:
    """统一判定默认模型最新信号是否可用于自动交易。"""

    @staticmethod
    def _read_signal_latest_key(
        redis_key: str,
        redis_client: Any,
        *,
        tenant_id: str,
        user_id: str,
    ) -> str:
        """读取最新信号 Redis 标记。

        标记由 engine 的 EngineSignalStreamPublisher 写入，默认落在 db0
        （SIGNAL_STREAM_REDIS_DB）。trade 服务传入的 redis_client 可能指向
        其它 db（如 db2），此时会读不到——降级到 engine 信号流 Redis 读取，
        保证 DB 权威 run_id 与 Redis 标记对齐。
        """
        try:
            value = str(redis_client.get(redis_key) or "").strip()
            if value:
                return value
        except Exception as exc:
            logger.warning("读取最新信号 Redis key 失败: %s", exc)

        try:
            from backend.shared.redis_sentinel_client import get_redis_sentinel_client

            stream_redis = get_redis_sentinel_client()
            value_raw = stream_redis.get(redis_key)
            if value_raw is not None:
                if isinstance(value_raw, bytes):
                    value_raw = value_raw.decode("utf-8", errors="ignore")
                return str(value_raw).strip()
        except Exception as exc:
            logger.warning("降级读取信号流 Redis 标记失败: %s", exc)
        return ""

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
        redis_latest_run_id = self._read_signal_latest_key(
            redis_key,
            redis_client,
            tenant_id=tenant,
            user_id=uid,
        )
        result["redis_latest_run_id"] = redis_latest_run_id or None

        if redis_latest_run_id and redis_latest_run_id != latest_run_id:
            # Redis 标记可能被历史回填推理覆盖（mark_latest_run 无条件更新）。
            # DB 的 qm_model_inference_runs + engine_signal_scores 才是权威，
            # 这里把标记同步为 DB 最新 run 后继续正常判定。
            logger.warning(
                "Redis 最新信号标记与 DB 不一致，自动同步: redis=%s db=%s",
                redis_latest_run_id,
                latest_run_id,
            )
            try:
                redis_client.set(redis_key, latest_run_id, ex=86400)
            except Exception as exc:
                logger.warning("同步 Redis 最新信号标记失败: %s", exc)
            result["redis_latest_run_id"] = latest_run_id
            redis_latest_run_id = latest_run_id

        if not redis_latest_run_id:
            # 标记缺失（推理完成后 mark_latest_run 未写成功或 TTL 过期）时，
            # 以 DB 权威自动补齐标记后继续判定，与上方不一致分支口径一致。
            logger.warning(
                "Redis 最新信号标记缺失，自动补齐: key=%s db=%s",
                redis_key,
                latest_run_id,
            )
            try:
                redis_client.set(redis_key, latest_run_id, ex=86400)
            except Exception as exc:
                logger.warning("补齐 Redis 最新信号标记失败: %s", exc)
            result["redis_latest_run_id"] = latest_run_id
            redis_latest_run_id = latest_run_id

        signal_count = await self._count_signal_rows(
            db,
            tenant_id=tenant,
            user_id=uid,
            run_id=latest_run_id,
        )
        result["signal_count"] = signal_count
        if signal_count <= 0:
            # 信号表为空（如被后续补推覆盖清空）时，回退默认模型 pred.parquet
            # 数据日截面计数，与手动任务信号加载共用同一兜底源，避免自动交易空转。
            pred_rows = await manual_execution_service.load_pred_parquet_signal_rows(
                tenant_id=tenant,
                user_id=uid,
                model_id=str(hosted_status.get("latest_default_model_id") or ""),
                data_trade_date=str(result.get("data_trade_date") or ""),
            )
            if pred_rows:
                result.update(
                    {
                        "available": True,
                        "status": "ready",
                        "signal_count": len(pred_rows),
                        "signal_source_fallback": "pred_parquet",
                        "message": (
                            "最新推理批次信号表为空，已从 pred.parquet 回退截面，"
                            f"可用于自动交易（run_id={latest_run_id}, "
                            f"fallback_count={len(pred_rows)}）"
                        ),
                    }
                )
                return self._apply_mode_policy(result)
            result.update(
                {
                    "available": False,
                    "status": "empty_signal",
                    "message": (
                        "最新推理批次没有可执行的 engine_signal_scores 信号，"
                        "且无 pred.parquet 可回退截面"
                    ),
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
                          AND (universe_tag IS NULL OR universe_tag = 'CN')
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
