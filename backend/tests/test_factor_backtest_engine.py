"""T-FB-04 单测：求值核心（因子 × 市场 → 指标 + 序列 + 终态）。

qlib 读写与子进程执行全部走**缝**（``_ensure_qlib`` / ``_load_features`` /
``_liquid_top_n`` / 子进程执行器）打桩——本文件钉的是**状态机与装配接线**：
- ``data_unsupported`` 静态缺列 **绝不执行**（免烧算力）；
- ``unavailable``（provider 未就绪）不跑；
- ``insufficient``（有效日 < min_days）不给评分（诚实降级）；
- ``ok`` 的指标与 ``ic.py`` 同源、成本已扣、序列载荷齐备；
- CN 走挖掘同源富化 + 评估器链；HK 走动态流动性池；
- 执行异常 → ``failed``（带格式化原因）；取消异常**向上抛**（路由层收口）。
"""

import numpy as np
import pandas as pd
import pytest

from backend.services.engine.factor_backtest import engine as E
from backend.services.engine.factor_backtest import profiles as P

pytestmark = pytest.mark.unit

_FACTOR = {
    "factor_id": "f-eng-1",
    "factor_code": 'def calculate_factor(df):\n    return df["$close"]\n',
}


def _make_fake_provider(tmp_path) -> str:
    """最小可用 qlib provider 目录布局（profile_status 要 real 读文件面）。"""
    prov = tmp_path / "prov"
    (prov / "calendars").mkdir(parents=True)
    (prov / "calendars" / "day.txt").write_text("2020-01-02\n2026-10-08\n")
    (prov / "instruments").mkdir(parents=True)
    (prov / "instruments" / "all.txt").write_text("us_t0\t2020-01-02\t2026-10-08\n")
    feat = prov / "features" / "us_t0"
    feat.mkdir(parents=True)
    for col in ("close", "open", "amount", "factor"):
        (feat / f"{col}.day.bin").write_bytes(b"")
    return str(prov)


def _fake_panel(n_days=140, n_inst=6):
    """合成行情：每日随机截面收益（σ=1%）复利成价格；返回 (df, f, r_true)。

    df 索引 (instrument, datetime)——qlib D.features 的层序；
    f 索引 (datetime, instrument)——因子产出侧层序（对齐逻辑会规整）。
    """
    dates = pd.bdate_range("2024-01-01", periods=n_days)
    insts = [f"us_t{i}" for i in range(n_inst)]
    idx_q = pd.MultiIndex.from_product([insts, dates], names=["instrument", "datetime"])
    rng = np.random.default_rng(7)
    rets = pd.Series(rng.normal(0.0, 0.01, size=len(idx_q)), index=idx_q)
    close = 10.0 * (1.0 + rets).groupby(level="instrument").cumprod()
    df = pd.DataFrame({"$close": close})
    for col in ("$open", "$high", "$low", "$volume", "$amount", "$factor"):
        df[col] = 1.0
    r_true = close.groupby(level="instrument").shift(-1) / close - 1.0
    r_sorted = r_true.swaplevel().sort_index().dropna()
    f = r_sorted + rng.normal(scale=1e-4, size=len(r_sorted))
    f = f.sort_index()
    f.index.names = ["datetime", "instrument"]
    return df, f, r_true


def _install_ok_seams(monkeypatch, df, f_series, *, captured=None):
    monkeypatch.setattr(E, "_ensure_qlib", lambda q: None)
    # 基准读数（T-FB-19）是外部数据面 IO（QuantDB parquet）：默认打桩不可用
    # → 回落等权（既有断言的基线）；基准接线用例自行覆写本缝。
    monkeypatch.setattr(E, "load_benchmark_returns", lambda bench, dates: None)
    # universe 解析是外部 IO 边界（生产走 D.instruments/QuantDB）；默认打桩，
    # 个别用例覆写以断言分派（CN 池 / HK 动态池 / all）。
    monkeypatch.setattr(E, "_resolve_instruments_for_universe", lambda mu, u: ["us_t0"])

    def fake_features(instruments, fields, start, end):
        if captured is not None:
            captured["instruments"] = instruments
            captured["fields"] = fields
        return df

    monkeypatch.setattr(E, "_load_features", fake_features)

    async def fake_exec_functional(factor_id, code, df_in, source_h5=None):
        if captured is not None:
            captured["source_h5"] = source_h5
        return f_series

    monkeypatch.setattr(E, "_run_functional_factor_subprocess", fake_exec_functional)

    async def fake_exec_class(factor_id, code, df_in):
        return f_series

    monkeypatch.setattr(E, "_run_factor_class_subprocess", fake_exec_class)


