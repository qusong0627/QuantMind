"""Big QMT order gateway.

The passorder signature follows src/api/qmt_jq_trade.
"""

import hashlib


class _QmtFallbackXtConstant(object):
    """QMT 策略沙箱没有可导入 xtquant package 时使用的最小常量集。"""

    FUTURE_ACCOUNT = 1
    SECURITY_ACCOUNT = 2
    CREDIT_ACCOUNT = 3
    FUTURE_OPTION_ACCOUNT = 5
    STOCK_OPTION_ACCOUNT = 6
    HUGANGTONG_ACCOUNT = 7
    SHENGANGTONG_ACCOUNT = 11
    CREDIT_FIN_BUY = 27
    CREDIT_SLO_SELL = 28
    CREDIT_BUY_SECU_REPAY = 29
    CREDIT_DIRECT_SECU_REPAY = 30
    CREDIT_SELL_SECU_REPAY = 31
    CREDIT_DIRECT_CASH_REPAY = 32
    CREDIT_FIN_BUY_SPECIAL = 40
    CREDIT_SLO_SELL_SPECIAL = 41
    CREDIT_BUY_SECU_REPAY_SPECIAL = 42
    CREDIT_DIRECT_SECU_REPAY_SPECIAL = 43
    CREDIT_SELL_SECU_REPAY_SPECIAL = 44
    CREDIT_DIRECT_CASH_REPAY_SPECIAL = 45


try:
    from xtquant import xtconstant as _xtconstant
except ImportError:
    # 完整 QMT 的模型运行时会注入 passorder 等 API，但部分券商终端不提供可 import 的 xtquant package。
    _xtconstant = _QmtFallbackXtConstant()

from ..account_type_map import account_type_for
from ..code_utils import normalize_stock_code
from ..exec_events import date_time_seconds
from ..models import CancelResult, OrderSnapshot, OrderSubmitResult, SignalAction, TradeSnapshot
from .position_bigqmt import _attr, _full_code, skip_unparsable_row


# Cap on the masked shape reported by describe_detail_fields (issue #154):
# long enough to show the segment layout, short enough that nothing large
# rides back on a diagnostic call.
_SHAPE_MASK_MAX = 200


PRICE_TYPE_ALIASES = {
    "LIMIT": 11,
    "FIX_PRICE": 11,
    "LATEST_PRICE": 5,
    "MARKET_PEER_PRICE_FIRST": 44,
    "MARKET_SH_CONVERT_5_LIMIT": 43,
    "MARKET_SZ_CONVERT_5_CANCEL": 47,
}


# MiniQMT's order_type (xtconstant) and passorder's opType are two different
# numberings. They agree on 27-32 and diverge on the special-margin family:
#
#   meaning            xtconstant           passorder opType (API ref 10.1)
#   融资买入 .. 直接还款   27-32                27-32      same
#   担保品买入 / 卖出      -- (CREDIT_BUY/SELL)  33 / 34
#   专项两融              40-45                70-75      DIFFERENT
#
# Sending 40 through unchanged would reach passorder as "期货组合开多", so the
# translation is not optional. Values are taken from xtconstant by NAME: PR #88
# asserted them as literals and encoded the same mistake in its tests, which is
# why they passed while the mapping was wrong.
_XC = _xtconstant

def _account_type_codes():
    """Name -> xtconstant account-type code.

    Read off xtconstant rather than written out (PR #88 is the standing
    reminder about literals), but NOT off ACCOUNT_TYPE_DICT: the xtquant that
    wins inside Big QMT is the terminal's own bundled copy at
    bin.x64/Lib/site-packages/xtquant, not this repo's shim, and it carries 91
    names where the shim has 538. ACCOUNT_TYPE_DICT is one of the 447 it does
    not have -- reading it at import time took the whole order gateway down
    with "module 'xtquant.xtconstant' has no attribute ACCOUNT_TYPE_DICT",
    which surfaces to clients as "order_gateway is not configured".

    So: the dict when it exists, and the individual *_ACCOUNT constants
    otherwise. Those 8 are in both copies.
    """
    codes = {}
    table = getattr(_xtconstant, "ACCOUNT_TYPE_DICT", None)
    if isinstance(table, dict):
        for code, name in table.items():
            try:
                codes[str(name).strip().upper()] = int(code)
            except (TypeError, ValueError):
                continue
    for attribute in dir(_xtconstant):
        if not attribute.endswith("_ACCOUNT"):
            continue
        value = getattr(_xtconstant, attribute)
        if isinstance(value, int) and not isinstance(value, bool):
            codes.setdefault(attribute[:-len("_ACCOUNT")], value)
    # ACCOUNT_TYPE_DICT names SECURITY_ACCOUNT "STOCK"; the attribute scan
    # yields "SECURITY". Both spellings reach callers, so keep both.
    security = getattr(_xtconstant, "SECURITY_ACCOUNT", 2)
    codes.setdefault("STOCK", security)
    codes.setdefault("SECURITY", security)
    return codes


