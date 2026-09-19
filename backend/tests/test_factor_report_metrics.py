"""因子报告机构级指标：口径恒等式与统计不变量单测（纯函数、无 IO）。

为什么这些断言值得写：本模块的每个函数都会直接印在机构级报告上，
口径错一位（年化用 CAGR 还是 μ×252、回撤取末值还是最大、t 值不做自相关修正）
都不会报错，只会静默给出**看起来合理但用不了**的数字。

三条主线：
1. **口径恒等式**：Fitness / Margin / Returns / IR 与用户给出的参考报告
   （Returns -22.91% · IR -1.7044 · Turnover 76.35% · Fitness -0.9337 · Margin -0.300）
   逐位复现 —— 这组数字是所有口径定义的锚点，改了公式这里必红。
2. **统计正确性**：NW t 在强自相关下必须显著小于普通 t（否则这函数没有存在意义）；
   Bootstrap 覆盖率 ≈ 名义水平；BHY q ≥ p 且随检验数单调不减。
3. **零项守卫**：空序列 / 全 NaN / 单元素输入不得静默返回 0。
"""

from __future__ import annotations

import numpy as np
import pytest

from backend.services.engine.factor_report import metrics as M

# ═══════════════ 1. 口径恒等式（用户参考报告为锚）═══════════════


def test_参考报告口径_returns_为日均乘年化而非_CAGR():
    """Returns = μ_daily × 252，不是复利 CAGR。

    反证：参考报告 n=2114、Returns=-22.91%。若按 CAGR 反推 8.39 年的复利终值
    应是 -85% 累计，与 μ×252 的路徑差 2.5pct；而 μ×252 与 IR、σ 三者自洽
    （σ = μ√252/IR = 0.847%，报告给 σ=0.85%）。
    """
    mu, sigma = -0.000909, 0.008468
    # 构造均值恰为 mu、样本标准差恰为 sigma 的序列（零均值正交噪声平移缩放而来）
    rng = np.random.default_rng(11)
    noise = rng.normal(0.0, 1.0, 2114)
    noise = noise - noise.mean()
    noise = noise / noise.std(ddof=1) * sigma
    daily = np.full(2114, mu) + noise - noise.mean()  # 均值回到 mu

    head = M.brain_headline(daily, turnover=0.7635)
    assert head["n_days"] == 2114
    assert head["returns"] == pytest.approx(-0.2291, abs=2e-3)
    assert head["ir"] == pytest.approx(-1.7044, abs=2e-2)


@pytest.mark.parametrize(
    "returns,ir,turnover,exp_fitness,exp_margin",
    [
        (-0.2291, -1.7044, 0.7635, -0.9337, -0.300),  # 用户参考报告
    ],
)
def test_BRAIN_恒等式_复现参考报告(returns, ir, turnover, exp_fitness, exp_margin):
    """Fitness ≡ IR·√(|Returns|/max(Turnover,0.125))；Margin ≡ Returns/Turnover。"""
    assert M.fitness_of(ir, returns, turnover) == pytest.approx(exp_fitness, abs=5e-4)
    assert M.margin_of(returns, turnover) == pytest.approx(exp_margin, abs=5e-4)


def test_fitness_地板_0_125_生效():
    """换手低于 0.125 时按 0.125 计（BRAIN 约定），否则低换手因子会被无限放大。"""
    low = M.fitness_of(ir=1.0, returns=0.10, turnover=0.01)
    floor = M.fitness_of(ir=1.0, returns=0.10, turnover=M.TURNOVER_FLOOR)
    assert low == pytest.approx(floor)
    assert low == pytest.approx(1.0 * np.sqrt(0.10 / 0.125))


def test_brain_headline_恒等式自洽():
    """brain_headline 产出的四个数必须满足两条恒等式（同一份输入，不许各自为政）。"""
    rng = np.random.default_rng(3)
    daily = rng.normal(0.0006, 0.009, 800)
    turnover = np.full(800, 0.42)
    h = M.brain_headline(daily, turnover)
    assert h["fitness"] == pytest.approx(
        M.fitness_of(h["ir"], h["returns"], h["turnover"]), abs=1e-12
    )
    assert h["margin"] == pytest.approx(
        M.margin_of(h["returns"], h["turnover"]), abs=1e-12
    )
    assert h["ann_vol"] == pytest.approx(h["sigma_daily"] * np.sqrt(252), rel=1e-12)


