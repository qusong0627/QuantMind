#!/usr/bin/env python3
"""Incrementally sync parquet artifacts from remote PostgreSQL.

Flow:
1. Check local parquet coverage.
2. Query remote PostgreSQL coverage.
3. Pull only rows newer than local max(trade_date).
4. Append, dedupe by (trade_date, symbol), and atomically rewrite parquet.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from dotenv import load_dotenv
from sqlalchemy import create_engine, text


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
LOGGER = logging.getLogger("sync_parquets_remote_pg")


@dataclass(frozen=True)
class SyncSpec:
    source_table: str
    target_path: Path
    key_columns: tuple[str, str] = ("trade_date", "symbol")


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
    url = (
        explicit_url
        or os.getenv("SOURCE_DATABASE_URL", "")
        or os.getenv("DATABASE_URL", "")
    ).strip()
    if url:
        return normalize_db_url(url)

    host = os.getenv("DB_HOST", "127.0.0.1")
    port = os.getenv("DB_PORT", "5432")
    user = os.getenv("DB_USER", "postgres")
    password = os.getenv("DB_PASSWORD", "")
    dbname = os.getenv("DB_NAME", "quantmind")
    return f"postgresql://{user}:{quote(password, safe='')}@{host}:{port}/{dbname}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Incrementally sync project parquet files from remote PostgreSQL")
    parser.add_argument("--database-url", default=None, help="Override DATABASE_URL")
    parser.add_argument("--chunk-size", type=int, default=100_000, help="Rows per fetch chunk")
    parser.add_argument("--dry-run", action="store_true", help="Show plan only")
    return parser.parse_args()


def load_target_columns(path: Path) -> list[str]:
    if not path.exists():
        raise FileNotFoundError(f"Target parquet not found: {path}")
    return pq.ParquetFile(path).schema.names


def get_local_coverage(path: Path) -> tuple[int, pd.Timestamp | None, pd.Timestamp | None]:
    if not path.exists():
        return 0, None, None
    frame = pd.read_parquet(path, columns=["trade_date"])
    if frame.empty:
        return 0, None, None
    series = pd.to_datetime(frame["trade_date"])
    return len(frame), series.min(), series.max()


def get_remote_columns(engine, table: str) -> list[str]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = :table
                ORDER BY ordinal_position
                """
            ),
            {"table": table},
        ).fetchall()
    return [row[0] for row in rows]


def get_remote_coverage(engine, table: str) -> tuple[int, pd.Timestamp | None, pd.Timestamp | None]:
    with engine.connect() as conn:
        row = conn.execute(
            text(f"SELECT COUNT(*)::bigint, MIN(trade_date), MAX(trade_date) FROM public.{table}")
        ).fetchone()
    count = int(row[0] or 0)
    min_date = pd.Timestamp(row[1]) if row[1] is not None else None
    max_date = pd.Timestamp(row[2]) if row[2] is not None else None
    return count, min_date, max_date


def fetch_incremental_rows(
    engine,
    table: str,
    selected_columns: list[str],
    local_max_date: pd.Timestamp | None,
    chunk_size: int,
) -> pd.DataFrame:
    column_sql = ", ".join(selected_columns)
    where_sql = ""
    params: dict[str, object] = {}
    if local_max_date is not None:
        where_sql = "WHERE trade_date > :local_max_date"
        params["local_max_date"] = local_max_date.date()
    sql = text(f"SELECT {column_sql} FROM public.{table} {where_sql} ORDER BY trade_date, symbol")

    chunks: list[pd.DataFrame] = []
    with engine.connect().execution_options(stream_results=True) as conn:
        for chunk in pd.read_sql_query(sql, conn, params=params, chunksize=chunk_size):
            if chunk.empty:
                continue
            chunk["trade_date"] = pd.to_datetime(chunk["trade_date"])
            chunks.append(chunk.reindex(columns=selected_columns))
    if not chunks:
        return pd.DataFrame(columns=selected_columns)
    return pd.concat(chunks, ignore_index=True)


