"""融合模型「数据面继承」契约测试 —— 纯函数，无 DB。

背景（2026-10-09 E2E 实锤）：`register_ensemble_model` 曾把 metadata 的
`data_source` 硬编码为 "parquet" —— 成员全部读 quantdb_factors 时，runner 按
错误数据源路由到 feature_snapshots（本机停更 2026-08），融合模型「能推理但读
陈旧数据」。修复 = 从成员 metadata 多数票继承整个数据面（data_source /
quantdb_dir pin / factor_source / schema 哈希 / 字段映射），且继承必须确定性
（并列有固定裁决），分歧必须可见（警告），绝不静默。
"""

from __future__ import annotations

from backend.shared.model_registry import (
    _inherit_data_plane_from_members,
    _majority_choice,
    _merge_field_sources,
)


def _quantdb_meta(
    *,
    quantdb_dir: str = "/data/quantcustom",
    factor_source: str = "l1_factors",
    schema_hash: str = "hash_a",
    fields: dict | None = None,
) -> dict:
    return {
        "data_source": "quantdb_factors",
        "quantdb_dir": quantdb_dir,
        "factor_source": factor_source,
        "factor_schema_hash": schema_hash,
        "factor_field_sources": fields if fields is not None else {"f1": "lib_a:c1"},
    }


class TestMajorityChoice:
    def test_majority_wins_and_tie_is_deterministic(self):
        assert _majority_choice(["a", "a", "b"]) == "a"
        # 并列 → 字典序最小（确定性，两次调用同值）
        assert _majority_choice(["b", "a"]) == "a"
        assert _majority_choice(["b", "a"]) == _majority_choice(["a", "b"])

    def test_prefer_breaks_tie_only_when_present(self):
        assert _majority_choice(["parquet", "quantdb_factors"], prefer="quantdb_factors") == "quantdb_factors"
        assert _majority_choice(["parquet", "parquet", "x"], prefer="quantdb_factors") == "parquet"

    def test_empty_and_blank_values_yield_none(self):
        assert _majority_choice([]) is None
        assert _majority_choice(["", "  "]) is None


class TestMergeFieldSources:
    def test_identical_maps_merge_without_warning(self):
        warnings: list[str] = []
        merged = _merge_field_sources(
            [{"f1": "lib:c1", "f2": "lib:c2"}, {"f1": "lib:c1", "f2": "lib:c2"}],
            warnings,
        )
        assert merged == {"f1": "lib:c1", "f2": "lib:c2"}
        assert warnings == []

    def test_conflict_takes_majority_and_warns(self):
        warnings: list[str] = []
        merged = _merge_field_sources(
            [{"f1": "lib:c1"}, {"f1": "lib:c1"}, {"f1": "lib:other"}],
            warnings,
        )
        assert merged == {"f1": "lib:c1"}
        assert warnings and "factor_field_sources_conflict" in warnings[0]


class TestInheritDataPlane:
    def test_unanimous_quantdb_members_inherit_full_plane(self):
        plane, warnings = _inherit_data_plane_from_members(
            [_quantdb_meta(), _quantdb_meta()]
        )
        assert plane == {
            "data_source": "quantdb_factors",
            "quantdb_dir": "/data/quantcustom",
            "factor_source": "l1_factors",
            "factor_schema_hash": "hash_a",
            "factor_field_sources": {"f1": "lib_a:c1"},
        }
        assert warnings == []

    def test_quantdb_dir_disagreement_majority_wins_and_warns(self):
        plane, warnings = _inherit_data_plane_from_members(
            [
                _quantdb_meta(quantdb_dir="/data/quantcustom"),
                _quantdb_meta(quantdb_dir="/data/quantcustom"),
                _quantdb_meta(quantdb_dir="/data/other"),
            ]
        )
        assert plane["quantdb_dir"] == "/data/quantcustom"
        assert any(w.startswith("mixed_quantdb_dir") for w in warnings)

    def test_mixed_plane_tie_prefers_quantdb_and_warns(self):
        # 1 quantdb + 1 parquet 平票：倾向 quantdb 面（带 pin 与就绪检查、可对账），
        # 且分歧必须可见 —— 半数成员的数据面与融合取数面不一致。
        plane, warnings = _inherit_data_plane_from_members(
            [_quantdb_meta(), {"data_source": "parquet"}]
        )
        assert plane["data_source"] == "quantdb_factors"
        assert any(w.startswith("mixed_data_source") for w in warnings)

    def test_all_parquet_inherits_data_dir_only(self):
        plane, warnings = _inherit_data_plane_from_members(
            [
                {"data_source": "parquet", "data_dir": "/data/snapshots"},
                {"data_source": "parquet", "data_dir": "/data/snapshots"},
            ]
        )
        assert plane == {"data_source": "parquet", "data_dir": "/data/snapshots"}
        assert warnings == []
        assert "quantdb_dir" not in plane

    def test_missing_data_source_defaults_to_parquet_without_extra_keys(self):
        plane, warnings = _inherit_data_plane_from_members([{}, {}])
        assert plane == {"data_source": "parquet"}
        assert warnings == []
