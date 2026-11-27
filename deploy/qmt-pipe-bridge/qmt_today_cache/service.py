# -*- coding: utf-8 -*-
"""今日行情缓存服务：内存 + Parquet + 采集线程 + RPC 查询（薄编排）。"""

from __future__ import annotations

import threading
import time

from .config import TodayCacheConfig
from .hot import HotTickStore
from .memory import Bar1mStore, LatestSnapshot
from .models import (
    date_str,
    in_cache_session,
    normalize_symbol,
    normalize_tick,
    parse_range_ms,
    safe_float,
    tick_last_price,
    tick_ts_ms,
)
from .parquet_store import PartStore


class TodayCacheService:
    """今日行情缓存（只读服务；订单路径不经过这里）。"""

    def __init__(
        self,
        config: TodayCacheConfig,
        *,
        fetcher,
        universe_provider,
        session_provider=None,
        logger=None,
        clock=time.time,
    ):
        self.config = config
        self.enabled = bool(config.enabled)
        self._fetcher = fetcher
        self._universe_provider = universe_provider
        self._session_provider = session_provider
        self._clock = clock
        self._log = logger or (lambda msg: None)

        self.latest = LatestSnapshot(
            max_age_s=config.max_age_s, max_symbols=config.max_symbols, clock=clock
        )
        self.bars = Bar1mStore(max_bars_per_symbol=config.max_bars_per_symbol)
        self.hot = HotTickStore(
            max_symbols=config.hot_max_symbols,
            max_per_symbol=config.hot_tick_max_per_symbol,
            min_interval_s=config.hot_tick_min_interval_s,
            stale_s=config.hot_tick_stale_s,
        )
        self.store = PartStore(
            config.dir,
            part_interval_s=config.part_interval_s,
            part_max_rows=config.part_max_rows,
            retention_days=config.bar1m_retention_days,
            kind_retention={
                "bar1m": config.bar1m_retention_days,
                "tick_hot": config.hot_tick_retention_days,
                "snapshot3s": 5,
            },
            write_queue_max=config.write_queue_max,
            compress=config.compress,
            spool_fallback=config.spool_fallback,
            disk_min_free_gb=config.disk_min_free_gb,
            logger=self._log,
            clock=clock,
        )
        self._stop = threading.Event()
        self._snapshot_thread: threading.Thread | None = None
        self._date = ""
        self._lock = threading.Lock()
        self._rollover_lock = threading.Lock()
        self._counters = {
            "hits": 0,
            "misses": 0,
            "stale": 0,
            "queries": 0,
            "ingest_calls": 0,
            "ingest_ticks": 0,
            "finalized_bars": 0,
            "recovered_bars": 0,
            "recovered_hot_ticks": 0,
            "hot_ingest_ticks": 0,
            "hot_queries": 0,
            "last_hot_ingest_ts": 0.0,
            "snapshot3s_rows": 0,
            "snapshot3s_rounds": 0,
            "last_snapshot3s_ts": 0.0,
            "snapshot_rounds": 0,
            "errors": 0,
            "last_ingest_ts": 0.0,
            "last_snapshot_ts": 0.0,
        }

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if not self.enabled:
            self._log("today_cache disabled by config")
            return
        self.store.start()
        try:
            self._recover()
        except Exception as exc:  # noqa: BLE001 - 恢复失败不能拦住服务
            self._bump("errors")
            self._log("today_cache recover failed: %r" % (exc,))
        self._snapshot_thread = threading.Thread(
            target=self._snapshot_loop, daemon=True, name="today_cache_snapshot"
        )
        self._snapshot_thread.start()
        self._log(
            "today_cache started dir=%s interval=%.1fs format=%s"
            % (
                self.config.dir,
                self.config.snapshot_interval_s,
                self.store.status().get("format"),
            )
        )

    def stop(self) -> None:
        if not self.enabled:
            return
        self._stop.set()
        if self._snapshot_thread is not None:
            self._snapshot_thread.join(timeout=10.0)
            self._snapshot_thread = None
        try:
            rows = self.bars.finalize_all(int(self._clock() * 1000))
            if rows:
                self.store.append("bar1m", rows)
        except Exception as exc:  # noqa: BLE001
            self._log("today_cache finalize failed: %r" % (exc,))
        self.store.stop()

    def _recover(self) -> None:
        today = date_str(int(self._clock() * 1000))
        rows = self.store.read("bar1m", today)
        loaded = self.bars.load(rows)
        hot_loaded = 0
        if self.config.hot_enabled:
            hot_rows = self.store.read("tick_hot", today)
            hot_loaded = self.hot.load(hot_rows)
        with self._lock:
            self._date = today
            self._counters["recovered_bars"] = int(loaded)
            self._counters["recovered_hot_ticks"] = int(hot_loaded)
        self.store.cleanup(today)

    # ------------------------------------------------------------------ ingest
    def ingest_ticks(self, ticks: dict, source: str = "qmt") -> int:
        """非阻塞：更新最新快照 + 1m 聚合 + 落盘队列。返回有效 tick 数。"""
        if not self.enabled or not ticks:
            return 0
        now_ms = int(self._clock() * 1000)
        today = date_str(now_ms)
        try:
            if self._date != today:
                with self._rollover_lock:
                    if self._date != today:
                        rows = self.bars.finalize_all(now_ms)
                        if rows:
                            self.store.append("bar1m", rows)
                        self.bars.reset()
                        self.latest.clear()
                        self.hot.reset()
                        with self._lock:
                            self._date = today
                        self.store.cleanup(today)
            written = self.latest.update(ticks, now_ms)
            finalized = self.bars.ingest(ticks, now_ms, source=source)
            if finalized:
                self.store.append("bar1m", finalized)
            with self._lock:
                self._counters["ingest_calls"] += 1
                self._counters["ingest_ticks"] += int(written)
                self._counters["finalized_bars"] += len(finalized)
                self._counters["last_ingest_ts"] = now_ms / 1000.0
            return written
        except Exception as exc:  # noqa: BLE001 - 缓存异常不影响行情链路
            self._bump("errors")
            self._log("today_cache ingest failed: %r" % (exc,))
            return 0

    def ingest_hot_ticks(self, ticks: dict, source: str = "qmt_quotes") -> int:
        """热集 tick 入缓存（只存变化）；返回新落盘行数。"""
        if not self.enabled or not self.config.hot_enabled or not ticks:
            return 0
        now_ms = int(self._clock() * 1000)
        try:
            rows = self.hot.ingest(ticks, now_ms, source)
            if rows:
                self.store.append("tick_hot", rows)
            with self._lock:
                self._counters["hot_ingest_ticks"] += len(rows)
                self._counters["last_hot_ingest_ts"] = now_ms / 1000.0
            return len(rows)
        except Exception as exc:  # noqa: BLE001
            self._bump("errors")
            self._log("today_cache hot ingest failed: %r" % (exc,))
            return 0

    def ingest_snapshot3s(self, ticks: dict, round_ts_ms: int) -> int:
        """全市场 3s 快照 → ``snapshot3s`` Parquet（跨截面同一时间戳）。"""
        if not self.enabled or not self.config.snapshot3s_enabled or not ticks:
            return 0
        round_ts_ms = int(round_ts_ms)
        date = date_str(round_ts_ms)
        rows: list[dict] = []
        for code, raw in ticks.items():
            symbol = normalize_symbol(code)
            tick = normalize_tick(raw)
            if not symbol or tick is None:
                continue
            last = tick_last_price(tick)
            if last is None or last <= 0:
                continue
            tick_ts = int(tick_ts_ms(tick, round_ts_ms))
            quality = "ok" if abs(round_ts_ms - tick_ts) <= 5000 else "stale"
            bid_price = tick.get("bidPrice")
            ask_price = tick.get("askPrice")
            bid1 = _level1(bid_price)
            ask1 = _level1(ask_price)
            bid_vol = tick.get("bidVol")
            ask_vol = tick.get("askVol")
            rows.append(
                {
                    "date": date,
                    "ts_ms": round_ts_ms,
                    "symbol": symbol,
                    "last": float(last),
                    "open": _num(tick.get("open")),
                    "high": _num(tick.get("high")),
                    "low": _num(tick.get("low")),
                    "pre_close": _num(tick.get("lastClose")),
                    "volume": _num(tick.get("volume")),
                    "amount": _num(tick.get("amount")),
                    "bid1": bid1,
                    "ask1": ask1,
                    "bid_vol1": _level1(bid_vol),
                    "ask_vol1": _level1(ask_vol),
                    "source": "qmt_snapshot",
                    "quality": quality,
                }
            )
        if not rows:
            return 0
        try:
            self.store.append("snapshot3s", rows)
            with self._lock:
                self._counters["snapshot3s_rows"] += len(rows)
                self._counters["snapshot3s_rounds"] += 1
                self._counters["last_snapshot3s_ts"] = round_ts_ms / 1000.0
            return len(rows)
        except Exception as exc:  # noqa: BLE001
            self._bump("errors")
            self._log("today_cache snapshot3s failed: %r" % (exc,))
            return 0

    # ------------------------------------------------------------------ snapshot loop
    def _snapshot_loop(self) -> None:
        if self._stop.wait(max(0.0, self.config.startup_grace_s)):
            return
        while not self._stop.is_set():
            started = time.perf_counter()
            try:
                if self.config.session_only and not self._in_session():
                    self._stop.wait(5.0)
                    continue
                codes = self._universe()
                if not codes:
                    self._stop.wait(30.0)
                    continue
                self._collect(codes)
                self._bump("snapshot_rounds")
                with self._lock:
                    self._counters["last_snapshot_ts"] = self._clock()
            except Exception as exc:  # noqa: BLE001
                self._bump("errors")
                self._log("today_cache snapshot failed: %r" % (exc,))
            elapsed = time.perf_counter() - started
            self._stop.wait(max(0.5, self.config.snapshot_interval_s - elapsed))

    def _in_session(self) -> bool:
        if self._session_provider is not None:
            try:
                return bool(self._session_provider())
            except Exception:  # noqa: BLE001
                return False
        return in_cache_session(int(self._clock() * 1000))

    def _universe(self) -> list[str]:
        try:
            codes = self._universe_provider() or []
        except Exception as exc:  # noqa: BLE001
            self._bump("errors")
            self._log("today_cache universe failed: %r" % (exc,))
            return []
        seen: set[str] = set()
        out: list[str] = []
        for code in codes:
            symbol = normalize_symbol(code)
            if not symbol or symbol in seen:
                continue
            seen.add(symbol)
            out.append(symbol)
            if len(out) >= self.config.max_symbols:
                break
        return out

    def _collect(self, codes: list[str]) -> None:
        chunk = self.config.chunk
        round_ts = int(self._clock() * 1000)
        for start in range(0, len(codes), chunk):
            if self._stop.is_set():
                return
            part = codes[start : start + chunk]
            try:
                data = self._fetcher(part)
            except Exception as exc:  # noqa: BLE001 - 单批失败不影响其他批
                self._bump("errors")
                self._log("today_cache fetch failed (%d codes): %r" % (len(part), exc))
                continue
            if isinstance(data, dict) and data:
                self.ingest_ticks(data, source="qmt_snapshot")
                self.ingest_snapshot3s(data, round_ts)

    # ------------------------------------------------------------------ RPC
    def try_handle_rpc(self, method: str, req: dict):
        """命中返回完整响应 dict；未命中/不处理返回 None。"""
        if not self.enabled:
            return None
        try:
            params = (req or {}).get("params") or {}
            if method == "get_full_tick":
                codes = self._codes(params, self.config.max_symbols)
                if not codes:
                    return None
                hit, reason = self.latest.get(codes, int(self._clock() * 1000))
                if hit is None:
                    self._bump("stale" if reason == "stale" else "misses")
                    return None
                self._bump("hits")
                return self._envelope(req, hit)
            if method == "cache_today_1m":
                codes = self._codes(params, self.config.max_symbols)
                if len(codes) > self.config.query_max_symbols:
                    return self._error(
                        req,
                        "TOO_MANY_SYMBOLS: cache_today_1m max=%d, got=%d"
                        % (self.config.query_max_symbols, len(codes)),
                    )
                if not codes:
                    return self._envelope(req, {})
                start_ms, end_ms = parse_range_ms(
                    params.get("start_time"),
                    params.get("end_time"),
                    int(self._clock() * 1000),
                )
                data = self.bars.query_columns(
                    codes, start_ms, end_ms, include_current=True
                )
                self._bump("queries")
                return self._envelope(req, data)
            if method == "cache_hot_ticks":
                if not self.config.hot_enabled:
                    return self._error(req, "HOT_TICKS_DISABLED")
                codes = self._codes(params, self.config.max_symbols)
                if len(codes) > self.config.hot_query_max_symbols:
                    return self._error(
                        req,
                        "TOO_MANY_SYMBOLS: cache_hot_ticks max=%d, got=%d"
                        % (self.config.hot_query_max_symbols, len(codes)),
                    )
                if not codes:
                    return self._envelope(req, {})
                start_ms, end_ms = parse_range_ms(
                    params.get("start_time"),
                    params.get("end_time"),
                    int(self._clock() * 1000),
                )
                data = self.hot.query(
                    codes, start_ms, end_ms, now_ms=int(self._clock() * 1000)
                )
                self._bump("hot_queries")
                return self._envelope(req, data)
            if method == "cache_status":
                return self._envelope(req, self.status())
        except Exception as exc:  # noqa: BLE001 - 查询失败按未命中，回落 QMT
            self._bump("errors")
            self._log("today_cache rpc %s failed: %r" % (method, exc))
        return None

    def _codes(self, params: dict, limit: int | None = None) -> list[str]:
        raw = params.get("codes")
        if raw is None:
            raw = params.get("stock_list")
        if isinstance(raw, str):
            raw = [item for item in raw.split(",") if item.strip()]
        if not isinstance(raw, (list, tuple)):
            return []
        cap = max(1, int(limit or self.config.max_symbols))
        seen: set[str] = set()
        out: list[str] = []
        for code in raw:
            symbol = normalize_symbol(code)
            if not symbol or symbol in seen:
                continue
            seen.add(symbol)
            out.append(symbol)
            if len(out) >= cap:
                break
        return out

    @staticmethod
    def _envelope(req: dict, data) -> dict:
        return {
            "request_id": (req or {}).get("request_id"),
            "ok": True,
            "data": data,
        }

    @staticmethod
    def _error(req: dict, message: str) -> dict:
        return {
            "request_id": (req or {}).get("request_id"),
            "ok": False,
            "error": message,
        }

    # ------------------------------------------------------------------ status
    def status(self) -> dict:
        now_ms = int(self._clock() * 1000)
        store = self.store.status()
        latest = self.latest.status(now_ms)
        hot = self.hot.status(now_ms)
        counts = self.bars.counts()
        with self._lock:
            counters = dict(self._counters)
            date = self._date
        now = self._clock()
        return {
            "enabled": self.enabled,
            "date": date,
            "snapshot_interval_s": self.config.snapshot_interval_s,
            "query_max_symbols": self.config.query_max_symbols,
            "snapshot_rounds": counters.get("snapshot_rounds", 0),
            "last_ingest_age_s": _age(now, counters.get("last_ingest_ts")),
            "last_snapshot_age_s": _age(now, counters.get("last_snapshot_ts")),
            "hits": counters.get("hits", 0),
            "misses": counters.get("misses", 0),
            "stale": counters.get("stale", 0),
            "queries": counters.get("queries", 0),
            "ingest_calls": counters.get("ingest_calls", 0),
            "ingest_ticks": counters.get("ingest_ticks", 0),
            "finalized_bars": counters.get("finalized_bars", 0),
            "recovered_bars": counters.get("recovered_bars", 0),
            "errors": counters.get("errors", 0),
            "latest_symbols": latest.get("symbols", 0),
            "latest_age_max_ms": latest.get("age_max_ms", -1),
            "bar_symbols": counts.get("symbols", 0),
            "completed_bars": counts.get("completed_bars", 0),
            "current_bars": counts.get("current_bars", 0),
            "hot_enabled": bool(self.config.hot_enabled),
            "hot_symbols": hot.get("symbols", 0),
            "hot_ticks": hot.get("ticks", 0),
            "hot_stale_symbols": hot.get("stale_symbols", 0),
            "hot_newest_age_ms": hot.get("newest_age_ms", -1),
            "hot_ingest_ticks": counters.get("hot_ingest_ticks", 0),
            "hot_queries": counters.get("hot_queries", 0),
            "recovered_hot_ticks": counters.get("recovered_hot_ticks", 0),
            "last_hot_ingest_age_s": _age(
                now, counters.get("last_hot_ingest_ts")
            ),
            "snapshot3s_enabled": bool(self.config.snapshot3s_enabled),
            "snapshot3s_rows": counters.get("snapshot3s_rows", 0),
            "snapshot3s_rounds": counters.get("snapshot3s_rounds", 0),
            "last_snapshot3s_age_s": _age(
                now, counters.get("last_snapshot3s_ts")
            ),
            "degraded": bool(store.get("degraded")),
            **store,
        }

    def _bump(self, key: str, amount: int = 1) -> None:
        with self._lock:
            self._counters[key] = int(self._counters.get(key) or 0) + amount


def _age(now: float, ts) -> float:
    try:
        value = float(ts or 0.0)
    except (TypeError, ValueError):
        return -1.0
    return round(max(0.0, now - value), 1) if value > 0 else -1.0


def _num(value) -> float:
    return float(safe_float(value, 0.0) or 0.0)


def _level1(values) -> float:
    if isinstance(values, (list, tuple)) and values:
        return float(safe_float(values[0], 0.0) or 0.0)
    return float(safe_float(values, 0.0) or 0.0)
