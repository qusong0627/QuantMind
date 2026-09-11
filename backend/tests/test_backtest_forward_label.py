"""回测评估正确性回归测试。

核心防护对象：历史 bug —— backtest_service 用 `mom_ret_{N}d`（**过去** N 日收益，
且本身就在多数模型的 feature_columns 里）当作 "实际未来收益" 来算 IC。
实测该 bug 使 IC 虚高约 15 倍（+0.646 vs 真实 +0.041）。
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

_INFERENCE_DIR = Path(__file__).resolve().parents[1] / "services" / "engine" / "inference"


def _load_module(name: str):
    """直接按文件加载，绕过 inference/__init__ 的 DB 依赖。"""
    full = f"_bt_test_{name}"
    if full in sys.modules:
        return sys.modules[full]
    if "_bt_test_pkg" not in sys.modules:
        pkg = types.ModuleType("_bt_test_pkg")
        pkg.__path__ = [str(_INFERENCE_DIR)]
        sys.modules["_bt_test_pkg"] = pkg
    spec = importlib.util.spec_from_file_location(full, _INFERENCE_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[full] = module
    # data_loader 内部 `from .trading_cost import ...`，需要先注册成同一包的子模块
    sys.modules[f"_bt_test_pkg.{name}"] = module
    module.__package__ = "_bt_test_pkg"
    spec.loader.exec_module(module)
    return module


trading_cost = _load_module("trading_cost")


@pytest.fixture
def synthetic_parquet(tmp_path: Path) -> Path:
    """3 只股票 × 20 个交易日的合成行情，close 单调可预测。"""
    dates = pd.bdate_range("2026-01-05", periods=20).strftime("%Y-%m-%d").tolist()
    rows = []
    for idx, symbol in enumerate(["SH600000", "SZ000001", "SZ300750"], start=1):
        for day, date in enumerate(dates):
            close = 100.0 * idx * (1.0 + 0.01 * day)
            rows.append(
                {
                    "symbol": symbol,
                    "trade_date": date,
                    "close": close,
                    "volume": 1_000_000.0,
                    "is_st": 0,
                    "pctchange": 0.01,
                    "listing_market": "沪市主板" if idx == 1 else "创业板",
                    # 过去 5 日收益，故意与前瞻标签不同
                    "mom_ret_5d": 0.05 if day >= 5 else 0.0,
                }
            )
    data_dir = tmp_path / "feature_snapshots"
    data_dir.mkdir()
    pd.DataFrame(rows).to_parquet(data_dir / "model_features_2026.parquet", index=False)
    return data_dir


# ── 前瞻标签正确性（核心 bug 防护） ────────────────────────────────────────


@pytest.mark.unit
def test_forward_label_is_future_return(synthetic_parquet: Path) -> None:
    # Arrange
    data_loader = _load_module("data_loader")
    horizon = 3
    raw = pd.read_parquet(synthetic_parquet / "model_features_2026.parquet")
    dates = sorted(raw["trade_date"].unique())

    # Act — 本用例守卫纯前瞻公式，显式关闭 T+1 执行日偏移（默认值为 1）
    labels = data_loader.load_forward_labels(
        dates=dates, horizon=horizon, data_dir=synthetic_parquet,
        signal_lag_days=0,
    )

    # Assert — 标签必须等于 close[T+N]/close[T]-1
    expected = raw.sort_values(["symbol", "trade_date"]).copy()
    expected["want"] = (
        expected.groupby("symbol")["close"].shift(-horizon) / expected["close"] - 1
    )
    merged = labels.merge(
        expected[["symbol", "trade_date", "want"]], on=["symbol", "trade_date"]
    )
    assert len(merged) == len(labels)
    assert np.allclose(merged["fwd_return"], merged["want"])

    # 末尾 horizon 个交易日不可能有标签 —— 这是判断实现正确的关键信号
    assert labels["trade_date"].max() == dates[-1 - horizon]


@pytest.mark.unit
def test_forward_label_signal_lag_uses_execution_close(synthetic_parquet: Path) -> None:
    """A 股 T+1 约定：signal_lag_days=1 时标签键仍为信号日 T，但收益按
    执行日成交：fwd[T] = close[T+1+N]/close[T+1] - 1（与生产调用方
    inference/backtest_service.py 传入方式一致）。"""
    data_loader = _load_module("data_loader")
    horizon, lag = 3, 1
    raw = pd.read_parquet(synthetic_parquet / "model_features_2026.parquet")
    dates = sorted(raw["trade_date"].unique())

    labels = data_loader.load_forward_labels(
        dates=dates, horizon=horizon, data_dir=synthetic_parquet,
        signal_lag_days=lag,
    )

    # 实现契约：键仍是信号日 T，close 被替换为执行日 T+lag 的收盘价，
    # 因此 fwd[T] = close[T+lag+H]/close[T+lag] - 1（行内直接对位，无需移日期）
    expected = raw.sort_values(["symbol", "trade_date"]).copy()
    expected["want_exec"] = (
        expected.groupby("symbol")["close"].shift(-(horizon + lag))
        / expected.groupby("symbol")["close"].shift(-lag)
        - 1
    )
    expected = expected.dropna(subset=["want_exec"])
    merged = labels.merge(
        expected[["symbol", "trade_date", "want_exec"]],
        on=["symbol", "trade_date"],
    )
    assert len(merged) == len(labels)
    assert np.allclose(merged["fwd_return"], merged["want_exec"])
    # 最后一个可执行信号日 = dates[-1-horizon-lag]
    assert labels["trade_date"].max() == dates[-1 - horizon - lag]


@pytest.mark.unit
def test_forward_label_differs_from_backward_momentum(synthetic_parquet: Path) -> None:
    """回归防护：标签绝不能等于 mom_ret_{N}d（过去收益）。"""
    # Arrange
    data_loader = _load_module("data_loader")
    raw = pd.read_parquet(synthetic_parquet / "model_features_2026.parquet")
    dates = sorted(raw["trade_date"].unique())

    # Act
    labels = data_loader.load_forward_labels(
        dates=dates, horizon=5, data_dir=synthetic_parquet
    )
    merged = labels.merge(
        raw[["symbol", "trade_date", "mom_ret_5d"]], on=["symbol", "trade_date"]
    )

    # Assert
    assert not np.allclose(merged["fwd_return"], merged["mom_ret_5d"])


@pytest.mark.unit
def test_forward_label_excludes_nonpositive_close(tmp_path: Path) -> None:
    """close<=0 的停牌/退市行会让 pct_change 产出 inf，必须提前剔除。"""
    # Arrange
    data_loader = _load_module("data_loader")
    dates = pd.bdate_range("2026-01-05", periods=8).strftime("%Y-%m-%d").tolist()
    rows = [
        {
            "symbol": "SH600000",
            "trade_date": date,
            "close": 0.0 if day == 3 else 100.0 + day,
            "volume": 1_000.0,
            "is_st": 0,
        }
        for day, date in enumerate(dates)
    ]
    data_dir = tmp_path / "fs"
    data_dir.mkdir()
    pd.DataFrame(rows).to_parquet(data_dir / "model_features_2026.parquet", index=False)

    # Act
    labels = data_loader.load_forward_labels(dates=dates, horizon=2, data_dir=data_dir)

    # Assert
    assert np.isfinite(labels["fwd_return"]).all()
    assert dates[3] not in set(labels["trade_date"])


# ── 涨跌停过滤 ────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_limit_up_rows_excluded_when_enabled() -> None:
    # Arrange
    data_loader = _load_module("data_loader")
    df = pd.DataFrame(
        [
            # 主板 ±10%：涨停
            {"symbol": "A", "close": 10.0, "volume": 1.0, "is_st": 0,
             "pctchange": 0.0999, "listing_market": "沪市主板"},
            # 主板正常
            {"symbol": "B", "close": 10.0, "volume": 1.0, "is_st": 0,
             "pctchange": 0.03, "listing_market": "沪市主板"},
            # 创业板 ±20%：10% 不算涨停
            {"symbol": "C", "close": 10.0, "volume": 1.0, "is_st": 0,
             "pctchange": 0.10, "listing_market": "创业板"},
            # 主板跌停
            {"symbol": "D", "close": 10.0, "volume": 1.0, "is_st": 0,
             "pctchange": -0.0998, "listing_market": "深市主板"},
        ]
    )

    # Act
    kept = data_loader.filter_untradable_rows(df, exclude_limit_moves=True)
    kept_all = data_loader.filter_untradable_rows(df, exclude_limit_moves=False)

    # Assert
    assert set(kept["symbol"]) == {"B", "C"}
    # 默认关闭时不改变推理路径的既有行为
    assert set(kept_all["symbol"]) == {"A", "B", "C", "D"}


# ── 交易成本模型 ──────────────────────────────────────────────────────────


@pytest.mark.unit
def test_cost_model_resolution_priority() -> None:
    # Arrange
    meta = {"context": {"commission_rate": 0.0003, "slippage": 0.002}}
    override = {"slippage": 0.005}

    # Act
    default = trading_cost.CostModel.resolve()
    from_meta = trading_cost.CostModel.resolve(meta)
    from_override = trading_cost.CostModel.resolve(meta, override)

    # Assert
    assert default.commission_rate == 0.00025
    assert from_meta.commission_rate == 0.0003  # metadata 覆盖默认
    assert from_meta.slippage == 0.002
    assert from_override.slippage == 0.005  # override 覆盖 metadata
    assert from_override.commission_rate == 0.0003  # 未被 override 的保留 metadata 值


@pytest.mark.unit
def test_cost_model_ignores_invalid_values() -> None:
    # Arrange / Act
    model = trading_cost.CostModel.resolve(
        {"context": {"commission_rate": "bad", "slippage": -1, "stamp_duty": None}}
    )

    # Assert — 非法值不得污染成本，全部回退默认
    assert model.commission_rate == 0.00025
    assert model.slippage == 0.001
    assert model.stamp_duty == 0.001


@pytest.mark.unit
def test_round_trip_cost_magnitude() -> None:
    """A 股默认费率下往返成本应在 0.30%~0.36%。"""
    # Arrange
    model = trading_cost.CostModel()

    # Act
    cost = model.round_trip_cost()
    cost_sh = model.round_trip_cost(is_sh=True)

    # Assert
    assert 0.0030 <= cost <= 0.0036
    assert cost_sh > cost  # 沪市多收过户费
    # 佣金双边 + 滑点双边 + 印花税单边
    assert cost == pytest.approx(0.00025 * 2 + 0.001 * 2 + 0.001)


@pytest.mark.unit
def test_price_limit_by_market() -> None:
    # Assert
    assert trading_cost.price_limit_for_market("沪市主板") == 0.10
    assert trading_cost.price_limit_for_market("创业板") == 0.20
    assert trading_cost.price_limit_for_market("科创板") == 0.20
    assert trading_cost.price_limit_for_market("北交所") == 0.30
    # 未知板块保守回退主板
    assert trading_cost.price_limit_for_market(None) == 0.10


# ── 统计指标 ──────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_newey_west_t_stat_is_nonzero_for_biased_series() -> None:
    """旧实现 shuffle 已算好的 IC 序列，均值对置换不变 ⇒ t_stat 恒为 0。"""
    # Arrange
    backtest = _load_module("backtest_service")
    ic_series = [0.05, 0.04, 0.06, 0.03, 0.05, 0.07, 0.04, 0.05]

    # Act
    t_no_lag = backtest._newey_west_t_stat(ic_series, lag=0)
    t_with_lag = backtest._newey_west_t_stat(ic_series, lag=3)

    # Assert
    assert t_no_lag is not None and abs(t_no_lag) > 2
    assert t_with_lag is not None
    # 复刻旧实现，确认它必然给出 0
    arr = np.array(ic_series)
    rng = np.random.RandomState(42)
    shuffled_means = []
    for _ in range(100):
        s = arr.copy()
        rng.shuffle(s)
        shuffled_means.append(float(np.nanmean(s)))
    assert float(np.nanmean(shuffled_means)) == pytest.approx(float(arr.mean()))


@pytest.mark.unit
def test_newey_west_t_stat_handles_degenerate_input() -> None:
    # Arrange
    backtest = _load_module("backtest_service")

    # Assert
    assert backtest._newey_west_t_stat([], lag=0) is None
    assert backtest._newey_west_t_stat([0.05], lag=0) is None
    # 零方差序列无法定义 t 值
    assert backtest._newey_west_t_stat([0.05, 0.05, 0.05, 0.05], lag=1) is None


@pytest.mark.unit
def test_max_drawdown_is_relative_not_absolute() -> None:
    """旧实现返回算术差（peak - r），未除峰值净值。"""
    # Arrange
    backtest = _load_module("backtest_service")
    curve = [0.0, 0.5, 0.2]  # 净值 1.0 → 1.5 → 1.2

    # Act
    dd = backtest._max_drawdown(curve)

    # Assert — 相对回撤 = (1.5-1.2)/1.5 = 20%，而非绝对差 0.3
    assert dd == pytest.approx(0.3 / 1.5)
    assert backtest._max_drawdown([]) == 0.0


@pytest.mark.unit
def test_annualized_sharpe_scales_with_sample_interval() -> None:
    """旧实现恒用 sqrt(252/1)，忽略采样间隔。"""
    # Arrange
    backtest = _load_module("backtest_service")
    returns = [0.01, 0.02, -0.005, 0.015, 0.008, 0.012, -0.002, 0.011]

    # Act
    daily = backtest._annualized_sharpe(returns, sample_interval=1, holding_days=1)
    every_20 = backtest._annualized_sharpe(returns, sample_interval=20, holding_days=1)

    # Assert — 采样越稀疏，年化周期数越少，Sharpe 越小
    assert abs(daily) > abs(every_20)
    assert every_20 == pytest.approx(daily / np.sqrt(20), rel=0.05)
