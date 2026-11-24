"""训练目标 × 模型能力矩阵（唯一事实源）。

机构级约束：
- 13 个可训练模型的类型集合、框架、参数容器键、目标能力只在这里定义；
  `per_model.py`、`model_trainers/registry.py`、`trainers_gbdt.py` 一律 import，
  不得复制。任何新增/删除模型必须同时改本文件，测试会强制 13 模型全覆盖。
- 本模块必须保持纯常量 + 纯函数：不 import torch/lightgbm/db，训练容器与 API
  容器都能在启动早期 import。
- 能力判定与错误码在这里单点定义；提交 API、编排器、训练容器三层复用同一函数
  （纵深防御，避免“前端拦了后端没拦”）。

能力语义：
- regression / classification / quantile：该模型能真正训练的目标模式；
- rank_loss：能使用按日分组排序损失（LambdaRank/YetiRank/rank:*）；
- sample_weight：训练器会把样本权重传到 `fit`（P1 起 linear/rf 接通；
  mlp 与全部 DL 不支持）；
- group_by_date：排序损失需要的按日分组能力；
- sequence：需要滚动时序窗口（Qlib TS / nativetft）。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Iterable

# ── 13 模型元数据 ─────────────────────────────────────────────────────────────

#: 模型 → 能力族。族只用于分组展示/统计，能力判定一律查 MODEL_CAPABILITIES。
MODEL_FAMILIES: dict[str, str] = {
    "lightgbm": "gbdt",
    "xgboost": "gbdt",
    "catboost": "gbdt",
    "linear": "sklearn_linear",
    "random_forest": "sklearn_ensemble",
    "mlp": "sklearn_neural",
    "gru": "qlib_dl_ts",
    "lstm": "qlib_dl_ts",
    "alstm": "qlib_dl_ts",
    "transformer": "qlib_dl_ts",
    "tcn": "qlib_dl_ts",
    "tabnet": "qlib_dl_flat",
    "nativetft": "custom_dl",
}

#: 与历史 registry._TREE_MODEL_TYPES 逐字一致（含 linear/random_forest 的
#: 6 参 sklearn 分派组；名字是历史包袱，不要按字面理解成“树模型”）。
TREE_MODEL_TYPES: frozenset[str] = frozenset(
    {"lightgbm", "xgboost", "catboost", "linear", "random_forest"}
)

#: 与历史 registry._DL_MODEL_TYPES 逐字一致（mlp 虽在集合里，实际走 sklearn）。
DL_MODEL_TYPES: frozenset[str] = frozenset(
    {"gru", "lstm", "alstm", "transformer", "tabnet", "tcn", "nativetft", "mlp"}
)

#: 非 Qlib 的自定义 DL 模型（hybrid 已剔除，未实现）。
CUSTOM_DL_MODEL_TYPES: frozenset[str] = frozenset({"nativetft", "mlp"})

#: 真正的 Qlib DL 模型（时序 + 扁平 TabNet）。
QLIB_DL_MODEL_TYPES: frozenset[str] = DL_MODEL_TYPES - CUSTOM_DL_MODEL_TYPES

#: sklearn 实现组（linear/rf/mlp）。
SKLEARN_MODEL_TYPES: frozenset[str] = frozenset(
    {"linear", "random_forest", "mlp"}
)

ALL_MODEL_TYPES: frozenset[str] = TREE_MODEL_TYPES | DL_MODEL_TYPES

#: 可参与集成的树模型（历史口径：TREE - linear）。
ENSEMBLE_MODEL_TYPES: frozenset[str] = TREE_MODEL_TYPES - {"linear"}

#: 模型 → 自家 params 容器键名（与 _build_config_yaml 读取键一致）。
#: linear/rf/mlp 与 DL 组共用顶层 dl_params 键（实现如此）。
MODEL_PARAMS_FIELD: dict[str, str] = {
    "lightgbm": "lgb_params",
    "xgboost": "xgb_params",
    "catboost": "catboost_params",
    "linear": "dl_params",
    "random_forest": "dl_params",
    "mlp": "dl_params",
    "gru": "dl_params",
    "lstm": "dl_params",
    "alstm": "dl_params",
    "transformer": "dl_params",
    "tabnet": "dl_params",
    "tcn": "dl_params",
    "nativetft": "dl_params",
}

#: 模型 → 框架（展示用；mlp 为 sklearn 实现）。
MODEL_FRAMEWORK: dict[str, str] = {
    "lightgbm": "lightgbm",
    "xgboost": "xgboost",
    "catboost": "catboost",
    "linear": "sklearn",
    "random_forest": "sklearn",
    "mlp": "sklearn",
    "gru": "pytorch",
    "lstm": "pytorch",
    "alstm": "pytorch",
    "transformer": "pytorch",
    "tabnet": "pytorch",
    "tcn": "pytorch",
    "nativetft": "pytorch",
}

#: 各模型可用的排序损失名（小写）。训练端必须按日分组。
RANK_OBJECTIVES: dict[str, set[str]] = {
    "lightgbm": {"lambdarank", "rank_xendcg"},
    "xgboost": {"rank:pairwise", "rank:ndcg", "rank:map"},
    "catboost": {
        "yetirank",
        "yetirankpairwise",
        "queryrmse",
        "querysoftmax",
        "queryauc",
        "lambdamart",
    },
}

#: 各模型的二分类 objective（小写）。仅用于冲突检测；训练器仍会强制覆盖。
BINARY_OBJECTIVES: dict[str, set[str]] = {
    "lightgbm": {"binary"},
    "xgboost": {"binary:logistic"},
    "catboost": {"logloss"},
}


@dataclass(frozen=True)
class ModelCapability:
    """单个模型的目标能力。字段名即 API 输出键。"""

    model_type: str
    family: str
    regression: bool
    classification: bool
    quantile: bool
    rank_loss: bool
    sample_weight: bool
    group_by_date: bool
    sequence: bool


# ── 逐模型能力表（13 行，测试强制与 ALL_MODEL_TYPES 相等）────────────────────
MODEL_CAPABILITIES: dict[str, ModelCapability] = {
    "lightgbm": ModelCapability(
        "lightgbm", MODEL_FAMILIES["lightgbm"], True, True, True, True, True, True, False
    ),
    "xgboost": ModelCapability(
        "xgboost", MODEL_FAMILIES["xgboost"], True, True, False, True, True, True, False
    ),
    "catboost": ModelCapability(
        "catboost", MODEL_FAMILIES["catboost"], True, True, False, True, True, True, False
    ),
    "linear": ModelCapability(
        "linear", MODEL_FAMILIES["linear"], True, True, False, False, True, False, False
    ),
    "random_forest": ModelCapability(
        "random_forest",
        MODEL_FAMILIES["random_forest"],
        True,
        True,
        False,
        False,
        True,
        False,
        False,
    ),
    "mlp": ModelCapability(
        "mlp", MODEL_FAMILIES["mlp"], True, True, False, False, False, False, False
    ),
    "gru": ModelCapability(
        "gru", MODEL_FAMILIES["gru"], True, False, False, False, False, False, True
    ),
    "lstm": ModelCapability(
        "lstm", MODEL_FAMILIES["lstm"], True, False, False, False, False, False, True
    ),
    "alstm": ModelCapability(
        "alstm", MODEL_FAMILIES["alstm"], True, False, False, False, False, False, True
    ),
    "transformer": ModelCapability(
        "transformer",
        MODEL_FAMILIES["transformer"],
        True,
        False,
        False,
        False,
        False,
        False,
        True,
    ),
    "tcn": ModelCapability(
        "tcn", MODEL_FAMILIES["tcn"], True, False, False, False, False, False, True
    ),
    "tabnet": ModelCapability(
        "tabnet", MODEL_FAMILIES["tabnet"], True, False, False, False, False, False, False
    ),
    "nativetft": ModelCapability(
        "nativetft",
        MODEL_FAMILIES["nativetft"],
        True,
        False,
        False,
        False,
        False,
        False,
        True,
    ),
}

if set(MODEL_CAPABILITIES) != set(ALL_MODEL_TYPES):  # pragma: no cover - 导入即失败
    raise RuntimeError(
        "MODEL_CAPABILITIES 与 ALL_MODEL_TYPES 不一致："
        f"missing={sorted(set(ALL_MODEL_TYPES) - set(MODEL_CAPABILITIES))} "
        f"extra={sorted(set(MODEL_CAPABILITIES) - set(ALL_MODEL_TYPES))}"
    )
if set(MODEL_PARAMS_FIELD) != set(ALL_MODEL_TYPES):  # pragma: no cover
    raise RuntimeError("MODEL_PARAMS_FIELD 未覆盖全部 13 个模型")
if set(MODEL_FRAMEWORK) != set(ALL_MODEL_TYPES):  # pragma: no cover
    raise RuntimeError("MODEL_FRAMEWORK 未覆盖全部 13 个模型")

#: 能力矩阵契约版本：新增字段/改变语义时 +1。
CAPABILITY_MATRIX_VERSION = 1


class CapabilityError(ValueError):
    """能力校验失败。`code` 是稳定机器码，API 转 422，容器转 RuntimeError。"""

    code = "target_capability_unsupported"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code


class ObjectiveConflictError(CapabilityError):
    code = "target_objective_conflict"


# ── 纯函数 ───────────────────────────────────────────────────────────────────


def normalize_model_types(
    model_type: str | None, model_types: Iterable[str] | None
) -> tuple[str, ...]:
    """归一化模型列表：优先 model_types，回退 model_type；保持顺序去重。"""
    raw = list(model_types or [])
    if not raw:
        raw = [model_type or "lightgbm"]
    out: list[str] = []
    for item in raw:
        name = str(item or "").strip().lower()
        if not name:
            continue
        if name not in ALL_MODEL_TYPES:
            raise CapabilityError(f"Unsupported model_type: {name}")
        if name not in out:
            out.append(name)
    if not out:
        raise CapabilityError("model_types must not be empty")
    return tuple(out)


def model_capability(model_type: str) -> ModelCapability:
    name = str(model_type or "").strip().lower()
    cap = MODEL_CAPABILITIES.get(name)
    if cap is None:
        raise CapabilityError(f"Unsupported model_type: {name}")
    return cap


def intersect_capability(model_types: Iterable[str]) -> dict[str, bool]:
    """多模型集成的能力交集。未知模型直接抛错。"""
    names = tuple(str(x).strip().lower() for x in model_types)
    if not names:
        raise CapabilityError("model_types must not be empty")
    caps = [model_capability(x) for x in names]
    return {
        "regression": all(c.regression for c in caps),
        "classification": all(c.classification for c in caps),
        "quantile": all(c.quantile for c in caps),
        "rank_loss": all(c.rank_loss for c in caps),
        "sample_weight": all(c.sample_weight for c in caps),
        "group_by_date": all(c.group_by_date for c in caps),
        "sequence": all(c.sequence for c in caps),
    }


def unsupported_models(model_types: Iterable[str], capability: str) -> list[str]:
    """返回不支持某能力的模型列表（保持输入顺序）。"""
    out: list[str] = []
    for name in model_types:
        cap = model_capability(name)
        if not bool(getattr(cap, capability)):
            out.append(str(name).strip().lower())
    return out


def _objective_owner(model_type: str, objective: str | None) -> set[str]:
    return RANK_OBJECTIVES.get(model_type, set()) if objective else set()


def validate_target_model_combo(
    model_types: Iterable[str],
    *,
    target_mode: str = "return",
    prediction_mode: str = "point",
    market: str = "CN",
    sample_weight: bool = False,
    objective: str | None = None,
) -> None:
    """校验目标模式 × 13 模型能力，失败抛 CapabilityError/ObjectiveConflictError。

    参数口径：
    - target_mode：``return`` / ``classification``；分位模式在 P1 仍以
      target_mode=return + prediction_mode=quantile 表达（与现有训练容器一致）。
    - objective：调用方按模型取出的原始 objective（lgb params.objective /
      xgb params.objective / catboost loss_function），可为 None。
    """
    names = tuple(str(x).strip().lower() for x in model_types)
    if not names:
        raise CapabilityError("model_types must not be empty")
    for name in names:
        model_capability(name)

    mode = str(target_mode or "return").strip().lower()
    pred_mode = str(prediction_mode or "point").strip().lower()
    market_upper = str(market or "CN").strip().upper()

    if mode not in {"return", "classification"}:
        raise CapabilityError(f"Unsupported target_mode: {mode}")
    if pred_mode not in {"point", "quantile"}:
        raise CapabilityError(f"Unsupported prediction_mode: {pred_mode}")

    # 1) 分类目标：全部成员必须支持。
    if mode == "classification":
        bad = unsupported_models(names, "classification")
        if bad:
            raise CapabilityError(
                "classification 目标不被以下模型支持："
                f"{', '.join(bad)}（仅支持 {', '.join(sorted(k for k, v in MODEL_CAPABILITIES.items() if v.classification))}）"
            )

    # 2) 分位模式：仅 CN 单 LightGBM + return。
    if pred_mode == "quantile":
        if market_upper != "CN":
            raise CapabilityError("prediction_mode=quantile 仅支持 CN 市场")
        if name := (names[0] if len(names) == 1 else None):
            if name != "lightgbm" or not model_capability(name).quantile:
                raise CapabilityError(
                    "prediction_mode=quantile 仅支持单 LightGBM 模型"
                )
        else:
            raise CapabilityError(
                "prediction_mode=quantile 仅支持单 LightGBM 模型（当前为多模型）"
            )
        if mode != "return":
            raise CapabilityError(
                "prediction_mode=quantile 只允许 target_mode=return"
            )

    # 3) 样本权重：全部成员必须支持。
    if sample_weight:
        bad = unsupported_models(names, "sample_weight")
        if bad:
            raise CapabilityError(
                "sample_weight 不被以下模型支持："
                f"{', '.join(bad)}（仅支持 lightgbm/xgboost/catboost/linear/random_forest）"
            )

    # 4) objective 冲突：排序损失只允许三棵 GBDT；二分类 objective 只允许
    #    classification 目标；回归目标拒绝二分类 objective。
    obj = str(objective or "").strip().lower()
    if not obj:
        return
    rank_owners = {k for k, v in RANK_OBJECTIVES.items() if obj in v}
    if obj in {o for values in RANK_OBJECTIVES.values() for o in values}:
        if mode == "classification":
            raise ObjectiveConflictError(
                f"objective={obj} 是排序损失，不能用于 classification 目标"
            )
        if len(names) != 1:
            raise ObjectiveConflictError(
                f"objective={obj} 排序损失只允许单模型训练（当前 {len(names)} 个）"
            )
        if names[0] not in rank_owners or not model_capability(names[0]).rank_loss:
            raise ObjectiveConflictError(
                f"objective={obj} 不被 model_type={names[0]} 支持（仅支持 "
                f"{', '.join(sorted(rank_owners))}）"
            )
        return

    binary_hit = any(obj in BINARY_OBJECTIVES.get(n, set()) for n in names)
    if mode == "return" and binary_hit:
        raise ObjectiveConflictError(
            f"objective={obj} 是二分类损失，不能用于 return 目标"
        )
    if mode == "classification" and not binary_hit:
        raise ObjectiveConflictError(
            f" objective={obj} 与 classification 目标冲突；"
            "二分类目标只允许 binary / binary:logistic / Logloss（或留空由训练器派生）"
        )


def capability_matrix() -> dict:
    """API 输出：13 模型 × 7 能力 + 目标模式支持 + 矩阵版本。"""
    modes = {
        "return": sorted(
            k for k, v in MODEL_CAPABILITIES.items() if v.regression
        ),
        "classification": sorted(
            k for k, v in MODEL_CAPABILITIES.items() if v.classification
        ),
        "quantile": sorted(
            k for k, v in MODEL_CAPABILITIES.items() if v.quantile
        ),
    }
    return {
        "matrix_version": CAPABILITY_MATRIX_VERSION,
        "models": [
            asdict(MODEL_CAPABILITIES[name])
            for name in sorted(MODEL_CAPABILITIES)
        ],
        "target_modes": modes,
        "rank_objectives": {
            name: sorted(values) for name, values in sorted(RANK_OBJECTIVES.items())
        },
        "binary_objectives": {
            name: sorted(values)
            for name, values in sorted(BINARY_OBJECTIVES.items())
        },
    }


__all__ = [
    "ALL_MODEL_TYPES",
    "BINARY_OBJECTIVES",
    "CAPABILITY_MATRIX_VERSION",
    "CUSTOM_DL_MODEL_TYPES",
    "CapabilityError",
    "DL_MODEL_TYPES",
    "ENSEMBLE_MODEL_TYPES",
    "MODEL_CAPABILITIES",
    "MODEL_FAMILIES",
    "MODEL_FRAMEWORK",
    "MODEL_PARAMS_FIELD",
    "ModelCapability",
    "ObjectiveConflictError",
    "QLIB_DL_MODEL_TYPES",
    "RANK_OBJECTIVES",
    "SKLEARN_MODEL_TYPES",
    "TREE_MODEL_TYPES",
    "capability_matrix",
    "intersect_capability",
    "model_capability",
    "normalize_model_types",
    "unsupported_models",
    "validate_target_model_combo",
]
