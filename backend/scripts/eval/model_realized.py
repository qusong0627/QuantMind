"""模型四维实证引擎（T-P4-05b-2 补全，设计《评估与打分体系》§2.2）。

``model_card.py`` 原本把「分层能力 / 稳健性 / 滚动健康 / 换手与成本」四维写成
``_insufficient(...)`` 占位（v1 缺省），结果是模型卡只有 OOS 一维有分——而
**分数看起来照常有**（缺维权重按剩余权重归一），前端无法分辨「真的评过」与
「根本没评」。本模块把这四维做成真算的，并强制把**用了哪一级证据**写进结果。

证据优先级（逐级降级，降级必须留痕）：
  1. ``<model_dir>/pred.parquet`` 的 test 段 —— 预测明细在盘，四维全算；
  2. ``metadata.eval_report`` —— 训练期留存的评估报告，可算分层/稳健/滚动，
     换手与成本**如实缺省**（报告里没有持仓序列，不硬造）；
  3. ``qm_model_inference_quality`` 每日 RankIC —— 只够算滚动健康；
  4. 都没有 —— 四维如实 ``insufficient`` + 指定 note，**绝不静默跳过**。

取数与算法分离：横截面统计全在 ``realized_stats.py``（纯函数），本模块负责
设计 §2.2 的评分口径、证据降级编排与文件 IO。夜间批量要对几十个 GB 级模型
重算，故 pred.parquet 的结果按 (mtime_ns, size) 缓存到
``<model_dir>/.eval_cache.json``：产物没变就零 parquet 读取。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from backend.services.engine.inference.trading_cost import CostModel
from backend.scripts.eval.model_cache import (
    CACHE_NAME,
    CACHE_VERSION,
    read_cache,
    read_cache_series,
    write_cache,
)
from backend.scripts.eval.realized_stats import (
    PRED_EVAL_COLUMNS,
    PRED_TEST_SPLIT,
    TRADING_DAYS,
    daily_ic,
    decile_stats,
    drop_degenerate_days,
    normalize_pred_frame,
    rolling_health,
    segment_ic,
    spearman_rank_corr,
    topk_by_date,
    topk_turnover,
)
from backend.shared.eval_scoring import DimensionScore, score_from_thresholds

# ── 证据层级（结果里必须写明用了哪一级） ────────────────────────────────
TIER_PRED = "pred.parquet:test"
TIER_EVAL_REPORT = "metadata.eval_report"
TIER_INFERENCE_QUALITY = "qm_model_inference_quality"
TIER_NONE = "insufficient"

TIER_LABELS = {
    TIER_PRED: "OOS 预测明细（pred.parquet test 段重算）",
    TIER_EVAL_REPORT: "metadata.eval_report（训练期留存的评估报告）",
    TIER_INFERENCE_QUALITY: "qm_model_inference_quality（每日推理 RankIC）",
    TIER_NONE: "无可用实证证据",
}

OFF_DISK_NOTE = "产物不在盘：storage_path 指向的目录不存在（无从实证——这是「没证据」，不是「通过」）"
NO_ARTIFACT_NOTE = (
    "目录在盘但无可用实证产物（pred.parquet / metadata.eval_report / "
    "qm_model_inference_quality 全缺）——「没证据」不等于「通过」"
)
NO_SIGNAL_NOTE = (
    "pred 全部交易日均为常数（模型没有任何截面区分度）：排序、分档、换手都无定义，"
    "四维如实缺省——「没信号」不等于「信号为 0」，不编造 IC=0 的分数"
)

# ── 评分口径（设计 §2.2 红线） ─────────────────────────────────────────
DEFAULT_TOP_K = 50
EVAL_REPORT_MIN_SEGMENT_DAYS = 10  # eval_report 的 split 段天数量级比日频段小
MONOTONICITY_MIN = 0.5  # 低于此判「阶梯非单调」
STEADY_IC_MIN = 0.01  # 滚动健康红线：近 20 日 IC 均值下限
LOW_CONFIDENCE_DAYS = 60  # 样本天数低于此 → 低置信（评级附 †）

DIM_LABELS = {
    "stratification": "分层能力",
    "robustness": "稳健性",
    "rolling_health": "滚动健康",
    "turnover_cost": "换手与成本",
}
DIM_WEIGHTS = {
    "stratification": 25.0,
    "robustness": 15.0,
    "rolling_health": 15.0,
    "turnover_cost": 15.0,
}
# 设计 §2.2 的维度次序（卡面/落库顺序，别依赖 dict 插入序）
DIM_ORDER = (
    "stratification",
    "robustness",
    "rolling_health",
    "turnover_cost",
)


def resolve_cost_model(
    meta: dict[str, Any] | None = None, override: dict[str, Any] | None = None
) -> CostModel:
    """费率口径唯一出处是 ``trading_cost.CostModel``（A 股默认 → metadata → override）。

    本函数只是把「模型 metadata.context 里可以带费率覆盖」这条口径接上，
    不在本模块另存一份费率表（历史上按板名查表查错过 132 行样本）。
    """
    return CostModel.resolve(meta, override)


# ── 四维评分（含设计 §2.2 红线） ───────────────────────────────────────


def _dim(
    key: str,
    score: float | None,
    detail: dict[str, Any],
    *,
    red_line_failed: bool = False,
) -> DimensionScore:
    return DimensionScore(
        key=key,
        label=DIM_LABELS[key],
        weight=DIM_WEIGHTS[key],
        score=None if score is None else round(float(score), 1),
        red_line_failed=bool(red_line_failed),
        detail=detail,
    )


def _insufficient_dim(
    key: str, note: str, extra: dict[str, Any] | None = None
) -> DimensionScore:
    """缺省维：note 必填；``extra`` 用于带上「缺了多少」这类覆盖证据。"""
    detail: dict[str, Any] = {"insufficient": True, "note": note}
    if extra:
        detail.update(extra)
    return _dim(key, None, detail)


def insufficient_dims(note: str) -> dict[str, DimensionScore]:
    """四维（不含 OOS）统一如实缺省——OOS 由 metadata.metrics 单独给分。"""
    return {key: _insufficient_dim(key, note) for key in DIM_LABELS}


def score_stratification(stats: dict[str, Any]) -> DimensionScore:
    """分层能力：单调性 + 多空 IR；阶梯非单调（含反向）→ 红线（设计 §2.2）。"""
    if not stats.get("sufficient") or stats.get("monotonicity") is None:
        return _insufficient_dim("stratification", str(stats.get("reason") or "缺省"))
    mono = float(stats["monotonicity"])
    ls_ir = abs(float(stats.get("ls_ir") or 0.0))
    mono_score = score_from_thresholds(
        mono, [(0.0, 0.0), (0.5, 50.0), (0.8, 75.0), (1.0, 100.0)]
    )
    ir_score = score_from_thresholds(
        ls_ir, [(0.0, 0.0), (0.5, 50.0), (1.0, 75.0), (2.0, 100.0)]
    )
    detail = {
        "mean_returns": stats["mean_returns"],
        "monotonicity": stats["monotonicity"],
        "strict_monotonic": stats["strict_monotonic"],
        "ls_mean": stats["ls_mean"],
        "ls_ir": stats["ls_ir"],
        "n_days": stats["n_days"],
        "n_groups": stats["n_groups"],
        "n_days_degenerate": stats.get("n_days_degenerate"),
    }
    red = mono < MONOTONICITY_MIN
    if red:
        detail["red_line"] = (
            f"阶梯非单调或反向（单调性 {mono:.2f} < {MONOTONICITY_MIN}）："
            "最高档不比最低档更赚，分层能力不成立"
        )
    return _dim(
        "stratification",
        (float(mono_score) + float(ir_score)) / 2.0,
        detail,
        red_line_failed=red,
    )


def score_robustness(stats: dict[str, Any]) -> DimensionScore:
    """稳健性：最差子样本 IC + 段间离散；任一子样本崩坏（IC ≤ 0）→ 红线。"""
    if not stats.get("sufficient"):
        return _insufficient_dim("robustness", str(stats.get("reason") or "缺省"))
    min_seg = float(stats["min_segment"])
    spread = float(stats.get("spread") or 0.0)
    seg_score = score_from_thresholds(
        min_seg, [(0.0, 0.0), (0.02, 50.0), (0.05, 75.0), (0.10, 100.0)]
    )
    # 「越低越好」用取负后的升序阈值表表达，避开 higher_is_better 镜像的歧义
    spread_score = score_from_thresholds(
        -spread, [(-0.20, 0.0), (-0.10, 40.0), (-0.05, 70.0), (0.0, 100.0)]
    )
    detail = {
        "by_split": stats["by_split"],
        "min_segment": stats["min_segment"],
        "min_segment_name": stats.get("min_segment_name"),
        "spread": stats.get("spread"),
        "segments_used": stats.get("segments_used"),
        "segment_axis": stats.get("segment_axis"),
        "min_segment_days": stats.get("min_segment_days"),
    }
    broken = list(stats.get("broken_segments") or [])
    if broken:
        detail["red_line"] = (
            f"子样本崩坏：{'、'.join(broken)} 段 IC ≤ 0（样本外预测力不成立）"
        )
    return _dim(
        "robustness",
        0.7 * float(seg_score) + 0.3 * float(spread_score),
        detail,
        red_line_failed=bool(broken),
    )


def score_rolling_health(stats: dict[str, Any]) -> DimensionScore:
    """滚动健康：近 20 日 IC 均值 + ICIR；近 20 日均值 < 0.01 → 红线（§2.2）。"""
    if not stats.get("sufficient") or stats.get("ic_mean_20") is None:
        return _insufficient_dim(
            "rolling_health",
            str(stats.get("reason") or "缺省"),
            {"coverage": stats["coverage"]} if stats.get("coverage") else None,
        )
    mean20 = float(stats["ic_mean_20"])
    icir = stats.get("icir")
    mean_score = score_from_thresholds(
        mean20, [(0.0, 0.0), (0.01, 40.0), (0.03, 65.0), (0.06, 85.0), (0.10, 100.0)]
    )
    icir_score = score_from_thresholds(
        icir, [(0.0, 0.0), (0.3, 40.0), (0.5, 65.0), (1.0, 85.0), (2.0, 100.0)]
    )
    detail = {
        "ic_mean_20": stats["ic_mean_20"],
        "ic_mean_60": stats.get("ic_mean_60"),
        "icir": icir,
        "drift": stats.get("drift"),
        "ic_last": stats.get("ic_last"),
        "n_days": stats.get("n_days"),
        "coverage": stats.get("coverage"),
    }
    red = mean20 < STEADY_IC_MIN
    if red:
        detail["red_line"] = (
            f"近 20 日 IC 均值 {mean20:.4f} < 0.01（滚动健康红线，设计 §2.2）"
        )
    return _dim(
        "rolling_health",
        (float(mean_score) + float(icir_score)) / 2.0,
        detail,
        red_line_failed=red,
    )


def score_turnover_cost(stats: dict[str, Any]) -> DimensionScore:
    """换手与成本：成本后年化收益；成本后 ≤ 0 → 红线（设计 §2.2）。"""
    if not stats.get("sufficient") or stats.get("net_annual") is None:
        return _insufficient_dim(
            "turnover_cost",
            str(stats.get("reason") or "无换手证据或毛利不可得：成本后收益无从计算"),
        )
    net = float(stats["net_annual"])
    score = score_from_thresholds(
        net, [(0.0, 0.0), (0.05, 40.0), (0.10, 65.0), (0.20, 85.0), (0.40, 100.0)]
    )
    detail = {
        "turnover_mean": stats.get("turnover_mean"),
        "round_trip_cost": stats.get("round_trip_cost"),
        "cost_drag_annual": stats.get("cost_drag_annual"),
        "gross_annual": stats.get("gross_annual"),
        "net_annual": stats.get("net_annual"),
        "top_k": stats.get("top_k"),
        "n_pairs": stats.get("n_pairs"),
        "trading_days": stats.get("trading_days"),
    }
    red = net <= 0
    if red:
        detail["red_line"] = (
            f"成本后年化 {net:.2%} ≤ 0：换手吃掉了全部毛利（设计 §2.2）"
        )
    return _dim("turnover_cost", score, detail, red_line_failed=red)


# ── 一级证据：pred.parquet test 段 ─────────────────────────────────────


def _stamp(dims: dict[str, DimensionScore], tier: str) -> dict[str, DimensionScore]:
    """给每维 detail 盖上证据层级戳（前端页脚与「假证据」排查都靠它）。"""
    out: dict[str, DimensionScore] = {}
    for key, dim in dims.items():
        detail = dict(dim.detail)
        detail["evidence"] = tier
        out[key] = DimensionScore(
            key, dim.label, dim.weight, dim.score, dim.red_line_failed, detail
        )
    return out


def _segment_axis_labels(stamps: pd.Series) -> tuple[str, np.ndarray]:
    """OOS 内部分段轴：跨年用年，否则用月。"""
    axis = "year" if stamps.dt.year.nunique() > 1 else "month"
    return axis, stamps.dt.strftime("%Y" if axis == "year" else "%Y-%m").to_numpy()


def _is_low_confidence(strat: dict[str, Any], health: dict[str, Any]) -> bool:
    for stats in (strat, health):
        n = stats.get("n_days")
        if n is not None and int(n) < LOW_CONFIDENCE_DAYS:
            return True
    return False


def pred_artifacts(
    frame: pd.DataFrame,
    *,
    cost_model: CostModel | None = None,
    n_groups: int = 10,
    top_k: int = DEFAULT_TOP_K,
    with_series: bool = False,
) -> tuple[dict[str, DimensionScore], dict[str, Any] | None]:
    """一级证据的**唯一算处**：pred.parquet test 段 → (四维, 长序列载荷)。

    统计只跑一次，分数与详情图共用同一份数——分头各算一次迟早会出现
    「卡上 82 分、曲线上却是另一组数」而没人发现。

    先过**秩退化闸门**：pred 全天取常数的日子被剔除并计数（实测 12 个真实
    pred.parquet 里有 1 个模型 120/120 天全常数）。全退化 → 四维如实缺省，
    绝不拿行序伪影当分层证据。
    """
    usable, coverage = drop_degenerate_days(frame)
    if usable.empty:
        dims = _stamp(insufficient_dims(NO_SIGNAL_NOTE), TIER_PRED)
        for dim in dims.values():
            dim.detail["coverage"] = coverage
        return dims, None

    stats = _pred_stats(usable, coverage, cost_model, n_groups, top_k)
    dims = _stamp(
        {
            "stratification": score_stratification(stats["strat"]),
            "robustness": score_robustness(stats["robust"]),
            "rolling_health": score_rolling_health(stats["health"]),
            "turnover_cost": score_turnover_cost(stats["cost"]),
        },
        TIER_PRED,
    )
    dims["stratification"].detail["low_confidence"] = _is_low_confidence(
        stats["strat"], stats["health"]
    )
    # 闸门在上游就跑了，strat 自己数到的退化日恒为 0——覆盖数必须由这里带出，
    # 否则「剔了多少天」在分层证据里会读成「一天都没剔」
    dims["stratification"].detail["coverage"] = coverage
    if not with_series:
        return dims, None
    from backend.scripts.eval.model_series import series_from_stats

    payload = series_from_stats(stats)
    payload["coverage"] = coverage
    return dims, payload


def dims_from_pred_frame(
    frame: pd.DataFrame,
    *,
    cost_model: CostModel | None = None,
    n_groups: int = 10,
    top_k: int = DEFAULT_TOP_K,
) -> dict[str, DimensionScore]:
    """一级证据：pred.parquet test 段 → 四维（不算长序列）。"""
    return pred_artifacts(frame, cost_model=cost_model, n_groups=n_groups, top_k=top_k)[
        0
    ]


def _pred_stats(
    usable: pd.DataFrame,
    coverage: dict[str, Any],
    cost_model: CostModel | None,
    n_groups: int,
    top_k: int,
) -> dict[str, dict[str, Any]]:
    """四类统计的取数（分层/分段/滚动/换手），与评分口径解耦便于单测。"""
    pred = usable["pred"].to_numpy()
    label = usable["label"].to_numpy()
    dates = usable["date_key"].to_numpy()

    strat = decile_stats(pred, label, dates, n_groups=n_groups)
    axis, segs = _segment_axis_labels(pd.to_datetime(usable["date_key"]))
    robust = segment_ic(pred, label, dates, segs)
    robust["segment_axis"] = axis
    ic_by_date = daily_ic(pred, label, dates)
    health = rolling_health(ic_by_date)
    health["coverage"] = coverage

    median_stocks = int(strat.get("median_stocks_per_day") or 0)
    top_k_used = max(1, min(int(top_k), max(1, median_stocks // 2)))
    gross = (
        None if strat.get("ls_mean") is None else float(strat["ls_mean"]) * TRADING_DAYS
    )
    cost = topk_turnover(
        topk_by_date(usable, k=top_k_used),
        k=top_k_used,
        round_trip_cost=(cost_model or CostModel()).round_trip_cost(),
        gross_annual=gross,
    )
    return {
        "strat": strat,
        "robust": robust,
        "health": health,
        "cost": cost,
        # 逐日 IC 序列本身留给 /eval/series 侧车（长度上千点，不进列表响应）
        "daily_ic": ic_by_date,
    }


# ── 二级证据：metadata.eval_report ─────────────────────────────────────


def _report_strat(report: dict[str, Any], groups: dict[str, Any]) -> dict[str, Any]:
    """eval_report 的 groups → 分层统计 dict（口径与 pred 路径同构）。"""
    means = groups.get("mean_returns")
    if not means or len(means) < 2:
        return {
            "sufficient": False,
            "reason": "eval_report 无 groups.mean_returns",
            "mean_returns": None,
            "monotonicity": None,
            "strict_monotonic": False,
            "ls_mean": None,
            "ls_ir": None,
            "ls_by_day": [],
            "n_days": 0,
            "n_groups": 0,
        }
    mono = groups.get("monotonicity")
    if mono is None:
        mono = spearman_rank_corr(
            np.arange(1, len(means) + 1, dtype=float), np.asarray(means, dtype=float)
        )
    if mono is None:
        # 阶梯全平：档间无高低可分，单调性无定义——不编 0.0（否则会被读成「非单调」红线）
        return {
            "sufficient": False,
            "reason": (
                f"eval_report 的 {len(means)} 档均值完全相同（各档无收益差）："
                "单调性无定义，分层能力如实缺省"
            ),
            "mean_returns": None,
            "monotonicity": None,
            "strict_monotonic": False,
            "ls_mean": None,
            "ls_ir": None,
            "ls_by_day": [],
            "n_days": report.get("n_days") or 0,
            "n_groups": int(groups.get("n_groups") or len(means)),
        }
    return {
        "sufficient": True,
        "reason": None,
        "mean_returns": [round(float(v), 6) for v in means],
        "monotonicity": round(float(mono), 6),
        "strict_monotonic": all(b > a for a, b in zip(means, means[1:], strict=False)),
        "ls_mean": round(float(means[-1]) - float(means[0]), 6),
        "ls_ir": (report.get("long_short") or {}).get("sharpe"),
        "ls_by_day": [],
        "n_days": report.get("n_days") or len(report.get("ic_curve") or []),
        "n_groups": int(groups.get("n_groups") or len(means)),
    }


def _report_segments(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """eval_report 的分段 IC：by_split + yearly（年度段一并作为子样本）。"""
    segments: dict[str, dict[str, Any]] = {}
    for name, body in (report.get("by_split") or {}).items():
        if isinstance(body, dict):
            segments[str(name)] = {
                "ic_mean": body.get("rank_ic", body.get("ic_mean")),
                "n_days": body.get("n_days"),
            }
    for row in report.get("yearly") or []:
        if isinstance(row, dict) and row.get("year") is not None:
            segments.setdefault(
                str(row["year"]),
                {"ic_mean": row.get("rank_ic"), "n_days": row.get("n_days")},
            )
    return segments


def _report_robust(segments: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """分段 → 稳健性统计 dict（崩坏判定用 eval_report 尺度的最小天数）。"""
    scored = {k: v for k, v in segments.items() if v.get("ic_mean") is not None}
    if not scored:
        return {"sufficient": False, "reason": "eval_report 无 by_split/yearly IC"}
    values = [float(v["ic_mean"]) for v in scored.values()]
    min_name = min(scored, key=lambda k: scored[k]["ic_mean"])
    return {
        "sufficient": True,
        "reason": None,
        "by_split": segments,
        "min_segment": float(scored[min_name]["ic_mean"]),
        "min_segment_name": min_name,
        "segments_used": sorted(scored),
        "broken_segments": sorted(
            k
            for k, v in scored.items()
            if float(v["ic_mean"]) <= 0
            and int(v.get("n_days") or 0) >= EVAL_REPORT_MIN_SEGMENT_DAYS
        ),
        "spread": round(max(values) - min(values), 6),
        "min_segment_days": EVAL_REPORT_MIN_SEGMENT_DAYS,
        "segment_axis": "split/year",
    }


def dims_from_eval_report(
    report: dict[str, Any], *, cost_model: CostModel | None = None
) -> dict[str, DimensionScore]:
    """二级证据：``metadata.eval_report``（训练期留存的评估报告）。

    报告里有分层（``groups``）、分段（``by_split``/``yearly``）、滚动（``ic_curve``），
    **没有持仓与成交序列** → 换手与成本如实缺省（不硬造）。
    """
    strat = _report_strat(report, report.get("groups") or {})
    robust = _report_robust(_report_segments(report))
    health = rolling_health(
        {str(i): float(v) for i, v in enumerate(report.get("ic_curve") or [])}
    )
    cost_note = "metadata.eval_report 无持仓/成交序列，成本后收益无从计算（如实缺省）"
    return _stamp(
        {
            "stratification": score_stratification(strat),
            "robustness": score_robustness(robust),
            "rolling_health": score_rolling_health(health),
            "turnover_cost": _insufficient_dim("turnover_cost", cost_note),
        },
        TIER_EVAL_REPORT,
    )


# ── 三级证据：qm_model_inference_quality ───────────────────────────────


def dims_from_inference_quality(
    model_id: str, rows: list[dict[str, Any]]
) -> dict[str, DimensionScore]:
    """三级证据：``qm_model_inference_quality`` 每日 RankIC → 只够滚动健康。"""
    ordered = sorted(
        (
            r
            for r in rows
            if r.get("rank_ic") is not None and r.get("trade_date") is not None
        ),
        key=lambda r: str(r["trade_date"]),
    )
    health = rolling_health(
        {str(r["trade_date"]): float(r["rank_ic"]) for r in ordered}
    )
    return _stamp(
        {
            "stratification": _insufficient_dim(
                "stratification",
                f"{model_id}：推理质量表只有逐日 RankIC，无预测明细，分层算不出（如实缺省）",
            ),
            "robustness": _insufficient_dim(
                "robustness",
                f"{model_id}：推理质量表只有逐日 RankIC，子样本分段算不出（如实缺省）",
            ),
            "rolling_health": score_rolling_health(health),
            "turnover_cost": _insufficient_dim(
                "turnover_cost",
                f"{model_id}：推理质量表无持仓序列，成本后收益无从计算（如实缺省）",
            ),
        },
        TIER_INFERENCE_QUALITY,
    )


# ── 取数（唯一文件 IO 处） ─────────────────────────────────────────────


def available_pred_columns(path: str | Path) -> tuple[str, ...]:
    """pred.parquet 实际存在的评估列（列投影读取的前提）。"""
    import pyarrow.parquet as pq

    names = set(pq.ParquetFile(str(path)).schema_arrow.names)
    return tuple(c for c in PRED_EVAL_COLUMNS if c in names)


def read_test_split(path: str | Path) -> pd.DataFrame:
    """读 pred.parquet 的 test 段（只读评估需要的列）。

    A 股用户模型的 pred.parquet 是百 MB 级（实测 54–127 MB），只投影
    ``PRED_EVAL_COLUMNS`` 里实际存在的列可显著降低夜间批量的 IO。
    """
    cols = available_pred_columns(path)
    if not cols:
        raise ValueError(f"pred.parquet 无可识别评估列: {path}")
    return normalize_pred_frame(
        pd.read_parquet(path, columns=list(cols)), split=PRED_TEST_SPLIT
    )


# ── 入口：证据优先级降级（唯一 IO 编排处） ─────────────────────────────


def _missing_dims(
    model_id: str, model_dir: Path | None, on_disk: bool
) -> tuple[dict[str, DimensionScore], dict[str, Any]]:
    note = OFF_DISK_NOTE if not on_disk else NO_ARTIFACT_NOTE
    return insufficient_dims(note), {
        "tier": TIER_NONE,
        "tier_label": TIER_LABELS[TIER_NONE],
        "source": None,
        "on_disk": on_disk,
        "cache": "n/a",
        "note": note,
        "model_id": model_id,
        "model_dir": str(model_dir) if model_dir else None,
    }


def _as_evidence(tier: str, source: str | None, **extra: Any) -> dict[str, Any]:
    return {
        "tier": tier,
        "tier_label": TIER_LABELS[tier],
        "source": source,
        "on_disk": True,
        "cache": "n/a",
        **extra,
    }


def _resolve_pred(
    model_id: str,
    directory: Path,
    pred_path: Path,
    cost_model: CostModel,
    *,
    collect_series: bool,
) -> tuple[dict[str, DimensionScore], dict[str, Any]] | None:
    """一级证据：pred.parquet 在盘则重算（或吃 sidecar），否则 None 交由下一级。

    ``collect_series`` 时序列随 evidence 的 ``series`` 键带出（**只给落盘用**：
    列表接口不许带它）。缓存命中但缓存里没有序列（v1 老缓存）→ 如实标注，
    不假装有序列。
    """
    if not pred_path.is_file():
        return None
    cached = read_cache(directory, pred_path)
    if cached is not None:
        series = read_cache_series(directory, pred_path) if collect_series else None
        extra: dict[str, Any] = {}
        if collect_series:
            extra["series"] = series
            if series is None:
                extra["series_note"] = (
                    f"缓存（{CACHE_NAME} v{CACHE_VERSION}）里没有长序列载荷"
                    "——删缓存重跑即可重建"
                )
        return cached, _as_evidence(
            TIER_PRED, str(pred_path), cache="hit", model_id=model_id, **extra
        )
    frame = read_test_split(pred_path)
    dims, series = pred_artifacts(
        frame, cost_model=cost_model, with_series=collect_series
    )
    write_cache(directory, pred_path, dims, series)
    return dims, _as_evidence(
        TIER_PRED,
        str(pred_path),
        cache="miss",
        n_rows=int(frame.shape[0]),
        model_id=model_id,
        **({"series": series} if collect_series else {}),
    )


def resolve_dims(
    model_id: str,
    *,
    meta: dict[str, Any] | None,
    model_dir: Path | str | None,
    quality_rows: list[dict[str, Any]] | None = None,
    cost_model: CostModel | None = None,
    collect_series: bool = False,
) -> tuple[dict[str, DimensionScore], dict[str, Any]]:
    """按证据优先级给出模型四维，并返回用了哪一级（降级必留痕）。

    ``quality_rows`` 由调用方从 ``qm_model_inference_quality`` 取好传入——本函数
    只做文件 IO，不碰 DB。

    ``collect_series=True`` 时 evidence 额外带 ``series``（长序列载荷，写侧车用）。
    **它是临时件**：调用方必须在落库前摘掉，别让它进 ``inputs_version``——
    列表接口就是靠「只有标量」才轻的。
    """
    directory = Path(model_dir) if model_dir else None
    on_disk = bool(directory and directory.is_dir())
    resolved_cost = cost_model or resolve_cost_model(meta)

    if directory is not None:
        hit = _resolve_pred(
            model_id,
            directory,
            directory / "pred.parquet",
            resolved_cost,
            collect_series=collect_series,
        )
        if hit is not None:
            return hit

    report = (meta or {}).get("eval_report")
    if isinstance(report, dict) and report:
        return dims_from_eval_report(report, cost_model=resolved_cost), _as_evidence(
            TIER_EVAL_REPORT, "metadata.eval_report", on_disk=on_disk, model_id=model_id
        )

    if quality_rows:
        return dims_from_inference_quality(model_id, quality_rows), _as_evidence(
            TIER_INFERENCE_QUALITY,
            "qm_model_inference_quality",
            on_disk=on_disk,
            n_rows=len(quality_rows),
            model_id=model_id,
        )

    return _missing_dims(model_id, directory, on_disk)
