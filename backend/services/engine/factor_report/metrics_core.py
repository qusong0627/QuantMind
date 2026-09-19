"""因子报告机构级指标层 —— **口径的唯一事实源**（纯函数、无 IO、无第三方依赖）。

机构级报告与「看起来像机构级」的唯一分界是**口径有没有歧义**。本模块把每条口径
写成命名常量 + 可执行代码 + :data:`DEFINITIONS` 文案三份，三者同源；前端 ⓘ 悬停
读的是同一份 ``DEFINITIONS``（经 ``/detail`` 的 ``definitions`` 字段下发），
**改口径只改这里**，不允许前端或服务层另抄一份文字。

口径（用参考报告反推验证过，见 ``test_factor_report_metrics.py``）::

    G1 = 因子值最小 … G10 = 因子值最大（与 IC 符号无关）
    多空组合 = G3 多 50% + G9 空 50%（dollar-neutral，总杠杆 100%），每日再平衡，等权
    ls_t   = 0.5 × (ret_G3,t − ret_G9,t)，毛收益
    换手    = 日均单边（两腿成员变动比例取平均），turnover ∈ [0, 1]
    Returns = mean(ls_daily) × 252          ← **不是复利 CAGR**
    IR      = mean/std × √252，无风险利率 = 0
    Margin  = Returns / Turnover
    Fitness = IR × √(|Returns| / max(Turnover, 0.125))
    ICIR    = mean(IC)/std(IC)，**不年化**（与平台既有口径一致）
    t 值    = 普通 ICIR×√n 与 Newey-West 调整值**并列**
    成本    = 默认双边 20bp，毛/净双口径并列

反推锚点（用户给出的参考报告）::

    Returns -22.91% / IR -1.7044 / Turnover 76.35%
    → Fitness = -1.7044 × √(0.2291/0.7635) = -0.9336   （报告 -0.9337）
    → Margin  = -0.2291 / 0.7635           = -0.3001   （报告 -0.300）

且 μ×252 与 IR、σ 三者自洽（σ = μ√252/IR = 0.847%，报告给 σ=0.85%），
而按 CAGR 反推会得到 -20.4%，对不上 —— 故 ``Returns`` 取简单年化。

⚠️ **不要**用 ``factor_deep_dive.crowding()`` 里的 ``ls_drawdown`` 与本模块的
:func:`max_drawdown` 对拍：前者是「末值相对历史高点」的**末值回撤**
（:func:`tail_drawdown`），不是最大回撤。两者只满足 ``最大回撤 ≤ 末值回撤 ≤ 0``。
"""

from __future__ import annotations

import math
import warnings
from typing import Any
from collections.abc import Iterable, Sequence

import numpy as np

# 秩的唯一实现（NaN 感知）在 neutralize，本模块不另抄一份。
# 该模块 import 期只做纯路径拼接（resolve_quantdb_subdir 不检查存在性），无 IO。
from .neutralize import rank_pct

# ── 口径常量（改口径 = 改这里，且只有这里）──────────────────────────
TRADING_DAYS = 252
"""年化交易日数。``Returns = μ_daily × TRADING_DAYS``。"""

TURNOVER_FLOOR = 0.125
"""Fitness 公式里换手率的下限（WorldQuant BRAIN 约定）。低换手因子不因此被无限放大。"""

DEFAULT_PARTICIPATION = 0.10
"""容量估算的参与率假设：单标的单日成交不超过其日成交额的 10%。
与模拟盘 ``SIMULATION_MAX_PARTICIPATION_RATE`` 同值 —— **是假设，不是实测**。"""

DEFAULT_COST_BPS = 20.0
"""默认双边交易成本（bp）。与 ``portfolio.DEFAULT_ROUND_TRIP_COST`` ≈0.20% 同口径。"""

MIN_SAMPLES = 20
"""任何横截面统计的最少样本数（沿用 ``service._spearman`` 的既有门槛）。"""

