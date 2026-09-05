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

_ALLOWED_MODEL_STATUSES = {
    "candidate",
    "syncing",
    "ready",
    "active",
    "archived",
    "failed",
}
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


_MARKET_ALIASES: dict[str, frozenset[str]] = {
    "CN": frozenset({"CN", "A", "A_SHARE", "A股", "CHINA"}),
    "HK": frozenset({"HK", "HONG_KONG", "港股"}),
    "US": frozenset({"US", "美股"}),
    "CRYPTO": frozenset({"CRYPTO", "加密", "加密货币"}),
    "FUTURES": frozenset({"FUTURES", "期货"}),
}


def _canonical_market(value: Any) -> str:
    """市场标识规范化（缺省/未知一律 CN）。"""
    raw = str(value or "").upper().strip()
    if not raw:
        return "CN"
    for canonical, hits in _MARKET_ALIASES.items():
        if raw in hits:
            return canonical
    return "CN"


def _model_market_of(model: dict[str, Any]) -> str:
    """模型记录 → 市场（metadata_json.market > context.market > model_id mdl_{market}_ 前缀）。"""
    meta = model.get("metadata_json") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:  # noqa: BLE001
            meta = {}
    raw = ""
    if isinstance(meta, dict):
        raw = str(meta.get("market") or "")
        if not raw:
            ctx = meta.get("context")
            if isinstance(ctx, dict):
                raw = str(ctx.get("market") or "")
    if not raw:
        mid = str(model.get("model_id") or "").lower()
        for market in ("hk", "us", "crypto", "futures", "cn"):
            if f"mdl_{market}_" in mid:
                raw = market
                break
    return _canonical_market(raw)


