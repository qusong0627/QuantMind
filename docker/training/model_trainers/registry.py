"""训练模型注册表与分派（B2/B3 建，B4 由 train.py 拆出）。

- 类型集合是注册键的全集（_ALL_MODEL_TYPES）。
- _dispatch_gbdt_sklearn：GBDT/sklearn 6 参签名查表分派。
- DLAdapter/_dispatch_dl：DL/TFT 组 DataFrame 专属签名适配分派。
- train.py 只保留编排（数据/流程/回测组装），经 import 复用本模块。
"""
from __future__ import annotations
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from model_trainers.metrics import _compute_metrics
from model_trainers.trainers_dl import (
    _predict_dl,
    _predict_nativetft,
    _train_dl,
    _train_nativetft,
)

if TYPE_CHECKING:
    import numpy as np

logger = logging.getLogger("quantmind.train")

_TREE_MODEL_TYPES = {"lightgbm", "xgboost", "catboost", "linear", "random_forest"}

_DL_MODEL_TYPES = {"gru", "lstm", "alstm", "transformer", "tabnet", "tcn", "nativetft", "mlp"}

_CUSTOM_DL_MODEL_TYPES = {"nativetft", "mlp"}  # 非 Qlib 的自定义 DL 模型（hybrid 已剔除，未实现）

_ALL_MODEL_TYPES = _TREE_MODEL_TYPES | _DL_MODEL_TYPES

_ENSEMBLE_MODEL_TYPES = _TREE_MODEL_TYPES - {"linear"}  # 可参与集成的树模型

@dataclass
class Trainer:
    name: str
    train_fn: Any = None  # 6 参签名 (cfg, features, X_train, y_train, X_val, y_val)，GBDT/sklearn 组
    framework: str = "unknown"
    is_dl: bool = False  # DL/TFT 组：走 dl_adapter 而非 train_fn（签名分裂，见 DLAdapter）
    dl_adapter: str | None = None  # "nativetft" | "dl"

MODEL_REGISTRY: dict[str, Trainer] = {}

def register_trainer(name: str, *, framework: str = "unknown", is_dl: bool = False, dl_adapter: str | None = None):
    """装饰器：集中注册，取代分派点 if/elif 链。"""

    def deco(fn):
        MODEL_REGISTRY[name] = Trainer(
            name=name, train_fn=fn, framework=framework, is_dl=is_dl, dl_adapter=dl_adapter
        )
        return fn

    return deco

def _dispatch_gbdt_sklearn(
    cfg: dict,
    model_type: str,
    features: list[str],
    X_train: np.ndarray,
    y_train: np.ndarray,
    X_val: np.ndarray,
    y_val: np.ndarray,
) -> Any:
    """GBDT/sklearn 组统一分派：lightgbm/xgboost/catboost/linear/random_forest/mlp。

    optuna 预搜索与分位分支保留在调用方原逻辑中，此处只替换裸训练函数选择。
    B4：旧 if/elif fallback 已删除（B2 A/B 全过），未知类型直接 ValueError。
    """
    trainer = MODEL_REGISTRY.get(model_type)
    if trainer is None:
        raise ValueError(f"Unsupported model_type: {model_type}")
    return trainer.train_fn(cfg, features, X_train, y_train, X_val, y_val)