ACCOUNT_TYPE_CODES = _account_type_codes()


def _first_nonzero(row, names, default=0.0):
    """First candidate attribute with a non-zero numeric value."""
    for name in names:
        value = getattr(row, name, None)
        if value is None:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number:
            return number
    return default


# 报单来源 last -- see exec_events._STRATEGY_NAME_FIELDS for why it belongs
# here at all. Kept character-for-character the same as that table, so a row
# and the event for the same order cannot disagree about who placed it; a
# literal copy rather than an import, because order_bigqmt is on the sandbox's
# single-file import graph. tests/.../test_order_source_strategy_name.py pins
# the two together. "strategyName" cannot appear on a native ORDER/DEAL row
# (and _attr here reads attributes only, never dict keys), so carrying it
# costs a miss on a name no row has -- cheaper than two tables drifting apart.
_STRATEGY_NAME_FIELDS = (
    "strategyName", "m_strStrategyName", "strategy_name", "m_strSource",
)


def _row_strategy_name(row):
    """First NON-EMPTY strategy-name candidate on a QMT row, or "".

    _attr stops at the first non-None and "" is not None, so m_strStrategyName
    present-but-blank -- which is what this terminal reports for every row --
    would shadow m_strSource and answer "unnamed" for an order that names
    itself.
    """
    for name in _STRATEGY_NAME_FIELDS:
        text = str(_attr(row, (name,), "") or "").strip()
        if text:
            return text
    return ""


def _data_attribute_names(row):
    """Public non-callable attribute names on a QMT row object.

    dir() alone lists methods too. Reading each one to check is safe -- the
    values are discarded -- but a property can raise, so each getattr is
    guarded rather than assumed.
    """
    names = []
    for name in dir(row):
        if name.startswith("_"):
            continue
        try:
            value = getattr(row, name)
        except Exception:
            continue
        if not callable(value):
            names.append(name)
    return sorted(names)



CREDIT_OPTYPE_BY_ORDER_TYPE = {
    _XC.CREDIT_FIN_BUY: 27,                    # 融资买入
    _XC.CREDIT_SLO_SELL: 28,                   # 融券卖出
    _XC.CREDIT_BUY_SECU_REPAY: 29,             # 买券还券
    _XC.CREDIT_DIRECT_SECU_REPAY: 30,          # 直接还券
    _XC.CREDIT_SELL_SECU_REPAY: 31,            # 卖券还款
    _XC.CREDIT_DIRECT_CASH_REPAY: 32,          # 直接还款
    _XC.CREDIT_FIN_BUY_SPECIAL: 70,            # 专项融资买入
    _XC.CREDIT_SLO_SELL_SPECIAL: 71,           # 专项融券卖出
    _XC.CREDIT_BUY_SECU_REPAY_SPECIAL: 72,     # 专项买券还券
    _XC.CREDIT_DIRECT_SECU_REPAY_SPECIAL: 73,  # 专项直接还券
    _XC.CREDIT_SELL_SECU_REPAY_SPECIAL: 74,    # 专项卖券还款
    _XC.CREDIT_DIRECT_CASH_REPAY_SPECIAL: 75,  # 专项直接还款
}

# Which side of the book each one is, for bookkeeping only -- the opType above
# is what actually goes to passorder. Repayment operations that move securities
# are classified by what they do to the holding.
_CREDIT_BUY_SIDE = frozenset({
    _XC.CREDIT_FIN_BUY, _XC.CREDIT_BUY_SECU_REPAY,
    _XC.CREDIT_FIN_BUY_SPECIAL, _XC.CREDIT_BUY_SECU_REPAY_SPECIAL,
})
_CREDIT_SELL_SIDE = frozenset({
    _XC.CREDIT_SLO_SELL, _XC.CREDIT_SELL_SECU_REPAY,
    _XC.CREDIT_DIRECT_SECU_REPAY,
    _XC.CREDIT_SLO_SELL_SPECIAL, _XC.CREDIT_SELL_SECU_REPAY_SPECIAL,
    _XC.CREDIT_DIRECT_SECU_REPAY_SPECIAL,
})


