"""自算 CNE5 式风格暴露与纯因子收益（A 股）——风格维度的**唯一**实现。

为什么自算：QuantDB 六大数据类里没有任何风格因子产物（全仓 grep ``style_factors``
零命中），而「超额收益是真本事还是风格 beta」是机构级因子报告必须回答的问题。
本模块产出两样东西，供 ``build_factor_report.py`` 与详情接口消费：

1. 逐日**风格暴露**（标准化后的 12 列）→ 因子与风格的截面秩相关、风格归因的回归元
2. 逐日**纯因子收益**（WLS 解出的 12 列）→ 多空/超额收益对风格的时序归因

## 十二个风格（口径写死在这里，改口径只能改这里）

| 风格 | 口径 |
|---|---|
| size | ``ln(total_mv)`` |
| beta | 252 日半衰期(63 日)加权回归的斜率 vs 沪深300 |
| momentum | ``Σ ln(1+r)``，T−252…T−21（**剔除最近 21 日**，避免与短期反转混淆） |
| residvol | ``0.74·z(DASTD) + 0.16·z(CMRA) + 0.10·z(HSIGMA)``（各自先标准化再加权） |
| nlsize | ``size³`` 再对 size 正交 |
| btop | ``1/pb`` |
| liquidity | ``0.35·z(STOM21) + 0.35·z(STOQ63) + 0.30·z(STOA252)``，换手 = volume/流通股本 |
| earningsyield | ``net_profit_ttm / total_mv`` |
| growth | ``0.5·z(营收TTM同比) + 0.5·z(净利TTM同比)`` |
| leverage | ``(ME + 长期借款 + 应付债券) / ME``，ME = total_mv |
| rmw | ``扣非净利TTM / 净资产``（用户指定的 A 股 RMW 口径，非学术 OP/BE） |
| cma | ``−资本开支TTM / 总资产``（用户指定口径；取负号使**高暴露 = 投资保守**，同 FF5 方向） |

后两个是 Fama–French 五因子的 RMW/CMA 在 A 股的对应物（用户 2026-09-22 指定口径：
价值用 BP、盈利用扣非 ROE、投资用资本开支/总资产）。财务面板是按公告日 PIT 的
TTM 序列，由 ``build_style_factors.py`` 取数；**面板缺财务键时这两个风格整体缺席**
（见 :func:`descriptors_from_panels`），此时全链路退化为原来的 10 风格，不会 KeyError。

标准化流水线（逐日，见 :func:`standardize_pipeline`）：
**±3MAD 缩尾 → z-score → 对 size 正交（部分风格）→ 行业去均值 → 重新标准化**。

⚠️ **不声称与商业 Barra 数据可比**：CNE5 的精细部分（Beta 的贝叶斯收缩、DASTD 的
指数加权参数、Growth 的 5 年历史增长率）按简化口径实现，产物一律标注「自算 CNE5 式」。

## 单位（2026-09-19 实测 quantdb，勿凭印象改）

``valuation``：``total_mv / net_profit_ttm / revenue_ttm`` = **元**，``circulating_capital`` = **股**；
``daily_forward``：``volume`` = 股、``amount`` = 万元；``balance``：金额 = 元。
→ 上面四个比值型风格（btop/earningsyield/liquidity/leverage）**量纲自洽，无需任何
换算常数**（这是刻意选的构造：需要 1e4 之类魔数的地方，宁可换一个口径）。
另外 z-score 对**同一列**的整体缩放不变，故 liquidity 即使单位判断有误也不受影响
（误差只会在「不同列之间」出现，而它只依赖一列）。

## NaN 纪律

一律**不插值、不填充**：覆盖不足 → NaN，并由调用方统计覆盖率写进 ``meta.json``。
这不是洁癖：风格暴露若被填充过，因子与风格的相关系数会把「填充值」也当成观测。
"""

from __future__ import annotations

import warnings
from collections.abc import Sequence
from pathlib import Path

import numpy as np

from . import neutralize as NZ
from .metrics import CLIP_MAD_K, MAD_TO_SIGMA

STYLE_NAMES: tuple[str, ...] = (
    "size",
    "beta",
    "momentum",
    "residvol",
    "nlsize",
    "btop",
    "liquidity",
    "earningsyield",
    "growth",
    "leverage",
    "rmw",
    "cma",
)

