"""Redis 分布式锁原语（token + SET NX EX + Lua CAS 释放）——全仓唯一实现。

口径来源：`inference_lock`（T-P1-02）里验证过的模式——释放必须校验属主
（值 == token 才 DEL），防止「锁过期后持锁方误删他人锁」；`inference_lock`
与 `training_singleflight`（P0-3）都委托本模块，不再各写一份 Lua。

使用约定：
- token 由调用方生成并保存，释放时原样回传；
- ``acquire`` 返回 bool；``release`` 返回 bool（键不存在/非本 token → False）；
- 全部为同步 redis-py 接口（与仓内既有用法一致，调用点自行决定线程/循环）。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# 释放必须校验属主：仅当键值 == 本次 token 才删
RELEASE_LUA = """
local current = redis.call("GET", KEYS[1])
if current == ARGV[1] then
    return redis.call("DEL", KEYS[1])
end
return 0
"""


def acquire(redis_client, key: str, token: str, ttl_seconds: int) -> bool:
    """SET NX EX 获取锁；成功 True，被占用 False。异常向上抛。"""
    ok = redis_client.set(key, token, ex=ttl_seconds, nx=True)
    return bool(ok)


def release(redis_client, key: str, token: str) -> bool:
    """CAS 释放（属主校验）；键不存在或非本 token 返回 False。"""
    result = redis_client.eval(RELEASE_LUA, 1, key, token)
    return bool(result)


def holder(redis_client, key: str) -> str | None:
    """当前持锁 token（无锁/键不存在 → None）。

    注意：仓内客户端 ``decode_responses=False``，``get`` 返回 **bytes**——
    必须显式解码。``str(bytes)`` 会得到 ``"b'...'"`` 字面量，拿它当 token
    回传会让释放 CAS 永远不等（自愈释放静默失效）。
    """
    value = redis_client.get(key)
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)