# passorder 的 opType 直通表：期货和 ETF 期权把「开/平、今/昨」编码在 opType
# 本身里。映射回 BUY/SELL 再重新拼一个 opType 会丢掉这些信息 —— 平今多会变成
# 普通卖出，这跟 issue #103 里 融资买入 变普通买入 是同一类错误。所以原样透传。
#
# 依据：https://dict.thinktrader.net/innerApi/enum_constants.html?id=NF25nX
#   期货/股指期权/商品期权   0-15   六键 0-5、四键 6-9、两键 10-15
#   ETF 期权                50-59
# 官方表里 16-22 没有定义，不要往里塞。
FUTURE_OP_TYPES = frozenset(range(0, 16))
ETF_OPTION_OP_TYPES = frozenset(range(50, 60))
PASSTHROUGH_OP_TYPES = FUTURE_OP_TYPES | ETF_OPTION_OP_TYPES

# 只用于记账的买卖方向。真正送进 passorder 的是上面的原始 opType。
#   平多 / 平昨多 / 平今多 是卖出动作；平空 / 平昨空 / 平今空 是买入动作。
_FUTURE_BUY_SIDE = frozenset({
    0,   # 开多
    4,   # 平昨空
    5,   # 平今空
    8,   # 平空，优先平今
    9,   # 平空，优先平昨
    12,  # 买入，如有空仓优先平今，余量开多
    13,  # 买入，如有空仓优先平昨，余量开多
    14,  # 买入，不优先平仓
})
_FUTURE_SELL_SIDE = frozenset({
    1,   # 平昨多
    2,   # 平今多
    3,   # 开空
    6,   # 平多，优先平今
    7,   # 平多，优先平昨
    10,  # 卖出，如有多仓优先平今，余量开空
    11,  # 卖出，如有多仓优先平昨，余量开空
    15,  # 卖出，不优先平仓
})
_ETF_OPTION_BUY_SIDE = frozenset({
    50,  # 买入开仓
    53,  # 买入平仓
    55,  # 备兑平仓
})
_ETF_OPTION_SELL_SIDE = frozenset({
    51,  # 卖出平仓
    52,  # 卖出开仓
    54,  # 备兑开仓
})
# 56 认购行权 / 57 认沽行权 / 58 证券锁定 / 59 证券解锁 没有买卖方向，
# 和 直接还款(32) 一样必须由调用方显式传 action。

# 能接受直通 opType 的账号类型（见 init_config.ACCOUNT_TYPES）。
# 股票账号收到期货 opType 时必须拒绝，不能回落到 23/24 —— 那会真的发出
# 一笔品种和方向都不对的股票单。
PASSTHROUGH_ACCOUNT_TYPES = frozenset({"FUTURE", "STOCK_OPTION"})


def passthrough_optype_of(order_type):
    """原样送进 passorder 的期货/期权 opType，不是则 None。"""
    try:
        value = int(order_type)
    except (TypeError, ValueError):
        return None
    return value if value in PASSTHROUGH_OP_TYPES else None


def passthrough_action_of(order_type):
    """期货/期权 opType 的买卖方向；没有方向（行权、锁定）则 None。"""
    try:
        value = int(order_type)
    except (TypeError, ValueError):
        return None
    if value in _FUTURE_BUY_SIDE or value in _ETF_OPTION_BUY_SIDE:
        return SignalAction.BUY.value
    if value in _FUTURE_SELL_SIDE or value in _ETF_OPTION_SELL_SIDE:
        return SignalAction.SELL.value
    return None


def credit_action_of(order_type):
    """BUY / SELL for a credit order_type, or None if it is not one.

    直接还款 (32 / 45) moves cash rather than securities, so it has no side;
    callers must pass an action for it explicitly.
    """
    try:
        value = int(order_type)
    except (TypeError, ValueError):
        return None
    if value in _CREDIT_BUY_SIDE:
        return SignalAction.BUY.value
    if value in _CREDIT_SELL_SIDE:
        return SignalAction.SELL.value
    return None


def credit_optype_of(order_type):
    """passorder opType for a MiniQMT credit order_type, or None."""
    try:
        return CREDIT_OPTYPE_BY_ORDER_TYPE.get(int(order_type))
    except (TypeError, ValueError):
        return None


def _action_from_offset_flag(offset_flag, op_type=None, account_type=None):
    """BUY/SELL for a normalized query row, from m_nOffsetFlag or m_nOpType.

    For stocks, m_nOffsetFlag IS the side (48=buy, 49=sell). For futures and
    ETF options, offset means open/close instead (48=open, 49=close): a
    sell-to-open row carries offset=48 and reads as BUY under the stock
    mapping, and a buy-to-close row reads as SELL. When the row belongs to a
    passthrough account (FUTURE / STOCK_OPTION), prefer the side of m_nOpType
    via passthrough_action_of() (futures 0-15, ETF options 50-55); offset
    stays as the fallback for rows without a usable opType (e.g. 56/57
    exercise rows, which have no side at all).
    """
    if account_type and str(account_type).upper() in PASSTHROUGH_ACCOUNT_TYPES:
        action = passthrough_action_of(op_type)
        if action is not None:
            return action
    return SignalAction.BUY.value if int(offset_flag or 0) == 48 else SignalAction.SELL.value


