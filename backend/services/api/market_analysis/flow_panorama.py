"""资金流全景 v2 — 桑基 + 板块/个股明细端点实现（细案 v2 §4/§5/§8）。

单位契约（与前端约定）：
- 节点/链接/成分行的 ``net``/``net_main`` 等金额一律为「元」原值（float）；
- 图表序列（``trend``/``series``）为「亿元」保留 4 位小数；
- ``net_ratio`` = 主力净额 / 成交额（窗口内归一后求和再相除）；
- 桑基 ``value``（链接宽度）= |net|，恒 >= 0；方向看 ``sign``（红=净流入、绿=净流出）。

预测分级（细案 v2 §3）：
- 板块行 ``streak``/``evidence`` 为 P 级字段，一律来自离线证据产物
  （``backend/shared/flow_evidence.py``）；产物缺失/陈旧 → null + meta 披露，
  不用陈旧 streak 冒充当日状态。
- 个股行不设资金流预测徽章（预研实测否决，细案 §2.5 表 A）——个股预测锚走
  因子板（P3 对接 ``factor`` 字段）。
"""

from __future__ import annotations

import logging
import re
from datetime import date as _date
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

from backend.shared import flow_evidence
from backend.shared.stock_utils import StockCodeUtil

from .quantdb_feed import (
    PERIOD_DAYS,
    _available,
    _cached,
    _get_partition_dates,
    _instrument_names,
    _load_l2_flow,
    _load_prices,
    _normalize_prefix,
    _read_partitioned,
    _sector_members,
    placeholder_symbols_by_dt,
)

logger = logging.getLogger(__name__)

# ── 板块体系 ─────────────────────────────────────────────────────────
SECTOR_SET_TYPE = {
    "industry1": "行业板块(一级)",
    "industry2": "行业板块(二级)",
    "concept": "概念板块",
}
SECTOR_SET_LABEL = {
    "industry1": "行业一级",
    "industry2": "行业二级",
    "concept": "概念板块",
}
SECTOR_SET_FOR_TYPE = {v: k for k, v in SECTOR_SET_TYPE.items()}
DEFAULT_SECTOR_SET = "industry2"

# ── 视图参数 ─────────────────────────────────────────────────────────
DIRECTIONS = ("all", "inflow", "outflow")
TOP_SECTORS_DEFAULT = 10
INFLOW_SHARE = 0.6  # direction=all 时流入/流出名额拆分（10 → 6 流入 + 4 流出）
STOCKS_PER_SECTOR_DEFAULT = 5
STOCKS_PER_SECTOR_MAX = 20
SECTOR_TREND_DAYS = 20  # 板块明细趋势窗口（交易日）
SECTOR_RECENT_DAYS = 5  # 成分行近 N 日逐日涨跌幅窗口（升序日期组）
SECTOR_PCT_MIN_DAYS = 10  # 20 日累计涨幅的最少有效日（停牌/次新不足则显 —）
MIN_BREADTH_MEMBERS = 5  # 广度计算的最小成分样本
MIN_SHARE_DENOM = 1e6  # |板块净额| 低于此值（100 万元）时占比落 null（分母过小不稳定）
MAX_STOCK_DAYS = 60  # 个股详情的最大序列长度
PANORAMA_TTL = 300.0  # 在线聚合缓存 5 分钟（细案 §8，/refresh 时随 feed 缓存清空）

# ── 层级节点 ─────────────────────────────────────────────────────────
MAIN_NODE = "主力资金"
RETAIL_NODE = "散户资金"
SIZE_ORDER = (
    ("超大单", "super"),
    ("大单", "large"),
    ("中单", "medium"),
    ("小单", "small"),
)
SIZE_PARENT = {
    "super": MAIN_NODE,
    "large": MAIN_NODE,
    "medium": RETAIL_NODE,
    "small": RETAIL_NODE,
}

EVIDENCE_DISCLOSURE = [
    "统计口径：板块（行业二级）连续净流入状态下，板块等权组合的前向收益相对全样本基准的超额；"
    "主口径为可执行版（信号盘后可得 → 次一交易日收盘建仓、持有 1/3/5 个交易日），对照列为信号日起算口径；均为前复权价。",
    "板块按当前成分回溯聚合，存在幸存者偏差；全部数字携带样本数与逐年分档，个别年份超额为负如实展示。",
    "历史超额≠未来收益：条件超额是状态的历史分布，不构成对未来的承诺或投资建议；强度为 bp 级。",
    "个股层资金流裸信号已实测不具预测力（RankIC≤0.01），列入「已实测不具预测力」一节，"
    "不参与任何排序、徽章与展示；个体判断以因子概率板与模型为准。",
]

DISCLOSURE = [
    "板块按当前成分回溯聚合，存在幸存者偏差；行业二级覆盖约八成个股，未覆盖个股不计入板块层。",
    "净额为已发生资金流量的描述（背景级），红=净流入、绿=净流出；不构成预测。",
    "「连续净流入」延续证据仅在行业二级板块上测量，携带样本数与分年统计；历史超额≠未来收益。",
]
STOCK_DISCLOSURE = [
    "个股层资金流裸信号经真实数据实测不具预测力（RankIC≤0.01），本页不提供个股资金流预测；"
    "个股预测以因子板/模型为准。",
    "主力=超大单+大单，散户=中单+小单；金额为窗口求和（单位归一后）。",
]

_STOCK_SUFFIX_RE = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")


# ---------------------------------------------------------------------------
# 取数与清洗
# ---------------------------------------------------------------------------


def _norm_date(date: str | None) -> str | None:
    """YYYY-MM-DD / YYYYMMDD → YYYYMMDD；空值原样返回。"""
    if not date:
        return None
    return str(date).replace("-", "").strip() or None


def _dash(raw: str) -> str:
    return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"


def _l2_dates_upto(target: str | None, n: int) -> list[str]:
    """截至 target（含）的最近 n 个有 L2 资金流的分区（降序）。"""
    dates = _get_partition_dates("6_ml_datasets/l2_factors")
    if target:
        dates = [d for d in dates if d <= target]
    return dates[:n]


def _with_derived(flow: pd.DataFrame) -> pd.DataFrame:
    """派生主力/散户净额与成交额（元）。"""
    return flow.assign(
        main=flow["flow_super_net"].fillna(0.0) + flow["flow_large_net"].fillna(0.0),
        retail=flow["flow_medium_net"].fillna(0.0) + flow["flow_small_net"].fillna(0.0),
        amount_y=flow["amount"].fillna(0.0) * 1e4,
    )