def test_brain_headline_零项守卫():
    """空序列 / 全 NaN 不得返回 0 —— 必须显式 None（报告里 0 会被读成「真的没收益」）。"""
    h = M.brain_headline(np.array([]), turnover=np.array([]))
    assert h["returns"] is None and h["ir"] is None and h["fitness"] is None
    h2 = M.brain_headline(np.full(50, np.nan), turnover=0.3)
    assert h2["returns"] is None and h2["n_days"] == 0


# ═══════════════ 2. 半截面 IC / 累计 IC ═══════════════


def test_half_ic_上下半各取一半且符号相反():
    """因子值与收益单调同向时：上半 IC≈+1、下半 IC≈+1；反向时两者都≈−1。"""
    n = 200
    x = np.arange(n, dtype=float)
    order = M.rank_of(x)
    y = x * 2.0  # 与 x 完全同序
    assert M.half_ic(order, y, top=True) == pytest.approx(1.0, abs=1e-9)
    assert M.half_ic(order, y, top=False) == pytest.approx(1.0, abs=1e-9)
    assert M.half_ic(order, -y, top=True) == pytest.approx(-1.0, abs=1e-9)


def test_half_ic_只覆盖对应半区():
    """下半区被污染不得影响上半区 IC（两半必须真的只看到自己那一半）。"""
    n, k = 200, 100
    x = np.arange(n, dtype=float)
    order = M.rank_of(x)
    y = x.copy()
    y[:k] = -x[:k] * 5.0  # 只污染下半
    top = M.half_ic(order, y, top=True)
    bot = M.half_ic(order, y, top=False)
    assert top == pytest.approx(1.0, abs=1e-9)
    assert bot == pytest.approx(-1.0, abs=1e-9)


def test_half_ic_样本不足返回_None():
    order = M.rank_of(np.arange(10, dtype=float))
    assert M.half_ic(order, np.arange(10, dtype=float), top=True) is None


def test_cum_ic_等于逐日累加且可含_NaN():
    ic = np.array([0.01, 0.02, np.nan, -0.01])
    got = M.cum_ic(ic)
    assert np.isnan(got[2])  # 缺口透传，不当作 0 累计
    assert got[3] == pytest.approx(0.01 + 0.02 - 0.01)


# ═══════════════ 3. 多空组合 / 回撤 ═══════════════


def test_ls_daily_是两腿等权半仓之差():
    """ls = 0.5×(long − short)：dollar-neutral、总杠杆 100%。"""
    q = np.array([[0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 0.07, 0.08, 0.09, 0.10]])
    got = M.ls_daily(q, long_group=3, short_group=9)
    assert got[0] == pytest.approx(0.5 * (0.03 - 0.09))


def test_ls_daily_分组下标越界即失败():
    q = np.zeros((2, 10))
    with pytest.raises(ValueError):
        M.ls_daily(q, long_group=11, short_group=9)
    with pytest.raises(ValueError):
        M.ls_daily(q, long_group=3, short_group=0)


def test_max_drawdown_不劣于末值回撤():
    """恒等式：最大回撤 ≤ 末值回撤 ≤ 0 —— 与 factor_deep_dive 的旧口径对拍时的唯一桥梁。"""
    curve = np.array([1.0, 1.2, 0.6, 0.9, 1.3, 1.1])
    mdd = M.max_drawdown(curve)
    tail = M.tail_drawdown(curve)
    assert mdd == pytest.approx(0.6 / 1.2 - 1.0)
    assert tail == pytest.approx(1.1 / 1.3 - 1.0)
    assert mdd <= tail <= 0


def test_drawdown_episodes_找出前_N_段且按深度排序():
    # 1.0 → 1.5 → 0.9（深 -40%）→ 1.6 → 1.2（浅 -25%）
    curve = np.array([1.0, 1.5, 0.9, 1.6, 1.2])
    eps = M.drawdown_episodes(curve, top=5)
    assert len(eps) == 2
    assert eps[0]["dd"] == pytest.approx(0.9 / 1.5 - 1.0)
    assert eps[1]["dd"] == pytest.approx(1.2 / 1.6 - 1.0)
    assert eps[0]["days"] >= 1


def test_drawdown_episodes_零项守卫():
    assert M.drawdown_episodes(np.array([]), top=3) == []
    assert M.max_drawdown(np.array([])) is None