# ── 终态：静态缺列免跑 ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_data_unsupported_skips_execution(monkeypatch):
    """静态缺列（富化列在非 CN 不存在）→ data_unsupported，执行器绝不启动。"""
    # Arrange
    called = {"exec": False}

    async def boom_exec(*a, **kw):
        called["exec"] = True
        raise AssertionError("不得执行")

    monkeypatch.setattr(E, "_run_functional_factor_subprocess", boom_exec)
    factor = {
        "factor_id": "f-x",
        "factor_code": 'def calculate_factor(df):\n    return df["$netflow_5"]\n',
    }
    # Act
    res = await E.evaluate_factor_market(factor, market="us_stock")
    # Assert
    assert res["status"] == "data_unsupported"
    assert res["compat"]["missing"] == ["$netflow_5"]
    assert called["exec"] is False
    assert res["metrics"] is None and res["series"] is None


@pytest.mark.asyncio
async def test_unavailable_when_provider_missing(tmp_path, monkeypatch):
    """provider 未就绪 → unavailable（不跑），reason 说明数据面缺失。"""
    # Arrange
    monkeypatch.setattr(
        P, "resolve_qlib_provider_uri", lambda m: str(tmp_path / "nope")
    )
    # Act
    res = await E.evaluate_factor_market(_FACTOR, market="hong_kong")
    # Assert
    assert res["status"] == "unavailable"
    assert res["reason"] == "provider_not_ready"
    assert res["metrics"] is None


# ── 终态：kind 判不出 / 执行失败 ─────────────────────────────────────


@pytest.mark.asyncio
async def test_unknown_kind_fails_before_execution(monkeypatch):
    """无 calculate_/Factor 类/main 守卫 → failed，不启动 qlib 与子进程。"""
    # Arrange
    monkeypatch.setattr(
        E, "_ensure_qlib", lambda q: (_ for _ in ()).throw(AssertionError("不得初始化"))
    )
    factor = {"factor_id": "f-y", "factor_code": "x = 1"}
    # Act
    res = await E.evaluate_factor_market(factor, market="a_share")
    # Assert
    assert res["status"] == "failed"
    assert res["reason"] == "unknown_factor_kind"


@pytest.mark.asyncio
async def test_execution_error_maps_to_failed_with_reason(monkeypatch, tmp_path):
    """执行器抛错 → failed（原因格式化后的文本非空）。"""
    # Arrange
    prov = _make_fake_provider(tmp_path)
    monkeypatch.setattr(P, "resolve_qlib_provider_uri", lambda m: prov)
    df, f_series, _ = _fake_panel()
    _install_ok_seams(monkeypatch, df, f_series)

    async def boom_exec(*a, **kw):
        raise RuntimeError("因子计算无输出")

    monkeypatch.setattr(E, "_run_functional_factor_subprocess", boom_exec)
    # Act
    res = await E.evaluate_factor_market(_FACTOR, market="us_stock")
    # Assert
    assert res["status"] == "failed"
    assert res["reason"] == "execution_error"
    assert "因子计算无输出" in res["message"]


# ── 终态：有效日不足 ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_insufficient_when_too_few_days(monkeypatch, tmp_path):
    """有效日 < min_days（默认 120）→ insufficient，不给评分与序列。"""
    # Arrange
    prov = _make_fake_provider(tmp_path)
    monkeypatch.setattr(P, "resolve_qlib_provider_uri", lambda m: prov)
    df, f_series, _ = _fake_panel(n_days=30)
    _install_ok_seams(monkeypatch, df, f_series)
    # Act
    res = await E.evaluate_factor_market(_FACTOR, market="us_stock")
    # Assert
    assert res["status"] == "insufficient"
    assert res["n_days"] == 29
    assert res["metrics"] is None and res["series"] is None


# ── ok 路径：指标 + 序列 + 接线 ──────────────────────────────────────


