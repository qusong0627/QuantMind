"""中性化原语：数学不变量 + **与冻结旧实现的逐位对拍**。

收口重构（把 ``factor_deep_dive.neutralized_ic`` 与
``evaluate_signal_factors --neutral`` 的实现换成共享模块）不允许改变任何口径，
所以本文件里冻结了一份**旧代码的逐行转录**作为 oracle：新实现必须与它同输入同输出。

数值口径：旧实现用 float32 哑变量矩阵、本实现用 float64。0/1 哑变量在 float64 里
可精确表示，故累积结果应当逐位一致；断言仍用 1e-12 的相对容差以防平台差异。
"""

from __future__ import annotations

import numpy as np
import pytest

from backend.services.engine.factor_report import neutralize as NZ


# ═══════════════ 冻结旧实现（factor_deep_dive.py:212-242 逐行转录）═══════════════


def _legacy_neutral_ic(M: np.ndarray, yy: np.ndarray, inds: np.ndarray, mv_rank: np.ndarray) -> np.ndarray:
    codes, inv = np.unique(inds, return_inverse=True)
    n_ind = len(codes)
    Dm = np.zeros((len(inds), n_ind), dtype=np.float32)
    Dm[np.arange(len(inds)), inv] = 1.0
    cnt = Dm.sum(axis=0)
    cnt[cnt == 0] = 1.0
    ind_mean = (Dm.T @ M) / cnt[:, None]
    M = M - Dm @ ind_mean
    mvc = mv_rank - mv_rank.mean()
    denom = float(mvc @ mvc)
    if denom <= 0:
        return None
    beta = (mvc @ M) / denom
    resid = M - np.outer(mvc, beta)
    yv = yy - yy.mean()
    sd_y = yv.std()
    sd_r = resid.std(axis=0)
    with np.errstate(invalid="ignore"):
        return (resid * yv[:, None]).mean(axis=0) / (sd_r * sd_y)


# ═══════════════ 1. 数学不变量 ═══════════════


def test_行业去均值_组内均值严格归零():
    rng = np.random.default_rng(101)
    n, k = 600, 4
    inds = np.array([f"IND{i % 7}" for i in range(n)])
    M = rng.normal(0, 1, (n, k))
    out = NZ.industry_demean(M, inds)
    for code in np.unique(inds):
        m = inds == code
        assert np.allclose(out[m].mean(axis=0), 0.0, atol=1e-12)


def test_行业去均值_只有一个行业时等于全局去均值():
    rng = np.random.default_rng(103)
    M = rng.normal(0, 1, (100, 3))
    out = NZ.industry_demean(M, np.array(["ONLY"] * 100))
    assert np.allclose(out, M - M.mean(axis=0, keepdims=True), atol=1e-12)


def test_正交后残差与控制变量内积为零():
    rng = np.random.default_rng(107)
    M = rng.normal(0, 1, (500, 3))
    c = rng.normal(0, 1, 500)
    resid = NZ.orthogonalize_to(M, c)
    cc = c - c.mean()
    assert np.allclose(cc @ resid, 0.0, atol=1e-9)


def test_纯行业因子_残差恒为零_IC_必须不可定义而非_0():
    """因子在每个行业内取常数 → 行业去均值后信息量为 0。

    此时**正确输出是 NaN（无信息可测）而不是 0**（0 会被读成「测过，没效果」）。
    """
    n = 300
    inds = np.array([f"IND{i % 5}" for i in range(n)])
    level = {f"IND{i}": float(i) for i in range(5)}
    x = np.array([level[c] for c in inds], dtype=float)
    y = x * 1.5 + 0.3  # 收益完全由行业解释
    resid = NZ.industry_demean(x.reshape(-1, 1), inds)
    assert np.allclose(resid, 0.0, atol=1e-12)
    assert np.isnan(NZ.residual_ic(resid, y)[0])


