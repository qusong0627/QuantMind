"""T-MV-09：机构级验证报告装配层（纯函数、无 IO、不重算指标）。

口径纪律（框架 §4 T-MV-09 与 §5 边界）：
1. **复用不复制**：IC/ICIR 取 T-FB 回测台账结果（``tfb_run_id`` 链回
   ``rd_agent_factor_backtests``，T-MV-10 排名消费）；半衰期与分组单调
   直接调 ``factor_report.metrics``（``half_life_days`` / ``monotonicity``），
   与 T-FB 报告同一纯函数层——本模块**不复制任何指标实现**，DSR 修正不属
   本任务，报告不给 DSR 结论。
2. **六节诚实降级**：IC、衰减、换手、分组单调、正交增量（T-MV-08 留痕
   只读透传）、PIT；每节显式 ``status`` + ``reason``，缺数据绝不填 0——
   ``n_days=0`` 的视界是「未测」不是「IC=0」，缺视界跳过不算 0。
3. **PIT = 截断不变性探针**：在探针日截断数据重算，探针日截面值逐点差
   ≤ ``PIT_TOL``（1e-9 绝对）才判 pass；任何点超容差 = 值依赖未来数据
   （前视）；共同标的不足判 unknown——**未知 ≠ 通过**。
4. **JSON 安全**：一切 NaN/Inf → None（与 ``metrics_core._none_if_nan``
   同规），报告必过 ``json.dumps``。

装配（:func:`build_validation_report`）由 ``scripts/mining_factor_validate.py``
在因子生成后自动调用；本模块自身不做任何 IO。
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from backend.services.engine.factor_report import metrics as M

PIT_TOL = 1e-9
"""截断不变性的绝对值容差：因果因子截断重算应逐位一致（parquet 往返无损）。"""

PIT_PROBE_OFFSET_DAYS = 30
"""探针日 = 数据日历倒数第 30 个交易日（留足尾部样本，探针不贴数据末缘）。"""

PIT_MIN_COMMON = M.MIN_SAMPLES
"""探针对照的最少共同标的数（复用平台横截面常量，不另起门槛）。"""

_CONCLUDED_SECTION_STATUSES = ("ok", "pass", "fail")
"""已出结论的节状态：只有这些节不重复计入顶层 ``unavailable``（降级面）。