class DLAdapter:
    """B3：DL/TFT 组统一分派适配（DataFrame 专属签名，与 6 参 Trainer 分离）。

    single=False → train_model 形状（9-tuple，含完成日志）；
    single=True → _train_single_model 形状（dict，pred 口径与原分支一致）。
    时序预测的 (symbol, trade_date, pred) 对齐、split 归属、_dl_metrics 口径
    均与原分支逐行一致，仅归属判断的换行排版归一（布尔逻辑不变）。
    """

    @staticmethod
    def _merge_full_pred(df, y_full_pred, val_df, test_df):
        full_pred_df = df[["symbol", "trade_date", "label"]].copy()
        full_pred_df = full_pred_df.merge(
            y_full_pred[["symbol", "trade_date", "pred"]],
            on=["symbol", "trade_date"], how="left",
        )
        full_pred_df["split"] = "train"
        full_pred_df.loc[
            (full_pred_df["trade_date"] >= val_df["trade_date"].min())
            & (full_pred_df["trade_date"] <= val_df["trade_date"].max()),
            "split",
        ] = "valid"
        full_pred_df.loc[
            (full_pred_df["trade_date"] >= test_df["trade_date"].min())
            & (full_pred_df["trade_date"] <= test_df["trade_date"].max()),
            "split",
        ] = "test"
        return full_pred_df

    @staticmethod
    def _test_metrics(test_df, full_pred_df):
        test_mask = full_pred_df["split"] == "test"
        y_test_pred = full_pred_df.loc[test_mask, "pred"].values
        y_test_true = full_pred_df.loc[test_mask, "label"].values
        return _compute_metrics(test_df, y_test_true.astype("float32"), y_test_pred.astype("float32"))

    @staticmethod
    def run_nativetft(
        model_type, cfg, features, train_df, val_df, test_df, df, fill_values, hardware,
        *, single, t_start,
    ):
        model_cfg = cfg.get("model", {})
        dl_params = model_cfg.get("dl_params", {})
        output_dir = Path("/workspace")
        model, train_m, val_m, dl_metadata = _train_nativetft(
            model_type, train_df, val_df, features, dl_params, output_dir, hardware=hardware
        )
        if not single:
            logger.info("Training finished in %.2fs (%s)", time.time() - t_start, model_type)
            logger.info(f"Val IC={val_m['ic']:.4f}")
        y_full_pred = _predict_nativetft(output_dir, df, features, dl_metadata)
        full_pred_df = DLAdapter._merge_full_pred(df, y_full_pred, val_df, test_df)
        test_m = DLAdapter._test_metrics(test_df, full_pred_df)
        if single:
            return {
                "model_type": model_type,
                "model": model,
                "fill_values": fill_values,
                "train_m": train_m,
                "val_m": val_m,
                "test_m": test_m,
                "dl_metadata": dl_metadata,
                "full_pred_df": full_pred_df,
                "elapsed": time.time() - t_start,
            }
        return (
            model,
            fill_values,
            train_m,
            val_m,
            test_m,
            full_pred_df.reset_index(drop=True),
            {
                "train": train_df.reset_index(drop=True),
                "valid": val_df.reset_index(drop=True),
                "test": test_df.reset_index(drop=True),
            },
            model_type,
            dl_metadata,
        )

    @staticmethod
    def run_dl(
        model_type, cfg, features, train_df, val_df, test_df, df, fill_values, hardware,
        *, single, t_start,
    ):
        model_cfg = cfg.get("model", {})
        dl_params = model_cfg.get("dl_params", {})
        output_dir = Path("/workspace")
        model, train_m, val_m, dl_metadata = _train_dl(
            model_type, train_df, val_df, features, dl_params, output_dir, hardware=hardware
        )
        if not single:
            logger.info("Training finished in %.2fs (%s)", time.time() - t_start, model_type)
            logger.info(f"Val IC={val_m['ic']:.4f}")
        y_full_pred = _predict_dl(output_dir, df, features, dl_metadata)
        full_pred_df = DLAdapter._merge_full_pred(df, y_full_pred, val_df, test_df)
        test_m = DLAdapter._test_metrics(test_df, full_pred_df)
        if single:
            return {
                "model_type": model_type,
                "model": model,
                "fill_values": fill_values,
                "train_m": train_m,
                "val_m": val_m,
                "test_m": test_m,
                "pred_df": full_pred_df.reset_index(drop=True),
                "split_frames": {
                    "train": train_df.reset_index(drop=True),
                    "valid": val_df.reset_index(drop=True),
                    "test": test_df.reset_index(drop=True),
                },
                "dl_metadata": dl_metadata,
                "elapsed": time.time() - t_start,
            }
        return (
            model,
            fill_values,
            train_m,
            val_m,
            test_m,
            full_pred_df.reset_index(drop=True),
            {
                "train": train_df.reset_index(drop=True),
                "valid": val_df.reset_index(drop=True),
                "test": test_df.reset_index(drop=True),
            },
            model_type,
            dl_metadata,
        )

def _dispatch_dl(
    cfg, model_type, features, train_df, val_df, test_df, df, fill_values, hardware,
    *, single, t_start,
):
    """B3：DL/TFT 组查表分派（B4 已删除旧分支 fallback，见 git 历史）。"""
    trainer = MODEL_REGISTRY.get(model_type)
    if trainer is None or not trainer.is_dl:
        raise ValueError(f"Unsupported DL model_type: {model_type}")
    if trainer.dl_adapter == "nativetft":
        return DLAdapter.run_nativetft(
            model_type, cfg, features, train_df, val_df, test_df, df,
            fill_values, hardware, single=single, t_start=t_start,
        )
    return DLAdapter.run_dl(
        model_type, cfg, features, train_df, val_df, test_df, df,
        fill_values, hardware, single=single, t_start=t_start,
    )

for _dl_name in sorted(_DL_MODEL_TYPES):
    if _dl_name == "mlp":
        continue  # B2 sklearn 注册已覆盖
    MODEL_REGISTRY[_dl_name] = Trainer(
        name=_dl_name,
        framework="pytorch",
        is_dl=True,
        dl_adapter="nativetft" if _dl_name == "nativetft" else "dl",
    )
