"""手动模式的提案计算与确认校验（纯函数，无 IO 无状态）。

从 day_runner 抽出，便于独立单测：
- scan_stop_loss   扫描止损候选（只提案不执行）
- simulate_fills   在账户副本上模拟止损成交，供调仓基于「止损后」的持仓计算
- validate_confirmed 服务端复校验用户确认清单

校验规则见 docs/replay/REPLAY_R3_R5_PLAN.md：
止损强制执行不可取消；数量只能调小；买入整手、卖出允许零头清仓。
"""

from __future__ import annotations

from typing import Any

from backend.services.simulation.services.local_market_data import DailyBar


def resolve_stop_fill_price(bar: DailyBar, stop_price: float) -> float:
    """止损成交价。

    触发条件用当日最低价，但成交价不能好于当日实际能成交的价格：
    跳空开盘时 open 已经低于 stop，按 stop 成交等于卖在一个当天不存在的价位，
    会让回放净值系统性偏高。故取 min(stop, open)，再受跌停价钳制。
    """
    price = min(stop_price, bar.open) if bar.open > 0 else stop_price
    if bar.limit_down > 0:
        price = max(price, bar.limit_down)
    return round(price, 4)


def scan_stop_loss(
    account_data: dict[str, Any],
    bars: dict[str, DailyBar],
    stop_loss_pct: float,
) -> list[dict[str, Any]]:
    """扫描止损，只返回提案不执行（propose 用）。

    T-P2-04：判定委托 ``exit_rules.evaluate_exit``（唯一实现）；触发价口径 = 日线 low
    （回放语义：当日最低价 ≤ 止损线即触发；与历史行为一致）。
    成交价逻辑（跌停钳制/跳空开盘价）保持不变。
    """
    from backend.shared.exit_rules import ExitRuleSet, PositionState, evaluate_exit

    rules = ExitRuleSet(hard_stop_pct=float(stop_loss_pct))
    out: list[dict[str, Any]] = []
    for symbol, pos in (account_data.get("positions") or {}).items():
        bar = bars.get(symbol)
        if bar is None or bar.suspended:
            continue
        cost = float(pos.get("cost") or 0.0)
        if cost <= 0:
            continue
        stop_price = cost * (1.0 - stop_loss_pct)
        # 唯一实现判定（low 触发口径）；bar.low 为 0/无效时按不触发处理
        decision = evaluate_exit(
            rules, PositionState(entry_price=cost, last_price=float(bar.low or 0.0))
        )
        if not decision.should_exit:
            continue
        stop_price = float(decision.snapshot.get("line") or stop_price)
        avail = pos.get("available_volume")
        qty = int(float(pos.get("volume", 0)) if avail is None else float(avail))
        if qty <= 0:
            continue
        fill_price = resolve_stop_fill_price(bar, stop_price)
        if fill_price <= 0:
            continue
        out.append(
            {
                "symbol": symbol,
                "side": "SELL",
                "quantity": qty,
                "est_price": round(fill_price, 4),
                "origin": "stop_loss",
                "cancellable": False,
                "stop_price": round(stop_price, 4),
                "avg_cost": round(cost, 4),
                "est_pnl": round((fill_price - cost) * qty, 2),
                "gap_down": bar.open < stop_price,
                "reason": f"止损: 成本{cost:.4f} 触发价{stop_price:.4f}",
            }
        )
    return out


def simulate_fills(
    account_data: dict[str, Any],
    proposals: list[dict[str, Any]],
) -> dict[str, Any]:
    """在账户副本上模拟成交，供后续调仓计算使用。

    propose 阶段不能真的动账户，但调仓必须基于「止损之后」的持仓来算，
    否则提案会与 auto 模式不一致（auto 是先执行止损再重读账户）。
    """
    import copy

    sim = copy.deepcopy(account_data)
    positions = sim.get("positions") or {}
    cash = float(sim.get("cash") or 0.0)
    for p in proposals:
        sym = p["symbol"]
        qty = float(p["quantity"])
        px = float(p["est_price"])
        pos = positions.get(sym)
        if not pos:
            continue
        if p["side"] == "SELL":
            new_vol = float(pos.get("volume", 0)) - qty
            cash += qty * px
            if new_vol <= 0.0001:
                positions.pop(sym, None)
            else:
                pos["volume"] = new_vol
                avail = pos.get("available_volume")
                if avail is not None:
                    pos["available_volume"] = max(0.0, float(avail) - qty)
                pos["market_value"] = new_vol * float(pos.get("price") or px)
    sim["cash"] = cash
    sim["positions"] = positions
    sim["total_asset"] = cash + sum(
        float(p.get("market_value") or 0.0) for p in positions.values()
    )
    return sim


