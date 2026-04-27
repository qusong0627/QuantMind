from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
from sqlalchemy import text

from backend.shared.database_manager_v2 import get_session


_SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


class ModelInferencePersistence:
    """模型推理结果与自动推理设置持久化。"""

    async def ensure_tables(self) -> None:
        statements = [
            """
            CREATE TABLE IF NOT EXISTS qm_model_inference_runs (
              run_id TEXT PRIMARY KEY,
              tenant_id TEXT NOT NULL DEFAULT 'default',
              user_id TEXT NOT NULL,
              model_id TEXT NOT NULL,
              data_trade_date DATE NOT NULL,
              prediction_trade_date DATE NOT NULL,
              status TEXT NOT NULL,
              signals_count INTEGER NOT NULL DEFAULT 0,
              duration_ms INTEGER,
              fallback_used BOOLEAN NOT NULL DEFAULT FALSE,
              fallback_reason TEXT,
              failure_stage TEXT,
              error_message TEXT,
              stdout TEXT,
              stderr TEXT,
              active_model_id TEXT,
              effective_model_id TEXT,
              model_source TEXT,
              active_data_source TEXT,
              request_json JSONB,
              result_json JSONB,
              created_at TIMESTAMPTZ NOT NULL,
              updated_at TIMESTAMPTZ NOT NULL
            );
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_qm_model_inference_runs_owner_created
              ON qm_model_inference_runs (tenant_id, user_id, created_at DESC);
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_qm_model_inference_runs_model_status
              ON qm_model_inference_runs (tenant_id, user_id, model_id, status, created_at DESC);
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_qm_model_inference_runs_target_date
              ON qm_model_inference_runs (tenant_id, user_id, prediction_trade_date DESC);
            """,
            """
            CREATE TABLE IF NOT EXISTS qm_model_inference_settings (
              tenant_id TEXT NOT NULL DEFAULT 'default',
              user_id TEXT NOT NULL,
              model_id TEXT NOT NULL,
              enabled BOOLEAN NOT NULL DEFAULT FALSE,
              schedule_time TEXT NOT NULL DEFAULT '',
              last_run_id TEXT,
              last_run_json JSONB,
              next_run_at TIMESTAMPTZ,
              created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
              updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
              PRIMARY KEY (tenant_id, user_id, model_id)
            );
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_qm_model_inference_settings_owner
              ON qm_model_inference_settings (tenant_id, user_id, model_id, updated_at DESC);
            """,
        ]
        async with get_session() as session:
            for stmt in statements:
                await session.execute(text(stmt))

    @staticmethod
    def _parse_json_field(value: Any) -> Any:
        if isinstance(value, (dict, list)):
            return value
        if isinstance(value, str) and value.strip():
            try:
                return json.loads(value)
            except Exception:
                return value
        return value

    @staticmethod
    def _schedule_desc(schedule_time: str) -> str:
        return ""

    @staticmethod
    def _parse_schedule_time(schedule_time: str) -> tuple[int, int]:
        raw = str(schedule_time or "").strip()
        try:
            hour_str, minute_str = raw.split(":", 1)
            hour = max(0, min(23, int(hour_str)))
            minute = max(0, min(59, int(minute_str)))
            return hour, minute
        except Exception:
            return 9, 30

    @classmethod
    def _compute_next_run_at(cls, schedule_time: str, reference: datetime | None = None) -> datetime:
        now = reference or datetime.now(_SHANGHAI_TZ)
        if now.tzinfo is None:
            now = now.replace(tzinfo=_SHANGHAI_TZ)
        hour, minute = cls._parse_schedule_time(schedule_time)
        scheduled_today = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

        try:
            cal = xcals.get_calendar("XSHG")
            if cal.is_session(now.date()) and now < scheduled_today:
                return scheduled_today
            next_session = cal.next_session(now.date())
            next_date = next_session.date() if hasattr(next_session, "date") else next_session
            return datetime.combine(next_date, time(hour, minute), tzinfo=_SHANGHAI_TZ)
        except Exception:
            if now < scheduled_today:
                return scheduled_today
            return scheduled_today + timedelta(days=1)  # type: ignore[name-defined]

    @staticmethod
    def _row_to_run(row: dict[str, Any]) -> dict[str, Any]:
        result = dict(row)
        for key in ("created_at", "updated_at"):
            if result.get(key) is not None:
                result[key] = result[key].isoformat()
        for key in ("data_trade_date", "prediction_trade_date"):
            if result.get(key) is not None:
                result[key] = str(result[key])
        result["request_json"] = ModelInferencePersistence._parse_json_field(result.get("request_json"))
        result["result_json"] = ModelInferencePersistence._parse_json_field(result.get("result_json"))
        return result

    @staticmethod
    def _row_to_settings(row: dict[str, Any]) -> dict[str, Any]:
        result = dict(row)
        for key in ("created_at", "updated_at", "next_run_at"):
            if result.get(key) is not None:
                result[key] = result[key].isoformat()
        result["last_run_json"] = ModelInferencePersistence._parse_json_field(result.get("last_run_json"))
        schedule_time = str(result.get("schedule_time") or "")
        result["schedule_desc"] = ModelInferencePersistence._schedule_desc(schedule_time)
        if result.get("next_run_at"):
            dt = result["next_run_at"]
            if isinstance(dt, str):
                result["next_run"] = dt
            else:
                result["next_run"] = result["next_run_at"]
        else:
            result["next_run"] = None
        return result

    async def create_run(
        self,
        *,
        run_id: str,
        tenant_id: str,
        user_id: str,
        model_id: str,
        data_trade_date: date,
        prediction_trade_date: date,
        status: str,
        request_payload: dict[str, Any],
        created_at: datetime,
    ) -> None:
        async with get_session() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO qm_model_inference_runs (
                      run_id, tenant_id, user_id, model_id, data_trade_date, prediction_trade_date,
                      status, signals_count, duration_ms, fallback_used, fallback_reason,
                      failure_stage, error_message, stdout, stderr,
                      active_model_id, effective_model_id, model_source, active_data_source,
                      request_json, result_json, created_at, updated_at
                    ) VALUES (
                      :run_id, :tenant_id, :user_id, :model_id, :data_trade_date, :prediction_trade_date,
                      :status, 0, NULL, FALSE, NULL,
                      NULL, NULL, NULL, NULL,
                      NULL, NULL, NULL, NULL,
                      CAST(:request_json AS JSONB), NULL, :created_at, :created_at
                    )
                    ON CONFLICT (run_id) DO UPDATE SET
                      tenant_id = EXCLUDED.tenant_id,
                      user_id = EXCLUDED.user_id,
                      model_id = EXCLUDED.model_id,
                      data_trade_date = EXCLUDED.data_trade_date,
                      prediction_trade_date = EXCLUDED.prediction_trade_date,
                      status = EXCLUDED.status,
                      request_json = EXCLUDED.request_json,
                      updated_at = EXCLUDED.updated_at
                    """
                ),
                {
                    "run_id": run_id,
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                    "model_id": model_id,
                    "data_trade_date": data_trade_date,
                    "prediction_trade_date": prediction_trade_date,
                    "status": status,
                    "request_json": json.dumps(request_payload, ensure_ascii=False),
                    "created_at": created_at,
                },
            )

    async def update_run(
        self,
        *,
        run_id: str,
        status: str,
        updated_at: datetime,
        signals_count: int = 0,
        duration_ms: int | None = None,
        fallback_used: bool = False,
        fallback_reason: str = "",
        failure_stage: str = "",
        error_message: str | None = None,
        stdout: str = "",
        stderr: str = "",
        active_model_id: str = "",
        effective_model_id: str = "",
        model_source: str = "",
        active_data_source: str = "",
        result_payload: dict[str, Any] | None = None,
    ) -> None:
        async with get_session() as session:
            await session.execute(
                text(
                    """
                    UPDATE qm_model_inference_runs
                    SET status = :status,
                        signals_count = :signals_count,
                        duration_ms = :duration_ms,
                        fallback_used = :fallback_used,
                        fallback_reason = :fallback_reason,
                        failure_stage = :failure_stage,
                        error_message = :error_message,
                        stdout = :stdout,
                        stderr = :stderr,
                        active_model_id = :active_model_id,
                        effective_model_id = :effective_model_id,
                        model_source = :model_source,
                        active_data_source = :active_data_source,
                        result_json = COALESCE(CAST(:result_json AS JSONB), result_json),
                        updated_at = :updated_at
                    WHERE run_id = :run_id
                    """
                ),
                {
                    "run_id": run_id,
                    "status": status,
                    "signals_count": int(signals_count or 0),
                    "duration_ms": duration_ms,
                    "fallback_used": bool(fallback_used),
                    "fallback_reason": fallback_reason,
                    "failure_stage": failure_stage,
                    "error_message": error_message,
                    "stdout": stdout,
                    "stderr": stderr,
                    "active_model_id": active_model_id,
                    "effective_model_id": effective_model_id,
                    "model_source": model_source,
                    "active_data_source": active_data_source,
                    "result_json": (
                        json.dumps(result_payload, ensure_ascii=False) if result_payload is not None else None
                    ),
                    "updated_at": updated_at,
                },
            )

    async def get_run(
        self,
        *,
        run_id: str,
        tenant_id: str,
        user_id: str,
    ) -> dict[str, Any] | None:
        async with get_session(read_only=True) as session:
            row = (
                await session.execute(
                    text(
                        """
                        SELECT *
                        FROM qm_model_inference_runs
                        WHERE run_id = :run_id
                          AND tenant_id = :tenant_id
                          AND user_id = :user_id
                        LIMIT 1
                        """
                    ),
                    {"run_id": run_id, "tenant_id": tenant_id, "user_id": user_id},
                )
            ).mappings().first()
        if not row:
            return None
        return self._row_to_run(dict(row))

    async def list_runs(
        self,
        *,
        tenant_id: str,
        user_id: str,
        model_id: str | None = None,
        run_id: str | None = None,
        status: str | None = None,
        inference_date: date | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        filters = [
            "tenant_id = :tenant_id",
            "user_id = :user_id",
        ]
        params: dict[str, Any] = {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "limit": int(page_size),
            "offset": max(page - 1, 0) * int(page_size),
        }
        if model_id:
            filters.append("model_id = :model_id")
            params["model_id"] = model_id
        if run_id:
            filters.append("run_id = :run_id")
            params["run_id"] = run_id
        if status:
            filters.append("status = :status")
            params["status"] = status
        if inference_date:
            filters.append("data_trade_date = :inference_date")
            params["inference_date"] = inference_date

        where_sql = " AND ".join(filters)
        async with get_session(read_only=True) as session:
            total_row = (
                (
                    await session.execute(
                        text(f"SELECT COUNT(*) AS total FROM qm_model_inference_runs WHERE {where_sql}"),
                        params,
                    )
                )
                .mappings()
                .first()
            )
            total = int((total_row or {}).get("total") or 0)
            rows = (
                (
                    await session.execute(
                        text(
                            f"""
                            SELECT *
                            FROM qm_model_inference_runs
                            WHERE {where_sql}
                            ORDER BY created_at DESC
                            LIMIT :limit OFFSET :offset
                            """
                        ),
                        params,
                    )
                )
                .mappings()
                .all()
            )

        items = [self._row_to_run(dict(row)) for row in rows]
        return {"page": page, "page_size": page_size, "total": total, "items": items}

    async def get_settings(
        self,
        *,
        tenant_id: str,
        user_id: str,
        model_id: str,
    ) -> dict[str, Any]:
        now = datetime.now(_SHANGHAI_TZ)
        async with get_session() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO qm_model_inference_settings (
                      tenant_id, user_id, model_id, enabled, schedule_time, last_run_id, last_run_json,
                      next_run_at, created_at, updated_at
                    ) VALUES (
                      :tenant_id, :user_id, :model_id, FALSE, '', NULL, NULL, NULL, :created_at, :created_at
                    )
                    ON CONFLICT (tenant_id, user_id, model_id) DO NOTHING
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                    "model_id": model_id,
                    "created_at": now,
                },
            )

        async with get_session(read_only=True) as session:
            row = (
                await session.execute(
                    text(
                        """
                        SELECT tenant_id, user_id, model_id, enabled, schedule_time, last_run_id, last_run_json,
                               next_run_at, created_at, updated_at
                        FROM qm_model_inference_settings
                        WHERE tenant_id = :tenant_id AND user_id = :user_id AND model_id = :model_id
                        LIMIT 1
                        """
                    ),
                    {"tenant_id": tenant_id, "user_id": user_id, "model_id": model_id},
                )
            ).mappings().first()
        if not row:
            raise RuntimeError("failed to initialize inference settings")
        result = self._row_to_settings(dict(row))
        if result.get("last_run_json") and isinstance(result["last_run_json"], dict):
            result["last_run"] = result["last_run_json"]
        else:
            result["last_run"] = None
        if result.get("next_run_at"):
            result["next_run"] = str(result["next_run_at"]).replace("T", " ")[:16]
        else:
            result["next_run"] = None
        return result

    async def update_settings(
        self,
        *,
        tenant_id: str,
        user_id: str,
        model_id: str,
        enabled: bool,
        schedule_time: str | None = None,
        last_run: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # 1. 确保记录存在 (通过 get_settings 进行幂等初始化)
        current = await self.get_settings(tenant_id=tenant_id, user_id=user_id, model_id=model_id)

        next_schedule_time = str(schedule_time or current.get("schedule_time") or "")
        next_run_at = self._compute_next_run_at(next_schedule_time) if enabled else None
        now = datetime.now(_SHANGHAI_TZ)

        last_run_id = (last_run or {}).get("run_id")
        last_run_payload = json.dumps(last_run, ensure_ascii=False) if last_run else None

        async with get_session() as session:
            await session.execute(
                text(
                    """
                    UPDATE qm_model_inference_settings
                    SET enabled = :enabled,
                        schedule_time = :schedule_time,
                        last_run_id = COALESCE(:last_run_id, last_run_id),
                        last_run_json = CASE 
                            WHEN CAST(:last_run_json_val AS TEXT) IS NULL THEN last_run_json 
                            ELSE CAST(:last_run_json_val AS JSONB) 
                        END,
                        next_run_at = :next_run_at,
                        updated_at = :updated_at
                    WHERE tenant_id = :tenant_id AND user_id = :user_id AND model_id = :model_id
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                    "model_id": model_id,
                    "enabled": bool(enabled),
                    "schedule_time": next_schedule_time,
                    "last_run_id": last_run_id,
                    "last_run_json_val": last_run_payload if last_run_payload is not None else None,
                    "next_run_at": next_run_at,
                    "updated_at": now,
                },
            )
            await session.commit()

        return await self.get_settings(tenant_id=tenant_id, user_id=user_id, model_id=model_id)

    async def record_run_to_settings(
        self,
        *,
        tenant_id: str,
        user_id: str,
        model_id: str,
        run_payload: dict[str, Any],
    ) -> dict[str, Any]:
        settings = await self.get_settings(tenant_id=tenant_id, user_id=user_id, model_id=model_id)
        schedule_time = str(settings.get("schedule_time") or "")
        enabled = bool(settings.get("enabled"))
        next_run_at = self._compute_next_run_at(schedule_time) if enabled else None
        now = datetime.now(_SHANGHAI_TZ)
        async with get_session() as session:
            await session.execute(
                text(
                    """
                    UPDATE qm_model_inference_settings
                    SET last_run_id = :last_run_id,
                        last_run_json = CAST(:last_run_json AS JSONB),
                        next_run_at = :next_run_at,
                        updated_at = :updated_at
                    WHERE tenant_id = :tenant_id AND user_id = :user_id AND model_id = :model_id
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                    "model_id": model_id,
                    "last_run_id": str(run_payload.get("run_id") or ""),
                    "last_run_json": json.dumps(run_payload, ensure_ascii=False),
                    "next_run_at": next_run_at,
                    "updated_at": now,
                },
            )
        return await self.get_settings(tenant_id=tenant_id, user_id=user_id, model_id=model_id)


model_inference_persistence = ModelInferencePersistence()
