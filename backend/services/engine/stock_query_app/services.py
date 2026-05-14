"""
股票查询服务模块

提供基于本地数据库的股票数据查询功能。
为后续LLM+Tushare集成预留接口。

Author: QuantMind Team
Version: 2.0.0
Last Updated: 2024-12-05
"""

import logging
import json
import os
from datetime import datetime
from functools import lru_cache
from typing import List, Optional

from sqlalchemy import or_

from backend.shared.cache import CacheManager
from backend.shared.stock_utils import StockCodeUtil

from .models import (
    MarketType,
    QueryResponse,
    SearchRequest,
    StockInfo,
    TradeStatus,
    format_stock_code,
    parse_stock_code,
)

try:
    from .local_models import SessionLocal, StockBasicInfo
except ImportError:
    StockBasicInfo = None
    SessionLocal = None

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _load_stock_index_items() -> list[dict]:
    """Load stock index json once for fallback name lookup."""
    candidates = []
    env_path = str(os.getenv("STOCK_INDEX_JSON_PATH", "")).strip()
    if env_path:
        candidates.append(env_path)
    candidates.extend(
        [
            "data/stocks/stocks_index.json",
            "/app/data/stocks/stocks_index.json",
            "/data/stocks/stocks_index.json",
        ]
    )

    for path in candidates:
        abs_path = os.path.abspath(path)
        if not os.path.exists(abs_path):
            continue
        try:
            with open(abs_path, encoding="utf-8") as f:
                payload = json.load(f)
            items = payload.get("items")
            if isinstance(items, list):
                logger.info("Loaded stock index from %s, items=%s", abs_path, len(items))
                return items
        except Exception:
            continue
    return []


def _lookup_stock_from_index(code: str) -> dict | None:
    text = str(code or "").strip().upper()
    if not text:
        return None

    prefix = StockCodeUtil.to_prefix(text)
    suffix = StockCodeUtil.to_suffix(prefix)
    pure = suffix.split(".", 1)[0] if "." in suffix else text.split(".", 1)[0]
    candidates = {text, prefix, suffix, pure}

    for item in _load_stock_index_items():
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or "").strip().upper()
        item_code = str(item.get("code") or "").strip().upper()
        item_exchange = str(item.get("exchange") or "").strip().upper()
        item_prefix = StockCodeUtil.to_prefix(symbol or f"{item_exchange}{item_code}")
        item_suffix = StockCodeUtil.to_suffix(item_prefix)
        item_pure = item_suffix.split(".", 1)[0] if "." in item_suffix else item_code
        item_candidates = {symbol, item_code, item_prefix, item_suffix, item_pure}

        if candidates & item_candidates:
            exchange = str(item.get("exchange") or "").strip().upper()
            formatted = symbol or (f"{exchange}{item_code}" if item_code and exchange else item_code)
            return {
                "code": formatted,
                "name": str(item.get("name") or formatted),
                "market": exchange or (formatted.split(".", 1)[1] if "." in formatted else ""),
                "industry": None,
                "sector": None,
                "list_date": None,
                "total_shares": None,
                "float_shares": None,
                "status": "trading",
                "company_name": None,
                "exchange": exchange,
                "currency": "CNY",
                "created_at": datetime.now().isoformat(),
                "updated_at": datetime.now().isoformat(),
            }
    return None


