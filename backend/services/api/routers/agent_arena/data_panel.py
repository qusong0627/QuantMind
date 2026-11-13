"""arena 数据面 · 族 9 路由层：/data-platform/*（6 个端点）。

上游 `api_server.py` L1944–2024（`/api/data-platform/markets|catalog|preview|root|scan`）。
错误语义照抄上游：未知市场 400、preview 的 LookupError 404、其余预览失败
500 + `{success:false,error:"预览失败: …"}`；POST root 空值 400 / 目录不存在 404。
成功面一律 `{success:true,data}` 信封（前端 `unwrap` = `(await p).data.data`）。

业务在服务层 `backend/services/agent_arena/data_panel.py`（gitignored 本地
文件），本层只做参数校验 + 信封 + 错误映射。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from backend.services.api.user_app.middleware.auth import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter()

#: 白名单：与上游 `_DP_MARKETS` 相同（quantbc 刻意不在列——上游也没有）。
_DP_MARKETS = {"quantdb", "quantus", "quanthk", "quantfutures"}


def _dp():
    from backend.services.agent_arena import data_panel as dp

    return dp


def _ensure_market(market: str) -> None:
    if market not in _DP_MARKETS:
        raise HTTPException(status_code=400, detail=f"未知市场: {market}")


@router.get("/data-platform/markets")
def dp_markets(_current_user: dict = Depends(get_current_user)):
    return {"success": True, "data": _dp().MARKETS}


@router.get("/data-platform/{market}/catalog")
def dp_catalog(market: str, _current_user: dict = Depends(get_current_user)):
    _ensure_market(market)
    return {"success": True, "data": _dp().build_catalog(market)}


@router.get("/data-platform/{market}/preview")
def dp_preview(
    market: str,
    dataset: str,
    symbol: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    _current_user: dict = Depends(get_current_user),
):
    _ensure_market(market)
    try:
        return {
            "success": True,
            "data": _dp().preview_dataset(market, dataset, symbol, limit),
        }
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 上游同款：预览失败不 5xx 裸崩
        logger.error(
            "arena data-platform preview failed (%s/%s): %s",
            market,
            dataset,
            exc,
            exc_info=True,
        )
        return JSONResponse(
            status_code=500, content={"success": False, "error": f"预览失败: {exc}"}
        )


@router.get("/data-platform/{market}/root")
def dp_root(market: str, _current_user: dict = Depends(get_current_user)):
    _ensure_market(market)
    return {
        "success": True,
        "data": {"market": market, "root": str(_dp().data_root(market))},
    }


@router.post("/data-platform/{market}/root")
async def dp_set_root(
    market: str, request: Request, _current_user: dict = Depends(get_current_user)
):
    """设置数据根目录（只影响展示/预览读取位置，不复制不移动数据）。"""
    _ensure_market(market)
    dp = _dp()
    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="请求体必须是 JSON") from exc
    root = str(body.get("root", "")).strip()
    if not root:
        raise HTTPException(status_code=400, detail="root 不能为空")
    if not dp.Path(root).is_dir():
        raise HTTPException(status_code=404, detail=f"目录不存在: {root}")
    return {
        "success": True,
        "data": {"market": market, "root": dp.set_data_root(market, root)},
    }


@router.get("/data-platform/{market}/scan")
def dp_scan(
    market: str,
    root: str | None = None,
    _current_user: dict = Depends(get_current_user),
):
    """文件夹预检：识别 root 下的数据集（文件数/大小），不改变当前根。"""
    _ensure_market(market)
    return {"success": True, "data": _dp().scan_folder(market, root or "")}