其余（``unavailable``、``unknown``、T-MV-08 的 ``no_panel``/``no_parents``
等）一律是「未结论」——未知 ≠ 通过，必须出现在降级面，节内 verdict 保留。
"""


def _num(v: Any) -> float | None:
    """非有限/不可转 → None（JSON 安全铁律；语义见 metrics_core._none_if_nan）。"""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _json_scalar(v: Any) -> Any:
    """指标字典逐值转换：数值走 ``_num``（NaN/Inf→None）；非数值**标量**
    （str/bool）原样透传——T-FB 指标里的溯源字段（``bench_used``/
    ``data_source``/``window``…）是字符串，被 ``_num`` 抹成 None 就等于
    报告丢口径（引擎契约「只记实际用上的口径」，引用面不得断链）。容器
    （dict/list）照旧不转：内部可含 NaN，JSON 铁律优先。
    """
    if v is None or isinstance(v, (str, bool)):
        return v
    if isinstance(v, (int, float)):
        return _num(v)
    return None


# ── 各节装配 ─────────────────────────────────────────────────────────


def ic_section(tfb: dict | None, h5_ic: dict | None) -> dict:
    """IC/ICIR 节：优先 T-FB 台账口径；失效则 h5 因子值×次日收益兜底。"""
    if tfb and tfb.get("ok") and tfb.get("metrics"):
        return {
            "status": "ok",
            "source": "tfb_run",
            "run_id": tfb.get("run_id"),
            "window": tfb.get("window"),
            "universe": tfb.get("universe"),
            "metrics": {k: _json_scalar(v) for k, v in tfb["metrics"].items()},
            "note": "T-FB 回测台账口径（本报告只引用不重算）",
        }

    stats = ((h5_ic or {}).get("stats_by_horizon") or {}).get(1)
    if stats and int(stats.get("n_days") or 0) > 0:
        reason = (tfb or {}).get("reason") or (tfb or {}).get("status") or "no_run"
        return {
            "status": "ok",
            "source": "h5_values",
            "run_id": None,
            "window": (h5_ic or {}).get("window"),
            "universe": (h5_ic or {}).get("universe"),
            "metrics": {k: _num(v) for k, v in stats.items()},
            "note": f"T-FB 运行不可用（{reason}）；本口径=挖掘 h5 因子值×Qlib 次日"
            "收益（1 日视界），与 T-FB 池口径可能不同",
        }

    return {
        "status": "unavailable",
        "reason": "no_ic_source",
        "note": "既无 T-FB 运行指标，也无 h5 因子值 IC 统计",
    }


def decay_section(stats_by_horizon: dict | None, *, error: str | None = None) -> dict:
    """衰减节：多视界 IC 曲线 + 半衰期（单源 ``M.half_life_days``）。

    ``stats_by_horizon`` 为空且 ``error`` 非空 = 数据面**尝试过但失败**：
    原因升级为 ``h5_stage_error`` 并附错误正文（区别于 h5 阶段未跑的
    ``no_h5_stats``——2026-10-10 集成实跑取证：真实异常曾被 no_h5_stats
    的「不可算/未生成」文案掩盖）。
    """
    if not stats_by_horizon:
        if error:
            return {
                "status": "unavailable",
                "reason": "h5_stage_error",
                "note": f"衰减节数据面计算失败：{error}",
            }
        return {
            "status": "unavailable",
            "reason": "no_h5_stats",
            "note": "无多视界 IC 统计（挖掘因子值不可算/未生成）",
        }

    curve: dict[str, float | None] = {}
    icir_by_horizon: dict[str, float | None] = {}
    n_days_by_horizon: dict[str, int] = {}
    ic_by_horizon: dict[int, float] = {}
    for raw_h in sorted(stats_by_horizon):
        try:
            h = int(raw_h)
        except (TypeError, ValueError):
            continue
        stats = stats_by_horizon[raw_h] or {}
        n_days = int(stats.get("n_days") or 0)
        if n_days <= 0:
            continue  # 无样本视界 = 未测：跳过而非当 0（当 0 会出假半衰期）
        ic = _num(stats.get("ic"))
        curve[str(h)] = ic
        icir_by_horizon[str(h)] = _num(stats.get("icir"))
        n_days_by_horizon[str(h)] = n_days
        if ic is not None:
            ic_by_horizon[h] = ic

    if not curve:
        return {
            "status": "unavailable",
            "reason": "no_h5_samples",
            "note": "全部视界无有效样本（n_days=0）",
        }

    half = M.half_life_days(ic_by_horizon) if ic_by_horizon else None
    return {
        "status": "ok",
        "curve": curve,
        "icir_by_horizon": icir_by_horizon,
        "n_days_by_horizon": n_days_by_horizon,
        "half_life_days": _num(half),
        "n_horizons": len(curve),
        "note": "视界=1/2/5/10 交易日；半衰期=|IC_H|≤|IC_1|/2 的最早 H（已测视界间"
        "线性插值，缺视界跳过不算 0）；未衰减到一半 → null",
    }


def turnover_section(series: dict | None) -> dict:
    """换手节：T-FB 序列的日均双边换手；界面单边口径 = 双边/2。"""
    vals = (series or {}).get("turnover")
    finite = [f for f in (_num(v) for v in (vals or [])) if f is not None]
    if not vals:
        return {
            "status": "unavailable",
            "reason": "no_series",
            "note": "无 T-FB 序列（回测未产出/已降级）",
        }
    if not finite:
        return {
            "status": "unavailable",
            "reason": "no_finite_turnover",
            "note": "序列存在但无有效换手值",
        }
    mean = float(np.mean(finite))
    return {
        "status": "ok",
        "daily_two_sided_mean": mean,
        "one_side_mean": mean / 2.0,
        "n_days": len(finite),
        "convention": "daily_two_sided",
        "note": "换手=日均双边 Σ|Δw|（T-FB 口径）；界面单边值=双边/2；成本已按 bps 计入净收益",
    }


def _bucket_ordinal(key: Any) -> int | None:
    """'q12' → 12；非 q+数字 形状返回 None（防御未知键）。"""
    text = str(key)
    if text.startswith("q") and text[1:].isdigit():
        return int(text[1:])
    return None


def _nav_mean(navs: Any) -> float | None:
    """累计净值序列 → 日均收益（缺口日剔除；<2 个有效点 → None）。"""
    raw = [_num(v) for v in (navs or [])]
    arr = np.array([v if v is not None else np.nan for v in raw], dtype=np.float64)
    if arr.size < 2:
        return None
    with np.errstate(invalid="ignore", divide="ignore"):
        rets = arr[1:] / arr[:-1] - 1.0
    finite = rets[np.isfinite(rets)]
    if finite.size == 0:
        return None
    return float(finite.mean())


def monotonicity_section(series: dict | None) -> dict:
    """分组单调节：各分位桶日均收益 → 分位序号秩相关（单源 ``M.monotonicity``）。"""
    curves = (series or {}).get("q_curves")
    if not isinstance(curves, dict) or not curves:
        return {
            "status": "unavailable",
            "reason": "no_series",
            "note": "无 T-FB 序列（回测未产出/已降级）",
        }
    keys = sorted(
        (k for k in curves if _bucket_ordinal(k) is not None),
        key=_bucket_ordinal,  # 数值序：字典序会把 q10 排到 q2 前（静默错序）
    )
    if len(keys) < 3:
        return {
            "status": "unavailable",
            "reason": "too_few_buckets",
            "n_buckets": len(keys),
            "note": "分位组 <3（单调性需 ≥3 桶判秩相关）；检查 T-FB n_buckets 参数",
        }

    bucket_means = [_nav_mean(curves[k]) for k in keys]
    value: float | None = None
    if all(m is not None for m in bucket_means):
        value = _num(M.monotonicity(bucket_means))
    if value is None:
        return {
            "status": "unavailable",
            "reason": "degenerate_buckets",
            "bucket_means": bucket_means,
            "q_order": keys,
            "note": "存在无收益样本的桶或全桶同值（方差为 0），单调性未定义——不当数字给结论",
        }
    return {
        "status": "ok",
        "value": value,
        "bucket_means": bucket_means,
        "n_buckets": len(keys),
        "q_order": keys,
        "note": "分位序号与分位桶日均收益的秩相关（q1 最低 … qN 最高；"
        "正 = 高分组收益更高；±1 = 完美单调）",
    }


def orthogonality_section(trace: dict | None) -> dict:
    """正交增量节：T-MV-08 留痕只读透传（不重算；缺留痕说明获取路径）。

    留痕无 ``status``（T-MV-08 契约字段）时为 ``unavailable/missing_status``
    ——不得洗成 "ok"：是否已评估无从判定（未知 ≠ 通过）。
    """
    if not trace:
        return {
            "status": "unavailable",
            "reason": "no_trace",
            "note": "正交留痕由池刷新（T-MV-08）写入 metadata_json.orthogonality；"
            "待因子入池刷新后重跑验证即有",
        }
    raw_status = trace.get("status")
    if not (isinstance(raw_status, str) and raw_status.strip()):
        return {
            "status": "unavailable",
            "reason": "missing_status",
            "note": "正交留痕缺少 status（T-MV-08 契约字段）——是否已评估无从判定",
        }
    parents = []
    for p in trace.get("parents") or []:
        if not isinstance(p, dict):
            continue
        parents.append(
            {
                "factor_id": p.get("factor_id"),
                "name": p.get("name"),
                "corr": _num(p.get("corr")),
            }
        )
    flag = trace.get("orthogonal")
    return {
        "status": raw_status.strip(),
        "residual_ic": _num(trace.get("residual_ic")),
        "candidate_ic": _num(trace.get("candidate_ic")),
        "max_parent_abs_corr": _num(trace.get("max_parent_abs_corr")),
        "threshold": _num(trace.get("threshold")),
        "orthogonal": bool(flag) if isinstance(flag, (bool, np.bool_)) else None,
        "n_days": trace.get("n_days"),
        "n_obs": trace.get("n_obs"),
        "evaluated_at": trace.get("evaluated_at"),
        "parents": parents,
        "note": "正交增量留痕（T-MV-08 池刷新产物，只读透传不重算）",
    }


def _probe_cross_section(series: pd.Series | None, probe_date: Any) -> pd.Series | None:
    """取 (trade_date, symbol) 索引序列在探针日的截面（symbol 为键）。"""
    if not isinstance(series, pd.Series) or series.empty:
        return None
    idx = series.index
    if not isinstance(idx, pd.MultiIndex):
        return None
    mask = np.asarray(idx.get_level_values(0) == probe_date, dtype=bool)
    if not mask.any():
        return None
    return pd.Series(series.to_numpy()[mask], index=idx.get_level_values(1)[mask])


def judge_truncation(
    full: pd.Series | None,
    truncated: pd.Series | None,
    *,
    probe_date: Any,
    tol: float = PIT_TOL,
    min_common: int = PIT_MIN_COMMON,
) -> dict:
    """截断不变性判定：探针日截面逐点比对（只比探针日，其他日期无约束）。"""

    def _finite_pairs(xs: pd.Series | None) -> pd.Series:
        if xs is None or xs.empty:
            return pd.Series(dtype=np.float64)
        vals = pd.to_numeric(xs, errors="coerce")
        return vals[np.isfinite(vals.to_numpy(dtype=np.float64))]

    f = _finite_pairs(_probe_cross_section(full, probe_date))
    t = _finite_pairs(_probe_cross_section(truncated, probe_date))
    base = {
        "status": "unknown",
        "probe_date": probe_date,
        "max_abs_diff": None,
        "n_common": 0,
        "n_diff": None,
        "tol": tol,
        "reason": "no_probe_section",
    }
    if f.empty or t.empty:
        return base

    # pandas 3 的 Index.intersection 空集有陷阱，统一走 numpy.isin 语义
    common = np.intersect1d(f.index.to_numpy(), t.index.to_numpy())
    n_common = int(common.size)
    if n_common < min_common:
        return {
            **base,
            "n_common": n_common,
            "reason": "insufficient_common_symbols",
        }

    fa = f.reindex(common).to_numpy(dtype=np.float64)
    ta = t.reindex(common).to_numpy(dtype=np.float64)
    diff = np.abs(fa - ta)
    n_diff = int((diff > tol).sum())
    return {
        "status": "pass" if n_diff == 0 else "fail",
        "probe_date": probe_date,
        "max_abs_diff": _num(float(diff.max())) if diff.size else None,
        "n_common": n_common,
        "n_diff": n_diff,
        "tol": tol,
        "reason": None,
    }


def pit_section(verdict: dict | None) -> dict:
    """PIT 节：探针判定透传 + 机制说明。"""
    if not verdict:
        return {
            "status": "unavailable",
            "reason": "probe_not_run",
            "note": "截断不变性探针未运行（h5 数据缺失或因子代码执行失败）",
        }
    return {
        "status": verdict.get("status") or "unknown",
        "probe_date": verdict.get("probe_date"),
        "max_abs_diff": _num(verdict.get("max_abs_diff")),
        "n_common": verdict.get("n_common"),
        "n_diff": verdict.get("n_diff"),
        "tol": _num(verdict.get("tol")),
        "reason": verdict.get("reason"),
        "note": "截断在探针日（去掉其后全部数据）重算，探针日截面值须逐点一致"
        "（容差内）；任何超容差差异 = 值依赖未来数据（前视）",
    }


def build_validation_report(
    *,
    factor_id: str,
    market: str,
    generated_at: str,
    tfb: dict | None = None,
    h5_ic: dict | None = None,
    series: dict | None = None,
    orthogonality: dict | None = None,
    pit: dict | None = None,
    h5_error: str | None = None,
) -> dict:
    """六节装配 → 验证报告（顶层状态只由「是否缺节」裁决）。

    ``tfb``：{"run_id","ok","status","metrics","window","universe","reason"}；
    ``h5_ic``：{"stats_by_horizon": {h: daily_ic_stats}, "window","universe"}；
    ``series``：T-FB ``rd_agent_factor_backtest_series.payload``；
    ``orthogonality``：T-MV-08 ``metadata_json.orthogonality`` 留痕；
    ``pit``：:func:`judge_truncation` 输出；
    ``h5_error``：h5 数据面失败的异常正文（None = 未尝试/成功）——
    透传给 :func:`decay_section` 区分「失败」与「未生成」。
    """
    sections = {
        "ic": ic_section(tfb, h5_ic),
        "decay": decay_section((h5_ic or {}).get("stats_by_horizon"), error=h5_error),
        "turnover": turnover_section(series),
        "monotonicity": monotonicity_section(series),
        "orthogonality": orthogonality_section(orthogonality),
        "pit": pit_section(pit),
    }
    unavailable = []
    for name, sec in sections.items():
        if sec["status"] in _CONCLUDED_SECTION_STATUSES:
            continue
        # 未结论态一律计入降级面（未知 ≠ 通过）：unavailable（无数据）、
        # pit unknown（对照不足/执行失败）、T-MV-08 降级态（no_panel 等）
        # 同一纪律——节内 verdict/原因保留，顶层不得冒充「验证齐全」。
        unavailable.append(
            {"section": name, "reason": sec.get("reason") or sec["status"]}
        )
    return {
        "factor_id": factor_id,
        "market": market,
        "generated_at": generated_at,
        "status": "completed" if not unavailable else "degraded",
        "tfb_run_id": (tfb or {}).get("run_id"),
        "sections": sections,
        "unavailable": unavailable,
    }