@pytest.mark.asyncio
async def test_ok_path_metrics_series_and_fields(monkeypatch, tmp_path):
    """正常路径：ok，指标齐备（含成本口径）、序列载荷齐备、字段含 $amount。"""
    # Arrange
    prov = _make_fake_provider(tmp_path)
    monkeypatch.setattr(P, "resolve_qlib_provider_uri", lambda m: prov)
    df, f_series, _ = _fake_panel()
    captured = {}
    _install_ok_seams(monkeypatch, df, f_series, captured=captured)
    # Act
    res = await E.evaluate_factor_market(_FACTOR, market="us_stock")
    # Assert
    assert res["status"] == "ok"
    m = res["metrics"]
    assert m["ic"] > 0.9  # f ≈ r_true，IC 应接近 1
    assert m["n_days"] >= 120
    assert m["cost_bps"] == 10  # T-FB-19 费率审计：美股 10bps 双边
    assert m["ann_return_net"] < m["ann_return"]
    assert res["series"]["meta"]["cost_bps"] == 10
    assert len(res["series"]["dates"]) == len(res["series"]["nav_long"])
    assert "$amount" in captured["fields"]  # 旧路径漏了它——新引擎必须带上
    assert res["compat"]["status"] == "portable"
    assert res["kind"] == "functional"
    assert res["data_source"] == "qlib_bin"
    assert res["universe"] == "all"
    assert res["window"]["end"] and res["window"]["start"]


# ── 基准接线（T-FB-19）──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_benchmark_index_used_when_available(monkeypatch, tmp_path):
    """指数读数可用 → 基准列换成指数收益；载荷与指标记真实口径。"""
    # Arrange
    prov = _make_fake_provider(tmp_path)
    monkeypatch.setattr(P, "resolve_qlib_provider_uri", lambda m: prov)
    df, f_series, _ = _fake_panel()
    _install_ok_seams(monkeypatch, df, f_series)
    seen = {}

    def fake_bench(bench, dates):
        seen["bench"] = bench
        idx = pd.to_datetime(pd.Index(dates))
        return pd.Series(0.002, index=idx)  # 每日 +0.2% 的合成指数

    monkeypatch.setattr(E, "load_benchmark_returns", fake_bench)
    # Act
    res = await E.evaluate_factor_market(_FACTOR, market="us_stock")
    # Assert
    assert res["status"] == "ok"
    assert seen["bench"] == "spx"  # 档案请求的美股基准
    assert res["series"]["bench"] == "spx"
    assert res["metrics"]["bench_used"] == "spx"
    assert res["metrics"]["benchmark"] == "spx"
    nav_bench = res["series"]["nav_bench"]
    # 前向收益口径：轴内每一天（含首日）都有一个次日收益，nav = Π(1+r) 从首日即开始
    assert nav_bench[-1] == pytest.approx(1.002 ** len(nav_bench), rel=1e-9)


@pytest.mark.asyncio
async def test_benchmark_falls_back_to_equal_weight_when_unavailable(
    monkeypatch, tmp_path
):
    """指数读数不可用（None）→ 保持等权兜底，实际口径如实标注、绝不冒充。"""
    # Arrange
    prov = _make_fake_provider(tmp_path)
    monkeypatch.setattr(P, "resolve_qlib_provider_uri", lambda m: prov)
    df, f_series, _ = _fake_panel()
    _install_ok_seams(monkeypatch, df, f_series)  # 基准缝默认 None = 读数失败
    # Act
    res = await E.evaluate_factor_market(_FACTOR, market="us_stock")
    # Assert：请求 spx 未遂 → 载荷与指标都记 equal_weight
    assert res["status"] == "ok"
    assert res["series"]["bench"] == "equal_weight"
    assert res["metrics"]["bench_used"] == "equal_weight"
    assert res["metrics"]["benchmark"] == "spx"


@pytest.mark.asyncio
async def test_non_cn_uses_all_instruments_and_no_mining_source(monkeypatch, tmp_path):
    """非 CN：instruments = all（无挖掘富化 h5），source_h5=None。"""
    # Arrange
    prov = _make_fake_provider(tmp_path)
    monkeypatch.setattr(P, "resolve_qlib_provider_uri", lambda m: prov)
    df, f_series, _ = _fake_panel()
    captured = {}
    _install_ok_seams(monkeypatch, df, f_series, captured=captured)
    monkeypatch.setattr(E, "_resolve_instruments_for_universe", lambda mu, u: "ALL_SEL")
    monkeypatch.setattr(E, "_resolve_mining_source_h5", lambda m: "/no/such.h5")
    # Act
    res = await E.evaluate_factor_market(_FACTOR, market="us_stock")
    # Assert
    assert res["status"] == "ok"
    assert captured["instruments"] == "ALL_SEL"
    assert captured["source_h5"] is None  # 非 CN 恒无挖掘同源（旧实现同判据）


