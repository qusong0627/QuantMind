"""T-FB-03 日度 IC 序列与组合曲线——机构级报告的数据面。

口径纪律（规划 §2.7 铁律）：本模块数字必须与**挖掘阶段**和**现行 CN 回测**
逐值可比。核心算法与 ``alpha_agent._vectorized_daily_spearman_ic`` 逐字同源
（秩相关 = 去均值内积 / 样本标准差积，ddof=1；icir = mean/(std+1e-8)），
单测对旧实现 1e-12 容差钉死——改一边两侧都红。

成本口径沿 ``factor_report.metrics_eval`` 约定：``net = ret - traded × bps/1e4``，
其中 ``traded`` 是**日双边交易比例** Σ|Δw|（cost_sensitivity 的 turnover 即此
口径）。界面展示的单边换手 = traded / 2，报告时注明。

所有 NaN 出口一律转 None（JSON 无 NaN），前端按「—」渲染，绝不显示成 0。
"""

from __future__ import annotations

import logging
import math

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

#: 与 factor_report 常量一致（年化因子；界面展示口径）。
TRADING_DAYS = 252

#: 头部/尾部组合默认比例（与挖掘/现行回测的 top 30% 一致）。
DEFAULT_TOP_PCT = 0.3

#: 分位桶数（q1 最低 … q5 最高）。
DEFAULT_N_BUCKETS = 5


def _finite_frame(f: pd.Series, r: pd.Series) -> pd.DataFrame:
    """(datetime, instrument) 对齐 + 双列有限值过滤——旧实现同款前置。"""
    df = pd.DataFrame({"f": f.values, "r": r.values})
    df["date"] = f.index.get_level_values("datetime")
    df["inst"] = f.index.get_level_values("instrument")
    df = df[np.isfinite(df["f"]) & np.isfinite(df["r"])]
    return df


def _rank_corr_by_date(df: pd.DataFrame) -> pd.Series:
    """逐日秩相关序列（对已过滤 frame；算法与旧实现逐字同源）。

    corr = Σ(fc·rc) / (n-1) ÷ sqrt(Σfc²/(n-1) · Σrc²/(n-1))，
    fc/rc 为日内在秩去均值分量；denom ≤ 1e-12 的日子（全同值）剔除。
    """
    if df.empty:
        return pd.Series(dtype=float)
    df = df.copy()
    df["f_rank"] = df.groupby("date")["f"].rank(method="average")
    df["r_rank"] = df.groupby("date")["r"].rank(method="average")
    g = df.groupby("date")
    means = g[["f_rank", "r_rank"]].transform("mean")
    df["fc"] = df["f_rank"] - means["f_rank"]
    df["rc"] = df["r_rank"] - means["r_rank"]
    df["fcr"] = df["fc"] * df["rc"]
    df["fc2"] = df["fc"] ** 2
    df["rc2"] = df["rc"] ** 2
    sums = g[["fcr", "fc2", "rc2"]].transform("sum")
    counts = g["fcr"].transform("count")
    n = (counts - 1).clip(lower=1)
    cov = sums["fcr"] / n
    var_f = sums["fc2"] / n
    var_r = sums["rc2"] / n
    denom = np.sqrt(var_f * var_r)
    df["corr"] = np.where(
        denom > 1e-12, cov / np.where(denom > 1e-12, denom, 1.0), np.nan
    )
    out = df.groupby("date")["corr"].first()
    return out[np.isfinite(out)]


def daily_ic_series(f: pd.Series, r: pd.Series) -> pd.Series:
    """逐日秩相关（DatetimeIndex，缺失日已剔除）。"""
    return _rank_corr_by_date(_finite_frame(f, r))