SIZE_ORTHOGONAL_STYLES: tuple[str, ...] = (
    "nlsize",
    "btop",
    "liquidity",
    "earningsyield",
    "growth",
    "leverage",
    "rmw",
    "cma",
)
"""对 size 正交的风格 —— 让「价值/成长/杠杆」讲的是自身，不是市值的影子。"""

BETA_WINDOW_DAYS = 252
BETA_HALFLIFE_DAYS = 63
"""Beta 回归的窗口与半衰期（日）。半衰期取 63 交易日 ≈ 一个季度，与 CNE5 同量级。"""

MOMENTUM_SKIP_DAYS = 21
"""动量剔除最近 21 个交易日 —— 近期收益混着短期反转，不剔除会得到两个信号的混合体。"""

MIN_WINDOW_FRACTION = 0.6
"""时序描述子要求窗口内至少这么比例的**有效权重**才出数。

不足即 NaN（不是「有多少算多少」）：252 日只覆盖 20 天的 beta 是噪声，
但它在报告里和真 beta 长得一模一样。
"""

RESIDVOL_WEIGHTS: tuple[tuple[str, float], ...] = (
    ("dastd", 0.74),
    ("cmra", 0.16),
    ("hsigma", 0.10),
)
LIQUIDITY_WINDOWS: tuple[tuple[int, float], ...] = ((21, 0.35), (63, 0.35), (252, 0.30))

WINDOW_DESCRIPTORS: tuple[str, ...] = ("beta", "momentum", "residvol", "liquidity")
"""标签里写着 252 日回看的描述子 —— 面板前 ``BETA_WINDOW_DAYS`` 行对它们**无定义**
（见 :func:`descriptors_from_panels` 末段的遮蔽）。其余六个是截面型，逐日独立，不受面板起点影响。"""
LEVERAGE_MAX = 100.0
"""杠杆（MLEV）合理上界；超出即判为脏数据 → NaN 并计入覆盖率。

存在的理由：``balance`` 与 ``valuation`` 若有一侧单位不是元，MLEV 会整体跑偏
（如 1e4 倍）。上界守卫让这种错误**可见**（覆盖率骤降），而不是安静地污染回归。
"""


# ═══════════════════ 截面预处理（逐行 = 逐日） ═══════════════════


def winsorize_rows(M: np.ndarray, k: float = CLIP_MAD_K) -> np.ndarray:
    """逐行（逐日）±k·1.4826·MAD 缩尾。常数列（MAD=0）原样返回。

    用 MAD 而非标准差：标准差本身被极值撑大，拿它当阈值等于「用被污染的尺量自己」。
    """
    A = np.asarray(M, dtype=np.float64)
    if A.ndim != 2 or A.shape[0] == 0:
        return A
    with warnings.catch_warnings():
        # 整行全 NaN（早年某些风格无覆盖）会让 nanmedian 报 All-NaN slice —— 是预期输入
        warnings.simplefilter("ignore", RuntimeWarning)
        med = np.nanmedian(A, axis=1, keepdims=True)
        scale = MAD_TO_SIGMA * np.nanmedian(np.abs(A - med), axis=1, keepdims=True)
    lo = np.where(scale > 0, med - k * scale, -np.inf)
    hi = np.where(scale > 0, med + k * scale, np.inf)
    return np.clip(A, lo, hi)


def zscore_rows(M: np.ndarray) -> np.ndarray:
    """逐行标准化（``ddof=0``，NaN 感知）。该行有效样本标准差为 0 → 整行 NaN。

    「无离散度」与「离散度很小」必须分开：前者没有截面信息（NaN 才对），后者是真信号。
    """
    A = np.asarray(M, dtype=np.float64)
    if A.ndim != 2 or A.shape[0] == 0:
        return A
    n = np.isfinite(A).sum(axis=1, keepdims=True)
    n_safe = np.where(n > 0, n, 1)
    mean = np.where(n > 0, np.nansum(A, axis=1, keepdims=True), np.nan) / n_safe
    dev = A - mean
    # 总体标准差（ddof=0）：与 metrics/residual_ic 的口径一致
    sd = np.sqrt(np.nansum(dev**2, axis=1, keepdims=True) / n_safe)
    with np.errstate(invalid="ignore", divide="ignore"):
        out = dev / np.where(sd > 0, sd, np.nan)
    return out


