"""pre5 缓存：生产新闻决策用，按交易日缓存全市场行业调整 5 日收益。

纯计算 ``pre5_from_frame`` 可单测；``load_pre5_map`` 读 QuantDB 并落
``data/news_brief/pre5_cache.json``。读不到一律返回空 dict，决策层按 INFO 降级。
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / "data" / "news_brief" / "pre5_cache.json"
MAX_AGE_S = 18 * 3600


def _data_dir() -> Path:
    env = str(os.getenv("QM_QUANTDB_DATA_DIR") or "").strip()
    if env:
        return Path(env)
    for candidate in (Path("/data/quantdb"), ROOT / "data" / "quantdb"):
        if candidate.is_dir():
            return candidate
    return ROOT / "data" / "quantdb"


def pre5_from_frame(
    px: pd.DataFrame,
    sectors: pd.DataFrame | None = None,
) -> dict[str, float]:
    """px: symbol, dt, close；sectors: symbol, sector_code（可选）。

    返回每个 symbol 最后一个交易日的 pre5（行业调整；无行业则用原始收益）。
    """
    if px.empty:
        return {}
    df = px[["symbol", "dt", "close"]].copy()
    df["symbol"] = df["symbol"].astype(str)
    df["dt"] = df["dt"].astype(int)
    df = df.sort_values(["symbol", "dt"]).reset_index(drop=True)
    df["r"] = df.groupby("symbol", sort=False)["close"].transform(
        lambda s: s / s.shift(1) - 1
    )
    if sectors is not None and not sectors.empty:
        sec = sectors[["symbol", "sector_code"]].drop_duplicates("symbol")
        df = df.merge(sec, on="symbol", how="left")
        sec_mean = (
            df.dropna(subset=["sector_code"])
            .groupby(["sector_code", "dt"], as_index=False)["r"]
            .mean()
            .rename(columns={"r": "r_sec"})
        )
        df = df.merge(sec_mean, on=["sector_code", "dt"], how="left")
        df["ar"] = df["r"] - df["r_sec"]
        df["ar"] = df["ar"].fillna(df["r"])
    else:
        df["ar"] = df["r"]
    df["pre5"] = df.groupby("symbol", sort=False)["ar"].transform(
        lambda s: s.shift(1).rolling(5).sum()
    )
    last = df.groupby("symbol", as_index=False)["pre5"].last()
    return {
        str(sym): float(val)
        for sym, val in zip(last.symbol, last.pre5)
        if pd.notna(val)
    }


def pre5_lookup(code: str, values: dict[str, float]) -> float | None:
    """代码兼容 suffix/prefix/裸 6 位。"""
    if not values:
        return None
    raw = str(code or "").upper().strip()
    if raw in values:
        return values[raw]
    compact = "".join(ch for ch in raw if ch.isalnum())
    for suffix in (".SH", ".SZ", ".BJ"):
        if compact.endswith(suffix[1:]):
            return values.get(compact[:6] + suffix)
    if compact.startswith(("SH", "SZ", "BJ")) and len(compact) >= 8:
        return values.get(compact[2:8] + "." + compact[:2])
    if compact.isdigit() and len(compact) == 6:
        for suffix in (".SH", ".SZ", ".BJ"):
            if compact + suffix in values:
                return values[compact + suffix]
    return None


def _load_cache(day: str) -> dict[str, float] | None:
    try:
        if time.time() - CACHE.stat().st_mtime > MAX_AGE_S:
            return None
        doc = json.loads(CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if str(doc.get("date") or "") != day:
        return None
    values = doc.get("values")
    if not isinstance(values, dict):
        return None
    return {str(k): float(v) for k, v in values.items() if isinstance(v, (int, float))}


def _save_cache(day: str, values: dict[str, float]) -> None:
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE.with_name(CACHE.name + ".tmp")
        tmp.write_text(
            json.dumps({"date": day, "ts": datetime.now().isoformat(timespec="seconds"),
                        "values": values}, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(CACHE)
    except OSError:
        logger.warning("[news_pre5] cache write failed", exc_info=True)


def load_pre5_map(
    as_of: str | int | None = None,
    *,
    cache_path: Path | None = None,
    data_dir: Path | None = None,
) -> dict[str, float]:
    """按日缓存；失败返回空 dict（调用方按 INFO 降级）。"""
    global CACHE
    if cache_path is not None:
        CACHE = cache_path
    day = str(as_of or datetime.now().strftime("%Y%m%d")).replace("-", "")[:8]
    cached = _load_cache(day)
    if cached is not None:
        return cached

    base = data_dir or _data_dir()
    daily = base / "1_kline_data" / "daily_unadjusted"
    members = base / "2_base_sector" / "sector_concept" / "sector_members.parquet"
    try:
        import duckdb

        con = duckdb.connect()
        try:
            px = con.execute(
                f"""
                SELECT symbol, dt, close
                FROM read_parquet('{daily}/dt=*/data.parquet')
                WHERE dt BETWEEN {int(day) - 60} AND {int(day)}
                """
            ).fetchdf()
            sec = pd.DataFrame()
            if members.is_file():
                sec = con.execute(
                    f"""
                    SELECT Symbol AS symbol, SectorCode AS sector_code
                    FROM read_parquet('{members}')
                    WHERE SectorType='行业板块(一级)'
                    """
                ).fetchdf()
        finally:
            con.close()
    except Exception:  # noqa: BLE001 - 数据不可达不阻塞新闻层
        logger.warning("[news_pre5] QuantDB load failed", exc_info=True)
        return {}

    values = pre5_from_frame(px, sec if not sec.empty else None)
    if values:
        _save_cache(day, values)
    return values
