"""MCP 管理面数据访问：全局开关、按 key 覆盖、审计（后台管理 + 对外 MCP 配置共用）。

表由 ``upgrade_v1.3.0.sql`` 建；本模块再 ``CREATE TABLE IF NOT EXISTS`` 自愈一次，
避免 API 先于迁移脚本启动时 500（与 eval_contract 的自愈同口径）。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text

from backend.shared.database_manager_v2 import get_session
from backend.shared.mcp_tool_catalog import (
    ALL_API_PERMISSIONS,
    DEFAULT_LIMITS,
    DEFAULT_TOOL_FLAGS,
    LIMIT_SPECS,
)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS qm_mcp_settings (
    id SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    tool_flags JSONB NOT NULL DEFAULT '{}'::jsonb,
    limits JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by TEXT
);
CREATE TABLE IF NOT EXISTS qm_mcp_key_settings (
    access_key TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    tool_flags JSONB NOT NULL DEFAULT '{}'::jsonb,
    limits JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS qm_mcp_audit (
    id BIGSERIAL PRIMARY KEY,
    ts TIMESTAMPTZ NOT NULL DEFAULT now(),
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    access_key_fp TEXT NOT NULL,
    session_id TEXT,
    tool TEXT NOT NULL,
    args_digest TEXT NOT NULL,
    status TEXT NOT NULL,
    error_code TEXT,
    latency_ms INTEGER,
    request_id TEXT,
    client_info TEXT
);
CREATE INDEX IF NOT EXISTS idx_qm_mcp_audit_ts ON qm_mcp_audit (ts DESC);
CREATE INDEX IF NOT EXISTS idx_qm_mcp_audit_key ON qm_mcp_audit (access_key_fp, ts DESC);
"""

_schema_ready = False
_schema_lock = asyncio.Lock()


class McpConfigError(ValueError):
    """可展示给管理员的配置错误。"""


async def ensure_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    async with _schema_lock:
        if _schema_ready:
            return
        async with get_session(read_only=False) as session:
            for statement in _SCHEMA_SQL.split(";"):
                if statement.strip():
                    await session.execute(text(statement))
            await session.commit()
        _schema_ready = True


def _merge_flags(raw: Any) -> dict[str, bool]:
    flags = dict(DEFAULT_TOOL_FLAGS)
    if isinstance(raw, dict):
        for key, value in raw.items():
            if key in flags and isinstance(value, bool):
                flags[key] = value
    return flags


def _merge_limits(raw: Any) -> dict[str, Any]:
    limits = dict(DEFAULT_LIMITS)
    if isinstance(raw, dict):
        for key, (lo, hi, _label) in LIMIT_SPECS.items():
            if key not in raw:
                continue
            try:
                value = float(raw[key])
            except (TypeError, ValueError):
                continue
            if key in {"calls_per_minute", "task_submits_per_day", "sim_orders_per_day", "query_max_rows", "audit_retention_days"}:
                value = int(value)
            limits[key] = min(hi, max(lo, value))
    return limits


def validate_permissions(permissions: list[str]) -> list[str]:
    unknown = sorted(set(permissions) - ALL_API_PERMISSIONS)
    if unknown:
        raise McpConfigError(f"未知权限码：{', '.join(unknown)}")
    return list(dict.fromkeys(permissions))


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except ValueError:
            return {}
    return {}


async def get_global_config() -> dict[str, Any]:
    await ensure_schema()
    async with get_session(read_only=False) as session:
        row = (
            await session.execute(
                text(
                    "SELECT enabled, tool_flags, limits, updated_at, updated_by "
                    "FROM qm_mcp_settings WHERE id = 1"
                )
            )
        ).mappings().first()
        if row is None:
            await session.execute(
                text(
                    "INSERT INTO qm_mcp_settings (id, enabled, tool_flags, limits, updated_by) "
                    "VALUES (1, TRUE, '{}'::jsonb, '{}'::jsonb, 'bootstrap') "
                    "ON CONFLICT (id) DO NOTHING"
                )
            )
            await session.commit()
            row = {
                "enabled": True,
                "tool_flags": {},
                "limits": {},
                "updated_at": None,
                "updated_by": "bootstrap",
            }
    return {
        "enabled": bool(row["enabled"]),
        "tool_flags": _merge_flags(_as_dict(row["tool_flags"])),
        "limits": _merge_limits(_as_dict(row["limits"])),
        "updated_at": row["updated_at"].isoformat() if row.get("updated_at") else None,
        "updated_by": row.get("updated_by"),
    }


