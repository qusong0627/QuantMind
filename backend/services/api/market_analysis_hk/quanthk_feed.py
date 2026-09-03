"""QuantHK 市场分析数据层 — 港股本地 parquet 实时聚合（DuckDB 直读）。

设计要点（与 A 股 market_analysis/quantdb_feed.py 对齐，但按港股特色调整）：
- 数据源：QM_QUANTHK_DATA_DIR（默认 data/quanthk/），复用 QuantHKDataHub 的目录解析
- K线口径：daily_forward 为 akshare stock_hk_daily 原始价（不复权），涨跌幅 = close_t/close_{t-1} - 1；
  港股无涨跌停，快涨快跌阈值取 ±5%
- 板块体系：akshare_profile「所属行业」（恒生行业分类，全市场覆盖），而非 sector/141 只子集
- 港股特色：南向资金（hsgt_south 日频持股，dt= 分区式）、CCASS 席位穿透
  （ccass_top50 + ccass_factors）、AH 对应股（ah_membership）、
  高股息/低估值（akshare_valuation + akshare_financial 快照）
- 容错：旧「每股一文件」南向布局个别 parquet 损坏时跳过并告警，不影响其余功能
"""

from __future__ import annotations

import logging
import os
import re
import threading
from functools import lru_cache
from typing import Any
from collections.abc import Iterable

import duckdb
import pandas as pd

from backend.services.api.market_analysis_hk.institutional_classifier import (
    CATEGORY_HKSCC,
    CATEGORY_ORDER,
    CATEGORY_SOUTHBOUND,
    category_label,
    classify,
    load_overrides,
)
from backend.services.api.market_analysis_shared.caching import cached, clear_cache
from backend.services.api.market_analysis_shared.display import fmt_yi, pct, safe_float
from backend.services.api.market_analysis_shared.market_days import (
    list_partition_dates,
    partition_dates_to_sql,
    to_iso,
)
from backend.services.api.market_analysis_shared.names import build_name_map
from backend.services.engine.data_platform.quanthk_hub import _resolve_quanthk_data_dir

logger = logging.getLogger(__name__)

# 数据目录与分区相对路径
DATA_DIR = _resolve_quanthk_data_dir()
KLINE_REL = "1_kline_data/daily_forward"  # 全市场日线（原始价）
INDEX_REL = "1_kline_data/index_daily"  # 指数日线
CCASS_TOP50_REL = "2_base_sector/ccass_top50"  # 中央结算系统 Top50 席位快照（日分区）
CCASS_FACTORS_REL = "6_ml_datasets/ccass_factors"  # CCASS 衍生因子（日分区）
SOUTH_REL = "2_base_sector/hsgt_south"  # 港股通南向持股（dt= 分区式，同步链路主布局）
SOUTH_LEGACY_GLOB = (
    "2_base_sector/hsgt_south/*.HK.parquet"  # 旧每股一文件布局（停更遗留，兜底）
)
PROFILE_GLOB = "2_base_sector/akshare_profile/*.parquet"  # 公司资料（含所属行业）
VALUATION_GLOB = "2_base_sector/akshare_valuation/*.parquet"  # 估值快照
FINANCIAL_GLOB = "2_base_sector/akshare_financial/*.parquet"  # 基本面快照
RM_GLOB = "2_base_sector/security_master/data.parquet"  # 证券主表（中文名）

# 核心指数
INDEX_OVERVIEW: list[dict[str, str]] = [
    {"symbol": "HSI.HK", "name": "恒生指数"},
    {"symbol": "HSCEI.HK", "name": "恒生国企"},
    {"symbol": "HSTECH.HK", "name": "恒生科技"},
    {"symbol": "HSCCI.HK", "name": "恒生红筹"},
]

# 港股无涨跌停：快涨/快跌阈值
BIG_MOVE_THRESHOLD = 5.0

_QUERY_TTL: float = 300.0  # 5 分钟
_LOCK = threading.Lock()


# ---- 基础设施 ----


def clear_cache_hk() -> None:
    """清空港股市场分析缓存（外部刷新入口）。"""
    clear_cache()


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


def _trading_days(end: str | None, n: int) -> list[str]:
    """截至 end（YYYYMMDD 或 None）的最近 n 个交易日，降序，[0]=最新。"""
    dates = list_partition_dates(KLINE_REL, DATA_DIR)
    if end:
        dates = [d for d in dates if d <= end]
    return dates[-n:][::-1]


def _latest_trade_date() -> str | None:
    d = _trading_days(None, 1)
    return d[0] if d else None


@lru_cache(maxsize=1)
def _name_map() -> dict[str, str]:
    """symbol(0700.HK) -> 中文名（security_master 全市场主表）。"""
    return build_name_map(str(DATA_DIR / RM_GLOB))


def _names(symbols: Iterable[str]) -> dict[str, str]:
    return {s: _name_map().get(s, s) for s in symbols}


def _latest_ccass_date() -> str | None:
    d = list_partition_dates(CCASS_FACTORS_REL, DATA_DIR)
    return d[-1] if d else None


# ---- 损耗/涨跌幅快照（全市场） ----


def _market_pct_snapshot() -> tuple[str | None, pd.DataFrame]:
    """最新交易日的全市场涨跌幅快照。

    返回 (trade_date, DataFrame[symbol, close, amount, pct_change])。
    口径：close_t/close_{t-1} - 1（不复权原始价；除净日存在轻微偏差，
    港股无官方涨跌幅表，此口径与平台 K 线一致）。剔除停牌（close<=0 / NaN）
    与当日新上市（无前收）的个股。
    """
    days = _trading_days(None, 2)
    if len(days) < 1:
        return None, pd.DataFrame()
    dt_in = partition_dates_to_sql(days)
    k = _q(
        "SELECT symbol, dt, close, amount, published_at FROM read_parquet("
        f"'{DATA_DIR / KLINE_REL}/**/*.parquet', hive_partitioning=1) "
        f"WHERE dt IN ({dt_in})"
    )
    if k.empty:
        return days[0], pd.DataFrame()
    # hive_partitioning 的 dt 分区列是 int64，统一转字符串后比较（与交易日历 str 对齐）
    k["dt"] = k["dt"].astype(str)
    # 分区内同一 symbol 可能有多条 release（同步去重抓取），保留最新发布行
    if "published_at" in k.columns:
        k = k.sort_values("published_at", ascending=False).drop_duplicates(
            ["symbol", "dt"], keep="first"
        )
    else:
        k = k.drop_duplicates(["symbol", "dt"], keep="first")
    if len(days) == 1:
        snap = k[k["dt"] == days[0]][["symbol", "close", "amount"]].copy()
        snap["pct_change"] = 0.0
        return days[0], snap[snap["close"].fillna(0) > 0]

    cur, prev = days[0], days[1]
    p = k.pivot_table(index="symbol", columns="dt", values="close")
    if cur not in p.columns or prev not in p.columns:
        snap = k[k["dt"] == cur][["symbol", "close", "amount"]].copy()
        snap["pct_change"] = 0.0
        return cur, snap
    prev_close = p[prev].reindex(p.index)
    valid = prev_close.notna() & (prev_close > 0)
    calc = (p[cur] / prev_close - 1) * 100
    calc = calc.where(valid)
    snap = k[k["dt"] == cur][["symbol", "close", "amount"]].copy()
    snap = snap.merge(calc.rename("pct_change").reset_index(), on="symbol", how="left")
    snap = snap[snap["close"].fillna(0) > 0]
    # 特殊公司行动（长停牌复牌/合股拆股）会造成单日 >100% 的纯价格跳变，
    # 港股无官方涨跌幅表兜底，裁剪到 ±100% 防其对行业均值/领涨龙头失真
    snap["pct_change"] = snap["pct_change"].fillna(0.0).clip(-100.0, 100.0)
    return cur, snap


# ---- 1. 大盘核心指数 ----


def get_indices_overview() -> list[dict[str, Any]]:
    """四大港股核心指数快照（价格/涨跌/成交额/5 日趋势）。"""

    def _load():
        if not _avail():
            return []
        latest = _latest_trade_date()
        days = _trading_days(latest, 30)
        if not days:
            return []
        dt_in = partition_dates_to_sql(days)
        sym_in = ",".join(f"'{i['symbol']}'" for i in INDEX_OVERVIEW)
        df = _q(
            "SELECT symbol, dt, close, amount FROM read_parquet("
            f"'{DATA_DIR / INDEX_REL}/**/*.parquet', hive_partitioning=1) "
            f"WHERE dt IN ({dt_in}) AND symbol IN ({sym_in})"
        )
        if df.empty:
            return []
        result = []
        for item in INDEX_OVERVIEW:
            sub = df[df["symbol"] == item["symbol"]].sort_values("dt")
            if sub.empty:
                continue
            closes = sub["close"].tolist()
            ref = str(sub["dt"].iloc[-1])
            last = safe_float(closes[-1])
            prev = safe_float(closes[-2]) if len(closes) > 1 else last
            change = last - prev
            result.append(
                {
                    "symbol": item["symbol"],
                    "name": item["name"],
                    "price": round(last, 2),
                    "change": round(change, 2),
                    "pct_change": pct(change / prev * 100 if prev else 0.0),
                    "turnover_yi": fmt_yi(safe_float(sub["amount"].iloc[-1])),
                    "trend": [round(c, 2) for c in closes[-5:]],
                    "trade_date": to_iso(ref if len(ref) == 8 else f"{ref:08d}"),
                }
            )
        return result

    return cached("hk_indices_overview", _load)


# ---- 2. 市场温度计（港股无涨跌停，快涨快跌口径） ----


