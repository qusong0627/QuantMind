"""组合权重与合成 IC：**与冻结旧实现的逐位对拍** + 显式分歧清单。

对拍对象是 ``portfolio.py:142-157`` 与 ``factor_deep_dive.py:414-424`` 的内联实现
（两处逐字相同，此处冻结一份）。收口重构不得改变口径，除非是文件顶部列出的
三处「旧实现静默产出垃圾」的有意分歧 —— 那三处各有独立测试，且**只在退化输入上触发**。
"""

from __future__ import annotations

import numpy as np
import pytest

from backend.services.engine.factor_report import optimize as OPT


# ═══════════════ 冻结旧实现（portfolio.py:142-157 逐行转录）═══════════════


def _legacy_weights(Sigma: np.ndarray, ic_mean: np.ndarray, shrink: float = 0.3) -> np.ndarray:
    sign = np.array([1.0 if float(v) >= 0 else -1.0 for v in ic_mean])
    mu = np.array([abs(float(v)) for v in ic_mean])
    S = (1 - shrink) * Sigma + shrink * np.eye(len(ic_mean))
    try:
        w = np.linalg.solve(S, mu * sign)
    except np.linalg.LinAlgError:
        w = mu * sign
    if not np.isfinite(w).all() or np.abs(w).sum() <= 0:
        w = mu * sign
    return sign * (np.abs(w) / np.abs(w).sum())      # 方向符号 + gross=1 归一


def _corr_matrix(rng, k: int) -> np.ndarray:
    """随机相关矩阵（对角恰为 1、对称正定），模拟真实的逐日 IC 相关矩阵。"""
    A = rng.normal(size=(k, k))
    S = (A @ A.T) / k + np.eye(k) * 0.1
    d = np.sqrt(np.diag(S))
    S = S / np.outer(d, d)
    np.fill_diagonal(S, 1.0)
    return S


# ═══════════════ 1. 收口回归：逐位对拍 ═══════════════


@pytest.mark.parametrize("seed", [11, 22, 33])
@pytest.mark.parametrize("k", [3, 8, 20])
def test_收口回归_与冻结旧实现逐位一致(seed, k):
    rng = np.random.default_rng(seed)
    Sigma = _corr_matrix(rng, k)
    ic_mean = rng.normal(0, 0.02, k)          # 正负混合，覆盖 sign 分支
    got = OPT.max_icir_weights(Sigma, ic_mean)["weights"]
    assert np.allclose(got, _legacy_weights(Sigma, ic_mean), rtol=1e-15, atol=0)


def test_收口回归_奇异矩阵回退路径一致():
    """Σ 全 1（秩 1）且不收缩 → solve 抛 LinAlgError → 两边都退化为 |μ|·sign 归一。"""
    k = 5
    rng = np.random.default_rng(5)
    Sigma = np.ones((k, k))
    ic_mean = rng.normal(0, 0.01, k)
    got = OPT.max_icir_weights(Sigma, ic_mean, shrink=0.0)
    assert got["fallback"] is True
    assert np.allclose(got["weights"], _legacy_weights(Sigma, ic_mean, shrink=0.0), rtol=1e-15)


# ═══════════════ 2. 口径不变量 ═══════════════


@pytest.mark.parametrize("k", [1, 4, 13])
def test_gross_恒为_1(k):
    rng = np.random.default_rng(77)
    r = OPT.max_icir_weights(_corr_matrix(rng, k), rng.normal(0, 0.02, k))
    assert np.abs(r["weights"]).sum() == pytest.approx(1.0, rel=1e-12)


def test_方向符号跟随_ic_mean_的符号():
    rng = np.random.default_rng(79)
    k = 6
    Sigma = _corr_matrix(rng, k)
    ic_mean = np.array([0.01, -0.01, 0.02, -0.03, 0.005, -0.002])
    r = OPT.max_icir_weights(Sigma, ic_mean)
    assert np.array_equal(r["sign"], np.array([1, -1, 1, -1, 1, -1], dtype=float))
    assert (np.sign(r["weights"]) == r["sign"]).all()


def test_单因子时权重为_1():
    r = OPT.max_icir_weights(np.array([[1.0]]), np.array([-0.03]))
    assert r["weights"][0] == pytest.approx(-1.0)


