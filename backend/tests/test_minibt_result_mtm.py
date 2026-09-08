# -*- coding: utf-8 -*-
"""minibt 逐日盯市(MTM)权益重建单测（backend/shared/minibt_result.py）。

minibt 原生 ``total_profit`` 是"现金 + 持仓成本"，持仓期间不随价格变动，
回撤/夏普严重偏乐观。这里覆盖：重建、空仓 bar 交叉校验、多标的/无空仓回退，
以及 MTM 指标重算与 result.json 口径提示。
"""
import os
import sys
import time

import pandas as pd
import pytest

project_root = os.path.join(os.path.dirname(__file__), "../../")
sys.path.append(project_root)

from backend.shared.minibt_result import (  # noqa: E402
    _apply_mtm_metrics,
    _build_run_result,
    _reconstruct_mtm_equity,
)


class _Side:
    def __init__(self, value):
        self.value = value


class _Order:
    def __init__(self, side, size, price, fee, when):
        self.side = _Side(side)
        self.executed_size = size
        self.executed_price = price
        self.executed_commission = fee
        self.executed_datetime = when


class _Broker:
    def __init__(self, symbol, orders):
        self.symbol = symbol
        self._orders = orders

    def get_completed_orders(self):
        return list(self._orders)


class _Account:
    def __init__(self, brokers, balance=1_000_000.0):
        self.brokers = brokers
        self._balance = balance


class _Strategy:
    def __init__(self, account):
        self._account = account


class _Bt:
    def __init__(self, account):
        self.strategies = [_Strategy(account)]


def _source_df(closes, start="2024-01-01"):
    return pd.DataFrame(
        {
            "datetime": pd.date_range(start, periods=len(closes), freq="D"),
            "close": closes,
        }
    )


def _res_frame(balance, positions):
    return pd.DataFrame({"total_profit": balance, "positions": positions})


def _buy_sell_account():
    """1,000,000 起步：bar1 买 100@12(费1)，bar3 卖 100@13(费1)。"""
    return _Account(
        [
            _Broker(
                "symbol0",
                [
                    _Order(0, 100.0, 12.0, 1.0, "2024-01-02"),
                    _Order(1, 100.0, 13.0, 1.0, "2024-01-04"),
                ],
            )
        ]
    )


