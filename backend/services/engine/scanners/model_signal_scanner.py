"""模型信号扫描器（T-P4-01）：推理信号 → 机会列表 —— 纯函数。

**移入不改写**：选股核心直接调用 `inference_backtest_service._select_stocks_daily`
（单一实现原则——等价性由构造保证，并由 test_model_signal_scanner 夹具+真库双侧锁定）。
扫描器只发现不决策：输出全部合格机会 + 批级行业/市场状态 meta，买不买由策略层决定。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from backend.shared.scanner_spi import DEFAULT_HORIZON, Opportunity
from backend.services.engine.inference.inference_backtest_service import (
    StrategyConfig,
    _compute_industry_signals,
    _market_state,
    _select_stocks_daily,
)


@dataclass
class ModelSignalSnapshot:
    """单日模型信号快照（纯 scan 的唯一输入；IO 见 model_signal_loader）。"""

    trade_date: str
    day_scores: pd.DataFrame  # columns: symbol（后缀式）、score（已去重、已剔非有限值）
    industry_map: dict[str, str]  # 后缀式 symbol → 申万行业
    price_day: pd.DataFrame | None = (
        None  # columns: symbol/pct_change/is_st（ST/涨跌停过滤）
    )
    history_scores: dict[str, dict[str, float] | None] | None = (
        None  # 3 天趋势过滤（可选）
    )
    rank_pct_by_symbol: dict[str, float] = field(
        default_factory=dict
    )  # 后缀式 → rank 分位
    index_ma20_ok: bool | None = None  # 上证 MA20 过滤（可选，入口门禁证据）


def scan_model_signals(
    snapshot: ModelSignalSnapshot,
    config: StrategyConfig | None = None,
    *,
    ts: str | None = None,
    mode: str = "absolute",
    profile: Any = None,
) -> tuple[list[Opportunity], dict[str, Any]]:
    """快照 → (机会列表, 批级 meta)。

    mode="absolute"（默认，存量等价基线）：绝对分数阈值（[0.10,0.12] 带、0.10 强行业）；
    mode="quantile"（T-P4-03，生产默认由 runner 指定）：阈值由当日分数分布推得
    （shared/signal_thresholds）——**尺度等变**，量纲错位不可能再归零。
    strength = rank_pct 分位（T-P1-01 口径；缺失记 0），score = round(strength×100)；
    evidence 携带 fusion_score/industry/trend 与批级证据索引（市场状态/行业 Top1）。
    """
    from dataclasses import replace as _replace

    from backend.shared.signal_thresholds import (
        DEFAULT_PROFILE,
        resolve_thresholds,
        thresholds_to_dict,
    )

    cfg = config or StrategyConfig()
    thresholds = None
    strong_threshold = 0.10  # 存量等价默认
    if str(mode) == "quantile":
        scores = (
            list(snapshot.day_scores["score"]) if not snapshot.day_scores.empty else []
        )
        thresholds = resolve_thresholds(scores, profile or DEFAULT_PROFILE)
        if thresholds is None:
            return [], {
                "trade_date": snapshot.trade_date,
                "scanner": "model_signal",
                "mode": "quantile",
                "thresholds": None,
                "picked": 0,
                "note": "无有效分数（分位阈值不可解析）",
            }
        cfg = _replace(
            cfg,
            score_min=thresholds.score_min,
            score_max=thresholds.score_max,
            entry_threshold=thresholds.entry_avg_top1,
            exit_threshold=thresholds.exit_avg_top1,
            strong_industry_min=thresholds.strong_industry_min,
        )
        strong_threshold = thresholds.strong_top1

    picks = _select_stocks_daily(
        snapshot.day_scores,
        snapshot.industry_map,
        cfg,
        snapshot.price_day,
        snapshot.history_scores,
    )
    ind_top1, _ind_count, avg_top1, strong_count = _compute_industry_signals(
        snapshot.day_scores, snapshot.industry_map, strong_threshold=strong_threshold
    )
    # 市场状态口径与阈值口径一致（T-P4-02 三方归一：分位模式用分位状态，
    # 否则窄分布模型下会出现"entry_ok=True 但状态=熊市"的自相矛盾）
    if thresholds is not None:
        from backend.shared.signal_thresholds import market_state_quantile

        market_state = market_state_quantile(avg_top1, thresholds)
    else:
        market_state = _market_state(avg_top1, strong_count)

    ts_text = str(ts or "")
    opportunities: list[Opportunity] = []
    for pick in picks:
        symbol = str(pick.get("symbol") or "")
        rank_pct = float(snapshot.rank_pct_by_symbol.get(symbol, 0.0) or 0.0)
        rank_pct = min(1.0, max(0.0, rank_pct))
        industry = str(pick.get("industry") or "")
        opportunities.append(
            Opportunity(
                symbol=symbol,
                market="CN",
                sources=("model_signal",),
                strength=round(rank_pct, 6),
                score=int(round(rank_pct * 100)),
                horizon=DEFAULT_HORIZON,
                evidence={
                    "fusion_score": float(pick.get("score") or 0.0),
                    "industry": industry,
                    "trend": str(pick.get("trend") or ""),
                    "industry_top1": float(ind_top1.get(industry) or 0.0),
                    "market_state": market_state,
                    "avg_top1": float(avg_top1),
                    "strong_industry_count": int(strong_count),
                },
                ts=ts_text,
                state="watch",
            )
        )
    meta: dict[str, Any] = {
        "trade_date": snapshot.trade_date,
        "scanner": "model_signal",
        "mode": str(mode),
        "thresholds": thresholds_to_dict(thresholds),
        "config": {
            "score_min": cfg.score_min,
            "score_max": cfg.score_max,
            "main_board_only": cfg.main_board_only,
            "exclude_st": cfg.exclude_st,
            "exclude_limit_moves": cfg.exclude_limit_moves,
        },
        "avg_top1": round(float(avg_top1), 6),
        "strong_industry_count": int(strong_count),
        "market_state": market_state,
        "industry_top1": ind_top1,
        "index_ma20_ok": snapshot.index_ma20_ok,
        # 入口门禁证据（扫描器只呈现，不拦截——由策略层决定）
        "entry_gate": {
            "ma20_ok": snapshot.index_ma20_ok,
            "entry_ok": (avg_top1 >= cfg.entry_threshold),
            "strong_ok": (strong_count >= cfg.strong_industry_min),
        },
        "picked": len(opportunities),
    }
    return opportunities, meta
