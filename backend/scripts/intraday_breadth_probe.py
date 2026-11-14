#!/usr/bin/env python3
"""盘中广度-量价背离探针（10:10 快照 → 剩余时段 / 次日）。

口径与 ``breadth_divergence_probe.py`` 的区别：
- 那边是**收盘**口径（日线）；
- 这边是**盘中**口径：用 min1_kline 取每个交易日 09:25~10:10 的成交额与
  10:10 最新价，按昨收（不复权）判涨跌，重建「40 分钟」广度/TRIN/额价差。
- 长周期上下文用 ``trin_daily.parquet`` 的指数收盘（T−1 收盘已知，无前视）。

输出两段：
1. 状态分桶表（广度 × TRIN × 长周期位置），每格给 n、剩余时段收益、次日收益；
2. 视频口径的具体状态（涨家<40% ∧ TRIN<0.5 ∧ 额价差>20pp）逐日明细。

用法::

    python -m backend.scripts.intraday_breadth_probe --build   # 先建快照
    python -m backend.scripts.intraday_breadth_probe           # 出报告
"""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pyarrow.parquet as pq

log = logging.getLogger("intraday_breadth_probe")

ROOT = Path(os.environ.get("QM_QUANTDB_DATA_DIR", "data/quantdb"))
MIN1_DIR = ROOT / "1_kline_data" / "min1_kline"
L2_DIR = ROOT / "1_kline_data" / "l2_min1"
UNADJ_DIR = ROOT / "1_kline_data" / "daily_unadjusted"
PANEL_PATH = ROOT / "5_technical_derived" / "market_breadth" / "trin_daily.parquet"
SNAP_PATH = (
    ROOT / "5_technical_derived" / "market_breadth" / "intraday_snapshot_1010.parquet"
)
CUTOFF = "10:10:00"


# --------------------------------------------------------------------------
# 1. 10:10 快照
# --------------------------------------------------------------------------
def build_snapshot(source: str = "l2", cutoff: str = CUTOFF) -> pd.DataFrame:
    """聚合每个 (symbol, date) 的开盘至 cutoff 成交额与最新价。

    ``l2``：``l2_min1`` 分区表（2017 起，全市场，列已含 symbol）；
    ``min1``：``min1_kline`` 逐票文件（2026 起，symbol 从文件名提取）。
    """
    con = duckdb.connect()
    if source == "l2":
        pattern = str(L2_DIR / "dt=*" / "data.parquet")
        symbol_expr = "symbol"
    else:
        pattern = str(MIN1_DIR / "*.parquet")
        symbol_expr = "regexp_extract(filename, '([0-9]{6}\\.(SH|SZ|BJ))\\.parquet', 1)"
    sql = f"""
    COPY (
        SELECT
            {symbol_expr} AS symbol,
            CAST(time AS DATE) AS date,
            sum(amount) AS amt40,
            count(*) AS bars40,
            arg_max(close, time) AS px40
        FROM read_parquet('{pattern}', filename={str(source == "min1").lower()})
        WHERE CAST(time AS TIME) <= TIME '{cutoff}'
          AND CAST(time AS TIME) >= TIME '09:25:00'
        GROUP BY 1, 2
        HAVING count(*) >= 20
    ) TO '{SNAP_PATH}' (FORMAT PARQUET)
    """
    con.execute(sql)
    df = pd.read_parquet(SNAP_PATH)
    log.info(
        "快照 %d 行，%d 交易日，%d 标的",
        len(df),
        df["date"].nunique(),
        df["symbol"].nunique(),
    )
    return df


def load_unadjusted_day(day: pd.Timestamp) -> pd.DataFrame:
    p = UNADJ_DIR / f"dt={day:%Y%m%d}" / "data.parquet"
    if not p.exists():
        return pd.DataFrame(columns=["symbol", "close", "amount"])
    return pq.read_table(p, columns=["symbol", "close", "amount"]).to_pandas()


def load_unadjusted_days(dates: list[pd.Timestamp]) -> dict[pd.Timestamp, pd.DataFrame]:
    """一次性装载所有交易日的未复权收盘（避免逐日重复读盘）。"""
    out = {}
    for d in dates:
        out[pd.Timestamp(d)] = load_unadjusted_day(pd.Timestamp(d))
    return out