def write_merged_parquet(
    target_path: Path,
    incoming: pd.DataFrame,
    key_columns: tuple[str, str],
) -> tuple[int, pd.Timestamp | None, pd.Timestamp | None]:
    target_schema = pq.ParquetFile(target_path).schema_arrow
    temp_path = target_path.with_name(target_path.name + ".tmp")
    if temp_path.exists():
        temp_path.unlink()

    writer = pq.ParquetWriter(temp_path, target_schema, compression="snappy")
    total_rows = 0
    min_date: pd.Timestamp | None = None
    max_date: pd.Timestamp | None = None

    if target_path.exists():
        existing_pf = pq.ParquetFile(target_path)
        total_rows += existing_pf.metadata.num_rows
        if existing_pf.metadata.num_rows > 0:
            date_col = pd.read_parquet(target_path, columns=["trade_date"])["trade_date"]
            if not date_col.empty:
                date_series = pd.to_datetime(date_col)
                min_date = date_series.min()
                max_date = date_series.max()
        for row_group_idx in range(existing_pf.num_row_groups):
            writer.write_table(existing_pf.read_row_group(row_group_idx))

    if not incoming.empty:
        incoming = incoming.reindex(columns=target_schema.names).copy()
        incoming["trade_date"] = pd.to_datetime(incoming["trade_date"])
        incoming_table = pa.Table.from_pandas(incoming, schema=target_schema, preserve_index=False, safe=False)
        writer.write_table(incoming_table)
        total_rows += len(incoming)
        inc_min = incoming["trade_date"].min()
        inc_max = incoming["trade_date"].max()
        min_date = inc_min if min_date is None else min(min_date, inc_min)
        max_date = inc_max if max_date is None else max(max_date, inc_max)

    writer.close()
    os.replace(temp_path, target_path)

    if total_rows == 0:
        return 0, None, None
    return total_rows, min_date, max_date


def metadata_path_for_parquet(target_path: Path) -> Path:
    return target_path.with_suffix(".metadata.json")


def relative_artifact_path(target_path: Path) -> str:
    parts = list(target_path.parts)
    if "db" in parts:
        return Path(*parts[parts.index("db"):]).as_posix()
    try:
        return target_path.relative_to(project_root()).as_posix()
    except ValueError:
        return target_path.name


def load_existing_metadata(metadata_path: Path) -> dict[str, object]:
    if not metadata_path.exists():
        return {}
    return json.loads(metadata_path.read_text(encoding="utf-8"))


def summarize_feature_coverage(df: pd.DataFrame, feature_columns: list[str]) -> dict[str, dict[str, float | int]]:
    row_count = len(df)
    summary: dict[str, dict[str, float | int]] = {}
    for feature_key in feature_columns:
        if feature_key not in df.columns:
            continue
        non_null = int(df[feature_key].notna().sum())
        null_rows = row_count - non_null
        summary[feature_key] = {
            "non_null_rows": non_null,
            "null_rows": null_rows,
            "coverage_ratio": round((non_null / row_count), 6) if row_count else 0.0,
        }
    return summary


