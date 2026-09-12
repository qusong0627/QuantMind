"""美股市场分析 —— 数据层基座（唯一与 parquet 目录耦合的地方）。

所有域模块（indices/breadth/sectors/earnings/analysts/holdings/valuation）
共用本文件的读取、缓存、交易日与名称/行业映射工具。换数据源只需改这里。

数据口径（实测，2026-09-12 复核）：
- `daily_forward` 是 **yfinance auto_adjust=False 的原始价（未复权）**，
  `volume` 为股数，**`amount` 为美元原始成交额**（无换算系数，与 A 股「股/万元」不同）
- 文件内部自带 `dt`(BIGINT) 列，会**遮蔽** hive 分区列 `dt`，因此按日期取数
  一律走显式分区文件列表（`_read_kline`），禁止对 `data/**/*.parquet` 全 glob
  （实测 250 日窗口：全 glob 1.73s vs 显式列表 0.05s，且全 glob 随历史增长退化）
- 标的池 517 只（标普500 + 纳指补充），非全市场；行业分类 516 只
"""

from __future__ import annotations

import logging
from typing import Any
from functools import lru_cache
from collections.abc import Iterable, Sequence

import duckdb
import pandas as pd

from backend.services.api.market_analysis_shared.caching import cached, clear_cache
from backend.services.api.market_analysis_shared.market_days import (
    list_partition_dates,
    partition_dates_to_sql,
    to_iso,
    trading_days_until,
)
from backend.services.api.market_analysis_shared.names import build_name_map
from backend.services.engine.data_platform.quantus_hub import _resolve_quantus_data_dir

logger = logging.getLogger(__name__)

# ---- 数据目录与分区相对路径 ----

DATA_DIR = _resolve_quantus_data_dir()

KLINE_REL = "1_kline_data/daily_forward"  # 全市场日线（原始价、美元成交额）
INDEX_REL = "1_kline_data/index_daily"  # 指数日线
VALUATION_REL = "5_technical_derived/valuation"  # 估值快照（日分区）
L1_REL = "6_ml_datasets/l1_factors"  # L1 量价因子（日分区）

SECTOR_GLOB = "2_base_sector/sector/*.parquet"  # GICS 行业（每股一文件，快照）
F10_GLOB = "2_base_sector/f10/*.parquet"  # 基本面快照（市值/PE/PB/股息/52周高低）
SECURITY_MASTER_REL = "2_base_sector/security_master/data.parquet"  # 标的池 + 中文名

ANALYST_REL = "4_analyst"  # 分析师 12 张子表（每股一文件）
DIVIDEND_GLOB = "3_financial_data/dividend/*.parquet"  # 分红事件
SPLITS_GLOB = "3_financial_data/splits/*.parquet"  # 拆股事件

# ---- 口径常量 ----

# 美股无涨跌停（有 LULD 熔断）：±5% 只作「异动」统计口径，含义与 A 股涨停不同
BIG_MOVE_THRESHOLD = 5.0

# 单日涨跌幅裁剪上限：拆股/大额分红造成纯价格跳变，裁剪防其污染行业均值与龙头榜
PCT_CLIP = 100.0

# 52 周 / 均线窗口（交易日数）
WINDOW_52W = 250
WINDOW_MA50 = 50
WINDOW_MA200 = 200

# 核心指数（index_daily 实际存在的 5 个；**无 VIX、无 ETF**）
INDEX_OVERVIEW: list[dict[str, str]] = [
    {"symbol": "SPX.US", "name": "标普500"},
    {"symbol": "NDX.US", "name": "纳斯达克100"},
    {"symbol": "IXIC.US", "name": "纳斯达克"},
    {"symbol": "DJI.US", "name": "道琼斯"},
    {"symbol": "SOX.US", "name": "费城半导体"},
]

# GICS 11 大类中文名（sector 列的实际取值来自 yahoo，含空值）
SECTOR_CN: dict[str, str] = {
    "Technology": "信息技术",
    "Industrials": "工业",
    "Consumer Cyclical": "可选消费",
    "Financial Services": "金融",
    "Healthcare": "医疗保健",
    "Consumer Defensive": "日常消费",
    "Utilities": "公用事业",
    "Real Estate": "房地产",
    "Communication Services": "通信服务",
    "Basic Materials": "基础材料",
    "Energy": "能源",
    "Unknown": "未分类",
}


# ---- 基础设施 ----


def clear_cache_us() -> None:
    """清空美股市场分析缓存（外部刷新入口）。"""
    clear_cache()
    _name_map.cache_clear()


