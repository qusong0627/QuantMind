"""回测结果持久化（PostgreSQL）"""

import asyncio
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy import text

from backend.services.engine.qlib_app.schemas.backtest import QlibBacktestResult
from backend.shared.database_manager_v2 import get_session
from backend.shared.utils import normalize_user_id
from backend.services.engine.qlib_app.utils.structured_logger import StructuredTaskLogger

logger = logging.getLogger(__name__)
task_logger = StructuredTaskLogger(logger, "BacktestPersistence")

try:
    from backend.shared.cos_service import get_cos_service
except Exception:
    get_cos_service = None


class BacktestPersistence:
    """PostgreSQL 持久化"""

    HISTORY_RETENTION_LIMIT = 10
    LARGE_RESULT_FIELDS = (
        "equity_curve",
        "drawdown_curve",
        "trades",
        "positions",
        "factor_metrics",
        "stratified_returns",
        "style_attribution",
        "rebalance_suggestions",
    )

    def __init__(self) -> None:
        self._local_result_root = self._resolve_local_result_root()
        self._local_result_root.mkdir(parents=True, exist_ok=True)
        self._enable_cos_backup = os.getenv("QLIB_BACKTEST_COS_BACKUP_ENABLED", "false").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
        self._cos_backup_prefix = os.getenv("QLIB_BACKTEST_COS_PREFIX", "backtests/results").strip().strip("/")
        self._cos_service = None
        if self._enable_cos_backup and get_cos_service:
            try:
                self._cos_service = get_cos_service()
                if not self._cos_service.client:
                    task_logger.warning("cos_backup_unavailable", "QLIB_BACKTEST_COS_BACKUP_ENABLED=true，但 COS 不可用，已回退仅本地存储")
            except Exception as exc:
                task_logger.warning("cos_service_init_failed", "初始化 COS 服务失败，已回退仅本地存储", error=str(exc))
                self._cos_service = None
        elif self._enable_cos_backup and not get_cos_service:
            task_logger.warning("cos_dependency_missing", "QLIB_BACKTEST_COS_BACKUP_ENABLED=true，但 COS 依赖不可用，已回退仅本地存储")

    async def ensure_tables(self) -> None:
        statements = [
            """
            CREATE TABLE IF NOT EXISTS qlib_backtest_runs (
              backtest_id TEXT PRIMARY KEY,
              user_id TEXT NOT NULL,
              tenant_id TEXT NOT NULL DEFAULT 'default',
              status TEXT NOT NULL,
              created_at TIMESTAMPTZ NOT NULL,
              completed_at TIMESTAMPTZ,
              task_id TEXT,
              config_json JSONB,
              result_json JSONB,
              result_file_path TEXT,
              result_cos_key TEXT,
              result_cos_url TEXT,
              result_backup_status TEXT NOT NULL DEFAULT 'none',
              result_backup_at TIMESTAMPTZ
            );
            """,
            """
            DO $$
            BEGIN
              IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name='qlib_backtest_runs' AND column_name='tenant_id'
              ) THEN
                ALTER TABLE qlib_backtest_runs ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default';
              END IF;
              IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name='qlib_backtest_runs' AND column_name='task_id'
              ) THEN
                ALTER TABLE qlib_backtest_runs ADD COLUMN task_id TEXT;
              END IF;
              IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name='qlib_backtest_runs' AND column_name='result_file_path'
              ) THEN
                ALTER TABLE qlib_backtest_runs ADD COLUMN result_file_path TEXT;
              END IF;
              IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name='qlib_backtest_runs' AND column_name='result_cos_key'
              ) THEN
                ALTER TABLE qlib_backtest_runs ADD COLUMN result_cos_key TEXT;
              END IF;
              IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name='qlib_backtest_runs' AND column_name='result_cos_url'
              ) THEN
                ALTER TABLE qlib_backtest_runs ADD COLUMN result_cos_url TEXT;
              END IF;
              IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name='qlib_backtest_runs' AND column_name='result_backup_status'
              ) THEN
                ALTER TABLE qlib_backtest_runs ADD COLUMN result_backup_status TEXT NOT NULL DEFAULT 'none';
              END IF;
              IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name='qlib_backtest_runs' AND column_name='result_backup_at'
              ) THEN
                ALTER TABLE qlib_backtest_runs ADD COLUMN result_backup_at TIMESTAMPTZ;
              END IF;
            END $$;
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_qlib_backtest_runs_user_created
              ON qlib_backtest_runs(user_id, created_at DESC);
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_qlib_backtest_runs_status
              ON qlib_backtest_runs(status);
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_qlib_backtest_runs_tenant_created
              ON qlib_backtest_runs(tenant_id, created_at DESC);
            """,
        ]
        async with get_session() as session:
            for statement in statements:
                await session.execute(text(statement))

    async def save_run(
        self,
        backtest_id: str,
        user_id: str,
        tenant_id: str,
        status: str,
        created_at: datetime,
        config: dict[str, Any] | None,
        result: QlibBacktestResult | None,
        completed_at: datetime | None = None,
        task_id: str | None = None,
    ) -> None:
        user_id = normalize_user_id(user_id)
        summary_payload, local_payload = self._split_result_payload(result)
        result_file_path = self._write_local_result(
            backtest_id=backtest_id,
            user_id=user_id,
            tenant_id=tenant_id,
            payload=local_payload,
        )
        has_local_payload = local_payload is not None
        backup_status = "none"
        if has_local_payload:
            backup_status = "pending" if self._can_backup_to_cos() else "local_only"
        config_json = json.dumps(config or {}, ensure_ascii=False)
        summary_json = json.dumps(summary_payload, ensure_ascii=False) if summary_payload is not None else None
        async with get_session() as session:
            await session.execute(
                text("""
                    INSERT INTO qlib_backtest_runs (
                      backtest_id, user_id, tenant_id, status, created_at, completed_at,
                      task_id, config_json, result_json, result_file_path,
                      result_backup_status
                    ) VALUES (
                      :backtest_id, :user_id, :tenant_id, :status, :created_at,
                      :completed_at, :task_id,
                      CAST(:config_json AS jsonb), CAST(:result_json AS jsonb), :result_file_path,
                      :result_backup_status
                    )
                    ON CONFLICT(backtest_id) DO UPDATE SET
                      status = EXCLUDED.status,
                      completed_at = EXCLUDED.completed_at,
                      task_id = EXCLUDED.task_id,
                      tenant_id = EXCLUDED.tenant_id,
                      config_json = EXCLUDED.config_json,
                      result_json = EXCLUDED.result_json,
                      result_file_path = EXCLUDED.result_file_path,
                      result_backup_status = EXCLUDED.result_backup_status
                    """),
                {
                    "backtest_id": backtest_id,
                    "user_id": user_id,
                    "tenant_id": tenant_id,
                    "status": status,
                    "created_at": created_at,
                    "completed_at": completed_at,
                    "config_json": config_json,
                    "result_json": summary_json,
                    "result_file_path": result_file_path,
                    "result_backup_status": backup_status,
                    "task_id": task_id,
                },
            )
            await self._prune_user_history(session, user_id, tenant_id)
        if has_local_payload and backup_status == "pending":
            self._trigger_cos_backup(
                backtest_id=backtest_id,
                user_id=user_id,
                tenant_id=tenant_id,
                result_file_path=result_file_path,
            )

    async def get_result(
        self,
        backtest_id: str,
        tenant_id: str | None = None,
        user_id: str | None = None,
        include_fields: list[str] | None = None,
        exclude_fields: list[str] | None = None,
    ) -> QlibBacktestResult | None:
        params = {"id": backtest_id}
        if tenant_id:
            params["tenant_id"] = tenant_id
        if user_id:
            params["user_id"] = normalize_user_id(user_id)

        # 优化：如果指定了字段，仅查询摘要 JSON，具体的合并在逻辑层按需处理
        async with get_session(read_only=True) as session:
            row = await session.execute(
                text(
                    "SELECT result_json, result_file_path, result_cos_key FROM qlib_backtest_runs "
                    "WHERE backtest_id = :id"
                    + (" AND tenant_id = :tenant_id" if tenant_id else "")
                    + (" AND user_id = :user_id" if user_id else "")
                ),
                params,
            )
            data = row.mappings().first()
        if not data:
            return None

        # 如果指定了某些字段且这些字段都不在大字段列表中，则不需要读取本地/COS文件
        needs_local = True
        if include_fields:
            needs_local = any(f in self.LARGE_RESULT_FIELDS for f in include_fields)
        elif exclude_fields:
            # 如果排除的大量字段包含所有大文件字段，则不需要加载本地
            needs_local = any(f in self.LARGE_RESULT_FIELDS and f not in exclude_fields for f in self.LARGE_RESULT_FIELDS)

        merged_payload = self._merge_summary_with_local(
            summary_payload=data["result_json"],
            result_file_path=data.get("result_file_path") if needs_local else None,
            result_cos_key=data.get("result_cos_key") if needs_local else None,
            backtest_id=backtest_id,
            user_id=params.get("user_id"),
            tenant_id=params.get("tenant_id"),
        )
        if not merged_payload:
            return None

        # 【核心性能优化 1】在 Pydantic 校验前剔除不需要的大型字段 (如成交流水、持仓)
        # 这能显著降低 parse_obj 的 CPU 开销和内存占用
        if exclude_fields:
            for field in exclude_fields:
                merged_payload.pop(field, None)

        # 【核心性能优化 2】仅在明确包含 trades 且确实有数据时才进行极其耗时的归一化
        should_normalize_trades = (include_fields is None or "trades" in include_fields) and (exclude_fields is None or "trades" not in exclude_fields)
        trades = merged_payload.get("trades")
        if should_normalize_trades and isinstance(trades, list) and trades:
            try:
                from backend.services.engine.qlib_app.services.risk_analyzer import RiskAnalyzer

                merged_payload["trades"] = RiskAnalyzer.normalize_trades_for_display(trades)
            except Exception as exc:
                task_logger.warning(
                    "normalize_trade_display_failed",
                    "Failed to normalize trade display fields",
                    backtest_id=backtest_id,
                    error=str(exc),
                )

        # 如果指定了字段，裁剪结果
        if include_fields:
            filtered = {
                k: v
                for k, v in merged_payload.items()
                if k in include_fields or k in ["backtest_id", "status", "created_at", "user_id"]
            }
            return QlibBacktestResult.model_validate(filtered)

        # 使用 model_validate 代替 parse_obj (Pydantic V2 语法，如果是 V1 保持 parse_obj)
        if hasattr(QlibBacktestResult, "model_validate"):
            return QlibBacktestResult.model_validate(merged_payload)
        return QlibBacktestResult.parse_obj(merged_payload)

    async def get_status(
        self,
        backtest_id: str,
        tenant_id: str | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any] | None:
        params = {"id": backtest_id}
        if tenant_id:
            params["tenant_id"] = tenant_id
        if user_id:
            params["user_id"] = normalize_user_id(user_id)
        async with get_session(read_only=True) as session:
            row = await session.execute(
                text(
                    """
                    SELECT backtest_id, status, created_at, completed_at, result_json
                    FROM qlib_backtest_runs WHERE backtest_id = :id
                    """
                    + (" AND tenant_id = :tenant_id" if tenant_id else "")
                    + (" AND user_id = :user_id" if user_id else "")
                    + """
                    """
                ),
                params,
            )
            data = row.mappings().first()
        if not data:
            return None
        status = data["status"]
        result_json = data["result_json"]
        error_message = None
        full_error = None
        if isinstance(result_json, dict):
            error_message = result_json.get("error_message")
            full_error = result_json.get("full_error")

        return {
            "backtest_id": data["backtest_id"],
            "status": status,
            "created_at": data["created_at"],
            "completed_at": data["completed_at"],
            "progress": 1.0 if status == "completed" else 0.0,
            "error_message": error_message,
            "full_error": full_error,
        }

    async def list_history(
        self, user_id: str, tenant_id: str | None = None, limit: int | None = None
    ) -> list[QlibBacktestResult]:
        """历史列表只返回摘要字段，不加载 equity_curve/trades/positions 等大字段"""
        user_id = normalize_user_id(user_id)
        params = {"user_id": user_id}
        if tenant_id:
            params["tenant_id"] = tenant_id
        sql_limit = ""
        if limit is not None and limit > 0:
            params["limit"] = int(limit)
            sql_limit = " LIMIT :limit"
        async with get_session(read_only=True) as session:
            rows = await session.execute(
                text(
                    """
                    SELECT
                        b.backtest_id, b.user_id, b.tenant_id, b.status, b.created_at, b.completed_at, b.task_id,
                        (b.result_json->>'annual_return')::float   AS annual_return,
                        (b.result_json->>'sharpe_ratio')::float    AS sharpe_ratio,
                        (b.result_json->>'max_drawdown')::float    AS max_drawdown,
                        (b.result_json->>'total_return')::float    AS total_return,
                        (b.result_json->>'volatility')::float      AS volatility,
                        (b.result_json->>'total_trades')::int      AS total_trades,
                        (b.result_json->>'win_rate')::float        AS win_rate,
                        (b.result_json->>'profit_factor')::float   AS profit_factor,
                        (b.result_json->>'execution_time')::float  AS execution_time,
                        b.result_json->>'benchmark_symbol'         AS benchmark_symbol,
                        (b.result_json->>'benchmark_return')::float AS benchmark_return,
                        b.result_json->>'error_message'            AS error_message,
                        b.config_json,
                        m.metadata_json->>'display_name'           AS model_name
                    FROM qlib_backtest_runs b
                    LEFT JOIN qm_user_models m
                        ON m.model_id = (b.config_json->>'model_id')
                        AND m.tenant_id = b.tenant_id
                        AND m.user_id = b.user_id
                    WHERE b.user_id = :user_id
                    """ + (" AND b.tenant_id = :tenant_id" if tenant_id else "") + " ORDER BY b.created_at DESC" + sql_limit
                ),
                params,
            )
            data = rows.mappings().all()

        results = []
        for row in data:
            obj = QlibBacktestResult(
                backtest_id=row["backtest_id"],
                user_id=row["user_id"],
                tenant_id=row["tenant_id"] or "default",
                status=row["status"],
                created_at=row["created_at"],
                completed_at=row["completed_at"],
                task_id=row["task_id"],
                annual_return=row["annual_return"],
                sharpe_ratio=row["sharpe_ratio"],
                max_drawdown=row["max_drawdown"],
                total_return=row["total_return"],
                volatility=row["volatility"],
                total_trades=row["total_trades"],
                win_rate=row["win_rate"],
                profit_factor=row["profit_factor"],
                execution_time=row["execution_time"],
                benchmark_symbol=row["benchmark_symbol"],
                benchmark_return=row["benchmark_return"],
                error_message=row["error_message"],
                config=row["config_json"] if isinstance(row["config_json"], dict) else None,
                model_name=row.get("model_name"),
            )
            results.append(obj)
        return results

    async def delete_run(self, backtest_id: str, user_id: str, tenant_id: str) -> bool:
        """删除回测记录（仅限用户自己的记录）"""
        user_id = normalize_user_id(user_id)
        async with get_session() as session:
            row = await session.execute(
                text("""
                    SELECT result_file_path
                    FROM qlib_backtest_runs
                    WHERE backtest_id = :backtest_id AND user_id = :user_id AND tenant_id = :tenant_id
                    """),
                {
                    "backtest_id": backtest_id,
                    "user_id": user_id,
                    "tenant_id": tenant_id,
                },
            )
            result_file_path = row.scalar_one_or_none()
            result = await session.execute(
                text("""
                    DELETE FROM qlib_backtest_runs
                    WHERE backtest_id = :backtest_id AND user_id = :user_id AND tenant_id = :tenant_id
                    """),
                {
                    "backtest_id": backtest_id,
                    "user_id": user_id,
                    "tenant_id": tenant_id,
                },
            )
            if result.rowcount > 0:
                self._remove_local_result_file(result_file_path)
            return result.rowcount > 0

    async def get_multiple_results(
        self,
        backtest_ids: list[str],
        tenant_id: str | None = None,
        user_id: str | None = None,
        include_fields: list[str] | None = None,
    ) -> list[QlibBacktestResult]:
        """批量获取回测结果"""
        if not backtest_ids:
            return []

        params = {"ids": backtest_ids}
        if tenant_id:
            params["tenant_id"] = tenant_id
        if user_id:
            params["user_id"] = normalize_user_id(user_id)
        async with get_session(read_only=True) as session:
            rows = await session.execute(
                text(
                    """
                    SELECT result_json, user_id, result_file_path, result_cos_key, backtest_id, tenant_id FROM qlib_backtest_runs
                    WHERE backtest_id = ANY(:ids) AND result_json IS NOT NULL
                    """
                    + (" AND tenant_id = :tenant_id" if tenant_id else "")
                    + (" AND user_id = :user_id" if user_id else "")
                    + """
                    ORDER BY created_at DESC
                    """
                ),
                params,
            )
            data = rows.all()

        needs_local = True
        if include_fields:
            needs_local = any(f in self.LARGE_RESULT_FIELDS for f in include_fields)
        should_normalize_trades = include_fields is None or "trades" in include_fields

        results = []
        for row in data:
            item = row[0]
            uid = row[1]
            result_file_path = row[2]
            result_cos_key = row[3]
            row_backtest_id = row[4]
            row_tenant_id = row[5]
            payload = self._merge_summary_with_local(
                summary_payload=item,
                result_file_path=result_file_path if needs_local else None,
                result_cos_key=result_cos_key if needs_local else None,
                backtest_id=row_backtest_id,
                user_id=uid,
                tenant_id=row_tenant_id,
            )
            if not isinstance(payload, dict):
                continue
            payload["user_id"] = uid

            # 仅在需要时进行耗时的归一化
            trades = payload.get("trades")
            if should_normalize_trades and isinstance(trades, list) and trades:
                try:
                    from backend.services.engine.qlib_app.services.risk_analyzer import RiskAnalyzer

                    payload["trades"] = RiskAnalyzer.normalize_trades_for_display(trades)
                except Exception:
                    pass

            # 裁剪字段
            if include_fields:
                payload = {
                    k: v
                    for k, v in payload.items()
                    if k in include_fields or k in ["backtest_id", "status", "created_at", "user_id"]
                }

            obj = QlibBacktestResult.parse_obj(payload)
            results.append(obj)
        return results

    async def check_db(self) -> bool:
        """简单数据库健康检查"""
        async with get_session(read_only=True) as session:
            row = await session.execute(text("SELECT 1"))
            return row.scalar_one_or_none() == 1

    def _result_to_payload(self, result: QlibBacktestResult | None) -> dict[str, Any] | None:
        if result is None:
            return None
        return self._normalize(result.dict())

    def _split_result_payload(
        self, result: QlibBacktestResult | None
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        payload = self._result_to_payload(result)
        if not payload:
            return None, None
        summary_payload = dict(payload)
        local_payload: dict[str, Any] = {}
        for field in self.LARGE_RESULT_FIELDS:
            value = summary_payload.pop(field, None)
            if value is not None:
                local_payload[field] = value
        return summary_payload, local_payload or None

    def _normalize(self, value: Any) -> Any:
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, dict):
            return {key: self._normalize(val) for key, val in value.items()}
        if isinstance(value, list):
            return [self._normalize(item) for item in value]
        return value

    def _resolve_local_result_root(self) -> Path:
        configured = os.getenv("QLIB_BACKTEST_RESULT_DIR")
        if configured:
            path = Path(configured)
            if path.is_absolute():
                return path
            return (self._find_project_root() / path).resolve()
        return (self._find_project_root() / "data" / "backtest_results").resolve()

    def _find_project_root(self) -> Path:
        current = Path(__file__).resolve()
        for parent in current.parents:
            if (parent / "requirements.txt").exists() and (parent / "backend").exists():
                return parent
        return Path.cwd()

    def _sanitize_segment(self, segment: str) -> str:
        safe = segment.replace("/", "_").replace("\\", "_").strip()
        return safe or "unknown"

    def _build_local_result_path(self, backtest_id: str, user_id: str, tenant_id: str) -> Path:
        tenant = self._sanitize_segment(tenant_id)
        user = self._sanitize_segment(user_id)
        file_name = f"{self._sanitize_segment(backtest_id)}.json"
        return self._local_result_root / tenant / user / file_name

    def _write_local_result(
        self,
        backtest_id: str,
        user_id: str,
        tenant_id: str,
        payload: dict[str, Any] | None,
    ) -> str | None:
        if payload is None:
            return None
        target = self._build_local_result_path(backtest_id=backtest_id, user_id=user_id, tenant_id=tenant_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        temp_path = target.with_suffix(".json.tmp")
        temp_path.write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )
        temp_path.replace(target)
        return str(target)

    def _read_local_result(self, result_file_path: str | None) -> dict[str, Any] | None:
        if not result_file_path:
            return None
        path = Path(result_file_path)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            return None
        return None

    def _merge_summary_with_local(
        self,
        summary_payload: dict[str, Any] | None,
        result_file_path: str | None,
        result_cos_key: str | None,
        backtest_id: str | None,
        user_id: str | None,
        tenant_id: str | None,
    ) -> dict[str, Any] | None:
        if summary_payload is None and not result_file_path and not result_cos_key:
            return None
        merged: dict[str, Any] = {}
        if isinstance(summary_payload, dict):
            merged.update(summary_payload)
        local_payload = self._read_local_result(result_file_path)
        if not isinstance(local_payload, dict) and result_file_path and result_cos_key:
            local_payload = self._restore_local_from_cos(
                result_file_path=result_file_path,
                result_cos_key=result_cos_key,
                backtest_id=backtest_id,
                user_id=user_id,
                tenant_id=tenant_id,
            )
        if isinstance(local_payload, dict):
            merged.update(local_payload)
        return merged or None

    def _remove_local_result_file(self, result_file_path: str | None) -> None:
        if not result_file_path:
            return
        try:
            path = Path(result_file_path)
            if path.exists():
                path.unlink()
        except Exception:
            return

    def _can_backup_to_cos(self) -> bool:
        return bool(
            self._enable_cos_backup and self._cos_service and self._cos_service.client and self._cos_service.bucket_name
        )

    def _build_cos_key(self, backtest_id: str, user_id: str, tenant_id: str) -> str:
        tenant = self._sanitize_segment(tenant_id)
        user = self._sanitize_segment(user_id)
        backtest = self._sanitize_segment(backtest_id)
        return f"{self._cos_backup_prefix}/{tenant}/{user}/{backtest}.json"

    def _trigger_cos_backup(
        self,
        backtest_id: str,
        user_id: str,
        tenant_id: str,
        result_file_path: str | None,
    ) -> None:
        if not result_file_path or not self._can_backup_to_cos():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            task_logger.warning("cos_backup_no_event_loop", "无可用事件循环，跳过 COS 冷备", backtest_id=backtest_id)
            return
        loop.create_task(
            self._backup_local_result_to_cos(
                backtest_id=backtest_id,
                user_id=user_id,
                tenant_id=tenant_id,
                result_file_path=result_file_path,
            )
        )

    async def _backup_local_result_to_cos(
        self,
        backtest_id: str,
        user_id: str,
        tenant_id: str,
        result_file_path: str,
    ) -> None:
        path = Path(result_file_path)
        if not path.exists():
            await self._update_backup_status(
                backtest_id=backtest_id,
                user_id=user_id,
                tenant_id=tenant_id,
                status="missing_local",
            )
            return
        cos_key = self._build_cos_key(backtest_id, user_id, tenant_id)
        try:
            content = await asyncio.to_thread(path.read_bytes)
            upload_resp = await asyncio.to_thread(
                self._cos_service.upload_file,
                content,
                cos_key,
                "backtests",
                "application/json",
                True,
            )
            if not isinstance(upload_resp, dict) or not upload_resp.get("success"):
                raise RuntimeError(str(upload_resp.get("error") if isinstance(upload_resp, dict) else upload_resp))
            file_url = upload_resp.get("file_url")
            await self._update_backup_status(
                backtest_id=backtest_id,
                user_id=user_id,
                tenant_id=tenant_id,
                status="backed_up",
                cos_key=cos_key,
                cos_url=file_url,
            )
        except Exception as exc:
            task_logger.warning("cos_backup_failed", "回测结果 COS 冷备失败", backtest_id=backtest_id, error=str(exc))
            await self._update_backup_status(
                backtest_id=backtest_id,
                user_id=user_id,
                tenant_id=tenant_id,
                status="failed",
            )

    async def _update_backup_status(
        self,
        backtest_id: str,
        user_id: str,
        tenant_id: str,
        status: str,
        cos_key: str | None = None,
        cos_url: str | None = None,
    ) -> None:
        async with get_session() as session:
            await session.execute(
                text("""
                    UPDATE qlib_backtest_runs
                    SET
                        result_backup_status = :status,
                        result_cos_key = COALESCE(:cos_key, result_cos_key),
                        result_cos_url = COALESCE(:cos_url, result_cos_url),
                        result_backup_at = now()
                    WHERE backtest_id = :backtest_id
                      AND user_id = :user_id
                      AND tenant_id = :tenant_id
                    """),
                {
                    "status": status,
                    "cos_key": cos_key,
                    "cos_url": cos_url,
                    "backtest_id": backtest_id,
                    "user_id": user_id,
                    "tenant_id": tenant_id,
                },
            )

    def _restore_local_from_cos(
        self,
        result_file_path: str,
        result_cos_key: str,
        backtest_id: str | None,
        user_id: str | None,
        tenant_id: str | None,
    ) -> dict[str, Any] | None:
        if not self._can_backup_to_cos():
            return None
        try:
            response = self._cos_service.client.get_object(
                Bucket=self._cos_service.bucket_name,
                Key=result_cos_key,
            )
            content = response["Body"].get_raw_stream().read()
            data = json.loads(content.decode("utf-8"))
            if not isinstance(data, dict):
                return None
            path = Path(result_file_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = path.with_suffix(".json.tmp")
            temp_path.write_bytes(content)
            temp_path.replace(path)
            if backtest_id and user_id and tenant_id:
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(
                        self._update_backup_status(
                            backtest_id=backtest_id,
                            user_id=user_id,
                            tenant_id=tenant_id,
                            status="restored_from_cos",
                        )
                    )
                except RuntimeError:
                    pass
            return data
        except Exception as exc:
            task_logger.warning("cos_restore_failed", "从 COS 回源回测文件失败", key=result_cos_key, error=str(exc))
            return None

    async def _prune_user_history(self, session, user_id: str, tenant_id: str) -> None:
        """
        每个 user_id + tenant_id 仅保留最近 HISTORY_RETENTION_LIMIT 条记录，
        避免回测历史无限增长导致查询和存储压力持续升高。
        """
        rows = await session.execute(
            text("""
                WITH ranked AS (
                    SELECT
                        backtest_id,
                        result_file_path,
                        ROW_NUMBER() OVER (
                            PARTITION BY user_id, tenant_id
                            ORDER BY created_at DESC, backtest_id DESC
                        ) AS rn
                    FROM qlib_backtest_runs
                    WHERE user_id = :user_id AND tenant_id = :tenant_id
                )
                SELECT backtest_id, result_file_path
                FROM ranked
                WHERE rn > :retention_limit
                """),
            {
                "user_id": user_id,
                "tenant_id": tenant_id,
                "retention_limit": self.HISTORY_RETENTION_LIMIT,
            },
        )
        stale_rows = rows.mappings().all()
        if not stale_rows:
            return
        stale_ids = [row["backtest_id"] for row in stale_rows]
        await session.execute(
            text("""
                DELETE FROM qlib_backtest_runs
                WHERE backtest_id = ANY(:backtest_ids)
                """),
            {"backtest_ids": stale_ids},
        )
        for row in stale_rows:
            self._remove_local_result_file(row.get("result_file_path"))