MIN_TSTAT_SAMPLES = 3
"""t 检验的最少样本数。"""

MAD_TO_SIGMA = 1.4826
"""MAD → 正态等价标准差的换算系数（``σ ≈ 1.4826 × MAD``）。"""

CLIP_MAD_K = 3.0
"""去极值比例（``clip_frac``）的判界倍数：偏离中位超过 ``3 × 1.4826 × MAD`` 记为极端值。"""

DEGENERATE_REL_STD = 1e-12
"""「实质无变化」的相对标准差门槛（相对序列自身量级）。

取 1e-12 而非 ``eps≈2.2e-16``：常量列的残差是若干 ulp 的舍入噪声，门槛必须高到
能盖住它；同时远低于任何真实信号（收益率的相对标准差在 1e-2 量级），不会误杀。
"""


# ── 口径文案（前端 ⓘ 的唯一来源）────────────────────────────────────
DEFINITIONS: dict[str, str] = {
    "group": "G1 = 因子值最小 … G10 = 因子值最大，与 IC 符号无关。"
    "故 IC<0 的因子按 G 高多 / G 低空使用天然亏钱，方向需自行反转。",
    "long_short": "多空组合 = 做多 G3（50%）+ 做空 G9（50%），dollar-neutral、总杠杆 100%，"
    "每日再平衡、等权持有。",
    "ls_daily": "多空日收益 ls_t = 0.5 × (ret_G3,t − ret_G9,t)，**毛收益**（不扣成本）。",
    "turnover": "日均单边换手：两腿各自成员变动比例取平均，∈ [0,1]。",
    "returns": "年化收益 = 日均多空收益 × 252（**简单年化，不是复利 CAGR**）。"
    "复利累计收益另列。",
    "ir": "信息比率 = 日均收益 / 日收益标准差 × √252，无风险利率取 0。",
    "margin": "Margin = Returns / Turnover（量纲不齐，为 WorldQuant BRAIN 既有约定）。",
    "fitness": f"Fitness = IR × √(|Returns| / max(Turnover, {TURNOVER_FLOOR}))。"
    f"换手低于 {TURNOVER_FLOOR} 时按 {TURNOVER_FLOOR} 计（BRAIN 约定）。",
    "ic": "日频横截面 Spearman 秩相关：当日因子值 vs T+k 前瞻收益。",
    "icir": "ICIR = mean(IC) / std(IC)，**不年化**（与平台既有口径一致）。",
    "t_value": "普通 t 值 = ICIR × √n。IC 存在自相关时会**高估**显著性，"
    "请与 Newey-West 调整 t 值并列阅读。",
    "nw_t": "Newey-West 调整 t 值：按 Bartlett 核修正自相关后的标准误，"
    "滞后阶数取 4(n/100)^(2/9)。自相关越强，NW t 越小。",
    "bhy": "Benjamini-Yekutieli 多重检验校正 q 值。同时检验的因子越多，"
    "同一 p 值对应的 q 越大 —— 这是「挖了 2000 个因子总有几个显著」的解药。",
    "bootstrap": "Bootstrap 百分位置信区间（默认 2000 次重抽样），不依赖正态假设。",
    "cost": f"双边交易成本默认 {DEFAULT_COST_BPS:.0f}bp（可配）。毛/净双口径并列，"
    "净口径下能看到成本吃掉多少。",
    "break_even": "盈亏平衡成本 = 日均毛收益 / 日均换手 × 10000（bp）。"
    "实际成本高于它，净收益转负。",
    "capacity": f"简化容量模型：AUM ≤ 参与率 × 持仓中位日成交额 × 持仓数 / 日换手。"
    f"参与率默认 {DEFAULT_PARTICIPATION:.0%}，**是假设不是实测**，"
    f"未计入冲击成本与相关性衰减。",
    "tradable": "理想口径假设任何价位都能成交；可交易口径剔除 T 日涨停（挡多头腿）、"
    "跌停（挡空头腿）、停牌的个股。「双轨差额」表达的是**理想口径高估了多少**，"
    "不是「策略会亏这么多」。",
    "max_drawdown": "最大回撤 = min(净值 / 历史最高净值 − 1)，取全区间最差值。"
    "区别于「末值回撤」（末值相对历史高点）。",
    "n_days": "有效天数：该指标实际参与计算的交易日数。样本不足时不给年化。",
    "style_corr": "因子值与自算 Barra CNE5 式十大风格暴露的横截面秩相关（逐日均值）。"
    "自算口径，与商业 Barra 数据不完全可比。",
    "ic_half": "半截面 IC：按当日因子值中位把股票切成上下两半，各算一次秩相关。"
    "上下半 IC 差异大 = 因子只在一端有效（非线性），比全截面 IC 更能暴露这种结构。",
    "ic_neutral": "中性化 IC：因子值先按行业去均值、再对 rank(总市值) 正交，"
    "残差与前瞻收益的秩相关。「IC 是不是只是行业/市值的代理」的答案。"
    "口径与 factor_deep_dive 收口到同一实现。",
    "ic_domain": "分市值域 IC：按当日总市值三分位切成 小/中/大盘 三个子域，各算一次秩相关。"
    "小盘有效而大盘无效的因子，大资金用不了 —— 这是「容量适用性」的直接证据。"
    "分位切点由当日全体市值确定，跨因子一致。",
    "clip_frac": "去极值比例：当日因子值偏离中位数超过 3×1.4826×MAD 的样本占比。"
    "比例异常高说明数据分布不稳（脏数据/口径漂移），是数据质量信号。"
    "常数列（MAD=0）记 NaN 而非 0。",
    "n_valid": "IC 可用样本数：当日因子值与前瞻收益同时有效的股票数。"
    "样本骤降的日子其 IC 统计意义弱，务必与该日的 IC 一并阅读。",
    "attribution": "风格归因回归：多空（或超额）日收益对十大风格纯因子收益做时序回归，"
    "给出 α、各风格 β 与 R²，用以分辨「超额是真本事还是风格 beta」。",
    "independence": "独立性代理：与该因子相关性最高的存量因子（含负相关）的 |ρ|，"
    "叠加既有五维评分卡的独立性维度（红线 |ρ|>0.9 需去重）。",
}


