"""推理锁的 CAS 释放在**生产用的包装客户端**上必须真的可用。

背景（实测 2026-09-18 celery worker 日志）：

    [InferenceScriptRunner] 释放推理锁失败（将由 TTL 过期）:
        'RedisSentinelClient' object has no attribute 'eval'
    [InferenceLock] 释放锁失败（TTL 兜底）: 同上

`backend/shared/inference_lock.py` 的 `release()` 用 Lua 做「值 == token 才 DEL」
的属主校验 —— T-P1-02 修的正是裸 `delete` 会误删他人锁。但线上传进去的是
`RedisSentinelClient` 包装类：它透出了 set/get/delete/publish/pipeline……唯独没有
`eval`，于是**每一次**释放都在 `finally` 里抛 AttributeError 被吞成一条 warning，
锁只能等 1 小时 TTL 自然过期。影响面是同一模型同一天的重复触发：重跑、补跑、
手动复审在锁存续期内一律拿到 `LOCK_HELD: 同日同模型全市场推理已在执行`。

`test_inference_lock.py` 里的 CAS 用例一直是绿的，因为它传的是**原生 redis 客户端**
（有 `eval`），压根没经过包装类。缺的就是这一环 —— 所以这里显式走包装类。
"""

import os

import pytest

from backend.shared.inference_lock import acquire, release


def _wrapper_redis():
    """真实 Redis 上的包装客户端（db=15 测试专用库，与业务隔离）。"""
    import redis as _redis

    from backend.shared.redis_sentinel_client import (
        RedisSentinelClient,
        RedisSentinelConfig,
    )

    try:
        probe = _redis.Redis(
            host=os.getenv("REDIS_HOST", "127.0.0.1"),
            port=int(os.getenv("REDIS_PORT", "6379")),
            db=15,
            socket_connect_timeout=2,
            socket_timeout=3,
        )
        probe.ping()
        probe.close()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Redis 不可用: {exc}")

    cfg = RedisSentinelConfig()
    cfg.use_sentinel = False  # 直连，避免测试依赖哨兵拓扑
    cfg.db = 15
    return RedisSentinelClient(cfg)


class TestSentinelClientExposesEval:
    def test_wrapper_has_eval(self):
        # 这条是整个文件的重点：包装类少透出一个方法，就让上层语义静默降级
        from backend.shared.redis_sentinel_client import RedisSentinelClient

        assert callable(getattr(RedisSentinelClient, "eval", None)), (
            "RedisSentinelClient 必须透出 eval —— inference_lock.release() 的 CAS "
            "释放依赖它；缺失时锁只能等 TTL 过期"
        )

    def test_inference_lock_still_uses_lua_cas(self):
        # 防「修」成客户端 GET+DEL：那会把属主校验退化成检查-使用竞态
        import backend.shared.inference_lock as il

        assert il._RELEASE_LUA.strip().startswith("local current"), "CAS 释放必须留在 Lua 里"
        assert "redis.call(\"DEL\"" in il._RELEASE_LUA


class TestReleaseThroughWrapper:
    def test_wrong_token_does_not_delete(self):
        # Arrange
        client = _wrapper_redis()
        key = "qm:lock:inference:daily:test:wrapper-cas"
        client.delete(key)

        # Act / Assert
        try:
            token = acquire(client, key, ttl_seconds=60)
            assert token, "首次获取应成功"

            assert release(client, key, "wrong-token") is False
            assert client.get(key) == token.encode(), "错误 token 不得删掉他人锁"

            # 正确 token 才放行 —— 这一步在修复前恒抛 AttributeError
            assert release(client, key, token) is True
            assert client.get(key) is None
        finally:
            client.delete(key)

    def test_release_is_idempotent_when_key_gone(self):
        client = _wrapper_redis()
        key = "qm:lock:inference:daily:test:wrapper-gone"
        client.delete(key)
        try:
            token = acquire(client, key, ttl_seconds=60)
            client.delete(key)  # 模拟 TTL 到期/他人清理
            assert release(client, key, token or "x") is False
        finally:
            client.delete(key)
