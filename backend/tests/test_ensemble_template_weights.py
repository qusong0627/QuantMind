"""融合推理模板的动态权重加载 —— v1 扁平 / v2 嵌套快照兼容契约。

背景（2026-10-09 机构级融合 v2）：`register_ensemble_model` 改写的
weight_snapshot.json 从 v1 扁平 {mid: float} 升级为 v2 嵌套
{"version": 2, "weights": {mid: float}, "diagnostics": [...]}。
旧模板 `float(v)` 遍历顶层会把 v2 的 version/as_of/diagnostics 当权重解析 →
异常 → **静默回退静态权重**（周更刷新形同虚设）。本测试锁定：
- v2 只认 weights 段，绝不把顶层标量误当成员权重；
- v1 扁平快照继续可用（旧模型目录不迁移）；
- 缺失/损坏/全零一律回退静态权重。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_TEMPLATE = (
    Path(__file__).resolve().parents[1]
    / "services"
    / "engine"
    / "inference"
    / "templates"
    / "inference_ensemble_src.py"
)
_STATIC = {"m_a": 0.5, "m_b": 0.5}


@pytest.fixture(scope="module")
def tpl():
    spec = importlib.util.spec_from_file_location("inference_ensemble_src_under_test", _TEMPLATE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write(tmp_path: Path, payload) -> Path:
    model_dir = tmp_path / "model"
    model_dir.mkdir(exist_ok=True)
    (model_dir / "weight_snapshot.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )
    return model_dir


class TestSnapshotCompat:
    def test_missing_snapshot_returns_static(self, tpl, tmp_path):
        assert tpl._load_dynamic_weights(tmp_path / "nope", _STATIC) == _STATIC

    def test_v1_flat_snapshot_normalized_skips_meta(self, tpl, tmp_path):
        model_dir = _write(
            tmp_path, {"m_a": 1.0, "m_b": 3.0, "_updated_at": "2026-09-01T00:00:00"}
        )
        out = tpl._load_dynamic_weights(model_dir, _STATIC)
        assert out == pytest.approx({"m_a": 0.25, "m_b": 0.75})

    def test_v2_nested_snapshot_uses_weights_section_only(self, tpl, tmp_path):
        model_dir = _write(
            tmp_path,
            {
                "version": 2,
                "as_of": "2026-10-09",
                "strategy": "icir_shrunk",
                "weights": {"m_a": 0.7, "m_b": 0.3},
                "diagnostics": [{"member_id": "m_a", "weight": 0.7}],
                "updated_at": "2026-10-09T01:00:00+00:00",
            },
        )
        out = tpl._load_dynamic_weights(model_dir, _STATIC)
        assert out == pytest.approx({"m_a": 0.7, "m_b": 0.3})

    def test_v2_zero_weights_falls_back_to_static(self, tpl, tmp_path):
        model_dir = _write(tmp_path, {"version": 2, "weights": {"m_a": 0.0, "m_b": 0.0}})
        assert tpl._load_dynamic_weights(model_dir, _STATIC) == _STATIC

    def test_corrupt_json_falls_back_to_static(self, tpl, tmp_path):
        model_dir = tmp_path / "model"
        model_dir.mkdir()
        (model_dir / "weight_snapshot.json").write_text("{not json", encoding="utf-8")
        assert tpl._load_dynamic_weights(model_dir, _STATIC) == _STATIC

    def test_v1_without_numeric_values_falls_back_to_static(self, tpl, tmp_path):
        model_dir = _write(tmp_path, {"note": "junk", "_updated_at": "2026-09-01"})
        assert tpl._load_dynamic_weights(model_dir, _STATIC) == _STATIC


class TestScoreNorm:
    """融合成员分数的截面标准化口径（2026-11-20 增加 z-score 默认）。"""

    def test_rank_pct_is_legacy_percentile(self, tpl):
        out = tpl._cross_sectional_norm({"a": 1.0, "b": 2.0, "c": 3.0}, "rank_pct")
        assert out == pytest.approx({"a": 1 / 3, "b": 2 / 3, "c": 1.0})

    def test_zscore_maps_to_centered_unit_interval(self, tpl):
        out = tpl._cross_sectional_norm({"a": 1.0, "b": 2.0, "c": 3.0}, "zscore")
        assert out["b"] == pytest.approx(0.5)
        assert out["a"] < 0.5 < out["c"]
        # 总体标准差 (ddof=0): sd=sqrt(2/3) → z=±sqrt(3/2)；映射 0.5 + z/(2*3)
        offset = (1.5**0.5) / 6.0
        assert out["a"] == pytest.approx(0.5 - offset)
        assert out["c"] == pytest.approx(0.5 + offset)

    def test_zscore_clip_saturates_earlier(self, tpl):
        # 9 个点、单个离群值 → 离群点 z≈2.83（介于 2.5 与 3 之间）
        raw = {**{f"s{i}": 0.0 for i in range(8)}, "outlier": 1.0}
        z = tpl._cross_sectional_norm(raw, "zscore")
        tight = tpl._cross_sectional_norm(raw, "zscore_clip")
        # zscore_clip 在 ±2.5 处先饱和（离群点直接打满 1.0）
        assert tight["outlier"] == pytest.approx(1.0)
        assert z["outlier"] < 1.0
        # 中段被映射得更开（除数 2*2.5 更小）
        assert tight["s0"] < z["s0"]

    def test_minmax(self, tpl):
        out = tpl._cross_sectional_norm({"a": 1.0, "b": 2.0, "c": 3.0}, "minmax")
        assert out == pytest.approx({"a": 0.0, "b": 0.5, "c": 1.0})

    def test_constant_scores_are_neutral(self, tpl):
        for method in ("zscore", "zscore_clip", "minmax"):
            out = tpl._cross_sectional_norm({"a": 1.0, "b": 1.0}, method)
            assert out == pytest.approx({"a": 0.5, "b": 0.5})

    def test_nan_is_neutral_and_ignored_in_stats(self, tpl):
        out = tpl._cross_sectional_norm(
            {"a": 1.0, "b": float("nan"), "c": 3.0}, "zscore"
        )
        assert out["b"] == pytest.approx(0.5)
        assert out["a"] < 0.5 < out["c"]

    def test_empty_and_unknown(self, tpl):
        assert tpl._cross_sectional_norm({}, "zscore") == {}
        with pytest.raises(ValueError):
            tpl._cross_sectional_norm({"a": 1.0}, "nope")

    def test_fuse_scores_records_norm(self, tpl):
        out = tpl.fuse_scores(
            {"m1": {"a": 1.0, "b": 2.0}, "m2": {"a": 2.0, "b": 1.0}},
            {"m1": 0.5, "m2": 0.5},
            score_norm="zscore",
        )
        assert out and all(row["score_norm"] == "zscore" for row in out)
        # 两成员完全反向 → 加权后中性，score ≈ 0
        assert all(abs(row["score"]) < 1e-9 for row in out)
