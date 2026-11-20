"""
Redis缓存管理器 - Qlib服务专用

提供回测结果的缓存功能，减轻数据库压力
"""

import json
import logging
import pickle
from typing import Optional

import redis
from redis import ConnectionPool

logger = logging.getLogger(__name__)
from backend.services.engine.qlib_app.utils.structured_logger import StructuredTaskLogger

task_logger = StructuredTaskLogger(logger, "CacheManager")


class CacheManager:
    """Redis缓存管理器"""

    # 缓存key前缀
    PREFIX_BACKTEST_RESULT = "qlib:backtest:result:"
    PREFIX_BACKTEST_STATUS = "qlib:backtest:status:"
    PREFIX_USER_HISTORY = "qlib:user:history:"
    PREFIX_HOT_STRATEGIES = "qlib:hot:strategies"

    # TTL配置（秒）
    TTL_RESULT = 86400  # 结果缓存24小时
    TTL_STATUS = 300  # 状态缓存5分钟
    TTL_HISTORY = 600  # 历史列表缓存10分钟
    TTL_HOT = 3600  # 热门策略缓存1小时

    def __init__(
        self,
        host: str = "localhost",
        port: int = 6379,
        db: int = 5,  # Qlib缓存专用DB
        password: str | None = None,
        max_connections: int = 50,
    ):
        """
        初始化缓存管理器

        Args:
            host: Redis主机
            port: Redis端口
            db: 数据库编号
            password: 密码
            max_connections: 最大连接数
        """
        self.pool = ConnectionPool(
            host=host,
            port=port,
            db=db,
            password=password,
            max_connections=max_connections,
            decode_responses=False,  # 返回bytes以支持pickle
            socket_timeout=5,
            socket_connect_timeout=5,
            socket_keepalive=True,
            health_check_interval=30,
        )
        self.client = redis.Redis(connection_pool=self.pool)
        self._initialized = False

    def _ensure_connection(self):
        """确保Redis连接可用"""
        if not self._initialized:
            try:
                self.client.ping()
                self._initialized = True
                task_logger.info("connected", "Redis缓存管理器已连接")
            except Exception as e:
                task_logger.error("connect_failed", "Redis连接失败", error=str(e))
                raise

    def _make_key(self, prefix: str, identifier: str) -> str:
        """构建缓存key"""
        return f"{prefix}{identifier}"

    # ==================== 回测结果缓存 ====================

    def get_backtest_result(self, backtest_id: str) -> dict | None:
        """获取回测结果缓存"""
        try:
            self._ensure_connection()
            key = self._make_key(self.PREFIX_BACKTEST_RESULT, backtest_id)
            data = self.client.get(key)

            if data:
                task_logger.debug("cache_hit", "缓存命中", key=key)
                return pickle.loads(data)

            task_logger.debug("cache_miss", "缓存未命中", key=key)
            return None
        except Exception as e:
            task_logger.error("get_backtest_result_failed", "获取缓存失败", error=str(e))
            return None

    def set_backtest_result(self, backtest_id: str, result: dict, ttl: int | None = None):
        """设置回测结果缓存"""
        try:
            self._ensure_connection()
            key = self._make_key(self.PREFIX_BACKTEST_RESULT, backtest_id)
            data = pickle.dumps(result)

            if ttl is None:
                ttl = self.TTL_RESULT

            self.client.setex(key, ttl, data)
            task_logger.debug("cache_set", "缓存已设置", key=key, ttl=ttl)
        except Exception as e:
            task_logger.error("set_backtest_result_failed", "设置缓存失败", error=str(e))

    def delete_backtest_result(self, backtest_id: str):
        """删除回测结果缓存"""
        try:
            self._ensure_connection()
            key = self._make_key(self.PREFIX_BACKTEST_RESULT, backtest_id)
            self.client.delete(key)
            task_logger.debug("cache_deleted", "缓存已删除", key=key)
        except Exception as e:
            task_logger.error("delete_backtest_result_failed", "删除缓存失败", error=str(e))

    # ==================== 回测状态缓存 ====================

    def get_backtest_status(self, backtest_id: str) -> dict | None:
        """获取回测状态缓存（短TTL）"""
        try:
            self._ensure_connection()
            key = self._make_key(self.PREFIX_BACKTEST_STATUS, backtest_id)
            data = self.client.get(key)

            if data:
                return json.loads(data)
            return None
        except Exception as e:
            task_logger.error("get_backtest_status_failed", "获取状态缓存失败", error=str(e))
            return None

    def set_backtest_status(self, backtest_id: str, status: dict, ttl: int | None = None):
        """设置回测状态缓存"""
        try:
            self._ensure_connection()
            key = self._make_key(self.PREFIX_BACKTEST_STATUS, backtest_id)
            data = json.dumps(status, ensure_ascii=False)

            if ttl is None:
                ttl = self.TTL_STATUS

            self.client.setex(key, ttl, data)
        except Exception as e:
            task_logger.error("set_backtest_status_failed", "设置状态缓存失败", error=str(e))

    # ==================== 用户历史缓存 ====================

    def get_user_history(self, user_id: str) -> list | None:
        """获取用户历史缓存"""
        try:
            self._ensure_connection()
            key = self._make_key(self.PREFIX_USER_HISTORY, user_id)
            data = self.client.get(key)

            if data:
                return pickle.loads(data)
            return None
        except Exception as e:
            task_logger.error("get_user_history_failed", "获取历史缓存失败", error=str(e))
            return None

    def set_user_history(self, user_id: str, history: list, ttl: int | None = None):
        """设置用户历史缓存"""
        try:
            self._ensure_connection()
            key = self._make_key(self.PREFIX_USER_HISTORY, user_id)
            data = pickle.dumps(history)

            if ttl is None:
                ttl = self.TTL_HISTORY

            self.client.setex(key, ttl, data)
        except Exception as e:
            task_logger.error("set_user_history_failed", "设置历史缓存失败", error=str(e))

    def invalidate_user_history(self, user_id: str):
        """使用户历史缓存失效（当有新回测时调用）"""
        try:
            self._ensure_connection()
            key = self._make_key(self.PREFIX_USER_HISTORY, user_id)
            self.client.delete(key)
        except Exception as e:
            task_logger.error("invalidate_user_history_failed", "清除历史缓存失败", error=str(e))

    # ==================== 热点数据缓存 ====================

    def get_hot_strategies(self) -> list | None:
        """获取热门策略列表"""
        try:
            self._ensure_connection()
            data = self.client.get(self.PREFIX_HOT_STRATEGIES)

            if data:
                return json.loads(data)
            return None
        except Exception as e:
            task_logger.error("get_hot_strategies_failed", "获取热门策略缓存失败", error=str(e))
            return None

    def set_hot_strategies(self, strategies: list, ttl: int | None = None):
        """设置热门策略列表"""
        try:
            self._ensure_connection()
            data = json.dumps(strategies, ensure_ascii=False)

            if ttl is None:
                ttl = self.TTL_HOT

            self.client.setex(self.PREFIX_HOT_STRATEGIES, ttl, data)
        except Exception as e:
            task_logger.error("set_hot_strategies_failed", "设置热门策略缓存失败", error=str(e))

    # ==================== 通用方法 ====================

    def clear_all(self):
        """清空所有缓存（谨慎使用）"""
        try:
            self._ensure_connection()
            # 只删除qlib命名空间的key
            keys = self.client.keys("qlib:*")
            if keys:
                self.client.delete(*keys)
                task_logger.info("clear_all", "已清空缓存", count=len(keys))
        except Exception as e:
            task_logger.error("clear_all_failed", "清空缓存失败", error=str(e))

    def get_stats(self) -> dict:
        """获取缓存统计信息"""
        try:
            self._ensure_connection()
            info = self.client.info("stats")
            keyspace = self.client.info("keyspace")

            return {
                "total_connections_received": info.get("total_connections_received"),
                "total_commands_processed": info.get("total_commands_processed"),
                "keyspace": keyspace,
                "qlib_keys_count": len(self.client.keys("qlib:*")),
            }
        except Exception as e:
            task_logger.error("get_stats_failed", "获取统计信息失败", error=str(e))
            return {}

    def close(self):
        """关闭连接池"""
        try:
            self.pool.disconnect()
            task_logger.info("closed", "Redis连接池已关闭")
        except Exception as e:
            task_logger.error("close_failed", "关闭连接池失败", error=str(e))


# 全局缓存管理器实例
_cache_manager: CacheManager | None = None


def get_cache_manager() -> CacheManager:
    """获取全局缓存管理器实例（单例）"""
    global _cache_manager

    if _cache_manager is None:
        import os

        # 确定 Redis 主机，如果在 Docker 中且未配置或配置为 localhost，则使用容器名
        # （便携包 QM_PORTABLE=1 例外：自带 Redis，永远连本机）
        redis_host = os.getenv("REDIS_HOST")
        is_docker = os.path.exists("/.dockerenv") and os.getenv("QM_PORTABLE") != "1"

        if not redis_host:
            redis_host = "quantmind-redis" if is_docker else "localhost"
        elif is_docker and redis_host in ("localhost", "127.0.0.1"):
            redis_host = "quantmind-redis"

        _cache_manager = CacheManager(
            host=redis_host,
            port=int(os.getenv("REDIS_PORT", "6379")),
            db=int(os.getenv("REDIS_CACHE_DB", "5")),
            password=os.getenv("REDIS_PASSWORD", None),
        )

    return _cache_manager
