"""bridge_stability 纯函数单测（Linux 可跑，无 Windows 依赖）。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parents[1] / 'bridge_stability.py'
_spec = importlib.util.spec_from_file_location('bridge_stability', _MODULE_PATH)
bs = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = bs
_spec.loader.exec_module(bs)


# ---------------------------------------------------------------- Governor
def test_fast_success_keeps_governor_at_1x():
    st = bs.observe(bs.GovernorState(), 200.0, True)
    assert st.multiplier == 1.0
    assert st.ewma_ms == 200.0


def test_slow_success_degrades_to_2x_before_timeouts():
    st = bs.observe(bs.GovernorState(), 2000.0, True)
    assert st.multiplier == 2.0
    assert bs.effective_interval(3.0, st) == 6.0


def test_timeout_doubles_multiplier_and_caps():
    st = bs.GovernorState()
    for _ in range(6):
        st = bs.observe(st, 0.0, False)
    assert st.multiplier == bs.GovernorConfig().max_multiplier
    assert st.timeouts == 6
    assert st.ewma_ms >= bs.GovernorConfig().very_slow_ms


def test_recovery_halves_only_after_consecutive_health():
    st = bs.observe(bs.GovernorState(multiplier=4.0, ewma_ms=4000.0), 100.0, True)
    assert st.multiplier == 4.0  # 一次快查询不直接恢复
    st = bs.observe(st, 100.0, True)
    assert st.multiplier == 4.0
    st = bs.observe(st, 100.0, True)
    assert st.multiplier == 2.0  # 连续 3 次健康才减半
    assert bs.effective_interval(2.0, st) == 4.0


def test_slow_ewma_keeps_at_least_2x_after_recovery_attempt():
    st = bs.GovernorState(multiplier=1.0, ewma_ms=0.0)
    for _ in range(3):
        st = bs.observe(st, 2000.0, True)
    assert st.multiplier >= 2.0


# ---------------------------------------------------------------- Cache
def test_trade_cache_hit_miss_and_ttl():
    now = [1000.0]
    cache = bs.TradeReadCache(clock=lambda: now[0])
    assert cache.get('get_asset', {}) == (False, None)
    cache.put('get_asset', {}, {'total_asset': 1}, ttl_s=2.0)
    assert cache.get('get_asset', {}) == (True, {'total_asset': 1})
    now[0] += 2.1
    assert cache.get('get_asset', {}) == (False, None)
    stats = cache.stats()
    assert stats['hits'] == 1 and stats['misses'] == 2


def test_trade_cache_key_normalises_param_order():
    assert bs.TradeReadCache.key('query_orders', {'cancelable_only': False, 'x': 1}) == (
        bs.TradeReadCache.key('QUERY_ORDERS', {'x': 1, 'cancelable_only': False})
    )


def test_trade_cache_invalidate_clears_entries():
    cache = bs.TradeReadCache()
    cache.put('get_positions', {}, [{'code': '600036.SH'}], ttl_s=5)
    cache.invalidate()
    assert cache.get('get_positions', {}) == (False, None)
    assert cache.stats()['invalidations'] == 1


def test_trade_read_ttl_defaults_and_overrides():
    assert bs.trade_read_ttl('get_asset') == 2.0
    assert bs.trade_read_ttl('query_stock_orders') == 1.0
    assert bs.trade_read_ttl('unknown_method', default_s=0.5) == 0.5


def test_cacheable_whitelist_excludes_non_reads():
    assert bs.is_cacheable_trade_read('get_asset') is True
    assert bs.is_cacheable_trade_read('query_stock_orders') is True
    # trade-context 里的非纯读方法绝不能缓存
    assert bs.is_cacheable_trade_read('sync_positions') is False
    assert bs.is_cacheable_trade_read('reload_deployment') is False
    assert bs.is_cacheable_trade_read('order_stock') is False


# ---------------------------------------------------------------- Restart guard
def _stamps(*seconds_ago: float, now: float = 10000.0) -> list[float]:
    return [now - s for s in seconds_ago]


def test_cap_stamps_keeps_newest_and_drops_old():
    now = 100000.0
    stamps = [now - i for i in range(100)] + [now - 90000.0]
    capped = bs.cap_stamps(stamps, now, bs.RestartConfig(stamp_keep=10))
    assert len(capped) == 10
    assert max(capped) == now
    assert min(capped) == now - 9


def test_restart_storm_blocks_further_restarts():
    now = 100000.0
    stamps = _stamps(10, 20, 30, 40, now=now)  # 30 分钟内 4 次
    dec = bs.restart_decision(stamps, now)
    assert dec.allowed is False
    assert dec.storm is True


def test_restart_hourly_budget_blocks():
    now = 100000.0
    cfg = bs.RestartConfig(storm_threshold=99, hourly_max=3)
    stamps = _stamps(100, 200, 300, now=now)
    dec = bs.restart_decision(stamps, now, cfg)
    assert dec.allowed is False
    assert 'hourly' in dec.reason


def test_restart_exponential_backoff_between_attempts():
    now = 100000.0
    cfg = bs.RestartConfig(base_backoff_s=60, storm_threshold=99, hourly_max=99)
    stamps = _stamps(10, now=now)  # 10s 前刚重启过 → 第二次要等 60s
    dec = bs.restart_decision(stamps, now, cfg)
    assert dec.allowed is False
    assert 'backoff' in dec.reason
    assert dec.backoff_remaining_s > 0


def test_restart_allowed_when_backoff_elapsed():
    now = 100000.0
    cfg = bs.RestartConfig(base_backoff_s=60, storm_threshold=99, hourly_max=99)
    stamps = _stamps(120, now=now)
    assert bs.restart_decision(stamps, now, cfg).allowed is True


def test_append_stamp_caps_immediately():
    now = 100000.0
    cfg = bs.RestartConfig(stamp_keep=3)
    stamps = [now - 1, now - 2, now - 3, now - 4]
    out = bs.append_stamp(stamps, now, cfg)
    assert len(out) == 3
    assert max(out) == now