class TestReconstructMtmEquity:
    def test_reconstructs_marked_to_market_equity(self):
        bt = _Bt(_buy_sell_account())
        source_df = _source_df([10.0, 12.0, 11.0, 13.0])
        # 空仓 bar 上 minibt 记账值 = 现金：bar0 初始、bar3 卖出后
        res = _res_frame([1_000_000.0, 999_999.0, 999_999.0, 1_000_098.0],
                         [0.0, 1.0, 1.0, 0.0])

        equity, reason = _reconstruct_mtm_equity(bt, source_df, res)

        assert reason == ""
        assert equity == pytest.approx(
            [1_000_000.0, 999_999.0, 999_899.0, 1_000_098.0]
        )

    def test_rejects_when_flat_bar_disagrees_with_minibt(self):
        bt = _Bt(_buy_sell_account())
        source_df = _source_df([10.0, 12.0, 11.0, 13.0])
        res = _res_frame([1_000_000.0, 999_999.0, 999_999.0, 1_000_000.0],
                         [0.0, 1.0, 1.0, 0.0])

        equity, reason = _reconstruct_mtm_equity(bt, source_df, res)

        assert equity is None
        assert "空仓 bar 与 minibt 记账不一致" in reason

    def test_rejects_multi_symbol_scripts(self):
        account = _buy_sell_account()
        account.brokers.append(_Broker("symbol1", []))
        bt = _Bt(account)

        equity, reason = _reconstruct_mtm_equity(
            bt, _source_df([10.0, 11.0]), _res_frame([1_000_000.0, 1_000_000.0], [0.0, 0.0])
        )

        assert equity is None
        assert "仅支持单标的" in reason

    def test_rejects_when_never_flat_and_no_sizes(self):
        bt = _Bt(_buy_sell_account())

        equity, reason = _reconstruct_mtm_equity(
            bt,
            _source_df([10.0, 11.0]),
            _res_frame([1_000_000.0, 1_000_000.0], [1.0, 1.0]),
        )

        assert equity is None
        assert "无 sizes 列" in reason

    def test_accepts_never_flat_when_sizes_match(self):
        account = _Account(
            [_Broker("symbol0", [_Order(0, 100.0, 12.0, 1.0, "2024-01-01")])]
        )
        bt = _Bt(account)
        # 全程持多仓:bar0 买 100@12(费1) → 现金 998,799,MTM 随收盘价波动
        res = pd.DataFrame(
            {"total_profit": [1_000_000.0, 1_000_000.0], "positions": [1.0, 1.0],
             "sizes": [100.0, 100.0]}
        )

        equity, reason = _reconstruct_mtm_equity(
            bt, _source_df([10.0, 11.0]), res
        )

        assert reason == ""
        assert equity == pytest.approx([999_799.0, 999_899.0])

    def test_rejects_when_size_disagrees(self):
        bt = _Bt(_buy_sell_account())
        res = pd.DataFrame(
            {"total_profit": [1_000_000.0, 999_999.0], "positions": [1.0, 1.0],
             "sizes": [200.0, 200.0]}
        )

        equity, reason = _reconstruct_mtm_equity(
            bt, _source_df([10.0, 11.0]), res
        )

        assert equity is None
        assert "持仓数量与 minibt 记账不一致" in reason


class TestApplyMtmMetrics:
    def test_recomputes_return_drawdown_sharpe(self):
        metrics = {"max_drawdown": 0.0, "sharpe": 9.9, "cum_return": 0.0,
                   "win_rate": 0.5, "n_trades": 2.0}

        out = _apply_mtm_metrics(metrics, [1.0, 1.1, 0.9, 1.2])

        assert out["cum_return"] == pytest.approx(0.2)
        assert out["max_drawdown"] == pytest.approx(0.2 / 1.1)
        assert out["win_rate"] == pytest.approx(2 / 3)
        assert out["n_trades"] == 2.0  # 其余指标保持 minibt 口径


class TestBuildRunResultEquityCaliber:
    def _payload(self, **kwargs):
        return _build_run_result(
            _Bt(_buy_sell_account()),
            _source_df([10.0, 12.0, 11.0, 13.0]),
            _res_frame([1_000_000.0, 999_999.0, 999_999.0, 1_000_098.0],
                       [0.0, 1.0, 1.0, 0.0]),
            {"max_drawdown": 0.0, "sharpe": 0.0, "cum_return": 0.0,
             "win_rate": 0.0, "n_trades": 0.0},
            1.0,
            time.time(),
            **kwargs,
        )

    def test_uses_mtm_series_and_flags_caliber(self):
        payload = self._payload(
            mtm_equity=[1_000_000.0, 999_999.0, 999_899.0, 1_000_098.0]
        )

        assert [p["value"] for p in payload["equity"]] == pytest.approx(
            [1_000_000.0, 999_999.0, 999_899.0, 1_000_098.0]
        )
        assert payload["extra"]["equity_caliber"] == "mtm"
        assert any("逐日盯市" in w for w in payload["warnings"])

    def test_falls_back_to_minibt_balance_with_warning(self):
        payload = self._payload(mtm_skip_reason="标的数=2(仅支持单标的)")

        assert [p["value"] for p in payload["equity"]] == pytest.approx(
            [1_000_000.0, 999_999.0, 999_999.0, 1_000_098.0]
        )
        assert payload["extra"]["equity_caliber"] == "minibt_balance"
        assert any("未能重建逐日盯市权益" in w for w in payload["warnings"])
