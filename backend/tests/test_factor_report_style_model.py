"""自算风格模型：数学不变量 + 已知答案。

风格产物一旦算错，报告上表现为「超额收益与某风格高度相关」这种**看起来像发现**的
结论 —— 比报错危险得多。故本文件的重点是**已知答案**：合成数据里注入已知的 beta、
已知的纯因子收益、已知的覆盖缺口，断言模型能原样还原。
"""

from __future__ import annotations

import numpy as np
import pytest

from backend.services.engine.factor_report import style_model as SM


# ═══════════════════ 1. 截面预处理 ═══════════════════


def test_缩尾把极值压到_k_倍_MAD_且不动正常值():
    rng = np.random.default_rng(11)
    row = rng.normal(0, 1, 500)
    row[0] = 1e6                       # 极值
    A = row.reshape(1, -1)
    out = SM.winsorize_rows(A, k=3.0)
    med = np.median(row)
    mad = 1.4826 * np.median(np.abs(row - med))
    assert out[0, 0] == pytest.approx(med + 3.0 * mad)
    assert out[0, 1:].max() < 10 * mad  # 其余值保持原样（本身就在界内）


def test_zscore_逐行标准化且常数列为_NaN():
    A = np.array([[1.0, 2.0, 3.0], [5.0, 5.0, 5.0]])
    out = SM.zscore_rows(A)
    assert out[0].mean() == pytest.approx(0.0)
    assert out[0].std() == pytest.approx(1.0)          # ddof=0
    assert np.isnan(out[1]).all(), "常数列没有截面信息，必须是 NaN 而不是 0"


def test_zscore_NaN_不参与均值与标准差():
    A = np.array([[1.0, 2.0, np.nan, 4.0]])
    out = SM.zscore_rows(A)
    assert np.isnan(out[0, 2])
    valid = np.array([1.0, 2.0, 4.0])
    assert out[0, [0, 1, 3]] == pytest.approx((valid - valid.mean()) / valid.std())


def test_blend_z_分量缺失时按剩余权重归一而不是整行作废():
    """growth 的两个分量在早年只有营收可用 —— 那一年不该整列消失。"""
    a = np.array([[1.0, 2.0, 3.0, 4.0]])
    b = np.array([[np.nan, np.nan, 3.0, 4.0]])
    out = SM.blend_z([(a, 0.5), (b, 0.5)])
    assert np.isfinite(out[0, 0])                       # 只剩 a 时按 a 自己归一
    assert out[0, 0] == pytest.approx(SM.zscore_rows(a)[0, 0])
    both = SM.blend_z([(a, 0.5), (b, 0.5)])
    only_a = SM.zscore_rows(a)
    only_b = SM.zscore_rows(b)
    assert both[0, 2] == pytest.approx(0.5 * only_a[0, 2] + 0.5 * only_b[0, 2])


def test_blend_z_全缺返回_NaN():
    nan = np.full((1, 3), np.nan)
    assert np.isnan(SM.blend_z([(nan, 0.5), (nan, 0.5)])).all()


# ═══════════════════ 2. 时序描述子（已知答案） ═══════════════════


def test_动量剔除最近_21_日():
    """构造「前段大涨 + 最近 21 日大跌」的序列：剔除口径下动量仍为正。"""
    n = 400
    close = np.ones((n, 1))
    close[1:300] = 1.01 ** np.arange(1, 300)[:, None]   # 前段每日 +1%
    close[300:] = close[299] * 0.99 ** np.arange(1, n - 299)[:, None]
    mom = SM.momentum(close)
    assert mom[-1, 0] > 0, "最近 21 日的下跌不该进动量"
    # 直接对照：Σ ln(1+r) over t-252..t-21
    r = SM.log_returns_from_close(close)[:, 0]
    want = np.nansum(r[n - 252 : n - 21])
    assert mom[-1, 0] == pytest.approx(want, rel=1e-9)


