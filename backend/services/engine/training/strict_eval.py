"""严格评估器：净 Top-N + 可成交 + 成本 + 容量 + 显著性（WP2）。

口径（与 2026-11-09 训练管线规划一致）：
- 信号日 t 收盘出分；买入 t+1 开盘（默认，可切 close 做研究对照）；卖出 t+1+H 收盘。
- 可成交：ST（名称 + PIT 缓存）、停牌/无价、涨停买不进（trade_mask.blocked_long）。
- 成本：按批次均值扣 round-trip 成本，三档 15/25/40bp 同时输出。
- 组合：固定 H 交易日批次（非重叠），每档 Top-N 等权、每只固定名义本金。
- 指标：净收益、MDD、月度、换手/留存、超额（HS300/ZZ1000）、HAC t、
  block bootstrap 95% CI、PSR、参与率（容量）。

设计约束：
- 纯函数（schedule/select/metrics）可单测；数据加载集中在 `StrictEvalData`。
- 不依赖训练容器：在应用容器 / Celery worker 里跑，读 /data 下的 parquet。
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

DATA_ROOT = Path(os.getenv("QM_DATA_ROOT", "/data"))
QDB = DATA_ROOT / "quantdb"
KLINE = QDB / "1_kline_data" / "daily_forward"
INDEX = QDB / "1_kline_data" / "index_daily"
TRADE_MASK = QDB / "5_technical_derived" / "trade_mask" / "trade_mask.parquet"
ST_CACHE = QDB / "factor_research" / "st_pit_cache"
SNAPSHOT = QDB / "factor_research" / "stock_snapshot.parquet"

DEFAULT_HORIZON = 10
DEFAULT_TOP_NS = (20, 50, 100, 200, 500)
DEFAULT_COST_BPS = (15.0, 25.0, 40.0)
TRADING_DAYS = 242


# ─────────────────────────────────────────────────────────────── pure helpers


def to_suffix(symbols: Iterable[str]) -> list[str]:
    """前缀式 SH600000 → 后缀式 600000.SH（已是后缀的原样返回）。"""
    out: list[str] = []
    for raw in symbols:
        s = str(raw).strip()
        if "." in s:
            out.append(s)
        elif len(s) > 2 and s[:2] in ("SH", "SZ", "BJ"):
            out.append(f"{s[2:]}.{s[:2]}")
        else:
            out.append(s)
    return out


def build_schedule(calendar: list[str], start: str, end: str, horizon: int) -> list[str]:
    """从 start 起每隔 horizon 个交易日取一个信号日（非重叠批次）。"""
    idx = {d: i for i, d in enumerate(calendar)}
    sigs = [d for d in calendar if d >= start]
    if not sigs:
        return []
    first = sigs[0]
    out = [first]
    while True:
        j = idx[out[-1]] + horizon
        if j >= len(calendar):
            break
        if calendar[j] > end:
            break
        out.append(calendar[j])
    return out


def pick_top_n(scores: pd.Series, n: int, excluded: set[str]) -> list[str]:
    """按分数降序取前 N，跳过不可交易集合。"""
    picked: list[str] = []
    for symbol in scores.sort_values(ascending=False).index:
        if symbol in excluded:
            continue
        picked.append(str(symbol))
        if len(picked) >= n:
            break
    return picked


def compound(returns: Iterable[float]) -> float:
    nav = 1.0
    for r in returns:
        nav *= 1.0 + float(r)
    return nav - 1.0


def max_drawdown(returns: Iterable[float]) -> float:
    nav = np.cumprod(1.0 + np.asarray(list(returns), dtype=float))
    if nav.size == 0:
        return 0.0
    peak = np.maximum.accumulate(nav)
    return float((nav / peak - 1.0).min())


def hac_t(returns: Iterable[float], lag: int = 0) -> float:
    """Newey-West HAC t（lag=0 即普通 t）。"""
    r = np.asarray(list(returns), dtype=float)
    r = r[np.isfinite(r)]
    n = r.size
    if n < 3:
        return float("nan")
    mu = float(r.mean())
    e = r - mu
    gamma0 = float(e @ e) / n
    var = gamma0
    for k in range(1, min(lag, n - 1) + 1):
        gamma = float(e[k:] @ e[:-k]) / n
        var += 2.0 * (1.0 - k / (lag + 1.0)) * gamma
    if var <= 0:
        return float("nan")
    return mu / math.sqrt(var / n)


def block_bootstrap_ci(
    returns: Iterable[float], *, block: int = 3, draws: int = 1000, seed: int = 7
) -> tuple[float, float]:
    r = np.asarray(list(returns), dtype=float)
    r = r[np.isfinite(r)]
    n = r.size
    if n < 4:
        return (float("nan"), float("nan"))
    rng = np.random.default_rng(seed)
    nblocks = max(1, math.ceil(n / block))
    means = np.empty(draws)
    for i in range(draws):
        starts = rng.integers(0, n, size=nblocks)
        sample = np.concatenate([r[s : s + block] for s in starts])[:n]
        means[i] = sample.mean()
    return (float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5)))


def probabilistic_sharpe(returns: Iterable[float]) -> float:
    """PSR（基准 SR=0），逐批收益口径。"""
    r = np.asarray(list(returns), dtype=float)
    r = r[np.isfinite(r)]
    n = r.size
    if n < 3 or r.std(ddof=0) == 0:
        return float("nan")
    sr = r.mean() / r.std(ddof=0)
    skew = float(pd.Series(r).skew())
    kurt = float(pd.Series(r).kurt()) + 3.0
    denom = math.sqrt(max(1e-12, 1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr * sr))
    from statistics import NormalDist

    return float(NormalDist().cdf(sr * math.sqrt(n - 1) / denom))


def summarize_batches(
    batch_rows: list[dict[str, Any]],
    *,
    horizon: int = DEFAULT_HORIZON,
    costs_bps: tuple[float, ...] = DEFAULT_COST_BPS,
) -> dict[str, Any]:
    """把批次明细汇总成报告（含三档成本）。"""
    if not batch_rows:
        return {"batches": 0}
    gross = np.array([row["gross"] for row in batch_rows], dtype=float)
    bench = np.array([row.get("bench", np.nan) for row in batch_rows], dtype=float)
    bench_zz = np.array([row.get("bench_zz", np.nan) for row in batch_rows], dtype=float)
    overlaps = np.array([row.get("overlap", np.nan) for row in batch_rows], dtype=float)
    months = [row["month"] for row in batch_rows]

    out: dict[str, Any] = {
        "batches": len(batch_rows),
        "gross_mean": float(np.nanmean(gross)),
        "win_batches": float(np.nanmean(gross > 0)),
        "overlap_prev": float(np.nanmean(overlaps)) if np.isfinite(overlaps).any() else None,
        "turnover_per_batch": float(1.0 - np.nanmean(overlaps)) if np.isfinite(overlaps).any() else None,
        "bench_hs300": compound(bench[np.isfinite(bench)]),
        "bench_zz1000": compound(bench_zz[np.isfinite(bench_zz)]),
        "monthly_gross": _monthly(months, gross),
        "batch_returns_gross": [round(float(x), 4) for x in gross],
        "signal_dates": [row["signal"] for row in batch_rows],
        "participation": _participation(batch_rows),
    }
    for bps in costs_bps:
        net = gross - bps / 1e4
        key = f"cost_{int(bps)}bp"
        total = compound(net)
        out[key] = {
            "total": total,
            "annualized": _annualize(total, len(net), horizon),
            "mdd": max_drawdown(net),
            "win_batches": float(np.nanmean(net > 0)),
            "avg_ret": float(np.nanmean(net)),
            "excess_hs300": total - out["bench_hs300"],
            "excess_zz1000": total - out["bench_zz1000"],
            "t_stat": hac_t(net, lag=0),
            "hac_t": hac_t(net, lag=1),
            "bootstrap_ci": list(block_bootstrap_ci(net)),
            "psr": probabilistic_sharpe(net),
            "monthly": _monthly(months, net),
        }
    return out


def _annualize(total: float, batches: int, horizon: int) -> float:
    """按「批次数 × horizon 个交易日」折年。"""
    if batches <= 0 or horizon <= 0:
        return float("nan")
    days = batches * horizon
    if days <= 0 or total <= -1:
        return float("nan")
    return float((1.0 + total) ** (TRADING_DAYS / days) - 1.0)


def _monthly(months: list[str], returns: np.ndarray) -> list[dict[str, Any]]:
    frame = pd.DataFrame({"month": months, "ret": returns})
    frame = frame[np.isfinite(frame["ret"])]
    if frame.empty:
        return []
    grouped = frame.groupby("month")["ret"].apply(lambda s: compound(s.dropna()))
    return [{"month": str(k), "ret": round(float(v), 4)} for k, v in grouped.items()]


def _participation(rows: list[dict[str, Any]]) -> dict[str, Any]:
    values = [
        p for row in rows for p in row.get("participation", []) if p is not None and np.isfinite(p)
    ]
    if not values:
        return {"n": 0}
    arr = np.asarray(values, dtype=float)
    return {
        "n": int(arr.size),
        "median": float(np.median(arr)),
        "p95": float(np.percentile(arr, 95)),
        "max": float(arr.max()),
        "over_10pct": int((arr > 0.10).sum()),
    }


# ─────────────────────────────────────────────────────────────── data loader


@dataclass
class StrictEvalData:
    """按需加载价格/指数/mask，带进程内缓存。"""

    kline_dir: Path = KLINE
    index_dir: Path = INDEX
    _price_cache: dict[str, pd.DataFrame] = field(default_factory=dict)
    _idx_day_cache: dict[str, dict[str, float]] = field(default_factory=dict)
    _st_sets: dict[str, set[str]] = field(default_factory=dict)
    _blocked: set[tuple[str, str]] | None = None
    _st_names: set[str] | None = None
    _snapshot_names: dict[str, str] | None = None

    def calendar(self, lo: str, hi: str) -> list[str]:
        out = []
        for entry in sorted(self.kline_dir.iterdir()):
            if not entry.name.startswith("dt="):
                continue
            day = entry.name[3:]
            if lo <= day <= hi and (entry / "data.parquet").exists():
                out.append(day)
        return out

    def prices(self, day: str, columns: tuple[str, ...] = ("open", "close", "amount")) -> pd.DataFrame:
        if day not in self._price_cache:
            path = self.kline_dir / f"dt={day}" / "data.parquet"
            self._price_cache[day] = pd.read_parquet(path, columns=["symbol", *columns])
        return self._price_cache[day]

    def index_close(self, symbol: str, days: Iterable[str]) -> pd.Series:
        out: dict[str, float] = {}
        for day in days:
            if day not in self._idx_day_cache:
                path = self.index_dir / f"dt={day}" / "data.parquet"
                closes: dict[str, float] = {}
                if path.exists():
                    frame = pd.read_parquet(path, columns=["symbol", "close"])
                    closes = {
                        str(row["symbol"]): float(row["close"])
                        for _, row in frame.iterrows()
                        if pd.notna(row["close"])
                    }
                self._idx_day_cache[day] = closes
            hit = self._idx_day_cache[day].get(symbol)
            if hit is not None:
                out[day] = hit
        return pd.Series(out, dtype=float)

    def blocked_pairs(self) -> set[tuple[str, str]]:
        if self._blocked is None:
            frame = pd.read_parquet(TRADE_MASK, columns=["dt", "symbol", "blocked_long"])
            frame["dt"] = frame["dt"].astype(str)
            self._blocked = set(
                zip(frame.loc[frame["blocked_long"], "dt"], frame.loc[frame["blocked_long"], "symbol"])
            )
        return self._blocked

    def st_name_set(self) -> set[str]:
        if self._st_names is None:
            frame = pd.read_parquet(SNAPSHOT, columns=["symbol", "name"])
            names = frame["name"].astype(str).str.upper().str.replace(" ", "", regex=False)
            self._st_names = set(frame.loc[names.str.contains("ST"), "symbol"].astype(str))
        return self._st_names

    def st_dates(self, symbol: str) -> set[str]:
        if symbol not in self._st_sets:
            path = ST_CACHE / f"{symbol}.parquet"
            out: set[str] = set()
            if path.exists():
                try:
                    frame = pd.read_parquet(path, columns=["time", "is_st"])
                    out = set(frame.loc[frame["is_st"], "time"].astype(str))
                except Exception:  # noqa: BLE001 — 单票缓存损坏不阻断全市场
                    out = set()
            self._st_sets[symbol] = out
        return self._st_sets[symbol]


# ─────────────────────────────────────────────────────────────── runner


def evaluate_run(
    *,
    run_id: str,
    pred_path: str | Path | None = None,
    market: str = "CN",
    horizon: int = DEFAULT_HORIZON,
    segments: dict[str, tuple[str, str]] | None = None,
    top_ns: tuple[int, ...] = DEFAULT_TOP_NS,
    cost_bps: tuple[float, ...] = DEFAULT_COST_BPS,
    entry_price: str = "open",
    capital_per_name: float = 50_000.0,
    data: StrictEvalData | None = None,
) -> dict[str, Any]:
    """计算一个 run 的严格评估报告（JSON 可序列化）。"""
    pred_file = Path(pred_path or (DATA_ROOT / "training_jobs" / run_id / "pred.parquet"))
    if not pred_file.exists():
        raise FileNotFoundError(f"pred.parquet not found: {pred_file}")
    loader = data or StrictEvalData()

    pred = pd.read_parquet(pred_file, columns=["symbol", "trade_date", "pred"])
    pred["trade_date"] = pd.to_datetime(pred["trade_date"]).dt.strftime("%Y%m%d")
    pred["symbol"] = to_suffix(pred["symbol"])
    pred = pred.dropna(subset=["pred"])

    if segments is None:
        segments = {"test": (pred["trade_date"].min(), pred["trade_date"].max())}

    report: dict[str, Any] = {
        "version": 1,
        "run_id": run_id,
        "market": market,
        "horizon_days": horizon,
        "entry_price": entry_price,
        "capital_per_name": capital_per_name,
        "cost_bps": list(cost_bps),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "segments": {k: {"start": v[0], "end": v[1]} for k, v in segments.items()},
        "results": {},
    }

    max_end = max(v[1] for v in segments.values())
    cal = loader.calendar(min(v[0] for v in segments.values()), _shift_day(max_end, horizon + 30))
    if not cal:
        raise ValueError("empty trading calendar")

    blocked = loader.blocked_pairs()
    st_names = loader.st_name_set()

    for seg_name, (lo, hi) in segments.items():
        sigs = build_schedule(cal, lo, hi, horizon)
        cal_idx = {d: i for i, d in enumerate(cal)}
        per_top: dict[str, list[dict[str, Any]]] = {str(n): [] for n in top_ns}
        prev_sel: dict[str, set[str]] = {str(n): set() for n in top_ns}
        for sig in sigs:
            i = cal_idx[sig]
            if i + 1 + horizon >= len(cal):
                continue
            entry, exit_ = cal[i + 1], cal[i + 1 + horizon]
            day_scores = pred[pred["trade_date"] == sig].set_index("symbol")["pred"]
            if day_scores.empty:
                continue
            entry_px = loader.prices(entry).set_index("symbol")
            exit_px = loader.prices(exit_).set_index("symbol")
            for n in top_ns:
                excluded: set[str] = set()
                for symbol in day_scores.index:
                    if symbol in st_names or sig in loader.st_dates(symbol):
                        excluded.add(symbol)
                        continue
                    if (entry, symbol) in blocked:
                        excluded.add(symbol)
                        continue
                    if symbol not in entry_px.index or symbol not in exit_px.index:
                        excluded.add(symbol)
                        continue
                sel = pick_top_n(day_scores, n, excluded)
                if not sel:
                    continue
                entry_col = "open" if entry_price == "open" else "close"
                r = (
                    exit_px.reindex(sel)["close"].to_numpy()
                    / entry_px.reindex(sel)[entry_col].to_numpy()
                    - 1.0
                )
                ok = np.isfinite(r)
                if ok.sum() < max(5, n // 3):
                    continue
                gross = float(np.nanmean(r))
                amount = entry_px.reindex(sel)["amount"].to_numpy()
                participation = [
                    float(capital_per_name / (a * 1e4)) if a and a > 0 else None for a in amount
                ]
                bench = _bench_return(loader, "000300.SH", entry, exit_)
                bench_zz = _bench_return(loader, "000852.SH", entry, exit_)
                overlap = (
                    len(prev_sel[str(n)] & set(sel)) / len(sel) if prev_sel[str(n)] else np.nan
                )
                prev_sel[str(n)] = set(sel)
                per_top[str(n)].append(
                    {
                        "signal": sig,
                        "entry": entry,
                        "exit": exit_,
                        "gross": gross,
                        "bench": bench,
                        "bench_zz": bench_zz,
                        "overlap": overlap,
                        "month": entry[:6],
                        "names": int(ok.sum()),
                        "participation": participation,
                    }
                )
        report["results"][seg_name] = {
            f"top{n}": summarize_batches(per_top[str(n)], horizon=horizon, costs_bps=cost_bps)
            for n in top_ns
        }

    return report


def _shift_day(day: str, days: int) -> str:
    """把 YYYYMMDD 往后推 N 个自然日（只用于给日历取上界，不做交易日推算）。"""
    from datetime import timedelta

    try:
        dt = datetime.strptime(day, "%Y%m%d")
    except ValueError:
        return day
    return (dt + timedelta(days=days)).strftime("%Y%m%d")


def _bench_return(loader: StrictEvalData, symbol: str, entry: str, exit_: str) -> float:
    close = loader.index_close(symbol, [entry, exit_])
    if len(close) < 2 or close.get(entry, 0) <= 0:
        return float("nan")
    return float(close[exit_] / close[entry] - 1.0)


def dump_report(report: dict[str, Any], out_path: str | Path) -> None:
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
