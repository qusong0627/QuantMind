#!/usr/bin/env python3
"""
QuantMind Parquet 数据源推理脚本 (inference.py 模板)
=====================================================
适用于训练数据来自 feature_snapshots/*.parquet 的 LightGBM 模型。

平台注入环境变量：
    MODEL_DIR      模型目录绝对路径（含 metadata.json + model.lgb）
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

import lightgbm as lgb
import numpy as np
import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("inference_parquet")

# ── 默认路径 ──────────────────────────────────────────────────────────────
_DEFAULT_DATA_DIR = "/app/db/feature_snapshots"


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
                   help="模型目录（含 metadata.json + model.lgb）")
    p.add_argument("--data-dir", type=str,
                   default=os.getenv("MODEL_TRAINING_DATA_DIR", _DEFAULT_DATA_DIR),
                   help="训练数据 parquet 目录")
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
# 3. 模型加载
# ═══════════════════════════════════════════════════════════════════════════

def load_model(model_dir: Path, meta: dict) -> lgb.Booster:
    model_file = meta.get("model_file", "model.lgb")
    model_path = model_dir / model_file
    if not model_path.exists():
        candidates = list(model_dir.glob("*.lgb")) + list(model_dir.glob("*.txt"))
        if not candidates:
            logger.error("未找到 LightGBM 模型文件: %s", model_dir)
            sys.exit(1)
        model_path = candidates[0]
        logger.warning("使用候选模型文件: %s", model_path.name)
    logger.info("加载模型: %s", model_path.name)
    return lgb.Booster(model_file=str(model_path))


# ═══════════════════════════════════════════════════════════════════════════
# 4. 数据加载
# ═══════════════════════════════════════════════════════════════════════════

def filter_untradable_rows(df: pd.DataFrame) -> pd.DataFrame:
    """过滤不可交易记录（停牌、零成交等）。

    剔除条件：
    - close <= 0（价格异常）
    - volume <= 0（零成交/停牌）
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

    return filtered


def load_date_data(trade_date: str, data_dir: Path, meta: dict) -> pd.DataFrame | None:
    """加载指定日期的特征数据。返回 None 表示该日期无数据（exit 2）。"""
    year = int(trade_date[:4])
    parquet_path = Path(data_dir) / f"model_features_{year}.parquet"
    if not parquet_path.exists():
        logger.warning("parquet 文件不存在: %s", parquet_path)
        return None

    df = pd.read_parquet(parquet_path, engine="pyarrow")
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


# ═══════════════════════════════════════════════════════════════════════════
# 5. 特征预处理
# ═══════════════════════════════════════════════════════════════════════════

def preprocess(df: pd.DataFrame, meta: dict) -> tuple[pd.DataFrame, list[str]]:
    """按 metadata.json 中的 feature_columns/fill_values 预处理，返回 (X_df, symbols)。"""
    feature_cols = meta.get("feature_columns") or meta.get("features", [])
    fill_values  = meta.get("fill_values", {})

    # 缺失列补 0
    missing = [c for c in feature_cols if c not in df.columns]
    if missing:
        logger.warning("缺少 %d 个特征列，将填 0: %s", len(missing), missing[:8])
        for c in missing:
            df[c] = 0.0

    X_df = df[feature_cols].copy()

    # 按 metadata 的 fill_values 填 NaN
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
    logger.info("  run_id    : %s", meta.get("run_id", "unknown"))
    logger.info("  features  : %d", len(meta.get("feature_columns") or meta.get("features", [])))

    # 2. 加载数据（日期不存在 → exit 2 触发兜底）
    day_df = load_date_data(trade_date, data_dir, meta)
    if day_df is None:
        msg = f"日期 {trade_date} 在 parquet 数据中无记录，触发兜底推理"
        logger.warning(msg)
        print(msg, file=sys.stderr)
        sys.exit(2)

    # 3. 加载模型
    model = load_model(model_dir, meta)

    # 4. 预处理
    X_df, symbols = preprocess(day_df, meta)

    if len(X_df) == 0:
        msg = f"日期 {trade_date} 预处理后无有效行"
        logger.warning(msg)
        print(msg, file=sys.stderr)
        sys.exit(2)

    # 5. 推理
    best_iter = meta.get("best_iteration")
    scores = model.predict(X_df.values.astype(np.float32), num_iteration=best_iter)
    logger.info("推理完成，生成 %d 条信号", len(scores))

    # 6. 输出 JSON
    signals = [
        {"symbol": sym, "score": float(score)}
        for sym, score in zip(symbols, scores)
        if not (isinstance(score, float) and (score != score))  # 过滤 NaN
    ]

    # 按 score 降序（辅助调试，不影响功能）
    signals.sort(key=lambda x: x["score"], reverse=True)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(signals, f, ensure_ascii=False)

    logger.info("已写入信号文件: %s  (%d 条)", out_path, len(signals))


if __name__ == "__main__":
    main()
