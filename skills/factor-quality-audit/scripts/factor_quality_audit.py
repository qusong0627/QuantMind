#!/usr/bin/env python3
"""单因子质量裁决 — 离线确定性引擎 + QuantDB(CN/HK/US) 数据装配。

来源：quantskills/skill-factor-mason（GPL-3.0-only）的方法论本地化：
「任何因子先按嫌疑人处理，只有通过时点、样本、成本、暴露四道检查，
才允许被称作 alpha。」本脚本把该立场落成可执行裁决：

  ① 票池构造检查   覆盖 / 截面宽度 / 停牌零成交 / ST 与上市天数过滤可复现性
  ② 时点对齐检查   同日口径 vs 次日执行口径 IC，效力保留率（PIT）
  ③ IC/IR 简版     均值 IC、ICIR、Newey-West t、命中率、分段稳定性（不做衰减曲线）
  ④ 换手与成本     两端各 10% 等权构造；毛利 vs 扣费（单边万 3）与盈亏平衡成本
  ⑤ 中性化残差     行业哑变量 + 规模（FWL 两步法）后的 IC 保留率

  裁决 ∈ {alpha, 行业暴露, 泄漏, 样本幻觉}，每项给判据、证据数字与置信说明。
  判据阈值全部集中在文件顶部常量区，可用 CLI 覆盖（--leak-ic 等）。

三种模式：
  --demo            内置 4 组确定性合成面板（泄漏/噪声/行业暴露/真 alpha），
                    纯标准库，宿主机与 dsh 可直接跑；断言失败即退出码 1。
  --input <csv>     任意长面板复核：date,symbol,factor,fwd_ret（必需三列 + 收益列，
                    fwd_ret 缺省时若含 close 列则按 (symbol,date) 现算 1 日前向收益）；
                    可选列 industry、size、fwd_ret_lag1 解锁 ⑤ 与 ②。
  --quantdb         在 quantmind 容器内运行（pandas/pyarrow 延迟导入）：
                    因子列 ← <market>/6_ml_datasets/<dataset>（CN 默认 features_daily；
                    HK/US 为 l1_factors）；收益 ← 1_kline_data/daily_forward 自算
                    （CN 前复权；HK/US 不复权）；票池 ← 窗口末日成交额前 N 或 --symbols；
                    行业 ← CN instrument_detail.rs_hyname / HK akshare_profile.所属行业 /
                    US sector.sector；规模 ← ln_mv_total 或 total_mv（取对数）。

标签列护栏：return_{n}d / future_return_{n}d / label_return 是未来收益标签。
与做纯 IC 诊断的技能不同，本工具**不拒绝**标签列——故意传入时会把它裁决为「泄漏」，
这是质检工具的职责（名称规则 + |IC|≥--leak-ic 数据规则双重判定）。

用法：
  python3 factor_quality_audit.py --demo
  python3 factor_quality_audit.py --input panel.csv --name MOM20 --out report.json
  python3 factor_quality_audit.py --quantdb --market CN --factor ma_gap_20 \
      --start 2024-01-01 --end 2025-12-31 --top-n 300 \
      --out /data/reports/factor-quality-audit/cn_ma_gap_20.json

输出：stdout 中文裁决卡 + JSON 报告（--out 指定）。结论是研究证据描述，不是交易信号。
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import random
import re
import sys
from datetime import date as _date
from datetime import timedelta
from pathlib import Path

# ------------------------------------------------------------------ 判据阈值（集中配置，CLI 可覆盖）
ANNUALIZE = 252.0            # 日频年化因子
MIN_IC_OBS = 60              # 日度 IC 观测少于此数拒绝裁决
WARN_IC_OBS = 252            # 少于此数降置信（不足一年）
MIN_NAMES_PER_CROSS = 10     # 当日截面有效标的数 < 10 跳过该日
MIN_SPREAD_NAMES = 10        # 多空/成本检查最少截面宽度
DEFAULT_QUANTILE = 0.10      # 两端各取 10% 等权
COST_ONE_SIDE_BP = 3.0       # 单边成本 万3（撮合保守口径；真单券商口径万2.5，两者不同）
LEAK_IC = 0.90               # |均值 IC| ≥ 它按泄漏嫌疑处理（真实因子远低于此）
SIG_T = 2.0                  # Newey-West |t| 显著阈值
SIG_T_SOFT = 1.5             # 软化阈值（需同时满足 SIG_IC_SOFT）
SIG_IC_SOFT = 0.02
WEAK_IC = 0.005              # |均值 IC| 低于它视为不可区分噪声
NEUTRAL_RETENTION = 0.30     # 中性化后 IC 保留率 < 它 → 行业/规模暴露
PIT_RETENTION_OK = 0.70      # 次日执行口径保留率 ≥ 它视为通过
PIT_RETENTION_WARN = 0.30    # 低于它 → 执行依赖贴线成交，降置信
MIN_LIST_DAYS = 60           # 上市天数检查阈值（自然日）
MAX_SUSPENDED_PCT = 0.05     # 停牌/零成交行占比超过它 → 票池检查降级
MIN_MEDIAN_NAMES_UNIVERSE = 30
DEMO_SEED = 20261008

LABEL_COL_PATTERNS = [
    re.compile(r"^label_return$", re.I),
    re.compile(r"^return_\d+d$", re.I),
    re.compile(r"^future_return_\d+d$", re.I),
]

DEFAULT_CAVEATS = [
    "IC 与净收益都是历史证据描述，不是交易信号；裁决不预测未来。",
    "成本口径：单边 3 bp（撮合保守口径；真单券商口径 2.5 bp，两者不同）。",
    "本结果不构成投资建议。",
]

# ------------------------------------------------------------------ 数据路径（相对数据根目录）
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
INDUSTRY_PATHS = {
    "CN": "quantdb/2_base_sector/instrument_detail/instrument_detail.parquet",
    "HK": "quanthk/2_base_sector/akshare_profile",
    "US": "quantus/2_base_sector/sector",
}
DATA_ROOT_CANDIDATES = ["/data", "/quantmind/data", "/home/zbox/projects/quantmind/data"]
DEFAULT_DATASET = {"CN": "features_daily", "HK": "l1_factors", "US": "l1_factors"}
SIZE_COL_CANDIDATES = ["ln_mv_total", "total_mv", "fun_total_mv", "float_mv"]
KLINE_ADJUSTMENT_NOTE = {
    "CN": "daily_forward=前复权（收益已含分红送转调整）",
    "HK": "daily_forward=不复权原始价（收益未含分红调整）",
    "US": "daily_forward=不复权原始价（收益未含分红调整）",
}


def is_label_column(name: str) -> bool:
    return any(p.match(str(name).strip()) for p in LABEL_COL_PATTERNS)


# ------------------------------------------------------------------ 统计核心（纯标准库）

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
    n = len(xs)
    if n < MIN_NAMES_PER_CROSS or n != len(ys):
        return None
    rx = rankdata_average(xs)
    ry = rankdata_average(ys)
    mx = sum(rx) / n
    my = sum(ry) / n
    num = sx = sy = 0.0
    for a, b in zip(rx, ry, strict=False):
        da, db = a - mx, b - my
        num += da * db
        sx += da * da
        sy += db * db
    den = math.sqrt(sx * sy)
    if den <= 0:
        return None
    return max(-1.0, min(1.0, num / den))


def pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 3 or n != len(ys):
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sx = sy = 0.0
    for a, b in zip(xs, ys, strict=False):
        da, db = a - mx, b - my
        num += da * db
        sx += da * da
        sy += db * db
    den = math.sqrt(sx * sy)
    if den <= 0:
        return None
    return max(-1.0, min(1.0, num / den))


def newey_west_t(values: list[float], lag: int = 5) -> float:
    """均值是否显著异于 0 的 Newey-West t（Bartlett 核；日度 IC 序列自相关校正）。"""
    arr = [v for v in values if v is not None and not math.isnan(v)]
    n = len(arr)
    if n < max(lag + 2, 5):
        return float("nan")
    mean = sum(arr) / n
    u = [v - mean for v in arr]
    nw = sum(v * v for v in u) / n
    for j in range(1, int(lag) + 1):
        w = 1.0 - j / (lag + 1.0)
        gamma_j = sum(u[t] * u[t - j] for t in range(j, n)) / n
        nw += 2.0 * w * gamma_j
    if nw <= 0:
        return float("nan")
    se = math.sqrt(nw / n)
    return mean / se if se > 0 else float("nan")


def summarize_ic(ics: list[float], nw_lag: int = 5) -> dict:
    arr = [v for v in ics if v is not None and not math.isnan(v)]
    n = len(arr)
    if n == 0:
        return {"n": 0, "mean_ic": None, "std_ic": None, "icir_raw": None,
                "icir_ann": None, "hit_rate": None, "nw_t": None}
    mean = sum(arr) / n
    std = math.sqrt(sum((v - mean) ** 2 for v in arr) / (n - 1)) if n > 1 else None
    icir_raw = mean / std if std and std > 0 else None
    hit = sum(1 for v in arr if (v > 0) == (mean > 0)) / n
    nw = newey_west_t(arr, lag=nw_lag)
    return {
        "n": n, "mean_ic": mean, "std_ic": std, "icir_raw": icir_raw,
        "icir_ann": icir_raw * math.sqrt(ANNUALIZE) if icir_raw is not None else None,
        "hit_rate": hit, "nw_t": None if math.isnan(nw) else nw,
    }


def _finite(v) -> bool:
    return v is not None and isinstance(v, float) and math.isfinite(v)


# ------------------------------------------------------------------ 面板组织

def group_by_day(rows: list[dict]) -> dict[str, list[dict]]:
    by_day: dict[str, list[dict]] = {}
    for r in rows:
        by_day.setdefault(r["date"], []).append(r)
    return by_day


def daily_ic_series(by_day: dict[str, list[dict]], ret_key: str = "r") -> list[tuple[str, float]]:
    """逐日截面 Spearman IC；当日有效样本不足返回 None 的日被跳过。"""
    out = []
    for d in sorted(by_day):
        sec = [r for r in by_day[d]
               if r.get("f") is not None and r.get(ret_key) is not None
               and not math.isnan(r["f"]) and not math.isnan(r[ret_key])]
        ic = spearman([r["f"] for r in sec], [r[ret_key] for r in sec])
        if ic is not None:
            out.append((d, ic))
    return out


def neutralize_values(sec: list[dict], use_industry: bool, use_size: bool) -> list[float] | None:
    """对因子做行业哑变量 + 规模的正交化（FWL 两步：组内去均值 → 扣除规模投影）。"""
    n = len(sec)
    f = [r["f"] for r in sec]
    if use_industry:
        groups: dict[str, list[int]] = {}
        for i, r in enumerate(sec):
            groups.setdefault(str(r.get("industry")), []).append(i)
        f_ = [0.0] * n
        s_ = [0.0] * n if use_size else None
        for idxs in groups.values():
            mf = sum(f[i] for i in idxs) / len(idxs)
            for i in idxs:
                f_[i] = f[i] - mf
            if s_ is not None:
                ms = sum(sec[i]["size"] for i in idxs) / len(idxs)
                for i in idxs:
                    s_[i] = sec[i]["size"] - ms
    else:
        mf = sum(f) / n
        f_ = [v - mf for v in f]
        s_ = None
        if use_size:
            ms = sum(r["size"] for r in sec) / n
            s_ = [r["size"] - ms for r in sec]
    if s_ is not None:
        ss = sum(v * v for v in s_)
        if ss > 0:
            beta = sum(a * b for a, b in zip(f_, s_, strict=False)) / ss
            f_ = [a - beta * b for a, b in zip(f_, s_, strict=False)]
    return f_


def neutralized_ic(by_day: dict[str, list[dict]], cfg: dict) -> dict:
    """中性化后的日度 IC 序列 + 暴露诊断（行业方差解释、与规模相关）。"""
    per_day = []
    ind_r2 = []
    size_corr = []
    industry_used = size_used = False
    for d in sorted(by_day):
        sec = [r for r in by_day[d]
               if r.get("f") is not None and r.get("r") is not None
               and not math.isnan(r["f"]) and not math.isnan(r["r"])]
        has_ind = any(r.get("industry") for r in sec)
        has_size = any(r.get("size") is not None for r in sec)
        if has_ind and has_size:
            sec = [r for r in sec if r.get("industry") and r.get("size") is not None]
        elif has_ind:
            sec = [r for r in sec if r.get("industry")]
        elif has_size:
            sec = [r for r in sec if r.get("size") is not None]
        if len(sec) < MIN_NAMES_PER_CROSS or (not has_ind and not has_size):
            continue
        industry_used = industry_used or has_ind
        size_used = size_used or has_size
        if has_ind:
            f = [r["f"] for r in sec]
            groups: dict[str, list[float]] = {}
            for r in sec:
                groups.setdefault(str(r["industry"]), []).append(r["f"])
            mf = sum(f) / len(f)
            ss_tot = sum((v - mf) ** 2 for v in f)
            ss_between = sum(len(v) * (sum(v) / len(v) - mf) ** 2 for v in groups.values())
            if ss_tot > 0:
                ind_r2.append(ss_between / ss_tot)
        if has_size:
            sc = pearson([r["f"] for r in sec], [r["size"] for r in sec])
            if sc is not None:
                size_corr.append(sc)
        resid = neutralize_values(sec, has_ind, has_size)
        ic = spearman(resid, [r["r"] for r in sec])
        if ic is not None:
            per_day.append((d, ic))
    return {
        "enabled": industry_used or size_used,
        "industry_used": industry_used,
        "size_used": size_used,
        "series": per_day,
        "industry_r2_median": _median(ind_r2),
        "size_corr_median": _median(size_corr),
    }


def _median(vals: list[float]) -> float | None:
    if not vals:
        return None
    s = sorted(vals)
    m = len(s) // 2
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2.0


def spread_and_cost(by_day: dict[str, list[dict]], quantile: float, cost_bp: float,
                    direction: int, ret_key: str = "r") -> dict:
    """两端各 quantile 等权、日频调仓：可执行端超额 / 理论多空 的毛与净。"""
    gross_side, gross_ls, cost_side, cost_ls = [], [], [], []
    turn_side, turn_ls = [], []
    prev: dict[str, set] | None = None
    round_trip = 2.0 * cost_bp / 1e4
    for d in sorted(by_day):
        sec = [r for r in by_day[d]
               if r.get("f") is not None and r.get(ret_key) is not None
               and not math.isnan(r["f"]) and not math.isnan(r[ret_key])]
        if len(sec) < MIN_SPREAD_NAMES:
            continue
        vals = sorted(sec, key=lambda r: r["f"])
        k = max(1, int(round(quantile * len(vals))))
        bottom, top = vals[:k], vals[-k:]
        mean_all = sum(r[ret_key] for r in vals) / len(vals)
        mean_top = sum(r[ret_key] for r in top) / k
        mean_bot = sum(r[ret_key] for r in bottom) / k
        side = top if direction >= 0 else bottom
        # 可执行端超额 = 该端相对全票池等权的真实盈亏（不做方向翻转，负值即该端无超额）
        gross_side.append(sum(r[ret_key] for r in side) / k - mean_all)
        # 理论多空 = 按信号方向持有多空两端（负 IC 因子即反向：多低分位、空高分位）
        gross_ls.append(direction * (mean_top - mean_bot))
        cur = {"top": {r["symbol"] for r in top}, "bottom": {r["symbol"] for r in bottom},
               "side": {r["symbol"] for r in side}}
        if prev is None:
            t_top = t_bot = t_side = 0.0
        else:
            t_top = 1.0 - len(cur["top"] & prev["top"]) / max(len(prev["top"]), 1)
            t_bot = 1.0 - len(cur["bottom"] & prev["bottom"]) / max(len(prev["bottom"]), 1)
            t_side = 1.0 - len(cur["side"] & prev["side"]) / max(len(prev["side"]), 1)
        turn_side.append(t_side)
        turn_ls.append((t_top + t_bot) / 2.0)
        cost_side.append(t_side * round_trip)
        cost_ls.append((t_top + t_bot) * round_trip)
        prev = cur
    if not gross_side:
        return {"n_days": 0}
    m = lambda xs: sum(xs) / len(xs)  # noqa: E731
    mean_turn_side = m(turn_side)
    mean_turn_ls = m(turn_ls)
    return {
        "n_days": len(gross_side),
        "gross_side_bp": m(gross_side) * 1e4,
        "cost_side_bp": m(cost_side) * 1e4,
        "net_side_bp": (m(gross_side) - m(cost_side)) * 1e4,
        "gross_ls_bp": m(gross_ls) * 1e4,
        "cost_ls_bp": m(cost_ls) * 1e4,
        "net_ls_bp": (m(gross_ls) - m(cost_ls)) * 1e4,
        "turnover_side_pct": mean_turn_side * 100.0,
        "turnover_ls_pct": mean_turn_ls * 100.0,
        "breakeven_side_one_side_bp": (
            m(gross_side) / (2.0 * mean_turn_side) * 1e4 if mean_turn_side > 0 else None),
        "net_side_ann_pct": (m(gross_side) - m(cost_side)) * ANNUALIZE * 100.0,
        "round_trip_note": f"单边 {cost_bp:.1f} bp × 2（卖出旧 + 买入新）",
    }


def segment_stats(ic_pairs: list[tuple[str, float]], n_seg: int = 3) -> list[dict]:
    ds = [d for d, _ in ic_pairs]
    vs = [v for _, v in ic_pairs]
    if len(vs) < n_seg * 5:
        n_seg = 1
    out = []
    size = max(1, len(vs) // n_seg)
    for i in range(0, len(vs), size):
        seg = vs[i:i + size]
        if len(seg) < 5:
            continue
        out.append({"start": ds[i], "end": ds[i + len(seg) - 1], "n": len(seg),
                    "mean_ic": sum(seg) / len(seg)})
    return out


# ------------------------------------------------------------------ 裁决

def _significance(s: dict, cfg: dict) -> tuple[bool, str]:
    t = abs(s["nw_t"]) if s["nw_t"] is not None else 0.0
    ic = abs(s["mean_ic"]) if s["mean_ic"] is not None else 0.0
    if t >= cfg["sig_t"]:
        return True, f"NW-t={t:.2f}≥{cfg['sig_t']}"
    if t >= cfg["sig_t_soft"] and ic >= cfg["sig_ic_soft"]:
        return True, f"NW-t={t:.2f}≥{cfg['sig_t_soft']} 且 |IC|={ic:.3f}≥{cfg['sig_ic_soft']}"
    return False, f"NW-t={t:.2f}、|IC|={ic:.4f} 均未过线"


def _downgrade(conf: str) -> str:
    return {"高": "中", "中": "低", "低": "低"}[conf]


def adjudicate(name: str, ic: dict, neutral: dict, pit: dict, cost: dict, cfg: dict,
               column: str | None = None) -> dict:
    mean_ic = ic.get("mean_ic")
    n_ic = ic.get("n") or 0
    label_hit = is_label_column(column or name) or (column is not None and is_label_column(name))
    criteria, evidence, caveats = [], [], []
    direction = 1 if (mean_ic or 0.0) >= 0 else -1

    # ① 泄漏（名称规则 + 数据规则）
    if label_hit:
        hit = column if (column is not None and is_label_column(column)) else name
        return _verdict("leakage", "高", f"因子列名 `{hit}` 命中未来收益标签黑名单"
                        "（return_{n}d / future_return_{n}d / label_return）",
                        ["名称规则：标签列 = 未来收益本身"], [], direction, cfg)
    if mean_ic is not None and abs(mean_ic) >= cfg["leak_ic"]:
        return _verdict("leakage", "高",
                        f"|均值 IC|={abs(mean_ic):.3f} ≥ {cfg['leak_ic']}：截面排序几乎复制了未来收益本身",
                        [f"数据规则：|IC| ≥ {cfg['leak_ic']}"],
                        ["先排查数据管道：因子列是否混入了标签/未来价格/未来财报字段。"], direction, cfg)

    # ② 样本幻觉（显著性不足）
    sig, why = _significance(ic, cfg)
    criteria.append("显著性判据：" + why)
    if not sig or (mean_ic is not None and abs(mean_ic) < cfg["weak_ic"]):
        conf = "高" if (n_ic >= WARN_IC_OBS and abs(ic.get("nw_t") or 0) < 1.0) else "中"
        ev = [f"均值 IC={_fmt(mean_ic, 4, signed=True)}，NW-t={_fmt(ic.get('nw_t'), 2, signed=True)}，"
              f"ICIR年化={_fmt(ic.get('icir_ann'), 2, signed=True)}，n={n_ic}"]
        caveats.append("该裁决只说明「在现有窗口与票池下与噪声不可区分」，"
                       "不等于因子在经济逻辑上一定无效；可换窗口/票池重验。")
        seg = segment_stats(ic.get("pairs") or [])
        neg = sum(1 for s in seg if s["mean_ic"] * direction < 0)
        if seg:
            ev.append(f"{len(seg)} 段中 {neg} 段与全样本方向相反")
        return _verdict("sample_illusion", conf, "效应量与显著性都过不了线，"
                        "当前证据无法把该因子与随机噪声区分开", criteria, ev + caveats, direction, cfg)

    # 显著：进入暴露 / 成本检查
    retention = neutral.get("retention")
    if neutral.get("enabled") and neutral.get("series"):
        ic_neu = neutral["summary"]
        evidence.append(f"中性化后 IC={_fmt(ic_neu['mean_ic'], 4, signed=True)}，"
                        f"保留率={_fmt(retention, 2)}（行业方差解释中位 "
                        f"{_fmt((neutral.get('industry_r2_median') or 0) * 100, 1)}%，"
                        f"与规模相关中位 {_fmt(neutral.get('size_corr_median'), 2)}）")
    if retention is not None and retention < cfg["neutral_retention"]:
        conf = "高" if retention < cfg["neutral_retention"] / 2 else "中"
        ev = [f"原始 IC={_fmt(mean_ic, 4, signed=True)}（显著），中性化后仅剩 "
              f"{_fmt(retention * 100, 1)}%（阈值 {cfg['neutral_retention'] * 100:.0f}%）"]
        ev += evidence
        caveats.append("若行业分类或规模口径有限（快照/静态），暴露归因也可能被低估或高估；"
                       "建议用逐日子行业口径复核。")
        return _verdict("industry_exposure", conf,
                        "信号的截面区分度主要来自行业（或规模）分组差异，"
                        "而不是行业内的个股排序", criteria, ev + caveats, direction, cfg)

    # 通过暴露检查 → 成本闸门决定 alpha 置信
    conf = "高"
    ev = [f"均值 IC={_fmt(mean_ic, 4, signed=True)}，NW-t={_fmt(ic.get('nw_t'), 2, signed=True)}，"
          f"ICIR年化={_fmt(ic.get('icir_ann'), 2, signed=True)}，n={n_ic}"] + evidence
    if n_ic < WARN_IC_OBS:
        conf = _downgrade(conf)
        caveats.append(f"IC 观测仅 {n_ic} 天（<252），样本偏少。")
    if not neutral.get("enabled"):
        conf = _downgrade(conf)
        caveats.append("行业/规模数据缺失，中性化检查未执行——行业暴露可能性未被排除。")
    if pit.get("retention") is not None:
        if pit["retention"] < cfg["pit_retention_warn"]:
            conf = _downgrade(conf)
            caveats.append(f"次日执行口径 IC 保留率仅 {pit['retention']:.0%}：alpha 高度依赖"
                           "贴近日收盘成交，实际执行延迟会显著侵蚀。")
        elif pit["retention"] < cfg["pit_retention_ok"]:
            conf = _downgrade(conf)
            caveats.append(f"次日执行口径保留 {pit['retention']:.0%}，存在轻度执行衰减。")
    elif pit.get("retention_note"):
        caveats.append(pit["retention_note"])
    if cost.get("n_days"):
        gross = cost["gross_side_bp"]
        net = cost["net_side_bp"]
        side_zh = "高" if direction > 0 else "低"
        if gross <= 0:
            conf = _downgrade(conf)
            caveats.append(
                f"可执行端（{side_zh}分位）毛超额 {gross:.1f} bp/日 ≤ 0：统计上有效的排序信息没有"
                "落实到可执行的极端分位上（秩 IC 与尾部价差可反号，或尾部结构不同）——"
                "只看单边多头口径时不成立，先复核尾部构造再谈成本。")
        elif net <= 0:
            conf = _downgrade(conf)
            caveats.append(f"可执行端毛 {gross:.1f} bp/日 > 0 但扣费后 {net:.1f} bp/日 ≤ 0："
                           f"统计上有效的信号在单边 {cfg['cost_bp']:.1f} bp 成本下不可行"
                           f"（盈亏平衡单边成本 {_fmt(cost.get('breakeven_side_one_side_bp'), 1)} bp）。")
        ev.append(f"可执行端（{side_zh}分位）：毛 {gross:.1f} bp/日，"
                  f"换手 {cost['turnover_side_pct']:.1f}%/日，净 {net:.1f} bp/日"
                  f"（{cost['n_days']} 个交易日）；理论多空净 {cost['net_ls_bp']:.1f} bp/日")
    seg = segment_stats(ic.get("pairs") or [])
    if seg and any(s["mean_ic"] * direction < 0 for s in seg):
        conf = _downgrade(conf)
        caveats.append("存在与全样本方向相反的分段，稳定性打折。")
    caveats.append("A股/港股做空受限：理论多空组合仅作强度参考，可执行口径看单边。")
    caveats.append("票池为窗口末日成交额事后选池（如适用），含轻微选择偏差。")
    return _verdict("alpha", conf,
                    "通过时点/样本/成本/暴露四道检查（阈值内），"
                    "行业与规模中性化后 IC 仍有保留", criteria, ev + caveats, direction, cfg)


def _verdict(label: str, conf: str, reason: str, criteria: list[str],
             evidence: list[str], direction: int, cfg: dict) -> dict:
    zh = {"leakage": "泄漏（疑似未来函数）", "sample_illusion": "样本幻觉（与噪声不可区分）",
          "industry_exposure": "行业暴露（信号主要是行业/规模下注）",
          "alpha": "alpha（截面选股信号）"}[label]
    return {
        "label": label,
        "label_zh": zh,
        "confidence": conf,
        "direction": "正向" if direction >= 0 else "负向",
        "direction_note": ("因子值越大 → 未来收益越高" if direction >= 0
                           else "因子值越大 → 未来收益越低（可执行多头端 = 低分位）"),
        "reason": reason,
        "criteria": criteria,
        "evidence": evidence,
        "thresholds": dict(cfg),
    }


# ------------------------------------------------------------------ 面板审计主流程

def audit_panel(rows: list[dict], name: str, cfg: dict, universe: dict | None = None,
                column: str | None = None) -> dict:
    if not rows:
        raise ValueError(f"因子 {name} 无有效面板行")
    by_day = group_by_day(rows)
    ic_pairs = daily_ic_series(by_day, "r")
    ic = summarize_ic([v for _, v in ic_pairs])
    ic["pairs"] = ic_pairs
    n_ic = ic["n"]
    if n_ic < MIN_IC_OBS:
        uniq = len({round(r["f"], 12) for r in rows if r.get("f") is not None})
        raise ValueError(
            f"因子 {name} 日度 IC 观测 {n_ic} < {MIN_IC_OBS}，拒绝裁决。"
            f"（窗口内非空行 {len(rows)}，因子取值去重 {uniq}——若 ≈1 说明是常量/未填充列）"
        )
    ic["low_sample_warning"] = n_ic < WARN_IC_OBS

    # 时点对齐：同日口径（因子日收盘起算） vs 次日执行口径
    pit = {"retention": None, "ic_same_day": ic["mean_ic"]}
    if any(r.get("r_lag") is not None for r in rows):
        lag_pairs = daily_ic_series(by_day, "r_lag")
        lag_sum = summarize_ic([v for _, v in lag_pairs])
        pit["ic_next_day_start"] = lag_sum["mean_ic"]
        pit["n"] = lag_sum["n"]
        if (lag_sum["mean_ic"] is not None and ic["mean_ic"] not in (None, 0.0)
                and abs(ic["mean_ic"]) >= cfg.get("weak_ic", WEAK_IC)):
            pit["retention"] = lag_sum["mean_ic"] / ic["mean_ic"]
        else:
            pit["retention_note"] = ("同日口径 IC 近零（|IC| < 弱阈值），保留率为比值噪声不展示；"
                                     "看两口径 IC 并列值即可")
    else:
        pit["note"] = "无次日口径收益列（fwd_ret_lag1 / 行情不足以计算），时点对齐检查仅覆盖同日口径"

    neutral = neutralized_ic(by_day, cfg)
    direction = 1 if (ic["mean_ic"] or 0.0) >= 0 else -1
    if neutral.get("enabled") and neutral.get("series"):
        ic_neu = summarize_ic([v for _, v in neutral["series"]])
        neutral["summary"] = ic_neu
        if (ic_neu["mean_ic"] is not None and ic["mean_ic"] not in (None, 0.0)
                and abs(ic["mean_ic"]) > 1e-9):
            neutral["retention"] = ic_neu["mean_ic"] / ic["mean_ic"]
    cost_key = "r_cost" if any(r.get("r_cost") is not None for r in rows) else "r"
    cost = spread_and_cost(by_day, cfg["quantile"], cfg["cost_bp"], direction, ret_key=cost_key)

    verdict = adjudicate(name, ic, neutral, pit, cost, cfg, column=column)
    ic_out = {k: v for k, v in ic.items() if k != "pairs"}
    ic_out["segments"] = segment_stats(ic_pairs)
    report = {
        "name": name,
        "n_days": len(by_day),
        "date_start": min(by_day),
        "date_end": max(by_day),
        "universe": universe or {"method": "as_given"},
        "checks": {
            "universe": universe or {"status": "not_checked"},
            "pit": pit,
            "ic": ic_out,
            "turnover_cost": cost,
            "neutralization": {k: v for k, v in neutral.items() if k != "series"},
        },
        "verdict": verdict,
        "caveats": list(DEFAULT_CAVEATS),
    }
    return report


# ------------------------------------------------------------------ CSV 模式（纯标准库）

def _norm_date(raw) -> str | None:
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
        except ValueError:
            return None
    return None


def _to_float(raw):
    try:
        v = float(str(raw).strip())
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def load_csv_panel(path: str, factor_col: str | None,
                   ret_col: str | None = None) -> tuple[list[dict], str, list[str]]:
    with open(path, encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        fields = reader.fieldnames or []
        raw = list(reader)
    lower = {c.lower().strip(): c for c in fields}

    def pick(*names):
        for n in names:
            if n.lower() in lower:
                return lower[n.lower()]
        return None

    dcol = pick("date", "日期", "trade_date", "time")
    scol = pick("symbol", "code", "ticker", "股票代码", "证券代码")
    fcol = pick(factor_col) if factor_col else pick("factor", "signal", "alpha", "因子")
    if not dcol or not scol or not fcol:
        raise ValueError(f"CSV 需含 date/symbol/factor 列，实际列：{fields}")
    rcol = pick(ret_col) if ret_col else pick("fwd_ret", "forward_return", "ret_1d", "ret", "收益")
    lagcol = pick("fwd_ret_lag1", "fwd_ret_next", "ret_lag1")
    ccol = pick("close") if not rcol else None
    indcol = pick("industry", "行业", "sector")
    szcol = pick("size", "市值", "market_cap", "total_mv", "ln_mv_total")
    if not rcol and not ccol:
        raise ValueError(f"CSV 需含 fwd_ret（或 close 现算 1 日前向收益）列，实际列：{fields}")

    notes = []
    if not rcol and ccol:
        notes.append("无 fwd_ret 列，用 close 按 (symbol,date) 排序现算 1 日前向收益")
    if not indcol:
        notes.append("无 industry 列 → 行业中性化未执行")
    if not szcol:
        notes.append("无 size 列 → 规模中性化未执行")

    rows: list[dict] = []
    closes: dict[str, list[tuple[str, float]]] = {}
    for raw_row in raw:
        d = _norm_date(raw_row.get(dcol))
        if d is None:
            continue
        fv = _to_float(raw_row.get(fcol))
        if fv is None:
            continue
        sym = str(raw_row.get(scol, "")).strip()
        rv = _to_float(raw_row.get(rcol)) if rcol else None
        if rcol is None:
            cv = _to_float(raw_row.get(ccol))
            if cv is not None:
                closes.setdefault(sym, []).append((d, cv))
        size = _to_float(raw_row.get(szcol)) if szcol else None
        if size is not None and szcol.lower() not in ("ln_mv_total", "log_size"):
            size = math.log(max(size, 1e-9))
        rows.append({
            "date": d, "symbol": sym, "f": fv, "r": rv,
            "r_lag": _to_float(raw_row.get(lagcol)) if lagcol else None,
            "industry": (str(raw_row.get(indcol)).strip() or None) if indcol else None,
            "size": size,
        })
    if rcol is None:
        for sym, pts in closes.items():
            pts.sort()
            fwd = {d: (pts[i + 1][1] / pts[i][1] - 1.0) for i, (d, _) in enumerate(pts) if i + 1 < len(pts)}
            for r in rows:
                if r["symbol"] == sym:
                    r["r"] = fwd.get(r["date"])
    rows = [r for r in rows if r["r"] is not None]
    if not rows:
        raise ValueError("CSV 无有效数据行（date/symbol/factor/fwd_ret 均需可解析）")
    return rows, fcol, notes


def run_csv(args, cfg) -> dict:
    rows, fcol, notes = load_csv_panel(args.input, args.factor_col, args.ret_col)
    by_day = group_by_day(rows)
    widths = sorted(len(v) for v in by_day.values())
    universe = {
        "method": f"csv:{Path(args.input).name}",
        "asof_date": max(by_day),
        "n_symbols": len({r["symbol"] for r in rows}),
        "cross_section": {"median_names": widths[len(widths) // 2], "min_names": widths[0],
                          "days_total": len(by_day)},
        "status": "partial",
        "findings": notes + ["CSV 模式不做停牌/ST/上市天数检查（面板已定型）"],
    }
    report = audit_panel(rows, args.name or fcol, cfg, universe=universe,
                         column=args.factor_col or fcol)
    report.update({"mode": "csv", "input": str(Path(args.input).resolve()), "factor_column": fcol})
    if is_label_column(fcol):
        report["label_column_note"] = "因子列名命中未来收益标签黑名单"
    report["caveats"] = [
        "fwd_ret 口径由输入方定义；成本检查假定日频调仓且 fwd_ret 为 1 日前向收益。",
        "CSV 面板的票池构造与时点对齐不可由本工具复核（数据已定型）。",
    ] + report["caveats"]
    return report


# ------------------------------------------------------------------ QuantDB 模式（容器 pandas）

def resolve_data_root() -> Path:
    env = os.environ.get("QM_DATA_ROOT")
    cands = ([Path(env)] if env else []) + [Path(p) for p in DATA_ROOT_CANDIDATES]
    for c in cands:
        if (c / "quantdb").is_dir():
            return c
    raise SystemExit(f"找不到数据根目录（需含 quantdb/）：{[str(c) for c in cands]}，可用 QM_DATA_ROOT 覆盖")


def _list_dt_partitions(base: Path, start: str, end: str) -> list[tuple[str, Path]]:
    if not base.is_dir():
        raise SystemExit(f"目录不存在：{base}")
    return [(p.name[3:], p / "data.parquet")
            for p in sorted(base.glob("dt=*")) if start <= p.name[3:] <= end]


def _load_industry(root: Path, market: str, symbols: list[str]) -> dict[str, str]:
    """行业映射：CN=instrument_detail.rs_hyname（静态快照）；HK=akshare_profile.所属行业；US=sector.sector。"""
    import pandas as pd  # noqa: PLC0415
    out: dict[str, str] = {}
    if market == "CN":
        f = root / INDUSTRY_PATHS["CN"]
        if not f.exists():
            return out
        d = pd.read_parquet(f, columns=["Symbol", "rs_hyname"])
        for sym, ind in zip(d["Symbol"].astype(str), d["rs_hyname"], strict=False):
            if isinstance(ind, str) and ind.strip():
                out[sym] = ind.strip()
    else:
        base = root / INDUSTRY_PATHS[market]
        col = "所属行业" if market == "HK" else "sector"
        for sym in symbols:
            f = base / f"{sym}.parquet"
            if not f.exists():
                continue
            try:
                d = pd.read_parquet(f)
            except Exception:                     # noqa: BLE001 单文件损坏不拖垮整批
                continue
            if col in d.columns and len(d) and isinstance(d[col].iloc[0], str):
                v = d[col].iloc[0].strip()
                if v:
                    out[sym] = v
    return out


def run_quantdb(args, cfg) -> dict:
    import pandas as pd            # noqa: PLC0415
    import pyarrow.parquet as pq   # noqa: PLC0415

    root = resolve_data_root()
    market = args.market.upper()
    dataset = args.dataset or DEFAULT_DATASET[market]
    if (market, dataset) not in DATASET_PATHS:
        raise SystemExit(f"市场 {market} 无数据集 {dataset}")
    factor_dir = root / DATASET_PATHS[(market, dataset)]
    kline_dir = root / KLINE_PATHS[market]
    h = args.horizon

    # ---- 窗口
    kparts_all = sorted(kline_dir.glob("dt=*"))
    if not kparts_all:
        raise SystemExit(f"行情目录无分区：{kline_dir}")
    end_compact = args.end.replace("-", "") if args.end else kparts_all[-1].name[3:]
    if args.start:
        start_compact = args.start.replace("-", "")
    else:
        d0 = _date.fromisoformat(f"{end_compact[:4]}-{end_compact[4:6]}-{end_compact[6:]}")
        start_compact = d0.replace(year=d0.year - 2).strftime("%Y%m%d")
    kparts = _list_dt_partitions(kline_dir, start_compact, end_compact)
    if not kparts:
        raise SystemExit(f"行情无 {start_compact}~{end_compact} 分区")
    tail = [(p.name[3:], p / "data.parquet")
            for p in kparts_all if p.name[3:] > end_compact][: h + 3]
    fparts = _list_dt_partitions(factor_dir, start_compact, end_compact)
    if not fparts:
        raise SystemExit(f"因子数据集无 {start_compact}~{end_compact} 分区：{factor_dir}")

    # ---- 票池
    universe_findings: list[str] = []
    last_dt, last_file = kparts[-1]
    if args.symbols:
        universe = [s.strip() for s in args.symbols.split(",") if s.strip()]
        umeta = {"method": "explicit_symbols", "asof_date": last_dt, "n_symbols": len(universe)}
    else:
        u = pd.read_parquet(last_file, columns=["symbol", "amount"]).dropna(subset=["amount"])
        u = u[u["amount"] > 0].sort_values("amount", ascending=False)
        take = args.top_n if args.top_n and args.top_n > 0 else len(u)
        universe = u["symbol"].head(take).astype(str).tolist()
        umeta = {"method": f"top_{take}_by_amount", "asof_date": last_dt, "n_symbols": len(universe)}
        universe_findings.append("票池=窗口末日成交额事后选池（含轻微选择偏差，非逐日动态池）")
    uset = set(universe)

    # ---- 行情：窗口 + 前视尾部，算同日/次日两种口径的前向收益
    def _read_kline(dt: str, f: Path) -> pd.DataFrame:
        cols = [c for c in ("symbol", "time", "close", "volume", "amount", "published_at")
                if c in pq.read_schema(f).names]
        df = pd.read_parquet(f, columns=cols)
        df["date"] = pd.to_datetime(df["time"]).dt.strftime("%Y-%m-%d")
        return df

    kdf = pd.concat([_read_kline(dt, f) for dt, f in (kparts + tail)], ignore_index=True)
    kdf = kdf.dropna(subset=["close"])
    before = len(kdf)
    if "published_at" in kdf.columns:
        kdf = (kdf.sort_values(["symbol", "date", "published_at"], kind="stable")
               .drop_duplicates(subset=["symbol", "date"], keep="last"))
        kdf = kdf.drop(columns=["published_at"])
    else:
        kdf = kdf.drop_duplicates(subset=["symbol", "date"], keep="first")
    dup_dropped = before - len(kdf)
    if dup_dropped:
        universe_findings.append(
            f"行情 {dup_dropped} 行重复 (symbol,date)（HK 双来源已知问题），已按 published_at 保留 akshare")
    kdf = kdf[kdf["symbol"].isin(uset)].sort_values(["symbol", "date"])
    if kdf.empty:
        raise SystemExit("票池在行情窗口内无数据（symbol 格式：CN=000001.SZ / HK=0001.HK / US=NVDA）")
    win_end = f"{end_compact[:4]}-{end_compact[4:6]}-{end_compact[6:]}"
    g = kdf.groupby("symbol", sort=False)["close"]
    kdf["r"] = g.shift(-h) / kdf["close"] - 1.0
    kdf["r_lag"] = g.shift(-(h + 1)) / g.shift(-1) - 1.0
    kdf["r1"] = g.shift(-1) / kdf["close"] - 1.0
    ret_df = kdf[kdf["date"] <= win_end][["symbol", "date", "r", "r_lag", "r1"]]

    # ---- 票池质检（停牌/覆盖/宽度在合并后统计）
    n_days_win = ret_df["date"].nunique()
    n_sym_win = ret_df["symbol"].nunique()
    sus = kdf[(kdf["date"] <= win_end)]
    sus_n = int(((sus.get("volume", pd.Series(dtype=float)).fillna(0) == 0)
                 | (sus.get("amount", pd.Series(dtype=float)).fillna(0) == 0)).sum())
    coverage = len(sus) / max(n_sym_win * n_days_win, 1)

    # ---- 因子面板（容忍列集漂移与缺失）
    cols_last = list(pq.read_schema(fparts[-1][1]).names)
    if args.factor not in cols_last:
        raise SystemExit(f"数据集 {market}/{dataset} 最新分区无列 {args.factor}；"
                         "可用 --dataset 切换或检查列名（HK/US 部分基本面列实测全 0 未填充）")
    size_col = None
    for c in SIZE_COL_CANDIDATES:
        if c in cols_last:
            size_col = c
            break
    frames, skipped = [], 0
    for dt, f in fparts:
        cols = list(pq.read_schema(f).names)
        if args.factor not in cols:
            skipped += 1
            continue
        date_col = "time" if "time" in cols else ("date" if "date" in cols else None)
        use = ["symbol"] + ([date_col] if date_col else []) + [args.factor]
        if size_col and size_col in cols:
            use.append(size_col)
        df = pd.read_parquet(f, columns=use)
        if date_col:
            df = df.rename(columns={date_col: "date"})
        else:
            df["date"] = f"{dt[:4]}-{dt[4:6]}-{dt[6:]}"
        df = df.rename(columns={args.factor: "f"})
        if size_col and size_col in df.columns:
            df = df.rename(columns={size_col: "size"})
        else:
            df["size"] = None
        df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
        frames.append(df[["symbol", "date", "f", "size"]])
    if not frames:
        raise SystemExit(f"所有分区缺因子列 {args.factor}")
    fac = pd.concat(frames, ignore_index=True).drop_duplicates(subset=["symbol", "date"], keep="first")
    fac = fac[fac["symbol"].isin(uset) & (fac["date"] <= win_end)]
    merged = fac.merge(ret_df, on=["symbol", "date"], how="inner")
    if merged.empty:
        raise SystemExit("因子面板与行情合并后为空（日期或 symbol 口径不一致？）")

    # 规模的量纲统一：ln_ 前缀直接用，其余取自然对数
    if size_col:
        if not size_col.lower().startswith(("ln_", "log_")):
            merged["size"] = merged["size"].where(merged["size"] > 0).map(
                lambda v: math.log(v) if v is not None and v == v else None)
        if merged["size"].notna().sum() < 0.5 * len(merged) or merged["size"].nunique() <= 1:
            universe_findings.append(f"规模列 {size_col} 实测近全空/恒定（当地已知未填充），规模中性化未执行")
            merged["size"] = None

    # ---- 行业映射
    ind_map = _load_industry(root, market, universe)
    merged["industry"] = merged["symbol"].map(ind_map)
    ind_cov = float(merged["industry"].notna().mean()) if len(merged) else 0.0
    if ind_cov < 0.3:
        universe_findings.append(f"行业映射覆盖仅 {ind_cov:.0%}，行业中性化解释力有限")
        merged["industry"] = None
    else:
        universe_findings.append(f"行业映射覆盖 {ind_cov:.0%}（{len(ind_map)} 键）")

    # ---- 成本检查固定用 1 日前向收益（日频调仓）
    rows = []
    for sym, date, fv, sz, ind, r, r_lag, r1 in zip(
            merged["symbol"], merged["date"], merged["f"], merged["size"],
            merged["industry"], merged["r"], merged["r_lag"], merged["r1"], strict=False):
        if fv is None or fv != fv:
            continue
        rows.append({
            "date": date, "symbol": sym, "f": float(fv),
            "r": None if r != r else float(r),
            "r_lag": None if r_lag != r_lag else float(r_lag),
            "r_cost": None if r1 != r1 else float(r1),
            "industry": ind if isinstance(ind, str) else None,
            "size": float(sz) if sz is not None and sz == sz else None,
        })
    rows = [r for r in rows if r["r"] is not None]

    widths = sorted(len(v) for v in group_by_day(rows).values())
    st_info, list_info = _universe_meta_cn(root, market, universe)
    universe_meta = {
        **umeta,
        "cross_section": {"median_names": _median([float(w) for w in widths]),
                          "min_names": widths[0] if widths else 0,
                          "days_total": len(widths)},
        "coverage_pct": coverage * 100.0,
        "suspended_like": {"rows": sus_n, "pct": sus_n / max(len(sus), 1) * 100.0},
        "st_filter": st_info,
        "listing_age": list_info,
        "findings": universe_findings,
    }
    st_info_ok = st_info.get("available")
    list_ok = list_info.get("available")
    med = universe_meta["cross_section"]["median_names"] or 0
    if coverage < 0.5 or med < MIN_MEDIAN_NAMES_UNIVERSE / 3:
        universe_meta["status"] = "fail"
    elif (not st_info_ok) or (not list_ok) or universe_meta["suspended_like"]["pct"] > MAX_SUSPENDED_PCT * 100:
        universe_meta["status"] = "partial"
    else:
        universe_meta["status"] = "pass"

    report = audit_panel(rows, args.factor, cfg, universe=universe_meta, column=args.factor)
    report.update({
        "mode": "quantdb",
        "market": market,
        "dataset": f"{market}/{dataset}",
        "window": {"start": f"{start_compact[:4]}-{start_compact[4:6]}-{start_compact[6:]}",
                   "end": win_end},
        "horizon": h,
        "returns": {"source": KLINE_PATHS[market], "adjustment": KLINE_ADJUSTMENT_NOTE[market],
                    "definition": f"同日口径 ret=close(t+{h})/close(t)-1；"
                                  f"次日执行口径 ret=close(t+{h + 1})/close(t+1)-1"},
        "factor_column": args.factor,
        "size_column": size_col,
    })
    report["caveats"] = [
        (f"主 IC 口径 H={h}；换手/成本检查固定按 1 日前向收益与日频调仓近似。"
         if h != 1 else "IC 与成本检查同为 1 日前向收益、日频调仓口径。"),
        KLINE_ADJUSTMENT_NOTE[market] + "。",
        ("CN 行业映射为 instrument_detail 静态快照（rs_hyname），期间行业调整不反映；"
         "ST 名单同为快照，静态口径含前视偏差。") if market == "CN" else "行业映射为外部分类快照。",
    ] + report["caveats"]
    return report


def _universe_meta_cn(root: Path, market: str, universe: list[str]) -> tuple[dict, dict]:
    """CN 的 ST / 上市天数据来源探查；HK/US 无对应口径则标记不可用。"""
    if market != "CN":
        return ({"available": False, "note": "当地无 ST 制度口径" if market == "HK" else "本地数据无 ST 口径"},
                {"available": False, "note": "本轮窗口因子集无 list_date 列"})
    import pandas as pd  # noqa: PLC0415
    f = root / INDUSTRY_PATHS["CN"]
    st = {"available": False, "note": "instrument_detail 不可用"}
    if f.exists():
        d = pd.read_parquet(f, columns=["Symbol", "IsSTGP", "HqDate"])
        uset = set(universe)
        n_st = int(d[d["Symbol"].astype(str).isin(uset)]["IsSTGP"].fillna(0).astype(float).sum())
        hq = str(d["HqDate"].iloc[0])[:8] if "HqDate" in d.columns and len(d) else "?"
        st = {"available": True, "source": "instrument_detail.IsSTGP",
              "n_st_in_universe": n_st, "snapshot": hq,
              "note": f"静态快照（HqDate={hq}），用它过滤历史样本含前视偏差；仅作现状说明"}
    return st, {"available": False, "note": "窗口数据集列集无 list_date（2026-09-21 起的新版才有）；"
                                           "如需上市天数过滤请用新版数据集或外部名单"}


# ------------------------------------------------------------------ Demo（确定性合成面板 + 断言）

_IND = ["银行", "软件", "医药", "有色", "电力"]
_IND_SHIFT = {ind: (i - 2) * 0.004 for i, ind in enumerate(_IND)}


def _demo_dates(n: int, start: _date = _date(2024, 1, 2)) -> list[str]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _demo_rows(kind: str, n_days: int = 260) -> list[dict]:
    """合成面板：40 只 × 5 行业。kind ∈ {leak, noise, industry, alpha}。"""
    offset = {"leak": 1, "noise": 2, "industry": 3, "alpha": 4}[kind]  # 稳定种子（不依赖 hash 随机盐）
    rng = random.Random(DEMO_SEED + offset)
    symbols = [(f"S{j:03d}", _IND[j % len(_IND)], 9.0 + 0.35 * j) for j in range(40)]
    dates = _demo_dates(n_days)
    prev_f = {s: 0.0 for s, _, _ in symbols}
    rows = []
    for d in dates:
        for sym, ind, lsz in symbols:
            size = lsz + rng.gauss(0, 0.05)
            if kind == "leak":                 # 因子 = 未来收益本身（泄漏面板）
                r = rng.gauss(0, 0.01)
                f = r + rng.gauss(0, 1e-6)
                r_lag = rng.gauss(0, 0.01)
            elif kind == "noise":              # 纯随机（样本幻觉面板）
                f = rng.gauss(0, 1)
                r, r_lag = rng.gauss(0, 0.01), rng.gauss(0, 0.01)
            elif kind == "industry":           # 行业收益代理 + 行业噪声（行业暴露面板）
                f = _IND_SHIFT[ind] / 0.004 + rng.gauss(0, 0.15)
                r = _IND_SHIFT[ind] + rng.gauss(0, 0.006)
                r_lag = _IND_SHIFT[ind] + rng.gauss(0, 0.006)
            else:                              # 持续性特质信号（真 alpha 面板）
                f = 0.95 * prev_f[sym] + rng.gauss(0, 0.31)
                prev_f[sym] = f
                r = 0.0025 * f + rng.gauss(0, 0.02)
                r_lag = 0.0025 * f + rng.gauss(0, 0.02)
            rows.append({"date": d, "symbol": sym, "f": f, "r": r, "r_lag": r_lag,
                         "industry": ind, "size": size})
    return rows


def run_demo(args, cfg) -> int:
    cases = [("mystery_signal", "leak", "leakage", "把未来收益当因子传入"),
             ("random_noise", "noise", "sample_illusion", "纯随机噪声"),
             ("sector_beta_proxy", "industry", "industry_exposure", "只靠行业差异有效"),
             ("idio_alpha_demo", "alpha", "alpha", "行业内生、可扣费的真信号")]
    print(f"因子质量裁决 — demo 自检（合成面板，seed={DEMO_SEED}）")
    print("=" * 62)
    reports, checks = [], []
    demo_universe = {"method": "synthetic_demo", "status": "not_applicable",
                     "findings": ["合成面板：票池构造/停牌/ST 检查不适用（数据由脚本生成）"]}
    for name, kind, expect, desc in cases:
        rep = audit_panel(_demo_rows(kind), name, cfg, universe=dict(demo_universe))
        reports.append(rep)
        got = rep["verdict"]["label"]
        checks.append((name, desc, expect, got, got == expect))
        print(render_card(rep))
        print()
    cost = reports[3]["checks"]["turnover_cost"]
    net = cost.get("net_side_bp")
    checks.append(("idio_alpha_demo#cost", "alpha 面板扣费后仍为正", "净>0",
                   f"{net:.2f} bp/日" if net is not None else "—", net is not None and net > 0))
    ret = reports[2]["checks"]["neutralization"].get("retention")
    checks.append(("sector_beta_proxy#retention", "行业面板中性化后 IC 塌缩",
                   f"<{cfg['neutral_retention']}", _fmt(ret, 2),
                   ret is not None and ret < cfg["neutral_retention"]))
    print("断言结果：")
    for name, desc, expect, got, ok in checks:
        print(f"  [{'通过' if ok else '失败'}] {name}（{desc}）→ 期望 {expect}，实得 {got}")
    all_ok = all(c[-1] for c in checks)
    if args.out:
        for rep in reports:
            rep["mode"] = "demo"
        _write_json(Path(args.out), {"mode": "demo", "reports": reports})
        print(f"\n已写入 JSON 报告：{args.out}")
    print(f"\ndemo 自检：{'全部通过' if all_ok else '存在失败项'}")
    return 0 if all_ok else 1


# ------------------------------------------------------------------ 渲染与输出

def _fmt(v, dp=4, signed=False) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    return f"{v:+.{dp}f}" if signed else f"{v:.{dp}f}"


def render_card(rep: dict) -> str:
    v = rep["verdict"]
    c = rep["checks"]
    ic, pit = c["ic"], c["pit"]
    uni, cost, neu = c["universe"], c["turnover_cost"], c["neutralization"]
    L = []
    L.append("=" * 64)
    L.append(f"因子质量裁决卡 — {rep['name']}"
             + (f"（{rep.get('dataset', rep.get('mode', ''))}）" if rep.get("dataset") or rep.get("mode") else ""))
    L.append("=" * 64)
    L.append(f"窗口：{rep['date_start']} → {rep['date_end']} ｜ 交易日 {rep['n_days']} ｜ "
             f"IC 观测 {ic['n']}" + ("  ⚠ 样本偏少" if ic.get("low_sample_warning") else ""))
    L.append("-" * 64)
    L.append(f"① 票池构造检查   [{uni.get('status', '')}]")
    if uni.get("cross_section"):
        cs = uni["cross_section"]
        L.append(f"   • 截面宽度：中位 {_fmt(cs.get('median_names'), 0)} 只/日（最小 {cs.get('min_names')}）")
    if uni.get("coverage_pct") is not None:
        L.append(f"   • 覆盖：近满 {uni['coverage_pct']:.1f}%；停牌/零成交行 "
                 f"{uni['suspended_like']['pct']:.2f}%")
    if uni.get("st_filter"):
        s = uni["st_filter"]
        L.append("   • ST 过滤：" + (f"{s.get('source')} 命中 {s.get('n_st_in_universe')} 只，"
                 f"{s.get('note', '')}" if s.get("available") else s.get("note", "")))
    if uni.get("listing_age"):
        s = uni["listing_age"]
        L.append("   • 上市天数：" + (f"{s.get('source')}" if s.get("available") else s.get("note", "")))
    for f in (uni.get("findings") or [])[:3]:
        L.append(f"   · {f}")
    L.append("② 时点对齐检查")
    if pit.get("ic_next_day_start") is not None:
        if pit.get("retention") is not None:
            L.append(f"   • 同日口径 IC={_fmt(pit['ic_same_day'], 4, signed=True)}；"
                     f"次日执行口径 IC={_fmt(pit['ic_next_day_start'], 4, signed=True)}；"
                     f"效力保留 {_fmt(pit['retention'] * 100, 1)}%")
        else:
            L.append(f"   • 同日口径 IC={_fmt(pit['ic_same_day'], 4, signed=True)}；"
                     f"次日执行口径 IC={_fmt(pit['ic_next_day_start'], 4, signed=True)}"
                     "（同日 IC 近零，保留率略）")
    else:
        L.append(f"   · {pit.get('note', '仅同日口径')}")
    L.append(f"③ IC/IR（简版，H={rep.get('horizon', 1)}）")
    L.append(f"   • 均值 IC {_fmt(ic['mean_ic'], 4, signed=True)}（{v['direction']}）｜ ICIR年化 "
             f"{_fmt(ic['icir_ann'], 2, signed=True)} ｜ NW-t {_fmt(ic['nw_t'], 2, signed=True)} ｜ "
             f"方向命中率 {_fmt((ic['hit_rate'] or 0) * 100, 1)}%")
    if ic.get("segments"):
        seg = "；".join(f"{s['start'][:7]}~{s['end'][:7]} {_fmt(s['mean_ic'], 4, signed=True)}"
                        for s in ic["segments"])
        L.append(f"   • 分段：{seg}")
    L.append(f"④ 换手与成本（日频调仓，单边 {rep['verdict']['thresholds']['cost_bp']:.1f} bp）")
    if cost.get("n_days"):
        L.append(f"   • 可执行端：毛 {cost['gross_side_bp']:.1f} bp/日 → 换手 "
                 f"{cost['turnover_side_pct']:.1f}%/日 → 净 {cost['net_side_bp']:.1f} bp/日"
                 f"（盈亏平衡单边 {_fmt(cost.get('breakeven_side_one_side_bp'), 1)} bp）")
        L.append(f"   • 理论多空：毛 {cost['gross_ls_bp']:.1f} → 净 {cost['net_ls_bp']:.1f} bp/日"
                 f"（空头端现实性见保留意见）")
    else:
        L.append("   · 截面不足，未能构造组合口径")
    L.append("⑤ 中性化残差（行业+规模）")
    if neu.get("enabled"):
        L.append(f"   • 行业方差解释（中位）{_fmt((neu.get('industry_r2_median') or 0) * 100, 1)}% ｜ "
                 f"与规模相关（中位）{_fmt(neu.get('size_corr_median'), 2)}")
        L.append(f"   • IC {_fmt(ic['mean_ic'], 4, signed=True)} → 残差 IC "
                 f"{_fmt((neu.get('summary') or {}).get('mean_ic'), 4, signed=True)} ｜ 保留率 "
                 f"{_fmt(neu.get('retention'), 2)}")
    else:
        L.append("   · 行业/规模数据缺失，未执行")
    L.append("-" * 64)
    L.append(f"裁决：{v['label_zh']}（方向：{v['direction']}）｜ 置信：{v['confidence']}")
    L.append(f"判据：{v['reason']}")
    for e in v["evidence"]:
        L.append(f"   • {e}")
    for x in v.get("criteria", []):
        L.append(f"   • {x}")
    L.append("保留意见 / 反证：")
    for cv in rep.get("caveats", []):
        L.append(f"   - {cv}")
    L.append("（研究证据描述，不构成投资建议。）")
    return "\n".join(L)


def json_safe(obj):
    if isinstance(obj, dict):
        return {k: json_safe(x) for k, x in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [json_safe(x) for x in obj]
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    return obj


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(json_safe(payload), ensure_ascii=False, indent=2), encoding="utf-8")


# ------------------------------------------------------------------ 入口

def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="单因子质量裁决（CSV 面板 / QuantDB 本地直读）",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--demo", action="store_true", help="内置合成面板自检（含断言）")
    mode.add_argument("--input", help="长面板 CSV：date,symbol,factor,fwd_ret[,…]")
    mode.add_argument("--quantdb", action="store_true", help="QuantDB 直读模式（容器内运行）")
    p.add_argument("--name", default=None, help="因子名称（CSV/demo 模式）")
    p.add_argument("--factor", default=None, help="因子列名（quantdb 模式）")
    p.add_argument("--factor-col", default=None, help="CSV 因子列名（默认自动探测 factor/signal）")
    p.add_argument("--ret-col", default=None, help="CSV 收益列名（默认自动探测 fwd_ret 等；可用 close 现算）")
    p.add_argument("--market", default="CN", choices=["CN", "HK", "US"])
    p.add_argument("--dataset", default=None, help="CN=features_daily/l1_factors/l2_factors；HK/US=l1_factors")
    p.add_argument("--start", default=None, help="起始日 YYYY-MM-DD（默认 end 前推 2 年）")
    p.add_argument("--end", default=None, help="结束日 YYYY-MM-DD（默认最新行情日）")
    p.add_argument("--top-n", type=int, default=300, help="窗口末日成交额前 N（默认 300；0=全部）")
    p.add_argument("--symbols", default=None, help="显式票池（后缀式逗号分隔）")
    p.add_argument("--horizon", type=int, default=1, help="IC 主口径前视周期（默认 1 日）")
    p.add_argument("--quantile", type=float, default=DEFAULT_QUANTILE, help="两端组合分位（默认 0.10）")
    p.add_argument("--cost-bp", type=float, default=COST_ONE_SIDE_BP, help="单边成本 bp（默认 3=万3）")
    p.add_argument("--leak-ic", type=float, default=LEAK_IC, help=f"泄漏判定 |IC| 阈值（默认 {LEAK_IC}）")
    p.add_argument("--sig-t", type=float, default=SIG_T, help=f"NW-t 显著阈值（默认 {SIG_T}）")
    p.add_argument("--neutral-retention", type=float, default=NEUTRAL_RETENTION,
                   help=f"中性化保留率阈值（默认 {NEUTRAL_RETENTION}）")
    p.add_argument("--nw-lag", type=int, default=5, help="Newey-West lag（默认 5）")
    p.add_argument("--out", default=None, help="JSON 报告输出路径")
    args = p.parse_args(argv)

    cfg = {
        "leak_ic": args.leak_ic, "sig_t": args.sig_t, "sig_t_soft": SIG_T_SOFT,
        "sig_ic_soft": SIG_IC_SOFT, "weak_ic": WEAK_IC,
        "neutral_retention": args.neutral_retention,
        "pit_retention_ok": PIT_RETENTION_OK, "pit_retention_warn": PIT_RETENTION_WARN,
        "quantile": args.quantile, "cost_bp": args.cost_bp, "nw_lag": args.nw_lag,
    }
    if args.demo:
        return run_demo(args, cfg)
    try:
        if args.input:
            rep = run_csv(args, cfg)
        else:
            if not args.factor:
                raise SystemExit("--quantdb 模式需 --factor 指定因子列")
            rep = run_quantdb(args, cfg)
    except ValueError as e:  # 面板不满足裁决条件（如 IC 观测不足）
        print(f"拒绝裁决：{e}", file=sys.stderr)
        return 2
    print(render_card(rep))
    if args.out:
        _write_json(Path(args.out), rep)
        print(f"\n已写入 JSON 报告：{args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
