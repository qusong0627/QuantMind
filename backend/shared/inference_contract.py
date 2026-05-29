from __future__ import annotations

import hashlib
import json
from typing import Any

import numpy as np
import pandas as pd


def canonical_json_hash(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def normalize_fill_values(fill_values: dict[str, Any] | None) -> dict[str, float]:
    out: dict[str, float] = {}
    for k, v in (fill_values or {}).items():
        try:
            n = float(v)
        except Exception:
            n = 0.0
        if not np.isfinite(n):
            n = 0.0
        out[str(k)] = n
    return out


def _hash_symbols(symbols: pd.Series) -> str:
    sym_text = "\n".join(symbols.astype(str).tolist())
    return hashlib.sha256(sym_text.encode("utf-8")).hexdigest()


def _hash_feature_matrix(df: pd.DataFrame, feature_columns: list[str]) -> str:
    if not feature_columns:
        return hashlib.sha256(b"").hexdigest()
    work = df.copy()
    for col in feature_columns:
        if col not in work.columns:
            work[col] = 0.0
    matrix = work[feature_columns].apply(pd.to_numeric, errors="coerce").fillna(0.0).to_numpy(dtype=np.float32)
    matrix = np.nan_to_num(matrix, nan=0.0, posinf=0.0, neginf=0.0)
    return hashlib.sha256(matrix.tobytes(order="C")).hexdigest()


def build_day_snapshot(df: pd.DataFrame, feature_columns: list[str], *, symbol_col: str = "symbol") -> dict[str, Any]:
    if df.empty:
        return {
            "row_count": 0,
            "symbol_hash": hashlib.sha256(b"").hexdigest(),
            "feature_hash": hashlib.sha256(b"").hexdigest(),
        }
    ordered = df.copy()
    ordered[symbol_col] = ordered[symbol_col].astype(str)
    ordered = ordered.sort_values([symbol_col], ascending=[True]).reset_index(drop=True)
    return {
        "row_count": int(len(ordered)),
        "symbol_hash": _hash_symbols(ordered[symbol_col]),
        "feature_hash": _hash_feature_matrix(ordered, feature_columns),
    }


def build_daily_manifest(
    df: pd.DataFrame,
    feature_columns: list[str],
    *,
    trade_date_col: str = "trade_date",
    symbol_col: str = "symbol",
) -> tuple[dict[str, Any], str]:
    manifest: dict[str, Any] = {}
    if df.empty:
        return manifest, canonical_json_hash(manifest)
    work = df.copy()
    work[trade_date_col] = pd.to_datetime(work[trade_date_col]).dt.strftime("%Y-%m-%d")
    for trade_date, frame in work.groupby(trade_date_col, sort=True):
        manifest[str(trade_date)] = build_day_snapshot(frame, feature_columns, symbol_col=symbol_col)
    return manifest, canonical_json_hash(manifest)


def compare_frozen_config(
    *,
    frozen_feature_columns: list[str],
    frozen_fill_values: dict[str, Any],
    frozen_best_iteration: int | None,
    frozen_target_horizon_days: int | None,
    actual_feature_columns: list[str],
    actual_fill_values: dict[str, Any],
    actual_best_iteration: int | None,
    actual_target_horizon_days: int | None,
) -> list[dict[str, Any]]:
    mismatches: list[dict[str, Any]] = []

    if list(frozen_feature_columns or []) != list(actual_feature_columns or []):
        mismatches.append(
            {
                "field": "feature_columns",
                "expected_hash": canonical_json_hash(list(frozen_feature_columns or [])),
                "actual_hash": canonical_json_hash(list(actual_feature_columns or [])),
            }
        )

    frozen_fill = normalize_fill_values(frozen_fill_values)
    actual_fill = normalize_fill_values(actual_fill_values)
    if frozen_fill != actual_fill:
        mismatches.append(
            {
                "field": "fill_values",
                "expected_hash": canonical_json_hash(frozen_fill),
                "actual_hash": canonical_json_hash(actual_fill),
            }
        )

    if int(frozen_best_iteration or 0) != int(actual_best_iteration or 0):
        mismatches.append(
            {
                "field": "best_iteration",
                "expected": int(frozen_best_iteration or 0),
                "actual": int(actual_best_iteration or 0),
            }
        )

    if int(frozen_target_horizon_days or 0) != int(actual_target_horizon_days or 0):
        mismatches.append(
            {
                "field": "target_horizon_days",
                "expected": int(frozen_target_horizon_days or 0),
                "actual": int(actual_target_horizon_days or 0),
            }
        )

    return mismatches
