# -*- coding: utf-8 -*-
"""minibt 逐笔明细提取单测（backend/shared/minibt_result.py）。

用假的 Bt/Broker/Order 替身覆盖：订单字段映射、未成交订单过滤、
以及 QM_MINIBT_INCLUDE_TRADES 开关（AI-IDE 默认不产出 trades）。
"""
import os
import sys
import time

import pandas as pd
import pytest

project_root = os.path.join(os.path.dirname(__file__), "../../")
sys.path.append(project_root)

from backend.shared.minibt_result import (  # noqa: E402
    _build_run_result,
    _extract_trades,
)


class _Side:
    def __init__(self, value):
        self.value = value


class _Order:
    def __init__(self, side=0, size=100.0, price=10.0, fee=1.0, value=1000.0,
                 when="2024-01-02 15:00:00"):
        self.side = _Side(side)
        self.executed_size = size
        self.executed_price = price
        self.executed_commission = fee
        self.executed_value = value
        self.executed_datetime = when
        self.create_time = when
        self.ref = 1


class _Broker:
    def __init__(self, symbol, orders):
        self.symbol = symbol
        self._orders = orders

    def get_completed_orders(self):
        return list(self._orders)


class _Account:
    def __init__(self, brokers):
        self.brokers = brokers


class _Strategy:
    def __init__(self, brokers):
        self._account = _Account(brokers)


class _Bt:
    def __init__(self, strategies):
        self.strategies = strategies


def _bt_with(orders, symbol="600036"):
    return _Bt([_Strategy([_Broker(symbol, orders)])])


def _metrics():
    return {
        "cum_return": 0.005,
        "annual_return": 0.1,
        "sharpe": 1.0,
        "max_drawdown": 0.02,
        "win_rate": 0.5,
        "n_trades": 1,
        "avg_position": 0.5,
        "profit_factor": 1.2,
        "total_fee": 9.6,
        "final_equity": 1005000.0,
    }


def _res_frame():
    return pd.DataFrame(
        {
            "total_profit": [1000000.0, 1005000.0],
            "positions": [0.0, 1.0],
            "total_fee": [0.0, 9.6],
        }
    )


class TestExtractTrades:
    def test_maps_order_fields_to_trade_records(self):
        bt = _bt_with([
            _Order(side=0, size=1000.0, price=38.5, fee=9.6, value=38500.0),
            _Order(side=1, size=1000.0, price=39.1, fee=19.5, value=39100.0,
                   when="2024-01-03 15:00:00"),
        ])

        trades = _extract_trades(bt)

        assert len(trades) == 2
        buy, sell = trades
        assert buy["direction"] == "BUY"
        assert buy["symbol"] == "600036"
        assert buy["qty"] == pytest.approx(1000.0)
        assert buy["price"] == pytest.approx(38.5)
        assert buy["detail"]["fee"] == pytest.approx(9.6)
        assert buy["detail"]["value"] == pytest.approx(38500.0)
        assert buy["pnl"] is None
        assert sell["direction"] == "SELL"
        assert sell["date"] == "2024-01-03 15:00:00"

    def test_skips_orders_without_execution(self):
        bt = _bt_with([
            _Order(size=0.0),
            _Order(size=None),
            _Order(size=200.0, side=0),
        ])

        trades = _extract_trades(bt)

        assert len(trades) == 1
        assert trades[0]["qty"] == pytest.approx(200.0)

    def test_tolerates_missing_structure(self):
        assert _extract_trades(_Bt([])) == []
        assert _extract_trades(object()) == []

    def test_sorts_by_date(self):
        bt = _bt_with([
            _Order(when="2024-03-01 15:00:00"),
            _Order(when="2024-01-05 15:00:00"),
        ])

        trades = _extract_trades(bt)

        assert [t["date"] for t in trades] == [
            "2024-01-05 15:00:00",
            "2024-03-01 15:00:00",
        ]


class TestSymbolHint:
    """minibt broker 按序号命名(symbol0)，逐笔明细要用数据源代码回填。"""

    def test_placeholder_symbol_replaced_by_hint(self):
        bt = _bt_with([_Order()], symbol="symbol0")

        trades = _extract_trades(bt, "600036.SH")

        assert trades[0]["symbol"] == "600036.SH"

    def test_real_symbol_kept_over_hint(self):
        bt = _bt_with([_Order()], symbol="000001")

        trades = _extract_trades(bt, "600036.SH")

        assert trades[0]["symbol"] == "000001"

    def test_placeholder_kept_without_hint(self):
        bt = _bt_with([_Order()], symbol="symbol0")

        trades = _extract_trades(bt)

        assert trades[0]["symbol"] == "symbol0"

    def test_build_run_result_reads_df_attrs(self, monkeypatch):
        monkeypatch.setenv("QM_MINIBT_INCLUDE_TRADES", "1")
        source_df = pd.DataFrame({"datetime": ["2024-01-02", "2024-01-03"]})
        source_df.attrs["symbol"] = "600036.SH"

        payload = _build_run_result(
            _bt_with([_Order()], symbol="symbol0"),
            source_df,
            _res_frame(),
            _metrics(),
            1.5,
            time.time(),
        )

        assert payload["trades"][0]["symbol"] == "600036.SH"


class TestBuildRunResultTradeFlag:
    def _payload(self, bt):
        return _build_run_result(
            bt,
            pd.DataFrame({"datetime": ["2024-01-02", "2024-01-03"]}),
            _res_frame(),
            _metrics(),
            1.5,
            time.time(),
        )

    def test_trades_empty_without_env_flag(self, monkeypatch):
        monkeypatch.delenv("QM_MINIBT_INCLUDE_TRADES", raising=False)
        bt = _bt_with([_Order()])

        payload = self._payload(bt)

        assert payload["trades"] == []
        assert all("订单流水" not in w for w in payload["warnings"])

    def test_trades_included_with_env_flag(self, monkeypatch):
        monkeypatch.setenv("QM_MINIBT_INCLUDE_TRADES", "1")
        bt = _bt_with([_Order()])

        payload = self._payload(bt)

        assert len(payload["trades"]) == 1
        assert any("订单流水" in w for w in payload["warnings"])

    def test_keeps_base_calibration_warnings(self, monkeypatch):
        monkeypatch.setenv("QM_MINIBT_INCLUDE_TRADES", "true")

        payload = self._payload(_bt_with([_Order()]))

        # 2 条基础口径 + 1 条权益口径(未传 mtm_equity → 回退提示) + 1 条逐笔口径
        assert len(payload["warnings"]) == 4
