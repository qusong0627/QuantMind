"""compute_psi_drift / _psi_single 单元测试。

覆盖双通道 PSI（水平 + 截面 rank）：
- 水平 PSI 保持原有语义（量纲敏感）
- 截面 rank PSI 对"整体水平平移"不敏感（消除牛市量能伪影）
- 截面 rank PSI 对"截面结构变化"敏感（真实的风格/排序漂移）
- 输出字段兼容原有前端消费（overall/max_psi/drift/top_drift_features）
"""

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd


def _load_training_module():
    root = Path(__file__).resolve().parents[2]
    module_path = root / "docker" / "training" / "train.py"
    # train.py 是**训练容器**的入口，import 的是同目录下的 `model_trainers.*` 包。
    # 主容器里 docker/training 不在 sys.path 上 → 收集期 ImportError 会中断整个 unit 套件。
    training_dir = str(module_path.parent)
    if training_dir not in sys.path:
        sys.path.insert(0, training_dir)
    spec = importlib.util.spec_from_file_location("quant_training_train", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


mod = _load_training_module()
# compute_psi_drift 由 train.py 转出（同时也验证了这层转出还在）；
# _psi_single / _rank_displacement 是 drift.py 的私有实现，P1 拆包后 train.py 不再转出
# 它们 → 向属主模块取（train.py 只转出 compute_psi_drift 这一个公开名）。
from diagnostics.drift import _psi_single, _rank_displacement  # noqa: E402

compute_psi_drift = mod.compute_psi_drift


# ─────────────────────────────────────────────────────────────
# 基础 PSI
# ─────────────────────────────────────────────────────────────
def test_psi_identical_distribution_is_zero():
    a = np.random.default_rng(0).normal(0, 1, 2000)
    psi = _psi_single(a, a)
    assert abs(psi) < 0.01


def test_psi_sharp_level_shift_is_large():
    rng = np.random.default_rng(1)
    a = rng.normal(0, 1, 2000)
    b = rng.normal(10, 1, 2000)  # 水平整体右移
    psi = _psi_single(a, b)
    assert psi > 0.5


def test_psi_constant_features_return_zero():
    a = np.full(500, 5.0)
    b = np.full(500, 5.0)
    assert _psi_single(a, b) == 0.0


def test_psi_small_samples_return_nan():
    a = np.random.default_rng(2).normal(0, 1, 10)
    b = np.random.default_rng(3).normal(0, 1, 10)
    assert np.isnan(_psi_single(a, b))


# ─────────────────────────────────────────────────────────────
# 截面 rank PSI：良性量纲膨胀不应触发
# ─────────────────────────────────────────────────────────────
def _make_df(rng, n_days, n_stocks, train_fn, recent_fn):
    """构造 (trade_date, symbol, feat) 数据框。"""
    rows = []
    dates = pd.date_range("2026-01-01", periods=n_days, freq="B")
    syms = [f"S{i:04d}" for i in range(n_stocks)]
    for d in dates:
        for s in syms:
            rows.append((d, s, 0.0))
    df = pd.DataFrame(rows, columns=["trade_date", "symbol", "feat"])
    # 按日截面赋值
    for i, d in enumerate(dates):
        is_recent = i >= n_days - 5  # 最后 5 天为 recent 段
        base = train_fn if not is_recent else recent_fn
        df.loc[df["trade_date"] == d, "feat"] = base(rng, n_stocks)
    return df


def _test_rank_psi(level_shift, structure_shift, n_stocks=400):
    """返回 (level_psi, rank_psi)。"""
    rng = np.random.default_rng(42)
    # 基准每日截面：按 symbol 序的 rank 信息
    sym_order = np.arange(n_stocks)

    def base_order(rng, n):
        # 每日截面用稳定的基准序 + 少量噪声 → 训练段排序结构稳定
        return sym_order + rng.normal(0, 10, n)

    def shifted_level(rng, n):
        # 整体水平翻倍（量纲膨胀），排序结构不变
        return base_order(rng, n) * (1 + level_shift)

    def shifted_structure(rng, n):
        # 排序结构反转（真实结构漂移）
        return -base_order(rng, n)

    df = _make_df(rng, 20, n_stocks, base_order, base_order)
    train_mask = df["trade_date"] < df["trade_date"].max() - pd.Timedelta(days=4)
    train_df = df[train_mask]
    recent_df = df[~train_mask]

    level_psi = _psi_single(
        train_df["feat"].to_numpy(), recent_df["feat"].to_numpy()
    )
    rank_psi = _psi_single(
        train_df.groupby("trade_date")["feat"].rank(pct=True).to_numpy(),
        recent_df.groupby("trade_date")["feat"].rank(pct=True).to_numpy(),
    )
    return level_psi, rank_psi


def test_level_shift_does_not_trigger_rank_psi():
    """整体水平膨胀 → 水平 PSI 大，但截面 rank 位移应接近 0。"""
    rng = np.random.default_rng(42)
    n_stocks = 400
    dates = pd.date_range("2026-01-01", periods=20, freq="B")
    syms = [f"S{i:04d}" for i in range(n_stocks)]
    rows = [(d, s, 0.0) for d in dates for s in syms]
    df = pd.DataFrame(rows, columns=["trade_date", "symbol", "feat"])
    base = np.arange(n_stocks) + rng.normal(0, 10, n_stocks)
    for i, d in enumerate(dates):
        is_recent = i >= 15
        val = base * 3.0 if is_recent else base  # recent 整体 3 倍
        df.loc[df["trade_date"] == d, "feat"] = val

    train_df = df[df["trade_date"] < dates[15]]
    recent_df = df[df["trade_date"] >= dates[15]]

    level_psi = _psi_single(train_df["feat"].to_numpy(), recent_df["feat"].to_numpy())
    rank_disp = _rank_displacement(train_df, recent_df, "feat")

    assert level_psi > 0.5  # 水平明显漂移
    assert rank_disp < 0.05  # 但截面结构稳定


def test_structure_shift_triggers_rank_psi():
    """个股截面位置重排（风格切换）→ 截面 rank 位移应显著。"""
    rng = np.random.default_rng(42)
    n_stocks = 400
    dates = pd.date_range("2026-01-01", periods=20, freq="B")
    syms = [f"S{i:04d}" for i in range(n_stocks)]
    rows = [(d, s, 0.0) for d in dates for s in syms]
    df = pd.DataFrame(rows, columns=["trade_date", "symbol", "feat"])
    # 训练段：前一半股票值高（排序前 50%）
    base = np.concatenate([np.linspace(100, 50, n_stocks // 2), np.linspace(50, 0, n_stocks // 2)])
    base = base + rng.normal(0, 1, n_stocks)
    for i, d in enumerate(dates):
        is_recent = i >= 15
        if is_recent:
            # recent 段：后一半股票值高（风格完全切换）
            val = np.concatenate([np.linspace(0, 50, n_stocks // 2), np.linspace(100, 50, n_stocks // 2)])
            val = val + rng.normal(0, 1, n_stocks)
        else:
            val = base
        df.loc[df["trade_date"] == d, "feat"] = val

    train_df = df[df["trade_date"] < dates[15]]
    recent_df = df[df["trade_date"] >= dates[15]]

    rank_disp = _rank_displacement(train_df, recent_df, "feat")

    assert rank_disp > 0.2


# ─────────────────────────────────────────────────────────────
# compute_psi_drift 整体
# ─────────────────────────────────────────────────────────────
def _make_market_df(n_days=120, n_stocks=300, recent_mult=2.0, structure_shift=False):
    """构造完整市场 df：若干特征，recent 段可选量能放大或结构重排。"""
    rng = np.random.default_rng(7)
    dates = pd.date_range("2026-01-01", periods=n_days, freq="B")
    syms = [f"S{i:04d}" for i in range(n_stocks)]
    df = pd.DataFrame([(d, s) for d in dates for s in syms], columns=["trade_date", "symbol"])
    half = n_stocks // 2
    for d in dates:
        is_recent = d >= dates[-8]
        base = rng.normal(0, 1, n_stocks)
        if is_recent:
            if structure_shift:
                # 结构漂移：前一半股票 +3、后一半股票 -3 → 截面身份重排
                base = np.concatenate([base[:half] + 3.0, base[half:] - 3.0])
            else:
                # 良性量能膨胀：整体放量，身份不变
                base = base * recent_mult
        df.loc[df["trade_date"] == d, "amount_ma_5"] = base
        # 一个纯噪声特征：水平不变
        df.loc[df["trade_date"] == d, "noise_feat"] = rng.normal(0, 1, n_stocks)
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    return df


def test_compute_psi_drift_basic_schema():
    df = _make_market_df()
    res = compute_psi_drift(df, ["amount_ma_5", "noise_feat"], "2026-01-01", "2026-03-01", n_recent_days=8)
    assert res["enabled"] is True
    # 兼容字段
    for k in ["overall", "max_psi", "drift", "top_drift_features", "train_start", "train_end", "recent_start", "recent_end"]:
        assert k in res
    assert res["drift"]["stable"] + res["drift"]["medium"] + res["drift"]["severe"] == 2
    assert all("psi" in f and "rank_disp" in f for f in res["top_drift_features"])


def test_compute_psi_drift_level_shift_is_benign():
    """整体量能膨胀（无结构变化）→ overall 不应判为 severe。"""
    df = _make_market_df(recent_mult=3.0, structure_shift=False)
    res = compute_psi_drift(df, ["amount_ma_5", "noise_feat"], "2026-01-01", "2026-03-01", n_recent_days=8)
    assert res["overall"] in ("stable", "warning")


def test_compute_psi_drift_structure_change_is_severe():
    """截面结构变化（一半股票升/一半降，身份重排）→ overall 应判 severe。"""
    df = _make_market_df(recent_mult=1.0, structure_shift=True)
    res = compute_psi_drift(df, ["amount_ma_5", "noise_feat"], "2026-01-01", "2026-03-01", n_recent_days=8)
    assert res["overall"] == "severe"


def test_compute_psi_drift_reports_benign_scale_shift():
    """整体量能膨胀（身份不变）→ 特征应标记 benign_scale=True 且 level 非 severe。"""
    df = _make_market_df(recent_mult=3.0, structure_shift=False)
    res = compute_psi_drift(df, ["amount_ma_5", "noise_feat"], "2026-01-01", "2026-03-01", n_recent_days=8)
    feats = {f["feature"]: f for f in res["top_drift_features"]}
    assert feats["amount_ma_5"]["benign_scale"] is True
    assert feats["amount_ma_5"]["level"] in ("stable", "medium")


def test_rank_displacement_unreliable_does_not_mask_drift():
    """rank_disp 不可估计（股票交集过小）→ 不得静默归 0 判良性，须标 unreliable。"""
    # 构造 recent 段全是新股（与 train 无交集）的 df
    rng = np.random.default_rng(9)
    dates = pd.date_range("2026-01-01", periods=30, freq="B")
    train_syms = [f"T{i:04d}" for i in range(200)]
    recent_syms = [f"R{i:04d}" for i in range(200)]  # 与 train 完全不同的代码体系
    rows = [(d, s, 0.0) for d in dates[:25] for s in train_syms] + [(d, s, 0.0) for d in dates[25:] for s in recent_syms]
    df = pd.DataFrame(rows, columns=["trade_date", "symbol", "feat"])
    for d in dates[:25]:
        df.loc[df["trade_date"] == d, "feat"] = rng.normal(0, 1, len(train_syms))
    for d in dates[25:]:
        df.loc[df["trade_date"] == d, "feat"] = rng.normal(5, 1, len(recent_syms))  # 水平右移

    train_df = df[df["trade_date"] < dates[25]]
    recent_df = df[df["trade_date"] >= dates[25]]
    rank_disp = _rank_displacement(train_df, recent_df, "feat")
    assert np.isnan(rank_disp)  # 交集为空 → nan

    # compute_psi_drift 整体：水平漂移显著但 rank 不可估计 → 不应因置 0 判 stable
    # train_end 设在 recent 段之前，避免 recent 被包进 train（否则交集重叠、rank 退化）
    res = compute_psi_drift(df, ["feat"], "2026-01-01", "2026-02-03", n_recent_days=5)
    feat = res["top_drift_features"][0]
    assert feat["rank_reliable"] is False
    assert feat["benign_scale"] is False  # 不可估计时不标良性
    assert feat["level"] in ("medium", "severe")  # 按水平 PSI 保守判级


def test_max_psi_reflects_structure_not_level():
    """max_psi 应输出最大结构漂移（rank_disp），而非水平 PSI。"""
    rng = np.random.default_rng(11)
    n_stocks = 400
    dates = pd.date_range("2026-01-01", periods=20, freq="B")
    syms = [f"S{i:04d}" for i in range(n_stocks)]
    rows = [(d, s, 0.0) for d in dates for s in syms]
    df = pd.DataFrame(rows, columns=["trade_date", "symbol", "feat"])
    base = np.arange(n_stocks) + rng.normal(0, 10, n_stocks)
    for i, d in enumerate(dates):
        is_recent = i >= 15
        val = base * 5.0 if is_recent else base  # 水平膨胀但身份不变
        df.loc[df["trade_date"] == d, "feat"] = val

    res = compute_psi_drift(df, ["feat"], "2026-01-01", "2026-02-15", n_recent_days=5)
    feat = res["top_drift_features"][0]
    # 水平 PSI 大（膨胀）但 rank_disp 小 → max_psi 反映的是 rank_disp
    assert feat["psi"] > 0.5
    assert res["max_psi"] < 0.1
    assert abs(res["max_psi"] - feat["rank_disp"]) < 1e-4


def test_stationary_autocorrelated_market_not_severe():
    """回归测试：平稳（无漂移）但截面 rank 强自相关的长历史市场，
    旧实现拿长训练窗均值对短 recent 窗会把采样噪声误判为严重漂移。
    修复后应判 stable（至多 warning），且输出噪声本底与自适应标记。"""
    rng = np.random.default_rng(123)
    n_days, n_stocks = 160, 300
    dates = pd.date_range("2026-01-01", periods=n_days, freq="B")
    syms = [f"S{i:04d}" for i in range(n_stocks)]
    df = pd.DataFrame([(d, s) for d in dates for s in syms], columns=["trade_date", "symbol"])
    # 每股 AR(1) 平稳过程：截面排序缓慢游走（强自相关），但任何窗口间无系统漂移
    for feat in ["mom_ret_20d", "fun_pe", "turn_20"]:
        vals = np.empty((n_days, n_stocks))
        x = rng.normal(0, 1, n_stocks)
        for i in range(n_days):
            x = 0.9 * x + np.sqrt(1 - 0.9**2) * rng.normal(0, 1, n_stocks)
            vals[i] = x
        df[feat] = vals.ravel()
    df["trade_date"] = pd.to_datetime(df["trade_date"])

    res = compute_psi_drift(
        df, ["mom_ret_20d", "fun_pe", "turn_20"],
        str(dates[0].date()), str(dates[-16].date()), n_recent_days=15,
    )
    assert res["enabled"] is True
    assert res["adaptive_thresholds"] is True
    assert res["noise_floor"] is not None and res["noise_floor"] > 0
    assert res["overall"] in ("stable", "warning")
    assert res["drift"]["severe"] == 0
