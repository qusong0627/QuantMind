"""盘中 4h 模型 · 实时统计与停牌闸（每日 09:31 后更新）。

五个指标（全部只用「昨日信号 + 今日开盘/昨收」）：
  1. sealed_rate   原始 Top20 中 14:50 已封板的比例（买不进）→ >0.8 停
  2. ic20          近 20 日截面 IC（预测 vs 4h 实际）→ <0.02 停
  3. win10         近 10 日开盘胜率 → <0.5 停
  4. net10         近 10 日累计净盈亏（100 万口径）→ <0 警戒
  5. regime_ok     沪深300 前一日 20 日收益>0 且回撤>-3% → False 停

用法（容器内，cwd=/，PYTHONPATH=/app）：
  # 回填历史（算指标 + 写 parquet）
  python -m backend.scripts.minute_model.track_daily --backfill 20260901 20261106
  # 当日结算（09:31 后：结算昨日信号 + 更新滚动指标 + 写 Redis）
  python -m backend.scripts.minute_model.track_daily --settle 20261109

输出：
  data/minute_model/live_stats.parquet   逐日五指标
  Redis db5 qm:minute_model:stats        最新一行（hash）+ halt 结论
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import pickle
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

log = logging.getLogger("minute_model.track")
PANEL = Path("data/minute_model/panel")
FEAT = Path("data/minute_model/daily")
MIN1 = Path("data/quantdb/1_kline_data/l2_min1")
DAILY = Path("data/quantdb/1_kline_data/daily_unadjusted")
MODEL = Path("data/minute_model/model/lgb_4h_snap.pkl")
OUT = Path("data/minute_model/live_stats.parquet")
REDIS_KEY = "qm:minute_model:stats"

COST = 0.0015
CAPITAL = 1_000_000
TOPN = 20
TH_SEALED = 0.8
TH_IC = 0.02
TH_WIN = 0.5


def _days() -> list[str]:
    return sorted(p.name.split("=")[1] for p in PANEL.glob("date=*") if (p / "data.parquet").exists())


def _calendar() -> list[str]:
    return sorted(p.name.split("=")[1] for p in DAILY.glob("dt=*"))


def _frame(con, d: str) -> pd.DataFrame:
    p = PANEL / f"date={d}" / "data.parquet"
    if p.exists():
        return con.sql(f"SELECT * FROM read_parquet('{p}') WHERE cutoff='14:50'").df()
    f = FEAT / f"date={d}" / "data.parquet"
    if not f.exists():
        return pd.DataFrame()
    df = con.sql(f"SELECT * FROM read_parquet('{f}') WHERE cutoff='14:50'").df()
    cal = _calendar()
    prev = [x for x in cal if x < d][-1]
    fac = con.sql(f"SELECT * FROM read_parquet('data/quantdb/6_ml_datasets/l1_l2_factors/dt={prev}/data.parquet')").df()
    m = pickle.load(open(MODEL, "rb"))
    ctx = [c for c in m["features"] if c in fac.columns]
    df = df.merge(fac[["symbol"] + ctx], on="symbol", how="left")
    reg = pd.read_parquet("data/minute_model/regime.parquet").set_index("date").loc[prev]
    for c in ("mkt_ret_5d", "mkt_ret_20d", "mkt_vol_20d", "mkt_dd_20d"):
        df[c] = float(reg[c])
    pc = con.sql(f"SELECT symbol, close AS prev_close FROM read_parquet('{DAILY}/dt={prev}/data.parquet')").df()
    df = df.merge(pc, on="symbol", how="left")
    df["ret_today_sofar"] = df["px"] / df["prev_close"] - 1
    lim = np.where(df["symbol"].str.startswith(("30", "68")), 0.195, 0.095)
    df["is_limit_up"] = (df["px"] / df["prev_close"] - 1 >= lim - 0.003) & (
        df["ask1"].isna() | (df["ask_depth_1"] == 0)
    )
    df["tradable"] = (df["n_bars"] >= 60) & (df["cum_volume"] > 0) & df["px"].notna() & (~df["is_limit_up"])
    return df


def _next_open(con, next_date: str, symbols: list[str]) -> dict:
    p = MIN1 / f"dt={next_date}" / "data.parquet"
    if not p.exists() or not symbols:
        return {}
    syms = ",".join(repr(s) for s in symbols)
    o = con.sql(
        f"SELECT symbol, CAST(close AS DOUBLE) open_px FROM read_parquet('{p}') "
        f"WHERE time=TIMESTAMP '{next_date[:4]}-{next_date[4:6]}-{next_date[6:]} 09:31:00' "
        f"AND symbol IN ({syms})"
    ).df()
    return dict(zip(o["symbol"], o["open_px"]))


def compute_day(con, model, feats, d: str, next_date: str | None) -> dict | None:
    df = _frame(con, d)
    if df.empty:
        return None
    raw = df[df["px"].notna() & (df["n_bars"] >= 60) & (df["cum_volume"] > 0)].copy()
    if len(raw) < 200:
        return None
    for c in feats:
        if c not in raw.columns:
            raw[c] = np.nan
    raw["pred"] = model.predict(raw[feats])
    raw_top = raw.nlargest(TOPN, "pred")
    sealed = int(raw_top["is_limit_up"].sum())
    buyable = raw[raw["tradable"]].copy()
    buy_top = buyable.nlargest(TOPN, "pred")
    opens = _next_open(con, next_date, buy_top["symbol"].tolist()) if next_date else {}
    buy_top["open_px"] = buy_top["symbol"].map(opens)
    gap = buy_top["open_px"] / buy_top["px"] - 1.0
    # IC（需要标签，可能为 NaN）
    ic = np.nan
    if "ret_4h" in raw.columns and buyable["ret_4h"].notna().sum() > 100:
        b = buyable.dropna(subset=["ret_4h"])
        rp = b["pred"].rank()
        rr = b["ret_4h"].rank()
        ic = float(np.corrcoef(rp, rr)[0, 1])
    gate = (float(raw["mkt_ret_20d"].iloc[0]) > 0) and (float(raw["mkt_dd_20d"].iloc[0]) > -0.03) \
        if "mkt_ret_20d" in raw.columns else False
    return {
        "date": d,
        "next": next_date or "",
        "sealed_rate": sealed / TOPN,
        "n_buyable": int(len(buyable)),
        "n": int(gap.notna().sum()),
        "win": int((gap > 0).sum()),
        "mean_gap": float(gap.mean()) if gap.notna().any() else np.nan,
        "net": float((CAPITAL * gap / TOPN - CAPITAL / TOPN * COST).sum()) if gap.notna().any() else np.nan,
        "ic": ic,
        "regime_ok": bool(gate),
        "ret20": float(raw["mkt_ret_20d"].iloc[0]) if "mkt_ret_20d" in raw.columns else np.nan,
        "dd20": float(raw["mkt_dd_20d"].iloc[0]) if "mkt_dd_20d" in raw.columns else np.nan,
    }


def with_rolling(rows: pd.DataFrame) -> pd.DataFrame:
    r = rows.sort_values("date").reset_index(drop=True)
    r["ic20"] = r["ic"].rolling(20, min_periods=5).mean()
    r["win10"] = r["win"].rolling(10).sum() / r["n"].rolling(10).sum()
    r["net10"] = r["net"].rolling(10).sum()
    r["sealed10"] = r["sealed_rate"].rolling(10).mean()

    def halt(x):
        reasons = []
        if pd.notna(x["sealed10"]) and x["sealed10"] > TH_SEALED:
            reasons.append("sealed")
        if pd.notna(x["ic20"]) and x["ic20"] < TH_IC:
            reasons.append("ic")
        if pd.notna(x["win10"]) and x["win10"] < TH_WIN:
            reasons.append("win")
        if not x["regime_ok"]:
            reasons.append("regime")
        return ",".join(reasons)

    r["halt_reasons"] = r.apply(halt, axis=1)
    r["halted"] = r["halt_reasons"].str.len() > 0
    return r


def write_redis(latest: dict) -> None:
    try:
        import os

        import redis

        client = redis.Redis(
            host=os.environ.get("REDIS_HOST", "localhost"),
            port=int(os.environ.get("REDIS_PORT", "6379")),
            password=os.environ.get("REDIS_PASSWORD") or None,
            db=int(os.environ.get("QM_MODEL_STATS_DB", "5")),
            decode_responses=True,
        )
        client.hset(REDIS_KEY, mapping={k: str(v) for k, v in latest.items()})
        client.hset(REDIS_KEY, mapping={"updated_at": pd.Timestamp.now().isoformat()})
    except Exception as exc:  # noqa: BLE001
        log.warning("写 Redis 失败：%s", exc)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", nargs=2, metavar=("START", "END"))
    ap.add_argument("--settle", default="")
    args = ap.parse_args()

    m = pickle.load(open(MODEL, "rb"))
    model, feats = m["model"], m["features"]
    con = duckdb.connect()
    con.execute("SET memory_limit='8GB'")

    if args.backfill:
        start, end = args.backfill
        days = [d for d in _days() if start <= d <= end]
        cal = _calendar()
        nxt = {cal[i]: cal[i + 1] for i in range(len(cal) - 1)}
        rows = []
        for d in days:
            nd = nxt.get(d)
            r = compute_day(con, model, feats, d, nd)
            if r:
                rows.append(r)
        r = with_rolling(pd.DataFrame(rows))
        r.to_parquet(OUT, index=False)
        print(r[["date", "next", "sealed_rate", "n", "win", "mean_gap", "net", "ic", "regime_ok", "halted", "halt_reasons"]].tail(15).round(3).to_string(index=False))
        latest = r.iloc[-1].to_dict()
        write_redis(latest)
        print("\n最新：", json.dumps({k: (round(v, 4) if isinstance(v, float) else v) for k, v in latest.items()}, ensure_ascii=False, default=str))
        return

    # --settle：结算指定日（该日开盘卖出的前一信号日），并更新滚动
    if args.settle:
        # 简化：重算近 30 天并写最新
        today = args.settle
        days = [d for d in _days() if d < today]
        days = days[-40:]
        cal = _calendar()
        nxt = {cal[i]: cal[i + 1] for i in range(len(cal) - 1)}
        rows = [r for d in days if (r := compute_day(con, model, feats, d, nxt.get(d)))]
        r = with_rolling(pd.DataFrame(rows))
        r.to_parquet(OUT, index=False)
        latest = r.iloc[-1].to_dict()
        write_redis(latest)
        print(json.dumps({k: (round(v, 4) if isinstance(v, float) else v) for k, v in latest.items()}, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