def test_动量覆盖不足返回_NaN():
    close = np.ones((30, 2))
    assert np.isnan(SM.momentum(close)[-1]).all()


def test_beta_已知答案_合成序列还原斜率():
    """r_i = 1.7·r_m + 噪声 → beta ≈ 1.7，HSIGMA ≈ 残差标准差（= σ_ε）。

    HSIGMA 是**残差**波动而不是总波动：``σ_r²·(1−ρ²)`` 展开后恰等于 σ_ε²。
    （写成 ``σ_ε²·(1−ρ²)`` 会得到一个小数，是本模块实现里真实踩过的坑 ——
    归一化漏掉时 hsigma 偏大 √Σw ≈ 9.2 倍，两种错都会让这个数「看起来像波动率」。）
    """
    rng = np.random.default_rng(17)
    T, N = 400, 40
    sig_m, sig_e, b = 0.012, 0.004, 1.7
    m = rng.normal(0, sig_m, T)
    R = np.column_stack([b * m + rng.normal(0, sig_e, T) for _ in range(N)])
    beta, hsigma = SM.beta_and_hsigma(R, m)
    assert np.nanmean(beta[-1]) == pytest.approx(b, abs=0.05)
    var_r = (b * sig_m) ** 2 + sig_e**2
    rho2 = (b * sig_m) ** 2 / var_r
    assert np.nanmedian(hsigma[-1]) == pytest.approx(np.sqrt(var_r * (1 - rho2)), rel=0.15)


def test_beta_按_近期加权覆盖_判定而不是简单计数():
    """半衰期权重下，「最近 100 天有效」与「只有最老的 100 天有效」**不是**同一回事。

    前者近端权重占 100 天窗口的 ~71%（够用），后者只剩 ~25%（不够）。
    这条断言锁的是判据的语义：覆盖度按**权重**算，与回归本身同一把尺子。
    """
    rng = np.random.default_rng(19)
    T, N = 400, 3
    m = rng.normal(0, 0.01, T)
    R = np.column_stack([m + rng.normal(0, 0.005, T) for _ in range(N)])
    R[:300, 0] = np.nan          # 只有最近 100 天
    R[100:, 1] = np.nan          # 只有最老的 100 天
    beta, _ = SM.beta_and_hsigma(R, m)
    assert np.isfinite(beta[-1, 0]), "近端 100 天占 71% 权重，应当出数"
    assert np.isnan(beta[-1, 1]), "只剩远端 100 天（25% 权重）不足，必须 NaN"
    assert np.isfinite(beta[-1, 2])


def test_cmra_等于窗口内累计路径的极差():
    rng = np.random.default_rng(23)
    r = rng.normal(0, 0.01, (300, 1))
    got = SM.cmra(r)[-1, 0]
    z = np.nancumsum(r[:, 0])
    want = z[-252:].max() - z[-252:].min()
    assert got == pytest.approx(want, rel=1e-9)


def test_dastd_等于窗口标准差():
    rng = np.random.default_rng(29)
    r = rng.normal(0, 0.02, (300, 1))
    assert SM.dastd(r)[-1, 0] == pytest.approx(np.std(r[-252:, 0]), rel=1e-9)


# ═══════════════════ 3. 截面描述子 ═══════════════════


def test_size_对非正市值给_NaN_而不是_inf():
    """实测 valuation 里有精确 0 的市值（float_mv 718 格）—— ln(0) = -inf 会污染整行。"""
    out = SM.size_exposure(np.array([[100.0, 0.0, -5.0, np.nan]]))
    assert np.isfinite(out[0, 0])
    assert np.isnan(out[0, 1:]).all()
    assert np.isfinite(out).sum() == 1


def test_btop_对非正_pb_给_NaN():
    out = SM.btop_exposure(np.array([[2.0, 0.0, -1.0]]))
    assert out[0, 0] == pytest.approx(0.5)
    assert np.isnan(out[0, 1:]).all()


