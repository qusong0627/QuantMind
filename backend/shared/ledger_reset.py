"""用户台账重置的市场收窄（唯一实现）：reset / OCR 同步共用。

背景（市场化账户，2026-09-16）：账户/批次/流水表已有市场维度，重置单市场时
不能把其它市场的台账行一并删掉（与快照 T-P1-07 修复同族）。

策略（逐表声明）：
- ``market``：市场列已就绪 → ``COALESCE(market,'CN')=:mkt`` 收窄；
  旧库无列 → CN 重置等价全删（存量数据均为 CN），**非 CN 重置跳过**（无法收窄，宁可不清）。
- ``symbol``：按品种形态正则收窄（与 sim_trades 同口径，路由层传入 clause/params）。
- ``cn_only``：仅 CN 生产的用户级表（日快照）→ 非 CN 重置跳过（避免误删 CN 日常）。

删除按 SAVEPOINT 隔离：单表失败（如旧库缺表/缺列）只跳过该表。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

logger = logging.getLogger(__name__)

MARKET_SCOPED_TABLES = (
    "simulation_accounts",
    "simulation_position_lots",
    "simulation_cash_ledger",
)
SYMBOL_SCOPED_TABLES = ("simulation_fills", "simulation_orders")
CN_ONLY_TABLES = ("simulation_account_daily", "simulation_position_daily")


async def _market_columns_present(session, tables: Iterable[str]) -> set[str]:
    from sqlalchemy import text as sa_text

    names = list(tables)
    try:
        rows = (
            await session.execute(
                sa_text(
                    "SELECT table_name FROM information_schema.columns "
                    "WHERE column_name = 'market' AND table_name = ANY(:t)"
                ),
                {"t": names},
            )
        ).fetchall()
        return {str(r[0]) for r in rows}
    except Exception as exc:  # noqa: BLE001 - 探测失败按无列处理（保守路径）
        logger.warning("[LedgerReset] 市场列探测失败: %s", exc)
        return set()


async def delete_user_ledger_rows(
    session,
    *,
    tenant_id: str,
    user_id_variants: Iterable[str],
    market: str,
    symbol_clause: str | None = None,
    symbol_params: dict[str, Any] | None = None,
) -> dict[str, int]:
    """按市场口径删除用户台账行；返回 {表: 删除行数}（跳过/失败的表不出现在结果里）。"""
    from sqlalchemy import text as sa_text

    market_n = str(market or "CN").upper()
    has_mkt = await _market_columns_present(session, MARKET_SCOPED_TABLES)
    counts: dict[str, int] = {}
    for raw_uid in user_id_variants:
        uid = str(raw_uid)
        for table in MARKET_SCOPED_TABLES:
            clause = ""
            params: dict[str, Any] = {"tid": tenant_id, "uid2": uid}
            if table in has_mkt:
                clause = " AND COALESCE(market,'CN')=:mkt"
                params["mkt"] = market_n
            elif market_n != "CN":
                # 旧库无市场列且非 CN：无法收窄 → 跳过（防误删 CN 台账）
                logger.warning(
                    "[LedgerReset] %s 无市场列，非 CN 重置跳过该表（market=%s）",
                    table,
                    market_n,
                )
                continue
            try:
                async with session.begin_nested():
                    result = await session.execute(
                        sa_text(
                            f"DELETE FROM {table} WHERE tenant_id=:tid AND user_id=:uid2{clause}"
                        ),
                        params,
                    )
                counts[table] = counts.get(table, 0) + int(
                    getattr(result, "rowcount", 0) or 0
                )
            except Exception:  # noqa: BLE001 - 表不存在/列缺失：跳过该表（SAVEPOINT 已回滚）
                continue
        for table in SYMBOL_SCOPED_TABLES:
            clause = symbol_clause or ""
            params = {"tid": tenant_id, "uid2": uid, **(symbol_params or {})}
            try:
                async with session.begin_nested():
                    result = await session.execute(
                        sa_text(
                            f"DELETE FROM {table} WHERE tenant_id=:tid AND user_id=:uid2{clause}"
                        ),
                        params,
                    )
                counts[table] = counts.get(table, 0) + int(
                    getattr(result, "rowcount", 0) or 0
                )
            except Exception:  # noqa: BLE001
                continue
        if market_n == "CN":
            for table in CN_ONLY_TABLES:
                try:
                    async with session.begin_nested():
                        result = await session.execute(
                            sa_text(
                                f"DELETE FROM {table} WHERE tenant_id=:tid AND user_id=:uid2"
                            ),
                            {"tid": tenant_id, "uid2": uid},
                        )
                    counts[table] = counts.get(table, 0) + int(
                        getattr(result, "rowcount", 0) or 0
                    )
                except Exception:  # noqa: BLE001
                    continue
    return counts
