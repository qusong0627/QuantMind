"""Authenticated, owner-scoped upload and retrieval API for research PDFs."""

from __future__ import annotations

import asyncio
import hashlib
import io
import os
import re
import stat
import zipfile
from pathlib import Path, PurePosixPath
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.datastructures import UploadFile

from backend.services.engine.auth_context import get_authenticated_identity
from backend.services.engine.alpha_agent.doc_store import (
    DocStore,
    _public_row,
    _storage_key,
    decode_original_parts,
)
from backend.shared.owner_path import user_scoped_dir

router = APIRouter()
_docs = DocStore()
MAX_DOCUMENT_BYTES = 20 * 1024 * 1024
MAX_DOCUMENT_FILES = 10
MAX_MULTIPART_OVERHEAD = 256 * 1024
MAX_MARKDOWN_PREVIEW_BYTES = 512 * 1024
_SAFE_FILENAME = re.compile(r"^[^\x00-\x1f\x7f/\\]{1,255}\.pdf$", re.IGNORECASE)


class OrganizedDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    organized_text: str = Field(min_length=1, max_length=256 * 1024)


def _owner(request: Request) -> tuple[str, str]:
    user_id, tenant_id = get_authenticated_identity(request)
    return tenant_id, user_id


def _artifact_root() -> Path:
    return Path(os.getenv("PIPELINE_ARTIFACTS_DIR", "/data/pipeline_artifacts"))


def _artifact_path(tenant_id: str, user_id: str, storage_key: str) -> Path:
    relative = PurePosixPath(_storage_key(storage_key))
    root = _artifact_root().absolute()
    owner_root = user_scoped_dir(root, tenant_id, user_id)
    target = owner_root.joinpath(*relative.parts)
    root_resolved = root.resolve()
    if not target.resolve(strict=False).is_relative_to(root_resolved):
        raise ValueError("Document artifact path escaped owner directory")
    directories = [
        directory
        for directory in target.parents
        if directory == root or directory.is_relative_to(root)
    ]
    for directory in reversed(directories):
        try:
            mode = os.lstat(directory).st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise ValueError("Document artifact path contains an unsafe directory")
    return target


def _unlink_artifact(tenant_id: str, user_id: str, storage_key: str) -> None:
    path = _artifact_path(tenant_id, user_id, storage_key)
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode) or os.stat(path).st_nlink != 1:
        raise ValueError("Document artifact is not a private regular file")
    path.unlink()


async def _delete_file_and_usage(
    tenant_id: str, user_id: str, storage_key: str
) -> None:
    try:
        await asyncio.to_thread(_unlink_artifact, tenant_id, user_id, storage_key)
        from backend.shared.storage_quota_ledger import (
            delete_local_file_usage,
            pipeline_artifact_resource_key,
        )

        resource_key = pipeline_artifact_resource_key(tenant_id, user_id, storage_key)

        await asyncio.to_thread(
            delete_local_file_usage, tenant_id, user_id, resource_key
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail="文档清理待恢复") from exc


