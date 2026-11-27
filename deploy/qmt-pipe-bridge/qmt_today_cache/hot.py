# -*- coding: utf-8 -*-
"""热集 tick 缓存（V2）：只存变化、紧凑数组、掉线标 stale。

热集 = 持仓 + `qm:qmt:hot_set` + 指数；由桥的 quotes worker 喂入。
tick 层不保证固定节拍：查不到/落后超过 ``hot_tick_stale_s`` 即标 stale，
调用方（后端）可用 TDX 快照补充。
"""

from __future__ import annotations

import bisect
import threading
from array import array

from .models import (
    date_str,
    normalize_symbol,
    normalize_tick,
    safe_float,
    tick_cum_amount,
    tick_cum_volume,
    tick_last_price,
    tick_ts_ms,
)

_STRIDE = 7
_TS, _LAST, _VOL, _AMT, _BID1, _ASK1, _PAD = range(_STRIDE)


def _first_price(values) -> float | None:
    if isinstance(values, (list, tuple)) and values:
        return safe_float(values[0])
    return None


def _bid1(tick: dict) -> float | None:
    value = _first_price(tick.get("bidPrice"))
    if value is None:
        value = safe_float(tick.get("bid1"))
    return value


def _ask1(tick: dict) -> float | None:
    value = _first_price(tick.get("askPrice"))
    if value is None:
        value = safe_float(tick.get("ask1"))
    return value


class HotTickStore:
    """热集 tick 环形缓存（按 symbol 扁平数组，只存变化/最小间隔）。"""

    def __init__(
        self,
        *,
        max_symbols: int = 800,
        max_per_symbol: int = 1200,
        min_interval_s: float = 1.0,
        stale_s: float = 10.0,
    ):
        self._max_symbols = max(1, int(max_symbols))
        self._max_values = max(60, int(max_per_symbol)) * _STRIDE
        self._min_interval_ms = max(0.0, float(min_interval_s)) * 1000.0
        self._stale_ms = max(1.0, float(stale_s)) * 1000.0
        self._lock = threading.Lock()
        self._data: dict[str, array] = {}
        self._source: dict[str, str] = {}
        self._last: dict[str, tuple] = {}

    # ------------------------------------------------------------------ ingest
    def ingest(self, ticks: dict, now_ms: int, source: str = "qmt") -> list[dict]:
        """吃 tick，返回需要落盘的**新变化**行。"""
        if not ticks:
            return []
        now_ms = int(now_ms)
        rows: list[dict] = []
        with self._lock:
            for code, raw in ticks.items():
                symbol = normalize_symbol(code)
                tick = normalize_tick(raw)
                if not symbol or tick is None:
                    continue
                last = tick_last_price(tick)
                if last is None or last <= 0:
                    continue
                ts = int(tick_ts_ms(tick, now_ms))
                if ts < now_ms - 10 * 60 * 1000:
                    continue
                if ts > now_ms + 120 * 1000:
                    ts = now_ms
                bid1 = _bid1(tick)
                ask1 = _ask1(tick)
                volume = tick_cum_volume(tick)
                amount = tick_cum_amount(tick)
                prev = self._last.get(symbol)
                if prev is not None:
                    prev_ts, prev_last, prev_vol, prev_bid, prev_ask = prev
                    if ts <= int(prev_ts):
                        continue
                    unchanged = (
                        last == prev_last
                        and volume == prev_vol
                        and bid1 == prev_bid
                        and ask1 == prev_ask
                    )
                    if unchanged and (ts - int(prev_ts)) < self._min_interval_ms:
                        continue
                if symbol not in self._data and len(self._data) >= self._max_symbols:
                    continue
                arr = self._data.setdefault(symbol, array("d"))
                arr.extend(
                    (
                        float(ts),
                        float(last),
                        float(volume),
                        float(amount),
                        float(bid1 or 0.0),
                        float(ask1 or 0.0),
                        0.0,
                    )
                )
                if len(arr) > self._max_values:
                    del arr[: len(arr) - self._max_values]
                self._source.setdefault(symbol, str(source or "qmt"))
                self._last[symbol] = (ts, last, volume, bid1, ask1)
                rows.append(
                    {
                        "date": date_str(ts),
                        "ts_ms": ts,
                        "symbol": symbol,
                        "last": float(last),
                        "volume": float(volume),
                        "amount": float(amount),
                        "bid1": float(bid1 or 0.0),
                        "ask1": float(ask1 or 0.0),
                        "source": str(source or "qmt"),
                    }
                )
        return rows

    # ------------------------------------------------------------------ query
    def query(self, symbols, start_ms: int, end_ms: int, *,
              now_ms: int) -> dict[str, dict]:
        """列式查询；每个 symbol 带 ``stale/age_ms/gap``。"""
        start_ms, end_ms = int(start_ms), int(end_ms)
        now_ms = int(now_ms)
        out: dict[str, dict] = {}
        with self._lock:
            snapshot = {}
            for code in symbols:
                symbol = normalize_symbol(code) or str(code)
                arr = self._data.get(symbol)
                snapshot[symbol] = (
                    arr[:] if arr else array("d"),
                    self._source.get(symbol, "qmt"),
                )
        for symbol, (arr, source) in snapshot.items():
            times = arr[0::_STRIDE]
            lo = bisect.bisect_left(times, start_ms)
            hi = bisect.bisect_right(times, end_ms)
            count = max(0, hi - lo)
            columns = {
                "ts_ms": [
                    int(value)
                    for value in arr[lo * _STRIDE + _TS : hi * _STRIDE + _TS : _STRIDE]
                ],
                "last": list(arr[lo * _STRIDE + _LAST : hi * _STRIDE + _LAST : _STRIDE]),
                "volume": list(arr[lo * _STRIDE + _VOL : hi * _STRIDE + _VOL : _STRIDE]),
                "amount": list(arr[lo * _STRIDE + _AMT : hi * _STRIDE + _AMT : _STRIDE]),
                "bid1": list(arr[lo * _STRIDE + _BID1 : hi * _STRIDE + _BID1 : _STRIDE]),
                "ask1": list(arr[lo * _STRIDE + _ASK1 : hi * _STRIDE + _ASK1 : _STRIDE]),
                "source": [source] * count,
            }
            last_ts = int(times[-1]) if len(times) else 0
            age_ms = max(0, now_ms - last_ts) if last_ts else -1
            columns["stale"] = bool(not last_ts or age_ms > self._stale_ms)
            columns["age_ms"] = age_ms
            columns["gap"] = count == 0
            out[symbol] = columns
        return out

    # ------------------------------------------------------------------ recovery
    def load(self, rows) -> int:
        loaded = 0
        grouped: dict[str, list[dict]] = {}
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            symbol = normalize_symbol(row.get("symbol"))
            ts = int(row.get("ts_ms") or 0)
            if not symbol or ts <= 0:
                continue
            grouped.setdefault(symbol, []).append(row)
        with self._lock:
            for symbol, items in grouped.items():
                if symbol not in self._data and len(self._data) >= self._max_symbols:
                    continue
                arr = self._data.setdefault(symbol, array("d"))
                existing = {int(arr[pos + _TS]) for pos in range(0, len(arr), _STRIDE)}
                items.sort(key=lambda item: int(item.get("ts_ms") or 0))
                for row in items:
                    ts = int(row.get("ts_ms") or 0)
                    if ts in existing:
                        continue
                    arr.extend(
                        (
                            float(ts),
                            float(row.get("last") or 0.0),
                            float(row.get("volume") or 0.0),
                            float(row.get("amount") or 0.0),
                            float(row.get("bid1") or 0.0),
                            float(row.get("ask1") or 0.0),
                            0.0,
                        )
                    )
                    existing.add(ts)
                    loaded += 1
                if len(arr) > self._max_values:
                    del arr[: len(arr) - self._max_values]
                self._source.setdefault(symbol, str(items[-1].get("source") or "qmt"))
                last = items[-1]
                self._last[symbol] = (
                    int(last.get("ts_ms") or 0),
                    float(last.get("last") or 0.0),
                    float(last.get("volume") or 0.0),
                    row_get(last, "bid1"),
                    row_get(last, "ask1"),
                )
        return loaded

    def reset(self) -> None:
        with self._lock:
            self._data.clear()
            self._source.clear()
            self._last.clear()

    def status(self, now_ms: int) -> dict:
        with self._lock:
            symbols = len(self._data)
            ticks = sum(len(v) // _STRIDE for v in self._data.values())
            stale = 0
            newest = 0
            for arr in self._data.values():
                if not len(arr):
                    continue
                last_ts = int(arr[-_STRIDE + _TS])
                newest = max(newest, last_ts)
                if int(now_ms) - last_ts > self._stale_ms:
                    stale += 1
        return {
            "symbols": symbols,
            "ticks": ticks,
            "stale_symbols": stale,
            "newest_age_ms": max(0, int(now_ms) - newest) if newest else -1,
        }


def row_get(row: dict, key: str):
    try:
        return row.get(key)
    except AttributeError:
        return None