# ═══════════════ 0. 基础统计原语 ═══════════════


def rank_of(x: np.ndarray) -> np.ndarray:
    """升序秩（1..n），并列取稳定序。与 ``service._rank`` 同口径。"""
    arr = np.asarray(x, dtype=np.float64)
    order = np.argsort(arr, kind="stable")
    r = np.empty(arr.size, dtype=np.float64)
    r[order] = np.arange(1, arr.size + 1, dtype=np.float64)
    return r


def _degenerate(v: np.ndarray, d: np.ndarray) -> bool:
    """序列是否**实质**无变化：中心化后的标准差相对自身量级可忽略。

    只判 ``d @ d == 0`` 不够 —— 常量列的浮点均值不会精确等于该常量（0.3 不可精确
    表示），残差是 1 ulp 级，``d @ d`` 于是非零，相关算出来是 ``-4.6e-17`` 这种噪声：
    看着像「无关」，实际是「未定义」。而**近**常量列更危险 —— 噪声被放大成 ±1，
    一个稳稳的假相关。故按相对量级判而不是按绝对零判。
    """
    scale = float(np.max(np.abs(v))) if v.size else 0.0
    if scale <= 0.0:
        return True
    return float(np.sqrt(d @ d / v.size)) <= scale * DEGENERATE_REL_STD


