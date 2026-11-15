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

from backend.services.api.market_analysis_hk import (
    hk_heatmap_drill as drill,
    hk_index_model,
    hk_opportunity as opp,
    hk_smart_money as hksm,
    quanthk_feed as feed,
)
from backend.services.api.user_app.middleware.auth import get_current_user
from backend.shared.model_registry import model_registry_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/market-analysis-hk", tags=["Market Analysis HK"])


def _fatal(exc: Exception) -> HTTPException:
    logger.exception("[market-analysis-hk] 请求失败")
    return HTTPException(status_code=500, detail=f"港股市场分析失败: {exc}")


def _is_hk_model(model: dict) -> bool:
    meta = model.get("metadata_json") or {}
    if isinstance(meta, str):
        meta = {}
    raw = str(meta.get("market") or (meta.get("context") or {}).get("market") or "")
    if raw.upper() in {"HK", "HONG_KONG", "港股", "HKEX", "XHKG"}:
        return True
    return "mdl_hk_" in str(model.get("model_id") or "").lower()


async def _resolve_hk_model(
    *, tenant_id: str, user_id: str, model_id: str | None
) -> dict | None:
    """显式 model_id 优先；否则默认港股模型；再否则最近一个 ready/active 港股模型。"""
    if model_id:
        model = await model_registry_service.get_model(
            tenant_id=tenant_id, user_id=user_id, model_id=model_id
        )
        if not model:
            raise HTTPException(status_code=404, detail=f"模型不存在: {model_id}")
        if not _is_hk_model(model):
            raise HTTPException(status_code=422, detail="该模型不是港股模型")
        return model
    model = await model_registry_service.get_default_model(
        tenant_id=tenant_id, user_id=user_id, market="HK"
    )
    if model:
        return model
    models = await model_registry_service.list_models(
        tenant_id=tenant_id, user_id=user_id, market="HK"
    )
    return next(
        (m for m in models if str(m.get("status") or "") in {"ready", "active"}), None
    )


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


