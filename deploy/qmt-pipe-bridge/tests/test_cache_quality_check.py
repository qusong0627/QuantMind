"""startup quality check 纯函数单测（不连 Redis/Windows）。"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parents[1] / "cache_quality_check.py"
_spec = importlib.util.spec_from_file_location("cache_quality_check", _MODULE_PATH)
qc = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = qc
_spec.loader.exec_module(qc)


def _levels(issues):
    return {issue.level for issue in issues}


def _codes(issues):
    return {issue.code for issue in issues}


def test_in_session_windows_and_weekend():
    assert qc.in_session(datetime(2026, 11, 27, 9, 20, tzinfo=qc.CN_TZ))
    assert qc.in_session(datetime(2026, 11, 27, 14, 59, tzinfo=qc.CN_TZ))
    assert not qc.in_session(datetime(2026, 11, 27, 12, 0, tzinfo=qc.CN_TZ))
    assert not qc.in_session(datetime(2026, 11, 28, 10, 0, tzinfo=qc.CN_TZ))


def test_config_healthy_and_risks():
    cfg = {
        "account_id": "40327478",
        "redis": {"host": "192.168.31.68", "port": 6379, "db": 5},
        "rpc": {"trade_min_interval_s": 3, "trade_burst": 3, "trade_read_timeout_s": 8},
        "budget": {"market_preempt_queue": 2, "order_preempt_queue": 3},
        "quotes": {"enabled": True},
        "sweep": {"enabled": True},
        "today_cache": {"enabled": True, "snapshot3s_enabled": True},
        "source_http": {"allow_orders": False},
        "watchdog": {"prefer_bridge_restart": True},
        "risk": {"max_order_amount": 200000, "max_orders_per_min": 30},
    }
    issues = qc.evaluate_config(cfg, pyarrow_version="25.0.1")
    assert not [i for i in issues if i.level == "FAIL"]

    cfg["account_id"] = ""
    cfg["source_http"]["allow_orders"] = True
    issues = qc.evaluate_config(cfg, pyarrow_version=None)
    assert "FAIL" in _levels(issues)
    assert "source_http.orders" in _codes(issues)
    assert "pyarrow" in _codes(issues)


def test_runtime_session_vs_offhours():
    metrics = {
        "strategy_state": "unresponsive",
        "positions_age_s": "-1",
        "asset_age_s": "-1",
        "queue_len": "0",
    }
    issues = qc.evaluate_runtime(metrics, session=True)
    assert "FAIL" in _levels(issues)
    issues = qc.evaluate_runtime(metrics, session=False)
    assert "FAIL" not in _levels(issues)
    assert "WARN" in _levels(issues)


def _metrics(**overrides):
    base = {
        "cache_enabled": "1",
        "cache_degraded": "0",
        "cache_errors": "0",
        "cache_dropped": "0",
        "cache_queue": "0",
        "cache_latest_symbols": "5225",
        "cache_last_ingest_age_s": "2.0",
        "cache_hot_symbols": "600",
        "cache_snapshot3s_rounds": "10",
    }
    base.update(overrides)
    return base


def test_cache_session_healthy_and_failures():
    issues = qc.evaluate_cache(_metrics(), session=True, source_ages=[0.5, 1.2])
    assert not issues

    issues = qc.evaluate_cache(
        _metrics(cache_latest_symbols="10"), session=True, source_ages=[0.5]
    )
    assert "FAIL" in _levels(issues)

    issues = qc.evaluate_cache(_metrics(), session=True, source_ages=[30.0])
    assert "FAIL" in _levels(issues)

    issues = qc.evaluate_cache(_metrics(), session=True, source_ages=[])
    assert "FAIL" in _levels(issues)

    issues = qc.evaluate_cache(
        _metrics(cache_degraded="1", cache_errors="3", cache_dropped="1"),
        session=False,
    )
    assert "WARN" in _levels(issues)
    assert "FAIL" not in _levels(issues)


def test_cache_disabled_is_fail():
    issues = qc.evaluate_cache(
        _metrics(cache_enabled="0"), session=False
    )
    assert "FAIL" in _levels(issues)


def test_parquet_validation():
    issues = qc.evaluate_parquet({"bar1m": None}, session=True)
    assert "WARN" in _levels(issues)
    issues = qc.evaluate_parquet(
        {
            "bar1m": {
                "rows": 10,
                "columns": ["date", "symbol", "minute_ts"],
                "bad_rows": 0,
            }
        },
        session=True,
    )
    assert "FAIL" in _levels(issues)
    issues = qc.evaluate_parquet(
        {
            "snapshot3s": {
                "rows": 10,
                "columns": list(qc.REQUIRED_COLUMNS["snapshot3s"]),
                "bad_rows": 2,
            }
        },
        session=True,
    )
    assert "WARN" in _levels(issues)


def test_divergence_detects_price_gap():
    now = 1000.0
    pairs = [
        {
            "symbol": "600036.SH",
            "qmt_last": 42.70,
            "tdx_last": 42.71,
            "qmt_ts": now - 1,
            "tdx_ts": now - 1,
        },
        {
            "symbol": "000001.SZ",
            "qmt_last": 12.00,
            "tdx_last": 12.50,
            "qmt_ts": now - 1,
            "tdx_ts": now - 1,
        },
        {
            "symbol": "600519.SH",
            "qmt_last": 1400.0,
            "tdx_last": 1300.0,
            "qmt_ts": now - 30,
            "tdx_ts": now - 1,
        },
    ]
    issues, summary = qc.evaluate_divergence(pairs, now=now)
    assert summary["pairs"] == 2  # stale qmt 的不算
    assert summary["over_threshold"] == 1
    assert "WARN" in _levels(issues)


def test_divergence_no_pairs_is_warn():
    issues, summary = qc.evaluate_divergence([], now=1000.0)
    assert summary["pairs"] == 0
    assert "WARN" in _levels(issues)
    assert "divergence.none" in _codes(issues)
