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


def _migration_statements() -> list[str]:
    return [ALTER_TEMPLATE.format(name=name, type=col_type) for name, col_type in CONTRACT_COLUMNS]


def ensure_signal_contract_columns(conn) -> None:
    """幂等补齐契约列（同步；**独立事务**执行，不污染调用方事务）。

    写入端入口调用（script_runner 写库链路用 psycopg2 同步会话）。
    用 ``engine.begin()`` 独立提交：调用方后续回滚不会让"已迁移"标记失真
    （ALTER 已提交、标记才置位；失败则回滚且不置标记，下次重试）。
    """
    global _ensured
    if _ensured:
        return
    from sqlalchemy import text as sa_text

    engine = conn.get_bind()
    with engine.begin() as migration_conn:
        for stmt in _migration_statements():
            migration_conn.execute(sa_text(stmt))
    _ensured = True


async def ensure_signal_contract_columns_async() -> None:
    """幂等补齐契约列（异步；**独立会话**执行，不污染调用方事务）。

    与同步变体共享进程级标记：任一先行执行过即跳过（列是全局的）。
    """
    global _ensured
    if _ensured:
        return
    from sqlalchemy import text as sa_text

    from backend.shared.database_manager_v2 import get_session

    async with get_session(read_only=False) as migration_session:
        for stmt in _migration_statements():
            await migration_session.execute(sa_text(stmt))
        await migration_session.commit()
    _ensured = True
