"""模拟盘 T+1 解锁任务单元测试。"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.services.simulation.services.simulation_t1_unlock_task import (
    _unlock_all_accounts,
)

_PATCH_TARGET = (
    "backend.services.simulation.services.simulation_t1_unlock_task.redis_client"
)


class TestUnlockAllAccounts:
    @pytest.mark.asyncio
    async def test_unlocks_valid_account_keys_only(self):
        # Arrange
        manager = MagicMock()
        manager.unlock_t1 = AsyncMock(
            side_effect=[
                {"success": True, "unlocked": 3},
                {"success": True, "unlocked": 0},
            ]
        )
        fake_redis = MagicMock()
        fake_redis.scan_iter.return_value = [
            "simulation:account:default:1",
            "simulation:account:default:00000001",
            "simulation:account:default:abc",  # 非数字 user_id 跳过
            "trade:account:default:1",  # 非模拟账户前缀跳过
            "simulation:account:short",  # 字段不足跳过
        ]

        # Act
        with patch(_PATCH_TARGET) as redis_client:
            redis_client.client = fake_redis
            unlocked_count = await _unlock_all_accounts(manager)

        # Assert: 2 个合法 key 各解锁一次，其中 1 个有新解锁持仓
        assert unlocked_count == 1
        assert manager.unlock_t1.await_count == 2

    @pytest.mark.asyncio
    async def test_market_suffix_is_passed_through(self):
        # Arrange: HK 后缀账户必须透传 market 解锁，不能解到 CN 账户上
        manager = MagicMock()
        manager.unlock_t1 = AsyncMock(return_value={"success": True, "unlocked": 1})
        fake_redis = MagicMock()
        fake_redis.scan_iter.return_value = ["simulation:account:default:7:HK"]

        # Act
        with patch(_PATCH_TARGET) as redis_client:
            redis_client.client = fake_redis
            assert await _unlock_all_accounts(manager) == 1

        # Assert
        manager.unlock_t1.assert_awaited_once_with(
            user_id=7, tenant_id="default", market="HK"
        )

    @pytest.mark.asyncio
    async def test_redis_scan_failure_returns_zero(self):
        manager = MagicMock()
        manager.unlock_t1 = AsyncMock()
        fake_redis = MagicMock()
        fake_redis.scan_iter.side_effect = RuntimeError("redis down")

        with patch(_PATCH_TARGET) as redis_client:
            redis_client.client = fake_redis
            assert await _unlock_all_accounts(manager) == 0
        manager.unlock_t1.assert_not_awaited()
