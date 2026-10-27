"""QuantFutures 期权 EOD 数据集测试（不联网）。"""

from __future__ import annotations

import importlib

import pandas as pd
import pytest

from backend.scripts import akshare_futures_options as opt


def _risk_raw():
    return pd.DataFrame(
        [
            {
                "TRADE_DATE": "2026-10-27",
                "SECURITY_ID": 10012635,
                "CONTRACT_ID": "510050C2610M02650",
                "CONTRACT_SYMBOL": "50ETF购10月2650",
                "DELTA_VALUE": 0.984,
                "THETA_VALUE": -0.824,
                "GAMMA_VALUE": 0.383,
                "VEGA_VALUE": 0.006,
                "RHO_VALUE": 0.007,
                "IMPLC_VOLATLTY": 0.691,
            },
            {
                "TRADE_DATE": "2026-10-27",
                "SECURITY_ID": 10012535,
                "CONTRACT_ID": "510050P2610M02700",
                "CONTRACT_SYMBOL": "50ETF沽10月2700",
                "DELTA_VALUE": -0.1,
                "THETA_VALUE": -0.2,
                "GAMMA_VALUE": 0.01,
                "VEGA_VALUE": 0.02,
                "RHO_VALUE": 0.0,
                "IMPLC_VOLATLTY": 0.0,
            },
            {
                "TRADE_DATE": "2026-10-27",
                "SECURITY_ID": 999,
                "CONTRACT_ID": "NOT_A_CONTRACT",
                "CONTRACT_SYMBOL": "坏数据",
                "DELTA_VALUE": 0.0,
                "THETA_VALUE": 0.0,
                "GAMMA_VALUE": 0.0,
                "VEGA_VALUE": 0.0,
                "RHO_VALUE": 0.0,
                "IMPLC_VOLATLTY": 0.0,
            },
        ]
    )


def test_norm_risk_parses_contract_and_zero_iv():
    out = opt._norm_risk(_risk_raw(), "20261027")

    assert len(out) == 2  # 非法合约被丢弃
    assert list(out.columns) == opt._RISK_COLS
    call = out[out["contract_id"] == "510050C2610M02650"].iloc[0]
    assert call["underlying"] == "510050"
    assert call["option_type"] == "call"
    assert call["expiry_month"] == "2026-10"
    assert call["strike"] == pytest.approx(2.650)
    assert call["implied_vol"] == pytest.approx(0.691)
    put = out[out["contract_id"] == "510050P2610M02700"].iloc[0]
    assert put["option_type"] == "put"
    assert pd.isna(put["implied_vol"])  # 0 IV 不是有效值


def test_norm_stats_sse_pcr_and_amount():
    raw = pd.DataFrame(
        [
            {
                "合约标的代码": "510050",
                "合约标的名称": "上证50ETF华夏",
                "合约数量": 102,
                "总成交额": 26317,
                "总成交量": 691741,
                "认购成交量": 388473,
                "认沽成交量": 303268,
                "认沽/认购": 78.07,
                "未平仓合约总数": 1416655,
                "未平仓认购合约数": 908817,
                "未平仓认沽合约数": 507838,
                "交易日": "2026-10-27",
            }
        ]
    )
    out = opt._norm_stats(raw, "SSE", "20261027").iloc[0]

    assert out["exchange"] == "SSE"
    assert out["underlying"] == "510050"
    assert out["amount_wan"] == pytest.approx(26317)
    assert out["pcr_volume"] == pytest.approx(303268 / 388473)
    assert out["pcr_oi"] == pytest.approx(507838 / 908817)
    assert out["trade_date"] == pd.Timestamp("2026-10-27")


def test_norm_stats_szse_maps_oi_ratio():
    raw = pd.DataFrame(
        [
            {
                "合约标的代码": "159915",
                "合约标的名称": "创业板ETF易方达",
                "成交量": 2762319,
                "认购成交量": 1632656,
                "认沽成交量": 1129663,
                "认沽/认购持仓比": 40.11,
                "未平仓合约总数": 2326084,
                "未平仓认购合约数": 1660184,
                "未平仓认沽合约数": 665900,
                "交易日": "2026-10-27",
            }
        ]
    )
    out = opt._norm_stats(raw, "SZSE", "20261027").iloc[0]

    assert out["exchange"] == "SZSE"
    assert out["underlying"] == "159915"
    assert pd.isna(out["amount_wan"])  # 深交所汇总无成交额
    assert out["pcr_volume"] == pytest.approx(1129663 / 1632656)
    assert out["pcr_oi"] == pytest.approx(665900 / 1660184)


