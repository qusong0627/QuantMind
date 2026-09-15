"""
Simulation Engine - 统一模拟盘引擎
信号 → 策略 → 行情 → 调仓 → 撮合 → 账本 → 快照
"""

import asyncio
import json
import logging
import math
import os
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from backend.services.trade_shared.redis_client import RedisClient
from backend.services.simulation.services.execution_engine import (
    ExecutionResult,
    SimulationExecutionEngine,
)
from backend.services.simulation.services.fund_snapshot_service import (
    SimulationFundSnapshotService,
)
from backend.services.simulation.services.local_market_data import (
    LocalMarketData,
    get_local_market_data,
)
from backend.services.simulation.services.rebalance_calculator import (
    Order,
    Quote,
    RebalanceCalculator,
    SimulationAccount,
    StrategyConfig,
    WeightMode,
)
from backend.services.simulation.services.market_rules import (
    infer_market_from_symbols,
    rules_for,
)
from backend.services.simulation.services.signal_loader import (
    SignalLoader,
    SignalScore,
    signal_loader,
)
from backend.services.simulation.services.simulation_manager import (
    SimulationAccountManager,
)
from backend.services.trade_shared.trade_config import settings
from backend.shared.database_manager_v2 import get_db_manager
from backend.shared.stock_utils import StockCodeUtil
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
        market_data: LocalMarketData | None = None,
    ):
        self.redis = redis or RedisClient()
        self.signal_loader = loader or signal_loader
        self.account_manager = SimulationAccountManager(self.redis)
        self.rebalance_calculator = RebalanceCalculator()
        self._market_data = market_data or get_local_market_data()

    async def run_cycle(
        self,
        tenant_id: str,
        user_id: str,
        strategy_id: str,
        run_id: str | None = None,
        params_override: dict[str, Any] | None = None,
        market: str | None = None,
        pool_id: str | None = None,
    ) -> ExecutionReport:
        """
        执行一次模拟盘调仓周期。

        Args:
            tenant_id: 租户 ID
            user_id: 用户 ID
            strategy_id: 策略 ID
            run_id: 指定信号批次 ID，若 None 则取最新
            params_override: 前端传递的策略参数覆盖
            market: 策略市场提示（激活策略 parameters.market）。
                   港股信号 symbol 为裸数字无法靠众数推断，须由调用方显式传入。

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
                # 1. 加载信号（market 提示时按市场过滤；缺省旧行为）
                signals = await self.signal_loader.load_latest_signals(
                    db=db,
                    tenant_id=tenant,
                    user_id=uid,
                    run_id=run_id,
                    market=market,
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

                # 1.5 市场推断：同一策略的信号来自同一模型/市场，按信号代码
                # 众数确定本轮行情源、交易规则与账户维度。
                # 港股信号为裸数字（DB 契约）——由激活策略的 market 提示直接指定。
                market = infer_market_from_symbols(
                    [s.symbol for s in signals], market_hint=market
                )
                logger.info(
                    "SimulationEngine: market=%s (from %d signals)",
                    market.value,
                    len(signals),
                )

                # 1.6 全局股票池过滤（P3）：严格语义，池为空或零命中即终止本轮，
                # 绝不放行全市场信号（否则模拟盘会买进池外标的）。
                override_pool = (
                    params_override.get("pool_id")
                    if isinstance(params_override, dict)
                    else None
                )
                effective_pool_id = pool_id or override_pool
                if effective_pool_id:
                    from backend.shared.stock_pool.filters import (
                        filter_signals_by_pool,
                    )
                    from backend.shared.stock_pool.resolver import (
                        ResolveContext,
                        resolver as pool_resolver,
                    )

                    pool_snapshot = await pool_resolver.resolve(
                        str(effective_pool_id),
                        ResolveContext(tenant_id=tenant, user_id=uid),
                    )
                    outcome = filter_signals_by_pool(
                        [
                            {"symbol": s.symbol, "score": getattr(s, "score", 0.0), "_ref": s}
                            for s in signals
                        ],
                        pool_snapshot,
                    )
                    if outcome.empty_pool or outcome.empty_result:
                        reason = "; ".join(outcome.warnings) or "池过滤后无信号"
                        logger.error(
                            "SimulationEngine: 池过滤失败 pool_id=%s tenant=%s user=%s: %s",
                            effective_pool_id,
                            tenant,
                            uid,
                            reason,
                        )
                        report.error = f"股票池过滤失败: {reason}"
                        return report
                    signals = [row["_ref"] for row in outcome.kept]
                    report.signal_count = len(signals)
                    logger.info(
                        "SimulationEngine: 池过滤 pool_id=%s kept=%d dropped=%d checksum=%s",
                        outcome.pool_id,
                        len(outcome.kept),
                        outcome.dropped,
                        outcome.pool_checksum,
                    )

                # 2. 加载策略配置
                strategy_config = await self._load_strategy_config(
                    db=db,
                    strategy_id=strategy_id,
                    user_id=uid,
                    params_override=params_override,
                    market=market,
                )

                # 3. 获取当前账户状态（按市场隔离）
                account_data = await self.account_manager.get_account(
                    user_id=int(uid) if uid.isdigit() else 0,
                    tenant_id=tenant,
                    market=market.value,
                )
                if not account_data:
                    logger.warning(
                        "SimulationEngine: 账户不存在, tenant=%s user=%s market=%s",
                        tenant,
                        uid,
                        market.value,
                    )
                    report.error = "账户不存在"
                    return report

                account = self._build_account(account_data)

                # 4. 批量获取行情（信号 + 现有持仓，一次分区直读）
                position_symbols = [
                    str(sym)
                    for sym, pos in (account.positions or {}).items()
                    if int(float((pos or {}).get("volume") or 0)) > 0
                ]
                symbols = list(dict.fromkeys([s.symbol for s in signals] + position_symbols))
                bars = await self._load_bars(symbols, market=market)
                quotes = self._quotes_from_bars(bars)

                # 5. 调仓计算
                orders = self.rebalance_calculator.calculate(
                    signals=signals,
                    strategy=strategy_config,
                    quotes=quotes,
                    account=account,
                )
                orders = self._apply_risk_buy_locks(
                    orders, tenant=tenant, user_id=uid, trade_date=datetime.now().date()
                )
                report.order_count = len(orders)

                if not orders:
                    logger.info(
                        "SimulationEngine: 无需调仓, tenant=%s user=%s",
                        tenant,
                        uid,
                    )
                    return report

                # 6. 模拟撮合（ashare_matcher + 当日不复权日 K）
                exec_engine = SimulationExecutionEngine(db, self.account_manager)
                for order in orders:
                    result = await self._execute_order(
                        db=db,
                        exec_engine=exec_engine,
                        order=order,
                        tenant_id=tenant,
                        user_id=uid,
                        strategy_id=strategy_id,
                        market=market,
                        run_id=exec_run_id,
                        bar=self._bar_for_symbol(bars, order.symbol),
                    )
                    report.orders.append(self._order_to_dict(order, result))
                    if result.success:
                        report.filled_count += 1
                        report.total_commission += result.commission
                    else:
                        report.rejected_count += 1

                await db.commit()

                # 7. 同步快照
                await self._sync_snapshot(tenant, uid, market)

                # 8. 更新账户快照
                updated_account = await self.account_manager.get_account(
                    user_id=int(uid) if uid.isdigit() else 0,
                    tenant_id=tenant,
                    market=market.value,
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
        market: Any = None,
    ) -> StrategyConfig:
        """加载策略配置，支持前端参数覆盖。

        lot_size 默认值按市场规则（CN 100 股整手，其余 1）；显式传入的
        策略参数仍优先。
        """
        rules = rules_for(market)
        default_lot = rules.lot_size
        # 默认配置
        config = StrategyConfig(lot_size=default_lot)

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
                    lot_size=int(params.get("lot_size", default_lot)),
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

    async def _fetch_quotes(
        self,
        symbols: list[str],
        as_of: date | None = None,
        market: Any = None,
    ) -> dict[str, Quote]:
        """从本地市场数据批量获取行情（一次分区直读替代逐 symbol HTTP）。

        as_of 指定基准交易日，仅时光回放会传；不传即按今天，活路径行为不变。
        """
        if not symbols:
            return {}
        bars = await self._load_bars(symbols, as_of=as_of, market=market)
        return self._quotes_from_bars(bars)

    async def _load_bars(
        self,
        symbols: list[str],
        as_of: date | None = None,
        market: Any = None,
    ) -> dict[str, Any]:
        if not symbols:
            return {}
        market_data = get_local_market_data(market)
        trade_date = as_of or datetime.now().date()
        latest = await asyncio.to_thread(market_data.latest_trade_date, trade_date)
        if latest is not None:
            trade_date = latest
        return await asyncio.to_thread(market_data.load_date, trade_date, symbols)

    @staticmethod
    def _quotes_from_bars(bars: dict[str, Any]) -> dict[str, Quote]:
        quotes: dict[str, Quote] = {}
        for sym, bar in bars.items():
            quote = Quote(
                symbol=sym,
                current_price=bar.close,
                is_limit_up=(bar.close >= bar.limit_up) if math.isfinite(bar.limit_up) else False,
                is_limit_down=(bar.close <= bar.limit_down) if bar.limit_down > 0 else False,
                is_suspended=bar.suspended,
                pre_close=bar.pre_close if bar.pre_close > 0 else None,
            )
            quotes[sym] = quote
            prefix = StockCodeUtil.to_prefix(sym)
            if prefix and prefix != sym:
                quotes[prefix] = quote
        logger.info("SimulationEngine: 本地行情 %d bars", len(bars))
        return quotes

    @staticmethod
    def _bar_for_symbol(bars: dict[str, Any], symbol: str) -> Any:
        if symbol in bars:
            return bars[symbol]
        suffix = StockCodeUtil.to_suffix(symbol)
        if suffix in bars:
            return bars[suffix]
        prefix = StockCodeUtil.to_prefix(symbol)
        return bars.get(prefix)

    def _build_account(self, data: dict[str, Any]) -> SimulationAccount:
        """构建账户对象"""
        return SimulationAccount(
            cash=float(data.get("cash", 0)),
            total_asset=float(data.get("total_asset", 0)),
            positions=data.get("positions", {}) or {},
        )

    def _apply_risk_buy_locks(
        self,
        orders: list[Order],
        *,
        tenant: str,
        user_id: str,
        trade_date: date,
    ) -> list[Order]:
        """Drop strategy buys blocked by an intraday risk lock."""
        try:
            from backend.services.live_trading.services.risk_lock import (
                filter_buy_orders,
                load_risk_locks,
            )

            locks = load_risk_locks(self.redis, tenant, user_id, trade_date)
            if not locks.account_frozen and not locks.symbols:
                return orders
            kept = filter_buy_orders(orders, locks)
            dropped = len(orders) - len(kept)
            if dropped:
                logger.info(
                    "SimulationEngine: 风控禁买过滤 tenant=%s user=%s dropped=%d frozen=%s",
                    tenant,
                    user_id,
                    dropped,
                    locks.account_frozen,
                )
            return kept
        except Exception as exc:
            logger.warning("SimulationEngine: 读取风控禁买锁失败: %s", exc)
            return orders

    async def _execute_order(
        self,
        db: AsyncSession,
        exec_engine: SimulationExecutionEngine,
        order: Order,
        tenant_id: str,
        user_id: str,
        strategy_id: str,
        market: Any = None,
        run_id: str = "",
        bar: Any = None,
    ) -> ExecutionResult:
        """执行单个订单（T-P2-01：改经 OrderRouter 唯一入口——幂等/落账/镜像收口在链内）"""
        from backend.services.simulation.services.order_router import (
            OrderRequest,
            submit_order,
        )
        from backend.shared.errfmt import locate
        from backend.shared.order_contract import (
            SOURCE_REBALANCE,
            build_sim_client_order_id,
        )

        routed = await submit_order(
            db,
            self.redis,
            OrderRequest(
                tenant_id=tenant_id,
                user_id=int(user_id) if str(user_id).isdigit() else 0,
                symbol=order.symbol,
                side=order.side,
                quantity=order.quantity,
                order_type="market",
                price=order.price,
                source=SOURCE_REBALANCE,
                client_order_id=build_sim_client_order_id(
                    run_id, order.symbol, order.side
                ),
                strategy_id=int(strategy_id) if str(strategy_id).isdigit() else None,
                remarks=(
                    str(order.reason).strip()[:500]
                    if getattr(order, "reason", None)
                    else None
                )
                or "策略托管自动调仓",
                bar=bar,
                run_id=run_id,
                mirror=True,
                mirror_source="simulation_engine",
            ),
        )
        if routed.duplicate:
            logger.info(
                locate(
                    "RULE:SIM-DEDUP",
                    "重复调仓单已存在，幂等跳过",
                    ref=str(routed.client_order_id or ""),
                    where="simulation/engine.py:_execute_order",
                )
            )
        if not routed.success:
            logger.warning(
                locate(
                    "RULE:SIM-EXEC",
                    f"模拟单被拒: {routed.message}",
                    ref=str(routed.order_id or ""),
                    where="simulation/engine.py:_execute_order",
                )
            )
        return ExecutionResult(
            success=routed.success,
            price=routed.fill_price,
            quantity=routed.filled_quantity,
            commission=routed.commission,
            price_source=routed.price_source,
            message=routed.message,
        )

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

    async def _sync_snapshot(self, tenant_id: str, user_id: str, market: Any = None) -> None:
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
