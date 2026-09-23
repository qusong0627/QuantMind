"""A 股交易规则契约层（执行侧唯一口径：数量 + 卖出定价 + 时段）。

实测口径（2026-09-11 真账户 40327478 压测）：

* SH/SZ 主板/创业板：买卖均需 100 的整数倍；**全量卖出**（数量 == 柜台可用量）
  允许碎股——柜台 ``251150`` 只在"部分卖出非整手"时拒单。
* 科创板（688/689）：买入单笔不低于 200 股、超过 200 股的部分按 1 股递增；
  卖出部分时 ≥200 股（低于 200 只能一次性全量卖出）。
* 北交所：最低 100 股，1 股递增。

**涨跌停幅度/价格不在此重算**——唯一事实源是
:mod:`backend.services.simulation.services.local_market_data`（``limit_pct`` /
``compute_limits`` / ``limit_threshold``），它已覆盖板别、ST 与历史改制日
（创业板 2020-08-24、ST 主板 2026-07-06），且北交所的截尾/进位与柜台一致。
本模块只做**执行侧**的事：把幅度换成"能报出去且能成交"的价格、把意图换成合法数量。

两条口径来自真单实录，改动前先读对应事故：

* **数量**（2026-09-08）：600 股 × 33% 意图 199 股被地板取整成 100（一半），
  模型下一轮对不上账 → 主板/创业板按**最近整手**取整。
* **定价**（2026-09-21）：止损触发时报跌停价属**越界申报** → 柜台废单，
  当日 002074 按跌停价报的 42 笔真单全废并形成每 2 分钟一笔的重试死循环
  → 见 :func:`aggressive_sell_price`。

本模块**纯函数、无 IO**；调用方负责把结果落到订单或告警。
"""

from __future__ import annotations

import math
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from zoneinfo import ZoneInfo

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

# 报价合理性上界：限价偏离参考价超过 ±20% 一律视为脏价格（不是交易意愿）。
# 唯一出处——``real_mirror_service._SANITY_MAX_DRIFT`` 曾在此重复定义。
SANITY_MAX_DRIFT = 0.20


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


_SH = ZoneInfo("Asia/Shanghai")

# 连续竞价：上午 09:30–11:30（不含 11:30），下午 13:00–14:57。
# 14:57–15:00 是**收盘集合竞价**——可申报但不可撤单，与连续竞价语义不同，故排除。
_CONTINUOUS_AUCTION = ((9 * 60 + 30, 11 * 60 + 30), (13 * 60, 14 * 60 + 57))


def _to_shanghai(now: datetime | None) -> datetime:
    """归一到上海时钟。naive 时间按**上海墙钟**解释。

    宿主可能跑在 JST（UTC+9）等非北京时区，用宿主本地钟会整体偏 1 小时且不报错。
    """
    current = now or datetime.now(_SH)
    if current.tzinfo is None:
        return current.replace(tzinfo=_SH)
    return current.astimezone(_SH)


def in_continuous_auction(now: datetime | None = None) -> bool:
    """是否处于沪深**连续竞价**时段（纯函数）。

    ⚠️ 与 :func:`backend.services.live_trading.services.risk_trigger_service.is_cn_continuous_auction`
    是**两个不同谓词**，勿合并：那个用于「风险扫描要不要跑」，窗口到 15:00（含收盘
    集合竞价——止损在收盘竞价里成交是可以接受的）；本函数用于**报价与撤单语义**
    （收盘集合竞价不可撤单，重挂类逻辑在此窗口必须停手）。
    """
    current = _to_shanghai(now)
    if current.weekday() >= 5:  # 周末；节假日需交易日历，此处不判（调用方另有闸门）
        return False
    m = current.hour * 60 + current.minute
    return any(start <= m < end for start, end in _CONTINUOUS_AUCTION)


