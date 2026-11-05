"""退市股因子覆盖探针（A2）测试。"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

from backend.scripts import delisted_factor_coverage as cov

RAW_REL = "factor_research/selection_v2_20261021/delisted_raw"


def _write_raw(root: Path, symbol: str) -> None:
    target = root / RAW_REL
    target.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        {
            "symbol": [symbol] * 3,
            "time": ["2020-01-02", "2020-01-06", "2020-01-10"],
            "close": [1.0, 1.1, 1.2],
        }
    ).to_parquet(target / f"{symbol}_qfq.parquet", index=False)


def _write_partition(root: Path, dt: str, symbols: list[str]) -> None:
    target = root / "6_ml_datasets" / "factor_defs" / f"dt={dt}"
    target.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame(
        {
            "symbol": symbols,
            "time": [dt] * len(symbols),
            "close": [1.0] * len(symbols),
            "mom_5": [0.1] * len(symbols),
            "vol_20": [0.2] * len(symbols),
            "fun_pe": [10.0] * len(symbols),
            "a101_001": [0.3] * len(symbols),
            "misc_col": [0] * len(symbols),
        }
    )
    frame.to_parquet(target / "data.parquet", index=False)


def test_report_coverage_and_column_classification(tmp_path: Path):
    for sym in ("000004.SZ", "000005.SZ", "000006.SZ"):
        _write_raw(tmp_path, sym)
    # 000004 在 01-03 命中；000005 只有 01-10；000006 完全没有
    _write_partition(tmp_path, "20200103", ["000004.SZ", "600000.SH"])
    _write_partition(tmp_path, "20200110", ["000005.SZ", "600000.SH"])

    report = cov.build_report(tmp_path, libraries=["factor_defs"], sample=10)

    assert report["delisted_symbols"] == 3
    lib = report["libraries"][0]
    assert lib["partitions"] == 2
    assert lib["latest"] == "20200110"
    assert lib["coverage"] == 0.6667
    assert sorted(lib["hits"]) == ["000004.SZ", "000005.SZ"]
    assert lib["misses"] == ["000006.SZ"]
    assert lib["columns"]["ohlcv_derived"] == 3  # close / mom_5 / vol_20
    assert lib["columns"]["formula_derived"] == 1
    assert lib["columns"]["non_derivable"] == 1
    assert lib["columns"]["other"] == 3  # symbol / time / misc_col


def test_missing_library_reports_error_not_crash(tmp_path: Path):
    _write_raw(tmp_path, "000004.SZ")

    report = cov.build_report(tmp_path, libraries=["not_a_lib"], sample=5)

    lib = report["libraries"][0]
    assert lib["error"] == "no_dt_partitions"
    assert lib["coverage"] is None


def test_load_delisted_spans(tmp_path: Path):
    _write_raw(tmp_path, "000004.SZ")

    spans = cov.load_delisted_spans(tmp_path / RAW_REL)

    assert "000004.SZ" in spans
    assert spans["000004.SZ"].first == date(2020, 1, 2)
    assert spans["000004.SZ"].last == date(2020, 1, 10)
