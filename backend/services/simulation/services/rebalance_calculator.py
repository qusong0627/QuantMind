"""
Rebalance Calculator - 调仓计算器
支持 TopK 筛选、TopkDropout 增量调仓、调仓周期、多种权重模式、
涨跌停过滤、先卖后买逻辑。增量调仓与回测引擎 TopkDropoutStrategy 同口径。
"""

import logging
import math
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Any

from backend.services.simulation.services.signal_loader import SignalScore
from backend.services.simulation.services.market_rules import lot_size_for_symbol

logger = logging.getLogger(__name__)


class WeightMode(str, Enum):
    """权重模式"""
    EQUAL = "equal"  # 等权
    SCORE_WEIGHTED = "score_weighted"  # 按 score 加权
    CUSTOM = "custom"  # 自定义权重


@dataclass
class StrategyConfig:
    """策略配置"""
    topk: int = 10
    weight_mode: WeightMode = WeightMode.EQUAL
    custom_weights: dict[str, float] = field(default_factory=dict)
    min_score: float = 0.0
    max_position_pct: float = 0.15  # 单只股票最大仓位比例
    lot_size: int = 100  # 最小交易单位

    # ── 以下为 R2 新增，默认值保持既有行为，实盘路径不受影响 ──
    # 是否启用 min_score 过滤。历史上 min_score 字段存在但从未被读取（死配置），
    # 直接启用会改变实盘选股结果，故用开关显式 opt-in。
    enable_min_score: bool = False
    # 权重被 max_position_pct 砍削后是否重新归一。
    # 关闭时 topk=5 + 等权 → 每只 0.2 砍到 0.15 → 仅投 75%，25% 永久空仓。
    renormalize_weights: bool = False
    # 买单是否按 score 降序生成。关闭时遍历 set()，现金不足时拒单对象随机不可复现。
    deterministic_buy_order: bool = False
    # 持仓中但已跌出目标的标的：即使跌停也生成卖单（由撮合层拒绝并留下记录）。
    # 关闭时跌停持仓在计算阶段就被静默丢弃，"该割的割不掉"且无痕迹。
    force_exit_on_limit_down: bool = False

    # ── TopkDropout 增量调仓（与回测引擎 TopkDropoutStrategy 对齐） ──
    # 每期轮换数量：只剔除跌出 topk 且分数最低的 n_drop 只，只新增
    # 进入 topk 且分数最高的 n_drop 只，避免分数排名波动导致的每期全量轮换。
    # 0 或 >= topk 时退化为每期全量调仓（既有行为）。
    n_drop: int = 0
    # 调仓周期（交易日）：仅当 day_index % rebalance_days == 0 时调仓，
    # 首日（day_index=0）必调仓建仓；1 = 每个交易日都调仓。
    rebalance_days: int = 1

    # ── 基本面 / ST 过滤（模拟盘托管链路；回放路径不读这些字段，不受影响）──
    # f_ 约束（去前缀，如 {"pe_ttm_max": 80}），由 FundamentalAligner 按交易日
    # features_daily 快照执行；与回测 recording_strategy、实盘 manual_execution 同口径。
    fundamental_constraints: dict[str, Any] = field(default_factory=dict)
    # 是否剔除 ST 标的（仅 CN；ST 名单来自本地 instrument_detail 快照）。
    exclude_st: bool = True


@dataclass
class Quote:
    """行情数据"""
    symbol: str
    current_price: float
    is_limit_up: bool = False
    is_limit_down: bool = False
    is_suspended: bool = False
    pre_close: float | None = None


@dataclass
class Order:
    """交易指令"""
    symbol: str
    side: str  # "BUY" | "SELL"
    quantity: int
    price: float
    reason: str = ""


@dataclass
class SimulationAccount:
    """模拟账户快照"""
    cash: float
    total_asset: float
    positions: dict[str, dict[str, Any]]


