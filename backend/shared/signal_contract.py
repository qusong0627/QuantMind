"""Signal 契约（T-P1-01）：engine_signal_scores 契约列 + 截面分位计算。

新增列（自愈式迁移，先例 ``rd_agent_persistence`` 的 ALTER IF NOT EXISTS）：
  market    TEXT              — 市场（CN/HK/US/FUTURES/CRYPTO；历史 NULL 视为 CN）
  rank_pct  DOUBLE PRECISION  — **截面分位 0..1**（percent_rank 口径，与 PG percent_rank() 对齐）
  source    TEXT              — batch | realtime（历史 NULL 视为 batch）
  signal_ts TIMESTAMPTZ       — 信号产生时间（批量≈created_at；实时为事件时刻）

命名决策：既有 ``score_rank INTEGER``（名次旧口径，selection/手动任务在消费）保持不变；
分位以 ``rank_pct`` 新增。**策略/风控阈值只允许引用 rank_pct**（主文档铁律三：阈值分位化）。

口径对齐：``compute_rank_pct`` 与回填 SQL 的 ``percent_rank() OVER (PARTITION BY run_id ORDER BY fusion_score)``
同口径——严格小于该值的个数 + 1 作名次（并列取最小名次），pct = (rank-1)/(n-1)，n==1 → 0.0。
"""

from __future__ import annotations

import math
from typing import Any
from collections.abc import Sequence

CONTRACT_COLUMNS = (
    ("market", "TEXT"),
    ("rank_pct", "DOUBLE PRECISION"),
    ("source", "TEXT"),
    ("signal_ts", "TIMESTAMPTZ"),
)

ALTER_TEMPLATE = (
    "ALTER TABLE engine_signal_scores ADD COLUMN IF NOT EXISTS {name} {type}"
)

SOURCE_BATCH = "batch"
SOURCE_REALTIME = "realtime"

_ensured = False


def normalize_market(market: Any) -> str:
    """universe_tag/market 归一：空与 'A' 视为 CN，其余大写。"""
    text = str(market or "").strip().upper()
    if text in ("", "A", "A_SHARE"):
        return "CN"
    return text


def compute_rank_pct(scores: Sequence[float | None]) -> list[float | None]:
    """截面分位（percent_rank 口径）。纯函数。

    - 并列值取**最小名次**（与 PG rank() 一致）；
    - n == 1 → 0.0；
    - 非有限值（None/NaN/±inf）→ 对应位置返回 None（排序时视为最低，仅参与 n 计数会失真，
      调用方应保证分数有效——本函数选择显式返回 None 而非静默补齐）。
    """
    n = len(scores)
    if n == 0:
        return []
    if n == 1:
        val = scores[0]
        return [0.0] if _finite(val) else [None]

    finite_flags = [_finite(s) for s in scores]
    order = sorted(
        (i for i in range(n) if finite_flags[i]),
        key=lambda i: float(scores[i]),
    )
    pcts: list[float | None] = [None] * n
    rank = 0  # 当前名次（并列取最小名次）
    prev_value: float | None = None
    for pos, idx in enumerate(order):
        value = float(scores[idx])
        if prev_value is None or not math.isclose(value, prev_value, rel_tol=0.0, abs_tol=0.0):
            rank = pos + 1
            prev_value = value
        pcts[idx] = (rank - 1) / (n - 1)
    return pcts


def _finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _statements_for(missing: set[str]) -> list[str]:
    return [
        ALTER_TEMPLATE.format(name=name, type=col_type)
        for name, col_type in CONTRACT_COLUMNS
        if name in missing
    ]


_PRECHECK_SQL = (
    "SELECT column_name FROM information_schema.columns "
    "WHERE table_name = 'engine_signal_scores' AND column_name = ANY(:cols)"
)


def _missing_columns_sync(conn) -> set[str]:
    from sqlalchemy import text as sa_text

    names = [name for name, _ in CONTRACT_COLUMNS]
    rows = conn.execute(sa_text(_PRECHECK_SQL), {"cols": names}).fetchall()
    present = {str(r[0]) for r in rows}
    return {n for n in names if n not in present}


async def _missing_columns_async(session) -> set[str]:
    from sqlalchemy import text as sa_text

    names = [name for name, _ in CONTRACT_COLUMNS]
    rows = (await session.execute(sa_text(_PRECHECK_SQL), {"cols": names})).fetchall()
    present = {str(r[0]) for r in rows}
    return {n for n in names if n not in present}


def ensure_signal_contract_columns(conn) -> None:
    """幂等补齐契约列（同步；**安全化**：先查 existence，只对缺列做 DDL）。

    2026-09-16 事故教训（见实施计划 T-P1-01 事故记录）：对热表无条件
    ``ADD COLUMN IF NOT EXISTS`` 仍会申请 AccessExclusive 锁——当调用方自身事务
    尚未提交（持 RowExclusive）且迁移连接用独立会话时，**形成自阻塞**；排队中的
    AccessExclusive 还会堵死该表的所有后续读写。故：
    1. 先查 information_schema（AccessShare，不与任何写冲突），列齐全直接返回（零 DDL）；
    2. 缺列才 ALTER，且 ``lock_timeout=3s`` 快速失败，不长时间钳制热表；
    3. 任何异常只告警不抛出（列缺失会在后续 INSERT 时显式报错），不拖垮业务链路。
    """
    global _ensured
    if _ensured:
        return
    import logging

    from sqlalchemy import text as sa_text

    logger = logging.getLogger(__name__)
    try:
        missing = _missing_columns_sync(conn)
        if not missing:
            _ensured = True
            return
        engine = conn.get_bind()
        with engine.begin() as migration_conn:
            migration_conn.execute(sa_text("SET LOCAL lock_timeout = '3s'"))
            for stmt in _statements_for(missing):
                migration_conn.execute(sa_text(stmt))
        _ensured = True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[SignalContract] 契约列自愈失败（不阻断业务；缺口列将在写入时报错）: %s", exc
        )


async def ensure_signal_contract_columns_async() -> None:
    """幂等补齐契约列（异步；与同步变体同款安全化：先查 existence + lock_timeout + 不抛出）。"""
    global _ensured
    if _ensured:
        return
    import logging

    from sqlalchemy import text as sa_text

    from backend.shared.database_manager_v2 import get_session

    logger = logging.getLogger(__name__)
    try:
        async with get_session(read_only=False) as pre_session:
            missing = await _missing_columns_async(pre_session)
        if not missing:
            _ensured = True
            return
        async with get_session(read_only=False) as migration_session:
            await migration_session.execute(sa_text("SET LOCAL lock_timeout = '3s'"))
            for stmt in _statements_for(missing):
                await migration_session.execute(sa_text(stmt))
            await migration_session.commit()
        _ensured = True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[SignalContract] 契约列自愈失败（不阻断业务；缺口列将在写入时报错）: %s", exc
        )
