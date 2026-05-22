"""QuantBot 任务存储 — 基于 rd_agent_factors 表的任务管理"""

import json
import logging
from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import text

from backend.shared.database_manager_v2 import get_session

logger = logging.getLogger(__name__)


class QuantBotTaskStore:
    """QuantBot 演化任务存储"""

    async def ensure_tables(self) -> None:
        """确保任务表存在（复用 rd_agent_factors 表，增加 task_type 区分）"""
        stmt = """
        CREATE TABLE IF NOT EXISTS quantbot_tasks (
          task_id TEXT PRIMARY KEY,
          user_id TEXT NOT NULL,
          request_json TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending',
          progress TEXT,
          result_json TEXT,
          error_message TEXT,
          factor_ids TEXT[],
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          completed_at TIMESTAMPTZ
        );
        """
        async with get_session() as session:
            await session.execute(text(stmt))
        logger.info("quantbot_tasks table ensured")

    async def create_task(
        self,
        user_id: str,
        request: dict[str, Any],
    ) -> str:
        """创建新任务，返回 task_id"""
        task_id = uuid4().hex[:16]
        request_json = json.dumps(request, ensure_ascii=False)
        async with get_session() as session:
            await session.execute(
                text("""
                    INSERT INTO quantbot_tasks (task_id, user_id, request_json, status)
                    VALUES (:task_id, :user_id, CAST(:request_json AS jsonb), 'pending')
                    """),
                {
                    "task_id": task_id,
                    "user_id": user_id,
                    "request_json": request_json,
                },
            )
        return task_id

    async def update_status(
        self,
        task_id: str,
        status: str,
        progress: str | None = None,
        result: dict[str, Any] | None = None,
        error_message: str | None = None,
        factor_ids: list[str] | None = None,
    ) -> None:
        """更新任务状态"""
        fields = {"status": status, "updated_at": datetime.now()}
        if progress is not None:
            fields["progress"] = progress
        if result is not None:
            fields["result_json"] = json.dumps(result, ensure_ascii=False)
        if error_message is not None:
            fields["error_message"] = error_message
        if factor_ids is not None:
            fields["factor_ids"] = factor_ids
        if status in ("completed", "failed"):
            fields["completed_at"] = datetime.now()

        async with get_session() as session:
            await session.execute(
                text("""
                    UPDATE quantbot_tasks SET
                      status = :status,
                      updated_at = :updated_at,
                      progress = :progress,
                      result_json = :result_json,
                      error_message = :error_message,
                      factor_ids = :factor_ids,
                      completed_at = :completed_at
                    WHERE task_id = :task_id
                    """),
                {
                    "task_id": task_id,
                    "status": status,
                    "updated_at": fields["updated_at"],
                    "progress": fields.get("progress"),
                    "result_json": fields.get("result_json"),
                    "error_message": fields.get("error_message"),
                    "factor_ids": fields.get("factor_ids"),
                    "completed_at": fields.get("completed_at"),
                },
            )

    async def get_task(self, task_id: str, user_id: str | None = None) -> dict[str, Any] | None:
        """查询单个任务"""
        query = "SELECT * FROM quantbot_tasks WHERE task_id = :task_id"
        params: dict = {"task_id": task_id}
        if user_id:
            query += " AND user_id = :user_id"
            params["user_id"] = user_id

        async with get_session(read_only=True) as session:
            row = await session.execute(text(query), params)
            data = row.mappings().first()

        if not data:
            return None

        result = dict(data)
        # Parse JSON fields
        if isinstance(result.get("request_json"), str):
            try:
                result["request"] = json.loads(result["request_json"])
            except Exception:
                result["request"] = {}
        if isinstance(result.get("result_json"), str):
            try:
                result["result"] = json.loads(result["result_json"])
            except Exception:
                result["result"] = {}
        result.pop("request_json", None)
        result.pop("result_json", None)
        return result

    async def list_tasks(self, user_id: str, limit: int = 20) -> list[dict[str, Any]]:
        """列出用户的所有任务"""
        async with get_session(read_only=True) as session:
            rows = await session.execute(
                text("""
                    SELECT task_id, status, progress, result_json, error_message,
                           factor_ids, created_at, updated_at, completed_at
                    FROM quantbot_tasks
                    WHERE user_id = :user_id
                    ORDER BY created_at DESC
                    LIMIT :limit
                    """),
                {"user_id": user_id, "limit": limit},
            )
            data = rows.mappings().all()

        results = []
        for row in data:
            item = dict(row)
            if isinstance(item.get("result_json"), str):
                try:
                    item["result"] = json.loads(item["result_json"])
                except Exception:
                    item["result"] = {}
            item.pop("result_json", None)
            results.append(item)
        return results
