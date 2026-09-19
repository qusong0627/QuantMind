"""读时派生层（blocks）的口径守卫。

这一层最容易出的错是**静默取错列**：拿全截面换组比例当组合换手、拿 Q10−Q1
当可配多空、把紧凑日期喂给按月解析的函数。三者都不会抛异常，只会给出
「看起来也像那么回事」的数字。故本文件的重点是**来源断言**，不只是数值断言。
"""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from backend.services.engine.factor_report import blocks as B
# 两个下划线私有函数由 IC 块自己拥有（blocks 只是组装方，不再代为导出）——
# 从定义处导入，才不会因为 façade 的 `__all__` 调整而静默 ImportError。
from backend.services.engine.factor_report import blocks_ic as BI


def _df(t: int = 6, *, gt: bool = True, tradable: bool = True,
        extras: bool = False) -> pd.DataFrame:
    """合成逐日序列：G_j 收益 = j × 1% × (1 + 0.1·i) —— 逐日变化，非退化。

    ``extras`` 加上半 IC / 中性化 / 分域 / clip_frac / n_valid 这些**构建期附加列**
    （逐日不同值，便于断言窗口切片）。
    """
    rows = []
    for i in range(t):
        scale = 1.0 + 0.1 * i
        rec = {
            "date": 20260101 + i,
            "ic": 0.01 * (i + 1),
            "turnover": 0.99,          # 全截面换组比例：与组合换手**不同**，故意设成极端值
            "coverage": 0.95,
        }
        if extras:
            rec.update({
                "ic_top": 0.02 * (i + 1), "ic_bot": -0.02 * (i + 1),
                "ic_neutral": 0.005 * (i + 1),
                "ic_large": 0.001 * (i + 1), "ic_mid": 0.002 * (i + 1),
                "ic_small": 0.003 * (i + 1),
                "clip_frac": 0.05, "n_valid": 1000.0 + i,
            })
        for j in range(1, 11):
            rec[f"q{j}"] = (j * 0.01) * scale
        if gt:
            # 只有 G3 换手 0.2、G9 换手 0.4 —— 组合换手必须是两者均值 0.3
            for j in range(1, 11):
                rec[f"gt{j}"] = 0.2 if j == 3 else (0.4 if j == 9 else 0.1)
        if tradable:
            for j in range(1, 11):
                # 可交易轨故意比理想轨差（理想 = j×1%×scale）：多头腿砍掉 0.2%、
                # 空头腿抬 0.2% —— 方向写反时下面的断言会红
                rec[f"q{j}_trad_long"] = (j * 0.01) * scale - 0.002
                rec[f"q{j}_trad_short"] = (j * 0.01) * scale + 0.002
        rows.append(rec)
    return pd.DataFrame(rows)


def _q(df: pd.DataFrame) -> np.ndarray:
    return df[[f"q{i}" for i in range(1, 11)]].to_numpy(dtype=np.float64)


# ═══════════════════ 1. 组合换手 vs 全截面换组比例 ═══════════════════


def test_组合换手取_gt_而不是全截面换组比例():
    """``turnover`` 列是全截面换组比例（本例 0.99），**不是** G3/G9 两条腿的换手。
    拿它当组合换手会系统性高估成本。"""
    df = _df()
    assert B.legs_turnover(df, 3, 9) == pytest.approx(0.3)
    h = B.headline_block(df, _q(df), df["ic"].to_numpy(), long_group=3, short_group=9,
                         cost_bps=20.0, k=1)
    assert h["turnover"] == pytest.approx(0.3), "7 指标环的 Turnover 必须是两条腿的换手"


