"""GBDT/sklearn 组训练器（B4 由 train.py 拆出，6 参签名）。

注册副作用：import 本模块即完成 6 条 @register_trainer 注册。
"""
from __future__ import annotations
import logging
import numpy as np
import os
import pandas as pd
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
    # 机构级起点（2026-11-09）：按 A 股日频选股的稳健区间取中值；
    # 这是起点不是调参终点，payload 的 params 可逐项覆盖。
    "objective":         "regression",
    "metric":            "l2",
    "boosting":          "gbdt",
    "num_leaves":        31,
    "learning_rate":     0.03,
    "feature_fraction":  0.7,
    "bagging_fraction":  0.8,
    "bagging_freq":      1,
    "min_data_in_leaf":  500,
    "path_smooth":       1.0,
    "lambda_l1":         0.5,
    "lambda_l2":         5.0,
    "max_depth":         6,
    # LightGBM 原生 API 的线程参数名是 num_threads（n_jobs 是 sklearn 层的别名，
    # 直接传 n_jobs 给 lgb.train() 会被忽略并回退到默认线程数，导致多核不生效）
    "num_threads":       _TRAIN_NTHREAD,
    "verbosity":         -1,
}

DEFAULT_XGB_PARAMS: dict[str, Any] = {
    # 机构级起点（2026-11-09）：hist + 0.03 lr + 深度 5 + 强正则
    "objective":        "reg:squarederror",
    "eval_metric":      "rmse",
    "max_depth":        5,
    "learning_rate":    0.03,
    "subsample":        0.8,
    "colsample_bytree": 0.8,
    "reg_alpha":        0.5,
    "reg_lambda":       10.0,
    "min_child_weight": 100,
    "tree_method":      "hist",
    "nthread":          -1,
    "verbosity":        0,
}

DEFAULT_CATBOOST_PARAMS: dict[str, Any] = {
    # 机构级起点（2026-11-09）：深度 5 + l2 10 + 2000 轮 + 早停 100
    "loss_function":    "RMSE",
    "depth":            5,
    "learning_rate":    0.03,
    "iterations":       2000,
    "l2_leaf_reg":      10.0,
    "random_strength":  2.0,
    "bagging_temperature": 1.0,
    "od_type":          "Iter",
    "od_wait":          100,
    "thread_count":     -1,
    "verbose":          100,
}

# ── 排序损失（IC / LambdaRank 类）────────────────────────────────────────────
# 三个 GBDT 的排序目标名不同；显式传这些 objective 时，训练端必须同时提供
# 「按日分组」信息（group/group_id），否则 LightGBM/CatBoost/XGBoost 直接报错。
RANK_OBJECTIVES: dict[str, set[str]] = {
    "lightgbm": {"lambdarank", "rank_xendcg"},
    "xgboost": {"rank:pairwise", "rank:ndcg", "rank:map"},
    "catboost": {"yetirank", "yetirankpairwise", "queryrmse", "querysoftmax", "queryauc", "lambdamart"},
}


def _group_counts(dates: Any) -> np.ndarray:
    """按日分组的连续样本数（LightGBM/XGBoost 的 group 参数）。dates 必须已按日排序。"""
    _, counts = np.unique(np.asarray(dates), return_counts=True)
    return counts.astype(np.int32)


def _bin_rank_labels(y: Any, dates: Any, n_bins: int = 10) -> np.ndarray:
    """把连续 rank 标签按日分箱成 0..n_bins-1 的 relevance（LambdaRank/NDCG 需要整数档位）。"""
    s = pd.Series(np.asarray(y))
    d = pd.Series(np.asarray(dates))
    r = s.groupby(d).rank(pct=True)
    return np.floor(r * n_bins).clip(0, n_bins - 1).astype(np.int32).to_numpy()


