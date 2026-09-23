import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from collections.abc import Iterable
from uuid import uuid4

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

from backend.services.api.routers.proxy_error_mapping import (
    map_upstream_http_error,
)
from backend.services.api.user_app.middleware.auth import get_current_user
from backend.shared.trusted_headers import sanitize_forward_headers

router = APIRouter(tags=["OpenClaw-QwenPaw"])

QWENPAW_BASE_URL = os.getenv("QWENPAW_BASE_URL", "http://qwenpaw:8088").rstrip("/")
QWENPAW_CHANNEL = os.getenv("QWENPAW_CHANNEL", "console")
QWENPAW_TIMEOUT_SECONDS = float(os.getenv("QWENPAW_TIMEOUT_SECONDS", "60"))
QWENPAW_SHARED_FILES_DIR = os.getenv("QWENPAW_SHARED_FILES_DIR", "/qwenpaw-shared").rstrip("/")
QWENPAW_SHARED_VISIBLE_DIR = os.getenv("QWENPAW_SHARED_VISIBLE_DIR", "/app/working").rstrip("/")
OPENCLAW_MAX_FILE_SIZE_BYTES = int(os.getenv("OPENCLAW_MAX_FILE_SIZE_BYTES", str(50 * 1024 * 1024)))

# QwenPaw 上游认证信息
QWENPAW_AUTH_USERNAME = os.getenv("QWENPAW_AUTH_USERNAME", "")
QWENPAW_AUTH_PASSWORD = os.getenv("QWENPAW_AUTH_PASSWORD", "")

# 服务间令牌缓存
_QWENPAW_TOKEN: str | None = None
_QWENPAW_TOKEN_EXPIRY: float = 0

_ALLOWED_UPLOAD_SUFFIXES = {
    ".pdf",
    ".doc",
    ".docx",
    ".txt",
    ".md",
    ".csv",
    ".tsv",
    ".xlsx",
    ".xlsm",
    ".xls",
    ".ppt",
    ".pptx",
    ".json",
    ".xml",
    ".html",
    ".htm",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
}


class OpenClawChatRequest(BaseModel):
    message: str
    session_id: str | None = None
    user_id: str | None = None
    attachments: list["OpenClawAttachment"] = []


class OpenClawAttachment(BaseModel):
    file_id: str
    original_name: str
    file_name: str
    file_size: int = 0
    content_type: str = "application/octet-stream"
    file_path: str
    uploaded_at: str | None = None


class OpenClawCreateSessionRequest(BaseModel):
    title: str | None = "新对话"
    session_id: str | None = None
    user_id: str | None = None


class OpenClawUpdateSessionTitleRequest(BaseModel):
    title: str
    user_id: str | None = None


class OpenClawDeleteFileRequest(BaseModel):
    user_id: str | None = None


OpenClawChatRequest.model_rebuild()


def _sanitize_headers(headers: Iterable, bearer_token: str | None = None) -> dict[str, str]:
    """客户端头 → QwenPaw 上游。

    2026-09-23 收紧：此前只过滤逐跳头，客户端自带的信任头会被透传。
    `bearer_token` 是**服务端**另行取得的凭证，与客户端头无关。
    """
    out = sanitize_forward_headers(headers)
    if bearer_token:
        out["Authorization"] = f"Bearer {bearer_token}"
    return out


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_user_id(current_user: dict, explicit_user_id: str | None = None) -> str:
    resolved = current_user.get("user_id") or current_user.get("sub") or explicit_user_id
    if resolved is None:
        raise HTTPException(status_code=400, detail="Missing user_id")
    return str(resolved)


def _qwenpaw_url(path: str) -> str:
    return f"{QWENPAW_BASE_URL}{path}"


def _safe_segment(value: str) -> str:
    sanitized = re.sub(r"[^a-zA-Z0-9._-]", "_", value or "")
    sanitized = sanitized.strip("._")
    return sanitized or "unknown"