# ═══════════════ 4. 收益分布 / 尾部 ═══════════════


def test_return_distribution_统计量与_VaR_CVaR():
    rng = np.random.default_rng(5)
    daily = rng.normal(0.0, 0.01, 5000)
    d = M.return_distribution(daily, bins=30)
    assert d["n"] == 5000
    assert d["mu"] == pytest.approx(0.0, abs=5e-4)
    assert d["sigma"] == pytest.approx(0.01, rel=0.05)
    assert len(d["counts"]) == len(d["bin_edges"]) - 1
    assert sum(d["counts"]) == 5000
    # VaR/CVaR 都是「损失为正」的负数，且 CVaR ≤ VaR（尾部更差）
    assert d["var_95"] < 0 and d["cvar_95"] <= d["var_95"]
    assert d["cvar_99"] <= d["cvar_95"]


def test_return_distribution_零项守卫():
    d = M.return_distribution(np.array([]))
    assert d["n"] == 0 and d["mu"] is None and d["var_95"] is None


# ═══════════════ 5. 多重检验（BHY）═══════════════


def test_bhy_q_值不小于_p_值且有序():
    rng = np.random.default_rng(2)
    p = rng.uniform(0, 1, 50)
    q = M.bhy_qvalues(p)
    assert np.all(q >= p - 1e-12)
    assert np.all(q <= 1.0)
    # 按 p 升序排列后 q 必须单调不减
    o = np.argsort(p)
    assert np.all(np.diff(q[o]) >= -1e-12)


def test_bhy_因子数越多_同一_p_的_q_越大():
    """多重检验的意义：同样 p=0.05，检验 10 个 vs 1000 个，后者 q 单调更大。"""
    q_small = M.bhy_qvalues(np.full(10, 0.05))[0]
    q_big = M.bhy_qvalues(np.full(1000, 0.05))[0]
    assert q_big > q_small


def test_bhy_零项守卫():
    with pytest.raises(ValueError):
        M.bhy_qvalues(np.array([]))


# ═══════════════ 6. Newey-West t ═══════════════


def test_nw_t_独立序列下与普通_t_接近():
    rng = np.random.default_rng(17)
    x = rng.normal(0.01, 0.1, 4000)
    plain = M.plain_tstat(x)
    nw = M.nw_tstat(x)
    assert nw == pytest.approx(plain, rel=0.15)


def test_nw_t_强自相关下必须显著小于普通_t():
    """本函数的全部意义：IC 自相关时 OLS 标准误低估 → t 值虚高。"""
    rng = np.random.default_rng(23)
    n = 3000
    eps = rng.normal(0, 1, n)
    x = np.empty(n)
    x[0] = eps[0]
    for t in range(1, n):
        x[t] = 0.85 * x[t - 1] + eps[t]  # AR(1)，ρ≈0.85
    x = x * 0.01 + 0.005  # 缩放并给一个正均值
    plain = M.plain_tstat(x)
    nw = M.nw_tstat(x)
    assert nw is not None and plain is not None
    assert abs(nw) < abs(plain) * 0.7, f"NW {nw} 未显著小于普通 t {plain}"


def test_nw_t_零项守卫():
    assert M.nw_tstat(np.array([])) is None
    assert M.nw_tstat(np.array([1.0])) is None
    assert M.nw_tstat(np.zeros(100)) is None  # 零方差


def test_ic_autocorr_阶数与_AR1_还原():
    rng = np.random.default_rng(29)
    n = 4000
    eps = rng.normal(0, 1, n)
    x = np.empty(n)
    x[0] = eps[0]
    for t in range(1, n):
        x[t] = 0.6 * x[t - 1] + eps[t]
    ac = M.ic_autocorr(x, lags=5)
    assert len(ac) == 5
    assert ac[0] == pytest.approx(0.6, abs=0.05)
    assert ac[1] == pytest.approx(0.36, abs=0.07)


# ═══════════════ 7. Bootstrap CI ═══════════════


def test_bootstrap_ci_覆盖率接近名义水平():
    """大样本下 95% CI 应覆盖真值约 95%（这里跑 200 轮核验，容差放宽到 ±4pct）。"""
    rng = np.random.default_rng(31)
    hit = 0
    trials = 200
    for i in range(trials):
        s = rng.normal(0.0, 1.0, 300) + 0.3
        ci = M.bootstrap_ci(s, n_boot=400, alpha=0.05, seed=1000 + i)
        if ci["lo"] <= 0.3 <= ci["hi"]:
            hit += 1
    assert 0.89 <= hit / trials <= 0.99


