"""QMT 执行端成交回收轮询器。

大 QMT 没有成交推送（big-convert RPC 只提供查询），因此必须轮询
``query_orders`` / ``query_trades``，把状态变化与成交经共享内核
（``qmt_exec_reconciler``）落到 ``orders`` / ``trades`` 表 —— 与 HTTP 桥上报
（``/bridge/execution``）走同一套匹配与落库逻辑，避免两条回报通道口径漂移。

设计要点
--------
* **只认自己的单**：仅处理备注以 ``qm`` 开头（或 strategy_name 等于本策略）的
  委托/成交，账户里的手工单、其他策略单一律不碰。
* **增量成交**：委托回报给的是**累计**成交量，成交回报给的是**单笔**成交量。
  优先用 ``query_trades`` 的明细累加；若委托已成交却查不到任何成交明细
  （跨日清理、接口缺数据），按「该订单在库内无成交行」为前提补一条**合成成交**，
  合成 ``exchange_trade_id`` 稳定（``qmt-synth-<委托键>``）→ 重复轮询不会重复入账；
  真实明细随后到达时**就地升级**该行为真实成交行，不新增行（否则同一笔成交双计）。
* **状态去重**：``(状态, 累计成交量)`` 未变化则跳过，减少无谓写库。
* **日切**：QMT 每日重置委托编号，跨自然日清空去重缓存。
* **异常隔离**：单轮失败只记日志，不退出循环；未启用/未配置时低频空转，
  不给桥 Redis 与数据库添压力。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from backend.services.live_trading.services.qmt_exec_client import (
    QmtExecClient,
    QmtExecError,
    get_qmt_exec_client,
    is_qmt_exec_remark,
    mask_account_id,
)
from backend.services.live_trading.services.qmt_exec_reconciler import (
    apply_execution_report,
    publish_order_event,
    resolve_order,
)
from backend.services.live_trading.services.trading_session import (
    is_trading_time,
    trade_date_str,
)

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 2.0  # 交易时段轮询间隔
OFF_HOURS_SLEEP_SECONDS = 30.0  # 非交易时段（只需低频确认开盘）
DISABLED_SLEEP_SECONDS = 60.0  # 未启用/未配置时空转间隔
SETTINGS_REFRESH_SECONDS = 30.0  # 页面配置重读间隔

_SYNTH_TRADE_PREFIX = "qmt-synth-"


class QmtExecPoller:
    """轮询 QMT 委托/成交 → 复用回报内核落库。"""

    def __init__(
        self,
        client: QmtExecClient | None = None,
        *,
        interval: float = POLL_INTERVAL_SECONDS,
        redis_factory: Any = None,
    ):
        self._client = client
        self._interval = max(0.5, float(interval))
        self._redis_factory = redis_factory
        self._redis: Any = None
        # 去重缓存：委托键 → (状态, 累计成交量)；成交 id 集合
        self._seen_orders: dict[str, tuple[str, float]] = {}
        self._seen_trades: set[str] = set()
        self._day = ""
        self._settings_at = 0.0
        self.stats: dict[str, int] = {
            "rounds": 0,
            "orders": 0,
            "trades": 0,
            "changed": 0,
            "unmatched": 0,
            "errors": 0,
        }

    # -- 依赖 -----------------------------------------------------------
    @property
    def client(self) -> QmtExecClient:
        return self._client or get_qmt_exec_client()

    def _redis_handle(self) -> Any:
        if self._redis is not None:
            return self._redis
        try:
            if self._redis_factory is not None:
                self._redis = self._redis_factory()
            else:
                from backend.services.trade_shared.redis_client import RedisClient

                client = RedisClient()
                client.connect()
                self._redis = client
        except Exception as exc:  # noqa: BLE001 - 事件推送失败不影响落库
            logger.warning("[QmtExecPoller] Redis 不可用，交易事件将不推送: %s", exc)
            self._redis = None
        return self._redis

    # -- 主循环 ---------------------------------------------------------
    async def run(self) -> None:
        client = self.client
        logger.info(
            "[QmtExecPoller] 启动 interval=%.1fs account=%s enabled=%s",
            self._interval,
            mask_account_id(client.account_id) or "(未配置)",
            client.configured,
        )
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                logger.info("[QmtExecPoller] 收到取消信号，退出轮询")
                raise
            except Exception as exc:  # noqa: BLE001 - 常驻任务不能因单轮失败退出
                self.stats["errors"] += 1
                logger.warning("[QmtExecPoller] 轮询失败: %s", exc, exc_info=True)
            await asyncio.sleep(self._next_sleep())

    def _next_sleep(self) -> float:
        if not self.client.configured:
            return DISABLED_SLEEP_SECONDS
        if not is_trading_time():
            return OFF_HOURS_SLEEP_SECONDS
        return self._interval

    # -- 单轮 -----------------------------------------------------------
    async def poll_once(self) -> dict[str, int]:
        """执行一轮回收。返回本轮计数（供测试/观测）。"""
        client = self.client
        await self._refresh_settings()
        if not client.configured:
            return {"skipped": 1}

        self._rollover_if_new_day()
        try:
            orders = await client.query_orders()
            trades = await client.query_trades()
        except QmtExecError as exc:
            self.stats["errors"] += 1
            logger.warning("[QmtExecPoller] 查询失败 code=%s: %s", exc.code, exc)
            return {"error": 1}

        cfg = client.effective_config()
        strategy_name = str(cfg.get("strategy_name") or "")
        self.stats["rounds"] += 1
        self.stats["orders"] += len(orders)
        self.stats["trades"] += len(trades)

        changed = 0
        async with self._session() as db:
            # 先落真实成交明细、再处理委托回报：反过来的话，委托回报会先补一条
            # 合成成交，同一轮稍后到达的成交明细又入账一次（同一笔成交双计）。
            touched = await self._sync_trades(db, trades, strategy_name=strategy_name)
            touched += await self._sync_orders(db, orders, strategy_name=strategy_name)
            if touched:
                await db.commit()
                redis = self._redis_handle()
                for order, status in touched:
                    publish_order_event(redis, order, status)
                changed = len(touched)
        self.stats["changed"] += changed
        return {"changed": changed, "orders": len(orders), "trades": len(trades)}

    @staticmethod
    def _session() -> Any:
        from backend.shared.database_manager_v2 import get_session

        return get_session()

    async def _refresh_settings(self) -> None:
        now = asyncio.get_running_loop().time()
        if now - self._settings_at < SETTINGS_REFRESH_SECONDS:
            return
        self._settings_at = now
        try:
            await self.client.refresh_settings()
        except Exception as exc:  # noqa: BLE001 - 配置读取失败沿用上一份
            logger.warning("[QmtExecPoller] 刷新页面配置失败: %s", exc)

    def _rollover_if_new_day(self) -> None:
        today = trade_date_str()
        if today != self._day:
            if self._day:
                logger.info(
                    "[QmtExecPoller] 日切 %s → %s，清空去重缓存", self._day, today
                )
            self._day = today
            self._seen_orders.clear()
            self._seen_trades.clear()

    # -- 委托 -----------------------------------------------------------
    async def _sync_orders(
        self, db: Any, orders: list[dict[str, Any]], *, strategy_name: str
    ) -> list[tuple[Any, Any]]:
        touched: list[tuple[Any, Any]] = []
        for item in orders:
            if not self._is_ours(item, strategy_name):
                continue
            key = self._order_key(item)
            status = str(item.get("status") or "")
            traded_volume = float(item.get("traded_volume") or 0)
            if self._seen_orders.get(key) == (status, traded_volume):
                continue

            order = await self._resolve(db, item)
            if order is None:
                self.stats["unmatched"] += 1
                logger.warning(
                    "[QmtExecPoller] 委托无法匹配订单 key=%s symbol=%s remark=%s",
                    key,
                    item.get("symbol"),
                    item.get("order_remark"),
                )
                continue

            prev_status = order.status
            new_status = await apply_execution_report(
                db,
                order=order,
                status_raw=status,
                filled_quantity=traded_volume,
                exchange_order_id=str(item.get("order_id") or ""),
                message=str(item.get("status_msg") or ""),
                report_symbol=str(item.get("symbol") or ""),
                report_side=str(item.get("side") or ""),
            )
            self._seen_orders[key] = (status, traded_volume)
            if new_status != prev_status:
                touched.append((order, new_status))
                logger.info(
                    "[QmtExecPoller] 委托状态 %s → %s order_id=%s symbol=%s 成交=%s/%s",
                    prev_status.value,
                    new_status.value,
                    order.order_id,
                    order.symbol,
                    order.filled_quantity,
                    order.quantity,
                )
            await self._maybe_synthesize_trade(db, item, order, touched)
        return touched

    async def _maybe_synthesize_trade(
        self,
        db: Any,
        item: dict[str, Any],
        order: Any,
        touched: list[tuple[Any, Any]],
    ) -> None:
        """委托已成交但无成交明细时补一条合成成交（库内无成交行才补）。"""
        traded_volume = float(item.get("traded_volume") or 0)
        if traded_volume <= 0 or str(item.get("status") or "") not in {
            "FILLED",
            "PARTIALLY_FILLED",
        }:
            return
        if float(getattr(order, "filled_quantity", 0.0) or 0.0) >= traded_volume:
            return
        if await self._has_trade(db, order):
            return
        price = float(item.get("traded_price") or 0) or float(
            getattr(order, "price", 0) or 0
        )
        prev_status = order.status
        new_status = await apply_execution_report(
            db,
            order=order,
            status_raw=str(item.get("status") or ""),
            filled_quantity=traded_volume,
            filled_price=price,
            exchange_order_id=str(item.get("order_id") or ""),
            exchange_trade_id=f"{_SYNTH_TRADE_PREFIX}{self._order_key(item)}",
            message="合成成交（query_trades 无明细）",
            report_symbol=str(item.get("symbol") or ""),
            report_side=str(item.get("side") or ""),
        )
        if new_status != prev_status:
            touched.append((order, new_status))
        logger.info(
            "[QmtExecPoller] 合成成交 order_id=%s symbol=%s qty=%s price=%s",
            order.order_id,
            order.symbol,
            traded_volume,
            price,
        )

    @staticmethod
    async def _has_trade(db: Any, order: Any) -> bool:
        from sqlalchemy import select

        from backend.services.trade_shared.models.trade import Trade

        result = await db.execute(
            select(Trade.trade_id).where(Trade.order_id == order.order_id).limit(1)
        )
        return result.scalar_one_or_none() is not None

    # -- 成交 -----------------------------------------------------------
    async def _sync_trades(
        self, db: Any, trades: list[dict[str, Any]], *, strategy_name: str
    ) -> list[tuple[Any, Any]]:
        touched: list[tuple[Any, Any]] = []
        for item in trades:
            if not self._is_ours(item, strategy_name):
                continue
            trade_id = self._trade_key(item)
            if not trade_id or trade_id in self._seen_trades:
                continue
            volume = float(item.get("traded_volume") or 0)
            if volume <= 0:
                continue
            order = await self._resolve(db, item)
            if order is None:
                self.stats["unmatched"] += 1
                logger.warning(
                    "[QmtExecPoller] 成交无法匹配订单 trade_id=%s symbol=%s remark=%s",
                    trade_id,
                    item.get("symbol"),
                    item.get("order_remark"),
                )
                continue
            prev_status = order.status
            price = float(item.get("traded_price") or 0)
            upgraded = await self._upgrade_synth_trade(
                db, order, trade_id=trade_id, volume=volume, price=price
            )
            new_status = await apply_execution_report(
                db,
                order=order,
                status_raw="PARTIALLY_FILLED",
                filled_quantity=volume,
                filled_price=price,
                exchange_order_id=str(item.get("order_id") or ""),
                exchange_trade_id=trade_id,
                message="成交回报",
                report_symbol=str(item.get("symbol") or ""),
                report_side=str(item.get("side") or ""),
            )
            self._seen_trades.add(trade_id)
            if new_status != prev_status:
                touched.append((order, new_status))
            logger.info(
                "[QmtExecPoller] 成交入账 trade_id=%s order_id=%s symbol=%s qty=%s price=%s 累计=%s%s",
                trade_id,
                order.order_id,
                order.symbol,
                volume,
                item.get("traded_price"),
                order.filled_quantity,
                "（替换合成成交）" if upgraded else "",
            )
        return touched

    async def _upgrade_synth_trade(
        self,
        db: Any,
        order: Any,
        *,
        trade_id: str,
        volume: float,
        price: float,
    ) -> bool:
        """把此前的合成成交行就地升级为真实成交行，避免同一笔成交双计。

        合成成交是「委托已成交但查不到成交明细」时的兜底；真实明细到达后若按常规
        插入，同一笔成交会留下两行（合成行与真实行 ``exchange_trade_id`` 不同，
        去重挡不住）→ 成交数量/金额双计。这里就地替换该行，并按差额校正订单累计。
        返回是否发生了替换。
        """
        from sqlalchemy import select

        from backend.services.trade_shared.models.trade import Trade

        result = await db.execute(
            select(Trade)
            .where(
                Trade.order_id == order.order_id,
                Trade.exchange_trade_id.like(f"{_SYNTH_TRADE_PREFIX}%"),
            )
            .limit(1)
        )
        row = result.scalars().first()
        if row is None:
            return False
        old_qty = float(row.quantity or 0.0)
        old_value = float(row.trade_value or 0.0)
        new_value = float(volume) * price if price else old_value
        order.filled_quantity = (
            float(getattr(order, "filled_quantity", 0.0) or 0.0) + float(volume) - old_qty
        )
        order.filled_value = (
            float(getattr(order, "filled_value", 0.0) or 0.0) + new_value - old_value
        )
        if order.filled_quantity > 0:
            order.average_price = order.filled_value / order.filled_quantity
        row.exchange_trade_id = trade_id
        row.quantity = float(volume)
        if price:
            row.price = price
            row.trade_value = new_value
        row.remarks = "成交回报（替换合成成交）"
        logger.info(
            "[QmtExecPoller] 合成成交升级为真实成交 order_id=%s synth_qty=%s "
            "real_qty=%s trade_id=%s",
            order.order_id,
            old_qty,
            volume,
            trade_id,
        )
        return True

    # -- 匹配 -----------------------------------------------------------
    @staticmethod
    def _is_ours(item: dict[str, Any], strategy_name: str) -> bool:
        if is_qmt_exec_remark(item.get("order_remark")):
            return True
        name = str(item.get("strategy_name") or "").strip()
        return bool(strategy_name) and name == strategy_name

    @staticmethod
    def _order_key(item: dict[str, Any]) -> str:
        for name in ("order_sysid", "order_id"):
            value = str(item.get(name) or "").strip()
            if value and value not in {"-1", "0"}:
                return value
        return f"{item.get('symbol')}:{item.get('side')}:{item.get('order_volume')}"

    @staticmethod
    def _trade_key(item: dict[str, Any]) -> str:
        """成交唯一键：优先券商成交号，缺失则用「委托键:量:价:时间」合成。"""
        for name in ("trade_id", "traded_id"):
            value = str(item.get(name) or "").strip()
            if value and value not in {"-1", "0"}:
                return value
        order_key = QmtExecPoller._order_key(item)
        volume = item.get("traded_volume")
        price = item.get("traded_price")
        traded_at = str(item.get("traded_at") or "")
        return f"{order_key}:{volume}:{price}:{traded_at}"

    async def _resolve(self, db: Any, item: dict[str, Any]) -> Any:
        """备注 → client_order_id → 委托编号 → 交易所编号，逐级匹配。"""
        client = self.client
        remark = str(item.get("order_remark") or "")
        cid = ""
        if remark:
            try:
                cid = await client.resolve_client_order_id(remark)
            except Exception as exc:  # noqa: BLE001 - 映射读取失败走兜底
                logger.warning(
                    "[QmtExecPoller] 备注映射查询失败 remark=%s: %s", remark, exc
                )
        candidates: list[dict[str, Any]] = []
        if cid:
            candidates.append({"client_order_id": cid})
        for name in ("order_id", "order_sysid"):
            value = str(item.get(name) or "").strip()
            if value and value not in {"-1", "0"}:
                candidates.append({"exchange_order_id": value})
        for kwargs in candidates:
            order, _matched = await resolve_order(
                db,
                symbol=str(item.get("symbol") or ""),
                side=str(item.get("side") or ""),
                **kwargs,
            )
            if order is not None:
                return order
        return None


qmt_exec_poller = QmtExecPoller()


async def run_qmt_exec_poller_task(
    interval_seconds: float = POLL_INTERVAL_SECONDS,
) -> None:
    """常驻任务入口（trade 服务 lifespan 注册）。未配置时不空转。"""
    poller = QmtExecPoller(interval=interval_seconds)
    await poller.run()
