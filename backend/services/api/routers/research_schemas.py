from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class SymbolsFeaturesRequest(BaseModel):
    symbols: list[str]


class WatchlistAddRequest(BaseModel):
    run_id: str | None = None
    stock_name: str | None = None
    features_snapshot: dict[str, Any] | None = None


class PoolAddRequest(BaseModel):
    run_id: str | None = None
    stock_name: str | None = None
    model_id: str | None = None
    fusion_score: float | None = None
    thesis_summary: str | None = None
    features_snapshot: dict[str, Any] | None = None
