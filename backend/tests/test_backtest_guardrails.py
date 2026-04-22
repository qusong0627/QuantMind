import asyncio
import os
import sys
import types
import json

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
    assert signal_data == "$close"
    assert signal_meta.get("source") == "close_fallback"


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
