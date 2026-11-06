"""bridge_gates 单测：故障期退避的判定边界。

核心不变量：
1. 策略账户通道判死（unresponsive）后，**交易类**只读请求立即失败、不转发；
2. 订单/撤单永远放行——查询通道坏了也不能拦住真单；
3. 行情类方法（get_full_tick 等，后台 listener 线程）不受影响，故障期照常；
4. 一次探针失败不算死（避免抖动误伤），连续失败达阈值才判死；
5. 探针一恢复就回到 ok。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parents[1] / 'bridge_gates.py'
_spec = importlib.util.spec_from_file_location('bridge_gates', _MODULE_PATH)
gates = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = gates
_spec.loader.exec_module(gates)


def test_order_like_set():
    for method in ('order_stock', 'PASSORDER', ' submit_order ', 'cancel_order_stock',
                   'cancel_order'):
        assert gates.is_order_like(method) is True, method
    for method in ('get_asset', 'query_stock_orders', 'get_full_tick', '', None):
        assert gates.is_order_like(method) is False, method


def test_trade_context_set():
    for method in ('get_asset', 'get_positions', 'query_orders', 'query_trades',
                   'query_stock_orders', 'query_stock_positions', 'query_stock_asset',
                   'query_stock_trades', 'reload_deployment', 'query_stock_position'):
        assert gates.is_trade_context(method) is True, method
    for method in ('get_full_tick', 'get_market_data', 'order_stock_async', '', None):
        assert gates.is_trade_context(method) is False, method


def test_drop_trade_context_when_unresponsive():
    assert gates.should_drop_request('get_asset', gates.STATE_UNRESPONSIVE) is True
    assert gates.should_drop_request('query_stock_orders',
                                     gates.STATE_UNRESPONSIVE) is True


def test_market_data_survives_unresponsive():
    # 行情方法跑在 listener 线程，主线程挂死时仍然可用，不能退避掉。
    assert gates.should_drop_request('get_full_tick',
                                     gates.STATE_UNRESPONSIVE) is False
    assert gates.should_drop_request('get_market_data',
                                     gates.STATE_UNRESPONSIVE) is False


def test_order_never_dropped_even_when_unresponsive():
    assert gates.should_drop_request('order_stock', gates.STATE_UNRESPONSIVE) is False
    assert gates.should_drop_request('cancel_order', gates.STATE_UNRESPONSIVE) is False


def test_no_drop_when_healthy_or_unknown():
    for state in (gates.STATE_OK, gates.STATE_UNKNOWN):
        assert gates.should_drop_request('get_asset', state) is False


def test_next_state_thresholds():
    assert gates.next_state(gates.STATE_UNKNOWN, True, 0) == gates.STATE_OK
    # 一次失败不判死
    assert gates.next_state(gates.STATE_UNKNOWN, False, 1) == gates.STATE_UNKNOWN
    # 连续失败达阈值判死
    assert gates.next_state(gates.STATE_UNKNOWN, False, 2) == gates.STATE_UNRESPONSIVE
    # 判死后继续失败保持
    assert gates.next_state(gates.STATE_UNRESPONSIVE, False, 5) == gates.STATE_UNRESPONSIVE
    # 恢复
    assert gates.next_state(gates.STATE_UNRESPONSIVE, True, 0) == gates.STATE_OK


def test_next_state_custom_threshold():
    assert gates.next_state(gates.STATE_UNKNOWN, False, 1, threshold=1) == \
        gates.STATE_UNRESPONSIVE
