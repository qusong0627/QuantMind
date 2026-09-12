# mypy: disable-error-code=untyped-decorator

"""美股个股终端 API Router — 标的池 / 概要 / 日线 / 详情聚合 / 资讯。

端点前缀：`/api/v1/stock-terminal-us`
结构对标 `market_analysis_us/router.py`：`Depends(get_current_user)` +
`asyncio.to_thread(feed.xxx)` + `except → HTTPException(500)`。
响应信封 `{"success": true, "data": ...}` 与 A 股终端一致（前端共享服务读 `resp.data.data`）。

**不复用 A 股 `routers/stock_terminal.py`**：那个 2174 行路由的 10 个逐股端点被
`^\\d{6}\\.(SH|SZ|BJ)$` 把守，且到处是 A 股语义（涨跌停 / 推理分 / 板块）。美股是
标普500 + 纳指池、未复权原始价、美元成交额，硬塞第三个 market 分支只会让它更难维护。

口径（响应体里以 `adjust` 与 `notes` 随行下发，勿用 A 股习惯误读）：
- 日线是**原始未复权价**，拆股日价格真实跳变（`splits` 返回全部历史拆股事件）
- `amount` 是**美元原始值**，不是 A 股的「股/万元」
- `/list` 默认只列最新交易日有行情的约 484 只（`include_delisted=true` 带出壳标的）
- 美股目前没有推理分数（engine_signal_scores 无 US 行），列表不含 fusion/side 字段

列表类端点上限按语义给：`/news?limit` ≤ 100（小列表），`/list?page_size` ≤ 600
（标的池共约 484 只，允许一次拉全做本地筛选），`/kline?days` ≤ 2000（约 8 年）。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from backend.services.api.stock_terminal_us.feed import (
    detail as detail_feed,
    kline as kline_feed,
    news as news_feed,
    universe as universe_feed,
)
from backend.services.api.stock_terminal_us.feed.base import (
    clear_cache_terminal,
    normalize_symbol,
)
from backend.services.api.user_app.middleware.auth import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/stock-terminal-us", tags=["Stock Terminal US"])


def _ok(data: Any) -> dict:
    """统一响应信封（与 A 股终端一致：前端共享服务读 `resp.data.data`）。"""
    return {"success": True, "data": data}


def _fatal(exc: Exception) -> HTTPException:
    logger.exception("[stock-terminal-us] 请求失败")
    return HTTPException(status_code=500, detail=f"美股个股终端失败: {exc}")


def _sym_or_400(symbol: str) -> str:
    sym = normalize_symbol(symbol)
    if not sym:
        raise HTTPException(status_code=400, detail=f"非法美股代码: {symbol!r}")
    return sym


def _not_found(symbol: str) -> HTTPException:
    return HTTPException(
        status_code=404,
        detail=f"未找到美股标的 {symbol}（不在标的池且最新交易日无成交）",
    )


@router.get("/list")
async def list_symbols(
    q: str | None = Query(None, description="代码 / 中文名 / 英文名模糊检索"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=600),
    include_delisted: bool = Query(
        False, description="是否带出退市/并购残留标的（has_quote=false）"
    ),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """标的池列表 / 搜索（默认只含最新分区有行情的标的；市值降序，分页）。"""
    _ = current_user
    try:
        result = await asyncio.to_thread(
            universe_feed.list_symbols, q, page, page_size, include_delisted
        )
    except Exception as exc:
        raise _fatal(exc) from exc
    return _ok(result)


@router.get("/profile")
async def get_profile(
    symbol: str = Query(..., description="美股代码，如 AAPL"),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """个股头部信息：名称 / 行业 / 最新收盘 / 涨跌幅 / 市值 / 52 周高低。"""
    _ = current_user
    sym = _sym_or_400(symbol)
    try:
        result = await asyncio.to_thread(universe_feed.get_profile, sym)
    except Exception as exc:
        raise _fatal(exc) from exc
    if result is None:
        raise _not_found(sym)
    return _ok(result)


@router.get("/kline")
async def get_kline(
    symbol: str = Query(..., description="美股代码，如 AAPL"),
    days: int = Query(
        500, ge=30, le=2000, description="最近 N 个交易日（无 start 时生效）"
    ),
    start: str | None = Query(None, description="起始日 YYYY-MM-DD（闭区间）"),
    end: str | None = Query(None, description="结束日 YYYY-MM-DD（闭区间）"),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """本地日线（原始未复权价，美元成交额）+ 全部历史拆股事件。"""
    _ = current_user
    sym = _sym_or_400(symbol)
    try:
        result = await asyncio.to_thread(kline_feed.get_kline, sym, days, start, end)
    except Exception as exc:
        raise _fatal(exc) from exc
    if result is None:
        raise _not_found(sym)
    return _ok(result)


@router.get("/detail")
async def get_detail(
    symbol: str = Query(..., description="美股代码，如 AAPL"),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """个股详情聚合：overview / valuation / financials / analysts / earnings /
    insiders / holdings / corporate_actions。

    每个面板独立容错：某段无数据（或无该标的文件）返回空骨架（键齐全、值为
    null/空数组），不整体 500。响应形状与前端 `stock-terminal-us/types.ts` 对齐。
    """
    _ = current_user
    sym = _sym_or_400(symbol)
    try:
        result = await asyncio.to_thread(detail_feed.get_detail, sym)
    except Exception as exc:
        raise _fatal(exc) from exc
    if result is None:
        raise _not_found(sym)
    return _ok(result)


@router.get("/news")
async def get_news(
    symbol: str = Query(..., description="美股代码，如 AAPL"),
    limit: int = Query(20, ge=1, le=100),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """个股中文资讯：news_article_enrichment 主路径（ticker + 标题，带情绪/事件标签），
    Huntly 标题匹配兜底；长度 ≤1 的代码不参与匹配。"""
    _ = current_user
    sym = _sym_or_400(symbol)
    try:
        result = await asyncio.to_thread(news_feed.get_stock_news, sym, limit)
    except Exception as exc:
        raise _fatal(exc) from exc
    if result is None:
        raise _not_found(sym)
    return _ok(result)


@router.post("/refresh")
async def trigger_refresh(current_user: dict = Depends(get_current_user)) -> dict:
    """清空终端 + 美股市场分析缓存（数据同步后调用；下次请求重建）。"""
    _ = current_user
    try:
        await asyncio.to_thread(clear_cache_terminal)
        result = {
            "status": "success",
            "message": "美股个股终端缓存已清空",
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        return _ok(result)
    except Exception as exc:
        raise _fatal(exc) from exc
