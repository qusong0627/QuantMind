"""store 窗口化读取 —— 年分区、窗口缓存键、个股快照 as-of 回归。

这些路径错了不会报错：2019 的请求静默读到 2020 的 Panel，或历史持仓画像
拿今天的最新市值/行业，回测数字看起来完全正常。故逐条钉住。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backend.services.engine.factor_research import store, windows as W


@pytest.fixture(autouse=True)
def _clear_cache():
    store._cache.clear()
    store._panel_cache.clear()
    yield
    store._cache.clear()
    store._panel_cache.clear()


def _panel_df() -> pd.DataFrame:
    rows = []
    for date, fwd in (("2019-12-31", 0.01), ("2020-01-23", 0.02), ("2021-01-29", 0.03)):
        for rk, sym in enumerate(["A", "B"], 1):
            rows.append(
                {
                    "factor_code": "f1",
                    "trade_date": pd.Timestamp(date),
                    "rank": rk,
                    "symbol": sym,
                    "score": float(rk),
                    "raw": float(rk),
                    "fwd_ret": fwd,
                }
            )
    return pd.DataFrame(rows)


def test_panel_只加载请求窗口的年份(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "artifact_dir", lambda *_a, **_k: tmp_path)
    W.write_year_partitions(_panel_df(), tmp_path, W.PANEL_DIR)

    p = store.panel("classic", "CN", "2019-01", "2019-12")
    assert p is not None
    assert [str(pd.Timestamp(d).date()) for d in p.dates] == ["2019-12-31"]

    p_all = store.panel("classic", "CN")
    assert len(p_all.dates) == 3


def test_panel_stamp_随窗口变化(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "artifact_dir", lambda *_a, **_k: tmp_path)
    W.write_year_partitions(_panel_df(), tmp_path, W.PANEL_DIR)

    s1 = store.panel_stamp("classic", "CN", "2019-01", "2019-12")
    s2 = store.panel_stamp("classic", "CN", "2020-01", "2020-12")
    assert s1 != s2


def test_scores_for_private_只读相交年份(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "artifact_dir", lambda *_a, **_k: tmp_path)
    d = tmp_path / W.SCORES_DIR
    d.mkdir(parents=True)
    pd.DataFrame(
        {
            "trade_date": [pd.Timestamp("2019-12-31"), pd.Timestamp("2020-01-23")],
            "symbol": ["A", "A"],
            "f1": [1.0, 2.0],
            "f2": [3.0, 4.0],
        }
    ).to_parquet(d / "year=2019.parquet", index=False)
    pd.DataFrame(
        {
            "trade_date": [pd.Timestamp("2020-01-23")],
            "symbol": ["A"],
            "f1": [2.0],
            "f2": [4.0],
        }
    ).to_parquet(d / "year=2020.parquet", index=False)

    got = store.scores_for(["f1"], "private", "CN", "2019-01", "2019-12")
    assert got is not None and len(got) == 1
    assert float(got.iloc[0]["score"]) == 1.0

    # 年份文件缺该列时跳过，不因单列缺失整批失败
    got_missing = store.scores_for(["not_there"], "private", "CN", "2019-01", "2019-12")
    assert got_missing is not None and got_missing.empty


def test_stock_snapshot_as_of_读当年截面(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "artifact_dir", lambda *_a, **_k: tmp_path)
    d = tmp_path / W.SNAPSHOT_DIR
    d.mkdir(parents=True)
    pd.DataFrame({"symbol": ["A"], "name": ["old"], "total_mv_yi": [10.0]}).to_parquet(
        d / "year=2019.parquet", index=False
    )
    pd.DataFrame({"symbol": ["A"], "name": ["new"], "total_mv_yi": [99.0]}).to_parquet(
        d / "year=2020.parquet", index=False
    )

    old = store.stock_snapshot("CN", as_of="2019-06-30")
    assert old is not None and old.iloc[0]["name"] == "old"
    new = store.stock_snapshot("CN", as_of="2020-06-30")
    assert new is not None and new.iloc[0]["name"] == "new"


def test_clip_dates_end_含当月最后一天():
    df = pd.DataFrame({"trade_date": pd.to_datetime(["2019-12-31", "2020-01-02"])})
    got = W.clip_dates(df, "trade_date", "2019-01", "2019-12")
    assert list(got["trade_date"]) == [pd.Timestamp("2019-12-31")]


def test_range_meta_退市缺口按当前窗口切片判定():
    """全库 flag 不能盖到所有年份：2016 缺口大、2020 覆盖好，各自判定。"""
    from backend.services.engine.factor_research import service

    months = [
        # 逐库审计口径优先：并集 missing_ratio=0（PIT 库盖住退市股），但最差库缺 10%
        {
            "date": "2016-07-29",
            "missing_ratio": 0.0,
            "worst_library_missing": 0.10,
            "kline_symbols": 2500,
        },
        {
            "date": "2020-01-23",
            "missing_ratio": 0.0,
            "worst_library_missing": 0.0,
            "kline_symbols": 3700,
        },
    ]
    base = {
        "market": "CN",
        "snapshot_meta": {},
        "coverage_summary": {
            "survivorship_threshold": 0.05,
            "survivorship_bias": True,
            "window": ["2016-07-29", "2026-11-05"],
        },
        "coverage_months": months,
    }
    old = {**base, "rdates": np.array([np.datetime64("2016-07-29")])}
    new = {**base, "rdates": np.array([np.datetime64("2020-01-23")])}
    assert service._range_meta(old)["survivorship_bias"] is True
    assert service._range_meta(new)["survivorship_bias"] is False


def test_year_partition_gap_raises_instead_of_silent_short_panel(tmp_path, monkeypatch):
    """2019 与 2021 有分区、2020 缺：请求 2019–2021 必须报错，不能静默少一年。"""
    monkeypatch.setattr(store, "artifact_dir", lambda *_a, **_k: tmp_path)
    rows = []
    for date in ("2019-12-31", "2021-01-29"):
        rows.append(
            {
                "factor_code": "f1",
                "trade_date": pd.Timestamp(date),
                "rank": 1,
                "symbol": "A",
                "score": 1.0,
                "raw": 1.0,
                "fwd_ret": 0.01,
            }
        )
    W.write_year_partitions(pd.DataFrame(rows), tmp_path, W.PANEL_DIR)

    assert W.missing_years(tmp_path / W.PANEL_DIR, "2019-01", "2021-12") == [2020]
    with pytest.raises(store.SnapshotMissing, match="2020"):
        store.panel("classic", "CN", "2019-01", "2021-12")


def test_panel_cache_按窗口有界(tmp_path, monkeypatch):
    """窗口化后 Panel 最大 1.4G/份；缓存必须只留最近 2 个窗口。"""
    monkeypatch.setattr(store, "artifact_dir", lambda *_a, **_k: tmp_path)
    rows = []
    for date in ("2019-12-31", "2020-12-31", "2021-12-31"):
        rows.append(
            {
                "factor_code": "f1",
                "trade_date": pd.Timestamp(date),
                "rank": 1,
                "symbol": "A",
                "score": 1.0,
                "raw": 1.0,
                "fwd_ret": 0.01,
            }
        )
    W.write_year_partitions(pd.DataFrame(rows), tmp_path, W.PANEL_DIR)
    for y in (2019, 2020, 2021):
        store.panel("classic", "CN", f"{y}-01", f"{y}-12")
    assert len(store._panel_cache) <= store._MAX_PANELS


def test_panel_请求区间在快照窗口外_返回None(tmp_path, monkeypatch):
    """不能对空 DataFrame 构造 Panel（int(max)=nan 会 500）；窗口外返回 None 让路由译 503。"""
    monkeypatch.setattr(store, "artifact_dir", lambda *_a, **_k: tmp_path)
    W.write_year_partitions(_panel_df(), tmp_path, W.PANEL_DIR)
    assert store.panel("classic", "CN", "2015-01", "2015-12") is None
