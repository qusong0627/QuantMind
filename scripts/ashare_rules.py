#!/usr/bin/env python3
"""A股板块交易规则速查（全管线统一的单一口径）。

覆盖：板块判定、涨跌停幅度、买卖最低申报量/手数、盘后固定价格交易窗口。
所有执行路径（哨兵/整点轮/自主调仓/强平守护/延期重放）与提示词注入统一引用，
禁止在各脚本里散落硬编码（2026-09-04 科创板碎股拒单、±9.9% 统一涨跌停误判复盘）。

口径（沪深交易所交易规则）：
- 主板（沪 600/601/603/605，深 000/001/002/003）：±10%（ST ±5%）；
  买入 100 股整数倍；卖出 100 股整数倍，碎股一次性卖出
- 创业板（300/301/302）：±20%；100 股整数倍，碎股一次性卖出；盘后固定价格交易 15:05-15:30
- 科创板（688/689）：±20%；买入/卖出申报 ≥200 股、可 1 股递增，余额不足 200 股一次性卖出；
  盘后固定价格交易 15:05-15:30
- 北交所（43/83/87/92 等）：±30%；申报 ≥100 股、可 1 股递增，不足 100 股一次性卖出
- 新股上市初期涨跌幅特殊（科创板/创业板前 5 日无涨跌幅、主板首日另计）——本模块不追踪
  上市日，全新股的闸门可能不准，由人工/复盘注意。
"""
import math
from datetime import date, datetime, time as _time

AH_START, AH_END = _time(15, 5), _time(15, 30)   # 盘后固定价格交易窗口


def board_of(code: str) -> str:
    """'star'科创板 | 'chinext'创业板 | 'bse'北交所 | 'main'主板 | 'unknown'。"""
    c = str(code).split(".")[0]
    if c.startswith(("688", "689", "689")):
        return "star"
    if c.startswith(("300", "301", "302")):
        return "chinext"
    if c.startswith(("43", "83", "87", "92", "88")) and len(c) == 6:
        return "bse"
    if c.startswith(("600", "601", "603", "605", "000", "001", "002", "003")):
        return "main"
    return "unknown"


def is_star_market(code: str) -> bool:
    return board_of(code) == "star"


# 最小可买股数（买入申报下限）：科创板 200 股起，主板/创业板/北交所 100 股起。
# 按资金量筛选/拦截（filter_affordable、compute_order）必须用它，不要各写各的。
MIN_BUY_QTY = {"star": 200}
DEFAULT_MIN_BUY_QTY = 100

# 判「买得起」的容差：限价买按现价+1% 报，故最小一手金额略超预算也算买得起。
MIN_LOT_SLACK = 1.02


def min_buy_qty(code: str) -> int:
    """最小可买股数（一次买入申报的下限）。"""
    return MIN_BUY_QTY.get(board_of(code), DEFAULT_MIN_BUY_QTY)


def min_buy_cost(code: str, price) -> float:
    """最小可买金额 = 最小可买股数 × 现价；价格非法返回 0（调用方按放行处理）。"""
    try:
        px = float(price or 0)
    except (TypeError, ValueError):
        return 0.0
    return round(min_buy_qty(code) * px, 2) if px > 0 else 0.0


# 主板风险警示股带宽沿革（沪深交易所《交易规则》2026-04-24 修订，2026-07-06 生效）：
# < 2026-07-06：ST/*ST ±5%；≥ 2026-07-06：与普通股一致 ±10%。创业板/科创板/北交所
# 的风险警示股从来就是原带宽。审计历史样本时按 bar 日期解析，勿全样本一刀切。
_ST_MAIN_BOARD_WIDENED = date(2026, 7, 6)


def _guess_st(code: str) -> bool:
    """显式 name 缺省时从静态名称表兜底识别 ST（查表失败按非 ST，纯规则模块不做 IO 重试）。"""
    try:
        from tools.stock_names import CN_STOCK_NAMES

        return "ST" in str(CN_STOCK_NAMES.get(code) or "").upper()
    except Exception:  # noqa: BLE001
        return False


def price_limit_pct(code: str, name: str | None = None, d: date | None = None) -> float:
    """涨跌停幅度（%），按板块+ST+日期解析：
    主板 ±10（ST：2026-07-06 前 ±5，之后 ±10，沪深交易所《交易规则》2026 修订）；
    创业板/科创板 ±20（含风险警示）；北交所 ±30（含风险警示）。
    name=None 时用静态名称表兜底识别 ST。"""
    b = board_of(code)
    if b == "star" or b == "chinext":
        return 20.0
    if b == "bse":
        return 30.0
    is_st = ("ST" in str(name).upper()) if name is not None else _guess_st(code)
    if is_st:
        eff = d or date.today()
        return 5.0 if eff < _ST_MAIN_BOARD_WIDENED else 10.0
    return 10.0