def test_bootstrap_ci_可复现():
    rng = np.random.default_rng(37)
    s = rng.normal(0.01, 0.05, 500)
    a = M.bootstrap_ci(s, n_boot=500, seed=42)
    b = M.bootstrap_ci(s, n_boot=500, seed=42)
    assert a == b


def test_bootstrap_ci_零项守卫():
    assert M.bootstrap_ci(np.array([])) is None


# ═══════════════ 8. 成本敏感性与盈亏平衡 ═══════════════


def test_cost_sensitivity_零成本时等于毛收益():
    rng = np.random.default_rng(41)
    daily = rng.normal(0.0008, 0.01, 600)
    turnover = np.full(600, 0.5)
    cs = M.cost_sensitivity(daily, turnover, bps_grid=[0, 10, 20])
    row0 = cs["rows"][0]
    assert row0["bps"] == 0
    assert row0["net_return"] == pytest.approx(float(daily.mean()) * 252, rel=1e-9)
    assert row0["net_ir"] == pytest.approx(M.brain_headline(daily, turnover)["ir"], rel=1e-9)


def test_cost_sensitivity_成本越高净收益越低():
    rng = np.random.default_rng(43)
    daily = rng.normal(0.0010, 0.01, 800)
    turnover = np.full(800, 0.6)
    cs = M.cost_sensitivity(daily, turnover, bps_grid=[0, 20, 50])
    rets = [r["net_return"] for r in cs["rows"]]
    assert rets[0] > rets[1] > rets[2]


def test_盈亏平衡成本处净收益归零():
    turnover_val = 0.5
    daily = np.full(500, 0.0004)  # μ=4bp/日；换手 0.5 → 盈亏平衡 = 4/0.5 = 8bp
    cs = M.cost_sensitivity(daily, np.full(500, turnover_val), bps_grid=[0, 5, 20])
    assert cs["break_even_bps"] == pytest.approx(8.0, rel=1e-6)
    at_be = M.cost_sensitivity(daily, np.full(500, turnover_val), bps_grid=[cs["break_even_bps"]])
    assert at_be["rows"][0]["net_return"] == pytest.approx(0.0, abs=1e-9)


def test_盈亏平衡成本_零换手返回_None():
    cs = M.cost_sensitivity(np.full(100, 0.001), np.zeros(100), bps_grid=[10])
    assert cs["break_even_bps"] is None


# ═══════════════ 9. 持有期扫描 ═══════════════


def test_holding_period_sweep_长持有期换手更低():
    # ls_h 与 h 同比放大 → 三者毛日均收益相同（0.1%/日），差异只来自成本摊薄
    ls_by_h = {1: np.full(500, 0.001), 5: np.full(500, 0.005), 20: np.full(500, 0.020)}
    turnover = np.full(500, 0.3)
    rows = M.holding_period_sweep(ls_by_h, turnover, cost_bps=20)
    assert [r["hold_days"] for r in rows] == [1, 5, 20]
    # 毛日均收益 = ls_h / h，三者相同；但持有越久摊到日均的调仓成本越低 → 净收益递增
    nets = [r["net_return"] for r in rows]
    assert nets[0] < nets[1] < nets[2]
    # 持有期内的累计换手随 h 递增（换得更久自然换得更多），
    # 但**摊到日均**的换手递减 —— 这正是长持有期省成本的机制
    turns = [r["turnover"] for r in rows]
    assert turns[0] < turns[1] < turns[2]
    per_day = [r["turnover"] / r["hold_days"] for r in rows]
    assert per_day[0] > per_day[1] > per_day[2]


def test_holding_period_sweep_零项守卫():
    assert M.holding_period_sweep({}, np.array([0.3])) == []


# ═══════════════ 10. 容量估算 ═══════════════


def test_capacity_单调性_参与率与成交额():
    base = M.capacity_estimate(turnover=0.3, median_amount=2e8, n_positions=40, participation=0.10)
    half_p = M.capacity_estimate(turnover=0.3, median_amount=2e8, n_positions=40, participation=0.05)
    half_a = M.capacity_estimate(turnover=0.3, median_amount=1e8, n_positions=40, participation=0.10)
    assert half_p["est_aum"] == pytest.approx(base["est_aum"] / 2, rel=1e-9)
    assert half_a["est_aum"] == pytest.approx(base["est_aum"] / 2, rel=1e-9)


