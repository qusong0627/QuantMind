#!/usr/bin/env python3
"""Backfill stock_daily_latest.consecutive_limit_up_days from limit_up_today."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from urllib.parse import quote

import psycopg2
from dotenv import load_dotenv


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
LOGGER = logging.getLogger("backfill_consecutive_limit_up_days")


def project_root() -> Path:
    return Path(__file__).resolve().parents[3]


ROOT = project_root()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


from backend.shared.stock_utils import StockCodeUtil


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
    if explicit_url:
        return normalize_db_url(explicit_url)

    host = os.getenv("DB_HOST", "").strip()
    port = os.getenv("DB_PORT", "5432").strip() or "5432"
    user = os.getenv("DB_USER", "").strip()
    password = os.getenv("DB_PASSWORD", "")
    dbname = os.getenv("DB_NAME", "").strip() or "quantmind"
    if host and user:
        return f"postgresql://{user}:{quote(password, safe='')}@{host}:{port}/{dbname}"

    url = os.getenv("DATABASE_URL", "").strip()
    if url:
        return normalize_db_url(url)
    host = "127.0.0.1"
    port = "5432"
    user = "quantmind"
    password = ""
    dbname = "quantmind"
    return f"postgresql://{user}:{quote(password, safe='')}@{host}:{port}/{dbname}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill stock_daily_latest.consecutive_limit_up_days")
    parser.add_argument("--database-url", default=None, help="Override DATABASE_URL")
    parser.add_argument("--start-date", default=None, help="Only update rows on/after this trade_date (YYYY-MM-DD)")
    parser.add_argument("--end-date", default=None, help="Only update rows on/before this trade_date (YYYY-MM-DD)")
    parser.add_argument("--symbols", nargs="*", default=None, help="Optional symbol filters, supports prefix or suffix codes")
    parser.add_argument("--sample-limit", type=int, default=20, help="Preview mismatched rows count")
    parser.add_argument("--apply", action="store_true", help="Persist updates, otherwise dry-run")
    return parser.parse_args()


def normalize_symbols(raw_symbols: list[str] | None) -> list[str]:
    if not raw_symbols:
        return []
    normalized = []
    for raw_symbol in raw_symbols:
        symbol = StockCodeUtil.to_prefix(str(raw_symbol or "").strip())
        if symbol:
            normalized.append(symbol)
    return sorted(set(normalized))


def build_scope_predicate(args: argparse.Namespace, normalized_symbols: list[str]) -> tuple[str, dict[str, object]]:
    clauses: list[str] = []
    params: dict[str, object] = {}
    if args.start_date:
        clauses.append("trade_date >= %(start_date)s")
        params["start_date"] = args.start_date
    if args.end_date:
        clauses.append("trade_date <= %(end_date)s")
        params["end_date"] = args.end_date
    if normalized_symbols:
        clauses.append("symbol = ANY(%(symbols)s)")
        params["symbols"] = normalized_symbols
    return (" AND ".join(clauses) if clauses else "TRUE"), params


def computed_days_cte(symbol_scope_sql: str) -> str:
    return f"""
        WITH ordered AS (
            SELECT
                trade_date,
                symbol,
                COALESCE(limit_up_today, 0) AS limit_up_today,
                SUM(CASE WHEN COALESCE(limit_up_today, 0) = 0 THEN 1 ELSE 0 END)
                    OVER (PARTITION BY symbol ORDER BY trade_date) AS grp
            FROM public.stock_daily_latest
            WHERE {symbol_scope_sql}
        ),
        calc AS (
            SELECT
                trade_date,
                symbol,
                CASE
                    WHEN limit_up_today = 1 THEN SUM(CASE WHEN limit_up_today = 1 THEN 1 ELSE 0 END)
                        OVER (PARTITION BY symbol, grp ORDER BY trade_date)
                    ELSE 0
                END AS computed_days
            FROM ordered
        )
    """


def preview_backfill(conn, scope_sql: str, scope_params: dict[str, object], sample_limit: int) -> int:
    preview_sql = f"""
        {computed_days_cte('TRUE' if 'symbols' not in scope_params else 'symbol = ANY(%(symbols)s)')}
        SELECT
            COUNT(*) FILTER (WHERE COALESCE(s.consecutive_limit_up_days, 0) IS DISTINCT FROM c.computed_days) AS mismatched_rows,
            COUNT(*) FILTER (WHERE c.computed_days > 0) AS computed_gt_zero,
            MAX(c.computed_days) AS computed_max_days
        FROM public.stock_daily_latest s
        JOIN calc c USING (trade_date, symbol)
        WHERE {scope_sql}
    """
    with conn.cursor() as cursor:
        cursor.execute(preview_sql, scope_params)
        mismatched_rows, computed_gt_zero, computed_max_days = cursor.fetchone()
        LOGGER.info(
            "Preview: mismatched_rows=%s computed_gt_zero=%s computed_max_days=%s",
            mismatched_rows,
            computed_gt_zero,
            computed_max_days,
        )

        sample_sql = f"""
            {computed_days_cte('TRUE' if 'symbols' not in scope_params else 'symbol = ANY(%(symbols)s)')}
            SELECT
                s.trade_date,
                s.symbol,
                COALESCE(s.limit_up_today, 0) AS limit_up_today,
                COALESCE(s.consecutive_limit_up_days, 0) AS current_days,
                c.computed_days
            FROM public.stock_daily_latest s
            JOIN calc c USING (trade_date, symbol)
            WHERE {scope_sql}
              AND COALESCE(s.consecutive_limit_up_days, 0) IS DISTINCT FROM c.computed_days
            ORDER BY s.symbol, s.trade_date
            LIMIT %(sample_limit)s
        """
        sample_params = dict(scope_params)
        sample_params["sample_limit"] = sample_limit
        cursor.execute(sample_sql, sample_params)
        rows = cursor.fetchall()
    for trade_date, symbol, limit_up_today, current_days, computed_days in rows:
        LOGGER.info(
            "Sample mismatch trade_date=%s symbol=%s limit_up_today=%s current=%s computed=%s",
            trade_date,
            symbol,
            limit_up_today,
            current_days,
            computed_days,
        )
    return int(mismatched_rows or 0)


def apply_backfill(conn, scope_sql: str, scope_params: dict[str, object]) -> int:
    update_sql = f"""
        {computed_days_cte('TRUE' if 'symbols' not in scope_params else 'symbol = ANY(%(symbols)s)')}
        UPDATE public.stock_daily_latest AS s
        SET consecutive_limit_up_days = c.computed_days
        FROM calc AS c
        WHERE s.trade_date = c.trade_date
          AND s.symbol = c.symbol
          AND {scope_sql}
          AND COALESCE(s.consecutive_limit_up_days, 0) IS DISTINCT FROM c.computed_days
    """
    with conn.cursor() as cursor:
        cursor.execute(update_sql, scope_params)
        updated_rows = cursor.rowcount
    conn.commit()
    return int(updated_rows or 0)


def main() -> None:
    args = parse_args()
    load_dotenv(ROOT / ".env", override=True)
    normalized_symbols = normalize_symbols(args.symbols)
    if args.symbols and not normalized_symbols:
        raise SystemExit("No valid symbols provided")

    db_url = get_database_url(args.database_url)
    scope_sql, scope_params = build_scope_predicate(args, normalized_symbols)

    LOGGER.info(
        "Start backfill scope: start_date=%s end_date=%s symbols=%s apply=%s",
        args.start_date,
        args.end_date,
        normalized_symbols or "ALL",
        args.apply,
    )

    conn = psycopg2.connect(db_url)
    try:
        mismatched_rows = preview_backfill(conn, scope_sql, scope_params, args.sample_limit)
        if not args.apply:
            LOGGER.info("Dry-run complete, no data updated")
            return
        if mismatched_rows == 0:
            LOGGER.info("No mismatched rows, skip update")
            return

        updated_rows = apply_backfill(conn, scope_sql, scope_params)
        LOGGER.info("Backfill complete, updated_rows=%s", updated_rows)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
