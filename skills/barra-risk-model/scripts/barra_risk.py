#!/usr/bin/env python3
"""Barra 式多因子风险模型（结构化截面风险）— 离线确定性引擎 + QuantDB(CN/HK/US) 数据装配。

来源：quantskills/skill-risk-model（GPL-3.0-only）。
方法论保留：风格+行业暴露 X → 逐日截面 WLS（权重 ∝ √市值）估因子收益 f 与特异收益 u
→ 因子协方差 F（Ledoit-Wolf 闭式收缩，默认）/ EWMA + 特异方差 Δ（EWMA）
→ 组合风险分解 σ² = b'Fb + w'Δw（Euler CCTR 逐因子归因），Σ = XFX' + Δ 可作为组合优化输入。

本地化（2026-10 标定）：
  --demo     内置确定性 3 因子小算例（纯标准库，宿主机/dsh 可跑，手算可验证）。
  --quantdb  pandas/pyarrow，**在 quantmind 容器内运行**：
     风格暴露（列部分）：SIZE/VALUE/LIQUIDITY 按市场列映射表选取（缺列/常量自动降级，见
        STYLE_SPEC；HK/US 无真实市值列，SIZE=ln(20 日均成交额) 代理，已实测披露口径）。
     价格类暴露（自算）：MOMENTUM(12-1)/SHORT_REV(21d)/VOLATILITY(60d)/BETA(120d)，
        由 daily_forward（CN 前复权 / HK/US 不复权）滚动计算。
     行业：CN=instrument_detail.rs_hyname（128 类，静态快照）；HK=akshare_profile 所属行业
        （31 类）；US=sector 目录 sector 列（11 类）；少于 --min-industry-names 的行业与缺失
        归入「其他」哑变量，行业数上限 --max-industries。
     票池：窗口末日（≤end 的最后一个行情日）成交额降序前 N（--top-n，默认 300）或 --symbols。
     权重：--weight-by sqrt_mcap（默认；CN=√total_mv，HK/US=√20 日均成交额代理）或 equal。
     HK 去重：daily_forward 2024-09-02~2026-05-08 有 paid_hk/akshare 双来源重复行，
        按 (symbol,date,published_at) 排序 keep='last'（保 akshare 原始价）。

标签列护栏：features_daily 各版本带 return_{n}d（≤2026-09-20，实测为**未来收益标签**）与
future_return_{n}d（≥2026-09-21，多数分区为空）；HK/US l1_factors 的 return_{n}d 实测为
**当日已实现收益**（非前视）。脚本的暴露候选表不含任何 return_*/future_* 列。

独立校验（--validate，默认 30 个抽样交易日）：numpy 正规方程解 vs numpy lstsq（QR）vs
纯标准库高斯消元逐日互校；Ledoit-Wolf 收缩强度纯 Python 与 numpy 双算；风险分解恒等式
（因子风险+特异风险=总风险）残差；显式 N×N 的 Σ=XFX'+Δ 二次型与分解值互校。

用法：
  python3 barra_risk.py --demo
  python3 barra_risk.py --quantdb --market CN --start 2024-01-01 --end 2025-12-31 \\
      --top-n 300 --out /data/reports/barra-risk-model/cn_2024_2025.json
  python3 barra_risk.py --quantdb --market CN --list-columns        # 探查列与填充度

输出：stdout 中文表格 + JSON 报告（--out）。结果仅描述历史风险结构，不构成投资建议。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from pathlib import Path

# ================================================================ 常量与契约

ANNUAL = 252.0                    # 年化因子（交易日）
RIDGE_REL = 1e-8                  # ℓ2 岭系数（×tr(X'X)/K，源为绝对值 1e-8，本地相对化）
MIN_REGRESSION_DAYS = 60          # 回归期数少于此拒绝输出
WARN_REGRESSION_DAYS = 252        # 少于此报告样本偏少
MIN_NAMES_OVER_FACTORS = 2        # 当日有效标的数须 ≥ K + 2（源口径）
MAX_ABS_RET = 0.5                 # |日收益|>50% 视为数据异常剔除（计数上报）
WARMUP_CAL_DAYS = 480             # 窗口起点前多读的日历天数（覆盖 252+21 交易日动量窗）
TAIL_PARTITIONS = 5               # 窗口末端后多读的分区数（前视一日 y 需要）

DATA_ROOT_CANDIDATES = ["/data", "/quantmind/data", "/home/zbox/projects/quantmind/data"]

MARKET_KLINE = {
    "CN": "quantdb/1_kline_data/daily_forward",     # 前复权
    "HK": "quanthk/1_kline_data/daily_forward",     # 不复权原始价
    "US": "quantus/1_kline_data/daily_forward",     # 不复权原始价
}
KLINE_ADJUSTMENT_NOTE = {
    "CN": "daily_forward=前复权（收益已含分红送转）",
    "HK": "daily_forward=不复权（价格收益；2024-09-02~2026-05-08 双来源重复行读取侧去重）",
    "US": "daily_forward=不复权（价格收益）",
}
DATASET_PATHS = {
    ("CN", "features_daily"): "quantdb/6_ml_datasets/features_daily",
    ("CN", "l1_factors"): "quantdb/6_ml_datasets/l1_factors",
    ("HK", "l1_factors"): "quanthk/6_ml_datasets/l1_factors",
    ("US", "l1_factors"): "quantus/6_ml_datasets/l1_factors",
}
DEFAULT_DATASET = {"CN": "features_daily", "HK": "l1_factors", "US": "l1_factors"}

# 列类风格映射：style → market → [(列名, 变换), ...]（按序取首个可用且非常量列）
STYLE_SPEC = {
    "SIZE": {
        "CN": [("total_mv", "log"), ("float_mv", "log"), ("fun_total_mv", "log")],
        "HK": [("liq_amount_ma_20", "log"), ("ln_mv_total", "identity")],
        "US": [("liq_amount_ma_20", "log"), ("ln_mv_total", "identity")],
    },
    "VALUE": {
        "CN": [("bp", "identity"), ("fun_bp", "identity"), ("pb", "reciprocal")],
        "HK": [],
        "US": [],
    },
    "LIQUIDITY": {
        "CN": [("hs_turnover", "log1p"), ("amount_ma_5", "log")],
        "HK": [("liq_turnover_os", "log1p"), ("liq_amihud_20", "log1p")],
        "US": [("liq_turnover_os", "log1p"), ("liq_amihud_20", "log1p")],
    },
}
SIZE_PROXY_NOTE = {
    "CN": "SIZE=ln(total_mv)（真实总市值）",
    "HK": "SIZE=ln(20 日均成交额/成交额)（总市值列全 0 不可用，成交额规模代理）",
    "US": "SIZE=ln(20 日均成交额/成交额)（总市值列全 0 不可用，成交额规模代理）",
}
LIQUIDITY_NOTE = {
    "CN": "LIQUIDITY=ln(1+换手率)（高=活跃）；旧列集兜底 ln(成交额 5 日均) 与 SIZE 有共线性",
    "HK": "LIQUIDITY=ln(1+换手 liq_turnover_os)（高=活跃）；兜底 Amihud 为不流动口径，方向相反",
    "US": "LIQUIDITY=ln(1+换手 liq_turnover_os)（高=活跃）；兜底 Amihud 为不流动口径，方向相反",
}
VALUE_NOTE = {"CN": "VALUE=账面市值比 B/P（bp 列优先，缺则 1/pb）", "HK": "本市场 bp/pb 列实测全 0，VALUE 不可用", "US": "同 HK"}
ZERO_PLACEHOLDER_NOTE = {
    "HK": "HK l1_factors 的 total_mv/float_mv/bp/pb/pe_ttm/turnover_rate/style_bp 列为非空但恒 0 的占位符；ln_mv_total/style_ln_mv_total 实测=ln(当日成交额)，非市值。",
    "US": "US l1_factors 同 HK：total_mv/float_mv/bp/pb/pe_ttm/turnover_rate/style_bp 恒 0 占位；ln_mv_total/style_ln_mv_total 实测=ln(当日成交额)，非市值。",
}

# 价格类暴露窗口：(滚动窗, 近期跳过天数)
PRICE_WINDOWS = {"MOMENTUM": (252, 21), "SHORT_REV": (21, 0), "VOLATILITY": (60, 0), "BETA": (120, 0)}
STYLE_ORDER = ["SIZE", "VALUE", "MOMENTUM", "SHORT_REV", "VOLATILITY", "BETA", "LIQUIDITY"]
STYLE_LABELS_CN = {
    "SIZE": "规模", "VALUE": "价值", "MOMENTUM": "动量", "SHORT_REV": "短期反转",
    "VOLATILITY": "波动率", "BETA": "市场贝塔", "LIQUIDITY": "流动性",
}

INDUSTRY_SOURCES = {
    "CN": {
        "kind": "file",
        "path": "quantdb/2_base_sector/instrument_detail/instrument_detail.parquet",
        "column": "rs_hyname", "symbol": "Symbol",
        "note": "通达信行业（128 类；静态快照，HqDate 见文件，期间调类不反映）",
    },
    "HK": {
        "kind": "dir",
        "path": "quanthk/2_base_sector/akshare_profile",
        "column": "所属行业", "symbol": "symbol",
        "note": "akshare 公司资料「所属行业」（31 类；非 GICS）",
    },
    "US": {
        "kind": "dir",
        "path": "quantus/2_base_sector/sector",
        "column": "sector", "symbol": "symbol",
        "note": "yahoo 行业分类（11 类，GICS 风格）",
    },
}
OTHER_INDUSTRY = "其他"

# 标签列（--list-columns 标注用，防前视；不得进入任何暴露）
LABEL_COLUMNS = {"future_return_1d", "future_return_3d", "future_return_5d", "future_return_10d",
                 "future_return_20d", "future_return_60d", "label_return"}

CAVEATS_BASE = [
    "风险模型是对历史协方差结构的结构化描述，不预测收益、不构成投资建议。",
    "行业分类为静态/快照口径（CN instrument_detail、HK akshare_profile、US yahoo sector），期间调类不反映。",
    "因子暴露逐日截面 MAD 温莎(3σ)+z-score 标准化；因子收益为日度截面 WLS 系数（Fama-MacBeth），"
    "可解释为「单位暴露的当日收益」，跨日可比、跨市场不可直接比较。",
]


def load_column_transform(name: str):
    if name == "identity":
        return lambda v: v
    if name == "log":
        return lambda v: math.log(v) if (v is not None and v > 0) else None
    if name == "log1p":
        return lambda v: math.log1p(v) if (v is not None and v >= 0) else None
    if name == "reciprocal":
        return lambda v: (1.0 / v) if (v is not None and v != 0) else None
    raise ValueError(f"未知列变换: {name}")


# ================================================================ 数值核心（纯标准库）

def solve_linear(mat: list[list[float]], rhs: list[float]) -> list[float]:
    """高斯消元（部分主元）解 A x = b；奇异抛 ValueError。"""
    n = len(rhs)
    a = [list(row) + [rhs[i]] for i, row in enumerate(mat)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(a[r][col]))
        if abs(a[piv][col]) < 1e-300:
            raise ValueError("矩阵奇异（主元≈0）")
        if piv != col:
            a[col], a[piv] = a[piv], a[col]
        inv = 1.0 / a[col][col]
        for r in range(col + 1, n):
            f = a[r][col] * inv
            if f == 0.0:
                continue
            for c in range(col, n + 1):
                a[r][c] -= f * a[col][c]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        s = a[r][n] - sum(a[r][c] * x[c] for c in range(r + 1, n))
        x[r] = s / a[r][r]
    return x


def invert_matrix(mat: list[list[float]]) -> list[list[float]]:
    """纯标准库求逆（逐列解方程）。"""
    n = len(mat)
    out = [[0.0] * n for _ in range(n)]
    for j in range(n):
        e = [1.0 if i == j else 0.0 for i in range(n)]
        col = solve_linear(mat, e)
        for i in range(n):
            out[i][j] = col[i]
    return out


def wls_fit(X: list[list[float]], y: list[float], w: list[float],
            ridge_rel: float = RIDGE_REL):
    """逐日截面 WLS（正规方程）：min Σ w_i (y_i − x_i'β)²。返回 (beta, resid)。

    源技能同口径：内部用 √w 加权、岭系数相对化。纯标准库，供 demo 与独立校验。
    """
    n, k = len(X), len(X[0])
    sw = [math.sqrt(max(wi, 0.0)) for wi in w]
    xtx = [[0.0] * k for _ in range(k)]
    xty = [0.0] * k
    for i in range(n):
        s = sw[i]
        if s == 0.0:
            continue
        xi = X[i]
        for a in range(k):
            xa = xi[a] * s
            if xa == 0.0:
                continue
            row = xtx[a]
            for b in range(a, k):
                row[b] += xa * xi[b] * s
            xty[a] += xa * y[i] * s
    scale = max(1e-12, sum(xtx[a][a] for a in range(k)) / k)
    for a in range(k):
        for b in range(a):
            xtx[a][b] = xtx[b][a]
        xtx[a][a] += ridge_rel * scale
    beta = solve_linear(xtx, xty)
    resid = [y[i] - sum(xi[a] * beta[a] for a in range(k)) for i, xi in enumerate(X)]
    return beta, resid


def _winsor_z(vals: list[float], k: float = 3.0) -> list[float]:
    """MAD 温莎（med±k·1.4826·MAD）+ z-score（ddof=0）；退化返回全 0。"""
    finite = [v for v in vals if v is not None and math.isfinite(v)]
    if not finite:
        return [0.0] * len(vals)
    med = sorted(finite)[len(finite) // 2]
    mad = sorted(abs(v - med) for v in finite)[len(finite) // 2]
    out = []
    for v in vals:
        if v is None or not math.isfinite(v):
            out.append(0.0)
            continue
        if mad > 0:
            lo, hi = med - k * 1.4826 * mad, med + k * 1.4826 * mad
            v = min(max(v, lo), hi)
        out.append(v)
    mean = sum(out) / len(out)
    sd = math.sqrt(sum((v - mean) ** 2 for v in out) / len(out))
    if sd <= 0:
        return [0.0] * len(out)
    return [(v - mean) / sd for v in out]


def ewma_weights(n: int, halflife: float) -> list[float]:
    lam = 0.5 ** (1.0 / halflife)
    w = [lam ** (n - 1 - i) for i in range(n)]      # 旧→新
    total = sum(w)
    return [v / total for v in w]


def factor_cov_ewma(rows: list[list[float]], halflife: float) -> list[list[float]]:
    """EWMA 因子协方差（半衰期加权、去加权均值）。Gram 构造 → 天然半正定。"""
    t = len(rows)
    k = len(rows[0])
    w = ewma_weights(t, halflife)
    mean = [sum(w[i] * rows[i][j] for i in range(t)) for j in range(k)]
    cov = [[0.0] * k for _ in range(k)]
    for i in range(t):
        d = [rows[i][j] - mean[j] for j in range(k)]
        wi = w[i]
        for a in range(k):
            da = d[a] * wi
            for b in range(a, k):
                cov[a][b] += da * d[b]
    for a in range(k):
        for b in range(a):
            cov[a][b] = cov[b][a]
    return cov


def ledoit_wolf_diag(rows: list[list[float]]) -> dict:
    """Ledoit-Wolf 收缩（对角目标，闭式，无需特征分解）。

    κ* = Σ_{i≠j} π̂_ij / (T · Σ_{i≠j} s_ij²)，π̂_ij = (1/T)Σ_t (x_it x_jt − s_ij)²；
    S 用 1/T 样本协方差；S_shrunk 对角不变、非对角 ×(1−κ)。S 与 diag(S) 均半正定
    → 任意 κ∈[0,1] 的凸组合半正定（LW 2004 对角目标特例）。
    """
    t = len(rows)
    k = len(rows[0])
    mean = [sum(r[j] for r in rows) / t for j in range(k)]
    xc = [[r[j] - mean[j] for j in range(k)] for r in rows]
    s = [[sum(xc[i][a] * xc[i][b] for i in range(t)) / t for b in range(k)] for a in range(k)]
    pi_sum = 0.0
    gamma = 0.0
    for a in range(k):
        for b in range(a + 1, k):
            s_ab = s[a][b]
            pi_ab = sum((xc[i][a] * xc[i][b] - s_ab) ** 2 for i in range(t)) / t
            pi_sum += pi_ab
            gamma += s_ab * s_ab
    kappa = 0.0 if gamma <= 0 else min(1.0, max(0.0, pi_sum / (t * gamma)))
    shrunk = [[s[a][b] * (1.0 - kappa) if a != b else s[a][a] for b in range(k)] for a in range(k)]
    return {"kappa": kappa, "sample_cov": s, "cov": shrunk, "pi_offdiag_sum": pi_sum, "gamma": gamma}


def specific_var_ewma(series: list[float | None], halflife: float, min_obs: int = 5) -> float | None:
    """特异方差：EWMA 加权（去加权均值）逐股；观测不足返回 None。"""
    vals = [v for v in series if v is not None and math.isfinite(v)]
    n = len(vals)
    if n < min_obs:
        return None
    w = ewma_weights(n, halflife)
    mean = sum(w[i] * vals[i] for i in range(n))
    return sum(w[i] * (vals[i] - mean) ** 2 for i in range(n))


def mat_vec(mat: list[list[float]], vec: list[float]) -> list[float]:
    return [sum(mat[i][j] * vec[j] for j in range(len(vec))) for i in range(len(mat))]


def decompose_risk(weights: dict[str, float], exposures: dict[str, list[float]],
                   factors: list[str], F: list[list[float]],
                   D: dict[str, float], direct_check: bool = False) -> dict:
    """组合风险分解：σ² = b'Fb + w'Dw；CCTR_k = b_k (Fb)_k（Euler，和=因子风险）。

    direct_check：另用「逐股对」双重求和 ΣΣw_iw_j X_i'F X_j 复算因子风险（不同求和路径）。
    """
    k = len(factors)
    syms = [s for s in exposures if s in weights]
    w = [weights[s] for s in syms]
    b = [sum(wi * exposures[s][j] for wi, s in zip(w, syms, strict=False)) for j in range(k)]
    fb = mat_vec(F, b)
    cctr = [b[j] * fb[j] for j in range(k)]
    factor_var = sum(cctr)
    spec_var = sum(wi * wi * D.get(s, 0.0) for wi, s in zip(w, syms, strict=False))
    total = factor_var + spec_var
    direct_resid = None
    if direct_check:
        direct = 0.0
        for wi, si in zip(w, syms, strict=False):
            xi = exposures[si]
            for wj, sj in zip(w, syms, strict=False):
                xj = exposures[sj]
                direct += wi * wj * sum(xi[a] * F[a][bb] * xj[bb] for a in range(k) for bb in range(k))
        direct_resid = abs(direct - factor_var) / total if total > 0 else abs(direct - factor_var)
    vols = {}
    for j, name in enumerate(factors):
        var_j = F[j][j]
        vols[name] = math.sqrt(max(var_j, 0.0))
    return {
        "n_symbols": len(syms),
        "total_var": total,
        "total_vol_ann": math.sqrt(max(total, 0.0)),
        "factor_var": factor_var,
        "specific_var": spec_var,
        "pct_factor": (factor_var / total) if total > 0 else None,
        "pct_specific": (spec_var / total) if total > 0 else None,
        "portfolio_exposures": dict(zip(factors, b, strict=False)),
        "cctr": dict(zip(factors, cctr, strict=False)),
        "factor_vols_ann": vols,
        "identity_residual_rel": abs(total - (factor_var + spec_var)) / total if total > 0 else 0.0,
        "direct_check_residual_rel": direct_resid,
    }


def min_variance_weights(syms: list[str], exposures: dict[str, list[float]], factors: list[str],
                         F: list[list[float]], D: list[float]) -> dict[str, float]:
    """极小方差组合 w ∝ Σ⁻¹1，Woodbury 展开（只需 K×K 求逆）：
    Σ⁻¹1 = Δ⁻¹1 − Δ⁻¹X (F⁻¹ + X'Δ⁻¹X)⁻¹ X'Δ⁻¹1。D 为逐股特异方差（正）。
    """
    k = len(factors)
    dinv = [1.0 / d if d > 0 else 0.0 for d in D]
    x_rows = [exposures[s] for s in syms]
    u = [sum(x_rows[i][a] * dinv[i] for i in range(len(syms))) for a in range(k)]
    # Woodbury：Σ⁻¹ = Δ⁻¹ − Δ⁻¹X (F⁻¹ + X'Δ⁻¹X)⁻¹ X'Δ⁻¹，故 M = F⁻¹ + X'Δ⁻¹X
    finv = invert_matrix(F)
    m = [[finv[a][b] + sum(x_rows[i][a] * dinv[i] * x_rows[i][b] for i in range(len(syms)))
          for b in range(k)] for a in range(k)]
    v = solve_linear(m, u)
    z = [dinv[i] - sum(x_rows[i][a] * v[a] for a in range(k)) for i in range(len(syms))]
    # 无约束解析解：负权重是真空头（对消因子暴露），截 0 会得出「另一个组合」，
    # 其波动可以高于等权——那就不是极小方差解了。保留原样并如实上报做空腿。
    total = sum(z)
    if abs(total) <= 1e-300:
        return {s: 1.0 / len(syms) for s in syms}
    return {s: z[i] / total for i, s in enumerate(syms)}


# ================================================================ 报告装配（两模式共用）

def factor_stats(factor_rows: list[dict], factors: list[str]) -> dict:
    """因子收益的均值/波动（年化）与 NW 无关的 t 值（mean/(std/√n)，日度）。"""
    out = {}
    for name in factors:
        vals = [r[name] for r in factor_rows if r.get(name) is not None and math.isfinite(r[name])]
        n = len(vals)
        if n < 2:
            out[name] = {"n": n, "mean_daily": None, "ann_ret": None, "ann_vol": None, "t": None}
            continue
        mean = sum(vals) / n
        sd = math.sqrt(sum((v - mean) ** 2 for v in vals) / (n - 1))
        out[name] = {
            "n": n,
            "mean_daily": mean,
            "ann_ret": mean * ANNUAL,
            "ann_vol": sd * math.sqrt(ANNUAL),
            "t": (mean / (sd / math.sqrt(n))) if sd > 0 else None,
        }
    return out


def style_corr_report(factor_rows: list[dict], style_names: list[str]) -> dict:
    """风格因子收益的相关系数（最近 min(252, n) 日，|corr|>0.5 的对列出）。"""
    rows = factor_rows[-252:]
    pairs = {}
    for i, a in enumerate(style_names):
        va = [r[a] for r in rows if r.get(a) is not None]
        if len(va) < 20:
            continue
        ma = sum(va) / len(va)
        sa = math.sqrt(sum((v - ma) ** 2 for v in va))
        for b in style_names[i + 1:]:
            vb = [r[b] for r in rows if r.get(b) is not None]
            if len(vb) != len(va) or sa <= 0:
                continue
            mb = sum(vb) / len(vb)
            sb = math.sqrt(sum((v - mb) ** 2 for v in vb))
            if sb <= 0:
                continue
            cov = sum((x - ma) * (y - mb) for x, y in zip(va, vb, strict=False))
            corr = cov / (sa * sb)
            pairs[f"{a}~{b}"] = corr
    top = dict(sorted(pairs.items(), key=lambda kv: -abs(kv[1]))[:8])
    return {"recent_days": len(rows), "top_abs_pairs": top}


def render_text(rep: dict) -> str:
    lines = []
    m = rep["model"]
    lines.append(f"Barra 式多因子风险模型（{rep.get('market', 'DEMO')}/{rep.get('mode')}）")
    lines.append("=" * 66)
    win = rep.get("window") or {}
    lines.append(
        f"窗口：{win.get('start', '—')} → {win.get('end', '—')}  ·  回归期数 {m['n_reg_periods']}"
        + (f"  ·  标的 {m['n_symbols']}" if m.get("n_symbols") else "")
        + ("  ⚠ 样本偏少" if rep.get("low_sample_warning") else "")
    )
    lines.append(f"因子数 K={m['n_factors']}（风格 {m['n_styles']} + 行业 {m['n_industries']}）"
                 f"  ·  有效截面中位 {m.get('median_names')} 只/日  ·  跳过 {m.get('days_skipped')} 日")
    lines.append(f"权重口径：{m['weight_scheme']}")
    if rep.get("style_dropped"):
        for s, why in rep["style_dropped"].items():
            lines.append(f"  ⚠ 风格 {s} 未启用：{why}")
    for f in rep.get("factors_dropped_zero", []):
        lines.append(f"  ⚠ 因子 {f} 全窗未识别（系数恒 0），已从模型剔除")

    lines.append("")
    lines.append("【因子收益（日度截面 WLS，年化）】")
    fs = rep["factor_stats"]
    styles = [s for s in fs if s in STYLE_LABELS_CN]
    if styles:
        lines.append(f"  {'风格':<10}{'年化收益':>10}{'年化波动':>10}{'t值':>8}")
        for s in styles:
            v = fs[s]
            label = STYLE_LABELS_CN[s]
            lines.append(
                f"  {label:<10}{_fmt(v['ann_ret'], 4, signed=True):>10}"
                f"{_fmt(v['ann_vol'], 4):>10}{_fmt(v['t'], 2, signed=True):>8}"
            )
    inds = [(k, v) for k, v in fs.items()
            if k not in STYLE_LABELS_CN and v.get("ann_ret") is not None]
    inds.sort(key=lambda kv: -abs(kv[1]["ann_ret"]))
    if inds:
        lines.append(f"  行业因子（|年化收益| 前 {min(6, len(inds))} / 共 {len(inds)}）：")
        for k, v in inds[:6]:
            lines.append(f"    {k:<16}{_fmt(v['ann_ret'], 4, signed=True):>10}"
                         f"{_fmt(v['ann_vol'], 4):>10}{_fmt(v['t'], 2, signed=True):>8}")

    lines.append("")
    lines.append("【因子协方差】")
    fc = rep["factor_cov"]
    if fc["method"] == "ledoit_wolf":
        lines.append(f"  方法：Ledoit-Wolf 对角目标收缩，收缩强度 κ={fc['kappa']:.4f}"
                     f"（T={fc['n_obs']} 期；κ 越大越靠近对角阵）")
    else:
        lines.append(f"  方法：EWMA（半衰期 {fc['halflife']} 日，T={fc['n_obs']} 期）")
    if fc.get("min_eigval") is not None:
        ok = "≥0 通过" if fc["min_eigval"] >= 0 else "为负，异常"
        lines.append(f"  PSD 检查：F 最小特征值 {fc['min_eigval']:.3e}（{ok}）")
    else:
        lines.append("  PSD：两种估计（LW 对角目标 / EWMA Gram）均天然半正定，无需特征截断")
    sc = rep.get("style_corr") or {}
    if sc.get("top_abs_pairs"):
        pretty = ", ".join(f"{k} {v:+.2f}" for k, v in sc["top_abs_pairs"].items())
        lines.append(f"  风格收益相关（|ρ| 前几，最近 {sc['recent_days']} 日）：{pretty}")

    lines.append("")
    lines.append("【特异风险】")
    sp = rep["specific"]
    lines.append(f"  EWMA 半衰期 {sp['halflife']} 日  ·  覆盖 {sp['n_symbols']} 只  ·  "
                 f"年化特异波动 中位 {_fmt(sp['median_ann_vol'], 4)} / "
                 f"P10 {_fmt(sp['p10_ann_vol'], 4)} / P90 {_fmt(sp['p90_ann_vol'], 4)}")

    lines.append("")
    lines.append("【组合风险分解】")
    at = rep["attribution"]
    lines.append(f"  组合：{at['portfolio']}（{at['n_symbols']} 只）")
    lines.append(f"  年化总波动 {_fmt(at['total_vol_ann'], 4)}  =  因子 {_fmt(math.sqrt(at['factor_var']), 4)}"
                 f"（{_pct(at['pct_factor'])}）+  特异 {_fmt(math.sqrt(at['specific_var']), 4)}"
                 f"（{_pct(at['pct_specific'])}）")
    tbl = at["factor_table"][:12]
    lines.append(f"  {'因子':<16}{'组合暴露':>10}{'方差贡献占比':>12}")
    for row in tbl:
        lines.append(f"  {row['factor']:<16}{row['exposure']:>10.3f}{_pct(row['pct_of_total_var']):>12}")
    mv = rep.get("min_variance")
    if mv and "error" in mv:
        lines.append(f"  极小方差组合：未计算（{mv['error']}）")
    elif mv:
        short_note = (f"，含 {mv['n_short']} 只空头（无约束解，做空腿合计 "
                      f"{_fmt((mv.get('gross_leverage', 1.0) - 1) / 2, 3)}）") if mv.get("n_short") else ""
        flag = "" if mv.get("le_equal_weight", True) else "  ⚠ 违反 ≤ 等权（内部一致性异常，勿用）"
        lines.append(f"  极小方差组合（Σ⁻¹1，Woodbury）：年化波动 {_fmt(mv['total_vol_ann'], 4)}"
                     f"  vs 上述组合 {_fmt(mv['equal_weight_vol_ann'], 4)}"
                     f"（比值 {_fmt(mv['vol_ratio'], 3)}{short_note}；验证协方差可求逆、可作优化输入）{flag}")

    va = rep.get("validation") or {}
    lines.append("")
    lines.append("【独立校验】")
    if va.get("solver_max_dev_vs_lstsq") is not None:
        lines.append(f"  因子收益：numpy 正规方程 vs lstsq 最大偏差 {va['solver_max_dev_vs_lstsq']:.3e}"
                     f"；vs 纯标准库高斯消元 {va['solver_max_dev_vs_stdlib']:.3e}"
                     f"（抽样 {va['n_sampled_days']} 日）")
    if va.get("kappa_dev_py_vs_np") is not None:
        lines.append(f"  LW 收缩强度 κ：纯 Python vs numpy 偏差 {va['kappa_dev_py_vs_np']:.3e}")
    if va.get("demo_exact_recovery_max_dev") is not None:
        lines.append(f"  demo 无噪声段因子真值还原偏差 {va['demo_exact_recovery_max_dev']:.3e}"
                     f"；单因子手算闭式偏差 {va['demo_closed_form_1factor_dev']:.3e}")
    lines.append(f"  风险恒等式残差：|factor+specific−total|/total = {va.get('identity_residual_rel', 0.0):.3e}")
    if va.get("direct_quadform_residual_rel") is not None:
        lines.append(f"  显式 Σ=XFX'+Δ 二次型 vs 分解：相对偏差 {va['direct_quadform_residual_rel']:.3e}")
    lines.append("")
    lines.append("说明：")
    for c in rep["caveats"]:
        lines.append(f"  - {c}")
    return "\n".join(lines)


def _fmt(v, dp=4, signed=False):
    if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
        return "—"
    return f"{v:+.{dp}f}" if signed else f"{v:.{dp}f}"


def _pct(v):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    return f"{v * 100:.1f}%"


def json_safe(obj):
    if isinstance(obj, dict):
        return {str(k): json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [json_safe(v) for v in obj]
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    return obj


# ================================================================ demo（确定性 3 因子小算例）

def _rng64(seed: int):
    """splitmix64 确定性伪随机流（纯整数运算，跨版本/跨平台逐位一致）。"""
    mask = 0xFFFFFFFFFFFFFFFF
    state = seed & mask

    def nxt():
        nonlocal state
        state = (state + 0x9E3779B97F4A7C15) & mask
        z = state
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & mask
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & mask
        z = z ^ (z >> 31)
        return z / 2 ** 64

    return nxt


def demo_payload(n_days: int = 48) -> dict:
    """6 只股票 / 3 因子（SIZE、MOMENTUM、行业哑变量 IND_B）确定性小算例。

    暴露固定（SIZE、MOM 为 z 化后风格值，IND_B 为 0/1 编码）；前 24 日特异收益恰为 0
    （WLS 应精确还原因子真值），后 24 日加 σ=0.004 噪声。组合用固定非均匀权重，
    保证因子暴露 b≠0、风险分解两侧（因子/特异）都有非零贡献。
    暴露矩阵刻意取正交设计（组内 SIZE/MOM 各自和相等，风格间协方差为 0），
    使 X'X 对角——单因子手算闭式 β=Σxy/Σx² 应精确等于三因子 WLS 解。
    """
    rng = _rng64(20261008)
    syms = [f"S{i}" for i in range(6)]
    size_raw = [-1.5, -0.5, 1.0, -1.5, -0.5, 1.0]
    mom_raw = [1.0, -1.0, 0.0, -1.0, 1.0, 0.0]
    ind_b = [0.0, 0.0, 0.0, 1.0, 1.0, 1.0]
    styles_z = {"SIZE": _winsor_z(size_raw), "MOMENTUM": _winsor_z(mom_raw)}
    # 行业哑变量用 ±0.5 编码（等价于 0/1 哑变量，仅平移）
    factors = ["SIZE", "MOMENTUM", "IND_B"]
    X = [[styles_z["SIZE"][i], styles_z["MOMENTUM"][i], (ind_b[i] - 0.5)] for i in range(6)]
    factor_rows = []
    y_by_day = []
    w = [1.0] * 6
    for t in range(n_days):
        f = [0.002 * (2 * rng() - 1) + 0.001,
             0.003 * (2 * rng() - 1),
             0.001 * (2 * rng() - 1)]
        factor_rows.append({"date": f"D{t + 1:02d}", **dict(zip(factors, f, strict=False))})
        y = []
        for i in range(6):
            v = sum(X[i][k] * f[k] for k in range(3))
            if t >= n_days // 2:
                v += 0.004 * (2 * rng() - 1)
            y.append(v)
        y_by_day.append(y)

    # 引擎：逐日 WLS（demo 中 y_t 与 X_t 同日对齐仅为演示引擎；quantdb 模式严格 t+1 对 t）
    est_rows, spec_rows = [], []
    for t in range(1, n_days):
        beta, resid = wls_fit(X, y_by_day[t], w)
        est_rows.append({"date": f"D{t + 1:02d}", **dict(zip(factors, beta, strict=False))})
        spec_rows.append({s: resid[i] for i, s in enumerate(syms)})
    half = n_days // 2 - 1          # est_rows 中无噪声段的行数（y 为 D02..D24）
    exact_err = max(
        abs(r[a] - t_[a])
        for r, t_ in zip(est_rows[:half], factor_rows[1:1 + half], strict=False)
        for a in factors
    )
    noisy_err = max(
        abs(r[a] - t_[a])
        for r, t_ in zip(est_rows[half:], factor_rows[1 + half:], strict=False)
        for a in factors
    )
    # 手算闭式校验（单因子 SIZE、等权）：β = Σxy/Σx²
    x1 = [row[0] for row in X]
    y1 = y_by_day[1]
    hand = sum(a * b for a, b in zip(x1, y1, strict=False)) / sum(a * a for a in x1)
    closed_dev = abs(hand - est_rows[0]["SIZE"])

    lw = ledoit_wolf_diag([[r[a] for a in factors] for r in est_rows])
    F = [[v * ANNUAL for v in row] for row in lw["cov"]]
    D = {}
    for s in syms:
        u = [row[s] for row in spec_rows]
        var = specific_var_ewma(u, halflife=60.0, min_obs=5)
        D[s] = (var if var is not None else 0.0) * ANNUAL
    exposures = {s: X[i] for i, s in enumerate(syms)}
    raw_w = [0.30, 0.25, 0.15, 0.15, 0.10, 0.05]      # 固定非均匀（偏低 SIZE）
    wsum = sum(raw_w)
    weights = {s: raw_w[i] / wsum for i, s in enumerate(syms)}
    attr = decompose_risk(weights, exposures, factors, F, D, direct_check=True)
    mv = min_variance_weights(syms, exposures, factors, F, [D[s] for s in syms])
    attr_mv = decompose_risk(mv, exposures, factors, F, D)

    report = {
        "skill": "barra-risk-model", "mode": "demo", "market": "DEMO",
        "window": {"start": "D02", "end": f"D{n_days:02d}"},
        "model": {
            "n_reg_periods": len(est_rows), "n_symbols": 6, "n_factors": 3,
            "n_styles": 2, "n_industries": 1, "median_names": 6, "days_skipped": 0,
            "weight_scheme": "equal（回归 WLS=demo 等权）",
        },
        "styles": ["SIZE", "MOMENTUM"], "style_dropped": {},
        "factor_stats": factor_stats(est_rows, factors),
        "factor_cov": {"method": "ledoit_wolf", "kappa": lw["kappa"], "n_obs": len(est_rows),
                       "annualized_cov": F, "factors": factors},
        "style_corr": style_corr_report(est_rows, ["SIZE", "MOMENTUM"]),
        "specific": {"halflife": 60.0, "n_symbols": 6,
                     "median_ann_vol": sorted(math.sqrt(max(v, 0)) for v in D.values())[3],
                     "p10_ann_vol": sorted(math.sqrt(max(v, 0)) for v in D.values())[0],
                     "p90_ann_vol": sorted(math.sqrt(max(v, 0)) for v in D.values())[-1],
                     "symbols": {s: {"ann_vol": math.sqrt(max(D[s], 0))} for s in syms}},
        "attribution": {
            "portfolio": "固定非均匀（demo：0.30/0.25/0.15/0.15/0.10/0.05）", "n_symbols": 6,
            "total_vol_ann": attr["total_vol_ann"], "factor_var": attr["factor_var"],
            "specific_var": attr["specific_var"], "pct_factor": attr["pct_factor"],
            "pct_specific": attr["pct_specific"],
            "factor_table": _attr_table(attr, factors),
        },
        "min_variance": {
            "total_vol_ann": attr_mv["total_vol_ann"], "equal_weight_vol_ann": attr["total_vol_ann"],
            "vol_ratio": attr_mv["total_vol_ann"] / attr["total_vol_ann"] if attr["total_vol_ann"] > 0 else None,
            "n_short": sum(1 for v in mv.values() if v < 0),
            "gross_leverage": sum(abs(v) for v in mv.values()),
            "le_equal_weight": bool(attr_mv["total_vol_ann"] <= attr["total_vol_ann"] * (1.0 + 1e-9)),
        },
        "validation": {
            "n_sampled_days": len(est_rows), "solver_max_dev_vs_lstsq": None,
            "solver_max_dev_vs_stdlib": None,
            "kappa_dev_py_vs_np": None,
            "identity_residual_rel": attr["identity_residual_rel"],
            "direct_quadform_residual_rel": attr["direct_check_residual_rel"],
            "demo_exact_recovery_max_dev": exact_err,
            "demo_noisy_half_max_dev": noisy_err,
            "demo_closed_form_1factor_dev": closed_dev,
        },
        "caveats": CAVEATS_BASE + [
            f"demo 为 3 因子确定性小算例：前 {n_days // 2} 日特异收益为 0，WLS 应精确还原因子真值"
            f"（无噪声段实测最大偏差 {exact_err:.2e}；含 σ=0.004 噪声段 {noisy_err:.2e}）。",
            "demo 的 y_t 与 X_t 同日对齐仅为演示引擎；quantdb 模式严格用 t+1 日收益回归 t 日暴露。",
        ],
    }
    return report


def _attr_table(attr: dict, factors: list[str]) -> list[dict]:
    rows = []
    total = attr["total_var"]
    for f in factors:
        rows.append({
            "factor": f,
            "exposure": attr["portfolio_exposures"][f],
            "var_contribution": attr["cctr"][f],
            "pct_of_total_var": (attr["cctr"][f] / total) if total > 0 else None,
            "factor_ann_vol": attr["factor_vols_ann"][f],
        })
    rows.sort(key=lambda r: -abs(r["pct_of_total_var"] or 0.0))
    return rows


# ================================================================ QuantDB 装配层

def resolve_data_root() -> Path:
    env = os.environ.get("QM_DATA_ROOT")
    cands = ([Path(env)] if env else []) + [Path(p) for p in DATA_ROOT_CANDIDATES]
    for c in cands:
        if (c / "quantdb").is_dir():
            return c
    raise SystemExit(f"找不到数据根目录（需含 quantdb/）：{cands}；或设置 QM_DATA_ROOT")


def _suffix_symbol(market: str, symbol: str) -> str:
    s = str(symbol).strip()
    if market == "US":
        return s.upper()
    if market == "HK":
        import re
        m = re.fullmatch(r"(\d{1,5})\.HK", s, re.IGNORECASE)
        return f"{int(m.group(1)):04d}.HK" if m else s.upper()
    return s.upper()


def _dt(compact: str) -> str:
    return f"{compact[:4]}-{compact[4:6]}-{compact[6:]}"


def _parts_in(base: Path, lo: str, hi: str) -> list[tuple[str, Path]]:
    return [(p.name[3:], p / "data.parquet")
            for p in sorted(base.glob("dt=*")) if lo <= p.name[3:] <= hi]


def list_dataset_columns(pd, base: Path, market: str, dataset: str) -> int:
    parts = sorted(base.glob("dt=*"))
    if not parts:
        raise SystemExit(f"数据集无分区：{base}")
    last = parts[-1] / "data.parquet"
    df = pd.read_parquet(last)
    cols = list(df.columns)
    n = len(df)
    print(f"数据集 {market}/{dataset}（{base}）：最新分区 {parts[-1].name}，{n} 行，{len(cols)} 列")
    label_like = sorted(c for c in cols if c in LABEL_COLUMNS or re.fullmatch(r"return_\d+d", c))
    print(f"\n标签/前视列（禁作暴露）：{label_like}")
    if label_like:
        print("  （return_{n}d / future_return_{n}d 都是收益标签列：CN features_daily 旧列集用前者、"
              "新列集用后者——任何情况下不得作为暴露列）")
    print("\n本市场风格列映射（按序取首个可用且非常量列；⚠=常量/全空）：")
    for style in STYLE_ORDER:
        cands = STYLE_SPEC.get(style, {}).get(market, [])
        if not cands:
            print(f"  {style:<11} （本市场无候选列）")
            continue
        picked = None
        for col, tf in cands:
            if col in cols:
                s = pd.to_numeric(df[col], errors="coerce")
                nu = int(s.nunique(dropna=True))
                mark = "" if nu > 1 else " ⚠"
                print(f"  {style:<11} 候选 {col}({tf})  nunique={nu}{mark}")
                if picked is None and nu > 1:
                    picked = col
            else:
                print(f"  {style:<11} 候选 {col}({tf})  缺列")
        print(f"  → 将使用：{picked or '（无可用的非恒定列，该风格将跳过）'}")
    return 0


def load_industry_map(pd, root: Path, market: str) -> tuple[dict[str, str], dict]:
    spec = INDUSTRY_SOURCES[market]
    path = root / spec["path"]
    if not path.exists():
        return {}, {"source": str(path), "note": "文件缺失", "n_symbols": 0}
    mapping: dict[str, str] = {}
    if spec["kind"] == "file":
        frame = pd.read_parquet(path, columns=[spec["symbol"], spec["column"]])
        sub = frame.rename(columns={spec["symbol"]: "symbol", spec["column"]: "industry"})
    else:
        frames = []
        for name in sorted(path.glob("*.parquet")):
            try:
                frames.append(pd.read_parquet(name, columns=[spec["symbol"], spec["column"]])
                              .rename(columns={spec["symbol"]: "symbol", spec["column"]: "industry"}))
            except Exception:
                continue
        if not frames:
            return {}, {"source": str(path), "note": "目录读取失败", "n_symbols": 0}
        sub = pd.concat(frames, ignore_index=True)
    for sym, ind in zip(sub["symbol"], sub["industry"], strict=False):
        if sym is None or (isinstance(sym, float) and math.isnan(sym)):
            continue
        if ind is None or (isinstance(ind, float) and math.isnan(ind)):
            continue
        val = str(ind).strip()
        if not val or val.lower() == "nan":
            continue
        mapping[_suffix_symbol(market, str(sym))] = val
    meta = {"source": str(path), "column": spec["column"], "note": spec["note"],
            "n_symbols": len(mapping), "n_industries": len(set(mapping.values()))}
    return mapping, meta


def _pick_style_column(pd, pq, factor_base: Path, parts: list[tuple[str, Path]], market: str,
                       style: str) -> tuple[list[tuple[str, str]], dict]:
    """从候选列中挑首个「各代分区均可读且非常量」的列。返回 (候选列表, 选取元信息)。"""
    cands = STYLE_SPEC.get(style, {}).get(market, [])
    if not cands:
        return [], {"reason": f"{market} 无候选列（{style} 跳过）"}
    sample = [parts[0], parts[len(parts) // 2], parts[-1]] if parts else []
    schemas = [set(pq.read_schema(p).names) for _, p in sample]
    latest = pd.read_parquet(sample[-1][1]) if sample else None
    for col, tf in cands:
        if not all(col in sc for sc in schemas):
            continue
        nu = int(pd.to_numeric(latest[col], errors="coerce").nunique(dropna=True))
        if nu <= 1:
            continue
        return [(col, tf)], {"column": col, "transform": tf,
                             "note": "各代分区均可读且非常量", "nunique_latest": nu}
    return [], {"reason": f"候选列 {[c for c, _ in cands]} 均缺列或为常量，{style} 跳过"}


def run_quantdb(args) -> dict:
    import numpy as np              # noqa: PLC0415
    import pandas as pd             # noqa: PLC0415
    import pyarrow.parquet as pq    # noqa: PLC0415

    root = resolve_data_root()
    market = args.market.upper()
    dataset = args.dataset or DEFAULT_DATASET[market]
    if (market, dataset) not in DATASET_PATHS:
        raise SystemExit(f"市场 {market} 无数据集 {dataset}；可用：{sorted(d for m, d in DATASET_PATHS if m == market)}")
    kline_dir = root / MARKET_KLINE[market]
    factor_dir = root / DATASET_PATHS[(market, dataset)]
    if not kline_dir.is_dir() or not factor_dir.is_dir():
        raise SystemExit(f"数据目录缺失：{kline_dir} 或 {factor_dir}")

    if args.list_columns:
        return {"_list_columns_done": list_dataset_columns(pd, factor_dir, market, dataset)}

    kparts_all = sorted(p.name[3:] for p in kline_dir.glob("dt=*"))
    fparts_all = sorted(p.name[3:] for p in factor_dir.glob("dt=*"))
    if not kparts_all or not fparts_all:
        raise SystemExit("行情或因子数据集无分区")
    k_last, f_last = kparts_all[-1], fparts_all[-1]
    end_c = (args.end or min(k_last, f_last)).replace("-", "")
    start_c = (args.start or
               (pd.Timestamp(_dt(end_c)) - pd.DateOffset(years=2)).strftime("%Y%m%d")).replace("-", "")
    if start_c > end_c:
        raise SystemExit(f"start > end：{start_c} > {end_c}")
    lo_c = (pd.Timestamp(_dt(start_c)) - pd.Timedelta(days=WARMUP_CAL_DAYS)).strftime("%Y%m%d")

    kparts = _parts_in(kline_dir, lo_c, end_c)
    if not kparts:
        raise SystemExit(f"行情无 {lo_c}~{end_c} 分区")
    # 尾部严格晚于 end_c，否则与 kparts 末分区重叠、末日行情被自己复制成「重复行」
    tail = [(dt, p) for dt, p in _parts_in(kline_dir, end_c, "99999999") if dt > end_c][:TAIL_PARTITIONS]
    fparts = _parts_in(factor_dir, lo_c, end_c)
    if not fparts:
        raise SystemExit(f"因子数据集无分区：{factor_dir}")

    # ---- 票池：≤end 的最后一个行情日按成交额取前 N（或 --symbols）
    last_k_dt, last_k_file = kparts[-1]
    if args.symbols:
        universe = [_suffix_symbol(market, s) for s in args.symbols.split(",") if s.strip()]
        universe_info = {"method": "explicit_symbols", "asof_date": _dt(last_k_dt), "n_symbols": len(universe)}
    else:
        u = pd.read_parquet(last_k_file, columns=["symbol", "amount"])
        u = u.dropna(subset=["amount"])
        u = u[u["amount"] > 0].sort_values("amount", ascending=False)
        n_take = args.top_n if args.top_n and args.top_n > 0 else len(u)
        universe = u["symbol"].head(n_take).astype(str).tolist()
        universe_info = {"method": f"top_{n_take}_by_amount", "asof_date": _dt(last_k_dt),
                         "n_symbols": len(universe),
                         "note": "按窗口末日成交额选池（事后票池，含轻微选择偏差；非逐日动态池）"}
    uset = set(universe)

    # ---- 行情（窗口+热身+尾部），去重、收益、次日收益 y
    frames = []
    for _dt_str, f in kparts + tail:
        cols = [c for c in ("symbol", "time", "close", "amount", "published_at") if c in pq.read_schema(f).names]
        df = pd.read_parquet(f, columns=cols)
        df["date"] = pd.to_datetime(df["time"]).dt.strftime("%Y-%m-%d")
        df = df[df["symbol"].isin(uset)]
        if len(df):
            frames.append(df)
    if not frames:
        raise SystemExit("票池在行情窗口中无数据（检查 symbol 格式：CN=000001.SZ / HK=0001.HK / US=NVDA）")
    kdf = pd.concat(frames, ignore_index=True)
    before = len(kdf)
    kdf = kdf.dropna(subset=["close"])
    n_nan_close = before - len(kdf)
    pre_dedup = len(kdf)
    if "published_at" in kdf.columns:
        kdf = kdf.sort_values(["symbol", "date", "published_at"], kind="stable") \
                 .drop_duplicates(subset=["symbol", "date"], keep="last")
        kdf = kdf.drop(columns=["published_at"])
    else:
        kdf = kdf.drop_duplicates(subset=["symbol", "date"], keep="first")
    dup_dropped = pre_dedup - len(kdf)
    kdf = kdf.sort_values(["symbol", "date"]).reset_index(drop=True)
    kdf["ret"] = kdf.groupby("symbol", sort=False)["close"].pct_change()
    # 先剔异常收益再取次日收益，避免把数据毛刺当 y
    n_bad_ret = int((kdf["ret"].abs() > MAX_ABS_RET).sum())
    kdf.loc[kdf["ret"].abs() > MAX_ABS_RET, "ret"] = None
    kdf["y"] = kdf.groupby("symbol", sort=False)["ret"].shift(-1)

    # ---- 列风格选取与读取
    style_picked: dict[str, dict] = {}
    need_cols = ["symbol"]
    for style in ("SIZE", "VALUE", "LIQUIDITY"):
        if args.styles and style not in args.styles:
            continue
        picked, meta = _pick_style_column(pd, pq, factor_dir, fparts, market, style)
        if picked:
            style_picked[style] = {**meta, "pairs": picked}
            need_cols.append(picked[0][0])
    fframes = []
    skipped_parts = 0
    for dt, f in fparts:
        cols = set(pq.read_schema(f).names)
        missing = [c for c in need_cols if c not in cols]
        use = [c for c in need_cols if c not in missing]
        if len(use) <= 1:
            skipped_parts += 1
            continue
        df = pd.read_parquet(f, columns=use)
        df["date"] = _dt(dt)
        for style, meta in style_picked.items():
            if meta["column"] not in df.columns:
                style_picked[style].setdefault("missing_parts", 0)
                style_picked[style]["missing_parts"] += 1
        fframes.append(df)
    fac = pd.concat(fframes, ignore_index=True) if fframes else pd.DataFrame(columns=need_cols + ["date"])
    fac = fac.drop_duplicates(subset=["symbol", "date"], keep="first")

    merged = kdf.merge(fac, on=["symbol", "date"], how="left") if len(fac) else kdf

    # ---- 列风格变换 + 价格类暴露（滚动）
    for style, meta in style_picked.items():
        tf = load_column_transform(meta["transform"])
        merged[style] = [tf(v) if v is not None and math.isfinite(v) else None
                         for v in pd.to_numeric(merged[meta["column"]], errors="coerce")]

    price_styles = [s for s in PRICE_WINDOWS if not args.styles or s in args.styles]
    g = merged.groupby("symbol", sort=False)
    merged["logret"] = np.log1p(merged["ret"].clip(lower=-0.999))
    merged["cs"] = g["logret"].cumsum()
    if "MOMENTUM" in price_styles:
        cs = merged.groupby("symbol", sort=False)["cs"]
        merged["MOMENTUM"] = np.exp(cs.shift(21) - cs.shift(252)) - 1.0
    if "SHORT_REV" in price_styles:
        cs = merged.groupby("symbol", sort=False)["cs"]
        merged["SHORT_REV"] = np.exp(merged["cs"] - cs.shift(21)) - 1.0
    if "VOLATILITY" in price_styles:
        merged["VOLATILITY"] = g["ret"].transform(
            lambda s: s.rolling(60, min_periods=60).std(ddof=0)) * math.sqrt(ANNUAL)
    if "BETA" in price_styles:
        # 市值/成交额加权市场收益（权重用前一日，与源技能一致）
        if args.weight_by == "equal" or "amount" not in merged.columns:
            wcol = None
        elif market == "CN" and style_picked.get("SIZE", {}).get("column") == "total_mv":
            wcol = "size_w"
            merged[wcol] = pd.to_numeric(merged["total_mv"], errors="coerce")
        else:
            wcol = "size_w"
            merged[wcol] = pd.to_numeric(merged.get("liq_amount_ma_20", merged["amount"]), errors="coerce")
        if wcol:
            # 权重取前一日（源技能口径），按 symbol 平移；正权重才参与
            mw = pd.to_numeric(merged[wcol], errors="coerce")
            mw = mw.groupby(merged["symbol"], sort=False).shift(1)
            mw = mw.where(mw > 0)
            merged["_wr"] = mw * merged["ret"].fillna(0.0)
            num = merged["_wr"].groupby(merged["date"]).sum()
            den = mw.groupby(merged["date"]).sum()
            mkt = (num / den).where(den > 0)
            mkt.name = "mkt"
            merged = merged.merge(mkt, left_on="date", right_index=True, how="left")
        else:
            merged["mkt"] = merged.groupby("date")["ret"].transform("mean")
        beta_out = pd.Series(np.nan, index=merged.index)
        for _, idx in merged.groupby("symbol", sort=False).groups.items():
            sub = merged.loc[idx]
            covr = sub["ret"].rolling(120, min_periods=90).cov(sub["mkt"])
            varr = sub["mkt"].rolling(120, min_periods=90).var(ddof=0)
            beta_out.loc[idx] = (covr / varr).to_numpy()
        merged["BETA"] = beta_out

    # ---- 权重列
    if args.weight_by == "equal":
        merged["_w"] = 1.0
        weight_note = "equal（等权 WLS）"
    else:
        if market == "CN" and style_picked.get("SIZE", {}).get("column") in ("total_mv", "float_mv", "fun_total_mv"):
            wcol = style_picked["SIZE"]["column"]
            merged["_w"] = np.sqrt(pd.to_numeric(merged[wcol], errors="coerce"))
            weight_note = f"sqrt_mcap：√{wcol}（真实市值）"
        else:
            cand = style_picked.get("SIZE", {}).get("column")
            base = "liq_amount_ma_20" if cand == "liq_amount_ma_20" else "amount"
            merged["_w"] = np.sqrt(pd.to_numeric(merged[base], errors="coerce"))
            weight_note = f"sqrt_mcap 代理：√{base}（{market} 无真实市值列，成交额规模代理）"
        merged.loc[~(merged["_w"] > 0), "_w"] = None

    # ---- 行业
    industry_on = not args.no_industry
    ind_map, ind_meta = ({}, {}) if not industry_on else load_industry_map(pd, root, market)
    if industry_on and not ind_map:
        industry_on = False
        ind_meta = {"note": "行业映射为空，退化为纯风格模型"}

    # ---- 逐日回归循环
    dates = sorted(d for d in merged["date"].unique() if start_c <= d.replace("-", "") <= end_c)
    # 注意：不能改写为 dict(merged.groupby("date"))——DataFrameGroupBy.keys 是属性（单列
    # 分组时为字符串），dict() 会把它当映射协议调用而报 'str' object is not callable
    by_date = {key: sub for key, sub in merged.groupby("date")}  # noqa: C416
    styles = [s for s in STYLE_ORDER if s in style_picked or s in price_styles]
    style_dropped = {}
    for s in STYLE_ORDER:
        if s not in styles:
            if args.styles and s not in args.styles:
                continue
            if s == "VALUE" and market in VALUE_NOTE:
                style_dropped[s] = VALUE_NOTE[market]
            else:
                style_dropped[s] = style_picked.get(s, {}).get("reason", "无可用列")
    # 行业排名（按票池覆盖），保留 ≥min-industry-names 且 ≤max-industries，其余归「其他」
    ind_counts: dict[str, int] = {}
    for s in uset:
        v = ind_map.get(s)
        if v:
            ind_counts[v] = ind_counts.get(v, 0) + 1
    kept_inds = [k for k, v in sorted(ind_counts.items(), key=lambda kv: -kv[1])
                 if v >= args.min_industry_names][:args.max_industries]
    kept_set = set(kept_inds)

    factors = styles + ([f"IND:{k}" for k in kept_inds] + [f"IND:{OTHER_INDUSTRY}"] if industry_on else [])
    factor_rows, spec_rows, x_last, last_date = [], [], None, None
    skip_reasons = {"no_data": 0, "too_few_names": 0}
    name_counts = []
    validate_days = []
    for d in dates:
        sub = by_date.get(d)
        if sub is None:
            skip_reasons["no_data"] += 1
            continue
        rows = []
        for rec in sub.to_dict("records"):
            y = rec.get("y")
            w = rec.get("_w")
            if y is None or not math.isfinite(y):
                continue
            if w is None or not (w > 0):
                continue
            styles_ok = all(rec.get(s) is not None and math.isfinite(rec[s]) for s in styles)
            if not styles_ok:
                continue
            rows.append(rec)
        if len(rows) < len(factors) + MIN_NAMES_OVER_FACTORS:
            skip_reasons["too_few_names"] += 1
            continue
        # 当日行业成分计数：当日 <min-industry-names 只的行业一律归「其他」。
        # 若照票池级保留，单票行业哑变量会在回归里过拟合该票（残差≈0 → 特异方差退化 → Σ 近奇异）。
        day_ind: dict[str, int] = {}
        if industry_on:
            for r in rows:
                v = ind_map.get(r["symbol"])
                if v:
                    day_ind[v] = day_ind.get(v, 0) + 1
        # 逐日截面 z 化风格（行业哑变量不标准化）
        X = []
        zvals = {}
        for s in styles:
            zvals[s] = _winsor_z([float(r[s]) for r in rows])
        ind_offsets = factors[len(styles):]
        for i, r in enumerate(rows):
            xrow = [zvals[s][i] for s in styles]
            if industry_on:
                v = ind_map.get(r["symbol"])
                name = (f"IND:{v}" if (v and v in kept_set and day_ind.get(v, 0) >= args.min_industry_names)
                        else f"IND:{OTHER_INDUSTRY}")
                xrow += [1.0 if f == name else 0.0 for f in ind_offsets]
            X.append(xrow)
        y = [float(r["y"]) for r in rows]
        w = [float(r["_w"]) for r in rows]
        beta, resid = _wls_np(np, X, y, w)
        factor_rows.append({"date": d, **dict(zip(factors, beta, strict=False))})
        spec_rows.append({r["symbol"]: resid[i] for i, r in enumerate(rows)})
        name_counts.append(len(rows))
        if len(validate_days) < args.validate:
            validate_days.append((d, X, y, w, beta))
        x_last = {r["symbol"]: X[i] for i, r in enumerate(rows)}
        last_date = d

    if not factor_rows:
        if skip_reasons["too_few_names"]:
            raise SystemExit(
                f"票池过小：全部 {skip_reasons['too_few_names']} 个交易日截面标的数 < 因子数+{MIN_NAMES_OVER_FACTORS}"
                "（单票或小票池无法做截面因子估计；扩大 --top-n 或检查暴露列覆盖）")
        raise SystemExit("没有任何有效回归期：检查窗口、票池与暴露数据完整性")
    n_periods = len(factor_rows)
    if n_periods < MIN_REGRESSION_DAYS:
        raise SystemExit(f"回归期数过少（{n_periods} < {MIN_REGRESSION_DAYS}），拒绝输出不可靠模型")
    low_sample = n_periods < WARN_REGRESSION_DAYS

    # ---- 恒零因子清理：某行业哑变量若从未在任一回归日出现（其成分股的行每天都因
    # 暴露缺失被丢），岭回归给它的系数恒为 0 → F 行/列全零 → 奇异。这类因子不识别，
    # 从因子集与各暴露向量中剔除（并上报），否则协方差不可求逆、PSD 检查失败。
    zero_factors = [f for f in factors if all(abs(r[f]) <= 1e-14 for r in factor_rows)]
    if zero_factors:
        drop_idx = {i for i, f in enumerate(factors) if f in set(zero_factors)}
        factors = [f for f in factors if f not in set(zero_factors)]
        for r in factor_rows:
            for f in zero_factors:
                del r[f]
        x_last = {s: [v for i, v in enumerate(vec) if i not in drop_idx]
                  for s, vec in x_last.items()}

    # ---- 协方差 F 与特异方差 Δ
    fr_matrix = [[r[f] for f in factors] for r in factor_rows]
    if args.cov_method == "ledoit_wolf":
        lw = ledoit_wolf_diag(fr_matrix)
        kappa = lw["kappa"]
        F_daily = lw["cov"]
        # κ 双算校验（numpy 与纯 Python 同式不同路径）
        try:
            t, k = len(fr_matrix), len(factors)
            xm = np.asarray(fr_matrix, dtype=float)
            xc = xm - xm.mean(axis=0, keepdims=True)
            s_np = xc.T @ xc / t
            p = xc[:, :, None] * xc[:, None, :]
            pi_np = ((p - s_np) ** 2).mean(axis=0)
            off = ~np.eye(k, dtype=bool)
            gamma_np = float((s_np[off] ** 2).sum())
            kappa_np = 0.0 if gamma_np <= 0 else min(1.0, max(0.0, float(pi_np[off].sum()) / (t * gamma_np)))
            kappa_dev = abs(kappa - kappa_np)
        except Exception:
            kappa_dev = None
    else:
        kappa, kappa_dev = None, None
        F_daily = factor_cov_ewma(fr_matrix, args.factor_halflife)
        lw = None
    F = [[v * ANNUAL for v in row] for row in F_daily]
    factor_cov_meta = {"method": args.cov_method, "n_obs": n_periods, "kappa": kappa,
                       "halflife": args.factor_halflife if args.cov_method == "ewma" else None,
                       "annualized_cov": F, "factors": factors}
    try:
        eig_min = float(np.linalg.eigvalsh(np.asarray(F, dtype=float)).min())
        factor_cov_meta["min_eigval"] = eig_min
    except Exception:
        pass

    D = {}
    d_vals = []
    for sym in sorted({s for row in spec_rows for s in row}):
        u = [row.get(sym) for row in spec_rows]
        var = specific_var_ewma(u, halflife=args.specific_halflife, min_obs=5)
        D[sym] = (var if var is not None else None)
        d_vals.append((sym, D[sym]))
    have_d = [v for _, v in d_vals if v is not None]
    med_d = sorted(have_d)[len(have_d) // 2] if have_d else 0.0
    n_filled = sum(1 for _, v in d_vals if v is None)
    for sym, v in d_vals:
        if v is None:
            D[sym] = med_d
    D_ann = {s: v * ANNUAL for s, v in D.items()}

    # ---- 组合权重（--weights CSV 或等权取最后回归日截面）
    if args.weights:
        import csv as _csv
        weights = {}
        with open(args.weights, encoding="utf-8-sig", newline="") as fh:
            for row in _csv.DictReader(fh):
                sym = row.get("symbol") or row.get("symbol ")
                try:
                    val = float(row.get("weight") or row.get("weight "))
                except (TypeError, ValueError):
                    continue
                if sym and val > 0:
                    weights[_suffix_symbol(market, sym.strip())] = val
        if not weights:
            raise SystemExit(f"权重文件无有效行：{args.weights}")
        total_w = sum(weights.values())
        weights = {k: v / total_w for k, v in weights.items()}
        portfolio_label = f"CSV:{args.weights}（{len(weights)} 只，归一化）"
    else:
        weights = {s: 1.0 / len(x_last) for s in x_last}
        portfolio_label = f"等权（最后回归日 {last_date} 截面）"
    dropped_w = [s for s in weights if s not in x_last]
    for s in dropped_w:
        weights.pop(s, None)
    if not weights:
        raise SystemExit("组合权重与模型截面无交集（检查 symbol 格式与票池）")
    tw = sum(weights.values())
    weights = {k: v / tw for k, v in weights.items()}

    # 大票池下跳过纯 Python 逐股对复算（O(N²K²)），改用显式 N×N Σ 二次型独立复核
    attr = decompose_risk(weights, x_last, factors, F, D_ann, direct_check=False)
    direct_rel = None
    try:  # 显式 N×N Σ = XFX' + Δ 二次型复核（numpy，与分解式两条计算路径）
        syms_x = list(x_last)
        Xn = np.asarray([x_last[s] for s in syms_x], dtype=float)
        Dn = np.asarray([D_ann[s] for s in syms_x], dtype=float)
        sig = Xn @ np.asarray(F, dtype=float) @ Xn.T + np.diag(Dn)
        wv = np.asarray([weights.get(s, 0.0) for s in syms_x], dtype=float)
        direct_var = float(wv @ sig @ wv)
        direct_rel = abs(direct_var - attr["total_var"]) / attr["total_var"] \
            if attr["total_var"] > 0 else None
        factor_cov_meta["asset_cov_invertible"] = bool(
            np.all(np.isfinite(np.linalg.inv(sig + np.eye(len(syms_x)) * 1e-12))))
    except Exception:
        pass

    # ---- 极小方差组合（Woodbury，验证 Σ 可作优化输入）
    min_var = None
    try:
        syms_x = list(x_last)
        mv_weights = min_variance_weights(syms_x, x_last, factors, F, [D_ann[s] for s in syms_x])
        attr_mv = decompose_risk(mv_weights, x_last, factors, F, D_ann)
        min_var = {
            "total_vol_ann": attr_mv["total_vol_ann"],
            "equal_weight_vol_ann": attr["total_vol_ann"],
            "vol_ratio": attr_mv["total_vol_ann"] / attr["total_vol_ann"] if attr["total_vol_ann"] > 0 else None,
            "n_symbols": len(syms_x),
            "n_short": sum(1 for v in mv_weights.values() if v < 0),
            "gross_leverage": sum(abs(v) for v in mv_weights.values()),
            # Cauchy–Schwarz 恒等式自检：真 Σ⁻¹1 解的波动必然 ≤ 等权波动；
            # 违反 = 求逆侧与评估侧 Σ 不一致（程序缺陷），必须显性暴露而非静默输出。
            "le_equal_weight": bool(attr_mv["total_vol_ann"] <= attr["total_vol_ann"] * (1.0 + 1e-9)),
            "note": "w ∝ Σ⁻¹1（Woodbury 展开，只对 K×K 求逆）——无约束解析解，负权重为真空头；"
                    "仅验证协方差可求逆、可作优化输入，非配置建议。",
        }
    except Exception as exc:
        min_var = {"error": str(exc)}

    # ---- 独立校验块
    validate_stats = {"n_sampled_days": len(validate_days)}
    if validate_days:
        dev_np_std = 0.0
        dev_np_lstsq = 0.0
        for _d, X, y, w, beta in validate_days:
            beta_py, _ = wls_fit(X, y, w)
            dev_np_std = max(dev_np_std, max(abs(a - b) for a, b in zip(beta, beta_py, strict=False)))
            Xn = np.asarray(X, dtype=float)
            yn = np.asarray(y, dtype=float)
            wn = np.asarray(w, dtype=float)
            sw = np.sqrt(wn)
            lstsq_beta = np.linalg.lstsq(Xn * sw[:, None], yn * sw, rcond=None)[0]
            dev_np_lstsq = max(dev_np_lstsq, max(abs(a - b) for a, b in zip(beta, lstsq_beta, strict=False)))
        validate_stats["solver_max_dev_vs_stdlib"] = dev_np_std
        validate_stats["solver_max_dev_vs_lstsq"] = dev_np_lstsq
    validate_stats["kappa_dev_py_vs_np"] = kappa_dev
    validate_stats["identity_residual_rel"] = attr["identity_residual_rel"]
    validate_stats["direct_quadform_residual_rel"] = direct_rel

    sp_vols = sorted(math.sqrt(max(v, 0.0)) for v in D_ann.values())
    report = {
        "skill": "barra-risk-model", "mode": "quantdb", "market": market, "dataset": dataset,
        "window": {"start": _dt(start_c), "end": _dt(end_c), "data_through": _dt(kparts[-1][0])},
        "universe": universe_info,
        "data_sources": {
            "kline": MARKET_KLINE[market] + "（" + KLINE_ADJUSTMENT_NOTE[market] + "）",
            "kline_last_partition": _dt(kparts[-1][0]),
            "factors": DATASET_PATHS[(market, dataset)],
            "factors_last_partition": _dt(fparts[-1][0]),
            "industry": ind_meta,
            "size_note": SIZE_PROXY_NOTE[market],
            "liquidity_note": LIQUIDITY_NOTE[market],
            "dup_dropped": dup_dropped, "n_nan_close": n_nan_close,
            "skipped_parts": skipped_parts, "n_bad_ret": n_bad_ret,
        },
        "model": {
            "n_reg_periods": n_periods, "n_symbols": len(x_last) if x_last else 0,
            "n_factors": len(factors), "n_styles": len(styles),
            "n_industries": len(factors) - len(styles),
            "median_names": sorted(name_counts)[len(name_counts) // 2],
            "days_skipped": sum(skip_reasons.values()), "skip_reasons": skip_reasons,
            "weight_scheme": weight_note,
        },
        "styles": styles, "style_dropped": style_dropped,
        "factors_dropped_zero": zero_factors,
        "style_picked": {k: {"column": v["column"], "transform": v["transform"],
                             "missing_parts": v.get("missing_parts", 0)}
                         for k, v in style_picked.items()},
        "factor_stats": factor_stats(factor_rows, factors),
        "factor_cov": factor_cov_meta,
        "style_corr": style_corr_report(factor_rows, [s for s in styles if s in STYLE_LABELS_CN]),
        "specific": {
            "halflife": args.specific_halflife, "n_symbols": len(D_ann),
            "n_filled_with_median": n_filled,
            "median_ann_vol": sp_vols[len(sp_vols) // 2] if sp_vols else None,
            "p10_ann_vol": sp_vols[max(0, int(len(sp_vols) * 0.1) - 1)] if sp_vols else None,
            "p90_ann_vol": sp_vols[min(len(sp_vols) - 1, int(len(sp_vols) * 0.9))] if sp_vols else None,
            "symbols": {s: {"ann_vol": math.sqrt(max(v, 0.0))} for s, v in D_ann.items()},
        },
        "attribution": {
            "portfolio": portfolio_label, "n_symbols": attr["n_symbols"],
            "n_weights_dropped": len(dropped_w),
            "total_vol_ann": attr["total_vol_ann"], "factor_var": attr["factor_var"],
            "specific_var": attr["specific_var"], "pct_factor": attr["pct_factor"],
            "pct_specific": attr["pct_specific"],
            "factor_table": _attr_table(attr, factors),
        },
        "min_variance": min_var,
        "validation": validate_stats,
        "low_sample_warning": low_sample,
        "caveats": CAVEATS_BASE + [
            f"回归期数 n={n_periods}" + ("，少于一年（252），样本偏少，谨慎解读。" if low_sample else "。"),
            universe_info.get("note", "显式票池。"),
            ("CN SIZE=ln(total_mv)（真实总市值）；WLS 权重口径见 model.weight_scheme。"
             if market == "CN" else
             "HK/US 无真实市值列：SIZE 与 WLS 权重均为成交额代理（" + SIZE_PROXY_NOTE[market] + "）。"),
            "特异风险为对角线假设（个股特异收益不相关）；行业因子收益含行业共同波动，"
            "组合分散度低时因子风险占比会被行业维度放大。",
            "本结果不构成投资建议。",
        ],
    }
    zp_note = ZERO_PLACEHOLDER_NOTE.get(market)
    if zp_note:
        report["caveats"].append(zp_note)
    if dup_dropped:
        if market == "HK":
            report["caveats"].append(
                f"行情去重：剔除 {dup_dropped} 行重复 (symbol,date)（2024-09-02~2026-05-08 paid_hk/akshare 双来源，按 published_at 取最新）。")
        else:
            report["caveats"].append(
                f"行情去重：剔除 {dup_dropped} 行重复 (symbol,date)（按 published_at 取最新）。")
    if n_nan_close:
        report["caveats"].append(f"剔除收盘价为空（停牌等）的 {n_nan_close} 行行情记录。")
    if zero_factors:
        report["caveats"].append(
            f"因子 {', '.join(zero_factors)} 在全窗口从未进入有效截面（成分股暴露数据缺失），"
            "系数恒 0、不识别，已从模型剔除（F 未含其行/列）。")
    if skipped_parts:
        report["caveats"].append(f"因子列在 {skipped_parts} 个分区缺失（列集漂移），这些分区被跳过。")
    if n_bad_ret:
        report["caveats"].append(f"|日收益|>{MAX_ABS_RET:.0%} 的 {n_bad_ret} 个样本按数据异常剔除。")
    return report


def _wls_np(np, X: list[list[float]], y: list[float], w: list[float]):
    """numpy 正规方程 WLS（与纯标准库 wls_fit 同语义：√w 加权 + 相对岭）。"""
    Xn = np.asarray(X, dtype=float)
    yn = np.asarray(y, dtype=float)
    wn = np.asarray(w, dtype=float)
    sw = np.sqrt(wn)
    Xw = Xn * sw[:, None]
    yw = yn * sw
    xtx = Xw.T @ Xw
    xty = Xw.T @ yw
    scale = max(1e-12, float(np.trace(xtx)) / Xn.shape[1])
    xtx = xtx + np.eye(Xn.shape[1]) * RIDGE_REL * scale
    beta = np.linalg.solve(xtx, xty)
    resid = (yn - Xn @ beta).tolist()
    return beta.tolist(), resid


# ================================================================ 入口

def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    p = argparse.ArgumentParser(
        description="Barra 式多因子风险模型（demo 小算例 / QuantDB 本地直读）",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--demo", action="store_true", help="内置确定性 3 因子小算例（纯标准库）")
    mode.add_argument("--quantdb", action="store_true", help="QuantDB 直读（容器内 pandas/numpy）")
    p.add_argument("--market", default="CN", choices=["CN", "HK", "US"], help="市场（默认 CN）")
    p.add_argument("--dataset", default=None,
                   help="因子数据集：CN=features_daily(默认)/l1_factors；HK/US=l1_factors")
    p.add_argument("--start", default=None, help="起始日 YYYY-MM-DD（默认 end 前推 2 年）")
    p.add_argument("--end", default=None, help="结束日 YYYY-MM-DD（默认 min(行情,因子) 最新分区）")
    p.add_argument("--top-n", type=int, default=300, help="按窗口末日成交额取前 N（默认 300；0=全部）")
    p.add_argument("--symbols", default=None, help="显式票池（后缀式逗号分隔；给定则忽略 --top-n）")
    p.add_argument("--styles", default=None,
                   help="风格子集（逗号分隔，默认全部可用）：SIZE,VALUE,MOMENTUM,SHORT_REV,VOLATILITY,BETA,LIQUIDITY")
    p.add_argument("--no-industry", action="store_true", help="纯风格模型（不加行业哑变量）")
    p.add_argument("--max-industries", type=int, default=30, help="保留行业数上限（默认 30，其余归「其他」）")
    p.add_argument("--min-industry-names", type=int, default=3, help="行业入模的最少票池覆盖（默认 3）")
    p.add_argument("--weight-by", default="sqrt_mcap", choices=["sqrt_mcap", "equal"],
                   help="WLS 权重口径（默认 √市值；HK/US 为成交额代理）")
    p.add_argument("--cov-method", default="ledoit_wolf", choices=["ledoit_wolf", "ewma"],
                   help="因子协方差估计（默认 LW 对角目标闭式收缩）")
    p.add_argument("--factor-halflife", type=float, default=90.0, help="EWMA 因子协方差半衰期（天，默认 90）")
    p.add_argument("--specific-halflife", type=float, default=60.0, help="EWMA 特异方差半衰期（天，默认 60）")
    p.add_argument("--weights", default=None, help="组合权重 CSV（symbol,weight）用于风险归因")
    p.add_argument("--validate", type=int, default=30, help="独立校验抽样交易日数（默认 30；0=跳过）")
    p.add_argument("--list-columns", action="store_true", help="列出数据集列与风格映射后退出（quantdb）")
    p.add_argument("--out", default=None, help="JSON 报告输出路径（父目录自动创建）")
    args = p.parse_args(argv)

    if args.styles:
        args.styles = {s.strip().upper() for s in args.styles.split(",") if s.strip()}
        bad = args.styles - set(STYLE_ORDER)
        if bad:
            raise SystemExit(f"未知风格 {sorted(bad)}；可用：{STYLE_ORDER}")

    if args.demo:
        report = demo_payload()
    else:
        report = run_quantdb(args)
        if "_list_columns_done" in report:
            return 0
    print(render_text(report))
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(json_safe(report), ensure_ascii=False, indent=2),
                            encoding="utf-8")
        print(f"\n已写入 JSON 报告：{out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