async def _get_qwenpaw_token() -> str | None:
    """获取并缓存 QwenPaw 系统的 JWT 令牌"""
    global _QWENPAW_TOKEN, _QWENPAW_TOKEN_EXPIRY
    now = time.time()

    # 如果缓存未过期（预留 60 秒缓冲区），直接返回
    if _QWENPAW_TOKEN and now < (_QWENPAW_TOKEN_EXPIRY - 60):
        return _QWENPAW_TOKEN

    # 如果环境变量本身不存在 (None)，则报错；允许空字符串（表示无密码）
    if not QWENPAW_AUTH_USERNAME:
        # 如果未配置用户名，视作未启用上游认证，返回 None 尝试免密
        return None

    print(f"[OpenClaw] Initiating service login to QwenPaw for user: {QWENPAW_AUTH_USERNAME}")
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{QWENPAW_BASE_URL}/api/auth/login",
                json={
                    "username": QWENPAW_AUTH_USERNAME,
                    "password": QWENPAW_AUTH_PASSWORD
                }
            )
            if resp.status_code != 200:
                print(f"[OpenClaw] QwenPaw login failed: {resp.status_code} {resp.text}")
                raise HTTPException(status_code=500, detail="QwenPaw upstream login failed")

            data = resp.json()
            token = data.get("token")
            if not token:
                print(f"[OpenClaw] QwenPaw login returned NO token (Auth may be disabled). Response: {data}")
                # 如果无 Token，返回 None 而不是抛出异常，以便后续尝试免密请求
                return None

            _QWENPAW_TOKEN = token
            # 默认缓存 1 小时 (3600s)
            _QWENPAW_TOKEN_EXPIRY = now + 3600
            return _QWENPAW_TOKEN
    except Exception as e:
        if isinstance(e, HTTPException):
            raise e
        print(f"[OpenClaw] Error during QwenPaw login: {e}")
        # 使用统一的异常映射，而不是强制 500
        raise map_upstream_http_error("qwenpaw", e) from None


def _attachments_root() -> Path:
    root = Path(QWENPAW_SHARED_FILES_DIR) / "quantmind_uploads"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _attachment_meta_root() -> Path:
    root = _attachments_root() / "_meta"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _session_attachment_dir(user_id: str, session_id: str) -> Path:
    directory = _attachments_root() / _safe_segment(user_id) / _safe_segment(session_id)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _visible_session_dir(user_id: str, session_id: str) -> str:
    return "/".join(
        [
            QWENPAW_SHARED_VISIBLE_DIR.rstrip("/"),
            "quantmind_uploads",
            _safe_segment(user_id),
            _safe_segment(session_id),
        ]
    )


def _session_attachment_index_path(user_id: str, session_id: str) -> Path:
    meta_dir = _attachment_meta_root() / _safe_segment(user_id) / _safe_segment(session_id)
    meta_dir.mkdir(parents=True, exist_ok=True)
    return meta_dir / "attachments.json"


