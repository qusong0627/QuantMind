"""同步 PG 引擎唯一事实源（worker 线程/脚本用；与 async 栈 database_manager_v2 并存）。

背景（2026-09-17 实锤）：``backend/shared/database.py`` 的 ``SessionLocal`` 在部分部署下
绑到 async URL（asyncpg 方言），worker 线程里一用即
``greenlet_spawn has not been called``。常驻同步服务（识别引擎/告警）必须用**显式
psycopg2 URL**。本模块把该解析收敛为单一实现（原 alert_service._resolve_db_url 逻辑），
供识别引擎 / anomaly 契约 / 数据质量告警共用。
"""

from __future__ import annotations

import os
from typing import Any

_engine: Any = None


def resolve_sync_db_url() -> str:
    """同步（psycopg2）数据库 URL：DATABASE_URL 归一（asyncpg→psycopg2）> DB_* 拼装。"""
    raw = os.getenv("DATABASE_URL", "").strip()
    if raw:
        if "asyncpg" in raw:
            return raw.replace("asyncpg", "psycopg2")
        if raw.startswith("postgresql://"):
            return raw.replace("postgresql://", "postgresql+psycopg2://", 1)
        return raw
    from urllib.parse import quote_plus as _q

    host = os.getenv("DB_HOST") or os.getenv("DB_MASTER_HOST", "quantmind-db")
    port = os.getenv("DB_PORT") or os.getenv("DB_MASTER_PORT", "5432")
    user = os.getenv("DB_USER", "quantmind")
    pwd = _q(os.getenv("DB_PASSWORD", "quantmind"))
    name = os.getenv("DB_NAME", "quantmind")
    return f"postgresql+psycopg2://{user}:{pwd}@{host}:{port}/{name}"


def get_sync_engine():
    """进程内缓存的同步引擎（pool_pre_ping；线程安全由连接池保证）。"""
    global _engine
    if _engine is None:
        from sqlalchemy import create_engine

        _engine = create_engine(resolve_sync_db_url(), pool_pre_ping=True, pool_size=3, max_overflow=5)
    return _engine


def sync_session():
    """同步会话上下文：``with sync_session() as s: ...``（退出自动 close）。"""
    from sqlalchemy.orm import Session

    return Session(get_sync_engine())
