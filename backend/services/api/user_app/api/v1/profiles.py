"""
Profile API Routes
"""

from fastapi import APIRouter, Depends, HTTPException, status

from backend.services.api.user_app.middleware.auth import get_current_user
from backend.services.api.user_app.schemas.user import (
    ResponseModel,
    UserProfileResponse,
    UserProfileUpdate,
)
from backend.services.api.user_app.services.profile_service import ProfileService

router = APIRouter(prefix="/profiles")


def get_profile_service() -> ProfileService:
    """获取档案服务"""
    return ProfileService()


@router.get("/{user_id}", response_model=ResponseModel)
async def get_profile(
    user_id: str,
    current_user: dict = Depends(get_current_user),
    profile_service: ProfileService = Depends(get_profile_service),
):
    """
    获取用户档案

    - **user_id**: 用户ID
    """
    # 检查权限
    if user_id != current_user["user_id"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="没有权限访问该用户档案")

    profile = await profile_service.get_profile(user_id, current_user["tenant_id"])

    if not profile:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户档案不存在")

    return {
        "code": 200,
        "message": "success",
        "data": UserProfileResponse.from_orm(profile).dict(),
    }


@router.put("/{user_id}", response_model=ResponseModel)
async def update_profile(
    user_id: str,
    profile_data: UserProfileUpdate,
    current_user: dict = Depends(get_current_user),
    profile_service: ProfileService = Depends(get_profile_service),
):
    """
    更新用户档案

    - **user_id**: 用户ID
    - **profile_data**: 档案更新数据
    """
    # 检查权限
    if user_id != current_user["user_id"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="没有权限修改该用户档案")

    profile = await profile_service.update_profile(user_id, current_user["tenant_id"], profile_data)

    if not profile:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户档案不存在")

    return {
        "code": 200,
        "message": "success",
        "data": UserProfileResponse.from_orm(profile).dict(),
    }


@router.get("/me/profile", response_model=ResponseModel)
async def get_my_profile(
    current_user: dict = Depends(get_current_user),
    profile_service: ProfileService = Depends(get_profile_service),
):
    """
    获取当前用户档案
    """
    user_id = current_user["user_id"]
    profile = await profile_service.get_profile(user_id, current_user["tenant_id"], use_cache=False)

    if not profile:
        # 如果档案不存在，创建一个
        profile = await profile_service.create_profile(user_id, current_user["tenant_id"])

    # profile 已经是 dict，直接使用
    return {
        "code": 200,
        "message": "success",
        "data": UserProfileResponse.model_validate(profile).model_dump(),
    }


@router.put("/me/profile", response_model=ResponseModel)
async def update_my_profile(
    profile_data: UserProfileUpdate,
    current_user: dict = Depends(get_current_user),
    profile_service: ProfileService = Depends(get_profile_service),
):
    """
    更新当前用户档案
    """
    user_id = current_user["user_id"]
    profile = await profile_service.update_profile(user_id, current_user["tenant_id"], profile_data)

    # profile 已经是 dict，直接使用
    return {
        "code": 200,
        "message": "success",
        "data": UserProfileResponse.model_validate(profile).model_dump(),
    }