def test_无_gt_列时组合换手为_None_而不是退回全截面():
    df = _df(gt=False)
    assert B.legs_turnover(df, 3, 9) is None
    h = B.headline_block(df, _q(df), df["ic"].to_numpy(), long_group=3, short_group=9,
                         cost_bps=20.0, k=1)
    assert h["turnover"] is None
    assert h["fitness"] is None, "无换手就无 Fitness/Margin（不写 0）"
    # 净口径同样必须整块置空：退化成「净收益 = 毛收益」等于在报告上写「成本为零」，
    # 读者会据此认为成本不重要 —— 那是把「不知道」显示成了「没有损耗」。
    for key in ("net_returns", "net_ir", "net_fitness", "net_cum_return"):
        assert h[key] is None, f"无换手时 {key} 必须是 None，不能等于毛口径"


# ═══════════════════ 2. 7 指标环的恒等式 ═══════════════════


def test_指标环满足_brain_恒等式():
    df = _df(t=60)
    h = B.headline_block(df, _q(df), df["ic"].to_numpy(), long_group=3, short_group=9,
                         cost_bps=20.0, k=1)
    assert h["returns"] == pytest.approx(h["mu_daily"] * 252)
    assert h["margin"] == pytest.approx(h["returns"] / h["turnover"])
    expect_fit = h["ir"] * np.sqrt(abs(h["returns"]) / max(h["turnover"], 0.125))
    assert h["fitness"] == pytest.approx(expect_fit)


def test_净口径比毛口径差_且成本为_0_时两者相等():
    df = _df(t=60)
    q, ic = _q(df), df["ic"].to_numpy()
    zero = B.headline_block(df, q, ic, long_group=3, short_group=9, cost_bps=0.0, k=1)
    assert zero["net_returns"] == pytest.approx(zero["returns"])
    paid = B.headline_block(df, q, ic, long_group=3, short_group=9, cost_bps=50.0, k=1)
    assert paid["net_returns"] < paid["returns"], "扣费后年化只能更低"


def test_指标环与分组块描述的是同一个请求窗口():
    """7 指标环必须与 IC/ICIR 同窗口 —— 这几个数是要一起读的。

    曾经的实现里 IC 取 ``ic_all[cut]``（窗口）而 Returns/IR 取全窗口：
    同一行里 ICIR 描述 250 天、IR 描述 2588 天；更糟的是 250 天窗口的报告上
    会显示十年累计收益，而分布图只有 250 个点 —— 两个数字对不上却都不报错。
    """
    df = _df(t=100)
    kw = {"factor": "a158_ROC20", "horizon": "fwd_ret_5", "long_group": 3,
          "short_group": 9, "cost_bps": 20.0, "snapshot": None}
    full = B.build_blocks(df, lookback=0, **kw)
    win = B.build_blocks(df, lookback=30, **kw)
    assert full["headline"]["n_dates"] == 100, "lookback<=0 应是整段全窗口"
    assert win["headline"]["n_dates"] == 30, "环必须只描述请求窗口"
    assert len(win["group_block"]["ls_daily"]) == 30
    assert win["headline"]["cum_return"] != pytest.approx(full["headline"]["cum_return"]), \
        "窗口内的累计收益不该等于全历史累计收益"
    # 累计型曲线仍走全窗口（截断就失去意义）——两处口径必须都锁住
    assert len(win["ic_block"]["ic_cum_full"]) == 100
    assert len(win["group_block"]["ls_daily"]) == 30