def _pearson_raw(a: np.ndarray, b: np.ndarray) -> float | None:
    """皮尔逊相关（不设样本门槛；门槛由各调用方按语义自定）。

    任一序列**实质**无变化 → ``None``（见 :func:`_degenerate`），不是 0。
    """
    x = np.asarray(a, dtype=np.float64)
    y = np.asarray(b, dtype=np.float64)
    n = min(x.size, y.size)
    if n < 2:
        return None
    x, y = x[:n], y[:n]
    dx, dy = x - x.mean(), y - y.mean()
    if _degenerate(x, dx) or _degenerate(y, dy):
        return None
    denom = math.sqrt(float(dx @ dx) * float(dy @ dy))
    if denom <= 0:
        return None
    return float(dx @ dy / denom)


def pearson(a: np.ndarray, b: np.ndarray) -> float | None:
    """皮尔逊相关；样本不足 ``MIN_SAMPLES`` / 任一序列无变化 → None。"""
    x = np.asarray(a, dtype=np.float64)
    if x.size < MIN_SAMPLES:
        return None
    return _pearson_raw(a, b)


def spearman(a: np.ndarray, b: np.ndarray) -> float:
    """秩相关（各自转秩后 Pearson）。不足/无变化返回 NaN。

    注意：常量序列的秩相关在数学上未定义 —— 必须在**转秩之前**判方差，
    否则稳定排序会给常量列安排上 1..n 的假秩，算出一个看似正常的相关性。
    """
    x = np.asarray(a, dtype=np.float64)
    y = np.asarray(b, dtype=np.float64)
    if x.size < MIN_SAMPLES or np.nanstd(x) <= 0 or np.nanstd(y) <= 0:
        return float("nan")
    got = pearson(rank_of(x), rank_of(y))
    return float("nan") if got is None else got


def _finite(x: Any) -> np.ndarray:
    arr = np.asarray(x, dtype=np.float64).ravel()
    return arr[np.isfinite(arr)]


def _none_if_nan(v: Any) -> float | None:
    """非有限 → None。对外 JSON 绝不能带 NaN/Inf（FastAPI 序列化会直接 500）。"""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


# ═══════════════ 1. IC 体系 ═══════════════


def half_ic(
    rank_x: np.ndarray, y: np.ndarray, *, top: bool, min_n: int = MIN_SAMPLES
) -> float | None:
    """半截面 IC：按因子值的中位切成上下半，各算一次秩相关。

    Args:
        rank_x: 当日**因子值的秩**（已排序好，避免二次排序）。
        y: 当日 T+k 前瞻收益（原始值）。
        top: True 取因子值高于中位的半区（Top 半），False 取另一半。

    Returns:
        秩相关系数；任一半样本不足 ``min_n`` 返回 None。
    """
    rx = np.asarray(rank_x, dtype=np.float64).ravel()
    yy = np.asarray(y, dtype=np.float64).ravel()
    n = min(rx.size, yy.size)
    if n < min_n * 2:
        return None
    rx, yy = rx[:n], yy[:n]
    ok = np.isfinite(rx) & np.isfinite(yy)
    rx, yy = rx[ok], yy[ok]
    if rx.size < min_n * 2:
        return None
    med = np.median(rx)
    mask = rx > med if top else rx <= med
    if int(mask.sum()) < min_n:
        return None
    # rx 在半区内仍是单调的 → 直接 Pearson(rx, rank(y)) 即该半区的秩相关
    return pearson(rx[mask], rank_of(yy[mask]))


def cum_ic(ic: Sequence[float]) -> np.ndarray:
    """累计 IC。缺口（NaN）当日不累计，但该点的累计值标为 NaN（不冒充 0）。"""
    arr = np.asarray(ic, dtype=np.float64).ravel()
    if arr.size == 0:
        return arr
    out = np.cumsum(np.where(np.isfinite(arr), arr, 0.0))
    out[~np.isfinite(arr)] = np.nan
    return out


