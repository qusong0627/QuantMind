#!/usr/bin/env python3
"""
缓存预热模块
Week 4 Day 3 - 自动预热热点数据

功能:
1. 启动时预热
2. 定时预热
3. 预热进度监控
4. 可配置预热数据集
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional
from collections.abc import Callable

logger = logging.getLogger(__name__)


@dataclass
class WarmupTask:
    """预热任务"""

    name: str
    description: str
    fetch_func: Callable
    keys: list[str]
    ttl: int = 3600
    priority: int = 1  # 1-5, 5最高
    enabled: bool = True


class CacheWarmupManager:
    """缓存预热管理器"""

    def __init__(self, cache_manager):
        """
        Args:
            cache_manager: 缓存管理器实例
        """
        self.cache_manager = cache_manager
        self.tasks: list[WarmupTask] = []
        self.stats = {
            "total_tasks": 0,
            "completed_tasks": 0,
            "failed_tasks": 0,
            "total_keys": 0,
            "warmed_keys": 0,
            "failed_keys": 0,
            "start_time": None,
            "end_time": None,
            "duration_seconds": 0,
        }

    def register_task(self, task: WarmupTask):
        """注册预热任务"""
        self.tasks.append(task)
        logger.info(f"✅ 注册预热任务: {task.name} ({len(task.keys)}个键)")

    async def warmup_all(self, parallel: bool = True) -> dict[str, Any]:
        """执行所有预热任务

        Args:
            parallel: 是否并行执行

        Returns:
            预热统计信息
        """
        self.stats["start_time"] = datetime.now()
        self.stats["total_tasks"] = len(self.tasks)
        self.stats["total_keys"] = sum(len(task.keys) for task in self.tasks)

        logger.info(f"🔥 开始缓存预热: {self.stats['total_tasks']}个任务, {self.stats['total_keys']}个键")

        # 按优先级排序
        sorted_tasks = sorted(self.tasks, key=lambda t: t.priority, reverse=True)

        if parallel:
            # 并行执行
            tasks = [self._warmup_task(task) for task in sorted_tasks if task.enabled]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for result in results:
                if isinstance(result, Exception):
                    logger.error(f"预热任务失败: {result}")
                    self.stats["failed_tasks"] += 1
        else:
            # 串行执行
            for task in sorted_tasks:
                if not task.enabled:
                    continue
                try:
                    await self._warmup_task(task)
                except Exception as e:
                    logger.error(f"预热任务 {task.name} 失败: {e}")
                    self.stats["failed_tasks"] += 1

        self.stats["end_time"] = datetime.now()
        self.stats["duration_seconds"] = (self.stats["end_time"] - self.stats["start_time"]).total_seconds()

        logger.info(
            f"✅ 缓存预热完成: 成功 {self.stats['completed_tasks']}/{self.stats['total_tasks']} 任务, "
            f"预热 {self.stats['warmed_keys']}/{self.stats['total_keys']} 键, "
            f"耗时 {self.stats['duration_seconds']:.2f}秒"
        )

        return self.stats

    async def _warmup_task(self, task: WarmupTask):
        """执行单个预热任务"""
        logger.info(f"🔄 执行预热任务: {task.name}")

        success_count = 0
        failed_count = 0

        for key in task.keys:
            try:
                # 检查是否已缓存
                cached = self.cache_manager.get(key)
                if cached is not None:
                    logger.debug(f"  ✓ 已缓存: {key}")
                    success_count += 1
                    continue

                # 获取数据
                data = await self._fetch_data(task.fetch_func, key)

                if data is not None:
                    # 写入缓存
                    self.cache_manager.set(key, data, ttl=task.ttl)
                    logger.debug(f"  ✓ 预热成功: {key}")
                    success_count += 1
                else:
                    logger.warning(f"  ✗ 数据为空: {key}")
                    failed_count += 1

            except Exception as e:
                logger.error(f"  ✗ 预热失败 {key}: {e}")
                failed_count += 1

        self.stats["completed_tasks"] += 1
        self.stats["warmed_keys"] += success_count
        self.stats["failed_keys"] += failed_count

        logger.info(f"✅ 任务 {task.name} 完成: 成功 {success_count}, 失败 {failed_count}")

    async def _fetch_data(self, fetch_func: Callable, key: str) -> Any | None:
        """获取数据（支持同步和异步函数）"""
        if asyncio.iscoroutinefunction(fetch_func):
            return await fetch_func(key)
        else:
            return fetch_func(key)


class QuantMindWarmup:
    """QuantMind项目缓存预热配置"""

    def __init__(self, cache_manager):
        self.cache_manager = cache_manager
        self.warmup_manager = CacheWarmupManager(cache_manager)
        self._register_tasks()

    def _register_tasks(self):
        """注册预热任务"""
        # 1. 热门股票数据
        self.warmup_manager.register_task(
            WarmupTask(
                name="popular_stocks",
                description="预热热门股票数据",
                fetch_func=self._fetch_stock_data,
                keys=[
                    "stock:SH600000",  # 浦发银行
                    "stock:SH600519",  # 贵州茅台
                    "stock:SZ000001",  # 平安银行
                    "stock:SZ000002",  # 万科A
                    "stock:SZ000858",  # 五粮液
                    "stock:SH601318",  # 中国平安
                    "stock:SH601398",  # 工商银行
                    "stock:SH601939",  # 建设银行
                ],
                ttl=600,  # 10分钟
                priority=5,
            )
        )

        # 2. 市场指数数据
        self.warmup_manager.register_task(
            WarmupTask(
                name="market_indices",
                description="预热市场指数数据",
                fetch_func=self._fetch_index_data,
                keys=[
                    "index:SH000001",  # 上证指数
                    "index:SZ399001",  # 深证成指
                    "index:SZ399006",  # 创业板指
                    "index:SH000300",  # 沪深300
                    "index:SH000016",  # 上证50
                    "index:SH000905",  # 中证500
                ],
                ttl=300,  # 5分钟
                priority=5,
            )
        )

        # 3. 策略模板
        self.warmup_manager.register_task(
            WarmupTask(
                name="strategy_templates",
                description="预热策略模板",
                fetch_func=self._fetch_strategy_template,
                keys=[
                    "template:ma_cross",
                    "template:rsi_strategy",
                    "template:macd_strategy",
                    "template:bollinger_bands",
                    "template:kdj_strategy",
                ],
                ttl=86400,  # 24小时
                priority=3,
            )
        )

        # 4. 用户配置
        self.warmup_manager.register_task(
            WarmupTask(
                name="user_configs",
                description="预热用户配置",
                fetch_func=self._fetch_user_config,
                keys=[
                    "config:default_risk",
                    "config:trading_rules",
                    "config:market_hours",
                ],
                ttl=3600,  # 1小时
                priority=4,
            )
        )

        # 5. 行业分类数据
        self.warmup_manager.register_task(
            WarmupTask(
                name="industry_data",
                description="预热行业分类数据",
                fetch_func=self._fetch_industry_data,
                keys=[
                    "industry:banks",
                    "industry:technology",
                    "industry:healthcare",
                    "industry:consumer",
                    "industry:real_estate",
                ],
                ttl=7200,  # 2小时
                priority=2,
            )
        )

    def _fetch_stock_data(self, key: str) -> dict | None:
        """获取股票数据（模拟）"""
        symbol = key.split(":", 1)[1]
        # 实际应该调用真实的数据获取函数
        return {
            "symbol": symbol,
            "name": f"股票{symbol}",
            "price": 100.0,
            "change": 1.5,
            "volume": 1000000,
            "timestamp": datetime.now().isoformat(),
        }

    def _fetch_index_data(self, key: str) -> dict | None:
        """获取指数数据（模拟）"""
        index_code = key.split(":", 1)[1]
        return {
            "code": index_code,
            "name": f"指数{index_code}",
            "value": 3000.0,
            "change": 0.5,
            "timestamp": datetime.now().isoformat(),
        }

    def _fetch_strategy_template(self, key: str) -> dict | None:
        """获取策略模板（模拟）"""
        template_id = key.split(":", 1)[1]

        templates = {
            "ma_cross": {
                "id": "ma_cross",
                "name": "双均线策略",
                "description": "短期均线上穿长期均线时买入，下穿时卖出",
                "parameters": ["short_period", "long_period"],
            },
            "rsi_strategy": {
                "id": "rsi_strategy",
                "name": "RSI策略",
                "description": "RSI超买超卖策略",
                "parameters": ["period", "overbought", "oversold"],
            },
            "macd_strategy": {
                "id": "macd_strategy",
                "name": "MACD策略",
                "description": "MACD金叉死叉策略",
                "parameters": ["fast", "slow", "signal"],
            },
        }

        return templates.get(template_id)

    def _fetch_user_config(self, key: str) -> dict | None:
        """获取用户配置（模拟）"""
        config_type = key.split(":", 1)[1]

        configs = {
            "default_risk": {
                "max_position": 0.2,
                "stop_loss": 0.05,
                "take_profit": 0.1,
            },
            "trading_rules": {
                "max_daily_trades": 10,
                "min_trade_amount": 1000,
                "max_trade_amount": 100000,
            },
            "market_hours": {
                "morning_start": "09:30",
                "morning_end": "11:30",
                "afternoon_start": "13:00",
                "afternoon_end": "15:00",
            },
        }

        return configs.get(config_type)

    def _fetch_industry_data(self, key: str) -> dict | None:
        """获取行业数据（模拟）"""
        industry = key.split(":", 1)[1]
        return {
            "industry": industry,
            "name": f"{industry}行业",
            "stock_count": 100,
            "avg_pe": 15.5,
            "top_stocks": ["SH600000", "SH600519", "SZ000001"],
        }

    async def run_warmup(self, parallel: bool = True) -> dict[str, Any]:
        """执行预热"""
        return await self.warmup_manager.warmup_all(parallel=parallel)


# 快速启动函数
async def warmup_cache(cache_manager, parallel: bool = True) -> dict[str, Any]:
    """快速预热缓存

    用法:
        from backend.shared.cache_enhanced import get_cache_manager
        from backend.shared.cache_warmup import warmup_cache

        cache_manager = get_cache_manager()
        stats = await warmup_cache(cache_manager)
        print(f"预热完成: {stats}")
    """
    warmup = QuantMindWarmup(cache_manager)
    return await warmup.run_warmup(parallel=parallel)
