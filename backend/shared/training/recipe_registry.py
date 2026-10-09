"""训练配方注册表（P1 · 设计文档《滚动训练与模型生命周期》§4.4/§4.8）。

配方 = 「一个可被月度滚动重复执行的模型语义快照」：模型类型、特征清单、
超参、预处理、因子源、窗口策略。派发时由本模块把它与窗口（六键 split）和
``rolling_meta`` 合成完整训练请求 payload —— 配方文件里**禁止**出现这两样，
防止模板里夹带过期窗口（注册表校验强制）。

``recipe_hash`` = 配方语义（payload 模板 + 窗口策略 + 市场/因子源）的 sha1，
写进 ``rolling_meta`` 并入库 campaign —— 跨窗口比对该值即可判断「中途换过
配方」，模型与训练数据的可追溯锚点。
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .rolling_window import VALID_MODES, RollingWindow, WindowPolicy

RECIPE_DIR = Path(__file__).resolve().parent / "recipes"

_REQUIRED_TOP_KEYS = (
    "recipe_id",
    "market",
    "factor_market",
    "factor_source",
    "target_horizon_days",
    "window_policy",
    "payload",
)

#: 派发时注入的 payload 键 —— 配方模板里出现即为配置漂移，直接拒绝装载。
#: ``wfa`` 同属「窗口类」配置：滚动重训的窗口由 rolling_window 单独计算，
#: 模板若自带走查段会与注入的六键 split 打架（DL 配方本就不做 WFA）。
_FORBIDDEN_PAYLOAD_KEYS = (
    "rolling_meta",
    "wfa",
    "train_start",
    "train_end",
    "valid_start",
    "valid_end",
    "test_start",
    "test_end",
)


class RecipeError(ValueError):
    """配方文件缺失 / 结构非法。调度与端点捕获后跳过本轮并告警。"""


@dataclass(frozen=True)
class Recipe:
    recipe_id: str
    market: str
    calendar_market: str
    factor_market: str
    factor_source: str
    target_horizon_days: int
    window_policy: WindowPolicy
    payload: dict[str, Any] = field(repr=False)
    description: str = ""

    def semantic_dict(self) -> dict[str, Any]:
        """参与 recipe_hash 的语义子集（description 等展示字段不计入）。"""
        return {
            "recipe_id": self.recipe_id,
            "market": self.market,
            "calendar_market": self.calendar_market,
            "factor_market": self.factor_market,
            "factor_source": self.factor_source,
            "target_horizon_days": self.target_horizon_days,
            "window_policy": self.window_policy.to_dict(),
            "payload": self.payload,
        }


def recipe_hash(recipe: Recipe) -> str:
    """配方语义 sha1（canonical JSON，键序无关）。"""
    canonical = json.dumps(
        recipe.semantic_dict(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RecipeError(message)


def _strict_policy(recipe_id: str, raw: Any) -> WindowPolicy:
    """配方里的 window_policy 走**严格**校验（与 Redis 配置的宽松回退相反：
    配方是随代码进 git 的资产，坏值应立刻炸出来而不是静默用默认档）。"""
    _require(isinstance(raw, dict), f"配方 {recipe_id}: window_policy 必须是对象")
    for key in ("train_days", "valid_days", "test_days"):
        if key in raw and raw[key] is not None:
            value = raw[key]
            _require(
                isinstance(value, int) and not isinstance(value, bool) and value > 0,
                f"配方 {recipe_id}: window_policy.{key} 必须是正整数",
            )
    mode = raw.get("mode")
    if mode is not None:
        _require(
            str(mode).lower() in VALID_MODES,
            f"配方 {recipe_id}: window_policy.mode 仅支持 {VALID_MODES}",
        )
    purge = raw.get("purge_days")
    if purge is not None:
        _require(
            isinstance(purge, int) and not isinstance(purge, bool) and purge >= 0,
            f"配方 {recipe_id}: window_policy.purge_days 必须是非负整数或 null",
        )
    return WindowPolicy.from_dict(raw)


def validate_recipe(data: Any) -> Recipe:
    """结构校验 + 规范化。测试可直接喂 dict。"""
    _require(isinstance(data, dict), "配方必须是 JSON 对象")
    for key in _REQUIRED_TOP_KEYS:
        _require(key in data, f"配方缺少必填字段: {key}")
    recipe_id = str(data["recipe_id"]).strip()
    _require(bool(recipe_id), "配方 recipe_id 不能为空")

    horizon = data["target_horizon_days"]
    _require(
        isinstance(horizon, int) and not isinstance(horizon, bool) and horizon > 0,
        f"配方 {recipe_id}: target_horizon_days 必须是正整数",
    )

    payload = data["payload"]
    _require(isinstance(payload, dict), f"配方 {recipe_id}: payload 必须是对象")
    for key in _FORBIDDEN_PAYLOAD_KEYS:
        _require(
            key not in payload,
            f"配方 {recipe_id}: payload 不得携带派发时注入的键 `{key}`",
        )
    _require(bool(payload.get("model_type")), f"配方 {recipe_id}: payload.model_type 必填")
    features = payload.get("features")
    _require(
        isinstance(features, list) and len(features) > 0,
        f"配方 {recipe_id}: payload.features 必须是非空列表",
    )
    _require(
        str(payload.get("factor_source") or "") == str(data["factor_source"]),
        f"配方 {recipe_id}: payload.factor_source 与顶层 factor_source 不一致",
    )

    policy = _strict_policy(recipe_id, data["window_policy"])
    return Recipe(
        recipe_id=recipe_id,
        market=str(data["market"]).strip().upper(),
        calendar_market=str(data.get("calendar_market") or data["market"]).strip().upper(),
        factor_market=str(data["factor_market"]).strip().upper(),
        factor_source=str(data["factor_source"]).strip(),
        target_horizon_days=int(horizon),
        window_policy=policy,
        payload=copy.deepcopy(payload),
        description=str(data.get("description") or ""),
    )


def load_recipe(recipe_id: str) -> Recipe:
    path = RECIPE_DIR / f"{recipe_id}.json"
    if not path.is_file():
        raise RecipeError(f"配方不存在: {recipe_id}（{path}）")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RecipeError(f"配方 {recipe_id} JSON 解析失败: {exc}") from exc
    recipe = validate_recipe(data)
    if recipe.recipe_id != recipe_id:
        raise RecipeError(
            f"配方文件名与 recipe_id 不一致: {recipe_id} != {recipe.recipe_id}"
        )
    return recipe


def list_recipes() -> list[dict[str, Any]]:
    """配置文件目录里的全部配方摘要（供 /recipes 只读端点）。"""
    out: list[dict[str, Any]] = []
    if not RECIPE_DIR.is_dir():
        return out
    for path in sorted(RECIPE_DIR.glob("*.json")):
        try:
            recipe = load_recipe(path.stem)
        except RecipeError as exc:  # 坏配方不拖垮列表，如实报告
            out.append({"recipe_id": path.stem, "valid": False, "error": str(exc)})
            continue
        out.append(
            {
                "recipe_id": recipe.recipe_id,
                "valid": True,
                "market": recipe.market,
                "calendar_market": recipe.calendar_market,
                "factor_market": recipe.factor_market,
                "factor_source": recipe.factor_source,
                "target_horizon_days": recipe.target_horizon_days,
                "window_policy": recipe.window_policy.to_dict(),
                "recipe_hash": recipe_hash(recipe),
                "description": recipe.description,
            }
        )
    return out


def build_rolling_meta(
    recipe: Recipe,
    campaign_id: str,
    window: RollingWindow,
    dispatched_by: str,
) -> dict[str, Any]:
    """``rolling_meta``（设计 §4.7 白名单成员，随 payload 全链路留存）。"""
    return {
        "campaign_id": campaign_id,
        "window_index": window.window_index,
        "anchor_date": window.anchor_date.isoformat(),
        "recipe_hash": recipe_hash(recipe),
        "purge_days": window.purge_days,
        "dispatched_by": dispatched_by,
    }


#: ``rolling_meta`` 的合法键 —— 归一化时唯一保留集（与 build_rolling_meta 对齐）。
ROLLING_META_KEYS = (
    "campaign_id",
    "window_index",
    "anchor_date",
    "recipe_hash",
    "purge_days",
    "dispatched_by",
)


def sanitize_rolling_meta(raw: Any) -> dict[str, Any] | None:
    """请求入参 → 白名单拷贝；非对象或过滤后为空时返回 None。

    公开训练端点也接受 ``rolling_meta`` 字段，这里做键白名单过滤，
    防止请求方夹带任意负载进 config.yaml / metadata.json（台账键集合恒定）。
    """
    if not isinstance(raw, dict):
        return None
    filtered = {key: raw[key] for key in ROLLING_META_KEYS if key in raw}
    return filtered or None


def build_training_payload(
    recipe: Recipe,
    window: RollingWindow,
    campaign_id: str,
    dispatched_by: str,
) -> dict[str, Any]:
    """配方模板 + 六键窗口 + rolling_meta → 完整训练请求 payload。

    模板深拷贝后合成，绝不改 recipe 本体（冻结资产）。
    """
    payload = copy.deepcopy(recipe.payload)
    payload.update(window.to_split_fields())
    payload["rolling_meta"] = build_rolling_meta(recipe, campaign_id, window, dispatched_by)
    return payload
