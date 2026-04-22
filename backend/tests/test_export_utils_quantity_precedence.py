from backend.services.engine.qlib_app.api.export_utils import _build_quick_trade_rows


def test_build_quick_trade_rows_prefers_explicit_quantity_over_factor_reconstruction():
    trades = [
        {
            "date": "2025-01-10",
            "symbol": "SH600018",
            "action": "sell",
            "price": 5.71999963515563,
            "quantity": 2700,
            "adj_price": 2.4670183658599854,
            "adj_quantity": 6264.226624905601,
            "factor": 0.4312969446182251,
            "totalAmount": 15453.962131551227,
            "commission": 13.738500643331143,
        }
    ]
    rows = _build_quick_trade_rows(trades=trades, equity_curve=[], initial_capital=1_000_000.0)
    assert len(rows) == 1
    assert rows[0]["qty_int"] == 2700
