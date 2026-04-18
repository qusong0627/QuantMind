#!/usr/bin/env python3
import os
import sys
import logging
import numpy as np
import pandas as pd
import lightgbm as lgb
from pathlib import Path
from datetime import datetime
from multiprocessing import Pool, cpu_count

# Pre-import restricts
os.environ["OMP_NUM_THREADS"] = str(cpu_count()) # Use all cores for training
os.environ["MKL_NUM_THREADS"] = "1"

from alpha158_calculator import Alpha158Calculator
from inference import QlibBinaryLoader

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout
)
logger = logging.getLogger("Alpha158_Train")

# Global loader for worker processes
_worker_loader = None

def init_worker(path):
    global _worker_loader
    _worker_loader = QlibBinaryLoader(path)

def process_symbol(args):
    symbol, start_date, end_date = args
    try:
        # Use the worker-local loader
        df = _worker_loader.load_market_data([symbol], start_date, end_date)
        if df.empty or len(df) < 100: return None
        
        # Calculate features and labels
        feats = Alpha158Calculator.calculate(df)
        label = Alpha158Calculator.calculate_label(df)
        
        combined = pd.concat([feats, label], axis=1)
        # Drop rows where label is NaN (trailing days)
        combined = combined.dropna(subset=['LABEL0'])
        return combined
    except Exception as e:
        logger.error(f"Error processing {symbol}: {str(e)}")
        return None