@pytest.mark.asyncio
async def test_cn_uses_csi300_and_mining_source(monkeypatch, tmp_path):
    """CN：universe=csi300 走池解析；富化 h5 命中时透传；评估器链并入 evaluators。"""
    # Arrange
    prov = _make_fake_provider(tmp_path)
    monkeypatch.setattr(P, "resolve_qlib_provider_uri", lambda m: prov)
    df, f_series, _ = _fake_panel()
    captured = {}
    _install_ok_seams(monkeypatch, df, f_series, captured=captured)
    seen = {}

    def fake_like_cn(market_upper, universe):
        seen["market_upper"], seen["universe"] = market_upper, universe
        return ["SH600036", "SH600519"]

    monkeypatch.setattr(E, "_resolve_instruments_for_universe", fake_like_cn)
    monkeypatch.setattr(
        E, "_resolve_mining_source_h5", lambda m: "/cache/daily_pv_all.h5"
    )

    def fake_evaluators(f_clean, r_clean, *, market, universe, factor_id):
        seen["eval_market"] = market
        return {"rre": 0.42}

    monkeypatch.setattr(E, "_run_mining_evaluators", fake_evaluators)
    # Act
    res = await E.evaluate_factor_market(_FACTOR, market="a_share")
    # Assert
    assert res["status"] == "ok"
    assert seen == {
        "market_upper": "CN",
        "universe": "csi300",
        "eval_market": "a_share",
    }
    assert captured["instruments"] == ["SH600036", "SH600519"]
    assert captured["source_h5"] == "/cache/daily_pv_all.h5"
    assert res["metrics"]["evaluators"] == {"rre": 0.42}


@pytest.mark.asyncio
async def test_hk_liquid_top_n_universe(monkeypatch, tmp_path):
    """HK：动态流动性池（top-N 列表传入 D.features），universe 记为默认池名。"""
    # Arrange
    prov = _make_fake_provider(tmp_path)
    monkeypatch.setattr(P, "resolve_qlib_provider_uri", lambda m: prov)
    df, f_series, _ = _fake_panel()
    captured = {}
    _install_ok_seams(monkeypatch, df, f_series, captured=captured)
    seen = {}

    def fake_top_n(qlib_market, top_n, end):
        seen["args"] = (qlib_market, top_n, end)
        return ["hk_0001.hk", "hk_0002.hk"]

    monkeypatch.setattr(E, "_liquid_top_n", fake_top_n)
    # Act
    res = await E.evaluate_factor_market(_FACTOR, market="hong_kong")
    # Assert
    assert res["status"] == "ok"
    assert seen["args"][0] == "HK" and seen["args"][1] == 500
    assert captured["instruments"] == ["hk_0001.hk", "hk_0002.hk"]
    assert res["universe"] == "liquid_top500"


@pytest.mark.asyncio
async def test_cancelled_propagates(monkeypatch, tmp_path):
    """取消异常必须向上抛（路由层负责把台账收口为 cancelled）。"""
    # Arrange
    prov = _make_fake_provider(tmp_path)
    monkeypatch.setattr(P, "resolve_qlib_provider_uri", lambda m: prov)
    df, f_series, _ = _fake_panel()
    _install_ok_seams(monkeypatch, df, f_series)
    from backend.services.engine.routers.alpha_agent import FactorBacktestCancelled

    async def cancel_exec(*a, **kw):
        raise FactorBacktestCancelled("回测已被用户取消")

    monkeypatch.setattr(E, "_run_functional_factor_subprocess", cancel_exec)
    # Act / Assert
    with pytest.raises(FactorBacktestCancelled):
        await E.evaluate_factor_market(_FACTOR, market="us_stock")


@pytest.mark.asyncio
async def test_class_factor_dispatches_to_class_executor(monkeypatch, tmp_path):
    """Factor 类代码 → factor_class 执行器（与函数式分流）。"""
    # Arrange
    prov = _make_fake_provider(tmp_path)
    monkeypatch.setattr(P, "resolve_qlib_provider_uri", lambda m: prov)
    df, f_series, _ = _fake_panel()
    _install_ok_seams(monkeypatch, df, f_series)
    called = {"cls": False, "fn": False}

    async def fake_cls(factor_id, code, df_in):
        called["cls"] = True
        return f_series

    async def fake_fn(*a, **kw):
        called["fn"] = True
        return None

    monkeypatch.setattr(E, "_run_factor_class_subprocess", fake_cls)
    monkeypatch.setattr(E, "_run_functional_factor_subprocess", fake_fn)
    factor = {
        "factor_id": "f-cls",
        "factor_code": "class MyFactor:\n    name = 'x'\n    def __call__(self, df):\n        return df['$close']\n",
    }
    # Act
    res = await E.evaluate_factor_market(factor, market="us_stock")
    # Assert
    assert res["status"] == "ok"
    assert res["kind"] == "factor_class"
    assert called == {"cls": True, "fn": False}


# ── T-FB-05 拆股形态收益掩码 ─────────────────────────────────────────


