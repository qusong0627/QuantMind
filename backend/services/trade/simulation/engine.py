"""
Simulation Engine - 统一模拟盘引擎
信号 → 策略 → 行情 → 调仓 → 撮合 → 账本 → 快照
"""

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.trade.redis_client import RedisClient
from backend.services.trade.simulation.services.execution_engine import (
    ExecutionResult,
    SimulationExecutionEngine,
)
from backend.services.trade.simulation.services.fund_snapshot_service import (
    SimulationFundSnapshotService,
)
from backend.services.trade.simulation.services.rebalance_calculator import (
    Order,
    Quote,
    RebalanceCalculator,
    SimulationAccount,
    StrategyConfig,
    WeightMode,
)
from backend.services.trade.simulation.services.signal_loader import (
    SignalLoader,
    SignalScore,
    signal_loader,
)
from backend.services.trade.simulation.services.simulation_manager import (
    SimulationAccountManager,
)
from backend.services.trade.trade_config import settings
from backend.shared.database_manager_v2 import get_db_manager
from backend.shared.strategy_storage import get_strategy_storage_service

logger = logging.getLogger(__name__)


@dataclass
class ExecutionReport:
    """执行报告"""
    tenant_id: str
    user_id: str
    strategy_id: str
    run_id: str
    executed_at: datetime
    signal_count: int = 0
    order_count: int = 0
    filled_count: int = 0
    rejected_count: int = 0
    total_commission: float = 0.0
    orders: list[dict[str, Any]] = field(default_factory=list)
    account_snapshot: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


