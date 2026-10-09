"""模型 → 滚动训练配方派生（模型管理页「滚动训练」派生面）。

云端导入（模型中心下载）与本地训练的模型包都带 ``metadata.json``（描述语义）
与 ``config.yaml``（完整训练配置）——本模块把两者读成一份**标准配方 dict**
（``recipe_registry.validate_recipe`` 可装载），落盘交给 ``save_user_recipe``。
派生配方与内建配方在派发链路上完全同权：``rolling_dispatch`` 侧零改动。

推导纪律（宁可不产出，不产一个派发即 422 的假配方）：
- 缺 features / 可训练 model_type / factor_source / factor_catalog_version
  任一 → ``RecipeError``（配置面拒绝生成）；
- 缺 ``config.yaml`` 的云端包：尽力从 ``metadata.json`` 推导 + warnings 明示降级；
- ``config.yaml`` 的 split 六键段**显式丢弃**——窗口由 rolling_window 在派发时
  注入，配方模板携带会被注册表校验直接拒绝；
- ``deploy_to_production`` 恒 False：派生只进训练，上线必须走晋升治理；
- ``auto_feature_filter`` 恒 ``"false"``：防止无 factor_selection 的包被训练侧
  默认裁成 top-80 特征（静默变更特征空间是跨窗口不可比的头号来源）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from ..utc_datetime import to_utc_iso, utc_now
from .recipe_registry import Recipe, RecipeError, validate_recipe
from .rolling_window import (
    DEFAULT_MODE,
    DEFAULT_TEST_DAYS,
    DEFAULT_TRAIN_DAYS,
    DEFAULT_VALID_DAYS,
)

METADATA_FILE = "metadata.json"
CONFIG_FILE = "config.yaml"

#: model_id 会拼进 recipe_id（`model_{model_id}`）→ 同一安全字符集守卫（防穿越）。
_SAFE_MODEL_ID = re.compile(r"[A-Za-z0-9._-]{1,128}")

#: 云端导入模型 id 惯例 ``mdl_{market}_...`` —— metadata/config 都缺市场时的兜底。
_MODEL_MARKET_PREFIX = re.compile(r"^mdl_([a-z]+)_")

_WINDOW_POLICY_KEYS = ("train_days", "valid_days", "test_days", "mode", "purge_days")

DEFAULT_TARGET_HORIZON_DAYS = 5
DEFAULT_MAX_TIME_MINUTES = 240


@dataclass(frozen=True)
class ModelRecipeResult:
    """派生结果：配方本体 + 可落盘 dict + 降级告警 + 溯源。"""

    recipe: Recipe
    recipe_dict: dict[str, Any]
    warnings: list[str]
    source_model_id: str
    source_files: list[str]


def _dig(raw: Any, *keys: str) -> Any:
    """从嵌套 dict 里安全取值；任一层不是 dict 返回 None。"""
    node = raw
    for key in keys:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def _read_metadata(model_dir: Path, warnings: list[str]) -> dict[str, Any] | None:
    path = model_dir / METADATA_FILE
    if _refuse_symlink(path):
        raise RecipeError(
            f"模型包 {METADATA_FILE} 是符号链接，拒绝读取"
            "（模型包文件不得用链接绕过目录边界）"
        )
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        warnings.append(f"{METADATA_FILE} 解析失败（{exc}），按缺失处理")
        return None
    if not isinstance(data, dict):
        warnings.append(f"{METADATA_FILE} 内容不是对象，按缺失处理")
        return None
    return data


def _read_config(model_dir: Path, warnings: list[str]) -> dict[str, Any] | None:
    path = model_dir / CONFIG_FILE
    if _refuse_symlink(path):
        raise RecipeError(
            f"模型包 {CONFIG_FILE} 是符号链接，拒绝读取"
            "（模型包文件不得用链接绕过目录边界）"
        )
    if not path.is_file():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        warnings.append(f"{CONFIG_FILE} 解析失败（{exc}），超参按默认值降级")
        return None
    if not isinstance(data, dict) or not data:
        warnings.append(f"{CONFIG_FILE} 内容为空或不是对象，超参按默认值降级")
        return None
    return data


def _refuse_symlink(path: Path) -> bool:
    """模型包文件禁止符号链接：目录级越界守卫在 router 层，文件级链接逃逸在此拦。"""
    return path.is_symlink()


def _canonical_market_of(raw: Any) -> str | None:
    """平台别名表（model_registry._MARKET_ALIASES）→ 规范市场；不可识别返回 None。

    必须走平台唯一别名表：'hong_kong'/'a_share'/'美股' 这类写法若只做 upper()
    会得到 'HONG_KONG'，到因子读取/特征挂载处静默坍缩成 CN——「HK 配方训练了
    A 股数据」的来源（该表与 SQL 侧 qm_market_of 逐字等价，不得在此另立映射）。
    """
    from backend.shared.model_registry import _MARKET_ALIASES

    value = str(raw or "").strip().upper()
    if not value:
        return None
    for canonical, hits in _MARKET_ALIASES.items():
        if value in hits:
            return canonical
    return None


#: 因子面市场允许的规范值：规范市场 + CUSTOM（内建 cn_nativetft_base 同款——
#: 自定义因子集在 context.market 记 CUSTOM，不是未知值）。
_FACTOR_MARKET_ALLOWED = ("CN", "HK", "US", "CRYPTO", "FUTURES", "CUSTOM")


def _resolve_market(
    model_id: str,
    metadata: dict[str, Any],
    config: dict[str, Any] | None,
    warnings: list[str],
) -> str:
    """配方市场：metadata.market（别名规范化）→ model_id 前缀 → benchmark 推断/CN。"""
    raw = metadata.get("market")
    if not raw:
        match = _MODEL_MARKET_PREFIX.match(model_id)
        if match:
            raw = match.group(1)
    canonical = _canonical_market_of(raw)
    if canonical:
        return canonical
    if str(raw or "").strip().upper() == "CUSTOM":
        return "CUSTOM"
    benchmark = str(
        _dig(metadata, "context", "benchmark")
        or _dig(config, "context", "benchmark")
        or ""
    )
    # training 入参层的市场解析（api 侧模块，惰性导入回避 fastapi 依赖扩散）
    from backend.shared.training.request import resolve_market

    resolved = resolve_market(raw, benchmark)
    if raw:
        warnings.append(
            f"市场标识 {raw!r} 不在平台别名表内，已按基准/默认推断为 {resolved}"
        )
    return resolved


def _resolve_factor_market(
    metadata: dict[str, Any], config: dict[str, Any] | None, fallback: str
) -> str:
    """因子面市场：config.context.market → metadata.context.market → 配方市场。

    识别不了的值**拒绝派生**而不是 upper() 后放行：非规范值会在因子读取/
    特征挂载处静默坍缩成 CN（假配方比没有配方更糟）。
    """
    for ctx in (_dig(config, "context"), _dig(metadata, "context")):
        if isinstance(ctx, dict) and ctx.get("market"):
            raw = str(ctx["market"]).strip()
            canonical = _canonical_market_of(raw) or (
                raw.upper() if raw.upper() == "CUSTOM" else None
            )
            if canonical is None:
                raise RecipeError(
                    f"无法推导配方：因子面市场标识无法识别: {raw!r}"
                    f"（允许 {_FACTOR_MARKET_ALLOWED} 或其别名）"
                )
            return canonical
    return fallback


def _pick_features(
    metadata: dict[str, Any], config: dict[str, Any] | None
) -> list[str] | None:
    for candidate in (
        metadata.get("features"),
        metadata.get("feature_columns"),
        metadata.get("requested_features"),
        _dig(config, "data", "features"),
    ):
        if isinstance(candidate, list) and candidate:
            return [str(f) for f in candidate]
    return None


def _pick_model_type(metadata: dict[str, Any], config: dict[str, Any] | None) -> str:
    raw = metadata.get("model_type") or _dig(config, "model", "type")
    if not raw:
        raise RecipeError(
            "无法推导配方：模型包缺少模型类型（metadata.json.model_type / config.yaml.model.type）"
        )
    model_type = str(raw).strip().lower()
    from backend.shared.training.request import ALLOWED_MODEL_TYPES

    if model_type not in ALLOWED_MODEL_TYPES:
        raise RecipeError(
            f"无法推导配方：模型类型不可训练: {model_type}"
            f"（允许: {sorted(ALLOWED_MODEL_TYPES)}）"
        )
    return model_type


def _pick_factor_source(metadata: dict[str, Any], config: dict[str, Any] | None) -> str:
    raw = metadata.get("factor_source") or _dig(config, "data", "factor_source")
    if not raw:
        raise RecipeError(
            "无法推导配方：模型包缺少因子源（factor_source）——"
            "无因子面的配方无法派发训练"
        )
    return str(raw).strip()


def _pick_factor_catalog_version(
    metadata: dict[str, Any], config: dict[str, Any] | None
) -> str:
    raw = metadata.get("factor_catalog_version") or _dig(
        config, "data", "factor_catalog_version"
    )
    if not raw:
        raise RecipeError(
            "无法推导配方：模型包缺少因子目录版本（factor_catalog_version）——"
            "未钉版本的因子面会在派发时被 422 拒绝，先补元数据再派生"
        )
    return str(raw).strip()


def _pick_target_horizon(
    metadata: dict[str, Any], config: dict[str, Any] | None, warnings: list[str]
) -> int:
    raw = metadata.get("target_horizon_days")
    if raw is None:
        raw = _dig(config, "label", "target_horizon_days")
    if raw is None:
        warnings.append(
            f"未找到 target_horizon_days（{METADATA_FILE} / {CONFIG_FILE}），"
            f"按默认 {DEFAULT_TARGET_HORIZON_DAYS} 天"
        )
        return DEFAULT_TARGET_HORIZON_DAYS
    try:
        horizon = int(raw)
    except (TypeError, ValueError) as exc:
        raise RecipeError(f"target_horizon_days 非法: {raw!r}") from exc
    if horizon <= 0:
        raise RecipeError(f"target_horizon_days 必须是正整数: {raw!r}")
    return horizon


def _window_policy_dict(
    override: dict[str, Any] | None, recipe_id: str
) -> dict[str, Any]:
    policy: dict[str, Any] = {
        "train_days": DEFAULT_TRAIN_DAYS,
        "valid_days": DEFAULT_VALID_DAYS,
        "test_days": DEFAULT_TEST_DAYS,
        "mode": DEFAULT_MODE,
        "purge_days": None,
    }
    if override is None:
        return policy
    if not isinstance(override, dict):
        raise RecipeError(f"配方 {recipe_id}: window_policy 必须是对象")
    unknown = sorted(set(override) - set(_WINDOW_POLICY_KEYS))
    if unknown:
        raise RecipeError(f"配方 {recipe_id}: window_policy 不支持字段 {unknown}")
    for key in _WINDOW_POLICY_KEYS:
        if key in override:
            policy[key] = override[key]
    return policy


def _build_payload(
    *,
    metadata: dict[str, Any],
    config: dict[str, Any] | None,
    warnings: list[str],
    model_type: str,
    features: list[str],
    factor_source: str,
    factor_catalog_version: str,
    factor_market: str,
    horizon: int,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model_type": model_type,
        "features": features,
        "factor_source": factor_source,
        "factor_catalog_version": factor_catalog_version,
        "target_horizon_days": horizon,
        "target_mode": str(
            metadata.get("target_mode")
            or _dig(config, "label", "target_mode")
            or "return"
        ),
        "label_formula": str(
            metadata.get("label_formula")
            or _dig(config, "label", "label_formula")
            or ""
        ),
        "training_window": str(_dig(config, "label", "training_window") or ""),
        "prediction_mode": str(
            metadata.get("prediction_mode")
            or _dig(config, "model", "prediction_mode")
            or "point"
        ),
        # 派生只进训练（deploy 须走晋升治理）；训练侧自动筛选显式关：
        # 缺 factor_selection 的云端包若被默认裁 top-80，特征空间会静默变化。
        "deploy_to_production": False,
        "auto_feature_filter": "false",
    }

    # context：metadata（迁移后描述）与 config（训练当时配置）并集，config 更权威
    context: dict[str, Any] = {}
    for ctx in (_dig(metadata, "context"), _dig(config, "context")):
        if isinstance(ctx, dict):
            context.update(ctx)
    context["market"] = factor_market
    payload["context"] = context

    max_time = _dig(config, "max_time_minutes")
    payload["max_time_minutes"] = (
        int(max_time)
        if isinstance(max_time, int) and not isinstance(max_time, bool)
        else DEFAULT_MAX_TIME_MINUTES
    )

    for key in ("ensemble", "num_boost_round", "early_stopping_rounds"):
        value = _dig(config, "model", key)
        if value is not None:
            payload[key] = value
    for key in ("dl_params", "xgb_params", "catboost_params"):
        value = _dig(config, "model", key)
        if isinstance(value, dict) and value:
            payload[key] = value
    # 树模型超参在 config 里叫 params，训练入口键名是 lgb_params
    params = _dig(config, "model", "params")
    if isinstance(params, dict) and params:
        payload["lgb_params"] = params
    val_ratio = _dig(config, "model", "val_ratio")
    if val_ratio is not None:
        payload["val_ratio"] = val_ratio

    for key in ("explain", "preprocessing", "factor_selection"):
        value = _dig(config, key)
        if isinstance(value, dict) and value:
            payload[key] = value
    required_artifacts = _dig(config, "output", "required_artifacts")
    if isinstance(required_artifacts, list) and required_artifacts:
        payload["required_artifacts"] = required_artifacts
    return payload


def derive_recipe_from_model(
    model_dir: Path,
    *,
    model_id: str,
    window_policy: dict[str, Any] | None = None,
) -> ModelRecipeResult:
    """从模型目录（metadata.json + config.yaml）推导一份标准滚动训练配方。

    只读模型目录；不落盘（落盘走 ``recipe_registry.save_user_recipe``）。
    """
    if not _SAFE_MODEL_ID.fullmatch(str(model_id or "")):
        raise RecipeError(
            f"非法 model_id: {model_id!r}（仅允许字母数字._-，≤128 字符）"
        )
    model_dir = Path(model_dir)
    if not model_dir.is_dir():
        raise RecipeError(f"模型目录不存在: {model_dir}")

    warnings: list[str] = []
    metadata = _read_metadata(model_dir, warnings)
    config = _read_config(model_dir, warnings)
    source_files: list[str] = []
    if metadata is not None:
        source_files.append(METADATA_FILE)
    if config is not None:
        source_files.append(CONFIG_FILE)
    if not source_files:
        raise RecipeError(
            f"模型目录缺少 {METADATA_FILE} 与 {CONFIG_FILE}，无法推导配方: {model_dir}"
        )
    if metadata is None:
        warnings.append(
            f"缺少 {METADATA_FILE}：市场/标签面按 {CONFIG_FILE} 与模型 id 推断"
        )
    elif config is None:
        warnings.append(
            f"缺少 {CONFIG_FILE}（云端包常见）：超参/预处理/解释器配置仅按 "
            f"{METADATA_FILE} 推导，dl_params 等将按训练侧默认值"
        )

    metadata = metadata or {}
    recipe_id = f"model_{model_id}"
    market = _resolve_market(model_id, metadata, config, warnings)
    factor_market = _resolve_factor_market(metadata, config, market)
    features = _pick_features(metadata, config)
    if not features:
        raise RecipeError(
            f"无法推导配方：模型包缺少特征清单"
            f"（{METADATA_FILE}.features / {CONFIG_FILE}.data.features）"
        )
    model_type = _pick_model_type(metadata, config)
    factor_source = _pick_factor_source(metadata, config)
    factor_catalog_version = _pick_factor_catalog_version(metadata, config)
    horizon = _pick_target_horizon(metadata, config, warnings)

    payload = _build_payload(
        metadata=metadata,
        config=config,
        warnings=warnings,
        model_type=model_type,
        features=features,
        factor_source=factor_source,
        factor_catalog_version=factor_catalog_version,
        factor_market=factor_market,
        horizon=horizon,
    )

    display_name = str(metadata.get("model_name") or "").strip()
    if display_name and display_name != model_id:
        description = f"派生自模型 {model_id}（{display_name}）"
    else:
        description = f"派生自模型 {model_id}"

    recipe_dict: dict[str, Any] = {
        "recipe_id": recipe_id,
        "description": description,
        "market": market,
        "calendar_market": market,
        "factor_market": factor_market,
        "factor_source": factor_source,
        "target_horizon_days": horizon,
        "window_policy": _window_policy_dict(window_policy, recipe_id),
        "payload": payload,
        "source_model_id": model_id,
        "derived_at": to_utc_iso(utc_now()),
    }
    recipe = validate_recipe(recipe_dict)
    return ModelRecipeResult(
        recipe=recipe,
        recipe_dict=recipe_dict,
        warnings=warnings,
        source_model_id=model_id,
        source_files=source_files,
    )
