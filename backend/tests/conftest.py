"""引擎域单元测试的公共隔离。

**可用内存闸的默认替身**（2026-10-13）：``launcher.running_capacity()`` 现在会按
**当前可用内存**收敛并发路数（内存吃紧 → 1 路串行，见
``hw_lock.advisory_running_capacity``）。若让单测读真机 ``/proc/meminfo``，同一份
代码在开发机（内存宽裕）与 CI（内存窄）上会得到不同并发上限，断言随宿主漂移——
单测必须在任何机器上都确定性通过。

所以这里默认把可用内存钉成「宽裕」，让容量断言只反映**配置**。要测收敛行为
本身的用例，自行 ``monkeypatch.setattr(hw_lock, "probe_available_memory_gb", ...)``
覆盖这个替身（用例级 patch 在 autouse fixture 之后生效）。

**arena 文件面落点隔离**（2026-10-14）：决策轮现在会把每轮的提示词/模型原文
append 进 ``<arena 根>/agent_data_astock/{agent}/log/{日}/log.jsonl``（见
``agent_arena/arena_files.py``）。单测里的 agent 名是 ``fake-model`` 之类的替身，
不隔离就会在**真数据根**里长出一批幽灵 agent，且 arena 前端会当真显示它们。
所以整个会话默认把根指到临时目录。

要往真根上写点东西的用例（不该有）自己设 ``QM_ARENA_DATA_ROOT``——用例级
``monkeypatch.setenv`` 在 autouse 之后生效，能覆盖这条；``test_arena_files.py``
的 ``arena_root`` 夹具就是这么换根的。
"""

from __future__ import annotations

import pytest

#: 「内存宽裕」替身值（GB）：大于任何用例配置的 路数 × 单任务 + 预留。
_PLENTIFUL_MEMORY_GB = 512.0


@pytest.fixture(autouse=True)
def _stub_available_memory(monkeypatch):
    from backend.services.engine.alpha_agent import hw_lock

    monkeypatch.setattr(
        hw_lock, "probe_available_memory_gb", lambda: _PLENTIFUL_MEMORY_GB
    )


@pytest.fixture(autouse=True)
def _sandbox_arena_data_root(tmp_path, monkeypatch):
    """把 arena 数据根钉到本用例的临时目录（理由见模块 docstring）。"""
    monkeypatch.setenv("QM_ARENA_DATA_ROOT", str(tmp_path))


@pytest.fixture(autouse=True)
def _sandbox_trading_policy(tmp_path, monkeypatch):
    """交易策略文件指到临时空路径（阶段 1，2026-10-28）。

    生产机 ``config/trading_policy.json`` 是**按机器/账户**的实盘策略（本机 3×¥20 万）；
    单测若读到它，会随部署环境漂移（同一份代码两个答案）。要测策略文件的用例在本用例
    内 ``monkeypatch.setenv('QM_TRADING_POLICY_FILE', ...)`` 覆盖（夹具先跑）。
    """
    from backend.shared.trading_policy import clear_cache

    monkeypatch.setenv(
        "QM_TRADING_POLICY_FILE", str(tmp_path / "no-trading-policy.json")
    )
    clear_cache()
    yield
    clear_cache()
