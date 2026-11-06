"""Owner-scoped persistence for AutoAlpha research documents.

File bytes are stored by the quota-managed artifact layer. This module stores
only logical artifact keys and document lifecycle metadata; it never accepts or
persists caller-provided absolute paths.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any

from sqlalchemy import text

from backend.shared.database_manager_v2 import get_session
from backend.shared.owner_scope import OwnerScope
from backend.shared.utc_datetime import to_utc_iso, utc_now

DOC_STATUSES = frozenset(
    {"uploaded", "parsing", "parsed", "parse_failed", "organized", "expired", "deleted"}
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_DOCUMENT_PARTS = 10
_UNSET = object()
_TRANSITIONS = {
    "uploaded": {"parsing", "parse_failed", "expired", "deleted"},
    "parsing": {"parsed", "parse_failed", "deleted"},
    "parsed": {"organized", "expired", "deleted"},
    "organized": {"expired", "deleted"},
    "parse_failed": {"parsing", "expired", "deleted"},
    "expired": {"deleted"},
    "deleted": set(),
}


def _storage_key(value: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise ValueError("Invalid document storage key")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError("Invalid document storage key")
    return path.as_posix()


def _public_row(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    for field in ("created_at", "updated_at", "organized_at"):
        if field in result and isinstance(result[field], datetime):
            result[field] = to_utc_iso(result[field])
    # Owner fields and storage locations are internal; API callers receive only
    # metadata after the repository has already enforced the owner scope.
    result.pop("tenant_id", None)
    result.pop("user_id", None)
    result.pop("storage_key", None)
    result.pop("markdown_key", None)
    result.pop("content_list_key", None)
    result.pop("parsed_artifact_keys", None)
    result.pop("parser_batch_id", None)
    result.pop("original_parts", None)
    return result


def _encode_original_parts(
    parts: list[dict[str, Any]] | None,
    *,
    storage_key: str,
    size_bytes: int,
) -> tuple[int, str | None]:
    if parts is None:
        return 1, "[]"
    if not isinstance(parts, list) or not 2 <= len(parts) <= MAX_DOCUMENT_PARTS:
        raise ValueError("Invalid document part count")
    clean: list[dict[str, Any]] = []
    for part in parts:
        if not isinstance(part, dict):
            raise ValueError("Invalid document part manifest")
        key = _storage_key(part.get("storage_key"))
        name = part.get("filename")
        ext = str(part.get("ext") or "").lower()
        size = part.get("size_bytes")
        digest = part.get("sha256")
        if (
            not isinstance(name, str)
            or not name
            or len(name) > 255
            or "/" in name
            or "\\" in name
            or "\x00" in name
            or ext != "pdf"
            or type(size) is not int
            or size < 1
            or not isinstance(digest, str)
            or not _SHA256_RE.fullmatch(digest)
        ):
            raise ValueError("Invalid document part metadata")
        clean.append(
            {
                "storage_key": key,
                "filename": name,
                "ext": ext,
                "size_bytes": size,
                "sha256": digest,
            }
        )
    if clean[0]["storage_key"] != _storage_key(storage_key):
        raise ValueError("Primary document storage key must match first part")
    if sum(part["size_bytes"] for part in clean) != size_bytes:
        raise ValueError("Document part sizes do not match document total")
    return len(clean), json.dumps(clean, ensure_ascii=False, allow_nan=False)


def decode_original_parts(row: dict[str, Any]) -> list[dict[str, Any]]:
    """Return validated logical input parts; old single-file rows stay readable."""
    raw = row.get("original_parts")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("Document part manifest is malformed") from exc
    if raw is None or raw == []:
        return [
            {
                "storage_key": _storage_key(str(row.get("storage_key") or "")),
                "filename": str(row.get("filename") or "document.pdf"),
                "ext": str(row.get("ext") or "pdf").lower(),
                "size_bytes": int(row.get("size_bytes") or 0),
                "sha256": str(row.get("sha256") or ""),
            }
        ]
    if not isinstance(raw, list) or not 2 <= len(raw) <= MAX_DOCUMENT_PARTS:
        raise ValueError("Document part manifest has invalid cardinality")
    count, encoded = _encode_original_parts(
        raw,
        storage_key=str(row.get("storage_key") or ""),
        size_bytes=int(row.get("size_bytes") or 0),
    )
    if count != int(row.get("files_count") or count) or encoded is None:
        raise ValueError("Document part manifest count mismatch")
    return json.loads(encoded)


class DocStore:
    """Persistence operations always require both authenticated owner fields."""

    async def create_doc(
        self,
        *,
        tenant_id: str,
        user_id: str,
        doc_id: str,
        filename: str,
        ext: str,
        size_bytes: int,
        sha256: str,
        storage_key: str,
        original_parts: list[dict[str, Any]] | None = None,
    ) -> bool:
        owner = OwnerScope(tenant_id, user_id)
        if not doc_id or len(doc_id) > 128:
            raise ValueError("Invalid document ID")
        if not filename or len(filename) > 255 or "\x00" in filename:
            raise ValueError("Invalid document filename")
        if not ext or len(ext) > 16 or not ext.isascii() or not ext.isalnum():
            raise ValueError("Invalid document extension")
        if type(size_bytes) is not int or size_bytes < 0:
            raise ValueError("Invalid document size")
        if not isinstance(sha256, str) or not _SHA256_RE.fullmatch(sha256):
            raise ValueError("Invalid document SHA-256")
        key = _storage_key(storage_key)
        files_count, parts_json = _encode_original_parts(
            original_parts, storage_key=key, size_bytes=size_bytes
        )
        now = utc_now()
        async with get_session() as session:
            result = await session.execute(
                text(
                    "INSERT INTO rd_agent_docs "
                    "(tenant_id,user_id,doc_id,filename,ext,size_bytes,sha256,"
                    "storage_key,files_count,original_parts,status,created_at,updated_at) "
                    "VALUES (:tenant_id,:user_id,:doc_id,:filename,:ext,:size_bytes,"
                    ":sha256,:storage_key,:files_count,CAST(:original_parts AS jsonb),"
                    "'uploaded',:now,:now) "
                    "ON CONFLICT (tenant_id,user_id,doc_id) DO NOTHING"
                ),
                {
                    **owner.sql_params(),
                    "doc_id": doc_id,
                    "filename": filename,
                    "ext": ext.lower(),
                    "size_bytes": size_bytes,
                    "sha256": sha256,
                    "storage_key": key,
                    "files_count": files_count,
                    "original_parts": parts_json,
                    "now": now,
                },
            )
            return result.rowcount == 1

    async def get_doc(
        self, doc_id: str, *, tenant_id: str, user_id: str
    ) -> dict[str, Any] | None:
        owner = OwnerScope(tenant_id, user_id)
        async with get_session(read_only=True) as session:
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT tenant_id,user_id,doc_id,filename,ext,size_bytes,sha256,"
                            "storage_key,status,parser_batch_id,page_count,markdown_key,"
                            "content_list_key,parsed_artifact_keys,parse_job_id,parse_state,organized_text,"
                            "organize_kind,organize_job_id,organize_state,organized_payload,"
                            "organize_prompt_version,organized_at,error,"
                            "created_at,updated_at FROM rd_agent_docs "
                            "WHERE tenant_id=:tenant_id AND user_id=:user_id AND doc_id=:doc_id"
                        ),
                        {**owner.sql_params(), "doc_id": doc_id},
                    )
                )
                .mappings()
                .first()
            )
        return dict(row) if row else None

    async def list_docs(
        self,
        *,
        tenant_id: str,
        user_id: str,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        owner = OwnerScope(tenant_id, user_id)
        if status is not None and status not in DOC_STATUSES:
            raise ValueError("Invalid document status")
        if not 1 <= limit <= 200 or offset < 0:
            raise ValueError("Document pagination is out of range")
        where = ["tenant_id=:tenant_id", "user_id=:user_id"]
        params: dict[str, Any] = {
            **owner.sql_params(),
            "limit": limit + 1,
            "offset": offset,
        }
        if status is not None:
            where.append("status=:status")
            params["status"] = status
        async with get_session(read_only=True) as session:
            result = await session.execute(
                text(
                    "SELECT tenant_id,user_id,doc_id,filename,ext,size_bytes,sha256,"
                    "storage_key,status,parser_batch_id,page_count,markdown_key,"
                    "content_list_key,parsed_artifact_keys,parse_job_id,parse_state,"
                    "organize_kind,organize_job_id,organize_state,"
                    "organize_prompt_version,organized_at,error,"
                    "created_at,updated_at FROM rd_agent_docs WHERE "
                    + " AND ".join(where)
                    + " ORDER BY created_at DESC,doc_id LIMIT :limit OFFSET :offset"
                ),
                params,
            )
            return [dict(row) for row in result.mappings().all()]

    async def update_doc(
        self,
        doc_id: str,
        *,
        tenant_id: str,
        user_id: str,
        expected_status: str,
        status: str,
        parser_batch_id: str | None = None,
        page_count: int | None = None,
        markdown_key: str | None = None,
        content_list_key: str | None = None,
        organized_text: str | None = None,
        organize_kind: str | None = None,
        organize_state: str | None = None,
        organized_payload: dict[str, Any] | None = None,
        organize_prompt_version: str | None = None,
        error: str | None | object = _UNSET,
        parse_job_id: str | None = None,
        parsed_artifact_keys: list[str] | None = None,
        parse_state: str | None = None,
    ) -> bool:
        owner = OwnerScope(tenant_id, user_id)
        if expected_status not in DOC_STATUSES or status not in DOC_STATUSES:
            raise ValueError("Invalid document status transition")
        if status not in _TRANSITIONS[expected_status]:
            raise ValueError("Invalid document status transition")
        if page_count is not None and (type(page_count) is not int or page_count < 0):
            raise ValueError("Invalid document page count")
        md_key = _storage_key(markdown_key) if markdown_key is not None else None
        content_key = (
            _storage_key(content_list_key) if content_list_key is not None else None
        )
        artifact_keys = None
        if parsed_artifact_keys is not None:
            if (
                not isinstance(parsed_artifact_keys, list)
                or len(parsed_artifact_keys) > 2000
            ):
                raise ValueError("Invalid parsed artifact key list")
            artifact_keys = [_storage_key(key) for key in parsed_artifact_keys]
        if organized_payload is not None and not isinstance(organized_payload, dict):
            raise ValueError("Invalid organized document payload")
        now = utc_now()
        async with get_session() as session:
            result = await session.execute(
                text(
                    "UPDATE rd_agent_docs SET status=:status,"
                    "parser_batch_id=COALESCE(:parser_batch_id,parser_batch_id),"
                    "page_count=COALESCE(:page_count,page_count),"
                    "markdown_key=COALESCE(:markdown_key,markdown_key),"
                    "content_list_key=COALESCE(:content_list_key,content_list_key),"
                    "parse_job_id=COALESCE(:parse_job_id,parse_job_id),"
                    "parse_state=COALESCE(:parse_state,parse_state),"
                    "parsed_artifact_keys=COALESCE(CAST(:parsed_artifact_keys AS jsonb),"
                    "parsed_artifact_keys),"
                    "organized_text=COALESCE(:organized_text,organized_text),"
                    "organize_kind=COALESCE(:organize_kind,organize_kind),"
                    "organize_state=COALESCE(:organize_state,organize_state),"
                    "organized_payload=COALESCE(CAST(:organized_payload AS jsonb),"
                    "organized_payload),"
                    "organize_prompt_version=COALESCE(:organize_prompt_version,"
                    "organize_prompt_version),"
                    "organized_at=CASE WHEN :status='organized' THEN :now ELSE organized_at END,"
                    "error=CASE WHEN :error_is_set THEN :error ELSE error END,"
                    "updated_at=:now WHERE tenant_id=:tenant_id "
                    "AND user_id=:user_id AND doc_id=:doc_id AND status=:expected_status"
                ),
                {
                    **owner.sql_params(),
                    "doc_id": doc_id,
                    "expected_status": expected_status,
                    "status": status,
                    "parser_batch_id": parser_batch_id,
                    "page_count": page_count,
                    "markdown_key": md_key,
                    "content_list_key": content_key,
                    "parse_job_id": parse_job_id,
                    "parse_state": parse_state,
                    "parsed_artifact_keys": (
                        json.dumps(artifact_keys) if artifact_keys is not None else None
                    ),
                    "organized_text": organized_text,
                    "organize_kind": organize_kind,
                    "organize_state": organize_state,
                    "organized_payload": (
                        json.dumps(organized_payload, ensure_ascii=False, allow_nan=False)
                        if organized_payload is not None
                        else None
                    ),
                    "organize_prompt_version": organize_prompt_version,
                    "error": None if error is _UNSET else error,
                    "error_is_set": error is not _UNSET,
                    "now": now,
                },
            )
            return result.rowcount == 1

    async def attach_parse_job(
        self,
        doc_id: str,
        *,
        tenant_id: str,
        user_id: str,
        job_id: str,
    ) -> bool:
        owner = OwnerScope(tenant_id, user_id)
        if not job_id or len(job_id) > 128:
            raise ValueError("Invalid document parse job ID")
        async with get_session() as session:
            result = await session.execute(
                text(
                    "UPDATE rd_agent_docs SET parse_job_id=:job_id,updated_at=:now "
                    "WHERE tenant_id=:tenant_id AND user_id=:user_id "
                    "AND doc_id=:doc_id AND status IN ('uploaded','parse_failed','parsing') "
                    "AND (parse_job_id IS NULL OR parse_job_id=:job_id OR status='parse_failed')"
                ),
                {
                    **owner.sql_params(),
                    "doc_id": doc_id,
                    "job_id": job_id,
                    "now": utc_now(),
                },
            )
            return result.rowcount == 1

    async def begin_parse(
        self,
        doc_id: str,
        *,
        tenant_id: str,
        user_id: str,
        job_id: str,
    ) -> bool:
        owner = OwnerScope(tenant_id, user_id)
        async with get_session() as session:
            result = await session.execute(
                text(
                    "UPDATE rd_agent_docs SET status='parsing',parse_job_id=:job_id,"
                    "parse_state='submitting',"
                    "parser_batch_id=NULL,page_count=NULL,markdown_key=NULL,"
                    "content_list_key=NULL,parsed_artifact_keys='[]'::jsonb,error=NULL,"
                    "updated_at=:now WHERE tenant_id=:tenant_id AND user_id=:user_id "
                    "AND doc_id=:doc_id AND (parse_job_id IS NULL OR parse_job_id=:job_id "
                    "OR status='parse_failed') "
                    "AND status IN ('uploaded','parse_failed')"
                ),
                {
                    **owner.sql_params(),
                    "doc_id": doc_id,
                    "job_id": job_id,
                    "now": utc_now(),
                },
            )
            return result.rowcount == 1

    async def record_parser_batch(
        self,
        doc_id: str,
        *,
        tenant_id: str,
        user_id: str,
        job_id: str,
        batch_id: str,
    ) -> bool:
        owner = OwnerScope(tenant_id, user_id)
        if not batch_id or len(batch_id) > 256:
            raise ValueError("Invalid MinerU batch ID")
        async with get_session() as session:
            result = await session.execute(
                text(
                    "UPDATE rd_agent_docs SET parser_batch_id=:batch_id,"
                    "parse_state='pending',updated_at=:now WHERE tenant_id=:tenant_id "
                    "AND user_id=:user_id AND doc_id=:doc_id AND parse_job_id=:job_id "
                    "AND status='parsing' AND parse_state='submitting'"
                ),
                {
                    **owner.sql_params(),
                    "doc_id": doc_id,
                    "job_id": job_id,
                    "batch_id": batch_id,
                    "now": utc_now(),
                },
            )
            return result.rowcount == 1

    async def update_parse_progress(
        self,
        doc_id: str,
        *,
        tenant_id: str,
        user_id: str,
        job_id: str,
        parse_state: str,
        page_count: int | None = None,
    ) -> bool:
        owner = OwnerScope(tenant_id, user_id)
        if not parse_state or len(parse_state) > 64:
            raise ValueError("Invalid document parse state")
        if page_count is not None and (type(page_count) is not int or page_count < 0):
            raise ValueError("Invalid document page count")
        async with get_session() as session:
            result = await session.execute(
                text(
                    "UPDATE rd_agent_docs SET parse_state=:parse_state,"
                    "page_count=COALESCE(:page_count,page_count),updated_at=:now "
                    "WHERE tenant_id=:tenant_id AND user_id=:user_id AND doc_id=:doc_id "
                    "AND parse_job_id=:job_id AND status='parsing'"
                ),
                {
                    **owner.sql_params(),
                    "doc_id": doc_id,
                    "job_id": job_id,
                    "parse_state": parse_state,
                    "page_count": page_count,
                    "now": utc_now(),
                },
            )
            return result.rowcount == 1

    def attach_organize_job(
        self,
        session: Any,
        doc_id: str,
        *,
        tenant_id: str,
        user_id: str,
        job_id: str,
        source_sha256: str,
        markdown_key: str,
    ) -> bool:
        """Atomically link a newly inserted job to the same private document."""
        owner = OwnerScope(tenant_id, user_id)
        if not job_id or len(job_id) > 128:
            raise ValueError("Invalid document organization job ID")
        key = _storage_key(markdown_key)
        if not isinstance(source_sha256, str) or not _SHA256_RE.fullmatch(source_sha256):
            raise ValueError("Invalid document organization source hash")
        result = session.execute(
            text(
                "UPDATE rd_agent_docs SET organize_job_id=:job_id,"
                "organize_state='queued',error=NULL,updated_at=:now "
                "WHERE tenant_id=:tenant_id AND user_id=:user_id AND doc_id=:doc_id "
                "AND sha256=:sha256 AND markdown_key=:markdown_key "
                "AND status IN ('parsed','organized') "
                "AND organize_state NOT IN ('queued','running','stopping')"
            ),
            {
                **owner.sql_params(),
                "doc_id": doc_id,
                "job_id": job_id,
                "sha256": source_sha256,
                "markdown_key": key,
                "now": utc_now(),
            },
        )
        return result.rowcount == 1

    async def set_organize_running(
        self, doc_id: str, *, tenant_id: str, user_id: str, job_id: str
    ) -> bool:
        owner = OwnerScope(tenant_id, user_id)
        async with get_session() as session:
            result = await session.execute(
                text(
                    "UPDATE rd_agent_docs SET organize_state='running',updated_at=:now "
                    "WHERE tenant_id=:tenant_id AND user_id=:user_id AND doc_id=:doc_id "
                    "AND organize_job_id=:job_id AND status IN ('parsed','organized') "
                    "AND organize_state IN ('queued','running','stopping')"
                ),
                {
                    **owner.sql_params(),
                    "doc_id": doc_id,
                    "job_id": job_id,
                    "now": utc_now(),
                },
            )
            return result.rowcount == 1

    def persist_organize_result(
        self,
        session: Any,
        doc_id: str,
        *,
        tenant_id: str,
        user_id: str,
        job_id: str,
        kind: str,
        prompt_version: str,
        payload: dict[str, Any],
        markdown: str,
    ) -> bool:
        owner = OwnerScope(tenant_id, user_id)
        if kind not in {"free", "paper"} or not prompt_version:
            raise ValueError("Invalid document organization metadata")
        if not isinstance(payload, dict) or not isinstance(markdown, str):
            raise ValueError("Invalid document organization result")
        if len(markdown.encode("utf-8")) > 256 * 1024:
            raise ValueError("Organized document exceeds 256 KiB")
        encoded_payload = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        )
        if len(encoded_payload.encode("utf-8")) > 256 * 1024:
            raise ValueError("Organized document payload exceeds 256 KiB")
        result = session.execute(
            text(
                "UPDATE rd_agent_docs SET status='organized',organize_state='completed',"
                "organized_text=:markdown,organized_payload=CAST(:payload AS jsonb),"
                "organize_kind=:kind,organize_prompt_version=:prompt_version,"
                "organized_at=:now,error=NULL,updated_at=:now "
                "WHERE tenant_id=:tenant_id AND user_id=:user_id AND doc_id=:doc_id "
                "AND organize_job_id=:job_id AND status IN ('parsed','organized') "
                "AND organize_state IN ('queued','running','stopping')"
            ),
            {
                **owner.sql_params(),
                "doc_id": doc_id,
                "job_id": job_id,
                "markdown": markdown,
                "payload": encoded_payload,
                "kind": kind,
                "prompt_version": prompt_version[:64],
                "now": utc_now(),
            },
        )
        return result.rowcount == 1

    async def finish_organize(
        self,
        doc_id: str,
        *,
        tenant_id: str,
        user_id: str,
        job_id: str,
        kind: str,
        prompt_version: str,
        payload: dict[str, Any],
        markdown: str,
    ) -> bool:
        owner = OwnerScope(tenant_id, user_id)
        if kind not in {"free", "paper"} or not prompt_version:
            raise ValueError("Invalid document organization metadata")
        if not isinstance(payload, dict) or not isinstance(markdown, str):
            raise ValueError("Invalid document organization result")
        if len(markdown.encode("utf-8")) > 256 * 1024:
            raise ValueError("Organized document exceeds 256 KiB")
        encoded_payload = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        )
        if len(encoded_payload.encode("utf-8")) > 256 * 1024:
            raise ValueError("Organized document payload exceeds 256 KiB")
        async with get_session() as session:
            result = await session.execute(
                text(
                    "UPDATE rd_agent_docs SET status='organized',organize_state='completed',"
                    "organized_text=:markdown,organized_payload=CAST(:payload AS jsonb),"
                    "organize_kind=:kind,organize_prompt_version=:prompt_version,"
                    "organized_at=:now,error=NULL,updated_at=:now "
                    "WHERE tenant_id=:tenant_id AND user_id=:user_id AND doc_id=:doc_id "
                    "AND organize_job_id=:job_id AND status IN ('parsed','organized') "
                    "AND organize_state IN ('queued','running','stopping')"
                ),
                {
                    **owner.sql_params(),
                    "doc_id": doc_id,
                    "job_id": job_id,
                    "markdown": markdown,
                    "payload": encoded_payload,
                    "kind": kind,
                    "prompt_version": prompt_version[:64],
                    "now": utc_now(),
                },
            )
            return result.rowcount == 1

    async def fail_organize(
        self,
        doc_id: str,
        *,
        tenant_id: str,
        user_id: str,
        job_id: str,
        state: str,
        error: str | None = None,
    ) -> bool:
        owner = OwnerScope(tenant_id, user_id)
        if state not in {"failed", "cancelled", "stopping"}:
            raise ValueError("Invalid document organization terminal state")
        async with get_session() as session:
            result = await session.execute(
                text(
                    "UPDATE rd_agent_docs SET organize_state=:state,error=:error,"
                    "updated_at=:now WHERE tenant_id=:tenant_id AND user_id=:user_id "
                    "AND doc_id=:doc_id AND organize_job_id=:job_id "
                    "AND organize_state IN ('queued','running','stopping')"
                ),
                {
                    **owner.sql_params(),
                    "doc_id": doc_id,
                    "job_id": job_id,
                    "state": state,
                    "error": error[:1000] if error else None,
                    "now": utc_now(),
                },
            )
            return result.rowcount == 1

    async def save_organized_draft(
        self,
        doc_id: str,
        *,
        tenant_id: str,
        user_id: str,
        organized_text: str,
    ) -> bool:
        owner = OwnerScope(tenant_id, user_id)
        if not isinstance(organized_text, str) or not organized_text.strip():
            raise ValueError("Organized draft cannot be empty")
        if len(organized_text.encode("utf-8")) > 256 * 1024:
            raise ValueError("Organized draft exceeds 256 KiB")
        async with get_session() as session:
            result = await session.execute(
                text(
                    "UPDATE rd_agent_docs SET organized_text=:organized_text,updated_at=:now "
                    "WHERE tenant_id=:tenant_id AND user_id=:user_id AND doc_id=:doc_id "
                    "AND status='organized' AND organize_state='completed'"
                ),
                {
                    **owner.sql_params(),
                    "doc_id": doc_id,
                    "organized_text": organized_text,
                    "now": utc_now(),
                },
            )
            return result.rowcount == 1

    async def mark_deleted(self, doc_id: str, *, tenant_id: str, user_id: str) -> bool:
        owner = OwnerScope(tenant_id, user_id)
        async with get_session() as session:
            result = await session.execute(
                text(
                    "UPDATE rd_agent_docs SET status='deleted',updated_at=:now "
                    "WHERE tenant_id=:tenant_id AND user_id=:user_id "
                    "AND doc_id=:doc_id AND status <> 'deleted'"
                ),
                {**owner.sql_params(), "doc_id": doc_id, "now": utc_now()},
            )
            return result.rowcount == 1


__all__ = ["DOC_STATUSES", "DocStore"]
