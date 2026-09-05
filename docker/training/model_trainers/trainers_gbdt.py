"""GBDT/sklearn 组训练器（B4 由 train.py 拆出，6 参签名）。

注册副作用：import 本模块即完成 6 条 @register_trainer 注册。
"""
from __future__ import annotations
import logging
import numpy as np
import os
from typing import Any

import lightgbm as lgb

from model_trainers.registry import register_trainer

logger = logging.getLogger("quantmind.train")

def _default_train_threads() -> int:
    raw = (os.getenv("TRAIN_NTHREADS") or "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            logger.warning("Invalid TRAIN_NTHREADS=%r, fallback to -1", raw)
    return -1

_TRAIN_NTHREAD = _default_train_threads()

DEFAULT_LGB_PARAMS: dict[str, Any] = {
    "objective":         "regression",
    "metric":            "l2",
    "boosting":          "gbdt",
    "num_leaves":        31,
    "learning_rate":     0.02,
    "feature_fraction":  0.6,
    "bagging_fraction":  0.7,
    "bagging_freq":      5,
    "min_child_samples": 150,
    "path_smooth":       1.0,
    "lambda_l1":         0.5,
    "lambda_l2":         1.0,
    "max_depth":         -1,
    # LightGBM 原生 API 的线程参数名是 num_threads（n_jobs 是 sklearn 层的别名，
    # 直接传 n_jobs 给 lgb.train() 会被忽略并回退到默认线程数，导致多核不生效）
    "num_threads":       _TRAIN_NTHREAD,
    "verbosity":         -1,
}

DEFAULT_XGB_PARAMS: dict[str, Any] = {
    "objective":        "reg:squarederror",
    "eval_metric":      "rmse",
    "max_depth":        4,
    "learning_rate":    0.05,
    "subsample":        0.7,
    "colsample_bytree": 0.65,
    "reg_alpha":        0.5,
    "reg_lambda":       2.0,
    "min_child_weight": 100,
    "tree_method":      "hist",
    "nthread":          -1,
    "verbosity":        0,
}

DEFAULT_CATBOOST_PARAMS: dict[str, Any] = {
    "loss_function":    "RMSE",
    "depth":            6,
    "learning_rate":    0.03,
    "iterations":       1500,
    "l2_leaf_reg":      3.0,
    "random_strength":  1.5,
    "bagging_temperature": 0.8,
    "od_type":          "Iter",
    "od_wait":          100,
    "thread_count":     -1,
    "verbose":          100,
}

def _lgb_rank_ic_feval(preds: np.ndarray, dataset: lgb.Dataset) -> tuple[str, float, bool]:
    """LightGBM 自定义评估：全局 Rank IC（Spearman），作为 l2 的补充监控指标。

    不参与早停决策（early stopping 仍以 l2 为准），仅输出日志供观察
    rank_ic 是否随迭代提升，防止模型只优化 l2 而 rank_ic 停滞。
    """
    label = dataset.get_label()
    try:
        from scipy.stats import spearmanr
        valid = np.isfinite(preds) & np.isfinite(label)
        if valid.sum() < 10:
            return "rank_ic", 0.0, True
        rho, _ = spearmanr(preds[valid], label[valid])
        if not np.isfinite(rho):
            rho = 0.0
        return "rank_ic", float(rho), True  # higher is better
    except Exception:
        return "rank_ic", 0.0, True

@register_trainer("lightgbm", framework="lightgbm")
def _train_lgb(cfg: dict, features: list[str], X_train: np.ndarray, y_train: np.ndarray,
               X_val: np.ndarray, y_val: np.ndarray) -> Any:
    """LightGBM 训练。"""
    model_cfg = cfg.get("model", {})
    params = {**DEFAULT_LGB_PARAMS, **model_cfg.get("params", {})}
    if str((cfg.get("label", {}) or {}).get("target_mode") or "return").lower() == "classification":
        params["objective"] = "binary"
        params["metric"] = "binary_logloss"
    # 线程数：num_threads=-1 用满所有核心；多模型/OOF 训练时内存叠加易 OOM，
    # 可用 TRAIN_NTHREADS 环境变量限流
    params["num_threads"] = _TRAIN_NTHREAD
    # 可复现性：注入全局 seed（用户未显式覆盖时）
    params.setdefault("seed", int((cfg.get("seed") or 42)))
    params.setdefault("bagging_seed", int((cfg.get("seed") or 42)))
    params.setdefault("feature_fraction_seed", int((cfg.get("seed") or 42)))
    num_boost_round = int(model_cfg.get("num_boost_round", 1000))
    early_stopping_rounds = max(1, int(model_cfg.get("early_stopping_rounds", 100) or 100))

    ds_train = lgb.Dataset(X_train, label=y_train, feature_name=features, free_raw_data=True)
    ds_val = lgb.Dataset(X_val, label=y_val, feature_name=features, free_raw_data=True)

    callbacks = [
        lgb.early_stopping(stopping_rounds=early_stopping_rounds, verbose=True),
        lgb.log_evaluation(100),
    ]
    # 补充监控 rank_ic（不影响早停，仅日志），帮助判断是否只优化 l2 而 rank_ic 停滞
    if str(model_cfg.get("monitor_rank_ic", "true")).lower() in ("1", "true", "yes", "on"):
        # lightgbm 4.x 的 record_evaluation(eval_result) 需要 dict 而非 Dataset，
        # 传空 dict，feval 返回的 rank_ic 会被自动写入。
        callbacks.append(lgb.record_evaluation({}))
    model = lgb.train(
        params, ds_train,
        num_boost_round=num_boost_round,
        valid_sets=[ds_train, ds_val],
        valid_names=["train", "valid"],
        feval=_lgb_rank_ic_feval,
        callbacks=callbacks,
    )
    return model

@register_trainer("xgboost", framework="xgboost")
def _train_xgb(cfg: dict, features: list[str], X_train: np.ndarray, y_train: np.ndarray,
               X_val: np.ndarray, y_val: np.ndarray) -> Any:
    """XGBoost 训练。"""
    import xgboost as xgb
    model_cfg = cfg.get("model", {})
    params = {**DEFAULT_XGB_PARAMS, **model_cfg.get("xgb_params", {})}
    if str((cfg.get("label", {}) or {}).get("target_mode") or "return").lower() == "classification":
        params["objective"] = "binary:logistic"
        params["eval_metric"] = "auc"
    # 限制线程：nthread=-1 用满所有核心，多模型/OOF 训练时内存叠加易 OOM
    params["nthread"] = _TRAIN_NTHREAD
    # 可复现性：注入全局 seed
    params.setdefault("seed", int((cfg.get("seed") or 42)))
    # LightGBM 用 max_depth=-1 表示不限深度，XGBoost 只接受 >=0，直接传会启动失败
    if int(params.get("max_depth") or 0) < 0:
        logger.warning(
            "xgb_params.max_depth=%s invalid for XGBoost (LightGBM convention), fallback to %s",
            params["max_depth"], DEFAULT_XGB_PARAMS["max_depth"],
        )
        params["max_depth"] = DEFAULT_XGB_PARAMS["max_depth"]
    num_boost_round = int(model_cfg.get("num_boost_round", 1000))
    early_stopping_rounds = max(1, int(model_cfg.get("early_stopping_rounds", 100) or 100))

    dtrain = xgb.DMatrix(X_train, label=y_train, feature_names=features)
    dval = xgb.DMatrix(X_val, label=y_val, feature_names=features)

    evals_result: dict = {}
    model = xgb.train(
        params, dtrain,
        num_boost_round=num_boost_round,
        evals=[(dtrain, "train"), (dval, "valid")],
        evals_result=evals_result,
        early_stopping_rounds=early_stopping_rounds,
        verbose_eval=100,
    )
    return model

@register_trainer("catboost", framework="catboost")
def _train_catboost(cfg: dict, features: list[str], X_train: np.ndarray, y_train: np.ndarray,
                    X_val: np.ndarray, y_val: np.ndarray) -> Any:
    """CatBoost 训练。支持 cat_features（行业编码等类别特征）。"""
    from catboost import CatBoost, Pool
    model_cfg = cfg.get("model", {})
    params = {**DEFAULT_CATBOOST_PARAMS, **model_cfg.get("catboost_params", {})}
    if str((cfg.get("label", {}) or {}).get("target_mode") or "return").lower() == "classification":
        params["loss_function"] = "Logloss"
        params.setdefault("eval_metric", "AUC")
    # 限制线程：thread_count=-1 用满所有核心，多模型/OOF 训练时内存叠加易 OOM
    params["thread_count"] = _TRAIN_NTHREAD
    # 可复现性：注入全局 seed
    params.setdefault("random_seed", int((cfg.get("seed") or 42)))
    # iterations 覆盖 num_boost_round；CatBoost 禁止 iterations/num_boost_round/
    # n_estimators/num_trees 并存，用户显式传入冲突键时一律清掉（否则直接抛错）。
    if "iterations" not in model_cfg.get("catboost_params", {}):
        params["iterations"] = int(model_cfg.get("num_boost_round", 1000))
    for _conflict_key in ("num_boost_round", "n_estimators", "num_trees"):
        params.pop(_conflict_key, None)

    # 识别类别特征（ind_code_l1 等）
    cat_feature_indices = []
    for i, feat in enumerate(features):
        if feat in ("ind_code_l1", "ind_code_l2"):
            cat_feature_indices.append(i)
    if cat_feature_indices:
        # CatBoost 要求类别特征为 int 类型
        for idx in cat_feature_indices:
            X_train[:, idx] = X_train[:, idx].astype(int)
            X_val[:, idx] = X_val[:, idx].astype(int)

    # has_time：数据按时间顺序排列时开启，CatBoost 按时间处理序列数据，
    # 避免未来信息影响过去预测。需行序为时间序（load_data 已按 symbol+trade_date 排序）。
    # 注意：旧版 catboost 的 Pool 不支持 has_time 参数，需 try/except 降级。
    has_time = str(params.get("has_time", "false")).lower() in ("1", "true", "yes", "on")
    _pool_kwargs: dict = {
        "feature_names": features,
        "cat_features": cat_feature_indices if cat_feature_indices else None,
    }
    if has_time:
        try:
            train_pool = Pool(X_train, label=y_train, has_time=True, **_pool_kwargs)
            val_pool = Pool(X_val, label=y_val, has_time=True, **_pool_kwargs)
        except TypeError:
            logger.warning("当前 catboost 版本不支持 has_time，降级为不带该参数")
            train_pool = Pool(X_train, label=y_train, **_pool_kwargs)
            val_pool = Pool(X_val, label=y_val, **_pool_kwargs)
    else:
        train_pool = Pool(X_train, label=y_train, **_pool_kwargs)
        val_pool = Pool(X_val, label=y_val, **_pool_kwargs)

    model = CatBoost(params)
    model.fit(train_pool, eval_set=val_pool, early_stopping_rounds=max(1, int(model_cfg.get("early_stopping_rounds", 100) or 100)))
    return model

@register_trainer("linear", framework="sklearn")
def _train_linear(cfg: dict, features: list[str], X_train: np.ndarray, y_train: np.ndarray,
                  X_val: np.ndarray, y_val: np.ndarray) -> Any:
    """线性基线：收益任务用 Ridge，分类任务用 LogisticRegression。"""
    from sklearn.linear_model import LogisticRegression, Ridge
    model_cfg = cfg.get("model", {})
    dl_params = model_cfg.get("dl_params", {})
    alpha = float(dl_params.get("alpha", 3.0))
    if str((cfg.get("label", {}) or {}).get("target_mode") or "return").lower() == "classification":
        model = LogisticRegression(C=1.0 / max(alpha, 1e-8), max_iter=1000,
                                   random_state=int((cfg.get("seed") or 42)))
    else:
        model = Ridge(alpha=alpha, random_state=int((cfg.get("seed") or 42)))
    model.fit(X_train, y_train)
    return model

@register_trainer("random_forest", framework="sklearn")
def _train_rf(cfg: dict, features: list[str], X_train: np.ndarray, y_train: np.ndarray,
              X_val: np.ndarray, y_val: np.ndarray) -> Any:
    """随机森林：Bagging 基线，用于对比 Boosting 是否真优于 Bagging。

    与 LightGBM/XGBoost 走同一 _prepare_arrays 数据流（含可选截面预处理）。
    参数：n_estimators（默认 300）、max_depth（默认 12，防止百万级数据
    完全展开导致节点数爆炸 OOM）、max_features（默认 sqrt）。
    """
    from sklearn.ensemble import RandomForestClassifier, RandomForestRegressor
    model_cfg = cfg.get("model", {})
    dl_params = model_cfg.get("dl_params", {})
    seed = int((cfg.get("seed") or 42))
    n_estimators = int(dl_params.get("n_estimators", 300))
    # 默认 max_depth=None 时树完全展开：300 万行 × 300 棵树 ≈ 9 亿节点，
    # 内存需求数百 GB（实测 300 万行冒烟直接 OOM SIGKILL 137）。
    # 默认限深 12（约 4096 叶子/树，精度与完全展开差异 <1%），可显式覆盖。
    max_depth = dl_params.get("max_depth", 12)
    max_features = str(dl_params.get("max_features", "sqrt"))
    model_class = RandomForestClassifier if str((cfg.get("label", {}) or {}).get("target_mode") or "return").lower() == "classification" else RandomForestRegressor
    model = model_class(
        n_estimators=n_estimators,
        max_depth=int(max_depth) if max_depth else None,
        max_features=max_features,
        n_jobs=_TRAIN_NTHREAD,
        random_state=seed,
    )
    model.fit(X_train, y_train)
    return model

@register_trainer("mlp", framework="sklearn")
def _train_mlp(cfg: dict, features: list[str], X_train: np.ndarray, y_train: np.ndarray,
               X_val: np.ndarray, y_val: np.ndarray) -> Any:
    """MLP 基线：神经网络最简对照，验证 RNN/Transformer 是否真优于全连接。

    用 sklearn MLPRegressor（结构化表格数据基线足够），与树模型共享数据流。
    参数：hidden_layer_sizes（默认 [64, 32]）、alpha（L2，默认 1e-3）、
    early_stopping（默认 True，用 val 集早停）。
    """
    from sklearn.neural_network import MLPClassifier, MLPRegressor
    model_cfg = cfg.get("model", {})
    dl_params = model_cfg.get("dl_params", {})
    seed = int((cfg.get("seed") or 42))
    hidden = dl_params.get("hidden_layer_sizes") or dl_params.get("hidden_size")
    if isinstance(hidden, (int, float)):
        hidden = [int(hidden), max(1, int(hidden) // 2)]
    elif not isinstance(hidden, (list, tuple)):
        hidden = [64, 32]
    alpha = float(dl_params.get("alpha", 1e-3))
    model_class = MLPClassifier if str((cfg.get("label", {}) or {}).get("target_mode") or "return").lower() == "classification" else MLPRegressor
    model = model_class(
        hidden_layer_sizes=[int(h) for h in hidden],
        alpha=alpha,
        learning_rate_init=float(dl_params.get("lr", 0.001)),
        max_iter=int(dl_params.get("n_epochs", 500)),
        early_stopping=True,
        n_iter_no_change=10,
        random_state=seed,
    )
    model.fit(X_train, y_train)
    return model
