"""串行补推任务表（``qm_model_inference_jobs``）持久化。

设计稿 §5.2：任务必须落库而不是进程内字典 —— 进程重启后任务状态仍可查，
且「单用户单活跃任务」由部分唯一索引兜底（旧端点与新批量端点共用）。
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from backend.shared.database_manager_v2 import get_session
from backend.shared.utc_datetime import utc_now

ACTIVE_STATUSES = ("queued", "running", "cancelling")
TERMINAL_STATUSES = ("partial", "completed", "failed", "cancelled")

# 列名必须是 ``current_trade_date``：``current_date`` 是 PostgreSQL 的函数式
# 关键字，当裸列名建表直接 ``syntax error at or near "current_date"``。
_DDL = (
    """
    CREATE TABLE IF NOT EXISTS qm_model_inference_jobs (
        job_id           TEXT PRIMARY KEY,
        tenant_id        TEXT NOT NULL,
        user_id          TEXT NOT NULL,
        status           TEXT NOT NULL,
        mode             TEXT NOT NULL DEFAULT 'gaps',
        model_ids        JSONB NOT NULL,
        request          JSONB NOT NULL,
        total_models     INTEGER NOT NULL DEFAULT 0,
        total_days       INTEGER NOT NULL DEFAULT 0,
        completed_days   INTEGER NOT NULL DEFAULT 0,
        failed_days      INTEGER NOT NULL DEFAULT 0,
        skipped_days     INTEGER NOT NULL DEFAULT 0,
        current_model_id TEXT,
        current_trade_date DATE,
        model_results    JSONB NOT NULL DEFAULT '[]'::jsonb,
        logs             JSONB NOT NULL DEFAULT '[]'::jsonb,
        error            TEXT,
        cancel_requested BOOLEAN NOT NULL DEFAULT FALSE,
        created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        started_at       TIMESTAMPTZ,
        finished_at      TIMESTAMPTZ,
        heartbeat_at     TIMESTAMPTZ
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_qm_inference_job_active
        ON qm_model_inference_jobs (tenant_id, user_id)
        WHERE status IN ('queued','running','cancelling')
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_qm_inference_jobs_owner_created
        ON qm_model_inference_jobs (tenant_id, user_id, created_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_qm_inference_jobs_status_heartbeat
        ON qm_model_inference_jobs (status, heartbeat_at)
    """,
)

#: ``update_job`` 允许写入的列（白名单，防调用方误写主键/归属）。
_UPDATABLE = frozenset(
    {
        "status",
        "total_models",
        "total_days",
        "completed_days",
        "failed_days",
        "skipped_days",
        "current_model_id",
        "current_trade_date",
        "model_results",
        "logs",
        "error",
        "cancel_requested",
        "started_at",
        "finished_at",
        "heartbeat_at",
    }
)
_JSON_COLUMNS = frozenset({"model_results", "logs"})


class ActiveJobExistsError(RuntimeError):
    """同一用户已有活跃任务（部分唯一索引冲突）。

    ``job`` 携带活跃任务整行（可选）：上层据此拼「是哪个模型在跑、剩余
    几天」的可读提示，避免只丢一个 job_id 让用户不知道在等什么。
    """

    def __init__(
        self, job_id: str | None = None, *, job: dict[str, Any] | None = None
    ) -> None:
        super().__init__("已有补推任务在执行")
        self.job_id = job_id
        self.job = job


class ModelInferenceJobPersistence:
    async def ensure_tables(self) -> None:
        async with get_session() as session:
            for statement in _DDL:
                await session.execute(text(statement))

    # ── 读 ────────────────────────────────────────────────────────────
    @staticmethod
    def _parse_json(value: Any) -> Any:
        if isinstance(value, (list, dict)):
            return value
        if isinstance(value, str) and value.strip():
            try:
                return json.loads(value)
            except ValueError:
                return value
        return value

    @classmethod
    def _row(cls, row: Any) -> dict[str, Any] | None:
        if row is None:
            return None
        payload = dict(row)
        for key in ("model_ids", "request", "model_results", "logs"):
            payload[key] = cls._parse_json(payload.get(key))
        return payload

    async def get_job(
        self, job_id: str, *, tenant_id: str, user_id: str
    ) -> dict[str, Any] | None:
        async with get_session() as session:
            row = (
                (
                    await session.execute(
                        text(
                            """
                            SELECT * FROM qm_model_inference_jobs
                            WHERE job_id = :job_id
                              AND tenant_id = :tenant_id AND user_id = :user_id
                            LIMIT 1
                            """
                        ),
                        {
                            "job_id": str(job_id),
                            "tenant_id": tenant_id,
                            "user_id": user_id,
                        },
                    )
                )
                .mappings()
                .first()
            )
        return self._row(row)

    async def list_jobs(
        self, *, tenant_id: str, user_id: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        async with get_session() as session:
            rows = (
                (
                    await session.execute(
                        text(
                            """
                            SELECT * FROM qm_model_inference_jobs
                            WHERE tenant_id = :tenant_id AND user_id = :user_id
                            ORDER BY created_at DESC
                            LIMIT :limit
                            """
                        ),
                        {
                            "tenant_id": tenant_id,
                            "user_id": user_id,
                            "limit": max(1, min(int(limit), 100)),
                        },
                    )
                )
                .mappings()
                .all()
            )
        return [self._row(row) for row in rows if row is not None]

    async def active_job(
        self, *, tenant_id: str, user_id: str
    ) -> dict[str, Any] | None:
        async with get_session() as session:
            row = (
                (
                    await session.execute(
                        text(
                            """
                            SELECT * FROM qm_model_inference_jobs
                            WHERE tenant_id = :tenant_id AND user_id = :user_id
                              AND status IN ('queued','running','cancelling')
                            ORDER BY created_at DESC
                            LIMIT 1
                            """
                        ),
                        {"tenant_id": tenant_id, "user_id": user_id},
                    )
                )
                .mappings()
                .first()
            )
        return self._row(row)

    # ── 写 ────────────────────────────────────────────────────────────
    async def create_job(
        self,
        *,
        job_id: str,
        tenant_id: str,
        user_id: str,
        model_ids: list[str],
        request: dict[str, Any],
        total_models: int,
        total_days: int,
        mode: str = "gaps",
    ) -> dict[str, Any]:
        active = await self.active_job(tenant_id=tenant_id, user_id=user_id)
        if active:
            raise ActiveJobExistsError(active.get("job_id"), job=active)
        try:
            async with get_session() as session:
                await session.execute(
                    text(
                        """
                        INSERT INTO qm_model_inference_jobs (
                          job_id, tenant_id, user_id, status, mode, model_ids, request,
                          total_models, total_days, model_results, logs
                        ) VALUES (
                          :job_id, :tenant_id, :user_id, 'queued', :mode,
                          CAST(:model_ids AS JSONB), CAST(:request AS JSONB),
                          :total_models, :total_days, '[]'::jsonb, '[]'::jsonb
                        )
                        """
                    ),
                    {
                        "job_id": job_id,
                        "tenant_id": tenant_id,
                        "user_id": user_id,
                        "mode": mode,
                        "model_ids": json.dumps(model_ids, ensure_ascii=False),
                        "request": json.dumps(request, ensure_ascii=False),
                        "total_models": int(total_models),
                        "total_days": int(total_days),
                    },
                )
        except IntegrityError as exc:
            active = await self.active_job(tenant_id=tenant_id, user_id=user_id)
            raise ActiveJobExistsError(
                active.get("job_id") if active else None,
                job=active,
            ) from exc
        created = await self.get_job(job_id, tenant_id=tenant_id, user_id=user_id)
        if created is None:  # pragma: no cover - 插入后必然可读
            raise RuntimeError("创建补推任务后无法读回")
        return created

    async def claim_running(self, job_id: str) -> bool:
        """queued → running 条件迁移。

        取消 queued 任务会直接落 cancelled；无条件改 running 会把终态盖回去。
        返回 False 表示已被取消或被别处领走，调用方应直接退出。
        """
        now = utc_now()
        async with get_session() as session:
            result = await session.execute(
                text(
                    """
                    UPDATE qm_model_inference_jobs
                    SET status = 'running', started_at = :now, heartbeat_at = :now
                    WHERE job_id = :job_id AND status = 'queued'
                    """
                ),
                {"job_id": str(job_id), "now": now},
            )
            return result.rowcount > 0

    async def update_job(self, job_id: str, **fields: Any) -> None:
        clean = {key: value for key, value in fields.items() if key in _UPDATABLE}
        if not clean:
            return
        assignments = []
        params: dict[str, Any] = {"job_id": str(job_id)}
        for key, value in clean.items():
            if key in _JSON_COLUMNS:
                params[key] = json.dumps(value, ensure_ascii=False, default=str)
                assignments.append(f"{key} = CAST(:{key} AS JSONB)")
            else:
                params[key] = value
                assignments.append(f"{key} = :{key}")
        assignments.append("updated_at = NOW()")
        async with get_session() as session:
            await session.execute(
                text(
                    f"UPDATE qm_model_inference_jobs SET {', '.join(assignments)} "
                    "WHERE job_id = :job_id"
                ),
                params,
            )

    async def request_cancel(
        self, job_id: str, *, tenant_id: str, user_id: str
    ) -> dict[str, Any] | None:
        job = await self.get_job(job_id, tenant_id=tenant_id, user_id=user_id)
        if job is None:
            return None
        if job["status"] in TERMINAL_STATUSES:
            return job
        if job["status"] == "queued":
            # 尚未开跑：直接终结，避免 /jobs 列表长期挂着一个不会被执行的任务。
            await self.update_job(
                job_id,
                status="cancelled",
                cancel_requested=True,
                finished_at=utc_now(),
            )
        else:
            await self.update_job(job_id, cancel_requested=True)
        return await self.get_job(job_id, tenant_id=tenant_id, user_id=user_id)

    async def reap_stale(
        self, *, tenant_id: str, user_id: str, heartbeat_timeout_seconds: int = 600
    ) -> int:
        """把心跳超时（或从未心跳）的 queued/running/cancelling 标记为 failed。

        读列表/读详情/建单前顺带自愈；进程重启后无需人工清表。
        """
        async with get_session() as session:
            result = await session.execute(
                text(
                    """
                    UPDATE qm_model_inference_jobs
                    SET status = 'failed',
                        error = COALESCE(error, '进程重启，任务中断'),
                        finished_at = NOW(),
                        updated_at = NOW()
                    WHERE tenant_id = :tenant_id AND user_id = :user_id
                      AND status IN ('queued','running','cancelling')
                      AND COALESCE(heartbeat_at, created_at)
                          < NOW() - make_interval(secs => :timeout)
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                    "timeout": max(1, int(heartbeat_timeout_seconds)),
                },
            )
            return int(result.rowcount or 0)


__all__ = [
    "ACTIVE_STATUSES",
    "ActiveJobExistsError",
    "ModelInferenceJobPersistence",
    "TERMINAL_STATUSES",
]