def _clean_flow(flow: pd.DataFrame) -> pd.DataFrame:
    """dt 归一为 str + 逐日剔除占位污染行 + 派生列。"""
    df = flow.copy()
    df["dt"] = df["dt"].astype(str)
    bad = placeholder_symbols_by_dt(df)
    if bad:
        bad_sets = df["dt"].map(lambda d: bad.get(d) or ())
        mask = [s in bs for s, bs in zip(df["symbol"], bad_sets, strict=True)]
        df = df[~pd.Series(mask, index=df.index)]
    return _with_derived(df)


def _symbol_sums(flow: pd.DataFrame) -> pd.DataFrame:
    """窗口内按 symbol 求和（金额单位：元）。"""
    if flow.empty:
        return pd.DataFrame()
    cols = [
        "flow_super_net",
        "flow_large_net",
        "flow_medium_net",
        "flow_small_net",
        "flow_buy_amount",
        "flow_sell_amount",
        "main",
        "retail",
        "amount_y",
    ]
    sums = flow.groupby("symbol")[cols].sum()
    return sums.rename(
        columns={
            "flow_super_net": "super",
            "flow_large_net": "large",
            "flow_medium_net": "medium",
            "flow_small_net": "small",
            "flow_buy_amount": "buy",
            "flow_sell_amount": "sell",
        }
    )


def _member_frame(members: pd.DataFrame, stype: str | None = None) -> pd.DataFrame:
    """板块成员（可选按类型过滤），(symbol, sector_name) 去重防重复计数。"""
    if members.empty or "sector_name" not in members.columns:
        return pd.DataFrame(columns=["symbol", "sector_name", "sector_type"])
    m = members[["symbol", "sector_name", "sector_type"]].copy()
    if stype:
        m = m[m["sector_type"] == stype]
    m = m.dropna(subset=["symbol", "sector_name"])
    m["symbol"] = m["symbol"].astype(str)
    m["sector_name"] = m["sector_name"].astype(str).str.strip()
    m = m[(m["symbol"] != "") & (m["sector_name"] != "")]
    return m.drop_duplicates(subset=["symbol", "sector_name"])


def _sector_agg(sym_sums: pd.DataFrame, member_df: pd.DataFrame) -> pd.DataFrame:
    """板块层窗口聚合（index=sector_name）。"""
    j = member_df.merge(sym_sums, left_on="symbol", right_index=True, how="inner")
    if j.empty:
        return pd.DataFrame()
    return j.groupby("sector_name").agg(
        super=("super", "sum"),
        large=("large", "sum"),
        medium=("medium", "sum"),
        small=("small", "sum"),
        main=("main", "sum"),
        retail=("retail", "sum"),
        buy=("buy", "sum"),
        sell=("sell", "sum"),
        amount_y=("amount_y", "sum"),
        members=("symbol", "nunique"),
    )


def _day_sector_stats(
    flow: pd.DataFrame, member_df: pd.DataFrame, latest_day: str, prices: pd.DataFrame
) -> dict[str, dict[str, Any]]:
    """最新交易日的板块广度与成分均涨幅（C 级背景）。"""
    day = flow[flow["dt"] == latest_day]
    out: dict[str, dict[str, Any]] = {}
    if day.empty:
        return out
    j = member_df.merge(day[["symbol", "main"]], on="symbol", how="inner")
    if not j.empty:
        grp = j.groupby("sector_name")["main"]
        for name, s in grp:
            if len(s) >= MIN_BREADTH_MEMBERS:
                out.setdefault(str(name), {})["breadth"] = round(
                    float((s > 0).mean()), 4
                )
    if not prices.empty and "pct_change" in prices.columns:
        p = member_df.merge(prices[["symbol", "pct_change"]], on="symbol", how="inner")
        for name, s in p.groupby("sector_name")["pct_change"]:
            out.setdefault(str(name), {})["pct_change"] = round(
                float(s.mean() or 0.0), 2
            )
    return out


