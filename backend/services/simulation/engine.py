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
from backend.services.live_trading.services.real_mirror_service import (
    mirror_virtual_fill,
)
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
    canonical_sim_uid,
)
from backend.services.trade_shared.trade_config import settings
from backend.shared.database_manager_v2 import get_session
from backend.shared.stock_utils import StockCodeUtil
from backend.shared.strategy_storage import get_strategy_storage_service

logger = logging.getLogger(__name__)

_STRATEGY_KWARG_KEYS = (
    "topk",
    "n_drop",
    "rebalance_days",
    "weight_mode",
    "min_score",
    "max_position_pct",
    "lot_size",
    "custom_weights",
    "enable_min_score",
    "renormalize_weights",
    "deterministic_buy_order",
    "force_exit_on_limit_down",
)


def _extract_strategy_config_kwargs(code_str: str) -> dict[str, Any]:
    """从策略代码 STRATEGY_CONFIG.kwargs 提取选股参数（topk/n_drop 等）。"""
    import ast

    if not code_str or not str(code_str).strip():
        return {}
    try:
        tree = ast.parse(code_str)
    except Exception:
        return {}
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(isinstance(t, ast.Name) and t.id == "STRATEGY_CONFIG" for t in targets):
            continue
        try:
            cfg = ast.literal_eval(node.value)
        except Exception:
            return {}
        if not isinstance(cfg, dict):
            return {}
        kwargs = cfg.get("kwargs") if isinstance(cfg.get("kwargs"), dict) else {}
        merged = {**kwargs}
        for key in _STRATEGY_KWARG_KEYS:
            if key in cfg and key not in merged:
                merged[key] = cfg[key]
        return {k: merged[k] for k in _STRATEGY_KWARG_KEYS if k in merged}
    return {}


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


async def count_sim_orders_today(db: AsyncSession, tenant: str, uid_int: int) -> int:
    """当日模拟单计数（排除已拒绝，与实盘口径对齐），失败返回 0。"""
    try:
        from datetime import datetime as _dt

        from sqlalchemy import func, select

        from backend.services.simulation.models.order import OrderStatus, SimOrder

        today_start = _dt.combine(_dt.now().date(), _dt.min.time())
        stmt = (
            select(func.count(SimOrder.id))
            .where(SimOrder.tenant_id == tenant)
            .where(SimOrder.user_id == uid_int)
            .where(SimOrder.created_at >= today_start)
            .where(SimOrder.status != OrderStatus.REJECTED)
        )
        res = await db.execute(stmt)
        return int(res.scalar() or 0)
    except Exception as exc:  # noqa: BLE001
        logger.warning("SimulationEngine: 当日订单计数失败, 按 0 处理: %s", exc)
        return 0