def blend_z(parts: Sequence[tuple[np.ndarray, float]]) -> np.ndarray:
    """各分量先缩尾 + 逐行 z-score 再加权求和（分量量纲不同，不能直接加权）。

    缩尾在这里统一做：合成前的原始分量常有极端离群（新股换手率、扭亏的增长率），
    它们会把 z-score 的标准差撑大，让其余 99% 的样本挤在一起。

    权重按「有效分量」归一：某分量当日缺失时，用其余分量按比例补足 —— 否则一个
    分量的缺失会把整行拖成 NaN，覆盖率一跌全跌。若全缺则整行 NaN。
    """
    if not parts:
        return np.full((0, 0), np.nan)
    total_w = np.zeros_like(np.asarray(parts[0][0], dtype=np.float64))
    acc = np.zeros_like(total_w)
    have = np.zeros(total_w.shape, dtype=bool)
    for arr, w in parts:
        z = zscore_rows(winsorize_rows(np.asarray(arr, dtype=np.float64)))
        ok = np.isfinite(z)
        acc = np.where(ok, acc + w * np.where(ok, z, 0.0), acc)
        total_w = np.where(ok, total_w + w, total_w)
        have |= ok
    with np.errstate(invalid="ignore", divide="ignore"):
        out = acc / np.where(total_w > 0, total_w, np.nan)
    return np.where(have, out, np.nan)


# ═══════════════════ 时序描述子 ═══════════════════


def log_returns_from_close(close: np.ndarray, max_abs: float = 0.5) -> np.ndarray:
    """对数收益面板。``|r| > max_abs`` 置 NaN —— 数据脏点而非真实价格跳动。

    不做复权处理：调用方给的是 ``daily_forward``（前复权）收盘价。
    停牌导致的行缺失按 0 收益处理（见 :func:`_rolling`）。
    """
    C = np.asarray(close, dtype=np.float64)
    if C.ndim != 2 or C.shape[0] < 2:
        return np.full_like(C, np.nan)
    with np.errstate(invalid="ignore", divide="ignore"):
        r = np.log(C[1:] / C[:-1])
    r[~np.isfinite(r)] = np.nan
    r[np.abs(r) > max_abs] = np.nan
    return np.vstack([np.full((1, C.shape[1]), np.nan), r])


def _rolling(A: np.ndarray, window: int, how: str) -> np.ndarray:
    """滚动窗口聚合的薄封装（``A`` 为 (T, N) 面板，返回同形状）。

    用 pandas 的 Cython 实现：``(T, N)`` 面板上的滚动均值/和/极值都是 O(T·N)，
    自己用 numpy 累加反而更慢。``min_periods`` 取 :data:`MIN_WINDOW_FRACTION` 比例的
    窗口长度 —— 低于它宁可不给数。
    """
    import pandas as pd

    min_periods = max(2, int(window * MIN_WINDOW_FRACTION))
    df = pd.DataFrame(np.asarray(A, dtype=np.float64))
    roll = df.rolling(window, min_periods=min_periods)
    if how == "std":
        # ddof=0：与 zscore_rows / residual_ic 的总体标准差口径一致
        return roll.std(ddof=0).to_numpy()
    out = {"mean": roll.mean, "sum": roll.sum, "max": roll.max, "min": roll.min}[how]()
    return out.to_numpy()


def build_returns(close: np.ndarray) -> np.ndarray:
    """逐日对数收益（首行 NaN）。"""
    return log_returns_from_close(close)


def momentum(close: np.ndarray) -> np.ndarray:
    """``Σ ln(1+r)`` over T−252…T−21（剔除最近 21 日）。"""
    r = log_returns_from_close(close)
    long_leg = _rolling(np.nan_to_num(r, nan=0.0), BETA_WINDOW_DAYS, "sum")
    skip_leg = _rolling(np.nan_to_num(r, nan=0.0), MOMENTUM_SKIP_DAYS, "sum")
    out = long_leg - skip_leg
    # 有效样本太少的行作废：NaN 被按 0 计入了滚动和
    return np.where(np.isfinite(_rolling(r, BETA_WINDOW_DAYS, "mean")), out, np.nan)


def dastd(returns: np.ndarray) -> np.ndarray:
    """252 日收益标准差（等权）。"""
    return _rolling(returns, BETA_WINDOW_DAYS, "std")