@router.post("/docs")
async def upload_document(request: Request) -> dict:
    tenant_id, user_id = _owner(request)
    raw_length = request.headers.get("content-length")
    if raw_length is None:
        raise HTTPException(status_code=411, detail="上传请求必须包含 Content-Length")
    try:
        content_length = int(raw_length)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Content-Length 无效") from exc
    if (
        content_length < 1
        or content_length > MAX_DOCUMENT_BYTES + MAX_MULTIPART_OVERHEAD
    ):
        raise HTTPException(status_code=413, detail="PDF 最大 20 MiB")

    try:
        form = await request.form(max_files=MAX_DOCUMENT_FILES, max_fields=0)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="无法解析上传表单") from exc
    try:
        uploads = form.getlist("file")
        if not uploads or any(not isinstance(value, UploadFile) for value in uploads):
            raise HTTPException(status_code=400, detail="缺少 PDF 文件（multipart 字段名 file）")
        if len(uploads) > MAX_DOCUMENT_FILES:
            raise HTTPException(
                status_code=400,
                detail=f"单次最多上传 {MAX_DOCUMENT_FILES} 个 PDF 文件",
            )
        parts: list[dict] = []
        total_size = 0
        composite = hashlib.sha256()
        for index, upload in enumerate(uploads, start=1):
            filename = upload.filename or ""
            if not _SAFE_FILENAME.fullmatch(filename) or filename.startswith("."):
                raise HTTPException(status_code=400, detail="文件名无效或格式不是 PDF")
            chunks: list[bytes] = []
            size = 0
            while chunk := await upload.read(
                min(1024 * 1024, MAX_DOCUMENT_BYTES + 1 - size)
            ):
                size += len(chunk)
                total_size += len(chunk)
                if size > MAX_DOCUMENT_BYTES or total_size > MAX_DOCUMENT_BYTES:
                    raise HTTPException(
                        status_code=413, detail="单次 PDF 上传合计最大 20 MiB"
                    )
                chunks.append(chunk)
            content = b"".join(chunks)
            if not content.startswith(b"%PDF-"):
                raise HTTPException(status_code=400, detail="文件内容不是 PDF")
            digest = hashlib.sha256(content).hexdigest()
            composite.update(f"{filename}\0{digest}\0".encode())
            parts.append(
                {
                    "filename": filename,
                    "content": content,
                    "size_bytes": len(content),
                    "sha256": digest,
                    "ext": "pdf",
                    "storage_key": (
                        "alpha_research/docs/{doc_id}/original.pdf"
                        if len(uploads) == 1
                        else f"alpha_research/docs/{{doc_id}}/originals/p{index}.pdf"
                    ),
                }
            )
    finally:
        await form.close()

    doc_id = str(uuid4())
    for part in parts:
        part["storage_key"] = part["storage_key"].format(doc_id=doc_id)
    relative_key = parts[0]["storage_key"]
    payload_sha256 = (
        parts[0]["sha256"] if len(parts) == 1 else composite.hexdigest()
    )
    display_name = parts[0]["filename"]
    if len(parts) > 1:
        display_name = f"{display_name}（共{len(parts)}个文件）"[:255]
    written_keys: list[str] = []
    try:
        from backend.shared.storage_quota_ledger import write_quota_managed_local_file

        for part in parts:
            await asyncio.to_thread(
                write_quota_managed_local_file,
                root=_artifact_root(),
                tenant_id=tenant_id,
                user_id=user_id,
                relative_path=part["storage_key"],
                content=part["content"],
                resource_namespace="pipeline_artifacts",
            )
            written_keys.append(part["storage_key"])
    except Exception as exc:
        for key in reversed(written_keys):
            try:
                await _delete_file_and_usage(tenant_id, user_id, key)
            except HTTPException:
                pass
        raise HTTPException(
            status_code=503, detail="存储配额或文档空间暂不可用"
        ) from exc

    try:
        created = await _docs.create_doc(
            tenant_id=tenant_id,
            user_id=user_id,
            doc_id=doc_id,
            filename=display_name,
            ext="pdf",
            size_bytes=total_size,
            sha256=payload_sha256,
            storage_key=relative_key,
            original_parts=(
                [
                    {key: value for key, value in part.items() if key != "content"}
                    for part in parts
                ]
                if len(parts) > 1
                else None
            ),
        )
        if not created:
            raise RuntimeError("Document ID collision")
    except Exception as exc:
        for key in reversed(written_keys):
            await _delete_file_and_usage(tenant_id, user_id, key)
        raise HTTPException(status_code=503, detail="文档登记失败，请稍后重试") from exc

    return {
        "code": 200,
        "data": {
            "doc_id": doc_id,
            "filename": display_name,
            "size_bytes": total_size,
            "sha256": payload_sha256,
            "files_count": len(parts),
            "status": "uploaded",
        },
    }


