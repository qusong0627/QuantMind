"""行情缓存优先读取（2026-10-30 全市场缓存 P3）。

桥的 tap/sweep 已把全市场快照写进 Redis（``market:snapshot:{symbol}``，db5）。
消费方（止损执行器、减仓执行器、脚本）**优先读缓存**，只有「缺失或陈旧」的标的
才回落到 QMT 管道——这样高频轮询不再各自占管道，订单自然更快。

判据：快照 ``ts`` 距 now 超过 ``max_age_s``（默认 5s）即视为陈旧。tap 全市场
0.5s 一轮，正常情况全部命中；tap 停摆时自动回落管道并保持原行为。
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Iterable, Mapping
from typing import Any

logger = logging.getLogger(__name__)

SNAPSHOT_KEY = "market:snapshot:%s"
DEFAULT_MAX_AGE_S = 5.0


def cache_enabled() -> bool:
    """行情缓存开关（``QM_QUOTE_CACHE``，默认开）。单测里由 conftest 关掉。"""
    return str(os.getenv("QM_QUOTE_CACHE", "on")).strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _to_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def to_tick(row: Mapping[str, Any]) -> dict[str, Any]:
    """快照 hash → ``get_full_tick`` 形状（消费方只用到价格/昨收/五档）。"""
    row = row or {}
    last = _to_float(row.get("last"))
    if last is None:
        last = _to_float(row.get("price"))
    return {
        "lastPrice": last,
        "lastClose": _to_float(row.get("pre_close")),
        "open": _to_float(row.get("open")),
        "high": _to_float(row.get("high")),
        "low": _to_float(row.get("low")),
        "volume": _to_float(row.get("volume")),
        "amount": _to_float(row.get("amount")),
        "bidPrice": [_to_float(row.get(f"bid{i}_p")) for i in range(1, 6)],
        "askPrice": [_to_float(row.get(f"ask{i}_p")) for i in range(1, 6)],
        "_ts": _to_float(row.get("ts")) or 0.0,
        "_source": str(row.get("source") or ""),
    }


def is_fresh(tick: Mapping[str, Any], now: float, max_age_s: float) -> bool:
    """价格可用且时间戳在窗口内才算命中缓存。"""
    price = _to_float((tick or {}).get("lastPrice"))
    ts = _to_float((tick or {}).get("_ts")) or 0.0
    if not price or price <= 0 or ts <= 0:
        return False
    return (now - ts) <= float(max_age_s or 0)


def _to_suffix(code: str) -> str:
    from backend.shared.stock_utils import StockCodeUtil

    raw = str(code or "").strip().upper()
    if not raw:
        return ""
    return raw if "." in raw else StockCodeUtil.to_suffix(raw).upper()


async def _bridge_redis():
    """桥 Redis（db5）异步客户端：与 QmtExecClient 页面配置同源。"""
    import redis.asyncio as aioredis

    from backend.services.live_trading.services.qmt_exec_client import QmtExecClient

    client = QmtExecClient.from_env()
    await client.refresh_settings()
    url = QmtExecClient._build_redis_url(client.effective_config())
    if not url:
        raise RuntimeError("桥 Redis 未配置")
    return aioredis.from_url(url, decode_responses=True)


async def read_snapshots(symbols: Iterable[str]) -> dict[str, dict[str, str]]:
    """批量读快照（pipeline HGETALL）。失败返回空字典（调用方回落管道）。"""
    codes = [_to_suffix(code) for code in symbols]
    codes = [code for code in codes if code]
    if not codes:
        return {}
    try:
        redis = await _bridge_redis()
        pipe = redis.pipeline(transaction=False)
        for code in codes:
            pipe.hgetall(SNAPSHOT_KEY % code)
        rows = await pipe.execute()
        await redis.aclose()
    except Exception as exc:  # noqa: BLE001 - 缓存不可用只降级
        logger.warning("[QuoteCache] 读快照失败，回落管道: %s", exc)
        return {}
    return {
        code: dict(row or {})
        for code, row in zip(codes, rows, strict=False)
        if isinstance(row, dict) and row
    }


async def get_ticks(
    client: Any,
    symbols: Iterable[str],
    *,
    max_age_s: float = DEFAULT_MAX_AGE_S,
    now: float | None = None,
) -> dict[str, Any]:
    """缓存优先取盘口：命中返回缓存 tick，缺失/陈旧的标的才走 ``get_full_tick``。"""
    originals = [str(code).strip() for code in symbols if str(code or "").strip()]
    codes = [_to_suffix(code) for code in originals]
    pairs = [(orig, code) for orig, code in zip(originals, codes, strict=False) if code]
    if not pairs:
        return {}
    suffix_codes = [code for _, code in pairs]
    rows = await read_snapshots(suffix_codes) if cache_enabled() else {}
    now_ts = time.time() if now is None else float(now)
    by_suffix: dict[str, Any] = {}
    missing: list[str] = []
    for code in suffix_codes:
        tick = to_tick(rows.get(code) or {})
        if is_fresh(tick, now_ts, max_age_s):
            by_suffix[code] = tick
        else:
            missing.append(code)
    if missing:
        fallback = await client.get_full_tick(missing)
        for code, item in (fallback or {}).items():
            by_suffix[str(code).upper()] = item
    # 返回键保持**调用方原始形态**（规则可能是 SH600036，缓存键是 600036.SH）
    out: dict[str, Any] = {}
    for orig, code in pairs:
        if code in by_suffix:
            out[orig] = by_suffix[code]
    return out