def cmra(returns: np.ndarray) -> np.ndarray:
    """252 日累计对数收益路径的极差 ``ln(max) − ln(min)``。

    停牌日（缺行）按 0 收益接续路径 —— 路径连续才谈得上「区间最大回撤」。
    """
    z = np.nancumsum(np.nan_to_num(returns, nan=0.0), axis=0)
    hi = _rolling(z, BETA_WINDOW_DAYS, "max")
    lo = _rolling(z, BETA_WINDOW_DAYS, "min")
    out = hi - lo
    return np.where(np.isfinite(_rolling(returns, BETA_WINDOW_DAYS, "mean")), out, np.nan)


def beta_and_hsigma(
    returns: np.ndarray,
    bench_ret: np.ndarray,
    *,
    window: int = BETA_WINDOW_DAYS,
    halflife: float = BETA_HALFLIFE_DAYS,
) -> tuple[np.ndarray, np.ndarray]:
    """半衰期加权的滚动 beta 与其残差波动 HSIGMA（闭式，无逐股回归）。

    权重只依赖 lag（``w_k = 0.5^(k/halflife)``），故加权一/二阶矩可写成 window 次
    按 lag 错位的矩阵加法 —— 5500 股 × 2588 天的逐股循环回归不可行，这是它的替代。
    残差波动由方差分解给出，不必真的算残差：
    ``hsigma = sqrt(var_r − cov²/var_m)``。

    NaN 感知：逐 (t, 股票) 按「该 lag 上股票与指数同时有效」累计权重，
    有效权重占比低于 :data:`MIN_WINDOW_FRACTION` → NaN。
    """
    R = np.asarray(returns, dtype=np.float64)
    m = np.asarray(bench_ret, dtype=np.float64).ravel()
    if R.ndim != 2 or R.shape[0] != m.size or R.shape[0] == 0:
        return np.full(R.shape, np.nan), np.full(R.shape, np.nan)
    T, N = R.shape
    w = 0.5 ** (np.arange(window, dtype=np.float64) / halflife)
    w_sum = float(w.sum())
    s1 = np.zeros((T, N))
    s_r = np.zeros((T, N))
    s_m = np.zeros((T, N))
    s_rr = np.zeros((T, N))
    s_rm = np.zeros((T, N))
    s_mm = np.zeros((T, N))
    for k in range(window):
        if k >= T:
            break
        r_k = R[: T - k]
        m_k = m[: T - k]
        ok = np.isfinite(r_k) & np.isfinite(m_k)[:, None]
        r0 = np.where(ok, r_k, 0.0)
        m0 = np.where(ok, m_k[:, None], 0.0)
        w_k = w[k]
        s1[k:] += w_k * ok
        s_r[k:] += w_k * r0
        s_m[k:] += w_k * m0
        s_rr[k:] += w_k * r0 * r0
        s_rm[k:] += w_k * r0 * m0
        s_mm[k:] += w_k * m0 * m0
    return _solve_beta(s1, s_r, s_m, s_rr, s_rm, s_mm, w_sum)


def _solve_beta(s1, s_r, s_m, s_rr, s_rm, s_mm, w_sum):
    """由加权矩解出 beta 与 hsigma（与 :func:`beta_and_hsigma` 拆开只为控制函数长度）。"""
    with np.errstate(invalid="ignore", divide="ignore"):
        s1_safe = np.where(s1 > 0, s1, np.nan)
        var_m = s_mm - s_m**2 / s1_safe
        var_r = s_rr - s_r**2 / s1_safe
        cov = s_rm - s_r * s_m / s1_safe
        beta = cov / np.where(var_m > 0, var_m, np.nan)
        # 三个矩都是 **未归一** 的 Σw·(·)，残差方差要除以 Σw 才是方差本身；
        # 少这一步得到的是 sqrt(Σw)·σ（≈9.5 倍），量纲错但数值「看起来也像波动率」。
        resid_var = (var_r - cov**2 / np.where(var_m > 0, var_m, np.nan)) / s1_safe
    hsigma = np.sqrt(np.maximum(resid_var, 0.0))
    enough = s1 >= MIN_WINDOW_FRACTION * w_sum
    return np.where(enough, beta, np.nan), np.where(enough, hsigma, np.nan)


# ═══════════════════ 截面描述子 ═══════════════════