def test_相关矩阵形状不符必须报错():
    with pytest.raises(ValueError):
        OPT.max_icir_weights(np.eye(4), np.zeros(3))


# ═══════════════ 3. 有意的行为分歧（各有独立测试，逐条可查）═══════════════


def test_分歧_ic_mean_全零返回等权而非_NaN():
    """旧实现 0/0 → NaN 权重（静默发出坏组合）；本实现等权 + fallback 标记。"""
    k = 4
    r = OPT.max_icir_weights(np.eye(k), np.zeros(k))
    assert r["fallback"] is True
    assert np.allclose(r["weights"], np.full(k, 0.25))
    assert np.isfinite(r["weights"]).all()


def test_分歧_ic_mean_含_NaN_必须显式报错():
    """旧实现三道守卫全部漏过 NaN（比较恒为 False），最终发出 NaN 权重。"""
    with pytest.raises(ValueError, match="非有限值"):
        OPT.max_icir_weights(np.eye(3), np.array([0.01, np.nan, 0.02]))


def test_分歧_误传相关矩阵给合成_IC_必须返回_None():
    """相关矩阵是 F×F，与长度 F 的权重**形状恰好兼容** —— 形状校验抓不住。

    真传错了会合成出 F 个「IC」并算出假 ICIR，故用「行数必须大于列数」兜住。
    """
    rng = np.random.default_rng(83)
    corr = _corr_matrix(rng, 10)
    assert OPT.composite_ic_series(corr, np.full(10, 0.1)) is None


# ═══════════════ 4. 合成 IC ═══════════════


def test_合成_IC_等于逐日线性合成():
    rng = np.random.default_rng(89)
    T, F = 400, 3
    M = rng.normal(0, 0.01, (T, F))
    w = np.array([0.5, -0.3, 0.2])
    r = OPT.composite_ic_series(M, w)
    series = M @ w
    assert r["n_days"] == T
    assert r["ic_mean"] == pytest.approx(series.mean(), rel=1e-15)
    assert r["ic_std"] == pytest.approx(series.std(), rel=1e-15)
    assert r["icir"] == pytest.approx(series.mean() / series.std(), rel=1e-15)


def test_合成_IC_的_ICIR_随分散化提升():
    """**这条断言就是「不能用相关矩阵合成」的原因**。

    三个低相关因子各自的 ICIR ≈ 1，等权合成后必须显著高于 1（分散化收益）。
    若误用相关矩阵参与合成，会得出「分散化让 ICIR 变差」的假结论。
    """
    rng = np.random.default_rng(97)
    T = 2000
    base = rng.normal(0, 1, (T, 3))
    M = (base @ np.array([[1.0, 0.1, 0.0], [0.1, 1.0, 0.1], [0.0, 0.1, 1.0]])) * 0.01 + 0.01
    single = [M[:, i].mean() / M[:, i].std() for i in range(3)]
    comb = OPT.composite_ic_series(M, np.full(3, 1 / 3))
    assert min(single) > 0.5
    assert comb["icir"] > max(single)


def test_合成_IC_丢弃非有限行后再计天数():
    rng = np.random.default_rng(101)
    M = rng.normal(0, 0.01, (100, 2))
    M[3, :] = np.nan
    r = OPT.composite_ic_series(M, np.array([0.5, 0.5]))
    assert r["n_days"] == 99


@pytest.mark.parametrize("bad", [
    np.zeros((0, 3)),                                    # 空
    np.zeros((50, 0)),                                   # 无因子
    np.zeros((5, 4)),                                    # 天数 ≤ 因子数（疑传错矩阵）
    np.full((100, 2), np.nan),                           # 全 NaN
])
def test_合成_IC_零项守卫(bad):
    n = bad.shape[1]
    assert OPT.composite_ic_series(bad, np.full(n, 0.5)) is None


def test_合成_IC_有效天数不足返回_None():
    rng = np.random.default_rng(103)
    M = rng.normal(0, 0.01, (25, 2))
    M[5:, :] = np.nan                     # 只剩 5 天有效 < MIN_COMPOSITE_DAYS
    assert OPT.composite_ic_series(M, np.array([0.5, 0.5])) is None


def test_合成_IC_权重长度不符返回_None():
    rng = np.random.default_rng(107)
    assert OPT.composite_ic_series(rng.normal(0, 1, (100, 3)), np.array([0.5, 0.5])) is None
