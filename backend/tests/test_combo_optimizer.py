"""组合权重优化器（combo_optimizer，P2 组合实验室）。

口径（与计划书一致，本文件钉死）：
- ``w = u / Σ|u|``（L1 归一，允许负权=反向暴露）；
- 目标 = **train 窗内日均 rank-IC 最大**（对 u 的正缩放不变）；
- train/valid 按日期**时序** 70/30——随机拆分会让同日前视泄漏；
- 日采样上限默认 120（linspace，沿 pool_panels.sample_days 同款）；
- 固定 seed 可复现（seed 随 train_metrics.config 落库）；时间预算到点截断时
  ``config.converged=False``（scipy 回调 StopIteration → success=False），
  「可复现」只在 converged=True 时成立——截断运行必须自曝，不许冒充收敛；
- 面板自带的前瞻收益列（fret）在所有选中因子上必须一致（同市场同口径），
  不一致宁可报错不硬算——把不同前瞻期混在一起算的 IC 是假指标。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backend.services.engine.mining_plugins import combo_optimizer as co
from backend.services.engine.mining_plugins.combo_optimizer import (
    ComboConfig,
    build_dataset,
    combo_values,
    daily_rank_ic,
    optimize_combo,
    resolve_cost_rate,
    split_dataset,
    window_metrics,
)

SYMBOLS = [f"SYM{i}" for i in range(8)]


def _days(n: int) -> list[str]:
    return [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2025-01-06", periods=n)]


def _panel(
    days, symbols, z: np.ndarray, fret: np.ndarray, tag: str = ""
) -> pd.DataFrame:
    """长表面板：trade_date/symbol/rank_pct/zscore/fret（rank_pct 优化器不用，凑格式）。"""
    n_days, n_sym = len(days), len(symbols)
    assert z.shape == (n_days, n_sym) and fret.shape == (n_days, n_sym)
    frame = pd.DataFrame(
        {
            "trade_date": np.repeat(days, n_sym),
            "symbol": np.tile(symbols, n_days),
            "rank_pct": z.ravel() * 0 + 0.5,
            "zscore": z.ravel().astype(np.float32),
            "fret": fret.ravel().astype(np.float32),
        }
    )
    if tag:
        frame["tag"] = tag
    return frame


def _two_factor_panels(n_days: int = 50, seed: int = 7, noise: float = 0.3):
    """t1/t2 两个独立真信号，fret = 0.05·t1 − 0.03·t2；观测带噪。

    单因子都不如组合：方向可回收（w1>0、w2<0），组合 IC 明显高于任一单因子。
    """
    rng = np.random.default_rng(seed)
    shape = (n_days, len(SYMBOLS))
    t1 = rng.normal(size=shape)
    t2 = rng.normal(size=shape)
    z1 = t1 + noise * rng.normal(size=shape)
    z2 = t2 + noise * rng.normal(size=shape)
    fret = 0.05 * t1 - 0.03 * t2 + 0.0005 * rng.normal(size=shape)
    days = _days(n_days)
    return {
        "f1": _panel(days, SYMBOLS, z1, fret),
        "f2": _panel(days, SYMBOLS, z2, fret),
    }, days


class TestBuildDataset:
    def test_intersects_symbols_and_days(self):
        days = _days(30)
        z = np.ones((30, 8))
        f1 = _panel(days, SYMBOLS, z, z)
        f2 = _panel(days, SYMBOLS[:5], np.ones((30, 5)), np.ones((30, 5)))
        ds = build_dataset({"f1": f1, "f2": f2}, ["f1", "f2"], max_days=120)
        assert set(ds["symbol"].unique()) == set(SYMBOLS[:5])
        assert len(ds) == 30 * 5
        assert [c for c in ds.columns if c.startswith("z")] == ["z0", "z1"]
        assert "fret" in ds.columns

    def test_missing_fret_column_rejected(self):
        days = _days(30)
        z = np.ones((30, 8))
        bad = _panel(days, SYMBOLS, z, z).drop(columns=["fret"])
        with pytest.raises(ValueError, match="缺.*收益列"):
            build_dataset(
                {"f1": bad, "f2": _panel(days, SYMBOLS, z, z)},
                ["f1", "f2"],
                max_days=120,
            )

    def test_inconsistent_fret_rejected(self):
        days = _days(30)
        z = np.ones((30, 8))
        f1 = _panel(days, SYMBOLS, z, z)
        f2 = _panel(days, SYMBOLS, z, z + 0.01)  # 不同前瞻期/复权口径的假象
        with pytest.raises(ValueError, match="前瞻收益.*不一致"):
            build_dataset({"f1": f1, "f2": f2}, ["f1", "f2"], max_days=120)

    def test_min_comb_symbols_days_guards(self):
        days = _days(10)  # < MIN_COMBO_DAYS
        z = np.ones((10, 8))
        with pytest.raises(ValueError, match="样本不足"):
            build_dataset(
                {"f1": _panel(days, SYMBOLS, z, z), "f2": _panel(days, SYMBOLS, z, z)},
                ["f1", "f2"],
                max_days=120,
            )

    def test_single_factor_rejected(self):
        days = _days(30)
        z = np.ones((30, 8))
        with pytest.raises(ValueError, match="至少"):
            build_dataset({"f1": _panel(days, SYMBOLS, z, z)}, ["f1"], max_days=120)

    def test_max_days_sampling_is_deterministic_and_keeps_ends(self):
        days = _days(300)
        z = np.ones((300, 8))
        panel = _panel(days, SYMBOLS, z, z)
        ds1 = build_dataset({"f1": panel, "f2": panel}, ["f1", "f2"], max_days=120)
        ds2 = build_dataset({"f1": panel, "f2": panel}, ["f1", "f2"], max_days=120)
        got = sorted(ds1["trade_date"].unique())
        assert len(got) == 120
        assert got == sorted(ds2["trade_date"].unique())
        assert got[0] == days[0] and got[-1] == days[-1]


class TestSplit:
    def test_chronological_70_30_no_overlap(self):
        frames, days = _two_factor_panels(n_days=50)
        ds = build_dataset(frames, ["f1", "f2"], max_days=120)
        train, valid = split_dataset(ds, train_ratio=0.7)
        tr_days = sorted(train["trade_date"].unique())
        va_days = sorted(valid["trade_date"].unique())
        assert len(tr_days) == 35 and len(va_days) == 15
        assert max(tr_days) < min(va_days)  # 时序拆分，绝不交叉
        assert not set(tr_days) & set(va_days)


class TestRankICAndMetrics:
    def test_daily_rank_ic_matches_manual_spearman(self):
        days = _days(20)  # ≥ MIN_COMBO_DAYS（闸门在 build 侧）
        z = np.array([[3.0, 1.0, 2.0, 0.0]] * 20)
        fret = np.array([[1.0, 3.0, 2.0, 0.0]] * 20)
        panel = _panel(days, SYMBOLS[:4], z, fret)
        ds = build_dataset({"f1": panel, "f2": panel}, ["f1", "f2"], max_days=120)
        ic = daily_rank_ic(ds, combo_values(ds, [1.0, 0.0]))
        assert len(ic) == 20
        assert ic.iloc[0] == pytest.approx(0.2)  # [4,2,3,1] vs [2,4,3,1] 的 Spearman

    def test_static_signal_zero_turnover(self):
        days = _days(30)
        cross = np.linspace(-1, 1, len(SYMBOLS))[None, :].repeat(30, axis=0)
        fret = 0.05 * cross
        panel = _panel(days, SYMBOLS, cross, fret)
        ds = build_dataset({"f1": panel, "f2": panel}, ["f1", "f2"], max_days=120)
        m = window_metrics(ds, [0.7, -0.3], resolve_cost_rate(ComboConfig()))
        assert m["ann_turnover"] == pytest.approx(0.0, abs=1e-9)
        assert m["mean_rank_ic"] == pytest.approx(1.0, abs=1e-6)  # 单调同序
        assert m["n_days"] > 0

    def test_cost_rate_monotone_on_rotating_signal(self):
        """日频翻转的多头名单：费率越高净收益越低（扣成本口径有效）。"""
        days = _days(40)
        base = np.linspace(-1, 1, len(SYMBOLS))[None, :].repeat(40, axis=0)
        sign = np.where(np.arange(40)[:, None] % 2 == 0, 1.0, -1.0)
        z = base * sign
        fret = 0.05 * base.repeat(1, 1)
        panel = _panel(days, SYMBOLS, z, fret)
        frames = {"f1": panel, "f2": panel}
        ds = build_dataset(frames, ["f1", "f2"], max_days=120)
        low = window_metrics(ds, [1.0, 0.0], 0.001)
        high = window_metrics(ds, [1.0, 0.0], 0.02)
        assert low["ann_turnover"] > 0.5
        assert high["ann_return_net"] < low["ann_return_net"]

    def test_default_cost_rate_single_source(self):
        from backend.services.engine.factor_research.analysis import COST_RATE

        assert resolve_cost_rate(ComboConfig()) == COST_RATE
        assert resolve_cost_rate(ComboConfig(cost_rate=0.01)) == 0.01


class TestOptimize:
    def test_recovers_direction_beats_singles_and_l1_normalized(self):
        frames, days = _two_factor_panels(n_days=60)
        cfg = ComboConfig(maxiter=60, popsize=10, seed=11)
        result = optimize_combo(frames, ["f1", "f2"], config=cfg)

        w = result["weights"]
        assert set(w) == {"f1", "f2"}
        assert sum(abs(v) for v in w.values()) == pytest.approx(1.0, abs=1e-9)
        assert w["f1"] > 0 and w["f2"] < 0  # fret = +0.05·t1 − 0.03·t2 → 方向可回收

        ds = build_dataset(frames, ["f1", "f2"], max_days=cfg.max_days)
        _, valid = split_dataset(ds, cfg.train_ratio)
        combo_ic = result["valid_metrics"]["mean_rank_ic"]
        ic1 = window_metrics(valid, [1.0, 0.0], resolve_cost_rate(cfg))["mean_rank_ic"]
        ic2 = window_metrics(valid, [0.0, 1.0], resolve_cost_rate(cfg))["mean_rank_ic"]
        assert combo_ic > max(ic1, ic2)

    def test_fixed_seed_reproducible(self):
        frames, _ = _two_factor_panels(n_days=50)
        cfg = ComboConfig(maxiter=40, popsize=8, seed=3)
        r1 = optimize_combo(frames, ["f1", "f2"], config=cfg)
        r2 = optimize_combo(frames, ["f1", "f2"], config=cfg)
        assert r1["weights"] == r2["weights"]

    def test_time_budget_truncation_marks_not_converged(self):
        """时间预算到点截断 → converged=False（scipy 回调 StopIteration 语义）。

        复现性验收「同 seed 重跑一致」只在 converged=True 时成立：截断运行
        必须用 config.converged=False 自曝，否则同 seed 重跑不一致会被误读
        成确定性缺陷（2026-10-08 复现事故的排查教训）。
        """
        frames, _ = _two_factor_panels(n_days=50)
        cfg = ComboConfig(maxiter=40, popsize=8, seed=3, time_budget_s=0.0)
        result = optimize_combo(frames, ["f1", "f2"], config=cfg)

        w = result["weights"]
        assert sum(abs(v) for v in w.values()) == pytest.approx(1.0, abs=1e-9)
        tr = result["train_metrics"]["config"]
        assert tr["converged"] is False
        assert tr["time_budget_s"] == 0.0

    def test_result_contract_curve_and_config(self):
        frames, days = _two_factor_panels(n_days=50)
        cfg = ComboConfig(maxiter=30, popsize=6, seed=5)
        result = optimize_combo(frames, ["f1", "f2"], config=cfg)

        assert set(result) == {
            "weights",
            "train_metrics",
            "valid_metrics",
            "train_window",
        }
        tr, va = result["train_metrics"], result["valid_metrics"]
        for metrics in (tr, va):
            assert metrics["n_days"] > 0
            assert metrics["mean_rank_ic"] is not None
            assert np.isfinite(metrics["mean_rank_ic"])
        assert tr["config"]["seed"] == 5
        assert tr["config"]["cost_rate"] == resolve_cost_rate(cfg)

        curve = va["curve"]
        # 扣成本净值与评估器同口径：valid 首日无「昨日名单」不计成本 → 曲线少一天
        assert len(curve["dates"]) == len(curve["values"]) == va["n_days"] - 1
        assert all(isinstance(v, float) for v in curve["values"][:3])
        assert curve["dates"] == sorted(curve["dates"])
        # 少的是首日不是尾日：曲线终点 = valid 窗最后一天
        ds = build_dataset(frames, ["f1", "f2"], max_days=cfg.max_days)
        _, valid = split_dataset(ds, cfg.train_ratio)
        assert curve["dates"][-1] == sorted(valid["trade_date"].unique())[-1]

    def test_max_factors_guard(self):
        frames, days = _two_factor_panels(n_days=30)
        many = {f"f{i}": frames["f1"] for i in range(co.MAX_FACTORS + 1)}
        with pytest.raises(ValueError, match="因子数"):
            optimize_combo(
                many, list(many), config=ComboConfig(maxiter=5, popsize=4, seed=1)
            )