def get_market_breadth() -> dict[str, Any]:
    """全市场情绪温度计：涨跌家数/成交额/上涨占比/大涨大跌分布。

    港股特色：无涨跌停限制，用 ±5% 作为「快涨/快跌」标识，替代 A 股的涨跌停统计。
    """

    def _empty(trade_date: str = ""):
        return {
            "trade_date": trade_date,
            "total_stocks": 0,
            "advance_count": 0,
            "decline_count": 0,
            "flat_count": 0,
            "big_up_count": 0,
            "big_down_count": 0,
            "total_turnover_yi": 0.0,
            "profit_effect": 50.0,
            "sentiment_score": 50,
        }

    def _load():
        if not _avail():
            return _empty()
        latest, snap = _market_pct_snapshot()
        if not latest or snap.empty:
            return _empty(to_iso(latest) if latest else "")
        pct_s = snap["pct_change"].fillna(0.0)
        adv = int((pct_s > 0).sum())
        dec = int((pct_s < 0).sum())
        flat = int((pct_s == 0).sum())
        big_up = int((pct_s >= BIG_MOVE_THRESHOLD).sum())
        big_down = int((pct_s <= -BIG_MOVE_THRESHOLD).sum())
        total = int(snap["amount"].fillna(0).sum() or 0)
        total_stocks = adv + dec + flat
        profit = round(adv / total_stocks * 100, 1) if total_stocks else 50.0
        return {
            "trade_date": to_iso(latest),
            "total_stocks": total_stocks,
            "advance_count": adv,
            "decline_count": dec,
            "flat_count": flat,
            "big_up_count": big_up,
            "big_down_count": big_down,
            "total_turnover_yi": fmt_yi(total),
            "profit_effect": profit,
            # 50 = 中性；上涨占比 100 => 100，0 => 0
            "sentiment_score": round(50 + (profit - 50) * 2.0, 1),
        }

    return cached("hk_market_breadth", _load)


# ---- 3. 恒生行业热力图 ----


@lru_cache(maxsize=1)
def _industry_map() -> dict[str, str]:
    """symbol(0700.HK) -> 所属行业（akshare_profile 全市场恒生行业分类）。"""
    try:
        df = _q(
            "SELECT DISTINCT symbol, 所属行业 FROM read_parquet("
            f"'{DATA_DIR}/2_base_sector/akshare_profile/*.parquet', union_by_name=true) "
        )
        df = df[df["所属行业"].notna() & (df["所属行业"] != "")]
        return dict(zip(df["symbol"].astype(str), df["所属行业"].astype(str), strict=True))
    except Exception as exc:  # 行业表缺失不该让热力图整个挂掉
        logger.warning("[quanthk_feed] 行业表读取失败: %s", exc)
        return {}


def _industry_groups(min_stocks: int = 3) -> dict[str, list[str]]:
    """行业 -> 成分股列表（过滤成分过少的行业，避免碎块）。"""
    groups: dict[str, list[str]] = {}
    for sym, ind in _industry_map().items():
        groups.setdefault(ind, []).append(sym)
    return {k: v for k, v in groups.items() if len(v) >= min_stocks}


def get_sector_heatmap(limit: int = 40) -> list[dict[str, Any]]:
    """恒生行业热力图：行业平均涨幅/成交额/领涨龙头（全市场口径）。"""

    def _load():
        if not _avail():
            return []
        _, prices = _market_pct_snapshot()
        if prices.empty:
            return []
        groups = _industry_groups()
        if not groups:
            return []
        names = _name_map()
        prices["name"] = prices["symbol"].map(lambda s: names.get(s, s))
        items: list[dict[str, Any]] = []
        for ind, syms in groups.items():
            sub = prices[prices["symbol"].isin(syms)]
            if sub.empty:
                continue
            avg = round(float(sub["pct_change"].mean()), 2)
            tot = float(sub["amount"].sum() or 0)
            leader = sub.sort_values("pct_change", ascending=False).iloc[0]
            items.append(
                {
                    "name": ind,
                    "value": max(fmt_yi(tot), 0.5),
                    "pct_change": avg,
                    "leader": str(leader["name"]),
                    "leader_pct": round(float(leader["pct_change"] or 0.0), 2),
                    "stock_count": int(len(sub)),
                }
            )
        items.sort(key=lambda x: x["value"], reverse=True)
        return items[:limit]

    return cached("hk_sector_heatmap", _load)


# ---- 4. 南向资金（港股通持股） ----


def _south_file_list() -> list[str]:
    """（仅旧布局兜底用）探测可读「每股一文件」parquet，跳过损坏文件。"""

    def _scan():
        import glob

        files = sorted(glob.glob(str(DATA_DIR / SOUTH_LEGACY_GLOB)))
        good: list[str] = []
        con = duckdb.connect()
        try:
            for f in files:
                try:
                    con.execute(f"SELECT * FROM read_parquet('{f}') LIMIT 0")
                    good.append(f)
                except Exception:
                    logger.warning(
                        "[quanthk_feed] 跳过损坏南向文件 %s", os.path.basename(f)
                    )
        finally:
            con.close()
        return good

    return cached("hk_south_files", _scan, ttl=6 * 3600)


def _read_south_safe() -> pd.DataFrame:
    """读取南向持股全库。

    主布局为 dt= 分区式（同步链路 quanthk_south_sync.py 产物，日频更新）；
    旧「每股一文件」布局（外部 data-hs 导入，2025-12 停更）作为兜底，
    并跳过其中个别写入中断的损坏 parquet。
    """
    if list_partition_dates(SOUTH_REL, DATA_DIR):
        # 只读 dt= 分区布局；** 会连带旧每股文件布局（含损坏 parquet）
        return _q(
            "SELECT symbol, query_date, holding_quantity, holding_percentage FROM "
            "read_parquet("
            f"'{DATA_DIR / SOUTH_REL}/dt=*/data.parquet', hive_partitioning=1, union_by_name=true)"
        )
    good = _south_file_list()
    if not good:
        return pd.DataFrame()
    try:
        # 900+ 文件必须用数组字面量，read_parquet 只接受 VARCHAR[] 或单路径
        files_literal = "[" + ",".join(f"'{f}'" for f in good) + "]"
        return _q(
            "SELECT symbol, query_date, holding_quantity, holding_percentage FROM "
            f"read_parquet({files_literal}, union_by_name=true)"
        )
    except Exception as exc:
        logger.warning("[quanthk_feed] 南向全量读取失败（降级为空）: %s", exc)
        return pd.DataFrame()


def _latest_prices() -> pd.DataFrame:
    """最新交易日 (symbol -> close, amount)，分区内多 release 已去重。"""
    latest_dt = _latest_trade_date()
    if not latest_dt:
        return pd.DataFrame()
    df = _q(
        "SELECT symbol, close, amount FROM read_parquet("
        f"'{DATA_DIR / KLINE_REL}/**/*.parquet', hive_partitioning=1) WHERE dt = {latest_dt}"
    )
    if df.empty:
        return df
    return df.drop_duplicates(subset=["symbol"], keep="first")


def _south_recent(max_days: int = 25) -> pd.DataFrame:
    """南向持股最近 max_days 交易日窗口（内部调用，已按 query_date 排序）。"""
    raw = _read_south_safe()
    if raw.empty:
        return raw
    raw["query_date"] = raw["query_date"].astype(str)
    dates = sorted(raw["query_date"].unique())
    keep = set(dates[max(0, len(dates) - max_days) :])
    return raw[raw["query_date"].isin(keep)]


def _south_cur_sanitized(df: pd.DataFrame) -> pd.DataFrame:
    """当日南向快照防御：holding_percentage=0 但持股量仍大（>100万股）的个股，
    视为当日披露字段缺失（HKEX 快照偶发），置 NaN 以免产生虚假的清仓/减持信号。"""
    out = df.copy()
    out["holding_percentage"] = out["holding_percentage"].astype(float)
    missing = (out["holding_percentage"] == 0.0) & (
        out["holding_quantity"].fillna(0) > 1e6
    )
    out.loc[missing, "holding_percentage"] = float("nan")
    return out


def get_south_overview() -> dict[str, Any]:
    """南向资金总览：最新披露日、覆盖股票数、总持仓市值、当日增减持家数。"""

    def _load():
        empty = {
            "trade_date": "",
            "covered_stocks": 0,
            "total_hold_value_yi": 0.0,
            "up_days_change": 0,
            "down_days_change": 0,
            "south_stock_count": 0,
        }
        raw = _south_recent(max_days=5)
        if raw.empty:
            return empty
        latest_date = raw["query_date"].max()
        cur = _south_cur_sanitized(raw[raw["query_date"] == latest_date])
        prev_dates = sorted(set(raw["query_date"]))
        prev_date = prev_dates[-2] if len(prev_dates) >= 2 else None
        # 持仓市值 = 持股量 × 当日收盘价（量价拼装）
        prices = _latest_prices()
        if not prices.empty:
            cur = cur.merge(prices, on="symbol", how="left")
            cur["hold_value"] = cur["holding_quantity"].fillna(0) * cur["close"].fillna(
                0
            )
        else:
            cur["hold_value"] = 0.0
        s = cur.set_index("symbol")
        prev = (
            raw[raw["query_date"] == prev_date].set_index("symbol")
            if prev_date
            else None
        )
        pct_cur = s.get("holding_percentage")
        if prev is not None:
            pct_prev = prev.get("holding_percentage").reindex(s.index).fillna(0.0)
            delta = (pct_cur - pct_prev) * 100  # 百分点
            up = int((delta > 0.05).sum())
            down = int((delta < -0.05).sum())
        else:
            up = down = 0
        return {
            "trade_date": str(latest_date)[:10],
            "covered_stocks": int(len(s)),
            "total_hold_value_yi": fmt_yi(float(cur["hold_value"].sum())),
            "up_days_change": up,
            "down_days_change": down,
            "south_stock_count": int((s.get("holding_percentage").fillna(0) > 0).sum()),
        }

    return cached("hk_south_overview", _load)


