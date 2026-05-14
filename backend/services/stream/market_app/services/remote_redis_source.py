"""
远程 Redis 行情快照数据源
读取外部推送到 Redis 的全市场快照数据

优先 Key 格式: market:snapshot:{symbol}  (e.g. market:snapshot:sh600000)
兼容 Key 格式: stock:{code}.{market}      (e.g. stock:600000.SH)
字段:          Now, Open, High, Low, PreClose/Close, Volume, Amount, timestamp
"""

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import redis.asyncio as aioredis

from ..market_config import settings
from .data_source import DataSourceAdapter
from backend.shared.stock_utils import StockCodeUtil

logger = logging.getLogger(__name__)


class RemoteRedisDataSource(DataSourceAdapter):
    """
    第三方行情快照 Redis 数据源 (OSS Edition)
    使用统一 Redis 实例 (REDIS_DB_MARKET)
    """

    def __init__(self):
        self._host = (settings.REDIS_HOST or "quantmind-redis").strip()
        self._port = int(settings.REDIS_PORT or 6379)
        self._password = (settings.REDIS_PASSWORD or "").strip() or None
        self._db = int(settings.REDIS_DB or 3)
        self._client: aioredis.Redis | None = None

    def _get_client(self) -> aioredis.Redis:
        if self._client is None:
            self._client = aioredis.Redis(
                host=self._host,
                port=self._port,
                password=self._password,
                db=self._db,
                decode_responses=True,
                socket_connect_timeout=3,
                socket_timeout=5,
            )
        return self._client

    def _normalize_symbol(self, symbol: str) -> str:
        """
        统一转换为 Prefix 格式 (SH600000)
        遵循 AGENTS.md 强制规范
        """
        return StockCodeUtil.to_prefix(symbol)

    def _to_snapshot_symbol(self, normalized_symbol: str) -> str:
        """
        将标准化代码转换为规范中的 symbol（小写市场前缀）
        600000.SH -> sh600000
        """
        parts = normalized_symbol.split(".")
        if len(parts) != 2:
            return normalized_symbol.lower()
        code, market = parts[0], parts[1].upper()
        prefix = "sh" if market == "SH" else ("sz" if market == "SZ" else "bj")
        return f"{prefix}{code}"

    async def fetch_quote(self, symbol: str) -> dict[str, Any] | None:
        """从远程 Redis 读取单只行情快照"""
        quotes = await self.fetch_quotes([symbol])
        return quotes[0] if quotes else None

    async def fetch_quotes(self, symbols: list[str]) -> list[dict[str, Any]]:
        """
        批量从远程 Redis 读取行情快照
        支持高性能的 Pipeline 批量查询
        """
        if not symbols:
            return []

        client = self._get_client()
        normalized_map = {self._normalize_symbol(s): s for s in symbols}
        
        # 批量构建查询 Key 列表 (全路径组合，增加容错性)
        # 每个 normalized symbol 会产生 3 个 candidate snapshot keys:
        # 1. market:snapshot:sh600000 (规范小写前缀)
        # 2. market:snapshot:SH600000 (规范大写前缀)
        # 3. stock:600000.SH (Legacy 后缀格式)
        
        pipeline_keys = []
        symbol_key_count = 3
        for n in normalized_map.keys():
            # n 已经是 SH600000 格式
            prefix = n[:2].lower() # sh
            code = n[2:] # 600000
            legacy = f"{code}.{n[:2].upper()}" # 600000.SH
            
            pipeline_keys.append(f"market:snapshot:{prefix}{code}")
            pipeline_keys.append(f"market:snapshot:{n}")
            pipeline_keys.append(f"stock:{legacy}")

        try:
            async with client.pipeline(transaction=False) as pipe:
                for key in pipeline_keys:
                    pipe.hgetall(key)
                results = await pipe.execute()
        except Exception as e:
            logger.error(f"[RemoteRedis] 批量读取失败: {e}")
            return []

        final_quotes = []
        now_ts = time.time()

        for idx, (normalized, original_symbol) in enumerate(normalized_map.items()):
            # 每个 symbol 对应 3 个结果，取第一个非空的
            start_idx = idx * symbol_key_count
            data = None
            for offset in range(symbol_key_count):
                candidate = results[start_idx + offset]
                if candidate:
                    data = candidate
                    break
            
            if not data:
                continue

            def _f(field: str) -> float | None:
                v = data.get(field)
                try:
                    return float(v) if v is not None else None
                except:
                    return None

            # 新鲜度规则:
            # - >60s 记为陈旧并告警
            # - >300s 视为不可用，直接跳过（触发上层安全保护）
            ts_raw = int(data.get("timestamp", 0) or 0)
            age = (now_ts - ts_raw) if ts_raw > 0 else 999999
            is_stale = age > 60

            if is_stale:
                logger.warning(
                    f"[RemoteRedis] 数据陈旧: {normalized} (age: {int(age)}s)"
                )
            if age > 300:
                logger.warning(
                    f"[RemoteRedis] 数据不可用(>300s): {normalized} (age: {int(age)}s)"
                )
                continue

            now_price = _f("Now")
            open_price = _f("Open")
            pre_close = _f("PreClose") or _f("Close")
            # 规范 required: Now/PreClose/Open/timestamp，缺失视为不可用
            if (
                now_price is None
                or open_price is None
                or pre_close is None
                or ts_raw <= 0
            ):
                logger.warning(f"[RemoteRedis] 快照字段缺失: {normalized}")
                continue

            final_quotes.append(
                {
                    "symbol": original_symbol,
                    "timestamp": datetime.fromtimestamp(ts_raw, tz=timezone.utc),
                    "current_price": now_price,
                    "open_price": open_price,
                    "high_price": _f("High"),
                    "low_price": _f("Low"),
                    "close_price": pre_close,
                    "volume": int(_f("Volume") or 0),
                    "amount": _f("Amount"),
                    "is_stale": is_stale,
                    "data_source": "remote_redis",
                }
            )

        return final_quotes

    async def fetch_kline(
        self,
        symbol: str,
        interval: str,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """远程 Redis 快照不包含 K 线历史，返回空列表"""
        logger.debug(f"[RemoteRedis] 不支持 K 线数据: {symbol}")
        return []

    async def fetch_series(
        self, symbol: str, seconds: int = 3600
    ) -> list[dict[str, Any]]:
        """
        从 Redis ZSET 中拉取过去 X 秒的行情序列
        用于计算日内均线、RSI 等技术指标
        """
        client = self._get_client()
        normalized = self._normalize_symbol(symbol)
        series_key = f"market:series:{normalized}"

        now_ts = int(time.time())
        start_ts = now_ts - seconds

        try:
            # 获取时间窗口内的所有快照点
            raw_data = await client.zrange(series_key, start_ts, now_ts, byscore=True)

            results = []
            for item in raw_data:
                try:
                    d = json.loads(item)
                    results.append(d)
                except:
                    continue
            return results
        except Exception as e:
            logger.error(f"[RemoteRedis] 拉取时序数据失败 {symbol}: {e}")
            return []

    async def append_series_point(
        self,
        symbol: str,
        quote: dict[str, Any],
        max_points: int = 6000,
        ttl_seconds: int = 172800,
    ) -> bool:
        """将实时行情追加到时序 ZSET，形成可回放序列闭环。"""
        client = self._get_client()
        normalized = self._normalize_symbol(symbol)
        series_key = f"market:series:{normalized}"

        dt = quote.get("timestamp")
        if isinstance(dt, datetime):
            aware_dt = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            ts = int(aware_dt.timestamp())
            dt_iso = aware_dt.astimezone(timezone.utc).isoformat()
        else:
            ts = int(time.time())
            dt_iso = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()

        payload = {
            "symbol": symbol,
            "normalized_symbol": normalized,
            "timestamp": ts,
            "datetime": dt_iso,
            "price": quote.get("current_price"),
            "open": quote.get("open_price"),
            "high": quote.get("high_price"),
            "low": quote.get("low_price"),
            "volume": quote.get("volume"),
            "amount": quote.get("amount"),
            "is_stale": quote.get("is_stale", False),
            "source": "remote_redis",
        }

        try:
            encoded = json.dumps(payload, ensure_ascii=False)
            async with client.pipeline(transaction=False) as pipe:
                pipe.zadd(series_key, {encoded: ts})
                # 保留最近 max_points 条，删除更旧数据
                pipe.zremrangebyrank(series_key, 0, -(max_points + 1))
                pipe.expire(series_key, ttl_seconds)
                await pipe.execute()
            return True
        except Exception as e:
            logger.error(f"[RemoteRedis] 写入时序数据失败 {symbol}: {e}")
            return False

    async def fetch_symbols(self, market: str | None = None) -> list[dict[str, Any]]:
        """扫描远程 Redis 返回全部股票代码列表（优先新规范 key）"""
        try:
            client = self._get_client()
            pattern = (
                f"market:snapshot:{market.lower()}*" if market else "market:snapshot:*"
            )
            keys: list[str] = []
            cursor = 0
            while True:
                cursor, batch = await client.scan(cursor, match=pattern, count=500)
                keys.extend(batch)
                if cursor == 0:
                    break

            # 兼容旧 key，避免迁移窗口丢标的
            if not keys:
                legacy_pattern = f"stock:*.{market.upper()}" if market else "stock:*"
                cursor = 0
                while True:
                    cursor, batch = await client.scan(
                        cursor, match=legacy_pattern, count=500
                    )
                    keys.extend(batch)
                    if cursor == 0:
                        break

            result = []
            for key in keys:
                if key.startswith("market:snapshot:"):
                    snap_symbol = key.removeprefix("market:snapshot:")
                    if len(snap_symbol) < 3:
                        continue
                    prefix = snap_symbol[:2].upper()
                    code = snap_symbol[2:]
                    mapped_market = (
                        "SH" if prefix == "SH" else ("SZ" if prefix == "SZ" else "BJ")
                    )
                    result.append(
                        {
                            "symbol": f"{code}.{mapped_market}",
                            "code": code,
                            "market": mapped_market,
                        }
                    )
                elif key.startswith("stock:"):
                    code_market = key.removeprefix("stock:")
                    parts = code_market.split(".")
                    if len(parts) == 2:
                        result.append(
                            {
                                "symbol": code_market,
                                "code": parts[0],
                                "market": parts[1],
                            }
                        )
            return result
        except Exception as e:
            logger.error(f"[RemoteRedis] fetch_symbols 失败: {e}")
            return []

    async def close(self) -> None:
        """关闭 Redis 连接"""
        if self._client:
            await self._client.aclose()
            self._client = None
