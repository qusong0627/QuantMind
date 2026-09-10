"""Big QMT market data adapter.

This module wraps two QMT runtime objects:

* ``ContextInfo`` — the strategy-scoped object exposed inside ``handlebar`` /
  ``init``. It carries methods that operate on the *current* subscribed context
  (``get_market_data_ex``, ``get_full_tick``, ``get_instrumentdetail`` ...).
* the **native xtdata SDK** (``bin.x64/Lib/site-packages/xtquant/xtdata.py``) —
  a module of global functions that talk to the local quote service directly.
  Some APIs only exist here, never as ContextInfo methods.

The split matters. Per the official docs and the ContextInfo IDE stub
(``_PyContextInfo.py``):

* ``get_sector_list`` / ``get_holidays`` are **xtdata module functions**
  (SDK xtdata.py lines 784 / 1197). They are *not* ContextInfo methods, so
  calling ``ContextInfo.get_sector_list()`` raises NotImplementedError.
* ``get_markets`` / ``get_market_last_trade_date`` do not exist in either the
  ContextInfo stub or the xtdata SDK — they are MiniQMT-only conveniences that
  must be synthesized from ``get_trading_dates``.
* ``get_trading_dates`` exists on BOTH objects but with **different first
  arguments**: the ContextInfo method takes ``stockcode`` while the xtdata
  module function takes ``market``. We pass ``market`` (that is what every
  caller in this codebase supplies), so we route through xtdata.

This module does not make trading decisions.
"""

import importlib
import time

from ..code_utils import (
    EXCHANGE_TOKENS, FUTURES_MARKET_CODES, normalize_stock_code)
from ..logging_setup import get_logger
from ..quote_utils import find_code_payload, is_option_code, latest_quote_row


log = get_logger("market")


MARKET_CODES = {"SH", "SZ", "BJ", "HK"}

# A market token asks QMT for every instrument the exchange lists, and stocks
# are a small minority: "SH" answers with 26744 instruments of which 2315 (8.7%)
# are stocks -- the rest is bonds, repos and the like. At a measured ~0.29ms per
# instrument that is 7.5s for the whole market against 0.88s for the stocks
# alone, and the cost is strictly linear in instrument count (issue #104).
#
# So narrow the REQUEST, not the response: resolve the token to a sector listing
# and ask QMT only for those codes. Filtering afterwards would still pay for
# every instrument. The sector lookup is FormulaServer-served (~10ms measured),
# so it costs nothing next to what it saves.
#
# Sector names are QMT's own, verified against a live terminal. Note "北证A股"
# is NOT one of them -- the Beijing board is "京市A股".
STOCK_SECTOR_BY_MARKET = {
    "SH": "上证A股",
    "SZ": "深证A股",
    "BJ": "京市A股",
}
# Cross-market sectors; results are filtered back to the requested exchange.
# Not narrowing at all: "all" keeps the market token, i.e. the pre-0.2.15
# behaviour of returning every instrument the exchange lists.
DEFAULT_TICK_TYPES = ("stock",)

SECTOR_BY_TYPE = {
    "stock": "沪深京A股",
    "fund": "沪深基金",
    "etf": "沪深ETF",
    "index": "沪深指数",
    "convertible": "沪深转债",
}


def normalize_market_or_stock_code(code):
    """Market token, or a normalised instrument code.

    Only the market-token test uppercases. Handing an uppercased string to
    normalize_stock_code would defeat its case preservation, which is what kept
    the #95 fix from reaching get_full_tick: "rb2610.SF" arrived there as
    "RB2610.SF", a code QMT does not recognise. Futures symbols follow each
    exchange's own convention and the spellings are not interchangeable.
    """
    text = str(code or "").strip()
    if text.upper() in EXCHANGE_TOKENS:
        return text.upper()
    return normalize_stock_code(text)


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return list(value)