class ModelRegistryService:
    def __init__(self) -> None:
        raw_user_models_root = Path(os.getenv("USER_MODELS_ROOT", "models/users"))
        self.user_models_root = (
            raw_user_models_root
            if raw_user_models_root.is_absolute()
            else Path("/app") / raw_user_models_root
        )
        self.primary_model_id = os.getenv("PRIMARY_MODEL_ID", "")
        self.fallback_model_id = os.getenv("FALLBACK_MODEL_ID", "")
        self.primary_model_dir = str(
            os.getenv("MODELS_PRODUCTION", "/app/models/production")
        )
        self.fallback_model_dir = str(os.getenv("MODELS_FALLBACK_PRODUCTION", ""))
        self.production_models_root = Path(self.primary_model_dir)

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
            """
            DELETE FROM qm_user_models
            WHERE model_id IN ('model_qlib', 'alpha158', 'sys-model_qlib', 'sys-alpha158')
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
            "created_at": row.get("created_at").isoformat()
            if row.get("created_at")
            else None,
            "updated_at": row.get("updated_at").isoformat()
            if row.get("updated_at")
            else None,
            "activated_at": row.get("activated_at").isoformat()
            if row.get("activated_at")
            else None,
        }

    def _find_system_model_file(
        self, dir_path: Path, metadata: dict[str, Any] | None = None
    ) -> str:
        candidates: list[str] = []
        meta = metadata if isinstance(metadata, dict) else {}
        files = meta.get("files") if isinstance(meta.get("files"), dict) else {}
        if isinstance(files, dict):
            checkpoint = (
                files.get("model_checkpoint")
                or files.get("model_file")
                or files.get("checkpoint")
            )
            if isinstance(checkpoint, str) and checkpoint.strip():
                candidates.append(checkpoint.strip())
        for key in ("model_file", "model_checkpoint", "checkpoint"):
            value = meta.get(key)
            if isinstance(value, str) and value.strip():
                candidates.append(value.strip())
        for ext in ("bin", "txt", "pkl", "pth", "onnx", "pt", "lgb", "xgb", "cbm"):
            candidates.append(f"model.{ext}")
        for name in candidates:
            if (dir_path / name).is_file():
                return name
        return candidates[0] if candidates else "model.bin"

    async def _resolve_system_model_record(
        self, explicit_id: str
    ) -> dict[str, Any] | None:
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
        model_info = (
            metadata.get("model_info")
            if isinstance(metadata.get("model_info"), dict)
            else {}
        )
        if isinstance(model_info, dict):
            display_name = str(
                model_info.get("name") or model_info.get("display_name") or ""
            ).strip()
        if not display_name:
            display_name = str(metadata.get("display_name") or raw)

        if raw == Path(self.primary_model_dir).name:
            canonical_model_id = self.primary_model_id
        elif raw == Path(self.fallback_model_dir).name:
            canonical_model_id = self.fallback_model_id
        else:
            canonical_model_id = f"sys-{raw}"

        context = (
            metadata.get("context") if isinstance(metadata.get("context"), dict) else {}
        )
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
                "model_type": metadata.get("model_type")
                or metadata.get("framework")
                or "",
                "feature_count": metadata.get("feature_count"),
                "features": metadata.get("feature_columns", []),
                "performance_metrics": metadata.get("performance_metrics", {}),
                "context": context,
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

    async def _ensure_system_default_record(
        self, *, tenant_id: str, user_id: str
    ) -> None:
        # OSS 部署未配置内置生产模型时 PRIMARY_MODEL_ID 为空；不能将空
        # 字符串注册为模型，否则用户每次登录都会看到一条无名称的“模型”。
        if not self.primary_model_id.strip():
            return
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
                    {
                        "tenant_id": tenant,
                        "user_id": user,
                        "model_id": self.primary_model_id,
                    },
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
                rich_metrics = dict(system_row[1]) if system_row[1] else {}
                system_storage = system_row[2] or self.primary_model_dir
                system_model_file = system_row[3] or "model.lgb"
            else:
                # 回退：尝试从文件读取
                meta_file = Path(self.primary_model_dir) / "metadata.json"
                if meta_file.exists():
                    try:
                        file_meta = json.loads(meta_file.read_text(encoding="utf-8"))
                        rich_metadata = {
                            **file_meta,
                            "system_default": True,
                            "readonly": True,
                        }
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

    async def _ensure_fallback_model_record(
        self, *, tenant_id: str, user_id: str
    ) -> None:
        """确保 fallback 模型（如 alpha158）也被注册到用户模型列表。"""
        if not self.fallback_model_id.strip():
            return
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
                    {
                        "tenant_id": tenant,
                        "user_id": user,
                        "model_id": self.fallback_model_id,
                    },
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

    async def list_models(
        self,
        *,
        tenant_id: str,
        user_id: str,
        include_archived: bool = False,
        market: str | None = None,
    ) -> list[dict[str, Any]]:
        tenant, user = self._normalize_owner(tenant_id=tenant_id, user_id=user_id)
        where_extra = "" if include_archived else "AND status <> 'archived'"
        params: dict[str, Any] = {"tenant_id": tenant, "user_id": user}
        if market:
            market_upper = str(market).upper().strip()
            where_extra += " AND COALESCE(metadata_json->>'market', 'CN') = :market"
            params["market"] = market_upper
        async with get_session(read_only=True) as session:
            rows = (
                (
                    await session.execute(
                        text(
                            f"""
                        SELECT tenant_id, user_id, model_id, source_run_id, status, storage_path, model_file,
                               metadata_json, metrics_json, is_default, created_at, updated_at, activated_at
                        FROM qm_user_models
                        WHERE tenant_id = :tenant_id AND user_id = :user_id
                          AND BTRIM(COALESCE(model_id, '')) <> '' {where_extra}
                        ORDER BY is_default DESC, updated_at DESC, created_at DESC
                        """
                        ),
                        params,
                    )
                )
                .mappings()
                .all()
            )
        return [self._row_to_model(dict(row)) for row in rows]

    async def get_model(
        self, *, tenant_id: str, user_id: str, model_id: str
    ) -> dict[str, Any] | None:
        tenant, user = self._normalize_owner(tenant_id=tenant_id, user_id=user_id)
        async with get_session(read_only=True) as session:
            row = (
                (
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
                        {
                            "tenant_id": tenant,
                            "user_id": user,
                            "model_id": str(model_id),
                        },
                    )
                )
                .mappings()
                .first()
            )
        return self._row_to_model(dict(row)) if row else None

    async def get_default_model(
        self, *, tenant_id: str, user_id: str, market: str | None = None
    ) -> dict[str, Any] | None:
        tenant, user = self._normalize_owner(tenant_id=tenant_id, user_id=user_id)
        market_clause = ""
        params: dict[str, Any] = {"tenant_id": tenant, "user_id": user}
        if market:
            market_clause = " AND COALESCE(metadata_json->>'market', 'CN') = :market"
            params["market"] = str(market).upper().strip()
        async with get_session(read_only=True) as session:
            row = (
                (
                    await session.execute(
                        text(
                            f"""
                        SELECT tenant_id, user_id, model_id, source_run_id, status, storage_path, model_file,
                               metadata_json, metrics_json, is_default, created_at, updated_at, activated_at
                        FROM qm_user_models
                        WHERE tenant_id = :tenant_id AND user_id = :user_id
                          AND is_default = TRUE AND status IN ('ready', 'active'){market_clause}
                        ORDER BY activated_at DESC NULLS LAST, updated_at DESC
                        LIMIT 1
                        """
                        ),
                        params,
                    )
                )
                .mappings()
                .first()
            )
        return self._row_to_model(dict(row)) if row else None

    async def set_default_model(
        self, *, tenant_id: str, user_id: str, model_id: str
    ) -> dict[str, Any]:
        tenant, user = self._normalize_owner(tenant_id=tenant_id, user_id=user_id)
        mid = str(model_id).strip()
        if not mid:
            raise ValueError("model_id is required")

        now = datetime.now(timezone.utc)
        async with get_session() as session:
            target = (
                (
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
                )
                .mappings()
                .first()
            )
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
                        (
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
                        )
                        .mappings()
                        .first()
                    )
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

            # 清除 system_default 标记，表明这是用户主动设置的默认模型
            target_metadata = target.get("metadata_json") or {}
            if isinstance(target_metadata, str):
                try:
                    target_metadata = json.loads(target_metadata)
                except Exception:
                    target_metadata = {}
            elif not isinstance(target_metadata, dict):
                target_metadata = {}
            cleaned_metadata = {
                k: v
                for k, v in target_metadata.items()
                if k not in ("system_default", "readonly")
            }

            await session.execute(
                text(
                    """
                    UPDATE qm_user_models
                    SET is_default = TRUE, activated_at = :activated_at, updated_at = :updated_at,
                        metadata_json = CAST(:metadata_json AS JSONB)
                    WHERE tenant_id = :tenant_id AND user_id = :user_id AND model_id = :model_id
                    """
                ),
                {
                    "tenant_id": tenant,
                    "user_id": user,
                    "model_id": mid,
                    "activated_at": now,
                    "updated_at": now,
                    "metadata_json": json.dumps(cleaned_metadata, ensure_ascii=False),
                },
            )

        model = await self.get_model(tenant_id=tenant, user_id=user, model_id=mid)
        if model is None:
            raise ValueError("model not found after update")
        return model

    async def archive_model(
        self, *, tenant_id: str, user_id: str, model_id: str
    ) -> dict[str, Any]:
        tenant, user = self._normalize_owner(tenant_id=tenant_id, user_id=user_id)
        mid = str(model_id).strip()
        if not mid:
            raise ValueError("model_id is required")

        model = await self.get_model(tenant_id=tenant, user_id=user, model_id=mid)
        if model is None:
            raise ValueError("model not found")
        metadata = (
            model.get("metadata_json")
            if isinstance(model.get("metadata_json"), dict)
            else {}
        )
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
                {
                    "tenant_id": tenant,
                    "user_id": user,
                    "model_id": mid,
                    "updated_at": now,
                },
            )

            default_exists = (
                (
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
                )
                .mappings()
                .first()
            )
            if not default_exists:
                candidate = (
                    (
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
                    )
                    .mappings()
                    .first()
                )
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

    async def activate_model(
        self, *, tenant_id: str, user_id: str, model_id: str
    ) -> dict[str, Any]:
        """手动激活 candidate 模型 → ready（软门禁触发的模型走此入口）。

        仅 candidate 可激活；激活后保留 quality_warnings（供展示），用户可设默认。
        """
        tenant, user = self._normalize_owner(tenant_id=tenant_id, user_id=user_id)
        mid = str(model_id).strip()
        if not mid:
            raise ValueError("model_id is required")

        model = await self.get_model(tenant_id=tenant, user_id=user, model_id=mid)
        if model is None:
            raise ValueError("model not found")
        if model.get("status") != "candidate":
            raise ValueError(
                f"only candidate models can be activated, current status: {model.get('status')}"
            )

        now = datetime.now(timezone.utc)
        async with get_session() as session:
            await session.execute(
                text(
                    """
                    UPDATE qm_user_models
                    SET status = 'ready', activated_at = :activated_at, updated_at = :updated_at
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
            await session.commit()

        activated = await self.get_model(tenant_id=tenant, user_id=user, model_id=mid)
        if activated is None:
            raise ValueError("activate result unavailable")
        return activated

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
                (
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
                )
                .mappings()
                .first()
            )
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
            "updated_at": row.get("updated_at").isoformat()
            if row.get("updated_at")
            else None,
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

        binding = await self.get_strategy_binding(
            tenant_id=tenant, user_id=user, strategy_id=sid
        )
        if binding is None:
            raise ValueError("binding not found after update")
        return binding

    async def delete_strategy_binding(
        self, *, tenant_id: str, user_id: str, strategy_id: str
    ) -> bool:
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
        market: str | None = None,
    ) -> ResolvedModel:
        tenant, user = self._normalize_owner(tenant_id=tenant_id, user_id=user_id)
        await self._ensure_system_default_record(tenant_id=tenant, user_id=user)

        reason_parts: list[str] = []
        # market 约束：传入时逐层校验模型市场，不匹配跳过该层；
        # 不传（None）= 旧行为逐字节不变（CN 兜底）
        wanted_market = _canonical_market(market) if market else None

        def _market_ok(item: dict[str, Any]) -> bool:
            return wanted_market is None or _model_market_of(item) == wanted_market

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
                if _market_ok(system_record):
                    return ResolvedModel(
                        effective_model_id=str(
                            system_record.get("model_id") or explicit_id
                        ),
                        model_source="explicit_system_model",
                        fallback_used=False,
                        fallback_reason="",
                        storage_path=str(system_record.get("storage_path") or ""),
                        model_file=str(system_record.get("model_file") or ""),
                        status=str(system_record.get("status") or "active"),
                    )
                reason_parts.append(
                    f"explicit system model={explicit_id} market mismatch"
                )

            explicit = await _load_ready(explicit_id)
            if explicit:
                if _market_ok(explicit):
                    return ResolvedModel(
                        effective_model_id=explicit_id,
                        model_source="explicit_model_id",
                        fallback_used=False,
                        fallback_reason="",
                        storage_path=str(explicit.get("storage_path") or ""),
                        model_file=str(explicit.get("model_file") or ""),
                        status=str(explicit.get("status") or "ready"),
                    )
                reason_parts.append(
                    f"explicit model_id={explicit_id} market mismatch "
                    f"(need {wanted_market})"
                )
            reason_parts.append(f"explicit model_id={explicit_id} not ready")

        sid = str(strategy_id or "").strip()
        if sid:
            binding = await self.get_strategy_binding(
                tenant_id=tenant, user_id=user, strategy_id=sid
            )
            if binding:
                binding_model_id = str(binding.get("model_id") or "")
                bound = await _load_ready(binding_model_id)
                if bound:
                    if _market_ok(bound):
                        return ResolvedModel(
                            effective_model_id=binding_model_id,
                            model_source="strategy_binding",
                            fallback_used=False,
                            fallback_reason="",
                            storage_path=str(bound.get("storage_path") or ""),
                            model_file=str(bound.get("model_file") or ""),
                            status=str(bound.get("status") or "ready"),
                        )
                    reason_parts.append(
                        f"strategy binding model_id={binding_model_id} "
                        f"market mismatch (need {wanted_market})"
                    )
                reason_parts.append(
                    f"strategy binding model_id={binding_model_id} not ready"
                )

        default = await self.get_default_model(
            tenant_id=tenant, user_id=user, market=wanted_market
        )
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
        # system 兜底模型为 CN 生产模型：显式约束非 CN 市场时不再落 CN 链
        if (wanted_market is None or wanted_market == "CN") and self.primary_model_id and Path(self.primary_model_dir).is_dir():
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

        return ResolvedModel(
            effective_model_id=None,
            model_source="none",
            fallback_used=False,
            fallback_reason=fallback_reason or "未配置可用模型",
            storage_path="",
            model_file="",
            status="not_found",
        )

    def _resolve_system_model_record_sync(
        self, explicit_id: str
    ) -> dict[str, Any] | None:
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
        model_info = (
            metadata.get("model_info")
            if isinstance(metadata.get("model_info"), dict)
            else {}
        )
        if isinstance(model_info, dict):
            display_name = str(
                model_info.get("name") or model_info.get("display_name") or ""
            ).strip()
        if not display_name:
            display_name = str(metadata.get("display_name") or raw)

        if raw == Path(self.primary_model_dir).name:
            canonical_model_id = self.primary_model_id
        elif raw == Path(self.fallback_model_dir).name:
            canonical_model_id = self.fallback_model_id
        else:
            canonical_model_id = f"sys-{raw}"

        context = (
            metadata.get("context") if isinstance(metadata.get("context"), dict) else {}
        )
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
                "model_type": metadata.get("model_type")
                or metadata.get("framework")
                or "",
                "feature_count": metadata.get("feature_count"),
                "features": metadata.get("feature_columns", []),
                "performance_metrics": metadata.get("performance_metrics", {}),
                "context": context,
                "train_start": metadata.get("train_start"),
                "train_end": metadata.get("train_end"),
                "valid_start": metadata.get("valid_start"),
                "valid_end": metadata.get("valid_end"),
                "test_start": metadata.get("test_start"),
                "test_end": metadata.get("test_end"),
            },
            "metrics_json": metadata.get("performance_metrics", {}),
        }

    def _get_model_sync(
        self, *, tenant_id: str, user_id: str, model_id: str
    ) -> dict[str, Any] | None:
        tenant, user = self._normalize_owner(tenant_id=tenant_id, user_id=user_id)
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
                )
                .mappings()
                .first()
            )
        return self._row_to_model(dict(row)) if row else None

    def _get_default_model_sync(
        self, *, tenant_id: str, user_id: str, market: str | None = None
    ) -> dict[str, Any] | None:
        tenant, user = self._normalize_owner(tenant_id=tenant_id, user_id=user_id)
        market_clause = ""
        params: dict[str, Any] = {"tenant_id": tenant, "user_id": user}
        if market:
            market_clause = " AND COALESCE(metadata_json->>'market', 'CN') = :market"
            params["market"] = str(market).upper().strip()
        with get_db() as session:
            row = (
                session.execute(
                    text(
                        f"""
                        SELECT tenant_id, user_id, model_id, source_run_id, status, storage_path, model_file,
                               metadata_json, metrics_json, is_default, created_at, updated_at, activated_at
                        FROM qm_user_models
                        WHERE tenant_id = :tenant_id AND user_id = :user_id
                          AND is_default = TRUE AND status IN ('ready', 'active'){market_clause}
                        ORDER BY activated_at DESC NULLS LAST, updated_at DESC
                        LIMIT 1
                        """
                    ),
                    params,
                )
                .mappings()
                .first()
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
                )
                .mappings()
                .first()
            )
        return dict(row) if row else None

    def resolve_effective_model_sync(
        self,
        *,
        tenant_id: str,
        user_id: str,
        strategy_id: str | None = None,
        model_id: str | None = None,
        market: str | None = None,
    ) -> dict[str, Any]:
        tenant, user = self._normalize_owner(tenant_id=tenant_id, user_id=user_id)

        reason_parts: list[str] = []
        # market 约束：传入时逐层校验模型市场，不匹配跳过该层（None = 旧行为）
        wanted_market = _canonical_market(market) if market else None

        def _market_ok(item: dict[str, Any]) -> bool:
            return wanted_market is None or _model_market_of(item) == wanted_market

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
                if _market_ok(system_record):
                    return ResolvedModel(
                        effective_model_id=str(
                            system_record.get("model_id") or explicit_id
                        ),
                        model_source="explicit_system_model",
                        fallback_used=False,
                        fallback_reason="",
                        storage_path=str(system_record.get("storage_path") or ""),
                        model_file=str(system_record.get("model_file") or ""),
                        status=str(system_record.get("status") or "active"),
                    ).to_dict()
                reason_parts.append(
                    f"explicit system model={explicit_id} market mismatch"
                )

            explicit = _load_ready(explicit_id)
            if explicit:
                if _market_ok(explicit):
                    return ResolvedModel(
                        effective_model_id=explicit_id,
                        model_source="explicit_model_id",
                        fallback_used=False,
                        fallback_reason="",
                        storage_path=str(explicit.get("storage_path") or ""),
                        model_file=str(explicit.get("model_file") or ""),
                        status=str(explicit.get("status") or "ready"),
                    ).to_dict()
                reason_parts.append(
                    f"explicit model_id={explicit_id} market mismatch "
                    f"(need {wanted_market})"
                )
            reason_parts.append(f"explicit model_id={explicit_id} not ready")

        sid = str(strategy_id or "").strip()
        if sid:
            binding = self._get_strategy_binding_sync(
                tenant_id=tenant, user_id=user, strategy_id=sid
            )
            if binding:
                binding_model_id = str(binding.get("model_id") or "")
                bound = _load_ready(binding_model_id)
                if bound:
                    if _market_ok(bound):
                        return ResolvedModel(
                            effective_model_id=binding_model_id,
                            model_source="strategy_binding",
                            fallback_used=False,
                            fallback_reason="",
                            storage_path=str(bound.get("storage_path") or ""),
                            model_file=str(bound.get("model_file") or ""),
                            status=str(bound.get("status") or "ready"),
                        ).to_dict()
                    reason_parts.append(
                        f"strategy binding model_id={binding_model_id} "
                        f"market mismatch (need {wanted_market})"
                    )
                reason_parts.append(
                    f"strategy binding model_id={binding_model_id} not ready"
                )

        default = self._get_default_model_sync(
            tenant_id=tenant, user_id=user, market=wanted_market
        )
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
        # system 兜底模型为 CN 生产模型：显式约束非 CN 市场时不再落 CN 链
        if (wanted_market is None or wanted_market == "CN") and self.primary_model_id and Path(self.primary_model_dir).is_dir():
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

        return ResolvedModel(
            effective_model_id=None,
            model_source="none",
            fallback_used=False,
            fallback_reason=fallback_reason or "未配置可用模型",
            storage_path="",
            model_file="",
            status="not_found",
        ).to_dict()

    @staticmethod
    def build_model_id_from_run(run_id: str, market: str = "CN") -> str:
        raw = str(run_id or "").strip()
        if not raw:
            raw = datetime.now().strftime("%Y%m%d%H%M%S")
        digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
        normalized = "".join(c if c.isalnum() or c in {"_", "-"} else "_" for c in raw)
        normalized = normalized[:88].strip("_") or "train_run"
        market_prefix = str(market or "CN").upper().strip()[:4] or "CN"
        return f"mdl_{market_prefix.lower()}_{normalized}_{digest}"

    @staticmethod
    def _infer_market_from_benchmark(benchmark: Any) -> str:
        """从 benchmark 推断市场，与 admin_training_utils._resolve_market 保持一致。"""
        raw = str(benchmark or "").upper().strip()
        _BENCHMARK_MARKET = {
            "HSI": "HK",
            "HSCEI": "HK",
            "HSTECH": "HK",
            "SPX": "US",
            "NDX": "US",
            "DJI": "US",
            "IXIC": "US",
            "BTC": "CRYPTO",
            "ETH": "CRYPTO",
            "CL": "FUTURES",
            "RB": "FUTURES",
            "AU": "FUTURES",
            "CU": "FUTURES",
        }
        return _BENCHMARK_MARKET.get(raw, "CN")

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

        # 先解析目标市场（显式 context.market 或 benchmark 推断），
        # 用于 model_id 前缀与存储路径市场分段
        req_context = (
            request_payload.get("context")
            if isinstance(request_payload.get("context"), dict)
            else {}
        )
        market_str = str(req_context.get("market") or "").upper().strip()
        if not market_str:
            market_str = self._infer_market_from_benchmark(req_context.get("benchmark"))
        market_str = market_str or "CN"

        model_id = self.build_model_id_from_run(run_id, market=market_str)
        now = datetime.now(timezone.utc)
        # 非 CN 市场模型按市场子目录分段，避免与 A 股模型混放
        model_dir = self.user_models_root / tenant / user / model_id
        if market_str and market_str != "CN":
            model_dir = (
                self.user_models_root / tenant / user / market_str.lower() / model_id
            )
        model_dir.mkdir(parents=True, exist_ok=True)

        metrics = (
            result_payload.get("metrics")
            if isinstance(result_payload.get("metrics"), dict)
            else {}
        )
        metadata = (
            result_payload.get("metadata")
            if isinstance(result_payload.get("metadata"), dict)
            else {}
        )

        # Propagate context (market, benchmark, etc.) from request to metadata
        existing_context = (
            metadata.get("context") if isinstance(metadata.get("context"), dict) else {}
        )
        merged_context = {**existing_context, **req_context}
        if (
            "market" not in merged_context
            or not str(merged_context.get("market") or "").strip()
        ):
            merged_context["market"] = market_str

        # Resolve display_name, appending market suffix if missing
        raw_display_name = str(
            request_payload.get("display_name")
            or metadata.get("display_name")
            or request_payload.get("job_name")
            or run_id
        )
        if market_str and not raw_display_name.upper().endswith(f"_{market_str}"):
            raw_display_name = f"{raw_display_name}_{market_str}"

        metadata = {
            **metadata,
            "context": merged_context,
            "market": market_str or "CN",
            "display_name": raw_display_name,
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
                {
                    "tenant_id": tenant,
                    "user_id": user,
                    "model_id": model_id,
                    "updated_at": now,
                },
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

        # 样本外验证软门禁：自动进入默认池前必须有可解释的 test Rank IC
        # 与 ICIR。门禁只阻止自动默认，用户仍可在模型页人工激活候选模型。
        gate_triggered = False
        _test_icir = self._extract_test_rank_icir(metadata, metrics)
        _test_rank_ic = self._extract_test_rank_ic(metadata, metrics)
        _gate_threshold = 0.05
        gate_reasons: list[str] = []
        if _test_icir is None:
            gate_reasons.append("缺少 test_rank_icir")
        elif _test_icir < _gate_threshold:
            gate_reasons.append(
                f"test_rank_icir={_test_icir:.4f} 低于软门禁阈值 {_gate_threshold}"
            )
        if _test_rank_ic is None:
            gate_reasons.append("缺少 test_rank_ic")
        elif _test_rank_ic <= 0:
            gate_reasons.append(f"test_rank_ic={_test_rank_ic:.4f} 非正")

        if sync_status == "ready" and gate_reasons:
            logger.warning(
                "Model %s held at candidate by out-of-sample quality gate: %s",
                model_id,
                "; ".join(gate_reasons),
            )
            sync_status = "candidate"
            gate_triggered = True
            metadata.setdefault("quality_warnings", []).append(
                f"样本外质量门禁：{'；'.join(gate_reasons)}，"
                "未自动激活。请人工评估后在模型管理页手动激活。"
            )

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
            elif gate_triggered:
                # 软门禁触发：保持 candidate，持久化 quality_warnings
                await session.execute(
                    text(
                        """
                        UPDATE qm_user_models
                        SET status = 'candidate',
                            model_file = :model_file,
                            metadata_json = :metadata_json,
                            updated_at = :updated_at
                        WHERE tenant_id = :tenant_id AND user_id = :user_id AND model_id = :model_id
                        """
                    ),
                    {
                        "tenant_id": tenant,
                        "user_id": user,
                        "model_id": model_id,
                        "model_file": model_file,
                        "metadata_json": json.dumps(metadata, ensure_ascii=False),
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

    async def register_ensemble_model(
        self,
        *,
        tenant_id: str,
        user_id: str,
        source_model_ids: list[str],
        display_name: str,
        weight_strategy: str = "equal",
        manual_weights: dict[str, float] | None = None,
        fusion_strategy: str = "linear",
        strategy_config: dict[str, float] | None = None,
    ) -> dict[str, Any]:
        """创建持久化融合模型（推理时融合多个源模型的预测）。

        源模型可以是任意类型（单模型 / stacking 融合 / 不同周期）。
        融合模型不包含二进制权重，其目录含：
          - ensemble_config.json  源模型引用 + 权重 + 策略
          - metadata.json         融合元信息
          - inference.py          融合推理脚本（复用 inference_ensemble_src 模板）

        权重策略：
          - equal        每个源模型等权
          - icir         按源模型 Val Rank ICIR 归一化加权
          - manual       使用 manual_weights（自动归一化到和为 1）
        """
        tenant, user = self._normalize_owner(tenant_id=tenant_id, user_id=user_id)
        await self.ensure_tables()

        source_model_ids = [
            str(m).strip() for m in (source_model_ids or []) if str(m).strip()
        ]
        if len(source_model_ids) < 2:
            raise ValueError("融合至少需要 2 个源模型")

        # 加载全部源模型（含 metadata_json + metrics_json）
        sources: list[dict[str, Any]] = []
        for mid in source_model_ids:
            model = await self.get_model(tenant_id=tenant, user_id=user, model_id=mid)
            if not model:
                raise ValueError(f"源模型不存在: {mid}")
            if str(model.get("status")) not in _READY_STATUSES:
                raise ValueError(
                    f"源模型 {mid} 状态为 {model.get('status')}，需为 ready/active"
                )
            sources.append(model)

        # 权重计算
        if weight_strategy == "icir":
            weights: dict[str, float] = {}
            for src in sources:
                mid = str(src["model_id"])
                meta = self._parse_json_field(src.get("metadata_json"))
                metrics = self._parse_json_field(src.get("metrics_json"))
                icir = None
                for k in ("val_rank_icir", "val_icir", "rank_icir"):
                    if k in metrics:
                        icir = float(metrics[k])
                        break
                if icir is None:
                    m = meta.get("metrics")
                    if isinstance(m, dict):
                        icir = float(m.get("val_rank_icir") or m.get("val_icir") or 0)
                weights[mid] = max(float(icir or 0), 0.0)
            total = sum(weights.values()) or 1.0
            if total <= 0:
                weights = {str(s["model_id"]): 1.0 / len(sources) for s in sources}
            else:
                weights = {k: v / total for k, v in weights.items()}
        elif weight_strategy == "manual":
            manual_weights = manual_weights or {}
            raw = {str(k): float(v) for k, v in manual_weights.items() if float(v) > 0}
            missing = [
                str(s["model_id"]) for s in sources if str(s["model_id"]) not in raw
            ]
            if missing:
                raise ValueError(f"manual 权重缺少源模型: {missing}")
            total = sum(raw.values()) or 1.0
            weights = {k: v / total for k, v in raw.items()}
        elif weight_strategy == "recent_ic":
            # 初始等权；每日回填任务按近30日生产 rank_ic 动态刷新 weight_snapshot.json
            weights = {str(s["model_id"]): 1.0 / len(sources) for s in sources}
        else:
            weights = {str(s["model_id"]): 1.0 / len(sources) for s in sources}

        # 从源模型 context 汇总市场（取出现最多的），用于 model_id 与存储分段
        benchmark = "SH000300"
        context: dict[str, Any] = {}
        for src in sources:
            meta = self._parse_json_field(src.get("metadata_json"))
            ctx = meta.get("context")
            if isinstance(ctx, dict):
                context.update(ctx)
        market = str(context.get("market") or "CN").upper()

        # 生成 model_id
        now = datetime.now(timezone.utc)
        ts = now.strftime("%Y%m%d%H%M%S")
        digest = hashlib.sha1(
            "|".join(sorted(source_model_ids)).encode("utf-8")
        ).hexdigest()[:8]
        model_id = f"mdl_{market.lower()}_ensemble_{ts}_{digest}"

        # 构建元数据
        source_meta_list = []
        for src in sources:
            meta = self._parse_json_field(src.get("metadata_json"))
            metrics = self._parse_json_field(src.get("metrics_json"))
            source_meta_list.append(
                {
                    "model_id": str(src["model_id"]),
                    "model_name": meta.get("display_name")
                    or meta.get("model_name")
                    or str(src["model_id"]),
                    "model_type": meta.get("model_type"),
                    "framework": meta.get("framework"),
                    "target_horizon_days": meta.get("target_horizon_days"),
                    "feature_count": meta.get("feature_count"),
                    "weight": round(weights.get(str(src["model_id"]), 0), 6),
                    "metrics": metrics,
                }
            )

        # 特征并集
        unified_features: list[str] = []
        for src in sources:
            meta = self._parse_json_field(src.get("metadata_json"))
            feats = meta.get("feature_columns") or meta.get("features") or []
            for f in feats:
                if f not in unified_features:
                    unified_features.append(f)
        feature_count = len(unified_features)

        raw_display_name = str(display_name or "").strip() or "Ensemble"
        if not raw_display_name.upper().endswith(f"_{market}"):
            raw_display_name = f"{raw_display_name}_{market}"

        metadata: dict[str, Any] = {
            "model_type": "ensemble",
            "model_name": raw_display_name,
            "display_name": raw_display_name,
            "ensemble_method": "fusion",
            "is_ensemble": True,
            "model_file": "ensemble_config.json",
            "framework": "ensemble",
            "ensemble_method": "fusion",
            "fusion_strategy": fusion_strategy,
            "strategy_config": strategy_config or {},
            "source_models": source_meta_list,
            "source_model_ids": [str(s["model_id"]) for s in sources],
            "weight_strategy": weight_strategy,
            "weights": {
                str(s["model_id"]): round(weights.get(str(s["model_id"]), 0), 6)
                for s in sources
            },
            "feature_count": feature_count,
            "features": unified_features,
            "feature_columns": unified_features,
            "context": context,
            "benchmark": benchmark,
            "market": market,
            "target_horizon_days": 15,
            "target_mode": "return",
            "data_source": "parquet",
            "generated_at": now.isoformat(),
            "metrics": {
                "val_ic": 0.0,
                "test_ic": 0.0,
                "score_direction": "normal",
            },
        }

        # 创建模型目录（非 CN 市场按市场子目录分段）
        model_dir = self.user_models_root / tenant / user / model_id
        if market and market != "CN":
            model_dir = (
                self.user_models_root / tenant / user / market.lower() / model_id
            )
        model_dir.mkdir(parents=True, exist_ok=True)

        # 写入 ensemble_config.json（源模型用绝对路径）
        ensemble_config = {
            "version": 1,
            "created_at": now.isoformat(),
            "weight_strategy": weight_strategy,
            "fusion_strategy": fusion_strategy,
            "strategy_config": strategy_config or {},
            "models": [
                {
                    "model_id": str(s["model_id"]),
                    "model_dir": str(
                        Path(
                            s.get("storage_path")
                            or (
                                self.user_models_root
                                / tenant
                                / user
                                / str(s["model_id"])
                            )
                        ).resolve()
                    ),
                    "weight": round(weights.get(str(s["model_id"]), 0), 6),
                    "target_horizon_days": self._parse_json_field(
                        s.get("metadata_json")
                    ).get("target_horizon_days"),
                    "feature_count": self._parse_json_field(s.get("metadata_json")).get(
                        "feature_count"
                    ),
                }
                for s in sources
            ],
        }
        (model_dir / "ensemble_config.json").write_text(
            json.dumps(ensemble_config, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (model_dir / "metadata.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        # 部署融合推理脚本（从模板复制，避免被 parquet 模板覆盖）
        template_path = (
            Path(__file__).parent.parent
            / "services"
            / "engine"
            / "inference"
            / "templates"
            / "inference_ensemble_src.py"
        )
        if template_path.is_file():
            shutil.copy2(template_path, model_dir / "inference.py")
        else:
            logger.warning("融合推理模板不存在: %s，模型推理可能失败", template_path)

        # 写库
        now_db = datetime.now(timezone.utc)
        async with get_session() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO qm_user_models (
                        tenant_id, user_id, model_id, source_run_id, status, storage_path, model_file,
                        metadata_json, metrics_json, is_default, created_at, updated_at, activated_at
                    ) VALUES (
                        :tenant_id, :user_id, :model_id, :source_run_id, 'ready', :storage_path, :model_file,
                        CAST(:metadata_json AS JSONB), CAST(:metrics_json AS JSONB), FALSE,
                        :created_at, :updated_at, :activated_at
                    )
                    ON CONFLICT (tenant_id, user_id, model_id) DO UPDATE SET
                        status = 'ready',
                        storage_path = EXCLUDED.storage_path,
                        model_file = EXCLUDED.model_file,
                        metadata_json = EXCLUDED.metadata_json,
                        metrics_json = EXCLUDED.metrics_json,
                        updated_at = EXCLUDED.updated_at
                    """
                ),
                {
                    "tenant_id": tenant,
                    "user_id": user,
                    "model_id": model_id,
                    "source_run_id": f"ensemble_{ts}",
                    "storage_path": str(model_dir.resolve()),
                    "model_file": "ensemble_config.json",
                    "metadata_json": json.dumps(metadata, ensure_ascii=False),
                    "metrics_json": json.dumps(
                        {"val_ic": 0.0, "test_ic": 0.0}, ensure_ascii=False
                    ),
                    "created_at": now_db,
                    "updated_at": now_db,
                    "activated_at": now_db,
                },
            )

            # 若无业务默认模型，设为默认
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
            if not has_business_default:
                await session.execute(
                    text(
                        """
                        UPDATE qm_user_models
                        SET is_default = FALSE, updated_at = :updated_at
                        WHERE tenant_id = :tenant_id AND user_id = :user_id AND is_default = TRUE
                        """
                    ),
                    {"tenant_id": tenant, "user_id": user, "updated_at": now_db},
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
                        "model_id": model_id,
                        "activated_at": now_db,
                        "updated_at": now_db,
                    },
                )

        logger.info(
            "[%s] 融合模型已创建: %s (%d 个源模型, 权重策略=%s)",
            model_id,
            raw_display_name,
            len(sources),
            weight_strategy,
        )
        return {
            "model_id": model_id,
            "status": "ready",
            "storage_path": str(model_dir.resolve()),
            "model_file": "ensemble_config.json",
            "metadata": metadata,
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
        artifact_names = [
            "model.lgb",
            "model.xgb",
            "model.cbm",
            "model.pkl",
            "model.pth",
            "model.txt",
            "model.bin",
            # Stacking 基模型命名: model_xgb.xgb, model_lgb.lgb 等
            "model_xgb.xgb",
            "model_xgb.pkl",
            "model_lgb.lgb",
            "model_lgb.txt",
            "model_cbm.cbm",
            "model_lin.pkl",
            "meta_model.pkl",
            "ensemble_config.json",
            "metadata.json",
            "pred.parquet",
            "pred.pkl",
            "config.yaml",
            "result.json",
            "inference.py",
            "shap_summary.csv",
        ]

        # 任务工作目录：容器部署沿用 /data/training_jobs/{run_id}；
        # 便携包等免 Docker 部署按 STORAGE_ROOT 落位（与本地直跑编排器写盘同源）
        from backend.shared.training_runtime import training_jobs_dir

        job_dir = training_jobs_dir(run_id)

        # 多模型模式：metadata.json 里 saved_models 记录了全部基模型文件名
        # （model_gru.pth / model_mlp.pkl 等新后缀名不在静态白名单），动态展开补全
        for predefined_dir in (target_dir, job_dir):
            meta_path = predefined_dir / "metadata.json"
            if meta_path.is_file():
                try:
                    saved = json.loads(meta_path.read_text(encoding="utf-8")).get("saved_models") or {}
                    artifact_names.extend(
                        v for v in saved.values() if v not in artifact_names
                    )
                except Exception:
                    pass
                break

        copied: list[str] = []
        for source_dir in (target_dir, job_dir):
            if not source_dir.exists() or not source_dir.is_dir():
                continue
            for filename in artifact_names:
                src = source_dir / filename
                if not src.is_file():
                    continue
                dest = target_dir / filename
                if src.resolve() == dest.resolve():
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)
                copied.append(filename)

        model_file = ""
        for candidate in (
            "model.lgb",
            "model.xgb",
            "model.cbm",
            "model.pkl",
            "model.pth",
            "model.txt",
            "model.bin",
            "model_xgb.xgb",
            "model_lgb.lgb",
            "model_cbm.cbm",
            "model_lin.pkl",
            "meta_model.pkl",
            "ensemble_config.json",
        ):
            if (target_dir / candidate).exists():
                model_file = candidate
                break

        if (target_dir / "metadata.json").exists() and model_file:
            return "ready", "", model_file

        cos = get_cos_service()
        source_prefix = f"models/candidates/{run_id}/"
        for filename in artifact_names:
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

        for candidate in (
            "model.lgb",
            "model.xgb",
            "model.cbm",
            "model.pkl",
            "model_xgb.xgb",
            "model_lgb.lgb",
            "model_cbm.cbm",
            "meta_model.pkl",
            "model.txt",
            "model.bin",
        ):
            if (target_dir / candidate).exists():
                model_file = candidate
                break

        if not copied:
            return (
                "failed",
                "no artifacts found in local training workspace or COS path",
                model_file,
            )

        return "ready", "", model_file

    @staticmethod
    def _extract_test_rank_icir(metadata: dict, metrics: dict) -> float | None:
        """从模型 metadata/metrics 提取 test 集 Rank ICIR（样本外验证指标）。

        优先取 metadata.metrics.test_rank_icir（train.py 写入）；回退 metrics_json。
        无法确定时返回 None（软门禁不生效，模型按原流程进入 ready）。
        """
        for src in (metrics, metadata.get("metrics"), metadata):
            if not isinstance(src, dict):
                continue
            v = src.get("test_rank_icir")
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                try:
                    return float(v)
                except (TypeError, ValueError):
                    pass
        return None

    @staticmethod
    def _extract_test_rank_ic(metadata: dict, metrics: dict) -> float | None:
        """从训练 metadata/metrics 提取 test 集 Rank IC。"""
        for src in (metrics, metadata.get("metrics"), metadata):
            if not isinstance(src, dict):
                continue
            v = src.get("test_rank_ic")
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                try:
                    return float(v)
                except (TypeError, ValueError):
                    pass
        return None

    @staticmethod
    def _validate_synced_model(
        *,
        target_dir: Path,
        model_file: str,
        request_payload: dict[str, Any],
    ) -> str:
        if not model_file or not (target_dir / model_file).exists():
            # 融合模型：以 ensemble_config.json 作为模型标识文件（无二进制权重）
            if (target_dir / "ensemble_config.json").exists():
                model_file = "ensemble_config.json"
            else:
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