def main():
    # Updated Time Ranges
    start_date = "2019-01-01"
    end_date = "2026-03-31"
    
    train_end_date = pd.Timestamp("2024-12-31")
    valid_end_date = pd.Timestamp("2025-06-30")
    
    model_output = str(Path(__file__).parent / "alpha158.bin")
    data_path = "/app/db/qlib_data"
    log_file = str(Path(__file__).parent / "train.log")
    
    # Also log to file
    file_handler = logging.FileHandler(log_file)
    file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(file_handler)

    logger.info("Starting Alpha158 V2 Training Pipeline")
    logger.info("Range: %s to %s", start_date, end_date)

    # Use a dummy loader just to get symbols
    main_loader = QlibBinaryLoader(data_path)
    symbols = [s for s in main_loader.all_symbols if not (s.startswith("BJ") or s.startswith("bj"))]
    logger.info("Total symbols after BJ filtering: %d (skipped %d)", len(symbols), len(main_loader.all_symbols) - len(symbols))

    # 1. Parallel Feature Engineering
    cores = min(cpu_count(), 64)
    logger.info("Using %d cores for parallel feature extraction...", cores)
    
    tasks = [(sym, start_date, end_date) for sym in symbols]
    
    all_dfs = []
    with Pool(processes=cores, initializer=init_worker, initargs=(data_path,)) as p:
        for i, res in enumerate(p.imap_unordered(process_symbol, tasks, chunksize=10)):
            if res is not None:
                all_dfs.append(res)
            if (i + 1) % 100 == 0:
                logger.info("Processed %d/%d symbols (valid entries: %d)...", i+1, len(symbols), len(all_dfs))

    if not all_dfs:
        logger.error("No data collected. Check data path.")
        sys.exit(1)

    logger.info("Combining data from %d symbols...", len(all_dfs))
    full_df = pd.concat(all_dfs).sort_index()
    del all_dfs # Free memory
    
    logger.info("Final dataset shape: %s", full_df.shape)
    
    # --- DUAL CROSS SECTIONAL NORMALIZATION ---
    logger.info("Applying cross-sectional percent rank normalization to features AND labels...")
    
    # We rank everything except possibly indices/non-feature cols, but here we rank everything in the columns
    # Every column (including LABEL0) is converted to a percentile rank (0.0-1.0) within its day
    full_df = full_df.groupby(level='datetime').rank(pct=True)
    
    logger.info("Dual cross-sectional rank normalization completed.")
    # ------------------------------------------

    # 2. Prepare Training/Validation/Testing split
    dt_values = full_df.index.get_level_values('datetime')
    
    train_mask = dt_values <= train_end_date
    valid_mask = (dt_values > train_end_date) & (dt_values <= valid_end_date)
    test_mask  = dt_values > valid_end_date
    
    train_data = full_df[train_mask]
    valid_data = full_df[valid_mask]
    test_data  = full_df[test_mask]
    
    if train_data.empty or valid_data.empty or test_data.empty:
        logger.error("Empty split! Train: %d, Valid: %d, Test: %d", len(train_data), len(valid_data), len(test_data))
        sys.exit(1)
    
    # Features & Labels
    X_train, y_train = train_data.drop(columns=['LABEL0']), train_data['LABEL0']
    X_valid, y_valid = valid_data.drop(columns=['LABEL0']), valid_data['LABEL0']
    X_test,  y_test  = test_data.drop(columns=['LABEL0']),  test_data['LABEL0']
    
    logger.info("Train: %d, Valid: %d, Test: %d", len(y_train), len(y_valid), len(y_test))

    # 3. Training
    params = {
        'boosting_type': 'gbdt',
        'objective': 'regression',
        'metric': {'l2'},
        'num_leaves': 31,
        'max_depth': 6,
        'min_data_in_leaf': 200,
        'learning_rate': 0.05,
        'feature_fraction': 0.8,
        'bagging_fraction': 0.8,
        'bagging_freq': 5,
        'lambda_l2': 1.0,
        'verbose': -1,
        'num_threads': cores
    }

    logger.info("Starting LightGBM training...")
    lgb_train = lgb.Dataset(X_train, y_train)
    lgb_eval = lgb.Dataset(X_valid, y_valid, reference=lgb_train)

    gbm = lgb.train(params,
                    lgb_train,
                    num_boost_round=1000,
                    valid_sets=lgb_eval,
                    callbacks=[
                        lgb.early_stopping(stopping_rounds=50),
                        lgb.log_evaluation(period=50)
                    ])

    # 4. Save Model
    logger.info("Saving model to %s", model_output)
    gbm.save_model(model_output)
    
    # 5. Evaluate IC on Multiple Segments
    def calculate_metrics(df_eval, tag="Validation"):
        logger.info(f"Evaluating metrics on {tag} set...")
        features = df_eval.drop(columns=['LABEL0'])
        labels = df_eval['LABEL0']
        preds = gbm.predict(features)
        
        tmp = df_eval.copy()
        tmp['PRED'] = preds
        daily_ic = tmp.groupby(level='datetime').apply(lambda x: x['PRED'].corr(x['LABEL0'], method='spearman'))
        
        mean_ic = daily_ic.mean()
        icir = mean_ic / daily_ic.std() if (daily_ic.std() != 0 and pd.notnull(daily_ic.std())) else 0.0
        logger.info("%s Mean IC: %.4f, ICIR: %.4f", tag, mean_ic, icir)
        return {"mean_ic": round(float(mean_ic), 4), "icir": round(float(icir), 4)}

    train_metrics = calculate_metrics(train_data, "Training")
    valid_metrics = calculate_metrics(valid_data, "Validation")
    test_metrics = calculate_metrics(test_data, "Testing")

    # 6. Update metadata.json
    import json
    meta_path = Path(__file__).parent / "metadata.json"
    if meta_path.exists():
        with open(meta_path, 'r', encoding='utf-8') as f:
            meta = json.load(f)
            
        if 'performance_metrics' not in meta:
            meta['performance_metrics'] = {}
            
        meta['performance_metrics']['train'] = train_metrics
        meta['performance_metrics']['valid'] = valid_metrics
        meta['performance_metrics']['test'] = test_metrics
        
        # Update labels in metadata
        meta['train_start'] = "2019-01-01"
        meta['train_end']   = "2024-12-31"
        meta['valid_start'] = "2025-01-01"
        meta['valid_end']   = "2025-06-30"
        meta['test_start']  = "2025-07-01"
        meta['test_end']    = "2026-03-31"
        meta['trained_at']  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        with open(meta_path, 'w', encoding='utf-8') as f:
            json.dump(meta, f, indent=2, ensure_ascii=False)
    logger.info("metadata.json updated with 3-way split metrics.")
    
    # 7. Generate Full Prediction File (pred.pkl) for Backtesting
    logger.info("Generating full prediction file for backtesting...")
    X_full = full_df.drop(columns=['LABEL0'])
    full_preds = gbm.predict(X_full)
    
    # Create prediction DataFrame with standard Qlib index (datetime, symbol)
    pred_df = pd.DataFrame(
        {'score': full_preds}, 
        index=X_full.index
    ).reorder_levels(['datetime', 'symbol']).sort_index()
    
    pred_path = Path(__file__).parent / "pred.pkl"
    pred_df.to_pickle(str(pred_path))
    logger.info("Full prediction file saved to %s (Shape: %s)", pred_path, pred_df.shape)
    
    logger.info("Training complete.")

if __name__ == "__main__":
    main()
