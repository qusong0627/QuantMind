"""市场分析 — QuantDB 本地 parquet 数据对接层。

把 Market Analysis 页面各接口从「硬编码假数据」切换到 QuantDB 本地数据：
- 指数概览：index_daily（最新交易日 9 大指数点位/涨跌/成交额/量比/近5日 trend）
- 市场广度：technical_indicators.pct_change（涨跌家数/涨停跌停/量能，复用 shared.market_breadth）
- 资金流：l2_factors（行业与个股资金净流向，最新交易日）
- 标签：sector_concept/sector_members + instrument_detail（真实行业/概念成分股）

取数口径与 quantdb_feed 对齐：交易日走 dt=YYYYMMDD 目录枚举，查询只打开真正
需要的分区文件（read_parquet 精确路径数组），参考表按源文件 mtime 缓存；
不用 ``SELECT DISTINCT dt`` / ``dt=*`` 全量 glob 去枚举上千个 parquet。
单位口径遵循 quantdb-fields 技能：个股 amount=万元、l2 flow=元、指数 volume=手。
"""
from __future__ import annotations

import logging
import threading
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

import pandas as pd

from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub
from backend.shared.market_breadth import (
    CAT_LIMIT_DOWN,
    CAT_LIMIT_UP,
    breadth_distribution,
    classify_by_pct,
    classify_price,
    compute_limits,
    is_bse_symbol,
    is_corp_action_pct,
    is_ex_div,
    limit_pct,
    market_breadth,
    sector_aggregate,
    TOL_BJ,
    TOL_SHSZ,
)

logger = logging.getLogger(__name__)

_duck_local = threading.local()

# 分区数据集（dt=YYYYMMDD/data.parquet）相对数据根的路径
_DAILY_REL = "1_kline_data/daily_unadjusted"
_INDEX_DAILY_REL = "1_kline_data/index_daily"
_TECH_REL = "5_technical_derived/technical_indicators"
_L2_REL = "6_ml_datasets/l2_factors"
_MEMBERS_REL = "2_base_sector/sector_concept/sector_members.parquet"
_INSTRUMENT_DIR = "2_base_sector/instrument_detail"


def _hub() -> QuantDBDataHub:
    """与 quantdb_feed 共用的 QuantDB 数据中枢单例。"""
    return QuantDBDataHub.get_instance()


def _data_dir() -> Path:
    """QuantDB 数据根目录。

    解析口径交给 QuantDBDataHub（QM_QUANTDB_DATA_DIR → 容器 /data/quantdb →
    本地盘符 → 仓库 data/quantdb）。此前这里只自己探两个硬编码候选，与数据平台
    实际挂载点不一致时整页查不到数据。
    """
    try:
        return _hub().data_dir
    except Exception as exc:  # pragma: no cover - hub 不可用时兜底
        logger.warning("QuantDBDataHub 数据目录解析失败，回退默认候选: %s", exc)
    for cand in (Path("/data/quantdb"), Path("data/quantdb")):
        if (cand / "1_kline_data").is_dir():
            return cand
    return Path("/data/quantdb")


def _sql_rel(rel: str = "") -> str:
    """拼 SQL 字面量用的 posix 路径（Windows 反斜杠会打断 DuckDB 的 glob）。"""
    path = _data_dir() / rel if rel else _data_dir()
    return str(path).replace("\\", "/")


def _partition_dates(rel_path: str) -> list[str]:
    """分区目录名即 dt 值：一次目录扫描得到降序交易日，不碰任何 parquet 内容。"""
    try:
        dd = _data_dir() / rel_path
        if not dd.is_dir():
            return []
        dates: list[str] = []
        for entry in dd.iterdir():
            name = entry.name
            if not name.startswith("dt="):
                continue
            value = name[3:]
            if value.isdigit() and entry.is_dir():
                dates.append(value)
        return sorted(dates, reverse=True)
    except OSError as exc:
        logger.warning("读取分区日期列表失败 %s: %s", rel_path, exc)
        return []


def _as_dt(value: Any) -> str | None:
    """校验外部传入的交易日（YYYYMMDD）；非法值返回 None，避免拼进 SQL。"""
    text = str(value or "").strip().replace("-", "")
    return text if text.isdigit() and len(text) == 8 else None


