#!/usr/bin/env python3
"""Incrementally upsert stock_daily_latest from db/custom/fundamental_aligned.parquet."""

from __future__ import annotations

import argparse
import logging
import math
import os
from pathlib import Path
from urllib.parse import quote

import pandas as pd
import psycopg2
from psycopg2.extras import execute_values
from dotenv import load_dotenv
from sqlalchemy import create_engine, text


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
LOGGER = logging.getLogger("sync_stock_daily_latest")

INT_COLUMNS = {
    "listed_days",
    "is_st",
    "consecutive_limit_up_days",
    "limit_up_today",
    "limit_down_today",
    "micro_jump_flag",
    "idx_all",
    "idx_hs300",
    "idx_zz1000",
    "idx_margin",
    "idx_chinext",
    "concept_ai",
    "concept_chip",
    "concept_new_energy",
    "concept_pv",
    "concept_military",
    "concept_medical",
    "concept_fintech",
    "concept_consumption",
    "concept_state_owned",
    "concept_lithium",
}


def project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def normalize_db_url(raw: str) -> str:
    url = raw.strip()
    if url.startswith("postgresql+asyncpg://"):
        url = url.replace("postgresql+asyncpg://", "postgresql://", 1)
    scheme, sep, rest = url.partition("://")
    if not sep or "@" not in rest:
        return url
    auth, host = rest.rsplit("@", 1)
    if ":" not in auth:
        return url
    user, password = auth.split(":", 1)
    return f"{scheme}://{user}:{quote(password, safe='')}@{host}"


def get_database_url(explicit_url: str | None = None) -> str:
    url = (explicit_url or os.getenv("DATABASE_URL", "")).strip()
    if url:
        return normalize_db_url(url)
    host = os.getenv("DB_HOST", "127.0.0.1")
    port = os.getenv("DB_PORT", "5432")
    user = os.getenv("DB_USER", "postgres")
    password = os.getenv("DB_PASSWORD", "")
    dbname = os.getenv("DB_NAME", "quantmind")
    return f"postgresql://{user}:{quote(password, safe='')}@{host}:{port}/{dbname}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Incrementally sync stock_daily_latest from parquet")
    parser.add_argument("--database-url", default=None, help="Override DATABASE_URL")
    parser.add_argument(
        "--parquet-path",
        default=str(project_root() / "db" / "custom" / "fundamental_aligned.parquet"),
        help="Source parquet path",
    )
    parser.add_argument("--batch-size", type=int, default=2000, help="Upsert batch size")
    parser.add_argument("--dry-run", action="store_true", help="Preview only")
    return parser.parse_args()


def get_table_coverage(engine) -> tuple[int, pd.Timestamp | None, pd.Timestamp | None]:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT COUNT(*)::bigint, MIN(trade_date), MAX(trade_date) FROM public.stock_daily_latest")
        ).fetchone()
    return int(row[0] or 0), pd.Timestamp(row[1]) if row[1] is not None else None, pd.Timestamp(row[2]) if row[2] is not None else None


def get_table_columns(engine) -> list[str]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema='public' AND table_name='stock_daily_latest'
                ORDER BY ordinal_position
                """
            )
        ).fetchall()
    return [row[0] for row in rows]


def load_incremental_frame(parquet_path: Path, local_max_date: pd.Timestamp | None) -> pd.DataFrame:
    frame = pd.read_parquet(parquet_path)
    frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    if local_max_date is not None:
        frame = frame[frame["trade_date"] > local_max_date].copy()
    return frame.sort_values(["trade_date", "symbol"]).reset_index(drop=True)


def normalize_frame(frame: pd.DataFrame, target_columns: list[str]) -> tuple[pd.DataFrame, list[str]]:
    common_columns = [col for col in target_columns if col in frame.columns]
    skipped_columns = [col for col in target_columns if col not in frame.columns]
    normalized = frame.reindex(columns=common_columns).copy()

    for col in common_columns:
        if col == "trade_date":
            normalized[col] = pd.to_datetime(normalized[col]).dt.date
            continue
        if col in INT_COLUMNS:
            normalized[col] = normalized[col].apply(
                lambda v: None if pd.isna(v) else int(v)
            )
            continue
        normalized[col] = normalized[col].apply(
            lambda v: None if pd.isna(v) else (float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else v)
        )

    return normalized, skipped_columns


def to_rows(frame: pd.DataFrame, columns: list[str]) -> list[tuple]:
    rows: list[tuple] = []
    for record in frame[columns].itertuples(index=False, name=None):
        cleaned = []
        for value in record:
            if isinstance(value, float) and math.isnan(value):
                cleaned.append(None)
            else:
                cleaned.append(value)
        rows.append(tuple(cleaned))
    return rows


def upsert_rows(db_url: str, columns: list[str], rows: list[tuple], batch_size: int) -> None:
    update_columns = [col for col in columns if col not in {"trade_date", "symbol"}]
    sql = f"""
        INSERT INTO public.stock_daily_latest ({", ".join(columns)})
        VALUES %s
        ON CONFLICT (trade_date, symbol) DO UPDATE SET
        {", ".join(f"{col}=EXCLUDED.{col}" for col in update_columns)}
    """
    conn = psycopg2.connect(db_url)
    try:
        with conn.cursor() as cur:
            for i in range(0, len(rows), batch_size):
                batch = rows[i:i + batch_size]
                execute_values(cur, sql, batch, page_size=batch_size)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main() -> None:
    args = parse_args()
    root = project_root()
    load_dotenv(root / ".env", override=True)

    parquet_path = Path(args.parquet_path).expanduser().resolve()
    db_url = get_database_url(args.database_url)
    engine = create_engine(db_url)

    table_columns = get_table_columns(engine)
    local_rows, local_min, local_max = get_table_coverage(engine)
    LOGGER.info(
        "stock_daily_latest current rows=%s range=[%s, %s]",
        local_rows,
        local_min.date() if local_min is not None else None,
        local_max.date() if local_max is not None else None,
    )

    incoming = load_incremental_frame(parquet_path, local_max)
    if incoming.empty:
        LOGGER.info("No parquet rows newer than local max trade_date")
        return

    LOGGER.info(
        "parquet incremental rows=%s dates=%s..%s",
        len(incoming),
        incoming["trade_date"].min().date(),
        incoming["trade_date"].max().date(),
    )

    normalized, skipped_columns = normalize_frame(incoming, table_columns)
    if skipped_columns:
        LOGGER.info("table columns not present in parquet, leaving untouched/default: %s", skipped_columns)

    if args.dry_run:
        return

    rows = to_rows(normalized, list(normalized.columns))
    upsert_rows(db_url, list(normalized.columns), rows, args.batch_size)

    local_rows, local_min, local_max = get_table_coverage(engine)
    LOGGER.info(
        "stock_daily_latest synced rows=%s range=[%s, %s]",
        local_rows,
        local_min.date() if local_min is not None else None,
        local_max.date() if local_max is not None else None,
    )


if __name__ == "__main__":
    main()
