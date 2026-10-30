"""扫描器多日回放（2026-10-30，P1 验证）。

数据：本地 ``min1_kline``（5,289 只 × 2026-01-05~08-25）+ L1 因子 dt=T-1。
宇宙：按 L1 ``amt_ma_20`` 取流动性前 ``--universe`` 只（默认 500）。
回放：在本地有数据的交易日里等距抽 ``--days`` 天（默认 30），每天：
  09:35–14:30 每 5 分钟决策 → top-3 买入 → 持 30 分钟或 +2%/-1% 先到先平。
输出：逐日胜率/均收益/超额、按日稳定性、带 L1 vs 不带 L1。

仓位口径：每笔固定名义 1 万元，收益按比例累计（不复利）。
"""

from __future__ import annotations

import argparse
import bisect
import json
import random
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import pyarrow.parquet as pq

MIN1_DIR = Path("/data/quantdb/1_kline_data/min1_kline")
L1_DIR = Path("/data/quantdb/6_ml_datasets/l1_factors")
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
NOTIONAL = 10_000.0


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


def l1_dates() -> list[str]:
    return sorted(
        p.name.split("=", 1)[1] for p in L1_DIR.iterdir() if p.name.startswith("dt=")
    )


def load_l1(day: str) -> dict[str, dict]:
    path = L1_DIR / f"dt={day}" / "data.parquet"
    table = pq.read_table(path)
    cols = {name: table.column(name).to_pylist() for name in table.column_names}
    need = [f for f in L1_FACTORS if f in cols]
    out = {}
    for i, sym in enumerate(cols["symbol"]):
        out[str(sym).upper()] = {
            "close": cols["close"][i],
            "amount": cols["amount"][i],
            "amt_ma_20": cols["amt_ma_20"][i],
            **{f: cols[f][i] for f in need},
        }
    return out


def l1_asof(day: str, dates: list[str]) -> dict:
    """**严格早于 day** 的最近一个 L1 分区（PIT，防前视）。

    分区 dt=D 的因子由 D 日收盘数据算出，盘中不可用；必须取 dt<D。
    """
    idx = bisect.bisect_left(dates, day) - 1
    if idx < 0:
        return {}
    return load_l1(dates[idx])


def load_prev_close(day: str) -> dict[str, float]:
    """D-1 的**不调整**收盘价（昨收口径，与分钟线原始价一致）。

    L1 的 close 是后复权价（000001 17.88 vs 原始 11.40），拿它当昨收会把
    chg 算成 -36%，整张截面报废。
    """
    path = (
        Path("/data/quantdb/1_kline_data/daily_unadjusted")
        / f"dt={day}"
        / "data.parquet"
    )
    if not path.exists():
        return {}
    t = pq.read_table(path).to_pydict()
    return {
        str(sym).upper(): float(close)
        for sym, close in zip(t["symbol"], t["close"], strict=False)
        if close
    }


def pick_universe(l1: dict[str, dict], n: int) -> list[str]:
    ranked = sorted(l1, key=lambda s: l1[s].get("amt_ma_20") or 0.0, reverse=True)
    return ranked[:n]


def load_min1(symbol: str, days: set[str]) -> dict[str, list[tuple]]:
    """单只 min1 文件 → {YYYYMMDD: [(hm, open, high, low, close, volume, amount)]}"""
    path = MIN1_DIR / f"{symbol}.parquet"
    if not path.exists():
        return {}
    table = pq.read_table(path)
    times = table.column("time").to_pylist()
    op = table.column("open").to_pylist()
    hi = table.column("high").to_pylist()
    lo = table.column("low").to_pylist()
    cl = table.column("close").to_pylist()
    vo = table.column("volume").to_pylist()
    am = table.column("amount").to_pylist()
    out: dict[str, list[tuple]] = defaultdict(list)
    for i, ts in enumerate(times):
        d = ts.strftime("%Y%m%d")
        if d not in days:
            continue
        hm = ts.hour * 100 + ts.minute
        if hm < 930 or hm > 1500:
            continue
        out[d].append((hm, op[i], hi[i], lo[i], cl[i], vo[i], am[i]))
    return dict(out)


def available_days(symbol: str) -> set[str]:
    path = MIN1_DIR / f"{symbol}.parquet"
    if not path.exists():
        return set()
    col = pq.read_table(path, columns=["time"]).column("time").to_pylist()
    return {ts.strftime("%Y%m%d") for ts in col}


