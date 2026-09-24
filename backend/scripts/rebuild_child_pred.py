#!/usr/bin/env python3
"""重建 stacking 子模型缺失的 pred.parquet / pred.pkl（CN，一次性修复工具）。

背景（2026-09-24 CN 全量审计）
------------------------------
`train_20260922012829_017570fe`（stacking：xgboost + catboost 基模型）拆分出的两个
子模型注册后 pred.parquet 缺失，DB/磁盘元数据都记 `pred_source='unavailable'`：
训练端 stacking 分支为省内存把基模型的全量预测 pop 掉（train.py:928-931，
`need_full_pred=False`），工作目录里根本没有 `pred_{type}.parquet`；拆分登记
`_find_per_algorithm_pred` 找不到它，`_materialize_child_predictions` 按设计
「不伪造、不复制父份」→ 子模型目录里没有自己的分数序列，评估中心一级证据
（pred.parquet）对这两个子模型永远缺席、多模型分数曲线画不出。

本工具**不重写特征/预处理/预测逻辑**，而是把训练端当时代码原样再跑一遍：
`load_data` → `_split_data` → `_prepare_arrays`(截面预处理) → `_predict_with_model`，
因此有两个硬性前提：① **必须在训练镜像容器里跑 build**（xgboost/catboost 版本与
训练一致，/app 布局 = 训练容器布局；脚本自检并拒绝其它容器）；② **验收判据 =
复算指标与存档逐位吻合**（元数据里存着训练当时的 val/test RankIC，xgboost
0.11716/0.078963、catboost 0.115636/0.080112；复算链路任一环错位——特征清单顺序、
切分、填充、方向——都会立刻显形），另有阳性对照（同日颠倒预测序，指标必须明显变）
证明判据有分辨力。

两段式使用（先 build 出 staging，人工复核后 install 才动正式产物与 DB）：

  # ① 训练镜像容器内（挂载集照抄编排器 local_docker_orchestrator.py，另加 models）
  docker run --rm --memory 56g \\
    -v <repo>/docker/training/data:/app/data:ro \\
    -v <repo>/docker/training/model_trainers:/app/model_trainers:ro \\
    -v <repo>/docker/training/diagnostics:/app/diagnostics:ro \\
    -v <repo>/docker/training/preprocessing.py:/app/preprocessing.py:ro \\
    -v <repo>/docker/training/parallel_utils.py:/app/parallel_utils.py:ro \\
    -v <repo>/backend:/app/backend:ro -v <repo>/models:/app/models:ro \\
    -v <repo>/data/quantdb:/tmp/quantdb_data:ro -v <staging>:/staging \\
    -e QUANTDB_DATA_DIR=/tmp/quantdb_data -e PYTHONPATH=/app \\
    ${TRAINING_IMAGE:-quantmind-oss-gpu:latest} \\
    python /app/backend/scripts/rebuild_child_pred.py build --staging /staging

  # ② quantmind 容器内（先干跑看判据，--apply 才写产物 + DB，含回读复验）
  docker exec -w /app quantmind python backend/scripts/rebuild_child_pred.py \\
      install --staging <staging 容器内路径> [--apply]

不写 `pred_{type}.parquet` 到工作目录：那是「训练当时的产物」的语义位，事后补写会
让将来的重拆分/审计分不清来源；子模型目录里的 pred.parquet 才是消费方读的东西。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# ── 白名单（唯一目标，不在名单里的 model_id 一律拒绝）───────────────────────
PARENT_RUN = "train_20260922012829_017570fe"
PARENT_MODEL_ID = f"mdl_cn_{PARENT_RUN}_8589f2d2"
TARGETS: dict[str, str] = {
    "xgboost": f"{PARENT_MODEL_ID}_xgboost_d27f302e",
    "catboost": f"{PARENT_MODEL_ID}_catboost_8f24cd5c",
}
STALE_PRED_SOURCE = "unavailable"
NEW_PRED_SOURCE = "rebuilt_full_model_20260924"

# 指标复算容差：链路逐行复刻时应为 0；数据面事后修订（因子面板重算）会带来微差
TOL_PASS = 1e-3
TOL_ABORT = 1e-2
# 阳性对照：颠倒同日预测序后的 RankIC 与存档值的差必须超过该量级，否则判据无分辨力
CONTROL_MIN_EFFECT = 0.05
MAX_NAN_RATIO = 1e-3
CHUNK_DAYS = 100

MODELS_ROOT_CANDIDATES = [Path("/app/models"), PROJECT_ROOT / "models"]
# build 只允许在训练镜像容器里跑：那里 /app/data 是训练数据包挂载点
TRAINING_APP_ROOT = Path("/app")


# ═══════════════════════════════════════════════════════════════════════
# 公共小工具
# ═══════════════════════════════════════════════════════════════════════


def _abort(msg: str) -> None:
    """任何一条守卫不过的出口：拒绝继续，绝不放行半份产物。"""
    print(f"**ABORT**：{msg}", file=sys.stderr)
    raise SystemExit(2)


def _md5(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _first_visible(candidates: list[Path], what: str) -> Path:
    for p in candidates:
        if p.is_dir():
            return p
    _abort(
        f"{what}根不可见（候选：{[str(c) for c in candidates]}）——"
        "路径判据落在错的命名空间里，不可据此判定产物缺失。"
    )


def _reroot(storage_path: str, models_root: Path) -> Path:
    """把 DB 里的 /app/models/... 路径重挂到当前可见的 models 根。"""
    for marker in ("/app/models/", f"{PROJECT_ROOT / 'models'}/"):
        if storage_path.startswith(marker):
            return models_root / storage_path[len(marker) :]
    return Path(storage_path)


def _resolve_model_dir(models_root: Path, model_id: str) -> Path:
    """在 models 根下按 model_id 目录名定位（storage_path 的层级与 DB 一致）。"""
    hits = sorted(models_root.glob(f"users/*/*/{model_id}"))
    if len(hits) != 1:
        _abort(
            f"model_id={model_id} 在 {models_root} 下命中 {len(hits)} 个目录，"
            "无法唯一确定（不做猜测）"
        )
    return hits[0]


def _ensure_training_imports() -> dict[str, Any]:
    """导入训练端模块；非训练镜像容器直接拒绝（版本面不保真）。"""
    if not (TRAINING_APP_ROOT / "data" / "loading.py").is_file():
        _abort(
            "不可见 /app/data/loading.py —— build 必须在**训练镜像**容器内运行"
            "（quantmind 运行镜像的 /app/data 是镜像内空目录，见命名空间陷阱）。"
        )
    for p in (str(TRAINING_APP_ROOT), str(PROJECT_ROOT)):
        if p not in sys.path:
            sys.path.insert(0, p)

    import xgboost
    import catboost

    from data.loading import load_data  # type: ignore
    from data.splits import _prepare_arrays, _split_data  # type: ignore
    from model_trainers.metrics import _compute_metrics, _rank_ic_series  # type: ignore
    from model_trainers.predict import _predict_with_model  # type: ignore

    return {
        "load_data": load_data,
        "_split_data": _split_data,
        "_prepare_arrays": _prepare_arrays,
        "_compute_metrics": _compute_metrics,
        "_rank_ic_series": _rank_ic_series,
        "_predict_with_model": _predict_with_model,
        "libs": (
            f"xgb={getattr(xgboost, '__version__', '?')} "
            f"cat={getattr(catboost, '__version__', '?')} "
            f"pd={pd.__version__} np={np.__version__}"
        ),
    }


def _rank_ic_mean(deps: dict[str, Any], frame: pd.DataFrame, pred_col: str) -> float:
    """按 metrics._rank_ic_series 的口径（逐日 rank 后的 Pearson）求均值。"""
    series = deps["_rank_ic_series"](frame, pred_col, "label")
    return float(np.mean(series)) if series else float("nan")


# ═══════════════════════════════════════════════════════════════════════
# build：训练端逐行复算 → staging
# ═══════════════════════════════════════════════════════════════════════


def _predict_chunked(
    deps: dict[str, Any],
    model: Any,
    frame: pd.DataFrame,
    model_type: str,
    features: list[str],
    fill,
) -> np.ndarray:
    """逐行等价的分块预测（复制自 docker/training/train.py:357-382，仅降内存峰值）。"""
    out = np.empty(len(frame), dtype=np.float64)
    dates = pd.Index(frame["trade_date"].unique())
    for i in range(0, len(dates), CHUNK_DAYS):
        mask = frame["trade_date"].isin(dates[i : i + CHUNK_DAYS]).to_numpy()
        if not mask.any():
            continue
        out[mask] = deps["_predict_with_model"](
            model, fill(frame.loc[mask]), model_type, features
        )
    return out


def _load_child_model(child_dir: Path, meta: dict, child_type: str) -> Any:
    model_file = str(meta.get("model_file") or "").strip()
    model_path = child_dir / model_file
    if not model_file or not model_path.is_file():
        _abort(f"模型权重不可见：{model_path}")
    if child_type == "xgboost":
        import xgboost as xgb

        model = xgb.Booster()
        model.load_model(str(model_path))
        return model
    from catboost import CatBoost

    model = CatBoost()
    model.load_model(str(model_path), format="cbm")
    return model


def _check_universe_against_parent(
    df: pd.DataFrame, parent_pred_path: Path
) -> dict[str, Any]:
    """行集守卫：父份 pred 的 (trade_date, symbol) 必须都在本次数据帧里。

    父份是同一 run 的头模型、读同一份因子里程碑；父份有而本次没有的行说明因子面板
    事后被修订过 —— 不写出带缺口的预测，直接停手。
    """
    parent = pd.read_parquet(parent_pred_path, columns=["trade_date", "symbol"])
    parent["trade_date"] = pd.to_datetime(parent["trade_date"])
    parent["symbol"] = parent["symbol"].astype(str)
    frame = pd.DataFrame(
        {
            "trade_date": pd.to_datetime(df["trade_date"]),
            "symbol": df["symbol"].astype(str),
        }
    )
    dup = int(frame.duplicated(["trade_date", "symbol"]).sum())
    if dup:
        _abort(f"本次数据帧有 {dup} 个重复的 (trade_date, symbol)")
    merged = parent.merge(
        frame, on=["trade_date", "symbol"], how="left", indicator=True
    )
    missing = int((merged["_merge"] == "left_only").sum())
    both = int((merged["_merge"] == "both").sum())
    stats = {
        "parent_rows": int(len(parent)),
        "matched_rows": both,
        "df_rows_not_in_parent": int(len(frame) - both),
        "parent_rows_missing_in_df": missing,
    }
    if missing:
        _abort(f"父份有 {missing} 行在本次数据帧里找不到 —— 特征面缺行，不写出半份预测")
    return stats


def _build_pred_frame(
    df: pd.DataFrame, pred: np.ndarray, val_df: pd.DataFrame, test_df: pd.DataFrame
) -> pd.DataFrame:
    """按 train.py:679-701（_train_single_model 的全量预测段）逐行复刻产物 frame。

    列序/列名与单模型路径 pred.parquet 一致
    ``[symbol, trade_date, label, label_return?, pred, split]`` —— 评估链
    （model_realized 一级证据、model_ic_monitor）消费的形状；缺 label/split 会被
    normalize_pred_frame 判为「无可评估列」。**父份三列旧格式不可照抄。**
    """
    extra = ["label_return"] if "label_return" in df.columns else []
    out = df[["symbol", "trade_date", "label", *extra]].copy()
    out["pred"] = pred
    dates = pd.to_datetime(out["trade_date"])
    v0, v1 = val_df["trade_date"].min(), val_df["trade_date"].max()
    t0, t1 = test_df["trade_date"].min(), test_df["trade_date"].max()
    out["split"] = np.select(
        [(dates >= v0) & (dates <= v1), (dates >= t0) & (dates <= t1)],
        ["valid", "test"],
        default="train",
    )
    return out.reset_index(drop=True)


def build_one(
    child_type: str,
    model_id: str,
    models_root: Path,
    staging_root: Path,
    deps: dict[str, Any],
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "child_type": child_type,
        "model_id": model_id,
        "run_id": PARENT_RUN,
        "verdict": "PENDING",
        "guards": [],
        "warnings": [],
    }

    def guard(ok: bool, why: str) -> None:
        report["guards"].append({"ok": bool(ok), "why": why})
        if not ok:
            print(f"**ABORT** [{model_id}] 守卫未过：{why}", file=sys.stderr)
            raise SystemExit(2)

    child_dir = _resolve_model_dir(models_root, model_id)
    parent_dir = _resolve_model_dir(models_root, PARENT_MODEL_ID)
    report["child_dir"] = str(child_dir)
    meta = json.loads((child_dir / "metadata.json").read_text(encoding="utf-8"))
    report["stored_metrics"] = dict(meta.get("metrics") or {})

    guard(
        str(meta.get("model_type")) == child_type,
        f"metadata.model_type={meta.get('model_type')!r} == {child_type!r}",
    )
    guard(
        str(meta.get("data_source")) == "quantdb_factors",
        f"data_source={meta.get('data_source')!r}",
    )
    guard(
        str(meta.get("factor_source")) == "l1_l2_factors",
        f"factor_source={meta.get('factor_source')!r}",
    )
    guard(
        str((meta.get("context") or {}).get("market") or "CN").upper() == "CN",
        "market == CN（本工具只做 CN）",
    )
    guard(
        str(meta.get("pred_source") or "") == STALE_PRED_SOURCE,
        f"pred_source={meta.get('pred_source')!r} == {STALE_PRED_SOURCE!r}"
        "（已材料化的子模型不重建）",
    )
    guard(
        meta.get("pred_rows") is None,
        f"pred_rows={meta.get('pred_rows')!r} is None（有行数说明已材料化过）",
    )
    for name in ("pred.parquet", "pred.pkl"):
        guard(not (child_dir / name).exists(), f"{name} 目前不存在（不覆盖既有产物）")
    cfg_path = child_dir / "config.yaml"
    guard(cfg_path.is_file(), "config.yaml 可见")

    import yaml

    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    guard(str(cfg.get("run_id")) == PARENT_RUN, f"config.run_id == {PARENT_RUN}")
    data_cfg = cfg.get("data") or {}
    context_cfg = cfg.get("context") or {}
    # 与 train.py:1202-1212 逐行同款的特征清单推导（factor_source 分支：不做基础列补齐）
    guard(str(data_cfg.get("factor_source") or "").strip(), "data.factor_source 非空")
    guard(
        not str((cfg.get("factor_selection") or {}).get("method") or "").strip(),
        "factor_selection.method 为空（跑过筛选则训练特征面 ≠ 提交清单，本工具拒绝）",
    )
    guard(
        not bool(context_cfg.get("industry_as_feature", False)),
        "industry_as_feature 关闭（开启会向特征表追加 ind_code_l1，本工具未覆盖）",
    )
    submitted = list(
        dict.fromkeys(
            [str(x).strip() for x in (data_cfg.get("features") or []) if str(x).strip()]
        )
    )
    features = [str(f) for f in (meta.get("features") or [])]
    guard(bool(features), f"features 非空（{len(features)} 列）")
    guard(
        submitted == features,
        f"config.data.features 与 metadata.features 逐位同序（{len(submitted)} 列）",
    )
    guard(not (meta.get("auto_appended_features") or []), "无自动补齐特征")
    report["feature_count"] = len(features)

    # ── 训练端逐行复算（参数逐项对齐 train.py:1213-1236 的调用点）────────
    df, valid_features = deps["load_data"](
        data_cfg["train_start"],
        data_cfg["train_end"],
        features,
        target_horizon_days=int(
            (cfg.get("label") or {}).get("target_horizon_days") or 1
        ),
        target_mode=str((cfg.get("label") or {}).get("target_mode") or "return"),
        cache_dir=cfg.get("cache", {}).get("dir"),
        valid_end=cfg.get("split", {}).get("valid", [None, None])[1],
        test_end=cfg.get("split", {}).get("test", [None, None])[1],
        source_mode=str(data_cfg.get("source_mode") or "LOCAL").strip().upper(),
        local_dir=str(data_cfg.get("local_dir") or "").strip() or None,
        market=str(context_cfg.get("market", "CN")).upper(),
        industry_as_feature=False,
        factor_source=str(data_cfg.get("factor_source") or "").strip() or None,
        quantdb_dir=str(data_cfg.get("quantdb_dir") or "").strip() or None,
        factor_field_sources=data_cfg.get("factor_field_sources") or None,
        pool_symbols=data_cfg.get("pool_symbols") or None,
    )
    report["df_rows"] = int(len(df))
    # 特征清单必须与训练当时逐位同序：xgboost inplace_predict 按列位取值，
    # 顺序错位不会报错、只会静默算错
    if list(valid_features) != features:
        _abort(
            f"[{model_id}] load_data 返回的特征清单与元数据不一致："
            f"元数据 {len(features)} 列 / 本次 {len(valid_features)} 列；"
            f"增={sorted(set(valid_features) - set(features))[:5]} "
            f"缺={sorted(set(features) - set(valid_features))[:5]}"
        )

    train_df, val_df, test_df = deps["_split_data"](df, cfg)
    fill_values, x_tr, y_tr, x_val, y_val, fill = deps["_prepare_arrays"](
        train_df,
        val_df,
        features,
        prep_cfg=cfg.get("preprocessing") or {},
        extra_frames=[test_df, df],  # 与训练同款：test 帧与全窗帧在调用内原地预处理
    )
    del x_tr, y_tr, x_val, y_val
    report["split_rows"] = {
        "train": int(len(train_df)),
        "valid": int(len(val_df)),
        "test": int(len(test_df)),
    }

    # 填充值指纹：训练当时把 93 个 fill_values 写进了元数据，逐列比对即可证明
    # 「截面预处理 + train 中位数」这段与当时逐位一致（信息项，硬闸门仍是 rank_ic 复算）
    fv_stored = meta.get("fill_values") or {}
    if fv_stored:
        checked = [c for c in features if c in fv_stored]
        mism = [
            c
            for c in checked
            if not np.isclose(
                float(fill_values.get(c, float("nan"))),
                float(fv_stored[c]),
                rtol=1e-5,
                atol=1e-8,
            )
        ]
        report["fill_values_check"] = {
            "checked": len(checked),
            "mismatched": len(mism),
            "sample": mism[:5],
        }
        if mism:
            report["warnings"].append(
                f"fill_values 有 {len(mism)}/{len(checked)} 列与训练存档不一致（样本：{mism[:5]}），"
                "疑似因子面板事后修订"
            )

    model = _load_child_model(child_dir, meta, child_type)

    # ── 验收①：训练当时的 val/test 指标必须能逐位复算出来 ──────────────
    # 与 train.py:670-673 同款：分块预测（分块仅浮点 ulp 级差，逐行等价）
    y_val_pred = _predict_chunked(deps, model, val_df, child_type, features, fill)
    val_m = deps["_compute_metrics"](
        val_df, val_df["label"].astype("float32").to_numpy(), y_val_pred
    )
    y_test_pred = _predict_chunked(deps, model, test_df, child_type, features, fill)
    test_m = deps["_compute_metrics"](
        test_df, test_df["label"].astype("float32").to_numpy(), y_test_pred
    )
    stored = report["stored_metrics"]
    for split in ("val", "test"):
        guard(
            stored.get(f"{split}_rank_ic") is not None,
            f"训练存档含 {split}_rank_ic（验收锚点存在）",
        )
    metric_rows = []
    worst = 0.0
    for split, m in (("val", val_m), ("test", test_m)):
        for key in ("rank_ic", "ic", "rank_icir"):
            got, want = float(m.get(key, float("nan"))), stored.get(f"{split}_{key}")
            row = {"metric": f"{split}_{key}", "recomputed": got, "stored": None}
            if want is None:
                row["stored_key_absent"] = True
            else:
                delta = abs(got - float(want)) if np.isfinite(got) else float("inf")
                row["stored"] = float(want)
                row["abs_delta"] = None if not np.isfinite(delta) else delta
                if key == "rank_ic":
                    worst = max(worst, delta)
            metric_rows.append(row)
    report["metric_reproduction"] = metric_rows
    report["worst_rank_ic_delta"] = None if not np.isfinite(worst) else worst
    if not np.isfinite(worst) or worst > TOL_ABORT:
        _abort(f"[{model_id}] 复算指标与存档偏差 {worst} > {TOL_ABORT}，链路未复刻成功")
    if worst > TOL_PASS:
        report["warnings"].append(
            f"rank_ic 复算偏差 {worst:.6f} 在 ({TOL_PASS}, {TOL_ABORT}]，"
            "疑似因子面板事后修订，请人工复核后再 install"
        )

    # ── 阳性对照：同日内颠倒预测序，指标必须明显变化（判据有分辨力）────
    ctrl = val_df[["trade_date", "label"]].copy()
    ctrl["_pred"] = -y_val_pred  # 日序反转后每日 rank 相关 ≈ -原值
    ctrl_rank_ic = _rank_ic_mean(deps, ctrl, "_pred")
    ctrl_effect = abs(ctrl_rank_ic - float(stored.get("val_rank_ic")))
    report["discrimination_control"] = {
        "reversed_rank_ic": ctrl_rank_ic,
        "abs_effect": ctrl_effect,
        "min_required": CONTROL_MIN_EFFECT,
    }
    if not np.isfinite(ctrl_effect) or ctrl_effect < CONTROL_MIN_EFFECT:
        _abort(f"[{model_id}] 阳性对照未通过（effect={ctrl_effect}），验收判据无分辨力")
    del ctrl, y_val_pred, y_test_pred

    # ── 全窗预测 + 方向纠正（同 train.py 非 stacking 保存路径）──────────
    pred = _predict_chunked(deps, model, df, child_type, features, fill)
    direction = str(stored.get("score_direction") or "normal")
    if direction == "reversed":
        pred = -pred
    report["score_direction"] = direction
    nan_ratio = float(np.mean(~np.isfinite(pred)))
    report["pred_nan_ratio"] = nan_ratio
    if nan_ratio > MAX_NAN_RATIO:
        _abort(f"[{model_id}] 全窗预测 NaN 占比 {nan_ratio:.4%} > {MAX_NAN_RATIO:.1%}")

    report["row_universe"] = _check_universe_against_parent(
        df, parent_dir / "pred.parquet"
    )
    out_frame = _build_pred_frame(df, pred, val_df, test_df)
    del pred

    # ── 写 staging + 回读复验（独立路径：从文件重算 val/test 段指标）─────
    out_dir = staging_root / PARENT_RUN / model_id
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "pred.parquet"
    out_frame.to_parquet(out_path, engine="pyarrow", compression="zstd", index=False)
    report["artifacts"] = {
        "pred.parquet": {
            "md5": _md5(out_path),
            "rows": int(len(out_frame)),
            "columns": list(out_frame.columns),
        }
    }

    # 回读：直接从文件自身的 label/split 重算（同时验证标签与切分标注落盘正确）
    back = pd.read_parquet(out_path)
    file_check = {}
    for split in ("valid", "test"):
        part = back[back["split"] == split].copy()
        part["_pred"] = part["pred"]
        got = _rank_ic_mean(deps, part, "_pred")
        stored_key = "val_rank_ic" if split == "valid" else "test_rank_ic"
        want = float(stored.get(stored_key))
        delta = abs(got - want) if np.isfinite(got) else float("inf")
        file_check[f"{split}_rank_ic"] = {
            "from_file": got,
            "stored": want,
            "abs_delta": delta,
        }
        if not np.isfinite(delta) or delta > TOL_PASS:
            _abort(f"[{model_id}] 回读复验 {split} RankIC 偏差 {delta} > {TOL_PASS}")
    report["file_readback_check"] = file_check
    report["coverage"] = {
        "start": str(back["trade_date"].min().date()),
        "end": str(back["trade_date"].max().date()),
        "rows": int(len(back)),
    }

    report["verdict"] = "PASS" if not report["warnings"] else "WARN"
    report["env"] = {"training_root": str(TRAINING_APP_ROOT), "libs": deps["libs"]}
    (out_dir / "build_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(
        f"[{report['verdict']}] {model_id} → {out_path} "
        f"(worst rank_ic delta={report['worst_rank_ic_delta']})"
    )
    return report


def cmd_build(args: argparse.Namespace) -> int:
    deps = _ensure_training_imports()
    models_root = _first_visible(MODELS_ROOT_CANDIDATES, "models")
    staging_root = Path(args.staging).resolve()
    staging_root.mkdir(parents=True, exist_ok=True)
    print(f"BUILD · root={models_root} · staging={staging_root} · libs={deps['libs']}")
    types = list(TARGETS) if args.child == "all" else [args.child]
    for child_type in types:
        build_one(child_type, TARGETS[child_type], models_root, staging_root, deps)
    print(f"完成：{len(types)} 个子模型（任一失败即整体 abort，无「部分成功」）")
    return 0


# ═══════════════════════════════════════════════════════════════════════
# install：写正式产物 + DB（默认干跑，--apply 才落盘）
# ═══════════════════════════════════════════════════════════════════════


def _get_engine():
    from sqlalchemy import create_engine

    db_url = os.getenv(
        "DATABASE_URL",
        f"postgresql://{os.getenv('DB_USER', 'quantmind')}:{os.getenv('DB_PASSWORD', 'quantmind2026')}"
        f"@{os.getenv('DB_HOST', 'db')}:{os.getenv('DB_PORT', '5432')}/{os.getenv('DB_NAME', 'quantmind')}",
    )
    if "+asyncpg" in db_url:
        db_url = db_url.replace("+asyncpg", "+psycopg2")
    return create_engine(db_url, pool_pre_ping=True, future=True)


def _write_pred_pkl(child_dir: Path) -> None:
    """与 model_registry._materialize_child_predictions 逐行同款地生成 pred.pkl。"""
    frame = pd.read_parquet(
        child_dir / "pred.parquet", columns=["trade_date", "symbol", "pred"]
    )
    pred_qlib = (
        frame.rename(
            columns={"trade_date": "datetime", "symbol": "instrument", "pred": "score"}
        )
        .assign(datetime=lambda d: pd.to_datetime(d["datetime"]))
        .set_index(["datetime", "instrument"])
        .sort_index()
    )
    pred_qlib.to_pickle(child_dir / "pred.pkl")


def _coverage_fields(pred_parquet: Path) -> dict[str, Any]:
    dates = pd.to_datetime(
        pd.read_parquet(pred_parquet, columns=["trade_date"])["trade_date"]
    )
    return {
        "pred_rows": int(len(dates)),
        "pred_coverage_start": str(dates.min().date()) if len(dates) else None,
        "pred_coverage_end": str(dates.max().date()) if len(dates) else None,
    }


def install_one(
    model_id: str,
    models_root: Path,
    staging_root: Path,
    engine: Any,
    apply: bool,
) -> dict[str, Any]:
    from sqlalchemy import text

    # child_type 不单列：model_id 后缀（_xgboost_/_catboost_）已唯一标识
    result: dict[str, Any] = {"model_id": model_id, "verdict": "PENDING"}
    out_dir = staging_root / PARENT_RUN / model_id
    staged_parquet = out_dir / "pred.parquet"
    report_path = out_dir / "build_report.json"
    if not staged_parquet.is_file() or not report_path.is_file():
        result["verdict"] = "skip:staging 缺产物"
        return result
    report = json.loads(report_path.read_text(encoding="utf-8"))
    result["build_verdict"] = report.get("verdict")

    def fail(why: str) -> dict[str, Any]:
        result["verdict"] = f"error:{why}"
        return result

    if report.get("verdict") not in ("PASS", "WARN"):
        return fail(f"build verdict={report.get('verdict')}")
    want_md5 = (report.get("artifacts") or {}).get("pred.parquet", {}).get("md5")
    if not want_md5 or _md5(staged_parquet) != want_md5:
        return fail("staged pred.parquet md5 与报告不符（产物被改动过）")

    child_dir = _resolve_model_dir(models_root, model_id)
    result["child_dir"] = str(child_dir)
    row = engine.execute(
        text("SELECT metadata_json FROM qm_user_models WHERE model_id = :mid"),
        {"mid": model_id},
    ).fetchone()
    if row is None:
        return fail("DB 无此 model_id")
    meta_db = row[0] if isinstance(row[0], dict) else json.loads(row[0] or "{}")
    if str(meta_db.get("pred_source") or "") != STALE_PRED_SOURCE:
        return fail(
            f"DB pred_source={meta_db.get('pred_source')!r} != {STALE_PRED_SOURCE!r}（可能已装过）"
        )

    target_parquet = child_dir / "pred.parquet"
    if target_parquet.is_file():
        if _md5(target_parquet) == want_md5:
            result["verdict"] = "already-installed（内容一致，跳过）"
        else:
            return fail("子模型目录已有内容不同的 pred.parquet，停手")
        if not (child_dir / "pred.pkl").is_file():
            return fail("pred.parquet 已装但 pred.pkl 缺失，需人工处理")
        return result
    if (child_dir / "pred.pkl").is_file():
        return fail("pred.pkl 已存在但 pred.parquet 缺失，停手")

    cov = _coverage_fields(staged_parquet)
    result["coverage"] = cov
    if not apply:
        result["verdict"] = "will-install（干跑）"
        return result

    # ── 落盘：先写文件（半成品可清理），再 CAS 写 DB，最后回读复验 ──────
    copied = False
    try:
        shutil.copy2(staged_parquet, target_parquet)
        copied = True
        _write_pred_pkl(child_dir)
    except Exception as exc:  # noqa: BLE001
        if copied:
            target_parquet.unlink(missing_ok=True)
            (child_dir / "pred.pkl").unlink(missing_ok=True)
        return fail(f"写产物失败并已回滚: {exc}")

    engine.execute(
        text(
            """
            UPDATE qm_user_models
               SET metadata_json = jsonb_set(jsonb_set(jsonb_set(jsonb_set(
                       metadata_json,
                       '{pred_source}', to_jsonb(cast(:src AS text))),
                       '{pred_rows}', to_jsonb(cast(:rows AS bigint))),
                       '{pred_coverage_start}', to_jsonb(cast(:cs AS text))),
                       '{pred_coverage_end}', to_jsonb(cast(:ce AS text)))
             WHERE model_id = :mid
               AND metadata_json->>'pred_source' = :stale
            """
        ),
        {
            "mid": model_id,
            "src": NEW_PRED_SOURCE,
            "rows": cov["pred_rows"],
            "cs": cov["pred_coverage_start"],
            "ce": cov["pred_coverage_end"],
            "stale": STALE_PRED_SOURCE,
        },
    )

    # 磁盘 metadata.json 同步（消费方读盘，DB 与盘不该分叉）
    meta_path = child_dir / "metadata.json"
    disk_meta = json.loads(meta_path.read_text(encoding="utf-8"))
    disk_meta.update({"pred_source": NEW_PRED_SOURCE, **cov})
    tmp = meta_path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(disk_meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    tmp.replace(meta_path)

    # ── 回读复验（DB 字段、文件 md5/行数、pkl 结构）─────────────────────
    back = engine.execute(
        text(
            "SELECT metadata_json->>'pred_source', metadata_json->>'pred_rows' "
            "FROM qm_user_models WHERE model_id = :mid"
        ),
        {"mid": model_id},
    ).fetchone()
    ok_db = (
        bool(back)
        and back[0] == NEW_PRED_SOURCE
        and str(back[1]) == str(cov["pred_rows"])
    )
    ok_file = _md5(target_parquet) == want_md5
    pkl = pd.read_pickle(child_dir / "pred.pkl")
    ok_pkl = list(getattr(pkl.index, "names", [])) == [
        "datetime",
        "instrument",
    ] and getattr(pkl, "shape", None) == (cov["pred_rows"], 1)
    result["readback"] = {"db": ok_db, "file_md5": ok_file, "pkl": ok_pkl}
    result["verdict"] = (
        "installed" if (ok_db and ok_file and ok_pkl) else "error:回读复验失败"
    )
    return result


def cmd_install(args: argparse.Namespace) -> int:
    models_root = _first_visible(MODELS_ROOT_CANDIDATES, "models")
    staging_root = Path(args.staging).resolve()
    engine = _get_engine()
    types = list(TARGETS) if args.child == "all" else [args.child]
    tag = "APPLY" if args.apply else "DRY-RUN"
    print(f"{tag} · root={models_root} · staging={staging_root}")
    failures = 0
    with engine.begin() as conn:
        for child_type in types:
            res = install_one(
                TARGETS[child_type], models_root, staging_root, conn, args.apply
            )
            detail = {k: v for k, v in res.items() if k not in ("model_id", "verdict")}
            print(f"  [{res['verdict']}] {res['model_id']} {json.dumps(detail)}")
            if str(res["verdict"]).startswith("error"):
                failures += 1
    print(f"完成：{len(types)} 个子模型，{failures} 处异常")
    return 1 if failures else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="训练镜像容器内复算，写 staging")
    b.add_argument("--staging", required=True, help="staging 根目录（容器内路径）")
    b.add_argument("--child", choices=[*TARGETS, "all"], default="all")
    b.set_defaults(func=cmd_build)
    i = sub.add_parser("install", help="quantmind 容器内写正式产物 + DB（默认干跑）")
    i.add_argument("--staging", required=True)
    i.add_argument("--child", choices=[*TARGETS, "all"], default="all")
    i.add_argument("--apply", action="store_true", help="真正写入（缺省只干跑）")
    i.set_defaults(func=cmd_install)
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
