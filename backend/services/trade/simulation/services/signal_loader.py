"""
Signal Loader - 从 engine_signal_scores 表加载最新 PK 信号
"""

import logging
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


@dataclass
class SignalScore:
    """信号得分数据结构"""
    symbol: str
    score: float
    trade_date: date
    run_id: str
    tenant_id: str
    user_id: str


class SignalLoader:
    """
    从 engine_signal_scores 表加载最新 PK 信号。
    支持按 run_id 指定批次，或取最新 trade_date。
    """

    async def load_latest_signals(
        self,
        db: AsyncSession,
        tenant_id: str,
        user_id: str,
        run_id: str | None = None,
        min_score: float = 0.0,
        limit: int | None = None,
    ) -> list[SignalScore]:
        """
        加载最新信号。

        Args:
            db: 数据库会话
            tenant_id: 租户 ID
            user_id: 用户 ID
            run_id: 指定批次 ID，若 None 则取最新 trade_date
            min_score: 最小得分阈值，低于此值的信号将被过滤
            limit: 返回数量限制

        Returns:
            信号列表，按 score 降序排列
        """
        tenant = (tenant_id or "").strip() or "default"
        uid = str(user_id or "").strip()

        if run_id:
            query = text("""
                SELECT symbol, fusion_score, trade_date, run_id, tenant_id, user_id
                FROM engine_signal_scores
                WHERE tenant_id = :tenant_id
                  AND user_id = :user_id
                  AND run_id = :run_id
                  AND fusion_score >= :min_score
                ORDER BY fusion_score DESC
                LIMIT :limit
            """)
            params = {
                "tenant_id": tenant,
                "user_id": uid,
                "run_id": run_id,
                "min_score": min_score,
                "limit": limit or 1000,
            }
        else:
            query = text("""
                SELECT symbol, fusion_score, trade_date, run_id, tenant_id, user_id
                FROM engine_signal_scores
                WHERE tenant_id = :tenant_id
                  AND user_id = :user_id
                  AND trade_date = (
                      SELECT MAX(trade_date) FROM engine_signal_scores
                      WHERE tenant_id = :tenant_id AND user_id = :user_id
                  )
                  AND fusion_score >= :min_score
                ORDER BY fusion_score DESC
                LIMIT :limit
            """)
            params = {
                "tenant_id": tenant,
                "user_id": uid,
                "min_score": min_score,
                "limit": limit or 1000,
            }

        try:
            result = await db.execute(query, params)
            rows = result.fetchall()
            signals = [
                SignalScore(
                    symbol=str(row[0]).upper(),
                    score=float(row[1]),
                    trade_date=row[2],
                    run_id=str(row[3]),
                    tenant_id=str(row[4]),
                    user_id=str(row[5]),
                )
                for row in rows
            ]
            logger.info(
                "SignalLoader: 加载信号 %d 条, tenant=%s user=%s run_id=%s",
                len(signals),
                tenant,
                uid,
                run_id or "latest",
            )
            return signals
        except Exception as e:
            logger.error("SignalLoader: 加载信号失败 %s", e, exc_info=True)
            return []

    async def load_latest_run_id(
        self,
        db: AsyncSession,
        tenant_id: str,
        user_id: str,
    ) -> str | None:
        """
        获取最新的 run_id。
        """
        tenant = (tenant_id or "").strip() or "default"
        uid = str(user_id or "").strip()

        query = text("""
            SELECT run_id
            FROM engine_signal_scores
            WHERE tenant_id = :tenant_id
              AND user_id = :user_id
            ORDER BY trade_date DESC, created_at DESC
            LIMIT 1
        """)
        try:
            result = await db.execute(query, {"tenant_id": tenant, "user_id": uid})
            row = result.fetchone()
            return str(row[0]) if row else None
        except Exception as e:
            logger.error("SignalLoader: 获取最新 run_id 失败 %s", e)
            return None

    async def load_signals_by_symbols(
        self,
        db: AsyncSession,
        tenant_id: str,
        user_id: str,
        symbols: list[str],
        run_id: str | None = None,
    ) -> dict[str, float]:
        """
        按指定股票代码加载信号得分。

        Returns:
            {symbol: score} 字典
        """
        if not symbols:
            return {}

        tenant = (tenant_id or "").strip() or "default"
        uid = str(user_id or "").strip()
        normalized_symbols = [s.upper() for s in symbols]

        if run_id:
            query = text("""
                SELECT symbol, fusion_score
                FROM engine_signal_scores
                WHERE tenant_id = :tenant_id
                  AND user_id = :user_id
                  AND run_id = :run_id
                  AND symbol = ANY(:symbols)
            """)
            params = {
                "tenant_id": tenant,
                "user_id": uid,
                "run_id": run_id,
                "symbols": normalized_symbols,
            }
        else:
            query = text("""
                SELECT symbol, fusion_score
                FROM engine_signal_scores
                WHERE tenant_id = :tenant_id
                  AND user_id = :user_id
                  AND trade_date = (
                      SELECT MAX(trade_date) FROM engine_signal_scores
                      WHERE tenant_id = :tenant_id AND user_id = :user_id
                  )
                  AND symbol = ANY(:symbols)
            """)
            params = {
                "tenant_id": tenant,
                "user_id": uid,
                "symbols": normalized_symbols,
            }

        try:
            result = await db.execute(query, params)
            rows = result.fetchall()
            return {str(row[0]).upper(): float(row[1]) for row in rows}
        except Exception as e:
            logger.error("SignalLoader: 按股票加载信号失败 %s", e)
            return {}


signal_loader = SignalLoader()