def test_IC块里按天取均值的量全部走请求窗口():
    """同一个 IC 卡片上「全截面 IC 均值」与「Top 半 IC 均值」必须同源。

    曾经 top/bot/neutral/domain/clip 全用整列：250 天窗口下前者的天数是 250、
    后者是 2588，而「中性化 2564 天」会配一张 250 个点的图 —— 并排显示却不同源。
    规则：**按天取均值 → 窗口；名字带 `_full` 的累计序列 → 全窗口**。
    """
    df = _df(t=100, extras=True)
    kw = {"factor": "a158_ROC20", "horizon": "fwd_ret_5", "long_group": 3,
          "short_group": 9, "cost_bps": 20.0, "snapshot": None}
    icb = B.build_blocks(df, lookback=30, **kw)["ic_block"]

    assert len(icb["ic_series"]) == 30 and len(icb["ic_neutral_series"]) == 30
    assert icb["ic_neutral_days"] == 30, "中性化天数必须等于窗口内的序列长度"
    # 逐日递增的合成序列：窗口均值必然大于全窗口均值（早期的值更小）
    full = B.build_blocks(df, lookback=0, **kw)["ic_block"]
    for key in ("ic_top_mean", "ic_bot_mean", "ic_neutral_mean"):
        assert icb[key] != pytest.approx(full[key]), f"{key} 看上去取了全窗口"
    for name in ("large", "mid", "small"):
        assert icb["ic_domain"][name] != pytest.approx(full["ic_domain"][name]), \
            f"分域 {name} 看上去取了全窗口"
    assert len(icb["ic_domain_series"]["large"]) == 30
    assert icb["n_valid_mean"] > full["n_valid_mean"], "n_valid 应随窗口右移而变大"


def test_累计曲线走全窗口_日频统计走请求窗口(monkeypatch):
    """两条口径的分界：**描述整段历史**的量（累计曲线/回撤/分年度）走全窗口，
    **描述日序列**的量（日收益/直方图/月度）服从 ``lookback``。

    曾经整块都按窗口切：250 天窗口下「分年度超额收益」只剩两根柱子、累计净值曲线
    从窗口起点重新起算（页面上看不出这是被截的，看着就像「这因子只活了两年」）。
    反向的错法同样存在：把日收益也放成全窗口，会导致时间序列与它旁边的分布图不同源。
    """
    df = _df(t=100)
    # 造一段跨两个自然年的日期 —— 否则「分年度」在窗口与全窗口下都是 1 行，断言会假通过
    df["date"] = [int(d.strftime("%Y%m%d"))
                  for d in pd.date_range("2021-01-04", periods=100, freq="W")]
    # ⚠️ 必须打在 `blocks` 这个名字空间上：`excess_block` 与 `build_blocks` 都在
    # blocks.py 里做模块级查找。若 `excess_block` 哪天被搬进 blocks_bench.py，
    # 这条 patch 会**只拦到 build_blocks 那一路**，excess_block 静默用真基准 ——
    # 下面关于 annual / excess_hist 的断言会变成「看着通过、其实没测到」。
    monkeypatch.setattr(B, "bench_daily_returns",
                        lambda sym, dates: np.full(len(dates), 0.0003))
    kw = {"factor": "a158_ROC20", "horizon": "fwd_ret_5", "long_group": 3,
          "short_group": 9, "cost_bps": 20.0, "snapshot": None, "bench_symbol": "000300.SH"}
    blk = B.build_blocks(df, lookback=30, **kw)

    grp = blk["group_block"]
    assert len(grp["cum_dates_full"]) == 100 and len(grp["dates"]) == 30, "两套轴必须各自成立"
    for key in ("long_cum", "short_cum", "short_book_cum", "ls_cum"):
        assert len(grp[key]) == 100, f"{key} 是累计型，被截成了窗口"
    for key in ("long_daily", "short_daily", "ls_daily"):
        assert len(grp[key]) == 30, f"{key} 是日频型，不该走全窗口"
    assert grp["ls_dist"]["n"] == 30, "日收益分布描述的是窗口内的日序列"
    assert len(grp["ls_monthly"]["matrix"]) >= 1

    trad = grp["tradable"]
    assert len(trad["ls_cum"]) == 100 and len(trad["ls_daily"]) == 30
    assert len(trad["cum_dates_full"]) == 100
    # 理想轨终值就是多空累计曲线的终值 —— 两条轨同源同区间，这是最容易分叉的地方
    assert trad["ideal_cum_end"] == pytest.approx(grp["ls_cum"][-1] - 1.0)

    exc = blk["excess_block"]
    assert len(exc["dates"]) == 100, "超额块主体是累计型，轴必须全窗口"
    assert len(exc["long_excess_cum"]) == 100
    assert len(exc["annual"]) == 2, "分年度超额必须覆盖全样本，不是窗口内那一年"
    assert sum(r["n_days"] for r in exc["annual"]) == 100
    assert len(exc["ls_daily"]) == 30 and len(exc["ls_dates"]) == 30
    assert exc["excess_hist"]["n"] == 30, "超额直方图与日序列同口径"


