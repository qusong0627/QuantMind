"""扫描器 **T+1 样本外** 回放（2026-10-30）。

背景（两条都改结论）
--------------------
1. A 股 **T+1**：当日买入不可当日卖出 → 原先「持 30 分钟 + 2%/-1%」的日内口径
   在 A 股不可执行。本脚本改为 **D 日 14:50 入场、D+1 日最早卖出**。
2. 样本外：本地 ``min1_kline`` 只到 2026-08-25，30 天回放（1–8 月）是样本内。
   本次用通达信桥分钟线（``data/tdx_min1/``，2026-07-22~10-29），
   **OOS = 2026-08-26 ~ 2026-10-29**（严格晚于样本内窗口）。

口径
----
* 宇宙：L1 ``amt_ma_20`` 前 500，锚定 20260825（早于 OOS，不构成前视）。
* 信号（D 日 14:50）：0.35·z(日内涨幅) + 0.20·z(区间位置) + 0.25·z(量能进度)
  + 0.20·z(L1 八因子综合)；L1 严格取 dt<D。涨停价（主板 ≥9.5%、创业板/科创
  ≥19.5%）不买——封板买不进。
* 买入：D 日 14:50 分钟收盘价。
* 卖出（**T+1**）：D+1 逐分钟检查 +2% 止盈 / -1% 止损（先到先平，同一分钟按
  止损），未触发则 14:55 收盘平。D+1 无数据（停牌）→ 该笔作废不计。
* 对照：``top3_plain``（top-3 但 D+1 14:55 无条件平）与 ``universe_plain``
  （全宇宙等权，D+1 14:55 平）——分离「选股」与「离场规则」各自的贡献。
"""

from __future__ import annotations

import argparse
import bisect
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[2]
L1_DIR = ROOT / "data" / "quantdb" / "6_ml_datasets" / "l1_factors"
DAILY_DIR = ROOT / "data" / "quantdb" / "1_kline_data" / "daily_unadjusted"
MIN1_DIR = ROOT / "data" / "tdx_min1"

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
ENTRY_HM = 145000
EXIT_HM = 145500
TAKE_PROFIT = 0.02
STOP_LOSS = -0.01
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
            "amt_ma_20": cols["amt_ma_20"][i],
            **{f: cols[f][i] for f in need},
        }
    return out


def l1_asof(day: str, dates: list[str]) -> dict:
    """严格早于 day 的最近 L1 分区（PIT）。"""
    idx = bisect.bisect_left(dates, day) - 1
    if idx < 0:
        return {}
    return load_l1(dates[idx])


def load_prev_close(day: str) -> dict[str, float]:
    """D-1 不调整收盘价（原始价，与分钟线同口径）。"""
    path = DAILY_DIR / f"dt={day}" / "data.parquet"
    if not path.exists():
        return {}
    t = pq.read_table(path).to_pydict()
    return {
        str(sym).upper(): float(close)
        for sym, close in zip(t["symbol"], t["close"], strict=False)
        if close
    }


def load_symbol(path: Path) -> dict[str, dict[str, np.ndarray]]:
    """单只分钟线 → {YYYYMMDD: {time,o,h,l,c,amt}}（numpy）。

    兼容两种 schema：TDX 下载（``date``=YYYYMMDD int + ``time``=HHMMSS int）与
    本地 ``min1_kline``（``time``=datetime，日期从中派生）。
    """
    raw = pq.read_table(path).to_pydict()
    times = raw["time"]
    if "date" in raw:
        date_arr = np.asarray(raw["date"], dtype=np.int64)
        time_arr = np.asarray(times, dtype=np.int64)
    else:
        dt_arr = np.asarray(times, dtype="datetime64[s]").astype("datetime64[m]")
        date_arr = (
            dt_arr.astype("datetime64[D]").astype(np.int64) + 719163
        )  # 1970-01-01 = ordinal 719163
        hh = dt_arr.astype("datetime64[h]").astype(np.int64) % 24
        mm = dt_arr.astype("datetime64[m]").astype(np.int64) % 60
        time_arr = hh * 10000 + mm * 100
    cols = {
        "time": time_arr,
        "open": np.asarray(raw["open"], dtype=float),
        "high": np.asarray(raw["high"], dtype=float),
        "low": np.asarray(raw["low"], dtype=float),
        "close": np.asarray(raw["close"], dtype=float),
        "amount": np.asarray(raw["amount"], dtype=float),
    }
    out: dict[str, dict[str, np.ndarray]] = {}
    for d in np.unique(date_arr):
        idx = np.nonzero(date_arr == d)[0]
        if idx.size < 30:
            continue
        key = str(int(d)) if "date" in raw else str(np.datetime64(int(d) - 719163, "D"))
        out[key.replace("-", "")] = {k: v[idx] for k, v in cols.items()}
    return out