def daily_ic_stats(f: pd.Series, r: pd.Series) -> dict:
    """IC 摘要指标——5 元组与 ``_vectorized_daily_spearman_ic`` 逐值一致。

    Returns: ``ic / rank_ic / icir / rank_icir / n_obs``（旧口径）+ 报表加项
        ``ic_std / n_days / ic_positive_rate / ic_nw_t``。
    """
    from backend.services.engine.factor_report.metrics_eval import nw_tstat

    df = _finite_frame(f, r)
    s = _rank_corr_by_date(df)
    n_obs = int(len(df))
    if s.empty:
        return {
            "ic": 0.0,
            "rank_ic": 0.0,
            "icir": 0.0,
            "rank_icir": 0.0,
            "n_obs": n_obs,
            "ic_std": 0.0,
            "n_days": 0,
            "ic_positive_rate": None,
            "ic_nw_t": None,
        }
    ic_mean = float(s.mean())
    rank_ic_median = float(s.median())
    std = float(s.std(ddof=1)) if len(s) > 1 else 0.0
    return {
        "ic": ic_mean,
        "rank_ic": rank_ic_median,
        "icir": ic_mean / (std + 1e-8),
        "rank_icir": rank_ic_median / (std + 1e-8),
        "n_obs": n_obs,
        "ic_std": std,
        "n_days": int(len(s)),
        "ic_positive_rate": float((s > 0).mean()),
        "ic_nw_t": nw_tstat(s.to_numpy()),
    }


def _traded_from_members(members: dict) -> pd.Series:
    """按日成员集算双边交易比例 Σ|Δw|（等权；首日 0，无前仓可卖）。"""
    dates = sorted(members)
    traded = {}
    prev: set | None = None
    for day in dates:
        cur = members[day]
        if prev is None or not cur or not prev:
            traded[day] = 0.0
        else:
            exited = len(prev - cur) / len(prev)
            entered = len(cur - prev) / len(cur)
            traded[day] = float(exited + entered)
        prev = cur
    return pd.Series(traded).reindex(dates)


def portfolio_curves(
    f: pd.Series,
    r: pd.Series,
    top_pct: float = DEFAULT_TOP_PCT,
    n_buckets: int = DEFAULT_N_BUCKETS,
) -> pd.DataFrame:
    """组合曲线：多头/空头/多空/分位桶 + 日双边交易比例 + 覆盖数。

    索引为交易日；列：``ret_long / ret_short / ret_ls / traded / coverage /
    q1..qN``（qN 最高分位；等权日收益）。头部 = f_rank ≥ 1-top_pct（与旧
    实现 ``f_rank pct >= 0.7`` 同款）。
    """
    df = _finite_frame(f, r)
    if df.empty:
        return pd.DataFrame(
            columns=["ret_long", "ret_short", "ret_ls", "traded", "coverage", "bench"]
            + [f"q{i}" for i in range(1, n_buckets + 1)]
        )
    df = df.copy()
    df["f_rank"] = df.groupby("date")["f"].rank(pct=True, method="average")
    long_mask = df["f_rank"] >= (1.0 - top_pct)
    short_mask = df["f_rank"] <= top_pct

    ret_long = df[long_mask].groupby("date")["r"].mean()
    ret_short = df[short_mask].groupby("date")["r"].mean()
    ret_bench = df.groupby("date")["r"].mean()
    coverage = df.groupby("date")["r"].count()

    # 分位桶：floor(rank_pct × N)，rank_pct=1.0 钳到 N
    bucket = np.minimum((df["f_rank"] * n_buckets).astype(int) + 1, n_buckets)
    df["bucket"] = bucket
    q = df.groupby(["date", "bucket"])["r"].mean().unstack()

    members = {day: set(g["inst"]) for day, g in df[long_mask].groupby("date")}
    traded = _traded_from_members(members)

    out = pd.DataFrame(
        {
            "ret_long": ret_long,
            "ret_short": ret_short,
            "ret_ls": ret_long - ret_short,
            "traded": traded,
            "coverage": coverage,
            "bench": ret_bench,
        }
    )
    for i in range(1, n_buckets + 1):
        out[f"q{i}"] = q[i] if i in q.columns else np.nan
    return out