def limit_price(pre_close: float, pct: float, direction: str) -> float | None:
    """涨跌停价 = 前收盘 × (1±pct)，四舍五入到 0.01 元（ROUND_HALF_UP，非银行家舍入）。"""
    from decimal import ROUND_HALF_UP, Decimal

    if not pre_close or pre_close <= 0:
        return None
    q = Decimal(str(pre_close)) * (Decimal("1") + Decimal(str(pct)) / 100
                                   * (1 if direction == "up" else -1))
    return float(q.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def at_limit_down(code: str, day_chg: float | None, name: str | None = None) -> bool:
    """当日涨跌是否已到/接近跌停（0.1pp 容差，兼容旧 -9.9 口径）。"""
    if day_chg is None:
        return False
    return day_chg <= -price_limit_pct(code, name) + 0.1


def protect_sell_price(code: str, pre_close: float | None,
                       name: str | None = None) -> float | None:
    """卖出价格**硬下限** = 当日跌停价（昨收 × (1−幅度)，四舍五入到分）。

    注意（2026-09-21 修正）：跌停价是「合法带的下界」而非「可报价」——
    连续竞价的有效竞价范围是 [基准价×98%, 涨停价]，报跌停价（−10%）属**越界申报**
    → 柜台废单（当日 002074 按跌停价报的 42 笔真单全废）。要真能成交的报单价
    用 aggressive_sell_price()；本函数只用于「不许报低于跌停价」的夹取和
    跌停排队场景（此时基准价本身贴近跌停价，报跌停价才合法）。

    原 2026-09-11「挂 2.21 成交 2.34」的实测结论只对**当时那只票的工况**成立
    （近跌停/竞价口径），不能推广成「报跌停价永远合法」——已在 09-21 被证伪。

    昨收缺失/非法返回 None（调用方降级，不臆造价格）。
    """
    try:
        prev = float(pre_close)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(prev) or prev <= 0:   # NaN/Inf 不得当昨收（Inf 会让 quantize 抛）
        return None
    return limit_price(prev, price_limit_pct(code, name), "down")


def aggressive_sell_price(code: str, pre_close: float | None, ref_price: float | None,
                          name: str | None = None) -> float | None:
    """连续竞价卖出**可成交且合法**的最激进报价 = max(跌停价, 现价 × 0.99)。

    有效竞价范围（沪深交易所连续竞价）：卖出申报不得低于「卖出基准价格」的 98%
    （基准价 = 盘口买一/最新价一类即时价）。报跌停价（−10%）只有在该票已贴近
    跌停时才落在带内，其余时候是**废单**——2026-09-21 002074 实录：止损位触发，
    市价 26.26 报跌停价 23.53（比市价低 10.4%），42 笔真单全 rejected、0 成交，
    还形成「触发→废单→重布防→再触发」每 2 分钟一笔的死循环。

    取现价×0.99 而非贴着 98% 下沿：留 1% 余量给「报价→柜台」在途的基准价波动
    （下沿报价遇到基准价上抬即越界），且本仓 LLM 卖出链路长期用 0.99 口径
    （2026-09-21 002202 限价 18.01 成交 18.18）。近跌停时现价×0.99 会低于跌停价
    → 此时取跌停价（基准价已贴近跌停价，跌停价仍在 98% 带内，合法）。

    现价取不到/非法（None/0/NaN）→ None（调用方降级：本轮不下单或走同口径兜底）。
    **绝不退回跌停价**——那正是 2026-09-21 那 42 笔废单的报价（审查 MEDIUM-3：
    现价缺失而昨收存在时旧实现 return floor，契约上把「最激进可成交价」退化成
    已知废单价）。跌停价只作为**下限夹取**参与（见下），不单独成价。
    """
    floor = protect_sell_price(code, pre_close, name)
    try:
        ref = float(ref_price)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        ref = 0.0
    # 非有限值不得进报价链（2026-09-21 审查 MEDIUM-5）：NaN 会让 ref<=0 判假 → 报出
    # NaN 单；+Inf 会让 quantize 抛 InvalidOperation，且调用点在 run_watch 的 per-rule
    # 循环里**无捕获** → 整轮哨兵崩、cron 每分钟重跑同错。
    if not math.isfinite(ref) or ref <= 0:
        return None
    quote = limit_price(ref, 1.0, "down")   # 现价 −1%，与全仓同一套 HALF_UP 到分
    if quote is None or quote <= 0:         # 0 价会被桥当「无价」→ 市价单语义，禁止
        return None
    if floor is not None and quote < floor:
        quote = floor
    return quote


def at_limit_up(code: str, day_chg: float | None, name: str | None = None) -> bool:
    if day_chg is None:
        return False
    return day_chg >= price_limit_pct(code, name) - 0.1


def round_sell_qty(code: str, raw: int, avail: int) -> int:
    """把目标卖出股数调成交易所可受理的量。返回 0 = 无合法可卖量。

    - 科创板/北交所：≥200/≥100 股起、1 股递增 → 起报量之上意图本身合法；
      卖出后会留下"一次性才能卖出"的碎股尾部 → 直接一次性全清（防之后卖不掉）
    - 主板/创业板：100 股整数倍 → 最近整手取整（恰半手向下，不放大意图）；
      2026-09-08 实录：600 股×33% 意图 199 股被地板取整成 100（一半），
      模型下一轮对不上账——取整改最近值，配合 intent_volume 对照行闭环
    """
    raw = min(max(int(raw or 0), 0), max(int(avail or 0), 0))
    if raw <= 0 or avail <= 0:
        return 0
    b = board_of(code)
    if b == "star":
        lot, min_qty = 200, 200
    elif b == "bse":
        lot, min_qty = 100, 100
    else:
        lot, min_qty = 100, 100  # 主板/创业板（unknown 按主板保守处理）
    if avail < min_qty:
        return avail  # 不足起报量只能一次性全卖
    if b in ("star", "bse"):
        qty = raw  # 1 股递增：任意量合法，不足起报量由下一行抬升
    else:
        qty = (raw // lot) * lot + (lot if raw % lot > lot // 2 else 0)
    if qty < min_qty:
        qty = min_qty  # 目标不足起报量 → 抬到最小可申报量
    if avail - qty < lot:
        qty = avail  # 剩余会是碎股 → 一次性全清
    return min(qty, avail)


def round_buy_qty(code: str, qty: int) -> int:
    """把目标买入股数调成可申报量（资金充足性由调用方另行校验）。返回 0 = 非法。"""
    qty = max(int(qty or 0), 0)
    if qty <= 0:
        return 0
    b = board_of(code)
    if b == "star":
        return max(qty, 200)  # ≥200 股、1 股递增
    if b == "bse":
        return max(qty, 100)
    return (qty // 100) * 100  # 主板/创业板 100 股整数倍


def after_hours_window(now: datetime) -> bool:
    """盘后固定价格交易窗口：交易日 15:05-15:30（科创板/创业板标的，收盘价撮合）。"""
    if now.weekday() >= 5:
        return False
    return AH_START <= now.time() <= AH_END


def in_continuous_auction(now: datetime) -> bool:
    """连续竞价时段（北京 9:30-11:30 / 13:00-14:57）——实盘下单的执行窗口。

    2026-09-18 实录：整点轮被数据源退避拖到收盘后执行，16:08 的卖出委托被柜台
    判废（fill_abort rejected）。非本窗口一律不下单：
      - 14:57-15:00 是收盘集合竞价（价格由竞价撮合，哨兵/整点轮都不下实单）；
      - 11:30-13:00 午休、开盘前、收盘后：委托要么被拒要么无意义。
    """
    if now.weekday() >= 5:
        return False
    m = now.hour * 60 + now.minute
    return (9 * 60 + 30 <= m < 11 * 60 + 30) or (13 * 60 <= m < 14 * 60 + 57)


def after_hours_eligible(code: str) -> bool:
    """盘后固定价格交易：2026-07-06 起由科创板扩展至全部 A 股（沪深主板/创业板/科创板）。
    北交所独立交易所不适用。"""
    return board_of(code) in ("main", "star", "chinext")


def rules_brief() -> str:
    """注入提示词的一行速览（给 agent 的规则常识，勿再逐条向工具求证）。
    口径 = 沪深交易所《交易规则》2026-04-24 修订（2026-07-06 实施）。"""
    return ("板块交易规则：主板涨跌停±10%（ST/*ST 2026-07-06 起同步放宽至±10%）、"
            "创业板/科创板±20%、北交所±30%；买入100股整数倍（科创板最低200股、可1股递增）；"
            "卖出同口径且碎股一次性卖出；2026-07-06 起盘后固定价格交易扩展至全部A股"
            "（15:05-15:30 收盘价撮合）；创业板引入做市商；北交所将推盘后定价并对风险警示股"
            "实行当日累计买入≤20万股（实施时间以交易所通知为准）。")


if __name__ == "__main__":
    from datetime import datetime as _dt
    print(rules_brief())
    for c in ("600309.SH", "688183.SH", "300750.SZ", "301170.SZ", "832000.BJ"):
        print(c, board_of(c), f"±{price_limit_pct(c)}%",
              "sell30%of600:", round_sell_qty(c, 180, 600),
              "buy100:", round_buy_qty(c, 100))
    print("盘后窗口 15:20:", after_hours_window(_dt(2026, 9, 4, 15, 20)),
          " 15:45:", after_hours_window(_dt(2026, 9, 4, 15, 45)),
          " 11:30:", after_hours_window(_dt(2026, 9, 4, 11, 30)))
