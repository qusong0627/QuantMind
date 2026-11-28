"""对外推理排名（MCP v2）：只读、分页、按凭据用户隔离。

`engine_signal_scores` 没有 `updated_at`，所以这里**不做游标增量**，只提供
「某模型某交易日的前 N 名 / 单票名次」这种有界读。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text

from backend.services.api.routers.external.auth import (
    ExternalPrincipal,
    require_external_principal,
)
from backend.services.api.routers.external.permissions import require_permission
from backend.shared.database_manager_v2 import get_session
from backend.shared.mcp_tool_catalog import MCP_INFERENCE_READ
from backend.shared.stock_name_mapper import resolve_name
from backend.shared.stock_utils import StockCodeUtil

router = APIRouter(tags=["External API · Inference"])


class RankingRow(BaseModel):
    rank: int
    symbol: str
    name: str = ""
    score: float | None = None
    rank_pct: float | None = None
    signal_side: str | None = None


class RankingsResponse(BaseModel):
    model_id: str
    trade_date: str
    total: int
    offset: int
    top: int
    rows: list[RankingRow]


class StockScoreResponse(BaseModel):
    model_id: str
    trade_date: str
    symbol: str
    name: str = ""
    score: float | None = None
    rank: int | None = None
    total: int
    rank_pct: float | None = None
    signal_side: str | None = None


async def _latest_run(
    *, tenant_id: str, user_id: str, model_id: str, trade_date: str | None
) -> tuple[str, date] | None:
    async with get_session(read_only=True) as session:
        if trade_date:
            row = (
                await session.execute(
                    text(
                        """
                        SELECT run_id, prediction_trade_date
                        FROM qm_model_inference_runs
                        WHERE tenant_id = :tenant_id AND user_id = :user_id
                          AND model_id = :model_id AND status = 'completed'
                          AND prediction_trade_date = CAST(:trade_date AS date)
                        ORDER BY created_at DESC
                        LIMIT 1
                        """
                    ),
                    {
                        "tenant_id": tenant_id,
                        "user_id": user_id,
                        "model_id": model_id,
                        "trade_date": trade_date,
                    },
                )
            ).first()
        else:
            row = (
                await session.execute(
                    text(
                        """
                        SELECT run_id, prediction_trade_date
                        FROM qm_model_inference_runs
                        WHERE tenant_id = :tenant_id AND user_id = :user_id
                          AND model_id = :model_id AND status = 'completed'
                        ORDER BY prediction_trade_date DESC, created_at DESC
                        LIMIT 1
                        """
                    ),
                    {
                        "tenant_id": tenant_id,
                        "user_id": user_id,
                        "model_id": model_id,
                    },
                )
            ).first()
    if row is None:
        return None
    return str(row[0]), row[1]


def _to_suffix(symbol: Any) -> str:
    return StockCodeUtil.to_suffix(str(symbol or "")) or str(symbol or "")


@router.get("/rankings", response_model=RankingsResponse)
async def get_rankings(
    model_id: str = Query(..., min_length=1, max_length=128),
    trade_date: str | None = Query(
        None, alias="date", description="交易日 YYYY-MM-DD；缺省=该模型最近一次推理"
    ),
    top: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0, le=10_000),
    principal: ExternalPrincipal = Depends(
        require_permission(MCP_INFERENCE_READ)
    ),
) -> RankingsResponse:
    if trade_date:
        try:
            date.fromisoformat(trade_date)
        except ValueError:
            raise HTTPException(status_code=400, detail="date_invalid") from None
    latest = await _latest_run(
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        model_id=model_id,
        trade_date=trade_date,
    )
    if latest is None:
        raise HTTPException(status_code=404, detail="inference_run_not_found")
    run_id, run_date = latest

    async with get_session(read_only=True) as session:
        rows = (
            (
                await session.execute(
                    text(
                        """
                        WITH scores AS (
                            SELECT symbol,
                                   MAX(fusion_score) AS score,
                                   MAX(signal_side) AS signal_side,
                                   MAX(rank_pct) AS rank_pct
                            FROM engine_signal_scores
                            WHERE run_id = :run_id
                              AND fusion_score IS NOT NULL
                            GROUP BY symbol
                        ), ranked AS (
                            SELECT symbol, score, signal_side, rank_pct,
                                   ROW_NUMBER() OVER (ORDER BY score DESC, symbol) AS rn,
                                   COUNT(*) OVER () AS total
                            FROM scores
                        )
                        SELECT symbol, score, signal_side, rank_pct, rn, total
                        FROM ranked
                        ORDER BY rn
                        LIMIT :top OFFSET :offset
                        """
                    ),
                    {"run_id": run_id, "top": top, "offset": offset},
                )
            )
            .mappings()
            .all()
        )
    if not rows:
        return RankingsResponse(
            model_id=model_id,
            trade_date=run_date.isoformat(),
            total=0,
            offset=offset,
            top=top,
            rows=[],
        )
    total = int(rows[0]["total"])
    return RankingsResponse(
        model_id=model_id,
        trade_date=run_date.isoformat(),
        total=total,
        offset=offset,
        top=top,
        rows=[
            RankingRow(
                rank=int(row["rn"]),
                symbol=_to_suffix(row["symbol"]),
                name=resolve_name(_to_suffix(row["symbol"])) or "",
                score=float(row["score"]) if row["score"] is not None else None,
                rank_pct=float(row["rank_pct"]) if row["rank_pct"] is not None else None,
                signal_side=str(row["signal_side"]) if row["signal_side"] else None,
            )
            for row in rows
        ],
    )


@router.get("/stock-score", response_model=StockScoreResponse)
async def get_stock_score(
    model_id: str = Query(..., min_length=1, max_length=128),
    symbol: str = Query(..., min_length=1, max_length=32),
    trade_date: str | None = Query(None, alias="date"),
    principal: ExternalPrincipal = Depends(
        require_permission(MCP_INFERENCE_READ)
    ),
) -> StockScoreResponse:
    if trade_date:
        try:
            date.fromisoformat(trade_date)
        except ValueError:
            raise HTTPException(status_code=400, detail="date_invalid") from None
    latest = await _latest_run(
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        model_id=model_id,
        trade_date=trade_date,
    )
    if latest is None:
        raise HTTPException(status_code=404, detail="inference_run_not_found")
    run_id, run_date = latest
    suffix = StockCodeUtil.to_suffix(symbol) or symbol
    prefix = StockCodeUtil.to_prefix(symbol) or suffix
    async with get_session(read_only=True) as session:
        row = (
            (
                await session.execute(
                    text(
                        """
                        WITH scores AS (
                            SELECT symbol,
                                   MAX(fusion_score) AS score,
                                   MAX(signal_side) AS signal_side,
                                   MAX(rank_pct) AS rank_pct
                            FROM engine_signal_scores
                            WHERE run_id = :run_id
                              AND fusion_score IS NOT NULL
                            GROUP BY symbol
                        ), ranked AS (
                            SELECT symbol, score, signal_side, rank_pct,
                                   ROW_NUMBER() OVER (ORDER BY score DESC, symbol) AS rn,
                                   COUNT(*) OVER () AS total
                            FROM scores
                        )
                        SELECT symbol, score, signal_side, rank_pct, rn, total
                        FROM ranked
                        WHERE symbol = :symbol
                           OR symbol = :bare
                           OR symbol = :prefix
                        LIMIT 1
                        """
                    ),
                    {
                        "run_id": run_id,
                        "symbol": suffix,
                        "bare": suffix.split(".")[0],
                        "prefix": prefix,
                    },
                )
            )
            .mappings()
            .first()
        )
    if row is None:
        return StockScoreResponse(
            model_id=model_id,
            trade_date=run_date.isoformat(),
            symbol=suffix,
            total=0,
        )
    return StockScoreResponse(
        model_id=model_id,
        trade_date=run_date.isoformat(),
        symbol=suffix,
        name=resolve_name(suffix) or "",
        score=float(row["score"]) if row["score"] is not None else None,
        rank=int(row["rn"]),
        total=int(row["total"]),
        rank_pct=float(row["rank_pct"]) if row["rank_pct"] is not None else None,
        signal_side=str(row["signal_side"]) if row["signal_side"] else None,
    )
