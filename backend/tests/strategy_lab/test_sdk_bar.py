"""Tests for Bar dataclass."""

import math

import pandas as pd

from backend.services.engine.strategy_lab.sdk.bar import Bar


def test_bar_basic_attrs():
    b = Bar(
        symbol="SH600036",
        date=pd.Timestamp("2025-06-12"),
        open=10.0,
        high=11.0,
        low=9.5,  # fidelity: allow-limit-threshold — 非阈值：Bar 夹具的 low
        close=10.5,
        volume=1_000_000,
        adj_close=10.5,
    )
    assert b.symbol == "SH600036"
    assert b.close == 10.5
    assert b.adj_close == 10.5


def test_bar_feature_lookup():
    b = Bar(
        symbol="SH600036",
        date=pd.Timestamp("2025-06-12"),
        open=10.0, high=11.0, low=9.5, close=10.5, volume=0,  # fidelity: allow-limit-threshold — 非阈值：Bar 夹具的 low
        _features={"momentum_20": 0.123, "pe": 12.3, "missing_nan": float("nan")},
    )
    assert b.feature("momentum_20") == 0.123
    assert b.feature("pe") == 12.3
    assert b.has_feature("momentum_20")
    # NaN treated as missing
    assert b.feature("missing_nan") is None
    # Unknown feature returns default
    assert b.feature("unknown") is None
    assert b.feature("unknown", default=0.0) == 0.0
    # Type coercion
    assert isinstance(b.feature("momentum_20"), float)


def test_bar_features_property_is_copy():
    b = Bar(
        symbol="x", date=pd.Timestamp("2025-01-01"),
        open=1, high=1, low=1, close=1, volume=0,
        _features={"a": 1.0},
    )
    feats = b.features
    feats["a"] = 999.0
    assert b.feature("a") == 1.0  # internal not mutated


def test_bar_to_dict():
    b = Bar(
        symbol="SH600036", date=pd.Timestamp("2025-06-12"),
        open=10.0, high=11.0, low=9.5, close=10.5, volume=1_000_000,  # fidelity: allow-limit-threshold — 非阈值：Bar 夹具的 low
    )
    d = b.to_dict()
    assert d["symbol"] == "SH600036"
    assert d["date"] == "2025-06-12T00:00:00"
    assert d["close"] == 10.5
