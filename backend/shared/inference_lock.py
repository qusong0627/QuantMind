"""推理单实例锁 + 信号就绪标记（T-P1-02）。

背景（2026-09-15 诊断）：同日多次 run 并存（9/14 三个 run、相隔 25 秒的双跑、
迟到 3 天的回填 run），下游按"最新 run"取值会读到残缺结果；且既有锁释放是裸
``delete``（无属主校验——持锁方过期后可能误删他人锁）。

本模块是唯一实现：
- ``acquire`` / ``release``：SET NX EX 获取 + **Lua CAS 释放**（值=token，属主校验）；
- ``inference_lock_key``：全市场持久化推理的单实例锁键（tenant/user/model/date）；
- 就绪标记 ``qm:signal:ready:{market}:{trade_date}``：**全量落库且校验通过才置位**
  （部分推/池裁剪/残 run 不置位），值=run_id；供下游等待（替代"猜最新 run"）。
  既有 ``qm:inference:completed:{date}`` 保持写入（兼容既有读取方）。
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid

logger = logging.getLogger(__name__)

LOCK_KEY_PREFIX = "qm:lock:inference:daily"
READY_KEY_PREFIX = "qm:signal:ready"

DEFAULT_LOCK_TTL_SECONDS = 3600  # 兜底 TTL；旧实现为 1800，超过 30 分钟的
#                                   运行会让锁中途过期（9/14 双跑的疑因之一）
DEFAULT_READY_TTL_SECONDS = 3 * 24 * 3600
DEFAULT_MIN_SYMBOLS = 1000  # 就绪门槛：低于该标的数据视为残 run

# 释放必须校验属主：仅当键值 == 本次 token 才删（防止锁过期后误删他人锁）
_RELEASE_LUA = """
local current = redis.call("GET", KEYS[1])
if current == ARGV[1] then
    return redis.call("DEL", KEYS[1])
end
return 0
"""


def lock_ttl_seconds() -> int:
    try:
        return max(60, int(os.getenv("INFERENCE_LOCK_TTL_SECONDS", DEFAULT_LOCK_TTL_SECONDS)))
    except (TypeError, ValueError):
        return DEFAULT_LOCK_TTL_SECONDS


def ready_min_symbols() -> int:
    try:
        return max(1, int(os.getenv("SIGNAL_READY_MIN_SYMBOLS", DEFAULT_MIN_SYMBOLS)))
    except (TypeError, ValueError):
        return DEFAULT_MIN_SYMBOLS


def inference_lock_key(tenant_id: str, user_id: str, model_key: str, trade_date: str) -> str:
    """全市场持久化推理的单实例锁键（同日/同租户/同用户/同模型 唯一）。"""
    return (
        f"{LOCK_KEY_PREFIX}:{str(tenant_id or 'default')}:{str(user_id or '')}"
        f":{str(model_key or 'default')}:{str(trade_date or '')}"
    )


def ready_key(market: str, trade_date: str) -> str:
    from backend.shared.signal_contract import normalize_market

    return f"{READY_KEY_PREFIX}:{normalize_market(market)}:{str(trade_date or '')}"


def acquire(redis_client, key: str, ttl_seconds: int | None = None) -> str | None:
    """获取锁；成功返回 token（释放时回传），被占用返回 None。异常向上抛。"""
    token = uuid.uuid4().hex
    ok = redis_client.set(key, token, ex=ttl_seconds or lock_ttl_seconds(), nx=True)
    return token if ok else None


def release(redis_client, key: str, token: str) -> bool:
    """CAS 释放（属主校验）；键不存在或非本 token 返回 False。"""
    result = redis_client.eval(_RELEASE_LUA, 1, key, token)
    return bool(result)


def should_mark_ready(*, partial: bool, symbol_count: int, min_symbols: int) -> bool:
    """就绪判定（纯函数）：非部分推 且 标的数达门槛 才置位。"""
    if partial:
        return False
    return int(symbol_count) >= int(min_symbols)


def mark_signal_ready_if_full(
    redis_client,
    *,
    market: str,
    trade_date: str,
    run_id: str,
    partial: bool,
    symbol_count: int,
    min_symbols: int | None = None,
) -> bool:
    """全量校验通过则置就绪标记；任何异常只告警不抛出（不影响主流程）。"""
    if redis_client is None:
        return False
    threshold = min_symbols if min_symbols is not None else ready_min_symbols()
    if not should_mark_ready(
        partial=partial, symbol_count=symbol_count, min_symbols=threshold
    ):
        logger.info(
            "[SignalReady] 不置就绪标记（partial=%s, symbols=%d < %d）trade_date=%s",
            partial,
            symbol_count,
            threshold,
            trade_date,
        )
        return False
    try:
        payload = json.dumps(
            {
                "run_id": str(run_id),
                "symbols": int(symbol_count),
                "market": market,
                "ts": int(time.time()),
            },
            ensure_ascii=False,
        )
        redis_client.set(
            ready_key(market, trade_date), payload, ex=DEFAULT_READY_TTL_SECONDS
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("[SignalReady] 写就绪标记失败（不影响主流程）: %s", exc)
        return False
