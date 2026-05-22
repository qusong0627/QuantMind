"""
企业级Redis哨兵客户端
支持主从自动故障转移、连接池、健康检查
"""

import json
import logging
import os
import pickle
import random
from pathlib import Path
from typing import Any, Optional

try:
    # 优先加载项目根目录 .env 供 Redis 配置使用
    from dotenv import load_dotenv  # type: ignore

    _ROOT_ENV = Path(__file__).resolve().parents[2] / ".env"
    if _ROOT_ENV.exists():
        load_dotenv(_ROOT_ENV, override=False)
except Exception:
    # 无 python-dotenv 或加载失败时忽略
    pass

from redis.sentinel import Sentinel

from redis import Redis

logger = logging.getLogger(__name__)


class RedisSentinelConfig:
    """Redis 配置加载"""

    def __init__(self):
        # 0. 确定是否在 Docker 环境
        is_docker = os.path.exists("/.dockerenv")

        # 1. 核心连接参数
        self.host = os.getenv("REDIS_HOST")
        print(f"[RedisSentinelConfig] REDIS_HOST from env: {self.host}")

        if not self.host:
            self.host = "quantmind-redis" if is_docker else "127.0.0.1"
            print(f"[RedisSentinelConfig] REDIS_HOST not set, defaulting to: {self.host}")
        elif is_docker and self.host in ("localhost", "127.0.0.1"):
            self.host = "quantmind-redis"
            print(f"[RedisSentinelConfig] Corrected localhost to quantmind-redis in Docker: {self.host}")

        self.port = int(os.getenv("REDIS_PORT", "6379"))
        self.db = int(os.getenv("REDIS_DB", "0"))
        self.password = os.getenv("REDIS_PASSWORD", None)

        # 2. 哨兵集群参数
        self.sentinels_raw = os.getenv("REDIS_SENTINELS", "")
        self.sentinels = self._parse_sentinels(self.sentinels_raw)
        self.master_name = os.getenv("REDIS_MASTER_NAME", "quantmind-master")

        # 3. 模式自动感应
        # 如果明确设置了哨兵地址，则启用哨兵模式；否则使用单机直连（兼容 GCP Memorystore）
        self.use_sentinel = (os.getenv("REDIS_USE_SENTINEL", "true").lower() == "true") and bool(self.sentinels)

        # 4. 连接池与超时
        self.max_connections = int(os.getenv("REDIS_MAX_CONNECTIONS", "100"))
        self.socket_timeout = float(os.getenv("REDIS_SOCKET_TIMEOUT", "5"))
        self.socket_connect_timeout = float(os.getenv("REDIS_SOCKET_CONNECT_TIMEOUT", "5"))
        self.health_check_interval = int(os.getenv("REDIS_HEALTH_CHECK_INTERVAL", "30"))
        self.decode_responses = False  # 保持 bytes 兼容 pickle

        if self.use_sentinel:
            logger.info(f"Redis Mode: SENTINEL (Masters: {self.master_name})")
        else:
            logger.info(f"Redis Mode: STANDALONE (Target: {self.host}:{self.port})")

    def _parse_sentinels(self, sentinels_str: str) -> list[tuple[str, int]]:
        """解析哨兵地址列表"""
        sentinels = []
        if not sentinels_str:
            return sentinels
        for s in sentinels_str.split(","):
            s = s.strip()
            if not s:
                continue
            parts = s.split(":")
            host = parts[0]
            port = int(parts[1]) if len(parts) > 1 else 26379
            sentinels.append((host, port))
        return sentinels


