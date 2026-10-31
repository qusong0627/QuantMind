"""每日复盘取数脚本：读 QuantDB 本地 parquet，产出 stats JSON + facts Markdown。

用法:
    python3 daily_review.py                  # 最新交易日
    python3 daily_review.py --date 20260814  # 指定交易日
    python3 daily_review.py --watch 601138.SH,600519.SH

输出（out_dir 默认 <repo>/data/reports/daily_review/）:
    {YYYY-MM-DD}_stats.json   结构化统计（后续阶段/智能体消费）
    {YYYY-MM-DD}_facts.md     事实清单（写复盘报告的事实依据，禁止编造）

单位口径遵循 skills/quantdb-fields/SKILL.md：
个股 volume=股、amount=万元；指数 volume=手、amount=万元；
technical_indicators.pct_change 为 %；valuation.float_mv 为元。
JSON 中带 _yi 后缀的金额字段单位统一为亿元。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import duckdb
import pandas as pd

def _find_repo_root(start: Path) -> Path:
    for p in [start, *start.parents]:
        if (p / "backend" / "main_oss.py").is_file():
            return p
    raise FileNotFoundError("未找到仓库根（含 backend/main_oss.py）")


_REPO_ROOT = _find_repo_root(Path(__file__).resolve())
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import review_stats as rs  # noqa: E402

INDEXES: list[tuple[str, str]] = [
    ("上证指数", "000001.SH"),
    ("深证成指", "399001.SZ"),
    ("创业板指", "399006.SZ"),
    ("科创50", "000688.SH"),
    ("北证50", "899050.BJ"),
    ("沪深300", "000300.SH"),
    ("中证500", "000905.SH"),
    ("中证1000", "000852.SH"),
    ("上证50", "000016.SH"),
]

# 说明这些数据集为何「本地落后」：它们不在每日自动同步表里，本地分区停滞，
# 上游仍有数据、可由 QuantDB 同步补齐 —— 并非上游停更。
_STALE_NOTE = "本地未同步，可由 QuantDB 同步补齐（非上游停更）"
STALE_NOTES: dict[str, str] = {
    "l1_l2_factors": _STALE_NOTE,
    "min1_kline": _STALE_NOTE,
    "min5_kline": _STALE_NOTE,
    "hsgt_north": _STALE_NOTE,
}


def _yi(v: float | None) -> str:
    return "[数据缺失]" if v is None else f"{v:,.2f} 亿元"


def q(db: duckdb.DuckDBPyConnection, sql: str) -> pd.DataFrame:
    return db.execute(sql).df()


def q1(db: duckdb.DuckDBPyConnection, sql: str) -> list:
    return db.execute(sql).fetchall()


def default_data_dir() -> Path:
    for cand in (_REPO_ROOT / "data" / "quantdb", Path("/data/quantdb")):
        if (cand / "1_kline_data").is_dir():
            return cand
    raise FileNotFoundError("找不到 QuantDB 数据目录（仓库下 data/quantdb 或 /data/quantdb）")


def resolve_trade_date(db: duckdb.DuckDBPyConnection, data_dir: Path, request: str | None) -> str:
    rows = q1(
        db,
        f"SELECT max(dt) FROM read_parquet('{data_dir}/1_kline_data/daily_unadjusted/dt=*/data.parquet',"
        f" hive_partitioning=true)",
    )
    latest = str(rows[0][0])
    if latest in ("None", ""):
        raise RuntimeError("daily_unadjusted 无数据")
    if not request:
        return latest
    request = request.replace("-", "")
    if request > latest:
        raise SystemExit(f"请求日期 {request} 晚于数据最新交易日 {latest}")
    rows = q1(
        db,
        f"SELECT max(dt) FROM read_parquet('{data_dir}/1_kline_data/daily_unadjusted/dt=*/data.parquet',"
        f" hive_partitioning=true) WHERE dt <= '{request}'",
    )
    if rows[0][0] is None:
        raise SystemExit(f"{request} 之前无交易日数据")
    return str(rows[0][0])


def load_trading_days(db: duckdb.DuckDBPyConnection, data_dir: Path, n: int, end: str) -> list[str]:
    rows = q1(
        db,
        f"SELECT DISTINCT dt FROM read_parquet("
        f"'{data_dir}/1_kline_data/daily_unadjusted/dt=*/data.parquet', hive_partitioning=true)"
        f" WHERE dt <= '{end}' ORDER BY dt DESC LIMIT {n}",
    )
    return [str(r[0]) for r in rows]


def load_unadj(db: duckdb.DuckDBPyConnection, data_dir: Path, dts: list[str]) -> pd.DataFrame:
    dt_in = ",".join(f"'{d}'" for d in dts)
    df = q(
        db,
        f"SELECT symbol, dt, open, high, low, close, volume, amount FROM read_parquet("
        f"'{data_dir}/1_kline_data/daily_unadjusted/dt=*/data.parquet', hive_partitioning=true)"
        f" WHERE dt IN ({dt_in})",
    )
    df["dt"] = df["dt"].astype(str)
    return df


def load_tech(db: duckdb.DuckDBPyConnection, data_dir: Path, dts: list[str]) -> pd.DataFrame:
    dt_in = ",".join(f"'{d}'" for d in dts)
    df = q(
        db,
        f"SELECT symbol, dt, pct_change, ma5, ma20 FROM read_parquet("
        f"'{data_dir}/5_technical_derived/technical_indicators/dt=*/data.parquet',"
        f" hive_partitioning=true) WHERE dt IN ({dt_in})",
    )
    df["dt"] = df["dt"].astype(str)
    return df


def build_today(
    db: duckdb.DuckDBPyConnection,
    data_dir: Path,
    trade_date: str,
    prev_date: str,
    st_set: set[str],
) -> tuple[pd.DataFrame, pd.Series, pd.Series]:
    """构造当日宽表：OHLCV(不复权) + 官方 pct_change + 涨跌停分类。"""
    unadj = load_unadj(db, data_dir, [trade_date, prev_date])
    prev_k = unadj[unadj["dt"] == prev_date][["symbol", "close"]].rename(
        columns={"close": "prev_close"}
    )
    today = unadj[unadj["dt"] == trade_date].merge(prev_k, on="symbol", how="left")
    tech = load_tech(db, data_dir, [trade_date])
    today = today.merge(tech[["symbol", "pct_change", "ma5", "ma20"]], on="symbol", how="left")
    today["is_st"] = today["symbol"].isin(st_set)
    today["ex_div"] = [
        rs.is_ex_div(float(p), float(c), float(pc)) if pd.notna(pc) else False
        for p, c, pc in zip(
            today["pct_change"].fillna(0.0), today["close"], today["prev_close"]
        )
    ]

    trade_dt_obj = pd.Timestamp(trade_date)
    trade_dt_date = trade_dt_obj.date()
    cats: list[str] = []
    up_prices: list[float] = []
    for _, row in today.iterrows():
        up: float = 0.0
        if pd.notna(row["prev_close"]) and not row["ex_div"]:
            up, down = rs.compute_limits(
                row["symbol"], float(row["prev_close"]), is_st=row["is_st"], trade_date=trade_dt_date
            )
            cats.append(
                rs.classify_price(float(row["close"]), float(row["high"]), up, down)
            )
        else:
            cats.append(
                rs.classify_by_pct(
                    float(row["pct_change"]) if pd.notna(row["pct_change"]) else 0.0,
                    row["symbol"],
                    bool(row["is_st"]),
                    trade_dt_date,
                )
            )
        up_prices.append(up)
    today["category"] = cats
    today["limit_up_price"] = up_prices

    # 后处理：|pct| 超板块限制 → 公司行为最先判（除权日价格法会误判跌停）；
    # normal → 按符号归 up/down/flat
    final_cats: list[str] = []
    for i, c in enumerate(cats):
        p = today["pct_change"].iloc[i]
        if pd.isna(p):
            final_cats.append("missing_pct")
            continue
        p = float(p)
        board = float(
            rs.limit_pct(today["symbol"].iloc[i], is_st=bool(today["is_st"].iloc[i]), trade_date=trade_dt_date)
        )
        if rs.is_corp_action_pct(p, board):
            final_cats.append(rs.CAT_CORP_ACTION)
        elif c in (rs.CAT_LIMIT_UP, rs.CAT_LIMIT_DOWN, rs.CAT_BROKE_UP):
            final_cats.append(c)
        elif c == rs.CAT_NORMAL:
            final_cats.append(rs.CAT_UP if p > 0 else (rs.CAT_DOWN if p < 0 else rs.CAT_FLAT))
        else:
            final_cats.append(c)
    today["category"] = final_cats

    pct_series = today.set_index("symbol")["pct_change"]
    close_series = today.set_index("symbol")["close"]
    return today, pct_series, close_series


def load_valuation_latest(
    db: duckdb.DuckDBPyConnection, data_dir: Path, trade_date: str
) -> tuple[pd.Series, pd.Series]:
    dts = load_trading_days(db, data_dir, 10, trade_date)
    dt_in = ",".join(f"'{d}'" for d in dts)
    dfv = q(
        db,
        f"SELECT symbol, dt, float_mv, circulating_capital FROM read_parquet("
        f"'{data_dir}/5_technical_derived/valuation/dt=*/data.parquet', hive_partitioning=true)"
        f" WHERE dt IN ({dt_in})",
    )
    if dfv.empty:
        return pd.Series(dtype=float), pd.Series(dtype=float)
    latest = dfv.sort_values("dt").groupby("symbol").tail(1)
    return latest.set_index("symbol")["float_mv"], latest.set_index("symbol")["circulating_capital"]


def load_index_stats(
    db: duckdb.DuckDBPyConnection, data_dir: Path, trade_date: str, n_days: int = 30
) -> list[dict]:
    dts = load_trading_days(db, data_dir, n_days, trade_date)
    if not dts:
        return []
    dt_in = ",".join(f"'{d}'" for d in dts)
    sym_in = ",".join(f"'{s}'" for _, s in INDEXES)
    df = q(
        db,
        f"SELECT symbol, dt, high, low, close, preClose, amount FROM read_parquet("
        f"'{data_dir}/1_kline_data/index_daily/dt=*/data.parquet', hive_partitioning=true)"
        f" WHERE dt IN ({dt_in}) AND symbol IN ({sym_in})",
    )
    df["dt"] = df["dt"].astype(str)
    rows = []
    for name, sym in INDEXES:
        sub = df[df["symbol"] == sym].sort_values("dt")
        if sub.empty or sub["dt"].iloc[-1] != trade_date:
            rows.append({"name": name, "symbol": sym, "missing": True})
            continue
        today = sub.iloc[-1]
        prior5 = sub["amount"].iloc[-6:-1].tolist()
        closes = sub["close"]
        ma5 = float(closes.iloc[-5:].mean())
        ma20 = float(closes.iloc[-20:].mean()) if len(closes) >= 20 else None
        # preClose 全 NULL：涨跌幅用 close 序列的上一交易日收盘计算
        prev_close_val = float(sub["close"].iloc[-2]) if len(sub) >= 2 else None
        pct = (
            round(float((today["close"] / prev_close_val - 1) * 100), 2)
            if prev_close_val
            else None
        )
        rows.append(
            {
                "name": name,
                "symbol": sym,
                "close": round(float(today["close"]), 2),
                "pct": pct,
                "amount_yi": round(float(today["amount"]) / 1e4, 2),
                "vol_ratio_5": rs.volume_ratio_5(float(today["amount"]), prior5),
                "vs_ma5": round(float((today["close"] / ma5 - 1) * 100), 2),
                "vs_ma20": round(float((today["close"] / ma20 - 1) * 100), 2) if ma20 else None,
            }
        )
    return rows


def load_market_stats(
    db: duckdb.DuckDBPyConnection,
    data_dir: Path,
    trade_date: str,
    today: pd.DataFrame,
    st_set: set[str],
    names: dict[str, str] | None = None,
) -> dict:
    suspended = today["volume"].fillna(0) == 0
    active_pct = today[~suspended]["pct_change"].dropna()
    breadth = rs.market_breadth(active_pct)

    cat = today["category"]
    exact_up = int((cat == rs.CAT_LIMIT_UP).sum())
    exact_down = int((cat == rs.CAT_LIMIT_DOWN).sum())
    broke_up = int((cat == rs.CAT_BROKE_UP).sum())
    corp_action = int((cat == rs.CAT_CORP_ACTION).sum())

    non_limit_pct = today[
        (~suspended)
        & (~today["category"].isin(
            set([rs.CAT_LIMIT_UP, rs.CAT_LIMIT_DOWN, rs.CAT_CORP_ACTION, "missing_pct"])
        ))
    ]["pct_change"].dropna()
    dist = rs.breadth_distribution(non_limit_pct)
    approx_up = dist["涨停"]
    approx_dn = dist["跌停"]
    dist["涨停"] = exact_up
    dist["跌停"] = exact_down
    dist[">7"] += approx_up
    dist["<-7"] += approx_dn

    total_amount = float(today["amount"].sum())
    prev_dates = load_trading_days(db, data_dir, 6, trade_date)
    if len(prev_dates) >= 2:
        prev_day = prev_dates[1]
        unadj_prev = load_unadj(db, data_dir, [prev_day])
        prev_total = float(unadj_prev["amount"].sum())
    else:
        prev_total = None
    prior5 = prev_dates[1:6]
    amt5: list[float] = []
    if prior5:
        unadj5 = load_unadj(db, data_dir, prior5)
        amt5 = [float(unadj5[unadj5["dt"] == d]["amount"].sum()) for d in prior5 if not unadj5[unadj5["dt"] == d].empty]
    ma5_amount = sum(amt5) / len(amt5) if amt5 else None

    streak_map: dict[str, int] = {}
    limit_today = today[today["category"] == rs.CAT_LIMIT_UP]["symbol"].tolist()
    if limit_today:
        dts12 = load_trading_days(db, data_dir, 12, trade_date)
        dt_in = ",".join(f"'{d}'" for d in dts12)
        sym_in = ",".join(f"'{s}'" for s in limit_today)
        tech12 = q(
            db,
            f"SELECT symbol, dt, pct_change FROM read_parquet("
            f"'{data_dir}/5_technical_derived/technical_indicators/dt=*/data.parquet',"
            f" hive_partitioning=true) WHERE dt IN ({dt_in}) AND symbol IN ({sym_in})",
        )
        tech12["dt"] = tech12["dt"].astype(str)
        trade_dt_obj = pd.Timestamp(trade_date)
        trade_dt_date = trade_dt_obj.date()
        st_map = today.set_index("symbol")["is_st"].to_dict()
        for sym, g in tech12.groupby("symbol"):
            g = g.sort_values("dt")
            if g["dt"].iloc[-1] != trade_date:
                continue
            tol = rs.TOL_BJ if rs.is_bse_symbol(sym) else rs.TOL_SHSZ
            board = float(rs.limit_pct(sym, is_st=st_map.get(sym, False), trade_date=trade_dt_date)) * 100
            n = rs.streak_from_tail(g["pct_change"].tolist(), float(board - tol))
            if n >= 2:
                streak_map[sym] = n

    return {
        "up_count": breadth["up_count"],
        "down_count": breadth["down_count"],
        "flat_count": breadth["flat_count"],
        "up_down_ratio": breadth["up_down_ratio"],
        "suspended_count": int(suspended.sum()),
        "limit_up": exact_up,
        "limit_down": exact_down,
        "broke_up": broke_up,
        "corp_action": corp_action,
        "max_streak": max(streak_map.values()) if streak_map else 0,
        "streaks": [
            {"symbol": sym, "name": names.get(sym, "") if names else "", "streak": n}
            for sym, n in sorted(streak_map.items(), key=lambda x: -x[1])
        ],
        "dist": dist,
        "total_amount_yi": round(total_amount / 1e4, 2),
        "prev_amount_yi": round(prev_total / 1e4, 2) if prev_total else None,
        "amount_ratio_prev": round(total_amount / prev_total, 2) if prev_total else None,
        "amount_ma5_yi": round(ma5_amount / 1e4, 2) if ma5_amount else None,
        "amount_ratio_ma5": round(total_amount / ma5_amount, 2) if ma5_amount else None,
    }


def load_sector_stats(
    db: duckdb.DuckDBPyConnection,
    data_dir: Path,
    pct: pd.Series,
    mv: pd.Series | None,
) -> dict[str, list[dict]]:
    members = q(
        db,
        f"SELECT SectorCode, SectorName, SectorType, Symbol FROM read_parquet("
        f"'{data_dir}/2_base_sector/sector_concept/sector_members.parquet')",
    )
    out: dict[str, list[dict]] = {}
    for stype in ("行业板块(一级)", "行业板块(二级)", "概念板块"):
        sub = members[members["SectorType"] == stype]
        agg = rs.sector_aggregate(sub, pct, mv)
        out[stype] = agg.head(15).to_dict("records")
        out[f"{stype}_bottom"] = agg.tail(5).to_dict("records")
    return out


def load_sentiment_stats(
    db: duckdb.DuckDBPyConnection, data_dir: Path, trade_date: str
) -> dict | None:
    df = q(
        db,
        f"SELECT buy_pressure, sell_pressure, gap_up_down, momentum_1d, momentum_3d, am_pm_trend"
        f" FROM read_parquet('{data_dir}/5_technical_derived/market_sentiment/dt={trade_date}/data.parquet')",
    )
    if df.empty:
        return None
    am_pm = df["am_pm_trend"]
    return {
        "n": len(df),
        "buy_pressure_mean": round(float(df["buy_pressure"].mean()), 4),
        "sell_pressure_mean": round(float(df["sell_pressure"].mean()), 4),
        "gap_up_down_mean": round(float(df["gap_up_down"].mean()), 4),
        "momentum_1d_mean": round(float(df["momentum_1d"].mean()), 4),
        "momentum_3d_mean": round(float(df["momentum_3d"].mean()), 4),
        "am_pm_up_pct": round(float((am_pm > 0).mean() * 100), 2),
        "am_pm_down_pct": round(float((am_pm < 0).mean() * 100), 2),
    }


def load_fund_stats(db: duckdb.DuckDBPyConnection, data_dir: Path, trade_date: str) -> dict:
    out: dict = {}
    dfm = q(
        db,
        f"SELECT dt, sum(finance_balance) AS fb, sum(slo_net) AS sn FROM read_parquet("
        f"'{data_dir}/2_base_sector/margin_trading/dt=*/data.parquet', hive_partitioning=true)"
        f" WHERE dt <= '{trade_date}' GROUP BY dt ORDER BY dt DESC LIMIT 2",
    )
    if not dfm.empty:
        cur = dfm.iloc[0]
        out["margin"] = {
            "latest_date": str(cur["dt"]).split(".")[0],
            "finance_balance_yi": round(float(cur["fb"]) / 1e4, 2),
            "slo_net_yi": round(float(cur["sn"]) / 1e4, 2) if pd.notna(cur["sn"]) else None,
        }
        if len(dfm) >= 2:
            out["margin"]["finance_balance_delta_yi"] = round(
                float(cur["fb"] - dfm.iloc[1]["fb"]) / 1e4, 2
            )
    else:
        out["margin"] = {"latest_date": None}

    qs = q1(
        db,
        f"SELECT DISTINCT quarter FROM read_parquet("
        f"'{data_dir}/2_base_sector/hsgt_north/quarter=*/data.parquet', hive_partitioning=true)"
        f" ORDER BY quarter DESC LIMIT 1",
    )
    if qs and qs[0]:
        latest_q = qs[0][0]
        dfn = q(
            db,
            f"SELECT symbol, holding_quantity, report_date FROM read_parquet("
            f"'{data_dir}/2_base_sector/hsgt_north/quarter=*/data.parquet', hive_partitioning=true)"
            f" WHERE quarter = '{latest_q}'",
        )
        # 无市值列：市值 = 持股量 × 最新不复权收盘价（≤ report_date）
        rpt = str(dfn["report_date"].max()) if "report_date" in dfn.columns else trade_date
        rpt_clean = rpt.replace(" 00:00:00", "")
        closes = q(
            db,
            f"SELECT symbol, dt, close FROM read_parquet("
            f"'{data_dir}/1_kline_data/daily_unadjusted/dt=*/data.parquet', hive_partitioning=true)"
            f" WHERE dt <= '{rpt_clean[:10].replace(chr(45), '')}'"
            f" AND symbol IN ({','.join(f'{chr(39)}{s}{chr(39)}' for s in dfn['symbol'].unique())})",
        )
        if not closes.empty:
            closes = closes.sort_values("dt").groupby("symbol").tail(1)
            merged = dfn.merge(closes[["symbol", "close"]], on="symbol", how="left")
            hv = float((merged["holding_quantity"] * merged["close"]).sum())
            if pd.isna(hv):
                hv = None
        else:
            hv = None
        out["north"] = {
            "quarter": latest_q,
            "total_value_yi": round(hv / 1e8, 2) if hv is not None else None,
            "stocks": len(dfn),
            "report_date": rpt_clean[:10],
        }
    else:
        out["north"] = {"quarter": None}

    rows = q1(
        db,
        f"SELECT max(dt) FROM read_parquet('{data_dir}/6_ml_datasets/l1_l2_factors/dt=*/data.parquet',"
        f" hive_partitioning=true)",
    )
    out["flow_latest"] = rows[0][0] if rows and rows[0] else None
    out["flow_note"] = STALE_NOTES["l1_l2_factors"]
    return out


def load_top_lists(
    today: pd.DataFrame,
    names: dict[str, str],
    industry: dict[str, str],
    turnover: pd.Series,
    include_st: bool,
) -> dict:
    df = today[today["pct_change"].notna()].copy()
    df["name"] = df["symbol"].map(names).fillna("")
    df["industry"] = df["symbol"].map(industry).fillna("未知")
    df = df[df["prev_close"].notna()]  # 剔除新股首日
    df = df[~df["category"].isin([rs.CAT_CORP_ACTION, "missing_pct"])]  # 剔除公司行为/无涨跌幅数据
    if not include_st:
        df = df[~df["is_st"]]
    df = df.sort_values("pct_change", ascending=False)

    def _pick(sub: pd.DataFrame, cols: list[str]) -> list[dict]:
        return [
            {
                c: (round(float(r[c]), 2) if c == "pct_change" else r[c])
                for c in cols
            }
            for _, r in sub.head(20).iterrows()
        ]

    gainers = _pick(df, ["symbol", "name", "pct_change", "industry", "category"])
    losers = _pick(df.sort_values("pct_change"), ["symbol", "name", "pct_change", "industry", "category"])

    amt = today[today["prev_close"].notna()].copy()
    amt = amt[~amt["category"].isin([rs.CAT_CORP_ACTION, "missing_pct"])]
    amt["name"] = amt["symbol"].map(names).fillna("")
    amt["industry"] = amt["symbol"].map(industry).fillna("未知")
    if not include_st:
        amt = amt[~amt["is_st"]]
    amt = amt.sort_values("amount", ascending=False)
    amount_top = [
        {
            "symbol": r["symbol"],
            "name": r["name"],
            "amount_yi": round(float(r["amount"]) / 1e4, 2),
            "industry": r["industry"],
            "pct": round(float(r["pct_change"]), 2) if pd.notna(r["pct_change"]) else None,
        }
        for _, r in amt.head(20).iterrows()
    ]

    tu = turnover.dropna().sort_values(ascending=False).head(10)
    turnover_top = [
        {
            "symbol": s,
            "name": names.get(s, ""),
            "industry": industry.get(s, "未知"),
            "turnover_pct": round(float(v), 2),
        }
        for s, v in tu.items()
    ]
    return {"gainers": gainers, "losers": losers, "amount_top": amount_top, "turnover_top": turnover_top}


def load_watch_list(
    today: pd.DataFrame,
    names: dict[str, str],
    industry: dict[str, str],
    turnover: pd.Series,
    watch: list[str],
) -> list[dict]:
    out = []
    df = today.set_index("symbol")
    for sym in watch:
        if sym not in df.index:
            out.append({"symbol": sym, "name": names.get(sym, ""), "note": "当日无数据"})
            continue
        r = df.loc[sym]
        if isinstance(r, pd.DataFrame):
            r = r.iloc[0]
        out.append(
            {
                "symbol": sym,
                "name": names.get(sym, ""),
                "industry": industry.get(sym, "未知"),
                "close": round(float(r["close"]), 2),
                "pct": round(float(r["pct_change"]), 2) if pd.notna(r["pct_change"]) else None,
                "amount_yi": round(float(r["amount"]) / 1e4, 2),
                "turnover_pct": round(float(turnover.get(sym)), 2) if pd.notna(turnover.get(sym)) else None,
                "ma5": round(float(r["ma5"]), 2) if pd.notna(r["ma5"]) else None,
                "ma20": round(float(r["ma20"]), 2) if pd.notna(r["ma20"]) else None,
                "category": r["category"],
                "ex_div": bool(r["ex_div"]),
            }
        )
    return out


def load_preflight(db: duckdb.DuckDBPyConnection, data_dir: Path, trade_date: str) -> list[dict]:
    checks = [
        ("daily_unadjusted", "1_kline_data/daily_unadjusted"),
        ("daily_forward", "1_kline_data/daily_forward"),
        ("daily_backward", "1_kline_data/daily_backward"),
        ("index_daily", "1_kline_data/index_daily"),
        ("technical_indicators", "5_technical_derived/technical_indicators"),
        ("market_sentiment", "5_technical_derived/market_sentiment"),
        ("valuation", "5_technical_derived/valuation"),
        ("margin_trading", "2_base_sector/margin_trading"),
        ("l1_factors", "6_ml_datasets/l1_factors"),
        ("l1_l2_factors", "6_ml_datasets/l1_l2_factors"),
    ]
    out = []
    for name, rel in checks:
        rows = q1(
            db,
            f"SELECT max(dt) FROM read_parquet('{data_dir}/{rel}/dt=*/data.parquet',"
            f" hive_partitioning=true)",
        )
        latest = rows[0][0] if rows and rows[0] else None
        status = "ok" if latest == trade_date else ("stale" if latest else "error")
        out.append({"dataset": name, "latest": latest, "status": status, "note": STALE_NOTES.get(name, "")})
    return out


def render_facts(stats: dict) -> str:
    L: list[str] = []
    meta = stats["meta"]
    L.append(f"# 每日复盘事实清单 {meta['trade_date']}（{meta['weekday']}）\n")
    if meta["prev_date"]:
        L.append(f"> **报告日期**：{meta['trade_date']}　**前一交易日**：{meta['prev_date']}\n")
    if meta["stale"]:
        L.append("## 数据滞后声明\n")
        for k, v in meta["stale"].items():
            L.append(f"- {k}：{v}")
        L.append("")

    L.append("## 一、大盘指数\n")
    L.append("| 指数 | 收盘 | 涨跌幅 | 成交额 | 量比 | vs MA5 | vs MA20 |")
    L.append("|---|---|---|---|---|---|---|")
    for r in stats["index"]:
        if r.get("missing"):
            L.append(f"| {r['name']} | [数据缺失] | | | | | |")
            continue
        L.append(
            f"| {r['name']} | {r['close']} | {r['pct']:+.2f}% | {_yi(r['amount_yi'])}"
            f" | {r['vol_ratio_5']} | {r['vs_ma5']:+.2f}% | {r['vs_ma20']:+.2f}% |"
        )
    L.append("")

    m = stats["market"]
    L.append("## 二、市场广度\n")
    L.append(
        f"- 上涨 **{m['up_count']}** / 下跌 **{m['down_count']}** / 平盘 {m['flat_count']}"
        f"（涨跌比 {m['up_down_ratio']}，停牌 {m['suspended_count']}）"
    )
    L.append(
        f"- 涨停 **{m['limit_up']}** / 跌停 **{m['limit_down']}** / 炸板 {m['broke_up']}"
        f"（最高连板 **{m['max_streak']}** 板；公司行为剔除 {m.get('corp_action', 0)} 只）"
    )
    L.append(
        f"- 两市成交额 **{_yi(m['total_amount_yi'])}**"
        f"（环比上一交易日 {m['amount_ratio_prev']}x；5 日均 {_yi(m['amount_ma5_yi'])}"
        f"，量比 {m['amount_ratio_ma5']}）"
    )
    if m["streaks"]:
        top_streak = m["streaks"][:10]
        L.append(
            "- 连板梯队："
            + "、".join(f"{x['streak']}板 {x['name']}（{x['symbol']}）" for x in top_streak)
        )
    L.append("")
    L.append("| 区间 | 家数 | 区间 | 家数 | 区间 | 家数 |")
    L.append("|---|---|---|---|---|---|")
    d = m["dist"]
    labels = ["涨停", ">7", "5~7", "3~5", "1~3", "0~1", "平盘", "-1~0", "-3~-1", "-5~-3", "-7~-5", "<-7", "跌停"]
    for i in range(0, 5):
        L.append(
            f"| {labels[i]} | {d.get(labels[i], 0)} | {labels[i+6]} | {d.get(labels[i+6], 0)}"
            f" | {labels[i+7] if i+7 < len(labels) else ' '} | {d.get(labels[i+7], '') if i+7 < len(labels) else ''} |"
        )
    L.append("")

    sec = stats["sectors"]
    L.append("## 三、板块表现（等权平均涨跌幅）\n")
    for stype, title in [("行业板块(一级)", "行业（一级）"), ("概念板块", "概念板块")]:
        rows = sec.get(stype, [])
        bottom = sec.get(f"{stype}_bottom", [])
        if not rows and not bottom:
            continue
        L.append(f"### {title}\n")
        L.append("| 板块 | 家数 | 平均涨跌幅 | 市值加权 |")
        L.append("|---|---|---|---|")
        for s in rows:
            mv_s = f"{s['mv_weighted_pct']:+.2f}%" if s.get("mv_weighted_pct") is not None else "—"
            L.append(f"| {s['SectorName']} | {s['n']} | {s['avg_pct']:+.2f}% | {mv_s} |")
        L.append("")
        for s in reversed(bottom):
            mv_s = f"{s['mv_weighted_pct']:+.2f}%" if s.get("mv_weighted_pct") is not None else "—"
            L.append(f"| {s['SectorName']} | {s['n']} | {s['avg_pct']:+.2f}% | {mv_s} |")
        L.append("")

    st = stats.get("sentiment")
    if st:
        L.append("## 四、市场情绪\n")
        L.append(f"- 样本 {st['n']} 只：买压均值 {st['buy_pressure_mean']} / 卖压均值 {st['sell_pressure_mean']}")
        L.append(
            f"- 日内动量 1d 均值 {st['momentum_1d_mean']} / 3d 均值 {st['momentum_3d_mean']}；"
            f"早盘时段上涨占比 {st['am_pm_up_pct']}%"
        )
        L.append("")

    fu = stats.get("funds", {})
    L.append("## 五、资金面\n")
    mg = fu.get("margin", {})
    if mg.get("latest_date"):
        L.append(
            f"- 两融余额（截至 {mg['latest_date']}）：融资余额 {_yi(mg['finance_balance_yi'])}"
            + (
                f"（环比 {mg['finance_balance_delta_yi']:+.2f} 亿）"
                if mg.get("finance_balance_delta_yi") is not None
                else ""
            )
            + "；融券净额 " + _yi(mg["slo_net_yi"])
        )
    else:
        L.append("- 两融余额：[数据缺失]")
    n = fu.get("north", {})
    if n.get("quarter"):
        L.append(
            f"- 北向资金：{n['quarter']} 季度快照（报告期 {n['report_date']}）持仓市值 {_yi(n['total_value_yi'])}，"
            f"覆盖 {n['stocks']} 只（2024-08 起日频改季度披露）"
        )
    L.append(f"- 主力资金：{fu.get('flow_note', '')}")
    L.append("")

    L.append("## 六、个股榜（剔除 ST 与新股首日）\n")
    L.append("### 涨幅榜 Top20\n")
    L.append("| 名称 | 代码 | 涨跌幅 | 行业 | 状态 |")
    L.append("|---|---|---|---|---|")
    for r in stats["top"]["gainers"]:
        L.append(f"| {r['name']} | {r['symbol']} | {r['pct_change']:+.2f}% | {r['industry']} | {r['category']} |")
    L.append("")
    L.append("### 跌幅榜 Bottom20\n")
    L.append("| 名称 | 代码 | 涨跌幅 | 行业 | 状态 |")
    L.append("|---|---|---|---|---|")
    for r in stats["top"]["losers"]:
        L.append(f"| {r['name']} | {r['symbol']} | {r['pct_change']:+.2f}% | {r['industry']} | {r['category']} |")
    L.append("")
    L.append("### 成交额榜 Top20\n")
    L.append("| 名称 | 代码 | 成交额 | 行业 | 涨跌幅 |")
    L.append("|---|---|---|---|---|")
    for r in stats["top"]["amount_top"]:
        L.append(f"| {r['name']} | {r['symbol']} | {_yi(r['amount_yi'])} | {r['industry']} | {r['pct']:+.2f}% |")
    L.append("")
    L.append("### 换手率榜 Top10\n")
    L.append("| 名称 | 代码 | 换手率 | 行业 |")
    L.append("|---|---|---|---|")
    for r in stats["top"]["turnover_top"]:
        L.append(f"| {r['name']} | {r['symbol']} | {r['turnover_pct']:.2f}% | {r['industry']} |")
    L.append("")

    if stats.get("watch"):
        L.append("## 七、自选/持仓复盘\n")
        L.append("| 名称 | 代码 | 收盘 | 涨跌幅 | 成交额 | 换手率 | MA20 | 行业 | 状态 |")
        L.append("|---|---|---|---|---|---|---|---|---|")
        for r in stats["watch"]:
            if r.get("note"):
                L.append(f"| {r['name']} | {r['symbol']} | {r['note']} |")
                continue
            ma20_s = f"{r['ma20']:.2f}" if r.get("ma20") else "[缺失]"
            L.append(
                f"| {r['name']} | {r['symbol']} | {r['close']} | {r['pct']:+.2f}% | {_yi(r['amount_yi'])}"
                f" | {r['turnover_pct']:.2f}% | {ma20_s} | {r['industry']} | {r['category']} |"
            )
        L.append("")

    L.append("## 数据说明\n")
    L.append("- 单位口径：[quantdb-fields] 技能。个股 volume=股、amount=万元（本清单已折算为亿元）；指数 volume=手。")
    L.append("- 涨停判定：收盘价 vs 涨停价（沪深四舍五入 / 北交所截尾进位），容差 SH/SZ 0.5%、BJ 1%；除权日改用官方 pct_change 兜底。")
    L.append("- 板块涨跌幅为成员等权平均；市值加权列仅在流通市值覆盖 ≥60% 时提供。")
    return "\n".join(L)


def main() -> None:
    ap = argparse.ArgumentParser(description="每日复盘取数")
    ap.add_argument("--date", help="复盘日期 YYYYMMDD，默认最新交易日")
    ap.add_argument("--data-dir", help="QuantDB 数据目录，默认自动探测")
    ap.add_argument("--out-dir", help="输出目录，默认 <repo>/data/reports/daily_review")
    ap.add_argument("--watch", help="自选/持仓股逗号分隔，如 601138.SH,600519.SH")
    ap.add_argument("--include-st", action="store_true", help="个股榜保留 ST")
    args = ap.parse_args()

    data_dir = Path(args.data_dir) if args.data_dir else default_data_dir()
    reports_root = Path(os.getenv("QM_REPORTS_DIR", str(_REPO_ROOT / "data" / "reports")))
    out_dir = Path(args.out_dir) if args.out_dir else reports_root / "daily_review"
    out_dir.mkdir(parents=True, exist_ok=True)

    db = duckdb.connect()
    trade_date = resolve_trade_date(db, data_dir, args.date)
    prev_dates = load_trading_days(db, data_dir, 2, trade_date)
    prev_date = prev_dates[1] if len(prev_dates) > 1 else ""
    trade_dt_obj = pd.Timestamp(trade_date)

    import os as _os
    _inst_path = f"{data_dir}/2_base_sector/instrument_detail/instrument_list.parquet"
    if not _os.path.exists(_inst_path):
        _inst_path = f"{data_dir}/2_base_sector/instrument_detail/instrument_detail.parquet"
    names_df = q(
        db,
        f"SELECT Symbol, Name FROM read_parquet("
        f"'{_inst_path}')",
    )
    names = dict(zip(names_df["Symbol"], names_df["Name"]))
    st_set = {s for s, n in names.items() if "ST" in n}

    members_df = q(
        db,
        f"SELECT SectorCode, SectorName, SectorType, Symbol FROM read_parquet("
        f"'{data_dir}/2_base_sector/sector_concept/sector_members.parquet')",
    )
    ind1 = members_df[members_df["SectorType"] == "行业板块(一级)"]
    ind2 = members_df[members_df["SectorType"] == "行业板块(二级)"]
    map1 = ind1.drop_duplicates(subset=["Symbol"]).set_index("Symbol")["SectorName"].to_dict()
    map2 = ind2.drop_duplicates(subset=["Symbol"]).set_index("Symbol")["SectorName"].to_dict()
    industry = {s: map1.get(s, map2.get(s, "未知")) for s in set(map1) | set(map2)}

    today, pct_series, _ = build_today(db, data_dir, trade_date, prev_date, st_set)
    mv, cap = load_valuation_latest(db, data_dir, trade_date)
    turnover: pd.Series = pd.Series(dtype=float)
    if not cap.empty:
        vol_today = today.set_index("symbol")["volume"]
        turnover = (vol_today / cap.where(cap > 0) * 100).dropna()

    market = load_market_stats(db, data_dir, trade_date, today, st_set, names)
    sectors = load_sector_stats(db, data_dir, pct_series, mv if not mv.empty else None)
    sentiment = load_sentiment_stats(db, data_dir, trade_date)
    funds = load_fund_stats(db, data_dir, trade_date)
    index_rows = load_index_stats(db, data_dir, trade_date)
    top = load_top_lists(today, names, industry, turnover, include_st=args.include_st)
    watch = (
        load_watch_list(today, names, industry, turnover, [s.strip() for s in args.watch.split(",") if s.strip()])
        if args.watch
        else []
    )

    weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    preflight = load_preflight(db, data_dir, trade_date)
    stale = {
        p["dataset"]: (f"{p['note']}（最新 {p['latest']}）" if p["note"] else f"最新 {p['latest']}")
        for p in preflight
        if p["status"] == "stale"
    }
    stats = {
        "meta": {
            "trade_date": str(trade_dt_obj.date()),
            "weekday": weekdays[trade_dt_obj.weekday()],
            "prev_date": str(pd.Timestamp(prev_date).date()) if prev_date else "",
            "stale": stale,
        },
        "preflight": preflight,
        "index": index_rows,
        "market": market,
        "sectors": sectors,
        "sentiment": sentiment,
        "funds": funds,
        "top": top,
        "watch": watch,
    }

    json_path = out_dir / f"{trade_dt_obj.date()}_stats.json"
    facts_path = out_dir / f"{trade_dt_obj.date()}_facts.md"
    json_path.write_text(json.dumps(stats, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    facts_path.write_text(render_facts(stats), encoding="utf-8")
    print(f"统计 JSON: {json_path}")
    print(f"事实清单:   {facts_path}")
    print(
        f"复盘日 {trade_dt_obj.date()}（{weekdays[trade_dt_obj.weekday()]}）："
        f"涨停 {market['limit_up']} / 跌停 {market['limit_down']} / 炸板 {market['broke_up']}，"
        f"成交额 {_yi(market['total_amount_yi'])}"
    )


if __name__ == "__main__":
    main()