async def check_sim_order_gate(
    db: AsyncSession,
    redis,
    *,
    tenant_id: str,
    user_id: object,
    symbol: str,
    quantity: float,
    price: float | None,
    market_str: str | None = None,
) -> list[str]:
    """手动下单 / 沙箱信号共用的模拟闸门检查，返回 violations（空=放行）。

    与托管调仓（SimulationEngine.run_cycle）同一规则口径。市价单无价
    格时算不出金额，只执行日笔数限制，其余 fail-open。永不抛异常。
    """
    try:
        import os as _os

        if _os.getenv("SIM_GATE_RISK_ENABLED", "true").strip().lower() in {
            "0",
            "false",
            "no",
            "off",
        }:
            return []

        from backend.services.live_trading.services.risk_rule_types import (
            GATE_RULE_TYPES,
            rule_matches_market,
            rule_matches_trading_mode,
        )
        from backend.services.live_trading.services.risk_service import RiskService
        from backend.services.simulation.services.market_rules import (
            infer_market_from_symbols,
        )
        from backend.services.simulation.services.simulation_manager import (
            SimulationAccountManager,
            canonical_sim_uid,
        )

        tenant = (tenant_id or "").strip() or "default"
        uid_int = canonical_sim_uid(user_id)
        mkt = market_str or infer_market_from_symbols([symbol]).value

        rules = await RiskService(db, redis).get_applicable_rules(uid_int)
        gate_rules = []
        for rule in rules:
            if str(getattr(rule, "rule_type", "")) not in GATE_RULE_TYPES:
                continue
            params = getattr(rule, "parameters", None) or {}
            if not isinstance(params, dict):
                continue
            if not rule_matches_trading_mode(params.get("trading_mode"), "SIMULATION"):
                continue
            if not rule_matches_market(params.get("markets"), mkt):
                continue
            gate_rules.append(rule)
        if not gate_rules:
            return []

        account = await SimulationAccountManager(redis).get_account(
            user_id=uid_int, tenant_id=tenant, market=mkt
        )
        portfolio_value = float((account or {}).get("total_asset") or 0)
        day_count = await count_sim_orders_today(db, tenant, uid_int)

        order_value = float(quantity or 0) * float(price or 0)
        if order_value <= 0:
            # 市价无价单：金额类规则无法评估，只执行日笔数限制
            gate_rules = [
                r for r in gate_rules if str(getattr(r, "rule_type", "")) == "max_daily_trades"
            ]
            if not gate_rules:
                return []
        return RiskService.eval_gate_violations(
            order_value, portfolio_value, day_count, gate_rules
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("SimulationEngine: 手动单闸门检查失败, fail-open: %s", exc)
        return []


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

    def _ensure_redis(self) -> None:
        """模块级单例默认是未 connect 的 RedisClient，bootstrap 必须接到 trade Redis。"""
        if getattr(self.redis, "client", None) is not None:
            return
        from backend.services.trade_shared.redis_client import get_redis

        connected = get_redis()
        if getattr(connected, "client", None) is None:
            return
        self.redis = connected
        self.account_manager = SimulationAccountManager(self.redis)

    async def run_cycle(
        self,
        tenant_id: str,
        user_id: str,
        strategy_id: str,
        run_id: str | None = None,
        params_override: dict[str, Any] | None = None,
        pool_id: str | None = None,
        signal_run_id: str | None = None,
        allow_stale_quotes: bool | None = None,
        max_orders: int | None = None,
    ) -> ExecutionReport:
        """
        执行一次模拟盘调仓周期。

        Args:
            tenant_id: 租户 ID
            user_id: 用户 ID
            strategy_id: 策略 ID
            run_id: 本轮执行 ID（订单备注/任务追踪），不是推理批次
            signal_run_id: 指定推理信号批次；None 则取最新截面
            params_override: 前端传递的策略参数覆盖
            allow_stale_quotes: 允许用本地日线兜底（bootstrap 盘后建仓）；
                None 时若 run_id 以 bootstrap_ 开头则自动开启
            max_orders: 单轮订单数上限（对应 live_trade_config.max_orders_per_cycle）。
                卖单（减仓/风控）优先且不占额度，买入超出部分被丢弃。

        Returns:
            执行报告
        """
        tenant = (tenant_id or "").strip() or "default"
        uid = str(user_id or "").strip()
        now = datetime.now()
        exec_run_id = run_id or f"sim_{now.strftime('%Y%m%d%H%M%S')}"
        stale_ok = (
            bool(allow_stale_quotes)
            if allow_stale_quotes is not None
            else str(exec_run_id).startswith("bootstrap_")
        )

        report = ExecutionReport(
            tenant_id=tenant,
            user_id=uid,
            strategy_id=strategy_id,
            run_id=exec_run_id,
            executed_at=now,
        )

        try:
            async with get_session() as db:
                # 1. 加载信号
                signals = await self.signal_loader.load_latest_signals(
                    db=db,
                    tenant_id=tenant,
                    user_id=uid,
                    run_id=signal_run_id,
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
                market = infer_market_from_symbols([s.symbol for s in signals])
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
                self._ensure_redis()
                account_data = await self.account_manager.get_account(
                    user_id=canonical_sim_uid(uid),
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
                quotes, live_ticks = await self._load_live_quotes(symbols)
                if stale_ok and len(live_ticks) < len(symbols):
                    # Bootstrap / 盘后：实时行情停更后往往只剩零星几只新鲜
                    # （实测 2/2827）。原实现只在「实时全空」时才用本地日线兜底，
                    # 部分覆盖时行情字典里就只有那几只，而选股阶段 _is_tradable
                    # 只认 quotes 里存在的标的——于是「盘后允许用陈旧价格建仓」
                    # 这个意图在选股阶段就被卡死，可交易标的被压到个位数。
                    # 改为按缺失标的补齐本地日线，实时行情优先不被覆盖。
                    missing = [s for s in symbols if s not in quotes]
                    if missing:
                        bars = await self._load_bars(missing, market=market)
                        bar_quotes = self._quotes_from_bars(bars)
                        bar_ticks = self._ticks_from_bars(bars)
                        if bar_quotes:
                            quotes = {**bar_quotes, **quotes}
                            for key, value in bar_ticks.items():
                                live_ticks.setdefault(key, value)
                            logger.warning(
                                "SimulationEngine: realtime partial, stale local "
                                "bars 补齐 tenant=%s user=%s symbols=%d "
                                "realtime=%d bars=%d (bootstrap/stale allowed)",
                                tenant,
                                uid,
                                len(symbols),
                                len(live_ticks),
                                len(bar_quotes),
                            )
                if not quotes:
                    report.error = "realtime_quote_unavailable"
                    logger.error(
                        "SimulationEngine: no fresh realtime quote; cycle rejected "
                        "tenant=%s user=%s symbols=%d",
                        tenant,
                        uid,
                        len(symbols),
                    )
                    return report

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
                orders = self._apply_max_buy_drop_gate(
                    orders, live_ticks=live_ticks, tenant=tenant, user_id=uid
                )
                if max_orders is not None and int(max_orders) > 0:
                    orders = self._truncate_orders(orders, int(max_orders))
                report.order_count = len(orders)

                # 闸门风控（管理后台 risk_rules）：规则只加载一次，逐单评估。
                # SELL 同样受单笔上下限约束（与 RiskService 口径一致）。
                market_str = str(getattr(market, "value", market) or "CN")
                gate_rules: list = []
                sim_day_count = 0
                if self._sim_gate_enabled():
                    gate_rules = await self._load_sim_gate_rules(
                        db, canonical_sim_uid(uid), market_str
                    )
                    if gate_rules:
                        sim_day_count = await self._sim_daily_order_count(
                            db, tenant, canonical_sim_uid(uid)
                        )
                        logger.info(
                            "SimulationEngine: 闸门风控生效 tenant=%s user=%s "
                            "rules=%d day_count=%d",
                            tenant,
                            uid,
                            len(gate_rules),
                            sim_day_count,
                        )

                if not orders:
                    logger.info(
                        "SimulationEngine: 无需调仓, tenant=%s user=%s",
                        tenant,
                        uid,
                    )
                    return report

                # 6. 模拟撮合（ashare_matcher + 当日不复权日 K）
                exec_engine = SimulationExecutionEngine(db, self.account_manager)
                failed_orders: list[str] = []
                for order in orders:
                    # 单笔失败不能拖垮整个调仓批次。
                    # 注意：不要用 begin_nested/SAVEPOINT —— ExecutionEngine 的
                    # apply_filled/mark_rejected 内部各自 await self.db.commit()，
                    # 会提前释放 SAVEPOINT 并使回滚失效；已成交单也因此早已独立落库。
                    # 这里只做异常隔离 + 定向回滚本笔残留即可。
                    try:
                        result = await self._execute_order(
                            db=db,
                            exec_engine=exec_engine,
                            order=order,
                            tenant_id=tenant,
                            user_id=uid,
                            strategy_id=strategy_id,
                            market=market,
                            run_id=exec_run_id,
                            live_tick=self._tick_for_symbol(
                                live_ticks, order.symbol
                            ),
                            allow_stale_fill=stale_ok,
                            gate_rules=gate_rules,
                            portfolio_value=float(getattr(account, "total_asset", 0) or 0),
                            daily_trade_count=sim_day_count,
                        )
                        # 被闸门拦截的不计入当日笔数（与实盘排除 REJECTED 口径对齐），
                        # 其余（成交/撮合失败）均计入，避免同轮内笔数限制被绕过。
                        if not (
                            not result.success
                            and str(result.message or "").startswith("risk_blocked:")
                        ):
                            sim_day_count += 1
                    except Exception as exc:  # noqa: BLE001
                        failed_orders.append(str(order.symbol))
                        logger.exception(
                            "SimulationEngine: 单笔执行异常已隔离 "
                            "tenant=%s user=%s run=%s symbol=%s side=%s err=%s",
                            tenant,
                            uid,
                            exec_run_id,
                            order.symbol,
                            order.side,
                            exc,
                        )
                        try:
                            await db.rollback()
                        except Exception:  # noqa: BLE001
                            logger.warning(
                                "SimulationEngine: 异常后回滚失败, "
                                "tenant=%s user=%s symbol=%s",
                                tenant,
                                uid,
                                order.symbol,
                            )
                        result = ExecutionResult(
                            success=False,
                            message=f"order_failed: {exc}"[:500],
                        )
                    report.orders.append(self._order_to_dict(order, result))
                    if result.success:
                        report.filled_count += 1
                        report.total_commission += result.commission
                    else:
                        report.rejected_count += 1

                if failed_orders:
                    logger.warning(
                        "SimulationEngine: 本轮 %d/%d 笔异常中止 %s",
                        len(failed_orders),
                        len(orders),
                        ",".join(failed_orders[:20]),
                    )
                    report.error = report.error or (
                        f"{len(failed_orders)} 笔订单执行异常已隔离"
                    )

                await db.commit()

                # 7. 同步快照
                await self._sync_snapshot(tenant, uid, market)

                # 8. 更新账户快照
                updated_account = await self.account_manager.get_account(
                    user_id=canonical_sim_uid(uid),
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

        参数优先级：params_override > strategies.parameters >
        STRATEGY_CONFIG.kwargs（策略代码）> StrategyConfig 默认值。
        """
        rules = rules_for(market)
        default_lot = rules.lot_size
        # 默认配置
        config = StrategyConfig(lot_size=default_lot)
        code_kwargs: dict[str, Any] = {}

        try:
            # 尝试从策略存储服务加载
            storage_svc = get_strategy_storage_service()
            strategy = await storage_svc.get(
                strategy_id=int(strategy_id) if strategy_id.isdigit() else strategy_id,
                user_id=user_id,
            )

            if strategy:
                code_kwargs = _extract_strategy_config_kwargs(
                    str(strategy.get("code") or "")
                )
                params = dict(strategy.get("parameters", {}) or {})
                # 代码里写的 topk/n_drop 补进 parameters（parameters 显式值优先）
                for key in _STRATEGY_KWARG_KEYS:
                    if key not in params and key in code_kwargs:
                        params[key] = code_kwargs[key]
                config = StrategyConfig(
                    topk=int(params.get("topk", 10)),
                    weight_mode=WeightMode(params.get("weight_mode", "equal")),
                    custom_weights=params.get("custom_weights", {}),
                    min_score=float(params.get("min_score", 0.0)),
                    max_position_pct=float(params.get("max_position_pct", 0.15)),
                    lot_size=int(params.get("lot_size", default_lot)),
                    n_drop=int(params.get("n_drop", 0) or 0),
                    rebalance_days=max(1, int(params.get("rebalance_days", 1) or 1)),
                    enable_min_score=bool(params.get("enable_min_score", False)),
                    renormalize_weights=bool(params.get("renormalize_weights", False)),
                    deterministic_buy_order=bool(
                        params.get("deterministic_buy_order", False)
                    ),
                    force_exit_on_limit_down=bool(
                        params.get("force_exit_on_limit_down", False)
                    ),
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
            if params_override.get("n_drop") is not None:
                config.n_drop = int(params_override["n_drop"])
            if params_override.get("rebalance_days") is not None:
                config.rebalance_days = max(1, int(params_override["rebalance_days"]))
            for flag in (
                "enable_min_score",
                "renormalize_weights",
                "deterministic_buy_order",
                "force_exit_on_limit_down",
            ):
                if params_override.get(flag) is not None:
                    setattr(config, flag, bool(params_override[flag]))
            logger.info(
                "SimulationEngine: 应用参数覆盖, topk=%d n_drop=%d weight_mode=%s",
                config.topk,
                config.n_drop,
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

    async def _load_live_quotes(
        self, symbols: list[str]
    ) -> tuple[dict[str, Quote], dict[str, dict[str, Any]]]:
        from backend.services.simulation.services.redis_series_quote import (
            fetch_series_ticks,
            fetch_snapshot_ticks,
        )

        ticks = dict(await fetch_series_ticks(symbols))
        missing = [symbol for symbol in dict.fromkeys(symbols) if symbol not in ticks]
        if missing:
            # 全市场快照不依赖 WS 订阅落序列；与手动撮合共用新鲜度检查。
            ticks.update(await fetch_snapshot_ticks(missing))
        quotes: dict[str, Quote] = {}
        indexed_ticks: dict[str, dict[str, Any]] = {}
        for symbol, tick in ticks.items():
            price = float(tick.get("price") or 0.0)
            if price <= 0:
                continue
            quote = Quote(symbol=symbol, current_price=price)
            for key in {
                symbol,
                StockCodeUtil.to_prefix(symbol),
                StockCodeUtil.to_suffix(symbol),
            }:
                if key:
                    quotes[key] = quote
                    indexed_ticks[key] = tick
        logger.info(
            "SimulationEngine: fresh realtime quotes %d/%d",
            len(ticks),
            len(symbols),
        )
        return quotes, indexed_ticks

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
            suffix = StockCodeUtil.normalize(sym)
            if suffix and suffix != sym:
                quotes[suffix] = quote
        logger.info("SimulationEngine: 本地行情 %d bars", len(bars))
        return quotes

    @staticmethod
    def _ticks_from_bars(bars: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """将本地日线 bar 转成 execute_order 可用的 tick 字典（bootstrap 兜底）。"""
        ticks: dict[str, dict[str, Any]] = {}
        for sym, bar in bars.items():
            price = float(getattr(bar, "close", 0) or 0)
            if price <= 0:
                continue
            tick = {
                "price": price,
                "price_source": "local_daily_close",
                "timestamp": None,
                "age_s": None,
            }
            for key in {
                sym,
                StockCodeUtil.to_prefix(sym),
                StockCodeUtil.to_suffix(sym),
            }:
                if key:
                    ticks[key] = tick
        return ticks

    @staticmethod
    def _bar_for_symbol(bars: dict[str, Any], symbol: str) -> Any:
        if symbol in bars:
            return bars[symbol]
        suffix = StockCodeUtil.to_suffix(symbol)
        if suffix in bars:
            return bars[suffix]
        prefix = StockCodeUtil.to_prefix(symbol)
        return bars.get(prefix)

    @staticmethod
    def _tick_for_symbol(
        ticks: dict[str, dict[str, Any]], symbol: str
    ) -> dict[str, Any] | None:
        return (
            ticks.get(symbol)
            or ticks.get(StockCodeUtil.to_suffix(symbol))
            or ticks.get(StockCodeUtil.to_prefix(symbol))
        )

    def _build_account(self, data: dict[str, Any]) -> SimulationAccount:
        """构建账户对象"""
        return SimulationAccount(
            cash=float(data.get("cash", 0)),
            total_asset=float(data.get("total_asset", 0)),
            positions=data.get("positions", {}) or {},
        )

    def _sim_gate_enabled(self) -> bool:
        """模拟盘闸门风控开关（默认开，SIM_GATE_RISK_ENABLED=0 可逃生）。"""
        import os as _os

        return _os.getenv("SIM_GATE_RISK_ENABLED", "true").strip().lower() not in {
            "0",
            "false",
            "no",
            "off",
        }

    async def _load_sim_gate_rules(
        self, db: AsyncSession, uid_int: int, market_str: str
    ) -> list:
        """加载适用于模拟盘的闸门类规则（SIMULATION/BOTH + 市场匹配）。

        失败时 fail-open 返回 []，避免风控表异常拖垮整轮调仓。
        """
        try:
            from backend.services.live_trading.services.risk_rule_types import (
                GATE_RULE_TYPES,
                rule_matches_market,
                rule_matches_trading_mode,
            )
            from backend.services.live_trading.services.risk_service import RiskService

            rules = await RiskService(db, self.redis).get_applicable_rules(uid_int)
            gated = []
            for rule in rules:
                if str(getattr(rule, "rule_type", "")) not in GATE_RULE_TYPES:
                    continue
                params = getattr(rule, "parameters", None) or {}
                if not isinstance(params, dict):
                    continue
                if not rule_matches_trading_mode(params.get("trading_mode"), "SIMULATION"):
                    continue
                if not rule_matches_market(params.get("markets"), market_str):
                    continue
                gated.append(rule)
            return gated
        except Exception as exc:  # noqa: BLE001
            logger.warning("SimulationEngine: 加载闸门风控规则失败, fail-open: %s", exc)
            return []

    @staticmethod
    def _eval_sim_gate(
        order_value: float,
        portfolio_value: float,
        daily_count: int,
        gate_rules: list,
    ) -> list[str]:
        """评估 4 类闸门规则，与 RiskService.check_order_risk 同口径（仅闸门部分）。"""
        from backend.services.live_trading.services.risk_service import RiskService

        return RiskService.eval_gate_violations(
            order_value, portfolio_value, daily_count, gate_rules
        )

    async def _sim_daily_order_count(
        self, db: AsyncSession, tenant: str, uid_int: int
    ) -> int:
        """当日模拟单计数（排除已拒绝，与实盘口径对齐），失败返回 0。"""
        return await count_sim_orders_today(db, tenant, uid_int)

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

    def _load_execution_risk_config(
        self, tenant: str, user_id: str
    ) -> dict[str, Any]:
        """
        读取用户在活跃策略里保存的执行风控参数（execution_config）。

        与实盘链路同源：Redis 的 active_strategy payload，
        sandbox_signal_consumer / risk_trigger_scanner 均从此处读取。
        读不到时返回空 dict，由调用方走默认阈值。
        """
        try:
            from backend.shared.simulation_account_keys import (
                active_strategy_lookup_keys,
            )

            client = getattr(self.redis, "client", None)
            if client is None:
                return {}
            raw = None
            for key in active_strategy_lookup_keys(tenant, user_id):
                raw = client.get(key)
                if raw:
                    break
            if not raw:
                return {}
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            data = json.loads(raw)
            cfg = (data or {}).get("execution_config") or {}
            return cfg if isinstance(cfg, dict) else {}
        except Exception as exc:
            logger.warning(
                "SimulationEngine: 读取 execution_config 失败, 跳过执行风控: %s",
                exc,
            )
            return {}

    @staticmethod
    def _intraday_pct_change(tick: dict[str, Any] | None) -> float | None:
        """
        估算当日涨跌幅，返回小数（如 -0.10 表示 -10%）。

        Redis series tick 只带当日 open，不带昨收，因此这里用
        (现价 - 今开) / 今开 作为日内跌幅口径。与 qlib runner 的
        pct_chg（相对昨收）存在口径差异，但趋势方向一致；
        拿到昨收后应改为相对昨收计算。
        """
        if not tick:
            return None
        try:
            price = float(tick.get("price") or 0)
            open_price = float(tick.get("open") or 0)
        except (TypeError, ValueError):
            return None
        if price <= 0 or open_price <= 0:
            return None
        return (price - open_price) / open_price

    def _apply_max_buy_drop_gate(
        self,
        orders: list[Order],
        *,
        live_ticks: dict[str, dict[str, Any]],
        tenant: str,
        user_id: str,
    ) -> list[Order]:
        """
        大跌拦截（execution_config.max_buy_drop）。

        语义对齐实盘 RiskGate：买单当日跌幅 <= max_buy_drop 时直接丢弃，
        卖单不受影响；无行情可判定的放行不误杀。
        缺省 -0.03 与 services/trade/runner/risk_gate.py 保持一致。
        """
        cfg = self._load_execution_risk_config(tenant, user_id)
        try:
            threshold = float(cfg.get("max_buy_drop"))
        except (TypeError, ValueError):
            threshold = -0.03
        if not (-0.10 <= threshold <= -0.01):
            logger.warning(
                "SimulationEngine: max_buy_drop=%s 超出范围[-0.10, -0.01], "
                "回退默认 -0.03 tenant=%s user=%s",
                cfg.get("max_buy_drop"),
                tenant,
                user_id,
            )
            threshold = -0.03

        kept: list[Order] = []
        dropped: list[str] = []
        no_quote = 0
        for order in orders:
            if str(order.side).upper() != "BUY":
                kept.append(order)
                continue
            pct = self._intraday_pct_change(
                self._tick_for_symbol(live_ticks, order.symbol)
            )
            if pct is None:
                no_quote += 1
                kept.append(order)
                continue
            if pct <= threshold:
                dropped.append(f"{order.symbol}={pct * 100:.2f}%")
                continue
            kept.append(order)

        if dropped or no_quote:
            logger.info(
                "[Risk] 模拟盘大跌拦截 tenant=%s user=%s threshold=%.2f%% "
                "orders=%d kept=%d dropped=%d no_quote=%d dropped_list=%s",
                tenant,
                user_id,
                threshold * 100,
                len(orders),
                len(kept),
                len(dropped),
                no_quote,
                ",".join(dropped[:20]),
            )
        return kept

    @staticmethod
    def _truncate_orders(orders: list[Order], max_orders: int) -> list[Order]:
        """按单轮上限截断订单：卖单（减仓/风控）优先保留且不占额度。

        ``max_orders_per_cycle`` 此前在模拟盘只被归一化、从无消费点，
        用户设 20 实际仍下 50 单。买入侧按生成顺序（分数降序）截断；
        卖单即使超出上限也全部保留——截断减仓单会让风险敞口关不掉。
        """
        if max_orders <= 0 or len(orders) <= max_orders:
            return orders
        sells = [o for o in orders if str(getattr(o, "side", "")).upper() == "SELL"]
        buys = [o for o in orders if str(getattr(o, "side", "")).upper() != "SELL"]
        room = max_orders - len(sells)
        kept_buys = buys[:room] if room > 0 else []
        if kept_buys or room <= 0:
            logger.info(
                "SimulationEngine: 单轮订单数超限，截断 max=%d "
                "sell=%d buy=%d → keep_buy=%d",
                max_orders,
                len(sells),
                len(buys),
                len(kept_buys),
            )
        return sells + kept_buys

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
        live_tick: dict[str, Any] | None = None,
        allow_stale_fill: bool = False,
        gate_rules: list | None = None,
        portfolio_value: float = 0.0,
        daily_trade_count: int = 0,
    ) -> ExecutionResult:
        """执行单个订单（虚拟撮合；成功后按开关镜像一笔真单到 QMT）

        gate_rules 非空时先过管理后台闸门风控，不通过则落 REJECTED
       （remarks 带 risk_blocked，前端交易记录可见），不进撮合。
        """
        from backend.services.simulation.models.order import (
            OrderSide,
            OrderType,
            SimOrder,
        )

        # 创建订单对象
        sim_order = SimOrder(
            tenant_id=tenant_id,
            user_id=canonical_sim_uid(user_id),
            symbol=order.symbol,
            side=OrderSide.BUY if order.side == "BUY" else OrderSide.SELL,
            order_type=OrderType.MARKET,
            quantity=order.quantity,
            price=order.price,
            strategy_id=int(strategy_id) if strategy_id.isdigit() else None,
            remarks=(str(order.reason).strip()[:500] if getattr(order, "reason", None) else None)
            or "策略托管自动调仓",
        )
        db.add(sim_order)
        await db.flush()
        from backend.services.simulation.models.order_v2 import SimulationOrderV2

        db.add(
            SimulationOrderV2(
                order_id=sim_order.order_id,
                tenant_id=tenant_id,
                user_id=str(sim_order.user_id),
                strategy_id=strategy_id or None,
                account_id=f"sim:{tenant_id}:{sim_order.user_id}",
                portfolio_id=int(sim_order.portfolio_id or 0),
                legacy_order_id=sim_order.id,
                symbol=sim_order.symbol,
                side=sim_order.side.value,
                position_side=str(
                    getattr(
                        getattr(sim_order, "position_side", "long"),
                        "value",
                        getattr(sim_order, "position_side", "long"),
                    )
                    or "long"
                ),
                trade_action=getattr(sim_order, "trade_action", None),
                order_type=sim_order.order_type.value,
                time_in_force="DAY",
                quantity=float(sim_order.quantity or 0.0),
                price=sim_order.price,
                trigger_source="hosted",
                status=sim_order.status.value,
            )
        )
        await db.flush()

        # 闸门风控（管理后台 risk_rules）：在撮合前拦截，落 REJECTED 可审计。
        if gate_rules:
            order_value = float(order.quantity or 0) * float(order.price or 0)
            violations = self._eval_sim_gate(
                order_value, portfolio_value, daily_trade_count, gate_rules
            )
            if violations:
                message = ("risk_blocked: " + "; ".join(violations))[:500]
                logger.info(
                    "SimulationEngine: 闸门风控拦截 tenant=%s user=%s "
                    "symbol=%s side=%s value=%.2f: %s",
                    tenant_id,
                    user_id,
                    order.symbol,
                    order.side,
                    order_value,
                    message,
                )
                await exec_engine.mark_rejected(sim_order, message)
                return ExecutionResult(success=False, message=message)

        # allow_stale_fill 分支下不会走 assess_execution_window，此处先置 None，
        # 避免下游部分成交排队时引用未初始化变量（历史 UnboundLocalError）。
        session_decision = None
        if not allow_stale_fill:
            session_decision = await exec_engine.assess_execution_window(sim_order)
            if not session_decision.can_execute:
                result = ExecutionResult(
                    success=False,
                    message=str(session_decision.message or "outside trading session"),
                )
                await exec_engine.mark_rejected(sim_order, result.message)
                return result

        snapshot = (
            exec_engine.market_snapshot_from_tick(order.symbol, live_tick)
            if live_tick
            else None
        )
        result = await exec_engine.execute_order(
            sim_order,
            market=getattr(market, "value", None),
            snapshot=snapshot,
            allow_stale_market_fill=allow_stale_fill,
        )
        if result.success:
            await exec_engine.apply_filled(sim_order, result)
            if result.quantity + 1e-6 < float(order.quantity or 0.0):
                from backend.services.simulation.services.order_service import (
                    SimOrderService,
                )

                await SimOrderService(db).queue_order(
                    sim_order,
                    "partially_filled; remainder queued for current DAY session",
                    trading_session_date=(
                        session_decision.target_trade_date
                        if session_decision is not None
                        else None
                    ),
                )
            # 双轨镜像：虚拟成交已生效，按开关/白名单/限额向大 QMT 补一笔真单。
            # 用独立会话（db=None），避免真单写入提前提交本周期未完成的虚拟账本；
            # mirror_virtual_fill 自身吞掉全部异常，不影响上面的虚拟成交。
            await mirror_virtual_fill(
                db=None,
                redis=self.redis,
                tenant_id=tenant_id,
                user_id=user_id,
                symbol=order.symbol,
                side=order.side,
                quantity=result.quantity,
                price=float(result.price or order.price or 0),
                sim_order_id=str(sim_order.order_id or ""),
                run_id=run_id,
                strategy_id=strategy_id,
                market=str(getattr(market, "value", market) or ""),
                source="simulation_engine",
            )
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
