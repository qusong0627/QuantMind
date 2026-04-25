import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd


def _load_training_module():
    root = Path(__file__).resolve().parents[2]
    module_path = root / "docker" / "training" / "train.py"
    spec = importlib.util.spec_from_file_location("quant_training_train", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class _DummyBooster:
    def __init__(self, *, raise_error: bool = False):
        self.best_iteration = 7
        self._raise_error = raise_error

    def predict(self, x, num_iteration=None, pred_contrib=False):
        if self._raise_error:
            raise RuntimeError("mock shap predict failure")
        arr = np.asarray(x, dtype=np.float32)
        if pred_contrib:
            base = np.zeros((arr.shape[0], 1), dtype=np.float32)
            return np.concatenate([arr * 0.1, base], axis=1)
        return arr.sum(axis=1)


def _sample_split_df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "symbol": ["000001", "000002", "000003"],
            "trade_date": pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"]),
            "label": [0.2, -0.1, 0.3],
            "f1": [1.0, 2.0, 3.0],
            "f2": [0.5, np.nan, 2.5],
        }
    )


def test_compute_shap_summary_completed(tmp_path: Path) -> None:
    module = _load_training_module()
    out_file = tmp_path / "shap_summary.csv"
    info = module._compute_shap_summary(
        model=_DummyBooster(),
        split_frames={"valid": _sample_split_df(), "test": pd.DataFrame(), "train": pd.DataFrame()},
        features=["f1", "f2"],
        fill_values={"f1": 0.0, "f2": 0.0},
        explain_cfg={"enable_shap": True, "shap_split": "valid", "shap_sample_rows": 30000},
        out_path=out_file,
    )
    assert info["status"] == "completed"
    assert info["split"] == "valid"
    assert out_file.exists()
    summary = pd.read_csv(out_file)
    assert list(summary.columns) == ["feature", "mean_abs_shap", "mean_shap", "positive_ratio"]
    assert set(summary["feature"].tolist()) == {"f1", "f2"}


def test_compute_shap_summary_disabled(tmp_path: Path) -> None:
    module = _load_training_module()
    out_file = tmp_path / "shap_summary.csv"
    info = module._compute_shap_summary(
        model=_DummyBooster(),
        split_frames={"valid": _sample_split_df(), "test": pd.DataFrame(), "train": pd.DataFrame()},
        features=["f1", "f2"],
        fill_values={"f1": 0.0, "f2": 0.0},
        explain_cfg={"enable_shap": False, "shap_split": "valid", "shap_sample_rows": 30000},
        out_path=out_file,
    )
    assert info["status"] == "disabled"
    assert not out_file.exists()


def test_compute_shap_summary_failed_non_blocking(tmp_path: Path) -> None:
    module = _load_training_module()
    out_file = tmp_path / "shap_summary.csv"
    info = module._compute_shap_summary(
        model=_DummyBooster(raise_error=True),
        split_frames={"valid": _sample_split_df(), "test": pd.DataFrame(), "train": pd.DataFrame()},
        features=["f1", "f2"],
        fill_values={"f1": 0.0, "f2": 0.0},
        explain_cfg={"enable_shap": True, "shap_split": "valid", "shap_sample_rows": 30000},
        out_path=out_file,
    )
    assert info["status"] == "failed"
    assert "mock shap predict failure" in str(info.get("error"))
