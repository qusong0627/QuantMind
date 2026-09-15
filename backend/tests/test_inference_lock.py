"""T-P1-02 测试：推理单实例锁（token + CAS 释放）+ 信号就绪标记。

覆盖：
1. should_mark_ready 纯函数（partial 不置位 / 门槛 / 达标）；
2. 键构造（锁键/就绪键，market 归一）；
3. 真实 Redis 集成（容器内）：并发抢锁仅一胜者；CAS 释放属主校验；错误 token 不误删；
4. 接线源断言：execute 包装 / 就绪标记两处 / celery 任务 CAS 释放（防回退裸 delete）。
"""

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from backend.shared.inference_lock import (
    DEFAULT_MIN_SYMBOLS,
    LOCK_KEY_PREFIX,
    acquire,
    inference_lock_key,
    ready_key,
    release,
    should_mark_ready,
)

_BACKEND = Path(__file__).resolve().parents[1]


# --- 纯函数 ---------------------------------------------------------------


def test_should_mark_ready_rules():
    assert should_mark_ready(partial=False, symbol_count=5000, min_symbols=1000) is True
    assert should_mark_ready(partial=True, symbol_count=5000, min_symbols=1000) is False
    assert should_mark_ready(partial=False, symbol_count=460, min_symbols=1000) is False
    assert should_mark_ready(partial=False, symbol_count=1000, min_symbols=1000) is True


def test_key_builders():
    key = inference_lock_key("default", "00000001", "mdl_x", "2026-09-16")
    assert key == f"{LOCK_KEY_PREFIX}:default:00000001:mdl_x:2026-09-16"
    assert ready_key("A", "2026-09-16").endswith(":CN:2026-09-16")
    assert ready_key("hk", "2026-09-16").endswith(":HK:2026-09-16")


# --- 真实 Redis 集成（不可用则跳过）-----------------------------------------


def _test_redis():
    try:
        import redis as redis_lib

        client = redis_lib.Redis(
            host=os.getenv("REDIS_HOST", "redis"),
            port=int(os.getenv("REDIS_PORT", "6379")),
            db=15,  # 测试专用库，避免污染业务
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=3,
        )
        client.ping()
        return client
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Redis 不可用: {exc}")


def test_acquire_release_cas_semantics():
    client = _test_redis()
    key = "qm:lock:inference:daily:test:t-p1-02"
    client.delete(key)
    try:
        token1 = acquire(client, key, ttl_seconds=60)
        assert token1, "首次获取应成功"
        assert acquire(client, key, ttl_seconds=60) is None, "被占用应返回 None"

        # 错误 token 不误删（CAS 属主校验——修复裸 delete 的核心语义）
        assert release(client, key, "wrong-token") is False
        assert client.get(key) == token1

        # 正确 token 释放
        assert release(client, key, token1) is True
        assert client.get(key) is None
    finally:
        client.delete(key)


def test_concurrent_acquire_single_winner():
    """并发抢锁：8 线程同键，仅 1 个胜者（修同日多 run 竞态的机制验证）。"""
    client = _test_redis()
    key = "qm:lock:inference:daily:test:t-p1-02-conc"
    client.delete(key)
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            tokens = list(pool.map(lambda _: acquire(client, key, ttl_seconds=60), range(8)))
        winners = [t for t in tokens if t]
        assert len(winners) == 1, f"应仅一个胜者，实际 {len(winners)}"
    finally:
        client.delete(key)


# --- 接线源断言 ------------------------------------------------------------


def test_runner_execute_is_lock_wrapped():
    src = (_BACKEND / "services/engine/inference/script_runner.py").read_text(encoding="utf-8")
    assert "def _execute_impl(" in src
    assert "return self._execute_impl(" in src
    assert "RULE:INFERENCE-LOCK" in src
    assert "LOCK_HELD" in src
    assert src.count("mark_signal_ready_if_full(") == 2, "两个持久化点都应置就绪标记"


def test_celery_task_releases_with_cas():
    src = (_BACKEND / "services/engine/tasks/celery_tasks.py").read_text(encoding="utf-8")
    assert "from backend.shared.inference_lock import release" in src
    assert "redis.delete(lock_key)" not in src, "不得回退为裸 delete（无属主校验）"
    assert "_LOCK_DEGRADED_TOKEN" in src


def test_admin_manual_shares_lock_with_celery_global_scope():
    """Admin 手动触发与 celery 全局任务必须真正互斥（同 scope 键形）。

    此前两端各自实现、键形不同（admin 无 scope），'防并发'注释与事实不符——
    这正是同日双跑的疑因之一。
    """
    src = (_BACKEND / "services/api/routers/admin/model_management.py").read_text(
        encoding="utf-8"
    )
    assert "from backend.shared.inference_lock import" in src
    assert ":default:system:global:" in src, "admin 手动锁必须与 celery 全局任务同 scope"
    assert "_release_inference_lock(redis, lock_key, lock_token)" in src
    assert "redis.delete(lock_key)" not in src


def test_no_local_lock_constant_copies():
    """锁常量唯一实现（禁本地副本——三份副本曾导致键形漂移）。"""
    for rel in (
        "services/api/routers/admin/model_management_utils.py",
        "services/engine/tasks/celery_tasks.py",
    ):
        src = (_BACKEND / rel).read_text(encoding="utf-8")
        assert "_INFERENCE_LOCK_TTL_SEC = " not in src, rel
        assert "_INFERENCE_LOCK_KEY_PREFIX = " not in src, rel


def test_default_min_symbols_sane():
    assert DEFAULT_MIN_SYMBOLS >= 100
