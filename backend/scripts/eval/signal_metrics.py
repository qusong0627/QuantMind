"""模型截面排序指标（纯计算层）：数据装载、中性化、IC / FM / 分位 / 稳定性。

从 ``model_signal_report.py`` 拆出，便于单测与复用；CLI 与报告排版仍在
``model_signal_report.py``。口径说明见该文件与 ``docs/FF5模型归因_使用说明.md``。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parents[3]
for _p in (str(_ROOT), str(_ROOT / "backend")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from backend.scripts.eval.ff_attribution import _load_st_pit, hac_ols  # noqa: E402
from backend.shared.quantdb_paths import resolve_quantdb_subdir  # noqa: E402
from backend.shared.stock_utils import StockCodeUtil  # noqa: E402

log = logging.getLogger("signal_metrics")

DECAY_HORIZONS = (1, 2, 3, 5, 10, 20)
PRED_COLS = (
    "symbol",
    "trade_date",
    "pred",
    "label_return",
    "label_return_h1",
    "label_return_h2",
    "label_return_h3",
    "label_return_h5",
    "label_return_h10",
    "fun_total_mv",
    "amt_log",
    "split",
)


# ---------------------------------------------------------------------------
# 数据装载
# ---------------------------------------------------------------------------
def load_industry_map() -> dict[str, str]:
    """suffix 代码 → 行业名（instrument_detail 的 rs_hyname，静态快照）。"""
    path = (
        resolve_quantdb_subdir("2_base_sector", "instrument_detail")
        / "instrument_detail.parquet"
    )
    if not path.exists():
        log.warning("instrument_detail 缺失，行业中性化退化为仅市值中性化")
        return {}
    df = pd.read_parquet(path, columns=["Symbol", "rs_hyname"])
    df = df.dropna(subset=["rs_hyname"])
    return {
        str(s): str(ind).strip()
        for s, ind in zip(df["Symbol"], df["rs_hyname"], strict=True)
        if str(ind).strip()
    }


def _attach_market_fields(df: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    """补齐 pred 可能缺的市值/成交额：valuation.total_mv（元）与 daily_forward.amount（万元）。

    rank 基础模型（如 label_rank）的 pred 不带特征列，中性化与 ADV 门槛需要外部补齐；
    pred 已有列时只填缺失值，不覆盖。
    """
    import duckdb

    val = resolve_quantdb_subdir("5_technical_derived", "valuation")
    fwd = resolve_quantdb_subdir("1_kline_data", "daily_forward")
    val_glob = str(val / "dt=*" / "*.parquet").replace("\\", "/")
    fwd_glob = str(fwd / "dt=*" / "*.parquet").replace("\\", "/")
    con = duckdb.connect()
    try:
        mv = con.execute(
            f"""
            SELECT strftime(time, '%Y%m%d') AS date_key, symbol, total_mv
            FROM read_parquet('{val_glob}', hive_partitioning=1)
            WHERE time >= DATE '{start}' AND time <= DATE '{end}'
            """
        ).fetchdf()
        amt = con.execute(
            f"""
            SELECT strftime(time, '%Y%m%d') AS date_key, symbol, amount
            FROM read_parquet('{fwd_glob}', hive_partitioning=1)
            WHERE time >= DATE '{start}' AND time <= DATE '{end}'
            """
        ).fetchdf()
    finally:
        con.close()
    mv["_mv"] = np.log(mv["total_mv"].clip(lower=1.0))
    amt["_amt"] = np.log(amt["amount"].clip(lower=1e-6))
    for frame, col, target in ((mv, "_mv", "fun_total_mv"), (amt, "_amt", "amt_log")):
        ext = frame[["date_key", "symbol", col]].rename(
            columns={"symbol": "sym_suffix", col: "_ext"}
        )
        df = df.merge(ext, on=["date_key", "sym_suffix"], how="left")
        if target in df.columns:
            df[target] = df[target].fillna(df["_ext"])
        else:
            df[target] = df["_ext"]
        df = df.drop(columns=["_ext"])
    return df


def load_signal_frame(
    pred_path: Path,
    start: str,
    end: str,
    split: str,
    *,
    min_adv: float,
    exclude_st: bool,
    with_size: bool = True,
) -> pd.DataFrame:
    """pred + 标签；过滤样本期/切分/缺失/ADV/(可选)ST；附 suffix 代码与行业。"""
    import pyarrow.parquet as pq

    avail = pq.ParquetFile(pred_path).schema_arrow.names
    cols = [c for c in PRED_COLS if c in set(avail)]
    df = pd.read_parquet(pred_path, columns=cols)
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    lo, hi = pd.Timestamp(start), pd.Timestamp(end)
    df = df[(df["trade_date"] >= lo) & (df["trade_date"] <= hi)]
    if split != "all":
        df = df[df["split"] == split]
    need = ["pred"] + (["label_return"] if "label_return" in df.columns else [])
    df = df.dropna(subset=[c for c in need if c in df.columns]).copy()
    df["date_key"] = df["trade_date"].dt.strftime("%Y%m%d")
    df["sym_suffix"] = df["symbol"].map(lambda s: StockCodeUtil.to_suffix(str(s)))
    df = _attach_market_fields(df, start, end)
    if min_adv > 0 and "amt_log" in df.columns:
        # amt_log = ln(amount 万元)，换算回元再比门槛
        amount_yuan = np.exp(df["amt_log"].to_numpy(dtype=float)) * 1e4
        df = df[amount_yuan >= float(min_adv)].copy()
    if exclude_st:
        st = _load_st_pit()
        if st:
            mask = [
                (s, d) not in st
                for s, d in zip(df["sym_suffix"], df["date_key"], strict=True)
            ]
            df = df[mask].copy()
    if with_size:
        if "fun_total_mv" in df.columns:
            df["log_size"] = df["fun_total_mv"].astype(float)
        else:
            df["log_size"] = np.nan
    industry = load_industry_map()
    df["industry"] = df["sym_suffix"].map(industry).fillna("UNKNOWN")
    return df.reset_index(drop=True)


def price_forward_returns(
    symbols: list[str], start: str, end: str, horizons: tuple[int, ...]
) -> dict[str, dict[int, pd.Series]]:
    """两种前向收益口径（按个股自己的交易日 shift）：

    - ``close_entry``：T+1 收盘买 → T+1+h 收盘卖（与训练标签同口径）；
    - ``open_entry``：T+1 开盘买 → T+1+h 收盘卖（可执行口径）。
    返回 ``{"close_entry": {h: Series(index=(date_key, symbol))}, "open_entry": {...}}``。
    """
    import duckdb

    fwd = resolve_quantdb_subdir("1_kline_data", "daily_forward")
    glob_pat = str(fwd / "dt=*" / "*.parquet").replace("\\", "/")
    pad_end = (pd.Timestamp(end) + pd.Timedelta(days=max(horizons) * 3 + 20)).strftime(
        "%Y-%m-%d"
    )
    con = duckdb.connect()
    try:
        px = con.execute(
            f"""
            SELECT symbol, strftime(time, '%Y%m%d') AS date_key, open, close
            FROM read_parquet('{glob_pat}', hive_partitioning=1)
            WHERE time >= DATE '{start}' AND time <= DATE '{pad_end}'
            """
        ).fetchdf()
    finally:
        con.close()
    px = px[px["symbol"].isin(set(symbols))]
    px = px.sort_values(["symbol", "date_key"])
    entry = {
        "open_entry": px.groupby("symbol")["open"].shift(-1),
        "close_entry": px.groupby("symbol")["close"].shift(-1),
    }
    index = pd.MultiIndex.from_arrays(
        [px["date_key"].to_numpy(), px["symbol"].to_numpy()],
        names=["date_key", "sym_suffix"],
    )
    out: dict[str, dict[int, pd.Series]] = {"open_entry": {}, "close_entry": {}}
    for h in horizons:
        future = px.groupby("symbol")["close"].shift(-(1 + h))
        for mode, entry_px in entry.items():
            ret = future / entry_px - 1.0
            s = pd.Series(ret.to_numpy(dtype=float), index=index)
            out[mode][h] = s[~s.index.duplicated(keep="first")]
    return out


# ---------------------------------------------------------------------------
# 截面工具
# ---------------------------------------------------------------------------
def _rank_pct(v: np.ndarray) -> np.ndarray:
    return pd.Series(v).rank(method="average", pct=True).to_numpy(dtype=float)


def _zscore(v: np.ndarray) -> np.ndarray:
    v = np.asarray(v, dtype=float)
    sd = np.nanstd(v)
    if not np.isfinite(sd) or sd <= 0:
        return np.zeros_like(v)
    return (v - np.nanmean(v)) / sd


def _industry_dummies(industry: pd.Series) -> np.ndarray:
    codes = industry.astype("category").cat.codes.to_numpy()
    n = len(codes)
    k = int(codes.max()) + 1 if n else 0
    if k <= 1:
        return np.zeros((n, 0))
    dummies = np.zeros((n, k - 1), dtype=float)
    keep = codes > 0
    dummies[np.arange(n)[keep], codes[keep] - 1] = 1.0
    return dummies


def _residualize(values: np.ndarray, controls: np.ndarray) -> np.ndarray:
    """values 对 [1, controls] 做截面 OLS，返回残差。"""
    if controls.shape[1] == 0:
        return values - values.mean()
    X = np.column_stack([np.ones(len(values)), controls])
    ok = np.isfinite(values) & np.all(np.isfinite(X), axis=1)
    resid = np.full(len(values), np.nan)
    if ok.sum() < X.shape[1] + 5:
        return resid
    beta, *_ = np.linalg.lstsq(X[ok], values[ok], rcond=None)
    resid[ok] = values[ok] - X[ok] @ beta
    return resid


def _cross_controls(frame: pd.DataFrame) -> np.ndarray:
    """截面控制变量：log 市值 z 分数 + 行业哑变量。"""
    cols = []
    if "log_size" in frame.columns and np.isfinite(frame["log_size"]).any():
        cols.append(_zscore(frame["log_size"].to_numpy(dtype=float))[:, None])
    ind = _industry_dummies(frame["industry"])
    if ind.shape[1]:
        cols.append(ind)
    return np.column_stack(cols) if cols else np.zeros((len(frame), 0))


def _spearman(a: np.ndarray, b: np.ndarray, min_n: int = 20) -> float:
    ok = np.isfinite(a) & np.isfinite(b)
    if ok.sum() < min_n:
        return np.nan
    return float(pd.Series(a[ok]).corr(pd.Series(b[ok]), method="spearman"))


# ---------------------------------------------------------------------------
# 指标
# ---------------------------------------------------------------------------
def ic_summary(series: pd.Series, lags: int = 4) -> dict:
    s = series.dropna()
    if len(s) < 5:
        return {
            "n": int(len(s)),
            "mean": np.nan,
            "std": np.nan,
            "icir": np.nan,
            "win_rate": np.nan,
            "t": np.nan,
            "hac_t": np.nan,
        }
    mean, std = float(s.mean()), float(s.std(ddof=1))
    t = mean / std * np.sqrt(len(s)) if std > 0 else np.nan
    h = hac_ols(
        s.to_numpy(), np.zeros((len(s), 0)), lags=max(1, min(lags, len(s) // 5))
    )
    return {
        "n": int(len(s)),
        "mean": mean,
        "std": std,
        "icir": mean / std if std > 0 else np.nan,
        "win_rate": float((s > 0).mean()),
        "t": float(t) if np.isfinite(t) else np.nan,
        "hac_t": float(h["t"][0]),
    }


def daily_cross_section_ic(df: pd.DataFrame) -> pd.DataFrame:
    """逐日截面：raw / 中性化信号 / 双重中性化 RankIC（三者强制同一批行）。

    中性化需要市值/行业控制；若某些行控制变量缺失，旧实现会让 raw 与 neutral
    落在不同样本上，两个 IC 不可直接比较。这里先取公共有效行再统一计算。
    """
    rows = []
    for date_key, frame in df.groupby("date_key", sort=True):
        ret = frame["label_return"].to_numpy(dtype=float)
        pred = frame["pred"].to_numpy(dtype=float)
        controls = _cross_controls(frame)
        sig_rank = _rank_pct(pred)
        ret_rank = _rank_pct(ret)
        resid_sig = _residualize(sig_rank, controls)
        resid_ret = _residualize(ret_rank, controls)
        common = np.isfinite(pred) & np.isfinite(ret)
        if controls.shape[1]:
            common &= np.all(np.isfinite(controls), axis=1)
        common &= np.isfinite(resid_sig) & np.isfinite(resid_ret)
        rows.append(
            {
                "date_key": date_key,
                "n": int(common.sum()),
                "ic_raw": _spearman(pred[common], ret[common]),
                "ic_neutral": _spearman(resid_sig[common], ret[common]),
                "ic_double": _spearman(resid_sig[common], resid_ret[common]),
            }
        )
    out = pd.DataFrame(rows).set_index("date_key")
    return out[out["n"] >= 50]


def fama_macbeth(
    df: pd.DataFrame, *, with_controls: bool, min_names: int = 100
) -> dict:
    """月末截面回归：y=label_return，x=标准化 pred（可选加市值/行业控制）。"""
    dates = df.groupby("date_key").size()
    month_end = (
        pd.Series(dates.index, index=pd.to_datetime(dates.index, format="%Y%m%d"))
        .groupby(lambda d: d.strftime("%Y-%m"))
        .max()
        .tolist()
    )
    slopes, tvals = [], []
    for date_key in month_end:
        frame = df[df["date_key"] == date_key]
        if len(frame) < min_names:
            continue
        y = frame["label_return"].to_numpy(dtype=float)
        x = _zscore(_rank_pct(frame["pred"].to_numpy(dtype=float)))
        if with_controls:
            X = np.column_stack([x, _cross_controls(frame)])
        else:
            X = x[:, None]
        try:
            res = hac_ols(y, X, lags=1)
        except ValueError:
            continue
        slopes.append(float(res["beta"][1]))
        tvals.append(float(res["t"][1]))
    if len(slopes) < 5:
        return {
            "n": len(slopes),
            "mean_slope": np.nan,
            "t": np.nan,
            "frac_abs_t_gt2": np.nan,
            "frac_positive": np.nan,
        }
    arr = np.asarray(slopes)
    t_series = np.asarray(tvals)
    sd = arr.std(ddof=1)
    return {
        "n": int(len(arr)),
        "mean_slope": float(arr.mean()),
        "t": float(arr.mean() / sd * np.sqrt(len(arr))) if sd > 0 else np.nan,
        "frac_abs_t_gt2": float((np.abs(t_series) > 2).mean()),
        "frac_positive": float((arr > 0).mean()),
    }


def ic_decay(df: pd.DataFrame, horizon_returns: dict[int, pd.Series]) -> pd.DataFrame:
    """各持有期的平均 RankIC：优先用 pred 自带标签列，缺失时用行情现算（收盘买口径）。"""
    rows = []
    for h in DECAY_HORIZONS:
        col = f"label_return_h{h}"
        if col in df.columns:
            frame = df.assign(_ret=df[col])
        elif h in horizon_returns:
            frame = df.merge(
                horizon_returns[h].rename("_ret"),
                left_on=["date_key", "sym_suffix"],
                right_index=True,
                how="left",
            )
        else:
            continue
        ics = [
            _spearman(g["pred"].to_numpy(dtype=float), g["_ret"].to_numpy(dtype=float))
            for _, g in frame.dropna(subset=["_ret"]).groupby("date_key")
        ]
        s = pd.Series(ics).dropna()
        rows.append(
            {
                "horizon": h,
                "n_days": int(len(s)),
                "mean_ic": float(s.mean()) if len(s) else np.nan,
                "t": float(s.mean() / s.std(ddof=1) * np.sqrt(len(s)))
                if len(s) > 2 and s.std(ddof=1) > 0
                else np.nan,
            }
        )
    return pd.DataFrame(rows)


def quantile_report(df: pd.DataFrame, n_q: int = 10) -> dict:
    """按 pred 截面分位分组，统计各组平均前向收益与 Q高−Q低 价差。"""
    frames = []
    for _, g in df.groupby("date_key", sort=True):
        if len(g) < n_q * 10:
            continue
        rank = g["pred"].rank(method="first")
        bucket = pd.qcut(rank, n_q, labels=False)
        frames.append(g.assign(bucket=bucket))
    if not frames:
        return {
            "buckets": [],
            "monotonicity": np.nan,
            "spread_mean": np.nan,
            "spread_t": np.nan,
            "spread_pos_rate": np.nan,
        }
    allq = pd.concat(frames, ignore_index=True)
    means = allq.groupby("bucket")["label_return"].mean().to_dict()
    daily = (
        allq.groupby(["date_key", "bucket"])["label_return"]
        .mean()
        .unstack("bucket")
        .dropna()
    )
    spread = daily[n_q - 1] - daily[0]
    t = (
        float(spread.mean() / spread.std(ddof=1) * np.sqrt(len(spread)))
        if len(spread) > 2 and spread.std(ddof=1) > 0
        else np.nan
    )
    mono = _spearman(
        np.arange(n_q, dtype=float),
        np.array([means.get(i, np.nan) for i in range(n_q)]),
        min_n=5,
    )
    return {
        "buckets": [means.get(i, np.nan) for i in range(n_q)],
        "monotonicity": mono,
        "spread_mean": float(spread.mean()),
        "spread_t": t,
        "spread_pos_rate": float((spread > 0).mean()),
    }


def stability_report(df: pd.DataFrame, horizon: int, top_ns: list[int]) -> dict:
    """相邻日 pred 秩相关 + 每 H 日调仓的 TopN 重合率。"""
    by_date = {
        d: g.set_index("sym_suffix")["pred"]
        for d, g in df.groupby("date_key", sort=True)
    }
    dates = sorted(by_date)
    autocorr, overlap = [], {n: [] for n in top_ns}
    for i in range(1, len(dates)):
        prev, cur = by_date[dates[i - 1]], by_date[dates[i]]
        common = prev.index.intersection(cur.index)
        if len(common) >= 100:
            autocorr.append(
                float(prev.loc[common].corr(cur.loc[common], method="spearman"))
            )
    for i in range(horizon, len(dates), horizon):
        prev, cur = by_date[dates[i - horizon]], by_date[dates[i]]
        for n in top_ns:
            a = set(prev.nlargest(n).index)
            b = set(cur.nlargest(n).index)
            if a:
                overlap[n].append(len(a & b) / n)
    return {
        "rank_autocorr_daily": float(np.nanmean(autocorr)) if autocorr else np.nan,
        "rank_autocorr_n": len(autocorr),
        "top_overlap": {
            n: float(np.mean(v)) if v else np.nan for n, v in overlap.items()
        },
    }