def _conn():
    """线程内复用的裸 DuckDB 连接。

    本模块的查询全部是显式路径的 ``read_parquet([...])``，不需要 DataHub 那套
    预挂载视图；走 ``hub.query()`` 会在每个新工作线程上先 CREATE 13 个
    ``dt=*/*.parquet + union_by_name`` 视图（实测首次查询 35s）。这里只留一个
    空连接，既复用 parquet 元数据缓存，又不为用不到的视图买单。
    """
    conn = getattr(_duck_local, "conn", None)
    if conn is None:
        import duckdb

        conn = duckdb.connect()
        _duck_local.conn = conn
    return conn


def _read(sql: str) -> pd.DataFrame:
    """执行查询；失败返回空表，由调用方短路，不把异常抛给接口层。"""
    try:
        return _conn().execute(sql).fetchdf()
    except Exception as exc:
        logger.warning("QuantDB 查询失败: %s", exc)
        return pd.DataFrame()


def _read_partitioned(rel_path: str, dates: list[str], cols: str) -> pd.DataFrame:
    """按精确分区路径读取指定交易日。

    与 ``dt=*`` glob + ``WHERE dt IN (...)`` 等价，但 DuckDB 不必枚举全部分区：
    daily_unadjusted 两天实测 0.78s → 0.007s，index_daily 三十天 1.92s → 0.017s。
    """
    base = _data_dir() / rel_path
    existing = [d for d in dates if (base / f"dt={d}").is_dir()]
    if len(existing) != len(dates):
        logger.debug("分区缺失跳过 %s: %d/%d 天", rel_path, len(dates) - len(existing), len(dates))
    if not existing:
        return pd.DataFrame()
    paths = ", ".join(f"'{_sql_rel(rel_path)}/dt={d}/*.parquet'" for d in existing)
    return _read(
        f"SELECT {cols}, dt FROM read_parquet([{paths}], hive_partitioning=true)"
    )


@lru_cache(maxsize=8)
def _read_ref_cached(rel: str, stamp: int) -> pd.DataFrame:
    """参考表读取；``stamp`` 为源文件 mtime_ns，仅参与缓存键。"""
    del stamp
    return _read(f"SELECT * FROM read_parquet('{_sql_rel(rel)}')")


def _ref_frame(rel: str) -> pd.DataFrame:
    """按数据版本缓存参考表（板块成分 7.9 万行 / 标的快照 5566 行）。"""
    path = _data_dir() / rel
    if not path.exists():
        logger.warning("市场分析参考表缺失: %s", path)
        return pd.DataFrame()
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        return _read_ref_cached(rel, 0)
    return _read_ref_cached(rel, stamp).copy()


def _instrument_rel() -> str | None:
    """标的快照文件名（instrument_detail / instrument_list 两份都存在过）。"""
    for fname in ("instrument_detail.parquet", "instrument_list.parquet"):
        if (_data_dir() / _INSTRUMENT_DIR / fname).exists():
            return f"{_INSTRUMENT_DIR}/{fname}"
    return None


def _instrument_frame() -> pd.DataFrame:
    rel = _instrument_rel()
    return _ref_frame(rel) if rel else pd.DataFrame()


def _pick_col(df: pd.DataFrame, *names: str) -> str | None:
    for name in names:
        if name in df.columns:
            return name
    return None


def _str_key_map(df: pd.DataFrame, key: str, value: str) -> dict[str, Any]:
    """DataFrame 两列 → {str(key): value}；两列同源，长度必然一致。"""
    return dict(zip(df[key].astype(str), df[value], strict=True))


def _latest_trade_date() -> str | None:
    """最新交易日（YYYYMMDD），以 daily_unadjusted 实际分区为准。"""
    dates = _partition_dates(_DAILY_REL)
    if dates:
        return dates[0]
    # 兜底：目录枚举不可用时退回全分区扫描（慢，但保持可用）
    df = _read(
        f"SELECT max(dt) AS dt FROM read_parquet('{_sql_rel(_DAILY_REL)}/dt=*/data.parquet',"
        " hive_partitioning=true)"
    )
    if df.empty or df.iloc[0]["dt"] is None:
        return None
    return str(int(df.iloc[0]["dt"]))


