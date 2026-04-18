"""
Unified SQLAlchemy schema registry for backend/services.

目标：
1. 统一维护各服务 SQLAlchemy metadata 的注册入口；
2. 为建表、巡检、测试提供一致的 schema 发现机制；
3. 减少在各模块/测试里散落的 Base.metadata 手工管理。
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from typing import Dict, List
from collections.abc import Iterable, Sequence

from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import AsyncEngine


@dataclass(frozen=True)
class SchemaSpec:
    key: str
    service: str
    base_module: str
    base_attr: str = "Base"
    bootstrap_modules: Sequence[str] = ()


@dataclass(frozen=True)
class LoadedSchema:
    key: str
    service: str
    metadata: MetaData


SCHEMA_SPECS: tuple[SchemaSpec, ...] = (
    SchemaSpec(
        key="api.community",
        service="quantmind-api",
        base_module="backend.services.api.community_app.db",
        bootstrap_modules=(
            "backend.services.api.community_app.models.community",
            "backend.services.api.community_app.models.audit",
        ),
    ),
    SchemaSpec(
        key="api.user",
        service="quantmind-api",
        base_module="backend.services.api.user_app.models.user",
        bootstrap_modules=(
            "backend.services.api.user_app.models.api_key",
            "backend.services.api.user_app.models.subscription",
            "backend.services.api.user_app.models.oauth",
            "backend.services.api.user_app.models.payment",
            "backend.services.api.user_app.models.sms",
            "backend.services.api.user_app.models.notification",
            "backend.services.api.user_app.models.kyc",
            "backend.services.api.user_app.models.rbac",
        ),
    ),
    SchemaSpec(
        key="trade.core",
        service="quantmind-trade",
        base_module="backend.services.trade.models.base",
        bootstrap_modules=(
            "backend.services.trade.models.order",
            "backend.services.trade.models.trade",
            "backend.services.trade.models.risk_rule",
            "backend.services.trade.models.preflight_snapshot",
            "backend.services.trade.models.real_account_snapshot",
            "backend.services.trade.models.qmt_agent_binding",
            "backend.services.trade.models.qmt_agent_session",
        ),
    ),
    SchemaSpec(
        key="trade.portfolio",
        service="quantmind-trade",
        base_module="backend.services.trade.portfolio.models",
    ),
    SchemaSpec(
        key="trade.simulation",
        service="quantmind-trade",
        base_module="backend.services.trade.simulation.models",
        bootstrap_modules=(
            "backend.services.trade.simulation.models.order",
            "backend.services.trade.simulation.models.trade",
            "backend.services.trade.simulation.models.fund_snapshot",
        ),
    ),
    SchemaSpec(
        key="engine.core",
        service="quantmind-engine",
        base_module="backend.services.engine.models",
        bootstrap_modules=(
            "backend.services.engine.models.market_data",
            "backend.services.engine.models.task",
        ),
    ),
    SchemaSpec(
        key="stream.market",
        service="quantmind-stream",
        base_module="backend.services.stream.market_app.models",
    ),
)


_SCHEMA_SPEC_MAP: dict[str, SchemaSpec] = {spec.key: spec for spec in SCHEMA_SPECS}


def _load_schema(spec: SchemaSpec) -> LoadedSchema:
    for module_path in spec.bootstrap_modules:
        import_module(module_path)
    base_module = import_module(spec.base_module)
    base = getattr(base_module, spec.base_attr)
    metadata = base.metadata
    return LoadedSchema(key=spec.key, service=spec.service, metadata=metadata)


def list_schema_keys() -> list[str]:
    return list(_SCHEMA_SPEC_MAP.keys())


def load_registered_schemas(
    schema_keys: Iterable[str] | None = None,
) -> list[LoadedSchema]:
    keys = list(schema_keys) if schema_keys is not None else list_schema_keys()
    loaded: list[LoadedSchema] = []
    for key in keys:
        spec = _SCHEMA_SPEC_MAP.get(key)
        if spec is None:
            raise KeyError(f"Unknown schema key: {key}")
        loaded.append(_load_schema(spec))
    return loaded


def detect_duplicate_tables(
    schema_keys: Iterable[str] | None = None,
) -> dict[str, list[str]]:
    seen: dict[str, list[str]] = {}
    for schema in load_registered_schemas(schema_keys):
        for table_name in schema.metadata.tables:
            seen.setdefault(table_name, []).append(schema.key)
    return {table: owners for table, owners in seen.items() if len(owners) > 1}


def registry_summary(schema_keys: Iterable[str] | None = None) -> list[dict]:
    rows: list[dict] = []
    for schema in load_registered_schemas(schema_keys):
        rows.append(
            {
                "key": schema.key,
                "service": schema.service,
                "table_count": len(schema.metadata.tables),
                "tables": sorted(schema.metadata.tables.keys()),
            }
        )
    return rows


async def create_registered_tables(
    engine: AsyncEngine,
    schema_keys: Iterable[str] | None = None,
    checkfirst: bool = True,
) -> None:
    """已禁用自动建表，强制使用 quantmind_init.sql"""
    # for schema in load_registered_schemas(schema_keys):
    #     async with engine.begin() as conn:
    #         await conn.run_sync(lambda sync_conn: schema.metadata.create_all(sync_conn, checkfirst=checkfirst))
    pass
