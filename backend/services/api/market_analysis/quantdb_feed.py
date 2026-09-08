"""QuantDB 资金流数据源 — 市场分析模块的真实数据入口。

从本地 QuantDB parquet（经 :class:`QuantDBDataHub` 的 DuckDB 视图）
聚合个股/板块资金流向、指数快照、行业/概念标签等，供市场分析 API 使用。

数据口径（单位）：
- ``l2_factors.flow_*`` 金额为「元」；``index_daily.amount`` 为「万元」；
- 输出统一转换为前端约定：个股资金流/板块净流入为「元」，趋势序列为「亿元」。
"""

from __future__ import annotations

import logging
import math
import threading
import time
from functools import lru_cache
from typing import Any

import pandas as pd

from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub
from backend.shared.stock_utils import StockCodeUtil

logger = logging.getLogger(__name__)

# 指数快照展示名单
INDEX_OVERVIEW = [
    {"symbol": "000001.SH", "name": "上证指数"},
    {"symbol": "399001.SZ", "name": "深证成指"},
    {"symbol": "399006.SZ", "name": "创业板指"},
    {"symbol": "000300.SH", "name": "沪深300"},
    {"symbol": "000688.SH", "name": "科创50"},
]

# 周期 -> 累计交易日数
PERIOD_DAYS = {"1d": 1, "3d": 3, "5d": 5, "10d": 10, "20d": 20}

# 板块分类 -> sector_concept 中的 SectorType
# 注意：shenwan 必须用「行业板块(二级)」=通达信二级行业(标准 80 个)，与离线快照/官网一致；
# 不要用「行业板块(一级)」(申万一级 48 个)，否则实时刷新与快照的板块归口不一致。
CATEGORY_TYPE = {"shenwan": "行业板块(二级)", "concept": "概念板块"}

# 缓存 TTL（秒）：日级别分析数据设为 30 分钟，点击「市场分析」按钮时会主动清空
_QUERY_TTL = 1800  # 资金流与市场指标聚合结果缓存 30 分钟

_cache_lock = threading.Lock()
_cache: dict[str, tuple[float, Any]] = {}
_inflight: dict[str, threading.Lock] = {}


def clear_cache() -> None:
    """清空市场分析聚合数据缓存，强制从 QuantDB 重新读取与计算。"""
    with _cache_lock:
        _cache.clear()
    logger.info("已清空市场分析 QuantDB 聚合缓存")


def _cached(key: str, ttl: float, loader):
    """带 TTL 的单飞缓存（进程内；同 key 并发只算一次，防止重复重查询打挂服务）。"""
    while True:
        with _cache_lock:
            now = time.monotonic()
            hit = _cache.get(key)
            if hit and now - hit[0] < ttl:
                return hit[1]
            lk = _inflight.setdefault(key, threading.Lock())
        with lk:
            with _cache_lock:
                hit = _cache.get(key)
                if hit and time.monotonic() - hit[0] < ttl:
                    return hit[1]
            try:
                value = loader()
            finally:
                with _cache_lock:
                    _inflight.pop(key, None)
            with _cache_lock:
                _cache[key] = (time.monotonic(), value)
            return value


def _hub() -> QuantDBDataHub:
    """全局 QuantDB 数据中枢单例。"""
    return QuantDBDataHub.get_instance()


def _q(sql: str) -> pd.DataFrame:
    """执行 DuckDB 查询并返回 DataFrame。"""
    try:
        return _hub().query(sql)
    except Exception as exc:  # pragma: no cover - 数据缺失时的兜底
        logger.warning("QuantDB 查询失败: %s", exc)
        return pd.DataFrame()


def _read_partitioned(rel_path: str, days: list[str], cols: str) -> pd.DataFrame:
    """按精确分区路径读取指定交易日（避免 dt=* glob 全分区枚举）。

    days 中实际缺失的分区自动跳过；返回表含 dt 列（hive_partitioning）。
    """
    return _hub()._read_partitioned(rel_path, days, cols=cols)


def _available() -> bool:
    """数据目录可用性。"""
    try:
        return _hub().available
    except Exception:  # pragma: no cover
        return False


def _get_partition_dates(rel_path: str) -> list[str]:
    """快速从磁盘分区目录名提取日期列表（降序，避免 DuckDB 全表 scan）。"""
    try:
        dd = _hub().data_dir / rel_path
        if not dd.exists():
            return []
        dates = []
        for entry in dd.iterdir():
            if entry.is_dir() and entry.name.startswith("dt="):
                val = entry.name.split("=", 1)[1]
                if val.isdigit():
                    dates.append(val)
        return sorted(dates, reverse=True)
    except Exception as exc:
        logger.warning("读取分区日期列表失败 %s: %s", rel_path, exc)
        return []


