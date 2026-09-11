"""A 股整手/板块数量规则（QMT 止损执行器与真单校验共用）。

实测口径（2026-09-11 真账户 40327478 压测）：

* SH/SZ 主板/创业板：买卖均需 100 的整数倍；**全量卖出**（数量 == 柜台可用量）
  允许碎股——柜台 ``251150`` 只在"部分卖出非整手"时拒单。
* 科创板（688/689）：买入单笔不低于 200 股、超过 200 股的部分按 1 股递增；
  卖出部分时 ≥200 股（低于 200 只能一次性全量卖出）。
* 北交所：最低 100 股，1 股递增。

本模块只负责"数量与板块"，不判断行情/价格；调用方负责把结果落到订单或告警。
"""

from __future__ import annotations

import math

BOARD_MAIN = "MAIN"
BOARD_GEM = "GEM"
BOARD_STAR = "STAR"
BOARD_BJ = "BJ"

_BOARD_NAMES = {
    BOARD_MAIN: "主板",
    BOARD_GEM: "创业板",
    BOARD_STAR: "科创板",
    BOARD_BJ: "北交所",
}

STAR_MIN_LOT = 200
DEFAULT_LOT = 100


def board_display(board: str) -> str:
    return _BOARD_NAMES.get(board, board)


def resolve_board(symbol: str) -> str:
    """代码 → 板块（支持 ``600036.SH`` / ``SH600036`` / ``600036``）。"""
    s = str(symbol or "").strip().upper()
    if not s:
        return BOARD_MAIN
    body, _, suffix = s.partition(".")
    code = body
    for prefix in ("SH", "SZ", "BJ"):
        if code.startswith(prefix) and len(code) > len(prefix):
            code = code[len(prefix):]
            break
    if suffix == "BJ" or code.startswith(("4", "8")):
        return BOARD_BJ
    if code.startswith(("688", "689")):
        return BOARD_STAR
    if code.startswith("30"):
        return BOARD_GEM
    return BOARD_MAIN


def align_sell_quantity(symbol: str, want: float, can_use: float) -> tuple[float, str]:
    """确定卖出报单数量（数量口径收敛，返回 ``(数量, 调整说明)``）。

    * ``want <= 0`` 或 ``want >= can_use`` → 全量卖出（碎股豁免，原样返回可用量）。
    * 部分卖出：主板/创业板按 100 向下取整（不足 100 时升到 100，可用不足则全量）；
      科创板部分卖出 ≥200 股、1 股递增（不足 200 降级为全量卖出）。
    * 可用量为 0 → 返回 ``(0, 原因)``，调用方应跳过并告警（T+1 锁定/已挂单占用）。
    """
    can = float(can_use or 0)
    if can <= 0:
        return 0.0, "柜台可用数量为 0（T+1 锁定或已被挂单占用）"
    q = float(want or 0)
    if q <= 0 or q >= can:
        return can, ""
    board = resolve_board(symbol)
    if board == BOARD_STAR:
        if q < STAR_MIN_LOT:
            return can, f"科创板部分卖出不足 {STAR_MIN_LOT} 股，按全量卖出 {can:g} 股"
        aligned = float(math.floor(q))
        if aligned != q:
            return aligned, f"科创板卖出按 1 股递增（{q:g}→{aligned:g}）"
        return aligned, ""
    if board == BOARD_BJ:
        if q < DEFAULT_LOT:
            if can >= DEFAULT_LOT:
                return float(DEFAULT_LOT), f"北交所卖出最少 {DEFAULT_LOT} 股（{q:g}→{DEFAULT_LOT}）"
            return can, f"北交所持仓不足 {DEFAULT_LOT} 股，按全量卖出 {can:g} 股"
        aligned = float(math.floor(q))
        if aligned != q:
            return aligned, f"北交所卖出按 1 股递增（{q:g}→{aligned:g}）"
        return aligned, ""
    if q % DEFAULT_LOT != 0:
        aligned = float(int(q // DEFAULT_LOT) * DEFAULT_LOT)
        if aligned <= 0:
            if can >= DEFAULT_LOT:
                return float(DEFAULT_LOT), f"部分卖出不足 1 手，按 1 手（{DEFAULT_LOT} 股）报单"
            return can, f"持仓不足 1 手，按全量卖出 {can:g} 股"
        return aligned, f"部分卖出整手对齐（{q:g}→{aligned:g}）"
    return q, ""


def describe_violation(
    symbol: str, side: str, quantity: float, *, full_position_sell: bool = False
) -> str | None:
    """数量是否必然被柜台拒（``None`` = 无已知问题）。用于提交前告警/拦截。"""
    qty = float(quantity or 0)
    if qty <= 0:
        return "数量必须大于 0"
    if qty != int(qty):
        return f"数量必须为整数股，got {qty:g}"
    side_raw = str(side or "").strip().upper()
    board = resolve_board(symbol)
    if side_raw == "SELL":
        if full_position_sell:
            return None
        label = board_display(board)
        if board == BOARD_STAR:
            if qty < STAR_MIN_LOT:
                return f"科创板部分卖出不足 {STAR_MIN_LOT} 股（余额不足时只能一次性全量卖出）"
            return None
        if board == BOARD_BJ:
            if qty < DEFAULT_LOT:
                return f"北交所卖出不足 {DEFAULT_LOT} 股"
            return None
        if qty % DEFAULT_LOT != 0:
            return f"{label}部分卖出需为 {DEFAULT_LOT} 股整数倍，got {qty:g}"
        return None
    if side_raw == "BUY":
        min_lot = STAR_MIN_LOT if board == BOARD_STAR else DEFAULT_LOT
        if qty < min_lot:
            return f"{board_display(board)}买入最少 {min_lot} 股，got {qty:g}"
        if board not in (BOARD_STAR, BOARD_BJ) and qty % DEFAULT_LOT != 0:
            return f"{board_display(board)}买入需为 {DEFAULT_LOT} 股整数倍，got {qty:g}"
        return None
    return None
