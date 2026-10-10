"""残差正交引擎（T-MV-08）——对池内强因子回归取残差，度量增量预测力。

「残差正交挖掘」：新候选若想进池，理想上与现有强因子解释的空间正交。
本引擎把候选因子对「池内 ICIR 前 K 父本」逐日截面 OLS 回归（z 空间、
含截距）取残差，回答两个问题：

1. **正交了吗**：残差与每个父本的日均秩相关 |ρ| 的最大值是否 < 阈值
   （默认 0.3；池去重线 ``CORR_EDGE_THRESHOLD`` 是 0.9，阈值远小于它）。
2. **残差里还有增量 IC 吗**：残差对前瞻收益（``fret``）的日均秩相关——
   剔除强因子暴露后可归因于本候选的增量预测力；同时给候选原值 IC 做对照。

口径纪律
--------
* 纯函数、只读面板帧（列 ``trade_date/symbol/zscore[/fret]``）；不碰 DB、
  不写盘——调用方是 ``pool_service.refresh_pool``（池级重算纪律：与
  novelty/pool_score 一样在刷新时算；回测钩子里算会让批量回测 O(n²) 卡顿）。
* 全部相关统计复用 ``pool_panels.daily_rank_corr``（池内两两相关同一实现）；
  样本门槛沿 ``factor_quality`` 的 MIN_STOCKS_PER_DAY=30 / MIN_DAYS=20。
* **complete-case**：评分只用候选与全部合格父本都有值的行——多父本缺测
  不剔行会让不同日的回归不同源。
* **诚实降级**：``no_parents`` / ``no_qualified_parents`` /
  ``insufficient_days`` / ``bad_panel`` / ``no_panel`` 一律显式 status；
  无有效相关样本时 ``orthogonal`` 记 None（未知 ≠ 通过）。
* 留痕落 ``rd_agent_factors.metadata_json.orthogonality``
  （``pool_service._write_orthogonality_trace``），验证面见 T-MV-09。
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from . import pool_panels

logger = logging.getLogger(__name__)

#: 父本数上限（ICIR 前 K）：覆盖主流暴露又不过拟合当日截面
DEFAULT_MAX_PARENTS = 5
#: 日截面最少股票数（与 factor_quality 的 MIN_STOCKS_PER_DAY 对齐）
DEFAULT_MIN_STOCKS_PER_DAY = 30
#: 最少有效日（与 factor_quality 的 MIN_DAYS 对齐）
DEFAULT_MIN_SAMPLE_DAYS = 20
#: 残差与父集合最大 |ρ| 的验收阈值：逐日截面内残差与父本现样本正交，
#: 留 0.3 容忍有限样本与秩变换误差
DEFAULT_MAX_PARENT_ABS_CORR = 0.3

_REQUIRED_COLUMNS = ("trade_date", "symbol", "zscore")


def _as_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


@dataclass(frozen=True)
class OrthogonalCriteria:
    """残差正交判据（默认样本门槛与 factor_quality 面板口径对齐）。"""

    max_parents: int = DEFAULT_MAX_PARENTS
    min_stocks_per_day: int = DEFAULT_MIN_STOCKS_PER_DAY
    min_sample_days: int = DEFAULT_MIN_SAMPLE_DAYS
    max_parent_abs_corr: float = DEFAULT_MAX_PARENT_ABS_CORR

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_parents": self.max_parents,
            "min_stocks_per_day": self.min_stocks_per_day,
            "min_sample_days": self.min_sample_days,
        }


@dataclass(frozen=True)
class OrthogonalReport:
    """一次残差正交评估的结果（status 枚举见模块 docstring）。"""

    status: str
    parents: tuple[str, ...] = ()
    parent_corrs: Mapping[str, float] = field(default_factory=dict)
    max_parent_abs_corr: float | None = None
    residual_ic: float | None = None
    candidate_ic: float | None = None
    n_days: int = 0
    n_obs: int = 0
    threshold: float = DEFAULT_MAX_PARENT_ABS_CORR
    criteria: Mapping[str, Any] = field(default_factory=dict)
    note: str | None = None

    @property
    def orthogonal(self) -> bool | None:
        """残差与父集合最大 |ρ| < 阈值；无有效相关样本时为 None（未知≠通过）。"""
        if self.max_parent_abs_corr is None:
            return None
        return self.max_parent_abs_corr < self.threshold

    def to_trace(self) -> dict[str, Any]:
        """可 JSON 序列化的留痕体（``evaluated_at`` 由写侧补）。"""
        return {
            "status": self.status,
            "threshold": self.threshold,
            "orthogonal": self.orthogonal,
            "max_parent_abs_corr": self.max_parent_abs_corr,
            "parents": [
                {
                    "factor_id": pid,
                    "abs_corr": (
                        abs(self.parent_corrs[pid])
                        if pid in self.parent_corrs
                        else None
                    ),
                }
                for pid in self.parents
            ],
            "residual_ic": self.residual_ic,
            "candidate_ic": self.candidate_ic,
            "n_days": self.n_days,
            "n_obs": self.n_obs,
            "criteria": dict(self.criteria),
            "note": self.note,
        }


def select_strong_parents(
    rows: Sequence[tuple[str, float | None]],
    *,
    exclude: str | None = None,
    k: int,
) -> list[str]:
    """ICIR 降序取前 K 父本；ICIR 缺失/非有限不参与，同分按 id 字典序稳定。"""
    entries: list[tuple[str, float]] = []
    for fid, icir in rows:
        fid = str(fid)
        if exclude is not None and fid == exclude:
            continue
        value = _as_float(icir)
        if value is None:
            continue
        entries.append((fid, value))
    entries.sort(key=lambda e: (-e[1], e[0]))
    return [fid for fid, _ in entries[: max(int(k), 0)]]


def _slice_days(frame: pd.DataFrame, days: set[str] | None) -> pd.DataFrame:
    if days is None:
        return frame
    return frame[frame["trade_date"].isin(days)]


def _has_columns(frame: pd.DataFrame, columns: Sequence[str]) -> bool:
    return (
        frame is not None
        and not frame.empty
        and all(c in frame.columns for c in columns)
    )


def _ranked(frame: pd.DataFrame, col: str) -> pd.DataFrame:
    """``trade_date/symbol/rank_pct`` 三列：日内百分位秩（与面板写侧同款）。"""
    out = frame[["trade_date", "symbol", col]].dropna(subset=[col]).copy()
    out["rank_pct"] = out.groupby("trade_date")[col].rank(pct=True)
    return out[["trade_date", "symbol", "rank_pct"]]


def evaluate_orthogonality(
    candidate: pd.DataFrame,
    parents: Mapping[str, pd.DataFrame],
    *,
    days: set[str] | None = None,
    criteria: OrthogonalCriteria | None = None,
) -> OrthogonalReport:
    """候选对父本集合逐日截面 OLS 取残差 → 正交性 + 增量 IC 报告。

    ``parents`` = {factor_id: 面板帧}（帧含 ``trade_date/symbol/zscore``，
    ``fret`` 可选）。父本先过合格性（与候选日重叠 ≥ ``min_stocks_per_day``
    只股票 × ``min_sample_days`` 天），再做 complete-case 逐日回归；任何
    一步样本不足 → 显式 status，绝不拿缺样本算出的数字当结论。
    """
    crit = criteria or OrthogonalCriteria()
    trace_criteria = crit.to_dict()
    if not parents:
        return OrthogonalReport(
            status="no_parents",
            threshold=crit.max_parent_abs_corr,
            criteria=trace_criteria,
        )
    if not _has_columns(candidate, _REQUIRED_COLUMNS):
        return OrthogonalReport(
            status="bad_panel",
            threshold=crit.max_parent_abs_corr,
            criteria=trace_criteria,
        )

    cand = _slice_days(candidate, days)

    # 1) 父本合格性：与候选的重叠日 ≥ min_stocks_per_day × min_sample_days
    qualified: list[tuple[str, pd.DataFrame]] = []
    for pid in sorted(parents):
        parent = _slice_days(parents[pid], days)
        if not _has_columns(parent, _REQUIRED_COLUMNS):
            continue
        overlap = cand[["trade_date", "symbol"]].merge(
            parent[["trade_date", "symbol"]], on=["trade_date", "symbol"], how="inner"
        )
        per_day = overlap.groupby("trade_date").size()
        if int((per_day >= crit.min_stocks_per_day).sum()) >= crit.min_sample_days:
            qualified.append((str(pid), parent))
    if not qualified:
        return OrthogonalReport(
            status="no_qualified_parents",
            threshold=crit.max_parent_abs_corr,
            criteria=trace_criteria,
        )

    # 2) complete-case 矩阵：候选 + 全部合格父本（fret 缺失不剔行——增量 IC
    #    只在有 fret 的日子上算，正交判定不依赖 fret）
    merged = cand
    for i, (_pid, parent) in enumerate(qualified):
        merged = merged.merge(
            parent[["trade_date", "symbol", "zscore"]].rename(
                columns={"zscore": f"parent_{i}"}
            ),
            on=["trade_date", "symbol"],
            how="inner",
        )
    x_cols = [f"parent_{i}" for i in range(len(qualified))]
    analyzed = merged.dropna(subset=["zscore", *x_cols]).rename(
        columns={"zscore": "candidate_z"}
    )

    # 3) 逐日截面 OLS（含截距）→ 残差
    resid_rows: list[pd.DataFrame] = []
    for day, group in analyzed.groupby("trade_date", sort=True):
        if len(group) < crit.min_stocks_per_day:
            continue
        x = np.column_stack([np.ones(len(group)), group[x_cols].to_numpy(dtype=float)])
        y = group["candidate_z"].to_numpy(dtype=float)
        beta, *_ = np.linalg.lstsq(x, y, rcond=None)
        row = pd.DataFrame(
            {
                "trade_date": day,
                "symbol": group["symbol"].to_numpy(),
                "residual": y - x @ beta,
                "candidate_z": y,
            }
        )
        if "fret" in group.columns:
            row["fret"] = group["fret"].to_numpy(dtype=float)
        resid_rows.append(row)

    n_days = len(resid_rows)
    if n_days < crit.min_sample_days:
        return OrthogonalReport(
            status="insufficient_days",
            parents=tuple(pid for pid, _ in qualified),
            threshold=crit.max_parent_abs_corr,
            criteria=trace_criteria,
            n_days=n_days,
        )
    resid = pd.concat(resid_rows, ignore_index=True)

    # 4) 正交性：残差 vs 各父本（与池内两两相关同一实现）
    rank_resid = _ranked(resid, "residual")
    parent_corrs: dict[str, float] = {}
    for pid, parent in qualified:
        res = pool_panels.daily_rank_corr(rank_resid, _ranked(parent, "zscore"))
        if res is not None:
            parent_corrs[pid] = float(res[0])
    max_abs = max((abs(v) for v in parent_corrs.values()), default=None)

    # 5) 增量 IC：残差对 fret 的日均秩相关（候选原值 IC 同批行对照）
    residual_ic: float | None = None
    candidate_ic: float | None = None
    note: str | None = None
    if "fret" in resid.columns and resid["fret"].notna().any():
        fret_rank = _ranked(resid, "fret")
        res = pool_panels.daily_rank_corr(rank_resid, fret_rank)
        if res is not None:
            residual_ic = float(res[0])
            res_cand = pool_panels.daily_rank_corr(_ranked(resid, "candidate_z"), fret_rank)
            if res_cand is not None:
                candidate_ic = float(res_cand[0])
    else:
        note = "fret_missing"

    return OrthogonalReport(
        status="ok",
        parents=tuple(pid for pid, _ in qualified),
        parent_corrs=parent_corrs,
        max_parent_abs_corr=max_abs,
        residual_ic=residual_ic,
        candidate_ic=candidate_ic,
        n_days=n_days,
        n_obs=int(len(resid)),
        threshold=crit.max_parent_abs_corr,
        criteria=trace_criteria,
        note=note,
    )


def build_traces(
    factors: Sequence[Mapping[str, Any]],
    frames: Mapping[str, pd.DataFrame],
    *,
    days: set[str] | None = None,
    criteria: OrthogonalCriteria | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """池级装配（``refresh_pool`` 直接调用）：因子清单 × 面板帧 → 留痕 + 统计。

    父本池 = 有面板因子按 ICIR 降序前 K+1（多取 1，排除候选自身后仍尽量
    给足 K）；每个候选对「池中除自己」的前 K 个父本做正交评估。无面板
    因子记 ``status=no_panel``（验证面 T-MV-09 需要如实可见）。
    """
    crit = criteria or OrthogonalCriteria()
    names = {
        str(f["factor_id"]): str(f.get("factor_name") or f["factor_id"])
        for f in factors
    }
    rows = [(str(f["factor_id"]), _as_float(f.get("icir"))) for f in factors]
    with_panel = [(fid, icir) for fid, icir in rows if fid in frames]
    strong = select_strong_parents(with_panel, k=crit.max_parents + 1)

    traces: dict[str, dict[str, Any]] = {}
    by_status: dict[str, int] = {}
    for fid, _icir in rows:
        if fid not in frames:
            traces[fid] = {"status": "no_panel"}
            by_status["no_panel"] = by_status.get("no_panel", 0) + 1
            continue
        parent_ids = [p for p in strong if p != fid][: crit.max_parents]
        report = evaluate_orthogonality(
            frames[fid],
            {pid: frames[pid] for pid in parent_ids},
            days=days,
            criteria=crit,
        )
        trace = report.to_trace()
        for entry in trace["parents"]:
            entry["name"] = names.get(entry["factor_id"], entry["factor_id"])
        traces[fid] = trace
        by_status[report.status] = by_status.get(report.status, 0) + 1

    evaluated = by_status.get("ok", 0)
    orth_true = sum(1 for t in traces.values() if t.get("orthogonal") is True)
    orth_false = sum(1 for t in traces.values() if t.get("orthogonal") is False)
    stats: dict[str, Any] = {
        "evaluated": evaluated,
        "orthogonal": orth_true,
        "not_orthogonal": orth_false,
        "unknown": evaluated - orth_true - orth_false,
        "no_panel": by_status.get("no_panel", 0),
        "by_status": by_status,
    }
    return traces, stats