class SimulationEngine:
    """
    统一模拟盘引擎：
    1. PK 信号 → 读取 engine_signal_scores 表最新信号
    2. 读策略 → 读取用户策略配置（TopK、权重、风控参数）
    3. 读行情 → 批量获取实时行情 + 涨跌停状态
    4. 调仓计算 → 目标权重 → 目标持仓 → 剔除涨跌停 → 交易指令
    5. 模拟撮合 → 滑点模拟 + 手续费扣除 + 账户更新
    6. 同步快照 → 行情更新后同步持仓市值 + 日级快照
    """

    def __init__(
        self,
        redis: RedisClient | None = None,
        loader: SignalLoader | None = None,
    ):
        self.redis = redis or RedisClient()
        self.signal_loader = loader or signal_loader
        self.account_manager = SimulationAccountManager(self.redis)
        self.rebalance_calculator = RebalanceCalculator()
        self._http: httpx.AsyncClient | None = None

    async def _http_client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=5.0)
        return self._http

    async def run_cycle(
        self,
        tenant_id: str,
        user_id: str,
        strategy_id: str,
        run_id: str | None = None,
        params_override: dict[str, Any] | None = None,
    ) -> ExecutionReport:
        """
        执行一次模拟盘调仓周期。

        Args:
            tenant_id: 租户 ID
            user_id: 用户 ID
            strategy_id: 策略 ID
            run_id: 指定信号批次 ID，若 None 则取最新
            params_override: 前端传递的策略参数覆盖

        Returns:
            执行报告
        """
        tenant = (tenant_id or "").strip() or "default"
        uid = str(user_id or "").strip()
        now = datetime.now()
        exec_run_id = run_id or f"sim_{now.strftime('%Y%m%d%H%M%S')}"

        report = ExecutionReport(
            tenant_id=tenant,
            user_id=uid,
            strategy_id=strategy_id,
            run_id=exec_run_id,
            executed_at=now,
        )

        try:
            db_manager = get_db_manager()
            async with db_manager.session() as db:
                # 1. 加载信号
                signals = await self.signal_loader.load_latest_signals(
                    db=db,
                    tenant_id=tenant,
                    user_id=uid,
                    run_id=run_id,
                )
                report.signal_count = len(signals)

                if not signals:
                    logger.info(
                        "SimulationEngine: 无信号, tenant=%s user=%s strategy=%s",
                        tenant,
                        uid,
                        strategy_id,
                    )
                    report.error = "无可用信号"
                    return report

                # 2. 加载策略配置
                strategy_config = await self._load_strategy_config(
                    db=db,
                    strategy_id=strategy_id,
                    user_id=uid,
                    params_override=params_override,
                )

                # 3. 批量获取行情
                symbols = [s.symbol for s in signals]
                quotes = await self._fetch_quotes(symbols)

                # 4. 获取当前账户状态
                account_data = await self.account_manager.get_account(
                    user_id=int(uid) if uid.isdigit() else 0,
                    tenant_id=tenant,
                )
                if not account_data:
                    logger.warning(
                        "SimulationEngine: 账户不存在, tenant=%s user=%s",
                        tenant,
                        uid,
                    )
                    report.error = "账户不存在"
                    return report

                account = self._build_account(account_data)

                # 5. 调仓计算
                orders = self.rebalance_calculator.calculate(
                    signals=signals,
                    strategy=strategy_config,
                    quotes=quotes,
                    account=account,
                )
                report.order_count = len(orders)

                if not orders:
                    logger.info(
                        "SimulationEngine: 无需调仓, tenant=%s user=%s",
                        tenant,
                        uid,
                    )
                    return report

                # 6. 模拟撮合
                exec_engine = SimulationExecutionEngine(db, self.account_manager)
                for order in orders:
                    result = await self._execute_order(
                        db=db,
                        exec_engine=exec_engine,
                        order=order,
                        tenant_id=tenant,
                        user_id=uid,
                        strategy_id=strategy_id,
                    )
                    report.orders.append(self._order_to_dict(order, result))
                    if result.success:
                        report.filled_count += 1
                        report.total_commission += result.commission
                    else:
                        report.rejected_count += 1

                await db.commit()

                # 7. 同步快照
                await self._sync_snapshot(tenant, uid)

                # 8. 更新账户快照
                updated_account = await self.account_manager.get_account(
                    user_id=int(uid) if uid.isdigit() else 0,
                    tenant_id=tenant,
                )
                report.account_snapshot = updated_account or {}

            logger.info(
                "SimulationEngine: 执行完成, tenant=%s user=%s orders=%d filled=%d rejected=%d",
                tenant,
                uid,
                report.order_count,
                report.filled_count,
                report.rejected_count,
            )
            return report

        except Exception as e:
            logger.error(
                "SimulationEngine: 执行失败, tenant=%s user=%s error=%s",
                tenant,
                uid,
                e,
                exc_info=True,
            )
            report.error = str(e)
            return report

    async def _load_strategy_config(
        self,
        db: AsyncSession,
        strategy_id: str,
        user_id: str,
        params_override: dict[str, Any] | None = None,
    ) -> StrategyConfig:
        """加载策略配置，支持前端参数覆盖"""
        # 默认配置
        config = StrategyConfig()

        try:
            # 尝试从策略存储服务加载
            storage_svc = get_strategy_storage_service()
            strategy = await storage_svc.get(
                strategy_id=int(strategy_id) if strategy_id.isdigit() else 0,
                user_id=user_id,
            )

            if strategy:
                params = strategy.get("parameters", {}) or {}
                config = StrategyConfig(
                    topk=int(params.get("topk", 10)),
                    weight_mode=WeightMode(params.get("weight_mode", "equal")),
                    custom_weights=params.get("custom_weights", {}),
                    min_score=float(params.get("min_score", 0.0)),
                    max_position_pct=float(params.get("max_position_pct", 0.15)),
                    lot_size=int(params.get("lot_size", 100)),
                )
        except Exception as e:
            logger.warning("SimulationEngine: 加载策略配置失败 %s, 使用默认配置", e)

        # 前端参数覆盖
        if params_override:
            if params_override.get("topk") is not None:
                config.topk = int(params_override["topk"])
            if params_override.get("weight_mode") is not None:
                config.weight_mode = WeightMode(params_override["weight_mode"])
            if params_override.get("custom_weights") is not None:
                config.custom_weights = params_override["custom_weights"]
            if params_override.get("min_score") is not None:
                config.min_score = float(params_override["min_score"])
            if params_override.get("max_position_pct") is not None:
                config.max_position_pct = float(params_override["max_position_pct"])
            if params_override.get("lot_size") is not None:
                config.lot_size = int(params_override["lot_size"])
            logger.info(
                "SimulationEngine: 应用参数覆盖, topk=%d weight_mode=%s",
                config.topk,
                config.weight_mode.value,
            )

        return config

    async def _fetch_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        """批量获取行情"""
        if not symbols:
            return {}

        quotes: dict[str, Quote] = {}
        market_url = settings.MARKET_DATA_SERVICE_URL.rstrip("/")
        client = await self._http_client()

        # 批量请求（可优化为并发）
        for symbol in symbols:
            try:
                resp = await client.get(
                    f"{market_url}/api/v1/quotes/{symbol}",
                    timeout=3.0,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    quotes[symbol.upper()] = Quote(
                        symbol=symbol.upper(),
                        current_price=float(data.get("current_price") or data.get("last_price") or 0),
                        is_limit_up=bool(data.get("is_limit_up")),
                        is_limit_down=bool(data.get("is_limit_down")),
                        is_suspended=bool(data.get("suspended") or data.get("is_suspended")),
                        pre_close=float(data.get("pre_close") or 0) or None,
                    )
            except Exception as e:
                logger.debug("SimulationEngine: 获取行情失败 %s: %s", symbol, e)

        logger.info("SimulationEngine: 获取行情 %d/%d", len(quotes), len(symbols))
        return quotes

    def _build_account(self, data: dict[str, Any]) -> SimulationAccount:
        """构建账户对象"""
        return SimulationAccount(
            cash=float(data.get("cash", 0)),
            total_asset=float(data.get("total_asset", 0)),
            positions=data.get("positions", {}) or {},
        )

    async def _execute_order(
        self,
        db: AsyncSession,
        exec_engine: SimulationExecutionEngine,
        order: Order,
        tenant_id: str,
        user_id: str,
        strategy_id: str,
    ) -> ExecutionResult:
        """执行单个订单"""
        from backend.services.trade.simulation.models.order import (
            OrderSide,
            OrderType,
            SimOrder,
        )

        # 创建订单对象
        sim_order = SimOrder(
            tenant_id=tenant_id,
            user_id=int(user_id) if user_id.isdigit() else 0,
            symbol=order.symbol,
            side=OrderSide.BUY if order.side == "BUY" else OrderSide.SELL,
            order_type=OrderType.MARKET,
            quantity=order.quantity,
            price=order.price,
            strategy_id=int(strategy_id) if strategy_id.isdigit() else None,
        )
        db.add(sim_order)
        await db.flush()

        # 执行订单
        result = await exec_engine.execute_order(sim_order)
        if result.success:
            await exec_engine.apply_filled(sim_order, result)
        else:
            await exec_engine.mark_rejected(sim_order, result.message)

        return result

    def _order_to_dict(self, order: Order, result: ExecutionResult) -> dict[str, Any]:
        """订单结果转字典"""
        return {
            "symbol": order.symbol,
            "side": order.side,
            "quantity": order.quantity,
            "price": order.price,
            "reason": order.reason,
            "success": result.success,
            "executed_price": result.price if result.success else None,
            "commission": result.commission if result.success else None,
            "message": result.message if not result.success else None,
        }

    async def _sync_snapshot(self, tenant_id: str, user_id: str) -> None:
        """同步快照"""
        try:
            await SimulationFundSnapshotService.capture_all(self.redis)
            logger.debug(
                "SimulationEngine: 快照同步完成, tenant=%s user=%s",
                tenant_id,
                user_id,
            )
        except Exception as e:
            logger.warning("SimulationEngine: 快照同步失败 %s", e)


simulation_engine = SimulationEngine()