def _trading_days(end: str | None, n: int) -> list[str]:
    """截至 end 的最近 n 个交易日（降序，[0] 为最新）。"""
    end_dt = _as_dt(end)
    dates = _partition_dates(_DAILY_REL)
    if dates:
        if end_dt:
            dates = [d for d in dates if d <= end_dt]
        return dates[:n]
    cond = f"WHERE dt <= {end_dt}" if end_dt else ""
    df = _read(
        f"SELECT DISTINCT dt FROM read_parquet('{_sql_rel(_DAILY_REL)}/dt=*/data.parquet',"
        f" hive_partitioning=true) {cond} ORDER BY dt DESC LIMIT {int(n)}"
    )
    return [] if df.empty else [str(int(v)) for v in df["dt"].tolist()]


def _load_st_names() -> set[str]:
    """ST 标的集合（按名称含 ST 判定，与旧实现口径一致）。"""
    df = _instrument_frame()
    sym = _pick_col(df, "Symbol", "symbol")
    name = _pick_col(df, "Name", "name")
    if df.empty or sym is None or name is None:
        return set()
    return {str(s) for s, n in zip(df[sym], df[name], strict=True) if "ST" in str(n)}


# ---------------------------------------------------------------------------
# 指数概览
# ---------------------------------------------------------------------------

_INDEXES: list[tuple[str, str]] = [
    ("上证指数", "000001.SH"),
    ("深证成指", "399001.SZ"),
    ("创业板指", "399006.SZ"),
    ("沪深300", "000300.SH"),
    ("科创50", "000688.SH"),
    ("北证50", "899050.BJ"),
    ("中证500", "000905.SH"),
    ("中证1000", "000852.SH"),
    ("上证50", "000016.SH"),
]


def indices_overview(trade_date: str | None = None) -> list[dict[str, Any]]:
    """大盘核心指数快照（最新交易日）。"""
    td = _as_dt(trade_date) or _latest_trade_date()
    if not td:
        return []
    dts = _trading_days(td, 30)
    if not dts:
        return []
    df = _read_partitioned(_INDEX_DAILY_REL, dts, "symbol, high, low, close, amount")
    if df.empty:
        return []
    df["dt"] = df["dt"].astype(str)
    out: list[dict[str, Any]] = []
    for name, sym in _INDEXES:
        sub = df[df["symbol"] == sym].sort_values("dt")
        if sub.empty:
            continue
        # index_daily 没有 preClose 列，涨跌幅用 close 序列自算
        today = sub.iloc[-1]
        prev_close = float(sub["close"].iloc[-2]) if len(sub) >= 2 else None
        pct = round((float(today["close"]) / prev_close - 1) * 100, 2) if prev_close else 0.0
        trend = [round(float(x), 2) for x in sub["close"].iloc[-5:].tolist()]
        out.append(
            {
                "symbol": sym,
                "name": name,
                "price": round(float(today["close"]), 2),
                "change": round(float(today["close"]) - prev_close, 2) if prev_close else 0.0,
                "pct_change": pct,
                "turnover": round(float(today["amount"]) / 1e4, 2),  # 万元→亿
                "trade_date": td,
                "trend": trend,
            }
        )
    return out


# ---------------------------------------------------------------------------
# 市场广度
# ---------------------------------------------------------------------------

