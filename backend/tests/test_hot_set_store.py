"""热集存储单一事实源（``backend/shared/hot_set_store.py``）单元测试。

背景（2026-09-17 盘中事故）：热集此前落**远端公共行情服**——多租户全局键被多实例
交替覆写（实测 502⇄529、单次翻新约 300 只），订阅会话反复重建。本模块把热集收敛到
**部署本地 Redis**；本文件锁定：键解析（env 隔离）、连接参数（本地 host/port/db）、
与远端行情配置**完全无关**（防止回归到远端）。
"""

from __future__ import annotations

import pytest


@pytest.mark.unit
def test_hot_set_key_default_and_env_override(monkeypatch):
    from backend.shared.hot_set_store import hot_set_key

    monkeypatch.delenv("QM_HOT_SET_KEY", raising=False)
    assert hot_set_key() == "qm:hot_set:symbols"

    monkeypatch.setenv("QM_HOT_SET_KEY", "qm:hot_set:test7:symbols")
    assert hot_set_key() == "qm:hot_set:test7:symbols"

    # 空串视为未设置（compose 里常见 `VAR=` 形态）
    monkeypatch.setenv("QM_HOT_SET_KEY", "  ")
    assert hot_set_key() == "qm:hot_set:symbols"


@pytest.mark.unit
def test_make_hot_set_client_targets_deployment_local_redis(monkeypatch):
    """连接参数取部署本地 Redis（REDIS_HOST/PORT/PASSWORD），与远端行情服无关。"""
    from backend.shared.hot_set_store import make_hot_set_client

    monkeypatch.setenv("REDIS_HOST", "redis")
    monkeypatch.setenv("REDIS_PORT", "6379")
    monkeypatch.setenv("REDIS_PASSWORD", "")
    monkeypatch.delenv("QM_HOT_SET_DB", raising=False)
    monkeypatch.delenv("REDIS_DB_GENERAL", raising=False)
    # 即便远端行情服有配置，也不得影响热集客户端
    monkeypatch.setenv("REMOTE_QUOTE_REDIS_HOST", "www.quantmindai.cn")
    monkeypatch.setenv("REMOTE_QUOTE_REDIS_PORT", "6379")

    client = make_hot_set_client()
    try:
        kwargs = client.connection_pool.connection_kwargs
        assert kwargs["host"] == "redis"
        assert kwargs["port"] == 6379
        assert kwargs["db"] == 0
        assert kwargs["password"] is None
    finally:
        client.close()


@pytest.mark.unit
def test_make_hot_set_client_db_resolution_order(monkeypatch):
    """db 解析：QM_HOT_SET_DB > REDIS_DB_GENERAL > 0；非法值回落默认。"""
    from backend.shared.hot_set_store import make_hot_set_client

    monkeypatch.setenv("REDIS_HOST", "redis")
    monkeypatch.setenv("REDIS_DB_GENERAL", "3")
    monkeypatch.delenv("QM_HOT_SET_DB", raising=False)
    c = make_hot_set_client()
    try:
        assert c.connection_pool.connection_kwargs["db"] == 3
    finally:
        c.close()

    monkeypatch.setenv("QM_HOT_SET_DB", "0")
    c = make_hot_set_client()
    try:
        assert c.connection_pool.connection_kwargs["db"] == 0
    finally:
        c.close()

    monkeypatch.setenv("QM_HOT_SET_DB", "not-a-number")
    c = make_hot_set_client()
    try:
        # 非法值沿链回落（REDIS_DB_GENERAL=3），不崩、不静默变 0
        assert c.connection_pool.connection_kwargs["db"] == 3
    finally:
        c.close()


@pytest.mark.unit
def test_tdx_config_hot_set_key_delegates_to_store(monkeypatch):
    """``tdx_aidata.config.hot_set_key`` 委托单一事实源（防止双实现漂移）。"""
    from backend.shared.hot_set_store import hot_set_key as store_key
    from backend.shared.tdx_aidata import config as tdx_config

    monkeypatch.setenv("QM_HOT_SET_KEY", "qm:hot_set:delegation-check")
    assert tdx_config.hot_set_key() == store_key() == "qm:hot_set:delegation-check"


@pytest.mark.integration
def test_read_hot_set_symbols_roundtrip_local_redis(monkeypatch):
    """真机往返：写本地 Redis → read_hot_set_symbols 读回（容器/宿主均可跑）。"""
    import uuid

    import redis as _redis

    from backend.shared.hot_set_store import make_hot_set_client, read_hot_set_symbols

    key = f"qm:hot_set:test-{uuid.uuid4().hex[:8]}"
    monkeypatch.setenv("QM_HOT_SET_KEY", key)
    client = make_hot_set_client()
    try:
        client.ping()
    except Exception as exc:  # noqa: BLE001
        client.close()
        pytest.skip(f"本地 Redis 不可达（如实跳过）: {exc}")
    try:
        client.sadd(key, "600036.SH", "000001.SZ")
        assert read_hot_set_symbols() == ["000001.SZ", "600036.SH"]
    finally:
        try:
            client.delete(key)
        except Exception:  # noqa: BLE001
            pass
        client.close()
