"""扫描器纸面模拟（2026-10-30 热集回放）。

数据：
- 盘中序列：Redis db3 ``market:series:*``（今天 09:30–15:16，~886 只）
- L1 因子：``data/quantdb/6_ml_datasets/l1_factors/dt={T-1}/data.parquet``（PIT）
- 昨收：L1 的 ``close`` 列（T-1 收盘）

策略（纸面，不下单）：
- 09:35–14:55 每 5 分钟决策一次；
- 打分 = 0.35*z(chg) + 0.20*z(日内位置) + 0.25*z(金额比) + 0.20*z(L1 综合分)
  - chg = 现价/昨收-1；日内位置=(价-日低)/(日高-日低)
  - 金额比 = 当日累计成交额 / (20日均额 amt_ma_20 × 当日进度)
  - L1 综合分 = 8 个动量/资金类因子的截面 z 均值（T-1，PIT）
- 每轮取 top-3（score>0、未持仓）按现价买入；持有 30 分钟或 +2%/-1% 先到先平。

对照：
- 基准 A：等权买入全样本并持有到收盘
- 基准 B：每轮取「涨幅最大 top-3」（不用 L1、不用位置/金额）
- 基准 C：随机 3 只
"""

from __future__ import annotations

import json
import random
import statistics
import time
import urllib.request
from datetime import datetime, timedelta, timezone

import redis

CN = timezone(timedelta(hours=8))
L1_PATH = "/data/quantdb/6_ml_datasets/l1_factors/dt={date}/data.parquet"
L1_FACTORS = (
    "mom_ret_5d",
    "mom_ret_20d",
    "amt_z_20",
    "amt_ratio_5_20",
    "turn_z_20",
    "tech_close_to_high_20",
    "mfi_14",
    "ind_strength_20",
)
HOLD_MIN = 30
TAKE_PROFIT = 0.02
STOP_LOSS = -0.01
STEP_MIN = 5


def zscore(values: list[float]) -> list[float]:
    n = len(values)
    if n == 0:
        return []
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / n
    std = var**0.5
    if std < 1e-12:
        return [0.0] * n
    return [max(-3.0, min(3.0, (v - mean) / std)) for v in values]


def load_l1(trade_date: str) -> dict[str, dict[str, float]]:
    import pyarrow.parquet as pq

    table = pq.read_table(L1_PATH.format(date=trade_date))
    cols = {name: table.column(name).to_pylist() for name in table.column_names}
    symbols = cols["symbol"]
    need = [f for f in L1_FACTORS if f in cols]
    out: dict[str, dict[str, float]] = {}
    for i, sym in enumerate(symbols):
        out[str(sym).upper()] = {
            "close": cols["close"][i],
            "amt_ma_20": cols["amt_ma_20"][i],
            **{f: cols[f][i] for f in need},
        }
    return out


def load_series(date_str: str) -> dict[str, list[tuple[float, float, float, float, float]]]:
    """{symbol: [(ts, price, high, low, amount), ...]}（只取当日）。"""
    r = redis.Redis(host="redis", port=6379, db=3, decode_responses=True)
    day = datetime.strptime(date_str, "%Y%m%d").replace(tzinfo=CN)
    start = day.replace(hour=9, minute=30).timestamp()
    end = day.replace(hour=15, minute=5).timestamp()
    out: dict[str, list] = {}
    for key in r.scan_iter("market:series:*", count=2000):
        rows = r.zrangebyscore(key, start, end)
        points = []
        for raw in rows:
            try:
                d = json.loads(raw)
                price = float(d.get("price") or 0)
                if price <= 0:
                    continue
                points.append(
                    (
                        float(d.get("timestamp") or 0),
                        price,
                        float(d.get("high") or price),
                        float(d.get("low") or price),
                        float(d.get("amount") or 0),
                    )
                )
            except (TypeError, ValueError):
                continue
        if len(points) >= 30:
            points.sort()
            from backend.shared.stock_utils import StockCodeUtil

            raw_code = key.split(":", 2)[-1].upper()
            suffix = (
                raw_code
                if "." in raw_code
                else StockCodeUtil.to_suffix(raw_code).upper()
            )
            out[suffix] = points
    return out