def test_与市值完全共线的因子_正交后残差为_NaN_而非浮点残渣():
    """x = a·mv_rank + b 与市值完全共线 → **返回 NaN**，不是 1e-13 的浮点残渣。

    这是 `COLLINEAR_TOL` 存在的理由：残渣「非零但无信息」，下游 `残渣/残渣` 会算出
    ±1 量级的**任意**相关系数（报告里看起来像「极端强因子」）。判据必须相对**原列**
    方差（这里 ss_res/ss_tot ≈ 1e-31），残差自身的量级缩放下认不出来。
    """
    rng = np.random.default_rng(109)
    mv = rng.uniform(0, 1, 400)
    x = (3.0 * mv + 7.0).reshape(-1, 1)
    resid = NZ.orthogonalize_to(x, mv)
    assert np.isnan(resid).all(), "共线列必须整列作废，不能返回浮点残渣"
    y = rng.normal(0, 0.02, 400)
    assert np.isnan(NZ.residual_ic(resid, y)[0])


def test_共线守卫不得误伤_弱但真实_的因子():
    """与市值相关 0.99（残留 14% 方差）是**真实信号**，守卫必须放行。

    守卫的误伤形态最危险：把「弱因子」静默变成「无定义」，报告上比 0 还难发现。
    """
    rng = np.random.default_rng(211)
    n = 800
    mv = rng.uniform(0, 1, n)
    e = rng.normal(0, 1, n)
    x = (mv + 0.14 * e).reshape(-1, 1)          # 残差方差占比 ~2% >> 1e-20
    resid = NZ.orthogonalize_to(x, mv)
    assert np.isfinite(resid).all()
    assert resid.std() > 0.1 * x.std()
    y = e + rng.normal(0, 0.1, n)
    assert NZ.residual_ic(resid, y)[0] > 0.7     # 残差里的信号照常被看见


def test_共线守卫_向量化形态与逐日形态同判():
    """逐行形态必须与逐日调用同一判据：共线的那一行全 NaN，其余行照常。"""
    mv = np.linspace(0.0, 1.0, 100)
    M = np.vstack([3.0 * mv + 7.0, np.random.default_rng(223).normal(0, 1, 100)])
    resid = NZ.orthogonalize_rows(M, np.vstack([mv, mv]))
    assert np.isnan(resid[0]).all()              # 共线行整行作废
    assert np.isfinite(resid[1]).all()           # 另一行不受影响


def test_控制变量零方差时原样返回而非报错():
    M = np.array([[1.0, 2.0], [3.0, 4.0]])
    assert np.allclose(NZ.orthogonalize_to(M, np.array([5.0, 5.0])), M)


def test_rank_pct_与_pandas_rank_同口径():
    pd = pytest.importorskip("pandas")
    rng = np.random.default_rng(113)
    M = rng.normal(0, 1, (200, 3))
    got = NZ.rank_pct(M)
    want = pd.DataFrame(M).rank(pct=True).to_numpy()
    assert np.allclose(got, want, atol=1e-12)


# ═══════════════ 2. 收口回归：与冻结旧实现逐位一致 ═══════════════


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_收口回归_行业加市值中性化逐位一致(seed):
    rng = np.random.default_rng(seed)
    n, k = 800, 6
    inds = np.array([f"IND{int(i)}" for i in rng.integers(0, 12, n)])
    M = rng.normal(0, 1, (n, k))
    mv_rank = rng.uniform(0, 1, n)
    y = M[:, 0] * 0.3 + mv_rank * 0.1 + rng.normal(0, 1, n)

    got = NZ.neutralize_ic(M, y, ind_codes=inds, control=mv_rank)
    want = _legacy_neutral_ic(M, y, inds, mv_rank)
    assert want is not None
    assert np.allclose(got, want, rtol=1e-12, atol=1e-15)


def test_行业去均值_NaN_不污染同行业其他股票():
    """旧实现 `Dᵀ@M` 会把一个 NaN 扩散成**整个行业整列**的 NaN。

    这条断言是 `ic_neutral_days` 恒为 1 那个缺陷的守卫：有缺失的日子必须照样出数。
    """
    inds = np.array(["A", "A", "A", "B", "B"])
    M = np.array([[1.0], [np.nan], [3.0], [10.0], [20.0]])
    out = NZ.industry_demean(M, inds)
    assert out[0, 0] == pytest.approx(-1.0)      # A 行业有效均值 = (1+3)/2 = 2
    assert out[2, 0] == pytest.approx(1.0)
    assert np.isnan(out[1, 0])                   # 缺失位仍是缺失
    assert out[3, 0] == pytest.approx(-5.0)      # B 行业不受 A 的缺失影响


