"""合并因子库列级 provenance（2026-11-22 重训来源解析）。"""

from __future__ import annotations

from backend.shared.factor_provenance import build_feature_provenance


def _rows(*items):
    return [
        {"column_name": col, "market": market, "dataset_id": dataset}
        for col, market, dataset in items
    ]


def test_ignores_merged_dataset_itself_and_prefers_original_library():
    out = build_feature_provenance(
        ["a101_013"],
        _rows(
            ("a101_013", "CUSTOM", "l1_factors"),
            ("a101_013", "CN", "alpha_library"),
        ),
        market="CUSTOM",
        dataset_id="l1_factors",
    )
    assert out["a101_013"]["source_market"] == "CN"
    assert out["a101_013"]["source_dataset"] == "alpha_library"


def test_unknown_and_other_columns_are_ignored():
    out = build_feature_provenance(
        ["a101_013", "not_in_any_catalog"],
        _rows(("gtja_036", "CN", "alpha_library"), ("a101_013", "CN", "alpha_library")),
        market="CUSTOM",
        dataset_id="l1_factors",
    )
    assert set(out) == {"a101_013"}


def test_falls_back_to_remaining_candidate_and_keeps_top5():
    rows = _rows(
        ("dup_0", "CUSTOM", "rd_mined"),
        ("dup_0", "CN", "alpha360"),
        ("dup_0", "CN", "factor_dictionary"),
        ("dup_0", "CN", "l1_l2_factors"),
        ("dup_0", "CN", "l1_factors"),
        ("dup_0", "CN", "tianfeng_daily"),
        ("dup_0", "CN", "tdx_ind"),
    )
    out = build_feature_provenance(
        ["dup_0"], rows, market="CUSTOM", dataset_id="l1_factors"
    )
    assert out["dup_0"]["source_dataset"] == "alpha360"
    assert len(out["dup_0"]["candidates"]) == 5


def test_same_library_tie_prefers_cn_market():
    out = build_feature_provenance(
        ["concept_exposure_top1"],
        _rows(
            ("concept_exposure_top1", "HK", "l1_factors"),
            ("concept_exposure_top1", "CN", "l1_factors"),
        ),
        market="CUSTOM",
        dataset_id="l1_factors",
    )
    assert out["concept_exposure_top1"]["source_market"] == "CN"