def at(points: list, ts: float):
    """序列中 ≤ts 的最后一个点（无则 None）。"""
    lo, hi = 0, len(points) - 1
    best = None
    while lo <= hi:
        mid = (lo + hi) // 2
        if points[mid][0] <= ts:
            best = points[mid]
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def run(date_str: str, top_n: int = 3, use_l1: bool = True) -> dict:
    l1 = load_l1("20261029")
    series = load_series(date_str)
    universe = [s for s in series if s in l1]
    if not universe:
        raise SystemExit("序列与 L1 无交集")
    day = datetime.strptime(date_str, "%Y%m%d").replace(tzinfo=CN)
    open_ts = day.replace(hour=9, minute=30).timestamp()
    close_ts = day.replace(hour=15, minute=0).timestamp()

    # L1 综合分（静态，开盘前算一次）
    l1_score: dict[str, float] = {}
    zs = [zscore([l1[s].get(f) or 0.0 for s in universe]) for f in L1_FACTORS]
    for i, s in enumerate(universe):
        l1_score[s] = sum(z[i] for z in zs) / len(zs) if zs else 0.0
    l1_z = dict(zip(universe, zscore([l1_score[s] for s in universe]), strict=False))

    def features(ts: float) -> dict[str, dict[str, float]]:
        out = {}
        progress = max(0.05, min(1.0, (ts - open_ts) / (close_ts - open_ts)))
        for s in universe:
            p = at(series[s], ts)
            if not p:
                continue
            _, price, high, low, amount = p
            pre = float(l1[s].get("close") or 0)
            amt_ma = float(l1[s].get("amt_ma_20") or 0)
            if pre <= 0:
                continue
            chg = price / pre - 1
            pos = (price - low) / (high - low) if high > low else 0.5
            amt_ratio = (amount / progress / amt_ma) if (amt_ma and amount and amount > 0) else 1.0
            out[s] = {"chg": chg, "pos": max(0.0, min(1.0, pos)), "amt_ratio": amt_ratio}
        return out

    trades: list[dict] = []
    open_pos: dict[str, dict] = {}
    decision = open_ts + STEP_MIN * 60
    while decision <= close_ts - HOLD_MIN * 60:
        feats = features(decision)
        if feats:
            codes = list(feats)
            z_chg = dict(zip(codes, zscore([feats[c]["chg"] for c in codes]), strict=False))
            z_pos = dict(zip(codes, zscore([feats[c]["pos"] for c in codes]), strict=False))
            z_amt = dict(zip(codes, zscore([feats[c]["amt_ratio"] for c in codes]), strict=False))
            scored = []
            for c in codes:
                if c in open_pos:
                    continue
                score = 0.35 * z_chg[c] + 0.20 * z_pos[c] + 0.25 * z_amt[c]
                if use_l1:
                    score += 0.20 * l1_z.get(c, 0.0)
                if score > 0:
                    scored.append((score, c, feats[c]["chg"]))
            scored.sort(reverse=True)
            for score, code, chg in scored[:top_n]:
                p = at(series[code], decision)
                if not p:
                    continue
                open_pos[code] = {
                    "entry_ts": decision,
                    "entry": p[1],
                    "score": round(score, 3),
                    "chg": round(chg, 4),
                }
        # 平仓检查（每分钟）
        for code in list(open_pos):
            pos = open_pos[code]
            exit_price = None
            exit_reason = ""
            t = pos["entry_ts"] + 60
            while t <= min(close_ts, pos["entry_ts"] + HOLD_MIN * 60):
                p = at(series[code], t)
                if p:
                    ret = p[1] / pos["entry"] - 1
                    if ret >= TAKE_PROFIT:
                        exit_price, exit_reason = p[1], "take_profit"
                        break
                    if ret <= STOP_LOSS:
                        exit_price, exit_reason = p[1], "stop_loss"
                        break
                t += 60
            if exit_price is None:
                end = min(close_ts, pos["entry_ts"] + HOLD_MIN * 60)
                p = at(series[code], end)
                if not p:
                    continue
                exit_price, exit_reason = p[1], "time"
            trades.append(
                {
                    "code": code,
                    **pos,
                    "exit_price": exit_price,
                    "ret": exit_price / pos["entry"] - 1,
                    "reason": exit_reason,
                }
            )
            del open_pos[code]
        decision += STEP_MIN * 60

    rets = [t["ret"] for t in trades]
    wins = sum(1 for r in rets if r > 0)
    # 基准 A：等权持有到收盘
    bench = []
    for s in universe:
        p0 = at(series[s], open_ts + 60)
        p1 = at(series[s], close_ts)
        if p0 and p1 and p0[1] > 0:
            bench.append(p1[1] / p0[1] - 1)
    return {
        "symbols": len(universe),
        "trades": len(trades),
        "win_rate": round(wins / len(trades), 4) if trades else None,
        "avg_ret": round(statistics.mean(rets), 6) if rets else None,
        "median_ret": round(statistics.median(rets), 6) if rets else None,
        "sum_ret_10k": round(sum(r * 10000 for r in rets), 0) if rets else 0,
        "bench_hold_avg": round(statistics.mean(bench), 6) if bench else None,
        "sample": trades[:5],
    }


if __name__ == "__main__":
    import sys

    date = sys.argv[1] if len(sys.argv) > 1 else "20261030"
    res = run(date)
    print(json.dumps({k: v for k, v in res.items() if k != "sample"}, ensure_ascii=False, indent=1))
    print("样例交易:")
    for t in res["sample"]:
        print(" ", t["code"], "entry", t["entry"], "exit", t["exit_price"], "ret", f"{t['ret']*100:.2f}%", t["reason"])