def idx_at_hm(bars: dict[str, np.ndarray], hm: int) -> int | None:
    times = bars["time"]
    pos = int(np.searchsorted(times, hm, side="right")) - 1
    if pos < 0 or pos >= len(times):
        return None
    return pos


def limit_up_pct(symbol: str) -> float:
    code = symbol.split(".")[0]
    if code.startswith(("300", "301", "688", "689")):
        return 19.5
    if code.startswith(("8", "4")):
        return 29.5
    return 9.5


def exit_t1(bars: dict[str, np.ndarray], entry: float) -> tuple[float, str] | None:
    """T+1 离场：逐分钟 +2%/-1%，否则 14:55 平。"""
    i_exit = idx_at_hm(bars, EXIT_HM)
    if i_exit is None:
        return None
    close = bars["close"][: i_exit + 1]
    r = close / entry - 1.0
    tp = np.nonzero(r >= TAKE_PROFIT)[0]
    sl = np.nonzero(r <= STOP_LOSS)[0]
    i_tp = int(tp[0]) if tp.size else 10**9
    i_sl = int(sl[0]) if sl.size else 10**9
    if i_sl <= i_tp and i_sl < 10**9:
        return float(r[i_sl]), "sl"
    if i_tp < 10**9:
        return float(r[i_tp]), "tp"
    return float(r[i_exit]), "time"


def simulate_day(
    day: str,
    next_day: str,
    l1: dict[str, dict],
    series: dict[str, dict[str, dict[str, np.ndarray]]],
    prev_close: dict[str, float],
    *,
    depth: int = 20,
    use_l1: bool = True,
) -> dict:
    feats: dict[str, tuple] = {}
    for sym, days in series.items():
        d = days.get(day)
        if d is None or sym not in l1:
            continue
        i = idx_at_hm(d, ENTRY_HM)
        if i is None:
            continue
        c = float(d["close"][i])
        pre = prev_close.get(sym)
        if not pre or c <= 0:
            continue
        chg = c / pre - 1.0
        if chg >= limit_up_pct(sym) / 100.0:
            continue  # 封板买不进
        hi = float(d["high"][: i + 1].max())
        lo = float(d["low"][: i + 1].min())
        pos = (c - lo) / (hi - lo) if hi > lo else 0.5
        amt = float(d["amount"][: i + 1].sum())
        amt_ma = float(l1[sym].get("amt_ma_20") or 0.0)
        progress = max(0.05, min(1.0, _session_progress(ENTRY_HM)))
        amt_ratio = (amt / progress / amt_ma) if amt_ma else 1.0
        feats[sym] = (chg, max(0.0, min(1.0, pos)), amt_ratio, c)

    baseline = []
    baseline_open = []
    for sym, f in feats.items():
        nd = series[sym].get(next_day)
        if nd is None:
            continue
        j = idx_at_hm(nd, EXIT_HM)
        if j is None:
            continue
        baseline.append(float(nd["close"][j]) / f[3] - 1.0)
        baseline_open.append(float(nd["close"][0]) / f[3] - 1.0)

    if len(feats) < 30:
        return {"day": day, "skip": "universe_too_small", "n": len(feats)}

    codes = list(feats)
    zc = dict(zip(codes, zscore([feats[c][0] for c in codes]), strict=False))
    zp = dict(zip(codes, zscore([feats[c][1] for c in codes]), strict=False))
    za = dict(zip(codes, zscore([feats[c][2] for c in codes]), strict=False))
    if use_l1:
        cols = [zscore([l1[c].get(f) or 0.0 for c in codes]) for f in L1_FACTORS]
        comp = [sum(col[i] for col in cols) / len(cols) for i in range(len(codes))]
        l1z = dict(zip(codes, zscore(comp), strict=False))
    else:
        l1z = {}

    scored = []
    for c in codes:
        sc = 0.35 * zc[c] + 0.20 * zp[c] + 0.25 * za[c]
        if use_l1:
            sc += 0.20 * l1z.get(c, 0.0)
        if sc > 0:
            scored.append((sc, c))
    scored.sort(reverse=True)

    ranked = []
    for sc, c in scored[:depth]:
        entry = feats[c][3]
        nd = series[c].get(next_day)
        if nd is None:
            continue
        res = exit_t1(nd, entry)
        if res is None:
            continue
        ret, reason = res
        j = idx_at_hm(nd, EXIT_HM)
        plain = float(nd["close"][j]) / entry - 1.0 if j is not None else None
        open_ret = float(nd["close"][0]) / entry - 1.0
        ranked.append(
            {
                "code": c,
                "score": round(sc, 3),
                "ret": ret,
                "reason": reason,
                "plain": plain,
                "open_ret": open_ret,
            }
        )

    return {
        "day": day,
        "n": len(feats),
        "ranked": ranked,
        "market_chg": round(statistics.median([f[0] for f in feats.values()]), 6),
        "baseline": round(statistics.mean(baseline), 6) if baseline else None,
        "baseline_open": round(statistics.mean(baseline_open), 6)
        if baseline_open
        else None,
        "baseline_n": len(baseline),
    }