def test_多空取的是可配分组而不是极值组():
    """构建期只存了 Q10−Q1 的 ``ls_{h}``；可配多空必须从 ``q1..q10`` 现算。"""
    df = _df(t=4)
    q = _q(df)
    blk = B.group_block(df, q, B.iso_dates(df["date"]), long_group=3, short_group=9,
                        cost_bps=20.0, k=1)
    ls = np.asarray(blk["ls_daily"], dtype=np.float64)
    assert ls == pytest.approx(0.5 * (q[:, 2] - q[:, 8]))
    assert not np.allclose(ls, 0.5 * (q[:, -1] - q[:, 0])), "别拿 Q10−Q1 冒充 G3/G9"


# ═══════════════════ 3. 可交易轨 ═══════════════════


def test_可交易轨比理想轨差_且两条腿各用各的口径():
    df = _df(t=30)
    q = _q(df)
    blk = B.group_block(df, q, B.iso_dates(df["date"]), long_group=3, short_group=9,
                        cost_bps=20.0, k=1)
    tr = blk["tradable"]
    assert tr["available"] is True
    assert tr["tradable_cum_end"] < tr["ideal_cum_end"], "剔掉不可成交的部分只能更差"
    assert tr["lost_return"] > 0
    # 两个 `_cum_end` 必须是**累计收益**，不是 cum_curve 的净值因子 ∏(1+r)。
    # 返回净值会让前端把 0.99 渲染成「+99%」—— 量级看着合理，错 100 个百分点。
    ideal_daily = np.asarray(blk["ls_daily"], dtype=np.float64)
    expect_ideal = float(np.prod(1.0 + ideal_daily) - 1.0)
    assert tr["ideal_cum_end"] == pytest.approx(expect_ideal, abs=1e-12)
    assert abs(tr["ideal_cum_end"]) < 1.0, "合成序列是日频小收益，累计收益不该接近 1"
    # 多头腿用 trad_long（减 0.2%）、空头腿用 trad_short（加 0.2%）→ 日收益比理想低 0.002
    ideal = np.asarray(blk["ls_daily"], dtype=np.float64)
    got = np.asarray(tr["ls_daily"], dtype=np.float64)
    assert got == pytest.approx(ideal - 0.002)


def test_可交易轨里的_inf_不再静默毁掉整条累计曲线():
    """构建期洗了理想轨的 ±inf，却漏洗可交易轨 —— inf 进 cumprod 立刻溢出，
    整条累计曲线变 NaN，而 available 仍报 True（判据是「有有限值」）。
    现在坏日子按 0 收益入曲线，并把坏天数显式披露出来。"""
    df = _df(t=30)
    df.loc[5, "q3_trad_long"] = np.inf
    blk = B.group_block(df, _q(df), B.iso_dates(df["date"]), long_group=3, short_group=9,
                        cost_bps=20.0, k=1)
    tr = blk["tradable"]
    assert tr["available"] is True
    assert tr["invalid_days"] == 1, "坏天数必须算得出来并披露"
    assert tr["tradable_cum_end"] is not None and np.isfinite(tr["tradable_cum_end"])
    assert np.isfinite(np.asarray(tr["ls_cum"], dtype=np.float64)).all(), "曲线不得整条变 NaN"