def get_south_stock_flow(period: int = 5, limit: int = 20) -> dict[str, Any]:
    """南向个股增减持榜：按持股占比（%）的变化排序（T 窗口，默认 5 日）。"""

    def _load():
        raw = _south_recent(max_days=max(period + 2, 7))
        if raw.empty:
            return {"trade_date": "", "period": period, "increase": [], "decrease": []}
        dates = sorted(raw["query_date"].unique())
        latest = dates[-1]
        idx = max(0, len(dates) - 1 - period)
        base = dates[idx]
        cur = _south_cur_sanitized(raw[raw["query_date"] == latest]).set_index("symbol")
        prev = raw[raw["query_date"] == base].set_index("symbol")
        delta_pct = (
            (
                cur["holding_percentage"]
                - prev["holding_percentage"].reindex(cur.index).fillna(0.0)
            )
            * 100
        ).dropna()
        delta_pct = delta_pct[(delta_pct.abs() > 0.05)]
        if delta_pct.empty:
            return {
                "trade_date": str(latest)[:10],
                "period": period,
                "increase": [],
                "decrease": [],
            }
        names = _name_map()
        rows = []
        for sym, d in delta_pct.items():
            rows.append(
                {
                    "symbol": sym,
                    "name": names.get(sym, sym),
                    "pct_change_abs": round(float(d), 3),
                    "holding_pct": round(
                        float(cur.loc[sym, "holding_percentage"] * 100), 3
                    ),
                }
            )
        rows.sort(key=lambda x: x["pct_change_abs"], reverse=True)
        price = _latest_prices()
        if not price.empty:
            pm = price.drop_duplicates(subset=["symbol"]).set_index("symbol")
            for r in rows:
                if r["symbol"] in pm.index and pd.notna(pm.loc[r["symbol"], "close"]):
                    r["price"] = round(float(pm.loc[r["symbol"], "close"]), 2)
                    r["turnover_yi"] = fmt_yi(safe_float(pm.loc[r["symbol"], "amount"]))
        return {
            "trade_date": str(latest)[:10],
            "period": period,
            "increase": rows[:limit],
            "decrease": rows[-limit:][::-1],
        }

    return cached(f"hk_south_flow_{period}", _load)


def get_south_sector_flow(limit: int = 20) -> list[dict[str, Any]]:
    """南向板块偏好：行业 × 南向持仓市值/占比（揭示南下资金行业配置方向）。"""

    def _load():
        raw = _south_recent(max_days=2)
        if raw.empty or not _industry_map():
            return []
        latest = raw["query_date"].max()
        cur = _south_cur_sanitized(raw[raw["query_date"] == latest])
        prices = _latest_prices()
        prices = prices[["symbol", "close"]].drop_duplicates(subset=["symbol"])
        if prices.empty:
            return []
        cur = cur.merge(prices, on="symbol", how="left")
        cur["hold_value"] = cur["holding_quantity"].fillna(0) * cur["close"].fillna(0)
        imp = _industry_map()
        cur["industry"] = cur["symbol"].map(lambda s: imp.get(str(s), "其他"))
        g = (
            cur.groupby("industry")
            .agg(
                hold_value=("hold_value", "sum"),
                pct_avg=("holding_percentage", "mean"),
                stock_count=("symbol", "count"),
            )
            .reset_index()
        )
        g = g.sort_values("hold_value", ascending=False)
        return [
            {
                "name": row["industry"],
                "hold_value_yi": fmt_yi(row["hold_value"]),
                "pct_avg": round(float(row["pct_avg"] * 100), 2),
                "stock_count": int(row["stock_count"]),
            }
            for _, row in g.head(limit).iterrows()
        ]

    return cached("hk_south_sector_flow", _load)


# ---- 5. CCASS 席位穿透 ----


def get_ccass_rankings(limit: int = 30) -> dict[str, Any]:
    """全市场 CCASS 集中度榜（前列托管行/券商占比）。

    依据 ccass_factors（日频衍生因子）：top10 席位占比、南向席位占比、
    前十集中度（HHI，Hirschman-Herfindahl，0~1）、以及 1 日变化。
    """

    def _load():
        d = _latest_ccass_date()
        if not d:
            return {"trade_date": "", "items": []}
        df = _q(
            "SELECT symbol, ca_top10_pct, ca_south_pct, ca_cust_pct, ca_hhi_disc, "
            "ca_top10_pct_d1, ca_cust_pct_d20 FROM read_parquet("
            f"'{DATA_DIR / CCASS_FACTORS_REL}/**/*.parquet', hive_partitioning=1) "
            f"WHERE dt = {d}"
        )
        if df.empty:
            return {"trade_date": to_iso(d), "items": []}
        df = df.dropna(subset=["ca_top10_pct"])
        names = _name_map()
        df = df.copy()
        df["top10_pct"] = df["ca_top10_pct"] * 100
        df["south_pct"] = df["ca_south_pct"].fillna(0.0) * 100
        df["cust_pct"] = df["ca_cust_pct"].fillna(0.0) * 100
        df["hhi"] = df["ca_hhi_disc"].fillna(0.0)
        df["d1"] = df["ca_top10_pct_d1"].fillna(0.0)
        df["name"] = df["symbol"].map(lambda s: names.get(str(s), str(s)))
        items = [
            {
                "symbol": str(r.symbol),
                "name": r.name,
                "top10_pct": round(float(r.top10_pct), 2),
                "south_pct": round(float(r.south_pct), 2),
                "cust_pct": round(float(r.cust_pct), 2),
                "hhi": round(float(r.hhi), 3),
                "top10_d1": round(float(r.d1), 2),
            }
            for r in df.itertuples(index=False)
        ]
        items.sort(key=lambda x: x["top10_pct"], reverse=True)
        return {"trade_date": to_iso(d), "items": items[:limit]}

    return cached("hk_ccass_rankings", _load)


def get_ccass_holding(symbol: str, limit: int = 30) -> dict[str, Any]:
    """个股 CCASS 席位穿透：前 N 大托管行/券商持仓明细（最新快照）。"""

    def _load():
        d = (
            list_partition_dates(CCASS_TOP50_REL, DATA_DIR)[-1]
            if list_partition_dates(CCASS_TOP50_REL, DATA_DIR)
            else None
        )
        if not d:
            return {"trade_date": "", "symbol": symbol, "name": symbol, "items": []}
        sym = symbol.upper()
        if not sym.endswith(".HK"):
            sym = sym + ".HK"
        df = _q(
            "SELECT stock_code, participant_id, participant_name, holding_quantity, "
            "holding_percentage FROM read_parquet("
            f"'{DATA_DIR / CCASS_TOP50_REL}/**/*.parquet', hive_partitioning=1) "
            f"WHERE dt = {d} AND stock_code = '{sym}'"
        )
        names = _name_map()
        if df.empty:
            return {
                "trade_date": to_iso(d),
                "symbol": sym,
                "name": names.get(sym, sym),
                "items": [],
            }
        df = df.dropna(subset=["participant_id"])
        items = [
            {
                "participant_id": str(r.participant_id),
                "participant_name": str(r.participant_name),
                "holding_quantity": int(r.holding_quantity or 0),
                "holding_pct": round(float(r.holding_percentage * 100 or 0.0), 3),
            }
            for r in df.itertuples(index=False)
        ]
        items.sort(key=lambda x: x["holding_pct"], reverse=True)
        return {
            "trade_date": to_iso(d),
            "symbol": sym,
            "name": names.get(sym, sym),
            "items": items[:limit],
        }

    return cached(f"hk_ccass_holding_{symbol.upper()}", _load)


def get_ccass_movers(limit: int = 20) -> dict[str, Any]:
    """CCASS 活跃度：席位新进/退出的异动股票（机构筹码变动信号）。"""

    def _load():
        d = _latest_ccass_date()
        if not d:
            return {"trade_date": "", "ne_entrants": [], "exits": []}
        df = _q(
            "SELECT symbol, ca_new_entrants_1d, ca_exits_1d, ca_top10_pct_d1 "
            "FROM read_parquet("
            f"'{DATA_DIR / CCASS_FACTORS_REL}/**/*.parquet', hive_partitioning=1) "
            f"WHERE dt = {d}"
        )
        if df.empty:
            return {"trade_date": to_iso(d), "new_entrants": [], "exits": []}
        names = _name_map()
        neu = df[df["ca_new_entrants_1d"].fillna(0) > 0]
        ext = df[df["ca_exits_1d"].fillna(0) > 0]

        def _rows(sub):
            return [
                {
                    "symbol": str(r.symbol),
                    "name": names.get(str(r.symbol), str(r.symbol)),
                    "count": int(r.ca_new_entrants_1d if sub is neu else r.ca_exits_1d),
                    "top10_d1": round(
                        float(r.ca_top10_pct_d1 * 100)
                        if pd.notna(r.ca_top10_pct_d1)
                        else 0.0,
                        2,
                    ),
                }
                for r in sub.itertuples(index=False)
            ]

        return {
            "trade_date": to_iso(d),
            "new_entrants": _rows(neu)[:limit],
            "exits": _rows(ext)[:limit],
        }

    return cached("hk_ccass_movers", _load)


# ---- 6. AH 对应股联动 ----


@lru_cache(maxsize=1)
def _ah_pairs_df() -> pd.DataFrame:
    try:
        return _q(
            "SELECT h_symbol, a_symbol, 名称 FROM read_parquet("
            f"'{DATA_DIR}/2_base_sector/ah_membership.parquet')"
        )
    except Exception:
        return pd.DataFrame()


def get_ah_pairs(limit: int = 50) -> dict[str, Any]:
    """AH 对应股联动：港股侧当日涨跌幅 + 对应 A 股代码 + AH 溢价（两地比价）。

    溢价列来自 ah_premium 最新分区：>0 = A 贵 H 便宜（H 折价），<0 = 倒挂。
    """

    def _load():
        pairs = _ah_pairs_df()
        if pairs.empty:
            return {"trade_date": "", "items": []}
        latest, snap = _market_pct_snapshot()
        if not latest or snap.empty:
            return {"trade_date": "", "items": []}
        names = _name_map()
        pm = snap.set_index("symbol")
        # AH 溢价（最新分区）：h_symbol -> premium_pct
        prem = {}
        dates = list_partition_dates("2_base_sector/ah_premium", DATA_DIR)
        if dates:
            try:
                pd_ = _q(
                    "SELECT h_symbol, premium_pct FROM read_parquet("
                    f"'{DATA_DIR}/2_base_sector/ah_premium/dt=*/data.parquet', hive_partitioning=1"
                    f") WHERE dt = {dates[-1]}"
                )
                prem = dict(zip(pd_["h_symbol"].astype(str), pd_["premium_pct"], strict=True))
            except Exception as exc:
                logger.warning("[quanthk_feed] AH 溢价 join 失败（忽略该列）: %s", exc)
        items = []
        for r in pairs.itertuples(index=False):
            h = str(r.h_symbol)
            if h not in pm.index:
                continue
            items.append(
                {
                    "h_symbol": h,
                    "h_name": names.get(h, h),
                    "h_pct_change": round(float(pm.loc[h, "pct_change"]), 2),
                    "h_close": round(float(pm.loc[h, "close"]), 2),
                    "a_symbol": str(r.a_symbol),
                    "cn_name": str(r.名称),
                    "premium_pct": round(float(prem[h]), 2) if h in prem else None,
                }
            )
        items.sort(key=lambda x: x["h_pct_change"], reverse=True)
        return {"trade_date": to_iso(latest), "items": items[:limit]}

    return cached("hk_ah_pairs", _load)


