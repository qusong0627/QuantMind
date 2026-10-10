"""远端行情 Redis 配置唯一事实源（REMOTE_QUOTE_REDIS_*）。

用途：行情推送方把全市场快照写入远端 Redis（`market:series` ZSET），
模拟撮合 L0 取价（`simulation/services/redis_series_quote.py`）与实盘预检
直连（`live_trading/routers/real_trading_utils.py`）共用同一实例。

背景（T-P0-03，2026-09-15）：该配置此前在上述两个文件各写一份默认值
（公共免费行情服地址 + 口令），口径与改动点分散，存在漂移风险。现收敛到
本模块，两处消费方统一 import。

约定：
- 默认值 = 官方**公共免费行情服**（公开地址/口令，供 OSS 开箱可用，
  非部署私密凭据；私有部署请在 .env / compose 显式配置覆盖）；
- ``REMOTE_QUOTE_DISABLED=true`` 可整体关闭远端行情（解析返回 None，
  调用方走本地日线兜底）；
- 读取优先级：进程环境变量 > 项目根 .env（非容器运行时的兜底）。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# 官方公共免费行情服（公开信息；覆盖方式见模块 docstring）
# ⚠️ 口令为**公开信息**（OSS 开箱默认），不是部署私密凭据；私有部署必须在
# .env 配 REMOTE_QUOTE_REDIS_HOST/PASSWORD 覆盖（2026-10-10 审计 M3：默认值
# 合法但必须可见——仍走公共服时 resolve 打一次 warning）。
FREE_FEED_HOST = "www.quantmindai.cn"
FREE_FEED_PORT = 6379
FREE_FEED_PASSWORD = "quantmind2026"
FREE_FEED_DB = 3

_DISABLED_VALUES = {"1", "true", "yes", "on"}

_root_env_cache: dict[str, str] | None = None
_warned_builtin_default = False


def _load_root_env_map() -> dict[str, str]:
    """兜底读取项目根 .env（服务进程未注入变量时）。读失败返回空表。"""
    global _root_env_cache
    if _root_env_cache is None:
        env_map: dict[str, str] = {}
        try:
            # backend/shared/remote_quote_config.py -> parents[2] = 项目根
            root_env = Path(__file__).resolve().parents[2] / ".env"
            if root_env.exists():
                for line in root_env.read_text(encoding="utf-8").splitlines():
                    raw = line.strip()
                    if not raw or raw.startswith("#") or "=" not in raw:
                        continue
                    key, value = raw.split("=", 1)
                    key = key.strip()
                    if key:
                        env_map[key] = value.strip().strip("'").strip('"')
        except Exception:
            pass
        _root_env_cache = env_map
    return _root_env_cache


def _read(key: str, default: str = "") -> str:
    value = os.getenv(key)
    if value is not None and str(value).strip() != "":
        return str(value).strip()
    return _load_root_env_map().get(key, default)


def remote_quote_disabled() -> bool:
    return str(os.getenv("REMOTE_QUOTE_DISABLED", "")).strip().lower() in _DISABLED_VALUES


def using_builtin_free_feed() -> bool:
    """是否按内置公网免费行情服解析（host 为内置默认）。

    私有部署应显式配 REMOTE_QUOTE_REDIS_HOST/PASSWORD；仍落回公共服时
    resolve 会打一次 warning（无 SLA、口令为公开信息）。
    """
    if remote_quote_disabled():
        return False
    return _read("REMOTE_QUOTE_REDIS_HOST", FREE_FEED_HOST) == FREE_FEED_HOST


def _warn_once_if_builtin_default(host: str) -> None:
    global _warned_builtin_default
    if _warned_builtin_default or host != FREE_FEED_HOST:
        return
    _warned_builtin_default = True
    logger.warning(
        "远端行情仍在使用内置公共免费行情服（%s）——该地址与口令为公开信息、无 SLA，"
        "私有部署请在 .env 配置 REMOTE_QUOTE_REDIS_HOST/PASSWORD 覆盖",
        host,
    )


def resolve_remote_quote_redis() -> tuple[str, int, str | None, int] | None:
    """解析 (host, port, password, db)；显式关闭或主机为空返回 None。"""
    if remote_quote_disabled():
        return None
    host = _read("REMOTE_QUOTE_REDIS_HOST", FREE_FEED_HOST)
    if not host:
        return None
    try:
        port = int(_read("REMOTE_QUOTE_REDIS_PORT", str(FREE_FEED_PORT)) or FREE_FEED_PORT)
    except (TypeError, ValueError):
        port = FREE_FEED_PORT
    try:
        db = int(_read("REMOTE_QUOTE_REDIS_DB", str(FREE_FEED_DB)) or FREE_FEED_DB)
    except (TypeError, ValueError):
        db = FREE_FEED_DB
    password = _read("REMOTE_QUOTE_REDIS_PASSWORD", FREE_FEED_PASSWORD) or None
    _warn_once_if_builtin_default(host)
    return host, port, password, db


def make_sync_client(*, socket_timeout: float = 5.0, socket_connect_timeout: float = 3.0):
    """构造**同步**远端行情 Redis 客户端（唯一构造点；未配置/已禁用返回 None）。

    消费方（订阅写侧/热集构建等）统一经此获取，避免各自拼装连接参数。
    """
    import redis as _redis

    resolved = resolve_remote_quote_redis()
    if resolved is None:
        return None
    host, port, password, db = resolved
    return _redis.Redis(
        host=host,
        port=port,
        password=password,
        db=db,
        decode_responses=True,
        socket_connect_timeout=socket_connect_timeout,
        socket_timeout=socket_timeout,
    )
