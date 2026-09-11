"""海外券商（老虎/盈透/富途）：符号映射、RSA 私钥处理、账户查询字段映射、
下单参数构造、按市场路由券商（broker:selected:{market}）。

tigeropen SDK 未安装的环境用最小 fake 模块树模拟（模块内均为懒加载 import）。
"""

from __future__ import annotations

import asyncio
import sys
import types
from types import SimpleNamespace
from unittest import mock

import pytest

from backend.services.trade_shared.models.order import TradingMode
from backend.services.trade.services.overseas_brokers import (
    IBBroker,
    TigerBroker,
    _ib_contract_params,
    _tiger_contract,
)
from backend.services.live_trading.services.trading_engine import (
    TradingEngine,
    _infer_broker_market,
    _selected_broker_type,
)


# ── 符号映射 ──────────────────────────────────────────────────────────────


def test_tiger_contract_us_stock():
    assert _tiger_contract("AAPL") == ("AAPL", "USD", "SMART")


def test_tiger_contract_us_suffix_stripped():
    assert _tiger_contract("AAPL.US") == ("AAPL", "USD", "SMART")


def test_tiger_contract_hk_padded_to_5_digits():
    assert _tiger_contract("0001.HK") == ("00001", "HKD", "SEHK")


def test_tiger_contract_hk_keeps_5_digits():
    assert _tiger_contract("0700.HK") == ("00700", "HKD", "SEHK")


def test_ib_contract_params_us_stock():
    assert _ib_contract_params("AAPL") == ("AAPL", "SMART", "USD")


def test_ib_contract_params_us_suffix_smart_exchange():
    assert _ib_contract_params("AAPL.US") == ("AAPL", "SMART", "USD")


def test_ib_contract_params_hk():
    assert _ib_contract_params("0700.HK") == ("0700", "SEHK", "HKD")


# ── RSA 私钥处理 ─────────────────────────────────────────────────────────


def test_wrap_pem_bare_base64_to_pem():
    b = TigerBroker()
    pem = b._wrap_pem("MIIBVAIBADANBgkqhkiG9w0BAQEFAASCAT4wggE6AgEAAkEA")
    assert pem.startswith("-----BEGIN RSA PRIVATE KEY-----\n")
    assert pem.endswith("-----END RSA PRIVATE KEY-----\n")
    # base64 按 64 字符折行
    assert all(len(line) == 64 for line in pem.splitlines()[1:-2] if line)


def test_wrap_pem_pem_text_passthrough():
    b = TigerBroker()
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIBVA==\n-----END RSA PRIVATE KEY-----\n"
    assert b._wrap_pem(pem) == pem.strip()


def test_wrap_pem_whitespace_inside_base64_stripped():
    b = TigerBroker()
    pem = b._wrap_pem("MIIB VA\nIBADAB")  # 12 个 base64 字符，内嵌空格/换行
    assert "MIIBVAIBADAB" in pem


def test_wrap_pem_incomplete_base64_raises():
    b = TigerBroker()
    with pytest.raises(RuntimeError, match="不是 4 的倍数"):
        b._wrap_pem("MIIBV")  # 5 个字符 → 4 的余 1


def test_wrap_pem_file_path_reads_file(tmp_path):
    key_file = tmp_path / "key.pem"
    key_file.write_text(
        "-----BEGIN RSA PRIVATE KEY-----\nMIIBVA==\n-----END RSA PRIVATE KEY-----\n"
    )
    b = TigerBroker()
    assert b._wrap_pem(str(key_file)).startswith("-----BEGIN RSA PRIVATE KEY-----")


# ── fake tigeropen 模块树（本机无 SDK 时验证参数构造）────────────────────


def _install_fake_tigeropen() -> dict[str, types.ModuleType]:
    """构造最小 fake tigeropen SDK（签名对齐容器内实测的 3.x 版本）。"""
    contract_utils = types.ModuleType("tigeropen.common.util.contract_utils")
    order_utils = types.ModuleType("tigeropen.common.util.order_utils")
    tigercfg = types.ModuleType("tigeropen.tiger_open_config")
    trade_client_mod = types.ModuleType("tigeropen.trade.trade_client")

    class Contract:
        def __init__(self, symbol, currency, sec_type=None, local_symbol=None,
                     exchange=None, contract_id=None):
            self.symbol = symbol
            self.currency = currency
            self.sec_type = sec_type
            self.local_symbol = local_symbol
            self.exchange = exchange
            self.contract_id = contract_id

    class Order:
        def __init__(self, account, contract, action, quantity, **kwargs):
            self.account = account
            self.contract = contract
            self.action = action
            self.quantity = quantity
            self.id = None
            for key, value in kwargs.items():
                setattr(self, key, value)

    def stock_contract(symbol, currency, local_symbol=None, exchange=None, contract_id=None):
        return Contract(symbol, currency, sec_type="STK",
                        local_symbol=local_symbol, exchange=exchange, contract_id=contract_id)

    def limit_order(account, contract, action, quantity, limit_price, time_in_force="DAY"):
        return Order(account, contract, action, quantity, limit_price=limit_price,
                     time_in_force=time_in_force)

    def market_order(account, contract, action, quantity, time_in_force="DAY"):
        return Order(account, contract, action, quantity, time_in_force=time_in_force)

    class TigerOpenClientConfig:
        pass

    class TradeClient:
        def __init__(self, config):
            self.client_config = config

        def place_order(self, order):  # noqa: D102
            order.id = 999999
            return 999999

    contract_utils.stock_contract = stock_contract
    order_utils.limit_order = limit_order
    order_utils.market_order = market_order
    tigercfg.TigerOpenClientConfig = TigerOpenClientConfig
    trade_client_mod.TradeClient = TradeClient

    for name, mod in {
        "tigeropen": types.ModuleType("tigeropen"),
        "tigeropen.common": types.ModuleType("tigeropen.common"),
        "tigeropen.common.util": types.ModuleType("tigeropen.common.util"),
        "tigeropen.common.util.contract_utils": contract_utils,
        "tigeropen.common.util.order_utils": order_utils,
        "tigeropen.tiger_open_config": tigercfg,
        "tigeropen.trade": types.ModuleType("tigeropen.trade"),
        "tigeropen.trade.trade_client": trade_client_mod,
    }.items():
        sys.modules.setdefault(name, mod)
    return sys.modules


