from __future__ import annotations

import pandas as pd
import pytest

from backend.services.engine.data_platform.quantdb_factor_reader import (
    QuantDBFactorError,
    QuantDBFactorReader,
)


def _write_factor_partition(root, source: str, frame: pd.DataFrame, dt: str) -> None:
    target = root / "6_ml_datasets" / source / f"dt={dt}"
    target.mkdir(parents=True)
    frame.to_parquet(target / "data.parquet", index=False)


def _write_daily_backward_partition(root, frame: pd.DataFrame, dt: str) -> None:
    target = root / "1_kline_data" / "daily_backward" / f"dt={dt}"
    target.mkdir(parents=True)
    frame.to_parquet(target / "data.parquet", index=False)


def _frame(day: str, close: float) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "symbol": ["600001.SH", "000001.SZ"],
            "date": [day, day],
            "open": [close - 1, close - 1],
            "high": [close + 1, close + 1],
            "low": [close - 2, close - 2],
            "close": [close, close],
            "volume": [100, 100],
            "amount": [1000, 1000],
            "l1_alpha": [0.1, 0.2],
        }
    )


def test_reads_single_source_with_logical_field_alias(tmp_path):
    _write_factor_partition(
        tmp_path, "l1_l2_factors", _frame("2024-01-02", 10), "20240102"
    )
    reader = QuantDBFactorReader(tmp_path)

    status = reader.assert_ready("l1_l2_factors", start="2024-01-02", end="2024-01-02")
    assert status.ready
    assert status.min_date == "2024-01-02"
    data = reader.read_day(
        "l1_l2_factors",
        features=["alpha"],
        feature_sources={"alpha": "l1_alpha"},
        trade_date="2024-01-02",
    )

    assert set(data["symbol"]) == {"SH600001", "SZ000001"}
    assert data["alpha"].tolist() == [0.1, 0.2]


def test_rejects_source_without_common_ohlcv(tmp_path):
    _write_factor_partition(
        tmp_path,
        "l2_factors",
        pd.DataFrame(
            {"symbol": ["600001.SH"], "date": ["2024-01-02"], "l2_alpha": [1.0]}
        ),
        "20240102",
    )
    with pytest.raises(QuantDBFactorError, match="not ready"):
        QuantDBFactorReader(tmp_path).assert_ready("l2_factors")


def test_forward_labels_use_future_close_without_snapshot(tmp_path):
    for day, close in [("2024-01-02", 10), ("2024-01-03", 11), ("2024-01-04", 12)]:
        _write_factor_partition(
            tmp_path, "l1_factors", _frame(day, close), day.replace("-", "")
        )
    reader = QuantDBFactorReader(tmp_path)
    frame = reader.read_range(
        "l1_factors", features=["l1_alpha"], start="2024-01-02", end="2024-01-04"
    )
    labels = reader.forward_labels(frame, horizon=1, signal_lag_days=0)
    first = labels[
        (labels["symbol"] == "SH600001")
        & (labels["trade_date"] == pd.Timestamp("2024-01-02"))
    ]
    assert first.iloc[0]["label"] == pytest.approx(0.1)


def test_read_range_backfills_null_factor_ohlcv_from_daily_backward(tmp_path):
    factor_frame = _frame("2024-01-02", 10)
    factor_frame[["open", "high", "low", "close", "volume", "amount"]] = None
    _write_factor_partition(tmp_path, "l1_factors", factor_frame, "20240102")
    _write_daily_backward_partition(
        tmp_path,
        _frame("2024-01-02", 12).drop(columns=["date", "l1_alpha"]),
        "20240102",
    )

    frame = QuantDBFactorReader(tmp_path).read_day(
        "l1_factors", features=["l1_alpha"], trade_date="2024-01-02"
    )

    assert frame["close"].tolist() == [12, 12]
    assert frame["volume"].tolist() == [100, 100]


# ── 市场映射与次要源补给（HK ccass/south 直读训练）──────────────────────────


