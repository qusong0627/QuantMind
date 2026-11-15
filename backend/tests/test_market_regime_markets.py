"""模型管理「大盘分析」指数候选按市场分派（不再给港股/美股套沪深300）。"""
from __future__ import annotations

import pandas as pd

from backend.services.api.routers import model_training as mt


def test_model_market_tag_aliases():
    assert mt._model_market_tag({"market": "HK"}) == "HK"
    assert mt._model_market_tag({"context": {"market": "HONG_KONG"}}) == "HK"
    assert mt._model_market_tag({"market": "US_STOCK"}) == "US"
    assert mt._model_market_tag({"market": "FUTURES"}) == "FUTURES"
    assert mt._model_market_tag({"market": "A股"}) == "CN"
    assert mt._model_market_tag({}) == "CN"


def test_regime_index_options_are_market_scoped():
    assert [c for c, _ in mt._market_regime_indices("HK")] == [
        "HSI.HK",
        "HSCEI.HK",
        "HSTECH.HK",
        "HSCCI.HK",
    ]
    assert "000300.SH" not in [c for c, _ in mt._market_regime_indices("HK")]
    assert "000300.SH" not in [c for c, _ in mt._market_regime_indices("US")]
    assert [c for c, _ in mt._market_regime_indices("FUTURES")][0] == "100001.CCI"
    assert mt._market_regime_indices("CRYPTO") == ()


def test_index_scope_non_cn_is_all_market():
    assert mt._index_scope("SPX.US", "US") == ("all", None)
    assert mt._index_scope("100001.CCI", "FUTURES") == ("all", None)


def test_hk_index_scope_reads_latest_membership(tmp_path, monkeypatch):
    root = tmp_path / "quanthk"
    d = root / "2_base_sector" / "index_membership" / "dt=20261114"
    d.mkdir(parents=True)
    pd.DataFrame(
        {
            "index_name": ["HSI"],
            "stock_code": ["0700.HK", "0005.HK"],
            "weight": [1.0, 2.0],
        }
    ).to_parquet(d / "HSI.parquet", index=False)
    monkeypatch.setenv("QM_QUANTHK_DATA_DIR", str(root))
    kind, value = mt._index_scope("HSI.HK", "HK")
    assert kind == "constituents"
    assert value == ["0005.HK", "0700.HK"]


def test_futures_regime_closes_from_ccidx(tmp_path, monkeypatch):
    root = tmp_path / "quantfutures"
    d = root / "2_base_sector" / "ccidx_index"
    d.mkdir(parents=True)
    pd.DataFrame(
        {
            "time": ["2026-09-01", "2026-09-02", "2026-09-03"],
            "index_code": ["100001.CCI"] * 3,
            "close": [100.0, 0.0, 102.0],
        }
    ).to_parquet(d / "data.parquet", index=False)
    monkeypatch.setenv("QM_QUANTFUTURES_DATA_DIR", str(root))
    out = mt._load_futures_regime_closes(
        ["100001.CCI"], "2026-09-01", "2026-09-03"
    )
    assert out == {"100001.CCI": {"2026-09-01": 100.0, "2026-09-03": 102.0}}