@router.get("/docs")
async def list_documents(
    request: Request,
    status: str | None = Query(default=None, max_length=32),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    tenant_id, user_id = _owner(request)
    try:
        rows = await _docs.list_docs(
            tenant_id=tenant_id,
            user_id=user_id,
            status=status,
            limit=limit,
            offset=offset,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "code": 200,
        "data": {
            "items": [_public_row(row) for row in rows[:limit]],
            "has_more": len(rows) > limit,
            "limit": limit,
            "offset": offset,
        },
    }


@router.get("/docs/{doc_id}")
async def get_document(doc_id: str, request: Request) -> dict:
    tenant_id, user_id = _owner(request)
    row = await _docs.get_doc(doc_id, tenant_id=tenant_id, user_id=user_id)
    if row is None or row["status"] in {"deleted", "expired"}:
        raise HTTPException(status_code=404, detail="文档不存在")
    return {"code": 200, "data": _public_row(row)}


@router.get("/docs/{doc_id}/original")
async def download_document(doc_id: str, request: Request) -> Response:
    tenant_id, user_id = _owner(request)
    row = await _docs.get_doc(doc_id, tenant_id=tenant_id, user_id=user_id)
    if row is None or row["status"] in {"deleted", "expired"}:
        raise HTTPException(status_code=404, detail="文档不存在")
    if int(row.get("files_count") or 1) > 1:
        try:
            parts = decode_original_parts(row)
            archive = io.BytesIO()
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
                for index, part in enumerate(parts, start=1):
                    path = _artifact_path(tenant_id, user_id, part["storage_key"])
                    mode = os.lstat(path).st_mode
                    if (
                        stat.S_ISLNK(mode)
                        or not stat.S_ISREG(mode)
                        or os.stat(path).st_nlink != 1
                        or path.stat().st_size != part["size_bytes"]
                    ):
                        raise ValueError("Unsafe document part")
                    digest = hashlib.sha256(path.read_bytes()).hexdigest()
                    if digest != part["sha256"]:
                        raise ValueError("Document part hash mismatch")
                    bundle.write(path, arcname=f"part-{index}/{part['filename']}")
            return Response(
                content=archive.getvalue(),
                media_type="application/zip",
                headers={
                    "Content-Disposition": 'attachment; filename="research-documents.zip"',
                    "Cache-Control": "private, no-store",
                    "X-Content-Type-Options": "nosniff",
                },
            )
        except (OSError, ValueError, KeyError) as exc:
            raise HTTPException(status_code=404, detail="文档原件不可用") from exc
    try:
        path = _artifact_path(tenant_id, user_id, row["storage_key"])
        mode = os.lstat(path).st_mode
        if stat.S_ISLNK(mode) or not stat.S_ISREG(mode) or os.stat(path).st_nlink != 1:
            raise ValueError("Unsafe document file")
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="文档文件不可用") from exc
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=row["filename"],
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/docs/{doc_id}/markdown")
async def preview_document_markdown(doc_id: str, request: Request) -> dict:
    tenant_id, user_id = _owner(request)
    row = await _docs.get_doc(doc_id, tenant_id=tenant_id, user_id=user_id)
    if row is None or row["status"] in {"deleted", "expired"}:
        raise HTTPException(status_code=404, detail="文档不存在")
    if row["status"] not in {"parsed", "organized"} or not row.get("markdown_key"):
        raise HTTPException(status_code=409, detail="文档尚未完成解析")
    try:
        path = _artifact_path(tenant_id, user_id, row["markdown_key"])
        mode = os.lstat(path).st_mode
        if stat.S_ISLNK(mode) or not stat.S_ISREG(mode) or os.stat(path).st_nlink != 1:
            raise ValueError("Unsafe Markdown artifact")

        def read_preview() -> tuple[str, bool]:
            with path.open("rb") as source:
                content = source.read(MAX_MARKDOWN_PREVIEW_BYTES + 1)
            truncated = len(content) > MAX_MARKDOWN_PREVIEW_BYTES
            return (
                content[:MAX_MARKDOWN_PREVIEW_BYTES].decode("utf-8", errors="replace"),
                truncated,
            )

        content, truncated = await asyncio.to_thread(read_preview)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="解析产物不可用") from exc
    return {
        "code": 200,
        "data": {
            "doc_id": doc_id,
            "content": content,
            "truncated": truncated,
            "max_bytes": MAX_MARKDOWN_PREVIEW_BYTES,
        },
    }


@router.put("/docs/{doc_id}/organized-text")
async def save_organized_draft(
    doc_id: str, payload: OrganizedDraftRequest, request: Request
) -> dict:
    tenant_id, user_id = _owner(request)
    try:
        saved = await _docs.save_organized_draft(
            doc_id,
            tenant_id=tenant_id,
            user_id=user_id,
            organized_text=payload.organized_text,
        )
    except ValueError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    if not saved:
        row = await _docs.get_doc(doc_id, tenant_id=tenant_id, user_id=user_id)
        if row is None or row["status"] in {"deleted", "expired"}:
            raise HTTPException(status_code=404, detail="文档不存在")
        raise HTTPException(status_code=409, detail="整理草稿状态已变化，请刷新后重试")
    row = await _docs.get_doc(doc_id, tenant_id=tenant_id, user_id=user_id)
    if row is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    return {"code": 200, "data": _public_row(row)}


@router.delete("/docs/{doc_id}")
async def delete_document(doc_id: str, request: Request) -> dict:
    tenant_id, user_id = _owner(request)
    row = await _docs.get_doc(doc_id, tenant_id=tenant_id, user_id=user_id)
    if row is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    if row["status"] == "parsing":
        raise HTTPException(
            status_code=409, detail="解析任务结束或确认停止后才能删除文档"
        )
    if row.get("organize_state") in {"queued", "running", "stopping"}:
        raise HTTPException(
            status_code=409, detail="整理任务结束或确认停止后才能删除文档"
        )
    if row["status"] != "deleted" and not await _docs.mark_deleted(
        doc_id, tenant_id=tenant_id, user_id=user_id
    ):
        latest = await _docs.get_doc(doc_id, tenant_id=tenant_id, user_id=user_id)
        if latest is None or latest["status"] != "deleted":
            raise HTTPException(status_code=409, detail="文档状态已变化，请刷新后重试")
        row = latest
    artifact_keys = row.get("parsed_artifact_keys") or []
    if not isinstance(artifact_keys, list):
        raise HTTPException(status_code=503, detail="文档产物清单异常，清理待恢复")
    try:
        original_keys = [part["storage_key"] for part in decode_original_parts(row)]
    except (TypeError, ValueError, KeyError) as exc:
        raise HTTPException(status_code=503, detail="文档原件清单异常，清理待恢复") from exc
    keys = list(dict.fromkeys([row["storage_key"], *original_keys, *artifact_keys]))
    for storage_key in keys:
        await _delete_file_and_usage(tenant_id, user_id, storage_key)
    return {"code": 200, "data": {"doc_id": doc_id, "status": "deleted"}}


__all__ = ["router"]
