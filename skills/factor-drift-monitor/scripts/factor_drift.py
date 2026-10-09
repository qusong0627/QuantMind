#!/usr/bin/env python3
"""因子漂移监测 — 逐分区扫描 QuantDB 因子面板（CN/HK/US）与确定性合成面板。

来源：quantskills/skill-factor-drift-monitor（GPL-3.0-only），方法论移植并本地化：
保留「覆盖/缺失/列集/分布」四类检查与 normal/watch/warning/failed 分级思想，
数据层由 PandaData Parquet 换为本地 QuantDB 直读，报告改为 JSON + stdout 中文表格。

两类模式：
  --demo               纯标准库，任意机器可跑。内置确定性合成面板（31 分区 × 50 标的 × 8 列）
                       与四种注入故障（末日整列缺失 / 列变常量 / 列集减少一列 / 分布 +2σ 平移），
                       断言四种注入各自触发对应告警类别、干净面板零 critical/零 warning。
  --quantdb ...        pandas/pyarrow 延迟导入，**在 quantmind 容器内运行**：
                       逐分区扫描 <market_root>/6_ml_datasets/<dataset>（CN 默认 features_daily，
                       另有 l1_factors/l2_factors；HK/US 为 l1_factors），行情分区（1_kline_data/
                       daily_forward）仅列目录不读文件，用于「分区断更 / 缺分区 / 节假日」判读。

四类检查（细节与阈值出处见 references/drift-methods.md）：
  ① 覆盖/断更：分区数、每分区标的数、日期连续性、最新分区 vs 行情与今天
  ② 缺失与常量：每列非空率、nunique、全空列、新出现的常量列
  ③ 列集漂移：相邻分区列增删/列序变化事件 + 最新分区 vs 基线列集
  ④ 分布漂移：数值列（float）对基线窗口的 PSI / K-S / 均值平移（σ 单位）/ 标准差比

标签列（label_return / return_{n}d / future_return_{n}d）单列为「label」类别：
存在性与填充变化会改变训练口径，本工具只报告不判故障。

用法（容器内）：
  python3 factor_drift.py --quantdb --market CN --dataset features_daily \
      --start 2026-06-01 --end 2026-10-07 --out /data/reports/factor-drift-monitor/cn_fd.json
  python3 factor_drift.py --quantdb --market HK --last-partitions 35
  python3 factor_drift.py --demo
本报告仅供本地研究使用；分析结论不构成投资建议（数据哨兵，不修改任何数据）。
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import os
import re
import sys
from array import array
from datetime import date as _date
from datetime import datetime, timedelta, timezone
from pathlib import Path

SKILL_NAME = "factor-drift-monitor"

# ------------------------------------------------------------------ 默认阈值（出处见 references/drift-methods.md）
DEFAULTS = {
    "psi_info": 0.10,               # 评分卡行业惯例 0.1/0.25；与源技能 watch/warning 线一致
    "psi_warn": 0.25,
    "ks_warn": 0.20,                # 源技能默认
    "mean_shift_warn": 1.0,         # σ 单位，源技能默认（>=1 sd）
    "missing_abs_warn": 0.20,       # 源技能默认：缺失率 >= 20%
    "missing_delta_warn": 0.10,     # 源技能默认：缺失率相对基线跳升 >= 10pp
    "missing_spike_critical": 0.30,  # 本地新增：单分区整列缺失（末日缺失率 - 基线 >= 30pp）
    "coverage_drop_warn": 0.05,     # 本地经验：CN 09-21 实测 -6.2% 标的数应报出
    "coverage_drop_critical": 0.20,
    "coverage_min_abs": 20,         # 标的绝对变化数低于此值不报（防小票池噪声）
    "stale_warn_partitions": 2,     # 因子最新分区落后行情 >= 2 个交易日
    "stale_critical_partitions": 5,
    "stale_calendar_days": 10,      # 无行情参照时的兜底：距今 > 10 个自然日
    "baseline_partitions": 20,      # 基线=窗口前 N 个分区（稳定期）
    "recent_partitions": 5,         # 对比=窗口后 N 个分区
    "sample_per_partition": 800,    # 每分区每列抽样上限（等距抽取，确定性）
    "max_sample": 20000,            # 每列每段样本总量上限（超出等距降采样）
    "min_sample": 200,              # 低于此样本量的分布统计标低置信度并降级为 info
    "max_dist_alerts": 15,          # 分布告警最多输出条数（其余计入 JSON 与汇总行）
}

# 分区内不作为统计对象的辅助列（仍参与列集增删对比）
SKIP_STATS = {"symbol", "date", "time", "release_id", "published_at"}

# 标签列模式（未来收益类；存在性/填充变化单独归类）
LABEL_COL_PATTERNS = [
    re.compile(r"^label_return$", re.IGNORECASE),
    re.compile(r"^return_\d+d$", re.IGNORECASE),
    re.compile(r"^future_return_\d+d$", re.IGNORECASE),
]

DATASET_PATHS = {
    ("CN", "features_daily"): "quantdb/6_ml_datasets/features_daily",
    ("CN", "l1_factors"): "quantdb/6_ml_datasets/l1_factors",
    ("CN", "l2_factors"): "quantdb/6_ml_datasets/l2_factors",
    ("HK", "l1_factors"): "quanthk/6_ml_datasets/l1_factors",
    ("US", "l1_factors"): "quantus/6_ml_datasets/l1_factors",
}
KLINE_PATHS = {
    "CN": "quantdb/1_kline_data/daily_forward",
    "HK": "quanthk/1_kline_data/daily_forward",
    "US": "quantus/1_kline_data/daily_forward",
}
DEFAULT_DATASET = {"CN": "features_daily", "HK": "l1_factors", "US": "l1_factors"}
DATA_ROOT_CANDIDATES = ["/data", "/quantmind/data", "/home/zbox/projects/quantmind/data"]

SEV_ORDER = {"critical": 0, "warning": 1, "info": 2}
CATEGORY_ORDER = ["staleness", "coverage", "integrity", "missing", "constant",
                  "column-set", "label", "distribution"]
CATEGORY_CN = {
    "staleness": "断更", "coverage": "覆盖", "integrity": "完整性",
    "missing": "缺失", "constant": "常量", "column-set": "列集",
    "label": "标签", "distribution": "分布",
}


def is_label_column(name: str) -> bool:
    return any(p.match(str(name).strip()) for p in LABEL_COL_PATTERNS)


# ------------------------------------------------------------------ 统计内核（纯标准库）

def _median(xs) -> float | None:
    arr = sorted(x for x in xs if x is not None)
    n = len(arr)
    if n == 0:
        return None
    m = n // 2
    return float(arr[m]) if n % 2 else (arr[m - 1] + arr[m]) / 2.0


def _mean(xs) -> float | None:
    arr = [x for x in xs if x is not None]
    return sum(arr) / len(arr) if arr else None


def _std(xs) -> float | None:
    """样本标准差（ddof=1）。"""
    arr = [x for x in xs if x is not None]
    n = len(arr)
    if n < 2:
        return None
    m = sum(arr) / n
    return math.sqrt(sum((x - m) ** 2 for x in arr) / (n - 1))


def _quantile_sorted(sorted_vals: list[float], q: float) -> float | None:
    """numpy percentile（linear 插值）同口径分位数。"""
    n = len(sorted_vals)
    if n == 0:
        return None
    if n == 1:
        return float(sorted_vals[0])
    pos = q * (n - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, n - 1)
    frac = pos - lo
    return float(sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac)


def psi(base_vals, cur_vals, bins: int = 10) -> float | None:
    """PSI（分箱按基线的分位数，首尾外扩 ±inf，与源技能同口径）。"""
    if len(base_vals) < bins * 2 or len(cur_vals) < bins * 2:
        return None
    sb = sorted(base_vals)
    edges = sorted({_quantile_sorted(sb, i / bins) for i in range(bins + 1)})
    if len(edges) < 3:               # 基线近常量，分箱退化
        return None
    interior = edges[1:-1]
    nb, nc = len(interior) + 1, len(interior) + 1
    ref = [0] * nb
    cur = [0] * nc
    for v in base_vals:
        ref[bisect.bisect_right(interior, v)] += 1
    for v in cur_vals:
        cur[bisect.bisect_right(interior, v)] += 1
    total = 0.0
    for r, c in zip(ref, cur, strict=False):
        pr = max(r / len(base_vals), 1e-6)
        pc = max(c / len(cur_vals), 1e-6)
        total += (pc - pr) * math.log(pc / pr)
    return total


def ks_distance(base_vals, cur_vals) -> float | None:
    """两样本 K-S 统计量（双指针单遍扫描）。"""
    if not base_vals or not cur_vals:
        return None
    a = sorted(base_vals)
    b = sorted(cur_vals)
    n, m = len(a), len(b)
    i = j = 0
    d = 0.0
    while i < n and j < m:
        if a[i] <= b[j]:
            i += 1
        else:
            j += 1
        d = max(d, abs(i / n - j / m))
    return max(d, abs(i / n - j / m))


def _stride_sample(values, cap: int) -> array:
    """等距抽样到 cap 个点（确定性）。"""
    if len(values) <= cap:
        return array("d", values)
    stride = math.ceil(len(values) / cap)
    return array("d", values[::stride])


def _decimate(sample: array, cap: int) -> array:
    if len(sample) <= cap:
        return sample
    stride = math.ceil(len(sample) / cap)
    return array("d", list(sample)[::stride])


def _dt_compact_to_iso(dt: str) -> str:
    return f"{dt[:4]}-{dt[4:6]}-{dt[6:]}"


def _parse_compact(dt: str) -> _date | None:
    try:
        return _date(int(dt[:4]), int(dt[4:6]), int(dt[6:8]))
    except (ValueError, IndexError):
        return None


# ------------------------------------------------------------------ 分区 IR（合成面板与 QuantDB 共用）

def _empty_partition(dt: str) -> dict:
    return {"dt": dt, "n_rows": 0, "symbols": 0, "dup_symbols": 0,
            "columns": [], "stats": {}, "empty": True, "date_mismatch": 0}


def profile_rows(rows: list[dict], dt: str, sample_cap: int = 800) -> dict:
    """由行字典构造分区 IR（纯标准库，合成面板路径）。"""
    if not rows:
        return _empty_partition(dt)
    cols: list[str] = []
    seen = set()
    for r in rows:
        for k in r:
            if k not in seen:
                seen.add(k)
                cols.append(k)
    cols = [c for c in cols if c not in SKIP_STATS]
    symbols = {str(r.get("symbol")) for r in rows}
    stats = {}
    for c in cols:
        vals = [r.get(c) for r in rows]
        present = [v for v in vals if v is not None]
        nums = [float(v) for v in present if isinstance(v, (int, float))]
        numeric = nums and len(nums) == len(present)
        st = {"nn": len(present), "nu": len(set(present)), "zero": None, "inf": None,
              "mean": None, "std": None, "min": None, "max": None, "sample": None}
        if numeric:
            finite = [v for v in nums if math.isfinite(v)]
            st["inf"] = len(nums) - len(finite)
            st["zero"] = sum(1 for v in finite if v == 0)
            st["mean"] = _mean(finite)
            st["std"] = _std(finite)
            st["min"] = min(finite) if finite else None
            st["max"] = max(finite) if finite else None
            st["sample"] = _stride_sample(finite, sample_cap)
        stats[c] = st
    return {"dt": dt, "n_rows": len(rows), "symbols": len(symbols),
            "dup_symbols": len(rows) - len(symbols), "columns": cols,
            "stats": stats, "empty": False, "date_mismatch": 0}


# ------------------------------------------------------------------ 检查引擎（纯标准库）

def _present_cols(part: dict, col: str) -> bool:
    return col in part["stats"]


def _missing_rate(part: dict, col: str) -> float | None:
    st = part["stats"].get(col)
    if st is None or part["n_rows"] == 0:
        return None
    return 1.0 - st["nn"] / part["n_rows"]


def _alert(severity: str, category: str, obj: str, baseline: str, current: str,
           hint: str, **detail) -> dict:
    return {"severity": severity, "category": category, "object": obj,
            "baseline": baseline, "current": current, "hint": hint, "detail": detail}


def _label_severity(cap: str, severity: str) -> str:
    """标签列的故障类告警降级为 warning（标签变化不一定是故障，但必须可见）。"""
    if severity == "critical":
        return "warning"
    return severity


def _analyze_staleness(parts: list[dict], scan: dict, cfg: dict, alerts: list) -> dict:
    last = parts[-1]["dt"]
    kline = sorted(scan.get("kline_dates") or [])
    out = {"last_partition": last, "kline_last": kline[-1] if kline else None,
           "kline_lag_partitions": None, "lag_vs_today_days": None}
    today = datetime.now(timezone.utc).date()
    d = _parse_compact(last)
    if d:
        out["lag_vs_today_days"] = (today - d).days
    if kline:
        lag = sum(1 for x in kline if x > last)
        out["kline_lag_partitions"] = lag
        if lag >= int(cfg["stale_critical_partitions"]):
            alerts.append(_alert(
                "critical", "staleness", f"{scan['market']}/{scan['dataset']}",
                f"行情分区至 {_dt_compact_to_iso(kline[-1])}",
                f"因子分区止于 {_dt_compact_to_iso(last)}（落后 {lag} 个行情分区）",
                "因子同步任务疑似中断或队列积压；先查数据同步调度、最近一次同步日志与下游写入"))
        elif lag >= int(cfg["stale_warn_partitions"]):
            alerts.append(_alert(
                "warning", "staleness", f"{scan['market']}/{scan['dataset']}",
                "行情分区已到最新", f"因子落后 {lag} 个行情分区（止于 {_dt_compact_to_iso(last)}）",
                "同步可能部分失败；核对当日同步日志"))
    elif out["lag_vs_today_days"] is not None and out["lag_vs_today_days"] > int(cfg["stale_calendar_days"]):
        alerts.append(_alert(
            "warning", "staleness", f"{scan['market']}/{scan['dataset']}",
            f"距今 > {cfg['stale_calendar_days']} 自然日",
            f"最新分区 {_dt_compact_to_iso(last)}（{out['lag_vs_today_days']} 天前，无行情参照）",
            "无行情分区可对照，节假日会误报；先确认行情数据集本身是否更新"))
    return out


def _analyze_partition_gaps(parts: list[dict], scan: dict, alerts: list) -> dict:
    dts = [p["dt"] for p in parts]
    dtset = set(dts)
    out = {"factor_partitions": len(parts), "kline_partitions_in_window": None,
           "missing_in_factor": [], "extra_vs_kline": [], "weekday_gaps_not_in_kline": []}
    if not dts:
        return out
    first, last = dts[0], dts[-1]
    kline = sorted(d for d in (scan.get("kline_dates") or []) if first <= d <= last)
    if kline:
        out["kline_partitions_in_window"] = len(kline)
        missing = [d for d in kline if d not in dtset]
        extra = [d for d in dts if d not in set(kline)]
        out["missing_in_factor"] = missing
        out["extra_vs_kline"] = extra
        if missing:
            show = ", ".join(_dt_compact_to_iso(d) for d in missing[:8])
            alerts.append(_alert(
                "warning", "coverage", f"{scan['market']}/{scan['dataset']} 缺分区",
                "行情窗口内逐日有分区", f"行情有而因子缺 {len(missing)} 个分区：{show}"
                + ("…" if len(missing) > 8 else ""),
                "单日分区整块缺失常见于当日同步失败；与行情对齐后补跑该日"))
        if extra:
            show = ", ".join(_dt_compact_to_iso(d) for d in extra[:8])
            alerts.append(_alert(
                "info", "coverage", f"{scan['market']}/{scan['dataset']} 多分区",
                "行情窗口内逐日有分区", f"因子有而行情无 {len(extra)} 个分区：{show}"
                + ("…" if len(extra) > 8 else ""),
                "因子侧存在行情侧没有的日期：口径错位或行情缺失，抽查该日数据"))
    # 工作日内两侧都缺的日期（多为节假日；仅提示，不告警）
    gaps = []
    cur = _parse_compact(first)
    end = _parse_compact(last)
    kset = set(kline)
    while cur and end and cur <= end:
        compact = cur.strftime("%Y%m%d")
        if cur.weekday() < 5 and compact not in dtset and compact not in kset:
            gaps.append(compact)
        cur += timedelta(days=1)
    out["weekday_gaps_not_in_kline"] = gaps
    return out


def _analyze_coverage(parts: list[dict], base_n: int, cfg: dict, alerts: list,
                      transition_dts: set) -> dict:
    base = [p for p in parts[:base_n] if not p["empty"]]
    base_med = _median([p["symbols"] for p in base]) if base else None
    out = {"baseline_median_symbols": base_med, "worst_drop": None, "affected": []}
    if not base_med or base_med <= 0:
        return out
    worst = None
    affected = []
    for p in parts[base_n:]:
        if p["empty"]:
            continue
        drop = (base_med - p["symbols"]) / base_med
        if drop > 0:
            affected.append({"dt": p["dt"], "drop": drop, "symbols": p["symbols"]})
            if worst is None or drop > worst["drop"]:
                worst = affected[-1]
    affected = [a for a in affected if a["drop"] >= float(cfg["coverage_drop_warn"])
                and (base_med - a["symbols"]) >= float(cfg["coverage_min_abs"])]
    if not affected:
        return out
    worst = max(affected, key=lambda a: a["drop"])
    sev = "critical" if worst["drop"] >= float(cfg["coverage_drop_critical"]) else "warning"
    coincident = sorted(transition_dts & {a["dt"] for a in affected})
    hint = "标的数骤降：退市/新股/过滤口径变更（如剔除 ST、停牌）"
    if coincident:
        hint += "；与同日列集切换同时出现，疑为上游改版（详见列集告警）"
    alerts.append(_alert(
        sev, "coverage", f"{worst['dt']} 起 {len(affected)} 个分区",
        f"基线标的中位数 {base_med:.0f}",
        f"最差 {worst['dt']}：{worst['symbols']}（-{worst['drop']:.1%}）",
        hint, affected_partitions=[a["dt"] for a in affected[:10]],
        affected_count=len(affected),
        coincident_transitions=coincident))
    out.update({"worst_drop": worst["drop"], "affected": affected[:20]})
    return out


def _analyze_missing(parts: list[dict], base: list[dict], recent: list[dict],
                     cfg: dict, alerts: list) -> list[dict]:
    all_cols: list[str] = []
    seen = set()
    for p in recent:
        for c in p["columns"]:
            if c not in seen and c not in SKIP_STATS:
                seen.add(c)
                all_cols.append(c)
    rows = []
    sparse_static: list[str] = []
    for col in all_cols:
        is_label = is_label_column(col)
        base_rates = [r for r in (_missing_rate(p, col) for p in base) if r is not None]
        recent_rates = [r for r in (_missing_rate(p, col) for p in recent) if r is not None]
        if not recent_rates:
            continue
        recent_med = _median(recent_rates)
        last_part = next((p for p in reversed(recent) if _present_cols(p, col)), None)
        last_rate = _missing_rate(last_part, col) if last_part else None
        entry = {"column": col, "is_label": is_label,
                 "baseline_missing": _median(base_rates) if base_rates else None,
                 "recent_missing": recent_med, "last_missing": last_rate}
        if not base_rates:
            if recent_med is not None and recent_med >= 0.5 and not is_label:
                alerts.append(_alert(
                    "info", "missing", col,
                    "基线窗口无此列", f"新增列近期缺失率 {recent_med:.1%}",
                    "上游新增列但填充不足；若下游要用先确认填充口径"))
            # 标签列的新增填充情况由 _analyze_labels 的时间线与回填前沿统一报告
            rows.append(entry)
            continue
        base_med = _median(base_rates)
        delta = recent_med - base_med if (recent_med is not None and base_med is not None) else None
        delta_warn = float(cfg["missing_delta_warn"])
        if (base_med or 0) >= 0.5 and (delta is None or delta < delta_warn):
            # 基线即高度缺失（结构稀疏：行业聚合列、未接入列…）且未恶化：属静态状态
            # 而非漂移，聚合为一条 info（见下），避免每次运行刷屏
            sparse_static.append(col)
            rows.append(entry)
            continue
        severity = None
        reason = None
        if (delta is not None and delta >= delta_warn) or (
                recent_med is not None and recent_med >= float(cfg["missing_abs_warn"])):
            stopped = recent_med >= 0.9 and (base_med or 0) <= 0.1
            severity = "critical" if stopped else "warning"
            if stopped:
                reason = "整列停填（近期几乎全空、基线基本全满）"
            elif delta is not None and delta >= delta_warn:
                reason = f"缺失率较基线上升 {delta:.1%}"
            else:
                reason = "缺失率绝对值超阈值（基线即偏高且未改善）"
        if last_rate is not None and base_med is not None and \
                last_rate - base_med >= float(cfg["missing_spike_critical"]):
            if severity != "critical":
                severity, reason = "critical", "最新分区整列缺失（单分区尖峰）"
        if severity:
            if is_label:
                severity = _label_severity(severity, severity)
            cur_txt = f"近期中位 {recent_med:.1%}" + (
                f"（最新 {last_rate:.1%}）" if last_rate is not None else "") + f" · {reason}"
            alerts.append(_alert(
                severity, "label" if is_label else "missing", col,
                f"基线缺失率 {base_med:.1%}", cur_txt,
                ("标签列填充变化会改变训练口径（不影响实时信号）；" if is_label else "")
                + "常见原因：上游按日重算失败、字段源失效、写 0 占位；先查该列最近一次成功填充的分区",
                reason=reason))
        rows.append(entry)
    if sparse_static:
        show = ", ".join(sparse_static[:12]) + ("…" if len(sparse_static) > 12 else "")
        alerts.append(_alert(
            "info", "missing", "结构性稀疏列（基线即缺）",
            "—", f"{len(sparse_static)} 列基线缺失率 ≥50% 且未较基线恶化 ≥10pp：{show}",
            "基线即缺的静态状态（非漂移）：多为行业聚合列/该市场未接入列；下游若要用先确认填充口径",
            columns=sparse_static))
    return rows


def _analyze_constants(parts: list[dict], base: list[dict], recent: list[dict],
                       cfg: dict, alerts: list) -> dict:
    def is_const(sts: list[dict]) -> bool:
        return bool(sts) and all(s["nu"] <= 1 and s["nn"] > 0 for s in sts)

    recent_cols = [c for c in recent[-1]["columns"] if c not in SKIP_STATS] if recent else []
    base_const, new_const, unfrozen = [], [], []
    for col in recent_cols:
        base_sts = [p["stats"][col] for p in base if col in p["stats"]]
        recent_sts = [p["stats"][col] for p in recent if col in p["stats"]]
        if not recent_sts:
            continue
        recent_const = len(recent_sts) == len(recent) and is_const(recent_sts)
        base_const_flag = len(base_sts) >= max(2, len(base) // 2) and is_const(base_sts)
        is_label = is_label_column(col)
        if recent_const and not base_const_flag:
            sev = "warning" if is_label else "critical"
            if base_sts:
                nu_max = max((s["nu"] for s in base_sts if s["nn"] > 0), default=0)
                if nu_max >= 2:
                    base_text = f"基线分区 nunique 最高 {nu_max}（存在变化）"
                else:
                    base_text = "基线分区部分缺失且 nunique≤1（非稳定常量）"
                cause = "常见原因：填充管道写 0/占位值、字段源失效；核对最近一次正常分区的取值"
            else:
                base_text = "基线窗口无此列（窗口内新增）"
                cause = ("该列在窗口内新增且自引入起即为常量：新增标志位（is_* 类）"
                         "全 0/全 1 常属预期，先确认语义与填充口径")
            alerts.append(_alert(
                sev, "label" if is_label else "constant", col, base_text,
                "最近分区 nunique=1（常量）",
                ("标签列在最近分区变为常量；" if is_label else "非标签列在最近分区变为常量；")
                + cause,
                nunique=recent_sts[-1]["nu"], non_null=recent_sts[-1]["nn"]))
            new_const.append(col)
        elif recent_const and base_const_flag:
            base_const.append(col)
        elif base_const_flag and not recent_const:
            unfrozen.append(col)
    if base_const:
        show = ", ".join(base_const[:12]) + ("…" if len(base_const) > 12 else "")
        alerts.append(_alert(
            "info", "constant", "基线期常量列",
            "—", f"{len(base_const)} 列两段均恒为同一值：{show}",
            "基线即存在的常量列（未填充/固定值），不计为告警；下游因子若含这些列属预期死重",
            columns=base_const))
    if unfrozen:
        show = ", ".join(unfrozen[:12]) + ("…" if len(unfrozen) > 12 else "")
        alerts.append(_alert(
            "info", "constant", "常量列恢复变化",
            "基线期常量", f"{len(unfrozen)} 列在最近分区出现变化：{show}",
            "原常量列开始填充或取值变化：可能是填充管道修复，也可能是新数据源接入"))
    return {"baseline_constants": base_const, "new_constants": new_const, "unfrozen": unfrozen}


def _split_label_cols(cols: list[str]) -> tuple[list[str], list[str]]:
    labels = [c for c in cols if is_label_column(c)]
    others = [c for c in cols if not is_label_column(c)]
    return labels, others


def _analyze_column_set(parts: list[dict], cfg: dict, alerts: list) -> dict:
    allow_removed = set(cfg.get("allow_removed") or [])
    allow_added = set(cfg.get("allow_added") or [])
    non_empty = [p for p in parts if not p["empty"]]
    transitions = []
    prev = None
    for p in non_empty:
        if prev is not None:
            old_dt, old_cols = prev
            new_set = set(p["columns"])
            old_set = set(old_cols)
            removed = [c for c in old_cols if c not in new_set]
            added = [c for c in p["columns"] if c not in old_set]
            order_only = not removed and not added and old_cols != p["columns"]
            if removed or added or order_only:
                transitions.append({"from_dt": old_dt, "to_dt": p["dt"], "removed": removed,
                                    "added": added, "order_only": order_only})
        prev = (p["dt"], p["columns"])

    for tr in transitions:
        ev = f"{_dt_compact_to_iso(tr['from_dt'])}→{_dt_compact_to_iso(tr['to_dt'])}"
        rem_labels, rem_others = _split_label_cols(tr["removed"])
        add_labels, add_others = _split_label_cols(tr["added"])
        rem_eff = [c for c in rem_others if c not in allow_removed]
        add_eff = [c for c in add_others if c not in allow_added]
        if rem_eff or rem_others or tr["order_only"]:
            if rem_eff:
                sev = "critical"
                cur = f"移除 {len(rem_eff)} 列" + (
                    f"（另有 {len(rem_others) - len(rem_eff)} 列已白名单）" if rem_eff != rem_others else "")
                hint = "上游 schema 变更：下游按列名读取会报缺列，按列位置对齐的读取会静默错位"
            elif rem_others:
                sev = "info"
                cur = f"移除 {len(rem_others)} 列（全部已白名单：{', '.join(rem_others[:8])}）"
                hint = "已知断点（白名单命中）降为提示：复核 references/known-breakpoints.md 后保留监控"
            else:
                sev = "info"
                cur = "移除 0 列"
                hint = "仅列序变化（集合不变）：按位置对齐的读取会静默错位"
            alerts.append(_alert(
                sev, "column-set", f"分区切换 {ev}", "上一分区列集", cur, hint,
                removed=rem_eff or rem_others, added_top=add_eff[:20],
                order_only=tr["order_only"],
                columns_added_count=len(add_eff)))
        if rem_labels or add_labels:
            tag = ""
            if rem_labels and add_labels:
                pairs = {c: f"future_{c}" for c in rem_labels}
                if all(pairs.get(c) in add_labels for c in rem_labels):
                    tag = "（疑似整体改名：return_Nd → future_return_Nd）"
            eff = [c for c in rem_labels if c not in allow_removed]
            sev = "warning" if eff else "info"
            alerts.append(_alert(
                sev, "label", f"分区切换 {ev}",
                "上一分区标签列集", f"移除 {rem_labels}；新增 {add_labels}",
                f"标签列存在性变化{tag}；会改变训练口径（旧名标签下游训练样本会全空）",
                removed=eff, added=add_labels))
        if add_eff and not rem_others and not rem_labels:
            alerts.append(_alert(
                "info", "column-set", f"分区切换 {ev}", "上一分区列集",
                f"新增 {len(add_eff)} 列：{', '.join(add_eff[:12])}" + ("…" if len(add_eff) > 12 else ""),
                "上游新增列通常无害；但新增列没有基线样本，本次不做分布对比", added=add_eff))
        elif add_others and not add_eff and not rem_others and not rem_labels:
            alerts.append(_alert(
                "info", "column-set", f"分区切换 {ev}", "上一分区列集",
                f"新增 {len(add_others)} 列（全部已白名单：{', '.join(add_others[:8])}）",
                "已知断点（白名单命中）降为提示：复核 references/known-breakpoints.md 后保留监控",
                added=add_others))
    base_cols = non_empty[0]["columns"] if non_empty else []
    last_cols = non_empty[-1]["columns"] if non_empty else []
    base_set, last_set = set(base_cols), set(last_cols)
    out = {
        "baseline_n_cols": len(base_cols), "last_n_cols": len(last_cols),
        "removed_vs_baseline": [c for c in base_cols if c not in last_set],
        "added_vs_baseline": [c for c in last_cols if c not in base_set],
        "transitions": transitions,
    }
    return out


def _analyze_labels(parts: list[dict], alerts: list) -> list[dict]:
    timeline = []
    stale_frontier = []
    cols: list[str] = []
    seen = set()
    for p in parts:
        for c in p["columns"]:
            if c not in seen:
                seen.add(c)
                cols.append(c)
    for col in cols:
        if not is_label_column(col):
            continue
        present = [p for p in parts if col in p["stats"] and not p["empty"]]
        if not present:
            continue
        fills = [_missing_rate(p, col) for p in present]
        fill_rate = [1.0 - f for f in fills if f is not None]
        entry = {
            "column": col, "first_dt": present[0]["dt"], "last_dt": present[-1]["dt"],
            "present_partitions": len(present),
            "median_fill": _median(fill_rate),
            "fill_min": min(fill_rate) if fill_rate else None,
            "fill_max": max(fill_rate) if fill_rate else None,
        }
        # 回填前沿：最后出现分区是否已填充（≥50% 视为已填充）；前沿停滞=标签重算任务疑似停跑
        filled_idx = [i for i, f in enumerate(fill_rate) if f >= 0.5]
        last_present = present[-1]["dt"]
        if filled_idx and present[filled_idx[-1]]["dt"] != last_present:
            entry["last_filled_dt"] = present[filled_idx[-1]]["dt"]
            entry["unfilled_tail_partitions"] = len(present) - 1 - filled_idx[-1]
            stale_frontier.append((col, present[filled_idx[-1]]["dt"], last_present))
        elif not filled_idx:
            entry["last_filled_dt"] = None
            entry["unfilled_tail_partitions"] = len(present)
            stale_frontier.append((col, None, last_present))
        else:
            entry["last_filled_dt"] = last_present
            entry["unfilled_tail_partitions"] = 0
        timeline.append(entry)
    if stale_frontier:
        txt = "；".join(
            f"{c}: 末次填充 {_dt_compact_to_iso(d) if d else '从未'}"
            f"（末分区 {_dt_compact_to_iso(last)}）"
            for c, d, last in stale_frontier[:8])
        alerts.append(_alert(
            "info", "label", "标签回填前沿",
            "标签列在出现期内应持续回填（≥50% 填充）",
            f"{len(stale_frontier)} 个标签列尾部未填充：{txt}",
            "回填前沿=最近一次成功回填的日期；前沿停滞通常意味标签重算任务停跑。"
            "标签只影响训练口径，不影响实时信号"))
    return timeline


def _analyze_distribution(base: list[dict], recent: list[dict], cfg: dict,
                          alerts: list) -> list[dict]:
    cols: list[str] = []
    seen = set()
    for p in base + recent:
        for c in p["columns"]:
            if c not in seen and c not in SKIP_STATS:
                seen.add(c)
                cols.append(c)
    results = []
    for col in cols:
        base_samples = [p["stats"][col]["sample"] for p in base
                        if col in p["stats"] and p["stats"][col]["sample"]]
        cur_samples = [p["stats"][col]["sample"] for p in recent
                       if col in p["stats"] and p["stats"][col]["sample"]]
        if not base_samples or not cur_samples:
            continue
        b = array("d")
        for s in base_samples:
            b.extend(s)
        c = array("d")
        for s in cur_samples:
            c.extend(s)
        b = _decimate(b, int(cfg["max_sample"]))
        c = _decimate(c, int(cfg["max_sample"]))
        if len(b) < 20 or len(c) < 20:
            continue
        base_stats = [p["stats"][col] for p in base if col in p["stats"]]
        base_mean = _median([s["mean"] for s in base_stats if s["mean"] is not None])
        base_std = _median([s["std"] for s in base_stats if s["std"] is not None])
        cur_stats = [p["stats"][col] for p in recent if col in p["stats"]]
        cur_mean = _median([s["mean"] for s in cur_stats if s["mean"] is not None])
        cur_std = _median([s["std"] for s in cur_stats if s["std"] is not None])
        shift = None
        if base_mean is not None and cur_mean is not None and base_std and base_std > 0:
            shift = (cur_mean - base_mean) / base_std
        p = psi(b, c)
        k = ks_distance(b, c)
        std_ratio = (cur_std / base_std) if (cur_std is not None and base_std) else None
        low_conf = len(b) < int(cfg["min_sample"]) or len(c) < int(cfg["min_sample"])
        severity = None
        if p is not None and p >= float(cfg["psi_warn"]):
            severity = "warning"
        if k is not None and k >= float(cfg["ks_warn"]):
            severity = "warning"
        if shift is not None and abs(shift) >= float(cfg["mean_shift_warn"]):
            severity = "warning"
        if severity is None and p is not None and p >= float(cfg["psi_info"]):
            severity = "info"
        if severity is None:
            continue
        if low_conf:
            severity = "info"
        results.append({"column": col, "psi": p, "ks": k, "mean_shift_sigma": shift,
                        "std_ratio": std_ratio, "n_base": len(b), "n_cur": len(c),
                        "low_confidence": low_conf, "severity": severity,
                        "is_label": is_label_column(col)})
    results.sort(key=lambda r: (-(r["psi"] or 0.0), r["column"]))
    shown = results[:int(cfg["max_dist_alerts"])]
    for r in shown:
        parts_bits = []
        if r["psi"] is not None:
            parts_bits.append(f"PSI={r['psi']:.3f}")
        if r["ks"] is not None:
            parts_bits.append(f"KS={r['ks']:.3f}")
        if r["mean_shift_sigma"] is not None:
            parts_bits.append(f"均值平移={r['mean_shift_sigma']:+.2f}σ")
        if r["std_ratio"] is not None:
            parts_bits.append(f"σ比={r['std_ratio']:.2f}")
        alerts.append(_alert(
            r["severity"], "distribution", r["column"],
            f"基线样本 {r['n_base']}",
            "，".join(parts_bits) + ("（低置信度）" if r["low_confidence"] else ""),
            "分布漂移可能是数据故障，也可能是真实市场状态变化；结合同窗口行情走势判读。"
            "价格水平类列（close/ma*/市值）在趋势行情中必然漂移，属预期",
            psi=r["psi"], ks=r["ks"], mean_shift_sigma=r["mean_shift_sigma"]))
    return results


def analyze(scan: dict, cfg: dict) -> dict:
    """对扫描结果执行四类检查，返回完整报告（纯标准库）。"""
    parts = sorted(scan["partitions"], key=lambda p: p["dt"])
    if len(parts) < 4:
        raise ValueError(f"分区数过少（{len(parts)} < 4），无法划分基线/对比窗口")
    alerts: list[dict] = []
    base_n = max(1, min(int(cfg["baseline_partitions"]), len(parts) - 1))
    r_n = max(1, min(int(cfg["recent_partitions"]), len(parts) - base_n))
    base = parts[:base_n]
    recent = parts[-r_n:]

    # ① 结构完整性 / 断更 / 分区连续性
    empties = [p["dt"] for p in parts if p["empty"]]
    if empties:
        alerts.append(_alert(
            "critical", "integrity", "空分区",
            "分区应含数据文件且非 0 行", f"{len(empties)} 个空分区：{empties[:8]}",
            "分区目录存在但数据文件缺失/0 行：当日写入失败或被清空；立即查同步日志"))
    dups = [(p["dt"], p["dup_symbols"]) for p in parts if p["dup_symbols"] > 0]
    if dups:
        alerts.append(_alert(
            "warning", "integrity", "重复主键",
            "每分区 (symbol) 唯一", f"{len(dups)} 个分区存在重复行，如 {dups[:4]}",
            "同一分区内同一标的多行：上游合并去重失效，聚合统计会被放大"))
    mism = [(p["dt"], p["date_mismatch"]) for p in parts if p.get("date_mismatch")]
    if mism:
        alerts.append(_alert(
            "warning", "integrity", "日期列与分区名不一致",
            "日期列值应等于分区 dt", f"{len(mism)} 个分区不一致，如 {mism[:4]}",
            "分区名与行内日期脱钩：按分区日期合并的管线会错位"))
    weekend = [p["dt"] for p in parts if not p["empty"]
               and (_parse_compact(p["dt"]) or _date(2000, 1, 1)).weekday() >= 5]
    if weekend:
        alerts.append(_alert(
            "info", "integrity", "周末分区", "交易日应为周一至周五",
            f"{len(weekend)} 个周末分区：{weekend[:6]}",
            "周末出现数据分区：核对是否补跑产物或口径异常"))
    inf_hits = [(p["dt"], c, s["inf"]) for p in recent
                for c, s in p["stats"].items() if s.get("inf")]
    if inf_hits:
        inf_cols = sorted({c for _, c, _ in inf_hits})
        alerts.append(_alert(
            "warning", "integrity", "非有限值（inf）",
            "数值列应为有限值（NaN=缺失另计）",
            f"{len(inf_hits)} 处 inf，涉及 {len(inf_cols)} 列：{inf_cols[:6]}"
            f"（首见 {inf_hits[0][0]}，如 {inf_hits[0][1]} 当日 {inf_hits[0][2]} 个）",
            "inf 污染均值/标准差/PSI 并会带进训练；先查该列上游公式是否除零，再决定过滤或修源",
            columns=inf_cols, cells=len(inf_hits)))

    staleness = _analyze_staleness(parts, scan, cfg, alerts)
    gaps = _analyze_partition_gaps(parts, scan, alerts)

    # ③ 列集（先算，供覆盖告警引用同日切换）
    schema = _analyze_column_set(parts, cfg, alerts)
    transition_dts = {t["to_dt"] for t in schema["transitions"]}

    # ① 覆盖
    coverage = _analyze_coverage(parts, base_n, cfg, alerts, transition_dts)

    # ② 缺失 / 常量
    missing_rows = _analyze_missing(parts, base, recent, cfg, alerts)
    constants = _analyze_constants(parts, base, recent, cfg, alerts)
    labels = _analyze_labels(parts, alerts)

    # ④ 分布
    distribution = _analyze_distribution(base, recent, cfg, alerts)

    for a in alerts:
        a["_sort"] = (SEV_ORDER[a["severity"]],
                      CATEGORY_ORDER.index(a["category"]) if a["category"] in CATEGORY_ORDER else 99,
                      -(a["detail"].get("psi") or 0.0) if a["category"] == "distribution" else 0,
                      a["object"])
    alerts.sort(key=lambda a: a["_sort"])
    for a in alerts:
        a.pop("_sort", None)

    non_empty = [p for p in parts if not p["empty"]]
    profile = {
        "partitions": len(parts),
        "empty_partitions": empties,
        "rows_min": min((p["n_rows"] for p in non_empty), default=0),
        "rows_max": max((p["n_rows"] for p in non_empty), default=0),
        "symbols_min": min((p["symbols"] for p in non_empty), default=0),
        "symbols_max": max((p["symbols"] for p in non_empty), default=0),
        "dup_symbol_rows": sum(p["dup_symbols"] for p in parts),
        "inf_cells": sum(s["inf"] or 0 for p in parts for s in p["stats"].values()),
    }
    summary = {"critical": 0, "warning": 0, "info": 0}
    for a in alerts:
        summary[a["severity"]] += 1
    dist_extra = max(0, len(distribution) - len([a for a in alerts if a["category"] == "distribution"]))
    caveats = [
        f"基线=前 {base_n} 个分区（{_dt_compact_to_iso(base[0]['dt'])}~{_dt_compact_to_iso(base[-1]['dt'])}），"
        f"对比=后 {r_n} 个分区（{_dt_compact_to_iso(recent[0]['dt'])}~{_dt_compact_to_iso(recent[-1]['dt'])}）；"
        "基线应取稳定期，若基线内已有断点会把基线本身拉偏",
        (f"分布统计为等距抽样（每分区每列≤{cfg['sample_per_partition']}、"
         f"每段≤{cfg['max_sample']} 个点），小票池/小样本结论自动降级为低置信度"),
        "行情分区只用于断更/缺分区/节假日判读，不读行情数值；节假日（如国庆/中秋）会表现为工作日缺口并按 info 提示",
        "标签列（label_return/return_Nd/future_return_Nd）变化只报不提级：标签变化改变训练口径，不影响实时信号",
        "分布漂移不区分「数据故障」与「真实市场变化」；价格水平类列在趋势行情中必然漂移，属预期",
        "本报告仅供本地研究使用；分析结论不构成投资建议（数据哨兵，不修改任何数据）",
    ]
    return {
        "skill": SKILL_NAME,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mode": scan.get("mode", "quantdb"),
        "market": scan.get("market"), "dataset": scan.get("dataset"),
        "data_root": scan.get("data_root"), "factor_path": scan.get("factor_path"),
        "kline_path": scan.get("kline_path"),
        "window": {"start": _dt_compact_to_iso(parts[0]["dt"]),
                   "end": _dt_compact_to_iso(parts[-1]["dt"]),
                   "first_partition": parts[0]["dt"], "last_partition": parts[-1]["dt"]},
        "config": dict(cfg),
        "profile": profile, "staleness": staleness, "partition_gaps": gaps,
        "coverage": coverage, "schema": schema, "constants": constants,
        "labels": labels, "missing_columns": missing_rows,
        "distribution": distribution, "distribution_not_shown": dist_extra,
        "alerts": alerts, "summary": summary, "caveats": caveats,
    }


# ------------------------------------------------------------------ 渲染

def _fmt(v, dp: int = 3, signed: bool = False) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    return f"{v:+.{dp}f}" if signed else f"{v:.{dp}f}"


def render_text(rep: dict) -> str:
    lines = []
    title = f"{rep['market']}/{rep['dataset']}" if rep.get("market") else str(rep.get("dataset"))
    lines.append(f"[{SKILL_NAME}] {title}")
    lines.append("=" * 72)
    w = rep["window"]
    pf = rep["profile"]
    lines.append(f"扫描窗口 {w['start']} ~ {w['end']} · 分区 {pf['partitions']}"
                 f" · 行 {pf['rows_min']}~{pf['rows_max']} · 标的 {pf['symbols_min']}~{pf['symbols_max']}")
    if pf["empty_partitions"]:
        lines.append(f"⚠ 空分区 {len(pf['empty_partitions'])} 个：{pf['empty_partitions'][:6]}")
    st = rep["staleness"]
    if st.get("kline_last"):
        lines.append(f"行情参照至 {_dt_compact_to_iso(st['kline_last'])}"
                     f"（落后 {st['kline_lag_partitions']} 个行情分区；最新分区距今 {st['lag_vs_today_days']} 天）")
    sch = rep["schema"]
    n_tr = len(sch["transitions"])
    lines.append(f"列集：基线 {sch['baseline_n_cols']} 列 → 最新 {sch['last_n_cols']} 列"
                 f"（相对基线 +{len(sch['added_vs_baseline'])}/−{len(sch['removed_vs_baseline'])}；"
                 f"窗口内切换 {n_tr} 次）")
    if sch["transitions"]:
        for t in sch["transitions"][:8]:
            bits = []
            if t["removed"]:
                bits.append("−" + ",".join(t["removed"][:6]) + ("…" if len(t["removed"]) > 6 else ""))
            if t["added"]:
                bits.append("+" + ",".join(t["added"][:6]) + ("…" if len(t["added"]) > 6 else ""))
            if t["order_only"]:
                bits.append("仅列序变化")
            lines.append(f"  切换 {_dt_compact_to_iso(t['from_dt'])}→{_dt_compact_to_iso(t['to_dt'])}："
                         + "；".join(bits))
    cs = rep.get("constants", {})
    if cs.get("baseline_constants"):
        cols = cs["baseline_constants"]
        lines.append(f"基线常量列 {len(cols)} 个：{', '.join(cols[:10])}" + ("…" if len(cols) > 10 else ""))
    gaps = rep["partition_gaps"]
    if gaps.get("weekday_gaps_not_in_kline"):
        g = ", ".join(_dt_compact_to_iso(d) for d in gaps["weekday_gaps_not_in_kline"][:8])
        lines.append(f"工作日缺口（两侧均无分区，疑为节假日）：{g}")
    lines.append("")
    labels = rep.get("labels") or []
    if labels:
        lines.append("【标签列时间线】")
        for lb in labels:
            tail = ""
            lf = lb.get("last_filled_dt")
            if lf != lb["last_dt"]:
                tail = f"  末次填充 {_dt_compact_to_iso(lf) if lf else '从未'}"
            lines.append(f"  {lb['column']:<18} {_dt_compact_to_iso(lb['first_dt'])}~{_dt_compact_to_iso(lb['last_dt'])}"
                         f"  出现 {lb['present_partitions']} 个分区  中位填充 {_fmt((lb['median_fill'] or 0) * 100, 1)}%{tail}")
        lines.append("")
    lines.append(f"【告警】按严重度排序（critical {rep['summary']['critical']} · "
                 f"warning {rep['summary']['warning']} · info {rep['summary']['info']}）")
    if not rep["alerts"]:
        lines.append("  （无告警，面板在两个窗口间无结构性或统计性漂移）")
    for a in rep["alerts"]:
        lines.append(f"  [{a['severity']}] {CATEGORY_CN.get(a['category'], a['category'])} · {a['object']}")
        lines.append(f"      基线：{a['baseline']}")
        lines.append(f"      当前：{a['current']}")
        lines.append(f"      提示：{a['hint']}")
    dist = rep.get("distribution") or []
    if dist:
        lines.append("")
        lines.append("【分布漂移明细】top（按 PSI 降序；完整列表见 JSON）")
        lines.append(f"  {'列':<24}{'PSI':>8}{'KS':>8}{'平移σ':>9}{'σ比':>7}  置信")
        max_rows = int(rep.get("config", {}).get("max_dist_alerts", 15))
        for r in dist[:max_rows]:
            lines.append(f"  {r['column']:<24}{_fmt(r['psi']):>8}{_fmt(r['ks']):>8}"
                         f"{_fmt(r['mean_shift_sigma'], 2, signed=True):>9}{_fmt(r['std_ratio'], 2):>7}"
                         f"  {'低' if r['low_confidence'] else '标准'}")
        extra = rep.get("distribution_not_shown") or 0
        if extra:
            lines.append(f"  （另有 {extra} 列越过 info 线未在表格展示）")
    lines.append("")
    lines.append("说明：")
    for c in rep["caveats"]:
        lines.append(f"  - {c}")
    return "\n".join(lines)


def json_safe(obj):
    if isinstance(obj, dict):
        return {k: json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [json_safe(v) for v in obj]
    if isinstance(obj, array):
        return [json_safe(v) for v in obj]
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    return obj


# ------------------------------------------------------------------ 合成面板（--demo，纯标准库）

DEMO_COLS = [
    ("close", 100.0, 5.0),
    ("mom_ret_20d", 0.02, 0.05),
    ("rsi_14", 50.0, 8.0),
    ("vol_std_20", 1.00, 0.25),
    ("turn_1", 2.50, 0.60),
    ("amt_log", 10.0, 0.40),
    ("chip_conc_20", 0.40, 0.08),
    ("return_5d", 0.01, 0.03),      # 标签列：全程稳定填充
]
DEMO_START = _date(2025, 1, 6)
DEMO_PARTITIONS = 31
DEMO_SYMBOLS = 50
DEMO_INJECT_FROM = 26                # 最后 5 个分区注入
DEMO_SHIFT_SIGMA = 2.0               # 注入④：均值平移 +2σ


def _demo_dates() -> list[str]:
    out, cur = [], DEMO_START
    while len(out) < DEMO_PARTITIONS:
        if cur.weekday() < 5:
            out.append(cur.strftime("%Y%m%d"))
        cur += timedelta(days=1)
    return out


def build_demo_scan(inject: bool) -> dict:
    """确定性合成面板：干净版或注入四种故障版（注入只影响最后 5 个分区）。"""
    import random  # noqa: PLC0415  （纯标准库；--demo 模式专用）
    rng = random.Random(20261008)
    dts = _demo_dates()
    partitions = []
    for idx, dt in enumerate(dts):
        rows = []
        for s in range(DEMO_SYMBOLS):
            row = {"symbol": f"SYN{s:03d}"}
            for name, mean, sigma in DEMO_COLS:
                row[name] = mean + sigma * rng.gauss(0.0, 1.0)
            if inject and idx >= DEMO_INJECT_FROM:
                # 注入②：某列变为常量
                row["rsi_14"] = 0.0
                # 注入④：分布均值平移 +2σ
                row["vol_std_20"] += DEMO_SHIFT_SIGMA * 0.25
                # 注入③：列集减少一列（key 不存在 → 该分区无此列）
                row.pop("chip_conc_20")
                # 注入①：末日分区整列缺失（仅在最后一个分区）
                if idx == len(dts) - 1:
                    row["amt_log"] = None
            rows.append(row)
        partitions.append(profile_rows(rows, dt, sample_cap=800))
    return {"mode": "demo", "market": "SYNTH", "dataset": "synthetic_panel",
            "data_root": "(builtin)", "factor_path": "(builtin)", "kline_path": None,
            "partitions": partitions, "kline_dates": dts}


DEMO_CFG = {"baseline_partitions": 20, "recent_partitions": 5}


def run_demo(out_path: str | None = None) -> int:
    cfg = {**DEFAULTS, **DEMO_CFG}
    clean = analyze(build_demo_scan(inject=False), dict(cfg))
    injected = analyze(build_demo_scan(inject=True), dict(cfg))
    print("== 干净面板（未注入）==")
    print(render_text(clean))
    print()
    print("== 注入面板 == 四种注入的期望告警")
    expect = [
        ("missing", "amt_log", "critical", "①末日分区整列缺失"),
        ("constant", "rsi_14", "critical", "②某列变为常量"),
        ("column-set", "chip_conc_20", "critical", "③列集减少一列"),
        ("distribution", "vol_std_20", "warning", "④分布均值平移 +2σ"),
    ]
    ok = True
    for category, obj, sev, desc in expect:
        hit = [a for a in injected["alerts"] if a["category"] == category
               and (a["object"] == obj or obj in str(a.get("detail", {})))]
        good = bool(hit) and min(SEV_ORDER[a["severity"]] for a in hit) <= SEV_ORDER[sev]
        status = "PASS" if good else "FAIL"
        ok = ok and good
        got = f"{hit[0]['severity']} · {hit[0]['current'][:48]}" if hit else "未触发"
        print(f"  [{status}] {desc}：期望 {category}/{obj} ≥{sev} → 实际 {got}")
    clean_bad = [a for a in clean["alerts"] if a["severity"] in ("critical", "warning")]
    status = "PASS" if not clean_bad else "FAIL"
    ok = ok and not clean_bad
    print(f"  [{status}] 干净面板零 critical/零 warning → "
          f"实际 critical={clean['summary']['critical']} warning={clean['summary']['warning']} "
          f"info={clean['summary']['info']}")
    for a in clean_bad:
        print(f"      误报：{a['severity']} {a['category']} {a['object']} · {a['current']}")
    print()
    print("== 注入面板完整报告 ==")
    print(render_text(injected))
    if out_path:
        p = Path(out_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(json_safe({"clean": clean, "injected": injected}),
                                ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n已写入 JSON：{p}")
    print(f"\n断言汇总：{'全部通过' if ok else '存在失败'}")
    return 0 if ok else 1


# ------------------------------------------------------------------ QuantDB 扫描（容器内 pandas/pyarrow）

def resolve_data_root() -> Path:
    env = os.environ.get("QM_DATA_ROOT")
    cands = ([Path(env)] if env else []) + [Path(p) for p in DATA_ROOT_CANDIDATES]
    for c in cands:
        if (c / "quantdb").is_dir():
            return c
    raise SystemExit(
        "找不到 QuantDB 数据根目录（需含 quantdb/ 子目录）："
        f"检查 {[str(c) for c in cands]}，或设置 QM_DATA_ROOT"
    )


def _profile_partition_parquet(f: Path, dt: str, cfg: dict, want_cols=None) -> dict:
    import pandas as pd              # noqa: PLC0415
    import pyarrow.parquet as pq     # noqa: PLC0415

    try:
        meta = pq.ParquetFile(f).metadata
    except FileNotFoundError:
        return _empty_partition(dt)
    if meta.num_rows == 0:
        return _empty_partition(dt)
    df = pd.read_parquet(f)
    cols = list(df.columns)
    if want_cols:
        cols = [c for c in cols if c in want_cols]
    symbols = df["symbol"].astype(str)
    dup = int(len(df) - symbols.nunique())
    date_mismatch = 0
    for cname in ("date", "time"):
        if cname in df.columns:
            dd = pd.to_datetime(df[cname], errors="coerce").dt.strftime("%Y%m%d")
            date_mismatch = int((dd.notna() & (dd != dt)).sum())
            break
    stats = {}
    cap = int(cfg["sample_per_partition"])
    for c in cols:
        if c in SKIP_STATS:
            continue
        s = df[c]
        nn = int(s.notna().sum())
        if nn == 0:
            stats[c] = {"nn": 0, "nu": 0, "zero": None, "inf": 0, "mean": None,
                        "std": None, "min": None, "max": None, "sample": None}
            continue
        try:
            nu = int(s.nunique(dropna=True))
        except TypeError:
            nu = -1
        st = {"nn": nn, "nu": nu, "zero": None, "inf": None, "mean": None,
              "std": None, "min": None, "max": None, "sample": None}
        if pd.api.types.is_float_dtype(s):
            import numpy as np          # noqa: PLC0415
            v_all = s.dropna().to_numpy(dtype="float64")
            finite_mask = np.isfinite(v_all)
            st["inf"] = int((~finite_mask).sum())
            v = v_all[finite_mask]
            if v.size == 0:
                stats[c] = st
                continue
            st["zero"] = int((v == 0).sum())
            st["mean"] = float(v.mean())
            st["std"] = float(v.std(ddof=1)) if v.size > 1 else None
            st["min"] = float(v.min())
            st["max"] = float(v.max())
            st["sample"] = _stride_sample(v.tolist(), cap)
        stats[c] = st
    return {"dt": dt, "n_rows": len(df), "symbols": int(symbols.nunique()),
            "dup_symbols": dup, "columns": cols, "stats": stats,
            "empty": False, "date_mismatch": date_mismatch}


def run_quantdb(args) -> tuple[dict, dict]:
    root = resolve_data_root()
    market = args.market.upper()
    dataset = args.dataset or DEFAULT_DATASET[market]
    key = (market, dataset)
    if key not in DATASET_PATHS:
        raise SystemExit(f"市场 {market} 无数据集 {dataset}；可用：{[d for (m, d) in DATASET_PATHS if m == market]}")
    factor_dir = root / DATASET_PATHS[key]
    kline_dir = root / KLINE_PATHS[market]
    if not factor_dir.is_dir():
        raise SystemExit(f"因子数据集目录不存在：{factor_dir}")

    parts = sorted(p for p in factor_dir.glob("dt=*") if p.is_dir())
    if not parts:
        raise SystemExit(f"数据集无分区：{factor_dir}")
    if args.last_partitions:
        parts = parts[-int(args.last_partitions):]
    if args.start:
        parts = [p for p in parts if p.name[3:] >= args.start.replace("-", "")]
    if args.end:
        parts = [p for p in parts if p.name[3:] <= args.end.replace("-", "")]
    if len(parts) < 4:
        raise SystemExit(f"窗口内分区不足（{len(parts)}），检查 --start/--end/--last-partitions")

    kline_dates = sorted(p.name[3:] for p in kline_dir.glob("dt=*") if p.is_dir()) \
        if kline_dir.is_dir() else []
    want_cols = [c.strip() for c in args.columns.split(",")] if args.columns else None

    cfg = dict(DEFAULTS)
    if args.baseline_partitions:
        cfg["baseline_partitions"] = args.baseline_partitions
    if args.recent_partitions:
        cfg["recent_partitions"] = args.recent_partitions
    if args.max_dist_alerts:
        cfg["max_dist_alerts"] = args.max_dist_alerts
    cfg["allow_removed"] = [c.strip() for c in args.allow_removed.split(",") if c.strip()] \
        if args.allow_removed else []
    cfg["allow_added"] = [c.strip() for c in args.allow_added.split(",") if c.strip()] \
        if args.allow_added else []

    print(f"扫描 {market}/{dataset}：{parts[0].name} ~ {parts[-1].name}（{len(parts)} 个分区）…",
          file=sys.stderr)
    partitions = []
    for i, p in enumerate(parts, 1):
        partitions.append(_profile_partition_parquet(p / "data.parquet", p.name[3:], cfg, want_cols))
        if i % 20 == 0 or i == len(parts):
            print(f"  已扫描 {i}/{len(parts)} 个分区", file=sys.stderr)

    scan = {"mode": "quantdb", "market": market, "dataset": dataset,
            "data_root": str(root), "factor_path": str(factor_dir),
            "kline_path": str(kline_dir) if kline_dir.is_dir() else None,
            "partitions": partitions, "kline_dates": kline_dates}
    return scan, cfg


# ------------------------------------------------------------------ 入口

def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="因子面板漂移监测（合成面板 / QuantDB 本地直读）",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--demo", action="store_true", help="内置确定性合成面板 + 四种注入断言（纯标准库）")
    mode.add_argument("--quantdb", action="store_true", help="QuantDB 直读模式（容器内 pandas/pyarrow）")
    p.add_argument("--market", default="CN", choices=["CN", "HK", "US"], help="市场（quantdb 模式）")
    p.add_argument("--dataset", default=None,
                   help="因子数据集：CN=features_daily/l1_factors/l2_factors；HK/US=l1_factors")
    p.add_argument("--start", default=None, help="起始日 YYYY-MM-DD")
    p.add_argument("--end", default=None, help="结束日 YYYY-MM-DD")
    p.add_argument("--last-partitions", type=int, default=None, help="只取最近 N 个分区（大市场抽取用）")
    p.add_argument("--baseline-partitions", type=int, default=None, help="基线分区数（默认 20）")
    p.add_argument("--recent-partitions", type=int, default=None, help="对比分区数（默认 5）")
    p.add_argument("--columns", default=None, help="只统计指定列（逗号分隔；默认全部）")
    p.add_argument("--max-dist-alerts", type=int, default=None, help="分布告警输出上限（默认 15）")
    p.add_argument("--allow-removed", default=None, help="白名单：这些列的移除降级为 info（已知断点用）")
    p.add_argument("--allow-added", default=None, help="白名单：这些列的新增降级为 info")
    p.add_argument("--out", default=None, help="JSON 报告输出路径（父目录自动创建）")
    args = p.parse_args(argv)

    if args.demo:
        return run_demo(args.out)

    scan, cfg = run_quantdb(args)
    rep = analyze(scan, cfg)
    print(render_text(rep))
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(json_safe(rep), ensure_ascii=False, indent=2),
                            encoding="utf-8")
        print(f"\n已写入 JSON 报告：{out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
