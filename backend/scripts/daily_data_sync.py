#!/usr/bin/env python3
"""
统一日常数据同步脚本
====================

数据源优先级（A 股日线）：
  1. QuantDB parquet (data/quantdb/) — 最权威，T-1 左右
  2. baostock — 稳定，T-1
  3. akshare (stock_zh_a_daily) — T-1，新浪源
  4. eltdx — T（当天），通达信

流程：
  1. 查 PG: 每只股票的 MAX(trade_date)
  2. PG 最新 < QuantDB parquet 最新 → 从 parquet 读增量写入 PG
  3. PG 最新 < 今天 → baostock 补齐 → akshare 兜底
  4. 需要当日 → eltdx 获取
  5. 增量更新 Qlib 缓存 (从 parquet 生成)
  6. 计算 MA/收益率等技术指标

用法：
  # 增量同步（默认，只补缺失天数）
  python backend/scripts/daily_data_sync.py --market A --incremental

  # 全量同步（重写所有数据）
  python backend/scripts/daily_data_sync.py --market A --full

  # 指定股票
  python backend/scripts/daily_data_sync.py --incremental --symbols 600519.SH,000001.SZ

  # 仅校准指标
  python backend/scripts/daily_data_sync.py --calibrate-only
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import struct
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("daily_sync")

# ---------------------------------------------------------------------------
# Paths & DB config
# ---------------------------------------------------------------------------
QLIB_DATA_DIR = PROJECT_ROOT / "db" / "qlib_data"
INVESTMENT_DATA_DIR = Path(
    os.getenv("QM_INVESTMENT_DATA_DIR", "/data/third_party/investment_data")
)
# A 股 Qlib 目录统一走 qlib_paths 解析（固定目录 /data/qlib/cn_data 优先）
from backend.shared.qlib_paths import resolve_qlib_provider_uri

QDB_QLIB_CACHE = Path(resolve_qlib_provider_uri("CN"))

# Default partition start date for stock_daily_latest backfills.
# 历史回测可通过 --start-date YYYY-MM-DD 或 env QM_SYNC_PARTITION_START 覆盖。
_DEFAULT_PARTITION_START = date(2026, 1, 1)


def _resolve_partition_start() -> date:
    """从环境变量解析 partition_start，回退到默认 2026-01-01。"""
    env_val = os.getenv("QM_SYNC_PARTITION_START")
    if env_val:
        try:
            return date.fromisoformat(env_val)
        except ValueError:
            log.warning(
                "Invalid QM_SYNC_PARTITION_START=%s, falling back to %s",
                env_val,
                _DEFAULT_PARTITION_START.isoformat(),
            )
    return _DEFAULT_PARTITION_START

DB_HOST = os.getenv("DB_HOST", os.getenv("DB_MASTER_HOST", "127.0.0.1"))
DB_PORT = int(os.getenv("DB_PORT", os.getenv("DB_MASTER_PORT", "5432")))
DB_NAME = os.getenv("DB_NAME", "quantmind")
DB_USER = os.getenv("DB_USER", "quantmind")
DB_PASS = os.getenv("DB_PASSWORD", "quantmind")


def _get_db_url() -> str:
    url = os.getenv("DATABASE_URL", "").strip()
    if url:
        url = url.replace("asyncpg", "psycopg2")
        if url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+psycopg2://", 1)
        return url
    from urllib.parse import quote_plus as _q
    return f"postgresql+psycopg2://{DB_USER}:{_q(DB_PASS)}@{DB_HOST}:{DB_PORT}/{DB_NAME}"


# ---------------------------------------------------------------------------
# Sync progress tracking (Redis)
# ---------------------------------------------------------------------------
_SYNC_PROGRESS_KEY = "quantmind:sync:progress"

_SYNC_STEPS = [
    ("init", "初始化"),
    ("pg_query", "查询PG最新数据"),
    ("data_sync", "多源数据同步"),
    ("qlib_bin", "更新Qlib二进制"),
    ("calibrate", "校准技术指标"),
    ("parquet", "更新特征Parquet"),
    ("done", "完成"),
]


def _update_sync_progress(step: str, detail: str = "", pct: int = 0, current: int = 0, total: int = 0):
    """Write current sync step to Redis for frontend polling."""
    try:
        import redis as _redis
        rds = _redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/0"), socket_timeout=1)
        data = {
            "step": step,
            "detail": detail,
            "pct": pct,
            "current": current,
            "total": total,
            "updated_at": datetime.now().isoformat(),
        }
        rds.set(_SYNC_PROGRESS_KEY, json.dumps(data), ex=1800)  # 30min TTL
    except Exception:
        pass


def get_sync_progress() -> dict:
    """Read current sync progress from Redis."""
    try:
        import redis as _redis
        rds = _redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/0"), socket_timeout=1)
        raw = rds.get(_SYNC_PROGRESS_KEY)
        if raw:
            return json.loads(raw)
    except Exception:
        pass
    return {"step": "idle", "detail": "", "pct": 0, "current": 0, "total": 0}


def clear_sync_progress():
    """Clear sync progress from Redis."""
    try:
        import redis as _redis
        rds = _redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/0"), socket_timeout=1)
        rds.delete(_SYNC_PROGRESS_KEY)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Qlib bin helpers
# ---------------------------------------------------------------------------
_QLIB_ROOT_CACHE: Optional[Path] = None
_CALENDAR_CACHE: Optional[np.ndarray] = None


def _find_qlib_root() -> Optional[Path]:
    """Find qlib_bin root, preferring QuantDB cache, then legacy dirs."""
    candidates = []
    for root in (QDB_QLIB_CACHE, QLIB_DATA_DIR, INVESTMENT_DATA_DIR):
        for cand in (root / "qlib_bin", root):
            cal = cand / "calendars" / "day.txt"
            if cal.exists() and (cand / "features").exists():
                try:
                    last_line = cal.read_text().strip().splitlines()[-1].strip()
                    candidates.append((last_line, cand))
                except Exception:
                    candidates.append(("", cand))
    if not candidates:
        return None
    # Pick the one with the most recent calendar date
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def _qlib_root() -> Path:
    """Get qlib root, cached. Raises if not found."""
    global _QLIB_ROOT_CACHE
    if _QLIB_ROOT_CACHE is not None:
        return _QLIB_ROOT_CACHE
    root = _find_qlib_root()
    if root is None:
        raise RuntimeError("Qlib data directory not found")
    _QLIB_ROOT_CACHE = root
    return root


def _load_calendar() -> np.ndarray:
    global _CALENDAR_CACHE
    if _CALENDAR_CACHE is not None:
        return _CALENDAR_CACHE
    qroot = _qlib_root()
    cal_path = qroot / "calendars" / "day.txt"
    lines = cal_path.read_text().strip().splitlines()
    _CALENDAR_CACHE = np.array([l.strip() for l in lines if l.strip()], dtype="datetime64[D]")
    return _CALENDAR_CACHE


def _read_qlib_bin(path: Path) -> tuple[int, np.ndarray]:
    raw = path.read_bytes()
    if len(raw) < 4:
        raise ValueError(f"{path} too small")
    start_idx = int(struct.unpack("<f", raw[:4])[0])
    n = (len(raw) - 4) // 4
    values = np.frombuffer(raw, dtype=np.float32, count=n, offset=4).copy()
    return start_idx, values


def _qlib_symbol(symbol: str) -> str:
    """600519.SH -> sh600519"""
    s = symbol.strip().upper()
    if "." in s:
        code, ex = s.split(".", 1)
        return f"{ex.lower()}{code}"
    return s.lower()


def _to_suffix_symbol(symbol: str) -> str:
    """SH600036 -> 600036.SH (PG internal → suffix format for QuantDB)"""
    s = symbol.strip().upper()
    if s.startswith("SH") or s.startswith("SZ") or s.startswith("BJ"):
        return f"{s[2:]}.{s[:2]}"
    if "." in s:
        return s
    # 纯数字自动识别
    if s.isdigit():
        if s.startswith("6") or s.startswith("9"):
            return f"{s}.SH"
        if s.startswith("0") or s.startswith("3") or s.startswith("2"):
            return f"{s}.SZ"
        if s.startswith("4") or s.startswith("8"):
            return f"{s}.BJ"
    return s


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------
def _get_engine():
    from sqlalchemy import create_engine
    db_url = os.getenv("DATABASE_URL", "").strip()
    if not db_url:
        db_url = f"postgresql+psycopg2://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
    elif "asyncpg" in db_url:
        db_url = db_url.replace("asyncpg", "psycopg2")
    elif db_url.startswith("postgresql://"):
        db_url = db_url.replace("postgresql://", "postgresql+psycopg2://", 1)
    return create_engine(db_url, pool_pre_ping=True)


def _get_pg_latest_dates(engine) -> dict[str, date]:
    """返回 {symbol: max_trade_date} 字典。"""
    from sqlalchemy import text as sql_text
    with engine.begin() as conn:
        rows = conn.execute(
            sql_text("SELECT symbol, MAX(trade_date) AS max_dt FROM stock_daily_latest GROUP BY symbol")
        ).fetchall()
    return {r[0]: r[1] for r in rows}


def _get_pg_symbol_list(engine) -> list[str]:
    """从 stock_daily_latest 获取所有 symbol。"""
    from sqlalchemy import text as sql_text
    with engine.begin() as conn:
        rows = conn.execute(
            sql_text("SELECT DISTINCT symbol FROM stock_daily_latest ORDER BY symbol")
        ).fetchall()
    return [r[0] for r in rows]


def _get_all_qlib_symbols() -> list[str]:
    """从 qlib_bin instruments/all.txt 获取所有 symbol。"""
    qroot = _qlib_root()
    inst_path = qroot / "instruments" / "all.txt"
    if not inst_path.exists():
        return []
    symbols = set()
    for line in inst_path.read_text().splitlines():
        parts = line.strip().split("\t")
        if parts and parts[0]:
            # sh600519 -> 600519.SH
            sym = parts[0].strip().lower()
            if len(sym) >= 2 and sym[:2] in ("sh", "sz", "bj"):
                code = sym[2:]
                ex = sym[:2].upper()
                symbols.add(f"{code}.{ex}")
            else:
                symbols.add(sym.upper())
    return sorted(symbols)


# ---------------------------------------------------------------------------
# Data source: investment_data (qlib_bin)
# ---------------------------------------------------------------------------
def _read_investment_data(symbol: str, start: date, end: date) -> Optional[pd.DataFrame]:
    """从 qlib_bin 读取指定 symbol 的日线数据。"""
    qroot = _qlib_root()
    qsym = _qlib_symbol(symbol)
    sym_dir = qroot / "features" / qsym
    if not sym_dir.exists():
        return None

    calendar = _load_calendar()
    cal_len = len(calendar)

    field_map = {
        "open": "open", "high": "high", "low": "low",
        "close": "close", "volume": "volume", "amount": "amount",
        "factor": "adj_factor",
    }
    series: dict[str, np.ndarray] = {}
    for qf, std in field_map.items():
        f = sym_dir / f"{qf}.day.bin"
        if not f.exists():
            continue
        try:
            start_idx, vals = _read_qlib_bin(f)
        except Exception:
            continue
        full = np.full(cal_len, np.nan, dtype=np.float64)
        end_idx = min(start_idx + len(vals), cal_len)
        full[start_idx:end_idx] = vals[:end_idx - start_idx]
        series[std] = full

    if "close" not in series:
        return None

    df = pd.DataFrame(series)
    df["trade_date"] = pd.to_datetime(calendar).date
    df = df.dropna(subset=["close"]).reset_index(drop=True)
    if df.empty:
        return None

    if start:
        df = df[df["trade_date"] >= start]
    if end:
        df = df[df["trade_date"] <= end]
    if df.empty:
        return None

    df["symbol"] = symbol.upper()
    for c in ("open", "high", "low", "close", "volume", "amount", "adj_factor"):
        if c not in df.columns:
            df[c] = pd.NA
    df["adj_factor"] = df["adj_factor"].fillna(1.0)
    df["source"] = "investment_data"
    return df[["symbol", "trade_date", "open", "high", "low", "close",
               "volume", "amount", "adj_factor", "source"]].reset_index(drop=True)


# ---------------------------------------------------------------------------
# Data source: baostock
# ---------------------------------------------------------------------------
_BS_LOGGED_IN = False


def _ensure_baostock():
    global _BS_LOGGED_IN
    if _BS_LOGGED_IN:
        return
    import baostock as bs
    rs = bs.login()
    if rs.error_code != "0":
        raise RuntimeError(f"baostock login failed: {rs.error_msg}")
    _BS_LOGGED_IN = True


def _to_bs_symbol(symbol: str) -> str:
    s = symbol.strip().upper()
    if "." in s:
        code, ex = s.split(".", 1)
        return f"{ex.lower()}.{code}"
    return s.lower()


def _read_baostock(symbol: str, start: date, end: date) -> Optional[pd.DataFrame]:
    try:
        import baostock as bs
        _ensure_baostock()
    except Exception:
        return None
    try:
        rs = bs.query_history_k_data_plus(
            _to_bs_symbol(symbol),
            "date,open,high,low,close,volume,amount,adjustflag,turn,pctChg",
            start_date=start.isoformat() if start else "",
            end_date=end.isoformat() if end else "",
            frequency="d",
            adjustflag="2",  # 前复权
        )
        if rs.error_code != "0":
            return None
        rows = []
        while rs.next():
            rows.append(rs.get_row_data())
        if not rows:
            return None
        df = pd.DataFrame(rows, columns=rs.fields)
        df["symbol"] = symbol.upper()
        df["trade_date"] = pd.to_datetime(df["date"]).dt.date
        for c in ("open", "high", "low", "close", "volume", "amount", "turn", "pctChg"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df["adj_factor"] = 1.0
        df["source"] = "baostock"
        return df[["symbol", "trade_date", "open", "high", "low", "close",
                    "volume", "amount", "adj_factor", "source"]]
    except Exception as exc:
        log.debug("baostock failed for %s: %s", symbol, exc)
        return None


# ---------------------------------------------------------------------------
# Data source: akshare
# ---------------------------------------------------------------------------
def _read_akshare(symbol: str, start: date, end: date) -> Optional[pd.DataFrame]:
    try:
        import akshare as ak
    except ImportError:
        return None
    try:
        # 尝试 stock_zh_a_hist (东方财富源)
        code = symbol.strip().upper().split(".")[0]
        raw = ak.stock_zh_a_hist(
            symbol=code, period="daily",
            start_date=start.strftime("%Y%m%d") if start else "20000101",
            end_date=end.strftime("%Y%m%d") if end else "20991231",
            adjust="qfq",
        )
        if raw is not None and not raw.empty:
            rename = {
                "日期": "trade_date", "开盘": "open", "收盘": "close",
                "最高": "high", "最低": "low", "成交量": "volume", "成交额": "amount",
            }
            df = raw.rename(columns=rename).copy()
            df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
            df["symbol"] = symbol.upper()
            for c in ("open", "high", "low", "close", "volume", "amount"):
                if c in df.columns:
                    df[c] = pd.to_numeric(df[c], errors="coerce")
            df["adj_factor"] = 1.0
            df["source"] = "akshare"
            return df[["symbol", "trade_date", "open", "high", "low", "close",
                        "volume", "amount", "adj_factor", "source"]]
    except Exception:
        pass

    # 备用: stock_zh_a_daily (新浪源)
    try:
        s = symbol.strip().upper()
        if "." in s:
            code, ex = s.split(".", 1)
            prefix = f"{ex.lower()}{code}"
        else:
            prefix = s.lower()
        raw = ak.stock_zh_a_daily(symbol=prefix, adjust="qfq")
        if raw is not None and not raw.empty:
            rename = {
                "date": "trade_date", "open": "open", "close": "close",
                "high": "high", "low": "low", "volume": "volume", "amount": "amount",
            }
            df = raw.rename(columns=rename).copy()
            df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
            if start:
                df = df[df["trade_date"] >= start]
            if end:
                df = df[df["trade_date"] <= end]
            if df.empty:
                return None
            df["symbol"] = symbol.upper()
            for c in ("open", "high", "low", "close", "volume", "amount"):
                if c in df.columns:
                    df[c] = pd.to_numeric(df[c], errors="coerce")
            df["adj_factor"] = 1.0
            df["source"] = "akshare"
            return df[["symbol", "trade_date", "open", "high", "low", "close",
                        "volume", "amount", "adj_factor", "source"]]
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Data source: eltdx (today's data)
# ---------------------------------------------------------------------------
def _read_eltdx(symbol: str) -> Optional[pd.DataFrame]:
    try:
        from mootdx.quotes import Quotes
    except ImportError:
        return None
    try:
        client = Quotes.factory(market="std")
        code = symbol.strip().upper().split(".")[0]
        raw = client.bars(symbol=code, frequency=9, offset=10)
        if raw is None or len(raw) == 0:
            return None
        df = raw.copy()
        if "datetime" in df.columns:
            df["trade_date"] = pd.to_datetime(df["datetime"]).dt.date
        elif "date" in df.columns:
            df["trade_date"] = pd.to_datetime(df["date"]).dt.date
        else:
            df["trade_date"] = pd.to_datetime(df.index).date
        df["symbol"] = symbol.upper()
        for c in ("open", "high", "low", "close", "volume", "amount"):
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce")
            else:
                df[c] = pd.NA
        df["adj_factor"] = 1.0
        df["source"] = "eltdx"
        return df[["symbol", "trade_date", "open", "high", "low", "close",
                    "volume", "amount", "adj_factor", "source"]]
    except Exception as exc:
        log.debug("eltdx failed for %s: %s", symbol, exc)
        return None


# ---------------------------------------------------------------------------
# PostgreSQL upsert
# ---------------------------------------------------------------------------
def _upsert_to_pg(engine, df: pd.DataFrame) -> int:
    """将 DataFrame 写入 stock_daily_latest（UPSERT）。"""
    from sqlalchemy import text as sql_text

    if df is None or df.empty:
        return 0

    df = df.copy()
    df = df.drop_duplicates(subset=["trade_date", "symbol"])

    # 只保留 OHLCV 核心列
    core_cols = ["trade_date", "symbol", "open", "high", "low", "close",
                 "volume", "amount", "adj_factor"]
    use_cols = [c for c in core_cols if c in df.columns]
    data = df[use_cols].fillna(0)
    data = data.replace([float("inf"), float("-inf")], 0)

    records = [tuple(row) for row in data.itertuples(index=False, name=None)]
    if not records:
        return 0

    non_pk = [c for c in use_cols if c not in ("trade_date", "symbol")]

    with engine.begin() as conn:
        for rec in records:
            rec_dict = dict(zip(use_cols, rec))
            placeholders = ", ".join([f":{c}" for c in use_cols])
            cols = ", ".join(use_cols)
            if non_pk:
                update_set = ", ".join([f"{c}=EXCLUDED.{c}" for c in non_pk])
                sql = (
                    f"INSERT INTO stock_daily_latest ({cols}) VALUES ({placeholders}) "
                    f"ON CONFLICT (trade_date, symbol) DO UPDATE SET {update_set}"
                )
            else:
                sql = (
                    f"INSERT INTO stock_daily_latest ({cols}) VALUES ({placeholders}) "
                    "ON CONFLICT (trade_date, symbol) DO NOTHING"
                )
            conn.execute(sql_text(sql), rec_dict)

    return len(records)


# ---------------------------------------------------------------------------
# Stock name updates (CN from baostock, HK/US/Crypto from yfinance)
# ---------------------------------------------------------------------------
_FEATURE_SNAPSHOT_DIR = PROJECT_ROOT / "db" / "feature_snapshots"

_MARKET_TABLE_CFG: dict[str, dict] = {
    "hk": {"parquet": "model_features_hk.parquet", "table": "stock_daily_latest_hk", "symbol_col": "instrument"},
    "us": {"parquet": "model_features_us.parquet", "table": "stock_daily_latest_us", "symbol_col": "instrument"},
    "crypto": {"parquet": "model_features_crypto.parquet", "table": "stock_daily_latest_crypto", "symbol_col": "instrument"},
}

_NON_CN_SDL_COLUMNS = {
    "symbol": "VARCHAR(20)", "trade_date": "DATE", "name": "VARCHAR(100)",
    "open": "DOUBLE PRECISION", "high": "DOUBLE PRECISION", "low": "DOUBLE PRECISION",
    "close": "DOUBLE PRECISION", "volume": "DOUBLE PRECISION", "amount": "DOUBLE PRECISION",
    "adj_factor": "DOUBLE PRECISION", "turnover_rate": "DOUBLE PRECISION",
    "pe_ttm": "DOUBLE PRECISION", "pb": "DOUBLE PRECISION", "roe": "DOUBLE PRECISION",
    "total_mv": "DOUBLE PRECISION", "float_mv": "DOUBLE PRECISION", "industry": "VARCHAR(100)",
}


def _fetch_cn_stock_names(symbols: list[str]) -> dict[str, str]:
    """Fetch CN stock names from baostock."""
    name_map: dict[str, str] = {}
    try:
        import baostock as bs
        _ensure_baostock()
    except Exception:
        log.warning("baostock not available, skipping CN name fetch")
        return name_map

    total = len(symbols)
    log.info("Fetching CN stock names from baostock (%d symbols)...", total)
    for i, sym in enumerate(symbols):
        try:
            bs_code = _to_bs_symbol(sym)
            rs = bs.query_stock_basic(code=bs_code)
            if rs.error_code == "0" and rs.next():
                row = rs.get_row_data()
                if row and len(row) >= 2 and row[1]:
                    name_map[sym] = row[1]
        except Exception:
            pass
        if (i + 1) % 500 == 0:
            log.info("  CN names progress: %d/%d", i + 1, total)

    log.info("Fetched %d CN stock names", len(name_map))
    return name_map


def _update_cn_stock_names(engine, name_map: dict[str, str]) -> int:
    """Update stock_name column in stock_daily_latest for CN stocks."""
    if not name_map:
        return 0
    from sqlalchemy import text as sql_text

    updated = 0
    with engine.begin() as conn:
        for sym, name in name_map.items():
            res = conn.execute(
                sql_text("UPDATE stock_daily_latest SET stock_name = :name WHERE symbol = :sym AND (stock_name IS NULL OR stock_name = '' OR stock_name = :sym)"),
                {"name": name, "sym": sym},
            )
            updated += res.rowcount
    log.info("Updated %d CN stock names", updated)
    return updated


def _fetch_non_cn_stock_names(symbols: list[str], market: str) -> dict[str, str]:
    """Fetch stock names for HK/US from yfinance, or extract base asset for crypto."""
    name_map: dict[str, str] = {}

    if market == "crypto":
        for sym in symbols:
            s = str(sym).upper()
            if s.endswith("USDT"):
                name_map[sym] = s[:-4]
            elif s.endswith("BUSD") or s.endswith("USD"):
                name_map[sym] = s[:-4]
            else:
                name_map[sym] = s
        return name_map

    try:
        import yfinance as yf
    except ImportError:
        log.warning("yfinance not installed, using symbols as names")
        return {sym: str(sym) for sym in symbols}

    total = len(symbols)
    log.info("Fetching %s stock names from yfinance (%d symbols)...", market.upper(), total)
    for i, sym in enumerate(symbols):
        try:
            ticker_sym = str(sym)
            if market == "hk":
                ticker_sym = f"{sym}.HK"
            ticker = yf.Ticker(ticker_sym)
            info = ticker.info
            name = info.get("shortName") or info.get("longName") or str(sym)
            name_map[sym] = name
        except Exception:
            name_map[sym] = str(sym)
        if (i + 1) % 50 == 0:
            log.info("  %s names progress: %d/%d", market.upper(), i + 1, total)

    log.info("Fetched %d %s stock names", len(name_map), market.upper())
    return name_map


def _sync_market_stock_tables(engine) -> dict[str, int]:
    """Sync HK/US/Crypto stock_daily_latest_* tables from parquet files."""
    from sqlalchemy import text as sql_text

    results: dict[str, int] = {}
    for market, cfg in _MARKET_TABLE_CFG.items():
        parquet_path = _FEATURE_SNAPSHOT_DIR / cfg["parquet"]
        table_name = cfg["table"]
        symbol_col = cfg["symbol_col"]

        if not parquet_path.exists():
            log.info("Parquet not found for %s: %s", market, parquet_path)
            results[market] = 0
            continue

        try:
            log.info("Syncing %s table from %s...", table_name, parquet_path.name)
            df = pd.read_parquet(parquet_path)

            # Get latest date per symbol
            if "trade_date" in df.columns:
                df["trade_date"] = pd.to_datetime(df["trade_date"])
                latest_date = df["trade_date"].max()
                df_latest = df[df["trade_date"] == latest_date].copy()
                log.info("  %s: latest date %s, %d symbols", market, latest_date.date(), len(df_latest))
            else:
                df_latest = df.copy()

            # Rename instrument -> symbol
            if symbol_col in df_latest.columns:
                df_latest = df_latest.rename(columns={symbol_col: "symbol"})

            # Fetch stock names
            symbols = df_latest["symbol"].unique().tolist()
            name_map = _fetch_non_cn_stock_names(symbols, market)

            # Prepare columns
            for col in _NON_CN_SDL_COLUMNS:
                if col not in df_latest.columns:
                    if col == "name":
                        df_latest[col] = df_latest["symbol"].map(name_map)
                    elif col == "industry":
                        df_latest[col] = ""
                    else:
                        df_latest[col] = 0.0

            cols_to_keep = [c for c in _NON_CN_SDL_COLUMNS if c in df_latest.columns]
            df_out = df_latest[cols_to_keep].copy()
            df_out["symbol"] = df_out["symbol"].astype(str)

            # Ensure table exists
            cols_ddl = ", ".join(f"{c} {t}" for c, t in _NON_CN_SDL_COLUMNS.items())
            with engine.begin() as conn:
                conn.execute(sql_text(f"CREATE TABLE IF NOT EXISTS {table_name} ({cols_ddl})"))

                # Check if DB already has newer data than parquet
                db_max = conn.execute(sql_text(f"SELECT MAX(trade_date) FROM {table_name}")).scalar()
                parquet_max = df_out["trade_date"].max() if "trade_date" in df_out.columns else None
                if db_max and parquet_max and pd.Timestamp(db_max) >= pd.Timestamp(parquet_max):
                    log.info("  %s: DB already has data up to %s, parquet only to %s — skipping overwrite",
                             table_name, db_max, parquet_max)
                    results[market] = 0
                    continue

                # Upsert instead of delete+insert
                update_cols = [c for c in df_out.columns if c not in ("symbol", "trade_date")]
                set_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)

                batch_size = 1000
                total_inserted = 0
                for i in range(0, len(df_out), batch_size):
                    batch = df_out.iloc[i:i + batch_size]
                    cols = ", ".join(batch.columns)
                    placeholders = ", ".join([f":{col}" for col in batch.columns])
                    upsert_sql = f"""
                        INSERT INTO {table_name} ({cols}) VALUES ({placeholders})
                        ON CONFLICT (symbol, trade_date) DO UPDATE SET {set_clause}
                    """
                    for _, row in batch.iterrows():
                        params = {col: (None if pd.isna(row[col]) else row[col]) for col in batch.columns}
                        conn.execute(sql_text(upsert_sql), params)
                    total_inserted += len(batch)

                # Create indexes
                conn.execute(sql_text(f"CREATE INDEX IF NOT EXISTS idx_{table_name}_symbol ON {table_name} (symbol)"))
                conn.execute(sql_text(f"CREATE INDEX IF NOT EXISTS idx_{table_name}_trade_date ON {table_name} (trade_date)"))

            log.info("  %s: %d rows upserted", table_name, total_inserted)
            results[market] = total_inserted
        except Exception as exc:
            log.warning("Failed to sync %s: %s", market, exc)
            results[market] = -1

    return results


# ---------------------------------------------------------------------------
# Technical indicators calibration
# ---------------------------------------------------------------------------
def _calibrate_indicators(engine, symbols: Optional[list[str]] = None, days: int = 90):
    """计算 MA/收益率/换手率等技术指标并更新 stock_daily_latest。"""
    from sqlalchemy import text as sql_text

    log.info("Calibrating technical indicators (last %d days)...", days)
    cutoff = date.today() - timedelta(days=days)

    with engine.begin() as conn:
        if symbols:
            # 指定股票
            sym_filter = "AND symbol = ANY(:symbols)"
            params = {"cutoff": cutoff, "symbols": symbols}
        else:
            sym_filter = ""
            params = {"cutoff": cutoff}

        # 读取原始数据
        rows = conn.execute(
            sql_text(f"""
                SELECT symbol, trade_date, open, high, low, close, volume, amount, adj_factor
                FROM stock_daily_latest
                WHERE trade_date >= :cutoff {sym_filter}
                ORDER BY symbol, trade_date
            """),
            params,
        ).fetchall()

    if not rows:
        log.info("No data to calibrate")
        return

    df = pd.DataFrame(rows, columns=["symbol", "trade_date", "open", "high", "low",
                                      "close", "volume", "amount", "adj_factor"])
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
    for c in ("open", "high", "low", "close", "volume", "amount", "adj_factor"):
        df[c] = pd.to_numeric(df[c], errors="coerce")

    log.info("Calibrating %d rows for %d symbols...", len(df), df["symbol"].nunique())

    # 计算指标
    df = df.sort_values(["symbol", "trade_date"])

    # MA
    for p in (5, 10, 20, 60):
        df[f"ma{p}"] = df.groupby("symbol")["close"].transform(
            lambda x: x.rolling(p, min_periods=1).mean()
        )
        df[f"ma_gap_{p}"] = ((df["close"] / df[f"ma{p}"]) - 1) * 100

    # 收益率
    df["ret"] = df.groupby("symbol")["close"].pct_change()
    df["return_1d"] = df.groupby("symbol")["close"].pct_change(1)
    df["return_3d"] = df.groupby("symbol")["close"].pct_change(3)
    df["return_5d"] = df.groupby("symbol")["close"].pct_change(5)
    df["return_10d"] = df.groupby("symbol")["close"].pct_change(10)
    df["return_20d"] = df.groupby("symbol")["close"].pct_change(20)
    df["return_60d"] = df.groupby("symbol")["close"].pct_change(60)

    # 波动率
    df["vol_std_5"] = df.groupby("symbol")["ret"].transform(
        lambda x: x.rolling(5, min_periods=1).std() * 100
    )
    df["vol_std_20"] = df.groupby("symbol")["ret"].transform(
        lambda x: x.rolling(20, min_periods=1).std() * 100
    )
    df["vol_std_60"] = df.groupby("symbol")["ret"].transform(
        lambda x: x.rolling(60, min_periods=1).std() * 100
    )

    # 涨跌幅
    df["pct_change"] = df["ret"] * 100

    # 写回数据库
    update_cols = [
        "ma5", "ma10", "ma20", "ma60",
        "ma_gap_5", "ma_gap_10", "ma_gap_20",
        "return_1d", "return_3d", "return_5d", "return_10d", "return_20d", "return_60d",
        "vol_std_5", "vol_std_20", "vol_std_60",
        "pct_change",
    ]

    # 批量更新
    batch_size = 500
    total_updated = 0
    with engine.begin() as conn:
        for i in range(0, len(df), batch_size):
            batch = df.iloc[i:i + batch_size]
            for _, row in batch.iterrows():
                sets = []
                params = {"sym": row["symbol"], "td": row["trade_date"]}
                for col in update_cols:
                    val = row.get(col)
                    if pd.notna(val) and not (isinstance(val, float) and (np.isinf(val) or np.isnan(val))):
                        sets.append(f"{col} = :{col}")
                        params[col] = float(val)
                if sets:
                    conn.execute(
                        sql_text(
                            f"UPDATE stock_daily_latest SET {', '.join(sets)} "
                            "WHERE symbol = :sym AND trade_date = :td"
                        ),
                        params,
                    )
                    total_updated += 1

    log.info("Indicators calibrated: %d rows updated", total_updated)


# ---------------------------------------------------------------------------
# Auto-create future partitions for stock_daily_latest
# ---------------------------------------------------------------------------
def _ensure_future_partitions(engine, months_ahead: int = 3):
    """Create monthly partitions for stock_daily_latest if they don't exist."""
    from datetime import date
    from dateutil.relativedelta import relativedelta
    from sqlalchemy import text as sql_text

    today = date.today()
    with engine.begin() as conn:
        for i in range(0, months_ahead + 1):
            target = today + relativedelta(months=i)
            year_month = target.strftime("%Y_%m")
            start = target.replace(day=1)
            if target.month == 12:
                end = start.replace(year=start.year + 1, month=1)
            else:
                end = start.replace(month=start.month + 1)

            part_name = f"stock_daily_new_{year_month}"
            exists = conn.execute(
                sql_text("SELECT 1 FROM pg_class WHERE relname = :name"),
                {"name": part_name},
            ).scalar()
            if not exists:
                conn.execute(sql_text(
                    f"CREATE TABLE IF NOT EXISTS {part_name} "
                    f"PARTITION OF stock_daily_latest "
                    f"FOR VALUES FROM ('{start}') TO ('{end}')"
                ))
                log.info("Created partition %s (%s to %s)", part_name, start, end)


