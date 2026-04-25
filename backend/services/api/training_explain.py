from __future__ import annotations

from typing import Any

from fastapi import HTTPException


DEFAULT_EXPLAIN_CFG: dict[str, Any] = {
    "enable_shap": True,
    "shap_split": "valid",
    "shap_sample_rows": 30000,
}
ALLOWED_SHAP_SPLIT = {"valid", "test", "train"}
MIN_SHAP_SAMPLE_ROWS = 1000
MAX_SHAP_SAMPLE_ROWS = 100000


def normalize_explain(raw: Any) -> dict[str, Any]:
    explain = raw if isinstance(raw, dict) else {}

    enable_shap = explain.get("enable_shap", DEFAULT_EXPLAIN_CFG["enable_shap"])
    if not isinstance(enable_shap, bool):
        raise HTTPException(status_code=422, detail="explain.enable_shap must be a boolean")

    shap_split = str(explain.get("shap_split", DEFAULT_EXPLAIN_CFG["shap_split"])).strip().lower()
    if shap_split not in ALLOWED_SHAP_SPLIT:
        raise HTTPException(status_code=422, detail="explain.shap_split must be one of: valid, test, train")

    shap_sample_rows_raw = explain.get("shap_sample_rows", DEFAULT_EXPLAIN_CFG["shap_sample_rows"])
    try:
        shap_sample_rows = int(shap_sample_rows_raw)
    except Exception as exc:
        raise HTTPException(status_code=422, detail="explain.shap_sample_rows must be an integer") from exc
    if not (MIN_SHAP_SAMPLE_ROWS <= shap_sample_rows <= MAX_SHAP_SAMPLE_ROWS):
        raise HTTPException(
            status_code=422,
            detail=f"explain.shap_sample_rows must be between {MIN_SHAP_SAMPLE_ROWS} and {MAX_SHAP_SAMPLE_ROWS}",
        )

    return {
        "enable_shap": enable_shap,
        "shap_split": shap_split,
        "shap_sample_rows": shap_sample_rows,
    }
