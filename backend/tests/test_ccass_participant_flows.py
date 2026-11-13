"""CCASS 参与方流水 / 结构因子 / 同步守卫的契约测试。

用最小临时数据集锁住：
1. 连续席位才有 delta；entry/exit 的 delta 必须是 NaN（掉出 top50 ≠ 清仓）；
2. 股票级净流只汇总连续席位，entry/exit 单列，净/毛比可算；
3. 持续天数、ADV（不含当日）、公司行动标记字段存在；
4. 节假日分区守卫与「上一交易日重复快照」守卫。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import backend.scripts.build_ml_signal_datasets as sig
from backend.scripts import quanthk_ccass_sync as ccass_sync


def _write_parquet(df: pd.DataFrame, path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path / "data.parquet", index=False)


def _ccass_day(rows: list[tuple[str, str, int, float, str]]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "stock_code": code,
                "stock_name": code,
                "participant_id": pid,
                "participant_name": name,
                "holding_quantity": qty,
                "holding_percentage": pct,
                "query_date": pd.Timestamp("2026-11-10").date(),
            }
            for code, pid, qty, pct, name in rows
        ]
    )


def _build_hub(tmp_path, monkeypatch):
    class _Hub:
        def __init__(self, data_dir):
            self.data_dir = data_dir

    monkeypatch.setattr(sig, "_hub", lambda: _Hub(tmp_path))


def _seed(tmp_path, monkeypatch):
    _build_hub(tmp_path, monkeypatch)
    cc = tmp_path / "2_base_sector" / "ccass_top50"
    k = tmp_path / "1_kline_data" / "daily_forward"
    _write_parquet(
        _ccass_day(
            [
                ("0001.HK", "A", 100, 0.10, "券商A"),
                ("0001.HK", "B", 50, 0.05, "券商B"),
                ("0002.HK", "A", 200, 0.20, "券商A"),
            ]
        ),
        cc / "dt=20261110",
    )
    _write_parquet(
        _ccass_day(
            [
                ("0001.HK", "A", 120, 0.12, "券商A"),
                ("0001.HK", "C", 30, 0.03, "券商C"),
                ("0002.HK", "A", 180, 0.18, "券商A"),
            ]
        ),
        cc / "dt=20261111",
    )
    _write_parquet(
        pd.DataFrame(
            {
                "symbol": ["0001.HK", "0002.HK"],
                "volume": [1000, 2000],
                "amount": [10000, 20000],
            }
        ),
        k / "dt=20261110",
    )
    _write_parquet(
        pd.DataFrame(
            {
                "symbol": ["0001.HK", "0002.HK"],
                "volume": [800, 1500],
                "amount": [8000, 15000],
            }
        ),
        k / "dt=20261111",
    )
    return cc


def test_participant_flows_entry_exit_and_net(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    result = sig.build_participant_flows()
    assert result["status"] == "ok"

    out = pd.read_parquet(
        tmp_path / "6_ml_datasets" / "ccass_participant_flows" / "dt=20261111" / "data.parquet"
    )
    s = out[out.stock_code == "0001.HK"].set_index("participant_id")

    assert s.loc["A", "delta_qty"] == 20
    assert s.loc["A", "top50_streak_days"] == 2
    assert bool(s.loc["A", "entry_flag"]) is False
    assert bool(s.loc["A", "exit_flag"]) is False

    assert bool(s.loc["B", "exit_flag"]) is True
    assert np.isnan(s.loc["B", "delta_qty"])
    assert np.isnan(s.loc["B", "holding_quantity"])
    assert s.loc["B", "prev_qty"] == 50

    assert bool(s.loc["C", "entry_flag"]) is True
    assert np.isnan(s.loc["C", "delta_qty"])
    assert s.loc["C", "holding_quantity"] == 30

    a = s.loc["A"]
    assert a["stock_flow_net"] == 20
    assert a["stock_flow_gross"] == 20
    assert a["stock_net_over_gross"] == pytest.approx(1.0)
    assert a["stock_entry_qty"] == 30
    assert a["stock_exit_qty"] == 50
    assert a["stock_top50_qty"] == 150
    assert a["stock_top50_qty_prev"] == 150
    assert a["stock_qty_ratio"] == pytest.approx(1.0)
    assert a["adv20_volume"] == pytest.approx(1000.0)  # 前一交易日均量
    assert a["delta_over_adv20"] == pytest.approx(0.02)

    # 0002 只有连续席位 A：净流出 20
    t = out[out.stock_code == "0002.HK"].set_index("participant_id")
    assert t.loc["A", "stock_flow_net"] == -20
    assert t.loc["A", "stock_flow_gross"] == 20
    assert t.loc["A", "stock_qty_ratio"] == pytest.approx(0.9)


def test_ccass_factors_streaming_output(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    result = sig.build_ccass_factors()
    assert result["status"] == "ok"

    out = pd.read_parquet(
        tmp_path / "6_ml_datasets" / "ccass_factors" / "dt=20261111" / "data.parquet"
    ).set_index("symbol")

    s = out.loc["0001.HK"]
    assert s["ca_n_pis"] == 2
    assert s["ca_disclosed_sum"] == pytest.approx(0.15)
    assert s["ca_top1_pct"] == pytest.approx(0.12)
    assert s["ca_top3_pct"] == pytest.approx(0.15)
    assert s["ca_hhi_disc"] == pytest.approx((0.12 / 0.15) ** 2 + (0.03 / 0.15) ** 2)
    assert s["ca_new_entrants_1d"] == 1
    assert s["ca_exits_1d"] == 1
    assert bool(s["ca_pct_usable"]) is True

    t = out.loc["0002.HK"]
    assert t["ca_disclosed_sum"] == pytest.approx(0.18)


def test_duplicate_partition_guard(tmp_path, monkeypatch):
    monkeypatch.setenv("QM_QUANTHK_DATA_DIR", str(tmp_path))
    cc = tmp_path / "2_base_sector" / "ccass_top50"
    day1 = pd.DataFrame(
        {
            "stock_code": ["0001.HK", "0002.HK"],
            "participant_id": ["A", "B"],
            "holding_quantity": [100, 200],
        }
    )
    _write_parquet(day1, cc / "dt=20261110")
    _write_parquet(day1.copy(), cc / "dt=20261111")
    calendar = {"20261110", "20261111"}
    assert ccass_sync._is_duplicate_partition("20261111", cc / "dt=20261111", calendar)

    day2 = day1.copy()
    day2.loc[0, "holding_quantity"] = 999
    _write_parquet(day2, cc / "dt=20261111")
    assert not ccass_sync._is_duplicate_partition("20261111", cc / "dt=20261111", calendar)


def test_trading_day_guard():
    calendar = {"20261110", "20261112"}
    assert ccass_sync._skip_reason(pd.Timestamp("2026-11-14").date(), calendar) == "weekend"
    assert ccass_sync._skip_reason(pd.Timestamp("2026-11-11").date(), calendar) == "non_trading_day"
    # 日历之后的日期（今天/未来）不否决，交给 HKEX + 重复指纹兜底
    assert ccass_sync._skip_reason(pd.Timestamp("2026-11-13").date(), calendar) is None