def market_breadth_stats(trade_date: str | None = None) -> dict[str, Any]:
    """涨跌家数 / 涨停跌停 / 量能（复用 daily-review 的涨跌停规则）。"""
    td = _as_dt(trade_date) or _latest_trade_date()
    if not td:
        return {}
    dts = _trading_days(td, 2)
    if len(dts) < 2:
        return {}
    prev, cur = dts[1], dts[0]

    st_set = _load_st_names()
    unadj = _read_partitioned(
        _DAILY_REL, dts, "symbol, open, high, low, close, volume, amount"
    )
    if unadj.empty:
        return {}
    unadj["dt"] = unadj["dt"].astype(str)
    tech = _read_partitioned(_TECH_REL, [cur], "symbol, pct_change")
    if tech.empty:
        tech = pd.DataFrame(columns=["symbol", "pct_change"])
    tech["symbol"] = tech["symbol"].astype(str)
    today = unadj[unadj["dt"] == cur].merge(
        unadj[unadj["dt"] == prev][["symbol", "close"]].rename(columns={"close": "prev_close"}),
        on="symbol",
        how="left",
    ).merge(tech[["symbol", "pct_change"]], on="symbol", how="left")
    today["is_st"] = today["symbol"].isin(st_set)

    trade_dt = datetime.strptime(cur, "%Y%m%d").date()
    cats: list[str] = []
    for _, row in today.iterrows():
        p = float(row["pct_change"]) if pd.notna(row["pct_change"]) else 0.0
        if pd.notna(row["prev_close"]) and not is_ex_div(p, float(row["close"]), float(row["prev_close"])):
            up, down = compute_limits(
                row["symbol"], float(row["prev_close"]), is_st=row["is_st"], trade_date=trade_dt
            )
            cats.append(classify_price(float(row["close"]), float(row["high"]), up, down))
        else:
            cats.append(classify_by_pct(p, row["symbol"], row["is_st"], trade_dt))
    today["category"] = cats

    suspended = today["volume"].fillna(0) == 0
    active_pct = today[~suspended]["pct_change"].dropna()
    breadth = market_breadth(active_pct)

    limit_up = int((today["category"] == CAT_LIMIT_UP).sum())
    limit_down = int((today["category"] == CAT_LIMIT_DOWN).sum())
    broke_up = int((today["category"].astype(str) == "broke_up").sum())
    non_limit = today[(~suspended) & (~today["category"].astype(str).isin([CAT_LIMIT_UP, CAT_LIMIT_DOWN, "corp_action"]))]
    dist = breadth_distribution(non_limit["pct_change"].dropna())

    total_amount = float(today["amount"].sum())
    prev_total = float(unadj[unadj["dt"] == prev]["amount"].sum())

    # 连板高度（近 12 日涨停连板）
    dts12 = _trading_days(td, 12)
    max_streak = 0
    if limit_up > 0:
        syms = today[today["category"] == CAT_LIMIT_UP]["symbol"].tolist()
        tech12 = _read_partitioned(_TECH_REL, dts12, "symbol, pct_change")
        if tech12.empty:
            tech12 = pd.DataFrame(columns=["symbol", "dt", "pct_change"])
        tech12 = tech12[tech12["symbol"].astype(str).isin({str(s) for s in syms})]
        tech12["dt"] = tech12["dt"].astype(str)
        st_map = today.set_index("symbol")["is_st"].to_dict()
        from backend.shared.market_breadth import streak_from_tail

        for sym, g in tech12.groupby("symbol"):
            g = g.sort_values("dt")
            if g["dt"].iloc[-1] != cur:
                continue
            tol = TOL_BJ if is_bse_symbol(sym) else TOL_SHSZ
            board = float(limit_pct(sym, is_st=st_map.get(sym, False), trade_date=trade_dt)) * 100
            n = streak_from_tail(g["pct_change"].tolist(), board - tol)
            max_streak = max(max_streak, n)

    return {
        "trade_date": td,
        "up_count": breadth["up_count"],
        "down_count": breadth["down_count"],
        "flat_count": breadth["flat_count"],
        "up_down_ratio": breadth["up_down_ratio"],
        "limit_up": limit_up,
        "limit_down": limit_down,
        "broke_up": broke_up,
        "max_streak": max_streak,
        "total_amount_yi": round(total_amount / 1e4, 2),
        "prev_amount_yi": round(prev_total / 1e4, 2),
        "dist": dist,
    }


# ---------------------------------------------------------------------------
# 资金流（l2_factors）
# ---------------------------------------------------------------------------

_FLOW_COLS = ["flow_net_amount", "flow_super_net", "flow_large_net", "flow_medium_net", "flow_small_net"]