def simulate_day(
    day: str,
    l1: dict[str, dict],
    series: dict[str, list[tuple]],
    prev_close: dict[str, float],
    *,
    top_n: int = 3,
    use_l1: bool = True,
) -> dict:
    """单日纸面回放。series: {symbol: [(hm, o, h, l, c, v, amt)]}"""
    universe = [s for s in series if s in l1 and len(series[s]) >= 200]
    if len(universe) < 30:
        return {"day": day, "skip": "universe_too_small", "n": len(universe)}
    # 每个因子先截面 z，再平均成综合分（不同因子量纲不同，先归一）
    cols = [zscore([l1[s].get(f) or 0.0 for s in universe]) for f in L1_FACTORS]
    composite = [sum(col[i] for col in cols) / len(cols) for i in range(len(universe))]
    l1z = dict(zip(universe, zscore(composite), strict=False))
    # 每只：累计 high/low/amount 的前缀数组（按分钟索引）
    pre = {s: float(prev_close.get(s) or 0) for s in universe}
    amt_ma = {s: float(l1[s].get("amt_ma_20") or 0) for s in universe}

    # 前缀数组：累计 high/low/amount —— O(1) 查询（逐分钟重算会把 30 天回放拖到几十分钟）
    pcomp: dict[str, tuple[list[float], list[float], list[float]]] = {}
    for s in universe:
        highs: list[float] = []
        lows: list[float] = []
        amts: list[float] = []
        hi = float("-inf")
        lo = float("inf")
        acc = 0.0
        for _hm, _o, hh, ll, c, _v, amt in series[s]:
            hi = max(hi, float(hh or c))
            lo = min(lo, float(ll or c))
            acc += float(amt or 0)
            highs.append(hi)
            lows.append(lo)
            amts.append(acc)
        pcomp[s] = (highs, lows, amts)

    def snapshot(s: str, idx: int):
        highs, lows, amts = pcomp[s]
        return series[s][idx][4], highs[idx], lows[idx], amts[idx]

    # 时间统一成「当日分钟数」：离场逐分钟检查（5 分钟网格会把 -1% 止损拖成 -5%）
    def to_min(hm: int) -> int:
        return (hm // 100) * 60 + hm % 100

    series_min = {
        s: [(to_min(p[0]), p[1], p[2], p[3], p[4], p[5], p[6]) for p in series[s]]
        for s in universe
    }
    grid = [
        to_min(t) for t in range(935, 1431) if t % 5 == 0 and (t <= 1130 or t >= 1300)
    ]
    idx_map = {s: {p[0]: i for i, p in enumerate(series_min[s])} for s in universe}
    idx_keys = {s: sorted(idx_map[s]) for s in universe}

    def idx_at(s: str, minute: int) -> int | None:
        keys = idx_keys.get(s) or []
        pos = bisect.bisect_right(keys, minute) - 1
        if pos < 0:
            return None
        return idx_map[s][keys[pos]]

    trades = []
    open_pos: dict[str, dict] = {}
    close_min = to_min(1500)
    open_min = to_min(930)
    for minute in grid:
        feats = {}
        for s in universe:
            i = idx_at(s, minute)
            if i is None:
                continue
            c = series_min[s][i][4]
            highs, lows, amts = pcomp[s]
            dh, dl, cum_amt = highs[i], lows[i], amts[i]
            if not pre[s]:
                continue
            chg = c / pre[s] - 1
            pos = (c - dl) / (dh - dl) if dh > dl else 0.5
            progress = max(0.05, min(1.0, (minute - open_min) / (close_min - open_min)))
            amt_ratio = (cum_amt / progress / amt_ma[s]) if amt_ma[s] else 1.0
            feats[s] = (chg, max(0.0, min(1.0, pos)), amt_ratio, i)
        if feats:
            codes = list(feats)
            zc = dict(zip(codes, zscore([feats[c][0] for c in codes]), strict=False))
            zp = dict(zip(codes, zscore([feats[c][1] for c in codes]), strict=False))
            za = dict(zip(codes, zscore([feats[c][2] for c in codes]), strict=False))
            scored = []
            for c in codes:
                if c in open_pos:
                    continue
                sc = 0.35 * zc[c] + 0.20 * zp[c] + 0.25 * za[c]
                if use_l1:
                    sc += 0.20 * l1z.get(c, 0.0)
                if sc > 0:
                    scored.append((sc, c))
            scored.sort(reverse=True)
            for sc, c in scored[:top_n]:
                i = feats[c][3]
                open_pos[c] = {
                    "entry_min": minute,
                    "entry": series_min[c][i][4],
                    "score": round(sc, 3),
                }
        for c in list(open_pos):
            p = open_pos[c]
            ret = None
            reason = ""
            for row in series_min[c]:
                minute2 = row[0]
                if minute2 <= p["entry_min"]:
                    continue
                if minute2 > p["entry_min"] + HOLD_MIN:
                    break
                r = row[4] / p["entry"] - 1
                if r >= TAKE_PROFIT:
                    ret, reason = r, "tp"
                    break
                if r <= STOP_LOSS:
                    ret, reason = r, "sl"
                    break
            if ret is None:
                i = idx_at(c, p["entry_min"] + HOLD_MIN)
                if i is None:
                    continue
                ret = series_min[c][i][4] / p["entry"] - 1
                reason = "time"
            trades.append(
                {"code": c, "entry": p["entry"], "ret": ret, "reason": reason}
            )
            del open_pos[c]
    rets = [t["ret"] for t in trades]
    return {
        "day": day,
        "n": len(universe),
        "trades": len(trades),
        "win_rate": round(sum(1 for r in rets if r > 0) / len(rets), 4)
        if rets
        else None,
        "avg_ret": round(statistics.mean(rets), 6) if rets else None,
        "pnl_10k": round(sum(r * NOTIONAL for r in rets), 0) if rets else 0,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--universe", type=int, default=500)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    dates = l1_dates()
    anchor = "20260825"  # 本地 min1 最后一天
    l1_latest = l1_asof(anchor, dates)
    universe = pick_universe(l1_latest, args.universe)
    print(
        f"L1 分区 {len(dates)} 个；宇宙取流动性前 {len(universe)} 只（锚定 {anchor}）"
    )

    all_days = sorted(available_days(universe[0]))
    all_days = [d for d in all_days if d <= anchor and d >= "20260105"]
    if not all_days:
        raise SystemExit("没有可用交易日")
    random.seed(args.seed)
    step = max(1, len(all_days) // args.days)
    sample = all_days[::step][: args.days]
    print(
        f"本地交易日 {len(all_days)} 天，等距抽 {len(sample)} 天：{sample[0]} … {sample[-1]}"
    )

    day_set = set(sample)
    series_all: dict[str, dict[str, list[tuple]]] = {}
    for i, sym in enumerate(universe):
        got = load_min1(sym, day_set)
        if got:
            series_all[sym] = got
        if (i + 1) % 100 == 0:
            print(f"  已加载 {i + 1}/{len(universe)} 只")
    print(f"有数据的标的 {len(series_all)} 只")

    out = {"with_l1": [], "no_l1": []}
    for d in sample:
        l1 = l1_asof(d, dates)
        if not l1:
            continue
        series = {s: v[d] for s, v in series_all.items() if d in v}
        idx = dates.index(d) if d in dates else -1
        prev_day = dates[idx - 1] if idx > 0 else ""
        prev_close = load_prev_close(prev_day) if prev_day else {}
        for key, use in (("with_l1", True), ("no_l1", False)):
            r = simulate_day(d, l1, series, prev_close, use_l1=use)
            if r.get("trades") is not None:
                out[key].append(r)
        w = out["with_l1"][-1] if out["with_l1"] else {}
        print(
            f"{d} n={w.get('n')} trades={w.get('trades')} win={w.get('win_rate')} "
            f"avg={w.get('avg_ret')} pnl={w.get('pnl_10k')} "
            f"| no_l1 avg={(out['no_l1'][-1] or {}).get('avg_ret') if out['no_l1'] else None}"
        )

    def summary(rows: list[dict]) -> dict:
        rets = [r["avg_ret"] for r in rows if r.get("avg_ret") is not None]
        pnl = sum(r.get("pnl_10k") or 0 for r in rows)
        trades = sum(r.get("trades") or 0 for r in rows)
        pos_days = sum(1 for x in rets if x > 0)
        return {
            "days": len(rows),
            "trades": trades,
            "positive_days": pos_days,
            "mean_daily_avg_ret": round(statistics.mean(rets), 6) if rets else None,
            "total_pnl_10k": round(pnl, 0),
            "worst_day_avg": round(min(rets), 6) if rets else None,
            "best_day_avg": round(max(rets), 6) if rets else None,
        }

    print("\n=== 汇总 ===")
    print("with_L1", json.dumps(summary(out["with_l1"]), ensure_ascii=False))
    print("no_L1  ", json.dumps(summary(out["no_l1"]), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
