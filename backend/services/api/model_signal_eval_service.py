"""测试集实盘力评估（滚动 3 个月）— API 侧计算、缓存与后台刷新。

回答「这个模型最近在推理过的测试集上排得怎么样、长多能不能跑赢打分池」。
与离线脚本的分工：
- 口径函数复用 ``backend/scripts/eval/signal_metrics.py``（IC/中性化/分位/稳定性）
  与 ``ff_attribution.py``（可执行 cohort 组合）；
- 本模块只做窗口裁剪、动态标签（行情现算）、缓存/作业状态与 API 载荷。

为什么不用 pred 自带的 ``label_return``：推理补推会追加没有标签的新日期，
滚动窗口要「随推理与行情自动前移」，所以统一用 ``daily_forward`` 现算
T+1 收盘 → T+H 收盘（模型训练口径），窗口末端取最后一个标签完整的交易日。

缓存与作业状态落在 ``<data>/factor_research/model_signal/<model_id>/``：
- ``ui_3m.json``：评估结果（原子写）
- ``ui_3m.status.json``：running/done/error + progress
- ``ui_3m.lock``：文件锁，防多 worker 重复计算
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from backend.scripts.eval.ff_attribution import (
    _load_st_pit,
    _trading_days,
    build_cohort_daily_returns,
)
from backend.scripts.eval.signal_metrics import (
    DECAY_HORIZONS,
    _attach_market_fields,
    daily_cross_section_ic,
    ic_decay,
    ic_summary,
    load_industry_map,
    price_forward_returns,
    quantile_report,
    stability_report,
)
from backend.shared.pred_freshness import pred_parquet_path
from backend.shared.quantdb_paths import resolve_quantdb_dir, resolve_quantdb_subdir
from backend.shared.stock_utils import StockCodeUtil

log = logging.getLogger(__name__)

EVAL_VERSION = 3  # 改口径必须 +1：指纹含它，旧缓存会因此失效并自动重算
WINDOW_TRADING_DAYS = 63  # 约 3 个自然月
TOPN_LIST = (20, 50, 100)
DEFAULT_HORIZON = 5
DEFAULT_MIN_ADV = 2.0e7
DEFAULT_COST_BPS = 15.0
LOCK_STALE_SECONDS = 15 * 60
# 指数统一读 1_kline_data/index_daily（open/close 全有），基准超额按 open→open 对齐
# TopN 的 T+1 开盘买入口径；不用 daily_forward 里的指数（只有部分、且只有 close）。
INDEX_SYMBOLS = {
    "hs300": "399300.SZ",  # 沪深300
    "sse": "000001.SH",  # 上证指数
    "szse": "399001.SZ",  # 深证成指
    "zz1000": "000852.SH",  # 中证1000
}
HISTORY_LIMIT = 120


class EvalUnavailable(RuntimeError):
    """模型不满足评估条件（无 pred / 非 A 股 / 窗口不足）。"""


# ---------------------------------------------------------------------------
# 路径与窗口
# ---------------------------------------------------------------------------
def _cache_dir(model_id: str) -> Path:
    return resolve_quantdb_dir().parent / "factor_research" / "model_signal" / model_id


def _cache_paths(model_id: str, window_days: int) -> dict[str, Path]:
    tag = f"ui_{window_days}d"
    base = _cache_dir(model_id)
    return {
        "report": base / f"{tag}.json",
        "status": base / f"{tag}.status.json",
        "lock": base / f"{tag}.lock",
    }


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, default=float), encoding="utf-8"
    )
    os.replace(tmp, path)


def _read_json(path: Path) -> dict | None:
    try:
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _latest_data_day() -> str:
    """daily_forward 最新分区日（YYYYMMDD）；用于缓存指纹随行情前移。"""
    fwd = resolve_quantdb_subdir("1_kline_data", "daily_forward")
    days = sorted(p.name.split("=", 1)[-1] for p in fwd.glob("dt=*"))
    return days[-1] if days else ""


def pred_fingerprint(pred_path: Path, *, horizon: int, window_days: int) -> dict:
    try:
        stat = pred_path.stat()
    except OSError:
        stat = None
    return {
        "pred_mtime": round(stat.st_mtime, 3) if stat else None,
        "pred_size": stat.st_size if stat else None,
        "data_day": _latest_data_day(),
        "horizon": int(horizon),
        "window_days": int(window_days),
        "eval_version": EVAL_VERSION,
    }


def test_signal_dates(pred_path: Path) -> list[str]:
    """pred 中 split=test 的信号日（YYYYMMDD，升序）。"""
    df = pd.read_parquet(pred_path, columns=["trade_date", "split"])
    df = df[df["split"] == "test"]
    if df.empty:
        return []
    dates = pd.to_datetime(df["trade_date"], errors="coerce").dropna()
    return sorted({d.strftime("%Y%m%d") for d in dates})


def pick_window(
    dates: list[str], tdays: list[str], horizon: int, window_days: int
) -> dict:
    """从信号日里裁出「标签完整」的最后 window_days 个交易日。

    ``dates`` 与 ``tdays`` 均为 YYYYMMDD；标签完整 = 信号日之后还有 T+1..T+1+H。
    """
    if not dates or not tdays:
        raise EvalUnavailable("没有可评估的测试集信号日")
    idx = {d: i for i, d in enumerate(tdays)}
    valid = [d for d in dates if d in idx and idx[d] + 1 + int(horizon) < len(tdays)]
    if len(valid) < 10:
        raise EvalUnavailable(
            f"测试集有标签的信号日不足（{len(valid)} 天，至少 10 天）"
        )
    end = valid[-1]
    start = valid[max(0, len(valid) - int(window_days))]
    return {
        "start": start,
        "end": end,
        "signal_days": len([d for d in valid if d >= start]),
        "pred_end": dates[-1],
        "first_signal": valid[0],
        "last_signal": end,
    }


# ---------------------------------------------------------------------------
# 数据装载
# ---------------------------------------------------------------------------
def _picks_from_frame(frame: pd.DataFrame, n: int) -> dict[str, list[str]]:
    picks: dict[str, list[str]] = {}
    for date_key, group in frame.groupby("date_key", sort=True):
        syms = group.nlargest(n, "pred")["sym_suffix"].tolist()
        if syms:
            picks[str(date_key)] = syms
    return picks


def load_rolling_frame(
    pred_path: Path,
    window: dict,
    *,
    horizon: int,
    min_adv: float = DEFAULT_MIN_ADV,
    include_st: bool = False,
) -> pd.DataFrame:
    """test 截面 + 行情现算标签 + 市值/行业/ADV/ST 过滤。"""
    df = pd.read_parquet(pred_path, columns=["symbol", "trade_date", "pred", "split"])
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df = df[df["split"] == "test"].dropna(subset=["pred"])
    lo = pd.Timestamp(window["start"])
    hi = pd.Timestamp(window["end"])
    df = df[(df["trade_date"] >= lo) & (df["trade_date"] <= hi)].copy()
    if df.empty:
        raise EvalUnavailable("窗口内没有推理结果")
    df["date_key"] = df["trade_date"].dt.strftime("%Y%m%d")
    df["sym_suffix"] = df["symbol"].map(lambda s: StockCodeUtil.to_suffix(str(s)))
    start_iso = pd.Timestamp(window["start"]).strftime("%Y-%m-%d")
    end_iso = pd.Timestamp(window["end"]).strftime("%Y-%m-%d")
    df = _attach_market_fields(df, start_iso, end_iso)
    if min_adv > 0:
        amount_yuan = np.exp(df["amt_log"].to_numpy(dtype=float)) * 1e4
        df = df[amount_yuan >= float(min_adv)].copy()
    if not include_st:
        st = _load_st_pit()
        if st:
            mask = [
                (s, d) not in st
                for s, d in zip(df["sym_suffix"], df["date_key"], strict=True)
            ]
            df = df[mask].copy()
    if df.empty:
        raise EvalUnavailable("ADV/ST 过滤后样本为空")
    rets = price_forward_returns(
        sorted(df["sym_suffix"].unique()), start_iso, end_iso, (horizon,)
    )["close_entry"][horizon]
    df = df.merge(
        rets.rename("label_return"),
        left_on=["date_key", "sym_suffix"],
        right_index=True,
        how="left",
    ).dropna(subset=["label_return"])
    if df.empty:
        raise EvalUnavailable("窗口内没有标签完整的推理行")
    df["log_size"] = pd.to_numeric(df["fun_total_mv"], errors="coerce")
    industry = load_industry_map()
    df["industry"] = df["sym_suffix"].map(industry).fillna("UNKNOWN")
    return df.reset_index(drop=True)


# ---------------------------------------------------------------------------
# 组合与基准
# ---------------------------------------------------------------------------
def equal_weight_market_returns(symbols: list[str], start: str, end: str) -> pd.Series:
    """打分池等权日收益（daily_forward 开盘价，open→open）。"""
    import duckdb

    if not symbols:
        return pd.Series(dtype=float)
    fwd = resolve_quantdb_subdir("1_kline_data", "daily_forward")
    glob_pat = str(fwd / "dt=*" / "*.parquet").replace("\\", "/")
    lo = (pd.Timestamp(start) - pd.Timedelta(days=10)).strftime("%Y-%m-%d")
    hi = (pd.Timestamp(end) + pd.Timedelta(days=10)).strftime("%Y-%m-%d")
    con = duckdb.connect()
    try:
        px = con.execute(
            f"""
            SELECT symbol, strftime(time, '%Y%m%d') AS date_key, open
            FROM read_parquet('{glob_pat}', hive_partitioning=1)
            WHERE time >= DATE '{lo}' AND time <= DATE '{hi}'
            """
        ).fetchdf()
    finally:
        con.close()
    px = px[px["symbol"].isin(set(symbols))]
    if px.empty:
        return pd.Series(dtype=float)
    wide = px.pivot(index="date_key", columns="symbol", values="open").sort_index()
    rets = wide.pct_change(fill_method=None)
    series = rets.loc[rets.index >= start].mean(axis=1)
    series.index = pd.to_datetime(series.index, format="%Y%m%d")
    return series.dropna()


def index_daily_returns(start: str, end: str) -> dict[str, pd.Series]:
    """四大指数日收益（``index_daily`` 开盘价 open→open）。

    与 TopN 的 T+1 开盘买入、逐日 open→open 口径严格对齐；不用 close→close，
    避免「组合开盘价、指数收盘价」的时点错配。中证500/中证1000 的代码在
    ``index_daily`` 里是 ``.SH``（不是 daily_forward 里的 ``.SZ``）。
    """
    import duckdb

    idx_dir = resolve_quantdb_subdir("1_kline_data", "index_daily")
    glob_pat = str(idx_dir / "dt=*" / "*.parquet").replace("\\", "/")
    lo = (pd.Timestamp(start) - pd.Timedelta(days=10)).strftime("%Y-%m-%d")
    hi = (pd.Timestamp(end) + pd.Timedelta(days=10)).strftime("%Y-%m-%d")
    con = duckdb.connect()
    try:
        px = con.execute(
            f"""
            SELECT symbol, strftime(time, '%Y%m%d') AS date_key, open
            FROM read_parquet('{glob_pat}', hive_partitioning=1)
            WHERE time >= DATE '{lo}' AND time <= DATE '{hi}'
              AND symbol IN ({",".join(f"'{s}'" for s in INDEX_SYMBOLS.values())})
            """
        ).fetchdf()
    finally:
        con.close()
    out: dict[str, pd.Series] = {}
    for name, symbol in INDEX_SYMBOLS.items():
        rows = px[px["symbol"] == symbol].sort_values("date_key")
        if rows.empty:
            continue
        s = pd.Series(
            rows["open"].to_numpy(dtype=float),
            index=pd.to_datetime(rows["date_key"], format="%Y%m%d"),
        )
        s = s.where(s > 0)
        ret = s.pct_change(fill_method=None).dropna()
        ret = ret[ret.index >= pd.Timestamp(start)]
        out[name] = ret
    return out


def _daily_stats(
    daily: pd.Series, market: pd.Series, benchmarks: dict[str, pd.Series] | None = None
) -> dict:
    if daily.empty:
        return {}
    nav = (1.0 + daily).cumprod()
    peak = nav.cummax()
    mdd = float((nav / peak - 1.0).min())
    std = float(daily.std(ddof=1))
    common = daily.index.intersection(market.index)
    if len(common) >= 5:
        top_nav = float((1.0 + daily.loc[common]).prod())
        mkt_nav = float((1.0 + market.loc[common]).prod())
        excess = top_nav - mkt_nav
        diff = (daily.loc[common] - market.loc[common]).dropna()
        excess_t = (
            float(diff.mean() / diff.std(ddof=1) * np.sqrt(len(diff)))
            if len(diff) > 2 and diff.std(ddof=1) > 0
            else np.nan
        )
    else:
        top_nav = float(nav.iloc[-1])
        mkt_nav = np.nan
        excess = np.nan
        excess_t = np.nan
    bench_stats: dict[str, float] = {}
    for name, series in (benchmarks or {}).items():
        common_b = daily.index.intersection(series.index)
        if len(common_b) >= 5:
            top_b = float((1.0 + daily.loc[common_b]).prod())
            idx_b = float((1.0 + series.loc[common_b]).prod())
            bench_stats[f"excess_{name}"] = top_b - idx_b
            bench_stats[f"{name}_return"] = idx_b - 1.0
        else:
            bench_stats[f"excess_{name}"] = np.nan
            bench_stats[f"{name}_return"] = np.nan
    return {
        "total_return": float(nav.iloc[-1] - 1.0),
        "annualized": float(nav.iloc[-1] ** (252.0 / len(daily)) - 1.0)
        if len(daily) > 0
        else np.nan,
        "mdd": mdd,
        "sharpe": float(daily.mean() / std * np.sqrt(252.0)) if std > 0 else np.nan,
        "excess": excess,
        "excess_t": excess_t,
        "market_return": float(mkt_nav - 1.0) if np.isfinite(mkt_nav) else np.nan,
        "days": int(len(daily)),
        "nav": [
            {"date": d.strftime("%Y-%m-%d"), "nav": round(float(v), 4)}
            for d, v in nav.items()
        ],
        **bench_stats,
    }


def topn_evaluation(
    frame: pd.DataFrame,
    window: dict,
    *,
    horizon: int,
    cost_bps: float,
    min_adv: float,
) -> list[dict]:
    symbols = sorted(frame["sym_suffix"].unique())
    market = equal_weight_market_returns(symbols, window["start"], window["end"])
    benchmarks = index_daily_returns(window["start"], window["end"])
    rows: list[dict] = []
    for n in TOPN_LIST:
        picks = _picks_from_frame(frame, n)
        daily, cohort = build_cohort_daily_returns(
            picks,
            horizon=horizon,
            cost_bps=cost_bps,
            start=window["start"],
            end=window["end"],
            min_adv=min_adv,
        )
        stats = _daily_stats(daily, market, benchmarks)
        rows.append(
            {
                "n": n,
                **{k: v for k, v in stats.items() if k != "nav"},
                "avg_names": cohort.get("avg_names"),
                "capacity_p5": cohort.get("capacity_p5"),
                "capacity_p10": cohort.get("capacity_p10"),
                "cohorts": cohort.get("cohorts"),
            }
        )
    return rows


# ---------------------------------------------------------------------------
# 指标聚合
# ---------------------------------------------------------------------------
def _weekly_ic(ic_daily: pd.DataFrame) -> list[dict]:
    if ic_daily.empty:
        return []
    idx = pd.to_datetime(ic_daily.index, format="%Y%m%d")
    frame = ic_daily.assign(week_start=idx.to_period("W").start_time)
    weekly = frame.groupby("week_start")[["ic_raw", "ic_neutral"]].mean().reset_index()
    weekly["week"] = pd.to_datetime(weekly["week_start"]).dt.strftime("%m-%d")
    return [
        {
            "week": row["week"],
            "ic": round(float(row["ic_raw"]), 4)
            if np.isfinite(row["ic_raw"])
            else None,
            "ic_neutral": round(float(row["ic_neutral"]), 4)
            if np.isfinite(row["ic_neutral"])
            else None,
        }
        for _, row in weekly.iterrows()
    ]


def build_verdict(
    raw: dict, neutral: dict, quant: dict, stab: dict, topn: list[dict]
) -> dict:
    """近 3 个月监控线（不是准入线；阈值比离线研究宽）。"""
    ranking_pass = bool(
        np.isfinite(raw.get("mean", np.nan))
        and raw["mean"] >= 0.03
        and raw.get("hac_t", 0) >= 2
    )
    neutral_pass = bool(
        np.isfinite(neutral.get("mean", np.nan))
        and neutral["mean"] >= 0.02
        and neutral.get("hac_t", 0) >= 2
    )
    decile_pass = bool(
        np.isfinite(quant.get("monotonicity", np.nan))
        and quant["monotonicity"] >= 0.7
        and quant.get("spread_mean", -1) > 0
    )
    top20 = next((r for r in topn if r["n"] == 20), {})
    top50 = next((r for r in topn if r["n"] == 50), {})
    long_pass = bool(
        np.isfinite(top20.get("excess", np.nan))
        and top20["excess"] > 0
        and top20.get("excess_t", 0) >= 1.5
        and np.isfinite(top50.get("excess", np.nan))
        and top50["excess"] > 0
    )
    stability_pass = bool(
        np.isfinite(stab.get("rank_autocorr_daily", np.nan))
        and stab["rank_autocorr_daily"] >= 0.8
    )
    if ranking_pass and neutral_pass and not long_pass:
        summary = "排序有效，但长多没跑赢打分池：先当选股输入/风险分层，不直接当多头。"
    elif ranking_pass and neutral_pass and long_pass:
        summary = "排序与长多都过监控线：可进影子盘，仍需看更长的 α 样本。"
    elif not ranking_pass and long_pass:
        summary = "排序弱但长多跑赢：先查是否少数极端票或风格暴露，不采信。"
    else:
        summary = "近 3 个月没有可确认的截面排序能力，不应作为选股信号。"
    return {
        "ranking_pass": ranking_pass,
        "neutral_pass": neutral_pass,
        "decile_pass": decile_pass,
        "long_pass": long_pass,
        "stability_pass": stability_pass,
        "summary": summary,
    }


def coverage_stats(pred_path: Path, window: dict, frame: pd.DataFrame) -> dict:
    """评估样本对「全部 test 推理行」的覆盖率：识别补推缺口/停牌造成的结构性缺日。"""
    raw = pd.read_parquet(pred_path, columns=["trade_date", "pred", "split"])
    raw["trade_date"] = pd.to_datetime(raw["trade_date"], errors="coerce")
    raw = raw[raw["split"] == "test"].dropna(subset=["trade_date", "pred"])
    raw = raw[
        (raw["trade_date"] >= pd.Timestamp(window["start"]))
        & (raw["trade_date"] <= pd.Timestamp(window["end"]))
    ]
    raw["date_key"] = raw["trade_date"].dt.strftime("%Y%m%d")
    counts = raw.groupby("date_key").size()
    if counts.empty:
        return {
            "avg": np.nan,
            "min": np.nan,
            "days": 0,
            "days_below_50": 0,
            "low_days": [],
        }
    got = frame.groupby("date_key").size()
    cover = (got.reindex(counts.index).fillna(0) / counts).clip(upper=1.0)
    low = [d for d, c in cover.items() if c < 0.5]
    return {
        "avg": float(cover.mean()),
        "min": float(cover.min()),
        "days": int(len(cover)),
        "days_below_50": int(len(low)),
        "low_days": low[:10],
    }


def admission_from_report(report: dict, *, stale: bool = False) -> dict:
    """标准化准入等级：green=五闸全过；yellow=排序/中性化过、长多或稳定性未过；red=排序不成立。"""
    verdict = report.get("verdict") or {}
    checks = {
        "ranking": bool(verdict.get("ranking_pass")),
        "neutral": bool(verdict.get("neutral_pass")),
        "decile": bool(verdict.get("decile_pass")),
        "long": bool(verdict.get("long_pass")),
        "stability": bool(verdict.get("stability_pass")),
    }
    labels = {
        "ranking": "截面排序",
        "neutral": "市值/行业中性化",
        "decile": "分位单调性",
        "long": "TopN 长多超额",
        "stability": "打分稳定性",
    }
    if all(checks.values()):
        grade = "green"
    elif checks["ranking"] and checks["neutral"]:
        grade = "yellow"
    else:
        grade = "red"
    reasons = [labels[k] for k, ok in checks.items() if not ok]
    # 独立周期太少时不允许给 green：T+20 在 63 个交易日里只有 3 个独立周期，
    # 五闸全过也可能只是少数几段行情，必须降级为观察。
    independent = int((report.get("window") or {}).get("independent_periods") or 0)
    if grade == "green" and independent and independent < 4:
        grade = "yellow"
        reasons.append(f"独立周期仅 {independent} 个（<4）")
    if stale:
        reasons.append("评估已过期")
    return {
        "grade": grade,
        "checks": checks,
        "reasons": reasons,
        "window_end": (report.get("window") or {}).get("end"),
        "checked_at": report.get("computed_at"),
        "stale": stale,
        "eval_version": report.get("eval_version", EVAL_VERSION),
    }


def unknown_admission(reason: str = "未完成标准化评估") -> dict:
    return {
        "grade": "unknown",
        "checks": {},
        "reasons": [reason],
        "window_end": None,
        "checked_at": None,
        "stale": True,
        "eval_version": EVAL_VERSION,
    }


# ---------------------------------------------------------------------------
# 历史快照（每日滚动体检趋势）
# ---------------------------------------------------------------------------
def _history_path(model_id: str, window_days: int) -> Path:
    return _cache_dir(model_id) / f"ui_{window_days}d.history.jsonl"


def snapshot_from_report(report: dict) -> dict:
    metrics = report.get("metrics") or {}
    quant = metrics.get("quantiles") or {}
    topn = report.get("topn") or []
    top20 = next((r for r in topn if r.get("n") == 20), {})
    top50 = next((r for r in topn if r.get("n") == 50), {})
    rank = metrics.get("rank_ic") or {}
    neutral = metrics.get("neutral_ic") or {}
    stab = metrics.get("stability") or {}
    return {
        "computed_at": report.get("computed_at"),
        "window_start": (report.get("window") or {}).get("start"),
        "window_end": (report.get("window") or {}).get("end"),
        "signal_days": (report.get("window") or {}).get("signal_days"),
        "rank_ic": rank.get("mean"),
        "neutral_ic": neutral.get("mean"),
        "icir": rank.get("icir"),
        "spread": quant.get("spread_mean"),
        "monotonicity": quant.get("monotonicity"),
        "top20_return": top20.get("total_return"),
        "top20_excess": top20.get("excess"),
        "top50_excess": top50.get("excess"),
        "stability": stab.get("rank_autocorr_daily"),
        "grade": (report.get("admission") or {}).get("grade"),
        "long_pass": (report.get("verdict") or {}).get("long_pass"),
    }


def append_history(model_id: str, window_days: int, report: dict) -> None:
    path = _history_path(model_id, window_days)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(snapshot_from_report(report), ensure_ascii=False) + "\n"
            )
    except OSError as exc:
        log.warning("写评估历史失败 model=%s: %s", model_id, exc)


def read_history(
    model_id: str, window_days: int, limit: int = HISTORY_LIMIT
) -> list[dict]:
    path = _history_path(model_id, window_days)
    try:
        if not path.is_file():
            return []
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("window_end"):
                rows.append(row)
    except OSError:
        return []
    # 同一窗口重算只保留最新一条；按窗口结束日升序取最后 limit 条
    by_window = {row["window_end"]: row for row in rows}
    ordered = [by_window[key] for key in sorted(by_window)]
    return ordered[-limit:]


# ---------------------------------------------------------------------------
# 主计算
# ---------------------------------------------------------------------------
def compute_report(
    model_id: str,
    storage_path: str,
    *,
    market: str = "CN",
    horizon: int = DEFAULT_HORIZON,
    window_days: int = WINDOW_TRADING_DAYS,
    min_adv: float = DEFAULT_MIN_ADV,
    cost_bps: float = DEFAULT_COST_BPS,
    include_st: bool = False,
    progress: Callable[[str, int, str], None] | None = None,
) -> dict:
    def tick(step: str, pct: int, message: str) -> None:
        if progress:
            progress(step, pct, message)

    if str(market).upper() != "CN":
        raise EvalUnavailable(f"当前仅支持 A 股模型（market={market}）")
    pred_path = pred_parquet_path(storage_path)
    if pred_path is None:
        raise EvalUnavailable("模型目录没有 pred.parquet（还没推理过）")

    tick("window", 5, "确定滚动窗口…")
    dates = test_signal_dates(pred_path)
    if not dates:
        raise EvalUnavailable("pred 里没有 test 切分（还没在测试集上推理）")
    tdays = _trading_days(
        dates[0],
        (pd.Timestamp(dates[-1]) + pd.Timedelta(days=120)).strftime("%Y-%m-%d"),
    )
    window = pick_window(dates, tdays, horizon, window_days)

    tick("load", 15, "加载推理与行情…")
    frame = load_rolling_frame(
        pred_path,
        window,
        horizon=horizon,
        min_adv=min_adv,
        include_st=include_st,
    )

    tick("ic", 35, "计算截面 IC / 中性化…")
    ic_daily = daily_cross_section_ic(frame)
    # 日频 IC 有 H 日重叠；HAC lag 必须随持有期放大（T+20 模型用 lag4 会低估标准误）
    hac_lags = max(1, min(int(horizon) - 1, 30))
    raw = ic_summary(ic_daily["ic_raw"], lags=hac_lags)
    neutral = ic_summary(ic_daily["ic_neutral"], lags=hac_lags)
    symbols = sorted(frame["sym_suffix"].unique())
    start_iso = pd.Timestamp(window["start"]).strftime("%Y-%m-%d")
    end_iso = pd.Timestamp(window["end"]).strftime("%Y-%m-%d")
    decay_rets = price_forward_returns(symbols, start_iso, end_iso, DECAY_HORIZONS)[
        "close_entry"
    ]
    decay = ic_decay(frame, decay_rets)

    tick("decile", 55, "计算分位与稳定性…")
    quant = quantile_report(frame, 10)
    stab = stability_report(frame, horizon, list(TOPN_LIST))
    coverage = coverage_stats(pred_path, window, frame)

    tick("portfolio", 70, "模拟 TopN 长多…")
    topn = topn_evaluation(
        frame,
        window,
        horizon=horizon,
        cost_bps=cost_bps,
        min_adv=min_adv,
    )
    verdict = build_verdict(raw, neutral, quant, stab, topn)
    benchmarks = index_daily_returns(start_iso, end_iso)
    bench_series = {
        name: [
            {"date": d.strftime("%Y-%m-%d"), "nav": round(float(v), 4)}
            for d, v in (1.0 + series).cumprod().items()
        ]
        for name, series in benchmarks.items()
    }
    pred_lag_days = len(
        [
            d
            for d in tdays
            if window["end"] < d <= pd.Timestamp(window["pred_end"]).strftime("%Y%m%d")
        ]
    )

    tick("done", 100, "完成")
    report = {
        "status": "ready",
        "model_id": model_id,
        "eval_version": EVAL_VERSION,
        "computed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "window": {
            "label": f"近 {window['signal_days']} 个交易日",
            "start": pd.Timestamp(window["start"]).strftime("%Y-%m-%d"),
            "end": pd.Timestamp(window["end"]).strftime("%Y-%m-%d"),
            "signal_days": window["signal_days"],
            "pred_end": pd.Timestamp(window["pred_end"]).strftime("%Y-%m-%d"),
            "pred_lag_days": int(pred_lag_days),
            "horizon": int(horizon),
            "independent_periods": max(1, int(window["signal_days"]) // int(horizon)),
        },
        "sample": {
            "rows": int(len(frame)),
            "symbols": int(frame["sym_suffix"].nunique()),
            "avg_names": float(frame.groupby("date_key").size().mean()),
        },
        "coverage": coverage,
        "metrics": {
            "rank_ic": raw,
            "neutral_ic": neutral,
            "decay": decay.to_dict(orient="records"),
            "quantiles": quant,
            "stability": stab,
        },
        "series": {"weekly_ic": _weekly_ic(ic_daily), "benchmarks": bench_series},
        "topn": topn,
        "verdict": verdict,
        "execution": {
            "entry": "T+1 开盘",
            "exit": f"T+1+{int(horizon)} 开盘",
            "cost_bps_per_side": float(cost_bps),
            "cost_note": "双边成本含佣金/印花税/滑点（按单边 bps 均摊）",
            "limit_up_entry_excluded": True,
            "limit_down_exit_extended": True,
            "st_pit_excluded": not include_st,
            "adv_floor_yuan": float(min_adv),
            "missing_price_skipped": True,
            "benchmark_basis": "open→open（与组合同一时点）",
        },
    }
    report["admission"] = admission_from_report(report)
    return report


# ---------------------------------------------------------------------------
# 缓存 / 作业状态
# ---------------------------------------------------------------------------
def _status_payload(
    status: str,
    *,
    progress: int = 0,
    step: str = "",
    message: str = "",
    error: str | None = None,
) -> dict:
    return {
        "status": status,
        "progress": int(progress),
        "step": step,
        "message": message,
        "error": error,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def read_status(model_id: str, window_days: int) -> dict | None:
    return _read_json(_cache_paths(model_id, window_days)["status"])


def get_state(
    model_id: str,
    storage_path: str,
    *,
    market: str = "CN",
    horizon: int = DEFAULT_HORIZON,
    window_days: int = WINDOW_TRADING_DAYS,
) -> dict:
    """GET 载荷：缓存是否可用、是否过期、后台是否在跑。"""
    if str(market).upper() != "CN":
        return {
            "status": "unavailable",
            "reason": f"当前仅支持 A 股模型（market={market}）",
        }
    pred_path = pred_parquet_path(storage_path)
    if pred_path is None:
        return {
            "status": "unavailable",
            "reason": "模型目录没有 pred.parquet（还没推理过）",
        }
    paths = _cache_paths(model_id, window_days)
    fingerprint = pred_fingerprint(pred_path, horizon=horizon, window_days=window_days)
    cached = _read_json(paths["report"])
    cached_report = cached.get("report") if cached else None
    history = read_history(model_id, window_days)
    status = read_status(model_id, window_days) or {}
    # 进程被杀时 lock 会在 finally 前残留/状态停在 running：锁文件不在即视为无作业
    if status.get("status") == "running" and not paths["lock"].is_file():
        status = {}
    stale = bool(cached) and cached.get("fingerprint") != fingerprint
    if status.get("status") == "running":
        return {
            "status": "running",
            "progress": status.get("progress", 0),
            "step": status.get("step", ""),
            "message": status.get("message", ""),
            "cached_report": cached_report,
            "admission": admission_from_report(cached_report, stale=True)
            if cached_report
            else unknown_admission("评估计算中"),
            "history": history,
            "stale": stale,
        }
    if status.get("status") == "error":
        return {
            "status": "stale" if cached else "missing",
            "report": cached_report,
            "admission": admission_from_report(cached_report, stale=True)
            if cached_report
            else unknown_admission(status.get("error") or "计算失败"),
            "history": history,
            "stale": True,
            "reason": status.get("error") or "计算失败，请重试",
        }
    if cached and not stale:
        return {
            "status": "ready",
            "report": cached_report,
            "admission": admission_from_report(cached_report),
            "history": history,
            "stale": False,
        }
    if cached and stale:
        return {
            "status": "stale",
            "report": cached_report,
            "admission": admission_from_report(cached_report, stale=True),
            "history": history,
            "stale": True,
            "reason": "推理或行情已更新，点刷新重算",
        }
    return {
        "status": "missing",
        "stale": True,
        "admission": unknown_admission(),
        "history": history,
        "reason": "还没有生成过统计",
    }


def admission_status(
    model_id: str,
    storage_path: str,
    *,
    market: str = "CN",
    horizon: int = DEFAULT_HORIZON,
    window_days: int = WINDOW_TRADING_DAYS,
) -> dict:
    """准入等级（只读缓存，不触发计算）；供模型激活/策略绑定前的闸门调用。"""
    try:
        state = get_state(
            model_id,
            storage_path,
            market=market,
            horizon=horizon,
            window_days=window_days,
        )
    except Exception as exc:  # noqa: BLE001 — 读取失败按 unknown 处理，由调用方决定是否放行
        log.warning("读取准入状态失败 model=%s: %s", model_id, exc)
        return unknown_admission("评估状态读取失败")
    return state.get("admission") or unknown_admission()


def _acquire_lock(lock_path: Path) -> bool:
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        try:
            age = time.time() - lock_path.stat().st_mtime
            if age > LOCK_STALE_SECONDS:
                lock_path.unlink(missing_ok=True)
                return _acquire_lock(lock_path)
        except OSError:
            pass
        return False
    except OSError:
        return False


def _release_lock(lock_path: Path) -> None:
    try:
        lock_path.unlink(missing_ok=True)
    except OSError:
        pass


def _compute_and_store(
    model_id: str,
    storage_path: str,
    *,
    market: str,
    horizon: int,
    window_days: int,
    min_adv: float,
    cost_bps: float,
    include_st: bool,
    progress: Callable[[str, int, str], None],
) -> dict:
    report = compute_report(
        model_id,
        storage_path,
        market=market,
        horizon=horizon,
        window_days=window_days,
        min_adv=min_adv,
        cost_bps=cost_bps,
        include_st=include_st,
        progress=progress,
    )
    pred_path = pred_parquet_path(storage_path)
    assert pred_path is not None
    report["fingerprint"] = pred_fingerprint(
        pred_path, horizon=horizon, window_days=window_days
    )
    paths = _cache_paths(model_id, window_days)
    _atomic_write_json(
        paths["report"], {"fingerprint": report["fingerprint"], "report": report}
    )
    append_history(model_id, window_days, report)
    _atomic_write_json(
        paths["status"],
        _status_payload("done", progress=100, step="done", message="完成"),
    )
    return report


def _run_job(
    model_id: str,
    storage_path: str,
    *,
    market: str,
    horizon: int,
    window_days: int,
    min_adv: float,
    cost_bps: float,
    include_st: bool,
) -> None:
    paths = _cache_paths(model_id, window_days)

    def progress(step: str, pct: int, message: str) -> None:
        _atomic_write_json(
            paths["status"],
            _status_payload("running", progress=pct, step=step, message=message),
        )

    try:
        progress("window", 2, "准备计算…")
        _compute_and_store(
            model_id,
            storage_path,
            market=market,
            horizon=horizon,
            window_days=window_days,
            min_adv=min_adv,
            cost_bps=cost_bps,
            include_st=include_st,
            progress=progress,
        )
    except Exception as exc:  # noqa: BLE001 — 作业错误要显式落盘给前端
        log.exception("模型实盘力评估失败 model=%s", model_id)
        _atomic_write_json(
            paths["status"],
            _status_payload("error", error=str(exc), message="计算失败"),
        )
    finally:
        _release_lock(paths["lock"])


def refresh_sync(
    model_id: str,
    storage_path: str,
    *,
    market: str = "CN",
    horizon: int = DEFAULT_HORIZON,
    window_days: int = WINDOW_TRADING_DAYS,
    min_adv: float = DEFAULT_MIN_ADV,
    cost_bps: float = DEFAULT_COST_BPS,
    include_st: bool = False,
) -> dict | None:
    """同步刷新（供 Celery 日更使用）：拿不到锁返回 None，不并发重复算。"""
    paths = _cache_paths(model_id, window_days)
    if not _acquire_lock(paths["lock"]):
        return None

    def progress(step: str, pct: int, message: str) -> None:
        _atomic_write_json(
            paths["status"],
            _status_payload("running", progress=pct, step=step, message=message),
        )

    try:
        progress("window", 2, "日更评估…")
        return _compute_and_store(
            model_id,
            storage_path,
            market=market,
            horizon=horizon,
            window_days=window_days,
            min_adv=min_adv,
            cost_bps=cost_bps,
            include_st=include_st,
            progress=progress,
        )
    except Exception as exc:  # noqa: BLE001 — 状态落盘，便于第二天排查
        log.exception("模型实盘力日更失败 model=%s", model_id)
        _atomic_write_json(
            paths["status"],
            _status_payload("error", error=str(exc), message="计算失败"),
        )
        raise
    finally:
        _release_lock(paths["lock"])


def start_refresh(
    model_id: str,
    storage_path: str,
    *,
    market: str = "CN",
    horizon: int = DEFAULT_HORIZON,
    window_days: int = WINDOW_TRADING_DAYS,
    min_adv: float = DEFAULT_MIN_ADV,
    cost_bps: float = DEFAULT_COST_BPS,
    include_st: bool = False,
) -> dict:
    """启动后台计算；已有活跃作业时直接返回 running。"""
    if str(market).upper() != "CN":
        return {
            "status": "unavailable",
            "reason": f"当前仅支持 A 股模型（market={market}）",
        }
    if pred_parquet_path(storage_path) is None:
        return {
            "status": "unavailable",
            "reason": "模型目录没有 pred.parquet（还没推理过）",
        }
    paths = _cache_paths(model_id, window_days)
    status = read_status(model_id, window_days) or {}
    if status.get("status") == "running" and not _lock_is_stale(paths["lock"]):
        return {"status": "running"}
    if not _acquire_lock(paths["lock"]):
        return {"status": "running"}
    _atomic_write_json(
        paths["status"],
        _status_payload("running", progress=1, step="window", message="排队中…"),
    )
    thread = threading.Thread(
        target=_run_job,
        args=(model_id, storage_path),
        kwargs={
            "market": market,
            "horizon": horizon,
            "window_days": window_days,
            "min_adv": min_adv,
            "cost_bps": cost_bps,
            "include_st": include_st,
        },
        daemon=True,
        name=f"signal-eval-{model_id[:16]}",
    )
    thread.start()
    return {"status": "running"}


def _lock_is_stale(lock_path: Path) -> bool:
    try:
        return time.time() - lock_path.stat().st_mtime > LOCK_STALE_SECONDS
    except OSError:
        return True
