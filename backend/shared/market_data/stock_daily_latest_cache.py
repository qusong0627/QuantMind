"""
stock_daily_latest 表的 Redis 缓存层
提供高性能的最新行情读取能力
"""

import json
import logging
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from backend.shared.market_db_manager import get_market_session
from backend.shared.redis_sentinel_client import get_redis_sentinel_client
from backend.shared.stock_utils import StockCodeUtil

logger = logging.getLogger(__name__)

# Redis Key 前缀
CACHE_KEY_PREFIX = "qm:stock_latest"
# 缓存有效期：30 分钟
CACHE_TTL = 1800 

class StockDailyLatestCache:
    """stock_daily_latest 表缓存管理器"""

    def __init__(self):
        self.redis = get_redis_sentinel_client()

    def _get_key(self, symbol: str) -> str:
        # 统一代码格式，例如 SH600519
        formatted_symbol = StockCodeUtil.to_prefix(symbol)
        return f"{CACHE_KEY_PREFIX}:{formatted_symbol}"

    def _serialize(self, data: dict) -> str:
        # 处理日期对象的序列化
        def json_serial(obj):
            if isinstance(obj, (datetime, date)):
                return obj.isoformat()
            raise TypeError(f"Type {type(obj)} not serializable")
        return json.dumps(data, default=json_serial)

    async def get_latest(self, symbol: str) -> Optional[dict[str, Any]]:
        """获取单只股票的最新行情（优先从缓存读取）"""
        key = self._get_key(symbol)
        try:
            cached_data = self.redis.get(key)
            if cached_data:
                return json.loads(cached_data)
        except Exception as e:
            logger.warning(f"Failed to read cache for {symbol}: {e}")

        # 缓存未命中，从数据库读取
        async with get_market_session() as session:
            result = await session.execute(
                text("SELECT * FROM stock_daily_latest WHERE symbol = :s ORDER BY trade_date DESC LIMIT 1"),
                {"s": symbol}
            )
            row = result.mappings().first()
            if row:
                data = dict(row)
                # 异步写入缓存（不阻塞主流程）
                self.set_latest_sync(symbol, data)
                return data
        return None

    def set_latest_sync(self, symbol: str, data: dict):
        """同步写入缓存"""
        key = self._get_key(symbol)
        try:
            self.redis.set(key, self._serialize(data), ex=CACHE_TTL)
        except Exception as e:
            logger.error(f"Failed to set cache for {symbol}: {e}")

    async def batch_get_latest(self, symbols: list[str]) -> dict[str, Any]:
        """批量获取最新行情"""
        if not symbols:
            return {}
            
        results = {}
        missing_symbols = []
        
        # 1. 尝试从 Redis 批量读取
        keys = [self._get_key(s) for s in symbols]
        try:
            cached_values = self.redis.mget(keys)
            for i, val in enumerate(cached_values):
                if val:
                    results[symbols[i]] = json.loads(val)
                else:
                    missing_symbols.append(symbols[i])
        except Exception as e:
            logger.warning(f"Redis mget failed: {e}")
            missing_symbols = symbols

        # 2. 缺失部分从数据库补偿
        if missing_symbols:
            async with get_market_session() as session:
                # 注意：这里使用 IN 查询，且只取最新日期的数据
                # 为了性能，建议数据库有 (symbol, trade_date) 的联合索引
                query = text("""
                    SELECT * FROM stock_daily_latest 
                    WHERE symbol = ANY(:symbols)
                """)
                db_results = await session.execute(query, {"symbols": missing_symbols})
                
                # 由于 stock_daily_latest 理论上每只股票只有一行最新数据，直接存入即可
                pipe = self.redis.pipeline()
                for row in db_results.mappings():
                    data = dict(row)
                    s = data['symbol']
                    results[s] = data
                    pipe.set(self._get_key(s), self._serialize(data), ex=CACHE_TTL)
                pipe.execute()
                
        return results

    async def warmup_cache(self) -> int:
        """预热缓存：将全市场最新行情载入 Redis"""
        logger.info("Starting stock_daily_latest cache warmup...")
        async with get_market_session() as session:
            # 获取全市场最新的记录（每个 symbol 仅取日期最大的一条）
            result = await session.execute(text("""
                SELECT DISTINCT ON (symbol) * 
                FROM stock_daily_latest 
                ORDER BY symbol, trade_date DESC
            """))
            rows = result.mappings().all()
            
            if not rows:
                logger.warning("No data found in stock_daily_latest for warmup.")
                return 0

            pipe = self.redis.pipeline()
            count = 0
            for row in rows:
                data = dict(row)
                pipe.set(self._get_key(data['symbol']), self._serialize(data), ex=CACHE_TTL)
                count += 1
                if count % 500 == 0:
                    pipe.execute()
                    pipe = self.redis.pipeline()
            
            pipe.execute()
            logger.info(f"Cache warmup completed. Processed {count} symbols.")
            return count

# 全局单例
stock_latest_cache = StockDailyLatestCache()
