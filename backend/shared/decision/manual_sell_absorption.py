"""未归属真单卖出的分账吸收计划（纯核心，不碰 DB）。

背景：``agent_ledger_fill`` 纪律 1——``orders.agent`` 为空的成交（人工在桥里点的
单、风控单）**不写分账**。人工清仓后账本留下幻影仓：读侧（复盘/对话提示词）有
真实账户闸门兜底，但决策轮「已用额度」、实况页 agent 净值、arena 台账仍按账本算。
``backend/scripts/repair_agent_ledger_manual_sells.py`` 用本模块判定并把结果写回
账本（走 ``apply_fill`` 同一实现，现金与回合行口径与真单一致）。

吸收判据（**全部满足**才吸收；任何一条不满足只报告不写）：

1. 账本持仓 > 真实账户持仓（真实持仓由调用方读好传入，与决策轮同一口径）——
   **只吸收差额**；相等即无差额跳过；真实更多属于人工买入，不在本计划器处理。
   （2026-11-23 修订：旧判据要求「该标的已清仓」，导致有底仓时的部分人工卖出
   永远不入账，账本与真实持仓长期背离。）
2. 未归属卖出总量足以覆盖**差额**（不足 = 账本本来就没记那么多，**不猜**）；
3. 账本每一行的 ``buy_ts`` 早于分配到的卖出成交（卖出之后才买的仓不由这笔解释；
   ``buy_ts`` 缺失同样不动——无法证明它当时存在）。

分配：按 ``buy_ts`` FIFO 逐行吃卖出成交（一笔单可跨多家 agent，一家也可跨多笔单），
每一段一条流水。幂等键 ``<prefix>:<order_id>:<agent>``——账本 fill 唯一键是
``(tenant, user, trade_date, fill_key)``，**不含 agent**，故键里必须带 agent：两家
同持一只票时第二家的流水才不会被幂等守卫吞掉。

**为什么单独成模块**：判定与分配是纯函数（无 DB、无时钟、无环境变量），修复脚本
只负责读数、写库与复验。后续做「每日幻影仓体检」时复用同一份判据，不另写一套。
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from typing import Any

from backend.shared.stock_utils import StockCodeUtil

#: 幂等键前缀（体检/审计按它把这类流水与券商成交号区分开）。
FILL_KEY_PREFIX = "manual-absorb"
#: 人工买入归属前缀（窗口内未归属买入 → manual agent 名下）。
MANUAL_BUY_PREFIX = "manual-buy"
#: 期初差异调整前缀（账本与真实持仓在窗口起点就对不上时的一次性调整）。
MANUAL_ADJUST_PREFIX = "manual-adjust"
#: 未归属成交的归属方（不是任何一个模型：真实账户里的人工持仓）。
MANUAL_AGENT = "manual"


def model_agent_names(agents: Any) -> list[str]:
    """账本 agent 集合 → **模型**名（排除 ``manual`` 归属桶），排序返回。

    赛马总控/净值曲线用它：``manual`` 只是让「真实持仓 = 模型分账 + manual」
    成立的对账桶，混进模型排名会把人工单算成模型业绩（2026-11-23 用户口径）。
    """
    out = {
        str(name).strip()
        for name in (agents or ())
        if str(name or "").strip() and str(name).strip() != MANUAL_AGENT
    }
    return sorted(out)


#: 卖出理由（只记录，进回合台账；与真单的 NULL 区分，行为归因可单独过滤）。
EXIT_REASON = "manual_absorb"

#: 浮点比较容差（股数/金额都是两位小数以内的量）。
EPS = 1e-6

# ── 跳过原因（报告与测试共用一份常量）─────────────────────────────────
REASON_REAL_UNAVAILABLE = "real_positions_unavailable"
#: 真实持仓 == 账本持仓（没有差额可吸收）。取代旧「有底仓就跳过」：
#: 部分人工卖出（真实 < 账本）同样要吸收，见 :data:`REASON_NO_GAP` 的引入背景。
REASON_NO_GAP = "real_account_matches_ledger"
#: 保留旧名（外部/历史调用可能 import）；语义已收窄为「无差额」。
REASON_STILL_HELD = REASON_NO_GAP
REASON_NO_SELL_EVIDENCE = "no_unattributed_sells"
REASON_INSUFFICIENT = "unattributed_sells_insufficient"
REASON_BOUGHT_AFTER_SELL = "position_bought_after_sell"
REASON_BUY_TS_UNKNOWN = "position_buy_ts_unknown"

REASON_TEXT = {
    REASON_REAL_UNAVAILABLE: "真实持仓不可读",
    REASON_NO_GAP: "真实持仓与账本一致（无差额）",
    REASON_NO_SELL_EVIDENCE: "窗口内无未归属卖出",
    REASON_INSUFFICIENT: "未归属卖出不足以覆盖账本比真实多出的差额",
    REASON_BOUGHT_AFTER_SELL: "卖出之后才买入",
    REASON_BUY_TS_UNKNOWN: "买入时间未知",
}


def to_float(value: Any) -> float:
    """宽容转 float（``None``/坏值 → 0），只用于读数与比对。"""
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def to_suffix_code(value: Any) -> str:
    """任意写法 → ``600760.SH``（账本持仓列口径）。"""
    return StockCodeUtil.to_suffix(str(value or "").strip())


def as_aware_utc(value: datetime | None) -> datetime | None:
    """统一成 aware UTC；naive 按 UTC 解释（调用方负责把北京墙钟先转好）。"""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class AbsorbFill:
    """一段吸收流水：某 agent 的某只持仓被一笔未归属卖出覆盖的数量。"""

    agent: str
    code: str
    volume: float
    price: float
    order_id: str
    fill_key: str
    trade_date: date
    filled_at: datetime
    cost_price: float
    realized_pnl: float
    closes: bool  # True = 这段把这行持仓清零（回合行 closed 口径）
    #: ``sell`` = 吸收未归属卖出（扣模型分行）；``buy`` = 人工买入归到 manual；
    #: ``adjust`` = 窗口起点账本与真实对不上的期初调整（方向由量价决定）。
    side: str = "sell"

    @property
    def proceeds(self) -> float:
        return round(self.volume * self.price, 2)


@dataclass(frozen=True, slots=True)
class AbsorbSkip:
    code: str
    reason: str
    detail: str


@dataclass(frozen=True, slots=True)
class AbsorbPlan:
    fills: tuple[AbsorbFill, ...] = ()
    skipped: tuple[AbsorbSkip, ...] = ()
    #: 每家模型的回款增量（按流水顺序逐笔 round，与 ``record_sell`` 写库一致）
    proceeds_by_agent: Mapping[str, float] = field(default_factory=dict)
    #: 每家模型的已实现盈亏（同上，逐笔 round）
    pnl_by_agent: Mapping[str, float] = field(default_factory=dict)


def plan_absorption(
    ledger_rows: Sequence[Mapping[str, Any]],
    sells: Sequence[Mapping[str, Any]],
    real_volumes: Mapping[str, float] | None,
    *,
    fill_key_prefix: str = FILL_KEY_PREFIX,
) -> AbsorbPlan:
    """账本持仓 × 未归属卖出 → 吸收计划（纯函数，判据见模块 docstring）。

    :param ledger_rows: ``{agent, code, volume, cost_price, buy_ts}``；``buy_ts`` 为
        aware UTC（``None`` = 不知道，整只票不动）。
    :param sells: ``{order_id, code, volume, price, executed_at, trade_date, agent?}``；
        ``executed_at`` 为 aware UTC；``agent`` 非空的行会被忽略（那是真单落账路径的活）。
    :param real_volumes: ``{suffix_code: volume}``；``None`` = 真实持仓不可读 → 整批不动。
    """
    ledger_by_code: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in ledger_rows:
        code = to_suffix_code(row.get("code"))
        volume = to_float(row.get("volume"))
        if not code or volume <= 0:
            continue
        ledger_by_code[code].append(
            {
                "agent": str(row.get("agent") or ""),
                "code": code,
                "volume": volume,
                "cost_price": to_float(row.get("cost_price")),
                "buy_ts": as_aware_utc(row.get("buy_ts")),
            }
        )

    sells_by_code: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for sell in sells:
        if str(sell.get("agent") or "").strip():
            continue  # 归属卖单不归吸收器管（真单落账路径已经写过或本就不该写）
        code = to_suffix_code(sell.get("code"))
        volume = to_float(sell.get("volume"))
        price = to_float(sell.get("price"))
        executed_at = as_aware_utc(sell.get("executed_at"))
        if not code or volume <= 0 or price <= 0 or executed_at is None:
            continue
        trade_date = sell.get("trade_date")
        if not isinstance(trade_date, date):
            trade_date = executed_at.date()
        sells_by_code[code].append(
            {
                "order_id": str(sell.get("order_id") or ""),
                "code": code,
                "volume": volume,
                "price": price,
                "executed_at": executed_at,
                "trade_date": trade_date,
            }
        )

    skipped: list[AbsorbSkip] = []
    fills: list[AbsorbFill] = []

    for code in sorted(ledger_by_code):
        if real_volumes is None:
            skipped.append(AbsorbSkip(code, REASON_REAL_UNAVAILABLE, ""))
            continue
        real = to_float(real_volumes.get(code))
        rows = ledger_by_code[code]
        ledger_total = round(sum(row["volume"] for row in rows), 4)
        # 只吸收「账本比真实多出来的差额」：真实 ≥ 账本时没有可吸收的卖出
        # （真实更多 = 人工买入，走 manual 买入归属，不在本计划器里处理）。
        gap = round(ledger_total - real, 4)
        if gap <= EPS:
            skipped.append(
                AbsorbSkip(
                    code, REASON_NO_GAP, f"账本 {ledger_total:g} / 真实 {real:g}"
                )
            )
            continue

        if any(row["buy_ts"] is None for row in rows):
            skipped.append(
                AbsorbSkip(
                    code, REASON_BUY_TS_UNKNOWN, "账本行缺 buy_ts，无法证明卖出时存在"
                )
            )
            continue

        code_sells = sorted(
            sells_by_code.get(code, []),
            key=lambda s: (s["executed_at"], s["order_id"]),
        )
        sold_total = round(sum(s["volume"] for s in code_sells), 4)
        if not code_sells:
            skipped.append(AbsorbSkip(code, REASON_NO_SELL_EVIDENCE, ""))
            continue
        if sold_total + EPS < gap:
            skipped.append(
                AbsorbSkip(
                    code,
                    REASON_INSUFFICIENT,
                    f"未归属卖出 {sold_total:g} 股 < 差额 {gap:g} 股",
                )
            )
            continue
        latest_sell = code_sells[-1]["executed_at"]
        late_rows = [row for row in rows if row["buy_ts"] > latest_sell]
        if late_rows:
            skipped.append(
                AbsorbSkip(
                    code,
                    REASON_BOUGHT_AFTER_SELL,
                    f"{late_rows[0]['agent']} 的买入晚于最后一笔未归属卖出",
                )
            )
            continue

        rows_sorted = sorted(rows, key=lambda r: (r["buy_ts"], r["agent"]))
        remaining = [s["volume"] for s in code_sells]
        code_fills: list[AbsorbFill] = []
        starved: dict[str, Any] | None = None
        starved_need = 0.0
        gap_left = gap

        for row in rows_sorted:
            if gap_left <= EPS:
                break
            row_volume = row["volume"]
            need = min(row_volume, gap_left)
            row_fills: list[AbsorbFill] = []
            for idx, sell in enumerate(code_sells):
                if remaining[idx] <= EPS:
                    continue
                if sell["executed_at"] < row["buy_ts"]:
                    continue  # 这笔卖出发生在建仓之前，解释不了这行持仓
                take = min(need, remaining[idx])
                if take <= EPS:
                    continue
                realized = round((sell["price"] - row["cost_price"]) * take, 2)
                row_fills.append(
                    AbsorbFill(
                        agent=row["agent"],
                        code=code,
                        volume=take,
                        price=sell["price"],
                        order_id=sell["order_id"],
                        fill_key=f"{fill_key_prefix}:{sell['order_id']}:{row['agent']}",
                        trade_date=sell["trade_date"],
                        filled_at=sell["executed_at"],
                        cost_price=row["cost_price"],
                        realized_pnl=realized,
                        closes=False,
                    )
                )
                remaining[idx] = round(remaining[idx] - take, 6)
                need = round(need - take, 6)
                if need <= EPS:
                    break
            if need > EPS:
                starved = row
                starved_need = need
                break
            allocated = round(min(row_volume, gap_left) - need, 6)
            # 该行被**差额全量**吸收才标清仓；只吸收一部分时账本仍有该 agent 的仓。
            if allocated + EPS >= row_volume:
                row_fills[-1] = replace(row_fills[-1], closes=True)
            code_fills.extend(row_fills)
            gap_left = round(gap_left - allocated, 6)

        if starved is not None:
            skipped.append(
                AbsorbSkip(
                    code,
                    REASON_INSUFFICIENT,
                    f"{starved['agent']} 缺 {starved_need:g} 股可分配卖出"
                    f"（账本比真实多 {gap:g} 股）",
                )
            )
            continue
        fills.extend(code_fills)

    proceeds: dict[str, float] = defaultdict(float)
    pnl: dict[str, float] = defaultdict(float)
    for fill in fills:
        proceeds[fill.agent] = round(proceeds[fill.agent] + fill.proceeds, 2)
        pnl[fill.agent] = round(pnl[fill.agent] + fill.realized_pnl, 2)

    return AbsorbPlan(
        fills=tuple(fills),
        skipped=tuple(skipped),
        proceeds_by_agent=dict(proceeds),
        pnl_by_agent=dict(pnl),
    )


def _merge_same_key_fills(fills: list[AbsorbFill]) -> list[AbsorbFill]:
    """同一 fill_key 的多段合并成一笔（按量加权价）。

    一笔未归属卖出若跨多个持仓批次（同一 agent 的多段，或期初调整刚补的持仓），
    FIFO 会生成多条 ``manual-absorb:{order_id}:{agent}``。fill_key 是当日唯一幂等键，
    不合并会让 apply_fill 在第二条判重复、整批回滚——2026-11-24 601825 即此形态。
    """
    merged: dict[str, AbsorbFill] = {}
    order: list[str] = []
    for fill in fills:
        prev = merged.get(fill.fill_key)
        if prev is None:
            merged[fill.fill_key] = fill
            order.append(fill.fill_key)
            continue
        total = prev.volume + fill.volume
        if total <= 0:
            continue
        merged[fill.fill_key] = AbsorbFill(
            agent=prev.agent,
            code=prev.code,
            volume=round(total, 6),
            price=(prev.price * prev.volume + fill.price * fill.volume) / total,
            order_id=prev.order_id,
            fill_key=prev.fill_key,
            trade_date=prev.trade_date,
            filled_at=min(prev.filled_at, fill.filled_at),
            cost_price=(prev.cost_price * prev.volume + fill.cost_price * fill.volume)
            / total,
            realized_pnl=round(prev.realized_pnl + fill.realized_pnl, 2),
            closes=prev.closes or fill.closes,
            side=prev.side,
        )
    return [merged[key] for key in order]


def plan_manual_flows(
    ledger_rows: Sequence[Mapping[str, Any]],
    buys: Sequence[Mapping[str, Any]],
    sells: Sequence[Mapping[str, Any]],
    real_volumes: Mapping[str, float] | None,
    *,
    fill_key_prefix: str = FILL_KEY_PREFIX,
    absorbed_order_ids: set[str] | None = None,
) -> AbsorbPlan:
    """把窗口内**未归属买入/卖出 + 期初差异**全部入账，使账本合计 = 真实持仓。

    与 :func:`plan_absorption` 的分工：后者只看「账本 > 真实」的期末差额，会漏掉
    「先卖后买」的人工卖出——期末账本反而**小于**真实持仓（2026-11-23 实况：
    601825 先卖 4500 股、又买 93300 股，期末真实 94300 ≫ 账本 5500）。
    本函数按**事件顺序**入账：

    1. 期初真实 = 期末真实 −（窗口买入 − 窗口卖出）；
    2. 期初账本 ≠ 期初真实 → 先记 ``manual-adjust``（账本多记按 FIFO 扣模型，
       账本少记补 ``manual`` 买入）；
    3. 时间顺序逐笔：未归属卖出按 ``buy_ts`` FIFO 扣账本（先模型、后 manual），
       未归属买入记 ``manual-buy`` 到 :data:`MANUAL_AGENT`；
    4. 期末校验：账本合计 == 真实持仓，否则整只票只报告不写。

    幂等键：``manual-absorb:{order_id}:{agent}`` / ``manual-buy:{order_id}:manual`` /
    ``manual-adjust:{code}:{side}``。
    """

    def _ledger(rows: Sequence[Mapping[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            code = to_suffix_code(row.get("code"))
            volume = to_float(row.get("volume"))
            if not code or volume <= 0:
                continue
            out[code].append(
                {
                    "agent": str(row.get("agent") or ""),
                    "code": code,
                    "volume": volume,
                    "cost_price": to_float(row.get("cost_price")),
                    "buy_ts": as_aware_utc(row.get("buy_ts")),
                }
            )
        return out

    def _events(items: Sequence[Mapping[str, Any]], side: str) -> dict[str, list[dict]]:
        out: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in items:
            if str(item.get("agent") or "").strip():
                continue  # 归属成交走真单落账路径
            if (
                absorbed_order_ids
                and str(item.get("order_id") or "") in absorbed_order_ids
            ):
                continue  # 已入过账（幂等：同一 order 的 manual-* 流水已存在）
            code = to_suffix_code(item.get("code"))
            volume = to_float(item.get("volume"))
            price = to_float(item.get("price"))
            executed_at = as_aware_utc(item.get("executed_at"))
            if not code or volume <= 0 or price <= 0 or executed_at is None:
                continue
            trade_date = item.get("trade_date")
            if not isinstance(trade_date, date):
                trade_date = executed_at.date()
            out[code].append(
                {
                    "order_id": str(item.get("order_id") or ""),
                    "code": code,
                    "volume": volume,
                    "price": price,
                    "executed_at": executed_at,
                    "trade_date": trade_date,
                    "side": side,
                }
            )
        return out

    ledger_by_code = _ledger(ledger_rows)
    buys_by_code = _events(buys, "buy")
    sells_by_code = _events(sells, "sell")

    skipped: list[AbsorbSkip] = []
    fills: list[AbsorbFill] = []
    codes = sorted(
        set(ledger_by_code)
        | set(buys_by_code)
        | set(sells_by_code)
        | set(real_volumes or {})
    )
    for code in codes:
        if real_volumes is None:
            skipped.append(AbsorbSkip(code, REASON_REAL_UNAVAILABLE, ""))
            continue
        real = to_float(real_volumes.get(code))
        rows = ledger_by_code.get(code, [])
        events = sorted(
            buys_by_code.get(code, []) + sells_by_code.get(code, []),
            key=lambda e: (e["executed_at"], e["order_id"]),
        )
        if any(row["buy_ts"] is None for row in rows):
            skipped.append(
                AbsorbSkip(
                    code, REASON_BUY_TS_UNKNOWN, "账本行缺 buy_ts，无法证明卖出时存在"
                )
            )
            continue
        net = round(
            sum(e["volume"] for e in events if e["side"] == "buy")
            - sum(e["volume"] for e in events if e["side"] == "sell"),
            4,
        )
        begin_real = round(real - net, 4)
        ledger_total = round(sum(row["volume"] for row in rows), 4)
        gap0 = round(ledger_total - begin_real, 4)
        holdings = [dict(row) for row in rows]
        stamp = events[-1]["executed_at"] if events else None
        if stamp is None and holdings:
            stamp = holdings[0]["buy_ts"]
        if stamp is None:
            skipped.append(
                AbsorbSkip(code, REASON_NO_SELL_EVIDENCE, "窗口内无未归属成交")
            )
            continue
        item_day = stamp.date()
        code_fills: list[AbsorbFill] = []

        # ① 期初差异：账本 vs 期初真实
        if gap0 > EPS:
            left = gap0
            for held in sorted(holdings, key=lambda h: (h["buy_ts"], h["agent"])):
                if left <= EPS:
                    break
                take = min(held["volume"], left)
                code_fills.append(
                    AbsorbFill(
                        agent=held["agent"],
                        code=code,
                        volume=take,
                        price=held["cost_price"],
                        order_id="",
                        fill_key=f"{MANUAL_ADJUST_PREFIX}:{code}:sell:{held['agent']}",
                        trade_date=item_day,
                        filled_at=stamp,
                        cost_price=held["cost_price"],
                        realized_pnl=0.0,
                        closes=take + EPS >= held["volume"],
                        side="sell",
                    )
                )
                held["volume"] = round(held["volume"] - take, 6)
                left = round(left - take, 6)
        elif gap0 < -EPS:
            need = round(-gap0, 4)
            price = next(
                (e["price"] for e in events if e["side"] == "buy"),
                next((h["cost_price"] for h in holdings if h["cost_price"] > 0), 0.0),
            )
            if price <= 0:
                skipped.append(
                    AbsorbSkip(code, REASON_INSUFFICIENT, "无可用参考价补齐期初差额")
                )
                continue
            code_fills.append(
                AbsorbFill(
                    agent=MANUAL_AGENT,
                    code=code,
                    volume=need,
                    price=price,
                    order_id="",
                    fill_key=f"{MANUAL_ADJUST_PREFIX}:{code}:buy",
                    trade_date=item_day,
                    filled_at=stamp,
                    cost_price=price,
                    realized_pnl=0.0,
                    closes=False,
                    side="buy",
                )
            )
            holdings.append(
                {
                    "agent": MANUAL_AGENT,
                    "code": code,
                    "volume": need,
                    "cost_price": price,
                    "buy_ts": stamp,
                }
            )

        # ② 窗口内未归属成交：按时间顺序入账
        starved: dict[str, Any] | None = None
        starved_need = 0.0
        for event in events:
            if event["side"] == "buy":
                code_fills.append(
                    AbsorbFill(
                        agent=MANUAL_AGENT,
                        code=code,
                        volume=event["volume"],
                        price=event["price"],
                        order_id=event["order_id"],
                        fill_key=f"{MANUAL_BUY_PREFIX}:{event['order_id']}:{MANUAL_AGENT}",
                        trade_date=event["trade_date"],
                        filled_at=event["executed_at"],
                        cost_price=event["price"],
                        realized_pnl=0.0,
                        closes=False,
                        side="buy",
                    )
                )
                holdings.append(
                    {
                        "agent": MANUAL_AGENT,
                        "code": code,
                        "volume": event["volume"],
                        "cost_price": event["price"],
                        "buy_ts": event["executed_at"],
                    }
                )
                continue
            left = event["volume"]
            for held in sorted(holdings, key=lambda h: (h["buy_ts"], h["agent"])):
                if left <= EPS:
                    break
                if held["volume"] <= EPS:
                    continue
                if held["buy_ts"] and event["executed_at"] < held["buy_ts"]:
                    continue
                take = min(held["volume"], left)
                realized = round((event["price"] - held["cost_price"]) * take, 2)
                code_fills.append(
                    AbsorbFill(
                        agent=held["agent"],
                        code=code,
                        volume=take,
                        price=event["price"],
                        order_id=event["order_id"],
                        fill_key=f"{fill_key_prefix}:{event['order_id']}:{held['agent']}",
                        trade_date=event["trade_date"],
                        filled_at=event["executed_at"],
                        cost_price=held["cost_price"],
                        realized_pnl=realized,
                        closes=take + EPS >= held["volume"],
                        side="sell",
                    )
                )
                held["volume"] = round(held["volume"] - take, 6)
                left = round(left - take, 6)
            if left > EPS:
                starved = event
                starved_need = left
                break
        if starved is not None:
            skipped.append(
                AbsorbSkip(
                    code,
                    REASON_INSUFFICIENT,
                    f"未归属卖出 {starved_need:g} 股无账本持仓可扣"
                    f"（期末真实 {real:g}）",
                )
            )
            continue

        after = round(sum(h["volume"] for h in holdings), 4)
        if abs(after - real) > EPS:
            skipped.append(
                AbsorbSkip(
                    code,
                    REASON_INSUFFICIENT,
                    f"入账后账本 {after:g} 股 ≠ 真实 {real:g} 股",
                )
            )
            continue
        fills.extend(code_fills)

    # 同一订单/agent 的多段卖出必须先合并：fill_key 当日唯一，逐段写会自撞幂等键
    fills = _merge_same_key_fills(fills)
    proceeds: dict[str, float] = defaultdict(float)
    pnl: dict[str, float] = defaultdict(float)
    for fill in fills:
        if fill.side != "buy":
            proceeds[fill.agent] = round(proceeds[fill.agent] + fill.proceeds, 2)
            pnl[fill.agent] = round(pnl[fill.agent] + fill.realized_pnl, 2)
    return AbsorbPlan(
        fills=tuple(fills),
        skipped=tuple(skipped),
        proceeds_by_agent=dict(proceeds),
        pnl_by_agent=dict(pnl),
    )


def fill_fields(fill: AbsorbFill) -> dict[str, Any]:
    """``AbsorbFill`` → 构造参数（dataclass 是 frozen 的，改字段只能重建）。"""
    return {
        "agent": fill.agent,
        "code": fill.code,
        "volume": fill.volume,
        "price": fill.price,
        "order_id": fill.order_id,
        "fill_key": fill.fill_key,
        "trade_date": fill.trade_date,
        "filled_at": fill.filled_at,
        "cost_price": fill.cost_price,
        "realized_pnl": fill.realized_pnl,
        "closes": fill.closes,
        "side": fill.side,
    }


def expected_cash(before: float, fills: Sequence[AbsorbFill]) -> float:
    """账本现金的期望值：卖出 ``+volume*price``、买入 ``−volume*price``，逐步 round。"""
    cash = round(to_float(before), 2)
    for fill in fills:
        sign = -1.0 if fill.side == "buy" else 1.0
        cash = round(cash + sign * fill.volume * fill.price, 2)
    return cash
