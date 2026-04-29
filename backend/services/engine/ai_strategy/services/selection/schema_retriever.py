import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import numpy as np

from ...ai_strategy_config import get_config as _get_config
from .dashscope_client import DashScopeClient

# 延迟加载配置
_ai_strategy_config = None


def _get_ai_strategy_config():
    """延迟获取配置"""
    global _ai_strategy_config
    if _ai_strategy_config is None:
        _ai_strategy_config = _get_config()
    return _ai_strategy_config

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SchemaColumn:
    table: str
    name: str
    description: str


TABLE_DESCRIPTIONS: dict[str, str] = {
    "stock_selection": "滚动近30天选股因子表，适合近期快速筛选与技术指标组合。",
    "stock_daily": "日线全量历史行情与估值表，适合长期/历史/风控/波动类筛选。",
    "stock_daily_latest": "最新交易日全市场快照表（每股一行），适合实时筛选与条件查询。",
}


STOCK_SELECTION_SCHEMA: list[SchemaColumn] = [
    SchemaColumn("stock_selection", "trade_date", "交易日期"),
    SchemaColumn("stock_selection", "symbol", "股票代码"),
    SchemaColumn("stock_selection", "name", "股票名称"),
    SchemaColumn("stock_selection", "close", "收盘价"),
    SchemaColumn("stock_selection", "market_cap", "总市值（万元）"),
    SchemaColumn("stock_selection", "industry", "行业"),
    SchemaColumn("stock_selection", "pe_ratio", "市盈率 TTM"),
    SchemaColumn("stock_selection", "pb_ratio", "市净率"),
    SchemaColumn("stock_selection", "ps_ratio", "市销率"),
    SchemaColumn("stock_selection", "roe", "净资产收益率"),
    SchemaColumn("stock_selection", "net_profit_growth", "净利润增长率"),
    SchemaColumn("stock_selection", "volume", "成交量（手）"),
    SchemaColumn("stock_selection", "amount", "成交额（元）"),
    SchemaColumn("stock_selection", "turnover_rate", "换手率"),
    SchemaColumn("stock_selection", "pct_chg", "涨跌幅"),
    SchemaColumn("stock_selection", "float_share_ratio", "流通股占比"),
    SchemaColumn("stock_selection", "is_st", "是否 ST"),
    SchemaColumn("stock_selection", "is_suspended", "是否停牌"),
    SchemaColumn("stock_selection", "is_listed_over_1y", "上市是否超过一年"),
    SchemaColumn("stock_selection", "macd_dif", "MACD DIF"),
    SchemaColumn("stock_selection", "macd_dea", "MACD DEA"),
    SchemaColumn("stock_selection", "macd_hist", "MACD 柱"),
    SchemaColumn("stock_selection", "kdj_k", "KDJ K"),
    SchemaColumn("stock_selection", "kdj_d", "KDJ D"),
    SchemaColumn("stock_selection", "kdj_j", "KDJ J"),
    SchemaColumn("stock_selection", "sma5", "5日均线"),
    SchemaColumn("stock_selection", "sma20", "20日均线"),
    SchemaColumn("stock_selection", "sma60", "60日均线"),
    SchemaColumn("stock_selection", "rsi", "RSI"),
]


STOCK_DAILY_SCHEMA: list[SchemaColumn] = [
    SchemaColumn("stock_daily", "trade_date", "交易日期"),
    SchemaColumn("stock_daily", "code", "股票代码"),
    SchemaColumn("stock_daily", "stock_name", "股票名称"),
    SchemaColumn("stock_daily", "open", "开盘价"),
    SchemaColumn("stock_daily", "high", "最高价"),
    SchemaColumn("stock_daily", "low", "最低价"),
    SchemaColumn("stock_daily", "close", "收盘价"),
    SchemaColumn("stock_daily", "volume", "成交量（手）"),
    SchemaColumn("stock_daily", "turnover", "成交额（元）"),
    SchemaColumn("stock_daily", "pct_change", "涨跌幅"),
    SchemaColumn("stock_daily", "turnover_rate", "换手率"),
    SchemaColumn("stock_daily", "pe_ttm", "市盈率 TTM"),
    SchemaColumn("stock_daily", "pb", "市净率"),
    SchemaColumn("stock_daily", "total_mv", "总市值（万元）"),
    SchemaColumn("stock_daily", "is_st", "是否 ST (1/0)"),
    SchemaColumn("stock_daily", "is_csi300", "是否沪深300成分股 (1/0)"),
    SchemaColumn("stock_daily", "is_csi500", "是否中证500成分股 (1/0)"),
    SchemaColumn("stock_daily", "is_csi800", "是否中证800成分股 (1/0)"),
    SchemaColumn("stock_daily", "is_csi1000", "是否中证1000成分股 (1/0)"),
]