def validate_confirmed(
    confirmed: list[dict[str, Any]],
    proposals: list[dict[str, Any]],
    account_data: dict[str, Any],
    lot_size: int = 100,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """服务端复校验用户确认清单，返回 (accepted, rejected)。

    规则（见 docs/replay/REPLAY_R3_R5_PLAN.md）：
    - 必须在提案内，方向一致
    - 数量只能调小，不能调大
    - 买入向下取整到整手；卖出允许零头（清仓）
    - 卖出不超可卖量；买入累计不超可用现金（按提案顺序）
    - 止损笔强制执行，用户剔除也加回
    """
    by_key = {(p["symbol"], p["side"]): p for p in proposals}
    confirmed_map: dict[tuple[str, str], int] = {}
    rejected: list[dict[str, Any]] = []

    for c in confirmed:
        sym = str(c.get("symbol") or "").upper()
        side = str(c.get("side") or "").upper()
        key = (sym, side)
        p = by_key.get(key)
        if p is None:
            rejected.append(
                {"symbol": sym, "side": side, "reason": "NOT_IN_PROPOSAL"}
            )
            continue
        try:
            qty = int(c.get("quantity") or 0)
        except (TypeError, ValueError):
            rejected.append(
                {"symbol": sym, "side": side, "reason": "INVALID_QUANTITY"}
            )
            continue
        if qty <= 0:
            rejected.append(
                {"symbol": sym, "side": side, "reason": "INVALID_QUANTITY"}
            )
            continue
        proposed_qty = int(p["quantity"])
        if qty > proposed_qty:
            rejected.append(
                {
                    "symbol": sym,
                    "side": side,
                    "reason": f"EXCEED_PROPOSED_QTY:{proposed_qty}",
                }
            )
            continue
        # 买入必须整手；卖出允许零头以便清仓
        if side == "BUY" and lot_size > 0:
            qty = (qty // lot_size) * lot_size
            if qty <= 0:
                rejected.append(
                    {"symbol": sym, "side": side, "reason": "BELOW_LOT_SIZE"}
                )
                continue
        confirmed_map[key] = qty

    # 止损强制加回
    for p in proposals:
        if p.get("cancellable") is False:
            confirmed_map[(p["symbol"], p["side"])] = int(p["quantity"])

    positions = account_data.get("positions") or {}
    cash_left = float(account_data.get("cash") or 0.0)
    accepted: list[dict[str, Any]] = []

    # 按提案顺序处理（止损/卖出在前），保证现金校验确定性
    for p in proposals:
        key = (p["symbol"], p["side"])
        qty = confirmed_map.get(key)
        if qty is None:
            continue
        sym, side = key
        px = float(p["est_price"])

        if side == "SELL":
            pos = positions.get(sym) or {}
            avail = pos.get("available_volume")
            cap = float(pos.get("volume", 0)) if avail is None else float(avail)
            if qty > cap:
                rejected.append(
                    {
                        "symbol": sym,
                        "side": side,
                        "reason": f"INSUFFICIENT_AVAILABLE:{int(cap)}",
                    }
                )
                continue
            cash_left += qty * px
        else:
            need = qty * px
            if need > cash_left:
                rejected.append(
                    {
                        "symbol": sym,
                        "side": side,
                        "reason": f"INSUFFICIENT_CASH:{round(cash_left, 2)}",
                    }
                )
                continue
            cash_left -= need

        accepted.append(
            {
                "symbol": sym,
                "side": side,
                "quantity": qty,
                "origin": p.get("origin", "signal"),
                "stop_price": p.get("stop_price"),
                "reason": p.get("reason", "manual"),
            }
        )
    return accepted, rejected

