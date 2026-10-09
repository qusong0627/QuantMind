"""探针：同进程内连续多个用例各自开库会话，看事件循环是否复用。"""
from __future__ import annotations
import asyncio
import pytest
from sqlalchemy import text


async def _ping():
    from backend.shared.database_manager_v2 import get_session
    async with get_session(read_only=True) as s:
        return (await s.execute(text("SELECT 1"))).scalar_one()


@pytest.mark.asyncio
async def test_probe_a():
    loop = asyncio.get_running_loop()
    from backend.shared.database_manager_v2 import get_db_manager
    db_manager = get_db_manager()
    await _ping()
    if db_manager._initialized:
        eng = db_manager._master_engine
        print(f"\n[A] loop={id(loop)} engine={id(eng)} pool_size={eng.pool.size()} checkedout={eng.pool.checkedout()}")
    assert True


@pytest.mark.asyncio
async def test_probe_b():
    loop = asyncio.get_running_loop()
    from backend.shared.database_manager_v2 import get_db_manager
    db_manager = get_db_manager()
    await _ping()
    eng = db_manager._master_engine
    print(f"\n[B] loop={id(loop)} engine={id(eng)} pool_size={eng.pool.size()} checkedout={eng.pool.checkedout()}")
    assert True


@pytest.mark.asyncio
async def test_probe_c():
    loop = asyncio.get_running_loop()
    from backend.shared.database_manager_v2 import get_db_manager
    db_manager = get_db_manager()
    await _ping()
    eng = db_manager._master_engine
    print(f"\n[C] loop={id(loop)} engine={id(eng)} pool_size={eng.pool.size()} checkedout={eng.pool.checkedout()}")
    assert True