# ---------------------------------------------------------------------------
# Main sync logic
# ---------------------------------------------------------------------------
def run_sync(
    market: str = "A",
    symbols: Optional[list[str]] = None,
    incremental: bool = True,
    update_qlib: bool = True,
    calibrate: bool = True,
    calibrate_days: int = 90,
    partition_start: Optional[date] = None,
) -> dict:
    """执行统一数据同步。

    本入口仅支持 A 股 (market='A' 或 'CN')。其他市场请使用：
      - REST: POST /admin/data-platform/sync-alpha-agent-market
      - CLI: python backend/scripts/update_market_features.py --market <market>

    数据源优先级:
      1. QuantDB parquet (data/quantdb/) — 最权威
      2. baostock — 稳定，T-1
      3. akshare — 新浪源
      4. eltdx — 当天
    """
    if market.upper() not in ("A", "CN"):
        raise ValueError(
            f"daily_data_sync.run_sync 仅支持 A/CN 市场，传入 market={market!r}。"
            " 对 HK/US/Crypto 市场请使用 /admin/data-platform/sync-alpha-agent-market "
            "或 python backend/scripts/update_market_features.py --market <market>。"
        )

    if partition_start is None:
        partition_start = _resolve_partition_start()
    log.info("Using partition_start=%s", partition_start.isoformat())

    result = {
        "market": market,
        "mode": "incremental" if incremental else "full",
        "started": datetime.now().isoformat(),
        "quantdb_parquet_synced": 0,
        "baostock_synced": 0,
        "akshare_synced": 0,
        "eltdx_synced": 0,
        "qlib_cache_updated": False,
        "stock_names_updated": 0,
        "market_tables_synced": {},
        "indicators_calibrated": False,
        "errors": [],
    }

    engine = _get_engine()
    _update_sync_progress("init", "初始化同步环境...", pct=5)

    # Auto-create future partitions for stock_daily_latest
    if market.upper() in ("A", "CN"):
        _ensure_future_partitions(engine, months_ahead=3)

    # 1. 确定股票列表
    if symbols:
        target_symbols = symbols
    else:
        # 合并 PG 已有 + qlib instruments
        pg_symbols = set(_get_pg_symbol_list(engine))
        qlib_symbols = set(_get_all_qlib_symbols())
        target_symbols = sorted(pg_symbols | qlib_symbols)
        log.info("Target symbols: %d (PG: %d, Qlib: %d)",
                 len(target_symbols), len(pg_symbols), len(qlib_symbols))

    if not target_symbols:
        log.warning("No symbols to sync")
        return result

    # 2. 获取 PG 最新日期
    pg_latest = _get_pg_latest_dates(engine)
    log.info("PG has %d symbols, latest dates loaded", len(pg_latest))

    # 3. 确定投资数据最新日期 (fallback 用)
    inv_latest_date: Optional[date] = None
    qroot = _find_qlib_root()
    if qroot:
        cal = _load_calendar()
        if len(cal) > 0:
            inv_latest_date = pd.to_datetime(cal[-1]).date()
            log.info("Investment data calendar (fallback): %s to %s (%d days)",
                     cal[0], cal[-1], len(cal))

    today = date.today()

    # 4. 同步每只股票
    _update_sync_progress("pg_query", "查询PG最新日期...", pct=10)
    bs_count = 0
    ak_count = 0
    eltdx_count = 0
    all_new_data = []

    total = len(target_symbols)
    # 快速跳过已到 T-1 的股票，避免白跑循环
    yesterday = today - timedelta(days=1)
    skip_count = 0
    need_sync_symbols = []
    for sym in target_symbols:
        pg_max = pg_latest.get(sym)
        if incremental and pg_max is not None and pg_max >= yesterday:
            skip_count += 1
            continue
        need_sync_symbols.append(sym)
    log.info("Skip %d already-up-to-date symbols, syncing %d", skip_count, len(need_sync_symbols))

    # --- Phase 1: 批量读取 QuantDB parquet（本地文件，快） ---
    qdb_count = 0
    still_need_symbols: list[str] = []
    quantdb_dir = Path(os.getenv("QM_QUANTDB_DATA_DIR", str(PROJECT_ROOT / "data" / "quantdb")))
    quantdb_available = quantdb_dir.is_dir()

    if quantdb_available and need_sync_symbols:
        try:
            from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub
            hub = QuantDBDataHub(quantdb_dir)
            if hub.available:
                # 获取 QuantDB 最新日期
                df_cal = hub.fetch_calendar()
                qdb_latest_date: Optional[date] = None
                if not df_cal.empty:
                    cal_col = None
                    for col in ("trade_date", "date", "time", "cal_date", "TradingDate"):
                        if col in df_cal.columns:
                            cal_col = col
                            break
                    if cal_col:
                        qdb_latest_date = pd.to_datetime(df_cal[cal_col]).max().date()
                        log.info("QuantDB parquet calendar latest: %s", qdb_latest_date)

                _update_sync_progress("data_sync", f"Phase 1: 批量读取 QuantDB parquet ({len(need_sync_symbols)} 只)...", pct=12, current=0, total=len(need_sync_symbols))

                qdb_batch: list[pd.DataFrame] = []
                for idx, sym in enumerate(need_sync_symbols):
                    if (idx + 1) % 1000 == 0:
                        log.info("QuantDB parquet batch: %d/%d", idx + 1, len(need_sync_symbols))
                        pct = 12 + int(18 * (idx + 1) / len(need_sync_symbols))
                        _update_sync_progress("data_sync", f"QuantDB parquet {idx+1}/{len(need_sync_symbols)}", pct=pct, current=idx+1, total=len(need_sync_symbols))

                    pg_max = pg_latest.get(sym)
                    need_start = None
                    if pg_max is None:
                        need_start = partition_start
                    elif qdb_latest_date and pg_max < qdb_latest_date:
                        need_start = max(pg_max + timedelta(days=1), partition_start)

                    if need_start and qdb_latest_date and need_start <= qdb_latest_date:
                        try:
                            # Convert PG internal format (SH600036) to suffix format (600036.SH)
                            qdb_sym = _to_suffix_symbol(sym)
                            qdb_df = hub.fetch_daily_kline(qdb_sym, need_start, qdb_latest_date, adjust="qfq")
                            if not qdb_df.empty:
                                qdb_df = qdb_df.copy()
                                qdb_df["symbol"] = sym
                                qdb_df["source"] = "quantdb_parquet"
                                qdb_df["adj_factor"] = 1.0
                                for c in ("open", "high", "low", "close", "volume", "amount"):
                                    if c in qdb_df.columns:
                                        qdb_df[c] = pd.to_numeric(qdb_df[c], errors="coerce").fillna(0)
                                qdb_batch.append(qdb_df)
                                qdb_count += 1
                                # 检查是否还需要 baostock 补
                                max_qdb = qdb_df["trade_date"].max()
                                if isinstance(max_qdb, date):
                                    pass
                                else:
                                    max_qdb = date.fromisoformat(str(max_qdb))
                                if max_qdb < today:
                                    still_need_symbols.append(sym)
                                continue
                        except Exception as exc:
                            log.debug("QuantDB parquet failed for %s: %s", sym, exc)
                    # 没有 QuantDB 数据，直接进 baostock 列表
                    still_need_symbols.append(sym)

                if qdb_batch:
                    qdb_combined = pd.concat(qdb_batch, ignore_index=True)
                    rows = _upsert_to_pg(engine, qdb_combined)
                    all_new_data.append(qdb_combined)
                    log.info("Phase 1 done: QuantDB parquet %d symbols, %d rows", qdb_count, len(qdb_combined))
            else:
                still_need_symbols = list(need_sync_symbols)
        except Exception as exc:
            log.warning("QuantDB parquet read failed, falling back: %s", exc)
            still_need_symbols = list(need_sync_symbols)
    else:
        still_need_symbols = list(need_sync_symbols)

    # --- Phase 2: baostock/akshare 补剩余缺口 ---
    _update_sync_progress("data_sync", f"Phase 2: baostock 补缺 {len(still_need_symbols)} 只...", pct=35, current=0, total=len(still_need_symbols))
    for idx, sym in enumerate(still_need_symbols):
        if (idx + 1) % 500 == 0:
            log.info("baostock: %d/%d symbols", idx + 1, len(still_need_symbols))
            pct = 35 + int(35 * (idx + 1) / len(still_need_symbols))
            _update_sync_progress("data_sync", f"baostock {idx+1}/{len(still_need_symbols)}", pct=pct, current=idx+1, total=len(still_need_symbols))

        pg_max = pg_latest.get(sym)
        need_start: Optional[date] = None

        if incremental:
            if pg_max is None:
                need_start = partition_start
            elif pg_max < today:
                need_start = max(pg_max + timedelta(days=1), partition_start)
            else:
                continue
        else:
            need_start = partition_start

        if need_start is None:
            continue

        # --- Source 2: baostock (fill gap to T-1) ---
        bs_end = today - timedelta(days=1)
        bs_start = need_start

        new_df = None
        if bs_start <= bs_end:
            bs_df = _read_baostock(sym, bs_start, bs_end)
            if bs_df is not None and not bs_df.empty:
                new_df = bs_df
                bs_count += 1

        # --- Source 3: akshare (fallback) ---
        ak_start = bs_start
        if new_df is not None and not new_df.empty:
            max_dt = new_df["trade_date"].max()
            if isinstance(max_dt, date):
                ak_start = max_dt + timedelta(days=1)
            else:
                ak_start = date.fromisoformat(str(max_dt)) + timedelta(days=1)

        if ak_start <= bs_end and (new_df is None or new_df.empty):
            ak_df = _read_akshare(sym, ak_start, bs_end)
            if ak_df is not None and not ak_df.empty:
                if new_df is not None and not new_df.empty:
                    new_df = pd.concat([new_df, ak_df], ignore_index=True)
                else:
                    new_df = ak_df
                ak_count += 1

        # --- Source 4: eltdx (today) ---
        if today.weekday() < 5:  # 工作日
            eltdx_needed = False
            if new_df is None or new_df.empty:
                eltdx_needed = True
            else:
                max_dt = new_df["trade_date"].max()
                if isinstance(max_dt, date):
                    eltdx_needed = max_dt < today
                else:
                    eltdx_needed = date.fromisoformat(str(max_dt)) < today

            if eltdx_needed:
                eltdx_df = _read_eltdx(sym)
                if eltdx_df is not None and not eltdx_df.empty:
                    # 只取今天的
                    eltdx_today = eltdx_df[eltdx_df["trade_date"] == today]
                    if not eltdx_today.empty:
                        if new_df is not None and not new_df.empty:
                            new_df = pd.concat([new_df, eltdx_today], ignore_index=True)
                        else:
                            new_df = eltdx_today
                        eltdx_count += 1

        # --- 写入 PG ---
        if new_df is not None and not new_df.empty:
            new_df = new_df.drop_duplicates(subset=["trade_date", "symbol"])
            try:
                rows = _upsert_to_pg(engine, new_df)
                all_new_data.append(new_df)
            except Exception as exc:
                result["errors"].append(f"{sym}: {exc}")
                log.debug("PG upsert failed for %s: %s", sym, exc)

    result["quantdb_parquet_synced"] = qdb_count
    result["baostock_synced"] = bs_count
    result["akshare_synced"] = ak_count
    result["eltdx_synced"] = eltdx_count

    log.info("Sync complete: quantdb_parquet=%d, baostock=%d, akshare=%d, eltdx=%d",
             qdb_count, bs_count, ak_count, eltdx_count)

    # 5. 更新 Qlib 缓存 (从 QuantDB parquet 生成)
    if update_qlib:
        _update_sync_progress("qlib_cache", "更新Qlib缓存...", pct=75)
        try:
            from backend.services.engine.qlib_data_builder import ensure_qlib_cache
            ensure_qlib_cache(quantdb_dir)
            result["qlib_cache_updated"] = True
            log.info("Qlib cache updated from parquet")
        except Exception as exc:
            result["errors"].append(f"qlib_cache: {exc}")
            log.warning("Qlib cache update failed: %s", exc)

    # 5.5 更新股票名称 + 同步多市场表
    _update_sync_progress("stock_names", "更新股票名称...", pct=78)
    try:
        if market.upper() in ("A", "CN"):
            # CN: 从 baostock 获取股票名称
            cn_symbols = _get_pg_symbol_list(engine)
            cn_name_map = _fetch_cn_stock_names(cn_symbols)
            cn_updated = _update_cn_stock_names(engine, cn_name_map)
            result["stock_names_updated"] = cn_updated
        else:
            result["stock_names_updated"] = 0
    except Exception as exc:
        result["errors"].append(f"cn_stock_names: {exc}")
        log.warning("CN stock name update failed: %s", exc)

    _update_sync_progress("market_tables", "同步多市场数据表...", pct=80)
    try:
        market_results = _sync_market_stock_tables(engine)
        result["market_tables_synced"] = market_results
        log.info("Market tables synced: %s", market_results)
    except Exception as exc:
        result["errors"].append(f"market_tables: {exc}")
        log.warning("Market table sync failed: %s", exc)

    # 6. 校准指标
    if calibrate:
        _update_sync_progress("calibrate", "校准技术指标...", pct=85)
        try:
            _calibrate_indicators(engine, symbols=symbols, days=calibrate_days)
            result["indicators_calibrated"] = True
        except Exception as exc:
            result["errors"].append(f"calibrate: {exc}")
            log.warning("Indicator calibration failed: %s", exc)

    # 7. New A-share models read QuantDB factors directly.  Do not materialise
    # or update model_features_*.parquet during routine sync.
    _update_sync_progress("parquet", "跳过旧特征快照（QuantDB 直读）", pct=92)
    result["parquet_updated"] = {"status": "skipped", "reason": "QuantDB direct factor reader"}

    # 关闭 baostock
    if _BS_LOGGED_IN:
        try:
            import baostock as bs
            bs.logout()
        except Exception:
            pass

    result["finished"] = datetime.now().isoformat()
    _update_sync_progress("done", "同步完成!", pct=100)
    return result


