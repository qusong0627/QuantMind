"""训练目标契约的容器侧校验（config.yaml → 自校验）。

设计：
- 新路径（label.target_id 存在）：严格校验 spec 可解析、hash 一致、平铺字段与
  spec 一致、契约版本已知、能力组合合法；任一不符即 abort。
- legacy 路径（无 target_id）：从平铺字段合成 spec，能力冲突只告警不 abort
  （旧任务/旧镜像的兼容边界）；标签诊断仍按同一套 spec 执行。
- 本模块只依赖 target_spec/capabilities，不连 DB；API 与训练容器共用。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from backend.shared.training.capabilities import (
    CapabilityError,
    ObjectiveConflictError,
    validate_target_model_combo,
)
from backend.shared.training.target_spec import (
    LabelTargetSpec,
    TargetSpecError,
    label_fields_match,
    spec_from_dict,
    spec_from_label_fields,
)

SUPPORTED_CONTRACT_VERSIONS = frozenset({1})


class TargetContractError(RuntimeError):
    code = "target_contract_invalid"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code


@dataclass(frozen=True)
class TargetContract:
    spec: LabelTargetSpec
    legacy: bool
    status: str
    base_hash: str
    effective_hash: str
    contract_version: int

    @property
    def target_mode(self) -> str:
        return str(self.spec.to_label_fields()["target_mode"])

    @property
    def target_id(self) -> str:
        return self.spec.target_id

    @property
    def version(self) -> int:
        return self.spec.version


def _cfg_label(cfg: Mapping[str, Any]) -> dict[str, Any]:
    label = cfg.get("label")
    return dict(label) if isinstance(label, Mapping) else {}


def _cfg_model(cfg: Mapping[str, Any]) -> dict[str, Any]:
    model = cfg.get("model")
    return dict(model) if isinstance(model, Mapping) else {}


def _cfg_market(cfg: Mapping[str, Any]) -> str:
    context = cfg.get("context")
    context = context if isinstance(context, Mapping) else {}
    return str(context.get("market") or "CN").upper()


def _extract_objective(model_type: str, model_cfg: Mapping[str, Any]) -> str | None:
    mt = str(model_type or "").lower()
    if mt == "lightgbm":
        return str((model_cfg.get("params") or {}).get("objective") or "").strip() or None
    if mt == "xgboost":
        return str((model_cfg.get("xgb_params") or {}).get("objective") or "").strip() or None
    if mt == "catboost":
        return str(
            (model_cfg.get("catboost_params") or {}).get("loss_function") or ""
        ).strip() or None
    return None


def _sample_weight_requested(cfg: Mapping[str, Any]) -> bool:
    sw = cfg.get("sample_weight")
    if not isinstance(sw, Mapping):
        return False
    try:
        half_life = float(sw.get("time_decay_half_life_days") or 0)
    except (TypeError, ValueError):
        half_life = 0.0
    return bool(sw.get("tradeable_mask") or sw.get("st") or half_life > 0)


def load_target_contract(cfg: Mapping[str, Any]) -> TargetContract:
    """从 config 读出/合成契约；新路径严格校验，legacy 合成。"""
    label = _cfg_label(cfg)
    target_id = str(label.get("target_id") or "").strip()
    if not target_id:
        spec = spec_from_label_fields(label, market=_cfg_market(cfg))
        digest = spec.spec_hash()
        return TargetContract(
            spec=spec,
            legacy=True,
            status="legacy",
            base_hash=digest,
            effective_hash=digest,
            contract_version=1,
        )

    version = int(label.get("label_contract_version") or 1)
    if version not in SUPPORTED_CONTRACT_VERSIONS:
        raise TargetContractError(
            f"不支持的 label_contract_version={version}；"
            f"当前镜像支持 {sorted(SUPPORTED_CONTRACT_VERSIONS)}",
            code="target_contract_version_unsupported",
        )
    raw_spec = label.get("target_spec")
    if not isinstance(raw_spec, Mapping):
        raise TargetContractError(
            "label.target_id 存在但缺少 target_spec（config 被截断/手改？）",
            code="target_spec_missing",
        )
    try:
        spec = spec_from_dict(raw_spec)
    except TargetSpecError as exc:
        raise TargetContractError(
            f"{exc.code}: {exc.message}", code=exc.code
        ) from exc

    expected_hash = str(label.get("target_hash") or "").strip()
    actual_hash = spec.spec_hash()
    if not expected_hash or expected_hash != actual_hash:
        raise TargetContractError(
            f"target_hash 不一致：config={expected_hash or '(empty)'} "
            f"computed={actual_hash}",
            code="target_spec_hash_mismatch",
        )
    if spec.target_id != target_id:
        raise TargetContractError(
            f"label.target_id={target_id} 与 target_spec.target_id={spec.target_id} 不一致",
            code="target_spec_field_mismatch",
        )
    if int(label.get("target_version") or 0) != spec.version:
        raise TargetContractError(
            "label.target_version 与 target_spec.version 不一致",
            code="target_spec_field_mismatch",
        )
    mismatch = label_fields_match(spec, label)
    if mismatch:
        raise TargetContractError(
            f"label 平铺字段与 target_spec 不一致：{', '.join(mismatch)}",
            code="target_spec_field_mismatch",
        )
    if "=" in str(label.get("label_formula") or ""):
        raise TargetContractError(
            "存在 target_spec 时禁止使用 label_formula 覆盖通道（%=...）",
            code="target_formula_override_forbidden",
        )
    return TargetContract(
        spec=spec,
        legacy=False,
        status=str(label.get("target_status") or "active"),
        base_hash=str(label.get("target_base_hash") or expected_hash),
        effective_hash=actual_hash,
        contract_version=version,
    )


def validate_training_target_contract(
    cfg: Mapping[str, Any],
    model_type: str,
    model_types: list[str] | None = None,
) -> TargetContract:
    """校验 config 与模型能力；新路径失败即 abort，legacy 只告警能力冲突。"""
    contract = load_target_contract(cfg)
    model_cfg = _cfg_model(cfg)
    names = list(model_types or []) or [model_type or model_cfg.get("type") or "lightgbm"]
    try:
        validate_target_model_combo(
            names,
            target_mode=contract.target_mode,
            prediction_mode=str(model_cfg.get("prediction_mode") or "point"),
            market=_cfg_market(cfg),
            sample_weight=_sample_weight_requested(cfg),
            objective=_extract_objective(str(names[0]).lower(), model_cfg),
        )
    except (CapabilityError, ObjectiveConflictError) as exc:
        if contract.legacy:
            import logging

            logging.getLogger("quantmind.train").warning(
                "legacy target capability warning: %s: %s", exc.code, exc.message
            )
        else:
            raise TargetContractError(
                f"{exc.code}: {exc.message}", code=exc.code
            ) from exc
    return contract


__all__ = [
    "SUPPORTED_CONTRACT_VERSIONS",
    "TargetContract",
    "TargetContractError",
    "load_target_contract",
    "validate_training_target_contract",
]
