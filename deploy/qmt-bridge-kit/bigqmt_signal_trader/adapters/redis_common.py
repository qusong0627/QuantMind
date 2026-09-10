"""Redis client helpers for Big QMT signal trader."""

import os


def _float_or_none(value, default=None):
    if value is None:
        return default
    if value == "":
        return default
    text = str(value).strip()
    if text.lower() in ("none", "null"):
        return None
    return float(value)


def redis_supports_protocol_kw():
    """redis-py 的 Redis.__init__ 从 5.0 起才有 protocol 参数；QMT 自带的
    redis-py 3.5.3 不认，硬传直接 TypeError 崩掉（issue #71，PR #67 的回归）。"""
    import inspect

    try:
        import redis

        return "protocol" in inspect.signature(redis.Redis.__init__).parameters
    except Exception:
        return False


def build_redis_client(config=None):
    config = config or {}
    try:
        import redis
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("redis package is required when Redis adapters are enabled") from exc

    url = config.get("url") or os.environ.get("BIGQMT_REDIS_URL")
    if url:
        return redis.Redis.from_url(
            url,
            socket_connect_timeout=_float_or_none(config.get("socket_connect_timeout", 1.5), 1.5),
            socket_timeout=_float_or_none(config.get("socket_timeout", 1.5), 1.5),
        )

    host = config.get("host") or os.environ.get("BIGQMT_REDIS_HOST") or "127.0.0.1"
    port = int(config.get("port") or os.environ.get("BIGQMT_REDIS_PORT") or 6379)
    db = int(config.get("db") or os.environ.get("BIGQMT_REDIS_DB") or 5)
    username = config.get("username") or os.environ.get("BIGQMT_REDIS_USERNAME") or None
    password = config.get("password") or os.environ.get("BIGQMT_REDIS_PASSWORD") or None
    # redis-py 8.x 默认 RESP3，Redis 5.0 只支持 RESP2 -> 强制 protocol=2；
    # 但 QMT 自带的 redis-py 3.5.3 没有 protocol 参数（issue #71），按版本能力透传。
    protocol = int(config.get("protocol") or os.environ.get("BIGQMT_REDIS_PROTOCOL") or 2)
    kwargs = dict(
        host=host,
        port=port,
        db=db,
        username=username,
        password=password,
        socket_connect_timeout=_float_or_none(config.get("socket_connect_timeout", 1.5), 1.5),
        socket_timeout=_float_or_none(config.get("socket_timeout", 1.5), 1.5),
        health_check_interval=int(config.get("health_check_interval", 30)),
    )
    if redis_supports_protocol_kw():
        kwargs["protocol"] = protocol
    return redis.Redis(**kwargs)


def decode_text(value):
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


def redis_mapping_to_text(mapping):
    return {decode_text(key): decode_text(value) for key, value in (mapping or {}).items()}


# redis streams (XADD/XREAD) need redis >= 5.0. Older servers (Windows builds
# are often 3.0.x) answer with "unknown command 'XADD'". Learn it once from the
# failure, say it once, and let callers skip xadd from then on -- pub/sub keeps
# working on those servers (issue #163).
# 事件流的滑动过期时间。
#
# maxlen 只挡住单个键的无限增长，挡不住**键本身永远不消失**：换个账号、停用一个
# 部署、跑一次探测，键就永远留在 redis 里。实测线上七个事件流全是永久键
# （position_events 843KB、order_events 1.41MB…）。
#
# 所以每次 xadd 之后顺手续一次期：还在写的流一直续上，停了的自然消失。一天足够
# 覆盖「昨天收盘到今天开盘」的回放需求，又不会让废弃账号的键长期占着（#213）。
EVENT_STREAM_TTL_SECONDS = 86400


def touch_stream_ttl(redis_client, key, ttl_seconds=EVENT_STREAM_TTL_SECONDS):
    """给事件流续期。失败不能影响写入本身 —— 这是清理，不是主路径。"""
    try:
        ttl = int(ttl_seconds)
    except (TypeError, ValueError):
        return False
    if ttl <= 0:
        return False
    try:
        redis_client.expire(key, ttl)
        return True
    except Exception:
        return False


_STREAMS_DEAD = False


def streams_dead():
    return _STREAMS_DEAD


def note_stream_failure(exc, log=None):
    """True when *exc* is the redis<5.0 'unknown command' stream failure.

    The first such failure logs once and marks streams dead process-wide.
    Any other exception returns False and changes nothing (transient redis
    issues stay retried).
    """
    global _STREAMS_DEAD
    if "unknown command" not in str(exc or "").lower():
        return False
    if _STREAMS_DEAD:
        return True
    _STREAMS_DEAD = True
    message = (
        "redis has no streams support (XADD: unknown command, redis < 5.0): "
        "event/position replay streams are disabled for this process; pub/sub "
        "callbacks keep working. Upgrade the redis server to >= 5.0 to "
        "restore replay."
    )
    try:
        if log is not None:
            log.warning(message)
        else:
            print("[bigqmt] " + message)
    except Exception:
        pass
    return True
