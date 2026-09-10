"""Redis Pub/Sub RPC for the Big QMT runtime.

By default the service can process selected requests directly in the Redis
listener thread. The in-memory queue and ``drain_pending`` are kept as a
runtime fallback for environments where a QMT API must run from a strategy
callback thread.
"""

import base64
import collections
import datetime as _dt
import json
import math
import queue
import threading
import time
import traceback
import uuid

from .adapters.redis_common import decode_text
from .code_utils import normalize_stock_code
from .models import AccountSnapshot, OrderRef, OrderRequest


# time.monotonic: unaffected by wall-clock jumps, so a settle deadline
# survives an NTP correction mid-session. Python 3.3+, fine on QMT's 3.6.
_monotonic = time.monotonic


RPC_REVISION = "20260715-execution-snapshot-v1"


READ_METHODS = {
    "ping",
    "get_deployment_info",
    "probe_capabilities",
    "probe_order_identity",
    "get_ticks",
    "get_instrument",
    "get_instrument_type",
    "get_market_data",
    "get_market_data_ex",
    "get_local_data",
    "get_stock_list_in_sector",
    "get_sector_list",
    "get_sector_info",
    "get_markets",
    "get_market_last_trade_date",
    "get_divid_factors",
    "download_history_data",
    "download_history_data2",
    "get_trading_dates",
    "get_holidays",
    "download_holiday_data",
    "download_his_st_data",
    "get_ipo_info",
    "get_etf_info",
    "download_etf_info",
    "get_option_list",
    "get_his_option_list",
    "get_his_option_list_batch",
    "get_financial_data",
    "download_financial_data",
    "download_financial_data2",
    "call_formula",
    "subscribe_formula",
    "unsubscribe_formula",
    "get_formula_result",
    "gen_factor_index",
    "get_positions",
    "get_position_statistics",
    "get_asset",
    "query_orders",
    "query_trades",
    "query_execution_snapshot",
    "describe_trade_detail_fields",
    "reload_deployment",
    "reload_status",
    "query_stock_position",
    "sync_positions",
    "submit_download_history_data",
    "submit_download_history_data2",
    "get_download_status",
    "wait_download",
    # 账户 / 融资融券 / 交易扩展查询（官方全局函数 + detail types）
    "query_account_infos",
    "query_account_status",
    "query_credit_detail",
    "query_credit_account",
    "query_stk_compacts",
    "query_credit_subjects",
    "query_credit_slo_code",
    "query_credit_assure",
    "query_appointment_info",
    "query_smt_secu_info",
    "query_smt_secu_rate",
    "smt_appointment",
    # 官方交易查询函数（直接暴露，运行时注入的全局函数）
    "get_value_by_order_id",
    "get_last_order_id",
    "get_ipo_data",
    "get_new_purchase_limit",
    "quote_subscription_status",
    "get_history_trade_detail_data",
    "get_assure_contract",
    "get_enable_short_contract",
    "get_unclosed_compacts",
    "get_closed_compacts",
    "get_debt_contract",
    "get_option_subject_position",
    "get_comb_option",
    "get_hkt_exchange_rate",
}

ORDER_METHODS = {
    "submit_order",
    "submit_orders_batch",
    "cancel_order",
    "cancel_orders_batch",
    # Raw native-passorder passthrough (route 2). Gated behind
    # allow_order_methods like every other write, and deferred to the adjust
    # thread by virtue of not being in READ_METHODS.
    "passorder",
}

# Whole-quote push subscription control methods. These drive a server-side
# QuoteSubscriptionManager (reference-counted ContextInfo.subscribe_whole_quote)
# rather than a market_data read; the data itself flows over the push channel.
QUOTE_SUBSCRIPTION_METHODS = {
    "subscribe_whole_quote",
    "unsubscribe_whole_quote",
    "quote_keepalive",
    "quote_unsubscribe_all",
}

LISTENER_DEFERRED_METHODS = {
    "sync_positions",
    # Trade-context queries route through QMT's get_trade_detail_data, which
    # returns EMPTY when called from the background RPC thread (it needs the main
    # strategy thread's context). Defer them so the adjust drain runs them on the
    # main thread -- costs up to one adjust interval (~500ms) but returns real
    # data. Asset queries use the same QMT detail API and must follow this rule.
    "get_asset",
    "get_positions",
    "get_position_statistics",
    "query_stock_position",
    "query_orders",
    "query_trades",
    "query_execution_snapshot",
    "describe_trade_detail_fields",
    "reload_deployment",
    "query_account_infos",
    "query_account_status",
    "query_credit_detail",
    "query_credit_account",
    "query_stk_compacts",
    "query_credit_subjects",
    "query_credit_slo_code",
    "query_credit_assure",
    "query_appointment_info",
    "get_ipo_data",   # 8-28: 交易类查询, 需主线程上下文 (后台线程返回空)
    # get_ipo_data 上面那条注释适用于所有走 QMT 交易类全局函数的查询，当时只
    # 修了它一个。真两融账户的体检报告把漏网的挑了出来：同一次运行里
    # query_stk_compacts -> 52 行、query_credit_subjects -> 71002 行、
    # query_credit_slo_code -> 50 行，而调同一个 QMT 函数的直连 get_* 全是 0 行。
    # 两边 handler 逐字节相同、账号相同，唯一差别就是 query_* 在这张表里、
    # 直连的不在，于是后者跑在后台 listener 线程上 —— 正是 get_ipo_data 当年
    # 踩的那个坑。孪生方法必须待在同一条线程路径上（#204）。
    "get_assure_contract",
    "get_enable_short_contract",
    "get_unclosed_compacts",
    "get_closed_compacts",
    "get_debt_contract",
    "get_option_subject_position",
    "get_comb_option",
    "get_new_purchase_limit",
    "get_hkt_exchange_rate",
    "query_smt_secu_info",
    "query_smt_secu_rate",
    "get_value_by_order_id",
    "get_last_order_id",
    "get_history_trade_detail_data",
}

# Trade-context queries route through QMT's get_trade_detail_data, which
# returns EMPTY when called from the background RPC thread (it needs the main
# strategy thread's context). Defer them so the adjust drain runs them on the
# main thread -- costs up to one adjust interval (~500ms) but returns real
# data. Asset queries use the same QMT detail API and must follow this rule.
#
# NOTE: do NOT blanket-defer all READ_METHODS here. Market-data reads
# (get_full_tick, get_market_data, ...) are thread-safe in the embedded
# terminal and must stay inline for low latency; the ZMQ transport has no
# adjust-driven drain for pending requests (its drain_request_queue is a
# no-op when the router thread exists), so deferring everything would stall
# them forever. Only the trade-context methods listed above go through drain.


METHOD_ALIASES = {
    "get_full_tick": "get_ticks",
    "get_instrument_detail": "get_instrument",
    "get_instrumentdetail": "get_instrument",
    "getDividFactors": "get_divid_factors",
    "query_stock_asset": "get_asset",
    "query_stock_positions": "get_positions",
    "query_position_statistics": "get_position_statistics",
    "query_stock_orders": "query_orders",
    "query_stock_trades": "query_trades",
    "order_stock": "submit_order",
    "order_stock_async": "submit_order",
    "order_stock_batch": "submit_orders_batch",
    "cancel_order_stock": "cancel_order",
    "cancel_order_stock_sysid": "cancel_order",
    "cancel_order_stock_batch": "cancel_orders_batch",
}

BUY_ORDER_TYPES = {"23", "STOCK_BUY", "BUY", "B"}
SELL_ORDER_TYPES = {"24", "STOCK_SELL", "SELL", "S"}
CANCELABLE_ORDER_STATUSES = {"50", "55"}
CANCELED_ORDER_STATUSES = {"53", "54"}

# Default 投资备注 / strategy name on an order the caller did not name.
# QMT shows it in the 委托 list's 报单来源 column (issue #154), so it is
# visible to anyone reading that screen. Override per call with
# strategy_name=, or once with rpc_default_strategy_name in the config;
# "" leaves the column blank the way a hand-placed order does.
DEFAULT_ORDER_STRATEGY_NAME = "bigqmt_rpc"
# query_credit_account 是查柜台的，官方参考 6.13 原话：「本函数只能有一个查询，
# 如果前面的查询正在进行中，后面的查询将会提前返回。本函数从服务器查询数据，
# 建议平均查询时间间隔 30s 一次，不可频繁调用。」所以桥这边自己限流，客户端
# 想怎么轮询都不会把柜台打爆（#202）。
# hollow 告警的最小间隔。跑错线程时每一次查询都会 hollow，不节流日志会被刷爆。
HOLLOW_WARNING_INTERVAL_SECONDS = 60.0
CREDIT_ACCOUNT_MIN_INTERVAL_SECONDS = 30.0
# 缓存超过这个年龄就不再交出去。
#
# 这条通道的缓存**从不自己刷新** —— 没人调就永远不刷，age_seconds 可以是 44
# 秒，也可以是三小时。真正的风险不是「30 秒 vs 0 秒」，而是一个只读 rows、
# 不看 age_seconds 的调用方，拿三小时前的**维持担保比例**去做决策还毫无察觉。
# 维持担保比例是强平线，读错方向是真损失。
#
# 120 秒：官方最小间隔是 30 秒，留够几次调用的余量，又不至于让陈数据在盘中
# 蒙混过关。要实时的维持担保比例请走 query_credit_detail（同步、读终端本地
# 缓存、无此问题）。传 0 表示不限制，那是显式放弃这层保护。
CREDIT_ACCOUNT_MAX_AGE_SECONDS = 120.0
# 一次 RPC 里最多等回调多久。**默认 0：不等。**
#
# handler 跑在 adjust 主线程上（它在 LISTENER_DEFERRED_METHODS 里，必须如此，
# 否则拿不到交易上下文）。原来这里 sleep 轮询等回调，实盘上（真两融账户、
# 已确认在大 QMT 里回调能触发）表现是：query_issued=true、等满 8 秒、
# callbacks_seen=0、无任何报错。
#
# 最可能的解释：QMT 把 credit_account_callback 投递到主线程上，而我正用等待
# 堵着这条线程 —— 用等待堵死了唯一能送达的那条路，回调只可能在 handler 返回
# 之后才进来。order_callback 走 C++ 线程，这个不一定同理，不能照搬。
#
# 所以默认改成「发出去就返回」：这一次拿到的是缓存（或空），回调随后自己落下，
# 下一次调用就能取到。在两种假设下都安全 —— 如果回调其实走的是 C++ 线程，
# 代价也只是多一次调用。想等的人显式传 wait_seconds 即可。
CREDIT_ACCOUNT_DEFAULT_WAIT_SECONDS = 0.0
CREDIT_ACCOUNT_MAX_WAIT_SECONDS = 10.0
TERMINAL_NON_CANCEL_ORDER_STATUSES = {"56", "57"}
# 51 已报待撤 / 52 部成待撤: the exchange has ACCEPTED the cancel and it is
# on its way. Neither cancelled nor failed -- keep waiting, and at the
# deadline report the acceptance instead of a "still status 51" failure
# (issue #151; the narrow-window twin of the #148 false negative).
CANCEL_IN_FLIGHT_STATUSES = {"51", "52"}
SAFE_B64_PREFIX = "b64s:"
SAFE_B64_DIGIT_ENCODE = str.maketrans("0123456789", "!#$%&()*~?")
SAFE_B64_DIGIT_DECODE = str.maketrans("!#$%&()*~?", "0123456789")
MARKET_DATA_METHODS = {
    "get_instrument_type",
    "get_market_data",
    "get_market_data_ex",
    "get_local_data",
    "get_stock_list_in_sector",
    "get_sector_list",
    "get_sector_info",
    "get_markets",
    "get_market_last_trade_date",
    "get_divid_factors",
    "download_history_data",
    "download_history_data2",
    "get_trading_dates",
    "get_holidays",
    "download_holiday_data",
    "download_his_st_data",
    "get_ipo_info",
    "get_etf_info",
    "download_etf_info",
    "get_option_list",
    "get_his_option_list",
    "get_his_option_list_batch",
    "get_financial_data",
    "download_financial_data",
    "download_financial_data2",
    "call_formula",
    "subscribe_formula",
    "unsubscribe_formula",
    "get_formula_result",
    "gen_factor_index",
    # 龙虎榜 / 股东 / 换手率 / 行业 / 收盘价
    "get_longhubang",
    "get_top10_share_holder",
    "get_holder_num",
    "get_turnover_rate",
    "get_industry",
    "get_close_price",
    # 期权定价 / 隐含波动率
    "bsm_price",
    "bsm_iv",
    "get_option_iv",
    "get_option_detail_data",
    "get_option_undl_data",
    "get_option_undl",
    # 财务扩展 / 因子
    "get_raw_financial_data",
    "get_factor_data",
    # 历史 ST / 指数权重
    "get_his_st_data",
    "get_his_index_data",
    # 期货 / 合约
    "get_main_contract",
    "get_his_contract_list",
    "get_date_location",
    "get_ETF_list",
    # 北向资金 / 港股通
    "get_north_finance_change",
    "get_hkt_statistics",
    "get_hkt_details",
    # 自定义板块（写）。issue #143：这一族以前只有 create_sector，而它在大 QMT
    # 上是静默空操作；其余几个在白名单里有名字却没实现，调用报 not implemented。
    "create_sector",
    "create_sector_folder",
    "add_stock_to_sector",
    "remove_stock_from_sector",
    "reset_sector_stock_list",
    # 基础查询辅助
    "get_stock_name",
    "get_stock_type",
    "get_last_close",
    "get_last_volume",
    "get_open_date",
    "get_contract_expire_date",
    "get_contract_multiplier",
    "get_float_caps",
    "get_total_share",
    "get_turn_over_rate",
    "get_weight_in_index",
    "get_svol",
    "get_bvol",
    "get_risk_free_rate",
    # L2 行情（需 L2 权限 + 原生 xtdata SDK 行情服务）
    "get_l2_quote",
    "get_l2_order",
    "get_l2_transaction",
    "subscribe_l2thousand",
    # 指数权重 / 交易日历 / 交易时段 / 可转债 / 品种判断
    "get_index_weight",
    "get_trading_calendar",
    "get_trade_times",
    "get_cb_info",
    "is_stock_type",
    # 板块增删
    "add_sector",
    "remove_sector",
    # 数据下载扩展
    "download_cb_data",
    "download_history_contracts",
    "download_index_weight",
    "download_sector_data",
    # 时间戳转换（纯计算，服务端本地）
    "datetime_to_timetag",
    "timetag_to_datetime",
}

# Keep READ_METHODS in sync with MARKET_DATA_METHODS: every market-data method
# forwarded to the adapter is also callable over RPC. (create_sector is a write
# op — creates/updates a custom sector — but it is harmless to expose; trading
# order writes stay gated behind ORDER_METHODS + allow_order_methods.)
READ_METHODS |= MARKET_DATA_METHODS
READ_METHODS |= QUOTE_SUBSCRIPTION_METHODS


def _maybe_scalar(value):
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return item()
        except Exception:
            return value
    return value


def _is_redis_timeout(exc):
    name = exc.__class__.__name__.lower()
    module = getattr(exc.__class__, "__module__", "")
    text = str(exc).lower()
    return ("redis" in module and "timeout" in name) or "timeout reading from socket" in text


def to_jsonable(value):
    # Fast path for the shapes that dominate a market payload. A whole-market
    # snapshot is 1.9M nodes, and every one of them used to walk the full
    # type-probing chain below -- starting with a getattr(value, "item") that
    # misses on every plain float. Measured on 51285 instruments: 628.7ms ->
    # 328.0ms, byte-identical output.
    #
    # Checked by exact type, not isinstance, on purpose: numpy's float64 is a
    # subclass of float, so identity checks let it fall through to the full
    # path where _maybe_scalar still unwraps it. Being fast here must not
    # change what anything serialises to.
    kind = type(value)
    if kind is float:
        return None if (math.isnan(value) or math.isinf(value)) else value
    if kind is int or kind is str or kind is bool or value is None:
        return value
    if kind is dict:
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if kind is list:
        return [to_jsonable(item) for item in value]

    value = _maybe_scalar(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return None
        return value
    if isinstance(value, (_dt.datetime, _dt.date)):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if hasattr(value, "isoformat") and value.__class__.__module__.startswith("pandas"):
        try:
            return value.isoformat()
        except Exception:
            return str(value)
    if hasattr(value, "to_dict") and hasattr(value, "columns") and hasattr(value, "index"):
        try:
            frame = value.reset_index()
            return {
                "__bigqmt_type__": "DataFrame",
                "columns": [str(col) for col in frame.columns],
                "records": to_jsonable(frame.to_dict("records")),
            }
        except Exception:
            return str(value)
    if hasattr(value, "to_dict") and hasattr(value, "index") and not isinstance(value, dict):
        try:
            return {
                "__bigqmt_type__": "Series",
                "data": to_jsonable(value.to_dict()),
            }
        except Exception:
            return str(value)
    # pandas Panel (3-D). QMT ships pandas 0.22, where get_financial_data with
    # several stocks AND several dates still returns one. A Panel has no
    # .columns and no .index, so it falls past the DataFrame and Series
    # branches all the way to the __dict__ fallback -- and vars(panel) is
    # {'_data': ..., 'is_copy': None}, which the underscore filter reduces to
    # {'is_copy': None}. That is exactly what callers got back (issue #115):
    # not an error, just the one public attribute the object happened to have.
    #
    # pandas removed Panel in 1.0, so the client cannot rebuild one even if we
    # sent the axes. Send a DataFrame per item instead; it arrives as a dict.
    if (hasattr(value, "major_axis") and hasattr(value, "minor_axis")
            and hasattr(value, "items") and not isinstance(value, dict)):
        try:
            labels = [str(item) for item in value.items]
            return {
                "__bigqmt_type__": "Panel",
                "items": labels,
                "major_axis": [str(item) for item in value.major_axis],
                "minor_axis": [str(item) for item in value.minor_axis],
                "data": {str(item): to_jsonable(value[item]) for item in value.items},
            }
        except Exception:
            return str(value)
    if hasattr(value, "tolist") and not isinstance(value, (str, bytes, bytearray)):
        try:
            return to_jsonable(value.tolist())
        except Exception:
            pass
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(item) for item in value]
    enum_value = getattr(value, "value", None)
    if isinstance(enum_value, (str, int, float, bool)):
        return enum_value
    if hasattr(value, "__dict__"):
        return {
            key: to_jsonable(item)
            for key, item in vars(value).items()
            if not key.startswith("_")
        }
    return str(value)