def _load_session_attachments(user_id: str, session_id: str) -> list[dict[str, Any]]:
    index_path = _session_attachment_index_path(user_id, session_id)
    if not index_path.exists():
        return []
    try:
        return json.loads(index_path.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_session_attachments(user_id: str, session_id: str, attachments: list[dict[str, Any]]) -> None:
    index_path = _session_attachment_index_path(user_id, session_id)
    index_path.write_text(
        json.dumps(attachments, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _normalize_attachment(attachment: dict[str, Any]) -> dict[str, Any]:
    return {
        "file_id": str(attachment.get("file_id") or ""),
        "original_name": str(attachment.get("original_name") or ""),
        "file_name": str(attachment.get("file_name") or ""),
        "file_size": int(attachment.get("file_size") or 0),
        "content_type": str(attachment.get("content_type") or "application/octet-stream"),
        "file_path": str(attachment.get("file_path") or ""),
        "uploaded_at": str(attachment.get("uploaded_at") or _now_iso()),
    }


def _build_attachment_prompt(
    attachments: list[OpenClawAttachment],
) -> str:
    if not attachments:
        return ""
    lines = [
        "你可以直接读取以下附件文件并据此完成用户请求。",
        "如需引用文件内容，请优先基于这些本地路径操作。",
    ]
    for attachment in attachments:
        lines.append(

                f"- 文件名: {attachment.original_name} | "
                f"路径: {attachment.file_path} | "
                f"类型: {attachment.content_type} | "
                f"大小: {attachment.file_size} 字节"

        )
    return "\n".join(lines)


def _validate_upload_filename(filename: str) -> str:
    if not filename:
        raise HTTPException(status_code=400, detail="文件名不能为空")
    suffix = Path(filename).suffix.lower()
    if suffix not in _ALLOWED_UPLOAD_SUFFIXES:
        raise HTTPException(
            status_code=400,
            detail=f"暂不支持的文件类型: {suffix or 'unknown'}",
        )
    return suffix


async def _qwenpaw_request(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
    timeout: httpx.Timeout | None = None,
) -> httpx.Response:
    request_timeout = timeout or httpx.Timeout(
        connect=5.0,
        read=QWENPAW_TIMEOUT_SECONDS,
        write=QWENPAW_TIMEOUT_SECONDS,
        pool=10.0,
    )

    # 获取服务间令牌
    token = await _get_qwenpaw_token()

    try:
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        async with httpx.AsyncClient(timeout=request_timeout) as client:
            response = await client.request(
                method,
                _qwenpaw_url(path),
                params=params,
                json=json_body,
                headers=_sanitize_headers(
                    headers.items()
                ),
            )
    except httpx.HTTPError as exc:
        raise map_upstream_http_error("qwenpaw", exc) from None

    if response.status_code >= 400:
        body = response.text[:1000]
        print(f"[OpenClaw] QwenPaw error {response.status_code}: {body}")

        # 如果是 401，可能是 Token 失效，强制清空缓存以便下次重试
        if response.status_code == 401:
            global _QWENPAW_TOKEN
            _QWENPAW_TOKEN = None

        raise HTTPException(
            status_code=response.status_code,
            detail={
                "message": "qwenpaw upstream returned error",
                "service": "qwenpaw",
                "status_code": response.status_code,
                "body": body,
            },
        )
    return response


async def _resolve_chat(user_id: str, session_id: str) -> dict[str, Any]:
    response = await _qwenpaw_request(
        "GET",
        "/api/chats",
        params={"user_id": user_id, "channel": QWENPAW_CHANNEL},
    )
    chats = response.json() or []
    for chat in chats:
        if str(chat.get("session_id") or "") == session_id:
            return chat
    raise HTTPException(status_code=404, detail="Session not found")


def _map_session(chat: dict[str, Any]) -> dict[str, Any]:
    timestamp = _now_iso()
    meta = chat.get("meta") or {}
    return {
        "session_id": str(chat.get("session_id") or ""),
        "user_id": str(chat.get("user_id") or ""),
        "title": str(chat.get("name") or "新对话"),
        "created_at": str(chat.get("created_at") or meta.get("created_at") or timestamp),
        "updated_at": str(chat.get("updated_at") or meta.get("updated_at") or timestamp),
        "message_count": int(chat.get("message_count") or meta.get("message_count") or 0),
        "last_message": str(chat.get("last_message") or meta.get("last_message") or ""),
        "upstream_chat_id": str(chat.get("id") or ""),
    }


def _extract_text_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [_extract_text_content(item) for item in content]
        return "".join(part for part in parts if part)
    if isinstance(content, dict):
        if isinstance(content.get("text"), str):
            return str(content["text"])
        if isinstance(content.get("content"), (str, list, dict)):
            return _extract_text_content(content.get("content"))
        return ""
    return str(content)


def _map_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for message in messages:
        role = str(message.get("role") or "assistant")
        normalized.append(
            {
                "role": role,
                "content": _extract_text_content(message.get("content")),
                "timestamp": str(message.get("timestamp") or _now_iso()),
            }
        )
    return normalized


@router.post("/api/v1/openclaw/chat", include_in_schema=False)
async def openclaw_chat(
    payload: OpenClawChatRequest,
    current_user: dict = Depends(get_current_user),
):
    user_id = _resolve_user_id(current_user, payload.user_id)
    session_id = payload.session_id or str(uuid4())
    attachment_prompt = _build_attachment_prompt(payload.attachments)
    message_text = payload.message.strip()
    if attachment_prompt:
        message_text = f"{attachment_prompt}\n\n用户请求：{message_text}"
    upstream_chat_id = session_id
    try:
        chat = await _resolve_chat(user_id, session_id)
        upstream_chat_id = str(chat.get("id") or session_id)
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
    qwenpaw_payload = {
        "input": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": message_text,
                    }
                ],
            }
        ],
        "chat_id": upstream_chat_id,
        "user_id": user_id,
        "session_id": session_id,
        "sender_id": user_id,
        "channel": QWENPAW_CHANNEL,
        "stream": True,
    }

    # 获取服务间令牌
    token = await _get_qwenpaw_token()

    try:
        client = httpx.AsyncClient(timeout=None)
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        request = client.build_request(
            "POST",
            _qwenpaw_url("/api/agent/process"),
            json=qwenpaw_payload,
            headers=headers
        )
        response = await client.send(request, stream=True)
    except httpx.HTTPError as exc:
        raise map_upstream_http_error("qwenpaw", exc) from None

    if response.status_code >= 400:
        # 如果是 401，强制清空 Token 缓存
        if response.status_code == 401:
            global _QWENPAW_TOKEN
            _QWENPAW_TOKEN = None

        body = await response.aread()
        await response.aclose()
        await client.aclose()
        raise HTTPException(
            status_code=response.status_code,
            detail={
                "message": "qwenpaw upstream returned error",
                "service": "qwenpaw",
                "status_code": response.status_code,
                "body": body.decode("utf-8", errors="ignore")[:1000],
            },
        )

    print(f"[OpenClaw] Chat request accepted: session_id={session_id}, user_id={user_id}")

    async def _cleanup() -> None:
        await response.aclose()
        await client.aclose()

    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    }

    return StreamingResponse(
        response.aiter_raw(),
        status_code=response.status_code,
        media_type="text/event-stream",
        headers=headers,
        background=BackgroundTask(_cleanup),
    )


