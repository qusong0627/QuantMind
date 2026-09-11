#!/usr/bin/env python3
"""Full sync of stock_daily_latest from QuantDB.

Updates all columns QuantDB can provide (features_daily 技术+估值 + 未复权K线
+ roe + industry), focusing on recent dates to keep the operation fast.
QuantDB 不产 is_st/idx_*/concept_*/涨停统计 等列, 这些列不参与同步。
"""
import os
import sys
import time
import logging
from datetime import date, timedelta

import pandas as pd
import psycopg2
from psycopg2.extras import execute_values

# 保证 backend 包可导入（admin 端点以 /app 为 cwd 调用本脚本）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("sync_stock_daily_full")

# 只同步最近 N 个交易日的数据（默认 30，覆盖近一个月）
MAX_DAYS = int(os.getenv("SYNC_MAX_DAYS", "30"))


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
    start = date.today() - timedelta(days=MAX_DAYS * 3)
    end = date.today()
    logger.info("Reading QuantDB frame [%s, %s]...", start, end)
    t0 = time.time()
    df = load_quantdb_frame(hub, start, end)
    logger.info(f"QuantDB frame loaded in {time.time()-t0:.1f}s, total rows: {len(df):,}")

    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date

    # 获取目标表列类型映射
    cur.execute("""
        SELECT column_name, data_type FROM information_schema.columns
        WHERE table_schema='public' AND table_name='stock_daily_latest'
        ORDER BY ordinal_position
    """)
    col_type_map = {r[0]: r[1] for r in cur.fetchall()}
    logger.info(f"Table has {len(col_type_map)} columns")

    # 取 QuantDB 和 table 的交集列（排除主键）
    parquet_cols = set(df.columns)
    sync_cols = sorted(parquet_cols & set(col_type_map.keys()) - {"trade_date", "symbol"})
    logger.info(f"Columns to sync ({len(sync_cols)}): {', '.join(sync_cols[:10])}...")

    # 只保留最近 N 个交易日的数据
    unique_dates = sorted(df["trade_date"].dropna().unique(), reverse=True)
    target_dates = set(unique_dates[:MAX_DAYS])
    logger.info(f"Syncing {len(target_dates)} most recent trade dates: {min(target_dates)} to {max(target_dates)}")

    df = df[df["trade_date"].isin(target_dates)].copy()
    logger.info(f"Filtered to {len(df):,} rows for target dates")

    # 去重（trade_date + symbol）
    df = df.drop_duplicates(subset=["trade_date", "symbol"], keep="last")

    # 创建 temp table
    col_defs = ["trade_date date", "symbol text"]
    for c in sync_cols:
        pg_type = col_type_map.get(c, "text")
        col_defs.append(f"{c} {pg_type}")

    cur.execute("DROP TABLE IF EXISTS _tmp_sdl_full_sync")
    cur.execute(f"CREATE TEMP TABLE _tmp_sdl_full_sync ({', '.join(col_defs)})")
    conn.commit()

    # 准备数据 - 按列类型做转换
    bool_cols = {c for c in sync_cols if col_type_map.get(c) == "boolean"}
    all_cols = ["trade_date", "symbol"] + sync_cols
    records = []
    for row in df[all_cols].itertuples(index=False):
        vals = []
        for i, v in enumerate(row):
            col_name = all_cols[i]
            if pd.isna(v):
                vals.append(None)
            elif col_name in bool_cols:
                vals.append(bool(v))
            else:
                vals.append(v)
        records.append(tuple(vals))

    logger.info(f"Inserting {len(records):,} rows into temp table...")
    t1 = time.time()
    execute_values(cur, f"""
        INSERT INTO _tmp_sdl_full_sync ({', '.join(all_cols)})
        VALUES %s
    """, records, page_size=10000)
    conn.commit()
    logger.info(f"Temp table populated in {time.time()-t1:.1f}s")

    # UPSERT: 已存在的行 UPDATE，不存在的行 INSERT
    non_pk = sync_cols
    update_set = ", ".join([f"{c}=EXCLUDED.{c}" for c in non_pk])
    insert_cols = ", ".join(all_cols)

    logger.info("Upserting into stock_daily_latest...")
    t2 = time.time()
    cur.execute(f"""
        INSERT INTO stock_daily_latest ({insert_cols})
        SELECT {insert_cols} FROM _tmp_sdl_full_sync
        ON CONFLICT (trade_date, symbol)
        DO UPDATE SET {update_set}
    """)
    affected = cur.rowcount
    conn.commit()
    logger.info(f"Upsert completed: {affected:,} rows affected in {time.time()-t2:.1f}s")

    # Cleanup
    cur.execute("DROP TABLE IF EXISTS _tmp_sdl_full_sync")
    conn.commit()

    # 验证结果
    logger.info("Verifying results on latest trade date...")
    cur.execute("""
        SELECT trade_date, COUNT(*) as total,
               COUNT(is_st) as is_st_nn,
               SUM(CASE WHEN is_st=1 THEN 1 ELSE 0 END) as st_count,
               COUNT(idx_hs300) as hs300_nn,
               SUM(CASE WHEN idx_hs300=1 THEN 1 ELSE 0 END) as hs300_count,
               COUNT(idx_zz1000) as zz1000_nn,
               SUM(CASE WHEN idx_zz1000=1 THEN 1 ELSE 0 END) as zz1000_count,
               COUNT(idx_margin) as margin_nn,
               SUM(CASE WHEN idx_margin=1 THEN 1 ELSE 0 END) as margin_count
        FROM stock_daily_latest
        WHERE trade_date IN (
            SELECT trade_date FROM stock_daily_latest
            GROUP BY trade_date HAVING COUNT(total_mv) > 0
            ORDER BY trade_date DESC LIMIT 3
        )
        GROUP BY trade_date
        ORDER BY trade_date DESC
    """)
    for row in cur.fetchall():
        logger.info(f"  {row[0]}: total={row[1]}, st={row[3]}, hs300={row[5]}, zz1000={row[7]}, margin={row[9]}")

    conn.close()
    logger.info("Done!")

if __name__ == "__main__":
    main()