def _session_progress(hm: int) -> float:
    minutes = (hm // 100) * 60 + hm % 100
    return (minutes - (9 * 60 + 30)) / (15 * 60 - (9 * 60 + 30))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", type=int, default=500)
    ap.add_argument("--anchor", default="20260825")
    ap.add_argument("--oos-start", default="20260826")
    ap.add_argument("--oos-end", default="20261029")
    ap.add_argument("--ns", default="1,3,5,10", help="组合名数（逗号分隔）")
    ap.add_argument(
        "--min1-dir",
        default=str(MIN1_DIR),
        help="分钟线目录（默认 TDX 下载；传本地 min1_kline 可做样本内对照）",
    )
    args = ap.parse_args()
    min1_dir = Path(args.min1_dir)
    ns = [int(x) for x in str(args.ns).split(",") if x.strip()]
    depth = max(ns)

    dates = l1_dates()
    anchor_l1 = l1_asof(args.anchor, dates)
    ranked_anchor = sorted(
        ((s, float(v.get("amt_ma_20") or 0.0)) for s, v in anchor_l1.items()),
        key=lambda x: x[1],
        reverse=True,
    )
    universe = [s for s, _ in ranked_anchor[: args.universe]]
    print(f"L1 分区 {len(dates)} 个；宇宙 {len(universe)} 只（锚定 {args.anchor}）")

    series: dict[str, dict[str, dict[str, np.ndarray]]] = {}
    missing = []
    for i, sym in enumerate(universe):
        p = min1_dir / f"{sym}.parquet"
        if not p.exists():
            missing.append(sym)
            continue
        series[sym] = load_symbol(p)
        if (i + 1) % 100 == 0:
            print(f"  已加载 {i + 1}/{len(universe)} 只")
    print(f"有分钟数据 {len(series)} 只；缺失 {len(missing)} 只")

    all_days = sorted({d for days in series.values() for d in days})
    oos = [d for d in all_days if args.oos_start <= d <= args.oos_end]
    print(
        f"分钟数据日 {len(all_days)} 天（{all_days[0]}~{all_days[-1]}）；回放 {len(oos)} 天"
    )

    daily_rows: list[dict] = []
    for i, day in enumerate(oos):
        pos = all_days.index(day)
        next_day = all_days[pos + 1] if pos + 1 < len(all_days) else ""
        if not next_day:
            continue
        l1 = l1_asof(day, dates)
        if not l1:
            continue
        didx = dates.index(day) if day in dates else bisect.bisect_left(dates, day)
        prev_day = dates[didx - 1] if didx > 0 else ""
        prev_close = load_prev_close(prev_day) if prev_day else {}
        r = simulate_day(day, next_day, l1, series, prev_close, depth=depth)
        if r.get("skip"):
            continue
        daily_rows.append(
            {
                "day": day,
                "n": r["n"],
                "ranked": r["ranked"],
                "market_chg": r["market_chg"],
                "baseline": r["baseline"],
                "baseline_open": r["baseline_open"],
            }
        )
        if (i + 1) % 5 == 0 or i == len(oos) - 1:
            head = r["ranked"][: ns[0]]
            avg = statistics.mean([p["ret"] for p in head]) if head else None
            print(
                f"  {i + 1}/{len(oos)} {day} k={len(head)} "
                f"avg={avg if avg is None else round(avg, 5)} base={r['baseline']}"
            )

    def summarize(
        per_day: list[dict], key: str, baseline_key: str, rets: list[float]
    ) -> dict:
        vals = [d[key] for d in per_day if d.get(key) is not None]
        if not vals:
            return {}
        mean_day = statistics.mean(vals)
        sd = statistics.stdev(vals) if len(vals) > 1 else 0.0
        t = mean_day / (sd / len(vals) ** 0.5) if sd > 0 else 0.0
        excess = [
            d[key] - d[baseline_key]
            for d in per_day
            if d.get(key) is not None and d.get(baseline_key) is not None
        ]
        ex_mean = statistics.mean(excess) if excess else 0.0
        ex_sd = statistics.stdev(excess) if len(excess) > 1 else 0.0
        ex_t = ex_mean / (ex_sd / len(excess) ** 0.5) if ex_sd > 0 else 0.0
        return {
            "days": len(vals),
            "trades": len(rets),
            "positive_days": sum(1 for v in vals if v > 0),
            "mean_daily_ret": round(mean_day, 6),
            "t_stat": round(t, 2),
            "excess_vs_universe": round(ex_mean, 6),
            "excess_t": round(ex_t, 2),
            "excess_positive_days": sum(1 for v in excess if v > 0),
            "total_pnl_10k": round(sum(rets) * NOTIONAL, 0),
            "win_rate": round(sum(1 for r in rets if r > 0) / len(rets), 4)
            if rets
            else None,
            "worst_day": round(min(vals), 6),
            "best_day": round(max(vals), 6),
        }

    def flat_stats(vals: list[float]) -> dict:
        if not vals:
            return {}
        sd = statistics.stdev(vals) if len(vals) > 1 else 0.0
        return {
            "days": len(vals),
            "mean_daily_ret": round(statistics.mean(vals), 6),
            "t_stat": round(statistics.mean(vals) / (sd / len(vals) ** 0.5), 2)
            if sd > 0
            else 0.0,
            "positive_days": sum(1 for v in vals if v > 0),
        }

    print("\n=== 汇总（T+1：D 日 14:50 买，D+1 最早卖） ===")
    primary_rows: list[dict] = []
    for n in ns:
        per_day = []
        trades = []
        for d in daily_rows:
            picks = d["ranked"][:n]
            if not picks:
                continue
            plains = [p["plain"] for p in picks if p["plain"] is not None]
            per_day.append(
                {
                    "day": d["day"],
                    "avg": statistics.mean([p["ret"] for p in picks]),
                    "plain_avg": statistics.mean(plains) if plains else None,
                    "open_avg": statistics.mean([p["open_ret"] for p in picks]),
                    "baseline": d["baseline"],
                    "baseline_open": d["baseline_open"],
                    "market_chg": d["market_chg"],
                }
            )
            trades.extend(picks)
        if n == (3 if 3 in ns else ns[0]):
            primary_rows = per_day
        rets_tp = [p["ret"] for p in trades]
        rets_plain = [p["plain"] for p in trades if p["plain"] is not None]
        rets_open = [p["open_ret"] for p in trades]
        for label, key, bkey, rets in (
            ("tp_sl", "avg", "baseline", rets_tp),
            ("plain", "plain_avg", "baseline", rets_plain),
            ("open", "open_avg", "baseline_open", rets_open),
        ):
            print(
                f"top{n:<2} {label:<6}",
                json.dumps(summarize(per_day, key, bkey, rets), ensure_ascii=False),
            )

    base_vals = [d["baseline"] for d in daily_rows if d.get("baseline") is not None]
    base_open_vals = [
        d["baseline_open"] for d in daily_rows if d.get("baseline_open") is not None
    ]
    print("universe_base", json.dumps(flat_stats(base_vals), ensure_ascii=False))
    print("universe_open", json.dumps(flat_stats(base_open_vals), ensure_ascii=False))

    reasons: dict[str, int] = defaultdict(int)
    for d in daily_rows:
        for p in d["ranked"][: (3 if 3 in ns else ns[0])]:
            reasons[p["reason"]] += 1
    print("离场原因(top3)", dict(reasons))

    if primary_rows:
        print("\n=== 按入场时市场状态分层（14:50 宇宙涨跌中位数，盘中实时可算） ===")
        buckets = (
            (-1.0, -0.01, "market<=-1%"),
            (-0.01, 0.01, "-1%<market<+1%"),
            (0.01, 1.0, "market>=+1%"),
        )
        for lo, hi, label in buckets:
            sub = [
                d
                for d in primary_rows
                if d.get("market_chg") is not None
                and lo <= d["market_chg"] < hi
                and d.get("avg") is not None
                and d.get("baseline") is not None
            ]
            if not sub:
                print(f"  {label}: 0 天")
                continue
            av = statistics.mean([d["avg"] for d in sub])
            bs = statistics.mean([d["baseline"] for d in sub])
            print(
                f"  {label}: {len(sub)} 天  strat={av:+.4%}  base={bs:+.4%}  "
                f"excess={av - bs:+.4%}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
