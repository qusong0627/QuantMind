"""风控引擎契约（T-RC-01 唯一实现）：判定上下文 / 决策 / 裁决。

设计立场（`docs/风险控制体系_设计方案.md` §一）：**fail-closed**——风控不可用、超时、
状态未知 → 一律拒绝；每条拦截必须可解释（规则 ID + 计算依据 + 快照值）。

**无 str-Enum**（防 `isinstance(x, str)` 恒真陷阱，见 memory `python-str-enum-isinstance-trap`）：
动作/级别一律用模块级常量 + Literal。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal
from collections.abc import Mapping

# ── 动作与级别（常量，非 Enum）─────────────────────────────────────────
ACTION_PASS: Literal["PASS"] = "PASS"
ACTION_REJECT: Literal["REJECT"] = "REJECT"   # 终止该单（fail-closed 的默认动作）
ACTION_WARN: Literal["WARN"] = "WARN"         # 记录并继续
ACTION_HALT: Literal["HALT"] = "HALT"         # 拒单 + 触发全局状态机迁移

Action = Literal["PASS", "REJECT", "WARN", "HALT"]

LEVELS = ("L0", "L1", "L2", "L3", "L4", "L5", "L6")
LEVEL_NAMES = {
    "L0": "系统级",
    "L1": "账户级",
    "L2": "策略级",
    "L3": "订单级",
    "L4": "标的级",
    "L5": "市场级",
    "L6": "数据/模型级",
}


@dataclass(frozen=True)
class Decision:
    """单条规则的判定结果（PASS 不入列；拦截/告警必带规则 ID 与依据）。"""

    rule_id: str
    level: str
    action: str
    reason: str
    evidence: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RiskVerdict:
    """一单的最终裁决：passed=False 即拒单；halt=True 需触发状态机迁移。"""

    passed: bool
    halt: bool
    decisions: tuple[Decision, ...]
    config_version: int
    checked_rules: tuple[str, ...]

    @property
    def rejects(self) -> tuple[Decision, ...]:
        return tuple(d for d in self.decisions if d.action == ACTION_REJECT)

    @property
    def warns(self) -> tuple[Decision, ...]:
        return tuple(d for d in self.decisions if d.action == ACTION_WARN)


@dataclass
class RiskContext:
    """判定上下文（**纯数据**；一切 IO 由适配器在构造时完成，规则只读）。

    缺失语义：L1 资金类字段为 None = 账户快照不可得 → fail-closed 拒单；
    建议类字段（行业集中度等）为 None → 记 WARN（数据可得性问题，不阻断交易）。
    """

    # ── 订单 ──
    market: str = "CN"
    symbol: str = ""
    side: str = "BUY"                      # BUY / SELL
    order_type: str = "LIMIT"              # LIMIT / MARKET
    price: float | None = None
    quantity: int = 0                      # 股
    amount: float | None = None            # 预估金额（元）；缺省 price*quantity
    client_order_id: str = ""
    fingerprint: str = ""                  # 参数指纹（重复单检测）
    forced_exit: bool = False              # 强平单：价格偏离闸门 bypass（保 sanity 上界）
    strategy_id: str = ""
    queued_intent: bool = False            # 盘后入队单：申报时段/行情时效校验延后到派发环节
    price_source: str = ""                 # 价格来源：snapshot / fallback_close / ""（不可得）

    # ── 账户快照（L1）──
    available_cash: float | None = None
    sellable_volume: int | None = None
    total_assets: float | None = None
    # 全账户持仓市值合计（元）—— 总杠杆闸 l1.leverage_cap 的分子；
    # None = 快照没读到持仓结构（≠ 没有持仓，后者是 0.0）→ 该规则 fail-closed 拒买
    total_position_value: float | None = None
    # 该快照的**时点与来源**：只读证据，让规则能说"这个数字是何时、从哪座账户读的"。
    # account_age_s = 判定时刻 − 快照时点（秒）；**None = 时点不可得，不是 0**（0 是"刚更新"）；
    # account_source = tdx_bridge / qmt_exec / sim（"" = 未标注）。
    account_age_s: float | None = None
    account_source: str = ""
    position_pct: float | None = None      # 该标的当前占比（0-1，含本单前）
    industry_pct: float | None = None      # 该行业当前占比（0-1）
    daily_pnl_pct: float | None = None     # 当日盈亏（%，负=亏）

    # ── 行情（L3/L6）──
    last_price: float | None = None
    quote_age_s: float | None = None
    book_crossed: bool = False
    book_empty: bool = False

    # ── 频率/重复（L3）──
    orders_last_minute: int = 0
    orders_today: int = 0
    cancels_today: int = 0
    # 当日已**成交**买入的标的（去重，`l1.new_buys_per_day` 的输入）。
    # None = 计数不可得（查询失败）≠ 空元组（今天还没买过）——买入新仓时 fail-closed，
    # 加仓/卖出不受影响。
    # **代码口径按库原样**（REAL 的 orders 是后缀式、sim_orders 是前缀式），不在构造处
    # 归一：归一必须发生在**比较处**（规则内把两侧都过 `StockCodeUtil.to_prefix`），
    # 否则日后新增的消费者照样会踩"跨层等值匹配静默查空"。
    opened_today: tuple[str, ...] | None = None
    recent_symbol_sides: tuple[tuple[str, str], ...] = ()   # 窗口内 (symbol, side)
    recent_fingerprints: tuple[str, ...] = ()               # 窗口内订单指纹

    # ── 系统（L0/L6）──
    now_ts: float = 0.0                    # 判定时刻（epoch 秒）
    clock_skew_ms: float | None = None
    kill_switch: bool = False
    contract_ok: bool = True

    def order_amount(self) -> float | None:
        if self.amount is not None:
            return float(self.amount)
        if self.price is not None and self.quantity:
            return float(self.price) * int(self.quantity)
        return None
