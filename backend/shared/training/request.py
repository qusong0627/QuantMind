"""训练入参层契约 TrainingRequest（方案 B1-2：_normalize_payload 纯校验迁移）。

定位（REFACTOR_TRAINING_B §3.3）：入参/编排层，与 config.yaml 层的 TrainingConfig
是两个模型。DB 耦合校验（特征目录）、显式 split gap 推导、各类透传仍留在
_normalize_payload；这里只收敛纯输入校验，且 422 detail 与现状逐字相同
（见 /tmp/norm_snap_base.json 快照，多错误并发时的报错优先级也与现状一致）。

行为变更（有意为之，均已记录）：
- 数值垃圾输入（val_ratio/num_boost_round/early_stopping_rounds/target_horizon_days
  非数字字符串）：现状未捕获 ValueError → 500；现收敛为 422 范围消息。
- 其余所有单错误输入的 status_code/detail 与现状逐字相同。

训练容器不消费本模块（api 侧专用）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

ALLOWED_MODEL_TYPES = frozenset(
    {
        "lightgbm",
        "xgboost",
        "catboost",
        "linear",
        "random_forest",
        "gru",
        "lstm",
        "alstm",
        "transformer",
        "tabnet",
        "tcn",
        "nativetft",
        "mlp",
        # hybrid_gru_tree：QLIB map 无实现，标准入口不可训练（2026-09 实测 KeyError），
        # 已从可选项剔除；有专用管线实现后再加回。
    }
)
ALLOWED_TARGET_MODE = {"return", "classification"}
ALLOWED_DEAL_PRICE = {"open", "close"}

_BENCHMARK_MARKET = {
    "HSI": "HK",
    "HSCEI": "HK",
    "HSTECH": "HK",
    "SPX": "US",
    "NDX": "US",
    "DJI": "US",
    "IXIC": "US",
    "BTC": "CRYPTO",
    "ETH": "CRYPTO",
    "CL": "FUTURES",
    "RB": "FUTURES",
    "AU": "FUTURES",
    "CU": "FUTURES",
}


def clamp_int(value: Any, default: int, lo: int, hi: int) -> int:
    """安全地把输入转成 int 并 clamp 到 [lo, hi]（从 admin_training_utils 下沉，单源）。"""
    try:
        n = int(value)
    except Exception:
        n = default
    return max(lo, min(hi, n))


def coerce_float(value: Any) -> float | None:
    """宽松转 float；None/垃圾/NaN/Inf 一律回 None（从 admin_training_utils 下沉）。"""
    try:
        if value is None:
            return None
        f = float(value)
        if f != f or f in (float("inf"), float("-inf")):
            return None
        return f
    except Exception:
        return None


def parse_date(date_str: str, field: str) -> datetime:
    """ISO 日期解析，失败抛 422（消息与现状逐字相同）。"""
    try:
        return datetime.fromisoformat(date_str)
    except Exception as exc:
        raise HTTPException(
            status_code=422, detail=f"Invalid date for {field}: {date_str}"
        ) from exc


def resolve_market(raw_market: Any, benchmark: str) -> str:
    """解析目标市场：显式字段优先，缺失/非法时从 benchmark 推断，回退 CN。"""
    market = str(raw_market or "").strip().upper()
    if market in ("CN", "US", "HK", "CRYPTO", "FUTURES"):
        return market
    return _BENCHMARK_MARKET.get(str(benchmark or "").upper(), "CN")


def normalize_prediction_mode(raw: Any) -> str:
    """归一化分位推理模式；非法值静默回落 point。"""
    mode = str(raw or "point").strip().lower()
    return mode if mode in ("point", "quantile") else "point"


class ContextRequest(BaseModel):
    """context 子对象校验（422 消息与 _normalize_context 逐字相同）。"""

    model_config = ConfigDict(extra="ignore")

    initial_capital: Any = None
    initialCapital: Any = None
    benchmark: Any = "SH000300"
    commission_rate: Any = None
    commissionRate: Any = None
    slippage: Any = None
    deal_price: Any = None
    dealPrice: Any = None
    market: Any = None
    industry_as_feature: Any = False

    def cleaned(self) -> dict[str, Any]:
        initial_capital = coerce_float(self.initial_capital)
        if initial_capital is None:
            initial_capital = coerce_float(self.initialCapital)
        initial_capital = (
            initial_capital if initial_capital is not None else 1_000_000.0
        )
        if initial_capital <= 0:
            raise HTTPException(
                status_code=422, detail="context.initial_capital must be > 0"
            )

        benchmark = str(self.benchmark or "SH000300").strip() or "SH000300"

        commission_rate = coerce_float(self.commission_rate)
        if commission_rate is None:
            commission_rate = coerce_float(self.commissionRate)
        commission_rate = commission_rate if commission_rate is not None else 0.00025
        if commission_rate < 0:
            raise HTTPException(
                status_code=422, detail="context.commission_rate must be >= 0"
            )

        slippage = coerce_float(self.slippage)
        slippage = slippage if slippage is not None else 0.0005
        if slippage < 0:
            raise HTTPException(status_code=422, detail="context.slippage must be >= 0")

        deal_price = str(self.deal_price or self.dealPrice or "close").strip().lower()
        if deal_price not in ALLOWED_DEAL_PRICE:
            raise HTTPException(
                status_code=422, detail="context.deal_price must be one of: open, close"
            )

        return {
            "initial_capital": initial_capital,
            "benchmark": benchmark,
            "commission_rate": commission_rate,
            "slippage": slippage,
            "deal_price": deal_price,
            "market": resolve_market(self.market, benchmark),
            "industry_as_feature": bool(self.industry_as_feature or False),
        }


class TrainingRequest(BaseModel):
    """训练入参清洗后契约。validate_request 按现状顺序执行纯校验（422 逐字相同），
    返回类型化的清洗结果；DB 耦合与推导仍在 _normalize_payload。
    """

    model_config = ConfigDict(extra="ignore")

    model_type: str = "lightgbm"
    model_types: list[str] | None = None
    ensemble: str = "none"
    wfa: dict | None = None
    display_name: str = "unnamed"
    job_name: str = "unnamed"
    train_start: str = "2023-01-11"
    train_end: str = "2024-12-31"
    val_ratio: float = 0.15
    num_boost_round: int = 1000
    early_stopping_rounds: int = 100
    features: list[str] = Field(default_factory=list)
    lgb_params: dict = Field(default_factory=dict)
    xgb_params: dict = Field(default_factory=dict)
    catboost_params: dict = Field(default_factory=dict)
    dl_params: dict = Field(default_factory=dict)
    target_horizon_days: int = 1
    horizons: list[int] | None = None
    target_mode: str = "return"
    label_formula: str = ""
    effective_trade_date: str = ""
    training_window: str = ""
    context: dict = Field(default_factory=dict)
    prediction_mode: str = "point"

    @classmethod
    def validate_request(cls, payload: dict[str, Any]) -> TrainingRequest:
        if not isinstance(payload, dict):
            raise HTTPException(status_code=422, detail="Payload must be a JSON object")

        model_type = str(payload.get("model_type", "lightgbm")).strip().lower()
        if model_type not in ALLOWED_MODEL_TYPES:
            raise HTTPException(
                status_code=422,
                detail=f"Unsupported model_type: {model_type}. Allowed: {sorted(ALLOWED_MODEL_TYPES)}",
            )

        model_types: list[str] | None = None
        raw_model_types = payload.get("model_types")
        if raw_model_types and isinstance(raw_model_types, list):
            model_types = [
                str(t).strip().lower() for t in raw_model_types if str(t).strip()
            ]
            for mt in model_types:
                if mt not in ALLOWED_MODEL_TYPES:
                    raise HTTPException(
                        status_code=422,
                        detail=f"Unsupported model_type in model_types: {mt}. "
                        f"Allowed: {sorted(ALLOWED_MODEL_TYPES)}",
                    )

        ensemble = str(payload.get("ensemble", "none")).strip().lower()
        if ensemble not in ("none", "stacking", "blending", "voting"):
            ensemble = "none"

        raw_wfa = payload.get("wfa")
        wfa: dict | None = None
        if raw_wfa:
            if not isinstance(raw_wfa, dict):
                raise HTTPException(status_code=422, detail="wfa must be an object")
            if str(raw_wfa.get("strategy", "rolling")).strip().lower() not in (
                "rolling",
                "expanding",
            ):
                raise HTTPException(
                    status_code=422,
                    detail="wfa.strategy must be one of: rolling, expanding",
                )
            wfa = raw_wfa

        display_name = (
            str(
                payload.get("display_name") or payload.get("job_name") or "unnamed"
            ).strip()
            or "unnamed"
        )
        if len(display_name) > 128:
            raise HTTPException(
                status_code=422, detail="display_name must be at most 128 characters"
            )
        job_name = str(payload.get("job_name", "unnamed")).strip() or "unnamed"

        train_start = str(payload.get("train_start", "2023-01-11")).strip()
        train_end = str(payload.get("train_end", "2024-12-31")).strip()
        dt_train_start = parse_date(train_start, "train_start")
        dt_train_end = parse_date(train_end, "train_end")
        if dt_train_start >= dt_train_end:
            raise HTTPException(
                status_code=422, detail="train_start must be earlier than train_end"
            )

        try:
            val_ratio = float(payload.get("val_ratio", 0.15))
        except Exception:
            raise HTTPException(
                status_code=422, detail="val_ratio must be between 0.01 and 0.5"
            ) from None
        if not (0.01 <= val_ratio <= 0.5):
            raise HTTPException(
                status_code=422, detail="val_ratio must be between 0.01 and 0.5"
            )

        try:
            num_boost_round = int(payload.get("num_boost_round", 1000))
        except Exception:
            raise HTTPException(
                status_code=422, detail="num_boost_round must be between 10 and 20000"
            ) from None
        if not (10 <= num_boost_round <= 20000):
            raise HTTPException(
                status_code=422, detail="num_boost_round must be between 10 and 20000"
            )

        try:
            early_stopping_rounds = int(payload.get("early_stopping_rounds", 100))
        except Exception:
            raise HTTPException(
                status_code=422,
                detail="early_stopping_rounds must be between 1 and 5000",
            ) from None
        if not (1 <= early_stopping_rounds <= 5000):
            raise HTTPException(
                status_code=422,
                detail="early_stopping_rounds must be between 1 and 5000",
            )

        raw_features = payload.get("features", []) or []
        if not isinstance(raw_features, list):
            raise HTTPException(
                status_code=422, detail="features must be a string array"
            )
        features: list[str] = []
        for item in raw_features:
            val = str(item).strip()
            if val and val not in features:
                features.append(val)
        if len(features) > 600:
            raise HTTPException(
                status_code=422, detail="features length cannot exceed 600"
            )

        for key, message in (
            ("lgb_params", "lgb_params must be an object"),
            ("xgb_params", "xgb_params must be an object"),
            ("catboost_params", "catboost_params must be an object"),
            ("dl_params", "dl_params must be an object"),
        ):
            params = payload.get(key, {}) or {}
            if not isinstance(params, dict):
                raise HTTPException(status_code=422, detail=message)

        try:
            target_horizon_days = int(payload.get("target_horizon_days", 1))
        except Exception:
            raise HTTPException(
                status_code=422, detail="target_horizon_days must be between 1 and 30"
            ) from None
        if not (1 <= target_horizon_days <= 30):
            raise HTTPException(
                status_code=422, detail="target_horizon_days must be between 1 and 30"
            )

        horizons: list[int] | None = None
        raw_horizons = payload.get("horizons")
        if raw_horizons is not None:
            if not isinstance(raw_horizons, list) or not raw_horizons:
                raise HTTPException(
                    status_code=422,
                    detail="horizons must be a non-empty array of integers",
                )
            horizons = []
            for h in raw_horizons:
                try:
                    hv = int(h)
                except Exception:
                    raise HTTPException(
                        status_code=422,
                        detail=f"horizons contains non-integer value: {h}",
                    ) from None
                if not (1 <= hv <= 30):
                    raise HTTPException(
                        status_code=422,
                        detail=f"horizons value must be between 1 and 30: {hv}",
                    )
                horizons.append(hv)

        target_mode = str(payload.get("target_mode", "return")).strip().lower()
        if target_mode not in ALLOWED_TARGET_MODE:
            raise HTTPException(
                status_code=422,
                detail="target_mode must be one of: return, classification",
            )

        label_formula = str(payload.get("label_formula") or "").strip()
        effective_trade_date = str(payload.get("effective_trade_date") or "").strip()
        if effective_trade_date:
            parse_date(effective_trade_date, "effective_trade_date")
        training_window = str(payload.get("training_window") or "").strip()

        context_raw = payload.get("context", {}) or {}
        if not isinstance(context_raw, dict):
            raise HTTPException(status_code=422, detail="context must be an object")
        context = ContextRequest.model_validate(context_raw).cleaned()

        prediction_mode = normalize_prediction_mode(payload.get("prediction_mode"))

        try:
            return cls(
                model_type=model_type,
                model_types=model_types,
                ensemble=ensemble,
                wfa=wfa,
                display_name=display_name,
                job_name=job_name,
                train_start=train_start,
                train_end=train_end,
                val_ratio=val_ratio,
                num_boost_round=num_boost_round,
                early_stopping_rounds=early_stopping_rounds,
                features=features,
                lgb_params=payload.get("lgb_params", {}) or {},
                xgb_params=payload.get("xgb_params", {}) or {},
                catboost_params=payload.get("catboost_params", {}) or {},
                dl_params=payload.get("dl_params", {}) or {},
                target_horizon_days=target_horizon_days,
                horizons=horizons,
                target_mode=target_mode,
                label_formula=label_formula,
                effective_trade_date=effective_trade_date,
                training_window=training_window,
                context=context,
                prediction_mode=prediction_mode,
            )
        except HTTPException:
            raise
        except Exception as exc:
            raise RuntimeError(f"TrainingRequest construction failed: {exc}") from exc
