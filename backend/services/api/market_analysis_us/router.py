# mypy: disable-error-code=untyped-decorator

"""美股市场分析 API Router — 大盘脉搏 / 市场宽度 / 板块轮动 / 财报季 / 分析师 / 筹码 / 估值。

端点前缀：`/api/v1/market-analysis-us`
结构对标港股 `market_analysis_hk/router.py`（REST 同步 + SSE 流式重算），
数据层由 `feed/` 下按域拆分的模块驱动（每个域独立可测、可单独迭代）。

口径提示（页面必须如实呈现，勿用 A 股/港股习惯误读）：
- 标的池为标普500 + 纳指补充共约 517 只，**不是全市场**
- 日线是**未复权原始价**，`amount` 是**美元原始成交额**
- **无 VIX、无 ETF**；期权数据不可用（只有 call、单一到期日、单快照）

列表类端点的 `limit` 上界统一为 100：这些榜单都是小列表，上限过紧会让前端
调参时撞上 422 而静默空面板（历史事故：`earnings/surprises?limit=60` 撞 `le=50`）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from backend.services.api.market_analysis_us.feed import (
    analysts,
    breadth,
    earnings,
    holdings,
    hotspot,
    indices,
    sectors,
    valuation,
)
from backend.services.api.market_analysis_us.feed.base import (
    clear_cache_us,
    feed_status,
)
from backend.services.api.user_app.middleware.auth import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/market-analysis-us", tags=["Market Analysis US"])


def _fatal(exc: Exception) -> HTTPException:
    logger.exception("[market-analysis-us] 请求失败")
    return HTTPException(status_code=500, detail=f"美股市场分析失败: {exc}")


# ---- 诊断 ----


@router.get("/status")
async def get_status(current_user: dict = Depends(get_current_user)) -> dict:
    """数据可用性、各数据集最新日期与口径提示（前端诊断 & 面板日期标注）。"""
    _ = current_user
    return feed_status()


# ---- Tab 1 大盘脉搏 ----


@router.get("/indices/overview")
async def get_indices_overview(current_user: dict = Depends(get_current_user)) -> list[dict]:
    """5 大指数快照（不含成交额：index_daily 的 amount 恒为 0）。"""
    _ = current_user
    try:
        return await asyncio.to_thread(indices.get_indices_overview)
    except Exception as exc:
        raise _fatal(exc) from exc


@router.get("/indices/spread")
async def get_index_spread(current_user: dict = Depends(get_current_user)) -> dict:
    """指数间相对强弱（成长 vs 价值 / 半导体 vs 大盘）。"""
    _ = current_user
    try:
        return await asyncio.to_thread(indices.get_index_spread)
    except Exception as exc:
        raise _fatal(exc) from exc


@router.get("/breadth")
async def get_market_breadth(current_user: dict = Depends(get_current_user)) -> dict:
    """市场温度计：涨跌家数 / 中位数涨幅 / ±5% 异动 / 成交额。"""
    _ = current_user
    try:
        return await asyncio.to_thread(breadth.get_market_breadth)
    except Exception as exc:
        raise _fatal(exc) from exc


@router.get("/heatmap")
async def get_sector_heatmap(
    limit: int = Query(default=40, ge=5, le=100),
    current_user: dict = Depends(get_current_user),
) -> list[dict]:
    """GICS 板块热力图（中位涨幅 / 成交额 / 领涨龙头）。"""
    _ = current_user
    try:
        return await asyncio.to_thread(sectors.get_sector_heatmap, limit)
    except Exception as exc:
        raise _fatal(exc) from exc


@router.get("/profit-leaders")
async def get_profit_leaders(
    limit: int = Query(default=10, ge=5, le=30),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """赚钱效应榜（涨幅 × 成交活跃度综合评分）。"""
    _ = current_user
    try:
        return await asyncio.to_thread(breadth.get_profit_leaders, limit)
    except Exception as exc:
        raise _fatal(exc) from exc


# ---- Tab 1 大盘脉搏 · 今日热门 ----


@router.get("/hot-stocks")
async def get_hot_stocks(
    kind: str = Query(default="amount", pattern="^(amount|rvol|gainers|losers)$"),
    limit: int = Query(default=20, ge=5, le=100),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """今日热门榜：amount=成交额 / rvol=量比 / gainers=涨幅 / losers=跌幅。"""
    _ = current_user
    try:
        return await asyncio.to_thread(hotspot.get_hot_stocks, kind, limit)
    except Exception as exc:
        raise _fatal(exc) from exc


@router.get("/unusual-volume")
async def get_unusual_volume(
    limit: int = Query(default=20, ge=5, le=100),
    min_rvol: float = Query(default=2.0, ge=1.0, le=20.0),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """放量异动榜：量比 ≥ min_rvol，按量比降序。"""
    _ = current_user
    try:
        return await asyncio.to_thread(hotspot.get_unusual_volume, limit, min_rvol)
    except Exception as exc:
        raise _fatal(exc) from exc


@router.get("/market-distribution")
async def get_market_distribution(current_user: dict = Depends(get_current_user)) -> dict:
    """全市场涨跌幅分布直方图 + 分位数。"""
    _ = current_user
    try:
        return await asyncio.to_thread(hotspot.get_market_distribution)
    except Exception as exc:
        raise _fatal(exc) from exc


@router.get("/market-stats")
async def get_market_stats(current_user: dict = Depends(get_current_user)) -> dict:
    """市场活力快照：成交额 / 量比中位数 / 放量占比 / ±5% 家数。"""
    _ = current_user
    try:
        return await asyncio.to_thread(hotspot.get_market_stats)
    except Exception as exc:
        raise _fatal(exc) from exc


# ---- Tab 2 市场宽度 ----


@router.get("/breadth/history")
async def get_breadth_history(
    days: int = Query(default=60, ge=10, le=250),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """宽度时间序列：A-D 线 / % 站上 MA50、MA200 / 新高新低家数。"""
    _ = current_user
    try:
        return await asyncio.to_thread(breadth.get_breadth_history, days)
    except Exception as exc:
        raise _fatal(exc) from exc


@router.get("/breadth/highlights")
async def get_breadth_highlights(
    limit: int = Query(default=30, ge=5, le=100),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """52 周位置榜：创新高 / 创新低 / 距高点最近 / 距高点最远。"""
    _ = current_user
    try:
        return await asyncio.to_thread(breadth.get_breadth_highlights, limit)
    except Exception as exc:
        raise _fatal(exc) from exc


# ---- Tab 3 板块轮动 ----


@router.get("/sector-rotation")
async def get_sector_rotation(
    limit: int = Query(default=24, ge=5, le=100),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """GICS 板块 1/5/20/60 日轮动 + 相对标普强弱 + 板块内宽度。"""
    _ = current_user
    try:
        return await asyncio.to_thread(sectors.get_sector_rotation, limit)
    except Exception as exc:
        raise _fatal(exc) from exc


@router.get("/sector-fund-flow")
async def get_sector_fund_flow(
    limit: int = Query(default=24, ge=5, le=100),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """板块资金流：成交额占比及其相对 20 日基准的变化（百分点）。"""
    _ = current_user
    try:
        return await asyncio.to_thread(sectors.get_sector_fund_flow, limit)
    except Exception as exc:
        raise _fatal(exc) from exc


@router.get("/sector-valuation")
async def get_sector_valuation(
    limit: int = Query(default=24, ge=5, le=100),
    current_user: dict = Depends(get_current_user),
) -> list[dict]:
    """板块估值温度计：PE/PB/股息率中位数 + 市值合计。"""
    _ = current_user
    try:
        return await asyncio.to_thread(sectors.get_sector_valuation, limit)
    except Exception as exc:
        raise _fatal(exc) from exc


# ---- Tab 4 财报季 ----


@router.get("/earnings/calendar")
async def get_earnings_calendar(
    days: int = Query(default=30, ge=1, le=120),
    limit: int = Query(default=50, ge=5, le=100),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """未来 N 天待披露财报（含 EPS/营收预期区间）。"""
    _ = current_user
    try:
        return await asyncio.to_thread(earnings.get_earnings_calendar, days, limit)
    except Exception as exc:
        raise _fatal(exc) from exc


@router.get("/earnings/surprises")
async def get_earnings_surprises(
    limit: int = Query(default=30, ge=5, le=100),
    lookback_days: int = Query(default=120, ge=7, le=400),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """近期已披露财报的超预期榜。"""
    _ = current_user
    try:
        return await asyncio.to_thread(
            earnings.get_earnings_surprises, limit, lookback_days
        )
    except Exception as exc:
        raise _fatal(exc) from exc


@router.get("/earnings/revisions")
async def get_earnings_revisions(
    limit: int = Query(default=30, ge=5, le=100),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """盈利预期修正榜（当前季度 EPS/营收同比增速）。"""
    _ = current_user
    try:
        return await asyncio.to_thread(earnings.get_earnings_revisions, limit)
    except Exception as exc:
        raise _fatal(exc) from exc


# ---- Tab 5 分析师动向 ----


@router.get("/analysts/upgrades")
async def get_analyst_upgrades(
    days: int = Query(default=30, ge=1, le=180),
    limit: int = Query(default=40, ge=5, le=100),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """近期评级升降级流水（含目标价调整）。"""
    _ = current_user
    try:
        return await asyncio.to_thread(analysts.get_analyst_upgrades, days, limit)
    except Exception as exc:
        raise _fatal(exc) from exc


@router.get("/analysts/targets")
async def get_analyst_targets(
    limit: int = Query(default=30, ge=5, le=100),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """目标价隐含空间榜（剔除退市/并购残留造成的离群值）。"""
    _ = current_user
    try:
        return await asyncio.to_thread(analysts.get_analyst_targets, limit)
    except Exception as exc:
        raise _fatal(exc) from exc


@router.get("/analysts/ratings")
async def get_analyst_ratings(current_user: dict = Depends(get_current_user)) -> dict:
    """全市场评级分布 + 看多比例最高/最低个股。"""
    _ = current_user
    try:
        return await asyncio.to_thread(analysts.get_analyst_ratings)
    except Exception as exc:
        raise _fatal(exc) from exc


# ---- Tab 6 资金与筹码 ----


@router.get("/insiders/movers")
async def get_insider_movers(
    days: int = Query(default=90, ge=7, le=365),
    limit: int = Query(default=20, ge=5, le=100),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """内部人交易榜：净买入 / 净卖出（只统计 Purchase 与 Sale）。"""
    _ = current_user
    try:
        return await asyncio.to_thread(holdings.get_insider_movers, days, limit)
    except Exception as exc:
        raise _fatal(exc) from exc


@router.get("/holdings/institutional")
async def get_institutional_holders(
    limit: int = Query(default=30, ge=5, le=100),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """机构持仓：全市场结构 + 机构增减持榜（13F 口径，含披露日）。"""
    _ = current_user
    try:
        return await asyncio.to_thread(holdings.get_institutional_holders, limit)
    except Exception as exc:
        raise _fatal(exc) from exc


@router.get("/corporate-actions/dividends")
async def get_dividend_calendar(
    days: int = Query(default=60, ge=7, le=180),
    limit: int = Query(default=40, ge=5, le=100),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """未来 N 天除息日历。"""
    _ = current_user
    try:
        return await asyncio.to_thread(holdings.get_dividend_calendar, days, limit)
    except Exception as exc:
        raise _fatal(exc) from exc


@router.get("/corporate-actions/splits")
async def get_recent_splits(
    days: int = Query(default=365, ge=30, le=1825),
    limit: int = Query(default=30, ge=5, le=100),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """近期拆股记录（同时用于排查跨期收益异常）。"""
    _ = current_user
    try:
        return await asyncio.to_thread(holdings.get_recent_splits, days, limit)
    except Exception as exc:
        raise _fatal(exc) from exc


@router.get("/corporate-actions/dividend-history")
async def get_dividend_history(
    limit: int = Query(default=30, ge=5, le=100),
    current_user: dict = Depends(get_current_user),
) -> list[dict]:
    """稳定分红标的（近一年按季派息）。"""
    _ = current_user
    try:
        return await asyncio.to_thread(holdings.get_dividend_history_summary, limit)
    except Exception as exc:
        raise _fatal(exc) from exc


# ---- Tab 7 估值主题 ----


@router.get("/valuation/rankings")
async def get_valuation_rankings(
    kind: str = Query(default="dividend", pattern="^(dividend|pe|pb)$"),
    limit: int = Query(default=20, ge=5, le=100),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """估值主题榜：dividend=高股息 / pe=低PE / pb=低PB。

    已施加健全性门槛（市值/PE/PB/股息率下限），剔除快照陈旧的壳标的。
    """
    _ = current_user
    try:
        return await asyncio.to_thread(valuation.get_valuation_rankings, kind, limit)
    except Exception as exc:
        raise _fatal(exc) from exc


@router.get("/valuation/size-tiers")
async def get_size_tiers(current_user: dict = Depends(get_current_user)) -> dict:
    """市值分层概览（超大盘/大盘/中盘/小盘）。"""
    _ = current_user
    try:
        return await asyncio.to_thread(valuation.get_size_tiers)
    except Exception as exc:
        raise _fatal(exc) from exc


@router.get("/valuation/overview")
async def get_valuation_overview(current_user: dict = Depends(get_current_user)) -> dict:
    """全市场估值概览（PE/PB/股息率中位数与分位）。"""
    _ = current_user
    try:
        return await asyncio.to_thread(valuation.get_valuation_overview)
    except Exception as exc:
        raise _fatal(exc) from exc


# ---- 手动刷新 ----


def _sse(event: str, data: Any) -> str:
    return (
        f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"
    )


@router.post("/refresh")
async def trigger_refresh(current_user: dict = Depends(get_current_user)) -> dict:
    """清缓存并重新计算全部美股市场分析数据（同步返回汇总）。"""
    _ = current_user
    try:
        clear_cache_us()
        b = await asyncio.to_thread(breadth.get_market_breadth)
        return {
            "status": "success",
            "trade_date": b.get("trade_date", ""),
            "total_stocks": b.get("total_stocks", 0),
            "message": "美股市场分析缓存已刷新",
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
    except Exception as exc:
        raise _fatal(exc) from exc


@router.post("/refresh/stream")
async def trigger_refresh_stream(
    request: Request,
    current_user: dict = Depends(get_current_user),
) -> StreamingResponse:
    """SSE 流式重算：清缓存后按步骤推送各板块，客户端边收边渲染。

    steps: start -> indices -> breadth -> heatmap -> breadth_history
    -> sector_rotation -> earnings -> analysts -> holdings -> valuation -> done
    """
    _ = current_user

    async def event_stream():
        yield _sse("start", {"message": "开始刷新美股市场分析…"})
        try:
            await asyncio.to_thread(clear_cache_us)
            steps = [
                ("indices", indices.get_indices_overview),
                ("market_stats", hotspot.get_market_stats),
                ("breadth", breadth.get_market_breadth),
                ("hot_amount", hotspot.get_hot_stocks, "amount", 20),
                ("hot_rvol", hotspot.get_hot_stocks, "rvol", 20),
                ("market_distribution", hotspot.get_market_distribution),
                ("sector_fund_flow", sectors.get_sector_fund_flow, 24),
                ("heatmap", sectors.get_sector_heatmap, 40),
                ("breadth_history", breadth.get_breadth_history, 60),
                ("breadth_highlights", breadth.get_breadth_highlights, 30),
                ("sector_rotation", sectors.get_sector_rotation, 24),
                ("earnings_calendar", earnings.get_earnings_calendar, 30, 50),
                ("earnings_surprises", earnings.get_earnings_surprises, 30, 120),
                ("analyst_upgrades", analysts.get_analyst_upgrades, 30, 40),
                ("analyst_targets", analysts.get_analyst_targets, 30),
                ("insiders", holdings.get_insider_movers, 90, 20),
                ("valuation", valuation.get_valuation_rankings, "dividend", 20),
            ]
            for name, func, *args in steps:
                result = await asyncio.to_thread(func, *args)
                if await request.is_disconnected():
                    return
                yield _sse(name, result)

            b = await asyncio.to_thread(breadth.get_market_breadth)
            yield _sse(
                "done",
                {
                    "trade_date": b.get("trade_date", ""),
                    "message": "美股市场分析刷新完成",
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                },
            )
        except Exception as exc:  # pragma: no cover - 兜底
            logger.exception("[market-analysis-us][stream] 刷新失败")
            yield _sse("error", {"message": f"美股市场分析刷新失败: {exc}"})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
