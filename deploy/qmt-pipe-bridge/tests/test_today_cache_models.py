"""qmt_today_cache.models 纯函数单测（Linux 可跑）。"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

_BASE = Path(__file__).resolve().parents[1]
if str(_BASE) not in sys.path:
    sys.path.insert(0, str(_BASE))

from qmt_today_cache import models as m  # noqa: E402


def _ms(text: str) -> int:
    return int(datetime.strptime(text, "%Y%m%d %H:%M:%S")
               .replace(tzinfo=m.CN_TZ).timestamp() * 1000)


def test_symbol_normalization_and_validation():
    assert m.normalize_symbol(" 600036.sh ") == "600036.SH"
    assert m.normalize_symbol("000001.SZ") == "000001.SZ"
    assert m.normalize_symbol("bad code") == ""
    assert m.is_valid_symbol("600036.SH")
    assert not m.is_valid_symbol("../../etc/passwd")


def test_timetag_parse_and_fallback():
    tick = {"timetag": "20261127 14:55:32", "lastPrice": 42.7}
    assert m.tick_ts_ms(tick, 0) == _ms("20261127 14:55:32")
    assert m.parse_timetag_ms({"time": 1795762532000}) == 1795762532000
    assert m.parse_timetag_ms({"time": 1795762532}) == 1795762532000
    assert m.tick_ts_ms({"bad": 1}, 1234) == 1234


def test_normalize_tick_keeps_qmt_shape_and_skips_bad_price():
    tick = {
        "timetag": "20261127 14:55:32",
        "lastPrice": 42.65,
        "lastClose": 43.0,
        "bidPrice": [42.6, 42.5],
        "askPrice": [42.7, 42.8],
        "volume": 1407012,
        "amount": 6007339000.0,
        "stockStatus": 3,
    }
    norm = m.normalize_tick(tick)
    assert norm["lastPrice"] == 42.65
    assert norm["bidPrice"] == [42.6, 42.5]
    assert m.normalize_tick({"lastPrice": 0}) is None
    assert m.normalize_tick({"lastPrice": None}) is None


def test_minute_and_date():
    ts = _ms("20261127 14:55:32")
    assert m.minute_start_ms(ts) == _ms("20261127 14:55:00")
    assert m.date_str(ts) == "20261127"
    start, end = m.day_bounds_ms("20261127")
    assert m.date_str(start) == "20261127"
    assert end - start == 24 * 3600 * 1000 - 1


def test_parse_range_accepts_date_and_epoch():
    now = _ms("20261127 14:55:00")
    start, end = m.parse_range_ms("20261127", None, now)
    assert start == m.day_bounds_ms("20261127")[0]
    assert end == m.day_bounds_ms("20261127")[1]
    start2, end2 = m.parse_range_ms(
        _ms("20261127 09:30:00"), _ms("20261127 09:31:00"), now
    )
    assert start2 == _ms("20261127 09:30:00")
    assert end2 == _ms("20261127 09:31:00")


def test_cache_session_window():
    assert m.in_cache_session(_ms("20261127 09:20:00"))
    assert m.in_cache_session(_ms("20261127 14:55:00"))
    assert not m.in_cache_session(_ms("20261127 12:00:00"))
    assert not m.in_cache_session(_ms("20261128 10:00:00"))  # Saturday


def test_vwap():
    assert m.vwap_of(1000, 100, 9.9) == 10.0
    assert m.vwap_of(0, 0, 9.9) == 9.9
