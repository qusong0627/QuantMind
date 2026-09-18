"""推理脚本模型文件定位（`_resolve_model_path` / `load_model`）回归测试。

背景（2026-09-18 线上事故）：
训练端 metadata.json 记的是 per-algorithm 权重名（`model_gru.pth` /
`model_nativetft.pth`），而产物同步按白名单把权重复制成通用名 `model.pth`，
两者对不上。推理脚本的兜底逻辑退化为「按扩展名盲搜目录」，且 `*.pkl` 排在
`*.pth` 之前 —— 于是同目录里的预测产物 `pred.pkl` 被当成模型加载，pickle
出来是 DataFrame，走到 `model.predict()` 必崩：

    AttributeError: 'DataFrame' object has no attribute 'predict'

用户可见表现：同一批训练出来的模型里，只有 `model_file` 恰好对得上的那个
（`model.pkl` 命中的那次）能推理，其余（全部 DL 模型）必然失败。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_TEMPLATE = (
    Path(__file__).resolve().parents[1]
    / "services"
    / "engine"
    / "inference"
    / "templates"
    / "inference_parquet.py"
)


@pytest.fixture(scope="module")
def tpl():
    """按文件加载推理模板（templates/ 不是包，且推理时代码会被复制进模型目录）。"""
    spec = importlib.util.spec_from_file_location("inference_parquet_tpl", _TEMPLATE)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _model_dir(tmp_path: Path, files: dict[str, bytes], meta: dict | None = None) -> Path:
    """搭一个模型目录：files 的文件名 → 内容，meta 写进 metadata.json。"""
    d = tmp_path / "mdl_test"
    d.mkdir()
    for name, payload in files.items():
        (d / name).write_bytes(payload)
    if meta is not None:
        (d / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    return d


# 训练产物目录的真实形态：权重 + 预测产物 + 配置脚本同处一室
_DL_FILES = {
    "model.pth": b"weights",
    "pred.pkl": b"prediction-dataframe",
    "pred.parquet": b"prediction-parquet",
    "result.json": b"{}",
    "config.yaml": b"k: v",
    "metadata.json": b"{}",
    "inference.py": b"# script",
}


class TestDeclaredModelFileWins:
    def test_declared_file_that_exists_is_used(self, tpl, tmp_path):
        d = _model_dir(
            tmp_path,
            {"model.pkl": b"sklearn-model", "pred.pkl": b"pred-df"},
            meta={"model_file": "model.pkl"},
        )

        assert tpl._resolve_model_path(d, {"model_file": "model.pkl"}) == d / "model.pkl"

    def test_missing_declared_name_falls_back_to_same_extension(self, tpl, tmp_path):
        # metadata 记 model_nativetft.pth，磁盘上只有 model.pth
        d = _model_dir(tmp_path, _DL_FILES, meta={"model_file": "model_nativetft.pth"})

        resolved = tpl._resolve_model_path(d, {"model_file": "model_nativetft.pth"})

        assert resolved == d / "model.pth"


class TestArtifactsAreNeverModels:
    def test_prediction_dump_does_not_shadow_weights(self, tpl, tmp_path):
        """核心回归：pred.pkl 不得盖过 model.pth。"""
        d = _model_dir(tmp_path, _DL_FILES)

        assert tpl._resolve_model_path(d, {}) == d / "model.pth"

    def test_result_json_is_not_a_model(self, tpl, tmp_path):
        d = _model_dir(tmp_path, {"result.json": b"{}", "pred.pkl": b"df"})

        assert tpl._resolve_model_path(d, {}) is None

    @pytest.mark.parametrize("name", ["pred.pkl", "pred_lstm.pkl", "result.txt", "shap_summary.csv"])
    def test_artifact_names_are_excluded_from_candidates(self, tpl, tmp_path, name):
        d = _model_dir(tmp_path, {name: b"x"})

        assert tpl._resolve_model_path(d, {}) is None

    def test_directory_with_only_predictions_is_fatal(self, tpl, tmp_path, caplog):
        """没有权重时必须明确报错退出，而不是把预测产物当模型用。"""
        d = _model_dir(tmp_path, {"pred.pkl": b"df", "pred.parquet": b"pq"})

        with pytest.raises(SystemExit) as exc:
            tpl.load_model(d, {})

        assert exc.value.code == 1


class TestExtensionPreference:
    def test_lightgbm_txt_wins_over_generic_pickle(self, tpl, tmp_path):
        d = _model_dir(tmp_path, {"model.txt": b"lgb", "pred.pkl": b"df", "meta_model.pkl": b"m"})

        assert tpl._resolve_model_path(d, {}) == d / "model.txt"

    def test_model_stem_preferred_over_other_names(self, tpl, tmp_path):
        # 权重文件带算法后缀时（拆分产物），优先取通用名 model.*
        d = _model_dir(tmp_path, {"model.pth": b"w", "model_gru.pth": b"w2"})

        assert tpl._resolve_model_path(d, {}) == d / "model.pth"

    def test_falls_back_to_algorithm_suffixed_weights(self, tpl, tmp_path):
        # 只有拆分出来的 per-algorithm 权重时，仍要能找到
        d = _model_dir(tmp_path, {"model_gru.pth": b"w", "pred.pkl": b"df"})

        assert tpl._resolve_model_path(d, {}) == d / "model_gru.pth"


class TestLoadModelDispatch:
    def test_pickle_loads_as_sklearn(self, tpl, tmp_path):
        import pickle

        d = _model_dir(tmp_path, {"model.pkl": pickle.dumps({"kind": "fake"}), "pred.pkl": b"df"})

        kind, obj = tpl.load_model(d, {"model_file": "model.pkl"})

        assert kind == "sklearn"
        assert obj == {"kind": "fake"}


class TestMetadataReconcile:
    """模型落盘时把 metadata.model_file 校准成磁盘上真实的权重名。"""

    @staticmethod
    def _reconcile(directory: Path, model_file: str) -> dict:
        from backend.shared.model_registry import ModelRegistryService

        ModelRegistryService._reconcile_metadata_model_file(directory, model_file)
        meta_path = directory / "metadata.json"
        return json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.is_file() else {}

    def test_stale_per_algorithm_name_is_rewritten(self, tmp_path):
        d = _model_dir(
            tmp_path,
            {"model.pth": b"w", "result.json": b"{}"},
            meta={"model_file": "model_nativetft.pth", "model_type": "NativeTFT"},
        )

        meta = self._reconcile(d, "model.pth")

        assert meta["model_file"] == "model.pth"
        assert meta["model_type"] == "NativeTFT"  # 其余字段保持不变

    def test_existing_declared_file_is_left_alone(self, tmp_path):
        d = _model_dir(
            tmp_path,
            {"model_gru.pth": b"w"},
            meta={"model_file": "model_gru.pth"},
        )

        meta = self._reconcile(d, "model.pth")

        # 声明名在磁盘上真实存在：保留原声明，不跟着 DB 列走
        assert meta["model_file"] == "model_gru.pth"

    def test_missing_metadata_is_a_noop(self, tmp_path):
        d = tmp_path / "mdl_bare"
        d.mkdir()

        self._reconcile(d, "model.pth")  # 不抛异常即可

        assert not (d / "metadata.json").exists()

    def test_empty_model_file_is_a_noop(self, tmp_path):
        d = _model_dir(tmp_path, {"model.pth": b"w"}, meta={"model_file": "model_x.pth"})

        meta = self._reconcile(d, "")

        assert meta["model_file"] == "model_x.pth"
