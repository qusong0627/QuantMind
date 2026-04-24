import json
import os
import time
from typing import Any, Dict, List

import redis


class SandboxContext:
    """
    提供给隔离执行沙箱 (Worker 进程) 的交易上下文 (Mock SDK)。
    在这里拦截所有真实的订单和查询动作，转为标准化的 JSON 结构放入内存队列中。
    """

    def __init__(
        self,
        tenant_id: str,
        user_id: str,
        strategy_id: str,
        run_id: str,
        exec_config: dict,
        live_trade_config: dict | None = None,
    ):
        self.tenant_id = tenant_id
        self.user_id = user_id
        self.strategy_id = strategy_id
        self.run_id = run_id
        self.exec_config = exec_config
        self.live_trade_config = live_trade_config or {}
        self.signals_queue: list[dict[str, Any]] = []
        self._current_time: float = time.time()
        self._redis: redis.Redis | None = None
        self._account_cache: dict[str, Any] = {}
        self._last_cache_time: float = 0

    def _get_redis(self) -> redis.Redis | None:
        """Worker 进程独立获取 Redis 连接"""
        if self._redis is not None:
            return self._redis
        try:
            host = os.getenv("REDIS_HOST", "127.0.0.1")
            port = int(os.getenv("REDIS_PORT", "6379"))
            password = os.getenv("REDIS_PASSWORD", None)
            db = int(os.getenv("REDIS_DB_TRADE", "2"))
            self._redis = redis.Redis(host=host, port=port, password=password, db=db, decode_responses=True)
            return self._redis
        except Exception:
            return None

    def _load_account_from_redis(self) -> dict[str, Any]:
        """从 Redis 加载账户状态，带 1 秒缓存"""
        now = time.time()
        if now - self._last_cache_time < 1.0 and self._account_cache:
            return self._account_cache

        r = self._get_redis()
        if not r:
            return self._account_cache

        key = f"simulation:account:{self.tenant_id}:{self.user_id}"
        try:
            raw = r.get(key)
            if raw:
                self._account_cache = json.loads(raw)
                self._last_cache_time = now
        except Exception:
            pass
        return self._account_cache

    def set_time(self, current_time: float):
        """由 Worker 事件循环驱动当前时间"""
        self._current_time = current_time

    def log(self, message: str):
        """收集策略日志并作为信号抛给引擎"""
        self.signals_queue.append({"type": "log", "timestamp": self._current_time, "message": str(message)})

    def _add_order_signal(self, symbol: str, quantity: int, price: float, side: str, order_type: str = "limit"):
        signal = {
            "type": "order",
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "strategy_id": self.strategy_id,
            "run_id": self.run_id,
            "timestamp": self._current_time,
            "data": {"symbol": symbol, "quantity": quantity, "price": price, "side": side, "order_type": order_type},
        }
        self.signals_queue.append(signal)

    def order_target_percent(self, symbol: str, target_percent: float):
        """
        常见交易API接口：设置目标持仓比例。
        沙箱在这里只产生一条 intent (意图信号)，不真实向柜台发单。
        """
        signal = {
            "type": "order_target_percent",
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "strategy_id": self.strategy_id,
            "run_id": self.run_id,
            "timestamp": self._current_time,
            "data": {"symbol": symbol, "target_percent": target_percent},
        }
        self.signals_queue.append(signal)

    def get_position(self, symbol: str) -> dict[str, Any]:
        """从 Redis 读取真实持仓状态"""
        account = self._load_account_from_redis()
        positions = account.get("positions", {})
        pos = positions.get(symbol.upper())
        if pos:
            return {
                "symbol": symbol.upper(),
                "volume": float(pos.get("volume", 0)),
                "cost": float(pos.get("cost", 0)),
                "price": float(pos.get("price", 0)),
                "market_value": float(pos.get("market_value", 0)),
            }
        return {"symbol": symbol.upper(), "volume": 0, "cost": 0, "price": 0, "market_value": 0}

    def get_cash(self) -> float:
        """从 Redis 读取真实可用现金"""
        account = self._load_account_from_redis()
        return float(account.get("cash", 0))

    def get_total_asset(self) -> float:
        """从 Redis 读取真实总资产"""
        account = self._load_account_from_redis()
        return float(account.get("total_asset", 0))

    def flush_signals(self) -> list[dict[str, Any]]:
        """Worker 每个 Tick 结束后，将收集到的信号抛出。"""
        signals = list(self.signals_queue)
        self.signals_queue.clear()
        return signals


def create_sandbox_context(
    tenant_id: str,
    user_id: str,
    strategy_id: str,
    run_id: str,
    exec_config: dict,
    live_trade_config: dict | None = None,
) -> SandboxContext:
    return SandboxContext(tenant_id, user_id, strategy_id, run_id, exec_config, live_trade_config)
