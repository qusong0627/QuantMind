"""RD-Agent 因子持久化 — 创建和管理 rd_agent_factors 表"""

import builtins
import hashlib
import json
import logging
import math
from datetime import datetime
from typing import Any

from sqlalchemy import text

from backend.services.engine.alpha_agent.task_spec import (
    IdempotencyConflict,
    MiningTaskSpec,
)
from backend.shared.database_manager_v2 import get_session
from backend.shared.owner_scope import OwnerScope
from backend.shared.utc_datetime import to_utc_iso, utc_now

logger = logging.getLogger(__name__)


def _parse_pg_json_ts(value: str) -> datetime:
    """解析 PG to_jsonb 吐出的 timestamptz 字符串。

    PG 会裁掉微秒末尾的 0（如 .930940→.93094，5 位小数），而 Python 3.10
    的 fromisoformat 只接受 3/6 位小数——直接调会抛 ValueError 导致整页 500。
    这里把小数部分补齐到 6 位后再解析。
    """
    text = value.replace("Z", "+00:00")
    if "." in text:
        head, tail = text.split(".", 1)
        digits = ""
        for ch in tail:
            if ch.isdigit():
                digits += ch
            else:
                break
        if digits:
            text = f"{head}.{digits.ljust(6, '0')}{tail[len(digits):]}"
    return datetime.fromisoformat(text)

_TASK_CREATE_FIELDS = frozenset(
    {
        "market",
        "universe",
        "loop_n",
        "direction",
        "data_source",
        "source_doc_id",
        "status",
        "progress",
        "phase",
        "progress_pct",
        "celery_task_id",
        "executor_json",
        "error_message",
        "token_usage",
    }
)
_TASK_UPDATE_FIELDS = frozenset(
    {
        "status",
        "progress",
        "phase",
        "progress_pct",
        "celery_task_id",
        "error_message",
        "token_usage",
    }
)