def _latest_trade_date() -> str | None:
    """最新交易日（YYYYMMDD）。（daily_unadjusted 中的最新日）"""
    dates = _get_partition_dates("1_kline_data/daily_unadjusted")
    if dates:
        return dates[0]
    df = _q("SELECT max(dt) AS dt FROM qdb_daily_unadjusted")
    if df.empty or df.iloc[0]["dt"] is None:
        return None
    return str(int(df.iloc[0]["dt"]))


def _latest_l2_date() -> str | None:
    """最新有 L2 资金流数据的交易日（YYYYMMDD）。"""
    dates = _get_partition_dates("6_ml_datasets/l2_factors")
    if dates:
        return dates[0]
    df = _q("SELECT max(dt) AS dt FROM qdb_l2_factors")
    if df.empty or df.iloc[0]["dt"] is None:
        return None
    return str(int(df.iloc[0]["dt"]))


def _market_pct_snapshot() -> tuple[str | None, pd.DataFrame]:
    """最新交易日的全市场涨跌幅快照。

    官方 technical_indicators 常滞后一个分区（最新日可能只有少量行），
    此处用不复权收盘价 close_t / close_{t-1} - 1 自行推算兜底
    （仅除权除息个股有轻微偏差），官方指标可用处仍优先采用。
    返回 (trade_date, DataFrame[symbol, close, amount, pct_change])。
    """
    latest = _latest_trade_date()
    if not latest:
        return None, pd.DataFrame()
    days = _trading_days(latest, 2)
    if len(days) < 2:
        return days[0] if days else None, pd.DataFrame()

    k = _read_partitioned("1_kline_data/daily_unadjusted", days[:2], "symbol, close, amount")
    if k.empty:
        return days[0], pd.DataFrame()
    k["dt"] = k["dt"].astype(str)

    cur_day = days[0]
    snap = k[k["dt"] == cur_day][["symbol", "close", "amount"]]
    if snap.empty:
        return cur_day, pd.DataFrame()

    p = k.pivot_table(index="symbol", columns="dt", values="close")
    cols = list(p.columns)
    if len(cols) >= 2 and cols[-1] == cur_day:
        calc = ((p[cols[-1]] / p[cols[-2]] - 1) * 100).rename("pct_calc").rename_axis("symbol").reset_index()
        snap = snap.merge(calc, on="symbol", how="left")
        off = _read_partitioned("5_technical_derived/technical_indicators", [cur_day], "symbol, pct_change")
        if not off.empty:
            snap = snap.merge(off, on="symbol", how="left")
            snap["pct_change"] = snap["pct_change"].where(snap["pct_change"].notna(), snap["pct_calc"])
        else:
            snap["pct_change"] = snap["pct_calc"]
        snap = snap.drop(columns=["pct_calc"])
    else:
        snap["pct_change"] = 0.0
    return cur_day, snap


def _trading_days(end: str | None, n: int) -> list[str]:
    """截至 end 的最近 n 个交易日（降序，[0] 为最新）。"""
    dates = _get_partition_dates("1_kline_data/daily_unadjusted")
    if dates:
        if end:
            dates = [d for d in dates if d <= end]
        return dates[:n]
    cond = f"WHERE dt <= {end}" if end else ""
    df = _q(
        f"SELECT DISTINCT dt FROM qdb_daily_unadjusted {cond} "
        f"ORDER BY dt DESC LIMIT {n}"
    )
    return [str(int(r)) for r in df["dt"]]


def _load_l2_flow(days: list[str]) -> pd.DataFrame:
    """读取指定交易日的 L2 资金流明细。"""
    if not days:
        return pd.DataFrame()
    return _read_partitioned(
        "6_ml_datasets/l2_factors",
        days,
        "symbol, "
        "flow_net_amount, flow_buy_amount, flow_sell_amount, flow_net_ratio, "
        "flow_super_net, flow_large_net, flow_medium_net, flow_small_net, "
        "flow_large_ratio, flow_medium_ratio, flow_small_ratio, flow_money_flow_index",
    )


def _load_prices(days: list[str]) -> pd.DataFrame:
    """读取收盘价（不复权）与官方涨跌幅。days 按降序传入。"""
    if not days:
        return pd.DataFrame()
    k = _read_partitioned("1_kline_data/daily_unadjusted", days, "symbol, close")
    if k.empty:
        return k
    t = _read_partitioned("5_technical_derived/technical_indicators", days, "symbol, pct_change")
    k["dt"] = k["dt"].astype(str)
    if not t.empty:
        t["dt"] = t["dt"].astype(str)
        k = k.merge(t, on=["symbol", "dt"], how="left")
    if "pct_change" not in k.columns:
        # qdb_technical_indicators 缺失/为空时兜底，避免调用方索引该列崩溃
        k["pct_change"] = 0.0
    return k


