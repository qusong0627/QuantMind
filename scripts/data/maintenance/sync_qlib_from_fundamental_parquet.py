#!/usr/bin/env python3
"""Incrementally update db/qlib_data from db/custom/fundamental_aligned.parquet.

The input parquet stores raw OHLCV plus cumulative back-adjustment factor.
This script converts prices to back-adjusted values and updates the existing
Qlib binary layout in-place.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd


REQUIRED_BINS = ("open", "high", "low", "close", "volume", "factor")


def project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Incrementally sync qlib_data from fundamental_aligned parquet")
    parser.add_argument(
        "--parquet-path",
        default=str(project_root() / "db" / "custom" / "fundamental_aligned.parquet"),
        help="Source parquet path",
    )
    parser.add_argument(
        "--qlib-dir",
        default=str(project_root() / "db" / "qlib_data"),
        help="Target qlib_data directory",
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview only")
    return parser.parse_args()


def load_calendar(path: Path) -> list[str]:
    return [line.strip() for line in path.read_text().splitlines() if line.strip()]


def build_incremental_map(df: pd.DataFrame, dates: list[str]) -> dict[str, dict[str, dict[str, float]]]:
    if not dates:
        return {}
    target_dates = set(dates)
    df = df[df["trade_date"].isin(target_dates)].copy()
    data_map: dict[str, dict[str, dict[str, float]]] = {}
    for row in df.itertuples(index=False):
        symbol = row.symbol
        trade_date = row.trade_date
        if symbol not in data_map:
            data_map[symbol] = {}
        data_map[symbol][trade_date] = {
            "open": float(row.open) if pd.notna(row.open) else np.nan,
            "high": float(row.high) if pd.notna(row.high) else np.nan,
            "low": float(row.low) if pd.notna(row.low) else np.nan,
            "close": float(row.close) if pd.notna(row.close) else np.nan,
            "volume": float(row.volume) if pd.notna(row.volume) else 0.0,
            "adj_factor": float(row.adj_factor) if pd.notna(row.adj_factor) else 1.0,
        }
    return data_map


def qlib_value(row: dict[str, float], field: str) -> float:
    factor = row["adj_factor"]
    if field == "open":
        return row["open"] * factor
    if field == "high":
        return row["high"] * factor
    if field == "low":
        return row["low"] * factor
    if field == "close":
        return row["close"] * factor
    if field == "factor":
        return factor
    if field == "volume":
        return row["volume"] / (100.0 * factor) if factor > 0 else 0.0
    return np.nan


def rewrite_bin(bin_path: Path, full_calendar: list[str], symbol_rows: dict[str, dict[str, float]], dry_run: bool) -> None:
    raw_arr = np.frombuffer(bin_path.read_bytes(), dtype="<f4").copy()
    if len(raw_arr) < 1:
        return

    start_idx = int(raw_arr[0])
    old_vals = raw_arr[1:]
    new_len = len(full_calendar) - start_idx
    if new_len <= len(old_vals):
        return

    field = bin_path.name.split(".")[0]
    new_vals = np.full(new_len, np.nan, dtype="<f4")
    if field == "volume":
        new_vals[:] = 0.0
    new_vals[: len(old_vals)] = old_vals

    for idx in range(len(old_vals), new_len):
        cal_idx = start_idx + idx
        if cal_idx >= len(full_calendar):
            break
        date = full_calendar[cal_idx]
        row = symbol_rows.get(date)
        if row is None:
            continue
        new_vals[idx] = qlib_value(row, field)

    if dry_run:
        return

    final_bin = np.concatenate(([np.float32(start_idx)], new_vals)).astype("<f4")
    bin_path.write_bytes(final_bin.tobytes())


def update_instruments(instruments_dir: Path, latest_date: str, dry_run: bool) -> None:
    for txt_file in instruments_dir.glob("*.txt"):
        lines = txt_file.read_text().splitlines()
        output: list[str] = []
        for line in lines:
            parts = line.split()
            if len(parts) >= 3 and parts[0].upper().startswith(("SH", "SZ")):
                parts[2] = latest_date
                output.append("\t".join(parts))
            else:
                output.append(line)
        if not dry_run:
            txt_file.write_text("\n".join(output) + "\n")


def main() -> None:
    args = parse_args()
    parquet_path = Path(args.parquet_path).expanduser().resolve()
    qlib_dir = Path(args.qlib_dir).expanduser().resolve()

    cal_path = qlib_dir / "calendars" / "day.txt"
    features_root = qlib_dir / "features"
    instruments_dir = qlib_dir / "instruments"

    df = pd.read_parquet(
        parquet_path,
        columns=["trade_date", "symbol", "open", "high", "low", "close", "volume", "adj_factor"],
    )
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d")
    df = df.sort_values(["trade_date", "symbol"]).reset_index(drop=True)

    existing_calendar = load_calendar(cal_path)
    all_dates = sorted(df["trade_date"].unique().tolist())
    missing_dates = [d for d in all_dates if d > existing_calendar[-1]]

    if not missing_dates:
        print("qlib_data already up to date")
        return

    full_calendar = sorted(set(existing_calendar + missing_dates))
    data_map = build_incremental_map(df, missing_dates)
    updated_symbols = 0

    for symbol, symbol_rows in data_map.items():
        symbol_dir = features_root / symbol.lower()
        if not symbol_dir.exists():
            continue
        updated_symbols += 1
        for field in REQUIRED_BINS:
            bin_path = symbol_dir / f"{field}.day.bin"
            if not bin_path.exists():
                continue
            rewrite_bin(bin_path, full_calendar, symbol_rows, args.dry_run)

    if not args.dry_run:
        cal_path.write_text("\n".join(full_calendar) + "\n")
    update_instruments(instruments_dir, full_calendar[-1], args.dry_run)

    print(
        f"qlib_data synced: symbols={updated_symbols}, dates={missing_dates[0]}..{missing_dates[-1]}, "
        f"dry_run={args.dry_run}"
    )


if __name__ == "__main__":
    main()
