# mypy: disable-error-code=untyped-decorator

"""港股市场分析 API Router — 恒指脉搏 / 南向资金 / CCASS 席位 / AH 比价 / 行业轮动。

端点前缀：/api/v1/market-analysis-hk
参考 A 股 market_analysis/router.py 的结构（REST 同步 + SSE 流式重算），
数据层完全由 quanthk_feed（本地 parquet 实时聚合）驱动。
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from backend.services.api.market_analysis_hk import quanthk_feed as feed
from backend.services.api.user_app.middleware.auth import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/market-analysis-hk", tags=["Market Analysis HK"])


def _fatal(exc: Exception) -> HTTPException:
    logger.exception("[market-analysis-hk] 请求失败")
    return HTTPException(status_code=500, detail=f"港股市场分析失败: {exc}")


# ---- 诊断 ----


@router.get("/status")
async def get_status(
    current_user: dict = Depends(get_current_user),
) -> dict:
    """数据可用性与最新日期（前端诊断 & 快照日期标注）。"""
    _ = current_user
    return feed.feed_status()


# ---- 大盘全景 ----


@router.get("/indices/overview")
async def get_indices_overview(
    current_user: dict = Depends(get_current_user),
) -> list[dict]:
    """恒生四大指数快照（价格/涨跌/成交额/5 日趋势）。"""
    _ = current_user
    try:
        return await asyncio.to_thread(feed.get_indices_overview)
    except Exception as exc:
        raise _fatal(exc) from exc


@router.get("/breadth")
async def get_market_breadth(
    current_user: dict = Depends(get_current_user),
) -> dict:
    """市场温度计：涨跌家数/成交额/上涨占比/±5% 快涨快跌分布。"""
    _ = current_user
    try:
        return await asyncio.to_thread(feed.get_market_breadth)
    except Exception as exc:
        raise _fatal(exc) from exc


@router.get("/heatmap")
async def get_sector_heatmap(
    limit: int = Query(default=40, ge=5, le=80),
    current_user: dict = Depends(get_current_user),
) -> list[dict]:
    """恒生行业热力图（平均涨幅/成交额/领涨龙头）。"""
    _ = current_user
    try:
        return await asyncio.to_thread(feed.get_sector_heatmap, limit)
    except Exception as exc:
        raise _fatal(exc) from exc


# ---- 南向资金 ----


@router.get("/south/overview")
async def get_south_overview(
    current_user: dict = Depends(get_current_user),
) -> dict:
    """南向总览：披露日/覆盖股票/总持仓市值/当日增减持家数。"""
    _ = current_user
    try:
        return await asyncio.to_thread(feed.get_south_overview)
    except Exception as exc:
        raise _fatal(exc) from exc


@router.get("/south/flow")
async def get_south_stock_flow(
    period: int = Query(default=5, ge=1, le=60),
    limit: int = Query(default=20, ge=5, le=50),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """南向个股增减持榜（持股占比 T 日变化，period=5/20 常见）。"""
    _ = current_user
    try:
        return await asyncio.to_thread(feed.get_south_stock_flow, period, limit)
    except Exception as exc:
        raise _fatal(exc) from exc


@router.get("/south/sectors")
async def get_south_sector_flow(
    limit: int = Query(default=20, ge=5, le=50),
    current_user: dict = Depends(get_current_user),
) -> list[dict]:
    """南向板块配置：行业 × 持仓市值/平均占比。"""
    _ = current_user
    try:
        return await asyncio.to_thread(feed.get_south_sector_flow, limit)
    except Exception as exc:
        raise _fatal(exc) from exc


# ---- CCASS 席位穿透 ----


@router.get("/ccass/rankings")
async def get_ccass_rankings(
    limit: int = Query(default=30, ge=5, le=100),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """全市场 CCASS 集中度榜（前十大席位/南向席位/客户占比 + HHI）。"""
    _ = current_user
    try:
        return await asyncio.to_thread(feed.get_ccass_rankings, limit)
    except Exception as exc:
        raise _fatal(exc) from exc


@router.get("/ccass/holding")
async def get_ccass_holding(
    symbol: str = Query(..., description="个股代码，如 0700.HK 或 0700"),
    limit: int = Query(default=30, ge=5, le=100),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """个股 CCASS 席位穿透（托管行/券商持仓明细 Top N）。"""
    _ = current_user
    try:
        return await asyncio.to_thread(feed.get_ccass_holding, symbol, limit)
    except Exception as exc:
        raise _fatal(exc) from exc


@router.get("/ccass/movers")
async def get_ccass_movers(
    limit: int = Query(default=20, ge=5, le=50),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """CCASS 异动：席位新进/退出的股票（机构筹码变动信号）。"""
    _ = current_user
    try:
        return await asyncio.to_thread(feed.get_ccass_movers, limit)
    except Exception as exc:
        raise _fatal(exc) from exc


# ---- AH 比价 ----


@router.get("/ah/pairs")
async def get_ah_pairs(
    limit: int = Query(default=50, ge=5, le=200),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """AH 对应股联动（港股侧涨跌 + 对应 A 股代码）。"""
    _ = current_user
    try:
        return await asyncio.to_thread(feed.get_ah_pairs, limit)
    except Exception as exc:
        raise _fatal(exc) from exc


# ---- 港股特色主题 ----


@router.get("/valuation/rankings")
async def get_valuation_rankings(
    kind: str = Query(default="dividend", pattern="^(dividend|pe|pb|ps|pcf)$"),
    limit: int = Query(default=20, ge=5, le=50),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """估值主题榜：dividend=高股息 / pe=低PE / pb=低PB / ps=低PS / pcf=低PCF。"""
    _ = current_user
    try:
        return await asyncio.to_thread(feed.get_valuation_rankings, kind, limit)
    except Exception as exc:
        raise _fatal(exc) from exc


@router.get("/ah-premium")
async def get_ah_premium_rankings(
    limit: int = Query(default=20, ge=5, le=50),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """AH 溢价榜：H 折价（A 贵 H 便宜）与倒挂（H 贵 A 便宜）两端对照。"""
    _ = current_user
    try:
        return await asyncio.to_thread(feed.get_ah_premium_rankings, limit)
    except Exception as exc:
        raise _fatal(exc) from exc


@router.get("/dividend-calendar")
async def get_dividend_calendar(
    days: int = Query(default=60, ge=7, le=180),
    limit: int = Query(default=40, ge=5, le=100),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """派息日历：未来 days 天内除息（ex_date）的公司列表。"""
    _ = current_user
    try:
        return await asyncio.to_thread(feed.get_dividend_calendar, days, limit)
    except Exception as exc:
        raise _fatal(exc) from exc


@router.get("/sector-rotation")
async def get_sector_rotation(
    limit: int = Query(default=24, ge=5, le=50),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """恒生行业轮动：1/5/20 日涨幅 + 成交额。"""
    _ = current_user
    try:
        return await asyncio.to_thread(feed.get_sector_rotation, limit)
    except Exception as exc:
        raise _fatal(exc) from exc


@router.get("/sector-valuation")
async def get_sector_valuation(
    limit: int = Query(default=24, ge=5, le=50),
    current_user: dict = Depends(get_current_user),
) -> list[dict]:
    """行业估值温度计：恒生行业 × PE 中位数 / 平均股息率（价值洼地识别）。"""
    _ = current_user
    try:
        return await asyncio.to_thread(feed.get_sector_valuation, limit)
    except Exception as exc:
        raise _fatal(exc) from exc


@router.get("/profit-leaders")
async def get_profit_leaders(
    limit: int = Query(default=10, ge=5, le=30),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """个股综合赚钱效应 Top N（涨幅 × 成交活跃度评分）。"""
    _ = current_user
    try:
        return await asyncio.to_thread(feed.get_profit_leaders, limit)
    except Exception as exc:
        raise _fatal(exc) from exc


# ---- 手动刷新 ----


def _sse(event: str, data: Any) -> str:
    return (
        f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"
    )


@router.post("/refresh")
async def trigger_refresh(
    current_user: dict = Depends(get_current_user),
) -> dict:
    """清缓存并重新计算全部港股市场分析数据（同步返回汇总）。"""
    _ = current_user
    try:
        feed.clear_cache_hk()
        breadth = await asyncio.to_thread(feed.get_market_breadth)
        return {
            "status": "success",
            "trade_date": breadth.get("trade_date", ""),
            "total_stocks": breadth.get("total_stocks", 0),
            "message": "港股市场分析缓存已刷新",
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
    except Exception as exc:
        raise _fatal(exc) from exc


@router.post("/refresh/stream")
async def trigger_refresh_stream(
    request: Request,
    current_user: dict = Depends(get_current_user),
) -> StreamingResponse:
    """SSE 流式重算：清缓存后按 steps 顺序推送各板块，客户端边收边渲染。

    steps: start -> indices -> breadth -> heatmap -> south -> ccass -> ah
    -> valuation -> rotation -> done
    """
    _ = current_user

    async def event_stream():
        yield _sse("start", {"message": "开始刷新港股市场分析…"})
        try:
            await asyncio.to_thread(feed.clear_cache_hk)

            steps = [
                ("indices", feed.get_indices_overview),
                ("breadth", feed.get_market_breadth),
                ("heatmap", feed.get_sector_heatmap, 40),
                ("south_overview", feed.get_south_overview),
                ("south_flow", feed.get_south_stock_flow, 5, 20),
                ("south_sectors", feed.get_south_sector_flow, 20),
                ("ccass_rankings", feed.get_ccass_rankings, 30),
                ("ccass_movers", feed.get_ccass_movers, 20),
                ("ah_pairs", feed.get_ah_pairs, 50),
                ("valuation", feed.get_valuation_rankings, "dividend", 20),
                ("ah_premium", feed.get_ah_premium_rankings, 20),
                ("dividend_calendar", feed.get_dividend_calendar, 60, 40),
                ("profit_leaders", feed.get_profit_leaders, 10),
                ("rotation", feed.get_sector_rotation, 24),
            ]
            for name, func, *args in steps:
                result = await asyncio.to_thread(func, *args)
                if await request.is_disconnected():
                    return
                yield _sse(name, result)

            breadth = await asyncio.to_thread(feed.get_market_breadth)
            yield _sse(
                "done",
                {
                    "trade_date": breadth.get("trade_date", ""),
                    "message": "港股市场分析刷新完成",
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                },
            )
        except Exception as exc:  # pragma: no cover - 兜底
            logger.exception("[market-analysis-hk][stream] 刷新失败")
            yield _sse("error", {"message": f"港股市场分析刷新失败: {exc}"})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