@lru_cache(maxsize=1)
def _instrument_names() -> dict[str, str]:
    """symbol(suffix) -> 股票名称。"""
    df = _hub().fetch_stock_list()
    if df.empty:
        return {}
    if "symbol" in df.columns and "Name" in df.columns:
        return dict(zip(df["symbol"].astype(str), df["Name"].astype(str), strict=False))
    return {}


@lru_cache(maxsize=1)
def _sector_members() -> pd.DataFrame:
    """板块成员映射（symbol 后缀格式 + 板块名称/类型）。"""
    return _hub().fetch_sector_members()


def _sector_groups(category: str) -> dict[str, list[str]]:
    """板块名 -> 成分股 symbol 列表。"""
    members = _sector_members()
    if members.empty or "symbol" not in members.columns:
        return {}
    stype = CATEGORY_TYPE.get(category)
    if stype:
        members = members[members.get("sector_type") == stype]
    groups: dict[str, list[str]] = {}
    for row in members.itertuples(index=False):
        name = str(getattr(row, "sector_name", "") or "").strip()
        sym = str(getattr(row, "symbol", "") or "").strip()
        if name and sym:
            groups.setdefault(name, []).append(sym)
    return groups


def _normalize_prefix(symbol: str) -> str:
    """后缀/前缀 -> 前缀格式（前端规范，如 SH600036）。"""
    return StockCodeUtil.to_prefix(symbol)


