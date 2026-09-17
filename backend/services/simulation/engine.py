"""
Simulation Engine - 统一模拟盘引擎
信号 → 策略 → 行情 → 调仓 → 撮合 → 账本 → 快照
"""

import asyncio
import json
import logging
import math
import os
from dataclasses import dataclass, field, replace
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
    infer_market,
    infer_market_from_symbols,
    normalize_order_quantity,
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
    # T-FE-05 可调 v2：人工改量的逐条裁定记录（applied/ignored，含原因——不静默）
    quantity_adjustments: list[dict[str, Any]] = field(default_factory=list)


def apply_quantity_overrides(
    orders: list[Order],
    overrides: dict[tuple[str, str], int] | None,
    *,
    exit_order_count: int = 0,
    market: str | None = None,
) -> tuple[list[Order], list[dict[str, Any]]]:
    """人工改量（T-FE-05 审后调整 v2）——**申报数量人工覆盖的唯一实现**。

    纪律（与 exclude_symbols 同族的机构口径）：
    - 退出规则单（前 ``exit_order_count`` 条，kind=exit）**不可改量**——风控动作不接受人工调整；
    - 覆盖键 (symbol, side) 必须命中本轮**真实计算**出的非退出单；两轮之间行情变化可能让
      计划单消失——未命中一律如实记入 ignored，**不猜测、不补单**；
    - 数量经 ``normalize_order_quantity`` 归一（申报单位唯一实现：科创板 200 起 1 股递增，
      主板/创业板整手向下取整）；归一后 ≤0（低于最小申报）该单拒改，如实记录；
    - **不改价格、不改方向、不新增单**——人工只能调量，不能越权构造订单。
    """
    result = list(orders)
    records: list[dict[str, Any]] = []
    if not overrides:
        return result, records

    exit_n = max(0, int(exit_order_count))
    seen_keys: set[tuple[str, str]] = set()
    for idx, order in enumerate(result):
        key = (str(order.symbol), str(order.side).upper())
        if key not in overrides:
            continue
        seen_keys.add(key)
        requested = int(overrides[key])
        if idx < exit_n:
            records.append(
                {
                    "symbol": order.symbol,
                    "side": order.side,
                    "requested": requested,
                    "applied": None,
                    "reason": "退出规则单不可改量（风控动作不绕过）",
                }
            )
            continue
        order_market = market if market else infer_market(order.symbol)
        normalized = normalize_order_quantity(requested, order.symbol, order_market)
        if normalized <= 0:
            records.append(
                {
                    "symbol": order.symbol,
                    "side": order.side,
                    "requested": requested,
                    "applied": None,
                    "reason": "低于最小申报数量，拒改",
                }
            )
            continue
        result[idx] = replace(order, quantity=int(normalized))
        records.append(
            {
                "symbol": order.symbol,
                "side": order.side,
                "requested": requested,
                "from": int(order.quantity),
                "to": int(normalized),
                "applied": True,
            }
        )

    for key, requested in overrides.items():
        if key in seen_keys:
            continue
        records.append(
            {
                "symbol": key[0],
                "side": key[1],
                "requested": int(requested),
                "applied": None,
                "reason": "本次计划中不存在同标的同方向单（行情变化可能已撤单），未应用",
            }
        )
    return result, records


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

    def _ensure_redis(self) -> None:
        """模块级单例默认是未 connect 的 RedisClient，bootstrap 必须接到 trade Redis。

        原地更新 account_manager.redis（不重建管理器对象）：保留实例级注入点
        （测试/联调常以 monkeypatch 替换 manager 方法，重建会静默绕过 mock）。
        """
        if getattr(self.redis, "client", None) is not None:
            return
        from backend.services.trade_shared.redis_client import get_redis

        connected = get_redis()
        if getattr(connected, "client", None) is None:
            return
        self.redis = connected
        self.account_manager.redis = connected

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
        quantity_overrides: dict[tuple[str, str], int] | None = None,
        signal_run_id: str | None = None,
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
            market: 策略市场提示（激活策略 parameters.market）。
                   港股信号 symbol 为裸数字无法靠众数推断，须由调用方显式传入。
            dry_run: **计划预演**（T-FE-05）——走同一 RebalanceCalculator 计算
                （含退出规则与风控买锁），但**不撮合、不落单、不写快照**；
                结果进 ``report.planned_orders``。任何写副作用路径都必须跳过。
            exclude_symbols: 人工排除集（T-FE-05 审后可调 v1）——命中的标的**不参与调仓**；
                **退出规则单不受排除影响**（风控退出不可被人工绕过，机构口径）。
            quantity_overrides: 人工改量（T-FE-05 审后可调 v2），键 (symbol, side) → 申报数量；
                **退出规则单不受改量影响**（风控动作不绕过）；未命中/归一失败不静默——
                逐条裁定记入 ``report.quantity_adjustments``（见 ``apply_quantity_overrides``）。

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
                # 1. 加载信号（market 提示时按市场过滤；signal_run_id 指定批次，None 取最新截面）
                signals = await self.signal_loader.load_latest_signals(
                    db=db,
                    tenant_id=tenant,
                    user_id=uid,
                    run_id=signal_run_id,
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
                if not quotes:
                    # 无新鲜实时行情：回落本地日线 bars（盘后/停更期的计划与预演可用，
                    # 保住 T-FE-05 dry-run/计划链）。**执行**不受影响：取价契约逐单守卫，
                    # strict 市价单遇陈旧价一律拒单（防止按昨收静默成交）。
                    bars = await self._load_bars(symbols, market=market)
                    quotes = self._quotes_from_bars(bars)
                    if not quotes:
                        report.error = "realtime_quote_unavailable"
                        logger.error(
                            "SimulationEngine: no fresh realtime quote and no local bars; "
                            "cycle rejected tenant=%s user=%s symbols=%d",
                            tenant,
                            uid,
                            len(symbols),
                        )
                        return report

                # 4.5 持仓退出评估（T-P2-04 v1）：调仓之前——退出卖单与调仓卖单共用
                # sim-{run}-{sym}-sell 幂等键（退出先记账，调仓重复自动跳过）
                exit_rules = await self._load_exit_ruleset(strategy_id, uid)
                exit_orders = await self._evaluate_position_exits(
                    account, quotes, exit_rules, tenant=tenant, user_id=uid, market=market
                )

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
                # T-P6-12：新闻风险 veto（策略配置 risk.veto.news_event=true 时生效；留痕）
                orders = await self._apply_news_veto(
                    orders, tenant=tenant, user_id=uid, strategy_id=strategy_id
                )
                orders = exit_orders + orders
                report.order_count = len(orders)

                # 4.6 人工改量（T-FE-05 v2）：只动调仓单的数量，退出规则单与价格/方向不可改；
                #     逐条裁定进 report（未命中/拒改不静默）
                if quantity_overrides:
                    orders, adjustments = apply_quantity_overrides(
                        orders,
                        quantity_overrides,
                        exit_order_count=len(exit_orders),
                        market=market,
                    )
                    report.quantity_adjustments = adjustments
                    logger.info(
                        "SimulationEngine: 人工改量 tenant=%s user=%s 裁定=%d 条",
                        tenant,
                        uid,
                        len(adjustments),
                    )

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
                        live_tick=self._tick_for_symbol(live_ticks, order.symbol),
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
            # T-P2-04b：移动止损键兼容两种口径（策略短键 / 实盘 sltp 的 _pct 长键）
            trail = exec_cfg.get("trailing_stop") or exec_cfg.get("trailing_stop_pct")
            if not sl and not tp and not mh and not trail:
                return None
            return ExitRuleSet(
                hard_stop_pct=abs(float(sl)) if sl else None,
                take_profit_pct=abs(float(tp)) if tp else None,
                max_hold_days=int(mh) if mh else None,
                trailing_stop_pct=abs(float(trail)) if trail else None,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("SimulationEngine: 加载退出规则失败 %s", exc)
            return None

    async def _evaluate_position_exits(
        self, account, quotes, exit_rules, *, tenant: str = "default", user_id: str = "", market: Any = None
    ) -> list[Order]:
        """评估持仓退出（唯一实现 exit_rules）；返回卖单（source=sltp），T+1 不可卖跳过。

        T-P2-04b：状态供给经 exit_state_service（开仓日=台账/成交最早，高水位=开仓以来
        不复权日线 high ∪ 持久化值 ∪ 当前价）——trailing/time_stop 从"零填充静默退化"
        变为真实生效；无开仓日历史的旧持仓按周期点名（不静默、不编数据）。
        """
        from backend.shared.errfmt import locate
        from backend.shared.exit_rules import PositionState, evaluate_exit
        from backend.shared.order_contract import SOURCE_SLTP

        if exit_rules is None:
            return []
        # 状态供给（唯一取数实现）：账户键双形态统一经归一后缀式索引
        states: dict = {}
        missing_open_date: list[str] = []
        try:
            from backend.services.simulation.services.exit_state_service import (
                load_symbol_exit_states,
            )

            prices: dict[str, float] = {}
            for sym, pos in (account.positions or {}).items():
                if not isinstance(pos, dict):
                    continue
                quote = quotes.get(sym) or quotes.get(StockCodeUtil.to_prefix(sym))
                if quote is not None and float(quote.current_price or 0) > 0:
                    from backend.services.simulation.services.exit_state_service import (
                        _norm_symbol,
                    )

                    prices[_norm_symbol(sym)] = float(quote.current_price)
            states, missing_open_date = await load_symbol_exit_states(
                redis_like=self.redis,
                tenant_id=tenant,
                user_id=str(user_id),
                market=market,
                positions={s: p for s, p in (account.positions or {}).items() if isinstance(p, dict)},
                last_prices=prices,
            )
        except Exception as exc:  # noqa: BLE001 - 供给失败退回 v1 语义（规则仍跑，缺状态项如实退化）
            logger.warning("SimulationEngine: 退出状态供给失败（退回 entry 近似）: %s", exc)
            states = {}
        if missing_open_date and (exit_rules.max_hold_days or exit_rules.trailing_stop_pct):
            logger.info(
                locate(
                    "RULE:EXIT",
                    f"{len(missing_open_date)} 个持仓无开仓日历史（台账早于 T-P1-04），"
                    f"time_stop 不可用、trailing 以持久化高水位/开仓价近似: "
                    f"{','.join(sorted(missing_open_date)[:5])}",
                    where="simulation/engine.py:_evaluate_position_exits",
                )
            )
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
            from backend.services.simulation.services.exit_state_service import (
                _norm_symbol as _ns,
            )

            st = states.get(_ns(sym))
            decision = evaluate_exit(
                exit_rules,
                PositionState(
                    entry_price=float(pos.get("cost") or 0),
                    last_price=float(quote.current_price),
                    high_water_price=(st.high_water if st is not None else None),
                    hold_days=(st.hold_days if st is not None else None),
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

    async def _load_live_quotes(
        self, symbols: list[str]
    ) -> tuple[dict[str, Quote], dict[str, dict[str, Any]]]:
        from backend.services.simulation.services.redis_series_quote import (
            fetch_series_ticks,
        )

        ticks = await fetch_series_ticks(symbols)
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

    async def _apply_news_veto(
        self,
        orders: list[Order],
        *,
        tenant: str,
        user_id: str,
        strategy_id: str,
    ) -> list[Order]:
        """新闻风险 veto（T-P6-12）：策略开启 ``risk.veto.news_event`` 时拦截当日被风险
        新闻命中的**买单**，并写 risk_events 留痕（rule_type=news_event_veto）。

        保守口径：策略未开启 / veto 集合为空 / 读取失败 → 一律放行（只记日志）。
        """
        if not orders:
            return orders
        try:
            from backend.services.live_trading.services import news_veto

            enabled = await asyncio.to_thread(
                news_veto.strategy_news_veto_enabled, strategy_id
            )
            if not enabled:
                return orders
            vetoes = await asyncio.to_thread(news_veto.load_news_vetoes, self.redis)
            if not vetoes:
                return orders
            kept, dropped = news_veto.filter_news_veto_buys(orders, vetoes)
            if dropped:
                symbols = sorted({str(getattr(o, "symbol", "") or "") for o in dropped})
                logger.info(
                    "SimulationEngine: 新闻veto过滤 tenant=%s user=%s strategy=%s dropped=%d symbols=%s",
                    tenant,
                    user_id,
                    strategy_id,
                    len(dropped),
                    symbols[:8],
                )
                await asyncio.to_thread(
                    news_veto.audit_veto_drop,
                    tenant_id=tenant,
                    user_id=user_id,
                    trade_date=None,
                    symbols=symbols,
                    message="news_event_veto",
                )
            return kept
        except Exception as exc:  # noqa: BLE001 - 过滤失败绝不阻断交易主链（保守放行）
            logger.warning("SimulationEngine: 新闻veto过滤失败（放行）: %s", exc)
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
        live_tick: dict[str, Any] | None = None,
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