def _l2_latest_dt() -> str | None:
    """最新有 L2 资金流的交易日（目录枚举，不扫全部分区）。"""
    dates = _partition_dates(_L2_REL)
    if dates:
        return dates[0]
    # 兜底同样只用显式路径：本模块的连接没有挂载任何 hub 视图
    df = _read(
        f"SELECT max(dt) AS dt FROM read_parquet('{_sql_rel(_L2_REL)}/dt=*/data.parquet',"
        " hive_partitioning=true)"
    )
    if df.empty or df.iloc[0]["dt"] is None:
        return None
    return str(int(df.iloc[0]["dt"]))


def _load_l2_flow(dt: str | None = None) -> pd.DataFrame:
    """读取指定（默认最新）交易日资金流（单位：元）。

    旧实现 ``FROM read_parquet('l2_factors/dt=*/data.parquet')`` 不带 dt 条件，
    每次请求都要读全历史（本机实测 885 个分区 / 8.2GB / 451 万行 / 22.5s），
    而所有调用方只取其中一天；单日精确分区实测 0.04s。
    """
    day = dt or _l2_latest_dt()
    if not day:
        return pd.DataFrame(columns=["symbol", *_FLOW_COLS, "dt"])
    df = _read(
        f"SELECT symbol, {', '.join(_FLOW_COLS)}, dt FROM read_parquet("
        f"'{_sql_rel(_L2_REL)}/dt={day}/*.parquet', hive_partitioning=true)"
    )
    if df.empty:
        return df
    df["dt"] = df["dt"].astype(str)
    return df


def _sector_industry_members() -> pd.DataFrame:
    """申万一级行业成分（板块成分表按 mtime 缓存后再过滤）。"""
    members = _load_sector_members()
    if members.empty or "SectorType" not in members.columns:
        return pd.DataFrame()
    return members[members["SectorType"].astype(str) == "行业板块(一级)"]


def money_flow_period(period: str = "1d", dimension: str = "sector") -> list[dict[str, Any]]:
    """指定周期资金净流向排行榜。

    单位：flow_* 为元。多周期选项（3d/5d/10d/20d）无历史累计时以最新单日真实值
    为准并标注截至日，绝不乘系数伪造多周期。
    个股级（stock）返回真实收盘价与涨跌幅。
    """
    latest = _l2_latest_dt()
    if not latest:
        return []
    day = _load_l2_flow(latest)
    if day.empty:
        return []

    if dimension == "sector":
        members = _sector_industry_members()
        if members.empty:
            return []
        merged = day.merge(members, left_on="symbol", right_on="Symbol", how="inner")
        agg = (
            merged.groupby(["SectorCode", "SectorName"])
            .agg(net=("flow_net_amount", "sum"), n=("symbol", "nunique"))
            .reset_index()
            .sort_values("net", ascending=False)
        )
        return [
            {
                "id": f"SW_{r['SectorCode']}",
                "name": r["SectorName"],
                "net_inflow": round(float(r["net"])),  # 元
                "stocks": int(r["n"]),
                "trade_date": latest,
            }
            for _, r in agg.head(31).iterrows()
        ]

    # stock 维度：真实收盘价 + 涨跌幅
    name_map = _load_instrument_names()
    td = _latest_trade_date()
    close_map: dict[str, float] = {}
    pct_map: dict[str, float] = {}
    if td:
        kline = _read_partitioned(_DAILY_REL, [td], "symbol, close")
        if not kline.empty:
            close_map = dict(zip(kline["symbol"].astype(str), kline["close"], strict=True))
        tech = _read_partitioned(_TECH_REL, [td], "symbol, pct_change")
        if not tech.empty:
            pct_map = dict(zip(tech["symbol"].astype(str), tech["pct_change"], strict=True))
    day = day.sort_values("flow_net_amount", ascending=False)
    out = []
    for _, r in day.head(31).iterrows():
        sym = str(r["symbol"])
        out.append(
            {
                "symbol": sym,
                "name": name_map.get(sym, ""),
                "net_inflow": round(float(r["flow_net_amount"])),  # 元
                "close_price": round(float(close_map[sym]), 2) if sym in close_map else None,
                "pct_change": round(float(pct_map[sym]), 2) if sym in pct_map else None,
                "trade_date": latest,
            }
        )
    return out