def test_earningsyield_量纲自洽():
    """net_profit_ttm 与 total_mv 同为元 → 比值就是收益率，无需换算常数。"""
    out = SM.earnings_yield(np.array([[1e10, 1e10, 1e10]]), np.array([[1e11, 0.0, 1e11]]))
    assert out[0, 0] == pytest.approx(0.1)
    assert np.isnan(out[0, 1])
    assert out[0, 2] == pytest.approx(0.1)


def test_leverage_与市值同量纲_并挡住脏数据():
    """ME=1e11 元、长债=3e10 元 → MLEV=1.3；超出上界的（如单位错）判 NaN。"""
    out = SM.leverage(np.array([[1e11, 1e11, 1e11]]), np.array([[3e10, 0.0, 5e13]]))
    assert out[0, 0] == pytest.approx(1.3)
    assert out[0, 1] == pytest.approx(1.0)
    assert np.isnan(out[0, 2]), "5e13/1e11=500 倍杠杆 → 脏数据守卫必须拦住"


def test_换手率_量纲自洽_且同列缩放不影响_z():
    """volume 与 circulating_capital 同为**股** → 换手率量级 0.02（不是 200 或 1e-6）。

    z-score 对同列整体缩放不变，故即使单位判断有误，liquidity 也不受影响 ——
    这一点是设计意图（避免在无法验证的量纲上写 1e4 魔数），单独锁住。
    """
    T, N = 300, 6
    cap = np.full((T, N), 1e9)
    vol = np.tile(np.linspace(1e7, 3e7, N), (T, 1))     # 有截面离散度才谈得上 z
    turn = vol / cap
    assert turn[-1, 0] == pytest.approx(0.01)           # 单位判据：股/股 → 无量纲换手率
    out = SM.liquidity(vol, cap)
    assert np.isfinite(out[-1]).all()
    scaled = SM.liquidity(vol * 1e-4, cap)              # 假设把股误当手：整体缩 1e4 倍
    assert scaled[-1] == pytest.approx(out[-1]), "同列整体缩放不该改变 z 后的合成结果"


# ═══════════════════ 4. 标准化流水线 ═══════════════════


def _pipeline_fixture(seed=31, T=60, N=300):
    rng = np.random.default_rng(seed)
    raw = {k: rng.normal(0, 1, (T, N)) for k in SM.STYLE_NAMES}
    # size 与 btop 人为强相关，验证「对 size 正交」确实生效
    raw["btop"] = 2.0 * raw["size"] + 0.3 * raw["btop"]
    codes = [f"IND{i % 8}" for i in range(N)]
    return raw, codes


def test_流水线输出逐日近似单位标准差且行业均值为零():
    raw, codes = _pipeline_fixture()
    out = SM.standardize_pipeline(raw, codes)
    for k, v in out.items():
        assert np.nanstd(v[10]) == pytest.approx(1.0, abs=0.02), k
        for ind in set(codes):
            m = np.array([c == ind for c in codes])
            assert abs(np.nanmean(v[10][m])) < 0.05, f"{k} 在 {ind} 内未去均值"


def test_流水线对_size_正交后相关性显著下降():
    raw, codes = _pipeline_fixture()
    z = {k: SM.zscore_rows(SM.winsorize_rows(v)) for k, v in raw.items()}
    before = _row_corr(z["btop"][10], z["size"][10])
    out = SM.standardize_pipeline(raw, codes)
    after = _row_corr(out["btop"][10], out["size"][10])
    assert before > 0.9, "构造保证正交前强相关"
    assert abs(after) < 0.2, f"对 size 正交后仍相关 {after:.3f}"


def test_流水线不修改输入的_NaN_位置():
    raw, codes = _pipeline_fixture()
    raw["beta"][5, 7] = np.nan
    out = SM.standardize_pipeline(raw, codes)
    assert np.isnan(out["beta"][5, 7]), "缺失必须保持缺失 —— 填充值会被当成观测计入相关"


