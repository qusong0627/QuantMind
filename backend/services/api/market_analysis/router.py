# mypy: disable-error-code=untyped-decorator

"""Market analysis API Router."""

import asyncio
import concurrent.futures
import json
import logging
import os
import re
import time
import datetime as _dt
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse

from backend.services.api.user_app.middleware.auth import get_current_user
from backend.shared.database_manager_v2 import get_session
from backend.shared.factor_board import board_io as _fb_board_io
from backend.shared.factor_board import config as _fb_config

logger = logging.getLogger(__name__)

from . import flow_panorama, flow_predictive, flow_probability, quantdb_feed, quantdb_snapshot as _snap
from . import sentiment as _sentiment
from . import smart_money as _sm
from .domain import SectorConflictError, SectorNotFoundError
from .repository import MarketAnalysisRepository
from .schemas import (
    AnomalyResponse,
    CreateSectorRequest,
    HeatmapItem,
    HeatmapResponse,
    MarketBreadthResponse,
    MoneyFlowPeriodItem,
    MoneyFlowPeriodResponse,
    SectorMetricsResponse,
    SectorResponse,
)
from .service import MarketAnalysisService
from .quantdb_realtime import QuantDBRealtimeUnavailable, get_snapshots

router = APIRouter(prefix="/api/v1/market-analysis", tags=["Market Analysis"])


