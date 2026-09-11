"""QMT 止盈/止损执行器：触发即用**保护价**下真单，保证"一定要卖"能落地。

背景与实测依据见 ``docs/QMT止损执行器与镜像链路完善计划.md``。核心决策：

* 触发规则复用 :func:`tdx_quote_feed.check_sltp_trigger`（与 TDX 桥 ``stop_loss_daemon``
  同口径），避免两套语义。
* 卖出保护价取桥的 ``DownStopPrice``（跌停价）：实测报跌停价成交在盘口买一
  （2.21 报 → 2.34 成交），既保证成交又不让价；跌停封死时挂队等待并如实告警。
* **不做追价/撤单重挂**：保护价已是当日最激进可报价，重挂只会丢队列优先级。
* 走内部真单链路（落 ``orders`` 表 + ``qmt_exec_poller`` 回收），天然绕过镜像闸门。
* 数量默认取柜台 ``can_use_volume`` 全量（全量卖出允许碎股）；部分卖出按板块整手对齐。
* 一次触发当日只执行一次（``armed → triggered → submitted → filled/…``），
  ``POST /reset`` 或改规则后重新武装。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from backend.services.live_trading.services.lot_rules import align_sell_quantity
from backend.services.live_trading.services.tdx_quote_feed import (
    check_sltp_trigger,
    load_sltp_config,
)
from backend.services.live_trading.services.trading_session import (
    TZ,
    is_trading_time,
    trade_date_str,
)

logger = logging.getLogger(__name__)

CONFIG_KEY = "qmt:sltp:executor:config"
STATE_KEY = "qmt:sltp:executor:state"

# 规则状态机
ST_ARMED = "armed"
ST_TRIGGERED = "triggered"
ST_SUBMITTED = "submitted"
ST_PARTIAL = "partial"
ST_FILLED = "filled"
ST_CANCELLED = "cancelled"
ST_REJECTED = "rejected"
ST_FAILED = "failed"
ST_SKIPPED = "skipped"

_TERMINAL_STATES = {ST_FILLED, ST_CANCELLED, ST_REJECTED, ST_FAILED, ST_SKIPPED}
_LIVE_STATES = {ST_SUBMITTED, ST_PARTIAL}
_HISTORY_DAYS = 7

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": False,
    "user_id": "1",
    "tenant_id": "default",
    "poll_interval_sec": 3,
    "protect_price_mode": "limit_floor",  # limit_floor（跌停价保护）| market
    "pending_alert_sec": 300,  # 未成交告警 + 余量策略触发阈值（秒，0=立即）
    "remainder_policy": "alert_only",  # alert_only | cancel | requote_at_protect_price
    "close_reminder_sec": 300,  # 收盘前提醒窗口（秒）
    "rules": [],
}

VALID_REMAINDER_POLICIES = ("alert_only", "cancel", "requote_at_protect_price")

# A 股收盘（沪深连续竞价 15:00 截止）
_CLOSE_HOUR = 15
_CLOSE_MINUTE = 0
# 重挂价格判定：与保护价差异不超过该值视为同价（避免无意义撤挂丢失队列优先级）
_REQUOTE_PRICE_EPSILON = 0.01
# 连续多少轮拿不到该标的行情就告警（默认 3s 轮询 ≈ 30 秒；行情恢复后计数归零）
_TICK_MISS_ALERT_THRESHOLD = 10

DEFAULT_RULE: dict[str, Any] = {
    "symbol": "",
    "enabled": True,
    "side": "SELL",
    "entry_price": None,
    "quantity": None,
    "stop_loss_pct": None,
    "take_profit_pct": None,
    "trailing_stop_pct": None,
}


# --------------------------------------------------------------------------
# 纯函数（可单测）
# --------------------------------------------------------------------------
def normalize_symbol(symbol: str) -> str:
    """任意口径 → 后缀式（``600036.SH``），规则表与状态键统一用它。"""
    raw = str(symbol or "").strip().upper()
    if not raw:
        return ""
    if "." in raw:
        return raw
    try:
        from backend.shared.stock_utils import StockCodeUtil

        suffix = StockCodeUtil.to_suffix(raw)
        if suffix:
            return suffix
    except Exception:  # noqa: BLE001 - 兜底不阻断
        pass
    return raw


def normalize_rule(raw: dict[str, Any]) -> dict[str, Any]:
    """清洗单条规则（未知字段忽略，数值安全转换）。"""
    rule = dict(DEFAULT_RULE)
    rule.update({k: v for k, v in (raw or {}).items() if k in DEFAULT_RULE})
    rule["symbol"] = normalize_symbol(rule.get("symbol"))
    side = str(rule.get("side") or "SELL").strip().upper()
    if side != "SELL":
        # 执行器是清仓语义：只允许卖出（买入会越止越买，建仓另走策略链路）
        logger.warning("[SltpExec] 规则 side=%s 非法，按 SELL 处理: %s", side, rule["symbol"])
        side = "SELL"
    rule["side"] = side
    for key in ("entry_price", "quantity", "stop_loss_pct", "take_profit_pct", "trailing_stop_pct"):
        value = rule.get(key)
        if value in ("", None):
            rule[key] = None
            continue
        try:
            rule[key] = float(value)
        except (TypeError, ValueError):
            rule[key] = None
    rule["enabled"] = bool(rule.get("enabled", True))
    return rule


def merge_config(raw: dict[str, Any] | None) -> dict[str, Any]:
    raw = raw or {}
    cfg = dict(DEFAULT_CONFIG)
    cfg.update({k: v for k, v in raw.items() if k in DEFAULT_CONFIG})
    # 兼容旧键：unfilled_alert_sec → pending_alert_sec（Phase 3.2 统一改名）
    if "pending_alert_sec" not in raw and "unfilled_alert_sec" in raw:
        cfg["pending_alert_sec"] = raw.get("unfilled_alert_sec")
    policy = str(cfg.get("remainder_policy") or "alert_only").strip().lower()
    cfg["remainder_policy"] = policy if policy in VALID_REMAINDER_POLICIES else "alert_only"
    cfg["rules"] = [normalize_rule(r) for r in (cfg.get("rules") or []) if isinstance(r, dict)]
    cfg["rules"] = [r for r in cfg["rules"] if r["symbol"]]
    cfg["enabled"] = bool(cfg.get("enabled"))
    return cfg


def trigger_config(rule: dict[str, Any], fallback: dict[str, Any] | None) -> dict[str, Any]:
    """规则触发阈值：规则内显式值优先，缺省回落设置页（``load_sltp_config``）口径。

    设置页把「止损止盈」整个关掉（``enabled=False``）时不再回落到它的阈值——
    否则用户关掉的提醒会以「执行器缺省阈值」的名义继续触发真单。
    """
    fb = fallback or {}
    if fb.get("enabled") is False:
        fb = {}
    cfg = {
        "stop_loss_pct": rule.get("stop_loss_pct") if rule.get("stop_loss_pct") is not None else fb.get("stop_loss_pct"),
        "take_profit_pct": rule.get("take_profit_pct") if rule.get("take_profit_pct") is not None else fb.get("take_profit_pct"),
        "trailing_stop_pct": rule.get("trailing_stop_pct") if rule.get("trailing_stop_pct") is not None else fb.get("trailing_stop_pct"),
        "highest_price": rule.get("highest_price"),
    }
    return cfg


def update_highest_price(previous: float | None, price: float) -> float:
    """最高价只升不降。"""
    prev = float(previous or 0)
    return max(prev, float(price or 0))


def rule_client_order_id(symbol: str, now_ts: float, generation: int = 1) -> str:
    """规则当日幂等委托号。

    同一个「标的 + 交易日 + 触发代数」永远得到同一个 ``client_order_id``：
    触发后进程崩溃/状态写回失败时按同号重试，调度器的 client_order_id 去重
    会返回已有委托而不是重复下单（下真单的链路不允许靠状态机兜底防重）。

    ``generation`` 是当日第几次触发；``POST /reset`` 重新武装后递增，
    保证「重新武装后再触发」仍能下出**新**单而不是被自己的旧号挡住。
    """
    day = datetime.fromtimestamp(float(now_ts), TZ).strftime("%Y%m%d")
    return f"sltp-{normalize_symbol(symbol)}-{day}-g{max(1, int(generation))}"


def is_retryable(state_item: dict[str, Any] | None) -> bool:
    """本轮是否可（重新）评估触发。

    ``armed`` 是常规状态；``triggered`` 但**没有任何委托号**说明上一次触发在
    落单前中断（进程被杀、状态写回后崩溃），允许重试——幂等由
    :func:`rule_client_order_id` 保证，重试不会变成重复下单。
    """
    item = state_item or {}
    status = str(item.get("status") or ST_ARMED)
    if status == ST_ARMED:
        return True
    return status == ST_TRIGGERED and not str(item.get("order_id") or "")


def resolve_protect_price(
    mode: str, detail: dict[str, Any] | None, live_price: float
) -> tuple[str | None, float, str]:
    """保护价决策（纯函数）：返回 ``(order_type, price, note)``，order_type 为 None 表示 fail-closed。

    * ``market`` 模式 → ``("MARKET", 0, …)``（柜台映射最新价委托，实测可成交）。
    * ``limit_floor`` 模式 → 桥的 ``DownStopPrice``（跌停价；实测报跌停价成交在盘口买一）。
      取不到则 fail-closed 返回 ``(None, 0, 原因)``，由调用方告警而非乱报价。
    """
    if str(mode or "").strip().lower() == "market":
        return "MARKET", 0.0, "市价委托（柜台映射最新价）"
    floor = _to_float((detail or {}).get("DownStopPrice"))
    if floor is None or floor <= 0:
        return None, 0.0, "桥未返回跌停价（DownStopPrice），fail-closed 不下单"
    return "LIMIT", round(float(floor), 2), f"跌停保护价 {float(floor):.2f}"


def _to_float(value: Any) -> float | None:
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if x == x and abs(x) != float("inf") else None


def order_status_to_rule_state(db_status: str) -> str | None:
    """DB 订单状态 → 规则状态（未知返回 None，保持原状）。"""
    s = str(db_status or "").strip().lower()
    return {
        "filled": ST_FILLED,
        "cancelled": ST_CANCELLED,
        "rejected": ST_REJECTED,
        "failed": ST_FAILED,
        "expired": ST_FAILED,
        "partially_filled": ST_PARTIAL,
        "submitted": ST_SUBMITTED,
        "pending": ST_SUBMITTED,
        "accepted": ST_SUBMITTED,
    }.get(s)


# --------------------------------------------------------------------------
# Redis 配置 / 状态
# --------------------------------------------------------------------------
def load_config(redis: Any) -> dict[str, Any]:
    try:
        raw = redis.get(CONFIG_KEY)
        return merge_config(raw if isinstance(raw, dict) else None)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[SltpExec] 读取配置失败，用默认（关闭）: %s", exc)
        return merge_config(None)


def save_config(redis: Any, cfg: dict[str, Any]) -> dict[str, Any]:
    clean = merge_config(cfg)
    redis.set(CONFIG_KEY, clean)
    return clean


def set_enabled(redis: Any, enabled: bool) -> dict[str, Any]:
    """只改总开关，不动规则表。

    与 :func:`save_config` 的区别是**读失败直接抛错**（调用方回 5xx）：把「读不到」
    当空配置再整体写回，会在 Redis 抖动时把规则表整份抹掉。这里只有真的读到
    （含键不存在 → 默认配置）才会写。
    """
    raw = redis.get(CONFIG_KEY)
    cfg = merge_config(raw if isinstance(raw, dict) else None)
    cfg["enabled"] = bool(enabled)
    redis.set(CONFIG_KEY, cfg)
    return cfg


def diff_state(
    before: dict[str, dict[str, Any]], after: dict[str, dict[str, Any]]
) -> tuple[set[str], set[str]]:
    """状态快照对比：返回 ``(改动过的标的, 被删除的标的)``。"""
    dirty = {symbol for symbol, item in after.items() if item != before.get(symbol)}
    removed = {symbol for symbol in before if symbol not in after}
    return dirty, removed


def load_state(redis: Any, today: str | None = None) -> dict[str, Any]:
    """读规则状态；跨日自动重置为 armed（当日一次触发的语义）。"""
    day = today or trade_date_str()
    try:
        raw = redis.get(STATE_KEY)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[SltpExec] 读取状态失败，用空状态: %s", exc)
        raw = None
    state = raw if isinstance(raw, dict) else {}
    if str(state.get("date") or "") != day:
        state = {"date": day, "rules": {}}
    state.setdefault("rules", {})
    for symbol, item in list(state["rules"].items()):
        if not isinstance(item, dict):
            state["rules"][symbol] = {"status": ST_ARMED}
        else:
            item.setdefault("status", ST_ARMED)
            item.setdefault("highest_price", None)
    return state


def save_state(
    redis: Any,
    state: dict[str, Any],
    *,
    dirty: set[str] | None = None,
    removed: set[str] | None = None,
) -> None:
    """写回规则状态。

    * 不带 ``dirty``：整份覆盖（reset / 初始化场景）。
    * 带 ``dirty``：先读回 Redis 现存状态，只覆盖本轮改动过的规则，其余保留。
      执行器每轮都写状态，整份覆盖会把并发的 ``POST /reset``、CLI ``--rm``/``--arm``
      一并冲掉（后写覆盖先写）——真单链路上「用户以为已经解除，执行器照旧触发」
      是不能接受的。
    """
    try:
        if dirty is None and removed is None:
            redis.set(STATE_KEY, state)
            return
        current = redis.get(STATE_KEY)
        day = str(state.get("date") or "")
        if isinstance(current, dict) and str(current.get("date") or "") == day:
            merged = dict(current)
        else:
            merged = {"date": day, "rules": {}}
        rules = dict(merged.get("rules") or {})
        for symbol in removed or ():
            rules.pop(symbol, None)
        for symbol in dirty or ():
            item = state["rules"].get(symbol)
            if item is None:
                rules.pop(symbol, None)
            else:
                rules[symbol] = item
        merged["rules"] = rules
        merged["date"] = day or merged.get("date")
        redis.set(STATE_KEY, merged)
        # 与落盘一致（并发新增/删除同步进进程内视图，供本轮后续步骤使用）
        state["rules"] = rules
        state["date"] = merged["date"]
    except Exception as exc:  # noqa: BLE001
        logger.error("[SltpExec] 状态写回失败: %s", exc)


def reset_rules(redis: Any, symbols: list[str] | None = None) -> dict[str, Any]:
    """重新武装（全部或指定标的）。"""
    state = load_state(redis)
    targets = [normalize_symbol(s) for s in symbols] if symbols else list(state["rules"].keys())
    for symbol in targets:
        if not symbol:
            continue
        item = state["rules"].get(symbol)
        if item is None:
            continue
        keep_entry = item.get("entry_price")
        state["rules"][symbol] = {
            "status": ST_ARMED,
            "highest_price": None,
            "entry_price": keep_entry,
            # generation 保留：重新武装后再触发要下**新**单（委托号含代数），
            # 而崩溃重试复用同代委托号、交给调度器幂等去重
            "generation": int(item.get("generation") or 0),
        }
    save_state(redis, state)
    return state


# --------------------------------------------------------------------------
# 依赖注入（便于单测）
# --------------------------------------------------------------------------
@dataclass
class SltpDeps:
    client: Any
    redis: Any
    dispatch: Callable[[dict[str, Any], str], Awaitable[dict[str, Any]]]
    notify: Callable[..., Awaitable[Any]]
    order_reader: Callable[[str], Awaitable[dict[str, Any] | None]]
    now: Callable[[], float] = time.time
    positions: Callable[[], Awaitable[list[dict[str, Any]]]] | None = None
    fallback_config: Callable[[str, str], dict[str, Any]] = load_sltp_config
    # 撤单（余量策略 cancel/requote 用）；未注入时策略退化为 alert_only
    cancel_order: Callable[[str], Awaitable[bool]] | None = None
    extras: dict[str, Any] = field(default_factory=dict)


async def _fetch_positions(deps: SltpDeps) -> list[dict[str, Any]]:
    if deps.positions is not None:
        return await deps.positions()
    try:
        return await deps.client.get_positions()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[SltpExec] 查询柜台持仓失败: %s", exc)
        return []


def _find_position(positions: list[dict[str, Any]], symbol: str) -> dict[str, Any] | None:
    target = normalize_symbol(symbol)
    for item in positions or []:
        code = normalize_symbol(str(item.get("stock_code") or item.get("symbol") or ""))
        if code and code == target:
            return item
    return None


def _position_entry_price(position: dict[str, Any] | None) -> float | None:
    if not position:
        return None
    for key in ("open_price", "avg_price", "cost_price"):
        value = _to_float(position.get(key))
        if value and value > 0:
            return value
    return None


# --------------------------------------------------------------------------
# 主循环
# --------------------------------------------------------------------------
async def run_sltp_cycle(deps: SltpDeps, *, config: dict[str, Any] | None = None) -> dict[str, Any]:
    """单轮：监控在途单 → 评估触发 → 下单。返回本轮摘要（便于日志/测试）。"""
    cfg = config or load_config(deps.redis)
    summary: dict[str, Any] = {
        "enabled": bool(cfg.get("enabled")),
        "evaluated": 0,
        "triggered": 0,
        "submitted": 0,
        "failed": 0,
        "monitored": 0,
    }
    if not cfg.get("enabled"):
        return summary

    rules = [r for r in (cfg.get("rules") or []) if r.get("enabled", True)]
    if not rules:
        return summary

    today = trade_date_str()
    state = load_state(deps.redis, today)
    # 本轮开始时的快照：结束/中途回写只覆盖改动过的规则（并发 reset/--rm 不被冲掉）
    before_rules = {symbol: dict(item) for symbol, item in state["rules"].items()}

    def _persist() -> None:
        dirty, removed = diff_state(before_rules, state["rules"])
        save_state(deps.redis, state, dirty=dirty, removed=removed)

    user_id = str(cfg.get("user_id") or "1")
    tenant_id = str(cfg.get("tenant_id") or "default")
    now = deps.now()

    # 1) 在途单监控（任何时段都做，保证收盘后仍能收到终态通知）
    await _monitor_pending(deps, cfg, state, user_id, tenant_id, now, summary)

    # 2) 交易时段内评估触发
    if not is_trading_time():
        await _notify_stranded_triggers(deps, state, user_id, tenant_id)
        _persist()
        return summary

    armed = [r for r in rules if is_retryable(state["rules"].get(r["symbol"]))]
    if not armed:
        _persist()
        return summary

    if not bool(getattr(deps.client, "configured", True)):
        return summary

    try:
        ticks = await deps.client.get_full_tick([r["symbol"] for r in armed])
    except Exception as exc:  # noqa: BLE001
        logger.warning("[SltpExec] 拉实时行情失败: %s", exc)
        return summary

    fallback_cfg: dict[str, Any] | None = None
    positions: list[dict[str, Any]] | None = None

    for rule in armed:
        symbol = rule["symbol"]
        st = state["rules"].setdefault(symbol, {"status": ST_ARMED})
        tick = (ticks or {}).get(symbol) or {}
        price = _to_float(tick.get("lastPrice"))
        summary["evaluated"] += 1
        if price is None or price <= 0:
            st["misses"] = int(st.get("misses") or 0) + 1
            st["last_tick_ts"] = now
            # 恰好到阈值时告警一次；行情恢复计数归零，可再次告警
            if st["misses"] == _TICK_MISS_ALERT_THRESHOLD:
                await deps.notify(
                    user_id,
                    f"{symbol} 行情缺失",
                    f"连续 {st['misses']} 轮未取到该标的实时行情，止盈止损规则暂时无法评估。"
                    "请检查行情通道/代码是否正确。",
                    "warning",
                    tenant_id=tenant_id,
                )
            continue
        st["misses"] = 0
        st["last_price"] = price
        st["highest_price"] = update_highest_price(st.get("highest_price"), price)

        if fallback_cfg is None:
            try:
                fallback_cfg = deps.fallback_config(tenant_id, user_id) or {}
            except Exception:  # noqa: BLE001
                fallback_cfg = {}

        # entry_price：规则显式 → 状态缓存 → 柜台持仓成本价
        entry = _to_float(rule.get("entry_price")) or _to_float(st.get("entry_price"))
        if not entry and positions is None:
            positions = await _fetch_positions(deps)
            entry = _position_entry_price(_find_position(positions, symbol))
        if not entry or entry <= 0:
            if not st.get("entry_missing_notified"):
                st["entry_missing_notified"] = True
                await deps.notify(
                    user_id,
                    f"{symbol} 止损规则缺少成本价",
                    "规则未配置 entry_price 且柜台无持仓成本，无法计算触发线；已跳过。",
                    "warning",
                    tenant_id=tenant_id,
                )
            continue
        st["entry_price"] = entry

        tcfg = trigger_config({**rule, "highest_price": st.get("highest_price")}, fallback_cfg)
        triggered, reason = check_sltp_trigger(price, entry, tcfg)
        if not triggered:
            continue

        st["status"] = ST_TRIGGERED
        st["reason"] = reason
        st["triggered_at"] = now
        # 先落 triggered 再下单：进程中断时留下「触发未落单」的痕迹，
        # 下一轮 is_retryable 会按同号重试（委托号幂等，不会重复下单）
        _persist()
        summary["triggered"] += 1
        logger.info("[SltpExec] 触发 %s: %s", symbol, reason)

        if positions is None:
            positions = await _fetch_positions(deps)
        position = _find_position(positions, symbol)
        await _execute_trigger(
            deps, cfg, state, st, rule, position, price, reason, user_id, tenant_id, now, summary
        )

    _persist()
    return summary


async def _execute_trigger(
    deps: SltpDeps,
    cfg: dict[str, Any],
    state: dict[str, Any],
    st: dict[str, Any],
    rule: dict[str, Any],
    position: dict[str, Any] | None,
    price: float,
    reason: str,
    user_id: str,
    tenant_id: str,
    now: float,
    summary: dict[str, Any],
) -> None:
    symbol = rule["symbol"]
    can_use = _to_float((position or {}).get("can_use_volume")) or 0.0
    quantity, note = align_sell_quantity(symbol, _to_float(rule.get("quantity")) or 0.0, can_use)
    if quantity <= 0:
        st["status"] = ST_SKIPPED
        st["skip_reason"] = note
        summary["failed"] += 1
        await deps.notify(
            user_id,
            f"{symbol} 触发未卖出",
            f"{reason}；但{note}。规则当日不再重试。",
            "warning",
            tenant_id=tenant_id,
        )
        return

    detail: dict[str, Any] | None = None
    mode = str(cfg.get("protect_price_mode") or "limit_floor")
    if mode.strip().lower() != "market":
        try:
            detail = await deps.client.get_instrument_detail(symbol)
        except Exception as exc:  # noqa: BLE001
            detail = None
            logger.warning("[SltpExec] 取 %s 合约详情失败: %s", symbol, exc)
    order_type, order_price, price_note = resolve_protect_price(mode, detail, price)
    if order_type is None:
        st["status"] = ST_FAILED
        st["skip_reason"] = price_note
        summary["failed"] += 1
        await deps.notify(
            user_id,
            f"{symbol} 触发但保护价不可用",
            f"{reason}；{price_note}。现价 {price:.2f}，请人工介入。",
            "error",
            tenant_id=tenant_id,
        )
        return

    generation = int(st.get("generation") or 0) + 1
    st["generation"] = generation
    # 当日同规则固定委托号：崩溃重试复用同号，由调度器幂等去重（不会重复下单）
    cid = rule_client_order_id(symbol, now, generation)
    remarks = f"sltp:{reason[:40]}" if reason else "sltp:trigger"
    order_data = {
        "symbol": symbol,
        "side": str(rule.get("side") or "SELL"),
        "quantity": float(quantity),
        "price": float(order_price),
        "order_type": order_type,
        "trading_mode": "REAL",
        "portfolio_id": 0,
        "strategy_id": None,
        "client_order_id": cid,
        "remarks": remarks,
    }
    try:
        resp = await deps.dispatch(order_data, user_id)
    except Exception as exc:  # noqa: BLE001
        resp = {"status": "error", "message": str(exc)}
    if str((resp or {}).get("status")) != "success":
        st["status"] = ST_FAILED
        st["failure"] = (resp or {}).get("message") or (resp or {}).get("detail") or str(resp)
        summary["failed"] += 1
        logger.error("[SltpExec] %s 下单失败: %s", symbol, st["failure"])
        await deps.notify(
            user_id,
            f"{symbol} 触发卖出失败",
            f"{reason}；下单失败：{st['failure']}。保护价 {price_note}，现价 {price:.2f}，请人工介入。",
            "error",
            tenant_id=tenant_id,
        )
        return

    st.update(
        {
            "status": ST_SUBMITTED,
            "order_id": str((resp or {}).get("order_id") or ""),
            "client_order_id": cid,
            "quantity": float(quantity),
            "order_price": float(order_price),
            "order_type": order_type,
            "price_note": price_note,
            "submitted_at": now,
        }
    )
    summary["submitted"] += 1
    extra = f"（{note}）" if note else ""
    logger.info(
        "[SltpExec] 已提交 %s %s %s股 @%s order_id=%s",
        symbol,
        order_type,
        quantity,
        order_price or "市价",
        st["order_id"],
    )
    await deps.notify(
        user_id,
        f"{symbol} 触发卖出已提交",
        f"{reason}；以{price_note}报单 {quantity:g} 股{extra}，委托号 {st['order_id']}。",
        "info",
        tenant_id=tenant_id,
    )


def seconds_to_close(now_ts: float) -> float:
    """距当日 15:00 收盘的秒数（收盘后为负）。"""
    now_dt = datetime.fromtimestamp(float(now_ts), TZ)
    close_dt = now_dt.replace(
        hour=_CLOSE_HOUR, minute=_CLOSE_MINUTE, second=0, microsecond=0
    )
    return (close_dt - now_dt).total_seconds()


async def _notify_stranded_triggers(
    deps: SltpDeps, state: dict[str, Any], user_id: str, tenant_id: str
) -> None:
    """触发后没能落单（进程中断）且已过交易时段：告警一次，交人工处理。

    交易时段内这类规则会由 :func:`is_retryable` 自动重试，不需要告警；
    但收盘后才发现的（例如重启后已过 15:00）当天已经没有补救机会，
    必须让人知道「触发了但没卖出去」。
    """
    for symbol, st in state.get("rules", {}).items():
        if str(st.get("status")) != ST_TRIGGERED or str(st.get("order_id") or ""):
            continue
        if st.get("stranded_notified"):
            continue
        st["stranded_notified"] = True
        await deps.notify(
            user_id,
            f"{symbol} 止损触发未能下单",
            f"{st.get('reason') or '触发'}；但触发时执行器中断且已收盘，当天未能报出委托。"
            "请人工确认是否手动卖出，或下一个交易日重新武装（POST /reset）。",
            "error",
            tenant_id=tenant_id,
        )


async def _monitor_pending(
    deps: SltpDeps,
    cfg: dict[str, Any],
    state: dict[str, Any],
    user_id: str,
    tenant_id: str,
    now: float,
    summary: dict[str, Any],
) -> None:
    alert_sec = float(cfg.get("pending_alert_sec") or 0)
    policy = str(cfg.get("remainder_policy") or "alert_only")
    close_window = float(cfg.get("close_reminder_sec") or 0)
    for symbol, st in list(state.get("rules", {}).items()):
        if st.get("status") not in _LIVE_STATES:
            continue
        order_id = str(st.get("order_id") or "")
        if not order_id:
            continue
        try:
            row = await deps.order_reader(order_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[SltpExec] 读订单 %s 失败: %s", order_id, exc)
            continue
        if not row:
            continue
        summary["monitored"] += 1
        db_status = str(row.get("status") or "").lower()
        filled = _to_float(row.get("filled_quantity")) or 0.0
        st["filled_quantity"] = filled
        st["last_status"] = db_status
        mapped = order_status_to_rule_state(db_status)
        if mapped in _TERMINAL_STATES | {ST_PARTIAL}:
            st["status"] = mapped
        if mapped in _TERMINAL_STATES:
            if not st.get("terminal_notified"):
                st["terminal_notified"] = True
                remaining = max(0.0, (_to_float(st.get("quantity")) or 0.0) - filled)
                avg = _to_float(row.get("average_price"))
                level = "info" if db_status == "filled" else "warning"
                tail = f"，剩余 {remaining:g} 股未成交" if remaining > 0 else ""
                await deps.notify(
                    user_id,
                    f"{symbol} 止损委托已{_status_cn(db_status)}",
                    f"成交 {filled:g} 股"
                    + (f" @ {avg:.2f}" if avg else "")
                    + tail
                    + f"（委托号 {order_id}）。",
                    level,
                    tenant_id=tenant_id,
                )
            continue

        quantity = _to_float(st.get("quantity")) or 0.0
        remaining = max(0.0, quantity - filled)
        pending_sec = now - float(st.get("submitted_at") or now)

        # 收盘前提醒：A 股当日有效，尾盘未成交的余量将随日终自动失效
        if (
            close_window > 0
            and not st.get("close_reminder_notified")
            and 0 <= seconds_to_close(now) <= close_window
            and remaining > 0
        ):
            st["close_reminder_notified"] = True
            await deps.notify(
                user_id,
                f"{symbol} 止损委托临近收盘",
                f"距收盘不足 {int(close_window / 60)} 分钟，仍有 {remaining:g} 股未成交；"
                "A 股委托当日有效，未成交部分将随日终自动失效，请确认是否需要人工处理。",
                "warning",
                tenant_id=tenant_id,
            )

        if alert_sec > 0 and pending_sec < alert_sec:
            continue

        if not st.get("unfilled_notified"):
            st["unfilled_notified"] = True
            await deps.notify(
                user_id,
                f"{symbol} 止损委托未成交",
                f"已挂 {int(pending_sec)} 秒"
                + (f"，部分成交 {filled:g} 股" if filled else "")
                + f"，剩余 {remaining:g} 股未成交。跌停封死时无买盘无法卖出，"
                "系统按时间优先排队等待；也可人工撤单改价。",
                "warning",
                tenant_id=tenant_id,
            )

        # 余量策略（每规则当日只执行一次）
        if not st.get("remainder_applied"):
            await _apply_remainder_policy(
                deps,
                cfg,
                st,
                symbol,
                remaining,
                policy,
                user_id,
                tenant_id,
                now,
                summary,
            )


async def _apply_remainder_policy(
    deps: SltpDeps,
    cfg: dict[str, Any],
    st: dict[str, Any],
    symbol: str,
    remaining: float,
    policy: str,
    user_id: str,
    tenant_id: str,
    now: float,
    summary: dict[str, Any],
) -> None:
    """未成交余量处理：alert_only（默认，仅提醒）/ cancel / requote_at_protect_price。"""
    if remaining <= 0:
        st["remainder_applied"] = True
        return
    if policy == "alert_only":
        st["remainder_applied"] = True
        return
    if deps.cancel_order is None:
        st["remainder_applied"] = True
        st["remainder_note"] = "cancel 未接线"
        logger.warning("[SltpExec] %s 余量策略 %s 需要 cancel_order 依赖，未注入", symbol, policy)
        return

    order_id = str(st.get("order_id") or "")
    order_price = _to_float(st.get("order_price")) or 0.0
    mode = str(cfg.get("protect_price_mode") or "limit_floor")
    order_type = str(st.get("order_type") or "")

    # 重挂前置：仅当当前委托价与保护价确有偏离才值得撤挂（否则丢队列优先级）
    if policy == "requote_at_protect_price":
        if order_type != "LIMIT":
            st["remainder_applied"] = True
            return
        try:
            detail = await deps.client.get_instrument_detail(symbol)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[SltpExec] %s 重挂取合约详情失败: %s", symbol, exc)
            return
        new_type, new_price, _note = resolve_protect_price(mode, detail, order_price)
        if new_type is None:
            st["remainder_applied"] = True
            return
        if new_type != "LIMIT" or abs(new_price - order_price) <= _REQUOTE_PRICE_EPSILON:
            st["remainder_applied"] = True
            st["remainder_note"] = "价格未偏离保护价，保持排队"
            return

    try:
        cancelled = await deps.cancel_order(order_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[SltpExec] %s 余量撤单异常: %s", symbol, exc)
        return
    if not cancelled:
        st["remainder_applied"] = True
        st["remainder_note"] = "撤单未受理（可能已成交/已撤销），保持现状"
        return

    summary["remainder_action"] = summary.get("remainder_action", 0) + 1

    if policy == "cancel":
        st["remainder_applied"] = True
        st["remainder_note"] = f"已撤单，剩余 {remaining:g} 股未成交"
        await deps.notify(
            user_id,
            f"{symbol} 未成交余量已撤单",
            f"委托挂满 {int(now - float(st.get('submitted_at') or now))} 秒未全部成交，"
            f"已按余量策略撤销，剩余 {remaining:g} 股了结。",
            "warning",
            tenant_id=tenant_id,
        )
        return

    # requote：撤旧挂新（保护价、当日额度按新委托重新计）
    requote_count = int(st.get("requote_count") or 0) + 1
    cid = f"{rule_client_order_id(symbol, now, int(st.get('generation') or 1))}-r{requote_count}"
    order_data = {
        "symbol": symbol,
        "side": "SELL",
        "quantity": float(remaining),
        "price": float(new_price),
        "order_type": "LIMIT",
        "trading_mode": "REAL",
        "portfolio_id": 0,
        "strategy_id": None,
        "client_order_id": cid,
        "remarks": f"sltp:requote{(st.get('reason') or '')[:32]}",
    }
    try:
        resp = await deps.dispatch(order_data, user_id)
    except Exception as exc:  # noqa: BLE001
        resp = {"status": "error", "message": str(exc)}
    if str((resp or {}).get("status")) != "success":
        st["remainder_applied"] = True
        st["remainder_note"] = f"重挂失败：{(resp or {}).get('message') or resp}"
        await deps.notify(
            user_id,
            f"{symbol} 余量重挂失败",
            f"旧委托已撤，重挂保护价 {new_price:.2f} 失败：{st['remainder_note']}。"
            f"剩余 {remaining:g} 股未了结，请人工介入。",
            "error",
            tenant_id=tenant_id,
        )
        return
    st.update(
        {
            "status": ST_SUBMITTED,
            "order_id": str((resp or {}).get("order_id") or ""),
            "client_order_id": cid,
            "quantity": float(remaining),
            "order_price": float(new_price),
            "submitted_at": now,
            "requote_count": requote_count,
            "remainder_note": f"已按保护价 {new_price:.2f} 重挂",
            "unfilled_notified": False,
            "close_reminder_notified": False,
        }
    )
    # 重挂后允许再观察一轮（保留 remainder_applied=False 会每轮重复撤挂，故置位）
    st["remainder_applied"] = True
    await deps.notify(
        user_id,
        f"{symbol} 未成交余量已重挂",
        f"旧委托价 {order_price:.2f} 偏离保护价，已撤单并重挂 {new_price:.2f}，"
        f"剩余 {remaining:g} 股。",
        "info",
        tenant_id=tenant_id,
    )


def _status_cn(status: str) -> str:
    return {
        "filled": "全部成交",
        "cancelled": "撤销",
        "rejected": "被柜台拒绝",
        "failed": "失败",
        "expired": "过期",
        "partially_filled": "部分成交",
    }.get(str(status).lower(), str(status))


def build_state_snapshot(redis: Any) -> dict[str, Any]:
    """给路由/CLI 的状态快照。"""
    return {"config": load_config(redis), "state": load_state(redis)}


# --------------------------------------------------------------------------
# 生产依赖与常驻任务
# --------------------------------------------------------------------------
def _build_default_deps(redis: Any, tenant_id: str = "default") -> SltpDeps:
    from sqlalchemy import select

    from backend.services.live_trading.services.internal_strategy_dispatcher import (
        dispatch_internal_strategy_order,
    )
    from backend.services.live_trading.services.qmt_exec_client import (
        get_qmt_exec_client,
    )
    from backend.shared.database_manager_v2 import get_session
    from backend.shared.notification_publisher import publish_notification_async
    from backend.services.trade_shared.models.order import Order

    client = get_qmt_exec_client()

    async def dispatch(order_data: dict[str, Any], user_id: str) -> dict[str, Any]:
        async with get_session() as db:
            return await dispatch_internal_strategy_order(
                order_data=order_data,
                user_id=str(user_id),
                tenant_id=tenant_id,
                redis=redis,
                db=db,
            )

    async def notify(
        user_id: str, title: str, content: str, level: str = "info", tenant_id: str = "default"
    ) -> Any:
        return await publish_notification_async(
            user_id=str(user_id),
            tenant_id=str(tenant_id or "default"),
            title=title,
            content=content,
            type="trading",
            level=level,
            action_url="/trading",
        )

    async def order_reader(order_id: str) -> dict[str, Any] | None:
        async with get_session(read_only=True) as db:
            row = (
                await db.execute(select(Order).where(Order.order_id == str(order_id)))
            ).scalar_one_or_none()
        if row is None:
            return None
        return {
            "status": str(getattr(row.status, "value", row.status) or ""),
            "filled_quantity": float(getattr(row, "filled_quantity", 0) or 0),
            "average_price": float(getattr(row, "average_price", 0) or 0),
            "remarks": str(getattr(row, "remarks", "") or ""),
        }

    async def cancel_order(order_id: str) -> bool:
        from backend.services.trade_shared.deps import get_redis as _get_redis
        from backend.services.live_trading.services.trading_engine import TradingEngine

        async with get_session() as db:
            row = (
                await db.execute(select(Order).where(Order.order_id == str(order_id)))
            ).scalar_one_or_none()
            if row is None:
                return False
            engine = TradingEngine(db, _get_redis())
            return bool(await engine.cancel_order_execution(row))

    return SltpDeps(
        client=client,
        redis=redis,
        dispatch=dispatch,
        notify=notify,
        order_reader=order_reader,
        cancel_order=cancel_order,
    )


async def run_qmt_sltp_executor_task() -> None:
    """常驻循环（随 trade 服务启动；失败只记日志，不拖垮进程）。"""
    from backend.services.trade_shared.deps import get_redis

    logger.info("[SltpExec] 止盈止损执行器任务启动")
    last_error = ""
    while True:
        interval = float(DEFAULT_CONFIG["poll_interval_sec"])
        try:
            redis = get_redis()
            cfg = load_config(redis)
            interval = max(1.0, float(cfg.get("poll_interval_sec") or interval))
            if cfg.get("enabled"):
                deps = _build_default_deps(
                    redis, tenant_id=str(cfg.get("tenant_id") or "default")
                )
                summary = await run_sltp_cycle(deps, config=cfg)
                last_error = ""
                if summary.get("triggered") or summary.get("submitted") or summary.get("failed"):
                    logger.info("[SltpExec] 本轮：%s", json.dumps(summary, ensure_ascii=False))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            if message != last_error:
                logger.error("[SltpExec] 轮询异常: %s", exc, exc_info=True)
                last_error = message
        await asyncio.sleep(interval)
