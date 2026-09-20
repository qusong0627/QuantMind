#!/usr/bin/env python3
"""
QuantMind 全市场批量推理工具
---------------------------
功能：按模型 metadata 从 QuantDB 因子源直读最新数据，生成全市场预测分。
用途：每日增量入库完成后，生成次日交易参考。
"""

import logging
import os
import sys
from pathlib import Path

import pandas as pd

# 路径配置
PROJECT_ROOT = Path(__file__).resolve().parents[4]
OUTPUT_DIR = PROJECT_ROOT / "data" / "predictions"

# 导入内部模块
sys.path.append(str(PROJECT_ROOT))
from backend.services.engine.inference.model_loader import ModelLoader
from backend.services.engine.inference.data_loader import (
    get_available_dates,
    load_date_data,
    preprocess,
)
from backend.services.engine.inference.backtest_service import BacktestService

# 日志配置
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("BatchPredict")


def run_batch_inference(model_id: str = "", target_date=None):
    """
    执行批量推理
    :param model_id: 模型目录名（必填；原默认 model_qlib 已废弃）
    :param target_date: 推理日期，默认为模型绑定 QuantDB 因子源的最新交易日
    """
    if not str(model_id or "").strip():
        raise ValueError("model_id 必填：系统内置 model_qlib 已废弃，请显式指定模型")
    # 1. Load immutable model metadata before choosing a data source.
    loader = ModelLoader(PROJECT_ROOT / "models" / "production")
    loader.load_model(model_id)
    model = loader.get_model(model_id)
    metadata = loader.get_model_metadata(model_id)

    if model is None or not metadata:
        logger.error("模型加载失败")
        return
    if str(metadata.get("data_source") or "").lower() != "quantdb_factors":
        raise RuntimeError(
            f"{model_id} is not a QuantDB direct-read model; retrain it before using batch inference"
        )

    feature_cols = metadata.get("feature_columns", [])
    if not feature_cols:
        logger.error("模型元数据中未发现特征列定义")
        return

    # pin 口径唯一实现见 backend/shared/quantdb_paths.resolve_pinned_data_dir
    from backend.shared.quantdb_paths import resolve_pinned_data_dir

    pinned_dir = resolve_pinned_data_dir(metadata.get("quantdb_dir"))
    data_dir = (
        pinned_dir
        if pinned_dir
        else Path(os.getenv("QUANTDB_DATA_DIR")
                  or os.getenv("QM_QUANTDB_DATA_DIR")
                  or str(PROJECT_ROOT / "data" / "quantdb"))
    )
    if target_date is None:
        available = get_available_dates(data_dir=data_dir, meta=metadata)
        if not available:
            raise RuntimeError(f"No available dates in QuantDB: {data_dir}")
        target_date = available[-1]
    target_date = pd.Timestamp(target_date).strftime("%Y-%m-%d")
    logger.info("开始 QuantDB 直读批量推理 - 日期: %s, 模型: %s", target_date, model_id)

    # 2. The shared loader enforces source schema, mapped fields, and the
    # canonical prefix stock-code format used by downstream persistence.
    day_df = load_date_data(target_date, data_dir=data_dir, meta=metadata)
    if day_df is None or day_df.empty:
        logger.error("在 %s 未找到可推理的 QuantDB 数据", target_date)
        return

    X_df, symbols = preprocess(day_df, metadata)
    scores = BacktestService._predict(model, X_df)
    if scores is None:
        raise RuntimeError(f"Batch prediction failed for {model_id}")

    # 3. Persist in the same code format and date contract as online inference.
    result_df = pd.DataFrame({"symbol": symbols, "date": target_date, "score": scores})

    # 7. 持久化
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    file_name = f"pred_{pd.Timestamp(target_date).strftime('%Y%m%d')}.csv"
    output_path = OUTPUT_DIR / file_name

    result_df.to_csv(output_path, index=False)

    # 同时生成一个“最新”快捷方式
    latest_path = OUTPUT_DIR / "latest_prediction.csv"
    result_df.to_csv(latest_path, index=False)

    logger.info(f"推理完成！结果已保存至: {output_path}")
    return output_path


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--model", type=str, default=None, help="模型目录名（必填；原默认 model_qlib 已废弃）")
    parser.add_argument("--date", type=str, default=None)
    args = parser.parse_args()

    if not args.model:
        parser.error("--model 必填：系统内置 model_qlib 已废弃")

    target_dt = pd.to_datetime(args.date) if args.date else None
    run_batch_inference(args.model, target_dt)