def _factorize_groups(dates: Any) -> np.ndarray:
    """CatBoost group_id：按日排序后行号即组号；返回每行的组 id。"""
    return pd.factorize(pd.Series(np.asarray(dates)), sort=True)[0].astype(np.int32)


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
               X_val: np.ndarray, y_val: np.ndarray, groups: tuple | None = None) -> Any:
    """LightGBM 训练；objective 为 lambdarank/rank_xendcg 时按日分组。"""
    model_cfg = cfg.get("model", {})
    params = {**DEFAULT_LGB_PARAMS, **model_cfg.get("params", {})}
    if str((cfg.get("label", {}) or {}).get("target_mode") or "return").lower() == "classification":
        params["objective"] = "binary"
        params["metric"] = "binary_logloss"
    _objective = str(params.get("objective") or "").lower()
    _rank = _objective in RANK_OBJECTIVES["lightgbm"] and groups is not None
    if _rank and str(params.get("metric") or "").lower() in ("l2", "rmse", "regression"):
        params["metric"] = "ndcg"
    # 线程数：num_threads=-1 用满所有核心；多模型/OOF 训练时内存叠加易 OOM，
    # 可用 TRAIN_NTHREADS 环境变量限流
    params["num_threads"] = _TRAIN_NTHREAD
    # 可复现性：注入全局 seed（用户未显式覆盖时）
    params.setdefault("seed", int((cfg.get("seed") or 42)))
    params.setdefault("bagging_seed", int((cfg.get("seed") or 42)))
    params.setdefault("feature_fraction_seed", int((cfg.get("seed") or 42)))
    num_boost_round = int(model_cfg.get("num_boost_round", 1000))
    early_stopping_rounds = max(1, int(model_cfg.get("early_stopping_rounds", 100) or 100))

    if _rank:
        gtr, gva = groups
        # lambdarank 只吃整数 relevance；rank_xendcg 支持连续标签
        y_tr = _bin_rank_labels(y_train, gtr) if _objective == "lambdarank" else y_train
        y_va = _bin_rank_labels(y_val, gva) if _objective == "lambdarank" else y_val
        ds_train = lgb.Dataset(
            X_train, label=y_tr, group=_group_counts(gtr),
            feature_name=features, free_raw_data=True,
        )
        ds_val = lgb.Dataset(
            X_val, label=y_va, group=_group_counts(gva),
            feature_name=features, reference=ds_train, free_raw_data=True,
        )
        logger.info(
            "LightGBM rank loss: objective=%s train_groups=%d val_groups=%d",
            _objective, len(np.unique(np.asarray(gtr))), len(np.unique(np.asarray(gva))),
        )
    else:
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
               X_val: np.ndarray, y_val: np.ndarray, groups: tuple | None = None) -> Any:
    """XGBoost 训练；objective 为 rank:* 时按日分组。"""
    import xgboost as xgb
    model_cfg = cfg.get("model", {})
    params = {**DEFAULT_XGB_PARAMS, **model_cfg.get("xgb_params", {})}
    if str((cfg.get("label", {}) or {}).get("target_mode") or "return").lower() == "classification":
        params["objective"] = "binary:logistic"
        params["eval_metric"] = "auc"
    _objective = str(params.get("objective") or "").lower()
    _rank = _objective in RANK_OBJECTIVES["xgboost"] and groups is not None
    if _rank and _objective in ("rank:ndcg", "rank:map"):
        params.setdefault("eval_metric", "ndcg")
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

    # QuantileDMatrix：hist 训练的省内存构造，不落 CSR 稠密副本（对比 DMatrix
    # 在 7.4M×273 float32 上省约 7GB + 直方图构建更省）。2026-09-15 XGB 在
    # DMatrix 构造处峰值冲顶 OOM(SIGKILL 137) 后切换；valid 用 ref=dtrain
    # 复用训练分箱。分箱细节（sketch 分位）与 DMatrix 略有差异，属内部实现。
    if _rank:
        gtr, gva = groups
        # 所有 rank:* 目标统一用整数 relevance 档位：rank:pairwise + ndcg 指标
        # 也要求整数标签（XGBoost 实测报 label_is_integer），分 10 档损失极小。
        y_tr = _bin_rank_labels(y_train, gtr)
        y_va = _bin_rank_labels(y_val, gva)
        dtrain = xgb.QuantileDMatrix(X_train, label=y_tr, feature_names=features)
        dval = xgb.QuantileDMatrix(X_val, label=y_va, feature_names=features, ref=dtrain)
        dtrain.set_group(_group_counts(gtr))
        dval.set_group(_group_counts(gva))
        logger.info(
            "XGBoost rank loss: objective=%s train_groups=%d val_groups=%d",
            _objective, len(np.unique(np.asarray(gtr))), len(np.unique(np.asarray(gva))),
        )
    else:
        dtrain = xgb.QuantileDMatrix(X_train, label=y_train, feature_names=features)
        dval = xgb.QuantileDMatrix(
            X_val, label=y_val, feature_names=features, ref=dtrain
        )


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
                    X_val: np.ndarray, y_val: np.ndarray, groups: tuple | None = None) -> Any:
    """CatBoost 训练。支持 cat_features；YetiRank/Query* 时按日传 group_id。"""
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
    _loss = str(params.get("loss_function") or "").lower()
    _rank = _loss in RANK_OBJECTIVES["catboost"] and groups is not None
    if _rank:
        gtr, gva = groups
        train_pool = Pool(
            X_train, label=y_train, group_id=_factorize_groups(gtr), **_pool_kwargs
        )
        val_pool = Pool(
            X_val, label=y_val, group_id=_factorize_groups(gva), **_pool_kwargs
        )
        logger.info(
            "CatBoost rank loss: loss=%s train_groups=%d val_groups=%d",
            _loss, len(np.unique(np.asarray(gtr))), len(np.unique(np.asarray(gva))),
        )
    elif has_time:
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

