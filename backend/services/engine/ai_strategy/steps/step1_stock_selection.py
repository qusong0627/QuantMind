"""Step 1: 股票池选择 - 条件解析与 DSL 生成"""

import logging
import os
import re
from typing import Any, Dict, List

from ..api.schemas.stock_pool import (
    Condition,
    ParseResponse,
)

logger = logging.getLogger(__name__)

FACTOR_COLUMN_MAP = {
    # ============ 估值因子 ============
    "market_cap": "total_mv",
    "float_mv": "float_mv",
    "pe": "pe_ttm",
    "pe_ttm": "pe_ttm",
    "pb": "pb",
    "ps": "ps_ratio",
    "roe": "roe",
    "bp": "bp",
    "ep_ttm": "ep_ttm",
    "ln_mv_total": "ln_mv_total",

    # ============ 价格因子 ============
    "close": "close",
    "open": "open",
    "high": "high",
    "low": "low",
    "pct_change": "pct_change",
    "pct_chg": "pct_change",
    "turnover_rate": "turnover_rate",
    "adj_factor": "adj_factor",

    # ============ 均线因子 ============
    "ma5": "ma5",
    "ma10": "ma10",
    "ma20": "ma20",
    "ma60": "ma60",
    "sma5": "ma5",
    "sma10": "ma10",
    "sma20": "ma20",
    "sma60": "ma60",
    "ma_gap_5": "ma_gap_5",
    "ma_gap_10": "ma_gap_10",
    "ma_gap_20": "ma_gap_20",

    # ============ 技术指标 ============
    "rsi": "rsi_14",
    "rsi_6": "rsi_6",
    "rsi_14": "rsi_14",
    "kdj_k": "kdj_k",
    "kdj_d": "kdj_d",
    "kdj_j": "kdj_j",
    "macd": "macd_hist",
    "macd_dif": "macd_dif",
    "macd_dea": "macd_dea",
    "macd_hist": "macd_hist",
    "dif": "macd_dif",
    "dea": "macd_dea",

    # ============ 收益率因子 ============
    "return_1d": "return_1d",
    "return_3d": "return_3d",
    "return_5d": "return_5d",
    "return_10d": "return_10d",
    "return_20d": "return_20d",
    "return_60d": "return_60d",

    # ============ 波动率因子 ============
    "vol_std_5": "vol_std_5",
    "vol_std_20": "vol_std_20",
    "vol_std_60": "vol_std_60",
    "vol_atr_14": "vol_atr_14",
    "atr": "vol_atr_14",
    "beta_20": "beta_20",

    # ============ 量能因子 ============
    "volume": "volume",
    "amount": "amount",
    "volume_ratio_5": "volume_ratio_5",
    "volume_ratio_20": "volume_ratio_20",
    "volume_ma_5": "volume_ma_5",
    "volume_ma_3": "volume_ma_3",
    "amount_ma_5": "amount_ma_5",

    # ============ 指数成分 ============
    "idx_hs300": "idx_hs300",
    "idx_zz500": "idx_zz500",
    "idx_zz1000": "idx_zz1000",
    "idx_margin": "idx_margin",
    "idx_chinext": "idx_chinext",
    "idx_all": "idx_all",
    "is_hs300": "idx_hs300",
    "hs300": "idx_hs300",
    "is_csi300": "idx_hs300",
    "csi300": "idx_hs300",
    "is_csi1000": "idx_zz1000",
    "csi1000": "idx_zz1000",
    "is_zz500": "idx_zz500",
    "zz500": "idx_zz500",
    "is_zz1000": "idx_zz1000",
    "zz1000": "idx_zz1000",

    # ============ 概念标签 ============
    "concept_ai": "concept_ai",
    "concept_chip": "concept_chip",
    "concept_new_energy": "concept_new_energy",
    "concept_ev": "concept_ev",
    "concept_pv": "concept_pv",
    "concept_lithium": "concept_lithium",
    "concept_semiconductor": "concept_semiconductor",
    "concept_military": "concept_military",
    "concept_medical": "concept_medical",
    "concept_cyber": "concept_cyber",
    "concept_fintech": "concept_fintech",
    "concept_consumption": "concept_consumption",
    "concept_real_estate": "concept_real_estate",
    "concept_infrastructure": "concept_infrastructure",
    "concept_state_owned": "concept_state_owned",

    # ============ 其他因子 ============
    "is_st": "is_st",
    "listed_days": "listed_days",
    "limit_up_today": "limit_up_today",
    "limit_down_today": "limit_down_today",
    "consecutive_limit_up_days": "consecutive_limit_up_days",
    "industry": "industry",
    "province": "province",
    "listing_market": "listing_market",
    "is_suspended": "is_suspended",
    "is_listed_over_1y": "is_listed_over_1y",
    "net_profit_growth": "net_profit_growth",
    "profit_growth": "profit_growth",
    "inst_ownership": "inst_ownership",
    "main_flow": "main_flow",
}

