#!/usr/bin/env python3
"""资金流预测力研究产物（离线）：质量分分位→可执行超额、净流入对照、热力图、模式。

口径（与 ``backend/shared/flow_quality.py`` 同源）：
- t 日收盘信号 → **t+1 收盘建仓** → t+1+h 收盘（exec，可执行）；
- 收益行业中性（行业二级等权均值），再减当日**过滤后宇宙**均值；
- 宇宙：剔除 ST / 收盘<2 元 / 成交额<5000 万 / 流通市值后 30%；
- 分位按日截面；命中按 h 天抽稀的日期级（篮子）胜率，避免重叠样本虚增 n。

用法（容器内）：
  python -m backend.scripts.market_analysis.build_flow_predictive
  ... --max-days 240 --out-dir /tmp/flow_pred_smoke   # 快速验证
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend.shared.flow_quality import COMPONENT_COLUMNS, quality_score  # noqa: E402
from backend.shared.quantdb_flow_units import normalize_l2_flow_money_to_yuan  # noqa: E402

log = logging.getLogger("build_flow_predictive")

L2_REL = "6_ml_datasets/l2_factors"
PX_REL = "1_kline_data/daily_forward"
VAL_REL = "5_technical_derived/valuation"
MEMBERS_REL = "2_base_sector/sector_concept/sector_members.parquet"
ST_REL = "2_base_sector/st_pit/st_pit.parquet"
HORIZONS = (3, 5, 10, 20)
DEFAULT_START = "20220101"
SAMPLE_STEP = 2
MIN_AMOUNT_YUAN = 5e7
MIN_PRICE = 2.0
SIZE_FLOOR_PCT = 0.3
PLACEHOLDER_SHARE = 25
REVIEW_MIN_N = 500

CONVENTIONS = {
    "exec_definition": "t 日收盘信号 → t+1 收盘建仓 → t+1+h 收盘（可执行主口径）",
    "return_space": "行业二级中性 → 减当日过滤后宇宙均值",
    "price_source": "daily_forward(qfq)",
    "flow_source": "6_ml_datasets/l2_factors（金额按分区归一为元）",
    "universe": "剔除 ST / 收盘<2 元 / 成交额<5000 万 / 流通市值后 30%",
    "sample": f"每 {SAMPLE_STEP} 个交易日取样（降低重叠与构建耗时）",
    "sector_aggregation": "当前成分回溯聚合（存在幸存者偏差）",
    "strength_note": "历史超额 ≠ 未来收益；单票胜率受左偏分布影响，日期级篮子胜率更稳",
    "score_definition": "各分项当日截面分位按符号等权（0~100，50=中性）；见 flow_quality.COMPONENTS",
}
DISCLOSURE = (
    "净流入金额本身没有稳定方向性；有增量的是订单流结构（成交方向一致性、卖压、"
    "撤单深度、VPIN、大单占比）与‘资金流×价格位置’。历史统计不等于未来收益。"
)


def _dates(data_dir: Path, rel: str) -> list[str]:
    d = data_dir / rel
    if not d.is_dir():
        return []
    return sorted(
        e.name[3:]
        for e in d.iterdir()
        if e.is_dir() and e.name.startswith("dt=") and e.name[3:].isdigit()
    )


def _load_flow(data_dir: Path, days: list[str]) -> pd.DataFrame:
    files = [str(data_dir / L2_REL / f"dt={d}" / "data.parquet") for d in days]
    files = [f for f in files if Path(f).exists()]
    if not files:
        return pd.DataFrame()
    con = duckdb.connect()
    try:
        df = con.execute(
            f"""SELECT symbol, dt, amount, flow_net_amount, {",".join(COMPONENT_COLUMNS)}
                FROM read_parquet({files!r})"""
        ).df()
    finally:
        con.close()
    if df.empty:
        return df
    df["dt"] = df["dt"].astype(str)
    df = normalize_l2_flow_money_to_yuan(df)
    dup = df.groupby(["dt", "flow_net_amount"])["symbol"].transform("size")
    df = df[dup < PLACEHOLDER_SHARE].copy()
    df["net_ratio"] = df["flow_net_amount"].astype(float) / (
        df["amount"].astype(float) * 1e4
    )
    return df


def _load_prices(data_dir: Path, days: list[str]) -> pd.DataFrame:
    files = [str(data_dir / PX_REL / f"dt={d}" / "data.parquet") for d in days]
    files = [f for f in files if Path(f).exists()]
    if not files:
        return pd.DataFrame()
    con = duckdb.connect()
    try:
        df = con.execute(
            f"SELECT symbol, time, close FROM read_parquet({files!r})"
        ).df()
    finally:
        con.close()
    df["dt"] = df["time"].dt.strftime("%Y%m%d")
    return df.drop(columns=["time"])


def _load_float_mv(data_dir: Path, days: list[str]) -> pd.DataFrame:
    files = [str(data_dir / VAL_REL / f"dt={d}" / "data.parquet") for d in days]
    files = [f for f in files if Path(f).exists()]
    if not files:
        return pd.DataFrame()
    con = duckdb.connect()
    try:
        df = con.execute(
            f"SELECT symbol, time, float_mv FROM read_parquet({files!r})"
        ).df()
    finally:
        con.close()
    df["dt"] = df["time"].dt.strftime("%Y%m%d")
    return df.drop(columns=["time"])


def _industry(data_dir: Path, symbols: pd.Index) -> pd.Series:
    p = data_dir / MEMBERS_REL
    if not p.is_file():
        return pd.Series(index=symbols, dtype=object)
    df = pd.read_parquet(p)
    df = df[df["SectorType"] == "行业板块(二级)"].drop_duplicates("Symbol")
    return df.set_index("Symbol")["SectorName"].reindex(symbols)


def _st_mask(data_dir: Path, dates: list[str], symbols: pd.Index) -> pd.DataFrame:
    p = data_dir / ST_REL
    if not p.is_file():
        return pd.DataFrame(False, index=dates, columns=symbols)
    st = pd.read_parquet(p)
    if st.empty:
        return pd.DataFrame(False, index=dates, columns=symbols)
    st["dt"] = st["time"].astype(str)
    st = st[st["is_st"].astype(bool)]
    by_date = {d: set(g["symbol"]) for d, g in st.groupby("dt")}
    out = np.zeros((len(dates), len(symbols)), dtype=bool)
    pos = {s: i for i, s in enumerate(symbols)}
    for di, d in enumerate(dates):
        # 用不超过该日的最近 ST 快照（ST 表可能滞后一天）
        snaps = [k for k in by_date if k <= d]
        if not snaps:
            continue
        for sym in by_date[max(snaps)]:
            j = pos.get(sym)
            if j is not None:
                out[di, j] = True
    return pd.DataFrame(out, index=dates, columns=symbols)


def _ind_neutral(x: pd.DataFrame, industry: pd.Series) -> pd.DataFrame:
    out = x.sub(x.mean(axis=1, skipna=True), axis=0)
    for name, cols in x.T.groupby(industry.fillna("__NA__")):
        if name == "__NA__" or len(cols) < 5:
            continue
        sub = x[cols.index]
        out[cols.index] = sub.sub(sub.mean(axis=1, skipna=True), axis=0)
    return out


def _deciles(
    score: pd.DataFrame,
    rel: pd.DataFrame,
    mask: pd.DataFrame,
    h: int,
    idx: np.ndarray | None = None,
) -> list[dict]:
    if idx is not None:
        score = score.iloc[idx]
        rel = rel.iloc[idx]
        mask = mask.iloc[idx]
    s = score.where(mask).to_numpy()
    e = rel.where(mask).to_numpy()
    q = np.floor(pd.DataFrame(s).rank(axis=1, pct=True).to_numpy() * 5).clip(0, 4)
    ok = np.isfinite(s) & np.isfinite(e)
    rows = []
    for k in range(5):
        m = (q == k) & ok
        vals = e[m]
        daily = np.nanmean(np.where(m, e, np.nan), axis=1)
        daily = daily[np.isfinite(daily)]
        idx = np.arange(0, len(daily), h)
        rows.append(
            {
                "q": k + 1,
                "n": int(m.sum()),
                "mean": round(float(vals.mean()), 3),
                "stock_win": round(float((vals > 0).mean()), 3),
                "date_win": round(float((daily[idx] > 0).mean()), 3),
                "date_mean": round(float(daily[idx].mean()), 3),
            }
        )
    return rows


def _state_table(
    score: pd.DataFrame,
    net: pd.DataFrame,
    r5: pd.DataFrame,
    rel: pd.DataFrame,
    mask: pd.DataFrame,
    h: int,
) -> list[dict]:
    """资金流状态 → 之后 h 日胜率：净流入方向 × 质量分位 × 过去 5 日涨跌档。

    透明概率表：每格只给历史日期级篮子的胜率与均值，不做点预测；样本不足不输出。
    """
    e = rel.where(mask).to_numpy()
    s = score.where(mask).to_numpy()
    f = net.where(mask).to_numpy()
    rr = r5.where(mask).to_numpy()
    q = np.floor(pd.DataFrame(s).rank(axis=1, pct=True).to_numpy() * 5).clip(0, 4)
    buckets = ((-np.inf, -3.0), (-3.0, 0.0), (0.0, 3.0), (3.0, np.inf))
    labels = ("跌>3%", "跌0~3%", "涨0~3%", "涨>3%")
    rows: list[dict] = []
    base = np.isfinite(e) & np.isfinite(s) & np.isfinite(f) & np.isfinite(rr)
    for sign in (0, 1):
        flow_mask = base & ((f > 0) == bool(sign))
        for qi in range(5):
            for bi, (lo, hi) in enumerate(buckets):
                m = flow_mask & (q == qi) & (rr > lo) & (rr <= hi)
                n = int(m.sum())
                if n < 500:
                    continue
                daily = np.nanmean(np.where(m, e, np.nan), axis=1)
                daily = daily[np.isfinite(daily)]
                if len(daily) < 20:
                    continue
                idx = np.arange(0, len(daily), h)
                vals = e[m]
                rows.append(
                    {
                        "flow": "inflow" if sign else "outflow",
                        "quality_q": qi + 1,
                        "r5_bucket": labels[bi],
                        "n": n,
                        "date_win": round(float((daily[idx] > 0).mean()), 3),
                        "date_mean": round(float(daily[idx].mean()), 3),
                        "stock_win": round(float((vals > 0).mean()), 3),
                    }
                )
    return rows


def _pattern(
    sig: pd.DataFrame, rel: pd.DataFrame, mask: pd.DataFrame, h: int, label: str
) -> dict | None:
    e = rel.where(mask).to_numpy()
    m = (sig & mask).to_numpy() & np.isfinite(e)
    n = int(m.sum())
    if n < REVIEW_MIN_N:
        return None
    vals = e[m]
    raw = np.nanmean(np.where(m, e, np.nan), axis=1)
    valid = np.isfinite(raw)
    daily = raw[valid]
    years = pd.to_datetime(pd.Index(rel.index[valid]), format="%Y%m%d").year
    yearly = {
        int(y): round(float((daily[years == y] > 0).mean()), 3)
        for y in sorted(set(years))
        if (years == y).any()
    }
    idx = np.arange(0, len(daily), h)
    return {
        "pattern": label,
        "h": h,
        "n": n,
        "date_win": round(float((daily[idx] > 0).mean()), 3),
        "date_mean": round(float(daily[idx].mean()), 4),
        "stock_win": round(float((vals > 0).mean()), 3),
        "yearly": yearly,
    }


def build(
    data_dir: Path, out_dir: Path, start: str, max_days: int | None = None
) -> dict:
    l2_all = [d for d in _dates(data_dir, L2_REL) if d >= start]
    if max_days:
        l2_all = l2_all[-max_days:]
    l2 = l2_all[::SAMPLE_STEP]
    if not l2:
        raise RuntimeError("没有 L2 分区")
    px_dates = [d for d in _dates(data_dir, PX_REL) if d >= l2[0]]
    val_dates = [d for d in px_dates if (data_dir / VAL_REL / f"dt={d}").is_dir()]
    log.info("窗口 %s~%s：%d 个取样日", l2[0], l2[-1], len(l2))

    flow = _load_flow(data_dir, l2)
    px = _load_prices(data_dir, px_dates)
    val = _load_float_mv(data_dir, val_dates)
    if flow.empty or px.empty:
        raise RuntimeError("L2 或价格数据为空")
    close = (
        px.pivot(index="dt", columns="symbol", values="close")
        .reindex(index=l2)
        .sort_index()
    )
    symbols = close.columns
    fmv = (
        val.pivot(index="dt", columns="symbol", values="float_mv").reindex(
            index=l2, columns=symbols
        )
        if not val.empty
        else pd.DataFrame(np.nan, index=l2, columns=symbols)
    )
    amount = (
        flow.pivot(index="dt", columns="symbol", values="amount").reindex(
            index=l2, columns=symbols
        )
        * 1e4
    )
    r20 = (close / close.shift(20) - 1.0) * 100.0
    vol20 = ((close / close.shift(1) - 1.0) * 100.0).rolling(20, min_periods=10).std()
    turn = (amount / fmv).rolling(20, min_periods=10).mean()
    st = _st_mask(data_dir, l2, symbols)
    mask = (
        close.notna()
        & (amount >= MIN_AMOUNT_YUAN)
        & (close >= MIN_PRICE)
        & (fmv.rank(axis=1, pct=True) >= SIZE_FLOOR_PCT)
        & (~st)
    )
    log.info("宇宙均值 %.0f 只/日", float(mask.sum(axis=1).mean()))

    features = {
        c: flow.pivot(index="dt", columns="symbol", values=c)
        .reindex(index=l2, columns=symbols)
        .sort_index()
        for c in COMPONENT_COLUMNS
    }
    net = (
        flow.pivot(index="dt", columns="symbol", values="net_ratio")
        .reindex(index=l2, columns=symbols)
        .sort_index()
    )
    z20 = (net - net.rolling(20, min_periods=10).mean()) / net.rolling(
        20, min_periods=10
    ).std()
    r5 = (close / close.shift(5) - 1.0) * 100.0
    industry = _industry(data_dir, symbols)
    score = quality_score(features, mask)

    def _fwd(h: int) -> pd.DataFrame:
        # t+1 收盘建仓 → t+1+h 收盘
        return (close.shift(-(h + 1)) / close.shift(-1) - 1.0) * 100.0

    rel: dict[int, pd.DataFrame] = {}
    for h in HORIZONS:
        ind = _ind_neutral(_fwd(h), industry)
        rel[h] = ind.sub(ind.where(mask).mean(axis=1, skipna=True), axis=0)

    tables: dict[str, list[dict]] = {}
    for h in HORIZONS:
        tables[f"composite_exec_h{h}"] = _deciles(score, rel[h], mask, h)
        tables[f"net_exec_h{h}"] = _deciles(net, rel[h], mask, h)
        tables[f"state_exec_h{h}"] = _state_table(score, net, r5, rel[h], mask, h)
    for nm, frame in (
        ("consistency", features["flow_consistency"]),
        ("sell_pressure", features["micro_trade_sell_pressure"]),
        ("cancel_depth", features["flow_cancel_depth_dist"]),
        ("vpin_ratio", features["micro_vpin_vol_ratio"]),
        ("interval", features["micro_trade_interval_mean"]),
        ("big_trade", features["flow_big_trade_ratio"]),
    ):
        tables[f"{nm}_exec_h5"] = _deciles(frame, rel[5], mask, 5)
    years = pd.to_datetime(pd.Index(rel[5].index), format="%Y%m%d").year
    for tag, sel in (("is", years <= 2024), ("oos", years >= 2025)):
        idx = np.where(sel)[0]
        if len(idx) < 20:
            continue
        for h in HORIZONS:
            tables[f"composite_exec_h{h}_{tag}"] = _deciles(score, rel[h], mask, h, idx)

    # 残差：控制 size / 20 日动量 / 波动 / 换手（行业已在 rel 内中性）
    ctrl = [
        fmv.where(mask).rank(axis=1, pct=True).to_numpy(),
        r20.where(mask).rank(axis=1, pct=True).to_numpy(),
        vol20.where(mask).rank(axis=1, pct=True).to_numpy(),
        turn.where(mask).rank(axis=1, pct=True).to_numpy(),
    ]
    for h in HORIZONS:
        y = rel[h].to_numpy()
        outm = np.full_like(y, np.nan)
        for i in range(len(y)):
            m = np.isfinite(y[i]) & mask.iloc[i].to_numpy()
            for c in ctrl:
                m &= np.isfinite(c[i])
            if int(m.sum()) < 200:
                continue
            x = np.column_stack([np.ones(int(m.sum()))] + [c[i][m] for c in ctrl])
            beta = np.linalg.lstsq(x, y[i][m], rcond=None)[0]
            outm[i, m] = y[i][m] - x @ beta
        resid = pd.DataFrame(outm, index=rel[h].index, columns=symbols)
        tables[f"composite_resid_exec_h{h}"] = _deciles(score, resid, mask, h)

    # 5×5：净流入 z20 分位（行）× 过去 5 日涨跌分位（列）
    heatmaps: dict[str, dict] = {}
    r5_rank = r5.where(mask).rank(axis=1, pct=True)
    z_rank = z20.where(mask).rank(axis=1, pct=True)
    for h in HORIZONS:
        e = rel[h].where(mask).to_numpy()
        zq = np.floor(z_rank.to_numpy() * 5).clip(0, 4)
        rq = np.floor(r5_rank.to_numpy() * 5).clip(0, 4)
        ok = np.isfinite(e)
        mean, win, cnt = [], [], []
        for zi in range(5):
            rm, rw, rn = [], [], []
            for ri in range(5):
                m = (zq == zi) & (rq == ri) & ok
                v = e[m]
                rm.append(round(float(np.nanmean(v)), 3) if m.sum() else None)
                rw.append(round(float(np.nanmean(v > 0)), 3) if m.sum() else None)
                rn.append(int(m.sum()))
            mean.append(rm)
            win.append(rw)
            cnt.append(rn)
        heatmaps[f"heatmap_exec_h{h}"] = {"mean": mean, "win": win, "n": cnt}

    patterns: dict[str, dict] = {}
    sigs = {
        "强一致+未大涨（一致性前20% & 5日涨幅≤2%）": (
            features["flow_consistency"].where(mask).rank(axis=1, pct=True) >= 0.8
        )
        & (r5 <= 2),
        "强一致+强势（一致性前20% & 5日涨幅≥5%）": (
            features["flow_consistency"].where(mask).rank(axis=1, pct=True) >= 0.8
        )
        & (r5 >= 5),
        "高净流入+大涨（z20≥1.5 & 5日涨幅≥5%）": (z20 >= 1.5) & (r5 >= 5),
        "净流出+大涨（z20≤-1.5 & 5日涨幅≥5%）": (z20 <= -1.5) & (r5 >= 5),
        "卖压前20%": features["micro_trade_sell_pressure"]
        .where(mask)
        .rank(axis=1, pct=True)
        >= 0.8,
        "撤单深度前20%": features["flow_cancel_depth_dist"]
        .where(mask)
        .rank(axis=1, pct=True)
        >= 0.8,
        "资金流质量分前20%": score.rank(axis=1, pct=True) >= 0.8,
        "资金流质量分后20%": score.rank(axis=1, pct=True) <= 0.2,
    }
    for label, sig in sigs.items():
        for h in HORIZONS:
            st = _pattern(sig, rel[h], mask, h, label)
            if st:
                patterns[f"{label}|h{h}"] = st

    built_at = datetime.now(timezone.utc).isoformat()
    payload = {
        "schema": "flow_predictive.v1",
        "built_at": built_at,
        "trade_date": l2[-1],
        "window": [l2[0], l2[-1]],
        "conventions": {**CONVENTIONS, "sample": CONVENTIONS["sample"]},
        "disclosure": DISCLOSURE,
        "tables": tables,
        "patterns": patterns,
        **heatmaps,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "flow_predictive.json"
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)
    log.info("写出 %s（%d 张表，%d 个模式）", path, len(tables), len(patterns))
    return payload


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    p = argparse.ArgumentParser(description="构建资金流预测力研究产物")
    p.add_argument(
        "--data-dir", default=os.getenv("QM_QUANTDB_DATA_DIR", "data/quantdb")
    )
    p.add_argument("--out-dir", default=None)
    p.add_argument("--start", default=DEFAULT_START)
    p.add_argument("--max-days", type=int, default=None)
    args = p.parse_args(argv)
    out = args.out_dir
    if not out:
        from backend.services.api.market_analysis.flow_predictive import resolve_dir

        out = str(resolve_dir())
    started = time.time()
    build(Path(args.data_dir), Path(out), args.start, args.max_days)
    log.info("完成，用时 %.1fs", time.time() - started)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
