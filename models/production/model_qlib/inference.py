#!/usr/bin/env python3
"""
QuantMind Parquet 数据源推理脚本
=================================
由训练流水线自动生成，适用于 feature_snapshots/*.parquet 数据源。

调用方式（由 InferenceScriptRunner 自动调用）：
    python inference.py --date YYYY-MM-DD --output /path/to/out.json

输出格式：[{"symbol": "sh600519", "score": 0.82}, ...]
exit code: 0=成功  1=致命错误  2=该日期无数据（触发兜底）
"""
from __future__ import annotations
import argparse, json, logging, os, sys
from pathlib import Path
import lightgbm as lgb
import numpy as np
import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", stream=sys.stderr)
logger = logging.getLogger("inference_parquet")

_DEFAULT_DATA_DIR = "/app/db/feature_snapshots"

def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--date", "-d", type=str, default=os.getenv("TRADE_DATE", ""))
    p.add_argument("--output", "-o", type=str, required=True)
    p.add_argument("--model-dir", type=str, default=os.getenv("MODEL_DIR", str(Path(__file__).parent)))
    p.add_argument("--data-dir", type=str, default=os.getenv("MODEL_TRAINING_DATA_DIR", _DEFAULT_DATA_DIR))
    return p.parse_args()

def load_metadata(model_dir):
    meta_path = Path(model_dir) / "metadata.json"
    if not meta_path.exists():
        logger.error("metadata.json 不存在: %s", meta_path); sys.exit(1)
    return json.loads(meta_path.read_text(encoding="utf-8"))

def load_model(model_dir, meta):
    model_path = Path(model_dir) / meta.get("model_file", "model.lgb")
    if not model_path.exists():
        candidates = list(Path(model_dir).glob("*.lgb")) + list(Path(model_dir).glob("*.txt"))
        if not candidates:
            logger.error("未找到 LightGBM 模型文件: %s", model_dir); sys.exit(1)
        model_path = candidates[0]
    logger.info("加载模型: %s", model_path.name)
    return lgb.Booster(model_file=str(model_path))

def load_date_data(trade_date, data_dir, meta):
    year = int(trade_date[:4])
    parquet_path = Path(data_dir) / f"model_features_{year}.parquet"
    if not parquet_path.exists():
        logger.warning("parquet 文件不存在: %s", parquet_path); return None
    df = pd.read_parquet(parquet_path, engine="pyarrow")
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d")
    day_df = df[df["trade_date"] == trade_date].copy()
    if len(day_df) == 0:
        logger.warning("日期 %s 无数据", trade_date); return None
    logger.info("找到 %d 条记录，日期=%s", len(day_df), trade_date)
    return day_df

def preprocess(df, meta):
    feature_cols = meta.get("feature_columns") or meta.get("features", [])
    fill_values  = meta.get("fill_values", {})
    missing = [c for c in feature_cols if c not in df.columns]
    if missing:
        logger.warning("缺少 %d 个特征列，填 0: %s", len(missing), missing[:8])
        for c in missing: df[c] = 0.0
    X_df = df[feature_cols].copy()
    for col, val in fill_values.items():
        if col in X_df.columns: X_df[col] = X_df[col].fillna(val)
    return X_df.fillna(0.0), df["symbol"].tolist()

def main():
    args = parse_args()
    trade_date = (args.date or "").strip()
    if not trade_date:
        logger.error("未指定推理日期"); sys.exit(1)
    model_dir, data_dir, out_path = Path(args.model_dir), Path(args.data_dir), Path(args.output)
    logger.info("=== parquet 推理脚本 === date=%s  model_dir=%s", trade_date, model_dir)
    meta  = load_metadata(model_dir)
    day_df = load_date_data(trade_date, data_dir, meta)
    if day_df is None:
        print(f"日期 {trade_date} 无数据，触发兜底", file=sys.stderr); sys.exit(2)
    model = load_model(model_dir, meta)
    X_df, symbols = preprocess(day_df, meta)
    if len(X_df) == 0:
        print(f"日期 {trade_date} 预处理后无有效行", file=sys.stderr); sys.exit(2)
    scores = model.predict(X_df.values.astype(np.float32), num_iteration=meta.get("best_iteration"))
    signals = sorted(
        [{"symbol": s, "score": float(v)} for s, v in zip(symbols, scores) if v == v],
        key=lambda x: x["score"], reverse=True
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(signals, ensure_ascii=False), encoding="utf-8")
    logger.info("已写入信号文件: %s  (%d 条)", out_path, len(signals))

if __name__ == "__main__":
    main()
