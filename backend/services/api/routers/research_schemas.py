from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class SymbolsFeaturesRequest(BaseModel):
    symbols: list[str]


class BatchFeaturesRequest(BaseModel):
    """QuantDB 全量特征批量查询请求。

    fields 为可选投影：传入 camelCase 字段名（如 momRet5d）时只返回这些字段，
    用于表格/筛选场景大幅压缩响应体（371 字段 → 按需字段）。
    """

    symbols: list[str]
    fields: list[str] | None = None
    trade_date: str | None = None


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


class SingleStockPredictionRequest(BaseModel):
    symbol: str
    model_id: str | None = None
    date: str | None = None
    horizon: int = 5
    market: str = "CN"
    # 共识矩阵成员（最多4个真实模型）；空=自动取当日全部有分数的模型
    consensus_model_ids: list[str] | None = None
    # 点击“开始预测推理”时由服务端执行已注册模型；查询历史结果时保持 false。
    execute: bool = False
    # 归因返回方式：inline=随核心响应返回（兼容旧调用方）；none=核心图先出，
    # 前端再调 /predict-stock/drivers。树模型 SHAP 较慢，默认仍保持 inline 兼容。
    drivers_mode: str = "inline"


class PredictionDriversRequest(BaseModel):
    """异步归因请求：核心预测完成后单独取 SHAP。"""

    symbol: str
    model_id: str | None = None
    date: str | None = None
    horizon: int = 5
    market: str = "CN"
    # 核心响应已解析出的实际模型/日期/共识成员；传了就跳过重解析，保证
    # 异步归因与图上那份预测是同一个模型、同一天（execute 路径不落库时尤其重要）。
    as_of_date: str | None = None
    model_name: str | None = None
    consensus_model_ids: list[str] | None = None


class FeatureDriverItem(BaseModel):
    name: str
    category: str | None = None
    value: float | None = None
    impact: float
    direction: str  # "positive" | "negative"


class ModelConsensusItem(BaseModel):
    model_id: str
    model_name: str
    model_type: str
    score: float
    rating: str
    horizon: int
    expected_return: float


class ForecastPoint(BaseModel):
    step: int
    date: str
    p10: float
    p50: float
    p90: float
    predicted_price: float
    upper_price: float
    lower_price: float


class SingleStockPredictionResponse(BaseModel):
    status: str
    symbol: str
    stock_name: str
    model_id: str
    model_name: str
    model_type: str
    as_of_date: str
    current_price: float
    horizon: int
    predicted_score: float
    expected_return: float
    confidence: float
    rating: str
    p10_return: float | None = None
    p50_return: float | None = None
    p90_return: float | None = None
    forecast_curve: list[ForecastPoint]
    forecast_warning: str | None = None
    drivers: list[FeatureDriverItem]
    consensus: list[ModelConsensusItem]
    consensus_score: float
    error: str | None = None
