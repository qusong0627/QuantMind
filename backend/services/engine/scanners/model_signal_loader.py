"""模型信号快照装载（T-P4-01）：DB → ModelSignalSnapshot（IO 与纯 scan 分离）。

装载口径与 /selection 端点完全一致（等价性前提）：信号查询含 universe_tag 门、
symbol 归一后缀式、按 symbol 去重 keep last、剔除非有限分数；价格/ST 标记只对
分数区间内候选查询（避免全市场批量查询）；上证 MA20 过滤复用同源实现。
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd
from sqlalchemy import text

from backend.shared.database_manager_v2 import get_session
from backend.shared.stock_utils import StockCodeUtil
from backend.services.engine.inference.inference_backtest_service import (
    StrategyConfig,
    _is_main_board,
    _is_star_market,
)
from backend.services.engine.inference.shenwan_industry import (
    load_shenwan_industry_map,
)
from backend.services.engine.scanners.model_signal_scanner import ModelSignalSnapshot


async def _resolve_latest_signal_identity(tenant_id: str) -> str | None:
    """自动取"最新写入身份"（max trade_date，并列取行数最多）——不硬编码键形
    （双键形教训：日更链路写 00000001，历史一次性写入 system，硬编码任一都会读到旧数据）。"""
    async with get_session(read_only=True) as session:
        row = (
            await session.execute(
                text(
                    "SELECT user_id FROM engine_signal_scores "
                    "WHERE tenant_id = :t AND (universe_tag IS NULL OR universe_tag = 'CN') "
                    "GROUP BY user_id "
                    "ORDER BY MAX(trade_date) DESC, COUNT(*) DESC LIMIT 1"
                ),
                {"t": tenant_id},
            )
        ).first()
    return str(row[0]) if row and row[0] is not None else None


async def load_model_signal_snapshot(
    trade_date: str | None = None,
    *,
    tenant_id: str = "default",
    user_id: str | None = None,
    config: StrategyConfig | None = None,
    with_price_flags: bool = True,
) -> ModelSignalSnapshot | None:
    """装载单日模型信号快照；无信号返回 None。

    ``user_id=None``：自动取最新写入身份（见 _resolve_latest_signal_identity）。
    """
    cfg = config or StrategyConfig()
    if not user_id:
        user_id = await _resolve_latest_signal_identity(tenant_id)
        if not user_id:
            return None
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
        SELECT s.symbol, s.fusion_score, s.rank_pct, s.trade_date
        FROM engine_signal_scores s
        WHERE s.tenant_id = :tenant_id AND s.user_id = :user_id
          AND (s.universe_tag IS NULL OR s.universe_tag = 'CN') AND {where}
        ORDER BY s.fusion_score DESC
        """
    )
    async with get_session(read_only=True) as session:
        rows = (await session.execute(query, params)).mappings().all()
    if not rows:
        return None

    resolved_date = str(rows[0]["trade_date"] or trade_date or "")
    records: list[dict[str, Any]] = []
    rank_pct_by_symbol: dict[str, float] = {}
    for r in rows:
        if r["fusion_score"] is None:
            continue
        suffix = StockCodeUtil.to_suffix(str(r["symbol"]).upper())
        records.append({"symbol": suffix, "score": float(r["fusion_score"])})
        if r["rank_pct"] is not None:
            rank_pct_by_symbol[suffix] = float(r["rank_pct"])

    day_scores = pd.DataFrame(records)
    if day_scores.empty:
        return None
    day_scores = day_scores.drop_duplicates(subset="symbol", keep="last")
    numeric = pd.to_numeric(day_scores["score"], errors="coerce")
    day_scores = day_scores[numeric.notna() & numeric.abs().ne(float("inf"))].copy()

    industry_map = load_shenwan_industry_map()

    price_day: pd.DataFrame | None = None
    if with_price_flags:
        # 与 selection 端点同口径：只对分数区间内候选查价格/ST 标记
        mask = (day_scores["score"] >= cfg.score_min) & (
            day_scores["score"] <= cfg.score_max
        )
        if cfg.main_board_only:
            mask &= day_scores["symbol"].apply(_is_main_board)
        mask &= ~day_scores["symbol"].apply(_is_star_market)
        candidates = day_scores.loc[mask, "symbol"].tolist()
        from backend.services.engine.routers.selection import _load_price_flags

        flags = await _load_price_flags(resolved_date, candidates)
        price_rows = [
            {
                "symbol": sym,
                "pct_change": fl.get("pct_change"),
                "is_st": fl.get("is_st"),
            }
            for sym, fl in flags.items()
        ]
        price_day = pd.DataFrame(price_rows) if price_rows else pd.DataFrame()

    index_ok: bool | None = None
    try:
        from backend.services.engine.routers.selection import _load_index_above_ma20

        index_ok, _ = await _load_index_above_ma20(resolved_date or None)
    except Exception:  # noqa: BLE001 - 指数过滤属证据位，失败不阻断扫描
        index_ok = None

    return ModelSignalSnapshot(
        trade_date=resolved_date,
        day_scores=day_scores,
        industry_map=industry_map,
        price_day=price_day,
        history_scores=None,  # 端点路径同口径：不带 3 天趋势（该过滤在回测路径启用）
        rank_pct_by_symbol=rank_pct_by_symbol,
        index_ma20_ok=index_ok,
    )