def test_write_partition(tmp_path):
    df = pd.DataFrame({"trade_date": [pd.Timestamp("2026-10-27")]})
    opt._write_part(tmp_path, "20261027", df)
    got = pd.read_parquet(tmp_path / "dt=20261027" / "data.parquet")
    assert len(got) == 1
    assert not list(tmp_path.glob("dt=20261027/.data.*.tmp"))


def test_trading_day_calendar_uses_daily_forward(tmp_path, monkeypatch):
    base = tmp_path / "1_kline_data" / "daily_forward"
    for d in ("20261023", "20261026", "20261027"):
        (base / f"dt={d}").mkdir(parents=True)
    monkeypatch.setattr(opt, "FUT_DATA_DIR", tmp_path)

    assert opt.recent_trading_days(2) == ["20261026", "20261027"]
    assert opt.trading_days_between("2026-10-24", "2026-10-27") == [
        "20261026",
        "20261027",
    ]


def test_catalog_and_dispatcher_include_options():
    from backend.scripts.quantfutures_daily_sync import (
        DEFAULT_EXTRA_DATASETS,
        EXTRA_DATASET_TASKS,
        _EXTRA_MODULES,
    )
    from backend.services.api.routers.admin.global_market_console import (
        _default_datasets,
    )

    specs = {s.dataset: s for s in _default_datasets("FUTURES")}
    assert specs["options_risk"].rel_dir == "2_base_sector/options_risk"
    assert specs["options_stats"].rel_dir == "2_base_sector/options_stats"
    assert specs["options_risk"].layout == "partition"

    assert {"options_risk", "options_stats"} <= set(DEFAULT_EXTRA_DATASETS)
    for ds, task in (
        ("options_risk", "options_risk"),
        ("options_stats", "options_stats"),
    ):
        mod_key, fn_name = EXTRA_DATASET_TASKS[ds]
        assert fn_name == task
        mod = importlib.import_module(_EXTRA_MODULES[mod_key])
        assert callable(getattr(mod, f"task_{task}", None))


def test_fetch_stats_day_calls_both_exchanges(monkeypatch):
    import sys
    import types

    sse = pd.DataFrame(
        [
            {
                "合约标的代码": "510050",
                "合约标的名称": "上证50ETF华夏",
                "合约数量": 102,
                "总成交额": 26317,
                "总成交量": 100,
                "认购成交量": 60,
                "认沽成交量": 40,
                "认沽/认购": 66.67,
                "未平仓合约总数": 200,
                "未平仓认购合约数": 120,
                "未平仓认沽合约数": 80,
                "交易日": "2026-10-27",
            }
        ]
    )
    szse = pd.DataFrame(
        [
            {
                "合约标的代码": "159915",
                "合约标的名称": "创业板ETF易方达",
                "成交量": 300,
                "认购成交量": 200,
                "认沽成交量": 100,
                "认沽/认购持仓比": 50.0,
                "未平仓合约总数": 400,
                "未平仓认购合约数": 250,
                "未平仓认沽合约数": 150,
                "交易日": "2026-10-27",
            }
        ]
    )
    fake = types.SimpleNamespace(option_daily_stats_sse=lambda date: sse)
    monkeypatch.setitem(sys.modules, "akshare", fake)
    monkeypatch.setattr(opt, "_fetch_szse_stats_raw", lambda date: szse)

    df, errs = opt._fetch_stats_day("20261027")
    assert errs == []
    assert set(df["exchange"]) == {"SSE", "SZSE"}
    assert set(df["underlying"]) == {"510050", "159915"}


def test_fetch_stats_day_skips_szse_before_launch(monkeypatch):
    import sys
    import types

    sse = pd.DataFrame(
        [
            {
                "合约标的代码": "510050",
                "合约标的名称": "上证50ETF华夏",
                "合约数量": 1,
                "总成交额": 1,
                "总成交量": 10,
                "认购成交量": 6,
                "认沽成交量": 4,
                "认沽/认购": 66.67,
                "未平仓合约总数": 20,
                "未平仓认购合约数": 12,
                "未平仓认沽合约数": 8,
                "交易日": "2016-01-05",
            }
        ]
    )

    def _must_not_call(date):
        raise AssertionError("SZSE should be skipped before 20191223")

    fake = types.SimpleNamespace(
        option_daily_stats_sse=lambda date: sse,
        option_daily_stats_szse=_must_not_call,
    )
    monkeypatch.setitem(sys.modules, "akshare", fake)

    df, errs = opt._fetch_stats_day("20160105")
    assert errs == []
    assert set(df["exchange"]) == {"SSE"}


