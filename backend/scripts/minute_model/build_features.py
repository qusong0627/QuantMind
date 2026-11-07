"""从 L2 parquet 构建 14:50 日内特征（每股每日一行）。

数据源（按优先级合并，同一天只取一份）：
  /media/zbox/nas-toshiba/l2-warehouse/parquet   2023-01-03 ~ 2024-10-31
  /media/zbox/nas-wd/l2-warehouse/parquet        2024-04-01 ~ 2025-11-28
  /media/zbox/sata/l2-warehouse/parquet          2024-11-01 ~ 2025-12-31

口径：
- 只用到 14:50 为止的数据（time_ms <= 145000000 / minute <= 1450），无后视；
- snapshot 价格单位万分之一元（106400 = 10.64），trades/orders 单位为元；
- 缺失/停牌：当日无快照的股票不出行，由面板层用「最后收盘价前向填充 + 量 0」处理；
- 输出：/data/minute_model/daily/date=YYYYMMDD/data.parquet（每股一行，约 50 列）。

用法（容器内）：
  cd /app && python3 -m backend.scripts.minute_model.build_features --probe 20240401
  cd /app && python3 -m backend.scripts.minute_model.build_features --all --workers 4
"""

from __future__ import annotations

import argparse
import logging
import os
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

log = logging.getLogger("minute_model.build")

def _quantdb_root() -> Path:
    """优先容器路径 /data/quantdb，宿主机上退回仓库 data/quantdb。"""
    env = os.environ.get("QM_QUANTDB_DATA_DIR")
    if env:
        return Path(env)
    if Path("/data/quantdb").is_dir() and os.access("/data/quantdb", os.W_OK):
        return Path("/data/quantdb")
    return Path(__file__).resolve().parents[3] / "data" / "quantdb"


WAREHOUSES = (
    Path("/media/zbox/nas-toshiba/l2-warehouse/parquet"),
    Path("/media/zbox/nas-wd/l2-warehouse/parquet"),
    Path("/media/zbox/sata/l2-warehouse/parquet"),
)
OUT = (_quantdb_root().parent / "minute_model" / "daily").resolve()
CUTOFF_MS = 145000000  # 14:50:00.000
CUTOFF_MIN = 1450
LARGE_AMT = 1.0e6
XLARGE_AMT = 5.0e6
BIG_ORDER_AMT = 5.0e5

SNAP_COLS = (
    ["wd_code", "time_ms", "成交价", "当日累计成交量", "当日成交额", "最高价", "最低价", "开盘价", "前收盘",
     "加权平均叫买价", "加权平均叫卖价", "叫买总量", "叫卖总量"]
    + [f"申买价{i}" for i in range(1, 11)]
    + [f"申卖价{i}" for i in range(1, 11)]
    + [f"申买量{i}" for i in range(1, 11)]
    + [f"申卖量{i}" for i in range(1, 11)]
)
TRADE_COLS = ["wd_code", "minute", "amount_yuan", "qty", "dir"]
ORDER_COLS = ["wd_code", "minute", "price_yuan", "qty", "dir", "is_cancel"]


def available_days() -> dict[str, Path]:
    """日期 → 仓库根（后出现的覆盖先出现的）。"""
    days: dict[str, Path] = {}
    for root in WAREHOUSES:
        snap = root / "snapshot"
        if not snap.is_dir():
            continue
        for p in sorted(snap.glob("date=*")):
            days[p.name.split("=")[1]] = root
    return days


def _read_table(root: Path, kind: str, date: str, cols: list[str]) -> pd.DataFrame:
    frames = []
    for f in sorted((root / kind / f"date={date}").glob("*.parquet")):
        frames.append(pq.ParquetFile(f).read(columns=cols).to_pandas())
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=cols)