# --------------------------------------------------------------------------
# 2. 盘中广度面板
# --------------------------------------------------------------------------
def build_panel(
    snap: pd.DataFrame, daily: dict[pd.Timestamp, pd.DataFrame]
) -> pd.DataFrame:
    snap = snap.copy()
    snap["date"] = pd.to_datetime(snap["date"])
    groups = dict(tuple(snap.groupby("date")))
    dates = sorted(groups)
    rows = []
    for i, d in enumerate(dates):
        day = pd.Timestamp(d)
        if i == 0:
            continue
        prev_day = pd.Timestamp(dates[i - 1])
        cur = daily.get(day, pd.DataFrame()).rename(columns={"close": "close_t"})
        prev = daily.get(prev_day, pd.DataFrame()).rename(
            columns={"close": "prev_close"}
        )
        if cur.empty or prev.empty:
            continue
        s = (
            groups[d]
            .merge(prev[["symbol", "prev_close"]], on="symbol", how="inner")
            .merge(cur[["symbol", "close_t"]], on="symbol", how="inner")
        )
        s = s[(s["prev_close"] > 0) & (s["px40"] > 0) & (s["close_t"] > 0)]
        if len(s) < 500:
            continue
        up = s["px40"] > s["prev_close"]
        down = s["px40"] < s["prev_close"]
        n_up, n_down = int(up.sum()), int(down.sum())
        amt_up = float(s.loc[up, "amt40"].sum())
        amt_down = float(s.loc[down, "amt40"].sum())
        ret_rest = s["close_t"] / s["px40"] - 1.0
        rows.append(
            {
                "date": day,
                "n": len(s),
                "up_share": n_up / len(s) * 100,
                "amt_share": amt_up / (amt_up + amt_down) * 100,
                "amt_gap_pp": amt_up / (amt_up + amt_down) * 100 - n_up / len(s) * 100,
                "trin_amt": (n_up / n_down) / (amt_up / amt_down),
                "net": n_up - n_down,
                "ret_rest_ew": float(ret_rest.mean() * 100),
                "ret_rest_vw": float(np.average(ret_rest, weights=s["amt40"]) * 100),
                "ret_rest_median": float(ret_rest.median() * 100),
                "pct_down_rest": float((ret_rest < 0).mean() * 100),
            }
        )
    out = pd.DataFrame(rows).set_index("date").sort_index()
    net = out["net"].astype(float)
    out["mcclellan"] = (
        net.ewm(span=19, adjust=False, min_periods=19).mean()
        - net.ewm(span=39, adjust=False, min_periods=39).mean()
    )
    out["mcl_chg5"] = out["mcclellan"] - out["mcclellan"].shift(5)
    return out


def add_next_day(
    panel: pd.DataFrame, daily: dict[pd.Timestamp, pd.DataFrame]
) -> pd.DataFrame:
    """次日收益（等权收盘对收盘）与指数次日收益。"""
    out = panel.copy()
    dates = sorted(out.index)
    next_ret_ew = []
    for i, d in enumerate(dates):
        if i + 1 >= len(dates):
            next_ret_ew.append(np.nan)
            continue
        c0 = daily[d][["symbol", "close"]].rename(columns={"close": "c0"})
        c1 = daily[dates[i + 1]][["symbol", "close"]].rename(columns={"close": "c1"})
        m = c0.merge(c1, on="symbol").dropna()
        next_ret_ew.append(
            float((m["c1"] / m["c0"] - 1).mean() * 100) if len(m) else np.nan
        )
    out["ret_next_ew"] = next_ret_ew
    idx_path = ROOT / "1_kline_data" / "index_daily"
    cl = {}
    for d in dates:
        p = idx_path / f"dt={d:%Y%m%d}" / "data.parquet"
        if p.exists():
            t = pq.read_table(p, columns=["symbol", "close"]).to_pandas()
            r = t[t["symbol"] == "000001.SH"]
            if not r.empty:
                cl[d] = float(r["close"].iloc[0])
    s = pd.Series(cl).sort_index()
    idx_next = (s.shift(-1) / s - 1) * 100
    out["ret_next_idx"] = idx_next.reindex(out.index)
    return out


def add_regime(panel: pd.DataFrame) -> pd.DataFrame:
    """长周期上下文：T−1 收盘的 250 日位置（10:10 时已知）。"""
    try:
        daily = pd.read_parquet(PANEL_PATH).set_index("date").sort_index()
    except Exception:
        return panel
    out = panel.copy()
    for sym, name in (("000001.SH", "sh"), ("000852.SH", "zz1000")):
        c = daily[f"close_{sym}"]
        hi = c.rolling(250, min_periods=250).max()
        ma = c.rolling(250, min_periods=250).mean()
        ctx = pd.DataFrame(
            {
                f"{name}_above_ma250": c > ma,
                f"{name}_dist_high": (c / hi - 1) * 100,
            }
        ).shift(1)
        out = out.join(ctx, how="left")
    return out


