"""机构择时 v2（影子盘）API：只读快照，不在请求线程里训练模型。"""

from __future__ import annotations

import threading
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status

from backend.services.api.user_app.middleware.auth import get_current_user
from backend.shared.market_timing_v2.auto_inference import refresh_snapshot
from backend.shared.market_timing_v2.signals import load_snapshot

router = APIRouter(prefix="/timing-v2", tags=["Market Analysis"])

_refresh_lock = threading.Lock()
_refresh_state: dict = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "error": None,
}


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _run_refresh(tenant_id: str | None, user_id: str | None) -> None:
    try:
        refresh_snapshot(force=True, tenant_id=tenant_id, user_id=user_id)
        _refresh_state.update({"error": None, "finished_at": _now()})
    except Exception as exc:  # noqa: BLE001
        _refresh_state.update({"error": str(exc)[:300], "finished_at": _now()})
    finally:
        _refresh_state["running"] = False


@router.get("")
async def get_timing_v2(current_user: dict = Depends(get_current_user)) -> dict:
    """返回最近一次盘后快照；优先读**当前用户作用域**的快照，缺失时回落到全局。"""
    tenant_id = str(current_user.get("tenant_id") or "default")
    user_id = str(current_user.get("user_id") or current_user.get("sub") or "")
    snapshot = load_snapshot(tenant_id, user_id or None)
    if snapshot is None or snapshot.get("status") == "missing":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(snapshot or {}).get("message")
            or "机构择时 v2 快照尚未生成，请等待盘后任务运行",
        )
    return snapshot


@router.post("/refresh")
async def refresh_timing_v2(current_user: dict = Depends(get_current_user)) -> dict:
    """异步重算快照（管理员；重建市场面板 + 训练 MLP，约 2-4 分钟）。

    只扫**当前用户**的模型库，快照写 `latest_{tenant}_{user}.json`，不影响其他用户。
    """
    if not current_user.get("is_admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="需要管理员权限"
        )
    tenant_id = str(current_user.get("tenant_id") or "default")
    user_id = str(current_user.get("user_id") or current_user.get("sub") or "") or None
    with _refresh_lock:
        if _refresh_state["running"]:
            return {"status": "running", **_refresh_state}
        _refresh_state.update(
            {"running": True, "started_at": _now(), "finished_at": None, "error": None}
        )
    threading.Thread(
        target=_run_refresh,
        args=(tenant_id, user_id),
        name="market-timing-v2-refresh",
        daemon=True,
    ).start()
    return {"status": "accepted", **_refresh_state}


@router.get("/refresh/status")
async def refresh_status(current_user: dict = Depends(get_current_user)) -> dict:
    _ = current_user
    return dict(_refresh_state)