def get_sector_valuation(limit: int = 24) -> list[dict[str, Any]]:
    """行业估值温度计 — 恒生行业 ×（PE 中位数 / 平均股息率 / 成分覆盖）。

    用途：轮动之外叠加估值维度，识别「低 PE + 高股息」的价值洼地行业。
    数据为 akshare 快照（published_at 标注语义）。
    """

    def _load():
        imp = _industry_map()
        if not imp:
            return []
        try:
            val = _q(
                'SELECT symbol, "市盈率-TTM" FROM read_parquet('
                f"'{DATA_DIR}/2_base_sector/akshare_valuation/*.parquet', union_by_name=true)"
            )
            fin = _q(
                'SELECT symbol, "股息率TTM(%)" FROM read_parquet('
                f"'{DATA_DIR}/2_base_sector/akshare_financial/*.parquet', union_by_name=true)"
            )
        except Exception as exc:
            logger.warning("[quanthk_feed] 行业估值读取失败: %s", exc)
            return []
        val["industry"] = val["symbol"].astype(str).map(imp)
        fin["industry"] = fin["symbol"].astype(str).map(imp)
        val = val[val["industry"].notna()].copy()
        fin = fin[fin["industry"].notna()].copy()
        val["_pe"] = val["市盈率-TTM"].apply(
            lambda x: float(x) if isinstance(x, (int, float)) and x > 0 else None
        )
        fin["_dy"] = fin["股息率TTM(%)"].apply(
            lambda x: float(x) if isinstance(x, (int, float)) and x > 0 else None
        )
        rows = []
        for ind, g in fin.groupby("industry"):
            pe_med = val[val["industry"] == ind]["_pe"].median()
            dy = g["_dy"].dropna()
            rows.append({
                "name": ind,
                "pe_median": round(float(pe_med), 1) if pd.notna(pe_med) else None,
                "dividend_yield": round(float(dy.median()), 2) if not dy.empty else None,
                "stock_count": int(len(g)),
            })
        rows.sort(key=lambda x: (x["dividend_yield"] is None, -(x["dividend_yield"] or -1)))
        return rows[:limit]

    return cached("hk_sector_valuation", _load)


# ---- 7. 高股息 / 低估值（港股特色主题） ----


def get_valuation_rankings(kind: str = "dividend", limit: int = 20) -> dict[str, Any]:
    """港股特色估值榜：dividend=高股息率 / pe=低PE-TTM / pb=低PB-MRQ。

    数据为 akshare 快照（非逐日），返回 published_at 供前端标注快照时间。
    """

    def _load():
        if kind == "dividend":
            try:
                df = _q(
                    'SELECT symbol, "股息率TTM(%)", "总市值(港元)", "市盈率", '
                    '"市净率", published_at FROM read_parquet('
                    f"'{DATA_DIR}/2_base_sector/akshare_financial/*.parquet', union_by_name=true)"
                )
                val_col, desc = "股息率TTM(%)", "高股息率"
            except Exception:
                return {
                    "kind": kind,
                    "titlename": "高股息率",
                    "published_at": "",
                    "items": [],
                }
        else:
            # pe/pb/ps/pcf 统一走 akshare_valuation（TTM 口径，升序）
            kind_map = {
                "pe": ("市盈率-TTM", "低 PE-TTM", "市净率-MRQ"),
                "pb": ("市净率-MRQ", "低 PB-MRQ", "市盈率-TTM"),
                "ps": ("市销率-TTM", "低 PS-TTM", "市盈率-TTM"),
                "pcf": ("市现率-TTM", "低 PCF-TTM", "市盈率-TTM"),
            }
            try:
                val_col, desc, sub_col = kind_map[kind]
                df = _q(
                    "SELECT symbol, "
                    f'"{val_col}", "{sub_col}", published_at FROM read_parquet('
                    f"'{DATA_DIR}/2_base_sector/akshare_valuation/*.parquet', union_by_name=true)"
                )
            except Exception:
                return {"kind": kind, "titlename": desc, "published_at": "", "items": []}
        df.columns = [str(c) for c in df.columns]
        if val_col not in df.columns:
            return {"kind": kind, "titlename": desc, "published_at": "", "items": []}
        df = df.dropna(subset=[val_col, "symbol"])
        df = df[df[val_col].apply(lambda x: isinstance(x, (int, float)))].copy()
        df["_v"] = df[val_col].astype(float)
        df = df[df["_v"] > 0]
        if kind == "dividend":
            # 高股息榜剔除小市值公司（<10 亿港元）：特殊股息/数据噪声密集区
            if "总市值(港元)" in df.columns:
                df["_cap"] = df["总市值(港元)"].apply(safe_float)
                df = df[df["_cap"] >= 1e9]
            df = df.sort_values("_v", ascending=False)
        else:
            df = df.sort_values("_v", ascending=True)
        names = _name_map()
        items = []
        for _, row in df.head(limit).iterrows():
            item = {
                "symbol": str(row["symbol"]),
                "name": names.get(str(row["symbol"]), str(row["symbol"])),
                "value": round(float(row["_v"]), 2),
            }
            if kind == "dividend":
                if "总市值(港元)" in df.columns:
                    item["total_market_cap_yi"] = fmt_yi(
                        safe_float(row.get("总市值(港元)"))
                    )
                if "市盈率" in df.columns:
                    item["pe"] = round(safe_float(row.get("市盈率")), 2)
            elif kind == "pe":
                if "市净率-MRQ" in df.columns:
                    item["pb"] = round(safe_float(row.get("市净率-MRQ")), 2)
            else:
                # pb/ps/pcf 副列统一附 PE-TTM 作对照
                if sub_col in df.columns:
                    item["pe"] = round(safe_float(row.get(sub_col)), 2)
            items.append(item)
        pub = df["published_at"].max() if "published_at" in df.columns else ""
        return {
            "kind": kind,
            "titlename": desc,
            "published_at": str(pub)[:10],
            "items": items,
        }

    return cached(f"hk_valuation_{kind}", _load)


def get_ah_premium_rankings(limit: int = 20) -> dict[str, Any]:
    """AH 溢价榜 — 同一资产两地比价（港股通/北水最喜欢盯的维度）。

    premium_pct > 0：A 股比 H 股贵（H 折价，买 H 更划算）；< 0：倒挂（A 折价）。
    数据来自 ah_premium 日频分区（HKEX/akshare，含汇率 fx_hkd_cny）。
    """
    def _load():
        empty = {"trade_date": "", "premium": [], "discount": []}
        dates = list_partition_dates("2_base_sector/ah_premium", DATA_DIR)
        if not dates:
            return empty
        d = dates[-1]
        try:
            df = _q(
                "SELECT h_symbol, a_symbol, a_close, h_close, fx_hkd_cny, premium_pct "
                "FROM read_parquet("
                f"'{DATA_DIR}/2_base_sector/ah_premium/dt=*/data.parquet', hive_partitioning=1"
                f") WHERE dt = {d}"
            )
        except Exception as exc:
            logger.warning("[quanthk_feed] AH 溢价读取失败: %s", exc)
            return empty
        if df.empty:
            return empty
        df = df.dropna(subset=["premium_pct", "h_symbol"])
        names = _name_map()
        rows = []
        for r in df.itertuples(index=False):
            rows.append({
                "h_symbol": str(r.h_symbol),
                "h_name": names.get(str(r.h_symbol), str(r.h_symbol)),
                "a_symbol": str(r.a_symbol),
                "a_close": round(safe_float(r.a_close), 2),
                "h_close": round(safe_float(r.h_close), 2),
                "fx_hkd_cny": round(safe_float(r.fx_hkd_cny), 4),
                "premium_pct": round(float(r.premium_pct), 2),
            })
        # premium 降序 = A 贵 H 便宜（H 折价榜）；升序 = A 便宜 H 贵（倒挂榜）
        premium = sorted(rows, key=lambda x: x["premium_pct"], reverse=True)[:limit]
        discount = sorted(rows, key=lambda x: x["premium_pct"])[:limit]
        return {"trade_date": to_iso(d), "premium": premium, "discount": discount}
    return cached("hk_ah_premium", _load)


def get_dividend_calendar(days: int = 60, limit: int = 40) -> dict[str, Any]:
    """派息日历 — 未来 days 天内除息（ex_date）的港股公司。

    港股高息文化特色：提前发现「除息前买入窗口 / 除息后税后收益」标的。
    """
    def _load():
        from datetime import date, timedelta

        empty = {"trade_date": "", "items": []}
        today = date.today()
        end = today + timedelta(days=days)
        try:
            df = _q(
                "SELECT symbol, ex_date, pay_date, plan, dividend FROM read_parquet("
                f"'{DATA_DIR}/3_financial_data/dividend/*.parquet', union_by_name=true"
                ")"
            )
        except Exception as exc:
            logger.warning("[quanthk_feed] 派息日历读取失败: %s", exc)
            return empty
        if df.empty:
            return empty
        df = df[df["ex_date"].notna()].copy()
        df["ex_date"] = pd.to_datetime(df["ex_date"]).dt.date
        df = df[(df["ex_date"] >= today) & (df["ex_date"] <= end)]
        if df.empty:
            return {"trade_date": "", "items": []}
        names = _name_map()
        items = []
        for r in df.sort_values("ex_date").itertuples(index=False):
            items.append({
                "symbol": str(r.symbol),
                "name": names.get(str(r.symbol), str(r.symbol)),
                "ex_date": str(r.ex_date),
                "pay_date": str(r.pay_date)[:10] if pd.notna(r.pay_date) else "",
                "plan": str(r.plan) if pd.notna(r.plan) else "",
                "dividend": round(safe_float(r.dividend), 4) if pd.notna(r.dividend) else None,
            })
        return {"trade_date": "", "items": items[:limit]}
    return cached("hk_dividend_calendar", _load)


