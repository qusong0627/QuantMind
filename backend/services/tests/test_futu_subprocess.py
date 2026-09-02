"""futu_subprocess 纯函数单元测试（不依赖 futu SDK / OpenD 网关）。"""

import math

from backend.services.trade.services.futu_subprocess import (
    _aggregate_positions,
    _as_float,
    _as_str,
    _merge_position,
    _parse_position_row,
    _place_result_from_row,
)


class TestAsFloat:
    def test_normal_numbers(self):
        assert _as_float(5) == 5.0
        assert _as_float("3.14") == 3.14
        assert _as_float(0) == 0.0

    def test_futu_garbage_values_fall_back_to_default(self):
        # futu 对未填数值列返回 'N/A' / NaN / None
        assert _as_float("N/A") == 0.0
        assert _as_float(None) == 0.0
        assert _as_float(math.nan) == 0.0
        assert _as_float("") == 0.0
        assert _as_float("abc", default=-1) == -1.0


class TestAsStr:
    def test_nan_and_none_become_empty(self):
        assert _as_str(None) == ""
        assert _as_str(math.nan) == ""

    def test_scalars_stringified(self):
        assert _as_str("HK.00700") == "HK.00700"
        assert _as_str(12345) == "12345"


class TestParsePositionRow:
    def test_closed_position_row_skipped(self):
        assert _parse_position_row({"code": "HK.00700", "qty": 0}) is None

    def test_missing_nominal_price_falls_back_to_market_value(self):
        pos = _parse_position_row(
            {
                "code": "HK.00700",
                "qty": 100,
                "market_val": 10000,
                "nominal_price": 0,
                "can_sell_qty": 100,
            }
        )
        assert pos["volume"] == 100
        assert pos["available_volume"] == 100
        assert pos["price"] == 100.0  # 10000 / 100

    def test_nominal_price_wins_when_present(self):
        pos = _parse_position_row(
            {
                "code": "HK.00700",
                "qty": 100,
                "market_val": 9000,
                "nominal_price": 95.5,
                "can_sell_qty": 50,
            }
        )
        assert pos["price"] == 95.5

    def test_closed_row_with_nan_qty_not_misread_as_position(self):
        assert _parse_position_row({"code": "HK.00700", "qty": math.nan}) is None

    def test_default_currency_when_missing_or_nan(self):
        assert _parse_position_row({"code": "HK.00700", "qty": 1})["currency"] == "HKD"
        assert (
            _parse_position_row({"code": "HK.00700", "qty": 1, "currency": math.nan})[
                "currency"
            ]
            == "HKD"
        )


class TestMergePosition:
    def test_quantity_weighted_cost_and_sums(self):
        existing = {
            "volume": 100,
            "available_volume": 80,
            "price": 10.0,
            "market_value": 1000.0,
            "cost": 9.0,
            "name": "腾讯控股",
            "currency": "HKD",
        }
        merged = _merge_position(
            existing,
            {
                "volume": 200,
                "available_volume": 200,
                "price": 20.0,
                "market_value": 4000.0,
                "cost": 15.0,
                "name": "腾讯控股",
                "currency": "HKD",
            },
        )
        assert merged["volume"] == 300
        assert merged["available_volume"] == 280
        assert merged["market_value"] == 5000.0
        assert merged["price"] == 5000.0 / 300
        assert merged["cost"] == (9.0 * 100 + 15.0 * 200) / 300
        assert merged["name"] == "腾讯控股"
        # 原输入不被就地修改
        assert existing["volume"] == 100


class TestAggregatePositions:
    def test_merge_same_code_and_skip_closed_rows(self):
        rows = [
            {
                "code": "HK.00700",
                "qty": 100,
                "market_val": 10000,
                "nominal_price": 100,
                "can_sell_qty": 100,
                "cost_price": 90,
            },
            {"code": "HK.00700", "qty": 0, "realized_pl": "N/A"},  # 已平仓行
            {
                "code": "US.AAPL",
                "qty": 10,
                "market_val": 2000,
                "nominal_price": 200,
                "can_sell_qty": 10,
                "cost_price": 180,
            },
            {
                "code": "HK.00700",
                "qty": 300,
                "market_val": 33000,
                "nominal_price": 110,
                "can_sell_qty": 250,
                "cost_price": 100,
            },
        ]
        positions = _aggregate_positions(rows)
        assert set(positions) == {"HK.00700", "US.AAPL"}
        hk = positions["HK.00700"]
        assert hk["volume"] == 400
        assert hk["available_volume"] == 350
        assert hk["market_value"] == 43000.0
        assert hk["price"] == 43000.0 / 400
        assert hk["cost"] == (90 * 100 + 100 * 300) / 400
        assert positions["US.AAPL"]["price"] == 200.0

    def test_empty_input(self):
        assert _aggregate_positions([]) == {}


class TestPlaceResultFromRow:
    def test_none_row_returns_pure_defaults(self):
        out = _place_result_from_row(None)
        assert out == {
            "order_id": "",
            "status": "",
            "filled_quantity": 0.0,
            "filled_price": 0.0,
            "message": "SUBMITTED",
        }

    def test_immediate_fill_passthrough(self):
        row = {
            "order_id": 930123,
            "order_status": "FILLED",
            "dealt_qty": 500,
            "dealt_avg_price": 101.5,
            "last_err_msg": "",
        }
        out = _place_result_from_row(row)
        assert out["order_id"] == "930123"
        assert out["status"] == "FILLED"
        assert out["filled_quantity"] == 500.0
        assert out["filled_price"] == 101.5
        assert out["message"] == "SUBMITTED"

    def test_nan_filled_fields_are_sanitized(self):
        out = _place_result_from_row(
            {
                "order_id": "order-1",
                "order_status": "SUBMITTED",
                "dealt_qty": math.nan,
                "dealt_avg_price": math.nan,
            }
        )
        assert out["filled_quantity"] == 0.0
        assert out["filled_price"] == 0.0

    def test_err_msg_propagates_instead_of_submitted(self):
        out = _place_result_from_row(
            {"order_id": "order-1", "last_err_msg": "资金不足"}
        )
        assert out["message"] == "资金不足"