def size_exposure(total_mv: np.ndarray) -> np.ndarray:
    """``ln(total_mv)``；非正市值（实测存在精确 0 的格子）→ NaN。

    不能先取对数再过滤：``ln(0) = -inf`` 会污染整行 z-score（-inf 不是缺失，
    ``nanmedian`` 也拦不住它）。见记忆「valuation 市值零值」。
    """
    mv = np.asarray(total_mv, dtype=np.float64)
    return np.where(mv > 0, np.log(np.where(mv > 0, mv, 1.0)), np.nan)


def btop_exposure(pb: np.ndarray) -> np.ndarray:
    """``1/pb``；pb ≤ 0（净资产为负）→ NaN（负的账面市值比没有「便宜」的含义）。"""
    x = np.asarray(pb, dtype=np.float64)
    return np.where(x > 0, 1.0 / np.where(x > 0, x, 1.0), np.nan)


def earnings_yield(net_profit_ttm: np.ndarray, total_mv: np.ndarray) -> np.ndarray:
    """``net_profit_ttm / total_mv``（同量纲，元/元）。市值非正 → NaN。"""
    p = np.asarray(net_profit_ttm, dtype=np.float64)
    mv = np.asarray(total_mv, dtype=np.float64)
    return np.where(mv > 0, p / np.where(mv > 0, mv, 1.0), np.nan)


def growth(revenue_ttm: np.ndarray, net_profit_ttm: np.ndarray, lag: int = 252) -> np.ndarray:
    """营收 TTM 与净利 TTM 的同比（各 z 后等权合成），滞后由 ``lag`` 给定（交易日）。

    用 ``valuation`` 自带的 TTM 列做同比、不另接 ``income`` 的 PIT 拼接：TTM 列本身
    已是「当日可得」的口径，而 ``income`` 的四季度求和在中报季逐票损坏（既有实测）。
    分母 ≤ 0（亏损或负营收）→ 该分量 NaN（增长率符号在此处无意义）。
    """
    rev = np.asarray(revenue_ttm, dtype=np.float64)
    npf = np.asarray(net_profit_ttm, dtype=np.float64)
    # 亏损转盈利这类跨零变化会让增长率爆量级 → 缩尾交给 blend_z 统一处理
    return blend_z([(_yoy(rev, lag), 0.5), (_yoy(npf, lag), 0.5)])


def _yoy(A: np.ndarray, lag: int) -> np.ndarray:
    """``A[t]/A[t-lag] − 1``；分母 ≤ 0 或滞后行不存在 → NaN。"""
    X = np.asarray(A, dtype=np.float64)
    if X.ndim != 2 or X.shape[0] <= lag:
        return np.full(X.shape if X.ndim == 2 else (0, 0), np.nan)
    prev = np.vstack([np.full((lag, X.shape[1]), np.nan), X[:-lag]])
    with np.errstate(invalid="ignore", divide="ignore"):
        out = X / np.where(prev > 0, prev, np.nan) - 1.0
    return np.where(np.isfinite(out), out, np.nan)


def liquidity(volume: np.ndarray, circulating_capital: np.ndarray) -> np.ndarray:
    """换手率的三窗口合成：``0.35·z(STOM21) + 0.35·z(STOQ63) + 0.30·z(STOA252)``。

    换手 = 日成交量 / 流通股本（源单位均为**股**，量纲自洽）。
    """
    vol = np.asarray(volume, dtype=np.float64)
    cap = np.asarray(circulating_capital, dtype=np.float64)
    turn = np.where(cap > 0, vol / np.where(cap > 0, cap, 1.0), np.nan)
    parts = [(_rolling(turn, win, "mean"), w) for win, w in LIQUIDITY_WINDOWS]
    return blend_z(parts)


def leverage(total_mv: np.ndarray, long_term_debt: np.ndarray) -> np.ndarray:
    """``MLEV = (ME + LD) / ME``，超出 :data:`LEVERAGE_MAX` 判为脏数据 → NaN。"""
    mv = np.asarray(total_mv, dtype=np.float64)
    ld = np.asarray(long_term_debt, dtype=np.float64)
    with np.errstate(invalid="ignore", divide="ignore"):
        out = (mv + ld) / np.where(mv > 0, mv, np.nan)
    return np.where((out > 0) & (out <= LEVERAGE_MAX), out, np.nan)


