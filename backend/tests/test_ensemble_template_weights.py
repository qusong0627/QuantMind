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
