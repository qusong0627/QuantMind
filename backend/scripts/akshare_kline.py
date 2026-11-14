#!/usr/bin/env python3
"""akshare 港美股日线 → QuantUS/QuantHK 增量同步脚本（akshare 主数据源）。

用 akshare 获取港股/美股日线（不复权全量历史），按 QuantDB 的 Hive 分区
格式增量落盘到本地 parquet。

市场:
  HK → data/quanthk/1_kline_data/daily_forward（代码 00700 → 0700.HK）
  US → data/quantus/1_kline_data/daily_forward（代码原样 AAPL）

增量逻辑:
  对每只股票，akshare 返回全量历史，按已落盘分区过滤后只写缺失部分。

用法:
  # 港股全量（最近 5 天）
  python backend/scripts/akshare_kline.py --market HK --days 5

  # 美股全量（SP500+NQ100）
  python backend/scripts/akshare_kline.py --market US --days 5

  # 指定股票
  python backend/scripts/akshare_kline.py --market HK --symbol 00700
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from backend.shared.kline_canonical import canonicalize  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("akshare_kline")

OUT_COLS = [
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

DEFAULT_THREADS = 6

MARKET_CONFIG = {
    "HK": {
        "env": "QM_QUANTHK_DATA_DIR",
        "default_dir": "/data/quanthk",
        "rel_dir": "1_kline_data/daily_forward",
    },
    "US": {
        "env": "QM_QUANTUS_DATA_DIR",
        "default_dir": "/data/quantus",
        "rel_dir": "1_kline_data/daily_forward",
    },
}


def _data_dir(market: str) -> Path:
    cfg = MARKET_CONFIG[market]
    env_val = os.getenv(cfg["env"], "").strip()
    if env_val:
        p = Path(env_val)
        p.mkdir(parents=True, exist_ok=True)
        return p
    p = Path(cfg["default_dir"])
    if p.is_dir():
        return p
    local = PROJECT_ROOT / "data" / ("quanthk" if market == "HK" else "quantus")
    local.mkdir(parents=True, exist_ok=True)
    return local


def _to_symbol(market: str, code: str) -> str:
    """代码标准化。HK: 5位→4位+.HK（容忍输入带 .HK 后缀）；US: 原样大写。"""
    code = code.strip()
    if market == "HK":
        code = code.split(".")[0].zfill(5)
        if code.startswith("0") and len(code) == 5:
            code = code[1:]
        return f"{code}.HK"
    return code.upper()


def _to_akshare_hk(symbol: str) -> str:
    """0001.HK → 00001。akshare 港股接口需要 5 位代码。"""
    return symbol.split(".")[0].zfill(5)


def _existing_partitions(market: str) -> set[str]:
    d = _data_dir(market) / MARKET_CONFIG[market]["rel_dir"]
    if not d.is_dir():
        return set()
    return {p.name[3:] for p in d.glob("dt=*")}


def _stock_list(market: str) -> list[str]:
    """代码池。HK: security_master 主表（新浪全市场，新股票自动进入）；US: US_SYMBOLS。"""
    if market == "HK":
        try:
            from backend.scripts.market_cn_names import _read_security_master

            mapping = _read_security_master("HK")
            if mapping:
                return sorted(mapping)
        except Exception as exc:  # noqa: BLE001
            log.warning("读取 security_master 失败，回退 hk.csv: %s", exc)
        for csv_path in (
            Path(__file__).parent / "hk.csv",
            Path("/data/hk.csv"),
            Path("/app/backend/scripts/hk.csv"),
        ):
            if csv_path.is_file():
                try:
                    df = pd.read_csv(csv_path, encoding="utf-8-sig")
                    if "id" in df.columns:
                        return df["id"].astype(str).str.zfill(5).tolist()
                except Exception:
                    pass
        return [
            "00001",
            "00002",
            "00005",
            "00006",
            "00700",
            "00939",
            "00941",
            "00998",
            "01299",
            "02318",
        ]
    else:
        try:
            from backend.services.engine.rd_agent.data_pipeline.us_data import (
                US_SYMBOLS,
            )

            if US_SYMBOLS:
                return list(US_SYMBOLS)
        except Exception as exc:  # noqa: BLE001
            log.warning("导入 US_SYMBOLS 失败: %s", exc)
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


def _fetch_stock(market: str, symbol: str, min_date: str) -> pd.DataFrame | None:
    """抓取单只股票全量日线（akshare，不复权）。"""
    import akshare as ak

    try:
        if market == "HK":
            df = ak.stock_hk_daily(symbol=_to_akshare_hk(symbol))
        else:
            df = ak.stock_us_daily(symbol=symbol, adjust="")
        if df is None or df.empty:
            return None
        df = df.rename(columns={"date": "time"})
        df["time"] = pd.to_datetime(df["time"], errors="coerce")
        df = df.dropna(subset=["time"])
        if min_date:
            df = df[df["time"].dt.strftime("%Y%m%d") >= min_date]
        if df.empty:
            return None
        df["symbol"] = _to_symbol(market, symbol)
        for c in ("open", "high", "low", "close"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df["volume"] = (
            pd.to_numeric(df["volume"], errors="coerce").fillna(0).astype("int64")
        )
        # 美股 akshare 无 amount，用 close*volume 估算（美元）；港股有 amount
        if "amount" not in df.columns:
            df["amount"] = pd.to_numeric(df["close"], errors="coerce") * pd.to_numeric(
                df["volume"], errors="coerce"
            ).fillna(0.0)
        else:
            df["amount"] = pd.to_numeric(df["amount"], errors="coerce").fillna(0.0)
        df["release_id"] = "akshare"
        df["published_at"] = datetime.now().isoformat(timespec="seconds")
        return df[OUT_COLS].dropna(subset=["close"])
    except Exception as exc:  # noqa: BLE001
        log.debug("抓取 %s %s 失败: %s", market, symbol, exc)
        return None


def sync(
    market: str,
    *,
    symbols: list[str] | None = None,
    days: int = 5,
    start_date: str | None = None,
    concurrent: int = DEFAULT_THREADS,
    dry_run: bool = False,
) -> dict:
    """增量同步 akshare 日线。

    start_date 指定历史起点（如 2001-01-01），akshare 返回全历史，
    按此过滤后写入 dt= 分区，实现全量回拉。不传则按 days 增量补最近。
    """
    market = market.upper()
    if market not in MARKET_CONFIG:
        raise ValueError(f"market 必须是 HK/US，收到 {market}")

    existing = _existing_partitions(market)
    end = date.today()
    if start_date:
        min_date = start_date.replace("-", "")
    else:
        start = end - timedelta(days=days * 1.7)
        min_date = start.strftime("%Y%m%d")

    syms = symbols or _stock_list(market)
    log.info(
        "[%s] 待同步标的: %d 只，从 %s (days=%s)", market, len(syms), min_date, days
    )

    if dry_run:
        return {
            "market": market,
            "stocks": len(syms),
            "min_date": min_date,
            "existing_partitions": len(existing),
            "dry_run": True,
        }

    frames: list[pd.DataFrame] = []
    ok = 0
    err = 0
    with ThreadPoolExecutor(max_workers=concurrent) as pool:
        futures = {pool.submit(_fetch_stock, market, s, min_date): s for s in syms}
        for future in as_completed(futures):
            df = future.result()
            if df is not None and not df.empty:
                frames.append(df)
                ok += 1
            else:
                err += 1

    if not frames:
        return {"market": market, "stocks": len(syms), "ok": ok, "err": err, "rows": 0}

    all_df = pd.concat(frames, ignore_index=True)
    target_dir = _data_dir(market) / MARKET_CONFIG[market]["rel_dir"]

    grouped = {
        ts.strftime("%Y%m%d"): g for ts, g in all_df.groupby(all_df["time"].dt.date)
    }
    written = 0
    for date_str, chunk in sorted(grouped.items()):
        dt_dir = target_dir / f"dt={date_str}"
        dt_dir.mkdir(parents=True, exist_ok=True)
        out = dt_dir / "data.parquet"
        if out.exists():
            old = pd.read_parquet(out)
            combined = pd.concat([old, chunk], ignore_index=True)
            # 一个股票日只留一行：paid_hk 历史优先于 akshare（见 kline_canonical）
            combined = canonicalize(combined)
            combined.to_parquet(out, index=False)
        else:
            chunk.to_parquet(out, index=False)
        written += 1

    return {
        "market": market,
        "stocks": len(syms),
        "ok": ok,
        "err": err,
        "rows": int(len(all_df)),
        "partitions_written": written,
        "start_date": min(grouped),
        "end_date": max(grouped),
        "target_dir": str(target_dir),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="akshare 港美股日线 → QuantUS/QuantHK")
    parser.add_argument("--market", required=True, choices=["HK", "US"], help="市场")
    parser.add_argument("--days", type=int, default=5, help="同步最近多少个交易日")
    parser.add_argument(
        "--start-date",
        default=None,
        help="历史起点 YYYY-MM-DD（全量回拉，如 2001-01-01）",
    )
    parser.add_argument("--symbol", default=None, help="指定股票代码，逗号分隔多个")
    parser.add_argument(
        "--concurrent", type=int, default=DEFAULT_THREADS, help="并发数"
    )
    parser.add_argument("--dry-run", action="store_true", help="仅预览，不抓取")
    args = parser.parse_args()

    try:
        syms = (
            [s.strip() for s in args.symbol.split(",") if s.strip()]
            if args.symbol
            else None
        )
        result = sync(
            args.market,
            symbols=syms,
            days=args.days,
            start_date=args.start_date,
            concurrent=args.concurrent,
            dry_run=args.dry_run,
        )
        print(result)
        return 0
    except Exception as exc:  # noqa: BLE001
        log.error("同步失败: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
