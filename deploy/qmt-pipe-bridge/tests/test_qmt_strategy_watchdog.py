"""qmt_strategy_watchdog 单测（纯函数 + 假 transport，无 Windows 依赖）。

背景：QMT 策略主线程被交易查询挂住时，ping/行情仍正常（后台线程），
只有 get_asset/query_orders 这类 adjust 线程方法超时。看门狗的判定逻辑
必须能区分「整体不可达」与「假活」，并且绝不在有在途委托时 reload。
"""
from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[1] / 'qmt_strategy_watchdog.py'
_spec = importlib.util.spec_from_file_location('qmt_strategy_watchdog', _MODULE_PATH)
wd = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = wd  # dataclass 解析注解需要模块已注册
_spec.loader.exec_module(wd)


def _ts(hour: int, minute: int, *, day: int = 6) -> float:
    """2026-11-06 是周五；用本地墙钟构造时间戳（函数本身按 localtime 判断）。"""
    return time.mktime((2026, 11, day, hour, minute, 0, 0, 0, -1))


def _probe(reachable=True, account_ok=True, error=''):
    return wd.Probe(reachable=reachable, account_ok=account_ok, error=error)


class FakeTransport:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def send_request(self, req, timeout_seconds):
        self.calls.append((req.get('method'), timeout_seconds))
        result = self.responses.get(req.get('method'))
        if isinstance(result, Exception):
            raise result
        return result


# --------------------------------------------------------------------------
# 判定：什么时候 reload / 什么时候只观察
# --------------------------------------------------------------------------
def test_ok_resets_failures():
    prev = {'failures': 3, 'reload_count': 1, 'day': time.strftime('%Y%m%d')}
    d = wd.evaluate(prev, _probe(), now_ts=_ts(10, 0), queue_has_orders=False)
    assert d.action == 'ok'
    assert d.failures == 0


def test_first_timeout_only_observes():
    d = wd.evaluate({}, _probe(account_ok=False), now_ts=_ts(10, 0),
                    queue_has_orders=False)
    assert d.action == 'observe'
    assert d.failures == 1


def test_threshold_timeouts_schedule_reload():
    prev = {'failures': 1, 'day': time.strftime('%Y%m%d')}
    d = wd.evaluate(prev, _probe(account_ok=False), now_ts=_ts(10, 0),
                    queue_has_orders=False)
    assert d.action == 'reload'
    assert d.failures == 2
    assert d.state['reload_count'] == 1
    assert d.state['last_reload_ts'] == _ts(10, 0)


def test_order_in_flight_blocks_reload():
    prev = {'failures': 1, 'day': time.strftime('%Y%m%d')}
    d = wd.evaluate(prev, _probe(account_ok=False), now_ts=_ts(10, 0),
                    queue_has_orders=True)
    assert d.action == 'observe'
    assert 'order' in d.reason


def test_cooldown_blocks_second_reload():
    now = _ts(10, 30)
    prev = {'failures': 3, 'day': time.strftime('%Y%m%d'),
            'last_reload_ts': now - 60, 'reload_count': 1}
    d = wd.evaluate(prev, _probe(account_ok=False), now_ts=now,
                    queue_has_orders=False)
    assert d.action == 'observe'
    assert 'cooldown' in d.reason


def test_daily_budget_exhausted_alerts():
    prev = {'failures': 3, 'day': time.strftime('%Y%m%d'),
            'last_reload_ts': 0, 'reload_count': 3}
    d = wd.evaluate(prev, _probe(account_ok=False), now_ts=_ts(10, 0),
                    queue_has_orders=False)
    assert d.action == 'alert'
    assert 'manual restart' in d.reason


def test_no_reload_outside_trading_hours():
    d = wd.evaluate({'failures': 1, 'day': time.strftime('%Y%m%d')},
                    _probe(account_ok=False), now_ts=_ts(8, 0),
                    queue_has_orders=False)
    assert d.action == 'observe'


def test_ping_failure_is_unreachable_alert_not_reload():
    d = wd.evaluate({}, _probe(reachable=False, account_ok=False),
                    now_ts=_ts(10, 0), queue_has_orders=False)
    assert d.action == 'alert'
    assert 'unreachable' in d.reason


