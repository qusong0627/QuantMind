"""模型特征契约（B3）：晋升 / 切默认前确认训练时的特征源仍然成立。

回答三个问题（全部以 metadata + 当前 QuantDB 源实况为准）：
1. ``factor_source`` 还在、可读吗；
2. 训练用的每个特征（含跨库 ``库:列``）当前源里都还有吗；
3. 训练记录的 ``factor_schema_hash`` 与当前源一致吗（列名集合，不是数据）。

非 QuantDB 因子模型（没有 ``factor_source``，如旧 parquet 模型）直接跳过，
不改变既有行为。阻断默认 fail-closed；只有显式 ``allow_schema_drift`` 才把
schema 漂移降级为 warning（调用方必须把理由写进审计）。
"""

from __future__ import annotations

import json
import time
from datetime import date
from pathlib import Path
from typing import Any

from backend.services.engine.data_platform.quantdb_factor_reader import (
    QuantDBFactorReader,
    split_features_by_availability,
)

#: describe 结果进程内缓存（同一模型/市场的重复晋升检查不反复扫 parquet）
_DESCRIBE_TTL_SECONDS = 60.0
_DESCRIBE_CACHE: dict[tuple[str, str], tuple[float, Any]] = {}

#: 模型数据截止日超过该天数 → warning（不是契约问题，是新鲜度提示）
_STALE_MODEL_DAYS = 90


def _parse_metadata(metadata: Any) -> dict[str, Any]:
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except ValueError:
            return {}
    return metadata if isinstance(metadata, dict) else {}


def _describe(reader: QuantDBFactorReader, source: str) -> Any:
    key = (str(getattr(reader, "data_dir", "")), source)
    now = time.monotonic()
    hit = _DESCRIBE_CACHE.get(key)
    if hit and now - hit[0] < _DESCRIBE_TTL_SECONDS:
        return hit[1]
    status = reader.describe(source)
    _DESCRIBE_CACHE[key] = (now, status)
    return status


def clear_contract_cache() -> None:
    """测试/热修后清缓存。"""
    _DESCRIBE_CACHE.clear()


def _stale_days(value: str) -> int | None:
    try:
        parsed = date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None
    return (date.today() - parsed).days


def validate_model_contract(
    model_id: str,
    *,
    metadata: Any = None,
    market: str | None = None,
    allow_schema_drift: bool = False,
) -> dict[str, Any]:
    """校验模型特征契约；返回 ``{ok, blocked, skipped, blockers, warnings, ...}``。

    本函数不抛异常（除不可预期的 IO 由内部捕获为 blocker）。
    """
    meta = _parse_metadata(metadata)
    factor_source = str(meta.get("factor_source") or "").strip()
    report: dict[str, Any] = {
        "model_id": model_id,
        "ok": True,
        "blocked": False,
        "skipped": False,
        "factor_source": factor_source or None,
        "market": market or str(meta.get("market") or meta.get("qm_market") or "CN"),
        "schema_hash": None,
        "expected_schema_hash": str(meta.get("factor_schema_hash") or "").strip()
        or None,
        "missing_features": [],
        "blockers": [],
        "warnings": [],
    }
    if not factor_source:
        report["skipped"] = True
        report["skip_reason"] = "not_quantdb_factor_model"
        return report

    features = [
        str(f)
        for f in (meta.get("features") or meta.get("requested_features") or [])
        if str(f).strip()
    ]
    report["feature_count"] = len(features)
    if not features:
        report["blockers"].append("features_empty")
        report["ok"] = False
        report["blocked"] = True
        return report

    try:
        reader = QuantDBFactorReader(market=report["market"])
        status = _describe(reader, factor_source)
    except Exception as exc:  # noqa: BLE001
        report["blockers"].append(f"factor_source_unavailable: {exc}")
        report["ok"] = False
        report["blocked"] = True
        return report

    report["schema_hash"] = str(getattr(status, "schema_hash", "") or "") or None
    report["source_ready"] = bool(getattr(status, "ready", False))
    report["source_max_date"] = getattr(status, "max_date", None)
    if not report["source_ready"]:
        reason = str(getattr(status, "reason", "") or "factor source not ready")
        report["blockers"].append(f"factor_source_unavailable: {reason}")
        report["ok"] = False
        report["blocked"] = True
        return report

    try:
        _valid, missing = split_features_by_availability(
            reader,
            features,
            meta.get("factor_field_sources")
            if isinstance(meta.get("factor_field_sources"), dict)
            else None,
            anchor=factor_source,
        )
    except Exception as exc:  # noqa: BLE001 - IO/parquet 错误算阻断，不吞成 missing
        report["blockers"].append(f"feature_check_failed: {exc}")
        report["ok"] = False
        report["blocked"] = True
        return report
    if missing:
        report["missing_features"] = missing
        report["blockers"].append(
            f"features_missing({len(missing)}): {', '.join(missing[:10])}"
        )

    expected_hash = report["expected_schema_hash"]
    if not expected_hash:
        report["warnings"].append("factor_schema_hash_missing")
    elif report["schema_hash"] != expected_hash:
        detail = (
            f"factor_schema_drift: recorded={expected_hash[:16]} "
            f"current={str(report['schema_hash'])[:16]}"
        )
        if allow_schema_drift:
            report["warnings"].append(detail + " (allow_schema_drift=true)")
        else:
            report["blockers"].append(detail)

    fingerprint = meta.get("data_fingerprint")
    labels_max = ""
    if isinstance(fingerprint, dict):
        labels_max = str(fingerprint.get("labels_max_date") or "")
    if not labels_max:
        report["warnings"].append("data_fingerprint_labels_max_missing")
    else:
        stale = _stale_days(labels_max)
        report["labels_max_date"] = labels_max
        if stale is not None and stale > _STALE_MODEL_DAYS:
            report["warnings"].append(
                f"model_data_stale: {stale} days (labels_max={labels_max})"
            )

    report["ok"] = not report["blockers"]
    report["blocked"] = bool(report["blockers"])
    return report


def contract_block_message(report: dict[str, Any]) -> str:
    """把 blocker 列表压成一句可展示/可审计的中文。"""
    blockers = report.get("blockers") or []
    if not blockers:
        return ""
    return "模型特征契约未通过：" + "；".join(str(b) for b in blockers[:6])
