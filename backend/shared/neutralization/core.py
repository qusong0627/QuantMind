"""分数级中性化纯函数（零 IO）。

口径（唯一真源，见 ``docs/推理中性化处理前端_生产级设计_20261030.md``）：

    r_i       = rank_pct(score_i)
    X_i       = [1, rank_pct(winsor(log_mv)), rank_pct(winsor(beta60)),
                 rank_pct(winsor(mom20))]
    β         = lstsq(X, r)
    resid_i   = r_i − X_i·β
    neutral_i = rank_pct(resid_i)

约定：

- 输入分数按**当日截面**传入，函数不感知日期，也不做任何跨日操作；
- 缺失控制变量的标的从回归样本剔除，输出 ``NaN``，由调用方标记；
- 样本不足 / 设计矩阵病态 / 覆盖率不足时返回 ``status='degraded'``，
  但**仍返回可计算的分数**——是否落库、是否可用于下单由服务层按模式决定；
- 任何情况下都不得用原始分数冒充中性化分数。
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, replace
from collections.abc import Mapping, Sequence

import numpy as np
import pandas as pd

#: 默认控制变量（顺序即回归设计矩阵列序）
DEFAULT_CONTROLS: tuple[str, ...] = ("log_mv", "beta60", "mom20")

#: 降级阈值（与设计文档 §7.1 一致）
DEFAULT_MIN_N = 300
DEFAULT_WINSOR_PCT = 0.01
DEFAULT_MIN_COVERAGE = 0.95
DEFAULT_MAX_CONDITION = 1e6
DEFAULT_MAX_EXPOSURE = 0.05

STATUS_OK = "ok"
STATUS_DEGRADED = "degraded"


class NeutralizationInputError(ValueError):
    """输入不合法（列为空、控制变量缺失等）。"""


@dataclass(frozen=True)
class NeutralizationConfig:
    """中性化口径。``config_version`` 由内容派生，调用方不得手填。"""

    controls: tuple[str, ...] = DEFAULT_CONTROLS
    universe_id: str = "cn_all_tradable"
    min_n: int = DEFAULT_MIN_N
    winsor_pct: float = DEFAULT_WINSOR_PCT
    min_coverage: float = DEFAULT_MIN_COVERAGE
    max_condition: float = DEFAULT_MAX_CONDITION
    max_exposure: float = DEFAULT_MAX_EXPOSURE
    control_lag: int = 0

    def canonical(self) -> dict:
        return {
            "controls": list(self.controls),
            "universe_id": self.universe_id,
            "min_n": self.min_n,
            "winsor_pct": self.winsor_pct,
            "min_coverage": self.min_coverage,
            "max_condition": self.max_condition,
            "max_exposure": self.max_exposure,
            "control_lag": self.control_lag,
        }

    @property
    def config_version(self) -> str:
        payload = json.dumps(self.canonical(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def with_controls(self, controls: Sequence[str]) -> NeutralizationConfig:
        return replace(self, controls=tuple(controls))


@dataclass(frozen=True)
class NeutralizationResult:
    """中性化结果。索引为标的（规范前缀式），值为 0~1 的百分位。"""

    neutral_score: pd.Series
    residual: pd.Series
    diagnostics: dict
    status: str
    reason: str | None = None

    @property
    def is_ok(self) -> bool:
        return self.status == STATUS_OK


def _rank_pct(values: pd.Series) -> pd.Series:
    """平均秩百分位（并列取均值秩）。空值原样保留为 NaN。"""
    return values.rank(pct=True, method="average")


def _winsorize(values: pd.Series, pct: float) -> pd.Series:
    if pct <= 0 or values.notna().sum() == 0:
        return values
    lo = values.quantile(pct)
    hi = values.quantile(1.0 - pct)
    if not math.isfinite(lo) or not math.isfinite(hi) or lo >= hi:
        return values
    return values.clip(lower=lo, upper=hi)


def _corr(a: pd.Series, b: pd.Series) -> float | None:
    if len(a) < 2 or a.std(ddof=0) == 0 or b.std(ddof=0) == 0:
        return None
    return float(a.corr(b))


def neutralize_scores(
    scores: pd.Series,
    controls: pd.DataFrame,
    config: NeutralizationConfig | None = None,
) -> NeutralizationResult:
    """对单个交易日截面做中性化。

    Parameters
    ----------
    scores:
        index=标的，values=原始分数（模型输出，任意量纲）。
    controls:
        index=标的，columns 至少包含 ``config.controls`` 中的全部列。
    config:
        口径；缺省用默认控制变量。
    """
    cfg = config or NeutralizationConfig()
    missing_cols = [c for c in cfg.controls if c not in controls.columns]
    if missing_cols:
        raise NeutralizationInputError(f"控制变量缺失: {missing_cols}")
    if scores.index.has_duplicates:
        raise NeutralizationInputError("scores 索引存在重复标的")
    if controls.index.has_duplicates:
        raise NeutralizationInputError("controls 索引存在重复标的")

    scores = pd.to_numeric(scores, errors="coerce")
    aligned = controls.loc[:, list(cfg.controls)].reindex(scores.index)
    aligned = aligned.apply(pd.to_numeric, errors="coerce")

    finite_mask = np.isfinite(scores.to_numpy(dtype=float))
    valid = pd.Series(finite_mask, index=scores.index) & aligned.notna().all(axis=1)
    n_total = int(len(scores))
    n_valid = int(valid.sum())

    empty = pd.Series(np.nan, index=scores.index, dtype=float)
    diagnostics: dict = {
        "n_symbols": n_valid,
        "n_total": n_total,
        "coverage": (n_valid / n_total) if n_total else 0.0,
        "r2": None,
        "betas": None,
        "condition_number": None,
        "exposure": {},
        "config_version": cfg.config_version,
        "controls": list(cfg.controls),
    }

    if n_valid < cfg.min_n:
        diagnostics["status"] = STATUS_DEGRADED
        diagnostics["reason"] = f"样本不足: {n_valid} < {cfg.min_n}"
        return NeutralizationResult(
            neutral_score=empty,
            residual=empty,
            diagnostics=diagnostics,
            status=STATUS_DEGRADED,
            reason=diagnostics["reason"],
        )

    x = aligned.loc[valid]
    y = scores.loc[valid]

    # 控制变量先截尾再取秩：极端市值/beta 不应主导回归
    x_rank = pd.DataFrame(
        {col: _rank_pct(_winsorize(x[col], cfg.winsor_pct)) for col in cfg.controls},
        index=x.index,
    )
    y_rank = _rank_pct(y)

    design = np.column_stack(
        [np.ones(n_valid, dtype=float)]
        + [x_rank[col].to_numpy(dtype=float) for col in cfg.controls]
    )
    target = y_rank.to_numpy(dtype=float)

    try:
        condition = float(np.linalg.cond(design))
    except np.linalg.LinAlgError:
        condition = float("inf")

    diagnostics["condition_number"] = condition
    if not math.isfinite(condition) or condition > cfg.max_condition:
        diagnostics["status"] = STATUS_DEGRADED
        diagnostics["reason"] = f"设计矩阵病态: cond={condition:.3g}"
        return NeutralizationResult(
            neutral_score=empty,
            residual=empty,
            diagnostics=diagnostics,
            status=STATUS_DEGRADED,
            reason=diagnostics["reason"],
        )

    try:
        beta, _, _, _ = np.linalg.lstsq(design, target, rcond=None)
    except np.linalg.LinAlgError as exc:  # pragma: no cover - 条件数闸已兜底
        diagnostics["status"] = STATUS_DEGRADED
        diagnostics["reason"] = f"回归失败: {exc}"
        return NeutralizationResult(
            neutral_score=empty,
            residual=empty,
            diagnostics=diagnostics,
            status=STATUS_DEGRADED,
            reason=diagnostics["reason"],
        )

    # 第一遍：对原始秩回归。OLS 只保证线性正交，秩空间仍有少量非线性残留，
    # 故再做一遍「残差秩 → 控制变量秩」回归（二遍收敛），把秩相关压到 ~0。
    resid1 = target - design.dot(beta)
    resid1_rank = _rank_pct(pd.Series(resid1, index=x.index)).to_numpy(dtype=float)
    beta2, _, _, _ = np.linalg.lstsq(design, resid1_rank, rcond=None)
    residual_arr = resid1_rank - design.dot(beta2)

    residual = pd.Series(residual_arr, index=x.index, dtype=float)
    neutral = _rank_pct(residual)

    variance = float(np.var(target))
    diagnostics["r2_pass1"] = (
        (1.0 - float(np.var(resid1)) / variance) if variance > 0 else None
    )
    diagnostics["r2"] = (
        (1.0 - float(np.var(residual_arr)) / variance) if variance > 0 else None
    )
    diagnostics["betas"] = {
        "intercept": float(beta2[0]),
        **{col: float(beta2[i + 1]) for i, col in enumerate(cfg.controls)},
    }
    diagnostics["exposure"] = {col: _corr(neutral, x_rank[col]) for col in cfg.controls}

    reasons: list[str] = []
    coverage = n_valid / n_total if n_total else 0.0
    if coverage < cfg.min_coverage:
        reasons.append(f"覆盖率不足: {coverage:.3f} < {cfg.min_coverage}")
    worst = max(
        (abs(v) for v in diagnostics["exposure"].values() if v is not None),
        default=0.0,
    )
    if worst > cfg.max_exposure:
        reasons.append(f"事后暴露残留: |ρ|={worst:.3f} > {cfg.max_exposure}")

    out = empty.copy()
    out.loc[neutral.index] = neutral.to_numpy(dtype=float)
    residual_out = empty.copy()
    residual_out.loc[residual.index] = residual.to_numpy(dtype=float)

    if reasons:
        diagnostics["status"] = STATUS_DEGRADED
        diagnostics["reason"] = "; ".join(reasons)
        return NeutralizationResult(
            neutral_score=out,
            residual=residual_out,
            diagnostics=diagnostics,
            status=STATUS_DEGRADED,
            reason=diagnostics["reason"],
        )

    diagnostics["status"] = STATUS_OK
    diagnostics["reason"] = None
    return NeutralizationResult(
        neutral_score=out,
        residual=residual_out,
        diagnostics=diagnostics,
        status=STATUS_OK,
        reason=None,
    )


def build_controls_frame(
    rows: Mapping[str, Mapping[str, float]],
) -> pd.DataFrame:
    """把 ``{symbol: {log_mv, beta60, mom20}}`` 转成 controls DataFrame。

    仅用于测试与调用方适配，生产中由 ``controls.load_controls`` 直接产出 DataFrame。
    """
    frame = pd.DataFrame.from_dict(rows, orient="index")
    frame.index.name = "symbol"
    return frame