def rmw_exposure(deducted_net_profit_ttm: np.ndarray, book_equity: np.ndarray) -> np.ndarray:
    """``扣非净利TTM / 净资产``（扣非 ROE 口径的 RMW，用户 2026-09-22 指定）。

    净资产 ≤ 0（资不抵债）→ NaN：负分母会让「亏损但净资产为负」的票在比率上
    翻正，得到一个假的「高盈利」。分子为负（真亏损）**保留**——那是低盈利暴露的
    真实取值，FF5 的 RMW 多空腿本来就靠它。
    """
    p = np.asarray(deducted_net_profit_ttm, dtype=np.float64)
    be = np.asarray(book_equity, dtype=np.float64)
    return np.where(be > 0, p / np.where(be > 0, be, 1.0), np.nan)


def cma_exposure(capex_ttm: np.ndarray, total_assets: np.ndarray) -> np.ndarray:
    """``−资本开支TTM / 总资产``（用户指定口径；负号使高暴露 = 投资保守，同 FF5 CMA 方向）。

    学术 FF5 用资本开支的**增速**；这里按用户要求改用强度（水平比值）——
    增速对低基数公司爆量级、对一次性大额投资过度敏感，水平比值稳健得多。
    总资产 ≤ 0 → NaN。
    """
    cx = np.asarray(capex_ttm, dtype=np.float64)
    ta = np.asarray(total_assets, dtype=np.float64)
    with np.errstate(invalid="ignore", divide="ignore"):
        out = -cx / np.where(ta > 0, ta, np.nan)
    return np.where(np.isfinite(out), out, np.nan)


def descriptors_from_panels(
    panels: dict[str, np.ndarray],
    bench_ret: np.ndarray,
) -> dict[str, np.ndarray]:
    """把原始数据面板拼成风格描述子（**未标准化**，标准化见 :func:`standardize_pipeline`）。

    Args:
        panels: ``total_mv / float_mv / pb / net_profit_ttm / revenue_ttm /
            circulating_capital / volume / close`` 与 ``long_term_debt``，
            全部是 (T, N) 面板（N 为股票，列顺序在所有面板间一致）。
            另有两组**可选**财务面板：``deducted_net_profit_ttm + book_equity``（出 rmw）
            与 ``capex_ttm + total_assets``（出 cma）——缺任一键则该风格整体不出，
            返回值退化为 10 风格（老数据/老测试路径不受影响）。
        bench_ret: 长度 T 的指数日收益（与面板日期对齐）。
    """
    close = panels["close"]
    total_mv = panels["total_mv"]
    size = size_exposure(total_mv)
    ret = build_returns(close)
    beta, hsigma = beta_and_hsigma(ret, bench_ret)
    w = dict(RESIDVOL_WEIGHTS)
    raw = {
        "size": size,
        "beta": beta,
        "momentum": momentum(close),
        "residvol": blend_z([(dastd(ret), w["dastd"]), (cmra(ret), w["cmra"]), (hsigma, w["hsigma"])]),
        "nlsize": size**3,
        "btop": btop_exposure(panels["pb"]),
        "liquidity": liquidity(panels["volume"], panels["circulating_capital"]),
        "earningsyield": earnings_yield(panels["net_profit_ttm"], total_mv),
        "growth": growth(panels["revenue_ttm"], panels["net_profit_ttm"]),
        "leverage": leverage(total_mv, panels["long_term_debt"]),
    }
    if "deducted_net_profit_ttm" in panels and "book_equity" in panels:
        raw["rmw"] = rmw_exposure(panels["deducted_net_profit_ttm"], panels["book_equity"])
    if "capex_ttm" in panels and "total_assets" in panels:
        raw["cma"] = cma_exposure(panels["capex_ttm"], panels["total_assets"])
    # 面板前 252 行没有完整的回看窗口。不遮蔽的话：momentum 用 0 补缺失（得到一个
    # 「标签写 252 日、实际只有几十日」的数）、beta/residvol 靠覆盖率判据在 t≈150 就出数、
    # liquidity 只剩 21/63 日分量 —— 三者都与标签口径不符，且与后面的值不可比。
    # 报告窗口通常在面板起点之后，代价为零；短区间构建则诚实少一段，不伪造。
    ramp = np.arange(len(close)) < BETA_WINDOW_DAYS
    for k in WINDOW_DESCRIPTORS:
        raw[k] = np.where(ramp[:, None], np.nan, raw[k])
    return raw