DSL_PREFIX = "SELECT symbol WHERE "
DELTA_REGEX = re.compile(
    r"DELTA\((?P<factor>[a-zA-Z0-9_]+),(?P<window>\d+)\)\s*" r"(?P<op>>=|<=|==|!=|>|<)\s*(?P<value>-?\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
SIMPLE_REGEX = re.compile(
    r"(?P<factor>[a-zA-Z0-9_]+)\s*(?P<op>>=|<=|==|!=|>|<|=)\s*(?P<value>-?\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
COMBINER_REGEX = re.compile(r"\s+(AND|OR)\s+", re.IGNORECASE)
MAX_LOOKBACK_DAYS = 400
LATEST_TABLE = "stock_daily_latest"

# total_mv 列口径可配置：默认“万元”（1亿=10000）。
# 若你的数据库是“千元”，可通过环境变量 AI_STRATEGY_TOTAL_MV_PER_YI=100000 覆盖。
MARKET_CAP_YI_TO_DB_UNIT = float(os.getenv("AI_STRATEGY_TOTAL_MV_PER_YI", "10000"))


def _condition_to_dsl(cond: Condition) -> str:
    t = cond.get("type")
    if t == "numeric":
        factor = cond["factor"]
        threshold = cond["threshold"]
        if factor == "market_cap":
            threshold = float(threshold) * MARKET_CAP_YI_TO_DB_UNIT
        return f"SELECT symbol WHERE {factor} {cond['operator']} {threshold}"
    if t == "trend":
        sign = "> 0" if cond.get("direction") == "up" else "< 0"
        return f"SELECT symbol WHERE DELTA({cond['factor']},{cond['window']}) {sign}"
    if t == "composite":
        children = cond.get("children", [])
        parts = [_condition_to_dsl(c).replace("SELECT symbol WHERE ", "") for c in children]
        op = cond.get("op", "AND").upper()
        return "SELECT symbol WHERE " + (f" {op} ".join(parts) if parts else "true")
    raise ValueError(f"未知条件类型: {t}")


def _extract_factors(cond: Condition) -> list[str]:
    t = cond.get("type")
    if t in ("numeric", "trend"):
        return [cond.get("factor")]
    if t == "composite":
        facs: list[str] = []
        for c in cond.get("children", []):
            facs.extend(_extract_factors(c))
        return facs
    return []


def _parse_dsl(dsl: str) -> tuple[list[dict[str, Any]], list[str]]:
    expr = dsl[len(DSL_PREFIX) :].strip()
    if not expr or expr.lower() == "true":
        return [], []

    parts = COMBINER_REGEX.split(expr)
    conditions: list[dict[str, Any]] = []
    combiners: list[str] = []
    for idx, part in enumerate(parts):
        if idx % 2 == 1:
            combiners.append(part.upper())
            continue

        text_part = part.strip()
        match = DELTA_REGEX.match(text_part)
        if match:
            conditions.append(
                {
                    "type": "delta",
                    "factor": match.group("factor"),
                    "window": int(match.group("window")),
                    "op": match.group("op"),
                    "value": float(match.group("value")),
                }
            )
            continue

        match = SIMPLE_REGEX.match(text_part)
        if match:
            conditions.append(
                {
                    "type": "simple",
                    "factor": match.group("factor"),
                    "op": match.group("op"),
                    "value": float(match.group("value")),
                }
            )
            continue

        raise ValueError(f"无法解析条件: {text_part}")

    if combiners and len(combiners) != len(conditions) - 1:
        raise ValueError("DSL条件解析失败：连接符数量异常")

    return conditions, combiners


def _map_factor(factor: str) -> str:
    key = factor.strip()
    if key not in FACTOR_COLUMN_MAP:
        raise ValueError(f"暂不支持的因子: {factor}")
    return FACTOR_COLUMN_MAP[key]


def parse_conditions(conditions: Condition) -> ParseResponse:
    """解析前端条件树为 DSL 语句"""
    dsl = _condition_to_dsl(conditions)
    mapping = {"factors": _extract_factors(conditions)}
    warnings = []
    suggestions = []
    if "market_cap" in mapping["factors"]:
        suggestions.append("可考虑加入行业过滤以提升针对性")
    return ParseResponse(
        dsl=dsl,
        mapping=mapping,
        warnings=warnings,
        confidence=0.95,
        suggestions=suggestions,
        version="1.0.0",
    )