# --------------------------------------------------------------------------
# 3. 报告
# --------------------------------------------------------------------------
def _md_table(df: pd.DataFrame) -> str:
    cols = [str(c) for c in df.columns]
    lines = ["| " + " | ".join(cols) + " |", "|" + "|".join(["---"] * len(cols)) + "|"]
    for _, row in df.iterrows():
        cells = []
        for v in row:
            if isinstance(v, (float, np.floating)):
                cells.append("" if not np.isfinite(v) else f"{v:.2f}")
            else:
                cells.append(str(v))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def _bucket(v: pd.Series, edges: list[float], labels: list[str]) -> pd.Series:
    return pd.cut(v, [-np.inf, *edges, np.inf], labels=labels, right=False)


def state_table(panel: pd.DataFrame) -> pd.DataFrame:
    d = panel.copy()
    d["breadth"] = _bucket(
        d["up_share"],
        [30, 40, 50, 60, 70],
        ["<30", "30-40", "40-50", "50-60", "60-70", ">=70"],
    )
    d["trin"] = _bucket(
        d["trin_amt"],
        [0.4, 0.6, 0.9, 1.2, 2.0],
        ["<0.4", "0.4-0.6", "0.6-0.9", "0.9-1.2", "1.2-2", ">=2"],
    )
    g = d.groupby(["breadth", "trin"], observed=True)
    out = g.agg(
        n=("ret_rest_ew", "size"),
        rest_ew=("ret_rest_ew", "mean"),
        rest_vw=("ret_rest_vw", "mean"),
        next_ew=("ret_next_ew", "mean"),
        win_rest=("ret_rest_ew", lambda s: (s > 0).mean() * 100),
    ).round(2)
    return out.reset_index()


def regime_table(panel: pd.DataFrame) -> pd.DataFrame:
    rows = []
    d = panel.dropna(subset=["sh_above_ma250"]).copy()
    d["breadth2"] = np.where(d["up_share"] < 40, "<40", ">=40")
    d["trin2"] = np.where(d["trin_amt"] < 0.5, "<0.5", ">=0.5")
    for above, grp in d.groupby("sh_above_ma250"):
        for (b, t), sub in grp.groupby(["breadth2", "trin2"]):
            rows.append(
                {
                    "上证在MA250上方": bool(above),
                    "涨家": b,
                    "TRIN": t,
                    "n": len(sub),
                    "rest_ew_%": round(sub["ret_rest_ew"].mean(), 2),
                    "next_ew_%": round(sub["ret_next_ew"].mean(), 2),
                    "win_rest_%": round((sub["ret_rest_ew"] > 0).mean() * 100, 1),
                }
            )
    return pd.DataFrame(rows)


def video_state(panel: pd.DataFrame) -> pd.DataFrame:
    d = panel.copy()
    mask = (d["up_share"] < 40) & (d["trin_amt"] < 0.5) & (d["amt_gap_pp"] > 20)
    sub = d[mask]
    if sub.empty:
        return pd.DataFrame()
    return sub[
        [
            "n",
            "up_share",
            "amt_gap_pp",
            "trin_amt",
            "mcclellan",
            "ret_rest_ew",
            "ret_rest_vw",
            "ret_next_ew",
            "ret_next_idx",
        ]
    ].round(2)


def _stat_block(sub: pd.DataFrame) -> dict:
    out = {"n": len(sub)}
    for c, lab in (("ret_rest_ew", "rest_ew"), ("ret_next_ew", "next_ew")):
        x = sub[c].dropna()
        if len(x) < 2:
            out[f"{lab}_mean"] = out[f"{lab}_t"] = np.nan
            continue
        out[f"{lab}_mean"] = round(float(x.mean()), 3)
        out[f"{lab}_win"] = round(float((x > 0).mean() * 100), 1)
        out[f"{lab}_t"] = round(float(x.mean() / (x.std(ddof=1) / np.sqrt(len(x)))), 2)
    return out


def video_state_stats(panel: pd.DataFrame) -> pd.DataFrame:
    """视频口径状态的收益汇总 + 按年/前后半段稳定性。"""
    mask = (
        (panel["up_share"] < 40)
        & (panel["trin_amt"] < 0.5)
        & (panel["amt_gap_pp"] > 20)
    )
    base = _stat_block(panel)
    rows = [
        {"segment": "全样本", **base},
        {"segment": "视频状态", **_stat_block(panel[mask])},
    ]
    for year, grp in panel.groupby(panel.index.year):
        rows.append({"segment": f"{year}", **_stat_block(grp[mask.reindex(grp.index)])})
    half = panel.index < pd.Timestamp("2022-01-01")
    rows.append(
        {
            "segment": "前半 2017-2021",
            **_stat_block(panel[half][mask.reindex(panel.index[half])]),
        }
    )
    rows.append(
        {
            "segment": "后半 2022-2026",
            **_stat_block(panel[~half][mask.reindex(panel.index[~half])]),
        }
    )
    return pd.DataFrame(rows)


