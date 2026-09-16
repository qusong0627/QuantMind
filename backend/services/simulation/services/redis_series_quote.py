"""Redis 实时行情直读（market:series ZSET），供模拟撮合使用。

外部行情推送方把全市场快照写入远端 Redis（与 stream 的 RemoteRedisDataSource
同一实例/DB），键格式遵循 AGENTS.md：序列键用标准前缀式
`market:series:SH600036`，成员为 JSON（含 price/open/high/low/volume/amount/
timestamp/source），score 即时间戳。

撮合取价时优先用本模块（Level 0）：盘中 tick 新鲜时直接按 Redis 现价成交；
陈旧或缺失时返回 None，由调用方走既有兜底链路。
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from backend.shared.freshness import UNAVAILABLE, FreshnessPolicy, quote_policy

logger = logging.getLogger(__name__)

SERIES_KEY_PREFIX = "market:series:"


def _env() -> tuple[str, int, str | None, int] | None:
    """远端行情连接参数；配置关闭/主机为空时返回 None（本级别取价禁用）。

    T-P0-03：默认值与读取逻辑收敛到 backend/shared/remote_quote_config.py
    （与实盘预检共用一份，消除两处重复的免费行情服默认值）。
    """
    from backend.shared.remote_quote_config import resolve_remote_quote_redis

    return resolve_remote_quote_redis()


def series_key_for(symbol: str) -> str | None:
    """Convert a validated CN/HK/US symbol to its exact series key."""
    import re as _re

    from backend.shared.stock_utils import StockCodeUtil

    raw = str(symbol or "").strip().upper()
    normalized = StockCodeUtil.to_prefix(raw)
    if _re.fullmatch(r"^(SH|SZ|BJ)\d{6}$", normalized):
        pass
    elif _re.fullmatch(r"(?:\d{4,5}\.HK|HK\d{5})", raw):
        normalized = raw
    elif _re.fullmatch(r"[A-Z][A-Z0-9.-]{0,14}", raw):
        normalized = raw
    else:
        return None
    return f"{SERIES_KEY_PREFIX}{normalized}"


def parse_series_member(
    member: str | bytes,
    score: float,
    now_ts: float,
    policy: FreshnessPolicy | None = None,
) -> dict[str, Any] | None:
    """解析单个 ZSET 成员；价格无效或不可用（unavailable）返回 None（纯函数，可单测）。

    新鲜度口径唯一走 ``backend.shared.freshness``（T-P6-05）；stale 仍可用并在
    返回值 ``freshness`` 字段如实标注。
    """
    try:
        data = json.loads(member)
    except (TypeError, ValueError):
        return None
    try:
        price = float(data.get("price") or 0)
    except (TypeError, ValueError):
        return None
    if price <= 0:
        return None
    try:
        ts = int(float(score))
    except (TypeError, ValueError):
        return None
    age = now_ts - ts
    level = (policy or quote_policy()).classify(age)
    if level == UNAVAILABLE:
        return None
    out: dict[str, Any] = {
        "price": price,
        "timestamp": ts,
        "age_s": age,
        "freshness": level,
        "source": data.get("source") or "redis_series",
    }
    for key in ("open", "high", "low", "volume", "amount"):
        try:
            val = data.get(key)
            out[key] = float(val) if val is not None else None
        except (TypeError, ValueError):
            out[key] = None
    return out


_client = None
_client_disabled_warned = False


def _get_client():
    """远端行情 Redis 客户端（单例复用，与 stream 侧同实例）。

    配置禁用（REMOTE_QUOTE_DISABLED 或主机为空）时返回 None，首次访问
    WARNING 一次——调用方一律走本地日线兜底，不静默连默认地址。
    """
    global _client, _client_disabled_warned
    if _client is None:
        env = _env()
        if env is None:
            if not _client_disabled_warned:
                logger.warning(
                    "[RedisSeriesQuote] 远端行情 Redis 未配置/已禁用，"
                    "L0 实时 tick 取价停用（走本地日线兜底）"
                )
                _client_disabled_warned = True
            return None
        import redis.asyncio as aioredis

        host, port, password, db = env
        _client = aioredis.Redis(
            host=host,
            port=port,
            password=password,
            db=db,
            decode_responses=True,
            socket_connect_timeout=3,
            socket_timeout=5,
        )
    return _client


async def fetch_series_tick(
    symbol: str, *, policy: FreshnessPolicy | None = None
) -> dict[str, Any] | None:
    """取 symbol 最新 tick；可用（fresh/stale）才返回，unavailable 返回 None。

    新鲜度口径唯一走 ``backend.shared.freshness.quote_policy()``（T-P6-05；
    旧 simulation 阈值环境变量作为兼容别名仅在该共享模块内读取）。
    """
    key = series_key_for(symbol)
    if not key:
        return None
    client = _get_client()
    if client is None:
        return None
    try:
        rows = await client.zrevrange(key, 0, 0, withscores=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[RedisSeriesQuote] 读取 %s 失败: %s", key, exc)
        return None
    if not rows:
        return None
    member, score = rows[0]
    tick = parse_series_member(member, float(score), time.time(), policy)
    if tick is None:
        logger.debug("[RedisSeriesQuote] %s 无可用 tick", key)
    return tick


async def fetch_series_ticks(
    symbols: list[str],
    *,
    policy: FreshnessPolicy | None = None,
    volume_window_sec: int = 60,
) -> dict[str, dict[str, Any]]:
    """Batch-load fresh ticks and recent incremental volume in one pipeline."""
    policy = policy or quote_policy()
    try:
        volume_window_sec = int(
            os.getenv("SIM_LIQUIDITY_WINDOW_SEC") or volume_window_sec
        )
    except (TypeError, ValueError):
        pass
    keyed = [(symbol, series_key_for(symbol)) for symbol in dict.fromkeys(symbols)]
    keyed = [(symbol, key) for symbol, key in keyed if key]
    client = _get_client()
    if client is None or not keyed:
        return {}
    now_ts = time.time()
    try:
        pipe = client.pipeline(transaction=False)
        for _, key in keyed:
            pipe.zrangebyscore(
                key,
                now_ts - max(1, volume_window_sec),
                now_ts,
                withscores=True,
            )
        rows_by_symbol = await pipe.execute()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[RedisSeriesQuote] 批量读取失败: %s", exc)
        return {}

    result: dict[str, dict[str, Any]] = {}
    for (symbol, _), rows in zip(keyed, rows_by_symbol, strict=True):
        if not rows:
            continue
        member, score = rows[-1]
        tick = parse_series_member(member, float(score), now_ts, policy)
        if tick is None:
            continue
        volumes: list[float] = []
        for raw_member, _raw_score in rows:
            try:
                payload = json.loads(raw_member)
                volume = float(payload.get("volume"))
                if volume >= 0:
                    volumes.append(volume)
            except (TypeError, ValueError, KeyError):
                continue
        tick["recent_volume"] = (
            max(0.0, volumes[-1] - volumes[0]) if len(volumes) >= 2 else None
        )
        result[symbol] = tick
    return result
