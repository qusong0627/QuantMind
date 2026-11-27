"""机构级训练护栏：默认值一致性、结构适配、前视泄漏、过拟合诊断。

覆盖三层：
1. 参数治理：后端机构基准与前端训练页默认值同源同值；
2. 模型结构：TCN 感受野必须覆盖 step_len；
3. 训练纪律：标签列不得进特征、切分时点间隔 >= embargo、train/val 指标诊断可审计。

不依赖 torch / qlib；只 import 纯常量模块与 pandas。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TRAINING_DIR = REPO_ROOT / "docker" / "training"
if str(TRAINING_DIR) not in sys.path:
    sys.path.insert(0, str(TRAINING_DIR))

from backend.shared.feature_guard import (  # noqa: E402
    find_hard_label_leaks,
    find_soft_return_features,
)
from data.integrity import (  # noqa: E402
    assert_no_feature_leak,
    overfit_diagnostics,
    time_integrity_report,
)
from data.splits import _prepare_arrays  # noqa: E402
from data.wfa_windows import split_wfa_window  # noqa: E402


def _load_pure_trainer_module(module_name: str, filename: str):
    """按文件路径加载 model_trainers 下的纯模块。

    包 `model_trainers/__init__.py` 会聚合导入 trainers_dl（依赖 torch），
    本机测试环境无 torch；纯常量/纯函数模块自身零依赖，直接按路径加载即可。
    """
    import importlib.util

    path = TRAINING_DIR / "model_trainers" / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_defaults = _load_pure_trainer_module("qm_training_defaults", "defaults.py")
_feature_roles = _load_pure_trainer_module("qm_feature_roles", "feature_roles.py")
_structure = _load_pure_trainer_module("qm_model_structure", "structure.py")

DEFAULT_LGB_PARAMS = _defaults.DEFAULT_LGB_PARAMS
DEFAULT_XGB_PARAMS = _defaults.DEFAULT_XGB_PARAMS
DEFAULT_CATBOOST_PARAMS = _defaults.DEFAULT_CATBOOST_PARAMS
categorical_feature_names = _feature_roles.categorical_feature_names
min_kernel_for_window = _structure.min_kernel_for_window
min_layers_for_window = _structure.min_layers_for_window
tcn_receptive_field = _structure.tcn_receptive_field
tcn_structure_guard = _structure.tcn_structure_guard

TRAINING_UTILS = REPO_ROOT / "electron" / "src" / "pages" / "training" / "trainingUtils.tsx"
GBDT_TRAINER = TRAINING_DIR / "model_trainers" / "trainers_gbdt.py"
FEATURE_GUARD = REPO_ROOT / "backend" / "shared" / "feature_guard.py"


# ── 1. 参数治理：后端机构基准 ────────────────────────────────────────────────

def test_gbdt_defaults_match_institutional_baseline() -> None:
    assert DEFAULT_LGB_PARAMS["learning_rate"] == 0.03
    assert DEFAULT_LGB_PARAMS["num_leaves"] == 31
    assert DEFAULT_LGB_PARAMS["max_depth"] == 6
    assert DEFAULT_LGB_PARAMS["min_data_in_leaf"] == 500
    assert DEFAULT_LGB_PARAMS["bagging_freq"] == 1
    assert DEFAULT_LGB_PARAMS["lambda_l2"] == 5.0
    assert DEFAULT_LGB_PARAMS["feature_fraction"] == 0.7
    assert DEFAULT_LGB_PARAMS["bagging_fraction"] == 0.8

    assert DEFAULT_XGB_PARAMS["max_depth"] == 5
    assert DEFAULT_XGB_PARAMS["subsample"] == 0.8
    assert DEFAULT_XGB_PARAMS["colsample_bytree"] == 0.8
    assert DEFAULT_XGB_PARAMS["reg_lambda"] == 10.0
    # 与 LGB min_data_in_leaf=500 同口径
    assert DEFAULT_XGB_PARAMS["min_child_weight"] == 500

    assert DEFAULT_CATBOOST_PARAMS["depth"] == 5
    assert DEFAULT_CATBOOST_PARAMS["l2_leaf_reg"] == 10.0
    assert DEFAULT_CATBOOST_PARAMS["random_strength"] == 2.0
    assert DEFAULT_CATBOOST_PARAMS["bagging_temperature"] == 1.0


def test_categorical_feature_names_only_industry_codes() -> None:
    assert categorical_feature_names(["a", "ind_code_l1", "b", "ind_code_l2"]) == [
        "ind_code_l1",
        "ind_code_l2",
    ]
    assert categorical_feature_names(["a", "b"]) == []


def _ts_block(source: str, marker: str) -> str:
    """截取 TS 中 `marker` 到其后第一个行首 `};` 之间的块。"""
    start = source.index(marker)
    end = source.index("\n};", start)
    return source[start:end]


def _assert_ts_value(block: str, key: str, value: str) -> None:
    pattern = rf"\b{re.escape(key)}:\s*{re.escape(value)}\b"
    assert re.search(pattern, block), f"trainingUtils.tsx 缺少 {key}: {value}"


def test_frontend_default_params_match_backend_baseline() -> None:
    """前端训练页会全量下发参数；默认值漂移等于训练口径漂移。"""
    source = TRAINING_UTILS.read_text(encoding="utf-8")
    block = _ts_block(source, "export const DEFAULT_PARAMS")

    expected = {
        "learning_rate": "0.03",
        "num_leaves": "31",
        "max_depth": "6",
        "min_data_in_leaf": "500",
        "bagging_freq": "1",
        "lambda_l1": "0.5",
        "lambda_l2": "5.0",
        "feature_fraction": "0.7",
        "bagging_fraction": "0.8",
        "num_boost_round": "1000",
        "xgb_max_depth": "5",
        "xgb_subsample": "0.8",
        "xgb_colsample_bytree": "0.8",
        "xgb_reg_lambda": "10.0",
        "xgb_min_child_weight": "500",
        "cb_depth": "5",
        "cb_l2_leaf_reg": "10.0",
        "cb_random_strength": "2.0",
        "cb_bagging_temperature": "1.0",
        "cb_iterations": "2000",
    }
    for key, value in expected.items():
        _assert_ts_value(block, key, value)


def test_frontend_dl_defaults_match_backend_training_behaviour() -> None:
    """GRU 系 lr 必须是 3e-4（后端实证 1e-3 大表首轮过冲）；MLP 必须有专属默认。"""
    source = TRAINING_UTILS.read_text(encoding="utf-8")
    block = _ts_block(source, "export const MODEL_DL_DEFAULTS")

    for model in ("gru", "lstm", "alstm"):
        sub = re.search(rf"\b{model}:\s*\{{([^}}]*)\}}", block)
        assert sub, f"MODEL_DL_DEFAULTS 缺少 {model}"
        assert re.search(r"dl_lr:\s*0\.0003\b", sub.group(1)), f"{model} lr 不是 3e-4"

    mlp = re.search(r"\bmlp:\s*\{([^}]*)\}", block)
    assert mlp, "MODEL_DL_DEFAULTS 缺少 mlp 默认块"
    assert re.search(r"dl_lr:\s*0\.001\b", mlp.group(1))
    assert re.search(r"dl_n_epochs:\s*500\b", mlp.group(1))
    assert re.search(r"dl_hidden_size:\s*128\b", mlp.group(1))


def test_lgb_trainer_declares_categories_and_explicit_early_stopping() -> None:
    source = GBDT_TRAINER.read_text(encoding="utf-8")
    assert "categorical_feature" in source
    assert "first_metric_only=True" in source


# ── 2. 结构适配：TCN 感受野 ──────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("kernel", "layers", "expected"),
    [(5, 2, 13), (5, 3, 29), (7, 2, 19), (3, 4, 31)],
)
def test_tcn_receptive_field(kernel: int, layers: int, expected: int) -> None:
    assert tcn_receptive_field(kernel, layers) == expected


def test_tcn_structure_guard_flags_default_short_window() -> None:
    guard = tcn_structure_guard(5, 2, step_len=20)
    assert guard["receptive_field"] == 13
    assert guard["covers_window"] is False
    assert guard["suggested_num_layers"] == 3
    assert guard["suggested_kernel_size"] == 9

    ok = tcn_structure_guard(5, 3, step_len=20)
    assert ok["covers_window"] is True
    assert ok["suggested_num_layers"] is None


def test_tcn_window_helpers() -> None:
    assert min_layers_for_window(5, 20) == 3
    assert min_layers_for_window(5, 100) == 5
    assert min_kernel_for_window(2, 20) == 9
    assert min_kernel_for_window(3, 20) == 5


# ── 3. 前视泄漏：特征名 ────────────────────────────────────────────────────

def test_feature_guard_stays_dependency_free() -> None:
    """护栏模块会被训练容器导入：不得依赖 fastapi/pydantic 等 API 侧包。"""
    source = FEATURE_GUARD.read_text(encoding="utf-8")
    assert "fastapi" not in source
    assert "pydantic" not in source


def test_hard_label_leaks_are_detected() -> None:
    leaks = find_hard_label_leaks(
        ["pe_ttm", "label", "label_return_h5", "fwd_ret_3", "future_return_10", "mom_ret_5d"]
    )
    assert leaks == ["label", "label_return_h5", "fwd_ret_3", "future_return_10"]


def test_soft_return_names_are_only_warned() -> None:
    assert find_soft_return_features(["return_1d", "return_20d", "mom_ret_5d"]) == [
        "return_1d",
        "return_20d",
    ]
    # 软名字不触发硬闸
    assert_no_feature_leak(["return_5d", "mom_ret_5d", "pe_ttm"])


def test_assert_no_feature_leak_raises_on_label_family() -> None:
    with pytest.raises(RuntimeError, match="前视泄漏"):
        assert_no_feature_leak(["f1", "label_vol"])
    with pytest.raises(RuntimeError, match="前视泄漏"):
        assert_no_feature_leak(["future_ret_5"])


# ── 4. 时点完整性 ────────────────────────────────────────────────────────────

def _frame(days: list[str], values: list[float] | None = None) -> pd.DataFrame:
    n = len(days)
    return pd.DataFrame(
        {
            "symbol": ["SH600000"] * n,
            "trade_date": pd.to_datetime(days),
            "f1": np.asarray(values if values is not None else range(n), dtype=np.float32),
            "label": np.zeros(n, dtype=np.float32),
        }
    )


def test_time_integrity_passes_with_embargo_gap() -> None:
    days = [f"2020-01-{d:02d}" for d in range(1, 31)]
    train = _frame(days[:10])
    val = _frame(days[16:20])
    test = _frame(days[26:30])
    report = time_integrity_report(
        train, val, test, all_dates=pd.to_datetime(days).unique(), horizon=5, lag=1
    )
    assert report["embargo_trading_days"] == 6
    assert report["train_valid_gap_trading_days"] == 6
    assert report["valid_test_gap_trading_days"] == 6


def test_time_integrity_rejects_gap_smaller_than_embargo() -> None:
    days = [f"2020-01-{d:02d}" for d in range(1, 31)]
    train = _frame(days[:10])
    val = _frame(days[12:16])  # 间隔 1 天 < 6
    test = _frame(days[26:30])
    with pytest.raises(RuntimeError, match="embargo"):
        time_integrity_report(
            train, val, test, all_dates=pd.to_datetime(days).unique(), horizon=5, lag=1
        )


def test_time_integrity_rejects_overlapping_segments() -> None:
    days = [f"2020-01-{d:02d}" for d in range(1, 31)]
    train = _frame(days[:12])
    val = _frame(days[10:16])
    test = _frame(days[26:30])
    with pytest.raises(RuntimeError, match="train < valid < test"):
        time_integrity_report(
            train, val, test, all_dates=pd.to_datetime(days).unique(), horizon=5, lag=1
        )


# ── 5. 预处理填充只从 train 估计（不得偷看 val）──────────────────────────────

def test_fill_values_are_estimated_from_train_only() -> None:
    train = _frame(["2020-01-06", "2020-01-07"], values=[1.0, 3.0])
    val = _frame(["2020-02-03", "2020-02-04"], values=[1000.0, 2000.0])

    fill_values, X_train, _, X_val, _, _ = _prepare_arrays(train, val, ["f1"])

    assert fill_values["f1"] == pytest.approx(2.0)
    assert np.asarray(X_train).flatten().tolist() == [1.0, 3.0]
    assert np.asarray(X_val).flatten().tolist() == [1000.0, 2000.0]


def test_cross_sectional_preprocess_never_reads_future_dates() -> None:
    from preprocessing import cross_sectional_preprocess

    day1 = {"trade_date": pd.to_datetime(["2020-01-06"] * 3), "f1": [1.0, 2.0, 3.0]}
    day2 = {"trade_date": pd.to_datetime(["2020-01-07"] * 3), "f1": [10.0, 20.0, 30.0]}
    base = pd.DataFrame({**day1, **day2})

    a = cross_sectional_preprocess(base.copy(), ["f1"], enabled=True).reset_index(drop=True)
    mutated = pd.DataFrame(
        {
            "trade_date": pd.to_datetime(["2020-01-06"] * 3 + ["2020-01-07"] * 3),
            "f1": [1.0, 2.0, 3.0, -999.0, 999.0, 0.0],
        }
    )
    b = cross_sectional_preprocess(mutated, ["f1"], enabled=True).reset_index(drop=True)

    # 改变次日取值不得影响首日截面 Z-score
    pd.testing.assert_series_equal(a.loc[:2, "f1"], b.loc[:2, "f1"], check_exact=True)


# ── 7. WFA 窗口：与主切分同口径的 embargo ───────────────────────────────────

def _wfa_expanding_cfg() -> dict:
    return {
        "strategy": "expanding",
        "val_months": 1,
        "step_months": 1,
        "train_years": 1,
        "start": "",
        "max_train_end": "",
    }


def _wfa_frame(n_days: int) -> pd.DataFrame:
    days = pd.bdate_range("2020-01-01", periods=n_days)
    n = n_days * 2
    return pd.DataFrame(
        {
            "symbol": ["SH600000"] * n,
            "trade_date": np.repeat(days.to_numpy(), 2),
            "f1": np.arange(n, dtype=np.float32),
            "label": np.zeros(n, dtype=np.float32),
        }
    )


def test_wfa_window_applies_embargo_between_train_and_val() -> None:
    df = _wfa_frame(60)
    train, val = split_wfa_window(df, _wfa_expanding_cfg(), 0, embargo_days=6)

    assert not train.empty and not val.empty
    assert train["trade_date"].max() < val["trade_date"].min()
    all_days = sorted(df["trade_date"].unique())
    gap = sum(
        1
        for d in all_days
        if train["trade_date"].max() < d < val["trade_date"].min()
    )
    assert gap >= 6


def test_wfa_window_skips_when_train_too_short_for_embargo() -> None:
    df = _wfa_frame(6)
    train, val = split_wfa_window(df, _wfa_expanding_cfg(), 0, embargo_days=6)
    assert train.empty and val.empty


# ── 8. 过拟合诊断 ────────────────────────────────────────────────────────────

def test_overfit_diagnostics_flags_train_val_gap() -> None:
    diag = overfit_diagnostics(
        {"rank_ic": 0.12}, {"rank_ic": 0.03}, best_iteration=50, num_boost_round=1000
    )
    assert diag["rank_ic_gap"] == pytest.approx(0.09)
    assert diag["overfit_flags"] == ["train_val_gap"]
    assert diag["best_iter_ratio"] == 0.05


def test_overfit_diagnostics_flags_no_signal() -> None:
    diag = overfit_diagnostics({"rank_ic": 0.02}, {"rank_ic": -0.01})
    assert "no_val_signal" in diag["overfit_flags"]


def test_overfit_diagnostics_flags_early_stop_boundaries() -> None:
    immediate = overfit_diagnostics(
        {"rank_ic": 0.04}, {"rank_ic": 0.03}, best_iteration=1, num_boost_round=1000
    )
    assert "stopped_immediately" in immediate["overfit_flags"]

    exhausted = overfit_diagnostics(
        {"rank_ic": 0.04}, {"rank_ic": 0.03}, best_iteration=990, num_boost_round=1000
    )
    assert "rounds_exhausted" in exhausted["overfit_flags"]


def test_overfit_diagnostics_clean_case_has_no_flags() -> None:
    diag = overfit_diagnostics(
        {"rank_ic": 0.06}, {"rank_ic": 0.04}, best_iteration=200, num_boost_round=1000
    )
    assert diag["overfit_flags"] == []