def test_split_like_returns_masked_with_counts():
    """整数比形态（拆股/反向拆股）掩码，非整数比真实暴动保留。

    数值样例取自 2026-10-09 美股全池校准清单：掩码侧 = 真拆股形态
    （NVDA -0.8993→10、CMG -0.6605→3、WMT -0.5→2、反向 +4.0068→5），
    保留侧 = SBNY 危机期真实暴动（+2.2、+1.6296、-0.5429 均非整数比）。
    """
    # Arrange
    values = [-0.8993, -0.6605, -0.5, 0.95, 4.0068, 2.2, -0.4, 1.6296, -0.5429, 0.01]
    r = pd.Series(values)
    # Act
    out, masked, kept = E._mask_split_like_returns(r)
    # Assert
    assert masked == 5
    assert kept == 4  # |ret|≥0.4 且非整数比：+2.2 / -0.4 / +1.6296 / -0.5429
    flagged = out.isna().tolist()
    assert flagged == [True, True, True, True, True, False, False, False, False, False]
    # 保留值与常规值原样不动
    assert out.iloc[5] == 2.2 and out.iloc[9] == 0.01


def test_split_mask_boundaries_nan_and_liquidation():
    """边界与退化值：k=2 容差边缘、NaN、-100% 亏光不误判。"""
    # Arrange —— k=2 的容差窗为 ret ∈ [-0.52381, -0.47368]
    r = pd.Series([-0.52, -0.47, -1.0, float("nan"), 0.9, 0.89])
    # Act
    out, masked, _ = E._mask_split_like_returns(r)
    # Assert
    assert masked == 2  # -0.52（窗内）、+0.9（k=1.9 恰在 5% 窗内）
    assert not np.isnan(out.iloc[1])  # -0.47 出窗保留
    assert out.iloc[2] == -1.0  # 亏光不是拆股形态（比例无穷），原样保留
    assert np.isnan(out.iloc[3])  # NaN 原样
    assert out.iloc[5] == 0.89  # 未达 +90% 判别门槛


def test_split_mask_preserves_index_and_scales_to_engine():
    """掩码保索引（只置 NaN 不删行）；无命中时序列不变。"""
    # Arrange
    idx = pd.MultiIndex.from_tuples(
        [("2024-01-02", "us_a"), ("2024-01-03", "us_a")],
        names=["datetime", "instrument"],
    )
    r = pd.Series([0.01, -0.9], index=idx)
    # Act
    out, masked, kept = E._mask_split_like_returns(r)
    # Assert
    assert masked == 1 and kept == 0
    assert list(out.index) == list(r.index)
    assert out.iloc[0] == 0.01 and np.isnan(out.iloc[1])
    same, m2, k2 = E._mask_split_like_returns(pd.Series([0.01, 0.02]))
    assert m2 == 0 and k2 == 0 and same.tolist() == [0.01, 0.02]


@pytest.mark.asyncio
async def test_engine_masks_split_bar_in_metrics_and_coverage(monkeypatch, tmp_path):
    """引擎集成：面板里嵌入一根 10:1 拆股假收益（-90%）→
    指标暴露 suspect_returns_masked=1、当日覆盖数减一、IC 不被污染。"""
    # Arrange
    prov = _make_fake_provider(tmp_path)
    monkeypatch.setattr(P, "resolve_qlib_provider_uri", lambda m: prov)
    df, f_series, _ = _fake_panel()  # n_days=140, n_inst=6
    n_inst = 6
    dates = pd.bdate_range("2024-01-01", periods=140)
    split_day = dates[-30]
    # us_t0 自 split_day 起整体 ×0.1（拆股后价格）→ 前一交易日的前向收益 ≈ -90%
    rows = (df.index.get_level_values("datetime") >= split_day) & (
        df.index.get_level_values("instrument") == "us_t0"
    )
    df.loc[rows, "$close"] = df.loc[rows, "$close"] * 0.1
    _install_ok_seams(monkeypatch, df, f_series)
    # Act
    res = await E.evaluate_factor_market(_FACTOR, market="us_stock")
    # Assert
    assert res["status"] == "ok"
    m = res["metrics"]
    assert m["suspect_returns_masked"] == 1
    assert m["extreme_returns_kept"] == 0
    assert m["ic"] > 0.9  # 假 bar 掩码后 IC 不被污染（f 与 r 其余全同）
    # 拆股日（前向收益口径 = 分割日前最后一个交易日）覆盖数减一
    series = res["series"]
    fake_day = str(dates[-31].date())
    cov = dict(zip(series["dates"], series["coverage"], strict=True))
    assert cov[fake_day] == n_inst - 1
