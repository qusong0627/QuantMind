#!/usr/bin/env python3
"""Sync index OHLCV from local qlib features into public.index_ohlcv_daily."""

from __future__ import annotations

import argparse
import logging
import math
import os
import struct
from pathlib import Path
from urllib.parse import quote

import pandas as pd
import psycopg2
from dotenv import load_dotenv
from psycopg2.extras import execute_values
from sqlalchemy import create_engine, text


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
LOGGER = logging.getLogger("sync_index_ohlcv")

INDEX_CONFIG = {
    "SH000300": {"name": "沪深300", "dirs": ("idx_sh000300", "sh000300")},
    "SH000905": {"name": "中证500", "dirs": ("idx_sh000905", "sh000905")},
    "SH000852": {"name": "中证1000", "dirs": ("idx_sh000852", "sh000852")},
}
FIELDS = ("open", "high", "low", "close", "volume")


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
    parser = argparse.ArgumentParser(description="Sync index OHLCV from qlib features into PostgreSQL")
    parser.add_argument("--database-url", default=None, help="Override DATABASE_URL")
    parser.add_argument(
        "--qlib-dir",
        default=str(project_root() / "db" / "qlib_data"),
        help="Qlib data directory",
    )
    parser.add_argument("--batch-size", type=int, default=2000, help="Upsert batch size")
    parser.add_argument("--dry-run", action="store_true", help="Preview only")
    return parser.parse_args()


def load_calendar(qlib_dir: Path) -> list[pd.Timestamp]:
    cal_path = qlib_dir / "calendars" / "day.txt"
    if not cal_path.exists():
        raise FileNotFoundError(f"Qlib calendar not found: {cal_path}")
    dates = [line.strip() for line in cal_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [pd.Timestamp(x) for x in dates]


def read_bin(bin_path: Path, calendar: list[pd.Timestamp]) -> pd.Series:
    if not bin_path.exists():
        return pd.Series(dtype="float64")
    raw = bin_path.read_bytes()
    if len(raw) < 4:
        return pd.Series(dtype="float64")
    start_idx = struct.unpack("<I", raw[:4])[0]
    arr = pd.Series(pd.array(struct.unpack(f"<{(len(raw) - 4) // 4}f", raw[4:]), dtype="Float32")).astype("float64")
    # 如果 start_idx 异常（大于 calendar 长度），则从 0 开始读取
    # 这通常发生在指数数据的 bin 文件中
    if start_idx >= len(calendar):
        LOGGER.debug("Abnormal start_idx=%d for %s, resetting to 0", start_idx, bin_path)
        start_idx = 0
    if start_idx >= len(calendar) or arr.empty:
        return pd.Series(dtype="float64")
    end_idx = min(start_idx + len(arr), len(calendar))
    n = max(0, end_idx - start_idx)
    if n == 0:
        return pd.Series(dtype="float64")
    idx = pd.to_datetime(calendar[start_idx:end_idx])
    return pd.Series(arr.iloc[:n].values, index=idx)


def build_index_frame(qlib_dir: Path) -> pd.DataFrame:
    calendar = load_calendar(qlib_dir)
    features_root = qlib_dir / "features"

    frames: list[pd.DataFrame] = []
    for symbol, meta in INDEX_CONFIG.items():
        inst_dir = None
        for d in meta["dirs"]:
            candidate = features_root / d
            if candidate.exists():
                inst_dir = candidate
                break
        if inst_dir is None:
            LOGGER.warning("Missing qlib index dir for %s, tried dirs=%s", symbol, list(meta["dirs"]))
            continue

        field_series: dict[str, pd.Series] = {}
        for field in FIELDS:
            field_series[field] = read_bin(inst_dir / f"{field}.day.bin", calendar)

        close_series = field_series["close"]
        if close_series.empty:
            LOGGER.warning("No close series for %s", symbol)
            continue

        df = pd.DataFrame(index=close_series.index)
        for field in FIELDS:
            df[field] = field_series[field].reindex(df.index)

        # qlib volume 常用“手”口径，这里统一转回“股”写入业务库。
        df["volume"] = pd.to_numeric(df["volume"], errors="coerce") * 100.0
        df["amount"] = pd.to_numeric(df["close"], errors="coerce") * pd.to_numeric(df["volume"], errors="coerce")
        df["pct_change"] = pd.to_numeric(df["close"], errors="coerce").pct_change() * 100.0
        df["symbol"] = symbol
        df["index_name"] = meta["name"]
        df["source"] = "qlib_features"
        df["trade_date"] = pd.to_datetime(df.index).date
        frames.append(df.reset_index(drop=True))

    if not frames:
        return pd.DataFrame(columns=["trade_date", "symbol"])
    out = pd.concat(frames, ignore_index=True)
    out = out.dropna(subset=["trade_date", "symbol", "close"]).copy()
    out = out.sort_values(["trade_date", "symbol"]).reset_index(drop=True)
    return out


def ensure_table(engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS public.index_ohlcv_daily (
                    id BIGSERIAL PRIMARY KEY,
                    trade_date DATE NOT NULL,
                    symbol VARCHAR(16) NOT NULL,
                    index_name VARCHAR(64),
                    open DOUBLE PRECISION,
                    high DOUBLE PRECISION,
                    low DOUBLE PRECISION,
                    close DOUBLE PRECISION,
                    volume DOUBLE PRECISION,
                    amount DOUBLE PRECISION,
                    pct_change DOUBLE PRECISION,
                    source VARCHAR(64),
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW(),
                    UNIQUE (trade_date, symbol)
                );
                CREATE INDEX IF NOT EXISTS idx_index_ohlcv_daily_symbol_date
                    ON public.index_ohlcv_daily(symbol, trade_date);
                CREATE INDEX IF NOT EXISTS idx_index_ohlcv_daily_trade_date
                    ON public.index_ohlcv_daily(trade_date);
                """
            )
        )


def get_table_columns(engine) -> list[str]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema='public' AND table_name='index_ohlcv_daily'
                ORDER BY ordinal_position
                """
            )
        ).fetchall()
    return [x[0] for x in rows]