def test_行业去均值_某格全缺只作废那一格():
    """「A 行业第一列全缺」只让那两行变 NaN，不得连累 B 行业或第二列。"""
    inds = np.array(["A", "A", "B", "B"])
    M = np.array([[np.nan, 1.0], [np.nan, 3.0], [1.0, 5.0], [3.0, 7.0]])
    out = NZ.industry_demean(M, inds)
    assert np.isnan(out[:2, 0]).all()            # A 的 0 列全缺 → 该格 NaN
    assert out[2, 0] == pytest.approx(-1.0)      # B 的 0 列照常（1,3 → 均值 2）
    assert out[3, 0] == pytest.approx(1.0)
    assert out[0, 1] == pytest.approx(-1.0)      # 第二列照常（1,3 → 均值 2）
    assert out[2, 1] == pytest.approx(-1.0)


def test_正交_NaN位不参与beta估计():
    """beta 必须只用「因子与市值同时有效」的样本；缺失位残差为 NaN。"""
    rng = np.random.default_rng(307)
    n = 200
    x = rng.normal(0, 1, n)
    c = rng.uniform(0, 1, n)
    x[rng.random(n) < 0.2] = np.nan
    c[rng.random(n) < 0.1] = np.nan
    ok = np.isfinite(x) & np.isfinite(c)
    got = NZ.orthogonalize_to(x.reshape(-1, 1), c).ravel()
    # 与实现同口径的显式重算（残差只定到相差一个常数，故必须用同一个中心化常数）
    cc = c - np.nanmean(c)
    beta = (cc[ok] @ x[ok]) / (cc[ok] ** 2).sum()
    assert np.allclose(got[ok], x[ok] - beta * cc[ok], rtol=1e-10)
    assert np.isnan(got[~ok]).all()
    # 口径本身的判据：有效子集上残差与市值正交
    assert abs(float(cc[ok] @ got[ok])) < 1e-8 * (cc[ok] ** 2).sum()


def test_残差IC_缺失位不参与且不整列作废():
    rng = np.random.default_rng(311)
    n = 300
    r = rng.normal(0, 1, (n, 2))
    y = r[:, 0] * 0.5 + rng.normal(0, 0.5, n)     # 第一列有真实相关
    r[rng.random((n, 2)) < 0.15] = np.nan
    ic = NZ.residual_ic(r, y)
    assert np.isfinite(ic[0]) and ic[0] > 0.3      # 有缺失也照样出数
    assert np.isfinite(ic[1])


def test_残差IC_收益含inf不被污染():
    """`fwd_ret` 由 close[t+h]/close[t]-1 得来，close[t]=0 时是 inf。

    `np.nanmean` 会把 inf 算进均值 → 整个 y 变 inf → 逐列相减出 NaN → 一条脏数据
    废掉当天所有因子。均值必须只取有效样本。
    """
    rng = np.random.default_rng(313)
    n = 300
    r = rng.normal(0, 1, (n, 3))
    y = r[:, 0] * 0.5 + rng.normal(0, 0.5, n)
    y[7] = np.inf
    y[9] = -np.inf
    ic = NZ.residual_ic(r, y)
    assert np.isfinite(ic).all(), f"inf 污染了残差 IC: {ic}"
    assert ic[0] > 0.3


def test_残差IC_收益全无效返回全NaN():
    r = np.random.default_rng(317).normal(0, 1, (50, 2))
    assert np.isnan(NZ.residual_ic(r, np.full(50, np.nan))).all()


def test_rank_pct_NaN_保持为NaN而非排到最大():
    """argsort 会把 NaN 排到末尾 —— 照抄就会得出「最缺失 = 最强因子」的反向结论。"""
    pd = pytest.importorskip("pandas")
    M = np.array([[3.0, np.nan], [1.0, 5.0], [np.nan, 1.0], [2.0, 3.0]])
    got = NZ.rank_pct(M)
    want = pd.DataFrame(M).rank(pct=True).to_numpy()
    assert np.isnan(got[2, 0]) and np.isnan(got[0, 1])
    assert np.allclose(got, want, atol=1e-12, equal_nan=True)