def win_rate(ic: Sequence[float]) -> float | None:
    """IC > 0 的交易日占比。零项守卫：空/全 NaN → None。"""
    arr = _finite(ic)
    if arr.size == 0:
        return None
    return float((arr > 0).mean())


def monotonicity(quantile_mean: Sequence[float]) -> float | None:
    """分位序号与分位平均收益的秩相关（±1 = 完美单调）。

    分位只有 10 个点，故**不套用 ``MIN_SAMPLES=20`` 的横截面门槛**
    （那是给逐日几千只股票用的），自带 ≥3 的门槛。
    """
    q = np.asarray(quantile_mean, dtype=np.float64).ravel()
    if q.size < 3 or not np.isfinite(q).all() or float(q.std()) <= 0:
        return None
    got = _pearson_raw(rank_of(np.arange(1, q.size + 1, dtype=np.float64)), rank_of(q))
    return _none_if_nan(got)


def half_life_days(
    ic_by_horizon: dict[Any, float], horizons: Sequence[int] = (1, 2, 5, 10, 20)
) -> float | None:
    """IC 衰减半衰期：|ic_H| ≤ |ic_1|/2 的最早 H（线性插值）；未衰减到一半 → None。

    缺失/NaN 视界**跳过而非当 0** —— 把「未测」当「已衰减」会得出假半衰期。
    """

    def _get(h: int) -> float | None:
        for key in (h, str(h), f"fwd_ret_{h}", f"t+{h}"):
            if key in ic_by_horizon:
                return _none_if_nan(ic_by_horizon[key])
        return None

    base = abs(_get(horizons[0]) or 0.0)
    if base <= 0:
        return None
    prev_h, prev_v = horizons[0], base
    for h in horizons[1:]:
        v = _get(h)
        if v is None:
            continue
        v = abs(v)
        if v <= base / 2.0:
            if prev_v <= v:
                return float(h)
            frac = (prev_v - base / 2.0) / max(1e-12, prev_v - v)
            return float(prev_h) + frac * (h - prev_h)
        prev_h, prev_v = h, v
    return None


def ic_autocorr(ic: Sequence[float], lags: int = 20) -> list[float | None]:
    """IC 序列的自相关 lag1..lagN（有偏估计，与 NW 的 γ 同分母）。"""
    x = np.asarray(ic, dtype=np.float64).ravel()
    out: list[float | None] = [None] * max(lags, 0)
    if lags <= 0 or x.size < 3:
        return out
    d = x - np.nanmean(x)
    d = np.where(np.isfinite(d), d, 0.0)
    denom = float(d @ d)
    if denom <= 0:
        return out
    for j in range(1, lags + 1):
        if x.size - j < 3:
            break
        out[j - 1] = float((d[j:] @ d[:-j]) / denom)
    return out


# ═══════════════ 2. 多空组合与回撤 ═══════════════


def ls_daily(q_mat: np.ndarray, long_group: int, short_group: int) -> np.ndarray:
    """多空日收益 = 0.5 × (long 组 − short 组)。分组下标 1-based。

    Raises:
        ValueError: 分组下标越界（G1..G{n}）。
    """
    q = np.asarray(q_mat, dtype=np.float64)
    if q.ndim != 2:
        raise ValueError("q_mat 必须是 T×G 的二维矩阵")
    n_g = q.shape[1]
    for name, g in (("long_group", long_group), ("short_group", short_group)):
        if not (1 <= int(g) <= n_g):
            raise ValueError(f"{name}={g} 越界（合法范围 1..{n_g}）")
    return 0.5 * (q[:, int(long_group) - 1] - q[:, int(short_group) - 1])


def cum_curve(daily: Sequence[float]) -> np.ndarray:
    """净值曲线：逐日复利，缺口按 0 收益处理（停牌日不产生收益）。"""
    d = np.asarray(daily, dtype=np.float64).ravel()
    return np.cumprod(1.0 + np.nan_to_num(d, nan=0.0))


