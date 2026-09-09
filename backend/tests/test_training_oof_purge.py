"""stacking OOF 折的标签窗口 purge 回归测试。

标签是未来 h 日收益（`cfg.label.target_horizon_days`），扩展窗口 OOF 若把
「最后 h 个训练日」留在训练集里，其标签窗口就与验证集重叠 → 信息泄漏，
元模型在验证/测试上的评估被高估。
"""

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

pytestmark = pytest.mark.unit

FEATURES = ["f1", "f2"]
N_DATES = 60
N_SYMBOLS = 30
FOLD_SIZE = N_DATES // 4  # n_folds=3 → 每折 15 个交易日


def _load_training_module():
    root = Path(__file__).resolve().parents[2]
    training_dir = root / "docker" / "training"
    # train.py 以顶层包名导入 diagnostics / model_trainers，需把训练目录入 path
    if str(training_dir) not in sys.path:
        sys.path.insert(0, str(training_dir))
    module_path = training_dir / "train.py"
    spec = importlib.util.spec_from_file_location(
        "quant_training_train_oof", module_path
    )
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def _sample_df(n_dates: int = N_DATES, n_symbols: int = N_SYMBOLS) -> pd.DataFrame:
    dates = pd.date_range("2024-01-02", periods=n_dates, freq="B")
    rows = []
    for d_i, date in enumerate(dates):
        for s_i in range(n_symbols):
            rows.append(
                {
                    "symbol": f"{s_i:06d}",
                    "trade_date": date,
                    "label": float(s_i - n_symbols / 2) * 1e-3,
                    "f1": float(d_i),
                    "f2": float(s_i),
                }
            )
    return pd.DataFrame(rows)


def _capture_folds(monkeypatch, cfg: dict, n_folds: int = 3) -> tuple[list[dict], list]:
    """跑 OOF 生成，捕获每折的 (train_dates, val_dates) 及全量交易日历。"""
    mod = _load_training_module()
    df = _sample_df()
    captured: list[dict] = []

    def fake_train(model_type, fold_train, fold_val, *_args, **_kwargs):
        captured.append(
            {
                "train_dates": sorted(fold_train["trade_date"].unique()),
                "val_dates": sorted(fold_val["trade_date"].unique()),
            }
        )
        return {"model": object(), "fill_values": dict.fromkeys(FEATURES, 0.0)}

    def fake_predict(_model, X, _model_type, _features):
        return np.zeros(len(X))

    monkeypatch.setattr(mod, "_train_single_model", fake_train)
    monkeypatch.setattr(mod, "_predict_with_model", fake_predict)
    mod._generate_oof_predictions("lightgbm", df, FEATURES, cfg, n_folds=n_folds)
    return captured, sorted(df["trade_date"].unique())


def _purged_gap(fold: dict, calendar: list) -> int:
    """训练集末日与验证集首日之间、按全量交易日历计数的交易日个数。"""
    idx = {d: i for i, d in enumerate(calendar)}
    return idx[fold["val_dates"][0]] - idx[fold["train_dates"][-1]] - 1


@pytest.mark.parametrize("horizon", [1, 3, 10])
def test_oof_fold_purges_label_horizon(monkeypatch, horizon):
    """训练集末尾 h 个交易日必须被剔除（其标签窗口与验证集重叠）。"""
    folds, calendar = _capture_folds(
        monkeypatch, {"label": {"target_horizon_days": horizon}}
    )
    assert folds, "未捕获到任何折"
    for fold in folds:
        assert max(fold["train_dates"]) < min(fold["val_dates"])
        assert _purged_gap(fold, calendar) == horizon


def test_oof_purge_defaults_to_one_when_horizon_missing(monkeypatch):
    """cfg 里没有 label.target_horizon_days 时默认 1 日。"""
    folds, calendar = _capture_folds(monkeypatch, {})
    assert folds
    for fold in folds:
        assert _purged_gap(fold, calendar) == 1


def test_oof_still_covers_all_validation_dates(monkeypatch):
    """purge 只削训练集，不应改变验证集覆盖面。"""
    folds, calendar = _capture_folds(monkeypatch, {"label": {"target_horizon_days": 3}})
    val_dates = sorted({d for f in folds for d in f["val_dates"]})
    assert val_dates == calendar[FOLD_SIZE:]  # 3 折验证集首尾相接覆盖后 45 天
    assert len(folds) == 3