def hot_set_path(day: str = "2026-11-13") -> pd.DataFrame:
    """用 L0.5 热集快照还原某日盘中广度路径（l2_min1 缺口时的替代口径）。

    注意：热集约 590 只，不是全市场，广度绝对值有偏；只看路径形状。
    """
    b = ROOT.parent / "l05_snapshots" / f"date={day.replace('-', '')}"
    if not b.exists():
        return pd.DataFrame()
    frames = [
        pq.read_table(
            b / f, columns=["symbol", "refresh_time", "price", "pre_close", "volume"]
        ).to_pandas()
        for f in os.listdir(b)
        if f.endswith(".parquet")
    ]
    df = pd.concat(frames, ignore_index=True)
    df["rt"] = df["refresh_time"].str.zfill(6).astype(int)
    rows = []
    for t in (94000, 101000, 103000, 113000, 140000, 150000):
        s = df[df["rt"] <= t].sort_values("rt").groupby("symbol").tail(1)
        s = s[(s["pre_close"] > 0) & (s["price"] > 0)]
        if s.empty:
            continue
        up = s["price"] > s["pre_close"]
        dn = s["price"] < s["pre_close"]
        trin = (up.sum() / max(dn.sum(), 1)) / (
            s.loc[up, "volume"].sum() / max(s.loc[dn, "volume"].sum(), 1)
        )
        idx = df[(df["symbol"] == "000001.SH") & (df["rt"] <= t)].sort_values("rt")
        rows.append(
            {
                "time": f"{t // 10000:02d}:{t // 100 % 100:02d}",
                "n": len(s),
                "up_share": round(float(up.mean() * 100), 1),
                "median_ret_%": round(
                    float((s["price"] / s["pre_close"] - 1).median() * 100), 2
                ),
                "vol_trin": round(float(trin), 2),
                "sh_index": round(float(idx["price"].iloc[-1]), 2)
                if len(idx)
                else np.nan,
            }
        )
    return pd.DataFrame(rows)


def _seg(panel: pd.DataFrame, mask: pd.Series, label: str) -> dict:
    x = panel[mask]
    out: dict = {"segment": label, "n": len(x)}
    for c, lab in (("ret_rest_ew", "rest"), ("ret_next_ew", "next")):
        a = x[c].dropna()
        out[f"{lab}_mean"] = round(float(a.mean()), 3) if len(a) else np.nan
        out[f"{lab}_win"] = round(float((a > 0).mean() * 100), 1) if len(a) else np.nan
        out[f"{lab}_t"] = (
            round(float(a.mean() / (a.std(ddof=1) / np.sqrt(len(a)))), 2)
            if len(a) > 2 and a.std(ddof=1) > 0
            else np.nan
        )
    return out


def incremental_intraday(panel: pd.DataFrame) -> pd.DataFrame:
    """单指标增量：广度 / MCL / 额价差，看谁真正提供信息。"""
    base = (panel["up_share"] < 40) & (panel["trin_amt"] < 0.5)
    rows = [
        _seg(panel, panel["up_share"] < 30, "广度<30"),
        _seg(panel, panel["up_share"] > 60, "广度>60"),
        _seg(panel, panel["mcl_chg5"] > 0, "MCL 扩"),
        _seg(panel, panel["mcl_chg5"] < 0, "MCL 缩"),
        _seg(panel, base, "弱广度+低TRIN"),
        _seg(panel, base & (panel["amt_gap_pp"] > 20), "  └ 额价差>20"),
        _seg(panel, base & (panel["amt_gap_pp"] <= 20), "  └ 额价差<=20"),
    ]
    return pd.DataFrame(rows)


def composite_table(panel: pd.DataFrame) -> pd.DataFrame:
    """长短结合：250 日线上/下方 × 10:10 广度三档。"""
    d = panel.dropna(subset=["sh_above_ma250"])
    rows = []
    for reg, grp in d.groupby("sh_above_ma250"):
        tag = "MA250上方" if reg else "MA250下方"
        rows.append(_seg(grp, grp["up_share"] < 30, f"{tag} 广度<30"))
        rows.append(
            _seg(
                grp,
                (grp["up_share"] >= 30) & (grp["up_share"] <= 60),
                f"{tag} 广度30-60",
            )
        )
        rows.append(_seg(grp, grp["up_share"] > 60, f"{tag} 广度>60"))
    return pd.DataFrame(rows)


