import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from docker import DockerClient
from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, text

from backend.services.api.routers.admin.db import TrainingJobRecord
from backend.services.api.user_app.middleware.auth import require_admin
from backend.services.engine.training.local_docker_orchestrator import LocalDockerOrchestrator
from backend.services.engine.training.training_log_stream import TrainingRunLogStream
from backend.shared.database_manager_v2 import get_session
from backend.shared.model_registry import model_registry_service
from .admin_training_utils import *
from .admin_training_utils import _resolve_admin_scope, _SetDefaultModelRequest, _SetStrategyBindingRequest
from .admin_training_utils import get_latest_training_run_for_owner

router = APIRouter(dependencies=[Depends(require_admin)])  # 路由器级认证兜底
logger = logging.getLogger(__name__)
@router.get("/user-models", summary="管理员查看用户模型列表（兼容别名）")
async def admin_list_user_models(
    tenant_id: str | None = None,
    user_id: str | None = None,
    include_archived: bool = False,
    market: str | None = None,
    current_user: dict[str, Any] = Depends(require_admin),
):
    scope_tenant, scope_user = _resolve_admin_scope(
        current_user=current_user,
        tenant_id=tenant_id,
        user_id=user_id,
    )
    items = await model_registry_service.list_models(
        tenant_id=scope_tenant,
        user_id=scope_user,
        include_archived=include_archived,
        market=market,
    )
    return {"tenant_id": scope_tenant, "user_id": scope_user, "items": items, "total": len(items)}


@router.get("/user-models/default", summary="管理员查看用户默认模型（兼容别名）")
async def admin_get_default_model(
    tenant_id: str | None = None,
    user_id: str | None = None,
    current_user: dict[str, Any] = Depends(require_admin),
):
    scope_tenant, scope_user = _resolve_admin_scope(
        current_user=current_user,
        tenant_id=tenant_id,
        user_id=user_id,
    )
    model = await model_registry_service.get_default_model(tenant_id=scope_tenant, user_id=scope_user)
    if not model:
        raise HTTPException(status_code=404, detail="Default model not found")
    return model