def test_sync_dates_write_skip_empty_error(tmp_path, monkeypatch):
    monkeypatch.setattr(opt, "FUT_DATA_DIR", tmp_path)
    payload = pd.DataFrame({"trade_date": [pd.Timestamp("2026-10-27")]})
    monkeypatch.setattr(opt, "_fetch_risk_day", lambda d: (payload, []))
    res = opt._sync_dates(["20261027"], "risk", overwrite=True, sleep=0)
    assert res["written"] == 1
    assert (
        tmp_path / "2_base_sector" / "options_risk" / "dt=20261027" / "data.parquet"
    ).exists()

    res = opt._sync_dates(["20261027"], "risk", overwrite=False, sleep=0)
    assert res["skipped"] == 1

    monkeypatch.setattr(opt, "_fetch_risk_day", lambda d: (pd.DataFrame(), []))
    res = opt._sync_dates(["20261026"], "risk", overwrite=True, sleep=0)
    assert res["empty"] == 1

    def _boom(d):
        raise RuntimeError("network down")

    monkeypatch.setattr(opt, "_fetch_risk_day", _boom)
    res = opt._sync_dates(["20261023"], "risk", overwrite=True, sleep=0)
    assert res["status"] == "error"
    assert res["error_count"] == 1


def test_backfill_dispatches_selected_tasks(monkeypatch):
    monkeypatch.setattr(opt, "trading_days_between", lambda start, end: ["20261027"])

    def _fake_sync(dates, kind, *, overwrite, sleep):
        return {"status": "ok", "dataset": f"options_{kind}", "dates": len(list(dates))}

    monkeypatch.setattr(opt, "_sync_dates", _fake_sync)
    out = opt.backfill(start="2026-10-27", end="2026-10-27", task="all", sleep=0)
    assert set(out) == {"start", "end", "dates", "risk", "stats"}
    assert out["risk"]["status"] == "ok"
    assert out["stats"]["status"] == "ok"


def test_data_dir_env(monkeypatch, tmp_path):
    monkeypatch.setenv("QM_QUANTFUTURES_DATA_DIR", str(tmp_path))
    assert opt._data_dir() == tmp_path


def test_retry_succeeds_after_transient_failure():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 2:
            raise ValueError("transient")
        return 42

    assert opt._retry(flaky, tries=3, delay=0) == 42
    assert calls["n"] == 2


def test_recent_trading_days_requires_partitions(tmp_path, monkeypatch):
    monkeypatch.setattr(opt, "FUT_DATA_DIR", tmp_path)
    with pytest.raises(RuntimeError):
        opt.recent_trading_days(5)


def test_recent_trading_days_zero_returns_all(tmp_path, monkeypatch):
    base = tmp_path / "1_kline_data" / "daily_forward"
    for d in ("20261026", "20261027"):
        (base / f"dt={d}").mkdir(parents=True)
    monkeypatch.setattr(opt, "FUT_DATA_DIR", tmp_path)
    assert opt.recent_trading_days(0) == ["20261026", "20261027"]


def test_trading_days_between_falls_back_to_business_days(monkeypatch):
    monkeypatch.setattr(opt, "_partition_dates", lambda: [])
    assert opt.trading_days_between("2026-10-24", "2026-10-27") == [
        "20261026",
        "20261027",
    ]


def test_write_part_cleans_on_error(tmp_path, monkeypatch):
    def boom(self, path, index=False):
        raise RuntimeError("disk full")

    monkeypatch.setattr(pd.DataFrame, "to_parquet", boom)
    with pytest.raises(RuntimeError):
        opt._write_part(tmp_path, "20261027", pd.DataFrame({"a": [1]}))
    assert not (tmp_path / "dt=20261027").exists()


def test_norm_empty_returns_empty_schema():
    assert opt._norm_risk(pd.DataFrame(), "20261027").empty
    assert opt._norm_stats(pd.DataFrame(), "SSE", "20261027").empty


def test_norm_risk_adds_missing_columns():
    raw = pd.DataFrame([{"CONTRACT_ID": "510050C2610M02650"}])
    out = opt._norm_risk(raw, "20261027").iloc[0]
    assert out["contract_id"] == "510050C2610M02650"
    assert pd.isna(out["delta"])
    assert pd.isna(out["implied_vol"])


def test_fetch_risk_day_uses_akshare(monkeypatch):
    import sys
    import types

    fake = types.SimpleNamespace(option_risk_indicator_sse=lambda date: _risk_raw())
    monkeypatch.setitem(sys.modules, "akshare", fake)
    df, errs = opt._fetch_risk_day("20261027")
    assert errs == []
    assert len(df) == 2


