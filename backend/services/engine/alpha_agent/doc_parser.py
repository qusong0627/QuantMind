"""Owner-scoped execution of one MinerU document parse job."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
import stat
import tempfile
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from backend.shared.owner_path import owner_segment, user_scoped_dir
from backend.shared.secret_redaction import redact_text
from backend.services.engine.alpha_agent.doc_store import DocStore, decode_original_parts
from backend.services.engine.alpha_agent.mineru_client import (
    MODE_LOCAL,
    MineruClient,
    MineruBatchItem,
    MineruFileSpec,
    MAX_PAGES_PER_FILE,
    count_pdf_pages,
    extract_zip_whitelist,
    resolve_mineru_config,
)

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 15
MAX_PARSE_SECONDS = 2 * 60 * 60
MAX_RESULT_FILES = 2000


class DocumentParseError(RuntimeError):
    """A document parse failed with a safe, user-displayable message."""


class DocumentParseCancelled(RuntimeError):
    """The external MinerU task reached a terminal state after cancellation."""


def _safe_source_parts(
    doc: dict[str, Any], *, tenant_id: str, user_id: str
) -> list[dict[str, Any]]:
    from backend.services.engine.alpha_agent.doc_api import _artifact_path

    try:
        parts = decode_original_parts(doc)
        for part in parts:
            path = _artifact_path(tenant_id, user_id, part["storage_key"])
            mode = os.lstat(path).st_mode
            if (
                stat.S_ISLNK(mode)
                or not stat.S_ISREG(mode)
                or os.stat(path).st_nlink != 1
            ):
                raise DocumentParseError("原始文档文件不安全")
            if path.stat().st_size != part["size_bytes"]:
                raise DocumentParseError("原始文档大小与登记信息不一致")
            digest = hashlib.sha256()
            with path.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    digest.update(chunk)
            if digest.hexdigest() != part["sha256"]:
                raise DocumentParseError("原始文档校验失败")
            part["path"] = path
        return parts
    except OSError as exc:
        raise DocumentParseError("原始文档文件不可用") from exc


def _part_data_id(doc_id: str, index: int, total: int) -> str:
    return doc_id if total == 1 else f"{doc_id}_p{index}"


def _match_part_results(
    doc_id: str, parts: list[dict[str, Any]], items: list[MineruBatchItem]
) -> list[MineruBatchItem | None]:
    by_data_id: dict[str, MineruBatchItem] = {}
    for item in items:
        if item.data_id:
            by_data_id.setdefault(str(item.data_id), item)
    matches: list[MineruBatchItem | None] = []
    for index, _part in enumerate(parts, start=1):
        match = by_data_id.get(_part_data_id(doc_id, index, len(parts)))
        matches.append(match)
    return matches


def _make_client(config):
    if config.mode == MODE_LOCAL:
        from backend.services.engine.alpha_agent.mineru_local import MineruLocalClient

        return MineruLocalClient(config)
    return MineruClient(config)


def _result_files(root: Path) -> list[Path]:
    paths = sorted(root.rglob("*"))
    files: list[Path] = []
    total_bytes = 0
    for path in paths:
        mode = os.lstat(path).st_mode
        if stat.S_ISDIR(mode):
            continue
        if stat.S_ISLNK(mode) or not stat.S_ISREG(mode) or os.stat(path).st_nlink != 1:
            raise DocumentParseError("解析产物包含不安全文件")
        total_bytes += path.stat().st_size
        if len(files) >= MAX_RESULT_FILES or total_bytes > 512 * 1024 * 1024:
            raise DocumentParseError("解析产物超过存储上限")
        files.append(path)
    if not files or not any(path.name == "full.md" for path in files):
        raise DocumentParseError("解析产物缺少 Markdown 正文")
    return files


async def execute_document_parse(
    request: dict[str, Any],
    *,
    tenant_id: str,
    user_id: str,
    job_id: str,
    progress: Callable[[dict[str, Any]], Awaitable[None]],
    is_cancelled: Callable[[], Awaitable[bool]],
    lifecycle: dict[str, Any],
    docs: DocStore | None = None,
    poll_interval: float = POLL_INTERVAL_SECONDS,
    max_wait_seconds: float = MAX_PARSE_SECONDS,
) -> dict[str, Any]:
    """Submit or resume a parse, persist allowlisted results and honor cancellation.

    Persisted ``parser_batch_id`` is the only resume point after an external
    submission. A crash in the submit/upload window is deliberately not retried
    blindly because that could create a second billable MinerU task.
    """
    # OSS-ADAPT: OSS 未安装 MinerU，文档解析不可用。
    raise RuntimeError("OSS 未安装 MinerU，文档解析不可用")
    if not isinstance(request, dict):
        raise DocumentParseError("文档解析任务快照无效")
    doc_id, source_sha256 = request.get("doc_id"), request.get("source_sha256")
    if (
        not isinstance(doc_id, str)
        or not doc_id
        or not isinstance(source_sha256, str)
        or len(source_sha256) != 64
    ):
        raise DocumentParseError("文档解析任务缺少有效文档引用")
    owner_segment(doc_id)
    owner_segment(job_id)
    docs = docs or DocStore()
    doc = await docs.get_doc(doc_id, tenant_id=tenant_id, user_id=user_id)
    if doc is None or doc.get("status") in {"deleted", "expired"}:
        raise DocumentParseError("文档不存在")
    if doc.get("sha256") != source_sha256:
        raise DocumentParseError("任务引用的原始文档版本已变化")
    if doc.get("parse_job_id") not in (None, job_id):
        raise DocumentParseError("文档已关联其他解析任务")

    batch_id = doc.get("parser_batch_id")
    if doc.get("status") in {"uploaded", "parse_failed"}:
        if not await docs.begin_parse(
            doc_id, tenant_id=tenant_id, user_id=user_id, job_id=job_id
        ):
            raise DocumentParseError("文档状态已变化，请刷新后重试")
        doc = await docs.get_doc(doc_id, tenant_id=tenant_id, user_id=user_id)
        if doc is None:
            raise DocumentParseError("文档不存在")
    elif doc.get("status") != "parsing":
        raise DocumentParseError("当前文档状态不支持解析")
    elif not batch_id:
        lifecycle["create_started"] = True
        raise DocumentParseError("解析提交状态无法确认，请重新提交解析")
    else:
        # A persisted batch ID proves that an external MinerU operation may
        # still be live; retain the engine lease on interruption/timeout.
        lifecycle["create_started"] = True

    config = resolve_mineru_config()
    if config is None:
        raise DocumentParseError("MinerU 解析通道未配置")
    from backend.services.engine.alpha_agent.doc_api import (
        _artifact_path,
        _artifact_root,
    )

    owner_root = user_scoped_dir(_artifact_root(), tenant_id, user_id)
    staging_root = _artifact_path(
        tenant_id,
        user_id,
        f"alpha_research/docs/{doc_id}/.staging-marker",
    ).parent
    owner_root.mkdir(parents=True, exist_ok=True)
    staging_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".parse-{job_id}-", dir=staging_root))
    client = _make_client(config)
    published: list[str] = []
    try:
        if not batch_id:
            if await is_cancelled():
                await docs.update_doc(
                    doc_id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    expected_status="parsing",
                    status="parse_failed",
                    parse_job_id=job_id,
                    parse_state="cancelled",
                    error="解析已取消",
                )
                lifecycle["confirmed_stopped"] = True
                raise DocumentParseCancelled("Document parsing cancelled")
            parts = _safe_source_parts(doc, tenant_id=tenant_id, user_id=user_id)
            if not parts:
                raise DocumentParseError("文档没有可解析的原件")
            specs: list[MineruFileSpec] = []
            for index, part in enumerate(parts, start=1):
                pages = await asyncio.to_thread(
                    lambda source=part["path"]: count_pdf_pages(source.read_bytes())
                )
                if pages is not None and pages > MAX_PAGES_PER_FILE:
                    raise DocumentParseError(
                        f"{part['filename']} 共 {pages} 页，MinerU 单文件上限为 {MAX_PAGES_PER_FILE} 页"
                    )
                specs.append(
                    MineruFileSpec(
                        name=str(part["filename"]),
                        data_id=_part_data_id(doc_id, index, len(parts)),
                        size_bytes=int(part["size_bytes"]),
                    )
                )
            lifecycle["create_started"] = True
            batch_id, urls = await client.create_upload_batch(specs)
            if len(urls) != len(parts):
                raise DocumentParseError("MinerU 上传链接数量异常")
            for part, url in zip(parts, urls, strict=True):
                await client.upload_file(url, part["path"])
            parser_job_id = await client.finalize_submission(batch_id)
            if not await docs.record_parser_batch(
                doc_id,
                tenant_id=tenant_id,
                user_id=user_id,
                job_id=job_id,
                batch_id=parser_job_id,
            ):
                raise DocumentParseError("解析任务关联已变化，无法安全继续")
            batch_id = parser_job_id
            await progress({"stage": "document_parse_submitted", "progress_pct": 5})

        deadline = time.monotonic() + max(1.0, min(max_wait_seconds, MAX_PARSE_SECONDS))
        parts = decode_original_parts(doc)
        matched: list[MineruBatchItem | None] = [None] * len(parts)
        cancel_requested = False
        while time.monotonic() < deadline:
            cancel_requested = cancel_requested or await is_cancelled()
            items = await client.get_batch_results(str(batch_id))
            matched = _match_part_results(doc_id, parts, items)
            all_present = all(item is not None for item in matched)
            all_terminal = all_present and all(item.is_terminal for item in matched if item)
            if not all_present:
                await docs.update_parse_progress(
                    doc_id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    job_id=job_id,
                    parse_state="pending",
                )
            elif all_terminal:
                break
            else:
                pages = sum(int(item.total_pages or 0) for item in matched if item)
                await docs.update_parse_progress(
                    doc_id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    job_id=job_id,
                    parse_state="running",
                    page_count=pages or None,
                )
                await progress({"stage": "document_parse_running", "progress_pct": 10})
            if cancel_requested:
                # MinerU has no supported cancellation endpoint. Keep the lease
                # while polling until its task is terminal before confirming stop.
                await asyncio.sleep(max(1.0, poll_interval))
                continue
            await asyncio.sleep(max(1.0, poll_interval))

        if not all(item is not None and item.is_terminal for item in matched):
            raise DocumentParseError("MinerU 解析仍未到达终态，保留任务供恢复器核验")
        lifecycle["confirmed_stopped"] = True
        completed_items = [item for item in matched if item is not None]
        if cancel_requested or await is_cancelled():
            await docs.update_doc(
                doc_id,
                tenant_id=tenant_id,
                user_id=user_id,
                expected_status="parsing",
                status="parse_failed",
                parse_job_id=job_id,
                parse_state="cancelled",
                error="解析已取消",
            )
            lifecycle["confirmed_stopped"] = True
            raise DocumentParseCancelled("Document parsing cancelled")
        failed_items = [item for item in completed_items if item.state == "failed"]
        if failed_items:
            failures = [
                redact_text(item.err_msg or "MinerU 文档解析失败")[:500]
                for item in failed_items
            ]
            message = "；".join(failures)[:1000]
            await docs.update_doc(
                doc_id,
                tenant_id=tenant_id,
                user_id=user_id,
                expected_status="parsing",
                status="parse_failed",
                parse_job_id=job_id,
                parse_state="failed",
                error=message,
            )
            raise DocumentParseError(message)
        if not completed_items or any(item.state != "done" for item in completed_items):
            raise DocumentParseError("MinerU 批次返回了未知终态")
        pages_total = sum(int(item.total_pages or 0) for item in completed_items)
        image_count = 0
        if len(parts) == 1:
            item = completed_items[0]
            if not item.full_zip_url:
                raise DocumentParseError("解析完成但没有产物下载地址")
            zip_path = staging / "mineru.zip"
            await client.download_zip(item.full_zip_url, zip_path)
            extracted_root = staging / "extracted"
            extraction = await asyncio.to_thread(
                extract_zip_whitelist, zip_path, extracted_root
            )
            artifact_root = extracted_root
            image_count = extraction.image_count
            files = _result_files(extracted_root)
        else:
            from backend.services.engine.alpha_agent.doc_merge import (
                MergePart,
                merge_parts as merge_documents,
            )

            merge_inputs: list[MergePart] = []
            for index, (part, item) in enumerate(zip(parts, completed_items, strict=True), start=1):
                if not item.full_zip_url:
                    raise DocumentParseError(f"{part['filename']} 解析完成但没有产物下载地址")
                zip_path = staging / f"mineru-p{index}.zip"
                part_root = staging / "parts" / f"p{index}"
                await client.download_zip(item.full_zip_url, zip_path)
                await asyncio.to_thread(
                    extract_zip_whitelist, zip_path, part_root
                )
                merge_inputs.append(
                    MergePart(name=str(part["filename"]), parsed_dir=part_root)
                )
            merged_root = staging / "merged"
            merged = await asyncio.to_thread(merge_documents, merge_inputs, merged_root)
            artifact_root = merged_root
            image_count = merged.image_count
            files = _result_files(merged_root)
        from backend.shared.storage_quota_ledger import write_quota_managed_local_file

        relative_prefix = Path("alpha_research") / "docs" / doc_id / "parsed"
        for path in files:
            relative = path.relative_to(artifact_root)
            key = (relative_prefix / relative).as_posix()
            await asyncio.to_thread(
                write_quota_managed_local_file,
                root=_artifact_root(),
                tenant_id=tenant_id,
                user_id=user_id,
                relative_path=key,
                content=path,
                resource_namespace="pipeline_artifacts",
            )
            published.append(key)

        md_key = f"{relative_prefix.as_posix()}/full.md"
        content_keys = [key for key in published if key.endswith("_content_list.json")]
        if await is_cancelled():
            await docs.update_doc(
                doc_id,
                tenant_id=tenant_id,
                user_id=user_id,
                expected_status="parsing",
                status="parse_failed",
                parse_job_id=job_id,
                parse_state="cancelled",
                error="解析已取消",
            )
            lifecycle["confirmed_stopped"] = True
            raise DocumentParseCancelled("Document parsing cancelled")
        updated = await docs.update_doc(
            doc_id,
            tenant_id=tenant_id,
            user_id=user_id,
            expected_status="parsing",
            status="parsed",
            parse_job_id=job_id,
            parse_state="done",
            page_count=pages_total or None,
            markdown_key=md_key,
            content_list_key=content_keys[0] if len(parts) == 1 and content_keys else None,
            parsed_artifact_keys=published,
            error=None,
        )
        if not updated:
            raise DocumentParseError("文档已删除或状态变化，解析结果已丢弃")
        await progress({"stage": "document_parse_completed", "progress_pct": 100})
        return {
            "doc_id": doc_id,
            "status": "parsed",
            "page_count": pages_total,
            "artifact_count": len(published),
            "image_count": image_count,
        }
    except Exception:
        if published:
            from backend.services.engine.alpha_agent.doc_api import (
                _delete_file_and_usage,
            )

            for key in reversed(published):
                try:
                    await _delete_file_and_usage(tenant_id, user_id, key)
                except Exception:
                    logger.exception("Failed to roll back document parse artifact")
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        await client.aclose()


__all__ = ["DocumentParseCancelled", "DocumentParseError", "execute_document_parse"]