STOCK_DAILY_LATEST_SCHEMA: list[SchemaColumn] = [
    SchemaColumn("stock_daily_latest", "trade_date", "交易日期"),
    SchemaColumn("stock_daily_latest", "code", "股票代码"),
    SchemaColumn("stock_daily_latest", "stock_name", "股票名称"),
    SchemaColumn("stock_daily_latest", "open", "开盘价"),
    SchemaColumn("stock_daily_latest", "high", "最高价"),
    SchemaColumn("stock_daily_latest", "low", "最低价"),
    SchemaColumn("stock_daily_latest", "close", "收盘价"),
    SchemaColumn("stock_daily_latest", "volume", "成交量（手）"),
    SchemaColumn("stock_daily_latest", "turnover", "成交额（元）"),
    SchemaColumn("stock_daily_latest", "pct_change", "涨跌幅"),
    SchemaColumn("stock_daily_latest", "turnover_rate", "换手率"),
    SchemaColumn("stock_daily_latest", "pe_ttm", "市盈率 TTM"),
    SchemaColumn("stock_daily_latest", "pb", "市净率"),
    SchemaColumn("stock_daily_latest", "total_mv", "总市值（万元）"),
    SchemaColumn("stock_daily_latest", "is_st", "是否 ST (1/0)"),
    SchemaColumn("stock_daily_latest", "is_hs300", "是否沪深300成分股 (1/0)"),
    SchemaColumn("stock_daily_latest", "is_csi1000", "是否中证1000成分股 (1/0)"),
    SchemaColumn("stock_daily_latest", "industry", "行业分类（如：半导体、互联网、银行、白酒等）"),
]


SCHEMAS: dict[str, list[SchemaColumn]] = {
    "stock_selection": STOCK_SELECTION_SCHEMA,
    "stock_daily": STOCK_DAILY_SCHEMA,
    "stock_daily_latest": STOCK_DAILY_LATEST_SCHEMA,
}


def _cosine_similarity(v1: np.ndarray, v2: np.ndarray) -> float:
    return float(np.dot(v1, v2) / (np.linalg.norm(v1) * np.linalg.norm(v2)))


class SchemaRetriever:
    def __init__(self) -> None:
        self.client = DashScopeClient()
        self.model = _get_ai_strategy_config().DASHSCOPE_EMBEDDING_MODEL
        self._column_vectors: dict[str, list[tuple[SchemaColumn, np.ndarray]]] = {}
        self._initialized = False

    def _embed(self, text: str) -> np.ndarray | None:
        try:
            resp = self.client.get_embedding(text, model=self.model)
            return np.array(resp["vector"])
        except Exception as exc:
            logger.error("Schema embedding failed: %s", exc)
            return None

    async def initialize(self) -> None:
        if self._initialized:
            return
        for table, columns in SCHEMAS.items():
            vectors: list[tuple[SchemaColumn, np.ndarray]] = []
            for col in columns:
                vec = self._embed(f"{col.name} {col.description}")
                if vec is not None:
                    vectors.append((col, vec))
            self._column_vectors[table] = vectors
        self._initialized = True

    def _heuristic_table_bias(self, query: str) -> dict[str, float]:
        q = query.lower()
        bias = {"stock_selection": 0.0, "stock_daily": 0.0, "stock_daily_latest": 0.0}
        if any(k in q for k in ["近30", "近期", "最近", "短期", "当日", "今日", "最新"]):
            bias["stock_daily_latest"] += 0.2
        if any(
            k in q
            for k in [
                "历史",
                "长期",
                "多年",
                "回撤",
                "波动",
                "beta",
                "atr",
                "boll",
                "stoch",
            ]
        ):
            bias["stock_daily"] += 0.15
        if any(k in q for k in ["全市场", "全量", "全部", "快照"]):
            bias["stock_daily_latest"] += 0.2
        return bias

    async def retrieve(self, query: str, top_k: int = 12) -> dict[str, object]:
        if not self._initialized:
            await self.initialize()

        query_vec = self._embed(query)
        if query_vec is None:
            return {
                "target_table": "stock_daily_latest",
                "table_scores": {
                    "stock_selection": 0.0,
                    "stock_daily": 0.0,
                    "stock_daily_latest": 0.0,
                },
                "candidate_fields": [],
                "allowed_fields": [c.name for c in STOCK_DAILY_LATEST_SCHEMA],
            }

        table_scores: dict[str, float] = {}
        candidates: dict[str, list[tuple[SchemaColumn, float]]] = {}
        for table, vectors in self._column_vectors.items():
            sims: list[tuple[SchemaColumn, float]] = []
            for col, vec in vectors:
                sims.append((col, _cosine_similarity(query_vec, vec)))
            sims.sort(key=lambda x: x[1], reverse=True)
            candidates[table] = sims
            top_scores = [s for _, s in sims[: min(top_k, len(sims))]]
            table_scores[table] = float(np.mean(top_scores)) if top_scores else 0.0

        bias = self._heuristic_table_bias(query)
        for table, score in table_scores.items():
            table_scores[table] = score + bias.get(table, 0.0)

        target_table = max(table_scores.items(), key=lambda x: x[1])[0]
        top_candidates = candidates.get(target_table, [])[:top_k]

        return {
            "target_table": target_table,
            "table_scores": table_scores,
            "candidate_fields": [
                {
                    "name": col.name,
                    "description": col.description,
                    "score": round(score, 4),
                }
                for col, score in top_candidates
            ],
            "allowed_fields": [c.name for c in SCHEMAS[target_table]],
        }


_retriever: SchemaRetriever | None = None


async def get_schema_retriever() -> SchemaRetriever:
    global _retriever
    if _retriever is None:
        _retriever = SchemaRetriever()
        await _retriever.initialize()
    return _retriever