def _float_or_none(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number


def _row_day(row):
    """The YYYYMMDD a bar belongs to, from whichever time column it carries.

    Bars come back keyed on stime/time/date depending on the build, and the
    value may be a millisecond epoch, a 'YYYY-MM-DD ...' string, or already a
    plain YYYYMMDD. Only the day matters here (issue #165 probes one day at a
    time), so normalise to that and give up quietly on anything else.
    """
    for key in ("stime", "time", "date", "datetime", "timetag"):
        raw = row.get(key)
        if raw in (None, ""):
            continue
        text = str(raw).strip()
        digits = "".join(ch for ch in text if ch.isdigit())
        if len(digits) >= 13:                      # ms epoch
            try:
                import datetime as _d

                return _d.datetime.fromtimestamp(int(digits[:13]) / 1000.0).strftime("%Y%m%d")
            except Exception:
                continue
        if len(digits) >= 8:                       # YYYYMMDD, possibly with a time tail
            return digits[:8]
    return ""


def _frame_rows(frame):
    """A market-data frame as a list of row dicts, whatever shape it arrived in.

    pandas DataFrame, this bridge's serialisable marker, a list of dicts, or a
    dict of column arrays -- the same four shapes quote_utils deals with. No
    pandas import: this also runs inside QMT's embedded Python.
    """
    if frame is None:
        return []
    if isinstance(frame, dict) and frame.get("__bigqmt_type__") == "DataFrame":
        return _frame_rows(frame.get("records") or [])
    if hasattr(frame, "columns") and hasattr(frame, "iloc"):
        try:
            columns = list(frame.columns)
            return [{name: frame[name][i] for name in columns}
                    for i in range(len(frame))]
        except Exception:
            return []
    if isinstance(frame, (list, tuple)):
        return [row for row in frame if isinstance(row, dict)]
    if isinstance(frame, dict):
        columns = {k: v for k, v in frame.items() if hasattr(v, "__len__")
                   and not isinstance(v, (str, bytes))}
        if not columns:
            return [dict(frame)]
        length = min(len(v) for v in columns.values())
        return [{k: v[i] for k, v in columns.items()} for i in range(length)]
    return []


def _raw_frame_columns(field_list):
    columns = [str(field) for field in (field_list or [])]
    if columns and "stime" not in columns:
        columns.insert(0, "stime")
    return columns


def _market_data_answer_empty(answer):
    """True when no code in the answer carries a single row.

    Covers both shapes get_market_data_ex can return: the raw path's
    serialisable marker dict (records list) and the plain path's pandas
    frames (index length). Anything unrecognised counts as an answer rather
    than as empty -- a retry must never replace data with nothing.
    """
    if not isinstance(answer, dict) or not answer:
        return True
    for value in answer.values():
        if isinstance(value, dict) and value.get("__bigqmt_type__") == "DataFrame":
            if value.get("records"):
                return False
        elif hasattr(value, "index"):
            try:
                if len(value.index) > 0:
                    return False
            except Exception:
                return False
        elif value:
            return False
    return True


def _raw_market_data_payload(payload, field_list, stock_list):
    if not isinstance(payload, dict):
        return payload
    source = {str(code): records for code, records in payload.items()}
    codes = []
    for code in list(stock_list or []) + list(source):
        text = str(code)
        if text not in codes:
            codes.append(text)
    columns = _raw_frame_columns(field_list)
    return {
        code: {
            "__bigqmt_type__": "DataFrame",
            "columns": columns,
            "records": source.get(code) or [],
        }
        for code in codes
    }


_NATIVE_XTDATA = None  # cached native xtdata SDK module (None = not yet tried)
_NATIVE_XTDATA_UNAVAILABLE = object()  # sentinel: looked, not importable


# MiniQMT table_list names -> Big QMT financial table names (issue #52).
# MiniQMT passes whole-table names; Big QMT's get_financial_data fieldList
# wants dotted "TABLE.field" entries, so whole-table names expand via
# _FINANCIAL_TABLE_FIELDS below.
_FINANCIAL_TABLE_MAP = {
    "Balance": "ASHAREBALANCESHEET",
    "Income": "ASHAREINCOME",
    "CashFlow": "ASHARECASHFLOW",
    "Capital": "CAPITALSTRUCTURE",
    "PershareIndex": "PERSHAREINDEX",
    "Top10Holder": "TOP10HOLDER",
    "Top10FlowHolder": "TOP10FLOWHOLDER",
    "HolderNum": "SHAREHOLDER",
}

_FINANCIAL_TABLE_FIELDS = {
    "ASHAREBALANCESHEET": [
        "m_timetag", "m_anntime", "cash_equivalents", "bill_receivable",
        "account_receivable", "advance_payment", "other_receivable",
        "other_current_assets", "total_current_assets", "inventories",
        "constru_in_process", "construction_materials", "long_deferred_expense",
        "total_non_current_assets", "int_rcv", "fin_assets_avail_for_sale",
        "held_to_mty_invest", "long_term_eqy_invest", "fix_assets",
        "intang_assets", "deferred_tax_assets", "tot_assets", "goodwill",
        "shortterm_loan", "dividend_payable", "other_payable",
        "non_current_liability_in_one_year", "other_current_liability",
        "longterm_account_payable", "accounts_payable", "advance_peceipts",
        "total_current_liability", "notes_payable", "long_term_loans",
        "grants_received", "other_non_current_liabilities",
        "non_current_liabilities", "specific_reserves", "tradable_fin_liab",
        "empl_ben_payable", "taxes_surcharges_payable", "int_payable",
        "bonds_payable", "deferred_tax_liab", "tot_liab", "cap_stk",
        "cap_rsrv", "surplus_rsrv", "undistributed_profit",
        "tot_shrhldr_eqy_excl_min_int", "minority_int",
        "tot_liab_shrhldr_eqy", "total_equity",
    ],
    "ASHAREINCOME": [
        "m_timetag", "m_anntime", "revenue", "total_operating_cost",
        "revenue_inc", "total_expense", "less_taxes_surcharges_ops",
        "sale_expense", "less_gerl_admin_exp", "financial_expense",
        "less_impair_loss_assets", "change_income_fair_value",
        "plus_net_invest_inc", "incl_inc_invest_assoc_jv_entp",
        "oper_profit", "plus_non_oper_rev", "less_non_oper_exp",
        "tot_profit", "inc_tax", "net_profit_incl_min_int_inc",
        "net_profit_excl_min_int_inc", "total_income", "total_income_minority",
        "earned_premium",
    ],
    "ASHARECASHFLOW": [
        "m_timetag", "m_anntime", "goods_sale_and_service_render_cash",
        "tax_levy_refund", "other_cash_recp_ral_oper_act",
        "stot_cash_inflows_oper_act", "goods_and_services_cash_paid",
        "cash_pay_beh_empl", "pay_all_typ_tax",
        "other_cash_pay_ral_oper_act", "stot_cash_outflows_oper_act",
        "net_cash_flows_oper_act", "cash_recp_return_invest",
        "net_cash_recp_disp_fiolta", "stot_cash_inflows_inv_act",
        "cash_paid_invest", "cash_pay_acq_const_fiolta",
        "other_cash_pay_ral_inv_act", "stot_cash_outflows_inv_act",
        "net_cash_flows_inv_act", "cash_recp_cap_contrib",
        "cash_recp_borrow", "other_cash_recp_ral_fnc_act",
        "stot_cash_inflows_fnc_act", "cash_prepay_amt_borr",
        "cash_pay_dist_dpcp_int_exp", "other_cash_pay_ral_fnc_act",
        "stot_cash_outflows_fnc_act", "net_cash_flows_fnc_act",
        "eff_fx_flu_cash", "net_incr_cash_cash_equ",
        "net_cash_deal_subcompany", "cash_from_mino_s_invest_sub",
        "fix_intan_other_asset_dispo_cash_payment",
    ],
    "CAPITALSTRUCTURE": [
        "m_timetag", "m_anntime", "total_capital", "circulating_capital",
        "free_float_capital", "restrict_circulating_capital",
    ],
    "PERSHAREINDEX": [
        "m_timetag", "m_anntime", "m_quarter",
        "s_fa_ocfps", "s_fa_bps", "s_fa_eps_basic", "s_fa_eps_diluted",
        "s_fa_undistributedps", "s_fa_surpluscapitalps",
        "adjusted_earnings_per_share", "du_return_on_equity",
        "sales_gross_profit", "inc_revenue_rate", "du_profit_rate",
        "inc_net_profit_rate", "adjusted_net_profit_rate",
        "inc_total_revenue_annual", "inc_net_profit_to_shareholders_annual",
        "adjusted_profit_to_profit_annual", "equity_roe", "net_roe",
        "total_roe", "gross_profit", "net_profit", "actual_tax_rate",
        "pre_pay_operate_income", "sales_cash_flow", "gear_ratio",
        "inventory_turnover", "s_fa_fcfeps", "s_fa_retainedps",
        "s_fa_fcffps", "s_fa_ebitps", "s_fa_cfps", "s_fa_grps",
        "s_fa_surplusreserveps", "s_fa_orps", "inc_revenue",
        "inc_gross_profit", "inc_profit_before_tax", "du_profit",
        "inc_net_profit", "adjusted_net_profit",
    ],
    "TOP10HOLDER": [
        "declareDate", "endDate", "name", "type", "quantity",
        "reason", "ratio", "nature", "rank",
    ],
    "TOP10FLOWHOLDER": [
        "declareDate", "endDate", "name", "type", "quantity",
        "reason", "ratio", "nature", "rank",
    ],
    "SHAREHOLDER": [
        "declareDate", "endDate", "shareholder", "shareholderA",
        "shareholderB", "shareholderH", "shareholderFloat",
        "shareholderOther",
    ],
}


def _translate_financial_fields(table_list):
    """Translate MiniQMT table names to Big QMT's dotted fieldList (issue #52).

    MiniQMT accepts whole-table names ("Balance") or dotted "Table.field";
    Big QMT only accepts dotted "BIGTABLE.field". Whole-table names expand to
    every field of the mapped table; dotted entries get their table prefix
    remapped; unknown names pass through unchanged so QMT decides.
    """
    out = []
    for item in table_list or []:
        name = str(item or "").strip()
        if not name:
            continue
        if "." in name:
            head, _, field = name.partition(".")
            big = _FINANCIAL_TABLE_MAP.get(head, head)
            out.append("%s.%s" % (big, field))
            continue
        big = _FINANCIAL_TABLE_MAP.get(name, name)
        if big in _FINANCIAL_TABLE_FIELDS:
            # MiniQMT table name, or a bare Big QMT table name: both expand
            # to the table's full dotted field list.
            for field in _FINANCIAL_TABLE_FIELDS[big]:
                out.append("%s.%s" % (big, field))
            continue
        out.append(name)
    return out


def _load_native_xtdata():
    """Return the *native* xtdata SDK module shipped with the QMT install.

    The Big QMT process ships two ``xtquant.xtdata`` modules:

    * ``python/xtquant/xtdata.py`` — our RPC shim (forwards back over Redis).
    * ``bin.x64/Lib/site-packages/xtquant/xtdata.py`` — the real SDK that
      connects to the local quote service via ``get_client()``.

    In the server-side adapter we need the real SDK because the global-data
    functions (sectors, holidays, trading dates) only exist there. We load it
    by absolute path so our shim (which may shadow it on ``sys.path``) never
    wins. Returns ``None`` when the SDK is unavailable (e.g. running outside
    QMT, or in a unit test) so callers can degrade gracefully.
    """
    global _NATIVE_XTDATA
    if _NATIVE_XTDATA is _NATIVE_XTDATA_UNAVAILABLE:
        return None
    if _NATIVE_XTDATA is not None:
        return _NATIVE_XTDATA
    try:
        import os
        import sys

        # Locate <qmt_root>/bin.x64/{lib,Lib}/site-packages that holds the REAL
        # xtquant package. Walk up from this file (works whether we live under
        # python/bigqmt_signal_trader/adapters/ in QMT or src/... in the repo).
        real_sp = None
        start = os.path.abspath(__file__)
        for _ in range(8):
            parent = os.path.dirname(start)
            if parent == start:
                break
            for libdir in ("lib", "Lib"):
                candidate = os.path.join(parent, "bin.x64", libdir, "site-packages")
                if os.path.isdir(os.path.join(candidate, "xtquant")):
                    real_sp = candidate
                    break
            if real_sp:
                break
            start = parent

        loaded = None
        if real_sp:
            # Import the real xtquant PACKAGE (not xtdata.py standalone) so its
            # package-relative imports (xtbson etc.) resolve. Un-shadow our RPC
            # shim (python/xtquant, src/xtquant) which otherwise wins on sys.path:
            # put the real site-packages first and drop any already-imported shim
            # xtquant modules (their __file__ is not under bin.x64/).
            if real_sp not in sys.path:
                sys.path.insert(0, real_sp)
            for name in [n for n in list(sys.modules) if n == "xtquant" or n.startswith("xtquant.")]:
                mod_file = getattr(sys.modules.get(name), "__file__", "") or ""
                if "bin.x64" not in mod_file:
                    del sys.modules[name]
            try:
                module = importlib.import_module("xtquant.xtdata")
                if "bin.x64" in (getattr(module, "__file__", "") or ""):
                    loaded = module
            except Exception:
                loaded = None
        _NATIVE_XTDATA = loaded if loaded is not None else _NATIVE_XTDATA_UNAVAILABLE
    except Exception:
        _NATIVE_XTDATA = _NATIVE_XTDATA_UNAVAILABLE
    return None if _NATIVE_XTDATA is _NATIVE_XTDATA_UNAVAILABLE else _NATIVE_XTDATA


class BigQmtMarketDataProvider:
    def __init__(self, context_info, native_xtdata=None, qmt_api=None):
        self.context_info = context_info
        # Allow injection for tests; otherwise resolve lazily on first use.
        self._native_xtdata = native_xtdata
        self.qmt_api = dict(qmt_api or {})

    def _context_method(self, method_name):
        method = getattr(self.context_info, method_name, None)
        if method is None:
            raise NotImplementedError("ContextInfo.%s is not available" % method_name)
        return method

    def _call_context(self, method_name, *args, **kwargs):
        return self._context_method(method_name)(*args, **kwargs)

    def _native(self):
        """Return the native xtdata SDK, resolving it lazily on first use.

        Returns None when the SDK is not importable. NOTE: in a Big QMT
        (full trading terminal) process the SDK loads but its get_client()
        cannot connect to a quote service — there is no MiniQMT process
        writing ~/.xtquant/*/xtdata.cfg. Callers must therefore be ready for
        the SDK call itself to raise "无法连接行情服务" and fall back.
        """
        if self._native_xtdata is None:
            self._native_xtdata = _load_native_xtdata()
        return self._native_xtdata

    # In a Big QMT terminal the SDK never gains a quote service mid-process, so
    # a failed native call is retried only once per this window instead of on
    # every call: measured on Guojin 2.1.19.0, EVERY get_trading_dates paid the
    # SDK's ~2.1s service-dial failure before falling back, and the first call
    # after a strategy start paid 21.6s (issue #160).
    NATIVE_FAILURE_CACHE_SECONDS = 600.0

    def _native_dead_marks(self):
        marks = getattr(self, "_native_dead_marks_dict", None)
        if marks is None:
            marks = self._native_dead_marks_dict = {}
        return marks

    def _native_known_dead(self, func_name):
        ts = self._native_dead_marks().get(func_name)
        return ts is not None and (time.time() - ts) < self.NATIVE_FAILURE_CACHE_SECONDS

    def _native_or_context(self, func_name, context_caller, *args, **kwargs):
        """Prefer the xtdata SDK function, fall back to a ContextInfo call.

        Several data APIs exist only as xtdata module functions. When the SDK
        is available AND its quote service is reachable we use it. Otherwise
        we fall back to ContextInfo so callers get a best-effort answer.

        A native failure is remembered per function for
        NATIVE_FAILURE_CACHE_SECONDS: in Big QMT the failure mode is "SDK
        present, quote service absent", which does not heal mid-process, and
        paying its multi-second dial timeout on every call made
        get_trading_dates cost 2.1s per call forever (issue #160).
        """
        module = self._native()
        if module is not None and not self._native_known_dead(func_name):
            fn = getattr(module, func_name, None)
            if fn is not None:
                try:
                    result = fn(*args, **kwargs)
                    self._native_dead_marks().pop(func_name, None)
                    return result
                except Exception:
                    # Big QMT path: SDK present but no quote service to talk
                    # to ("无法连接行情服务"). Don't crash — let the ContextInfo
                    # fallback have a turn.
                    self._native_dead_marks()[func_name] = time.time()
        return context_caller()

    def _call_first_supported(self, shapes):
        last_error = None
        for method_name, args, kwargs in shapes:
            method = getattr(self.context_info, method_name, None)
            if method is None:
                continue
            try:
                return method(*args, **kwargs)
            except TypeError as exc:
                last_error = exc
                continue
        if last_error is not None:
            raise last_error
        raise NotImplementedError("none of the ContextInfo methods is available")

    def _market_data_shapes(self, method_name, **params):
        field_list = list(params.get("field_list") or params.get("fields") or [])
        stock_list = _as_list(params.get("stock_list") or params.get("stock_code"))
        period = params.get("period", "1d")
        start_time = params.get("start_time", "")
        end_time = params.get("end_time", "")
        count = params.get("count", -1)
        dividend_type = params.get("dividend_type", "none")
        fill_data = params.get("fill_data", True)
        data_dir = params.get("data_dir")

        mini_kwargs = {
            "field_list": field_list,
            "stock_list": stock_list,
            "period": period,
            "start_time": start_time,
            "end_time": end_time,
            "count": count,
            "dividend_type": dividend_type,
            "fill_data": fill_data,
        }
        big_kwargs = {
            "fields": field_list,
            "stock_code": stock_list,
            "period": period,
            "start_time": start_time,
            "end_time": end_time,
            "count": count,
            "dividend_type": dividend_type,
        }
        if method_name == "get_local_data" and data_dir is not None:
            mini_kwargs["data_dir"] = data_dir
            big_kwargs["data_dir"] = data_dir
        positional_tail_kwargs = {
            "period": period,
            "start_time": start_time,
            "end_time": end_time,
            "count": count,
            "dividend_type": dividend_type,
        }
        if method_name == "get_local_data" and data_dir is not None:
            positional_tail_kwargs["data_dir"] = data_dir

        # fill_data (停牌填充) reaches big QMT too. Its signature has it --
        #   C.get_market_data_ex(fields, stock_code, period, start_time,
        #                        end_time, count, dividend_type, fill_data,
        #                        subscribe)
        # -- and big_kwargs is the FIRST shape tried, so it succeeded while
        # silently dropping the argument: a caller asking not to fill suspended
        # days got them filled anyway, with no error (issue #167, @zxm9999).
        #
        # Carried in a shape of its own, tried ahead of the bare one, rather
        # than added to big_kwargs directly: a terminal whose signature lacks
        # fill_data would raise TypeError, and _call_first_supported only falls
        # through on TypeError -- so the bare shape has to stay reachable.
        big_kwargs_filled = dict(big_kwargs, fill_data=fill_data)
        positional_tail_filled = dict(positional_tail_kwargs, fill_data=fill_data)

        return [
            (method_name, (), big_kwargs_filled),
            (method_name, (), big_kwargs),
            (method_name, (), mini_kwargs),
            (
                method_name,
                (field_list, stock_list, period, start_time, end_time, count, dividend_type, fill_data),
                {},
            ),
            (method_name, (field_list, stock_list), positional_tail_filled),
            (method_name, (field_list, stock_list), positional_tail_kwargs),
            (
                method_name,
                (field_list,),
                {
                    "stock_code": stock_list,
                    "period": period,
                    "start_time": start_time,
                    "end_time": end_time,
                    "count": count,
                    "dividend_type": dividend_type,
                },
            ),
            (
                method_name,
                (field_list,),
                {
                    "stock_list": stock_list,
                    "period": period,
                    "start_time": start_time,
                    "end_time": end_time,
                    "count": count,
                    "dividend_type": dividend_type,
                    "fill_data": fill_data,
                },
            ),
        ]

    def _sector_codes(self, sector):
        """Cached sector listing. Membership does not change intraday and the
        lookup is FormulaServer-served, so once per sector per run is enough."""
        cache = getattr(self, "_sector_cache", None)
        if cache is None:
            cache = self._sector_cache = {}
        if sector not in cache:
            try:
                cache[sector] = list(self.get_stock_list_in_sector(sector) or [])
            except Exception:
                cache[sector] = []
        return cache[sector]

    def _expand_market_token(self, market, types):
        """Codes of the requested types on one exchange, or None to give up.

        None means "could not narrow", and the caller keeps the market token: a
        slow answer beats an empty one, the same rule the key mapping below
        follows.
        """
        collected = []
        for kind in types:
            kind = str(kind or "").strip().lower()
            sector = None
            if kind == "stock":
                sector = STOCK_SECTOR_BY_MARKET.get(market)
            if sector is None:
                sector = SECTOR_BY_TYPE.get(kind)
            if sector is None:
                return None          # unknown type: do not silently drop it
            listing = self._sector_codes(sector)
            if not listing:
                return None          # sector unavailable on this terminal
            suffix = "." + market
            collected.extend(c for c in listing if str(c).upper().endswith(suffix))
        seen, unique = set(), []
        for code in collected:
            if code not in seen:
                seen.add(code)
                unique.append(code)
        return unique or None

    def _notice_narrowed(self, market, kept, total_hint):
        """Say once per process that a market token was narrowed.

        The default changed from "everything the exchange lists" to stocks, so
        a caller who wanted bonds or repos would otherwise just see fewer rows
        and no reason why.
        """
        seen = getattr(self, "_narrow_notified", None)
        if seen is None:
            seen = self._narrow_notified = set()
        if market in seen:
            return
        seen.add(market)
        try:
            print("[bigqmt_market] %s narrowed to %d %s; pass types=['all'] for "
                  "every instrument the exchange lists" % (market, kept, total_hint))
        except Exception:
            pass

    def get_ticks(self, codes, types=None):
        """Snapshot quotes, keyed the way the CALLER spelled each code.

        Codes go to QMT upper-cased, but the futures exchanges use lower-case
        instrument codes ('rb2708.SF', 'a2609.DF'), so returning QMT's keys made
        ``code in result`` fail for every one of them and the level-2 book look
        missing (issue #58). get_market_data_ex already echoes the caller's
        spelling; this brings get_full_tick in line.

        Only the CASE is restored, not the normalization: '600000' still comes
        back as '600000.SH', because completing the suffix is useful and callers
        rely on it. Only codes that differ from their normalized form purely by
        case -- which is exactly the futures situation -- are mapped back.

        A code QMT answers under a key we did not ask for is passed through
        untouched rather than dropped: losing a quote is worse than an
        unexpected key.
        """
        requested = list(codes or [])
        normalized_codes = [normalize_market_or_stock_code(code) for code in requested]
        # Default to stocks. A market token lists every instrument the exchange
        # carries and stocks are 8.7% of it, so the old default made everyone pay
        # 7.5s for a 0.9s answer. types=["all"] restores the full listing.
        wanted = list(types) if types else list(DEFAULT_TICK_TYPES)
        if not any(str(kind).strip().lower() == "all" for kind in wanted):
            expanded = []
            for code in normalized_codes:
                # A futures exchange lists only futures, so its token already
                # says what it holds -- there is nothing to narrow and no
                # A-share sector to narrow it with.
                narrowable = code in MARKET_CODES and code not in FUTURES_MARKET_CODES
                narrowed = (self._expand_market_token(code, wanted)
                            if narrowable else None)
                if narrowed is None:
                    expanded.append(code)     # not a token, or could not narrow
                else:
                    expanded.extend(narrowed)
                    self._notice_narrowed(code, len(narrowed), "/".join(wanted))
            normalized_codes = expanded
        data = self.context_info.get_full_tick(normalized_codes) or {}
        if not isinstance(data, dict):
            return data or {}

        # Full Big-QMT 2.1.19.0 can return no entry from get_full_tick for an
        # explicitly requested .SHO/.SZO contract even while its tick stream is
        # active. The same contract is available through get_market_data_ex
        # (period="tick"), including the five-level book. Recover only missing
        # option symbols so the fast native path for stocks/funds is unchanged.
        answered = {str(key).upper() for key in data}
        missing_options = [
            code for code in normalized_codes
            if is_option_code(code) and str(code).upper() not in answered
        ]
        if missing_options:
            try:
                fallback = self.get_market_data_ex(
                    field_list=[],
                    stock_list=missing_options,
                    period="tick",
                    count=1,
                    dividend_type="none",
                    fill_data=False,
                ) or {}
                for code in missing_options:
                    row = latest_quote_row(find_code_payload(fallback, code))
                    if row:
                        data[code] = row
            except Exception as exc:
                # A mixed stock/option request must still return the native
                # snapshots it already has when an older QMT lacks this API.
                if not getattr(self, "_option_tick_fallback_warned", False):
                    self._option_tick_fallback_warned = True
                    log.warning(
                        "option tick fallback failed for %s: %s",
                        missing_options, exc,
                    )

        # Map any answer key back to the caller's spelling when the two differ
        # only by case. Keyed on the upper-cased form deliberately: we now send
        # the caller's own spelling, so this must work whether QMT echoes that
        # back or answers in a canonical case of its own -- and which of those
        # it does could not be observed here (no futures data on this terminal).
        # Structural differences (a completed suffix) are left alone, since
        # "600000" -> "600000.SH" is normalization callers rely on.
        original_by_upper = {}
        for original, normalized in zip(requested, normalized_codes):
            original, normalized = str(original), str(normalized)
            if original.upper() == normalized.upper():
                original_by_upper.setdefault(original.upper(), original)

        if not original_by_upper:
            return data
        return dict(
            (original_by_upper.get(str(key).upper(), key), value)
            for key, value in data.items()
        )

    def get_instrument(self, code):
        normalized = normalize_stock_code(code)
        data = self.context_info.get_instrumentdetail(normalized)
        return data or {}

    def get_instrument_type(self, code, variety_list=None):
        if hasattr(self.context_info, "get_instrument_type"):
            return self.context_info.get_instrument_type(code, variety_list)
        normalized = normalize_stock_code(code)
        pure = normalized.split(".")[0]
        result = {
            "stock": pure.startswith(("000", "001", "002", "003", "300", "301", "600", "601", "603", "605", "688", "689")),
            "fund": pure.startswith(("15", "16", "50", "51", "56", "58")),
            "etf": pure.startswith(("15", "51", "56", "58")),
            "bond": pure.startswith(("11", "12")),
            "index": pure.startswith(("000", "399")) and not normalized.startswith(("000001.SZ", "000002.SZ")),
        }
        if variety_list:
            return {str(name): bool(result.get(str(name), False)) for name in variety_list}
        return result

    def get_stock_list_in_sector(self, sector_name, real_timetag=-1):
        shapes = [
            ("get_stock_list_in_sector", (sector_name, real_timetag), {}),
            ("get_stock_list_in_sector", (sector_name,), {}),
        ]
        data = self._call_first_supported(shapes)
        return data or []

    def get_market_data(
        self,
        field_list=None,
        stock_list=None,
        period="1d",
        start_time="",
        end_time="",
        count=-1,
        dividend_type="none",
        fill_data=True,
    ):
        return self._call_first_supported(
            self._market_data_shapes(
                "get_market_data",
                field_list=field_list,
                stock_list=stock_list,
                period=period,
                start_time=start_time,
                end_time=end_time,
                count=count,
                dividend_type=dividend_type,
                fill_data=fill_data,
            )
        )

    # Synthesis periods (weekly and up) where an all-fields (field_list=[])
    # answer of zero rows is not necessarily truthful: a live terminal
    # reported 0 rows for 1mon/1q/1hy/1y with fields=[] while the same bars
    # read fine with an explicit OHLCV list (issue #219). [] means "all
    # columns" to QMT, and how a build expands that for synthesized periods is
    # not trustworthy. Retry once with the explicit K-line field list from the
    # terminal's own reference; an empty retry still means empty.
    _SYNTH_PERIOD_FIELD_RETRY = ("1w", "1mon", "1q", "1hy", "1y")
    _KLINE_ALL_FIELDS = (
        "time", "open", "high", "low", "close", "volume", "amount",
        "settle", "openInterest", "preClose", "suspendFlag",
    )

    def get_market_data_ex(self, **kwargs):
        answer = self._get_market_data_ex_once(**kwargs)
        fields = kwargs.get("field_list") or kwargs.get("fields")
        period = str(kwargs.get("period") or "")
        if (fields or period not in self._SYNTH_PERIOD_FIELD_RETRY
                or not _market_data_answer_empty(answer)):
            return answer
        retry = dict(kwargs)
        retry.pop("fields", None)
        retry["field_list"] = list(self._KLINE_ALL_FIELDS)
        retried = self._get_market_data_ex_once(**retry)
        # The retry is a strict improvement only when it found rows; otherwise
        # keep the original (empty) answer, so "no data" stays "no data".
        return retried if not _market_data_answer_empty(retried) else answer

    def _get_market_data_ex_once(self, **kwargs):
        raw_method = getattr(self.context_info, "get_market_data_ex_ori", None)
        if callable(raw_method):
            raw_data = self._call_first_supported(
                self._market_data_shapes("get_market_data_ex_ori", **kwargs)
            )
            return _raw_market_data_payload(
                raw_data,
                kwargs.get("field_list") or kwargs.get("fields"),
                kwargs.get("stock_list") or kwargs.get("stock_code"),
            )
        shapes = self._market_data_shapes("get_market_data_ex", **kwargs)
        if hasattr(self.context_info, "get_market_data"):
            shapes.extend(self._market_data_shapes("get_market_data", **kwargs))
        return self._call_first_supported(shapes)

    def get_local_data(self, **kwargs):
        shapes = self._market_data_shapes("get_local_data", **kwargs)
        if hasattr(self.context_info, "get_market_data"):
            shapes.extend(self._market_data_shapes("get_market_data", **kwargs))
        return self._call_first_supported(shapes)

    # Probing more than this many candidate ex-dividend days in one range
    # request is a sign the filter did not narrow anything (a code with no
    # daily bars, say). Stop rather than issue hundreds of RPCs.
    _DIVID_MAX_PROBES = 40
    # Prices are 2-3 decimals; this is well inside a real dividend and well
    # outside float noise.
    _DIVID_PRICE_EPSILON = 1e-4

    def get_divid_factors(self, stock_code, start_time="", end_time=""):
        """Dividend factors over a RANGE, not just one day (issue #165).

        This used to collapse any range to ``end_time or start_time`` and ask
        for that single day. Since a given day is almost never an ex-dividend
        day, a range request answered ``{}`` -- and an empty dict is
        indistinguishable from "no events in this range". A reporter's nightly
        whole-market factor sync ran that way for a month: every symbol
        returned empty, the factor table silently stopped updating, and nothing
        errored because K-lines kept arriving normally.

        The old comment claimed "the xtdata SDK has the same 2-arg shape". It
        does not -- the terminal's bundled SDK is
        ``get_divid_factors(stock_code, start_time, end_time)``. So try the
        range shapes first, and only fall back to expanding the range here.

        The expansion does not walk every trading day. An ex-dividend day is
        exactly a day whose ``preClose`` differs from the previous bar's
        ``close`` (that is what the adjusted reference price means), so one
        daily-bar read narrows a two-year window to a handful of candidates,
        and only those get probed.
        """
        start = str(start_time or "").strip()
        end = str(end_time or "").strip()
        if not start and not end:
            return self._call_context("get_divid_factors", stock_code)
        if not start or not end or start == end:
            # A single day was asked for; answer it directly.
            return self._call_context("get_divid_factors", stock_code, end or start)

        for shape in (
            lambda: self._native_divid_factors(stock_code, start, end),
            lambda: self._call_context("get_divid_factors", stock_code, start, end),
        ):
            try:
                answer = shape()
            except Exception:
                continue
            if answer:
                return dict(answer)

        return self._expand_divid_factors(stock_code, start, end)

    def _native_divid_factors(self, stock_code, start_time, end_time):
        module = self._native()
        func = getattr(module, "get_divid_factors", None) if module is not None else None
        if not callable(func):
            return None
        return func(stock_code, start_time, end_time)

    def _download_daily_window(self, stock_code, start_time, end_time):
        """One terminal-side daily-bars download for a window that scanned
        empty (#222).

        Uses the terminal's own downloader (the injected
        ``download_history_data`` / ``down_history_data`` global), never the
        embedded xtdata SDK -- that one has no reachable data service in the
        full terminal. True when a downloader answered; False when there is no
        channel or it raised -- the caller keeps the original honest error.
        """
        download = (self.qmt_api.get("download_history_data")
                    or self.qmt_api.get("down_history_data"))
        if not callable(download):
            return False
        try:
            download(stock_code, "1d", start_time, end_time)
        except Exception:
            return False
        return True

    def _divid_candidate_days(self, stock_code, start_time, end_time):
        """Days in the window that look like ex-dividend days.

        preClose on an ex-dividend day is the adjusted reference price, so it
        differs from the previous bar's raw close. Everywhere else the two are
        equal -- measured on this terminal across 649 daily bars, they matched
        on all but the handful of genuine ex-dividend days.
        """
        frame = (self.get_market_data_ex(
            field_list=["close", "preClose", "stime"], stock_list=[stock_code],
            period="1d", start_time=start_time, end_time=end_time,
            dividend_type="none", fill_data=False) or {}).get(stock_code)
        rows = _frame_rows(frame)
        self._divid_scan_rows = len(rows)
        candidates = []
        previous_close = None
        for row in rows:
            close = _float_or_none(row.get("close"))
            pre_close = _float_or_none(row.get("preClose"))
            day = _row_day(row)
            if day and pre_close:
                if previous_close is None:
                    candidates.append(day)      # first bar: no previous to compare
                elif abs(pre_close - previous_close) > self._DIVID_PRICE_EPSILON:
                    candidates.append(day)
            if close:
                previous_close = close
        return candidates

    def _expand_divid_factors(self, stock_code, start_time, end_time):
        """Aggregate single-day lookups over the candidate days.

        Raises rather than returning ``{}`` when there were no daily bars to
        scan. An empty dict would mean "no dividends in this range", and the
        whole point of #165 is that a silent empty answer is what let a factor
        table stop updating for a month unnoticed. No bars means we cannot
        tell, and saying so is the only honest answer.
        """
        self._divid_scan_rows = 0
        # Set before the scan so the error can say what was tried (#222).
        self._divid_download_note = (
            ". Download the daily history first (download_history_data), or "
            "ask one date at a time")
        try:
            candidates = self._divid_candidate_days(stock_code, start_time, end_time)
        except Exception:
            candidates = []
        if self._divid_scan_rows < 2 and self._download_daily_window(
                stock_code, start_time, end_time):
            # #222: the bridge knows what is missing, so it fetches it rather
            # than telling the caller to. The downloader may answer before the
            # bars are actually local, so rescan a few times on a short leash
            # before concluding anything.
            self._divid_download_note = (
                ". The bridge downloaded the daily window itself "
                "(download_history_data) and the terminal still returned %d "
                "daily bar(s)" )
            for _attempt in range(3):
                try:
                    candidates = self._divid_candidate_days(
                        stock_code, start_time, end_time)
                except Exception:
                    candidates = []
                if self._divid_scan_rows >= 2:
                    break
                time.sleep(1.0)
            self._divid_download_note %= self._divid_scan_rows
        if self._divid_scan_rows < 2:
            raise RuntimeError(
                "get_divid_factors(%r, %s, %s) cannot answer a range here: big "
                "QMT's ContextInfo takes only (code, single date), so the range "
                "is expanded by scanning daily bars for ex-dividend days -- and "
                "this terminal returned %d daily bar(s) for that window, too "
                "few to compare even one preClose against the previous close"
                "%s. Returning {} would have been indistinguishable from 'no "
                "dividends in this range' (issue #165)."
                % (stock_code, start_time, end_time, self._divid_scan_rows,
                   self._divid_download_note))
        merged = {}
        merged = {}
        for day in candidates[:self._DIVID_MAX_PROBES]:
            try:
                answer = self._call_context("get_divid_factors", stock_code, day)
            except Exception:
                continue
            if answer:
                merged.update(answer)
        return merged

    def _download(self, func_name, sdk_args, sdk_kwargs, ctx_call):
        """Download history in Big QMT.

        Full Big QMT exposes historical-data supplementation as the injected
        global ``down_history_data`` function. Prefer it directly; the MiniQMT
        ``xtdata`` client usually cannot connect inside the full terminal.
        """
        down_history_data = self.qmt_api.get("down_history_data")
        if callable(down_history_data):
            if func_name == "download_history_data":
                stock_code, period, start_time, end_time = sdk_args
                return down_history_data(stock_code, period, start_time, end_time)
            if func_name == "download_history_data2":
                stock_list, period, start_time, end_time = sdk_args
                result = None
                for stock_code in stock_list:
                    result = down_history_data(stock_code, period, start_time, end_time)
                return result

        if getattr(self.context_info, func_name, None) is not None:
            return ctx_call()
        raise NotImplementedError(
            "%s unavailable (down_history_data unavailable; ContextInfo has no %s)"
            % (func_name, func_name)
        )

    def download_history_data(self, stock_code, period, start_time="", end_time="", incrementally=None):
        def _via_context():
            kwargs = {"stock_code": stock_code, "period": period, "start_time": start_time, "end_time": end_time}
            if incrementally is not None:
                kwargs["incrementally"] = incrementally
            return self._call_context("download_history_data", **kwargs)

        sdk_kwargs = {"incrementally": incrementally} if incrementally is not None else {}
        return self._download(
            "download_history_data", (stock_code, period, start_time, end_time), sdk_kwargs, _via_context
        )

    def download_history_data2(self, stock_list, period, start_time="", end_time="", incrementally=None):
        stock_list = _as_list(stock_list)

        def _via_context():
            kwargs = {"stock_list": stock_list, "period": period, "start_time": start_time, "end_time": end_time}
            if incrementally is not None:
                kwargs["incrementally"] = incrementally
            return self._call_context("download_history_data2", **kwargs)

        sdk_kwargs = {"incrementally": incrementally} if incrementally is not None else {}
        return self._download(
            "download_history_data2", (stock_list, period, start_time, end_time), sdk_kwargs, _via_context
        )

    def get_trading_dates(self, market, start_time="", end_time="", count=-1):
        # xtdata SDK signature: get_trading_dates(market, start_time, end_time, count)
        # ContextInfo stub signature: get_trading_dates(stockcode, start_date, end_date, count, period)
        # — note the FIRST argument differs (market vs stockcode). Every caller in
        # this codebase passes a market code, so the xtdata SDK is the correct path.
        def _via_context():
            # ContextInfo 需要证券代码而不是市场代码；SH/SZ 使用代表指数，
            # 调用方已经传入完整代码时保持原值。
            context_stock = {"SH": "000001.SH", "SZ": "399001.SZ"}.get(str(market).upper(), market)
            return self._call_context("get_trading_dates", context_stock, start_time, end_time, count)

        return self._native_or_context(
            "get_trading_dates", _via_context, market, start_time, end_time, count
        )

    def get_holidays(self):
        """Return the holiday (non-trading) date list.

        Authoritative source is the xtdata SDK (xtdata.py line 1197). In a Big
        QMT (full terminal) process the SDK is present but cannot reach its
        quote service, and ContextInfo has no get_holidays method. In that
        case we derive the holidays from the A-share trading calendar: any
        weekday in a recent window that is NOT a trading day is a holiday.
        This is slower than the SDK (it walks the calendar) but correct.
        """
        def _via_context():
            return self._call_context("get_holidays")

        try:
            result = self._native_or_context("get_holidays", _via_context)
            if result:
                return result
        except Exception:
            pass
        # Big QMT fallback: derive holidays from the trading calendar.
        return self._holidays_from_trading_calendar()

    def _holidays_from_trading_calendar(self, years_back=1):
        """Derive holiday dates (YYYYMMDD strings) from trading dates.

        Walks business days across [today - years_back, today] and collects
        those that are absent from the A-share trading calendar. Requires
        get_trading_dates to work (it does in Big QMT via ContextInfo).
        """
        import datetime

        try:
            trading = set(str(d) for d in (self.get_trading_dates("SH", "", "", -1) or []))
        except Exception:
            return []
        today = datetime.date.today()
        start = today.replace(year=today.year - years_back, month=1, day=1)
        holidays = []
        cur = start
        one_day = datetime.timedelta(days=1)
        while cur <= today:
            if cur.weekday() < 5:  # Mon-Fri
                ymd = cur.strftime("%Y%m%d")
                if ymd not in trading:
                    holidays.append(ymd)
            cur += one_day
        return holidays

    def download_holiday_data(self, incrementally=True):
        def _via_context():
            return self._call_context("download_holiday_data", incrementally=incrementally)

        module = self._native()
        if module is not None and hasattr(module, "download_holiday_data"):
            try:
                return module.download_holiday_data(incrementally)
            except TypeError:
                # older SDKs may not accept the keyword
                return module.download_holiday_data()
        return _via_context()

    def get_ipo_info(self, start_time="", end_time=""):
        return self._call_context("get_ipo_info", start_time, end_time)

    def get_etf_info(self):
        # xtdata SDK 函数（SDK 893 行），ContextInfo 无此方法，走 native SDK。
        def _via_context():
            return self._raise_unavailable("get_etf_info")
        return self._native_or_context("get_etf_info", _via_context)

    def download_etf_info(self):
        return self._call_context("download_etf_info")

    def get_option_list(self, undl_code, dedate, opttype="", isavailavle=False):
        return self._call_context("get_option_list", undl_code, dedate, opttype, isavailavle)

    def get_his_option_list(self, undl_code, dedate):
        return self._call_context("get_his_option_list", undl_code, dedate)

    def get_his_option_list_batch(self, undl_code, start_time="", end_time=""):
        return self._call_context("get_his_option_list_batch", undl_code, start_time, end_time)

    def get_financial_data(self, stock_list, table_list=None, start_time="", end_time="", report_type="report_time"):
        # ContextInfo stub signature: get_financial_data(fieldList, stockList, startDate, endDate, report_type)
        # — fieldList (table_list) comes FIRST, stockList SECOND. Our public API keeps
        # the xtdata order (stock_list, table_list) so callers don't change, but we
        # must swap when forwarding to ContextInfo.
        # Big QMT also wants dotted "BIGTABLE.field" entries, not MiniQMT table
        # names — translate whole-table names to the full field list (issue #52).
        return self._call_context(
            "get_financial_data",
            _translate_financial_fields(table_list),
            stock_list,
            start_time,
            end_time,
            report_type,
        )

    def download_financial_data(self, stock_list, table_list=None, start_time="", end_time="", incrementally=None):
        # download_financial_data is an xtdata SDK function, not a ContextInfo method.
        # Try native SDK first, fall back to ContextInfo (may raise NotImplementedError).
        kwargs = {
            "stock_list": stock_list,
            "table_list": table_list or [],
            "start_time": start_time,
            "end_time": end_time,
        }
        if incrementally is not None:
            kwargs["incrementally"] = incrementally
        def _via_context():
            return self._call_context("download_financial_data", **kwargs)
        return self._native_or_context("download_financial_data", _via_context, **kwargs)

    def download_financial_data2(self, stock_list, table_list=None, start_time="", end_time=""):
        # download_financial_data2 is an xtdata SDK function, not a ContextInfo method.
        def _via_context():
            return self._call_context("download_financial_data2", stock_list, table_list or [], start_time, end_time)
        return self._native_or_context(
            "download_financial_data2", _via_context, stock_list, table_list or [], start_time, end_time
        )

    # Well-known sector names that Big QMT's ContextInfo recognises for
    # get_stock_list_in_sector / get_sector. Used as a fallback when the full
    # sector list is not enumerable (Big QMT has no get_sector_list method and
    # the xtdata SDK's quote service is unreachable inside the full terminal).
    _FALLBACK_SECTORS = (
        "沪深A股", "沪市A股", "深市A股", "科创板", "创业板",
        "上证期权", "深证期权", "中金所",
        "沪市债券", "深市债券",
        "沪市基金", "深市基金", "沪深ETF",
    )

    def get_sector_list(self, allow_fallback=False):
        """Return the terminal's sector names, or say it cannot (issue #143).

        The authoritative source is the xtdata SDK. Inside a Big QMT full
        terminal the SDK is present but cannot reach its quote service
        ("无法连接行情服务"), and ContextInfo has no get_sector_list at all --
        so on this class of terminal there is no real answer.

        This used to return ``_FALLBACK_SECTORS`` in that case: 13 curated
        names, indistinguishable from a real listing. The caller cannot tell,
        and a user's own sectors never appear no matter how many they created.
        I gave a wrong answer on issue #130 by reading exactly that list and
        believing it, which is the whole argument for raising instead.

        Pass ``allow_fallback=True`` to opt into the curated names -- they are
        still useful for driving ``get_stock_list_in_sector``, which does work.
        Asking for them is fine; being handed them unasked is not.
        """
        def _via_context():
            return self._call_context("get_sector_list")

        try:
            result = self._native_or_context("get_sector_list", _via_context)
            if result:
                return result
        except Exception:
            pass
        # A JSON-RPC caller sends "true"/"1"; a Python caller sends True.
        if str(allow_fallback).strip().lower() in ("1", "true", "yes", "on"):
            return list(self._FALLBACK_SECTORS)
        raise NotImplementedError(
            "get_sector_list cannot enumerate this terminal's sectors: the "
            "native xtdata SDK is present but its quote service is unreachable "
            "from inside Big QMT, and ContextInfo has no get_sector_list. It "
            "used to answer with a hardcoded list of %d well-known names, "
            "which looks exactly like a real listing and never contains your "
            "own sectors (issue #143). Pass allow_fallback=True to get those "
            "names deliberately -- get_stock_list_in_sector works with them."
            % len(self._FALLBACK_SECTORS))

    def get_sector_info(self, sector_name=""):
        # xtdata SDK 函数，ContextInfo 无此方法，走 native SDK。
        def _via_context():
            return self._raise_unavailable("get_sector_info")
        return self._native_or_context("get_sector_info", _via_context, sector_name)

    def get_markets(self):
        # No such function exists in either ContextInfo or the xtdata SDK.
        # MiniQMT-only convenience; synthesize from the known A-share markets.
        return list(MARKET_CODES)

    def get_market_last_trade_date(self, market):
        # No such function exists in either ContextInfo or the xtdata SDK.
        # Derive it from get_trading_dates(market, count=1) — last entry.
        try:
            dates = self.get_trading_dates(market, "", "", 1) or []
        except Exception:
            dates = []
        if not dates:
            return None
        # xtdata returns millisecond timestamps (long list); take the last one.
        try:
            return dates[-1]
        except Exception:
            return None

    def call_formula(self, formula_name, stock_code, period, start_time="", end_time="", count=-1, dividend_type=None, extend_param=None):
        return self._call_context(
            "call_formula",
            formula_name,
            stock_code,
            period,
            start_time,
            end_time,
            count,
            dividend_type,
            extend_param or {},
        )

    def subscribe_formula(self, formula_name, stock_code, period, start_time="", end_time="", count=-1, dividend_type=None, extend_param=None):
        return self._call_context(
            "subscribe_formula",
            formula_name,
            stock_code,
            period,
            start_time,
            end_time,
            count,
            dividend_type,
            extend_param or {},
        )

    def unsubscribe_formula(self, request_id):
        return self._call_context("unsubscribe_formula", request_id)

    def get_formula_result(self, request_id, start_time="", end_time="", count=-1, timeout_second=-1):
        return self._call_context("get_formula_result", request_id, start_time, end_time, count, timeout_second)

    def gen_factor_index(self, data_name, formula_name, vars, sector_list, start_time="", end_time="", period="1d", dividend_type="none"):
        return self._call_context(
            "gen_factor_index",
            data_name,
            formula_name,
            vars,
            sector_list,
            start_time,
            end_time,
            period,
            dividend_type,
        )

    # ------------------------------------------------------------------
    # 龙虎榜 / 股东 / 换手率（参考 Rockyzsu/QMT 暴露的 ContextInfo 方法）
    # 签名严格按 _PyContextInfo.py 桩核对，避免参数错位。
    # ------------------------------------------------------------------

    def get_longhubang(self, stock_list=None, start_time="", end_time="", count=-1):
        # ContextInfo stub: get_longhubang(stock_list=[], startTime='', endTime='', count=-1)
        # 桩里有特殊逻辑：endTime 传 int 时当作 count + endTime=startTime + startTime='0'。
        # 我们直接按 4 参数语义透传，避免触发桩的 int 歧义分支。
        return self._call_context(
            "get_longhubang",
            list(stock_list or []),
            start_time,
            end_time,
            count,
        )

    def get_top10_share_holder(self, stock_list, data_name, start_time, end_time, report_type="report_time"):
        # ContextInfo stub: get_top10_share_holder(stock_list, data_name, start_time, end_time, report_type='report_time')
        # data_name 只接受 'holder' 或 'flow_holder'；report_type 只接受 'report_time' 或 'announce_time'。
        return self._call_context(
            "get_top10_share_holder",
            stock_list,
            data_name,
            start_time,
            end_time,
            report_type,
        )

    def get_holder_num(self, stock_list=None, start_time="", end_time="", report_type="report_time"):
        # ContextInfo stub: get_holder_num(stock_list=[], startTime='', endTime='', report_type='report_time')
        # 返回股东户数 DataFrame。
        return self._call_context(
            "get_holder_num",
            list(stock_list or []),
            start_time,
            end_time,
            report_type,
        )

    def get_turnover_rate(self, stock_code=None, start_time="19720101", end_time="22010101"):
        # ContextInfo stub: get_turnover_rate(stock_code=[], start_time='19720101', end_time='22010101')
        # 注意：start_time/end_time 必须是 8 位日期串（YYYYMMDD），否则返回空 DataFrame。
        return self._call_context(
            "get_turnover_rate",
            list(stock_code or []),
            start_time,
            end_time,
        )

    def get_industry(self, industry_name):
        # ContextInfo stub: get_industry(industry_name, real_timetag = -1)
        # 注意桩签名有第二个可选参数 real_timetag，默认 -1（最新）。
        return self._call_context("get_industry", industry_name, -1)

    def get_close_price(self, market, stock_code, real_timetag, period=86400000, divid_type=0):
        # ContextInfo stub: get_close_price(market, stockCode, realTimetag, period=86400000, dividType=0)
        return self._call_context("get_close_price", market, stock_code, real_timetag, period, divid_type)

    # ------------------------------------------------------------------
    # 期权定价（BSM）/ 隐含波动率
    # ------------------------------------------------------------------

    def bsm_price(self, opt_type, target_price, strike_price, risk_free, sigma, days, dividend=0):
        # ContextInfo stub: bsm_price(optType, targetPrice, strikePrice, riskFree, sigma, days, dividend=0)
        # opt_type: 'C'(call) / 'P'(put)。target_price 可为 list（批量）。
        return self._call_context(
            "bsm_price",
            opt_type,
            target_price,
            strike_price,
            risk_free,
            sigma,
            days,
            dividend,
        )

    def bsm_iv(self, opt_type, target_price, strike_price, option_price, risk_free, days, dividend=0):
        # ContextInfo stub: bsm_iv(optType, targetPrice, strikePrice, optionPrice, riskFree, days, dividend=0)
        return self._call_context(
            "bsm_iv",
            opt_type,
            target_price,
            strike_price,
            option_price,
            risk_free,
            days,
            dividend,
        )

    def get_option_iv(self, opt_code):
        # ContextInfo stub: get_option_iv(opt_code) — 计算单只期权的隐含波动率。
        return self._call_context("get_option_iv", opt_code)

    def get_option_detail_data(self, stockcode):
        # ContextInfo stub: get_option_detail_data(stockcode)
        return self._call_context("get_option_detail_data", stockcode)

    def get_option_undl_data(self, undl_code_ref=""):
        # ContextInfo stub: get_option_undl_data(undl_code_ref='') — 标的下所有期权。
        # 传空串返回全市场期权-标的映射 dict。
        return self._call_context("get_option_undl_data", undl_code_ref)

    def get_option_undl(self, opt_code):
        # ContextInfo stub: get_option_undl(opt_code) — 期权的标的代码。
        return self._call_context("get_option_undl", opt_code)

    # ------------------------------------------------------------------
    # 财务扩展 / 因子数据
    # ------------------------------------------------------------------

    def get_raw_financial_data(self, field_list, stock_list, start_time, end_time, report_type="report_time", data_type="dict"):
        # ContextInfo stub: get_raw_financial_data(fieldList, stockList, startDate, endDate, report_type='report_time', data_type='dict')
        # 返回原始财务数据（未做字段对齐），data_type 可为 'dict'/'frame'。
        return self._call_context(
            "get_raw_financial_data",
            field_list,
            stock_list,
            start_time,
            end_time,
            report_type,
            data_type,
        )

    def get_factor_data(self, field_list, stock_list, start_date, end_date):
        # ContextInfo stub: get_factor_data(field_list, stock_list, start_date, end_date)
        # 返回因子库数据。
        return self._call_context(
            "get_factor_data",
            field_list,
            stock_list,
            start_date,
            end_date,
        )

    # ------------------------------------------------------------------
    # 历史 ST / 指数权重
    # ------------------------------------------------------------------

    def get_his_st_data(self, stock_code):
        # ContextInfo stub: get_his_st_data(stockCode) — 历史 ST 状态。
        return self._call_context("get_his_st_data", stock_code)

    def get_his_index_data(self, stock_code):
        # ContextInfo stub: get_his_index_data(stockCode) — 历史指数权重。
        return self._call_context("get_his_index_data", stock_code)

    # ------------------------------------------------------------------
    # 期货 / 合约
    # ------------------------------------------------------------------

    def get_main_contract(self, code_market):
        # ContextInfo stub: get_main_contract(codemarket)
        return self._call_context("get_main_contract", code_market)

    def get_his_contract_list(self, market):
        # ContextInfo stub: get_his_contract_list(market)
        return self._call_context("get_his_contract_list", market)

    def get_date_location(self, date):
        # ContextInfo stub: get_date_location(date) — 日期在交易日历中的位置。
        return self._call_context("get_date_location", date)

    def get_ETF_list(self, market, stock_code, type_list=None):
        # ContextInfo stub: get_ETF_list(market, stockcode, typeList=[])
        return self._call_context("get_ETF_list", market, stock_code, list(type_list or []))

    # ------------------------------------------------------------------
    # 北向资金 / 港股通
    # ------------------------------------------------------------------

    def get_north_finance_change(self, period):
        # ContextInfo stub: get_north_finance_change(period) — 北向资金流入流出。
        return self._call_context("get_north_finance_change", period)

    def get_hkt_statistics(self, stock_code):
        # ContextInfo stub: get_hkt_statistics(stock_code) — 港股通统计。
        return self._call_context("get_hkt_statistics", stock_code)

    def get_hkt_details(self, stock_code):
        # ContextInfo stub: get_hkt_details(stock_code) — 港股通明细。
        return self._call_context("get_hkt_details", stock_code)

    # ------------------------------------------------------------------
    # 自定义板块写入（issue #143）
    #
    # 三条通道都枚举过（probe_capabilities 的 sector_probe 块）：
    #
    #   ContextInfo        create_sector / get_sector / get_stock_list_in_sector
    #   QMT 注入的全局函数  一个都没有
    #   原生 xtdata SDK     add_sector / remove_sector / get_sector_list
    #
    # 文档 §4.7 记的那一族（create_sector_folder / add_stock_to_sector /
    # reset_sector_stock_list / remove_stock_from_sector）在三条通道上都不
    # 存在 —— 不是「还没实现」，是这台终端给不出来。所以它们在这里用
    # add_sector 组合出来，而不是去找一个不存在的原生函数。
    #
    # 而 ContextInfo.create_sector 存在、能调、返回 None、什么都不做：实测
    # 前后都是 13 个板块，新板块一个没建。所以每一次写入之后都回读校验。
    # 宁可把一次成功的写入误报成失败，也不能再让调用方以为建好了 —— #142
    # 就是这么来的，而静默的错比响亮的错难查得多。
    # ------------------------------------------------------------------

    _SECTOR_WRITE_UNAVAILABLE = (
        "%s cannot be performed on this terminal: the native xtdata sector API "
        "(add_sector/remove_sector) is present but its quote service is "
        "unreachable from inside Big QMT (\"无法连接行情服务\"), and Big QMT's "
        "own ContextInfo exposes only create_sector, which accepts the call and "
        "silently does nothing. Run probe_capabilities and read sector_probe to "
        "see which channels this terminal has (issue #143)."
    )

    @staticmethod
    def _sector_code_key(code):
        """Compare membership without tripping over case or spacing."""
        text = str(code or "").strip()
        if not text:
            return ""
        try:
            return normalize_stock_code(text)
        except Exception:
            return text.upper()

    def _sector_members(self, sector_name):
        """Current members, or [] when the sector does not exist yet.

        Read-only and uncached: the caller is about to write, so the cached
        listing used by _sector_codes would be exactly the wrong answer.
        """
        try:
            return [str(code) for code in (
                self.get_stock_list_in_sector(sector_name) or [])]
        except Exception:
            return []

    def _write_sector(self, method_name, sector_name, stock_list):
        """Push a full member list, preferring the only channel that works.

        Returns whatever the channel returned; the caller verifies. Raises
        NotImplementedError when no channel exists at all, so "impossible" and
        "attempted but ineffective" stay distinguishable.
        """
        codes = [str(code) for code in (stock_list or [])]
        module = self._native()
        native_error = None
        if module is not None and callable(getattr(module, "add_sector", None)):
            try:
                return module.add_sector(sector_name, codes)
            except Exception as exc:
                native_error = exc
        context_info = getattr(self, "context_info", None)
        if callable(getattr(context_info, "create_sector", None)):
            # Known no-op on Big QMT 2.1.19.0 -- tried anyway because another
            # build may honour it, and the caller's verify step catches it
            # either way.
            return self._call_context("create_sector", sector_name, codes)
        raise NotImplementedError(
            (self._SECTOR_WRITE_UNAVAILABLE % method_name)
            + ("" if native_error is None
               else " Native attempt failed with: %s: %s"
                    % (native_error.__class__.__name__, native_error)))

    def _verify_sector_members(self, method_name, sector_name,
                               must_contain=(), must_not_contain=()):
        """Read the sector back and confirm the write actually landed."""
        members = {self._sector_code_key(code)
                   for code in self._sector_members(sector_name)}
        missing = [code for code in must_contain
                   if self._sector_code_key(code) not in members]
        lingering = [code for code in must_not_contain
                     if self._sector_code_key(code) in members]
        if missing or lingering:
            detail = []
            if missing:
                detail.append("still missing %s" % (missing[:5],))
            if lingering:
                detail.append("still present %s" % (lingering[:5],))
            raise RuntimeError(
                "%s on sector %r reported no error but the sector did not "
                "change (%s). Big QMT's ContextInfo.create_sector accepts the "
                "call and does nothing; this terminal has no working sector "
                "write channel (issue #143)."
                % (method_name, sector_name, "; ".join(detail)))
        return True

    def create_sector(self, sector_name, stock_list):
        """Create (or overwrite) a custom sector and confirm it exists.

        Signature deliberately NOT the ``(parent_node, sector_name, overwrite)``
        form in docs §4.7: that function is absent from all three channels on
        every terminal probed so far, while ``add_sector(name, stock_list)`` is
        the shape the real SDK offers. Adopting a signature for a function that
        does not exist would trade one wrong answer for another.
        """
        codes = [str(code) for code in (stock_list or [])]
        self._write_sector("create_sector", sector_name, codes)
        self._verify_sector_members("create_sector", sector_name, must_contain=codes)
        return sector_name

    def reset_sector_stock_list(self, sector, stock_list):
        """Replace a sector's members outright."""
        codes = [str(code) for code in (stock_list or [])]
        self._write_sector("reset_sector_stock_list", sector, codes)
        self._verify_sector_members("reset_sector_stock_list", sector,
                                    must_contain=codes)
        return True

    def add_stock_to_sector(self, sector, stock_code):
        """Add one code, keeping the existing members.

        Read-merge-write rather than a bare append, so it is correct whether
        the underlying channel replaces the list or merges into it -- the two
        SDK generations disagree about that and this terminal cannot be asked.
        """
        code = str(stock_code or "").strip()
        if not code:
            raise ValueError("stock_code is required")
        members = self._sector_members(sector)
        if self._sector_code_key(code) in {self._sector_code_key(m) for m in members}:
            return True
        self._write_sector("add_stock_to_sector", sector, members + [code])
        self._verify_sector_members("add_stock_to_sector", sector,
                                    must_contain=[code])
        return True

    def remove_stock_from_sector(self, sector, stock_code):
        """Drop one code, keeping the rest.

        The verify step matters most here: if the channel merges instead of
        replacing, the write "succeeds" and the code stays -- silently, which
        is the failure mode this whole family is being fixed for.
        """
        code = str(stock_code or "").strip()
        if not code:
            raise ValueError("stock_code is required")
        wanted = self._sector_code_key(code)
        members = self._sector_members(sector)
        remaining = [m for m in members if self._sector_code_key(m) != wanted]
        if len(remaining) == len(members):
            return True                      # not a member; nothing to do
        self._write_sector("remove_stock_from_sector", sector, remaining)
        self._verify_sector_members("remove_stock_from_sector", sector,
                                    must_not_contain=[code])
        return True

    def create_sector_folder(self, parent_node, folder_name, overwrite=False):
        """No channel on any probed terminal offers this."""
        raise NotImplementedError(
            self._SECTOR_WRITE_UNAVAILABLE % "create_sector_folder")

    # ------------------------------------------------------------------
    # 基础查询辅助
    # ------------------------------------------------------------------

    def get_stock_name(self, stock):
        # ContextInfo stub: get_stock_name(stock)
        return self._call_context("get_stock_name", stock)

    def get_stock_type(self, stock):
        # ContextInfo stub: get_stock_type(stock)
        return self._call_context("get_stock_type", stock)

    def get_last_close(self, stock):
        # ContextInfo stub: get_last_close(stock)
        return self._call_context("get_last_close", stock)

    def get_last_volume(self, stock):
        # ContextInfo stub: get_last_volume(stock)
        return self._call_context("get_last_volume", stock)

    def get_open_date(self, stock):
        # ContextInfo stub: get_open_date(stock) — 上市日期。
        return self._call_context("get_open_date", stock)

    def get_contract_expire_date(self, stock):
        # ContextInfo stub: get_contract_expire_date(stock) — 到期日。
        return self._call_context("get_contract_expire_date", stock)

    def get_contract_multiplier(self, stockcode):
        # ContextInfo stub: get_contract_multiplier(stockcode) — 合约乘数。
        return self._call_context("get_contract_multiplier", stockcode)

    def get_float_caps(self, stockcode):
        # ContextInfo stub: get_float_caps(stockcode) — 流通市值。
        return self._call_context("get_float_caps", stockcode)

    def get_total_share(self, stockcode):
        # ContextInfo stub: get_total_share(stockcode) — 总股本。
        return self._call_context("get_total_share", stockcode)

    def get_turn_over_rate(self, stockcode):
        # ContextInfo stub: get_turn_over_rate(stockcode) — 换手率（单值版，区别于上面的 get_turnover_rate 区间版）。
        return self._call_context("get_turn_over_rate", stockcode)

    def get_weight_in_index(self, mtkindexcode, stockcode):
        # ContextInfo stub: get_weight_in_index(mtkindexcode, stockcode) — 指数中权重。
        return self._call_context("get_weight_in_index", mtkindexcode, stockcode)

    def get_svol(self, stock):
        # ContextInfo stub: get_svol(stock)
        return self._call_context("get_svol", stock)

    def get_bvol(self, stock):
        # ContextInfo stub: get_bvol(stock)
        return self._call_context("get_bvol", stock)

    def get_risk_free_rate(self, index=-1):
        # ContextInfo stub: get_risk_free_rate(index) — 无风险利率。
        return self._call_context("get_risk_free_rate", index)

    # ------------------------------------------------------------------
    # L2 行情（需 L2 权限）
    # ------------------------------------------------------------------

    def get_l2_quote(self, field_list=None, stock_code="", start_time="", end_time="", count=-1):
        # xtdata SDK: get_l2_quote(field_list=[], stock_code='', start_time='', end_time='', count=-1)
        # ContextInfo 无此方法；走原生 xtdata SDK，连不上则 NotImplementedError。
        return self._native_or_context(
            "get_l2_quote",
            lambda: self._raise_unavailable("get_l2_quote"),
            list(field_list or []), stock_code, start_time, end_time, count,
        )

    def get_l2_order(self, field_list=None, stock_code="", start_time="", end_time="", count=-1):
        # xtdata SDK: get_l2_order(...) — L2 逐笔委托。
        return self._native_or_context(
            "get_l2_order",
            lambda: self._raise_unavailable("get_l2_order"),
            list(field_list or []), stock_code, start_time, end_time, count,
        )

    def get_l2_transaction(self, field_list=None, stock_code="", start_time="", end_time="", count=-1):
        # xtdata SDK: get_l2_transaction(...) — L2 逐笔成交。
        return self._native_or_context(
            "get_l2_transaction",
            lambda: self._raise_unavailable("get_l2_transaction"),
            list(field_list or []), stock_code, start_time, end_time, count,
        )

    def subscribe_l2thousand(self, stock_code, gear_num=0, callback=None):
        # xtdata SDK: subscribe_l2thousand(stock_code, gear_num=0, callback=None) — 千档盘口订阅。
        # callback 在 RPC 模型下无意义（无回调通道），忽略。
        module = self._native()
        if module is not None and hasattr(module, "subscribe_l2thousand"):
            try:
                return module.subscribe_l2thousand(stock_code, gear_num, callback)
            except Exception:
                pass
        return self._raise_unavailable("subscribe_l2thousand")

    # ------------------------------------------------------------------
    # 指数权重 / 交易日历 / 交易时段 / 可转债
    # ------------------------------------------------------------------

    def get_index_weight(self, index_code):
        # xtdata SDK: get_index_weight(index_code) — 指数成分权重。
        # ContextInfo 有 get_weight_in_index(indexcode, stockcode) 但语义不同（单股权重）。
        return self._native_or_context(
            "get_index_weight",
            lambda: self._raise_unavailable("get_index_weight"),
            index_code,
        )

    def get_trading_calendar(self, market, start_time="", end_time="", tradetimes=False):
        # xtdata SDK: get_trading_calendar(market, start_time='', end_time='', tradetimes=False)
        # ContextInfo 无此方法。SDK 不可用时从 get_trading_dates 派生（不含 tradetimes 时段）。
        def _fallback():
            try:
                dates = self.get_trading_dates(market, start_time, end_time, -1) or []
                return [str(d) for d in dates]
            except Exception:
                return self._raise_unavailable("get_trading_calendar")
        return self._native_or_context(
            "get_trading_calendar", _fallback, market, start_time, end_time, tradetimes
        )

    def get_trade_times(self, stockcode):
        # xtdata SDK: get_trade_times(stockcode) — 日内交易时段。
        # 传市场（'SH'）或代码（'600000.SH'）。返回 [[开始,结束,类型], ...]。
        return self._native_or_context(
            "get_trade_times",
            lambda: self._raise_unavailable("get_trade_times"),
            stockcode,
        )

    def get_cb_info(self, stockcode):
        # xtdata SDK: get_cb_info(stockcode) — 可转债信息。
        return self._native_or_context(
            "get_cb_info",
            lambda: self._raise_unavailable("get_cb_info"),
            stockcode,
        )

    def is_stock_type(self, stock, tag):
        # xtdata SDK: is_stock_type(stock, tag) — 品种判断（tag 如 'stock'/'fund'/'bond'）。
        # ContextInfo 有 is_stock/is_fund/is_future 但签名不同，这里走 SDK。
        return self._native_or_context(
            "is_stock_type",
            lambda: self._raise_unavailable("is_stock_type"),
            stock, tag,
        )

    # ------------------------------------------------------------------
    # 板块增删（自定义板块管理）
    # ------------------------------------------------------------------

    def add_sector(self, sector_name, stock_list):
        """xtdata SDK ``add_sector(sector_name, stock_list)``.

        Used to swallow the native failure and fall through to
        ContextInfo.create_sector -- which does nothing, so on Big QMT this was
        a silent no-op too (issue #143). It now goes through the same
        write-then-verify path as the rest of the family.
        """
        codes = [str(code) for code in (stock_list or [])]
        self._write_sector("add_sector", sector_name, codes)
        self._verify_sector_members("add_sector", sector_name, must_contain=codes)
        return True

    def remove_sector(self, sector_name):
        """xtdata SDK ``remove_sector(sector_name)`` -- delete a custom sector.

        No ContextInfo equivalent exists (`remove_sector` is absent there), so
        this is the one member of the family with a single channel.
        """
        module = self._native()
        if module is not None and callable(getattr(module, "remove_sector", None)):
            try:
                return module.remove_sector(sector_name)
            except Exception as exc:
                raise RuntimeError(
                    "remove_sector(%r) failed on the native xtdata SDK: %s: %s"
                    % (sector_name, exc.__class__.__name__, exc))
        raise NotImplementedError(
            self._SECTOR_WRITE_UNAVAILABLE % "remove_sector")

    # ------------------------------------------------------------------
    # 数据下载扩展
    # ------------------------------------------------------------------

    def download_cb_data(self):
        # xtdata SDK: download_cb_data() — 下载可转债数据。
        module = self._native()
        if module is not None and hasattr(module, "download_cb_data"):
            try:
                return module.download_cb_data()
            except Exception:
                pass
        return self._raise_unavailable("download_cb_data")

    def download_history_contracts(self):
        # xtdata SDK: download_history_contracts() — 下载过期合约数据。
        module = self._native()
        if module is not None and hasattr(module, "download_history_contracts"):
            try:
                return module.download_history_contracts()
            except Exception:
                pass
        return self._raise_unavailable("download_history_contracts")

    def download_index_weight(self):
        # xtdata SDK: download_index_weight() — 下载指数权重数据。
        module = self._native()
        if module is not None and hasattr(module, "download_index_weight"):
            try:
                return module.download_index_weight()
            except Exception:
                pass
        return self._raise_unavailable("download_index_weight")

    def download_sector_data(self):
        # xtdata SDK: download_sector_data() — 下载行业板块数据。
        module = self._native()
        if module is not None and hasattr(module, "download_sector_data"):
            try:
                return module.download_sector_data()
            except Exception:
                pass
        return self._raise_unavailable("download_sector_data")

    # ------------------------------------------------------------------
    # 时间戳转换（纯计算，无需 QMT，服务端本地实现）
    # ------------------------------------------------------------------

    @staticmethod
    def datetime_to_timetag(datetime_str, format="%Y%m%d%H%M%S"):
        # xtdata SDK: datetime_to_timetag(datetime, format="%Y%m%d%H%M%S")
        # 把日期时间字符串转成毫秒时间戳。纯本地计算。
        import datetime as _dt
        try:
            dt = _dt.datetime.strptime(str(datetime_str), format)
            return int(dt.timestamp() * 1000)
        except Exception:
            return 0

    @staticmethod
    def timetag_to_datetime(timetag, format):
        # xtdata SDK: timetag_to_datetime(timetag, format) — 毫秒时间戳转字符串。
        import datetime as _dt
        try:
            dt = _dt.datetime.fromtimestamp(int(timetag) / 1000.0)
            return dt.strftime(format)
        except Exception:
            return ""

    @staticmethod
    def _raise_unavailable(method_name):
        raise NotImplementedError(
            "%s is unavailable: needs native xtdata SDK quote service "
            "(not reachable in Big QMT full terminal)" % method_name
        )