def get_stock_detail(symbol: str) -> dict[str, Any]:
    """个股综合详情（港股右侧面板）— CCASS 席位 / 南向持股 / 估值 / 分红 / 财务 / 分析师。

    聚合 quanthk 各本地数据集，供 /research/hk/stock-detail 一次拉取。
    """
    from datetime import date, timedelta

    sym = symbol.upper()
    if not sym.endswith(".HK"):
        sym = sym + ".HK"
    names = _name_map()
    name = names.get(sym, sym)
    result: dict[str, Any] = {
        "symbol": sym, "name": name,
        "ccass": {"trade_date": "", "total_pct": 0.0, "top": []},
        "south": {"trade_date": "", "holding_pct": None, "holding_quantity": None,
                  "d1": None, "d5": None, "d20": None, "series": []},
        "valuation": {}, "dividend": [], "financial": {}, "analyst": {},
    }

    # 1) CCASS 席位（前 10）与汇总
    try:
        cc = get_ccass_holding(sym, limit=10)
        result["ccass"]["trade_date"] = cc.get("trade_date", "")
        result["ccass"]["top"] = [
            {"participant_id": it["participant_id"], "participant_name": it["participant_name"],
             "holding_pct": it["holding_pct"]} for it in cc.get("items", [])
        ]
        result["ccass"]["total_pct"] = round(sum(it["holding_pct"] for it in cc.get("items", [])), 2)
    except Exception as exc:
        logger.warning("[quanthk_feed] stock_detail ccass(%s): %s", sym, exc)

    # 2) 南向持股：最新 + 5/20 日变化 + 近 20 日序列
    try:
        raw = _read_south_safe()
        if not raw.empty:
            raw = raw[raw["symbol"] == sym].copy()
            raw["query_date"] = raw["query_date"].astype(str)
            raw = raw.sort_values("query_date")
            if not raw.empty:
                latest = raw.iloc[-1]
                result["south"]["trade_date"] = str(latest["query_date"])[:10]
                result["south"]["holding_pct"] = round(float(latest["holding_percentage"]) * 100, 3)
                result["south"]["holding_quantity"] = int(latest["holding_quantity"])
                pct_col = raw["holding_percentage"].astype(float) * 100
                result["south"]["d1"] = round(float(pct_col.iloc[-1] - pct_col.iloc[-2]), 3) if len(pct_col) >= 2 else None
                result["south"]["d5"] = round(float(pct_col.iloc[-1] - pct_col.iloc[-6]), 3) if len(pct_col) >= 6 else None
                result["south"]["d20"] = round(float(pct_col.iloc[-1] - pct_col.iloc[-21]), 3) if len(pct_col) >= 21 else None
                _tail = raw.iloc[-20:]
                result["south"]["series"] = [
                    {"date": str(r.query_date)[:10], "pct": round(float(r.holding_percentage) * 100, 2)}
                    for r in _tail.itertuples(index=False)
                ]
    except Exception as exc:
        logger.warning("[quanthk_feed] stock_detail south(%s): %s", sym, exc)

    # 3) 估值快照（akshare_valuation + financial）
    try:
        v = _q(
            'SELECT symbol, "市盈率-TTM", "市净率-MRQ", "市销率-TTM", published_at FROM read_parquet('
            f"'{DATA_DIR}/2_base_sector/akshare_valuation/*.parquet', union_by_name=true)"
        )
        row = v[v["symbol"] == sym]
        if not row.empty:
            r = row.iloc[0]
            result["valuation"].update({
                "pe_ttm": round(float(r["市盈率-TTM"]), 2) if pd.notna(r["市盈率-TTM"]) else None,
                "pb": round(float(r["市净率-MRQ"]), 2) if pd.notna(r["市净率-MRQ"]) else None,
                "ps_ttm": round(float(r["市销率-TTM"]), 2) if pd.notna(r["市销率-TTM"]) else None,
                "published_at": str(r.get("published_at", ""))[:10],
            })
        f = _q(
            'SELECT symbol, "股息率TTM(%)", "总市值(港元)", "市盈率", "市净率", "股东权益回报率(%)" '
            'FROM read_parquet('
            f"'{DATA_DIR}/2_base_sector/akshare_financial/*.parquet', union_by_name=true)"
        )
        rowf = f[f["symbol"] == sym]
        if not rowf.empty:
            rf = rowf.iloc[0]
            result["valuation"].update({
                "dividend_yield": round(float(rf["股息率TTM(%)"]), 2) if pd.notna(rf["股息率TTM(%)"]) else None,
                "total_mv_yi": round(float(rf["总市值(港元)"]) / 1e8, 1) if pd.notna(rf["总市值(港元)"]) else None,
            })
            result["financial"] = {
                "roe": round(float(rf["股东权益回报率(%)"]), 2) if pd.notna(rf["股东权益回报率(%)"]) else None,
            }
    except Exception as exc:
        logger.warning("[quanthk_feed] stock_detail valuation(%s): %s", sym, exc)

    # 4) 财务速览（营收/净利/毛利率/每股股息）
    try:
        f2 = _q(
            'SELECT symbol, "营业总收入", "净利润", "销售净利率(%)", "每股股息TTM(港元)", "基本每股收益(元)" '
            'FROM read_parquet('
            f"'{DATA_DIR}/2_base_sector/akshare_financial/*.parquet', union_by_name=true)"
        )
        rowf2 = f2[f2["symbol"] == sym]
        if not rowf2.empty:
            r2 = rowf2.iloc[0]
            result["financial"].update({
                "revenue": float(r2["营业总收入"]) if pd.notna(r2["营业总收入"]) else None,
                "net_profit": float(r2["净利润"]) if pd.notna(r2["净利润"]) else None,
                "net_margin": round(float(r2["销售净利率(%)"]), 2) if pd.notna(r2["销售净利率(%)"]) else None,
                "dps_ttm": round(float(r2["每股股息TTM(港元)"]), 4) if pd.notna(r2["每股股息TTM(港元)"]) else None,
                "eps": round(float(r2["基本每股收益(元)"]), 4) if pd.notna(r2["基本每股收益(元)"]) else None,
            })
    except Exception as exc:
        logger.warning("[quanthk_feed] stock_detail financial(%s): %s", sym, exc)

    # 5) 分红历史（近 12 条）
    try:
        # dividend 表 symbol 存在 5 位（00700）与 4 位（0700.HK）两种格式：归一化互查
        _digits = "".join(ch for ch in sym if ch.isdigit()).lstrip("0")
        _alt = f"{_digits}.HK" if _digits else sym
        _q5 = _digits.zfill(5)
        d = _q(
            "SELECT ex_date, pay_date, plan, dividend, trade_date FROM read_parquet("
            f"'{DATA_DIR}/3_financial_data/dividend/*.parquet', union_by_name=true"
            ") WHERE symbol IN ('"
            + sym
            + "', '"
            + _alt
            + "', '"
            + _q5
            + "')"
        )
        if not d.empty:
            # 两种 schema 并存：akshare 详表有 ex_date/plan；yahoo 历史仅 trade_date/dividend。
            # 以「除息日（缺省用 trade_date）降序」合并展示完整派息历史。
            def _d(row):
                v = row.ex_date if pd.notna(row.ex_date) else row.trade_date
                return pd.to_datetime(v) if pd.notna(v) else pd.NaT

            d = d.copy()
            d["disp_date"] = d.apply(_d, axis=1)
            d = d[d["disp_date"].notna()].sort_values("disp_date", ascending=False)
            result["dividend"] = [
                {
                    "ex_date": str(r.disp_date.date()),
                    "pay_date": str(r.pay_date)[:10] if pd.notna(r.pay_date) else "",
                    "plan": str(r.plan) if pd.notna(r.plan) else "",
                    "dividend": round(float(r.dividend), 4) if pd.notna(r.dividend) else None,
                }
                for r in d.head(12).itertuples(index=False)
            ]
    except Exception as exc:
        logger.warning("[quanthk_feed] stock_detail dividend(%s): %s", sym, exc)

    # 6) 分析师：目标价 + 评级
    try:
        pt = _q(
            "SELECT mean, high, low FROM read_parquet("
            f"'{DATA_DIR}/4_analyst/analyst_price_targets/*.parquet', union_by_name=true"
            ") WHERE symbol = '"
            + sym
            + "'"
        )
        if not pt.empty:
            r = pt.iloc[0]
            result["analyst"]["price_target"] = {
                "mean": round(float(r["mean"]), 2) if pd.notna(r["mean"]) else None,
                "high": round(float(r["high"]), 2) if pd.notna(r["high"]) else None,
                "low": round(float(r["low"]), 2) if pd.notna(r["low"]) else None,
            }
        rec = _q(
            "SELECT period, strongBuy, buy, hold, sell, strongSell FROM read_parquet("
            f"'{DATA_DIR}/4_analyst/recommendations/*.parquet', union_by_name=true"
            ") WHERE symbol = '"
            + sym
            + "'"
        )
        if not rec.empty:
            rec = rec.sort_values("period", ascending=False).iloc[0]
            buys = float(rec["strongBuy"] or 0) + float(rec["buy"] or 0)
            holds = float(rec["hold"] or 0)
            sells = float(rec["sell"] or 0) + float(rec["strongSell"] or 0)
            total = buys + holds + sells
            result["analyst"]["recommendation"] = {
                "period": str(rec["period"]),
                "buy": buys, "hold": holds, "sell": sells,
                "buy_ratio": round(buys / total * 100, 1) if total else None,
            }
    except Exception as exc:
        logger.warning("[quanthk_feed] stock_detail analyst(%s): %s", sym, exc)

    return cached(f"hk_stock_detail_{sym}", lambda: result, ttl=300.0)


# ---- 8. 行业轮动（多周期强弱） ----


