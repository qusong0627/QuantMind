"""训练 run 对比（WP6）：同口径严格评估 + 配置 diff + 单轴判定。

纯函数集中在这里，路由只负责取数与组装，便于单测。
"""

from __future__ import annotations

from typing import Any

# 影响「模型是什么」的字段；展示/运行时/血缘字段不参与 diff（否则每次提交都不同）
EXPERIMENT_KEYS = (
    "train_start",
    "train_end",
    "valid_start",
    "valid_end",
    "test_start",
    "test_end",
    "target_horizon_days",
    "target_mode",
    "execution_price",
    "label_transform",
    "label_top_pct",
    "label_drop_extreme_pct",
    "model_type",
    "model_types",
    "ensemble",
    "prediction_mode",
    "lgb_params",
    "xgb_params",
    "catboost_params",
    "dl_params",
    "features",
    "factor_source",
    "factor_catalog_versions",
    "factor_field_sources",
    "factor_selection",
    "auto_feature_filter",
    "preprocessing",
    "sample_weight",
)

_PARAM_KEYS = {"lgb_params", "xgb_params", "catboost_params", "dl_params"}
# 特征「来源修复」（裸名→库:列 / 换目录版本）与特征清单是同一个实验轴：
# 2026-11-19 tdx 修复就是 features+field_sources+catalog_versions 一起变，
# 拆成三轴会把一次单轴修复误判成多轴。
_FEATURE_AXIS_KEYS = {
    "features",
    "factor_source",
    "factor_field_sources",
    "factor_catalog_versions",
}
_VOLATILE_IN_LISTS = {"generated_at"}


def canonical_config(payload: dict[str, Any] | None) -> dict[str, Any]:
    payload = payload or {}
    out: dict[str, Any] = {}
    for key in EXPERIMENT_KEYS:
        if key not in payload:
            continue
        value = payload.get(key)
        if key == "features" and isinstance(value, list):
            out[key] = sorted(str(x) for x in value)
        elif key == "factor_field_sources" and isinstance(value, dict):
            out[key] = {str(k): str(v) for k, v in sorted(value.items())}
        elif key == "factor_catalog_versions" and isinstance(value, dict):
            out[key] = {str(k): str(v) for k, v in sorted(value.items())}
        elif isinstance(value, dict):
            out[key] = {str(k): v for k, v in sorted(value.items()) if k not in _VOLATILE_IN_LISTS}
        else:
            out[key] = value
    return out


def _flatten(value: Any, prefix: str, out: dict[str, Any]) -> None:
    if isinstance(value, dict) and value:
        for key, child in value.items():
            _flatten(child, f"{prefix}.{key}", out)
    else:
        out[prefix] = value


def config_diff(
    baseline: dict[str, Any], candidate: dict[str, Any]
) -> list[dict[str, Any]]:
    """返回逐字段差异（path / baseline / candidate），列表字段整体比较。"""
    left: dict[str, Any] = {}
    right: dict[str, Any] = {}
    for key in EXPERIMENT_KEYS:
        if key in baseline or key in candidate:
            _flatten(baseline.get(key, "<absent>"), key, left)
            _flatten(candidate.get(key, "<absent>"), key, right)
    out: list[dict[str, Any]] = []
    for path in sorted(set(left) | set(right)):
        a = left.get(path, "<absent>")
        b = right.get(path, "<absent>")
        if a != b:
            out.append({"path": path, "baseline": a, "candidate": b})
    return out


def axis_of(path: str) -> str:
    top = path.split(".", 1)[0]
    if top in _PARAM_KEYS:
        return "model_params"
    if top in _FEATURE_AXIS_KEYS:
        return "features"
    return top


def diff_axes(diffs: list[dict[str, Any]]) -> list[str]:
    return sorted({axis_of(str(d.get("path") or "")) for d in diffs})


def is_single_axis(diffs: list[dict[str, Any]]) -> bool:
    return len(diff_axes(diffs)) == 1


def summarize_strict(report_row: dict[str, Any] | None) -> dict[str, Any]:
    """从 strict report 行提取对比用的 headline（测试段 Top20 / 25bp）。"""
    if not report_row:
        return {"status": "missing"}
    report = report_row.get("report") or {}
    results = report.get("results") or {}
    segment = results.get("test") or (list(results.values())[-1] if results else {})
    top20 = (segment or {}).get("top20") or {}
    cost = top20.get("cost_25bp") or {}
    segments = report.get("segments") or {}
    return {
        "status": report_row.get("status"),
        "segment": "test" if "test" in results else (list(results)[-1] if results else ""),
        "window": segments.get("test") if "test" in segments else (list(segments.values())[-1] if segments else None),
        "batches": top20.get("batches"),
        "total_25bp": cost.get("total"),
        "mdd": cost.get("mdd"),
        "excess_hs300": cost.get("excess_hs300"),
        "hac_t": cost.get("hac_t"),
        "psr": cost.get("psr"),
        "turnover_per_batch": top20.get("turnover_per_batch"),
        "error": report_row.get("error") or "",
    }
