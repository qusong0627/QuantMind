#!/usr/bin/env python3
"""因子 IC 衰减与稳定性诊断 — 离线确定性引擎 + QuantDB(CN/HK/US) 数据装配。

来源：quantskills/skill-factor-ic-decay（GPL-3.0-only）。
方法论保留（日度截面 Spearman IC、ICIR、Newey-West t、滚动/分段稳定性、多周期半衰期），
数据层由 PandaData 改为本地 QuantDB 直读（2026-10 标定）：

  --input CSV 模式：纯标准库，任意机器可跑。长面板 CSV：date,symbol,factor,fwd_ret
      （可选多周期列 fwd_ret_{n}，如 fwd_ret_1/5/10/20；缺省用最短周期做主口径）。
  --quantdb 模式：pandas/pyarrow，**在 quantmind 容器内运行**。
      因子面板 ← <market_root>/6_ml_datasets/<dataset>（CN 默认 features_daily，另有 l1_factors/l2_factors；
                HK/US 为 l1_factors）——2024 与 2026 版 features_daily 列集不同（50 vs 78 列），
                脚本按分区逐列读取，缺列分区跳过并计数。
      收益     ← <market_root>/1_kline_data/daily_forward（CN=前复权；HK/US=**不复权**原始价）
                ret_h(t) = close(t+h)/close(t) - 1，h 为同一标的序列的后续第 h 个交易日，
                收益读取范围自动向窗口末端之后多取 ~40 个交易日以覆盖 h<=20 的前视窗。
      HK 去重：quanthk 的 daily_forward 在 2024-09-02~2026-05-08 存在 paid_hk/akshare
                双来源重复行。多数标的近等，但约 0.5%~5% 的标的 paid_hk 带（前）复权
                缩放（实测 1211.HK ×3、0788.HK ×10、0755.HK ×100）——脚本按
                published_at 排序后 keep='last' 保留 akshare（原始价）并计数。
      票池     ← 窗口末日（<=end 的最后一个交易日）成交额降序前 N（--top-n，默认 300）；
                或 --symbols 显式指定（parquet 后缀式：CN=000001.SZ / HK=0001.HK / US=NVDA）。

标签列护栏：features_daily 各版本分别带 return_{n}d（2024-2025）与 future_return_{n}d（2026+）
标签列，二者都是"未来收益"标签；脚本拒绝把这些列当因子计算（防前视泄漏），命中即报错退出。

用法：
  # CSV 模式（纯标准库）
  python3 ic_decay.py --input panel.csv --name MOM20 --out report.json

  # QuantDB 模式（quantmind 容器内）
  python3 ic_decay.py --quantdb --market CN --dataset features_daily \
      --factors ma_gap_20,rsi_14,vol_std_20 --start 2024-01-01 --end 2025-12-31 \
      --top-n 300 --out /data/reports/factor-ic-decay/cn_features_2024_2025.json

  # 探查某数据集可用列（标注标签列）
  python3 ic_decay.py --quantdb --market CN --list-columns

输出：stdout 中文表格 + JSON 报告（--out 指定文件路径）。
半衰期与强度标签是历史证据描述，不是"还能用 N 天"的承诺，也不是交易信号。
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import sys
from datetime import date as _date
from pathlib import Path

MIN_IC_OBS = 60              # 日度 IC 观测少于此数拒绝输出
WARN_IC_OBS = 252            # 少于此数警告样本偏少
ANNUALIZE = 252.0            # 日度 IC 年化因子
MIN_NAMES_PER_CROSS = 3      # 当日有效截面标的数 < 3 跳过该日（与源技能一致）

# 标签列模式（未来收益，禁止当因子；来自 Dataset 内置标签列）
LABEL_COL_PATTERNS = [
    re.compile(r"^label_return$", re.I),
    re.compile(r"^return_\d+d$", re.I),
    re.compile(r"^future_return_\d+d$", re.I),
]

# 数据集/行情路径（相对数据根目录）
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
DATA_ROOT_CANDIDATES = ["/data", "/quantmind/data", "/home/zbox/projects/quantmind/data"]

DEFAULT_DATASET = {"CN": "features_daily", "HK": "l1_factors", "US": "l1_factors"}
KLINE_ADJUSTMENT_NOTE = {
    "CN": "daily_forward=前复权（收益已含分红送转调整）",
    "HK": "daily_forward=不复权原始价（收益未含分红调整，IC 口径偏保守）",
    "US": "daily_forward=不复权原始价（收益未含分红调整，IC 口径偏保守）",
}

# 数据集内非因子列（--list-columns 的"建议因子列"会剔除）
AUX_COLUMNS = {
    "symbol", "time", "date", "open", "high", "low", "close", "volume", "amount",
    "adj_factor", "release_id", "published_at", "Symbol_val", "close_val",
}


def is_label_column(name: str) -> bool:
    return any(p.match(name.strip()) for p in LABEL_COL_PATTERNS)


# ------------------------------------------------------------------ 统计引擎（纯标准库）

def rankdata_average(values: list[float]) -> list[float]:
    """平均秩（并列取平均），与 scipy rankdata(method='average') 同口径。"""
    n = len(values)
    order = sorted(range(n), key=lambda i: values[i])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg_rank = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg_rank
        i = j + 1
    return ranks


def spearman(xs: list[float], ys: list[float]) -> float | None:
    """两向量 Spearman 秩相关；样本不足或退化返回 None。"""
    n = len(xs)
    if n < MIN_NAMES_PER_CROSS or n != len(ys):
        return None
    rx = rankdata_average(xs)
    ry = rankdata_average(ys)
    mx = sum(rx) / n
    my = sum(ry) / n
    num = 0.0
    sx = 0.0
    sy = 0.0
    for a, b in zip(rx, ry, strict=False):
        da = a - mx
        db = b - my
        num += da * db
        sx += da * da
        sy += db * db
    den = math.sqrt(sx * sy)
    if den <= 0:
        return None
    ic = num / den
    # 数值护栏
    if ic > 1.0:
        ic = 1.0
    elif ic < -1.0:
        ic = -1.0
    return ic


def newey_west_t(values: list[float], lag: int = 5) -> float:
    """均值是否显著异于 0 的 Newey-West t（Bartlett 核）。"""
    arr = [v for v in values if v is not None and not math.isnan(v)]
    n = len(arr)
    if n < max(lag + 2, 5):
        return float("nan")
    mean = sum(arr) / n
    u = [v - mean for v in arr]
    gamma0 = sum(v * v for v in u) / n
    nw = gamma0
    for j in range(1, int(lag) + 1):
        w = 1.0 - j / (lag + 1.0)
        gamma_j = sum(u[t] * u[t - j] for t in range(j, n)) / n
        nw += 2.0 * w * gamma_j
    if nw <= 0:            # 极端小样本下方差估计可为负，截断
        return float("nan")
    se = math.sqrt(nw / n)
    if se <= 0:
        return float("nan")
    return mean / se


def summarize_ic(ics: list[float], nw_lag: int = 5) -> dict:
    arr = [v for v in ics if v is not None and not math.isnan(v)]
    n = len(arr)
    if n == 0:
        return {
            "n": 0, "mean_ic": None, "std_ic": None, "icir_raw": None,
            "icir_ann": None, "hit_rate": None, "nw_t": None,
        }
    mean = sum(arr) / n
    std = None
    if n > 1:
        var = sum((v - mean) ** 2 for v in arr) / (n - 1)
        std = math.sqrt(var)
    icir_raw = mean / std if std and std > 0 else None
    icir_ann = icir_raw * math.sqrt(ANNUALIZE) if icir_raw is not None else None
    hit = sum(1 for v in arr if v > 0) / n
    nw = newey_west_t(arr, lag=nw_lag)
    return {
        "n": n,
        "mean_ic": mean,
        "std_ic": std,
        "icir_raw": icir_raw,
        "icir_ann": icir_ann,
        "hit_rate": hit,
        "nw_t": None if math.isnan(nw) else nw,
    }


def rolling_stats(ic_pairs: list[tuple[str, float]], window: int = 60) -> list[dict]:
    """滚动窗口（默认 60 交易日）均值 IC 与年化 ICIR。"""
    dates = [d for d, _ in ic_pairs]
    ics = [v for _, v in ic_pairs]
    n = len(ics)
    min_periods = max(10, window // 3)
    out = []
    for i in range(n):
        lo = max(0, i - window + 1)
        seg = ics[lo:i + 1]
        if len(seg) < min_periods:
            continue
        s = summarize_ic(seg, nw_lag=0)
        out.append({
            "date": dates[i],
            "mean_ic": s["mean_ic"],
            "std_ic": s["std_ic"],
            "icir_ann": s["icir_ann"],
        })
    return out


def segment_stats(ic_pairs: list[tuple[str, float]], window: int = 60) -> list[dict]:
    """非重叠分段（默认每 60 个 IC 观测一段）：分段均值 IC 与胜率。"""
    dates = [d for d, _ in ic_pairs]
    ics = [v for _, v in ic_pairs]
    seg_min = max(10, window // 3)
    out = []
    for start in range(0, len(ics), window):
        seg = ics[start:start + window]
        if len(seg) < seg_min:
            continue
        mean = sum(seg) / len(seg)
        out.append({
            "start": dates[start],
            "end": dates[start + len(seg) - 1],
            "n": len(seg),
            "mean_ic": mean,
            "hit_rate": sum(1 for v in seg if v > 0) / len(seg),
        })
    return out


def fit_half_life(curve: list[dict]) -> dict:
    """对正均值 IC 周期做 log-linear OLS：log(IC)=log(A)-h/τ，半衰期=τ·ln(2)。"""
    pts = [
        (float(p["h"]), float(p["mean_ic"]))
        for p in curve
        if p.get("h") is not None and p.get("h") > 0
        and p.get("mean_ic") is not None and not math.isnan(p["mean_ic"])
        and p["mean_ic"] > 0
    ]
    empty = {
        "A": None, "tau": None, "half_life": None,
        "r_squared": None, "n_points": len(pts), "fitted": False,
        "note": "不足以拟合指数衰减（需 ≥2 个正均值 IC 周期）",
    }
    if len(pts) < 2:
        return empty
    hs = [p[0] for p in pts]
    log_ic = [math.log(p[1]) for p in pts]
    mh = sum(hs) / len(hs)
    ml = sum(log_ic) / len(log_ic)
    cov = sum((h - mh) * (lv - ml) for h, lv in zip(hs, log_ic, strict=False))
    var = sum((h - mh) ** 2 for h in hs)
    if var <= 0:
        return {**empty, "note": "OLS 拟合失败（周期无方差）"}
    b = cov / var                      # 斜率 = -1/τ
    a = ml - b * mh
    if b >= 0:
        return {
            "A": math.exp(a), "tau": None, "half_life": None,
            "r_squared": None, "n_points": len(pts), "fitted": False,
            "note": "IC 未呈指数衰减（斜率≥0），半衰期未定义",
        }
    tau = -1.0 / b
    half_life = tau * math.log(2.0)
    ss_res = sum((lv - (a + b * h)) ** 2 for h, lv in zip(hs, log_ic, strict=False))
    ss_tot = sum((lv - ml) ** 2 for lv in log_ic)
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else float("nan")
    return {
        "A": math.exp(a),
        "tau": tau,
        "half_life": half_life,
        "r_squared": None if math.isnan(r2) else r2,
        "n_points": len(pts),
        "fitted": True,
        "note": f"IC(h)=A·exp(-h/τ)，τ={tau:.2f}，半衰期={half_life:.2f} 日",
    }


def strength_label(mean_ic: float | None, nw_t: float | None) -> str:
    """证据摘要标签（启发式；不是下单门槛，也不是交易信号）。"""
    if mean_ic is None:
        return "weak_or_noise"
    t = abs(nw_t) if nw_t is not None else 0.0
    if abs(mean_ic) >= 0.03 and t >= 2.0:
        return "strong"
    if abs(mean_ic) >= 0.01 and t >= 1.5:
        return "moderate"
    return "weak_or_noise"


# ------------------------------------------------------------------ 报告装配（两种模式共用）

def build_factor_report(
    rows: list[dict],
    name: str,
    horizons: list[tuple[str, int | None]],
    window: int,
    nw_lag: int,
    extra: dict | None = None,
) -> dict:
    """rows: [{"date": "YYYY-MM-DD", "f": float, "h": {label: ret}}]。
    horizons: [(label, h_num or None)]，主口径取第一个（最短周期）。"""
    if not rows:
        raise ValueError(f"因子 {name} 无有效面板行")
    dates = sorted({r["date"] for r in rows})
    primary_label = horizons[0][0]

    ic_by_h: dict[str, list[tuple[str, float]]] = {}
    for label, _h in horizons:
        pairs = []
        for r in rows:
            v = r["h"].get(label)
            if v is None or math.isnan(v) or math.isnan(r["f"]):
                continue
            pairs.append((r["date"], r["f"], v))
        # 按日截面
        by_day: dict[str, list[tuple[float, float]]] = {}
        for d, f, v in pairs:
            by_day.setdefault(d, []).append((f, v))
        per_day = []
        skipped = 0
        for d in sorted(by_day):
            sec = by_day[d]
            ic = spearman([a for a, _ in sec], [b for _, b in sec])
            if ic is None:
                skipped += 1
                continue
            per_day.append((d, ic))
        ic_by_h[label] = per_day

    primary = ic_by_h[primary_label]
    n_ic = len(primary)
    if n_ic < MIN_IC_OBS:
        n_uniq = len({round(r["f"], 12) for r in rows})
        if n_uniq <= 1:
            raise ValueError(
                f"因子 {name} 在窗口内无截面变化（nunique={n_uniq}，常数/全空列），"
                "无法计算 IC——该列很可能未填充（HK l1_factors 已知部分基本面列实测全 0）。"
                "先用 --list-columns 查看非空计数与 nunique。"
            )
        raise ValueError(
            f"因子 {name} 的日度 IC 观测数过少（{n_ic} < {MIN_IC_OBS}），拒绝输出不可靠诊断。"
            f"窗口内非空行 {len(rows)}（nunique={n_uniq}），截面 <{MIN_NAMES_PER_CROSS} 只或被跳过的天过多；"
            "请扩大窗口（建议 ≥252 个交易日）或放宽票池。"
        )
    low_sample = n_ic < WARN_IC_OBS
    summary = summarize_ic([v for _, v in primary], nw_lag=nw_lag)

    # 截面宽度诊断（主口径）
    by_day_count: dict[str, int] = {}
    for r in rows:
        v = r["h"].get(primary_label)
        if v is None or math.isnan(v) or math.isnan(r["f"]):
            continue
        by_day_count[r["date"]] = by_day_count.get(r["date"], 0) + 1
    counts = sorted(by_day_count.values())
    cross_section = {}
    if counts:
        mid = counts[len(counts) // 2] if len(counts) % 2 else (
            counts[len(counts) // 2 - 1] + counts[len(counts) // 2]) / 2
        cross_section = {
            "min_names": counts[0],
            "median_names": mid,
            "days_skipped": len(by_day_count) - n_ic,
        }

    curve = []
    for label, h_num in horizons:
        per_day_h = ic_by_h[label]
        s = summarize_ic([v for _, v in per_day_h], nw_lag=nw_lag)
        curve.append({
            "horizon": label,
            "h": h_num,
            "mean_ic": s["mean_ic"],
            "std_ic": s["std_ic"],
            "n": s["n"],
            "icir_ann": s["icir_ann"],
            "nw_t": s["nw_t"],
            "hit_rate": s["hit_rate"],
        })

    if len(curve) >= 2 and any(p.get("h") for p in curve):
        fit = fit_half_life(curve)
    else:
        fit = {
            "A": None, "tau": None, "half_life": None, "r_squared": None,
            "n_points": 0, "fitted": False,
            "note": "单周期模式，无法估计半衰期；提供多周期列/--horizons 可拟合",
        }

    mean_ic = summary["mean_ic"]
    caveats = [
        "IC 是历史截面预测力的统计描述，不是买卖指令；衰减快不代表立刻失效，慢也不保证持续有效。",
        f"日度 IC 观测数 n={n_ic}"
        + ("，少于一年（252），样本偏少，谨慎解读。" if low_sample else "。"),
        f"Newey-West t 使用 lag={nw_lag}（处理 IC 序列自相关）；重叠收益周期会进一步抬高自相关。",
        "多周期收益由同一价格序列滚动生成，存在重叠持有期，衰减曲线解释需谨慎。",
        "防未来函数：因子列必须是当日收盘前可得的信息；标签列（return_Nd/future_return_Nd/label_return）"
        "已由脚本拒绝，但因子本身的时点正确性仍由数据生产方保证。",
        "本结果不构成投资建议。",
    ]
    if extra and extra.get("caveats"):
        caveats = list(extra["caveats"]) + caveats

    report = {
        "name": name,
        "n_ic": n_ic,
        "date_start": dates[0],
        "date_end": dates[-1],
        "low_sample_warning": low_sample,
        "window": window,
        "nw_lag": nw_lag,
        "primary_horizon": primary_label,
        "summary": summary,
        "strength": strength_label(mean_ic, summary["nw_t"]),
        "cross_section": cross_section,
        "segments": segment_stats(primary, window),
        "rolling": rolling_stats(primary, window),
        "decay_curve": curve,
        "half_life_fit": fit,
        "caveats": caveats,
    }
    if extra:
        for key in ("column", "dataset", "n_symbols"):
            if key in extra:
                report[key] = extra[key]
    return report


def _fmt(v, dp=4, signed=False) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    return f"{v:+.{dp}f}" if signed else f"{v:.{dp}f}"


def render_text(rep: dict) -> str:
    lines = []
    lines.append(f"因子 IC 衰减诊断（{rep['name']}）")
    lines.append("=" * 60)
    head = (
        f"窗口：{rep['date_start']} → {rep['date_end']}  ·  IC 观测 {rep['n_ic']}"
        + (f"  ·  标的 {rep['n_symbols']}" if rep.get("n_symbols") else "")
        + ("  ⚠ 样本偏少" if rep["low_sample_warning"] else "")
    )
    lines.append(head)
    s = rep["summary"]
    lines.append("")
    lines.append("【全样本 IC】（主口径：" + rep.get("primary_horizon", "H") + "）")
    lines.append(f"  均值 IC     : {_fmt(s['mean_ic'], signed=True)}")
    lines.append(f"  标准差      : {_fmt(s['std_ic'])}")
    lines.append(f"  ICIR (raw)  : {_fmt(s['icir_raw'], 3, signed=True)}   (= mean/std)")
    lines.append(f"  ICIR (年化) : {_fmt(s['icir_ann'], 3, signed=True)}   (= mean/std * sqrt(252))")
    lines.append(f"  命中率 IC>0 : {_fmt(s['hit_rate'] * 100 if s['hit_rate'] is not None else None, 1)}%")
    lines.append(f"  Newey-West t: {_fmt(s['nw_t'], 2, signed=True)}  (lag={rep['nw_lag']})")
    lines.append(f"  强度标签    : {rep['strength']}  （证据描述，非交易信号）")
    cs = rep.get("cross_section") or {}
    if cs:
        lines.append(
            f"  截面宽度    : 中位 {cs.get('median_names')} 只/日（最小 {cs.get('min_names')}，"
            f"跳过 {cs.get('days_skipped')} 日）"
        )
    lines.append("")
    lines.append(f"【滚动稳定性】窗口={rep['window']} 交易日（日度 IC 滚动）")
    roll = rep["rolling"]
    if roll:
        last = roll[-1]
        means = [r["mean_ic"] for r in roll if r.get("mean_ic") is not None]
        lines.append(
            f"  最新滚动均值 IC={_fmt(last['mean_ic'], signed=True)}，"
            f"年化 ICIR={_fmt(last.get('icir_ann'), 3, signed=True)}  （截至 {last['date']}）"
        )
        if means:
            lines.append(f"  滚动均值 IC 范围：[{_fmt(min(means), signed=True)}, {_fmt(max(means), signed=True)}]")
    else:
        lines.append("  （滚动序列为空）")
    lines.append("")
    lines.append(f"【分段稳定性】非重叠每 {rep['window']} 个 IC 观测一段")
    segs = rep["segments"]
    if segs:
        lines.append(f"  {'区间':<25} {'均值IC':>9} {'胜率':>7} {'n':>5}")
        for sg in segs:
            lines.append(
                f"  {sg['start']} ~ {sg['end']:<10} {_fmt(sg['mean_ic'], 4, signed=True):>9} "
                f"{_fmt(sg['hit_rate'] * 100, 1):>6}% {sg['n']:>5}"
            )
    else:
        lines.append("  （分段为空）")
    lines.append("")
    lines.append("【衰减曲线】")
    curve = rep["decay_curve"]
    if curve:
        lines.append(f"  {'周期':>6} {'均值IC':>10} {'标准差':>10} {'ICIR年化':>10} {'NW-t':>8} {'n':>6}")
        for p in curve:
            lines.append(
                f"  {str(p['horizon']):>6} {_fmt(p['mean_ic'], 4, signed=True):>10} "
                f"{_fmt(p['std_ic'], 4):>10} {_fmt(p['icir_ann'], 3, signed=True):>10} "
                f"{_fmt(p['nw_t'], 2, signed=True):>8} {p['n']:>6}"
            )
    else:
        lines.append("  （无衰减曲线）")
    fit = rep["half_life_fit"]
    lines.append("")
    lines.append("【半衰期拟合】")
    if fit.get("fitted"):
        lines.append(
            f"  A={_fmt(fit['A'])}，τ={_fmt(fit['tau'], 2)}，半衰期={_fmt(fit['half_life'], 2)} 日"
        )
        lines.append(f"  R²={_fmt(fit['r_squared'], 3)}，拟合点数={fit['n_points']}")
    else:
        lines.append(f"  未拟合：{fit.get('note', '')}")
    lines.append("")
    lines.append("说明：")
    for c in rep["caveats"]:
        lines.append(f"  - {c}")
    return "\n".join(lines)


def json_safe(obj):
    """nan/inf → null，非有限浮点一律置空。"""
    if isinstance(obj, dict):
        return {k: json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [json_safe(v) for v in obj]
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    return obj


# ------------------------------------------------------------------ CSV 模式（纯标准库）

def _norm_date(raw: str) -> str | None:
    s = str(raw).strip()
    if not s:
        return None
    if len(s) >= 10:
        head = s[:10].replace("/", "-").replace(".", "-")
        try:
            return _date.fromisoformat(head).isoformat()
        except ValueError:
            pass
    digits = re.sub(r"\D", "", s)
    if len(digits) == 8:
        try:
            return f"{digits[:4]}-{digits[4:6]}-{digits[6:]}"
        except Exception:
            return None
    return None


def _to_float(raw) -> float:
    try:
        v = float(str(raw).strip())
        return v if math.isfinite(v) else float("nan")
    except (TypeError, ValueError):
        return float("nan")


def load_csv_panel(
    path: str,
    factor_col: str = "factor",
    ret_col: str = "fwd_ret",
    horizon_label: str = "H",
) -> tuple[list[dict], list[tuple[str, int | None]], str]:
    with open(path, encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        fieldnames = reader.fieldnames or []
        raw_rows = list(reader)

    lower = {c.lower().strip(): c for c in fieldnames}

    def pick(*names):
        for n in names:
            if n.lower() in lower:
                return lower[n.lower()]
        return None

    dcol = pick("date", "日期", "trade_date", "time")
    scol = pick("symbol", "code", "ticker", "股票代码", "证券代码")
    fcol = pick(factor_col, "factor", "signal", "alpha", "因子")
    if not dcol or not scol or not fcol:
        raise ValueError(f"CSV 需含 date/symbol/{factor_col} 列，实际列：{fieldnames}")
    if is_label_column(fcol) and fcol != factor_col:
        raise ValueError(f"列 {fcol} 命中标签列黑名单（未来收益），禁止当因子计算")

    rcol = pick(ret_col, "fwd_ret", "forward_return", "ret", "收益")
    multi_cols: list[tuple[int, str]] = []
    for c in fieldnames:
        m = re.match(r"(?i)^fwd_ret_(\d+)$", str(c).strip())
        if m:
            multi_cols.append((int(m.group(1)), c))
    multi_cols.sort()

    if not rcol and not multi_cols:
        raise ValueError(f"CSV 需含 {ret_col} 或 fwd_ret_{{n}} 列，实际列：{fieldnames}")

    if multi_cols:
        horizons = [(f"H{n}", n) for n, _ in multi_cols]
    else:
        horizons = [(horizon_label, None)]  # 单列模式：周期未知，用 --horizon-label 标注

    rows = []
    for raw in raw_rows:
        d = _norm_date(raw.get(dcol, ""))
        if d is None:
            continue
        fv = _to_float(raw.get(fcol))
        if math.isnan(fv):
            continue
        hvals = {}
        if multi_cols:
            for n, c in multi_cols:
                hvals[f"H{n}"] = _to_float(raw.get(c))
        else:
            hvals[horizon_label] = _to_float(raw.get(rcol))
        rows.append({"date": d, "f": fv, "h": hvals})
    if not rows:
        raise ValueError("CSV 无有效数据行（date/symbol/factor 均需可解析）")
    return rows, horizons, (fcol if not multi_cols else "multi")


def run_csv(args) -> dict:
    rows, horizons, src = load_csv_panel(
        args.input, args.factor_col, args.ret_col, horizon_label=args.horizon_label
    )
    name = args.name or "factor"
    rep = build_factor_report(
        rows, name=name, horizons=horizons, window=args.window, nw_lag=args.nw_lag,
        extra={"source": f"CSV:{args.input}"},
    )
    wrapper = {
        "mode": "csv",
        "input": str(Path(args.input).resolve()),
        "universe": {"method": "csv_as_given"},
        "factors": [rep],
    }
    return wrapper, [rep]


# ------------------------------------------------------------------ QuantDB 模式（容器 pandas）

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


def _list_dt_partitions(base: Path, start_compact: str, end_compact: str) -> list[tuple[str, Path]]:
    out = []
    if not base.is_dir():
        raise SystemExit(f"目录不存在：{base}")
    for p in sorted(base.glob("dt=*")):
        dt = p.name[3:]
        if start_compact <= dt <= end_compact:
            out.append((dt, p / "data.parquet"))
    return out


def run_quantdb_list_columns(args):
    root = resolve_data_root()
    market = args.market.upper()
    dataset = args.dataset or DEFAULT_DATASET[market]
    key = (market, dataset)
    if key not in DATASET_PATHS:
        raise SystemExit(f"市场 {market} 无数据集 {dataset}；可用：{[d for (m, d) in DATASET_PATHS if m == market]}")
    import pandas as pd            # noqa: PLC0415  （容器内依赖）
    import pyarrow.parquet as pq   # noqa: PLC0415

    base = root / DATASET_PATHS[key]
    parts = sorted(base.glob("dt=*"))
    if not parts:
        raise SystemExit(f"数据集无分区：{base}")
    last = parts[-1] / "data.parquet"
    cols = list(pq.read_schema(last).names)
    df = pd.read_parquet(last)
    labels = [c for c in cols if is_label_column(c)]
    suggested = [c for c in cols if c not in AUX_COLUMNS and c not in labels]
    n = len(df)
    print(f"数据集 {market}/{dataset}（{base}）")
    print(f"最新分区：{parts[-1].name}  共 {len(cols)} 列，{n} 行")
    print(f"\n标签列（禁止当因子，{len(labels)}）：{labels}")
    print(f"\n辅助列：{sorted(c for c in cols if c in AUX_COLUMNS)}")
    print(f"\n建议因子列（{len(suggested)}；非空计数/nunique 取自最新分区，⚠=常量或全空）：")
    for c in suggested:
        s = df[c]
        nn = int(s.notna().sum())
        try:
            nu = int(s.nunique(dropna=True))
        except TypeError:
            nu = -1
        flag = " ⚠ 常量/全空，不可用" if nu <= 1 else (f" ⚠ 非空仅 {nn}/{n}" if nn < n * 0.5 else "")
        print(f"  {c:<32} 非空 {nn}/{n}  nunique {nu}{flag}")
    return 0


def run_quantdb(args) -> tuple[dict, list[dict]]:
    import pandas as pd            # noqa: PLC0415
    import pyarrow.parquet as pq   # noqa: PLC0415

    root = resolve_data_root()
    market = args.market.upper()
    dataset = args.dataset or DEFAULT_DATASET[market]
    key = (market, dataset)
    if key not in DATASET_PATHS:
        raise SystemExit(f"市场 {market} 无数据集 {dataset}；可用：{[d for (m, d) in DATASET_PATHS if m == market]}")
    factor_dir = root / DATASET_PATHS[key]
    kline_dir = root / KLINE_PATHS[market]

    factors = [f.strip() for f in (args.factors or "").split(",") if f.strip()]
    if not factors:
        raise SystemExit("--quantdb 模式需 --factors 指定因子列（逗号分隔）；可用 --list-columns 探查")
    bad = [f for f in factors if is_label_column(f)]
    if bad:
        raise SystemExit(
            f"拒绝把标签列当因子：{bad}（这些列是未来收益标签，用了就是前视泄漏）。"
            "若想评估「昨日收益的反转效应」，请自行构造显式的滞后收益列。"
        )

    horizons = sorted({int(h) for h in str(args.horizons).split(",") if h.strip()})
    if not horizons or any(h <= 0 for h in horizons):
        raise SystemExit("--horizons 需为正整数列表，如 1,5,10,20")

    # ---- 窗口与分区
    kparts_all = sorted(kline_dir.glob("dt=*"))
    if not kparts_all:
        raise SystemExit(f"行情目录无分区：{kline_dir}")
    if args.end:
        end_compact = args.end.replace("-", "")
    else:
        end_compact = kparts_all[-1].name[3:]
    if args.start:
        start_compact = args.start.replace("-", "")
    else:
        d = _date.fromisoformat(f"{end_compact[:4]}-{end_compact[4:6]}-{end_compact[6:]}")
        start_compact = (d.replace(year=d.year - 2)).strftime("%Y%m%d")

    kparts = _list_dt_partitions(kline_dir, start_compact, end_compact)
    if not kparts:
        raise SystemExit(f"行情无 {start_compact}~{end_compact} 分区")
    # h<=20 需要窗口末端之后的收盘价：多读 ~40 个交易日
    tail = [(p.name[3:], p / "data.parquet") for p in kparts_all if p.name[3:] > end_compact][:40]
    fparts = _list_dt_partitions(factor_dir, start_compact, end_compact)
    if not fparts:
        raise SystemExit(f"因子数据集无 {start_compact}~{end_compact} 分区：{factor_dir}")

    # ---- 票池：窗口末日前 N 成交额（或 --symbols）
    last_dt, last_file = kparts[-1]
    universe_info: dict
    if args.symbols:
        universe = [s.strip() for s in args.symbols.split(",") if s.strip()]
        universe_info = {"method": "explicit_symbols", "asof_date": last_dt, "n_symbols": len(universe)}
    else:
        u = pd.read_parquet(last_file, columns=["symbol", "amount"])
        u = u.dropna(subset=["amount"])
        u = u[u["amount"] > 0].sort_values("amount", ascending=False)
        n_take = args.top_n if args.top_n and args.top_n > 0 else len(u)
        universe = u["symbol"].head(n_take).astype(str).tolist()
        universe_info = {
            "method": f"top_{n_take}_by_amount",
            "asof_date": last_dt,
            "n_symbols": len(universe),
            "note": "按窗口末日成交额选取（事后票池，含轻微选择偏差；非逐日动态池）",
        }
    uset = set(universe)

    # ---- 行情：读窗口 + 前视尾部，逐标的算 ret_h
    def _read_kline(dt: str, f: Path) -> pd.DataFrame:
        cols = [
            c
            for c in ("symbol", "time", "close", "published_at")
            if c in pq.read_schema(f).names
        ]
        df = pd.read_parquet(f, columns=cols)
        df["date"] = pd.to_datetime(df["time"]).dt.strftime("%Y-%m-%d")
        return df

    kframes = [_read_kline(dt, f) for dt, f in (kparts + tail)]
    kdf = pd.concat(kframes, ignore_index=True)
    before = len(kdf)
    kdf = kdf.dropna(subset=["close"])
    # HK daily_forward 2024-09-02~2026-05-08 存在 paid_hk+akshare 双来源重复行；
    # paid_hk 对部分标的携带（前）复权缩放（实测 1211.HK ×3、0788.HK ×10、
    # 0755.HK ×100），必须保留 akshare（published_at 较晚、为原始价）。
    if "published_at" in kdf.columns:
        kdf = kdf.sort_values(
            ["symbol", "date", "published_at"], kind="stable"
        ).drop_duplicates(subset=["symbol", "date"], keep="last")
        kdf = kdf.drop(columns=["published_at"])
    else:
        kdf = kdf.drop_duplicates(subset=["symbol", "date"], keep="first")
    dup_dropped = before - len(kdf)
    kdf = kdf[kdf["symbol"].isin(uset)]
    if kdf.empty:
        raise SystemExit("指定票池在行情窗口中无数据（检查 symbol 格式：CN=000001.SZ / HK=0001.HK / US=NVDA）")
    kdf = kdf.sort_values(["symbol", "date"])
    for h in horizons:
        kdf[f"ret_{h}"] = kdf.groupby("symbol", sort=False)["close"].shift(-h) / kdf["close"] - 1.0
    ret_cols = [f"ret_{h}" for h in horizons]
    ret_df = kdf[kdf["date"] <= f"{end_compact[:4]}-{end_compact[4:6]}-{end_compact[6:]}"][
        ["symbol", "date"] + ret_cols]

    # ---- 因子面板：逐分区读取（容忍列集漂移），日期列优先 time，缺省用分区 dt
    frames = []
    skipped_parts = []
    date_col_name = None
    for dt, f in fparts:
        cols = list(pq.read_schema(f).names)
        if date_col_name is None and ("time" in cols or "date" in cols):
            date_col_name = "time" if "time" in cols else "date"
        missing = [c for c in factors if c not in cols]
        if missing:
            skipped_parts.append({"dt": dt, "missing": missing})
            continue
        use = ["symbol", date_col_name] if date_col_name else ["symbol"]
        df = pd.read_parquet(f, columns=use + factors)
        if date_col_name:
            df = df.rename(columns={date_col_name: "date"})
        else:                       # HK/US l1_factors 无日期列：日期取分区名
            df["date"] = f"{dt[:4]}-{dt[4:6]}-{dt[6:]}"
        df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
        frames.append(df[["symbol", "date", *factors]])
    if not frames:
        raise SystemExit(
            f"所有分区都缺请求的因子列 {factors}；用 --list-columns 查看可用列。"
            f"（首个缺列分区：{skipped_parts[:1]}）"
        )
    fac = pd.concat(frames, ignore_index=True)
    fac = fac.drop_duplicates(subset=["symbol", "date"], keep="first")
    fac = fac[fac["symbol"].isin(uset)]
    merged = fac.merge(ret_df, on=["symbol", "date"], how="inner")
    if merged.empty:
        raise SystemExit("因子面板与行情合并后为空（日期或 symbol 口径不一致？）")

    # ---- 逐因子组装（单因子数据不足不拖垮整批：记 stub 继续）
    reports = []
    failed = []
    n_symbols = merged["symbol"].nunique()
    for fcol in factors:
        sub = merged[["date", fcol] + ret_cols].dropna(subset=[fcol])
        rows = []
        for tup in sub.itertuples(index=False):
            d = tup[0]
            fv = float(tup[1])
            hvals = {f"H{h}": (float(tup[2 + i]) if not math.isnan(float(tup[2 + i])) else float("nan"))
                     for i, h in enumerate(horizons)}
            rows.append({"date": d, "f": fv, "h": hvals})
        caveats = []
        if skipped_parts:
            caveats.append(
                f"因子列在 {len(skipped_parts)} 个分区缺失（列集随年份漂移），这些分区被跳过："
                f"{skipped_parts[:3]}{'...' if len(skipped_parts) > 3 else ''}"
            )
        if dup_dropped:
            caveats.append(
                f"行情存在 {dup_dropped} 行重复 (symbol,date)（HK 双来源已知问题），"
                f"已按 published_at 取最新（akshare 原始价）去重。"
            )
        caveats.append(
            f"票池口径：{universe_info['method']} @ {universe_info['asof_date']}（"
            + ("事后选池，含轻微选择偏差" if not args.symbols else "显式指定") + "）。"
        )
        try:
            rep = build_factor_report(
                rows, name=fcol, horizons=[(f"H{h}", h) for h in horizons],
                window=args.window, nw_lag=args.nw_lag,
                extra={
                    "column": fcol,
                    "dataset": f"{market}/{dataset}",
                    "n_symbols": n_symbols,
                    "caveats": caveats,
                },
            )
            reports.append(rep)
        except ValueError as exc:
            failed.append({
                "name": fcol,
                "column": fcol,
                "dataset": f"{market}/{dataset}",
                "status": "insufficient_data",
                "reason": str(exc),
                "n_rows": len(rows),
            })
    if not reports:
        msg = "；".join(f"{x['name']}：{x['reason']}" for x in failed)
        raise SystemExit(f"所有因子都无法产出诊断：{msg}")

    wrapper = {
        "mode": "quantdb",
        "market": market,
        "dataset": dataset,
        "dataset_path": DATASET_PATHS[key],
        "data_root": str(root),
        "window": {"start": f"{start_compact[:4]}-{start_compact[4:6]}-{start_compact[6:]}",
                   "end": f"{end_compact[:4]}-{end_compact[4:6]}-{end_compact[6:]}"},
        "data_through": kparts[-1][0],
        "returns": {
            "source": KLINE_PATHS[market],
            "adjustment": KLINE_ADJUSTMENT_NOTE[market],
            "definition": "ret_h(t)=close(t+h)/close(t)-1，h=同一标的序列的后续第 h 个交易日",
        },
        "universe": universe_info,
        "horizons": horizons,
        "factors": reports,
        "failed_factors": failed,
    }
    return wrapper, reports, failed


# ------------------------------------------------------------------ 入口

def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="因子 IC 衰减与稳定性诊断（CSV 面板 / QuantDB 本地直读）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--input", help="长面板 CSV：date,symbol,factor,fwd_ret[，fwd_ret_{n}]")
    mode.add_argument("--quantdb", action="store_true", help="QuantDB 直读模式（容器内 pandas/pyarrow）")

    p.add_argument("--name", default=None, help="因子名称（CSV 模式）")
    p.add_argument("--market", default="CN", choices=["CN", "HK", "US"], help="市场（quantdb 模式，默认 CN）")
    p.add_argument("--dataset", default=None, help="因子数据集：CN=features_daily/l1_factors/l2_factors；HK/US=l1_factors")
    p.add_argument("--factors", default=None, help="因子列名，逗号分隔（quantdb 模式）")
    p.add_argument("--start", default=None, help="起始日 YYYY-MM-DD（默认：end 前推 2 年）")
    p.add_argument("--end", default=None, help="结束日 YYYY-MM-DD（默认：最新行情日）")
    p.add_argument("--top-n", type=int, default=300, help="按窗口末日成交额取前 N 只（默认 300；0=全部）")
    p.add_argument("--symbols", default=None, help="显式票池（后缀式逗号分隔），给定则忽略 --top-n")
    p.add_argument("--horizons", default="1,5,10,20", help="前视周期（交易日），默认 1,5,10,20")
    p.add_argument("--window", type=int, default=60, help="滚动/分段窗口（交易日，默认 60）")
    p.add_argument("--nw-lag", type=int, default=5, help="Newey-West lag（默认 5）")
    p.add_argument("--factor-col", default="factor", help="CSV 因子列名（默认 factor）")
    p.add_argument("--ret-col", default="fwd_ret", help="CSV 收益列名（默认 fwd_ret）")
    p.add_argument("--horizon-label", default="H", help="CSV 单周期模式的周期标签（默认 H）")
    p.add_argument("--out", default=None, help="JSON 报告输出路径（文件；父目录自动创建）")
    p.add_argument("--list-columns", action="store_true", help="列出数据集全部列并标注标签列后退出（quantdb 模式）")
    args = p.parse_args(argv)

    if args.list_columns:
        if not args.quantdb:
            raise SystemExit("--list-columns 仅在 --quantdb 模式下可用")
        return run_quantdb_list_columns(args)

    if args.input:
        wrapper, reports = run_csv(args)
        failed = []
    else:
        wrapper, reports, failed = run_quantdb(args)

    for i, rep in enumerate(reports):
        if i:
            print()
        print(render_text(rep))

    if failed:
        print("\n以下因子数据不足，未产出诊断（已写入 JSON 的 failed_factors）：")
        for x in failed:
            print(f"  - {x['name']}（非空行 {x['n_rows']}）：{x['reason']}")

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(json_safe(wrapper), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"\n已写入 JSON 报告：{out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
