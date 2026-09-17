"""热集存储唯一事实源（键 + 连接；T-P6-06 收口）。

**为什么是本地 Redis**（2026-09-17 盘中事故裁决）：
热集（``qm:hot_set:symbols``）是**每个部署私有的工作集**，不是行情数据。此前它被放在
远端公共行情服上——远端服是多租户共享实例，任何两个接入的部署都会用同一个全局键
互相覆写（实测：本机便携实例与生产栈每 60s 交替覆写 502⇄529、单次翻新约 300 只），
订阅侧随之每 20~40s 重建会话（resubscribes 18→34），是盘中"零数据帧"的可归因自伤来源。
本地化后：写者/读者（builder / 订阅 worker / 备源席 / regime / 实时推理 / 验收器）
全部在同一部署的 Redis 上，彻底消除撞名，并顺带消除对远端服的读超时
（``hot_set read: Timeout connecting to server``）。

- 键：``QM_HOT_SET_KEY`` env 可隔离（默认 ``qm:hot_set:symbols``；与订阅 worker 同键）；
- 连接：部署本地 Redis（``REDIS_HOST/REDIS_PORT/REDIS_PASSWORD``，db 取
  ``QM_HOT_SET_DB`` > ``REDIS_DB_GENERAL`` > 0——与 ``qm:qmt:quote:backup:*``、
  ``qm:market:tdx_aidata:config`` 等协调键同库）。
"""

from __future__ import annotations

import os
from typing import Any

DEFAULT_HOT_SET_KEY = "qm:hot_set:symbols"


def hot_set_key() -> str:
    """热集符号集键（唯一事实源；测试/多实例可用 env 隔离）。"""
    env = str(os.getenv("QM_HOT_SET_KEY") or "").strip()
    return env or DEFAULT_HOT_SET_KEY


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name) or "").strip() or default)
    except (TypeError, ValueError):
        return default


def make_hot_set_client(
    *,
    decode_responses: bool = True,
    socket_timeout: float = 5.0,
    socket_connect_timeout: float = 3.0,
) -> Any:
    """构造**部署本地**热集 Redis 客户端（唯一构造点）。

    本地 Redis 由部署自带（compose 的 ``redis`` 服务 / 便携包内嵌实例），
    容器内同网可达；连接失败按调用方各自的错误处理路径失败（不做静默回落——
    热集是订阅链的输入，读不到必须如实暴露）。
    """
    import redis as _redis

    return _redis.Redis(
        host=os.getenv("REDIS_HOST") or "redis",
        port=_env_int("REDIS_PORT", 6379),
        password=os.getenv("REDIS_PASSWORD") or None,
        db=_env_int("QM_HOT_SET_DB", _env_int("REDIS_DB_GENERAL", 0)),
        decode_responses=decode_responses,
        socket_connect_timeout=socket_connect_timeout,
        socket_timeout=socket_timeout,
    )


def read_hot_set_symbols() -> list[str]:
    """读热集符号（排序；读失败向上抛，由调用方决定降级口径）。"""
    client = make_hot_set_client()
    try:
        return sorted(client.smembers(hot_set_key()) or [])
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass
