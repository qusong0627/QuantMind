from __future__ import annotations

import asyncio
import inspect
import json
import logging
from typing import Any

from backend.services.trade_shared.redis_client import RedisClient

logger = logging.getLogger(__name__)


class SimulationRuntimeRestorer:
    """Restore simulation sandbox workers from active strategy state after trade restarts."""

    def __init__(self, redis: RedisClient):
        self.redis = redis

    async def restore_all(self) -> int:
        if not self.redis.client:
            return 0

        restored = 0
        for raw_key in self.redis.client.scan_iter(
            match="trade:active_strategy:*", count=500
        ):
            try:
                restored += 1 if await self.restore_key(str(raw_key)) else 0
            except Exception as exc:
                logger.warning(
                    "simulation runtime restore skipped key=%s error=%s",
                    raw_key,
                    exc,
                    exc_info=True,
                )
        if restored > 0:
            logger.info("simulation runtime restored active sandboxes: %s", restored)
        return restored

    async def restore_key(self, key: str) -> bool:
        raw = self.redis.client.get(key)
        if not raw:
            return False
        try:
            active_data = json.loads(raw)
        except Exception:
            return False
        if not isinstance(active_data, dict):
            return False
        if str(active_data.get("mode") or "").upper() != "SIMULATION":
            return False

        from backend.shared.simulation_account_keys import parse_active_strategy_key
        from backend.shared.simulation_account_keys import resolve_active_identity

        parsed = parse_active_strategy_key(key)
        if not parsed:
            return False
        tenant_id, user_id = resolve_active_identity(
            tenant_suffix=parsed[0], user_suffix=parsed[1], payload=active_data
        )
        restored = await self.restore_active_payload(
            tenant_id=tenant_id,
            user_id=user_id,
            active_data=active_data,
        )
        # 历史键（如 000admin）恢复成功后删除，避免新旧双键并存
        if restored:
            try:
                from backend.shared.simulation_account_keys import active_strategy_key

                canonical = active_strategy_key(tenant_id, user_id)
                if canonical != key:
                    self.redis.client.delete(key)
            except Exception:
                pass
        return restored

    async def restore_active_payload(
        self,
        *,
        tenant_id: str,
        user_id: str,
        active_data: dict[str, Any],
    ) -> bool:
        strategy_id = str(active_data.get("strategy_id") or "").strip()
        if not strategy_id:
            return False

        from backend.services.trade.sandbox.manager import sandbox_manager

        if sandbox_manager.is_strategy_running(tenant_id, user_id, strategy_id):
            return False

        # 优先用启动时持久化的代码快照（覆盖 strategy_file 上传场景），避免重启后因存储查不到而丢运行态
        code_str = str(active_data.get("code_str") or "").strip()
        if not code_str:
            code_str = await self._resolve_code(strategy_id=strategy_id, user_id=user_id)
        if not code_str.strip():
            logger.warning(
                "simulation runtime restore skipped missing code: tenant=%s user=%s strategy=%s",
                tenant_id,
                user_id,
                strategy_id,
            )
            # 幽灵态清理：沙箱已死、代码也找不回时，清掉 active 键并把 portfolio 置 stopped，
            # 避免 Redis 有键 + PG running 但实际无运行，前端与 DB 长期错位。
            await self._cleanup_ghost(tenant_id=tenant_id, user_id=user_id)
            return False

        exec_config = (
            dict(active_data.get("execution_config"))
            if isinstance(active_data.get("execution_config"), dict)
            else {}
        )
        live_trade_config = (
            dict(active_data.get("live_trade_config"))
            if isinstance(active_data.get("live_trade_config"), dict)
            else {}
        )
        sandbox_run_id = sandbox_manager.submit_strategy(
            tenant_id=tenant_id,
            user_id=user_id,
            strategy_id=strategy_id,
            code_str=code_str,
            exec_config=exec_config,
            live_trade_config=live_trade_config,
        )
        active_data["sandbox_restored_run_id"] = sandbox_run_id
        # 保留原始 started_at 锚点，不覆盖，避免 5 日等调仓节奏漂移
        try:
            from backend.shared.simulation_account_keys import active_strategy_key

            self.redis.client.set(
                active_strategy_key(tenant_id, user_id),
                json.dumps(active_data, ensure_ascii=False),
            )
        except Exception:
            pass
        logger.info(
            "simulation sandbox restored: tenant=%s user=%s strategy=%s run_id=%s",
            tenant_id,
            user_id,
            strategy_id,
            sandbox_run_id,
        )
        return True

    async def _cleanup_ghost(self, *, tenant_id: str, user_id: str) -> None:
        """清理无法恢复的幽灵运行态（active 键 + portfolio run_status）。永不抛异常。"""
        try:
            if self.redis.client:
                self.redis.client.delete(
                    f"trade:active_strategy:{tenant_id}:{str(user_id).zfill(8)}"
                )
        except Exception:
            pass
        try:
            from sqlalchemy import desc as _desc
            from sqlalchemy import select as _select

            from backend.services.trade_shared.portfolio.models import Portfolio as _Portfolio
            from backend.shared.database_manager_v2 import get_session as _get_session

            async with _get_session() as session:
                for uid_form in {str(user_id), str(user_id).zfill(8)}:
                    try:
                        res = await session.execute(
                            _select(_Portfolio)
                            .where(
                                _Portfolio.tenant_id == tenant_id,
                                _Portfolio.user_id == uid_form,
                                _Portfolio.run_status == "running",
                                _Portfolio.is_deleted.is_(False),
                            )
                            .order_by(_desc(_Portfolio.updated_at))
                            .limit(1)
                        )
                        pf = res.scalars().first()
                        if pf is not None:
                            pf.run_status = "stopped"
                            await session.commit()
                            logger.info(
                                "simulation ghost cleaned: portfolio %s running->stopped",
                                getattr(pf, "id", "?"),
                            )
                    except Exception:
                        try:
                            await session.rollback()
                        except Exception:
                            pass
        except Exception as exc:
            logger.warning("simulation ghost cleanup failed: %s", exc)

    async def _resolve_code(self, *, strategy_id: str, user_id: str) -> str:
        if strategy_id.startswith("sys_"):
            template_id = strategy_id.replace("sys_", "", 1)
            try:
                from backend.services.engine.qlib_app.services.strategy_templates import (
                    get_template_by_id,
                )

                template = get_template_by_id(template_id)
                return str(getattr(template, "code", "") or "")
            except Exception:
                return ""

        if not strategy_id.isdigit():
            return ""

        try:
            from backend.shared.strategy_storage import get_strategy_storage_service

            storage_svc = get_strategy_storage_service()
            strategy = storage_svc.get(
                strategy_id=int(strategy_id),
                user_id=user_id,
            )
            if inspect.isawaitable(strategy):
                strategy = await strategy
            if isinstance(strategy, dict):
                return str(strategy.get("code") or "")
        except Exception as exc:
            logger.warning(
                "failed to resolve simulation strategy code for restore: strategy=%s error=%s",
                strategy_id,
                exc,
            )
        return ""
