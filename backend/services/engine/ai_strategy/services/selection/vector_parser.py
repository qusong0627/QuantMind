import logging
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

# 定义量化策略原型库（用于语义路由）
STRATEGY_PROTOTYPES = {
    "value_investing": [
        "低市盈率低市净率价值投资",
        "寻找被低估的蓝筹股",
        "PB和PE都处于历史低位的股票",
        "高分红且估值合理的公司",
        "破净股或者是低PB的优质公司",
        "沪深300成分股中估值最低的标的",
        "高股息率且市值较大的公司",
    ],
    "growth_investing": [
        "高增长绩优股",
        "净利润增长率超过30%的创业板股票",
        "ROE持续改善的高成长公司",
        "营收和利润双增长的白马股",
        "高资本开支且业绩兑现的成长股",
        "中证500中的高成长中小盘股票",
        "研发投入占比高且收入增长快的科创板公司",
    ],
    "technical_analysis": [
        "RSI超卖且放量反弹",
        "KDJ超卖区域回升的股票",
        "放量突破关键压力位的标的",
        "低位放量的个股筛选",
        "RSI指标底背离的股票",
        "股价站上60日均线且成交量放大的股票",
        "布林带触底反弹且换手率增加的标的",
    ],
    "market_sentiment": [
        "近期涨停且换手率高的妖股",
        "热门题材板块的领领涨龙头",
        "资金大幅流入且股价强势的个股",
        "游资青睐的高波动标的",
        "短期涨幅剧烈且成交额巨大的股票",
        "全市场换手率前10%的热门股票",
        "大幅放量且价格突破近期高点的个股",
    ],
    "index_component_filtering": [
        "沪深300成分股筛选",
        "中证500中的优质标的",
        "中证1000小盘股精选",
        "属于中证800指数的标的",
        "排除ST后的沪深300股票",
    ],
    "corporate_governance": [
        "国有企业中的高分红标的",
        "优质民营企业筛选",
        "具有央企背景的蓝筹股",
        "地方国企中估值较低的股票",
    ],
}


class StrategyVectorParser:
    """基于 DashScope embedding 的策略语义解析器"""

    def __init__(self):
        self.client = DashScopeClient()
        self.model = _get_ai_strategy_config().DASHSCOPE_EMBEDDING_MODEL

        self._prototype_vectors: dict[str, list[np.ndarray]] = {}
        self._initialized = False

    def _get_embedding(self, text: str) -> np.ndarray | None:
        """获取文本向量"""
        try:
            response = self.client.get_embedding(text, model=self.model)
            return np.array(response["vector"])
        except Exception as e:
            logger.error("Vector embedding failed: %s", e)
            return None

    @staticmethod
    def _cosine_similarity(v1: np.ndarray, v2: np.ndarray) -> float:
        """计算余弦相似度"""
        return np.dot(v1, v2) / (np.linalg.norm(v1) * np.linalg.norm(v2))

    async def initialize(self):
        """预计算并缓存策略原型向量"""
        if self._initialized:
            return

        logger.info("Initializing StrategyVectorParser prototype embeddings...")
        for category, samples in STRATEGY_PROTOTYPES.items():
            vectors = []
            for sample in samples:
                vec = self._get_embedding(sample)
                if vec is not None:
                    vectors.append(vec)
            self._prototype_vectors[category] = vectors
        self._initialized = True
        logger.info("StrategyVectorParser initialization complete.")

    def match_strategy_prototype(self, query: str) -> tuple[str, float]:
        """将用户查询匹配到最近似的策略原型"""
        if not self._initialized:
            return "general", 0.0

        query_vec = self._get_embedding(query)
        if query_vec is None:
            return "general", 0.0

        best_category = "general"
        max_sim = 0.0
        for category, vectors in self._prototype_vectors.items():
            for vec in vectors:
                sim = self._cosine_similarity(query_vec, vec)
                if sim > max_sim:
                    max_sim = sim
                    best_category = category
        return best_category, max_sim


_vector_parser: Optional["StrategyVectorParser"] = None


async def get_strategy_vector_parser() -> "StrategyVectorParser":
    global _vector_parser
    if _vector_parser is None:
        _vector_parser = StrategyVectorParser()
        await _vector_parser.initialize()
    return _vector_parser
