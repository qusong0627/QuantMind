"""选股 API — 实盘选股（策略文档 v2.0）。

数据流:
  engine_signal_scores (DB)  →  读取最新/指定交易日信号
      →  申万行业映射 → 行业信号（avgTop1、强行业数、市场状态）
      →  个股分数区间 + 主板 + ST/涨跌停 + 3 天趋势过滤
      →  返回候选股、行业排行、被排除示例

口径说明:
  - engine_signal_scores.trade_date 存的是「信号生效日」(T+1)，与回测引擎一致：
    取 trade_date 作为「推理完成日」，买入在 T+1 执行。
  - symbol 格式混杂（sh600519 / 600036.SH / SH600036），统一经 StockCodeUtil.to_suffix 归一。
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, timedelta
from pathlib import Path
from time import time as _now
from typing import Any

import pandas as pd
from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import text

from backend.shared.database_manager_v2 import get_session
from backend.shared.stock_utils import StockCodeUtil

from backend.services.engine.auth_context import get_authenticated_identity
from backend.services.engine.inference.inference_backtest_service import (
    _compute_industry_signals,
    _market_state,
    _select_stocks_daily,
    StrategyConfig,
)
from backend.services.engine.inference.shenwan_industry import (
    load_shenwan_industry_map,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/selection", tags=["Selection"])

# 策略预设参数（与回测引擎 preset 对齐）
_PRESETS: dict[str, dict[str, float | int]] = {
    "conservative": {"entry": 0.10, "exit": 0.10, "strong_min": 5},
    "balanced": {"entry": 0.09, "exit": 0.06, "strong_min": 2},
    "aggressive": {"entry": 0.07, "exit": 0.06, "strong_min": 1},
}


def _resolve_config(strategy: str) -> StrategyConfig:
    preset = _PRESETS.get(strategy, _PRESETS["balanced"])
    cfg = StrategyConfig()
    cfg.entry_threshold = float(preset["entry"])
    cfg.exit_threshold = float(preset["exit"])
    cfg.strong_industry_min = int(preset["strong_min"])
    return cfg


def _position_advice(avg_top1: float, strong_count: int) -> dict[str, str]:
    """按仓位管理表给出仓位建议。"""
    if avg_top1 >= 0.12 and strong_count >= 5:
        return {"position": "100%", "reason": "牛市，满仓可追强信号"}
    if avg_top1 >= 0.10 and strong_count >= 3:
        return {"position": "50%", "reason": "震荡偏强，半仓只做强区间"}
    if avg_top1 >= 0.09 and strong_count >= 2:
        return {"position": "30%", "reason": "震荡，轻仓快进快出"}
    if avg_top1 >= 0.06:
        return {"position": "0-30%", "reason": "震荡偏弱，观望或极轻仓"}
    return {"position": "0%", "reason": "熊市，绝对空仓"}


async def _load_signal_day(
    tenant_id: str,
    user_id: str,
    trade_date: str | None,
) -> tuple[str, list[dict[str, Any]]]:
    """读取指定交易日（或最新）的信号。返回 (resolved_date, [{symbol, fusion_score}])。"""
    params: dict[str, Any] = {"tenant_id": tenant_id, "user_id": user_id}

    if trade_date:
        params["trade_date"] = date.fromisoformat(trade_date)
        where = "s.trade_date = :trade_date"
    else:
        where = """
            s.trade_date = (
                SELECT MAX(trade_date) FROM engine_signal_scores
                WHERE tenant_id = :tenant_id AND user_id = :user_id
                  AND (universe_tag IS NULL OR universe_tag = 'CN')
            )
        """

    query = text(
        f"""
        SELECT s.symbol, s.fusion_score, s.trade_date
        FROM engine_signal_scores s
        WHERE s.tenant_id = :tenant_id AND s.user_id = :user_id
          AND (s.universe_tag IS NULL OR s.universe_tag = 'CN') AND {where}
        ORDER BY s.fusion_score DESC
        """
    )

    rows = []
    async with get_session(read_only=True) as session:
        result = await session.execute(query, params)
        rows = result.mappings().all()

    if not rows:
        return (trade_date or "", [])

    resolved_date = str(rows[0]["trade_date"] or trade_date or "")
    signals = [
        {"symbol": str(r["symbol"]).upper(), "fusion_score": float(r["fusion_score"])}
        for r in rows
        if r["fusion_score"] is not None
    ]
    return (resolved_date, signals)


async def _load_stock_names(
    symbols: list[str],
) -> dict[str, str]:
    """批量查股票名称（stock_daily_latest.stock_name，symbol 前缀格式）。"""
    if not symbols:
        return {}
    prefix_map: dict[str, str] = {}
    for s in symbols:
        try:
            prefix_map[StockCodeUtil.to_prefix(s)] = s
        except Exception:
            continue
    if not prefix_map:
        return {}

    result_map: dict[str, str] = {}
    async with get_session(read_only=True) as session:
        for chunk in _chunks(list(prefix_map.keys()), 500):
            q = text(
                """
                SELECT DISTINCT ON (symbol) symbol, stock_name
                FROM stock_daily_latest
                WHERE symbol = ANY(:codes)
                  AND stock_name IS NOT NULL AND stock_name != ''
                ORDER BY symbol, trade_date DESC
                """
            )
            res = await session.execute(q, {"codes": chunk})
            for row in res.mappings():
                prefix = str(row["symbol"] or "").strip().upper()
                suffix = prefix_map.get(prefix)
                if suffix:
                    result_map[suffix] = str(row["stock_name"] or "")
    return result_map


def _chunks(items: list[str], n: int):
    for i in range(0, len(items), n):
        yield items[i : i + n]


async def _load_index_above_ma20(target_date: str | None = None) -> tuple[bool, str]:
    """上证指数在指定日期（缺省最新）收盘 vs MA20。"""
    try:
        from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub

        hub = QuantDBDataHub()
        end = date.fromisoformat(target_date) if target_date else date.today()
        start = end - timedelta(days=40)
        df = hub.fetch_index_kline("000001.SH", start, end)
        if df is None or df.empty:
            return True, "无指数数据"
        df = df.sort_values("trade_date")
        close = df["close"].astype(float)
        close.index = pd.DatetimeIndex(pd.to_datetime(df["trade_date"]))
        # 取 target_date 当日（或之前最近一日）的收盘与 MA20
        up_to = close.loc[:pd.Timestamp(end)]
        if len(up_to) < 20:
            return True, "指数数据不足20日"
        last = float(up_to.iloc[-1])
        ma20 = float(up_to.rolling(20).mean().iloc[-1])
        return (last >= ma20, f"上证{last:.0f}/MA20{ma20:.0f}")
    except Exception as exc:
        logger.warning("加载指数 MA20 失败: %s", exc)
        return True, "指数数据不可用"


async def _load_price_flags(
    trade_date: str,
    symbols: list[str],
) -> dict[str, dict[str, Any]]:
    """加载个股价格/涨跌停/ST 标记（stock_daily_latest，当日或最近一条）。

    用于实盘过滤涨停买不进/跌停卖不出、ST 剔除。取当日若缺失则取最近一日。
    """
    if not symbols:
        return {}
    normalized = {StockCodeUtil.to_prefix(s): s for s in symbols}
    flags: dict[str, dict[str, Any]] = {}
    d_param = date.fromisoformat(trade_date) if trade_date else None
    if d_param is None:
        return {}
    async with get_session(read_only=True) as session:
        seen: set[str] = set()
        for chunk in _chunks(list(normalized.keys()), 300):
            # DISTINCT ON 取每个 symbol 最新一条（<= trade_date），避免拉全历史
            q = text(
                """
                SELECT DISTINCT ON (symbol) symbol, trade_date, pct_change, is_st
                FROM stock_daily_latest
                WHERE symbol = ANY(:codes)
                  AND trade_date <= :d
                ORDER BY symbol, trade_date DESC
                """
            )
            res = await session.execute(q, {"codes": chunk, "d": d_param})
            for row in res.mappings():
                sym = str(row["symbol"] or "").strip().upper()
                suffix = normalized.get(sym)
                if suffix is None or suffix in seen:
                    continue
                seen.add(suffix)
                flags[suffix] = {
                    "pct_change": float(row["pct_change"]) if row["pct_change"] is not None else None,
                    "is_st": int(row["is_st"] or 0),
                }
    return flags


@router.get("/daily")
async def daily_selection(
    request: Request,
    strategy: str = Query("balanced"),
    date: str | None = Query(None, description="信号交易日，缺省取最新"),
    ignore_ma20: bool = Query(False, description="勾选后忽略大盘MA20强制空仓，允许入场"),
):
    """今日选股：市场状态 + 行业排行 + 候选股 + 被排除示例。"""
    user_id, tenant_id = get_authenticated_identity(request)
    cfg = _resolve_config(strategy)

    # 1. 读信号
    trade_date, signals = await _load_signal_day(tenant_id, user_id, date)
    if not signals:
        return {
            "status": "success",
            "meta": {"trade_date": trade_date or None, "strategy": strategy, "total_signals": 0},
            "market_state": {"state": "无信号", "should_enter": False, "position_advice": "0%"},
            "industry_signals": [],
            "candidates": [],
            "excluded_examples": [],
            "warnings": [f"无推理信号（tenant={tenant_id}, user={user_id}, date={date or '最新'}）"],
        }

    # 2. 归一化 symbol → DataFrame[symbol, score]
    day_scores = pd.DataFrame(
        [{"symbol": s["symbol"], "score": s["fusion_score"]} for s in signals]
    )
    day_scores["symbol"] = day_scores["symbol"].map(StockCodeUtil.to_suffix)
    day_scores = day_scores.drop_duplicates(subset="symbol", keep="last")

    # 过滤非有限分数（NaN/±Inf），避免下游 JSON 序列化失败
    _num_scores = pd.to_numeric(day_scores["score"], errors="coerce")
    day_scores = day_scores[_num_scores.notna() & _num_scores.abs().ne(float("inf"))].copy()

    # 3. 行业信号
    industry_map = load_shenwan_industry_map()
    ind_top1, ind_count, avg_top1, strong_count = _compute_industry_signals(
        day_scores, industry_map
    )
    state = _market_state(avg_top1, strong_count)
    index_above_ma20, index_detail = await _load_index_above_ma20(trade_date or None)
    position = _position_advice(avg_top1, strong_count)

    # 4. 入场判断 + 选股
    ma20_ok = index_above_ma20 or ignore_ma20
    should_enter = (
        ma20_ok
        and avg_top1 >= cfg.entry_threshold
        and strong_count >= cfg.strong_industry_min
    )
    candidates: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []

    if should_enter:
        # 只对分数区间内的候选股查价格/ST 标记（避免全市场 19 批查询）
        score_mask = (day_scores["score"] >= cfg.score_min) & (day_scores["score"] <= cfg.score_max)
        if cfg.main_board_only:
            score_mask &= day_scores["symbol"].apply(_is_main_board_code)
        score_mask &= ~day_scores["symbol"].apply(_is_star_market_code)
        cand_symbols = day_scores.loc[score_mask, "symbol"].tolist()
        price_flags = await _load_price_flags(trade_date, cand_symbols)
        # 把实盘价格/ST 标记转成回测选股所需的 price_day DataFrame
        price_rows = [
            {
                "symbol": sym,
                "pct_change": fl["pct_change"],
                "is_st": fl["is_st"],
            }
            for sym, fl in price_flags.items()
        ]
        price_day = pd.DataFrame(price_rows) if price_rows else pd.DataFrame()
        picks = _select_stocks_daily(day_scores, industry_map, cfg, price_day)
        candidates = picks
    else:
        reason = []
        if not index_above_ma20 and not ignore_ma20:
            reason.append(f"大盘跌破MA20（{index_detail}）")
        if avg_top1 < cfg.entry_threshold:
            reason.append(f"行业avgTop1={avg_top1:.3f} 低于入场线{cfg.entry_threshold}")
        if strong_count < cfg.strong_industry_min:
            reason.append(f"强行业数{strong_count} 低于阈值{cfg.strong_industry_min}")
        excluded.append({
            "symbol": "", "score": 0, "reason": "未入场",
            "detail": "；".join(reason) or "市场状态不满足入场条件",
        })

    # 5. 行业排行（Top1 排序前 15，单次 groupby 取各行业最高分股）
    top_stock_by_ind = _top_stock_by_industry(day_scores, industry_map)
    industry_signals = sorted(
        [{"industry": i, "top1": v, "stock": top_stock_by_ind.get(i, "")}
         for i, v in ind_top1.items()],
        key=lambda x: -x["top1"],
    )[:15]

    # 6. 股票名称补齐
    cand_symbols = [c["symbol"] for c in candidates]
    name_map = await _load_stock_names(cand_symbols)
    for c in candidates:
        c["name"] = name_map.get(c["symbol"], "")
        # 买入理由
        reasons = ["黄金区间" if cfg.score_min <= c["score"] <= cfg.score_max else "分数区间"]
        if c.get("trend") in ("先升后降", "上升中", "明日回落"):
            reasons.append("先升后降")
        if _is_main_board_code(c["symbol"]):
            reasons.append("主板")
        ind = c.get("industry", "")
        if ind in ind_top1 and ind_top1[ind] >= cfg.entry_threshold:
            reasons.append("行业确认")
        c["buy_reason"] = "+".join(reasons)
        c["warnings"] = []

    return {
        "status": "success",
        "meta": {
            "trade_date": trade_date,
            "strategy": strategy,
            "total_signals": len(signals),
            "strategy_config": {
                "entry_threshold": cfg.entry_threshold,
                "exit_threshold": cfg.exit_threshold,
                "strong_industry_min": cfg.strong_industry_min,
                "score_min": cfg.score_min,
                "score_max": cfg.score_max,
                "max_positions": cfg.max_positions,
            },
        },
        "market_state": {
            "state": state,
            "avg_top1": round(avg_top1, 4),
            "strong_count": strong_count,
            "index_above_ma20": index_above_ma20,
            "index_detail": index_detail,
            "ignore_ma20": ignore_ma20,
            "should_enter": should_enter,
            "position": position["position"],
            "position_reason": position["reason"],
        },
        "industry_signals": industry_signals,
        "candidates": candidates,
        "excluded_examples": excluded,
        "warnings": [],
    }


@router.get("/history")
async def selection_history(
    request: Request,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
    strategy: str = Query("balanced"),
):
    """历史选股（按天重算，供回看）。"""
    user_id, tenant_id = get_authenticated_identity(request)
    cfg = _resolve_config(strategy)
    industry_map = load_shenwan_industry_map()

    query = text(
        """
        SELECT trade_date, symbol, fusion_score
        FROM engine_signal_scores
        WHERE tenant_id = :tenant_id AND user_id = :user_id
          AND (universe_tag IS NULL OR universe_tag = 'CN')
          AND trade_date BETWEEN :from AND :to
        ORDER BY trade_date, fusion_score DESC
        """
    )
    rows = []
    async with get_session(read_only=True) as session:
        result = await session.execute(
            query, {
                "tenant_id": tenant_id,
                "user_id": user_id,
                "from": date.fromisoformat(from_date),
                "to": date.fromisoformat(to_date),
            }
        )
        rows = result.mappings().all()

    if not rows:
        return {"status": "success", "days": [], "total": 0}

    panel = pd.DataFrame(
        [{"trade_date": str(r["trade_date"]), "symbol": str(r["symbol"]).upper(),
          "score": float(r["fusion_score"])} for r in rows if r["fusion_score"] is not None]
    )
    panel["symbol"] = panel["symbol"].map(StockCodeUtil.to_suffix)

    days: list[dict[str, Any]] = []
    for trade_date, group in panel.groupby("trade_date", sort=True):
        day_df = group.drop_duplicates(subset="symbol", keep="last")
        ind_top1, _, avg_top1, strong_count = _compute_industry_signals(day_df, industry_map)
        state = _market_state(avg_top1, strong_count)
        should_enter = avg_top1 >= cfg.entry_threshold and strong_count >= cfg.strong_industry_min
        picks = _select_stocks_daily(day_df, industry_map, cfg, None) if should_enter else []
        days.append({
            "trade_date": trade_date,
            "state": state,
            "avg_top1": round(avg_top1, 4),
            "strong_count": strong_count,
            "should_enter": should_enter,
            "candidates": picks,
        })

    return {"status": "success", "days": days, "total": len(days)}


def _top_stock_by_industry(
    day_scores: pd.DataFrame,
    industry_map: dict[str, str],
) -> dict[str, str]:
    """单次返回各行业分数最高股票的 symbol（避免逐行业反复排序）。"""
    joined = day_scores.copy()
    joined["industry"] = joined["symbol"].map(industry_map)
    joined = joined[joined["industry"].notna() & (joined["industry"] != "")]
    if joined.empty:
        return {}
    idx = joined.groupby("industry")["score"].idxmax()
    return {joined.loc[i, "industry"]: str(joined.loc[i, "symbol"]) for i in idx.values}


def _is_main_board_code(symbol: str) -> bool:
    s = symbol.split(".")[0] if "." in symbol else symbol
    return s.startswith(("600", "601", "603", "605", "000", "001", "002"))


def _is_star_market_code(symbol: str) -> bool:
    s = symbol.split(".")[0] if "." in symbol else symbol
    return s.startswith(("688", "300", "301"))


# ---------------------------------------------------------------------------
# 负分多空参考（QuantMind负分精确落地规则）
# 分数×市值×板块 网格矩阵，依据 2024-2026 跨年 328万条 统计
# ---------------------------------------------------------------------------

def _cap_bucket(total_mv: float | None) -> str:
    """按总市值(元)分桶：微盘<30亿 小盘30-100亿 中盘100-300亿 大盘300-1000亿 超大盘>1000亿。"""
    if total_mv is None:
        return "未知"
    if total_mv < 3e9:
        return "微盘"
    if total_mv < 1e10:
        return "小盘"
    if total_mv < 3e10:
        return "中盘"
    if total_mv < 1e11:
        return "大盘"
    return "超大盘"


def _board_type(symbol: str) -> str:
    """板块类型：沪深主板 / 创业板 / 科创板 / 北交所 / 其他。"""
    s = symbol.split(".")[0] if "." in symbol else symbol
    if s.startswith("688"):
        return "科创板"
    if s.startswith(("300", "301")):
        return "创业板"
    if s.startswith(("8", "4", "92")) and len(s) == 6 and not s.startswith(("00", "30", "60")):
        # 北交所：43/83/87/88/92 开头
        if s.startswith(("43", "83", "87", "88", "92")):
            return "北交所"
    if s.startswith(("600", "601", "603", "605", "000", "001", "002", "003")):
        return "沪深主板"
    return "其他"


def _short_signal(score: float, cap: str) -> tuple[bool, str]:
    """按研究矩阵判断是否做空/回避。返回 (是否做空, 理由)。"""
    if score >= -0.06:
        return False, "轻负分(>-0.06)无信息"
    # 大盘/超大盘：负分是错杀，不做空
    if cap in ("大盘", "超大盘"):
        if score <= -0.22:
            return True, "超大盘跌破警戒线-0.22，大盘股也会崩"
        return False, "大盘/超大盘负分是错杀"
    # 科创板负分抗跌，做空价值最低
    return True, "负分可做空"


def _missed_opportunity(score: float, cap: str, board: str) -> bool:
    """判断是否为「负分错杀」（值得关注反弹）。"""
    if score >= -0.06:
        return False
    # 超大盘 -0.13~-0.14 上涨概率 56.8%
    if cap == "超大盘" and -0.14 <= score <= -0.13:
        return True
    # 大盘 -0.11 上涨概率 51.3%
    if cap == "大盘" and -0.115 <= score <= -0.105:
        return True
    # 科创板 -0.06~-0.15 均收全为正
    if board == "科创板" and score >= -0.15:
        return True
    # 银行等行业错杀
    return False


# 市值快照缓存：stock_daily_latest 每次写入全表最新交易日，一天内市值基本不变，
# 无需每个请求都跑 5s 的 DISTINCT ON 全表扫描。
_cap_cache: dict[str, float | None] = {}
_cap_cache_ts: float = 0.0
_CAP_CACHE_TTL = 600.0  # 10 分钟


async def _load_cap_snapshot() -> dict[str, float | None]:
    """加载全表最新交易日的市值快照（prefix symbol → 市值元），带缓存。"""
    global _cap_cache, _cap_cache_ts
    if _cap_cache and (_now() - _cap_cache_ts) < _CAP_CACHE_TTL:
        return _cap_cache

    caps: dict[str, float | None] = {}
    async with get_session(read_only=True) as session:
        # 优先取最新交易日；若该日 total_mv 未回填（全 NULL），回退到最近有市值数据的交易日
        r = await session.execute(text(
            "SELECT trade_date, COUNT(total_mv) AS mv_cnt FROM stock_daily_latest "
            "GROUP BY trade_date ORDER BY trade_date DESC LIMIT 5"
        ))
        target_date = None
        for row in r.mappings():
            if int(row["mv_cnt"] or 0) > 1000:
                target_date = row["trade_date"]
                break
        if target_date is None:
            return caps

        r = await session.execute(text(
            "SELECT symbol, total_mv FROM stock_daily_latest "
            "WHERE trade_date = :d AND total_mv IS NOT NULL"
        ), {"d": target_date})
        for row in r.mappings():
            caps[str(row["symbol"]).strip().upper()] = float(row["total_mv"])
    _cap_cache = caps
    _cap_cache_ts = _now()
    return caps


async def _load_cap_and_name(
    symbols: list[str],
) -> tuple[dict[str, float | None], dict[str, str]]:
    """批量加载市值(元)和股票名称。

    市值: stock_daily_latest.total_mv（prefix 格式 SH600172，最新交易日快照，带缓存）
    名称: stocks.name（suffix 格式 600172.SH）
    返回 ({suffix_symbol: total_mv}, {suffix_symbol: name})
    """
    caps: dict[str, float | None] = {}
    names: dict[str, str] = {}
    if not symbols:
        return caps, names

    prefix_map = {StockCodeUtil.to_prefix(s): s for s in symbols}
    snapshot = await _load_cap_snapshot()
    for prefix, suffix in prefix_map.items():
        if prefix in snapshot:
            caps[suffix] = snapshot[prefix]

    # 名称（suffix 格式）
    suffix_list = [s for s in symbols if "." in s or len(s) >= 6]
    async with get_session(read_only=True) as session:
        for chunk in _chunks(suffix_list, 500):
            q2 = text(
                "SELECT symbol, name FROM stocks WHERE symbol = ANY(:codes) "
                "AND name IS NOT NULL AND name != ''"
            )
            res2 = await session.execute(q2, {"codes": chunk})
            for row in res2.mappings():
                sym = str(row["symbol"] or "").strip().upper()
                names[sym] = str(row["name"] or "")
    return caps, names


@router.get("/negative")
async def negative_selection(
    request: Request,
    date: str | None = Query(None, description="信号交易日，缺省取最新"),
):
    """负分多空参考：做空候选 + 错杀参考 + 分数×市值分布矩阵。"""
    user_id, tenant_id = get_authenticated_identity(request)

    trade_date, signals = await _load_signal_day(tenant_id, user_id, date)
    if not signals:
        return {
            "status": "success",
            "meta": {"trade_date": trade_date or None, "total_signals": 0},
            "short_candidates": [], "missed_reference": [],
            "matrix": [], "warnings": ["无推理信号"],
        }

    # 归一化 symbol → suffix
    day_scores = pd.DataFrame(
        [{"symbol": s["symbol"], "score": s["fusion_score"]} for s in signals]
    )
    day_scores["symbol"] = day_scores["symbol"].map(StockCodeUtil.to_suffix)
    day_scores = day_scores.drop_duplicates(subset="symbol", keep="last")

    # 过滤非有限分数（NaN/±Inf），避免下游 JSON 序列化失败（Inf 会通过负分过滤）
    _num_scores = pd.to_numeric(day_scores["score"], errors="coerce")
    day_scores = day_scores[_num_scores.notna() & _num_scores.abs().ne(float("inf"))].copy()
    if day_scores.empty:
        return {
            "status": "success",
            "meta": {"trade_date": trade_date or None, "total_signals": len(signals), "negative_count": 0},
            "short_candidates": [], "missed_reference": [],
            "matrix": [], "warnings": ["信号分数均无效（非有限值）"],
        }

    # 只保留负分（研究关注 < -0.06）
    neg_df = day_scores[day_scores["score"] < -0.06].copy()
    neg_df = neg_df.sort_values("score")

    # 市值 + 名称
    all_symbols = neg_df["symbol"].tolist()
    caps, names = await _load_cap_and_name(all_symbols)
    neg_df["cap"] = neg_df["symbol"].map(lambda s: _cap_bucket(caps.get(s)))
    neg_df["board"] = neg_df["symbol"].map(_board_type)
    neg_df["name"] = neg_df["symbol"].map(names)

    # 做空候选 + 错杀参考
    short_candidates: list[dict[str, Any]] = []
    missed_reference: list[dict[str, Any]] = []
    for row in neg_df.itertuples(index=False):
        item = {
            "symbol": row.symbol,
            "name": row.name or "",
            "score": round(float(row.score), 4),
            "cap": row.cap,
            "board": row.board,
        }
        do_short, reason = _short_signal(float(row.score), row.cap)
        if do_short:
            # 做空聚焦小市值/微盘，分数越负优先级越高
            item["short_reason"] = reason
            short_candidates.append(item)
        if _missed_opportunity(float(row.score), row.cap, row.board):
            item["missed_reason"] = "负分错杀，可能反弹"
            missed_reference.append(item)

    # 做空候选按 (市值从小到大, 分数从负到正) 排序
    cap_order = {"微盘": 0, "小盘": 1, "中盘": 2, "大盘": 3, "超大盘": 4, "未知": 5}
    short_candidates.sort(key=lambda x: (cap_order.get(x["cap"], 5), x["score"]))

    # 分数×市值矩阵（统计每格股票数 + 做空建议）
    matrix: list[dict[str, Any]] = []
    score_bands = [
        ("≤-0.25", lambda s: s <= -0.25),
        ("-0.25~-0.20", lambda s: -0.25 < s <= -0.20),
        ("-0.20~-0.15", lambda s: -0.20 < s <= -0.15),
        ("-0.15~-0.10", lambda s: -0.15 < s <= -0.10),
        ("-0.10~-0.06", lambda s: -0.10 < s <= -0.06),
    ]
    cap_buckets = ["微盘", "小盘", "中盘", "大盘", "超大盘"]
    for band_label, band_fn in score_bands:
        row_entries: list[dict[str, Any]] = []
        for cap_label in cap_buckets:
            count = int(((neg_df["score"].map(band_fn)) & (neg_df["cap"] == cap_label)).sum())
            row_entries.append({"cap": cap_label, "count": count})
        matrix.append({"score_band": band_label, "caps": row_entries})

    return {
        "status": "success",
        "meta": {
            "trade_date": trade_date,
            "total_signals": len(signals),
            "negative_count": len(neg_df),
        },
        "short_candidates": short_candidates[:30],
        "missed_reference": missed_reference[:20],
        "matrix": matrix,
        "warnings": [],
    }


async def _load_industry_score_avg(
    day_scores: pd.DataFrame,
    industry_map: dict[str, str],
    *,
    positive: bool,
) -> list[dict[str, Any]]:
    """行业分数 avg：positive=True 统计正分，False 统计负分。

    返回 [{industry, count, avg, extreme}]，正分按 avg 降序（最强行业在前），
    负分按 avg 升序（最深负分行业在前）。
    """
    if day_scores.empty:
        return []
    sym_industry: dict[str, str] = {}
    for row in day_scores.itertuples(index=False):
        sym = row.symbol
        sym_industry[sym] = industry_map.get(sym) or ""

    subset = day_scores[day_scores["score"] > 0 if positive else day_scores["score"] < 0].copy()
    subset["industry"] = subset["symbol"].map(sym_industry)
    subset = subset[subset["industry"].notna() & (subset["industry"] != "")]
    if subset.empty:
        return []

    agg = subset.groupby("industry").agg(
        count=("score", "count"),
        avg=("score", "mean"),
        extreme=("score", "max" if positive else "min"),
    ).reset_index()
    agg = agg.sort_values("avg", ascending=(not positive)).reset_index(drop=True)
    prefix = "pos" if positive else "neg"
    return [
        {
            "industry": r.industry,
            f"{prefix}_count": int(r.count),
            f"{prefix}_avg": round(float(r.avg), 4),
            f"{prefix}_extreme": round(float(r.extreme), 4),
        }
        for r in agg.itertuples(index=False)
    ]


async def _load_board_score_avg(
    day_scores: pd.DataFrame,
    *,
    positive: bool,
) -> list[dict[str, Any]]:
    """板块分数 avg：主板/创业板/科创板/北交所/其他。"""
    if day_scores.empty:
        return []
    subset = day_scores[day_scores["score"] > 0 if positive else day_scores["score"] < 0].copy()
    subset["board"] = subset["symbol"].map(_board_type)
    if subset.empty:
        return []
    agg = subset.groupby("board").agg(
        count=("score", "count"),
        avg=("score", "mean"),
    ).reset_index()
    prefix = "pos" if positive else "neg"
    return [
        {
            "board": r.board,
            f"{prefix}_count": int(r.count),
            f"{prefix}_avg": round(float(r.avg), 4),
        }
        for r in agg.itertuples(index=False)
    ]


async def _load_industry_negative_avg(
    signals: list[dict[str, Any]],
    day_scores: pd.DataFrame,
    industry_map: dict[str, str],
) -> list[dict[str, Any]]:
    """负分行业 avg（兼容旧调用）。"""
    return await _load_industry_score_avg(day_scores, industry_map, positive=False)


async def _load_board_negative_avg(
    day_scores: pd.DataFrame,
) -> list[dict[str, Any]]:
    """板块负分 avg（兼容旧调用）。"""
    return await _load_board_score_avg(day_scores, positive=False)


@router.post("/score-calibration")
async def submit_score_calibration(
    request: Request,
    days: int = Query(180, ge=30, le=478, description="回测历史交易日数"),
    horizons: str = Query("1,3,5,10", description="未来 N 日收益列表，逗号分隔，如 1,3,5,10"),
    top_n: int = Query(50, ge=10, le=200, description="排名前 N 内重点标注"),
    model_id: str = Query("", description="模型 ID，按该模型的历史信号校准（缺省用全部信号）"),
):
    """提交模型分数校准任务，立即返回 task_id，后台异步计算。"""
    user_id, tenant_id = get_authenticated_identity(request)
    task_id = f"calib_{int(_now() * 1000)}_{__import__('uuid').uuid4().hex[:8]}"
    _calib_persist(task_id, {
        "task_id": task_id,
        "status": "pending",
        "progress": 0,
        "message": "任务已提交，等待调度",
        "user_id": user_id,
        "params": {"days": days, "horizons": horizons, "top_n": top_n, "model_id": model_id},
        "result": None,
        "error": None,
        "created_at": __import__("datetime").datetime.now().isoformat(),
    })
    asyncio.create_task(
        _run_score_calibration(task_id, user_id, tenant_id, days, horizons, top_n, model_id),
        name=f"score-calib-{task_id}",
    )
    return {
        "status": "submitted",
        "task_id": task_id,
        "data": {"task_id": task_id, "status": "pending", "progress": 0},
    }


@router.get("/score-calibration/{task_id}")
async def get_score_calibration_task(task_id: str, request: Request):
    """查询校准任务进度。"""
    user_id, tenant_id = get_authenticated_identity(request)
    task = _calib_tasks.get(task_id)
    if not task:
        return {"status": "not_found", "detail": "任务不存在"}
    if task.get("user_id") != user_id:
        return {"status": "error", "detail": "无权访问该任务"}
    return {
        "status": task.get("status"),
        "task_id": task_id,
        "progress": task.get("progress", 0),
        "message": task.get("message", ""),
        "result": task.get("result"),
        "error": task.get("error"),
        "meta": {
            "model_scope": "全部历史信号（当前模型版本）",
            "backtest_days": task.get("params", {}).get("days"),
            "horizons": _parse_horizons(task.get("params", {}).get("horizons", "1,3,5,10")),
            "top_n": task.get("params", {}).get("top_n"),
        },
    }


async def _run_score_calibration(
    task_id: str, user_id: str, tenant_id: str, days: int, horizons: str, top_n: int,
    model_id: str = "",
) -> None:
    """后台执行分数校准，分阶段更新进度。"""
    try:
        _calib_update(task_id, status="running")
        _calib_update(task_id, progress=5)
        _calib_update(task_id, message="读取历史信号...")

        horizon_list = _parse_horizons(horizons)

        # 1. 读取历史信号（限制条数避免全表扫描）
        #    指定 model_id 时，先查该模型的历史 run_id，只校准该模型的信号
        extra_where = ""
        params: dict[str, Any] = {"tenant_id": tenant_id, "user_id": user_id}
        if model_id:
            model_runs: list[str] = []
            async with get_session(read_only=True) as session:
                res = await session.execute(
                    text(
                        "SELECT run_id FROM qm_model_inference_runs "
                        "WHERE tenant_id = :tenant_id AND user_id = :user_id "
                        "AND model_id = :model_id AND status = 'completed' "
                        "AND run_id IS NOT NULL"
                    ),
                    {"tenant_id": tenant_id, "user_id": user_id, "model_id": model_id},
                )
                model_runs = [str(r[0]) for r in res.fetchall() if r[0]]
            if model_runs:
                # run_id 数量可能较多，用 IN 子查询按 run_id 关联信号
                extra_where = " AND run_id IN (SELECT run_id FROM qm_model_inference_runs WHERE tenant_id = :tenant_id AND user_id = :user_id AND model_id = :model_id AND status = 'completed') "
                params["model_id"] = model_id
                _calib_update(task_id, message=f"按模型 {model_id[:24]}... 过滤 {len(model_runs)} 个历史批次")

        query = text(
            f"""
            SELECT trade_date, symbol, fusion_score, score_rank
            FROM engine_signal_scores
            WHERE tenant_id = :tenant_id AND user_id = :user_id
              AND (universe_tag IS NULL OR universe_tag = 'CN')
              AND fusion_score IS NOT NULL
              {extra_where}
            ORDER BY trade_date DESC
            LIMIT 2000000
            """
        )
        rows = []
        async with get_session(read_only=True) as session:
            result = await session.execute(query, params)
            rows = result.mappings().all()
        if not rows:
            _calib_update(task_id, status="failed")
            _calib_update(task_id, error="无历史信号数据")
            return

        _calib_update(task_id, progress=15)
        _calib_update(task_id, message="分组统计信号...")

        # 按日期分组取最近 days 个交易日
        date_scores: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            d = str(r["trade_date"])
            date_scores.setdefault(d, []).append(
                {"symbol": str(r["symbol"]).upper(), "score": float(r["fusion_score"]),
                 "rank": int(r["score_rank"]) if r["score_rank"] else None}
            )
        all_dates = sorted(date_scores.keys())[-days:]
        if not all_dates:
            _calib_update(task_id, status="failed")
            _calib_update(task_id, error="无可用交易日")
            return

        # 2. 加载价格面板（线程池避免阻塞事件循环）
        _calib_update(task_id, progress=30)
        _calib_update(task_id, message="加载价格面板...")
        from pathlib import Path as _Path
        from backend.services.engine.inference.inference_backtest_service import _load_price_panel

        from backend.shared.qlib_paths import resolve_qlib_provider_uri

        data_dir = _Path(
            __import__("os").getenv("QLIB_DIR") or resolve_qlib_provider_uri("CN")
        )
        panel = await asyncio.to_thread(_load_price_panel, data_dir, all_dates)
        if panel.empty:
            _calib_update(task_id, status="failed")
            _calib_update(task_id, error="无法加载价格面板")
            return
        close_pivot = panel.pivot_table(index="symbol", columns="trade_date", values="close", aggfunc="last")
        del panel

        # 3. 市值快照
        _calib_update(task_id, progress=45)
        _calib_update(task_id, message="加载市值快照...")
        caps_snapshot = await _load_cap_snapshot()

        # 3.5 大盘状态判定：上证指数(000001.SH) 收盘价 vs MA20(20日均线)
        #   - 大盘多 = 指数收盘 >= MA20（趋势向上，顺势可做多）
        #   - 大盘空 = 指数收盘 <  MA20（趋势向下，系统性风险，防守/回避）
        # 与选股方法论"系统性风险过滤"一致：指数跌破20日均线强制空仓，避开崩盘。
        index_above_ma20: dict[str, bool] = {}
        try:
            from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub

            _hub = QuantDBDataHub()
            # 扩大指数窗口：信号最早日往前推 45 天，确保 MA20 有足够前置数据（需前19天）
            _start = date.fromisoformat(all_dates[0]) - timedelta(days=45)
            _end = date.fromisoformat(all_dates[-1]) + timedelta(days=1)
            _idx_df = _hub.fetch_index_kline("000001.SH", _start, _end)
            if _idx_df is not None and not _idx_df.empty:
                _idx_df = _idx_df.sort_values("trade_date").reset_index(drop=True)
                _idx_close = _idx_df["close"].astype(float)
                _idx_ma20 = _idx_close.rolling(20).mean()  # MA20 = 最近20个交易日收盘均值
                for _i in range(len(_idx_df)):
                    _d = str(_idx_df.loc[_i, "trade_date"])[:10]  # 转纯 YYYY-MM-DD
                    _c = float(_idx_close.iloc[_i])
                    _ma = _idx_ma20.iloc[_i]
                    if _ma == _ma and _ma > 0:  # not NaN（前 19 天无 MA20）
                        index_above_ma20[_d] = bool(_c >= _ma)  # 显式转 Python bool（_c>=_ma 可能是 numpy.bool_）
        except Exception as _exc:
            logger.warning("load index regime failed: %s", _exc)

        logger.info("index_above_ma20 loaded %d days (signals %d days)", len(index_above_ma20), len(all_dates))

        # 3.6 加载 QuantDB sector_concept 多维度归属：地区/概念/风格/行业
        #     symbol(suffix) → {regions:[], concepts:[], styles:[]}
        symbol_dims: dict[str, dict[str, list[str]]] = {}
        try:
            _hub = QuantDBDataHub()
            _sector_df = _hub.fetch_sector_members()
            if _sector_df is not None and not _sector_df.empty and "Symbol" in _sector_df.columns:
                for _row in _sector_df.itertuples(index=False):
                    _sym = str(_row.Symbol).strip().upper()
                    _name = str(getattr(_row, "SectorName", "") or "").strip()
                    _type = str(getattr(_row, "SectorType", "") or "").strip()
                    if not _sym or not _name:
                        continue
                    dims = symbol_dims.setdefault(_sym, {"regions": [], "concepts": [], "styles": []})
                    if "地区" in _type:
                        if _name not in dims["regions"]:
                            dims["regions"].append(_name)
                    elif "概念" in _type:
                        if len(dims["concepts"]) < 15:  # 限制每只股票概念数，避免过载
                            dims["concepts"].append(_name)
                    elif "风格" in _type:
                        if _name not in dims["styles"]:
                            dims["styles"].append(_name)
            logger.info("sector_concept loaded: %d stocks with dimensions", len(symbol_dims))
        except Exception as _exc:
            logger.warning("load sector_concept failed: %s", _exc)

        # 4. 逐日计算多周期收益
        _calib_update(task_id, progress=55)
        _calib_update(task_id, message="回测多周期收益...")
        records: list[dict[str, Any]] = []
        max_h = max(horizon_list)
        total_days = len(all_dates)
        for idx, d in enumerate(all_dates):
            if idx + max_h >= total_days:
                break
            day_items = date_scores[d]
            if not day_items:
                continue
            day_regime = index_above_ma20.get(d)
            sorted_items = sorted(day_items, key=lambda x: -x["score"])
            total = len(sorted_items)
            for rank_i, it in enumerate(sorted_items):
                suffix = StockCodeUtil.to_suffix(it["symbol"])
                prefix = StockCodeUtil.to_prefix(it["symbol"])
                try:
                    c0 = close_pivot.at[suffix, d]
                except KeyError:
                    continue
                if not c0 or c0 <= 0:
                    continue
                rets: dict[int, float] = {}
                for h in horizon_list:
                    f_idx = idx + h
                    if f_idx >= total_days:
                        continue
                    f_date = all_dates[f_idx]
                    try:
                        c1 = close_pivot.at[suffix, f_date]
                    except KeyError:
                        continue
                    if not c1:
                        continue
                    rets[h] = (float(c1) / float(c0) - 1.0) * 100.0
                if not rets:
                    continue
                cap = _cap_bucket(caps_snapshot.get(prefix))
                board = _board_type(suffix)
                _dims = symbol_dims.get(suffix, {})
                records.append({
                    "score": it["score"], "rets": rets, "cap": cap, "board": board,
                    "rank_pct": (rank_i + 1) / total, "rank": rank_i + 1, "total": total,
                    "regime": day_regime,  # True=大盘>MA20, False=大盘<MA20, None=无数据
                    "regions": _dims.get("regions", []),
                    "concepts": _dims.get("concepts", []),
                    "styles": _dims.get("styles", []),
                    "_day": d,
                })
            if idx % 20 == 0:
                _calib_update(task_id, progress=55 + int(40 * (idx + 1) / total_days))
                _calib_update(task_id, message=f"回测中... {idx+1}/{total_days} 天")

        if not records:
            _calib_update(task_id, status="failed")
            _calib_update(task_id, error="回测无有效样本")
            return

        _calib_update(task_id, progress=95)
        _calib_update(task_id, message="汇总统计...")

        result = await _aggregate_calibration(records, all_dates, date_scores, horizon_list, top_n)
        _calib_update(task_id, status="completed")
        _calib_update(task_id, progress=100)
        _calib_update(task_id, message="完成")
        _calib_update(task_id, result=result)
    except Exception as exc:
        logger.error("score calibration task %s failed: %s", task_id, exc, exc_info=True)
        _calib_update(task_id, status="failed")
        _calib_update(task_id, error=str(exc))


def _parse_horizons(horizons: str) -> list[int]:
    out = []
    for h in str(horizons).split(","):
        h = h.strip()
        if h.isdigit() and 1 <= int(h) <= 20:
            out.append(int(h))
    return out or [1, 3, 5, 10]


async def _compute_market_signal(
    all_dates: list[str],
    date_scores: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """大盘信号：每日全部股票信号均值/涨跌家数 → 次日上证指数红/绿概率。

    统计逻辑：
      - 每日计算全部信号均值、正分家数、负分家数、正分占比
      - 关联次日上证指数涨跌（红=收涨，绿=收跌）
      - 分档统计：信号均值>阈值时次日红盘概率 vs 全部日基线
    """
    try:
        # 1. 加载上证指数收盘（QuantDB index_daily）
        from datetime import date as _date, timedelta as _td
        from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub

        hub = QuantDBDataHub()
        end = _date.fromisoformat(all_dates[-1]) if all_dates else _date.today()
        start = end - _td(days=40)
        df = hub.fetch_index_kline("000001.SH", start, end)
        if df is None or df.empty:
            return {"status": "unavailable", "detail": "无指数数据"}
        df = df.sort_values("trade_date")
        index_close = dict(zip(df["trade_date"].astype(str), df["close"].astype(float)))
        index_dates = sorted(index_close.keys())

        # 2. 每日市场广度 + 次日指数方向
        day_breadth: list[dict[str, Any]] = []
        for idx, d in enumerate(all_dates):
            if d not in index_close:
                continue
            day_items = date_scores.get(d, [])
            if not day_items:
                continue
            scores = [x["score"] for x in day_items]
            avg = sum(scores) / len(scores)
            pos = sum(1 for s in scores if s > 0)
            neg = sum(1 for s in scores if s < 0)
            total = len(scores)
            # 次日指数涨跌（找 d 之后最近的指数交易日）
            next_idx = index_dates.index(d) + 1 if d in index_dates else -1
            if next_idx >= len(index_dates):
                continue
            next_d = index_dates[next_idx]
            if next_d not in index_close or d not in index_close:
                continue
            next_chg = (index_close[next_d] / index_close[d] - 1.0) * 100.0
            day_breadth.append({
                "date": d,
                "avg_score": round(avg, 4),
                "pos_count": pos,
                "neg_count": neg,
                "pos_ratio": round(pos / total * 100.0, 1) if total else 0,
                "next_day_index_chg": round(next_chg, 3),
                "next_day_red": 1 if next_chg > 0 else 0,
            })

        if len(day_breadth) < 10:
            return {"status": "insufficient", "detail": "样本不足"}

        # 3. 分档统计：信号均值/正分占比 阈值 → 次日红盘概率
        def _prob_at_threshold(field: str, threshold: float, ge: bool) -> dict[str, Any]:
            matched = [b for b in day_breadth if (b[field] >= threshold if ge else b[field] <= threshold)]
            if len(matched) < 5:
                return None
            red = sum(1 for b in matched if b["next_day_red"])
            return {
                "condition": f"{field}{'>=' if ge else '<='}{threshold}",
                "days": len(matched),
                "red_prob": round(red / len(matched) * 100.0, 1),
                "green_prob": round((len(matched) - red) / len(matched) * 100.0, 1),
                "avg_next_chg": round(sum(b["next_day_index_chg"] for b in matched) / len(matched), 3),
            }

        avg_thresholds = [0.05, 0.0, -0.05, -0.10]
        pos_ratio_thresholds = [50.0, 40.0, 60.0]
        signal_table = []
        for t in avg_thresholds:
            row = _prob_at_threshold("avg_score", t, True)
            if row: signal_table.append(row)
        for t in pos_ratio_thresholds:
            row = _prob_at_threshold("pos_ratio", t, True)
            if row: signal_table.append(row)

        # 基线（全部日）
        all_red = sum(1 for b in day_breadth if b["next_day_red"])
        baseline = {
            "days": len(day_breadth),
            "red_prob": round(all_red / len(day_breadth) * 100.0, 1),
            "avg_next_chg": round(sum(b["next_day_index_chg"] for b in day_breadth) / len(day_breadth), 3),
        }

        return {
            "status": "success",
            "baseline": baseline,
            "signal_table": signal_table,
            "recent_days": day_breadth[-15:],
            "index_symbol": "000001.SH",
        }
    except Exception as exc:
        logger.warning("compute market signal failed: %s", exc)
        return {"status": "error", "detail": str(exc)}


def _make_equal_bins(
    pairs: list[tuple[float, float]],
    n_bins: int = 20,
) -> list[dict[str, Any]]:
    """等宽分桶（覆盖全分数范围 min~max）。

    分位数分桶在分数偏斜时只覆盖密集区间，高分/低分尾部被合并。
    等宽分桶保证 ±3（融合模型）或 ±0.3（普通模型）全范围覆盖，
    每桶边界均匀，用户能看到完整分数区间下的收益/胜率分布。
    """
    if not pairs:
        return []
    scores = [p[0] for p in pairs]
    smin = min(scores)
    smax = max(scores)
    span = smax - smin
    if span <= 1e-12:
        return [{"score_min": smin, "score_max": smax, "pairs": pairs, "n": len(pairs)}]
    bins = []
    for i in range(n_bins):
        lo = smin + span * i / n_bins
        hi = smin + span * (i + 1) / n_bins
        if i == n_bins - 1:
            seg = [p for p in pairs if p[0] >= lo]
        else:
            seg = [p for p in pairs if lo <= p[0] < hi]
        if seg:
            bins.append({
                "score_min": round(lo, 4),
                "score_max": round(hi, 4),
                "pairs": seg,
                "n": len(seg),
            })
    return bins


def _compute_winrate_zones(
    records: list[dict[str, Any]],
    horizon_list: list[int],
) -> dict[str, Any]:
    """按胜率/收益反推最优分数区间（相对最优，适配任意模型）。

    核心思路（用户要求）：先统计每个分数附近的胜率/收益，
    再从统计反推「胜率最高的分数段」（做多）和「下跌概率最高的分数段」（做空）。

    方法：
      1. 对每个 horizon，把样本按分数分成 N 个细档（分位数）
      2. 计算每档的胜率/下跌概率/均收
      3. 找「胜率最高」且样本足够的连续分数段 = 最优做多区间
      4. 找「下跌概率最高」的连续分数段 = 最优做空区间
      5. 同时输出胜率≥80%/90%的段（若存在）
    """
    if not records or not horizon_list:
        return {"status": "empty", "detail": "无样本"}

    results: list[dict[str, Any]] = []
    for h in horizon_list:
        pairs = []
        for r in records:
            ret = r["rets"].get(h)
            if ret is not None:
                pairs.append((r["score"], ret))
        if len(pairs) < 500:
            continue
        pairs.sort(key=lambda x: x[0])
        n = len(pairs)

        # 等宽分桶（覆盖全分数范围，高分尾部不合并）
        raw_bins = _make_equal_bins(pairs, n_bins=20)
        bins = []
        for b in raw_bins:
            seg_rets = [p[1] for p in b["pairs"]]
            wins = sum(1 for x in seg_rets if x > 0)
            downs = sum(1 for x in seg_rets if x < 0)
            bins.append({
                "score_min": b["score_min"],
                "score_max": b["score_max"],
                "n": len(seg_rets),
                "win_rate": wins / len(seg_rets) if seg_rets else 0,
                "down_prob": downs / len(seg_rets) if seg_rets else 0,
                "avg_ret": sum(seg_rets) / len(seg_rets) if seg_rets else 0,
                "horizon": h,
            })

        if len(bins) < 5:
            continue

        # 相对最优：胜率最高的前 3 档合并为做多区间
        by_win = sorted(bins, key=lambda b: -b["win_rate"])
        best3 = by_win[:3]
        if best3:
            best_min = min(b["score_min"] for b in best3)
            best_max = max(b["score_max"] for b in best3)
            seg = [p for p in pairs if best_min <= p[0] <= best_max]
            seg_rets = [p[1] for p in seg]
            results.append({
                "type": "long",
                "horizon": h,
                "score_min": round(best_min, 4),
                "score_max": round(best_max, 4),
                "n": len(seg),
                "win_rate": round(sum(1 for x in seg_rets if x > 0) / len(seg) * 100.0, 1) if seg else 0,
                "down_prob": round(sum(1 for x in seg_rets if x < 0) / len(seg) * 100.0, 1) if seg else 0,
                "avg_ret": round(sum(seg_rets) / len(seg), 3) if seg else 0,
                "label": "胜率最高(做多)",
            })

        # 相对最差：下跌概率最高的前 3 档合并为做空区间
        by_down = sorted(bins, key=lambda b: -b["down_prob"])
        worst3 = by_down[:3]
        if worst3:
            worst_min = min(b["score_min"] for b in worst3)
            worst_max = max(b["score_max"] for b in worst3)
            seg = [p for p in pairs if worst_min <= p[0] <= worst_max]
            seg_rets = [p[1] for p in seg]
            results.append({
                "type": "short",
                "horizon": h,
                "score_min": round(worst_min, 4),
                "score_max": round(worst_max, 4),
                "n": len(seg),
                "win_rate": round(sum(1 for x in seg_rets if x > 0) / len(seg) * 100.0, 1) if seg else 0,
                "down_prob": round(sum(1 for x in seg_rets if x < 0) / len(seg) * 100.0, 1) if seg else 0,
                "avg_ret": round(sum(seg_rets) / len(seg), 3) if seg else 0,
                "label": "下跌概率最高(做空)",
            })

    if not results:
        return {"status": "none", "detail": "无有效样本"}

    results.sort(key=lambda x: (-x["win_rate"]) if x["type"] == "long" else x["down_prob"])
    return {
        "status": "success",
        "note": "按胜率反推最优分数区间：先统计每档胜率/下跌概率，再合并出做多最优段与做空最优段",
        "zones": results[:20],
    }


def _compute_condition_zones(
    records: list[dict[str, Any]],
    horizon_list: list[int],
) -> dict[str, Any]:
    """多条件组合最优区间：大盘状态 × 市值 × 板块 × 分数 → 最优做多/做空段。

    用户需求：结合行情板块筛选，找出「达到某分数就涨/跌」的区间。
    条件维度：
      - 大盘：上证>MA20（多）/ <MA20（空）
      - 市值：微盘/小盘/中盘/大盘/超大盘
      - 板块：沪深主板/创业板/科创板/北交所
    对每个条件组合，把样本按分数分桶，统计胜率/均收，
    输出每个条件下「胜率最高分数段（买入区间）」和「下跌概率最高分数段（卖出/回避区间）」。
    """
    if not records:
        return {"status": "empty", "detail": "无样本"}

    # 条件组合：只用样本量足够的组合
    # 收集所有可能的组合键
    combos: dict[tuple, list[dict[str, Any]]] = {}
    for r in records:
        regime = "大盘多" if r.get("regime") is True else ("大盘空" if r.get("regime") is False else "大盘未知")
        cap = r.get("cap") or "未知"
        board = r.get("board") or "其他"
        key = (regime, cap, board)
        combos.setdefault(key, []).append(r)

    results = []
    for h in horizon_list:
        for (regime, cap, board), items in combos.items():
            if len(items) < 2000:
                continue
            # 该组合下按分数等宽分桶（覆盖全范围）
            items_sorted = sorted(items, key=lambda x: x["score"])
            raw_pairs = [(x["score"], x["rets"].get(h)) for x in items_sorted]
            raw_pairs = [(s, r) for s, r in raw_pairs if r is not None]
            if len(raw_pairs) < 150:
                continue
            raw_bins = _make_equal_bins(raw_pairs, n_bins=8)
            bins = []
            for b in raw_bins:
                rets_h = [p[1] for p in b["pairs"]]
                if len(rets_h) < 100:
                    continue
                wins = sum(1 for x in rets_h if x > 0)
                downs = sum(1 for x in rets_h if x < 0)
                bins.append({
                    "score_min": b["score_min"],
                    "score_max": b["score_max"],
                    "n": len(rets_h),
                    "win_rate": wins / len(rets_h),
                    "down_prob": downs / len(rets_h),
                    "avg_ret": sum(rets_h) / len(rets_h),
                })
            if len(bins) < 3:
                continue

            # 胜率最高段（买入区间）：输出 Top2，避免只看最佳一段
            best_bins = sorted(bins, key=lambda b: -b["win_rate"])[:2]
            for best in best_bins:
                results.append({
                    "type": "buy",
                    "horizon": h,
                    "regime": regime,
                    "cap": cap,
                    "board": board,
                    "score_min": round(best["score_min"], 4),
                    "score_max": round(best["score_max"], 4),
                    "n": best["n"],
                    "win_rate": round(best["win_rate"] * 100.0, 1),
                    "down_prob": round(best["down_prob"] * 100.0, 1),
                    "avg_ret": round(best["avg_ret"], 3),
                    "label": "买入区间",
                })
            # 下跌概率最高段（卖出/回避区间）：输出 Top2
            worst_bins = sorted(bins, key=lambda b: -b["down_prob"])[:2]
            for worst in worst_bins:
                results.append({
                    "type": "sell",
                    "horizon": h,
                    "regime": regime,
                    "cap": cap,
                    "board": board,
                    "score_min": round(worst["score_min"], 4),
                    "score_max": round(worst["score_max"], 4),
                    "n": worst["n"],
                    "win_rate": round(worst["win_rate"] * 100.0, 1),
                    "down_prob": round(worst["down_prob"] * 100.0, 1),
                    "avg_ret": round(worst["avg_ret"], 3),
                    "label": "卖出/回避区间",
                })

    if not results:
        return {"status": "none", "detail": "无有效样本"}

    # 排序：买入按胜率降序，卖出按下跌概率降序
    buy = sorted([r for r in results if r["type"] == "buy"], key=lambda x: -x["win_rate"])
    sell = sorted([r for r in results if r["type"] == "sell"], key=lambda x: -x["down_prob"])
    return {
        "status": "success",
        "note": "多条件组合：大盘状态×市值×板块，找出胜率最高的买入分数段与下跌概率最高的卖出/回避分数段",
        "metric_note": "大盘状态 = 上证指数(000001.SH)收盘价 vs 20日均线(MA20)：指数>=MA20为「大盘多」(趋势向上)，<MA20为「大盘空」(趋势向下/系统性风险)",
        "buy_zones": buy[:20],
        "sell_zones": sell[:20],
    }


def _compute_dimension_zones(
    records: list[dict[str, Any]],
    horizon_list: list[int],
) -> dict[str, Any]:
    """多维度分数校准：地区/概念/风格 × 分数 → 涨跌概率。

    利用 QuantDB sector_concept：每只股票归属地区板块/概念板块/风格板块。
    对每个地区/概念/风格，统计该维度下不同分数段的胜率/下跌概率/均收，
    输出「胜率最高的分数段（买入）」和「下跌概率最高的分数段（回避）」。
    """
    if not records:
        return {"status": "empty", "detail": "无样本"}

    main_h = horizon_list[0] if horizon_list else 5
    # 记录里每个 symbol 的维度（去重后收集）
    # 先收集所有维度下足够样本的组合
    dim_items: dict[str, dict[str, list[dict[str, Any]]]] = {
        "region": {}, "concept": {}, "style": {},
    }
    for r in records:
        for dim_key, field in (("region", "regions"), ("concept", "concepts"), ("style", "styles")):
            for name in (r.get(field) or []):
                if not name:
                    continue
                # 只统计样本多的热门维度
                dim_items[dim_key].setdefault(name, []).append(r)

    def _dim_zones(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if len(items) < 3000:
            return []
        items_sorted = sorted(items, key=lambda x: x["score"])
        n = len(items_sorted)
        # 分 8 档
        bin_size = max(200, n // 8)
        bins = []
        for i in range(0, n, bin_size):
            seg = items_sorted[i:i + bin_size]
            if len(seg) < 150:
                continue
            rets_h = [x["rets"].get(main_h) for x in seg]
            rets_h = [x for x in rets_h if x is not None]
            if len(rets_h) < 100:
                continue
            wins = sum(1 for x in rets_h if x > 0)
            downs = sum(1 for x in rets_h if x < 0)
            bins.append({
                "score_min": seg[0]["score"],
                "score_max": seg[-1]["score"],
                "n": len(rets_h),
                "win_rate": wins / len(rets_h),
                "down_prob": downs / len(rets_h),
                "avg_ret": sum(rets_h) / len(rets_h),
            })
        if len(bins) < 3:
            return []
        best = max(bins, key=lambda b: b["win_rate"])
        worst = max(bins, key=lambda b: b["down_prob"])
        return [{
            "buy": {
                "score_min": round(best["score_min"], 3),
                "score_max": round(best["score_max"], 3),
                "n": best["n"],
                "win_rate": round(best["win_rate"] * 100.0, 1),
                "down_prob": round(best["down_prob"] * 100.0, 1),
                "avg_ret": round(best["avg_ret"], 3),
            },
            "sell": {
                "score_min": round(worst["score_min"], 3),
                "score_max": round(worst["score_max"], 3),
                "n": worst["n"],
                "win_rate": round(worst["win_rate"] * 100.0, 1),
                "down_prob": round(worst["down_prob"] * 100.0, 1),
                "avg_ret": round(worst["avg_ret"], 3),
            },
        }]

    result: dict[str, list[dict[str, Any]]] = {"region": [], "concept": [], "style": []}
    for dim_key, items_map in dim_items.items():
        for name, items in items_map.items():
            zones = _dim_zones(items)
            if zones:
                result[dim_key].append({"name": name, "total_n": len(items), **zones[0]})

    # 按买入胜率排序
    for k in result:
        result[k].sort(key=lambda x: -x["buy"]["win_rate"])

    # 只保留热门维度
    return {
        "status": "success",
        "region": result["region"][:15],
        "concept": result["concept"][:20],
        "style": result["style"][:10],
        "note": "多维度×分数：QuantDB sector_concept 地区/概念/风格板块，统计各维度下不同分数段的涨跌概率（主周期 T+N）",
    }


async def _aggregate_calibration(
    records: list[dict[str, Any]],
    all_dates: list[str],
    date_scores: dict[str, list[dict[str, Any]]],
    horizon_list: list[int],
    top_n: int,
) -> dict[str, Any]:
    """从 records 聚合分数档矩阵与汇总。"""
    import collections

    # 等宽分档：覆盖全分数范围（min~max），高分/低分尾部也细分
    # 等分位数分档在分数偏斜时只覆盖密集区间，最高分档可能只到 0.867
    # 而模型实际有 ±3，尾部全部并成一档。等宽保证 ±3 都显示。
    all_scores_sorted = sorted(r["score"] for r in records)
    score_min = min(all_scores_sorted)
    score_max = max(all_scores_sorted)
    n_bands = 12
    score_span = score_max - score_min
    if score_span <= 1e-9:
        band_edges = [score_min, score_max]
    else:
        # 等宽边界（含首尾）
        band_edges = [score_min + score_span * i / n_bands for i in range(n_bands + 1)]
    n_real = len(band_edges) - 1
    band_labels = [f"{band_edges[i]:.3f}~{band_edges[i+1]:.3f}" for i in range(n_real)]
    band_labels[-1] = f"≥{band_edges[-2]:.3f}"

    def _band_idx(score: float) -> int:
        for i in range(n_real):
            if score <= band_edges[i + 1]:
                return i
        return n_real - 1

    def _band(score: float) -> str:
        return band_labels[_band_idx(score)]

    # 分数档×市值档×horizon
    band_cap_h: dict[tuple[str, str, int], list[float]] = collections.defaultdict(list)
    band_h: dict[str, dict[int, list[float]]] = collections.defaultdict(lambda: collections.defaultdict(list))
    band_ranks: dict[str, list[int]] = collections.defaultdict(list)
    band_rank_pcts: dict[str, list[float]] = collections.defaultdict(list)
    for r in records:
        b = _band(r["score"])
        for h, ret in r["rets"].items():
            band_cap_h[(b, r["cap"], h)].append(ret)
            band_h[b][h].append(ret)
        band_ranks[b].append(r["rank"])
        band_rank_pcts[b].append(r["rank_pct"])

    main_h = horizon_list[0] if horizon_list else 5
    cap_order = ["微盘", "小盘", "中盘", "大盘", "超大盘"]
    matrix = []
    for band_label in band_labels:
        caps = []
        for cap in cap_order:
            rets = band_cap_h.get((band_label, cap, main_h), [])
            if rets:
                caps.append({
                    "cap": cap, "n": len(rets),
                    "down_prob": round(sum(1 for x in rets if x < 0) / len(rets) * 100.0, 1),
                    "avg_ret": round(sum(rets)/len(rets), 3),
                })
            else:
                caps.append({"cap": cap, "n": 0, "down_prob": None, "avg_ret": None})
        matrix.append({"score_band": band_label, "caps": caps})

    score_summary = []
    for band_label in band_labels:
        h_dict = band_h.get(band_label)
        if not h_dict:
            continue
        ranks = band_ranks[band_label]
        rank_pcts = band_rank_pcts[band_label]
        horizons_stats = []
        for h in sorted(h_dict.keys()):
            rets = h_dict[h]
            if not rets:
                continue
            sorted_rets = sorted(rets)
            horizons_stats.append({
                "horizon": h, "n": len(rets),
                "win_rate": round(sum(1 for x in rets if x > 0) / len(rets) * 100.0, 1),
                "down_prob": round(sum(1 for x in rets if x < 0) / len(rets) * 100.0, 1),
                "avg_ret": round(sum(rets) / len(rets), 3),
                "median_ret": round(sorted_rets[len(sorted_rets)//2], 3),
            })
        main_rets = band_h[band_label].get(main_h, [])
        score_summary.append({
            "score_band": band_label,
            "n": len(ranks),
            "top50_count": sum(1 for rk in ranks if rk <= 50),
            "avg_rank": round(sum(ranks) / len(ranks), 1),
            "avg_rank_pct": round(sum(rank_pcts) / len(rank_pcts), 3),
            "main_horizon_avg_ret": round(sum(main_rets) / len(main_rets), 3) if main_rets else None,
            "horizons": horizons_stats,
        })

    # 标注档位性质：最优/最差/观察/最热
    # 依据主周期均收：最高=最优，最低=最差；样本最多=最热；其余=观察
    if score_summary:
        by_avg = [s for s in score_summary if s.get("main_horizon_avg_ret") is not None]
        if by_avg:
            best = max(by_avg, key=lambda s: s["main_horizon_avg_ret"])["score_band"]
            worst = min(by_avg, key=lambda s: s["main_horizon_avg_ret"])["score_band"]
            hottest = max(score_summary, key=lambda s: s["n"])["score_band"]
            for s in score_summary:
                nature = "观察"
                if s["score_band"] == best:
                    nature = "最优"
                elif s["score_band"] == worst:
                    nature = "最差"
                elif s["score_band"] == hottest:
                    nature = "最热"
                s["nature"] = nature

    # 负分行业/板块 avg（最新日）
    latest_date = all_dates[-1]
    latest_items = date_scores[latest_date]
    latest_df = pd.DataFrame(
        [{"symbol": StockCodeUtil.to_suffix(x["symbol"]), "score": x["score"]}
         for x in latest_items]
    )
    industry_map = load_shenwan_industry_map()
    neg_industry_avg = await _load_industry_negative_avg(latest_items, latest_df, industry_map)
    neg_board_avg = await _load_board_negative_avg(latest_df)
    # 正分行业/板块 avg（新增：最强行业/板块排名）
    pos_industry_avg = await _load_industry_score_avg(latest_df, industry_map, positive=True)
    pos_board_avg = await _load_board_score_avg(latest_df, positive=True)

    # 推荐分数区间
    recommended = None
    for s in score_summary:
        avg = s.get("main_horizon_avg_ret")
        if s["n"] >= 50 and avg is not None and avg > 0:
            if recommended is None or avg > recommended.get("main_horizon_avg_ret", -999):
                recommended = s

    # 大盘信号：全市场信号均值 → 次日指数红/绿概率
    market_signal = await _compute_market_signal(all_dates, date_scores)

    # 胜率聚类：按分数排序，滑动找出高胜率分数区间
    # 核心思路：先统计每个分数附近的胜率，再反推"胜率≥阈值"的分数段
    winrate_zones = _compute_winrate_zones(records, horizon_list)
    # 多条件组合最优区间：大盘状态 × 市值 × 板块
    condition_zones = _compute_condition_zones(records, horizon_list)
    # 多维度分数校准：地区/概念/风格 × 分数 → 涨跌概率
    dimension_zones = _compute_dimension_zones(records, horizon_list)

    # ── 方向自检 + 超额收益 ──
    # 1. 每档超额收益：相对当日全市场均收的 excess_ret（主周期）
    #    弱市里所有档绝对收益可能为负，超额收益为正说明该档跑赢大盘
    try:
        # 每日全市场均收（主周期）
        main_ret_daily: dict[str, list[float]] = {}
        for r in records:
            ret = r["rets"].get(main_h)
            if ret is not None:
                main_ret_daily.setdefault(r["score"], []).append(ret)  # placeholder, unused
        # 直接算：每档 avg_ret - 全市场当日 avg_ret
        day_avg: dict[str, float] = {}
        for r in records:
            ret = r["rets"].get(main_h)
            if ret is not None:
                day_avg.setdefault(r.get("_day", ""), []).append(ret)  # noqa
        # 简化：用全样本主周期均收作市场基准
        all_main_rets = [r["rets"][main_h] for r in records if main_h in r["rets"]]
        market_avg_ret = round(sum(all_main_rets) / len(all_main_rets), 4) if all_main_rets else 0.0
        for s in score_summary:
            avg = s.get("main_horizon_avg_ret")
            if avg is not None:
                s["excess_ret"] = round(float(avg) - market_avg_ret, 4)
            else:
                s["excess_ret"] = None
    except Exception as _exc:  # noqa: BLE001
        logger.warning("excess_ret calc failed: %s", _exc)
        for s in score_summary:
            s.setdefault("excess_ret", None)

    # 2. RankIC：按日算分数排名 vs 主周期收益排名的 Spearman 相关
    rank_ic = None
    rank_ic_positive_days = 0
    rank_ic_total_days = 0
    direction_ok = None
    try:
        import numpy as _np

        _ic_vals = []
        for r in records:
            _day = r.get("_day") or r.get("date") or ""
            _ic_vals.append((_day, r["score"], r["rets"].get(main_h)))
        # 按日分组
        _day_groups: dict[str, list[tuple[float, float]]] = {}
        for _d, _sc, _rt in _ic_vals:
            if _rt is None:
                continue
            _day_groups.setdefault(_d, []).append((_sc, _rt))
        _ic_list = []
        for _d, _pairs in _day_groups.items():
            if len(_pairs) < 20:
                continue
            _sc_sorted = sorted(set(p[0] for p in _pairs))
            _rt_sorted = sorted(set(p[1] for p in _pairs))
            _rank_map_s = {v: i + 1 for i, v in enumerate(_sc_sorted)}
            _rank_map_r = {v: i + 1 for i, v in enumerate(_rt_sorted)}
            _s_ranks = [_rank_map_s[p[0]] for p in _pairs]
            _r_ranks = [_rank_map_r[p[1]] for p in _pairs]
            _n = len(_pairs)
            _d_s = [x - sum(_s_ranks) / _n for x in _s_ranks]
            _d_r = [x - sum(_r_ranks) / _n for x in _r_ranks]
            _ss = sum(a * b for a, b in zip(_d_s, _d_s))
            _rr = sum(a * b for a, b in zip(_d_r, _d_r))
            _sr = sum(a * b for a, b in zip(_d_s, _d_r))
            if _ss > 1e-12 and _rr > 1e-12:
                _ic = _sr / (_ss * _rr) ** 0.5
                _ic_list.append(_ic)
                if _ic > 0:
                    rank_ic_positive_days += 1
                rank_ic_total_days += 1
        if _ic_list:
            rank_ic = round(sum(_ic_list) / len(_ic_list), 4)
            direction_ok = rank_ic > 0
    except Exception as _exc:  # noqa: BLE001
        logger.warning("rank_ic calc failed: %s", _exc)

    # 3. 市场状态：来自 market_signal 的基线（全市场均涨跌）
    market_state = "unknown"
    try:
        _base = (market_signal or {}).get("baseline") or {}
        _base_avg = _base.get("avg_chg") if isinstance(_base, dict) else None
        if _base_avg is not None:
            market_state = "牛" if _base_avg > 0 else "熊"
    except Exception:  # noqa: BLE001
        market_state = "unknown"

    return {
        "matrix": matrix,
        "score_summary": score_summary,
        "neg_industry_avg": neg_industry_avg[:10],
        "neg_board_avg": neg_board_avg,
        "pos_industry_avg": pos_industry_avg[:10],
        "pos_board_avg": pos_board_avg,
        "market_signal": market_signal,
        "winrate_zones": winrate_zones,
        "condition_zones": condition_zones,
        "dimension_zones": dimension_zones,
        "recommended_band": recommended,
        "total_samples": len(records),
        "latest_trade_date": latest_date,
        "score_range": {
            "min": round(score_min, 4),
            "max": round(score_max, 4),
            "band_count": n_real,
        },
        "direction_check": {
            "rank_ic": rank_ic,
            "direction_ok": direction_ok,
            "positive_days": rank_ic_positive_days,
            "total_days": rank_ic_total_days,
            "market_state": market_state,
            "market_avg_ret": market_avg_ret,
        },
    }


# 校准任务存储：内存 + JSON 文件持久化（重启后历史仍可查看）
_calib_tasks: dict[str, dict[str, Any]] = {}

def _calib_store_path() -> Path:
    import os as _os
    base = _os.getenv("QM_DATA_DIR", str(Path(_os.getcwd()) / "data"))
    p = Path(base) / "score_calibration_tasks.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _calib_load_all() -> None:
    """从 JSON 文件加载已持久化的任务。"""
    global _calib_tasks
    try:
        p = _calib_store_path()
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                _calib_tasks.update(data)
    except Exception as exc:
        logger.warning("load calib tasks failed: %s", exc)


def _calib_persist(task_id: str, task: dict[str, Any]) -> None:
    """把单条任务持久化到 JSON 文件（内存 + 磁盘）。"""
    _calib_tasks[task_id] = task
    try:
        p = _calib_store_path()
        p.write_text(
            json.dumps(_calib_tasks, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.warning("persist calib task %s failed: %s", task_id, exc)



def _calib_update(task_id: str, **fields: Any) -> dict[str, Any]:
    """更新任务状态并持久化。返回更新后的任务 dict。"""
    task = _calib_tasks.get(task_id) or {}
    task.update(fields)
    _calib_persist(task_id, task)
    return task


# 启动时加载历史

_calib_load_all()


@router.get("/score-calibration-history")
async def list_score_calibration_history(
    request: Request,
    limit: int = Query(20, ge=1, le=100),
    model_id: str = Query("", description="模型 ID，只返回该模型的校准历史"),
):
    """校准历史：列出最近的校准任务（内存存储，重启后清空）。

    支持按 model_id 过滤 —— 只显示当前模型的校准历史，不同模型历史隔离。
    """
    user_id, tenant_id = get_authenticated_identity(request)
    tasks = []
    for tid, t in _calib_tasks.items():
        if t.get("user_id") != user_id:
            continue
        # 按模型过滤：指定 model_id 时只返回该模型的校准任务
        task_model = (t.get("params") or {}).get("model_id") or ""
        if model_id and task_model != model_id:
            continue
        tasks.append({
            "task_id": tid,
            "status": t.get("status"),
            "progress": t.get("progress"),
            "message": t.get("message"),
            "params": t.get("params", {}),
            "created_at": t.get("created_at"),
            "total_samples": (t.get("result") or {}).get("total_samples"),
            "recommended_band": ((t.get("result") or {}).get("recommended_band") or {}).get("score_band"),
            "latest_trade_date": (t.get("result") or {}).get("latest_trade_date"),
        })
    # 最新在前
    tasks.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    return {"status": "success", "items": tasks[:limit], "total": len(tasks)}
