#!/usr/bin/env python3
import os
import sys
import json
import argparse
import logging
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import datetime, timedelta

# Stability Fix: Strictly restrict threading BEFORE importing LightGBM
os.environ["OMP_NUM_THREADS"] = "1"
os.environ["MKL_NUM_THREADS"] = "1"
os.environ["OPENBLAS_NUM_THREADS"] = "1"
os.environ["VECLIB_MAXIMUM_THREADS"] = "1"
os.environ["NUMEXPR_NUM_THREADS"] = "1"

import lightgbm as lgb
from alpha158_calculator import Alpha158Calculator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr
)
logger = logging.getLogger("Alpha158_V2")

class QlibBinaryLoader:
    def __init__(self, data_path):
        self.data_path = Path(data_path)
        self.calendar_path = self.data_path / "calendars" / "day.txt"
        self.instruments_path = self.data_path / "instruments" / "all.txt"
        
        if not self.calendar_path.exists():
            raise FileNotFoundError(f"Calendar not found: {self.calendar_path}")
            
        self.calendar = [line.strip() for line in self.calendar_path.read_text().splitlines()]
        self.date_to_idx = {date: idx for idx, date in enumerate(self.calendar)}
        
        if self.instruments_path.exists():
            self.all_symbols = [line.strip().split('\t')[0] for line in self.instruments_path.read_text().splitlines()]
        else:
            self.all_symbols = [d.name for d in (self.data_path / "features").iterdir() if d.is_dir()]

    def load_feature(self, symbol, feature, start_idx, end_idx):
        # Handle casing
        symbol_path = symbol.lower()
        candidates = [
            self.data_path / "features" / symbol_path / f"{feature}.day.bin",
            self.data_path / "features" / symbol_path / f"{feature}.bin",
            self.data_path / "features" / symbol / f"{feature}.day.bin",
        ]
        bin_path = next((p for p in candidates if p.exists()), None)
        if not bin_path: return None
        
        filesize = os.path.getsize(bin_path)
        available_count = filesize // 4
        
        # If the requested start is beyond file end, we have no data
        if start_idx >= available_count: return None
        
        # Determine how much we can actually read
        read_end_idx = min(end_idx, available_count - 1)
        count = read_end_idx - start_idx + 1
        
        if count <= 0: return None
        
        offset = start_idx * 4
        with open(bin_path, 'rb') as f:
            f.seek(offset)
            data = np.fromfile(f, dtype=np.float32, count=count)
            
        # If we read less than requested (due to file end), pad with NaNs if needed
        # but actually load_market_data expects the returned array to be correct for the specific dates it slices.
        # So we should return the data and let the caller handle alignment or pad here.
        if len(data) < (end_idx - start_idx + 1):
            padded = np.full(end_idx - start_idx + 1, np.nan, dtype=np.float32)
            padded[:len(data)] = data
            return padded
        return data

    def load_market_data(self, symbols, start_date, end_date):
        # Find nearest indices in calendar
        valid_dates = [d for d in self.calendar if start_date <= d <= end_date]
        if not valid_dates:
            return pd.DataFrame()
            
        s_date, e_date = valid_dates[0], valid_dates[-1]
        s_idx, e_idx = self.date_to_idx[s_date], self.date_to_idx[e_date]
        
        features = ["open", "high", "low", "close", "volume", "vwap", "factor"]
        dates = self.calendar[s_idx:e_idx+1]
        
        all_data = []
        for sym in symbols:
            sym_data = {}
            valid = True
            for feat in features:
                data = self.load_feature(sym, feat, s_idx, e_idx)
                if data is not None and len(data) == len(dates):
                    sym_data[feat] = data
                elif feat in ["vwap", "factor"]:
                    # Fallback for derived features not in binary
                    if feat == "vwap": sym_data[feat] = sym_data.get("close", np.zeros_like(dates))
                    else: sym_data[feat] = np.ones_like(dates, dtype=np.float32)
                else:
                    valid = False; break
            if valid:
                df = pd.DataFrame(sym_data, index=pd.to_datetime(dates))
                df.index.name = 'datetime'; df['symbol'] = sym
                all_data.append(df)
        
        if not all_data: return pd.DataFrame()
        return pd.concat(all_data).set_index('symbol', append=True).reorder_levels(['symbol', 'datetime']).sort_index()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True, help="Inference date YYYY-MM-DD")
    parser.add_argument("--output", required=True, help="Output JSON path")
    parser.add_argument("--model_path", default=str(Path(__file__).parent / "alpha158.bin"))
    parser.add_argument("--data_path", default="/app/db/qlib_data")
    args = parser.parse_args()

    logger.info("Alpha158 V2: Initializing Targeted Inference Pipeline")
    
    # 1. Load Data
    loader = QlibBinaryLoader(args.data_path)
    # Filter out BJ stocks from the loader's default symbols
    target_symbols = [s for s in loader.all_symbols if not (s.startswith("BJ") or s.startswith("bj"))]
    
    # Alpha158 needs historical background for rolling features (max window is 60)
    target_dt = datetime.strptime(args.date, "%Y-%m-%d")
    lookback_days = 90 # Buffer for valid trading days
    hist_start_dt = (target_dt - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    # Find nearest valid start date in calendar
    start_date = next((d for d in reversed(loader.calendar) if d <= hist_start_dt), loader.calendar[0])
    
    logger.info("Loading binary market data for %d symbols (skipped BJ stocks)...", len(target_symbols))
    df = loader.load_market_data(target_symbols, start_date, args.date)
    
    if df.empty:
        logger.error("No valid market data loaded. Aborting.")
        sys.exit(2)

    # 2. Calculate Features
    logger.info("Calculating 158 factors for %d symbols across %d days...", len(loader.all_symbols), len(df.groupby(level='datetime')))
    features_df = Alpha158Calculator.calculate(df)
    
    # Only keep the target date for prediction
    target_date_ts = pd.Timestamp(args.date)
    if target_date_ts not in features_df.index.get_level_values('datetime'):
        # Find latest available date in features
        available_dates = features_df.index.get_level_values('datetime').unique()
        target_date_ts = available_dates[-1]
        logger.warning("Target date %s missing in features. Falling back to latest date: %s", args.date, target_date_ts)

    X = features_df.xs(target_date_ts, level='datetime')
    symbols = X.index.tolist()
    
    if X.empty:
        logger.error("Empty feature set for target date. Aborting.")
        sys.exit(2)

    # 3. Prediction
    # EXTRA STABILITY: Load model explicitly and set threads
    logger.info("Loading model and predicting for %d symbols...", len(X))
    try:
        booster = lgb.Booster(model_file=args.model_path)
        # Force single-threaded prediction to avoid SEGV on large multi-core servers
        preds = booster.predict(X.values.astype(np.float32), num_threads=1)
    except Exception as e:
        logger.error("LightGBM Prediction Failed: %s", e)
        # Final fallback: If booster fails to load, create fake scores to maintain UI flow
        # but mark it as failure in logs
        sys.exit(1)
    
    # 4. Filter and Output
    results = [{"symbol": sym, "score": float(p)} for sym, p in zip(symbols, preds) if not pd.isna(p)]
    results.sort(key=lambda x: x["score"], reverse=True)
    
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(results, f)
    
    logger.info("Inference successful. Generated %d signals.", len(results))

if __name__ == "__main__":
    main()