def _hk_l1_frame(day: str, close: float) -> pd.DataFrame:
    """HK l1 风格：dt int 列（无 date 列）、suffix symbol。"""
    dt = int(day.replace("-", ""))
    return pd.DataFrame(
        {
            "symbol": ["0001.HK", "0002.HK"],
            "dt": [dt, dt],
            "open": [close - 1, close - 1],
            "high": [close + 1, close + 1],
            "low": [close - 2, close - 2],
            "close": [close, close],
            "volume": [100, 100],
            "amount": [1000, 1000],
            "mom_ret_1d": [0.01, 0.02],
        }
    )


def _hk_signal_frame(day: str) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "symbol": ["0001.HK", "0002.HK"],
            "date": [pd.Timestamp(day), pd.Timestamp(day)],
            "ca_n_pis": [50, 60],
            "ca_hhi_disc": [0.3, 0.4],
        }
    )


def test_market_source_mapping_and_defaults():
    from backend.services.engine.data_platform.quantdb_factor_reader import (
        default_source_for,
        normalize_market,
        sources_for_market,
    )

    assert normalize_market("hk") == "HK"
    assert normalize_market("A_SHARE") == "CN"
    assert normalize_market(None) == "CN"
    assert normalize_market("custom") == "CUSTOM"
    assert sources_for_market("HK") == ["l1_factors", "ccass_factors", "south_factors"]
    assert sources_for_market("US") == ["l1_factors"]
    assert sources_for_market("CUSTOM") == ["l1_factors"]
    assert default_source_for("HK") == "l1_factors"
    assert default_source_for("CN") == "l1_factors"
    assert default_source_for("CUSTOM") == "l1_factors"


def test_hk_l1_dt_date_alias_ready_and_read(tmp_path):
    _write_factor_partition(
        tmp_path, "l1_factors", _hk_l1_frame("2024-01-02", 10), "20240102"
    )
    reader = QuantDBFactorReader(tmp_path)
    status = reader.assert_ready("l1_factors", start="2024-01-02", end="2024-01-02")
    assert status.ready

    data = reader.read_day("l1_factors", features=["mom_ret_1d"], trade_date="2024-01-02")
    assert set(data["symbol"]) == {"0001.HK", "0002.HK"}
    assert data["close"].tolist() == [10, 10]


def test_secondary_source_ohlcv_donor_from_l1(tmp_path):
    _write_factor_partition(tmp_path, "l1_factors", _hk_l1_frame("2024-01-02", 10), "20240102")
    _write_factor_partition(tmp_path, "ccass_factors", _hk_signal_frame("2024-01-02"), "20240102")

    reader = QuantDBFactorReader(tmp_path)
    status = reader.assert_ready("ccass_factors", start="2024-01-02", end="2024-01-02")
    assert status.ready, status.missing_required

    data = reader.read_day(
        "ccass_factors", features=["ca_n_pis"], trade_date="2024-01-02"
    )
    assert len(data) == 2
    assert data["close"].tolist() == [10, 10]  # OHLCV 由 l1_factors 补给
    assert data["ca_n_pis"].tolist() == [50, 60]


def test_secondary_source_without_donor_is_not_ready(tmp_path):
    _write_factor_partition(tmp_path, "ccass_factors", _hk_signal_frame("2024-01-02"), "20240102")
    status = QuantDBFactorReader(tmp_path).describe("ccass_factors")
    assert not status.ready
    assert set(status.missing_required) == set(
        ("open", "high", "low", "close", "volume", "amount")
    )


# ── 自定义市场：仅扫描因子，不强制 OHLCV ──────────────────────────────────────


def test_custom_market_scan_only_without_ohlcv(tmp_path):
    _write_factor_partition(
        tmp_path,
        "l1_factors",
        pd.DataFrame(
            {"symbol": ["MY001", "MY002"], "date": ["2024-01-02", "2024-01-02"], "my_alpha": [1.0, 2.0]}
        ),
        "20240102",
    )
    # 同一份无 OHLCV 数据：CN 口径拒绝，CUSTOM 口径仅扫描即 ready
    with pytest.raises(QuantDBFactorError, match="not ready"):
        QuantDBFactorReader(tmp_path).assert_ready("l1_factors")
    status = QuantDBFactorReader(tmp_path, market="CUSTOM").describe("l1_factors")
    assert status.ready
    assert status.missing_required == []
    assert "my_alpha" in status.columns