def _max_drawdown(nav: pd.Series) -> float:
    """最大回撤（负数，如 -0.23）。"""
    if nav.empty:
        return 0.0
    running_max = nav.cummax()
    dd = nav / running_max - 1.0
    return float(dd.min())


def perf_metrics(ret_long: pd.Series, traded: pd.Series, cost_bps: int) -> dict:
    """绩效：毛/净年化、夏普、最大回撤、年化换手（口径见模块 docstring）。

    毛口径公式与现行实现一致（mean×252 / mean/std×√252，std+1e-8），
    保证 CN 数字与因子库现行回测可比。
    """
    ret = ret_long.dropna()
    if ret.empty:
        return {
            "ann_return": None,
            "ann_vol": None,
            "sharpe": None,
            "max_drawdown": None,
            "ann_turnover": None,
            "ann_return_net": None,
            "sharpe_net": None,
        }
    d = ret.to_numpy()
    t = traded.reindex(ret.index).fillna(0.0).to_numpy()
    mean, std = float(d.mean()), float(d.std(ddof=1)) if len(d) > 1 else 0.0
    ann_return = mean * TRADING_DAYS
    ann_vol = std * math.sqrt(TRADING_DAYS)
    sharpe = mean / (std + 1e-8) * math.sqrt(TRADING_DAYS)
    nav = pd.Series(np.cumprod(1.0 + d), index=ret.index)

    net = d - t * cost_bps / 1e4
    net_mean, net_std = (
        float(net.mean()),
        float(net.std(ddof=1)) if len(net) > 1 else 0.0,
    )
    return {
        "ann_return": ann_return,
        "ann_vol": ann_vol,
        "sharpe": sharpe,
        "max_drawdown": _max_drawdown(nav),
        "ann_turnover": float(t.mean() * TRADING_DAYS),
        "ann_return_net": net_mean * TRADING_DAYS,
        "sharpe_net": net_mean / (net_std + 1e-8) * math.sqrt(TRADING_DAYS),
    }


def _json_list(series: pd.Series) -> list:
    """Series → JSON 安全列表（NaN/Inf → None）。"""
    out = []
    for v in series.tolist():
        if v is None or (isinstance(v, float) and not math.isfinite(v)):
            out.append(None)
        else:
            out.append(float(v))
    return out


def build_series_payload(
    curves: pd.DataFrame,
    ic_series: pd.Series,
    cost_bps: int,
    top_pct: float = DEFAULT_TOP_PCT,
) -> dict:
    """台账序列载荷（JSON 安全）——`/runs/{id}/series` 的下钻数据面。

    日期轴 = 组合曲线索引；IC 序列按轴对齐（缺失日 None）。净值曲线一律
    起点 1.0 复利累计；IC 累计为**求和**（IC 不是收益率，无误导性复利）。
    """
    dates = curves.index
    nav_long = (1.0 + curves["ret_long"].fillna(0.0)).cumprod()
    nav_ls = (1.0 + curves["ret_ls"].fillna(0.0)).cumprod()
    nav_bench = (1.0 + curves["bench"].fillna(0.0)).cumprod()
    q_curves = {
        col: _json_list((1.0 + curves[col].fillna(0.0)).cumprod())
        for col in sorted(c for c in curves.columns if c.startswith("q"))
    }
    ic_aligned = ic_series.reindex(dates)
    ic_cum = ic_aligned.fillna(0.0).cumsum()
    return {
        "dates": [str(d.date()) if hasattr(d, "date") else str(d) for d in dates],
        "ic": _json_list(ic_aligned),
        "ic_cum": _json_list(ic_cum),
        "nav_long": _json_list(nav_long),
        "nav_ls": _json_list(nav_ls),
        "nav_bench": _json_list(nav_bench),
        "q_curves": q_curves,
        "turnover": _json_list(curves["traded"]),
        "coverage": [int(v) for v in curves["coverage"].tolist()],
        "bench": "equal_weight",
        "meta": {
            "cost_bps": cost_bps,
            "top_pct": top_pct,
            "n_buckets": len(q_curves),
            "turnover_convention": "daily_two_sided",
        },
    }