def stock_money_flow(limit: int = 20) -> list[dict[str, Any]]:
    """个股资金流向排行榜（l2 真实资金流 + 当日真实收盘价/涨跌幅）。

    单位：flow_* 为元；close_price 为元（不复权）；pct_change 为 %。
    """
    latest = _l2_latest_dt()
    if not latest:
        return []
    day = _load_l2_flow(latest).sort_values("flow_net_amount", ascending=False)
    if day.empty:
        return []

    name_map = _load_instrument_names()

    # 当日真实行情：收盘价（不复权）+ 涨跌幅（%），只取排行榜涉及的分区
    close_map: dict[str, float] = {}
    pct_map: dict[str, float] = {}
    kline = _read_partitioned(_DAILY_REL, [latest], "symbol, close")
    if not kline.empty:
        close_map = _str_key_map(kline, "symbol", "close")
    tech = _read_partitioned(_TECH_REL, [latest], "symbol, pct_change")
    if not tech.empty:
        pct_map = _str_key_map(tech, "symbol", "pct_change")

    out = []
    for _, r in day.head(limit).iterrows():
        sym = str(r["symbol"])
        out.append(
            {
                "symbol": sym,
                "name": name_map.get(sym, ""),
                "close_price": round(float(close_map[sym]), 2) if sym in close_map else None,
                "pct_change": round(float(pct_map[sym]), 2) if sym in pct_map else None,
                "net_inflow": round(float(r["flow_net_amount"])),
                "main_ratio": round(
                    (float(r["flow_super_net"]) + float(r["flow_large_net"])) / max(float(r["flow_net_amount"]), 1) * 100, 1
                ) if pd.notna(r["flow_net_amount"]) and r["flow_net_amount"] else None,
                "super_large": round(float(r["flow_super_net"])),
                "large": round(float(r["flow_large_net"])),
                "medium": round(float(r["flow_medium_net"])),
                "small": round(float(r["flow_small_net"])),
                "trade_date": latest,
            }
        )
    return out


def money_flow_sankey() -> dict[str, Any]:
    """主力/散户资金流向桑基图（l2 真实行业资金流，单位：元）。"""
    latest = _l2_latest_dt()
    if not latest:
        return {"nodes": [], "links": [], "trade_date": ""}
    day = _load_l2_flow(latest)
    if day.empty:
        return {"nodes": [], "links": [], "trade_date": ""}
    members = _sector_industry_members()
    if members.empty:
        return {"nodes": [], "links": [], "trade_date": latest}
    merged = day.merge(members, left_on="symbol", right_on="Symbol", how="inner")
    agg = merged.groupby("SectorName").agg(
        super=("flow_super_net", "sum"),
        large=("flow_large_net", "sum"),
        medium=("flow_medium_net", "sum"),
        small=("flow_small_net", "sum"),
    ).fillna(0)

    # 主力（超大+大单）净流入行业；散户（中+小单）净流入行业
    main_sectors = agg[agg["super"] + agg["large"] > 0].sort_values("super", ascending=False).head(6)
    retail_sectors = agg[agg["medium"] + agg["small"] > 0].sort_values("medium", ascending=False).head(4)

    nodes = [
        {"name": "主力资金 (Net Buy)"},
        {"name": "散户资金 (Retail)"},
        {"name": "超大单 (Super Large)"},
        {"name": "大单 (Large)"},
        {"name": "中单 (Medium)"},
        {"name": "小单 (Small)"},
    ]
    links = [
        {"source": "主力资金 (Net Buy)", "target": "超大单 (Super Large)",
         "value": int(float(agg["super"].sum()))},
        {"source": "主力资金 (Net Buy)", "target": "大单 (Large)",
         "value": int(float(agg["large"].sum()))},
        {"source": "散户资金 (Retail)", "target": "中单 (Medium)",
         "value": int(float(agg["medium"].sum()))},
        {"source": "散户资金 (Retail)", "target": "小单 (Small)",
         "value": int(float(agg["small"].sum()))},
    ]

    for name, row in main_sectors.iterrows():
        target = "超大单 (Super Large)" if row["super"] >= row["large"] else "大单 (Large)"
        nodes.append({"name": name})
        links.append({"source": target, "target": name, "value": int(abs(float(max(row["super"], row["large"]))))})
    for name, row in retail_sectors.iterrows():
        target = "中单 (Medium)" if abs(row["medium"]) >= abs(row["small"]) else "小单 (Small)"
        nodes.append({"name": name})
        links.append({"source": target, "target": name, "value": int(abs(float(max(row["medium"], row["small"]))))})

    return {"nodes": nodes, "links": links, "trade_date": latest}


