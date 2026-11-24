"""WP6 run 对比：配置 diff + 单轴判定 + 严格评估摘要。"""

from __future__ import annotations

from backend.services.engine.training.run_compare import (
    canonical_config,
    config_diff,
    diff_axes,
    is_single_axis,
    summarize_strict,
)


def _payload(**overrides):
    base = {
        "job_name": "a",
        "generated_at": "2026-11-20T00:00:00Z",
        "features": ["b", "a"],
        "train_start": "2016-01-01",
        "train_end": "2024-12-31",
        "lgb_params": {"objective": "regression", "seed": 42},
        "label_transform": "rank",
        "target_hash": "a" * 64,
    }
    base.update(overrides)
    return base


def test_canonical_config_ignores_volatile_and_sorts_features():
    cfg = canonical_config(_payload())
    assert cfg["features"] == ["a", "b"]
    assert "job_name" not in cfg
    assert "generated_at" not in cfg


def test_single_axis_loss_change():
    a = canonical_config(_payload())
    b = canonical_config(_payload(lgb_params={"objective": "rank_xendcg", "seed": 42}))
    diffs = config_diff(a, b)
    assert [d["path"] for d in diffs] == ["lgb_params.objective"]
    assert diff_axes(diffs) == ["model_params"]
    assert is_single_axis(diffs) is True


def test_multi_axis_detected():
    a = canonical_config(_payload())
    b = canonical_config(
        _payload(features=["a", "c"], label_transform="zscore")
    )
    diffs = config_diff(a, b)
    axes = diff_axes(diffs)
    assert "features" in axes and "label_transform" in axes
    assert is_single_axis(diffs) is False


def test_target_hash_is_experiment_axis():
    a = canonical_config(_payload())
    b = canonical_config(_payload(target_hash="b" * 64))
    diffs = config_diff(a, b)
    assert [d["path"] for d in diffs] == ["target_hash"]
    assert diff_axes(diffs) == ["target_hash"]


def test_feature_source_repair_is_single_axis():
    a = canonical_config(
        _payload(
            features=["tdx_amihud", "a"],
            factor_field_sources={"tdx_amihud": "tdx_amihud"},
            factor_catalog_versions={"l1_factors": "v-old"},
        )
    )
    b = canonical_config(
        _payload(
            features=["tdx_amihud", "a"],
            factor_field_sources={"tdx_amihud": "tdx_ind:tdx_amihud"},
            factor_catalog_versions={"l1_factors": "v-new", "tdx_ind": "v-tdx"},
        )
    )
    diffs = config_diff(a, b)
    assert diff_axes(diffs) == ["features"]
    assert is_single_axis(diffs) is True


def test_summarize_strict_prefers_test_segment():
    row = {
        "status": "ready",
        "report": {
            "segments": {"valid": {"start": "20250101", "end": "20251231"},
                         "test": {"start": "20260101", "end": "20260930"}},
            "results": {
                "valid": {"top20": {"batches": 24, "cost_25bp": {"total": 0.9}}},
                "test": {
                    "top20": {
                        "batches": 17,
                        "turnover_per_batch": 0.8,
                        "cost_25bp": {
                            "total": 0.1,
                            "mdd": -0.15,
                            "excess_hs300": 0.16,
                            "hac_t": 0.7,
                            "psr": 0.76,
                        },
                    }
                },
            },
        },
    }
    out = summarize_strict(row)
    assert out["segment"] == "test"
    assert out["total_25bp"] == 0.1
    assert out["batches"] == 17
    assert out["window"]["start"] == "20260101"


def test_summarize_missing_report():
    assert summarize_strict(None)["status"] == "missing"