class StockQueryService:
    """股票查询服务

    提供本地数据库股票数据查询功能。
    后续将集成LLM解析和Tushare数据源。

    Attributes:
        cache_manager: 缓存管理器
        cache_config: 缓存配置参数
        session_factory: 数据库会话工厂
    """

    def __init__(
        self,
        cache_manager: CacheManager = None,
        session_factory=None,
    ):
        """初始化股票查询服务

        Args:
            cache_manager: 缓存管理器，用于数据缓存
            session_factory: 数据库会话工厂
        """
        self.cache_manager = cache_manager or CacheManager()

        # 缓存配置
        self.cache_config = {
            "basic_info": 86400,  # 基础信息缓存1天
            "search": 300,  # 搜索结果缓存5分钟
        }

        # 本地数据库配置
        self.session_factory = session_factory or SessionLocal
        self.use_local_db = self.session_factory is not None and StockBasicInfo is not None

    async def search_stocks(self, request: SearchRequest) -> QueryResponse:
        """搜索股票

        Args:
            request: 搜索请求对象

        Returns:
            QueryResponse: 搜索结果
        """
        if not self.use_local_db:
            return QueryResponse(success=False, message="本地数据库不可用", data=[], total=0)

        try:
            return await self._search_local_stocks(request)
        except Exception as error:
            logger.error("股票搜索失败: %s", error, exc_info=True)
            return QueryResponse(success=False, message=f"搜索失败: {str(error)}", data=[], total=0)

    async def _search_local_stocks(self, request: SearchRequest) -> QueryResponse:
        """基于本地股票主表执行模糊搜索"""
        # 生成缓存键
        cache_key = (
            f"search_local:{request.keyword}:{request.search_type}:"
            f"{request.market.value if request.market else 'all'}:"
            f"{request.limit}:{request.offset}"
        )

        # 尝试从缓存获取
        cached_result = await self.cache_manager.get(cache_key)
        if cached_result:
            logger.info("从缓存获取搜索结果: %s", request.keyword)
            return QueryResponse.from_dict(cached_result)

        keyword = request.keyword.strip()
        if not keyword:
            return QueryResponse(success=True, message="关键字为空", data=[], total=0)

        with self.session_factory() as session:
            # 基础查询: 只查询正常交易的股票
            query = session.query(StockBasicInfo).filter(StockBasicInfo.status == 1)

            # 市场过滤
            if request.market:
                query = query.filter(StockBasicInfo.exchange == request.market.value)

            # 关键词搜索
            like_pattern = f"%{keyword}%"
            if request.search_type == "code":
                query = query.filter(StockBasicInfo.stock_code.like(f"{keyword}%"))
            elif request.search_type == "name":
                query = query.filter(StockBasicInfo.stock_name.like(like_pattern))
            elif request.search_type == "industry":
                query = query.filter(StockBasicInfo.industry.like(like_pattern))
            else:  # all
                query = query.filter(
                    or_(
                        StockBasicInfo.stock_code.like(f"{keyword}%"),
                        StockBasicInfo.stock_name.like(like_pattern),
                        StockBasicInfo.industry.like(like_pattern),
                    )
                )

            # 获取总数
            total = query.count()

            # 分页查询
            results = query.order_by(StockBasicInfo.stock_code).offset(request.offset).limit(request.limit).all()

        # 转换为StockInfo对象
        stocks = [self._convert_to_stock_info(record) for record in results]

        # 构建响应
        response = QueryResponse(
            success=True,
            message=f"搜索成功，共 {total} 条记录",
            data=[stock.to_dict() for stock in stocks],
            total=total,
        )

        # 缓存结果
        await self.cache_manager.set(cache_key, response.to_dict(), ttl_l1=self.cache_config["search"])

        return response

    def _convert_to_stock_info(self, record) -> StockInfo:
        """将数据库记录转换为StockInfo对象"""
        exchange = (record.exchange or "").upper()
        try:
            market = MarketType[exchange]
        except KeyError:
            market = MarketType.SZ

        status_map = {
            1: TradeStatus.TRADING,
            0: TradeStatus.SUSPENDED,
            -1: TradeStatus.DELISTED,
        }
        status = status_map.get(record.status, TradeStatus.TRADING)

        code = record.stock_code or ""
        formatted_code = f"{exchange}{code}" if exchange else code

        return StockInfo(
            code=formatted_code,
            name=record.stock_name,
            market=market,
            industry=record.industry,
            sector=record.sector,
            list_date=record.list_date,
            pe_ttm=(float(record.pe_ttm) if record.pe_ttm is not None else None),
            pb=(float(record.pb) if record.pb is not None else None),
            ps_ttm=(float(record.ps_ttm) if record.ps_ttm is not None else None),
            pcf_ncf_ttm=(float(record.pcf_ncf_ttm) if record.pcf_ncf_ttm is not None else None),
            roe=(float(record.roe) if record.roe is not None else None),
            net_profit_margin=(float(record.net_profit_margin) if record.net_profit_margin is not None else None),
            gross_profit_margin=(float(record.gross_profit_margin) if record.gross_profit_margin is not None else None),
            is_st=bool(record.is_st),
            turnover_rate=(float(record.turnover_rate) if record.turnover_rate is not None else None),
            status=status,
            exchange=exchange,
            created_at=record.created_at or datetime.now(),
            updated_at=record.updated_at or datetime.now(),
        )

    async def get_stock_info(self, code: str) -> QueryResponse:
        """获取股票基础信息

        Args:
            code: 股票代码 (支持 000001.SZ 或 000001 格式)

        Returns:
            QueryResponse: 股票信息
        """
        try:
            stock_code, market = parse_stock_code(code)
            formatted_code = format_stock_code(stock_code, market)

            # 尝试从缓存获取
            cache_key = f"stock_info:{formatted_code}"
            cached_result = await self.cache_manager.get(cache_key)
            if cached_result:
                logger.info("从缓存获取股票信息: %s", formatted_code)
                return QueryResponse.from_dict(cached_result)

            # 从本地数据库查询
            if self.use_local_db:
                try:
                    stock_info = self._fetch_local_stock_info(stock_code, market)
                except Exception as db_error:
                    logger.warning("本地股票表查询失败，回退索引文件: %s", db_error)
                    stock_info = None

                if stock_info:
                    response = QueryResponse(
                        success=True,
                        message="获取股票信息成功",
                        data=stock_info.to_dict(),
                        total=1,
                    )
                    # 缓存结果
                    await self.cache_manager.set(
                        cache_key,
                        response.to_dict(),
                        ttl_l1=self.cache_config["basic_info"],
                    )
                    return response

            # 回退到本地 JSON 索引（不依赖 stocks 表）
            fallback = _lookup_stock_from_index(formatted_code)
            if fallback:
                response = QueryResponse(
                    success=True,
                    message="通过本地索引获取股票信息成功",
                    data=fallback,
                    total=1,
                )
                await self.cache_manager.set(
                    cache_key,
                    response.to_dict(),
                    ttl_l1=self.cache_config["basic_info"],
                )
                return response

            # 未找到
            return QueryResponse(
                success=True,
                message=f"未找到股票信息: {formatted_code}",
                data=None,
                total=0,
            )

        except Exception as e:
            logger.error("获取股票信息失败: %s", e)
            return QueryResponse(success=False, message=f"获取股票信息失败: {str(e)}")

    def _fetch_local_stock_info(self, stock_code: str, market: MarketType) -> StockInfo | None:
        """从本地数据库查询股票信息"""
        if not self.session_factory or StockBasicInfo is None:
            return None

        with self.session_factory() as session:
            record = (
                session.query(StockBasicInfo)
                .filter(
                    StockBasicInfo.stock_code == stock_code,
                    StockBasicInfo.exchange == market.value,
                    StockBasicInfo.status == 1,
                )
                .first()
            )

            if not record:
                return None

            return self._convert_to_stock_info(record)