class RDAgentFactorPersistence:
    """管理 RD-Agent 生成的因子数据，供 QuantMind 回测读取共享"""

    async def ensure_tables(self) -> None:
        """确保 rd_agent_factors 表存在（含历史表向后兼容的列）"""
        stmt = """
        CREATE TABLE IF NOT EXISTS rd_agent_factors (
          factor_id TEXT PRIMARY KEY,
          factor_name TEXT NOT NULL,
          factor_code TEXT,
          status TEXT NOT NULL DEFAULT 'pending',
          ic_value DOUBLE PRECISION,
          sharpe_ratio DOUBLE PRECISION,
          annual_return DOUBLE PRECISION,
          max_drawdown DOUBLE PRECISION,
          user_id TEXT,
          tenant_id TEXT NOT NULL DEFAULT '',
          metadata_json JSONB,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        ALTER TABLE rd_agent_factors ADD COLUMN IF NOT EXISTS user_id TEXT;
        ALTER TABLE rd_agent_factors ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT '';
        ALTER TABLE rd_agent_factors ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();
        ALTER TABLE rd_agent_factors ADD COLUMN IF NOT EXISTS market TEXT;
        ALTER TABLE rd_agent_factors ADD COLUMN IF NOT EXISTS universe TEXT;
        ALTER TABLE rd_agent_factors ADD COLUMN IF NOT EXISTS rank_ic DOUBLE PRECISION;
        ALTER TABLE rd_agent_factors ADD COLUMN IF NOT EXISTS factor_formulation TEXT;
        ALTER TABLE rd_agent_factors ADD COLUMN IF NOT EXISTS data_source TEXT;
        ALTER TABLE rd_agent_factors ADD COLUMN IF NOT EXISTS date_range TEXT;
        CREATE INDEX IF NOT EXISTS idx_rd_agent_factors_user_id ON rd_agent_factors(user_id);
        CREATE INDEX IF NOT EXISTS idx_rd_agent_factors_tenant_user ON rd_agent_factors(tenant_id, user_id);
        CREATE INDEX IF NOT EXISTS idx_rd_agent_factors_market ON rd_agent_factors(market);
        CREATE INDEX IF NOT EXISTS idx_rd_agent_factors_universe ON rd_agent_factors(universe);
        """
        async with get_session() as session:
            for s in [x.strip() for x in stmt.split(";") if x.strip()]:
                await session.execute(text(s))
        logger.info("rd_agent_factors table ensured")

        # 迁移 metadata_json 中的字段到专列
        await self.migrate_metadata_to_columns()

    async def migrate_metadata_to_columns(self) -> None:
        """将 metadata_json 中的 market/formulation 等字段提取到专列。幂等执行。"""
        try:
            async with get_session() as session:
                await session.execute(text("""
                    UPDATE rd_agent_factors
                    SET market = metadata_json::jsonb ->> 'market'
                    WHERE market IS NULL
                      AND metadata_json IS NOT NULL
                      AND metadata_json::jsonb ->> 'market' IS NOT NULL
                """))
                await session.execute(text("""
                    UPDATE rd_agent_factors
                    SET factor_formulation = metadata_json::jsonb ->> 'factor_formulation'
                    WHERE factor_formulation IS NULL
                      AND metadata_json IS NOT NULL
                      AND metadata_json::jsonb ->> 'factor_formulation' IS NOT NULL
                """))
            logger.info("rd_agent_factors metadata migration completed")
        except Exception as exc:
            logger.warning("rd_agent_factors metadata migration failed (non-fatal): %s", exc)

    async def save_factor(
        self,
        factor_id: str,
        factor_name: str,
        factor_code: str,
        user_id: str | None = None,
        tenant_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        market: str | None = None,
        universe: str | None = None,
        factor_formulation: str | None = None,
        data_source: str | None = None,
    ) -> None:
        """保存 RD-Agent 生成的因子"""
        meta_json = json.dumps(metadata or {}, ensure_ascii=False)
        async with get_session() as session:
            await session.execute(
                text("""
                    INSERT INTO rd_agent_factors
                        (factor_id, factor_name, factor_code, status, user_id, tenant_id, metadata_json,
                         market, universe, factor_formulation, data_source)
                    VALUES
                        (:factor_id, :factor_name, :factor_code, 'pending', :user_id, :tenant_id, :metadata_json,
                         :market, :universe, :factor_formulation, :data_source)
                    ON CONFLICT (factor_id) DO UPDATE SET
                        factor_name = EXCLUDED.factor_name,
                        factor_code = EXCLUDED.factor_code,
                        updated_at = now()
                    """),
                {
                    "factor_id": factor_id,
                    "factor_name": factor_name,
                    "factor_code": factor_code,
                    "user_id": user_id,
                    "tenant_id": tenant_id or "",
                    "metadata_json": meta_json,
                    "market": market,
                    "universe": universe,
                    "factor_formulation": factor_formulation,
                    "data_source": data_source,
                },
            )

    async def list_factor_revisions(
        self,
        factor_id: str,
        *,
        tenant_id: str | None,
        user_id: str | None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Read immutable source revisions for one factor in the current scope."""
        owner = OwnerScope(tenant_id, user_id)
        if not isinstance(factor_id, str) or not factor_id or len(factor_id) > 128:
            raise ValueError("Invalid factor ID")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Factor revision limit must be between 1 and 100")
        async with get_session() as session:
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT revision.revision_no,revision.factor_name,"
                            "revision.factor_code,revision.factor_formulation,"
                            "revision.created_at FROM rd_agent_factor_revisions AS revision "
                            "JOIN rd_agent_factors AS factor ON factor.factor_id=revision.factor_id "
                            "AND factor.tenant_id=revision.tenant_id "
                            "AND factor.user_id=revision.user_id "
                            "WHERE revision.tenant_id=:tenant_id "
                            "AND revision.user_id=:user_id AND revision.factor_id=:factor_id "
                            "AND factor.tenant_id=:tenant_id AND factor.user_id=:user_id "
                            "ORDER BY revision.revision_no DESC LIMIT :limit"
                        ),
                        {**owner.sql_params(), "factor_id": factor_id, "limit": limit},
                    )
                )
                .mappings()
                .all()
            )
        return [
            {
                "revision_no": row["revision_no"],
                "factor_name": row["factor_name"],
                "factor_code": row["factor_code"],
                "factor_code_sha256": hashlib.sha256(
                    row["factor_code"].encode("utf-8")
                ).hexdigest(),
                "factor_formulation": row["factor_formulation"],
                "created_at": to_utc_iso(row["created_at"]),
            }
            for row in rows
        ]

    async def record_factor_lifecycle_evaluation(
        self,
        factor_id: str,
        evaluation: dict[str, Any],
        *,
        evaluated_at: datetime,
        factor_revision_no: int | None,
        evidence_job_id: str | None,
        user_id: str,
        tenant_id: str,
        idempotency_month_end: str | None = None,
        apply_downweight: bool = True,
    ) -> dict[str, Any]:
        """Atomically update lifecycle metadata and append an immutable event."""
        from backend.shared.factor_lifecycle import lifecycle_write_payload

        owner = OwnerScope(tenant_id, user_id)
        async with get_session() as session:
            result = await session.execute(
                text(
                    "SELECT metadata_json->'lifecycle' AS lifecycle, "
                    "(SELECT MAX(revision_no) FROM rd_agent_factor_revisions AS revision "
                    "WHERE revision.tenant_id=factor.tenant_id "
                    "AND revision.user_id=factor.user_id "
                    "AND revision.factor_id=factor.factor_id) AS current_revision_no "
                    "FROM rd_agent_factors AS factor WHERE factor.factor_id=:factor_id "
                    "AND factor.tenant_id=:tenant_id AND factor.user_id=:user_id "
                    "AND factor.archived_at IS NULL FOR UPDATE"
                ),
                {"factor_id": factor_id, **owner.sql_params()},
            )
            row = result.mappings().first()
            if row is None:
                raise LookupError("Factor owner scope no longer matches")
            if evidence_job_id and factor_revision_no != row.get("current_revision_no"):
                raise ValueError("Factor code changed during lifecycle evaluation")
            current = row.get("lifecycle")
            if isinstance(current, str):
                try:
                    current = json.loads(current)
                except ValueError:
                    current = {}
            current = dict(current) if isinstance(current, dict) else {}
            if idempotency_month_end:
                prior = await session.execute(
                    text(
                        "SELECT 1 FROM rd_agent_factor_lifecycle_events "
                        "WHERE tenant_id=:tenant_id AND user_id=:user_id "
                        "AND factor_id=:factor_id AND event_type='evaluation' "
                        "AND event_snapshot #>> "
                        "'{evaluation,verdict,latest,month_end}'=:month_end "
                        "AND factor_revision_no IS NOT DISTINCT FROM :revision_no "
                        "LIMIT 1"
                    ),
                    {
                        **owner.sql_params(),
                        "factor_id": factor_id,
                        "month_end": idempotency_month_end,
                        "revision_no": factor_revision_no,
                    },
                )
                if prior.first() is not None:
                    return current
            lifecycle = lifecycle_write_payload(
                evaluation,
                existing_lifecycle=current,
                now=evaluated_at,
                apply_downweight=apply_downweight,
            )
            update = await session.execute(
                text(
                    "UPDATE rd_agent_factors SET metadata_json=jsonb_set("
                    "COALESCE(metadata_json,'{}'::jsonb),'{lifecycle}',"
                    "CAST(:lifecycle AS jsonb),true),updated_at=:updated_at "
                    "WHERE factor_id=:factor_id AND tenant_id=:tenant_id "
                    "AND user_id=:user_id"
                ),
                {
                    "lifecycle": json.dumps(lifecycle, ensure_ascii=False),
                    "updated_at": evaluated_at,
                    "factor_id": factor_id,
                    **owner.sql_params(),
                },
            )
            if update.rowcount != 1:
                raise LookupError("Factor owner scope no longer matches")
            snapshot = {"evaluation": evaluation, "lifecycle": lifecycle}
            await session.execute(
                text(
                    "INSERT INTO rd_agent_factor_lifecycle_events "
                    "(event_id,tenant_id,user_id,factor_id,event_type,evaluated_at,"
                    "factor_revision_no,evidence_job_id,event_snapshot) SELECT "
                    "COALESCE(MAX(event_id),0)+1,:tenant_id,:user_id,:factor_id,"
                    "'evaluation',:evaluated_at,:factor_revision_no,:evidence_job_id,"
                    "CAST(:event_snapshot AS jsonb) FROM rd_agent_factor_lifecycle_events "
                    "WHERE tenant_id=:tenant_id AND user_id=:user_id "
                    "AND factor_id=:factor_id"
                ),
                {
                    **owner.sql_params(),
                    "factor_id": factor_id,
                    "evaluated_at": evaluated_at,
                    "factor_revision_no": factor_revision_no,
                    "evidence_job_id": evidence_job_id,
                    "event_snapshot": json.dumps(snapshot, ensure_ascii=False),
                },
            )
            return lifecycle

    async def list_factor_lifecycle_events(
        self,
        factor_id: str,
        *,
        user_id: str,
        tenant_id: str,
        limit: int = 50,
        before_event_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Read a bounded page of lifecycle events for exactly one owner."""
        owner = OwnerScope(tenant_id, user_id)
        if not 1 <= limit <= 100:
            raise ValueError("Lifecycle history page size must be between 1 and 100")
        cursor_clause = (
            " AND event_id < :before_event_id" if before_event_id is not None else ""
        )
        params: dict[str, Any] = {
            **owner.sql_params(),
            "factor_id": factor_id,
            "limit": limit,
        }
        if before_event_id is not None:
            params["before_event_id"] = before_event_id
        async with get_session(read_only=True) as session:
            result = await session.execute(
                text(
                    "SELECT event_id,event_type,evaluated_at,factor_revision_no,"
                    "evidence_job_id,event_snapshot FROM "
                    "rd_agent_factor_lifecycle_events WHERE tenant_id=:tenant_id "
                    "AND user_id=:user_id AND factor_id=:factor_id"
                    f"{cursor_clause} ORDER BY event_id DESC LIMIT :limit"
                ),
                params,
            )
            return [dict(row) for row in result.mappings().all()]

    async def restore_factor_lifecycle(
        self,
        factor_id: str,
        reason: str,
        *,
        user_id: str,
        tenant_id: str,
    ) -> dict[str, Any]:
        """Clear a retired override and append a private, owner-scoped audit event."""
        owner = OwnerScope(tenant_id, user_id)
        if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 500:
            raise ValueError(
                "A restore reason between 1 and 500 characters is required"
            )
        now = utc_now()
        async with get_session() as session:
            result = await session.execute(
                text(
                    "SELECT metadata_json->'lifecycle' AS lifecycle "
                    "FROM rd_agent_factors WHERE factor_id=:factor_id "
                    "AND tenant_id=:tenant_id AND user_id=:user_id "
                    "AND archived_at IS NULL FOR UPDATE"
                ),
                {"factor_id": factor_id, **owner.sql_params()},
            )
            row = result.mappings().first()
            if row is None:
                raise LookupError("Factor owner scope no longer matches")
            lifecycle = row.get("lifecycle")
            if isinstance(lifecycle, str):
                try:
                    lifecycle = json.loads(lifecycle)
                except ValueError:
                    lifecycle = {}
            lifecycle = dict(lifecycle) if isinstance(lifecycle, dict) else {}
            if lifecycle.get("state") != "downweighted":
                raise ValueError("Only a downweighted factor can be restored")
            events = lifecycle.get("events")
            events = list(events) if isinstance(events, list) else []
            events.append(
                {
                    "at": to_utc_iso(now),
                    "from": "downweighted",
                    "to": None,
                    "action": "manual_restore",
                    "actor_user_id": owner.user_id,
                    "reason": reason.strip(),
                }
            )
            lifecycle["events"] = events[-24:]
            for key in ("state", "since", "reason", "evidence", "blind_until"):
                lifecycle.pop(key, None)
            update = await session.execute(
                text(
                    "UPDATE rd_agent_factors SET metadata_json=jsonb_set("
                    "COALESCE(metadata_json,'{}'::jsonb),'{lifecycle}',"
                    "CAST(:lifecycle AS jsonb),true),updated_at=:updated_at "
                    "WHERE factor_id=:factor_id AND tenant_id=:tenant_id "
                    "AND user_id=:user_id"
                ),
                {
                    "lifecycle": json.dumps(lifecycle, ensure_ascii=False),
                    "updated_at": now,
                    "factor_id": factor_id,
                    **owner.sql_params(),
                },
            )
            if update.rowcount != 1:
                raise LookupError("Factor owner scope no longer matches")
            await session.execute(
                text(
                    "INSERT INTO rd_agent_factor_lifecycle_events "
                    "(event_id,tenant_id,user_id,factor_id,event_type,evaluated_at,event_snapshot) "
                    "SELECT COALESCE(MAX(event_id),0)+1,:tenant_id,:user_id,:factor_id,"
                    "'manual_restore',:evaluated_at,CAST(:event_snapshot AS jsonb) "
                    "FROM rd_agent_factor_lifecycle_events WHERE tenant_id=:tenant_id "
                    "AND user_id=:user_id AND factor_id=:factor_id"
                ),
                {
                    **owner.sql_params(),
                    "factor_id": factor_id,
                    "evaluated_at": now,
                    "event_snapshot": json.dumps(
                        {
                            "action": "manual_restore",
                            "reason": reason.strip(),
                            "actor_user_id": owner.user_id,
                            "lifecycle": lifecycle,
                        },
                        ensure_ascii=False,
                    ),
                },
            )
            return lifecycle

    async def list_factors(
        self,
        user_id: str | None = None,
        status: str | None = None,
        market: str | None = None,
        universe: str | None = None,
        limit: int = 50,
        task_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """列出因子（支持按状态、用户、市场、宇宙、来源任务过滤）"""
        conditions = []
        params: dict[str, Any] = {"limit": limit}
        if user_id:
            conditions.append("user_id = :user_id")
            params["user_id"] = user_id
        if status:
            conditions.append("status = :status")
            params["status"] = status
        if market:
            conditions.append("market = :market")
            params["market"] = market
        if universe:
            conditions.append("universe = :universe")
            params["universe"] = universe
        if task_id:
            conditions.append("metadata_json->>'task_id' = :task_id")
            params["task_id"] = task_id

        where = " AND ".join(conditions) if conditions else "1=1"
        async with get_session(read_only=True) as session:
            rows = await session.execute(
                text(f"""
                    SELECT factor_id, factor_name, factor_code, status, ic_value, sharpe_ratio,
                           annual_return, max_drawdown, rank_ic, user_id, market, universe,
                           factor_formulation, data_source, date_range, metadata_json, created_at, updated_at
                    FROM rd_agent_factors
                    WHERE {where}
                    ORDER BY created_at DESC
                    LIMIT :limit
                    """),
                params,
            )
            data = rows.mappings().all()
            results = []
            for r in data:
                item = dict(r)
                raw_meta = item.pop("metadata_json", None)
                if isinstance(raw_meta, dict):
                    item["metadata"] = raw_meta
                elif isinstance(raw_meta, str):
                    try:
                        item["metadata"] = json.loads(raw_meta)
                    except Exception:
                        item["metadata"] = {}
                else:
                    item["metadata"] = {}
                results.append(item)
            return results

    async def get_factor(self, factor_id: str) -> dict[str, Any] | None:
        """获取单个因子详情"""
        async with get_session(read_only=True) as session:
            row = await session.execute(
                text("""
                    SELECT factor_id, factor_name, factor_code, status, ic_value, sharpe_ratio,
                           annual_return, max_drawdown, rank_ic, user_id, market, universe,
                           factor_formulation, data_source, date_range, metadata_json, created_at, updated_at
                    FROM rd_agent_factors
                    WHERE factor_id = :factor_id
                    """),
                {"factor_id": factor_id},
            )
            r = row.mappings().first()
            if not r:
                return None
            item = dict(r)
            raw_meta = item.pop("metadata_json", None)
            if isinstance(raw_meta, dict):
                item["metadata"] = raw_meta
            elif isinstance(raw_meta, str):
                try:
                    item["metadata"] = json.loads(raw_meta)
                except Exception:
                    item["metadata"] = {}
            else:
                item["metadata"] = {}
            return item

    async def update_factor_metrics(
        self,
        factor_id: str,
        status: str | None = None,
        ic_value: float | None = None,
        sharpe_ratio: float | None = None,
        annual_return: float | None = None,
        max_drawdown: float | None = None,
        rank_ic: float | None = None,
        icir: float | None = None,
        rank_icir: float | None = None,
        universe: str | None = None,
        date_range: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """更新因子的回测指标，metadata 与已有值合并而非覆盖

        icir / rank_icir 不是表字段，写入 metadata_json（前端从 metadata 读取）。
        """
        fields: dict[str, Any] = {"updated_at": datetime.now()}
        if status is not None:
            fields["status"] = status
        if ic_value is not None:
            fields["ic_value"] = ic_value
        if sharpe_ratio is not None:
            fields["sharpe_ratio"] = sharpe_ratio
        if annual_return is not None:
            fields["annual_return"] = annual_return
        if max_drawdown is not None:
            fields["max_drawdown"] = max_drawdown
        if rank_ic is not None:
            fields["rank_ic"] = rank_ic
        if universe is not None:
            fields["universe"] = universe
        if date_range is not None:
            fields["date_range"] = date_range
        metric_meta: dict[str, Any] = {}
        if icir is not None:
            metric_meta["icir"] = icir
        if rank_icir is not None:
            metric_meta["rank_icir"] = rank_icir
        if metadata is not None or metric_meta or status == "completed":
            # Merge with existing metadata to preserve task_id, market, etc.
            async with get_session(read_only=True) as session:
                row = await session.execute(
                    text("SELECT metadata_json FROM rd_agent_factors WHERE factor_id = :factor_id"),
                    {"factor_id": factor_id},
                )
                existing = row.scalar()
            merged = {}
            if existing:
                try:
                    merged = json.loads(existing) if isinstance(existing, str) else (existing or {})
                except Exception:
                    merged = {}
            merged.update(metadata or {})
            merged.update(metric_meta)
            if status == "completed":
                # 回测成功时清掉历史失败原因，否则前端会把上一次的报错当成当前状态
                merged["backtest_error"] = None
            fields["metadata_json"] = json.dumps(merged, ensure_ascii=False)

        set_clause = ", ".join(f"{k} = :{k}" for k in fields)
        async with get_session() as session:
            await session.execute(
                text(f"""
                    UPDATE rd_agent_factors SET {set_clause}
                    WHERE factor_id = :factor_id
                    """),
                {**fields, "factor_id": factor_id},
            )

    async def recover_stuck_factors(
        self, max_age_min: int = 15, target_status: str = "backtesting"
    ) -> int:
        """恢复卡死超过 max_age_min 分钟的因子（默认 backtesting 状态）。

        把超时的 status 改成 failed，并在 metadata.backtest_error 写入原因。
        用于引擎进程崩溃 / 600s subprocess timeout 后清理。

        Returns: 受影响的行数。
        """
        async with get_session() as session:
            result = await session.execute(
                text("""
                    UPDATE rd_agent_factors
                    SET status = 'failed',
                        metadata_json = jsonb_set(
                            COALESCE(metadata_json, '{}'::jsonb),
                            '{backtest_error}',
                            to_jsonb('timeout_or_engine_crash'::text),
                            true
                        ),
                        updated_at = now()
                    WHERE status = :s
                      AND updated_at < now() - (:max_age_min || ' minutes')::interval
                    """),
                {"s": target_status, "max_age_min": str(max_age_min)},
            )
            count = result.rowcount or 0
            if count:
                logger.warning(
                    "Recovered %d stuck factors (status=%s, older than %d min)",
                    count, target_status, max_age_min,
                )
            return count

# ---------------------------------------------------------------------------
# OSS-ADAPT: AlphaMiningTaskPersistence（企业版同文件类移植，逐字搬运）。
# - ensure_tables 保留：OSS schema.task_schema_statements() 返回 []，
#   调用为无害空操作（OSS 建表走 db_init.sql 第 68 节 + data/upgrade_v1.0.16.sql）。
# - resource_lease_pools 查询保留（建池锁/取消锁/排队位置）：表 + alpha_mining
#   种子行（ready=true）由 db_init.sql 第 68 节与 upgrade_v1.0.16.sql 提供。
# - 源类经 grep 确认无 CVM / 远端执行 / 计费分支，无需剔除。
# - tenant_id / user_id 硬隔离保留（OwnerScope 双维度）。
# - 注意：create / request_cancel / queue_position 读写 resource_task_leases，
#   该表在 OSS 尚无 DDL（见 upgrade_v1.0.16.sql 文件头），相关路径需后续补表。
# ---------------------------------------------------------------------------
class AlphaMiningTaskPersistence:
    """因子挖掘任务表（多用户硬隔离的事实源，替代 launcher 内存 dict）。"""

    class CapacityExceeded(RuntimeError):
        def __init__(self, *, user_count: int, total_count: int):
            self.user_count = user_count
            self.total_count = total_count
            super().__init__("Alpha mining admission capacity exceeded")

    async def ensure_tables(self) -> None:
        from backend.services.engine.alpha_agent.schema import task_schema_statements

        async with get_session() as session:
            for statement in task_schema_statements():
                await session.execute(text(statement))
        logger.info("alpha_mining_tasks table ensured")

    async def create(
        self,
        task_id: str,
        tenant_id: str,
        user_id: str,
        *,
        max_per_user: int | None = None,
        max_global: int | None = None,
        specification: MiningTaskSpec | None = None,
        **fields: Any,
    ) -> str:
        from backend.shared.resource_lease import LeaseUnavailable

        if not tenant_id or not user_id:
            raise ValueError("Both task owner dimensions are required")
        owner = OwnerScope(tenant_id, user_id)
        if fields.keys() - _TASK_CREATE_FIELDS:
            raise ValueError("Unsupported task creation fields")
        if fields.get("status", "queued") not in {"pending", "queued"}:
            raise ValueError("New tasks must enter through the queue")
        tenant_id, user_id = owner.tenant_id, owner.user_id
        if any(n is not None and n <= 0 for n in (max_per_user, max_global)):
            raise ValueError("Factor-mining capacity limits must be positive")
        if specification is not None:
            if not isinstance(specification, MiningTaskSpec):
                raise ValueError("Invalid mining task specification")
            parameters = json.loads(specification.params_json)
            defaults = {
                "market": "a_share",
                "universe": "csi300",
                "loop_n": 3,
                "direction": "",
                "data_source": "",
            }
            if any(
                parameters.get(key, default) != fields.get(key, default)
                for key, default in defaults.items()
            ):
                raise ValueError("Mining snapshot does not match execution parameters")
            fields["execution_fingerprint"] = specification.execution_fingerprint
        cols = ["task_id", "tenant_id", "user_id"] + list(fields.keys())
        placeholders = ", ".join(f":{c}" for c in cols)
        async with get_session() as session:
            # Share the execution pool's lock: the queue entry and business row
            # commit together, so a crash cannot leave an invisible FIFO blocker.
            ready = (
                await session.execute(
                    text(
                        "SELECT ready FROM resource_lease_pools "
                        "WHERE pool='alpha_mining' FOR UPDATE"
                    )
                )
            ).scalar_one_or_none()
            if not ready:
                raise LeaseUnavailable("Alpha mining pool awaits reconciliation")
            if specification is not None:
                if specification.request_key is not None:
                    existing = (
                        (
                            await session.execute(
                                text(
                                    "SELECT task_id,request_fingerprint FROM alpha_mining_request_keys "
                                    "WHERE tenant_id=:tenant_id AND user_id=:user_id "
                                    "AND request_key=:request_key"
                                ),
                                {
                                    **owner.sql_params(),
                                    "request_key": specification.request_key,
                                },
                            )
                        )
                        .mappings()
                        .first()
                    )
                    if existing:
                        if (
                            existing["request_fingerprint"]
                            != specification.request_fingerprint
                        ):
                            raise IdempotencyConflict(
                                "Idempotency key belongs to different input"
                            )
                        return existing["task_id"]
                existing = (
                    (
                        await session.execute(
                            text(
                                "SELECT task_id FROM alpha_mining_tasks "
                                "WHERE tenant_id=:tenant_id AND user_id=:user_id "
                                "AND execution_fingerprint=:fingerprint "
                                "AND status IN ('pending','queued','running','stopping')"
                            ),
                            {
                                **owner.sql_params(),
                                "fingerprint": specification.execution_fingerprint,
                            },
                        )
                    )
                    .mappings()
                    .first()
                )
                if existing:
                    await self._bind_request(
                        session, owner, existing["task_id"], specification
                    )
                    return existing["task_id"]
            if specification is not None:
                from backend.services.engine.alpha_agent.factor_visibility import (
                    lock_active_factors_async,
                )

                seed_ids = parameters.get("seed_factor_ids") or []
                if not isinstance(seed_ids, list) or any(
                    not isinstance(value, str) or not value for value in seed_ids
                ):
                    raise ValueError("Invalid selected parent factor references")
                await lock_active_factors_async(session, owner, seed_ids)
            source_doc_id = fields.get("source_doc_id")
            if source_doc_id is not None:
                source_doc = (
                    await session.execute(
                        text(
                            "SELECT doc_id FROM rd_agent_docs "
                            "WHERE tenant_id=:tenant_id AND user_id=:user_id "
                            "AND doc_id=:doc_id AND status='organized' "
                            "AND organize_state='completed' FOR UPDATE"
                        ),
                        {**owner.sql_params(), "doc_id": source_doc_id},
                    )
                ).scalar_one_or_none()
                if source_doc is None:
                    raise ValueError(
                        "Source document is no longer owned and ready for mining"
                    )
            counts = (
                (
                    await session.execute(
                        text(
                            "SELECT COUNT(*) AS total_count, "
                            "COUNT(*) FILTER (WHERE tenant_id=:tenant_id AND user_id=:user_id) AS user_count "
                            "FROM alpha_mining_tasks WHERE status IN ('pending','queued','running','stopping')"
                        ),
                        owner.sql_params(),
                    )
                )
                .mappings()
                .one()
            )
            user_count = int(counts["user_count"])
            total_count = int(counts["total_count"])
            if (max_per_user is not None and user_count >= max_per_user) or (
                max_global is not None and total_count >= max_global
            ):
                raise self.CapacityExceeded(
                    user_count=user_count, total_count=total_count
                )
            await session.execute(
                text(
                    "INSERT INTO resource_task_leases "
                    "(pool,task_id,tenant_id,user_id,state) "
                    "VALUES ('alpha_mining',:task_id,:tenant_id,:user_id,'queued')"
                ),
                {"task_id": task_id, "tenant_id": tenant_id, "user_id": user_id},
            )
            result = await session.execute(
                text(
                    f"INSERT INTO alpha_mining_tasks ({', '.join(cols)}) VALUES ({placeholders})"
                ),
                {
                    "task_id": task_id,
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                    **fields,
                },
            )
            if result.rowcount != 1:
                raise ValueError("Alpha mining task ID already exists")
            if specification is not None:
                await session.execute(
                    text(
                        "INSERT INTO alpha_mining_task_specs "
                        "(tenant_id,user_id,task_id,schema_version,algorithm,algorithm_version,"
                        "request_key,request_fingerprint,request_json,params_json,data_snapshot) "
                        "VALUES (:tenant_id,:user_id,:task_id,:schema_version,:algorithm,:version,"
                        ":request_key,:fingerprint,CAST(:request AS jsonb),CAST(:params AS jsonb),"
                        "CAST(:data AS jsonb))"
                    ),
                    {
                        **owner.sql_params(),
                        "task_id": task_id,
                        "schema_version": specification.schema_version,
                        "algorithm": specification.algorithm,
                        "version": specification.algorithm_version,
                        "request_key": specification.request_key,
                        "fingerprint": specification.request_fingerprint,
                        "request": specification.request_json,
                        "params": specification.params_json,
                        "data": specification.data_snapshot_json,
                    },
                )
                await self._bind_request(session, owner, task_id, specification)
            return task_id

    @staticmethod
    async def _bind_request(
        session, owner: OwnerScope, task_id: str, spec: MiningTaskSpec
    ) -> None:
        if spec.request_key is None:
            return
        await session.execute(
            text(
                "INSERT INTO alpha_mining_request_keys "
                "(tenant_id,user_id,request_key,task_id,request_fingerprint,request_json) "
                "VALUES (:tenant_id,:user_id,:key,:task,:fingerprint,CAST(:request AS jsonb))"
            ),
            {
                **owner.sql_params(),
                "key": spec.request_key,
                "task": task_id,
                "fingerprint": spec.request_fingerprint,
                "request": spec.request_json,
            },
        )

    async def get_by_request_key(
        self, request_key: str, *, tenant_id: str, user_id: str
    ) -> dict[str, Any] | None:
        """Recover a submitted task on the primary without re-running admission."""
        owner = OwnerScope(tenant_id, user_id)
        async with get_session() as session:
            result = await session.execute(
                text(
                    "SELECT task.* FROM alpha_mining_request_keys AS binding "
                    "JOIN alpha_mining_tasks AS task "
                    "ON task.task_id=binding.task_id "
                    "AND task.tenant_id=binding.tenant_id "
                    "AND task.user_id=binding.user_id "
                    "WHERE binding.request_key=:request_key "
                    "AND binding.tenant_id=:tenant_id AND binding.user_id=:user_id "
                    "AND task.tenant_id=:tenant_id AND task.user_id=:user_id"
                ),
                {"request_key": request_key, **owner.sql_params()},
            )
            row = result.mappings().first()
            return dict(row) if row else None

    async def get(
        self,
        task_id: str,
        *,
        tenant_id: str | None = None,
        user_id: str | None = None,
        consistent: bool = False,
    ) -> dict[str, Any] | None:
        owner = OwnerScope(tenant_id, user_id)
        conditions = [
            "task_id = :task_id",
            "tenant_id = :tenant_id",
            "user_id = :user_id",
        ]
        params = {"task_id": task_id, **owner.sql_params()}
        async with get_session(read_only=not consistent) as session:
            row = await session.execute(
                text(
                    f"SELECT * FROM alpha_mining_tasks WHERE {' AND '.join(conditions)}"
                ),
                params,
            )
            r = row.mappings().first()
            return dict(r) if r else None

    async def get_for_owner_with_spec(
        self,
        task_id: str,
        *,
        tenant_id: str,
        user_id: str,
        consistent: bool = False,
    ) -> dict[str, Any] | None:
        """Read one owner's mining task and immutable execution spec."""
        owner = OwnerScope(tenant_id, user_id)
        async with get_session(read_only=not consistent) as session:
            result = await session.execute(
                text(
                    "SELECT task.*, COALESCE(spec.algorithm, 'rdagent') AS algorithm, "
                    "spec.algorithm_version, spec.params_json AS params_snapshot, "
                    "spec.request_json AS request_snapshot, "
                    "spec.data_snapshot AS data_snapshot "
                    "FROM alpha_mining_tasks AS task "
                    "LEFT JOIN alpha_mining_task_specs AS spec "
                    "ON spec.task_id=task.task_id AND spec.tenant_id=task.tenant_id "
                    "AND spec.user_id=task.user_id "
                    "WHERE task.task_id=:task_id AND task.tenant_id=:tenant_id "
                    "AND task.user_id=:user_id"
                ),
                {"task_id": task_id, **owner.sql_params()},
            )
            row = result.mappings().first()
            return dict(row) if row else None

    async def get_for_dispatch(self, task_id: str) -> dict[str, Any] | None:
        """Trusted queue consumer lookup; never use for HTTP resource reads.

        The queue owns a durable task ID, not a caller-supplied identity. Resolve
        the owner here and require it before acquiring the execution lease.
        """
        async with get_session(read_only=True) as session:
            result = await session.execute(
                text(
                    "SELECT task.*, COALESCE(spec.algorithm, 'rdagent') AS algorithm, "
                    "spec.algorithm_version, spec.params_json AS params_snapshot, "
                    "spec.request_json AS request_snapshot, "
                    "spec.data_snapshot AS data_snapshot "
                    "FROM alpha_mining_tasks AS task "
                    "LEFT JOIN alpha_mining_task_specs AS spec "
                    "ON spec.task_id = task.task_id "
                    "AND spec.tenant_id = task.tenant_id "
                    "AND spec.user_id = task.user_id "
                    "WHERE task.task_id = :task_id"
                ),
                {"task_id": task_id},
            )
            row = result.mappings().first()
            if row is None:
                return None
            owner = OwnerScope(row.get("tenant_id"), row.get("user_id"))
            if any(row.get(key) != value for key, value in owner.sql_params().items()):
                raise ValueError("Persisted task owner is not canonical")
            return dict(row)

    async def seal_data_snapshot(
        self,
        task_id: str,
        *,
        tenant_id: str,
        user_id: str,
        data_snapshot: dict[str, Any],
    ) -> str:
        """Seal a statistical task's captured data exactly once before calculation."""
        owner = OwnerScope(tenant_id, user_id)
        if (
            not isinstance(data_snapshot, dict)
            or data_snapshot.get("status") != "frozen"
        ):
            raise ValueError("A frozen statistical data snapshot is required")
        for key in ("panel_sha256", "metadata_sha256", "symbols_sha256"):
            value = data_snapshot.get(key)
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(char not in "0123456789abcdef" for char in value)
            ):
                raise ValueError(f"Invalid frozen data snapshot field: {key}")
        encoded_snapshot = json.dumps(
            data_snapshot,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        if len(encoded_snapshot.encode("utf-8")) > 256 * 1024:
            raise ValueError("Frozen data snapshot exceeds 256 KiB")

        async with get_session() as session:
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT task.status,task.execution_fingerprint,spec.algorithm,"
                            "spec.algorithm_version,spec.request_json,spec.params_json,"
                            "spec.data_snapshot FROM alpha_mining_tasks AS task "
                            "JOIN alpha_mining_task_specs AS spec ON spec.task_id=task.task_id "
                            "AND spec.tenant_id=task.tenant_id AND spec.user_id=task.user_id "
                            "WHERE task.task_id=:task AND task.tenant_id=:tenant_id "
                            "AND task.user_id=:user_id FOR UPDATE OF task,spec"
                        ),
                        {"task": task_id, **owner.sql_params()},
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise LookupError("Statistical task owner scope no longer matches")
            if row["algorithm"] != "statistical":
                raise ValueError("Only statistical task data can be sealed")
            if row["status"] != "running":
                raise ValueError("Statistical task must be running to seal its data")
            current_snapshot = row["data_snapshot"]
            if isinstance(current_snapshot, str):
                current_snapshot = json.loads(current_snapshot)
            if current_snapshot.get("status") == "frozen":
                supplied = dict(data_snapshot)
                if (
                    "variant_memory" not in supplied
                    and "variant_memory" in current_snapshot
                ):
                    supplied["variant_memory"] = current_snapshot["variant_memory"]
                if current_snapshot != supplied:
                    raise ValueError(
                        "Statistical task data snapshot is already immutable"
                    )
                return row["execution_fingerprint"]
            if current_snapshot.get("status") != "preparing":
                raise ValueError(
                    "Statistical task is not awaiting data snapshot sealing"
                )

            request = row["request_json"]
            params = row["params_json"]
            if isinstance(request, str):
                request = json.loads(request)
            if isinstance(params, str):
                params = json.loads(params)
            from backend.services.engine.alpha_agent.statistical.variant_ledger import (
                freeze_memory,
            )

            data_snapshot = {
                **data_snapshot,
                "variant_memory": await freeze_memory(
                    session,
                    owner=owner,
                    algorithm_version=row["algorithm_version"],
                    params=params,
                    snapshot=data_snapshot,
                ),
            }
            encoded_snapshot = json.dumps(
                data_snapshot,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            if len(encoded_snapshot.encode("utf-8")) > 256 * 1024:
                raise ValueError("Frozen statistical memory snapshot exceeds 256 KiB")
            spec = MiningTaskSpec.build(
                algorithm="statistical",
                algorithm_version=row["algorithm_version"],
                request=request,
                params=params,
                data_snapshot=data_snapshot,
            )
            updated_spec = await session.execute(
                text(
                    "UPDATE alpha_mining_task_specs SET data_snapshot=CAST(:snapshot AS jsonb) "
                    "WHERE task_id=:task AND tenant_id=:tenant_id AND user_id=:user_id "
                    "AND algorithm='statistical'"
                ),
                {"snapshot": encoded_snapshot, "task": task_id, **owner.sql_params()},
            )
            updated_task = await session.execute(
                text(
                    "UPDATE alpha_mining_tasks SET execution_fingerprint=:fingerprint, "
                    "updated_at=now() WHERE task_id=:task AND tenant_id=:tenant_id "
                    "AND user_id=:user_id AND status='running' "
                    "AND execution_fingerprint=:previous_fingerprint"
                ),
                {
                    "fingerprint": spec.execution_fingerprint,
                    "previous_fingerprint": row["execution_fingerprint"],
                    "task": task_id,
                    **owner.sql_params(),
                },
            )
            if updated_spec.rowcount != 1 or updated_task.rowcount != 1:
                raise RuntimeError(
                    "Statistical task data snapshot sealing lost its fence"
                )
            return spec.execution_fingerprint

    async def save_result(
        self,
        task_id: str,
        *,
        tenant_id: str,
        user_id: str,
        execution_fingerprint: str,
        result: dict[str, Any],
        max_bytes: int = 4 * 1024 * 1024,
    ) -> None:
        """Persist one immutable result under the task's exact owner and spec."""
        owner = OwnerScope(tenant_id, user_id)
        if not isinstance(result, dict):
            raise ValueError("Mining result must be an object")
        if (
            not isinstance(execution_fingerprint, str)
            or len(execution_fingerprint) != 64
            or any(char not in "0123456789abcdef" for char in execution_fingerprint)
        ):
            raise ValueError("A valid execution fingerprint is required")
        encoded = json.dumps(
            result,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        if len(encoded.encode("utf-8")) > max_bytes:
            raise ValueError(
                f"Mining result exceeds the {max_bytes}-byte storage limit"
            )

        async with get_session() as session:
            task = (
                (
                    await session.execute(
                        text(
                            "SELECT status, execution_fingerprint "
                            "FROM alpha_mining_tasks WHERE task_id=:task "
                            "AND tenant_id=:tenant_id AND user_id=:user_id FOR UPDATE"
                        ),
                        {"task": task_id, **owner.sql_params()},
                    )
                )
                .mappings()
                .first()
            )
            if task is None:
                raise LookupError("Mining task owner scope no longer matches")
            if task["execution_fingerprint"] != execution_fingerprint:
                raise ValueError("Mining result does not match the frozen task spec")
            if task["status"] != "running":
                raise ValueError("Mining task is not in a result-persistable state")

            inserted = await session.execute(
                text(
                    "INSERT INTO alpha_mining_task_results "
                    "(tenant_id,user_id,task_id,execution_fingerprint,result_json) "
                    "VALUES (:tenant_id,:user_id,:task, :fingerprint,CAST(:result AS jsonb)) "
                    "ON CONFLICT (tenant_id,user_id,task_id) DO NOTHING"
                ),
                {
                    **owner.sql_params(),
                    "task": task_id,
                    "fingerprint": execution_fingerprint,
                    "result": encoded,
                },
            )
            if inserted.rowcount == 1:
                return
            existing = (
                (
                    await session.execute(
                        text(
                            "SELECT execution_fingerprint,result_json "
                            "FROM alpha_mining_task_results WHERE task_id=:task "
                            "AND tenant_id=:tenant_id AND user_id=:user_id"
                        ),
                        {"task": task_id, **owner.sql_params()},
                    )
                )
                .mappings()
                .first()
            )
            if (
                existing is None
                or existing["execution_fingerprint"] != execution_fingerprint
                or existing["result_json"] != result
            ):
                raise RuntimeError("A different immutable result already exists")

    async def complete_with_result(
        self,
        task_id: str,
        *,
        tenant_id: str,
        user_id: str,
        execution_fingerprint: str,
        result: dict[str, Any],
        factors: list[dict[str, Any]] | None = None,
        max_bytes: int = 4 * 1024 * 1024,
    ) -> bool:
        """Atomically store the immutable result and complete its running task."""
        owner = OwnerScope(tenant_id, user_id)
        if not isinstance(result, dict):
            raise ValueError("Mining result must be an object")
        if (
            not isinstance(execution_fingerprint, str)
            or len(execution_fingerprint) != 64
            or any(char not in "0123456789abcdef" for char in execution_fingerprint)
        ):
            raise ValueError("A valid execution fingerprint is required")
        encoded = json.dumps(
            result,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        if len(encoded.encode("utf-8")) > max_bytes:
            raise ValueError(
                f"Mining result exceeds the {max_bytes}-byte storage limit"
            )
        factor_rows = factors or []
        if len(factor_rows) > 33:
            raise ValueError("Statistical task has too many selected factors")
        validated_factors = []
        seen_ids = set()
        for factor in factor_rows:
            required = {
                "factor_id",
                "factor_name",
                "factor_code",
                "rank_ic",
                "factor_formulation",
                "market",
                "universe",
                "date_range",
                "data_source",
                "metadata",
            }
            if not isinstance(factor, dict) or factor.keys() != required:
                raise ValueError("Statistical factor row has an invalid schema")
            if (
                not isinstance(factor["factor_id"], str)
                or not factor["factor_id"]
                or len(factor["factor_id"]) > 128
                or factor["factor_id"] in seen_ids
                or not isinstance(factor["factor_name"], str)
                or not factor["factor_name"]
                or len(factor["factor_name"]) > 256
                or not isinstance(factor["factor_code"], str)
                or not factor["factor_code"]
                or len(factor["factor_code"]) > 8192
                or not isinstance(factor["factor_formulation"], str)
                or len(factor["factor_formulation"]) > 2000
                or factor["market"] != "a_share"
                or not isinstance(factor["universe"], str)
                or len(factor["universe"]) > 128
                or not isinstance(factor["date_range"], str)
                or len(factor["date_range"]) > 64
                or not isinstance(factor["data_source"], str)
                or not isinstance(factor["metadata"], dict)
                or factor["metadata"].get("task_id") != task_id
                or factor["metadata"].get("algorithm") != "statistical"
                or factor["metadata"].get("execution_fingerprint")
                != execution_fingerprint
                or (
                    factor["rank_ic"] is not None
                    and (
                        type(factor["rank_ic"]) not in (int, float)
                        or not math.isfinite(factor["rank_ic"])
                    )
                )
            ):
                raise ValueError("Statistical factor row contains invalid values")
            seen_ids.add(factor["factor_id"])
            metadata_json = json.dumps(
                factor["metadata"],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            if len(metadata_json.encode("utf-8")) > 256 * 1024:
                raise ValueError("Statistical factor metadata exceeds 256 KiB")
            validated_factors.append({**factor, "metadata_json": metadata_json})

        async with get_session() as session:
            task = (
                (
                    await session.execute(
                        text(
                            "SELECT status, execution_fingerprint FROM alpha_mining_tasks "
                            "WHERE task_id=:task AND tenant_id=:tenant_id "
                            "AND user_id=:user_id FOR UPDATE"
                        ),
                        {"task": task_id, **owner.sql_params()},
                    )
                )
                .mappings()
                .first()
            )
            if task is None:
                raise LookupError("Mining task owner scope no longer matches")
            if task["execution_fingerprint"] != execution_fingerprint:
                raise ValueError("Mining result does not match the frozen task spec")
            if task["status"] != "running":
                return False

            for factor in validated_factors:
                inserted_factor = await session.execute(
                    text(
                        "INSERT INTO rd_agent_factors "
                        "(factor_id,factor_name,factor_code,status,user_id,tenant_id,"
                        "metadata_json,market,universe,rank_ic,ic_value,factor_formulation,"
                        "data_source,date_range) VALUES (:factor_id,:factor_name,:factor_code,"
                        "'pending',:user_id,:tenant_id,CAST(:metadata AS jsonb),:market,"
                        ":universe,:rank_ic,:rank_ic,:factor_formulation,:data_source,:date_range) "
                        "ON CONFLICT (factor_id) DO NOTHING"
                    ),
                    {
                        **owner.sql_params(),
                        "factor_id": factor["factor_id"],
                        "factor_name": factor["factor_name"],
                        "factor_code": factor["factor_code"],
                        "metadata": factor["metadata_json"],
                        "market": factor["market"],
                        "universe": factor["universe"],
                        "rank_ic": factor["rank_ic"],
                        "factor_formulation": factor["factor_formulation"],
                        "data_source": factor["data_source"],
                        "date_range": factor["date_range"],
                    },
                )
                if inserted_factor.rowcount != 1:
                    existing_factor = (
                        (
                            await session.execute(
                                text(
                                    "SELECT tenant_id,user_id,factor_name,factor_code,metadata_json "
                                    "FROM rd_agent_factors WHERE factor_id=:factor_id "
                                    "AND tenant_id=:tenant_id AND user_id=:user_id"
                                ),
                                {
                                    "factor_id": factor["factor_id"],
                                    **owner.sql_params(),
                                },
                            )
                        )
                        .mappings()
                        .first()
                    )
                    if (
                        existing_factor is None
                        or existing_factor["tenant_id"] != owner.tenant_id
                        or existing_factor["user_id"] != owner.user_id
                        or existing_factor["factor_name"] != factor["factor_name"]
                        or existing_factor["factor_code"] != factor["factor_code"]
                        or existing_factor["metadata_json"] != factor["metadata"]
                    ):
                        raise ValueError(
                            "Statistical factor ID belongs to another record"
                        )

            inserted = await session.execute(
                text(
                    "INSERT INTO alpha_mining_task_results "
                    "(tenant_id,user_id,task_id,execution_fingerprint,result_json) "
                    "VALUES (:tenant_id,:user_id,:task,:fingerprint,CAST(:result AS jsonb)) "
                    "ON CONFLICT (tenant_id,user_id,task_id) DO NOTHING"
                ),
                {
                    **owner.sql_params(),
                    "task": task_id,
                    "fingerprint": execution_fingerprint,
                    "result": encoded,
                },
            )
            if inserted.rowcount != 1:
                existing = (
                    (
                        await session.execute(
                            text(
                                "SELECT execution_fingerprint,result_json "
                                "FROM alpha_mining_task_results WHERE task_id=:task "
                                "AND tenant_id=:tenant_id AND user_id=:user_id"
                            ),
                            {"task": task_id, **owner.sql_params()},
                        )
                    )
                    .mappings()
                    .first()
                )
                if (
                    existing is None
                    or existing["execution_fingerprint"] != execution_fingerprint
                    or existing["result_json"] != result
                ):
                    raise RuntimeError("A different immutable result already exists")
            from backend.services.engine.alpha_agent.statistical.variant_ledger import (
                record_result as record_variant_result,
            )

            await record_variant_result(
                session, owner=owner, task_id=task_id, result=result
            )
            updated = await session.execute(
                text(
                    "UPDATE alpha_mining_tasks SET status='completed', "
                    "phase='completed', progress='因子挖掘完成', progress_pct=100, "
                    "completed_at=COALESCE(completed_at,now()), updated_at=now() "
                    "WHERE task_id=:task AND tenant_id=:tenant_id AND user_id=:user_id "
                    "AND status='running' AND execution_fingerprint=:fingerprint"
                ),
                {
                    **owner.sql_params(),
                    "task": task_id,
                    "fingerprint": execution_fingerprint,
                },
            )
            if updated.rowcount != 1:
                raise RuntimeError("Task left running state during result completion")
            return True

    async def get_result(
        self, task_id: str, *, tenant_id: str, user_id: str
    ) -> dict[str, Any] | None:
        """Read a result only in the complete authenticated owner scope."""
        owner = OwnerScope(tenant_id, user_id)
        async with get_session(read_only=True) as session:
            row = await session.execute(
                text(
                    "SELECT result_json FROM alpha_mining_task_results "
                    "WHERE task_id=:task AND tenant_id=:tenant_id AND user_id=:user_id"
                ),
                {"task": task_id, **owner.sql_params()},
            )
            value = row.scalar_one_or_none()
        if isinstance(value, str):
            value = json.loads(value)
        return dict(value) if isinstance(value, dict) else None

    async def history_page(
        self,
        *,
        tenant_id: str,
        user_id: str,
        limit: int = 50,
        offset: int = 0,
        status: str | None = None,
        include_archived: bool = False,
    ) -> dict[str, Any]:
        """One PG snapshot for owned history, total and paginated factor counts.

        Explicit projection excludes internal executor state and filesystem paths.
        Old rows retain unknown snapshots/completion times instead of inventing them.
        """
        owner = OwnerScope(tenant_id, user_id)
        if not 1 <= limit <= 200 or not 0 <= offset <= 1000000:
            raise ValueError("Invalid task history pagination")
        states = {
            "pending",
            "queued",
            "running",
            "stopping",
            "completed",
            "failed",
            "cancelled",
        }
        if status is not None and status not in states:
            raise ValueError("Unknown mining task status")
        where = "t.tenant_id=:tenant_id AND t.user_id=:user_id"
        params: dict[str, Any] = {
            **owner.sql_params(),
            "limit": limit,
            "offset": offset,
        }
        if status is not None:
            where += " AND t.status=:status"
            params["status"] = status
        if not include_archived:
            where += " AND t.archived_at IS NULL"
        async with get_session(read_only=True) as session:
            result = await session.execute(
                text(f"""
                WITH matching AS MATERIALIZED (
                    SELECT t.task_id,t.market,t.universe,t.loop_n,t.direction,t.data_source,
                           t.source_doc_id,
                           t.status,t.progress,t.phase,t.progress_pct,t.created_at,t.updated_at,
                           t.completed_at,t.archived_at,t.error_message AS error,
                           COALESCE(s.algorithm,'rdagent') AS algorithm,
                           s.algorithm_version,s.params_json AS params_snapshot,
                           s.request_json->>'batch_id' AS batch_id,
                           s.request_json->'batch_index' AS batch_index,
                           s.request_json->'batch_size' AS batch_size,
                           CASE WHEN jsonb_typeof(s.params_json->'seed_factor_ids')='array'
                                THEN s.params_json->'seed_factor_ids' ELSE '[]'::jsonb END AS seed_factor_ids,
                           s.data_snapshot,s.schema_version
                    FROM alpha_mining_tasks t LEFT JOIN alpha_mining_task_specs s
                      ON s.task_id=t.task_id AND s.tenant_id=t.tenant_id AND s.user_id=t.user_id
                    WHERE {where}
                ), page AS (
                    SELECT * FROM matching ORDER BY created_at DESC,task_id DESC
                    LIMIT :limit OFFSET :offset
                ), decorated AS (
                    SELECT page.*, (SELECT count(*) FROM rd_agent_factors f
                        WHERE f.tenant_id=:tenant_id AND f.user_id=:user_id
                          AND f.metadata_json->>'task_id'=page.task_id) AS factor_count
                    FROM page
                )
                SELECT (SELECT count(*) FROM matching) AS total,
                       COALESCE((SELECT jsonb_agg(to_jsonb(d) ORDER BY d.created_at DESC,d.task_id DESC)
                                 FROM decorated d),'[]'::jsonb) AS tasks
            """),
                params,
            )
            row = result.mappings().one()
        tasks = row["tasks"]
        if isinstance(tasks, str):
            tasks = json.loads(tasks)
        for task in tasks:
            for field in ("created_at", "updated_at", "completed_at", "archived_at"):
                value = task.get(field)
                task[field] = (
                    to_utc_iso(_parse_pg_json_ts(value))
                    if value
                    else None
                )
        return {
            "tasks": tasks,
            "total": int(row["total"]),
            "limit": limit,
            "offset": offset,
        }

    async def archive_terminal(
        self, task_id: str, *, tenant_id: str, user_id: str
    ) -> bool:
        """Hide one owned terminal task from history while preserving its evidence."""
        if not isinstance(task_id, str) or not task_id.strip():
            raise ValueError("A task ID is required")
        owner = OwnerScope(tenant_id, user_id)
        async with get_session() as session:
            updated = await session.execute(
                text(
                    "UPDATE alpha_mining_tasks SET archived_at=now(), updated_at=now() "
                    "WHERE task_id=:task_id AND tenant_id=:tenant_id AND user_id=:user_id "
                    "AND status IN ('completed','failed','cancelled') AND archived_at IS NULL "
                    "RETURNING task_id"
                ),
                {"task_id": task_id, **owner.sql_params()},
            )
            if updated.first() is not None:
                return True
            existing = (
                (
                    await session.execute(
                        text(
                            "SELECT status,archived_at FROM alpha_mining_tasks "
                            "WHERE task_id=:task_id AND tenant_id=:tenant_id AND user_id=:user_id"
                        ),
                        {"task_id": task_id, **owner.sql_params()},
                    )
                )
                .mappings()
                .first()
            )
            if existing is None:
                return False
            if existing["archived_at"] is not None:
                return True
            if existing["status"] not in {"completed", "failed", "cancelled"}:
                raise ValueError("Only terminal mining tasks can be archived")
            return False

    async def restore_terminal(
        self, task_id: str, *, tenant_id: str, user_id: str
    ) -> bool:
        """Restore an owned archived terminal task to visible history."""
        if not isinstance(task_id, str) or not task_id.strip():
            raise ValueError("A task ID is required")
        owner = OwnerScope(tenant_id, user_id)
        async with get_session() as session:
            updated = await session.execute(
                text(
                    "UPDATE alpha_mining_tasks SET archived_at=NULL, updated_at=now() "
                    "WHERE task_id=:task_id AND tenant_id=:tenant_id AND user_id=:user_id "
                    "AND status IN ('completed','failed','cancelled') AND archived_at IS NOT NULL "
                    "RETURNING task_id"
                ),
                {"task_id": task_id, **owner.sql_params()},
            )
            if updated.first() is not None:
                return True
            existing = (
                (
                    await session.execute(
                        text(
                            "SELECT status,archived_at FROM alpha_mining_tasks "
                            "WHERE task_id=:task_id AND tenant_id=:tenant_id AND user_id=:user_id"
                        ),
                        {"task_id": task_id, **owner.sql_params()},
                    )
                )
                .mappings()
                .first()
            )
            if existing is None:
                return False
            if existing["status"] not in {"completed", "failed", "cancelled"}:
                raise ValueError("Only terminal mining tasks can be restored")
            return True

    async def touch_sandbox_heartbeat(
        self, task_id: str, *, tenant_id: str, user_id: str
    ) -> bool:
        """Record that the sandbox execution body is still alive.

        只写 ``executor_json.progress.sandbox_heartbeat_at``：不动
        ``progress_updated_at``（它代表阶段切换，被改会让进度语义失真），也不动
        ``updated_at``（``stopping`` 宽限期按它计算）。停滞对账器把两者一起取
        最大值，长实验因此不会被误杀。
        """
        if not tenant_id or not user_id:
            raise ValueError("Both task owner dimensions are required")
        owner = OwnerScope(tenant_id, user_id)
        async with get_session() as session:
            result = await session.execute(
                text(
                    "UPDATE alpha_mining_tasks SET executor_json = "
                    "COALESCE(executor_json,'{}'::jsonb) || jsonb_build_object("
                    "'progress', COALESCE(executor_json->'progress','{}'::jsonb) || "
                    "jsonb_build_object('sandbox_heartbeat_at', "
                    "to_char(now() AT TIME ZONE 'utc', "
                    "'YYYY-MM-DD\"T\"HH24:MI:SS.MS\"Z\"'))) "
                    "WHERE task_id=:task_id AND tenant_id=:tenant_id "
                    "AND user_id=:user_id AND status='running'"
                ),
                {"task_id": task_id, **owner.sql_params()},
            )
            return result.rowcount == 1

    async def update_progress(self, task_id, *, tenant_id, user_id, snapshot):
        if not tenant_id or not user_id:
            raise ValueError("Both task owner dimensions are required")
        OwnerScope(tenant_id, user_id)
        async with get_session() as session:
            result = await session.execute(
                text(
                    "UPDATE alpha_mining_tasks SET phase=:phase, progress=:progress, "
                    "progress_pct=:percent, executor_json=COALESCE(executor_json,'{}'::jsonb) "
                    "|| jsonb_build_object('progress',CAST(:snapshot AS jsonb)), updated_at=NOW() "
                    "WHERE task_id=:task AND tenant_id=:tenant AND user_id=:user "
                    "AND status='running' AND COALESCE(progress_pct,0)<=:percent"
                ),
                {
                    "task": task_id,
                    "tenant": tenant_id,
                    "user": user_id,
                    "phase": snapshot["phase"],
                    "progress": snapshot["progress"],
                    "percent": snapshot["progress_pct"],
                    "snapshot": json.dumps(snapshot),
                },
            )
            return result.rowcount == 1

    async def update(
        self,
        task_id: str,
        *,
        tenant_id: str,
        user_id: str,
        expected_status: tuple[str, ...] | None = None,
        **fields: Any,
    ) -> bool:
        if not fields:
            return False
        if not tenant_id or not user_id:
            raise ValueError("Both task owner dimensions are required")
        owner = OwnerScope(tenant_id, user_id)
        if fields.keys() - _TASK_UPDATE_FIELDS:
            raise ValueError("Unsupported task update fields")
        if "status" in fields and fields["status"] not in {
            "pending",
            "queued",
            "running",
            "stopping",
            "completed",
            "failed",
            "cancelled",
        }:
            raise ValueError("Invalid task status")
        allowed_statuses = {"pending", "queued", "running", "stopping"}
        statuses = expected_status or ("pending", "queued", "running")
        if not statuses or any(status not in allowed_statuses for status in statuses):
            raise ValueError("Invalid expected task state")
        state_params = {
            f"expected_status_{i}": state for i, state in enumerate(statuses)
        }
        state_clause = ", ".join(f":expected_status_{i}" for i in range(len(statuses)))
        set_clause = ", ".join(f"{k} = :{k}" for k in fields) + ", updated_at = now()"
        if fields.get("status") in {"completed", "failed", "cancelled"}:
            set_clause += ", completed_at = now()"
        async with get_session() as session:
            result = await session.execute(
                text(
                    f"UPDATE alpha_mining_tasks SET {set_clause} "
                    "WHERE task_id = :task_id AND tenant_id = :tenant_id "
                    f"AND user_id = :user_id AND status IN ({state_clause})"
                ),
                {
                    **fields,
                    "task_id": task_id,
                    **owner.sql_params(),
                    **state_params,
                },
            )
            return result.rowcount == 1

    async def list(
        self,
        tenant_id: str,
        user_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        owner = OwnerScope(tenant_id, user_id)
        conditions = ["tenant_id = :tenant_id"]
        params: dict[str, Any] = {**owner.sql_params(), "limit": limit}
        conditions.append("user_id = :user_id")
        if status:
            conditions.append("status = :status")
            params["status"] = status
        async with get_session(read_only=True) as session:
            rows = await session.execute(
                text(
                    f"SELECT * FROM alpha_mining_tasks WHERE {' AND '.join(conditions)}"
                    " ORDER BY created_at DESC LIMIT :limit"
                ),
                params,
            )
            return [dict(r) for r in rows.mappings().all()]

    async def count_running(self, tenant_id: str | None = None) -> dict[str, Any]:
        conditions = ["status IN ('pending','queued','running','stopping')"]
        params: dict[str, Any] = {}
        if tenant_id:
            conditions.append("tenant_id = :tenant_id")
            params["tenant_id"] = tenant_id
        async with get_session(read_only=True) as session:
            rows = await session.execute(
                text(
                    f"SELECT tenant_id, user_id, COUNT(*) AS n FROM alpha_mining_tasks"
                    f" WHERE {' AND '.join(conditions)} GROUP BY tenant_id, user_id"
                ),
                params,
            )
            by_owner: list[dict[str, Any]] = []
            total = 0
            for r in rows.mappings().all():
                total += int(r["n"])
                by_owner.append(
                    {
                        "tenant_id": str(r["tenant_id"]),
                        "user_id": str(r["user_id"]),
                        "count": int(r["n"]),
                    }
                )
            return {"global": total, "by_owner": by_owner}

    async def request_cancel(
        self, task_id: str, *, tenant_id: str, user_id: str
    ) -> str | None:
        """Atomically cancel queued work or request stop for a running task.

        ``stopping`` remains active for quota accounting until the worker confirms
        that its detached RD-Agent process has exited.
        """
        if not tenant_id or not user_id:
            raise ValueError("Both task owner dimensions are required")
        async with get_session() as session:
            await session.execute(
                text(
                    "SELECT pool FROM resource_lease_pools "
                    "WHERE pool='alpha_mining' FOR UPDATE"
                )
            )
            result = await session.execute(
                text(
                    "UPDATE alpha_mining_tasks SET "
                    "status = CASE WHEN status IN ('pending','queued') "
                    "THEN 'cancelled' WHEN status='running' THEN 'stopping' "
                    "ELSE status END, "
                    "error_message = 'Cancelled by user', "
                    "completed_at = CASE WHEN status IN ('pending','queued') "
                    "THEN COALESCE(completed_at,now()) ELSE completed_at END, "
                    "updated_at = CASE WHEN status IN ('stopping','cancelled') "
                    "THEN updated_at ELSE now() END "
                    "WHERE task_id = :task_id AND tenant_id = :tenant_id "
                    "AND user_id = :user_id "
                    "AND status IN ('pending','queued','running','stopping','cancelled') "
                    "RETURNING status"
                ),
                {"task_id": task_id, "tenant_id": tenant_id, "user_id": user_id},
            )
            row = result.mappings().first()
            if row:
                await session.execute(
                    text(
                        "UPDATE resource_task_leases SET "
                        "state=CASE WHEN state='queued' THEN 'cancelled' ELSE state END, "
                        "executor=executor || jsonb_build_object('stop_requested', true), updated_at=now() "
                        "WHERE pool='alpha_mining' AND task_id=:task_id "
                        "AND tenant_id=:tenant_id AND user_id=:user_id "
                        "AND state IN ('queued','reserved','running','suspect')"
                    ),
                    {"task_id": task_id, "tenant_id": tenant_id, "user_id": user_id},
                )
            return str(row["status"]) if row else None

    async def queue_position(
        self, task_id: str, *, tenant_id: str, user_id: str
    ) -> int | None:
        """Return only this owner's position; other owners' identities stay private."""
        if not tenant_id or not user_id:
            raise ValueError("Both task owner dimensions are required")
        async with get_session(read_only=True) as session:
            result = await session.execute(
                text(
                    "SELECT (SELECT count(*) FROM resource_task_leases AS waiting "
                    "WHERE waiting.pool=current_task.pool AND waiting.state='queued' "
                    "AND (waiting.created_at,waiting.task_id) <= "
                    "(current_task.created_at,current_task.task_id)) AS position "
                    "FROM resource_task_leases AS current_task "
                    "WHERE current_task.pool='alpha_mining' AND current_task.task_id=:task_id "
                    "AND current_task.tenant_id=:tenant_id AND current_task.user_id=:user_id "
                    "AND current_task.state='queued'"
                ),
                {"task_id": task_id, "tenant_id": tenant_id, "user_id": user_id},
            )
            value = result.scalar_one_or_none()
            return int(value) if value is not None else None

    async def set_process_group(
        self, task_id: str, *, tenant_id: str, user_id: str, pgid: int, leader: dict
    ) -> bool:
        if not tenant_id or not user_id or int(pgid) <= 0:
            raise ValueError("Owned task and positive process group are required")
        async with get_session() as session:
            result = await session.execute(
                text(
                    "UPDATE alpha_mining_tasks SET executor_json = executor_json || "
                    # pgid 必须显式 CAST：asyncpg 对 jsonb_build_object 的裸参数
                    # 推不出类型，报 IndeterminateDatatypeError: could not
                    # determine data type of parameter $1（2026-10-12 实测）。
                    "jsonb_build_object('process_group', CAST(:pgid AS integer), "
                    "'leader', CAST(:leader AS jsonb)) "
                    "WHERE task_id = :task_id AND tenant_id = :tenant_id "
                    "AND user_id = :user_id AND status IN ('running','stopping')"
                ),
                {
                    "task_id": task_id,
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                    "pgid": int(pgid),
                    "leader": json.dumps(leader),
                },
            )
            return result.rowcount == 1

    async def list_stopping(self, limit: int = 100) -> builtins.list[dict[str, Any]]:
        async with get_session(read_only=True) as session:
            rows = await session.execute(
                text(
                    "SELECT task_id,tenant_id,user_id,executor_json,updated_at "
                    "FROM alpha_mining_tasks WHERE status='stopping' "
                    "ORDER BY updated_at LIMIT :limit"
                ),
                {"limit": max(1, min(int(limit), 500))},
            )
            return [dict(row) for row in rows.mappings().all()]

    async def confirm_cancelled(
        self, task_id: str, *, tenant_id: str, user_id: str
    ) -> bool:
        if not tenant_id or not user_id:
            raise ValueError("Both task owner dimensions are required")
        async with get_session() as session:
            result = await session.execute(
                text(
                    "UPDATE alpha_mining_tasks SET status='cancelled', updated_at=now(), "
                    "completed_at=COALESCE(completed_at,now()) "
                    "WHERE task_id=:task_id AND tenant_id=:tenant_id AND user_id=:user_id "
                    "AND status='stopping'"
                ),
                {"task_id": task_id, "tenant_id": tenant_id, "user_id": user_id},
            )
            return result.rowcount == 1