def _q(sql: str) -> pd.DataFrame:
    """执行 DuckDB 只读查询（短连接，线程安全）。"""
    con = duckdb.connect()
    try:
        return con.execute(sql).fetchdf()
    finally:
        con.close()


def _avail() -> bool:
    """数据目录与关键分区可用性。"""
    return (DATA_DIR / "1_kline_data").is_dir()


def _glob_sql(rel_glob: str) -> str:
    return f"read_parquet('{DATA_DIR / rel_glob}', hive_partitioning=1)"


def _partition_file(rel_dir: str, ymd: str) -> str | None:
    """分区文件的绝对路径（不存在返回 None）。"""
    path = DATA_DIR / rel_dir / f"dt={ymd}" / "data.parquet"
    return str(path) if path.exists() else None


def _read_partitioned(
    rel_dir: str, ymd_days: Sequence[str], columns: str = "*"
) -> pd.DataFrame:
    """按显式分区文件列表读取（**唯一正确的按日期取数方式**）。

    走文件列表而不是 `**/*.parquet` + WHERE：文件内 dt 列会遮蔽 hive 分区列，
    DuckDB 无法裁剪，且耗时会随库内总分区数增长。

    `columns` 必须按需裁剪：日线文件有 11 列，`SELECT *` 读 310 个分区耗时
    1.62s，只取 symbol/dt/close 三列只要 0.05s（32 倍差距）。
    """
    files = [f for f in (_partition_file(rel_dir, d) for d in ymd_days) if f]
    if not files:
        return pd.DataFrame()
    literal = ",".join(repr(f) for f in files)
    return _q(f"SELECT {columns} FROM read_parquet([{literal}])")


def _trading_days(end: str | None, n: int) -> list[str]:
    """截至 end（YYYYMMDD 或 None=最新）的最近 n 个交易日，降序，[0]=最新。"""
    dates = list_partition_dates(KLINE_REL, DATA_DIR)
    if end:
        dates = [d for d in dates if d <= end]
    return dates[-n:][::-1]


def _latest_trade_date() -> str | None:
    d = _trading_days(None, 1)
    return d[0] if d else None


def _latest_index_date() -> str | None:
    """指数分区最新日期（**通常滞后个股若干天**，页面需分别标注）。"""
    dates = list_partition_dates(INDEX_REL, DATA_DIR)
    return dates[-1] if dates else None


def _index_trading_days(end: str | None, n: int) -> list[str]:
    """**指数分区自己的**交易日序列（降序）。

    不可用个股日历替代：index_daily 与 daily_forward 的分区集合不同，
    混用会取到不存在的分区、静默返回空。
    """
    dates = list_partition_dates(INDEX_REL, DATA_DIR)
    if end:
        dates = [d for d in dates if d <= end]
    return dates[-n:][::-1]


