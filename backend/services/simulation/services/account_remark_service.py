"""模拟账户盘中重估（mark-to-market）worker。

背景：Redis 模拟账户的 market_value/total_asset 只在成交瞬间（Lua）更新，
盘中无成交时资金概览等页面读到的是冻结旧值。本 worker 在 A 股交易时段内
周期性用远端 Redis 行情序列（market:series:*，与撮合同源）重估持仓现价与
市值，只改 price/market_value/total_asset，不碰现金/成本/可卖量。

PG 为主、Redis 为缓存：重估失败仅记日志，下轮重试。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from backend.services.trade_shared.redis_client import RedisClient

logger = logging.getLogger(__name__)

_SH_TZ = ZoneInfo("Asia/Shanghai")


def _in_trading_session(now: datetime | None = None) -> bool:
    """A 股交易时段门控（周一至周五 09:00–15:35，含集合竞价；其余时间跳过省资源）。"""
    now = now or datetime.now(_SH_TZ)
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    return 9 * 60 <= minutes <= 15 * 60 + 35


class SimulationRemarkWorker:
    """周期性重估模拟账户持仓市值。"""

    def __init__(self, redis: RedisClient, interval_seconds: int = 30):
        self.redis = redis
        self.interval_seconds = max(10, int(interval_seconds))
        self._stopped = asyncio.Event()
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stopped.clear()
        self._task = asyncio.create_task(self._run(), name="sim-remark-worker")

    async def stop(self) -> None:
        self._stopped.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def remark_once(self) -> int:
        """重估全部模拟账户，返回有变动的账户数。"""
        from backend.services.simulation.services.redis_series_quote import (
            fetch_series_tick,
        )
        from backend.shared.stock_utils import StockCodeUtil

        if not self.redis.client:
            return 0
        try:
            keys = list(
                self.redis.client.scan_iter(match="simulation:account:*", count=500)
            )
        except Exception as exc:
            logger.warning("Simulation remark scan failed: %s", exc)
            return 0

        changed = 0
        for key in keys:
            try:
                raw = self.redis.client.get(str(key))
                if not raw:
                    continue
                account = json.loads(raw)
                if not isinstance(account, dict):
                    continue
                positions = account.get("positions")
                if not isinstance(positions, dict) or not positions:
                    continue
                dirty = False
                total_mv = 0.0
                for pos_key, pos in positions.items():
                    if not isinstance(pos, dict):
                        continue
                    vol = float(pos.get("volume") or 0)
                    if vol <= 0:
                        continue
                    code = str(pos_key).split("::", 1)[0]
                    tick = await fetch_series_tick(code)
                    if not tick:
                        total_mv += float(pos.get("market_value") or 0)
                        continue
                    px = round(float(tick["price"]), 4)
                    mv = round(vol * px, 2)
                    if abs(mv - float(pos.get("market_value") or 0)) > 1e-9 or abs(
                        px - float(pos.get("price") or 0)
                    ) > 1e-9:
                        pos["price"] = px
                        pos["market_value"] = mv
                        dirty = True
                    total_mv += mv
                if not dirty:
                    continue
                total_mv = round(total_mv, 2)
                account["market_value"] = total_mv
                account["total_asset"] = round(
                    float(account.get("cash") or 0)
                    + total_mv
                    - float(account.get("short_market_value") or 0),
                    2,
                )
                self.redis.client.set(str(key), json.dumps(account, ensure_ascii=False))
                changed += 1
            except Exception as exc:
                logger.debug("Simulation remark skipped %s: %s", key, exc)
                continue
        return changed

    async def _run(self) -> None:
        while not self._stopped.is_set():
            try:
                if _in_trading_session():
                    changed = await self.remark_once()
                    if changed:
                        logger.info("Simulation remark updated %d account(s)", changed)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Simulation remark worker failed: %s", exc)
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=self.interval_seconds)
            except asyncio.TimeoutError:
                continue


def remark_enabled() -> bool:
    return os.getenv("SIM_REMARK_ENABLED", "true").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def remark_interval_seconds() -> int:
    try:
        return max(10, int(os.getenv("SIM_REMARK_INTERVAL_SECONDS", "30")))
    except (TypeError, ValueError):
        return 30
