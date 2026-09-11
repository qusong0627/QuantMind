"""
Market analysis snapshot compute — 移植自 quantmind/website 的离线快照方案，适配当前 QuantDB schema。

原理：
  D:\\quant_data (通达信 parquet, dt 分区) --DuckDB+适配层--> JSON 快照 + SQLite 标签库
  服务器只读快照，实现"计算与托管分离"、按日归档、?date= 历史回查。

数据口径（与 quantmindoss/electron 的 quantdb_feed 一致）：
  - pct_change 为百分点点口径（涨停=10）；标的 ID 统一小写后缀（000001.SZ）。
  - 真实资金流来自 l2_factors 的 flow_* 列。

用法：
  python backend/scripts/market_snapshot/compute.py [--data-dir D:\\quant_data] [--out data/market-analysis]
  python backend/scripts/market_snapshot/compute.py --ref-date 2026-08-25   # 历史回补单日
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

# 允许直接从仓库根运行（python backend/scripts/market_snapshot/compute.py）
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import duckdb  # noqa: F401,E402 (schema_adapter 依赖)
import pandas as pd

from backend.scripts.market_snapshot.schema_adapter import (
    DATASET_DIRS,
    DEFAULT_DATA_DIR,
    get_conn,
    q as _q,
)

DEFAULT_OUT_DIR = (
    Path(os.getenv("QM_MARKET_SNAPSHOT_DIR", "")).resolve()
    if os.getenv("QM_MARKET_SNAPSHOT_DIR")
    else Path(os.getcwd()) / "data" / "market-analysis"
)

INDEX_OVERVIEW = [
    {"symbol": "000001.SH", "name": "上证指数"},
    {"symbol": "399001.SZ", "name": "深证成指"},
    {"symbol": "399006.SZ", "name": "创业板指"},
    {"symbol": "000300.SH", "name": "沪深300"},
    {"symbol": "000688.SH", "name": "科创50"},
]

PERIOD_DAYS = {"1d": 1, "3d": 3, "5d": 5, "10d": 10, "20d": 20}

# 历史回补锚定：--ref-date 设置时覆盖「取最新交易日」；默认 None。
_REF_OVERRIDE: str | None = None


# ---------- 分区/交易日 ----------

def _get_partition_dates(data_dir: Path, rel_path: str) -> list[str]:
    dd = data_dir / rel_path
    if not dd.is_dir():
        return []
    dates = []
    for entry in dd.iterdir():
        if entry.is_dir() and entry.name.startswith("dt="):
            val = entry.name.split("=", 1)[1]
            if val.isdigit():
                dates.append(val)
    return sorted(dates, reverse=True)


def _latest_trade_date(data_dir: Path) -> str | None:
    dates = _get_partition_dates(data_dir, DATASET_DIRS["daily_unadjusted"])
    if not dates:
        return None
    if _REF_OVERRIDE:
        return _REF_OVERRIDE if _REF_OVERRIDE in dates else None
    return dates[0]


def _latest_l2_date(data_dir: Path) -> str | None:
    dates = _get_partition_dates(data_dir, DATASET_DIRS["l2_factors"])
    if not dates:
        return None
    if _REF_OVERRIDE:
        return _REF_OVERRIDE if _REF_OVERRIDE in dates else None
    return dates[0]


def _trading_days(data_dir: Path, end: str | None, n: int) -> list[str]:
    dates = _get_partition_dates(data_dir, DATASET_DIRS["daily_unadjusted"])
    if not dates:
        return []
    if end:
        dates = [d for d in dates if d <= end]
    return dates[:n]


# ---------- 数据加载（基于归一视图） ----------

def _load_l2_flow(con, days: list[str]) -> pd.DataFrame:
    if not days:
        return pd.DataFrame()
    dt_in = ", ".join(f"'{d}'" for d in days)
    return _q(
        con,
        "SELECT symbol, dt, flow_net_amount, flow_buy_amount, flow_sell_amount, flow_net_ratio, "
        "flow_super_net, flow_large_net, flow_medium_net, flow_small_net, "
        "flow_large_ratio, flow_medium_ratio, flow_small_ratio, flow_money_flow_index "
        f"FROM qdb_l2_factors WHERE dt IN ({dt_in})",
    )


def _load_prices(con, days: list[str]) -> pd.DataFrame:
    if not days:
        return pd.DataFrame()
    dt_in = ", ".join(f"'{d}'" for d in days)
    k = _q(con, f"SELECT symbol, dt, close FROM qdb_daily_unadjusted WHERE dt IN ({dt_in})")
    if k.empty:
        return k
    k["dt"] = k["dt"].astype(str)
    t = _q(con, f"SELECT symbol, dt, pct_change FROM qdb_technical_indicators WHERE dt IN ({dt_in})")
    if not t.empty:
        t["dt"] = t["dt"].astype(str)
        k = k.merge(t, on=["symbol", "dt"], how="left")
    if "pct_change" not in k.columns:
        k["pct_change"] = 0.0
    return k


def _market_pct_snapshot(con, data_dir: Path) -> tuple[str | None, pd.DataFrame]:
    latest = _latest_trade_date(data_dir)
    if not latest:
        return None, pd.DataFrame()
    days = _trading_days(data_dir, latest, 2)
    if len(days) < 2:
        return days[0] if days else None, pd.DataFrame()
    dt_in = ", ".join(f"'{d}'" for d in days[:2])
    k = _q(con, f"SELECT symbol, dt, close, amount FROM qdb_daily_unadjusted WHERE dt IN ({dt_in})")
    if k.empty:
        return days[0], pd.DataFrame()
    k["dt"] = k["dt"].astype(str)
    cur_day = days[0]
    snap = k[k["dt"] == cur_day][["symbol", "close", "amount"]].copy()
    if snap.empty:
        return cur_day, pd.DataFrame()
    p = k.pivot_table(index="symbol", columns="dt", values="close")
    cols = list(p.columns)
    if len(cols) >= 2 and cols[-1] == cur_day:
        calc = ((p[cols[-1]] / p[cols[-2]] - 1) * 100).rename("pct_calc").rename_axis("symbol").reset_index()
        snap = snap.merge(calc, on="symbol", how="left")
        off = _q(con, f"SELECT symbol, pct_change FROM qdb_technical_indicators WHERE dt = {cur_day}")
        if not off.empty:
            snap = snap.merge(off, on="symbol", how="left")
            snap["pct_change"] = snap["pct_change"].where(snap["pct_change"].notna(), snap["pct_calc"])
        else:
            snap["pct_change"] = snap["pct_calc"]
        snap = snap.drop(columns=["pct_calc"])
    else:
        snap["pct_change"] = 0.0
    return cur_day, snap


# ---------- 工具 ----------

def _normalize_prefix(symbol: str) -> str:
    s = str(symbol).strip().upper()
    if "." in s:
        code, suffix = s.split(".", 1)
        if suffix in ("SH", "SZ", "BJ"):
            return f"{suffix}{code}"
        return s.replace(".", "")
    if s.startswith(("SH", "SZ", "BJ")):
        return s
    if s[:1] in ("6", "9"):
        return f"SH{s}"
    if s[:1] in ("0", "3", "2"):
        return f"SZ{s}"
    if s[:1] in ("4", "8"):
        return f"BJ{s}"
    return s


def _f(v, default: float = 0.0) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return default if math.isnan(f) else f


def _main_ratio(net, super_net, large_net, buy, sell) -> float:
    denom = abs(_f(buy)) + abs(_f(sell))
    if denom <= 0:
        return 0.0
    return round((_f(super_net) + _f(large_net)) / denom * 100, 2)


def _day_flow_series(flow: pd.DataFrame, days: list[str]) -> list[float]:
    if flow.empty:
        return [0.0] * len(days)
    flow = flow.copy()
    flow["_dt"] = flow["dt"].astype(str)
    s = flow.groupby("_dt")["flow_net_amount"].sum()
    return [round(float(s.get(d, 0.0)) / 1e8, 2) for d in days]


def _fix_gbk(text) -> str:
    if not isinstance(text, str) or not text:
        return text
    for enc in ("latin1", "cp1252"):
        try:
            raw = text.encode(enc)
            decoded = raw.decode("gbk")
            if any("\u4e00" <= ch <= "\u9fff" for ch in decoded):
                return decoded
        except Exception:
            continue
    return text


def _instrument_names(data_dir: Path) -> dict[str, str]:
    p = data_dir / DATASET_DIRS["instrument_list"]
    if not p.exists():
        p = data_dir / "2_base_sector" / "instrument_detail" / "instrument_detail.parquet"
    if not p.exists():
        return {}
    try:
        df = pd.read_parquet(p)
        sym_col = "Symbol" if "Symbol" in df.columns else "symbol"
        name_col = "Name" if "Name" in df.columns else "name"
        if sym_col in df.columns and name_col in df.columns:
            return {str(s): _fix_gbk(str(n))
                    for s, n in zip(df[sym_col].astype(str), df[name_col].astype(str))}
    except Exception as exc:
        print(f"[warn] load instrument names failed: {exc}", file=sys.stderr)
    return {}


def _sector_members(data_dir: Path) -> pd.DataFrame:
    p = data_dir / DATASET_DIRS["sector_members"]
    if not p.exists():
        return pd.DataFrame()
    try:
        df = pd.read_parquet(p)
        rename = {"SectorCode": "sector_code", "SectorName": "sector_name",
                  "SectorType": "sector_type", "Symbol": "symbol"}
        df = df.rename(columns=rename)
        for col in ("sector_name", "sector_type"):
            if col in df.columns:
                df[col] = df[col].astype(str).apply(_fix_gbk)
        return df
    except Exception as exc:
        print(f"[warn] load sector members failed: {exc}", file=sys.stderr)
        return pd.DataFrame()


def _is_garbled(s) -> bool:
    if not s:
        return True
    return not any("\u4e00" <= c <= "\u9fff" for c in s)


def _sector_groups(data_dir: Path, category: str) -> dict[str, list[str]]:
    members = _sector_members(data_dir)
    if members.empty or "symbol" not in members.columns:
        return {}
    if category == "shenwan":
        lvl2 = members[members["sector_type"].astype(str).str.contains("二级")]
        if lvl2.empty:
            return {}
        groups: dict[str, list[str]] = {}
        for row in lvl2.itertuples(index=False):
            sname = str(getattr(row, "sector_name", "") or "").strip()
            sym = str(getattr(row, "symbol", "") or "").strip()
            if sname and sym:
                groups.setdefault(sname, []).append(sym)
        return groups
    groups = {}
    for row in members.itertuples(index=False):
        sname = str(getattr(row, "sector_name", "") or "").strip()
        sym = str(getattr(row, "symbol", "") or "").strip()
        code = str(getattr(row, "sector_code", "") or "").strip()
        if not sname or not sym or _is_garbled(sname):
            continue
        if code.startswith("880"):
            groups.setdefault(sname, []).append(sym)
    return groups


# ---------- 聚合（复刻 quantdb_feed / 官网快照） ----------

def get_indices_overview(con, data_dir: Path) -> list[dict]:
    latest = _latest_trade_date(data_dir)
    if not latest:
        return []
    days = _trading_days(data_dir, latest, 30)
    if not days:
        return []
    dt_in = ", ".join(f"'{d}'" for d in days)
    sym_in = ",".join(f"'{i['symbol']}'" for i in INDEX_OVERVIEW)
    df = _q(con, f"SELECT symbol, dt, close, amount FROM qdb_index_daily WHERE dt IN ({dt_in}) AND symbol IN ({sym_in})")
    if df.empty:
        return []
    df["dt"] = df["dt"].astype(str)
    result = []
    # 参考日：以指数自身最新可用交易日为准，而非 daily_unadjusted。
    # 若 index_daily 落后于最新交易日（如 8/28 分区未同步），避免把上一交易日
    # 的收盘按最新日显示——每个指数输出其真实 trade_date，前端据此标注。
    for item in INDEX_OVERVIEW:
        sub = df[df["symbol"] == item["symbol"]].sort_values("dt")
        if sub.empty:
            continue
        closes = sub["close"].tolist()
        ref = sub["dt"].iloc[-1]
        last_close = float(closes[-1])
        prev_close = float(closes[-2]) if len(closes) > 1 else last_close
        change = last_close - prev_close
        pct = (change / prev_close * 100) if prev_close else 0.0
        turnover = float(sub["amount"].iloc[-1] or 0.0) / 10000.0
        result.append({
            "symbol": _normalize_prefix(item["symbol"]),
            "name": item["name"],
            "price": round(last_close, 2),
            "change": round(change, 2),
            "pct_change": round(pct, 2),
            "turnover": round(turnover, 2),
            "trend": [round(float(c), 2) for c in closes[-5:]],
            "trade_date": f"{ref[:4]}-{ref[4:6]}-{ref[6:]}",
        })
    return result


def get_market_breadth(con, data_dir: Path) -> dict:
    empty = {
        "trade_date": "", "advance_count": 0, "decline_count": 0, "flat_count": 0,
        "limit_up_count": 0, "limit_down_count": 0, "total_turnover_yi": 0.0,
        "exploded_ratio": 0.0, "profit_effect_score": 50.0, "profit_effect": 50.0,
        "limit_up_broken_ratio": 0.0,
    }
    latest, snap = _market_pct_snapshot(con, data_dir)
    if not latest or snap.empty:
        empty["trade_date"] = (f"{latest[:4]}-{latest[4:6]}-{latest[6:]}" if latest else "")
        return empty
    pct = snap["pct_change"].fillna(0.0)
    adv = int((pct > 0).sum()); dec = int((pct < 0).sum()); flat = int((pct == 0).sum())
    l_up = int((pct >= 9.8).sum()); l_down = int((pct <= -9.8).sum())
    total_amt = float(snap["amount"].sum() or 0.0)
    if total_amt > 1e11:
        turnover_yi = round(total_amt / 1e8, 1)
    elif total_amt > 1e7:
        turnover_yi = round(total_amt / 1e4, 1)
    else:
        turnover_yi = round(total_amt, 1)
    total_stocks = adv + dec + flat
    profit_effect = round((adv / total_stocks * 100) if total_stocks > 0 else 50.0, 1)
    exploded = round(10.0 + (dec / max(total_stocks, 1) * 8.0), 1)
    return {
        "trade_date": f"{latest[:4]}-{latest[4:6]}-{latest[6:]}",
        "advance_count": adv, "decline_count": dec, "flat_count": flat,
        "limit_up_count": l_up, "limit_down_count": l_down, "total_turnover_yi": turnover_yi,
        "profit_effect": profit_effect, "profit_effect_score": profit_effect,
        "limit_up_broken_ratio": exploded, "exploded_ratio": exploded,
    }


def get_sector_heatmap(con, data_dir: Path, category: str = "shenwan") -> list[dict]:
    dates, prices = _market_pct_snapshot(con, data_dir)
    if prices.empty:
        return []
    names = _instrument_names(data_dir)
    prices["name"] = prices["symbol"].map(lambda s: names.get(s, s))
    prices["pct_change"] = prices["pct_change"].fillna(0.0)
    groups = _sector_groups(data_dir, category)
    if not groups:
        return []
    today = str(dates).zfill(8) if dates else ""
    mv_map: dict[str, float] = {}
    # 全部分类统一用估值总市值作为矩形面积(市值权重)，不再仅限 shenwan
    if today:
        vdf = _q(con, f"SELECT symbol, total_mv FROM qdb_valuation WHERE dt = {today}")
        if not vdf.empty:
            for r in vdf.itertuples(index=False):
                sym = str(r.symbol).strip().upper()
                tv = float(r.total_mv or 0.0)
                if not math.isfinite(tv):
                    tv = 0.0
                mv_map[sym] = tv
    items = []
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
            # 估值缺失时统一按成交额(亿)兜底，单位一致，避免多分支换算比例错乱
            # 注意：amount 口径为万元，万元→亿除 1e4（曾误写 1e8 导致全部分类被保底 10）。
            tot_amt = float(sub["amount"].sum() or 0.0)
            val_yi = round(tot_amt / 1e4, 1)
        leader_row = sub.sort_values("pct_change", ascending=False).iloc[0]
        items.append({
            "name": sname, "value": max(val_yi, 10.0), "pct_change": avg_pct,
            "leader": str(leader_row.get("name") or leader_row["symbol"]),
            "leader_pct": round(float(leader_row["pct_change"] or 0.0), 2),
        })
    items.sort(key=lambda x: x["value"], reverse=True)
    limit = 80 if category == "shenwan" else 50
    return items[:limit]


def _detect_placeholder_symbols(flow: pd.DataFrame, share_thresh: int = 25) -> set[str]:
    bad: set[str] = set()
    if flow.empty or "flow_net_amount" not in flow.columns:
        return bad
    ff = flow[flow["flow_net_amount"].notna() & (flow["flow_net_amount"] != 0)]
    if ff.empty:
        return bad
    counts = ff.groupby("flow_net_amount")["symbol"].nunique().reset_index(name="n")
    for v in counts[counts["n"] >= share_thresh]["flow_net_amount"].tolist():
        bad |= set(ff[ff["flow_net_amount"] == v]["symbol"])
    return bad


def get_stock_flow_full(con, data_dir: Path, limit: int = 6500) -> list[dict]:
    ref = _latest_l2_date(data_dir)
    if not ref:
        return []
    days = _trading_days(data_dir, ref, 1)
    flow = _load_l2_flow(con, days)
    if flow.empty:
        return []
    bad = _detect_placeholder_symbols(flow)
    if bad:
        flow = flow[~flow["symbol"].isin(bad)]
    prices = _load_prices(con, [days[0]])
    names = _instrument_names(data_dir)
    flow = flow.merge(prices[["symbol", "close", "pct_change"]], on="symbol", how="left")
    flow = flow.sort_values("flow_net_amount", ascending=False).head(limit)
    items = []
    for row in flow.itertuples(index=False):
        net = _f(row.flow_net_amount)
        items.append({
            "symbol": _normalize_prefix(row.symbol), "name": names.get(row.symbol, ""),
            "close_price": round(_f(row.close), 2), "pct_change": round(_f(row.pct_change), 2),
            "net_inflow": int(net), "gross_inflow": int(_f(row.flow_buy_amount)),
            "gross_outflow": int(_f(row.flow_sell_amount)),
            "main_ratio": _main_ratio(net, row.flow_super_net, row.flow_large_net, row.flow_buy_amount, row.flow_sell_amount),
            "super_large": int(_f(row.flow_super_net)), "large": int(_f(row.flow_large_net)),
            "medium": int(_f(row.flow_medium_net)), "small": int(_f(row.flow_small_net)),
        })
    return items


def get_stock_money_flow(con, data_dir: Path, limit: int = 20) -> list[dict]:
    ref = _latest_l2_date(data_dir)
    if not ref:
        return []
    days = _trading_days(data_dir, ref, 30)
    if not days:
        return []
    today = days[0]
    hist = _load_l2_flow(con, days)
    if hist.empty:
        return []
    hist["_dt"] = hist["dt"].astype(str)
    flow = hist[hist["_dt"] == today]
    if flow.empty:
        return []
    bad_by_dt: dict[str, set[str]] = {}
    if "flow_net_amount" in hist.columns:
        hng = hist[hist["flow_net_amount"].notna() & (hist["flow_net_amount"] != 0)][["symbol", "_dt", "flow_net_amount"]]
        cc = hng.groupby(["_dt", "flow_net_amount"]).size().reset_index(name="n")
        # itertuples 会把下划线开头的列名重命名为位置名，row._dt 必抛 AttributeError；
        # 此处按位置解包（列序固定为 [_dt, flow_net_amount, n]）。
        for _dt_val, _famt, _n in cc[cc["n"] >= 25].itertuples(index=False):
            dt = str(_dt_val)
            syms = set(hng[(hng["_dt"] == dt) & (hng["flow_net_amount"] == _famt)]["symbol"])
            bad_by_dt.setdefault(dt, set()).update(syms)
    prices = _load_prices(con, [today])
    names = _instrument_names(data_dir)
    flow = flow.merge(prices[["symbol", "close", "pct_change"]], on="symbol", how="left")
    top = flow.sort_values("flow_net_amount", ascending=False).head(limit)
    top_syms = set(top["symbol"])
    grp_by_prefix: dict[str, pd.DataFrame] = {}
    for sym, grp in hist[hist["symbol"].isin(top_syms)].groupby("symbol"):
        grp_by_prefix[_normalize_prefix(sym)] = grp.sort_values("dt")
    trend_map = {sym: _day_flow_series(grp, days) for sym, grp in grp_by_prefix.items()}
    detail_map: dict[str, list[dict]] = {}
    for sym, grp in grp_by_prefix.items():
        rows = []
        for row in grp.itertuples(index=False):
            dt = str(row.dt)
            if dt in bad_by_dt and sym in bad_by_dt[dt]:
                continue
            rows.append({
                "date": dt,
                "inflow": round(_f(row.flow_buy_amount) / 1e8, 2),
                "outflow": round(_f(row.flow_sell_amount) / 1e8, 2),
                "net_flow": round(_f(row.flow_net_amount) / 1e8, 2),
            })
        detail_map[sym] = rows
    items = []
    for row in top.itertuples(index=False):
        sym_prefix = _normalize_prefix(row.symbol)
        net = _f(row.flow_net_amount)
        items.append({
            "symbol": sym_prefix, "name": names.get(row.symbol, ""),
            "close_price": round(_f(row.close), 2), "pct_change": round(_f(row.pct_change), 2),
            "net_inflow": int(net), "gross_inflow": int(_f(row.flow_buy_amount)),
            "gross_outflow": int(_f(row.flow_sell_amount)),
            "main_ratio": _main_ratio(net, row.flow_super_net, row.flow_large_net, row.flow_buy_amount, row.flow_sell_amount),
            "super_large": int(_f(row.flow_super_net)), "large": int(_f(row.flow_large_net)),
            "medium": int(_f(row.flow_medium_net)), "small": int(_f(row.flow_small_net)),
            "trend_30d": trend_map.get(sym_prefix, []),
            "daily_details_30d": detail_map.get(sym_prefix, []),
        })
    return items


def get_money_flow_sankey(con, data_dir: Path) -> dict | None:
    latest = _latest_l2_date(data_dir)
    if not latest:
        return None
    days = _trading_days(data_dir, latest, 1)
    flow = _load_l2_flow(con, days)
    if flow.empty:
        return None
    groups = _sector_groups(data_dir, "shenwan")
    agg = []
    for name, syms in groups.items():
        grp = flow[flow["symbol"].isin(syms)]
        if grp.empty:
            continue
        agg.append((name,
                    _f(grp["flow_super_net"].sum()), _f(grp["flow_large_net"].sum()),
                    _f(grp["flow_medium_net"].sum()), _f(grp["flow_small_net"].sum())))
    if not agg:
        return None
    agg.sort(key=lambda x: abs(x[1] + x[2] + x[3] + x[4]), reverse=True)
    top = agg[:8]
    nodes = [
        {"name": "主力资金 (Net Buy)"}, {"name": "散户资金 (Retail)"},
        {"name": "超大单 (Super Large)"}, {"name": "大单 (Large)"},
        {"name": "中单 (Medium)"}, {"name": "小单 (Small)"},
    ]
    links: dict[tuple[str, str], float] = {}
    def yi(v: float) -> float:
        return abs(v) / 1e8
    for name, s, l, m, sm in top:
        nodes.append({"name": name})
        links[("超大单 (Super Large)", "主力资金 (Net Buy)")] = links.get(("超大单 (Super Large)", "主力资金 (Net Buy)"), 0.0) + yi(s)
        links[("大单 (Large)", "主力资金 (Net Buy)")] = links.get(("大单 (Large)", "主力资金 (Net Buy)"), 0.0) + yi(l)
        links[("中单 (Medium)", "散户资金 (Retail)")] = links.get(("中单 (Medium)", "散户资金 (Retail)"), 0.0) + yi(m)
        links[("小单 (Small)", "散户资金 (Retail)")] = links.get(("小单 (Small)", "散户资金 (Retail)"), 0.0) + yi(sm)
        links[("主力资金 (Net Buy)", name)] = yi(s + l)
        links[("散户资金 (Retail)", name)] = yi(m + sm)
    return {"nodes": nodes, "links": [{"source": src, "target": dst, "value": round(v, 2)} for (src, dst), v in links.items()]}


def get_money_flow_period(con, data_dir: Path, period: str = "1d", dimension: str = "sector",
                          category: str = "shenwan", limit: int = 31) -> list[dict]:
    n_days = PERIOD_DAYS.get(period.lower(), 1)
    ref = _latest_l2_date(data_dir)
    if not ref:
        return []
    days = _trading_days(data_dir, ref, 20)
    if not days:
        return []
    window = days[:n_days]
    flow_all = _load_l2_flow(con, days)
    if flow_all.empty:
        return []
    flow_all["_dt"] = flow_all["dt"].astype(str)
    window_dt = set(window)
    flow = flow_all[flow_all["_dt"].isin(window_dt)].copy()
    if flow.empty:
        return []
    prices = _load_prices(con, [days[0]])
    names = _instrument_names(data_dir)
    trend_by_sym = {sym: _day_flow_series(grp, days) for sym, grp in flow_all.groupby("symbol")}

    def _build_items(grouped, is_sector: bool) -> list[dict]:
        items = []
        for key, grp in grouped:
            net = _f(grp["flow_net_amount"].sum())
            super_net = _f(grp["flow_super_net"].sum())
            large_net = _f(grp["flow_large_net"].sum())
            medium_net = _f(grp["flow_medium_net"].sum())
            small_net = _f(grp["flow_small_net"].sum())
            buy = _f(grp["flow_buy_amount"].sum())
            sell = _f(grp["flow_sell_amount"].sum())
            if is_sector:
                name = str(key)
                pct = _f(grp[grp["_dt"] == window[0]]["pct_change"].mean()) if not grp.empty else 0.0
                prices_row = prices[prices["symbol"].isin(grp["symbol"].unique())]
                last_price = _f(prices_row["close"].mean()) if not prices_row.empty else 0.0
                trend = _day_flow_series(flow_all[flow_all["symbol"].isin(grp["symbol"].unique())], days)
                id_ = name
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
                "id": id_, "name": name, "symbol": symbol_out,
                "pct_change": round(pct, 2), "close_price": round(last_price, 2),
                "net_inflow": net, "main_ratio": _main_ratio(net, super_net, large_net, buy, sell),
                "super_large": super_net, "large": large_net, "medium": medium_net, "small": small_net,
                "trend_20d": trend,
            })
        items.sort(key=lambda x: x["net_inflow"], reverse=True)
        return items

    if dimension == "stock":
        return _build_items(flow.groupby("symbol"), is_sector=False)[:limit]
    groups = _sector_groups(data_dir, category)
    merged = flow.merge(prices[["symbol", "pct_change"]], on="symbol", how="left")
    rows = []
    for name, syms in groups.items():
        grp = merged[merged["symbol"].isin(syms)]
        if grp.empty:
            continue
        grp = grp.copy()
        grp["_sector"] = name
        rows.append(grp)
    if rows:
        cat = pd.concat(rows, ignore_index=True)
        return _build_items(cat.groupby("_sector"), is_sector=True)[:limit]
    return []


def get_tag_stats(data_dir: Path, limit: int = 30) -> dict:
    members = _sector_members(data_dir)
    empty = {"total_sectors": 0, "total_stocks": 0, "avg_tags_per_stock": 0.0,
             "max_tags_per_stock": 0, "total_relations": 0, "hot_tags": []}
    if members.empty or "symbol" not in members.columns:
        return empty
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
    grp = members.groupby(["sector_name", "sector_type"], dropna=False).size().reset_index(name="count").sort_values("count", ascending=False)
    hot = [{"name": str(r.sector_name), "type": str(getattr(r, "sector_type", "") or "通用标签"), "count": int(r.count)} for r in grp.head(limit).itertuples(index=False)]
    return {"total_sectors": total_sectors, "total_stocks": total_stocks,
            "avg_tags_per_stock": avg_tags, "max_tags_per_stock": max_tags,
            "total_relations": total_relations, "hot_tags": hot[:limit]}


def build_tags_db(data_dir: Path, out_dir: Path, date: str, heatmap: dict | None = None,
                  copy_latest: bool = True) -> Path:
    import shutil
    members = _sector_members(data_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    db_path = out_dir / f"{date}.db"
    con = sqlite3.connect(str(db_path))
    try:
        con.execute("DROP TABLE IF EXISTS tags")
        con.execute("DROP TABLE IF EXISTS meta")
        con.execute("DROP TABLE IF EXISTS sector_mv")
        con.execute("CREATE TABLE tags(symbol TEXT, sector_code TEXT, sector_name TEXT, sector_type TEXT)")
        con.execute("CREATE TABLE sector_mv(category TEXT, name TEXT, value REAL, pct_change REAL, leader TEXT, leader_pct REAL)")
        con.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
        con.execute("INSERT INTO meta VALUES('trade_date', ?)", (date,))
        n = 0
        if not members.empty and "symbol" in members.columns:
            df = members[["symbol", "sector_code", "sector_name", "sector_type"]].copy()
            df["symbol"] = df["symbol"].astype(str).map(_normalize_prefix)
            df = df[df["symbol"].str.match(r"^(SH|SZ|BJ)\d{6}$", na=False)]
            df = df[df["sector_name"].str.contains("[\u4e00-\u9fff]", na=False) & (df["sector_name"].str.strip() != "")]
            rows = [tuple(r) for r in df.itertuples(index=False, name=None)]
            con.executemany("INSERT INTO tags VALUES(?,?,?,?)", rows)
            n = len(rows)
        mv_rows = []
        for cat, items in (heatmap or {}).items():
            for it in (items or []):
                mv_rows.append((cat, it.get("name"), it.get("value"),
                                it.get("pct_change"), it.get("leader"), it.get("leader_pct")))
        con.executemany("INSERT INTO sector_mv VALUES(?,?,?,?,?,?)", mv_rows)
        con.execute("CREATE INDEX idx_sym ON tags(symbol)")
        con.execute("CREATE INDEX idx_tag ON tags(sector_name)")
        con.execute("CREATE INDEX idx_type ON tags(sector_type)")
        con.execute("CREATE INDEX idx_sector_mv ON sector_mv(category)")
        con.commit()
    finally:
        con.close()
    if copy_latest:
        shutil.copyfile(db_path, out_dir / "latest.db")
    print(f"[step] tags db -> {db_path} ({n} 条 + sector_mv {len(mv_rows)} 行)" + (" + latest.db" if copy_latest else " [回补]"))
    return db_path


def main():
    parser = argparse.ArgumentParser(description="Compute market analysis snapshot from local QuantDB parquet")
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR), help="QuantDB data dir (default D:\\quant_data)")
    parser.add_argument("--out", default=str(DEFAULT_OUT_DIR), help="Output dir for JSON snapshots + SQLite")
    parser.add_argument("--periods", nargs="*", default=["1d", "5d", "20d"], help="money-flow periods (精简三档)")
    parser.add_argument("--ref-date", default=None, help="锚定交易日 YYYY-MM-DD 计算单日历史快照(回补)")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    if not data_dir.exists():
        print(f"[error] data_dir not exists: {data_dir}", file=sys.stderr)
        sys.exit(1)

    is_backfill = False
    if args.ref_date:
        rd = args.ref_date.replace("-", "")
        if len(rd) != 8 or not rd.isdigit():
            print("[error] --ref-date 格式应为 YYYY-MM-DD", file=sys.stderr)
            sys.exit(1)
        global _REF_OVERRIDE
        _REF_OVERRIDE = rd
        is_backfill = True
        print(f"[info] --ref-date={args.ref_date} → 历史回补模式")
    else:
        print("[info] 正常模式（每日最新）")

    t0 = datetime.now()
    con = get_conn(data_dir)
    print(f"[info] data_dir={data_dir} out_dir={out_dir} latest_trade={_latest_trade_date(data_dir)} latest_l2={_latest_l2_date(data_dir)}")

    indices = get_indices_overview(con, data_dir)
    print(f"[step] indices {len(indices)}")
    breadth = get_market_breadth(con, data_dir)
    print(f"[step] breadth trade_date={breadth.get('trade_date')}")
    heatmap_shenwan = get_sector_heatmap(con, data_dir, "shenwan")
    print(f"[step] heatmap shenwan {len(heatmap_shenwan)}")
    heatmap_concept = get_sector_heatmap(con, data_dir, "concept")
    print(f"[step] heatmap concept {len(heatmap_concept)}")
    sankey = get_money_flow_sankey(con, data_dir)
    print(f"[step] sankey nodes={len(sankey['nodes']) if sankey else 0}")
    stock_flow = get_stock_money_flow(con, data_dir, limit=20)
    print(f"[step] stock_flow {len(stock_flow)}")
    stock_flow_full = get_stock_flow_full(con, data_dir)
    print(f"[step] stock_flow_full {len(stock_flow_full)}")
    tag_stats = get_tag_stats(data_dir, limit=30)
    print(f"[step] tag_stats {tag_stats['total_sectors']} sectors")

    money_flow_periods = {}
    for p in args.periods:
        for dim in ["sector", "stock"]:
            for cat in (["shenwan", "concept"] if dim == "sector" else ["shenwan"]):
                key = f"{p}_{dim}_{cat}"
                limit = 80 if dim == "sector" else 31
                money_flow_periods[key] = get_money_flow_period(con, data_dir, period=p, dimension=dim, category=cat, limit=limit)
                print(f"[step] period {key} {len(money_flow_periods[key])}")

    trade_date = breadth.get("trade_date") or _latest_trade_date(data_dir) or datetime.now().strftime("%Y-%m-%d")
    file_date = f"{trade_date[:4]}-{trade_date[5:7]}-{trade_date[8:]}" if "-" in trade_date else \
        f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}"

    snapshot = {
        "trade_date": file_date,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "indices": indices, "breadth": breadth,
        "heatmap": {"shenwan": heatmap_shenwan, "concept": heatmap_concept},
        "sankey": sankey or {"nodes": [], "links": []},
        "stock_flow": stock_flow, "stock_flow_full": stock_flow_full,
        "tag_stats": tag_stats, "money_flow_periods": money_flow_periods,
    }

    dated_path = out_dir / f"{file_date}.json"
    with open(dated_path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)
    if not is_backfill:
        with open(out_dir / "latest.json", "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)

    build_tags_db(data_dir, out_dir, file_date,
                  heatmap={"shenwan": heatmap_shenwan, "concept": heatmap_concept},
                  copy_latest=not is_backfill)

    elapsed = (datetime.now() - t0).total_seconds()
    tail = "latest.json + latest.db" if not is_backfill else f"{file_date}.db [回补]"
    print(f"[done] snapshot -> {dated_path} + {tail} ({elapsed:.1f}s) trade_date={file_date}")


if __name__ == "__main__":
    main()