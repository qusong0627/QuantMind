"""Owner-scoped personal paper vault: upload, keep and delete papers."""

from __future__ import annotations

import asyncio
import hashlib
import re
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from sqlalchemy import text
from starlette.datastructures import UploadFile

from backend.services.engine.alpha_agent.doc_api import (
    _artifact_path,
    _artifact_root,
    _delete_file_and_usage,
)
from backend.services.engine.auth_context import get_authenticated_identity
from backend.shared.database_manager_v2 import get_session
from backend.shared.owner_scope import OwnerScope

router = APIRouter()

MAX_PAPER_BYTES = 20 * 1024 * 1024
MAX_MULTIPART_OVERHEAD = 256 * 1024
_SAFE_PAPER_FILENAME = re.compile(r"^[^\x00-\x1f\x7f/\\]{1,255}\.pdf$", re.IGNORECASE)
_PAPER_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _owner(request: Request) -> tuple[str, str]:
    user_id, tenant_id = get_authenticated_identity(request)
    if not tenant_id or not user_id:
        raise HTTPException(status_code=401, detail="Authenticated owner is required")
    return str(tenant_id), str(user_id)


def _public(row: dict) -> dict:
    return {
        "paper_id": row["paper_id"],
        "filename": row["filename"],
        "size_bytes": int(row["size_bytes"]),
        "sha256": row["sha256"],
        "created_at": row["created_at"],
    }


@router.post("/papers")
async def upload_paper(request: Request) -> dict:
    tenant_id, user_id = _owner(request)
    raw_length = request.headers.get("content-length")
    if raw_length is None:
        raise HTTPException(status_code=411, detail="上传请求必须包含 Content-Length")
    try:
        content_length = int(raw_length)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Content-Length 无效") from exc
    if content_length < 1 or content_length > MAX_PAPER_BYTES + MAX_MULTIPART_OVERHEAD:
        raise HTTPException(status_code=413, detail="PDF 最大 20 MiB")

    try:
        form = await request.form(max_files=1, max_fields=0)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="无法解析上传表单") from exc
    try:
        values = form.getlist("file")
        if len(values) != 1 or not isinstance(values[0], UploadFile):
            raise HTTPException(status_code=400, detail="缺少 PDF 文件（multipart 字段名 file）")
        upload = values[0]
        filename = upload.filename or ""
        if not _SAFE_PAPER_FILENAME.fullmatch(filename) or filename.startswith("."):
            raise HTTPException(status_code=400, detail="文件名无效或格式不是 PDF")
        chunks: list[bytes] = []
        size = 0
        while chunk := await upload.read(min(1024 * 1024, MAX_PAPER_BYTES + 1 - size)):
            size += len(chunk)
            if size > MAX_PAPER_BYTES:
                raise HTTPException(status_code=413, detail="PDF 最大 20 MiB")
            chunks.append(chunk)
        content = b"".join(chunks)
        if not content.startswith(b"%PDF-"):
            raise HTTPException(status_code=400, detail="文件内容不是 PDF")
    finally:
        await form.close()

    paper_id = uuid4().hex
    storage_key = f"alpha_research/papers/{paper_id}/original.pdf"
    digest = hashlib.sha256(content).hexdigest()
    try:
        from backend.shared.storage_quota_ledger import write_quota_managed_local_file

        await asyncio.to_thread(
            write_quota_managed_local_file,
            root=_artifact_root(),
            tenant_id=tenant_id,
            user_id=user_id,
            relative_path=storage_key,
            content=content,
            resource_namespace="pipeline_artifacts",
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail="存储配额或论文空间暂不可用") from exc

    try:
        owner = OwnerScope(tenant_id, user_id)
        async with get_session() as session:
            await session.execute(
                text(
                    "INSERT INTO user_papers "
                    "(tenant_id,user_id,paper_id,filename,size_bytes,sha256,storage_key) "
                    "VALUES (:tenant_id,:user_id,:paper,:filename,:size,:sha,:key)"
                ),
                {
                    **owner.sql_params(),
                    "paper": paper_id,
                    "filename": filename[:255],
                    "size": len(content),
                    "sha": digest,
                    "key": storage_key,
                },
            )
    except Exception as exc:
        try:
            await _delete_file_and_usage(tenant_id, user_id, storage_key)
        except HTTPException:
            pass
        raise HTTPException(status_code=503, detail="论文登记失败，请稍后重试") from exc

    return {
        "code": 200,
        "data": {
            "paper_id": paper_id,
            "filename": filename[:255],
            "size_bytes": len(content),
            "sha256": digest,
        },
    }