# ---------------------------------------------------------------------------
# 标签双向查询
# ---------------------------------------------------------------------------

def _load_sector_members() -> pd.DataFrame:
    """板块成分表（7.9 万行；按源文件 mtime 缓存，同一天内多次调用只读一次）。"""
    return _ref_frame(_MEMBERS_REL)


def _load_instrument_names() -> dict[str, str]:
    df = _instrument_frame()
    sym = _pick_col(df, "Symbol", "symbol")
    name = _pick_col(df, "Name", "name")
    if df.empty or sym is None or name is None:
        return {}
    return dict(zip(df[sym].astype(str), df[name], strict=True))


def stocks_by_tag(tag: str, limit: int = 30) -> dict[str, Any]:
    """根据标签查个股（真实板块成分 + 真实名称 + 最新成交/涨跌幅）。"""
    members = _load_sector_members()
    if members.empty or "SectorName" not in members.columns:
        return {"tag": tag, "total": 0, "items": []}
    matched = members[members["SectorName"].astype(str).str.contains(tag, case=False, na=False)]
    if matched.empty:
        return {"tag": tag, "total": 0, "items": []}

    symbols = matched["Symbol"].unique()[:limit].tolist()
    td = _latest_trade_date()
    wanted = {str(s) for s in symbols}
    tech_map: dict[str, float] = {}
    if td:
        tech = _read_partitioned(_TECH_REL, [td], "symbol, pct_change")
        if not tech.empty:
            tech = tech[tech["symbol"].astype(str).isin(wanted)]
            tech_map = _str_key_map(tech, "symbol", "pct_change")
    names = _load_instrument_names()

    items = []
    for i, sym in enumerate(symbols):
        key = str(sym)
        items.append(
            {
                "symbol": key,
                "name": names.get(key, f"成分股_{i + 1}"),
                "close_price": None,
                "pct_change": round(float(tech_map[key]), 2) if key in tech_map else None,
                "market_cap": None,
                "net_inflow": None,
                "trade_date": td or "",
            }
        )
    return {"tag": tag, "total": len(symbols), "items": items}


def tags_by_stock(symbol: str) -> dict[str, Any]:
    """根据个股查标签（真实行业/概念归属）。"""
    members = _load_sector_members()
    if members.empty or "Symbol" not in members.columns:
        return {"symbol": symbol, "stock_name": "", "tags": {}, "total": 0}
    names = _load_instrument_names()
    if symbol.endswith((".SH", ".SZ", ".BJ")) or symbol.isdigit():
        # 后缀或纯数字 → 匹配 Symbol
        matched = members[members["Symbol"].astype(str).str.contains(symbol, case=False, na=False)]
    else:
        # 名称 → 先映射代码再匹配
        code = {v: k for k, v in names.items()}.get(symbol)
        matched = members[members["Symbol"] == code] if code else pd.DataFrame()
    if matched.empty:
        return {"symbol": symbol, "stock_name": "", "tags": {}, "total": 0}

    tags_by_type: dict[str, list[str]] = {}
    for _, row in matched.iterrows():
        stype = str(row.get("SectorType", "通用标签"))
        sname = str(row.get("SectorName", ""))
        if sname:
            tags_by_type.setdefault(stype, []).append(sname)
    # 去重保序
    for k in tags_by_type:
        tags_by_type[k] = list(dict.fromkeys(tags_by_type[k]))
    return {
        "symbol": symbol,
        "stock_name": names.get(str(matched.iloc[0]["Symbol"]), ""),
        "tags": tags_by_type,
        "total": int(matched["Symbol"].nunique()),
    }