def _f_or_none(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(f) else f


def _ratio(main: float, amount_y: float) -> float | None:
    if not amount_y or amount_y <= 0:
        return None
    return round(main / amount_y, 4)


def _sign(v: float) -> int:
    return 1 if v > 0 else (-1 if v < 0 else 0)


# ---------------------------------------------------------------------------
# 证据上下文（P 级字段的唯一入口）
# ---------------------------------------------------------------------------


def _evidence_context(
    sector_set: str, latest_day: str
) -> tuple[dict[str, dict], dict | None, dict[str, Any]]:
    """(streak_rows, evidence_doc, meta)。非行业二级体系不适用，一律空。"""
    applicable = sector_set == flow_evidence.EVIDENCE_SECTOR_SET
    meta: dict[str, Any] = {
        "applicable": applicable,
        "available": False,
        "trade_date": None,
        "window": None,
        "reason": None,
    }
    if not applicable:
        meta["reason"] = "not_applicable"
        return {}, None, meta
    latest = flow_evidence.load_latest()
    if latest is None:
        meta["reason"] = "not_built"
        return {}, None, meta
    if flow_evidence.norm_trade_date(latest.get("trade_date")) != latest_day:
        meta["reason"] = "stale"
        meta["trade_date"] = latest.get("trade_date")
        return {}, None, meta
    doc = flow_evidence.load_evidence()
    meta.update(
        {
            "available": True,
            "trade_date": latest.get("trade_date"),
            "window": (doc or {}).get("window"),
            "built_at": (doc or {}).get("built_at"),
        }
    )
    return flow_evidence.streak_map(latest), doc, meta


def _badge(
    streak: int | None, evidence_doc: dict | None, applicable: bool
) -> dict | None:
    if not applicable:
        return None
    return flow_evidence.pick_badge(streak, evidence_doc)


# ---------------------------------------------------------------------------
# 桑基图（4 层）
# ---------------------------------------------------------------------------


def _mk_link(src: str, dst: str, net: float) -> dict[str, Any] | None:
    value = abs(net)
    if value < 1.0:  # 元级零头边不画
        return None
    return {
        "source": src,
        "target": dst,
        "value": round(value, 2),
        "net": round(net, 2),
        "sign": _sign(net),
    }


def _select_sectors(agg: pd.DataFrame, direction: str, top_sectors: int) -> list[str]:
    """按方向筛选展示板块：all → 流入 6/流出 4；inflow/outflow → 单向 TopN。"""
    pos = agg[agg["main"] > 0].sort_values("main", ascending=False)
    neg = agg[agg["main"] < 0].sort_values("main", ascending=True)
    if direction == "inflow":
        return [str(s) for s in pos.index[:top_sectors]]
    if direction == "outflow":
        return [str(s) for s in neg.index[:top_sectors]]
    n_in = max(0, min(top_sectors, int(round(top_sectors * INFLOW_SHARE))))
    n_out = max(0, top_sectors - n_in)
    return [str(s) for s in pos.index[:n_in]] + [str(s) for s in neg.index[:n_out]]


def _sankey_graph(
    agg: pd.DataFrame,
    member_df: pd.DataFrame,
    sym_sums: pd.DataFrame,
    names: dict[str, str],
    close_map: dict[str, float],
    pct_map: dict[str, float],
    sector_pct: dict[str, float | None],
    streak_rows: dict[str, dict],
    evidence_doc: dict | None,
    sector_evidence_ok: bool,
    direction: str,
    top_sectors: int,
    stocks_per_sector: int,
) -> dict[str, Any]:
    shown = _select_sectors(agg, direction, top_sectors)
    if not shown:
        return {"nodes": [], "links": [], "shown": []}

    sec_syms = {
        s: set(member_df[member_df["sector_name"] == s]["symbol"]) for s in shown
    }
    all_syms = set().union(*sec_syms.values())
    sub = sym_sums.loc[sym_sums.index.isin(all_syms)]
    totals = {
        c: float(sub[c].sum())
        for c in ("super", "large", "medium", "small", "main", "retail")
    }

    nodes: list[dict[str, Any]] = [
        {
            "name": MAIN_NODE,
            "kind": "entity",
            "key": "main",
            "layer": 1,
            "net": round(totals["super"] + totals["large"], 2),
            "sign": _sign(totals["super"] + totals["large"]),
        },
        {
            "name": RETAIL_NODE,
            "kind": "entity",
            "key": "retail",
            "layer": 1,
            "net": round(totals["medium"] + totals["small"], 2),
            "sign": _sign(totals["medium"] + totals["small"]),
        },
    ]
    links: list[dict[str, Any]] = []
    for label, col in SIZE_ORDER:
        nodes.append(
            {
                "name": label,
                "kind": "size",
                "key": col,
                "layer": 2,
                "net": round(totals[col], 2),
                "sign": _sign(totals[col]),
            }
        )
        link = _mk_link(SIZE_PARENT[col], label, totals[col])
        if link:
            links.append(link)

    for s in shown:
        row = agg.loc[s]
        main = float(row["main"])
        streak_info = streak_rows.get(s) or {}
        streak = streak_info.get("streak")
        streak = (
            int(streak)
            if isinstance(streak, (int, float)) and not pd.isna(streak)
            else None
        )
        nodes.append(
            {
                "name": s,
                "kind": "sector",
                "key": s,
                "layer": 3,
                "net": round(main, 2),
                "net_total": round(main + float(row["retail"]), 2),
                "sign": _sign(main),
                "streak": streak,
                "evidence": _badge(streak, evidence_doc, sector_evidence_ok),
                "members": int(row["members"]),
                "pct_change": sector_pct.get(s),
                "net_ratio": _ratio(main, float(row["amount_y"])),
            }
        )
        for label, col in SIZE_ORDER:
            link = _mk_link(label, s, float(row[col]))
            if link:
                links.append(link)

        ranked = sym_sums.loc[sym_sums.index.isin(sec_syms[s])].sort_values(
            "main", ascending=False
        )
        if ranked.empty:
            continue
        total = len(ranked)
        # 个股层选取：direction=inflow/outflow → 单向 TopK；all → 双侧 6:4——
        # 每板块同时给出「流入排名前 n_in」与「流出排名最深 n_out」两翼
        # （用户口径：到个股的流入/流出排名都要）。短板块自动收敛，
        # 尾翼与头翼重叠时去重保序（头翼在前）。
        if direction == "inflow":
            picked = ranked.head(stocks_per_sector)
        elif direction == "outflow":
            picked = ranked.tail(stocks_per_sector).iloc[::-1]
        else:
            n_in = max(1, int(round(stocks_per_sector * INFLOW_SHARE)))
            n_out = stocks_per_sector - n_in
            picked_in = ranked.head(n_in)
            if n_out <= 0:
                picked = picked_in
            else:
                picked_out = ranked.tail(n_out).iloc[::-1]
                picked_out = picked_out[~picked_out.index.isin(picked_in.index)]
                picked = pd.concat([picked_in, picked_out])
        for sym, srow in picked.iterrows():
            link = _mk_link(
                s, names.get(sym) or _normalize_prefix(sym), float(srow["main"])
            )
            if link is None:
                continue
            links.append(link)
            nodes.append(
                {
                    "name": link["target"],
                    "kind": "stock",
                    "key": _normalize_prefix(sym),
                    "layer": 4,
                    "sector": s,
                    "net": round(float(srow["main"]), 2),
                    "net_total": round(float(srow["main"]) + float(srow["retail"]), 2),
                    "sign": _sign(float(srow["main"])),
                    "inner_rank": int(ranked.index.get_loc(sym)) + 1,
                    "inner_total": total,
                    "close": close_map.get(sym),
                    "pct_change": pct_map.get(sym),
                }
            )

    # ECharts sankey 按 name 标识节点：重名合并为一个节点（同一票出现在多个概念板块时的
    # 正常情形，多条入边共用节点，无环），此处按 name 去重保持结构干净。
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for n in nodes:
        if n["name"] in seen:
            continue
        seen.add(n["name"])
        deduped.append(n)
    return {"nodes": deduped, "links": links, "shown": shown}


# ---------------------------------------------------------------------------
# Panorama 组装
# ---------------------------------------------------------------------------


def _sector_master_rows(
    agg: pd.DataFrame,
    day_stats: dict[str, dict],
    streak_rows: dict[str, dict],
    evidence_doc: dict | None,
    sector_evidence_ok: bool,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for name, r in agg.iterrows():
        main = float(r["main"])
        info = streak_rows.get(str(name)) or {}
        streak = info.get("streak")
        streak = (
            int(streak)
            if isinstance(streak, (int, float)) and not pd.isna(streak)
            else None
        )
        stats = day_stats.get(str(name)) or {}
        rows.append(
            {
                "name": str(name),
                "net_main": round(main, 2),
                "net_total": round(main + float(r["retail"]), 2),
                "net_ratio": _ratio(main, float(r["amount_y"])),
                "breadth": stats.get("breadth"),
                "pct_change": stats.get("pct_change"),
                "members": int(r["members"]),
                "buy": round(float(r["buy"]), 2),
                "sell": round(float(r["sell"]), 2),
                "sign": _sign(main),
                "streak": streak,
                "evidence": _badge(streak, evidence_doc, sector_evidence_ok),
            }
        )
    # 默认排序 = 延续状态驱动（细案 §5）：streak≥2 命中在前 → 连流天数降序 → 净额降序。
    rows.sort(
        key=lambda x: (
            0 if (x["streak"] or 0) >= 2 else 1,
            -(x["streak"] or 0),
            -x["net_main"],
        )
    )
    return rows


def _empty_panorama(
    sector_set: str, period: str, direction: str, reason: str
) -> dict[str, Any]:
    return {
        "available": False,
        "trade_date": None,
        "period": period,
        "sector_set": sector_set,
        "direction": direction,
        "nodes": [],
        "links": [],
        "sectors": [],
        "meta": {"reason": reason, "disclosure": DISCLOSURE},
    }


def get_panorama(
    period: str = "1d",
    sector_set: str = DEFAULT_SECTOR_SET,
    top_sectors: int = TOP_SECTORS_DEFAULT,
    stocks_per_sector: int = STOCKS_PER_SECTOR_DEFAULT,
    direction: str = "all",
    date: str | None = None,
) -> dict[str, Any]:
    """资金流全景（桑基 v2 + 板块 master 榜）。参数非法时收敛到默认值。"""
    if sector_set not in SECTOR_SET_TYPE:
        sector_set = DEFAULT_SECTOR_SET
    if direction not in DIRECTIONS:
        direction = "all"
    period = str(period or "1d").lower()
    if period not in PERIOD_DAYS:
        period = "1d"
    top_sectors = max(2, min(int(top_sectors or TOP_SECTORS_DEFAULT), 30))
    stocks_per_sector = max(
        1,
        min(int(stocks_per_sector or STOCKS_PER_SECTOR_DEFAULT), STOCKS_PER_SECTOR_MAX),
    )
    target = _norm_date(date)
    key = f"flow_panorama_{period}_{sector_set}_{top_sectors}_{stocks_per_sector}_{direction}_{target or 'latest'}"
    return _cached(
        key,
        PANORAMA_TTL,
        lambda: _panorama_impl(
            period, sector_set, top_sectors, stocks_per_sector, direction, target
        ),
    )


def _panorama_impl(
    period: str,
    sector_set: str,
    top_sectors: int,
    stocks_per_sector: int,
    direction: str,
    target: str | None,
) -> dict[str, Any]:
    if not _available():
        return _empty_panorama(sector_set, period, direction, "quantdb_unavailable")
    n_days = PERIOD_DAYS[period]
    days = _l2_dates_upto(target, n_days)
    if not days or (target and days[0] != target):
        return _empty_panorama(
            sector_set, period, direction, "date_not_found" if target else "no_l2_data"
        )
    flow = _clean_flow(_load_l2_flow(days))
    if flow.empty:
        return _empty_panorama(sector_set, period, direction, "no_flow_data")

    members = _sector_members()
    member_df = _member_frame(members, SECTOR_SET_TYPE[sector_set])
    if member_df.empty:
        return _empty_panorama(sector_set, period, direction, "sector_set_empty")

    prices = _load_prices([days[0]])
    names = _instrument_names()
    close_map: dict[str, float] = {}
    pct_map: dict[str, float] = {}
    if not prices.empty:
        for r in prices.itertuples(index=False):
            c = _f_or_none(r.close)
            p = _f_or_none(r.pct_change)
            if c is not None:
                close_map[str(r.symbol)] = round(c, 2)
            if p is not None:
                pct_map[str(r.symbol)] = round(p, 2)

    sym_sums = _symbol_sums(flow)
    agg = _sector_agg(sym_sums, member_df)
    if agg.empty:
        return _empty_panorama(sector_set, period, direction, "no_flow_data")

    streak_rows, evidence_doc, ev_meta = _evidence_context(sector_set, days[0])
    sector_evidence_ok = ev_meta.get("available", False)

    day_stats = _day_sector_stats(flow, member_df, days[0], prices)
    sector_pct = {k: v.get("pct_change") for k, v in day_stats.items()}
    sectors = _sector_master_rows(
        agg, day_stats, streak_rows, evidence_doc, sector_evidence_ok
    )
    graph = _sankey_graph(
        agg,
        member_df,
        sym_sums,
        names,
        close_map,
        pct_map,
        sector_pct,
        streak_rows,
        evidence_doc,
        sector_evidence_ok,
        direction,
        top_sectors,
        stocks_per_sector,
    )

    covered = len(set(sym_sums.index) & set(member_df["symbol"]))
    total_syms = int(len(sym_sums))
    meta = {
        "unit": "yuan",
        "sector_set": sector_set,
        "sector_set_label": SECTOR_SET_LABEL[sector_set],
        "sector_type": SECTOR_SET_TYPE[sector_set],
        "period": period,
        "window_days": [days[-1], days[0]],
        "direction": direction,
        "coverage": {
            "flow_symbols": total_syms,
            "covered_symbols": covered,
            "covered_pct": round(covered / total_syms * 100, 1) if total_syms else None,
        },
        "evidence": ev_meta,
        "disclosure": DISCLOSURE,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
    return {
        "available": True,
        "trade_date": _dash(days[0]),
        "trade_date_raw": days[0],
        "period": period,
        "sector_set": sector_set,
        "direction": direction,
        "stocks_per_sector": stocks_per_sector,
        "top_sectors": top_sectors,
        "nodes": graph["nodes"],
        "links": graph["links"],
        "sectors": sectors,
        "meta": meta,
    }


# ---------------------------------------------------------------------------
# 板块明细（master-detail 的 detail 面）
# ---------------------------------------------------------------------------


def get_sector_detail(
    name: str,
    sector_set: str = DEFAULT_SECTOR_SET,
    period: str = "1d",
    top: int = 50,
    date: str | None = None,
) -> dict[str, Any] | None:
    """板块明细 + 成分贡献（找不到板块返回 None）。"""
    if sector_set not in SECTOR_SET_TYPE:
        sector_set = DEFAULT_SECTOR_SET
    period = str(period or "1d").lower()
    if period not in PERIOD_DAYS:
        period = "1d"
    top = max(1, min(int(top or 50), 200))
    target = _norm_date(date)
    key = f"flow_sector_{name}_{sector_set}_{period}_{top}_{target or 'latest'}"
    return _cached(
        key,
        PANORAMA_TTL,
        lambda: _sector_detail_impl(
            str(name or "").strip(), sector_set, period, top, target
        ),
    )


def _sector_detail_impl(
    name: str, sector_set: str, period: str, top: int, target: str | None
) -> dict[str, Any] | None:
    if not name or not _available():
        return None
    n_days = PERIOD_DAYS[period]
    days = _l2_dates_upto(target, max(n_days, SECTOR_TREND_DAYS))
    if not days or (target and days[0] != target):
        return None

    member_df = _member_frame(_sector_members(), SECTOR_SET_TYPE[sector_set])
    sec_members = (
        member_df[member_df["sector_name"] == name]["symbol"].unique().tolist()
    )
    if not sec_members:
        return None

    flow = _clean_flow(_load_l2_flow(days))
    if flow.empty:
        return None
    sec_set = set(sec_members)
    window = set(days[:n_days])
    flow_w = flow[flow["dt"].isin(window) & flow["symbol"].isin(sec_set)]
    sym_sums = _symbol_sums(flow_w)
    if sym_sums.empty:
        return None
    totals = sym_sums.sum(numeric_only=True)
    main_total = float(totals["main"])

    # 趋势（升序，亿元）：窗口内逐日板块主力净额与净额比
    flow_sec = flow[flow["symbol"].isin(sec_set)]
    trend_days = sorted(days[:SECTOR_TREND_DAYS])
    trend: list[dict[str, Any]] = []
    if not flow_sec.empty:
        by_day = flow_sec.groupby("dt").agg(
            main=("main", "sum"), amount_y=("amount_y", "sum")
        )
        for d in trend_days:
            if d not in by_day.index:
                continue
            r = by_day.loc[d]
            trend.append(
                {
                    "date": _dash(d),
                    "net_main": round(float(r["main"]) / 1e8, 4),
                    "net_ratio": _ratio(float(r["main"]), float(r["amount_y"])),
                }
            )

    recent_days = sorted(days[:SECTOR_RECENT_DAYS])
    # 价格窗一次读满 20 日：近 5 日逐日涨幅与 20 日累计涨幅共用（20 日窗 ⊇ 5 日窗）。
    pct_days = sorted(days[:SECTOR_TREND_DAYS])
    prices = _load_prices(pct_days)
    # _day_sector_stats 按 symbol 直接 merge 价格：多日价格会把均值跨日放大，
    # 广度/成分均涨幅只准用当日切片；近 N 日序列另行逐日对齐（见下）。
    price_today = prices[prices["dt"] == days[0]] if not prices.empty else prices
    day_stats = _day_sector_stats(flow, member_df, days[0], price_today).get(name) or {}
    names = _instrument_names()
    streak_rows, evidence_doc, ev_meta = _evidence_context(sector_set, days[0])
    info = streak_rows.get(name) or {}
    streak = info.get("streak")
    streak = (
        int(streak)
        if isinstance(streak, (int, float)) and not pd.isna(streak)
        else None
    )

    ranked = sym_sums.sort_values("main", ascending=False)
    # 近 N 日逐日涨跌幅：symbol → {raw_dt: pct|null}；无价不漏行（对齐统一日期组）
    recent_pct: dict[str, dict[str, float | None]] = {}
    if not prices.empty and "pct_change" in prices.columns:
        pv = prices[prices["symbol"].isin(sec_set)]
        for pr in pv.itertuples(index=False):
            recent_pct.setdefault(str(pr.symbol), {})[str(pr.dt)] = _f_or_none(
                pr.pct_change
            )
    # 近 20 交易日累计涨幅：官方逐日涨跌幅复利连乘（= 区间收盘比，官方口径已含
    # 除权调整，对复权断点免疫）；有效日 < SECTOR_PCT_MIN_DAYS（停牌/次新）不给值
    # 显 —，绝不落 0。
    pct20: dict[str, float | None] = {}
    for sym, by_day in recent_pct.items():
        vals = [v for v in (by_day.get(d) for d in pct_days) if v is not None]
        if len(vals) < SECTOR_PCT_MIN_DAYS:
            pct20[sym] = None
            continue
        acc = 1.0
        for v in vals:
            acc *= 1.0 + v / 100.0
        pct20[sym] = round((acc - 1.0) * 100.0, 2)
    member_rows: list[dict[str, Any]] = []
    for sym, r in ranked.iterrows():
        m = float(r["main"])
        member_rows.append(
            {
                "symbol": _normalize_prefix(sym),
                "name": names.get(sym, ""),
                "net_main": round(m, 2),
                "net_total": round(m + float(r["retail"]), 2),
                "super": round(float(r["super"]), 2),
                "large": round(float(r["large"]), 2),
                "medium": round(float(r["medium"]), 2),
                "small": round(float(r["small"]), 2),
                "share": round(m / main_total, 4)
                if abs(main_total) >= MIN_SHARE_DENOM
                else None,
                "inner_rank": int(ranked.index.get_loc(sym)) + 1,
                "inner_total": int(len(ranked)),
                "recent": [
                    {"date": _dash(d), "pct": recent_pct.get(sym, {}).get(d)}
                    for d in recent_days
                ],
                "pct_20d": pct20.get(sym),
            }
        )
    member_rows.sort(key=lambda x: abs(x["net_main"]), reverse=True)
    member_rows = member_rows[:top]

    return {
        "available": True,
        "name": name,
        "sector_set": sector_set,
        "sector_set_label": SECTOR_SET_LABEL[sector_set],
        "period": period,
        "trade_date": _dash(days[0]),
        "trade_date_raw": days[0],
        "summary": {
            "net_main": round(main_total, 2),
            "net_total": round(main_total + float(totals["retail"]), 2),
            "net_ratio": _ratio(main_total, float(totals["amount_y"])),
            "super": round(float(totals["super"]), 2),
            "large": round(float(totals["large"]), 2),
            "medium": round(float(totals["medium"]), 2),
            "small": round(float(totals["small"]), 2),
            "buy": round(float(totals["buy"]), 2),
            "sell": round(float(totals["sell"]), 2),
            "breadth": day_stats.get("breadth"),
            "pct_change": day_stats.get("pct_change"),
            "members": int(len(sec_members)),
            "data_symbols": int(len(sym_sums)),
            "streak": streak,
            "evidence": _badge(streak, evidence_doc, ev_meta.get("available", False)),
        },
        "trend": trend,
        "members": member_rows,
        "meta": {
            "unit": "yuan",
            "window_days": [days[min(n_days, len(days)) - 1], days[0]],
            "evidence": ev_meta,
            "disclosure": DISCLOSURE,
        },
    }


# ---------------------------------------------------------------------------
# 个股明细（资金流「返回板块」的双向面）
# ---------------------------------------------------------------------------

_TYPE_PRIORITY = {
    "行业板块(二级)": 0,
    "行业板块(一级)": 1,
    "概念板块": 2,
    "地区板块": 3,
    "风格板块": 4,
}


def get_stock_detail(
    symbol: str, days: int = 20, date: str | None = None
) -> dict[str, Any] | None:
    """个股 20 日五档序列 + 板块归因（非法/未知代码返回 None）。"""
    days = max(1, min(int(days or 20), MAX_STOCK_DAYS))
    target = _norm_date(date)
    key = f"flow_stock_{symbol}_{days}_{target or 'latest'}"
    return _cached(
        key,
        PANORAMA_TTL,
        lambda: _stock_detail_impl(str(symbol or "").strip(), days, target),
    )


def _stock_detail_impl(
    symbol: str, days: int, target: str | None
) -> dict[str, Any] | None:
    suffix = StockCodeUtil.to_suffix(symbol) if symbol else ""
    if not _STOCK_SUFFIX_RE.match(suffix or ""):
        return None
    if not _available():
        return None
    load_n = max(days, SECTOR_TREND_DAYS)
    days_list = _l2_dates_upto(target, load_n)
    if not days_list or (target and days_list[0] != target):
        return None

    flow = _clean_flow(_load_l2_flow(days_list))
    if flow.empty:
        return None
    sym_flow = flow[flow["symbol"] == suffix].sort_values("dt")
    if sym_flow.empty:
        return None

    prices = _load_prices(days_list)
    price_by_dt: dict[str, tuple[float | None, float | None]] = {}
    if not prices.empty and "symbol" in prices.columns:
        pr = prices[prices["symbol"] == suffix]
        for r in pr.itertuples(index=False):
            c = _f_or_none(r.close)
            price_by_dt[str(r.dt)] = (
                round(c, 2) if c is not None else None,
                _f_or_none(r.pct_change),
            )
    names = _instrument_names()

    # 事后 h 日涨跌：close(t)→close(t+h)，仅历史回看的展示字段，不参与信号。
    # 最新几天自然没有未来价 → null；不向前填充、不回落成 0。
    fwd_map: dict[str, dict[str, float | None]] = {}
    series_dates = sorted(days_list[:days])
    if series_dates:
        fwd_dates_desc = [
            d
            for d in _get_partition_dates("1_kline_data/daily_forward")
            if d >= series_dates[0]
        ][: len(series_dates) + 25]
        if fwd_dates_desc:
            fw = _read_partitioned(
                "1_kline_data/daily_forward", fwd_dates_desc, "symbol, close"
            )
            if not fw.empty and "symbol" in fw.columns:
                fw = fw[fw["symbol"] == suffix].copy()
                fw["dt"] = fw["dt"].astype(str)
                asc = sorted(fwd_dates_desc)
                close_seq = fw.set_index("dt")["close"].reindex(asc)
                vals = close_seq.to_numpy(dtype=float)
                pos = {d: i for i, d in enumerate(asc)}
                for d in series_dates:
                    i = pos.get(d)
                    if i is None or not np.isfinite(vals[i]) or vals[i] <= 0:
                        continue
                    item: dict[str, float | None] = {}
                    for h in (5, 10, 20):
                        j = i + h
                        item[f"fwd_{h}d"] = (
                            round(float(vals[j] / vals[i] - 1.0) * 100, 2)
                            if j < len(vals) and np.isfinite(vals[j])
                            else None
                        )
                    fwd_map[d] = item

    # 序列（升序，亿元；缺失日落 null 不落 0）
    flow_by_dt = sym_flow.set_index("dt")
    series: list[dict[str, Any]] = []
    for d in sorted(list(days_list)[:days]):
        close, pct = price_by_dt.get(d, (None, None))
        if d in flow_by_dt.index:
            r = flow_by_dt.loc[d]
            series.append(
                {
                    "date": _dash(d),
                    "super": round(float(r["flow_super_net"]) / 1e8, 4),
                    "large": round(float(r["flow_large_net"]) / 1e8, 4),
                    "medium": round(float(r["flow_medium_net"]) / 1e8, 4),
                    "small": round(float(r["flow_small_net"]) / 1e8, 4),
                    "main": round(float(r["main"]) / 1e8, 4),
                    "retail": round(float(r["retail"]) / 1e8, 4),
                    "net_ratio": _ratio(float(r["main"]), float(r["amount_y"])),
                    "close": close,
                    "pct_change": pct,
                    **fwd_map.get(d, {}),
                }
            )
        elif close is not None:
            series.append(
                {
                    "date": _dash(d),
                    "super": None,
                    "large": None,
                    "medium": None,
                    "small": None,
                    "main": None,
                    "retail": None,
                    "net_ratio": None,
                    "close": close,
                    "pct_change": pct,
                    **fwd_map.get(d, {}),
                }
            )

    # 窗口汇总（1/5/20 日，元）
    sym_sums_all = _symbol_sums(flow)
    windows: dict[str, Any] = {}
    for key, k in (("1d", 1), ("5d", 5), ("20d", 20)):
        sub = sym_flow[sym_flow["dt"].isin(set(days_list[:k]))]
        if sub.empty:
            windows[key] = None
            continue
        agg_row = _symbol_sums(sub)
        if agg_row.empty:
            windows[key] = None
            continue
        r = agg_row.iloc[0]
        windows[key] = {
            "net_main": round(float(r["main"]), 2),
            "net_total": round(float(r["main"]) + float(r["retail"]), 2),
            "net_ratio": _ratio(float(r["main"]), float(r["amount_y"])),
            "days": int(min(k, len(days_list))),
        }

    # 板块归因（「个股资金流返回板块」）：所属板块 + 板块内 rank + 贡献占比
    streak_rows, evidence_doc, ev_meta = _evidence_context(
        flow_evidence.EVIDENCE_SECTOR_SET, days_list[0]
    )
    ok = ev_meta.get("available", False)
    attribution = _stock_attribution(
        suffix, sym_sums_all, streak_rows, evidence_doc, ok
    )
    stock_main = float(sym_flow["main"].sum())
    for item in attribution:
        item["stock_net_main"] = round(stock_main, 2)

    latest_close, latest_pct = price_by_dt.get(days_list[0], (None, None))
    return {
        "available": True,
        "symbol": _normalize_prefix(suffix),
        "suffix": suffix,
        "name": names.get(suffix, ""),
        "trade_date": _dash(days_list[0]),
        "trade_date_raw": days_list[0],
        "close": latest_close,
        "pct_change": latest_pct,
        "series": series,
        "windows": windows,
        "attribution": attribution,
        "factor": _factor_board_snapshot(suffix, days_list[0]),
        "meta": {
            "unit": "yuan",
            "series_unit": "yi",
            "window_days": [days_list[min(days, len(days_list)) - 1], days_list[0]],
            "disclosure": STOCK_DISCLOSURE,
            "forward_note": "fwd_5d/10d/20d = close(t)→close(t+h)，仅事后回看展示，不参与信号",
        },
    }


def _factor_board_snapshot(suffix: str, flow_date_raw: str) -> dict[str, Any] | None:
    """个股「因子板共振态」：读因子概率板 members 伴生件（P3-③）。

    定位用行业二级体系——一只票恰属一个二级行业，概念多对多不适合做「所属行业」。
    数据源是产板伴生件 ``members/latest.json``（``backend/shared/factor_board``），
    缺失/损坏一律返回 None：个股资金流主路径**绝不因因子板缺席而失败**。

    ``stale_days`` = 资金流交易日 − 板决策日（正 = 板落后于行情；口径与
    ``factor_board.consume._staleness`` 一致，阈值同为 ``OVERLAY_STALE_DAYS``）。
    超阈或日期不可解析 → ``stale=True``，展示层转陈旧态；绝不用陈旧板冒充当日。
    """
    try:
        from backend.shared.factor_board import board_io as fb_io
        from backend.shared.factor_board import config as fb_config
        from backend.shared.factor_board.consume import OVERLAY_STALE_DAYS

        index = fb_io.load_members_index(fb_config.resolve_board_dir())
        if not isinstance(index, dict):
            return None
        sectors = index.get("sectors")
        l2 = sectors.get("industry_l2") if isinstance(sectors, dict) else None
        hit_sector: str | None = None
        hit_row: dict[str, Any] | None = None
        if isinstance(l2, dict):
            for sector_name, rows in l2.items():
                if not isinstance(rows, list):
                    continue
                for row in rows:
                    if isinstance(row, dict) and str(row.get("symbol")) == suffix:
                        hit_sector, hit_row = str(sector_name), row
                        break
                if hit_row is not None:
                    break

        decision = str(index.get("decision_date") or "").strip() or None
        stale_days: int | None = None
        if decision and flow_date_raw:
            try:
                d_flow = _date.fromisoformat(_dash(str(flow_date_raw)))
                d_decision = _date.fromisoformat(decision)
                stale_days = (d_flow - d_decision).days
            except ValueError:
                stale_days = None
        stale = stale_days is None or stale_days > OVERLAY_STALE_DAYS

        return {
            "available": hit_row is not None,
            "decision_date": decision,
            "trade_date": index.get("trade_date"),
            "stale_days": stale_days,
            "stale": stale,
            "sector_l2": hit_sector,
            "quadrant": None if hit_row is None else hit_row.get("quadrant"),
            "composite": None if hit_row is None else hit_row.get("composite"),
            "model_rank": None if hit_row is None else hit_row.get("model_rank"),
        }
    except Exception:  # pragma: no cover - 兜底：因子板是增强面，绝不拦主路径
        logger.exception("[flow-panorama] 因子板快照读取失败（按缺失处理）")
        return None


def _stock_attribution(
    suffix: str,
    sym_sums_all: pd.DataFrame,
    streak_rows: dict[str, dict],
    evidence_doc: dict | None,
    sector_evidence_ok: bool,
) -> list[dict[str, Any]]:
    """该股所在的全部板块（各体系）+ 板块窗口净额 + 板块内 rank/贡献占比。"""
    member_df = _member_frame(_sector_members())
    if member_df.empty:
        return []
    mine = member_df[member_df["symbol"] == suffix]
    if mine.empty:
        return []

    j = member_df.merge(sym_sums_all, left_on="symbol", right_index=True, how="inner")
    if j.empty:
        return []
    # 不可写作 dict(j.groupby(...))：DataFrameGroupBy.keys 是 list 属性，
    # dict() 会按 mapping 调 keys() 而 TypeError（ruff C416 自动修复会踩此坑）。
    grouped: dict[Any, pd.DataFrame] = {}
    for key, grp in j.groupby(["sector_type", "sector_name"]):
        grouped[key] = grp

    out: list[dict[str, Any]] = []
    for r in mine.itertuples(index=False):
        grp = grouped.get((r.sector_type, r.sector_name))
        if grp is None or grp.empty:
            continue
        sector_main = float(grp["main"].sum())
        my_row = grp[grp["symbol"] == suffix]
        if my_row.empty:
            continue
        my_main = float(my_row["main"].iloc[0])
        rank = int((grp["main"] > my_main).sum()) + 1
        streak = None
        evidence = None
        if r.sector_type == SECTOR_SET_TYPE[flow_evidence.EVIDENCE_SECTOR_SET]:
            info = streak_rows.get(str(r.sector_name)) or {}
            st = info.get("streak")
            streak = (
                int(st) if isinstance(st, (int, float)) and not pd.isna(st) else None
            )
            evidence = _badge(streak, evidence_doc, sector_evidence_ok)
        out.append(
            {
                "name": str(r.sector_name),
                "type": str(r.sector_type),
                "sector_set": SECTOR_SET_FOR_TYPE.get(str(r.sector_type)),
                "net_main": round(sector_main, 2),
                "share": round(my_main / sector_main, 4)
                if abs(sector_main) >= MIN_SHARE_DENOM
                else None,
                "inner_rank": rank,
                "inner_total": int(len(grp)),
                "streak": streak,
                "evidence": evidence,
            }
        )
    out.sort(key=lambda x: (_TYPE_PRIORITY.get(x["type"], 9), -abs(x["net_main"])))
    return out


# ---------------------------------------------------------------------------
# 证据台（细案 v2 §6：命中榜 + 信号档案 + 否决清单）
# ---------------------------------------------------------------------------


def _dash_any(raw: Any) -> str | None:
    """裸日期/ISO 日期 → ISO；空值 → None。"""
    s = flow_evidence.norm_trade_date(raw)
    if not s:
        return None
    return _dash(s) if len(s) == 8 and s.isdigit() else s


def _streak_of(v: Any) -> int | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(f) else int(f)


def get_evidence(date: str | None = None) -> dict[str, Any]:
    """证据台：命中榜 + 信号档案（过闸 / 否决两节），只读离线产物。

    ``date`` 为空 → 命中榜取 ``latest.json``（经新鲜度门：落后于最新 L2 分区
    即 ``hits_state=stale``，不用陈旧 streak 冒充当日）；``date`` 为快照日 →
    命中榜从 ``panel_sector.parquet`` 该日回溯（跨日状态可取历史值）。
    条件统计（conditions）为全窗信号档案，与其新鲜度无关，恒定随档案返回。
    """
    target = _norm_date(date)
    key = f"flow_evidence_{target or 'latest'}"
    return _cached(key, PANORAMA_TTL, lambda: _evidence_impl(target))


def _evidence_hits(
    target: str | None, doc: dict[str, Any]
) -> tuple[list[dict[str, Any]], str | None, str]:
    """(命中榜行, 命中榜交易日 ISO, 状态)。状态：ok / stale / no_rows / unbuilt。"""
    if target:
        panel = flow_evidence.load_sector_panel()
        if panel is None or panel.empty or "dt" not in panel.columns:
            return [], _dash_any(target), "no_rows"
        rows = panel[panel["dt"].astype(str) == target]
        if rows.empty:
            return [], _dash_any(target), "no_rows"
        hits: list[dict[str, Any]] = []
        for r in rows.itertuples(index=False):
            st = _streak_of(getattr(r, "streak", None))
            if st is None or st < flow_evidence.BADGE_LEVELS[-1]:
                continue
            nm = _f_or_none(getattr(r, "net_main", None))
            nr = _f_or_none(getattr(r, "net_ratio", None))
            bd = _f_or_none(getattr(r, "breadth", None))
            hits.append(
                {
                    "name": str(r.sector),
                    "streak": st,
                    "net_main": round(nm, 2) if nm is not None else None,
                    "net_ratio": round(nr, 4) if nr is not None else None,
                    "breadth": round(bd, 4) if bd is not None else None,
                    "members": int(getattr(r, "members", 0) or 0),
                    "evidence": flow_evidence.pick_badge(st, doc),
                }
            )
        hits.sort(key=lambda x: (-x["streak"], -(x["net_main"] or 0.0)))
        return hits, _dash_any(target), "ok"

    latest = flow_evidence.load_latest()
    if latest is None:
        return [], None, "unbuilt"
    l2_dates = _get_partition_dates("6_ml_datasets/l2_factors")
    fresh = bool(l2_dates) and (
        flow_evidence.norm_trade_date(latest.get("trade_date")) == l2_dates[0]
    )
    if not fresh:
        return [], _dash_any(latest.get("trade_date")), "stale"
    hits = []
    for name, row in flow_evidence.streak_map(latest).items():
        st = _streak_of(row.get("streak"))
        if st is None or st < flow_evidence.BADGE_LEVELS[-1]:
            continue
        hits.append(
            {
                "name": name,
                "streak": st,
                "net_main": _f_or_none(row.get("net_main")),
                "net_ratio": _f_or_none(row.get("net_ratio")),
                "breadth": _f_or_none(row.get("breadth")),
                "members": int(row.get("members") or 0),
                "evidence": flow_evidence.pick_badge(st, doc),
            }
        )
    hits.sort(key=lambda x: (-x["streak"], -(x["net_main"] or 0.0)))
    return hits, _dash_any(latest.get("trade_date")), "ok"


def _evidence_impl(target: str | None) -> dict[str, Any]:
    doc = flow_evidence.load_evidence()
    if doc is None:
        return {
            "available": False,
            "reason": "not_built",
            "requested_date": _dash_any(target),
            "trade_date": None,
            "built_at": None,
            "window": None,
            "min_cond_n": flow_evidence.MIN_COND_N,
            "conventions": {},
            "gate_criteria": None,
            "conditions": {},
            "rejected": [],
            "hits": [],
            "hits_trade_date": None,
            "hits_state": "unbuilt",
            "meta": {"disclosure": EVIDENCE_DISCLOSURE},
        }
    hits, hits_trade_date, hits_state = _evidence_hits(target, doc)
    return {
        "available": True,
        "reason": None,
        "requested_date": _dash_any(target),
        "trade_date": _dash_any(doc.get("trade_date")),
        "trade_date_raw": flow_evidence.norm_trade_date(doc.get("trade_date")),
        "built_at": doc.get("built_at"),
        "window": doc.get("window"),
        "min_cond_n": doc.get("min_cond_n", flow_evidence.MIN_COND_N),
        "badge_levels": doc.get("badge_levels"),
        "badge_horizon": doc.get("badge_horizon"),
        "conventions": doc.get("conventions") or {},
        "gate_criteria": doc.get("gate_criteria"),
        "coverage": doc.get("coverage"),
        "conditions": doc.get("conditions") or {},
        "rejected": doc.get("rejected") or [],
        "hits": hits,
        "hits_trade_date": hits_trade_date,
        "hits_state": hits_state,
        "meta": {
            "evidence_trade_date": _dash_any(doc.get("trade_date")),
            "evidence_window": doc.get("window"),
            "disclosure": EVIDENCE_DISCLOSURE,
        },
    }
