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

# series 成员 volume 为**手**（写侧契约：tdx_hot_set_feed/qmt_quote_backup 均为手口径），
# 下游日频核按**股**做整手取整（capacity // lot_size * lot_size，lot=100 股）。
SERIES_VOLUME_UNIT_SHARES = 100


def recent_traded_shares(window_rows: list[tuple[Any, Any]]) -> float | None:
    """窗口内**同源**累计成交量差（股）；不可验证一律 None，绝不返回 0 冒充「无流动性」。

    2026-10-09 事故（决策轮买单全拒 ``insufficient_realtime_liquidity``，日频核容量为 0）
    暴露的三个口径缺陷：
    * **单位**：series volume 为手，日频核按股取整——手当股再缩 100 倍，常态归零；
    * **跨源**：热集席（tdx_bridge）与 QMT 备源（qmt_big）交替写同一键，两源累计量
      刷新不同频、可先后倒挂（实测 qmt 36546 → tdx 36436），混合相减出负数被钳 0；
    * **粒度**：TDX 快照量按批刷新（实测整分钟平值），同源差 0 不代表「无成交」。
    ⇒ 只取**最新成员同源**的成员求差；同源 <2 个、或差 ≤0 一律 None（走
    ``SIM_LIQUIDITY_UNVERIFIED_MAX_NOTIONAL`` 兜底，>10 万拒、小额放行）；
    正常差值 ×100 归一为股，与下游整手取整同一坐标系。
    """
    pairs: list[tuple[str, float]] = []
    for raw_member, _raw_score in window_rows:
        try:
            payload = json.loads(raw_member)
            volume = float(payload.get("volume"))
            if volume >= 0:
                pairs.append((str(payload.get("source") or ""), volume))
        except (TypeError, ValueError, KeyError):
            continue
    if len(pairs) < 2:
        return None
    last_source = pairs[-1][0]
    same_source = [volume for source, volume in pairs if source == last_source]
    if len(same_source) < 2:
        return None
    delta = same_source[-1] - same_source[0]
    if delta <= 0:
        return None
    return delta * SERIES_VOLUME_UNIT_SHARES


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
    """Batch-load fresh ticks and recent incremental volume in one pipeline.

    **价格判定与单只取价（:func:`fetch_series_tick`）同答案**：最新成员直接
    ``zrevrange`` 取，可用性一律由 :func:`parse_series_member` 按新鲜度策略分级
    （stale≤300s 可用须标注）。量能窗（``volume_window_sec``）只用于
    ``recent_volume`` 增量，**不参与价格取舍**。

    2026-10-09 事故：本函数原用一条 ``zrangebyscore(now-60s, now)`` 同时承担取价
    与量差——桥席按 ~100s/只轮转写 series，最新 tick 常态落在 60s 窗外，批量取价
    整批漏掉「stale 但策略判定可用」的合法 tick（而单只取价正常返回），决策轮
    买单因此全拒「无法获取实时行情，模拟单拒绝成交」。硬窗不能顶着**快照/帧
    节拍**的下限设——节拍一变，窗就变成静默的取价门禁。

    ``recent_volume`` 口径见 :func:`recent_traded_shares`：**股**（手×100 归一）、
    仅同源成员求差、不可验证为 None（None 在撮合核是「量能未知，10 万内放行」，
    0 是「容量 0，硬拒」——语义混淆即毒源）。
    """
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
            pipe.zrevrange(key, 0, 0, withscores=True)
            pipe.zrangebyscore(
                key,
                now_ts - max(1, volume_window_sec),
                now_ts,
                withscores=True,
            )
        flat = await pipe.execute()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[RedisSeriesQuote] 批量读取失败: %s", exc)
        return {}

    result: dict[str, dict[str, Any]] = {}
    for idx, (symbol, _) in enumerate(keyed):
        latest_rows = flat[idx * 2]
        window_rows = flat[idx * 2 + 1]
        if not latest_rows:
            continue
        member, score = latest_rows[0]
        tick = parse_series_member(member, float(score), now_ts, policy)
        if tick is None:
            continue
        tick["recent_volume"] = recent_traded_shares(window_rows)
        result[symbol] = tick
    return result
