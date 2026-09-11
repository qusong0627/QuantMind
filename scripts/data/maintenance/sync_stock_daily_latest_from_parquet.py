#!/usr/bin/env python3
"""Incrementally upsert stock_daily_latest from QuantDB (quantdb_hub).

数据源: QuantDB features_daily(技术+估值) + 未复权 K 线 + 3_financial_data(roe)
      + instrument_detail(industry)。
注意: QuantDB 不产 is_st/idx_*/concept_*/涨停统计/listed_days 等列, 这些列不再由
本脚本写入 (stock_daily_latest 相应列保持原值/NULL)。
"""

from __future__ import annotations

import argparse
import logging
import math
import os
from datetime import date, timedelta
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
    parser = argparse.ArgumentParser(description="Incrementally sync stock_daily_latest from QuantDB")
    parser.add_argument("--database-url", default=None, help="Override DATABASE_URL")
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


def _rename_date_symbol(df: pd.DataFrame) -> pd.DataFrame:
    """统一 QuantDB 各视图的 symbol / 交易日列名。"""
    sym_col = next((c for c in ("symbol", "Symbol") if c in df.columns), None)
    date_col = next((c for c in ("time", "trade_date") if c in df.columns), None)
    renamed = {}
    if sym_col and sym_col != "symbol":
        renamed[sym_col] = "symbol"
    if date_col and date_col != "trade_date":
        renamed[date_col] = "trade_date"
    if renamed:
        df = df.rename(columns=renamed)
    return df


def _latest_roe(hub, symbol: str) -> float | None:
    """从 3_financial_data/pershare_index 取每股最近报告期 roe (net_roe 优先)。"""
    try:
        f = hub.fetch_financial(symbol, "pershare_index")
        for col in ("net_roe", "total_roe", "equity_roe", "roe_dupont"):
            if col in f.columns:
                vals = pd.to_numeric(f[col], errors="coerce").dropna()
                if not vals.empty:
                    return float(vals.iloc[-1])
    except Exception:
        pass
    return None


def load_quantdb_frame(hub, start_date, end_date) -> pd.DataFrame:
    """从 QuantDB 构建待同步宽表 (features_daily 技术+估值 + 未复权K线 + roe/industry)。"""
    frames: list[pd.DataFrame] = []

    fd = hub.fetch_features_daily(start=start_date, end=end_date)
    if not fd.empty:
        fd = _rename_date_symbol(fd)
        frames.append(fd)

    # features_daily 无 open/high/low/volume/amount, 用未复权 K 线补 (先复权再同步)
    symbols = sorted(fd["symbol"].unique().tolist()) if not fd.empty else []
    if symbols:
        try:
            kl = hub.fetch_daily_kline_batch(symbols, start_date, end_date, adjust="none")
            if not kl.empty:
                kl = _rename_date_symbol(kl)
                frames.append(kl)
        except Exception as exc:
            LOGGER.warning("QuantDB kline fetch failed: %s", exc)

    if not frames:
        return pd.DataFrame()
    frame = frames[0]
    for extra in frames[1:]:
        keys = [c for c in ("symbol", "trade_date") if c in frame.columns and c in extra.columns]
        frame = frame.merge(extra, on=keys, how="outer", suffixes=("", "_k"))

    if "symbol" in frame.columns:
        symbols_all = sorted(frame["symbol"].unique().tolist())
        try:
            roe = {s: _latest_roe(hub, s) for s in symbols_all}
            if any(v is not None for v in roe.values()):
                frame["roe"] = frame["symbol"].map(roe)
            ind = hub.fetch_instrument_industry()
            if not ind.empty and "symbol" in ind.columns and "ind_name_l1" in ind.columns:
                ind_map = dict(zip(ind["symbol"], ind["ind_name_l1"], strict=False))
                frame["industry"] = frame["symbol"].map(ind_map)
        except Exception as exc:
            LOGGER.warning("QuantDB roe/industry attach failed: %s", exc)

    if "trade_date" in frame.columns:
        frame["trade_date"] = pd.to_datetime(frame["trade_date"])
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


def invalidate_data_status_cache() -> bool:
    """清除 Redis 中的数据状态缓存，确保前端获取最新数据。"""
    try:
        from backend.shared.redis_sentinel_client import get_redis_sentinel_client
        redis = get_redis_sentinel_client()
        redis.delete("qm:admin:data_status")
        LOGGER.info("Redis cache invalidated: qm:admin:data_status")
        return True
    except Exception as e:
        LOGGER.warning("Failed to invalidate Redis cache: %s", e)
        return False


def main() -> None:
    args = parse_args()
    root = project_root()
    load_dotenv(root / ".env", override=True)

    db_url = get_database_url(args.database_url)
    engine = create_engine(db_url)

    from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub
    hub = QuantDBDataHub.get_instance()
    if not hub.available:
        LOGGER.error("QuantDB data not available; abort sync")
        return

    table_columns = get_table_columns(engine)
    local_rows, local_min, local_max = get_table_coverage(engine)
    LOGGER.info(
        "stock_daily_latest current rows=%s range=[%s, %s]",
        local_rows,
        local_min.date() if local_min is not None else None,
        local_max.date() if local_max is not None else None,
    )

    # 增量范围: local_max 次日 → 今天; 空表则回看一年
    start_date = (local_max + pd.Timedelta(days=1)).date() if local_max is not None else date.today() - timedelta(days=365)
    end_date = date.today()
    incoming = load_quantdb_frame(hub, start_date, end_date)
    if incoming.empty:
        LOGGER.info("No QuantDB rows in [%s, %s]", start_date, end_date)
        return

    LOGGER.info(
        "QuantDB incremental rows=%s dates=%s..%s",
        len(incoming),
        incoming["trade_date"].min().date(),
        incoming["trade_date"].max().date(),
    )

    normalized, skipped_columns = normalize_frame(incoming, table_columns)
    if skipped_columns:
        LOGGER.info("table columns not present in QuantDB, leaving untouched/default: %s", skipped_columns)

    if args.dry_run:
        return

    rows = to_rows(normalized, list(normalized.columns))
    upsert_rows(db_url, list(normalized.columns), rows, args.batch_size)

    # 同步完成后清除 Redis 缓存
    invalidate_data_status_cache()

    local_rows, local_min, local_max = get_table_coverage(engine)
    LOGGER.info(
        "stock_daily_latest synced rows=%s range=[%s, %s]",
        local_rows,
        local_min.date() if local_min is not None else None,
        local_max.date() if local_max is not None else None,
    )


if __name__ == "__main__":
    main()
