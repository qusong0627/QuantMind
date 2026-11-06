"""Canonical normalization for factor formula identity comparisons."""

from __future__ import annotations

import re

_FORMULA_NOISE_RE = re.compile(
    r"\\left|\\right|\\[;,!]|\\quad|\\qquad|\\thinspace|\\medspace"
)


def normalize_formula(formulation: str | None) -> str:
    """Remove LaTeX layout noise while preserving mathematical structure."""
    value = str(formulation or "").strip()
    if not value:
        return ""
    if len(value) >= 2 and value.startswith("$") and value.endswith("$"):
        value = value[1:-1]
    value = _FORMULA_NOISE_RE.sub("", value)
    return re.sub(r"\s+", "", value).lower()
