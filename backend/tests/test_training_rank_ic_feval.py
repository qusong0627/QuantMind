"""rank_ic 监控指标：降采样 + 节流，避免拖慢 LightGBM 多线程训练。"""

from __future__ import annotations

import importlib
import inspect
import sys
import types
from pathlib import Path

import numpy as np
import pytest

TRAINING_ROOT = Path(__file__).resolve().parents[2] / "docker" / "training"


@pytest.fixture(scope="module")
def gbdt():
    """加载 trainers_gbdt，跳过依赖 torch 的包聚合导入。"""
    names = (
        "model_trainers",
        "model_trainers.registry",
        "model_trainers.trainers_gbdt",
    )
    saved = {name: sys.modules.get(name) for name in names}
    sys.path.insert(0, str(TRAINING_ROOT))
    try:
        pkg = types.ModuleType("model_trainers")
        pkg.__path__ = [str(TRAINING_ROOT / "model_trainers")]
        registry = types.ModuleType("model_trainers.registry")
        registry.register_trainer = lambda *args, **kwargs: (lambda fn: fn)
        sys.modules["model_trainers"] = pkg
        sys.modules["model_trainers.registry"] = registry
        module = importlib.import_module("model_trainers.trainers_gbdt")
    finally:
        sys.path.remove(str(TRAINING_ROOT))

    try:
        yield module
    finally:
        for name, mod in saved.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod


class _Dataset:
    def __init__(self, label: np.ndarray) -> None:
        self._label = label

    def get_label(self) -> np.ndarray:
        return self._label


def test_rank_ic_feval_throttles_and_caches(gbdt):
    feval = gbdt._make_lgb_rank_ic_feval(max_rows=500, every_calls=3)
    rng = np.random.default_rng(0)
    label = rng.standard_normal(20_000)
    preds = label + rng.standard_normal(20_000) * 0.1
    dataset = _Dataset(label)

    # 前两次调用直接返回缓存值，不计算
    assert feval(preds, dataset)[1] == 0.0
    assert feval(preds, dataset)[1] == 0.0

    name, rho, higher = feval(preds, dataset)
    assert name == "rank_ic"
    assert rho > 0.9
    assert higher is True

    # 第四次调用命中缓存，与第三次一致
    assert feval(preds, dataset)[1] == rho


def test_rank_ic_feval_samples_before_spearman(gbdt, monkeypatch):
    seen: dict[str, int] = {}
    from scipy import stats

    real_spearmanr = stats.spearmanr

    def spy(x, y, *args, **kwargs):
        seen["n"] = len(x)
        return real_spearmanr(x, y, *args, **kwargs)

    monkeypatch.setattr(stats, "spearmanr", spy)

    feval = gbdt._make_lgb_rank_ic_feval(max_rows=500, every_calls=1)
    rng = np.random.default_rng(1)
    label = rng.standard_normal(10_000)
    feval(label + rng.standard_normal(10_000) * 0.1, _Dataset(label))

    assert seen["n"] <= 500


def test_lightgbm_early_stopping_uses_l2_only(gbdt):
    src = inspect.getsource(gbdt._train_lgb)
    assert "first_metric_only=True" in src
    assert "_make_lgb_rank_ic_feval()" in src
    assert "feval=_lgb_rank_ic_feval" not in src
