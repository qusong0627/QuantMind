"""多算法模型拆分：per-algorithm 元数据重建与预测产物归属的回归测试。

背景（三个已修的缺陷）：
1. 拆分子模型原样继承父 metadata 的 `model_class_name`/`model_params`（主模型
   是 GRU 时 6 个 DL 子模型全部按 GRU 架构加载 → RuntimeError: size mismatch）；
2. 父 `best_iteration` 是主模型的值，xgboost 子模型拿到 null 后模板把
   `iteration_range=None` 传给 xgboost>=3.2 → TypeError；
3. `pred.parquet` 被当共享产物原样复制给所有子模型 → 多模型曲线画出 13 条
   相同的线。
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from backend.shared.model_algorithm_meta import (
    build_algorithm_metadata,
    build_algorithm_metrics,
    build_child_metrics_json,
    build_dl_model_params,
    build_nativetft_arch,
    comparison_index,
    read_xgboost_best_iteration,
)
from backend.shared.model_registry import ModelRegistryService

_PARENT_META = {
    "run_id": "train_20260912134006_566f6da2",
    "model_type": "gru",
    "primary_model_type": "gru",
    "model_class_name": "GRU",
    "model_params": {"d_feat": 4, "hidden_size": 64, "num_layers": 2, "dropout": 0.2},
    "is_sequence_model": True,
    "input_spec": {
        "tensor_shape": [None, 20, 4],
        "feature_columns": ["a", "b", "c", "d"],
    },
    "feature_columns": ["a", "b", "c", "d"],
    "feature_count": 4,
    "dl_params": {"batch_size": 4000, "early_stopping_rounds": 3, "n_epochs": 8},
    "feat_norm": {"mean": [0.0] * 4, "std": [1.0] * 4},
    "saved_models": {
        "lightgbm": "model_lgb.lgb",
        "xgboost": "model_xgb.xgb",
        "gru": "model_gru.pth",
        "lstm": "model_lstm.pth",
        "transformer": "model_transformer.pth",
        "tcn": "model_tcn.pth",
        "tabnet": "model_tabnet.pth",
        "nativetft": "model_nativetft.pth",
        "mlp": "model_mlp.pkl",
    },
    "comparison": [
        {
            "model_type": "gru",
            "val_ic": 0.033,
            "val_rank_ic": 0.037,
            "val_rank_icir": 0.403,
            "val_rmse": 0.289,
            "val_auc": 0.515,
            "test_ic": -0.036,
            "elapsed_seconds": 223.2,
        },
        {
            "model_type": "xgboost",
            "val_ic": -0.025,
            "val_rank_ic": -0.029,
            "val_rank_icir": -0.235,
            "val_rmse": 0.289,
            "val_auc": 0.483,
            "test_ic": 0.032,
            "elapsed_seconds": 89.4,
        },
    ],
}


class TestBuildAlgorithmMetadata:
    def test_non_dl_algo_drops_primary_arch_fields(self):
        fields, drops = build_algorithm_metadata("lightgbm", _PARENT_META)

        assert fields["framework"] == "lightgbm"
        assert "model_class_name" not in fields
        assert "model_params" not in fields
        assert {
            "model_class_name",
            "model_params",
            "is_sequence_model",
            "input_spec",
        } <= drops

    @pytest.mark.parametrize(
        ("model_type", "class_name", "expected"),
        [
            (
                "gru",
                "GRU",
                {"d_feat": 4, "hidden_size": 64, "num_layers": 2, "dropout": 0.2},
            ),
            (
                "lstm",
                "LSTM",
                {"d_feat": 4, "hidden_size": 64, "num_layers": 2, "dropout": 0.2},
            ),
            (
                "transformer",
                "TransformerModel",
                {
                    "d_feat": 4,
                    "d_model": 64,
                    "nhead": 4,
                    "num_layers": 2,
                    "dropout": 0.2,
                },
            ),
            (
                "tcn",
                "TCN",
                {
                    "d_feat": 4,
                    "n_chans": 128,
                    "kernel_size": 5,
                    "num_layers": 2,
                    "dropout": 0.2,
                },
            ),
        ],
    )
    def test_dl_params_match_training_defaults(self, model_type, class_name, expected):
        fields, _ = build_algorithm_metadata(model_type, _PARENT_META)

        assert fields["model_class_name"] == class_name
        assert fields["model_params"] == expected

    def test_tabnet_uses_its_own_param_contract(self):
        fields, _ = build_algorithm_metadata("tabnet", _PARENT_META)

        assert fields["model_class_name"] == "TabnetModel"
        assert fields["model_params"] == {
            "d_feat": 4,
            "out_dim": 1,
            "final_out_dim": 1,
            "n_d": 64,
            "n_a": 64,
            "n_steps": 5,
        }
        assert fields["is_sequence_model"] is False

    def test_customized_dl_params_are_honoured(self):
        meta = {
            **_PARENT_META,
            "dl_params": {"dl_hidden_size": 128, "dl_num_layers": 3, "dl_step_len": 30},
            # 训练端 input_spec 由同一个 step_len 生成，二者一致
            "input_spec": {
                "tensor_shape": [None, 30, 4],
                "feature_columns": ["a", "b", "c", "d"],
            },
        }

        fields, _ = build_algorithm_metadata("lstm", meta)

        assert fields["model_params"]["hidden_size"] == 128
        assert fields["model_params"]["num_layers"] == 3
        assert fields["input_spec"]["tensor_shape"] == [None, 30, 4]

    def test_nativetft_gets_arch_without_qlib_class_name(self):
        fields, _ = build_algorithm_metadata("nativetft", _PARENT_META)

        assert "model_class_name" not in fields
        assert fields["model_arch"] == {
            "input_dim": 4,
            "hidden_dim": 64,
            "num_heads": 4,
            "dropout": 0.2,
        }

    def test_sequence_models_inherit_feat_norm(self):
        fields, _ = build_algorithm_metadata("lstm", _PARENT_META)

        assert fields["feat_norm"] == _PARENT_META["feat_norm"]

    def test_trainer_algorithm_metadata_wins_over_reconstruction(self):
        trained = {
            "model_class_name": "LSTM",
            "model_params": {
                "d_feat": 4,
                "hidden_size": 32,
                "num_layers": 1,
                "dropout": 0.1,
            },
            "is_sequence_model": True,
            "input_spec": {
                "tensor_shape": [None, 15, 4],
                "feature_columns": ["a", "b", "c", "d"],
            },
            "dl_params": {"dl_step_len": 15},
            "feat_norm": {"mean": [9.0] * 4, "std": [2.0] * 4},
        }
        meta = {**_PARENT_META, "algorithm_metadata": {"lstm": trained}}

        fields, _ = build_algorithm_metadata("lstm", meta)

        assert fields["algorithm_meta_source"] == "trainer"
        assert fields["model_params"] == trained["model_params"]
        assert fields["feat_norm"] == trained["feat_norm"]

    def test_tree_primary_run_marks_reconstructed_defaults(self):
        # 主算法是树模型时训练端不落 dl_metadata，只有顶层树模型字段
        meta = {
            k: v
            for k, v in _PARENT_META.items()
            if k
            not in (
                "dl_params",
                "feat_norm",
                "input_spec",
                "model_class_name",
                "model_params",
                "is_sequence_model",
            )
        }
        meta["model_type"] = "lightgbm"

        fields, _ = build_algorithm_metadata("lstm", meta)

        assert fields["algorithm_meta_source"] == "reconstructed_defaults"
        assert "algorithm_meta_warning" in fields
        assert "feat_norm" not in fields


class TestAlgorithmMetrics:
    def test_metrics_taken_from_comparison_entry(self):
        comp = comparison_index(_PARENT_META)

        metrics = build_algorithm_metrics("gru", _PARENT_META, comp)

        assert metrics["val_ic"] == 0.033
        assert metrics["val_rank_icir"] == 0.403
        assert metrics["score_direction"] == "normal"

    def test_negative_val_ic_marks_reversed(self):
        comp = comparison_index(_PARENT_META)

        metrics = build_algorithm_metrics("xgboost", _PARENT_META, comp)

        assert metrics["score_direction"] == "reversed"

    def test_missing_entry_returns_none(self):
        comp = comparison_index(_PARENT_META)

        assert build_algorithm_metrics("catboost", _PARENT_META, comp) is None

    def test_child_db_metrics_only_emit_recorded_fields(self):
        comp = comparison_index(_PARENT_META)

        db_metrics = build_child_metrics_json(comp["xgboost"])

        assert db_metrics["val"]["rmse"] == 0.289
        assert db_metrics["val"]["auc"] == 0.483
        assert db_metrics["test"]["ic"] == 0.032
        assert "test" in db_metrics and "rmse" not in db_metrics["test"]
        assert build_child_metrics_json(None) == {}


class TestParamBuilders:
    def test_nativetft_hidden_dim_is_trimmed_to_heads_multiple(self):
        arch = build_nativetft_arch({"hidden_size": 70, "num_heads": 8})

        assert arch["hidden_dim"] % arch["num_heads"] == 0
        assert arch["hidden_dim"] == 64

    def test_nativetft_falls_back_when_hidden_smaller_than_heads(self):
        arch = build_nativetft_arch({"hidden_size": 2, "num_heads": 8})

        assert arch["hidden_dim"] == 16

    def test_transformer_nhead_derives_from_hidden_size(self):
        params = build_dl_model_params("transformer", 4, {"hidden_size": 128})

        assert params["d_model"] == 128
        assert params["nhead"] == 8

    def test_read_xgboost_best_iteration_missing_file_returns_none(self, tmp_path):
        assert read_xgboost_best_iteration(tmp_path / "nope.xgb") is None


def _pred_frame(values: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "symbol": ["AAPL", "MSFT"],
            "trade_date": ["2026-01-05", "2026-01-05"],
            "label": [0.1, -0.2],
            "pred": values,
            "split": ["test", "test"],
        }
    )


def _write_meta(directory: Path, meta: dict) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "metadata.json").write_text(
        json.dumps(meta, ensure_ascii=False), encoding="utf-8"
    )


class TestChildMetadataAssembly:
    """`_build_child_model_metadata` 的预测归属与算法字段重写。"""

    def _service(self) -> ModelRegistryService:
        return ModelRegistryService()

    def _call(self, service, parent: Path, child: Path, model_type: str, job_dir=None):
        return service._build_child_model_metadata(
            parent_meta=json.loads((parent / "metadata.json").read_text("utf-8")),
            record={"source_run_id": "train_x"},
            model_type=model_type,
            model_file=_PARENT_META["saved_models"][model_type],
            child_id=child.name,
            child_dir=child,
            parent_dir=parent,
            job_dir=job_dir,
            comparison=comparison_index(_PARENT_META),
            primary_type="gru",
            parent_pred_md5s={
                name: service._file_md5(parent / name)
                for name in ("pred.parquet", "pred.pkl")
            },
        )

    def test_per_algorithm_prediction_is_used(self, tmp_path):
        service = self._service()
        parent = tmp_path / "parent"
        _write_meta(parent, _PARENT_META)
        _pred_frame([0.5, 0.6]).to_parquet(parent / "pred.parquet")
        (parent / "pred.pkl").write_bytes(b"parent-pkl")
        job = tmp_path / "job"
        job.mkdir()
        _pred_frame([0.9, -0.9]).to_parquet(job / "pred_lstm.parquet")
        (parent / _PARENT_META["saved_models"]["lstm"]).write_bytes(b"w")
        child = tmp_path / "child"
        child.mkdir()

        meta = self._call(service, parent, child, "lstm", job_dir=job)

        assert meta["pred_source"] == "pred_lstm.parquet"
        assert meta["pred_rows"] == 2
        assert meta["pred_coverage_start"] == "2026-01-05"
        copied = pd.read_parquet(child / "pred.parquet")
        assert copied["pred"].tolist() == [0.9, -0.9]
        assert pickle_scores(child / "pred.pkl") == [0.9, -0.9]
        assert meta["model_class_name"] == "LSTM"
        assert meta["model_params"]["hidden_size"] == 64
        # comparison 里没有 lstm 条目：宁可缺省也不回填主模型（GRU）的指标
        assert "metrics" not in meta

    def test_missing_prediction_removes_parent_copy_and_nulls_coverage(self, tmp_path):
        service = self._service()
        parent = tmp_path / "parent"
        _write_meta(parent, _PARENT_META)
        _pred_frame([0.5, 0.6]).to_parquet(parent / "pred.parquet")
        parent_pkl = b"parent-pkl"
        (parent / "pred.pkl").write_bytes(parent_pkl)
        (parent / _PARENT_META["saved_models"]["tcn"]).write_bytes(b"w")
        child = tmp_path / "child"
        child.mkdir()
        # 模拟旧版拆分：子目录里是父份的原样拷贝
        (child / "pred.parquet").write_bytes((parent / "pred.parquet").read_bytes())
        (child / "pred.pkl").write_bytes(parent_pkl)

        meta = self._call(service, parent, child, "tcn")

        assert meta["pred_source"] == "unavailable"
        assert meta["pred_rows"] is None
        assert not (child / "pred.parquet").exists()
        assert not (child / "pred.pkl").exists()

    def test_primary_algo_adopts_parent_prediction(self, tmp_path):
        service = self._service()
        parent = tmp_path / "parent"
        _write_meta(parent, _PARENT_META)
        _pred_frame([0.5, 0.6]).to_parquet(parent / "pred.parquet")
        (parent / "pred.pkl").write_bytes(b"parent-pkl")
        (parent / _PARENT_META["saved_models"]["gru"]).write_bytes(b"w")
        child = tmp_path / "child"
        child.mkdir()

        meta = self._call(service, parent, child, "gru")

        assert meta["pred_source"] == "parent_primary"
        assert (child / "pred.parquet").is_file()
        assert meta["pred_rows"] == 2
        # 主算法在 comparison 里有条目：指标按算法取值
        assert meta["metrics"]["val_rank_icir"] == 0.403
        assert meta["metrics"]["score_direction"] == "normal"

    def test_ensemble_children_lose_ensemble_flags(self, tmp_path):
        service = self._service()
        parent = tmp_path / "parent"
        _write_meta(
            parent,
            {
                **_PARENT_META,
                "is_ensemble": True,
                "ensemble_method": "stacking",
                "meta_model_file": "meta_model.pkl",
                "base_model_files": {"lightgbm": "model_lgb.lgb"},
            },
        )
        _pred_frame([0.5, 0.6]).to_parquet(parent / "pred.parquet")
        (parent / "pred.pkl").write_bytes(b"parent-pkl")
        (parent / _PARENT_META["saved_models"]["gru"]).write_bytes(b"w")
        child = tmp_path / "child"
        child.mkdir()

        meta = self._call(service, parent, child, "gru")

        assert meta["is_ensemble"] is False
        assert "ensemble_method" not in meta
        assert "meta_model_file" not in meta
        # 集成训练下父份是集成预测而非主基模型的预测，不能沿用
        assert meta["pred_source"] == "unavailable"
        assert not (child / "pred.parquet").exists()


def pickle_scores(path: Path) -> list[float]:
    frame = pd.read_pickle(path)
    return frame["score"].tolist()