# 报单时间。大 QMT 的 ORDER 行把日期和时间分成两个字段, MiniQMT 的
# XtOrder.order_time 是 Unix 秒, 所以要拼接后转换。成交那条路径早就读了
# m_strTradeTime, 委托这边一直漏掉 (issue #48)。
_ORDER_DATE_FIELDS = ("m_strInsertDate", "m_strOrderDate", "insert_date", "order_date")
_ORDER_TIME_FIELDS = ("m_strInsertTime", "m_strOrderTime", "insert_time", "order_time")

# 取不到时打印该行实际有哪些 m_*, 每进程一次。字段名无法离线核实,
# 猜一个然后静默返回 0 正是订单方向那个 bug 的成因。
_missing_order_time_reported = []


def _report_missing_order_time(row):
    if _missing_order_time_reported:
        return
    _missing_order_time_reported.append(True)
    try:
        available = sorted(n for n in dir(row) if n.startswith("m_"))
    except Exception:
        available = []
    print(
        "[bigqmt_order] order_time not found (tried %s / %s); ORDER row exposes: %s"
        % (", ".join(_ORDER_DATE_FIELDS), ", ".join(_ORDER_TIME_FIELDS),
           ", ".join(available) or "<none>")
    )


# 委托状态描述。官方字段表 (docs/BIGQMT_INNER_PYTHON_API_REFERENCE.md):
#   m_strCancelInfo  废单原因      <- 状态 57 时柜台的拒单理由在这里
#   m_strErrorMsg    状态信息
# 柜台消息形如 "[COUNTER] 资金可用余额不足，尚需[4789.630]"; 两个字段都空过,
# 客户端就只能看到一个没有原因的失败 (issue #60)。废单原因优先, 它更具体。
_STATUS_MSG_FIELDS = (
    "m_strCancelInfo",
    "m_strErrorMsg",
    "m_strStatusMsg",
    "status_msg",
    "error_msg",
)


def _status_message(row):
    for name in _STATUS_MSG_FIELDS:
        value = _attr(row, (name,))
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _order_time_seconds(row):
    """把 ORDER 行的报单日期+时间转成 Unix 秒, 拿不到返回 0。

    容忍几种实际会遇到的写法: 日期 '20260819' 或 '2026-08-19',
    时间 '093015'、'09:30:15' 或 '09:30:15.123'。已经是数字时间戳的直接用
    (毫秒会被归一到秒)。
    """
    raw_time = _attr(row, _ORDER_TIME_FIELDS)
    raw_date = _attr(row, _ORDER_DATE_FIELDS)
    if raw_time is None and raw_date is None:
        _report_missing_order_time(row)
        return 0

    # 已是数字: 当成时间戳 (>1e11 视为毫秒)。
    if isinstance(raw_time, (int, float)) and not isinstance(raw_time, bool):
        value = float(raw_time)
        if value > 1e11:
            value /= 1000.0
        if value > 1e8:      # 像时间戳而不是 093015 这种时分秒
            return int(value)

    date_text = "".join(ch for ch in str(raw_date or "") if ch.isdigit())
    time_text = "".join(ch for ch in str(raw_time or "") if ch.isdigit())
    if not date_text or len(date_text) < 8:
        return 0
    time_text = (time_text + "000000")[:6]   # 补齐到 HHMMSS, 丢掉毫秒
    try:
        import time as _time

        parsed = _time.strptime(date_text[:8] + time_text, "%Y%m%d%H%M%S")
        return int(_time.mktime(parsed))
    except Exception:
        return 0


def _price_type_value(value, default):
    if value is None or value == "":
        return int(default)
    try:
        return int(value)
    except (TypeError, ValueError):
        text = str(value).strip().upper()
        return int(PRICE_TYPE_ALIASES.get(text, default))


