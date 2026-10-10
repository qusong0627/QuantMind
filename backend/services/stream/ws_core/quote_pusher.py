#!/usr/bin/env python3
"""
实时行情数据推送器
Updated: 2026-02-19 - 接入远程 Redis 行情快照数据源
Updated: 2026-10-10 - T4-4（审计 H14）：移除 market:series 伪实时回写；
                      push 载荷带 data_source 来源标注
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any
from collections.abc import Iterable

from backend.services.stream.market_app.database import AsyncSessionLocal
from backend.services.stream.market_app.market_config import settings
from backend.services.stream.market_app.models import Quote
from backend.services.stream.market_app.services.remote_redis_source import (
    RemoteRedisDataSource,
)
from backend.services.stream.market_app.services.quantdb_source import (
    QuantDBDataSource,
)

from .manager import manager

logger = logging.getLogger(__name__)

# 全局数据源实例（延迟初始化）
_remote_redis_source: RemoteRedisDataSource | None = None
_quantdb_source: QuantDBDataSource | None = None


def get_remote_redis_source() -> RemoteRedisDataSource:
    global _remote_redis_source
    if _remote_redis_source is None:
        _remote_redis_source = RemoteRedisDataSource()
    return _remote_redis_source


def get_quantdb_source() -> QuantDBDataSource:
    global _quantdb_source
    if _quantdb_source is None:
        _quantdb_source = QuantDBDataSource()
    return _quantdb_source


def _as_utc_aware(dt: datetime | None) -> datetime:
    if dt is None:
        return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class QuotePusher:
    """实时行情推送器

    负责推送实时股票行情数据到订阅的客户端
    """

    def __init__(self):
        """初始化推送器"""
        self.running = False
        self.subscribed_stocks: set[str] = set()  # 存储所有正在被订阅的代码
        self.push_task: asyncio.Task | None = None  # 中心化推送任务
        self.push_interval = 2.0  # 全局拉取间隔（秒）
        self.cache: dict[str, dict[str, Any]] = {}  # 行情缓存
        self.persist_to_db = True
        self.warmup_symbols: set[str] = {
            s.strip() for s in (settings.STREAM_WARMUP_SYMBOLS or "").split(",") if s.strip()
        }
        logger.info("实时行情推送器初始化")

    async def start(self):
        """启动推送器"""
        if self.running:
            return
        self.running = True
        self.push_task = asyncio.create_task(self._centralized_push_loop())
        logger.info("实时行情推送器启动")

    async def stop(self):
        """停止推送器"""
        self.running = False
        if self.push_task:
            self.push_task.cancel()
            try:
                await self.push_task
            except asyncio.CancelledError:
                pass
        self.push_task = None
        logger.info("实时行情推送器停止")

    async def subscribe_quote(self, stock_code: str):
        """订阅股票行情"""
        self.subscribed_stocks.add(stock_code)
        logger.info(f"订阅列表增加: {stock_code}, 当前共 {len(self.subscribed_stocks)} 只")

    async def unsubscribe_quote(self, stock_code: str):
        """取消订阅股票行情"""
        if stock_code in self.subscribed_stocks:
            self.subscribed_stocks.remove(stock_code)
            logger.info(f"订阅列表移除: {stock_code}")

    async def reconcile_subscriptions(self, topics: Iterable[str]):
        """根据连接管理器中的主题重算股票订阅集合。"""
        stock_topics = {
            topic.split("stock.", 1)[1] for topic in topics if isinstance(topic, str) and topic.startswith("stock.")
        }
        if stock_topics != self.subscribed_stocks:
            self.subscribed_stocks = stock_topics
            logger.info("订阅列表已重算: %d 只股票", len(self.subscribed_stocks))

    async def _centralized_push_loop(self):
        """
        中心化行情推送循环
        一次性抓取所有被订阅的代码，降低 Redis IO 压力
        优先 RemoteRedis 实时快照，缺失时回退 QuantDB 日线兜底
        """
        while self.running:
            try:
                await self._push_once()
                # 等待下次全量拉取
                await asyncio.sleep(self.push_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"中心化推送循环错误: {e}")
                await asyncio.sleep(2.0)

    async def _push_once(self) -> None:
        """单轮：拉取 → 兜底 → 落库 → 推送（由循环周期调用；测试可直达单轮）。

        T4-4（审计 H14）：QuantDB 日线兜底只落库、只推送（data_source 显式标注），
        **不再回写 market:series**——此前把「当日零点时间戳 + is_stale 恒 False」
        的日线当盘中时序点写进行情 Redis 市场键（伪实时，全链零提示）。
        market:series 的唯一写侧 = 行情席位（tdx_hot_set_feed / qmt_quote_backup /
        tdx_aidata）；本服务只是消费方。
        """
        redis_source = get_remote_redis_source()
        quantdb_source = get_quantdb_source()

        # 无订阅时仍拉取一小组保活标的，维持行情拉取与落库心跳
        if not self.subscribed_stocks and not self.warmup_symbols:
            return

        # 1. 批量抓取行情（优先 Redis 实时快照）
        stock_list = list(self.subscribed_stocks) if self.subscribed_stocks else list(self.warmup_symbols)
        results = await redis_source.fetch_quotes(stock_list)

        # 2. Redis 未覆盖的标的，用 QuantDB 本地日线兜底补充（仅落库/推送，不回写市场键）
        fetched_symbols = {r["symbol"] for r in results}
        missing = [s for s in stock_list if s not in fetched_symbols]
        if missing:
            try:
                qdb_results = await quantdb_source.fetch_quotes(missing)
                results.extend(qdb_results)
                logger.debug(f"[quantdb] 补充 {len(qdb_results)}/{len(missing)} 只行情")
            except Exception as e:
                logger.warning(f"[quantdb] 兜底补充行情失败: {e}")

        if self.persist_to_db and results:
            await self._persist_quotes(results)
            await self._report_persist_stats(len(results))

        # 3. 分发数据
        for quote in results:
            stock_code = quote["symbol"]
            topic = f"stock.{stock_code}"

            # 转化为推送协议格式（带 data_source 来源标注：席位/日线兜底可辨识）
            push_data = {
                "stock_code": stock_code,
                "price": quote["current_price"],
                "open": quote.get("open_price"),
                "high": quote.get("high_price"),
                "low": quote.get("low_price"),
                "volume": quote.get("volume"),
                "amount": quote.get("amount"),
                "is_stale": quote.get("is_stale", False),
                "data_source": quote.get("data_source") or "remote_redis",
                "timestamp": (
                    quote["timestamp"].isoformat()
                    if isinstance(quote["timestamp"], datetime)
                    else quote["timestamp"]
                ),
            }

            # 3. 检查是否有变化并推送
            if self._has_quote_changed(stock_code, push_data):
                message = {
                    "type": "quote",
                    "stock_code": stock_code,
                    "data": push_data,
                    "timestamp": time.time(),
                }

                count = await manager.publish(topic, message)
                if count > 0:
                    logger.debug(f"推送行情 {stock_code} 到 {count} 个客户端")

                self.cache[stock_code] = push_data

    async def _persist_quotes(self, quotes: list[dict[str, Any]]) -> None:
        """将 WS 推送使用的同一批行情落库到 quotes 表。"""
        rows: list[Quote] = []
        for quote in quotes:
            symbol = quote.get("symbol")
            current_price = quote.get("current_price")
            if not symbol or current_price is None:
                continue
            rows.append(
                Quote(
                    symbol=str(symbol),
                    timestamp=(
                        _as_utc_aware(quote.get("timestamp"))
                        if isinstance(quote.get("timestamp"), datetime)
                        else datetime.now(timezone.utc)
                    ),
                    open_price=quote.get("open_price"),
                    high_price=quote.get("high_price"),
                    low_price=quote.get("low_price"),
                    close_price=quote.get("close_price"),
                    current_price=current_price,
                    volume=int(quote.get("volume") or 0),
                    amount=quote.get("amount"),
                    data_source=quote.get("data_source", "remote_redis"),
                )
            )

        if not rows:
            return

        try:
            async with AsyncSessionLocal() as session:
                session.add_all(rows)
                await session.commit()
        except Exception as e:
            logger.error(f"行情落库失败: {e}")

    async def _report_persist_stats(self, count: int) -> None:
        """更新行情落库速率统计（供模拟盘 preflight 检查行情落库状态）。"""
        try:
            redis_client = get_remote_redis_source()._get_client()
            key = "market:stream:persist_stats"
            now_ts = time.time()
            prev_raw = await redis_client.get(key)
            prev = {}
            if prev_raw:
                try:
                    prev = json.loads(prev_raw)
                except Exception:
                    prev = {}
            prev_ts = float(prev.get("ts", 0) or 0)
            prev_count = int(prev.get("quotes", 0) or 0)
            elapsed = max(1.0, now_ts - prev_ts)
            rps = (count / elapsed) if elapsed > 0 else 0.0
            await redis_client.set(
                key,
                json.dumps(
                    {
                        "quotes_per_sec": round(rps, 2),
                        "quotes": prev_count + count,
                        "ts": now_ts,
                        "window": round(elapsed, 1),
                    }
                ),
                ex=1800,
            )
        except Exception as e:
            logger.warning(f"更新行情落库统计失败: {e}")

    def _has_quote_changed(self, stock_code: str, new_data: dict[str, Any]) -> bool:
        """
        检查行情是否有变化

        Args:
            stock_code: 股票代码
            new_data: 新行情数据

        Returns:
            是否有变化
        """
        if stock_code not in self.cache:
            return True

        old_data = self.cache[stock_code]

        # 比较价格是否变化
        return old_data.get("price") != new_data.get("price")

    async def push_kline(self, stock_code: str, period: str = "1min"):
        """
        推送K线数据

        Args:
            stock_code: 股票代码
            period: K线周期
        """
        topic = f"kline.{stock_code}.{period}"

        # TODO: 获取K线数据
        kline_data = await self._fetch_kline(stock_code, period)

        if kline_data:
            message = {
                "type": "kline",
                "stock_code": stock_code,
                "period": period,
                "data": kline_data,
                "timestamp": time.time(),
            }

            await manager.publish(topic, message)

    async def _fetch_kline(self, stock_code: str, period: str) -> dict[str, Any] | None:
        """
        获取K线数据

        Args:
            stock_code: 股票代码
            period: K线周期

        Returns:
            K线数据
        """
        # TODO: 实现K线数据获取
        return {
            "open": 100.0,
            "high": 102.0,
            "low": 99.0,
            "close": 101.0,
            "volume": 10000,
            "timestamp": datetime.now().isoformat(),
        }

    def get_stats(self) -> dict[str, Any]:
        """
        获取推送统计

        Returns:
            统计信息
        """
        return {
            "running": self.running,
            "active_pushers": 1 if self.push_task and not self.push_task.done() else 0,
            "subscribed_stocks": len(self.subscribed_stocks),
            "cached_stocks": len(self.cache),
            "push_interval": self.push_interval,
        }


# 全局推送器实例
quote_pusher = QuotePusher()
