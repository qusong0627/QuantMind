import asyncio
import os
import sys
import types
import json
from pathlib import Path

import pytest

project_root = os.path.join(os.path.dirname(__file__), "../../")
sys.path.append(project_root)

from backend.services.engine.qlib_app.services.backtest_service import QlibBacktestService
from backend.services.engine.qlib_app.services.risk_analyzer import RiskAnalyzer
from backend.services.engine.qlib_app.services import risk_analyzer as risk_analyzer_module


def test_normalize_signal_config_rejects_module_path_only_dict():
    service = QlibBacktestService()
    signal = {"module_path": "backend.services.engine.qlib_app.utils.simple_signal"}
    assert service._normalize_signal_config(signal) == "$close"


def test_build_signal_data_rejects_module_path_only_signal_dict():
    service = QlibBacktestService()
    request = types.SimpleNamespace(
        strategy_params=types.SimpleNamespace(
            signal={"module_path": "backend.services.engine.qlib_app.utils.simple_signal"}
        ),
        universe="all",
        start_date="2025-01-01",
        end_date="2025-01-02",
    )
    signal_data, signal_meta = asyncio.run(service._build_signal_data(request))
    assert signal_data is not None
    assert signal_meta.get("source") in {"feature_field", "close_fallback"}


def test_enforce_signal_quality_blocks_implicit_close_fallback():
    service = QlibBacktestService()
    request = types.SimpleNamespace(allow_feature_signal_fallback=False)

    with pytest.raises(ValueError, match="禁止回退到 \\$close"):
        service._enforce_signal_quality(
            {"source": "close_fallback", "fallback_reason": "pred_path_not_found"},
            request=request,
        )


def test_lag_signal_frame_moves_signal_to_next_trade_date():
    import pandas as pd

    idx = pd.MultiIndex.from_tuples(
        [
            (pd.Timestamp("2025-01-02"), "SH600000"),
            (pd.Timestamp("2025-01-03"), "SH600000"),
            (pd.Timestamp("2025-01-06"), "SH600000"),
        ],
        names=["datetime", "instrument"],
    )
    df = pd.DataFrame({"score": [1.0, 2.0, 3.0]}, index=idx)

    lagged = QlibBacktestService._lag_signal_frame(df, 1)

    assert (pd.Timestamp("2025-01-02"), "SH600000") not in lagged.index
    assert lagged.loc[(pd.Timestamp("2025-01-03"), "SH600000"), "score"] == 1.0
    assert lagged.loc[(pd.Timestamp("2025-01-06"), "SH600000"), "score"] == 2.0


def test_build_pred_signal_meta_uses_lagged_effective_dates():
    import pandas as pd

    service = QlibBacktestService()
    idx = pd.MultiIndex.from_tuples(
        [
            (pd.Timestamp("2025-01-02"), "SH600000"),
            (pd.Timestamp("2025-01-03"), "SH600000"),
            (pd.Timestamp("2025-01-06"), "SH600000"),
        ],
        names=["datetime", "instrument"],
    )
    pred = pd.DataFrame({"score": [0.1, 0.2, 0.3]}, index=idx)
    request = types.SimpleNamespace(
        start_date="2025-01-03",
        end_date="2025-01-06",
        signal_lag_days=1,
    )

    meta = service._build_pred_signal_meta(pred, "/tmp/pred.pkl", request)

    assert meta["max_signal_date"] == "2025-01-06"
    assert meta["rows_in_range"] == 2
    assert meta["date_count"] == 2


def test_build_signal_data_supports_explicit_parquet_path(tmp_path: Path):
    service = QlibBacktestService()
    parquet_path = tmp_path / "pred.parquet"
    parquet_path.write_text("", encoding="utf-8")
    request = types.SimpleNamespace(
        strategy_params=types.SimpleNamespace(signal=str(parquet_path)),
        universe="all",
        start_date="2025-01-01",
        end_date="2025-01-31",
    )

    captured = {}

    def fake_load_pred(path, req):
        captured["path"] = path
        captured["request"] = req
        return "parquet-signal", {"source": "pred_pkl"}

    service._load_pred_pkl = fake_load_pred  # type: ignore[method-assign]

    signal_data, signal_meta = asyncio.run(service._build_signal_data(request))

    assert signal_data == "parquet-signal"
    assert signal_meta["source"] == "pred_pkl"
    assert captured["path"] == str(parquet_path)
    assert captured["request"] is request