def rebuild_feature_snapshot_metadata(target_path: Path) -> None:
    metadata_path = metadata_path_for_parquet(target_path)
    existing = load_existing_metadata(metadata_path)

    df = pd.read_parquet(target_path)
    df["trade_date"] = pd.to_datetime(df["trade_date"])

    auto_feature_columns = df.columns[2:].tolist()
    existing_feature_columns = list(existing.get("feature_columns") or [])
    feature_columns = existing_feature_columns + [
        col for col in auto_feature_columns if col not in existing_feature_columns
    ]
    if not feature_columns:
        feature_columns = auto_feature_columns

    missing_feature_keys = [feature for feature in feature_columns if feature not in df.columns]
    feature_coverage = summarize_feature_coverage(df, feature_columns)
    partial_feature_keys = [
        feature
        for feature, summary in feature_coverage.items()
        if 0 < int(summary["non_null_rows"]) < len(df)
    ]

    year = existing.get("year")
    if year is None:
        suffix = target_path.stem.split("_")[-1]
        year = int(suffix) if suffix.isdigit() else None

    min_trade_date = df["trade_date"].min()
    max_trade_date = df["trade_date"].max()
    lookback_days = existing.get("lookback_days")
    calc_start_date = existing.get("calc_start_date")
    if min_trade_date is not None and lookback_days is not None:
        try:
            calc_start_date = (min_trade_date - pd.Timedelta(days=int(lookback_days))).date().isoformat()
        except Exception:
            calc_start_date = existing.get("calc_start_date")

    payload = {
        **existing,
        "year": year,
        "calc_start_date": calc_start_date,
        "output_start_date": min_trade_date.date().isoformat() if pd.notna(min_trade_date) else None,
        "output_end_date": max_trade_date.date().isoformat() if pd.notna(max_trade_date) else None,
        "row_count": int(len(df)),
        "symbol_count": int(df["symbol"].nunique()) if "symbol" in df.columns else 0,
        "missing_feature_keys": missing_feature_keys,
        "partial_feature_keys": partial_feature_keys,
        "implemented_feature_count": len(feature_columns) - len(missing_feature_keys),
        "feature_coverage": feature_coverage,
        "feature_columns": feature_columns,
        "output_path": relative_artifact_path(target_path),
    }
    metadata_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def sync_one(engine, spec: SyncSpec, chunk_size: int, dry_run: bool) -> None:
    target_columns = load_target_columns(spec.target_path)
    remote_columns = get_remote_columns(engine, spec.source_table)
    selected_columns = [col for col in target_columns if col in remote_columns]
    missing = [col for col in target_columns if col not in remote_columns]
    extra = [col for col in remote_columns if col not in target_columns]
    if missing:
        raise RuntimeError(f"{spec.source_table} missing target columns: {missing}")

    local_rows, local_min, local_max = get_local_coverage(spec.target_path)
    remote_rows, remote_min, remote_max = get_remote_coverage(engine, spec.source_table)

    LOGGER.info(
        "%s local rows=%s range=[%s, %s] | remote rows=%s range=[%s, %s]",
        spec.source_table,
        local_rows,
        local_min.date() if local_min is not None else None,
        local_max.date() if local_max is not None else None,
        remote_rows,
        remote_min.date() if remote_min is not None else None,
        remote_max.date() if remote_max is not None else None,
    )
    if extra:
        LOGGER.info("%s source-only columns skipped: %s", spec.source_table, extra)

    if remote_max is None:
        LOGGER.warning("%s has no remote data, skipped", spec.source_table)
        return
    if local_max is not None and remote_max <= local_max:
        LOGGER.info("%s already up to date", spec.source_table)
        return

    incoming = fetch_incremental_rows(engine, spec.source_table, selected_columns, local_max, chunk_size)
    LOGGER.info("%s incremental rows fetched=%s", spec.source_table, len(incoming))
    if dry_run:
        return

    total_rows, min_date, max_date = write_merged_parquet(spec.target_path, incoming, spec.key_columns)
    LOGGER.info(
        "%s synced rows=%s range=[%s, %s]",
        spec.source_table,
        total_rows,
        min_date.date() if min_date is not None else None,
        max_date.date() if max_date is not None else None,
    )
    if spec.target_path.parent.name == "feature_snapshots":
        rebuild_feature_snapshot_metadata(spec.target_path)
        LOGGER.info("%s metadata sidecar refreshed", spec.target_path.name)


def main() -> None:
    args = parse_args()
    root = project_root()
    load_dotenv(root / ".env", override=True)

    engine = create_engine(get_database_url(args.database_url))
    specs = [
        SyncSpec("fundamental_aligned", root / "db" / "custom" / "fundamental_aligned.parquet"),
        SyncSpec("feature_snapshots", root / "db" / "feature_snapshots" / "model_features_2026.parquet"),
    ]
    for spec in specs:
        sync_one(engine, spec, chunk_size=args.chunk_size, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
