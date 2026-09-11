#!/usr/bin/env python3
"""Backfill financial data (total_mv, pe_ttm, pb) into stock_daily_latest from QuantDB."""
import os
import sys
import time
import logging
from datetime import date, timedelta

import pandas as pd
import psycopg2
from psycopg2.extras import execute_values

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("backfill_financial")

def main():
    conn = psycopg2.connect(
        f"postgresql://{os.getenv('DB_USER')}:{os.getenv('DB_PASSWORD')}@"
        f"{os.getenv('DB_HOST')}:{os.getenv('DB_PORT')}/{os.getenv('DB_NAME')}"
    )
    cur = conn.cursor()

    from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub
    from sync_stock_daily_latest_from_parquet import load_quantdb_frame

    hub = QuantDBDataHub.get_instance()
    if not hub.available:
        logger.error("QuantDB data not available; abort")
        conn.close()
        return
    start = date.today() - timedelta(days=400)
    end = date.today()
    logger.info("Reading QuantDB frame [%s, %s]...", start, end)
    df = load_quantdb_frame(hub, start, end)
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date

    # Keep only rows with financial data
    cols = [c for c in ("trade_date", "symbol", "total_mv", "pe_ttm", "pb") if c in df.columns]
    valid = df[df["total_mv"].notna()][cols].copy() if "total_mv" in df.columns else pd.DataFrame(columns=cols)
    logger.info(f"Rows with valid financial data: {len(valid)}")

    # Create temp table
    cur.execute("DROP TABLE IF EXISTS _tmp_financial_backfill")
    cur.execute("""
        CREATE TEMP TABLE _tmp_financial_backfill (
            trade_date date,
            symbol text,
            total_mv double precision,
            pe_ttm double precision,
            pb double precision
        )
    """)
    conn.commit()

    # Insert into temp table using execute_values
    rows = [
        (r.trade_date, r.symbol,
         float(r.total_mv),
         float(r.pe_ttm) if pd.notna(r.pe_ttm) else None,
         float(r.pb) if pd.notna(r.pb) else None)
        for r in valid.itertuples(index=False)
    ]
    logger.info(f"Inserting {len(rows)} rows into temp table...")
    execute_values(cur, """
        INSERT INTO _tmp_financial_backfill (trade_date, symbol, total_mv, pe_ttm, pb)
        VALUES %s
    """, rows, page_size=10000)
    conn.commit()
    logger.info("Temp table populated successfully")

    # Bulk update using UPDATE FROM
    logger.info("Updating stock_daily_latest from temp table...")
    t0 = time.time()
    cur.execute("""
        UPDATE stock_daily_latest s
        SET total_mv = t.total_mv,
            pe_ttm = t.pe_ttm,
            pb = t.pb
        FROM _tmp_financial_backfill t
        WHERE s.trade_date = t.trade_date
          AND s.symbol = t.symbol
          AND (s.total_mv IS NULL OR s.pe_ttm IS NULL OR s.pb IS NULL)
    """)
    updated = cur.rowcount
    conn.commit()
    elapsed = time.time() - t0
    logger.info(f"Updated {updated} rows in {elapsed:.1f}s")

    # Cleanup
    cur.execute("DROP TABLE IF EXISTS _tmp_financial_backfill")
    conn.commit()
    conn.close()
    logger.info("Done!")

if __name__ == "__main__":
    main()
