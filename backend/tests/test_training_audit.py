"""WP7 数据审计：未来列/标签契约/trade_mask 覆盖 + 晋升闸。"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from backend.services.engine.services.model_rollout_service import audit_gate
from backend.services.engine.training import pit_audit_service as pas


def _sandbox(tmp_path: Path, monkeypatch, *, features=None, label_formula=None, mask_days=None):
    data_root = tmp_path
    qdb = data_root / "quantdb"
    (qdb / "factor_research" / "st_pit_cache").mkdir(parents=True)
    (qdb / "5_technical_derived" / "trade_mask").mkdir(parents=True)
    run_dir = data_root / "training_jobs" / "train_x"
    run_dir.mkdir(parents=True)

    meta = {
        "features": features if features is not None else ["mom_ret_5d", "concept_leader_score"],
        "label_formula": label_formula
        if label_formula is not None
        else "label = close(T+11) / open(T+1) - 1",
        "target_horizon_days": 10,
        "execution_lag_days": 1,
        "data_fingerprint": {"labels_max_date": "2026-09-18"},
    }
    (run_dir / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    pd.DataFrame(
        {
            "symbol": ["SH600000", "SH600000", "SZ000001", "SZ000001"],
            "trade_date": pd.to_datetime(["2026-01-05", "2026-01-06", "2026-01-05", "2026-01-06"]),
        }
    ).to_parquet(run_dir / "pred.parquet")

    days = mask_days if mask_days is not None else ["20260105", "20260106"]
    pd.DataFrame(
        {
            "dt": [int(d) for d in days],
            "symbol": ["600000.SH"] * len(days),
            "blocked_long": [False] * len(days),
        }
    ).to_parquet(qdb / "5_technical_derived" / "trade_mask" / "trade_mask.parquet")
    pd.DataFrame({"symbol": ["600000.SH"]}).to_parquet(qdb / "factor_research" / "stock_snapshot.parquet")
    for sym in ("600000.SH", "000001.SZ"):
        pd.DataFrame({"time": ["20260105"], "is_st": [False]}).to_parquet(
            qdb / "factor_research" / "st_pit_cache" / f"{sym}.parquet"
        )
    monkeypatch.setattr(pas, "DATA_ROOT", data_root)
    monkeypatch.setattr(pas, "QDB", qdb)
    monkeypatch.setattr(pas, "TRADE_MASK", qdb / "5_technical_derived" / "trade_mask" / "trade_mask.parquet")
    monkeypatch.setattr(pas, "ST_CACHE", qdb / "factor_research" / "st_pit_cache")
    monkeypatch.setattr(pas, "PIT_PROBE", qdb / "factor_research" / "pit_audit.json")
    return run_dir


def _by_id(report):
    return {c["id"]: c for c in report["checks"]}


def test_clean_run_passes(tmp_path, monkeypatch):
    _sandbox(tmp_path, monkeypatch)
    report = pas.build_audit("train_x")
    checks = _by_id(report)
    assert report["blocked"] is False
    assert checks["future_feature_names"]["status"] == "pass"
    assert checks["label_contract"]["status"] == "pass"
    assert checks["trade_mask_coverage"]["status"] == "pass"
    assert checks["survivorship_universe"]["status"] == "pass"  # 无历史分区样本


def test_future_feature_blocks(tmp_path, monkeypatch):
    _sandbox(tmp_path, monkeypatch, features=["mom_ret_5d", "return_5d"])
    report = pas.build_audit("train_x")
    assert report["blocked"] is True
    assert "return_5d" in _by_id(report)["future_feature_names"]["detail"]


def test_bad_label_contract_blocks(tmp_path, monkeypatch):
    _sandbox(tmp_path, monkeypatch, label_formula="label = close(T+11)/close(T) - 1")
    report = pas.build_audit("train_x")
    assert report["blocked"] is True
    assert _by_id(report)["label_contract"]["status"] == "fail"


def test_missing_label_formula_is_warning_not_block(tmp_path, monkeypatch):
    _sandbox(tmp_path, monkeypatch, label_formula="")
    report = pas.build_audit("train_x")
    assert report["blocked"] is False
    check = _by_id(report)["label_contract"]
    assert check["level"] == "warn" and check["status"] == "fail"


def test_trade_mask_missing_days_blocks(tmp_path, monkeypatch):
    _sandbox(tmp_path, monkeypatch, mask_days=["20260105", "20260106", "20260107"])
    # 删掉 20260106，制造缺口（缺口 1/2 > 2%）
    mask_path = tmp_path / "quantdb" / "5_technical_derived" / "trade_mask" / "trade_mask.parquet"
    pd.DataFrame({"dt": [20260105], "symbol": ["600000.SH"], "blocked_long": [False]}).to_parquet(mask_path)
    report = pas.build_audit("train_x")
    assert _by_id(report)["trade_mask_coverage"]["status"] == "fail"
    assert report["blocked"] is True


def test_audit_gate_blocks_missing_and_failed():
    blocked, reason, _ = audit_gate(source_run_id="train_x", audit_row=None)
    assert blocked and "audit-report" in reason

    failed = {
        "status": "ready",
        "report": {
            "blocked": True,
            "checks": [
                {"id": "future_feature_names", "level": "fail", "status": "fail", "detail": "return_5d"}
            ],
            "warnings": [],
        },
    }
    blocked, reason, _ = audit_gate(source_run_id="train_x", audit_row=failed)
    assert blocked and "future_feature_names" in reason

    blocked, _, warnings = audit_gate(
        source_run_id="train_x", audit_row=failed, allow_fail=True
    )
    assert blocked is False
    assert any("allow_audit_fail" in w for w in warnings)


def test_audit_gate_no_run_skips():
    blocked, _, warnings = audit_gate(source_run_id="", audit_row=None)
    assert blocked is False
    assert warnings