@router.get("/indices/model-scores")
async def get_indices_model_scores(
    model_id: str | None = Query(
        default=None, max_length=120, description="港股模型 ID；缺省取默认/最新可用模型"
    ),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """宽基指数模型排序分：HSI/HSCEI/HSTECH/HSCCI 成分聚合（只描述位置）。"""
    tenant_id = str(current_user.get("tenant_id") or "default")
    user_id = str(current_user.get("user_id") or current_user.get("sub") or "")
    try:
        model = await _resolve_hk_model(
            tenant_id=tenant_id, user_id=user_id, model_id=model_id
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _fatal(exc) from exc
    if model is None:
        return {
            "available": False,
            "reason": "暂无可用港股模型（请先在模型管理训练或设默认）",
            "model": None,
            "indices": [],
        }
    try:
        return await asyncio.to_thread(hk_index_model.get_index_model_scores, model)
    except Exception as exc:  # noqa: BLE001
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


@router.get("/heatmap/drill")
async def get_heatmap_drill(
    sector: str = Query(
        ..., min_length=1, max_length=60, description="行业名（恒生口径）"
    ),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """L2 下钻：行业成分股（成交额降序，≤300 只；父子和账，行业名不存在 404）。"""
    _ = current_user
    try:
        result = await asyncio.to_thread(drill.get_heatmap_drill, sector)
    except Exception as exc:
        raise _fatal(exc) from exc
    if result is None:
        raise HTTPException(status_code=404, detail=f"行业不存在: {sector}")
    return result


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


# ---- 机会雷达（因子复合评分）----


@router.get("/opportunity/stocks")
async def get_opportunity_stocks(
    limit: int = Query(default=30, ge=5, le=100),
    sector: str | None = Query(default=None, max_length=60),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """机会个股榜：六柱因子复合评分降序（含行业排名/触发标签/柱分解）。"""
    _ = current_user
    try:
        return await asyncio.to_thread(opp.get_opportunity_stocks, limit, sector)
    except Exception as exc:
        raise _fatal(exc) from exc


@router.get("/opportunity/sectors")
async def get_opportunity_sectors(
    limit: int = Query(default=12, ge=3, le=31),
    min_stocks: int = Query(default=8, ge=3, le=50),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """机会行业榜：行业中位机会分 + 机会宽度 + 南向变化 + 前排个股。"""
    _ = current_user
    try:
        return await asyncio.to_thread(opp.get_opportunity_sectors, limit, min_stocks)
    except Exception as exc:
        raise _fatal(exc) from exc


@router.get("/opportunity/risk")
async def get_risk_watch(
    limit: int = Query(default=20, ge=5, le=50),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """风险警示榜：机会分对称反向 + 事实理由标注（弱势/波动/减持/筹码弱）。"""
    _ = current_user
    try:
        return await asyncio.to_thread(opp.get_risk_watch, limit)
    except Exception as exc:
        raise _fatal(exc) from exc


@router.get("/opportunity/summary")
async def get_opportunity_summary(
    current_user: dict = Depends(get_current_user),
) -> dict:
    """机会雷达口径元信息：门槛/权重/因子清单/覆盖率/数据日期（分母透明度）。"""
    _ = current_user
    try:
        return await asyncio.to_thread(opp.get_opportunity_summary)
    except Exception as exc:
        raise _fatal(exc) from exc


# ---- 机构持仓分析 ----

_INST_CATEGORIES = "^(all|cn_broker|southbound|hk|us_eu|apac|other)$"


@router.get("/institutional/overview")
async def get_institutional_overview(
    current_user: dict = Depends(get_current_user),
) -> dict:
    """市场机构持仓结构：按资金属性分类的持仓市值 / 占比 / 5 日增减持家数。"""
    _ = current_user
    try:
        return await asyncio.to_thread(feed.get_institutional_overview)
    except Exception as exc:
        raise _fatal(exc) from exc


@router.get("/institutional/movers")
async def get_institutional_movers(
    category: str = Query(default="all", pattern=_INST_CATEGORIES),
    window: int = Query(default=5, ge=1, le=60),
    direction: str = Query(default="increase", pattern="^(increase|decrease)$"),
    limit: int = Query(default=20, ge=5, le=50),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """机构增减持榜：窗口间某资金属性分类净增/净减的股票排行。"""
    _ = current_user
    try:
        return await asyncio.to_thread(
            feed.get_institutional_movers, category, window, direction, limit
        )
    except Exception as exc:
        raise _fatal(exc) from exc


@router.get("/institutional/stock")
async def get_institutional_stock(
    symbol: str = Query(..., description="个股代码，如 0700.HK 或 0700"),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """个股机构持仓：分类结构 + 参与者明细 + 南向口径 + 60 日分类持仓趋势。"""
    _ = current_user
    try:
        return await asyncio.to_thread(feed.get_institutional_stock, symbol)
    except Exception as exc:
        raise _fatal(exc) from exc


@router.get("/institutional/stocks/suggest")
async def get_institutional_stock_suggest(
    q: str = Query(..., min_length=1, max_length=30),
    limit: int = Query(default=10, ge=1, le=20),
    current_user: dict = Depends(get_current_user),
) -> list[dict]:
    """个股搜索建议（symbol 前缀 / 中英文名，简繁双字形）。"""
    _ = current_user
    try:
        return await asyncio.to_thread(feed.get_institutional_stock_suggest, q, limit)
    except Exception as exc:
        raise _fatal(exc) from exc


@router.get("/institutional/participants")
async def get_institutional_participants(
    category: str = Query(default="all", pattern=_INST_CATEGORIES),
    q: str = Query(default="", max_length=60),
    limit: int = Query(default=50, ge=5, le=200),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """参与者分类审计：席位/名称 → 分类/性质/最新日持仓，支持分类与关键词过滤。"""
    _ = current_user
    try:
        return await asyncio.to_thread(
            feed.get_institutional_participants, category, q, limit
        )
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
                ("opportunity_stocks", opp.get_opportunity_stocks, 30),
                ("opportunity_sectors", opp.get_opportunity_sectors, 12, 8),
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


# ---- 聪明钱（细案 §5；只读产物，无产物 404 hk_smart_money_not_built） ----


async def _hksm_read(fn, *args, **kwargs):
    """产物读侧统一调度：线程池执行 + 未建/查无 404 / 参数 400 收敛。"""
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except hksm.HkSmartMoneyNotBuilt:
        raise HTTPException(
            status_code=404, detail="hk_smart_money_not_built"
        ) from None
    except hksm.HkSeatNotFound:
        raise HTTPException(status_code=404, detail="seat_not_found") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except Exception as exc:
        raise _fatal(exc) from exc


@router.get("/smart-money/overview")
async def get_hk_smart_money_overview(
    current_user: dict = Depends(get_current_user),
) -> dict:
    """概览：新鲜度 + 类型面板（持仓/5d/20d 净额 + 入柱 + 最强变体）+ 60 日趋势。

    类型效力为历史统计（双过闸裁定口径），非模型预测、非交易指令。
    """
    _ = current_user
    return await _hksm_read(hksm.get_overview)


@router.get("/smart-money/types")
async def get_hk_smart_money_types(
    horizon: str = Query(default="10", description="聚焦窗口 5/10/20（入柱裁定取 10）"),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """类型效力全网格：24 信号 × h5/h10/h20（IC/分位超额/命中/裁定格）。"""
    _ = current_user
    return await _hksm_read(hksm.get_types, horizon)


@router.get("/smart-money/seats")
async def get_hk_smart_money_seats(
    window: str = Query(default="60", description="窗口：20 / 60 / all（0 同 all）"),
    category: str | None = Query(
        default=None, description="类型：cn_broker / us_eu / hk / apac / other"
    ),
    sort: str = Query(
        default="lb_skill",
        description="排序：lb_skill / skill / vw_skill / avg_exc / shrunk_hit / n",
    ),
    min_events: int = Query(
        default=5, ge=1, le=1000, description="最少事件数（行级过滤）"
    ),
    limit: int = Query(
        default=hksm.SEATS_LIMIT_DEFAULT,
        ge=5,
        le=hksm.SEATS_LIMIT_MAX,
        description="返回条数",
    ),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """席位榜：方向技能（90% 下界默认排序）/胜率/收缩——事后统计，非预测。"""
    _ = current_user
    return await _hksm_read(
        hksm.get_seats,
        window=window,
        category=category,
        sort=sort,
        min_events=min_events,
        limit=limit,
    )


@router.get("/smart-money/seat/{participant_id}")
async def get_hk_smart_money_seat_profile(
    participant_id: str,
    current_user: dict = Depends(get_current_user),
) -> dict:
    """席位档案：三窗统计 + 月度战绩 + 活跃标的 + 最近事件（查无 404）。"""
    _ = current_user
    return await _hksm_read(hksm.get_seat_profile, participant_id)


@router.get("/smart-money/persistence")
async def get_hk_smart_money_persistence(
    current_user: dict = Depends(get_current_user),
) -> dict:
    """持续性审计：散点/秩相关/四分位 OOS/类别前后半（构建期实算，如实披露）。"""
    _ = current_user
    return await _hksm_read(hksm.get_persistence)


@router.get("/smart-money/consensus")
async def get_hk_smart_money_consensus(
    limit: int = Query(
        default=hksm.CONSENSUS_LIMIT_DEFAULT,
        ge=5,
        le=hksm.CONSENSUS_LIMIT_MAX,
        description="返回条数",
    ),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """共识榜：入柱信号截面分位等权复合分；零入柱 → 空榜 + evidence_only=true。"""
    _ = current_user
    return await _hksm_read(hksm.get_consensus, limit)


@router.get("/smart-money/stock/{symbol}")
async def get_hk_smart_money_stock(
    symbol: str,
    days: int = Query(
        default=hksm.STOCK_DAYS_DEFAULT,
        ge=hksm.STOCK_DAYS_MIN,
        le=hksm.STOCK_DAYS_MAX,
        description="窗口（交易日）",
    ),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """个股档案：事件时间线 + 类型持仓快照 + 窗口统计（无事件也 200）。"""
    _ = current_user
    return await _hksm_read(hksm.get_stock, symbol, days)


@router.get("/smart-money/evidence")
async def get_hk_smart_money_evidence(
    current_user: dict = Depends(get_current_user),
) -> dict:
    """证据台：协议/全网格裁定/标定网格/排除计数/A 席对账/方法学/警告。"""
    _ = current_user
    return await _hksm_read(hksm.get_evidence)
