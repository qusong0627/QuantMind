from fastapi import APIRouter, Depends, HTTPException, status

from backend.services.api.user_app.middleware.auth import get_current_user
from backend.services.api.user_app.schemas.api_key import (
    ApiKeyCreate,
    ApiKeyInfo,
    ApiKeyListResponse,
    ApiKeyResponse,
    ApiKeyRotateSecretResponse,
    ApiKeyUpdate,
)
from backend.services.api.user_app.services.api_key_service import ApiKeyService
from backend.shared.database_manager_v2 import get_session

router = APIRouter(prefix="/api-keys", tags=["API Keys"])


@router.post("", response_model=ApiKeyResponse, status_code=status.HTTP_201_CREATED)
async def create_api_key(data: ApiKeyCreate, current_user: dict = Depends(get_current_user)):
    """
    Create a new API Key.
    The secret key is only returned once.
    """
    async with get_session(read_only=False) as session:
        service = ApiKeyService(session)
        return await service.create_api_key(
            user_id=current_user["user_id"],
            tenant_id=current_user["tenant_id"],
            data=data,
        )


@router.get("", response_model=ApiKeyListResponse)
async def list_api_keys(current_user: dict = Depends(get_current_user)):
    """
    List user's API Keys.
    """
    async with get_session(read_only=True) as session:
        service = ApiKeyService(session)
        keys = await service.get_user_keys(user_id=current_user["user_id"], tenant_id=current_user["tenant_id"])
        return {"items": keys, "count": len(keys)}


@router.put("/{access_key}", response_model=ApiKeyInfo)
async def update_api_key(access_key: str, data: ApiKeyUpdate, current_user: dict = Depends(get_current_user)):
    """
    Update API Key (name, permissions, status).
    """
    async with get_session(read_only=False) as session:
        service = ApiKeyService(session)
        key = await service.update_api_key(user_id=current_user["user_id"], access_key=access_key, data=data)
        if not key:
            raise HTTPException(status_code=404, detail="API Key not found")
        return key


@router.delete("/{access_key}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_api_key(access_key: str, current_user: dict = Depends(get_current_user)):
    """
    Revoke (delete) an API Key.
    """
    async with get_session(read_only=False) as session:
        service = ApiKeyService(session)
        success = await service.delete_api_key(user_id=current_user["user_id"], access_key=access_key)
        if not success:
            raise HTTPException(status_code=404, detail="API Key not found")


@router.post("/init", response_model=ApiKeyInfo, summary="确保用户至少有一个默认交易 API Key")
async def init_default_api_key(current_user: dict = Depends(get_current_user)):
    """
    幂等接口：若该用户尚无 API Key，则自动创建一个名为"交易系统默认Key"的 Key。
    若已存在，则返回最新的一条 Key 的 access_key（secret_key 不再返回）。
    前端在管理员进入「后台管理 → 系统设置 → SDK 接入凭证」时调用此接口。
    """
    user_id = current_user["user_id"]
    tenant_id = current_user["tenant_id"]

    async with get_session(read_only=False) as session:
        service = ApiKeyService(session)
        # 1. 查询是否已存在
        existing_keys = await service.get_user_keys(user_id=user_id, tenant_id=tenant_id)
        if existing_keys:
            # 已存在，返回最晚创建的一条（access_key 可展示，secret 不再返回）
            return existing_keys[0]

        # 2. 不存在，自动生成默认 Key
        create_data = ApiKeyCreate(
            name="交易系统默认Key",
            permissions=[
                "mcp.use",
                "mcp.data.read",
                "mcp.control.read",
                "trade.read",
                "trade.write",
            ],
        )
        created = await service.create_api_key(user_id=user_id, tenant_id=tenant_id, data=create_data)
        # 返回 ApiKeyInfo（不含 secret_key 明文，前端只需显示 access_key）
        return ApiKeyInfo(
            id=created.id,
            access_key=created.access_key,
            name=created.name,
            permissions=created.permissions,
            is_active=created.is_active,
            created_at=created.created_at,
            expires_at=created.expires_at,
            last_used_at=None,
        )


@router.post(
    "/{access_key}/rotate-secret",
    response_model=ApiKeyRotateSecretResponse,
    summary="重置 API Key 的 Secret Key",
)
async def rotate_api_key_secret(
    access_key: str,
    current_user: dict = Depends(get_current_user),
):
    async with get_session(read_only=False) as session:
        service = ApiKeyService(session)
        result = await service.rotate_secret(
            user_id=current_user["user_id"],
            tenant_id=current_user["tenant_id"],
            access_key=access_key,
        )
        if result is None:
            raise HTTPException(status_code=404, detail="API Key not found")
        return result
