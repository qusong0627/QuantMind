"""Owner-scoped durable execution for document organization jobs."""

from __future__ import annotations

import asyncio
import os
import stat
from collections.abc import Awaitable, Callable
from typing import Any

from backend.shared.owner_path import user_scoped_dir
from backend.shared.secret_redaction import redact_text
from backend.services.engine.alpha_agent.doc_organize import (
    ORGANIZE_KINDS,
    PROMPT_VERSION,
    OrganizeError,
    _default_chat_factory,
    organize_document,
)
from backend.services.engine.alpha_agent.doc_store import DocStore

MAX_ORGANIZE_SOURCE_BYTES = 5 * 1024 * 1024


class DocumentOrganizeCancelled(OrganizeError):
    """The active LLM call returned and the organization job was stopped."""


def _read_source(
    storage_key: str, *, tenant_id: str, user_id: str
) -> str:
    from backend.services.engine.alpha_agent.doc_api import (
        _artifact_path,
        _artifact_root,
    )

    path = _artifact_path(tenant_id, user_id, storage_key)
    owner_root = user_scoped_dir(_artifact_root().absolute(), tenant_id, user_id)
    if not path.resolve(strict=True).is_relative_to(owner_root.resolve(strict=True)):
        raise OrganizeError("解析产物不属于当前文档属主目录")
    mode = os.lstat(path).st_mode
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode) or os.stat(path).st_nlink != 1:
        raise OrganizeError("解析产物文件不安全")
    if path.stat().st_size > MAX_ORGANIZE_SOURCE_BYTES:
        raise OrganizeError("解析文本超过整理上限，请先缩短文档")
    return path.read_text(encoding="utf-8", errors="replace")


async def execute_document_organization(
    request: dict[str, Any],
    *,
    tenant_id: str,
    user_id: str,
    job_id: str,
    progress: Callable[[dict[str, Any]], Awaitable[None]],
    is_cancelled: Callable[[], Awaitable[bool]],
    lifecycle: dict[str, Any],
    docs: DocStore | None = None,
) -> dict[str, Any]:
    if not isinstance(request, dict):
        raise OrganizeError("文档整理任务快照无效")
    doc_id = request.get("doc_id")
    source_sha256 = request.get("source_sha256")
    markdown_key = request.get("markdown_key")
    kind = request.get("organize_kind")
    extra = request.get("extra")
    prompt_version = request.get("prompt_version")
    if (
        not isinstance(doc_id, str)
        or not isinstance(source_sha256, str)
        or not isinstance(markdown_key, str)
        or kind not in ORGANIZE_KINDS
        or not isinstance(prompt_version, str)
        or (extra is not None and not isinstance(extra, str))
    ):
        raise OrganizeError("文档整理任务缺少有效的文档引用或整理口径")
    docs = docs or DocStore()
    doc = await docs.get_doc(doc_id, tenant_id=tenant_id, user_id=user_id)
    if (
        doc is None
        or doc.get("status") not in {"parsed", "organized"}
        or doc.get("tenant_id") != tenant_id
        or doc.get("user_id") != user_id
        or doc.get("sha256") != source_sha256
        or doc.get("markdown_key") != markdown_key
        or doc.get("organize_job_id") != job_id
    ):
        raise OrganizeError("文档版本或属主与整理任务快照不匹配")
    if prompt_version != PROMPT_VERSION:
        await docs.fail_organize(
            doc_id,
            tenant_id=tenant_id,
            user_id=user_id,
            job_id=job_id,
            state="failed",
            error="整理模板已升级，请重新提交",
        )
        raise OrganizeError("整理模板版本已变化，请重新提交任务")
    if not await docs.set_organize_running(
        doc_id, tenant_id=tenant_id, user_id=user_id, job_id=job_id
    ):
        await docs.fail_organize(
            doc_id,
            tenant_id=tenant_id,
            user_id=user_id,
            job_id=job_id,
            state="failed",
            error="文档整理状态已变化",
        )
        raise OrganizeError("文档整理状态已变化，请刷新后重试")
    try:
        text = await asyncio.to_thread(
            _read_source, markdown_key, tenant_id=tenant_id, user_id=user_id
        )
        from backend.services.engine.alpha_agent.llm_client import resolve_llm_config

        config = resolve_llm_config()
        if config is None:
            raise OrganizeError("LLM 整理服务未配置，请联系管理员")
        base_chat = _default_chat_factory(config)
        call_count = 0

        async def tracked_chat(messages, **kwargs) -> str:
            nonlocal call_count
            if await is_cancelled():
                await docs.fail_organize(
                    doc_id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    job_id=job_id,
                    state="cancelled",
                    error="整理任务已取消",
                )
                lifecycle["confirmed_stopped"] = True
                raise DocumentOrganizeCancelled("Document organization cancelled")
            call_count += 1
            lifecycle["create_started"] = True
            await progress(
                {"stage": "document_organize_llm", "llm_calls": call_count}
            )
            try:
                result = await base_chat(messages, **kwargs)
            except Exception:
                # The HTTP call has returned an error; no local request remains live.
                lifecycle["confirmed_stopped"] = True
                raise
            lifecycle["confirmed_stopped"] = True
            if await is_cancelled():
                await docs.fail_organize(
                    doc_id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    job_id=job_id,
                    state="cancelled",
                    error="整理任务已取消",
                )
                raise DocumentOrganizeCancelled("Document organization cancelled")
            return result

        result = await organize_document(
            text=text,
            kind=kind,
            extra=extra,
            config=config,
            chat_fn=tracked_chat,
        )
        if await is_cancelled():
            await docs.fail_organize(
                doc_id,
                tenant_id=tenant_id,
                user_id=user_id,
                job_id=job_id,
                state="cancelled",
                error="整理任务已取消",
            )
            raise DocumentOrganizeCancelled("Document organization cancelled")
        return {
            "doc_id": doc_id,
            "status": "organized",
            "organize_kind": result["kind"],
            "prompt_version": result["prompt_version"],
            "truncated": result["truncated"],
            "chunks_used": result["chunks_used"],
            "_organization_result": {
                "kind": result["kind"],
                "prompt_version": result["prompt_version"],
                "payload": result["payload"],
                "markdown": result["markdown"],
            },
        }
    except DocumentOrganizeCancelled:
        raise
    except Exception as exc:
        await docs.fail_organize(
            doc_id,
            tenant_id=tenant_id,
            user_id=user_id,
            job_id=job_id,
            state="failed",
            error=redact_text(str(exc))[:1000],
        )
        raise
