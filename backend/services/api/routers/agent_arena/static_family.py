"""arena 数据面 · 族 8：数据根静态文件（`/data/{path}`）。

前端直接 fetch 的基准文件（`data/benchmark_nasdaq100.json`、
`data/A_stock/index_daily_sse_50.json` 等，见 client.ts fetchBenchmark 走
`api.get('/data/...')`）由这里从数据根伺服。

与上游（api_server.py L200-213）的**安全加固偏离**：上游
`candidate = get_data_root()/path; candidate.is_file()` 对 `../` 不做处理——
`/api/data/../../etc/passwd` 一类路径会被解析出根外文件并按 FileResponse 送走。
本实现先 `resolve()` 再做 `is_relative_to(根)` 判据，越界一律 404（与「不存在」
同形，不给目录结构探测面）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse

from backend.services.agent_arena.arena_config import get_data_root, load_arena_config
from backend.services.api.user_app.middleware.auth import get_current_user

router = APIRouter()

_MEDIA_TYPES = {
    ".json": "application/json",
    ".jsonl": "application/json",
    ".csv": "text/csv",
    ".html": "text/html",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".css": "text/css",
    ".js": "application/javascript",
}


def _media_type(path) -> str:
    return _MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream")


@router.get("/data/{path:path}")
def arena_data_file(
    path: str,
    _current_user: dict = Depends(get_current_user),
):
    """返回数据根下的实时文件（越界路径与不存在一律 404）。"""
    root = get_data_root(load_arena_config()).resolve()
    candidate = (root / path).resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        raise HTTPException(status_code=404, detail=f"数据文件不存在: {path}")
    return FileResponse(candidate, media_type=_media_type(candidate))


@router.get("/data/{path:path}/")
async def arena_data_dir(path: str, request: Request):
    """目录形 URL → 去掉尾斜杠（照抄上游的 307 重定向行为）。"""
    return RedirectResponse(url=request.url.path.rstrip("/"))