def standardize_pipeline(
    raw: dict[str, np.ndarray],
    ind_codes: Sequence[str],
) -> dict[str, np.ndarray]:
    """逐日标准化流水线：缩尾 → z → 对 size 正交 → 行业去均值 → 重新标准化。

    ⚠️ 行业去均值会**部分恢复**与 size 的相关（投影不再正交于 size）——这是本口径
    的已知近似，由 ``meta.json`` 里的 ``size_corr_residual`` 实测值披露，不假装为零。

    Args:
        raw: (T, N) 描述子面板（未标准化）。
        ind_codes: 长度 N 的行业代码（**按股票**，逐日恒定 —— 行业映射是静态快照）。
    """
    z = {k: zscore_rows(winsorize_rows(v)) for k, v in raw.items()}
    size_col = z.get("size")
    if size_col is not None:
        for k in SIZE_ORTHOGONAL_STYLES:
            if k in z:
                z[k] = NZ.orthogonalize_rows(z[k], size_col)
    codes = np.asarray(list(ind_codes), dtype=object)
    out = {}
    for k, v in z.items():
        # 转置后调用：industry_demean 沿 axis=0 分组，而行业按**股票**分组
        demeaned = NZ.industry_demean(np.ascontiguousarray(v.T), codes).T
        out[k] = zscore_rows(demeaned)
    return out


def size_residual_corr(exposures: dict[str, np.ndarray]) -> dict[str, float | None]:
    """逐风格报出「标准化后仍与 size 的平均截面相关」—— 对 :func:`standardize_pipeline`
    那道已知近似的**实测披露**（行业去均值会把一部分 size 分量还回来）。
    取不到任何有效截面（全 NaN）时给 ``None``，**不给 0**（0 会被读成「完全正交」）。"""
    size = np.asarray(exposures["size"], dtype=np.float64)
    out: dict[str, float | None] = {}
    for k in (s for s in STYLE_NAMES if s in exposures):
        v = np.asarray(exposures[k], dtype=np.float64)
        ok = np.isfinite(v) & np.isfinite(size)
        n = np.maximum(ok.sum(axis=1), 1)
        a = np.where(ok, size, 0.0)
        b = np.where(ok, v, 0.0)
        da = np.where(ok, size - (a.sum(axis=1) / n)[:, None], 0.0)
        db = np.where(ok, v - (b.sum(axis=1) / n)[:, None], 0.0)
        denom = np.sqrt((da**2).sum(axis=1) * (db**2).sum(axis=1))
        with np.errstate(invalid="ignore", divide="ignore"):
            cc = np.where(denom > 0, (da * db).sum(axis=1) / np.where(denom > 0, denom, 1.0), np.nan)
        out[k] = round(float(np.nanmean(cc)), 4) if np.isfinite(cc).any() else None
    return out


# ═══════════════════ 纯因子收益（WLS） ═══════════════════


def pure_factor_returns(
    exposures: dict[str, np.ndarray],
    ind_codes: Sequence[str],
    fwd_ret: np.ndarray,
    weights: np.ndarray,
) -> tuple[np.ndarray, dict[str, np.ndarray]]:
    """横截面 WLS 解 ``r = Σ X_k f_k + Σ D_j d_j + ε``，返回 (T, K) 的纯因子收益。

    权重 = ``√clip(float_mv)``。行业哑变量与截距共线（哑变量和为 1），用
    ``lstsq`` 的 SVD 最小范数解处理，不手删基准组（删哪一组是任意的，结果不可比）。

    某股票当日只要**任一风格暴露 / 行业 / 权重**缺失就整只剔除 —— 混合填充会
    让「纯」因子收益里混进填充值，而填充的正是缺失最严重的股票。剔除比例写入
    ``returns.parquet`` 的 ``n_used`` / ``n_universe`` 两列备查。

    解的风格集 = ``exposures`` 实际含有的 :data:`STYLE_NAMES` 子集：财务面板缺失时
    rmw/cma 不在字典里，模型就退化为 10 风格（而不是把整列 NaN 塞进回归 ——
    那会把**每一天**的样本全部剔空，产出整段空区间）。

    Returns:
        ``(pure, diag)``：``pure`` 是 (T, K) 的纯因子收益（样本不足的日全 NaN）；
        ``diag`` 含逐日 ``n_used`` / ``n_universe``，用于把「剔除比例」写进产物备查。
    """
    T = _first_shape(exposures)
    styles = [s for s in STYLE_NAMES if s in exposures]
    codes = np.asarray(list(ind_codes), dtype=object)
    out = np.full((T, len(styles)), np.nan)
    used = np.zeros(T, dtype=int)
    universe = np.zeros(T, dtype=int)
    for t in range(T):
        f, n_used, n_uni = _solve_one_day(exposures, styles, codes, fwd_ret, weights, t)
        if f is not None:
            out[t] = f
        used[t] = n_used
        universe[t] = n_uni
    return out, {"n_used": used, "n_universe": universe, "styles": styles}


