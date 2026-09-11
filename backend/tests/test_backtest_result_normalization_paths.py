import asyncio
import os
import sys
import types

project_root = os.path.join(os.path.dirname(__file__), "../../")
sys.path.append(project_root)

from backend.services.engine.qlib_app.services import backtest_service as service_mod
from backend.services.engine.qlib_app.services import backtest_service_query as query_mod
from backend.services.engine.qlib_app.services.risk_analyzer import RiskAnalyzer


class FakeResultModel:
    def __init__(self, **kwargs):
        self.trades = kwargs.get("trades", [])
        for k, v in kwargs.items():
            setattr(self, k, v)

    def dict(self):
        return {"trades": self.trades}


class FakeCache:
    def __init__(self, cached_result=None):
        self.cached_result = cached_result
        self.set_calls = []

    def get_backtest_result(self, key):
        return self.cached_result

    def set_backtest_result(self, key, value):
        self.set_calls.append((key, value))


def test_get_result_normalizes_trades_on_cache_hit(monkeypatch):
    service = service_mod.QlibBacktestService()
    service._initialized = True
    service._cache = FakeCache(cached_result={"trades": [{"price": 0.54}]})

    # QlibBacktestResult 的名字解析发生在定义模块（query mixin）的全局命名空间，
    # patch 必须打在那里才会生效。
    monkeypatch.setattr(query_mod, "QlibBacktestResult", FakeResultModel)

    called = {}

    def fake_normalize(cls, trades):
        called["trades"] = trades
        return [{"price": 3.86}]

    monkeypatch.setattr(
        RiskAnalyzer,
        "normalize_trades_for_display",
        classmethod(fake_normalize),
    )

    result = asyncio.run(service.get_result(backtest_id="bid", tenant_id="default", user_id="u1"))

    assert called["trades"] == [{"price": 0.54}]
    assert result.trades == [{"price": 3.86}]


def test_get_result_normalizes_trades_on_persistence_path_and_writes_cache(monkeypatch):
    service = service_mod.QlibBacktestService()
    service._initialized = True
    cache = FakeCache(cached_result=None)
    service._cache = cache

    async def fake_get_result(backtest_id, tenant_id=None, user_id=None, exclude_fields=None):
        assert exclude_fields is None  # 未排除 trades 时应完整取回
        return FakeResultModel(trades=[{"price": 0.54}])

    service._persistence = types.SimpleNamespace(get_result=fake_get_result)

    called = {}

    def fake_normalize(cls, trades):
        called["trades"] = trades
        return [{"price": 3.86}]

    monkeypatch.setattr(
        RiskAnalyzer,
        "normalize_trades_for_display",
        classmethod(fake_normalize),
    )

    result = asyncio.run(service.get_result(backtest_id="bid2", tenant_id="default", user_id="u2"))

    assert called["trades"] == [{"price": 0.54}]
    assert result.trades == [{"price": 3.86}]
    assert len(cache.set_calls) == 1
    assert cache.set_calls[0][0] == "default:u2:bid2"
    assert cache.set_calls[0][1]["trades"] == [{"price": 3.86}]