def _dedupe_bars(df: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    """同 (symbol, dt) 可能有多条 release（同步去重抓取），保留最新发布行。"""
    if df.empty:
        return df
    if "published_at" in df.columns:
        return df.sort_values("published_at", ascending=False).drop_duplicates(
            keys, keep="first"
        )
    return df.drop_duplicates(keys, keep="first")


# ---- 全市场截面快照 ----

_MARKET_SNAP_COLS = "symbol, dt, close, amount, volume"


def _market_pct_snapshot() -> tuple[str | None, pd.DataFrame]:
    """最新交易日的全市场涨跌幅快照（**跨域唯一实现**）。

    返回 (trade_date, DataFrame[symbol, close, amount, volume, pct_change])。
    口径：close_t / close_{t-1} - 1（**原始未复权价**）。剔除停牌（close<=0/NaN）
    与当日新上市（无前收）的个股；±100% 之外视为拆股/公司行动造成的价格跳变，
    裁剪以免污染行业均值与龙头榜。
    """
    days = _trading_days(None, 2)
    if not days:
        return None, pd.DataFrame()
    k = _read_partitioned(KLINE_REL, days, columns=_MARKET_SNAP_COLS)
    if k.empty:
        return days[0], pd.DataFrame()
    k = _dedupe_bars(k, ["symbol", "dt"])
    k["dt"] = k["dt"].astype(str)
    cur = days[0]
    cols = [c for c in ("symbol", "close", "amount", "volume") if c in k.columns]
    if len(days) == 1:
        snap = k[k["dt"] == cur][cols].copy()
        snap["pct_change"] = 0.0
        return cur, snap[snap["close"].fillna(0) > 0]

    prev = days[1]
    p = k.pivot_table(index="symbol", columns="dt", values="close")
    if cur not in p.columns or prev not in p.columns:
        snap = k[k["dt"] == cur][cols].copy()
        snap["pct_change"] = 0.0
        return cur, snap[snap["close"].fillna(0) > 0]
    prev_close = p[prev].reindex(p.index)
    valid = prev_close.notna() & (prev_close > 0)
    calc = ((p[cur] / prev_close - 1) * 100).where(valid)
    snap = k[k["dt"] == cur][cols].copy()
    snap = snap.merge(calc.rename("pct_change").reset_index(), on="symbol", how="left")
    snap = snap[snap["close"].fillna(0) > 0]
    snap["pct_change"] = snap["pct_change"].fillna(0.0).clip(-PCT_CLIP, PCT_CLIP)
    return cur, snap


# ---- 量能基准（量比计算的底座） ----

_VOLUME_BASELINE_WINDOW = 20


def _volume_baseline(window: int = _VOLUME_BASELINE_WINDOW) -> pd.DataFrame:
    """各标的在**前 window 个交易日**的平均成交量/成交额。

    返回 DataFrame[symbol, avg_volume, avg_amount, base_days]。

    **基准窗口不含最新交易日** —— 量比 = 当日量 / 前 20 日均量，若把当日
    算进基准，巨量当日会抬高分母把自己稀释掉（专业口径都是「前 N 日」）。
    """
    days = _trading_days(None, window + 1)
    if len(days) < 2:
        return pd.DataFrame(columns=["symbol", "avg_volume", "avg_amount", "base_days"])
    base_days = days[1 : window + 1]  # days[0] 是最新交易日，排除
    k = _read_partitioned(KLINE_REL, base_days, columns="symbol, volume, amount")
    if k.empty:
        return pd.DataFrame(columns=["symbol", "avg_volume", "avg_amount", "base_days"])
    agg = k.groupby("symbol").agg(
        avg_volume=("volume", "mean"),
        avg_amount=("amount", "mean"),
        base_days=("volume", "size"),
    )
    return agg.reset_index()


def _hot_snapshot() -> tuple[str | None, pd.DataFrame]:
    """最新交易日截面 + 量比/成交额（热门榜的公共底座）。

    返回 DataFrame[symbol, close, amount, volume, pct_change, avg_volume,
    avg_amount, rvol]。`rvol`（量比）为当日量 / 前 20 日均量，基准不足
    20 天的标的 rvol 置空而不是用短窗口凑数（新股量比会严重失真）。
    """
    latest, snap = _market_pct_snapshot()
    if not latest or snap.empty:
        return latest, pd.DataFrame()
    base = _volume_baseline()
    df = snap.merge(base, on="symbol", how="left")
    enough = df["base_days"].fillna(0) >= _VOLUME_BASELINE_WINDOW
    avg_vol = df["avg_volume"].where(df["avg_volume"].fillna(0) > 0)
    df["rvol"] = (df["volume"] / avg_vol).where(enough)
    return latest, df


# ---- 名称 / 行业 / 基本面快照 ----


@lru_cache(maxsize=1)
def _name_map() -> dict[str, str]:
    """symbol(AAPL) -> 中文名（security_master 全市场主表）。"""
    return build_name_map(str(DATA_DIR / SECURITY_MASTER_REL))


def _names(symbols: Iterable[str]) -> dict[str, str]:
    return {s: _name_map().get(s, s) for s in symbols}


def _universes() -> pd.DataFrame:
    """标的池主表（symbol / cn_name / en_name）。"""
    path = DATA_DIR / SECURITY_MASTER_REL
    if not path.exists():
        return pd.DataFrame(columns=["symbol", "cn_name", "en_name"])

    def _load() -> pd.DataFrame:
        df = pd.read_parquet(path)
        return df[["symbol", "cn_name", "en_name"]].drop_duplicates("symbol")

    return cached("us:universe", _load, ttl=1800.0)


def _sector_map() -> pd.DataFrame:
    """GICS 行业映射：symbol -> sector(11 大类) / industry(细分)。

    来源 `2_base_sector/sector/{SYMBOL}.parquet`（每股一文件，快照）。
    security_master 里**没有**行业字段，行业一律走这张表。
    """

    def _load() -> pd.DataFrame:
        df = _q(
            f"SELECT symbol, sector, industry FROM {_glob_sql(SECTOR_GLOB)}"
        )
        if df.empty:
            return pd.DataFrame(columns=["symbol", "sector", "industry"])
        df = df.drop_duplicates("symbol")
        # 29 只 sector 为空 → 归为「未分类」，避免热力图/轮动出现 NaN 分组
        df["sector"] = df["sector"].fillna("").replace("", "Unknown")
        df["industry"] = df["industry"].fillna("")
        return df

    return cached("us:sector_map", _load, ttl=1800.0)


def _f10_snapshot() -> pd.DataFrame:
    """基本面快照：市值 / PE / PB / 股息率 / 52 周高低 / 中文名。

    这是**估值类分析的主数据源**（valuation 分区曾长期静默写空，
    见 docs/美股市场分析模块_设计方案.md §四）。
    """

    def _load() -> pd.DataFrame:
        df = _q(
            "SELECT symbol, name, market_cap, pe_ratio, pb_ratio, dividend_yield, "
            f'"52w_high", "52w_low", avg_volume FROM {_glob_sql(F10_GLOB)}'
        )
        if df.empty:
            return pd.DataFrame(
                columns=[
                    "symbol", "name", "market_cap", "pe_ratio", "pb_ratio",
                    "dividend_yield", "52w_high", "52w_low", "avg_volume",
                ]
            )
        return df.drop_duplicates("symbol")

    return cached("us:f10", _load, ttl=1800.0)


def _load_analyst_table(name: str) -> pd.DataFrame:
    """读取 `4_analyst/{name}/` 下全部标的文件（约 480-500 个小文件）。

    结果带长 TTL 缓存：这些是快照/事件式小表，全市场合计行数不大
    （最大的 upgrades_downgrades 约 18 万行），但按请求重读文件会很浪费。
    """

    def _load() -> pd.DataFrame:
        path = DATA_DIR / ANALYST_REL / name
        if not path.is_dir():
            return pd.DataFrame()
        files = sorted(path.glob("*.parquet"))
        if not files:
            return pd.DataFrame()
        literal = ",".join(repr(str(f)) for f in files)
        return _q(f"SELECT * FROM read_parquet([{literal}], union_by_name=true)")

    return cached(f"us:analyst:{name}", _load, ttl=1800.0)


def _load_events(rel_glob: str) -> pd.DataFrame:
    """读取事件式数据集（分红 / 拆股）。"""

    def _load() -> pd.DataFrame:
        if not (DATA_DIR / rel_glob).parent.is_dir():
            return pd.DataFrame()
        return _q(f"SELECT * FROM {_glob_sql(rel_glob)}")

    return cached(f"us:events:{rel_glob}", _load, ttl=1800.0)


def _sector_cn(sector: str | None) -> str:
    """GICS 英文类名 -> 中文展示名。"""
    if not sector:
        return SECTOR_CN["Unknown"]
    return SECTOR_CN.get(sector, sector)


# ---- 诊断 ----


def feed_status() -> dict[str, Any]:
    """数据可用性与各数据集最新日期（前端诊断 & 面板日期标注）。"""
    avail = _avail()
    return {
        "available": avail,
        "data_dir": str(DATA_DIR),
        "kline_latest": _latest_trade_date() if avail else None,
        "index_latest": _latest_index_date() if avail else None,
        "valuation_latest": (
            list_partition_dates(VALUATION_REL, DATA_DIR)[-1:] or [None]
        )[0]
        if avail
        else None,
        "universe_size": int(len(_universes())) if avail else 0,
        "sector_covered": int(len(_sector_map())) if avail else 0,
        "indices": INDEX_OVERVIEW,
        # 页面需向用户明示的口径与覆盖限制
        "notes": {
            "universe": "标的池：标普500 + 纳指补充，非全市场",
            "price_adjust": "日线为原始价（未复权），成交额为美元原始值",
            "no_vix": "本地指数数据无 VIX，也无任何 ETF",
            "index_lag": "指数分区通常滞后个股若干交易日，面板分别标注日期",
        },
    }


# 供 router 的 SSE 刷新复用
__all__ = [
    "DATA_DIR",
    "KLINE_REL",
    "INDEX_REL",
    "BIG_MOVE_THRESHOLD",
    "INDEX_OVERVIEW",
    "clear_cache_us",
    "feed_status",
    "_q",
    "_avail",
    "_trading_days",
    "_latest_trade_date",
    "_latest_index_date",
    "_index_trading_days",
    "_market_pct_snapshot",
    "_read_partitioned",
    "_partition_file",
    "_glob_sql",
    "_dedupe_bars",
    "_names",
    "_universes",
    "_sector_map",
    "_f10_snapshot",
    "_load_analyst_table",
    "_load_events",
    "_sector_cn",
    "to_iso",
    "trading_days_until",
    "partition_dates_to_sql",
]
