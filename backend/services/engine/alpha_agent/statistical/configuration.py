"""One parameter contract for preview, admission and frozen statistical execution."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class TradabilityOverrides(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    exclude_suspended: bool | None = None
    st_snapshot_symbols: list[str] | None = Field(None, max_length=20000)
    min_list_days: int | None = Field(None, ge=0, le=5000, strict=True)
    exclude_limit_locked: bool | None = None
    limit_tolerance: float | None = Field(None, ge=0, le=0.05)
    min_amount_quantile: float | None = Field(None, ge=0, lt=1)
    min_amount_window: int | None = Field(None, ge=1, le=250, strict=True)

    @model_validator(mode="after")
    def normalize_symbols(self):
        if self.st_snapshot_symbols is not None:
            from backend.shared.stock_utils import StockCodeUtil

            normalized = []
            for symbol in self.st_snapshot_symbols:
                value = StockCodeUtil.to_suffix(symbol)
                if not value or StockCodeUtil.exchange(value) not in {"SH", "SZ", "BJ"}:
                    raise ValueError("ST snapshot symbols must be valid A-share codes")
                normalized.append(value)
            self.st_snapshot_symbols = sorted(set(normalized))
        return self


class StatisticalOverrides(BaseModel):
    """Optional overrides; defaults remain owned by StatisticalConfig."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    horizons: list[Annotated[int, Field(ge=1, le=250, strict=True)]] | None = Field(
        None, min_length=1, max_length=8
    )
    top_k: int | None = Field(None, ge=1, le=32, strict=True)
    min_names_per_day: int | None = Field(None, ge=3, le=10000, strict=True)
    min_valid_days: int | None = Field(None, ge=5, le=5000, strict=True)
    quantiles: int | None = Field(None, ge=2, le=20, strict=True)
    fdr_q: float | None = Field(None, gt=0, le=1)
    require_fdr: bool | None = None
    oos_min_ratio: float | None = Field(None, ge=0, le=2)
    oos_min_days: int | None = Field(None, ge=1, le=5000, strict=True)
    neutralize: Literal["none", "industry", "industry_size"] | None = None
    tradability: TradabilityOverrides | None = None
    stability_folds: int | None = Field(None, ge=2, le=20, strict=True)
    min_fold_sign_consistency: float | None = Field(None, gt=0, le=1)
    min_fold_days: int | None = Field(None, ge=1, le=1000, strict=True)
    require_stability: bool | None = None
    dedup_corr_reject: float | None = Field(None, gt=0, le=1)
    dedup_corr_warn: float | None = Field(None, ge=0, lt=1)
    require_dedup: bool | None = None
    composite_enabled: bool | None = None
    composite_z_window: int | None = Field(None, ge=2, le=250, strict=True)
    net_ic_cost_rate: float | None = Field(None, ge=0, le=1)

    @model_validator(mode="after")
    def unique_horizons(self):
        if self.horizons and len(set(self.horizons)) != len(self.horizons):
            raise ValueError("horizons 不能重复")
        return self


STATISTICAL_PARAMETER_KEYS = frozenset(StatisticalOverrides.model_fields)


def resolve_statistical_config(params: dict | None = None):
    """Normalize JSON overrides and apply the runner's actual defaults."""
    from .engine import StatisticalConfig
    from .tradability import TradabilityConfig

    raw = StatisticalOverrides.model_validate(params or {}).model_dump(
        exclude_none=True
    )
    if "horizons" in raw:
        raw["horizons"] = tuple(raw["horizons"])
    if "tradability" in raw:
        tradability = raw["tradability"]
        if "st_snapshot_symbols" in tradability:
            tradability["st_snapshot_symbols"] = tuple(
                tradability["st_snapshot_symbols"]
            )
        raw["tradability"] = TradabilityConfig(**tradability)
    return StatisticalConfig(**raw)


def statistical_config_payload(params: dict | None = None) -> dict:
    """JSON-compatible effective values for the catalogue and confirmation dialog."""
    from dataclasses import asdict

    result = asdict(resolve_statistical_config(params))
    if result["net_ic_cost_rate"] is None:
        from backend.services.engine.inference.trading_cost import CostModel

        result["net_ic_cost_rate"] = CostModel.resolve().round_trip_cost()
    result["horizons"] = list(result["horizons"])
    result["tradability"]["st_snapshot_symbols"] = list(
        result["tradability"]["st_snapshot_symbols"]
    )
    return result