@router.get("/papers")
async def list_papers(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    tenant_id, user_id = _owner(request)
    owner = OwnerScope(tenant_id, user_id)
    async with get_session(read_only=True) as session:
        rows = (
            await session.execute(
                text(
                    "SELECT paper_id,filename,size_bytes,sha256,created_at "
                    "FROM user_papers WHERE tenant_id=:tenant_id AND user_id=:user_id "
                    "ORDER BY created_at DESC, paper_id DESC LIMIT :limit OFFSET :offset"
                ),
                {**owner.sql_params(), "limit": limit, "offset": offset},
            )
        ).mappings().all()
    return {"code": 200, "data": [_public(dict(row)) for row in rows]}


@router.get("/papers/{paper_id}")
async def get_paper(paper_id: str, request: Request, response: Response) -> dict:
    tenant_id, user_id = _owner(request)
    if not _PAPER_ID.fullmatch(paper_id):
        raise HTTPException(status_code=404, detail="论文不存在")
    owner = OwnerScope(tenant_id, user_id)
    async with get_session(read_only=True) as session:
        row = (
            await session.execute(
                text(
                    "SELECT paper_id,filename,size_bytes,sha256,created_at "
                    "FROM user_papers "
                    "WHERE tenant_id=:tenant_id AND user_id=:user_id AND paper_id=:paper"
                ),
                {**owner.sql_params(), "paper": paper_id},
            )
        ).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="论文不存在")
    response.headers["Cache-Control"] = "private, no-store"
    return {"code": 200, "data": _public(dict(row))}


@router.get("/papers/{paper_id}/download")
async def download_paper(paper_id: str, request: Request):
    tenant_id, user_id = _owner(request)
    if not _PAPER_ID.fullmatch(paper_id):
        raise HTTPException(status_code=404, detail="论文不存在")
    owner = OwnerScope(tenant_id, user_id)
    async with get_session(read_only=True) as session:
        row = (
            await session.execute(
                text(
                    "SELECT filename,storage_key FROM user_papers "
                    "WHERE tenant_id=:tenant_id AND user_id=:user_id AND paper_id=:paper"
                ),
                {**owner.sql_params(), "paper": paper_id},
            )
        ).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="论文不存在")
    try:
        path: Path = _artifact_path(tenant_id, user_id, str(row["storage_key"]))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="论文文件不存在或已清理") from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail="论文文件不存在或已清理")
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=str(row["filename"]),
        headers={"Cache-Control": "private, no-store"},
    )


@router.delete("/papers/{paper_id}")
async def delete_paper(paper_id: str, request: Request, response: Response) -> dict:
    tenant_id, user_id = _owner(request)
    if not _PAPER_ID.fullmatch(paper_id):
        raise HTTPException(status_code=404, detail="论文不存在")
    owner = OwnerScope(tenant_id, user_id)
    async with get_session(read_only=True) as session:
        row = (
            await session.execute(
                text(
                    "SELECT storage_key FROM user_papers "
                    "WHERE tenant_id=:tenant_id AND user_id=:user_id AND paper_id=:paper"
                ),
                {**owner.sql_params(), "paper": paper_id},
            )
        ).mappings().first()
        if row is None:
            raise HTTPException(status_code=404, detail="论文不存在")
        storage_key = str(row["storage_key"])
    # Delete the artifact first: a quota failure keeps the row for a clean retry
    # instead of orphaning the file.
    try:
        await _delete_file_and_usage(tenant_id, user_id, storage_key)
    except HTTPException as exc:
        raise HTTPException(status_code=503, detail="论文文件清理失败，请稍后重试") from exc
    async with get_session() as session:
        result = await session.execute(
            text(
                "DELETE FROM user_papers "
                "WHERE tenant_id=:tenant_id AND user_id=:user_id AND paper_id=:paper"
            ),
            {**owner.sql_params(), "paper": paper_id},
        )
        if result.rowcount != 1:
            raise HTTPException(status_code=409, detail="论文已被删除或发生变化")
    response.headers["Cache-Control"] = "private, no-store"
    return {"code": 200, "data": {"paper_id": paper_id, "deleted": True}}