def _snapshot_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df[df["time_ms"] <= CUTOFF_MS]
    if df.empty:
        return pd.DataFrame()
    df = df.sort_values(["wd_code", "time_ms"])
    g = df.groupby("wd_code", sort=False)
    last = g.tail(1).set_index("wd_code")
    px = last["成交价"].astype(float) / 1e4
    px = px.where(px > 0)
    prev = last["前收盘"].astype(float) / 1e4
    out = pd.DataFrame(index=last.index)
    out["px"] = px
    out["ret_sofar"] = px / prev - 1.0
    out["open_gap"] = (last["开盘价"].astype(float) / 1e4) / prev - 1.0
    hi = last["最高价"].astype(float) / 1e4
    lo = last["最低价"].astype(float) / 1e4
    out["range_pos"] = (px - lo) / (hi - lo).replace(0.0, np.nan)
    out["cum_volume"] = last["当日累计成交量"].astype(float)
    out["cum_amount"] = last["当日成交额"].astype(float)
    out["vwap_gap"] = px / (out["cum_amount"] / out["cum_volume"].replace(0, np.nan)) - 1.0

    bid1 = last["申买价1"].astype(float) / 1e4
    ask1 = last["申卖价1"].astype(float) / 1e4
    mid = (bid1 + ask1) / 2.0
    out["spread"] = (ask1 - bid1) / mid.replace(0, np.nan)
    for k in (1, 5, 10):
        bidv = sum(last[f"申买量{i}"].astype(float) for i in range(1, k + 1))
        askv = sum(last[f"申卖量{i}"].astype(float) for i in range(1, k + 1))
        out[f"depth_sum_{k}"] = bidv + askv
        out[f"imb_{k}"] = (bidv - askv) / (bidv + askv).replace(0, np.nan)
    bidv_all = sum(last[f"申买量{i}"].astype(float) for i in range(1, 11))
    out["big_bid_ratio"] = (
        sum(last[f"申买量{i}"].astype(float) for i in range(1, 4))
        / bidv_all.replace(0, np.nan)
    )
    wb = last["加权平均叫买价"].astype(float) / 1e4
    wa = last["加权平均叫卖价"].astype(float) / 1e4
    out["weighted_bid_gap"] = (wb / px - 1.0).where(wb > 0)
    out["weighted_ask_gap"] = (wa / px - 1.0).where(wa > 0)
    out["book_ratio"] = last["叫买总量"].astype(float) / last["叫卖总量"].replace(0, np.nan)

    # 分钟收益（用快照价格序列重采样）
    df["minute"] = (df["time_ms"] // 10000000) * 100 + (df["time_ms"] // 100000) % 100
    minute_last = (
        df.groupby(["wd_code", "minute"]).last()["成交价"].astype(float).div(1e4).unstack()
    )
    if minute_last.shape[1] >= 30:
        def ret_at(minutes: int) -> pd.Series:
            now = minute_last.iloc[:, -1]
            past = minute_last.iloc[:, -1 - minutes]
            return now / past - 1.0

        out["ret_5m"] = ret_at(5)
        out["ret_15m"] = ret_at(15)
        out["ret_30m"] = ret_at(30)
        out["vol_15m"] = minute_last.pct_change(axis=1).iloc[:, -15:].std(axis=1)
        out["vol_30m"] = minute_last.pct_change(axis=1).iloc[:, -30:].std(axis=1)
    return out


def _trade_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df[(df["minute"] <= CUTOFF_MIN) & df["dir"].isin(["B", "S"])]
    if df.empty:
        return pd.DataFrame()
    df["amount_yuan"] = df["amount_yuan"].astype(float)
    df["qty"] = df["qty"].astype(float)
    buy = df[df["dir"] == "B"].groupby("wd_code").agg(
        buy_amt=("amount_yuan", "sum"), buy_qty=("qty", "sum"), buy_n=("qty", "size")
    )
    sell = df[df["dir"] == "S"].groupby("wd_code").agg(
        sell_amt=("amount_yuan", "sum"), sell_qty=("qty", "sum"), sell_n=("qty", "size")
    )
    out = buy.join(sell, how="outer").fillna(0.0)
    total = out["buy_amt"] + out["sell_amt"]
    out["t_buy_ratio"] = out["buy_amt"] / total.replace(0, np.nan)
    out["t_net_amt"] = out["buy_amt"] - out["sell_amt"]
    out["t_trade_n"] = out["buy_n"] + out["sell_n"]
    out["t_avg_amt"] = total / out["t_trade_n"].replace(0, np.nan)
    large = df[df["amount_yuan"] >= LARGE_AMT]
    lg = large.groupby(["wd_code", "dir"])["amount_yuan"].sum().unstack(fill_value=0.0)
    out["t_large_net"] = lg.get("B", 0.0) - lg.get("S", 0.0)
    out["t_large_ratio"] = (
        (lg.get("B", 0.0) + lg.get("S", 0.0)) / total.replace(0, np.nan)
    )
    xl = df[df["amount_yuan"] >= XLARGE_AMT]
    xg = xl.groupby(["wd_code", "dir"])["amount_yuan"].sum().unstack(fill_value=0.0)
    out["t_xlarge_net"] = xg.get("B", 0.0) - xg.get("S", 0.0)
    # 成交方向自相关（按时间序列的一阶相关）
    df = df.sort_values(["wd_code", "minute"])
    df["sign"] = np.where(df["dir"] == "B", 1.0, -1.0)
    out["t_dir_autocorr"] = (
        df.groupby("wd_code")["sign"].apply(lambda s: s.autocorr(lag=1)).fillna(0.0)
    )
    return out


def _order_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df[df["minute"] <= CUTOFF_MIN]
    if df.empty:
        return pd.DataFrame()
    df["notional"] = df["price_yuan"].astype(float) * df["qty"].astype(float)
    g = df.groupby("wd_code")
    out = pd.DataFrame(index=g.size().index)
    out["o_order_n"] = g.size()
    out["o_cancel_rate"] = g["is_cancel"].mean()
    canc = df[df["is_cancel"].astype(bool)]
    if not canc.empty:
        cg = canc.groupby(["wd_code", "dir"]).size().unstack(fill_value=0)
        out["o_cancel_imb"] = (cg.get("B", 0) - cg.get("S", 0)) / (
            cg.get("B", 0) + cg.get("S", 0)
        ).replace(0, np.nan)
    out["o_big_ratio"] = g.apply(
        lambda x: float((x["notional"] >= BIG_ORDER_AMT).mean()), include_groups=False
    )
    return out


def process_day(date: str, root: Path) -> pd.DataFrame | None:
    started = time.time()
    snap = _read_table(root, "snapshot", date, SNAP_COLS)
    if snap.empty:
        return None
    features = _snapshot_features(snap)
    if features.empty:
        return None
    trades = _read_table(root, "trades", date, TRADE_COLS)
    if not trades.empty:
        features = features.join(_trade_features(trades), how="left")
    orders = _read_table(root, "orders", date, ORDER_COLS)
    if not orders.empty:
        features = features.join(_order_features(orders), how="left")
    features["n_feat_ok"] = features.notna().sum(axis=1)
    features = features[features["n_feat_ok"] >= 5].drop(columns=["n_feat_ok"])
    features.index.name = "symbol"
    features = features.reset_index()
    features["date"] = date
    log.info("%s: %d 只，%.1fs", date, len(features), time.time() - started)
    return features


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--probe", default="")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--start", default="")
    parser.add_argument("--end", default="")
    args = parser.parse_args()
    days = available_days()
    print(f"可用交易日 {len(days)} 天：{min(days)} ~ {max(days)}", flush=True)
    if args.probe:
        out = process_day(args.probe, days[args.probe])
        if out is not None:
            print(out.describe().T.round(4).to_string())
            (OUT / f"probe_{args.probe}.parquet").parent.mkdir(parents=True, exist_ok=True)
            out.to_parquet(OUT / f"probe_{args.probe}.parquet", index=False)
        return
    if not args.all:
        return
    targets = [d for d in sorted(days) if (not args.start or d >= args.start) and (not args.end or d <= args.end)]
    OUT.mkdir(parents=True, exist_ok=True)
    done = 0
    for date in targets:
        path = OUT / f"date={date}" / "data.parquet"
        if path.exists():
            done += 1
            continue
        try:
            out = process_day(date, days[date])
        except Exception as exc:  # pragma: no cover
            log.warning("%s 失败：%s", date, exc)
            continue
        if out is None:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        out.to_parquet(path, index=False)
        done += 1
        if done % 20 == 0:
            print(f"进度 {done}/{len(targets)}（{date}）", flush=True)
    print(f"完成 {done}/{len(targets)}", flush=True)


if __name__ == "__main__":
    main()