def test_无可交易列时显式降级而不是写_0():
    df = _df(t=10, tradable=False)
    blk = B.group_block(df, _q(df), B.iso_dates(df["date"]), long_group=3, short_group=9,
                        cost_bps=20.0, k=1)
    assert blk["tradable"]["available"] is False
    assert "重跑构建" in blk["tradable"]["reason"]
    assert blk["tradable"]["ideal_cum_end"] is not None, "理想轨照常供数"


# ═══════════════════ 4. 日期格式（按月解析的静默陷阱）═══════════════════


def test_紧凑日期必须转_ISO_否则月度矩阵会解析出_91_月():
    assert B.iso_dates([20260918]) == ["2026-09-18"]
    assert B.iso_dates(["2026-09-18"]) == ["2026-09-18"], "已是 ISO 的原样返回"
    df = _df(t=40)
    blk = B.group_block(df, _q(df), B.iso_dates(df["date"]), long_group=3, short_group=9,
                        cost_bps=20.0, k=1)
    months = blk["ls_monthly"]["months"]
    assert months and all(1 <= m <= 12 for m in months), f"月份越界：{months}"


# ═══════════════════ 5. 零项守卫 ═══════════════════


def test_空_IC_序列不抛且不给假显著():
    empty = np.array([], dtype=np.float64)
    sig = B.significance_block(empty, empty, None, "f")
    assert sig["t_value"] is None and sig["nw_t_value"] is None
    assert sig["q_value_bhy"] is None, "无 p 值就不能有 q 值"
    assert sig["bootstrap_ic_mean"] is None
    assert sig["n_factors_tested"] is None


def test_缓存目录快照缺因子时_独立性为_None():
    assert BI._independence(None, "x") is None
    assert BI._independence({"correlation": {"factors": ["a"], "matrix": [[1.0]]}}, "b") is None
    ind = BI._independence(
        {"correlation": {"factors": ["a", "b"], "matrix": [[1.0, -0.8], [-0.8, 1.0]]}}, "a")
    assert ind["max_corr"] == pytest.approx(0.8), "取 |ρ|，负相关同样是「不独立」"


def test_全库_t_值来自_icir_乘根号_n():
    snap = {"meta": {"n_dates": 100}, "factors": [{"name": "a", "icir": 0.2}, {"name": "b", "icir": -0.1}]}
    t, n = BI._library_t_values(snap)
    assert n == 100
    assert t.tolist() == pytest.approx([2.0, 1.0]), "取绝对值：双侧检验不关心方向"


def test_指定基准取不到时该项置灰_其余基准照常():
    """用户点名中证1000 而它取不到时，**不能静默换成沪深300 还不说**：
    换是可以的（否则整块空着），但被点名的那一项必须在 benchmarks[] 里带 reason，
    且 bench_symbol 必须自曝用的是哪条。"""
    dates = ["2026-01-05", "2026-01-06"]
    out = B.excess_block(dates, np.array([0.01, 0.02]), np.array([0.005, 0.01]), "999999.XX")
    asked = [b for b in out["benchmarks"] if b["symbol"] == "999999.XX"]
    assert asked and asked[0]["available"] is False
    assert "取不到" in asked[0]["reason"]
    assert out["bench_symbol"] != "999999.XX", "实际用的基准必须自曝，不能冒充被点名的那个"


def test_全部基准都取不到时才整块降级():
    dates = ["2026-01-05", "2026-01-06"]
    out = B.excess_block(dates, np.array([0.01, 0.02]), np.array([0.005, 0.01]), None)
    # 真实环境里 300/500/1000 都在，故这里只断言「要么给出基准、要么带 reason」，
    # 不允许出现既不可用又没有原因的空白块
    assert out["available"] is True or out.get("reason")


# ═══════════════════ 6. 稳健性块 ═══════════════════


