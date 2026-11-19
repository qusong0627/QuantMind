"""qfq 标签取价面板的单元测试。"""
from __future__ import annotations

import pandas as pd
import pytest

from backend.shared.qfq_label_prices import (
    PANEL_COLUMNS,
    forward_return_labels,
    load_qfq_label_panel,
)


def _write_partition(root, dt: str, rows: list[dict]) -> None:
    d = root / "1_kline_data" / "daily_forward" / f"dt={dt}"
    d.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_parquet(d / "data.parquet", index=False)


def test_returns_empty_when_dir_missing(tmp_path):
    out = load_qfq_label_panel(tmp_path, "2010-01-04", "2015-12-31", market="CN")
    assert out.empty
    assert list(out.columns) == list(PANEL_COLUMNS)


def test_non_cn_market_is_noop(tmp_path):
    _write_partition(
        tmp_path,
        "20100104",
        [{"symbol": "600000.SH", "time": pd.Timestamp("2010-01-04"),
          "open": 10.0, "close": 10.5}],
    )
    out = load_qfq_label_panel(tmp_path, "2010-01-04", "2015-12-31", market="HK")
    assert out.empty


def test_reads_qfq_and_converts_symbol_to_prefix(tmp_path):
    _write_partition(
        tmp_path,
        "20100104",
        [
            {"symbol": "600000.SH", "time": pd.Timestamp("2010-01-04"),
             "open": 10.0, "close": 10.5},
            {"symbol": "000001.SZ", "time": pd.Timestamp("2010-01-04"),
             "open": 20.0, "close": 21.0},
        ],
    )
    _write_partition(
        tmp_path,
        "20100105",
        [{"symbol": "600000.SH", "time": pd.Timestamp("2010-01-05"),
          "open": 10.6, "close": 11.0}],
    )
    out = load_qfq_label_panel(tmp_path, "2010-01-04", "2010-01-05")
    assert list(out.columns) == list(PANEL_COLUMNS)
    assert sorted(out["symbol"].unique()) == ["SH600000", "SZ000001"]
    assert len(out) == 3
    row = out[(out["symbol"] == "SH600000") & (out["trade_date"] == pd.Timestamp("2010-01-04"))]
    assert row["_qfq_open"].iloc[0] == pytest.approx(10.0)
    assert row["_qfq_close"].iloc[0] == pytest.approx(10.5)


def test_date_range_is_applied(tmp_path):
    _write_partition(
        tmp_path,
        "20100104",
        [{"symbol": "600000.SH", "time": pd.Timestamp("2010-01-04"),
          "open": 10.0, "close": 10.5}],
    )
    _write_partition(
        tmp_path,
        "20100105",
        [{"symbol": "600000.SH", "time": pd.Timestamp("2010-01-05"),
          "open": 10.6, "close": 11.0}],
    )
    out = load_qfq_label_panel(tmp_path, "2010-01-05", "2010-01-05")
    assert len(out) == 1
    assert out["trade_date"].iloc[0] == pd.Timestamp("2010-01-05")


def test_no_bar_day_is_absent_so_callers_can_mask(tmp_path):
    """停牌日不补行：该标的当日不出现在面板里。"""
    _write_partition(
        tmp_path,
        "20100104",
        [{"symbol": "600000.SH", "time": pd.Timestamp("2010-01-04"),
          "open": 10.0, "close": 10.5}],
    )
    _write_partition(
        tmp_path,
        "20100105",
        [{"symbol": "000001.SZ", "time": pd.Timestamp("2010-01-05"),
          "open": 20.0, "close": 21.0}],
    )
    out = load_qfq_label_panel(tmp_path, "2010-01-04", "2010-01-05")
    assert len(out) == 2
    assert "SH600000" not in set(out.loc[out["trade_date"] == pd.Timestamp("2010-01-05"), "symbol"])


def _panel(rows: list[tuple[str, str, float]]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "symbol": [r[0] for r in rows],
            "trade_date": pd.to_datetime([r[1] for r in rows]),
            "_qfq_open": [r[2] for r in rows],
            "_qfq_close": [r[2] for r in rows],
        }
    )


def test_forward_return_labels_use_t_plus_one_entry():
    # 7 个交易日：close = 100..106；horizon=5, lag=1 → T 日标签 = close[T+6]/close[T+1]-1
    days = [f"2010-01-{d:02d}" for d in range(4, 11)]
    panel = _panel([("SH600000", d, 100.0 + i) for i, d in enumerate(days)])
    labels = forward_return_labels(panel, horizon=5, signal_lag_days=1)
    first = labels.iloc[0]
    assert first["label"] == pytest.approx(106.0 / 101.0 - 1.0)
    # 末尾 lag+horizon 行无标签
    assert labels["label"].tail(6).isna().all()


def test_forward_return_labels_do_not_bleed_across_symbols():
    days = [f"2010-01-{d:02d}" for d in range(4, 11)]
    panel = pd.concat(
        [
            _panel([("SH600000", d, 100.0 + i) for i, d in enumerate(days)]),
            _panel([("SZ000001", d, 200.0 + i * 2) for i, d in enumerate(days)]),
        ],
        ignore_index=True,
    )
    labels = forward_return_labels(panel, horizon=5, signal_lag_days=1)
    a = labels[labels["symbol"] == "SH600000"].reset_index(drop=True)
    b = labels[labels["symbol"] == "SZ000001"].reset_index(drop=True)
    assert a.iloc[0]["label"] == pytest.approx(106.0 / 101.0 - 1.0)
    assert b.iloc[0]["label"] == pytest.approx(212.0 / 202.0 - 1.0)


def test_forward_return_labels_empty_panel_is_empty():
    out = forward_return_labels(pd.DataFrame(), horizon=5)
    assert out.empty
    assert list(out.columns) == ["symbol", "trade_date", "label"]