def get_profit_leaders(limit: int = 10) -> dict[str, Any]:
    """个股综合赚钱效应 Top N — 港股特色口径。

    评分 = 当日涨跌幅(线性贡献) + 成交活跃度(对数贡献)：
      score = pct_change + 2 * log10(amount/1e8)
    （涨幅 5% + 成交 10 亿 ≈ 9 分；涨幅 8% + 成交 0.5 亿 ≈ 8.4 分）
    过滤条件：当日成交额 >= 5000 万港元（剔除仙股/无量异动）、
    涨跌幅在 ±10% 内（剔除停牌复牌/公司行动残渣）、当日有正常收盘价。
    """

    def _load():
        import math

        empty = {"trade_date": "", "items": []}
        if not _avail():
            return empty
        latest, snap = _market_pct_snapshot()
        if not latest or snap.empty:
            return empty
        df = snap.copy()
        df["amount"] = df["amount"].fillna(0.0)
        df = df[df["amount"] >= 5e7]  # 5000 万港元流动性门槛
        df = df[df["pct_change"].abs() <= 10.0]  # 正常日内波动范围
        if df.empty:
            return empty
        df["score"] = df["pct_change"] + 2.0 * df["amount"].apply(
            lambda a: math.log10(max(a / 1e8, 1.0))
        )
        df = df.sort_values("score", ascending=False).head(limit)
        names = _name_map()
        items = [
            {
                "symbol": str(r.symbol),
                "name": names.get(str(r.symbol), str(r.symbol)),
                "pct_change": round(float(r.pct_change), 2),
                "turnover_yi": fmt_yi(float(r.amount)),
                "score": round(float(r.score), 2),
            }
            for r in df.itertuples(index=False)
        ]
        return {"trade_date": to_iso(latest), "items": items}

    return cached("hk_profit_leaders", _load)


def get_sector_rotation(limit: int = 24) -> dict[str, Any]:
    """恒生行业轮动：1/5/20 日平均涨幅 + 今日成交额，识别强弱板块切换。"""

    def _load():
        days = _trading_days(None, 40)
        if len(days) < 5:
            return {"trade_date": "", "items": []}
        dt_in = partition_dates_to_sql(days)
        k = _q(
            "SELECT symbol, dt, close, amount FROM read_parquet("
            f"'{DATA_DIR / KLINE_REL}/**/*.parquet', hive_partitioning=1) "
            f"WHERE dt IN ({dt_in})"
        )
        if k.empty:
            return {"trade_date": "", "items": []}
        imp = _industry_map()
        if not imp:
            return {"trade_date": "", "items": []}
        k["industry"] = k["symbol"].map(lambda s: imp.get(str(s), "其他"))
        # 行业日收益率（同日成份股均值，避免个股权重差异）
        k = k[k["close"].fillna(0) > 0].copy()
        k["dt"] = k["dt"].astype(str)
        latest = days[0]
        # 收盘价均值面板 → 行业指数（等权）
        panel = k.pivot_table(
            index="dt", columns="industry", values="close", aggfunc="mean"
        )
        amounts = k[k["dt"] == latest].groupby("industry")["amount"].sum()
        items = []
        for ind in panel.columns:
            closes = panel[ind].dropna()
            if len(closes) < 5:
                continue
            ret1 = (closes.iloc[-1] / closes.iloc[-2] - 1) * 100
            ret5 = (closes.iloc[-1] / closes.iloc[-5] - 1) * 100
            ret20 = (
                (closes.iloc[-1] / closes.iloc[min(20, len(closes))] - 1) * 100
                if len(closes) > 20
                else None
            )
            items.append(
                {
                    "name": ind,
                    "ret_1d": round(float(ret1), 2),
                    "ret_5d": round(float(ret5), 2),
                    "ret_20d": round(float(ret20), 2) if ret20 is not None else None,
                    "turnover_yi": fmt_yi(amounts.get(ind, 0.0)),
                }
            )
        items.sort(key=lambda x: (x["ret_5d"] is None, -(x["ret_5d"] or -999)))
        return {"trade_date": to_iso(latest), "items": items[:limit]}

    return cached("hk_sector_rotation", _load)


def feed_status() -> dict[str, Any]:
    """数据可用性与最新日期状态（供 /status 端点与前端诊断）。"""
    return {
        "available": _avail(),
        "data_dir": str(DATA_DIR),
        "latest_kline_date": to_iso(_latest_trade_date())
        if _latest_trade_date()
        else None,
        "latest_ccass_date": to_iso(_latest_ccass_date())
        if _latest_ccass_date()
        else None,
        "industry_count": len(_industry_map()),
        "south_files": len(_south_file_list()),
    }


# ---- 7. 机构持仓分析（CCASS 席位 × 资金属性分类） ----
#
# 口径（与 institutional_classifier 配套，均基于 ccass_top50 单源）：
# - 分类：内资（cn_broker=中資券商 + southbound=港股通A席）/ 港资（默认桶）/
#   外资·欧美 us_eu / 外资·亚太 apac / 其他 other；香港中央結算(代理人) 防御类不参与加总
# - 增减持 = 席位持仓量跨分区差分（5/20/60 个 ccass 分区），含过户/结算噪音；
#   估算市值 = 数量 × 最新收盘价（不复权口径，除净日有偏差）
# - 南向（hsgt_south）不参与加总：港股通持股已在 CCASS 的 A 席内，仅作交叉校验

INST_OVERRIDE_REL = "2_base_sector/institutional_overrides.parquet"
INST_TREND_DAYS = 61  # 个股趋势序列最近分区数（含当前 = 60+1）
INST_WINDOWS = (5, 20, 60)

# 搜索用简繁字形折叠表（证券简称高频字；数据侧为简体，用户可能输繁体）
_SC_TC_PAIRS: tuple[tuple[str, str], ...] = (
    ("腾", "騰"), ("讯", "訊"), ("汇", "匯"), ("丰", "豐"), ("银", "銀"),
    ("证", "證"), ("东", "東"), ("亚", "亞"), ("电", "電"), ("铁", "鐵"),
    ("药", "藥"), ("医", "醫"), ("华", "華"), ("车", "車"), ("贸", "貿"),
    ("险", "險"), ("园", "園"), ("运", "運"), ("农", "農"), ("湾", "灣"),
    ("龙", "龍"), ("台", "臺"), ("万", "萬"), ("亿", "億"), ("风", "風"),
    ("财", "財"), ("长", "長"), ("广", "廣"), ("国", "國"), ("门", "門"),
    ("开", "開"), ("发", "發"), ("达", "達"), ("团", "團"), ("产", "產"),
    ("实", "實"), ("业", "業"), ("债", "債"), ("权", "權"), ("机", "機"),
    ("胜", "勝"), ("环", "環"), ("气", "氣"), ("矿", "礦"), ("护", "護"),
    ("网", "網"), ("飞", "飛"), ("阳", "陽"), ("馆", "館"),
    ("宁", "寧"), ("万", "萬"), ("卫", "衛"), ("宝", "寶"), ("滨", "濱"),
    ("济", "濟"), ("苏", "蘇"), ("沪", "滬"), ("深", "深"), ("星", "星"),
    ("变", "變"), ("态", "態"), ("极", "極"), ("远", "遠"), ("进", "進"),
    ("续", "續"), ("车", "車"), ("联", "聯"), ("华", "華"), ("复", "復"),
    ("丰", "豐"), ("丽", "麗"), ("杰", "傑"), ("优", "優"), ("势", "勢"),
    ("拟", "擬"), ("价", "價"), ("额", "額"), ("营", "營"), ("义", "義"),
    ("礼", "禮"), ("劲", "勁"), ("丽", "麗"), ("洁", "潔"), ("乐", "樂"),
    ("欢", "歡"), ("观", "觀"), ("款", "款"), ("间", "間"), ("门", "門"),
    ("动", "動"), ("际", "際"), ("设", "設"), ("协", "協"), ("领", "領"),
    ("岛", "島"), ("润", "潤"), ("维", "維"), ("创", "創"), ("鹏", "鵬"),
    ("声", "聲"), ("兴", "興"), ("凤", "鳳"), ("仓", "倉"), ("捞", "撈"),
    ("师", "師"), ("烟", "煙"), ("导", "導"), ("货", "貨"), ("图", "圖"),
    ("书", "書"), ("纽", "紐"), ("约", "約"), ("云", "雲"), ("货", "貨"),
    ("当", "當"), ("时", "時"), ("样", "樣"), ("关", "關"), ("买", "買"),
    ("卖", "賣"), ("线", "線"), ("红", "紅"), ("绿", "綠"), ("帮", "幫"),
    ("邮", "郵"), ("铁", "鐵"), ("窝", "窩"), ("涛", "濤"), ("桥", "橋"),
)
# 繁体异体字 → 简体（如 証/證 同为繁，各自对应 证）
_EXTRA_TC_SC: tuple[tuple[str, str], ...] = (
    ("証", "证"), ("昇", "升"), ("恆", "恒"), ("羣", "群"), ("峯", "峰"),
    ("裏", "里"), ("裡", "里"), ("啓", "启"), ("爲", "为"), ("螞", "蚂"),
    ("滙", "汇"), ("贛", "赣"), ("鋭", "锐"), ("峯", "峰"), ("盃", "杯"),
)


def _build_fold_maps() -> tuple[dict[str, str], dict[str, str]]:
    sc2tc = dict(_SC_TC_PAIRS)
    tc2sc = {tc: sc for sc, tc in _SC_TC_PAIRS}
    for tc, sc in _EXTRA_TC_SC:
        tc2sc[tc] = sc
    return sc2tc, tc2sc


_SC2TC, _TC2SC = _build_fold_maps()
_SC_TO_TC_MAP = str.maketrans("".join(_SC2TC), "".join(_SC2TC[k] for k in _SC2TC))
_TC_TO_SC_MAP = str.maketrans("".join(_TC2SC), "".join(_TC2SC[k] for k in _TC2SC))


def _text_variants(text: str) -> set[str]:
    """输入文本的简繁字形变体（原样 + 简→繁 + 繁→简）。"""
    return {
        text,
        text.translate(_SC_TO_TC_MAP),
        text.translate(_TC_TO_SC_MAP),
    }


def _ccass_top_dates(n: int) -> list[str]:
    """ccass_top50 自身单位分区日历最近 n 天（升序；n<=0 返空）。"""
    dates = list_partition_dates(CCASS_TOP50_REL, DATA_DIR)
    return dates[-n:] if n > 0 else []


def _latest_ccass_top_date() -> str | None:
    d = _ccass_top_dates(1)
    return d[-1] if d else None


