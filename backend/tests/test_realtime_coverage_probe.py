"""T-RT-03 热集行情覆盖探针：符号归一与分档（纯函数；只读脚本不触网）。"""

from __future__ import annotations

import pytest

from backend.scripts.realtime_coverage_probe import (
    bucket_of,
    classify,
    normalize_symbol,
)


@pytest.mark.unit
def test_normalize_symbol_all_forms():
    assert normalize_symbol("000001.SH") == ("000001", "SH")
    assert normalize_symbol("600036.SH") == ("600036", "SH")
    assert normalize_symbol("SH600036") == ("600036", "SH")
    assert normalize_symbol("sh600036") == ("600036", "SH")
    assert normalize_symbol("600036") == ("600036", "SH")
    assert normalize_symbol("000001") == ("000001", "SZ")
    assert normalize_symbol("300750") == ("300750", "SZ")
    assert normalize_symbol("430047") == ("430047", "BJ")
    assert normalize_symbol("BAD!!!") is None


@pytest.mark.unit
def test_index_and_stock_with_same_digits_are_not_folded():
    """000001.SH（指数）与 000001.SZ（平安银行）必须是两个身份——覆盖统计不能合并。"""
    assert normalize_symbol("000001.SH") != normalize_symbol("000001.SZ")
    assert bucket_of("000001", "SH") == "index"
    assert bucket_of("000001", "SZ") == "sz_main"


@pytest.mark.unit
def test_classify_states_and_buckets():
    now = 1_800_000_000.0
    hot = ["600036.SH", "000001.SZ", "000001.SH", "300750.SZ"]
    snapshots = {
        ("600036", "SH"): {"timestamp": str(int(now - 10))},  # fresh
        ("000001", "SZ"): {"timestamp": str(int(now - 120))},  # stale（可用）
        ("000001", "SH"): {"timestamp": str(int(now - 400))},  # expired
    }
    report = classify(hot, snapshots, now=now)
    assert report["counts"] == {"fresh": 1, "stale": 1, "expired": 1, "missing": 1}
    assert report["usable_ratio"] == 0.5
    assert report["buckets"]["index"] == {
        "total": 1,
        "fresh": 0,
        "stale": 0,
        "expired": 1,
        "missing": 0,
    }
    assert report["buckets"]["sz_main"] == {
        "total": 2,
        "fresh": 0,
        "stale": 1,
        "expired": 0,
        "missing": 1,
    }
    states = {item["symbol"]: item["state"] for item in report["symbols"]}
    assert states["300750.SZ"] == "missing"
    assert states["600036.SH"] == "fresh"


@pytest.mark.unit
def test_classify_empty_hot_set_is_not_a_zero_ratio():
    report = classify([], {}, now=1_800_000_000.0)
    assert report["hot_total"] == 0
    assert report["usable_ratio"] is None  # 空集不是 0%，是「不知道」