def _first_shape(exposures: dict[str, np.ndarray]) -> int:
    for v in exposures.values():
        return int(np.asarray(v).shape[0])
    return 0


def _solve_one_day(exposures, styles, codes, fwd_ret, weights, t: int):
    """单日 WLS。返回 ``(纯因子收益 | None, 有效数, 当日全集数)``。"""
    y = np.asarray(fwd_ret, dtype=np.float64)[t]
    w = np.asarray(weights, dtype=np.float64)[t]
    X = np.column_stack([np.asarray(exposures[s], dtype=np.float64)[t] for s in styles])
    n_universe = int(np.isfinite(y).sum())
    ok = np.isfinite(y) & np.isfinite(w) & (w > 0)
    for j in range(X.shape[1]):
        ok &= np.isfinite(X[:, j])
    ok &= np.array([c is not None and str(c) != "nan" for c in codes])
    n_used = int(ok.sum())
    if n_used < len(styles) + 2:
        return None, n_used, n_universe
    code_u, inv = np.unique(codes[ok], return_inverse=True)
    D = np.zeros((n_used, code_u.size))
    D[np.arange(n_used), inv] = 1.0
    A = np.hstack([X[ok], D])
    sw = np.sqrt(w[ok])
    try:
        sol, *_ = np.linalg.lstsq(A * sw[:, None], y[ok] * sw, rcond=None)
    except np.linalg.LinAlgError:
        return None, n_used, n_universe
    return sol[: len(styles)], n_used, n_universe


# ─────────────────────────── 产物读取（读时消费方）───────────────────────────

STYLE_LABELS: dict[str, str] = {
    "size": "规模",
    "beta": "贝塔",
    "momentum": "动量",
    "residvol": "残差波动",
    "nlsize": "非线性规模",
    "btop": "账面市值比",
    "liquidity": "流动性",
    "earningsyield": "盈利收益",
    "growth": "成长",
    "leverage": "杠杆",
    "rmw": "扣非ROE",
    "cma": "投资保守",
}
"""风格中文名（前端表头用）。与 :data:`STYLE_NAMES` 必须同键 —— 缺一个就
在页面显示成英文标识，故由 :func:`label_of` 兜底而不是直接下标。"""


def label_of(style: str) -> str:
    return STYLE_LABELS.get(str(style), str(style))


def style_factors_dir() -> Path:
    """风格产物根目录（经 ``quantdb_paths`` 解析，禁硬编码）。"""
    from backend.shared.quantdb_paths import resolve_quantdb_subdir

    return Path(resolve_quantdb_subdir("5_technical_derived", "style_factors"))


def load_pure_returns(horizon: int = 1) -> dict[int, dict[str, float]] | None:
    """读某前瞻期的纯因子收益长表 → ``{dt_int: {style: 日收益}}``。

    产物缺失/该 horizon 无行 → ``None``（调用方降级，**不写 0**：全 0 的风格收益
    会让归因回归得出「α 等于全部超额」这种看似漂亮的假结论）。
    """
    import pyarrow.parquet as pq

    path = style_factors_dir() / "returns.parquet"
    if not path.exists():
        return None
    try:
        df = pq.read_table(path, filters=[("horizon", "=", int(horizon))]).to_pandas()
    except Exception as e:  # noqa: BLE001 — 风格产物坏了不该拖垮整个详情接口
        import logging

        logging.getLogger(__name__).warning("风格纯因子收益读取失败（%s）：%s", path, e)
        return None
    if df.empty:
        return None
    out: dict[int, dict[str, float]] = {}
    for row in df.itertuples(index=False):
        rec = {}
        for s in STYLE_NAMES:
            v = getattr(row, s, None)
            rec[s] = float(v) if v is not None and np.isfinite(v) else float("nan")
        out[int(row.dt)] = rec
    return out