def test_new_day_resets_reload_budget():
    prev = {'failures': 3, 'day': '20261105', 'last_reload_ts': 0,
            'reload_count': 3}
    d = wd.evaluate(prev, _probe(account_ok=False), now_ts=_ts(10, 0),
                    queue_has_orders=False)
    assert d.action == 'reload'
    assert d.state['reload_count'] == 1


# --------------------------------------------------------------------------
# 交易时段边界
# --------------------------------------------------------------------------
@pytest.mark.parametrize('hour,minute,expected', [
    (9, 14, False), (9, 15, True), (11, 35, True), (11, 36, False),
    (12, 55, True), (15, 10, True), (15, 11, False),
])
def test_trading_window_boundaries(hour, minute, expected):
    assert wd.in_trading_window(_ts(hour, minute)) is expected


def test_trading_window_closed_on_weekend():
    # 2026-11-07 是周六
    assert wd.in_trading_window(_ts(10, 0, day=7)) is False


# --------------------------------------------------------------------------
# 队列解码（fail-closed）
# --------------------------------------------------------------------------
def test_queue_without_orders():
    assert wd.queue_has_order_requests(
        [], decode_payload=lambda raw: raw) is False


def test_queue_detects_order_method():
    payload = '{"method":"order_stock","params":{}}'
    assert wd.queue_has_order_requests([payload], decode_payload=lambda raw: raw)


def test_queue_ignores_read_only_methods():
    payload = '{"method":"query_stock_positions","params":{}}'
    assert not wd.queue_has_order_requests([payload], decode_payload=lambda raw: raw)


def test_undecodable_item_counts_as_order():
    assert wd.queue_has_order_requests(['not-json'], decode_payload=lambda raw: raw)


# --------------------------------------------------------------------------
# 探测：区分「整体不可达」与「假活」
# --------------------------------------------------------------------------
def test_probe_ping_timeout_marks_unreachable():
    transport = FakeTransport({'ping': TimeoutError('pipe timeout')})
    probe = wd.probe_once(transport, {**wd.DEFAULT_CONFIG})
    assert probe.reachable is False
    assert probe.account_ok is False


def test_probe_ping_ok_asset_timeout_is_fake_alive():
    transport = FakeTransport({
        'ping': {'ok': True, 'data': {'pong': True}},
        'get_asset': TimeoutError('named pipe rpc timeout'),
    })
    probe = wd.probe_once(transport, {**wd.DEFAULT_CONFIG})
    assert probe.reachable is True
    assert probe.account_ok is False
    assert 'timeout' in probe.error.lower()


def test_probe_account_ok():
    transport = FakeTransport({
        'ping': {'ok': True, 'data': {'pong': True}},
        'get_asset': {'ok': True, 'data': {'cash': 1.0}},
    })
    probe = wd.probe_once(transport, {**wd.DEFAULT_CONFIG})
    assert probe.reachable and probe.account_ok


# --------------------------------------------------------------------------
# 配置：与伴生桥共用同一份 bridge_config.json
# --------------------------------------------------------------------------
def test_load_config_maps_bridge_config(tmp_path):
    path = tmp_path / 'bridge_config.json'
    path.write_text(json.dumps({
        'account_id': '40327478',
        'redis': {'host': '192.168.31.68', 'port': 6390, 'db': 5},
        'pipe': {'name': 'bigqmt_rpc'},
        'watchdog': {'fail_threshold': 3, 'reload_max_per_day': 5},
    }), encoding='utf-8')
    cfg = wd.load_config(str(path))
    assert cfg['account_id'] == '40327478'
    assert cfg['redis_host'] == '192.168.31.68'
    assert cfg['redis_port'] == 6390
    assert cfg['redis_db'] == 5
    assert cfg['fail_threshold'] == 3
    assert cfg['reload_max_per_day'] == 5


def test_load_config_missing_file_uses_defaults(tmp_path):
    cfg = wd.load_config(str(tmp_path / 'nope.json'))
    assert cfg['fail_threshold'] == wd.DEFAULT_CONFIG['fail_threshold']
    assert cfg['enabled'] is True

