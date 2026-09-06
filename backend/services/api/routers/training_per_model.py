"""按模型独立训练入口（共享层 + 模型独立配置）。

- 13 个 POST /training/{model}，由 build_per_model_router 工厂一次生成，
  在用户态（/api/v1/models）与管理态（/api/v1/admin/models）各挂一套。
- 每个端点只收自家 schema（extra="forbid"）：跨模型 params 当场 422。
- handler 薄到只有：补 model_type → submit_training_job（共享核心；
  深度校验/编排/回调全复用，submit 内部 import 避免本模块拖重依赖）。
- 旧 /run-training 保留：多模型（model_types）/ensemble 走它。
"""

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends

from backend.shared.training.per_model import MODEL_FRAMEWORK, REQUEST_MODELS


def build_per_model_router(auth_dep: Callable[..., Any]) -> APIRouter:
    """生成 13 个独立模型训练入口。auth_dep 为鉴权依赖（用户态/管理态各一）。"""
    router = APIRouter()

    for model_name, schema_cls in REQUEST_MODELS.items():

        def _make_handler(model_name: str = model_name, schema_cls: type = schema_cls):
            async def run_model_training(
                payload: schema_cls,  # 闭包绑定的具体 schema 类（非字符串注解，FastAPI 可解析）
                background_tasks: BackgroundTasks,
                current_user: dict = Depends(auth_dep),
            ):
                from backend.services.api.routers.admin.admin_training_utils import (
                    submit_training_job,
                )

                data = payload.model_dump(mode="json", exclude_none=True)
                data["model_type"] = model_name
                return await submit_training_job(data, background_tasks, current_user)

            run_model_training.__name__ = f"run_training_{model_name}"
            run_model_training.__doc__ = (
                f"启动 {model_name} 模型训练任务（独立入口，{MODEL_FRAMEWORK[model_name]}）。"
                "只接受本模型 params 容器，跨模型参数直接 422。"
            )
            return run_model_training

        router.add_api_route(
            f"/training/{model_name}",
            _make_handler(),
            methods=["POST"],
            summary=f"启动{model_name}模型训练任务（独立入口）",
        )
    return router
