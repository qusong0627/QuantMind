"""因子池面板缓存（pool_panels）——落盘格式、秩相关口径、多样性矩阵。

口径来源（与 ``rd_mined_materialize._max_abs_corr`` 同源，本文件钉死）：
逐日截面 rank 后按日求皮尔逊相关、再对日均值——面板已存 rank_pct，
对 rank_pct 求皮尔逊 ≡ 逐日 Spearman。每天 ≥20 对、≥5 个有效日才算数；
无重叠返回 None（调用方按「无面板」降级，宁可漏报不可误报）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backend.services.engine.mining_plugins.pool_panels import (
    canonicalize_values,
    corr_matrix,
    pair_corr,
    panel_path,
    read_panel,
    sample_days,
    write_panel,
)


def _series(days, symbols, rng, *, start=1.0):
    """构造 (datetime, instrument) 两层索引的因子值 Series（instrument 用后缀式）。"""
    idx = pd.MultiIndex.from_product([days, symbols], names=["datetime", "instrument"])
    return pd.Series(rng.normal(size=len(idx)) * start, index=idx)


class TestCanonicalize:
    def test_prefix_symbols_and_str_dates(self):
        idx = pd.MultiIndex.from_tuples(
            [("2026-01-05", "600036.SH"), ("2026-01-05", "000001.SZ")],
            names=["datetime", "instrument"],
        )
        frame = canonicalize_values(pd.Series([1.0, 2.0], index=idx))
        assert list(frame["trade_date"]) == ["2026-01-05", "2026-01-05"]
        assert sorted(frame["symbol"]) == ["SH600036", "SZ000001"]

    def test_nan_and_inf_dropped(self):
        idx = pd.MultiIndex.from_tuples(
            [
                ("2026-01-05", "SH600036"),
                ("2026-01-05", "SZ000001"),
                ("2026-01-05", "SZ000002"),
            ],
            names=["datetime", "instrument"],
        )
        frame = canonicalize_values(pd.Series([1.0, np.nan, np.inf], index=idx))
        assert len(frame) == 1

    def test_duplicate_index_keeps_last(self):
        idx = pd.MultiIndex.from_tuples(
            [("2026-01-05", "SH600036"), ("2026-01-05", "SH600036")],
            names=["datetime", "instrument"],
        )
        frame = canonicalize_values(pd.Series([1.0, 9.0], index=idx))
        assert len(frame) == 1
        assert frame["value"].iloc[0] == pytest.approx(9.0)

    def test_bad_index_raises(self):
        with pytest.raises(ValueError):
            canonicalize_values(pd.Series([1.0], index=[0]))


class TestWriteReadRoundtrip:
    @pytest.fixture()
    def panel_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("QM_FACTOR_POOL_PANEL_DIR", str(tmp_path / "panels"))
        return tmp_path

    def test_roundtrip_columns_dtypes_and_rank(self, panel_env):
        rng = np.random.default_rng(7)
        days = [f"2026-01-{d:02d}" for d in range(1, 6)]
        symbols = [f"SH60000{i}" for i in range(5)]
        ref = write_panel("a_share", "fid1", _series(days, symbols, rng))
        assert ref == "a_share/fid1.parquet"
        panel = read_panel("a_share", "fid1")
        assert set(panel.columns) == {"trade_date", "symbol", "rank_pct", "zscore"}
        assert panel["rank_pct"].dtype == np.float32
        assert panel["zscore"].dtype == np.float32
        # 逐日 rank-pct ∈ (0, 1]，最大 1.0；逐日 zscore 均值 ≈ 0
        for _, g in panel.groupby("trade_date"):
            assert g["rank_pct"].max() == pytest.approx(1.0)
            assert (g["rank_pct"] > 0).all()
            assert g["zscore"].mean() == pytest.approx(0.0, abs=1e-5)

    def test_missing_panel_returns_none(self, panel_env):
        assert read_panel("a_share", "nope") is None

    def test_corrupted_panel_returns_none(self, panel_env):
        path = panel_path("a_share", "bad")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"not a parquet at all")
        assert read_panel("a_share", "bad") is None

    def test_empty_values_returns_none(self, panel_env):
        assert write_panel("a_share", "empty", pd.Series(dtype=float)) is None

    def test_forward_return_column_written_and_normalized(self, panel_env):
        """fret 走同一 canonicalize：后缀式收益代码 → 前缀式行能逐一对上。"""
        rng = np.random.default_rng(3)
        days = [f"2026-01-{d:02d}" for d in range(1, 6)]
        ret_idx = pd.MultiIndex.from_product(
            [days, [f"60000{i}.SH" for i in range(5)]], names=["datetime", "instrument"]
        )
        ret = pd.Series(rng.normal(scale=0.01, size=len(ret_idx)), index=ret_idx)
        write_panel(
            "a_share",
            "fid_ret",
            _series(days, [f"SH60000{i}" for i in range(5)], rng),
            forward_return=ret,
        )
        panel = read_panel("a_share", "fid_ret")
        assert "fret" in panel.columns
        assert panel["fret"].dtype == np.float32
        assert panel["fret"].notna().all()
        got = panel.loc[
            (panel["trade_date"] == "2026-01-01") & (panel["symbol"] == "SH600001"),
            "fret",
        ].iloc[0]
        assert got == pytest.approx(
            float(ret.loc[("2026-01-01", "600001.SH")]), rel=1e-6
        )

    def test_forward_return_partial_coverage_keeps_rows_with_nan(self, panel_env):
        rng = np.random.default_rng(4)
        days = [f"2026-01-{d:02d}" for d in range(1, 4)]
        symbols = [f"SH60000{i}" for i in range(4)]
        ret_idx = pd.MultiIndex.from_tuples(
            [(days[0], "SH600000")], names=["datetime", "instrument"]
        )
        ret = pd.Series([0.01], index=ret_idx)
        write_panel(
            "a_share", "fid_part", _series(days, symbols, rng), forward_return=ret
        )
        panel = read_panel("a_share", "fid_part")
        assert len(panel) == len(days) * len(symbols)  # 行不因收益缺失而丢
        assert panel["fret"].notna().sum() == 1

    def test_without_forward_return_no_fret_column(self, panel_env):
        """不传收益 → 不写 fret 列（旧版面板格式，读侧据此判「缺收益」）。"""
        rng = np.random.default_rng(5)
        write_panel("a_share", "fid_legacy", _series(["2026-01-05"], ["SH600000"], rng))
        panel = read_panel("a_share", "fid_legacy")
        assert "fret" not in panel.columns


class TestSampleDays:
    def test_caps_and_deterministic(self):
        days = [f"2026-{m:02d}-{d:02d}" for m in range(1, 13) for d in range(1, 29)]
        picked = sample_days(days, n=60)
        assert len(picked) == 60
        assert picked == sorted(picked)
        assert picked == sample_days(list(reversed(days)), n=60)  # 输入顺序无关

    def test_endpoints_included_when_truncating(self):
        days = [f"2026-01-{d:02d}" for d in range(1, 29)]
        picked = sample_days(days, n=5)
        assert picked[0] == "2026-01-01" and picked[-1] == "2026-01-28"

    def test_short_input_returned_asis(self):
        assert sample_days(["2026-01-01", "2026-01-02"], n=60) == [
            "2026-01-01",
            "2026-01-02",
        ]


def _panel_frame(days, symbols, values_by_day):
    rows = []
    for d in days:
        vals = values_by_day(d)
        ranks = pd.Series(vals).rank(pct=True)
        z = (vals - vals.mean()) / (vals.std() + 1e-12)
        for i, s in enumerate(symbols):
            rows.append((d, s, float(ranks.iloc[i]), float(z[i])))
    return pd.DataFrame(rows, columns=["trade_date", "symbol", "rank_pct", "zscore"])


class TestPairCorr:
    def _two(self, days, symbols, rng):
        a = pd.DataFrame(
            [
                (d, s, p, p * 2 - 1)
                for d in days
                for s, p in zip(
                    symbols, np.linspace(0.1, 0.9, len(symbols)), strict=True
                )
            ],
            columns=["trade_date", "symbol", "rank_pct", "zscore"],
        )
        b = a.copy()
        b["rank_pct"] = 1.0 - b["rank_pct"]  # 完全反序 → rho = -1
        return a, b

    def test_identical_factor_rho_one(self):
        days = [f"2026-01-{d:02d}" for d in range(1, 8)]
        symbols = [f"SH6000{i:02d}" for i in range(30)]
        rng = np.random.default_rng(1)
        a = _panel_frame(days, symbols, lambda d: pd.Series(rng.normal(size=30)))
        rho = pair_corr(a, a)
        assert rho is not None and rho[0] == pytest.approx(1.0)
        assert rho[1] >= 5

    def test_inverted_factor_rho_minus_one(self):
        days = [f"2026-01-{d:02d}" for d in range(1, 8)]
        symbols = [f"SH6000{i:02d}" for i in range(30)]
        rng = np.random.default_rng(2)
        a = _panel_frame(days, symbols, lambda d: pd.Series(rng.normal(size=30)))
        b = a.copy()
        b["rank_pct"] = b.groupby("trade_date")["rank_pct"].transform(lambda s: 1.0 - s)
        rho = pair_corr(a, b)
        assert rho is not None and rho[0] == pytest.approx(-1.0)

    def test_insufficient_days_returns_none(self):
        day = "2026-01-05"
        symbols = [f"SH6000{i:02d}" for i in range(30)]
        rng = np.random.default_rng(3)
        a = _panel_frame([day], symbols, lambda d: pd.Series(rng.normal(size=30)))
        assert pair_corr(a, a) is None  # 仅 1 个有效日 < 5

    def test_insufficient_pairs_per_day_none(self):
        days = [f"2026-01-{d:02d}" for d in range(1, 8)]
        symbols = [f"SH6000{i}" for i in range(10)]  # 每天 10 对 < 20
        rng = np.random.default_rng(4)
        a = _panel_frame(days, symbols, lambda d: pd.Series(rng.normal(size=10)))
        assert pair_corr(a, a) is None

    def test_no_overlap_returns_none(self):
        days = [f"2026-01-{d:02d}" for d in range(1, 8)]
        s1 = [f"SH6000{i}" for i in range(30)]
        s2 = [f"SZ0000{i}" for i in range(30)]
        rng = np.random.default_rng(5)
        a = _panel_frame(days, s1, lambda d: pd.Series(rng.normal(size=30)))
        b = _panel_frame(days, s2, lambda d: pd.Series(rng.normal(size=30)))
        assert pair_corr(a, b) is None


class TestCorrMatrix:
    def test_diagonal_one_and_symmetric(self):
        days = [f"2026-01-{d:02d}" for d in range(1, 8)]
        symbols = [f"SH6000{i:02d}" for i in range(40)]
        rng = np.random.default_rng(6)
        frames = {
            "a": _panel_frame(days, symbols, lambda d: pd.Series(rng.normal(size=40))),
            "b": _panel_frame(days, symbols, lambda d: pd.Series(rng.normal(size=40))),
        }
        m = corr_matrix(frames)
        assert list(m.index) == ["a", "b"] and list(m.columns) == ["a", "b"]
        assert m.loc["a", "a"] == pytest.approx(1.0)
        assert m.loc["a", "b"] == pytest.approx(m.loc["b", "a"])