# ---------------------------------------------------------------------------
# Update investment_data from GitHub
# ---------------------------------------------------------------------------
def update_investment_data(version: str = "") -> dict:
    """下载最新 investment_data qlib_bin 并解压。"""
    log.info("Updating investment_data...")
    # 复用 sync_investment_data.py 的逻辑
    from backend.scripts.sync_investment_data import (
        _get_latest_version, _download_asset, _extract_qlib_data,
        DOWNLOAD_DIR, ASSET_NAME,
    )

    if not version:
        version = _get_latest_version()
    log.info("Target version: %s", version)

    archive_path = DOWNLOAD_DIR / version / ASSET_NAME
    if not archive_path.exists():
        _download_asset(version, archive_path)
    else:
        log.info("Archive already exists: %s", archive_path)

    _extract_qlib_data(archive_path)

    # 清除缓存，确保后续读取使用最新数据
    global _QLIB_ROOT_CACHE, _CALENDAR_CACHE
    _QLIB_ROOT_CACHE = None
    _CALENDAR_CACHE = None

    return {"version": version, "status": "ok"}


# ---------------------------------------------------------------------------
# Feature parquet incremental update
# ---------------------------------------------------------------------------
def update_feature_parquet(since: str = "", until: str = "") -> dict:
    """增量更新 feature parquet（从 stock_daily_latest 补充缺失日期）。"""
    log.info("Updating feature parquet...")
    try:
        from backend.scripts.update_feature_parquet import (
            PARQUET_PATH, fetch_data, compute_all_features,
        )
    except ImportError as e:
        return {"status": "skipped", "reason": f"import failed: {e}"}

    if not PARQUET_PATH.exists():
        return {"status": "skipped", "reason": "parquet file not found"}

    import asyncio as _asyncio

    existing = pd.read_parquet(PARQUET_PATH, engine="pyarrow")
    existing["trade_date"] = pd.to_datetime(existing["trade_date"]).dt.date
    max_date = existing["trade_date"].max()

    since_date = date.fromisoformat(since) if since else max_date + timedelta(days=1)
    until_date = date.fromisoformat(until) if until else date.today()

    if since_date > until_date:
        log.info("Feature parquet already up to date (max=%s)", max_date)
        return {"status": "up_to_date", "max_date": str(max_date)}

    log.info("Feature parquet: updating %s to %s (existing max=%s)", since_date, until_date, max_date)

    db_df = _asyncio.run(fetch_data(since_date, until_date, lookback_days=120))
    if db_df.empty:
        return {"status": "skipped", "reason": "no new data in DB"}

    target_dates = set()
    d = since_date
    while d <= until_date:
        target_dates.add(d)
        d += timedelta(days=1)

    new_data = compute_all_features(db_df, target_dates)
    if new_data.empty:
        return {"status": "skipped", "reason": "no valid features computed"}

    # 合并：去掉重叠日期，追加新数据
    overlap = set(new_data["trade_date"].unique()) & set(existing["trade_date"].unique())
    if overlap:
        existing = existing[~existing["trade_date"].isin(overlap)]

    all_cols = list(dict.fromkeys(list(existing.columns) + [c for c in new_data.columns if c not in existing.columns]))
    new_data = new_data.reindex(columns=all_cols, fill_value=0)
    for c in all_cols:
        if c not in existing.columns:
            existing[c] = 0
    existing = existing.reindex(columns=all_cols, fill_value=0)

    combined = pd.concat([existing, new_data], ignore_index=True)
    combined = combined.sort_values(["trade_date", "symbol"]).reset_index(drop=True)
    combined.to_parquet(str(PARQUET_PATH), index=False, engine="pyarrow")

    log.info("Feature parquet updated: %d new rows, total %d rows",
             len(new_data), len(combined))
    return {
        "status": "ok",
        "new_rows": len(new_data),
        "total_rows": len(combined),
        "since": str(since_date),
        "until": str(until_date),
    }


