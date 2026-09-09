"""position_signal 的 QuantDB 路径解析回归测试。

背景：便携包数据目录是 ``$STORAGE_ROOT/quantdb``，机器上没有 ``/data/quantdb``。
修复前 position_signal 硬编码该路径 → instrument_detail 读不到 → 全量降级成
「其他」行业，groupby 塌陷使 ``pct_industry == pct_market``、``position_score`` 算错；
校准产物（ic_weights/payoff_table）同样读不到且**完全静默**退回经验默认。
"""
from __future__ import annotations

import json

import pytest

pytest.importorskip("pandas")
pytest.importorskip("duckdb")

from backend.services.engine.inference import position_signal  # noqa: E402

_SYMBOLS = ["600036.SH", "600519.SH", "000001.SZ", "300750.SZ"]


def _write_instrument_detail(root, rows):
    import pandas as pd

    detail = root / "2_base_sector" / "instrument_detail"
    detail.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        rows,
        columns=["Symbol", "rs_hyname", "Ltsz"],
    ).to_parquet(detail / "instrument_detail.parquet")


@pytest.fixture
def quantdb_root(tmp_path, monkeypatch):
    """env 指向 tmp 数据目录：两个行业（银行 2 只 / 白酒 1 只 / 电池 1 只）。"""
    root = tmp_path / "quantdb"
    _write_instrument_detail(
        root,
        [
            ("600036.SH", "银行", 2124.91),
            ("000001.SZ", "银行", 2305.40),
            ("600519.SH", "白酒", 18000.0),
            ("300750.SZ", "电池", 900.0),
        ],
    )
    monkeypatch.setenv("QM_QUANTDB_DATA_DIR", str(root))
    return root


@pytest.mark.unit
def test_industry_groups_do_not_collapse(quantdb_root):
    """修好路径后：行业/板块/市值分组各自成立，百分位不再全等于全市场。"""
    out = position_signal.compute_position_scores(
        _SYMBOLS, [0.9, 0.8, 0.5, 0.4], ["BUY"] * 4
    )
    by_symbol = {r["symbol"]: r for r in out}
    assert len(by_symbol) == 4

    bank = by_symbol["600036.SH"]
    # 银行组内只有 2 只（0.333/0.667），与全市场 4 只（0.2/0.4/0.6/0.8）不同
    assert bank["pct_industry"] != bank["pct_market"]
    # 市值档（超大盘 3 只 / 大盘 1 只）与行业组（银行 2 只）口径不同
    assert bank["industry_top10_avg"] != bank["cap_top10_avg"]


@pytest.mark.unit
def test_metadata_missing_degrades_with_path_in_warning(tmp_path, monkeypatch, caplog):
    """数据目录里没有 instrument_detail：不抛异常，但 warning 必须带解析后的路径。"""
    root = tmp_path / "quantdb"
    (root / "placeholder.txt").parent.mkdir(parents=True, exist_ok=True)
    (root / "placeholder.txt").write_text("non-empty", encoding="utf-8")
    monkeypatch.setenv("QM_QUANTDB_DATA_DIR", str(root))

    with caplog.at_level("WARNING", logger=position_signal.__name__):
        out = position_signal.compute_position_scores(
            _SYMBOLS, [0.9, 0.8, 0.5, 0.4], ["BUY"] * 4
        )

    assert len(out) == 4
    # 组塌陷：行业/板块/市值百分位退化成全市场百分位
    assert all(r["pct_industry"] == r["pct_market"] for r in out)
    assert all(r["industry_top10_avg"] == r["cap_top10_avg"] for r in out)
    assert str(root / "2_base_sector" / "instrument_detail") in caplog.text


@pytest.mark.unit
def test_calibration_loaded_from_data_dir(quantdb_root):
    """数据目录里的校准产物优先（允许客户重校准）。"""
    cal_dir = quantdb_root / "position_signal_calibration"
    cal_dir.mkdir()
    weights = {"market": 0.4, "industry": 0.3, "board": 0.2, "cap": 0.1}
    (cal_dir / "ic_weights.json").write_text(
        json.dumps({"weights": weights}), encoding="utf-8"
    )

    got, _ = position_signal._load_calibration()

    assert got == weights


@pytest.mark.unit
def test_calibration_falls_back_to_builtin_defaults(quantdb_root):
    """数据目录无校准 → 用仓库内置默认（而非粗糙的 _DEFAULT_WEIGHTS）。"""
    builtin = (
        position_signal.__file__.rsplit("/", 1)[0]
        + "/calibration_defaults/ic_weights.json"
    )
    with open(builtin, encoding="utf-8") as f:
        expected = json.load(f)["weights"]

    got, _ = position_signal._load_calibration()

    assert got == pytest.approx(expected)
    assert got != position_signal._DEFAULT_WEIGHTS


@pytest.mark.unit
def test_duckdb_read_parquet_accepts_bound_param():
    """read_parquet(?) 参数绑定必须可用（路径可能含单引号/Windows 反斜杠）。"""
    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect()
    try:
        with pytest.raises(Exception) as excinfo:
            con.execute(
                "SELECT * FROM read_parquet(?)", ["/nonexistent/x.parquet"]
            ).fetchdf()
        assert "Binder Error" not in str(excinfo.value)
    finally:
        con.close()