def test_分段统计带日期区间且段数正确():
    df = _df(t=40)
    dates = B.iso_dates(df["date"])
    rb = B.robust_block(df, dates, df["ic"].to_numpy(), np.full(40, 0.001), 0.3)
    segs = rb["sub_period"]
    assert len(segs) == 4
    assert segs[0]["start"] == dates[0] and segs[0]["end"] == dates[segs[0]["i1"] - 1]
    assert all(s["start"] is not None for s in segs)
    assert rb["oos"]["in_sample_ic"] is not None


# ═══════════ 7. 响应体体积与「两类量各自带轴」 ═══════════
#
# 铁律：**噪声型序列服从 lookback，累计型序列走全窗口**，且每个 `_full` 序列必须
# 与**全窗口那一套**日期轴配对。写反了不会抛异常，只有两种症状，且都不报错：
#
#   ① 累计序列误走 lookback → 序列短、日期轴长，前端拿 250 个点去配 2600 个类目
#      标签，ECharts 把它们放在最左侧 ~10%（**看着有图、其实只画了窗口那一段**）。
#      实测命中过一次：`cum_ic_top_full` / `cum_ic_bot_full`，名字带 `_full`、
#      却读的是 `df[tail_slice]`。
#   ② 噪声序列误走全窗口 → 每加一条就多几万字节，十数列叠加即数量级膨胀。
#
# 故这里两条腿都断言：轴长逐条钉死（抓 ①），总量设上限（抓 ②）。

_N_DAYS = 2600
_LOOKBACK = 250
# 实测 1.01 MB（2600 天 × 全列：10 组 × 双轨 + 8 个 IC 口径 + 10 风格 + 基准超额）。
# 上限给 ~2×：只拦「数量级」级别的膨胀。单条序列窗口写反只涨几十 KB，2 MB 拦不住，
# 那类错由下面的轴长断言负责抓 —— 两个断言的职责不重叠。
_PAYLOAD_CAP_BYTES = 2_000_000
_STYLES = ("size", "beta", "momentum", "residvol", "nlsize", "btop",
           "liquidity", "earningsyield", "growth", "leverage")


def _long_frame(n: int = _N_DAYS) -> pd.DataFrame:
    """跨 2018–2026 的千日级帧。

    与 :func:`_df` 分开，因为后者的 ``scale = 1 + 0.1·i`` 是为「逐日非退化」设计的
    指数增长，上千天会把累计曲线撑成 ``inf`` —— 体积测量要的是**真实量级的收益**，
    ``inf`` / ``None`` 的 JSON 表示反而更短，会让上限断言失去意义。
    """
    days = np.busday_offset("2018-01-01", np.arange(n), roll="forward").astype(
        "datetime64[D]")
    rng = np.random.default_rng(7)
    rows = []
    for i in range(n):
        tide = float(np.sin(i / 37.0))
        base = 0.0004 * (1 + 0.4 * tide)
        rec = {
            "date": int(str(days[i]).replace("-", "")),
            "ic": 0.0001 * tide,
            "turnover": 0.5 + 0.01 * tide,
            "coverage": 0.93,
            "ic_top": 0.0002 * tide, "ic_bot": -0.0002 * tide,
            "ic_neutral": 0.00008 * tide,
            "ic_large": 0.00004 * tide, "ic_mid": 0.00007 * tide,
            "ic_small": 0.00011 * tide,
            "clip_frac": 0.031 + 0.001 * tide, "n_valid": 4200.0 + i % 97,
        }
        for j in range(1, 11):
            rec[f"q{j}"] = base * j
            rec[f"gt{j}"] = 0.12 + 0.01 * (j % 3) + 0.002 * tide
            rec[f"q{j}_trad_long"] = base * j - 0.00007
            rec[f"q{j}_trad_short"] = base * j + 0.00007
        for h in (1, 2, 3, 5, 10, 20):
            rec[f"ls_{h}"] = 0.0002 * (1 + 0.5 * tide) / np.sqrt(h)
        for s in _STYLES:
            rec[f"sc_{s}"] = 0.02 * tide + float(rng.normal(0, 0.01))
        rows.append(rec)
    return pd.DataFrame(rows)


