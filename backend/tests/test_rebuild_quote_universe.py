"""全市场订阅标的集合重建：代码归一化与污染过滤。"""

from __future__ import annotations

from backend.scripts.rebuild_quote_universe import normalize_symbols


def test_normalize_prefix_and_suffix_forms_collapse() -> None:
    out = normalize_symbols(
        ["SH600036", "600036.SH", "SZ000001", "920000.BJ", "BJ920000"]
    )
    assert out == ["600036.SH", "000001.SZ", "920000.BJ"]


def test_normalize_drops_non_cn_and_empty() -> None:
    assert normalize_symbols(["", "  ", "AAPL", "00700.HK", "SH"]) == []


def test_normalize_keeps_stable_order() -> None:
    out = normalize_symbols(["SZ000001", "SH600036"])
    assert out == ["000001.SZ", "600036.SH"]
