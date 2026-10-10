"""T-MV-09 验证报告落盘（专用表 ``rd_agent_factor_validations``）。

为什么另起专表、不写 ``rd_agent_factors.metadata_json``：挖掘收尾窗口里
物化器（后台子进程）正对同一行做 JSONB 读-改-写浅合并
（``update_factor_metrics``，非原子），验证报告再挤进去会丢更新。专表按
``(factor_id, market)`` 一行——语义 = **该因子最新一次验证**；历次尝试的
数据留在各自的 T-FB run 台账（report 里的 ``tfb_run_id`` 可回溯），本表
只保最新结论。

纪律：
- 状态机 ``running → completed/degraded/failed``；``start_validation``
  重置整行（旧报告/旧错误一并清空），``finish_validation`` 只收口仍处
  running 的行（幂等；重复调用返回 False）。
- 单写者前提：验证脚本持全局 flock 串行化，同表不会出现两个并发尝试——
  收口守卫只需 ``status='running'``（无需 attempt token）。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import text

from backend.shared.database_manager_v2 import get_session

logger = logging.getLogger(__name__)

#: 终态词表（running 只能由 start_validation 写；收口不接受）。
VALIDATION_TERMINAL_STATUSES: tuple[str, ...] = ("completed", "degraded", "failed")


async def ensure_table() -> None:
    """建表（幂等；脚本启动时自调用，不依赖 engine 次序）。"""
    async with get_session() as session:
        await session.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS rd_agent_factor_validations (
                  factor_id TEXT NOT NULL,
                  market TEXT NOT NULL,
                  status TEXT NOT NULL DEFAULT 'running'
                    CHECK (status IN ('running', 'completed', 'degraded', 'failed')),
                  tfb_run_id TEXT,
                  report_json JSONB,
                  error TEXT,
                  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                  finished_at TIMESTAMPTZ,
                  PRIMARY KEY (factor_id, market)
                )
                """
            )
        )
        await session.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_rd_agent_factor_validations_status "
                "ON rd_agent_factor_validations(status, created_at DESC)"
            )
        )
    logger.info("rd_agent_factor_validations table ensured")


async def start_validation(factor_id: str, market: str) -> None:
    """登记一次验证尝试（重置为本轮 running；旧报告/旧错误清空）。"""
    async with get_session() as session:
        await session.execute(
            text(
                """
                INSERT INTO rd_agent_factor_validations
                    (factor_id, market, status)
                VALUES (:factor_id, :market, 'running')
                ON CONFLICT (factor_id, market) DO UPDATE
                    SET status = 'running',
                        tfb_run_id = NULL,
                        report_json = NULL,
                        error = NULL,
                        created_at = now(),
                        finished_at = NULL
                """
            ),
            {"factor_id": factor_id, "market": market},
        )


async def finish_validation(
    factor_id: str,
    market: str,
    *,
    status: str,
    report: dict[str, Any] | None = None,
    tfb_run_id: str | None = None,
    error: str | None = None,
) -> bool:
    """按 (factor_id, market) 精确收口（幂等：仅当仍在 running 时生效）。"""
    if status not in VALIDATION_TERMINAL_STATUSES:
        raise ValueError(f"invalid validation terminal status: {status!r}")

    fields: dict[str, Any] = {"status": status}
    params: dict[str, Any] = {"factor_id": factor_id, "market": market}
    if tfb_run_id is not None:
        fields["tfb_run_id"] = tfb_run_id
    if error is not None:
        fields["error"] = error
    set_clause = ", ".join(f"{k} = :{k}" for k in fields)
    params.update(fields)
    if report is not None:
        set_clause += ", report_json = CAST(:report_json AS JSONB)"
        params["report_json"] = json.dumps(report, ensure_ascii=False)

    async with get_session() as session:
        result = await session.execute(
            text(
                f"""
                UPDATE rd_agent_factor_validations
                SET {set_clause}, finished_at = now()
                WHERE factor_id = :factor_id
                  AND market = :market
                  AND status = 'running'
                """
            ),
            params,
        )
        return (result.rowcount or 0) > 0


async def get_validation(factor_id: str, market: str) -> dict[str, Any] | None:
    """取最新验证行（``report_json`` 解包为 ``report``）；无则 None。"""
    async with get_session(read_only=True) as session:
        rows = await session.execute(
            text(
                """
                SELECT factor_id, market, status, tfb_run_id, report_json,
                       error, created_at, finished_at
                FROM rd_agent_factor_validations
                WHERE factor_id = :factor_id AND market = :market
                """
            ),
            {"factor_id": factor_id, "market": market},
        )
        row = rows.mappings().first()
    if row is None:
        return None
    item = dict(row)
    raw = item.pop("report_json", None)
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = None
    item["report"] = raw if isinstance(raw, dict) else None
    return item