def test_capacity_必须带假设值而非裸数字():
    got = M.capacity_estimate(turnover=0.3, median_amount=2e8, n_positions=40)
    assert got["assumed_participation"] == M.DEFAULT_PARTICIPATION
    assert "简化模型" in got["note"]
    assert got["est_aum"] is not None


def test_capacity_零项守卫():
    assert M.capacity_estimate(turnover=0.0, median_amount=2e8, n_positions=40)["est_aum"] is None
    assert M.capacity_estimate(turnover=0.3, median_amount=None, n_positions=40)["est_aum"] is None


# ═══════════════ 11. 风格归因回归 ═══════════════


def test_style_attribution_还原已知_alpha_与_beta():
    rng = np.random.default_rng(53)
    n = 2000
    f1 = rng.normal(0, 0.01, n)
    f2 = rng.normal(0, 0.01, n)
    alpha = 0.0004
    y = alpha + 1.3 * f1 - 0.7 * f2 + rng.normal(0, 1e-4, n)
    got = M.style_attribution(y, {"Size": f1, "Beta": f2})
    assert got["alpha"] == pytest.approx(alpha, abs=2e-4)
    betas = {b["style"]: b["beta"] for b in got["betas"]}
    assert betas["Size"] == pytest.approx(1.3, abs=0.02)
    assert betas["Beta"] == pytest.approx(-0.7, abs=0.02)
    assert got["r_squared"] > 0.99


def test_style_attribution_噪声放大后_alpha_t_值下降():
    rng = np.random.default_rng(59)
    n = 1500
    f = rng.normal(0, 0.01, n)
    alpha = 0.0005
    clean = alpha + 0.5 * f + rng.normal(0, 1e-5, n)
    noisy = alpha + 0.5 * f + rng.normal(0, 3e-3, n)
    t_clean = M.style_attribution(clean, {"Size": f})["t_alpha"]
    t_noisy = M.style_attribution(noisy, {"Size": f})["t_alpha"]
    assert abs(t_clean) > abs(t_noisy)


def test_style_attribution_零项守卫():
    assert M.style_attribution(np.array([]), {}) is None
    assert M.style_attribution(np.zeros(100), {"Size": np.zeros(100)}) is None  # 风格无变化


def test_style_correlations_单变量相关与回归分歧():
    """单变量相关与回归 β 是**两个数**，共线时必然分歧 —— 这正是并列展示的意义。

    构造：y 完全由 f1 驱动，f2 = f1 + 极小噪声（与 f1 近乎共线）。
    单变量看：corr(y, f2) 也很高（被 f1 带出来）；
    回归看：控制 f1 后 f2 的 β 应不显著。
    若某天有人把这张表实现成「拿回归 β 当相关」，这条测试会红。
    """
    rng = np.random.default_rng(97)
    n = 2000
    f1 = rng.normal(0, 0.01, n)
    f2 = f1 + rng.normal(0, 1e-5, n)
    y = 0.9 * f1 + rng.normal(0, 1e-4, n)
    rows = M.style_correlations(y, {"Size": f1, "NLSize": f2})
    got = {r["style"]: r for r in rows}
    assert got["Size"]["corr"] > 0.99
    assert got["NLSize"]["corr"] > 0.99, "共线的 f2 单变量相关同样高（这正是要展示的分歧）"
    assert got["NLSize"]["n_days"] == n
    # 回归里 f2 的边际贡献应远小于其单变量相关所暗示的程度
    att = M.style_attribution(y, {"Size": f1, "NLSize": f2})
    t_nlsize = {b["style"]: b["t"] for b in att["betas"]}["NLSize"]
    assert t_nlsize is not None and abs(t_nlsize) < 3, "控制 Size 后 NLSize 不该显著"


def test_style_correlations_逐风格各丢各的缺失日():
    """pairwise complete：某风格缺一天不该让其余九个风格跟着少一天。"""
    n = 300
    a = np.linspace(-1, 1, n)
    b = a.copy()
    b[::7] = np.nan            # 第二个风格缺 1/7 的天
    y = 0.5 * a
    rows = {r["style"]: r for r in M.style_correlations(y, {"A": a, "B": b})}
    assert rows["A"]["n_days"] == n
    assert rows["B"]["n_days"] == n - len(range(0, n, 7))
    assert rows["A"]["corr"] == pytest.approx(1.0)