def test_面板起点的前_252_行_窗口型风格必须无定义():
    """面板从 --start 起算，「前 252 行」没有完整回看窗口。

    不遮蔽的后果不是报错而是**换口径**：momentum 用 0 补缺失（标签写 252 日、
    实际几十日）、beta 靠覆盖率判据在 t≈150 就出数、liquidity 只剩 21/63 日分量。
    三条都与标签不符且与后面的值不可比 —— 报告窗口通常远在其后，遮蔽代价为零。
    """
    rng = np.random.default_rng(53)
    T, N = 300, 40
    panels = {
        "total_mv": np.full((T, N), 1e10),
        "float_mv": np.full((T, N), 1e10),
        "pb": rng.uniform(0.5, 5.0, (T, N)),
        "net_profit_ttm": np.full((T, N), 1e9),
        "revenue_ttm": np.full((T, N), 1e10),
        "circulating_capital": np.full((T, N), 1e9),
        "volume": rng.uniform(1e6, 1e7, (T, N)),
        "close": 10 * np.cumprod(1 + rng.normal(0, 0.01, (T, N)), axis=0),
        "long_term_debt": np.full((T, N), 1e9),
    }
    raw = SM.descriptors_from_panels(panels, rng.normal(0, 0.01, T))
    for k in SM.WINDOW_DESCRIPTORS:
        assert np.isnan(raw[k][: SM.BETA_WINDOW_DAYS]).all(), f"{k} 前 252 行必须无定义"
        assert np.isfinite(raw[k][SM.BETA_WINDOW_DAYS :]).any(), f"{k} 过了爬坡段必须出数"
    # 截面型不受面板起点影响：第 0 天就有值
    for k in ("size", "nlsize", "btop", "earningsyield", "leverage"):
        assert np.isfinite(raw[k][0]).all(), f"{k} 是截面型，不该被爬坡遮蔽"


def test_size_残差相关_全空给_None_而不是_0():
    """0 会被读成「与市值完全正交」——宁可 null。"""
    T, N = 3, 50
    expo = {k: np.full((T, N), np.nan) for k in SM.STYLE_NAMES}
    assert set(SM.size_residual_corr(expo).values()) == {None}


def test_size_残差相关_能测出残留的暴露():
    T, N = 3, 400
    rng = np.random.default_rng(59)
    size = rng.normal(0, 1, (T, N))
    expo = {k: rng.normal(0, 1, (T, N)) for k in SM.STYLE_NAMES}
    expo["size"] = size
    expo["btop"] = 0.6 * size + 0.8 * rng.normal(0, 1, (T, N))   # 残留 0.6 相关
    out = SM.size_residual_corr(expo)
    assert out["size"] == pytest.approx(1.0, abs=1e-3)
    assert out["btop"] == pytest.approx(0.6, abs=0.05)


def _row_corr(a, b):
    ok = np.isfinite(a) & np.isfinite(b)
    return float(np.corrcoef(a[ok], b[ok])[0, 1])


# ═══════════════════ 5. 纯因子收益（WLS 已知答案） ═══════════════════


def test_WLS_还原注入的纯因子收益():
    """r = Σ X_k f_k + 行业效应 + ε，注入已知 f → lstsq 必须还原。

    权重取常数（等权），避免权重与暴露相关带来的加权最小二乘偏差干扰断言。
    """
    rng = np.random.default_rng(37)
    T, N = 3, 400
    f_true = np.zeros(len(SM.STYLE_NAMES))
    f_true[:3] = [0.01, -0.02, 0.005]              # 只注入前三个风格，其余为 0
    codes = np.array([f"IND{i % 10}" for i in range(N)])
    ind_effect = {f"IND{i}": rng.normal(0, 0.005) for i in range(10)}
    expo = {k: rng.normal(0, 1, (T, N)) for k in SM.STYLE_NAMES}
    y = np.zeros((T, N))
    for t in range(T):
        y[t] = sum(expo[k][t] * f_true[i] for i, k in enumerate(SM.STYLE_NAMES))
        y[t] += np.array([ind_effect[c] for c in codes])
        y[t] += rng.normal(0, 1e-6, N)
    w = np.ones((T, N))
    pure, diag = SM.pure_factor_returns(expo, codes, y, w)
    assert np.allclose(pure, f_true, atol=1e-6)
    assert (diag["n_used"] == N).all()