def build_report(panel: pd.DataFrame) -> str:
    a: list[str] = []
    p = a.append
    p("# 盘中广度-量价背离（10:10 快照）")
    p("")
    p(
        f"区间 {panel.index[0]:%Y-%m-%d} ~ {panel.index[-1]:%Y-%m-%d}，"
        f"{len(panel)} 个交易日。数据源：QuantDB min1_kline（09:25~10:10）+ "
        "daily_unadjusted（昨收/当日收盘）。"
    )
    p("")
    p(
        "口径：涨跌按 **10:10 最新价 vs 昨收**；成交额为 09:25~10:10 累计；"
        "剩余时段收益 = 10:10 → 当日收盘；次日收益 = 当日收盘 → 次日收盘（等权）。"
    )
    p("")
    p("## 一、状态分桶：广度 × TRIN → 剩余时段 / 次日")
    p("")
    p(_md_table(state_table(panel)))
    p("")
    p("## 二、叠加长周期位置（T−1 收盘是否在 250 日线上方）")
    p("")
    p(_md_table(regime_table(panel)))
    p("")
    p("## 三、视频口径的具体状态（涨家<40% ∧ TRIN<0.5 ∧ 额价差>20pp）")
    p("")
    p(
        "先看全样本与分年稳定性；`rest_ew` = 10:10→收盘等权收益，"
        "`next_ew` = 收盘→次日收盘等权收益。"
    )
    p("")
    p(_md_table(video_state_stats(panel)))
    p("")
    vs = video_state(panel)
    if vs.empty:
        p("样本内没有交易日同时满足三条件。")
    else:
        p(f"逐日明细（最近 40 条，共 {len(vs)} 条）：")
        p("")
        p(_md_table(vs.tail(40).reset_index()))
    p("")
    p("## 四、相关性（全样本，仅作方向参考）")
    p("")
    corr = (
        panel[
            [
                "up_share",
                "trin_amt",
                "amt_gap_pp",
                "mcl_chg5",
                "ret_rest_ew",
                "ret_next_ew",
            ]
        ]
        .corr()
        .round(3)
    )
    p(_md_table(corr.reset_index().rename(columns={"index": ""})))
    p("")
    p("## 四·B、单指标增量与长短结合")
    p("")
    p("重点是看：去掉哪个条件，信号还在？加上哪个条件，信号反而消失？")
    p("")
    p(_md_table(incremental_intraday(panel)))
    p("")
    p(_md_table(composite_table(panel)))
    p("")
    p("## 五、2026-11-13 盘中路径（L0.5 热集，非全市场）")
    p("")
    p(
        "l2_min1 覆盖到 11-10，13 日只有 L0.5 热集快照（约 591 只）。"
        "热集偏向活跃股，广度绝对值不代表全市场，只看当天路径形状。"
    )
    p("")
    hp = hot_set_path("2026-11-13")
    if hp.empty:
        p("（当日无热集快照）")
    else:
        p(_md_table(hp))
    p("")
    return "\n".join(a)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    ap = argparse.ArgumentParser(description="盘中广度-量价背离探针")
    ap.add_argument("--build", action="store_true", help="重建 10:10 快照")
    ap.add_argument(
        "--source",
        choices=("l2", "min1"),
        default="l2",
        help="l2=l2_min1 全历史（默认）；min1=min1_kline 2026 起",
    )
    ap.add_argument(
        "--reuse",
        action="store_true",
        help="复用已生成的 intraday_market_panel.parquet，跳过重算",
    )
    ap.add_argument(
        "--out", default=str(SNAP_PATH.parent / "intraday_breadth_divergence.md")
    )
    args = ap.parse_args(argv)

    panel_path = SNAP_PATH.parent / "intraday_market_panel.parquet"
    if args.reuse and panel_path.exists():
        panel = pd.read_parquet(panel_path).sort_index()
    else:
        if args.build or not SNAP_PATH.exists():
            snap = build_snapshot(args.source)
        else:
            snap = pd.read_parquet(SNAP_PATH)
        days = [pd.Timestamp(d) for d in sorted(pd.to_datetime(snap["date"].unique()))]
        daily = load_unadjusted_days(days)
        panel = add_regime(add_next_day(build_panel(snap, daily), daily))
        panel.to_parquet(panel_path)
    report = build_report(panel)
    Path(args.out).write_text(report, encoding="utf-8")
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
