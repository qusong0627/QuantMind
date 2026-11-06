"""sltp_sell_now.build_plan 单测：卖出数量/全量标记/跳过原因（纯函数）。"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parents[2] / 'scripts' / 'sltp_sell_now.py'
_spec = importlib.util.spec_from_file_location('sltp_sell_now', _MODULE_PATH)
sell = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = sell
_spec.loader.exec_module(sell)


def _pos(code, volume, can_use):
    return {"stock_code": code, "volume": volume, "can_use_volume": can_use}


def test_full_position_sell():
    plan, skipped = sell.build_plan(
        [_pos('600036.SH', 1000, 1000)],
        {'600036.SH': {'lastPrice': 41.0}},
        ['600036.SH'])
    assert not skipped
    assert plan == [{'symbol': '600036.SH', 'qty': 1000.0,
                     'full_position': True, 'price': 41.0}]


def test_t1_partial_is_not_full_position():
    plan, _ = sell.build_plan(
        [_pos('600036.SH', 1000, 300)],
        {'600036.SH': {'lastPrice': 41.0}},
        ['600036.SH'])
    assert plan[0]['qty'] == 300.0
    assert plan[0]['full_position'] is False


def test_prefix_code_matches_suffix_symbol():
    plan, skipped = sell.build_plan(
        [_pos('SH600036', 500, 500)],
        {'600036.SH': {'lastPrice': 41.0}},
        ['600036.SH'])
    assert not skipped and plan[0]['qty'] == 500.0


def test_zero_available_is_skipped():
    plan, skipped = sell.build_plan(
        [_pos('600036.SH', 1000, 0)],
        {'600036.SH': {'lastPrice': 41.0}},
        ['600036.SH'])
    assert not plan and '可卖为 0' in skipped[0]


def test_missing_position_is_skipped():
    plan, skipped = sell.build_plan([], {'600036.SH': {'lastPrice': 41.0}},
                                    ['600036.SH'])
    assert not plan and '无持仓' in skipped[0]


def test_missing_quote_is_skipped_fail_closed():
    plan, skipped = sell.build_plan([_pos('600036.SH', 100, 100)], {},
                                    ['600036.SH'])
    assert not plan and '无实时行情' in skipped[0]