def test_normalize_trades_for_display_backfills_factor_and_price(monkeypatch):
    monkeypatch.setattr(
        RiskAnalyzer,
        "_load_factor_map",
        classmethod(lambda cls, pairs: {("SZ002822", "2025-01-02"): 0.14105364680290222}),
    )
    trades = [
        {
            "date": "2025-01-02",
            "symbol": "SZ002822",
            "price": 0.5444670915603638,
            "quantity": 34738.55593997433,
            "totalAmount": 18914.00051764482,
            "adj_price": None,
            "adj_quantity": None,
            "factor": None,
        }
    ]
    normalized = RiskAnalyzer.normalize_trades_for_display(trades)
    row = normalized[0]
    assert row["factor"] == pytest.approx(0.14105364680290222)
    assert row["price"] == pytest.approx(3.86, rel=1e-3)
    assert row["quantity"] == pytest.approx(4900.0, rel=1e-6)


def test_normalize_trades_for_display_snaps_cn_board_lot_under_factor_drift(monkeypatch):
    monkeypatch.setattr(
        RiskAnalyzer,
        "_load_factor_map",
        classmethod(lambda cls, pairs: {("SH600018", "2025-01-10"): 0.4312969446182251}),
    )
    trades = [
        {
            "date": "2025-01-10",
            "symbol": "SH600018",
            "price": 2.4670183658599854,
            "quantity": 6264.226624905601,
            "totalAmount": 15453.962131551227,
            "adj_price": None,
            "adj_quantity": None,
            "factor": None,
        }
    ]
    normalized = RiskAnalyzer.normalize_trades_for_display(trades)
    row = normalized[0]
    assert row["price"] == pytest.approx(5.72, rel=1e-3)
    assert row["quantity"] == pytest.approx(2700.0, rel=1e-6)


