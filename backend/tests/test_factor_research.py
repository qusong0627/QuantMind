"""因子研究模块（factor_research）核心计算回归 —— 合成数据，无 IO。

钉住所依赖的不变量：
1. 打分 rank→正态分位：方向翻转、±4 截断、NaN 保持、截面归一
2. TTM 口径：income=单季求和；cashflow=上年年报+本年累计−上年同期（实测口径，见 financials.py 头注释）
3. IC：完全单调 → 1，反向 → −1
4. Top-N 回测：首期换手 1（全额计费）、涨跌停外的成本按换手计
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from backend.services.engine.factor_research import analysis, scorecard, service
from backend.services.engine.factor_research.engine import rank_to_score
from backend.services.engine.factor_research.financials import (
    _ttm_single_quarter,
    _ttm_ytd,
)

SYMS = [f"{i:06d}.SZ" for i in range(10)]
DATES = pd.to_datetime(["2026-01-30", "2026-02-27", "2026-03-31", "2026-04-30"])


# ---------------------------------------------------------------------------
# 打分
# ---------------------------------------------------------------------------
def test_rank_to_score_direction_and_bounds():
    raw = pd.DataFrame({"f": [1.0, 2.0, 3.0, 4.0]}, index=["s1", "s2", "s3", "s4"])
    s_pos = rank_to_score(raw, 1)
    s_neg = rank_to_score(raw, -1)
    assert list(s_pos["f"]) == sorted(s_pos["f"])  # 正向：值大者分高
    assert np.allclose(s_pos.to_numpy(), -s_neg.to_numpy())
    assert s_pos.to_numpy().max() <= 4 and s_pos.to_numpy().min() >= -4
    assert abs(float(s_pos["f"].mean())) < 1e-9  # 截面均值 ≈ 0


def test_rank_to_score_nan_preserved():
    raw = pd.DataFrame({"f": [1.0, np.nan, 3.0]}, index=["s1", "s2", "s3"])
    s = rank_to_score(raw, 1)
    assert np.isnan(s.loc["s2", "f"])
    assert not np.isnan(s.loc["s1", "f"])


# ---------------------------------------------------------------------------
# TTM
# ---------------------------------------------------------------------------
def _q(idx: list[str], vals: list[float], col: str = "v") -> pd.DataFrame:
    return pd.DataFrame({col: vals}, index=pd.to_datetime(idx))


def test_ttm_single_quarter_sums_four_consecutive():
    df = _q(
        ["2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31", "2026-03-31"],
        [10.0, 20.0, 30.0, 40.0, 5.0],
    )
    ttm = _ttm_single_quarter(df, ("v",))
    assert np.isnan(ttm["v"].iloc[2])  # 不足 4 季
    assert ttm["v"].iloc[3] == 100.0  # 10+20+30+40
    assert ttm["v"].iloc[4] == 95.0  # 20+30+40+5


def test_ttm_ytd_uses_prior_fy_and_same_quarter():
    # 茅台式 YTD：Q1=10, H1=30, 9M=50, FY=90；上年 FY=80、上年 Q1=8
    df = _q(
        ["2024-03-31", "2024-12-31", "2025-03-31", "2025-06-30"],
        [8.0, 80.0, 10.0, 30.0],
    )
    ttm = _ttm_ytd(df, ("v",))
    assert ttm["v"].iloc[1] == 80.0  # 年报即 TTM
    assert ttm["v"].iloc[2] == 80.0 + 10.0 - 8.0  # 82
    assert np.isnan(ttm["v"].iloc[0])  # 缺上年年报


# ---------------------------------------------------------------------------
# IC / 回测 / KPI
# ---------------------------------------------------------------------------
def _score_fwd(perfect: bool):
    rng = np.random.default_rng(3)
    scores = pd.DataFrame(
        rng.normal(size=(4, 30)), index=DATES, columns=[f"{i:04d}" for i in range(30)]
    )
    fwd = scores.copy()
    if not perfect:
        fwd = -fwd
    return scores, fwd


def test_ic_perfect_and_reversed():
    s, f = _score_fwd(perfect=True)
    ic = analysis.ic_series(s, f)
    assert ic.dropna().min() > 0.99
    s2, f2 = _score_fwd(perfect=False)
    ic2 = analysis.ic_series(s2, f2)
    assert ic2.dropna().max() < -0.99


def test_backtest_topn_first_period_cost_and_nav():
    scores = pd.DataFrame(
        [
            [3.0, 2.0, 1.0, 0.0],
            [3.0, 2.0, 1.0, 0.0],
            [3.0, 2.0, 1.0, 0.0],
            [np.nan] * 4,
        ],
        index=DATES,
        columns=["s1", "s2", "s3", "s4"],
    )
    fwd = pd.DataFrame(
        [
            [0.10, 0.05, 0.02, 0.01],
            [0.10, 0.05, 0.02, 0.01],
            [np.nan] * 4,
            [np.nan] * 4,
        ],
        index=DATES,
        columns=["s1", "s2", "s3", "s4"],
    )
    bt = analysis.backtest_topn(scores, fwd, top_n=2)
    # 首期：picks=[s1,s2]，turnover=1.0，成本 0.002；gross=(0.10+0.05)/2=0.075
    assert bt["turnover"].iloc[0] == 1.0
    exp_net = 0.075 - 0.002
    assert abs(bt["ret"].iloc[0] - exp_net) < 1e-9
    # 第二期持仓不变 → 换手 0、无成本
    assert bt["turnover"].iloc[1] == 0.0
    assert abs(bt["ret"].iloc[1] - 0.075) < 1e-9
    assert abs(bt["nav"].iloc[1] - (1 + exp_net) * (1 + 0.075)) < 1e-9


def test_kpi_basic():
    nav = pd.Series([1.0, 1.1, 1.05, 1.2], index=DATES)
    ret = nav.pct_change().fillna(0.0)
    k = analysis.kpi(ret, nav)
    assert k["n_months"] == 4
    assert k["max_drawdown"] is not None and 0 < k["max_drawdown"] < 0.1
    assert 0 <= k["win_rate"] <= 1


def test_leaderboard_ranks_by_composite():
    metrics = {
        "A": {"ic_mean": 0.10, "sharpe": 2.0, "annual_return": 0.3},
        "B": {"ic_mean": 0.01, "sharpe": 0.2, "annual_return": 0.05},
        "C": {"ic_mean": -0.09, "sharpe": 1.0, "annual_return": 0.1},  # 强反向
    }
    lb = analysis.leaderboard(metrics)
    assert lb[0]["code"] == "A" and lb[0]["rank"] == 1
    assert lb[0]["composite"] >= lb[-1]["composite"]


# ---------------------------------------------------------------------------
# scorecard（月末名次面板 → 任意 N / 任意区间）
# ---------------------------------------------------------------------------
PANEL_MONTHS = ["2026-01-31", "2026-02-28", "2026-03-31", "2026-04-30"]


def _mk_panel(factor_data: dict) -> pd.DataFrame:
    """factor_data: {code: {score: {sym: [每月]}, fwd: {sym: [每月]}, raw: 可选}}"""
    rows = []
    for code, d in factor_data.items():
        syms = list(d["score"])
        for mi, m in enumerate(PANEL_MONTHS):
            ranked = sorted(syms, key=lambda s: -d["score"][s][mi])
            for rk, s in enumerate(ranked, 1):
                rows.append(
                    {
                        "factor_code": code,
                        "trade_date": pd.Timestamp(m),
                        "rank": rk,
                        "symbol": s,
                        "score": d["score"][s][mi],
                        "raw": (d.get("raw") or {}).get(s, [0.0] * len(PANEL_MONTHS))[mi],
                        "fwd_ret": d["fwd"][s][mi],
                    }
                )
    return pd.DataFrame(rows)


def _const_panel() -> scorecard.Panel:
    fd = {
        "X": {
            "score": {"A": [3, 3, 3, 3], "B": [2, 2, 2, 2], "C": [1, 1, 1, 1]},
            "fwd": {
                "A": [0.10, 0.20, 0.30, None],
                "B": [0.04, 0.05, 0.06, None],
                "C": [-0.05, -0.05, -0.05, None],
            },
        }
    }
    return scorecard.Panel(_mk_panel(fd))


def test_panel_topn_returns_and_cost():
    p = _const_panel()
    mask = scorecard.month_mask(p.dates, None, None)
    s1 = scorecard.topn_series(p, p.index("X"), 1, mask)
    # 首月换手 100% → 10% − 0.2%；其后持仓不变换手 0
    assert abs(s1["ret"][0] - (0.10 - 0.002)) < 1e-5
    assert abs(s1["ret"][1] - 0.20) < 1e-5
    assert abs(s1["nav"][-1] - (1 + 0.10 - 0.002) * 1.2 * 1.3) < 1e-4
    s2 = scorecard.topn_series(p, p.index("X"), 2, mask)
    assert abs(s2["ret"][0] - ((0.10 + 0.04) / 2 - 0.002)) < 1e-5
    assert s2["nav"][-1] < s1["nav"][-1]  # 更强的个股集中在 top1


def _legacy_panel_matrix(df: pd.DataFrame) -> dict:
    """Panel.__init__ 的**旧实现**（冻结参照），用来钉住下标口径。

    旧版月份下标是「建字典 + 逐行 `di[x]` 查」的 Python 循环。2754 因子 × 3226 万行
    下这一段要 24.8s 且全程持 GIL（引擎健康检查被它饿死），已换成 searchsorted ——
    这里保留旧算法，逐元素比对两者结果。

    字典键直接用日期标量（原版写的是 `int(d)`，只在 datetime64[ns] 下成立；
    pandas 3 默认微秒分辨率、`int(datetime64[us])` 会抛，与本用例无关）。
    """
    tds = pd.to_datetime(df["trade_date"]).to_numpy()
    dates = np.unique(tds)
    codes = sorted(str(c) for c in df["factor_code"].unique())
    di = {d: i for i, d in enumerate(dates)}
    ci = {c: i for i, c in enumerate(codes)}
    k = int(df["rank"].max())
    f, m = len(codes), len(dates)
    out = {
        "fwd": np.full((f, m, k), np.nan, dtype=np.float32),
        "score": np.full((f, m, k), np.nan, dtype=np.float32),
        "raw": np.full((f, m, k), np.nan, dtype=np.float32),
        "sym_codes": np.full((f, m, k), -1, dtype=np.int32),
    }
    fi = df["factor_code"].astype(str).map(ci).to_numpy(dtype=int)
    mi = np.array([di[x] for x in tds], dtype=int)
    rk = df["rank"].to_numpy(dtype=int) - 1
    out["fwd"][fi, mi, rk] = df["fwd_ret"].to_numpy(dtype=np.float32)
    out["score"][fi, mi, rk] = df["score"].to_numpy(dtype=np.float32)
    out["raw"][fi, mi, rk] = df["raw"].to_numpy(dtype=np.float32)
    cats = df["symbol"].astype(str).astype("category").cat
    out["symbols"] = [str(c) for c in cats.categories]
    out["sym_codes"][fi, mi, rk] = cats.codes.to_numpy(dtype=np.int32)
    out["codes"], out["dates"] = codes, dates
    return out


def test_panel_month_index_matches_legacy_implementation():
    """面板构造逐元素等价于旧实现 —— 含缺档、乱序、多因子。

    缺档（某月 rank 不连续）是重点：新写法用 searchsorted 定位月份，一旦 dates 与 tds
    的口径对不上，下标会**静默错位**（不像旧版逐行查字典会 KeyError），所以必须整表比对。
    """
    rows = []
    months = ["2026-01-31", "2026-02-28", "2026-03-31", "2026-04-30"]
    for code in ("A", "B"):
        for mi, mon in enumerate(months):
            syms = ["000001.SZ", "600000.SH", "300750.SZ"]
            if mi % 2 == 1:
                syms = ["000001.SZ", "300750.SZ"]  # 缺 rank=2 → 该格保持初始填充
            for rk, sym in enumerate(syms, 1):
                rows.append(
                    {
                        "factor_code": code,
                        "trade_date": pd.Timestamp(mon),
                        "rank": rk,
                        "symbol": sym,
                        "score": float(10 - rk - mi),
                        "raw": float(rk) * 0.5,
                        "fwd_ret": 0.01 * rk,
                    }
                )
    df = pd.DataFrame(rows).sample(frac=1.0, random_state=7)  # 打乱行序

    p = scorecard.Panel(df)
    ref = _legacy_panel_matrix(df)

    assert list(p.codes) == ref["codes"] == ["A", "B"]
    assert np.array_equal(p.dates, ref["dates"])
    assert p.symbols == ref["symbols"]
    for key in ("fwd", "score", "raw"):
        np.testing.assert_array_equal(getattr(p, key), ref[key], err_msg=key)
    np.testing.assert_array_equal(p._sym_codes, ref["sym_codes"])
    # 参与量与缺位量都写死：2 因子 × 4 月 × 3 名次槽 = 24 槽，其中 20 槽有数据、4 槽缺档。
    # 全 NaN（没比上）或全填满（没覆盖缺档）都说明用例本身失效，而不是代码对。
    assert len(df) == np.isfinite(p.score).sum() == 20, "实际落格数与行数必须一致"
    assert (p._sym_codes == -1).sum() == 4, "缺档月份应留下空位，否则没测到 rank 断裂"


def _legacy_holdings_profile(p, fi: int, n: int, snap_idx):
    """`_holdings_profile` 的旧实现（冻结参照）：逐行 `snap_idx.loc[sym]` 取整行。

    排行榜要给 2754 个因子各查 30 只持仓 = 82,620 次 `.loc`，每次现造一个 Series，
    实测 7.7s —— 已改成预先把两列转成 dict 再查。这里留旧实现做等价对照。
    """
    last = len(p.dates) - 1
    mvs: list[float] = []
    inds: dict[str, int] = {}
    for sym in p.sym_at(fi, last, n):
        if snap_idx is None or sym not in snap_idx.index:
            continue
        row = snap_idx.loc[sym]
        mv = row.get("total_mv_yi")
        if mv is not None and np.isfinite(mv):
            mvs.append(float(mv))
        ind = row.get("industry")
        if isinstance(ind, str) and ind:
            inds[ind] = inds.get(ind, 0) + 1
    med = round(float(np.median(mvs)), 1) if mvs else None
    style = None
    if med is not None:
        style = "大盘" if med >= 500 else ("中盘" if med >= 100 else "小盘")
    return (
        med,
        style,
        [{"name": k, "count": v} for k, v in sorted(inds.items(), key=lambda kv: -kv[1])[:3]],
    )


def test_holdings_profile_matches_legacy_loc_path():
    """持仓画像改走 dict 查表后口径不变（市值 NaN、缺行业、不在快照里都要照旧处理）。"""
    p = _const_panel()
    snap = pd.DataFrame(
        [
            {"symbol": "A", "total_mv_yi": 620.0, "industry": "银行"},
            {"symbol": "B", "total_mv_yi": np.nan, "industry": None},  # 市值坏、行业缺
            # C 故意不在快照里
        ]
    )
    snap_idx = snap.set_index("symbol")
    snap_mv, snap_ind = service._snapshot_maps(snap)

    assert sorted(snap_mv) == ["A", "B"], f"市值表键集不对：{sorted(snap_mv)}"
    assert snap_mv["A"] == 620.0
    assert np.isnan(snap_mv["B"]), "NaN 市值要原样保留，由 _holdings_profile 过滤"
    assert snap_ind["A"] == "银行"
    assert not isinstance(snap_ind["B"], str), "缺行业不能落成字符串，否则会被计进行业分布"

    got = service._holdings_profile(p, p.index("X"), 3, snap_mv, snap_ind)
    ref = _legacy_holdings_profile(p, p.index("X"), 3, snap_idx)

    assert got == ref, f"新旧口径不一致：{got} != {ref}"
    # 只有 A 的市值可用 → 中位 620 → 大盘；行业只有银行一条
    assert got == (620.0, "大盘", [{"name": "银行", "count": 1}])


def test_ic_stats_from_matches_long_table_entry():
    """预分组索引 `ic_index`/`ic_stats_from` 与直接查长表逐字段等价。

    排行榜原来对每个因子都 `ic[ic["factor_code"] == code]` 全表扫一遍，
    2746 因子 × 21.2 万行实测 22.2s（占排行榜 26.7s 的大头）。改走索引后必须同口径。
    """
    months = pd.to_datetime(PANEL_MONTHS)
    rows = []
    for code, base in (("A", 0.05), ("B", -0.02)):
        for i, m in enumerate(months):
            rows.append({"factor_code": code, "trade_date": m, "ic": base + 0.01 * i})
    ic = pd.DataFrame(rows)
    idx = scorecard.ic_index(ic)
    mask_dates = np.asarray(months)

    assert sorted(idx) == ["A", "B"], f"索引没覆盖全部因子：{sorted(idx)}"

    for code in ("A", "B"):
        direct = scorecard.ic_stats(ic, code, mask_dates)
        indexed = scorecard.ic_stats_from(idx.get(code), mask_dates)
        assert direct == indexed, f"{code} 两种入口口径不一致"
        assert direct["ic_mean"] is not None, "没算出来，等于在比两个 None"

    empty = {"ic_mean": None, "ic_std": None, "ic_ir": None, "ic_win_rate": None}
    # 区间不含任何月份 / 因子不存在，四个入口都要给同一份空结果
    empty_mask = np.asarray(pd.to_datetime(["2030-01-31"]))
    assert [
        scorecard.ic_stats(ic, "A", empty_mask),
        scorecard.ic_stats_from(idx.get("A"), empty_mask),
        scorecard.ic_stats(ic, "NOPE", mask_dates),
        scorecard.ic_stats_from(idx.get("NOPE"), mask_dates),
    ] == [empty] * 4


def test_month_mask_slices_range():
    p = _const_panel()
    mask = scorecard.month_mask(p.dates, "2026-02", "2026-03")
    assert int(mask.sum()) == 2
    s = scorecard.topn_series(p, p.index("X"), 1, mask)
    # 区间首月（2 月）全额计费持有到 3 月：20% − 0.2%
    assert len(s["ret"]) == 1
    assert abs(s["ret"][0] - (0.20 - 0.002)) < 1e-5
    # 4 月不在区间内 → 不参与
    mask2 = scorecard.month_mask(p.dates, "2026-04", None)
    assert int(mask2.sum()) == 1


def test_nscan_orders_by_n():
    p = _const_panel()
    mask = scorecard.month_mask(p.dates, None, None)
    rows = scorecard.nscan(p, p.index("X"), mask, max_n=3)
    assert [r["n"] for r in rows] == [1, 2, 3]
    assert rows[0]["final_nav"] > rows[2]["final_nav"]  # top1 > top3（C 是拖累）


def test_composite_scores_direction():
    df = pd.DataFrame(
        [
            {"ic_mean": 0.08, "ic_ir": 0.5, "annual_return": 0.25, "sharpe": 1.5, "max_drawdown": 0.15, "win_rate": 0.6},
            {"ic_mean": -0.05, "ic_ir": -0.3, "annual_return": -0.15, "sharpe": -0.8, "max_drawdown": 0.45, "win_rate": 0.4},
        ]
    )
    out = scorecard.composite_scores(df)
    assert out.loc[0, "composite"] > out.loc[1, "composite"]
    assert out.loc[0, "eff_z"] > 0 and out.loc[1, "eff_z"] < 0


def test_env_tags_bull_regime():
    n = 18
    bench = np.array([0.001] * 6 + [0.06] * (n - 6))
    ra = bench + 0.02  # 牛市里超额更突出
    rb = bench - 0.02
    env = scorecard.env_tags({"A": ra, "B": rb}, bench)
    assert env["A"] == "牛市进攻型"
    assert env["B"] == "全天候型"


def test_time_tags_recent_shift():
    dates = pd.date_range("2024-01-31", periods=24, freq="ME")
    rows = []
    for i, d in enumerate(dates):
        rows.append({"trade_date": d, "factor_code": "UP", "ic": 0.02 if i < 12 else 0.05})
        rows.append({"trade_date": d, "factor_code": "DOWN", "ic": 0.05 if i < 12 else 0.02})
        rows.append({"trade_date": d, "factor_code": "IDLE", "ic": 0.004})
    df = pd.DataFrame(rows)
    tags = scorecard.time_tags(df, ["UP", "DOWN", "IDLE"], dates.to_numpy())
    assert tags["UP"] == "近期转强"
    assert tags["DOWN"] == "近期失效"
    assert tags["IDLE"] == "持续低效"


def test_weight_grid_valid():
    from backend.services.engine.factor_research.service import _weight_grid

    for k in (2, 3, 5):
        grid = _weight_grid(k)
        assert 5 <= len(grid) <= 900
        for w in grid:
            assert abs(sum(w) - 1.0) < 1e-9
            assert all(x >= 0 for x in w)
