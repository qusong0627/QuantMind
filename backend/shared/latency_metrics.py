"""端到端时延打点（T-P6-05）：源时间戳 → 可消费点，落 ``intel:latency`` 滚动统计。

口径：``latency_ms = (可消费时刻 - 源时间戳) × 1000``。源时间戳=行情水印（如 TDX 帧
refresh_time 换算出的 epoch）；**负值=消费方时钟落后/未来戳——如实计数 future_count，
不静默丢弃**（未来分数会长期霸榜 ZSET，属可观测异常）。

Redis 结构（主 Redis db0；前缀 ``intel:latency:``）：
- HASH  ``intel:latency:{stage}``          最新窗口统计：window_s/samples/min|p50|p95|max|avg_ms/
  total_count/future_count/updated_at
- ZSET  ``intel:latency:{stage}:series``   score=flush 时刻，member=json 摘要；按 rank 保 24h 量级

唯一写入面 = ``LatencyRecorder``（线程安全；Redis 写失败只计数不抛——不阻断实时链）；
读取 = ``read_latency`` / ``read_all``（CLI 报表/前端面板/状态快照共用）。
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from collections import deque
from typing import Any

from backend.shared.signal_thresholds import (
    _percentile_nearest_rank,  # 复用既有最近秩实现（与 shadow_compare 同口径），禁第三份
)

logger = logging.getLogger(__name__)

LATENCY_PREFIX = "intel:latency"
DEFAULT_WINDOW_SIZE = 1000
DEFAULT_FLUSH_SECONDS = 30.0
DEFAULT_SERIES_KEEP = 2880  # 30s × 2880 ≈ 24h
SERIES_TTL_S = 90000  # 25h，兜底防 rank 截断失效
DEFAULT_FRESH_GUARD_MS = 300_000.0  # 新鲜档上限：>5min 的帧是陈旧重放（非传输时延）

_client: Any = None


def _default_client():
    """主 Redis 惰性单例（env 与 diagnose/health.py 同口径）；不可用抛异常由调用方兜。"""
    global _client
    if _client is None:
        import redis as redis_lib

        _client = redis_lib.Redis(
            host=os.getenv("REDIS_HOST") or "redis",
            port=int(os.getenv("REDIS_PORT", "6379")),
            db=int(os.getenv("REDIS_DB", "0")),
            password=os.getenv("REDIS_PASSWORD") or None,
            decode_responses=True,
            socket_connect_timeout=3,
            socket_timeout=5,
        )
    return _client


def _stats(ordered: list[float]) -> dict[str, Any]:
    """升序样本 → 统计块（最近秩分位口径）。"""
    n = len(ordered)
    return {
        "samples": n,
        "min_ms": round(ordered[0], 2),
        "p50_ms": round(_percentile_nearest_rank(ordered, 0.50), 2),
        "p95_ms": round(_percentile_nearest_rank(ordered, 0.95), 2),
        "max_ms": round(ordered[-1], 2),
        "avg_ms": round(sum(ordered) / n, 2),
    }


class LatencyRecorder:
    """按 stage 汇总时延样本：窗口分位 + 阈值 flush 落 Redis。线程安全、best-effort。"""

    def __init__(
        self,
        stage: str,
        *,
        redis_client: Any | None = None,
        window_size: int = DEFAULT_WINDOW_SIZE,
        flush_seconds: float = DEFAULT_FLUSH_SECONDS,
        flush_samples: int | None = None,
        series_keep: int = DEFAULT_SERIES_KEEP,
        fresh_guard_ms: float | None = DEFAULT_FRESH_GUARD_MS,
    ) -> None:
        self.stage = str(stage)
        window_size = max(10, int(window_size))
        self._samples: deque[float] = deque(maxlen=window_size)
        self._fresh_samples: deque[float] = deque(maxlen=window_size)
        self.fresh_guard_ms = (
            float(fresh_guard_ms) if fresh_guard_ms is not None else None
        )
        self._lock = threading.Lock()
        self._redis_client = redis_client
        self.flush_seconds = float(flush_seconds)
        self.flush_samples = int(flush_samples) if flush_samples else max(1, window_size // 2)
        self.series_keep = int(series_keep)
        self._last_flush = time.monotonic()
        self._pending = 0
        self.counters: dict[str, Any] = {
            "observed": 0,
            "rejected": 0,
            "future": 0,
            "stale": 0,
            "flushes": 0,
            "flush_errors": 0,
            "flush_skipped": 0,
            "last_error": None,
        }

    # ── 写入侧 ──────────────────────────────────────────────────────

    def observe(self, latency_ms: float) -> None:
        """记录一个样本（非数/NaN/Inf 计入 rejected；负值计入 future 照常入窗）。

        双通道语义：全量 → 本 stage（端到端真相）；``0 ≤ lat ≤ fresh_guard_ms`` 的样本
        额外入 **新鲜档**（``{stage}_fresh``）——夜盘/停牌标的的陈旧重放帧（数小时级）
        不是传输时延，不应稀释"行情到达 <2s"验收口径（2026-09-17 实测：夜盘帧 p50≈9h）。
        """
        try:
            value = float(latency_ms)
        except (TypeError, ValueError):
            with self._lock:
                self.counters["rejected"] += 1
            return
        if math.isnan(value) or math.isinf(value):
            with self._lock:
                self.counters["rejected"] += 1
            return
        with self._lock:
            self._samples.append(value)
            self._pending += 1
            self.counters["observed"] += 1
            if value < 0:
                self.counters["future"] += 1  # 未来戳：入全量档、不算新鲜也不计陈旧
            elif self.fresh_guard_ms is not None:
                if value <= self.fresh_guard_ms:
                    self._fresh_samples.append(value)
                else:
                    self.counters["stale"] += 1

    def _client(self):
        if self._redis_client is not None:
            return self._redis_client
        self._redis_client = _default_client()
        return self._redis_client

    def maybe_flush(self, *, force: bool = False) -> bool:
        """达到样本数/时间阈值（或 force）时落一次 Redis；未到期返回 False。"""
        with self._lock:
            due = force or (
                self._pending > 0
                and (
                    self._pending >= self.flush_samples
                    or time.monotonic() - self._last_flush >= self.flush_seconds
                )
            )
            if not due or not self._samples:
                return False
        return self.flush()

    def flush(self) -> bool:
        with self._lock:
            samples = sorted(self._samples)
            fresh = sorted(self._fresh_samples)
            observed = int(self.counters["observed"])
            future = int(self.counters["future"])
            stale = int(self.counters["stale"])
            self._pending = 0
            self._last_flush = time.monotonic()
        if not samples:
            return False
        now_ts = time.time()

        def _payload(block: list[float], *, count: int | None = None) -> dict[str, Any]:
            out: dict[str, Any] = {
                **_stats(block),
                "flush_interval_s": round(self.flush_seconds, 1),
                "window_size": self._samples.maxlen,
                "updated_at": round(now_ts, 3),
            }
            if count is not None:
                out["total_count"] = count
            return out

        payload = _payload(samples, count=observed)
        payload["future_count"] = future
        payload["stale_count"] = stale
        fresh_payload = (
            _payload(fresh, count=len(fresh)) if fresh and self.fresh_guard_ms is not None else None
        )
        if fresh_payload is not None:
            fresh_payload["fresh_guard_ms"] = self.fresh_guard_ms

        def _series(body: dict[str, Any]) -> str:
            return json.dumps(
                {k: body[k] for k in ("samples", "p50_ms", "p95_ms", "max_ms", "avg_ms")},
                ensure_ascii=False,
            )

        try:
            client = self._client()
            pipe = client.pipeline(transaction=False)

            def _write(stage: str, body: dict[str, Any]) -> None:
                hash_key = f"{LATENCY_PREFIX}:{stage}"
                series_key = f"{LATENCY_PREFIX}:{stage}:series"
                pipe.hset(hash_key, mapping={k: str(v) for k, v in body.items()})
                pipe.zadd(series_key, {_series(body): now_ts})
                pipe.zremrangebyrank(series_key, 0, -(self.series_keep + 1))
                pipe.expire(series_key, SERIES_TTL_S)

            _write(self.stage, payload)
            if fresh_payload is not None:
                _write(f"{self.stage}_fresh", fresh_payload)
            pipe.execute()
            with self._lock:
                self.counters["flushes"] += 1
            return True
        except Exception as exc:  # noqa: BLE001 - 打点失败不阻断实时链
            with self._lock:
                self.counters["flush_errors"] += 1
                self.counters["last_error"] = f"flush: {exc}"
            logger.warning("[Latency] %s flush 失败: %s", self.stage, exc)
            return False

    # ── 读取侧 ──────────────────────────────────────────────────────

    def snapshot(self) -> dict[str, Any]:
        """进程内视图（worker 状态快照用）：窗口分位 + 计数，不读 Redis。"""
        with self._lock:
            samples = sorted(self._samples)
            fresh = sorted(self._fresh_samples)
            pending = self._pending
        out: dict[str, Any] = {
            "stage": self.stage,
            "pending": pending,
            **dict(self.counters),
        }
        if samples:
            out.update(_stats(samples))
        if fresh and self.fresh_guard_ms is not None:
            out["fresh"] = _stats(fresh)
        return out


def _decode_stats(raw: dict) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in (raw or {}).items():
        name = key.decode() if isinstance(key, bytes) else str(key)
        text = value.decode() if isinstance(value, bytes) else str(value)
        try:
            out[name] = int(text) if text.isdigit() or (
                text.startswith("-") and text[1:].isdigit()
            ) else float(text)
        except (TypeError, ValueError):
            out[name] = text
    return out


def read_latency(stage: str, *, redis_client: Any | None = None) -> dict[str, Any] | None:
    """读某 stage 的最新窗口统计；无记录返回 None。"""
    client = redis_client or _default_client()
    raw = client.hgetall(f"{LATENCY_PREFIX}:{stage}")
    if not raw:
        return None
    return _decode_stats(raw)


def read_all(*, redis_client: Any | None = None) -> dict[str, dict[str, Any]]:
    """枚举全部 stage 的统计块（跳过 :series 趋势键）。"""
    client = redis_client or _default_client()
    out: dict[str, dict[str, Any]] = {}
    for raw_key in client.scan_iter(match=f"{LATENCY_PREFIX}:*", count=200):
        key = raw_key.decode() if isinstance(raw_key, bytes) else str(raw_key)
        if key.endswith(":series"):
            continue
        stage = key[len(LATENCY_PREFIX) + 1 :]
        stats = read_latency(stage, redis_client=client)
        if stats:
            out[stage] = stats
    return out


def read_series(
    stage: str, *, limit: int = 120, redis_client: Any | None = None
) -> list[dict[str, Any]]:
    """读趋势序列（最近 limit 个 flush 摘要，按时间升序）。"""
    client = redis_client or _default_client()
    rows = client.zrange(f"{LATENCY_PREFIX}:{stage}:series", -int(limit), -1, withscores=True)
    out: list[dict[str, Any]] = []
    for member, score in rows:
        text = member.decode() if isinstance(member, bytes) else str(member)
        try:
            payload = json.loads(text)
        except (TypeError, ValueError):
            continue
        payload["at"] = float(score)
        out.append(payload)
    return out
