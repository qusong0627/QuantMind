"""训练配置 Schema（方案 B1：config.yaml 跨容器契约的类型化）。

设计约束（REFACTOR_TRAINING_B §3.3 / §5）：
1. key 集合与默认值语义与 ``_build_config_yaml`` 现状逐字节对齐（字段顺序按现状排放）。
2. “读者有、生产者无”的死配置（``optuna/drift/n_folds/meta_alpha/monitor_rank_ic``）
   只建类型、不参与序列化（``exclude=True``），本轮不激活任何行为变更。
3. ``seed`` 恒定 42（train.py 默认），B1 同样只建类型不发射，避免 key 集合漂移。
4. 数值钳制语义复制现状（而非收紧拒绝），B1 行为不变；非法枚举触发 ValidationError
   时调用方回退 legacy 手拼 dict 并记 warning（fail-open，B2 再收紧）。

字段来源/落点核对见 REFACTOR_TRAINING_B §3.3.1。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Market = Literal["CN", "HK", "US", "CRYPTO", "FUTURES"]
ModelType = Literal[
    "lightgbm",
    "xgboost",
    "catboost",
    "linear",
    "random_forest",
    "mlp",
    "gru",
    "lstm",
    "alstm",
    "transformer",
    "tra",
    "hist",
    "tabnet",
    "tcn",
    "nativetft",
    # hybrid_gru_tree：QLIB map 无实现，已从可选项剔除（见 request.ALLOWED_MODEL_TYPES）。
]


class ModelCfg(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: ModelType = "lightgbm"
    types: list[ModelType] | None = None
    ensemble: str = "none"
    prediction_mode: Literal["point", "quantile"] = "point"
    num_boost_round: int = 1000
    early_stopping_rounds: int = 100
    val_ratio: float | None = 0.15
    params: dict = Field(default_factory=dict)
    xgb_params: dict = Field(default_factory=dict)
    catboost_params: dict = Field(default_factory=dict)
    dl_params: dict = Field(default_factory=dict)
    # ── 死配置：只建类型，不发射（exclude=True，见模块 docstring）──
    n_folds: int | None = Field(default=None, exclude=True)
    meta_alpha: float | None = Field(default=None, exclude=True)
    monitor_rank_ic: bool = Field(default=True, exclude=True)

    @field_validator("xgb_params", mode="before")
    @classmethod
    def _drop_invalid_xgb_depth(cls, v):
        # LightGBM max_depth=-1 约定对 XGBoost 非法：现状在两处手写剥离，
        # 收进 schema 后单点处理（幂等，重复执行无影响）。
        if isinstance(v, dict) and isinstance(v.get("max_depth"), (int, float)):
            if v["max_depth"] < 0:
                v = {k: val for k, val in v.items() if k != "max_depth"}
        return v


class DataCfg(BaseModel):
    model_config = ConfigDict(extra="ignore")

    train_start: str = "2022-01-01"
    train_end: str = "2024-12-31"
    features: list[str] = Field(default_factory=list)
    source_mode: Literal["LOCAL", "COS"] = "LOCAL"
    local_dir: str | None = None
    factor_source: str | None = None
    factor_catalog_version: str | None = None
    factor_schema_hash: str | None = None
    factor_field_sources: dict = Field(default_factory=dict)
    factor_catalog_published_at: str | None = None
    factor_coverage: dict = Field(default_factory=dict)
    quantdb_dir: str | None = None


class LabelCfg(BaseModel):
    model_config = ConfigDict(extra="ignore")

    target_horizon_days: int = 1
    target_mode: Literal["return", "classification"] = "return"
    label_formula: str = ""
    effective_trade_date: str = ""
    training_window: str = ""


class ContextCfg(BaseModel):
    model_config = ConfigDict(extra="ignore")

    initial_capital: float = 1_000_000
    benchmark: str = "SH000300"
    commission_rate: float = 0.00025
    slippage: float = 0.0005
    deal_price: Literal["open", "close"] = "close"
    market: str = "CN"
    industry_as_feature: bool = False


class SplitCfg(BaseModel):
    model_config = ConfigDict(extra="ignore")

    train: list[str] = Field(min_length=2, max_length=2)
    valid: list[str] = Field(min_length=2, max_length=2)
    test: list[str] = Field(min_length=2, max_length=2)


class OutputCfg(BaseModel):
    model_config = ConfigDict(extra="ignore")

    result_path: str = "/workspace/result.json"
    # 以 _build_config_yaml 的默认值为准（不含 config.yaml，见 §3.3.2③）。
    required_artifacts: list[str] = Field(
        default_factory=lambda: [
            "model.lgb",
            "pred.pkl",
            "metadata.json",
            "result.json",
        ]
    )


class CallbackCfg(BaseModel):
    model_config = ConfigDict(extra="ignore")

    url: str = ""
    secret: str = ""


class TrainingConfig(BaseModel):
    """config.yaml 顶层。字段顺序与 _build_config_yaml 现状排放一致。"""

    model_config = ConfigDict(extra="ignore")

    run_id: str = "unknown"
    job_name: str = "unnamed"
    # 恒定 42（train.py 默认），B1 只建类型不发射。
    seed: int = Field(default=42, exclude=True)
    data: DataCfg = Field(default_factory=DataCfg)
    model: ModelCfg = Field(default_factory=ModelCfg)
    label: LabelCfg = Field(default_factory=LabelCfg)
    context: ContextCfg = Field(default_factory=ContextCfg)
    explain: dict = Field(default_factory=dict)
    output: OutputCfg = Field(default_factory=OutputCfg)
    callback: CallbackCfg = Field(default_factory=CallbackCfg)
    cache: dict | None = None
    split: SplitCfg | None = None
    wfa: dict | None = None
    max_time_minutes: int = 120
    factor_selection: dict | None = None
    preprocessing: dict | None = None
    # ── 死配置：只建类型，不发射 ──
    optuna: dict | None = Field(default=None, exclude=True)
    drift: dict | None = Field(default=None, exclude=True)

    @field_validator("max_time_minutes", mode="before")
    @classmethod
    def _clamp_max_time(cls, v):
        # 复制现状钳制语义：max(10, int(v or 120))，异常回落 120。
        try:
            return max(10, int(v or 120))
        except Exception:
            return 120

    @classmethod
    def from_dict(cls, raw: dict) -> TrainingConfig:
        return cls.model_validate(raw)


# 条件键：现状缺失时不发射（无键），schema 侧以 None 占位。
CONDITIONAL_KEYS = ("split", "wfa", "factor_selection", "preprocessing")


def dump_contract_dict(cfg: TrainingConfig) -> dict:
    """返回与现状 key 集合一致的 config 字典（条件键 None 占位剥离）。

    api 编排器与单测都走此函数，保证“key 集合不变”由单点守护。
    """
    out = cfg.model_dump()
    for _key in CONDITIONAL_KEYS:
        if out.get(_key) is None:
            out.pop(_key, None)
    return out
