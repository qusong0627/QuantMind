import logging
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from backend.shared.strategy_storage import get_strategy_storage_service
from backend.shared.utils import normalize_user_id

logger = logging.getLogger(__name__)
router = APIRouter()

class CreateItemRequest(BaseModel):
    name: str
    dir: str | None = None
    parameters: dict[str, Any] | None = None  # 市场等元数据（parameters.market 供策略库隔离）

class SaveRequest(BaseModel):
    content: str
    parameters: dict[str, Any] | None = None

class SetRootRequest(BaseModel):
    path: str

def _get_user_id(request: Request) -> str:
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    raw = user.get("user_id") or user.get("sub")
    if raw is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    return str(raw)

@router.post("/set-root")
async def set_root(request: Request, body: SetRootRequest):
    """Cloud IDE workspace root is virtual — accept and acknowledge."""
    return {"status": "success", "current_root": body.path}


@router.get("/list")
async def list_files(request: Request, path: str = "", market: str | None = None):
    """
    列出策略工作区。在云端模式下，每个策略记录对应一个文件。

    文件夹为虚拟层：按策略 parameters.ide_dir 聚合，不落库。
    - path 为空 → 返回根目录：无 ide_dir 的策略文件 + 一级子文件夹
    - path 非空 → 返回该文件夹内的文件 + 下一级子文件夹，parent 为上级目录
    market 过滤：切到港股时只列港股策略（CN 不传 = 现状全量）。
    """
    try:
        user_id = _get_user_id(request)
        svc = get_strategy_storage_service()

        # 获取用户的所有策略
        items = svc.list(user_id=user_id, market=market)

        # 将策略项映射为 IDE 文件项；过滤存量 [folder] 污染数据
        file_items = []
        dirs: set[str] = set()
        cur = (path or "").strip("/")
        prefix = f"{cur}/" if cur else ""
        for s in items:
            _nm = s.get("name") or ""
            _tags = s.get("tags") or []
            if _nm.startswith("[folder]") or "folder" in [str(t).lower() for t in _tags]:
                continue
            if (s.get("parameters") or {}).get("type") == "folder":
                continue
            ide_dir = str((s.get("parameters") or {}).get("ide_dir") or "").strip("/")
            ide_items_entry = {
                "id": s["id"],
                "name": s["name"] + ".py" if not s["name"].endswith(".py") else s["name"],
                "path": s["id"], # 在云端，路径即 ID
                "type": "file",
                "size": 0, # TODO: 优化获取大小
                "last_modified": s.get("updated_at"),
            }
            if ide_dir:
                if cur and ide_dir != cur and not ide_dir.startswith(prefix):
                    continue  # 属于其它文件夹,当前目录不显示
                if ide_dir != cur:
                    rel = ide_dir[len(prefix):]
                    top = rel.split("/")[0]
                    dirs.add(f"{prefix}{top}")
                    continue  # 当前目录只显示文件,子目录单独聚合
                file_items.append(ide_items_entry)
            else:
                if cur:
                    continue  # 无 ide_dir 的策略只在根目录显示
                file_items.append(ide_items_entry)

        folder_items = [
            {
                "id": f"virtual-folder:{d}",
                "name": d.split("/")[-1],
                "path": d,
                "type": "dir",
                "is_dir": True,
                "size": 0,
                "last_modified": None,
            }
            for d in sorted(dirs)
        ]

        parent = "/".join(cur.split("/")[:-1]) if cur else None
        return {
            "items": folder_items + file_items,
            "base": "cloud_workspace",
            "parent": parent,
            "current": cur,
        }
    except Exception as e:
        logger.error(f"Failed to list cloud files: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/create/file")
async def create_file(request: Request, item: CreateItemRequest):
    try:
        user_id = _get_user_id(request)
        svc = get_strategy_storage_service()

        # 去掉 .py 后缀作为策略名
        name = item.name
        if name.endswith(".py"):
            name = name[:-3]

        res = await svc.save(
            user_id=user_id,
            name=name,
            code="# New Strategy\n",
            metadata={
                "status": "DRAFT",
                "description": "Created via Cloud IDE",
                "dir": item.dir or "",
                "parameters": {
                    **(item.parameters or {}),
                    # 记录文件在 IDE 虚拟文件夹中的归属(list 按 ide_dir 聚合)
                    **({"ide_dir": item.dir.strip("/")} if (item.dir or "").strip("/") else {}),
                },
            }
        )
        return {"status": "success", "id": res["id"]}
    except Exception as e:
        logger.error(f"Failed to create cloud file: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/create/folder")
async def create_folder(request: Request, item: CreateItemRequest):
    """创建文件夹 — 统一管理：文件夹为前端虚拟层，不再写入 strategies 表污染策略列表"""
    name = item.name.strip("/")
    if not name:
        raise HTTPException(status_code=400, detail="文件夹名称不能为空")
    # 虚拟文件夹：不落库，由前端基于策略的 dir 字段聚合展示；此处仅 ack
    # 存量 [folder] 污染数据由 list_files 过滤，不再新增
    return {"status": "success", "id": f"virtual-folder:{name}", "name": name, "virtual": True}

@router.get("/{file_id:path}")
async def get_content(request: Request, file_id: str):
    try:
        user_id = _get_user_id(request)
        svc = get_strategy_storage_service()

        # 兼容带 .py 的请求
        sid = file_id
        if sid.endswith(".py") and "-" in sid: # UUID-like
             sid = sid[:-3]

        strategy = await svc.get(sid, user_id=user_id)
        if not strategy:
            raise HTTPException(status_code=404, detail="Strategy not found")

        return {"content": strategy.get("code", "")}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get strategy content: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/{file_id:path}")
async def save_content(request: Request, file_id: str, item: SaveRequest):
    try:
        user_id = _get_user_id(request)
        svc = get_strategy_storage_service()

        sid = file_id
        if sid.endswith(".py"):
            sid = sid[:-3]

        # 针对 422 调试：记录请求详情
        if not item.content:
             logger.warning(f"Empty content received for sid={sid}")

        # 先获取元数据以保留
        try:
            existing = await svc.get(sid, user_id=user_id)
        except Exception as e:
            logger.error(f"Failed to fetch strategy {sid} before save: {e}")
            raise HTTPException(status_code=404, detail="Strategy not found")

        if not existing:
             raise HTTPException(status_code=404, detail="Strategy not found")

        merged_parameters = {
            **(existing.get("parameters") or {}),
            **(item.parameters or {}),
        }
        await svc.save(
            user_id=user_id,
            strategy_id=sid,
            name=existing["name"],
            code=item.content,
            metadata={
                "description": existing.get("description"),
                "tags": existing.get("tags"),
                "parameters": merged_parameters,
                "is_verified": existing.get("is_verified", False)
            }
        )
        return {"status": "success"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to save strategy content: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.delete("/{file_id:path}")
async def delete_item(request: Request, file_id: str):
    # 虚拟文件夹删除直接成功
    if file_id.startswith("virtual-folder:"):
        return {"status": "success", "virtual": True}
    try:
        user_id = _get_user_id(request)
        svc = get_strategy_storage_service()

        sid = file_id
        if sid.endswith(".py"):
            sid = sid[:-3]

        success = await svc.delete(sid, user_id=user_id)
        if not success:
            raise HTTPException(status_code=404, detail="Strategy not found")
        return {"status": "success"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to delete strategy: {e}")
        raise HTTPException(status_code=500, detail=str(e))
