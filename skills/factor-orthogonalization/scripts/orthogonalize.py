#!/usr/bin/env python3
"""因子正交化工具箱 — 对称正交(Löwdin) / Gram-Schmidt / 基准暴露残差化 + QuantDB(CN/HK/US) 装配。

来源：quantskills/skill-factor-orthogonalize（GPL-3.0-only）。
方法论保留（逐日截面回归残差化、控制变量 winsorize(5×MAD)+z-score、残差再标准化、
暴露清零/IC 保真诊断、反模式清单），数据层由 PandaData/API 改为本地 QuantDB 直读：

  --demo 模式：纯标准库，任意机器可跑。内置确定性合成面板（3 行业 × 60 标的 × 45 日），
      外加手算金样断言（2×2 Löwdin 闭式、Gram-Schmidt 闭式、4 股 2 行业手算残差）。
  --quantdb 模式：pandas/pyarrow 延迟导入，**在 quantmind 容器内运行**。
      因子列 ← <market_root>/6_ml_datasets/<dataset>（CN 默认 features_daily，另有 l1_factors/l2_factors；
              HK/US 为 l1_factors，无日期列→日期取分区名 dt=；HK 需按 (symbol,date,published_at) 去重）
      规模代理 ← CN features_daily 的 float_mv 实测全填充（5234/5234，用真市值，ln 变换）；
              HK l1_factors 市值列实测全 0 未填充（turnover_rate/float_mv/total_mv nunique=1 且全零），
              自动退回对数成交额代理 ln(20 日均成交额)（源技能 log_dollar_vol 同款口径）；
              US l1_factors 的 ln_mv_total 实测全填充（484/484）→ 直接用。
      行业哑变量 ← CN: quantdb/2_base_sector/instrument_detail（rs_hyname，通达信 128 类，静态快照）；
              HK: quanthk/2_base_sector/akshare_profile（所属行业 31 类；l1_factors 的 industry 列
              实测为全空字符串不可用）；US: quantus/2_base_sector/sector（sector 11 类 / industry 更细）。
      收益 ← <market_root>/1_kline_data/daily_forward，ret_h(t)=close(t+h)/close(t)-1，
              读取范围自动向窗口末端之后多取 ~40 个交易日。

三种方法的定位（详见 references/methods.md）：
  sym  对称正交：W = Z · S^(-1/2)（S=因子相关矩阵，Jacobi 特征分解），对输入顺序不敏感，
       最小二乘意义下最接近原因子；输出是整组因子的线性混合（解释性改变）。
  gs   Gram-Schmidt（modified，双向正交化）：顺序敏感——第一个因子原样保留；
       适合「已有主因子 + 新因子做增量」；换次序结果不同（--gs-order 可做敏感性检查）。
  resid 残差化：逐日截面回归 y ~ 行业哑变量 + 规模 + 风格（FWL 等价实现），残差再标准化；
       每个因子独立处理、保留自身语义；IC 通常下降是预期行为，不是 bug。
       残差最终标准化默认纯仿射 z-score（--resid-final-scale affine，严格保持零暴露到 1e-15）；
       winsorize_zscore 为源技能口径（截尾非线性，实测个别日会重新引入 ≤1e-1 规模暴露）。

独立校验（脚本内置、随报告输出）：
  1) 对称正交/Gram-Schmidt 后相关矩阵 max|非对角|（双精度预期 ~1e-15 量级）；
  2) 残差化后与基准的暴露：残差-规模 |corr| 与残差-行业 R²（预期 ≤1e-12）；
  3) quantdb 模式在首个处理日把 FWL 残差与 numpy.linalg.lstsq（显式哑变量矩阵、SVD）对拍；
  4) --demo 模式运行手算金样断言，逐条 PASS/FAIL。

标签列护栏：features_daily 的 return_{n}d / future_return_{n}d / label_return 与
l1_factors 的 return_{n}d 都是未来收益标签，脚本拒绝把标签列当因子或控制变量。

用法：
  # 纯标准库演示（宿主机/dsh 可直接跑）
  python3 orthogonalize.py --demo --out /tmp/demo_orth.json

  # QuantDB 实跑（quantmind 容器内）
  python3 orthogonalize.py --quantdb --market CN \
      --factors ma_gap_20,rsi_14,vol_std_20,dividend_rate \
      --start 2024-01-01 --end 2025-12-31 --top-n 300 \
      --out /data/reports/factor-orthogonalization/cn_2024_2025.json

  # 探查列与填充度
  python3 orthogonalize.py --quantdb --market CN --list-columns

输出：stdout 中文表格 + JSON 报告（--out 指定文件路径）。
正交化是研究处理，不改变「因子未来是否有效」的不确定性；本工具不构成投资建议。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from collections import Counter
from datetime import date as _date, timedelta
from pathlib import Path
from statistics import median

# ---------------------------------------------------------------- 常量

MIN_NAMES_PER_CROSS = 30      # 逐日截面回归/正交的最小标的数（源技能 MIN_SAMPLES=30）
MIN_IC_NAMES = 10             # 计算日度 rank IC 的最小截面（源技能 daily_rank_ic 同口径）
WINSORIZE_N_MAD = 5.0         # 控制变量逐日 winsorize 倍数（源技能 5×MAD）
SINGULAR_EIG_TOL = 1e-10      # 相关矩阵最小特征值低于此判奇异（完全共线/常量列）
ILL_COND_EIG_TOL = 1e-2       # 低于此记「病态」警告（仍处理）
DOF_MIN_RESID = 5             # 残差化自由度护栏：N - 组数 - 控制列数 ≥ 此值
MIN_RETENTION_DENOM = 5e-3    # |原始 mean IC| 低于此值时不给「IC 保留率」比值（分母不稳）
MAX_ORTH_FACTORS = 24         # sym/gs 一次处理的因子数上限（Jacobi 成本与可读性护栏）
MAX_GS_FACTORS = 64

LABEL_COL_PATTERNS = [
    re.compile(r"^label_return$", re.I),
    re.compile(r"^return_\d+d$", re.I),
    re.compile(r"^future_return_\d+d$", re.I),
]

DATA_ROOT_CANDIDATES = ["/data", "/quantmind/data", "/home/zbox/projects/quantmind/data"]

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

# 规模代理候选列（auto 模式；按顺序做填充度体检，不合格自动退回对数成交额）
AUTO_SIZE_COLS = {
    ("CN", "features_daily"): ["float_mv", "total_mv"],
    ("CN", "l1_factors"): ["fun_float_mv", "fun_total_mv"],
    ("CN", "l2_factors"): ["fun_float_mv", "fun_total_mv"],
    ("HK", "l1_factors"): ["ln_mv_total", "total_mv", "float_mv"],
    ("US", "l1_factors"): ["ln_mv_total", "total_mv", "float_mv"],
}
SIZE_FILL_MIN_NONNULL = 0.80  # 候选列非空占比门槛
SIZE_FILL_MAX_ZERO = 0.20     # 零值占比门槛（未填充列常以 0 填充）

INDUSTRY_SOURCES = {
    "CN": {
        "path": "quantdb/2_base_sector/instrument_detail/instrument_detail.parquet",
        "key": "Symbol", "column": "rs_hyname",
        "note": "通达信行业 128 类；instrument_detail 为静态快照（HqDate=20260720），"
                "行业重分类不反映在历史（已知局限）",
    },
    "HK": {
        "path": "quanthk/2_base_sector/akshare_profile",
        "key": "filename", "column": "所属行业",
        "note": "akshare 公司资料「所属行业」31 类；l1_factors 的 industry 列实测全为空字符串不可用",
    },
    "US": {
        "path": "quantus/2_base_sector/sector",
        "key": "filename", "columns": {"sector": "sector", "industry": "industry"},
        "note": "yahoo 行业：sector 11 类（默认）/ industry 更细",
    },
}

AUX_COLUMNS = {
    "symbol", "time", "date", "open", "high", "low", "close", "volume", "amount",
    "adj_factor", "release_id", "published_at", "Symbol_val", "close_val", "industry",
}

KLINE_ADJUSTMENT_NOTE = {
    "CN": "daily_forward=前复权（收益已含分红送转调整）",
    "HK": "daily_forward=不复权原始价（收益未含分红调整）",
    "US": "daily_forward=不复权原始价（收益未含分红调整）",
}


def is_label_column(name: str) -> bool:
    return any(p.match(name.strip()) for p in LABEL_COL_PATTERNS)


# ================================================================ 纯标准库数值核心

def _finite(v) -> bool:
    return isinstance(v, float) and math.isfinite(v)


def mean(vals: list[float]) -> float:
    return sum(vals) / len(vals)


def sample_std(vals: list[float]) -> float:
    n = len(vals)
    if n < 2:
        return 0.0
    m = mean(vals)
    return math.sqrt(sum((v - m) ** 2 for v in vals) / (n - 1))


def pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 3 or n != len(ys):
        return None
    mx, my = mean(xs), mean(ys)
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


def rankdata_average(values: list[float]) -> list[float]:
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
    if len(xs) < 3 or len(xs) != len(ys):
        return None
    return pearson(rankdata_average(xs), rankdata_average(ys))


def winsorize_zscore(vals: list[float], n_mad: float = WINSORIZE_N_MAD) -> list[float]:
    """逐日截面稳健标准化（源技能口径）：MAD 截尾 → 减均值除样本标准差；NaN 原样保留。"""
    clean = [v for v in vals if _finite(v)]
    if len(clean) < 2:
        return [float("nan")] * len(vals)
    med = median(clean)
    mad = median([abs(v - med) for v in clean])
    out = []
    if mad > 0 and math.isfinite(mad):
        lo = med - n_mad * 1.4826 * mad
        hi = med + n_mad * 1.4826 * mad
        out = [min(max(v, lo), hi) if _finite(v) else float("nan") for v in vals]
    else:
        out = [v if _finite(v) else float("nan") for v in vals]
    cc = [v for v in out if _finite(v)]
    m, s = mean(cc), sample_std(cc)
    if s <= 0:
        return [float("nan")] * len(vals)
    return [(v - m) / s if _finite(v) else float("nan") for v in out]


def corr_matrix(cols: list[list[float]]) -> list[list[float]]:
    """列间 Pearson 相关矩阵（调用方须保证各列完整无 NaN）。退化相关置 1/0 由调用方检查。"""
    k = len(cols)
    out = [[0.0] * k for _ in range(k)]
    for i in range(k):
        out[i][i] = 1.0
        for j in range(i + 1, k):
            r = pearson(cols[i], cols[j])
            if r is None:
                r = float("nan")
            out[i][j] = out[j][i] = r
    return out


def jacobi_eigh(mat: list[list[float]], tol: float = 1e-13,
                max_sweeps: int = 100) -> tuple[list[float], list[list[float]]]:
    """对称矩阵 Jacobi 特征分解。返回 (特征值降序, 特征向量矩阵，列为特征向量)。"""
    n = len(mat)
    a = [row[:] for row in mat]
    v = [[1.0 if i == j else 0.0 for j in range(n)] for i in range(n)]
    for _ in range(max_sweeps):
        off = math.sqrt(sum(a[i][j] ** 2 for i in range(n) for j in range(n) if i != j))
        if off <= tol:
            break
        for p in range(n - 1):
            for q in range(p + 1, n):
                if abs(a[p][q]) <= tol * 0.01:
                    continue
                theta = (a[q][q] - a[p][p]) / (2.0 * a[p][q])
                t = (1.0 if theta >= 0 else -1.0) / (abs(theta) + math.sqrt(theta * theta + 1.0))
                c = 1.0 / math.sqrt(t * t + 1.0)
                s = t * c
                for i in range(n):
                    aip, aiq = a[i][p], a[i][q]
                    a[i][p] = c * aip - s * aiq
                    a[i][q] = s * aip + c * aiq
                for i in range(n):
                    api, aqi = a[p][i], a[q][i]
                    a[p][i] = c * api - s * aqi
                    a[q][i] = s * api + c * aqi
                for i in range(n):
                    vip, viq = v[i][p], v[i][q]
                    v[i][p] = c * vip - s * viq
                    v[i][q] = s * vip + c * viq
    eig = [a[i][i] for i in range(n)]
    order = sorted(range(n), key=lambda i: eig[i], reverse=True)
    eig_sorted = [eig[i] for i in order]
    vecs = [[v[r][i] for i in order] for r in range(n)]
    return eig_sorted, vecs


def _mat_mul_cols(mat: list[list[float]], cols: list[list[float]]) -> list[list[float]]:
    """W_j = Σ_i cols_i · mat[i][j]。"""
    n = len(cols[0])
    k = len(cols)
    out = []
    for j in range(k):
        col = [0.0] * n
        for i in range(k):
            c = mat[i][j]
            if c == 0.0:
                continue
            ci = cols[i]
            for r in range(n):
                col[r] += c * ci[r]
        out.append(col)
    return out


def symmetric_orthogonalize(cols: list[list[float]]) -> tuple[list[list[float]] | None,
                                                              list[list[float]] | None,
                                                              str]:
    """Löwdin 对称正交：W = Z · S^(-1/2)，S 为相关矩阵。返回 (W, T, 说明)。"""
    k = len(cols)
    if k < 2:
        return None, None, "因子少于 2 个，对称正交无意义"
    s = corr_matrix(cols)
    for i in range(k):
        for j in range(k):
            if not math.isfinite(s[i][j]):
                return None, None, f"相关矩阵含退化元素（第 {i + 1}/{j + 1} 列常量或样本不足）"
    eig, vecs = jacobi_eigh(s)
    if eig[-1] <= SINGULAR_EIG_TOL:
        return None, None, f"相关矩阵奇异（最小特征值 {eig[-1]:.2e}，因子间完全共线或常量列）"
    ill = eig[-1] < ILL_COND_EIG_TOL
    inv_sqrt = [[0.0] * k for _ in range(k)]
    for i in range(k):
        wi = 1.0 / math.sqrt(eig[i])
        for r in range(k):
            vr = vecs[r][i] * wi
            for c in range(k):
                inv_sqrt[r][c] += vr * vecs[c][i]
    w = _mat_mul_cols(inv_sqrt, cols)
    note = "病态（近共线，混合系数放大）" if ill else "ok"
    return w, inv_sqrt, note


def gram_schmidt(cols: list[list[float]]) -> list[list[float]] | None:
    """Modified Gram-Schmidt + 二次正交化（数值稳定）；返回与输入同序的正交列。"""
    q: list[list[float]] = []
    for f in cols:
        v = f[:]
        for _pass in range(2):
            for prev in q:
                pv = sum(a * b for a, b in zip(v, prev, strict=False))
                pp = sum(b * b for b in prev)
                if pp <= 0:
                    continue
                coef = pv / pp
                v = [a - coef * b for a, b in zip(v, prev, strict=False)]
        nn = math.sqrt(sum(x * x for x in v))
        if nn <= 1e-12:
            return None
        q.append([x / nn for x in v])
    return q


def solve_normal_equations(gram: list[list[float]], rhs: list[float],
                           tol: float = 1e-10) -> tuple[list[float] | None, int | None]:
    """列主元高斯消元解 Gram·b = rhs；奇异时返回 (None, 奇异主元所在变量下标)。"""
    m = len(rhs)
    a = [gram[i][:] + [rhs[i]] for i in range(m)]
    scale = max((abs(a[i][j]) for i in range(m) for j in range(m)), default=0.0)
    thr = max(tol * max(scale, 1e-300), 1e-300)
    for col in range(m):
        piv = max(range(col, m), key=lambda r: abs(a[r][col]))
        if abs(a[piv][col]) < thr:
            return None, col
        a[col], a[piv] = a[piv], a[col]
        for r in range(col + 1, m):
            f = a[r][col] / a[col][col]
            if f == 0.0:
                continue
            for c in range(col, m + 1):
                a[r][c] -= f * a[col][c]
    x = [0.0] * m
    for r in range(m - 1, -1, -1):
        acc = a[r][m] - sum(a[r][c] * x[c] for c in range(r + 1, m))
        x[r] = acc / a[r][r]
    return x, None


def residualize_fwl(y: list[float], groups: list[str],
                    controls: list[list[float]]) -> tuple[list[float], list[float],
                                                          list[int], int]:
    """Frisch-Waugh-Lovell 残差化：等价于 y ~ 行业全哑变量 + controls 的 OLS 残差。

    组内去均值后对（同样组内去均值的）控制列做 OLS（无截距），残差即全模型残差。
    返回 (residuals, betas, dropped_control_idx, n_groups)。
    """
    n = len(y)
    gsum: dict[str, list[float]] = {}
    for g, v in zip(groups, y, strict=False):
        acc = gsum.setdefault(g, [0.0, 0.0])
        acc[0] += v
        acc[1] += 1.0
    gy = [gsum[g][0] / gsum[g][1] for g in groups]
    yt = [v - gv for v, gv in zip(y, gy, strict=False)]

    kept: list[int] = []
    kept_cols: list[list[float]] = []
    dropped: list[int] = []
    for idx, c in enumerate(controls):
        csum: dict[str, list[float]] = {}
        for g, v in zip(groups, c, strict=False):
            acc = csum.setdefault(g, [0.0, 0.0])
            acc[0] += v
            acc[1] += 1.0
        ct = [v - csum[g][0] / csum[g][1] for g, v in zip(groups, c, strict=False)]
        norm2 = sum(v * v for v in ct)
        if norm2 <= 1e-10 * max(n, 1):     # 组内无变化 → 该控制列不可识别，剔除
            dropped.append(idx)
            continue
        kept.append(idx)
        kept_cols.append(ct)

    betas_kept: list[float] = []
    while kept_cols:
        m = len(kept_cols)
        gram = [[sum(a * b for a, b in zip(kept_cols[i], kept_cols[j], strict=False))
                 for j in range(m)] for i in range(m)]
        rhs = [sum(a * b for a, b in zip(kept_cols[i], yt, strict=False)) for i in range(m)]
        sol, bad = solve_normal_equations(gram, rhs)
        if sol is not None:
            betas_kept = sol
            break
        dropped.append(kept.pop(bad))
        kept_cols.pop(bad)
    resid = yt[:]
    for idx, b in zip(kept, betas_kept, strict=False):
        col = controls[idx]
        gsum_c: dict[str, list[float]] = {}
        for g, v in zip(groups, col, strict=False):
            acc = gsum_c.setdefault(g, [0.0, 0.0])
            acc[0] += v
            acc[1] += 1.0
        for r, (g, v) in enumerate(zip(groups, col, strict=False)):
            resid[r] -= b * (v - gsum_c[g][0] / gsum_c[g][1])
    betas = [betas_kept[kept.index(i)] if i in kept and kept.index(i) < len(betas_kept) else 0.0
             for i in range(len(controls))]
    return resid, betas, sorted(dropped), len(gsum)


def _group_mean_map(vals: list[float], groups: list[str]) -> dict[str, list[float]]:
    gsum: dict[str, list[float]] = {}
    for g, v in zip(groups, vals, strict=False):
        acc = gsum.setdefault(g, [0.0, 0.0])
        acc[0] += v
        acc[1] += 1.0
    return gsum


def industry_r2(vals: list[float], groups: list[str]) -> float:
    """把 vals 对行业全哑变量 + 截距回归的 R²（中心化组间平方和 / 总平方和）。"""
    gsum = _group_mean_map(vals, groups)
    gm = mean(vals)
    total = sum((v - gm) ** 2 for v in vals)
    if total <= 0:
        return 0.0
    between = sum((acc[0] / acc[1] - gm) ** 2 * acc[1] for acc in gsum.values())
    return min(1.0, between / total)


def industry_max_abs_group_mean(z_vals: list[float], groups: list[str]) -> float | None:
    """行业暴露（源技能报告口径）：标准化值（单位=σ）的 max|行业组均值|，无量纲。"""
    gsum: dict[str, list[float]] = {}
    for g, v in zip(groups, z_vals, strict=False):
        if not _finite(v):
            continue
        acc = gsum.setdefault(g, [0.0, 0.0])
        acc[0] += v
        acc[1] += 1.0
    means = [abs(acc[0] / acc[1]) for acc in gsum.values() if acc[1] > 0]
    return max(means) if means else None


def zscore_affine(vals: list[float]) -> list[float]:
    """纯仿射 z-score（无截尾）：严格保序保相关，残差零暴露性质不受扰动。"""
    clean = [v for v in vals if _finite(v)]
    if len(clean) < 2:
        return [float("nan")] * len(vals)
    m, s = mean(clean), sample_std(clean)
    if s <= 0:
        return [float("nan")] * len(vals)
    return [(v - m) / s if _finite(v) else float("nan") for v in vals]


def _col_demean_within(vals: list[float], groups: list[str]) -> list[float]:
    gsum: dict[str, list[float]] = {}
    for g, v in zip(groups, vals, strict=False):
        acc = gsum.setdefault(g, [0.0, 0.0])
        acc[0] += v
        acc[1] += 1.0
    return [v - gsum[g][0] / gsum[g][1] for g, v in zip(groups, vals, strict=False)]


# ================================================================ 金样（手算可验证）

def golden_checks() -> list[dict]:
    """手算金样断言：算法闭式 / 手算残差案例。任何一条不过说明实现偏离数学定义。"""
    checks: list[dict] = []

    def add(name, ok, expected, actual):
        checks.append({"name": name, "passed": bool(ok),
                       "expected": expected, "actual": actual})

    # 1) 2×2 Löwdin 闭式：ρ=0.6 → S^(-1/2) = [[a,b],[b,a]]，
    #    a=(1/2)(1/√1.6+1/√0.4)=1.1858535309847363，b=(1/2)(1/√1.6−1/√0.4)=−0.39528470752104746
    rho = 0.6
    s = [[1.0, rho], [rho, 1.0]]
    eig, vecs = jacobi_eigh(s)
    k = 2
    inv_sqrt = [[0.0] * k for _ in range(k)]
    for i in range(k):
        wi = 1.0 / math.sqrt(eig[i])
        for r in range(k):
            for c in range(k):
                inv_sqrt[r][c] += vecs[r][i] * wi * vecs[c][i]
    a_exp = 0.5 * (1.0 / math.sqrt(1.6) + 1.0 / math.sqrt(0.4))
    b_exp = 0.5 * (1.0 / math.sqrt(1.6) - 1.0 / math.sqrt(0.4))
    add("Löwdin 2×2 闭式 a", abs(inv_sqrt[0][0] - a_exp) < 1e-9, a_exp, inv_sqrt[0][0])
    add("Löwdin 2×2 闭式 b", abs(inv_sqrt[0][1] - b_exp) < 1e-9, b_exp, inv_sqrt[0][1])

    # 2) 构造 ρ≈0.6 的两列验证 W=Z·S^(-1/2) 后相关≈0 且列1= a·z1+b·z2
    z1 = [1.0, 1.0, -1.0, -1.0, 1.3, -1.3, 0.4, -0.4]
    z1 = winsorize_zscore(z1)
    base = [0.8, -0.2, -0.9, 0.3, 1.1, -1.2, 0.5, -0.4]
    z2 = winsorize_zscore(base)
    w, _, _ = symmetric_orthogonalize([z1, z2])
    r12 = pearson(w[0], w[1])
    add("Löwdin 输出列相关≈0", r12 is not None and abs(r12) < 1e-12, 0.0, r12)

    # 3) Gram-Schmidt 闭式：第二个因子剔除第一个后的分量 ∝ (z2 − ρ·z1)（单位化后可逐位比对）
    rho_emp = pearson(z1, z2)
    q = gram_schmidt([z1, z2])
    plain = [b - rho_emp * a for a, b in zip(z1, z2, strict=False)]
    nn = math.sqrt(sum(v * v for v in plain))
    plain = [v / nn for v in plain]
    dev = max(abs(x - y) for x, y in zip(q[1], plain, strict=False))
    add("Gram-Schmidt 闭式偏离<1e-12", dev < 1e-12, 0.0, dev)
    dot_q = sum(a * b for a, b in zip(q[0], q[1], strict=False))
    add("Gram-Schmidt 输出列正交", abs(dot_q) < 1e-12, 0.0, dot_q)

    # 4) 手算残差案例：4 股 2 行业，y=(1,3,2,5)，size=(0,1,2,4)，组 (A,A,B,B)
    #    β = 4/2.5 = 1.6，残差 = (−0.2, 0.2, 0.1, −0.1)
    y = [1.0, 3.0, 2.0, 5.0]
    size = [0.0, 1.0, 2.0, 4.0]
    groups = ["A", "A", "B", "B"]
    resid, betas, dropped, _ng = residualize_fwl(y, groups, [size])
    exp_resid = [-0.2, 0.2, 0.1, -0.1]
    dev_r = max(abs(a - b) for a, b in zip(resid, exp_resid, strict=False))
    add("FWL 手算残差 β=1.6", abs(betas[0] - 1.6) < 1e-12, 1.6, betas[0])
    add("FWL 手算残差向量偏离<1e-12", dev_r < 1e-12, 0.0, dev_r)
    add("FWL 无控制列被误剔", not dropped and not _ng * 0, [], dropped)
    # 残差对基准正交（手算案例：Σres·size_centered = 0）
    size_c = _col_demean_within(size, groups)
    dot = sum(a * b for a, b in zip(resid, size_c, strict=False))
    add("FWL 残差·基准严格正交", abs(dot) < 1e-12, 0.0, dot)

    # 5) 行业 R²：常量组内 → 全解释；纯噪声 → 非零但 <1
    r2 = industry_r2(resid, groups)
    add("FWL 残差行业 R²≈0", r2 < 1e-12, 0.0, r2)
    return checks


# ================================================================ demo 合成面板（纯标准库）

class _LCG:
    """确定性 LCG（数值配方常数），保证跨机器/跨版本可复现。"""

    def __init__(self, seed: int):
        self.state = seed & 0xFFFFFFFF

    def u01(self) -> float:
        self.state = (1664525 * self.state + 1013904223) & 0xFFFFFFFF
        return self.state / 4294967296.0

    def gauss(self) -> float:
        u1 = max(self.u01(), 1e-12)
        u2 = self.u01()
        return math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2)


def build_demo_days(n_stocks: int = 60, n_days: int = 45) -> list[dict]:
    """确定性合成面板：3 行业 × n_stocks × n_days，4 个因子 + 2 个控制 + 前视收益。

    因子构造（让暴露/相关诊断有非平凡数字）：
      f1 与行业、规模都相关；f2 = 0.7·f1 + 噪声；f3 与规模负相关；f4 纯噪声。
      真实 alpha 只来自 f1 的“正交后残差成分”与 f4 的成分 → 残差化后 IC 基本保留。
    """
    rng = _LCG(20261008)
    industries = ["IndA", "IndB", "IndC"]
    ind_effect = {"IndA": 0.6, "IndB": 0.0, "IndC": -0.6}
    symbols, sizes, inds = [], [], []
    for i in range(n_stocks):
        ind = industries[i % 3]
        symbols.append(f"DEMO{i:04d}.XX")
        inds.append(ind)
        sizes.append(math.exp(rng.gauss()))          # 规模对数正态
    size_z = winsorize_zscore(sizes)
    days: list[dict] = []
    for t in range(n_days):
        rows = []
        for i in range(n_stocks):
            drift = 0.05 * math.sin(0.3 * t + i)
            ind_v = ind_effect[inds[i]]
            e1 = rng.gauss() * 0.8
            clean1 = 0.6 * ind_v + 0.5 * size_z[i] + e1     # 含行业+规模暴露
            e2 = rng.gauss() * 0.8
            e3 = rng.gauss() * 0.9
            e4 = rng.gauss()
            f1 = clean1 + 0.2 * drift
            f2 = 0.7 * f1 + e2
            f3 = -0.5 * size_z[i] + 0.3 * ind_v + e3
            f4 = 0.9 * e4
            alpha = 0.030 * e1 + 0.020 * e4           # 真 alpha 只含 f1 的残差成分与 f4
            ret1 = alpha + rng.gauss() * 0.02
            ret5 = 2.4 * alpha + rng.gauss() * 0.045
            rows.append({
                "symbol": symbols[i], "ind": inds[i],
                "size": sizes[i], "styles": [],
                "f": [f1, f2, f3, f4],
                "ret": {"H1": ret1, "H5": ret5 if t < n_days - 5 else None},
            })
        days.append({"date": f"DEMO-{t:03d}", "rows": rows})
    return days


# ================================================================ 方法执行（两种模式共用）

def _finite_rows(rows: list[dict], idxs: list[int] | None = None) -> list[dict]:
    out = []
    for r in rows:
        f = r["f"]
        use = idxs if idxs is not None else range(len(f))
        if all(_finite(f[j]) for j in use):
            out.append(r)
    return out


def process_days(days: list[dict], factors: list[str], methods: list[str],
                 gs_order: str = "asis", self_check: bool = False,
                 self_check_hook=None, resid_final_scale: str = "affine") -> dict:
    """对逐日截面执行所选方法，聚合诊断。返回 aggregates dict。

    resid_final_scale：残差最终标准化方式。
      affine = 纯仿射 z-score（默认）——严格保持「残差与基准零相关」到浮点精度；
      winsorize_zscore = 源技能口径（截尾后再标准化），截尾是非线性操作，
      实测会在个别交易日把 ≤7e-2 的规模暴露重新引入最终产出。
    self_check_hook(day, factor_idx) 在 quantdb 模式下用于 numpy 对拍（见 run_quantdb）。
    """
    k = len(factors)
    agg: dict = {
        "sym": {"days_used": 0, "skipped": Counter(), "names_per_day": [],
                "offdiag_max": [], "offdiag_mean": [], "ill_conditioned_days": 0,
                "corr_before_sum": None, "corr_after_sum": None},
        "gs": {"days_used": 0, "skipped": Counter(), "names_per_day": [],
               "offdiag_max": [], "offdiag_mean": [],
               "corr_before_sum": None, "corr_after_sum": None},
        "resid": {"days_used": dict.fromkeys(factors, 0),
                  "skipped": {f: Counter() for f in factors},
                  "names_per_day": {f: [] for f in factors},
                  "expo_before": {f: {"size": [], "styles": {}, "ind_r2": [],
                                      "ind_max_abs_mean": []} for f in factors},
                  "expo_after": {f: {"size": [], "styles": {}, "ind_r2": [],
                                     "ind_max_abs_mean": []} for f in factors},
                  "ctrl_corr_pre_z_max": [], "ctrl_corr_final": [],
                  "dropped_control_days": Counter()},
    }
    n_styles = len(days[0]["rows"][0]["styles"]) if days and days[0]["rows"] else 0
    for meth in ("sym", "gs"):
        if k >= 2:
            agg[meth]["corr_before_sum"] = [[0.0] * k for _ in range(k)]
            agg[meth]["corr_after_sum"] = [[0.0] * k for _ in range(k)]
        for f in factors:
            agg["resid"]["expo_before"][f]["styles"] = {f"style{i}": [] for i in range(n_styles)}
            agg["resid"]["expo_after"][f]["styles"] = {f"style{i}": [] for i in range(n_styles)}

    if k < 2:
        for m in ("sym", "gs"):
            if m in methods:
                agg[m]["skipped"]["因子少于2个"] = 1

    ic_rows: dict[str, dict[str, dict[str, list]]] = {
        m: {f: {h: [] for h in _collect_horizons(days)} for f in factors}
        for m in ("raw", "raw_on_method_rows", "sym", "gs", "resid")
    }

    gate_ok = False
    for day in days:
        rows = day["rows"]
        if not rows:
            continue
        dates_rows = rows

        # ---------- sym / gs（因子间正交，需要全因子完整截面）
        if k >= 2 and ("sym" in methods or "gs" in methods):
            complete = _finite_rows(dates_rows)
            if len(complete) < MIN_NAMES_PER_CROSS:
                for m in ("sym", "gs"):
                    if m in methods:
                        agg[m]["skipped"]["截面不足"] += 1
            else:
                cols = [winsorize_zscore([r["f"][j] for r in complete]) for j in range(k)]
                if any(all(not _finite(v) for v in c) for c in cols):
                    for m in ("sym", "gs"):
                        if m in methods:
                            agg[m]["skipped"]["当日因子常量"] += 1
                else:
                    before = corr_matrix(cols)
                    if "sym" in methods:
                        w, _t, note = symmetric_orthogonalize(cols)
                        if w is None:
                            agg["sym"]["skipped"]["相关矩阵奇异"] += 1
                        else:
                            if note != "ok":
                                agg["sym"]["ill_conditioned_days"] += 1
                            after = corr_matrix(w)
                            agg["sym"]["days_used"] += 1
                            agg["sym"]["names_per_day"].append(len(complete))
                            offs = [abs(after[i][j]) for i in range(k) for j in range(k) if i != j]
                            agg["sym"]["offdiag_max"].append(max(offs))
                            agg["sym"]["offdiag_mean"].append(mean(offs))
                            _accum(agg["sym"]["corr_before_sum"], before)
                            _accum(agg["sym"]["corr_after_sum"], after)
                            for j, f in enumerate(factors):
                                for h in _collect_horizons(days):
                                    rr = [(r["f"][j], r["ret"].get(h)) for r in complete
                                          if r["ret"].get(h) is not None]
                                    _record_ic(ic_rows, "raw_on_method_rows", f, h,
                                               [a for a, _ in rr], [b for _, b in rr],
                                               len(rr))
                                    rr_w = [(w[j][i], complete[i]["ret"].get(h))
                                            for i in range(len(complete))
                                            if complete[i]["ret"].get(h) is not None]
                                    _record_ic(ic_rows, "sym", f, h,
                                               [a for a, _ in rr_w], [b for _, b in rr_w],
                                               len(rr_w))
                    if "gs" in methods:
                        order = list(range(k))
                        if gs_order == "reverse":
                            order = order[::-1]
                        cols_ordered = [cols[j] for j in order]
                        q = gram_schmidt(cols_ordered)
                        if q is None:
                            agg["gs"]["skipped"]["正交列退化"] += 1
                        else:
                            q_back = [None] * k
                            for pos, j in enumerate(order):
                                q_back[j] = winsorize_zscore(q[pos])
                            after = corr_matrix(q_back)
                            agg["gs"]["days_used"] += 1
                            agg["gs"]["names_per_day"].append(len(complete))
                            offs = [abs(after[i][j]) for i in range(k) for j in range(k) if i != j]
                            agg["gs"]["offdiag_max"].append(max(offs))
                            agg["gs"]["offdiag_mean"].append(mean(offs))
                            _accum(agg["gs"]["corr_before_sum"], before)
                            _accum(agg["gs"]["corr_after_sum"], after)
                            for j, f in enumerate(factors):
                                for h in _collect_horizons(days):
                                    rr_w = [(q_back[j][i], complete[i]["ret"].get(h))
                                            for i in range(len(complete))
                                            if complete[i]["ret"].get(h) is not None]
                                    _record_ic(ic_rows, "gs", f, h,
                                               [a for a, _ in rr_w], [b for _, b in rr_w],
                                               len(rr_w))

        # ---------- resid（对基准暴露逐日截面回归，逐因子独立）
        if "resid" in methods:
            for j, fname in enumerate(factors):
                usable = [r for r in dates_rows
                          if _finite(r["f"][j])
                          and _finite(r["size"])
                          and all(_finite(s) for s in r["styles"])]
                if len(usable) < MIN_NAMES_PER_CROSS:
                    agg["resid"]["skipped"][fname]["截面不足"] += 1
                    continue
                y = [r["f"][j] for r in usable]
                if sample_std(y) <= 0:
                    agg["resid"]["skipped"][fname]["当日因子常量"] += 1
                    continue
                groups = [r["ind"] for r in usable]
                controls = [winsorize_zscore([r["size"] for r in usable])]
                for si in range(n_styles):
                    controls.append(winsorize_zscore([r["styles"][si] for r in usable]))
                if any(all(not _finite(v) for v in c) for c in controls):
                    agg["resid"]["skipped"][fname]["控制列退化"] += 1
                    continue
                n_g = len(set(groups))
                if len(usable) - n_g - len(controls) < DOF_MIN_RESID:
                    agg["resid"]["skipped"][fname]["自由度护栏"] += 1
                    continue
                controls_clean = [[v if _finite(v) else 0.0 for v in c] for c in controls]
                resid_raw, _betas, dropped, n_groups = residualize_fwl(y, groups, controls_clean)
                if dropped:
                    agg["resid"]["dropped_control_days"][fname] += 1
                agg["resid"]["days_used"][fname] += 1
                agg["resid"]["names_per_day"][fname].append(len(usable))

                expo_b = agg["resid"]["expo_before"][fname]
                expo_a = agg["resid"]["expo_after"][fname]
                size_c = controls_clean[0]
                # —— before：原始因子的暴露（max|z组均值| 用稳健标准化后的值，单位=σ）
                r_b = pearson(y, size_c)
                if r_b is not None:
                    expo_b["size"].append(r_b)
                for si in range(n_styles):
                    rb = pearson(y, controls_clean[1 + si])
                    if rb is not None:
                        expo_b["styles"][f"style{si}"].append(rb)
                expo_b["ind_r2"].append(industry_r2(y, groups))
                mz_b = industry_max_abs_group_mean(winsorize_zscore(y), groups)
                if mz_b is not None:
                    expo_b["ind_max_abs_mean"].append(mz_b)
                # —— 回归后（标准化前）的数学性质：与基准严格正交
                r_a_pre = pearson(resid_raw, size_c)
                if r_a_pre is not None:
                    agg["resid"]["ctrl_corr_pre_z_max"].append(abs(r_a_pre))

                # —— 最终产出（交付物）：所有 after 暴露指标都在它上面测
                resid_z = (winsorize_zscore(resid_raw) if resid_final_scale == "winsorize_zscore"
                           else zscore_affine(resid_raw))
                resid_z_filled = [v if _finite(v) else 0.0 for v in resid_z]
                r_a_final = pearson(resid_z_filled, size_c)
                if r_a_final is not None:
                    agg["resid"]["ctrl_corr_final"].append(abs(r_a_final))
                    expo_a["size"].append(r_a_final)
                for si in range(n_styles):
                    ra = pearson(resid_z_filled, controls_clean[1 + si])
                    if ra is not None:
                        expo_a["styles"][f"style{si}"].append(ra)
                expo_a["ind_r2"].append(industry_r2(resid_z, groups))
                mz_a = industry_max_abs_group_mean(resid_z, groups)
                if mz_a is not None:
                    expo_a["ind_max_abs_mean"].append(mz_a)

                if self_check and not gate_ok and self_check_hook is not None:
                    gate_ok = self_check_hook(day, j, usable, y, groups,
                                              controls_clean, resid_raw)

                for h in _collect_horizons(days):
                    rr = [(r["f"][j], r["ret"].get(h)) for r in usable
                          if r["ret"].get(h) is not None]
                    _record_ic(ic_rows, "raw_on_method_rows", fname, h,
                               [a for a, _ in rr], [b for _, b in rr], len(rr))
                    rr_r = [(resid_z[i], usable[i]["ret"].get(h))
                            for i in range(len(usable))
                            if _finite(resid_z[i]) and usable[i]["ret"].get(h) is not None]
                    _record_ic(ic_rows, "resid", fname, h,
                               [a for a, _ in rr_r], [b for _, b in rr_r], len(rr_r))

        # ---------- 无方法行的原始 IC（全体有效行）
        for j, fname in enumerate(factors):
            for h in _collect_horizons(days):
                rr = [(r["f"][j], r["ret"].get(h)) for r in dates_rows
                      if _finite(r["f"][j]) and r["ret"].get(h) is not None]
                _record_ic(ic_rows, "raw", fname, h,
                           [a for a, _ in rr], [b for _, b in rr], len(rr))
    agg["ic"] = ic_rows
    return agg


def _collect_horizons(days: list[dict]) -> list[str]:
    hs: list[str] = []
    for day in days:
        for r in day["rows"]:
            for h in r["ret"]:
                if h not in hs:
                    hs.append(h)
        if hs:
            break
    return hs


def _accum(mat: list[list[float]] | None, add: list[list[float]]) -> None:
    if mat is None:
        return
    for i in range(len(mat)):
        for j in range(len(mat)):
            mat[i][j] += add[i][j]


def _record_ic(store: dict, method: str, factor: str, horizon: str,
               xs: list[float], ys: list[float], n_avail: int) -> None:
    if len(xs) < MIN_IC_NAMES:
        return
    ic = spearman(xs, ys)
    if ic is not None:
        store[method][factor][horizon].append(ic)


def summarize_ic_series(series: list[float]) -> dict:
    if not series:
        return {"n": 0, "mean_ic": None, "std_ic": None, "hit_rate": None}
    m = mean(series)
    s = sample_std(series)
    return {
        "n": len(series), "mean_ic": m, "std_ic": s,
        "hit_rate": sum(1 for v in series if v > 0) / len(series),
    }


def finalize_aggregates(agg: dict, factors: list[str], horizons: list[str]) -> dict:
    """把 process_days 的原始累加整理为报告结构。"""
    out: dict = {"corr_matrix": {}, "exposure": {}, "ic": {}, "day_counts": {},
                 "self_checks": {}}
    k = len(factors)
    for meth in ("sym", "gs"):
        m = agg[meth]
        n = m["days_used"]
        cm = {}
        for tag, key in (("before", "corr_before_sum"), ("after", "corr_after_sum")):
            mat = m[key]
            cm[tag] = ([[mat[i][j] / n for j in range(k)] for i in range(k)]
                       if mat and n else None)
        out["corr_matrix"][meth] = {
            "before_mean_daily": cm["before"], "after_mean_daily": cm["after"],
            "after_offdiag_abs_max": max(m["offdiag_max"]) if m["offdiag_max"] else None,
            "after_offdiag_abs_mean_daily_mean":
                mean(m["offdiag_mean"]) if m["offdiag_mean"] else None,
            "ill_conditioned_days": m.get("ill_conditioned_days", 0),
        }
        out["day_counts"][meth] = {
            "days_used": n,
            "median_names_per_day": median(m["names_per_day"]) if m["names_per_day"] else None,
            "skipped": dict(m["skipped"]),
        }
    r = agg["resid"]
    out["day_counts"]["resid"] = {
        "days_used": dict(r["days_used"]),
        "median_names_per_day": {f: (median(r["names_per_day"][f])
                                     if r["names_per_day"][f] else None) for f in factors},
        "skipped": {f: dict(r["skipped"][f]) for f in factors},
        "dropped_control_days": dict(r["dropped_control_days"]),
    }
    for f in factors:
        b, a = r["expo_before"][f], r["expo_after"][f]

        def _ms(seq):
            return {"mean": mean(seq) if seq else None, "n_days": len(seq)}

        out["exposure"][f] = {
            "size_corr_before": _ms(b["size"]),
            "size_corr_after": _ms(a["size"]),
            "industry_r2_before": _ms(b["ind_r2"]),
            "industry_r2_after": _ms(a["ind_r2"]),
            "industry_max_abs_group_mean_z_before": _ms(b["ind_max_abs_mean"]),
            "industry_max_abs_group_mean_z_after": _ms(a["ind_max_abs_mean"]),
            "style_corr_before": {c: _ms(v) for c, v in b["styles"].items()},
            "style_corr_after": {c: _ms(v) for c, v in a["styles"].items()},
        }
    out["self_checks"]["resid_ctrl_corr_pre_zscore_abs_max"] = (
        max(r["ctrl_corr_pre_z_max"]) if r["ctrl_corr_pre_z_max"] else None)
    out["self_checks"]["resid_ctrl_corr_final_abs_max"] = (
        max(r["ctrl_corr_final"]) if r["ctrl_corr_final"] else None)
    out["self_checks"]["resid_ctrl_corr_final_abs_mean"] = (
        mean(r["ctrl_corr_final"]) if r["ctrl_corr_final"] else None)

    ic = agg["ic"]
    out["ic"] = {"horizons": horizons, "by_method": {}, "retention": {}}
    for meth in ("raw", "sym", "gs", "resid", "raw_on_method_rows"):
        out["ic"]["by_method"][meth] = {
            f: {h: summarize_ic_series(ic[meth][f][h]) for h in horizons} for f in factors}
    for meth in ("sym", "gs", "resid"):
        out["ic"]["retention"][meth] = {}
        for f in factors:
            out["ic"]["retention"][meth][f] = {}
            for h in horizons:
                base = summarize_ic_series(ic["raw_on_method_rows"][f][h])["mean_ic"]
                now = summarize_ic_series(ic[meth][f][h])["mean_ic"]
                item = {"mean_ic_raw_same_rows": base, "mean_ic_method": now}
                if base is not None and now is not None and abs(base) >= MIN_RETENTION_DENOM:
                    item["retention"] = now / base
                else:
                    item["retention"] = None
                    item["retention_note"] = (
                        "原始 |mean IC| < 0.005，比值不稳定，不给保留率" if base is not None
                        else "原始 mean IC 缺失")
                out["ic"]["retention"][meth][f][h] = item
    return out


# ================================================================ quantdb 装配（容器 pandas）

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


def _dt_to_iso(dt: str) -> str:
    return f"{dt[:4]}-{dt[4:6]}-{dt[6:]}"


def _pick_date_col(cols: list[str]) -> str | None:
    if "time" in cols:
        return "time"
    if "date" in cols:
        return "date"
    return None


def run_quantdb_list_columns(args) -> int:
    import pandas as pd            # noqa: PLC0415
    import pyarrow.parquet as pq   # noqa: PLC0415

    root = resolve_data_root()
    market = args.market.upper()
    dataset = args.dataset or DEFAULT_DATASET[market]
    key = (market, dataset)
    if key not in DATASET_PATHS:
        raise SystemExit(f"市场 {market} 无数据集 {dataset}；可用：{[d for (m, d) in DATASET_PATHS if m == market]}")
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
    print(f"\n标签列（禁止当因子/控制变量，{len(labels)}）：{labels}")
    print(f"\n辅助列：{sorted(c for c in cols if c in AUX_COLUMNS)}")
    size_cands = AUTO_SIZE_COLS.get(key, [])
    if size_cands:
        print(f"\n规模代理候选（auto 会体检填充度）：{size_cands}")
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
    print("\n行业分类来源：")
    src = INDUSTRY_SOURCES[market]
    print(f"  {src['path']}（{src['note']}）")
    return 0


def _read_panel(parts: list[tuple[str, Path]], need_cols: list[str]) -> tuple:
    """逐分区读取（容忍列集漂移）：返回 (DataFrame[symbol,date,cols...], skipped_parts)。"""
    import pandas as pd            # noqa: PLC0415
    import pyarrow.parquet as pq   # noqa: PLC0415

    frames = []
    skipped = []
    for dt, f in parts:
        cols = list(pq.read_schema(f).names)
        missing = [c for c in need_cols if c not in cols]
        if missing:
            skipped.append({"dt": dt, "missing": missing})
            continue
        date_col = _pick_date_col(cols)
        use = ["symbol"] + ([date_col] if date_col else []) + list(need_cols)
        df = pd.read_parquet(f, columns=use)
        if date_col:
            df = df.rename(columns={date_col: "date"})
            df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
        else:
            df["date"] = _dt_to_iso(dt)
        frames.append(df[["symbol", "date", *need_cols]])
    if not frames:
        raise SystemExit(f"所有分区都缺请求列 {need_cols}；首个缺列分区：{skipped[:1]}")
    panel = pd.concat(frames, ignore_index=True)
    panel = panel.drop_duplicates(subset=["symbol", "date"], keep="first")
    return panel, skipped


def _read_kline(parts: list[tuple[str, Path]]) -> tuple:
    import pandas as pd            # noqa: PLC0415
    import pyarrow.parquet as pq   # noqa: PLC0415

    frames = []
    for _dt, f in parts:
        cols = list(pq.read_schema(f).names)
        use = [c for c in ("symbol", "time", "close", "amount", "published_at") if c in cols]
        df = pd.read_parquet(f, columns=use)
        df["date"] = pd.to_datetime(df["time"]).dt.strftime("%Y-%m-%d")
        frames.append(df)
    if not frames:
        raise SystemExit("行情分区为空")
    kdf = pd.concat(frames, ignore_index=True)
    before = len(kdf)
    kdf = kdf.dropna(subset=["close"])
    if "published_at" in kdf.columns:
        # HK/US 双来源重复行（HK 实测 2024-09-02~2026-05-08 paid_hk+akshare）：
        # 按 published_at 排序 keep='last' 保 akshare 原始价（paid_hk 个别标的带复权缩放）
        kdf = kdf.sort_values(["symbol", "date", "published_at"], kind="stable")
        kdf = kdf.drop_duplicates(subset=["symbol", "date"], keep="last")
        kdf = kdf.drop(columns=["published_at"])
        dup = before - len(kdf)
    else:
        kdf = kdf.drop_duplicates(subset=["symbol", "date"], keep="first")
        dup = before - len(kdf)
    return kdf, dup


def _resolve_size_plan(root: Path, market: str, dataset: str, user_choice: str,
                       fparts: list[tuple[str, Path]]) -> dict:
    """决定规模代理：显式列 / auto 候选体检 / 对数成交额代理。"""
    import pandas as pd            # noqa: PLC0415
    import pyarrow.parquet as pq   # noqa: PLC0415

    key = (market, dataset)
    if user_choice == "dollar_volume":
        return {"kind": "dollar_volume", "column": None, "transform": "ln(20日均成交额)",
                "note": "用户指定对数成交额代理"}
    cands = [c.strip() for c in user_choice.split(",") if c.strip()] \
        if user_choice != "auto" else AUTO_SIZE_COLS.get(key, [])
    for cand in cands:
        stats = []
        sample = fparts[:: max(1, len(fparts) // 3)][:3]
        ok = True
        for _dt, f in sample:
            cols = list(pq.read_schema(f).names)
            if cand not in cols:
                ok = False
                break
            s = pd.read_parquet(f, columns=[cand])[cand]
            nn = float(s.notna().mean())
            zr = float((s.fillna(0) == 0).mean())
            stats.append({"nn": nn, "zero_share": zr})
            if nn < SIZE_FILL_MIN_NONNULL or zr > SIZE_FILL_MAX_ZERO:
                ok = False
                break
        if ok:
            already_log = cand.startswith("ln_") or cand.startswith("style_ln_")
            return {"kind": "column", "column": cand,
                    "transform": "原值" if already_log else "ln(·)",
                    "note": f"填充体检通过（样本分区非空≥{SIZE_FILL_MIN_NONNULL:.0%}、零值≤{SIZE_FILL_MAX_ZERO:.0%}）"}
        if user_choice != "auto":
            raise SystemExit(
                f"指定规模列 {cand} 未通过填充体检（样本 = {stats}）；"
                "该列很可能未填充。可改用 --size-col dollar_volume 走对数成交额代理。")
    return {"kind": "dollar_volume", "column": None, "transform": "ln(20日均成交额)",
            "note": "市值类候选列未通过填充体检（HK l1_factors 实测市值列全 0），"
                    "退回对数成交额代理（规模+流动性混合代理，源技能 log_dollar_vol 同款口径）"}


def load_industry_map(root: Path, market: str, level: str) -> tuple[dict, dict]:
    import pandas as pd            # noqa: PLC0415

    src = INDUSTRY_SOURCES[market]
    path = root / src["path"]
    if market == "CN":
        df = pd.read_parquet(path, columns=[src["key"], src["column"]])
        mapping = {str(k): str(v).strip() for k, v in
                   zip(df[src["key"]], df[src["column"]], strict=False) if str(v).strip()}
        info = {"source": src["path"], "column": src["column"],
                "n_categories": len(set(mapping.values())), "n_symbols": len(mapping),
                "note": src["note"]}
        return mapping, info
    # HK / US：目录内 {SYM}.parquet
    col = src["columns"][level] if market == "US" else src["column"]
    mapping = {}
    fails = 0
    for fp in path.glob("*.parquet"):
        try:
            d = pd.read_parquet(fp, columns=[col])
        except Exception:
            fails += 1
            continue
        vals = [str(v).strip() for v in d[col].dropna().tolist() if str(v).strip()]
        if vals:
            mapping[fp.stem] = vals[0]
    info = {"source": src["path"], "column": col, "level": level if market == "US" else None,
            "n_categories": len(set(mapping.values())), "n_symbols": len(mapping),
            "note": src["note"] + (f"；{fails} 个文件读取失败已跳过" if fails else "")}
    return mapping, info


def run_quantdb(args) -> dict:
    import pandas as pd            # noqa: PLC0415

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
    styles = [c.strip() for c in (args.controls or "").split(",") if c.strip()]
    bad = [c for c in factors + styles if is_label_column(c)]
    if bad:
        raise SystemExit(
            f"拒绝把标签列当因子/控制变量：{bad}（未来收益标签，用了就是前视泄漏）")
    overlap = set(factors) & set(styles)
    if overlap:
        raise SystemExit(f"控制列与因子列重叠：{sorted(overlap)}——残差化会把该因子直接清空；请拆开重跑")

    horizons = sorted({int(h) for h in str(args.horizons).split(",") if h.strip()})
    if not horizons or any(h <= 0 for h in horizons):
        raise SystemExit("--horizons 需为正整数列表，如 1,5")

    methods = [m.strip() for m in args.methods.split(",") if m.strip()]
    bad_m = [m for m in methods if m not in ("sym", "gs", "resid")]
    if bad_m:
        raise SystemExit(f"未知方法 {bad_m}；可用 sym,gs,resid")
    if len(factors) > MAX_GS_FACTORS and "gs" in methods:
        raise SystemExit(f"Gram-Schmidt 因子数上限 {MAX_GS_FACTORS}")
    if len(factors) > MAX_ORTH_FACTORS and ("sym" in methods or "gs" in methods):
        print(f"⚠ 因子数 {len(factors)} 超过 {MAX_ORTH_FACTORS}，sym/gs 已自动跳过（只跑 resid）")
        methods = [m for m in methods if m == "resid"] or ["resid"]

    # ---- 窗口
    kparts_all = sorted(kline_dir.glob("dt=*"))
    if not kparts_all:
        raise SystemExit(f"行情目录无分区：{kline_dir}")
    fparts_all = sorted(factor_dir.glob("dt=*"))
    if not fparts_all:
        raise SystemExit(f"因子数据集无分区：{factor_dir}")
    end_compact = (args.end.replace("-", "") if args.end else
                   min(kparts_all[-1].name[3:], fparts_all[-1].name[3:]))
    if args.end is None and end_compact < min(kparts_all[-1].name[3:], fparts_all[-1].name[3:]):
        print(f"⚠ 因子/行情最新分区不一致，end 自动收敛到 {_dt_to_iso(end_compact)}")
    if args.start:
        start_compact = args.start.replace("-", "")
    else:
        d = _date.fromisoformat(_dt_to_iso(end_compact))
        start_compact = (d.replace(year=d.year - 2)).strftime("%Y%m%d")
    if start_compact > end_compact:
        raise SystemExit(f"窗口为空：start {start_compact} > end {end_compact}")

    kparts = _list_dt_partitions(kline_dir, start_compact, end_compact)
    if not kparts:
        raise SystemExit(f"行情无 {_dt_to_iso(start_compact)}~{_dt_to_iso(end_compact)} 分区")
    fparts = _list_dt_partitions(factor_dir, start_compact, end_compact)
    if not fparts:
        raise SystemExit(f"因子数据集无 {_dt_to_iso(start_compact)}~{_dt_to_iso(end_compact)} 分区：{factor_dir}")
    tail = [(p.name[3:], p / "data.parquet") for p in kparts_all if p.name[3:] > end_compact][:40]
    # 对数成交额代理需要窗口前的历史（20 日均额）
    pre_start = (_date.fromisoformat(_dt_to_iso(start_compact)) - timedelta(days=60)).strftime("%Y%m%d")
    kparts_pre = _list_dt_partitions(kline_dir, pre_start, start_compact)[:-1]  # 不含 start 当日，避免重复

    # ---- 票池（窗口末日成交额前 N）
    last_dt, last_file = kparts[-1]
    if args.symbols:
        pool = [s.strip() for s in args.symbols.split(",") if s.strip()]
        universe_info = {"method": "explicit_symbols", "asof_date": _dt_to_iso(last_dt),
                         "n_symbols": len(pool)}
    else:
        u = pd.read_parquet(last_file, columns=["symbol", "amount"])
        u = u.dropna(subset=["amount"])
        u = u[u["amount"] > 0].sort_values("amount", ascending=False)
        n_take = args.top_n if args.top_n and args.top_n > 0 else len(u)
        pool = u["symbol"].head(n_take).astype(str).tolist()
        universe_info = {"method": f"top_{n_take}_by_amount", "asof_date": _dt_to_iso(last_dt),
                         "n_symbols": len(pool),
                         "note": "按窗口末日成交额选取（事后票池，含轻微选择偏差；非逐日动态池）"}
    pset = set(pool)

    # ---- 规模代理（先解析，列式代理需一并读入面板）
    size_plan = _resolve_size_plan(root, market, dataset, args.size_col, fparts)
    col_extra = [size_plan["column"]] if size_plan["kind"] == "column" else []

    # ---- 因子 + 控制列面板
    need_cols = list(dict.fromkeys(factors + styles + col_extra))
    panel, skipped_parts = _read_panel(fparts, need_cols)
    panel = panel[panel["symbol"].isin(pset)]

    # ---- 行情（前视尾部 + 成交额代理历史）
    kdf, dup_rows = _read_kline(kparts + tail)
    kdf_pool = kdf[kdf["symbol"].isin(pset)].sort_values(["symbol", "date"])
    if kdf_pool.empty:
        raise SystemExit("票池在行情窗口无数据（检查 symbol 格式：CN=000001.SZ / HK=0001.HK / US=NVDA）")
    ret = {}
    for h in horizons:
        col = f"ret_{h}"
        kdf_pool = kdf_pool.copy()
        kdf_pool[col] = kdf_pool.groupby("symbol", sort=False)["close"].shift(-h) / kdf_pool["close"] - 1.0
        ret[h] = col

    # ---- 规模代理并回面板
    if size_plan["kind"] == "column":
        size_panel = panel[["symbol", "date", size_plan["column"]]].rename(
            columns={size_plan["column"]: "size"})
        if size_plan["transform"] == "ln(·)":
            size_panel = size_panel.copy()
            size_panel["size"] = size_panel["size"].where(size_panel["size"] > 0)
            size_panel["size"] = size_panel["size"].map(
                lambda v: math.log(v) if v == v and v and v > 0 else float("nan"))
    else:
        kdf_all, _dup2 = _read_kline(kparts_pre + kparts)
        kdf_all = kdf_all[kdf_all["symbol"].isin(pset)].sort_values(["symbol", "date"])
        dv = (kdf_all.groupby("symbol", sort=False)["amount"]
              .rolling(20, min_periods=5).mean().reset_index(level=0, drop=True))
        kdf_all = kdf_all.assign(dollar_vol=dv)
        dvp = kdf_all[["symbol", "date", "dollar_vol"]].copy()
        dvp["size"] = dvp["dollar_vol"].map(
            lambda v: math.log(v) if v == v and v and v > 0 else float("nan"))
        dvp = dvp[dvp["date"] >= _dt_to_iso(start_compact)]
        size_panel = dvp[["symbol", "date", "size"]]
        mean_fill = float((size_panel["size"] > 0).mean())
        size_plan["note"] += f"（对数成交额非空占比 {mean_fill:.1%}）"

    # ---- 行业映射
    if args.no_industry:
        ind_map, ind_info = {}, {"source": None, "note": "--no-industry：基准仅规模+风格"}
    else:
        ind_map, ind_info = load_industry_map(root, market, args.industry_level)

    merged = panel.merge(size_panel, on=["symbol", "date"], how="left")
    for _h, col in ret.items():
        merged = merged.merge(kdf_pool[["symbol", "date", col]], on=["symbol", "date"], how="left")

    n_missing_ind = 0
    days: list[dict] = []
    by_date = merged.groupby("date", sort=True)
    style_cols = list(styles)
    for dt, g in by_date:
        rows = []
        for tup in g.itertuples(index=False):
            d = dict(zip(g.columns, tup, strict=False))
            ind = ind_map.get(d["symbol"])
            if ind is None:
                if args.no_industry:
                    ind = "ALL"
                else:
                    ind = "UNKNOWN"
                    n_missing_ind += 1
            rows.append({
                "symbol": d["symbol"], "ind": ind,
                "size": float(d["size"]) if d["size"] == d["size"] else float("nan"),
                "styles": [float(d[c]) if d[c] == d[c] else float("nan") for c in style_cols],
                "f": [float(d[c]) if d[c] == d[c] else float("nan") for c in factors],
                "ret": {f"H{h}": (float(d[ret[h]]) if d[ret[h]] == d[ret[h]] else None)
                        for h in horizons},
            })
        days.append({"date": dt, "rows": rows})
    if not days:
        raise SystemExit("窗口内无有效面板行")
    n_pool_symbols = merged["symbol"].nunique()
    ind_cov = 1.0 if args.no_industry else (
        (n_pool_symbols - len({r["symbol"] for day in days for r in day["rows"]
                               if r["ind"] == "UNKNOWN"})) / max(n_pool_symbols, 1))

    # ---- 自检钩子：FWL vs numpy lstsq（首个处理日，显式哑变量矩阵）
    self_check_result: dict = {"status": "skipped"}

    def _hook(day, j, usable, y, groups, controls_clean, resid_raw):
        try:
            import numpy as np     # noqa: PLC0415
        except ImportError:
            self_check_result.update({"status": "unavailable", "reason": "容器无 numpy"})
            return True
        cats = sorted(set(groups))
        gidx = {g: i for i, g in enumerate(cats)}
        x = np.zeros((len(usable), len(cats) + len(controls_clean)))
        for i, g in enumerate(groups):
            x[i, gidx[g]] = 1.0
        for si, c in enumerate(controls_clean):
            x[:, len(cats) + si] = np.asarray(c)
        beta, *_ = np.linalg.lstsq(x, np.asarray(y), rcond=None)
        resid_np = np.asarray(y) - x @ beta
        dev = float(np.max(np.abs(resid_np - np.asarray(resid_raw))))
        self_check_result.update({
            "status": "ok", "date": day["date"], "factor": factors[j],
            "n_rows": len(usable), "n_dummies": len(cats),
            "max_abs_diff_vs_numpy_lstsq": dev,
            "passed": dev < 1e-8,
        })
        return True

    agg = process_days(days, factors, methods, gs_order=args.gs_order,
                       self_check=not args.no_self_check, self_check_hook=_hook,
                       resid_final_scale=args.resid_final_scale)
    finalized = finalize_aggregates(agg, factors, _collect_horizons(days))

    caveats = []
    if skipped_parts:
        caveats.append(
            f"因子/控制列在 {len(skipped_parts)} 个分区缺失（列集随年份漂移），这些分区被跳过："
            f"{skipped_parts[:3]}{'...' if len(skipped_parts) > 3 else ''}")
    if dup_rows:
        caveats.append(f"行情存在 {dup_rows} 行重复 (symbol,date)（HK 双来源已知问题），"
                       "已按 published_at 取最新（akshare 原始价）去重。")
    if n_missing_ind:
        caveats.append(f"票池中 {n_missing_ind} 行个股-日缺行业分类，归入 UNKNOWN 组（单独占一组）。")
    caveats.append(f"票池口径：{universe_info['method']} @ {universe_info['asof_date']}"
                   + ("（显式指定）" if args.symbols else "（事后选池，含轻微选择偏差）"))
    if "resid" in methods:
        caveats.append("残差化后 IC 下降是预期行为（剥离的暴露本身含收益预测成分时尤甚）；"
                       "保留率低不等于因子失效，独立性与保真是两个维度。")
        if args.resid_final_scale == "winsorize_zscore":
            caveats.append("resid_final_scale=winsorize_zscore（源技能口径）：截尾是非线性操作，"
                           "实测会在个别交易日重新引入 ≤1e-1 量级的规模暴露（见自检段）；"
                           "需要严格零暴露时分最终标准化请用 affine。")
    caveats.append("防未来函数：因子/控制列必须是当日收盘前可得信息；标签列已由脚本拒绝，"
                   "但因子本身的时点正确性仍由数据生产方保证。行业分类为静态快照（见基准信息）。")
    caveats.append("本结果不构成投资建议。")

    wrapper = {
        "mode": "quantdb",
        "market": market,
        "dataset": dataset,
        "data_root": str(root),
        "window": {"start": _dt_to_iso(start_compact), "end": _dt_to_iso(end_compact),
                   "trading_days": len(days)},
        "data_through": last_dt,
        "returns": {"source": KLINE_PATHS[market], "adjustment": KLINE_ADJUSTMENT_NOTE[market],
                    "definition": "ret_h(t)=close(t+h)/close(t)-1，h=同一标的序列的后续第 h 个交易日"},
        "universe": universe_info,
        "benchmark": {
            "industry": {**ind_info, "coverage_pct": ind_cov},
            "size": size_plan,
            "styles": styles,
        },
        "methods_run": methods,
        "gs_order": args.gs_order,
        "resid_final_scale": args.resid_final_scale,
        "factors": factors,
        "horizons": [f"H{h}" for h in horizons],
        **finalized,
        "caveats": caveats,
    }
    wrapper["self_checks"]["fwl_vs_numpy_lstsq"] = self_check_result
    return wrapper


def run_demo(args) -> dict:
    golden = golden_checks()
    days = build_demo_days()
    factors = ["F1", "F2", "F3", "F4"]
    methods = [m.strip() for m in args.methods.split(",") if m.strip()] or ["sym", "gs", "resid"]
    agg = process_days(days, factors, methods, gs_order=args.gs_order,
                       resid_final_scale=args.resid_final_scale)
    horizons = _collect_horizons(days)
    finalized = finalize_aggregates(agg, factors, horizons)
    n_pass = sum(1 for c in golden if c["passed"])
    caveats = [
        "demo 数据为确定性合成面板（3 行业 × 60 标的 × 45 日，LCG 种子固定），用于验证实现与手算金样；"
        "数字本身无市场含义。",
        "F1/F2/F3 与行业、规模存在构造性相关，用于展示暴露清零与 IC 保真诊断。",
        "本结果不构成投资建议。",
    ]
    wrapper = {
        "mode": "demo",
        "window": {"start": days[0]["date"], "end": days[-1]["date"],
                   "trading_days": len(days)},
        "universe": {"method": "synthetic_panel", "n_symbols": len({r["symbol"] for d in days for r in d["rows"]})},
        "benchmark": {
            "industry": {"source": "synthetic_3_industries", "n_categories": 3,
                         "coverage_pct": 1.0},
            "size": {"kind": "column", "column": "size", "transform": "ln(·)"},
            "styles": [],
        },
        "methods_run": methods,
        "gs_order": args.gs_order,
        "resid_final_scale": args.resid_final_scale,
        "factors": factors,
        "horizons": horizons,
        **finalized,
        "golden_checks": golden,
        "golden_summary": f"{n_pass}/{len(golden)} 条通过",
        "caveats": caveats,
    }
    return wrapper


# ================================================================ 渲染

def _fmt(v, dp=4, signed=False) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    return f"{v:+.{dp}f}" if signed else f"{v:.{dp}f}"


def render_text(rep: dict) -> str:
    lines = []
    head = f"因子正交化报告（{rep.get('market', 'DEMO')}"
    if rep.get("dataset"):
        head += f" / {rep['dataset']}"
    head += "）"
    lines.append(head)
    lines.append("=" * 64)
    w = rep["window"]
    lines.append(f"窗口：{w['start']} → {w['end']}  ·  交易日 {w['trading_days']}"
                 + (f"  ·  票池 {rep['universe'].get('n_symbols')}" if rep["universe"].get("n_symbols") else ""))
    bm = rep["benchmark"]
    if bm.get("industry", {}).get("source"):
        lines.append(f"基准行业：{bm['industry']['source']}"
                     f"（{bm['industry'].get('n_categories')} 类，覆盖 {bm['industry'].get('coverage_pct', 0):.1%}）")
    if bm.get("size"):
        lines.append(f"基准规模：{bm['size'].get('transform', '')}"
                     + (f" 列 {bm['size'].get('column')}" if bm["size"].get("column") else "（对数成交额代理）"))
    if bm.get("styles"):
        lines.append(f"基准风格：{', '.join(bm['styles'])}")
    lines.append(f"因子（{len(rep['factors'])}）：{', '.join(rep['factors'])}")

    for meth in ("sym", "gs"):
        cm = rep["corr_matrix"].get(meth)
        if not cm or cm.get("before_mean_daily") is None:
            continue
        name = "对称正交（Löwdin）" if meth == "sym" else f"Gram-Schmidt（次序={rep.get('gs_order')}）"
        lines.append("")
        lines.append(f"【{name}】")
        k = len(rep["factors"])
        for tag, mat in (("正交前", cm["before_mean_daily"]), ("正交后", cm["after_mean_daily"])):
            lines.append(f"  {tag}因子相关矩阵（全窗口日均）：")
            lines.append("        " + "".join(f"{f[:8]:>10}" for f in rep["factors"]))
            for i in range(k):
                lines.append(f"  {rep['factors'][i][:6]:>6}" +
                             "".join(f"{mat[i][j]:>10.4f}" for j in range(k)))
        lines.append(f"  正交后 max|非对角相关|：{_fmt(cm.get('after_offdiag_abs_max'), 3)}"
                     f"（应≈0；日均均值 {_fmt(cm.get('after_offdiag_abs_mean_daily_mean'), 3)}）")
        dc = rep["day_counts"].get(meth, {})
        lines.append(f"  处理天数：{dc.get('days_used')}"
                     f"（跳过 {dc.get('skipped', {}) or '无'}；截面中位数 {dc.get('median_names_per_day')} 只/日）")

    if "resid" in rep["methods_run"]:
        lines.append("")
        lines.append("【基准暴露残差化】逐因子独立回归（行业哑变量 + 规模 + 风格）")
        lines.append(f"  {'因子':<14}{'规模corr前':>10}{'规模corr后':>10}{'行业R²前':>9}{'行业R²后':>9}"
                     f"{'行业max|z|前':>13}{'行业max|z|后':>13}")
        for f in rep["factors"]:
            e = rep["exposure"][f]
            lines.append(f"  {f[:12]:<14}"
                         f"{_fmt(e['size_corr_before']['mean'], 3, True):>10}"
                         f"{_fmt(e['size_corr_after']['mean'], 3, True):>10}"
                         f"{_fmt(e['industry_r2_before']['mean'], 3):>9}"
                         f"{_fmt(e['industry_r2_after']['mean'], 3):>9}"
                         f"{_fmt(e['industry_max_abs_group_mean_z_before']['mean'], 3):>13}"
                         f"{_fmt(e['industry_max_abs_group_mean_z_after']['mean'], 3):>13}")
        dc = rep["day_counts"]["resid"]
        lines.append(f"  处理天数（逐因子）：{dc['days_used']}")
        sk = {f: v for f, v in dc["skipped"].items() if v}
        if sk:
            lines.append(f"  跳过明细：{sk}")

    lines.append("")
    lines.append("【IC 保留率】秩 IC（与原始同一天同一截面行对比）")
    lines.append(f"  {'因子':<14}{'周期':<6}{'原始IC':>10}{'sym':>10}{'gs':>10}{'resid':>10}{'残差保留':>10}")
    for f in rep["factors"]:
        for h in rep["ic"]["horizons"]:
            base = rep["ic"]["retention"]["resid"][f][h]["mean_ic_raw_same_rows"]
            row = [f[:12], h, _fmt(base, 4, True)]
            for meth in ("sym", "gs"):
                v = rep["ic"]["by_method"][meth][f][h]["mean_ic"]
                row.append(_fmt(v, 4, True))
            v = rep["ic"]["by_method"]["resid"][f][h]["mean_ic"]
            row.append(_fmt(v, 4, True))
            ret = rep["ic"]["retention"]["resid"][f][h]["retention"]
            row.append(_fmt(ret, 2) if ret is not None else "—")
            lines.append(f"  {row[0]:<14}{row[1]:<6}{row[2]:>10}{row[3]:>10}{row[4]:>10}{row[5]:>10}{row[6]:>10}")
    lines.append("  （原始 IC 全样本口径见 JSON ic.by_method.raw；保留率仅在 |原始 mean IC|≥0.005 时给出）")

    lines.append("")
    lines.append("【独立校验】")
    sc = rep.get("self_checks", {})
    if "sym" in rep["corr_matrix"] and rep["corr_matrix"]["sym"]:
        v = rep["corr_matrix"]["sym"].get("after_offdiag_abs_max")
        lines.append(f"  对称正交后相关矩阵 max|非对角| = {_fmt(v, 3)}（预期 ≤1e-12 量级）")
    if "gs" in rep["corr_matrix"] and rep["corr_matrix"]["gs"]:
        v = rep["corr_matrix"]["gs"].get("after_offdiag_abs_max")
        lines.append(f"  Gram-Schmidt 后 max|非对角| = {_fmt(v, 3)}")
    v = sc.get("resid_ctrl_corr_pre_zscore_abs_max")
    if v is not None:
        lines.append(f"  残差（标准化前）与基准 |corr| 最大 = {v:.2e}（预期 ≤1e-12）")
    v = sc.get("resid_ctrl_corr_final_abs_max")
    if v is not None:
        vm = sc.get("resid_ctrl_corr_final_abs_mean")
        lines.append(f"  残差（最终产出，{rep.get('resid_final_scale', 'affine')} 标准化后）"
                     f"与规模 |corr|：最大 {v:.2e}、日均 {vm:.2e}")
    chk = sc.get("fwl_vs_numpy_lstsq", {})
    if chk.get("status") == "ok":
        lines.append(f"  FWL 残差 vs numpy.linalg.lstsq（显式哑变量，{chk['date']}/{chk['factor']}）："
                     f"max|diff| = {chk['max_abs_diff_vs_numpy_lstsq']:.2e}"
                     f"（{'通过' if chk.get('passed') else '⚠ 未通过'}）")
    elif chk.get("status") == "unavailable":
        lines.append(f"  numpy 对拍跳过：{chk.get('reason')}")
    if rep.get("golden_summary"):
        lines.append(f"  金样断言：{rep['golden_summary']}")
        for c in rep.get("golden_checks", []):
            if not c["passed"]:
                lines.append(f"    ✗ {c['name']}：期望 {c['expected']}，实际 {c['actual']}")

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
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, Counter):
        return dict(obj)
    return obj


# ================================================================ 入口

def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="因子正交化工具箱（对称正交 / Gram-Schmidt / 基准暴露残差化；demo 纯标准库 / QuantDB 本地直读）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--demo", action="store_true", help="内置确定性合成数据（纯标准库）")
    mode.add_argument("--quantdb", action="store_true", help="QuantDB 直读模式（容器内 pandas/pyarrow）")

    p.add_argument("--market", default="CN", choices=["CN", "HK", "US"], help="市场（quantdb 模式，默认 CN）")
    p.add_argument("--dataset", default=None, help="因子数据集：CN=features_daily/l1_factors/l2_factors；HK/US=l1_factors")
    p.add_argument("--factors", default=None, help="待正交因子列，逗号分隔（quantdb 模式）")
    p.add_argument("--controls", default=None, help="额外风格控制列（来自同一数据集），逗号分隔；规模代理自动解析")
    p.add_argument("--size-col", default="auto",
                   help="规模代理：auto（默认，按市场候选列体检）/ 显式列名 / dollar_volume（对数成交额）")
    p.add_argument("--methods", default="sym,gs,resid", help="方法：sym,gs,resid（默认全部）")
    p.add_argument("--resid-final-scale", default="affine",
                   choices=["affine", "winsorize_zscore"],
                   help="残差最终标准化：affine=纯仿射 z-score（默认，严格保持零暴露）；"
                        "winsorize_zscore=源技能口径（截尾会重新引入个别日的少量暴露）")
    p.add_argument("--gs-order", default="asis", choices=["asis", "reverse"],
                   help="Gram-Schmidt 次序（默认 asis；reverse 做顺序敏感性检查）")
    p.add_argument("--industry-level", default="sector", choices=["sector", "industry"],
                   help="US 行业粒度（默认 sector 11 类；其余市场忽略）")
    p.add_argument("--no-industry", action="store_true", help="跳过行业哑变量（基准仅规模+风格）")
    p.add_argument("--start", default=None, help="起始日 YYYY-MM-DD（默认：end 前推 2 年）")
    p.add_argument("--end", default=None, help="结束日 YYYY-MM-DD（默认：因子与行情最新分区较早者）")
    p.add_argument("--top-n", type=int, default=300, help="窗口末日成交额前 N 只（默认 300；0=全部）")
    p.add_argument("--symbols", default=None, help="显式票池（后缀式逗号分隔），给定则忽略 --top-n")
    p.add_argument("--horizons", default="1,5", help="前视周期（交易日），默认 1,5")
    p.add_argument("--no-self-check", action="store_true", help="关闭 numpy 对拍自检")
    p.add_argument("--out", default=None, help="JSON 报告输出路径（父目录自动创建）")
    p.add_argument("--list-columns", action="store_true", help="列出数据集列与填充度后退出（quantdb 模式）")
    args = p.parse_args(argv)

    if args.list_columns:
        if not args.quantdb:
            raise SystemExit("--list-columns 仅在 --quantdb 模式下可用")
        return run_quantdb_list_columns(args)

    if args.demo:
        rep = run_demo(args)
    else:
        rep = run_quantdb(args)

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