class OrderSettlement(object):
    """One order awaiting its order_sys_id.

    Why this cannot simply run on a background thread: get_trade_detail_data
    returns EMPTY off the main strategy thread (see LISTENER_DEFERRED_METHODS),
    so a background poll would find nothing and report every order as silently
    rejected. The retries have to happen on the adjust thread -- just without
    holding it.

    server_error is carried here rather than on handlers._last_server_error:
    that slot belongs to whichever request is in flight, and settling writes to
    it long after this request left the handler (issue #43).
    """

    __slots__ = ("order_request", "result", "deadline", "attempts", "server_error",
                 "request", "response")

    def __init__(self, order_request, result, deadline):
        self.order_request = order_request
        self.result = result
        self.deadline = deadline
        self.attempts = 0
        self.server_error = ""
        self.request = None
        self.response = None


class CancelSettlement(object):
    """One native cancel result awaiting the order's actual status.

    Full QMT's injected ``cancel`` return is not reliable across terminal
    builds -- in either direction.  On Guojin 2.1.19.0 it returned falsey
    even though the broker acknowledged the request and the order moved to
    status 54 within 67 ms (issue #148), and truthy for orders that do not
    exist at all (issue #151).  Like order-id settlement, status readback
    must stay on the adjust thread because get_trade_detail_data is empty on
    worker threads.
    """

    __slots__ = ("order_ref", "account_id", "result", "deadline", "attempts",
                 "request", "response")

    def __init__(self, order_ref, account_id, result, deadline):
        self.order_ref = order_ref
        self.account_id = account_id
        self.result = result
        self.deadline = deadline
        self.attempts = 0
        self.request = None
        self.response = None