@router.get("/realtime/snapshots")
async def get_realtime_snapshots(
    symbols: list[str] = Query(..., min_length=1, max_length=100),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Serve QuantDB snapshots without exposing its internal credential to clients."""
    _ = current_user
    try:
        return await get_snapshots(symbols)
    except QuantDBRealtimeUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="QuantDB 实时行情源不可用",
        ) from exc


def _translate_error(exc: Exception) -> HTTPException:
    if isinstance(exc, SectorNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, SectorConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=500, detail=str(exc))


# ---- Sectors ----


@router.post("/sectors", response_model=SectorResponse, status_code=201)
async def create_sector(
    request: CreateSectorRequest,
    current_user: dict = Depends(get_current_user),
) -> SectorResponse:
    try:
        async with get_session(read_only=False) as session:
            service = MarketAnalysisService(MarketAnalysisRepository(session))
            sector = await service.create_sector(request)
            await session.commit()
            return SectorResponse.model_validate(sector)
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.get("/sectors", response_model=list[SectorResponse])
async def list_sectors(
    sector_type: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    current_user: dict = Depends(get_current_user),
) -> list[SectorResponse]:
    async with get_session(read_only=True) as session:
        service = MarketAnalysisService(MarketAnalysisRepository(session))
        items, _ = await service.list_sectors(
            sector_type=sector_type, limit=limit, offset=offset
        )
        return [SectorResponse.model_validate(s) for s in items]


@router.get("/sectors/{sector_id}", response_model=SectorResponse)
async def get_sector(
    sector_id: str,
    current_user: dict = Depends(get_current_user),
) -> SectorResponse:
    try:
        async with get_session(read_only=True) as session:
            service = MarketAnalysisService(MarketAnalysisRepository(session))
            sector = await service.get_sector(sector_id)
            return SectorResponse.model_validate(sector)
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.get("/sectors/{sector_id}/constituents")
async def list_constituents(
    sector_id: str,
    current_user: dict = Depends(get_current_user),
) -> list[dict]:
    try:
        async with get_session(read_only=True) as session:
            service = MarketAnalysisService(MarketAnalysisRepository(session))
            items = await service.list_constituents(sector_id)
            return [
                {
                    "instrument": str(c.instrument),
                    "weight": float(c.weight) if c.weight else None,
                }
                for c in items
            ]
    except Exception as exc:
        raise _translate_error(exc) from exc


# ---- Metrics ----


@router.get("/sectors/{sector_id}/metrics/latest", response_model=SectorMetricsResponse)
async def get_latest_metrics(
    sector_id: str,
    current_user: dict = Depends(get_current_user),
) -> SectorMetricsResponse:
    try:
        async with get_session(read_only=True) as session:
            service = MarketAnalysisService(MarketAnalysisRepository(session))
            m = await service.get_latest_metrics(sector_id)
            return SectorMetricsResponse(
                trade_date=str(m.trade_date),
                sector_id=str(m.sector_id),
                avg_pct_change=m.avg_pct_change,
                median_pct_change=m.median_pct_change,
                total_market_cap=m.total_market_cap,
                avg_turnover_rate=m.avg_turnover_rate,
                advance_count=m.advance_count,
                decline_count=m.decline_count,
                flat_count=m.flat_count,
                net_inflow=m.net_inflow,
                sentiment_score=m.sentiment_score,
                sentiment_label=m.sentiment_label,
                details=m.details if isinstance(m.details, dict) else {},
            )
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.get(
    "/sectors/{sector_id}/metrics/history", response_model=list[SectorMetricsResponse]
)
async def get_metrics_history(
    sector_id: str,
    start_date: date = Query(...),
    end_date: date = Query(...),
    current_user: dict = Depends(get_current_user),
) -> list[SectorMetricsResponse]:
    try:
        async with get_session(read_only=True) as session:
            service = MarketAnalysisService(MarketAnalysisRepository(session))
            items = await service.get_metrics_history(
                sector_id, start_date=start_date, end_date=end_date
            )
            return [
                SectorMetricsResponse(
                    trade_date=str(m.trade_date),
                    sector_id=str(m.sector_id),
                    avg_pct_change=m.avg_pct_change,
                    median_pct_change=m.median_pct_change,
                    total_market_cap=m.total_market_cap,
                    avg_turnover_rate=m.avg_turnover_rate,
                    advance_count=m.advance_count,
                    decline_count=m.decline_count,
                    flat_count=m.flat_count,
                    net_inflow=m.net_inflow,
                    sentiment_score=m.sentiment_score,
                    sentiment_label=m.sentiment_label,
                    details=m.details if isinstance(m.details, dict) else {},
                )
                for m in items
            ]
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.get("/breadth", response_model=MarketBreadthResponse)
async def get_market_breadth(
    date: str | None = Query(default=None, description="快照日期 YYYY-MM-DD，默认最新"),
    realtime: bool = Query(
        default=False, description="true 时跳过快照，强制实时计算最新数据"
    ),
    current_user: dict = Depends(get_current_user),
) -> MarketBreadthResponse:
    """获取大盘情绪温度计与赚钱效应指标（优先快照，缺失回退实时聚合；显式历史日期无快照则空）。"""
    _ = current_user
    if not realtime:
        data = _snap.breadth(date)
        if data:
            return MarketBreadthResponse(**data)
    if not date:
        data = quantdb_feed.get_market_breadth()
    else:
        data = {
            "trade_date": date,
            "advance_count": 0,
            "decline_count": 0,
            "flat_count": 0,
            "limit_up_count": 0,
            "limit_down_count": 0,
            "total_turnover_yi": 0.0,
            "exploded_ratio": 0.0,
            "profit_effect_score": 0.0,
            "profit_effect": 0.0,
            "limit_up_broken_ratio": 0.0,
        }
    return MarketBreadthResponse(**data)


@router.get("/sentiment")
async def get_market_sentiment(
    date: str | None = Query(
        default=None,
        pattern=r"^\d{4}-\d{2}-\d{2}$",
        description="回看日期 YYYY-MM-DD（只用该日及以前的数据重算信号）",
    ),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """大盘情绪：广度压力分 + 各指标分位 + 各指数的历史同状态扇形分布。

    最新一期只读预计算快照（``backend/scripts/market_sentiment_snapshot.py`` 产出）；
    指定 ``date`` 时走历史回看，服务端按最近交易日吸附、进程内缓存特征帧，
    且信号只用该日及以前的数据（``indices[*].actual`` 是之后真实走势，仅供对照）。
    """
    _ = current_user
    return _sentiment.load_sentiment(date)


@router.get("/heatmap")
async def get_heatmap(
    trade_date: date | None = Query(default=None, description="PPT 交易日（回退用）"),
    category: str = Query(default="shenwan", description="分类: shenwan 或 concept"),
    date: str | None = Query(default=None, description="快照日期 YYYY-MM-DD，默认最新"),
    realtime: bool = Query(
        default=False, description="true 时跳过快照，强制实时计算最新数据"
    ),
    current_user: dict = Depends(get_current_user),
):
    """获取申万一级行业或热门概念热力矩形图（优先快照/SQLite）。"""
    _ = current_user
    if not realtime:
        snap = _snap.heatmap(category=category, date=date)
        if snap and snap["items"]:
            return snap
    if date:
        # 显式历史日期无快照 → 返回空，不回落实时/今日，避免日期错配
        return {"trade_date": date, "category": category, "items": []}

    real_items = quantdb_feed.get_sector_heatmap(category=category)
    if real_items:
        return {
            "trade_date": str(trade_date or _dt.date.today()),
            "category": category,
            "items": real_items,
        }

    if trade_date:
        async with get_session(read_only=True) as session:
            service = MarketAnalysisService(MarketAnalysisRepository(session))
            items = await service.get_heatmap(trade_date)
            return HeatmapResponse(
                trade_date=str(trade_date),
                items=[HeatmapItem(**item) for item in items],
            )
    return {"trade_date": str(_dt.date.today()), "category": category, "items": []}


@router.get("/heatmap/drill")
async def get_heatmap_drill(
    category: Literal["shenwan", "industry1", "concept"] = Query(
        default="shenwan",
        description="板块分类：shenwan=二级行业 / industry1=一级行业 / concept=概念",
    ),
    name: str = Query(
        ...,
        min_length=1,
        max_length=64,
        description="板块名（与 /heatmap items[].name 一致）",
    ),
    date: str | None = Query(default=None, description="交易日 YYYY-MM-DD（默认最新）"),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """板块 → 成分个股热力矩形图下钻（矩形面积=总市值，颜色可按涨跌幅/主力净额）。

    顶层图（/heatmap）任一板块点击后进入本层；板块名或分类不存在返回 404
    （detail 带 category/name 供前端定位），前端据此回退顶层而不是显示空白图。
    """
    _ = current_user
    data = await asyncio.to_thread(
        quantdb_feed.get_sector_drill, category=category, name=name, date=date
    )
    if data is None:
        raise HTTPException(
            status_code=404,
            detail={
                "message": f"板块不存在或暂无数据：{category} / {name}",
                "category": category,
                "name": name,
            },
        )
    return data


# ---- Anomalies ----


@router.get("/anomalies", response_model=list[AnomalyResponse])
async def list_anomalies(
    trade_date: date | None = Query(default=None),
    anomaly_type: str | None = Query(default=None),
    sector_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: dict = Depends(get_current_user),
) -> list[AnomalyResponse]:
    async with get_session(read_only=True) as session:
        service = MarketAnalysisService(MarketAnalysisRepository(session))
        items, _ = await service.list_anomalies(
            trade_date=trade_date,
            anomaly_type=anomaly_type,
            sector_id=sector_id,
            limit=limit,
            offset=offset,
        )
        return [AnomalyResponse.model_validate(a) for a in items]


# ---- Indices & Money Flow Extensions (QuantDB 真实驱动) ----


@router.get("/status")
async def get_snapshot_status(
    date: str | None = Query(default=None, description="快照日期 YYYY-MM-DD，默认最新"),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """快照状态（存在性 / 交易日 / 生成时间 / 各块数量）。"""
    _ = current_user
    st = _snap.status(date)
    if st:
        return st
    return {
        "has_snapshot": False,
        "message": "暂无快照，请先执行 backend/scripts/market_snapshot/compute.py",
    }


@router.get("/snapshot/dates")
async def get_snapshot_dates(
    current_user: dict = Depends(get_current_user),
) -> dict:
    """列出所有可用快照日期（YYYY-MM-DD 降序），供前端日期选择器枚举。"""
    _ = current_user
    return {"dates": _snap.available_dates(), "latest": _snap.trade_date()}


# ---- 因子概率板（市场分析 · 细案 §六，P1 只读面） ----


@router.get("/factor-board")
async def get_factor_board(
    date: str | None = Query(
        default=None,
        pattern=r"^\d{4}-\d{2}-\d{2}$",
        description="决策日 YYYY-MM-DD（产物文件名口径），默认最新",
    ),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """因子概率板产物：读 JSON 原样返回，另附 `available_dates`（供日期切换）。

    语义（细案 §6.3）：无产物或指定决策日无文件 → 404（detail 含
    available_dates）；文件存在但损坏、非对象、或内容 meta.decision_date
    与请求日不符 → 409。目录与产板同源（resolve_board_dir：env >
    容器 /data > 仓库 data，无需另行注入）。
    """
    _ = current_user
    out_dir = _fb_config.resolve_board_dir()
    dates = _fb_board_io.available_dates(out_dir)
    board = _fb_board_io.load_board(out_dir, date)
    if board is None:
        target = out_dir / f"{date}.json" if date else out_dir / "latest.json"
        if target.exists():
            raise HTTPException(
                status_code=409,
                detail=f"因子概率板产物损坏（{target.name}），请检查生成任务",
            )
        raise HTTPException(
            status_code=404,
            detail={
                "message": "因子概率板尚未生成"
                if not date
                else f"决策日 {date} 无产物",
                "available_dates": dates,
            },
        )
    if not isinstance(board, dict):
        raise HTTPException(
            status_code=409, detail="因子概率板产物格式异常（顶层非对象）"
        )
    meta = board.get("meta") if isinstance(board.get("meta"), dict) else {}
    decision = str((meta or {}).get("decision_date") or "")
    if date and decision != date:
        raise HTTPException(
            status_code=409,
            detail=f"产物内容 decision_date={decision or '缺失'} 与请求 {date} 不符",
        )
    return {**board, "available_dates": dates}


@router.get("/factor-board/members")
async def get_factor_board_members(
    type: Literal["industry_l1", "industry_l2", "concept"] = Query(
        ..., description="板块分类键（与 sectors 三个键一致）"
    ),
    name: str = Query(
        ...,
        min_length=1,
        max_length=64,
        description="板块名（sectors.items[].name 原样），如「航空机场」",
    ),
    date: str | None = Query(
        default=None,
        pattern=r"^\d{4}-\d{2}-\d{2}$",
        description="决策日 YYYY-MM-DD（产物文件名口径），默认最新",
    ),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """板块成分钻取：读伴生明细 ``{out}/members/{decision_date}.json``。

    口径：明细由产板 CLI 随板落盘（含 ST 与无模型覆盖标的，板块内按合成分降序；
    板 JSON 本身不含成员，概念展开量太大）。404：该决策日无明细（旧版产物未重跑
    产板）或板块名不存在（detail 带 available_dates 供纠正）。
    """
    _ = current_user
    out_dir = _fb_config.resolve_board_dir()
    dates = _fb_board_io.available_dates(out_dir)
    decision = date or (dates[0] if dates else None)
    index = _fb_board_io.load_members_index(out_dir, decision)
    if index is None:
        raise HTTPException(
            status_code=404,
            detail={
                "message": (
                    f"决策日 {decision} 无板块成分明细（旧版产物或产板未重跑——"
                    "重跑 build_factor_board.py 即生成）"
                    if decision
                    else "板块成分明细尚未生成（需先产板）"
                ),
                "available_dates": dates,
            },
        )
    sectors = index.get("sectors")
    group = sectors.get(type) if isinstance(sectors, dict) else None
    rows = group.get(name) if isinstance(group, dict) else None
    if rows is None:
        raise HTTPException(
            status_code=404,
            detail={
                "message": f"板块成分不存在：{type} / {name}（板块名或已变更，以当日产物为准）",
                "available_dates": dates,
            },
        )
    return {
        "schema_version": index.get("schema_version"),
        "decision_date": index.get("decision_date"),
        "type": type,
        "name": name,
        "n": len(rows),
        "items": rows,
    }


@router.get("/indices/overview")
async def get_indices_overview(
    date: str | None = Query(default=None, description="快照日期 YYYY-MM-DD，默认最新"),
    realtime: bool = Query(
        default=False, description="true 时跳过快照，强制实时计算最新数据"
    ),
    current_user: dict = Depends(get_current_user),
) -> list[dict]:
    """获取大盘核心指数快照（优先读快照，缺失回退实时聚合；显式历史日期无快照则空）。"""
    _ = current_user
    if not realtime:
        data = _snap.indices(date)
        if data:
            return data
    if date:
        return []
    data = quantdb_feed.get_indices_overview()
    if data:
        return data
    return []


@router.get("/money-flow/stocks")
async def get_stock_money_flow(
    limit: int = Query(default=20, ge=1, le=100),
    date: str | None = Query(default=None, description="快照日期 YYYY-MM-DD，默认最新"),
    realtime: bool = Query(
        default=False, description="true 时跳过快照，强制实时计算最新数据"
    ),
    current_user: dict = Depends(get_current_user),
) -> list[dict]:
    """个股资金流向排行榜（优先快照，缺失回退实时 L2 资金流）"""
    _ = current_user
    if not realtime:
        data = _snap.stock_flow(limit=limit, date=date)
        if data:
            return data
    if date:
        return []
    data = quantdb_feed.get_stock_money_flow(limit=limit)
    if data:
        return data
    return []


@router.get("/money-flow/stocks/full")
async def get_stock_money_flow_full(
    date: str | None = Query(default=None, description="快照日期 YYYY-MM-DD，默认最新"),
    realtime: bool = Query(
        default=False, description="true 时跳过快照，强制实时计算最新数据"
    ),
    current_user: dict = Depends(get_current_user),
) -> list[dict]:
    """全市场个股资金流（供前端本地搜索）；优先快照，无快照时回退实时单日全市场。"""
    _ = current_user
    if not realtime:
        data = _snap.stock_flow_full(date)
        if data:
            return data
    if date:
        # 显式历史日期无快照 → 返回空，不回落实时，避免日期错配
        return []
    return await asyncio.to_thread(quantdb_feed.get_stock_money_flow_full)


@router.get("/money-flow/predictive")
async def get_flow_predictive(
    date: str | None = Query(default=None, description="快照日期 YYYY-MM-DD，默认最新"),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """资金流预测力：离线研究产物 + 当日质量分/形态匹配（口径见 flow_quality.py）。"""
    _ = current_user
    return await asyncio.to_thread(flow_predictive.get_predictive, date)


@router.get("/money-flow/probability")
async def get_flow_probability(
    date: str | None = Query(default=None, description="快照日期 YYYY-MM-DD，默认最新"),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """资金流概率模型产物：未来 h 日跑赢排序分 + 当日强弱/逆流候选（离线周更）。"""
    _ = current_user
    return await asyncio.to_thread(flow_probability.get_probability, date)


@router.get("/money-flow/panorama")
async def get_money_flow_panorama(
    period: str = Query("1d", description="周期: 1d, 3d, 5d, 10d, 20d"),
    sectors: str = Query(
        "industry2", description="板块体系: industry1 / industry2 / concept"
    ),
    top_sectors: int = Query(
        10, ge=2, le=30, description="桑基展示板块数（all 时按 6:4 拆流入/流出）"
    ),
    stocks_per_sector: int = Query(
        5,
        ge=1,
        le=20,
        description="每板块展示的个股数（all=流入前 6/流出最深 4 双侧；inflow/outflow=单向 TopK）",
    ),
    direction: str = Query("all", description="方向: all / inflow / outflow"),
    date: str | None = Query(default=None, description="交易日 YYYY-MM-DD（默认最新）"),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """资金流全景：桑基 v2（4 层、符号双向、板块延续徽章）+ 板块 master 榜。"""
    _ = current_user
    if period.lower() not in quantdb_feed.PERIOD_DAYS:
        raise HTTPException(status_code=400, detail="period_invalid")
    if sectors not in flow_panorama.SECTOR_SET_TYPE:
        raise HTTPException(status_code=400, detail="sector_set_invalid")
    if direction not in flow_panorama.DIRECTIONS:
        raise HTTPException(status_code=400, detail="direction_invalid")
    return await asyncio.to_thread(
        flow_panorama.get_panorama,
        period=period,
        sector_set=sectors,
        top_sectors=top_sectors,
        stocks_per_sector=stocks_per_sector,
        direction=direction,
        date=date,
    )


@router.get("/money-flow/sector")
async def get_money_flow_sector(
    name: str = Query(..., min_length=1, description="板块名称（精确）"),
    sectors: str = Query(
        "industry2", description="板块体系: industry1 / industry2 / concept"
    ),
    period: str = Query("1d", description="周期: 1d, 3d, 5d, 10d, 20d"),
    top: int = Query(50, ge=1, le=200, description="成分行数上限"),
    date: str | None = Query(default=None, description="交易日 YYYY-MM-DD（默认最新）"),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """板块明细：窗口汇总 + 20 日趋势 + 成分贡献（master-detail 的 detail 面）。"""
    _ = current_user
    if period.lower() not in quantdb_feed.PERIOD_DAYS:
        raise HTTPException(status_code=400, detail="period_invalid")
    if sectors not in flow_panorama.SECTOR_SET_TYPE:
        raise HTTPException(status_code=400, detail="sector_set_invalid")
    data = await asyncio.to_thread(
        flow_panorama.get_sector_detail,
        name=name,
        sector_set=sectors,
        period=period,
        top=top,
        date=date,
    )
    if data is None:
        raise HTTPException(status_code=404, detail="sector_not_found")
    return data


@router.get("/money-flow/stock")
async def get_money_flow_stock(
    symbol: str = Query(
        ..., min_length=1, description="股票代码（SH600036 / 600036.SH / 600036）"
    ),
    days: int = Query(20, ge=1, le=60, description="序列长度（交易日）"),
    date: str | None = Query(default=None, description="交易日 YYYY-MM-DD（默认最新）"),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """个股明细：20 日五档序列 + 板块归因（资金流「返回板块」的双向面）。"""
    _ = current_user
    data = await asyncio.to_thread(
        flow_panorama.get_stock_detail,
        symbol=symbol,
        days=days,
        date=date,
    )
    if data is None:
        raise HTTPException(status_code=404, detail="stock_not_found")
    return data


@router.get("/money-flow/evidence")
async def get_money_flow_evidence(
    date: str | None = Query(
        default=None,
        description="交易日 YYYY-MM-DD（默认最新；快照日命中榜走日面板回溯）",
    ),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """证据台：命中榜（延续状态 × 历史条件超额）+ 信号档案（过闸/否决两节）。"""
    _ = current_user
    return await asyncio.to_thread(flow_panorama.get_evidence, date=date)


# ---- 聪明钱（市场分析 · 细案 §5.2；只读产物，无产物 404 smart_money_not_built） ----


async def _sm_read(fn, *args, **kwargs):
    """产物读侧统一调度：线程池执行 + SmartMoneyNotBuilt → 404 收敛。"""
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except _sm.SmartMoneyNotBuilt:
        raise HTTPException(status_code=404, detail="smart_money_not_built") from None


@router.get("/smart-money/overview")
async def get_smart_money_overview(
    current_user: dict = Depends(get_current_user),
) -> dict:
    """概览：龙虎榜/机构/两融/大宗/调研/北向统计卡 + 趋势序列 + 新鲜度。"""
    _ = current_user
    return await _sm_read(_sm.get_overview)


@router.get("/smart-money/stocks")
async def get_smart_money_stocks(
    limit: int = Query(
        _sm.STOCKS_LIMIT_DEFAULT, ge=1, le=_sm.STOCKS_LIMIT_MAX, description="每页行数"
    ),
    offset: int = Query(0, ge=0, description="分页偏移"),
    date: str | None = Query(
        None,
        description="历史快照日 YYYY-MM-DD；命中最近 ≤date 的计分日，"
        "响应 as_of 标注真实快照日（研究评分，非交易指令）",
    ),
    event: str | None = Query(
        None, description="最近事件类型：lhb / inst / block_trade / survey"
    ),
    industry: str | None = Query(None, description="行业名精确匹配（rs_hyname）"),
    min_score: float | None = Query(None, ge=0, le=100, description="复合分下限"),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """选股榜：筛选 + 分页（默认当日；date= 走历史快照现算）。"""
    _ = current_user
    if event is not None and event not in _sm.STOCKS_EVENTS:
        raise HTTPException(status_code=400, detail="event_invalid")
    try:
        return await _sm_read(
            _sm.get_stocks,
            limit=limit,
            offset=offset,
            date=date,
            event=event,
            industry=industry,
            min_score=min_score,
        )
    except _sm.SmartMoneySnapshotMissing:
        raise HTTPException(status_code=404, detail="smart_money_no_snapshot") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.get("/smart-money/stock/{symbol}")
async def get_smart_money_stock(
    symbol: str,
    current_user: dict = Depends(get_current_user),
) -> dict:
    """个股档案：榜上条目 + 时间线 + 北向序列 + 席位构成 + 大宗明细
    （未上榜 200, on_board=false）。"""
    _ = current_user
    return await _sm_read(_sm.get_stock_detail, symbol)


@router.get("/smart-money/track-record")
async def get_smart_money_track_record(
    current_user: dict = Depends(get_current_user),
) -> dict:
    """历史战绩：Top-N 组合逐日收益/窗口汇总/分箱/滚动 IC/最近案例。"""
    _ = current_user
    return await _sm_read(_sm.get_track_record)


@router.get("/smart-money/event-study")
async def get_smart_money_event_study(
    current_user: dict = Depends(get_current_user),
) -> dict:
    """事件族 CAR：族内当日池分位 q5/q1 的超额路径（族级统计，非个股预测）。"""
    _ = current_user
    return await _sm_read(_sm.get_event_study)


@router.get("/smart-money/seats")
async def get_smart_money_seats(
    window: str = Query("all", description="胜率窗口：20 / 60 / all"),
    sort: str = Query("win_rate", description="排序：win_rate 或 net（全史净买额）"),
    cls: str | None = Query(
        None, alias="class", description="席位类型：inst / north / branch / other"
    ),
    limit: int = Query(
        _sm.SEATS_LIMIT_DEFAULT, ge=1, le=_sm.SEATS_LIMIT_MAX, description="返回条数"
    ),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """席位胜率榜（情报面：席位历史胜率，非交易指令）。"""
    _ = current_user
    try:
        return await _sm_read(
            _sm.get_seats, window=window, sort=sort, seat_class=cls, limit=limit
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.get("/smart-money/branches")
async def get_smart_money_branches(
    window: int = Query(5, description="净买聚合窗口（交易日）：5 或 20"),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """活跃营业部榜（净买降序，含代表买卖股；原始层现算）。"""
    _ = current_user
    if window not in _sm.BRANCHES_WINDOWS:
        raise HTTPException(status_code=400, detail="window_invalid")
    return await _sm_read(_sm.get_branches, window=window)


@router.get("/smart-money/evidence")
async def get_smart_money_evidence(
    current_user: dict = Depends(get_current_user),
) -> dict:
    """验证证据台：双过闸 IC/分位超额/分年/相关塌缩 + 口径说明。"""
    _ = current_user
    return await _sm_read(_sm.get_evidence)


@router.get("/smart-money/dates")
async def get_smart_money_dates(
    current_user: dict = Depends(get_current_user),
) -> dict:
    """可用评分快照日期。"""
    _ = current_user
    return await _sm_read(_sm.available_dates)


# ---- Tag Dual Lookup Endpoints (标签双向查询) ----


@router.get("/tags/stats")
async def get_tag_stats(
    limit: int = Query(default=30, ge=1, le=100),
    date: str | None = Query(default=None, description="快照日期 YYYY-MM-DD，默认最新"),
    hot_type: str | None = Query(
        default=None,
        description="按板块体系过滤热门（如：行业板块(二级)）；缺省为全局混合 top-N",
    ),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """标签体系统计与热门标签（优先 SQLite 快照，缺失回退 sector_members 聚合）"""
    _ = current_user
    st = _snap.tag_stats(limit=limit, date=date, hot_type=hot_type)
    if st:
        return st
    if date:
        return {
            "trade_date": date,
            "total_sectors": 0,
            "total_stocks": 0,
            "avg_tags_per_stock": 0.0,
            "max_tags_per_stock": 0,
            "total_relations": 0,
            "hot_tags": [],
        }
    return quantdb_feed.get_tag_stats(limit=limit, hot_type=hot_type)


@router.get("/tags/by-tag")
async def get_stocks_by_tag(
    tag: str = Query(..., description="标签或板块名称，如：低空经济 / 华为概念 / 电子"),
    limit: int = Query(default=30, ge=1, le=200),
    date: str | None = Query(default=None, description="快照日期 YYYY-MM-DD，默认最新"),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """根据标签查个股（优先 SQLite 快照，缺失回退实时聚合）。"""
    _ = current_user
    st = _snap.stocks_by_tag(tag=tag, limit=limit, date=date)
    # 快照 tags 表只存 symbol/sector_type，无行情字段；无行情时必须回退
    # 实时聚合，否则前端 v.toFixed 会因 undefined 崩溃。
    if st and st["items"] and "close_price" in st["items"][0]:
        return {"tag": tag, "total": len(st["items"]), "items": st["items"]}
    if date:
        return {"tag": tag, "total": 0, "items": []}
    items = quantdb_feed.get_stocks_by_tag(tag=tag, limit=limit)
    if items is not None:
        return {"tag": tag, "total": len(items), "items": items}
    return {"tag": tag, "total": 0, "items": []}


@router.get("/tags/by-stock")
async def get_tags_by_stock(
    symbol: str = Query(..., description="股票代码或名称，如：SH600036 或 招商银行"),
    date: str | None = Query(default=None, description="快照日期 YYYY-MM-DD，默认最新"),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """根据个股查标签（优先 SQLite 快照，缺失回退实时聚合）。"""
    _ = current_user
    norm = _snap_normalize_symbol(symbol)
    st = _snap.tags_by_stock(symbol=norm, date=date)
    if st and st["tags"]:
        return {"symbol": symbol, "tags": st["tags"]}
    if date:
        return {"symbol": symbol, "tags": {}}
    tags = quantdb_feed.get_tags_by_stock(symbol=symbol)
    if tags:
        return {"symbol": symbol, "tags": tags}
    return {"symbol": symbol, "tags": {}}


@router.get("/tags/matches")
async def match_sectors_by_name(
    q: str = Query(
        ..., min_length=1, max_length=32, description="板块名关键词，如：航空 / 华为"
    ),
    limit: int = Query(default=30, ge=1, le=100),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """板块名候选解析（统一搜索框用）。

    只扫成分表、不算任何行情/资金流——逐击键调用也毫秒级返回。匹配为子串、
    大小写不敏感；排序精确 > 前缀 > 子串、组内按成分数降序。**不筛板块类型**：
    地区/风格等非资金流体系板块也如实返回（消费方按类型决定是否可联动）。
    """
    _ = current_user
    matches = quantdb_feed.get_tag_matches(q=q, limit=limit)
    return {"q": q, "matches": matches}


def _snap_normalize_symbol(symbol: str) -> str:
    """把用户输入归一为快照标签库使用的 Prefix（SH600036）。"""
    s = symbol.strip().upper()
    m = re.fullmatch(r"(\d{6})\.(SH|SZ|BJ)", s)
    if m:
        return f"{m.group(2)}{m.group(1)}"
    if re.fullmatch(r"\d{6}", s):
        return f"SH{s}" if s[0] in "69" else f"SZ{s}" if s[0] in "032" else f"BJ{s}"
    return s


@router.get("/money-flow/period", response_model=MoneyFlowPeriodResponse)
async def get_money_flow_by_period(
    period: str = Query("1d", description="周期: 1d, 3d, 5d, 10d, 20d"),
    dimension: str = Query("sector", description="维度: sector 或 stock"),
    category: str = Query("shenwan", description="分类: shenwan 或 concept"),
    limit: int = Query(31, ge=1, le=100),
    date: str | None = Query(default=None, description="快照日期 YYYY-MM-DD，默认最新"),
    realtime: bool = Query(
        default=False, description="true 时跳过快照，强制实时计算最新数据"
    ),
    current_user: dict = Depends(get_current_user),
) -> MoneyFlowPeriodResponse:
    """获取指定交易日周期 (1D/3D/5D/10D/20D) 的资金净流向排行榜（优先快照）。"""
    _ = current_user
    today_str = datetime.now().strftime("%Y-%m-%d")
    raw_items = None
    if not realtime:
        raw_items = _snap.money_flow_period(
            period=period,
            dimension=dimension,
            category=category,
            limit=limit,
            date=date,
        )
    if raw_items is None:
        if date:
            raw_items = []
        else:
            raw_items = quantdb_feed.get_money_flow_period(
                period=period,
                dimension=dimension,
                category=category,
                limit=limit,
            )
    if raw_items:
        items = [MoneyFlowPeriodItem(**it) for it in raw_items]
        return MoneyFlowPeriodResponse(
            trade_date=today_str,
            period=period,
            dimension=dimension,
            items=items,
        )
    return MoneyFlowPeriodResponse(
        trade_date=today_str,
        period=period,
        dimension=dimension,
        items=[],
    )


@router.post("/analyze")
@router.post("/refresh")
async def trigger_market_analysis(
    current_user: dict = Depends(get_current_user),
) -> dict:
    """手动触发重算/刷新：优先提示快照模式，无快照则实时重算。"""
    _ = current_user
    st = _snap.status()
    if st and st["has_snapshot"]:
        return {
            "status": "snapshot",
            "trade_date": st["trade_date"],
            "generated_at": st["generated_at"],
            "indices_count": st["indices_count"],
            "message": "快照模式：请重新执行 backend/scripts/market_snapshot/compute.py 后覆盖 data/market-analysis/*.json",
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
    quantdb_feed.clear_cache()
    latest_dt = quantdb_feed._latest_trade_date()
    indices = quantdb_feed.get_indices_overview()
    breadth = quantdb_feed.get_market_breadth()

    return {
        "status": "success",
        "trade_date": latest_dt,
        "indices_count": len(indices) if indices else 0,
        "total_turnover_yi": breadth.get("total_turnover_yi", 0) if breadth else 0,
        "message": "已从 QuantDB 读取最新数据并完成市场分析",
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def _sse(event: str, data: Any) -> str:
    """构造一条 SSE 事件（event 名 + JSON data），两步式保持向前兼容。"""
    payload = json.dumps(data, ensure_ascii=False, default=str)
    return f"event: {event}\ndata: {payload}\n\n"


# 手动分析持久化的多周期组合（与离线 compute.py 产出的 money_flow_periods 键一致）
_PERSIST_PERIOD_COMBOS: tuple[tuple[str, str, str], ...] = (
    ("1d", "sector", "shenwan"),
    ("1d", "sector", "concept"),
    ("1d", "stock", "shenwan"),
    ("5d", "sector", "shenwan"),
    ("5d", "sector", "concept"),
    ("5d", "stock", "shenwan"),
    ("20d", "sector", "shenwan"),
    ("20d", "sector", "concept"),
    ("20d", "stock", "shenwan"),
)


def _persist_stream_snapshot(
    *,
    trade_date: str,
    generated_at: str,
    indices: Any,
    breadth: Any,
    heatmap_shenwan: Any,
    heatmap_concept: Any,
    stock_flow: Any,
    stock_flow_full: Any,
    money_flow_periods: dict[str, Any],
) -> str | None:
    """把手动分析结果合并写入快照目录（{date}.json + latest.json）。

    只替换本次算出的块；tag_stats 等离线块保留旧快照内容，等 04:00 beat
    全量重算后自然对齐。永不抛异常，失败仅记日志（不影响本次分析返回）。
    返回写入的 trade_date，失败返回 None。
    """
    try:
        env = os.getenv("QM_MARKET_SNAPSHOT_DIR", "").strip()
        out_dir = (
            Path(env).expanduser()
            if env
            else Path(os.getcwd()) / "data" / "market-analysis"
        )
        out_dir.mkdir(parents=True, exist_ok=True)

        base: dict[str, Any] = {}
        latest_path = out_dir / "latest.json"
        if latest_path.is_file():
            try:
                base = json.loads(latest_path.read_text(encoding="utf-8")) or {}
            except Exception:
                base = {}
        if not isinstance(base, dict):
            base = {}

        old_heatmap = (
            base.get("heatmap") if isinstance(base.get("heatmap"), dict) else {}
        )
        heatmap: dict[str, Any] = dict(old_heatmap)
        if heatmap_shenwan:
            heatmap["shenwan"] = heatmap_shenwan
        if heatmap_concept:
            heatmap["concept"] = heatmap_concept

        doc: dict[str, Any] = dict(base)
        # 旧版快照携带 sankey 块（已退役，消费方为零）：重写时顺带剔除，防止存量字段长留
        doc.pop("sankey", None)
        doc.update(
            {
                "trade_date": trade_date,
                "generated_at": generated_at,
                "indices": indices or [],
                "breadth": breadth or {},
                "heatmap": heatmap,
                "stock_flow": stock_flow or [],
                "stock_flow_full": stock_flow_full or base.get("stock_flow_full") or [],
                "money_flow_periods": money_flow_periods
                or base.get("money_flow_periods")
                or {},
            }
        )

        dated_path = out_dir / f"{trade_date}.json"
        tmp_dated = dated_path.with_suffix(".json.tmp")
        tmp_latest = latest_path.with_suffix(".json.tmp")
        payload = json.dumps(doc, ensure_ascii=False)
        tmp_dated.write_text(payload, encoding="utf-8")
        tmp_latest.write_text(payload, encoding="utf-8")
        tmp_dated.replace(dated_path)
        tmp_latest.replace(latest_path)
        _refresh_latest_tags_db(out_dir, trade_date, heatmap_shenwan, heatmap_concept)
        logger.info(
            "[market-analysis][stream] 快照已持久化 trade_date=%s dir=%s",
            trade_date,
            out_dir,
        )
        return trade_date
    except Exception as exc:  # noqa: BLE001
        logger.warning("[market-analysis][stream] 快照持久化失败: %s", exc)
        return None


def _refresh_latest_tags_db(
    out_dir: Path,
    trade_date: str,
    heatmap_shenwan: Any,
    heatmap_concept: Any,
) -> None:
    """同步刷新 latest.db 的 sector_mv（与 trade_date），与离线 build_tags_db 同表结构。

    背景：/heatmap 优先读 SQLite latest.db，而流式落盘此前只写 JSON，
    导致手动分析后表头（读 JSON 的 breadth）已是新交易日、热力图（读 db）
    仍是旧交易日的错配。只替换 sector_mv + meta，tags 标签库留给离线全量重建。
    永不抛异常。
    """
    try:
        import sqlite3

        rows: list[tuple] = []
        for cat, items in (("shenwan", heatmap_shenwan), ("concept", heatmap_concept)):
            for it in items or []:
                if not isinstance(it, dict):
                    continue
                rows.append(
                    (
                        cat,
                        it.get("name"),
                        it.get("value"),
                        it.get("pct_change"),
                        it.get("leader"),
                        it.get("leader_pct"),
                    )
                )
        if not rows:
            return
        db_path = out_dir / "latest.db"
        con = sqlite3.connect(str(db_path), timeout=30)
        try:
            con.execute(
                "CREATE TABLE IF NOT EXISTS sector_mv("
                "category TEXT, name TEXT, value REAL, "
                "pct_change REAL, leader TEXT, leader_pct REAL)"
            )
            con.execute(
                "CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)"
            )
            con.execute("DELETE FROM sector_mv")
            con.executemany("INSERT INTO sector_mv VALUES(?,?,?,?,?,?)", rows)
            con.execute(
                "INSERT INTO meta(key, value) VALUES('trade_date', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (trade_date,),
            )
            con.commit()
        finally:
            con.close()
        logger.info(
            "[market-analysis][stream] latest.db sector_mv 已刷新 trade_date=%s rows=%d",
            trade_date,
            len(rows),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[market-analysis][stream] latest.db 刷新失败: %s", exc)


# 受限分析执行器：最多 2 个并发线程。
# asyncio.to_thread/run_in_executor 无法取消已启动的线程，客户端断连后线程仍会跑完；
# 若不限制并发，多次断连会留下大量 DuckDB 重查询线程，打满 CPU/磁盘把 API 事件循环饿死。
_ANALYSIS_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=2, thread_name_prefix="qm-market-analysis"
)


async def _run_step(name: str, func, *args) -> Any:
    """在受限线程池中执行单个分析步骤，避免阻塞 API 事件循环。"""
    loop = asyncio.get_running_loop()
    step_start = time.monotonic()
    result = await loop.run_in_executor(_ANALYSIS_EXECUTOR, func, *args)
    logger.info(
        "[market-analysis][stream] step=%s duration_ms=%.0f",
        name,
        (time.monotonic() - step_start) * 1000,
    )
    return result


@router.post("/analyze/stream")
async def trigger_market_analysis_stream(
    request: Request,
    current_user: dict = Depends(get_current_user),
) -> StreamingResponse:
    """手动触发市场分析（SSE 流式）。

    清空缓存后按步骤顺序执行，每算完一部分立即推送该部分数据：
    indices -> breadth -> heatmap -> stock_flow -> done。
    前端边收边渲染，无需等全部分析完成。
    客户端断连时立即停止后续步骤（当前线程跑完后自然结束，不浪费资源）。

    注意：手动触发强制绕过离线快照，始终从 QuantDB 实时计算最新交易日数据，
    避免快照存在时秒回旧数据导致前端无法刷新。
    """
    _ = current_user

    async def event_stream():
        yield _sse("start", {"message": "开始读取市场分析数据…"})
        try:
            # 手动触发必须绕过离线快照，强制清缓存并实时从 QuantDB 计算最新数据，
            # 否则快照存在时秒回旧快照，前端数据无法刷新。
            await asyncio.to_thread(quantdb_feed.clear_cache)
            latest_dt = await _run_step("trade_date", quantdb_feed._latest_trade_date)
            if await request.is_disconnected():
                return

            indices = await _run_step("indices", quantdb_feed.get_indices_overview)
            yield _sse("indices", {"indices": indices})
            if await request.is_disconnected():
                return

            breadth = await _run_step("breadth", quantdb_feed.get_market_breadth)
            yield _sse("breadth", {"breadth": breadth})
            if await request.is_disconnected():
                return

            heatmap = await _run_step(
                "heatmap", quantdb_feed.get_sector_heatmap, "shenwan"
            )
            yield _sse("heatmap", {"heatmap": heatmap})
            if await request.is_disconnected():
                return

            stock_flow = await _run_step(
                "stock_flow", quantdb_feed.get_stock_money_flow, 50
            )
            yield _sse("stock_flow", {"stock_flow": stock_flow})
            if await request.is_disconnected():
                return

            # 手动分析算完即落盘（概念热力/全量个股/多周期一并补算），否则离开页面
            # 重进时只能读到旧 latest.json，表现为“分析完一切换就回退到上一交易日”。
            heatmap_concept = await _run_step(
                "heatmap_concept", quantdb_feed.get_sector_heatmap, "concept"
            )
            if await request.is_disconnected():
                return
            stock_flow_full = await _run_step(
                "stock_flow_full", quantdb_feed.get_stock_money_flow_full
            )
            if await request.is_disconnected():
                return
            money_flow_periods: dict[str, Any] = {}
            for _period, _dim, _cat in _PERSIST_PERIOD_COMBOS:
                if await request.is_disconnected():
                    return
                items = await _run_step(
                    f"period_{_period}_{_dim}_{_cat}",
                    quantdb_feed.get_money_flow_period,
                    _period,
                    _dim,
                    _cat,
                    31,
                )
                if items:
                    money_flow_periods[f"{_period}_{_dim}_{_cat}"] = items

            trade_date = str((breadth or {}).get("trade_date") or "").strip()
            if not trade_date and latest_dt:
                _raw = str(latest_dt).strip().replace("-", "")
                if len(_raw) == 8 and _raw.isdigit():
                    trade_date = f"{_raw[:4]}-{_raw[4:6]}-{_raw[6:8]}"
            persisted_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            persisted = None
            if trade_date:
                persisted = await asyncio.to_thread(
                    _persist_stream_snapshot,
                    trade_date=trade_date,
                    generated_at=persisted_at,
                    indices=indices,
                    breadth=breadth,
                    heatmap_shenwan=heatmap,
                    heatmap_concept=heatmap_concept,
                    stock_flow=stock_flow,
                    stock_flow_full=stock_flow_full,
                    money_flow_periods=money_flow_periods,
                )

            yield _sse(
                "done",
                {
                    "trade_date": latest_dt,
                    "persisted_trade_date": persisted,
                    "message": "已从 QuantDB 读取最新数据并完成市场分析",
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                },
            )
        except Exception as exc:  # pragma: no cover - 兜底
            logger.exception("[market-analysis][stream] 流式分析执行失败")
            yield _sse("error", {"message": f"市场分析失败: {exc}"})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
