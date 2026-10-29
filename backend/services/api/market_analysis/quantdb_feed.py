"""QuantDB 资金流数据源 — 市场分析模块的真实数据入口。

从本地 QuantDB parquet（经 :class:`QuantDBDataHub` 的 DuckDB 视图）
聚合个股/板块资金流向、指数快照、行业/概念标签等，供市场分析 API 使用。

数据口径（单位）：
- ``l2_factors.flow_*`` 金额：按分区混存（万元/元），读入经
  ``normalize_l2_flow_money_to_yuan`` 归一为「元」；``index_daily.amount`` 为「万元」；
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
from backend.shared.quantdb_flow_units import normalize_l2_flow_money_to_yuan
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
PERIOD_DAYS = {"1d": 1, "2d": 2, "3d": 3, "5d": 5, "10d": 10, "20d": 20}

# 板块分类 -> sector_concept 中的 SectorType
# 注意：shenwan 必须用「行业板块(二级)」=通达信二级行业(标准 80 个)，与离线快照/官网一致；
# 不要用「行业板块(一级)」(申万一级 48 个)，否则实时刷新与快照的板块归口不一致。
# 键名 shenwan 为历史遗留（语义=通达信二级行业），前端已按此键取值，勿改动；
# industry1 为一级行业下钻新增（无离线快照，走实时聚合兜底）。
CATEGORY_TYPE = {
    "shenwan": "行业板块(二级)",
    "industry1": "行业板块(一级)",
    "concept": "概念板块",
}

# 分类展示名（面包屑/标题文案唯一出处）
CATEGORY_LABEL = {
    "shenwan": "通达信二级行业",
    "industry1": "通达信一级行业",
    "concept": "概念板块",
}

# 个股下钻单板块最多渲染的矩形数（概念板块可达 1200+ 只，整图上屏会拖垮渲染）；
# 超过则按市值降序取前 N，并在 summary.shown/total 中如实披露截断。
MAX_DRILL_ITEMS = 300

# 缓存 TTL（秒）：日级别分析数据设为 30 分钟，点击「市场分析」按钮时会主动清空
_QUERY_TTL = 1800  # 资金流与市场指标聚合结果缓存 30 分钟

# L2 单日读取的分区宽度与上探上限：最新交易日的 L2 分区常延后落盘（T+1 追平），
# 必须向前回溯到最近有数据的交易日，否则板块层「主力净额」整列恒为 0。
_L2_FLOW_WINDOW = 8
_L2_FLOW_MAX_LOOKBACK = 40

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


def _market_pct_snapshot(
    anchor: str | None = None,
) -> tuple[str | None, pd.DataFrame]:
    """指定交易日（默认最新）的全市场涨跌幅快照。

    官方 technical_indicators 常滞后一个分区（最新日可能只有少量行），
    此处用不复权收盘价 close_t / close_{t-1} - 1 自行推算兜底
    （仅除权除息个股有轻微偏差），官方指标可用处仍优先采用。
    ``anchor`` 传 YYYYMMDD 可锚定历史交易日（下钻回溯用），None=最新交易日。
    返回 (trade_date, DataFrame[symbol, close, amount, pct_change])。
    """
    latest = str(anchor).zfill(8) if anchor else _latest_trade_date()
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
    """读取指定交易日的 L2 资金流明细（金额列归一为元）。"""
    if not days:
        return pd.DataFrame()
    df = _read_partitioned(
        "6_ml_datasets/l2_factors",
        days,
        "symbol, amount, "
        "flow_net_amount, flow_buy_amount, flow_sell_amount, flow_net_ratio, "
        "flow_super_net, flow_large_net, flow_medium_net, flow_small_net, "
        "flow_large_ratio, flow_medium_ratio, flow_small_ratio, flow_money_flow_index",
    )
    return normalize_l2_flow_money_to_yuan(df)


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


def placeholder_symbols_by_dt(hist: pd.DataFrame, share_thresh: int = 25) -> dict[str, set[str]]:
    """逐日识别被整行占位（多只股票共享同一高精度常量）的 symbol。

    与 :func:`_detect_placeholder_symbols` 同判据（值被 >=share_thresh 只共享），
    但按 dt 分组——多日窗口里同一常量可能跨日巧合重复，按日判定防误伤。
    返回 {dt(YYYYMMDD): {symbol, ...}}。
    """
    bad: dict[str, set[str]] = {}
    if hist.empty or "flow_net_amount" not in hist.columns or "dt" not in hist.columns:
        return bad
    hng = hist[hist["flow_net_amount"].notna() & (hist["flow_net_amount"] != 0)]
    if hng.empty:
        return bad
    hng = hng.assign(_dt=hng["dt"].astype(str))
    cc = hng.groupby(["_dt", "flow_net_amount"])["symbol"].nunique().reset_index(name="n")
    # itertuples 会把下划线开头的列名重命名为位置名，row._dt 必抛 AttributeError；
    # 此处按位置解包（列序固定为 [_dt, flow_net_amount, n]）。
    for _dt_val, _famt, _n in cc[cc["n"] >= share_thresh].itertuples(index=False):
        dt = str(_dt_val)
        syms = set(hng[(hng["_dt"] == dt) & (hng["flow_net_amount"] == _famt)]["symbol"])
        bad.setdefault(dt, set()).update(syms)
    return bad


def get_stock_money_flow(limit: int = 50) -> list[dict[str, Any]]:
    """个股资金流向排行榜（当日主力净流入排序；默认与看板 Top 50 口径一致）。"""
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
    bad_by_dt = placeholder_symbols_by_dt(hist)
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


def _limit_counts(pct: pd.Series, symbols: pd.Series, trade_date: str) -> tuple[int, int]:
    """涨停/跌停家数 —— 口径唯一事实源见 ``shared.market_breadth``。

    旧实现是两条写死的 `pct >= 9.8` / `pct <= -9.8`：20%/30% 板上任何
    +9.8% 以上的普通阳线都被计成涨停（家数虚高），ST 5% 板上真封死的票
    又永远漏计 —— 用户看到的「涨停 N 家」既偏高又偏低。

    ``trade_date`` 是**最新**交易日（YYYYMMDD），所以用当前 ST 快照是**同期**
    口径而非历史回放，不存在前视偏差。今日（≥ 2026-07-06）ST 主板与主板同为
    10%，ST 集合对当日家数实际无影响；保留它是为了让历史日期调用时口径正确。
    """
    from datetime import date as _date

    from backend.shared.market_breadth import limit_up_down_counts

    try:
        from backend.services.simulation.services.local_market_data import (
            get_local_market_data,
        )

        st = get_local_market_data()._st_symbol_set()
    except Exception as exc:  # noqa: BLE001
        # 不静默：ST 快照缺失在 2026-07-06 之前会让 5% 板整批漏计
        logger.warning("ST 快照不可用，涨停/跌停家数按非 ST 口径统计: %s", exc)
        st = frozenset()
    td = _date(int(trade_date[:4]), int(trade_date[4:6]), int(trade_date[6:8]))
    return limit_up_down_counts(pct, symbols, trade_date=td, st_symbols=st)


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
        l_up, l_down = _limit_counts(pct, snap["symbol"], latest)
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


def get_sector_heatmap(
    category: str = "shenwan", period: str = "1d"
) -> list[dict[str, Any]]:
    """板块热力图数据（涨跌幅 + 周期主力净额/日均/加速度 + 市值权重 + 龙头）。

    周期口径：
    - ``flow_net``：当前 N 日主力净额（亿）；
    - ``flow_daily``：当前 N 日日均（亿/日）；
    - ``prev_flow_daily``：前一 N 日日均；
    - ``flow_accel``：当前日均 − 前一窗口日均（亿/日），正=加速流入；
    - ``flow_ratio``：当前 N 日主力净额 / 成分成交额；
    - ``flow_ratio_accel``：当前 N 日板块净额比日均 − 前一 N 日日均（与离线
      panel_sector.net_ratio 同口径，供方向概率状态表查表）；
    - ``pct_period``：板块等权 N 日涨幅（%）；``pct_change`` 保留当日均涨幅；
    - ``ret_period_sum``：板块逐日等权收益之和（与 panel_sector.ret 同口径）；
    - ``breadth_flow``：当前 N 日「成分主力净流入为正」的日均占比（资金广度）；
      ``breadth_flow_chg`` = 当前 − 前一窗口（情绪升温/降温）；
    - ``pct_above_ma20``：成分收盘价在 MA20 上方的占比（价格广度，情绪算法行业化）；
      ``pct_above_ma20_chg`` = 当前 − 5 个交易日前。
    """
    n_days = PERIOD_DAYS.get(str(period or "1d").lower(), 1)

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

        # 市值权重（总市值，元→亿；缺失回退成交额）
        today_s = str(today).zfill(8) if today else ""
        mv_map: dict[str, float] = {}
        if today_s:
            vdf = _read_partitioned(
                "5_technical_derived/valuation", [today_s], "symbol, total_mv"
            )
            if not vdf.empty:
                for r in vdf.itertuples(index=False):
                    sym = str(r.symbol).strip().upper()
                    tv = float(r.total_mv or 0.0)
                    if not math.isfinite(tv):
                        tv = 0.0
                    mv_map[sym] = tv

        # 周期主力净额：最近 2N 个有 L2 数据的交易日
        flow_days = _l2_days(today_s, max(n_days * 2, n_days + 1)) if today_s else []
        cur_days = set(flow_days[:n_days])
        prev_days = set(flow_days[n_days : n_days * 2])
        cur_flow = prev_flow = pd.DataFrame()
        if flow_days:
            flow_all = _load_l2_flow(flow_days)
            if not flow_all.empty:
                flow_all["dt"] = flow_all["dt"].astype(str)
                bad = placeholder_symbols_by_dt(flow_all)
                if bad:
                    keep = [
                        sym not in (bad.get(str(dt)) or set())
                        for sym, dt in zip(
                            flow_all["symbol"], flow_all["dt"], strict=True
                        )
                    ]
                    flow_all = flow_all[keep]
                flow_all["main"] = flow_all["flow_super_net"].fillna(
                    0.0
                ) + flow_all["flow_large_net"].fillna(0.0)
                flow_all["amount_y"] = flow_all["amount"].astype(float) * 1e4
                cur_flow = flow_all[flow_all["dt"].isin(cur_days)]
                prev_flow = flow_all[flow_all["dt"].isin(prev_days)]
        # 板块逐日净额比（sum(main)/sum(amount)）→ 当前/前一窗口日均值；
        # 与离线 panel_sector.net_ratio 同口径，供状态表方向概率使用。
        cur_ratio = prev_ratio = pd.Series(dtype="float64")
        cur_breadth = prev_breadth = pd.Series(dtype="float64")
        if flow_days and not flow_all.empty:
            sym2group = {s: name for name, syms in groups.items() for s in syms}
            mapped = flow_all[flow_all["symbol"].isin(sym2group)].copy()
            mapped["sector"] = mapped["symbol"].map(sym2group)
            mapped["main_pos"] = (mapped["main"] > 0).astype(float)
            daily = mapped.groupby(["sector", "dt"]).agg(
                main=("main", "sum"),
                amount_y=("amount_y", "sum"),
                breadth=("main_pos", "mean"),
            )
            daily["ratio"] = daily["main"] / daily["amount_y"].where(
                daily["amount_y"] > 0
            )
            idx_dt = daily.index.get_level_values("dt")
            cur_ratio = daily[idx_dt.isin(cur_days)].groupby("sector")["ratio"].mean()
            prev_ratio = daily[idx_dt.isin(prev_days)].groupby("sector")["ratio"].mean()
            cur_breadth = (
                daily[idx_dt.isin(cur_days)].groupby("sector")["breadth"].mean()
            )
            prev_breadth = (
                daily[idx_dt.isin(prev_days)].groupby("sector")["breadth"].mean()
            )

        cur_sums = (
            cur_flow.groupby("symbol")[["main", "amount_y"]].sum()
            if not cur_flow.empty
            else pd.DataFrame()
        )
        prev_sums = (
            prev_flow.groupby("symbol")[["main", "amount_y"]].sum()
            if not prev_flow.empty
            else pd.DataFrame()
        )

        # 周期涨幅：N 日等权（收盘价，未复权；1 日用当日官方涨跌幅）
        # ret_period_sum = 板块逐日等权收益之和（与 panel_sector.ret 滚动求和同口径，
        # 供方向概率状态表查表）；pct_period 仍保留首尾收盘口径供展示。
        period_pct: dict[str, float] = {}
        ret_period_sum: dict[str, float] = {}
        ret_period_sum_prev: dict[str, float] = {}
        price_accel: dict[str, float] = {}
        if n_days >= 1:
            # 多取一倍窗口：当前 N 日 + 前 N 日，用于价格加速度
            ret_days = _trading_days(today_s, n_days * 2 + 1) if today_s else []
            if len(ret_days) >= 2:
                closes = _read_partitioned(
                    "1_kline_data/daily_unadjusted",
                    ret_days,
                    "symbol, close",
                )
                if not closes.empty:
                    closes["dt"] = closes["dt"].astype(str)
                    piv = closes.pivot_table(
                        index="symbol", columns="dt", values="close"
                    )
                    first, last = ret_days[min(n_days, len(ret_days) - 1)], ret_days[0]
                    if first in piv.columns and last in piv.columns:
                        ret = (piv[last] / piv[first] - 1.0) * 100.0
                        period_pct = {
                            str(k): float(v)
                            for k, v in ret.items()
                            if math.isfinite(float(v))
                        }
                    # 逐日收益（ret_days 降序）：day i / day i+1 − 1；2N 日用于价格加速度
                    daily = pd.DataFrame(index=piv.index)
                    for i in range(min(n_days * 2, len(ret_days) - 1)):
                        d0, d1 = ret_days[i], ret_days[i + 1]
                        if d0 in piv.columns and d1 in piv.columns:
                            daily[d0] = piv[d0] / piv[d1] - 1.0
                    if not daily.empty:
                        cur_cols = list(daily.columns[:n_days])
                        prev_cols = list(daily.columns[n_days : n_days * 2])
                        for sname, syms in groups.items():
                            sub = daily.reindex([s for s in syms if s in daily.index])
                            if sub.empty:
                                continue
                            cur = sub.reindex(columns=cur_cols).mean(axis=0, skipna=True)
                            cur_total = cur.sum(min_count=1)
                            if pd.notna(cur_total):
                                ret_period_sum[sname] = float(cur_total) * 100.0
                            if prev_cols:
                                prev = sub.reindex(columns=prev_cols).mean(
                                    axis=0, skipna=True
                                )
                                prev_total = prev.sum(min_count=1)
                                if pd.notna(prev_total):
                                    ret_period_sum_prev[sname] = (
                                        float(prev_total) * 100.0
                                    )
                                if pd.notna(cur_total) and pd.notna(prev_total):
                                    price_accel[sname] = round(
                                        (float(cur_total) - float(prev_total))
                                        / n_days
                                        * 100.0,
                                        4,
                                    )

        # 价格广度（情绪算法的行业化）：成分收盘价在 MA20 上方的占比（当前/5日前）。
        price_breadth: dict[str, tuple[float, float | None]] = {}
        if today_s:
            breadth_days = _trading_days(today_s, 26)
            if len(breadth_days) >= 25:
                bclose = _read_partitioned(
                    "1_kline_data/daily_unadjusted",
                    breadth_days,
                    "symbol, close",
                )
                if not bclose.empty:
                    bclose["dt"] = bclose["dt"].astype(str)
                    bpiv = bclose.pivot_table(
                        index="symbol", columns="dt", values="close"
                    )
                    cols = [d for d in breadth_days if d in bpiv.columns]
                    if len(cols) >= 25:
                        cur_above = (bpiv[cols[0]] > bpiv[cols[:20]].mean(axis=1)).astype(float)
                        prev_above = (
                            bpiv[cols[5]] > bpiv[cols[5:25]].mean(axis=1)
                        ).astype(float)
                        for sname, syms in groups.items():
                            present = [s for s in syms if s in bpiv.index]
                            if not present:
                                continue
                            cur_vals = cur_above.reindex(present).dropna()
                            prev_vals = prev_above.reindex(present).dropna()
                            if cur_vals.empty:
                                continue
                            price_breadth[sname] = (
                                float(cur_vals.mean()),
                                float(prev_vals.mean()) if not prev_vals.empty else None,
                            )

        items: list[dict[str, Any]] = []
        for sname, syms in groups.items():
            sub = prices[prices["symbol"].isin(syms)]
            if sub.empty:
                continue
            avg_pct = round(float(sub["pct_change"].mean() or 0.0), 2)
            vals = [
                period_pct[s]
                for s in syms
                if s in period_pct and math.isfinite(period_pct[s])
            ]
            pct_period = (
                round(sum(vals) / len(vals), 2) if vals else round(avg_pct, 2)
            )
            val_yi: float | None = None
            if mv_map:
                tot_mv = sum(mv_map.get(s, 0.0) for s in syms)
                if tot_mv > 0:
                    val_yi = round(tot_mv / 1e8, 1)
            if val_yi is None:
                tot_amt = float(sub["amount"].sum() or 0.0)
                if tot_amt > 1e11:
                    val_yi = round(tot_amt / 1e8, 1)
                elif tot_amt > 1e7:
                    val_yi = round(tot_amt / 1e4, 1)
                else:
                    val_yi = round(tot_amt, 1)

            leader_row = sub.sort_values("pct_change", ascending=False).iloc[0]
            cur_main = (
                float(cur_sums.loc[[s for s in syms if s in cur_sums.index], "main"].sum())
                if not cur_sums.empty
                else 0.0
            )
            cur_amount = (
                float(
                    cur_sums.loc[
                        [s for s in syms if s in cur_sums.index], "amount_y"
                    ].sum()
                )
                if not cur_sums.empty
                else 0.0
            )
            prev_main = (
                float(prev_sums.loc[[s for s in syms if s in prev_sums.index], "main"].sum())
                if not prev_sums.empty
                else 0.0
            )
            flow_net = round(cur_main / 1e8, 4)
            flow_daily = round(flow_net / n_days, 4) if n_days else None
            prev_daily = (
                round((prev_main / 1e8) / len(prev_days), 4)
                if prev_days and not prev_sums.empty
                else None
            )
            flow_accel = (
                round(flow_daily - prev_daily, 4)
                if flow_daily is not None and prev_daily is not None
                else None
            )
            flow_ratio = (
                round(cur_main / cur_amount, 4) if cur_amount > 0 else None
            )
            cur_ratio_raw = cur_ratio.get(sname) if sname in cur_ratio.index else None
            prev_ratio_raw = prev_ratio.get(sname) if sname in prev_ratio.index else None
            cur_ratio_v = (
                float(cur_ratio_raw)
                if cur_ratio_raw is not None and math.isfinite(float(cur_ratio_raw))
                else None
            )
            prev_ratio_v = (
                float(prev_ratio_raw)
                if prev_ratio_raw is not None and math.isfinite(float(prev_ratio_raw))
                else None
            )
            flow_ratio_accel = (
                round(cur_ratio_v - prev_ratio_v, 4)
                if cur_ratio_v is not None and prev_ratio_v is not None
                else None
            )
            cur_breadth_raw = (
                cur_breadth.get(sname) if sname in cur_breadth.index else None
            )
            prev_breadth_raw = (
                prev_breadth.get(sname) if sname in prev_breadth.index else None
            )
            cur_breadth_v = (
                float(cur_breadth_raw)
                if cur_breadth_raw is not None
                and math.isfinite(float(cur_breadth_raw))
                else None
            )
            prev_breadth_v = (
                float(prev_breadth_raw)
                if prev_breadth_raw is not None
                and math.isfinite(float(prev_breadth_raw))
                else None
            )
            breadth_chg = (
                round(cur_breadth_v - prev_breadth_v, 4)
                if cur_breadth_v is not None and prev_breadth_v is not None
                else None
            )
            items.append(
                {
                    "name": sname,
                    "value": max(val_yi, 10.0),
                    "pct_change": avg_pct,
                    "pct_period": pct_period,
                    "ret_period_sum": (
                        round(ret_period_sum[sname], 4)
                        if sname in ret_period_sum
                        else None
                    ),
                    "ret_period_sum_prev": (
                        round(ret_period_sum_prev[sname], 4)
                        if sname in ret_period_sum_prev
                        else None
                    ),
                    "price_accel": price_accel.get(sname),
                    "flow_net": flow_net,
                    "flow_daily": flow_daily,
                    "prev_flow_daily": prev_daily,
                    "flow_accel": flow_accel,
                    "flow_ratio": flow_ratio,
                    "prev_flow_ratio": None if prev_ratio_v is None else round(prev_ratio_v, 4),
                    "flow_ratio_accel": flow_ratio_accel,
                    "breadth_flow": None if cur_breadth_v is None else round(cur_breadth_v, 4),
                    "breadth_flow_prev": (
                        None if prev_breadth_v is None else round(prev_breadth_v, 4)
                    ),
                    "breadth_flow_chg": breadth_chg,
                    "pct_above_ma20": (
                        round(price_breadth[sname][0], 4)
                        if sname in price_breadth
                        else None
                    ),
                    "pct_above_ma20_prev": (
                        None
                        if sname not in price_breadth
                        or price_breadth[sname][1] is None
                        else round(price_breadth[sname][1], 4)
                    ),
                    "pct_above_ma20_chg": (
                        round(
                            price_breadth[sname][0] - price_breadth[sname][1],
                            4,
                        )
                        if sname in price_breadth
                        and price_breadth[sname][1] is not None
                        else None
                    ),
                    "period": period,
                    "period_days": n_days,
                    "leader": str(leader_row.get("name") or leader_row["symbol"]),
                    "leader_pct": round(float(leader_row["pct_change"] or 0.0), 2),
                }
            )

        items.sort(key=lambda x: x["value"], reverse=True)
        return items

    return _cached(f"heatmap_{category}_{n_days}", _QUERY_TTL, _load)

def _load_valuation_mv(days: list[str]) -> pd.DataFrame:
    """指定交易日的个股总市值（元）。days 降序，逐日回退取首个可用日。"""
    if not days:
        return pd.DataFrame()
    df = _read_partitioned("5_technical_derived/valuation", days, "symbol, total_mv")
    if df.empty:
        return pd.DataFrame()
    df = df.copy()
    df["dt"] = df["dt"].astype(str)
    df["symbol"] = df["symbol"].astype(str).str.strip().str.upper()
    df["total_mv"] = pd.to_numeric(df["total_mv"], errors="coerce")
    df = df[df["total_mv"].notna() & (df["total_mv"] > 0)]
    # 同一 symbol 多日出现时保留最新日（days 降序 → drop_duplicates 保留首行）
    return df.drop_duplicates(subset=["symbol"], keep="first")


def _clean_flow_rows(flow: pd.DataFrame) -> pd.DataFrame:
    """剔除 L2 占位污染行（多只股票共享同一常量值=假数据）。

    占位判定与 ``placeholder_symbols_by_dt`` 同判据，但只针对当前单日子集，
    避免为了一次下钻而把整个窗口读进来。
    """
    if flow.empty:
        return flow
    bad = placeholder_symbols_by_dt(flow).get(str(flow["dt"].iloc[0]), set())
    return flow[~flow["symbol"].isin(bad)]


def _load_l2_main_flow(end: str | None) -> tuple[str | None, dict[str, float]]:
    """截至 ``end`` 的最近一个有 L2 数据的交易日的主力净额（元）。

    主力口径 = flow_super_net + flow_large_net（与 flow_panorama 一致）。
    L2 分区常滞后于行情分区（最新交易日尚无行），故自 ``end`` 向前回溯；
    返回 ``(flow_date, {symbol: 元})``；取不到时 ``(None, {})``，调用方须如实留空
    （而不是把 0 当成「无净额」展示）。
    """
    if not end:
        return None, {}
    for d in _l2_days(str(end).zfill(8), _L2_FLOW_MAX_LOOKBACK):
        # 必须带上 amount/flow_net_amount/flow_net_ratio：单位判别器靠
        # 「flow_net_amount / amount ≈ flow_net_ratio」恒等式分档（分区混存元/万元），
        # 少了这三列会静默退回「已是元」，万元分区被当元读 → 板块净额整列 0.00。
        fl = _read_partitioned(
            "6_ml_datasets/l2_factors",
            [d],
            "symbol, amount, flow_net_amount, flow_net_ratio, "
            "flow_super_net, flow_large_net",
        )
        if fl.empty:
            continue
        fl = _clean_flow_rows(fl)
        if fl.empty:
            continue
        # 逐日金额档位归一（元），与 flow_panorama 的主力口径一致
        fl = normalize_l2_flow_money_to_yuan(fl)
        m: dict[str, float] = {}
        for r in fl.itertuples(index=False):
            m[str(r.symbol).strip()] = _f(getattr(r, "flow_super_net", 0.0)) + _f(
                getattr(r, "flow_large_net", 0.0)
            )
        if m:
            return d, m
    return None, {}


def get_sector_drill(
    category: str = "shenwan",
    name: str = "",
    date: str | None = None,
) -> dict[str, Any] | None:
    """板块 → 成分个股热力矩形图（下钻）。

    矩形面积=个股总市值（亿，与板块层同口径，父子面积自洽）；
    颜色可由前端按 ``pct_change``（涨跌幅换挡）或 ``flow_net``（主力净额，元→亿）着色。
    超过 :data:`MAX_DRILL_ITEMS` 的成分按市值降序截断，并在 ``summary`` 中如实披露。

    找不到板块（名不存在/该体系无此板块）返回 ``None``；无行情数据的成分保留在
    结果里（``pct_change=0``、``value`` 走成交额兜底），确保「板块有多少票就出多少图块」。
    """
    sector_name = str(name or "").strip()
    if not sector_name or category not in CATEGORY_TYPE:
        return None
    target = _normalize_date(date)

    def _load() -> dict[str, Any] | None:
        if not _available():
            return None

        members = _sector_members()
        if members.empty or "sector_type" not in members.columns:
            return None
        stype = CATEGORY_TYPE[category]
        sub = members[
            (members["sector_type"].astype(str) == stype)
            & (members["sector_name"].astype(str).str.strip() == sector_name)
        ]
        if sub.empty:
            return None
        syms = sorted(
            {str(s).strip() for s in sub["symbol"].astype(str) if str(s).strip()}
        )
        if not syms:
            return None

        trade_date, prices = _market_pct_snapshot(target)
        if prices.empty:
            # 涨跌幅快照缺失：仍产出成分（市值/名称可用），颜色退化为 0
            prices = pd.DataFrame(columns=["symbol", "close", "amount", "pct_change"])
            trade_date = target or _latest_trade_date()

        ref = str(trade_date or target or _latest_trade_date() or "").zfill(8)
        days = _trading_days(ref, 8) if ref else []

        # 市值（元→亿）：优先参考日，缺失则向前回退到最近可用分区
        mv = _load_valuation_mv(days)
        mv_map: dict[str, float] = {}
        if not mv.empty:
            for r in mv.itertuples(index=False):
                v = float(r.total_mv or 0.0)
                mv_map[str(r.symbol)] = v if math.isfinite(v) else 0.0

        # L2 主力净额（flow_super_net + flow_large_net，元→亿）：参考日（缺失则前溯）
        flow_map: dict[str, float] = {}
        if ref:
            _, flow_map = _load_l2_main_flow(ref)

        names = _instrument_names()
        price_map: dict[str, dict[str, float]] = {}
        for r in prices.itertuples(index=False):
            price_map[str(r.symbol).strip()] = {
                "pct_change": _f(getattr(r, "pct_change", 0.0)),
                "amount": _f(getattr(r, "amount", 0.0)),
            }

        rows: list[dict[str, Any]] = []
        for sym in syms:
            p = price_map.get(sym)
            pct = p["pct_change"] if p else 0.0
            amount_wan = p["amount"] if p else 0.0
            mv_yuan = mv_map.get(sym, 0.0)
            val_yi = (
                round(mv_yuan / 1e8, 2) if mv_yuan > 0 else round(amount_wan / 1e4, 2)
            )
            net_yuan = flow_map.get(sym, 0.0)
            rows.append(
                {
                    "symbol": _normalize_prefix(sym),
                    "name": names.get(sym, sym),
                    "value": max(val_yi, 0.01),
                    "pct_change": round(pct, 2),
                    "flow_net": round(net_yuan / 1e8, 4),
                    "is_leader": False,
                }
            )

        total = len(rows)
        rows.sort(key=lambda x: x["value"], reverse=True)
        truncated = total > MAX_DRILL_ITEMS
        if truncated:
            rows = rows[:MAX_DRILL_ITEMS]
        if rows:
            rows[0]["is_leader"] = True

        adv = sum(1 for r in rows if r["pct_change"] > 0)
        dec = sum(1 for r in rows if r["pct_change"] < 0)
        flat = len(rows) - adv - dec
        avg_pct = (
            round(sum(r["pct_change"] for r in rows) / len(rows), 2) if rows else 0.0
        )
        total_flow = (
            round(sum(r["flow_net"] for r in rows if r["flow_net"] is not None), 2)
            if any(r["flow_net"] is not None for r in rows)
            else None
        )

        return {
            "trade_date": _dash(ref),
            "category": category,
            "category_label": CATEGORY_LABEL.get(category, category),
            "sector_name": sector_name,
            "items": rows,
            "summary": {
                "total": total,
                "shown": len(rows),
                "truncated": truncated,
                "advance": adv,
                "decline": dec,
                "flat": flat,
                "avg_pct": avg_pct,
                "total_flow_yi": total_flow,
                "total_mv_yi": round(sum(r["value"] for r in rows), 1),
            },
        }

    key = f"sector_drill_{category}_{sector_name}_{target or 'latest'}"
    return _cached(key, _QUERY_TTL, _load)


def _l2_days(end: str, n: int) -> list[str]:
    """截至 end 的最近 n 个有 L2 数据的交易日（降序）。"""
    dates = _get_partition_dates("6_ml_datasets/l2_factors")
    if dates:
        return [d for d in dates if d <= end][:n]
    return []


def _normalize_date(date: str | None) -> str | None:
    """YYYY-MM-DD / YYYYMMDD → YYYYMMDD；非法输入返回 None。"""
    if not date:
        return None
    s = str(date).strip().replace("-", "")
    return s if len(s) == 8 and s.isdigit() else None


def _dash(yyyymmdd: str | None) -> str:
    """YYYYMMDD → YYYY-MM-DD（None/空 → 空串）。"""
    s = str(yyyymmdd or "").zfill(8)
    return f"{s[:4]}-{s[4:6]}-{s[6:]}" if len(s) == 8 and s.isdigit() else ""


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


MATCH_LIMIT_DEFAULT = 30


def match_sectors(
    members: pd.DataFrame, q: str, limit: int = MATCH_LIMIT_DEFAULT
) -> list[dict[str, Any]]:
    """把用户输入解析为板块名候选（纯函数；只扫成分表，不算行情/资金流）。

    匹配语义与 ``/tags/by-tag`` 同为**子串、大小写不敏感**（``regex=False``，
    用户输入的 ``(``/``[`` 等正则元字符原样当字面量，不会误伤）。排序：精确
    > 前缀 > 子串，组内按成分数降序、名称升序——「航空」应先把「航空机场」
    抬出来，而不是随机顺序。返回 ``[{name, sector_type, count}]``。
    """
    query = (q or "").strip().lower()
    if not query or members.empty or "sector_name" not in members.columns:
        return []
    m = members.copy()
    m["sector_name"] = m["sector_name"].astype(str).str.strip()
    m = m[m["sector_name"] != ""]
    if m.empty:
        return []
    sub = m[m["sector_name"].str.lower().str.contains(query, regex=False, na=False)]
    if sub.empty:
        return []
    grp = (
        sub.groupby(["sector_name", "sector_type"], dropna=False)
        .size()
        .reset_index(name="count")
    )
    rows: list[tuple[int, int, str, dict[str, Any]]] = []
    for r in grp.itertuples(index=False):
        name = str(r.sector_name)
        nl = name.lower()
        rank = 0 if nl == query else (1 if nl.startswith(query) else 2)
        rows.append(
            (
                rank,
                -int(r.count),
                name,
                {
                    "name": name,
                    "sector_type": str(r.sector_type or "通用标签"),
                    "count": int(r.count),
                },
            )
        )
    rows.sort(key=lambda x: x[:3])
    return [item for *_, item in rows[:limit]]


def get_tag_matches(q: str, limit: int = MATCH_LIMIT_DEFAULT) -> list[dict[str, Any]]:
    """板块名模糊解析（成员表进程内 lru 缓存，逐击键扫描即毫秒级；不进 _cached
    ——用户输入是开集，无界缓存键会随击键膨胀）。"""
    return match_sectors(_sector_members(), q, limit)


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


def get_tag_stats(limit: int = 30, hot_type: str | None = None) -> dict[str, Any]:
    """标签体系统计与热门标签（按成分股数量排序，真实 sector_members 聚合）。

    hot_type 按体系过滤热门（呼应 SQLite 快照侧同参）；统计四数恒为全量口径。
    """
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
        if hot_type:
            grp = grp[grp["sector_type"].astype(str) == hot_type]
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

    return _cached(f"tag_stats_{limit}_{hot_type or 'all'}", 300.0, _load)
