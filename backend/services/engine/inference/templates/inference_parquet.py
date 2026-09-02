#!/usr/bin/env python3
"""
QuantMind Parquet 数据源推理脚本 (inference.py 模板)
=====================================================
适用于训练数据来自 feature_snapshots/*.parquet 的所有模型类型：
  - 树模型: LightGBM / XGBoost / CatBoost / sklearn
  - 深度学习: GRU / LSTM / ALSTM / Transformer / TCN / TabNet (PyTorch)

平台注入环境变量：
    MODEL_DIR      模型目录绝对路径（含 metadata.json + model 文件）
    TRADE_DATE     推理日期（同 --date 参数，互为备份）
    OUTPUT_FORMAT  固定值 json

调用方式（由 InferenceScriptRunner 自动调用）：
    python inference.py --date YYYY-MM-DD --output /path/to/out.json

输出格式（写入 --output 文件）：
    [{"symbol": "sh600519", "score": 0.82}, ...]

exit code：
    0  = 成功
    1  = 致命错误（模型/元数据损坏）
    2  = 该日期无可用数据（触发 alpha158 兜底）
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

import pickle

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
    from catboost import CatBoost
except ImportError:
    CatBoost = None

try:
    import torch
except ImportError:
    torch = None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("inference_parquet")

# ── 默认路径 ──────────────────────────────────────────────────────────────
_DEFAULT_DATA_DIR = "/app/db/feature_snapshots"


def _quantdb_reader(meta: dict, data_dir: Path):
    """Open the immutable raw QuantDB source for new direct-read models."""
    if meta.get("data_source") != "quantdb_factors":
        return None
    from backend.services.engine.data_platform.quantdb_factor_reader import QuantDBFactorReader
    pinned_dir = Path(str(meta.get("quantdb_dir") or ""))
    return QuantDBFactorReader(pinned_dir if pinned_dir.is_dir() else data_dir)


# ═══════════════════════════════════════════════════════════════════════════
# 1. 参数解析
# ═══════════════════════════════════════════════════════════════════════════

def parse_args():
    p = argparse.ArgumentParser(description="parquet 推理脚本")
    p.add_argument("--date", "-d", type=str,
                   default=os.getenv("TRADE_DATE", ""),
                   help="推理基准日期 YYYY-MM-DD")
    p.add_argument("--output", "-o", type=str, required=True,
                   help="输出 JSON 文件路径")
    p.add_argument("--model-dir", type=str,
                   default=os.getenv("MODEL_DIR", str(Path(__file__).parent)),
                   help="模型目录（含 metadata.json + model.lgb/model.xgb）")
    p.add_argument("--data-dir", type=str,
                   default=os.getenv("MODEL_TRAINING_DATA_DIR", _DEFAULT_DATA_DIR),
                   help="训练数据 parquet 目录")
    p.add_argument("--market", type=str, default=None,
                   help="市场（脚本运行器注入，如 HK/US；覆盖 metadata.context.market）")
    return p.parse_args()


# ═══════════════════════════════════════════════════════════════════════════
# 2. 元数据加载
# ═══════════════════════════════════════════════════════════════════════════

def load_metadata(model_dir: Path) -> dict:
    meta_path = model_dir / "metadata.json"
    if not meta_path.exists():
        logger.error("metadata.json 不存在: %s", meta_path)
        sys.exit(1)
    with open(meta_path, encoding="utf-8") as f:
        return json.load(f)


# ═══════════════════════════════════════════════════════════════════════════
# 3. 模型加载（支持 LightGBM / XGBoost / CatBoost / sklearn / PyTorch DL）
# ═══════════════════════════════════════════════════════════════════════════

# Qlib DL 模型类映射
_QLIB_DL_MAP: dict[str, tuple[str, str]] = {
    "GRU":              ("qlib.contrib.model.pytorch_gru_ts",         "GRU"),
    "LSTM":             ("qlib.contrib.model.pytorch_lstm_ts",        "LSTM"),
    "ALSTM":            ("qlib.contrib.model.pytorch_alstm_ts",       "ALSTM"),
    "TransformerModel": ("qlib.contrib.model.pytorch_transformer_ts", "TransformerModel"),
    "TCN":              ("qlib.contrib.model.pytorch_tcn_ts",         "TCN"),
    "TabnetModel":      ("qlib.contrib.model.pytorch_tabnet",         "TabnetModel"),
}


def _load_pytorch_model(model_path: Path, meta: dict):
    """加载 PyTorch / Qlib DL 模型。"""
    if torch is None:
        logger.error("模型为 PyTorch 格式，但 torch 未安装")
        sys.exit(1)

    model_class_name = meta.get("model_class_name")
    model_type = str(meta.get("model_type", ""))
    # NativeTFT：训练端保存 state_dict，需重建架构再 load_state_dict
    if model_type.upper() == "NATIVETFT":
        import torch.nn as nn
        import torch.nn.functional as F
        arch = meta.get("model_arch") or {}
        input_dim = int(arch.get("input_dim", 7))
        hidden_dim = int(arch.get("hidden_dim", 64))
        num_heads = int(arch.get("num_heads", 4))
        dropout = float(arch.get("dropout", 0.2))

        class _GRN(nn.Module):
            def __init__(self, i, h, o, p):
                super().__init__()
                self.lin1 = nn.Linear(i, h)
                self.lin2 = nn.Linear(h, h)
                self.gate = nn.Linear(h, o)
                self.drop = nn.Dropout(p)
                self.norm = nn.LayerNorm(o)
                self.skip = nn.Linear(i, o) if i != o else nn.Identity()
            def forward(self, x):
                h = F.elu(self.lin1(x)); h = self.lin2(h); h = self.drop(h)
                g = self.gate(h).sigmoid()
                return self.norm(self.skip(x) + g * h)

        class _NativeTFTNet(nn.Module):
            def __init__(self):
                super().__init__()
                self.input_proj = nn.Linear(input_dim, hidden_dim)
                self.gru = nn.GRU(hidden_dim, hidden_dim, batch_first=True)
                self.attn = nn.MultiheadAttention(hidden_dim, num_heads, batch_first=True)
                self.grn = _GRN(hidden_dim, hidden_dim, hidden_dim, dropout)
                self.output_layer = nn.Linear(hidden_dim, 1)
            def forward(self, x):
                h = self.input_proj(x)
                h_gru, _ = self.gru(h)
                attn_out, _ = self.attn(h_gru, h_gru, h_gru)
                h = h_gru + attn_out
                h = self.grn(h[:, -1, :])
                return self.output_layer(h).squeeze(-1)

        model = _NativeTFTNet()
        state_dict = torch.load(str(model_path), map_location="cpu", weights_only=True)
        model.load_state_dict(state_dict)
        model.eval()
        logger.info("NativeTFT 模型已加载: input=%d hidden=%d heads=%d", input_dim, hidden_dim, num_heads)
        return ("torch", model)

    if model_class_name and model_class_name in _QLIB_DL_MAP:
        # Qlib DL 模型: 重建模型架构 + 加载 state_dict
        import importlib
        mod_path, cls_name = _QLIB_DL_MAP[model_class_name]
        mod = importlib.import_module(mod_path)
        ModelCls = getattr(mod, cls_name)

        model_params = dict(meta.get("model_params", {}))
        model_params["GPU"] = -1  # 推理用 CPU
        model_obj = ModelCls(**model_params)

        state_dict = torch.load(str(model_path), map_location="cpu", weights_only=True)
        # 查找内部 PyTorch 模型：不同 Qlib 模型类用不同属性名
        # GRU→GRU_model, LSTM→LSTM_model, ALSTM→ALSTM_model, TCN→TCN_model,
        # TransformerModel→model, TabnetModel→tabnet_model
        inner_model = None
        for attr in ("model", "GRU_model", "gru_model", "LSTM_model", "lstm_model",
                     "ALSTM_model", "alstm_model", "TCN_model", "tcn_model",
                     "tabnet_model", "transformer_model"):
            inner_model = getattr(model_obj, attr, None)
            if inner_model is not None:
                break
        if inner_model is not None and state_dict is not None:
            inner_model.load_state_dict(state_dict)
            inner_model.eval()
        model_obj.fitted = True
        logger.info("Qlib DL 模型已加载: %s", model_class_name)
        return ("torch_qlib", model_obj)

    # 通用 PyTorch 模型
    model = torch.load(str(model_path), map_location="cpu", weights_only=False)
    if hasattr(model, "eval"):
        model.eval()
    logger.info("PyTorch 模型已加载")
    return ("torch", model)


def load_model(model_dir: Path, meta: dict):
    model_file = meta.get("model_file", "")
    model_path = model_dir / model_file if model_file else None

    # 如果 metadata 没指定，按扩展名搜索
    if not model_path or not model_path.exists():
        for ext in ("*.xgb", "*.lgb", "*.cbm", "*.pkl", "*.pth", "*.pt", "*.txt", "*.bin"):
            candidates = list(model_dir.glob(ext))
            if candidates:
                model_path = candidates[0]
                break
        else:
            logger.error("未找到模型文件: %s", model_dir)
            sys.exit(1)
        logger.warning("使用候选模型文件: %s", model_path.name)

    suffix = model_path.suffix.lower()
    logger.info("加载模型: %s (格式=%s)", model_path.name, suffix)

    if suffix == ".xgb":
        if xgb is None:
            logger.error("模型为 XGBoost 格式，但 xgboost 未安装")
            sys.exit(1)
        booster = xgb.Booster()
        booster.load_model(str(model_path))
        return ("xgb", booster)
    elif suffix == ".cbm":
        if CatBoost is None:
            logger.error("模型为 CatBoost 格式，但 catboost 未安装")
            sys.exit(1)
        model = CatBoost()
        model.load_model(str(model_path), format="cbm")
        return ("catboost", model)
    elif suffix == ".pkl":
        with open(model_path, "rb") as f:
            model = pickle.load(f)
        return ("sklearn", model)
    elif suffix in (".pth", ".pt"):
        return _load_pytorch_model(model_path, meta)
    else:
        # .lgb / .txt → LightGBM
        if lgb is None:
            logger.error("模型为 LightGBM 格式，但 lightgbm 未安装")
            sys.exit(1)
        return ("lgb", lgb.Booster(model_file=str(model_path)))


# ═══════════════════════════════════════════════════════════════════════════
# 4. 数据加载
# ═══════════════════════════════════════════════════════════════════════════

def filter_untradable_rows(df: pd.DataFrame) -> pd.DataFrame:
    """过滤不可交易记录（停牌、零成交、ST 股等）。

    剔除条件：
    - close <= 0（价格异常）
    - volume <= 0（零成交/停牌）
    - is_st == 1（ST / *ST / 退市整理股）
    """
    if df.empty:
        return df

    filtered = df.copy()

    if "close" in filtered.columns:
        filtered = filtered.loc[
            pd.to_numeric(filtered["close"], errors="coerce") > 0
        ].copy()

    if "volume" in filtered.columns:
        filtered = filtered.loc[
            pd.to_numeric(filtered["volume"], errors="coerce") > 0
        ].copy()

    # 排除 ST / *ST / 退市股
    if "is_st" in filtered.columns:
        filtered = filtered.loc[
            pd.to_numeric(filtered["is_st"], errors="coerce") != 1
        ].copy()

    return filtered


def _resolve_parquet_path(data_dir: Path, trade_date: str, meta: dict) -> Path | None:
    """Resolve parquet file path based on market context."""
    market = str(
        (meta.get("context") or {}).get("market", "")
    ).upper() if isinstance(meta.get("context"), dict) else ""

    # Market-specific parquet files (no year suffix)
    _MARKET_PARQUET: dict[str, str] = {
        "HK": "model_features_hk.parquet",
        "US": "model_features_us.parquet",
        "CRYPTO": "model_features_crypto.parquet",
        "FUTURES": "model_features_futures.parquet",
    }

    if market in _MARKET_PARQUET:
        p = Path(data_dir) / _MARKET_PARQUET[market]
        if p.exists():
            return p
        logger.warning("市场 parquet 文件不存在: %s", p)

    # CN or fallback: year-based parquet
    year = int(trade_date[:4])
    p = Path(data_dir) / f"model_features_{year}.parquet"
    if p.exists():
        return p

    # Legacy: no year suffix
    p = Path(data_dir) / "model_features.parquet"
    if p.exists():
        return p

    return None


def load_date_data(trade_date: str, data_dir: Path, meta: dict) -> pd.DataFrame | None:
    """加载指定日期的特征数据。返回 None 表示该日期无数据（exit 2）。"""
    reader = _quantdb_reader(meta, data_dir)
    if reader is not None:
        features = list(meta.get("feature_columns") or meta.get("features") or [])
        source = str(meta.get("factor_source") or "l1_l2_factors")
        try:
            status = reader.assert_ready(source, start=trade_date, end=trade_date)
            expected_hash = str(meta.get("factor_schema_hash") or "")
            if expected_hash and expected_hash != status.schema_hash:
                raise RuntimeError("QuantDB schema hash differs from model metadata")
            day_df = reader.read_day(
                source, features=features, trade_date=trade_date,
                feature_sources=meta.get("factor_field_sources") or None,
            )
            day_df["trade_date"] = pd.to_datetime(day_df["trade_date"]).dt.strftime("%Y-%m-%d")
        except Exception as exc:
            logger.error("QuantDB 直读失败: %s", exc)
            return None
    else:
        parquet_path = _resolve_parquet_path(data_dir, trade_date, meta)
        if parquet_path is None:
            logger.warning("找不到可用的 parquet 文件 (data_dir=%s, market=%s)", data_dir, (meta.get("context") or {}).get("market", ""))
            return None

        df = pd.read_parquet(parquet_path, engine="pyarrow")
        # 非 A 股 parquet 使用 'instrument' 列而非 'symbol'
        if "symbol" not in df.columns and "instrument" in df.columns:
            df = df.rename(columns={"instrument": "symbol"})
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d")
        day_df = df[df["trade_date"] == trade_date].copy()

    if len(day_df) == 0:
        logger.warning("日期 %s 在 parquet 中无数据", trade_date)
        return None

    # 过滤不可交易记录（停牌、零成交等）
    before_filter = len(day_df)
    day_df = filter_untradable_rows(day_df)
    after_filter = len(day_df)
    if before_filter != after_filter:
        logger.info(
            "过滤不可交易记录: %d -> %d (剔除 %d 条)",
            before_filter, after_filter, before_filter - after_filter
        )

    if len(day_df) == 0:
        logger.warning("日期 %s 过滤后无可交易数据", trade_date)
        return None

    logger.info("找到 %d 条可交易记录，日期=%s", len(day_df), trade_date)
    return day_df


def _apply_feat_norm(X: np.ndarray, meta: dict) -> np.ndarray:
    """按 metadata 的 feat_norm (训练集 mean/std) 标准化，推理与训练同分布。

    DL 时序模型训练时用训练集统计量标准化，推理必须复用同一统计量。
    无 feat_norm 时原样返回（旧模型兼容）。
    """
    fn = meta.get("feat_norm")
    if not isinstance(fn, dict) or not fn.get("mean") or not fn.get("std"):
        return X
    mean = np.asarray(fn["mean"], dtype=np.float32)
    std = np.asarray(fn["std"], dtype=np.float32)
    std = np.where(std == 0, 1.0, std)
    X = (X - mean) / std
    return np.nan_to_num(X, nan=0.0, posinf=0.0, neginf=0.0)


def _predict_dl_sequence(inner_model, window_df: pd.DataFrame, features: list[str],
                         meta: dict, step_len: int) -> np.ndarray:
    """DL 时序模型推理：按 symbol 分组建 step_len 滑窗，feat_norm 标准化后前向。

    返回与 window_df 中"每个 symbol 最后一个交易日"一一对应的预测分数。
    Qlib TS 模型期望输入 [batch, step_len, d_feat]，输出逐样本分数。
    """
    if torch is None:
        logger.error("DL 时序推理需要 torch")
        sys.exit(1)
    window_df = window_df.copy()
    # 只保留每个 symbol 的最后一个交易日用于输出（对应推理目标日）
    preds = []
    device = getattr(inner_model, "device", None)
    for sym, grp in window_df.groupby("symbol", sort=False):
        grp = grp.sort_values("trade_date")
        X = grp[features].values.astype(np.float32)
        X = _apply_feat_norm(X, meta)
        # 窗口不足 step_len 的股票跳过（Qlib TS 模型需要完整窗口）
        if len(X) < step_len:
            logger.warning("symbol %s 窗口不足 %d 天，跳过", sym, step_len)
            preds.append((str(sym), np.nan))
            continue
        # 最近 step_len 天建窗，前向取最后一天的输出
        x_tensor = torch.from_numpy(X[-step_len:]).unsqueeze(0).float()  # [1, step_len, d_feat]
        # TCN 期望 channels-first [batch, d_feat, seq]（训练时 qlib 内部 transpose）
        if "TCN" in str(meta.get("model_class_name", "")):
            x_tensor = x_tensor.transpose(1, 2)
        if device is not None:
            x_tensor = x_tensor.to(device)
        with torch.no_grad():
            out = inner_model(x_tensor)
            if isinstance(out, tuple):
                out = out[0]
            if hasattr(out, "dim") and out.dim() == 2 and out.shape[1] == 1:
                out = out.squeeze(-1)
            score = float(out.detach().cpu().numpy().reshape(-1)[-1])
        preds.append((str(sym), score))
    return np.array([p[1] for p in preds], dtype=np.float64)


def load_window_data(trade_date: str, data_dir: Path, meta: dict, step_len: int) -> pd.DataFrame:
    """加载 [trade_date - step_len 交易日, trade_date] 的历史窗口数据，供 DL 时序模型建窗。

    只取 trade_date 当天仍有记录的 symbol（与单日推理的股票集合一致），
    每 symbol 取 step_len 天窗口（含当日）。无历史窗口的 symbol 会因样本不足
    被 _build_ts_dataloader 丢弃——这里是按 symbol 过滤窗口。
    """
    reader = _quantdb_reader(meta, data_dir)
    if reader is not None:
        source = str(meta.get("factor_source") or "l1_l2_factors")
        features = list(meta.get("feature_columns") or meta.get("features") or [])
        try:
            dates = [d for d in reader.available_dates(source, end=trade_date) if d <= trade_date]
            if not dates:
                return pd.DataFrame()
            return reader.read_range(
                source, features=features, feature_sources=meta.get("factor_field_sources") or None,
                start=dates[max(0, len(dates) - step_len)], end=trade_date,
            )
        except Exception as exc:
            logger.error("QuantDB 时序窗口读取失败: %s", exc)
            return pd.DataFrame()

    parquet_path = _resolve_parquet_path(data_dir, trade_date, meta)
    if parquet_path is None:
        logger.warning("找不到可用的 parquet 文件 (data_dir=%s, market=%s)", data_dir, (meta.get("context") or {}).get("market", ""))
        return pd.DataFrame()

    df = pd.read_parquet(parquet_path, engine="pyarrow")
    if "symbol" not in df.columns and "instrument" in df.columns:
        df = df.rename(columns={"instrument": "symbol"})
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d")

    day_df = df[df["trade_date"] == trade_date].copy()
    if len(day_df) == 0:
        return pd.DataFrame()
    day_df = filter_untradable_rows(day_df)
    if len(day_df) == 0:
        return pd.DataFrame()

    target_symbols = set(day_df["symbol"].astype(str))
    window = df[df["symbol"].astype(str).isin(target_symbols)].copy()
    # 窗口必须截止到 trade_date（防止未来数据泄漏，且回填历史日期时
    # tail() 会取到 parquet 末尾的未来窗口，导致各日推理分数完全相同）
    window = window[window["trade_date"] <= trade_date]
    window = window.sort_values(["symbol", "trade_date"])

    # 每 symbol 保留最近 step_len 天（含当日）
    keep = window.groupby("symbol", group_keys=False).tail(step_len)
    logger.info("DL 窗口数据: %d 只股票, 每只≤%d 天, 合计 %d 行", len(target_symbols), step_len, len(keep))
    return keep


# ═══════════════════════════════════════════════════════════════════════════
# 5. 特征预处理
# ═══════════════════════════════════════════════════════════════════════════

def _cross_sectional_preprocess_inline(X_df: pd.DataFrame, meta: dict) -> pd.DataFrame:
    """截面预处理（与 train.py _prepare_arrays 的 prep_cfg 逻辑一致）。

    单日数据 = 一个截面：对每列分位缩尾 → 截面 Z-score。
    类别特征（ind_code_l1/l2）不参与变换。推理端无法复现中性化（无行业列），已略。
    """
    prep = meta.get("preprocessing")
    if not isinstance(prep, dict) or not prep.get("enabled"):
        return X_df
    _exclude = {"ind_code_l1", "ind_code_l2"}
    feats = [c for c in X_df.columns if c not in _exclude and c != "symbol"]
    for c in feats:
        col = pd.to_numeric(X_df[c], errors="coerce").to_numpy(dtype=np.float64)
        valid = ~np.isnan(col)
        if valid.sum() == 0:
            X_df[c] = 0.0
            continue
        # 分位缩尾
        if prep.get("winsor", True) and valid.sum() >= 10:
            lo = float(np.quantile(col[valid], 0.01))
            hi = float(np.quantile(col[valid], 0.99))
            if np.isfinite(lo) and np.isfinite(hi) and lo < hi:
                col = np.where(valid, np.clip(col, lo, hi), col)
        # 截面 Z-score
        mu = col[valid].mean()
        sd = col[valid].std()
        if sd == 0 or not np.isfinite(sd):
            X_df[c] = np.where(valid, 0.0, np.nan)
        else:
            X_df[c] = np.where(valid, (col - mu) / sd, np.nan)
    logger.info("Cross-sectional preprocessing applied (%d features)", len(feats))
    return X_df


def preprocess(df: pd.DataFrame, meta: dict) -> tuple[pd.DataFrame, list[str]]:
    """按 metadata.json 预处理，返回 (X_df, symbols)。

    新模型（metadata 含 preprocessing.enabled=true）走截面预处理；
    旧模型走 fill_values 路径（行为不变，兼容已注册模型）。
    """
    feature_cols = meta.get("feature_columns") or meta.get("features", [])
    fill_values  = meta.get("fill_values", {})

    # features_daily.return_Nd 是未来 N 日收益，不能映射为 mom_ret_Nd（过去收益），
    # 否则线上推理会把未来信息喂给模型
    _leaky = [
        c for c in ("return_1d", "return_3d", "return_5d",
                    "return_10d", "return_20d", "return_60d")
        if c in df.columns
    ]
    if _leaky:
        df = df.drop(columns=_leaky, errors="ignore")
        logger.warning("Dropped forward-looking return columns: %s", _leaky)

    # 缺失列补 0
    missing = [c for c in feature_cols if c not in df.columns]
    if missing:
        logger.warning("缺少 %d 个特征列，将填 0: %s", len(missing), missing[:8])
        for c in missing:
            df[c] = 0.0

    X_df = df[feature_cols].copy()

    prep = meta.get("preprocessing")
    if isinstance(prep, dict) and prep.get("enabled"):
        # 新模型：截面预处理（含缺失值填充，保持训练同分布）
        X_df = _cross_sectional_preprocess_inline(X_df, meta)
        X_df = X_df.fillna(0.0)
    else:
        # 旧模型：按 metadata 的 fill_values 填 NaN
        for col, val in fill_values.items():
            if col in X_df.columns:
                X_df[col] = X_df[col].fillna(val)
        X_df = X_df.fillna(0.0)

    symbols = df["symbol"].tolist()
    return X_df, symbols


# ═══════════════════════════════════════════════════════════════════════════
# 6. 主流程
# ═══════════════════════════════════════════════════════════════════════════

def main():
    args = parse_args()

    trade_date = (args.date or "").strip()
    if not trade_date:
        logger.error("未指定推理日期（--date 或 TRADE_DATE 环境变量）")
        sys.exit(1)

    model_dir = Path(args.model_dir)
    data_dir  = Path(args.data_dir)
    out_path  = Path(args.output)

    logger.info("=== parquet 推理脚本 ===")
    logger.info("  model_dir : %s", model_dir)
    logger.info("  data_dir  : %s", data_dir)
    logger.info("  date      : %s", trade_date)
    logger.info("  output    : %s", out_path)

    # 1. 元数据
    meta  = load_metadata(model_dir)
    # 脚本运行器对非 CN 市场注入 --market（HK/US/...），覆盖元数据市场归属，
    # 确保数据解析（因子直读/快照）走对应市场的数据管线
    if args.market:
        meta.setdefault("context", {})["market"] = args.market.upper()
        logger.info("  market    : %s (CLI 注入)", args.market)
    logger.info("  run_id    : %s", meta.get("run_id", "unknown"))
    logger.info("  features  : %d", len(meta.get("feature_columns") or meta.get("features", [])))

    # 2. 加载数据（日期不存在 → exit 2 触发兜底）
    day_df = load_date_data(trade_date, data_dir, meta)
    if day_df is None:
        msg = f"日期 {trade_date} 在 parquet 数据中无记录，触发兜底推理"
        logger.warning(msg)
        print(msg, file=sys.stderr)
        sys.exit(2)

    # 3. 加载模型。分位模型仍以 P50 作为平台主分数，但同时读取三个产物。
    is_quantile_model = (
        isinstance(meta.get("prediction_contract"), dict)
        and meta["prediction_contract"].get("kind") == "quantile_return"
    )
    quantile_files = meta.get("quantile_models") or {}
    required = ("p10", "p50", "p90")
    quantile_complete = all(
        isinstance(quantile_files.get(key), str)
        and (model_dir / quantile_files[key]).is_file()
        for key in required
    )
    # 分位模型产物缺失时回退单模型（model.lgb）推理，避免配置成分位模型但只
    # 部署了单个模型导致当日推理整体失败。窗口提示后按普通模型继续。
    if is_quantile_model and not quantile_complete:
        logger.warning(
            "分位模型产物不完整: %s，回退单模型推理（分数非分位口径）",
            quantile_files,
        )
        is_quantile_model = False
    if is_quantile_model:
        if lgb is None:
            logger.error("分位 LightGBM 模型推理需要 lightgbm")
            sys.exit(1)
        quantile_models = {key: lgb.Booster(model_file=str(model_dir / quantile_files[key])) for key in required}
        model_type, model = "lgb_quantile", quantile_models["p50"]
    else:
        model_type, model = load_model(model_dir, meta)

    # 4. 预处理
    X_df, symbols = preprocess(day_df, meta)

    if len(X_df) == 0:
        msg = f"日期 {trade_date} 预处理后无有效行"
        logger.warning(msg)
        print(msg, file=sys.stderr)
        sys.exit(2)

    # 5. 推理（不同框架使用不同的 predict 接口）
    best_iter = meta.get("best_iteration")
    X_values = X_df.values.astype(np.float32)

    quantile_values: np.ndarray | None = None
    if model_type == "lgb_quantile":
        raw = np.column_stack([
            quantile_models["p10"].predict(X_values),
            quantile_models["p50"].predict(X_values),
            quantile_models["p90"].predict(X_values),
        ])
        raw = np.sort(raw, axis=1)
        calibration = meta.get("calibration") or {}
        offset = float(calibration.get("offset") or 0.0)
        raw[:, 0] -= offset
        raw[:, 2] += offset
        quantile_values = np.sort(raw, axis=1)
        scores = quantile_values[:, 1]
    elif model_type == "xgb":
        dmat = xgb.DMatrix(X_values, feature_names=list(X_df.columns))
        scores = model.predict(dmat, iteration_range=(0, best_iter) if best_iter else None)
    elif model_type == "catboost":
        # 分类模型必须输出正类概率而非 predict() 的硬标签，保证线上排序与
        # 训练期 AUC/选股信号口径一致。
        if str(meta.get("target_mode") or "return").lower() == "classification":
            scores = model.predict_proba(X_values)[:, 1]
        else:
            scores = model.predict(X_values)
    elif model_type == "sklearn":
        if hasattr(model, "predict_proba"):
            scores = model.predict_proba(X_values)[:, 1]
        else:
            scores = model.predict(X_values)
    elif model_type in ("torch_qlib", "torch"):
        # PyTorch DL 模型推理
        inner_model = model
        if model_type == "torch_qlib":
            # Qlib DL 模型: 找到内部 PyTorch 模型
            inner_model = None
            for attr in ("model", "GRU_model", "gru_model", "LSTM_model", "lstm_model",
                         "ALSTM_model", "alstm_model", "TCN_model", "tcn_model",
                         "tabnet_model", "transformer_model"):
                inner_model = getattr(model, attr, None)
                if inner_model is not None:
                    break
            if inner_model is None:
                logger.error("Qlib DL 模型内部 PyTorch 模型未找到")
                sys.exit(1)
        inner_model.eval()
        is_seq = bool(meta.get("is_sequence_model", False))
        if is_seq:
            # 时序模型：读历史窗口 → 按 symbol 滑窗 → 标准化 → 前向
            step_len = int((meta.get("dl_params") or {}).get("dl_step_len", 20))
            window_df = load_window_data(trade_date, data_dir, meta, step_len)
            if len(window_df) == 0:
                logger.warning("日期 %s 无足够历史窗口供 DL 时序推理，触发兜底", trade_date)
                print("DL sequence inference: no window data", file=sys.stderr)
                sys.exit(2)
            features_meta = meta.get("feature_columns") or meta.get("features", [])
            scores = _predict_dl_sequence(inner_model, window_df, list(features_meta), meta, step_len)
            # symbols 对齐 window_df 每 symbol 最后一个交易日
            symbols = []
            for sym, grp in window_df.groupby("symbol", sort=False):
                _ = grp.sort_values("trade_date")
                symbols.append(str(sym))
        else:
            # 扁平模型（TabNet 等）：单日二维输入
            x_tensor = torch.from_numpy(X_values)
            device = getattr(model, "device", None) or getattr(inner_model, "device", None)
            if device is not None:
                x_tensor = x_tensor.to(device)
            with torch.no_grad():
                # TabNet.forward(x, priors) 需要 priors 参数（训练时 qlib 内部构造）
                if "Tabnet" in str(meta.get("model_class_name", "")):
                    priors = torch.ones(x_tensor.shape[0], x_tensor.shape[1], dtype=x_tensor.dtype)
                    if device is not None:
                        priors = priors.to(device)
                    pred = inner_model(x_tensor, priors)
                else:
                    pred = inner_model(x_tensor)
                if isinstance(pred, tuple):
                    pred = pred[0]
                pred = pred.detach().cpu().numpy()
            scores = pred.flatten()
    else:
        # LightGBM
        scores = model.predict(X_values, num_iteration=best_iter)

    logger.info("推理完成，生成 %d 条信号", len(scores))

    # 方向纠正：如果训练时检测到 IC < 0（模型反向），翻转分数使正分=看涨
    score_direction = meta.get("score_direction", "")
    if score_direction == "reversed":
        if quantile_values is not None:
            quantile_values = np.column_stack((-quantile_values[:, 2], -quantile_values[:, 1], -quantile_values[:, 0]))
        scores = -scores
        logger.info("检测到反向模型 (score_direction=reversed)，已翻转分数")

    # 6. 输出 JSON
    signals = []
    for idx, (sym, score) in enumerate(zip(symbols, scores)):
        if not np.isfinite(score):
            continue
        signal: dict[str, object] = {"symbol": sym, "score": float(score)}
        if quantile_values is not None:
            p10, p50, p90 = quantile_values[idx]
            if np.isfinite(p10) and np.isfinite(p50) and np.isfinite(p90):
                signal["detail"] = {
                    "quantile_prediction": {
                        "p10": float(p10), "p50": float(p50), "p90": float(p90),
                        "calibrated": True,
                        "central_coverage": float((meta.get("calibration") or {}).get("central_coverage") or 0.8),
                        "calibrated_coverage": (meta.get("calibration") or {}).get("calibrated_coverage"),
                    }
                }
        signals.append(signal)

    # 按 score 降序（辅助调试，不影响功能）
    signals.sort(key=lambda x: x["score"], reverse=True)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(signals, f, ensure_ascii=False)

    logger.info("已写入信号文件: %s  (%d 条)", out_path, len(signals))


if __name__ == "__main__":
    main()