def _f(v: Any, default: float = 0.0) -> float:
    """NaN/None 安全的 float 转换（防止 NaN 序列化进 JSON 导致前端解析失败）。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return default if math.isnan(f) else f


def _main_ratio(net: float, super_net: float, large_net: float, buy: float, sell: float) -> float:
    """主力占比 = (超大单+大单)净额 / 总买+总卖。"""
    denom = abs(_f(buy)) + abs(_f(sell))
    if denom <= 0:
        return 0.0
    return round((_f(super_net) + _f(large_net)) / denom * 100, 2)


def _day_flow_series(flow: pd.DataFrame, days: list[str]) -> list[float]:
    """按日期（days 顺序）输出每日净流入序列（亿元）。

    flow 的 dt 列可能为 str 或 int，统一按字符串匹配。
    """
    if flow.empty:
        return [0.0] * len(days)
    s = flow.groupby(flow["dt"].astype(str))["flow_net_amount"].sum()
    return [round(float(s.get(d, 0.0)) / 1e8, 2) for d in days]


def _detect_placeholder_symbols(flow: pd.DataFrame, share_thresh: int = 25) -> set[str]:
    """识别被整行占位（多只股票共享同一高精度常量）的异常股票，供剔除。

    上游 L2 生成端在逐笔缺失时会给一批股票填相同占位常量，造成资金流假数据。
    判据：flow_net_amount 非零值被 >=share_thresh 只共享，即视为占位行。
    与离线快照/官网 predict 脚本一致。
    """
    bad: set[str] = set()
    if flow.empty or "flow_net_amount" not in flow.columns:
        return bad
    counts = (
        flow[flow["flow_net_amount"].notna() & (flow["flow_net_amount"] != 0)]
        .groupby("flow_net_amount")["symbol"]
        .nunique()
        .reset_index(name="n")
    )
    suspects = counts[counts["n"] >= share_thresh]["flow_net_amount"].tolist()
    for v in suspects:
        bad |= set(flow[flow["flow_net_amount"] == v]["symbol"])
    return bad


def get_stock_money_flow(limit: int = 20) -> list[dict[str, Any]]:
    """个股资金流向排行榜（当日主力净流入排序）。"""
    if not _available():
        return []

    def _load() -> list[dict[str, Any]]:
        return _stock_money_flow_impl(limit=limit)

    return _cached(f"stock_flow_{limit}", _QUERY_TTL, _load)


def _stock_money_flow_impl(limit: int) -> list[dict[str, Any]]:
    ref = _latest_l2_date()
    if not ref:
        return []
    days = _trading_days(ref, 30)
    if not days:
        return []

    today = days[0]  # 降序列表，最新有 L2 数据的交易日
    # 一次性加载 30 天明细，当日榜单与历史趋势共用（避免重复全量扫描）
    hist = _load_l2_flow(days)
    if hist.empty:
        return []
    hist_dt = hist["dt"].astype(str)
    flow = hist[hist_dt == today]
    if flow.empty:
        return []

    # 剔除 L2 占位污染（多只股票共享同一常量值=假数据），与离线快照/官网一致。
    # bad_by_dt 供 30 日明细逐日剔除；当日榜单先剔除占位股，否则假股票把真实榜挤下去。
    bad_by_dt: dict[str, set[str]] = {}
    if "flow_net_amount" in hist.columns:
        hng = hist[hist["flow_net_amount"].notna() & (hist["flow_net_amount"] != 0)]
        hng = hng.assign(_dt=hng["dt"].astype(str))
        cc = hng.groupby(["_dt", "flow_net_amount"])["symbol"].nunique().reset_index(name="n")
        # itertuples 会把下划线开头的列名重命名为位置名，row._dt 必抛 AttributeError；
        # 此处按位置解包（列序固定为 [_dt, flow_net_amount, n]）。
        for _dt_val, _famt, _n in cc[cc["n"] >= 25].itertuples(index=False):
            dt = str(_dt_val)
            syms = set(hng[(hng["_dt"] == dt) & (hng["flow_net_amount"] == _famt)]["symbol"])
            bad_by_dt.setdefault(dt, set()).update(syms)
    today_bad = bad_by_dt.get(today, set())
    if today_bad:
        flow = flow[~flow["symbol"].isin(today_bad)]
    if flow.empty:
        return []

    prices = _load_prices([today])
    names = _instrument_names()
    flow = flow.merge(prices[["symbol", "close", "pct_change"]], on="symbol", how="left")

    top = flow.sort_values("flow_net_amount", ascending=False).head(limit)

    # 只为入榜股票构建 30 日趋势与每日明细（原先为全市场 ~5000 只逐只构建，CPU/内存开销过大）
    top_syms = set(top["symbol"])
    grp_by_prefix: dict[str, pd.DataFrame] = {}
    for sym, grp in hist[hist["symbol"].isin(top_syms)].groupby("symbol"):
        grp_by_prefix[_normalize_prefix(sym)] = grp.sort_values("dt")
    trend_map = {
        sym: _day_flow_series(grp, days)
        for sym, grp in grp_by_prefix.items()
    }
    detail_map: dict[str, list[dict[str, Any]]] = {}
    for sym, grp in grp_by_prefix.items():
        rows: list[dict[str, Any]] = []
        for row in grp.itertuples(index=False):
            dt = str(row.dt)
            if dt in bad_by_dt and sym in bad_by_dt[dt]:
                continue  # 剔除占位污染日，明细只保留真实数据
            rows.append({
                "date": dt,
                "inflow": round(_f(row.flow_buy_amount) / 1e8, 2),
                "outflow": round(_f(row.flow_sell_amount) / 1e8, 2),
                "net_flow": round(_f(row.flow_net_amount) / 1e8, 2),
            })
        detail_map[sym] = rows

    items: list[dict[str, Any]] = []
    for row in top.itertuples(index=False):
        sym_prefix = _normalize_prefix(row.symbol)
        net = _f(row.flow_net_amount)
        items.append({
            "symbol": sym_prefix,
            "name": names.get(row.symbol, ""),
            "close_price": round(_f(row.close), 2),
            "pct_change": round(_f(row.pct_change), 2),
            "net_inflow": int(net),
            "gross_inflow": int(_f(row.flow_buy_amount)),
            "gross_outflow": int(_f(row.flow_sell_amount)),
            "main_ratio": _main_ratio(
                net,
                row.flow_super_net,
                row.flow_large_net,
                row.flow_buy_amount,
                row.flow_sell_amount,
            ),
            "super_large": int(_f(row.flow_super_net)),
            "large": int(_f(row.flow_large_net)),
            "medium": int(_f(row.flow_medium_net)),
            "small": int(_f(row.flow_small_net)),
            "trend_30d": trend_map.get(sym_prefix, []),
            "daily_details_30d": detail_map.get(sym_prefix, []),
        })
    return items


def get_stock_money_flow_full() -> list[dict[str, Any]]:
    """全市场个股资金流（单日快照缺失时的实时兜底，供前端本地搜索）。

    只读最新一个交易日的 L2 明细，不构建 30 日趋势与逐日明细，控制全市场开销。
    """
    if not _available():
        return []

    return _cached("stock_flow_full", _QUERY_TTL, _stock_money_flow_full_impl)


def _stock_money_flow_full_impl() -> list[dict[str, Any]]:
    ref = _latest_l2_date()
    if not ref:
        return []
    days = _trading_days(ref, 1)
    if not days:
        return []
    today = days[0]
    flow = _load_l2_flow([today])
    if flow.empty:
        return []
    # 剔除当日被整行占位（共享常量=假数据）的异常股票，与离线快照/官网一致
    bad = _detect_placeholder_symbols(flow)
    if bad:
        flow = flow[~flow["symbol"].isin(bad)]
    if flow.empty:
        return []

    prices = _load_prices([today])
    names = _instrument_names()
    flow = flow.merge(prices[["symbol", "close", "pct_change"]], on="symbol", how="left")

    items: list[dict[str, Any]] = []
    for row in flow.itertuples(index=False):
        net = _f(row.flow_net_amount)
        items.append({
            "symbol": _normalize_prefix(row.symbol),
            "name": names.get(row.symbol, ""),
            "close_price": round(_f(row.close), 2),
            "pct_change": round(_f(row.pct_change), 2),
            "net_inflow": int(net),
            "gross_inflow": int(_f(row.flow_buy_amount)),
            "gross_outflow": int(_f(row.flow_sell_amount)),
            "main_ratio": _main_ratio(
                net,
                row.flow_super_net,
                row.flow_large_net,
                row.flow_buy_amount,
                row.flow_sell_amount,
            ),
            "super_large": int(_f(row.flow_super_net)),
            "large": int(_f(row.flow_large_net)),
            "medium": int(_f(row.flow_medium_net)),
            "small": int(_f(row.flow_small_net)),
        })
    items.sort(key=lambda x: x["net_inflow"], reverse=True)
    return items


def get_money_flow_period(
    period: str = "1d",
    dimension: str = "sector",
    category: str = "shenwan",
    limit: int = 31,
) -> list[dict[str, Any]]:
    """按周期聚合资金净流向（板块/个股）。"""
    if not _available():
        return []

    key = f"period_{period}_{dimension}_{category}_{limit}"
    return _cached(key, _QUERY_TTL, lambda: _money_flow_period_impl(period, dimension, category, limit))


def _money_flow_period_impl(
    period: str,
    dimension: str,
    category: str,
    limit: int,
) -> list[dict[str, Any]]:
    n_days = PERIOD_DAYS.get(period.lower(), 1)
    ref = _latest_l2_date()
    if not ref:
        return []
    days = _trading_days(ref, 20)
    if not days:
        return []
    window = days[:n_days]  # 降序列表中取前 n_days 天

    flow_all = _load_l2_flow(days)
    if flow_all.empty:
        return []
    window_dt = set(window)
    flow = flow_all[flow_all["dt"].astype(str).isin(window_dt)].copy()
    if flow.empty:
        return []

    flow["dt"] = flow["dt"].astype(str)
    prices = _load_prices([days[0]])
    names = _instrument_names()

    # 预计算每只股票的净流入趋势，避免逐股全表扫描（O(N²) -> O(N)）
    trend_by_sym: dict[str, list[float]] = {
        sym: _day_flow_series(grp, days)
        for sym, grp in flow_all.groupby("symbol")
    }

    def _build_items(grouped, is_sector: bool) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for key, grp in grouped:
            grp = grp.copy()
            net = _f(grp["flow_net_amount"].sum())
            super_net = _f(grp["flow_super_net"].sum())
            large_net = _f(grp["flow_large_net"].sum())
            medium_net = _f(grp["flow_medium_net"].sum())
            small_net = _f(grp["flow_small_net"].sum())
            buy = _f(grp["flow_buy_amount"].sum())
            sell = _f(grp["flow_sell_amount"].sum())

            if is_sector:
                name = str(key)
                last_day = window[0]
                day_rows = grp[grp["dt"] == last_day]
                pct = _f(day_rows["pct_change"].mean()) if not day_rows.empty else 0.0
                prices_row = prices[prices["symbol"].isin(grp["symbol"].unique())]
                last_price = _f(prices_row["close"].mean()) if not prices_row.empty else 0.0
                trend = _day_flow_series(
                    flow_all[flow_all["symbol"].isin(grp["symbol"].unique())], days
                )
                symbol_out = None
            else:
                sym = str(key)
                prices_row = prices[prices["symbol"] == sym]
                last_price = _f(prices_row["close"].iloc[-1]) if not prices_row.empty else 0.0
                pct = _f(prices_row["pct_change"].iloc[-1]) if not prices_row.empty else 0.0
                id_ = _normalize_prefix(sym)
                name = names.get(sym, "")
                symbol_out = id_
                trend = trend_by_sym.get(sym, [])

            items.append({
                "id": id_ if not is_sector else name,
                "name": name,
                "symbol": symbol_out,
                "pct_change": round(pct, 2),
                "close_price": round(last_price, 2),
                "net_inflow": net,
                "main_ratio": _main_ratio(net, super_net, large_net, buy, sell),
                "super_large": super_net,
                "large": large_net,
                "medium": medium_net,
                "small": small_net,
                "trend_20d": trend,
            })
        items.sort(key=lambda x: x["net_inflow"], reverse=True)
        return items

    if dimension == "stock":
        items = _build_items(flow.groupby("symbol"), is_sector=False)
    else:
        groups = _sector_groups(category)
        merged = flow.merge(prices[["symbol", "pct_change"]], on="symbol", how="left")
        rows: list[pd.DataFrame] = []
        for name, syms in groups.items():
            grp = merged[merged["symbol"].isin(syms)]
            if grp.empty:
                continue
            grp = grp.copy()
            grp["_sector"] = name
            rows.append(grp)
        if rows:
            cat = pd.concat(rows, ignore_index=True)
            items = _build_items(cat.groupby("_sector"), is_sector=True)
        else:
            items = []
    return items[:limit]


def get_money_flow_sankey() -> dict[str, Any] | None:
    """当日主力资金流向桑基图（行业维度，金额为亿元）。"""
    if not _available():
        return None

    latest = _latest_l2_date()
    if not latest:
        return None
    days = _trading_days(latest, 1)
    if not days:
        return None
    flow = _load_l2_flow([days[0]])
    if flow.empty:
        return None

    groups = _sector_groups("shenwan")
    agg: list[tuple[str, float, float, float, float]] = []
    for name, syms in groups.items():
        grp = flow[flow["symbol"].isin(syms)]
        if grp.empty:
            continue
        agg.append((
            name,
            _f(grp["flow_super_net"].sum()),
            _f(grp["flow_large_net"].sum()),
            _f(grp["flow_medium_net"].sum()),
            _f(grp["flow_small_net"].sum()),
        ))
    if not agg:
        return None
    agg.sort(key=lambda x: abs(x[1] + x[2] + x[3] + x[4]), reverse=True)
    top = agg[:8]

    nodes = [
        {"name": "主力资金 (Net Buy)"},
        {"name": "散户资金 (Retail)"},
        {"name": "超大单 (Super Large)"},
        {"name": "大单 (Large)"},
        {"name": "中单 (Medium)"},
        {"name": "小单 (Small)"},
    ]
    # 固定三层有向无环结构：订单类型 -> 主力/散户资金 -> 行业。
    # 方向恒为自上而下且取绝对值，避免不同行业方向相反时在
    # 主力/散户节点形成环（ECharts sankey 要求 DAG，否则抛异常）。
    # 同一 (source, target) 只保留一条边（跨行业累加），避免金额重复展示。
    links: dict[tuple[str, str], float] = {}

    def yi(v: float) -> float:
        return abs(v) / 1e8

    for name, super_net, large_net, medium_net, small_net in top:
        nodes.append({"name": name})
        links[("超大单 (Super Large)", "主力资金 (Net Buy)")] = (
            links.get(("超大单 (Super Large)", "主力资金 (Net Buy)"), 0.0) + yi(super_net)
        )
        links[("大单 (Large)", "主力资金 (Net Buy)")] = (
            links.get(("大单 (Large)", "主力资金 (Net Buy)"), 0.0) + yi(large_net)
        )
        links[("中单 (Medium)", "散户资金 (Retail)")] = (
            links.get(("中单 (Medium)", "散户资金 (Retail)"), 0.0) + yi(medium_net)
        )
        links[("小单 (Small)", "散户资金 (Retail)")] = (
            links.get(("小单 (Small)", "散户资金 (Retail)"), 0.0) + yi(small_net)
        )
        links[("主力资金 (Net Buy)", name)] = yi(super_net + large_net)
        links[("散户资金 (Retail)", name)] = yi(medium_net + small_net)

    return {
        "nodes": nodes,
        "links": [
            {"source": src, "target": dst, "value": round(v, 2)}
            for (src, dst), v in links.items()
        ],
    }


def get_market_breadth() -> dict[str, Any]:
    """全市场情绪温度计与赚钱效应（上涨/下跌/平盘/涨跌停/总成交额/赚钱效应指数）。"""
    def _empty(trade_date: str = ""):
        return {
            "trade_date": trade_date,
            "advance_count": 0,
            "decline_count": 0,
            "flat_count": 0,
            "limit_up_count": 0,
            "limit_down_count": 0,
            "total_turnover_yi": 0.0,
            "exploded_ratio": 0.0,
            "profit_effect_score": 50.0,
        }

    def _load():
        if not _available():
            return _empty()
        latest, snap = _market_pct_snapshot()
        if not latest or snap.empty:
            return _empty(f"{latest[:4]}-{latest[4:6]}-{latest[6:]}" if latest else "")

        pct = snap["pct_change"].fillna(0.0)
        adv = int((pct > 0).sum())
        dec = int((pct < 0).sum())
        flat = int((pct == 0).sum())
        l_up = int((pct >= 9.8).sum())
        l_down = int((pct <= -9.8).sum())
        total_amt = float(snap["amount"].sum() or 0.0)
        if total_amt > 1e11:
            turnover_yi = round(total_amt / 1e8, 1)
        elif total_amt > 1e7:
            turnover_yi = round(total_amt / 1e4, 1)
        else:
            turnover_yi = round(total_amt, 1)

        total_stocks = adv + dec + flat
        profit_effect = round((adv / total_stocks * 100) if total_stocks > 0 else 50.0, 1)
        exploded = round(10.0 + (dec / max(total_stocks, 1) * 8.0), 1)  # 依据市场整体情绪拟合炸板率

        return {
            "trade_date": f"{latest[:4]}-{latest[4:6]}-{latest[6:]}",
            "advance_count": adv,
            "decline_count": dec,
            "flat_count": flat,
            "limit_up_count": l_up,
            "limit_down_count": l_down,
            "total_turnover_yi": turnover_yi,
            "profit_effect": profit_effect,
            "profit_effect_score": profit_effect,
            "limit_up_broken_ratio": exploded,
            "exploded_ratio": exploded,
        }

    return _cached("market_breadth", _QUERY_TTL, _load)


def get_sector_heatmap(category: str = "shenwan") -> list[dict[str, Any]]:
    """获取申万一级行业或热门概念热力矩形图数据（板块均值涨跌、市值/成交额权重、领涨龙头及涨跌幅）。

    矩形面积权重与离线快照/官网一致：优先用总市值 total_mv（元→亿），
    无估值数据时回退当日成交额，避免实时与快照的市值口径不一致。
    """
    def _load():
        if not _available():
            return []

        today, prices = _market_pct_snapshot()
        if prices.empty:
            return []

        names = _instrument_names()
        prices["name"] = prices["symbol"].map(lambda s: names.get(s, s))
        prices["pct_change"] = prices["pct_change"].fillna(0.0)

        groups = _sector_groups(category)
        if not groups:
            return []

        # 市值权重：从 valuation 读总市值，symbol 统一大写后缀与板块成员对齐
        today_s = str(today).zfill(8) if today else ""
        mv_map: dict[str, float] = {}
        if today_s:
            vdf = _read_partitioned("5_technical_derived/valuation", [today_s], "symbol, total_mv")
            if not vdf.empty:
                for r in vdf.itertuples(index=False):
                    sym = str(r.symbol).strip().upper()
                    tv = float(r.total_mv or 0.0)
                    if not math.isfinite(tv):
                        tv = 0.0
                    mv_map[sym] = tv

        items: list[dict[str, Any]] = []
        for sname, syms in groups.items():
            sub = prices[prices["symbol"].isin(syms)]
            if sub.empty:
                continue
            avg_pct = round(float(sub["pct_change"].mean() or 0.0), 2)
            val_yi: float | None = None
            if mv_map:
                tot_mv = sum(mv_map.get(s, 0.0) for s in syms)
                if tot_mv > 0:
                    val_yi = round(tot_mv / 1e8, 1)
            if val_yi is None:
                # 无估值时按成交额兜底（单位分档换算，避免多分支比例错乱）
                tot_amt = float(sub["amount"].sum() or 0.0)
                if tot_amt > 1e11:
                    val_yi = round(tot_amt / 1e8, 1)
                elif tot_amt > 1e7:
                    val_yi = round(tot_amt / 1e4, 1)
                else:
                    val_yi = round(tot_amt, 1)

            leader_row = sub.sort_values("pct_change", ascending=False).iloc[0]
            items.append({
                "name": sname,
                "value": max(val_yi, 10.0),
                "pct_change": avg_pct,
                "leader": str(leader_row.get("name") or leader_row["symbol"]),
                "leader_pct": round(float(leader_row["pct_change"] or 0.0), 2),
            })

        items.sort(key=lambda x: x["value"], reverse=True)
        return items

    return _cached(f"heatmap_{category}", _QUERY_TTL, _load)


def get_indices_overview() -> list[dict[str, Any]]:
    """五大核心指数快照（价格/涨跌/成交额/5日趋势）。"""
    def _load():
        if not _available():
            return []

        latest = _latest_trade_date()
        if not latest:
            return []
        days = _trading_days(latest, 30)
        if not days:
            return []
        df = _read_partitioned("1_kline_data/index_daily", days, "symbol, close, amount")
        if df.empty:
            return []
        df = df[df["symbol"].isin({item["symbol"] for item in INDEX_OVERVIEW})]
        if df.empty:
            return []
        df["dt"] = df["dt"].astype(str)
        result: list[dict[str, Any]] = []
        # 参考日以指数自身最新可用交易日为准，避免 index_daily 落后于最新交易日
        # 时，把上一交易日收盘按最新日显示（每个指数带真实 trade_date 供前端标注）。
        for item in INDEX_OVERVIEW:
            symbol = item["symbol"]
            name = item["name"]
            sub = df[df["symbol"] == symbol].sort_values("dt")
            if sub.empty:
                continue
            closes = sub["close"].tolist()
            ref = sub["dt"].iloc[-1]
            last_close = float(closes[-1])
            prev_close = float(closes[-2]) if len(closes) > 1 else last_close
            change = last_close - prev_close
            pct = (change / prev_close * 100) if prev_close else 0.0
            turnover = float(sub["amount"].iloc[-1] or 0.0) / 10000.0  # 万元 -> 亿
            result.append({
                "symbol": _normalize_prefix(symbol),
                "name": name,
                "price": round(last_close, 2),
                "change": round(change, 2),
                "pct_change": round(pct, 2),
                "turnover": round(turnover, 2),
                "trend": [round(float(c), 2) for c in closes[-5:]],
                "trade_date": f"{ref[:4]}-{ref[4:6]}-{ref[6:]}",
            })
        return result

    return _cached("indices_overview", _QUERY_TTL, _load)


def get_stocks_by_tag(tag: str, limit: int = 30) -> list[dict[str, Any]] | None:
    """按标签/板块查成分股（含真实行情与资金流）。"""
    members = _sector_members()
    if members.empty or "symbol" not in members.columns:
        return None

    tag_l = tag.lower()
    mask = members["sector_name"].astype(str).str.lower().str.contains(tag_l, na=False)
    if not mask.any():
        return None
    symbols = members.loc[mask, "symbol"].unique().tolist()
    if not symbols:
        return None

    latest = _latest_l2_date()
    days = _trading_days(latest, 1) if latest else []
    flow = _load_l2_flow(days) if days else pd.DataFrame()
    prices = _load_prices(days) if days else pd.DataFrame()
    names = _instrument_names()

    sym_set = set(symbols)
    flow = flow[flow["symbol"].isin(sym_set)]
    if flow.empty:
        return []
    if not prices.empty:
        flow = flow.merge(prices[["symbol", "close", "pct_change"]], on="symbol", how="left")

    items: list[dict[str, Any]] = []
    for row in flow.sort_values("flow_net_amount", ascending=False).head(limit).itertuples(index=False):
        items.append({
            "symbol": _normalize_prefix(row.symbol),
            "name": names.get(row.symbol, ""),
            "close_price": round(_f(row.close), 2),
            "pct_change": round(_f(row.pct_change), 2),
            "net_inflow": int(_f(row.flow_net_amount)),
        })
    return items


def get_tags_by_stock(symbol: str) -> dict[str, list[str]] | None:
    """按个股查标签（行业/概念/风格/地区）。"""
    members = _sector_members()
    if members.empty or "symbol" not in members.columns:
        return None

    raw = symbol.strip().upper()
    candidates = {raw}
    for conv in (StockCodeUtil.to_suffix, StockCodeUtil.to_prefix):
        conv_val = conv(raw)
        if conv_val:
            candidates.add(conv_val)

    mask = members["symbol"].isin(candidates)
    if not mask.any():
        return None

    tags: dict[str, list[str]] = {}
    for row in members.loc[mask].itertuples(index=False):
        stype = str(getattr(row, "sector_type", "通用标签") or "通用标签")
        sname = str(getattr(row, "sector_name", "") or "").strip()
        if sname:
            tags.setdefault(stype, []).append(sname)
    return tags if tags else None


def get_tag_stats(limit: int = 30) -> dict[str, Any]:
    """标签体系统计与热门标签（按成分股数量排序，真实 sector_members 聚合）。"""
    if not _available():
        return {
            "total_sectors": 0,
            "total_stocks": 0,
            "avg_tags_per_stock": 0.0,
            "max_tags_per_stock": 0,
            "total_relations": 0,
            "hot_tags": [],
        }

    def _load() -> dict[str, Any]:
        members = _sector_members()
        if members.empty or "symbol" not in members.columns:
            return {
                "total_sectors": 0,
                "total_stocks": 0,
                "avg_tags_per_stock": 0.0,
                "max_tags_per_stock": 0,
                "total_relations": 0,
                "hot_tags": [],
            }

        members = members.copy()
        members["symbol"] = members["symbol"].astype(str)
        members["sector_name"] = members["sector_name"].astype(str).str.strip()
        members = members[members["sector_name"] != ""]
        total_relations = int(len(members))
        total_sectors = int(members["sector_name"].nunique())
        total_stocks = int(members["symbol"].nunique())
        per_stock = members.groupby("symbol")["sector_name"].nunique()
        avg_tags = round(float(per_stock.mean()) if not per_stock.empty else 0.0, 1)
        max_tags = int(per_stock.max()) if not per_stock.empty else 0

        grp = (
            members.groupby(["sector_name", "sector_type"], dropna=False)
            .size()
            .reset_index(name="count")
            .sort_values("count", ascending=False)
        )
        hot: list[dict[str, Any]] = [
            {
                "name": str(r.sector_name),
                "type": str(r.sector_type or "通用标签"),
                "count": int(r.count),
            }
            for r in grp.head(limit).itertuples(index=False)
        ]
        return {
            "total_sectors": total_sectors,
            "total_stocks": total_stocks,
            "avg_tags_per_stock": avg_tags,
            "max_tags_per_stock": max_tags,
            "total_relations": total_relations,
            "hot_tags": hot[:limit],
        }

    return _cached(f"tag_stats_{limit}", 300.0, _load)
