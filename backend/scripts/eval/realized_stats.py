"""实证统计工具箱（T-P4-05b-2）：**纯函数，无 IO、无评分口径**。

模型四维（分层/稳健/滚动/换手）真正依赖的横截面统计都在这里：pred 帧归一、
逐日 RankIC、日等权分层阶梯、子样本分段 IC、滚动窗口、Top-k 换手。

设计纪律（踩过的坑）：
- **不许把缺标签当 0**：``normalize_pred_frame`` 缺标签列直接报错——把「没标签」
  当 0 会让「无预测力」伪装成「收益为 0」；
- **一律日等权**：先按日聚合再对天平均，绝不按行合并（大样本日会淹没小样本日，
  实测能把多空方向算反）；
- **窗口顺序 = 调用方顺序**：不按键字典序排序，``str(i)`` 这类键排出来是
  「1,10,11,…,2」，窗口取尾会取到别的日子上去；
- **不足就说不足**：空样本 / 单票 / 全 NaN 一律 ``sufficient=False`` + ``reason``。

评分口径在 ``model_realized.py``（设计 §2.2 红线），本模块不认识分数。
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

TRADING_DAYS = 252
PRED_EVAL_COLUMNS = (
    "symbol",
    "trade_date",
    "label",
    "label_return",
    "pred",
    "split",
)
PRED_TEST_SPLIT = "test"
LABEL_COLUMNS = ("label", "label_return")

MIN_DECILE_DAYS = 3  # 分层：至少 3 个交易日（再少连分组都不稳）
MIN_DECILE_STOCKS = 5  # 分层：单日至少 5 只（否则一刀切不出档）
MIN_SEGMENT_DAYS = 20  # 稳健性：子样本短于此天数不参与「崩坏」判定
MIN_IC_DAYS = 20  # 滚动健康：不足 20 个交易日不给 20 日口径


# ── pred 帧归一 ────────────────────────────────────────────────────────


def normalize_pred_frame(
    frame: pd.DataFrame, *, split: str | None = None
) -> pd.DataFrame:
    """统一 pred schema → ``{symbol, trade_date, label, pred, split, date_key}``。

    两代 schema：``label``（当期）与 ``label_return``（早期）。缺标签列或预测列
    直接报错。入参不被修改（返回新副本）。
    """
    out = frame.copy()
    label_col = next((c for c in LABEL_COLUMNS if c in out.columns), None)
    if label_col is None:
        raise ValueError(
            "pred 缺标签列（label / label_return 都没有）：无法评估预测力，拒绝按 0 计算"
        )
    if "pred" not in out.columns:
        raise ValueError("pred 缺预测列（pred）：无法评估")
    if "trade_date" not in out.columns:
        raise ValueError("pred 缺日期列（trade_date）：无法按日聚合")

    out["label"] = pd.to_numeric(out[label_col], errors="coerce").astype(float)
    out["pred"] = pd.to_numeric(out["pred"], errors="coerce").astype(float)
    out["trade_date"] = pd.to_datetime(out["trade_date"])
    out["date_key"] = out["trade_date"].dt.strftime("%Y-%m-%d")

    if split is not None and "split" in out.columns:
        out = out[out["split"].astype(str) == str(split)]
    return out


# ── 基础工具 ───────────────────────────────────────────────────────────


def _clean_mask(pred: np.ndarray, label: np.ndarray, dates: np.ndarray) -> np.ndarray:
    """有效行掩码：pred/label 任一为 NaN、或日期缺失的行不成对，剔除而非填 0。"""
    p = np.asarray(pred, dtype=float).ravel()
    y = np.asarray(label, dtype=float).ravel()
    return np.isfinite(p) & np.isfinite(y) & ~pd.isna(np.asarray(dates).ravel())


def _dated_arrays(
    pred: np.ndarray, label: np.ndarray, dates: np.ndarray
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    keep = _clean_mask(pred, label, dates)
    return (
        np.asarray(pred, dtype=float).ravel()[keep],
        np.asarray(label, dtype=float).ravel()[keep],
        np.asarray(dates).ravel()[keep],
    )


def spearman_rank_corr(a: np.ndarray, b: np.ndarray) -> float | None:
    """Spearman 秩相关（秩上的 Pearson，不引 scipy）。

    任一序列在全截面取常数 → **返回 None**（相关无定义）。这里**不返回 0.0**：
    常数预测意味着模型没给出任何截面区分度，「无排序」不是「零相关」，
    编一个 0 会让坏模型看起来「评过了」。
    """
    if a.size < 2:
        return None
    ra = pd.Series(a).rank().to_numpy()
    rb = pd.Series(b).rank().to_numpy()
    if ra.std() == 0 or rb.std() == 0:
        return None
    return float(np.corrcoef(ra, rb)[0, 1])


def _day_status(pred: np.ndarray, label: np.ndarray) -> str:
    """单日可用性：ok / thin（不足 2 只）/ degenerate（pred 或 label 常数）。"""
    if pred.size < 2:
        return "thin"
    if pd.Series(pred).nunique() <= 1 or pd.Series(label).nunique() <= 1:
        return "degenerate"
    return "ok"


def daily_ic_coverage(
    pred: np.ndarray, label: np.ndarray, dates: np.ndarray
) -> dict[str, Any]:
    """逐日 IC 覆盖度：有效 / 秩退化 / 单票。写进证据里，别让缺口无声消失。"""
    p, y, d = _dated_arrays(pred, label, dates)
    counts = {"ok": 0, "thin": 0, "degenerate": 0}
    degenerate_days: list[str] = []
    for day in sorted(pd.unique(d)):
        mask = d == day
        status = _day_status(p[mask], y[mask])
        counts[status] += 1
        if status == "degenerate":
            degenerate_days.append(str(day))
    return {
        "n_days_total": int(sum(counts.values())),
        "n_days_ok": counts["ok"],
        "n_days_thin": counts["thin"],
        "n_days_degenerate": counts["degenerate"],
        "degenerate_days": degenerate_days[:10],
    }


def daily_ic(
    pred: np.ndarray, label: np.ndarray, dates: np.ndarray
) -> dict[str, float]:
    """逐日截面 RankIC，**键按日期升序**（下游窗口口径依赖这个顺序）。

    秩退化日（pred 或 label 取常数）与单票日**不产 IC、不填 0**；
    缺口量见 ``daily_ic_coverage``。
    """
    p, y, d = _dated_arrays(pred, label, dates)
    out: dict[str, float] = {}
    for day in sorted(pd.unique(d)):
        mask = d == day
        if _day_status(p[mask], y[mask]) != "ok":
            continue
        rho = spearman_rank_corr(p[mask], y[mask])
        if rho is not None:
            out[str(day)] = round(rho, 6)
    return out


# ── 分层（日等权） ─────────────────────────────────────────────────────


def _empty_strat(
    reason: str, n_days: int = 0, n_degenerate: int = 0, n_thin: int = 0
) -> dict[str, Any]:
    return {
        "sufficient": False,
        "reason": reason,
        "mean_returns": None,
        "monotonicity": None,
        "strict_monotonic": False,
        "ls_mean": None,
        "ls_ir": None,
        "ls_by_day": [],
        "n_days": n_days,
        "n_rows": 0,
        "n_groups": 0,
        "median_stocks_per_day": 0,
        "n_days_degenerate": n_degenerate,
        "n_days_thin": n_thin,
    }


def _day_group_means(
    dp: np.ndarray, dy: np.ndarray, n_groups: int
) -> np.ndarray | None:
    """单日各档等收益（不足以分档 / 预测常数 / 分档后有空档 → None）。

    pred 全天取常数时**必须返回 None**：此时 ``rank`` 只是行序，分出来的「档」
    是行序伪影，据此算出的阶梯与单调性是编出来的证据。
    """
    if dp.size < MIN_DECILE_STOCKS or pd.Series(dp).nunique() <= 1:
        return None
    g = min(int(n_groups), dp.size)
    # rank(method='first') 先打散并列值，避免 qcut 在重复值上抛 duplicate edges
    bucket = pd.qcut(
        pd.Series(dp).rank(method="first"), q=g, labels=False, duplicates="drop"
    ).to_numpy()
    means = np.array([dy[bucket == i].mean() for i in range(g)], dtype=float)
    return None if np.isnan(means).any() else means


def _daily_ladders(
    pred: np.ndarray, label: np.ndarray, dates: np.ndarray, n_groups: int
) -> tuple[list[np.ndarray], list[float], list[int], int, int]:
    """逐日的档均值阶梯、多空收益、当日票数、秩退化日数、票数不足日数。

    先过票数闸门再判退化：单票日的 pred 天然是常数，但它是**票数不足**，
    不是「模型没给排序」——两者混计会把缺失说成模型的错。
    """
    ladders: list[np.ndarray] = []
    ls: list[float] = []
    sizes: list[int] = []
    degenerate = thin = 0
    for day in pd.unique(dates):
        mask = dates == day
        if int(mask.sum()) < MIN_DECILE_STOCKS:
            thin += 1
            continue
        means = _day_group_means(pred[mask], label[mask], n_groups)
        if means is None:  # 票数已够 → 只可能是 pred 截面常数
            degenerate += 1
            continue
        ladders.append(means)
        ls.append(float(means[-1] - means[0]))
        sizes.append(int(mask.sum()))
    return ladders, ls, sizes, degenerate, thin


def _too_few_days(
    ladders: list[np.ndarray], degenerate: int, thin: int, n_total: int
) -> dict[str, Any]:
    """有效日不足 → 如实缺省；把「缺的是常数日还是票数日」写进 reason。"""
    if degenerate > 0 and degenerate + thin >= n_total:
        return _empty_strat(
            f"全部 {n_total} 个交易日的 pred 均为常数（模型无截面区分度）——"
            "排序与分档都无定义，拒绝编造阶梯",
            n_degenerate=degenerate,
            n_thin=thin,
        )
    return _empty_strat(
        f"样本天数不足（有效 {len(ladders)} 天 < {MIN_DECILE_DAYS} 天；"
        f"另跳过 {degenerate} 个预测常数日、{thin} 个票数不足日）",
        n_days=len(ladders),
        n_degenerate=degenerate,
        n_thin=thin,
    )


def decile_stats(
    pred: np.ndarray,
    label: np.ndarray,
    dates: np.ndarray,
    *,
    n_groups: int = 10,
) -> dict[str, Any]:
    """分层统计：各档**日均**收益 + 单调性 + 多空均值/IR（日等权，见模块 docstring）。

    预测常数日被跳过并计数（``n_days_degenerate``）；票数不足日另计
    （``n_days_thin``）；有效日不足时如实缺省并说明是哪一类缺。
    """
    p, y, d = _dated_arrays(pred, label, dates)
    if p.size == 0:
        return _empty_strat("样本为空（无有效 pred 与 label 配对）")

    ladders, ls, sizes, degenerate, thin = _daily_ladders(p, y, d, n_groups)
    if len(ladders) < MIN_DECILE_DAYS:
        return _too_few_days(ladders, degenerate, thin, int(pd.unique(d).size))

    means = [round(float(v), 6) for v in np.vstack(ladders).mean(axis=0)]
    ls_arr = np.asarray(ls, dtype=float)
    ls_std = float(ls_arr.std(ddof=1)) if ls_arr.size > 1 else 0.0
    mono = spearman_rank_corr(
        np.arange(1, len(means) + 1, dtype=float), np.asarray(means)
    )
    if mono is None:
        # 各档均值完全相同：档间无高低可分，单调性无定义——不编 0.0
        return _empty_strat(
            f"各档日均收益完全相同（{len(means)} 档无差）：单调性无定义，分层能力如实缺省",
            n_days=len(ladders),
            n_degenerate=degenerate,
            n_thin=thin,
        )
    return {
        "sufficient": True,
        "reason": None,
        "mean_returns": means,
        "monotonicity": round(mono, 6),
        "strict_monotonic": all(b > a for a, b in zip(means, means[1:], strict=False)),
        "ls_mean": round(float(ls_arr.mean()), 6),
        "ls_ir": round(float(ls_arr.mean() / ls_std), 6) if ls_std > 0 else 0.0,
        "ls_by_day": [round(v, 6) for v in ls],
        "n_days": len(ladders),
        "n_rows": int(p.size),
        "n_groups": len(means),
        "median_stocks_per_day": int(np.median(sizes)),
        "n_days_degenerate": degenerate,
        "n_days_thin": thin,
    }


# ── 子样本分段 ─────────────────────────────────────────────────────────


def _segment_means(
    pred: np.ndarray, label: np.ndarray, dates: np.ndarray, seg: np.ndarray
) -> dict[str, dict[str, Any]]:
    """每段的日均 IC 与天数（段内无有效 IC → ic_mean=None，照样列出）。"""
    out: dict[str, dict[str, Any]] = {}
    for name in pd.unique(seg):
        mask = seg == name
        ics = daily_ic(pred[mask], label[mask], dates[mask])
        out[str(name)] = {
            "ic_mean": round(float(np.mean(list(ics.values()))), 6) if ics else None,
            "n_days": len(ics),
        }
    return out


def _insufficient_segment(reason: str, by_split: dict[str, Any], **extra: Any) -> dict:
    return {
        "sufficient": False,
        "reason": reason,
        "by_split": by_split,
        "min_segment": None,
        "broken_segments": [],
        **extra,
    }


def segment_ic(
    pred: np.ndarray,
    label: np.ndarray,
    dates: np.ndarray,
    segments: np.ndarray,
    *,
    min_segment_days: int = MIN_SEGMENT_DAYS,
) -> dict[str, Any]:
    """分段 IC：每段日均 IC + 最小段 + 崩坏段（IC ≤ 0）。

    短于 ``min_segment_days`` 的段**照报但不算崩坏**——段太短时「崩坏」是噪声
    不是证据。崩坏段一旦出现即触发稳健性红线（设计 §2.2）。
    """
    raw_seg = np.asarray(segments).ravel()
    p, y, d = _dated_arrays(pred, label, dates)
    if p.size == 0 or raw_seg.size != np.asarray(pred).ravel().size:
        return _insufficient_segment("样本为空或分段长度不匹配", {})

    by_split = _segment_means(p, y, d, raw_seg[_clean_mask(pred, label, dates)])
    scored = {k: v for k, v in by_split.items() if v["ic_mean"] is not None}
    if not scored:
        return _insufficient_segment("各段均无有效 IC（单日样本不足 2 只）", by_split)

    days = int(min_segment_days)
    qualified = {k: v for k, v in scored.items() if v["n_days"] >= days}
    if not qualified:
        longest = max(v["n_days"] for v in scored.values())
        return _insufficient_segment(
            f"无段达到最小天数 {days}（最长 {longest} 天）",
            by_split,
            min_segment_days=days,
        )

    values = [float(v["ic_mean"]) for v in qualified.values()]
    min_name = min(qualified, key=lambda k: qualified[k]["ic_mean"])
    return {
        "sufficient": True,
        "reason": None,
        "by_split": by_split,
        "min_segment": qualified[min_name]["ic_mean"],
        "min_segment_name": min_name,
        "segments_used": sorted(qualified),
        "broken_segments": sorted(k for k, v in qualified.items() if v["ic_mean"] <= 0),
        "spread": round(max(values) - min(values), 6),
        "min_segment_days": days,
    }


# ── 滚动窗口 ───────────────────────────────────────────────────────────


def rolling_health(
    ic_by_date: dict[str, float], *, short: int = 20, long: int = 60
) -> dict[str, Any]:
    """近 ``short`` / ``long`` 日 IC 均值、ICIR、漂移（近 − 长）。

    天数不足 ``short`` 时如实缺省——用 3 天的 IC 谈「滚动健康」是自欺。
    序列顺序 = 入参字典顺序（见模块 docstring：不按键排序）。
    """
    items = [(k, float(v)) for k, v in ic_by_date.items() if v is not None]
    n = len(items)
    if n < int(short):
        return {
            "sufficient": False,
            "reason": f"IC 序列天数不足（{n} < {short}）",
            "ic_mean_20": None,
            "ic_mean_60": None,
            "ic_mean": None,
            "icir": None,
            "drift": None,
            "ic_last": None,
            "n_days": n,
            "short": int(short),
            "long": int(long),
        }
    vals = np.asarray([v for _, v in items], dtype=float)
    near = vals[-int(short) :]
    far = vals[-int(long) :]
    std = float(vals.std(ddof=1)) if vals.size > 1 else 0.0
    return {
        "sufficient": True,
        "reason": None,
        "ic_mean_20": round(float(near.mean()), 6),
        "ic_mean_60": round(float(far.mean()), 6),
        "ic_mean": round(float(vals.mean()), 6),
        "icir": float(vals.mean() / std) if std > 0 else 0.0,
        "drift": round(float(near.mean() - far.mean()), 6),
        "ic_last": round(float(vals[-1]), 6),
        "n_days": n,
        "short": int(short),
        "long": int(long),
    }


# ── Top-k 换手与成本拖累 ───────────────────────────────────────────────


def _turnover_series(rank_by_date: dict[str, list[str]], k: int) -> list[float]:
    """逐对相邻交易日的 Top-k 替换率 ``1 − |topk_t ∩ topk_{t−1}| / k``。"""
    dates = list(rank_by_date)
    series: list[float] = []
    for prev, cur in zip(dates, dates[1:], strict=False):
        a = set(rank_by_date[prev][:k])
        b = set(rank_by_date[cur][:k])
        if not a and not b:
            continue
        series.append(round(1.0 - len(a & b) / float(k), 6))
    return series


def topk_turnover(
    rank_by_date: dict[str, list[str]],
    *,
    k: int,
    round_trip_cost: float,
    trading_days: int = TRADING_DAYS,
    gross_annual: float | None = None,
) -> dict[str, Any]:
    """Top-k 组合换手与成本拖累。

    成本拖累 = 日均换手 × 双边成本 × 年交易日；``gross_annual`` 给了就一并算
    成本后年化（红线「成本后 ≤ 0」用）。
    """
    kk = max(1, int(k))
    series = _turnover_series(rank_by_date, kk)
    if len(rank_by_date) < 2 or not series:
        return {
            "sufficient": False,
            "reason": "不足两个有效交易日，无法度量换手",
            "turnover_mean": None,
            "turnover_series": series,
            "n_pairs": len(series),
            "n_days": len(rank_by_date),
            "top_k": kk,
        }
    turnover_mean = float(np.mean(series))
    drag = turnover_mean * float(round_trip_cost) * int(trading_days)
    out: dict[str, Any] = {
        "sufficient": True,
        "reason": None,
        "turnover_mean": round(turnover_mean, 6),
        "turnover_series": series,
        "n_pairs": len(series),
        "n_days": len(rank_by_date),
        "top_k": kk,
        "round_trip_cost": float(round_trip_cost),
        "cost_drag_annual": drag,
        "trading_days": int(trading_days),
    }
    out["gross_annual"] = None if gross_annual is None else float(gross_annual)
    out["net_annual"] = None if gross_annual is None else float(gross_annual) - drag
    return out


def topk_by_date(
    frame: pd.DataFrame, *, k: int, pred_col: str = "pred", symbol_col: str = "symbol"
) -> dict[str, list[str]]:
    """每日按 pred 降序取 Top-k 代码（A 股口径：分高者在前）。"""
    ranked: dict[str, list[str]] = {}
    for day, chunk in frame.groupby("date_key", sort=True):
        ordered = chunk.sort_values(pred_col, ascending=False)
        ranked[str(day)] = [str(s) for s in ordered[symbol_col].head(int(k)).tolist()]
    return ranked


def drop_degenerate_days(frame: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, Any]]:
    """剔除秩退化日（pred 全天取常数）→ (可用帧, 覆盖度证据)。

    常数预测没有排序可言，依据它算出的分层、分段、Top-k 换手全是行序伪影；
    实测 12 个真实 pred.parquet 里就有 1 个模型 120/120 天全常数。
    覆盖度随帧返回，让「跳过了多少」进证据而不是无声消失。
    """
    nunique = frame.groupby("date_key")["pred"].nunique()
    degenerate = sorted(str(d) for d in nunique[nunique <= 1].index)
    kept = frame[~frame["date_key"].astype(str).isin(set(degenerate))]
    coverage = {
        "n_days_total": int(nunique.shape[0]),
        "n_days_degenerate": len(degenerate),
        "degenerate_days": degenerate[:10],
        "n_days_used": int(kept["date_key"].nunique()),
        "n_rows_used": int(kept.shape[0]),
    }
    return kept, coverage
