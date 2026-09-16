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

from backend.services.trade_shared.redis_client import RedisClient, redis_client
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
from backend.shared.database_manager_v2 import get_session
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
    # T-FE-05：计划预演（dry_run=True 时填 planned_orders，不执行撮合/不落账）
    dry_run: bool = False
    planned_orders: list[dict[str, Any]] = field(default_factory=list)


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
        # P0 修复：默认必须引用**共享单例** redis_client（启动时 connect 的那一个）。
        # 此前 `redis or RedisClient()` 每次新建未连接实例 → client 恒 None →
        # 账户/风控/快照全链路拿不到数据（与 db_manager.session 同源的静默断链）。
        self.redis = redis or redis_client
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
        dry_run: bool = False,
        exclude_symbols: set[str] | None = None,
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
            dry_run: **计划预演**（T-FE-05）——走同一 RebalanceCalculator 计算
                （含退出规则与风控买锁），但**不撮合、不落单、不写快照**；
                结果进 ``report.planned_orders``。任何写副作用路径都必须跳过。
            exclude_symbols: 人工排除集（T-FE-05 审后可调 v1）——命中的标的**不参与调仓**；
                **退出规则单不受排除影响**（风控退出不可被人工绕过，机构口径）。

        Returns:
            执行报告（dry_run 时 executed_at 仅为计算时刻）
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
            # P0 修复（T-FE-05 预演实机复现）：DatabaseManager 无 session() API（9-01 重构
            # 引入的 AttributeError 使托管/引导/手动全部模拟周期死在入口，静默进 report.error）。
            # 统一走共享 get_session（master 会话 + 出口提交语义）。
            async with get_session() as db:
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
                if exclude_symbols:
                    from backend.shared.stock_utils import StockCodeUtil as _SCU

                    excluded_norm = set()
                    for _sym in exclude_symbols:
                        excluded_norm.add(str(_sym))
                        excluded_norm.add(_SCU.to_suffix(str(_sym)))
                        excluded_norm.add(_SCU.to_prefix(str(_sym)))
                    before = len(signals)
                    signals = [s for s in signals if s.symbol not in excluded_norm]
                    logger.info(
                        "SimulationEngine: 人工排除 %d 个标的，信号 %d → %d",
                        len(exclude_symbols),
                        before,
                        len(signals),
                    )
                # P0 修复：信号表 symbol 为纯数字（DB 契约），行情/账户/撮合为后缀式。
                # 在引擎边界统一归一（CN → 600036.SH），否则全部信号会因行情键失配
                # 被当"不可交易"过滤——托管周期自 9/13 统一路径起静默空转（零订单）。
                if getattr(market, "value", str(market)).upper() in ("CN", "A"):
                    for _sig in signals:
                        _suffix = StockCodeUtil.to_suffix(_sig.symbol)
                        if _suffix:
                            _sig.symbol = _suffix

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

                # 4.5 持仓退出评估（T-P2-04 v1）：调仓之前——退出卖单与调仓卖单共用
                # sim-{run}-{sym}-sell 幂等键（退出先记账，调仓重复自动跳过）
                exit_rules = await self._load_exit_ruleset(strategy_id, uid)
                exit_orders = self._evaluate_position_exits(account, quotes, exit_rules)

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
                orders = exit_orders + orders
                report.order_count = len(orders)

                if dry_run:
                    # T-FE-05 计划预演：同源计算（退出+调仓+风控买锁）→ 只报告不执行
                    report.dry_run = True
                    report.planned_orders = [
                        self._plan_to_dict(
                            order,
                            quotes,
                            kind="exit" if idx < len(exit_orders) else "rebalance",
                        )
                        for idx, order in enumerate(orders)
                    ]
                    logger.info(
                        "SimulationEngine: 计划预演(未执行), tenant=%s user=%s orders=%d",
                        tenant,
                        uid,
                        len(report.planned_orders),
                    )
                    return report

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

    async def _load_exit_ruleset(self, strategy_id: str, user_id: str):
        """持仓退出规则（T-P2-04 v1）：与实盘隐式止损同源——策略 execution_config。"""
        from backend.shared.exit_rules import ExitRuleSet

        try:
            storage_svc = get_strategy_storage_service()
            strategy = await storage_svc.get(
                strategy_id=int(strategy_id) if str(strategy_id).isdigit() else 0,
                user_id=user_id,
            )
            params = (strategy or {}).get("parameters", {}) or {}
            exec_cfg = params.get("execution_config") or {}
            sl = exec_cfg.get("stop_loss")
            tp = exec_cfg.get("take_profit")
            mh = exec_cfg.get("max_hold_days")
            if not sl and not tp and not mh:
                return None
            return ExitRuleSet(
                hard_stop_pct=abs(float(sl)) if sl else None,
                take_profit_pct=abs(float(tp)) if tp else None,
                max_hold_days=int(mh) if mh else None,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("SimulationEngine: 加载退出规则失败 %s", exc)
            return None

    def _evaluate_position_exits(self, account, quotes, exit_rules) -> list[Order]:
        """评估持仓退出（唯一实现 exit_rules）；返回卖单（source=sltp），T+1 不可卖跳过。"""
        from backend.shared.errfmt import locate
        from backend.shared.exit_rules import PositionState, evaluate_exit
        from backend.shared.order_contract import SOURCE_SLTP

        if exit_rules is None:
            return []
        out: list[Order] = []
        for sym, pos in (account.positions or {}).items():
            if not isinstance(pos, dict):
                continue
            volume = int(float(pos.get("volume") or 0))
            if volume <= 0:
                continue
            quote = quotes.get(sym) or quotes.get(StockCodeUtil.to_prefix(sym))
            if quote is None or float(quote.current_price or 0) <= 0:
                continue
            decision = evaluate_exit(
                exit_rules,
                PositionState(
                    entry_price=float(pos.get("cost") or 0),
                    last_price=float(quote.current_price),
                ),
            )
            if not decision.should_exit:
                continue
            avail = pos.get("available_volume")
            available = int(float(volume if avail is None else avail))
            if available <= 0:  # T+1 锁定中：记录可见，次日解锁再卖
                logger.info(
                    locate(
                        "RULE:EXIT",
                        f"退出信号但 T+1 不可卖 {sym}: {decision.reason}",
                        where="simulation/engine.py:_evaluate_position_exits",
                    )
                )
                continue
            order = Order(
                symbol=sym,
                side="SELL",
                quantity=available,
                price=0.0,
                reason=f"[{decision.rule_id}] {decision.reason}",
            )
            try:
                order.source = SOURCE_SLTP  # 来源分类（交易台/对账可见）
            except Exception:  # noqa: BLE001
                pass
            out.append(order)
            logger.info(
                locate(
                    "RULE:EXIT",
                    f"持仓退出（{decision.rule_id}）{sym} x{available}: {decision.reason}",
                    where="simulation/engine.py:_evaluate_position_exits",
                )
            )
        return out

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
                source=getattr(order, "source", None) or SOURCE_REBALANCE,
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

    @staticmethod
    def _plan_to_dict(order: Order, quotes: dict[str, Quote], *, kind: str) -> dict[str, Any]:
        """调仓指令 → 计划预演条目（T-FE-05）：含理由/触发类别/预估金额与涨跌停状态。"""
        quote = quotes.get(order.symbol)
        price = float(order.price or 0.0)
        return {
            "symbol": order.symbol,
            "side": order.side,
            "quantity": int(order.quantity or 0),
            "price": price,
            "estimated_amount": round(price * int(order.quantity or 0), 2),
            "reason": order.reason or "",
            "kind": kind,  # exit=退出规则触发 | rebalance=定期调仓
            "is_limit_up": bool(getattr(quote, "is_limit_up", False)),
            "is_limit_down": bool(getattr(quote, "is_limit_down", False)),
            "is_suspended": bool(getattr(quote, "is_suspended", False)),
        }

    def _order_to_dict(self, order: Order, result: ExecutionResult) -> dict[str, Any]:
        """订单结果转字典"""
        return {
            "symbol": order.symbol,
            "side": order.side,
            "quantity": order.quantity,
            "price": order.price,
            "reason": order.reason,
            "source": getattr(order, "source", None) or "rebalance",
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
