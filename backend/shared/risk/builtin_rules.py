"""内置风控规则（T-RC-01 首批）：L0 系统级 / L1 账户级 / L3 订单级 / L6 数据级。

口径总纲（与 `docs/风险控制体系_设计方案.md` 对齐）：
- **fail-closed**：资金类/价格新鲜度等关键字段缺失（None）→ REJECT，绝不"缺数据放行"；
- 建议类字段缺失（行业占比等）→ WARN（数据可得性问题可见化，不阻断交易）；
- 规则仅在**配置显式列出**时生效（`always_on` 除外：L0 急停/时段），参数缺省见 default_params；
- 时间一律 epoch 秒 → 中国市场按固定 UTC+8（中国无夏令时）解释。

边界口径（可测）：
- 阈值比较一律"**等于阈值放行、超过才拦**"（`amount > max` 拦，`== max` 过）；
- 数量单位=股；金额单位=元。
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any
from collections.abc import Mapping

from backend.shared.risk.contracts import (
    ACTION_HALT,
    ACTION_REJECT,
    ACTION_WARN,
    Decision,
    RiskContext,
)
from backend.shared.risk.registry import rule

CST = timezone(timedelta(hours=8))

# 浮点边界容差：十进制字面量的二进制表示误差（如 0.10+0.05>0.15）不构成越限——
# 阈值比较一律 `value > limit + _EPS`（"等于阈值放行"口径的可测实现）。
_EPS = 1e-9

# A 股默认申报时段（含集合竞价；午休/盘后拒绝）——按交易所口径可配置
CN_SESSION_DEFAULT = [["09:15", "11:30"], ["13:00", "15:00"]]
HK_SESSION_DEFAULT = [["09:30", "12:00"], ["13:00", "16:00"]]


def _reject(rule_id: str, level: str, reason: str, **evidence: Any) -> Decision:
    return Decision(rule_id=rule_id, level=level, action=ACTION_REJECT, reason=reason, evidence=evidence)


def _warn(rule_id: str, level: str, reason: str, **evidence: Any) -> Decision:
    return Decision(rule_id=rule_id, level=level, action=ACTION_WARN, reason=reason, evidence=evidence)


def _hm_ok(now_hm: str, windows: list[list[str]]) -> bool:
    return any(start <= now_hm < end for start, end in windows)


# ── L0 系统级 ─────────────────────────────────────────────────────────


@rule("l0.kill_switch", "L0", "急停开关：置位时全停（拒新单 + 触发全撤迁移）", always_on=True)
def l0_kill_switch(ctx: RiskContext, params: Mapping[str, Any]) -> Decision | None:
    if ctx.kill_switch:
        return Decision(
            rule_id="l0.kill_switch",
            level="L0",
            action=ACTION_HALT,
            reason="急停开关置位（kill switch）",
            evidence={"kill_switch": True},
        )
    return None


@rule("l0.session", "L0", "交易日/时段校验（周末与场外拒单）", always_on=True)
def l0_session(ctx: RiskContext, params: Mapping[str, Any]) -> Decision | None:
    windows_by_market = params.get(
        "windows", {"CN": CN_SESSION_DEFAULT, "HK": HK_SESSION_DEFAULT}
    )
    market = str(ctx.market or "CN").upper()
    windows = windows_by_market.get(market)
    if not windows:
        return _reject("l0.session", "L0", f"市场时段未配置（{market}），fail-closed", market=market)
    ts = ctx.now_ts or time.time()
    local = datetime.fromtimestamp(ts, tz=CST)
    if str(local.date()) in {str(d) for d in (params.get("holidays") or [])}:
        return _reject("l0.session", "L0", "非交易日（节假日）", date=str(local.date()))
    if local.weekday() >= 5:
        return _reject("l0.session", "L0", "非交易日（周末）", date=str(local.date()))
    now_hm = local.strftime("%H:%M")
    if not _hm_ok(now_hm, windows):
        return _reject("l0.session", "L0", "非申报时段", hm=now_hm, windows=windows)
    return None


@rule("l0.clock_drift", "L0", "时钟漂移校验（与交易所时间偏差超限拒单）", max_skew_ms=500.0)
def l0_clock_drift(ctx: RiskContext, params: Mapping[str, Any]) -> Decision | None:
    if ctx.clock_skew_ms is None:
        return None  # 未测量（适配器可提供）；测量后超限必拦
    max_skew = float(params.get("max_skew_ms", 500.0))
    if abs(float(ctx.clock_skew_ms)) > max_skew:
        return _reject("l0.clock_drift", "L0", "时钟漂移超限",
                       skew_ms=ctx.clock_skew_ms, max_skew_ms=max_skew)
    return None


# ── L1 账户级 ─────────────────────────────────────────────────────────


@rule("l1.available_cash", "L1", "买入可用资金校验（快照缺失=拒，fail-closed）")
def l1_available_cash(ctx: RiskContext, params: Mapping[str, Any]) -> Decision | None:
    if str(ctx.side).upper() != "BUY":
        return None
    if ctx.available_cash is None:
        return _reject("l1.available_cash", "L1", "账户快照缺失（可用资金未知），fail-closed")
    amount = ctx.order_amount()
    if amount is None:
        return _reject("l1.available_cash", "L1", "订单金额不可得（价格/数量缺失），fail-closed")
    if amount > float(ctx.available_cash) + _EPS:
        return _reject("l1.available_cash", "L1", "买入金额超可用资金",
                       amount=amount, available_cash=ctx.available_cash)
    return None


@rule("l1.t1_sellable", "L1", "T+1 可卖量校验（卖出 ≤ 可用持仓）")
def l1_t1_sellable(ctx: RiskContext, params: Mapping[str, Any]) -> Decision | None:
    if str(ctx.side).upper() != "SELL":
        return None
    if ctx.sellable_volume is None:
        return _reject("l1.t1_sellable", "L1", "可卖量未知（快照缺失），fail-closed")
    if int(ctx.quantity) > int(ctx.sellable_volume):
        return _reject("l1.t1_sellable", "L1", "卖出量超可卖持仓",
                       quantity=ctx.quantity, sellable=ctx.sellable_volume)
    return None


@rule("l1.position_cap", "L1", "单票市值上限（占比=持仓+本单 ≤ 上限）", max_pct=0.15)
def l1_position_cap(ctx: RiskContext, params: Mapping[str, Any]) -> Decision | None:
    if str(ctx.side).upper() != "BUY":
        return None
    if ctx.total_assets is None or float(ctx.total_assets) <= 0:
        return _reject("l1.position_cap", "L1", "总资产未知（快照缺失），fail-closed")
    amount = ctx.order_amount()
    if amount is None:
        return _reject("l1.position_cap", "L1", "订单金额不可得，fail-closed")
    max_pct = float(params.get("max_pct", 0.15))
    held = float(ctx.position_pct or 0.0)
    after = held + amount / float(ctx.total_assets)
    if after > max_pct + _EPS:
        return _reject("l1.position_cap", "L1", "单票占比超上限",
                       after_pct=round(after, 4), max_pct=max_pct)
    return None


@rule("l1.industry_cap", "L1", "行业集中度上限（占比未知记 WARN，超限拒）", max_pct=0.30)
def l1_industry_cap(ctx: RiskContext, params: Mapping[str, Any]) -> Decision | None:
    if str(ctx.side).upper() != "BUY":
        return None
    max_pct = float(params.get("max_pct", 0.30))
    if ctx.industry_pct is None:
        return _warn("l1.industry_cap", "L1", "行业占比未知（数据可得性），放行并记录")
    if ctx.total_assets is None:
        return _warn("l1.industry_cap", "L1", "总资产未知，行业占比无法折算，放行并记录")
    amount = ctx.order_amount() or 0.0
    after = float(ctx.industry_pct) + amount / float(ctx.total_assets)
    if after > max_pct + _EPS:
        return _reject("l1.industry_cap", "L1", "行业集中度超上限",
                       after_pct=round(after, 4), max_pct=max_pct)
    return None


@rule("l1.daily_loss_limit", "L1", "日内亏损限额（≤ -max_loss_pct% 停止开仓）", max_loss_pct=3.0)
def l1_daily_loss_limit(ctx: RiskContext, params: Mapping[str, Any]) -> Decision | None:
    if str(ctx.side).upper() != "BUY":
        return None
    if ctx.daily_pnl_pct is None:
        return None
    limit = -abs(float(params.get("max_loss_pct", 3.0)))
    if float(ctx.daily_pnl_pct) <= limit:
        return _reject("l1.daily_loss_limit", "L1", "日内亏损达限额，停止开仓",
                       daily_pnl_pct=ctx.daily_pnl_pct, limit_pct=limit)
    return None


# ── L3 订单级 ─────────────────────────────────────────────────────────


@rule("l3.max_order_value", "L3", "单笔金额上限", max_value=100_000.0)
def l3_max_order_value(ctx: RiskContext, params: Mapping[str, Any]) -> Decision | None:
    amount = ctx.order_amount()
    if amount is None:
        return _reject("l3.max_order_value", "L3", "订单金额不可得，fail-closed")
    max_value = float(params.get("max_value", 100_000.0))
    if amount > max_value + _EPS:
        return _reject("l3.max_order_value", "L3", "单笔金额超限", amount=amount, max_value=max_value)
    return None


@rule("l3.price_deviation", "L3", "价格偏离闸门（限价 vs 最新价；强平单仅保 sanity 上界）",
      max_dev=0.02, sanity_max_dev=0.20)
def l3_price_deviation(ctx: RiskContext, params: Mapping[str, Any]) -> Decision | None:
    if str(ctx.order_type).upper() != "LIMIT" or ctx.price is None or ctx.last_price in (None, 0):
        return None
    dev = abs(float(ctx.price) / float(ctx.last_price) - 1.0)
    if ctx.forced_exit:
        cap = float(params.get("sanity_max_dev", 0.20))
        if dev > cap + _EPS:
            return _reject("l3.price_deviation", "L3", "强平单价格超 sanity 上界",
                           dev=round(dev, 4), cap=cap, forced_exit=True)
        return None
    max_dev = float(params.get("max_dev", 0.02))
    if dev > max_dev + _EPS:
        return _reject("l3.price_deviation", "L3", "委托价偏离最新价超限",
                       dev=round(dev, 4), max_dev=max_dev, price=ctx.price, last=ctx.last_price)
    return None


@rule("l3.stale_quote", "L3", "陈旧价拒单（行情时间戳早于阈值；不可得=拒）", max_age_s=5.0)
def l3_stale_quote(ctx: RiskContext, params: Mapping[str, Any]) -> Decision | None:
    if ctx.quote_age_s is None:
        return _reject("l3.stale_quote", "L3", "行情时间戳不可得，fail-closed")
    max_age = float(params.get("max_age_s", 5.0))
    if float(ctx.quote_age_s) > max_age:
        return _reject("l3.stale_quote", "L3", "行情陈旧",
                       age_s=ctx.quote_age_s, max_age_s=max_age)
    return None


@rule("l3.order_frequency", "L3", "下单频率上限（每分钟）", max_per_minute=20)
def l3_order_frequency(ctx: RiskContext, params: Mapping[str, Any]) -> Decision | None:
    max_per_minute = int(params.get("max_per_minute", 20))
    if int(ctx.orders_last_minute) >= max_per_minute:
        return _reject("l3.order_frequency", "L3", "下单频率超限",
                       orders_last_minute=ctx.orders_last_minute, max_per_minute=max_per_minute)
    return None


@rule("l3.cancel_ratio", "L3", "撤单率监控（超限记 WARN 供限频，不直接拒单）",
      max_ratio=0.40, min_orders=10)
def l3_cancel_ratio(ctx: RiskContext, params: Mapping[str, Any]) -> Decision | None:
    min_orders = int(params.get("min_orders", 10))
    if int(ctx.orders_today) < min_orders:
        return None
    ratio = int(ctx.cancels_today) / max(1, int(ctx.orders_today))
    max_ratio = float(params.get("max_ratio", 0.40))
    if ratio > max_ratio:
        return _warn("l3.cancel_ratio", "L3", "撤单率超监管参考线（建议限频）",
                     ratio=round(ratio, 4), max_ratio=max_ratio)
    return None


@rule("l3.self_trade", "L3", "自成交防范（窗口内同标的反向单存在即拒）")
def l3_self_trade(ctx: RiskContext, params: Mapping[str, Any]) -> Decision | None:
    opposite = "SELL" if str(ctx.side).upper() == "BUY" else "BUY"
    for sym, side in ctx.recent_symbol_sides:
        if str(sym) == str(ctx.symbol) and str(side).upper() == opposite:
            return _reject("l3.self_trade", "L3", "同标的窗口内存在反向委托（自成交风险）",
                           symbol=ctx.symbol, opposite=opposite)
    return None


@rule("l3.lot_size", "L3", "整手校验（买入整手：主板 100/科创 200；卖出允许零股清仓）",
      default_lot=100, star_lot=200)
def l3_lot_size(ctx: RiskContext, params: Mapping[str, Any]) -> Decision | None:
    qty = int(ctx.quantity)
    if qty <= 0:
        return _reject("l3.lot_size", "L3", "委托数量非正", quantity=qty)
    if str(ctx.side).upper() != "BUY":
        return None
    code = str(ctx.symbol)
    num = "".join(ch for ch in code if ch.isdigit())[:6]
    lot = int(params.get("star_lot", 200)) if num.startswith("688") else int(params.get("default_lot", 100))
    if qty % lot != 0:
        return _reject("l3.lot_size", "L3", "买入数量非整手",
                       quantity=qty, lot=lot, symbol=ctx.symbol)
    return None


@rule("l3.duplicate_fingerprint", "L3", "重复单防范（窗口内同参数指纹存在即拒）")
def l3_duplicate_fingerprint(ctx: RiskContext, params: Mapping[str, Any]) -> Decision | None:
    fp = str(ctx.fingerprint or "").strip()
    if fp and fp in ctx.recent_fingerprints:
        return _reject("l3.duplicate_fingerprint", "L3", "窗口内存在同参数委托（重复单）",
                       fingerprint=fp)
    return None


# ── L6 数据/模型级 ────────────────────────────────────────────────────


@rule("l6.book_invalid", "L6", "盘口异常（倒挂/空盘口）拒单")
def l6_book_invalid(ctx: RiskContext, params: Mapping[str, Any]) -> Decision | None:
    if ctx.book_crossed or ctx.book_empty:
        return _reject("l6.book_invalid", "L6", "盘口异常",
                       crossed=ctx.book_crossed, empty=ctx.book_empty)
    return None


@rule("l6.contract_mismatch", "L6", "模型/特征契约不符拒单（由上游置位）")
def l6_contract_mismatch(ctx: RiskContext, params: Mapping[str, Any]) -> Decision | None:
    if not ctx.contract_ok:
        return _reject("l6.contract_mismatch", "L6", "模型/特征契约不符")
    return None
