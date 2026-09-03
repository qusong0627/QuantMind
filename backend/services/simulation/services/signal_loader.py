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


def _market_where(market: str | None) -> str:
    """engine_signal_scores 市场过滤条件（universe_tag；历史老行 NULL 视为 CN）。

    market 缺省（None）返回空串 = 不加条件（旧行为逐字节不变）。
    """
    if not market:
        return ""
    m = str(market).upper()
    if m in ("A", "CN"):
        return " AND (universe_tag IS NULL OR universe_tag = 'CN')"
    # 非 CN（HK）：当日新行看 universe_tag；HK 历史老行（无 tag）按模型桶兜底
    return " AND (universe_tag = 'HK' OR feature_version LIKE 'script_v1_mdl_hk_%')"


def _to_market_symbol(symbol: str, market: str | None) -> str:
    """港股信号 symbol 归一为 0001.HK 后缀（下游行情/规则/众数推断自洽）；其余原样。"""
    if not market or str(market).upper() != "HK":
        return symbol
    from backend.shared.stock_utils import StockCodeUtil

    return StockCodeUtil.to_hk_suffix(symbol)


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
        market: str | None = None,
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
        mkt_clause = _market_where(market)

        if run_id:
            query = text(f"""
                SELECT symbol, fusion_score, trade_date, run_id, tenant_id, user_id
                FROM engine_signal_scores
                WHERE tenant_id = :tenant_id
                  AND user_id = :user_id
                  AND run_id = :run_id
                  AND fusion_score >= :min_score
                  {mkt_clause}
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
            query = text(f"""
                SELECT symbol, fusion_score, trade_date, run_id, tenant_id, user_id
                FROM engine_signal_scores
                WHERE tenant_id = :tenant_id
                  AND user_id = :user_id
                  AND trade_date = (
                      SELECT MAX(trade_date) FROM engine_signal_scores
                      WHERE tenant_id = :tenant_id AND user_id = :user_id{mkt_clause}
                  )
                  AND fusion_score >= :min_score
                  {mkt_clause}
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
                    symbol=_to_market_symbol(str(row[0]).upper(), market),
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
            if signals or run_id:
                return signals
            # 非 CN 市场无信号时不允许回退 CN pred.parquet（A 股兜底源）
            if market and str(market).upper() not in ("A", "CN"):
                return signals
            # 取最新批次路径且信号表为空（如被后续补推覆盖清空）：回退默认模型
            # pred.parquet 数据日截面，与手动任务信号加载共用同一兜底源。
            return await self._load_pred_parquet_fallback(tenant, uid)
        except Exception as e:
            logger.error("SignalLoader: 加载信号失败 %s", e, exc_info=True)
            return []

    async def _load_pred_parquet_fallback(
        self, tenant_id: str, user_id: str
    ) -> list[SignalScore]:
        """信号表为空时从默认模型 pred.parquet 回退最新截面。

        trade_date 用生效日（T+1，与信号表口径一致）；无生效日时用数据日。
        任何解析失败都静默返回空列表，不影响主路径。
        """
        try:
            from datetime import date as _date

            from backend.services.live_trading.services.manual_execution_service import (
                manual_execution_service,
            )

            hosted = await manual_execution_service.get_default_model_hosted_status(
                tenant_id=tenant_id, user_id=user_id
            )
            model_id = str(hosted.get("latest_default_model_id") or "").strip()
            data_trade_date = str(hosted.get("data_trade_date") or "").strip()[:10]
            if not model_id or not data_trade_date:
                return []
            effective_raw = (
                hosted.get("prediction_trade_date") or data_trade_date
            )
            effective_date = _date.fromisoformat(str(effective_raw)[:10])
            fallback_run_id = f"pred_parquet_{model_id}"

            rows = await manual_execution_service.load_pred_parquet_signal_rows(
                tenant_id=tenant_id,
                user_id=user_id,
                model_id=model_id,
                data_trade_date=data_trade_date,
            )
            signals = [
                SignalScore(
                    symbol=str(row.get("symbol") or "").upper(),
                    score=float(row.get("fusion_score") or 0.0),
                    trade_date=effective_date,
                    run_id=fallback_run_id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                )
                for row in rows
                if row.get("symbol")
            ]
            if signals:
                logger.info(
                    "SignalLoader: 信号表为空，从 pred.parquet 回退 %d 条截面, "
                    "tenant=%s user=%s date=%s",
                    len(signals),
                    tenant_id,
                    user_id,
                    effective_date,
                )
            return signals
        except Exception as e:
            logger.warning("SignalLoader: pred.parquet 回退失败 %s", e)
            return []

    async def load_signals_for_date(
        self,
        db: AsyncSession,
        tenant_id: str,
        user_id: str,
        trade_date: date,
        run_id: str | None = None,
        min_score: float = 0.0,
        limit: int | None = None,
        market: str | None = None,
    ) -> list[SignalScore]:
        """加载**指定交易日**的信号（时光回放用）。

        与 load_latest_signals 的区别是不取 MAX(trade_date) 而是精确匹配。
        注意 engine_signal_scores.trade_date 存的是「信号生效日」(T+1)，
        所以这里传的 trade_date 就是要交易的那一天，不需要再做偏移；
        偏移只发生在生成信号时（用 prev_session(D) 作为推理数据日）。
        """
        tenant = (tenant_id or "").strip() or "default"
        uid = str(user_id or "").strip()

        conditions = [
            "tenant_id = :tenant_id",
            "user_id = :user_id",
            "trade_date = :trade_date",
            "fusion_score >= :min_score",
        ]
        mkt_clause = _market_where(market)
        if mkt_clause:
            conditions.append(mkt_clause[5:])  # 去掉前导 " AND "
        params: dict[str, Any] = {
            "tenant_id": tenant,
            "user_id": uid,
            "trade_date": trade_date,
            "min_score": min_score,
            "limit": limit or 1000,
        }
        if run_id:
            conditions.append("run_id = :run_id")
            params["run_id"] = run_id

        query = text(f"""
            SELECT symbol, fusion_score, trade_date, run_id, tenant_id, user_id
            FROM engine_signal_scores
            WHERE {" AND ".join(conditions)}
            ORDER BY fusion_score DESC
            LIMIT :limit
        """)

        try:
            rows = (await db.execute(query, params)).fetchall()
            signals = [
                SignalScore(
                    symbol=_to_market_symbol(str(row[0]).upper(), market),
                    score=float(row[1]),
                    trade_date=row[2],
                    run_id=str(row[3]),
                    tenant_id=str(row[4]),
                    user_id=str(row[5]),
                )
                for row in rows
            ]
            logger.info(
                "SignalLoader: 加载 %s 的信号 %d 条, tenant=%s user=%s run_id=%s",
                trade_date,
                len(signals),
                tenant,
                uid,
                run_id or "any",
            )
            return signals
        except Exception as e:
            logger.error(
                "SignalLoader: 加载指定日期信号失败 date=%s %s", trade_date, e,
                exc_info=True,
            )
            return []

    async def load_latest_run_id(
        self,
        db: AsyncSession,
        tenant_id: str,
        user_id: str,
        market: str | None = None,
    ) -> str | None:
        """
        获取最新的 run_id。
        """
        tenant = (tenant_id or "").strip() or "default"
        uid = str(user_id or "").strip()

        mkt_clause = _market_where(market)
        query = text(f"""
            SELECT run_id
            FROM engine_signal_scores
            WHERE tenant_id = :tenant_id
              AND user_id = :user_id
              {mkt_clause}
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
        market: str | None = None,
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
        mkt_clause = _market_where(market)

        if run_id:
            query = text(f"""
                SELECT symbol, fusion_score
                FROM engine_signal_scores
                WHERE tenant_id = :tenant_id
                  AND user_id = :user_id
                  AND run_id = :run_id
                  AND symbol = ANY(:symbols)
                  {mkt_clause}
            """)
            params = {
                "tenant_id": tenant,
                "user_id": uid,
                "run_id": run_id,
                "symbols": normalized_symbols,
            }
        else:
            query = text(f"""
                SELECT symbol, fusion_score
                FROM engine_signal_scores
                WHERE tenant_id = :tenant_id
                  AND user_id = :user_id
                  AND trade_date = (
                      SELECT MAX(trade_date) FROM engine_signal_scores
                      WHERE tenant_id = :tenant_id AND user_id = :user_id{mkt_clause}
                  )
                  AND symbol = ANY(:symbols)
                  {mkt_clause}
            """)
            params = {
                "tenant_id": tenant,
                "user_id": uid,
                "symbols": normalized_symbols,
            }

        try:
            result = await db.execute(query, params)
            rows = result.fetchall()
            return {
                _to_market_symbol(str(row[0]).upper(), market): float(row[1])
                for row in rows
            }
        except Exception as e:
            logger.error("SignalLoader: 按股票加载信号失败 %s", e)
            return {}


signal_loader = SignalLoader()
