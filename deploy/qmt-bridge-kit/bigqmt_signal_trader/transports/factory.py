"""Transport factory: pick a transport backend by name.

``build_transport(name, config, ...)`` returns a ready transport instance.
``name`` is the ``rpc.transport`` config value (default ``"redis"``). Unknown
names raise :class:`ValueError`. Optional dependencies (``zmq``, a mysql
driver) are imported lazily; a missing dependency surfaces as a clear
``ImportError`` only when that transport is actually selected.
"""

from .base import RpcTransport
from .redis_transport import RedisTransport


KNOWN_TRANSPORTS = ("redis", "zmq", "mysql", "shm", "pipe")


def transport_supports_drain(name):
    """这个传输自己实现了 drain_request_queue 吗（不构造实例）。

    调用方要判断「能不能走 adjust 轮询」，而**构造实例是有副作用的** ——
    建 redis 连接、起线程。用它做探测污染过测试环境一次，所以这里只导入
    模块、比对类属性，不 build。

    工厂是「有哪些传输」的唯一来源，这个判断也该在这里，而不是让调用方
    各自维护一张名单（pipe 就是在别处的名单里漏掉，导致 drain 从未生效）。
    """
    from .base import RpcTransport

    name = str(name or "redis").lower()
    try:
        if name in ("redis", "", "default"):
            from .redis_transport import RedisTransport as cls
        elif name == "zmq":
            from .zmq_transport import ZmqTransport as cls
        elif name == "mysql":
            from .mysql_transport import MysqlTransport as cls
        elif name == "pipe":
            from .pipe_transport import NamedPipeTransport as cls
        else:
            return False
    except ImportError:
        # 缺依赖（没装 pyzmq / mysql 驱动）是合法的「不支持」。但类名写错也会
        # 走到这里 —— MySqlTransport / MysqlTransport 就错过一次，被静默吞成
        # False。所以只吞 ImportError，AttributeError 之类必须炸出来。
        return False
    own = getattr(cls, "drain_request_queue", None)
    return own is not None and own is not getattr(RpcTransport, "drain_request_queue", None)


def build_transport(
    name,
    config=None,
    account_id="",
    print_prefix="[bigqmt_rpc]",
):
    """Construct a transport by name.

    ``config`` is the ``rpc`` config dict. Each backend reads its own sub-keys
    (``config["zmq"]``, ``config["mysql"]``); Redis reads the legacy keys
    (``request_channel_template`` etc.) plus ``redis_client``/
    ``response_redis_client`` that the caller may inject.
    """
    config = dict(config or {})
    name = str(name or "redis").lower()

    if name in ("redis", "", "default"):
        return _build_redis(config, account_id, print_prefix)
    if name == "zmq":
        return _build_zmq(config, account_id, print_prefix)
    if name == "mysql":
        return _build_mysql(config, account_id, print_prefix)
    if name == "pipe":
        # Windows named pipe: no third-party package, no socket. The only wire
        # that works on a terminal whose import whitelist rejects socket and
        # whose bundled Python cannot pip install.
        from .pipe_transport import NamedPipeTransport

        pipe_config = dict(config.get("pipe") or {})
        return NamedPipeTransport(
            account_id=account_id,
            print_prefix=print_prefix,
            pipe_name=pipe_config.get("pipe_name") or "bigqmt_rpc",
            connect_timeout_seconds=float(
                pipe_config.get("connect_timeout_seconds") or 5.0),
        )
    if name == "shm":
        return _build_shm(config, account_id, print_prefix)
    raise ValueError(
        "unknown rpc transport %r (known: %s)" % (name, ", ".join(KNOWN_TRANSPORTS))
    )


def _build_redis(config, account_id, print_prefix):
    redis_client = config.get("redis_client")
    if redis_client is None:
        from ..adapters.redis_common import build_redis_client

        redis_config = dict(config.get("redis") or {})
        redis_client = build_redis_client(redis_config)
    response_redis_client = config.get("response_redis_client")
    if response_redis_client is None:
        response_redis_client = redis_client
    return RedisTransport(
        redis_client,
        account_id=account_id,
        response_redis_client=response_redis_client,
        request_channel_template=config.get(
            "request_channel_template", "bigqmt:rpc:req:{account_id}"
        ),
        request_queue_template=config.get(
            "request_queue_template", "bigqmt:rpc:queue:{account_id}"
        ),
        response_channel_template=config.get(
            "response_channel_template", "bigqmt:rpc:resp:{account_id}:{request_id}"
        ),
        response_list_template=config.get(
            "response_list_template", "bigqmt:rpc:respq:{account_id}:{request_id}"
        ),
        response_key_template=config.get(
            "response_key_template", "bigqmt:rpc:resp:{account_id}:{request_id}"
        ),
        response_ttl_seconds=int(config.get("response_ttl_seconds", 60)),
        queue_poll_interval_seconds=float(config.get("queue_poll_interval_seconds", 0.02)),
        debug_log_limit=int(config.get("debug_log_limit", 0)),
        print_prefix=print_prefix,
    )


def _build_zmq(config, account_id, print_prefix):
    from .zmq_transport import ZmqTransport

    zmq_config = dict(config.get("zmq") or {})
    # Wire up service discovery: if the caller injected a redis_client (server
    # side) or provided redis connection settings, the ZMQ transport can
    # publish/look up the actual bound port when the default port is taken.
    discovery_client = zmq_config.get("discovery_redis_client")
    if discovery_client is None and config.get("redis_client") is not None:
        discovery_client = config.get("redis_client")
        zmq_config["discovery_redis_client"] = discovery_client
    if discovery_client is None and config.get("redis"):
        # Build a small client just for discovery from the redis config block.
        try:
            from ..adapters.redis_common import build_redis_client

            discovery_client = build_redis_client(dict(config.get("redis") or {}))
            zmq_config["discovery_redis_client"] = discovery_client
        except Exception:
            pass
    return ZmqTransport.from_config(
        zmq_config,
        account_id=account_id,
        print_prefix=print_prefix,
    )


def _build_mysql(config, account_id, print_prefix):
    from .mysql_transport import MysqlTransport

    return MysqlTransport.from_config(
        config.get("mysql") or {},
        account_id=account_id,
        print_prefix=print_prefix,
    )


def _build_shm(config, account_id, print_prefix):
    from .shm_transport import SharedMemoryTransport

    return SharedMemoryTransport(
        account_id=account_id,
        print_prefix=print_prefix,
        **dict(config.get("shm") or {})
    )