async def update_global_config(
    *, patch: dict[str, Any], admin_id: str
) -> dict[str, Any]:
    current = await get_global_config()
    enabled = bool(patch.get("enabled", current["enabled"]))
    flags = _merge_flags({**current["tool_flags"], **_as_dict(patch.get("tool_flags"))})
    limits = _merge_limits({**current["limits"], **_as_dict(patch.get("limits"))})
    await ensure_schema()
    async with get_session(read_only=False) as session:
        await session.execute(
            text(
                """
                INSERT INTO qm_mcp_settings (id, enabled, tool_flags, limits, updated_by)
                VALUES (1, :enabled, CAST(:flags AS jsonb), CAST(:limits AS jsonb), :by)
                ON CONFLICT (id) DO UPDATE SET
                    enabled = EXCLUDED.enabled,
                    tool_flags = EXCLUDED.tool_flags,
                    limits = EXCLUDED.limits,
                    updated_at = now(),
                    updated_by = EXCLUDED.updated_by
                """
            ),
            {
                "enabled": enabled,
                "flags": json.dumps(flags),
                "limits": json.dumps(limits),
                "by": admin_id,
            },
        )
        await session.commit()
    return await get_global_config()


async def get_key_settings(access_key: str) -> dict[str, Any] | None:
    await ensure_schema()
    async with get_session(read_only=True) as session:
        row = (
            await session.execute(
                text(
                    "SELECT access_key, tenant_id, user_id, enabled, tool_flags, limits, "
                    "created_at, updated_at FROM qm_mcp_key_settings WHERE access_key = :key"
                ),
                {"key": access_key},
            )
        ).mappings().first()
    if row is None:
        return None
    return {
        "access_key": row["access_key"],
        "tenant_id": row["tenant_id"],
        "user_id": row["user_id"],
        "enabled": bool(row["enabled"]),
        "tool_flags": _as_dict(row["tool_flags"]),
        "limits": _as_dict(row["limits"]),
        "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
        "updated_at": row["updated_at"].isoformat() if row.get("updated_at") else None,
    }


async def upsert_key_settings(
    *,
    access_key: str,
    tenant_id: str,
    user_id: str,
    enabled: bool | None = None,
    tool_flags: dict[str, Any] | None = None,
    limits: dict[str, Any] | None = None,
) -> dict[str, Any]:
    await ensure_schema()
    current = await get_key_settings(access_key) or {}
    async with get_session(read_only=False) as session:
        await session.execute(
            text(
                """
                INSERT INTO qm_mcp_key_settings
                    (access_key, tenant_id, user_id, enabled, tool_flags, limits)
                VALUES
                    (:key, :tenant, :user, :enabled, CAST(:flags AS jsonb), CAST(:limits AS jsonb))
                ON CONFLICT (access_key) DO UPDATE SET
                    enabled = EXCLUDED.enabled,
                    tool_flags = EXCLUDED.tool_flags,
                    limits = EXCLUDED.limits,
                    updated_at = now()
                """
            ),
            {
                "key": access_key,
                "tenant": tenant_id,
                "user": user_id,
                "enabled": bool(enabled if enabled is not None else current.get("enabled", True)),
                "flags": json.dumps(tool_flags or current.get("tool_flags") or {}),
                "limits": json.dumps(limits or current.get("limits") or {}),
            },
        )
        await session.commit()
    result = await get_key_settings(access_key)
    assert result is not None
    return result


def effective_config(
    *,
    global_config: dict[str, Any],
    key_settings: dict[str, Any] | None,
    permissions: list[str],
) -> dict[str, Any]:
    """全局配置 + 单 key 覆盖 → MCP 服务实际使用的配置。"""
    flags = dict(global_config["tool_flags"])
    limits = dict(global_config["limits"])
    if key_settings:
        flags.update(
            {k: v for k, v in _as_dict(key_settings.get("tool_flags")).items() if k in flags and isinstance(v, bool)}
        )
        limits = _merge_limits({**limits, **_as_dict(key_settings.get("limits"))})
    return {
        "enabled": bool(global_config["enabled"] and (key_settings is None or key_settings.get("enabled", True))),
        "tool_flags": flags,
        "limits": limits,
        "permissions": sorted(set(permissions)),
    }