class BigQmtOrderGateway:
    def __init__(
        self,
        context_info,
        account_id="",
        passorder_func=None,
        cancel_func=None,
        get_trade_detail_data_func=None,
        account_type="STOCK",
        combo_type=1101,
        price_type=11,
        quick_trade=2,
    ):
        self.context_info = context_info
        self.account_id = account_id
        self.passorder = passorder_func
        self.cancel_func = cancel_func
        self.get_trade_detail_data = get_trade_detail_data_func
        self.account_type = account_type
        self.combo_type = combo_type
        self.price_type = price_type
        self.quick_trade = quick_trade

    def _resolve_account_type(self, account_id):
        """Per-request account_type: map lookup if configured, else default.

        When BIGQMT_ACCOUNT_TYPE_MAP is configured (multi-account deployment),
        the same gateway serves multiple accounts and must use the correct
        account_type for each request. When no map is configured (the common
        single-account case), this returns self.account_type unchanged.
        """
        return account_type_for(account_id, self.account_type)

    def _require_passorder(self):
        if self.passorder is None:
            raise RuntimeError("passorder is not available in Big QMT runtime")
        return self.passorder

    def _require_cancel(self):
        if self.cancel_func is None:
            raise RuntimeError("cancel is not available in Big QMT runtime")
        return self.cancel_func

    def _account_type_code(self, account_id=None):
        """The account type as MiniQMT reports it: an xtconstant int.

        xttype.XtOrder/XtTrade both carry account_type, and real MiniQMT fills
        it with SECURITY_ACCOUNT unconditionally. This deployment knows better
        -- it is configured with the type it actually trades as -- and #92
        showed what silence costs: a credit account read as STOCK returns an
        all-zero asset row with no error. So report the configured type and
        fall back to SECURITY_ACCOUNT only when there is nothing to report.

        When account_id is given and BIGQMT_ACCOUNT_TYPE_MAP is configured,
        the per-request type is used instead of self.account_type (same #92
        class of bug for multi-account deployments).
        """
        if account_id is not None and hasattr(self, "_resolve_account_type"):
            text = str(self._resolve_account_type(account_id) or "").strip().upper()
        else:
            text = str(self.account_type or "").strip().upper()
        if text in ACCOUNT_TYPE_CODES:
            return ACCOUNT_TYPE_CODES[text]
        try:
            # Already a code -- some configs set the number directly.
            return int(text)
        except (TypeError, ValueError):
            return getattr(_xtconstant, "SECURITY_ACCOUNT", 2)

    def _instrument_name(self, row, stock_code, cache):
        """证券名称 —— from the row if QMT put it there, else ContextInfo.

        Position rows carry m_strInstrumentName (position_bigqmt reads it), so
        order/deal rows plausibly do too; whether they actually do is a
        question about the terminal, not about us, hence the fallback. The
        cache is per query call: get_stock_name is an in-process ContextInfo
        call, but a day of orders can repeat the same code many times.
        """
        name = str(_attr(row, ("m_strInstrumentName", "instrument_name",
                               "stock_name"), "") or "")
        if name:
            return name
        if stock_code in cache:
            return cache[stock_code]
        resolved = ""
        getter = getattr(self.context_info, "get_stock_name", None)
        if getter is not None:
            try:
                resolved = str(getter(stock_code) or "")
            except Exception:
                resolved = ""
        cache[stock_code] = resolved
        return resolved

    def _require_query_func(self):
        if self.get_trade_detail_data is None:
            raise RuntimeError("get_trade_detail_data is not available in Big QMT runtime")
        return self.get_trade_detail_data

    @staticmethod
    def build_user_order_id(signal_id):
        text = str(signal_id or "")
        digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]
        return "bq:%s:%s" % (digest, text[:30])

    def submit(self, request):
        passorder = self._require_passorder()
        action = str(request.action).upper()
        raw_order_type = getattr(request, "order_type", None)
        credit_optype = credit_optype_of(raw_order_type)
        passthrough_optype = passthrough_optype_of(raw_order_type)
        if credit_optype is not None:
            # A credit operation carries more than a side: mapping it back to
            # BUY/SELL would turn 融资买入 into an ordinary buy, which is the
            # bug behind issue #103 -- worse than the rejection it replaced,
            # because it places a real but different order.
            op_type = credit_optype
        elif passthrough_optype is not None:
            # Futures/option opTypes carry open-vs-close and today-vs-yesterday.
            # Forward them untouched -- but only on an account that can trade
            # them. Letting a futures opType fall through to 23/24 on a STOCK
            # account is the #103 failure mode again: 平今多 (a close) would go
            # out as an ordinary stock buy.
            account_type = str(self._resolve_account_type(request.account_id or self.account_id) or "").upper()
            if account_type not in PASSTHROUGH_ACCOUNT_TYPES:
                raise ValueError(
                    # ASCII only: this goes to QMT's own log, which drops
                    # non-ASCII (a Chinese install path came back mangled).
                    "order_type %s is a futures/option opType but account_type "
                    "is %r. Set account_type to one of %s, or use 23/24 for "
                    "stock orders." % (
                        passthrough_optype, account_type,
                        "/".join(sorted(PASSTHROUGH_ACCOUNT_TYPES))))
            op_type = passthrough_optype
        elif action == SignalAction.BUY.value:
            op_type = 23
        elif action == SignalAction.SELL.value:
            op_type = 24
        else:
            raise ValueError("unsupported order action: %s" % request.action)

        user_order_id = str(request.remark or "").strip() or self.build_user_order_id(request.signal_id)
        account_id = request.account_id or self.account_id
        passorder(
            op_type,
            self.combo_type,
            account_id,
            normalize_stock_code(request.stock_code),
            _price_type_value(request.price_type, self.price_type),
            float(request.price),
            int(request.volume),
            request.strategy_name,
            self.quick_trade,
            user_order_id,
            self.context_info,
        )
        return OrderSubmitResult(
            status="SUBMITTED",
            user_order_id=user_order_id,
            order_sys_id=None,
            message="passorder submitted",
        )

    def passorder_passthrough(
        self,
        op_type,
        order_type,
        account_id,
        order_code,
        price_type,
        price,
        volume,
        strategy_name,
        quick_trade,
        user_order_id,
        dry_run=False,
    ):
        """Call QMT's native passorder with the 11-arg signature untouched.

        The deliberate escape hatch (route 2): the caller controls every
        argument, including orderType (股/手 vs 金额 vs 比例 vs 组合) and
        quickTrade, which submit()/order_stock do not expose. NONE of submit()'s
        safety nets run here -- no opType validation (a credit or futures opType
        that submit() would guard becomes the caller's responsibility, see
        #103), no code case-normalization (#95), no settlement read-back. The
        caller asked for the native API, so they own its contract.

        ContextInfo is supplied here, not by the caller: passorder reads
        internals off the raw QMT ContextInfo (e.g. .request_id) and it only
        exists inside QMT, so it can never travel over RPC.

        ``dry_run`` resolves everything and returns the exact 11-tuple that
        WOULD be sent, without calling passorder. It is how this path is
        verified against the live terminal without placing an order, and it
        lets a caller confirm their own argument mapping the same way.

        passorder itself is async and returns None (QMT assigns the 合同编号
        later, on the order_callback push), so there is no order id to return
        synchronously -- exactly as with order_stock_async.
        """
        passorder = self._require_passorder()
        args = [
            int(op_type),
            int(order_type),
            str(account_id or self.account_id),
            str(order_code),                 # RAW: no normalization (#95)
            int(price_type),
            float(price),
            int(volume),
            str(strategy_name or ""),
            int(quick_trade),
            str(user_order_id or ""),
        ]
        if dry_run:
            return {
                "dry_run": True,
                "would_call": "passorder",
                "args": list(args),
                "context_info_supplied": self.context_info is not None,
            }
        passorder(*(args + [self.context_info]))
        return {
            "dry_run": False,
            "submitted": True,
            "user_order_id": args[9],
        }

    def cancel(self, order_ref, account_id=None):
        cancel_func = self._require_cancel()
        aid = account_id or self.account_id
        account_type = self._resolve_account_type(aid)
        ok = cancel_func(order_ref.order_sys_id, aid, account_type, self.context_info)
        return CancelResult(success=bool(ok), message="" if ok else "cancel returned false")

    def query_orders(self, account_id, strategy_name):
        return self.query_orders_strict(account_id, strategy_name)

    def query_orders_strict(self, account_id, strategy_name):
        query = self._require_query_func()
        account_type = self._resolve_account_type(account_id)
        rows = query(account_id, account_type, "ORDER", strategy_name) or []
        result = []
        account_type_code = self._account_type_code(account_id)
        name_cache = {}
        for row in rows:
            try:
                stock_code = _full_code(
                    _attr(row, ("m_strInstrumentID", "instrument_id", "stock_code")),
                    _attr(row, ("m_strExchangeID", "exchange_id", "market")),
                )
            except Exception as exc:
                skip_unparsable_row("ORDER", row, exc)
                continue
            result.append(
                OrderSnapshot(
                    order_sys_id=str(_attr(row, ("m_strOrderSysID", "order_sys_id"), "") or ""),
                    user_order_id=str(_attr(row, ("m_strRemark", "user_order_id", "remark"), "") or ""),
                    stock_code=stock_code,
                    action=_action_from_offset_flag(
                        _attr(row, ("m_nOffsetFlag", "offset_flag"), 0),
                        _attr(row, ("m_nOpType", "op_type"), None),
                        self._resolve_account_type(account_id),
                    ),
                    volume=int(_attr(row, ("m_nVolumeTotalOriginal", "volume"), 0) or 0),
                    traded_volume=int(_attr(row, ("m_nVolumeTraded", "traded_volume"), 0) or 0),
                    status=str(_attr(row, ("m_nOrderStatus", "status"), "") or ""),
                    # The trade builder has this fallback and orders did not:
                    # a query filtered by strategy_name returned rows that all
                    # passed the filter yet reported strategy_name="" (issue
                    # #156 follow-up). When a filter is given, every row
                    # belongs to it by construction.
                    #
                    # The row's own 报单来源 now counts as "the row said so"
                    # (issue #174), so an unfiltered query names bridge-placed
                    # orders instead of leaning on the identity store.
                    strategy_name=_row_strategy_name(row) or strategy_name or "",
                    price=float(_attr(row, ("m_dLimitPrice", "m_dPrice", "price"), 0.0) or 0.0),
                    remark=str(_attr(row, ("m_strRemark", "remark"), "") or ""),
                    order_time=_order_time_seconds(row),
                    status_msg=_status_message(row),
                    price_type=_attr(row, ("m_nOrderPriceType", "price_type")),
                    traded_price=float(
                        _attr(row, ("m_dTradedPrice", "traded_price", "avg_traded_price"), 0.0) or 0.0
                    ),
                    # 柜台自己给的成交金额 (issue #173). ccxt 的 order["cost"]
                    # 要的就是它。以前只有 DEAL 行透出 amount, 所以拿委托的
                    # cost 得按 order_sysid 聚合成交 (多一次 RPC), 或者自己
                    # 拿 traded_price * traded_volume 去算。
                    #
                    # 实盘验过 (0.3.19, 当日 14 笔委托): trade_amount 与按
                    # order_sysid 聚合的 DEAL 金额 14/14 逐笔相等。
                    #
                    # 注意 issue 里"分笔成交时成交均价舍入会差几分"这条,
                    # 在这台终端上**没有复现**: m_dTradedPrice 不是两位小数,
                    # 它带完整精度 (唯一一笔分价成交 55.14/55.13 报的是
                    # 55.13666666666666), 所以估算值当天一分不差。取这个
                    # 字段的理由是它是柜台的原值 -- 不依赖某台终端的
                    # m_dTradedPrice 精度, 也不用多发一次 RPC。
                    trade_amount=float(
                        _attr(row, ("m_dTradeAmount", "trade_amount", "amount"), 0.0) or 0.0
                    ),
                    # MiniQMT XtOrder carries these and this bridge never sent
                    # them, so every client saw AttributeError (issue #133).
                    account_type=account_type_code,
                    instrument_name=self._instrument_name(row, stock_code, name_cache),
                    # 股东代码. MiniQMT's XtOrder has it; the ORDER rows this
                    # terminal returns do not -- none of their 120 attributes
                    # is a shareholder id. Kept so the field exists (a caller
                    # reading it gets "" rather than AttributeError) and so a
                    # broker whose QMT does supply it is picked up.
                    secu_account=str(_attr(row, ("m_strShareholderID", "m_strSecuAccount",
                                                 "secu_account"), "") or ""),
                    offset_flag=_attr(row, ("m_nOffsetFlag", "offset_flag")),
                    direction=_attr(row, ("m_nDirection", "direction")),
                )
            )
        return result

    def query_trades(self, account_id, strategy_name):
        return self.query_trades_strict(account_id, strategy_name)

    def query_trades_strict(self, account_id, strategy_name):
        query = self._require_query_func()
        account_type = self._resolve_account_type(account_id)
        # DEAL is the documented trade detail type; an empty result is a success.
        if str(strategy_name or "").strip():
            rows = query(account_id, account_type, "DEAL", strategy_name) or []
        else:
            rows = query(account_id, account_type, "DEAL") or []
        result = []
        account_type_code = self._account_type_code(account_id)
        name_cache = {}
        for row in rows:
            traded_at_raw = _attr(row, ("m_strTradeTime", "trade_time", "traded_at"), "")
            try:
                stock_code = _full_code(
                    _attr(row, ("m_strInstrumentID", "instrument_id", "stock_code")),
                    _attr(row, ("m_strExchangeID", "exchange_id", "market")),
                )
            except Exception as exc:
                skip_unparsable_row("DEAL", row, exc)
                continue
            result.append(
                TradeSnapshot(
                    trade_id=str(_attr(row, ("m_strTradeID", "trade_id"), "") or ""),
                    order_sys_id=str(_attr(row, ("m_strOrderSysID", "order_sys_id"), "") or ""),
                    stock_code=stock_code,
                    action=_action_from_offset_flag(
                        _attr(row, ("m_nOffsetFlag", "offset_flag"), 0),
                        _attr(row, ("m_nOpType", "op_type"), None),
                        self._resolve_account_type(account_id),
                    ),
                    volume=int(_attr(row, ("m_nVolume", "volume"), 0) or 0),
                    price=float(_attr(row, ("m_dPrice", "m_dTradePrice", "price"), 0.0) or 0.0),
                    traded_at=str(traded_at_raw or ""),
                    user_order_id=str(_attr(row, ("m_strRemark", "user_order_id", "remark"), "") or ""),
                    # 官方 Deal 字段: m_dTradeAmount 成交额; m_strTradeDate+
                    # m_strTradeTime 合成 Unix 秒; 策略名来自查询过滤参数。
                    amount=float(_attr(row, ("m_dTradeAmount", "amount"), 0.0) or 0.0),
                    # The row's own strategy name first. m_strStrategyName is
                    # genuinely absent here -- but the name is not: it comes
                    # back in 报单来源 (m_strSource), which is passorder's
                    # strategyName argument (issue #174, and #154 measured it).
                    # Reading only m_strStrategyName is why this field answered
                    # "" for everything (issue #133).
                    #
                    # The filter is the fallback: when one IS given every row
                    # belongs to it by construction. For orders this bridge
                    # submitted with no filter, the RPC layer still backstops
                    # from the identity store.
                    strategy_name=_row_strategy_name(row) or strategy_name or "",
                    traded_time=date_time_seconds(
                        _attr(row, ("m_strTradeDate", "trade_date", "m_strDealDate")),
                        traded_at_raw,
                    ),
                    account_type=account_type_code,
                    instrument_name=self._instrument_name(row, stock_code, name_cache),
                    secu_account=str(_attr(row, ("m_strShareholderID", "m_strSecuAccount",
                                                 "secu_account"), "") or ""),
                    # XtTrade.commission 手续费. A live DEAL row carries BOTH
                    # m_dComssion (QMT's own misspelling) and m_dCommission, so
                    # first-non-None would stop at whichever comes first even
                    # when it is the 0.0 one. Take the first non-zero instead.
                    commission=_first_nonzero(
                        row, ("m_dComssion", "m_dCommission", "commission")),
                    offset_flag=_attr(row, ("m_nOffsetFlag", "offset_flag")),
                    direction=_attr(row, ("m_nDirection", "direction")),
                )
            )
        return result

    @staticmethod
    def _field_shape(value):
        """A field's shape with its identity removed.

        Issue #154 asked what 报单来源 (m_strSource) actually holds -- the
        reporter's screenshot showed something that looks like a MAC address
        and a device GUID, and they wanted it gone. Answering "is any of that
        ours?" needs the value, and the value is exactly the thing nobody
        should put on a wire.

        So report the shape instead: length, the size of each ``|`` segment,
        and a character-class mask (digits to #, letters to a, separators
        kept). The mask is lossy by construction, which is the point -- it
        cannot carry an identifier back, and it is still enough to tell
        ``a#-#a-##-...`` apart from ``aaaaaa_aaa``, which is the whole
        question.
        """
        text = "" if value is None else str(value)
        masked = []
        for ch in text[:_SHAPE_MASK_MAX]:
            if ch.isdigit():
                masked.append("#")
            elif ch.isalpha():
                masked.append("a")
            else:
                masked.append(ch)          # separators carry the structure
        return {
            "length": len(text),
            "empty": not text,
            "segments": [len(part) for part in text.split("|")] if text else [],
            "mask": "".join(masked),
        }

    def describe_detail_fields(self, account_id, detail_types=None,
                               shape_fields=None):
        """Which attributes QMT's own ORDER / DEAL rows actually carry.

        Names only, never values. Three issues so far (#113, #130, #133) have
        been "field X is missing", and each one cost a deploy-and-restart cycle
        to answer, because nothing outside QMT can see what
        get_trade_detail_data hands back. A row carries prices, volumes and
        counter ids, and this travels the same channel as any other RPC, so
        the values stay here.

        ``shape_fields`` names attributes to report the *shape* of as well --
        see _field_shape. Still not values: a mask cannot be read back into an
        identifier, so this keeps the promise above while answering "what kind
        of thing is in this field" (issue #154).
        """
        query = self._require_query_func()
        wanted_shapes = [str(name) for name in (shape_fields or []) if str(name or "").strip()]
        described = {}
        for detail_type in (detail_types or ("ORDER", "DEAL")):
            entry = {"rows": 0, "attributes": [], "error": ""}
            try:
                rows = query(account_id, self._resolve_account_type(account_id), str(detail_type)) or []
                entry["rows"] = len(rows)
                if rows:
                    entry["attributes"] = _data_attribute_names(rows[0])
                    if wanted_shapes:
                        shapes = {}
                        for name in wanted_shapes:
                            distinct = {}
                            for row in rows:
                                shape = self._field_shape(_attr(row, (name,), ""))
                                key = shape["mask"]
                                if key not in distinct:
                                    shape["rows"] = 0
                                    distinct[key] = shape
                                distinct[key]["rows"] += 1
                            shapes[name] = sorted(
                                distinct.values(), key=lambda s: -s["rows"])
                        entry["shapes"] = shapes
            except Exception as exc:
                entry["error"] = "%s: %s" % (type(exc).__name__, exc)
            described[str(detail_type)] = entry
        return described

    def query_submission_identities_strict(self, account_id, strategy_name):
        orders = self.query_orders_strict(account_id, strategy_name)
        trades = self.query_trades_strict(account_id, strategy_name)
        return orders, trades
