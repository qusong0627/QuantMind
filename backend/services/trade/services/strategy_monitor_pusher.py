"""策略监控推送源：把模拟盘账户的实时盈亏写入 Redis Stream `strategy_events`。

为什么需要它
------------
仪表盘「策略监控」在 WebSocket 连上后会关掉轮询（`useStrategies.ts` 中
`realtimeStatus === 'connected'` 时不注册 `refreshOrchestrator`），只依赖 WS 推送；
而后端此前没有任何 producer 往 `strategy.*` 主题发消息，卡片挂载后永不刷新。

本 worker 周期性扫描模拟账户（其 `price/market_value/total_asset` 已被
`account_remark_service` 按实时价重估），算出今日/累计盈亏后 XADD 到
`strategy_events`，由 stream 服务的 `StrategyPusher` 转成 WS 消息推给
`strategy.{user_id}` 订阅者。前端收到后重新拉取策略列表。

只推送「有变化」的账户，避免每轮刷屏；PG 为主、Redis 为缓存，失败仅记日志。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from decimal import Decimal
from typing import Any

from backend.services.simulation.services.fund_snapshot_service import (
    SimulationFundSnapshotService,
)
from backend.services.trade_shared.redis_client import RedisClient
from backend.services.trade_shared.simulation_manager import (
    SimulationAccountManager,
    resolve_sim_subs,
)
from backend.shared.simulation_account_keys import parse_account_key

logger = logging.getLogger(__name__)

STRATEGY_EVENTS_STREAM = "strategy_events"

DEFAULT_INITIAL_CASH = 1_000_000.0
COOLDOWN_DAYS = 30


def push_enabled() -> bool:
    return os.getenv("SIM_STRATEGY_PUSH_ENABLED", "true").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def push_interval_seconds() -> int:
    try:
        return max(5, int(os.getenv("SIM_STRATEGY_PUSH_INTERVAL_SECONDS", "30")))
    except (TypeError, ValueError):
        return 30


def _fingerprint(payload: dict[str, Any]) -> str:
    """变化判定指纹：只比较会驱动前端重绘的字段。"""
    keys = ("total_asset", "today_pnl", "today_return", "total_pnl", "total_return")
    return "|".join(f"{k}={payload.get(k)}" for k in keys)


class StrategyMonitorPusher:
    """周期性把模拟盘盈亏写进 strategy_events 流。"""

    def __init__(self, redis: RedisClient, interval_seconds: int = 30):
        self.redis = redis
        self.interval_seconds = max(5, int(interval_seconds))
        self._stopped = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._last_fingerprints: dict[str, str] = {}

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stopped.clear()
        self._task = asyncio.create_task(
            self._run(), name="sim-strategy-monitor-pusher"
        )

    async def stop(self) -> None:
        self._stopped.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def push_once(self) -> int:
        """扫描并推送一次，返回实际推送的账户数。"""
        if not self.redis.client:
            return 0
        try:
            keys = list(
                self.redis.client.scan_iter(match="simulation:account:*", count=500)
            )
        except Exception as exc:
            logger.warning("Strategy monitor push scan failed: %s", exc)
            return 0

        manager = SimulationAccountManager(self.redis)
        pushed = 0
        for key in keys:
            try:
                key_str = key.decode() if isinstance(key, bytes) else str(key)
                parsed = parse_account_key(key_str)
                if not parsed:
                    continue
                tenant, user, market = parsed
                sim_uid = int(user) if str(user).isdigit() else 0

                raw = self.redis.client.get(key_str)
                if not raw:
                    continue
                account = json.loads(raw)
                if not isinstance(account, dict):
                    continue
                total_asset = float(account.get("total_asset") or 0.0)

                settings = await manager.get_settings(
                    user_id=sim_uid,
                    tenant_id=tenant,
                    default_initial_cash=DEFAULT_INITIAL_CASH,
                    cooldown_days=COOLDOWN_DAYS,
                )
                initial_capital = Decimal(
                    str(settings.get("initial_cash", DEFAULT_INITIAL_CASH))
                )
                baselines = await SimulationFundSnapshotService.get_baselines(
                    tenant_id=tenant,
                    user_id=str(user),
                    initial_capital=initial_capital,
                )
                day_open = float(baselines.get("day_open_equity") or initial_capital)
                base = float(initial_capital)

                today_pnl = round(total_asset - day_open, 2)
                total_pnl = round(total_asset - base, 2)
                today_return = round(today_pnl / day_open * 100.0, 4) if day_open else 0.0
                total_return = round(total_pnl / base * 100.0, 4) if base else 0.0

                payload = {
                    "tenant_id": tenant,
                    "sim_user_id": str(user),
                    "market": market,
                    "total_asset": round(total_asset, 2),
                    "today_pnl": today_pnl,
                    "today_return": today_return,
                    "total_pnl": total_pnl,
                    "total_return": total_return,
                }

                topics = [str(user)]
                topics.extend(resolve_sim_subs(sim_uid, tenant))

                # 指纹要把目标主题算进去：submap 是前端首次调用模拟盘接口后才
                # 建立的，若只比对盈亏，新出现的 strategy.{sub} 主题会因为
                # 「数据没变」而永远收不到第一条推送。
                fp = _fingerprint(payload) + "|topics=" + ",".join(topics)
                if self._last_fingerprints.get(key_str) == fp:
                    continue

                for topic_user in dict.fromkeys(topics):
                    if not topic_user:
                        continue
                    event = dict(payload)
                    event["user_id"] = topic_user
                    self.redis.publish_event(STRATEGY_EVENTS_STREAM, event)
                self._last_fingerprints[key_str] = fp
                pushed += 1
            except Exception as exc:
                logger.debug("Strategy monitor push skipped %s: %s", key, exc)
                continue
        return pushed

    async def _run(self) -> None:
        while not self._stopped.is_set():
            try:
                pushed = await self.push_once()
                if pushed:
                    logger.debug(
                        "Strategy monitor push updated %d account(s)", pushed
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Strategy monitor push failed: %s", exc)
            try:
                await asyncio.wait_for(
                    self._stopped.wait(), timeout=self.interval_seconds
                )
            except asyncio.TimeoutError:
                continue
