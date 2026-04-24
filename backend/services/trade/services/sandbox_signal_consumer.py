"""
Sandbox Signal Consumer - 消费沙箱 Worker 产生的模拟盘信号，并转换为真实订单执行。

运行在 trade 服务主进程中，作为后台异步任务启动。
"""

import asyncio
import json
import logging
import math
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.trade.redis_client import redis_client
from backend.services.trade.simulation.models.order import OrderSide, OrderType
from backend.services.trade.simulation.schemas.order import SimOrderCreate
from backend.services.trade.simulation.services.execution_engine import (
    ExecutionResult,
    SimulationExecutionEngine,
)
from backend.services.trade.simulation.services.order_service import SimOrderService
from backend.services.trade.simulation.services.simulation_manager import SimulationAccountManager
from backend.services.trade.trade_config import settings
from backend.shared.database_manager_v2 import get_db_manager

logger = logging.getLogger(__name__)

_SIGNAL_QUEUE = "trade:simulation:signals"
_BATCH_SIZE = 50
_POLL_INTERVAL = 0.2


class SandboxSignalConsumer:
    """
    消费沙箱信号并执行模拟盘订单。

    支持的信号类型：
    - order_target_percent: 按目标持仓比例自动计算下单量并执行
    - order: 直接下单信号（包含具体数量和价格）
    - log: 仅记录日志
    """

    def __init__(self):
        self.is_running = False
        self._http: httpx.AsyncClient | None = None
        self._account_manager = SimulationAccountManager(redis_client)

    async def _http_client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=5.0)
        return self._http

    async def start(self):
        """启动信号消费循环"""
        self.is_running = True
        logger.info("[SandboxSignalConsumer] 已启动，监听队列: %s", _SIGNAL_QUEUE)
        try:
            while self.is_running:
                await self._consume_batch()
                await asyncio.sleep(_POLL_INTERVAL)
        except asyncio.CancelledError:
            logger.info("[SandboxSignalConsumer] 收到取消信号，正在停止...")
        except Exception as e:
            logger.error("[SandboxSignalConsumer] 运行异常: %s", e, exc_info=True)
        finally:
            self.is_running = False
            if self._http:
                await self._http.aclose()
            logger.info("[SandboxSignalConsumer] 已停止。")

    async def stop(self):
        """停止信号消费"""
        self.is_running = False

    async def _consume_batch(self):
        """批量消费信号"""
        if not redis_client.client:
            return

        for _ in range(_BATCH_SIZE):
            try:
                raw_sig = redis_client.client.lpop(_SIGNAL_QUEUE)
                if not raw_sig:
                    break
                sig = json.loads(raw_sig)
                await self._process_signal(sig)
            except Exception as e:
                logger.error("[SandboxSignalConsumer] 处理信号失败: %s", e)

    async def _process_signal(self, sig: dict[str, Any]):
        """处理单个信号"""
        sig_type = sig.get("type")
        tenant_id = sig.get("tenant_id", "default")
        user_id = str(sig.get("user_id", ""))
        strategy_id = sig.get("strategy_id", "unknown")

        if sig_type == "log":
            logger.info("[Sandbox Log %s] %s", strategy_id, sig.get("message"))
            return

        if sig_type == "order_target_percent":
            await self._handle_order_target_percent(sig, tenant_id, user_id, strategy_id)
        elif sig_type == "order":
            await self._handle_direct_order(sig, tenant_id, user_id, strategy_id)
        else:
            logger.debug("[SandboxSignalConsumer] 未知信号类型: %s", sig_type)

    async def _handle_order_target_percent(
        self, sig: dict[str, Any], tenant_id: str, user_id: str, strategy_id: str
    ):
        """
        处理 order_target_percent 信号：
        1. 读取当前账户状态（现金 + 持仓）
        2. 计算目标持仓数量
        3. 计算需要买卖的数量
        4. 创建订单并执行
        """
        data = sig.get("data", {})
        symbol = data.get("symbol")
        target_percent = float(data.get("target_percent", 0))
        run_id = sig.get("run_id", "")

        if not symbol:
            logger.warning("[SandboxSignalConsumer] 信号缺少 symbol")
            return

        user_id_int = int(user_id) if user_id.isdigit() else 0
        if user_id_int <= 0:
            logger.warning("[SandboxSignalConsumer] 无效的 user_id: %s", user_id)
            return

        # 获取账户状态
        account = await self._account_manager.get_account(user_id_int, tenant_id=tenant_id)
        if not account:
            logger.warning("[SandboxSignalConsumer] 账户不存在: tenant=%s user=%s", tenant_id, user_id)
            return

        total_asset = float(account.get("total_asset", 0))
        if total_asset <= 0:
            logger.warning("[SandboxSignalConsumer] 总资产为 0，跳过下单")
            return

        # 获取当前价格
        current_price = await self._get_current_price(symbol)
        if current_price <= 0:
            logger.warning("[SandboxSignalConsumer] 无法获取 %s 的价格，跳过下单", symbol)
            return

        # 计算目标持仓数量（A 股 100 股整数倍）
        target_value = total_asset * target_percent
        target_volume = int(target_value / current_price / 100) * 100

        # 获取当前持仓
        positions = account.get("positions", {})
        current_pos = positions.get(symbol.upper())
        current_volume = int(float(current_pos.get("volume", 0))) if current_pos else 0

        # 计算需要交易的量
        delta = target_volume - current_volume
        if abs(delta) < 100:
            logger.debug(
                "[SandboxSignalConsumer] %s 调仓量不足 100 股 (delta=%d)，跳过",
                symbol, delta,
            )
            return

        # 确定买卖方向
        if delta > 0:
            side = OrderSide.BUY
            quantity = delta
        else:
            side = OrderSide.SELL
            quantity = abs(delta)

        logger.info(
            "[SandboxSignalConsumer] %s %s: 当前=%d 目标=%d 交易=%d 价格=%.2f",
            symbol, side.value, current_volume, target_volume, quantity, current_price,
        )

        # 创建并执行订单
        await self._create_and_execute_order(
            tenant_id=tenant_id,
            user_id=user_id_int,
            strategy_id=strategy_id,
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=current_price,
            run_id=run_id,
        )

    async def _handle_direct_order(
        self, sig: dict[str, Any], tenant_id: str, user_id: str, strategy_id: str
    ):
        """处理直接下单信号"""
        data = sig.get("data", {})
        symbol = data.get("symbol")
        quantity = int(data.get("quantity", 0))
        price = float(data.get("price", 0))
        side_str = data.get("side", "BUY")
        run_id = sig.get("run_id", "")

        if not symbol or quantity <= 0:
            logger.warning("[SandboxSignalConsumer] 直接下单信号缺少必要参数")
            return

        user_id_int = int(user_id) if user_id.isdigit() else 0
        if user_id_int <= 0:
            return

        side = OrderSide.BUY if side_str.upper() == "BUY" else OrderSide.SELL

        await self._create_and_execute_order(
            tenant_id=tenant_id,
            user_id=user_id_int,
            strategy_id=strategy_id,
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=price if price > 0 else await self._get_current_price(symbol),
            run_id=run_id,
        )

    async def _create_and_execute_order(
        self,
        tenant_id: str,
        user_id: int,
        strategy_id: str,
        symbol: str,
        side: OrderSide,
        quantity: int,
        price: float,
        run_id: str,
    ):
        """创建订单并执行"""
        db_manager = get_db_manager()
        async with db_manager.session() as db:
            order_service = SimOrderService(db)
            exec_engine = SimulationExecutionEngine(db, self._account_manager)

            # 创建订单
            order_create = SimOrderCreate(
                symbol=symbol,
                side=side,
                order_type=OrderType.MARKET,
                quantity=quantity,
                price=price,
                strategy_id=strategy_id,
                remarks=f"Sandbox signal: {run_id}",
            )
            order = await order_service.create_order(tenant_id, user_id, order_create)
            logger.info("[SandboxSignalConsumer] 订单创建: order_id=%s", order.order_id)

            # 提交订单
            from backend.services.trade.simulation.models.order import OrderStatus
            order.status = OrderStatus.SUBMITTED
            order.submitted_at = datetime.now()
            await db.commit()

            # 执行订单
            result = await exec_engine.execute_order(order)
            if result.success:
                trade = await exec_engine.apply_filled(order, result)
                logger.info(
                    "[SandboxSignalConsumer] 订单成交: %s %s %d@%.2f, commission=%.2f",
                    symbol, side.value, result.quantity, result.price, result.commission,
                )
            else:
                await exec_engine.mark_rejected(order, result.message)
                logger.warning("[SandboxSignalConsumer] 订单被拒: %s - %s", symbol, result.message)

            await db.commit()

    async def _get_current_price(self, symbol: str) -> float:
        """获取当前市场价格"""
        market_url = settings.MARKET_DATA_SERVICE_URL.rstrip("/")
        endpoint = f"{market_url}/api/v1/quotes/{symbol}"
        try:
            client = await self._http_client()
            resp = await client.get(endpoint)
            if resp.status_code == 200:
                data = resp.json()
                px = data.get("current_price") or data.get("last_price")
                if px and float(px) > 0:
                    return float(px)
        except Exception as e:
            logger.warning("获取 %s 行情失败: %s", symbol, e)
        return 0.0


# 单例
sandbox_signal_consumer = SandboxSignalConsumer()
