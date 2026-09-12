"""多算法训练产物拆分：per-algorithm 元数据重建。

一次「多算法训练」的 metadata.json 只记录**主模型**（primary）的算法元数据
（``model_class_name`` / ``model_params`` / ``input_spec`` 等来自训练端的
``dl_metadata``）。拆分出的其它算法子模型若原样继承父 metadata，推理端会按
主模型的架构重建权重（实测 6 个 DL 子模型全部
``RuntimeError: size mismatch``）。

本模块按训练端（``docker/training/model_trainers/trainers_dl.py``）同一口径，
从父 metadata 的 ``dl_params`` + 特征列重建每个算法自己的架构参数，以及从
``comparison`` 提取该算法自己的指标，供
``ModelRegistryService.split_multi_model_entries`` 写出单算法 metadata。

口径一致性：训练端把 ``model_params`` 写进 metadata 时会剔除
GPU / n_epochs / lr / batch_size / early_stop / metric（推理端补 GPU=-1），
本模块产出同样剔除后的字段。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Qlib DL 类名映射（与训练端 _QLIB_TS_MODEL_MAP / _QLIB_FLAT_MODEL_MAP 一致）
ALGO_CLASS_NAMES: dict[str, str] = {
    "gru": "GRU",
    "lstm": "LSTM",
    "alstm": "ALSTM",
    "transformer": "TransformerModel",
    "tcn": "TCN",
    "tabnet": "TabnetModel",
}

# 序列（3D 窗口）模型；nativetft 非 Qlib 类但有独立架构元数据
SEQUENCE_TYPES = frozenset({"gru", "lstm", "alstm", "transformer", "tcn", "nativetft"})
DL_TYPES = frozenset(set(ALGO_CLASS_NAMES) | {"nativetft"})

# 与训练端 _get_model_framework 一致
ALGO_FRAMEWORKS: dict[str, str] = {
    "lightgbm": "lightgbm",
    "xgboost": "xgboost",
    "catboost": "catboost",
    "linear": "sklearn",
    "random_forest": "sklearn",
    "gru": "pytorch",
    "lstm": "pytorch",
    "alstm": "pytorch",
    "transformer": "pytorch",
    "tabnet": "pytorch",
    "tcn": "pytorch",
    "nativetft": "pytorch",
    "mlp": "pytorch",
}

# 父 metadata 里属于「主算法」私有的字段：拆分子模型时先剔除再按算法重建，
# 避免把主模型（如 GRU）的架构/迭代数写给别的算法。
ALGO_SCOPED_FIELDS = (
    "model_class_name",
    "model_params",
    "is_sequence_model",
    "input_spec",
    "feat_norm",
    "dl_params",
    "model_arch",
    "best_iteration",
)

_DEFAULT_STEP_LEN = 20


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _feature_columns(parent_meta: Mapping[str, Any]) -> list[str]:
    cols = parent_meta.get("feature_columns") or parent_meta.get("features")
    if isinstance(cols, list) and cols:
        return [str(c) for c in cols]
    spec_cols = _as_dict(parent_meta.get("input_spec")).get("feature_columns")
    if isinstance(spec_cols, list) and spec_cols:
        return [str(c) for c in spec_cols]
    return []


def _d_feat(parent_meta: Mapping[str, Any]) -> int:
    cols = _feature_columns(parent_meta)
    if cols:
        return len(cols)
    shape = _as_dict(parent_meta.get("input_spec")).get("tensor_shape")
    if isinstance(shape, list) and shape and isinstance(shape[-1], int):
        return int(shape[-1])
    count = parent_meta.get("feature_count")
    return int(count) if isinstance(count, int) and count > 0 else 0


def _step_len(parent_meta: Mapping[str, Any]) -> int:
    shape = _as_dict(parent_meta.get("input_spec")).get("tensor_shape")
    if isinstance(shape, list) and len(shape) == 3 and isinstance(shape[1], int):
        if int(shape[1]) > 0:
            return int(shape[1])
    dl_params = _as_dict(parent_meta.get("dl_params"))
    value = dl_params.get("dl_step_len", dl_params.get("step_len"))
    if isinstance(value, (int, float)) and int(value) > 0:
        return int(value)
    return _DEFAULT_STEP_LEN


def build_dl_model_params(
    model_type: str, d_feat: int, dl_params: Mapping[str, Any]
) -> dict[str, Any]:
    """按训练端口径重建 Qlib DL 模型的构造参数（剔除非架构键）。"""

    def _dl(key: str, default: Any) -> Any:
        return dl_params.get(f"dl_{key}", dl_params.get(key, default))

    if model_type == "transformer":
        d_model = int(_dl("hidden_size", 64))
        nhead = max(1, d_model // 16)
        d_model = nhead * (d_model // nhead)
        if d_model < nhead:
            d_model = nhead * 2
        return {
            "d_feat": d_feat,
            "d_model": d_model,
            "nhead": nhead,
            "num_layers": int(_dl("num_layers", 2)),
            "dropout": float(_dl("dropout", 0.2)),
        }
    if model_type == "tabnet":
        return {
            "d_feat": d_feat,
            "out_dim": 1,
            "final_out_dim": 1,
            "n_d": int(_dl("hidden_size", 64)),
            "n_a": int(_dl("hidden_size", 64)),
            "n_steps": max(1, int(_dl("num_layers", 5))),
        }
    if model_type == "tcn":
        return {
            "d_feat": d_feat,
            "n_chans": int(_dl("hidden_size", 128)),
            "kernel_size": int(_dl("kernel_size", 5)),
            "num_layers": int(_dl("num_layers", 2)),
            "dropout": float(_dl("dropout", 0.2)),
        }
    # gru / lstm / alstm
    return {
        "d_feat": d_feat,
        "hidden_size": int(_dl("hidden_size", 64)),
        "num_layers": int(_dl("num_layers", 2)),
        "dropout": float(_dl("dropout", 0.2)),
    }


def build_nativetft_arch(dl_params: Mapping[str, Any]) -> dict[str, Any]:
    """按训练端 _train_nativetft 口径重建 NativeTFT 架构（hidden 需被 heads 整除）。"""
    hidden_dim = int(
        dl_params.get("dl_hidden_size", dl_params.get("hidden_size", 64)) or 64
    )
    num_heads = max(
        1, int(dl_params.get("dl_num_heads", dl_params.get("num_heads", 4)) or 4)
    )
    hidden_dim = num_heads * (hidden_dim // num_heads)
    if hidden_dim < num_heads:
        hidden_dim = num_heads * 2
    dropout = float(dl_params.get("dl_dropout", dl_params.get("dropout", 0.2)) or 0.2)
    return {"hidden_dim": hidden_dim, "num_heads": num_heads, "dropout": dropout}


def build_algorithm_metadata(
    model_type: str, parent_meta: Mapping[str, Any]
) -> tuple[dict[str, Any], set[str]]:
    """重建单算法子模型的算法相关元数据。

    返回 ``(要写入的字段, 要删除的父模型遗留字段)``；调用方先删除再更新。
    非 DL 算法（树模型 / sklearn）只返回框架名，架构字段全部剔除。

    数据源优先级：
    1. ``parent_meta.algorithm_metadata[model_type]``——训练端为每个基模型单独
       持久化的 dl_metadata（含 feat_norm），是最权威来源；
    2. 由父 metadata 的 dl_params + 特征列按训练端口径重建（老产物没有 1）。

    两者都拿不到 DL 上下文（父模型为主算法且非 DL 训练）时，只能按训练默认
    口径重建，此时写入 ``algorithm_meta_source=reconstructed_defaults`` 与告警，
    说明标准化统计量无法复原。
    """
    mtype = str(model_type or "").strip().lower()
    drops = set(ALGO_SCOPED_FIELDS)
    fields: dict[str, Any] = {"framework": ALGO_FRAMEWORKS.get(mtype, "unknown")}
    if mtype not in DL_TYPES:
        return fields, drops

    trained = _as_dict(_as_dict(parent_meta.get("algorithm_metadata")).get(mtype))
    trained_spec = _as_dict(trained.get("input_spec"))
    dl_params = _as_dict(parent_meta.get("dl_params")) or _as_dict(
        trained.get("dl_params")
    )
    d_feat = _d_feat(parent_meta) or _d_feat({"input_spec": trained_spec})
    feature_columns = _feature_columns(parent_meta) or _feature_columns(
        {"input_spec": trained_spec}
    )
    step_len = _step_len(parent_meta)
    trained_shape = trained_spec.get("tensor_shape")
    if (
        isinstance(trained_shape, list)
        and len(trained_shape) == 3
        and isinstance(trained_shape[1], int)
        and trained_shape[1] > 0
    ):
        step_len = int(trained_shape[1])
    is_seq = mtype in SEQUENCE_TYPES
    fields["dl_params"] = dict(dl_params)
    fields["is_sequence_model"] = is_seq
    fields["input_spec"] = {
        "tensor_shape": [None, step_len, d_feat] if is_seq else [None, d_feat],
        "feature_columns": feature_columns,
    }
    if mtype == "nativetft":
        # 训练端 nativetft 的 dl_metadata 不写 model_class_name（非 Qlib 类）
        arch = _as_dict(trained.get("model_arch")) or {
            "input_dim": d_feat,
            **build_nativetft_arch(dl_params),
        }
        fields["model_arch"] = arch
    else:
        fields["model_class_name"] = ALGO_CLASS_NAMES[mtype]
        fields["model_params"] = _as_dict(trained.get("model_params")) or (
            build_dl_model_params(mtype, d_feat, dl_params)
        )
    if is_seq:
        # 权威顺序：该算法训练时报的 feat_norm → 主模型的（各 TS 模型在同一
        # 训练集上统计，数值相同）
        norm = _as_dict(trained.get("feat_norm")) or _as_dict(
            parent_meta.get("feat_norm")
        )
        if norm.get("mean") and norm.get("std"):
            fields["feat_norm"] = norm

    has_dl_context = bool(
        trained
        or parent_meta.get("feat_norm")
        or parent_meta.get("input_spec")
        or parent_meta.get("dl_params")
    )
    if trained:
        fields["algorithm_meta_source"] = "trainer"
    elif has_dl_context:
        fields["algorithm_meta_source"] = "reconstructed"
    else:
        fields["algorithm_meta_source"] = "reconstructed_defaults"
        fields["algorithm_meta_warning"] = (
            "父训练的主算法不是深度学习模型，训练端未持久化该算法的 dl_params/"
            "feat_norm；架构与特征标准化按训练默认口径重建。若训练时自定义过 DL "
            "参数或序列窗口，推理结果不可靠——重新训练（新版本会持久化 "
            "algorithm_metadata）后再使用。"
        )
        logger.warning(
            "拆分子模型 %s：父 metadata 缺 DL 上下文，按默认口径重建架构", mtype
        )
    return fields, drops


def comparison_index(parent_meta: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """父 metadata.comparison（list）→ {model_type: 指标条目}。"""
    out: dict[str, dict[str, Any]] = {}
    comp = parent_meta.get("comparison")
    if not isinstance(comp, list):
        return out
    for entry in comp:
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("model_type") or "").strip().lower()
        if key:
            out[key] = entry
    return out


def _finite_or_none(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    fv = float(value)
    return fv if fv == fv and fv not in (float("inf"), float("-inf")) else None


def build_algorithm_metrics(
    model_type: str,
    parent_meta: Mapping[str, Any],
    comparison: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """从 comparison 提取该算法的指标，映射为单模型 metadata.metrics 口径。

    与训练端一致：score_direction 由 val_ic 符号决定（ic<0 → reversed）。
    取不到 comparison 条目时返回 None（调用方不应回填主模型指标）。
    """
    mtype = str(model_type or "").strip().lower()
    entry = (comparison or comparison_index(parent_meta)).get(mtype)
    if not isinstance(entry, dict):
        return None
    metrics: dict[str, Any] = {}
    for key in (
        "val_ic",
        "val_rank_ic",
        "val_rank_icir",
        "test_ic",
        "test_rank_ic",
        "test_rank_icir",
    ):
        value = _finite_or_none(entry.get(key))
        if value is not None:
            metrics[key] = value
    val_ic = _finite_or_none(entry.get("val_ic"))
    metrics["score_direction"] = (
        "normal" if val_ic is None or val_ic >= 0 else "reversed"
    )
    return metrics or None


def build_child_metrics_json(entry: Mapping[str, Any] | None) -> dict[str, Any]:
    """子模型 DB metrics_json：只写 comparison 里真实存在的字段。"""
    if not isinstance(entry, dict):
        return {}
    val: dict[str, Any] = {}
    for src_key, dst_key in (
        ("val_rmse", "rmse"),
        ("val_auc", "auc"),
        ("val_ic", "ic"),
    ):
        value = _finite_or_none(entry.get(src_key))
        if value is not None:
            val[dst_key] = value
    for src_key, dst_key in (
        ("val_rank_ic", "rank_ic"),
        ("val_rank_icir", "rank_icir"),
    ):
        value = _finite_or_none(entry.get(src_key))
        if value is not None:
            val[dst_key] = value
    test: dict[str, Any] = {}
    for src_key, dst_key in (
        ("test_ic", "ic"),
        ("test_rank_ic", "rank_ic"),
        ("test_rank_icir", "rank_icir"),
    ):
        value = _finite_or_none(entry.get(src_key))
        if value is not None:
            test[dst_key] = value
    out: dict[str, Any] = {}
    if val:
        out["val"] = val
    if test:
        out["test"] = test
    elapsed = _finite_or_none(entry.get("elapsed_seconds"))
    if elapsed is not None:
        out["elapsed_seconds"] = elapsed
    return out


def read_xgboost_best_iteration(model_path: Path) -> int | None:
    """从 xgboost 权重文件读出真实迭代轮数。

    优先用早停记录的 ``best_iteration``；未启用早停时该属性不存在，
    回退到文件内的总树数（``iteration_range=(0, N)`` 与不传等价，仅显式固定口径）。
    读不出返回 None，推理端按「全树」处理。
    """
    try:
        import warnings

        import xgboost as xgb

        booster = xgb.Booster()
        with warnings.catch_warnings():
            # .xgb 后缀的实际编码是 UBJSON，xgboost 会就此告警；与读取无关
            warnings.simplefilter("ignore", UserWarning)
            booster.load_model(str(model_path))
        best = getattr(booster, "best_iteration", None)
        if isinstance(best, int) and best > 0:
            return best
        rounds = int(booster.num_boosted_rounds())
        return rounds if rounds > 0 else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取 xgboost best_iteration 失败 %s: %s", model_path, exc)
        return None
