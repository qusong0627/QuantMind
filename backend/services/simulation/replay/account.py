"""回放会话账户：与日常模拟盘账户完全隔离。

只覆盖 Redis key 方法，两个 Lua 脚本（含 T+1 扣减与 available_volume 的
nil 兼容分支）原样继承 —— 那个分支一旦丢失，存量持仓会永久不可卖且不报错。

附带的隔离好处：`replay:` 前缀不匹配 `simulation:account:*`，所以
SimulationFundSnapshotService.capture_all 的全局 SCAN 永远扫不到回放账户。
"""

from __future__ import annotations

import uuid

from backend.services.trade_shared.redis_client import RedisClient, get_redis
from backend.services.trade_shared.simulation_manager import (
    SimulationAccountManager,
)
from backend.shared.trade_account_cache import write_json_cache


class ReplayAccountManager(SimulationAccountManager):
    """按 session_id 而非 (tenant, user) 寻址的账户管理器。

    父类方法签名带 user_id/tenant_id，这里一律忽略：session_id 已经唯一确定
    一个回放账户。调用方仍需传占位值以满足签名（传 0 / "default" 即可），
    统一由 for_session() 包装避免出错。
    """

    def __init__(self, session_id: uuid.UUID | str, redis: RedisClient | None = None):
        # 省略 redis 时用 get_redis()：它保证共享单例已连接，
        # 直接 RedisClient() 只会拿到 client=None 的未连接实例。
        super().__init__(redis or get_redis())
        self._session_id = str(session_id)

    @property
    def session_id(self) -> str:
        return self._session_id

    def _get_key(self, user_id: int, tenant_id: str, market: str = "CN") -> str:
        # 签名与父类 SimulationAccountManager._get_key 对齐（多市场支持后
        # 父类会传入 market 占位）；回放账户按 session_id 寻址，忽略 market。
        return f"replay:account:{self._session_id}"

    def _get_settings_key(self, user_id: int, tenant_id: str) -> str:
        return f"replay:settings:{self._session_id}"

    def _lookup_keys(self, user_id: int, tenant_id: str, market: str = "CN") -> list[str]:
        # 回放账户与日常模拟盘**严格隔离**：读取只认会话键，不并入用户名别名
        # （父类别名查找面向 simulation:account:* ，若并集可能读到真实账户数据）。
        return [self._get_key(user_id, tenant_id, market)]

    def _settings_lookup_keys(self, user_id: int, tenant_id: str) -> list[str]:
        # 同上：设置读取同样只认会话键。
        return [self._get_settings_key(user_id, tenant_id)]

    # ------------------------------------------------------------------
    # 便捷包装：省掉调用方到处传占位的 user_id/tenant_id
    # ------------------------------------------------------------------
    async def init(self, initial_cash: float) -> dict:
        return await self.init_account(user_id=0, initial_cash=initial_cash, tenant_id="default")

    async def get(self) -> dict | None:
        return await self.get_account(user_id=0, tenant_id="default")

    def write(self, account_data: dict) -> None:
        """整体回写账户（收盘估值需要，Lua 只按最后成交价算市值）。"""
        write_json_cache(self.redis, self._get_key(0, "default"), account_data)

    async def unlock(self) -> dict:
        return await self.unlock_t1(user_id=0, tenant_id="default")

    async def apply_fill(
        self,
        symbol: str,
        delta_cash: float,
        delta_volume: float,
        price: float,
    ) -> dict:
        return await self.update_balance(
            user_id=0,
            symbol=symbol,
            delta_cash=delta_cash,
            delta_volume=delta_volume,
            price=price,
            tenant_id="default",
        )

    def drop(self) -> None:
        """丢弃会话时清除 Redis 账户。"""
        if self.redis.client:
            self.redis.client.delete(self._get_key(0, "default"))
            self.redis.client.delete(self._get_settings_key(0, "default"))
