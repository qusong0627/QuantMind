#!/usr/bin/env python3
"""QuantFutures 期权数据集 — 上交所/深交所 ETF 期权 EOD。

数据源（akshare，交易所官方口径）:
  - 上交所逐合约风险指标: ``option_risk_indicator_sse(date=YYYYMMDD)``
    Delta/Gamma/Theta/Vega/Rho + 隐含波动率
  - 上交所每日统计: ``option_daily_stats_sse(date=YYYYMMDD)``
    按标的成交量/认购认沽/PCR/持仓
  - 深交所每日统计: ``option_daily_stats_szse(date=YYYYMMDD)``
    按标的成交量/PCR/持仓

落盘（data/quantfutures/2_base_sector/）:
  options_risk/dt=YYYYMMDD/data.parquet   逐合约 Greeks/IV（SSE）
  options_stats/dt=YYYYMMDD/data.parquet  按标的汇总（SSE + SZSE）

覆盖边界:
  - SSE 逐合约风险指标至少 2016-01 起；50ETF 最早，300ETF/500ETF/科创50
    随上市日起才有数据。
  - SSE 汇总 2016-01 起；SZSE 汇总 2019-12-23 起（159919 期权上市；
    创业板/中证500 期权 2022-09-19 起才有）。
  - **不含逐合约 OHLC/成交量**；不含 CFFEX（akshare 1.18.88 的
    ``option_cffex_*_daily_sina`` 报 SyntaxError，需另找官方源）。
  - 期权 T 日数据一般 T 日盘后发布；每日增量默认重写最近 N 个交易日，
    以补齐延迟发布/停市日。

用法:
  # 每日增量（最近 5 个交易日）
  python backend/scripts/akshare_futures_options.py --task all --days 5

  # 10 年回填（默认跳过已有分区，可断点续跑）
  python backend/scripts/akshare_futures_options.py --task all \
      --start 2016-01-01 --end 2026-10-27

  # 强制重写
  python backend/scripts/akshare_futures_options.py --task all \
      --start 2016-01-01 --end 2026-10-27 --force
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import requests

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

log = logging.getLogger("futures_options")

_RELEASE = "akshare"
_SZSE_START = "20191223"
_KLINE_DIR = "1_kline_data/daily_forward"
_RISK_DIR = "2_base_sector/options_risk"
_STATS_DIR = "2_base_sector/options_stats"

UNDERLYING_NAMES: dict[str, str] = {
    "510050": "上证50ETF",
    "510300": "沪深300ETF",
    "510500": "中证500ETF",
    "588000": "科创50ETF",
    "588080": "科创50ETF易方达",
    "159901": "深证100ETF",
    "159915": "创业板ETF",
    "159919": "沪深300ETF嘉实",
    "159922": "中证500ETF嘉实",
}

_CONTRACT_RE = re.compile(
    r"^(?P<underlying>\d{6})(?P<opt_type>[CP])(?P<expiry_ym>\d{4})M(?P<strike>\d+)$"
)

_RISK_REN = {
    "TRADE_DATE": "trade_date",
    "SECURITY_ID": "security_id",
    "CONTRACT_ID": "contract_id",
    "CONTRACT_SYMBOL": "contract_name",
    "DELTA_VALUE": "delta",
    "THETA_VALUE": "theta",
    "GAMMA_VALUE": "gamma",
    "VEGA_VALUE": "vega",
    "RHO_VALUE": "rho",
    "IMPLC_VOLATLTY": "implied_vol",
}

_SSE_STATS_REN = {
    "合约标的代码": "underlying",
    "合约标的名称": "underlying_name",
    "合约数量": "n_contracts",
    "总成交额": "amount_wan",
    "总成交量": "volume",
    "认购成交量": "call_volume",
    "认沽成交量": "put_volume",
    "认沽/认购": "pcr_volume_pct",
    "未平仓合约总数": "open_interest",
    "未平仓认购合约数": "call_oi",
    "未平仓认沽合约数": "put_oi",
    "交易日": "trade_date",
}

_SZSE_STATS_REN = {
    "合约标的代码": "underlying",
    "合约标的名称": "underlying_name",
    "成交量": "volume",
    "认购成交量": "call_volume",
    "认沽成交量": "put_volume",
    "认沽/认购持仓比": "pcr_oi_pct",
    "未平仓合约总数": "open_interest",
    "未平仓认购合约数": "call_oi",
    "未平仓认沽合约数": "put_oi",
    "交易日": "trade_date",
}

_RISK_COLS = [
    "trade_date",
    "underlying",
    "underlying_name",
    "contract_id",
    "security_id",
    "contract_name",
    "option_type",
    "strike",
    "expiry_month",
    "delta",
    "gamma",
    "theta",
    "vega",
    "rho",
    "implied_vol",
    "source",
    "release_id",
    "published_at",
]

_STATS_COLS = [
    "trade_date",
    "exchange",
    "underlying",
    "underlying_name",
    "n_contracts",
    "amount_wan",
    "volume",
    "call_volume",
    "put_volume",
    "pcr_volume",
    "open_interest",
    "call_oi",
    "put_oi",
    "pcr_oi",
    "source",
    "release_id",
    "published_at",
]


def _data_dir() -> Path:
    env = os.getenv("QM_QUANTFUTURES_DATA_DIR", "").strip()
    if env:
        return Path(env)
    for candidate in (
        Path("/data/quantfutures"),
        PROJECT_ROOT / "data" / "quantfutures",
    ):
        if candidate.is_dir():
            return candidate
    return PROJECT_ROOT / "data" / "quantfutures"


FUT_DATA_DIR = _data_dir()


@contextmanager
def _request_timeout(seconds: float = 20.0):
    """给 akshare 内部的 requests.get 加超时，避免单日请求无限挂起。"""
    original = requests.get

    def _get(*args, **kwargs):
        kwargs.setdefault("timeout", seconds)
        return original(*args, **kwargs)

    requests.get = _get
    try:
        yield
    finally:
        requests.get = original


def _retry(fn: Callable[[], Any], *, tries: int = 3, delay: float = 0.8) -> Any:
    """交易所接口偶发断连；退避重试。"""
    last: Exception | None = None
    for i in range(tries):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            last = exc
            if i < tries - 1:
                time.sleep(delay * (i + 1))
    assert last is not None
    raise last


def _partition_dates() -> list[str]:
    d = FUT_DATA_DIR / _KLINE_DIR
    if not d.is_dir():
        return []
    return sorted(
        p.name[3:]
        for p in d.iterdir()
        if p.is_dir() and p.name.startswith("dt=") and p.name[3:].isdigit()
    )


def recent_trading_days(days: int) -> list[str]:
    """最近 N 个交易日（以期货日K分区为日历）。"""
    dates = _partition_dates()
    if not dates:
        raise RuntimeError(
            f"期货日K分区不存在，无法确定交易日: {FUT_DATA_DIR / _KLINE_DIR}"
        )
    if days <= 0:
        return dates
    return dates[-days:]


def trading_days_between(start: str, end: str | None = None) -> list[str]:
    """[start, end] 内的交易日；以期货日K分区为日历。"""
    start_s = pd.Timestamp(start).strftime("%Y%m%d")
    end_s = pd.Timestamp(end).strftime("%Y%m%d") if end else "99999999"
    dates = [d for d in _partition_dates() if start_s <= d <= end_s]
    if dates:
        return dates
    # 分区缺失时退回工作日；调用方会因源端休市返回空而跳过。
    fallback_end = pd.Timestamp(end) if end else pd.Timestamp.today()
    return [
        d.strftime("%Y%m%d")
        for d in pd.bdate_range(pd.Timestamp(start_s), fallback_end)
    ]


def _write_part(out_dir: Path, d: str, df: pd.DataFrame) -> None:
    """原子写分区：先写临时文件再 os.replace，避免并发/中断留半文件。"""
    pdir = out_dir / f"dt={d}"
    pdir.mkdir(parents=True, exist_ok=True)
    tmp = pdir / f".data.{os.getpid()}.tmp"
    try:
        df.to_parquet(tmp, index=False)
        os.replace(tmp, pdir / "data.parquet")
    except Exception:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        if not (pdir / "data.parquet").exists():
            pdir.rmdir()
        raise


def _stamp(df: pd.DataFrame) -> pd.DataFrame:
    df["release_id"] = _RELEASE
    df["published_at"] = pd.Timestamp.now().isoformat()
    return df


def _expiry_month(ym: Any) -> Any:
    if not isinstance(ym, str) or len(ym) != 4 or not ym.isdigit():
        return np.nan
    return f"20{ym[:2]}-{ym[2:]}"


def _norm_risk(raw: pd.DataFrame, trade_date: str) -> pd.DataFrame:
    """SSE 逐合约风险指标 → 长表。"""
    if raw is None or raw.empty:
        return pd.DataFrame(columns=_RISK_COLS)
    df = raw.rename(columns=_RISK_REN).copy()
    for c in ("trade_date", "security_id", "contract_id", "contract_name"):
        if c not in df.columns:
            df[c] = np.nan
    df["contract_id"] = (
        df["contract_id"]
        .where(df["contract_id"].notna())
        .astype("string")
        .str.upper()
        .str.strip()
    )
    ex = df["contract_id"].str.extract(_CONTRACT_RE)
    df["underlying"] = ex["underlying"]
    df["option_type"] = ex["opt_type"].map({"C": "call", "P": "put"})
    df["expiry_month"] = ex["expiry_ym"].map(_expiry_month)
    df["strike"] = pd.to_numeric(ex["strike"], errors="coerce") / 1000.0
    for c in ("delta", "gamma", "theta", "vega", "rho", "implied_vol"):
        if c not in df.columns:
            df[c] = np.nan
        df[c] = pd.to_numeric(df[c], errors="coerce")
    # 源端对深度实值/无成交合约给 0；0 不是有效 IV，避免污染 ATM/skew。
    df.loc[df["implied_vol"] <= 0, "implied_vol"] = np.nan
    df["underlying_name"] = df["underlying"].map(UNDERLYING_NAMES)
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
    df["trade_date"] = df["trade_date"].fillna(pd.Timestamp(trade_date))
    df["security_id"] = df["security_id"].astype(str)
    df["source"] = "sse"
    df = df.dropna(subset=["contract_id", "underlying"])
    df = df.drop_duplicates(["trade_date", "contract_id"], keep="last")
    df = _stamp(df)
    return df[_RISK_COLS]


def _norm_stats(raw: pd.DataFrame, exchange: str, trade_date: str) -> pd.DataFrame:
    """SSE/SZSE 每日统计 → 统一宽表。"""
    if raw is None or raw.empty:
        return pd.DataFrame(columns=_STATS_COLS)
    ren = _SSE_STATS_REN if exchange == "SSE" else _SZSE_STATS_REN
    df = raw.rename(columns=ren).copy()
    for c in (
        "underlying",
        "underlying_name",
        "n_contracts",
        "amount_wan",
        "volume",
        "call_volume",
        "put_volume",
        "pcr_volume_pct",
        "open_interest",
        "call_oi",
        "put_oi",
        "pcr_oi_pct",
        "trade_date",
    ):
        if c not in df.columns:
            df[c] = np.nan
    df["underlying"] = (
        df["underlying"].astype(str).str.extract(r"(\d{6})", expand=False)
    )
    for c in (
        "n_contracts",
        "amount_wan",
        "volume",
        "call_volume",
        "put_volume",
        "open_interest",
        "call_oi",
        "put_oi",
        "pcr_volume_pct",
        "pcr_oi_pct",
    ):
        df[c] = pd.to_numeric(
            df[c].astype(str).str.replace(",", "", regex=False), errors="coerce"
        )
    df["pcr_volume"] = df["put_volume"] / df["call_volume"].replace(0, np.nan)
    df["pcr_volume"] = df["pcr_volume"].fillna(df["pcr_volume_pct"] / 100.0)
    df["pcr_oi"] = df["put_oi"] / df["call_oi"].replace(0, np.nan)
    df["pcr_oi"] = df["pcr_oi"].fillna(df["pcr_oi_pct"] / 100.0)
    df["underlying_name"] = df["underlying_name"].fillna(
        df["underlying"].map(UNDERLYING_NAMES)
    )
    df["trade_date"] = pd.to_datetime(
        df["trade_date"].astype(str), errors="coerce"
    ).fillna(pd.Timestamp(trade_date))
    df["exchange"] = exchange
    df["source"] = exchange.lower()
    df = df.dropna(subset=["underlying"])
    df = df.drop_duplicates(["trade_date", "exchange", "underlying"], keep="last")
    df = _stamp(df)
    return df[_STATS_COLS]


def _fetch_risk_day(d: str) -> tuple[pd.DataFrame, list[str]]:
    import akshare as ak

    def _call() -> pd.DataFrame:
        try:
            return ak.option_risk_indicator_sse(date=d)
        except KeyError:
            # 休市日源端返回空 result，akshare 取列时抛 KeyError；按空数据处理。
            return pd.DataFrame()

    with _request_timeout():
        raw = _retry(_call)
    return _norm_risk(raw, d), []


def _fetch_szse_stats_raw(date: str) -> pd.DataFrame:
    """SZSE 日度概况直连（akshare 原实现无 UA，易被断连）。"""
    url = "http://investor.szse.cn/api/report/ShowReport/data"
    params = {
        "SHOWTYPE": "JSON",
        "CATALOGID": "ysprdzb",
        "TABKEY": "tab1",
        "txtQueryDate": f"{date[:4]}-{date[4:6]}-{date[6:]}",
        "random": str(time.time()),
    }
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
        ),
        "Referer": "https://investor.szse.cn/market/option/day/index.html",
        "Accept": "application/json, text/javascript, */*; q=0.01",
    }
    r = requests.get(url, params=params, headers=headers, timeout=20)
    payload = r.json()
    if not payload or not payload[0].get("data"):
        return pd.DataFrame()
    df = pd.DataFrame(payload[0]["data"])
    ren = {
        "bddm": "合约标的代码",
        "bdmc": "合约标的名称",
        "cjl": "成交量",
        "rccjl": "认购成交量",
        "rpcjl": "认沽成交量",
        "rcrpccb": "认沽/认购持仓比",
        "wpchyzs": "未平仓合约总数",
        "wpcrchys": "未平仓认购合约数",
        "wpcrphys": "未平仓认沽合约数",
    }
    df = df.rename(columns=ren)
    keep = [
        "合约标的代码",
        "合约标的名称",
        "成交量",
        "认购成交量",
        "认沽成交量",
        "认沽/认购持仓比",
        "未平仓合约总数",
        "未平仓认购合约数",
        "未平仓认沽合约数",
    ]
    for c in keep:
        if c not in df.columns:
            return pd.DataFrame()
    df = df[keep].copy()
    df["交易日"] = f"{date[:4]}-{date[4:6]}-{date[6:]}"
    return df


def _fetch_stats_day(d: str) -> tuple[pd.DataFrame, list[str]]:
    import akshare as ak

    frames: list[pd.DataFrame] = []
    errors: list[str] = []
    for exchange, fn in (
        ("SSE", ak.option_daily_stats_sse),
        ("SZSE", _fetch_szse_stats_raw),
    ):
        # 深市 159919 期权 2019-12-23 上市；更早日期接口会因缺列抛 KeyError。
        if exchange == "SZSE" and d < _SZSE_START:
            continue

        def _call(fn=fn) -> pd.DataFrame:
            try:
                return fn(date=d)
            except KeyError:
                # 休市日/未上市日期源端无列，按空数据处理。
                return pd.DataFrame()

        try:
            with _request_timeout():
                raw = _retry(_call, tries=4, delay=1.0)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{exchange}: {exc}")
            continue
        if raw is not None and not raw.empty:
            frames.append(_norm_stats(raw, exchange, d))
    if not frames:
        return pd.DataFrame(columns=_STATS_COLS), errors
    return pd.concat(frames, ignore_index=True), errors


def _sync_dates(
    dates: Iterable[str],
    kind: str,
    *,
    overwrite: bool,
    sleep: float,
) -> dict[str, Any]:
    out_dir = FUT_DATA_DIR / (_RISK_DIR if kind == "risk" else _STATS_DIR)
    fetch = _fetch_risk_day if kind == "risk" else _fetch_stats_day
    dates = list(dates)
    written = skipped = empty = 0
    errors: list[str] = []
    for i, d in enumerate(dates, 1):
        part = out_dir / f"dt={d}" / "data.parquet"
        if part.exists() and not overwrite:
            skipped += 1
            continue
        try:
            df, errs = fetch(d)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{d}: {exc}")
            if sleep:
                time.sleep(sleep)
            continue
        errors.extend(f"{d}: {e}" for e in errs)
        if df.empty:
            empty += 1
            if sleep:
                time.sleep(sleep)
            continue
        _write_part(out_dir, d, df)
        written += 1
        if i % 50 == 0:
            log.info(
                "%s 进度 %d/%d（写 %d / 空 %d）", kind, i, len(dates), written, empty
            )
        if sleep:
            time.sleep(sleep)
    status = "ok"
    if errors and written == 0:
        status = "error"
    elif errors:
        status = "partial"
    return {
        "status": status,
        "dataset": f"options_{kind}",
        "dates": len(dates),
        "written": written,
        "skipped": skipped,
        "empty": empty,
        "error_count": len(errors),
        "errors": errors[:20],
        "last_date": dates[-1] if dates else None,
    }


def task_options_risk(days: int = 5) -> dict[str, Any]:
    """每日增量：最近 N 个交易日的逐合约风险指标。"""
    return _sync_dates(recent_trading_days(days), "risk", overwrite=True, sleep=0.05)


def task_options_stats(days: int = 5) -> dict[str, Any]:
    """每日增量：最近 N 个交易日的按标的汇总。"""
    return _sync_dates(recent_trading_days(days), "stats", overwrite=True, sleep=0.05)


def backfill(
    *,
    start: str,
    end: str | None = None,
    task: str = "all",
    force: bool = False,
    sleep: float = 0.2,
) -> dict[str, Any]:
    """按交易日区间回填；默认跳过已有分区，可断点续跑。"""
    dates = trading_days_between(start, end)
    out: dict[str, Any] = {
        "start": dates[0] if dates else None,
        "end": dates[-1] if dates else None,
        "dates": len(dates),
    }
    if task in ("all", "risk"):
        out["risk"] = _sync_dates(dates, "risk", overwrite=force, sleep=sleep)
    if task in ("all", "stats"):
        out["stats"] = _sync_dates(dates, "stats", overwrite=force, sleep=sleep)
    return out


def _cli() -> int:
    parser = argparse.ArgumentParser(description="QuantFutures 期权 EOD")
    parser.add_argument("--task", choices=("risk", "stats", "all"), default="all")
    parser.add_argument("--days", type=int, default=5)
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--force", action="store_true", help="回填时重写已有分区")
    parser.add_argument("--sleep", type=float, default=0.2)
    args = parser.parse_args()

    if args.start:
        result = backfill(
            start=args.start,
            end=args.end,
            task=args.task,
            force=args.force,
            sleep=args.sleep,
        )
    elif args.task == "risk":
        result = task_options_risk(args.days)
    elif args.task == "stats":
        result = task_options_stats(args.days)
    else:
        result = {
            "risk": task_options_risk(args.days),
            "stats": task_options_stats(args.days),
        }
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    raise SystemExit(_cli())
