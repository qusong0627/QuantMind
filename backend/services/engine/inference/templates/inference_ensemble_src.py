#!/usr/bin/env python3
"""
QuantMind 融合模型推理脚本 (inference_ensemble_src.py)
=======================================================
用户从模型管理页多选已有模型创建融合模型后，该脚本负责对融合模型执行推理：
读取模型目录中的 ensemble_config.json，逐一加载源模型 → 预测 → 截面百分位
归一化 → 加权融合 → 共识度统计 → 输出标准信号 JSON。

支持的源模型类型：
  - LightGBM (.lgb / .txt)
  - XGBoost (.xgb)
  - CatBoost (.cbm)
  - sklearn (.pkl)
  - Stacking 融合 (is_ensemble + ensemble_method=stacking，加载基模型 + meta_model)
  - PyTorch DL 时序/扁平模型 (.pth / .pt)：委派成员目录 inference.py 推理，
    窗口按 trade_date 截断，与单模型推理口径一致（同步 parquet 模板）

平台注入环境变量：
    MODEL_DIR      融合模型目录绝对路径（含 ensemble_config.json + metadata.json）
    TRADE_DATE     推理日期（同 --date 参数，互为备份）
    OUTPUT_FORMAT  固定值 json
    MODEL_TRAINING_DATA_DIR  特征 parquet 数据目录

调用方式（由 InferenceScriptRunner 自动调用）：
    python inference.py --date YYYY-MM-DD --output /path/to/out.json

输出格式（写入 --output 文件）：
    [{"symbol": "SH600519", "score": 0.15, "consensus": 3, "horizons": 3, "detail": {...}}, ...]

exit code：
    0  = 成功
    1  = 致命错误（模型/配置损坏）
    2  = 该日期无可用数据（触发 alpha158 兜底）
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import pickle
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd

try:
    import lightgbm as lgb
except ImportError:
    lgb = None

try:
    import xgboost as xgb
except ImportError:
    xgb = None

try:
    from catboost import CatBoost, Pool
except ImportError:
    CatBoost = None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("inference_ensemble_src")

_DEFAULT_DATA_DIR = "/app/db/feature_snapshots"


# ═══════════════════════════════════════════════════════════════════════════
# 1. 配置加载
# ═══════════════════════════════════════════════════════════════════════════

def load_ensemble_config(model_dir: Path) -> dict:
    """从融合模型目录读取 ensemble_config.json。"""
    config_path = model_dir / "ensemble_config.json"
    if not config_path.exists():
        raise FileNotFoundError(f"ensemble_config.json 不存在: {config_path}")
    with open(config_path, encoding="utf-8") as f:
        config = json.load(f)
    if not isinstance(config, dict) or not config.get("models"):
        raise ValueError(f"ensemble_config.json 格式无效或 models 为空: {config_path}")
    return config


# ═══════════════════════════════════════════════════════════════════════════
# 2. 数据加载
# ═══════════════════════════════════════════════════════════════════════════

def _resolve_parquet_path(data_dir: Path, trade_date: str, market: str = "CN") -> Path | None:
    """解析年度/市场 parquet 文件路径。

    market 非 CN 时优先取各市场汇总文件（model_features_{market}.parquet），
    CN 回退到按年拆分文件。
    """
    _MARKET_PARQUET = {
        "HK": "model_features_hk.parquet",
        "US": "model_features_us.parquet",
        "CRYPTO": "model_features_crypto.parquet",
        "FUTURES": "model_features_futures.parquet",
    }
    market_upper = str(market or "CN").upper()
    if market_upper in _MARKET_PARQUET:
        p = data_dir / _MARKET_PARQUET[market_upper]
        if p.exists():
            return p

    year = int(trade_date[:4])
    p = data_dir / f"model_features_{year}.parquet"
    if p.exists():
        return p
    p = data_dir / "model_features.parquet"
    return p if p.exists() else None


def load_day_data(trade_date: str, data_dir: Path, market: str = "CN") -> pd.DataFrame | None:
    """加载指定交易日的全市场特征数据。"""
    parquet_path = _resolve_parquet_path(data_dir, trade_date, market=market)
    if parquet_path is None:
        logger.warning("找不到 parquet 文件 (data_dir=%s)", data_dir)
        return None

    df = pd.read_parquet(parquet_path, engine="pyarrow")
    if "symbol" not in df.columns and "instrument" in df.columns:
        df = df.rename(columns={"instrument": "symbol"})
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d")
    day_df = df[df["trade_date"] == trade_date].copy()

    if len(day_df) == 0:
        logger.warning("日期 %s 无数据", trade_date)
        return None

    # 过滤不可交易：价格/成交量为零或负
    if "close" in day_df.columns:
        day_df = day_df[pd.to_numeric(day_df["close"], errors="coerce") > 0]
    if "volume" in day_df.columns:
        day_df = day_df[pd.to_numeric(day_df["volume"], errors="coerce") > 0]

    if len(day_df) == 0:
        logger.warning("日期 %s 过滤后无可交易数据", trade_date)
        return None

    return day_df


# ═══════════════════════════════════════════════════════════════════════════
# 3. 源模型加载（支持单模型 + stacking 融合）
# ═══════════════════════════════════════════════════════════════════════════

def _load_base_model(model_path: Path, model_type: str):
    """按类型加载单个基模型权重。"""
    suffix = model_path.suffix.lower()
    if model_type == "lightgbm" or suffix in (".lgb", ".txt"):
        if lgb is None:
            raise ImportError("lightgbm 未安装")
        return lgb.Booster(model_file=str(model_path))
    if model_type == "xgboost" or suffix == ".xgb":
        if xgb is None:
            raise ImportError("xgboost 未安装")
        booster = xgb.Booster()
        booster.load_model(str(model_path))
        return booster
    if model_type == "catboost" or suffix == ".cbm":
        if CatBoost is None:
            raise ImportError("catboost 未安装")
        cb = CatBoost()
        cb.load_model(str(model_path), format="cbm")
        return cb
    if suffix in (".pth", ".pt"):
        # PyTorch 模型：不在此进程内加载（架构重建复杂且易版本漂移），
        # 返回标记 dict，由 main() 委派成员目录 inference.py 推理。
        return {"__dl_member__": True, "path": str(model_path)}
    # .pkl → sklearn / pickle 模型
    with open(model_path, "rb") as f:
        return pickle.load(f)


def load_source_model(model_dir: Path) -> tuple[object, dict]:
    """加载源模型。返回 (model, meta)。

    对 stacking 融合源模型，加载全部基模型 + meta_model，包装为 StackingEnsemble。
    """
    meta_path = model_dir / "metadata.json"
    if not meta_path.exists():
        raise FileNotFoundError(f"metadata.json 不存在: {meta_path}")
    with open(meta_path, encoding="utf-8") as f:
        meta = json.load(f)

    is_stacking = bool(meta.get("is_ensemble")) and str(meta.get("ensemble_method", "")).lower() == "stacking"
    if is_stacking:
        model = _load_stacking(model_dir, meta)
        return model, meta

    model_file = meta.get("model_file", "")
    model_path = model_dir / model_file if model_file else None
    if not model_path or not model_path.exists():
        for ext in ("*.xgb", "*.lgb", "*.cbm", "*.pkl", "*.txt", "*.pth", "*.pt"):
            candidates = list(model_dir.glob(ext))
            if candidates:
                model_path = candidates[0]
                break
    if not model_path or not model_path.exists():
        raise FileNotFoundError(f"未找到模型文件: {model_dir}")

    model_type = str(meta.get("model_type", "")).lower()
    return _load_base_model(model_path, model_type), meta


def _sync_member_script(member_dir: Path) -> Path:
    """同步成员推理脚本到最新 parquet 模板（仅平台托管脚本）。

    runner 只在成员模型作为主模型推理时才同步它的 inference.py；
    融合委派子进程前必须自己同步，否则成员用旧模板（如窗口截断缺失）。
    """
    script = member_dir / "inference.py"
    tpl = Path("/app/backend/services/engine/inference/templates/inference_parquet.py")
    if not script.exists() or not tpl.is_file():
        return script
    try:
        text = script.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return script
    marker = "QuantMind Parquet 数据源推理脚本 (inference.py 模板)"
    if marker in text and marker not in tpl.read_text(encoding="utf-8", errors="ignore"):
        # 注意：本文件自身的注释里也含 marker 字符串，runner 侧按
        # marker 识别托管脚本时会误把融合脚本当 parquet 模板覆写。
        # 这里只同步真正的 parquet 托管脚本（模板自身包含 marker），
        # 避免把成员融合脚本（ensemble_src 模板）错误覆写成 parquet 模板。
        try:
            script.write_text(tpl.read_text(encoding="utf-8"), encoding="utf-8")
            logger.info("已同步成员推理脚本到最新模板: %s", script)
        except OSError as e:
            logger.warning("成员脚本同步失败，沿用现有脚本: %s", e)
    return script


def _predict_dl_source_model(member_dir: Path, trade_date: str, data_dir: Path,
                             out_root: Path, market: str = "CN") -> dict[str, float]:
    """DL (.pth/.pt) 源模型：委派其成员目录 inference.py 推理。

    成员脚本先同步到最新 parquet 模板（窗口按 trade_date 截断，
    防止未来数据泄漏）。输出临时 JSON，下次运行覆盖。
    """
    script = _sync_member_script(member_dir)
    if not script.exists():
        raise FileNotFoundError(f"源模型推理脚本不存在: {script}")
    out_root.mkdir(parents=True, exist_ok=True)
    out_path = out_root / f"member_{member_dir.name}.json"
    env = dict(os.environ)
    # 必须强制覆盖：runner 启动融合脚本时 MODEL_DIR 指向融合模型目录，
    # 成员脚本会把它当成自己的模型目录读错 metadata。
    env["MODEL_DIR"] = str(member_dir)
    env["TRADE_DATE"] = trade_date
    cmd = [sys.executable, str(script), "--date", trade_date,
           "--model-dir", str(member_dir),
           "--data-dir", str(data_dir), "--output", str(out_path)]
    # 与 runner 主脚本口径一致：非 CN 市场显式透传，否则成员按默认市场取数
    if str(market or "CN").upper() not in ("", "CN", "A"):
        cmd += ["--market", str(market).upper()]
    proc = subprocess.run(
        cmd,
        capture_output=True, text=True, timeout=1800, env=env,
        # 必须显式 utf-8：中文 Windows 默认按 ANSI(GBK) 解码子进程输出，
        # 而子进程（PYTHONIOENCODING=utf-8）写的是 UTF-8，一旦日志含中文
        # 就 UnicodeDecodeError，成员全部失败 → 融合脚本 exit 1
        # （前端「脚本返回非零退出码」）。
        encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-3:]
        raise RuntimeError(f"exit={proc.returncode}: {' | '.join(tail)}")
    with open(out_path, encoding="utf-8") as fh:
        rows = json.load(fh)
    scores: dict[str, float] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        sym = str(row.get("symbol") or row.get("instrument") or "")
        val = row.get("score") if row.get("score") is not None else row.get("fusion_score")
        if not sym or val is None:
            continue
        try:
            f = float(val)
        except (TypeError, ValueError):
            continue
        if f == f:  # 过滤 NaN
            scores[sym] = f
    if not scores:
        raise RuntimeError("成员推理输出为空")
    return scores


class _StackingEnsemble:
    """Stacking 源模型推理包装：基模型预测 → 元学习器融合。"""

    def __init__(self, base_models: dict, meta_model, model_types: list[str],
                 fill_values: dict, features: list[str]):
        self.base_models = base_models
        self.meta_model = meta_model
        self.model_types = model_types
        self.fill_values = fill_values
        self.features = features

    def predict(self, X: np.ndarray) -> np.ndarray:
        base_preds = []
        for mt in self.model_types:
            model = self.base_models.get(mt)
            if model is None:
                continue
            # 基模型训练时使用与融合模型相同的特征矩阵（stacking 用统一特征），
            # 这里直接对已填充的 X 预测
            if mt == "lightgbm":
                pred = model.predict(X)
            elif mt == "xgboost":
                # XGBoost Booster 需要特征名匹配训练时的顺序
                names = model.feature_names
                dmat = xgb.DMatrix(X, feature_names=names) if names else xgb.DMatrix(X)
                pred = model.predict(dmat)
            elif mt == "catboost":
                pred = np.asarray(model.predict(Pool(X))).flatten()
            else:
                pred = model.predict(X).flatten()
            base_preds.append(pred)
        if not base_preds:
            raise ValueError("Stacking 基模型为空")
        meta_X = np.column_stack(base_preds)
        return self.meta_model.predict(meta_X)


def _load_stacking(model_dir: Path, meta: dict) -> _StackingEnsemble:
    """加载 stacking 融合源模型的全部基模型 + meta_model。"""
    model_types = meta.get("model_types", [])
    saved_models = meta.get("saved_models", {})
    base_fv = meta.get("base_model_fill_values", {})
    global_fv = meta.get("fill_values", {})
    features = meta.get("feature_columns") or meta.get("features", [])

    base_models = {}
    for mt in model_types:
        model_file = saved_models.get(mt, "")
        if not model_file:
            continue
        model_path = model_dir / model_file
        if not model_path.exists():
            logger.warning("Stacking 基模型缺失: %s", model_path)
            continue
        base_models[mt] = _load_base_model(model_path, mt)

    meta_model_path = model_dir / meta.get("meta_model_file", "meta_model.pkl")
    if not meta_model_path.exists():
        raise FileNotFoundError(f"Stacking meta_model 不存在: {meta_model_path}")
    with open(meta_model_path, "rb") as f:
        meta_data = pickle.load(f)
    meta_model = meta_data["model"] if isinstance(meta_data, dict) else meta_data

    fill_values = {}
    for mt in model_types:
        per_model_fv = base_fv.get(mt, {}) if isinstance(base_fv, dict) else {}
        fill_values[mt] = per_model_fv if per_model_fv else (global_fv if isinstance(global_fv, dict) else {})

    logger.info("加载 Stacking 融合源模型: %d 个基模型 + 元学习器", len(base_models))
    return _StackingEnsemble(
        base_models=base_models,
        meta_model=meta_model,
        model_types=model_types,
        fill_values=fill_values,
        features=features,
    )


# ═══════════════════════════════════════════════════════════════════════════
# 4. 单源模型预测
# ═══════════════════════════════════════════════════════════════════════════

def predict_with_model(model, meta: dict, day_df: pd.DataFrame) -> dict[str, float]:
    """对单个源模型执行推理，返回 {symbol: raw_score}。

    meta 决定特征列与填充值；day_df 为当日全市场数据副本。
    """
    feature_cols = meta.get("feature_columns") or meta.get("features", [])
    fill_values = meta.get("fill_values", {})
    best_iter = meta.get("best_iteration")

    # features_daily.return_Nd 是未来 N 日收益，不能作为特征喂给模型（标签泄漏）
    _leaky = [c for c in ("return_1d", "return_3d", "return_5d",
                          "return_10d", "return_20d", "return_60d")
              if c in day_df.columns]
    if _leaky:
        day_df = day_df.drop(columns=_leaky, errors="ignore")

    # 缺失列补 0
    missing = [c for c in feature_cols if c not in day_df.columns]
    if missing:
        logger.warning("源模型缺 %d 个特征列，填 0: %s", len(missing), missing[:8])
        for c in missing:
            day_df[c] = 0.0

    X = day_df[feature_cols].copy()
    for col, val in fill_values.items():
        if col in X.columns:
            X[col] = X[col].fillna(val)
    X = X.fillna(0.0)
    X_values = X.values.astype(np.float32)
    symbols = day_df["symbol"].tolist()

    is_stacking = isinstance(model, _StackingEnsemble)
    if is_stacking:
        scores = model.predict(X_values)
    elif hasattr(model, "get_dump") or str(type(model).__name__) == "Booster":
        # LightGBM Booster 支持 num_iteration；XGBoost 3.x Booster 需要 DMatrix
        if str(type(model).__module__).startswith("xgboost"):
            dmat = xgb.DMatrix(X_values, feature_names=list(feature_cols)) if xgb else None
            scores = model.predict(dmat)
        else:
            scores = model.predict(X_values, num_iteration=best_iter)
    else:
        # sklearn / pickle 模型
        if hasattr(model, "predict_proba"):
            scores = model.predict_proba(X_values)[:, 1]
        else:
            scores = model.predict(X_values)

    scores = np.asarray(scores).flatten()

    # 方向纠正：训练时 IC<0 的模型分数已翻转
    if meta.get("score_direction") == "reversed":
        scores = -scores

    result = {}
    for sym, s in zip(symbols, scores, strict=True):
        f = float(s)
        if f == f:  # 过滤 NaN
            result[sym] = f
    return result


# ═══════════════════════════════════════════════════════════════════════════
# 5. 融合逻辑
# ═══════════════════════════════════════════════════════════════════════════

_Z_CLIP = 3.0  # 保留（兼容 detail 展示用），融合主通道改为截面 rank 百分位
_RANK_DECAY = 0.6  # 时间平滑指数衰减（近5日）
_IC_DECAY = 0.1  # 动态权重 IC 指数衰减（exp(-d/10)）


def _cross_sectional_rank(scores: dict[str, float]) -> dict[str, float]:
    """单模型当日分数 → 截面 rank 百分位 (0,1)。

    消除不同模型分数量纲/分布差异，与训练标签截面 rank 口径一致。
    """
    if not scores:
        return {}
    syms = list(scores.keys())
    vals = [scores[s] for s in syms]
    s = pd.Series(vals)
    ranks = s.rank(method="average", pct=True)  # 0~1
    return {sym: float(ranks.iloc[i]) for i, sym in enumerate(syms)}


def _apply_time_smoothing(ranked: dict[str, float], history: dict[str, dict] | None,
                          model_id: str) -> dict[str, float]:
    """L2 时间平滑：当日 rank 分数与近5日历史分数指数加权 (0.6^d)。

    history = {model_id: {symbol: rank_score, ...}}（近5日聚合后的历史分数）。
    无历史则原样返回（冷启动单日）。
    """
    if not history or not history.get(model_id):
        return ranked
    hist = history[model_id]
    out = {}
    for sym, cur in ranked.items():
        h = hist.get(sym)
        if h is None:
            out[sym] = cur
        else:
            # 指数加权：当日权重 1，历史权重 0.6
            out[sym] = (cur + _RANK_DECAY * h) / (1 + _RANK_DECAY)
    return out


def _load_dynamic_weights(model_dir: Path, static_weights: dict[str, float]) -> dict[str, float]:
    """读 weight_snapshot.json 动态权重；缺失/异常回退静态权重。"""
    snap = model_dir / "weight_snapshot.json"
    if not snap.exists():
        return dict(static_weights)
    try:
        with open(snap, encoding="utf-8") as f:
            data = json.load(f)
        weights = {k: float(v) for k, v in data.items() if k != "_updated_at" and v is not None}
        tot = sum(weights.values())
        if tot <= 0:
            return dict(static_weights)
        return {k: v / tot for k, v in weights.items()}
    except Exception as e:
        logger.warning("weight_snapshot.json 加载失败，回退静态权重: %s", e)
        return dict(static_weights)


def _load_smooth_history(model_dir: Path) -> dict[str, dict] | None:
    """读 smooth_history.json（近5日各模型各 symbol 的 rank 分数）。"""
    f = model_dir / "smooth_history.json"
    if not f.exists():
        return None
    try:
        with open(f, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as e:
        logger.warning("smooth_history.json 加载失败: %s", e)
        return None


def fuse_scores(
    all_scores: dict[str, dict[str, float]],
    weights: dict[str, float],
    fusion_strategy: str = "linear",
    strategy_config: dict | None = None,
    model_horizons: dict[str, int] | None = None,
    model_dir: Path | None = None,
) -> list[dict]:
    """生产级五层融合。

    L1 截面 rank 化：每模型当日分数 → rank 百分位 (0,1)，消除量纲
    L2 时间平滑：与近5日历史分数指数加权（读 smooth_history.json）
    L3 动态权重：读 weight_snapshot.json（recent_ic 动态权）或静态权重
    L4 共识调节：方向一致率放大、分歧度收缩
    L5 置信度：agreement × coverage，输出 confidence 字段

    Returns:
        [{"symbol", "score"(-1~1), "consensus", "confidence", "zfusion", "detail"}, ...]
    """
    strategy_config = strategy_config or {}
    model_horizons = model_horizons or {}
    boundary = float(strategy_config.get("periodic_boundary", 10))
    gate_threshold = float(strategy_config.get("confidence_threshold", 0.6))

    # L1: 每模型截面 rank 化
    ranked: dict[str, dict[str, float]] = {}
    for mid, scores in all_scores.items():
        if not scores:
            continue
        r = _cross_sectional_rank(scores)
        if r:
            ranked[mid] = r

    if not ranked:
        return []

    # L2: 时间平滑（读 smooth_history.json）
    smooth_history = _load_smooth_history(model_dir) if model_dir else None
    if smooth_history:
        ranked = {mid: _apply_time_smoothing(r, smooth_history, mid) for mid, r in ranked.items()}

    # L3: 动态权重（读 weight_snapshot.json 覆盖静态）
    eff_weights = _load_dynamic_weights(model_dir, weights) if model_dir else dict(weights)

    n_models = len(ranked)
    results = []

    for sym in set().union(*(r.keys() for r in ranked.values())):
        per_model: dict[str, dict] = {}
        w_map: dict[str, float] = {}
        rank_map: dict[str, float] = {}

        for mid in sorted(ranked):
            if sym in ranked[mid]:
                w = eff_weights.get(mid, 1.0 / n_models)
                rank_map[mid] = ranked[mid][sym]
                w_map[mid] = w
                per_model[mid] = {
                    "raw": round(all_scores[mid][sym], 6),
                    "rank": round(ranked[mid][sym], 4),
                    "horizon": model_horizons.get(mid),
                }

        if not rank_map:
            continue

        # 共识度（rank>0.5 看多）与分歧度（rank 分数 std）
        vals = list(rank_map.values())
        n_bull = sum(1 for v in vals if v > 0.5)
        n_bear = sum(1 for v in vals if v < 0.5)
        consensus = max(n_bull, n_bear)
        agreement = consensus / n_models
        dispersion = float(np.std(vals)) if len(vals) > 1 else 0.0

        # 各策略融合（在 rank 分数上执行）
        if fusion_strategy == "majority_vote":
            majority_dir = 1 if n_bull >= n_bear else 0
            aligned = {mid: v for mid, v in rank_map.items() if (v > 0.5) == (majority_dir > 0) and v != 0.5}
            if not aligned:
                fused = 0.5
            else:
                tot_w = sum(w_map[mid] for mid in aligned)
                fused = sum(v * w_map[mid] for mid, v in aligned.items()) / tot_w if tot_w > 0 else 0.5
                fused = 0.5 + (fused - 0.5) * (len(aligned) / n_models)
        elif fusion_strategy == "periodic_hierarchy":
            long_v = [v for mid, v in rank_map.items() if (model_horizons.get(mid) or boundary) >= boundary]
            short_v = [v for mid, v in rank_map.items() if (model_horizons.get(mid) or boundary) < boundary]
            if not short_v:
                short_v = list(rank_map.values())
            fused = sum(short_v) / len(short_v) if short_v else 0.5
            if long_v:
                long_dir = sum(1 for v in long_v if v > 0.5) - sum(1 for v in long_v if v < 0.5)
                if long_dir > 0:
                    fused = 0.5 + (fused - 0.5) * 1.5
                elif long_dir < 0:
                    fused = 0.5 + (fused - 0.5) * 0.3
                else:
                    fused = 0.5 + (fused - 0.5) * 0.5
        elif fusion_strategy == "confidence_gate":
            consensus_ratio = agreement
            tot_w = sum(w_map.values())
            fused = sum(v * w_map[mid] for mid, v in rank_map.items()) / tot_w if tot_w > 0 else 0.5
            if consensus_ratio >= gate_threshold:
                pass
            elif consensus_ratio >= 0.4:
                fused = 0.5 + (fused - 0.5) * 0.5
            else:
                fused = 0.5
        else:
            # linear 默认
            tot_w = sum(w_map.values())
            fused = sum(v * w_map[mid] for mid, v in rank_map.items()) / tot_w if tot_w > 0 else 0.5

        # L4: 共识调节 + 分歧收缩（映射到 -1~1 后调节）
        score01 = max(0.0, min(1.0, fused))
        score01 = 0.5 + (score01 - 0.5) * (1 + 0.5 * (agreement - 0.5)) / (1 + 1.5 * dispersion)
        score01 = max(0.0, min(1.0, score01))
        fused_signal = (score01 - 0.5) * 2.0  # -1~1，正=看涨

        # L5: 置信度
        confidence = agreement * (1.0 - min(1.0, dispersion * 2.0))
        confidence = max(0.0, min(1.0, confidence))

        results.append({
            "symbol": sym,
            "score": round(float(fused_signal), 6),
            "consensus": int(consensus),
            "confidence": round(float(confidence), 4),
            "zfusion": round(float(fused_signal), 6),
            "detail": per_model,
        })

    return results


# ═══════════════════════════════════════════════════════════════════════════
# 6. 主流程
# ═══════════════════════════════════════════════════════════════════════════

def parse_args():
    p = argparse.ArgumentParser(description="融合模型推理")
    p.add_argument("--date", "-d", type=str, default=os.getenv("TRADE_DATE", ""))
    p.add_argument("--output", "-o", type=str, required=True)
    p.add_argument("--model-dir", type=str, default=os.getenv("MODEL_DIR", ""))
    p.add_argument("--data-dir", type=str, default=os.getenv("MODEL_TRAINING_DATA_DIR", _DEFAULT_DATA_DIR))
    p.add_argument("--market", type=str, default=os.getenv("MARKET", "CN"),
                   choices=["CN", "US", "HK", "CRYPTO", "FUTURES"],
                   help="目标市场，用于选择对应 parquet 数据")
    return p.parse_args()


def main():
    args = parse_args()

    trade_date = (args.date or "").strip()
    if not trade_date:
        logger.error("未指定推理日期（--date 或 TRADE_DATE 环境变量）")
        sys.exit(1)

    model_dir = Path(args.model_dir)
    data_dir = Path(args.data_dir)
    out_path = Path(args.output)
    market = (args.market or "CN").upper().strip()

    logger.info("=== 融合模型推理 ===")
    logger.info("  date     : %s", trade_date)
    logger.info("  market   : %s", market)
    logger.info("  model_dir: %s", model_dir)
    logger.info("  data_dir : %s", data_dir)

    # 1. 加载融合配置
    config = load_ensemble_config(model_dir)
    model_configs = config.get("models", [])
    logger.info("融合 %d 个源模型: %s", len(model_configs),
                [m.get("model_id", "?") for m in model_configs])

    # 2. 加载当日数据
    day_df = load_day_data(trade_date, data_dir, market=market)
    if day_df is None:
        msg = f"日期 {trade_date} 无数据"
        logger.warning(msg)
        print(msg, file=sys.stderr)
        sys.exit(2)

    # 融合算法与参数（ensemble_config.json）
    fusion_strategy = str(config.get("fusion_strategy") or "linear")
    strategy_config = config.get("strategy_config") or {}

    # 3. 逐源模型推理（缺失模型优雅降级）
    all_scores: dict[str, dict[str, float]] = {}
    weights: dict[str, float] = {}
    model_horizons: dict[str, int] = {}
    member_out_root = out_path.parent / "member_runs"

    for mc in model_configs:
        mid = str(mc.get("model_id") or mc.get("name") or "?")
        m_dir = Path(mc.get("model_dir") or "")
        w = float(mc.get("weight", 0.1))
        h = mc.get("target_horizon_days")
        if isinstance(h, (int, float)):
            model_horizons[mid] = int(h)

        if not m_dir.exists():
            logger.warning("源模型目录缺失，跳过 %s: %s", mid, m_dir)
            continue

        try:
            model, meta = load_source_model(m_dir)
            if isinstance(model, dict) and model.get("__dl_member__"):
                # DL 源模型：委派成员推理脚本（窗口截断 + 全流程兼容）
                scores = _predict_dl_source_model(
                    m_dir, trade_date, data_dir, member_out_root, market)
                logger.info("源模型 %s (DL): %d 条信号, weight=%.3f", mid, len(scores), w)
                all_scores[mid] = scores
                weights[mid] = w
                continue
            scores = predict_with_model(model, meta, day_df.copy())
            if scores:
                all_scores[mid] = scores
                weights[mid] = w
                logger.info("源模型 %s: %d 条信号, weight=%.3f", mid, len(scores), w)
            else:
                logger.warning("源模型 %s 预测为空，跳过", mid)
        except Exception as e:
            logger.warning("源模型 %s 推理失败: %s", mid, e)

    if not all_scores:
        logger.error("所有源模型推理均失败")
        sys.exit(1)

    # 4. 融合（生产级五层：rank化 + 时间平滑 + 动态权重 + 共识 + 置信度）
    signals = fuse_scores(
        all_scores,
        weights,
        fusion_strategy=fusion_strategy,
        strategy_config=strategy_config,
        model_horizons=model_horizons,
        model_dir=model_dir,
    )
    signals.sort(key=lambda x: x["score"], reverse=True)
    logger.info("融合完成: %d 条信号 (strategy=%s)", len(signals), fusion_strategy)

    # 5. 输出
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(signals, f, ensure_ascii=False)

    logger.info("已写入融合信号: %s (%d 条)", out_path, len(signals))


if __name__ == "__main__":
    main()
