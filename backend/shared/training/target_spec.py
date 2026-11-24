"""训练目标定义（LabelTargetSpec）：字段契约、规范化哈希、覆盖白名单。

机构级约束：
- Spec 一旦落库即不可变；任何字段变化必须升 version（DB 触发器兜底）。
- hash 只由本模块的 canonical JSON 生成；容器收到 spec 后必须重新校验，
  不信任 DB/文件里的 hash 文本。
- P1 只支持三种 family：forward_return / classification_binary / quantile_return；
  分类阈值、中性化、成本、事件障碍等 P2 字段不提前占位，避免“字段存在但
  未实现”的静默陷阱。

本模块保持纯函数：不连 DB、不 import 训练重依赖。
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator

Market = Literal["CN", "HK", "US", "CRYPTO", "FUTURES", "CUSTOM"]
Family = Literal["forward_return", "classification_binary", "quantile_return"]
Mode = Literal["regression", "classification", "quantile"]
TransformType = Literal[
    "rank",
    "zscore",
    "raw",
    "top_tail",
    "size_excess_rank",
    "size_excess_zscore",
]

FAMILY_MODE: dict[str, str] = {
    "forward_return": "regression",
    "classification_binary": "classification",
    "quantile_return": "quantile",
}

#: 与训练容器 `label.*` 平铺字段一一对应的族；quantile 在容器里仍以
#: target_mode=return + prediction_mode=quantile 表达。
FAMILY_TARGET_MODE: dict[str, str] = {
    "forward_return": "return",
    "classification_binary": "classification",
    "quantile_return": "return",
}

#: 覆盖白名单（扁平键）。entry/exit 是治理锁，不允许覆盖。
OVERRIDE_WHITELIST: dict[str, frozenset[str]] = {
    "forward_return": frozenset(
        {
            "horizon_days",
            "label_transform",
            "label_top_pct",
            "label_drop_extreme_pct",
        }
    ),
    "classification_binary": frozenset({"horizon_days"}),
    "quantile_return": frozenset(),
}

_TARGET_ID_RE = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
_DEFAULT_TOP_PCT = 0.2


class TargetSpecError(ValueError):
    """目标定义非法。code 是稳定机器码（API/容器沿用）。"""

    code = "target_invalid"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code


class EntrySpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: P1 治理锁：A 股 T+1；任何非 1 值都是前视风险，注册表直接拒绝。
    lag_days: int = 1
    price: Literal["open", "close"] = "open"


class ExitSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: P1 只支持收盘出场；开盘出场保留给后续版本。
    price: Literal["close"] = "close"


class TransformSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: TransformType = "rank"
    top_pct: float = Field(default=_DEFAULT_TOP_PCT, ge=0.05, le=0.5)
    drop_extreme_pct: float = Field(default=0.0, ge=0.0, le=0.2)


class LabelTargetSpec(BaseModel):
    """一个不可变的目标定义。`version` 属于身份，hash 覆盖全部字段。"""

    model_config = ConfigDict(extra="forbid")

    target_id: str
    #: 0 仅用于 legacy 平铺请求的运行时合成；落库必须 >=1（DB CHECK）。
    version: int = Field(ge=0)
    market: Market
    family: Family
    mode: Mode
    horizon_days: int = Field(ge=1, le=30)
    entry: EntrySpec = Field(default_factory=EntrySpec)
    exit: ExitSpec = Field(default_factory=ExitSpec)
    transform: TransformSpec = Field(default_factory=TransformSpec)
    label_contract_version: int = 1
    description: str = Field(default="", max_length=500)

    @field_validator("market", mode="before")
    @classmethod
    def _market_upper(cls, v):
        return str(v or "").strip().upper()

    # ── 派生 ─────────────────────────────────────────────────────────────
    def to_label_fields(self) -> dict[str, Any]:
        """转成训练容器 `label` 块沿用的一组平铺字段。"""
        return {
            "target_horizon_days": int(self.horizon_days),
            "target_mode": FAMILY_TARGET_MODE[self.family],
            "execution_price": self.entry.price,
            "label_transform": self.transform.type,
            "label_top_pct": float(self.transform.top_pct),
            "label_drop_extreme_pct": float(self.transform.drop_extreme_pct),
        }

    @property
    def needs_quantile_prediction(self) -> bool:
        return self.family == "quantile_return"

    def canonical_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    def spec_hash(self) -> str:
        return compute_spec_hash(self.canonical_dict())


def validate_spec_semantics(spec: LabelTargetSpec) -> None:
    """跨字段语义校验（独立于 pydantic，保证错误码不被 ValidationError 吞掉）。"""
    if not _TARGET_ID_RE.match(spec.target_id):
        raise TargetSpecError(
            "target_id 必须匹配 ^[a-z][a-z0-9_]{2,63}$",
            code="target_id_invalid",
        )
    expected = FAMILY_MODE[spec.family]
    if spec.mode != expected:
        raise TargetSpecError(
            f"family={spec.family} 要求 mode={expected}，当前 mode={spec.mode}",
            code="target_family_mode_mismatch",
        )
    if int(spec.entry.lag_days) != 1:
        raise TargetSpecError(
            "entry.lag_days 在 P1 只能为 1（A 股 T+1 治理锁）",
            code="target_execution_lag_locked",
        )
    transform = spec.transform
    if transform.type != "top_tail" and transform.top_pct != _DEFAULT_TOP_PCT:
        raise TargetSpecError(
            "label_top_pct 只对 transform.type=top_tail 生效；"
            f"其它变换必须保持默认 {_DEFAULT_TOP_PCT}",
            code="target_top_pct_without_top_tail",
        )
    if transform.drop_extreme_pct > 0 and transform.type not in {
        "rank",
        "zscore",
        "raw",
    }:
        raise TargetSpecError(
            "label_drop_extreme_pct>0 只允许 transform.type 为 rank/zscore/raw；"
            f"当前为 {transform.type}（语义未定义，拒绝而非静默忽略）",
            code="target_drop_extreme_unsupported_transform",
        )
    if spec.family == "classification_binary":
        if transform.type != "rank":
            raise TargetSpecError(
                "classification_binary 只允许 transform.type=rank（训练端跳过变换）",
                code="target_classification_transform_unsupported",
            )
        if transform.drop_extreme_pct != 0:
            raise TargetSpecError(
                "classification_binary 不允许 label_drop_extreme_pct>0",
                code="target_drop_extreme_unsupported_transform",
            )
    if spec.family == "quantile_return" and transform.type != "raw":
        raise TargetSpecError(
            "quantile_return 要求 transform.type=raw（分位回归直接吃原始收益）",
            code="target_quantile_transform_unsupported",
        )


def compute_spec_hash(spec_dict: Mapping[str, Any]) -> str:
    """canonical JSON（排序键、无空格、无 NaN）→ sha256。"""
    canonical = json.dumps(
        dict(spec_dict),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def spec_from_dict(raw: Mapping[str, Any]) -> LabelTargetSpec:
    try:
        spec = LabelTargetSpec.model_validate(dict(raw))
    except Exception as exc:  # pydantic ValidationError（类型/范围）
        raise TargetSpecError(f"目标定义非法: {exc}", code="target_invalid") from exc
    validate_spec_semantics(spec)
    return spec


def spec_from_label_fields(
    fields: Mapping[str, Any],
    *,
    market: str = "CN",
    target_id: str = "legacy",
    version: int = 0,
) -> LabelTargetSpec:
    """从旧平铺字段合成 spec（legacy 请求零行为变化的桥）。

    旧口径默认 execution_price=close；这里必须以字段实际值为准，不能用
    EntrySpec 的 open 默认值，否则 legacy 标签会悄悄从收盘买变成开盘买。
    """
    mode = str(fields.get("target_mode") or "return").strip().lower()
    family: Family = (
        "classification_binary" if mode == "classification" else "forward_return"
    )
    transform = str(fields.get("label_transform") or "rank").strip().lower()
    if family == "classification_binary":
        # 训练容器在分类分支跳过变换；保留 rank 以维持旧 config 键值。
        transform = "rank"
    spec = LabelTargetSpec(
        target_id=target_id,
        version=version,
        market=market,
        family=family,
        mode=FAMILY_MODE[family],
        horizon_days=int(fields.get("target_horizon_days") or 1),
        entry=EntrySpec(
            lag_days=1,
            price=str(fields.get("execution_price") or "close").strip().lower(),
        ),
        exit=ExitSpec(price="close"),
        transform=TransformSpec(
            type=transform,
            top_pct=float(fields.get("label_top_pct") or _DEFAULT_TOP_PCT),
            drop_extreme_pct=float(fields.get("label_drop_extreme_pct") or 0.0),
        ),
        description="legacy payload synthesized at submit time",
    )
    validate_spec_semantics(spec)
    return spec


def apply_overrides(
    spec: LabelTargetSpec, overrides: Mapping[str, Any] | None
) -> LabelTargetSpec:
    """按 family 白名单覆盖允许字段，并重新走完整校验。"""
    if not overrides:
        return spec
    if not isinstance(overrides, Mapping):
        raise TargetSpecError("label_overrides 必须是对象", code="target_override_invalid")
    allowed = OVERRIDE_WHITELIST[spec.family]
    unknown = set(overrides) - allowed
    if unknown:
        raise TargetSpecError(
            f"family={spec.family} 不允许覆盖字段：{', '.join(sorted(unknown))}；"
            f"允许：{', '.join(sorted(allowed)) or '（无）'}",
            code="target_override_not_allowed",
        )

    data = spec.model_dump(mode="python")
    if "horizon_days" in overrides:
        data["horizon_days"] = overrides["horizon_days"]
    if "label_transform" in overrides:
        data["transform"]["type"] = str(overrides["label_transform"]).strip().lower()
    if "label_top_pct" in overrides:
        data["transform"]["top_pct"] = overrides["label_top_pct"]
    if "label_drop_extreme_pct" in overrides:
        data["transform"]["drop_extreme_pct"] = overrides["label_drop_extreme_pct"]
    return spec_from_dict(data)


def label_fields_match(spec: LabelTargetSpec, fields: Mapping[str, Any]) -> list[str]:
    """返回不一致的平铺字段名（容器自校验用；空列表=一致）。"""
    expected = spec.to_label_fields()
    bad: list[str] = []
    for key, value in expected.items():
        if key not in fields:
            bad.append(key)
            continue
        actual = fields[key]
        if isinstance(value, float):
            try:
                if abs(float(actual) - value) > 1e-12:
                    bad.append(key)
            except (TypeError, ValueError):
                bad.append(key)
        elif isinstance(value, int):
            try:
                if int(actual) != value:
                    bad.append(key)
            except (TypeError, ValueError):
                bad.append(key)
        elif str(actual) != str(value):
            bad.append(key)
    return bad


__all__ = [
    "EntrySpec",
    "ExitSpec",
    "FAMILY_MODE",
    "FAMILY_TARGET_MODE",
    "Family",
    "LabelTargetSpec",
    "Market",
    "OVERRIDE_WHITELIST",
    "TargetSpecError",
    "TransformSpec",
    "apply_overrides",
    "compute_spec_hash",
    "label_fields_match",
    "spec_from_dict",
    "spec_from_label_fields",
    "validate_spec_semantics",
]
