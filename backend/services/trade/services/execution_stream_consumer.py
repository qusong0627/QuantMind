from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from typing import Any, Dict, List, Optional
from uuid import UUID

import redis
from sqlalchemy import and_, select

from backend.services.trade_shared.models.enums import OrderStatus
from backend.services.trade_shared.models.order import Order
from backend.services.trade_shared.models.trade import Trade
from backend.shared.agent_ledger_fill import post_fill_for_order
from backend.shared.database_manager_v2 import get_session
from backend.shared.notification_publisher import publish_notification_async

logger = logging.getLogger(__name__)


class ExecutionStreamConsumer:
    """
    消费执行回报事件并写入 PG：
    - order_filled: 按 broker_order_id+exec_id 幂等落成交并更新订单状态
    - order_rejected: 更新订单为 REJECTED
    - order_submitted: 将 PENDING 推进到 SUBMITTED
    """

    def __init__(self):
        self.enabled = os.getenv("ENABLE_EXEC_STREAM_CONSUMER", "false").lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        self.stream_prefix = os.getenv("EXEC_STREAM_PREFIX", "qm:exec:stream")
        self.group = os.getenv("EXEC_STREAM_GROUP", "exec-trade")
        self.consumer_name = os.getenv("EXEC_STREAM_CONSUMER_NAME", "trade-consumer-1")
        self.batch_size = int(os.getenv("EXEC_STREAM_BATCH_SIZE", "100"))
        self.block_ms = int(os.getenv("EXEC_STREAM_BLOCK_MS", "3000"))
        self.max_retry = int(os.getenv("EXEC_STREAM_MAX_RETRY", "3"))
        self.dlq_prefix = os.getenv("EXEC_STREAM_DLQ_PREFIX", "qm:exec:dlq")
        tenants_raw = os.getenv("EXEC_STREAM_TENANTS", "default")
        self.tenants = [t.strip() for t in tenants_raw.split(",") if t.strip()] or ["default"]
        self.stream_redis_host = os.getenv("EXEC_STREAM_REDIS_HOST", os.getenv("REDIS_HOST", "localhost"))
        self.stream_redis_port = int(os.getenv("EXEC_STREAM_REDIS_PORT", os.getenv("REDIS_PORT", "6379")))
        self.stream_redis_password = os.getenv("EXEC_STREAM_REDIS_PASSWORD", os.getenv("REDIS_PASSWORD", ""))
        self.stream_redis_db = int(os.getenv("EXEC_STREAM_REDIS_DB", "0"))

        self._running = False
        self._task: asyncio.Task | None = None
        self._stream_client: redis.Redis | None = None

    def _stream_names(self) -> list[str]:
        return [f"{self.stream_prefix}:{tenant}" for tenant in self.tenants]

    async def start(self) -> None:
        if not self.enabled:
            logger.info("Execution stream consumer disabled")
            return
        if self._running:
            return

        self._running = True
        self._ensure_groups()
        self._task = asyncio.create_task(self._run(), name="execution-stream-consumer")
        logger.info(
            "Execution stream consumer started: streams=%s group=%s consumer=%s",
            self._stream_names(),
            self.group,
            self.consumer_name,
        )

    async def stop(self) -> None:
        self._running = False
        if self._task is None:
            return
        try:
            await asyncio.wait_for(self._task, timeout=5.0)
        except Exception:
            self._task.cancel()
        finally:
            self._task = None
        if self._stream_client is not None:
            try:
                self._stream_client.close()
            except Exception:
                pass
            self._stream_client = None
        logger.info("Execution stream consumer stopped")

    def _get_stream_client(self) -> redis.Redis:
        if self._stream_client is None:
            self._stream_client = redis.Redis(
                host=self.stream_redis_host,
                port=self.stream_redis_port,
                db=self.stream_redis_db,
                password=self.stream_redis_password,
                decode_responses=True,
                socket_timeout=5.0,
                socket_connect_timeout=5.0,
            )
            self._stream_client.ping()
        return self._stream_client

    def _ensure_groups(self) -> None:
        client = self._get_stream_client()

        for stream in self._stream_names():
            try:
                client.xgroup_create(stream, self.group, id="$", mkstream=True)
                logger.info("Created stream group: stream=%s group=%s", stream, self.group)
            except Exception as exc:
                if "BUSYGROUP" not in str(exc):
                    logger.warning("xgroup_create failed stream=%s: %s", stream, exc)

    async def _run(self) -> None:
        try:
            client = self._get_stream_client()
        except Exception as exc:
            logger.error("Execution stream consumer cannot start: redis unavailable: %s", exc)
            return

        streams = dict.fromkeys(self._stream_names(), ">")
        while self._running:
            try:
                items = await asyncio.to_thread(
                    client.xreadgroup,
                    groupname=self.group,
                    consumername=self.consumer_name,
                    streams=streams,
                    count=self.batch_size,
                    block=self.block_ms,
                )
                if not items:
                    continue

                for stream_name, records in items:
                    for message_id, fields in records:
                        ok = await self._process_message(
                            stream_name=stream_name,
                            message_id=message_id,
                            fields=fields,
                        )
                        if ok:
                            try:
                                await asyncio.to_thread(client.xack, stream_name, self.group, message_id)
                            except Exception as ack_exc:
                                logger.warning(
                                    "xack failed stream=%s msg=%s err=%s",
                                    stream_name,
                                    message_id,
                                    ack_exc,
                                )
            except Exception as exc:
                logger.error("Execution stream consume loop error: %s", exc, exc_info=True)
                await asyncio.sleep(1.0)

    async def _process_message(self, *, stream_name: str, message_id: str, fields: dict[str, Any]) -> bool:
        event_type = str(fields.get("event_type") or "").strip()
        if not event_type:
            logger.warning("skip message without event_type stream=%s msg=%s", stream_name, message_id)
            return True

        try:
            if event_type == "order_filled":
                await self._handle_order_filled(fields)
            elif event_type == "order_rejected":
                await self._handle_order_rejected(fields)
            elif event_type == "order_submitted":
                await self._handle_order_submitted(fields)
            elif event_type == "order_cancelled":
                await self._handle_order_cancelled(fields)
            elif event_type == "order_duplicate_skipped":
                pass
            else:
                logger.info("ignore unsupported event_type=%s", event_type)
            return True
        except ValueError as exc:
            self._write_dlq(
                stream_name=stream_name,
                message_id=message_id,
                fields=fields,
                reason=f"validation_error:{exc}",
            )
            return True
        except Exception as exc:
            logger.error(
                "process execution event failed stream=%s msg=%s type=%s err=%s",
                stream_name,
                message_id,
                event_type,
                exc,
                exc_info=True,
            )
            return self._retry_or_dlq(
                stream_name=stream_name,
                message_id=message_id,
                fields=fields,
                reason=f"processing_error:{exc}",
            )

    def _dlq_stream_name(self, stream_name: str, tenant: str) -> str:
        return f"{self.dlq_prefix}:{tenant or 'default'}"

    @staticmethod
    def _tenant_from_stream(stream_name: str) -> str:
        try:
            return str(stream_name).rsplit(":", 1)[-1]
        except Exception:
            return "default"

    def _write_dlq(
        self,
        *,
        stream_name: str,
        message_id: str,
        fields: dict[str, Any],
        reason: str,
    ) -> None:
        try:
            client = self._get_stream_client()
        except Exception as exc:
            logger.error("dlq write failed: redis unavailable: %s", exc)
            return

        tenant = self._tenant_from_stream(stream_name)
        dlq_stream = self._dlq_stream_name(stream_name, tenant)
        payload = {
            "source_stream": str(stream_name),
            "source_message_id": str(message_id),
            "failed_at_ms": str(int(time.time() * 1000)),
            "reason": reason,
            "event_type": str(fields.get("event_type") or ""),
            "tenant_id": str(fields.get("tenant_id") or tenant),
            "user_id": str(fields.get("user_id") or ""),
            "client_order_id": str(fields.get("client_order_id") or ""),
            "broker_order_id": str(fields.get("broker_order_id") or ""),
            "exec_id": str(fields.get("exec_id") or ""),
            "raw_event_json": json.dumps(fields, ensure_ascii=False),
        }
        try:
            client.xadd(dlq_stream, payload, maxlen=200000, approximate=True)
            logger.warning(
                "execution event moved to DLQ stream=%s source_msg=%s reason=%s",
                dlq_stream,
                message_id,
                reason,
            )
        except Exception as exc:
            logger.error("dlq xadd failed stream=%s err=%s", dlq_stream, exc)

    def _retry_or_dlq(
        self,
        *,
        stream_name: str,
        message_id: str,
        fields: dict[str, Any],
        reason: str,
    ) -> bool:
        try:
            client = self._get_stream_client()
        except Exception as exc:
            logger.error("retry failed: redis unavailable: %s", exc)
            return False

        retry_count = int(fields.get("retry_count") or 0)
        if retry_count >= self.max_retry:
            self._write_dlq(
                stream_name=stream_name,
                message_id=message_id,
                fields=fields,
                reason=f"{reason};retry_exhausted={retry_count}",
            )
            return True

        retry_event = dict(fields)
        retry_event["retry_count"] = str(retry_count + 1)
        retry_event["last_error"] = reason
        retry_event["retried_at_ms"] = str(int(time.time() * 1000))
        retry_event["original_message_id"] = str(message_id)
        try:
            client.xadd(stream_name, retry_event, maxlen=200000, approximate=True)
            logger.warning(
                "execution event requeued stream=%s msg=%s retry=%d",
                stream_name,
                message_id,
                retry_count + 1,
            )
            return True
        except Exception as exc:
            logger.error("requeue failed stream=%s msg=%s err=%s", stream_name, message_id, exc)
            return False

    @staticmethod
    def _parse_uuid(raw: str) -> UUID:
        return UUID(str(raw))

    @staticmethod
    def _try_parse_uuid(raw: Any) -> UUID | None:
        try:
            text = str(raw or "").strip()
            if not text:
                return None
            return uuid.UUID(text)
        except Exception:
            return None

    @staticmethod
    def _to_int(raw: Any) -> int | None:
        try:
            text = str(raw or "").strip()
            if not text:
                return None
            return int(text)
        except Exception:
            return None

    async def _resolve_order(self, session, fields: dict[str, Any]) -> Order | None:
        order_uuid = self._try_parse_uuid(fields.get("order_id"))
        if order_uuid is not None:
            result = await session.execute(select(Order).where(Order.order_id == order_uuid).limit(1))
            order = result.scalar_one_or_none()
            if order is not None:
                return order

        tenant_id = str(fields.get("tenant_id") or "").strip()
        user_id = self._to_int(fields.get("user_id"))
        client_order_id = str(fields.get("client_order_id") or "").strip()
        if tenant_id and user_id is not None and client_order_id:
            result = await session.execute(
                select(Order)
                .where(
                    and_(
                        Order.tenant_id == tenant_id,
                        Order.user_id == str(user_id),
                        Order.client_order_id == client_order_id,
                    )
                )
                .limit(1)
            )
            order = result.scalar_one_or_none()
            if order is not None:
                return order

        exchange_order_id = str(
            fields.get("exchange_order_id") or fields.get("broker_order_id") or ""
        ).strip()
        if tenant_id and user_id is not None and exchange_order_id:
            result = await session.execute(
                select(Order)
                .where(
                    and_(
                        Order.tenant_id == tenant_id,
                        Order.user_id == str(user_id),
                        Order.exchange_order_id == exchange_order_id,
                    )
                )
                .limit(1)
            )
            order = result.scalar_one_or_none()
            if order is not None:
                return order

        broker_order_uuid = self._try_parse_uuid(fields.get("broker_order_id"))
        if broker_order_uuid is not None:
            result = await session.execute(select(Order).where(Order.order_id == broker_order_uuid).limit(1))
            order = result.scalar_one_or_none()
            if order is not None:
                return order

        return None

    @staticmethod
    def _trade_idempotency_key(fields: dict[str, Any]) -> str:
        exchange_trade_id = str(fields.get("exchange_trade_id") or "").strip()
        if exchange_trade_id:
            return exchange_trade_id

        broker_order_id = str(fields.get("broker_order_id") or fields.get("exchange_order_id") or "").strip()
        exec_id = str(fields.get("exec_id") or "").strip()
        if broker_order_id and exec_id:
            return f"{broker_order_id}:{exec_id}"
        raise ValueError("order_filled missing exchange_trade_id or broker_order_id+exec_id")

    async def _handle_order_submitted(self, fields: dict[str, Any]) -> None:
        async with get_session() as session:
            order = await self._resolve_order(session, fields)
            if order is None:
                return
            if order.status == OrderStatus.PENDING:
                order.status = OrderStatus.SUBMITTED
                order.remarks = ((order.remarks or "") + f" [STREAM_SUBMITTED event_id={fields.get('event_id')}]")[
                    -500:
                ]

    async def _handle_order_rejected(self, fields: dict[str, Any]) -> None:
        reason = str(fields.get("reason") or "stream rejected")

        async with get_session() as session:
            order = await self._resolve_order(session, fields)
            if order is None:
                return
            if order.status not in {OrderStatus.FILLED, OrderStatus.CANCELLED}:
                order.status = OrderStatus.REJECTED
                order.remarks = ((order.remarks or "") + f" [STREAM_REJECTED {reason}]")[-500:]
                await publish_notification_async(
                    user_id=str(order.user_id),
                    tenant_id=str(order.tenant_id or "default"),
                    title="订单被拒绝",
                    content=f"{order.symbol} 下单失败：{reason}",
                    type="trading",
                    level="error",
                    action_url="/trading",
                )

    async def _handle_order_filled(self, fields: dict[str, Any]) -> None:
        idem_key = self._trade_idempotency_key(fields)

        filled_qty = float(fields.get("filled_qty") or fields.get("quantity") or 0.0)
        filled_price = float(fields.get("filled_price") or fields.get("price") or 0.0)
        if filled_qty <= 0 or filled_price <= 0:
            raise ValueError("order_filled invalid filled_qty/filled_price")

        async with get_session() as session:
            # 幂等检查：已有同 idem_key 的成交则直接返回
            dup_result = await session.execute(select(Trade).where(Trade.exchange_trade_id == idem_key).limit(1))
            if dup_result.scalar_one_or_none() is not None:
                return

            order = await self._resolve_order(session, fields)
            if order is None:
                raise ValueError(
                    "order not found for execution event "
                    f"order_id={fields.get('order_id')} "
                    f"client_order_id={fields.get('client_order_id')} "
                    f"exchange_order_id={fields.get('exchange_order_id') or fields.get('broker_order_id')}"
                )

            trade_value = filled_qty * filled_price
            trade = Trade(
                order_id=order.order_id,
                tenant_id=order.tenant_id,
                user_id=order.user_id,
                portfolio_id=order.portfolio_id,
                symbol=order.symbol,
                side=order.side,
                trading_mode=order.trading_mode,
                quantity=filled_qty,
                price=filled_price,
                trade_value=trade_value,
                commission=0.0,
                exchange_trade_id=idem_key,
                exchange_name="execution_stream",
                remarks=str(fields.get("reason") or ""),
            )
            session.add(trade)

            # P2.7 分账：同一事务把成交记进 ``orders.agent`` 名下（非 LLM 腿静默跳过）。
            # 与 ``qmt_exec_reconciler`` 同一条纪律：只在插入分支落账（上面已按
            # 成交号去重过），不 catch（写不进去 = 这笔成交也不提交，留给重投）。
            # 成交日取 UTC 日：重投（``_trade_idempotency_key`` 的键）要能算出同一天。
            await post_fill_for_order(
                session,
                order=order,
                fill_key=idem_key,
                quantity=filled_qty,
                price=filled_price,
            )

            # T-P1-03：Fill 契约——成交价来源（券商回报），REAL 订单可溯取价链路
            from backend.shared.order_contract import (
                PRICE_SOURCE_BROKER_FILL,
                ensure_order_contract_columns_async,
            )

            await ensure_order_contract_columns_async()
            order.price_source = PRICE_SOURCE_BROKER_FILL

            order.filled_quantity = float(order.filled_quantity or 0.0) + filled_qty
            order.filled_value = float(order.filled_value or 0.0) + trade_value
            if order.filled_quantity > 0:
                order.average_price = order.filled_value / order.filled_quantity

            if order.filled_quantity >= float(order.quantity or 0.0):
                order.status = OrderStatus.FILLED
                await publish_notification_async(
                    user_id=str(order.user_id),
                    tenant_id=str(order.tenant_id or "default"),
                    title="订单成交确认",
                    content=f"{order.symbol} 成交 {filled_qty} 股，价格 {filled_price}",
                    type="trading",
                    level="success",
                    action_url="/trading",
                )
            elif order.filled_quantity > 0:
                order.status = OrderStatus.PARTIALLY_FILLED

            await session.commit()

    async def _handle_order_cancelled(self, fields: dict[str, Any]) -> None:
        reason = str(fields.get("reason") or "stream cancelled")

        async with get_session() as session:
            order = await self._resolve_order(session, fields)
            if order is None:
                return

            if order.status not in {OrderStatus.FILLED, OrderStatus.CANCELLED}:
                order.status = OrderStatus.CANCELLED
                order.remarks = ((order.remarks or "") + f" [STREAM_CANCELLED {reason}]")[-500:]

                await publish_notification_async(
                    user_id=str(order.user_id),
                    tenant_id=str(order.tenant_id or "default"),
                    title="订单已撤销",
                    content=f"{order.symbol} 订单已撤回：{reason}",
                    type="trading",
                    level="info",
                    action_url="/trading",
                )

                # [Automation] 1分钟超时自动轮转买入逻辑
                if "timeout" in reason.lower() and order.side.name.upper() == "BUY":
                    logger.info("Triggering rotate-buy-next for user %s due to order %s timeout", order.user_id, order.order_id)

                    # 尝试异步触发下一只股票买入 (Fire and forget from this context)
                    async def _rotate_task():
                        async with get_session() as rotate_session:
                            from backend.services.trade_shared.redis_client import get_redis
                            from backend.services.live_trading.services.trading_engine import TradingEngine

                            redis_client = get_redis()
                            engine = TradingEngine(rotate_session, redis_client)

                            # 获取轮转建议
                            suggestion = await engine.rotate_buy_next(order.user_id, order.portfolio_id, order.symbol)
                            if suggestion:
                                logger.info("Rotating from %s to %s for user %s", order.symbol, suggestion['symbol'], order.user_id)
                                # 理想情况下处理下单逻辑：
                                # 此处建议由 TradingEngine 进一步封装一个 submit_rotate_order
                                # 为简化演示，此处记录建议已达成。
                                pass

                    asyncio.create_task(_rotate_task())

            await session.commit()