def test_WLS_剔除任一暴露缺失的股票并计数():
    rng = np.random.default_rng(41)
    T, N = 1, 300
    codes = np.array([f"IND{i % 6}" for i in range(N)])
    expo = {k: rng.normal(0, 1, (T, N)) for k in SM.STYLE_NAMES}
    expo["growth"][0, :50] = np.nan                    # 早年成长风格缺覆盖
    y = rng.normal(0, 0.01, (T, N))
    w = np.ones((T, N))
    _, diag = SM.pure_factor_returns(expo, codes, y, w)
    assert diag["n_used"][0] == N - 50
    assert diag["n_universe"][0] == N


def test_WLS_样本不足给_NaN_而不是硬解():
    rng = np.random.default_rng(43)
    T, N = 1, 5                                      # 5 只股票 < 10 个风格
    codes = np.array(["IND0"] * N)
    expo = {k: rng.normal(0, 1, (T, N)) for k in SM.STYLE_NAMES}
    pure, diag = SM.pure_factor_returns(expo, codes, rng.normal(0, 0.01, (T, N)), np.ones((T, N)))
    assert np.isnan(pure).all()
    assert diag["n_used"][0] == N


def test_WLS_权重为零的股票不参与():
    rng = np.random.default_rng(47)
    T, N = 1, 200
    codes = np.array([f"IND{i % 5}" for i in range(N)])
    expo = {k: rng.normal(0, 1, (T, N)) for k in SM.STYLE_NAMES}
    w = np.ones((T, N))
    w[0, :20] = 0.0                                    # 市值缺失/为 0
    _, diag = SM.pure_factor_returns(expo, codes, rng.normal(0, 0.01, (T, N)), w)
    assert diag["n_used"][0] == N - 20


# ═══════════════════ 6. FF5 扩展（RMW / CMA） ═══════════════════


def test_rmw_等于扣非TTM除以净资产_负净资产给_NaN():
    npf = np.array([[1e9, -2e8, 5e8]])
    be = np.array([[1e10, 1e10, -1e8]])                # 第三只资不抵债
    out = SM.rmw_exposure(npf, be)
    assert out[0, 0] == pytest.approx(0.1)
    assert out[0, 1] == pytest.approx(-0.02), "真亏损必须保留负值（低盈利暴露的真实取值）"
    assert np.isnan(out[0, 2]), "负净资产比率会翻正，必须 NaN"


def test_cma_取负号_高资本开支得负暴露():
    capex = np.array([[1e8, 5e8, 1e8]])
    ta = np.array([[1e10, 1e10, 0.0]])
    out = SM.cma_exposure(capex, ta)
    assert out[0, 0] == pytest.approx(-0.01)
    assert out[0, 1] < out[0, 0], "投资更激进（capex/TA 更大）必须暴露更低"
    assert np.isnan(out[0, 2]), "总资产为 0 → NaN 而不是 inf"


