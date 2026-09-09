"""
申万行业映射加载。

数据源: QuantDB instrument_detail.parquet 的 rs_hyname 字段（申万行业名，128个）。
加载一次缓存到内存，供推理回测的行业信号计算使用。

行业映射代码格式统一为规范 suffix（600036.SH），与 engine_signal_scores.symbol
混合格式对齐需经 StockCodeUtil.to_suffix。
"""
from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

import pandas as pd

from backend.shared.quantdb_paths import resolve_quantdb_dir, resolve_quantdb_subdir
from backend.shared.stock_utils import StockCodeUtil

logger = logging.getLogger(__name__)


def _sector_dirs() -> list[Path]:
    """instrument_detail 的候选目录（由 QuantDB 数据目录解析派生，勿硬编码）。

    便携包（免 Docker）数据目录是 ``$STORAGE_ROOT/quantdb``，硬编码 ``/data/quantdb``
    会让申万行业映射整体失效、退到 stocks 表的证监会分类。
    """
    root = resolve_quantdb_dir()
    return [resolve_quantdb_subdir("2_base_sector", "instrument_detail"), root]

# 静态 fallback：若 parquet 缺失，用 stocks 表的行业字段兜底
_FALLBACK_DB_TABLE = "stocks"


@lru_cache(maxsize=1)
def load_shenwan_industry_map() -> dict[str, str]:
    """加载申万行业映射 {规范suffix股票代码: 申万行业名}。

    优先读 instrument_detail.parquet 的 rs_hyname（128个申万行业），
    parquet 缺失时回退 stocks 表 industry 字段（证监会分类）。
    返回空 dict 表示无数据。
    """
    df = _load_from_parquet()
    if df is not None and not df.empty:
        return df.set_index("symbol")["industry"].to_dict()

    fallback = _load_from_db_fallback()
    if fallback:
        return fallback

    logger.warning("申万行业映射不可用：parquet 与 DB fallback 均无数据")
    return {}


def _load_from_parquet() -> pd.DataFrame | None:
    path = _resolve_instrument_detail_path()
    if path is None:
        return None
    try:
        df = pd.read_parquet(path, columns=["Symbol", "rs_hyname"])
        sym_col = "Symbol" if "Symbol" in df.columns else "symbol"
        ind_col = "rs_hyname" if "rs_hyname" in df.columns else "industry"
        df = df.rename(columns={sym_col: "symbol", ind_col: "industry"})
        df["symbol"] = df["symbol"].astype(str).str.strip()
        df["symbol"] = df["symbol"].map(StockCodeUtil.to_suffix)
        df["industry"] = df["industry"].astype(str).str.strip()
        # 剔除空行业
        df = df[df["industry"].notna() & (df["industry"] != "") & (df["industry"] != "nan")]
        df = df.drop_duplicates(subset="symbol", keep="last")
        n = df["industry"].nunique() if not df.empty else 0
        logger.info("申万行业映射加载: %d 只股票, %d 个行业", len(df), n)
        return df
    except Exception as exc:
        logger.warning("读取 instrument_detail.parquet 失败: %s", exc)
        return None


def _resolve_instrument_detail_path() -> Path | None:
    for base in _sector_dirs():
        p = Path(base)
        for name in ("instrument_list.parquet", "instrument_detail.parquet"):
            if (p / name).exists():
                return p / name
        for name in ("instrument_list.parquet", "instrument_detail.parquet"):
            if (p / "instrument_detail" / name).exists():
                return p / "instrument_detail" / name
    return None


def _load_from_db_fallback() -> dict[str, str]:
    try:
        from backend.shared.database_manager_v2 import get_session
        from sqlalchemy import text

        rows: list[dict] = []

        async def _query() -> None:
            async with get_session(read_only=True) as session:
                result = await session.execute(
                    text("SELECT symbol, industry FROM stocks WHERE industry IS NOT NULL")
                )
                rows.extend(result.mappings().all())

        import asyncio

        asyncio.get_event_loop().run_until_complete(_query())
        mapping: dict[str, str] = {}
        for row in rows:
            sym = StockCodeUtil.to_suffix(str(row.get("symbol") or ""))
            ind = str(row.get("industry") or "").strip()
            if sym and ind and ind != "nan":
                mapping[sym] = ind
        logger.info("DB fallback 行业映射: %d 只股票, %d 个行业", len(mapping), len(set(mapping.values())))
        return mapping
    except Exception as exc:
        logger.warning("DB fallback 行业映射加载失败: %s", exc)
        return {}