def test_千日帧的响应体体积上限与两套轴各自成立():
    """上限断言与轴断言互为「非空参与」守卫，故合在一个测试里。"""
    df = _long_frame()
    blk = B.build_blocks(df, factor="a158_ROC20", horizon="fwd_ret_5",
                         lookback=_LOOKBACK, long_group=3, short_group=9,
                         cost_bps=20.0, snapshot=None, bench_symbol=None)
    ic, grp = blk["ic_block"], blk["group_block"]

    # ── 零项守卫：两套轴若重合，下面的轴断言全部恒真；块若整体降级，
    #    体积断言也会「因为什么都没算」而通过 ──
    assert _N_DAYS > _LOOKBACK, "帧太短：两套轴重合，轴断言退化为恒真"
    assert ic["available"] is True and grp["available"] is True, "块降级了，测的不是目标路径"

    # ── ① 累计型：全窗口，且与全窗口日期轴同长 ──
    for name in ("ic_cum_full", "cum_ic_top_full", "cum_ic_bot_full", "ir_rolling_full"):
        assert len(ic[name]) == len(ic["cum_dates_full"]) == _N_DAYS, (
            f"ic_block.{name} 是累计/全窗口序列，却带 length={len(ic[name])}，"
            f"而 cum_dates_full 是 {len(ic['cum_dates_full'])} —— 前端三线图会把"
            f"它画在最左侧 {len(ic[name]) / max(len(ic['cum_dates_full']), 1):.0%} 处")
    for name in ("long_cum", "short_cum", "short_book_cum", "ls_cum"):
        assert len(grp[name]) == len(grp["cum_dates_full"]) == _N_DAYS, name
    assert len(grp["tradable"]["ls_cum"]) == _N_DAYS
    assert len(grp["tradable"]["cum_dates_full"]) == _N_DAYS

    # ── ② 噪声型：服从 lookback ──
    for name in ("ic_series", "ic_rolling", "ic_rolling_long", "ic_neutral_series",
                 "dates"):
        assert len(ic[name]) == _LOOKBACK, name
    for name in ("long_daily", "short_daily", "ls_daily", "dates"):
        assert len(grp[name]) == _LOOKBACK, name
    # 分域 / 风格暴露时序同属噪声型。⚠️ 先判非空：对空 dict 做 all(...) 恒为 True，
    # 本条若只写 all(...) 就在「这一块整个没算出来」时静默通过。
    dom = ic["ic_domain_series"]
    exp = blk["style_block"]["exposure_ts"]
    assert len(dom) == 3, f"分域 IC 应有三档，实得 {sorted(dom)}"
    assert len(exp) == len(_STYLES), f"风格暴露时序应有 {len(_STYLES)} 条，实得 {sorted(exp)}"
    assert all(len(v) == _LOOKBACK for v in dom.values())
    assert all(len(v) == _LOOKBACK for v in exp.values())

    # ── ③ 体积 ──
    raw = json.loads(json.dumps(blk))  # 走一遍真实序列化路径（非有限数会在这里暴露）
    size = len(json.dumps(raw, ensure_ascii=False).encode("utf-8"))
    assert size > _PAYLOAD_CAP_BYTES // 4, (
        f"响应体只有 {size:,} 字节 —— 远小于实测量级，说明块内容大面积缺失，"
        f"下面的上限断言会变成假通过")
    assert size < _PAYLOAD_CAP_BYTES, (
        f"详情响应体 {size:,} 字节 ≥ 上限 {_PAYLOAD_CAP_BYTES:,}。"
        f"检查：新加的序列是噪声型还是累计型？噪声型必须服从 lookback"
        f"（累计型走全窗口是刻意的，别一刀切改短）")
