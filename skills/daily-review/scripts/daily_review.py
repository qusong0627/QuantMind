"""每日复盘取数脚本：读 QuantDB 本地 parquet，产出 stats JSON + facts Markdown。

用法:
    python3 daily_review.py                  # 最新交易日
    python3 daily_review.py --date 20260814  # 指定交易日
    python3 daily_review.py --watch 601138.SH,600519.SH
    python3 daily_review.py --watch-note "持仓源停更说明"   # 盘后流水线传入；无 --watch 也渲染该段

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
import logging
import sys
from datetime import date
from pathlib import Path

import duckdb
import pandas as pd

logger = logging.getLogger("daily_review")


def _f(v) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    return f if f == f else 0.0


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

STALE_NOTES: dict[str, str] = {
    "l2_factors": "",  # L2 已恢复日更（2026-08 复测），无滞后
    "min1_kline": "停更 2026-07-24",
    "min5_kline": "停更 2026-07-24",
    "hsgt_north": "北向 2024-08 起改季度披露，只有季度快照",
}

# ── 因子透视（升级版复盘：L1/L2 全市场截面 + 板块资金流）──
# 14 个推荐因子皆正向 alpha（值越高=信号越强），见 l2_recommended_factors.csv
L2_POSITIVE_FACTORS: list[str] = [
    "micro_vpin_vol_ratio", "micro_vpin_amount_ratio", "micro_pin",
    "micro_zone_distribution", "micro_zone_vol_ratio_T4", "micro_zone_vol_ratio_T6",
    "micro_zone_vol_ratio_T5", "micro_zone_vol_ratio_T3", "micro_zone_rv_ratio_close",
    "vol_price_divergence", "micro_open_gap", "micro_impact_decay_half_life",
    "micro_liquidity_daily_pattern", "flow_imbalance_revert_speed",
]
L2_VPIN_FACTORS: list[str] = ["micro_vpin_vol_ratio", "micro_vpin_amount_ratio", "micro_pin"]
L1_KEY_FACTORS: list[str] = [
    "turn_1", "turn_5", "mom_ret_1d", "mom_ret_5d", "mom_ret_10d",
    "vol_std_20", "vol_atr_14", "style_idio_vol_20",
]


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
    # 兜底：官方分区缺行时（供应商发布中断），用后复权序列补 pct_change/ma5/ma20。
    # 后复权相邻收盘比 = 官方涨跌幅（含除权除息口径）。
    need_days = load_trading_days(db, data_dir, 25, max(dts))
    if len(need_days) > 1:
        day_in = ",".join(f"'{d}'" for d in need_days)
        closes = q(
            db,
            f"SELECT symbol, dt, close FROM read_parquet("
            f"'{data_dir}/1_kline_data/daily_backward/dt=*/data.parquet',"
            f" hive_partitioning=true) WHERE dt IN ({day_in})",
        )
        closes["dt"] = closes["dt"].astype(str)
        fb = tech_fallback_from_backward(closes, sorted(need_days))
        fb = fb[fb["dt"].isin(dts)]
        if not fb.empty:
            df = df.merge(fb, on=["symbol", "dt"], how="outer", suffixes=("", "_fb"))
            for col in ("pct_change", "ma5", "ma20"):
                df[col] = df[col].combine_first(df[f"{col}_fb"])
            df = df[["symbol", "dt", "pct_change", "ma5", "ma20"]]
    return df


def tech_fallback_from_backward(closes: pd.DataFrame, dts: list[str]) -> pd.DataFrame:
    """用后复权收盘序列兜底 pct_change/ma5/ma20（官方 tech_ind 缺行时）。

    closes: [symbol, dt, close]；dts: 升序交易日列表。
    返回 [symbol, dt, pct_change, ma5, ma20]，覆盖 dts[1:]（首日无前收 → 剔除）。
    停牌缺行按 ffill 处理（pct=0 且参与均线）；历史不足 5/20 日 ma 为 NaN。
    """
    closes = closes.drop_duplicates(["symbol", "dt"])
    full = closes.pivot(index="symbol", columns="dt", values="close")
    full = full.reindex(columns=dts)
    full = full.ffill(axis=1)
    pct = full.diff(axis=1) / full.shift(axis=1) * 100.0
    ma5 = full.T.rolling(5, min_periods=5).mean().T
    ma20 = full.T.rolling(20, min_periods=20).mean().T
    rows: list[pd.DataFrame] = []
    for d in dts:
        if d not in pct.columns:
            continue
        rows.append(
            pd.DataFrame(
                {
                    "symbol": full.index,
                    "dt": d,
                    "pct_change": pct[d].to_numpy(),
                    "ma5": ma5[d].to_numpy(),
                    "ma20": ma20[d].to_numpy(),
                }
            )
        )
    out = pd.concat(rows, ignore_index=True)
    return out[out["pct_change"].notna()]


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
        f"SELECT symbol, dt, high, low, close, amount FROM read_parquet("
        f"'{data_dir}/1_kline_data/index_daily/dt=*/data.parquet', hive_partitioning=true)"
        f" WHERE CAST(dt AS VARCHAR) IN ({dt_in}) AND CAST(symbol AS VARCHAR) IN ({sym_in})",
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
        tech12 = load_tech(db, data_dir, dts12)
        tech12 = tech12[tech12["symbol"].isin(limit_today)]
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
        f"SELECT max(dt) FROM read_parquet('{data_dir}/6_ml_datasets/l2_factors/dt=*/data.parquet',"
        f" hive_partitioning=true)",
    )
    out["l2_latest"] = rows[0][0] if rows and rows[0] else None
    out["l2_note"] = STALE_NOTES["l2_factors"]
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
        ("l2_factors", "6_ml_datasets/l2_factors"),
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


def load_factor_stats(
    db: duckdb.DuckDBPyConnection,
    data_dir: Path,
    trade_date: str,
    prev_date: str,
    industry_map: dict[str, str],
) -> dict:
    """L1/L2 因子全市场截面 + 板块超级大单资金流（升级版复盘数据）。

    返回 { l1: {col: {now, prev, delta}}, l2: {strong_pct, vpin_mean, divergence_mean,
    divergence_prev, super_net_yi, super_prev_yi}, sector_flow: [{industry, net_yi, n}] }。
    任一数据集缺失时返回空 dict 对应键。
    """
    out: dict = {}

    # ── L1 换手/动量/波动 关键因子均值（当日 vs 前一日）──
    l1_now: dict[str, float] = {}
    l1_prev: dict[str, float] = {}
    try:
        l1_files = (
            f"{data_dir}/6_ml_datasets/l1_factors/dt={trade_date}/data.parquet",
            f"{data_dir}/6_ml_datasets/l1_factors/dt={prev_date}/data.parquet",
        )
        dfn = q(db, f"SELECT * FROM read_parquet('{l1_files[0]}')")
        dfp = q(db, f"SELECT * FROM read_parquet('{l1_files[1]}')")
        avail = [c for c in L1_KEY_FACTORS if c in dfn.columns]
        for c in avail:
            if dfn[c].notna().mean() < 0.3:
                continue
            now_v = float(dfn[c].mean())
            prev_v = float(dfp[c].mean()) if c in dfp.columns and dfp[c].notna().mean() >= 0.3 else None
            l1_now[c] = round(now_v, 4)
            if prev_v is not None:
                l1_prev[c] = round(prev_v, 4)
        if l1_now:
            out["l1"] = {
                c: {"now": l1_now[c], "prev": l1_prev.get(c), "delta": round(l1_now[c] - l1_prev[c], 4) if c in l1_prev else None}
                for c in l1_now
            }
    except Exception as exc:  # noqa: BLE001
        logger.warning("load l1 factor stats failed: %s", exc)

    # ── L2 微观结构：正向因子强度 + VPIN + 量价背离 + 超级大单 ──
    try:
        l2f_now = f"{data_dir}/6_ml_datasets/l2_factors/dt={trade_date}/data.parquet"
        l2f_prev = f"{data_dir}/6_ml_datasets/l2_factors/dt={prev_date}/data.parquet"
        avail = [c for c in L2_POSITIVE_FACTORS if c in q(db, f"SELECT * FROM read_parquet('{l2f_now}') LIMIT 0").columns]
        sel = ", ".join(avail + ["symbol", "flow_super_net", "vol_price_divergence"])
        dn_f = q(db, f"SELECT {sel} FROM read_parquet('{l2f_now}')")
        dp_f = q(db, f"SELECT {sel} FROM read_parquet('{l2f_prev}')")
        for c in avail:
            dn_f[c] = pd.to_numeric(dn_f[c], errors="coerce")
            dp_f[c] = pd.to_numeric(dp_f[c], errors="coerce")

        # 每因子当日截面百分位（0~1），股票 = 各因子平均百分位 → 强信号股（≥0.65）
        pct_cols = {}
        for c in avail:
            s = dn_f[c].rank(pct=True)
            pct_cols[c] = s
        strength = pd.concat(pct_cols, axis=1).mean(axis=1)
        strong_pct = float((strength >= 0.65).mean()) if len(strength) else None

        vpin_avail = [c for c in L2_VPIN_FACTORS if c in avail]
        vpin_mean = (
            round(float(pd.concat([pct_cols[c] for c in vpin_avail], axis=1).mean(axis=1).median()), 3)
            if vpin_avail else None
        )
        divergence_mean = round(float(dn_f["vol_price_divergence"].mean()), 3)
        divergence_prev = round(float(dp_f["vol_price_divergence"].mean()), 3) if not dp_f.empty else None
        # flow_super_net 单位=元 → 亿
        super_net_yi = round(float(dn_f["flow_super_net"].sum()) / 1e8, 2) if "flow_super_net" in dn_f.columns else None
        super_prev_yi = round(float(dp_f["flow_super_net"].sum()) / 1e8, 2) if not dp_f.empty and "flow_super_net" in dp_f.columns else None

        out["l2"] = {
            "date": trade_date,
            "strong_pct": round(strong_pct, 4) if strong_pct is not None else None,
            "vpin_mean": vpin_mean,
            "divergence_mean": divergence_mean,
            "divergence_prev": divergence_prev,
            "super_net_yi": super_net_yi,
            "super_prev_yi": super_prev_yi,
        }

        # ── 板块资金流：按申万一级聚合超级大单净额（亿）──
        sf = dn_f[["symbol", "flow_super_net"]].copy()
        sf["industry"] = sf["symbol"].map(industry_map).fillna("未知")
        grp = sf.groupby("industry")["flow_super_net"].sum()
        cnt = sf.groupby("industry")["flow_super_net"].count()
        sector_flow = [
            {"industry": k, "net_yi": round(v / 1e8, 2), "n": int(cnt.get(k, 0))}
            for k, v in grp.sort_values(ascending=False).items()
        ]
        out["sector_flow"] = sector_flow
    except Exception as exc:  # noqa: BLE001
        logger.warning("load l2 factor stats failed: %s", exc)
        out.setdefault("l2", None)

    return out


def render_watch_section(stats: dict) -> str:
    """§九 自选/持仓复盘（含**持仓源注记**）。整段独立成函数：注记是数据可信度声明，
    单独可测——快照停更/缺失时该段必须「标注 + 空表」，绝不拿旧名单编（T3-1，
    2026-10-10：盘后链曾读 2026-09-29 起停更的僵尸账本，每晚照编）。"""
    watch = stats.get("watch") or []
    note = str(stats.get("watch_note") or "").strip()
    if not watch and not note:
        return ""
    parts = ["## 九、自选/持仓复盘\n"]
    if note:
        parts.append(f"> {note}\n")
    if watch:
        parts.append("| 名称 | 代码 | 收盘 | 涨跌幅 | 成交额 | 换手率 | MA20 | 行业 | 状态 |")
        parts.append("|---|---|---|---|---|---|---|---|---|")
        for r in watch:
            if r.get("note"):
                parts.append(f"| {r['name']} | {r['symbol']} | {r['note']} |")
                continue
            ma20_s = f"{r['ma20']:.2f}" if r.get("ma20") else "[缺失]"
            parts.append(
                f"| {r['name']} | {r['symbol']} | {r['close']} | {r['pct']:+.2f}% | {_yi(r['amount_yi'])}"
                f" | {r['turnover_pct']:.2f}% | {ma20_s} | {r['industry']} | {r['category']} |"
            )
    parts.append("")
    return "\n".join(parts)


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

    # ── 升级：行业多日涨跌幅（1/3/5 日，提炼市场分析口径）──
    smd = stats.get("sector_multiday") or {}
    smd_items = smd.get("items") or []
    if smd_items:
        L.append("### 行业多日涨跌幅（成分股中位数，5 日榜 Top10/Bottom10）\n")
        L.append("| 行业 | 1日% | 3日% | 5日% |")
        L.append("|---|---|---|---|")
        for it in smd_items[:10]:
            L.append(f"| {it['name']} | {it['pct_1d']:+.2f}% | {it['pct_3d']:+.2f}% | {it['pct_5d']:+.2f}% |")
        for it in reversed(smd_items[-10:]):
            L.append(f"| {it['name']} | {it['pct_1d']:+.2f}% | {it['pct_3d']:+.2f}% | {it['pct_5d']:+.2f}% |")
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
    L.append(f"- 主力资金（L2）：{fu.get('l2_note', '')}")
    L.append("")

    # ── 升级：板块主力资金 1/5/10 日（提炼市场分析口径）──
    sf = stats.get("sector_flow") or {}
    sf_items = sf.get("items") or []
    if sf_items:
        L.append("### 板块主力资金净流入（亿元，1/5/10 日 Top10）\n")
        L.append("| 行业 | 1日 | 5日 | 10日 |")
        L.append("|---|---|---|---|")
        for it in sf_items[:10]:
            L.append(f"| {it['name']} | {it['flow_1d_yi']:+.2f} | {it['flow_5d_yi']:+.2f} | {it['flow_10d_yi']:+.2f} |")
        L.append("")
        neg10 = sorted(sf_items, key=lambda x: x["flow_10d_yi"])[:10]
        L.append("**10 日净流出 Top10**\n")
        L.append("| 行业 | 1日 | 5日 | 10日 |")
        L.append("|---|---|---|---|")
        for it in neg10:
            L.append(f"| {it['name']} | {it['flow_1d_yi']:+.2f} | {it['flow_5d_yi']:+.2f} | {it['flow_10d_yi']:+.2f} |")
        L.append("")

    # ── 六、新闻情绪（当日有新闻的股票）──
    nv = stats.get("news")
    if nv:
        stocks = nv.get("stocks") or []
        stock_count = nv.get("stock_count") or len(stocks)
        total_n = int(_f(nv.get("n")))
        bull = int(_f(nv.get("bullish"))); bear = int(_f(nv.get("bearish")))
        net_ratio = (bull - bear) / total_n if total_n else 0
        L.append("## 六、新闻情绪（当日有新闻的股票匹配）\n")
        L.append(
            f"- 当日命中新闻 **{total_n}** 篇 / 涉及股票 **{stock_count}** 只；"
            f"利好 {bull} / 利空 {bear} / 中性 {int(_f(nv.get('neutral')))} → 净情绪 {net_ratio:+.0%}"
        )
        L.append(
            f"- 来源质量：高质量源 {int(_f(nv.get('gold_news')))} 篇 / 反向源 {int(_f(nv.get('reverse_news')))} 篇"
            f"；黄金时段(19-22点)利好 {int(_f(nv.get('golden_hour_bullish')))}/{int(_f(nv.get('golden_hour_total')))} 篇"
        )
        # 升级：新闻-股价联动（当日有新闻股票的走势 vs 全市场）
        pl = nv.get("price_link")
        if pl:
            excess_txt = ""
            if pl.get("market_avg_pct") is not None and pl.get("excess_pct") is not None:
                win = "跑赢" if pl["excess_pct"] > 0 else "跑输"
                excess_txt = f"（相对全市场超额 {pl['excess_pct']:+.2f}%，{win}）"
            L.append(
                f"- **新闻-股价联动**：当日有新闻的 {pl['n']} 只股票平均涨幅 "
                f"**{pl['avg_pct']:+.2f}%**{excess_txt}；"
            )
            if pl.get("bull_avg_pct") is not None and pl.get("bear_avg_pct") is not None:
                L.append(
                    f"  看多新闻股平均 {pl['bull_avg_pct']:+.2f}% vs 看空新闻股 {pl['bear_avg_pct']:+.2f}%"
                    f"（差 {pl['bull_avg_pct'] - pl['bear_avg_pct']:+.2f}pp，正=新闻情绪对股价有预测力）"
                )
        sf = nv.get("sector_focus")
        if sf:
            L.append("### 新闻聚焦板块（按有新闻股票的行业聚合）\n")
            L.append("| 行业 | 有新闻股票数 | 新闻数 | 净情绪 |")
            L.append("|---|---|---|---|")
            for sr in sf[:12]:
                L.append(
                    f"| {sr['industry']} | {sr['n']} | {sr['news']} | {sr['net_ratio']:+.0%} |"
                )
            L.append("")
        L.append("### 当日有新闻个股（新闻数 + 净情绪 + 当日涨跌，Top15）\n")
        L.append("| 名称 | 代码 | 当日涨跌 | 篇数 | 利好 | 利空 | 净情绪 | 事件标签 |")
        L.append("|---|---|---|---|---|---|---|---|")
        for st in stocks[:15]:
            tags = "、".join((st.get("tags") or [])[:3]) if st.get("tags") else ""
            pct_txt = f"{st['today_pct']:+.2f}%" if st.get("today_pct") is not None else "—"
            L.append(
                f"| {st.get('name','')} | {st['symbol']} | {pct_txt} | {st['news_count']} | {st.get('bullish',0)}"
                f" | {st.get('bearish',0)} | {st.get('net_ratio',0):+.0%} | {tags} |"
            )
        L.append("")
    else:
        L.append("## 六、新闻情绪\n")
        L.append("- 未运行 news_review.py（容器内）：无当日新闻情绪数据。先执行 `docker exec quantmind python3 /app/skills/daily-review/scripts/news_review.py --date {date}` 补上，方向研判会加回该维度。".replace("{date}", stats.get("meta", {}).get("trade_date", "").replace("-", "")))
        L.append("")

    # ── 七、L1/L2 因子透视 + 板块资金流 ──
    fv = stats.get("factors") or {}
    if fv:
        L.append("## 七、L1/L2 因子透视\n")
        l1 = fv.get("l1") or {}
        if l1:
            L.append("### L1 换手/动量/波动（全市场均值）\n")
            L.append("| 因子 | 当日 | 前日 | 变化 |")
            L.append("|---|---|---|---|")
            label = {
                "turn_1": "换手率1日", "turn_5": "换手率5日", "mom_ret_1d": "动量1日",
                "mom_ret_5d": "动量5日", "mom_ret_10d": "动量10日", "vol_std_20": "波动率20日",
                "vol_atr_14": "ATR14", "style_idio_vol_20": "特质波动20日",
            }
            for c, v in l1.items():
                p = v.get("prev"); d = v.get("delta")
                dl = "" if d is None else f"{d:+.3f}" if abs(float(d)) >= 0.001 else ""
                L.append(f"| {label.get(c, c)} | {v['now']:.4f} | {p if p is not None else '—'} | {dl} |")
            L.append("")
        l2 = fv.get("l2") or {}
        if l2:
            strong = l2.get("strong_pct")
            L.append("### L2 微观结构（全市场截面）\n")
            L.append(f"- 正向因子强信号股占比：**{strong:.0%}**（14 推荐因子截面均值≥65 分位；越高=知情资金越扩散）" if strong is not None else "- 正向因子强信号占比：[缺失]")
            L.append(f"- VPIN 家族全市场中位分位：**{l2.get('vpin_mean')}**；量价背离均值 {l2.get('divergence_mean')}（前日 {l2.get('divergence_prev')}）")
            L.append(
                f"- 超级大单净额：**{l2.get('super_net_yi'):+.2f} 亿**（前日 {l2.get('super_prev_yi'):+.2f} 亿）"
                if l2.get("super_net_yi") is not None else "- 超级大单净额：[缺失]"
            )
            L.append("")
        sf2 = fv.get("sector_flow") or []
        if sf2:
            L.append("### 板块超级大单净额（亿，申万一级）\n")
            L.append("| 净流入 | 净流出 |")
            L.append("|---|---|")
            inflow = [r for r in sf2 if r["net_yi"] >= 0][:8]
            outflow = [r for r in sf2 if r["net_yi"] < 0][-8:]
            mlen = max(len(inflow), len(outflow))
            for i in range(mlen):
                li = f"{inflow[i]['industry']} {inflow[i]['net_yi']:+.1f}亿" if i < len(inflow) else ""
                lo = f"{outflow[i]['industry']} {outflow[i]['net_yi']:+.1f}亿" if i < len(outflow) else ""
                L.append(f"| {li} | {lo} |")
            L.append("")

    L.append("## 八、个股榜（剔除 ST 与新股首日）\n")
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

    watch_md = render_watch_section(stats)
    if watch_md:
        L.append(watch_md)

    # ── 十、次日走势研判（方向引擎，六维加权）──
    d = stats.get("direction") or {}
    if d.get("direction"):
        L.append("## 十、次日走势研判\n")
        L.append(
            f"> **方向：{d['direction']}**（得分 {d['total_score']:+.2f} / 满分 {d['max_score']:.1f}）"
            f" · **置信度 {'★' * int(d.get('confidence') or 0)}**（{d.get('confidence')}/5）\n"
        )
        L.append("| 维度 | 得分 | 权重 | 依据 |")
        L.append("|---|---|---|---|")
        for dim in d.get("dimensions") or []:
            L.append(f"| {dim['name']} | {dim['score']:+.2f} | {dim['weight']} | {dim['evidence']} |")
        L.append("")
        L.append("**研判口径**：正分=看多证据、负分=看空证据、±0=中性/数据缺失；")
        L.append("方向只是六维信号的可解释合成，非预测承诺。明日以指数/广度验证：方向 + 置信度星级 + 各维依据。")
        L.append("")

    # ── 十一、模型推理信号（昨日验证 + 明日 Top5）──
    mi = stats.get("model_inference")
    if mi:
        L.append("## 十一、模型推理信号\n")
        L.append(f"- 模型：`{mi['model_id']}`（推理信号自动查询 PG，默认每日推理模型）")
        att = mi.get("inference_attempt")
        if att:
            if att.get("error"):
                L.append(f"- ⚠️ 推理自动补跑异常：{att['error']}")
            elif att.get("skip"):
                L.append(f"- ⚠️ 推理自动补跑跳过：{att['skip']}")
            else:
                ok_days = [d for d, r in att.items() if isinstance(r, dict) and r.get("ok")]
                fail_days = [d for d, r in att.items() if isinstance(r, dict) and not r.get("ok")]
                if ok_days:
                    L.append(f"- ✅ 无推理 run，已自动补跑成功：{', '.join(ok_days)}（推理约 10-30 秒）")
                for d in fail_days:
                    detail = str(att[d].get("detail", ""))[-200:]
                    L.append(f"- ⚠️ 补跑 {d} 失败：{detail}")
        prev_block = mi.get("prev_vs_today")
        if prev_block:
            runx = prev_block
            fb_tag = "（当日无推理，取最近一次）" if runx.get("fallback") else ""
            L.append(f"### 昨日推理 → 今日验证（推理 {runx['data_trade_date']} → 信号 {runx['prediction_trade_date']}）{fb_tag}\n")
            if runx.get("fallback_note"):
                L.append(f"- {runx['fallback_note']}")
            h = runx.get("hit_summary") or {}
            if h.get("n"):
                excess = h.get("excess_pct")
                if excess is None:
                    excess_txt = "（无全市场对照；"
                else:
                    win = "跑赢" if excess > 0 else "跑输"
                    excess_txt = f"（相对全市场超额 {excess:+.3f}%，{win}市场；"
                L.append(
                    f"- 前 {h['n']} 信号今日平均涨幅 **{h['avg_pct']:+.3f}%**"
                    f"{excess_txt}命中率 {h['hit_rate']*100:.0f}%，"
                    f"上涨 {h['up']}/下跌 {h['down']}，涨停 {h['limit_up']} / 跌停 {h['limit_down']}）"
                )
            L.append("\n| 排名 | 名称 | 代码 | 信号分 | 今日涨跌 | 成交额(亿) | 状态 |")
            L.append("|---|---|---|---|---|---|---|")
            for i, sig in enumerate(runx["signals"][:10], 1):
                pct_txt = f"{sig['today_pct']:+.2f}%" if sig.get("today_pct") is not None else "—"
                amt_txt = f"{sig['amount_yi']:.1f}" if sig.get("amount_yi") is not None else "—"
                L.append(f"| {i} | {sig.get('name','')} | {sig['symbol']} | {sig['fusion_score']:.4f} | {pct_txt} | {amt_txt} | {sig.get('category','')} |")
            L.append("")
        next_block = mi.get("next_top5")
        if next_block:
            runx = next_block
            tag = "（今日推理未跑，取最近一次推理）" if runx.get("fallback") else ""
            L.append(f"### 明日信号 Top5（推理 {runx['data_trade_date']} → 预测 {runx['prediction_trade_date']}）{tag}\n")
            if runx.get("fallback_note"):
                L.append(f"- {runx['fallback_note']}")
            L.append("\n| 排名 | 名称 | 代码 | 信号分 | 方向 |")
            L.append("|---|---|---|---|---|")
            for i, sig in enumerate(runx["signals"], 1):
                side = sig.get("signal_side") or "—"
                L.append(f"| {i} | {sig.get('name','')} | {sig['symbol']} | {sig['fusion_score']:.4f} | {side} |")
            L.append("")
        L.append("**口径**：信号来自模型推理 PG（fusion_score 降序）；昨日验证对照当日实际涨跌，"
                 "命中率=上涨占比，超额=信号股平均涨幅−全市场平均。推理信号仅供研究，不构成投资建议。\n")

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
    ap.add_argument("--watch-note", help="持仓段注记（盘后流水线传入：快照时刻/停更说明；无 --watch 也渲染该段）")
    ap.add_argument("--include-st", action="store_true", help="个股榜保留 ST")
    ap.add_argument("--model", help="模型推理信号 model_id，默认每日推理模型")
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

    names_df = q(
        db,
        f"SELECT Symbol, Name FROM read_parquet("
        f"'{data_dir}/2_base_sector/instrument_detail/instrument_detail.parquet')",
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
    sector_multiday = load_sector_multiday(db, data_dir, trade_date, industry)
    sector_flow = load_sector_flow_multiday(db, data_dir, trade_date, industry)
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
    factors = load_factor_stats(db, data_dir, trade_date, prev_date, industry)

    # 新闻情绪 stats（{date}_news.json 由 news_review.py 在容器内产出；缺失则方向降级）
    news_stats: dict | None = None
    news_path = out_dir / f"{trade_dt_obj.date()}_news.json"
    if news_path.exists():
        try:
            news_stats = json.loads(news_path.read_text(encoding="utf-8"))
            # 升级：给有新闻的股票注入当日涨跌幅 + 汇总（新闻股 vs 全市场、看多 vs 看空）
            if news_stats and news_stats.get("stocks"):
                market_avg = float(pct_series.dropna().mean()) if len(pct_series.dropna()) else None
                for st in news_stats["stocks"]:
                    sym = st.get("symbol", "")
                    st["today_pct"] = (
                        round(float(pct_series[sym]), 2)
                        if sym in pct_series.index and pd.notna(pct_series[sym])
                        else None
                    )
                with_pct = [s for s in news_stats["stocks"] if s.get("today_pct") is not None]
                if with_pct:
                    avg_all = sum(s["today_pct"] for s in with_pct) / len(with_pct)
                    bull_grp = [s["today_pct"] for s in with_pct if s.get("net_ratio", 0) > 0]
                    bear_grp = [s["today_pct"] for s in with_pct if s.get("net_ratio", 0) < 0]
                    news_stats["price_link"] = {
                        "n": len(with_pct),
                        "avg_pct": round(avg_all, 2),
                        "market_avg_pct": round(market_avg, 2) if market_avg is not None else None,
                        "excess_pct": round(avg_all - market_avg, 2) if market_avg is not None else None,
                        "bull_avg_pct": round(sum(bull_grp) / len(bull_grp), 2) if bull_grp else None,
                        "bear_avg_pct": round(sum(bear_grp) / len(bear_grp), 2) if bear_grp else None,
                    }
        except Exception as exc:  # noqa: BLE001
            logger.warning("read news stats failed: %s", exc)

    # ── 模型推理信号（昨日→今日验证 + 今日→明日 Top5）──
    model_inference: dict | None = None
    try:
        import inference_signals as isig
        from review_stats import inference_hit_rate

        model_id = args.model or isig.resolve_latest_model_id()
        td = trade_dt_obj.date()

        prev_run = isig.load_prev_vs_today(model_id, td, fallback=False)
        next_run = isig.load_next_top_n(model_id, td, fallback=False)

        # 无推理 run 时先自动补跑（用户要求：不能跳过；推理仅 10-30 秒）
        # 补跑特征日 = 复盘日前一交易日（prediction=复盘日供验证）+ 复盘日（供明日信号）
        inference_attempt: dict[str, Any] = {}
        if not prev_run or not next_run:
            try:
                import shutil
                import subprocess

                if shutil.which("docker"):
                    script_src = Path(__file__).resolve().parent / "trigger_inference.py"
                    subprocess.run(
                        ["docker", "cp", str(script_src), "quantmind:/tmp/"],
                        capture_output=True, timeout=60,
                    )
                    prev_date_str = prev_date if prev_date else ""
                    for d in (prev_date_str, trade_date):
                        if not d:
                            continue
                        p = subprocess.run(
                            ["docker", "exec", "quantmind", "python3",
                             "/tmp/trigger_inference.py", "--date", d],
                            capture_output=True, text=True, timeout=600,
                        )
                        out = (p.stdout or "").strip().splitlines()[-1] if p.stdout else ""
                        inference_attempt[d] = {
                            "ok": p.returncode == 0,
                            "detail": out or (p.stderr or "").strip()[-300:],
                        }
                        if p.returncode == 0:
                            # 补跑成功 → 重新查询
                            prev_run = isig.load_prev_vs_today(model_id, td, fallback=False)
                            next_run = isig.load_next_top_n(model_id, td, fallback=False)
                else:
                    inference_attempt = {"skip": "docker 不可用（宿主机无 docker CLI）"}
            except Exception as exc:  # noqa: BLE001
                inference_attempt = {"error": str(exc)[:200]}
            # 补跑失败/无 docker → 回退最近一次推理并标注
            if not prev_run:
                prev_run = isig.load_prev_vs_today(model_id, td, fallback=True)
            if not next_run:
                next_run = isig.load_next_top_n(model_id, td, fallback=True)

        if prev_run or next_run:
            cat_map = today.set_index("symbol")["category"].to_dict()
            model_inference = {"model_id": model_id, "trade_date": str(td)}
            if inference_attempt:
                model_inference["inference_attempt"] = inference_attempt
            # 验证基准日：fallback run 的 prediction 日可能 ≠ trade_date（如当日无推理），
            # 须按 run 自身 prediction 日取涨跌/成交额，避免用错日期验证
            def _pct_series_for(pred_date: str) -> pd.Series:
                if str(pred_date).replace("-", "") == trade_date:
                    return pct_series
                p_dates = load_trading_days(db, data_dir, 2, str(pred_date).replace("-", ""))
                if len(p_dates) < 2:
                    return pct_series
                df = load_unadj(db, data_dir, p_dates)
                if df.empty:
                    return pct_series
                cur = df[df["dt"] == p_dates[0]]
                prev = df[df["dt"] == p_dates[1]].set_index("symbol")["close"]
                s = cur.set_index("symbol")["close"]
                pct = (s / prev.reindex(s.index) - 1) * 100
                return pct.dropna()

            all_sigs = (prev_run["signals"] if prev_run else []) + (next_run["signals"] if next_run else [])
            for sig in all_sigs:
                sig["name"] = names.get(sig["symbol"], "")
                sig["category"] = cat_map.get(sig["symbol"], "")
            for runx in (prev_run, next_run):
                if not runx:
                    continue
                ps = _pct_series_for(runx["prediction_trade_date"])
                market_avg_run = float(ps.dropna().mean()) if len(ps.dropna()) else None
                today_run = today.set_index("symbol").get("amount", pd.Series(dtype=float))
                for sig in runx["signals"]:
                    sym = sig["symbol"]
                    sig["today_pct"] = (
                        round(float(ps[sym]), 2) if sym in ps.index and pd.notna(ps[sym]) else None
                    )
                    sig["amount_yi"] = (
                        round(float(today_run[sym]) / 1e4, 2)
                        if sym in today_run.index and pd.notna(today_run.get(sym))
                        else None
                    )
                runx["_market_avg"] = market_avg_run
            if prev_run:
                prev_run["hit_summary"] = inference_hit_rate(
                    prev_run["signals"],
                    _pct_series_for(prev_run["prediction_trade_date"]),
                    market_avg=prev_run.get("_market_avg"), category_map=cat_map,
                )
                model_inference["prev_vs_today"] = prev_run
            if next_run:
                model_inference["next_top5"] = next_run
    except Exception as exc:  # noqa: BLE001
        logger.warning("load model inference failed (降级): %s", exc)
        model_inference = None

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
        "factors": factors,
        "news": news_stats,
        "model_inference": model_inference,
        "top": top,
        "watch": watch,
        "watch_note": (args.watch_note or "").strip() or None,
        "sector_multiday": sector_multiday,
        "sector_flow": sector_flow,
    }
    # 次日走势方向：六维加权 → 明确方向 + 置信度（news 缺失时自动降级提示）
    from direction_engine import score_dimensions

    stats["direction"] = score_dimensions(stats)

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
    d = stats.get("direction") or {}
    if d.get("direction"):
        print(
            f"次日方向研判：{d['direction']}（得分 {d['total_score']}/{d['max_score']}）"
            f"置信度 {'★' * int(d.get('confidence') or 0)}"
        )



# ── 升级：行业多日涨跌幅（1/3/5 日，提炼市场分析口径）──

def load_sector_multiday(
    db: duckdb.DuckDBPyConnection,
    data_dir: Path,
    trade_date: str,
    industry_map: dict[str, str],
) -> dict:
    """行业 1/3/5 日累计涨幅（成分股中位数口径，与 market-analysis 一致）。

    读 daily_unadjusted 最近 6 个交易日 → 按申万一级行业聚合中位数涨幅。
    """
    days = load_trading_days(db, data_dir, 6, trade_date)
    if len(days) < 3:
        return {"latest": trade_date, "items": []}
    df = load_unadj(db, data_dir, days)
    if df.empty:
        return {"latest": trade_date, "items": []}
    piv = df.pivot_table(index="symbol", columns="dt", values="close")
    cols = [c for c in days if c in piv.columns]  # 保持日期顺序（最新在前）
    if len(cols) < 2:
        return {"latest": trade_date, "items": []}
    close_t0 = piv[cols[0]]

    def _pct(offset: int) -> pd.Series:
        idx = len(cols) - 1 - offset
        if idx < 0:
            return pd.Series(dtype=float)
        return (close_t0 / piv[cols[idx]] - 1) * 100

    p1, p3, p5 = _pct(0), _pct(2), _pct(4)
    per = pd.DataFrame({"pct_1d": p1, "pct_3d": p3, "pct_5d": p5})

    groups: dict[str, list[str]] = {}
    for sym, ind in industry_map.items():
        if ind != "未知":
            groups.setdefault(ind, []).append(sym)

    items = []
    for name, syms in groups.items():
        sub = per.loc[per.index.intersection(syms)]
        if sub.empty:
            continue
        items.append({
            "name": name,
            "pct_1d": round(float(sub["pct_1d"].median()), 2),
            "pct_3d": round(float(sub["pct_3d"].median()), 2),
            "pct_5d": round(float(sub["pct_5d"].median()), 2),
        })
    items.sort(key=lambda x: x["pct_5d"], reverse=True)
    return {"latest": trade_date, "items": items}


def load_sector_flow_multiday(
    db: duckdb.DuckDBPyConnection,
    data_dir: Path,
    trade_date: str,
    industry_map: dict[str, str],
) -> dict:
    """板块主力资金净流入 1/5/10 日（L2 flow_net_amount 按行业聚合，亿元）。"""
    days = load_trading_days(db, data_dir, 10, trade_date)
    if not days:
        return {"latest": trade_date, "items": []}
    dt_in = ",".join(f"'{d}'" for d in days)
    df = q(
        db,
        f"SELECT symbol, dt, flow_net_amount FROM read_parquet("
        f"'{data_dir}/6_ml_datasets/l2_factors/dt=*/data.parquet', hive_partitioning=true)"
        f" WHERE dt IN ({dt_in})",
    )
    if df.empty:
        return {"latest": trade_date, "items": []}
    df["dt"] = df["dt"].astype(str)  # l2_factors dt 为 int（20260828），统一字符串
    df["ind"] = df["symbol"].map(industry_map).fillna("未知")
    df = df[df["ind"] != "未知"]
    if df.empty:
        return {"latest": trade_date, "items": []}
    df["net_yi"] = df["flow_net_amount"] / 1e8
    idx_of = {d: i for i, d in enumerate(days)}

    def _agg(n: int) -> pd.Series:
        keep = [d for d in days[:n] if d in idx_of]
        sub = df[df["dt"].isin(keep)]
        return sub.groupby("ind")["net_yi"].sum()

    g1, g5, g10 = _agg(1), _agg(5), _agg(10)
    items = []
    for ind in g10.index:
        items.append({
            "name": ind,
            "flow_1d_yi": round(float(g1.get(ind, 0.0)), 2),
            "flow_5d_yi": round(float(g5.get(ind, 0.0)), 2),
            "flow_10d_yi": round(float(g10.get(ind, 0.0)), 2),
        })
    items.sort(key=lambda x: x["flow_10d_yi"], reverse=True)
    return {"latest": trade_date, "items": items}


if __name__ == "__main__":
    main()