def _load_ccass_window(dates: list[str]) -> pd.DataFrame:
    """读取指定日期的 CCASS 明细（内部列裁剪 + 类型清洗）。"""
    if not dates:
        return pd.DataFrame()
    dt_in = partition_dates_to_sql(dates)
    df = _q(
        "SELECT stock_code, participant_id, participant_name, holding_quantity, "
        "holding_percentage, dt FROM read_parquet("
        f"'{DATA_DIR / CCASS_TOP50_REL}/dt=*/data.parquet', hive_partitioning=1) "
        f"WHERE dt IN ({dt_in})"
    )
    if df.empty:
        return df
    df["dt"] = df["dt"].astype(str)  # hive 分区列 int64 → str
    df = df[df["stock_code"].astype(str).str.endswith(".HK")].copy()
    df["holding_quantity"] = df["holding_quantity"].fillna(0).astype("int64")
    return df


def _participant_registry() -> dict[tuple[str | None, str], tuple[str, str]]:
    """(participant_id, participant_name) → (category, kind) 名册。

    近 10 个分区 DISTINCT 全量参与者（≈500 家），人工覆盖表优先，
    规则未收录的新席位在聚合时即时 classify() 兜底。缓存 6 小时。
    """
    def _load() -> dict[tuple[str | None, str], tuple[str, str]]:
        dates = _ccass_top_dates(10)
        if not dates:
            return {}
        dt_in = partition_dates_to_sql(dates)
        df = _q(
            "SELECT DISTINCT participant_id, participant_name FROM read_parquet("
            f"'{DATA_DIR / CCASS_TOP50_REL}/dt=*/data.parquet', hive_partitioning=1) "
            f"WHERE dt IN ({dt_in})"
        )
        overrides = load_overrides(DATA_DIR)
        out: dict[tuple[str | None, str], tuple[str, str]] = {}
        for r in df.itertuples(index=False):
            pid = None if pd.isna(r.participant_id) else str(r.participant_id)
            nm = "" if pd.isna(r.participant_name) else str(r.participant_name)
            out[(pid, nm)] = classify(pid, nm, overrides)
        return out

    return cached("hk_inst_participants", _load, ttl=6 * 3600)


def _apply_categories(df: pd.DataFrame) -> pd.DataFrame:
    """给 CCASS 明细行附加 category/kind 两列（名册 merge，未收录行即时兜底）。"""
    out = df.copy()
    out["_pid"] = out["participant_id"].map(
        lambda v: None if pd.isna(v) else str(v)
    )
    out["_nm"] = out["participant_name"].map(
        lambda v: "" if pd.isna(v) else str(v)
    )
    reg = _participant_registry()
    if reg:
        reg_df = pd.DataFrame(
            [
                {"_pid": pid, "_nm": nm, "category": cat, "kind": kind}
                for (pid, nm), (cat, kind) in reg.items()
            ]
        )
        out = out.merge(reg_df, on=["_pid", "_nm"], how="left")
    else:
        out["category"] = pd.NA
        out["kind"] = pd.NA
    miss = out["category"].isna()
    if miss.any():
        overrides = load_overrides(DATA_DIR)
        for idx in out.index[miss]:
            pid = out.at[idx, "_pid"]
            nm = out.at[idx, "_nm"]
            cat, kind = classify(pid, nm, overrides)
            out.at[idx, "category"] = cat
            out.at[idx, "kind"] = kind
    return out.drop(columns=["_pid", "_nm"])


def _inst_prices() -> dict[str, float]:
    """symbol → 最新收盘价（缺失为 0，估算市值兜底）。"""
    df = _latest_prices()
    if df.empty:
        return {}
    return {
        str(s): float(c) if pd.notna(c) and float(c) > 0 else 0.0
        for s, c in zip(df["symbol"], df["close"], strict=True)
    }


def _inst_window_delta(
    dates: list[str], category: str
) -> tuple[pd.DataFrame, set[str], set[str]]:
    """窗口两端（dates[0] vs dates[-1]）按股票聚合的增减持差分。

    返回 (delta_df[index=stock_code, cur_qty, hold_pct, delta_qty, delta_pct],
    base_seen, cur_seen)。category="all" 聚合除 hkscc 外的全部机构席位。
    """
    df = _load_ccass_window(dates)
    if df.empty:
        return pd.DataFrame(), set(), set()
    df = _apply_categories(df)
    if category != "all":
        df = df[df["category"] == category]
    else:
        df = df[df["category"] != CATEGORY_HKSCC]
    cur_dt, base_dt = dates[-1], dates[0]
    cur = df[df["dt"] == cur_dt]
    base = df[df["dt"] == base_dt]
    cur_seen = set(cur["stock_code"].astype(str))
    base_seen = set(base["stock_code"].astype(str))
    piv_q = df.pivot_table(
        index="stock_code", columns="dt", values="holding_quantity",
        aggfunc="sum", fill_value=0,
    )
    piv_p = df.pivot_table(
        index="stock_code", columns="dt", values="holding_percentage",
        aggfunc="sum", fill_value=0,
    )
    if cur_dt not in piv_q.columns or base_dt not in piv_q.columns:
        return pd.DataFrame(), base_seen, cur_seen
    delta = pd.DataFrame(
        {
            "cur_qty": piv_q[cur_dt].astype("int64"),
            "hold_pct": (piv_p[cur_dt] * 100).round(2),
            "delta_qty": (piv_q[cur_dt] - piv_q[base_dt]).astype("int64"),
            "delta_pct": ((piv_p[cur_dt] - piv_p[base_dt]) * 100).round(2),
        }
    ).reset_index()
    return delta, base_seen, cur_seen


def get_institutional_overview() -> dict[str, Any]:
    """市场机构持仓结构：全市场按资金属性分类的持仓市值 / 占比 / 5 日增减持家数。"""

    def _load():
        empty = {
            "trade_date": "", "south_date": "", "stock_count": 0,
            "disclosed_value_yi": 0.0, "categories": [],
            "change_stats": {"window": 5, "increased": 0, "decreased": 0},
            "hkscc_nominees": {"value_yi": 0.0, "noted": False},
        }
        dates = _ccass_top_dates(6)  # 最新 + 5 日窗口
        if len(dates) < 2:
            return empty
        df = _load_ccass_window(dates)
        if df.empty:
            return empty
        df = _apply_categories(df)
        prices = _inst_prices()
        df["close"] = df["stock_code"].map(lambda s: prices.get(str(s), 0.0))
        df["value"] = df["holding_quantity"] * df["close"]

        cur_dt, base_dt = dates[-1], dates[0]
        cur = df[df["dt"] == cur_dt]
        base = df[df["dt"] == base_dt]
        total_value = float(cur["value"].sum())
        hkscc = cur[cur["category"] == CATEGORY_HKSCC]

        rows = []
        for cat in CATEGORY_ORDER:
            c = cur[cur["category"] == cat]
            b = base[base["category"] == cat]
            qty = int(c["holding_quantity"].sum())
            val = float(c["value"].sum())
            base_qty = int(b["holding_quantity"].sum()) if len(b) else 0
            base_val = float(b["value"].sum()) if len(b) else 0.0
            kinds = c["kind"].dropna()
            dom_kind = kinds.mode().iloc[0] if len(kinds) else "broker"
            rows.append(
                {
                    "category": cat,
                    "label": category_label(cat),
                    "kind": str(dom_kind),
                    "holding_qty": qty,
                    "value_yi": round(val / 1e8, 2),
                    "pct_of_disclosed": round(val / total_value * 100, 2)
                    if total_value > 0 else 0.0,
                    "d1_qty": int(qty - base_qty),
                    "d1_yi": round((val - base_val) / 1e8, 2),
                }
            )

        deltas = (cur.groupby("stock_code")["holding_quantity"].sum()
                  - base.groupby("stock_code")["holding_quantity"].sum())
        south_dates = list_partition_dates(SOUTH_REL, DATA_DIR)
        return {
            "trade_date": to_iso(cur_dt),
            "south_date": to_iso(south_dates[-1]) if south_dates else "",
            "stock_count": int(cur["stock_code"].nunique()),
            "disclosed_value_yi": round(total_value / 1e8, 2),
            "categories": rows,
            "change_stats": {
                "window": 5,
                "increased": int((deltas > 0).sum()),
                "decreased": int((deltas < 0).sum()),
            },
            "hkscc_nominees": {
                "value_yi": round(float(hkscc["value"].sum()) / 1e8, 2),
                "noted": bool(len(hkscc)),
            },
        }

    return cached("hk_inst_overview", _load)


def get_institutional_movers(
    category: str = "all", window: int = 5,
    direction: str = "increase", limit: int = 20,
) -> dict[str, Any]:
    """机构增减持榜：窗口间某资金属性分类净增/净减的股票排行。"""

    def _load():
        empty = {
            "trade_date": "", "base_date": "", "window": window,
            "category": category, "direction": direction, "items": [],
        }
        dates = _ccass_top_dates(window + 1)
        if len(dates) < 2:
            return empty
        delta_df, base_seen, cur_seen = _inst_window_delta(dates, category)
        if delta_df.empty:
            return empty
        names = _name_map()
        prices = _inst_prices()
        items = []
        for r in delta_df.itertuples(index=False):
            dq = int(r.delta_qty)
            if direction == "increase" and dq <= 0:
                continue
            if direction == "decrease" and dq >= 0:
                continue
            sym = str(r.stock_code)
            px = prices.get(sym, 0.0)
            items.append(
                {
                    "symbol": sym,
                    "name": names.get(sym, sym),
                    "price": round(px, 2) if px > 0 else None,
                    "hold_yi": round(r.cur_qty * px / 1e8, 2),
                    "hold_pct": float(r.hold_pct),
                    "delta_qty": dq,
                    "delta_yi": round(dq * px / 1e8, 2),
                    "delta_pct_abs": float(r.delta_pct),
                    "first_seen": sym in (cur_seen - base_seen),
                }
            )
        items.sort(key=lambda x: abs(x["delta_yi"]), reverse=True)
        return {
            "trade_date": to_iso(dates[-1]),
            "base_date": to_iso(dates[0]),
            "window": window,
            "category": category,
            "direction": direction,
            "items": items[:limit],
        }

    return cached(f"hk_inst_movers_{category}_{window}_{direction}_{limit}", _load)


