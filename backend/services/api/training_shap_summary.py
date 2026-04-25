from __future__ import annotations

import csv
from pathlib import Path
from typing import Any


def to_float_or(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def to_int_or(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def read_shap_summary_rows(file_path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with file_path.open("r", encoding="utf-8") as fp:
        reader = csv.DictReader(fp)
        for raw in reader:
            feature = str((raw or {}).get("feature") or "").strip()
            if not feature:
                continue
            rows.append(
                {
                    "feature": feature,
                    "mean_abs_shap": to_float_or((raw or {}).get("mean_abs_shap"), 0.0),
                    "mean_shap": to_float_or((raw or {}).get("mean_shap"), 0.0),
                    "positive_ratio": to_float_or((raw or {}).get("positive_ratio"), 0.0),
                }
            )
    rows.sort(key=lambda item: float(item.get("mean_abs_shap") or 0.0), reverse=True)
    for idx, row in enumerate(rows, start=1):
        row["rank"] = idx
    return rows