# ---------------------------------------------------------------------------
# Sync status
# ---------------------------------------------------------------------------
def get_sync_status() -> dict:
    """获取当前同步状态摘要。"""
    engine = _get_engine()
    from sqlalchemy import text as sql_text

    with engine.begin() as conn:
        row = conn.execute(
            sql_text("""
                SELECT
                    MAX(trade_date) AS latest_date,
                    MIN(trade_date) AS earliest_date,
                    COUNT(DISTINCT symbol) AS symbol_count,
                    COUNT(*) AS total_rows
                FROM stock_daily_latest
            """)
        ).fetchone()

    qroot = _find_qlib_root()
    cal_info = {}
    if qroot:
        cal_path = qroot / "calendars" / "day.txt"
        if cal_path.exists():
            lines = cal_path.read_text().strip().splitlines()
            cal_info = {
                "calendar_start": lines[0].strip() if lines else None,
                "calendar_end": lines[-1].strip() if lines else None,
                "calendar_days": len(lines),
            }
        feat_dir = qroot / "features"
        if feat_dir.exists():
            cal_info["qlib_stocks"] = len(list(feat_dir.iterdir()))

    return {
        "pg_latest_date": row[0].isoformat() if row[0] else None,
        "pg_earliest_date": row[1].isoformat() if row[1] else None,
        "pg_symbol_count": row[2],
        "pg_total_rows": row[3],
        "investment_data_dir": str(INVESTMENT_DATA_DIR),
        "investment_data_exists": INVESTMENT_DATA_DIR.exists(),
        **cal_info,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="QuantMind 统一日常数据同步")
    parser.add_argument(
        "--market",
        default="A",
        choices=["A", "CN"],
        help="市场 (仅支持 A/CN。HK/US/Crypto 请用 update_market_features.py)",
    )
    parser.add_argument("--incremental", action="store_true", default=True,
                        help="增量模式（只补缺失天数）")
    parser.add_argument("--full", action="store_true", help="全量模式（重写）")
    parser.add_argument("--symbols", default="", help="指定股票（逗号分隔）")
    parser.add_argument("--no-qlib-bin", action="store_true", help="不更新 qlib bin")
    parser.add_argument("--no-calibrate", action="store_true", help="不校准指标")
    parser.add_argument("--calibrate-days", type=int, default=90, help="指标校准回溯天数")
    parser.add_argument("--update-investment-data", action="store_true",
                        help="仅更新 investment_data（下载最新 qlib_bin）")
    parser.add_argument("--calibrate-only", action="store_true", help="仅校准指标")
    parser.add_argument("--status", action="store_true", help="显示同步状态")
    parser.add_argument(
        "--start-date",
        default=None,
        help="partition 起始日期 YYYY-MM-DD（默认 2026-01-01，可用 env QM_SYNC_PARTITION_START）",
    )
    args = parser.parse_args()

    if args.status:
        import json
        status = get_sync_status()
        print(json.dumps(status, indent=2, ensure_ascii=False))
        return 0

    if args.update_investment_data:
        result = update_investment_data()
        log.info("Investment data updated: %s", result)
        return 0

    if args.calibrate_only:
        engine = _get_engine()
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()] if args.symbols else None
        _calibrate_indicators(engine, symbols=symbols, days=args.calibrate_days)
        return 0

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()] if args.symbols else None
    incremental = not args.full

    partition_start_arg: Optional[date] = None
    if args.start_date:
        try:
            partition_start_arg = date.fromisoformat(args.start_date)
        except ValueError:
            log.error("Invalid --start-date=%s (expect YYYY-MM-DD)", args.start_date)
            return 2

    result = run_sync(
        market=args.market,
        symbols=symbols,
        incremental=incremental,
        update_qlib=not args.no_qlib_bin,
        calibrate=not args.no_calibrate,
        calibrate_days=args.calibrate_days,
        partition_start=partition_start_arg,
    )

    log.info("=" * 60)
    log.info("Sync result:")
    log.info("  Mode: %s", result["mode"])
    log.info("  quantdb_parquet: %d symbols", result["quantdb_parquet_synced"])
    log.info("  baostock: %d symbols", result["baostock_synced"])
    log.info("  akshare: %d symbols", result["akshare_synced"])
    log.info("  eltdx: %d symbols", result["eltdx_synced"])
    log.info("  qlib_cache updated: %s", result["qlib_cache_updated"])
    log.info("  indicators calibrated: %s", result["indicators_calibrated"])
    if result["errors"]:
        log.warning("  errors: %d", len(result["errors"]))
        for e in result["errors"][:10]:
            log.warning("    - %s", e)
    log.info("=" * 60)

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted")
        sys.exit(1)
    except Exception as e:
        log.error("FATAL: %s", e, exc_info=True)
        sys.exit(1)
