from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import text

from backend.shared.cos_service import get_cos_service
from backend.shared.database_manager_v2 import get_session
from backend.shared.database_pool import get_db

logger = logging.getLogger(__name__)

_ALLOWED_MODEL_STATUSES = {"candidate", "syncing", "ready", "active", "archived", "failed"}
_READY_STATUSES = {"ready", "active"}
_SYSTEM_MODEL_METADATA = {"system_default": True, "readonly": True}


@dataclass
class ResolvedModel:
    effective_model_id: str
    model_source: str
    fallback_used: bool
    fallback_reason: str
    storage_path: str
    model_file: str
    status: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "effective_model_id": self.effective_model_id,
            "model_source": self.model_source,
            "fallback_used": self.fallback_used,
            "fallback_reason": self.fallback_reason,
            "storage_path": self.storage_path,
            "model_file": self.model_file,
            "status": self.status,
        }


class ModelRegistryService:
    def __init__(self) -> None:
        self.user_models_root = Path(os.getenv("USER_MODELS_ROOT", "models/users"))
        self.primary_model_id = os.getenv("PRIMARY_MODEL_ID", "model_qlib")
        self.fallback_model_id = os.getenv("FALLBACK_MODEL_ID", "alpha158")
        self.primary_model_dir = str(os.getenv("MODELS_PRODUCTION", "/app/models/production/model_qlib"))
        self.fallback_model_dir = str(
            os.getenv("MODELS_FALLBACK_PRODUCTION", "/app/models/production/alpha158")
        )
        self.production_models_root = Path(self.primary_model_dir).parent

    async def ensure_tables(self) -> None:
        stmts = [
            """
            CREATE TABLE IF NOT EXISTS qm_user_models (
                tenant_id VARCHAR(64) NOT NULL,
                user_id VARCHAR(64) NOT NULL,
                model_id VARCHAR(128) NOT NULL,
                source_run_id VARCHAR(64),
                status VARCHAR(32) NOT NULL DEFAULT 'candidate',
                storage_path TEXT,
                model_file VARCHAR(255),
                metadata_json JSONB,
                metrics_json JSONB,
                is_default BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                activated_at TIMESTAMPTZ,
                PRIMARY KEY (tenant_id, user_id, model_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS qm_strategy_model_bindings (
                tenant_id VARCHAR(64) NOT NULL,
                user_id VARCHAR(64) NOT NULL,
                strategy_id VARCHAR(128) NOT NULL,
                model_id VARCHAR(128) NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (tenant_id, user_id, strategy_id)
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_qm_user_models_user_status
            ON qm_user_models (tenant_id, user_id, status, updated_at DESC)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_qm_strategy_model_bindings_model
            ON qm_strategy_model_bindings (tenant_id, user_id, model_id)
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_qm_user_models_default_per_user
            ON qm_user_models (tenant_id, user_id)
            WHERE is_default = TRUE
            """,
        ]
        async with get_session() as session:
            for stmt in stmts:
                await session.execute(text(stmt))

    @staticmethod
    def _normalize_owner(*, tenant_id: str, user_id: str) -> tuple[str, str]:
        tenant = str(tenant_id or "default").strip() or "default"
        user = str(user_id or "").strip()
        if not user:
            raise ValueError("user_id is required")
        return tenant, user

    @staticmethod
    def _parse_json_field(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            try:
                payload = json.loads(value)
                if isinstance(payload, dict):
                    return payload
            except Exception:
                return {}
        return {}

    def _row_to_model(self, row: dict[str, Any]) -> dict[str, Any]:
        metadata_json = self._parse_json_field(row.get("metadata_json"))
        metrics_json = self._parse_json_field(row.get("metrics_json"))
        return {
            "tenant_id": str(row.get("tenant_id") or "default"),
            "user_id": str(row.get("user_id") or ""),
            "model_id": str(row.get("model_id") or ""),
            "source_run_id": str(row.get("source_run_id") or ""),
            "status": str(row.get("status") or ""),
            "storage_path": str(row.get("storage_path") or ""),
            "model_file": str(row.get("model_file") or ""),
            "metadata_json": metadata_json,
            "metrics_json": metrics_json,
            "is_default": bool(row.get("is_default")),
            "created_at": row.get("created_at").isoformat() if row.get("created_at") else None,
            "updated_at": row.get("updated_at").isoformat() if row.get("updated_at") else None,
            "activated_at": row.get("activated_at").isoformat() if row.get("activated_at") else None,
        }

    def _find_system_model_file(self, dir_path: Path, metadata: dict[str, Any] | None = None) -> str:
        candidates: list[str] = []
        meta = metadata if isinstance(metadata, dict) else {}
        files = meta.get("files") if isinstance(meta.get("files"), dict) else {}
        if isinstance(files, dict):
            checkpoint = files.get("model_checkpoint") or files.get("model_file") or files.get("checkpoint")
            if isinstance(checkpoint, str) and checkpoint.strip():
                candidates.append(checkpoint.strip())
        for key in ("model_file", "model_checkpoint", "checkpoint"):
            value = meta.get(key)
            if isinstance(value, str) and value.strip():
                candidates.append(value.strip())
        for ext in ("bin", "txt", "pkl", "pth", "onnx", "pt", "lgb"):
            candidates.append(f"model.{ext}")
        for name in candidates:
            if (dir_path / name).is_file():
                return name
        return candidates[0] if candidates else "model.bin"

    async def _resolve_system_model_record(self, explicit_id: str) -> dict[str, Any] | None:
        raw = str(explicit_id or "").strip()
        if not raw:
            return None
        if raw.startswith("sys-"):
            raw = raw[4:]
        dir_path = self.production_models_root / raw
        if not dir_path.exists() or not dir_path.is_dir():
            return None

        meta_path = dir_path / "metadata.json"
        metadata: dict[str, Any] = {}
        if meta_path.is_file():
            try:
                metadata = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                metadata = {}
        files = metadata.get("files") if isinstance(metadata.get("files"), dict) else {}
        display_name = ""
        model_info = metadata.get("model_info") if isinstance(metadata.get("model_info"), dict) else {}
        if isinstance(model_info, dict):
            display_name = str(model_info.get("name") or model_info.get("display_name") or "").strip()
        if not display_name:
            display_name = str(metadata.get("display_name") or raw)

        if raw == Path(self.primary_model_dir).name:
            canonical_model_id = self.primary_model_id
        elif raw == Path(self.fallback_model_dir).name:
            canonical_model_id = self.fallback_model_id
        else:
            canonical_model_id = f"sys-{raw}"

        return {
            "model_id": canonical_model_id,
            "dir_name": raw,
            "tenant_id": "system",
            "user_id": "system",
            "status": "active",
            "storage_path": str(dir_path),
            "model_file": self._find_system_model_file(dir_path, metadata),
            "display_name": display_name,
            "metadata_json": {
                "display_name": display_name,
                "model_type": metadata.get("model_type") or metadata.get("framework") or "",
                "feature_count": metadata.get("feature_count"),
                "features": metadata.get("feature_columns", []),
                "performance_metrics": metadata.get("performance_metrics", {}),
                "train_start": metadata.get("train_start"),
                "train_end": metadata.get("train_end"),
                "valid_start": metadata.get("valid_start"),
                "valid_end": metadata.get("valid_end"),
                "test_start": metadata.get("test_start"),
                "test_end": metadata.get("test_end"),
            },
            "metrics_json": metadata.get("performance_metrics", {}),
        }

    async def _materialize_system_model_record(
        self,
        *,
        tenant_id: str,
        user_id: str,
        system_record: dict[str, Any],
        is_default: bool = False,
        activated_at: datetime | None = None,
    ) -> dict[str, Any]:
        tenant, user = self._normalize_owner(tenant_id=tenant_id, user_id=user_id)
        now = datetime.now(timezone.utc)
        model_id = str(system_record.get("model_id") or "").strip()
        if not model_id:
            raise ValueError("system model id is required")

        metadata_json = dict(system_record.get("metadata_json") or {})
        metadata_json = {
            **metadata_json,
            "system_default": True,
            "readonly": True,
        }
        metrics_json = system_record.get("metrics_json") or {}

        async with get_session() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO qm_user_models (
                        tenant_id, user_id, model_id, source_run_id, status, storage_path, model_file,
                        metadata_json, metrics_json, is_default, created_at, updated_at, activated_at
                    ) VALUES (
                        :tenant_id, :user_id, :model_id, NULL, :status, :storage_path, :model_file,
                        CAST(:metadata_json AS JSONB), CAST(:metrics_json AS JSONB), :is_default,
                        :created_at, :updated_at, :activated_at
                    )
                    ON CONFLICT (tenant_id, user_id, model_id)
                    DO UPDATE SET
                        status = EXCLUDED.status,
                        storage_path = EXCLUDED.storage_path,
                        model_file = EXCLUDED.model_file,
                        metadata_json = EXCLUDED.metadata_json,
                        metrics_json = EXCLUDED.metrics_json,
                        is_default = EXCLUDED.is_default,
                        activated_at = EXCLUDED.activated_at,
                        updated_at = EXCLUDED.updated_at
                    """
                ),
                {
                    "tenant_id": tenant,
                    "user_id": user,
                    "model_id": model_id,
                    "status": str(system_record.get("status") or "active"),
                    "storage_path": str(system_record.get("storage_path") or ""),
                    "model_file": str(system_record.get("model_file") or "model.bin"),
                    "metadata_json": json.dumps(metadata_json, ensure_ascii=False),
                    "metrics_json": json.dumps(metrics_json, ensure_ascii=False),
                    "is_default": bool(is_default),
                    "created_at": now,
                    "updated_at": now,
                    "activated_at": activated_at,
                },
            )

        model = await self.get_model(tenant_id=tenant, user_id=user, model_id=model_id)
        if model is None:
            raise ValueError("system model materialization failed")
        return model

    async def _ensure_system_default_record(self, *, tenant_id: str, user_id: str) -> None:
        tenant, user = self._normalize_owner(tenant_id=tenant_id, user_id=user_id)
        now = datetime.now(timezone.utc)
        async with get_session() as session:
            exists = (
                await session.execute(
                    text(
                        """
                        SELECT 1
                        FROM qm_user_models
                        WHERE tenant_id = :tenant_id AND user_id = :user_id AND model_id = :model_id
                        LIMIT 1
                        """
                    ),
                    {"tenant_id": tenant, "user_id": user, "model_id": self.primary_model_id},
                )
            ).first()
            if exists:
                return

            current_default = (
                await session.execute(
                    text(
                        """
                        SELECT 1
                        FROM qm_user_models
                        WHERE tenant_id = :tenant_id AND user_id = :user_id AND is_default = TRUE
                        LIMIT 1
                        """
                    ),
                    {"tenant_id": tenant, "user_id": user},
                )
            ).first()

            # 优先从 system 记录读取完整 metadata，回退到文件，最后用 stub
            system_row = (
                await session.execute(
                    text(
                        """
                        SELECT metadata_json, metrics_json, storage_path, model_file
                        FROM qm_user_models
                        WHERE tenant_id = :tenant_id AND user_id = 'system' AND model_id = :model_id
                        LIMIT 1
                        """
                    ),
                    {"tenant_id": tenant, "model_id": self.primary_model_id},
                )
            ).first()

            if system_row and system_row[0]:
                rich_metadata = dict(system_row[0])
                rich_metadata.update({"system_default": True, "readonly": True})
                rich_metrics   = dict(system_row[1]) if system_row[1] else {}
                system_storage = system_row[2] or self.primary_model_dir
                system_model_file = system_row[3] or "model.lgb"
            else:
                # 回退：尝试从文件读取
                meta_file = Path(self.primary_model_dir) / "metadata.json"
                if meta_file.exists():
                    try:
                        file_meta = json.loads(meta_file.read_text(encoding="utf-8"))
                        rich_metadata = {**file_meta, "system_default": True, "readonly": True}
                        rich_metrics = file_meta.get("metrics", {})
                    except Exception:
                        rich_metadata = _SYSTEM_MODEL_METADATA.copy()
                        rich_metrics = {}
                else:
                    rich_metadata = _SYSTEM_MODEL_METADATA.copy()
                    rich_metrics = {}
                system_storage = self.primary_model_dir
                system_model_file = "model.lgb"

            await session.execute(
                text(
                    """
                    INSERT INTO qm_user_models (
                        tenant_id, user_id, model_id, source_run_id, status, storage_path, model_file,
                        metadata_json, metrics_json, is_default, created_at, updated_at, activated_at
                    ) VALUES (
                        :tenant_id, :user_id, :model_id, NULL, 'active', :storage_path, :model_file,
                        CAST(:metadata_json AS JSONB), CAST(:metrics_json AS JSONB), :is_default,
                        :created_at, :updated_at, :activated_at
                    )
                    """
                ),
                {
                    "tenant_id": tenant,
                    "user_id": user,
                    "model_id": self.primary_model_id,
                    "storage_path": system_storage,
                    "model_file": system_model_file,
                    "metadata_json": json.dumps(rich_metadata, ensure_ascii=False),
                    "metrics_json": json.dumps(rich_metrics, ensure_ascii=False),
                    "is_default": bool(not current_default),
                    "created_at": now,
                    "updated_at": now,
                    "activated_at": now if not current_default else None,
                },
            )

    async def _ensure_fallback_model_record(self, *, tenant_id: str, user_id: str) -> None:
        """确保 fallback 模型（如 alpha158）也被注册到用户模型列表。"""
        tenant, user = self._normalize_owner(tenant_id=tenant_id, user_id=user_id)
        now = datetime.now(timezone.utc)
        async with get_session() as session:
            exists = (
                await session.execute(
                    text(
                        """
                        SELECT 1
                        FROM qm_user_models
                        WHERE tenant_id = :tenant_id AND user_id = :user_id AND model_id = :model_id
                        LIMIT 1
                        """
                    ),
                    {"tenant_id": tenant, "user_id": user, "model_id": self.fallback_model_id},
                )
            ).first()
            if exists:
                return

            # 检查 fallback 模型目录是否存在
            fallback_dir = Path(self.fallback_model_dir)
            if not fallback_dir.exists() or not fallback_dir.is_dir():
                return

            # 读取 metadata.json
            meta_file = fallback_dir / "metadata.json"
            metadata: dict[str, Any] = {}
            if meta_file.exists():
                try:
                    metadata = json.loads(meta_file.read_text(encoding="utf-8"))
                except Exception:
                    metadata = {}

            metadata.update({"system_default": True, "readonly": True})
            metrics = metadata.get("performance_metrics", {})
            model_file = self._find_system_model_file(fallback_dir, metadata)

            await session.execute(
                text(
                    """
                    INSERT INTO qm_user_models (
                        tenant_id, user_id, model_id, source_run_id, status, storage_path, model_file,
                        metadata_json, metrics_json, is_default, created_at, updated_at, activated_at
                    ) VALUES (
                        :tenant_id, :user_id, :model_id, NULL, 'active', :storage_path, :model_file,
                        CAST(:metadata_json AS JSONB), CAST(:metrics_json AS JSONB), FALSE,
                        :created_at, :updated_at, NULL
                    )
                    ON CONFLICT (tenant_id, user_id, model_id) DO NOTHING
                    """
                ),
                {
                    "tenant_id": tenant,
                    "user_id": user,
                    "model_id": self.fallback_model_id,
                    "storage_path": self.fallback_model_dir,
                    "model_file": model_file,
                    "metadata_json": json.dumps(metadata, ensure_ascii=False),
                    "metrics_json": json.dumps(metrics, ensure_ascii=False),
                    "created_at": now,
                    "updated_at": now,
                },
            )

    async def list_models(self, *, tenant_id: str, user_id: str, include_archived: bool = False) -> list[dict[str, Any]]:
        tenant, user = self._normalize_owner(tenant_id=tenant_id, user_id=user_id)
        await self._ensure_system_default_record(tenant_id=tenant, user_id=user)
        # 同时确保 fallback 模型（如 alpha158）也被注册
        await self._ensure_fallback_model_record(tenant_id=tenant, user_id=user)
        where_extra = "" if include_archived else "AND status <> 'archived'"
        async with get_session(read_only=True) as session:
            rows = (
                await session.execute(
                    text(
                        f"""
                        SELECT tenant_id, user_id, model_id, source_run_id, status, storage_path, model_file,
                               metadata_json, metrics_json, is_default, created_at, updated_at, activated_at
                        FROM qm_user_models
                        WHERE tenant_id = :tenant_id AND user_id = :user_id {where_extra}
                        ORDER BY is_default DESC, updated_at DESC, created_at DESC
                        """
                    ),
                    {"tenant_id": tenant, "user_id": user},
                )
            ).mappings().all()
        return [self._row_to_model(dict(row)) for row in rows]

    async def get_model(self, *, tenant_id: str, user_id: str, model_id: str) -> dict[str, Any] | None:
        tenant, user = self._normalize_owner(tenant_id=tenant_id, user_id=user_id)
        await self._ensure_system_default_record(tenant_id=tenant, user_id=user)
        async with get_session(read_only=True) as session:
            row = (
                await session.execute(
                    text(
                        """
                        SELECT tenant_id, user_id, model_id, source_run_id, status, storage_path, model_file,
                               metadata_json, metrics_json, is_default, created_at, updated_at, activated_at
                        FROM qm_user_models
                        WHERE tenant_id = :tenant_id AND user_id = :user_id AND model_id = :model_id
                        LIMIT 1
                        """
                    ),
                    {"tenant_id": tenant, "user_id": user, "model_id": str(model_id)},
                )
            ).mappings().first()
        return self._row_to_model(dict(row)) if row else None

    async def get_default_model(self, *, tenant_id: str, user_id: str) -> dict[str, Any] | None:
        tenant, user = self._normalize_owner(tenant_id=tenant_id, user_id=user_id)
        await self._ensure_system_default_record(tenant_id=tenant, user_id=user)
        async with get_session(read_only=True) as session:
            row = (
                await session.execute(
                    text(
                        """
                        SELECT tenant_id, user_id, model_id, source_run_id, status, storage_path, model_file,
                               metadata_json, metrics_json, is_default, created_at, updated_at, activated_at
                        FROM qm_user_models
                        WHERE tenant_id = :tenant_id AND user_id = :user_id
                          AND is_default = TRUE AND status IN ('ready', 'active')
                        ORDER BY activated_at DESC NULLS LAST, updated_at DESC
                        LIMIT 1
                        """
                    ),
                    {"tenant_id": tenant, "user_id": user},
                )
            ).mappings().first()
        return self._row_to_model(dict(row)) if row else None

    async def set_default_model(self, *, tenant_id: str, user_id: str, model_id: str) -> dict[str, Any]:
        tenant, user = self._normalize_owner(tenant_id=tenant_id, user_id=user_id)
        mid = str(model_id).strip()
        if not mid:
            raise ValueError("model_id is required")

        now = datetime.now(timezone.utc)
        async with get_session() as session:
            target = (
                await session.execute(
                    text(
                        """
                        SELECT model_id, status, metadata_json
                        FROM qm_user_models
                        WHERE tenant_id = :tenant_id AND user_id = :user_id AND model_id = :model_id
                        LIMIT 1
                        """
                    ),
                    {"tenant_id": tenant, "user_id": user, "model_id": mid},
                )
            ).mappings().first()
            if not target:
                system_record = await self._resolve_system_model_record(mid)
                if system_record is not None:
                    await self._materialize_system_model_record(
                        tenant_id=tenant,
                        user_id=user,
                        system_record=system_record,
                        is_default=False,
                        activated_at=None,
                    )
                    # 用 canonical model_id（可能与 mid 不同，如 sys-model_qlib → model_qlib）
                    canonical_mid = str(system_record.get("model_id") or mid)
                    mid = canonical_mid
                    target = (
                        await session.execute(
                            text(
                                """
                                SELECT model_id, status, metadata_json
                                FROM qm_user_models
                                WHERE tenant_id = :tenant_id AND user_id = :user_id AND model_id = :model_id
                                LIMIT 1
                                """
                            ),
                            {"tenant_id": tenant, "user_id": user, "model_id": mid},
                        )
                    ).mappings().first()
            if not target:
                raise ValueError("model not found")
            status = str(target.get("status") or "")
            if status not in _READY_STATUSES:
                raise ValueError("model is not ready")

            await session.execute(
                text(
                    """
                    UPDATE qm_user_models
                    SET is_default = FALSE, updated_at = :updated_at
                    WHERE tenant_id = :tenant_id AND user_id = :user_id AND is_default = TRUE
                    """
                ),
                {"tenant_id": tenant, "user_id": user, "updated_at": now},
            )
            await session.execute(
                text(
                    """
                    UPDATE qm_user_models
                    SET is_default = TRUE, activated_at = :activated_at, updated_at = :updated_at
                    WHERE tenant_id = :tenant_id AND user_id = :user_id AND model_id = :model_id
                    """
                ),
                {
                    "tenant_id": tenant,
                    "user_id": user,
                    "model_id": mid,
                    "activated_at": now,
                    "updated_at": now,
                },
            )

        model = await self.get_model(tenant_id=tenant, user_id=user, model_id=mid)
        if model is None:
            raise ValueError("model not found after update")
        return model

    async def archive_model(self, *, tenant_id: str, user_id: str, model_id: str) -> dict[str, Any]:
        tenant, user = self._normalize_owner(tenant_id=tenant_id, user_id=user_id)
        mid = str(model_id).strip()
        if not mid:
            raise ValueError("model_id is required")

        model = await self.get_model(tenant_id=tenant, user_id=user, model_id=mid)
        if model is None:
            raise ValueError("model not found")
        metadata = model.get("metadata_json") if isinstance(model.get("metadata_json"), dict) else {}
        if bool(metadata.get("readonly")):
            raise ValueError("system model cannot be archived")

        now = datetime.now(timezone.utc)
        async with get_session() as session:
            await session.execute(
                text(
                    """
                    UPDATE qm_user_models
                    SET status = 'archived', is_default = FALSE, updated_at = :updated_at
                    WHERE tenant_id = :tenant_id AND user_id = :user_id AND model_id = :model_id
                    """
                ),
                {"tenant_id": tenant, "user_id": user, "model_id": mid, "updated_at": now},
            )

            default_exists = (
                await session.execute(
                    text(
                        """
                        SELECT model_id
                        FROM qm_user_models
                        WHERE tenant_id = :tenant_id AND user_id = :user_id
                          AND is_default = TRUE AND status IN ('ready', 'active')
                        LIMIT 1
                        """
                    ),
                    {"tenant_id": tenant, "user_id": user},
                )
            ).mappings().first()
            if not default_exists:
                candidate = (
                    await session.execute(
                        text(
                            """
                            SELECT model_id
                            FROM qm_user_models
                            WHERE tenant_id = :tenant_id AND user_id = :user_id
                              AND status IN ('ready', 'active') AND model_id <> :archived_id
                            ORDER BY updated_at DESC
                            LIMIT 1
                            """
                        ),
                        {"tenant_id": tenant, "user_id": user, "archived_id": mid},
                    )
                ).mappings().first()
                if candidate:
                    await session.execute(
                        text(
                            """
                            UPDATE qm_user_models
                            SET is_default = TRUE, activated_at = :activated_at, updated_at = :updated_at
                            WHERE tenant_id = :tenant_id AND user_id = :user_id AND model_id = :model_id
                            """
                        ),
                        {
                            "tenant_id": tenant,
                            "user_id": user,
                            "model_id": str(candidate.get("model_id")),
                            "activated_at": now,
                            "updated_at": now,
                        },
                    )

        archived = await self.get_model(tenant_id=tenant, user_id=user, model_id=mid)
        if archived is None:
            raise ValueError("archive result unavailable")
        return archived

    async def get_strategy_binding(
        self,
        *,
        tenant_id: str,
        user_id: str,
        strategy_id: str,
    ) -> dict[str, Any] | None:
        tenant, user = self._normalize_owner(tenant_id=tenant_id, user_id=user_id)
        sid = str(strategy_id).strip()
        async with get_session(read_only=True) as session:
            row = (
                await session.execute(
                    text(
                        """
                        SELECT b.tenant_id, b.user_id, b.strategy_id, b.model_id, b.updated_at,
                               m.status AS model_status, m.storage_path, m.model_file
                        FROM qm_strategy_model_bindings b
                        LEFT JOIN qm_user_models m
                          ON m.tenant_id = b.tenant_id AND m.user_id = b.user_id AND m.model_id = b.model_id
                        WHERE b.tenant_id = :tenant_id AND b.user_id = :user_id AND b.strategy_id = :strategy_id
                        LIMIT 1
                        """
                    ),
                    {"tenant_id": tenant, "user_id": user, "strategy_id": sid},
                )
            ).mappings().first()
        if not row:
            return None
        return {
            "tenant_id": str(row.get("tenant_id") or tenant),
            "user_id": str(row.get("user_id") or user),
            "strategy_id": str(row.get("strategy_id") or sid),
            "model_id": str(row.get("model_id") or ""),
            "model_status": str(row.get("model_status") or ""),
            "storage_path": str(row.get("storage_path") or ""),
            "model_file": str(row.get("model_file") or ""),
            "updated_at": row.get("updated_at").isoformat() if row.get("updated_at") else None,
        }

    async def set_strategy_binding(
        self,
        *,
        tenant_id: str,
        user_id: str,
        strategy_id: str,
        model_id: str,
    ) -> dict[str, Any]:
        tenant, user = self._normalize_owner(tenant_id=tenant_id, user_id=user_id)
        sid = str(strategy_id).strip()
        mid = str(model_id).strip()
        if not sid:
            raise ValueError("strategy_id is required")
        if not mid:
            raise ValueError("model_id is required")

        model = await self.get_model(tenant_id=tenant, user_id=user, model_id=mid)
        if model is None:
            raise ValueError("model not found")
        if str(model.get("status") or "") not in _READY_STATUSES:
            raise ValueError("model is not ready")

        now = datetime.now(timezone.utc)
        async with get_session() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO qm_strategy_model_bindings
                    (tenant_id, user_id, strategy_id, model_id, updated_at)
                    VALUES (:tenant_id, :user_id, :strategy_id, :model_id, :updated_at)
                    ON CONFLICT (tenant_id, user_id, strategy_id)
                    DO UPDATE SET model_id = EXCLUDED.model_id, updated_at = EXCLUDED.updated_at
                    """
                ),
                {
                    "tenant_id": tenant,
                    "user_id": user,
                    "strategy_id": sid,
                    "model_id": mid,
                    "updated_at": now,
                },
            )

        binding = await self.get_strategy_binding(tenant_id=tenant, user_id=user, strategy_id=sid)
        if binding is None:
            raise ValueError("binding not found after update")
        return binding

    async def delete_strategy_binding(self, *, tenant_id: str, user_id: str, strategy_id: str) -> bool:
        tenant, user = self._normalize_owner(tenant_id=tenant_id, user_id=user_id)
        sid = str(strategy_id).strip()
        async with get_session() as session:
            result = await session.execute(
                text(
                    """
                    DELETE FROM qm_strategy_model_bindings
                    WHERE tenant_id = :tenant_id AND user_id = :user_id AND strategy_id = :strategy_id
                    """
                ),
                {"tenant_id": tenant, "user_id": user, "strategy_id": sid},
            )
            rowcount = int(getattr(result, "rowcount", 0) or 0)
        return rowcount > 0

    async def resolve_effective_model(
        self,
        *,
        tenant_id: str,
        user_id: str,
        strategy_id: str | None = None,
        model_id: str | None = None,
    ) -> ResolvedModel:
        tenant, user = self._normalize_owner(tenant_id=tenant_id, user_id=user_id)
        await self._ensure_system_default_record(tenant_id=tenant, user_id=user)

        reason_parts: list[str] = []

        async def _load_ready(mid: str) -> dict[str, Any] | None:
            item = await self.get_model(tenant_id=tenant, user_id=user, model_id=mid)
            if not item:
                return None
            if str(item.get("status") or "") not in _READY_STATUSES:
                return None
            return item

        explicit_id = str(model_id or "").strip()
        if explicit_id:
            system_record = await self._resolve_system_model_record(explicit_id)
            if system_record:
                return ResolvedModel(
                    effective_model_id=str(system_record.get("model_id") or explicit_id),
                    model_source="explicit_system_model",
                    fallback_used=False,
                    fallback_reason="",
                    storage_path=str(system_record.get("storage_path") or ""),
                    model_file=str(system_record.get("model_file") or ""),
                    status=str(system_record.get("status") or "active"),
                )

            explicit = await _load_ready(explicit_id)
            if explicit:
                return ResolvedModel(
                    effective_model_id=explicit_id,
                    model_source="explicit_model_id",
                    fallback_used=False,
                    fallback_reason="",
                    storage_path=str(explicit.get("storage_path") or ""),
                    model_file=str(explicit.get("model_file") or ""),
                    status=str(explicit.get("status") or "ready"),
                )
            reason_parts.append(f"explicit model_id={explicit_id} not ready")

        sid = str(strategy_id or "").strip()
        if sid:
            binding = await self.get_strategy_binding(tenant_id=tenant, user_id=user, strategy_id=sid)
            if binding:
                binding_model_id = str(binding.get("model_id") or "")
                bound = await _load_ready(binding_model_id)
                if bound:
                    return ResolvedModel(
                        effective_model_id=binding_model_id,
                        model_source="strategy_binding",
                        fallback_used=False,
                        fallback_reason="",
                        storage_path=str(bound.get("storage_path") or ""),
                        model_file=str(bound.get("model_file") or ""),
                        status=str(bound.get("status") or "ready"),
                    )
                reason_parts.append(f"strategy binding model_id={binding_model_id} not ready")

        default = await self.get_default_model(tenant_id=tenant, user_id=user)
        if default:
            default_id = str(default.get("model_id") or "")
            return ResolvedModel(
                effective_model_id=default_id,
                model_source="user_default",
                fallback_used=False,
                fallback_reason="",
                storage_path=str(default.get("storage_path") or ""),
                model_file=str(default.get("model_file") or ""),
                status=str(default.get("status") or "active"),
            )

        fallback_reason = "; ".join(reason_parts).strip()
        if fallback_reason:
            fallback_reason = f"{fallback_reason}; fallback to system model"
        else:
            fallback_reason = "no user model configured, fallback to system model"

        return ResolvedModel(
            effective_model_id=self.primary_model_id,
            model_source="system_fallback",
            fallback_used=True,
            fallback_reason=fallback_reason,
            storage_path=self.primary_model_dir,
            model_file="model.lgb",
            status="active",
        )

    def _ensure_system_default_record_sync(self, *, tenant_id: str, user_id: str) -> None:
        tenant, user = self._normalize_owner(tenant_id=tenant_id, user_id=user_id)
        now = datetime.now(timezone.utc)
        with get_db() as session:
            exists = (
                session.execute(
                    text(
                        """
                        SELECT 1
                        FROM qm_user_models
                        WHERE tenant_id = :tenant_id AND user_id = :user_id AND model_id = :model_id
                        LIMIT 1
                        """
                    ),
                    {"tenant_id": tenant, "user_id": user, "model_id": self.primary_model_id},
                ).first()
            )
            if exists:
                return

            current_default = (
                session.execute(
                    text(
                        """
                        SELECT 1
                        FROM qm_user_models
                        WHERE tenant_id = :tenant_id AND user_id = :user_id AND is_default = TRUE
                        LIMIT 1
                        """
                    ),
                    {"tenant_id": tenant, "user_id": user},
                ).first()
            )

            system_row = (
                session.execute(
                    text(
                        """
                        SELECT metadata_json, metrics_json, storage_path, model_file
                        FROM qm_user_models
                        WHERE tenant_id = :tenant_id AND user_id = 'system' AND model_id = :model_id
                        LIMIT 1
                        """
                    ),
                    {"tenant_id": tenant, "model_id": self.primary_model_id},
                ).first()
            )

            if system_row and system_row[0]:
                rich_metadata = dict(system_row[0])
                rich_metadata.update({"system_default": True, "readonly": True})
                rich_metrics = dict(system_row[1]) if system_row[1] else {}
                system_storage = system_row[2] or self.primary_model_dir
                system_model_file = system_row[3] or "model.lgb"
            else:
                meta_file = Path(self.primary_model_dir) / "metadata.json"
                if meta_file.is_file():
                    try:
                        file_meta = json.loads(meta_file.read_text(encoding="utf-8"))
                        rich_metadata = {**file_meta, "system_default": True, "readonly": True}
                        rich_metrics = file_meta.get("metrics", {})
                    except Exception:
                        rich_metadata = _SYSTEM_MODEL_METADATA.copy()
                        rich_metrics = {}
                else:
                    rich_metadata = _SYSTEM_MODEL_METADATA.copy()
                    rich_metrics = {}
                system_storage = self.primary_model_dir
                system_model_file = "model.lgb"

            session.execute(
                text(
                    """
                    INSERT INTO qm_user_models (
                        tenant_id, user_id, model_id, source_run_id, status, storage_path, model_file,
                        metadata_json, metrics_json, is_default, created_at, updated_at, activated_at
                    ) VALUES (
                        :tenant_id, :user_id, :model_id, NULL, 'active', :storage_path, :model_file,
                        CAST(:metadata_json AS JSONB), CAST(:metrics_json AS JSONB), :is_default,
                        :created_at, :updated_at, :activated_at
                    )
                    ON CONFLICT (tenant_id, user_id, model_id) DO NOTHING
                    """
                ),
                {
                    "tenant_id": tenant,
                    "user_id": user,
                    "model_id": self.primary_model_id,
                    "storage_path": system_storage,
                    "model_file": system_model_file,
                    "metadata_json": json.dumps(rich_metadata, ensure_ascii=False),
                    "metrics_json": json.dumps(rich_metrics, ensure_ascii=False),
                    "is_default": bool(not current_default),
                    "created_at": now,
                    "updated_at": now,
                    "activated_at": now if not current_default else None,
                },
            )

    def _resolve_system_model_record_sync(self, explicit_id: str) -> dict[str, Any] | None:
        raw = str(explicit_id or "").strip()
        if not raw:
            return None
        if raw.startswith("sys-"):
            raw = raw[4:]
        dir_path = self.production_models_root / raw
        if not dir_path.exists() or not dir_path.is_dir():
            return None

        meta_path = dir_path / "metadata.json"
        metadata: dict[str, Any] = {}
        if meta_path.is_file():
            try:
                metadata = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                metadata = {}
        display_name = ""
        model_info = metadata.get("model_info") if isinstance(metadata.get("model_info"), dict) else {}
        if isinstance(model_info, dict):
            display_name = str(model_info.get("name") or model_info.get("display_name") or "").strip()
        if not display_name:
            display_name = str(metadata.get("display_name") or raw)

        if raw == Path(self.primary_model_dir).name:
            canonical_model_id = self.primary_model_id
        elif raw == Path(self.fallback_model_dir).name:
            canonical_model_id = self.fallback_model_id
        else:
            canonical_model_id = f"sys-{raw}"

        return {
            "model_id": canonical_model_id,
            "dir_name": raw,
            "tenant_id": "system",
            "user_id": "system",
            "status": "active",
            "storage_path": str(dir_path),
            "model_file": self._find_system_model_file(dir_path, metadata),
            "display_name": display_name,
            "metadata_json": {
                "display_name": display_name,
                "model_type": metadata.get("model_type") or metadata.get("framework") or "",
                "feature_count": metadata.get("feature_count"),
                "features": metadata.get("feature_columns", []),
                "performance_metrics": metadata.get("performance_metrics", {}),
                "train_start": metadata.get("train_start"),
                "train_end": metadata.get("train_end"),
                "valid_start": metadata.get("valid_start"),
                "valid_end": metadata.get("valid_end"),
                "test_start": metadata.get("test_start"),
                "test_end": metadata.get("test_end"),
            },
            "metrics_json": metadata.get("performance_metrics", {}),
        }

    def _get_model_sync(self, *, tenant_id: str, user_id: str, model_id: str) -> dict[str, Any] | None:
        tenant, user = self._normalize_owner(tenant_id=tenant_id, user_id=user_id)
        self._ensure_system_default_record_sync(tenant_id=tenant, user_id=user)
        with get_db() as session:
            row = (
                session.execute(
                    text(
                        """
                        SELECT tenant_id, user_id, model_id, source_run_id, status, storage_path, model_file,
                               metadata_json, metrics_json, is_default, created_at, updated_at, activated_at
                        FROM qm_user_models
                        WHERE tenant_id = :tenant_id AND user_id = :user_id AND model_id = :model_id
                        LIMIT 1
                        """
                    ),
                    {"tenant_id": tenant, "user_id": user, "model_id": str(model_id)},
                ).mappings().first()
            )
        return self._row_to_model(dict(row)) if row else None

    def _get_default_model_sync(self, *, tenant_id: str, user_id: str) -> dict[str, Any] | None:
        tenant, user = self._normalize_owner(tenant_id=tenant_id, user_id=user_id)
        self._ensure_system_default_record_sync(tenant_id=tenant, user_id=user)
        with get_db() as session:
            row = (
                session.execute(
                    text(
                        """
                        SELECT tenant_id, user_id, model_id, source_run_id, status, storage_path, model_file,
                               metadata_json, metrics_json, is_default, created_at, updated_at, activated_at
                        FROM qm_user_models
                        WHERE tenant_id = :tenant_id AND user_id = :user_id
                          AND is_default = TRUE AND status IN ('ready', 'active')
                        ORDER BY activated_at DESC NULLS LAST, updated_at DESC
                        LIMIT 1
                        """
                    ),
                    {"tenant_id": tenant, "user_id": user},
                ).mappings().first()
            )
        return self._row_to_model(dict(row)) if row else None

    def _get_strategy_binding_sync(
        self, *, tenant_id: str, user_id: str, strategy_id: str
    ) -> dict[str, Any] | None:
        tenant, user = self._normalize_owner(tenant_id=tenant_id, user_id=user_id)
        sid = str(strategy_id or "").strip()
        if not sid:
            return None
        with get_db() as session:
            row = (
                session.execute(
                    text(
                        """
                        SELECT tenant_id, user_id, strategy_id, model_id, updated_at
                        FROM qm_strategy_model_bindings
                        WHERE tenant_id = :tenant_id AND user_id = :user_id AND strategy_id = :strategy_id
                        LIMIT 1
                        """
                    ),
                    {"tenant_id": tenant, "user_id": user, "strategy_id": sid},
                ).mappings().first()
            )
        return dict(row) if row else None

    def resolve_effective_model_sync(
        self,
        *,
        tenant_id: str,
        user_id: str,
        strategy_id: str | None = None,
        model_id: str | None = None,
    ) -> dict[str, Any]:
        tenant, user = self._normalize_owner(tenant_id=tenant_id, user_id=user_id)
        self._ensure_system_default_record_sync(tenant_id=tenant, user_id=user)

        reason_parts: list[str] = []

        def _load_ready(mid: str) -> dict[str, Any] | None:
            item = self._get_model_sync(tenant_id=tenant, user_id=user, model_id=mid)
            if not item:
                return None
            if str(item.get("status") or "") not in _READY_STATUSES:
                return None
            return item

        explicit_id = str(model_id or "").strip()
        if explicit_id:
            system_record = self._resolve_system_model_record_sync(explicit_id)
            if system_record:
                return ResolvedModel(
                    effective_model_id=str(system_record.get("model_id") or explicit_id),
                    model_source="explicit_system_model",
                    fallback_used=False,
                    fallback_reason="",
                    storage_path=str(system_record.get("storage_path") or ""),
                    model_file=str(system_record.get("model_file") or ""),
                    status=str(system_record.get("status") or "active"),
                ).to_dict()

            explicit = _load_ready(explicit_id)
            if explicit:
                return ResolvedModel(
                    effective_model_id=explicit_id,
                    model_source="explicit_model_id",
                    fallback_used=False,
                    fallback_reason="",
                    storage_path=str(explicit.get("storage_path") or ""),
                    model_file=str(explicit.get("model_file") or ""),
                    status=str(explicit.get("status") or "ready"),
                ).to_dict()
            reason_parts.append(f"explicit model_id={explicit_id} not ready")

        sid = str(strategy_id or "").strip()
        if sid:
            binding = self._get_strategy_binding_sync(tenant_id=tenant, user_id=user, strategy_id=sid)
            if binding:
                binding_model_id = str(binding.get("model_id") or "")
                bound = _load_ready(binding_model_id)
                if bound:
                    return ResolvedModel(
                        effective_model_id=binding_model_id,
                        model_source="strategy_binding",
                        fallback_used=False,
                        fallback_reason="",
                        storage_path=str(bound.get("storage_path") or ""),
                        model_file=str(bound.get("model_file") or ""),
                        status=str(bound.get("status") or "ready"),
                    ).to_dict()
                reason_parts.append(f"strategy binding model_id={binding_model_id} not ready")

        default = self._get_default_model_sync(tenant_id=tenant, user_id=user)
        if default:
            default_id = str(default.get("model_id") or "")
            return ResolvedModel(
                effective_model_id=default_id,
                model_source="user_default",
                fallback_used=False,
                fallback_reason="",
                storage_path=str(default.get("storage_path") or ""),
                model_file=str(default.get("model_file") or ""),
                status=str(default.get("status") or "active"),
            ).to_dict()

        fallback_reason = "; ".join(reason_parts).strip()
        if fallback_reason:
            fallback_reason = f"{fallback_reason}; fallback to system model"
        else:
            fallback_reason = "no user model configured, fallback to system model"

        return ResolvedModel(
            effective_model_id=self.primary_model_id,
            model_source="system_fallback",
            fallback_used=True,
            fallback_reason=fallback_reason,
            storage_path=self.primary_model_dir,
            model_file="model.lgb",
            status="active",
        ).to_dict()

    @staticmethod
    def build_model_id_from_run(run_id: str) -> str:
        raw = str(run_id or "").strip()
        if not raw:
            raw = datetime.now().strftime("%Y%m%d%H%M%S")
        digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
        normalized = "".join(c if c.isalnum() or c in {"_", "-"} else "_" for c in raw)
        normalized = normalized[:88].strip("_") or "train_run"
        return f"mdl_{normalized}_{digest}"

    async def register_model_from_training_run(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
        request_payload: dict[str, Any],
        result_payload: dict[str, Any],
    ) -> dict[str, Any]:
        tenant, user = self._normalize_owner(tenant_id=tenant_id, user_id=user_id)
        await self.ensure_tables()
        model_id = self.build_model_id_from_run(run_id)
        now = datetime.now(timezone.utc)
        model_dir = self.user_models_root / tenant / user / model_id
        model_dir.mkdir(parents=True, exist_ok=True)

        metrics = result_payload.get("metrics") if isinstance(result_payload.get("metrics"), dict) else {}
        metadata = result_payload.get("metadata") if isinstance(result_payload.get("metadata"), dict) else {}
        metadata = {
            **metadata,
            "display_name": str(
                request_payload.get("display_name")
                or metadata.get("display_name")
                or request_payload.get("job_name")
                or run_id
            ),
            "model_name": str(
                request_payload.get("display_name")
                or metadata.get("model_name")
                or request_payload.get("job_name")
                or run_id
            ),
            "target_horizon_days": request_payload.get("target_horizon_days"),
            "target_mode": request_payload.get("target_mode"),
            "label_formula": request_payload.get("label_formula"),
            "training_window": request_payload.get("training_window"),
        }

        async with get_session() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO qm_user_models (
                        tenant_id, user_id, model_id, source_run_id, status, storage_path, model_file,
                        metadata_json, metrics_json, is_default, created_at, updated_at, activated_at
                    ) VALUES (
                        :tenant_id, :user_id, :model_id, :source_run_id, 'candidate', :storage_path, '',
                        CAST(:metadata_json AS JSONB), CAST(:metrics_json AS JSONB), FALSE,
                        :created_at, :updated_at, NULL
                    )
                    ON CONFLICT (tenant_id, user_id, model_id)
                    DO UPDATE SET
                        source_run_id = EXCLUDED.source_run_id,
                        status = 'candidate',
                        storage_path = EXCLUDED.storage_path,
                        metadata_json = EXCLUDED.metadata_json,
                        metrics_json = EXCLUDED.metrics_json,
                        updated_at = EXCLUDED.updated_at
                    """
                ),
                {
                    "tenant_id": tenant,
                    "user_id": user,
                    "model_id": model_id,
                    "source_run_id": str(run_id),
                    "storage_path": str(model_dir.resolve()),
                    "metadata_json": json.dumps(metadata, ensure_ascii=False),
                    "metrics_json": json.dumps(metrics, ensure_ascii=False),
                    "created_at": now,
                    "updated_at": now,
                },
            )

            await session.execute(
                text(
                    """
                    UPDATE qm_user_models
                    SET status = 'syncing', updated_at = :updated_at
                    WHERE tenant_id = :tenant_id AND user_id = :user_id AND model_id = :model_id
                    """
                ),
                {"tenant_id": tenant, "user_id": user, "model_id": model_id, "updated_at": now},
            )

        sync_status, sync_error, model_file = self._sync_candidate_artifacts(
            run_id=run_id,
            tenant_id=tenant,
            user_id=user,
            model_id=model_id,
            target_dir=model_dir,
        )

        validation_error = self._validate_synced_model(
            target_dir=model_dir,
            model_file=model_file,
            request_payload=request_payload,
        )
        if validation_error and not sync_error:
            sync_error = validation_error
            sync_status = "failed"

        async with get_session() as session:
            if sync_status == "ready":
                has_business_default = (
                    await session.execute(
                        text(
                            """
                            SELECT 1
                            FROM qm_user_models
                            WHERE tenant_id = :tenant_id AND user_id = :user_id
                              AND is_default = TRUE
                              AND COALESCE((metadata_json->>'system_default')::boolean, FALSE) = FALSE
                            LIMIT 1
                            """
                        ),
                        {"tenant_id": tenant, "user_id": user},
                    )
                ).first()
                should_set_default = not bool(has_business_default)
                if should_set_default:
                    await session.execute(
                        text(
                            """
                            UPDATE qm_user_models
                            SET is_default = FALSE, updated_at = :updated_at
                            WHERE tenant_id = :tenant_id AND user_id = :user_id AND is_default = TRUE
                            """
                        ),
                        {"tenant_id": tenant, "user_id": user, "updated_at": now},
                    )

                await session.execute(
                    text(
                        """
                        UPDATE qm_user_models
                        SET status = 'ready',
                            model_file = :model_file,
                            is_default = :is_default,
                            activated_at = CASE WHEN :is_default THEN :activated_at ELSE activated_at END,
                            updated_at = :updated_at
                        WHERE tenant_id = :tenant_id AND user_id = :user_id AND model_id = :model_id
                        """
                    ),
                    {
                        "tenant_id": tenant,
                        "user_id": user,
                        "model_id": model_id,
                        "model_file": model_file,
                        "is_default": bool(should_set_default),
                        "activated_at": now,
                        "updated_at": now,
                    },
                )
            else:
                await session.execute(
                    text(
                        """
                        UPDATE qm_user_models
                        SET status = 'failed', model_file = :model_file, updated_at = :updated_at
                        WHERE tenant_id = :tenant_id AND user_id = :user_id AND model_id = :model_id
                        """
                    ),
                    {
                        "tenant_id": tenant,
                        "user_id": user,
                        "model_id": model_id,
                        "model_file": model_file,
                        "updated_at": now,
                    },
                )

        return {
            "model_id": model_id,
            "status": sync_status,
            "error": sync_error or "",
            "storage_path": str(model_dir.resolve()),
            "model_file": model_file,
        }

    def _sync_candidate_artifacts(
        self,
        *,
        run_id: str,
        tenant_id: str,
        user_id: str,
        model_id: str,
        target_dir: Path,
    ) -> tuple[str, str, str]:
        # 1. 优先检查本地是否已经有训练产出的模型（热路径）
        # 兼容 LocalDockerOrchestrator 的挂载路径
        required = ["model.lgb", "metadata.json"]
        local_exists = all((target_dir / f).exists() for f in required)

        if local_exists:
            logger.info(f"Model {model_id} already exists locally in {target_dir}, skipping COS sync.")
            model_file = "model.lgb"
            return "ready", "", model_file

        # 2. 如果本地没有，则尝试从 COS 同步（原逻辑备份）
        cos = get_cos_service()
        source_prefix = f"models/candidates/{run_id}/"
        required_all = ["model.lgb", "model.txt", "metadata.json", "pred.pkl", "config.yaml", "result.json"]
        copied: list[str] = []

        for filename in required_all:
            key = f"{source_prefix}{filename}"
            try:
                data = cos.get_object_bytes(key)
                if data is None:
                    continue
                dest = target_dir / filename
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
                copied.append(filename)
            except Exception:
                continue

        model_file = ""
        for candidate in ("model.lgb", "model.txt", "model.bin"):
            if (target_dir / candidate).exists():
                model_file = candidate
                break

        if not copied:
            return "failed", "no artifacts found in local or COS path", model_file

        return "ready", "", model_file

    @staticmethod
    def _validate_synced_model(
        *,
        target_dir: Path,
        model_file: str,
        request_payload: dict[str, Any],
    ) -> str:
        if not model_file or not (target_dir / model_file).exists():
            return "model file missing after sync"
        metadata_path = target_dir / "metadata.json"
        if not metadata_path.exists():
            return "metadata.json missing after sync"

        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            if not isinstance(metadata, dict):
                return "metadata.json is not a valid object"
        except Exception as exc:
            return f"metadata.json parse failed: {exc}"

        feature_dim = metadata.get("feature_count")
        if feature_dim is None:
            features = metadata.get("features")
            if isinstance(features, list):
                feature_dim = len(features)
        try:
            if int(feature_dim or 0) <= 0:
                return "metadata feature dimension is missing"
        except Exception:
            return "metadata feature dimension is invalid"

        target_horizon_days = request_payload.get("target_horizon_days")
        target_mode = request_payload.get("target_mode")
        if int(target_horizon_days or 0) <= 0:
            return "target_horizon_days is missing in request payload"
        if str(target_mode or "").strip() == "":
            return "target_mode is missing in request payload"

        return ""


model_registry_service = ModelRegistryService()