def _descriptor_panels(T=4, N=3, seed=71):
    rng = np.random.default_rng(seed)
    return {
        "total_mv": np.full((T, N), 1e10),
        "float_mv": np.full((T, N), 1e10),
        "pb": rng.uniform(0.5, 5.0, (T, N)),
        "net_profit_ttm": np.full((T, N), 1e9),
        "revenue_ttm": np.full((T, N), 1e10),
        "circulating_capital": np.full((T, N), 1e9),
        "volume": rng.uniform(1e6, 1e7, (T, N)),
        "close": 10 * np.cumprod(1 + rng.normal(0, 0.01, (T, N)), axis=0),
        "long_term_debt": np.full((T, N), 1e9),
    }


def test_描述子_财务面板缺席时不产出_rmw_cma():
    """老调用路径（测试与旧产物）只给 9 个基础面板 —— 必须退化为 10 风格，不 KeyError。"""
    panels = _descriptor_panels()
    raw = SM.descriptors_from_panels(panels, np.zeros(4))
    assert "rmw" not in raw and "cma" not in raw


def test_描述子_财务面板在场时产出_rmw_cma():
    panels = _descriptor_panels()
    panels["deducted_net_profit_ttm"] = np.array([[1e9, 2e9, 3e9]] * 4, dtype=float)
    panels["book_equity"] = np.full((4, 3), 1e10)
    panels["capex_ttm"] = np.array([[1e8, 2e8, 3e8]] * 4, dtype=float)
    panels["total_assets"] = np.full((4, 3), 1e10)
    raw = SM.descriptors_from_panels(panels, np.zeros(4))
    assert raw["rmw"][0, 2] == pytest.approx(3e9 / 1e10)
    assert raw["cma"][0, 0] == pytest.approx(-1e8 / 1e10)


def test_WLS_只解入参里实际存在的风格():
    """财务面板缺失时 rmw/cma 不在 exposures 里 —— 解的列集必须跟着缩，
    而不是拿整列 NaN 去回归（那会把每天都剔空，产出整段空区间）。"""
    rng = np.random.default_rng(53)
    T, N = 30, 200
    codes = np.array([f"IND{i % 5}" for i in range(N)])
    expo = {k: rng.normal(0, 1, (T, N)) for k in ("size", "beta", "btop")}
    y = 0.01 * expo["btop"] + rng.normal(0, 0.001, (T, N))
    pure, diag = SM.pure_factor_returns(expo, codes, y, np.ones((T, N)))
    assert diag["styles"] == ["size", "beta", "btop"]
    assert pure.shape == (T, 3)
    assert np.isfinite(pure).any(), "三种风格必须照常出数"


# ═══════════════════ 7. 风格块契约（前端「口径声明」的清单来源） ═══════════════════


def test_风格块携带完整风格目录_按_STYLE_NAMES_顺序():
    """前端口径声明逐条列举风格名，清单由这里的 ``styles`` 给。

    少了这一把键，前端就只剩自己硬编码的兜底清单 —— 上一次扩风格（10 → 12）
    正是被那份硬编码漏掉，页面照旧宣称「十大风格」。故此处把目录钉死：
    顺序 = STYLE_NAMES（字典序会让人读不出分组），label 必须非空。
    """
    import pandas as pd

    from backend.services.engine.factor_report import blocks as B

    T = 8
    df = pd.DataFrame({"date": [20260101 + i for i in range(T)]})
    for s in SM.STYLE_NAMES:                      # 每风格一列构建期相关列
        df[f"sc_{s}"] = np.linspace(-0.5, 0.5, T)
    blk = B.style_block(df, ["2026-01-01"] * T, np.zeros(T), None, horizon=5)

    styles = blk.get("styles")
    assert styles, "风格目录缺失 —— 前端又要退回到自己硬编码的风格清单"
    assert [s["key"] for s in styles] == list(SM.STYLE_NAMES)
    assert all(s["label"] for s in styles), "每个风格都必须带中文名（表头直接读它）"
    # 覆盖表按 |均值相关| 降序重排，与目录顺序**不同** —— 目录不能拿覆盖表反推
    assert {e["style"] for e in blk["exposures"]} == set(SM.STYLE_NAMES)