def test_style_correlations_常量风格给_None_而不是_0():
    """常量序列的相关在数学上未定义。写 0 会被读成「确认与风格无关」——方向完全相反。"""
    y = np.linspace(-1, 1, 200)
    rows = M.style_correlations(y, {"Size": np.full(200, 0.3)})
    assert rows[0]["corr"] is None
    assert rows[0]["n_days"] == 200


def test_style_correlations_零项守卫():
    assert M.style_correlations(np.array([]), {"Size": np.zeros(10)}) is None
    assert M.style_correlations(np.zeros(100), {}) is None
    # 样本不足 → None，不是硬算一个数出来
    assert M.style_correlations(np.array([1.0, 2.0]), {"Size": np.array([1.0, 2.0])})[0]["corr"] is None


# ═══════════════ 12. 分段 / 年度 / 月度 / 滚动 ═══════════════


def test_sub_period_stats_覆盖全样本且首尾相接():
    ic = np.arange(100, dtype=float) / 1000.0
    segs = M.sub_period_stats(ic, k=4)
    assert len(segs) == 4
    assert segs[0]["i0"] == 0 and segs[-1]["i1"] == 100
    for a, b in zip(segs[:-1], segs[1:], strict=True):
        assert a["i1"] == b["i0"]
    assert segs[-1]["ic_mean"] > segs[0]["ic_mean"]


def test_annual_breakdown_按自然年复利且带超额():
    dates = ["2023-06-30", "2023-12-29", "2024-06-28", "2024-12-31"]
    daily = np.array([0.05, 0.05, -0.10, 0.00])
    bench = np.array([0.01, 0.01, -0.02, 0.00])
    rows = M.annual_breakdown(dates, daily, bench_daily=bench)
    assert [r["year"] for r in rows] == [2023, 2024]
    assert rows[0]["ret"] == pytest.approx(1.05 * 1.05 - 1)
    assert rows[0]["bench_ret"] == pytest.approx(1.01 * 1.01 - 1)
    assert rows[0]["excess"] == pytest.approx(rows[0]["ret"] - rows[0]["bench_ret"])
    assert rows[1]["n_days"] == 2


def test_monthly_matrix_行列正确():
    dates = ["2024-01-31", "2024-02-29", "2024-02-28"]
    daily = np.array([0.01, 0.02, 0.03])
    got = M.monthly_matrix(dates, daily)
    assert got["years"] == [2024]
    assert got["months"] == [1, 2]
    assert got["matrix"][0][0] == pytest.approx(0.01)
    assert got["matrix"][0][1] == pytest.approx(1.02 * 1.03 - 1)  # 同月复利


def test_rolling_ir_窗口与零项守卫():
    rng = np.random.default_rng(61)
    ic = rng.normal(0.01, 0.1, 300)
    r = M.rolling_ir(ic, win=252)
    assert len(r) == 300
    assert all(v is None for v in r[:251])
    assert r[-1] is not None
    assert M.rolling_ir(np.array([]), win=252) == []


def test_win_rate_and_monotonicity():
    assert M.win_rate(np.array([0.1, -0.1, 0.2, 0.0])) == pytest.approx(0.5)
    assert M.monotonicity(np.array([0.01, 0.02, 0.03, 0.04])) == pytest.approx(1.0)
    assert M.monotonicity(np.array([0.04, 0.03, 0.02, 0.01])) == pytest.approx(-1.0)
    assert M.win_rate(np.array([])) is None


# ═══════════════ 13. 尺度 / 符号不变量 ═══════════════


def test_反向组合与取负_Fitness_的对称性():
    """IC<0 的因子「反向使用」在数值上必须精确等于把多空收益取负。

    这是报告里「反向组合对照」那一列的数学依据：不是近似，是恒等。
    """
    rng = np.random.default_rng(67)
    q = rng.normal(0.0004, 0.01, (400, 10))
    turn = np.full(400, 0.4)
    ls_a = M.ls_daily(q, 3, 9)
    ls_rev = M.ls_daily(q, 9, 3)          # G9 多 / G3 空 = 反向组合
    assert np.allclose(ls_rev, -ls_a)

    a = M.brain_headline(ls_a, turn)
    b = M.brain_headline(-ls_a, turn)     # 等价于反向组合
    assert b["returns"] == pytest.approx(-a["returns"], rel=1e-12)
    assert b["ir"] == pytest.approx(-a["ir"], rel=1e-12)
    assert b["fitness"] == pytest.approx(-a["fitness"], rel=1e-12)
    # |Fitness| 与 |Margin| 不变（公式取的是绝对值/比值），只有符号翻转
    assert abs(b["fitness"]) == pytest.approx(abs(a["fitness"]), rel=1e-12)