async def list_clients(limit: int = 200) -> list[dict[str, Any]]:
    await ensure_schema()
    async with get_session(read_only=True) as session:
        rows = (
            (
                await session.execute(
                    text(
                        """
                        SELECT a.access_key, a.name, a.user_id, a.tenant_id,
                               a.permissions, a.is_active, a.created_at, a.last_used_at,
                               a.expires_at,
                               k.enabled AS mcp_enabled,
                               k.tool_flags AS mcp_tool_flags,
                               k.limits AS mcp_limits,
                               k.updated_at AS mcp_updated_at
                        FROM api_keys a
                        LEFT JOIN qm_mcp_key_settings k ON k.access_key = a.access_key
                        ORDER BY COALESCE(a.last_used_at, a.created_at) DESC NULLS LAST
                        LIMIT :limit
                        """
                    ),
                    {"limit": max(1, min(limit, 1000))},
                )
            )
            .mappings()
            .all()
        )
    clients: list[dict[str, Any]] = []
    for row in rows:
        permissions = row["permissions"] or []
        if isinstance(permissions, str):
            try:
                permissions = json.loads(permissions)
            except ValueError:
                permissions = []
        key_settings = None
        if row["mcp_enabled"] is not None:
            key_settings = {
                "enabled": bool(row["mcp_enabled"]),
                "tool_flags": _as_dict(row["mcp_tool_flags"]),
                "limits": _as_dict(row["mcp_limits"]),
                "updated_at": row["mcp_updated_at"].isoformat() if row["mcp_updated_at"] else None,
            }
        clients.append(
            {
                "access_key": row["access_key"],
                "access_key_fp": row["access_key"][-6:],
                "name": row["name"] or "",
                "user_id": row["user_id"],
                "tenant_id": row["tenant_id"],
                "permissions": permissions,
                "is_active": bool(row["is_active"]),
                "created_at": row["created_at"].isoformat() if row["created_at"] else None,
                "last_used_at": row["last_used_at"].isoformat() if row["last_used_at"] else None,
                "expires_at": row["expires_at"].isoformat() if row["expires_at"] else None,
                "mcp": key_settings,
            }
        )
    return clients


async def get_audit(
    *,
    limit: int = 100,
    offset: int = 0,
    user_id: str | None = None,
    tool: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    await ensure_schema()
    where = []
    params: dict[str, Any] = {"limit": max(1, min(limit, 500)), "offset": max(0, offset)}
    if user_id:
        where.append("user_id = :user_id")
        params["user_id"] = user_id
    if tool:
        where.append("tool = :tool")
        params["tool"] = tool
    if status:
        where.append("status = :status")
        params["status"] = status
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    async with get_session(read_only=True) as session:
        total = int(
            (
                await session.execute(
                    text(f"SELECT COUNT(*) FROM qm_mcp_audit{clause}"), params
                )
            ).scalar()
            or 0
        )
        rows = (
            (
                await session.execute(
                    text(
                        f"SELECT id, ts, tenant_id, user_id, access_key_fp, session_id, tool, "
                        f"args_digest, status, error_code, latency_ms, request_id, client_info "
                        f"FROM qm_mcp_audit{clause} ORDER BY ts DESC LIMIT :limit OFFSET :offset"
                    ),
                    params,
                )
            )
            .mappings()
            .all()
        )
    return {
        "total": total,
        "items": [
            {
                **dict(row),
                "ts": row["ts"].isoformat() if row.get("ts") else None,
            }
            for row in rows
        ],
    }


async def add_audit(events: list[dict[str, Any]]) -> None:
    if not events:
        return
    await ensure_schema()
    async with get_session(read_only=False) as session:
        for event in events[:100]:
            await session.execute(
                text(
                    """
                    INSERT INTO qm_mcp_audit
                        (tenant_id, user_id, access_key_fp, session_id, tool,
                         args_digest, status, error_code, latency_ms, request_id, client_info)
                    VALUES
                        (:tenant_id, :user_id, :access_key_fp, :session_id, :tool,
                         :args_digest, :status, :error_code, :latency_ms, :request_id, :client_info)
                    """
                ),
                {
                    "tenant_id": str(event.get("tenant_id") or "default"),
                    "user_id": str(event.get("user_id") or ""),
                    "access_key_fp": str(event.get("access_key_fp") or ""),
                    "session_id": event.get("session_id"),
                    "tool": str(event.get("tool") or ""),
                    "args_digest": str(event.get("args_digest") or ""),
                    "status": str(event.get("status") or "ok"),
                    "error_code": event.get("error_code"),
                    "latency_ms": int(event.get("latency_ms") or 0),
                    "request_id": event.get("request_id"),
                    "client_info": str(event.get("client_info") or "")[:500],
                },
            )
        await session.commit()


async def prune_audit(retention_days: int) -> int:
    await ensure_schema()
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(7, int(retention_days)))
    async with get_session(read_only=False) as session:
        result = await session.execute(
            text("DELETE FROM qm_mcp_audit WHERE ts < :cutoff"), {"cutoff": cutoff}
        )
        await session.commit()
        return int(result.rowcount or 0)