def normalize_value(value):
    if pd.isna(value):
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    return value


def upsert_rows(db_url: str, columns: list[str], rows: list[tuple], batch_size: int) -> int:
    update_columns = [col for col in columns if col not in {"trade_date", "symbol"}]
    sql = f"""
        INSERT INTO public.index_ohlcv_daily ({", ".join(columns)})
        VALUES %s
        ON CONFLICT (trade_date, symbol) DO UPDATE SET
        {", ".join(f"{col}=EXCLUDED.{col}" for col in update_columns)},
        updated_at = NOW()
    """
    affected = 0
    conn = psycopg2.connect(db_url)
    try:
        with conn.cursor() as cur:
            for i in range(0, len(rows), batch_size):
                batch = rows[i : i + batch_size]
                execute_values(cur, sql, batch, page_size=batch_size)
                affected += len(batch)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return affected


def main() -> None:
    args = parse_args()
    root = project_root()
    load_dotenv(root / ".env", override=True)

    qlib_dir = Path(args.qlib_dir).expanduser().resolve()
    db_url = get_database_url(args.database_url)
    engine = create_engine(db_url)

    ensure_table(engine)
    table_columns = get_table_columns(engine)

    frame = build_index_frame(qlib_dir)
    if frame.empty:
        LOGGER.info("No index OHLCV rows found in qlib features")
        return

    insert_columns = [c for c in table_columns if c in frame.columns and c not in {"id", "created_at", "updated_at"}]
    payload = frame[insert_columns].copy()
    rows = [tuple(normalize_value(v) for v in row) for row in payload.itertuples(index=False, name=None)]

    LOGGER.info(
        "Prepared index rows=%s date_range=[%s, %s] symbols=%s",
        len(payload),
        payload["trade_date"].min(),
        payload["trade_date"].max(),
        sorted(payload["symbol"].dropna().unique().tolist()),
    )
    if args.dry_run:
        return

    affected = upsert_rows(db_url, insert_columns, rows, args.batch_size)
    LOGGER.info("index_ohlcv_daily upsert done, affected rows=%s", affected)


if __name__ == "__main__":
    main()