@router.patch("/user-models/default", summary="管理员设置用户默认模型（兼容别名）")
async def admin_set_default_model(
    payload: _SetDefaultModelRequest,
    tenant_id: str | None = None,
    user_id: str | None = None,
    current_user: dict[str, Any] = Depends(require_admin),
):
    scope_tenant, scope_user = _resolve_admin_scope(
        current_user=current_user,
        tenant_id=tenant_id,
        user_id=user_id,
    )
    try:
        return await model_registry_service.set_default_model(
            tenant_id=scope_tenant,
            user_id=scope_user,
            model_id=payload.model_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/user-models/{model_id}", summary="管理员查看用户单模型（兼容别名）")
async def admin_get_user_model(
    model_id: str,
    tenant_id: str | None = None,
    user_id: str | None = None,
    current_user: dict[str, Any] = Depends(require_admin),
):
    scope_tenant, scope_user = _resolve_admin_scope(
        current_user=current_user,
        tenant_id=tenant_id,
        user_id=user_id,
    )
    model = await model_registry_service.get_model(tenant_id=scope_tenant, user_id=scope_user, model_id=model_id)
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")
    return model


@router.post("/user-models/{model_id}/archive", summary="管理员归档用户模型（兼容别名）")
async def admin_archive_user_model(
    model_id: str,
    tenant_id: str | None = None,
    user_id: str | None = None,
    current_user: dict[str, Any] = Depends(require_admin),
):
    scope_tenant, scope_user = _resolve_admin_scope(
        current_user=current_user,
        tenant_id=tenant_id,
        user_id=user_id,
    )
    try:
        return await model_registry_service.archive_model(
            tenant_id=scope_tenant,
            user_id=scope_user,
            model_id=model_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get(
    "/user-models/strategy-bindings/{strategy_id}",
    summary="管理员查看用户策略模型绑定（兼容别名）",
)
async def admin_get_strategy_binding(
    strategy_id: str,
    tenant_id: str | None = None,
    user_id: str | None = None,
    current_user: dict[str, Any] = Depends(require_admin),
):
    scope_tenant, scope_user = _resolve_admin_scope(
        current_user=current_user,
        tenant_id=tenant_id,
        user_id=user_id,
    )
    binding = await model_registry_service.get_strategy_binding(
        tenant_id=scope_tenant,
        user_id=scope_user,
        strategy_id=strategy_id,
    )
    if not binding:
        raise HTTPException(status_code=404, detail="Strategy binding not found")
    return binding


@router.put(
    "/user-models/strategy-bindings/{strategy_id}",
    summary="管理员设置用户策略模型绑定（兼容别名）",
)
async def admin_set_strategy_binding(
    strategy_id: str,
    payload: _SetStrategyBindingRequest,
    tenant_id: str | None = None,
    user_id: str | None = None,
    current_user: dict[str, Any] = Depends(require_admin),
):
    scope_tenant, scope_user = _resolve_admin_scope(
        current_user=current_user,
        tenant_id=tenant_id,
        user_id=user_id,
    )
    try:
        return await model_registry_service.set_strategy_binding(
            tenant_id=scope_tenant,
            user_id=scope_user,
            strategy_id=strategy_id,
            model_id=payload.model_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.delete(
    "/user-models/strategy-bindings/{strategy_id}",
    summary="管理员解除用户策略模型绑定（兼容别名）",
)
async def admin_delete_strategy_binding(
    strategy_id: str,
    tenant_id: str | None = None,
    user_id: str | None = None,
    current_user: dict[str, Any] = Depends(require_admin),
):
    scope_tenant, scope_user = _resolve_admin_scope(
        current_user=current_user,
        tenant_id=tenant_id,
        user_id=user_id,
    )
    deleted = await model_registry_service.delete_strategy_binding(
        tenant_id=scope_tenant,
        user_id=scope_user,
        strategy_id=strategy_id,
    )
    return {"deleted": bool(deleted), "strategy_id": strategy_id}


@router.post("/run-training", summary="启动云端模型训练任务")
async def run_training(
    payload: dict[str, Any],
    background_tasks: BackgroundTasks,
    current_user: dict[str, Any] = Depends(require_admin),
):
    return await submit_training_job(payload, background_tasks, current_user)


@router.get("/training-nodes", summary="列出可用的训练节点及就绪状态")
async def list_training_nodes(
    include_status: bool = True,
    current_user: dict[str, Any] = Depends(require_admin),
):
    """列出训练节点（本地 Docker + 配置的多台 AutoDL 远程节点）。

    默认 include_status=True，并发探测各节点的实时硬件与就绪状态。
    """
    from backend.services.engine.training.node_manager import NodeStatus, load_training_nodes

    if include_status:
        statuses = await NodeStatus.collect_all()
        status_map = {s["id"]: s for s in statuses}
    else:
        status_map = {}

    local_status = status_map.get("local") or {}
    local_online = local_status.get("online", True)
    local_readiness = local_status.get("readiness", "ready" if local_online else "offline")

    nodes = [{
        "id": "local",
        "type": "local",
        # 名称随执行环境变化：Docker daemon 可达 → 本地 Docker；
        # 便携包等免 Docker 部署 → 本地直跑（由 collect_local 探测填充）
        "name": local_status.get("node_name") or "本地 Docker",
        "description": local_status.get("node_description") or "本机 GPU / CPU 容器训练",
        "available": local_online,
        "online": local_online,
        "readiness": local_readiness,
        "readiness_label": local_status.get("readiness_label", "本地就绪"),
        "gpu_summary": local_status.get("gpu_summary", "本地设备"),
        "status_desc": local_status.get("status_desc", "本机执行环境"),
        "error": local_status.get("error") or local_status.get("docker_error"),
        "status": local_status,
    }]
    for n in load_training_nodes():
        node_id = n["id"]
        n_status = status_map.get(node_id) or {}
        n_online = n_status.get("online", False) if include_status else False
        n_readiness = n_status.get("readiness", "ready" if n_online else "offline")
        nodes.append({
            "id": node_id,
            "type": "remote",
            # 配置名称可能包含临时显卡型号或测试备注；训练界面统一以
            # AutoDL 呈现，节点 ID 仍用于实际调度与配置定位。
            "name": "AutoDL",
            "host": n.get("host"),
            "port": n.get("port", 22),
            "description": f"AutoDL 远程 GPU 训练节点（{n.get('host')}）",
            "available": n_online,
            "online": n_online,
            "readiness": n_readiness,
            "readiness_label": n_status.get("readiness_label", "已就绪" if n_online else "未连接"),
            "gpu_summary": n_status.get("gpu_summary", n.get("gpus") or "远程 GPU"),
            "status_desc": n_status.get("status_desc", f"主机: {n.get('host')}:{n.get('port', 22)}"),
            "error": n_status.get("error"),
            "status": n_status,
        })
    return {"nodes": nodes}


@router.post("/training-nodes/test", summary="测试训练节点连接")
async def test_training_node(
    body: dict[str, Any],
    current_user: dict[str, Any] = Depends(require_admin),
):
    """测试 AutoDL 远程节点 SSH 连接 + docker 可用性。

    body: {"node_id": "autodl-1"}，连接配置从节点配置表读取。
    """
    node_id = str((body or {}).get("node_id") or "autodl-1")
    if node_id == "local":
        from backend.services.engine.training.node_manager import NodeStatus
        status = await NodeStatus.collect_local()
        return {
            "success": status.get("online", True),
            "node": node_id,
            "docker": status.get("docker_available", True),
            "status": status,
        }
    try:
        from backend.services.engine.training.orchestrator_base import get_orchestrator

        orch = get_orchestrator(node_id)
        return {"success": True, "node": node_id, **await orch.test_connection()}
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "node": node_id, "error": str(exc)}


@router.get("/training-nodes/{node_id}/status", summary="获取训练节点实时状态")
async def get_training_node_status(
    node_id: str,
    current_user: dict[str, Any] = Depends(require_admin),
):
    """采集单台节点（本地或 AutoDL 远程）的实时状态（CPU/GPU/内存/训练容器）。

    供后台状态面板及训练配置页展示。
    """
    from backend.services.engine.training.node_manager import NodeStatus, get_node_config

    if node_id == "local":
        return await NodeStatus.collect_local()

    node = get_node_config(node_id)
    if not node:
        return {"id": node_id, "name": node_id, "online": False, "readiness": "offline", "error": "节点未配置"}
    return await NodeStatus.collect(node)


@router.post("/training-nodes/config", summary="新增或更新 AutoDL 节点配置")
async def save_training_node_config(
    body: dict[str, Any],
    current_user: dict[str, Any] = Depends(require_admin),
):
    """新增或更新节点配置，写回 config/training_nodes.yaml。

    body 字段：id/name/host/port/user/ssh_password/ssh_key/work_dir/docker_image/gpus。
    密码/密钥留空表示保持不变（编辑场景）；新增节点必须提供其一。
    """
    from backend.services.engine.training.node_manager import save_training_node

    try:
        node = save_training_node(body or {})
        return {"success": True, "node": node}
    except ValueError as exc:
        return {"success": False, "error": str(exc)}


@router.delete("/training-nodes/{node_id}", summary="删除 AutoDL 节点配置")
async def delete_training_node_config(
    node_id: str,
    current_user: dict[str, Any] = Depends(require_admin),
):
    """从 training_nodes.yaml 删除节点配置。"""
    from backend.services.engine.training.node_manager import delete_training_node

    deleted = delete_training_node(node_id)
    return {"success": deleted, "node": node_id}


@router.get("/training-nodes/{node_id}/detail", summary="获取 AutoDL 节点配置详情")
async def get_training_node_detail(
    node_id: str,
    current_user: dict[str, Any] = Depends(require_admin),
):
    """获取单个节点配置详情（剔除明文密码，仅返回 has_password/has_key 标记）。"""
    from backend.services.engine.training.node_manager import get_training_node_detail

    node = get_training_node_detail(node_id)
    if node is None:
        return {"success": False, "error": "节点未配置"}
    return {"success": True, "node": node}


@router.get("/training-runs/{run_id}", summary="获取训练任务状态")
async def get_training_run(
    run_id: str,
    current_user: dict[str, Any] = Depends(require_admin),
):
    return await get_training_run_for_owner(run_id, current_user)


@router.post(
    "/training-runs/{run_id}/complete",
    summary="训练完成回调（内部接口）",
    status_code=401,
    responses={401: {"description": "Invalid or missing X-Internal-Call-Secret"}},
)
async def training_complete_callback(
    run_id: str,
    result: dict[str, Any],
    x_internal_call_secret: str = Header(default="", alias="X-Internal-Call-Secret"),
):
    return await complete_training_run(run_id, result, x_internal_call_secret)


@router.get("/training-jobs", summary="管理员查看训练任务列表")
async def list_training_jobs(
    status: str | None = None,
    tenant_id: str | None = None,
    user_id: str | None = None,
    page: int = 1,
    page_size: int = 20,
    current_user: dict[str, Any] = Depends(require_admin),
) -> dict[str, Any]:
    page = max(1, page)
    page_size = min(max(1, page_size), 100)
    offset = (page - 1) * page_size

    filters: list[str] = []
    params: dict[str, Any] = {}

    if status:
        filters.append("status = :status")
        params["status"] = status
    if tenant_id:
        filters.append("tenant_id = :tenant_id")
        params["tenant_id"] = tenant_id
    if user_id:
        filters.append("user_id = :user_id")
        params["user_id"] = user_id

    where_clause = ("WHERE " + " AND ".join(filters)) if filters else ""

    async with get_session(read_only=True) as session:
        total_row = (
            await session.execute(
                text(f"SELECT COUNT(*) FROM admin_training_jobs {where_clause}"),
                params,
            )
        ).scalar_one()

        rows = (
            await session.execute(
                text(
                    f"""
                    SELECT id, tenant_id, user_id, status, progress, instance_id,
                           logs, result, request_payload, created_at, updated_at
                    FROM admin_training_jobs
                    {where_clause}
                    ORDER BY created_at DESC
                    LIMIT :limit OFFSET :offset
                    """
                ),
                {**params, "limit": page_size, "offset": offset},
            )
        ).mappings().all()

    items = []
    for row in rows:
        result_json = row["result"] if isinstance(row["result"], dict) else {}
        req_payload = row["request_payload"] if isinstance(row["request_payload"], dict) else {}
        model_reg = result_json.get("model_registration") or {}
        items.append(
            {
                "run_id": row["id"],
                "tenant_id": row["tenant_id"],
                "user_id": row["user_id"],
                "status": row["status"],
                "progress": int(row["progress"] or 0),
                "instance_id": row["instance_id"],
                "model_type": req_payload.get("model_type", ""),
                "job_name": req_payload.get("job_name", ""),
                "features_count": len(req_payload.get("features") or []),
                "train_start": req_payload.get("train_start", ""),
                "train_end": req_payload.get("train_end", ""),
                "registered_model_id": model_reg.get("model_id") or "",
                "has_logs": bool(str(row["logs"] or "").strip()),
                "created_at": str(row["created_at"] or ""),
                "updated_at": str(row["updated_at"] or ""),
            }
        )

    return {
        "total": int(total_row or 0),
        "page": page,
        "page_size": page_size,
        "items": items,
    }