class BigQmtRpcHandlers:
    """Whitelisted RPC method handlers backed by replaceable adapters."""

    def __init__(
        self,
        account_id,
        market_data,
        position_provider,
        order_gateway=None,
        position_sync_sink=None,
        allow_order_methods=False,
        allowed_methods=None,
        qmt_api=None,
        settle_orders_inline=False,
        order_settle_timeout_seconds=3.0,
        quote_subscription_manager=None,
        default_strategy_name=None,
    ):
        # What an order carries when the caller names no strategy. It is not
        # internal: QMT puts it in the 委托 list's 报单来源 column, so every
        # order announces "bigqmt_rpc" to anyone reading that screen, and a
        # reporter asked to be rid of it (issue #154). Per-call
        # strategy_name= always won; there was just no way to set the default
        # once. Empty string is a real answer here -- it leaves the column
        # blank, the way a hand-placed order does.
        self.default_strategy_name = (
            DEFAULT_ORDER_STRATEGY_NAME if default_strategy_name is None
            else str(default_strategy_name))
        self.account_id = str(account_id or "")
        self.market_data = market_data
        self.position_provider = position_provider
        self.order_gateway = order_gateway
        self.position_sync_sink = position_sync_sink
        self.allow_order_methods = bool(allow_order_methods)
        self.quote_subscription_manager = quote_subscription_manager
        # QMT runtime-injected global functions (passorder/get_trade_detail_data/
        # 融资融券查询等)。由 strategy._build_config 解析注入。
        self.qmt_api = dict(qmt_api or {})
        self._submit_journal = {}
        # In-process order-identity journal: remark -> strategy_name, written
        # at submit time and read back at query time. The Redis identity store
        # covers restarts and other processes; this covers deployments with no
        # Redis at all (zmq single-file), where attribution used to silently
        # read "" forever (issue #156 follow-up to #133). Bounded FIFO,
        # same 24h TTL as the Redis store.
        self._order_identity_local = collections.OrderedDict()
        # Order settlement. Async by default: blocking here holds the QMT main
        # strategy thread, which serializes every other request behind it and
        # caps throughput at ~2 orders/sec (issue #44).
        self._pending_settlement = None
        self.settle_orders_inline = bool(settle_orders_inline)
        self.order_settle_timeout_seconds = float(order_settle_timeout_seconds)
        # Server-side diagnostic for silent failures (e.g. passorder submitted
        # but order not found in system). Surfaced to client via server_error.
        self._last_server_error = ""
        # 上一次 hollow 告警的时间戳。节流是必须的：一个跑错线程的部署会让
        # **每一次**交易类查询都 hollow，不节流的话日志会被刷爆（#139 就是
        # 一行写了 16 次那种教训）。
        self._last_hollow_warning_at = 0.0
        # 信用账户的「查柜台」通道（#202）。query_credit_account 是异步的：结果
        # 只从 credit_account_callback 出来，所以这里存最后一次回调的结果，由
        # note_credit_account 填。官方 6.13 明说「只能有一个查询」「建议 30s 一
        # 次，不可频繁调用」，所以单飞 + 最小间隔，不能每个请求都透传下去。
        self._credit_account_lock = threading.Lock()
        self._credit_account_rows = []
        self._credit_account_seq = 0
        self._credit_account_stamp = 0.0      # 回调落地的时间
        self._credit_account_asked = 0.0      # 上一次真的发出查询的时间
        self._credit_account_inflight = False
        self._credit_account_error = ""
        # QMT 实际回调了多少次。0 = QMT 没回调；>0 但没数据 = 回调来了但结果
        # 没落进来。两者的排查方向完全不同，从外面看却一样（#202）。
        self._credit_account_callbacks = 0
        # 回调实际落在哪条线程上。adjust 线程 = 等待会把它堵死；C++ 回调线程
        # = 等待是安全的。这是个事实问题，记下来就不用猜（#202）。
        self._credit_account_callback_thread = ""
        self.credit_account_min_interval_seconds = CREDIT_ACCOUNT_MIN_INTERVAL_SECONDS
        self.credit_account_max_wait_seconds = CREDIT_ACCOUNT_MAX_WAIT_SECONDS
        self.credit_account_max_age_seconds = CREDIT_ACCOUNT_MAX_AGE_SECONDS
        if allowed_methods is None:
            allowed = set(READ_METHODS)
            if self.allow_order_methods:
                allowed.update(ORDER_METHODS)
            self.allowed_methods = allowed
        else:
            self.allowed_methods = {str(method) for method in allowed_methods}

    def _request_account_id(self, params):
        params = params or {}
        account = params.get("account")
        if isinstance(account, dict):
            account = account.get("account_id") or account.get("accountID") or account.get("id")
        account_id = str(params.get("account_id") or account or self.account_id or "")
        if not account_id:
            raise ValueError("account_id is required")
        return account_id

    def _canonical_method(self, method):
        return METHOD_ALIASES.get(method, method)

    def handle(self, method, params=None):
        requested_method = str(method or "").strip()
        method = self._canonical_method(requested_method)
        params = dict(params or {})
        # Clear the diagnostic slot per request. It is instance state read by
        # EVERY response (see _build_response), so without this a single failed
        # submit_order stamps its server_error onto every later ping/query until
        # the next order runs -- reporting a stale failure on requests that
        # succeeded (issue #43).
        self._last_server_error = ""
        self._pending_settlement = None
        if not requested_method:
            raise ValueError("method is required")
        if method not in self.allowed_methods:
            raise ValueError("rpc method is not allowed: %s" % requested_method)
        if method in ORDER_METHODS and not self.allow_order_methods:
            raise PermissionError("order rpc methods are disabled")
        handler = getattr(self, "_handle_%s" % method, None)
        if handler is None and method in MARKET_DATA_METHODS:
            return self._handle_market_data_method(method, params)
        elif handler is None:
            raise ValueError("rpc method is not implemented: %s" % requested_method)
        return handler(params)

    def _handle_ping(self, params):
        return {
            "pong": True,
            "account_id": self.account_id,
            "allow_order_methods": bool(self.allow_order_methods),
            "rpc_revision": RPC_REVISION,
            "version": _deployed_version(),
            "account_type": self._reported_account_type(
                params.get("account_id") or self.account_id),
            "server_time": _dt.datetime.now(),
        }

    def _reported_account_type(self, account_id=None):
        """What this deployment will actually trade as.

        The client's StockAccount(..., "CREDIT") never reaches the server --
        the type comes from BIGQMT_ACCOUNT_TYPE in the QMT-side config -- so a
        caller declaring CREDIT against a STOCK deployment has no way to see
        the mismatch. It shows up as an all-zero credit asset row instead
        (issue #92). Empty when there is no gateway to ask.

        When account_id is given and the gateway supports per-request
        resolution, the map-aware type is returned (same #92 class of bug
        for multi-account deployments where a FUTURE account sees "STOCK").
        """
        gateway = self.order_gateway
        if gateway is None:
            return ""
        if account_id is not None and hasattr(gateway, "_resolve_account_type"):
            return str(gateway._resolve_account_type(account_id) or "").strip().upper()
        return str(getattr(gateway, "account_type", "") or "").strip().upper()

    def _handle_get_deployment_info(self, params):
        """Where this bridge is running from, and which build it is.

        Deploying into QMT is a file copy, and QMT keeps modules in sys.modules
        across strategy re-runs -- so "the copy never happened" and "the copy
        landed but was not picked up" are indistinguishable from the client
        side. This lets the client ask instead of guessing, and gives a sync
        tool somewhere to copy to without the trading process rewriting its own
        code.
        """
        import os
        import sys as _sys

        info = {
            "version": "",
            "package_dir": "",
            "qmt_python_dir": "",
            "strategy_dir": "",
            "python_version": "",
            "rpc_revision": RPC_REVISION,
        }
        try:
            info["python_version"] = ".".join(
                str(part) for part in _sys.version_info[:3])
        except Exception:
            pass
        try:
            # Submodule: the QMT sandbox never execs the root package.
            from bigqmt_signal_trader.version import deployment_report

            version, package_dir = deployment_report()
            info["version"] = version
            info["package_dir"] = package_dir
            # The QMT python directory is the package's parent: that is where a
            # sync tool writes, and where the top-level modules live.
            info["qmt_python_dir"] = os.path.dirname(package_dir)
        except Exception as exc:
            info["error"] = "%s: %s" % (exc.__class__.__name__, exc)
        try:
            strategy = _sys.modules.get("bigqmt_signal_trader_strategy")
            path = getattr(strategy, "__file__", "")
            if path:
                info["strategy_dir"] = os.path.dirname(os.path.abspath(path))
        except Exception:
            pass
        return info

    # probe 时检查的关键 QMT 运行时全局函数（存在与否决定对应能力可用性）。
    _PROBE_QMT_GLOBALS = (
        "passorder", "cancel", "get_trade_detail_data",
        "download_history_data", "download_history_data2", "down_history_data",
        "get_history_trade_detail_data", "get_value_by_order_id", "get_last_order_id",
        "get_ipo_data", "get_new_purchase_limit",
        "get_assure_contract", "get_enable_short_contract",
        "get_unclosed_compacts", "get_closed_compacts", "get_debt_contract",
        "get_option_subject_position", "get_comb_option", "get_hkt_exchange_rate",
    )

    # probe 时抽查的 ContextInfo 方法。
    # 板块写入那几个是为 issue #142 加的：读取（get_sector_list /
    # get_stock_list_in_sector）确认可用，而写入走的是哪条通道一直没验过 ——
    # 官方文档把 create_sector(parent_node, sector_name, overwrite) 记为 QMT
    # 全局函数，本仓库却按 ContextInfo.create_sector(sectorname, stocklist) 调。
    # 只探测存在性，不调用：create_sector 是写操作。
    _PROBE_CONTEXT_METHODS = (
        "get_full_tick", "get_market_data_ex", "get_market_data", "get_local_data",
        "subscribe_quote", "subscribe_whole_quote", "unsubscribe_quote",
        "get_trading_dates", "get_financial_data", "get_stock_list_in_sector",
        "do_back_test", "get_trade_detail_data",
        "get_sector_list", "create_sector", "create_sector_folder",
        "add_sector", "remove_sector", "remove_stock_from_sector", "reset_sector",
    )

    def _handle_probe_capabilities(self, params):
        """只读能力探测：当前部署暴露了哪些 QMT callable。

        部署后跑一次就能回答「这台券商 QMT 缺什么」——只读调用，不触发任何
        委托。返回三部分：运行时全局函数绑定情况、ContextInfo 方法存在性、
        信用接口只读探测（调用一次看是否报错/返回行数）。
        """
        info = {
            "account_id": self.account_id,
            "allow_order_methods": self.allow_order_methods,
            "settle_orders_inline": self.settle_orders_inline,
            "order_settle_timeout_seconds": self.order_settle_timeout_seconds,
            "qmt_globals": {},
            "contextinfo_methods": {},
            "credit_probe": {},
            "server_time": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        for name in self._PROBE_QMT_GLOBALS:
            info["qmt_globals"][name] = callable(self.qmt_api.get(name))
        context_info = getattr(self.market_data, "context_info", None)
        for name in self._PROBE_CONTEXT_METHODS:
            info["contextinfo_methods"][name] = callable(getattr(context_info, name, None))
        # 板块写入的全局函数通道（issue #142）。注意 False 的含义有限：QMT 把全局
        # 只注入 *被挂载的那个文件* 的命名空间（PR #134 修的就是这件事），而这里
        # 看到的是本模块的 globals + builtins + 策略捕获过的 qmt_api。所以
        # True 说明确实拿得到，False 只说明「这条路径上没有」，不等于终端没有。
        info["global_namespace"] = {}
        for name in ("create_sector", "create_sector_folder", "add_sector",
                     "remove_sector", "remove_stock_from_sector", "reset_sector",
                     # query_credit_account 也走这条查：它在 qmt_api 里没有时，
                     # 要能分清「这台终端根本没有这个函数」和「有但没绑上」。
                     # 策略侧 _resolve_runtime_name 的兜底正是 builtins，所以
                     # builtins 里没有就等于这台终端不提供（#202）。
                     "query_credit_account"):
            found = self.qmt_api.get(name)
            if not callable(found):
                try:
                    import builtins
                    found = globals().get(name) or getattr(builtins, name, None)
                except Exception:
                    found = None
            info["global_namespace"][name] = callable(found)
        # 信用接口只读探测：不存在的全局直接标 unavailable；存在的真调一次，
        # 记录是否报错和返回行数（担保品/融券标的可能很多行，只计数）。
        # get_unclosed_compacts / get_closed_compacts 是两参数签名（accountID,
        # accountType），单参数调用会撞 boost::python 的 ArgumentError —— 探测
        # 自己少传一个参数，把一个好用的接口报成 ok:False（#201）。
        _two_arg = ("get_unclosed_compacts", "get_closed_compacts")
        for name in ("get_assure_contract", "get_enable_short_contract",
                     "get_unclosed_compacts", "get_debt_contract"):
            func = self.qmt_api.get(name)
            if not callable(func):
                info["credit_probe"][name] = {"available": False}
                continue
            try:
                if name in _two_arg:
                    rows = func(self.account_id,
                                self._configured_account_type(self.account_id)) or []
                else:
                    rows = func(self.account_id) or []
                report = {"available": True, "ok": True}
                report.update(self._describe_probe_rows(rows))
                info["credit_probe"][name] = report
            except Exception as exc:
                info["credit_probe"][name] = {
                    "available": True, "ok": False,
                    "error": "%s: %s" % (exc.__class__.__name__, exc),
                }
        # 信用账户对象本身（query_credit_detail 的数据源）。空列表有两种成因 ——
        # 这本账户不是信用账户，还是这台终端读不到 —— m_nBrokerType 能分开：
        # 3 = 信用。#201 的报告只看到「空列表」，无从判断是哪一种。
        info["credit_probe"]["get_trade_detail_data(CREDIT,ACCOUNT)"] = \
            self._probe_credit_account_object()
        info["credit_callback"] = {
            "note_credit_account_available": hasattr(self, "note_credit_account"),
            "callbacks_seen": getattr(self, "_credit_account_callbacks", 0),
            "cached_rows": len(getattr(self, "_credit_account_rows", []) or []),
            "last_callback_thread": getattr(self, "_credit_account_callback_thread", ""),
        }
        info["ctypes_probe"] = self._probe_ctypes_named_pipe()
        info["thread_routing"] = self._probe_thread_routing()
        info["sector_probe"] = self._probe_sector_channels()
        info["order_watch"] = self._probe_order_watch()
        info["reply_residency"] = self._probe_reply_residency()
        return info

    # ------------------------------------------------------------------
    # 信用账户「查柜台」通道（#202）
    # ------------------------------------------------------------------

    def note_credit_account(self, seq, result):
        """credit_account_callback 的落点。

        QMT 从 C++ 回调线程调进来，所以这里只做「归一化 + 存下来」，绝不碰
        get_trade_detail_data 之类需要主线程上下文的东西，也绝不抛出去 ——
        回调里抛异常在 QMT 那边表现为无堆栈的 SystemError。
        """
        try:
            rows = _normalize_detail_rows([result] if result is not None else [])
        except Exception as exc:
            with self._credit_account_lock:
                self._credit_account_callbacks += 1
                self._credit_account_inflight = False
                self._credit_account_error = "%s: %s" % (exc.__class__.__name__, exc)
            return False
        try:
            thread_name = threading.current_thread().name
        except Exception:
            thread_name = "?"
        with self._credit_account_lock:
            self._credit_account_callbacks += 1
            self._credit_account_callback_thread = thread_name
            self._credit_account_rows = rows
            self._credit_account_stamp = time.time()
            self._credit_account_inflight = False
            self._credit_account_error = ""
            try:
                self._credit_account_seq = int(seq)
            except Exception:
                pass
        return True

    def _issue_credit_account_query(self, account_id):
        """Fire query_credit_account once, honouring the counter's rate limit.

        Returns (issued, reason). Not issuing is a normal outcome, not a
        failure: the cached answer is still served.
        """
        func = self.qmt_api.get("query_credit_account")
        if not callable(func):
            return False, "query_credit_account is not bound in this deployment"
        context_info = getattr(self.market_data, "context_info", None)
        if context_info is None:
            return False, "no ContextInfo available"
        now = time.time()
        with self._credit_account_lock:
            if self._credit_account_inflight:
                # 兜底：inflight 卡住超过一个最小间隔就当它丢了，重新放行。
                # 官方说「前一个查询还在进行中，后面的会提前返回」，所以重问
                # 最坏也就是提前返回；而卡死是永久失去这条通道。
                stuck_for = now - self._credit_account_asked
                if stuck_for < self.credit_account_min_interval_seconds:
                    return False, ("a query is already in flight (%.1fs)"
                                   % stuck_for)
                self._credit_account_inflight = False
            since = now - self._credit_account_asked
            if self._credit_account_asked and since < self.credit_account_min_interval_seconds:
                return False, ("rate limited: %.1fs since the last query, minimum %.1fs"
                               % (since, self.credit_account_min_interval_seconds))
            self._credit_account_inflight = True
            self._credit_account_asked = now
            seq = int(now)
        try:
            func(account_id, seq, context_info)
        except Exception as exc:
            with self._credit_account_lock:
                self._credit_account_inflight = False
                self._credit_account_error = "%s: %s" % (exc.__class__.__name__, exc)
            return False, self._credit_account_error
        return True, ""

    def _handle_query_credit_account(self, params):
        """信用账户明细，查柜台的那条路（官方参考 6.13）。

        query_credit_account(accId, seq, ContextInfo) 是异步的，结果只从
        credit_account_callback 出来，所以这个 handler 做的是：发一次查询（受
        30s 最小间隔约束），有界地等回调落地，然后连同「数据是什么时候的、这次
        有没有真发出去」一起返回。等不到就返回上一次的缓存 —— 但响应里会说清楚
        stale=true，不会把陈数据冒充新的。
        """
        account_id = self._request_account_id(params)
        wait_seconds = params.get("wait_seconds")
        if wait_seconds is None:
            wait_seconds = CREDIT_ACCOUNT_DEFAULT_WAIT_SECONDS
        wait_seconds = max(0.0, min(self.credit_account_max_wait_seconds,
                                    float(wait_seconds)))
        max_age_seconds = params.get("max_age_seconds")
        if max_age_seconds is None:
            max_age_seconds = self.credit_account_max_age_seconds
        max_age_seconds = max(0.0, float(max_age_seconds))
        with self._credit_account_lock:
            stamp_before = self._credit_account_stamp
            known_callback_thread = self._credit_account_callback_thread
        # 回调如果投递在**本线程**上，等待就永远等不到 —— 我们正占着它。实盘
        # 实测（真两融账户）：callback_thread="MainThread"，而 handler 也在
        # MainThread，等满 8 秒 callbacks_seen 不动，那几次回调全是在两次调用
        # 之间、handler 没占着线程的时候落下的。所以一旦观测到这种情况就不再
        # 等，直接返回缓存 —— 白等只是把 adjust 主线程按住几秒（#202）。
        wait_pointless = False
        if wait_seconds > 0 and known_callback_thread:
            try:
                wait_pointless = (known_callback_thread
                                  == threading.current_thread().name)
            except Exception:
                wait_pointless = False
            if wait_pointless:
                wait_seconds = 0.0
        issued, reason = self._issue_credit_account_query(account_id)
        deadline = time.time() + wait_seconds
        while issued and time.time() < deadline:
            with self._credit_account_lock:
                if self._credit_account_stamp > stamp_before:
                    break
                if not self._credit_account_inflight:
                    break
            # sleep 让出 GIL —— 但如果回调本来就要走这条线程，让出 GIL 也没用，
            # 见 CREDIT_ACCOUNT_DEFAULT_WAIT_SECONDS 上面的说明。
            time.sleep(0.02)
        waited = round(time.time() - (deadline - wait_seconds), 3)
        with self._credit_account_lock:
            # 等不到就把 inflight 放掉。原来只有「回调到达」和「func 抛异常」
            # 两条路会清它 —— 超时路径没人清，于是**一次回调丢失就把这条通道
            # 永久卡死**：之后每次调用都撞 "a query is already in flight"，再也
            # 发不出去。放掉的代价只是可能重复问一次柜台，而 30s 最小间隔还在。
            timed_out = (self._credit_account_inflight
                         and not (self._credit_account_stamp > stamp_before))
            if timed_out:
                self._credit_account_inflight = False
            rows = list(self._credit_account_rows)
            stamp = self._credit_account_stamp
            error = self._credit_account_error
            seq = self._credit_account_seq
            callbacks = self._credit_account_callbacks
        # 太陈的数据宁可不给。给出去而不被察觉，比给不出更坏。
        age = round(time.time() - stamp, 3) if stamp else None
        dropped_stale = False
        stale_reason = ""
        if rows and max_age_seconds > 0 and age is not None and age > max_age_seconds:
            dropped_stale = True
            stale_reason = (
                "缓存已 %.1f 秒未更新，超过 max_age_seconds=%.0f，不再交出 —— "
                "这条通道的缓存不会自己刷新。查询已发出，稍后再调一次即可拿到"
                "新的；要实时数据请用 query_credit_detail。" % (age, max_age_seconds))
            rows = []
        return {
            "rows": rows,
            "count": len(rows),
            "query_issued": issued,
            "not_issued_reason": "" if issued else reason,
            "fresh": bool(stamp and stamp > stamp_before),
            "stale": bool(rows and not (stamp and stamp > stamp_before)),
            "age_seconds": age,
            "max_age_seconds": max_age_seconds,
            # 有数据但太陈，已经扣下不给 —— 必须说出来，否则和「没数据」没区别
            "dropped_stale": dropped_stale,
            "dropped_stale_reason": stale_reason,
            "seq": seq,
            "error": error,
            "callback_bound": callable(self.qmt_api.get("query_credit_account")),
            # 这两个才是能定位问题的：callbacks_seen=0 说明 QMT 压根没回调
            # （等太短？账户不支持？回调没挂上？），>0 说明回调通了、问题在别处。
            "callbacks_seen": callbacks,
            "waited_seconds": waited,
            # 回调实际落在哪条线程上。这一条能证伪上面那个「回调走主线程、
            # 被我的等待堵住」的假设 —— 不用再猜。
            "callback_thread": self._credit_account_callback_thread,
            # 这次是不是等超时并把 inflight 放掉了
            "inflight_released": bool(timed_out),
            # 请求要等、但我们已经知道等不到，得说出来，别让人以为等过了
            "wait_skipped_same_thread": bool(wait_pointless),
            "note": ("回调投递在本线程（%s）上，等待永远等不到 —— 已跳过等待。"
                     "查询已发出，结果随后落进缓存，下一次调用即可取到"
                     % known_callback_thread) if wait_pointless else
                    ("默认不等回调：查询已发出，结果随后落进缓存，下一次调用即可"
                     "取到（handler 跑在 adjust 主线程上，等待可能把回调堵在门外）"
                     if wait_seconds == 0 else ""),
        }

    def _describe_probe_rows(self, rows):
        """行数不等于有数据 —— 这条探测自己踩过这个坑。

        QMT 的交易类查询跑在主策略线程之外时，返回的不是空列表，而是**行数
        对、字段全空**的对象（本机实测：把 get_asset 移出 defer 名单，它照样
        返回 5 个键，但 cash / total_asset / frozen_cash / market_value 全是
        None）。原来这里只报 len(rows)，于是在一份真两融账户的报告里给出了
        `ok: true, rows: 71002`，而同一次运行里走 handler 的同一个函数是 0 行
        —— 探测反过来把人带偏了（#204）。

        所以这里过一遍 handler 用的同一个归一化，再看**字段里到底有没有东西**。
        CLAUDE.md 那句「Verify the meaning, not the shape」，说的就是这个。
        """
        report = {"rows": len(rows)}
        try:
            normalized = _normalize_detail_rows(rows)
        except Exception as exc:
            report["normalize_error"] = "%s: %s" % (exc.__class__.__name__, exc)
            return report
        report["normalized_rows"] = len(normalized)
        first = normalized[0] if normalized else None
        if not isinstance(first, dict):
            return report
        report["fields"] = len(first)
        # None / "" 才算「没拿到」。0 是合法值（没有负债就是 0），不能当空看。
        present = [key for key, value in first.items()
                   if value is not None and value != ""]
        report["populated_fields"] = len(present)
        # first 必须真的有字段。零字段是「没数据」，不是「空行」—— 判错的话
        # 一个返回 {} 的接口会被报成跑错线程（本机实跑第一次就误报了）。
        if first and not present:
            # 行在、字段全空 —— 这就是跑错线程的签名，必须说出来，不能让
            # 一个 rows=71002 看着像成功。
            report["hollow"] = True
            report["note"] = ("行数对但字段全空 —— QMT 交易类查询在主策略线程"
                              "之外就是这个样子，查 thread_routing")
        return report

    def _probe_ctypes_named_pipe(self):
        """QMT 的导入白名单放不放行 ctypes + kernel32 命名管道。

        白名单拒过 socket（还包括 logging.handlers 间接引入的那次），所以在
        某些券商终端上 redis / pyzmq 都装不进去，桥根本没有线可用。命名管道
        走 ctypes.WinDLL("kernel32")，是标准库、也不是 socket —— 如果放行，
        它就是那类终端上唯一能用的传输。

        这一条只能在 QMT 沙箱里问：用终端自带解释器 import 只测得到解释器，
        测不到 QMT 运行时的白名单。逐级探测，每一级失败都记下原因，因为
        「import 过了」不等于「建得出管道」——DLL 载入和内核对象创建是两回事。
        """
        report = {}
        try:
            import ctypes
            report["import_ctypes"] = True
        except Exception as exc:
            report["import_ctypes"] = False
            report["error"] = "%s: %s" % (exc.__class__.__name__, exc)
            return report
        try:
            from ctypes import wintypes  # noqa: F401
            report["import_wintypes"] = True
        except Exception as exc:
            report["import_wintypes"] = False
            report["error"] = "%s: %s" % (exc.__class__.__name__, exc)
            return report
        try:
            dll = ctypes.WinDLL("kernel32", use_last_error=True)
            report["load_kernel32"] = True
        except Exception as exc:
            report["load_kernel32"] = False
            report["error"] = "%s: %s" % (exc.__class__.__name__, exc)
            return report
        for func in ("CreateNamedPipeW", "ConnectNamedPipe", "ReadFile",
                     "WriteFile", "CancelIoEx", "CreateFileW"):
            try:
                report["fn_" + func] = bool(getattr(dll, func))
            except Exception:
                report["fn_" + func] = False
        # 真去建一个管道再立刻关掉。这一步才是「能不能用」的答案：DLL 载入
        # 成功不代表内核允许创建命名管道对象（组策略/沙箱可能单独拦）。
        try:
            from ctypes import wintypes

            dll.CreateNamedPipeW.restype = wintypes.HANDLE
            path = "%s%s" % (chr(92) * 2 + "." + chr(92) + "pipe" + chr(92),
                             "bigqmt_whitelist_probe")
            handle = dll.CreateNamedPipeW(path, 0x00000003,
                                          0x00000004 | 0x00000002 | 0x00000000,
                                          1, 4096, 4096, 0, None)
            invalid = ctypes.c_void_p(-1).value
            if handle == invalid:
                report["create_pipe"] = False
                report["create_pipe_error"] = ctypes.get_last_error()
            else:
                report["create_pipe"] = True
                report["pipe_path"] = path
                try:
                    dll.CloseHandle(handle)
                except Exception:
                    pass
        except Exception as exc:
            report["create_pipe"] = False
            report["create_pipe_error"] = "%s: %s" % (exc.__class__.__name__, exc)
        return report

    def _probe_thread_routing(self):
        """哪些方法跑在后台 listener 线程上，哪些被推回 adjust 主线程。

        QMT 的交易类查询在主策略线程之外返回空 —— 这是本项目最重要的一条约束，
        也是 #204 的症结：同一个 QMT 函数，走 adjust 线程有 71002 行，走 listener
        线程 0 行。而远端报告里看不到路由配置，只能靠猜。这一项把它摊开。

        `sample` 里给的是几个有代表性的方法，不是全集：全集有一百多个，塞进
        报告没人看得完。
        """
        service = getattr(self, "rpc_service", None)
        if service is None:
            return {"available": False,
                    "note": "handlers has no rpc_service backref"}
        report = {
            "available": True,
            "process_in_listener": bool(getattr(service, "process_in_listener", False)),
        }
        listener_methods = getattr(service, "listener_methods", None)
        if listener_methods is not None:
            try:
                report["listener_method_count"] = len(listener_methods)
            except Exception:
                pass
        decide = getattr(service, "_should_process_in_listener", None)
        if callable(decide):
            sample = {}
            for name in ("ping", "get_ticks", "get_market_data_ex",
                         "query_credit_detail", "query_stk_compacts",
                         "query_credit_subjects", "get_unclosed_compacts",
                         "get_assure_contract", "get_enable_short_contract",
                         "get_ipo_data", "get_new_purchase_limit",
                         "probe_capabilities", "get_asset", "get_positions"):
                try:
                    sample[name] = ("listener_thread" if decide({"method": name})
                                    else "adjust_thread")
                except Exception as exc:
                    sample[name] = "unknown: %s" % exc.__class__.__name__
            report["sample"] = sample
        return report

    def _probe_credit_account_object(self):
        """Read-only probe of the credit account object behind query_credit_detail.

        Reports the row count and, when there is a row, m_nBrokerType (3 =
        信用) plus whether the credit-only fields came through -- so an empty
        answer can be told apart from a non-credit account, which the bare
        [] the reporter saw could not (#201).
        """
        # getattr 而不是属性直取：不是每种 gateway 都有这个属性（dry-run 的就
        # 没有），而 probe_capabilities 是诊断接口，它自己崩掉是最坏的结果。
        gateway = self.order_gateway
        get_detail = getattr(gateway, "get_trade_detail_data", None)
        if gateway is None or not callable(get_detail):
            return {"available": False}
        try:
            raw = get_detail(self.account_id, "CREDIT", "ACCOUNT", "") or []
        except Exception as exc:
            return {"available": True, "ok": False,
                    "error": "%s: %s" % (exc.__class__.__name__, exc)}
        report = {"available": True, "ok": True}
        report.update(self._describe_probe_rows(raw))
        rows = _normalize_detail_rows(raw) or []
        if rows and isinstance(rows[0], dict):
            report["broker_type"] = rows[0].get("m_nBrokerType")
            # 键在不等于值在。跑错线程时字段名一个不少、值全是 None，用
            # `key in row` 判断会报 has_credit_fields=True，而调用方拿到的
            # 是一行 null —— 和 rows=71002 那个误导是同一个错误（#204）。
            report["has_credit_fields"] = any(
                rows[0].get(key) is not None for key in
                ("m_dPerAssurescaleValue", "m_dFinMaxQuota", "m_dSloMaxQuota"))
        return report

    def _probe_reply_residency(self):
        """How long finished replies wait for the transport to send them.

        Handlers run on the adjust thread, so on zmq every reply is queued for
        the router thread and only leaves at the top of its loop. This says
        whether that wait is where the round trip goes (#104).
        """
        transport = getattr(self, "rpc_transport", None)
        stats = getattr(transport, "reply_residency_stats", None)
        if not callable(stats):
            return {"available": False}
        try:
            report = dict(stats())
        except Exception as exc:
            return {"available": False,
                    "error": "%s: %s" % (exc.__class__.__name__, exc)}
        report["available"] = True
        return report

    def _probe_order_watch(self):
        """Is the callback-fed settlement table live here (issue #164)?

        The table is wired onto the handlers by bigqmt_signal_trader_strategy,
        a top-level file QMT execs -- reload_deployment cannot refresh it. So a
        deployment can carry every line of the #164 code and still be running
        the old poll loop until someone restarts the strategy, with nothing to
        tell the two apart. This says which one is running.

        Counts only: remarks and order ids identify orders.
        """
        table = getattr(self, "order_watch_table", None)
        if table is None:
            return {
                "wired": False,
                "note": ("settlement is polling; the watch table is wired in "
                         "the strategy file, which needs a strategy RESTART "
                         "(reload_deployment cannot refresh it)"),
            }
        report = {"wired": True}
        try:
            report.update(table.stats())
        except Exception as exc:
            report["stats_error"] = "%s: %s" % (exc.__class__.__name__, exc)
        return report

    # 板块通道探测（issue #143）。写入板块有三条可能的通道，名字还各不相同：
    #   * ContextInfo.create_sector           大 QMT 内置 Python
    #   * 原生 xtdata.add_sector/remove_sector MiniQMT SDK（要行情服务）
    #   * QMT 注入的全局函数                   文档 §4.7 那一族
    # 哪条能用只有终端自己知道，所以枚举而不是查固定表 —— 上面那两个 block
    # 就是查表，于是 add_stock_to_sector / reset_sector_stock_list 根本没被
    # 看见过。全部只读：不建板块、不改成分股。
    _SECTOR_WRITE_NAMES = (
        "create_sector", "create_sector_folder", "add_stock_to_sector",
        "remove_stock_from_sector", "reset_sector_stock_list",
        "add_sector", "remove_sector", "reset_sector",
    )

    @staticmethod
    def _enumerate_sector_names(target):
        if target is None:
            return []
        try:
            return sorted(n for n in dir(target)
                          if "sector" in n.lower() and callable(getattr(target, n, None)))
        except Exception:
            return []

    def _probe_sector_channels(self):
        report = {}
        context_info = getattr(self.market_data, "context_info", None)
        report["contextinfo_sector_names"] = self._enumerate_sector_names(context_info)
        report["qmt_global_sector_names"] = sorted(
            name for name, func in (self.qmt_api or {}).items()
            if "sector" in name.lower() and callable(func))
        report["write_names_found"] = {
            name: {
                "contextinfo": callable(getattr(context_info, name, None)),
                "qmt_global": callable((self.qmt_api or {}).get(name)),
            }
            for name in self._SECTOR_WRITE_NAMES
        }

        native = None
        try:
            from .adapters.market_bigqmt import _load_native_xtdata

            native = _load_native_xtdata()
        except Exception as exc:
            report["native_xtdata_error"] = "%s: %s" % (exc.__class__.__name__, exc)
        report["native_xtdata_loaded"] = native is not None
        report["native_sector_names"] = self._enumerate_sector_names(native)
        for name in self._SECTOR_WRITE_NAMES:
            if name in report["write_names_found"]:
                report["write_names_found"][name]["native_xtdata"] = callable(
                    getattr(native, name, None))

        # 唯一真调的一次，而且是读：它回答「这台终端到底能不能列出真实板块」,
        # 也就是 get_sector_list 现在是不是在拿硬编码兜底冒充真数据。
        if native is not None and callable(getattr(native, "get_sector_list", None)):
            try:
                listing = native.get_sector_list() or []
                report["native_get_sector_list"] = {
                    "ok": True, "count": len(listing),
                    "sample": [str(x) for x in list(listing)[:8]],
                }
            except Exception as exc:
                report["native_get_sector_list"] = {
                    "ok": False, "error": "%s: %s" % (exc.__class__.__name__, exc)}
        else:
            report["native_get_sector_list"] = {"ok": False, "error": "not available"}
        try:
            reported = self.market_data.get_sector_list() or []
            fallback = list(getattr(self.market_data, "_FALLBACK_SECTORS", ()) or ())
            report["get_sector_list_now"] = {
                "count": len(reported),
                "is_the_hardcoded_fallback": list(reported) == fallback,
                "sample": [str(x) for x in list(reported)[:8]],
            }
        except Exception as exc:
            report["get_sector_list_now"] = {
                "error": "%s: %s" % (exc.__class__.__name__, exc)}
        return report

    # ------------------------------------------------------------------
    # 全推行情订阅控制（引用计数共享 ContextInfo.subscribe_whole_quote）。
    # 数据本身走推送通道；这里只负责订阅生命周期 + 心跳。
    # ------------------------------------------------------------------

    def _require_quote_manager(self):
        manager = self.quote_subscription_manager
        if manager is None:
            raise RuntimeError("whole-quote push subscription is not configured on this server")
        return manager

    @staticmethod
    def _quote_params(params, require_codes=False):
        params = params or {}
        client_id = str(params.get("client_id") or "").strip()
        sub_id = str(params.get("sub_id") or "").strip()
        if not client_id:
            raise ValueError("client_id is required")
        if not sub_id:
            raise ValueError("sub_id is required")
        codes = [str(c) for c in (params.get("codes") or []) if str(c or "").strip()]
        if require_codes and not codes:
            raise ValueError("codes is required")
        return client_id, sub_id, codes

    def _handle_subscribe_whole_quote(self, params):
        manager = self._require_quote_manager()
        client_id, sub_id, codes = self._quote_params(params, require_codes=True)
        return manager.subscribe(client_id, sub_id, codes)

    def _handle_unsubscribe_whole_quote(self, params):
        manager = self._require_quote_manager()
        client_id, sub_id, _codes = self._quote_params(params)
        manager.unsubscribe(client_id, sub_id)
        return {}

    def _handle_quote_keepalive(self, params):
        manager = self._require_quote_manager()
        client_id, sub_id, _codes = self._quote_params(params)
        manager.keepalive(client_id, sub_id)
        return {}

    def _handle_quote_subscription_status(self, params):
        """Read-only: what is subscribed, by how many clients, and how stale.

        The answer to "I lost my seq and something is still pushing": a combo
        that stays fresh has a LIVE keepalive feeding it (a leftover client
        process, not a leak); one going silent past the heartbeat timeout is
        about to be reaped.
        """
        return self._require_quote_manager().status()

    def _handle_quote_unsubscribe_all(self, params):
        """Operator kill switch: tear every combo down without needing a seq.

        keepalive is a no-op on unknown sub_ids, so a force-cleared combo
        stays down until someone subscribes again.
        """
        manager = self._require_quote_manager()
        return {"unsubscribed": manager.unsubscribe_all()}

    def _identity_redis(self):
        """Redis for the order-identity store, or None.

        Deliberately its own attribute rather than reusing the download-job
        client. Only a redis TRANSPORT builds that one, so on a zmq deployment
        it is None -- which silently meant orders were never remembered at
        submit time and so could never be attributed on query (issue #133).
        The strategy wires this one from the redis config whatever the
        transport, and falls back to the download-job client for deployments
        that predate it. Everything here treats None as "no attribution",
        never as an error: naming an order is a nicety, the order is not.
        """
        return (getattr(self, "order_identity_redis_client", None)
                or getattr(self, "download_job_redis_client", None))

    def _download_job_redis(self):
        redis_client = getattr(self, "download_job_redis_client", None)
        if redis_client is None:
            raise RuntimeError("download jobs require a Redis client")
        return redis_client

    def _require_download_worker(self):
        """Refuse to queue work nothing is going to pick up.

        The worker is _pump_download_jobs on the adjust tick, and it is OFF by
        default on Big QMT for the reason the runtime records: the terminal's
        embedded xtdata SDK has no reachable data service, so a download would
        raise 无法连接行情服务 anyway. Same root cause as the download_* methods
        in #130.

        Accepting a job into a queue no one drains looks like success and never
        completes -- worse than the refusal it replaces. So say no, and say
        which switch turns it on.
        """
        if not getattr(self, "download_jobs_enabled", False):
            raise RuntimeError(
                "async download jobs are disabled on this deployment, so a "
                "submitted job would sit in the queue forever: nothing runs it. "
                "Big QMT's embedded xtdata SDK has no reachable data service to "
                "download through. Supplement history from the terminal's "
                "数据管理/补充数据 UI and read it back with get_market_data_ex / "
                "get_local_data. Set download_jobs_enabled=True in the local "
                "config only where a MiniQMT/xtdata data service is reachable.")

    def _handle_submit_download_history_data2(self, params):
        self._require_download_worker()
        from .download_jobs import submit_download_job

        stock_list = params.get("stock_list") or params.get("stock_code") or []
        if isinstance(stock_list, str):
            stock_list = [stock_list]
        return submit_download_job(
            self._download_job_redis(),
            self.account_id,
            stock_list,
            params.get("period"),
            method="download_history_data2",
            start_time=params.get("start_time", ""),
            end_time=params.get("end_time", ""),
            incrementally=params.get("incrementally"),
            chunk_size=int(params.get("chunk_size") or getattr(self, "download_job_chunk_size", 10)),
            job_ttl_seconds=int(params.get("job_ttl_seconds") or getattr(self, "download_job_ttl_seconds", 3600)),
        )

    def _handle_submit_download_history_data(self, params):
        stock_code = params.get("stock_code") or params.get("code")
        next_params = dict(params or {})
        next_params["stock_list"] = [stock_code] if stock_code else []
        return self._handle_submit_download_history_data2(next_params)

    def _handle_get_download_status(self, params):
        from .download_jobs import read_download_status

        job_id = params.get("job_id")
        if not job_id:
            raise ValueError("job_id is required")
        status = read_download_status(self._download_job_redis(), self.account_id, job_id)
        if status is None:
            raise KeyError("download job not found or expired: %s" % job_id)
        return status

    def _handle_wait_download(self, params):
        from .download_jobs import wait_download_job

        job_id = params.get("job_id")
        if not job_id:
            raise ValueError("job_id is required")
        return wait_download_job(
            self._download_job_redis(),
            self.account_id,
            job_id,
            wait_seconds=float(params.get("wait_seconds", 600.0)),
            poll_interval_seconds=float(params.get("poll_interval_seconds", 0.5)),
        )


    def _handle_get_ticks(self, params):
        codes = params.get("codes")
        if isinstance(codes, str):
            codes = [codes]
        if not codes:
            code = params.get("code")
            codes = [code] if code else []
        if not codes:
            raise ValueError("codes or code is required")
        types = params.get("types")
        if isinstance(types, str):
            types = [types]
        if not types:
            # Pass one argument when nothing was asked for, so a market-data
            # provider whose get_ticks takes only `codes` keeps working.
            return self.market_data.get_ticks(codes)
        return self.market_data.get_ticks(codes, types=list(types))

    def _handle_get_instrument(self, params):
        code = params.get("code")
        if not code:
            raise ValueError("code is required")
        return self.market_data.get_instrument(code)

    def _handle_market_data_method(self, method, params):
        handler = getattr(self.market_data, method, None)
        if handler is None:
            raise NotImplementedError("market data method is not available: %s" % method)
        return handler(**dict(params or {}))

    def _handle_get_positions(self, params):
        return self.position_provider.get_positions(self._request_account_id(params))

    def _handle_get_position_statistics(self, params):
        return self.position_provider.get_position_statistics(self._request_account_id(params))

    def _handle_query_stock_position(self, params):
        stock_code = str(params.get("stock_code") or params.get("code") or "").strip()
        if not stock_code:
            raise ValueError("stock_code is required")
        normalized_code = normalize_stock_code(stock_code)
        positions = self.position_provider.get_positions(self._request_account_id(params))
        return positions.get(normalized_code)

    def _handle_get_asset(self, params):
        return self.position_provider.get_asset(self._request_account_id(params))

    def _handle_query_orders(self, params):
        if self.order_gateway is None:
            raise RuntimeError("order_gateway is not configured")
        # strategy_name filters orders by the name used in passorder. An empty
        # string returns ALL orders for the account (verified via diagnostic:
        # st="" -> 9 orders, st="bigqmt_signal_trader" -> 0). Default to ""
        # so callers see every order unless they explicitly filter.
        orders = self.order_gateway.query_orders(
            self._request_account_id(params),
            str(params.get("strategy_name") or ""),
        )
        if _bool_value(params.get("cancelable_only"), False):
            orders = [
                order
                for order in orders
                if str(getattr(order, "status", "") or "") in CANCELABLE_ORDER_STATUSES
            ]
        return self._attribute_to_strategies(
            self._request_account_id(params), orders)

    def _handle_query_trades(self, params):
        if self.order_gateway is None:
            raise RuntimeError("order_gateway is not configured")
        # Empty strategy_name returns ALL deals for the account (see query_orders
        # note). Default "" so callers see every trade unless they filter.
        strategy_name = params.get("strategy_name")
        if strategy_name is None:
            strategy_name = ""
        account_id = self._request_account_id(params)
        return self._attribute_to_strategies(
            account_id,
            self.order_gateway.query_trades(account_id, str(strategy_name)),
        )

    def _attribute_to_strategies(self, account_id, snapshots):
        """Put the caller's strategy name on rows this bridge submitted.

        Neither the ORDER nor the DEAL rows get_trade_detail_data returns carry
        m_strStrategyName -- checked by listing every attribute on a live
        terminal. What the row DOES carry (m_strSource) is the QMT-side
        strategy's registered name -- this bridge process's name, e.g.
        BIGQMT_REDIS_DRYRUN -- not the strategy_name the caller passed at order
        time (#216: placed as 'DaBanStrategy', reported as '大QMT桥接器').
        #174 misread that field as "passorder's strategyName coming back".

        Orders this bridge submitted are remembered at submit time, keyed by
        the user_order_id that rides out as the order remark, and that record
        is authoritative -- it WINS over the row's field whenever it has a
        name. Rows without a remark (hand-placed orders) or without an
        identity record (another process's orders) keep the row's value.
        """
        rows = list(snapshots or [])
        candidates = [row for row in rows
                      if str(getattr(row, "user_order_id", "") or "").strip()]
        if not candidates:
            return rows
        redis_client = self._identity_redis()
        if redis_client is not None:
            try:
                from .exec_events import order_identity_map

                identities = order_identity_map(
                    redis_client, account_id,
                    [getattr(row, "user_order_id", "") for row in candidates])
                for row in candidates:
                    identity = identities.get(
                        str(getattr(row, "user_order_id", "") or "").strip())
                    if identity and identity.get("strategy_name"):
                        row.strategy_name = str(identity.get("strategy_name") or "")
            except Exception:
                pass
        # No-Redis deployments still name what THIS process submitted: the
        # in-process journal written at submit time (issue #156). It runs
        # after the redis store so this process's own record wins a collision.
        journal = getattr(self, "_order_identity_local", None)
        if journal:
            now = time.time()
            for row in candidates:
                key = (str(account_id or ""),
                       str(getattr(row, "user_order_id", "") or "").strip())
                entry = journal.get(key)
                if not entry:
                    continue
                ts, name = entry
                if name and now - ts <= self._ORDER_IDENTITY_LOCAL_TTL_SECONDS:
                    row.strategy_name = name
        return rows

    _ORDER_IDENTITY_LOCAL_LIMIT = 5000
    _ORDER_IDENTITY_LOCAL_TTL_SECONDS = 86400.0

    def _remember_order_identity_local(self, account_id, remark, strategy_name):
        remark = str(remark or "").strip()
        if not remark:
            return
        key = (str(account_id or ""), remark)
        try:
            journal = getattr(self, "_order_identity_local", None)
            if journal is None:
                # Tests (and the QMT sandbox) build handlers via __new__ and
                # skip __init__ -- create on first use.
                journal = self._order_identity_local = collections.OrderedDict()
            journal[key] = (time.time(), str(strategy_name or ""))
            journal.move_to_end(key)
            while len(journal) > self._ORDER_IDENTITY_LOCAL_LIMIT:
                journal.popitem(last=False)
        except Exception:
            pass

    def _handle_describe_trade_detail_fields(self, params):
        """Report which attributes QMT's ORDER / DEAL rows carry. Names only.

        Answers "why is field X empty / missing" without another
        deploy-and-restart round trip -- see BigQmtOrderGateway
        .describe_detail_fields. Deferred to the main thread with the other
        trade-context queries: get_trade_detail_data returns EMPTY off it.
        """
        if self.order_gateway is None:
            raise RuntimeError("order_gateway is not configured")
        describe = getattr(self.order_gateway, "describe_detail_fields", None)
        if describe is None:
            raise RuntimeError(
                "this deployment predates describe_trade_detail_fields; "
                "sync and restart the strategy")
        detail_types = params.get("detail_types") or params.get("detail_type")
        if isinstance(detail_types, str):
            detail_types = [detail_types]
        shape_fields = params.get("shape_fields") or params.get("shape_field")
        if isinstance(shape_fields, str):
            shape_fields = [shape_fields]
        if shape_fields:
            return describe(self._request_account_id(params), detail_types,
                            shape_fields=shape_fields)
        return describe(self._request_account_id(params), detail_types)

    def _handle_reload_deployment(self, params):
        """Re-import the package and re-run init, without a strategy restart.

        Only schedules it: the reload calls reset_app(), which stops the RPC
        service answering this very request, so the reply has to go out first.
        It runs on the next adjust tick -- poll reload_status.

        Refreshes everything under bigqmt_signal_trader/. Cannot refresh the
        strategy file or the entry script: QMT execs those, and a module cannot
        reload the one it is running in. Those still need a restart.
        """
        hook = getattr(self, "reload_hook", None)
        if hook is None:
            raise RuntimeError(
                "this deployment cannot reload itself (it predates "
                "reload_deployment); sync and restart the strategy once, after "
                "which reloads no longer need a restart")
        return hook(str((params or {}).get("reason") or ""))

    def _handle_reload_status(self, params):
        status_hook = getattr(self, "reload_status_hook", None)
        if status_hook is None:
            raise RuntimeError("this deployment predates reload_deployment")
        return status_hook()

    def _handle_query_execution_snapshot(self, params):
        if self.order_gateway is None:
            raise RuntimeError("order_gateway is not configured")
        account_id = self._request_account_id(params)
        order_name = params.get("order_strategy_name")
        if order_name is None:
            order_name = params.get("strategy_name", self.default_strategy_name)
        trade_name = params.get("trade_strategy_name")
        if trade_name is None:
            trade_name = ""
        return {
            "account_id": account_id,
            "server_time": _dt.datetime.now(),
            "rpc_revision": RPC_REVISION,
            "orders": self.order_gateway.query_orders(account_id, str(order_name)),
            "trades": self.order_gateway.query_trades(account_id, str(trade_name)),
        }

    def _handle_sync_positions(self, params):
        account_id = self._request_account_id(params)
        snapshot = AccountSnapshot(
            account_id=account_id,
            asset=self.position_provider.get_asset(account_id),
            positions=self.position_provider.get_positions(account_id),
            reason=str(params.get("reason") or "rpc"),
            updated_at=_dt.datetime.now(),
        )
        if self.position_sync_sink is not None:
            self.position_sync_sink.publish(snapshot)
        return snapshot

    # ------------------------------------------------------------------
    # 账户 / 融资融券 / 交易扩展查询
    # 这些是 Big QMT 运行时注入的全局函数（同 passorder），不在 ContextInfo 桩里。
    # 函数名严格按官方文档（trading_function.html），通过 self.qmt_api 调用。
    # 无该权限/函数未注入时降级为空列表。
    # ------------------------------------------------------------------

    def _warn_if_hollow(self, source, rows):
        """交易类响应「行数对、字段全空」时大声报出来，别让它静默过去。

        QMT 的交易类查询跑在主策略线程之外时，返回的不是空列表，而是行数对、
        字段全是 None 的对象（本机实测：get_asset 移出 defer 名单后照样回 5 个
        键，值全空）。从客户端看像「这个账户没钱」，不像调用失败 —— #204 就是
        这么在线上静默了几个月。

        静态闸（tests/.../test_trade_context_deferral_guard.py）保证我们**自己**
        不写漏，但挡不住「换一家券商行为不一样」。所以运行时再看一眼：真出现了
        就记一条 warning，把函数名和当前线程名一起写出来，让「每家不一样」成为
        被观测到的事实，而不是我们猜出来的。

        只告警、不改路由：defer 实测只要 2.5-3.0ms，而猜错方向的代价是静默返回
        错数据，两边完全不对等；而且「跑错线程」和「这个账户真没数据」从返回值
        上分不开，据此自动切换只会把一次误判固化下来。
        """
        if not rows:
            return rows
        first = rows[0] if isinstance(rows, (list, tuple)) else rows
        if not isinstance(first, dict) or not first:
            return rows
        if any(value is not None and value != "" for value in first.values()):
            return rows
        now = time.time()
        if now - self._last_hollow_warning_at < HOLLOW_WARNING_INTERVAL_SECONDS:
            return rows
        self._last_hollow_warning_at = now
        try:
            thread_name = threading.current_thread().name
        except Exception:
            thread_name = "?"
        try:
            from .logging_setup import get_logger
            get_logger("rpc").warning(
                "%s returned %d row(s) with every field empty, on thread %r. "
                "QMT trade-context queries answer this way off the main strategy "
                "thread -- the call did NOT fail, the data is simply not there. "
                "Check probe_capabilities thread_routing (#204).",
                source, len(rows), thread_name)
        except Exception:
            pass
        return rows

    def _call_qmt_global(self, func_name, *args, **kwargs):
        """Call a QMT runtime-injected global function, returning [] on failure.

        These functions (get_assure_contract / get_unclosed_compacts / ...)
        are injected by QMT into the process global namespace, same as
        passorder. When unavailable (no margin account, function not bound)
        we degrade to [] rather than crashing the RPC.
        """
        func = self.qmt_api.get(func_name)
        if func is None:
            return []
        try:
            return self._warn_if_hollow(func_name,
                                        _normalize_detail_rows(func(*args, **kwargs)))
        except Exception:
            return []

    def _call_qmt_scalar(self, func_name, *args, **kwargs):
        """Same as _call_qmt_global, for the QMT globals that answer with a
        plain scalar rather than detail rows -- get_last_order_id returns the
        委托号 as a str.

        The row normaliser iterates whatever it is handed, so a string goes in
        and a list of one empty dict PER CHARACTER comes out: the real order id
        '635005110' became [{}, {}, {}, {}, {}, {}, {}, {}, {}] and '-1'
        (QMT's "not found") became [{}, {}]. An int raises TypeError inside the
        normaliser and is swallowed to []. Same shape as #96, where get_ipo_data
        was destroyed the same way (#207).
        """
        func = self.qmt_api.get(func_name)
        if func is None:
            return None
        try:
            return _normalize_mapping_value(func(*args, **kwargs))
        except Exception:
            return None

    def _call_qmt_object(self, func_name, *args, **kwargs):
        """Same as _call_qmt_global, for the QMT globals that answer with ONE
        object rather than a list of them -- get_value_by_order_id returns a
        single 委托/成交 object.

        Handing that straight to the row normaliser iterates the object itself,
        which raises TypeError and gets swallowed to [] -- the call looks like
        "no such order" when it actually worked (#207).
        """
        func = self.qmt_api.get(func_name)
        if func is None:
            return {}
        try:
            value = func(*args, **kwargs)
        except Exception:
            return {}
        if value is None:
            return {}
        if isinstance(value, (list, tuple)):
            rows = _normalize_detail_rows(value)
            return rows[0] if rows else {}
        rows = _normalize_detail_rows([value])
        return rows[0] if rows else {}

    def _call_qmt_mapping(self, func_name, *args, **kwargs):
        """Same as _call_qmt_global, for the QMT globals that answer with a
        mapping rather than a row list -- get_ipo_data, get_new_purchase_limit.

        The row normaliser would iterate such a dict by key and throw the values
        away, so these keep their shape and only have their values made
        JSON-safe.
        """
        func = self.qmt_api.get(func_name)
        if func is None:
            return {}
        try:
            data = func(*args, **kwargs)
        except Exception:
            return {}
        if isinstance(data, dict):
            return dict((str(key), _normalize_mapping_value(value))
                        for key, value in data.items())
        # Some brokers hand back rows even here; normalise rather than drop.
        return _normalize_detail_rows(data)

    def _configured_account_type(self, account_id=None):
        """The account type this deployment will trade as, per-request aware.

        When the gateway supports per-request account_type resolution (i.e.
        has ``_resolve_account_type`` from BIGQMT_ACCOUNT_TYPE_MAP), this
        returns the map-aware type for the given account_id.  When no map
        is configured, returns the gateway's own ``account_type`` unchanged.

        The *account_id* parameter is explicit (not read from instance state)
        because zmq deployments have a background listener thread calling
        market-data methods concurrently with the adjust thread calling trade
        methods -- storing params on ``self`` would race (issue #43, #164).
        """
        gateway = self.order_gateway
        if gateway is not None and hasattr(gateway, "_resolve_account_type"):
            try:
                aid = account_id or self.account_id or ""
                if aid:
                    return str(gateway._resolve_account_type(aid)).strip().upper()
            except Exception:
                pass
        return str(
            getattr(gateway, "account_type", "CREDIT") or "CREDIT"
        ).strip().upper()

    def _query_trade_detail(self, params, detail_type, strategy_name="",
                            account_type=None):
        """get_trade_detail_data with one of the 6 official detail types.

        Official strDatatype values: ACCOUNT / POSITION / POSITION_STATISTICS /
        ORDER / DEAL / TASK. Other strings are NOT supported as *detail types* —
        use the dedicated functions below for the margin contract queries.

        *account_type* overrides the deployment's configured type for this one
        call. That is the ACCOUNT-TYPE axis, not the detail-type axis: the
        credit account object is `(accId, 'CREDIT', 'ACCOUNT')`, which the
        caller asks for explicitly regardless of what this deployment trades
        as (#201).
        """
        account_id = self._request_account_id(params)
        gateway = self.order_gateway
        if gateway is None or gateway.get_trade_detail_data is None:
            return []
        if account_type is None:
            account_type = (gateway._resolve_account_type(account_id)
                            if hasattr(gateway, "_resolve_account_type")
                            else getattr(gateway, "account_type", "STOCK"))
        try:
            rows = gateway.get_trade_detail_data(account_id, account_type, detail_type, strategy_name)
            return self._warn_if_hollow(
                "get_trade_detail_data(%s,%s)" % (account_type, detail_type),
                _normalize_detail_rows(rows))
        except Exception:
            return []

    def _handle_query_account_infos(self, params):
        # 账户信息 — get_trade_detail_data(ACCOUNT)
        return self._query_trade_detail(params, "ACCOUNT")

    def _handle_query_account_status(self, params):
        # 账户状态 — 用 TASK detail type 近似（委托任务状态）
        return self._query_trade_detail(params, "TASK")

    def _handle_query_credit_detail(self, params):
        # 信用（两融）账户明细 — get_trade_detail_data(accId, 'CREDIT', 'ACCOUNT')
        # 返回 CCreditAccountDetail（维持担保比例 / 融资融券授信额度 / 合约金额…），
        # 与 MiniQMT query_credit_detail -> XtCreditDetail 对应，也是本仓
        # docs/MiniQMT_2_BigQMT-Skill/api_mapping.md 一直记着的映射。
        #
        # 0.3.23 之前这里调的是 get_debt_contract(accId)：那是「负债合约明细」——
        # 一张张融资融券合约，不是账户对象，而且官方参考 6.17 已标记【已弃用】
        # （改用 get_unclosed_compacts / get_closed_compacts）。两融账户查它必然
        # 拿不到账户信息，没有未了结负债时更是直接空列表（#201）。负债合约本身
        # 走 query_stk_compacts（get_unclosed_compacts）。
        return self._query_trade_detail(params, "ACCOUNT", account_type="CREDIT")

    def _handle_query_stk_compacts(self, params):
        # 未平仓合约（负债）— 官方 get_unclosed_compacts
        return self._call_qmt_global(
            "get_unclosed_compacts",
            self._request_account_id(params),
            self._configured_account_type(self._request_account_id(params)),
        )

    def _handle_query_credit_subjects(self, params):
        # 融资标的（担保品）— 官方 get_assure_contract
        return self._call_qmt_global("get_assure_contract", self._request_account_id(params))

    def _handle_query_credit_slo_code(self, params):
        # 融券标的 — 官方 get_enable_short_contract
        return self._call_qmt_global("get_enable_short_contract", self._request_account_id(params))

    def _handle_query_credit_assure(self, params):
        # 担保品合约 — 同 query_credit_subjects（get_assure_contract）
        return self._call_qmt_global("get_assure_contract", self._request_account_id(params))

    def _handle_query_appointment_info(self, params):
        # 新股数据 — 官方 get_ipo_data(type)
        # 8-28 修复: 原实现把 account_id 当第一个参数传给 get_ipo_data (期望 type),
        # 导致返回 [{}]. 改为透传 type 参数 ("STOCK"/"BOND"/缺省全部).
        return self._call_qmt_global(
            "get_ipo_data", str(params.get("type") or params.get("stock_type") or ""))

    def _handle_query_smt_secu_info(self, params):
        # 期权标的持仓 — 官方 get_option_subject_position
        return self._call_qmt_global("get_option_subject_position", self._request_account_id(params))

    def _handle_query_smt_secu_rate(self, params):
        # 组合期权 — 官方 get_comb_option
        return self._call_qmt_global("get_comb_option", self._request_account_id(params))

    def _handle_smt_appointment(self, params):
        # SMB/预约打新属于交易类，需要下单通道；当前不支持。
        raise NotImplementedError("smt_appointment is not supported via Big QMT RPC")

    # 官方交易查询函数（直接暴露）
    def _handle_get_value_by_order_id(self, params):
        # 官方签名 get_value_by_order_id(orderId, accountID, strAccountType,
        # strDatatype)，strDatatype 是 'ORDER' / 'DEAL'（6.11）。原来只传了
        # orderId 一个参数 —— 少三个，_call_qmt_global 把 TypeError 吞掉返回
        # 空，从客户端看就是「查不到这笔委托」（#207）。
        order_id = str(params.get("order_id") or params.get("order_sysid") or "")
        if not order_id:
            raise ValueError("order_id is required")
        account_id = self._request_account_id(params)
        detail_type = str(params.get("detail_type")
                          or params.get("datatype") or "ORDER").strip().upper()
        # 返回的是单个委托/成交对象，不是行列表 —— 走 _call_qmt_object，
        # 否则 _normalize_detail_rows 会去迭代这个对象、抛 TypeError 被吞成
        # []，看起来像「查不到这笔委托」（#207）。
        return self._call_qmt_object(
            "get_value_by_order_id", order_id, account_id,
            self._configured_account_type(account_id), detail_type)

    def _handle_get_last_order_id(self, params):
        # 官方签名 get_last_order_id(accountID, strAccountType, strDatatype
        # [, strategyName])（6.11）。原来只传了 accountID。strategyName 只对
        # 本地下单有效，用部署的默认策略名，空则不传。
        account_id = self._request_account_id(params)
        detail_type = str(params.get("detail_type")
                          or params.get("datatype") or "ORDER").strip().upper()
        strategy_name = params.get("strategy_name")
        if strategy_name is None:
            strategy_name = self.default_strategy_name
        strategy_name = str(strategy_name or "")
        args = [account_id, self._configured_account_type(account_id), detail_type]
        if strategy_name:
            args.append(strategy_name)
        # 返回的是委托号字符串，不是行列表 —— 走 _call_qmt_scalar。实盘实测：
        # 走 _call_qmt_global 时 '635005110' 被按字符迭代成 9 个空 dict，
        # QMT 表示「没找到」的 '-1' 变成 2 个空 dict（#207）。
        return self._call_qmt_scalar("get_last_order_id", *args)

    def _handle_get_ipo_data(self, params):
        # get_ipo_data answers with a dict KEYED BY SUBSCRIPTION CODE, not with
        # detail rows. Sending it through _normalize_detail_rows iterates the
        # dict, i.e. its keys, and attribute-scrapes each code string -- so
        # {"730001": {...}, "001234": {...}} came out as [{}, {}]: codes, issue
        # prices and quantities all gone. #96 fixed the `type` argument but the
        # response was still being destroyed here.
        return self._call_qmt_mapping(
            "get_ipo_data", str(params.get("type") or params.get("stock_type") or ""))

    def _handle_get_new_purchase_limit(self, params):
        # Documented as returning a dict of 板块 -> 额度, so it has the same
        # shape problem get_ipo_data had (6.10 in the API reference).
        return self._call_qmt_mapping(
            "get_new_purchase_limit", self._request_account_id(params))

    def _handle_get_history_trade_detail_data(self, params):
        account_id = self._request_account_id(params)
        detail_type = str(params.get("detail_type") or params.get("datatype") or "DEAL")
        start_date = str(params.get("start_date") or params.get("start_time") or "")
        end_date = str(params.get("end_date") or params.get("end_time") or "")
        # 官方签名 get_history_trade_detail_data(accountID, strAccountType,
        # strDatatype, startDate, endDate)（6.9）。原来漏了 strAccountType，
        # 于是 detail_type 被塞进了账户类型的位置 —— 少一个参数、还错位，
        # 和 #96 里 get_ipo_data 把 account_id 当 type 传是同一个形状（#207）。
        result = self._call_qmt_global(
            "get_history_trade_detail_data", account_id,
            self._configured_account_type(account_id),
            detail_type, start_date, end_date
        )
        return result

    def _handle_get_assure_contract(self, params):
        return self._call_qmt_global("get_assure_contract", self._request_account_id(params))

    def _handle_get_enable_short_contract(self, params):
        return self._call_qmt_global("get_enable_short_contract", self._request_account_id(params))

    def _handle_get_unclosed_compacts(self, params):
        return self._call_qmt_global(
            "get_unclosed_compacts",
            self._request_account_id(params),
            self._configured_account_type(self._request_account_id(params)),
        )

    def _handle_get_closed_compacts(self, params):
        return self._call_qmt_global(
            "get_closed_compacts",
            self._request_account_id(params),
            self._configured_account_type(self._request_account_id(params)),
        )

    def _handle_get_debt_contract(self, params):
        return self._call_qmt_global("get_debt_contract", self._request_account_id(params))

    def _handle_get_option_subject_position(self, params):
        return self._call_qmt_global("get_option_subject_position", self._request_account_id(params))

    def _handle_get_comb_option(self, params):
        return self._call_qmt_global("get_comb_option", self._request_account_id(params))

    def _handle_get_hkt_exchange_rate(self, params):
        # 官方签名 get_hkt_exchange_rate(accountID, accountType)，accountType 须为
        # HUGANGTONG / SHENGANGTONG（6.18）。原来一个参数都没传，而 _call_qmt_global
        # 会把 TypeError 吞掉返回 {} —— 又一次静默返回空，从客户端看和「这台终端
        # 没有港股通」一模一样（#205）。
        account_type = str(params.get("account_type")
                           or params.get("accountType") or "HUGANGTONG").strip().upper()
        return self._call_qmt_mapping(
            "get_hkt_exchange_rate", self._request_account_id(params), account_type)

    def _handle_download_history_data(self, params):
        """download_history_data is a QMT global function (issue #32).

        It is NOT a ContextInfo method — the adapter's _call_context path
        always raised NotImplementedError. Now route through qmt_api (the
        injected global), falling back to the adapter (which tries native
        xtdata SDK then ContextInfo).
        """
        func = self.qmt_api.get("download_history_data")
        # Some QMT builds only expose down_history_data (same signature, 4 args).
        if func is None:
            func = self.qmt_api.get("down_history_data")
        if func is not None:
            try:
                stock_code = str(params.get("stock_code") or "")
                period = str(params.get("period") or "1d")
                start_time = str(params.get("start_time") or "")
                end_time = str(params.get("end_time") or "")
                result = func(stock_code, period, start_time, end_time)
                return bool(result) if result is not None else True
            except Exception as exc:
                raise RuntimeError("download_history_data failed: %s" % exc)
        # Fallback: adapter tries native xtdata SDK then ContextInfo.
        # If the adapter lacks the method, return False (not crash).
        try:
            return self._handle_market_data_method("download_history_data", params)
        except (NotImplementedError, AttributeError):
            return False

    def _handle_download_history_data2(self, params):
        """download_history_data2 is a QMT global function (issue #32).

        Native signature includes an optional callback for progress; the QMT
        global may require it, so pass a no-op when the client didn't.

        Some QMT builds expose only the single-stock download globals
        (``download_history_data`` / ``down_history_data``) — fall back to a
        per-code loop with those. Without this the RPC returned False and
        nothing was downloaded, so reads only ever saw the latest day
        (issue #54).
        """
        stock_list = list(params.get("stock_list") or [])
        period = str(params.get("period") or "1d")
        start_time = str(params.get("start_time") or "")
        end_time = str(params.get("end_time") or "")
        func = self.qmt_api.get("download_history_data2")
        if func is not None:
            try:
                # Try with a no-op callback first (some QMT builds require it);
                # fall back to 4-arg call if that raises TypeError.
                try:
                    result = func(stock_list, period, start_time, end_time, lambda data: None)
                except TypeError:
                    result = func(stock_list, period, start_time, end_time)
                return bool(result) if result is not None else True
            except Exception as exc:
                raise RuntimeError("download_history_data2 failed: %s" % exc)
        single = self.qmt_api.get("download_history_data") or self.qmt_api.get("down_history_data")
        if single is not None:
            try:
                result = None
                for code in stock_list:
                    result = single(code, period, start_time, end_time)
                return bool(result) if result is not None else True
            except Exception as exc:
                raise RuntimeError("download_history_data2 per-code fallback failed: %s" % exc)
        try:
            return self._handle_market_data_method("download_history_data2", params)
        except (NotImplementedError, AttributeError):
            return False

    def _handle_probe_order_identity(self, params):
        """Diagnose the strategy_name backfill chain, one link at a time.

        #156's reporter: the identity record exists in Redis, the key matches,
        and the query still reads strategy_name="". Nothing along the chain
        can say which link dropped it from the outside -- this answers that
        from the inside: is the identity Redis wired at all, does the key
        exist under THIS account, is the local journal covering it (#156's
        in-process fallback), and does the raw lookup raise.
        """
        account_id = self._request_account_id(params)
        remark = str(params.get("remark") or params.get("user_order_id") or "").strip()
        redis_client = self._identity_redis()
        journal = getattr(self, "_order_identity_local", None) or {}
        out = {
            "account_id": account_id,
            "remark": remark,
            "identity_redis_wired": redis_client is not None,
            "local_journal_size": len(journal),
        }
        if not remark:
            out["note"] = "pass remark=<the order's remark> to check the key"
            return out
        from .exec_events import order_identity_key

        key = order_identity_key(account_id, remark)
        out["identity_key"] = key
        if redis_client is not None:
            try:
                out["redis_hit"] = bool(redis_client.get(key))
            except Exception as exc:
                out["redis_hit"] = None
                out["lookup_error"] = "%s: %s" % (exc.__class__.__name__, exc)
        entry = journal.get((account_id, remark))
        out["local_hit"] = bool(entry and entry[1])
        if entry:
            out["local_strategy_name"] = entry[1]
        return out

    def _handle_download_holiday_data(self, params):
        # MiniQMT downloads a holiday table from the xtdata service. Big QMT's
        # terminal maintains the trading calendar itself (refreshed at login
        # and by its own data updater), so there is nothing to download and no
        # ContextInfo method to call -- the old generic path answered with
        # NotImplementedError (issue #163). A clear no-op instead.
        return {
            "ok": True,
            "downloaded": False,
            "note": ("Big QMT maintains the trading calendar itself; nothing "
                     "to download. get_trading_dates/get_holidays read the "
                     "terminal's own data."),
        }

    def _handle_download_his_st_data(self, params):
        # Same shape as download_holiday_data: ST history lives in the
        # terminal's own data on Big QMT (issue #163).
        return {
            "ok": True,
            "downloaded": False,
            "note": ("Big QMT maintains ST history in the terminal's own data; "
                     "nothing to download. get_his_st_data reads it directly."),
        }

    def _order_action_from_params(self, params):
        action = str(params.get("action") or "").upper()
        if action:
            return action
        raw = params.get("order_type")
        order_type = str(raw or "").upper()
        if order_type in BUY_ORDER_TYPES:
            return "BUY"
        if order_type in SELL_ORDER_TYPES:
            return "SELL"
        # Credit operations carry their side in the type itself (issue #103).
        # 直接还款 moves cash rather than securities and has no side, so it
        # still needs an explicit action rather than being guessed at.
        credit = _credit_action_of(raw)
        if credit:
            return credit
        if _credit_optype_of(raw) is not None:
            raise ValueError(
                "order_type %s has no implicit buy/sell side; pass action "
                "explicitly" % raw)
        # Futures (0-15) and ETF option (50-59) opTypes carry the side in the
        # type itself. 行权/锁定 (56-59) do not, so they fall through to the
        # same "pass action explicitly" rejection as 直接还款.
        passthrough = _passthrough_action_of(raw)
        if passthrough:
            return passthrough
        if _passthrough_optype_of(raw) is not None:
            raise ValueError(
                "order_type %s has no implicit buy/sell side; pass action "
                "explicitly" % raw)
        if raw in (None, ""):
            raise ValueError("action or order_type is required")
        # An order_type WAS supplied and was not recognised. Saying "required"
        # here sent a reporter looking at their own call for twenty minutes
        # (issue #92): the real answer is almost always that the package
        # deployed inside QMT predates the type they are using, and a
        # client-side pip upgrade cannot fix that -- this code runs in QMT.
        raise ValueError(
            # ASCII only: this text is written to QMT's own log, which drops
            # non-ASCII characters (a Chinese install path came back mangled).
            "order_type %r is not recognised by the package deployed in QMT "
            "(%s). Credit order types (27-32, and 40-45 special) need 0.3.1 "
            "or newer HERE, in the QMT python directory -- upgrading the "
            "client with pip does not change this file. Run "
            "xt_trader.sync_deployment(), restart the strategy, then check "
            "xtdata.get_deployment_info()." % (raw, self._deployed_version()))

    @staticmethod
    def _deployed_version():
        """Never raises: this only ever runs while building an error message."""
        try:
            from bigqmt_signal_trader.version import __version__

            return __version__
        except Exception:
            return "unknown version"

    def _forwarded_order_type(self, params):
        """The order_type to forward untouched to passorder, or None.

        Credit (27-32/40-45/70-75) and futures/option (0-15/50-59) opTypes both
        encode more than a side, so submit() needs the raw value. Read straight
        from params -- no state is carried between calls, so this does not
        depend on the order OrderRequest's keyword arguments happen to be
        evaluated in.
        """
        raw = params.get("order_type")
        if _credit_optype_of(raw) is not None:
            return raw
        if _passthrough_optype_of(raw) is not None:
            return raw
        return None

    # 旧名保留：外部调用方和既有测试还在用
    _credit_order_type_from_params = _forwarded_order_type

    def _handle_submit_order(self, params):
        if self.order_gateway is None:
            raise RuntimeError("order_gateway is not configured")
        price = params.get("price")
        signal_id = str(params.get("signal_id") or "rpc-%s" % uuid.uuid4().hex)
        order_tag = str(params.get("remark") or params.get("order_remark") or "").strip()
        if not order_tag:
            order_tag = "bqrpc:%s" % signal_id
        request = OrderRequest(
            signal_id=signal_id,
            account_id=self._request_account_id(params),
            action=self._order_action_from_params(params),
            stock_code=str(params.get("stock_code") or ""),
            volume=int(params.get("volume") or params.get("order_volume") or 0),
            price=float(price if price not in (None, "") else 0),
            price_type=params.get("price_type") or "LIMIT",
            strategy_name=str(params.get("strategy_name")
                              or self.default_strategy_name),
            remark=order_tag,
            order_type=self._forwarded_order_type(params),
        )
        if request.action not in ("BUY", "SELL"):
            raise ValueError("action must be BUY or SELL")
        if not request.stock_code:
            raise ValueError("stock_code is required")
        if request.volume <= 0:
            raise ValueError("volume must be positive")

        try:
            from .exec_events import remember_order_identity

            remember_order_identity(
                self._identity_redis(),
                request.account_id,
                request.remark,
                strategy_name=request.strategy_name,
                stock_code=request.stock_code,
            )
        except Exception:
            pass
        # Always journal locally too -- cheap, and the only attribution a
        # no-Redis deployment has (issue #156).
        self._remember_order_identity_local(
            request.account_id, request.remark, request.strategy_name)

        result = self.order_gateway.submit(request)

        # 委托后校验：确认委托是否真的进了系统。passorder 调用成功但委托没进
        # 系统时（静默失败），记录 server_error 让客户端知道。匹配严格按
        # user_order_id(remark) 精确比对，不做 stock_code+action 的模糊兜底。
        # QMT 的委托号是异步分配的（passorder 无返回值），这里按唯一
        # user_order_id(remark) 精确匹配并回填 order_sys_id，避免客户端把
        # 「已提交但暂无委托号」误判为下单失败（issue #38）。
        self._last_server_error = ""

        # Async callers opt out of waiting for the order id. MiniQMT's
        # order_stock_async returns a seq immediately and delivers the id through
        # order_callback, so holding the reply until settlement is exactly the
        # latency the async API exists to avoid (issue #50). The order_callback
        # push already carries order_sys_id, so nothing is lost -- only the
        # post-submit "did it land?" check is skipped, and a silent rejection
        # surfaces as the absence of that push rather than as server_error.
        if not _bool_value(params.get("wait_settlement"), True):
            return result

        if self.settle_orders_inline:
            # Opt-out: block here the way this used to. Kept only for runtimes
            # with no adjust drain to retry on.
            try:
                import time as _time
                _time.sleep(self.order_settle_timeout_seconds)
                self._apply_order_lookup(
                    OrderSettlement(request, result, 0.0), final=True, inline=True)
            except Exception:
                pass
            return result
        # Hand the settlement to the caller rather than raising: handle() stays
        # a plain function for anyone driving handlers directly, and only the
        # service defers its reply.
        self._pending_settlement = OrderSettlement(
            request, result, _monotonic() + self.order_settle_timeout_seconds
        )
        return result

    def take_pending_settlement(self):
        """Pop the settlement the last submit_order registered, if any."""
        settlement = self._pending_settlement
        self._pending_settlement = None
        return settlement

    def _apply_order_lookup(self, settlement, final=False, inline=False):
        """Look the order up by remark. True when settled, False to retry.

        MUST run on the main strategy thread -- get_trade_detail_data returns
        empty anywhere else.
        """
        request = settlement.order_request
        settlement.attempts += 1
        # Fast path: QMT's order_callback already pushed the answer
        # (issue #164). A miss means nothing -- fall through to the poll.
        watch = getattr(self, "order_watch_table", None)
        if watch is not None:
            try:
                watched_sysid = watch.sysid_for_remark(request.remark)
            except Exception:
                watched_sysid = None
            if watched_sysid:
                try:
                    settlement.result.order_sys_id = watched_sysid
                except Exception:
                    pass
                return True
        try:
            orders = self.order_gateway.query_orders(request.account_id, "") or []
            by_remark = [
                o for o in orders
                if str(getattr(o, "user_order_id", "") or "").strip() == request.remark.strip()
            ]
            if by_remark:
                sysid = str(getattr(by_remark[0], "order_sys_id", "") or "")
                if sysid:
                    try:
                        settlement.result.order_sys_id = sysid
                    except Exception:
                        pass
                    return True
                # The row is there but m_strOrderSysID is not populated yet.
                # Settling here publishes order_sys_id=None, the client turns
                # that into -1, and a LIVE order is reported as ORDER_REJECTED
                # -- a caller who retries on rejection double-orders. Measured
                # on Guojin 2.1.19.0: the id was present on an immediate manual
                # readback right after the -1 (issue #152). So keep waiting;
                # the row already proves the order reached the broker.
                if not final:
                    return False
                message = (
                    "ORDER IS LIVE -- DO NOT RESUBMIT. passorder reached the "
                    "broker and the order row exists (stock=%s action=%s "
                    "price=%.2f volume=%d), but QMT had still not assigned "
                    "order_sys_id after %d lookup(s), so this reply carries no "
                    "id. Find it by remark %r, or in the 委托 list; it is not a "
                    "rejection (issue #152)."
                    % (request.stock_code, request.action, request.price,
                       request.volume, settlement.attempts, request.remark)
                )
                settlement.server_error = message
                if inline:
                    self._last_server_error = message
                return True
            if not final:
                # Not there yet. QMT assigns the id asynchronously, so an early
                # miss is normal -- only a miss at the deadline is a real one.
                return False
            # Deadline reached with no remark match -> not in the system. Do NOT
            # fall back to matching stock_code+action: order_tag is a unique id
            # we generated, so a miss is always a real miss, while an unrelated
            # order on the same stock and side (a manual one, or an earlier
            # unfilled order) would silently suppress this warning and leave
            # order_sys_id unfilled with no signal at all (issue #41).
            # The first thing to check is the strategy's run mode, not the
            # order. QMT's 模型交易 list has a 运行模式 column that defaults to
            # 模拟, and in that mode passorder matches internally and never
            # reaches the broker: the call succeeds, SUBMITTED comes back, and
            # every lookup finds nothing. This message used to lead with
            # "check price range / permissions", and a reporter spent two
            # hours there before finding the mode (issue #122).
            message = (
                "passorder submitted but order not found in system "
                "(stock=%s action=%s price=%.2f volume=%d, %d lookup(s)). "
                "FIRST check the strategy's run mode: in QMT's 模型交易 list the "
                "运行模式 column defaults to 模拟, where passorder matches "
                "internally and never reaches the broker -- switch it to 实盘 "
                "(a simulated account stays simulated). The editor window and "
                "backtest/signal modes place no real order either. If the mode "
                "is already 实盘, then check price range and permissions."
                % (request.stock_code, request.action, request.price,
                   request.volume, settlement.attempts)
            )
            settlement.server_error = message
            if inline:
                self._last_server_error = message
            return True
        except Exception:
            # A failed lookup must not lose the order -- it is already submitted.
            return True

    def _handle_submit_orders_batch(self, params):
        orders = params.get("orders") or []
        if not isinstance(orders, list) or not orders:
            raise ValueError("orders must be a non-empty list")
        if len(orders) > 500:
            raise ValueError("orders exceeds batch limit 500")
        batch_started = time.time()
        batch_id = str(params.get("batch_id") or uuid.uuid4().hex)
        account_id = self._request_account_id(params)
        strategy_name = str(
            params.get("strategy_name")
            or (orders[0] or {}).get("strategy_name")
            or self.default_strategy_name
        )
        # order_stock_async routes a queued backlog through here (#181), but it
        # never promised order_stock_batch's idempotency contract, and
        # inheriting it swallowed orders while answering success (#190):
        # no remark is normal for async (the single path invents
        # "bqrpc:<uuid>"), and repeating one is legal there. Under this batch
        # endpoint the first meant ORDER_TAG_REQUIRED and placed nothing, the
        # second meant "already submitted" and placed one of N.
        #
        # Default True so deliberate order_stock_batch callers -- who supply
        # tags precisely to be protected against a retry double-ordering --
        # keep exactly today's behaviour. Only a caller that opts out gets the
        # single path's semantics.
        idempotent = params.get("idempotent")
        idempotent = True if idempotent is None else bool(idempotent)
        existing_by_tag = {}
        lookup_ok = True
        requires_lookup = idempotent and any(
            bool((item or {}).get("require_idempotency_check")) for item in orders)
        if requires_lookup:
            try:
                identity_query = getattr(self.order_gateway, "query_submission_identities_strict", None)
                if callable(identity_query):
                    existing, trades = identity_query(account_id, strategy_name)
                else:
                    query = getattr(self.order_gateway, "query_orders_strict", None)
                    existing = query(account_id, strategy_name) if callable(query) else self.order_gateway.query_orders(account_id, strategy_name)
                    trades = []
                existing_by_tag = {
                    str(getattr(order, "user_order_id", "") or ""): order
                    for order in existing or []
                    if str(getattr(order, "user_order_id", "") or "")
                }
                for trade in trades or []:
                    tag = str(getattr(trade, "user_order_id", "") or "")
                    if tag and tag not in existing_by_tag:
                        existing_by_tag[tag] = trade
            except Exception:
                lookup_ok = False
        results = []
        for index, item in enumerate(orders):
            item = dict(item or {})
            # No per-item settlement, and not negotiable per item (#181).
            #
            # _handle_submit_order defaults wait_settlement to True and parks
            # the settlement in a SINGLE slot; the service takes that slot once
            # per request. N orders in one request therefore overwrite each
            # other and only the last survives -- carrying the batch's own
            # request/response, so the whole batch reply gets held until that
            # one order settles or times out, and that one order's diagnostic
            # ("order not found in system", naming one stock, one price, one
            # volume) is attached to a reply covering all of them. The
            # order_sys_id it back-fills is read by nobody: each result dict is
            # built from the submit result before any settlement runs.
            #
            # Nothing is lost by skipping it. The batch answers per item
            # already, and order_sys_id arrives on the order_callback push the
            # same way it does for order_stock_async (#50). Honouring an
            # explicit wait_settlement per item would need a settlement queue
            # keyed to one reply, which is the "把单槽改成队列" half of #181.
            item["wait_settlement"] = False
            order_tag = str(item.get("order_remark") or item.get("remark") or item.get("signal_id") or "")
            if not order_tag and not idempotent:
                # Same invention as _handle_submit_order, so an async order
                # with no remark reaches the broker instead of being rejected.
                # Only the signal_id is set: _handle_submit_order derives the
                # remark from it as "bqrpc:rpc-<hex>", and pre-building the
                # remark here would double the prefix and change the string QMT
                # shows in 备注 (which #152's settlement lookup matches on).
                item.setdefault("signal_id", "rpc-%s" % uuid.uuid4().hex)
                order_tag = "bqrpc:%s" % item["signal_id"]
            if not order_tag:
                results.append({
                    "index": index,
                    "batch_id": batch_id,
                    "success": False,
                    "accepted": False,
                    "explicit_failure": True,
                    "code": -3,
                    "error": "ORDER_TAG_REQUIRED",
                    "user_order_id": "",
                })
                continue
            known = existing_by_tag.get(order_tag) if idempotent else None
            journal_key = (account_id, strategy_name, order_tag)
            journal = self._submit_journal.get(journal_key) if idempotent else None
            if known is not None or journal is not None:
                results.append({
                    "index": index,
                    "batch_id": batch_id,
                    "success": True,
                    "accepted": True,
                    "confirmed": known is not None,
                    "idempotent": True,
                    "code": 0,
                    "order_sys_id": str(getattr(known, "order_sys_id", "") or (journal or {}).get("order_sys_id") or ""),
                    "user_order_id": order_tag,
                })
                continue
            if bool(item.get("require_idempotency_check")) and not lookup_ok:
                results.append({
                    "index": index,
                    "batch_id": batch_id,
                    "success": False,
                    "accepted": False,
                    "explicit_failure": False,
                    "code": -2,
                    "error": "IDEMPOTENCY_CHECK_UNAVAILABLE",
                    "user_order_id": order_tag,
                })
                continue
            try:
                result = self._handle_submit_order(item)
                response = {
                    "index": index,
                    "batch_id": batch_id,
                    "success": True,
                    "accepted": True,
                    "confirmed": False,
                    "idempotent": False,
                    "code": 0,
                    "order_sys_id": str(getattr(result, "order_sys_id", None) or ""),
                    "user_order_id": str(getattr(result, "user_order_id", None) or ""),
                }
                # Not journalled when the caller opted out: those tags are not
                # identities (async may repeat a remark for genuinely different
                # orders), so recording them would let one suppress a later
                # deliberate order_stock_batch item that reuses the string.
                if order_tag and idempotent:
                    self._submit_journal[journal_key] = dict(response)
                results.append(response)
            except Exception as exc:
                results.append({
                    "index": index,
                    "batch_id": batch_id,
                    "success": False,
                    "accepted": False,
                    "explicit_failure": True,
                    "code": -1,
                    "error": "%s: %s" % (exc.__class__.__name__, exc),
                    "user_order_id": order_tag,
                })
        # One line per batch, always: a batch that outlives the client's
        # timeout keeps running to completion here, and without this line the
        # doubled orders that follow have no server-side trace.
        try:
            from .logging_setup import get_logger
            get_logger("rpc").info(
                "batch submit account=%s items=%d placed=%d failed=%d %.0fms",
                account_id, len(orders),
                sum(1 for r in results if r.get("success") and not r.get("idempotent")),
                sum(1 for r in results if not r.get("success")),
                (time.time() - batch_started) * 1000.0)
        except Exception:
            pass
        return results

    def _handle_passorder(self, params):
        """Raw native-passorder passthrough (route 2).

        Forwards the 11-arg passorder signature straight through to QMT, with
        ContextInfo supplied server-side. No opType validation, no code
        normalization, no settlement read-back -- the caller owns the native
        contract. See BigQmtOrderGateway.passorder_passthrough.

        Accepts both the native argument names (op_type/order_type/price_type/
        prType...) and passorder's own spellings, so a caller can paste the
        signature they know. account defaults to the request/gateway account.
        """
        if self.order_gateway is None:
            raise RuntimeError("order_gateway is not configured")
        passthrough = getattr(self.order_gateway, "passorder_passthrough", None)
        if not callable(passthrough):
            raise RuntimeError("this deployment predates the passorder passthrough")

        def pick(*names, **kw):
            for name in names:
                if name in params and params[name] is not None:
                    return params[name]
            return kw.get("default")

        op_type = pick("op_type", "opType", "optype")
        order_type = pick("order_type", "orderType", "ordertype",
                          default=self.order_gateway.combo_type)
        account_id = self._request_account_id(params)
        order_code = pick("order_code", "orderCode", "stock_code", "code")
        price_type = pick("price_type", "prType", "prtype",
                          default=self.order_gateway.price_type)
        price = pick("price", default=-1)
        volume = pick("volume", "order_volume", "vol")
        strategy_name = pick("strategy_name", "strategyName",
                             default=self.default_strategy_name)
        quick_trade = pick("quick_trade", "quickTrade",
                           default=self.order_gateway.quick_trade)
        user_order_id = pick("user_order_id", "userOrderId", "order_remark",
                             "remark", default="")
        if op_type is None:
            raise ValueError("op_type is required")
        if not order_code:
            raise ValueError("order_code is required")
        if volume is None:
            raise ValueError("volume is required")
        return passthrough(
            op_type=op_type,
            order_type=order_type,
            account_id=account_id,
            order_code=order_code,
            price_type=price_type,
            price=price,
            volume=volume,
            strategy_name=strategy_name,
            quick_trade=quick_trade,
            user_order_id=user_order_id,
            dry_run=bool(pick("dry_run", "dryRun", default=False)),
        )

    def _handle_cancel_order(self, params):
        if self.order_gateway is None:
            raise RuntimeError("order_gateway is not configured")
        account_id = self._request_account_id(params)
        order_sys_id = str(params.get("order_sys_id") or params.get("order_sysid") or params.get("order_id") or "")
        if not order_sys_id:
            raise ValueError("order_sys_id or order_id is required")
        order_ref = OrderRef(
            order_sys_id=order_sys_id,
            user_order_id=str(params.get("user_order_id") or ""),
        )
        result = self.order_gateway.cancel(order_ref, account_id=account_id)

        # The native cancel return is not trustworthy in EITHER direction.
        # #148: falsey while the broker accepted the cancel (status became 54
        # within 67 ms).  #151: truthy for an order that does not exist at
        # all -- the return describes "the request was sent", not "the order
        # was cancelled".  So both directions settle against the order
        # snapshot now.  A truthy return gets ONE immediate lookup first: the
        # common case (order exists, already 53/54) confirms without an extra
        # round trip, so the fast path stays fast; only an unconfirmed truthy
        # pays the settle wait.
        settlement = CancelSettlement(
            order_ref,
            account_id,
            result,
            _monotonic() + self.order_settle_timeout_seconds,
        )
        if getattr(result, "success", None) is not False:
            try:
                if self._apply_cancel_lookup(settlement):
                    return result
            except Exception:
                pass  # fall through to the parked/inline wait below
        if self.settle_orders_inline:
            try:
                import time as _time
                _time.sleep(self.order_settle_timeout_seconds)
                self._apply_cancel_lookup(settlement, final=True)
            except Exception:
                pass
        else:
            self._pending_settlement = settlement
        return result

    def _handle_cancel_orders_batch(self, params):
        """Cancel N orders in one RPC.

        Settlement lookups are skipped (same reason as submit_orders_batch:
        get_trade_detail_data is empty off the adjust thread). The cancel
        callback on the client confirms the outcome asynchronously.
        """
        if self.order_gateway is None:
            raise RuntimeError("order_gateway is not configured")
        items = params.get("items") or []
        if not isinstance(items, list) or not items:
            raise ValueError("items must be a non-empty list")
        if len(items) > 500:
            raise ValueError("items exceeds batch limit 500")
        results = []
        for index, item in enumerate(items):
            item = dict(item or {})
            account_id = item.get("account_id") or self._request_account_id(params)
            order_sys_id = str(
                item.get("order_sysid")
                or item.get("order_sys_id")
                or item.get("order_id")
                or ""
            )
            market = str(item.get("market") or "")
            # accepted/confirmed mirror the submit batch's vocabulary: the
            # native cancel return answers "the request went out", not "the
            # order is cancelled" -- and it lies in BOTH directions live
            # (#148: false while the order was cancelled 67ms later; #151:
            # true for an order that did not exist). The single-cancel path
            # settles against the order snapshot; this batch path skips that
            # (it is the latency the batch exists to avoid), so nothing here
            # may claim "cancelled". The confirmation is the order-status push
            # (54) or a query read-back.
            entry = {"index": index, "success": False,
                     "accepted": False, "confirmed": False}
            if not order_sys_id:
                entry["error"] = "order_sysid is required"
                results.append(entry)
                continue
            order_ref = OrderRef(
                order_sys_id=order_sys_id,
                user_order_id=str(item.get("user_order_id") or ""),
            )
            try:
                result = self.order_gateway.cancel(order_ref, account_id=account_id)
                entry["success"] = bool(getattr(result, "success", result))
                entry["accepted"] = True
                entry["message"] = str(getattr(result, "message", "") or "")
            except Exception as exc:
                entry["error"] = "%s: %s" % (type(exc).__name__, exc)
            results.append(entry)
        return results

    def _settle_cancel_from_status(self, settlement, status, order_sys_id, final):
        """One status answer, from the watch table or the snapshot row."""
        if status in CANCELED_ORDER_STATUSES:
            settlement.result.success = True
            settlement.result.message = ""
            return True
        if status in TERMINAL_NON_CANCEL_ORDER_STATUSES:
            settlement.result.success = False
            settlement.result.message = (
                "cancel was not confirmed: order %s reached status %s"
                % (order_sys_id, status)
            )
            return True
        if status in CANCEL_IN_FLIGHT_STATUSES:
            if not final:
                return False
            # The exchange has accepted the cancel and it is on its way --
            # 51/52 transition to 54 in milliseconds normally, slower around
            # the close or under congestion. That is not a failed cancel, and
            # reporting one is the #148 false negative through a narrower
            # window (issue #151).
            settlement.result.success = True
            settlement.result.message = (
                "cancel accepted by exchange, still in flight: order %s is status %s"
                % (order_sys_id, status)
            )
            return True
        if not final:
            return False
        settlement.result.success = False
        settlement.result.message = (
            "cancel was not confirmed after %d lookup(s): order %s is still status %s"
            % (settlement.attempts, order_sys_id, status or "unknown")
        )
        return True

    def _apply_cancel_lookup(self, settlement, final=False):
        """Resolve an ambiguous native cancel return from the order snapshot."""
        settlement.attempts += 1
        order_sys_id = str(settlement.order_ref.order_sys_id or "")
        # Fast path: the order's own status change was pushed to us by QMT's
        # order_callback (issue #164). A table miss falls through to the poll.
        watch = getattr(self, "order_watch_table", None)
        if watch is not None:
            try:
                watched = watch.status_for_sysid(order_sys_id)
            except Exception:
                watched = None
            if watched is not None:
                return self._settle_cancel_from_status(
                    settlement, str(watched), order_sys_id, final)
        try:
            strict_query = getattr(self.order_gateway, "query_orders_strict", None)
            if callable(strict_query):
                orders = strict_query(settlement.account_id, "") or []
            else:
                orders = self.order_gateway.query_orders(settlement.account_id, "") or []
        except Exception as exc:
            if not final:
                return False
            settlement.result.success = False
            settlement.result.message = (
                "cancel status lookup failed after %d attempt(s): %s: %s"
                % (settlement.attempts, exc.__class__.__name__, exc)
            )
            return True

        matches = [
            order for order in orders
            if str(getattr(order, "order_sys_id", "") or "") == order_sys_id
        ]
        if not matches:
            if not final:
                return False
            settlement.result.success = False
            settlement.result.message = (
                "cancel was not confirmed: order %s was not found after %d lookup(s)"
                % (order_sys_id, settlement.attempts)
            )
            return True

        status = str(getattr(matches[0], "status", "") or "")
        return self._settle_cancel_from_status(
            settlement, status, order_sys_id, final)


def _bool_value(value, default=False):
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on")


def _credit_action_of(order_type):
    try:
        from bigqmt_signal_trader.adapters.order_bigqmt import credit_action_of

        return credit_action_of(order_type)
    except Exception:
        return None


def _credit_optype_of(order_type):
    try:
        from bigqmt_signal_trader.adapters.order_bigqmt import credit_optype_of

        return credit_optype_of(order_type)
    except Exception:
        return None


def _passthrough_optype_of(order_type):
    try:
        from bigqmt_signal_trader.adapters.order_bigqmt import passthrough_optype_of

        return passthrough_optype_of(order_type)
    except Exception:
        return None


def _passthrough_action_of(order_type):
    try:
        from bigqmt_signal_trader.adapters.order_bigqmt import passthrough_action_of

        return passthrough_action_of(order_type)
    except Exception:
        return None


def _deployed_version():
    """Version of the bridge actually running here, or "" if unknown."""
    try:
        from bigqmt_signal_trader.version import __version__

        return __version__
    except Exception:
        return ""


def _normalize_mapping_value(value):
    """Make one mapping value JSON-safe without flattening its shape."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return dict((str(k), _normalize_mapping_value(v)) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return [_normalize_mapping_value(v) for v in value]
    rows = _normalize_detail_rows([value])
    return rows[0] if rows else {}


def _normalize_detail_rows(rows):
    """Convert get_trade_detail_data row objects into JSON-serializable dicts.

    QMT returns objects with m_strXxx / m_nXxx / m_dXxx attributes. We map
    each to its public attributes so the result survives JSON encoding.
    """
    if not rows:
        return []
    result = []
    for row in rows:
        if isinstance(row, dict):
            result.append(row)
            continue
        item = {}
        for name in dir(row):
            if name.startswith("_"):
                continue
            # QMT 的委托对象上除了 60 个 m_* 字段，还挂着 60 个枚举常量，
            # 名字是 '0' / '-1' / '101' 这种数字串（值就是常量本身）。dir()
            # 一并刮进来会让载荷翻倍、还夹着看不懂的键。非标识符的名字一律
            # 丢掉 —— 真字段（m_xxx / 普通属性名）都是合法标识符，不会误伤。
            if not name.isidentifier():
                continue
            try:
                value = getattr(row, name)
            except Exception:
                continue
            if callable(value):
                continue
            item[name] = value
        result.append(item)
    return result


# A response carries typed envelopes (DataFrame/Series/Panel) only sometimes.
# A whole-market snapshot is ~20 MB of plain scalars with none in it, and
# walking that tree on the client to rebuild nothing costs 341ms -- more than
# parsing it did. Checking the raw text for the marker is a C substring scan
# over the same bytes: 3.7ms. So the answer rides along on the envelope and the
# client skips the walk when there is provably nothing to restore. Measured on
# 51285 instruments: 345.9ms -> 3.7ms.
TYPED_PAYLOAD_MARKER = '"__bigqmt_type__"'
TYPED_PAYLOAD_FLAG = "__bigqmt_typed__"


def loads_rpc_response(raw):
    """Parse a response envelope and record whether it carries typed data."""
    text = decode_text(raw)
    response = json.loads(text)
    if isinstance(response, dict):
        response[TYPED_PAYLOAD_FLAG] = TYPED_PAYLOAD_MARKER in text
    return response


def encode_rpc_request_payload(request):
    """Encode request JSON so patched QMT Redis clients do not inspect stock-code text."""

    raw = json.dumps(request, ensure_ascii=False).encode("utf-8")
    encoded = base64.b64encode(raw).decode("ascii").translate(SAFE_B64_DIGIT_ENCODE)
    return SAFE_B64_PREFIX + encoded


def decode_rpc_request_payload(text):
    text = str(text)
    if not text.startswith(SAFE_B64_PREFIX):
        return text
    encoded = text[len(SAFE_B64_PREFIX):].translate(SAFE_B64_DIGIT_DECODE)
    return base64.b64decode(encoded.encode("ascii")).decode("utf-8")


class RedisPubSubRpcService:
    """Receive RPC requests from Redis and write responses back to Redis."""

    def __init__(
        self,
        redis_client,
        handlers,
        account_id="",
        response_redis_client=None,
        request_channel_template="bigqmt:rpc:req:{account_id}",
        request_queue_template="bigqmt:rpc:queue:{account_id}",
        response_channel_template="bigqmt:rpc:resp:{account_id}:{request_id}",
        response_list_template="bigqmt:rpc:respq:{account_id}:{request_id}",
        response_key_template="bigqmt:rpc:resp:{account_id}:{request_id}",
        response_ttl_seconds=60,
        max_queue_size=200,
        process_in_listener=False,
        listener_methods=None,
        background_threads=True,
        queue_poll_interval_seconds=0.02,
        debug_log_limit=0,
        print_prefix="[bigqmt_rpc]",
        transport=None,
    ):
        self.listen_redis = redis_client
        self.redis = response_redis_client or redis_client
        self.handlers = handlers
        self.account_id = str(account_id or "")
        self.request_channel_template = request_channel_template
        self.request_queue_template = request_queue_template
        self.response_channel_template = response_channel_template
        self.response_list_template = response_list_template
        self.response_key_template = response_key_template
        self.response_ttl_seconds = int(response_ttl_seconds)
        self.process_in_listener = bool(process_in_listener)
        self.background_threads = bool(background_threads)
        if listener_methods is None:
            listener_methods = ("ping",)
        self.listener_methods = self._expand_listener_methods(listener_methods)
        self.queue_poll_interval_seconds = max(0.001, float(queue_poll_interval_seconds))
        self.debug_log_limit = int(debug_log_limit)
        self._received_count = 0
        self._processed_count = 0
        # (account_id, request_id) -> {"at", "response"} for write methods,
        # so a transparently retried RPUSH cannot dispatch a second order (#245).
        self._order_requests = collections.OrderedDict()
        self._duplicate_order_requests = 0
        self._published_count = 0
        self._deferred_count = 0
        self.print_prefix = print_prefix
        self.pending = queue.Queue(maxsize=int(max_queue_size))
        # Submit/cancel replies waiting on a main-thread order snapshot.
        # Unbounded on purpose: every entry represents a live broker operation,
        # so dropping one would strand it with no reply.
        self._pending_settlements = queue.Queue()
        self._running = threading.Event()
        self._thread = None
        self._queue_thread = None
        self._pubsub = None
        # Transport owns the wire. Default to a RedisTransport built from the
        # same clients/templates so behavior is unchanged. An explicit
        # ``transport`` (e.g. ZmqTransport) overrides the Redis path entirely.
        if transport is None:
            from .transports.redis_transport import RedisTransport

            transport = RedisTransport(
                redis_client,
                account_id=self.account_id,
                response_redis_client=response_redis_client,
                request_channel_template=request_channel_template,
                request_queue_template=request_queue_template,
                response_channel_template=response_channel_template,
                response_list_template=response_list_template,
                response_key_template=response_key_template,
                response_ttl_seconds=response_ttl_seconds,
                queue_poll_interval_seconds=queue_poll_interval_seconds,
                debug_log_limit=debug_log_limit,
                print_prefix=print_prefix,
            )
        self._transport = transport
        # Let the handlers reach the transport for read-only diagnostics
        # (reply-queue residency, #104). Set here rather than in the
        # strategy file so a reload picks it up without a restart.
        try:
            self.handlers.rpc_transport = transport
        except Exception:
            pass
        # 同理，让 handlers 能报出「这个方法会跑在哪条线程上」。#204 查了半天
        # 才发现症结是线程路由，而远端报告里恰恰看不到这个 —— 一个只能远程
        # 诊断的部署，把决定行为的开关藏起来就等于让人猜。
        try:
            self.handlers.rpc_service = self
        except Exception:
            pass
        # Route inbound raw payloads through the service's dispatch (which
        # applies the inline-vs-deferred fork) instead of transport.deliver().
        self._transport.on_raw_payload = self._handle_received_payload

    @property
    def request_channel(self):
        return self.request_channel_template.format(account_id=self.account_id)

    @property
    def request_queue(self):
        return self.request_queue_template.format(account_id=self.account_id)

    def start(self):
        self._running.set()
        # Delegate thread lifecycle to the transport. The transport invokes the
        # on_request callback with a decoded request dict; enqueue_payload routes
        # it through the inline-vs-deferred fork and publishes the response
        # itself (returns None so the transport's deliver() does not double-send).
        # RedisTransport additionally routes raw bytes through on_raw_payload
        # (set in __init__) for its own receive loops.
        self._transport.start_receiving(
            self.enqueue_payload,
            background_threads=self.background_threads,
        )
        # Mirror transport threads onto the service for stop()/diagnostics.
        self._thread = getattr(self._transport, "_thread", None)
        self._queue_thread = getattr(self._transport, "_queue_thread", None)
        if not self.background_threads:
            print("%s started queue=%s background_threads=False" % (self.print_prefix, self.request_queue))
            return
        print("%s started channel=%s queue=%s" % (self.print_prefix, self.request_channel, self.request_queue))

    def stop(self):
        self._running.clear()
        try:
            self._transport.stop()
        except Exception:
            pass
        # The transport owns the threads now; keep the attributes for back-compat.
        self._thread = None
        self._queue_thread = None
        self._pubsub = None

    def _listen_loop(self):
        while self._running.is_set():
            try:
                pubsub = self.listen_redis.pubsub(ignore_subscribe_messages=True)
                self._pubsub = pubsub
                pubsub.subscribe(self.request_channel)
                if self.debug_log_limit > 0:
                    print("%s subscribed channel=%s" % (self.print_prefix, self.request_channel))
                while self._running.is_set():
                    message = pubsub.get_message(timeout=1.0)
                    if not self._running.is_set():
                        break
                    if not message or message.get("type") != "message":
                        continue
                    self._handle_received_payload(message.get("data"), "pubsub")
            except Exception:
                print("%s listener failed:\n%s" % (self.print_prefix, traceback.format_exc()))
                time.sleep(1.0)
            finally:
                try:
                    if self._pubsub is not None:
                        self._pubsub.close()
                except Exception:
                    pass
                self._pubsub = None

    def _queue_loop(self):
        while self._running.is_set():
            try:
                if self.debug_log_limit > 0:
                    print("%s queue polling key=%s" % (self.print_prefix, self.request_queue))
                while self._running.is_set():
                    # brpop blocks server-side until an item arrives (or the
                    # short timeout fires), so a request is picked up within
                    # ~1ms of being pushed instead of waiting up to
                    # queue_poll_interval_seconds. The 1s ceiling lets us
                    # re-check _running for a clean shutdown.
                    item = self.listen_redis.brpop(self.request_queue, timeout=1)
                    if not self._running.is_set():
                        break
                    if not item:
                        continue
                    raw = item[1] if isinstance(item, (list, tuple)) and len(item) >= 2 else item
                    self._handle_received_payload(raw, "queue")
            except Exception:
                print("%s queue listener failed:\n%s" % (self.print_prefix, traceback.format_exc()))
                time.sleep(1.0)

    def _handle_received_payload(self, raw_payload, source):
        self._received_count += 1
        if self._received_count <= self.debug_log_limit:
            try:
                preview = self._loads(raw_payload)
                method = str(preview.get("method") or "")
                print(
                    "%s received source=%s method=%s inline=%s"
                    % (self.print_prefix, source, method, self._should_process_in_listener(preview))
                )
                self.enqueue_payload(preview)
                return
            except Exception:
                print("%s receive preview failed:\n%s" % (self.print_prefix, traceback.format_exc()))
        self.enqueue_payload(raw_payload)

    def enqueue_payload(self, raw_payload):
        payload = self._loads(raw_payload)
        if self._should_process_in_listener(payload):
            self.process_request(payload)
            return
        self._deferred_count += 1
        if self._deferred_count <= self.debug_log_limit:
            print(
                "%s deferred method=%s pending_before=%s"
                % (self.print_prefix, payload.get("method"), self.pending.qsize())
            )
        try:
            self.pending.put_nowait(payload)
        except queue.Full:
            # A full pending queue (client polling storm) must not raise into the
            # adjust thread — QMT stops the strategy on a callback raise. Drop the
            # oldest request and keep the newest instead of crashing.
            try:
                self.pending.get_nowait()
            except Exception:
                pass
            try:
                self.pending.put_nowait(payload)
            except Exception:
                pass

    def _should_process_in_listener(self, payload):
        if not self.process_in_listener:
            return False
        method = str((payload or {}).get("method") or "")
        canonical = getattr(self.handlers, "_canonical_method", lambda value: value)(method)
        # Second gate, on the per-request path rather than the config path: a
        # trade-context method never runs on the receive thread, whatever
        # listener_methods happens to contain (#252). The expansion below is a
        # one-off at construction; this runs for every request, so a spelling
        # nobody thought of cannot route get_trade_detail_data off the main
        # strategy thread -- where it answers with the right row count and
        # every field None, which a client reads as "no money", not as a
        # failed call.
        if method in LISTENER_DEFERRED_METHODS or canonical in LISTENER_DEFERRED_METHODS:
            return False
        if method in self.listener_methods:
            return True
        return canonical in self.listener_methods

    def _expand_listener_methods(self, listener_methods):
        """Which methods may run inline on the receive thread.

        The deferred set is subtracted **last**, over the whole result, not
        only inside the ``"*"`` branch. Naming a method explicitly used to
        bypass the subtraction entirely, so
        ``rpc_listener_methods=("get_asset",)`` put a trade-context method
        back on the receive thread. That is harmless while the receive thread
        IS the adjust thread (rpc_background_threads False), and silently
        wrong the moment it is not: off the main strategy thread these answer
        with the right row count and every field None -- which reads as "this
        account has no money", not as a failed call.

        Making it depend on two unrelated keys agreeing is what kept
        ``rpc_background_threads`` pinned to False as a blanket rule. Enforce
        it here instead: no configuration can route a trade-context method to
        a background thread, so the flag is free to be chosen on latency.
        """
        resolve = getattr(self.handlers, "_canonical_method", lambda value: value)
        methods = set()
        for method in listener_methods or ():
            method = str(method)
            if method in ("*", "all", "read", "readonly"):
                methods.update(READ_METHODS)
            else:
                methods.add(method)
                methods.add(resolve(method))
        # Subtract by CANONICAL name, not by the literal spelling. The loop
        # above keeps the caller's alias alongside the canonical name, so a
        # plain `methods - LISTENER_DEFERRED_METHODS` removed `get_positions`
        # and left `query_stock_positions` sitting right next to it -- and
        # `_should_process_in_listener` matches the raw method name first, so
        # the alias won (#252). `("get_positions",)` and `("*",)` were correct
        # throughout, which is what made this read as "the config spelling
        # changes the answer" rather than as a dispatch bug.
        return set(m for m in methods if resolve(m) not in LISTENER_DEFERRED_METHODS)

    def _loads(self, raw_payload):
        if isinstance(raw_payload, dict):
            return dict(raw_payload)
        text = decode_text(raw_payload)
        text = decode_rpc_request_payload(text)
        payload = json.loads(text)
        if not isinstance(payload, dict):
            raise ValueError("rpc payload must be a json object")
        return payload

    def settle_pending_orders(self, max_items=100):
        """Retry parked submit/cancel lookups on the adjust thread.

        A queue rather than a list because rpc_listener_methods is configurable:
        if submit_order is ever put in it, the producer becomes the listener
        thread while this consumer stays on adjust.

        Unsettled entries go back on the queue, so each order costs one lookup
        per adjust tick until it resolves or its deadline passes.
        """
        settled = 0
        # Snapshot the size first. Unsettled entries go back on the same queue,
        # so draining until empty would keep re-picking them and spin one adjust
        # tick into many lookups per order.
        batch = min(int(max_items), self._pending_settlements.qsize())
        for _ in range(batch):
            try:
                settlement = self._pending_settlements.get_nowait()
            except queue.Empty:
                break
            expired = _monotonic() >= settlement.deadline
            try:
                if isinstance(settlement, CancelSettlement):
                    done = self.handlers._apply_cancel_lookup(settlement, final=expired)
                else:
                    done = self.handlers._apply_order_lookup(settlement, final=expired)
            except Exception:
                done = True  # never strand a submitted order in the queue
            if not done:
                self._pending_settlements.put(settlement)
                continue
            response = settlement.response
            response["data"] = to_jsonable(settlement.result)
            response["ok"] = True
            if getattr(settlement, "server_error", ""):
                response["server_error"] = settlement.server_error
            response["handled_at"] = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            try:
                self._publish_response(settlement.request, response)
            except Exception:
                pass
            settled += 1
        return settled

    def pending_settlement_count(self):
        return self._pending_settlements.qsize()

    def drain_pending(self, max_items=20):
        # Settle carry-overs from earlier ticks before taking on new work.
        self.settle_pending_orders()
        processed = 0
        for _ in range(int(max_items)):
            try:
                request = self.pending.get_nowait()
            except queue.Empty:
                break
            if self._processed_count < self.debug_log_limit:
                print(
                    "%s draining method=%s pending_after=%s"
                    % (self.print_prefix, request.get("method"), self.pending.qsize())
                )
            self.process_request(request)
            processed += 1
        # Settle again so an order submitted in THIS drain still replies on this
        # tick. One lookup, no sleep -- if QMT has not assigned the id yet we
        # simply retry next tick rather than holding the thread (issue #44).
        self.settle_pending_orders()
        return processed

    def drain_request_queue(self, max_items=20):
        # Delegate to the transport when it owns the wire directly; for Redis
        # the transport's drain drives _handle_received_payload (which honors
        # the inline-vs-deferred fork), matching the original semantics.
        transport_drain = getattr(self._transport, "drain_request_queue", None)
        if transport_drain is not None and not isinstance(self._transport, type(None)):
            return transport_drain(max_items=max_items)
        processed = 0
        for _ in range(int(max_items)):
            item = self.listen_redis.lpop(self.request_queue)
            if not item:
                break
            self.process_request(self._loads(item))
            processed += 1
        return processed

    ORDER_DEDUP_MAX = 512
    ORDER_DEDUP_TTL_SECONDS = 600

    def _canonical(self, method):
        resolve = getattr(self.handlers, "_canonical_method", None)
        return resolve(method) if callable(resolve) else method

    def _claim_order_request(self, key):
        """Claim (account_id, request_id) for a write. None == first time.

        Returns the existing entry when this id has already been seen, so the
        caller can answer without dispatching a second native order.
        """
        store = getattr(self, "_order_requests", None)
        if store is None:
            store = self._order_requests = collections.OrderedDict()
        now = time.time()
        # Prune by age, then by size. Both are bounded on purpose: this runs
        # on the adjust thread and must not grow with uptime.
        for stale in [k for k, v in store.items()
                      if now - v.get("at", now) > self.ORDER_DEDUP_TTL_SECONDS]:
            store.pop(stale, None)
        while len(store) > self.ORDER_DEDUP_MAX:
            store.popitem(last=False)
        existing = store.get(key)
        if existing is not None:
            return existing
        store[key] = {"at": now, "response": None}
        return None

    def _remember_order_response(self, key, response):
        store = getattr(self, "_order_requests", None)
        if store is not None and key in store:
            store[key]["response"] = response

    def process_request(self, request):
        request = dict(request or {})
        request_id = str(request.get("request_id") or request.get("id") or uuid.uuid4().hex)
        account_id = str(request.get("account_id") or self.account_id or "")
        method = str(request.get("method") or "")

        # At-most-once for writes (#245). The client's redis-py connection
        # retries transparently on ConnectionError/TimeoutError, and the retry
        # re-sends the whole command -- conn.retry wraps send AND parse, so an
        # RPUSH the server already accepted is sent again when the reply is
        # lost. Nothing downstream deduped: order_stock / order_stock_async
        # both alias to submit_order, and _handle_submit_order has no
        # idempotency check (only submit_orders_batch does). So one
        # client.call could dispatch two native passorders.
        #
        # The retry re-sends the identical payload, request_id included, which
        # is what makes this fixable here: the second copy is recognisable.
        # Answer it with the first one's response instead of dispatching --
        # both copies name the same reply key, so it lands where the client is
        # already waiting.
        order_key = None
        if request_id and self._canonical(method) in ORDER_METHODS:
            order_key = (account_id, request_id)
            seen = self._claim_order_request(order_key)
            if seen is not None:
                self._duplicate_order_requests += 1
                remembered = seen.get("response")
                print("%s duplicate order request suppressed method=%s request_id=%s"
                      % (self.print_prefix, method, request_id))
                if remembered is None:
                    # Still in flight: the first copy publishes to the same
                    # reply key when it settles. Nothing to do but not dispatch.
                    return None
                try:
                    self._publish_response(request, remembered)
                except Exception:
                    pass
                return remembered
        response = {
            "schema_version": 1,
            "request_id": request_id,
            "account_id": account_id,
            "method": method,
            "ok": False,
            "data": None,
            "error": "",
            # server_error carries QMT-side diagnostic info (e.g. passorder
            # submitted but order not found in system, get_trade_detail_data
            # returned empty) that doesn't raise an exception but indicates a
            # problem. Lets clients see why an operation silently failed.
            "server_error": "",
            "handled_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            # Wall clock, not perf_counter: the client is a separate process
            # on the same machine, so these are directly comparable to its own
            # time.time() and decompose the round trip into wait / handle /
            # return without guessing (#104).
            "_t_recv": time.time(),
        }
        try:
            if self.account_id and account_id and account_id != self.account_id:
                raise PermissionError("account_id mismatch")
            _t0 = time.perf_counter() if method == "ping" else 0.0
            result = self.handlers.handle(method, request.get("params") or {})
            _t1 = time.perf_counter() if method == "ping" else 0.0
            response["data"] = to_jsonable(result)
            response["ok"] = True
            # Surface server-side diagnostics when the handler recorded one.
            server_error = getattr(self.handlers, "_last_server_error", None)
            if server_error:
                response["server_error"] = str(server_error)
            # passorder may still be awaiting its asynchronously assigned id;
            # a falsey native cancel may still be awaiting a reliable terminal
            # status (#148). Park either reply instead of sleeping on this
            # thread; a later adjust tick settles and publishes it (#44).
            take = getattr(self.handlers, "take_pending_settlement", None)
            settlement = take() if callable(take) else None
            if settlement is not None:
                settlement.request = request
                settlement.response = response
                self._pending_settlements.put(settlement)
                self._deferred_count += 1
                if order_key is not None:
                    self._remember_order_response(order_key, response)
                return response
        except Exception as exc:
            response["error"] = "%s: %s" % (exc.__class__.__name__, exc)
        response["_t_reply"] = time.time()
        if order_key is not None:
            self._remember_order_response(order_key, response)
        _t_pub0 = time.perf_counter() if method == "ping" else 0.0
        try:
            self._publish_response(request, response)
        except Exception:
            # A response-publish failure (e.g. redis outage) must not propagate
            # to the adjust thread — QMT stops the strategy on a callback raise.
            # The request already ran; the client will just see a timeout.
            import traceback as _tb
            try:
                from .logging_setup import get_logger
                get_logger("rpc").error(
                    "publish response failed method=%s:\n%s", method, _tb.format_exc()
                )
            except Exception:
                pass
        if method == "ping":
            # Logged AFTER publish: this print goes to the QMT output panel,
            # which costs ~1 adjust tick of GIL wait on the serving thread --
            # between the handler and the send it inflated ping's round trip
            # by ~100ms and made the breakdown lie (#104).
            try:
                from .logging_setup import get_logger
                get_logger("rpc").info(
                    "ping breakdown handle=%.1fms jsonable=%.1fms publish=%.1fms",
                    (_t1 - _t0) * 1000.0,
                    (_t_pub0 - _t1) * 1000.0,
                    (time.perf_counter() - _t_pub0) * 1000.0)
            except Exception:
                pass
        self._processed_count += 1
        if self._processed_count <= self.debug_log_limit:
            print("%s responded method=%s ok=%s" % (self.print_prefix, method, response["ok"]))
        return response

    def _format_response_target(self, template, account_id, request_id):
        if not template:
            return ""
        return template.format(account_id=account_id, request_id=request_id)

    def _publish_response(self, request, response):
        # Delegate to the transport (RedisTransport fans out to key/list/channel;
        # ZMQ/MySQL transports use their native reply path).
        self._transport.send_response(request, response)
        self._published_count = getattr(self._transport, "_published_count", self._published_count)

    def _response_clients(self):
        clients = [self.redis]
        if self.listen_redis is not self.redis:
            clients.append(self.listen_redis)
        return clients

    def _write_response_key(self, response_key, ttl_seconds, payload):
        first_error = None
        wrote = 0
        for client in self._response_clients():
            try:
                if ttl_seconds > 0:
                    client.setex(response_key, ttl_seconds, payload)
                else:
                    client.set(response_key, payload)
                wrote += 1
            except Exception as exc:
                if first_error is None:
                    first_error = exc
        if wrote <= 0 and first_error is not None:
            raise first_error
        return wrote

    def _push_response_list(self, response_list, ttl_seconds, payload):
        first_error = None
        pushed = 0
        for client in self._response_clients():
            try:
                client.rpush(response_list, payload)
                if ttl_seconds > 0:
                    client.expire(response_list, ttl_seconds)
                pushed += 1
            except Exception as exc:
                if first_error is None:
                    first_error = exc
        if pushed <= 0 and first_error is not None:
            raise first_error
        return pushed

    def _publish_response_channel(self, response_channel, payload):
        first_error = None
        receivers = 0
        published = 0
        for client in self._response_clients():
            try:
                receivers += int(client.publish(response_channel, payload) or 0)
                published += 1
            except Exception as exc:
                if first_error is None:
                    first_error = exc
        if published <= 0 and first_error is not None:
            raise first_error
        self._published_count += 1
        if self._published_count <= self.debug_log_limit:
            print("%s published response receivers=%s" % (self.print_prefix, receivers))
        return receivers


def call_redis_rpc(
    redis_client,
    account_id,
    method,
    params=None,
    request_channel_template="bigqmt:rpc:req:{account_id}",
    request_queue_template="bigqmt:rpc:queue:{account_id}",
    response_channel_template="bigqmt:rpc:resp:{account_id}:{request_id}",
    response_list_template="bigqmt:rpc:respq:{account_id}:{request_id}",
    response_key_template="bigqmt:rpc:resp:{account_id}:{request_id}",
    timeout_seconds=3.0,
    ttl_seconds=60,
    transport="queue",
):
    """Small external client helper for tests and admin scripts."""

    request_id = uuid.uuid4().hex
    request_channel = request_channel_template.format(account_id=account_id)
    request_queue = request_queue_template.format(account_id=account_id)
    response_channel = response_channel_template.format(account_id=account_id, request_id=request_id)
    response_list = response_list_template.format(account_id=account_id, request_id=request_id)
    response_key = response_key_template.format(account_id=account_id, request_id=request_id)
    request = {
        "schema_version": 1,
        "request_id": request_id,
        "account_id": account_id,
        "method": method,
        "params": params or {},
        "reply_channel": response_channel,
        "reply_list": response_list,
        "reply_key": response_key,
        "ttl_seconds": ttl_seconds,
    }
    payload = encode_rpc_request_payload(request)
    if str(transport or "queue").lower() in ("queue", "list", "blpop"):
        redis_client.rpush(request_queue, payload)
        redis_client.expire(request_queue, max(60, int(ttl_seconds)))
        deadline = time.time() + float(timeout_seconds)
        while True:
            raw_response = redis_client.get(response_key)
            if raw_response:
                return loads_rpc_response(raw_response)
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            wait_timeout = max(1, int(min(remaining, 1.0) + 0.999))
            try:
                item = redis_client.blpop(response_list, timeout=wait_timeout)
            except Exception as exc:
                if _is_redis_timeout(exc):
                    continue
                raise
            if item:
                raw_response = item[1] if isinstance(item, (list, tuple)) and len(item) >= 2 else item
                try:
                    redis_client.delete(response_list)
                except Exception:
                    pass
                return loads_rpc_response(raw_response)
        raw_response = redis_client.get(response_key)
        if raw_response:
            return loads_rpc_response(raw_response)
        raise TimeoutError(
            "redis rpc timeout: %s account_id=%s request_queue=%s" % (method, account_id, request_queue)
        )

    pubsub = redis_client.pubsub(ignore_subscribe_messages=True)
    try:
        pubsub.subscribe(response_channel)
        redis_client.publish(request_channel, payload)
        deadline = time.time() + float(timeout_seconds)
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            message = pubsub.get_message(timeout=remaining)
            if not message or message.get("type") != "message":
                continue
            response = loads_rpc_response(message.get("data"))
            if response.get("request_id") == request_id:
                return response
        raw_response = redis_client.get(response_key)
        if raw_response:
            return loads_rpc_response(raw_response)
        raise TimeoutError("redis rpc timeout: %s" % method)
    finally:
        try:
            pubsub.close()
        except Exception:
            pass
