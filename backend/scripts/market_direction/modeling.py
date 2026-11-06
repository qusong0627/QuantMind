"""大盘方向模型的训练内核：内部 purged CV、三族模型、概率校准与评估。

切分协议（与 ``features.SPLIT_BOUNDS`` 一致）：

    train  2018-01-02 ~ 2024-05-31
    valid  2024-06-03 ~ 2025-06-30   （模型选择 / 概率校准 / 阈值选择）
    test   2025-07-01 ~ 2026-06-30   （一次性样本外，不参与任何选择）
    oos    2026-07-01 ~ 2026-11-05   （最终留出）

每段尾部按 ``horizon + 2`` 剔除「标签窗口伸进下一段」的样本。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from backend.scripts.market_direction.features import long_flat_curve

MODEL_NAMES = (
    "lightgbm",
    "lightgbm_tiny",
    "catboost",
    "logistic",
    "composite",
    "trend",
    "ensemble",
)


@dataclass
class FittedModel:
    name: str
    model: Any
    best_iteration: int | None = None
    feature_importance: dict[str, float] = field(default_factory=dict)

    def predict_proba(self, X: pd.DataFrame) -> np.ndarray:
        if self.name in ("lightgbm", "lightgbm_tiny"):
            return np.asarray(self.model.predict(X.to_numpy()), dtype=float)
        if self.name == "catboost":
            proba = self.model.predict_proba(X)
            return np.asarray(proba, dtype=float)[:, 1]
        if self.name in ("composite", "trend"):
            return self.model.predict_proba(X)
        return np.asarray(self.model.predict_proba(X), dtype=float)[:, 1]


def inner_folds(
    n_train: int, horizon: int, n_folds: int = 4, block: int = 252, embargo: int = 2
) -> list[tuple[int, int, int, int]]:
    """扩窗内层 CV：返回 (train_start, train_end, val_start, val_end) 位置元组。"""
    folds: list[tuple[int, int, int, int]] = []
    for k in range(n_folds):
        val_end = n_train - block * (n_folds - 1 - k)
        val_start = val_end - block
        train_end = val_start - (horizon + embargo)
        if train_end < 300 or val_start <= 0 or val_end > n_train:
            continue
        folds.append((0, train_end, val_start, val_end))
    return folds


def _lgb_params(seed: int, profile: str = "default") -> dict[str, Any]:
    params: dict[str, Any] = {
        "objective": "binary",
        "metric": "auc",
        "learning_rate": 0.03,
        "num_leaves": 7,
        "max_depth": 3,
        "min_data_in_leaf": 40,
        "feature_fraction": 0.35,
        "bagging_fraction": 0.7,
        "bagging_freq": 1,
        "lambda_l1": 0.5,
        "lambda_l2": 10.0,
        "verbosity": -1,
        "seed": seed,
        "num_threads": 8,
        "deterministic": True,
        "force_row_wise": True,
    }
    if profile == "tiny":
        params.update(
            {
                "num_leaves": 3,
                "max_depth": 2,
                "min_data_in_leaf": 100,
                "feature_fraction": 0.2,
                "lambda_l2": 30.0,
            }
        )
    return params


def fit_lightgbm(
    X: pd.DataFrame,
    y: np.ndarray,
    folds: list[tuple[int, int, int, int]],
    seed: int = 42,
    profile: str = "default",
) -> FittedModel:
    import lightgbm as lgb

    best_iters: list[int] = []
    for _, tr_end, va_start, va_end in folds:
        train = lgb.Dataset(X.iloc[:tr_end], label=y[:tr_end])
        valid = lgb.Dataset(X.iloc[va_start:va_end], label=y[va_start:va_end])
        booster = lgb.train(
            _lgb_params(seed, profile),
            train,
            num_boost_round=800,
            valid_sets=[valid],
            callbacks=[lgb.early_stopping(50, verbose=False)],
        )
        best_iters.append(int(booster.best_iteration or 400))
    rounds = max(50, int(np.median(best_iters))) if best_iters else 300

    model = lgb.train(
        _lgb_params(seed, profile), lgb.Dataset(X, label=y), num_boost_round=rounds
    )
    importance = dict(
        zip(X.columns, model.feature_importance(importance_type="gain"), strict=False)
    )
    return FittedModel("lightgbm", model, rounds, importance)


def fit_catboost(
    X: pd.DataFrame,
    y: np.ndarray,
    folds: list[tuple[int, int, int, int]],
    seed: int = 42,
) -> FittedModel:
    from catboost import CatBoostClassifier

    best_iters: list[int] = []
    for _, tr_end, va_start, va_end in folds:
        model = CatBoostClassifier(
            iterations=800,
            learning_rate=0.03,
            depth=3,
            l2_leaf_reg=10.0,
            rsm=0.35,
            loss_function="Logloss",
            eval_metric="AUC",
            random_seed=seed,
            od_type="Iter",
            od_wait=50,
            verbose=False,
            allow_writing_files=False,
            thread_count=8,
        )
        model.fit(
            X.iloc[:tr_end],
            y[:tr_end],
            eval_set=(X.iloc[va_start:va_end], y[va_start:va_end]),
            use_best_model=True,
        )
        best_iters.append(int(model.get_best_iteration() or 400))
    rounds = max(50, int(np.median(best_iters))) if best_iters else 300

    final = CatBoostClassifier(
        iterations=rounds,
        learning_rate=0.03,
        depth=3,
        l2_leaf_reg=10.0,
        rsm=0.35,
        loss_function="Logloss",
        random_seed=seed,
        verbose=False,
        allow_writing_files=False,
        thread_count=8,
    )
    final.fit(X, y)
    importance = dict(zip(X.columns, final.get_feature_importance(), strict=False))
    return FittedModel("catboost", final, rounds, importance)


def fit_logistic(
    X: pd.DataFrame, y: np.ndarray, folds: list[tuple[int, int, int, int]]
) -> FittedModel:
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler

    def _pipeline(c: float) -> Pipeline:
        return Pipeline(
            [
                ("impute", SimpleImputer(strategy="median")),
                ("scale", StandardScaler()),
                ("clf", LogisticRegression(C=c, max_iter=3000, solver="lbfgs")),
            ]
        )

    best_c, best_auc = 0.1, -np.inf
    for c in (0.01, 0.1, 1.0):
        scores = []
        for _, tr_end, va_start, va_end in folds:
            pipe = _pipeline(c).fit(X.iloc[:tr_end], y[:tr_end])
            proba = pipe.predict_proba(X.iloc[va_start:va_end])[:, 1]
            from backend.scripts.market_direction.features import auc_score

            value = auc_score(y[va_start:va_end], proba)
            if np.isfinite(value):
                scores.append(value)
        mean_auc = float(np.mean(scores)) if scores else float("nan")
        if np.isfinite(mean_auc) and mean_auc > best_auc:
            best_auc, best_c = mean_auc, c

    model = _pipeline(best_c).fit(X, y)
    coefs = model.named_steps["clf"].coef_[0]
    importance = dict(zip(X.columns, np.abs(coefs), strict=False))
    return FittedModel("logistic", model, None, importance)


def fit_models(
    X: pd.DataFrame, y: np.ndarray, horizon: int, names: tuple[str, ...]
) -> dict[str, FittedModel]:
    folds = inner_folds(len(X), horizon)
    out: dict[str, FittedModel] = {}
    if "lightgbm" in names:
        out["lightgbm"] = fit_lightgbm(X, y, folds)
    if "lightgbm_tiny" in names:
        out["lightgbm_tiny"] = fit_lightgbm(X, y, folds, profile="tiny")
    if "catboost" in names:
        out["catboost"] = fit_catboost(X, y, folds)
    if "logistic" in names:
        out["logistic"] = fit_logistic(X, y, folds)
    if "composite" in names:
        composite = CompositeModel.fit(X, y, list(X.columns))
        out["composite"] = FittedModel(
            "composite", composite, None, composite.feature_importance
        )
    if "trend" in names:
        trend = TrendModel()
        out["trend"] = FittedModel("trend", trend, None, trend.feature_importance)
    if "ensemble" in names:
        members = [m for k, m in out.items() if k != "composite"]
        if len(members) >= 2:
            out["ensemble"] = FittedModel("ensemble", members)  # type: ignore[arg-type]
    return out


# ---------------------------------------------------------------------------
# 训练窗内特征稳定性筛选（不碰 valid/test）
# ---------------------------------------------------------------------------


def stable_features(
    X: pd.DataFrame,
    y: np.ndarray,
    n_blocks: int = 4,
    min_sign_blocks: int = 3,
    min_abs_edge: float = 0.015,
    max_features: int = 24,
) -> list[str]:
    """按训练窗内分块 AUC 的符号一致性挑特征。

    规则：把训练窗等分成 ``n_blocks`` 段，逐段算单因子 AUC；
    要求 ≥ ``min_sign_blocks`` 段与全训练窗同向，且各段 |AUC-0.5| 的
    最小值 ≥ ``min_abs_edge``；最后按「最差段 edge」降序取前 N。
    """
    from backend.scripts.market_direction.features import auc_score

    n = len(X)
    block = n // n_blocks
    if block < 60:
        return []
    scores: list[tuple[str, float]] = []
    for col in X.columns:
        x = X[col].to_numpy(float)
        full = auc_score(y, x)
        if not np.isfinite(full) or abs(full - 0.5) < min_abs_edge:
            continue
        direction = 1.0 if full > 0.5 else -1.0
        edges: list[float] = []
        signs = 0
        for k in range(n_blocks):
            start = k * block
            end = n if k == n_blocks - 1 else (k + 1) * block
            block_auc = auc_score(y[start:end], x[start:end])
            if np.isfinite(block_auc):
                edge = (block_auc - 0.5) * direction
                edges.append(edge)
                signs += int(edge > 0)
        if len(edges) < n_blocks or signs < min_sign_blocks:
            continue
        worst = float(np.min(edges))
        if worst < min_abs_edge / 2:
            continue
        scores.append((col, worst))
    scores.sort(key=lambda kv: -kv[1])
    return [name for name, _ in scores[:max_features]]


class CompositeModel:
    """等权符号合成：每因子按训练窗方向 z-score 后平均（零参数，抗过拟合）。"""

    def __init__(
        self,
        features: list[str],
        signs: np.ndarray,
        center: np.ndarray,
        scale: np.ndarray,
        gain: float = 1.0,
    ):
        self.features = features
        self.signs = signs
        self.center = center
        self.scale = scale
        self.gain = gain

    @classmethod
    def fit(cls, X: pd.DataFrame, y: np.ndarray, features: list[str]) -> CompositeModel:
        from backend.scripts.market_direction.features import auc_score

        sub = X[features].to_numpy(float)
        center = np.nanmedian(sub, axis=0)
        scale = np.nanstd(sub, axis=0)
        scale = np.where(np.isfinite(scale) & (scale > 0), scale, 1.0)
        signs = np.zeros(len(features))
        for j, name in enumerate(features):
            column = X[name].to_numpy(float)
            ok = np.isfinite(column) & np.isfinite(y)
            value = auc_score(y[ok], column[ok]) if ok.sum() > 50 else np.nan
            signs[j] = 1.0 if (np.isfinite(value) and value >= 0.5) else -1.0
        return cls(features, signs, center, scale)

    def score(self, X: pd.DataFrame) -> np.ndarray:
        sub = X[self.features].to_numpy(float)
        z = (sub - self.center) / self.scale
        z = np.where(np.isfinite(z), z, 0.0)
        return z @ self.signs / max(len(self.features), 1)

    def predict_proba(self, X: pd.DataFrame) -> np.ndarray:
        return _sigmoid_safe(self.score(X) * self.gain)

    @property
    def feature_importance(self) -> dict[str, float]:
        return dict.fromkeys(self.features, 1.0)


class TrendModel:
    """规则基线：均线/回撤趋势投票（零拟合，必须打败的基准）。"""

    FEATURES = (
        "idx_ma_gap_20",
        "idx_ma_gap_60",
        "idx_ma_gap_120",
        "idx_ret_60",
        "idx_dd_252",
    )

    @property
    def feature_importance(self) -> dict[str, float]:
        return dict.fromkeys(self.FEATURES, 1.0)

    def predict_proba(self, X: pd.DataFrame) -> np.ndarray:
        votes = np.zeros(len(X))
        used = 0
        for name in self.FEATURES:
            if name not in X.columns:
                continue
            votes += np.sign(X[name].to_numpy(float))
            used += 1
        score = votes / max(used, 1)
        return _sigmoid_safe(2.0 * score)


def _sigmoid_safe(z: np.ndarray) -> np.ndarray:
    z = np.asarray(z, dtype=float)
    return 1.0 / (1.0 + np.exp(-np.clip(z, -20, 20)))


def prior_shift_probs(
    p: np.ndarray, source_rate: float, target_rates: np.ndarray
) -> np.ndarray:
    """先验基础率平移（逐日目标率）：只改截距，不改变排序。"""
    eps = 1e-6
    p = np.clip(np.asarray(p, dtype=float), eps, 1 - eps)
    source = float(np.clip(source_rate, eps, 1 - eps))
    target = np.clip(np.asarray(target_rates, dtype=float), eps, 1 - eps)
    delta = np.log(target / (1 - target)) - np.log(source / (1 - source))
    return _sigmoid_safe(np.log(p / (1 - p)) + delta)


def predict(model: FittedModel, X: pd.DataFrame) -> np.ndarray:
    if model.name != "ensemble":
        return model.predict_proba(X)
    probs = [m.predict_proba(X) for m in model.model]
    return np.mean(probs, axis=0)


# ---------------------------------------------------------------------------
# 评估
# ---------------------------------------------------------------------------


def evaluate_binary(
    y: np.ndarray, p: np.ndarray, horizon: int, label: str
) -> dict[str, Any]:
    from backend.scripts.market_regime.calibration import (
        brier,
        brier_skill,
        ece,
        logloss,
        reliability_table,
    )

    from backend.scripts.market_direction.features import (
        auc_score,
        block_bootstrap_auc,
    )

    y = np.asarray(y, dtype=float)
    p = np.asarray(p, dtype=float)
    ok = np.isfinite(y) & np.isfinite(p)
    y, p = y[ok], p[ok]
    n = len(y)
    result: dict[str, Any] = {"label": label, "n": n}
    if n == 0:
        return result
    base_rate = float(np.mean(y))
    pred = (p >= 0.5).astype(float)
    result.update(
        {
            "base_rate": round(base_rate, 4),
            "mean_p": round(float(np.mean(p)), 4),
            "auc": round(auc_score(y, p), 4),
            "acc": round(float(np.mean(pred == y)), 4),
            "brier": round(brier(y, p), 4),
            "brier_skill": round(brier_skill(y, p, base_rate), 4),
            "logloss": round(logloss(y, p), 4),
            "ece": round(ece(y, p), 4),
        }
    )
    lo, hi = block_bootstrap_auc(y, p, block=max(horizon, 2))
    result["auc_ci"] = [round(lo, 4), round(hi, 4)]
    result["balanced_acc"] = round(
        0.5 * (float(np.mean(pred[y == 1] == 1)) if (y == 1).any() else np.nan)
        + 0.5 * (float(np.mean(pred[y == 0] == 0)) if (y == 0).any() else np.nan),
        4,
    )
    result["reliability"] = reliability_table(y, p, bins=8)
    return result


def selective_table(y: np.ndarray, p: np.ndarray) -> list[dict[str, Any]]:
    """高置信子集命中率（回答「只在最有把握时出手，胜率多少」）。"""
    from backend.scripts.market_direction.features import wilson_interval

    y = np.asarray(y, dtype=float)
    p = np.asarray(p, dtype=float)
    ok = np.isfinite(y) & np.isfinite(p)
    y, p = y[ok], p[ok]
    rows: list[dict[str, Any]] = []
    for threshold in (0.5, 0.55, 0.6, 0.65):
        for side, mask in (("up", p >= threshold), ("down", p <= 1 - threshold)):
            n = int(mask.sum())
            if n == 0:
                continue
            hit = int(np.sum(y[mask] == (1 if side == "up" else 0)))
            lo, hi = wilson_interval(hit, n)
            rows.append(
                {
                    "side": side,
                    "threshold": threshold,
                    "n": n,
                    "share": round(n / len(y), 3),
                    "hit_rate": round(hit / n, 4),
                    "wilson_lo": round(lo, 4),
                    "wilson_hi": round(hi, 4),
                }
            )
    return rows


def strategy_eval(
    returns: np.ndarray, p: np.ndarray, horizon: int, threshold: float = 0.5
) -> dict[str, Any]:
    """非重叠采样（每 horizon 天取一个点）的 long/flat 策略 vs 买入持有。"""
    returns = np.asarray(returns, dtype=float)
    p = np.asarray(p, dtype=float)
    n = len(returns)
    idx = np.arange(0, n, max(horizon, 1))
    r = returns[idx]
    signal = (p[idx] >= threshold).astype(float)
    strategy = long_flat_curve(r, signal, cost_per_switch=0.0005)
    benchmark = long_flat_curve(r, np.ones(len(r)), cost_per_switch=0.0)
    return {
        "threshold": threshold,
        "strategy": {k: round(v, 4) for k, v in strategy.items()},
        "buy_hold": {k: round(v, 4) for k, v in benchmark.items()},
    }