def max_drawdown(curve: Sequence[float]) -> float | None:
    """最大回撤（≤0）。区别于末值回撤，见 :func:`tail_drawdown`。"""
    c = _finite(curve)
    if c.size == 0:
        return None
    peak = np.maximum.accumulate(c)
    dd = c / np.maximum(peak, 1e-12) - 1.0
    return float(np.min(dd))


def tail_drawdown(curve: Sequence[float]) -> float | None:
    """末值回撤（≤0）：末值相对历史高点。``factor_deep_dive.crowding()`` 用的是这个口径。"""
    c = _finite(curve)
    if c.size == 0:
        return None
    peak = float(np.max(c))
    if peak <= 0:
        return None
    return float(c[-1] / peak - 1.0)


def drawdown_episodes(
    curve: Sequence[float], top: int = 5, min_depth: float = 1e-9
) -> list[dict[str, Any]]:
    """回撤区间（峰→谷），按深度降序取前 ``top`` 段。

    返回 ``[{i0, i1, dd, days, recovered}]``；``i0/i1`` 是曲线下标，日期由调用方映射。
    """
    c = np.asarray(curve, dtype=np.float64).ravel()
    if c.size == 0 or top <= 0:
        return []
    eps: list[dict[str, Any]] = []
    peak, peak_i = -math.inf, 0
    trough, trough_i = math.inf, 0

    def _flush(recovered: bool) -> None:
        if (
            math.isfinite(peak)
            and math.isfinite(trough)
            and peak > 0
            and (peak - trough) > min_depth
        ):
            eps.append(
                {
                    "i0": int(peak_i),
                    "i1": int(trough_i),
                    "dd": float(trough / peak - 1.0),
                    "days": int(trough_i - peak_i),
                    "recovered": bool(recovered),
                }
            )

    for i, v in enumerate(c):
        if not math.isfinite(v):
            continue
        if v >= peak:
            _flush(recovered=True)
            peak, peak_i, trough, trough_i = float(v), i, math.inf, i
        elif v < trough:
            trough, trough_i = float(v), i
    _flush(recovered=False)  # 尾部未恢复段
    eps.sort(key=lambda e: e["dd"])
    return eps[:top]


# ═══════════════ 3. 收益分布与尾部 ═══════════════


def return_distribution(daily: Sequence[float], bins: int = 40) -> dict[str, Any]:
    """日收益分布：μ/σ/n + 直方图 + 偏度/超额峰度 + 历史 VaR/CVaR(95/99)。"""
    empty = {
        "n": 0,
        "mu": None,
        "sigma": None,
        "skew": None,
        "kurt": None,
        "bin_edges": [],
        "counts": [],
        "var_95": None,
        "cvar_95": None,
        "var_99": None,
        "cvar_99": None,
    }
    d = _finite(daily)
    if d.size == 0:
        return empty
    mu = float(d.mean())
    sigma = float(d.std(ddof=1)) if d.size > 1 else 0.0
    counts, edges = np.histogram(d, bins=max(1, int(bins)))
    skew = kurt = None
    if sigma > 0:
        z = (d - mu) / sigma
        skew = float((z**3).mean())
        kurt = float((z**4).mean() - 3.0)  # 超额峰度（正态 = 0）

    def _tail(q: float) -> tuple[float | None, float | None]:
        var = float(np.quantile(d, q))
        tail = d[d <= var]
        cvar = float(tail.mean()) if tail.size else None
        return var, cvar

    var95, cvar95 = _tail(0.05)
    var99, cvar99 = _tail(0.01)
    return {
        "n": int(d.size),
        "mu": mu,
        "sigma": sigma,
        "skew": skew,
        "kurt": kurt,
        "bin_edges": [float(v) for v in edges],
        "counts": [int(v) for v in counts],
        "var_95": var95,
        "cvar_95": cvar95,
        "var_99": var99,
        "cvar_99": cvar99,
    }


