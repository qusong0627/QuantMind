"""退出规则唯一实现（T-P2-04）：持仓退出的判定、优先级与触发依据快照。

收敛背景：退出规则此前散落 5 处（TDX `check_sltp_trigger`、风控触发器
`evaluate_user_account`、回放 `scan_stop_loss`、回测 `StopLossManager`、策略
execution_config 参数），且**模拟活盘引擎完全没有退出评估**。本模块把
"给定规则集 + 持仓状态 → 是否退出/哪条规则/依据快照"收敛为纯函数。

优先级（先命中先出；与既有 ``check_sltp_trigger`` 求值顺序保持一致：
止盈先于移动止损，二者重叠时报止盈——存量行为兼容）：
  1 hard_stop 硬止损 → 2 take_profit 固定止盈 → 3 trailing_stop 移动止损
  → 4 signal_exit 信号消失 → 5 time_stop 时间止损
设计文档 §2.3 的更高优先级（风控强制/事件退出）由风控闸门层处理，不属本模块。

触发价口径由调用方选择：实时 last_price（盘中/托管）或日线 low（回放）——
同一阈值、同一比较方向；快照留档触发时的全部依据供复盘。
"""

from __future__ import annotations

from dataclasses import dataclass, field

RULE_HARD_STOP = "hard_stop"
RULE_TAKE_PROFIT = "take_profit"
RULE_TRAILING = "trailing_stop"
RULE_SIGNAL = "signal_exit"
RULE_TIME = "time_stop"

RULE_PRIORITY: dict[str, int] = {
    RULE_HARD_STOP: 1,
    RULE_TAKE_PROFIT: 2,
    RULE_TRAILING: 3,
    RULE_SIGNAL: 4,
    RULE_TIME: 5,
}


@dataclass(frozen=True)
class ExitRuleSet:
    """一组退出规则（None/0 表示该规则关闭）。"""

    hard_stop_pct: float | None = None
    take_profit_pct: float | None = None
    trailing_stop_pct: float | None = None
    max_hold_days: int | None = None


@dataclass(frozen=True)
class PositionState:
    """持仓状态。触发价口径：last_price（实时/收盘）或日线 low（由调用方传入对应值）。"""

    entry_price: float
    last_price: float
    high_water_price: float | None = None  # 持仓以来最高价（只升不降，调用方维护）
    hold_days: int | None = None


@dataclass(frozen=True)
class ExitDecision:
    should_exit: bool = False
    rule_id: str = ""
    priority: int = 99
    reason: str = ""
    snapshot: dict = field(default_factory=dict)


def evaluate_exit(
    rules: ExitRuleSet,
    pos: PositionState,
    *,
    signal_gone: bool = False,
    signal_reason: str = "",
) -> ExitDecision:
    """退出判定唯一实现（纯函数）。先命中先出；无命中返回 not should_exit。"""
    entry = float(pos.entry_price or 0.0)
    price = float(pos.last_price or 0.0)
    if entry <= 0 or price <= 0:
        return ExitDecision(snapshot={"error": "entry/price 无效"})

    def _decide(rule_id: str, reason: str, **snap) -> ExitDecision:
        return ExitDecision(
            should_exit=True,
            rule_id=rule_id,
            priority=RULE_PRIORITY[rule_id],
            reason=reason,
            snapshot={"entry": entry, "price": price, **snap},
        )

    # 1. 硬止损（文案与 check_sltp_trigger 保持兼容）
    sl = rules.hard_stop_pct
    if sl and float(sl) > 0:
        line = entry * (1 - float(sl))
        if price <= line:
            return _decide(
                RULE_HARD_STOP, f"止损触发 现价{price:.2f} ≤ {line:.2f}", line=round(line, 4)
            )

    # 2. 固定止盈
    tp = rules.take_profit_pct
    if tp and float(tp) > 0:
        line = entry * (1 + float(tp))
        if price >= line:
            return _decide(
                RULE_TAKE_PROFIT, f"止盈触发 现价{price:.2f} ≥ {line:.2f}", line=round(line, 4)
            )

    # 3. 移动止损（从最高价回撤）
    trail = rules.trailing_stop_pct
    if trail and float(trail) > 0:
        highest = float(pos.high_water_price or entry)
        line = highest * (1 - float(trail))
        if price <= line:
            return _decide(
                RULE_TRAILING,
                f"移动止损 现价{price:.2f} ≤ {line:.2f}（最高 {highest:.2f}）",
                line=round(line, 4),
                high_water=round(highest, 4),
            )

    # 4. 信号消失
    if signal_gone:
        return _decide(RULE_SIGNAL, signal_reason or "信号消失退出")

    # 5. 时间止损
    if rules.max_hold_days and pos.hold_days is not None:
        if int(pos.hold_days) >= int(rules.max_hold_days):
            return _decide(
                RULE_TIME,
                f"持有 {pos.hold_days} 日 ≥ 上限 {rules.max_hold_days} 日",
                hold_days=pos.hold_days,
            )

    return ExitDecision(snapshot={"entry": entry, "price": price})
