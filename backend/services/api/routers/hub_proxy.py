"""
模型广场（Model Hub）反向代理 与 本地模型发布

1. 反向代理：/api/v1/hub/* 转发到远程量化模型社区广场（quantdb.quantmind.cloud），
   写入类操作注入服务端已配置的 QUANTDB_API_KEY（见 shared/runtime_secrets.py）。

2. 本地模型发布：/api/v1/hub/publish-local 由后端在容器内完成「打包模型目录(tar.gz)
   → 申请上传凭据 → 直传 COS → 激活发布」整个流程。模型文件只存在于后端容器，
   前端无法读取，因此真正的压缩与上传都发生在后端。
"""

import io
import json
import os
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel

from backend.services.api.user_app.middleware.auth import get_current_user
from backend.shared.model_registry import model_registry_service
from backend.shared.runtime_secrets import get_secret

router = APIRouter(tags=["HubProxy"])

HUB_BASE_URL = os.getenv("QUANTDB_HUB_URL", "https://quantdb.quantmind.cloud").rstrip("/")

_HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
}

# 远端只接受 gzip 压缩的模型包
_UPLOAD_CONTENT_TYPE = "application/gzip"


class PublishLocalRequest(BaseModel):
    model_id: str
    name: str
    description: str = ""
    market: str = "CN"
    algorithm: str = "CatBoost"
    target_horizon: str = "T+5"
    target_mode: str = "classification"
    test_ic: float = 0.0
    rank_ic: float = 0.0
    sharpe_ratio: float = 0.0
    annual_return: float = 0.0
    max_drawdown: float = 0.0
    calmar_ratio: float = 0.0
    visibility: str = "public"


def _forward_headers(request: Request) -> dict[str, str]:
    return {k: v for k, v in request.headers.items() if k.lower() not in _HOP_HEADERS}


def _require_api_key() -> str:
    api_key = get_secret("QUANTDB_API_KEY")
    if not api_key:
        raise HTTPException(
            status_code=400,
            detail="QUANTDB_API_KEY 未配置，无法访问模型广场。请在「个人中心 → 数据平台 QuantDB」中配置 API Key。",
        )
    return api_key


def _build_model_archive(model_dir: Path) -> bytes:
    """把模型目录（metadata.json + 模型文件等）打包成内存中的 tar.gz。"""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for p in sorted(model_dir.rglob("*")):
            if p.is_file():
                tar.add(p, arcname=p.relative_to(model_dir).as_posix())
    return buf.getvalue()


# ── 注意顺序：具体的 /api/v1/hub/publish-local 必须先于下方的 catch-all 注册，
# ── 否则会被 {path:path} 捕获、误当普通广场接口代理到远端而 404。


@router.post("/api/v1/hub/publish-local", summary="打包并发布本地模型到广场")
async def publish_local_model(
    req: PublishLocalRequest,
    current_user: dict[str, Any] = Depends(get_current_user),
):
    tenant_id = str(current_user.get("tenant_id") or "default")
    user_id = str(current_user.get("user_id") or current_user.get("sub") or "")

    api_key = _require_api_key()

    model = await model_registry_service.get_model(
        tenant_id=tenant_id, user_id=user_id, model_id=req.model_id
    )
    if not model:
        raise HTTPException(status_code=404, detail=f"未找到本地模型 {req.model_id}")
    if model.get("status") not in ("ready", "active"):
        raise HTTPException(
            status_code=422, detail=f"模型状态为 {model.get('status')}，暂不可发布"
        )

    model_dir = Path(model.get("storage_path") or "")
    if not model_dir.is_dir():
        raise HTTPException(
            status_code=404, detail=f"模型文件目录不存在: {model_dir}"
        )

    # 1. 打包模型目录（tar.gz）
    archive = _build_model_archive(model_dir)

    hub_headers = {"X-API-Key": api_key}
    timeout = httpx.Timeout(connect=5.0, read=120.0, write=180.0, pool=10.0)

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            # 2. 申请上传凭据
            ticket_payload = {
                "name": req.name,
                "description": req.description,
                "market": req.market,
                "algorithm": req.algorithm,
                "target_horizon": req.target_horizon,
                "target_mode": req.target_mode,
                "test_ic": req.test_ic,
                "rank_ic": req.rank_ic,
                "sharpe_ratio": req.sharpe_ratio,
                "annual_return": req.annual_return,
                "max_drawdown": req.max_drawdown,
                "calmar_ratio": req.calmar_ratio,
                "visibility": req.visibility,
                "file_size_bytes": len(archive),
            }
            ticket_resp = await client.post(
                f"{HUB_BASE_URL}/api/v1/hub/models/upload-ticket",
                json=ticket_payload,
                headers=hub_headers,
            )
            if ticket_resp.status_code >= 300:
                raise HTTPException(
                    status_code=502,
                    detail=f"获取上传凭据失败({ticket_resp.status_code}): {ticket_resp.text[:300]}",
                )
            ticket = ticket_resp.json()
            hub_model_id = ticket.get("model_id")
            upload_url = ticket.get("upload_url")
            if not hub_model_id or not upload_url:
                raise HTTPException(status_code=502, detail="上传凭据缺少 model_id/upload_url")

            # 3. 直传模型包（gzip）
            upload_resp = await client.put(
                upload_url,
                content=archive,
                headers={"Content-Type": _UPLOAD_CONTENT_TYPE},
            )
            if upload_resp.status_code >= 300:
                raise HTTPException(
                    status_code=502,
                    detail=f"模型包上传失败({upload_resp.status_code}): {upload_resp.text[:300]}",
                )

            # 4. 激活发布
            publish_resp = await client.post(
                f"{HUB_BASE_URL}/api/v1/hub/models/{hub_model_id}/publish",
                headers=hub_headers,
            )
            if publish_resp.status_code >= 300:
                raise HTTPException(
                    status_code=502,
                    detail=f"发布激活失败({publish_resp.status_code}): {publish_resp.text[:300]}",
                )
            try:
                publish_detail = publish_resp.json()
            except Exception:  # noqa: BLE001
                publish_detail = {}
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"模型广场服务不可达: {exc}") from exc

    return {
        "success": True,
        "model_id": hub_model_id,
        "packaged_size": len(archive),
        "detail": publish_detail,
    }


@router.api_route("/api/v1/hub/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_model_hub(path: str, request: Request):
    api_key = _require_api_key()

    upstream_url = f"{HUB_BASE_URL}/api/v1/hub/{path}"
    if request.url.query:
        upstream_url += f"?{request.url.query}"

    method = request.method.upper()
    headers = _forward_headers(request)
    headers["X-API-Key"] = api_key
    body = await request.body()

    timeout = httpx.Timeout(connect=5.0, read=60.0, write=60.0, pool=10.0)

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.request(
                method, upstream_url,
                content=body if body else None,
                headers=headers,
            )
            return Response(
                content=resp.content,
                status_code=resp.status_code,
                headers={"content-type": resp.headers.get("content-type", "application/json")},
            )
    except httpx.HTTPError:
        return PlainTextResponse("模型广场服务不可达", status_code=502)
