"""T-FB-16 机构报告标量块：台账行 + 序列载荷 → 报告数字（纯函数、无 IO）。

数据面只用**已落盘的两样东西**：

- 台账行 ``run``（``metrics`` 里带评估期算好的 ``ic_nw_t`` 等标量、``benchmark``
  参考指数名、``batch_id`` 批次归属）；
- 序列载荷 ``series``（``/runs/{id}/series`` 同款：dates/ic/nav_long/nav_ls/
  nav_bench/q_curves/turnover/coverage/bench/meta）。

口径纪律：

- 所有统计**一律调用 ``factor_report.metrics`` 的现有纯函数**，本模块不重写
  任何公式——重写必漂移。金样测试逐值锁定「装配值 == 同源函数直调值」。
- 日度多空收益由 ``nav_ls`` 无损反推（``nav = cumprod(1+r)``，首日 ``r0 = nav0-1``，
  缺失日 fillna(0) 在落盘侧已完成）；turnover 直接用载荷里的**日双边**序列
  （口径见 ``ic.py`` 模块 docstring：``net = ret - traded × bps/1e4``）。
- **降级/未完成终态不出任何数字**（``available=False`` + ``reason`` 原文），
  绝不拿 0/空值冒充。
- 从序列载荷**算不出来**的能力进 ``unavailable`` 清单并写明缺什么输入
  （容量缺成交额、半衰期缺多视界 IC、风格归因缺风格收益、多期持有缺多期
  多空序列）——诚实降级优先于造数。

显著性口径：

- ``nw_t`` 与台账 ``metrics.ic_nw_t`` 同源同值（评估期由同一 ``nw_tstat`` 算出，
  金样断言二者逐位相等）；矩阵显著性列（T-FB-18）经
  :func:`significance_summary` 直接读台账存值，与报告块的族校正单源
  （:func:`q_value_from_family`）；
- ``q_value_bhy`` 的「族」= **同批次全部完成单元**（用户一次派发即一族假设，
  批内单元格共享同一数据面与同一参考系）；无批次上下文时 ``q = p`` 且在
  ``family_note`` 明说未做多重校正；
- ``dsr`` 的 ``n_trials`` 默认 = 批内完成单元数（查询参数可覆盖），来源记入
  ``n_trials_source``——挖掘史选型的真实试错数不可考，这是可计算的**下界**，
  必须在 ``dsr_note`` 里说明，不得默认读者把它当全史试次数。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import numpy as np

from backend.services.engine.factor_report import metrics as M

#: 序列载荷算不出的报告块（各写明缺的具体输入；前端按「暂缺」陈列）。
_UNAVAILABLE_BLOCKS: tuple[dict[str, str], ...] = (
    {
        "block": "capacity",
        "reason": "序列载荷不含成交额与持仓市值，容量模型缺输入",
    },
    {
        "block": "holding_period",
        "reason": "运行只存 1 日持有期口径，无 2/5/10/20 日多空序列",
    },
    {
        "block": "ic_half_life",
        "reason": "运行只存单视界 IC，无 1/2/5/10/20 日多视界 IC 衰减表",
    },
    {
        "block": "style_attribution",
        "reason": "未接入风格因子收益序列（需市值/价值等因子日收益）",
    },
)

#: 族校正注文（报告块与矩阵显著性列共用同一措辞，防两侧漂移）。
_FAMILY_NOTE_CORRECTED = "族 = 同一批次全部完成单元（NW t → 正态双侧 p → BY 校正）"
_FAMILY_NOTE_UNCORRECTED = "无批次族上下文：q = p（n=1，未做多重校正）"


def q_value_from_family(family_nw_t: Sequence[float], self_index: int) -> float:
    """族校正 q 值：族 NW t 列表（含自身，调用方对齐顺序）→ 正态双侧 p →
    BY 校正后取自身位。报告块与矩阵显著性列（T-FB-18）走这一条实现。

    调用方契约：``family_nw_t`` 各元素均非 None（族成员已按 ``ic_nw_t``
    存在性过滤）、``0 <= self_index < len(family_nw_t)``。
    """
    p_list = [M.normal_pvalue(float(t)) for t in family_nw_t]
    return float(M.bhy_qvalues(p_list)[int(self_index)])


def significance_summary(
    nw_t: float | None,
    family_nw_t: Sequence[float] | None = None,
    self_index: int = 0,
) -> dict[str, Any] | None:
    """显著性摘要（矩阵格用）：NW t → p → BY q（族校正可选）。

    - ``nw_t`` 取台账 ``metrics.ic_nw_t``（与报告块 ``nw_t`` 同源同值）；缺失
      → None——矩阵格显示「—」，绝不造数。
    - 族契约同 :func:`q_value_from_family`；无族/自身不在族 → ``q = p``、
      ``family_n = 1``、``family_note`` 明说未校正（与报告块同措辞单源）。
    """
    if nw_t is None:
        return None
    nw_t = float(nw_t)
    p_value = M.normal_pvalue(nw_t)
    q_value = p_value
    family_n = 1
    family_note = _FAMILY_NOTE_UNCORRECTED
    if family_nw_t and 0 <= int(self_index) < len(family_nw_t):
        family_n = len(family_nw_t)
        if p_value is not None:
            q_value = q_value_from_family(family_nw_t, int(self_index))
        family_note = _FAMILY_NOTE_CORRECTED
    return {
        "nw_t": nw_t,
        "p_value": p_value,
        "q_value_bhy": q_value,
        "family_n": family_n,
        "family_note": family_note,
    }


def _nav_daily(nav: Sequence[float | None] | None) -> np.ndarray:
    """净值序列 → 日度收益（``nav = cumprod(1+r)`` 的无损反推）。

    首日以 1.0 为前值（``r0 = nav0 - 1``）；None/NaN 原样传播（下游纯函数
    统一 ``_finite`` 过滤，绝不在这里静默补 0 之外的值——fillna(0) 的语义
    在落盘侧已定）。
    """
    n = np.asarray(nav or [], dtype=np.float64).ravel()
    if n.size == 0:
        return np.empty(0, dtype=np.float64)
    prev = np.concatenate((np.ones(1, dtype=np.float64), n[:-1]))
    with np.errstate(divide="ignore", invalid="ignore"):
        return n / prev - 1.0


def _skew_kurt_of(x: np.ndarray) -> tuple[float, float]:
    """中心矩偏度 / **原始峰度**（正态 = 3；DSR 参数约定同 metrics_eval）。

    样本不足或零方差 → (0.0, 3.0)：等价于正态假设，不让 None 把 DSR 打成缺失。
    """
    d = x[np.isfinite(x)]
    if d.size < 2:
        return 0.0, 3.0
    c = d - d.mean()
    m2 = float((c**2).mean())
    if m2 <= 0:
        return 0.0, 3.0
    m3 = float((c**3).mean())
    m4 = float((c**4).mean())
    return m3 / m2**1.5, m4 / m2**2


def _unavailable_report(status: str, reason: str | None, note: str) -> dict[str, Any]:
    """降级出口：占位字段齐全、**不含任何数字**。"""
    return {
        "available": False,
        "status": status,
        "reason": reason,
        "note": note,
    }


def _build_significance(
    ic: np.ndarray,
    turnover: np.ndarray,
    daily_ir: float | None,
    n_days: int,
    ls_daily: np.ndarray,
    *,
    family_nw_t: Sequence[float] | None,
    self_index: int,
    n_trials: int | None,
    n_trials_source: str | None,
) -> dict[str, Any]:
    """显著性块：NW/普通 t、BY 校正 q、DSR、Bootstrap CI、拥挤度。"""
    nw_t = M.nw_tstat(ic)
    plain_t = M.plain_tstat(ic)
    p_value = M.normal_pvalue(nw_t)

    # 族校正：族 = 同批次全部完成单元的 NW t（调用方保证非空且 self_index 对齐）。
    # q 计算与矩阵显著性列共用 q_value_from_family（单源）。
    q_value = p_value
    family_n = 1
    family_note: str
    if family_nw_t and 0 <= int(self_index) < len(family_nw_t):
        family_n = len(family_nw_t)
        if p_value is not None:
            q_value = q_value_from_family(family_nw_t, int(self_index))
        family_note = _FAMILY_NOTE_CORRECTED
    else:
        family_note = _FAMILY_NOTE_UNCORRECTED

    if n_trials is None:
        if family_n > 1:
            n_trials_value, source = family_n, "batch_completed_units"
        else:
            n_trials_value, source = 1, "default_single"
    else:
        n_trials_value = int(n_trials)
        source = n_trials_source or "param"

    skew, kurt = _skew_kurt_of(ls_daily)
    dsr = M.deflated_sharpe(daily_ir, n_trials_value, int(n_days), skew, kurt)

    return {
        "plain_t": plain_t,
        "nw_t": nw_t,
        "p_value": p_value,
        "q_value_bhy": q_value,
        "family_n": family_n,
        "family_note": family_note,
        "dsr": dsr,
        "n_trials": n_trials_value,
        "n_trials_source": source,
        "dsr_note": (
            "DSR 去膨胀所用试次数取批内完成单元数（挖掘史选型的真实试错数不可考，"
            "此为可计算下界，非全史试次数）"
        ),
        "bootstrap": M.bootstrap_ci(ic, stat="mean"),
        "crowding": M.crowding_score(ic, turnover),
    }


def _build_excess(run: dict[str, Any], series: dict[str, Any]) -> dict[str, Any]:
    """超额基准标注：等权兜底绝不冒充指数超额（载荷 bench 恒 equal_weight）。"""
    kind = str(series.get("bench") or "equal_weight")
    ref = (run.get("metrics") or {}).get("benchmark")
    if kind == "equal_weight":
        ref_part = f"台账参考指数 {ref} 未接入指数序列，" if ref else "未接入指数序列，"
        return {
            "kind": kind,
            "benchmark_ref": ref,
            "label": "区间等权兜底",
            "note": (
                f"超额基准为全域等权组合（兜底口径）；{ref_part}"
                "不得把该差额解读为对指数的超额。"
            ),
        }
    return {
        "kind": kind,
        "benchmark_ref": ref,
        "label": kind,
        "note": f"超额基准 = 载荷 bench={kind}（序列口径见落盘侧）。",
    }


def build_report_block(
    run: dict[str, Any],
    series: dict[str, Any] | None,
    *,
    family_nw_t: Sequence[float] | None = None,
    self_index: int = 0,
    n_trials: int | None = None,
    n_trials_source: str | None = None,
) -> dict[str, Any]:
    """装配报告标量块。

    Args:
        run: 台账行（需 ``status``；completed 时需 ``metrics``，批次族需
            ``batch_id`` 已由调用方查好兄弟行）。
        series: 序列载荷（``/runs/{id}/series`` 的 ``series`` 段原样）。
        family_nw_t: 族内各单元 NW t 列表（含自身；调用方对齐顺序）。None →
            无族上下文，q=p。
        self_index: 自身在 ``family_nw_t`` 中的位置。
        n_trials: DSR 试次数覆盖（None → 批内完成单元数 / 1）。
        n_trials_source: 覆盖来源标签（None → 自动）。

    Returns:
        JSON 安全 dict：completed → 标量块（headline/significance/cost_grid/
        excess/unavailable/meta）；其余终态 → ``available=False`` + reason。
    """
    status = str(run.get("status") or "unknown")
    if status != "completed":
        return _unavailable_report(
            status,
            run.get("error") or f"status={status}",
            "非完成终态：报告不出数字（诚实降级，原因见 reason）",
        )
    if not series or not series.get("dates"):
        return _unavailable_report(
            status,
            "series_not_stored",
            "序列未落盘（或落盘失败），报告无数据面可装配",
        )

    dates = series.get("dates") or []
    ic = np.asarray(series.get("ic") or [], dtype=np.float64).ravel()
    turnover = np.asarray(series.get("turnover") or [], dtype=np.float64).ravel()
    ls_daily = _nav_daily(series.get("nav_ls"))

    headline = M.brain_headline(ls_daily, turnover)
    significance = _build_significance(
        ic,
        turnover,
        headline.get("ir"),
        int(headline.get("n_days") or 0),
        ls_daily,
        family_nw_t=family_nw_t,
        self_index=self_index,
        n_trials=n_trials,
        n_trials_source=n_trials_source,
    )
    meta = series.get("meta") or {}
    return {
        "available": True,
        "status": status,
        "run_id": run.get("run_id"),
        "n_days": len(dates),
        "headline": headline,
        "significance": significance,
        "cost_grid": M.cost_sensitivity(ls_daily, turnover),
        "excess": _build_excess(run, series),
        "unavailable": [dict(b) for b in _UNAVAILABLE_BLOCKS],
        "meta": {
            "cost_bps": meta.get("cost_bps"),
            "top_pct": meta.get("top_pct"),
            "turnover_convention": meta.get("turnover_convention"),
            "source": "stored_series",
        },
    }
