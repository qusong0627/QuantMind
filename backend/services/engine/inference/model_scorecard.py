"""模型推理成绩单（滚动 1 个月）：胜率 / 收益 / 准确性 / 自动权重。

口径（2026-11-18 用户要求，机构级可对账）：

* **信号来源**：``qm_model_inference_runs``（市场研究/推理中心的批量推理结果）+
  ``engine_signal_scores``（每次 run 的逐票分数）。
* **标的池**：该 run 的全部打分票（``signals_count``）；另单独统计 **Top-K**
  （默认 20，按 fusion_score 降序）——「模型推理出来的股票」的可交易口径。
* **收益**：T = ``data_trade_date``（推理日）；**T+1 开盘买入**，持有模型
  ``target_horizon_days`` 个交易日后 **T+1+N 收盘卖出**（A股 T+1；与训练标签
  ``close(T+N+1)/open(T+1)-1`` 同口径）。复权价来自 QuantDB ``daily_forward``。
* **指标**：
  - 胜率 = Top-K 中收益 > 0 的占比（跌的照算，不做幸存者过滤）；
  - 平均/中位收益、相对中证1000 同窗口超额；
  - 全池 Rank IC / Pearson IC（准确性）；十分位多空（Top10% − Bottom10%）；
  - 滚动窗口 = 最近 ``WINDOW_COHORTS``（默认 22 个已成熟交易日）；
  - 综合分 0–100（胜率 40% + 超额 30% + ICIR 20% + 批次正收益占比 10%）；
  - 权重 = 同 market+horizon 组内 ``max(0, score-50)`` 归一化（全为 0 时等权）。
* **何时算**：每个交易日后（16:45）把「今天刚成熟」的批次（exit_date = 今天）
  落库；滚动表按 as_of 重算。幂等：主键 upsert。
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import numpy as np
import pandas as pd
from sqlalchemy import text

from backend.shared.database_manager_v2 import get_session
from backend.shared.stock_utils import StockCodeUtil

logger = logging.getLogger(__name__)

DEFAULT_TOP_K = 20
WINDOW_COHORTS = 22  # 最近一个月（约 22 个交易日）
_BENCHMARK = "000852.SH"  # 中证1000
_QUANTDB_DIR = Path(
    __import__("os").environ.get("QM_QUANTDB_DATA_DIR", "/data/quantdb")
)

_COHORT_DDL = """
CREATE TABLE IF NOT EXISTS qm_model_scorecard_cohorts (
    model_id        TEXT NOT NULL,
    market          TEXT NOT NULL DEFAULT 'CN',
    horizon_days    INTEGER NOT NULL,
    signal_date     DATE NOT NULL,
    entry_date      DATE NOT NULL,
    exit_date       DATE NOT NULL,
    top_k           INTEGER NOT NULL,
    n_signals       INTEGER NOT NULL,
    n_matured       INTEGER NOT NULL,
    win_rate        REAL,
    avg_return      REAL,
    median_return   REAL,
    excess_return   REAL,
    rank_ic         REAL,
    ic              REAL,
    top_decile_avg  REAL,
    bottom_decile_avg REAL,
    long_short      REAL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (model_id, signal_date, top_k)
)
"""

_ROLLING_DDL = """
CREATE TABLE IF NOT EXISTS qm_model_scorecard_rolling (
    model_id        TEXT NOT NULL,
    market          TEXT NOT NULL DEFAULT 'CN',
    horizon_days    INTEGER NOT NULL,
    as_of           DATE NOT NULL,
    top_k           INTEGER NOT NULL,
    window_days     INTEGER NOT NULL,
    cohorts         INTEGER NOT NULL,
    win_rate        REAL,
    avg_return      REAL,
    median_return   REAL,
    cohort_hit_rate REAL,
    excess_return   REAL,
    rank_ic         REAL,
    ic_ir           REAL,
    t_stat          REAL,
    score           REAL,
    weight          REAL,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (model_id, as_of, top_k)
)
"""


# ── 纯函数（可单测）─────────────────────────────────────────────────────


def rank_ic(scores: pd.Series, returns: pd.Series) -> float:
    """Spearman rank IC（同 ``inference_quality_backfill`` 的算法）。"""
    df = pd.DataFrame({"s": scores, "r": returns}).dropna()
    if len(df) < 10:
        return float("nan")
    rs = df["s"].rank().to_numpy()
    rr = df["r"].rank().to_numpy()
    if rs.std() == 0 or rr.std() == 0:
        return float("nan")
    cs, cr = rs - rs.mean(), rr - rr.mean()
    denom = math.sqrt(float((cs**2).sum() * (cr**2).sum()))
    return float((cs * cr).sum() / denom) if denom else float("nan")


def pearson_ic(scores: pd.Series, returns: pd.Series) -> float:
    df = pd.DataFrame({"s": scores, "r": returns}).dropna()
    if len(df) < 3:
        return float("nan")
    if df["s"].std() == 0 or df["r"].std() == 0:
        return float("nan")
    return float(np.corrcoef(df["s"], df["r"])[0, 1])


def cohort_metrics(
    scores: pd.DataFrame,
    returns: pd.Series,
    benchmark_return: float,
    *,
    top_k: int = DEFAULT_TOP_K,
) -> dict[str, Any]:
    """单批次指标。``scores``: [symbol, score]；``returns``: index=symbol 的 T+1→T+1+N 收益。"""
    df = scores.copy()
    df["ret"] = df["symbol"].map(returns)
    matured = df.dropna(subset=["ret"])
    n_signals = int(len(df))
    n_matured = int(len(matured))
    out: dict[str, Any] = {
        "n_signals": n_signals,
        "n_matured": n_matured,
        "win_rate": None,
        "avg_return": None,
        "median_return": None,
        "excess_return": None,
        "rank_ic": None,
        "ic": None,
        "top_decile_avg": None,
        "bottom_decile_avg": None,
        "long_short": None,
    }
    if n_matured == 0:
        return out

    ranked = df.sort_values("score", ascending=False)
    top = ranked.head(max(1, int(top_k))).dropna(subset=["ret"])
    if len(top):
        out["win_rate"] = float((top["ret"] > 0).mean())
        out["avg_return"] = float(top["ret"].mean())
        out["median_return"] = float(top["ret"].median())
        if benchmark_return is not None and not math.isnan(benchmark_return):
            out["excess_return"] = float(top["ret"].mean() - benchmark_return)

    out["rank_ic"] = rank_ic(matured["score"], matured["ret"])
    out["ic"] = pearson_ic(matured["score"], matured["ret"])

    q = matured["score"].rank(pct=True)
    top_dec = matured.loc[q >= 0.9, "ret"]
    bot_dec = matured.loc[q <= 0.1, "ret"]
    if len(top_dec) and len(bot_dec):
        out["top_decile_avg"] = float(top_dec.mean())
        out["bottom_decile_avg"] = float(bot_dec.mean())
        out["long_short"] = float(top_dec.mean() - bot_dec.mean())
    return out


def composite_score(
    win_rate: float | None,
    excess_return: float | None,
    ic_ir: float | None,
    cohort_hit_rate: float | None,
) -> float:
    """0–100 综合分（权重固定并写死，避免事后调参）。"""
    def _clip(x: float) -> float:
        return max(0.0, min(1.0, float(x)))

    wr = _clip(((win_rate if win_rate is not None else 0.5) - 0.5) / 0.15)
    ex = _clip(((excess_return if excess_return is not None else 0.0) + 0.02) / 0.07)
    ir = _clip(((ic_ir if ic_ir is not None else 0.0) + 0.2) / 1.2)
    hit = _clip(((cohort_hit_rate if cohort_hit_rate is not None else 0.5) - 0.5) / 0.3)
    return round(100.0 * (0.40 * wr + 0.30 * ex + 0.20 * ir + 0.10 * hit), 2)


def normalize_weights(rows: Iterable[Mapping[str, Any]]) -> dict[str, float]:
    """同组内权重：``max(0, score-50)`` 归一化；全体 ≤50 分时退化为 softmax。

    为什么要有退化分支：熊市月份所有模型绝对分都会低（胜率/绝对收益被市场拖累），
    若一律归零就变成等权，权重面失去区分度。softmax 只改变**相对**权重，不改变
    「分数越低权重越低」的方向；温度 8 让组内差距可读（10 分差 ≈ 3.5 倍权重）。
    """
    rows = list(rows)
    if not rows:
        return {}
    raw = {str(r["model_id"]): max(0.0, float(r.get("score") or 0.0) - 50.0) for r in rows}
    total = sum(raw.values())
    if total > 0:
        return {mid: v / total for mid, v in raw.items()}

    scores = {str(r["model_id"]): float(r.get("score") or 0.0) for r in rows}
    top = max(scores.values())
    exps = {mid: math.exp((s - top) / 8.0) for mid, s in scores.items()}
    denom = sum(exps.values())
    return {mid: v / denom for mid, v in exps.items()}


def rolling_stats(cohorts: pd.DataFrame) -> dict[str, Any]:
    """滚动窗口聚合。``cohorts`` 每行一个批次（已按 signal_date 取最近 N 个）。"""
    out: dict[str, Any] = {
        "cohorts": int(len(cohorts)),
        "win_rate": None,
        "avg_return": None,
        "median_return": None,
        "cohort_hit_rate": None,
        "excess_return": None,
        "rank_ic": None,
        "ic_ir": None,
        "t_stat": None,
        "score": None,
    }
    if cohorts.empty:
        return out

    wr = cohorts["win_rate"].dropna()
    rets = cohorts["avg_return"].dropna()
    excess = cohorts["excess_return"].dropna()
    ics = cohorts["rank_ic"].dropna()

    if len(wr):
        out["win_rate"] = float(wr.mean())
    if len(rets):
        out["avg_return"] = float(rets.mean())
        out["median_return"] = float(cohorts["median_return"].dropna().median())
        if len(rets) >= 2 and rets.std(ddof=1) > 0:
            out["t_stat"] = float(rets.mean() / (rets.std(ddof=1) / math.sqrt(len(rets))))
        out["cohort_hit_rate"] = float((rets > 0).mean())
    if len(excess):
        out["excess_return"] = float(excess.mean())
    if len(ics):
        out["rank_ic"] = float(ics.mean())
        if len(ics) >= 2 and ics.std(ddof=1) > 0:
            out["ic_ir"] = float(ics.mean() / ics.std(ddof=1))
    out["score"] = composite_score(
        out["win_rate"], out["excess_return"], out["ic_ir"], out["cohort_hit_rate"]
    )
    return out


# ── 数据装载 ────────────────────────────────────────────────────────────


def trading_days(start: date, end: date) -> list[date]:
    """QuantDB 日线分区即交易日历（权威）。"""
    out: list[date] = []
    base = _QUANTDB_DIR / "1_kline_data" / "daily_forward"
    if not base.is_dir():
        return out
    for p in base.glob("dt=*"):
        try:
            d = datetime.strptime(p.name[3:], "%Y%m%d").date()
        except ValueError:
            continue
        if start <= d <= end:
            out.append(d)
    return sorted(out)


#: 单次 refresh_all 的进程内分区缓存：54 个模型 × 40 个交易日重复读同一批
#: parquet 是本任务最贵的一步（实测首跑 >6 分钟）。分区小（~5k 行），缓存安全。
_PRICE_PARTITIONS: dict[date, pd.DataFrame] = {}
_BENCH_PARTITIONS: dict[date, pd.DataFrame] = {}


def clear_price_cache() -> None:
    _PRICE_PARTITIONS.clear()
    _BENCH_PARTITIONS.clear()


def load_prices(days: list[date], symbols: set[str]) -> pd.DataFrame:
    """读指定交易日的复权价（symbol/time/open/close），symbol 用后缀口径。"""
    frames = []
    base = _QUANTDB_DIR / "1_kline_data" / "daily_forward"
    for d in days:
        df = _PRICE_PARTITIONS.get(d)
        if df is None:
            f = base / f"dt={d.strftime('%Y%m%d')}" / "data.parquet"
            df = (
                pd.read_parquet(f, columns=["symbol", "open", "close"])
                if f.exists()
                else pd.DataFrame(columns=["symbol", "open", "close"])
            )
            _PRICE_PARTITIONS[d] = df
        sub = df[df["symbol"].isin(symbols)]
        if len(sub):
            frames.append(sub.assign(trade_date=d))
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(
        columns=["symbol", "open", "close", "trade_date"]
    )


def load_benchmark(days: list[date]) -> pd.DataFrame:
    frames = []
    base = _QUANTDB_DIR / "1_kline_data" / "index_daily"
    for d in days:
        df = _BENCH_PARTITIONS.get(d)
        if df is None:
            f = base / f"dt={d.strftime('%Y%m%d')}" / "data.parquet"
            df = (
                pd.read_parquet(f, columns=["symbol", "open", "close"])
                if f.exists()
                else pd.DataFrame(columns=["symbol", "open", "close"])
            )
            _BENCH_PARTITIONS[d] = df
        sub = df[df["symbol"] == _BENCHMARK]
        if len(sub):
            frames.append(sub.assign(trade_date=d))
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(
        columns=["symbol", "open", "close", "trade_date"]
    )


async def load_signals(session, run_id: str) -> pd.DataFrame:
    rows = (
        await session.execute(
            text(
                "SELECT symbol, fusion_score FROM engine_signal_scores "
                "WHERE run_id = :rid AND fusion_score IS NOT NULL"
            ),
            {"rid": run_id},
        )
    ).fetchall()
    if not rows:
        return pd.DataFrame(columns=["symbol", "score"])
    df = pd.DataFrame(rows, columns=["symbol", "score"])
    df["score"] = pd.to_numeric(df["score"], errors="coerce")
    df = df.dropna(subset=["score"])
    df["symbol"] = df["symbol"].map(lambda s: StockCodeUtil.to_suffix(str(s)))
    return df.drop_duplicates(subset=["symbol"])


async def load_runs(session, *, since: date, until: date) -> pd.DataFrame:
    rows = (
        await session.execute(
            text(
                """
                SELECT DISTINCT ON (r.model_id, r.data_trade_date)
                       r.run_id, r.model_id, r.data_trade_date,
                       COALESCE(m.metadata_json->>'market', 'CN') AS market,
                       CASE WHEN COALESCE(m.metadata_json->>'target_horizon_days', '')
                                 ~ '^[0-9]+$'
                            THEN (m.metadata_json->>'target_horizon_days')::int
                            ELSE 5 END AS horizon
                FROM qm_model_inference_runs r
                LEFT JOIN qm_user_models m
                  ON m.tenant_id = r.tenant_id AND m.user_id = r.user_id
                 AND m.model_id = r.model_id
                WHERE r.status = 'completed' AND r.signals_count > 0
                  AND r.data_trade_date BETWEEN :since AND :until
                ORDER BY r.model_id, r.data_trade_date, r.created_at DESC
                """
            ),
            {"since": since, "until": until},
        )
    ).fetchall()
    return pd.DataFrame(
        rows, columns=["run_id", "model_id", "data_trade_date", "market", "horizon"]
    )


# ── 计算与落库 ──────────────────────────────────────────────────────────


async def ensure_tables() -> None:
    async with get_session() as db:
        await db.execute(text(_COHORT_DDL))
        await db.execute(text(_ROLLING_DDL))
        await db.commit()


async def refresh_model(
    model_id: str,
    market: str,
    horizon: int,
    as_of: date,
    *,
    top_k: int = DEFAULT_TOP_K,
    window: int = WINDOW_COHORTS,
    lookback_days: int = 45,
) -> dict[str, Any]:
    """重算某模型截至 ``as_of`` 的全部已成熟批次 + 滚动行。"""
    since = as_of - timedelta(days=lookback_days + horizon * 2)
    days = trading_days(since, as_of)
    if len(days) < 2:
        return {"model_id": model_id, "status": "no_calendar"}
    pos = {d: i for i, d in enumerate(days)}

    async with get_session(read_only=True) as db:
        runs = await load_runs(db, since=since, until=as_of)
        runs = runs[runs["model_id"] == model_id]
        signal_frames = {}
        for row in runs.itertuples(index=False):
            signal_frames[row.run_id] = await load_signals(db, row.run_id)

    if runs.empty:
        return {"model_id": model_id, "status": "no_runs"}

    symbols_all = set().union(*[set(f["symbol"]) for f in signal_frames.values()]) if signal_frames else set()
    prices = load_prices(days, symbols_all)
    bench = load_benchmark(days)
    px = prices.set_index(["trade_date", "symbol"])
    bench_px = bench.set_index("trade_date") if len(bench) else pd.DataFrame()

    cohorts: list[dict[str, Any]] = []
    for row in runs.itertuples(index=False):
        t = row.data_trade_date
        i = pos.get(t)
        if i is None or i + 1 + horizon >= len(days):
            continue
        entry_day, exit_day = days[i + 1], days[i + 1 + horizon]
        if exit_day > as_of:
            continue  # 未成熟
        scores = signal_frames.get(row.run_id)
        if scores is None or scores.empty:
            continue

        # 每票：T+1 开盘 → T+1+H 收盘（缺任一价 → 不成熟，剔除）
        ent = prices[prices["trade_date"] == entry_day][["symbol", "open"]].rename(columns={"open": "entry"})
        ext = prices[prices["trade_date"] == exit_day][["symbol", "close"]].rename(columns={"close": "exit"})
        merged = ent.merge(ext, on="symbol")
        merged["ret"] = merged["exit"] / merged["entry"] - 1
        returns = merged.set_index("symbol")["ret"]

        bench_ret = float("nan")
        if not bench_px.empty and entry_day in bench_px.index and exit_day in bench_px.index:
            bench_ret = float(
                bench_px.loc[exit_day, "close"] / bench_px.loc[entry_day, "open"] - 1
            )

        m = cohort_metrics(scores, returns, bench_ret, top_k=top_k)
        m.update(
            model_id=model_id,
            market=market,
            horizon_days=int(horizon),
            signal_date=t,
            entry_date=entry_day,
            exit_date=exit_day,
            top_k=int(top_k),
        )
        cohorts.append(m)

    if not cohorts:
        return {"model_id": model_id, "status": "no_matured"}

    cdf = pd.DataFrame(cohorts).sort_values("signal_date")
    async with get_session() as db:
        for rec in cohorts:
            await db.execute(
                text(
                    """
                    INSERT INTO qm_model_scorecard_cohorts
                        (model_id, market, horizon_days, signal_date, entry_date, exit_date,
                         top_k, n_signals, n_matured, win_rate, avg_return, median_return,
                         excess_return, rank_ic, ic, top_decile_avg, bottom_decile_avg, long_short)
                    VALUES (:model_id, :market, :horizon_days, :signal_date, :entry_date, :exit_date,
                            :top_k, :n_signals, :n_matured, :win_rate, :avg_return, :median_return,
                            :excess_return, :rank_ic, :ic, :top_decile_avg, :bottom_decile_avg, :long_short)
                    ON CONFLICT (model_id, signal_date, top_k) DO UPDATE SET
                        market=EXCLUDED.market, horizon_days=EXCLUDED.horizon_days,
                        entry_date=EXCLUDED.entry_date, exit_date=EXCLUDED.exit_date,
                        n_signals=EXCLUDED.n_signals, n_matured=EXCLUDED.n_matured,
                        win_rate=EXCLUDED.win_rate, avg_return=EXCLUDED.avg_return,
                        median_return=EXCLUDED.median_return, excess_return=EXCLUDED.excess_return,
                        rank_ic=EXCLUDED.rank_ic, ic=EXCLUDED.ic,
                        top_decile_avg=EXCLUDED.top_decile_avg,
                        bottom_decile_avg=EXCLUDED.bottom_decile_avg, long_short=EXCLUDED.long_short
                    """
                ),
                {k: _none_if_nan(rec.get(k)) for k in (
                    "model_id", "market", "horizon_days", "signal_date", "entry_date", "exit_date",
                    "top_k", "n_signals", "n_matured", "win_rate", "avg_return", "median_return",
                    "excess_return", "rank_ic", "ic", "top_decile_avg", "bottom_decile_avg", "long_short",
                )},
            )
        await db.commit()

    window_df = cdf.tail(window)
    stats = rolling_stats(window_df)
    async with get_session() as db:
        await db.execute(
            text(
                """
                INSERT INTO qm_model_scorecard_rolling
                    (model_id, market, horizon_days, as_of, top_k, window_days, cohorts,
                     win_rate, avg_return, median_return, cohort_hit_rate, excess_return,
                     rank_ic, ic_ir, t_stat, score, weight)
                VALUES (:model_id, :market, :horizon_days, :as_of, :top_k, :window_days, :cohorts,
                        :win_rate, :avg_return, :median_return, :cohort_hit_rate, :excess_return,
                        :rank_ic, :ic_ir, :t_stat, :score, NULL)
                ON CONFLICT (model_id, as_of, top_k) DO UPDATE SET
                    market=EXCLUDED.market, horizon_days=EXCLUDED.horizon_days,
                    window_days=EXCLUDED.window_days, cohorts=EXCLUDED.cohorts,
                    win_rate=EXCLUDED.win_rate, avg_return=EXCLUDED.avg_return,
                    median_return=EXCLUDED.median_return, cohort_hit_rate=EXCLUDED.cohort_hit_rate,
                    excess_return=EXCLUDED.excess_return, rank_ic=EXCLUDED.rank_ic,
                    ic_ir=EXCLUDED.ic_ir, t_stat=EXCLUDED.t_stat, score=EXCLUDED.score,
                    updated_at=now()
                """
            ),
            {
                "model_id": model_id,
                "market": market,
                "horizon_days": int(horizon),
                "as_of": as_of,
                "top_k": int(top_k),
                "window_days": int(window),
                **{k: _none_if_nan(stats.get(k)) for k in (
                    "cohorts", "win_rate", "avg_return", "median_return", "cohort_hit_rate",
                    "excess_return", "rank_ic", "ic_ir", "t_stat", "score",
                )},
            },
        )
        await db.commit()

    return {
        "model_id": model_id,
        "status": "ok",
        "cohorts": len(cohorts),
        "window": stats,
    }


def _none_if_nan(value: Any) -> Any:
    if isinstance(value, float) and math.isnan(value):
        return None
    return value


async def refresh_all(as_of: date | None = None, *, top_k: int = DEFAULT_TOP_K) -> dict[str, Any]:
    """全模型重算 + 同组权重归一化（as_of 缺省取上海日期）。"""
    if as_of is None:
        as_of = (datetime.now(timezone.utc) + timedelta(hours=8)).date()
    await ensure_tables()
    since = as_of - timedelta(days=60)
    async with get_session(read_only=True) as db:
        runs = await load_runs(db, since=since, until=as_of)
    if runs.empty:
        return {"status": "no_runs", "as_of": str(as_of)}
    metas = runs.drop_duplicates(subset=["model_id"])[["model_id", "market", "horizon"]]
    results = []
    for row in metas.itertuples(index=False):
        try:
            results.append(
                await refresh_model(
                    row.model_id, row.market, int(row.horizon), as_of, top_k=top_k
                )
            )
        except Exception as exc:  # noqa: BLE001 单模型失败不影响其它模型
            logger.exception("[Scorecard] %s 失败", row.model_id)
            results.append({"model_id": row.model_id, "status": "error", "error": str(exc)})

    # 同 market+horizon 组内归一化权重
    async with get_session() as db:
        rows = (
            await db.execute(
                text(
                    "SELECT model_id, market, horizon_days, score FROM qm_model_scorecard_rolling "
                    "WHERE as_of = :d AND top_k = :k"
                ),
                {"d": as_of, "k": int(top_k)},
            )
        ).fetchall()
        groups: dict[tuple[str, int], list[dict[str, Any]]] = {}
        for r in rows:
            groups.setdefault((str(r[1]), int(r[2])), []).append(
                {"model_id": str(r[0]), "score": float(r[3] or 0.0)}
            )
        for (market, horizon), members in groups.items():
            weights = normalize_weights(members)
            for mid, w in weights.items():
                await db.execute(
                    text(
                        "UPDATE qm_model_scorecard_rolling SET weight = :w, updated_at = now() "
                        "WHERE model_id = :m AND as_of = :d AND top_k = :k"
                    ),
                    {"w": w, "m": mid, "d": as_of, "k": int(top_k)},
                )
        await db.commit()

    ok = [r for r in results if r.get("status") == "ok"]
    return {"status": "ok", "as_of": str(as_of), "models": len(results), "computed": len(ok), "results": results}
