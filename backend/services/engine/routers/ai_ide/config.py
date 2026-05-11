import logging
import os
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from backend.shared.auth import get_internal_call_secret

logger = logging.getLogger(__name__)
router = APIRouter()


class LLMConfig(BaseModel):
    qwen_api_key: str


def _get_user_info(request: Request):
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user


def _get_api_gateway_url():
    """获取 API Gateway URL，OSS 模式下使用 127.0.0.1"""
    # 优先使用环境变量
    url = os.getenv("INTERNAL_API_GATEWAY_URL", "")
    if url:
        return url
    # OSS 单容器模式，所有服务在同一容器内
    return "http://127.0.0.1:8000"


@router.get("/llm")
async def get_llm_config(request: Request):
    """获取 LLM 配置状态（从用户 Profile 中读取并脱敏）"""
    user = _get_user_info(request)
    user_id = user["user_id"]
    tenant_id = user.get("tenant_id", "default")

    api_gateway = _get_api_gateway_url()

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            headers = {
                "X-Internal-Call": get_internal_call_secret(),
                "X-User-Id": user_id,
                "X-Tenant-Id": tenant_id,
            }
            # 调用 Gateway 的 profiles 接口获取详情
            resp = await client.get(f"{api_gateway}/api/v1/profiles/{user_id}", headers=headers)
            if resp.status_code == 200:
                body = resp.json()
                data = body.get("data", {})
                key = data.get("api_key")
                has_key = bool(key and key.strip())
                masked = f"{key[:3]}****{key[-4:]}" if has_key and len(key) > 8 else ""
                return {
                    "success": True,
                    "has_key": has_key,
                    "masked_key": masked,
                }
            else:
                logger.warning(f"Failed to fetch profile: {resp.status_code} {resp.text}")
    except Exception as e:
        logger.error(f"Failed to fetch profile for user {user_id}: {e}")

    return {"success": True, "has_key": False, "masked_key": ""}


@router.post("/llm")
async def save_llm_config(request: Request, config: LLMConfig):
    """保存 LLM API Key，同步到用户 Profile"""
    user = _get_user_info(request)
    user_id = user["user_id"]
    tenant_id = user.get("tenant_id", "default")

    new_key = config.qwen_api_key.strip()
    if not new_key:
        raise HTTPException(status_code=400, detail="API Key 不能为空")

    api_gateway = _get_api_gateway_url()

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            headers = {
                "X-Internal-Call": get_internal_call_secret(),
                "X-User-Id": user_id,
                "X-Tenant-Id": tenant_id,
            }
            # 更新 Profile (使用网关内部已有的 profiles/{user_id} 接口)
            resp = await client.put(
                f"{api_gateway}/api/v1/profiles/{user_id}",
                headers=headers,
                json={"api_key": new_key},
            )
            if resp.status_code != 200:
                logger.error(f"Failed to update profile for user {user_id}: {resp.text}")
                raise HTTPException(status_code=resp.status_code, detail="同步到用户服务失败")

        return {"success": True, "message": "配置已成功同步到个人档案"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to save config for user {user_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