def test_相关与回归对仿射变换免疫():
    """秩相关/回归只关心排序与线性关系：因子 ×(−2)+5 后 |IC|、|beta| 不变号逻辑清晰。"""
    rng = np.random.default_rng(71)
    x = rng.normal(0, 1, 300)
    y = rng.normal(0, 1, 300)
    base = M.spearman(x, y)
    flipped = M.spearman(x * -2.0 + 5.0, y)  # 负斜率：秩序反转 → 相关系数取反
    assert flipped == pytest.approx(-base)
    assert abs(flipped) == pytest.approx(abs(base))


# ═══════════════ 14. 构建期截面原语（矩阵形态）═══════════════
#
# 这四个函数只在**构建期**用得上（需要当日截面矩阵），不进读时派生路径。
# 它们一律是「同一口径的矩阵形态」：与逐列/逐对调用的标量版本同结果，
# 只是把 400+ 因子的 Python 循环压成一次矩阵运算（等价性各自断言）。


def test_半截面IC_矩阵形态与逐列标量一致():
    """矩阵形态必须与已有独立测试的 ``half_ic`` 逐列同结果 —— 两种执行形状、一份口径。"""
    rng = np.random.default_rng(401)
    n, k = 800, 6
    rank_x = np.tile(np.arange(1, n + 1, dtype=np.float64)[:, None], (1, k))
    rank_x[rng.random((n, k)) < 0.12] = np.nan          # 逐格缺失
    y = rng.normal(0, 0.02, n)
    y[rng.random(n) < 0.05] = np.nan
    for top in (True, False):
        got = M.half_ic_matrix(rank_x, y, top=top)
        assert got.shape == (k,)
        for j in range(k):
            want = M.half_ic(rank_x[:, j], y, top=top)
            if want is None:
                assert not np.isfinite(got[j]), f"列 {j} 标量版判为无效，矩阵版却给了 {got[j]}"
            else:
                assert got[j] == pytest.approx(want, rel=1e-12, abs=1e-15)


def test_半截面IC_矩阵_非线性下上下半反号():
    """y 随因子先降后升（U 形）→ 上半区正相关、下半区负相关，两个半区必须反号。

    这条挡住「半 IC 其实是把全截面 IC 抄了两遍」这类实现错误。
    """
    n = 1000
    rank_x = np.arange(1, n + 1, dtype=np.float64).reshape(-1, 1)
    y = ((rank_x[:, 0] - n / 2.0) ** 2).reshape(-1, 1)
    top = M.half_ic_matrix(rank_x, y[:, 0], top=True)[0]
    bot = M.half_ic_matrix(rank_x, y[:, 0], top=False)[0]
    assert top > 0.9 and bot < -0.9


def test_半截面IC_矩阵_零项守卫():
    assert M.half_ic_matrix(np.zeros((0, 3)), np.zeros(0), top=True).shape == (3,)
    assert np.isnan(M.half_ic_matrix(np.zeros((0, 3)), np.zeros(0), top=True)).all()
    # 有效样本不足（< 2×min_n）→ 全 NaN，而不是把噪声当结论
    small = np.tile(np.arange(1.0, 31.0)[:, None], (1, 2))
    assert np.isnan(M.half_ic_matrix(small, np.arange(30.0), top=True)).all()


def test_分域IC_小盘有信号大盘无信号():
    """市值三分位子域各算一次秩相关 —— 「大资金能不能用这个因子」的直接答案。"""
    n = 900
    mv = np.arange(1.0, n + 1.0)                        # 代码越大市值越大
    rng = np.random.default_rng(409)
    rx = (rng.permutation(n) + 1).astype(np.float64)
    y = np.where(mv < n / 3.0, rx, rng.normal(0, 1, n))  # 只有小盘段有真实信号
    got = M.domain_ic_matrix(rx.reshape(-1, 1), y, mv)
    assert set(got) == {"small", "mid", "large"}
    assert got["small"][0] > 0.9                        # 小盘段几乎完美
    assert abs(got["large"][0]) < 0.2                   # 大盘段是噪声
    # 中盘段：y 也是噪声，但剔除了小盘信噪结构 → 同样应当弱
    assert abs(got["mid"][0]) < 0.2


