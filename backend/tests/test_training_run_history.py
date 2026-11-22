"""训练记录列表摘要（2026-11-22 模型训练历史）。"""

from __future__ import annotations

from datetime import datetime

from backend.services.api.routers.model_training import _training_run_summary


def _row(**over):
    base = {
        "id": "train_20261120050719_test",
        "status": "completed",
        "progress": 100,
        "created_at": datetime(2026, 11, 20, 5, 7, 19),
        "updated_at": datetime(2026, 11, 20, 5, 41, 59),
        "request_payload": {
            "display_name": "LGB_20_T10_Alpha152_Base_CN",
            "model_type": "lightgbm",
            "target_horizon_days": 10,
            "features": [f"f{i}" for i in range(152)],
            "context": {"market": "CN", "benchmark": "SH000300"},
        },
        "result": {
            "metrics": {
                "test": {"rank_ic": 0.101, "icir": 0.688, "auc": 0.53, "rmse": 0.99},
                "val": {"rank_ic": 0.12, "icir": 0.8},
            },
            "metadata": {
                "feature_count": 45,
                "model_id": "mdl_x",
                "eval_report": {
                    "by_split": {
                        "test": {"mean": 0.101, "icir": 0.688},
                    }
                },
            },
        },
        "logs": "epoch 1 ...",
    }
    base.update(over)
    return base


def test_summary_extracts_metrics_duration_and_logs():
    out = _training_run_summary(_row())
    assert out["run_id"] == "train_20261120050719_test"
    assert out["display_name"] == "LGB_20_T10_Alpha152_Base_CN"
    assert out["market"] == "CN"
    assert out["target_horizon_days"] == 10
    assert out["feature_count"] == 45
    assert out["duration_seconds"] == 2080.0
    assert out["metrics"]["test_rank_ic"] == 0.101
    assert out["metrics"]["test_icir"] == 0.688
    assert out["metrics"]["val_rank_ic"] == 0.12
    assert out["has_result"] is True
    assert out["has_logs"] is True
    assert out["logs_chars"] == len("epoch 1 ...")
    assert out["strict"] is None


def test_summary_rank_ic_falls_back_to_eval_report():
    row = _row()
    row["result"] = {
        "metrics": {"test": {}, "val": {}},
        "metadata": {
            "feature_count": 10,
            "eval_report": {"by_split": {"test": {"mean": 0.087, "icir": 1.2}}},
        },
    }
    out = _training_run_summary(row)
    assert out["metrics"]["test_rank_ic"] == 0.087
    assert out["metrics"]["test_icir"] == 1.2


def test_summary_failed_run_without_result():
    out = _training_run_summary(
        _row(status="failed", result=None, logs="", progress=42)
    )
    assert out["status"] == "failed"
    assert out["progress"] == 42
    assert out["has_result"] is False
    assert out["has_logs"] is False
    assert out["metrics"]["test_rank_ic"] is None


def test_summary_uses_precomputed_logs_chars_without_pulling_logs():
    row = _row()
    row.pop("logs")
    row["logs_chars"] = 12345
    out = _training_run_summary(row)
    assert out["has_logs"] is True
    assert out["logs_chars"] == 12345


def test_summary_strict_headline():
    strict_row = {
        "status": "ready",
        "report": {
            "results": {
                "test": {
                    "top20": {
                        "batches": 10,
                        "turnover_per_batch": 0.5,
                        "cost_25bp": {
                            "total": 0.01,
                            "mdd": -0.05,
                            "excess_hs300": 0.02,
                            "hac_t": 2.1,
                            "psr": 0.8,
                        },
                    }
                }
            },
            "segments": {"test": {"start": "20260411", "end": "20260930"}},
        },
    }
    out = _training_run_summary(_row(), strict_row)
    assert out["strict"]["status"] == "ready"
    assert out["strict"]["segment"] == "test"
    assert out["strict"]["batches"] == 10
    assert out["strict"]["total_25bp"] == 0.01
    assert out["strict"]["excess_hs300"] == 0.02
