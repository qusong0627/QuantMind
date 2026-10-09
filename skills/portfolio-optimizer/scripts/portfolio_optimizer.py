#!/usr/bin/env python3
"""组合优化工具箱 — 均值-方差 / 最小方差 / 最大夏普 / 风险平价 / 最大分散化。

来源：quantskills/skill-portfolio-optimize（GPL-3.0-only）方法论移植。目标函数与约束
口径保留（凸组合优化 + 个股上限 / 行业中性 / 暴露中性 / 换手约束 + 求解诊断），数据层由
panda_data SDK 改为本地 QuantDB 直读（CN/HK/US，2026-10 标定），并本地化新增：

  - 纯标准库求解器（投影梯度 + Dykstra 精确投影），--demo / --input 零依赖可跑；
    容器内自动升级 scipy SLSQP 后端（--solver auto，可 --cross-check 对拍）。
  - 事前风险分解：各资产风险贡献（RC）+ 因子/特质 GLS 分解（F=(XᵀΣ⁻¹X)⁻¹）。
  - 独立校验块：最小方差无约束解 vs 闭式解 w=Σ⁻¹1/(1ᵀΣ⁻¹1)、风险平价 RC 等分检查、
    demo 2 资产闭式解金样常量。

模式：
  --demo          内置确定性合成数据（纯标准库；2 资产手算金样 + 5 资产五目标/约束演示）
  --input PATH    .csv 宽收益面板（第一列日期、其余列标的日收益）或 .json 完整问题规格
  --cov PATH      symbol×symbol 协方差矩阵 CSV（年化口径，原样使用）
  --quantdb       QuantDB 直读（quantmind 容器内）：票池=窗口末日成交额前 N，
                  收益=daily_forward（CN 前复权；HK/US 不复权），HK 双来源重复行按
                  (symbol,date,published_at) 去重保留 akshare 原始价。

输出：stdout 中文报告 + --out JSON（权重 / 事前风险分解 / 换手 / 约束绑定清单 / 校验块）。
研究工具，非投资建议。
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# ------------------------------------------------------------------ 常量

OBJECTIVES = ("min_variance", "mean_variance", "max_sharpe", "risk_parity", "max_diversification")
OBJECTIVE_CN = {
    "min_variance": "最小方差",
    "mean_variance": "均值-方差",
    "max_sharpe": "最大夏普",
    "risk_parity": "风险平价(ERC)",
    "max_diversification": "最大分散化",
}
NEEDS_MU = ("mean_variance", "max_sharpe")      # 需要预期收益 μ 的目标
CONVEX_OBJ = ("min_variance", "mean_variance")  # 凸目标：单起点即可全局最优
VARIANCE_ONLY = ("min_variance", "risk_parity")  # 预算 0 时会塌缩到零组合的目标

DATA_ROOT_CANDIDATES = ["/data", "/quantmind/data", "/home/zbox/projects/quantmind/data"]
KLINE_PATHS = {
    "CN": "quantdb/1_kline_data/daily_forward",
    "HK": "quanthk/1_kline_data/daily_forward",
    "US": "quantus/1_kline_data/daily_forward",
}
FACTOR_DATASET_PATHS = {
    ("CN", "features_daily"): "quantdb/6_ml_datasets/features_daily",
    ("CN", "l1_factors"): "quantdb/6_ml_datasets/l1_factors",
    ("CN", "l2_factors"): "quantdb/6_ml_datasets/l2_factors",
    ("HK", "l1_factors"): "quanthk/6_ml_datasets/l1_factors",
    ("US", "l1_factors"): "quantus/6_ml_datasets/l1_factors",
}
DEFAULT_FACTOR_DATASET = {"CN": "features_daily", "HK": "l1_factors", "US": "l1_factors"}
KLINE_ADJUSTMENT_NOTE = {
    "CN": "daily_forward=前复权（收益含分红送转调整）",
    "HK": "daily_forward=不复权原始价（收益未含分红）；2024-09-02~2026-05-08 双来源重复行已去重",
    "US": "daily_forward=不复权原始价（收益未含分红）",
}
LABEL_COL_PATTERNS = [
    re.compile(r"^label_return$", re.I),
    re.compile(r"^return_\d+d$", re.I),
    re.compile(r"^future_return_\d+d$", re.I),
]
AUX_COLUMNS = {
    "symbol", "time", "date", "open", "high", "low", "close", "volume", "amount",
    "adj_factor", "release_id", "published_at", "Symbol_val", "close_val",
}

BIND_TOL = 1e-6          # 约束"绑定"判定容差（权重口径）
FEAS_TOL = 1e-5          # 求解结果可行性容差
PG_TOL = 1e-10           # 投影梯度收敛容差（步长 ∞ 范数）
PG_MAX_ITER = 4000       # 投影梯度默认迭代上限
DYKSTRA_MAX_ITER = 3000
DYKSTRA_TOL = 1e-13
UNBOUNDED = 1e3          # 闭式解对照时的"无界"箱约束（万元级权重不会触界）

# ------------------------------------------------------------------ 线性代数（纯标准库）

def matvec(a_mat: list[list[float]], x: list[float]) -> list[float]:
    return [sum(v * xi for v, xi in zip(row, x, strict=False)) for row in a_mat]


def transpose(a_mat: list[list[float]]) -> list[list[float]]:
    return [list(col) for col in zip(*a_mat, strict=False)]


def quad_form(w: list[float], s_mat: list[list[float]]) -> float:
    sw = matvec(s_mat, w)
    return sum(wi * vi for wi, vi in zip(w, sw, strict=False))


def gauss_jordan_inverse(a_mat: list[list[float]], tol_rel: float = 1e-12):
    """高斯-约当求逆（部分主元）；奇异返回 None。tol_rel 相对最大元素。"""
    n = len(a_mat)
    if n == 0:
        return []
    scale = max(1e-300, max(abs(v) for row in a_mat for v in row))
    tol = tol_rel * scale
    aug = [list(a_mat[i]) + [1.0 if i == j else 0.0 for j in range(n)] for i in range(n)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(aug[r][col]))
        if abs(aug[piv][col]) < tol:
            return None
        aug[col], aug[piv] = aug[piv], aug[col]
        pv = aug[col][col]
        aug[col] = [v / pv for v in aug[col]]
        for r in range(n):
            if r != col and aug[r][col] != 0.0:
                f = aug[r][col]
                aug[r] = [a - f * b for a, b in zip(aug[r], aug[col], strict=False)]
    return [row[n:] for row in aug]


def jacobi_eigh(s_mat: list[list[float]], max_sweeps: int = 60, tol: float = 1e-13):
    """对称矩阵特征分解（循环 Jacobi）。返回 (特征值, 特征向量按列)。纯标准库。"""
    n = len(s_mat)
    a_mat = [list(row) for row in s_mat]
    v_mat = [[1.0 if i == j else 0.0 for j in range(n)] for i in range(n)]
    for _ in range(max_sweeps):
        off = math.sqrt(sum(a_mat[i][j] ** 2 for i in range(n) for j in range(i + 1, n)))
        diag_max = max(1e-300, max(abs(a_mat[i][i]) for i in range(n)))
        if off <= tol * diag_max:
            break
        for p in range(n - 1):
            for q in range(p + 1, n):
                if abs(a_mat[p][q]) <= 1e-300:
                    continue
                theta = (a_mat[q][q] - a_mat[p][p]) / (2.0 * a_mat[p][q])
                t = math.copysign(1.0, theta) / (abs(theta) + math.sqrt(theta * theta + 1.0)) \
                    if theta != 0.0 else 1.0
                c = 1.0 / math.sqrt(t * t + 1.0)
                s = t * c
                for k in range(n):
                    akp, akq = a_mat[k][p], a_mat[k][q]
                    a_mat[k][p], a_mat[k][q] = c * akp - s * akq, s * akp + c * akq
                for k in range(n):
                    apk, aqk = a_mat[p][k], a_mat[q][k]
                    a_mat[p][k], a_mat[q][k] = c * apk - s * aqk, s * apk + c * aqk
                for k in range(n):
                    vkp, vkq = v_mat[k][p], v_mat[k][q]
                    v_mat[k][p], v_mat[k][q] = c * vkp - s * vkq, s * vkp + c * vkq
    return [a_mat[i][i] for i in range(n)], v_mat


def symmetric_eig_range(s_mat: list[list[float]]):
    """返回 (最小特征值, 最大特征值)；有 numpy 用 numpy（快），否则 Jacobi。"""
    try:
        import numpy as np  # noqa: PLC0415

        vals = np.linalg.eigvalsh(np.asarray(s_mat, dtype=float))
        return float(vals[0]), float(vals[-1])
    except ImportError:
        vals, _ = jacobi_eigh(s_mat)
        return min(vals), max(vals)


def ensure_pd(s_mat: list[list[float]], notes: list[str]):
    """对称化 + 正定检查 + 特征值截断修复。返回修复后的 Σ。"""
    n = len(s_mat)
    scale = max(1e-300, max(abs(v) for row in s_mat for v in row))
    asym = max(abs(s_mat[i][j] - s_mat[j][i]) for i in range(n) for j in range(n))
    if asym > 1e-10 * scale:
        notes.append(f"Σ 非对称（max|Σ−Σᵀ|={asym:.3e}），已对称化。")
    sym = [[(s_mat[i][j] + s_mat[j][i]) / 2.0 for j in range(n)] for i in range(n)]
    for i in range(n):
        if sym[i][i] < 0:
            raise SystemExit(f"Σ 对角元为负（资产 {i} 方差 {sym[i][i]:.3e}）——协方差矩阵非法，检查输入。")
    mean_var = sum(sym[i][i] for i in range(n)) / n
    if mean_var <= 0:
        raise SystemExit("Σ 全零（所有资产方差为 0）——检查输入或收益面板。")
    lam_min, lam_max = symmetric_eig_range(sym)
    eps = 1e-8 * mean_var
    if lam_min >= eps:
        return sym
    if lam_min < -0.5 * mean_var:
        raise SystemExit(
            f"Σ 严重不正定（最小特征值 {lam_min:.3e} < −0.5·均方差 {mean_var:.3e}）——"
            "多半是输入错误（手工矩阵笔误/单位混乱），拒绝求解。"
        )
    fix = _eig_clip(sym, eps)
    notes.append(
        f"Σ 奇异/近奇异：最小特征值 {lam_min:.3e}（均方差 {mean_var:.3e}，条件数≈{lam_max / max(lam_min, 1e-300):.2e}），"
        f"已按特征值截断修复（λ<{eps:.2e} 截到 {eps:.2e}）。样本窗口过短或标的数接近样本天数时常见；"
        "建议 --cov-method ledoit_wolf 或加长 --cov-window。"
    )
    return fix


def _eig_clip(s_mat: list[list[float]], eps: float) -> list[list[float]]:
    try:
        import numpy as np  # noqa: PLC0415

        arr = np.asarray(s_mat, dtype=float)
        vals, vecs = np.linalg.eigh(arr)
        vals = np.clip(vals, eps, None)
        fixed = (vecs * vals) @ vecs.T
        fixed = (fixed + fixed.T) / 2.0
        return fixed.tolist()
    except ImportError:
        vals, vecs = jacobi_eigh(s_mat)
        n = len(s_mat)
        vals = [max(v, eps) for v in vals]
        return [
            [sum(vecs[i][k] * vals[k] * vecs[j][k] for k in range(n)) for j in range(n)]
            for i in range(n)
        ]


# ------------------------------------------------------------------ 协方差估计

def pairwise_cov(rows: list[list[float | None]], n: int, ddof: int = 1):
    """成对完整（pairwise-complete）样本协方差。返回 (Σ, 每对角计数, 最小成对样本数)。"""
    counts = [0] * n
    sums = [0.0] * n
    for r in rows:
        for j in range(n):
            v = r[j]
            if v is not None:
                sums[j] += v
                counts[j] += 1
    means = [(sums[j] / counts[j]) if counts[j] > 0 else 0.0 for j in range(n)]
    cov = [[0.0] * n for _ in range(n)]
    min_pair = None
    for a in range(n):
        if counts[a] <= ddof:
            raise SystemExit(f"资产 {a} 有效收益样本仅 {counts[a]} 个——不足以估计方差。")
        for b in range(a, n):
            s = 0.0
            c = 0
            for r in rows:
                va, vb = r[a], r[b]
                if va is not None and vb is not None:
                    s += (va - means[a]) * (vb - means[b])
                    c += 1
            if c <= ddof:
                raise SystemExit(f"资产对 ({a},{b}) 有效共同样本仅 {c} 个——无法估计协方差，剔除标的或补数据。")
            v = s / (c - ddof)
            cov[a][b] = v
            cov[b][a] = v
            min_pair = c if min_pair is None else min(min_pair, c)
    return cov, counts, (min_pair or 0)


def ledoit_wolf_cov(rows: list[list[float | None]], n: int):
    """Ledoit-Wolf 收缩协方差（需 numpy+sklearn；complete-case 行）。返回 (Σ, 丢弃行数) 或 None。"""
    try:
        import numpy as np  # noqa: PLC0415
        from sklearn.covariance import LedoitWolf  # noqa: PLC0415
    except ImportError:
        return None
    kept = [r for r in rows if all(v is not None for v in r)]
    dropped = len(rows) - len(kept)
    if len(kept) < max(10, n // 2):
        return None
    lw = LedoitWolf().fit(np.asarray(kept, dtype=float))
    cov = np.asarray(lw.covariance_, dtype=float)
    ddof_fix = len(kept) / max(1, len(kept) - 1)  # sklearn 用 MLE（/T），对齐到 /（T−1）
    return (cov * ddof_fix).tolist(), dropped


def estimate_cov_matrix(rows, n, method, notes):
    """按方法估计 Σ（未年化）。返回 (Σ, meta)。method: auto/ledoit_wolf/sample。"""
    if method == "auto":
        method = "ledoit_wolf"
    if method == "ledoit_wolf":
        lw = ledoit_wolf_cov(rows, n)
        if lw is not None:
            cov, dropped = lw
            note = f"Σ 估计：Ledoit-Wolf 收缩（complete-case），丢弃含缺失行 {dropped} 行。"
            if dropped:
                notes.append(note)
            return cov, {"method": "ledoit_wolf", "n_rows_used": None, "rows_dropped": dropped}
        notes.append("Ledoit-Wolf 不可用（缺 sklearn 或有效样本过少），回退成对完整样本协方差。")
        method = "sample"
    cov, counts, min_pair = pairwise_cov(rows, n)
    return cov, {"method": "sample_pairwise", "min_pair_obs": min_pair}


# ------------------------------------------------------------------ 目标函数与梯度

def company_name(i: int) -> str:
    return f"#{i}"


def build_objective(objective: str, mu, s_mat, risk_aversion: float):
    """返回 (f, grad, maximize)。f/grad 为纯 list 运算；minimize 形式由调用方取负。"""
    n = len(s_mat)
    if objective == "min_variance":
        def f(w):
            return quad_form(w, s_mat)

        def grad(w):
            sw = matvec(s_mat, w)
            return [2.0 * v for v in sw]

        return f, grad, False
    if objective == "mean_variance":
        def f(w):
            return -sum(a * b for a, b in zip(mu, w, strict=False)) \
                + 0.5 * risk_aversion * quad_form(w, s_mat)

        def grad(w):
            sw = matvec(s_mat, w)
            return [-mu[i] + risk_aversion * sw[i] for i in range(n)]

        return f, grad, False
    if objective == "max_sharpe":
        def f(w):
            sw = matvec(s_mat, w)
            var = max(sum(a * b for a, b in zip(w, sw, strict=False)), 1e-18)
            return -(sum(a * b for a, b in zip(mu, w, strict=False)) / math.sqrt(var))

        def grad(w):
            sw = matvec(s_mat, w)
            var = max(sum(a * b for a, b in zip(w, sw, strict=False)), 1e-18)
            vol = math.sqrt(var)
            ret = sum(a * b for a, b in zip(mu, w, strict=False))
            return [-mu[i] / vol + ret * sw[i] / (vol ** 3) for i in range(n)]

        return f, grad, True
    if objective == "risk_parity":
        def f(w):
            sw = matvec(s_mat, w)
            rc = [w[i] * sw[i] for i in range(n)]
            target = sum(rc) / n
            return sum((r - target) ** 2 for r in rc)

        def grad(w):
            sw = matvec(s_mat, w)
            rc = [w[i] * sw[i] for i in range(n)]
            c_var = sum(rc)
            u = [r - c_var / n for r in rc]
            su = matvec(s_mat, u)
            return [2.0 * (sw[i] * u[i] + w[i] * su[i]) for i in range(n)]

        return f, grad, False
    if objective == "max_diversification":
        sig = [math.sqrt(max(s_mat[i][i], 0.0)) for i in range(n)]

        def f(w):
            sw = matvec(s_mat, w)
            var = max(sum(a * b for a, b in zip(w, sw, strict=False)), 1e-18)
            return -(sum(a * b for a, b in zip(sig, w, strict=False)) / math.sqrt(var))

        def grad(w):
            sw = matvec(s_mat, w)
            var = max(sum(a * b for a, b in zip(w, sw, strict=False)), 1e-18)
            vol = math.sqrt(var)
            wsig = sum(a * b for a, b in zip(sig, w, strict=False))
            return [-sig[i] / vol + wsig * sw[i] / (vol ** 3) for i in range(n)]

        return f, grad, True
    raise SystemExit(f"未知目标 '{objective}'，可选：{list(OBJECTIVES)}")


def risk_contributions(w: list[float], s_mat: list[list[float]]):
    """各资产风险贡献 RC_i = w_i(Σw)_i；返回 (绝对 RC 列表, 组合方差, 份额列表)。"""
    sw = matvec(s_mat, w)
    rc = [w[i] * sw[i] for i in range(len(w))]
    var = sum(rc)
    if var <= 0:
        zero = [0.0] * len(w)
        return rc, var, zero
    return rc, var, [r / var for r in rc]


def factor_risk_decomposition(w, s_mat, x_cols, factor_names, notes):
    """GLS 因子/特质风险分解：F=(XᵀΣ⁻¹X)⁻¹，S=Σ−XFXᵀ；e=Xᵀw。

    x_cols 为 [factor][asset] 布局（来自 exposures dict 的列）。返回 dict（含
    per_factor 贡献与恒等式核对）。"""
    n = len(w)
    k = len(x_cols)
    x_mat = [[x_cols[j][i] for j in range(k)] for i in range(n)]  # [asset][factor]
    sinv = gauss_jordan_inverse(s_mat)
    if sinv is None:
        return {"applicable": False, "reason": "Σ 不可逆（即使修复后），跳过因子分解"}
    xt_sinv_x = [
        [sum(x_mat[a][i] * sinv[a][b] * x_mat[b][j] for a in range(n) for b in range(n))
         for j in range(k)]
        for i in range(k)
    ]
    f_inv = gauss_jordan_inverse(xt_sinv_x)
    if f_inv is None:
        return {"applicable": False, "reason": "XᵀΣ⁻¹X 不可逆（暴露列线性相关），跳过因子分解"}
    fe = matvec(f_inv, [sum(x_cols[j][i] * w[i] for i in range(n)) for j in range(k)])
    e = [sum(x_cols[j][i] * w[i] for i in range(n)) for j in range(k)]
    f_var = sum(e[j] * fe[j] for j in range(k))
    total_var = quad_form(w, s_mat)
    spec_var = total_var - f_var
    if spec_var < -1e-9 * max(total_var, 1e-30):
        notes.append(f"因子分解数值异常：特质方差 {spec_var:.3e} < 0（Σ 修复或求逆误差），已截为 0。")
    spec_var = max(spec_var, 0.0)
    per_factor = {
        factor_names[j]: {
            "exposure": e[j],
            "risk_share": (e[j] * fe[j] / total_var) if total_var > 0 else None,
        }
        for j in range(k)
    }
    return {
        "applicable": True,
        "total_var": total_var,
        "factor_var": f_var,
        "specific_var": spec_var,
        "factor_share": (f_var / total_var) if total_var > 0 else None,
        "specific_share": (spec_var / total_var) if total_var > 0 else None,
        "per_factor": per_factor,
        "identity_gap": abs((f_var + spec_var) - total_var),
    }


# ------------------------------------------------------------------ 约束构造与投影

@dataclass
class Constraints:
    n: int
    lo: list
    hi: list
    budget: float
    aeq: list = field(default_factory=list)      # 额外等式行（行业/暴露，不含预算行）
    beq: list = field(default_factory=list)
    eq_labels: list = field(default_factory=list)
    wp: list | None = None                       # 上期权重（换手约束/诊断）
    tau: float | None = None


def build_constraints(n, long_only, budget, weight_cap, w_prev, turnover_limit):
    """构造箱约束 + 预算（+换手）。额外等式由调用方 append。"""
    cap = weight_cap if weight_cap is not None else 1.0
    lo_val = 0.0 if long_only else -cap
    cons = Constraints(n=n, lo=[lo_val] * n, hi=[cap] * n, budget=float(budget),
                       wp=list(w_prev) if w_prev is not None else None,
                       tau=turnover_limit)
    errs = []

    if weight_cap is not None and weight_cap <= 0:
        errs.append(f"--weight-cap 必须为正（收到 {weight_cap}）。")
    total_lo, total_hi = sum(cons.lo), sum(cons.hi)
    if not (total_lo - 1e-9 <= cons.budget <= total_hi + 1e-9):
        errs.append(
            f"权重上限不可行：n={n} 只、每只上限 {cap:g}（合计 {total_hi:g}），"
            f"预算 sum(w)={cons.budget:g} 超出可达范围 [{total_lo:g}, {total_hi:g}]。"
            "放宽 --weight-cap 或减小 --budget。"
        )
    if long_only and cons.budget <= 1e-12:
        errs.append(
            f"禁止做空（未加 --long-short）且预算 sum(w)={cons.budget:g}：非负权重求和为 0 "
            "只能全零。美元中性组合请加 --long-short（预算 0 + 个股上限）。"
        )
    if abs(cons.budget) <= 1e-12 and not long_only:
        pass  # 目标相关塌缩检查在后面按 objective 做
    if turnover_limit is not None:
        if w_prev is None:
            errs.append("--turnover-limit 需要 --prev-weights 提供上期权重。")
        else:
            if turnover_limit < 0:
                errs.append("换手上限不能为负。")
            else:
                lower = sum(
                    max(0.0, cons.wp[i] - cons.hi[i], cons.lo[i] - cons.wp[i]) for i in range(n)
                )
                if turnover_limit < lower - 1e-12:
                    errs.append(
                        f"换手约束不可行：即使每只资产只移动到边界，L1 距离下界也需 {lower:.4f} > "
                        f"上限 {turnover_limit:.4f}（上期权重越界部分太多）。放宽 --turnover-limit。"
                    )
    if errs:
        raise SystemExit("约束不可行：\n  - " + "\n  - ".join(errs))
    return cons


def add_group_equalities(cons: Constraints, labels: list[str], targets: dict, kind: str):
    """加行业等式：每个行业 Σ_{i∈s} w_i = target_s。"""
    new_rows, new_targets, new_labels = [], [], []
    for grp in sorted(set(labels)):
        mask = [1.0 if lb == grp else 0.0 for lb in labels]
        tgt = float(targets.get(grp, 0.0))
        g_lo = sum(cons.lo[i] for i in range(cons.n) if mask[i])
        g_hi = sum(cons.hi[i] for i in range(cons.n) if mask[i])
        if not (g_lo - 1e-9 <= tgt <= g_hi + 1e-9):
            raise SystemExit(
                f"行业目标不可行：行业「{grp}」（{sum(mask):.0f} 只）权重和范围 "
                f"[{g_lo:g}, {g_hi:g}]，目标 {tgt:g} 超出。放宽个股上限或修改 --sector-targets。"
            )
        new_rows.append(mask)
        new_targets.append(tgt)
        new_labels.append(f"{kind}[{grp}]={tgt:g}")
    tsum = sum(new_targets)
    if abs(tsum - cons.budget) > 1e-9 * max(1.0, abs(cons.budget)):
        raise SystemExit(
            f"行业目标和 Σtargets={tsum:g} 与预算 sum(w)={cons.budget:g} 矛盾：所有行业净敞口之和"
            "必然等于组合净敞口。行业中性（目标全 0）只在美元中性组合（--budget 0 --long-short）下可行；"
            "或给出与预算匹配的 --sector-targets（例如按基准行业权重，其和须等于预算）。"
        )
    if abs(tsum - cons.budget) <= 1e-9 * max(1.0, abs(cons.budget)) and len(new_rows) > 1:
        # 行业目标和恰等于预算：最后一行是预算等式的推论（冗余），去掉以保持等式行满秩
        # （SLSQP 的 Jacobian 秩亏会显著劣化收敛——见 references/methodology.md）
        new_rows.pop()
        new_targets.pop()
        new_labels.pop()
    cons.aeq.extend(new_rows)
    cons.beq.extend(new_targets)
    cons.eq_labels.extend(new_labels)


def add_exposure_equalities(cons: Constraints, x_cols: list[list[float]], names: list[str]):
    """加暴露中性等式行 Xᵀw = 0（暴露须中心化；空列会被剔除）。"""
    keep = []
    for j, col in enumerate(x_cols):
        if max(abs(v) for v in col) < 1e-12:
            print(f"  提示：暴露列「{names[j]}」中心化后全为 0（常数暴露列），已剔除（中性自动满足）。")
            continue
        keep.append(j)
    if not keep:
        raise SystemExit("--exposures 所有列都是常数（中心化后为 0），暴露中性无意义。检查输入列。")
    # x_cols 是 [factor][asset]；逐因子构造等式行 Xᵀw = 0 并做箱约束可达性检查
    n = cons.n
    for j in keep:
        row = [x_cols[j][i] for i in range(n)]
        lo_pos = sum(cons.lo[i] * row[i] for i in range(n) if row[i] > 0)
        hi_pos = sum(cons.hi[i] * row[i] for i in range(n) if row[i] > 0)
        lo_neg = sum(cons.hi[i] * row[i] for i in range(n) if row[i] < 0)
        hi_neg = sum(cons.lo[i] * row[i] for i in range(n) if row[i] < 0)
        lo_possible = lo_pos + lo_neg
        hi_possible = hi_pos + hi_neg
        if not (lo_possible - 1e-9 <= 0.0 <= hi_possible + 1e-9):
            raise SystemExit(
                f"暴露中性不可行：暴露「{names[j]}」在箱约束下的可达范围 "
                f"[{lo_possible:.4g}, {hi_possible:.4g}] 不含 0。检查暴露是否已中心化/方向是否反了。"
            )
        cons.aeq.append(row)
        cons.beq.append(0.0)
        cons.eq_labels.append(f"暴露中性[{names[j]}]")


def project_box(v, lo, hi):
    return [min(max(v[i], lo[i]), hi[i]) for i in range(len(v))]


def project_box_budget(v, lo, hi, budget):
    """精确投影到 {lo<=w<=hi, Σw=budget}（对乘子 θ 二分）：w_i=clip(v_i−θ)。"""
    if budget is None:
        return project_box(v, lo, hi)
    total_lo, total_hi = sum(lo), sum(hi)
    if budget <= total_lo + 1e-12:
        return list(lo)
    if budget >= total_hi - 1e-12:
        return list(hi)

    def f(theta):
        s = 0.0
        for i in range(len(v)):
            w = v[i] - theta
            if w < lo[i]:
                w = lo[i]
            elif w > hi[i]:
                w = hi[i]
            s += w
        return s - budget

    a_bound = min(v[i] - hi[i] for i in range(len(v)))
    b_bound = max(v[i] - lo[i] for i in range(len(v)))
    for _ in range(80):
        mid = 0.5 * (a_bound + b_bound)
        if f(mid) > 0:
            a_bound = mid
        else:
            b_bound = mid
    theta = 0.5 * (a_bound + b_bound)
    return [min(max(v[i] - theta, lo[i]), hi[i]) for i in range(len(v))]


def project_affine(v, a_mat, b_vec, inv_aat):
    """精确投影到 {Aw=b}：w = v − Aᵀ(AAᵀ)⁻¹(Av−b)。"""
    resid = [x - b for x, b in zip(matvec(a_mat, v), b_vec, strict=False)]
    lam = matvec(inv_aat, resid)
    at_lam = matvec(transpose(a_mat), lam)
    return [v[i] - at_lam[i] for i in range(len(v))]


def project_l1_ball(v, center, radius):
    """精确投影到 {‖w−center‖₁ ≤ radius}（soft-threshold）。"""
    d = [v[i] - center[i] for i in range(len(v))]
    total = sum(abs(x) for x in d)
    if total <= radius:
        return list(v)
    u = sorted((abs(x) for x in d), reverse=True)
    n = len(u)
    cum = 0.0
    theta = 0.0
    for rho in range(1, n + 1):
        cum += u[rho - 1]
        t = (cum - radius) / rho
        if rho == n or u[rho] <= t:
            theta = t
            break
    theta = max(theta, 0.0)
    return [center[i] + math.copysign(max(abs(d[i]) - theta, 0.0), d[i]) for i in range(len(d))]


def build_projection(cons: Constraints):
    """返回投影到可行集 {箱∩预算} ∩ {额外等式} ∩ {换手 L1 球} 的函数（Dykstra）。"""
    proj_sets = []

    box_budget = lambda v: project_box_budget(v, cons.lo, cons.hi, cons.budget)  # noqa: E731
    proj_sets.append(box_budget)

    if cons.aeq:
        aat = [[sum(a * b for a, b in zip(row_i, row_j, strict=False)) for row_j in cons.aeq]
               for row_i in cons.aeq]
        inv_aat = gauss_jordan_inverse(aat)
        if inv_aat is None:
            raise SystemExit(
                "等式约束冗余（行秩亏，AAᵀ 不可逆）：常见原因是两个暴露列完全相同、或某暴露恒等于"
                "常数（中心化后全 0）而未被剔除。去掉重复/退化的等式约束后重试。"
            )
        proj_sets.append(lambda v: project_affine(v, cons.aeq, cons.beq, inv_aat))

    if cons.tau is not None:
        proj_sets.append(lambda v: project_l1_ball(v, cons.wp, cons.tau))

    if len(proj_sets) == 1:
        return proj_sets[0]

    def dykstra(v):
        n = len(v)
        x = list(v)
        p = [[0.0] * n for _ in proj_sets]
        for _ in range(DYKSTRA_MAX_ITER):
            max_change = 0.0
            for i, proj in enumerate(proj_sets):
                y_in = [x[j] + p[i][j] for j in range(n)]
                y = proj(y_in)
                p[i] = [y_in[j] - y[j] for j in range(n)]
                chg = max(abs(y[j] - x[j]) for j in range(n))
                if chg > max_change:
                    max_change = chg
                x = y
            if max_change < DYKSTRA_TOL:
                break
        return x

    return dykstra


# ------------------------------------------------------------------ 求解器

def _feasibility_violation(w, cons: Constraints) -> float:
    """求解结果对全部约束的最大违背量。"""
    viol = 0.0
    for i in range(cons.n):
        viol = max(viol, cons.lo[i] - w[i], w[i] - cons.hi[i])
    viol = max(viol, abs(sum(w) - cons.budget))
    for row, b in zip(cons.aeq, cons.beq, strict=False):
        viol = max(viol, abs(sum(a * wi for a, wi in zip(row, w, strict=False)) - b))
    if cons.tau is not None:
        viol = max(viol, sum(abs(w[i] - cons.wp[i]) for i in range(cons.n)) - cons.tau)
    return viol


def diagnose_feasibility(cons: Constraints, max_iter: int = 1500):
    """可行集诊断（相位一）：在箱约束内用投影梯度最小化全部约束违背平方和。

    等式行（含预算）与换手约束取平方和；若最小化后总违背仍显著为正，可行集为空。
    返回 (min_viol_inf, row_gaps)：row_gaps 为每行的残余违背（∞ 意义下最大的几行）。
    """
    n = cons.n
    rows = [list(r) for r in cons.aeq] + [[1.0] * n]
    targets = list(cons.beq) + [cons.budget]
    labels = list(cons.eq_labels) + [f"预算={cons.budget:g}"]

    def row_gap(w, row, b):
        return sum(a * wi for a, wi in zip(row, w, strict=False)) - b

    def obj(w):
        s = sum(row_gap(w, r, b) ** 2 for r, b in zip(rows, targets, strict=False))
        if cons.tau is not None:
            e = max(0.0, sum(abs(w[i] - cons.wp[i]) for i in range(n)) - cons.tau)
            s += e * e
        return s

    def grad(w):
        g = [0.0] * n
        for r, b in zip(rows, targets, strict=False):
            d = row_gap(w, r, b)
            if d != 0.0:
                for i in range(n):
                    g[i] += 2.0 * d * r[i]
        if cons.tau is not None:
            e = max(0.0, sum(abs(w[i] - cons.wp[i]) for i in range(n)) - cons.tau)
            if e > 0.0:
                for i in range(n):
                    g[i] += 2.0 * e * (1.0 if w[i] >= cons.wp[i] else -1.0)
        return g

    proj = lambda v: project_box(v, cons.lo, cons.hi)  # noqa: E731
    starts = [
        proj([cons.budget / n] * n),
        list(cons.lo),
        list(cons.hi),
    ]
    best_w, best_f = None, None
    for x0 in starts:
        w_star, fv, _it, _conv = _pg_minimize(obj, grad, x0, proj, max_iter, tol=1e-12)
        if best_f is None or fv < best_f:
            best_f, best_w = fv, w_star
    gaps = [(labels[j], row_gap(best_w, rows[j], targets[j])) for j in range(len(rows))]
    if cons.tau is not None:
        l1 = sum(abs(best_w[i] - cons.wp[i]) for i in range(n))
        gaps.append((f"换手 L1≤{cons.tau:g}", max(0.0, l1 - cons.tau)))
    total_viol = math.sqrt(max(best_f, 0.0))
    inf_viol = max((abs(g) for _l, g in gaps), default=0.0)
    return inf_viol, total_viol, gaps


def _pg_minimize(f, grad, x0, proj, max_iter, tol):
    """投影梯度下降（Armijo 回溯）。返回 (w, f(w), 迭代数, 收敛标志)。"""
    n = len(x0)
    w = proj(x0)
    fw = f(w)
    alpha = 1.0
    converged = False
    it = 0
    while it < max_iter:
        it += 1
        g = grad(w)
        moved = False
        for _ in range(60):
            trial = proj([w[i] - alpha * g[i] for i in range(n)])
            diff = [trial[i] - w[i] for i in range(n)]
            step_inf = max(abs(v) for v in diff)
            if step_inf <= tol:
                converged = True
                break
            gd = sum(g[i] * diff[i] for i in range(n))
            if gd >= 0:
                break
            ft = f(trial)
            if ft <= fw + 1e-4 * gd:
                w, fw = trial, ft
                alpha = min(alpha * 1.8, 1e6)
                moved = True
                break
            alpha *= 0.5
        if converged:
            break
        if not moved:
            converged = True  # 无法再下降（到了投影后的稳定点）
            break
    return w, fw, it, converged


def _inverse_vol_start(cons: Constraints, s_mat):
    vols = [math.sqrt(max(s_mat[i][i], 0.0)) for i in range(cons.n)]
    if any(v <= 0 for v in vols):
        return None
    raw = [1.0 / v for v in vols]
    s = sum(raw)
    return [cons.budget * r / s for r in raw]


def make_starts(objective, cons, s_mat, proj, f, grad, max_iter):
    ew = [cons.budget / cons.n] * cons.n
    starts = [proj(ew)]
    inv = _inverse_vol_start(cons, s_mat)
    if inv is not None:
        starts.append(proj(inv))
    if objective not in CONVEX_OBJ:
        f_mv, g_mv, _ = build_objective("min_variance", None, s_mat, 0.0)
        w_mv, _, _, _ = _pg_minimize(f_mv, g_mv, starts[0], proj, max_iter=min(max_iter, 1500), tol=PG_TOL)
        starts.append(w_mv)
    uniq = []
    for s in starts:
        if not any(max(abs(s[i] - u[i]) for i in range(cons.n)) < 1e-12 for u in uniq):
            uniq.append(s)
    return uniq


def solve_stdlib(objective, mu, s_mat, cons, risk_aversion, max_iter, tol=PG_TOL):
    """纯标准库后端：多起点投影梯度。"""
    f, grad, _ = build_objective(objective, mu, s_mat, risk_aversion)
    proj = build_projection(cons)
    starts = make_starts(objective, cons, s_mat, proj, f, grad, max_iter)
    best = None
    total_it = 0
    all_converged = True
    for x0 in starts:
        w, fw, it, conv = _pg_minimize(f, grad, x0, proj, max_iter, tol)
        total_it += it
        if not conv:
            all_converged = False
        if best is None or fw < best[1]:
            best = (w, fw)
    w, fw = best
    return {
        "backend": "stdlib-pg",
        "weights": w,
        "objective_value": fw,
        "n_starts": len(starts),
        "iterations": total_it,
        "converged": all_converged,
        "message": f"投影梯度（{len(starts)} 起点，共 {total_it} 次迭代）"
                   + ("" if all_converged else "；部分起点未收敛到步长容差"),
    }


def solve_scipy(objective, mu, s_mat, cons, risk_aversion, max_iter):
    """scipy SLSQP 后端（源技能同款求解器）；numpy/scipy 缺失时报错。"""
    try:
        import numpy as np  # noqa: PLC0415
        from scipy.optimize import minimize  # noqa: PLC0415
    except ImportError as exc:
        raise SystemExit(f"--solver scipy 需要 numpy+scipy（{exc}）。改用 --solver stdlib。") from exc

    f, grad, _ = build_objective(objective, mu, s_mat, risk_aversion)
    proj = build_projection(cons)

    bounds = list(zip(cons.lo, cons.hi, strict=False))
    sci_cons = [{"type": "eq", "fun": (lambda w: np.sum(w) - cons.budget)}]
    for row, b in zip(cons.aeq, cons.beq, strict=False):
        row_np = np.asarray(row, dtype=float)
        sci_cons.append({"type": "eq", "fun": (lambda w, r=row_np, t=b: float(r @ w) - t)})
    if cons.tau is not None:
        wp_np = np.asarray(cons.wp, dtype=float)
        tau = cons.tau
        sci_cons.append({
            "type": "ineq",
            "fun": (lambda w, p=wp_np, t=tau: t - float(np.sum(np.abs(w - p)))),
        })

    def f_np(w_arr):
        return f([float(v) for v in w_arr])

    def g_np(w_arr):
        return np.asarray(grad([float(v) for v in w_arr]), dtype=float)

    starts = make_starts(objective, cons, s_mat, proj, f, grad, max_iter)
    best = None
    total_it = 0
    ok_any = False
    last_msg = ""
    for x0 in starts:
        res = minimize(f_np, np.asarray(x0, dtype=float), method="SLSQP",
                       jac=g_np, bounds=bounds, constraints=sci_cons,
                       options={"maxiter": max_iter, "ftol": 1e-12})
        total_it += int(getattr(res, "nit", 0) or 0)
        ok_any = ok_any or bool(res.success)
        last_msg = str(res.message)
        val = float(res.fun)
        if best is None or val < best[1]:
            best = ([float(v) for v in res.x], val)
    w, fw = best
    return {
        "backend": "scipy-slsqp",
        "weights": w,
        "objective_value": fw,
        "n_starts": len(starts),
        "iterations": total_it,
        "converged": ok_any,
        "message": f"SLSQP（{len(starts)} 起点）：{last_msg}",
    }


def solve_problem(objective, mu, s_mat, cons, risk_aversion, args, force_backend=None):
    backend = force_backend or args.solver
    if backend == "auto":
        if objective == "risk_parity":
            # RC 目标在最优附近非常平坦，SLSQP 会早停：实测 60 只 RC max−min 相对差 0.66%，
            # 同一问题投影梯度为 4.5e-8（见 references/methodology.md 标定记录）。auto 直接选 stdlib。
            backend = "stdlib"
        else:
            try:
                import scipy  # noqa: F401, PLC0415

                backend = "scipy"
            except ImportError:
                backend = "stdlib"
    if backend == "scipy":
        return solve_scipy(objective, mu, s_mat, cons, risk_aversion, args.max_iter)
    return solve_stdlib(objective, mu, s_mat, cons, risk_aversion, args.max_iter)


# ------------------------------------------------------------------ 闭式解与独立校验

def closed_form_min_variance(s_mat):
    """最小方差无约束解 w = Σ⁻¹1 / (1ᵀΣ⁻¹1)。奇异返回 None。"""
    n = len(s_mat)
    sinv = gauss_jordan_inverse(s_mat)
    if sinv is None:
        return None
    ones = [1.0] * n
    w = matvec(sinv, ones)
    s = sum(w)
    if abs(s) < 1e-14:
        return None
    return [v / s for v in w]


def verify_minvar_closed_form(s_mat, backend, max_iter, notes):
    """独立校验：最小方差无约束解 vs 闭式解（同一 Σ）。返回校验 dict。"""
    n = len(s_mat)
    w_cf = closed_form_min_variance(s_mat)
    if w_cf is None:
        return {"applicable": False, "reason": "Σ 奇异，闭式解不可得"}
    gap_impl = None
    try:
        import numpy as np  # noqa: PLC0415

        sinv_np = np.linalg.inv(np.asarray(s_mat, dtype=float))
        w_np = sinv_np @ np.ones(n)
        w_np = w_np / w_np.sum()
        gap_impl = max(abs(w_np[i] - w_cf[i]) for i in range(n))
    except ImportError:
        pass
    if max(abs(v) for v in w_cf) > UNBOUNDED * 0.9:
        return {"applicable": False, "reason": "闭式解触界（1e3），校验跳过"}
    cons = Constraints(n=n, lo=[-UNBOUNDED] * n, hi=[UNBOUNDED] * n, budget=1.0)
    f, grad, _ = build_objective("min_variance", None, s_mat, 0.0)
    proj = build_projection(cons)
    if backend.startswith("scipy"):
        import numpy as np  # noqa: PLC0415
        from scipy.optimize import minimize  # noqa: PLC0415

        res = minimize(lambda w: float(np.asarray(w).T @ np.asarray(s_mat) @ np.asarray(w)),
                       np.full(n, 1.0 / n), method="SLSQP",
                       jac=lambda w: 2.0 * (np.asarray(s_mat) @ np.asarray(w)),
                       bounds=[(-UNBOUNDED, UNBOUNDED)] * n,
                       constraints=[{"type": "eq", "fun": lambda w: float(np.sum(w)) - 1.0}],
                       options={"maxiter": 800, "ftol": 1e-14})
        w_sol = [float(v) for v in res.x]
    else:
        w_sol, _, _, _ = _pg_minimize(f, grad, [1.0 / n] * n, proj, max_iter=min(max_iter, 3000),
                                      tol=1e-12)
    gap = max(abs(w_sol[i] - w_cf[i]) for i in range(n))
    gap_l2 = math.sqrt(sum((w_sol[i] - w_cf[i]) ** 2 for i in range(n)) / n)
    return {
        "applicable": True,
        "max_abs_gap": gap,
        "rms_gap": gap_l2,
        "closed_form_two_impl_gap": gap_impl,
        "backend": backend,
        "note": "对照问题=仅预算约束（允许做空、无上限）的最小方差；偏差为权重 ∞ 范数。"
                + ("（闭式解另经 numpy.linalg.inv 独立复算）" if gap_impl is not None else ""),
    }


# ------------------------------------------------------------------ 诊断 / 绑定 / 报告

def compute_diagnostics(problem_, w, s_mat, diag_extra):
    n = len(w)
    w = [0.0 if abs(v) < 1e-8 else v for v in w]
    var = quad_form(w, s_mat)
    vol = math.sqrt(max(var, 0.0))
    rc, _, rc_share = risk_contributions(w, s_mat)
    abs_w = [abs(v) for v in w]
    gross = sum(abs_w)
    hhi = sum((v / gross) ** 2 for v in abs_w) if gross > 0 else None
    diag = {
        "n_assets": n,
        "n_holdings": sum(1 for v in w if abs(v) > 1e-6),
        "sum_weights": sum(w),
        "gross_exposure": gross,
        "max_weight": max(w),
        "min_weight": min(w),
        "hhi_abs": hhi,
        "effective_n": (1.0 / hhi) if hhi else None,
        "portfolio_vol_annual": vol,
        "risk_contributions": {problem_["symbols"][i]: rc_share[i] for i in range(n)},
        "risk_contribution_abs": {problem_["symbols"][i]: rc[i] for i in range(n)},
    }
    mu = problem_.get("mu")
    if mu is not None:
        ret = sum(a * b for a, b in zip(mu, w, strict=False))
        diag["expected_return_annual"] = ret
        diag["sharpe_annual"] = (ret / vol) if vol > 0 else None
    cons = problem_["_cons"]
    if cons.wp is not None:
        l1 = sum(abs(w[i] - cons.wp[i]) for i in range(n))
        diag["turnover_l1"] = l1
        diag["turnover_one_way"] = l1 / 2.0
        if cons.tau is not None:
            diag["turnover_limit"] = cons.tau
    sectors = problem_.get("sectors")
    if sectors:
        agg = {}
        for i, sym in enumerate(problem_["symbols"]):
            agg[sectors[sym]] = agg.get(sectors[sym], 0.0) + w[i]
        diag["sector_exposure"] = dict(sorted(agg.items()))
    exposures = problem_.get("exposures")
    if exposures:
        diag["factor_exposure"] = {
            name: sum(col[i] * w[i] for i in range(n)) for name, col in exposures.items()
        }
        diag.update(diag_extra.get("factor_risk", {}) or {})
    return diag


def binding_constraints(problem_, w, cons: Constraints):
    """约束绑定清单（不等式报紧绑，等式报实现值/松弛）。"""
    items = []
    n = cons.n
    syms = problem_["symbols"]
    at_hi = [i for i in range(n) if cons.hi[i] < UNBOUNDED / 2 and w[i] >= cons.hi[i] - BIND_TOL]
    at_lo = [i for i in range(n) if cons.lo[i] > -UNBOUNDED / 2 and w[i] <= cons.lo[i] + BIND_TOL
             and cons.lo[i] < -1e-9]
    if at_hi:
        top = sorted(at_hi, key=lambda i: -w[i])[:3]
        items.append(
            f"单票上限 {cons.hi[0]:.4f} 绑定：{len(at_hi)} 只"
            f"（如 {', '.join(f'{syms[i]}={w[i]:.4f}' for i in top)}）"
        )
    if at_lo:
        top = sorted(at_lo, key=lambda i: w[i])[:3]
        items.append(
            f"单票下限 {cons.lo[0]:.4f} 绑定：{len(at_lo)} 只"
            f"（如 {', '.join(f'{syms[i]}={w[i]:.4f}' for i in top)}）"
        )
    if not at_hi and not at_lo:
        items.append("单票上下限：无绑定")
    items.append(f"预算 Σw={sum(w):.6f}（目标 {cons.budget:g}，含等式的残差 {abs(sum(w) - cons.budget):.2e}）")
    for row, b, label in zip(cons.aeq, cons.beq, cons.eq_labels, strict=False):
        realized = sum(a * wi for a, wi in zip(row, w, strict=False))
        items.append(f"{label}：实现值 {realized:+.2e}（残差 {abs(realized - b):.1e}）")
    if cons.tau is not None:
        l1 = sum(abs(w[i] - cons.wp[i]) for i in range(n))
        slack = cons.tau - l1
        if slack <= BIND_TOL:
            items.append(f"换手约束紧绑：L1={l1:.4f} ≈ 上限 {cons.tau:.4f}（贴合，解由换手约束决定）")
        else:
            items.append(f"换手约束未绑定：L1={l1:.4f} < 上限 {cons.tau:.4f}（松弛 {slack:.4f}）")
    return items


def _fmt(v, dp=4, signed=False):
    if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
        return "—"
    return f"{v:+.{dp}f}" if signed else f"{v:.{dp}f}"


def _fmt_sci(v):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    return f"{v:.2e}"


def render_text(rep: dict, compact: bool = False) -> str:
    d = rep["diagnostics"]
    lines = []
    title = f"组合优化 · {OBJECTIVE_CN[rep['objective']]}（{rep['objective']}）"
    lines.append(title)
    lines.append("=" * 64)
    lines.append(f"求解后端：{rep['solver']['backend']}（{rep['solver']['n_starts']} 起点，"
                 f"{'收敛' if rep['solver']['converged'] else '⚠ 未完全收敛'}）")
    lines.append(f"数据：{rep['problem']['cov_source']}"
                 + (f"；μ：{rep['problem']['mu_source']}" if rep["problem"].get("mu_source") else ""))
    c = rep["problem"]["constraints"]
    constraint_bits = [f"预算 {c['budget']:g}", f"箱 [{c['lo']:g}, {c['hi']:g}]"]
    if c.get("sector_neutral"):
        constraint_bits.append(f"行业中性（{c['n_sectors']} 行业）")
    if c.get("exposure_neutral"):
        constraint_bits.append(f"暴露中性（{', '.join(c['exposure_names'])}）")
    if c.get("turnover_limit") is not None:
        constraint_bits.append(f"换手上限 {c['turnover_limit']:.3f}")
    lines.append("约束：" + "；".join(constraint_bits))
    lines.append("-" * 64)
    lines.append(f"持仓 {d['n_holdings']}/{d['n_assets']}；Σw={d['sum_weights']:.6f}；"
                 f"gross={d['gross_exposure']:.6f}；最大权重 {d['max_weight']:.4f}")
    lines.append(f"年化波动（事前）={d['portfolio_vol_annual'] * 100:.2f}%"
                 + (f"；预期收益（μ 口径）={d['expected_return_annual'] * 100:.2f}%"
                    f"；夏普={_fmt(d.get('sharpe_annual'), 3)}" if "expected_return_annual" in d else ""))
    if d.get("effective_n"):
        lines.append(f"集中度：HHI(|w|)={d['hhi_abs']:.4f}；有效持仓数≈{d['effective_n']:.1f}")
    if compact:
        top = sorted(rep["weights"].items(), key=lambda kv: -abs(kv[1]))[:5]
        lines.append("主要权重：" + "，".join(f"{k}={v:+.4f}" for k, v in top))
        v = rep.get("verification", {})
        cf = v.get("minvar_closed_form", {})
        if cf.get("applicable"):
            lines.append(f"校验：最小方差闭式解偏差 max|Δw|={_fmt_sci(cf['max_abs_gap'])}"
                         f"（rms {_fmt_sci(cf['rms_gap'])}）")
        rp = v.get("risk_parity_rc")
        if rp:
            lines.append(f"校验：风险平价 RC 等分 max−min 相对差 {rp['max_min_gap_rel'] * 100:.6f}%"
                         + (f"（{rp['note']}）" if rp.get("note") else ""))
        lines.append("=" * 64)
        return "\n".join(lines)

    top = sorted(rep["weights"].items(), key=lambda kv: -abs(kv[1]))[:10]
    lines.append("-" * 64)
    lines.append("权重（前 10）：")
    for k, v in top:
        lines.append(f"  {k:<14} {v:+.4f}")
    lines.append("-" * 64)
    lines.append("事前风险分解")
    rc_sorted = sorted(d["risk_contributions"].items(), key=lambda kv: -abs(kv[1]))
    lines.append("  资产 RC 份额（前 8，Σ=1): " + "，".join(
        f"{k} {v * 100:.1f}%" for k, v in rc_sorted[:8]))
    if d.get("rc_max_min_gap_rel") is not None:
        label = "RC 等分检查" if rep["objective"] == "risk_parity" else "RC 分布差异（相对 1/n）"
        lines.append(f"  {label}：max−min 相对差 {d['rc_max_min_gap_rel'] * 100:.4f}%"
                     + ("（约束绑定下不等分属预期）" if d.get("rc_gap_note") else ""))
    if "sector_exposure" in d:
        lines.append("  行业暴露：" + "，".join(f"{k}={v:+.4f}" for k, v in d["sector_exposure"].items()))
    if "factor_exposure" in d:
        tag = "（中性应为 0）" if rep["problem"]["constraints"].get("exposure_neutral") else ""
        lines.append(f"  因子暴露{tag}：" + "，".join(
            f"{k}={v:+.3f}" for k, v in d["factor_exposure"].items()))
    if d.get("factor_share") is not None:
        lines.append(f"  风险结构：因子 {d['factor_share'] * 100:.1f}% / 特质 {d['specific_share'] * 100:.1f}%"
                     f"（恒等式残差 {_fmt_sci(d.get('identity_gap'))}）")
        lines.append("  因子贡献：" + "，".join(
            f"{k} {v['risk_share'] * 100:.1f}%（暴露 {v['exposure']:+.3f}）"
            for k, v in d.get("per_factor", {}).items()))
    if "turnover_l1" in d:
        extra = (f"，上限 {d['turnover_limit']:.4f}" if "turnover_limit" in d else "（无上限，仅诊断）")
        lines.append(f"换手：L1={d['turnover_l1']:.4f}（单边 {d['turnover_one_way']:.4f}）{extra}")
    lines.append("-" * 64)
    lines.append("约束绑定清单：")
    for item in d["binding_constraints"]:
        lines.append(f"  - {item}")
    lines.append("-" * 64)
    lines.append("独立校验：")
    v = rep.get("verification", {})
    cf = v.get("minvar_closed_form", {})
    if cf.get("applicable"):
        lines.append(f"  · 最小方差无约束解 vs 闭式解 w=Σ⁻¹1/(1ᵀΣ⁻¹1)：max|Δw|={_fmt_sci(cf['max_abs_gap'])}，"
                     f"rms={_fmt_sci(cf['rms_gap'])}（{cf['backend']} 后端）")
        if cf.get("closed_form_two_impl_gap") is not None:
            lines.append(f"    闭式解两实现互差（高斯-约当 vs numpy）：{_fmt_sci(cf['closed_form_two_impl_gap'])}")
    else:
        lines.append(f"  · 闭式解对照跳过：{cf.get('reason', '不适用')}")
    rp = v.get("risk_parity_rc")
    if rp:
        lines.append(f"  · 风险平价 RC 等分：max−min 相对差 {rp['max_min_gap_rel'] * 100:.4f}%"
                     + (f"（注：{rp['note']}）" if rp.get("note") else ""))
    xc = v.get("cross_check")
    if xc:
        lines.append(f"  · 后端对拍（{xc['backend_a']} vs {xc['backend_b']}）：max|Δw|={_fmt_sci(xc['max_abs_gap'])}，"
                     f"目标值差 {_fmt_sci(xc['objective_gap'])}")
    lines.append("")
    lines.append("说明（caveats）：")
    for caveat in rep["caveats"]:
        lines.append(f"  - {caveat}")
    lines.append("=" * 64)
    return "\n".join(lines)


def json_safe(obj):
    if isinstance(obj, dict):
        return {k: json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [json_safe(v) for v in obj]
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    return obj


# ------------------------------------------------------------------ 统一求解入口

def solve_and_report(problem_: dict, args, mode: str, compact: bool = False):
    """problem_ 键：symbols/mu/cov/sectors/exposures/w_prev/objective/risk_aversion/
    budget/long_only/weight_cap/turnover_limit/cov_source/mu_source/notes。"""
    notes = list(problem_.get("notes") or [])
    symbols = problem_["symbols"]
    n = len(symbols)
    if n < 1:
        raise SystemExit("资产数为 0。")
    objective = problem_["objective"]
    mu = problem_.get("mu")
    if objective in NEEDS_MU and mu is None:
        raise SystemExit(
            f"目标 {objective} 需要预期收益 μ：用 --mu <csv>（symbol,mu，年化）或（quantdb 下）"
            "--mu-file；显式用历史均值当 μ 需加 --mu-from-mean（噪声大，会放大 MVO 极端解）。"
            "不需要 μ 的目标：min_variance / risk_parity / max_diversification。"
        )
    s_mat = ensure_pd(problem_["cov"], notes)

    cons = build_constraints(
        n, long_only=problem_["long_only"], budget=problem_["budget"],
        weight_cap=problem_["weight_cap"], w_prev=problem_.get("w_prev"),
        turnover_limit=problem_.get("turnover_limit"),
    )
    if abs(cons.budget) <= 1e-12 and objective in VARIANCE_ONLY:
        raise SystemExit(
            f"目标 {objective} 在预算 sum(w)=0 下会塌缩：零组合（方差 0）可行且最优。"
            "美元中性组合请用带收益项的目标（mean_variance / max_sharpe），"
            "或改 --budget 1 走全投资多头。"
        )
    sector_neutral = False
    sectors = problem_.get("sectors")
    sector_targets = problem_.get("sector_targets")  # None=仅展示；dict=施加行业等式约束
    if sector_targets is not None:
        if not sectors:
            raise SystemExit("行业约束已启用但缺少行业映射（--sectors CSV / quantdb-auto / JSON sectors）。")
        missing = [s for s in symbols if s not in sectors]
        if missing:
            raise SystemExit(f"行业映射缺 {len(missing)} 只（如 {missing[:3]}）——补映射或剔除标的。")
        labels = [sectors[s] for s in symbols]
        add_group_equalities(cons, labels, sector_targets, "行业")
        sector_neutral = all(abs(t) < 1e-12 for t in sector_targets.values())
    exposures = problem_.get("exposures")
    exposure_neutral = bool(exposures) and bool(problem_.get("exposure_neutral"))
    if exposure_neutral:
        names = list(exposures.keys())
        x_cols = [list(exposures[name]) for name in names]
        add_exposure_equalities(cons, x_cols, names)
    problem_["_cons"] = cons

    # 求解 + 后端对拍
    res = solve_problem(objective, mu, s_mat, cons, problem_["risk_aversion"], args)
    w = res["weights"]
    viol = _feasibility_violation(w, cons)
    if viol > FEAS_TOL:
        inf_viol, _total_viol, gaps = diagnose_feasibility(cons)
        if inf_viol > FEAS_TOL:
            worst = sorted(gaps, key=lambda kv: -abs(kv[1]))[:4]
            detail = "；".join(f"{lab} 残余 {g:+.4f}" for lab, g in worst)
            raise SystemExit(
                f"求解失败：约束联合不可行——在箱约束内以最小二乘最小化全部约束违背后，"
                f"残余最大仍为 {inf_viol:.4e}（> {FEAS_TOL:g}）。最突出的矛盾行：{detail}。"
                "常见组合：暴露中性/行业目标 + 个股上限 + 换手上限同时生效时空交集。"
                "放宽其中一条（个股上限或目标）后重试。"
            )
        raise SystemExit(
            f"求解失败：结果对约束的最大违背量 {viol:.2e} > {FEAS_TOL:g}，但可行集非空"
            f"（相位一最小残余 {inf_viol:.2e}）——属求解器数值问题。"
            "换 --solver 后端、加大 --max-iter 或放宽容差后重试。"
        )
    if not res["converged"]:
        notes.append(f"求解器未报告完整收敛（{res['message']}），但结果满足全部约束（违背 {viol:.1e}）——"
                     "可加大 --max-iter 或换后端复核。")
    cross = None
    if getattr(args, "cross_check", False):
        other = "stdlib" if res["backend"].startswith("scipy") else "scipy"
        try:
            res2 = solve_problem(objective, mu, s_mat, cons, problem_["risk_aversion"], args,
                                 force_backend=other)
            cross = {
                "backend_a": res["backend"],
                "backend_b": res2["backend"],
                "max_abs_gap": max(abs(w[i] - res2["weights"][i]) for i in range(n)),
                "objective_gap": abs(res["objective_value"] - res2["objective_value"]),
            }
        except SystemExit as exc:
            notes.append(f"后端对拍跳过：{exc}")

    diag_extra = {}
    if exposures:
        fdec = factor_risk_decomposition(w, s_mat, [list(exposures[name]) for name in exposures],
                                         list(exposures.keys()), notes)
        if fdec.get("applicable"):
            diag_extra["factor_risk"] = {
                "factor_var": fdec["factor_var"],
                "specific_var": fdec["specific_var"],
                "factor_share": fdec["factor_share"],
                "specific_share": fdec["specific_share"],
                "per_factor": fdec["per_factor"],
                "identity_gap": fdec["identity_gap"],
            }

    diag = compute_diagnostics(problem_, w, s_mat, diag_extra)
    rc_share = diag["risk_contributions"]
    nz = [v for v in rc_share.values() if abs(v) > 1e-12]
    if len(nz) >= 2:
        diag["rc_max_min_gap_rel"] = (max(nz) - min(nz)) / (1.0 / n)
    diag["binding_constraints"] = binding_constraints(problem_, w, cons)

    verification = {}
    verification["minvar_closed_form"] = verify_minvar_closed_form(s_mat, res["backend"],
                                                                  args.max_iter, notes)
    if objective == "risk_parity":
        rp = {"max_min_gap_rel": diag.get("rc_max_min_gap_rel")}
        if any(abs(w[i] - cons.hi[i]) < BIND_TOL for i in range(n) if cons.hi[i] < UNBOUNDED / 2):
            rp["note"] = "有单票上限绑定，RC 不等分属预期；无约束 RC 等分检查见 demo/无上限运行"
            diag["rc_gap_note"] = True
        verification["risk_parity_rc"] = rp
    if cross:
        verification["cross_check"] = cross

    problem_render = {
        "cov_source": problem_.get("cov_source", "未注明"),
        "mu_source": problem_.get("mu_source"),
        "constraints": {
            "budget": cons.budget,
            "lo": cons.lo[0],
            "hi": cons.hi[0],
            "sector_neutral": sector_neutral,
            "n_sectors": len(set(sectors.values())) if sectors else 0,
            "exposure_neutral": exposure_neutral,
            "exposure_names": list(exposures.keys()) if exposures else [],
            "turnover_limit": cons.tau,
            "n_equalities": len(cons.aeq) + 1,
        },
    }

    caveats = list(notes)
    caveats += [
        "Σ 由历史收益估计：事前风险≠未来风险，窗口越短估计噪声越大（条件数已报）。",
        "本结果是最优化问题的解，不是收益预测；换手为 L1 口径，未含交易成本与冲击。",
        "MVO/MaxSharpe 对 μ 的估计误差极敏感（教科书级陷阱）：μ 噪声会推出极端权重，"
        "务必配合 --weight-cap 使用，或改用 min_variance / risk_parity。",
        "多空（--long-short）已放松下界，但未建模融券成本、券源与卖空约束的可实现性。",
        "研究工具输出，不构成投资建议。",
    ]
    if problem_.get("long_only") is False:
        caveats.insert(3, "做空腿依赖融券可得性；A 股融券成本/券源受限，实盘需另加约束。")

    report = {
        "skill": "portfolio-optimizer",
        "mode": mode,
        "objective": objective,
        "objective_value": res["objective_value"],
        "solver": {k: res[k] for k in ("backend", "n_starts", "iterations", "converged", "message")},
        "problem": problem_render,
        "weights": {symbols[i]: round(w[i], 10) for i in range(n)},
        "diagnostics": diag,
        "verification": verification,
        "caveats": caveats,
    }
    if problem_.get("extra_report"):
        report.update(problem_["extra_report"])
    report["report_text"] = render_text(report, compact=compact)
    return report


def default_problem(symbols, cov, **kw):
    p = {
        "symbols": list(symbols),
        "mu": None,
        "cov": cov,
        "sectors": None,
        "sector_targets": None,
        "exposures": None,
        "exposure_neutral": False,
        "w_prev": None,
        "objective": "mean_variance",
        "risk_aversion": 5.0,
        "budget": 1.0,
        "long_only": True,
        "weight_cap": None,
        "turnover_limit": None,
        "cov_source": "内置",
        "mu_source": None,
        "notes": [],
    }
    p.update(kw)
    return p


# ------------------------------------------------------------------ demo（纯标准库）

class Lcg:
    """确定性线性同余发生器（仅整数运算，跨平台逐位一致）。"""

    def __init__(self, seed: int):
        self.state = seed & 0x7FFFFFFF

    def uniform(self) -> float:
        self.state = (1103515245 * self.state + 12345) & 0x7FFFFFFF
        return self.state / 0x7FFFFFFF

    def normal(self) -> float:
        u1 = max(self.uniform(), 1e-12)
        u2 = self.uniform()
        return math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2)


GOLD_TOL = 1e-6  # 金样校验容差（求解器数值精度内）


def run_demo(args) -> int:
    """内置确定性 demo：2 资产闭式解金样 + 5 资产五目标/约束/校验演示。"""
    print("组合优化工具箱 · 内置演示（确定性合成数据，纯标准库可跑）")
    print("=" * 64)
    reports_out = {}

    # ---- A. 2 资产手算金样 -------------------------------------------------
    print("\n【A. 2 资产闭式解金样】（σ1=20%、σ2=30%、ρ=0.1，μ=(8%,12%) 年化）")
    s2 = [[0.04, 0.006], [0.006, 0.09]]
    mu2 = [0.08, 0.12]
    sym2 = ["A", "B"]
    # 闭式解（手算可验证）：
    #   最小方差：w1=(σ2²−ρσ1σ2)/(σ1²+σ2²−2ρσ1σ2) = 0.084/0.118 = 42/59
    w_mv_gold = [42.0 / 59.0, 17.0 / 59.0]
    #   风险平价（2 资产 ERC 与相关性无关）：w1=σ2/(σ1+σ2)=0.6
    w_rp_gold = [0.6, 0.4]
    #   最大夏普（切点组合）：w ∝ Σ⁻¹μ ∝ [0.00648, 0.00432] → [0.6, 0.4]
    w_ms_gold = [0.6, 0.4]
    gold_checks = []
    for obj, gold, label in (
        ("min_variance", w_mv_gold, "min_variance（手算 42/59）"),
        ("risk_parity", w_rp_gold, "risk_parity（逆波动率 0.6/0.4）"),
        ("max_sharpe", w_ms_gold, "max_sharpe（切点 0.6/0.4）"),
    ):
        p = default_problem(
            sym2, s2, mu=mu2 if obj == "max_sharpe" else None, objective=obj,
            long_only=False, weight_cap=None, budget=1.0,
            cov_source="demo Σ=[[0.04,0.006],[0.006,0.09]]",
            mu_source="demo μ=(0.08,0.12)" if obj == "max_sharpe" else None,
        )
        rep = solve_and_report(p, args, mode="demo", compact=True)
        w = [rep["weights"][s] for s in sym2]
        gap = max(abs(w[i] - gold[i]) for i in range(2))
        ok = gap < GOLD_TOL
        gold_checks.append({"objective": obj, "gold": gold, "solver": w, "max_abs_gap": gap, "pass": ok})
        print(f"  {label:<34} 解=({w[0]:.8f}, {w[1]:.8f})  偏差={gap:.2e}  {'✓' if ok else '✗ 超容差'}")
    cf2 = closed_form_min_variance(s2)
    cf_gap = max(abs(cf2[i] - w_mv_gold[i]) for i in range(2))
    gold_checks.append({"objective": "closed_form_formula_vs_hand", "max_abs_gap": cf_gap,
                        "pass": cf_gap < 1e-12})
    print(f"  闭式解公式自身 vs 手算常量（42/59）                    偏差={cf_gap:.2e}  "
          f"{'✓' if cf_gap < 1e-12 else '✗'}")

    # ---- B. 5 资产五目标 -------------------------------------------------
    rng = Lcg(20261008)
    n_b = 5
    days = 260
    sym5 = [f"A{i + 1}" for i in range(n_b)]
    betas = [1.1, 0.9, 0.7, 1.3, 0.5]
    idvol = [0.010, 0.014, 0.012, 0.018, 0.009]
    mu5 = [0.06, 0.05, 0.10, 0.12, 0.08]
    ret_rows = []
    for _ in range(days):
        mkt = rng.normal() * 0.010
        ret_rows.append([betas[i] * mkt + rng.normal() * idvol[i] + 0.0002 for i in range(n_b)])
    s5, _, _ = pairwise_cov(ret_rows, n_b)
    s5 = [[v * 252.0 for v in row] for row in s5]
    print("\n【B. 五目标对比】（5 资产合成面板 260 日 → 年化 Σ；上限 30%，全投资多头）")
    print(f"  {'目标':<18}{'波动':>8}{'持仓':>6}{'最大权重':>10}{'有效N':>8}  头部 RC")
    for obj in OBJECTIVES:
        p = default_problem(sym5, s5, mu=mu5, objective=obj, weight_cap=0.30, budget=1.0,
                            long_only=True, cov_source="demo 5 资产样本 Σ×252",
                            mu_source="demo μ")
        rep = solve_and_report(p, args, mode="demo", compact=True)
        d = rep["diagnostics"]
        rc_top = sorted(d["risk_contributions"].items(), key=lambda kv: -abs(kv[1]))[:2]
        print(f"  {OBJECTIVE_CN[obj]:<18}{d['portfolio_vol_annual'] * 100:>7.2f}%"
              f"{d['n_holdings']:>6}{d['max_weight']:>10.4f}{d['effective_n'] or 0:>8.1f}  "
              + "，".join(f"{k} {v * 100:.0f}%" for k, v in rc_top))
        reports_out.setdefault("objectives", {})[obj] = {
            "vol": d["portfolio_vol_annual"], "max_weight": d["max_weight"],
            "n_holdings": d["n_holdings"], "effective_n": d["effective_n"],
            "verification": rep["verification"],
        }

    sectors5 = {"A1": "银行", "A2": "银行", "A3": "白酒", "A4": "科技", "A5": "科技"}
    exposures5 = {"size": [-1.2, -0.4, 0.3, 0.9, 1.4]}
    w_prev_ls = [0.25, -0.25, 0.0, 0.25, -0.25]

    # ---- C. 行业中性 + 换手（多空美元中性） --------------------------------
    print("\n【C. 行业中性 + 个股上限 25% + 换手上限（多空美元中性，均值-方差 λ=8）】")
    p = default_problem(sym5, s5, mu=mu5, objective="mean_variance", risk_aversion=8.0,
                        long_only=False, weight_cap=0.25, budget=0.0,
                        sectors=sectors5, sector_targets={},
                        w_prev=w_prev_ls, turnover_limit=1.2,
                        cov_source="demo 5 资产样本 Σ×252", mu_source="demo μ")
    rep_c = solve_and_report(p, args, mode="demo")
    print(rep_c["report_text"])
    reports_out["sector_neutral_turnover"] = {
        "sector_exposure": rep_c["diagnostics"].get("sector_exposure"),
        "turnover_l1": rep_c["diagnostics"].get("turnover_l1"),
        "weights": rep_c["weights"],
    }

    # ---- D. 暴露中性（size） ----------------------------------------------
    print("\n【D. 暴露中性（size）最小方差】（全投资多头；暴露自动中心化）")
    p = default_problem(sym5, s5, objective="min_variance", weight_cap=0.30, budget=1.0,
                        long_only=True, exposures=exposures5, exposure_neutral=True,
                        cov_source="demo 5 资产样本 Σ×252")
    rep_d = solve_and_report(p, args, mode="demo")
    print(rep_d["report_text"])
    reports_out["exposure_neutral"] = {
        "factor_exposure": rep_d["diagnostics"].get("factor_exposure"),
        "factor_share": rep_d["diagnostics"].get("factor_share"),
        "identity_gap": rep_d["diagnostics"].get("identity_gap"),
    }

    # ---- E. 风险平价 RC 等分检查（无上限） ---------------------------------
    print("\n【E. 风险平价 RC 等分检查】（5 资产、无上限、全投资多头）")
    p = default_problem(sym5, s5, objective="risk_parity", budget=1.0, long_only=True,
                        cov_source="demo 5 资产样本 Σ×252")
    rep_e = solve_and_report(p, args, mode="demo", compact=True)
    print(rep_e["report_text"])
    reports_out["risk_parity_check"] = rep_e["verification"].get("risk_parity_rc")
    reports_out["gold_checks"] = gold_checks

    # ---- F. 报错语义演示 ---------------------------------------------------
    print("【F. 约束不可行的报错语义（预期报错，非故障）】")
    bad_cases = [
        ("上限过紧（5 只×0.05=0.25 < 1）", {"weight_cap": 0.05}),
        ("行业中性（目标和全 0）与预算 1 矛盾",
         {"weight_cap": 0.25, "budget": 1.0, "sectors": sectors5, "sector_targets": {}}),
        ("换手上限低于几何下界",
         {"weight_cap": 0.25, "w_prev": [1.0, 0, 0, 0, 0], "turnover_limit": 0.05, "budget": 1.0}),
    ]
    for desc, kw in bad_cases:
        base = {"objective": "min_variance", "long_only": not kw.pop("long_short", False),
                "cov_source": "demo"}
        try:
            p = default_problem(sym5, s5, **{**base, **kw})
            solve_and_report(p, args, mode="demo", compact=True)
            print(f"  ✗ 「{desc}」预期报错但通过了（demo 异常）")
        except SystemExit as exc:
            msg = " ".join(str(exc).split())
            print(f"  ✓ 「{desc}」→ {msg}")
    print("\ndemo 完成。研究工具输出，不构成投资建议。")

    if args.out:
        out = {"mode": "demo", "gold_checks": gold_checks,
               "objectives": reports_out.get("objectives", {}),
               "sector_neutral_turnover": reports_out.get("sector_neutral_turnover"),
               "exposure_neutral": reports_out.get("exposure_neutral"),
               "risk_parity_check": reports_out.get("risk_parity_check")}
        write_json(args.out, out)
    return 0


def write_json(path: str, payload: dict):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(json_safe(payload), ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已写入 JSON 报告：{p}")


# ------------------------------------------------------------------ 输入解析（CSV / JSON，纯标准库）

def _to_float(raw):
    if raw is None:
        return None
    s = str(raw).strip()
    if s == "" or s.lower() in ("na", "nan", "none", "null", "-"):
        return None
    try:
        v = float(s)
        return v if math.isfinite(v) else None
    except ValueError:
        return None


def read_csv_rows(path: str):
    with open(path, encoding="utf-8-sig", newline="") as fh:
        reader = csv.reader(fh)
        try:
            header = next(reader)
        except StopIteration as exc:
            raise SystemExit(f"CSV 为空：{path}") from exc
        rows = [row for row in reader if any(str(c).strip() for c in row)]
    return [h.strip() for h in header], rows


def load_returns_panel(path: str):
    """宽收益面板：第一列日期，其余列标的日收益。返回 (symbols, rows, dates, meta)。"""
    header, rows = read_csv_rows(path)
    if len(header) < 2:
        raise SystemExit(f"收益面板需至少 2 列（日期+1 个标的）：{path}")
    date_col = header[0]
    symbols = header[1:]
    drops = {}
    for c in symbols:
        drops[c] = drops.get(c, 0)
    out_rows, dates, skipped = [], [], 0
    for raw in rows:
        if len(raw) < len(header):
            raw = raw + [""] * (len(header) - len(raw))
        date_val = str(raw[0]).strip()
        vals = [_to_float(raw[j + 1]) for j in range(len(symbols))]
        if all(v is None for v in vals):
            skipped += 1
            continue
        dates.append(date_val)
        out_rows.append(vals)
    if len(dates) < 10:
        raise SystemExit(f"收益面板有效行过少（{len(dates)}），至少需要 10 行。检查第一列是否为日期。")
    meta = {"date_col": date_col, "n_rows": len(dates), "rows_skipped": skipped,
            "start": dates[0], "end": dates[-1]}
    return symbols, out_rows, dates, meta


def load_symbol_value_csv(path: str, value_name: str = "value"):
    """两列 CSV：symbol,value。返回 {symbol: value}。"""
    header, rows = read_csv_rows(path)
    if len(header) < 2:
        raise SystemExit(f"{path} 需要两列（symbol,{value_name}）")
    out = {}
    for raw in rows:
        if len(raw) < 2:
            continue
        sym = str(raw[0]).strip()
        v = _to_float(raw[1])
        if not sym or v is None:
            continue
        out[sym] = v
    if not out:
        raise SystemExit(f"{path} 无有效数据行（symbol,{value_name}）。")
    return out


def load_cov_csv(path: str):
    """symbol×symbol 协方差矩阵 CSV（第一列 symbol）。原样使用（年化口径由提供方负责）。"""
    header, rows = read_csv_rows(path)
    if len(header) < 2:
        raise SystemExit(f"协方差矩阵 CSV 至少 2 列：{path}")
    symbols = header[1:]
    n = len(symbols)
    if len(rows) != n:
        raise SystemExit(f"协方差矩阵应为方阵：表头 {n} 个标的、数据 {len(rows)} 行。")
    row_syms = [str(r[0]).strip() for r in rows]
    if row_syms != symbols:
        raise SystemExit("协方差矩阵行列标的顺序不一致（第一列须与表头相同顺序）。")
    cov = []
    for raw in rows:
        vals = [_to_float(raw[j + 1]) for j in range(n)]
        if any(v is None for v in vals):
            raise SystemExit(f"协方差矩阵存在空值：行 {raw[0]}")
        cov.append(vals)
    return symbols, cov


def load_exposures_csv(path: str):
    """symbol,x1[,x2...] 暴露表。返回 (symbols顺序列表, {暴露名: {symbol: 值}})。"""
    header, rows = read_csv_rows(path)
    if len(header) < 2:
        raise SystemExit(f"暴露表至少需要 2 列（symbol + 至少 1 个暴露）：{path}")
    names = header[1:]
    syms, data = [], {nm: {} for nm in names}
    for raw in rows:
        sym = str(raw[0]).strip()
        if not sym:
            continue
        syms.append(sym)
        for j, nm in enumerate(names):
            v = _to_float(raw[j + 1]) if j + 1 < len(raw) else None
            if v is not None:
                data[nm][sym] = v
    if not syms:
        raise SystemExit(f"暴露表无有效行：{path}")
    return names, data


def _align_or_die(problem_: dict, key: str, mapping: dict, what: str, fill=None, notes=None):
    """把 {symbol: value} 对齐到 problem_ 的 symbols；缺失按 fill 处理并记 note。"""
    symbols = problem_["symbols"]
    vals = []
    missing = []
    for s in symbols:
        if s in mapping:
            vals.append(mapping[s])
        elif fill is not None:
            vals.append(fill)
            missing.append(s)
        else:
            missing.append(s)
            vals.append(None)
    if missing:
        if fill is None:
            raise SystemExit(f"{what} 缺失标的 {len(missing)} 只（如 {missing[:3]}），无法对齐——补齐或剔除。")
        if notes is not None:
            notes.append(f"{what} 缺失 {len(missing)} 只，已按 {fill} 填充（如 {missing[:3]}）。")
    return vals


def run_input(args) -> int:
    notes = []
    if args.cov:
        symbols, cov = load_cov_csv(args.cov)
        mu, mu_source = None, None
        if args.mu:
            mu_map = load_symbol_value_csv(args.mu, "mu")
            mu = _align_or_die({"symbols": symbols}, "mu", mu_map, "μ", notes=notes)
            mu_source = f"CSV:{args.mu}（年化口径，原样使用）"
        problem_ = default_problem(
            symbols, cov, mu=mu, objective=args.objective, risk_aversion=args.risk_aversion,
            budget=args.budget, long_only=not args.long_short, weight_cap=args.weight_cap,
            turnover_limit=args.turnover_limit, notes=notes, mu_source=mu_source,
            cov_source=f"CSV:{args.cov}（矩阵原样使用：年化口径与单位由提供方负责）",
        )
        _attach_constraint_inputs(problem_, args, notes)
    elif args.input.lower().endswith(".json"):
        problem_ = load_problem_json(args.input, notes)
    else:
        symbols, rows, _dates, meta = load_returns_panel(args.input)
        cov, cov_meta = estimate_cov_matrix(rows, len(symbols), args.cov_method, notes)
        cov = [[v * args.periods for v in row] for row in cov]
        mu, mu_source = None, None
        if args.mu:
            mu_map = load_symbol_value_csv(args.mu, "mu")
            mu = _align_or_die({"symbols": symbols}, "mu", mu_map, "μ", notes=notes)
            mu_source = f"CSV:{args.mu}（年化口径，原样使用）"
        elif args.mu_from_mean:
            mu = []
            for j in range(len(symbols)):
                vals = [r[j] for r in rows if r[j] is not None]
                mu.append(sum(vals) / len(vals) * args.periods)
            mu_source = "历史均值×年化（--mu-from-mean，噪声大）"
            notes.append("μ 取面板历史均值年化：这是 MVO 极端解的经典来源（估计误差被 λ⁻¹Σ⁻¹ 放大），"
                         "仅作演示/对照，慎用于任何实盘决策。")
        problem_ = default_problem(
            symbols, cov, mu=mu, objective=args.objective, risk_aversion=args.risk_aversion,
            budget=args.budget, long_only=not args.long_short, weight_cap=args.weight_cap,
            turnover_limit=args.turnover_limit, notes=notes, mu_source=mu_source,
            cov_source=(f"CSV:{args.input}（{meta['start']}~{meta['end']}，{meta['n_rows']} 行，"
                        f"{cov_meta['method']}，年化 ×{args.periods}）"),
        )
        _attach_constraint_inputs(problem_, args, notes)
    _apply_cli_sector_flags(problem_, args)
    rep = solve_and_report(problem_, args, mode="input")
    print(rep["report_text"])
    if args.out:
        write_json(args.out, rep)
    return 0


def _attach_constraint_inputs(problem_, args, notes):
    """从命令行 CSV 挂载行业/暴露/上期权重（--input / --cov 模式共用）。"""
    if args.sectors and args.sectors != "auto":
        _header, rows = read_csv_rows(args.sectors)
        sec_map = {}
        for raw in rows:
            if len(raw) >= 2 and str(raw[0]).strip() and str(raw[1]).strip():
                sec_map[str(raw[0]).strip()] = str(raw[1]).strip()
        if not sec_map:
            raise SystemExit(f"行业映射 CSV 无有效行（symbol,sector）：{args.sectors}")
        symbols = problem_["symbols"]
        missing = [s for s in symbols if s not in sec_map]
        if missing:
            keep = [s for s in symbols if s in sec_map]
            if not keep:
                raise SystemExit("行业映射与标的池无交集。")
            idx = [i for i, s in enumerate(symbols) if s in sec_map]
            notes.append(f"行业映射缺失 {len(missing)} 只（如 {missing[:3]}），已从组合中剔除。")
            problem_["cov"] = [[problem_["cov"][i][j] for j in idx] for i in idx]
            if problem_.get("mu") is not None:
                problem_["mu"] = [problem_["mu"][i] for i in idx]
            problem_["symbols"] = keep
        problem_["sectors"] = {s: sec_map[s] for s in problem_["symbols"]}
    elif args.sectors == "auto":
        raise SystemExit("--sectors auto 仅在 --quantdb 模式可用。")
    if args.exposures:
        names, data = load_exposures_csv(args.exposures)
        cols = {}
        for nm in names:
            vals = _align_or_die(problem_, "exposures", data[nm], f"暴露「{nm}」", fill=0.0, notes=notes)
            cols[nm] = _demean(vals)
        problem_["exposures"] = cols
        notes.append("暴露已按标的池中心化（中性=对池内均值中性；原始正暴露+多头组合下不中心化通常无解）。")
    if args.prev_weights:
        pw = load_symbol_value_csv(args.prev_weights, "weight")
        problem_["w_prev"] = _align_or_die(problem_, "w_prev", pw, "上期权重", fill=0.0, notes=notes)


def _demean(vals):
    m = sum(vals) / len(vals)
    return [v - m for v in vals]


def _parse_sector_targets(spec, sectors):
    targets = {}
    for part in spec.split(","):
        if not part.strip():
            continue
        if "=" not in part:
            raise SystemExit(f"--sector-targets 格式应为「行业=值,行业=值」：{part}")
        key, val = part.split("=", 1)
        targets[key.strip()] = float(val)
    if sectors:
        unknown = [k for k in targets if k not in set(sectors.values())]
        if unknown:
            raise SystemExit(f"--sector-targets 含未知行业 {unknown}；池内行业：{sorted(set(sectors.values()))[:10]}")
    return targets


def _apply_cli_sector_flags(problem_, args):
    """把 --sector-neutral / --sector-targets 应用到已装配的 problem_（覆盖 JSON/文件自带配置）。"""
    if args.sector_targets:
        problem_["sector_targets"] = _parse_sector_targets(args.sector_targets, problem_.get("sectors"))
    elif args.sector_neutral:
        problem_["sector_targets"] = {}
    if problem_.get("sector_targets") is not None and not problem_.get("sectors"):
        raise SystemExit("行业约束需要行业映射：--sectors <csv>（--quantdb 下可用 --sectors auto），"
                         "或 JSON 的 sectors 字段。")
    if args.exposure_neutral:
        if not problem_.get("exposures"):
            raise SystemExit("--exposure-neutral 需要提供暴露：--exposures <csv>"
                             "（--quantdb 下用 --exposure-cols，JSON 用 exposures 字段）。")
        problem_["exposure_neutral"] = True


def load_problem_json(path: str, notes):
    """完整问题规格 JSON（与 barra-risk-model 类技能的对接协议，字段见 references）。

    必填：cov（嵌套 dict {sym:{sym:v}} 或行主序二维数组）、symbols（数组 cov 时必填）。
    可选：mu / sectors / sector_targets（出现即施加行业约束）/ exposures / w_prev /
    objective / risk_aversion / budget / long_only / weight_cap / turnover_limit / notes。
    JSON 中矩阵与暴露原样使用（年化口径、是否中心化由提供方负责）。
    """
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"JSON 解析失败：{exc}") from exc
    syms = payload.get("symbols")
    mu_raw = payload.get("mu")
    cov_raw = payload.get("cov")
    if cov_raw is None:
        raise SystemExit("问题 JSON 必须含 cov（嵌套 dict {sym:{sym:v}} 或行主序二维数组）与 symbols。")
    if isinstance(cov_raw, dict):
        if syms is None:
            syms = list(cov_raw.keys())
        cov = []
        for s in syms:
            row = cov_raw.get(s) or {}
            vals = [row.get(t) for t in syms]
            if any(v is None for v in vals):
                raise SystemExit(f"cov 缺 {s} 与其他标的的协方差（symbols 口径需一致）。")
            cov.append([float(v) for v in vals])
    else:
        cov = [[float(v) for v in row] for row in cov_raw]
        if syms is None:
            syms = [f"#{i}" for i in range(len(cov))]
    if len(cov) != len(syms) or any(len(r) != len(syms) for r in cov):
        raise SystemExit("cov 与 symbols 维度不一致。")
    mu, mu_source = None, None
    if mu_raw is not None:
        if isinstance(mu_raw, dict):
            mu = [mu_raw.get(s) for s in syms]
            if any(v is None for v in mu):
                raise SystemExit("mu 缺部分标的。")
            mu = [float(v) for v in mu]
        else:
            mu = [float(v) for v in mu_raw]
            if len(mu) != len(syms):
                raise SystemExit("mu 与 symbols 长度不一致。")
        mu_source = "JSON:mu（年化口径）"
    sectors = payload.get("sectors")
    if sectors is not None:
        missing = [s for s in syms if s not in sectors]
        if missing:
            raise SystemExit(f"JSON sectors 缺 {len(missing)} 只（如 {missing[:3]}）。")
    exposures = None
    exp_raw = payload.get("exposures")
    if exp_raw:
        names = sorted({k for row in exp_raw.values() for k in row})
        exposures = {nm: [float(exp_raw.get(s, {}).get(nm, 0.0)) for s in syms] for nm in names}
    w_prev = payload.get("w_prev")
    if isinstance(w_prev, dict):
        w_prev = [float(w_prev.get(s, 0.0)) for s in syms]
    elif w_prev is not None:
        w_prev = [float(v) for v in w_prev]
    problem_ = default_problem(syms, cov, mu=mu, mu_source=mu_source, sectors=sectors,
                               exposures=exposures, w_prev=w_prev, notes=notes,
                               cov_source=f"JSON:{path}（问题规格：矩阵原样使用）")
    problem_["sector_targets"] = payload.get("sector_targets")  # None=仅展示不约束
    problem_["exposure_neutral"] = bool(payload.get("exposure_neutral"))
    if problem_["exposure_neutral"] and not exposures:
        raise SystemExit("JSON exposure_neutral=true 但未提供 exposures 字段。")
    for key in ("objective", "risk_aversion", "budget", "long_only", "weight_cap", "turnover_limit"):
        if payload.get(key) is not None:
            problem_[key] = payload[key]
    if problem_["objective"] not in OBJECTIVES:
        raise SystemExit(f"JSON objective 非法：{problem_['objective']}，可选 {list(OBJECTIVES)}")
    extra_notes = payload.get("notes")
    if isinstance(extra_notes, list):
        notes.extend(str(x) for x in extra_notes)
    return problem_


# ------------------------------------------------------------------ QuantDB 装配层

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


def is_label_column(name: str) -> bool:
    return any(p.match(name.strip()) for p in LABEL_COL_PATTERNS)


def run_quantdb_list_columns(args) -> int:
    root = resolve_data_root()
    market = args.market.upper()
    dataset = args.exposure_dataset or DEFAULT_FACTOR_DATASET[market]
    key = (market, dataset)
    if key not in FACTOR_DATASET_PATHS:
        raise SystemExit(f"市场 {market} 无数据集 {dataset}；可用：{[d for (m, d) in FACTOR_DATASET_PATHS if m == market]}")
    import pandas as pd  # noqa: PLC0415
    import pyarrow.parquet as pq  # noqa: PLC0415

    base = root / FACTOR_DATASET_PATHS[key]
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
    print(f"\n标签列（禁止当暴露/信号，{len(labels)}）：{labels}")
    print(f"\n建议暴露列（{len(suggested)}；非空计数/nunique 取自最新分区，⚠=常量或全空）：")
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


def _read_kline_frames(root, market, parts):
    import pandas as pd  # noqa: PLC0415
    import pyarrow.parquet as pq  # noqa: PLC0415

    frames = []
    for p in parts:
        f = p / "data.parquet"
        cols = [c for c in ("symbol", "time", "close", "published_at")
                if c in pq.read_schema(f).names]
        df = pd.read_parquet(f, columns=cols)
        df["date"] = pd.to_datetime(df["time"]).dt.strftime("%Y-%m-%d")
        frames.append(df)
    if not frames:
        raise SystemExit(f"行情无分区：{parts}")
    kdf = pd.concat(frames, ignore_index=True)
    before = len(kdf)
    kdf = kdf.dropna(subset=["close"])
    if "published_at" in kdf.columns:
        kdf = kdf.sort_values(["symbol", "date", "published_at"], kind="stable") \
            .drop_duplicates(subset=["symbol", "date"], keep="last")
        kdf = kdf.drop(columns=["published_at"])
    else:
        kdf = kdf.drop_duplicates(subset=["symbol", "date"], keep="first")
    return kdf, before - len(kdf)


def _auto_sectors(root, market, symbols, notes):
    """行业映射：CN=instrument_detail.rs_hyname；HK=akshare_profile 所属行业；US=sector/*.parquet。"""
    import pandas as pd  # noqa: PLC0415

    out = {}
    if market == "CN":
        p = root / "quantdb/2_base_sector/instrument_detail/instrument_detail.parquet"
        df = pd.read_parquet(p, columns=["Symbol", "rs_hyname"])
        mp = df.dropna(subset=["rs_hyname"]).set_index("Symbol")["rs_hyname"].to_dict()
        out = {s: str(mp[s]) for s in symbols if s in mp and str(mp[s]).strip()}
    elif market == "HK":
        base = root / "quanthk/2_base_sector/akshare_profile"
        for s in symbols:
            f = base / f"{s}.parquet"
            if f.is_file():
                try:
                    df = pd.read_parquet(f, columns=["所属行业"])
                    v = str(df["所属行业"].iloc[0]).strip() if len(df) else ""
                    if v and v.lower() not in ("nan", "none"):
                        out[s] = v
                except Exception:  # noqa: BLE001 —— 个别文件损坏不应拖垮
                    continue
    elif market == "US":
        base = root / "quantus/2_base_sector/sector"
        for s in symbols:
            f = base / f"{s}.parquet"
            if f.is_file():
                try:
                    df = pd.read_parquet(f, columns=["sector"])
                    v = str(df["sector"].iloc[0]).strip() if len(df) else ""
                    if v and v.lower() not in ("nan", "none"):
                        out[s] = v
                except Exception:  # noqa: BLE001
                    continue
    missing = [s for s in symbols if s not in out]
    if missing:
        notes.append(f"行业映射缺失 {len(missing)} 只（如 {missing[:3]}），已从组合剔除。")
    return out


def run_quantdb(args) -> int:
    import pandas as pd  # noqa: PLC0415

    root = resolve_data_root()
    market = args.market.upper()
    notes = []
    kline_dir = root / KLINE_PATHS[market]
    all_parts = sorted(kline_dir.glob("dt=*"))
    if not all_parts:
        raise SystemExit(f"行情目录无分区：{kline_dir}")
    end_compact = (args.end or all_parts[-1].name[3:]).replace("-", "")
    kparts = [p for p in all_parts if p.name[3:] <= end_compact]
    if not kparts:
        raise SystemExit(f"行情无 ≤{end_compact} 的分区")
    end_dt = kparts[-1].name[3:]

    # ---- 票池：窗口末日成交额前 N（或 --symbols）
    last_file = kparts[-1] / "data.parquet"
    if args.symbols:
        universe = [s.strip() for s in args.symbols.split(",") if s.strip()]
        pool_note = "显式 --symbols"
    else:
        u = pd.read_parquet(last_file, columns=["symbol", "amount"]).dropna(subset=["amount"])
        u = u[u["amount"] > 0].sort_values("amount", ascending=False)
        n_take = args.top_n if args.top_n and args.top_n > 0 else len(u)
        universe = u["symbol"].head(n_take).astype(str).tolist()
        pool_note = f"窗口末日({end_dt})成交额前 {n_take}（事后票池，含轻微选择偏差）"
    uset = set(universe)

    # ---- 收益面板：取末端 cov_window+1 个分区（估计 Σ 只需末端窗口）
    cov_window = args.cov_window if args.cov_window and args.cov_window > 0 else 0
    if cov_window > 0:
        need = cov_window + 1
        kparts_use = kparts[-need:]
    else:
        start_compact = (args.start or "").replace("-", "")
        kparts_use = [p for p in kparts if p.name[3:] >= start_compact] if start_compact else kparts
    kdf, dup_dropped = _read_kline_frames(root, market, kparts_use)
    kdf = kdf[kdf["symbol"].isin(uset)]
    if kdf.empty:
        raise SystemExit("票池在行情窗口中无数据（检查 symbol 格式：CN=000001.SZ / HK=0001.HK / US=NVDA）")
    close_wide = kdf.pivot_table(index="date", columns="symbol", values="close", aggfunc="last") \
        .sort_index()
    # fill_method=None：绝不用前收盘顶替缺失日（'pad' 会把缺失日收益伪造成 0，系统性低估波动）
    ret_wide = close_wide.pct_change(fill_method=None).iloc[1:]
    ret_wide = ret_wide.dropna(axis=1, how="all")
    symbols = [s for s in universe if s in ret_wide.columns]
    ret_wide = ret_wide[symbols]
    if len(symbols) < 5:
        raise SystemExit(f"有效标的过少（{len(symbols)}）——扩大票池或检查数据。")
    rows = [[None if pd.isna(v) else float(v) for v in row] for row in ret_wide.to_numpy()]
    dates = [str(d) for d in ret_wide.index]

    # ---- Σ 与 μ
    cov, cov_meta = estimate_cov_matrix(rows, len(symbols), args.cov_method, notes)
    cov = [[v * args.periods for v in r] for r in cov]
    cov_source = (f"QuantDB {market}/1_kline_data/daily_forward（{dates[0]}~{dates[-1]}，"
                  f"{len(dates)} 个交易日，{cov_meta['method']}，年化 ×{args.periods}）；"
                  f"票池：{pool_note}")
    notes.append(KLINE_ADJUSTMENT_NOTE[market])
    if dup_dropped:
        notes.append(f"行情存在 {dup_dropped} 行重复 (symbol,date)（HK 双来源已知问题），"
                     "已按 published_at 保留 akshare 原始价。")
    mu, mu_source = None, None
    if args.mu_file:
        mu_map = load_symbol_value_csv(args.mu_file, "mu")
        mu = _align_or_die({"symbols": symbols}, "mu", mu_map, "μ", notes=notes)
        mu_source = f"CSV:{args.mu_file}（年化口径，原样使用）"
    elif args.mu_from_mean:
        mu = []
        for j in range(len(symbols)):
            vals = [r[j] for r in rows if r[j] is not None]
            mu.append(sum(vals) / len(vals) * args.periods)
        mu_source = "历史均值×年化（--mu-from-mean，噪声大）"
        notes.append("μ 取收益窗口历史均值年化：MVO 极端解的经典来源（估计误差被 λ⁻¹Σ⁻¹ 放大），仅作对照。")

    # ---- 行业与暴露
    sectors, exposures = None, None
    if args.sectors == "auto":
        sec_map = _auto_sectors(root, market, symbols, notes)
        keep = [s for s in symbols if s in sec_map]
        if not keep:
            raise SystemExit("自动行业映射与票池无交集。")
        if len(keep) < len(symbols):
            idx = [symbols.index(s) for s in keep]
            cov = [[cov[i][j] for j in idx] for i in idx]
            if mu is not None:
                mu = [mu[i] for i in idx]
            symbols = keep
        sectors = {s: sec_map[s] for s in symbols}
    if args.exposure_cols:
        exposures = _load_exposures_quantdb(root, market, symbols, args, notes)

    problem_ = default_problem(
        symbols, cov, mu=mu, objective=args.objective, risk_aversion=args.risk_aversion,
        budget=args.budget, long_only=not args.long_short, weight_cap=args.weight_cap,
        turnover_limit=args.turnover_limit, sectors=sectors, exposures=exposures,
        cov_source=cov_source, mu_source=mu_source, notes=notes,
    )
    if args.prev_weights:
        pw = load_symbol_value_csv(args.prev_weights, "weight")
        problem_["w_prev"] = _align_or_die(problem_, "w_prev", pw, "上期权重", fill=0.0, notes=notes)
    _apply_cli_sector_flags(problem_, args)
    rep = solve_and_report(problem_, args, mode="quantdb")
    rep["universe"] = {"method": pool_note, "asof": end_dt, "n_assets": len(symbols)}
    print(rep["report_text"])
    if args.out:
        write_json(args.out, rep)
    return 0


def _load_exposures_quantdb(root, market, symbols, args, notes):
    import pandas as pd  # noqa: PLC0415
    import pyarrow.parquet as pq  # noqa: PLC0415

    dataset = args.exposure_dataset or DEFAULT_FACTOR_DATASET[market]
    key = (market, dataset)
    if key not in FACTOR_DATASET_PATHS:
        raise SystemExit(f"市场 {market} 无数据集 {dataset}；可用：{[d for (m, d) in FACTOR_DATASET_PATHS if m == market]}")
    cols_req = [c.strip() for c in args.exposure_cols.split(",") if c.strip()]
    bad = [c for c in cols_req if is_label_column(c)]
    if bad:
        raise SystemExit(f"拒绝把标签列当暴露（前视泄漏）：{bad}")
    base = root / FACTOR_DATASET_PATHS[key]
    end_compact = (args.end or "").replace("-", "")
    parts = sorted(p for p in base.glob("dt=*")
                   if not end_compact or p.name[3:] <= end_compact)
    if not parts:
        raise SystemExit(f"数据集无分区：{base}")
    win = args.exposure_window if args.exposure_window and args.exposure_window > 0 else 20
    parts = parts[-win:]
    frames = []
    date_col = None
    for p in parts:
        f = p / "data.parquet"
        cols = list(pq.read_schema(f).names)
        if date_col is None:
            date_col = "time" if "time" in cols else ("date" if "date" in cols else None)
        missing = [c for c in cols_req if c not in cols]
        if missing:
            raise SystemExit(f"暴露列 {missing} 在分区 {p.name} 缺失——用 --list-columns 探查列名。")
        use = ["symbol"] + ([date_col] if date_col else [])
        df = pd.read_parquet(f, columns=use + cols_req)
        df["date"] = df[date_col].astype(str) if date_col else p.name[3:]
        frames.append(df)
    fac = pd.concat(frames, ignore_index=True)
    fac = fac[fac["symbol"].isin(set(symbols))]
    zdata = {}
    for c in cols_req:
        g = fac.dropna(subset=[c]).groupby("date")[c]
        z = (fac[c] - g.transform("mean")) / g.transform("std").replace(0, None)
        zdata[c] = z
    zdf = pd.DataFrame(zdata)
    zdf["symbol"] = fac["symbol"].values
    avg = zdf.dropna(how="all").groupby("symbol").mean()
    exposures = {}
    for c in cols_req:
        vals = []
        missing_n = 0
        for s in symbols:
            v = avg[c].get(s) if s in avg.index else None
            if v is None or (isinstance(v, float) and math.isnan(v)):
                vals.append(0.0)
                missing_n += 1
            else:
                vals.append(float(v))
        exposures[c] = _demean(vals)
        if missing_n:
            notes.append(f"暴露「{c}」在 {missing_n} 只标的上缺失，已填 0（中心化后=池内均值）。")
    notes.append(f"暴露来自 {market}/{dataset} 最近 {len(parts)} 个分区（截面 z-score 后按日均值，再池内中心化），"
                 "暴露中性=对池内均值中性。")
    return exposures


# ------------------------------------------------------------------ CLI

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="组合优化工具箱：均值-方差 / 最小方差 / 最大夏普 / 风险平价 / 最大分散化"
                    "（QuantDB 本地直读 CN/HK/US；--demo 纯标准库可跑）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--demo", action="store_true", help="内置确定性演示（纯标准库）")
    mode.add_argument("--input", metavar="PATH", help=".csv 宽收益面板 或 .json 完整问题规格")
    mode.add_argument("--cov", metavar="PATH", help="symbol×symbol 协方差矩阵 CSV（年化，原样使用）")
    mode.add_argument("--quantdb", action="store_true", help="QuantDB 直读（容器内 pandas/pyarrow）")

    p.add_argument("--objective", default="mean_variance", choices=list(OBJECTIVES),
                   help="优化目标（默认 mean_variance；min_variance/risk_parity/max_diversification 不需要 μ）")
    p.add_argument("--risk-aversion", type=float, default=5.0, help="均值-方差的 λ（默认 5.0）")
    p.add_argument("--weight-cap", type=float, default=None, help="单票权重上限（小数，如 0.05）")
    p.add_argument("--long-short", action="store_true", help="允许做空（下界 = −上限）")
    p.add_argument("--budget", type=float, default=1.0, help="sum(w) 目标（默认 1.0；美元中性用 0）")
    p.add_argument("--sectors", metavar="PATH|auto", default=None,
                   help="行业映射 CSV（symbol,sector）；--quantdb 下可用 auto")
    p.add_argument("--sector-neutral", action="store_true", help="行业目标和=0（多空美元中性书）")
    p.add_argument("--sector-targets", default=None, metavar="S=V,S=V",
                   help="各行业目标权重（和须等于 --budget），如 银行=0.3,科技=0.7")
    p.add_argument("--exposures", metavar="PATH",
                   help="暴露表 CSV（symbol,x1[,x2...]；自动中心化。默认仅做因子风险分解与展示）")
    p.add_argument("--exposure-neutral", action="store_true",
                   help="施加暴露中性约束 Xᵀw=0（需与 --exposures/--exposure-cols 同用）")
    p.add_argument("--prev-weights", metavar="PATH", help="上期权重 CSV（symbol,weight）")
    p.add_argument("--turnover-limit", type=float, default=None, help="换手上限 ‖w−w_prev‖₁ ≤ τ")
    p.add_argument("--mu", metavar="PATH", help="预期收益 CSV（symbol,mu，年化口径）")
    p.add_argument("--mu-from-mean", action="store_true",
                   help="用收益窗口历史均值×年化当 μ（显式开启；噪声大，MVO 极端解来源）")
    p.add_argument("--cov-method", choices=["auto", "ledoit_wolf", "sample"], default="auto",
                   help="Σ 估计：auto=有 sklearn 用 Ledoit-Wolf 否则样本；sample=成对完整样本")
    p.add_argument("--periods", type=int, default=252, help="年化周期数（默认 252）")
    p.add_argument("--solver", choices=["auto", "stdlib", "scipy"], default="auto",
                   help="求解后端（auto=容器内优先 scipy SLSQP；stdlib=纯标准库投影梯度）")
    p.add_argument("--cross-check", action="store_true", help="额外跑另一后端并报告权重差")
    p.add_argument("--max-iter", type=int, default=PG_MAX_ITER, help=f"迭代上限（默认 {PG_MAX_ITER}）")
    p.add_argument("--out", metavar="PATH", help="JSON 报告输出路径")

    q = p.add_argument_group("QuantDB 模式")
    q.add_argument("--market", default="CN", choices=["CN", "HK", "US"], help="市场（默认 CN）")
    q.add_argument("--start", default=None, help="数据起点 YYYY-MM-DD（仅 --cov-window 0 时生效）")
    q.add_argument("--end", default=None, help="数据终点 YYYY-MM-DD（默认最新）")
    q.add_argument("--cov-window", type=int, default=120,
                   help="取窗口末端 N 个交易日估计 Σ（默认 120；0=整个 --start~--end 窗口）")
    q.add_argument("--top-n", type=int, default=60, help="按窗口末日成交额取前 N 只（默认 60；0=全部）")
    q.add_argument("--symbols", default=None, help="显式票池（parquet 后缀式，逗号分隔）")
    q.add_argument("--mu-file", metavar="PATH", help="预期收益 CSV（symbol,mu，年化）")
    q.add_argument("--exposure-cols", default=None, help="暴露列（逗号分隔，从因子数据集读取）")
    q.add_argument("--exposure-dataset", default=None,
                   help=f"暴露数据集（默认 {DEFAULT_FACTOR_DATASET}）")
    q.add_argument("--exposure-window", type=int, default=20,
                   help="暴露取最近 N 个分区的截面 z-score 再按日均值（默认 20）")
    q.add_argument("--list-columns", action="store_true", help="列出数据集列并标注标签列后退出")
    return p


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001 —— Windows 控制台兜底
        pass
    args = build_parser().parse_args(argv)

    if args.list_columns:
        if not args.quantdb:
            raise SystemExit("--list-columns 仅在 --quantdb 模式下可用")
        return run_quantdb_list_columns(args)
    if args.demo:
        return run_demo(args)
    if args.quantdb:
        return run_quantdb(args)
    if args.input or args.cov:
        return run_input(args)
    raise SystemExit("请指定模式：--demo / --input <csv|json> / --cov <csv> / --quantdb（见 --help）")


if __name__ == "__main__":
    sys.exit(main())