def _scaled_pipeline(model: Any) -> Any:
    """尺度敏感模型统一预处理：先 z-score 再拟合/预测（scaler 随模型一起 pickle）。

    Cross300 因子的量纲跨 20 个数量级（入选列 absmax 达 9e23）。Ridge/MLP 直接
    吃原始值会被极端列主导：Ridge 的 float32 正规方程溢出（2026-10-18 首轮
    Linear 实败），MLP 首层激活/梯度爆炸（同日 MLP 预测 rmse 1.8e11、三档
    rank IC 全负）。StandardScaler 与回归器同装在 Pipeline 里落盘，预测路径
    `model.predict` 自动复用训练集均值/方差，训练与推理同口径。
    """
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler

    return Pipeline([("scaler", StandardScaler()), ("model", model)])


@register_trainer("linear", framework="sklearn")
def _train_linear(cfg: dict, features: list[str], X_train: np.ndarray, y_train: np.ndarray,
                  X_val: np.ndarray, y_val: np.ndarray) -> Any:
    """线性基线：收益任务用 Ridge，分类任务用 LogisticRegression（均先标准化）。"""
    from sklearn.linear_model import LogisticRegression, Ridge
    model_cfg = cfg.get("model", {})
    dl_params = model_cfg.get("dl_params", {})
    alpha = float(dl_params.get("alpha", 3.0))
    if str((cfg.get("label", {}) or {}).get("target_mode") or "return").lower() == "classification":
        model = LogisticRegression(C=1.0 / max(alpha, 1e-8), max_iter=1000,
                                   random_state=int((cfg.get("seed") or 42)))
    else:
        model = Ridge(alpha=alpha, random_state=int((cfg.get("seed") or 42)))
    model = _scaled_pipeline(model)
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

    用 sklearn MLPRegressor（结构化表格数据基线足够），与树模型共享数据流；
    入模前经 `_scaled_pipeline` 标准化——MLP 不像树模型那样对量纲免疫，
    原始 Cross300 因子的极端量纲会把首层激活与梯度直接打爆。
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
    # batch_size 必须显式传递：sklearn 默认 200，在 640 万行上意味着每轮
    # 3.2 万次微型更新（Python 循环瓶颈，实测 28 分钟仍未跑完首轮预算）。
    # 尊重载荷的 batch_size（DL 批统一 4000）后单轮约 8 秒。
    batch_size = int(dl_params.get("batch_size", 200) or 200)
    model = _scaled_pipeline(
        model_class(
            hidden_layer_sizes=[int(h) for h in hidden],
            alpha=alpha,
            learning_rate_init=float(dl_params.get("lr", 0.001)),
            batch_size=batch_size,
            max_iter=int(dl_params.get("n_epochs", 500)),
            early_stopping=True,
            n_iter_no_change=10,
            random_state=seed,
        )
    )
    model.fit(X_train, y_train)
    return model
