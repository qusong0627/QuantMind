"""训练超参机构基准（单一事实源，纯常量，不 import 第三方库）。

为什么单独成文件：
- 训练容器（trainers_gbdt.py）、编排层（train.py）、测试（backtest 口径）都要读
  同一组起点；放在 `trainers_gbdt` 里会被 `model_trainers.registry → trainers_dl`
  的 torch 依赖牵连，测试与轻量调用方无法单独导入。
- 这里的值是「机构起点」而不是「最优值」：按 A 股日频截面（8M 行、151–4700
  列、低信噪）取稳健区间中值，payload 的显式参数可逐项覆盖。

前端 `electron/src/pages/training/trainingUtils.tsx` 的 DEFAULT_PARAMS 必须与
本文件保持一致（`backend/tests/test_training_institutional_defaults.py` 会校验）。

线程参数统一写 -1，由各训练器在实际训练前用 `TRAIN_NTHREAD` 环境变量覆盖
（多模型/OOF 并训时需要限流防 OOM）。
"""

from __future__ import annotations

from typing import Any

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
    "num_threads":       -1,
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
    # 与 LGB min_data_in_leaf=500 同口径：平方损失下 hessian≈1/样本，
    # min_child_weight=100 等价每叶仅 100 个样本，8M 行面板上叶子过细。
    "min_child_weight": 500,
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