def _half_up_cent(value: float) -> float | None:
    """四舍五入到分（``ROUND_HALF_UP``，**非**银行家舍入）。

    交易所报价用 HALF_UP；Python 内建 ``round`` 是银行家舍入，会与柜台差一分。
    非有限值返回 ``None`` —— ``Inf`` 会让 ``quantize`` 抛异常，``NaN`` 会被静默
    序列化成非法报价单。
    """
    try:
        d = Decimal(repr(float(value)))
    except (TypeError, ValueError):
        return None
    if not d.is_finite():
        return None
    try:
        return float(d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    except InvalidOperation:
        return None


def price_limit_pct(
    symbol: str, *, is_st: bool = False, trade_date: date | None = None
) -> Decimal:
    """该标的当日的涨跌幅限制比例（唯一事实源见模块 docstring）。

    ``is_st`` 由调用方给出：本仓**没有**逐日 ST 口径（静态快照即前视偏差），
    故不在此臆造。2026-07-06 起 ST 主板与主板同幅，今天的调用点传 False 与
    True 等价；判**历史**日期时缺口是真的。
    """
    from backend.services.simulation.services.local_market_data import limit_pct

    return limit_pct(
        symbol,
        is_st=is_st,
        trade_date=trade_date or _to_shanghai(None).date(),
    )


def protect_sell_price(
    symbol: str,
    pre_close: float | None,
    *,
    is_st: bool = False,
    trade_date: date | None = None,
    floor: float | None = None,
) -> float | None:
    """卖出**硬下限** = 当日跌停价。

    ⚠️ 这是「合法区间的下界」，**不是**一个可以报出去的价格：连续竞价的有效竞价
    范围是 ``[基准价 × 98%, 涨停价]``，报跌停价属**越界申报** → 柜台废单。
    要能成交的报单价用 :func:`aggressive_sell_price`；本函数只用于**夹取**
    （``max(跌停价, 报价)``）与跌停排队场景（此时基准价本身贴近跌停价，报跌停价才合法）。

    ``floor`` 传入时直接采用（桥的 ``DownStopPrice`` 是权威值，已含板别/ST/日期口径，
    不必也不该在本地重算）。昨收缺失/非法且未注入 ``floor`` → ``None``，不臆造价格。
    """
    if floor is not None:
        f = _half_up_cent(floor)
        return f if f and f > 0 else None
    try:
        prev = float(pre_close)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(prev) or prev <= 0:
        return None
    from backend.services.simulation.services.local_market_data import compute_limits

    _up, down = compute_limits(
        symbol,
        prev,
        is_st=is_st,
        trade_date=trade_date or _to_shanghai(None).date(),
    )
    down = _half_up_cent(down)
    return down if down and down > 0 else None


def aggressive_sell_price(
    symbol: str,
    pre_close: float | None,
    ref_price: float | None,
    *,
    is_st: bool = False,
    trade_date: date | None = None,
    floor: float | None = None,
) -> float | None:
    """连续竞价卖出**可成交且合法的最激进报价** = ``max(跌停价, 现价 × 0.99)``。

    2026-09-21 实录（002074）：止损位触发，市价 26.26 却报跌停价 23.53
    （比市价低 10.4%）→ **42 笔真单全 rejected、0 成交**，并形成
    「触发→废单→重布防→再触发」每 2 分钟一笔的死循环。原「挂 2.21 成交 2.34」
    的实测结论只对当时那只票的工况（近跌停）成立，不能推广。

    取现价 × 0.99 而非贴着 98% 下沿：留 1% 余量给「报价 → 柜台」在途的基准价波动
    （贴下沿报价遇基准价上抬即越界）。近跌停时现价 × 0.99 会跌破跌停价 ——
    此时取跌停价（基准价已贴近跌停价，跌停价仍在带内，合法）。

    现价缺失/非法（``None`` / ``0`` / ``NaN`` / ``±Inf``）→ ``None``，
    **绝不退回跌停价**（那正是那 42 笔废单的报价）。
    """
    if floor is None:
        floor = protect_sell_price(
            symbol, pre_close, is_st=is_st, trade_date=trade_date
        )
    try:
        ref = float(ref_price)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(ref) or ref <= 0:
        return None
    quote = _half_up_cent(ref * 0.99)
    if quote is None or quote <= 0:
        return None
    # 比隔壁严一档：拿不到下限就**不报价**，而不是「无下限照报」。
    # 没有下限时无法分辨「正常回调」与「已封跌停」——后者报现价 × 0.99 必然越界。
    if floor is None:
        return None
    return max(quote, floor)


def resolve_limit_price(
    side: str,
    base_price: float | None,
    *,
    requested: float | None = None,
    max_slip: float = 0.02,
    sanity: float = SANITY_MAX_DRIFT,
) -> tuple[float | None, str]:
    """定出**报得出去**的限价（调用方限价合法则采用，否则拒绝）。返回 ``(价, 问题)``。

    ``requested=None`` 时按既有公式派生：买 ``base × (1+slip)``、卖 ``base × (1-slip)``
    ——与 ``real_mirror_service._submit_payload`` 的历史行为一致，唯一差别是改用
    :func:`_half_up_cent`（``ROUND_HALF_UP`` 是交易所口径，内建 ``round`` 是银行家
    舍入，恰在 ``.xx5`` 会差一分）。

    给出 ``requested`` 时（隔壁 LLM 的 ``limit_px`` 就是这种"贴着打保成交"的价），
    **单边带**语义：

    * 买：不得**高于** ``base × (1+slip)``；卖：不得**低于** ``base × (1-slip)``。
    * 反方向（买报更低、卖报更高）只是"挂远了可能不成交"，不拒；
      但偏离 ``base`` 超过 ``sanity`` 视为**脏价格**（不是交易意愿）→ 拒。
    * 越界一律 ``None`` + 原因，**绝不静默回落到派生价** —— 改价即伪造回执。

    拒因（调用方据此告警/回执，勿当字符串瞎比）：``unknown_side`` /
    ``no_reference_price`` / ``limit_price_invalid`` / ``limit_price_not_positive`` /
    ``limit_price_too_aggressive`` / ``limit_price_sanity``。
    """
    raw_side = str(side or "").strip().upper()
    if raw_side not in ("BUY", "SELL"):
        return None, "unknown_side"
    is_buy = raw_side == "BUY"
    try:
        base = float(base_price)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None, "no_reference_price"
    if not math.isfinite(base) or base <= 0:
        return None, "no_reference_price"
    # 带不得比 sanity 更宽：``max_slippage_pct`` 配成 0.3 也只会拿到 ±sanity 的带。
    try:
        slip = float(max_slip)
    except (TypeError, ValueError):
        slip = 0.0
    if not math.isfinite(slip) or slip < 0:
        slip = 0.0
    slip = min(slip, sanity)
    band = _half_up_cent(base * (1 + slip) if is_buy else base * (1 - slip))
    if band is None or band <= 0:
        return None, "limit_price_not_positive"
    if requested is None:
        return band, ""
    try:
        want = float(requested)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None, "limit_price_invalid"
    if not math.isfinite(want):
        return None, "limit_price_invalid"
    if want <= 0:
        return None, "limit_price_not_positive"
    price = _half_up_cent(want)
    if price is None or price <= 0:
        return None, "limit_price_not_positive"
    if (is_buy and price > band) or (not is_buy and price < band):
        return None, "limit_price_too_aggressive"
    if sanity > 0 and abs(price - base) / base > sanity:
        return None, "limit_price_sanity"
    return price, ""


_LIMIT_PROBLEM_TEXT = {
    "unknown_side": "买卖方向无法识别",
    "no_reference_price": "没有参考价（快照缺失），无从判断限价是否合理",
    "limit_price_invalid": "限价不是有效数字",
    "limit_price_not_positive": "限价必须大于 0",
    "limit_price_too_aggressive": "限价越过当日允许的滑点带",
    "limit_price_sanity": "限价偏离参考价过远（超过 ±20%），疑似脏数据",
}


def describe_limit_problem(problem: str) -> str:
    """限价拒因 → 面向用户的一句中文（未知拒因原样透出，不编一句假的）。"""
    key = str(problem or "").strip()
    return _LIMIT_PROBLEM_TEXT.get(key, key or "未知原因")


def align_sell_quantity(symbol: str, want: float, can_use: float) -> tuple[float, str]:
    """确定卖出报单数量（数量口径收敛，返回 ``(数量, 调整说明)``）。

    * ``want <= 0`` 或 ``want >= can_use`` → 全量卖出（碎股豁免，原样返回可用量）。
    * **主板/创业板**：按**最近整手**取整（恰半手向下，不放大意图）。
      2026-09-08 实录：600 股 × 33% 意图 199 股被地板取整成 100（一半），
      模型下一轮对不上账 —— 故不用 ``floor``。
    * **科创板/北交所**：1 股递增，意图本身合法；不足最小申报量（200/100）时
      **抬到最小申报量**，而非清仓。
    * 卖出后剩余为碎股（< 1 手）→ 一次性全清，防之后卖不掉。
    * 可用量为 0 → 返回 ``(0, 原因)``，调用方应跳过并告警（T+1 锁定/已挂单占用）。
    """
    can = float(can_use or 0)
    if can <= 0:
        return 0.0, "柜台可用数量为 0（T+1 锁定或已被挂单占用）"
    q = float(want or 0)
    if q <= 0 or q >= can:
        return can, ""
    board = resolve_board(symbol)
    lot = min_qty = STAR_MIN_LOT if board == BOARD_STAR else DEFAULT_LOT
    if can < min_qty:
        return can, f"可用 {can:g} 股不足最小申报量 {min_qty} 股，按全量卖出"
    note = ""
    if board in (BOARD_STAR, BOARD_BJ):
        qty = int(q)  # 1 股递增
    else:
        raw = int(q)
        qty = (raw // lot) * lot + (lot if raw % lot > lot // 2 else 0)
        if qty != raw:
            note = f"部分卖出整手对齐（{q:g}→{qty:g}）"
    if qty < min_qty:
        note = (
            f"部分卖出不足{board_display(board)}最小申报量 "
            f"{min_qty} 股（{q:g}→{min_qty}）"
        )
        qty = min_qty
    if can - qty < lot:
        note = f"{note}；" if note else ""
        note += f"剩余 {can - qty:g} 股为碎股，一次性全清 {can:g} 股"
        qty = int(can)
    return float(min(qty, int(can))), note


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