_FAKE_TIGEROPEN = _install_fake_tigeropen()


def _fake_position(symbol: str, qty=10.0, salable=8.0, price=100.0,
                   value=1000.0, avg_cost=90.0) -> SimpleNamespace:
    return SimpleNamespace(
        contract=SimpleNamespace(symbol=symbol),
        quantity=qty,
        salable_qty=salable,
        market_price=price,
        market_value=value,
        average_cost=avg_cost,
    )


def _fake_assets(account="3667944", net=500000.0, cash=200000.0, gross=300000.0):
    return [SimpleNamespace(
        account=account,
        summary=SimpleNamespace(
            net_liquidation=net, cash=cash, gross_position_value=gross,
        ),
    )]


def _tiger_client(assets=None, positions=None, place_result=None):
    client = mock.Mock()
    client.client_config = SimpleNamespace(account="3667944")
    client.get_assets.return_value = assets if assets is not None else _fake_assets()
    client.get_positions.return_value = positions if positions is not None else []
    client.place_order.return_value = place_result
    return client


# ── 账户查询字段映射 ─────────────────────────────────────────────────────


def test_query_account_maps_summary_and_positions():
    b = TigerBroker()
    client = _tiger_client(
        positions=[_fake_position("AAPL"), _fake_position("00700", qty=100.0, salable=100.0)],
    )
    with mock.patch.object(b, "_get_client", return_value=client):
        result = asyncio.run(b.query_account("test"))
    assert result["total_asset"] == 500000.0
    assert result["cash"] == 200000.0
    assert result["market_value"] == 300000.0  # gross_position_value 优先
    aapl = result["positions"]["AAPL"]
    assert aapl["volume"] == 10.0
    assert aapl["available_volume"] == 8.0
    assert aapl["price"] == 100.0
    assert aapl["cost"] == 90.0
    assert result["positions"]["00700"]["volume"] == 100.0


def test_query_account_no_assets_returns_empty():
    b = TigerBroker()
    client = _tiger_client(assets=[])
    with mock.patch.object(b, "_get_client", return_value=client):
        assert asyncio.run(b.query_account("test")) == {}


def test_query_account_positions_failure_keeps_summary():
    b = TigerBroker()
    client = _tiger_client()
    client.get_positions.side_effect = RuntimeError("positions api down")
    with mock.patch.object(b, "_get_client", return_value=client):
        result = asyncio.run(b.query_account("test"))
    assert result["total_asset"] == 500000.0
    assert result["positions"] == {}


# ── 下单参数构造 ─────────────────────────────────────────────────────────


def test_place_order_market_builds_contract_kwargs():
    b = TigerBroker()
    client = _tiger_client(place_result=123456789)
    with mock.patch.object(b, "_get_client", return_value=client):
        result = asyncio.run(b.place_order(1, "AAPL", "BUY", 10, "market"))
    assert result.success
    assert result.exchange_order_id == "123456789"
    order = client.place_order.call_args.args[0]
    assert order.action == "BUY"
    assert order.quantity == 10.0
    assert order.account == "3667944"
    assert order.contract.symbol == "AAPL"
    assert order.contract.currency == "USD"
    assert order.contract.exchange == "SMART"  # 关键字传参而非 local_symbol


def test_place_order_limit_hk_contract():
    b = TigerBroker()
    client = _tiger_client()

    def _fake_place(order):  # SDK place_order 无返回值时回填 order.id
        order.id = 999999
        return None

    client.place_order.side_effect = _fake_place
    with mock.patch.object(b, "_get_client", return_value=client):
        result = asyncio.run(b.place_order(1, "0700.HK", "SELL", 100, "limit", price=350.0))
    assert result.success
    assert result.exchange_order_id == "999999"
    order = client.place_order.call_args.args[0]
    assert order.contract.symbol == "00700"
    assert order.contract.currency == "HKD"
    assert order.contract.exchange == "SEHK"
    assert order.limit_price == 350.0