@router.post("/api/v1/openclaw/files/upload", include_in_schema=False)
async def upload_openclaw_file(
    file: UploadFile = File(...),
    session_id: str = Form(...),
    user_id: str | None = Form(None),
    current_user: dict = Depends(get_current_user),
):
    # 调试日志：记录接收到的字段
    print(f"[OpenClaw] File upload request - session_id: {session_id}, user_id: {user_id}, filename: {file.filename}, content_type: {file.content_type}")

    resolved_user_id = _resolve_user_id(current_user, user_id)
    _validate_upload_filename(file.filename or "")

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="文件内容不能为空")
    if len(data) > OPENCLAW_MAX_FILE_SIZE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"文件大小超过限制（{OPENCLAW_MAX_FILE_SIZE_BYTES} 字节）",
        )

    file_id = str(uuid4())
    safe_name = _safe_segment(Path(file.filename or "").stem)
    suffix = Path(file.filename or "").suffix.lower()
    stored_name = f"{safe_name}-{file_id[:8]}{suffix}"
    target_dir = _session_attachment_dir(resolved_user_id, session_id)
    target_path = target_dir / stored_name
    target_path.write_bytes(data)

    attachment = _normalize_attachment(
        {
            "file_id": file_id,
            "original_name": file.filename or stored_name,
            "file_name": stored_name,
            "file_size": len(data),
            "content_type": file.content_type or "application/octet-stream",
            "file_path": (f"{_visible_session_dir(resolved_user_id, session_id)}/" f"{stored_name}"),
            "uploaded_at": _now_iso(),
        }
    )
    attachments = _load_session_attachments(resolved_user_id, session_id)
    attachments.append(attachment)
    _save_session_attachments(resolved_user_id, session_id, attachments)
    print(f"[OpenClaw] File uploaded successfully - file_id: {file_id}, path: {attachment['file_path']}")
    return attachment


@router.get("/api/v1/openclaw/files", include_in_schema=False)
async def list_openclaw_files(
    session_id: str = Query(...),
    current_user: dict = Depends(get_current_user),
):
    user_id = _resolve_user_id(current_user)
    attachments = _load_session_attachments(user_id, session_id)
    return {
        "session_id": session_id,
        "files": [_normalize_attachment(item) for item in attachments],
    }


@router.delete(
    "/api/v1/openclaw/files/{session_id}/{file_id}",
    include_in_schema=False,
)
async def delete_openclaw_file(
    session_id: str,
    file_id: str,
    payload: OpenClawDeleteFileRequest,
    current_user: dict = Depends(get_current_user),
):
    user_id = _resolve_user_id(current_user, payload.user_id)
    attachments = _load_session_attachments(user_id, session_id)
    next_attachments: list[dict[str, Any]] = []
    deleted = False

    for attachment in attachments:
        normalized = _normalize_attachment(attachment)
        if normalized["file_id"] != file_id:
            next_attachments.append(normalized)
            continue

        target_path = _session_attachment_dir(user_id, session_id) / normalized["file_name"]
        if target_path.exists():
            target_path.unlink()
        deleted = True

    if not deleted:
        raise HTTPException(status_code=404, detail="附件不存在")

    _save_session_attachments(user_id, session_id, next_attachments)
    return {"deleted": True, "session_id": session_id, "file_id": file_id}


@router.get("/api/v1/openclaw/push-messages", include_in_schema=False)
async def openclaw_push_messages(
    session_id: str = Query(...),
    current_user: dict = Depends(get_current_user),
):
    user_id = _resolve_user_id(current_user)
    response = await _qwenpaw_request(
        "GET",
        "/api/console/push-messages",
        params={
            "session_id": session_id,
            "user_id": user_id,
            "channel": QWENPAW_CHANNEL,
        },
    )
    data = response.json() or {}
    messages = data.get("messages") or []
    return {"session_id": session_id, "messages": _map_messages(messages)}