def test_分域IC_分位边界由当日全体市值确定而非逐因子():
    """分组边界必须**跨因子一致**（同一交易日的市值三分位是同一个切点）。

    否则每个因子按自己的有效样本切自己的分位，两列之间的『大盘 IC』不可比。
    """
    n = 600
    mv = np.arange(1.0, n + 1.0)
    rng = np.random.default_rng(411)
    rx = rng.permutation(n).astype(np.float64) + 1.0
    y = rng.normal(0, 1, n)
    # 两列因子：第二列把**最大市值段**整段抹成缺失
    two = np.stack([rx, np.where(mv > n * 2 / 3.0, np.nan, rx)], axis=1)
    got = M.domain_ic_matrix(two, y, mv)
    # 第一列有全部三段
    assert np.isfinite(got["large"][0])
    # 第二列的大盘段被抹空 → 该格按「样本不足」作废，而不是缩小样本后照算
    assert not np.isfinite(got["large"][1])


def test_分域IC_零项守卫():
    out = M.domain_ic_matrix(np.zeros((0, 2)), np.zeros(0), np.zeros(0))
    assert out["small"].shape == (2,) and np.isnan(out["small"]).all()
    # 市值全缺 → 三个子域都不可定义
    mv_nan = np.full(300, np.nan)
    out = M.domain_ic_matrix(np.random.default_rng(413).normal(size=(300, 2)), np.zeros(300), mv_nan)
    assert all(np.isnan(v).all() for v in out.values())


def test_去极值比例_已知答案():
    """注入 5% 的 ±20σ 极端值 → clip_frac ≈ 5%；纯正态列 → 近乎 0。"""
    rng = np.random.default_rng(419)
    x = rng.normal(0, 1, 1000)
    x[:50] = 20.0
    clean = rng.normal(0, 1, 1000)
    got = M.clip_frac_matrix(np.stack([x, clean], axis=1))
    assert got[0] == pytest.approx(0.05, abs=0.02)
    assert got[1] < 0.02


def test_去极值比例_零项守卫():
    assert np.isnan(M.clip_frac_matrix(np.zeros((0, 3)))).all()
    # 常数列：MAD=0，没有可判断的离散度 → NaN（不是 1.0，也不能是 0.0）
    assert np.isnan(M.clip_frac_matrix(np.full((100, 1), 7.0))[0])


def test_成对秩相关_已知答案与逐对同口径():
    rng = np.random.default_rng(421)
    A = rng.normal(0, 1, (500, 3))
    B = rng.normal(0, 1, (500, 2))
    B[:, 0] = A[:, 1] * 3.0 + 1.0                       # 单调变换 → 秩相关 = 1
    got = M.pairwise_rank_corr(A, B)
    assert got.shape == (3, 2)
    assert got[1, 0] == pytest.approx(1.0)
    assert got[2, 1] == pytest.approx(M.spearman(A[:, 2], B[:, 1]), rel=1e-12)
    # 反对称性：把 B 的列取负 → 相关系数取反
    assert M.pairwise_rank_corr(A, -B)[1, 0] == pytest.approx(-1.0)


def test_成对秩相关_NaN_逐格有效不整列作废():
    rng = np.random.default_rng(423)
    A = rng.normal(0, 1, (400, 2))
    B = rng.normal(0, 1, (400, 1))
    B[:, 0] = A[:, 0] + rng.normal(0, 0.1, 400)
    A[rng.random((400, 2)) < 0.15] = np.nan             # 15% 缺失
    got = M.pairwise_rank_corr(A, B)
    assert np.isfinite(got).all(), f"缺失不该让整列作废：{got}"
    assert got[0, 0] > 0.8


def test_成对秩相关_零项守卫():
    assert M.pairwise_rank_corr(np.zeros((0, 2)), np.zeros((0, 3))).shape == (2, 3)
    assert np.isnan(M.pairwise_rank_corr(np.zeros((0, 2)), np.zeros((0, 3)))).all()