class RedisSentinelClient:
    """统一 Redis 客户端（屏蔽底层部署差异）"""

    def __init__(self, config: RedisSentinelConfig | None = None):
        self.config = config or RedisSentinelConfig()
        self._sentinel: Sentinel | None = None
        self._master_client: Redis | None = None
        self._slave_client: Redis | None = None
        self._initialized = False

    def _ensure_connection(self):
        """确保连接已建立"""
        if self._initialized:
            return

        try:
            if not self.config.use_sentinel:
                # --- Standalone 模式 (GCP Memorystore / Dev) ---
                self._master_client = Redis(
                    host=self.config.host,
                    port=self.config.port,
                    db=self.config.db,
                    password=self.config.password,
                    socket_timeout=self.config.socket_timeout,
                    socket_connect_timeout=self.config.socket_connect_timeout,
                    max_connections=self.config.max_connections,
                    health_check_interval=self.config.health_check_interval,
                    decode_responses=self.config.decode_responses,
                )
                self._slave_client = None  # Standalone 模式下读写均为主
            else:
                # --- Sentinel 模式 (HA Cluster) ---
                self._sentinel = Sentinel(
                    self.config.sentinels,
                    socket_timeout=self.config.socket_timeout,
                    password=self.config.password,
                    decode_responses=self.config.decode_responses,
                )
                self._master_client = self._sentinel.master_for(
                    self.config.master_name,
                    password=self.config.password,
                    max_connections=self.config.max_connections,
                )
                self._slave_client = self._sentinel.slave_for(
                    self.config.master_name,
                    password=self.config.password,
                    max_connections=self.config.max_connections,
                )

            self._master_client.ping()
            self._initialized = True
            logger.info("✅ Redis Connection Established")
        except Exception as e:
            logger.error(f"❌ Redis Connection Failed: {e}")
            raise

    def get_master_info(self) -> dict:
        """获取主库信息"""
        self._ensure_connection()
        if self._sentinel:
            return self._sentinel.discover_master(self.config.master_name)
        return {
            "host": self.config.single_host,
            "port": self.config.single_port,
            "db": self.config.single_db,
            "mode": "single",
        }

    def get_slave_info(self) -> list[dict]:
        """获取从库信息"""
        self._ensure_connection()
        if self._sentinel:
            return self._sentinel.discover_slaves(self.config.master_name)
        return []

    def ping(self, use_slave: bool = False) -> bool:
        """
        测试Redis连接

        Args:
            use_slave: 是否使用从库

        Returns:
            bool: 连接是否正常
        """
        try:
            self._ensure_connection()
            if use_slave and self._slave_client:
                return self._slave_client.ping()
            else:
                return self._master_client.ping()
        except Exception as e:
            logger.error(f"Redis ping failed: {e}")
            return False

    def get(self, key: str, use_slave: bool = True) -> bytes | None:
        """
        获取缓存值

        Args:
            key: 缓存键
            use_slave: 是否使用从库读取

        Returns:
            缓存值(bytes)，如果不存在返回None
        """
        try:
            self._ensure_connection()
            client = self._slave_client if use_slave and self._slave_client else self._master_client
            return client.get(key)
        except Exception as e:
            logger.error(f"Redis GET failed for key {key}: {e}")
            # 如果从库失败，尝试主库
            if use_slave and self._slave_client:
                try:
                    return self._master_client.get(key)
                except Exception as e2:
                    logger.error(f"Redis GET failed on master for key {key}: {e2}")
            return None

    def mget(self, keys: list[str], use_slave: bool = True) -> list[bytes | None]:
        """
        批量获取缓存值

        Args:
            keys: 缓存键列表
            use_slave: 是否使用从库读取

        Returns:
            缓存值列表，顺序与keys一致
        """
        try:
            self._ensure_connection()
            client = self._slave_client if use_slave and self._slave_client else self._master_client
            return client.mget(keys)
        except Exception as e:
            logger.error(f"Redis MGET failed for keys {keys}: {e}")
            if use_slave and self._slave_client:
                try:
                    return self._master_client.mget(keys)
                except Exception as e2:
                    logger.error(f"Redis MGET failed on master: {e2}")
            return [None] * len(keys)

    def set(self, key: str, value: bytes, ex: int | None = None, nx: bool = False) -> bool:
        """
        设置缓存值

        Args:
            key: 缓存键
            value: 缓存值(bytes)
            ex: 过期时间(秒)，None表示不过期
            nx: 仅当key不存在时设置

        Returns:
            bool: 是否设置成功
        """
        try:
            self._ensure_connection()
            return self._master_client.set(key, value, ex=ex, nx=nx)
        except Exception as e:
            logger.error(f"Redis SET failed for key {key}: {e}")
            return False

    def setex(self, key: str, time: int, value: bytes) -> bool:
        """
        设置缓存值并指定过期时间

        Args:
            key: 缓存键
            time: 过期时间(秒)
            value: 缓存值(bytes)

        Returns:
            bool: 是否设置成功
        """
        try:
            self._ensure_connection()
            return self._master_client.setex(key, time, value)
        except Exception as e:
            logger.error(f"Redis SETEX failed for key {key}: {e}")
            return False

    def incr(self, key: str, amount: int = 1) -> int:
        """
        递增数值键

        Args:
            key: 缓存键
            amount: 增量，默认+1

        Returns:
            int: 递增后的值
        """
        try:
            self._ensure_connection()
            return self._master_client.incr(key, amount)
        except Exception as e:
            logger.error(f"Redis INCR failed for key {key}: {e}")
            raise

    def rpush(self, key: str, *values: Any) -> int:
        """
        在列表右侧插入元素

        Args:
            key: 列表键
            *values: 要插入的值

        Returns:
            int: 列表长度
        """
        try:
            self._ensure_connection()
            return self._master_client.rpush(key, *values)
        except Exception as e:
            logger.error(f"Redis RPUSH failed for key {key}: {e}")
            raise

    def lrange(self, key: str, start: int, end: int) -> list:
        """获取列表指定范围的元素，返回 bytes 列表"""
        try:
            self._ensure_connection()
            return self._master_client.lrange(key, start, end)
        except Exception as e:
            logger.error(f"Redis LRANGE failed for key {key}: {e}")
            return []

    def publish(self, channel: str, message: str) -> int:
        """向频道发布消息"""
        try:
            self._ensure_connection()
            return self._master_client.publish(channel, message)
        except Exception as e:
            logger.error(f"Redis PUBLISH failed for channel {channel}: {e}")
            return 0

    def xadd(
        self,
        stream: str,
        fields: dict[str, Any],
        maxlen: int | None = None,
        approximate: bool = True,
    ) -> bytes:
        """向 Redis Stream 写入一条消息。"""
        try:
            self._ensure_connection()
            return self._master_client.xadd(
                stream,
                fields,
                maxlen=maxlen,
                approximate=approximate,
            )
        except Exception as e:
            logger.error(f"Redis XADD failed for stream {stream}: {e}")
            raise

    def hset(self, key: str, field: str = None, value: Any = None, mapping: dict = None) -> int:
        """设置 Hash 字段值"""
        try:
            self._ensure_connection()
            return self._master_client.hset(key, field, value, mapping)
        except Exception as e:
            logger.error(f"Redis HSET failed for key {key}: {e}")
            return 0

    def hgetall(self, key: str, use_slave: bool = True) -> dict:
        """获取 Hash 所有字段"""
        try:
            self._ensure_connection()
            client = self._slave_client if use_slave and self._slave_client else self._master_client
            res = client.hgetall(key)
            # 兼容 decode_responses=False 模式，手动转码
            if not self.config.decode_responses:
                return {k.decode(): v.decode() for k, v in res.items()}
            return res
        except Exception as e:
            logger.error(f"Redis HGETALL failed for key {key}: {e}")
            return {}

    def hexists(self, key: str, field: str, use_slave: bool = True) -> bool:
        """检查 Hash 中是否存在指定字段"""
        try:
            self._ensure_connection()
            client = self._slave_client if use_slave and self._slave_client else self._master_client
            return client.hexists(key, field)
        except Exception as e:
            logger.error(f"Redis HEXISTS failed for key {key}: {e}")
            return False

    def hdel(self, key: str, *fields: str) -> int:
        """删除 Hash 字段"""
        try:
            self._ensure_connection()
            return self._master_client.hdel(key, *fields)
        except Exception as e:
            logger.error(f"Redis HDEL failed for key {key}: {e}")
            return 0

    def xlen(self, stream: str) -> int:
        """获取 Stream 长度"""
        try:
            self._ensure_connection()
            return self._master_client.xlen(stream)
        except Exception as e:
            logger.error(f"Redis XLEN failed for stream {stream}: {e}")
            return 0

    def delete(self, *keys: str) -> int:
        """
        删除缓存键

        Args:
            *keys: 要删除的键列表

        Returns:
            int: 成功删除的键数量
        """
        try:
            self._ensure_connection()
            return self._master_client.delete(*keys)
        except Exception as e:
            logger.error(f"Redis DELETE failed for keys {keys}: {e}")
            return 0

    def exists(self, *keys: str) -> int:
        """
        检查键是否存在

        Args:
            *keys: 要检查的键列表

        Returns:
            int: 存在的键数量
        """
        try:
            self._ensure_connection()
            client = self._slave_client if self._slave_client else self._master_client
            return client.exists(*keys)
        except Exception as e:
            logger.error(f"Redis EXISTS failed for keys {keys}: {e}")
            return 0

    def expire(self, key: str, time: int) -> bool:
        """
        设置键的过期时间

        Args:
            key: 缓存键
            time: 过期时间(秒)

        Returns:
            bool: 是否设置成功
        """
        try:
            self._ensure_connection()
            return self._master_client.expire(key, time)
        except Exception as e:
            logger.error(f"Redis EXPIRE failed for key {key}: {e}")
            return False

    def ttl(self, key: str) -> int:
        """
        获取键的剩余过期时间

        Args:
            key: 缓存键

        Returns:
            int: 剩余秒数，-1表示永不过期，-2表示键不存在
        """
        try:
            self._ensure_connection()
            client = self._slave_client if self._slave_client else self._master_client
            return client.ttl(key)
        except Exception as e:
            logger.error(f"Redis TTL failed for key {key}: {e}")
            return -2

    def pipeline(self):
        """
        创建Pipeline用于批量操作

        Returns:
            redis.client.Pipeline
        """
        try:
            self._ensure_connection()
            return self._master_client.pipeline()
        except Exception as e:
            logger.error(f"Redis PIPELINE failed: {e}")
            raise

    def get_ttl_with_jitter(self, base_ttl: int, jitter_percent: float = 0.1) -> int:
        """
        获取带随机偏移的TTL，避免缓存雪崩

        Args:
            base_ttl: 基础TTL(秒)
            jitter_percent: 随机偏移百分比

        Returns:
            int: 带随机偏移的TTL
        """
        jitter = int(base_ttl * jitter_percent * random.random())
        return base_ttl + jitter

    def set_object(
        self,
        key: str,
        obj: Any,
        ex: int | None = None,
        serializer: str = "pickle",
    ) -> bool:
        """
        存储Python对象

        Args:
            key: 缓存键
            obj: Python对象
            ex: 过期时间(秒)
            serializer: 序列化方式 ('pickle' 或 'json')

        Returns:
            bool: 是否设置成功
        """
        try:
            if serializer == "json":
                value = json.dumps(obj).encode()
            else:  # pickle
                value = pickle.dumps(obj)
            return self.set(key, value, ex=ex)
        except Exception as e:
            logger.error(f"Redis SET_OBJECT failed for key {key}: {e}")
            return False

    def get_object(self, key: str, use_slave: bool = True, serializer: str = "pickle") -> Any | None:
        """
        获取Python对象

        Args:
            key: 缓存键
            use_slave: 是否使用从库
            serializer: 序列化方式 ('pickle' 或 'json')

        Returns:
            Python对象，如果不存在返回None
        """
        try:
            value = self.get(key, use_slave=use_slave)
            if value is None:
                return None

            if serializer == "json":
                return json.loads(value.decode())
            else:  # pickle
                return pickle.loads(value)
        except Exception as e:
            logger.error(f"Redis GET_OBJECT failed for key {key}: {e}")
            return None

    def health_check(self) -> dict:
        """
        健康检查

        Returns:
            dict: 健康检查结果
        """
        result = {"master": False, "slave": False, "sentinel": False}

        try:
            self._ensure_connection()

            # 检查主库
            result["master"] = self.ping(use_slave=False)

            # 检查从库
            if self._slave_client:
                result["slave"] = self.ping(use_slave=True)

            # 检查哨兵
            if self._sentinel:
                try:
                    master_info = self.get_master_info()
                    result["sentinel"] = master_info is not None
                except Exception as e:
                    logger.error(f"Sentinel health check failed: {e}")
                    result["sentinel"] = False

        except Exception as e:
            logger.error(f"Redis health check failed: {e}")

        return result

    def get_info(self) -> dict:
        """获取Redis信息"""
        try:
            self._ensure_connection()
            return {
                "master": self._master_client.info(),
                "slave": self._slave_client.info() if self._slave_client else None,
            }
        except Exception as e:
            logger.error(f"Redis GET_INFO failed: {e}")
            return {}

    def close(self):
        """关闭Redis连接"""
        if self._master_client:
            try:
                self._master_client.close()
                logger.info("Redis master client closed")
            except Exception as e:
                logger.error(f"Error closing Redis master client: {e}")

        if self._slave_client:
            try:
                self._slave_client.close()
                logger.info("Redis slave client closed")
            except Exception as e:
                logger.error(f"Error closing Redis slave client: {e}")

        self._initialized = False


# 全局Redis客户端实例
_redis_sentinel_client: RedisSentinelClient | None = None


def get_redis_sentinel_client() -> RedisSentinelClient:
    """
    获取全局Redis哨兵客户端实例(单例模式)

    Returns:
        RedisSentinelClient: Redis哨兵客户端实例
    """
    global _redis_sentinel_client

    if _redis_sentinel_client is None:
        _redis_sentinel_client = RedisSentinelClient()

    return _redis_sentinel_client


def close_redis_sentinel_client():
    """关闭全局Redis哨兵客户端"""
    global _redis_sentinel_client

    if _redis_sentinel_client:
        _redis_sentinel_client.close()
        _redis_sentinel_client = None