class RebalanceCalculator:
    """
    调仓计算器：
    0. 调仓周期闸门（rebalance_days，非调仓日不出调仓单）
    1. 按 score 全量排序，跳过不可交易后连续补位至 TopK
    2. TopkDropout 增量轮换（有持仓时每期最多换 n_drop 只）
    3. 计算目标权重（支持多种模式）
    4. 计算目标持仓金额 → 目标股数
    5. 计算买卖指令（先卖后买）
    """

    def calculate(
        self,
        signals: list[SignalScore],
        strategy: StrategyConfig,
        quotes: dict[str, Quote],
        account: SimulationAccount,
        day_index: int = 0,
    ) -> list[Order]:
        """
        计算调仓指令。

        Args:
            signals: 信号列表
            strategy: 策略配置
            quotes: 行情数据 {symbol: Quote}
            account: 当前账户状态
            day_index: 会话内第几个交易日（0 起），用于调仓周期闸门

        Returns:
            交易指令列表（先卖后买）
        """
        if not signals:
            logger.info("RebalanceCalculator: 无信号，跳过调仓")
            return []

        # 0. 调仓周期闸门：非调仓日不产生任何调仓单（止损走 day_runner 独立路径，不受影响）。
        # day_index=0（首日）必建仓，与回测引擎首日全量买入一致。
        if strategy.rebalance_days > 1 and day_index % strategy.rebalance_days != 0:
            logger.debug(
                "RebalanceCalculator: day_index=%d 非调仓日（周期 %d），跳过",
                day_index, strategy.rebalance_days,
            )
            return []

        # 0. min_score 过滤（opt-in，见 StrategyConfig.enable_min_score）
        if strategy.enable_min_score:
            before = len(signals)
            signals = [s for s in signals if s.score >= strategy.min_score]
            if before != len(signals):
                logger.info(
                    "RebalanceCalculator: min_score=%.6f 过滤 %d → %d",
                    strategy.min_score, before, len(signals),
                )
            if not signals:
                logger.info("RebalanceCalculator: min_score 过滤后无信号")
                return []

        # 1–2. 全市场按分排序后跳过不可交易，连续补位直到凑满 topk
        # （禁止先截断宇宙再过滤，否则真 TOP 被滤掉后无法用更低名次补齐）
        ranked = sorted(signals, key=lambda x: x.score, reverse=True)
        topk_signals: list[SignalScore] = []
        for sig in ranked:
            if len(topk_signals) >= strategy.topk:
                break
            if self._is_tradable(sig, quotes):
                topk_signals.append(sig)
        if not topk_signals:
            logger.info("RebalanceCalculator: 无可交易标的，跳过调仓")
            return []
        if len(topk_signals) < strategy.topk:
            logger.info(
                "RebalanceCalculator: 可交易不足 topk=%d，仅选中 %d",
                strategy.topk,
                len(topk_signals),
            )

        # 2.5 TopkDropout 增量轮换：有持仓时不追「当日完整 topk」，只换 n_drop 只，
        #     避免分数排名小幅波动导致每期全量轮换（换手/费用失控）。
        target_signals, dropout_active = self._select_topk_dropout(
            topk_signals, signals, account, strategy
        )

        if dropout_active:
            # 增量模式：保留股不动（不重新配权），剔除股全卖，
            # 新增股用剔除腾出的资金均分 —— 与回测引擎 TopkDropout 一致，
            # 轮换笔数被钉死在每期 ≤ 2×n_drop。
            target_positions = self._calc_dropout_positions(
                target_signals, quotes, account, strategy
            )
        else:
            # 3. 计算目标权重（首日建仓 / 全量调仓路径）
            weights = self._calc_weights(target_signals, strategy)

            # 4. 计算目标持仓
            target_positions = self._calc_target_positions(
                target_signals, weights, quotes, account, strategy
            )

        # 5. 生成调仓指令（先卖后买）
        # score_order 用于买单确定性排序；force_exit 让跌停持仓也能生成卖单
        orders = self._generate_orders(
            account.positions,
            target_positions,
            quotes,
            score_order=[s.symbol for s in target_signals]
            if strategy.deterministic_buy_order
            else None,
            force_exit_on_limit_down=strategy.force_exit_on_limit_down,
        )

        logger.info(
            "RebalanceCalculator: 生成 %d 条指令, 卖出=%d 买入=%d",
            len(orders),
            sum(1 for o in orders if o.side == "SELL"),
            sum(1 for o in orders if o.side == "BUY"),
        )
        return orders

    def _select_topk_dropout(
        self,
        topk_signals: list[SignalScore],
        signals: list[SignalScore],
        account: SimulationAccount,
        strategy: StrategyConfig,
    ) -> tuple[list[SignalScore], bool]:
        """TopkDropout 目标集选择（对齐回测引擎 TopkDropoutStrategy）。

        返回 (目标信号列表, 是否启用增量模式)。
        规则：
        - 无持仓（首日建仓）或关闭增量调仓（n_drop<=0 或 >=topk）→ (完整 topk, False)；
        - 有持仓：
          * 剔除：持仓中跌出 topk 的，按分数升序取最低的 n_drop 只卖出；
          * 新增：topk 中未持仓的，按分数降序取前「剔除数」只买入（持仓数稳定）；
          * 其余持仓保留，无分数也不动。
        与 qlib 差异：qlib 用「上一期 topk 选择列表」判剔除，回放用真实持仓，
        因为成交拒绝/止损后真实持仓与选择列表可能不一致。
        与回测一致的另一面：止损/跌停卖不掉造成的持仓缺口不会主动补满，
        只在后续轮换中渐进修复。
        """
        held_symbols = [
            sym
            for sym, pos in account.positions.items()
            if int(float(pos.get("volume", 0) or 0)) > 0
        ]
        if not held_symbols or not (0 < strategy.n_drop < strategy.topk):
            return topk_signals, False

        score_map = {s.symbol: s.score for s in signals}
        topk_set = {s.symbol for s in topk_signals}
        held_set = set(held_symbols)

        # 剔除：跌出 topk 的持仓，分数最低的 n_drop 只（无分数的排最前）
        out_of_topk = sorted(
            (sym for sym in held_symbols if sym not in topk_set),
            key=lambda sym: score_map.get(sym, float("-inf")),
        )
        drop_set = set(out_of_topk[: strategy.n_drop])

        # 新增：topk 中未持仓的，按分数降序取与剔除等量（没有剔除就不买入）
        add_signals = [
            s for s in topk_signals if s.symbol not in held_set
        ][: len(drop_set)]

        # 保留持仓：有分数用分数，无分数记 0（仅影响 score_weighted 权重）
        sig_date = signals[0].trade_date if signals else date.today()
        kept_signals = [
            SignalScore(
                symbol=sym,
                score=score_map.get(sym, 0.0),
                trade_date=sig_date,
                run_id="rebalance",
                tenant_id="rebalance",
                user_id="rebalance",
            )
            for sym in held_symbols
            if sym not in drop_set
        ]

        logger.info(
            "RebalanceCalculator: TopkDropout 持仓=%d 剔除=%d 新增=%d 目标=%d",
            len(held_symbols), len(drop_set), len(add_signals),
            len(kept_signals) + len(add_signals),
        )
        return kept_signals + add_signals, True

    def _calc_dropout_positions(
        self,
        target_signals: list[SignalScore],
        quotes: dict[str, Quote],
        account: SimulationAccount,
        strategy: StrategyConfig,
    ) -> dict[str, int]:
        """增量调仓的目标持仓：保留股维持现股数，剔除股目标 0（不在目标里即全卖），
        新增股用剔除持仓腾出的资金（按当前价估算）均分、取整手。

        买卖对账（bug fix）：新增只数不再钉死在「计划剔除数」，而是与「实际可释放
        资金的剔除股」对齐——只有「T+1 可卖量 > 0 且行情价有效」的剔除股才计入可
        释放资金，并把新增上限钳到这类标的的数量。否则盘中实时行情覆盖不全时，
        卖了 2 只却仍按 10 只去分摊买 10 只，仓位越调越胖（实测 50→53）。
        """
        kept_set = {s.symbol for s in target_signals}
        positions = account.positions
        target: dict[str, int] = {}

        # 保留股：目标 = 当前股数（不产生调量单）
        # 剔除股：只有「可卖量 > 0 且行情价有效」才计入可释放资金与新增上限。
        # 无行情/T+1 不可卖的剔除股仍会在 _generate_orders 里（按 force_exit 兜底）
        # 挂单留痕，但其资金不计入本轮买入，避免用卖不掉的持仓去超买。
        freed_value = 0.0
        fundable = 0
        for sym, pos in positions.items():
            vol = int(float(pos.get("volume", 0) or 0))
            if vol <= 0:
                continue
            if sym in kept_set:
                target[sym] = vol
                continue
            sell_qty = self._clamp_sell_qty(pos, 0)
            if sell_qty <= 0:
                continue
            quote = quotes.get(sym)
            if not quote or quote.current_price <= 0:
                continue
            freed_value += sell_qty * float(quote.current_price)
            fundable += 1

        # 新增股：目标集里未持仓的（顺序已按分数降序），上限 = 可释放资金标的数
        held_with_vol = {
            sym for sym, pos in positions.items()
            if int(float(pos.get("volume", 0) or 0)) > 0
        }
        add_pool = [s for s in target_signals if s.symbol not in held_with_vol]
        add_list = add_pool[:fundable]
        skipped_unfunded = [s.symbol for s in add_pool[fundable:]]
        skipped_no_quote: list[str] = []
        skipped_zero_qty: list[str] = []
        if add_list and freed_value > 0:
            per_value = freed_value / len(add_list)
            for s in add_list:
                quote = quotes.get(s.symbol)
                if not quote or quote.current_price <= 0:
                    skipped_no_quote.append(s.symbol)
                    continue
                lot = self._lot_for(s.symbol, strategy)
                qty = int(per_value / quote.current_price // lot) * lot
                if qty > 0:
                    target[s.symbol] = qty
                else:
                    skipped_zero_qty.append(s.symbol)
        logger.info(
            "RebalanceCalculator: Dropout 对账 可释放标的=%d 释放资金=%.2f 新增候选=%d unfunded=%s no_quote=%s zero_qty=%s",
            fundable,
            freed_value,
            len(add_list),
            ",".join(skipped_unfunded[:10]),
            ",".join(skipped_no_quote[:10]),
            ",".join(skipped_zero_qty[:10]),
        )
        return target

    def _filter_tradable(
        self,
        signals: list[SignalScore],
        quotes: dict[str, Quote],
    ) -> list[SignalScore]:
        """剔除不可交易标的（保留顺序，供测试/调试复用）。"""
        return [sig for sig in signals if self._is_tradable(sig, quotes)]

    def _is_tradable(
        self,
        sig: SignalScore,
        quotes: dict[str, Quote],
    ) -> bool:
        """单票是否可进入本期目标持仓。

        按信号方向分别过滤：
        - 买入信号：跳过涨停、停牌
        - 卖出信号：跳过跌停、停牌
        - 无方向信号：跳过涨跌停、停牌（保守策略）
        """
        quote = quotes.get(sig.symbol)
        if not quote:
            logger.debug("RebalanceCalculator: %s 无行情数据，跳过", sig.symbol)
            return False
        if quote.is_suspended:
            logger.debug("RebalanceCalculator: %s 停牌，跳过", sig.symbol)
            return False
        if quote.current_price <= 0:
            logger.debug("RebalanceCalculator: %s 价格无效，跳过", sig.symbol)
            return False

        side = getattr(sig, "side", None)
        if side == "BUY" or side == "buy":
            if quote.is_limit_up:
                logger.debug("RebalanceCalculator: %s 涨停，买入跳过", sig.symbol)
                return False
        elif side == "SELL" or side == "sell":
            if quote.is_limit_down:
                logger.debug("RebalanceCalculator: %s 跌停，卖出跳过", sig.symbol)
                return False
        else:
            if quote.is_limit_up or quote.is_limit_down:
                logger.debug(
                    "RebalanceCalculator: %s 涨跌停（up=%s down=%s），跳过",
                    sig.symbol,
                    quote.is_limit_up,
                    quote.is_limit_down,
                )
                return False
        return True

    def _calc_weights(
        self,
        signals: list[SignalScore],
        strategy: StrategyConfig,
    ) -> dict[str, float]:
        """计算目标权重"""
        if not signals:
            return {}

        if strategy.weight_mode == WeightMode.CUSTOM and strategy.custom_weights:
            return strategy.custom_weights

        if strategy.weight_mode == WeightMode.SCORE_WEIGHTED:
            total_score = sum(sig.score for sig in signals)
            if total_score > 0:
                return {
                    sig.symbol: sig.score / total_score
                    for sig in signals
                }
            # 分数全为非正：退化为等权（与下方分支一致）

        # 等权（含默认分支）：分母取 topk 与实际选中数的较大者。
        #
        # 原实现恒为 1/len(signals)，会把「可交易标的不足」直接放大成单票目标：
        # topk=50 而只有 2 只未超龄时每只目标飙到 50%，再被 max_position_pct
        # 削到 15%，两只就吃掉 30% 仓位——而这 2 只只是「恰好行情没超龄」，
        # 并非信号最强。以 topk 为分母则样本不足时自然留出现金，
        # 与回测 topk 等权口径（恒 1/topk）一致。
        n = len(signals)
        basis = max(int(strategy.topk or 0), n, 1)
        if n < basis:
            logger.info(
                "RebalanceCalculator: 可交易 %d 只 < topk=%d，等权按 1/%d 配权"
                "（总目标仓位 %.1f%%，不足部分留现金）",
                n,
                basis,
                basis,
                100.0 * n / basis,
            )
        weight = 1.0 / basis
        return {sig.symbol: weight for sig in signals}

    def _calc_target_positions(
        self,
        signals: list[SignalScore],
        weights: dict[str, float],
        quotes: dict[str, Quote],
        account: SimulationAccount,
        strategy: StrategyConfig,
    ) -> dict[str, int]:
        """计算目标持仓股数"""
        target_positions = {}
        total_asset = account.total_asset

        # bug 6 fix: max_position_pct 与 topk 冲突时的处理。
        # 例：topk=5 等权 → 每只 0.2，被 cap=0.15 砍削后总和 0.75 → 25% 永久空仓。
        # n * cap < 1 说明单票上限与持仓数在数学上无法同时满足，此时以「投满」
        # 为优先，把 cap 放宽到 1/n —— 否则用户设了 topk=5 却永远只有 75% 仓位，
        # 且没有任何提示。cap 仍对个别超配标的（score_weighted 下）生效。
        cap = strategy.max_position_pct
        if strategy.renormalize_weights and weights:
            n = len(weights)
            if n > 0 and n * cap < 1.0:
                relaxed = 1.0 / n
                logger.info(
                    "RebalanceCalculator: max_position_pct=%.4f 与 topk=%d 冲突"
                    "（上限合计 %.2f < 1），放宽单票上限至 %.4f 以避免资金闲置",
                    cap, n, n * cap, relaxed,
                )
                cap = relaxed
            weights = self._waterfill_weights(weights, cap)

        for sig in signals:
            weight = weights.get(sig.symbol, 0)
            if weight <= 0:
                continue

            # 单只股票最大仓位限制
            effective_weight = min(weight, cap)
            target_value = total_asset * effective_weight

            quote = quotes.get(sig.symbol)
            if not quote or quote.current_price <= 0:
                continue

            # 计算目标股数（向下取整到整手）
            raw_quantity = target_value / quote.current_price
            lot_quantity = self._floor_to_lot(
                raw_quantity, self._lot_for(sig.symbol, strategy)
            )

            if lot_quantity > 0:
                target_positions[sig.symbol] = lot_quantity

        return target_positions

    @staticmethod
    def _waterfill_weights(
        weights: dict[str, float],
        cap: float,
        max_rounds: int = 32,
    ) -> dict[str, float]:
        """把超过 cap 的权重溢出量重新分配给未触顶标的，保持总和不变。

        调用方需保证 len(weights) * cap >= 总和，否则全部触顶后溢出量无处安放
        （_calc_target_positions 通过放宽 cap 到 1/n 来保证这一点）。
        """
        total = sum(weights.values())
        if total <= 0:
            return weights
        n = len(weights)
        if n * cap <= total:
            # 容量确实不足（调用方未放宽 cap），全部触顶
            return dict.fromkeys(weights, cap)

        out = dict(weights)
        for _ in range(max_rounds):
            overflow = 0.0
            free: list[str] = []
            for sym, w in out.items():
                if w > cap:
                    overflow += w - cap
                    out[sym] = cap
                elif w < cap:
                    free.append(sym)
            if overflow <= 1e-12 or not free:
                break
            share = overflow / len(free)
            for sym in free:
                out[sym] += share
        return out

    @staticmethod
    def _lot_for(symbol: str, strategy: StrategyConfig) -> int:
        """CN 按板块手数（科创板 200），其它市场用策略默认 lot_size。"""
        return max(1, int(lot_size_for_symbol(symbol) or strategy.lot_size or 100))

    def _floor_to_lot(self, quantity: float, lot_size: int = 100) -> int:
        """向下取整到整手"""
        if quantity <= 0:
            return 0
        return int(quantity // lot_size) * lot_size

    @staticmethod
    def _clamp_sell_qty(current_pos: dict[str, Any], target_qty: int) -> int:
        """按 T+1 可卖量把「当前 - 目标」钳制为实际可卖股数（不可卖返回 0）。"""
        current_qty = int(float(current_pos.get("volume", 0) or 0))
        if current_qty <= target_qty:
            return 0
        sell_qty = current_qty - target_qty
        available = current_pos.get("available_volume")
        if available is not None:
            sell_qty = min(sell_qty, int(float(available)))
        return max(0, sell_qty)

    def _resolve_sell(
        self,
        symbol: str,
        current_pos: dict[str, Any],
        target_qty: int,
        quotes: dict[str, Quote],
        force_exit_on_limit_down: bool,
    ) -> tuple[int, float]:
        """解析一笔减仓的「可卖股数 + 参考价」；不可卖返回 (0, 0.0)。

        - T+1：有 available_volume 时按可用量钳制；
        - 无有效行情价且开启 force_exit_on_limit_down 时，回退持仓价/成本价挂单，
          交由撮合层显式拒绝并留痕，而非在计算阶段静默丢弃；
        - 卖单最终被丢弃时打 WARNING，避免「该割的割不掉」再次无痕。
        """
        current_qty = int(float(current_pos.get("volume", 0) or 0))
        sell_qty = self._clamp_sell_qty(current_pos, target_qty)
        if sell_qty <= 0:
            if current_qty > target_qty:
                logger.warning(
                    "RebalanceCalculator: %s 计划减仓 %d 股，但 T+1 可卖量为 0，"
                    "本期卖单跳过（缺口待后续轮换修复）",
                    symbol,
                    current_qty - target_qty,
                )
            return 0, 0.0

        quote = quotes.get(symbol)
        price = float(quote.current_price) if quote else 0.0
        if price <= 0 and force_exit_on_limit_down:
            pre = current_pos.get("price") or current_pos.get("cost")
            try:
                price = float(pre) if pre else 0.0
            except (TypeError, ValueError):
                price = 0.0
            if price > 0:
                logger.warning(
                    "RebalanceCalculator: %s 无有效行情价，回退持仓价 %.4f 生成卖单"
                    "（交由撮合层判定）",
                    symbol,
                    price,
                )
        if price <= 0:
            logger.warning(
                "RebalanceCalculator: %s 计划减仓 %d 股，但无有效价格且未开启"
                " force_exit_on_limit_down，卖单被丢弃",
                symbol,
                sell_qty,
            )
            return 0, 0.0
        return sell_qty, price

    def _generate_orders(
        self,
        current_positions: dict[str, dict[str, Any]],
        target_positions: dict[str, int],
        quotes: dict[str, Quote],
        score_order: list[str] | None = None,
        force_exit_on_limit_down: bool = False,
    ) -> list[Order]:
        """
        生成调仓指令（先卖后买）。

        逻辑：
        1. 先计算需要卖出的股票（当前持有但不在目标中，或目标数量小于当前）
        2. 再计算需要买入的股票（目标中有但当前没有，或目标数量大于当前）

        score_order 非空时，买单按该顺序生成（bug 5 fix：现金不足时被拒的
        是分数最低的，结果可复现）。为 None 时沿用原 set 遍历行为。

        force_exit_on_limit_down=True 时，跌停的待清仓持仓仍生成卖单
        （bug 4 fix：让撮合层显式拒绝并留下 LIMIT_DOWN 记录，而非计算阶段静默丢弃）。
        """
        sell_orders: list[Order] = []
        buy_orders: list[Order] = []

        current_symbols = sorted(current_positions.keys())

        # 需要卖出的股票
        for symbol in current_symbols:
            current_pos = current_positions.get(symbol, {})
            current_qty = int(float(current_pos.get("volume", 0) or 0))
            target_qty = target_positions.get(symbol, 0)

            sell_qty, price = self._resolve_sell(
                symbol,
                current_pos,
                target_qty,
                quotes,
                force_exit_on_limit_down,
            )
            if sell_qty > 0:
                sell_orders.append(Order(
                    symbol=symbol,
                    side="SELL",
                    quantity=sell_qty,
                    price=price,
                    reason=f"调仓卖出: 当前{current_qty} → 目标{target_qty}",
                ))

        # 需要买入的股票 —— 按 score 降序保证可复现
        if score_order:
            buy_symbols = [s for s in score_order if s in target_positions]
        else:
            buy_symbols = list(target_positions.keys())

        for symbol in buy_symbols:
            target_qty = target_positions.get(symbol, 0)
            current_pos = current_positions.get(symbol, {})
            current_qty = int(float(current_pos.get("volume", 0) or 0))

            if target_qty > current_qty:
                buy_qty = target_qty - current_qty
                quote = quotes.get(symbol)
                price = quote.current_price if quote else 0

                if buy_qty > 0 and price > 0:
                    buy_orders.append(Order(
                        symbol=symbol,
                        side="BUY",
                        quantity=buy_qty,
                        price=price,
                        reason=f"调仓买入: 当前{current_qty} → 目标{target_qty}",
                    ))

        # 先卖后买
        return sell_orders + buy_orders


rebalance_calculator = RebalanceCalculator()
