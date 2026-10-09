from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, Field


class Layer1LgbmConfig(BaseModel):
    enabled: bool = True
    universe: str = "all_a_share"
    rebalance_cycle: str = "1d"
    select_top_n: int = Field(300, ge=1, le=5000)
    output_top_n: int = Field(50, ge=1, le=500)
    score_field: str = "lgbm_score"
    weight_field: str = "weight"


class Layer2TftConfig(BaseModel):
    enabled: bool = True
    horizon_days: int = Field(5, ge=1, le=30)
    lookback_days: int = Field(30, ge=5, le=365)
    prefilter_top_n: int = Field(100, ge=1, le=1000)
    final_top_n: int = Field(50, ge=1, le=500)
    score_field: str = "tft_score"
    confidence_field: str = "tft_confidence"
    min_confidence: float = Field(0.55, ge=0.0, le=1.0)
    gate_mode: Literal["soft", "hard"] = "soft"


class LiquidityGateConfig(BaseModel):
    enabled: bool = True
    min_avg_turnover_20d: float = Field(50000000, ge=0)


class VolatilityGateConfig(BaseModel):
    enabled: bool = True
    max_volatility_20d: float = Field(0.06, ge=0)


class ExternalRiskSourceConfig(BaseModel):
    """外部风控数据源配置（新闻/宏观/政策）。"""

    enabled: bool = False
    provider: str = "internal"  # internal | akshare | custom
    endpoint: str = ""  # HTTP endpoint，internal 时留空
    cache_ttl_seconds: int = Field(3600, ge=0)
    fallback_risk_weight: float = Field(1.0, ge=0.0, le=1.5)


class Layer3RiskGateConfig(BaseModel):
    enabled: bool = True
    max_single_weight: float = Field(0.05, ge=0, le=1)
    max_industry_weight: float = Field(0.30, ge=0, le=1)
    max_turnover: float = Field(0.35, ge=0, le=1)
    liquidity_gate: LiquidityGateConfig = Field(default_factory=LiquidityGateConfig)
    volatility_gate: VolatilityGateConfig = Field(default_factory=VolatilityGateConfig)
    external_risk_source: ExternalRiskSourceConfig = Field(default_factory=ExternalRiskSourceConfig)


class MergeConfig(BaseModel):
    mode: Literal["weighted", "rank"] = "weighted"
    lgbm_weight: float = Field(0.65, ge=0, le=1)
    tft_weight: float = Field(0.35, ge=0, le=1)
    fallback: Literal["lgbm_only", "reject"] = "lgbm_only"


class FusionRulesConfig(BaseModel):
    version: str = "2026-02-26"
    enabled: bool = True
    description: str = "三层融合规则"
    layer1_lgbm: Layer1LgbmConfig = Field(default_factory=Layer1LgbmConfig)
    layer2_tft: Layer2TftConfig = Field(default_factory=Layer2TftConfig)
    layer3_risk_gate: Layer3RiskGateConfig = Field(default_factory=Layer3RiskGateConfig)
    merge: MergeConfig = Field(default_factory=MergeConfig)


_DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "fusion_rules.json"


def _resolve_config_path() -> Path:
    configured = os.getenv("ENGINE_FUSION_RULES_PATH")
    return Path(configured) if configured else _DEFAULT_CONFIG_PATH


@lru_cache(maxsize=1)
def load_fusion_rules_config() -> FusionRulesConfig:
    config_path = _resolve_config_path()
    if not config_path.exists():
        return FusionRulesConfig()

    with config_path.open("r", encoding="utf-8") as fp:
        payload: dict[str, Any] = json.load(fp)
    return FusionRulesConfig(**payload)


def get_fusion_rules_snapshot() -> dict[str, Any]:
    return load_fusion_rules_config().model_dump()