def test_fetch_stats_day_partial_error(monkeypatch):
    import sys
    import types

    sse = pd.DataFrame(
        [
            {
                "合约标的代码": "510050",
                "合约标的名称": "上证50ETF华夏",
                "合约数量": 1,
                "总成交额": 1,
                "总成交量": 10,
                "认购成交量": 6,
                "认沽成交量": 4,
                "认沽/认购": 66.67,
                "未平仓合约总数": 20,
                "未平仓认购合约数": 12,
                "未平仓认沽合约数": 8,
                "交易日": "2026-10-27",
            }
        ]
    )

    def _fail(date):
        raise RuntimeError("szse down")

    fake = types.SimpleNamespace(option_daily_stats_sse=lambda date: sse)
    monkeypatch.setitem(sys.modules, "akshare", fake)
    monkeypatch.setattr(opt, "_fetch_szse_stats_raw", _fail)
    df, errs = opt._fetch_stats_day("20261027")
    assert len(errs) == 1
    assert len(df) == 1


def test_fetch_stats_day_all_empty(monkeypatch):
    import sys
    import types

    fake = types.SimpleNamespace(option_daily_stats_sse=lambda date: pd.DataFrame())
    monkeypatch.setitem(sys.modules, "akshare", fake)
    monkeypatch.setattr(opt, "_fetch_szse_stats_raw", lambda date: pd.DataFrame())
    df, errs = opt._fetch_stats_day("20261027")
    assert df.empty
    assert errs == []


def test_task_options_risk_and_stats_dispatch(monkeypatch):
    monkeypatch.setattr(opt, "recent_trading_days", lambda days: ["20261027"])
    monkeypatch.setattr(
        opt,
        "_sync_dates",
        lambda dates, kind, **kwargs: {"kind": kind},
    )
    assert opt.task_options_risk(1)["kind"] == "risk"
    assert opt.task_options_stats(1)["kind"] == "stats"


def test_sync_dates_partial_status(tmp_path, monkeypatch):
    monkeypatch.setattr(opt, "FUT_DATA_DIR", tmp_path)
    monkeypatch.setattr(
        opt,
        "_fetch_risk_day",
        lambda d: (
            pd.DataFrame({"trade_date": [pd.Timestamp("2026-10-27")]}),
            ["warn"],
        ),
    )
    res = opt._sync_dates(["20261027"], "risk", overwrite=True, sleep=0)
    assert res["status"] == "partial"
    assert res["written"] == 1


def test_cli_backfill(monkeypatch, capsys):
    monkeypatch.setattr(opt, "backfill", lambda **kwargs: {"ok": True})
    monkeypatch.setattr(
        "sys.argv", ["prog", "--start", "2026-10-27", "--end", "2026-10-27"]
    )
    assert opt._cli() == 0
    assert '"ok": true' in capsys.readouterr().out


def test_fetch_risk_day_holiday_keyerror_is_empty(monkeypatch):
    import sys
    import types

    def _no_data(date):
        raise KeyError("None of [Index([...])] are in the [columns]")

    fake = types.SimpleNamespace(option_risk_indicator_sse=_no_data)
    monkeypatch.setitem(sys.modules, "akshare", fake)
    df, errs = opt._fetch_risk_day("20160101")
    assert df.empty
    assert errs == []


def test_request_timeout_restores_requests_get():
    import requests

    before = requests.get
    with opt._request_timeout(1.0):
        assert requests.get is not before
    assert requests.get is before


def test_fetch_szse_stats_raw_parses_payload(monkeypatch):
    class _Resp:
        def json(self):
            return [
                {
                    "data": [
                        {
                            "bddm": "159915",
                            "bdmc": "创业板ETF易方达",
                            "cjl": "2,762,319",
                            "rccjl": "1,632,656",
                            "rpcjl": "1,129,663",
                            "rcrpccb": "40.11",
                            "wpchyzs": "2,326,084",
                            "wpcrchys": "1,660,184",
                            "wpcrphys": "665,900",
                        }
                    ]
                }
            ]

    monkeypatch.setattr(opt.requests, "get", lambda *args, **kwargs: _Resp())
    raw = opt._fetch_szse_stats_raw("20261027")
    assert raw.loc[0, "合约标的代码"] == "159915"
    assert raw.loc[0, "成交量"] == "2,762,319"
    norm = opt._norm_stats(raw, "SZSE", "20261027").iloc[0]
    assert norm["volume"] == pytest.approx(2762319)
    assert norm["pcr_volume"] == pytest.approx(1129663 / 1632656)