def test_向量化正交与逐日正交_IC_一致():
    """``orthogonalize_rows``（evaluate_signal_factors 的网格形态）必须与
    逐日调用 ``orthogonalize_to`` 得到同一个残差 IC —— 两种执行形状、一份口径。"""
    rng = np.random.default_rng(211)
    T, N = 40, 120
    M = rng.normal(0, 1, (T, N))
    M[rng.random((T, N)) < 0.05] = np.nan          # 随机缺失
    C = rng.uniform(0, 1, (T, N))
    C[rng.random((T, N)) < 0.03] = np.nan
    Y = rng.normal(0, 0.02, (T, N))

    vec = NZ.orthogonalize_rows(M, C)
    for t in range(T):
        row = M[t]
        ctl = C[t]
        ok = np.isfinite(row) & np.isfinite(ctl)
        if ok.sum() < 3:
            continue
        per_day = NZ.orthogonalize_to(row[ok].reshape(-1, 1), ctl[ok]).ravel()
        got = NZ.residual_ic(vec[t][ok].reshape(-1, 1), Y[t][ok])[0]
        want = NZ.residual_ic(per_day.reshape(-1, 1), Y[t][ok])[0]
        if np.isfinite(want):
            assert got == pytest.approx(want, rel=1e-10, abs=1e-14)
        else:
            assert not np.isfinite(got)


def test_向量化正交_缺失位保持缺失():
    """有效位给出残差、原始缺失位仍是 NaN。

    ⚠️ 样例必须有 **≥3 个有效点**：只有 2 个有效点时「截距 + 斜率」两个参数恰好把
    任意两点拟合干净，残差恒为 0 —— 守卫会（正确地）判为共线并整行作废。
    该性质单列一条断言在下面。
    """
    M = np.array([[1.0, 2.0, 3.0, np.nan], [2.0, 3.0, 4.0, 5.0]])
    C = np.array([[1.0, 5.0, 2.0, 3.0], [3.0, 2.0, 1.0, 4.0]])
    out = NZ.orthogonalize_rows(M, C)
    assert np.isnan(out[0, 3])          # 原始缺失不因正交被"补上"
    assert np.isfinite(out[0, 0])


def test_两点有效必判共线_与逐日形态同判():
    """只有 2 个有效点时两点确定一条直线 → 残差恒 0 → NaN（不是「数值太小的 bug」）。"""
    M = np.array([[1.0, 2.0, np.nan]])
    C = np.array([[1.0, 2.0, 3.0]])
    assert np.isnan(NZ.orthogonalize_rows(M, C)).all()
    assert np.isnan(NZ.orthogonalize_to(M.ravel()[:2].reshape(-1, 1), C.ravel()[:2])).all()


def test_收口回归_仅市值中性化的单变量路径一致():
    """``evaluate_signal_factors --neutral`` 只正交市值，不碰行业。"""
    rng = np.random.default_rng(7)
    n = 500
    x = rng.normal(0, 1, n)
    mv_rank = rng.uniform(0, 1, n)
    y = rng.normal(0, 0.02, n)

    got = NZ.neutralize_ic(x.reshape(-1, 1), y, control=mv_rank)[0]
    want = NZ.residual_ic(NZ.orthogonalize_to(x.reshape(-1, 1), mv_rank), y)[0]
    assert got == pytest.approx(want, rel=1e-15)


# ═══════════════ 3. 零项守卫 ═══════════════


def test_零项守卫_空与形状不符():
    assert NZ.industry_demean(np.zeros((0, 3)), np.array([])).shape == (0, 3)
    assert NZ.orthogonalize_to(np.zeros((0, 3)), np.array([])).shape == (0, 3)
    out = NZ.residual_ic(np.zeros((0, 3)), np.array([]))
    assert out.shape == (3,) and np.isnan(out).all()
    # 长度不匹配（残差行数 ≠ y 长度）必须返回 NaN 而不是广播出错误结果
    assert np.isnan(NZ.residual_ic(np.zeros((10, 2)), np.zeros(9))).all()


def test_残差_IC_全常数收益返回_NaN():
    rng = np.random.default_rng(127)
    M = rng.normal(0, 1, (300, 2))
    assert np.isnan(NZ.residual_ic(M, np.full(300, 0.01))).all()