def test_place_order_failure_returns_failed_result():
    b = TigerBroker()
    client = _tiger_client()
    client.place_order.side_effect = RuntimeError("permission denied")
    with mock.patch.object(b, "_get_client", return_value=client):
        result = asyncio.run(b.place_order(1, "AAPL", "BUY", 10, "market"))
    assert not result.success
    assert "permission denied" in result.message


def test_cancel_order_passes_global_id_and_account():
    b = TigerBroker()
    client = mock.Mock()
    client.client_config = SimpleNamespace(account="3667944")
    with mock.patch.object(b, "_get_client", return_value=client), \
         mock.patch.object(b, "_account", return_value="3667944"):
        assert asyncio.run(b.cancel_order("987654321")) is True
    client.cancel_order.assert_called_once_with(id=987654321, account="3667944")


# ── 市场推断与按市场路由券商 ─────────────────────────────────────────────


def test_infer_broker_market_by_symbol():
    assert _infer_broker_market("AAPL") == "US"
    assert _infer_broker_market("0700.HK") == "HK"
    assert _infer_broker_market("600036.SH") == "CN"
    assert _infer_broker_market("CL.FUT") == "FUTURES"
    assert _infer_broker_market("BTCUSDT") == "CRYPTO"
    assert _infer_broker_market(None) == "CN"


def test_selected_broker_type_reads_redis():
    redis = SimpleNamespace(client=SimpleNamespace(get=lambda key: b"tiger"))
    assert _selected_broker_type(redis, "HK") == "tiger"


def test_selected_broker_type_empty_when_redis_down():
    assert _selected_broker_type(None, "HK") == ""
    redis = SimpleNamespace(client=SimpleNamespace(get=mock.Mock(side_effect=RuntimeError)))
    assert _selected_broker_type(redis, "HK") == ""


def _engine_with_redis(selected: dict[str, str] | None = None) -> TradingEngine:
    engine = TradingEngine.__new__(TradingEngine)  # 绕过 __init__（不连 DB/Redis）
    engine._broker_cache = {}

    def fake_get(key: str):
        for market, broker in (selected or {}).items():
            if key == f"broker:selected:{market}":
                return broker.encode()
        return None

    engine.redis = SimpleNamespace(client=SimpleNamespace(get=fake_get))
    return engine


def test_get_broker_real_falls_back_to_global_type():
    engine = _engine_with_redis()
    with mock.patch("backend.services.live_trading.services.trading_engine.settings.ENABLE_REAL_TRADING", True), \
         mock.patch("backend.services.live_trading.services.trading_engine.settings.REAL_BROKER_TYPE", "tdx"):
        broker = engine._get_broker(TradingMode.REAL, "AAPL")
    assert broker.__class__.__name__ == "TdxBroker"


def test_get_broker_real_routes_to_selected_tiger_for_us():
    engine = _engine_with_redis({"US": "tiger"})
    with mock.patch("backend.services.live_trading.services.trading_engine.settings.ENABLE_REAL_TRADING", True), \
         mock.patch("backend.services.live_trading.services.trading_engine.settings.REAL_BROKER_TYPE", "tdx"):
        broker = engine._get_broker(TradingMode.REAL, "AAPL")
    assert isinstance(broker, TigerBroker)


def test_get_broker_real_routes_to_selected_ib_for_hk():
    engine = _engine_with_redis({"HK": "ib"})
    with mock.patch("backend.services.live_trading.services.trading_engine.settings.ENABLE_REAL_TRADING", True), \
         mock.patch("backend.services.live_trading.services.trading_engine.settings.REAL_BROKER_TYPE", "tdx"):
        broker = engine._get_broker(TradingMode.REAL, "0700.HK")
    assert isinstance(broker, IBBroker)


def test_get_broker_real_disabled_uses_paper_broker():
    engine = _engine_with_redis({"US": "tiger"})
    with mock.patch("backend.services.live_trading.services.trading_engine.settings.ENABLE_REAL_TRADING", False):
        broker = engine._get_broker(TradingMode.REAL, "AAPL")
    assert broker.__class__.__name__ == "PaperTradingBroker"


def test_get_broker_simulation_uses_paper_broker():
    engine = _engine_with_redis()
    broker = engine._get_broker(TradingMode.SIMULATION, "AAPL")
    assert broker.__class__.__name__ == "PaperTradingBroker"


def test_get_broker_cache_respects_market_and_type():
    engine = _engine_with_redis({"US": "tiger", "HK": "ib"})
    with mock.patch("backend.services.live_trading.services.trading_engine.settings.ENABLE_REAL_TRADING", True):
        us_broker = engine._get_broker(TradingMode.REAL, "AAPL")
        hk_broker = engine._get_broker(TradingMode.REAL, "0700.HK")
    assert isinstance(us_broker, TigerBroker)
    assert isinstance(hk_broker, IBBroker)
    assert len(engine._broker_cache) == 2