def test_recording_strategy_drops_pool_file_local_before_super(monkeypatch):
    qlib = pytest.importorskip("qlib")
    assert qlib is not None

    from backend.services.engine.qlib_app.utils import recording_strategy as rs

    captured = {}

    def fake_init_redis(self, kwargs):
        return None

    def fake_init_dynamic_risk(self, kwargs):
        return None

    def fake_super_init(self, *args, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(rs.RedisRecordingStrategy, "init_redis", fake_init_redis)
    monkeypatch.setattr(rs.RedisRecordingStrategy, "init_dynamic_risk", fake_init_dynamic_risk)
    monkeypatch.setattr(rs.TopkDropoutStrategy, "__init__", fake_super_init)

    rs.RedisRecordingStrategy(
        signal="$close",
        topk=10,
        n_drop=2,
        pool_file_local="/tmp/custom_pool.txt",
        rebalance_days=1,
    )

    assert "pool_file_local" not in captured


def test_normalize_trades_for_display_deduplicates_mixed_writers(monkeypatch):
    monkeypatch.setattr(RiskAnalyzer, "_load_factor_map", classmethod(lambda cls, pairs: {}))
    trades = [
        {
            "date": "2025-01-07",
            "symbol": "SH600018",
            "action": "buy",
            "price": 5.84,
            "quantity": 2700,
            "amount": 15768.0,
            "commission": 5.79,
            "timestamp": "2026-04-22T10:00:00",
        },
        {
            "date": "2025-01-07",
            "symbol": "SH600018",
            "action": "buy",
            "price": 5.84,
            "quantity": 2700.0,
            "totalAmount": 15768.0,
            "commission": 5.79,
            "adj_price": 2.5197833776474,
            "adj_quantity": 6264.226624905601,
            "factor": 0.4312969446182251,
            "equity_after": 999686.33,
        },
    ]

    normalized = RiskAnalyzer.normalize_trades_for_display(trades)
    assert len(normalized) == 1
    row = normalized[0]
    assert row["quantity"] == pytest.approx(2700.0, rel=1e-6)
    assert row["totalAmount"] == pytest.approx(15768.0, rel=1e-6)
    assert row["adj_price"] == pytest.approx(2.5197833776474, rel=1e-9)


def test_build_trades_list_deduplicates_redis_dupes(monkeypatch):
    monkeypatch.setattr(RiskAnalyzer, "_load_factor_map", classmethod(lambda cls, pairs: {}))

    class _FakeRedis:
        def lrange(self, key, start, end):
            assert key == "qlib:backtest:trades:bt-dup"
            exchange_trade = {
                "date": "2025-01-07",
                "symbol": "SH600018",
                "action": "buy",
                "price": 5.84,
                "quantity": 2700,
                "amount": 15768.0,
                "commission": 5.79,
            }
            strategy_trade = {
                "date": "2025-01-07",
                "symbol": "SH600018",
                "action": "buy",
                "price": 5.84,
                "quantity": 2700.0,
                "totalAmount": 15768.0,
                "commission": 5.79,
                "adj_price": 2.5197833776474,
                "adj_quantity": 6264.226624905601,
                "factor": 0.4312969446182251,
            }
            return [json.dumps(exchange_trade), json.dumps(strategy_trade)]

    monkeypatch.setattr(risk_analyzer_module, "get_redis_sentinel_client", lambda: _FakeRedis())
    trades = RiskAnalyzer._build_trades_list({}, backtest_id="bt-dup")
    assert len(trades) == 1
    assert trades[0]["totalAmount"] == pytest.approx(15768.0, rel=1e-6)


def test_advanced_trade_stats_handles_missing_pnl_columns():
    trades = [
        {"date": "2025-01-02", "symbol": "SH600000", "action": "buy", "price": 10.0, "quantity": 100},
        {"date": "2025-01-03", "symbol": "SH600001", "action": "sell", "price": 11.0, "quantity": 100},
    ]

    stats = RiskAnalyzer._calculate_advanced_trade_stats(trades)

    assert stats["pnl_distribution"]["counts"]
    assert stats["trade_frequency_series"]["values"] == [2.0]


def test_risk_metrics_uses_geometric_benchmark_annualization(monkeypatch):
    import pandas as pd

    dates = pd.to_datetime(["2025-01-02", "2025-01-03", "2025-01-06", "2025-01-07", "2025-01-08"])
    daily_returns = pd.Series([0.01, 0.02, -0.01, 0.0, 0.03], index=dates)
    prices = pd.Series([100.0, 101.0, 103.02, 101.9898, 101.9898, 105.049494], index=pd.to_datetime([
        "2025-01-01",
        "2025-01-02",
        "2025-01-03",
        "2025-01-06",
        "2025-01-07",
        "2025-01-08",
    ]))
    idx = pd.MultiIndex.from_product([["SH000300"], prices.index], names=["instrument", "datetime"])
    bm_df = pd.DataFrame({"$close": prices.to_numpy()}, index=idx)

    monkeypatch.setattr(risk_analyzer_module.D, "features", lambda *args, **kwargs: bm_df, raising=False)

    metrics = RiskAnalyzer._compute_risk_metrics(
        daily_returns=daily_returns,
        benchmark="SH000300",
        start_date="2025-01-01",
        end_date="2025-01-08",
        annual_return=9.99,
        risk_free_rate=0.0,
    )

    bm_returns = prices.pct_change().dropna()
    aligned = pd.concat([daily_returns, bm_returns], axis=1, join="inner")
    aligned.columns = ["portfolio", "benchmark"]
    beta = aligned["portfolio"].cov(aligned["benchmark"]) / aligned["benchmark"].var()
    expected_port_annual = (1.0 + aligned["portfolio"]).prod() ** (252 / len(aligned)) - 1
    expected_bm_annual = (1.0 + aligned["benchmark"]).prod() ** (252 / len(aligned)) - 1

    assert metrics["beta"] == pytest.approx(beta)
    assert metrics["alpha"] == pytest.approx(expected_port_annual - beta * expected_bm_annual)