class StockSearchService:
    """股票搜索专用服务"""

    def __init__(self, query_service: StockQueryService = None):
        self.query_service = query_service or StockQueryService()

    async def search_by_code(self, code: str) -> QueryResponse:
        """按代码搜索股票"""
        request = SearchRequest(keyword=code, search_type="code", limit=1)
        return await self.query_service.search_stocks(request)

    async def search_by_name(self, name: str, limit: int = 20) -> QueryResponse:
        """按名称搜索股票"""
        request = SearchRequest(keyword=name, search_type="name", limit=limit)
        return await self.query_service.search_stocks(request)

    async def search_by_keyword(self, keyword: str, limit: int = 20) -> QueryResponse:
        """按关键词搜索股票"""
        request = SearchRequest(keyword=keyword, search_type="all", limit=limit)
        return await self.query_service.search_stocks(request)

    async def search_by_industry(self, industry: str, limit: int = 50) -> QueryResponse:
        """按行业搜索股票"""
        request = SearchRequest(keyword=industry, search_type="industry", limit=limit)
        return await self.query_service.search_stocks(request)

    async def search_stocks(self, query: str, limit: int = 10) -> list[dict]:
        """搜索股票 (兼容路由调用的方法名)"""
        response = await self.search_by_keyword(query, limit=limit)
        if response.success:
            return response.data
        return []