@router.get("/api/v1/openclaw/sessions", include_in_schema=False)
async def list_openclaw_sessions(
    current_user: dict = Depends(get_current_user),
):
    user_id = _resolve_user_id(current_user)
    try:
        response = await _qwenpaw_request(
            "GET",
            "/api/chats",
            params={"user_id": user_id, "channel": QWENPAW_CHANNEL},
        )
        data = response.json()
    except Exception as exc:
        # 如果上游服务未部署或不可达，返回空列表实现优雅降级
        print(f"[OpenClaw] Upstream qwenpaw unavailable, returning empty sessions: {exc}")
        return []

    # 兼容处理：如果上游返回的是 {"chats": [...]} 或其他结构
    if isinstance(data, dict) and "chats" in data:
        chats = data["chats"]
    elif isinstance(data, list):
        chats = data
    else:
        print(f"[OpenClaw] Unexpected chats response format for user {user_id}: {type(data)}")
        chats = []

    return [_map_session(chat) for chat in chats if isinstance(chat, dict)]


@router.post("/api/v1/openclaw/sessions", include_in_schema=False)
async def create_openclaw_session(
    payload: OpenClawCreateSessionRequest,
    current_user: dict = Depends(get_current_user),
):
    user_id = _resolve_user_id(current_user, payload.user_id)
    session_id = payload.session_id or str(uuid4())
    request_payload = {
        "name": payload.title or "新对话",
        "session_id": session_id,
        "user_id": user_id,
        "channel": QWENPAW_CHANNEL,
        "meta": {"created_at": _now_iso(), "updated_at": _now_iso()},
    }
    response = await _qwenpaw_request("POST", "/api/chats", json_body=request_payload)
    return _map_session(response.json() or request_payload)


@router.get(
    "/api/v1/openclaw/sessions/{session_id}/messages",
    include_in_schema=False,
)
async def get_openclaw_session_messages(
    session_id: str,
    current_user: dict = Depends(get_current_user),
):
    user_id = _resolve_user_id(current_user)
    chat = await _resolve_chat(user_id, session_id)
    response = await _qwenpaw_request("GET", f"/api/chats/{chat['id']}")
    data = response.json() or {}
    return {
        "session_id": session_id,
        "messages": _map_messages(data.get("messages") or []),
    }


@router.put(
    "/api/v1/openclaw/sessions/{session_id}/title",
    include_in_schema=False,
)
async def update_openclaw_session_title(
    session_id: str,
    payload: OpenClawUpdateSessionTitleRequest,
    current_user: dict = Depends(get_current_user),
):
    user_id = _resolve_user_id(current_user, payload.user_id)
    chat = await _resolve_chat(user_id, session_id)
    # QwenPaw API 只接受 name 字段，不允许额外字段
    request_payload = {
        "name": payload.title,
    }
    await _qwenpaw_request("PUT", f"/api/chats/{chat['id']}", json_body=request_payload)
    return {"updated": True, "session_id": session_id, "title": payload.title}


@router.delete(
    "/api/v1/openclaw/sessions/{session_id}",
    include_in_schema=False,
)
async def delete_openclaw_session(
    session_id: str,
    current_user: dict = Depends(get_current_user),
):
    user_id = _resolve_user_id(current_user)
    chat = await _resolve_chat(user_id, session_id)
    await _qwenpaw_request("DELETE", f"/api/chats/{chat['id']}")
    return {"deleted": True, "session_id": session_id}


@router.get("/api/v1/openclaw/health", include_in_schema=False)
async def openclaw_health(current_user: dict = Depends(get_current_user)):
    _resolve_user_id(current_user)
    started_at = time.perf_counter()

    gateway_status = "healthy"
    qwenpaw_status = "unhealthy"
    qwenpaw_error: str | None = None
    try:
        response = await _qwenpaw_request(
            "GET",
            "/api/chats",
            params={"channel": QWENPAW_CHANNEL},
            timeout=httpx.Timeout(connect=2.0, read=5.0, write=5.0, pool=2.0),
        )
        if response.status_code == 200:
            qwenpaw_status = "healthy"
    except Exception as exc:
        print(f"[OpenClaw] Health check upstream check failed: {exc}")
        qwenpaw_status = "unreachable"
        qwenpaw_error = str(exc)

    latency_ms = round((time.perf_counter() - started_at) * 1000, 2)
    overall_status = "healthy" if qwenpaw_status == "healthy" else "degraded"
    return {
        "status": overall_status,
        "service": "openclaw-gateway",
        "timestamp": _now_iso(),
        "components": {
            "api": {
                "status": gateway_status,
                "latency_ms": latency_ms,
            },
            "qwenpaw": {
                "status": qwenpaw_status,
                "latency_ms": latency_ms,
                "error": qwenpaw_error,
            },
        },
    }
