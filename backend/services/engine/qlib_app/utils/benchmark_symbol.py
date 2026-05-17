"""Benchmark symbol normalization utilities for Qlib data lookup."""

from __future__ import annotations


_ALIAS_TO_CANONICAL = {
    "CSI300": "IDX_SH000300",
    "HS300": "IDX_SH000300",
    "000300": "IDX_SH000300",
    "SH000300": "IDX_SH000300",
    "SZ399300": "IDX_SH000300",
    "CSI500": "IDX_SH000905",
    "ZZ500": "IDX_SH000905",
    "000905": "IDX_SH000905",
    "SH000905": "IDX_SH000905",
    "CSI1000": "IDX_SH000852",
    "ZZ1000": "IDX_SH000852",
    "000852": "IDX_SH000852",
    "SH000852": "IDX_SH000852",
}


def normalize_benchmark_symbol(symbol: str | None) -> str:
    raw = (symbol or "").strip().upper()
    if not raw:
        return "IDX_SH000300"
    if raw.startswith("IDX_SH") or raw.startswith("IDX_SZ"):
        return raw
    return _ALIAS_TO_CANONICAL.get(raw, raw)


def benchmark_candidates(symbol: str | None) -> list[str]:
    """Return ordered symbol candidates for lookup: new namespace first."""
    canonical = normalize_benchmark_symbol(symbol)
    candidates: list[str] = [canonical]
    if canonical.startswith("IDX_"):
        legacy = canonical[4:]
        candidates.append(legacy)
        if legacy == "SH000300":
            candidates.append("SZ399300")
    seen = set()
    ordered: list[str] = []
    for item in candidates:
        if item and item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered

