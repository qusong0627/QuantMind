# -*- coding: utf-8 -*-
"""今日行情内存层：最新快照 + 1m 聚合（紧凑数组、线程安全、可单测）。

1m 完成 bar 用 ``array('d')`` 扁平存储（每 bar 10 个 double），全市场
5,225 只 × 240 根 ≈ 100MB，避免 Python dict 的数百 MB 开销。
"""

from __future__ import annotations

import bisect
import threading
import time
from array import array
from datetime import datetime

from .models import (
    CN_TZ,
    date_str,
    minute_start_ms,
    normalize_symbol,
    normalize_tick,
    public_bar,
    tick_cum_amount,
    tick_cum_volume,
    tick_last_price,
    tick_ts_ms,
    vwap_of,
)

#: 超过该时长的旧 tick 不参与 1m 聚合（防止隔夜/久远旧值造出今天的 bar）。
#: 10 分钟：冷门股可能很久没有成交，旧 last 不该生成新的 1m bar。
_STALE_TICK_MS = 10 * 60 * 1000
#: 时钟偏差容忍（未来时间）。
_FUTURE_TICK_MS = 120 * 1000

#: 扁平 bar 的 stride / 索引。
_STRIDE = 10
_TS, _OPEN, _HIGH, _LOW, _CLOSE, _VOL, _AMT, _VWAP, _CUMV, _CUMA = range(_STRIDE)


class LatestSnapshot:
    """全市场最新 tick（copy-on-write，读路径无锁）。"""

    def __init__(self, *, max_age_s: float = 5.0, max_symbols: int = 6000,
                 clock=time.time):
        self._max_age_ms = max(1.0, float(max_age_s)) * 1000.0
        self._max_symbols = max(100, int(max_symbols))
        self._clock = clock
        self._lock = threading.Lock()
        self._data: dict[str, tuple[dict, int]] = {}

    def update(self, ticks: dict, now_ms: int) -> int:
        if not ticks:
            return 0
        written = 0
        # 多个 ingest 线程（snapshot/sweep/quotes）并发：copy+swap 必须在锁内，
        # 否则后写者会用旧副本覆盖掉另一线程刚写入的 symbol。
        with self._lock:
            current = dict(self._data)
            for code, tick in ticks.items():
                symbol = normalize_symbol(code)
                norm = normalize_tick(tick)
                if not symbol or norm is None:
                    continue
                current[symbol] = (norm, int(tick_ts_ms(norm, now_ms)))
                written += 1
            if len(current) > self._max_symbols:
                keep = sorted(
                    current.items(), key=lambda item: item[1][1], reverse=True
                )
                current = dict(keep[: self._max_symbols])
            self._data = current
        return written

    def get(self, codes, now_ms: int) -> tuple[dict | None, str]:
        if not codes:
            return None, "miss"
        data = self._data
        out: dict[str, dict] = {}
        for code in codes:
            symbol = normalize_symbol(code)
            item = data.get(symbol) if symbol else None
            if item is None:
                return None, "miss"
            tick, ts_ms = item
            if int(now_ms) - int(ts_ms) > self._max_age_ms:
                return None, "stale"
            out[symbol] = tick
        return out, "hit"

    def clear(self) -> None:
        with self._lock:
            self._data = {}

    def status(self, now_ms: int) -> dict:
        data = self._data
        if not data:
            return {"symbols": 0, "age_max_ms": -1}
        newest = max(ts for _, ts in data.values())
        return {
            "symbols": len(data),
            "age_max_ms": int(max(0, int(now_ms) - int(newest))),
        }


class Bar1mStore:
    """当天 1m 聚合：完成 bar 紧凑数组，当前 bar 单独维护。"""

    def __init__(self, *, max_bars_per_symbol: int = 320):
        self._max_bars = max(60, int(max_bars_per_symbol))
        self._lock = threading.Lock()
        self._completed: dict[str, array] = {}
        self._source: dict[str, str] = {}
        self._date: dict[str, str] = {}
        self._current: dict[str, dict] = {}
        self._recovered_cum: dict[str, tuple[float, float]] = {}

    # ------------------------------------------------------------------ ingest
    def ingest(self, ticks: dict, now_ms: int, source: str = "qmt") -> list[dict]:
        """吃掉一批 tick，返回本批**完成**的 1m bar。"""
        if not ticks:
            return []
        now_ms = int(now_ms)
        finalized: list[dict] = []
        with self._lock:
            for code, tick in ticks.items():
                symbol = normalize_symbol(code)
                norm = normalize_tick(tick)
                if not symbol or norm is None:
                    continue
                last = tick_last_price(norm)
                if last is None or last <= 0:
                    continue
                ts = int(tick_ts_ms(norm, now_ms))
                if ts < now_ms - _STALE_TICK_MS:
                    continue
                if ts > now_ms + _FUTURE_TICK_MS:
                    ts = now_ms
                minute = minute_start_ms(ts)
                cum_v = tick_cum_volume(norm)
                cum_a = tick_cum_amount(norm)
                state = self._current.get(symbol)
                if state is None:
                    start_v, start_a = self._first_start(
                        symbol, minute, cum_v, cum_a
                    )
                    state = self._new_state(
                        symbol, minute, last, ts, cum_v, cum_a, source,
                        start_v=start_v, start_a=start_a,
                    )
                    self._current[symbol] = state
                elif minute < int(state["minute_ts"]):
                    continue
                elif ts < int(state["_last_ts"]):
                    # 同一分钟内的迟到 tick：不能拿旧价覆盖 close/高低点。
                    continue
                elif cum_v + 1.0 < float(state["_cum_v"]):
                    finalized.append(self._finalize(symbol, state))
                    # 累计量重置（账户切换/数据跳变）：从当前累计量重新起算，
                    # 避免把跳变后的全天量当成一根 bar。
                    state = self._new_state(
                        symbol, minute, last, ts, cum_v, cum_a, source,
                        start_v=cum_v, start_a=cum_a,
                    )
                    self._current[symbol] = state
                elif minute > int(state["minute_ts"]):
                    finalized.append(self._finalize(symbol, state))
                    state = self._new_state(
                        symbol,
                        minute,
                        last,
                        ts,
                        cum_v,
                        cum_a,
                        source,
                        start_v=float(state["_cum_v"]),
                        start_a=float(state["_cum_a"]),
                    )
                    self._current[symbol] = state
                self._update_state(state, last, ts, cum_v, cum_a)
        return finalized

    def _first_start(self, symbol, minute, cum_v, cum_a):
        """首 tick 的起始累计量。

        * 有恢复的完成 bar：用最后一根 bar 的累计量（只算之后增量）；
        * 当天开盘首分钟（≤09:30）：从 0 开始，把集合竞价量计入第一根 bar；
        * 盘中重启（如 10:30 才起来）：从当前累计量开始，避免把上午全天量
          算进重启后的第一根 1m bar。
        """
        recovered = self._recovered_cum.pop(symbol, None)
        if recovered is not None:
            return recovered
        dt = datetime.fromtimestamp(float(minute) / 1000.0, CN_TZ)
        if (dt.hour, dt.minute) <= (9, 30):
            return 0.0, 0.0
        return float(cum_v), float(cum_a)

    @staticmethod
    def _new_state(symbol, minute, last, ts, cum_v, cum_a, source, *,
                   start_v=0.0, start_a=0.0) -> dict:
        return {
            "date": date_str(minute),
            "symbol": symbol,
            "minute_ts": int(minute),
            "open": float(last),
            "high": float(last),
            "low": float(last),
            "close": float(last),
            "volume": 0.0,
            "amount": 0.0,
            "vwap": 0.0,
            "source": str(source or "qmt"),
            "final": False,
            "_start_v": float(start_v),
            "_start_a": float(start_a),
            "_cum_v": float(cum_v),
            "_cum_a": float(cum_a),
            "_last_ts": int(ts),
        }

    @staticmethod
    def _update_state(state: dict, last: float, ts: int, cum_v: float,
                      cum_a: float) -> None:
        state["high"] = max(float(state["high"]), float(last))
        state["low"] = min(float(state["low"]), float(last))
        state["close"] = float(last)
        state["volume"] = max(0.0, float(cum_v) - float(state["_start_v"]))
        state["amount"] = max(0.0, float(cum_a) - float(state["_start_a"]))
        state["vwap"] = vwap_of(state["amount"], state["volume"], last)
        state["_cum_v"] = float(cum_v)
        state["_cum_a"] = float(cum_a)
        state["_last_ts"] = int(ts)

    def _finalize(self, symbol: str, state: dict) -> dict:
        row = public_bar(state)
        row["final"] = True
        row["cum_volume"] = float(state["_cum_v"])
        row["cum_amount"] = float(state["_cum_a"])
        row["vwap"] = vwap_of(row.get("amount"), row.get("volume"), row.get("close"))
        arr = self._completed.setdefault(symbol, array("d"))
        arr.extend(
            (
                float(row["minute_ts"]),
                float(row["open"]),
                float(row["high"]),
                float(row["low"]),
                float(row["close"]),
                float(row["volume"]),
                float(row["amount"]),
                float(row["vwap"]),
                float(row["cum_volume"]),
                float(row["cum_amount"]),
            )
        )
        self._source.setdefault(symbol, str(row.get("source") or "qmt"))
        self._date.setdefault(symbol, str(row.get("date") or ""))
        max_values = self._max_bars * _STRIDE
        if len(arr) > max_values:
            del arr[: len(arr) - max_values]
        return dict(row)

    # ------------------------------------------------------------------ close/query
    def finalize_all(self, now_ms: int | None = None) -> list[dict]:
        rows: list[dict] = []
        with self._lock:
            for symbol, state in list(self._current.items()):
                rows.append(self._finalize(symbol, state))
            self._current.clear()
        return rows

    def query_columns(self, codes, start_ms: int, end_ms: int, *,
                      include_current: bool = True) -> dict[str, dict]:
        """列式查询：每个 symbol 返回 ``{field: [values]}``。

        比 list-of-dicts 快一个量级、JSON 也小得多；600 只 × 240 根全量
        可以在几十毫秒内完成（array 切片是 C 级操作）。
        """
        start_ms, end_ms = int(start_ms), int(end_ms)
        with self._lock:
            snapshot: dict[str, tuple[array, str, str, dict | None]] = {}
            for code in codes:
                symbol = normalize_symbol(code) or str(code)
                arr = self._completed.get(symbol)
                current = self._current.get(symbol)
                snapshot[symbol] = (
                    arr[:] if arr else array("d"),
                    self._source.get(symbol, "qmt"),
                    self._date.get(symbol, ""),
                    dict(current) if current is not None else None,
                )
        out: dict[str, dict] = {}
        for symbol, (arr, source, date, current) in snapshot.items():
            times = arr[0::_STRIDE]
            lo = bisect.bisect_left(times, start_ms)
            hi = bisect.bisect_right(times, end_ms)
            count = max(0, hi - lo)
            columns = {
                "date": [date or date_str(int(times[lo])) if count else ""] * count,
                "minute_ts": [
                    int(value) for value in arr[lo * _STRIDE + _TS : hi * _STRIDE + _TS : _STRIDE]
                ],
                "open": list(arr[lo * _STRIDE + _OPEN : hi * _STRIDE + _OPEN : _STRIDE]),
                "high": list(arr[lo * _STRIDE + _HIGH : hi * _STRIDE + _HIGH : _STRIDE]),
                "low": list(arr[lo * _STRIDE + _LOW : hi * _STRIDE + _LOW : _STRIDE]),
                "close": list(arr[lo * _STRIDE + _CLOSE : hi * _STRIDE + _CLOSE : _STRIDE]),
                "volume": list(arr[lo * _STRIDE + _VOL : hi * _STRIDE + _VOL : _STRIDE]),
                "amount": list(arr[lo * _STRIDE + _AMT : hi * _STRIDE + _AMT : _STRIDE]),
                "vwap": list(arr[lo * _STRIDE + _VWAP : hi * _STRIDE + _VWAP : _STRIDE]),
                "cum_volume": list(
                    arr[lo * _STRIDE + _CUMV : hi * _STRIDE + _CUMV : _STRIDE]
                ),
                "cum_amount": list(
                    arr[lo * _STRIDE + _CUMA : hi * _STRIDE + _CUMA : _STRIDE]
                ),
                "source": [source] * count,
                "final": [True] * count,
            }
            if include_current and current is not None:
                row = public_bar(current)
                minute = int(row.get("minute_ts") or 0)
                if start_ms <= minute <= end_ms:
                    for key in (
                        "minute_ts", "open", "high", "low", "close", "volume",
                        "amount", "vwap", "cum_volume", "cum_amount", "source",
                    ):
                        columns.setdefault(key, []).append(row.get(key))
                    columns.setdefault("date", []).append(row.get("date") or date)
                    columns.setdefault("final", []).append(False)
            out[symbol] = columns
        return out

    def query(self, codes, start_ms: int, end_ms: int, *,
              include_current: bool = True) -> dict[str, list[dict]]:
        start_ms, end_ms = int(start_ms), int(end_ms)
        # 先在锁内只做 array 切片（C 级复制，毫秒级），把构造 dict 放到锁外，
        # 避免全市场查询期间阻塞 ingest 线程。
        with self._lock:
            snapshot: dict[str, tuple[array, str, str, dict | None]] = {}
            for code in codes:
                symbol = normalize_symbol(code) or str(code)
                arr = self._completed.get(symbol)
                current = self._current.get(symbol)
                snapshot[symbol] = (
                    arr[:] if arr else array("d"),
                    self._source.get(symbol, "qmt"),
                    self._date.get(symbol, ""),
                    dict(current) if current is not None else None,
                )
        out: dict[str, list[dict]] = {}
        for symbol, (arr, source, date, current) in snapshot.items():
            selected: list[dict] = []
            for offset in range(0, len(arr), _STRIDE):
                ts = int(arr[offset + _TS])
                if ts < start_ms or ts > end_ms:
                    continue
                selected.append(
                    {
                        "date": date or date_str(ts),
                        "symbol": symbol,
                        "minute_ts": ts,
                        "open": arr[offset + _OPEN],
                        "high": arr[offset + _HIGH],
                        "low": arr[offset + _LOW],
                        "close": arr[offset + _CLOSE],
                        "volume": arr[offset + _VOL],
                        "amount": arr[offset + _AMT],
                        "vwap": arr[offset + _VWAP],
                        "cum_volume": arr[offset + _CUMV],
                        "cum_amount": arr[offset + _CUMA],
                        "source": source,
                        "final": True,
                    }
                )
            if include_current and current is not None:
                row = public_bar(current)
                row["final"] = False
                if start_ms <= int(row.get("minute_ts") or 0) <= end_ms:
                    selected.append(row)
            selected.sort(key=lambda item: int(item.get("minute_ts") or 0))
            out[symbol] = selected
        return out

    def load(self, rows) -> int:
        """恢复当天已落盘 bar（按 symbol+minute 去重，紧凑数组）。"""
        loaded = 0
        grouped: dict[str, list[dict]] = {}
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            symbol = normalize_symbol(row.get("symbol"))
            minute = int(row.get("minute_ts") or 0)
            if not symbol or minute <= 0:
                continue
            grouped.setdefault(symbol, []).append(row)
        with self._lock:
            for symbol, items in grouped.items():
                arr = self._completed.setdefault(symbol, array("d"))
                existing = {
                    int(arr[pos + _TS]) for pos in range(0, len(arr), _STRIDE)
                }
                items.sort(key=lambda item: int(item.get("minute_ts") or 0))
                for row in items:
                    minute = int(row.get("minute_ts") or 0)
                    if minute in existing:
                        continue
                    arr.extend(
                        (
                            float(minute),
                            float(row.get("open") or 0.0),
                            float(row.get("high") or 0.0),
                            float(row.get("low") or 0.0),
                            float(row.get("close") or 0.0),
                            float(row.get("volume") or 0.0),
                            float(row.get("amount") or 0.0),
                            float(row.get("vwap") or 0.0),
                            float(row.get("cum_volume") or 0.0),
                            float(row.get("cum_amount") or 0.0),
                        )
                    )
                    existing.add(minute)
                    loaded += 1
                self._source.setdefault(symbol, str(items[-1].get("source") or "qmt"))
                self._date.setdefault(symbol, str(items[-1].get("date") or ""))
                max_values = self._max_bars * _STRIDE
                if len(arr) > max_values:
                    del arr[: len(arr) - max_values]
                last = items[-1]
                if last.get("cum_volume") is not None and last.get("cum_amount") is not None:
                    self._recovered_cum[symbol] = (
                        float(last.get("cum_volume") or 0.0),
                        float(last.get("cum_amount") or 0.0),
                    )
        return loaded

    def reset(self) -> None:
        with self._lock:
            self._completed.clear()
            self._source.clear()
            self._date.clear()
            self._current.clear()
            self._recovered_cum.clear()

    def counts(self) -> dict:
        with self._lock:
            return {
                "symbols": len(self._completed),
                "completed_bars": sum(len(v) // _STRIDE for v in self._completed.values()),
                "current_bars": len(self._current),
            }
