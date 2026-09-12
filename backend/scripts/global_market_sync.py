#!/usr/bin/env python3
"""Global market (US/HK) full sync — QuantUS / QuantHK 数据摄取核心。

从 yahoo_finance 拉取美股/港股全量数据，按 QuantDB 的格式落盘本地 parquet。

数据段:
  1. daily_kline  — 日线 OHLCV → 1_kline_data/daily_forward/dt=YYYYMMDD/
  2. dividend     — 分红历史 → 3_financial_data/dividend/{symbol}.parquet
  3. splits       — 拆股记录 → 3_financial_data/splits/{symbol}.parquet
  4. balance/income/cash_flow — 财务三表 → 3_financial_data/{type}/{symbol}.parquet
  5. valuation    — 估值快照 → 5_technical_derived/valuation/dt=YYYYMMDD/
  6. sector/f10   — 行业/基本面快照 → 2_base_sector/instrument_detail/
  7. recommendations/earnings_* — 分析师预期 → 4_analyst/
  8. major_holders/insider_transactions — 持仓/内部人 → 4_analyst/
  9. options_chain — 期权链 → 4_options/{symbol}.parquet
  10. calendar    — 分红/财报日历 → 4_analyst/calendar/

标的池:
  US: 复用 rd_agent data_pipeline 的 SP500_FULL + NASDAQ_EXTRA (~540只)
  HK: 复用 hk_data 的 HSI_CONSTITUENTS + HSTECH_EXTRA + HK_POPULAR (~82只)

用法:
  # 美股全量同步最近 5 个交易日（含所有数据段）
  python backend/scripts/global_market_sync.py --market US --days 5

  # 指定标的
  python backend/scripts/global_market_sync.py --market HK --symbols 0700.HK,9988.HK --days 5

  # 跳过慢速数据段（财务三表/持仓/期权）
  python backend/scripts/global_market_sync.py --market US --fast

数据目录:
  QM_QUANTUS_DATA_DIR  (默认 data/quantus/)  — 美股
  QM_QUANTHK_DATA_DIR  (默认 data/quanthk/)  — 港股
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("global_market_sync")

_MARKET_ENV = {"US": "QM_QUANTUS_DATA_DIR", "HK": "QM_QUANTHK_DATA_DIR"}
_MARKET_DEFAULT_DIR = {
    "US": "/data/quantus",
    "HK": "/data/quanthk",
}
_MARKET_LOCAL_DIR = {
    "US": str(PROJECT_ROOT / "data" / "quantus"),
    "HK": str(PROJECT_ROOT / "data" / "quanthk"),
}
SYNC_WORKERS = 8
FAST_WORKERS = 12
# 限流相关：财务/分析师/持仓等高频段用低并发 + 退避重试
SLOW_FIELDS_WORKERS = 3
RATE_LIMIT_MAX_RETRIES = 5  # 限流最大重试次数

# QuantDB 日线 schema（10 列）
KLINE_COLS = [
    "symbol",
    "time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "release_id",
    "published_at",
]

# 时间序列数据段 → 落盘到 3_financial_data/{dir}/{symbol}.parquet（增量追加）
# 键 = adapter 字段名，dir = 落盘目录名
SERIES_FIELDS = {
    "dividend": {"dir": "dividend"},
    "splits": {"dir": "splits"},
    "financial_report": {"dir": "balance"},
    "income_statement": {"dir": "income"},
    "cash_flow": {"dir": "cashflow"},
}

# 快照数据段 → 落盘到各目录（每标的每类一文件，按时间覆盖）
SNAPSHOT_FIELDS = {
    "valuation": "5_technical_derived/valuation",
    "sector": "2_base_sector/sector",
    "f10": "2_base_sector/f10",
    "recommendations": "4_analyst/recommendations",
    "upgrades_downgrades": "4_analyst/upgrades_downgrades",
    "earnings_history": "4_analyst/earnings_history",
    "earnings_dates": "4_analyst/earnings_dates",
    "earnings_estimate": "4_analyst/earnings_estimate",
    "revenue_estimate": "4_analyst/revenue_estimate",
    "growth_estimates": "4_analyst/growth_estimates",
    "analyst_price_targets": "4_analyst/analyst_price_targets",
    "major_holders": "4_analyst/major_holders",
    "mutual_fund_holders": "4_analyst/mutual_fund_holders",
    "calendar": "4_analyst/calendar",
    "insider_transactions": "4_analyst/insider_transactions",
    "options_chain": "4_options",
}


def _data_dir(market: str) -> Path:
    env_var = _MARKET_ENV[market]
    env_val = os.getenv(env_var, "").strip()
    if env_val:
        return Path(env_val)
    container_dir = Path(_MARKET_DEFAULT_DIR[market])
    if container_dir.is_dir():
        return container_dir
    local_dir = Path(_MARKET_LOCAL_DIR[market])
    local_dir.mkdir(parents=True, exist_ok=True)
    return local_dir


def _make_adapter():
    from backend.services.engine.data_platform.adapters.yahoo_finance_adapter import (
        YahooFinanceAdapter,
    )

    return YahooFinanceAdapter()


def _universe(market: str) -> list[str]:
    """获取全市场标的池。

    HK: security_master 主表（新浪全市场快照，新上市股票自动进入）
    US: rd_agent data_pipeline 的 US_SYMBOLS
    """
    if market == "HK":
        try:
            from backend.scripts.market_cn_names import _read_security_master

            mapping = _read_security_master("HK")
            if mapping:
                syms = sorted(mapping)
                log.info("HK 标的池来自 security_master: %d 只", len(syms))
                return syms
        except Exception as exc:  # noqa: BLE001
            log.warning("读取 security_master 失败，回退 HK_SYMBOLS: %s", exc)
    if market == "US":
        try:
            from backend.services.engine.rd_agent.data_pipeline.us_data import (
                US_SYMBOLS,
            )

            if US_SYMBOLS:
                return list(US_SYMBOLS)
        except Exception as exc:  # noqa: BLE001
            log.warning("导入 US_SYMBOLS 失败，回退内置列表: %s", exc)
        return [
            "AAPL",
            "MSFT",
            "GOOGL",
            "AMZN",
            "NVDA",
            "META",
            "TSLA",
            "BRK-B",
            "JPM",
            "V",
        ]
    if market == "HK":
        try:
            from backend.services.engine.rd_agent.data_pipeline.hk_data import (
                HK_SYMBOLS,
            )

            if HK_SYMBOLS:
                return list(HK_SYMBOLS)
        except Exception as exc:  # noqa: BLE001
            log.warning("导入 HK_SYMBOLS 失败，回退内置列表: %s", exc)
        return [
            "0700.HK",
            "9988.HK",
            "0388.HK",
            "0005.HK",
            "1299.HK",
            "2318.HK",
            "0941.HK",
            "1398.HK",
            "3988.HK",
            "0883.HK",
        ]
    raise ValueError(f"market 必须是 US/HK，收到 {market}")


def _resolve_symbols(market: str, symbols: str | None) -> list[str]:
    if symbols:
        return [s.strip().upper() for s in symbols.split(",") if s.strip()]
    return _universe(market)


def _fetch_daily_one(
    adapter, symbol: str, start: date, end: date
) -> pd.DataFrame | None:
    try:
        df = _retry_on_rate_limit(adapter.fetch_daily, symbol, start, end, adjust="qfq")
        if df is None or df.empty:
            return None
        return df
    except Exception as exc:  # noqa: BLE001
        log.warning("拉取 %s 日线失败: %s", symbol, exc)
        return None


def _normalise_kline(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """yahoo 输出 → QuantDB 日线 schema。"""
    out = pd.DataFrame(
        {
            "symbol": symbol,
            "time": pd.to_datetime(df["trade_date"]),
            "open": pd.to_numeric(df["open"], errors="coerce"),
            "high": pd.to_numeric(df["high"], errors="coerce"),
            "low": pd.to_numeric(df["low"], errors="coerce"),
            "close": pd.to_numeric(df["close"], errors="coerce"),
            "volume": pd.to_numeric(df["volume"], errors="coerce").fillna(0.0),
            "amount": pd.to_numeric(df["close"], errors="coerce")
            * pd.to_numeric(df["volume"], errors="coerce").fillna(0.0),
        }
    )
    out["release_id"] = "yahoo"
    out["published_at"] = datetime.now().isoformat(timespec="seconds")
    return out[KLINE_COLS].dropna(subset=["close"])


def _write_partition(
    root: Path,
    date_str: str,
    chunk: pd.DataFrame,
    dedup_cols: tuple[str, ...] | None = None,
) -> Path:
    """写单个 Hive 分区 dt=YYYYMMDD/data.parquet。"""
    dt_dir = root / f"dt={date_str}"
    dt_dir.mkdir(parents=True, exist_ok=True)
    target = dt_dir / "data.parquet"
    if target.exists():
        old = pd.read_parquet(target)
        combined = pd.concat([old, chunk], ignore_index=True)
        dedup_cols = dedup_cols or ("symbol", "time")
        combined = combined.drop_duplicates(subset=list(dedup_cols), keep="last")
        combined.to_parquet(target, index=False)
    else:
        chunk.to_parquet(target, index=False)
    return target


def _write_series_file(root: Path, symbol: str, df: pd.DataFrame) -> Path:
    """写时间序列标的文件 {root}/{symbol}.parquet，增量追加去重。"""
    root.mkdir(parents=True, exist_ok=True)
    target = root / f"{symbol}.parquet"
    # 确定去重键：report_date（财务）或 trade_date（分红/拆股）
    date_col = (
        "report_date"
        if "report_date" in df.columns
        else ("trade_date" if "trade_date" in df.columns else None)
    )
    if target.exists():
        old = pd.read_parquet(target)
        combined = pd.concat([old, df], ignore_index=True)
        if date_col and date_col in combined.columns:
            combined = combined.drop_duplicates(subset=[date_col], keep="last")
        combined.to_parquet(target, index=False)
    else:
        df.to_parquet(target, index=False)
    return target


def _write_snapshot_file(root: Path, symbol: str, df: pd.DataFrame) -> Path:
    """写快照文件 {root}/{symbol}.parquet（直接覆盖，保留最新快照）。"""
    root.mkdir(parents=True, exist_ok=True)
    target = root / f"{symbol}.parquet"
    df.to_parquet(target, index=False)
    return target


def _normalise_valuation(symbol: str, data: dict) -> pd.DataFrame | None:
    """yahoo info → QuantDB 估值 schema。

    入参 data 是 `YahooFinanceAdapter.fetch_field("valuation")` 返回行的字典，
    键名与该适配器输出保持一致（**snake_case**：market_cap / trailing_pe /
    price_to_book ...）。历史 bug：此处曾按 yfinance 原生 camelCase
    （marketCap / trailingPE）取值，两边对不上导致全部落成 NULL，
    且照常建分区写空行 —— 静默失败两周（2026-08-14 ~ 08-27 有值，08-28 起全空）。
    """
    try:
        row = {
            "symbol": symbol,
            "time": pd.Timestamp.now().normalize(),
            # 适配器不返回行情价，留空而不是填 0（0 会被下游当真实价格）
            "close": None,
            "total_capital": None,
            "circulating_capital": None,
            "total_mv": _opt_float(data.get("market_cap")),
            "float_mv": None,
            "net_profit_ttm": _opt_float(data.get("net_income")),
            "revenue_ttm": _opt_float(data.get("revenue")),
            "equity": None,
            "annual_net_profit": None,
            "pe_ttm": _opt_float(data.get("trailing_pe")),
            "pe_static": None,
            "pb": _opt_float(data.get("price_to_book")),
            "ps_ttm": _opt_float(data.get("price_to_sales")),
            "dividend_rate": None,
            "release_id": "yahoo",
            "published_at": datetime.now().isoformat(timespec="seconds"),
        }
        if row["total_mv"] is None and row["pe_ttm"] is None and row["pb"] is None:
            # 三个核心字段全空 = 上游 schema 又对不上了，不能静默写空分区
            log.warning("估值标准化 %s: 核心字段全空，疑似字段名漂移", symbol)
        return pd.DataFrame([row])
    except Exception as exc:  # noqa: BLE001
        log.warning("估值标准化失败 %s: %s", symbol, exc)
        return None


def _opt_float(value: object) -> float | None:
    """安全转 float：None / NaN / 空串 → None（不落 0，避免下游当真值）。"""
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(out) else out


def _is_rate_limited(exc: Exception) -> bool:
    """判断是否 yfinance 限流错误。"""
    msg = str(exc)
    return "Too Many Requests" in msg or "429" in msg or "Rate limited" in msg


def _retry_on_rate_limit(fn, *args, retries: int = RATE_LIMIT_MAX_RETRIES, **kwargs):
    """调用 fn，遇限流则指数退避重试。

    限流窗口通常持续 10-60 分钟，等待从 30s 指数退避到 5 分钟，
    让单标的请求在限流期内自我恢复（配合低并发）。
    """
    wait = 30
    for attempt in range(retries):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            if not _is_rate_limited(exc) or attempt == retries - 1:
                raise
            log.warning(
                "限流(%s)，等待 %.0fs 后重试 %s",
                args[-1] if args else "?",
                wait,
                type(exc).__name__,
            )
            time.sleep(wait)
            wait = min(wait * 2, 300)
    return None


def _fetch_series_one(adapter, field: str, symbol: str) -> pd.DataFrame | None:
    """拉取单个时间序列字段（限流自动退避重试）。"""
    try:
        df = _retry_on_rate_limit(adapter.fetch_field, field, symbol)
        if df is None or df.empty:
            return None
        return df
    except Exception as exc:  # noqa: BLE001
        log.debug("拉取 %s %s 失败: %s", symbol, field, exc)
        return None


def sync_kline(
    market: str,
    symbols: list[str],
    start: date,
    end: date,
    *,
    workers: int = SYNC_WORKERS,
) -> dict:
    """拉取日线并按交易日分区落盘。"""
    adapter = _make_adapter()
    root = _data_dir(market) / "1_kline_data" / "daily_forward"
    root.mkdir(parents=True, exist_ok=True)

    frames = []
    errors = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_fetch_daily_one, adapter, s, start, end): s for s in symbols
        }
        for future in as_completed(futures):
            symbol = futures[future]
            df = future.result()
            if df is not None and not df.empty:
                frames.append(_normalise_kline(df, symbol))
            else:
                errors += 1

    if not frames:
        return {
            "market": market,
            "symbols": len(symbols),
            "rows": 0,
            "partitions": 0,
            "errors": errors,
        }

    all_df = pd.concat(frames, ignore_index=True)
    grouped = {
        ts.strftime("%Y%m%d"): g for ts, g in all_df.groupby(all_df["time"].dt.date)
    }
    written = 0
    for date_str, chunk in sorted(grouped.items()):
        _write_partition(root, date_str, chunk)
        written += 1

    return {
        "market": market,
        "symbols": len(symbols),
        "rows": int(len(all_df)),
        "partitions": written,
        "errors": errors,
        "start_date": min(grouped),
        "end_date": max(grouped),
    }


def sync_series(
    market: str, symbols: list[str], field: str, *, workers: int = SLOW_FIELDS_WORKERS
) -> dict:
    """拉取时间序列字段（分红/拆股/财务三表）到标的级 parquet。"""
    adapter = _make_adapter()
    rel_dir = "3_financial_data/" + SERIES_FIELDS[field]["dir"]
    root = _data_dir(market) / rel_dir

    written = 0
    errors = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_fetch_series_one, adapter, field, s): s for s in symbols
        }
        for future in as_completed(futures):
            symbol = futures[future]
            df = future.result()
            if df is not None and not df.empty:
                try:
                    _write_series_file(root, symbol, df)
                    written += 1
                except Exception as exc:  # noqa: BLE001
                    log.warning("写入 %s %s 失败: %s", symbol, field, exc)
                    errors += 1
            else:
                errors += 1

    return {
        "field": field,
        "symbols": len(symbols),
        "written": written,
        "errors": errors,
        "dir": rel_dir,
    }


def _fetch_snapshot_one(adapter, field: str, symbol: str) -> pd.DataFrame | None:
    """拉取单个快照字段（限流自动退避重试）。"""
    try:
        df = _retry_on_rate_limit(adapter.fetch_field, field, symbol)
        if df is None or df.empty:
            return None
        return df
    except Exception as exc:  # noqa: BLE001
        log.debug("拉取 %s %s 快照失败: %s", symbol, field, exc)
        return None


def sync_snapshot(
    market: str, symbols: list[str], field: str, *, workers: int = SLOW_FIELDS_WORKERS
) -> dict:
    """拉取快照字段（估值/评级/持仓/期权）到标的级文件。"""
    adapter = _make_adapter()
    rel_dir = SNAPSHOT_FIELDS[field]
    root = _data_dir(market) / rel_dir

    # 估值是分区格式，走 _write_partition
    if field == "valuation":
        frames = []

        def _fetch_val(symbol: str):
            try:
                df = adapter.fetch_field("valuation", symbol)
                if df is None or df.empty:
                    return None
                return _normalise_valuation(symbol, df.iloc[0].to_dict())
            except Exception as exc:  # noqa: BLE001
                log.debug("拉取 %s 估值失败: %s", symbol, exc)
                return None

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_fetch_val, s): s for s in symbols}
            for future in as_completed(futures):
                df = future.result()
                if df is not None and not df.empty:
                    frames.append(df)
        if not frames:
            return {"field": field, "written": 0, "errors": len(symbols)}
        all_df = pd.concat(frames, ignore_index=True)
        max_date = all_df["time"].max().strftime("%Y%m%d")
        _write_partition(root, max_date, all_df)
        return {
            "field": field,
            "written": len(all_df),
            "partition": max_date,
            "dir": rel_dir,
        }

    written = 0
    errors = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_fetch_snapshot_one, adapter, field, s): s for s in symbols
        }
        for future in as_completed(futures):
            symbol = futures[future]
            df = future.result()
            if df is not None and not df.empty:
                try:
                    _write_snapshot_file(root, symbol, df)
                    written += 1
                except Exception as exc:  # noqa: BLE001
                    log.warning("写入 %s %s 快照失败: %s", symbol, field, exc)
                    errors += 1
            else:
                errors += 1

    return {
        "field": field,
        "symbols": len(symbols),
        "written": written,
        "errors": errors,
        "dir": rel_dir,
    }


def _business_days(end: date, n: int) -> list[date]:
    """取最近 n 个工作日（粗略，不含假期）。"""
    days: list[date] = []
    d = end
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    return days


def run(
    market: str,
    *,
    days: int = 5,
    symbols: str | None = None,
    fast: bool = False,
    skip_kline: bool = False,
) -> dict:
    """全量同步：日线 + 时间序列 + 快照。

    Args:
        market: US / HK
        days: 同步最近多少个交易日（仅日线使用，快照/时序用全量）
        symbols: 指定标的（默认全市场）
        fast: True 时跳过慢速数据段（财务三表/持仓/期权/内部人）
        skip_kline: 跳过日线（仅同步数据段）
    """
    market = market.upper()
    if market not in _MARKET_ENV:
        raise ValueError(f"market 必须是 US/HK，收到 {market}")

    # 数据源配置：勾选的才同步（akshare/ccass/南向/北向/雅虎）
    from backend.shared.data_source_config import is_source_enabled

    yahoo_enabled = is_source_enabled(market, "yahoo")
    akshare_enabled = is_source_enabled(market, "akshare")
    ccass_enabled = market == "HK" and is_source_enabled(market, "ccass")
    south_enabled = market == "HK" and is_source_enabled(market, "hsgt_south")
    log.info(
        "[%s] 数据源勾选: akshare=%s ccass=%s 南向=%s yahoo=%s",
        market,
        akshare_enabled,
        ccass_enabled,
        south_enabled,
        yahoo_enabled,
    )

    syms = _resolve_symbols(market, symbols)
    log.info("[%s] 同步 %d 个标的，最近 %d 天", market, len(syms), days)

    result: dict = {"market": market, "symbols": len(syms), "sources": {}}

    # akshare 日线（主数据源，港股/美股 K 线）
    if akshare_enabled:
        try:
            from backend.scripts.akshare_kline import sync as akshare_sync

            r = akshare_sync(market, symbols=syms, days=days)
            result["sources"]["akshare"] = r
            log.info("[%s] akshare 日线同步完成: %s", market, r)
        except Exception as exc:  # noqa: BLE001
            log.warning("[%s] akshare 日线同步失败: %s", market, exc)
            result["sources"]["akshare"] = {"status": "error", "error": str(exc)}

    # 港股 CCASS 机构持仓
    if ccass_enabled:
        try:
            from backend.scripts.quanthk_ccass_sync import run as ccass_sync

            r = ccass_sync(days=days)
            result["sources"]["ccass"] = r
            log.info("[%s] CCASS 同步完成: %s", market, r)
        except Exception as exc:  # noqa: BLE001
            log.warning("[%s] CCASS 同步失败: %s", market, exc)
            result["sources"]["ccass"] = {"status": "error", "error": str(exc)}

    # 港股南向资金（港股通）
    if south_enabled:
        try:
            from backend.scripts.quanthk_south_sync import sync as south_sync

            r = south_sync(days=days)
            result["sources"]["hsgt_south"] = r
            log.info("[%s] 南向资金同步完成: %s", market, r)
        except Exception as exc:  # noqa: BLE001
            log.warning("[%s] 南向资金同步失败: %s", market, exc)
            result["sources"]["hsgt_south"] = {"status": "error", "error": str(exc)}

    # 证券主表 + f10 中文名回填（HK: 新浪全市场 / US: 腾讯行情）。
    # 放在 yahoo 段之前：即使雅虎未勾选，主表与中文名也保持新鲜。
    try:
        from backend.scripts.market_cn_names import (
            backfill as cn_backfill,
            build_security_master as cn_master,
        )

        master_r = cn_master(market)
        backfill_r = cn_backfill(market, syms, rebuild_master=False)
        result["cn_names"] = {"master": master_r, "backfill": backfill_r}
        log.info("[%s] 证券主表=%s 中文名回填=%s", market, master_r, backfill_r)
    except Exception as exc:  # noqa: BLE001
        log.warning("[%s] 中文名回填失败: %s", market, exc)
        result["cn_names"] = {"status": "error", "error": str(exc)}

    # yahoo 数据段（雅虎勾选才同步：估值/财务/分析师/持仓等）
    if not yahoo_enabled:
        log.info("[%s] 雅虎数据源未启用（数据源配置），跳过 yahoo 同步", market)
        return result

    if not skip_kline:
        end = date.today()
        start = min(_business_days(end, days))
        kline = sync_kline(market, syms, start, end)
        result["kline"] = kline
        log.info("[%s] K线同步完成: %s", market, kline)

    # 时间序列字段（分红/拆股/财务三表）
    series_fields = list(SERIES_FIELDS.keys())
    if fast:
        series_fields = [f for f in series_fields if f in ("dividend", "splits")]
    result["series"] = {}
    for i, field in enumerate(series_fields):
        r = sync_series(market, syms, field)
        result["series"][field] = r
        log.info("[%s] %s 同步完成: %s", market, field, r)
        # 字段间冷却，避免连续高频请求触发限流
        if i < len(series_fields) - 1:
            time.sleep(5)

    # 快照字段（估值/评级/持仓/期权）
    snapshot_fields = list(SNAPSHOT_FIELDS.keys())
    if fast:
        snapshot_fields = ["valuation", "sector", "f10", "recommendations"]
    result["snapshots"] = {}
    for i, field in enumerate(snapshot_fields):
        r = sync_snapshot(market, syms, field)
        result["snapshots"][field] = r
        log.info("[%s] %s 快照完成: %s", market, field, r)
        if i < len(snapshot_fields) - 1:
            time.sleep(5)

    # yahoo f10 的 name 是英文长名，同步后再次回填中文名（防覆盖）
    try:
        from backend.scripts.market_cn_names import backfill as cn_backfill

        r = cn_backfill(market, syms, rebuild_master=False)
        result["cn_names"]["backfill_after_yahoo"] = r
        log.info("[%s] 中文名回填（yahoo 后）: %s", market, r)
    except Exception as exc:  # noqa: BLE001
        log.warning("[%s] 中文名回填（yahoo 后）失败: %s", market, exc)

    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="QuantUS/QuantHK 全量数据摄取")
    parser.add_argument(
        "--market", required=True, choices=["US", "HK"], help="市场: US=美股, HK=港股"
    )
    parser.add_argument(
        "--days", type=int, default=5, help="同步最近多少个交易日（日线）"
    )
    parser.add_argument("--symbols", default=None, help="逗号分隔标的池（默认全市场）")
    parser.add_argument(
        "--fast", action="store_true", help="跳过慢速数据段（财务三表/持仓/期权）"
    )
    parser.add_argument("--skip-kline", action="store_true", help="跳过日线")
    args = parser.parse_args()

    try:
        result = run(
            args.market,
            days=args.days,
            symbols=args.symbols,
            fast=args.fast,
            skip_kline=args.skip_kline,
        )
        print(result)
        return 0
    except Exception as exc:  # noqa: BLE001
        log.error("同步失败: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