def get_institutional_stock(symbol: str) -> dict[str, Any]:
    """个股机构持仓：分类结构 + 参与者明细 + 南向口径 + 分类持仓趋势。"""

    def _load():
        sym = symbol.upper()
        if not sym.endswith(".HK"):
            sym = sym + ".HK"
        empty = {
            "symbol": sym, "name": sym, "trade_date": "", "south_pct": None,
            "price": None, "disclosed_pct": 0.0, "categories": [],
            "participants": [], "trend": {"dates": [], "series": []},
        }
        names = _name_map()
        empty["name"] = names.get(sym, sym)
        dates = _ccass_top_dates(INST_TREND_DAYS)
        if not dates:
            return empty
        dt_in = partition_dates_to_sql(dates)
        df = _q(
            "SELECT stock_code, participant_id, participant_name, holding_quantity, "
            "holding_percentage, dt FROM read_parquet("
            f"'{DATA_DIR / CCASS_TOP50_REL}/dt=*/data.parquet', hive_partitioning=1) "
            f"WHERE dt IN ({dt_in}) AND stock_code = '{sym}'"
        )
        if df.empty:
            return empty
        df["dt"] = df["dt"].astype(str)
        df["holding_quantity"] = df["holding_quantity"].fillna(0).astype("int64")
        df = _apply_categories(df)
        prices = _inst_prices()
        px = prices.get(sym, 0.0)
        cur_dt = dates[-1]
        cur = df[df["dt"] == cur_dt]
        total_pct = float(cur["holding_percentage"].fillna(0).sum()) * 100

        def _base_pct(cat: str, base_dt: str) -> float:
            sub = df[(df["dt"] == base_dt) & (df["category"] == cat)]
            return float(sub["holding_percentage"].fillna(0).sum()) * 100

        cat_rows = []
        cat_agg = (
            cur.groupby("category")["holding_quantity"].sum()
        )
        for cat in CATEGORY_ORDER:
            if cat not in cat_agg.index or cat_agg[cat] == 0:
                continue
            c = cur[cur["category"] == cat]
            qty = int(cat_agg[cat])
            pct = float(c["holding_percentage"].fillna(0).sum()) * 100
            kinds = c["kind"].dropna()
            dom_kind = str(kinds.mode().iloc[0]) if len(kinds) else "broker"
            deltas = []
            for w in INST_WINDOWS:
                if len(dates) <= w:
                    continue
                base_dt = dates[-1 - w]
                bq = int(
                    df[(df["dt"] == base_dt) & (df["category"] == cat)][
                        "holding_quantity"
                    ].sum()
                )
                deltas.append(
                    {
                        "window": w,
                        "delta_qty": int(qty - bq),
                        "delta_yi": round((qty - bq) * px / 1e8, 2),
                        "delta_pct_abs": round(pct - _base_pct(cat, base_dt), 2),
                    }
                )
            cat_rows.append(
                {
                    "category": cat,
                    "label": category_label(cat),
                    "kind": dom_kind,
                    "holding_qty": qty,
                    "value_yi": round(qty * px / 1e8, 2),
                    "pct_of_total": round(pct, 2),
                    "deltas": deltas,
                }
            )

        # 参与者明细（前 50 席位，含 5/20/60 日 Δ）
        def _pid_key(pid: Any, nm: Any) -> tuple[Any, str]:
            p = None if pd.isna(pid) else str(pid)
            n = "" if pd.isna(nm) else str(nm)
            return (p, n)

        base_keys: dict[tuple[Any, str], dict[str, int]] = {}
        for w in INST_WINDOWS:
            if len(dates) <= w:
                continue
            base_dt = dates[-1 - w]
            sub = df[df["dt"] == base_dt]
            base_keys[w] = {
                _pid_key(r.participant_id, r.participant_name): int(
                    r.holding_quantity
                )
                for r in sub.itertuples(index=False)
            }
        participants = []
        for r in cur.sort_values("holding_quantity", ascending=False).itertuples(
            index=False
        ):
            pid = None if pd.isna(r.participant_id) else str(r.participant_id)
            nm = "" if pd.isna(r.participant_name) else str(r.participant_name)
            part = {
                "participant_id": pid,
                "participant_name": str(r.participant_name),
                "category": str(r.category),
                "kind": str(r.kind),
                "holding_quantity": int(r.holding_quantity),
                "holding_pct": round(float(r.holding_percentage or 0.0) * 100, 3),
            }
            for w in INST_WINDOWS:
                base = base_keys.get(w, {})
                part[f"delta_{w}d_qty"] = int(
                    part["holding_quantity"] - base.get(_pid_key(pid, nm), 0)
                )
            participants.append(part)

        # 分类持仓趋势（按分区日历对齐，缺日为 0）
        cats_in_trend = [c for c in CATEGORY_ORDER if c in df["category"].unique()]
        cats_in_trend += [c for c in df["category"].unique() if c not in CATEGORY_ORDER]
        series = []
        for cat in cats_in_trend:
            sub = (
                df[df["category"] == cat]
                .groupby("dt")["holding_quantity"]
                .sum()
                .reindex(dates)
                .fillna(0)
                .astype("int64")
            )
            series.append(
                {
                    "category": cat,
                    "label": category_label(cat),
                    "values": sub.tolist(),
                }
            )
        south_pct = next(
            (r["pct_of_total"] for r in cat_rows if r["category"] == CATEGORY_SOUTHBOUND),
            None,
        )
        return {
            "symbol": sym,
            "name": names.get(sym, sym),
            "trade_date": to_iso(cur_dt),
            "south_pct": south_pct,
            "price": round(px, 2) if px > 0 else None,
            "disclosed_pct": round(total_pct, 2),
            "categories": cat_rows,
            "participants": participants,
            "trend": {
                "dates": [to_iso(d) for d in dates],
                "series": series,
            },
        }

    return cached(f"hk_inst_stock_{symbol.upper()}", _load)


def _inst_name_pool() -> dict[str, str]:
    """symbol → 搜索名池：master 简体中文名 + CCASS 繁体 stock_name + 英文名（合并）。"""

    def _load() -> dict[str, str]:
        pool: dict[str, list[str]] = {}
        try:
            master = _q(
                "SELECT symbol, cn_name, en_name FROM read_parquet("
                f"'{DATA_DIR / RM_GLOB}')"
            )
            for r in master.itertuples(index=False):
                sym = str(r.symbol)
                names = []
                if pd.notna(r.cn_name):
                    names.append(str(r.cn_name))
                if pd.notna(r.en_name):
                    names.append(str(r.en_name))
                pool[sym] = names
        except Exception as exc:  # noqa: BLE001 - 主表缺失时降级仅用 CCASS 名
            logger.warning("[quanthk_feed] security_master 读取失败: %s", exc)
        d = _latest_ccass_top_date()
        if d:
            try:
                cc = _q(
                    "SELECT DISTINCT stock_code, stock_name FROM read_parquet("
                    f"'{DATA_DIR / CCASS_TOP50_REL}/dt=*/data.parquet', "
                    "hive_partitioning=1) WHERE dt = " + d
                )
                for r in cc.itertuples(index=False):
                    sym = str(r.stock_code)
                    if pd.notna(r.stock_name):
                        pool.setdefault(sym, [])
                        if str(r.stock_name) not in pool[sym]:
                            pool[sym].append(str(r.stock_name))
            except Exception:  # noqa: BLE001 - 昵称池降级
                pass
        return {sym: " ".join(names).lower() for sym, names in pool.items()}

    return cached("hk_inst_name_pool", _load, ttl=6 * 3600)


def get_institutional_stock_suggest(keyword: str, limit: int = 10) -> list[dict[str, Any]]:
    """证券名模糊搜索（symbol 前缀 / 中文名包含·简繁双字形 / 英文名前缀）。"""
    kw = (keyword or "").strip().lower()
    if not kw:
        return []

    def _load() -> list[dict[str, Any]]:
        pool = _inst_name_pool()
        kw_variants = _text_variants(kw)
        rows = []
        for sym, haystack in pool.items():
            if (
                sym.startswith(kw)
                or sym.lstrip("0").startswith(kw)
                or any(kv in hv for kv in kw_variants for hv in _text_variants(haystack))
            ):
                cn = haystack.split(" ")[0]
                rows.append({"symbol": sym, "name": cn or sym})
        rows.sort(
            key=lambda x: (
                not x["symbol"].lower().startswith(kw),
                not x["name"].startswith(keyword.strip()),
                x["symbol"],
            )
        )
        return rows[:limit]

    return cached(f"hk_inst_suggest_{kw}_{limit}", _load)


def get_institutional_participants(
    category: str = "all", q: str = "", limit: int = 50,
) -> dict[str, Any]:
    """参与者分类审计：全量席位/名称 → 分类/性质/最新日持仓市值，支持过滤。"""

    def _load():
        empty = {"trade_date": "", "total": 0, "items": []}
        d = _latest_ccass_top_date()
        if not d:
            return empty
        df = _load_ccass_window([d])
        if df.empty:
            return empty
        df = _apply_categories(df)
        prices = _inst_prices()
        df["close"] = df["stock_code"].map(lambda s: prices.get(str(s), 0.0))
        df["value"] = df["holding_quantity"] * df["close"]
        if category != "all":
            df = df[df["category"] == category]
        kw = q.strip().lower()
        kw_variants = _text_variants(kw)
        grouped = df.groupby(["participant_id", "participant_name"], dropna=False)
        items = []
        for (pid, nm), g in grouped:
            pid_s = "" if pd.isna(pid) else str(pid)
            nm_s = "" if pd.isna(nm) else str(nm)
            if kw:
                nm_variants = _text_variants(nm_s.lower())
                name_hit = any(
                    kv in hv for kv in kw_variants for hv in nm_variants
                )
                if kw not in pid_s.lower() and not name_hit:
                    continue
            cat = str(g["category"].iloc[0])
            kind = str(g["kind"].iloc[0])
            items.append(
                {
                    "participant_id": pid_s,
                    "participant_name": nm_s,
                    "category": cat,
                    "kind": kind,
                    "hold_yi": round(float(g["value"].sum()) / 1e8, 2),
                    "stocks": int(g["stock_code"].nunique()),
                }
            )
        items.sort(key=lambda x: x["hold_yi"], reverse=True)
        return {
            "trade_date": to_iso(d),
            "total": len(items),
            "items": items[:limit],
        }

    return cached(f"hk_inst_participants_{category}_{q.lower()}_{limit}", _load)
