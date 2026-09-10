"""MiniQMT-style client objects backed by Big QMT Redis RPC.

This module is the replacement edge for existing code that already calls
``xt_trader.query_stock_positions(...)`` or ``xtdata.get_full_tick(...)``.
The Big QMT process remains the only place that touches QMT runtime APIs.
"""

import os
import json
import time
import uuid
import queue as _queue
from collections import OrderedDict as _OrderedDict
import threading
import importlib
import datetime as _dt
from typing import Any, Dict, Iterable, List, Optional
# Only these three are used below, but every public constant is re-exported
# further down: docs/XTQUANT_COMPAT_REPLACEMENT.md tells callers to do
# ``from bigqmt_signal_trader import xtquant_compat as xtconstant`` and read
# e.g. ``xtconstant.ORDER_SUCCEEDED`` off this module.
#
# This is deliberately not ``from xtquant.xtconstant import *``. That form is a
# SyntaxError ("import * only allowed at module level") in the single-file QMT
# builds, which exec each module inside a function body (issue #76).
from xtquant import xtconstant as _xtconstant
from xtquant.xtconstant import ORDER_UNKNOWN, STOCK_BUY, STOCK_SELL
from xtquant.xttype import StockAccount

from .full_tick_cache import request_full_tick_cache, wait_full_tick_cache
from .local_cache import LocalMarketCache
from .order_id import OrderId, order_sys_id_of
from .redis_rpc import TYPED_PAYLOAD_FLAG, call_redis_rpc
from .logging_setup import get_logger

log = get_logger("xtquant_compat")


# Re-export every public xtconstant name on this module, replacing what
# ``import *`` used to do implicitly. Before #73 these 110-odd constants were
# defined here outright, and the documented "approach 1" migration path binds
# this module as ``xtconstant``, so dropping them would break callers that read
# e.g. ``xtquant_compat.FIX_PRICE``.
#
# Written as an explicit loop rather than ``import *`` (a SyntaxError inside the
# single-file builds' function-scope exec, issue #76) and rather than a
# module-level ``__getattr__`` (PEP 562, Python 3.7+, while QMT ships 3.6).
for _const_name in dir(_xtconstant):
    if not _const_name.startswith("_"):
        globals().setdefault(_const_name, getattr(_xtconstant, _const_name))
del _const_name


# Default OHLCV fields pulled + cached by get_local_data fallback_rpc.
# MiniQMT documents an empty field list as "all fields", including ``time`` and
# ``openInterest`` for K-lines.  Cache fills deliberately use a bounded subset
# instead, but omitting those two standard fields leaves the resulting frame
# observably incompatible with MiniQMT consumers.
DEFAULT_DOWNLOAD_FIELDS = [
    "time", "open", "high", "low", "close", "volume", "amount", "openInterest",
]
# Codes per get_market_data_ex request. One request carries a single RPC timeout,
# so a wide stock_list either fits or loses everything (issue #47).
DEFAULT_MARKET_DATA_CHUNK = 100
_TIME_COL_NAMES = ("stime", "time", "index", "date", "datetime", "timetag")


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return list(value)



CLIENT_CONFIG_MODULE_ENV = "BIGQMT_CLIENT_CONFIG_MODULE"
DEFAULT_CLIENT_CONFIG_MODULES = (
    "bigqmt_signal_trader_client_config",
    "bigqmt_signal_trader_local_config",
)



class CompatObject:
    """Small attribute object matching xtquant's object-style returns."""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)

    def __repr__(self):
        items = ", ".join("%s=%r" % (key, value) for key, value in sorted(self.__dict__.items()))
        return "%s(%s)" % (self.__class__.__name__, items)





class XtQuantTraderCallback:
    def on_disconnected(self):
        pass

    def on_stock_order(self, order):
        pass

    def on_stock_trade(self, trade):
        pass

    def on_order_error(self, order_error):
        pass

    def on_cancel_error(self, cancel_error):
        pass

    def on_order_stock_async_response(self, response):
        pass

    def on_cancel_order_stock_async_response(self, response):
        pass

    def on_account_status(self, status):
        pass


def _env_int(name, default):
    value = os.environ.get(name)
    if value in (None, ""):
        return default
    return int(value)


def _env_float(name, default):
    value = os.environ.get(name)
    if value in (None, ""):
        return default
    return float(value)


def _env_bool(name, default=False):
    value = os.environ.get(name)
    if value in (None, ""):
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on")


def _bool_value(value, default=False):
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on")


def _import_optional_module(module_name):
    try:
        return importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name == module_name:
            return None
        raise


def _quote_client_id():
    """Process-stable client id for whole-quote subscriptions. Config or env wins;
    otherwise read/create a persisted id so a restarted client is recognised as
    the same subscriber by the server."""
    client_config = load_client_config()
    configured = client_config.get("quote_client_id") or os.environ.get("BIGQMT_QUOTE_CLIENT_ID")
    if configured:
        return str(configured)
    cache_path = os.path.join(os.path.expanduser("~"), ".cache", "bigqmt", "quote_client_id")
    try:
        with open(cache_path, "r") as handle:
            existing = handle.read().strip()
            if existing:
                return existing
    except OSError:
        pass
    new_id = uuid.uuid4().hex
    try:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(cache_path, "w") as handle:
            handle.write(new_id)
    except OSError:
        pass
    return new_id


def _quote_push_zmq_address(client):
    """Derive the server whole-quote PUB address: same host as the RPC zmq
    endpoint, RPC port + 1 (the PUB socket binds a distinct port)."""
    from .transports.zmq_transport import DEFAULT_ZMQ_HOST, _default_zmq_port

    zmq_config = dict(getattr(client, "zmq_config", {}) or {})
    explicit = zmq_config.get("quote_push_connect_address")
    if explicit:
        return str(explicit)
    host = zmq_config.get("host") or DEFAULT_ZMQ_HOST
    port = zmq_config.get("port")
    base_port = int(port) if port is not None else _default_zmq_port(client.account_id)
    return "tcp://%s:%d" % (host, base_port + 1)


def _missing_account_id_message():
    """Say what was searched and what to do, not just that something is missing.

    "Big QMT account_id is required" told the reader nothing about where the
    config was looked for, so a config file placed one sys.path away from the
    running interpreter looked identical to no config at all (issue #90).
    """
    searched = list(DEFAULT_CLIENT_CONFIG_MODULES)
    selected = os.environ.get(CLIENT_CONFIG_MODULE_ENV)
    if selected and selected not in searched:
        searched.insert(0, selected)
    found = None
    try:
        config = load_client_config()
        found = (config or {}).get("module")
    except Exception:
        pass

    if found:
        detail = ("imported %s, but it defines no BIGQMT_ACCOUNT_ID"
                  % found)
    else:
        detail = ("none of these modules could be imported: %s"
                  % ", ".join(searched))
    lines = [
        "Big QMT account_id is required -- %s." % detail,
        "Fix it in any one of these ways:",
        "  1. put bigqmt_signal_trader_client_config.py somewhere on sys.path"
        " (the current working directory counts), with BIGQMT_ACCOUNT_ID set;",
        "  2. set the BIGQMT_ACCOUNT_ID environment variable;",
        "  3. call bigqmt_signal_trader.xtquant_compat.configure(account_id=...)"
        " before use;",
        "  or run `bigqmt-init`, which writes both config files for you.",
        "configure() also runs at import time, so a config put in place after"
        " importing this module needs configure() called again.",
    ]
    return "\n".join(lines)


def load_client_config(module_name=None):
    """Load local private client config without requiring environment variables."""
    candidates = []
    selected = module_name or os.environ.get(CLIENT_CONFIG_MODULE_ENV)
    if selected:
        candidates.append(str(selected))
    candidates.extend(name for name in DEFAULT_CLIENT_CONFIG_MODULES if name not in candidates)

    for candidate in candidates:
        module = _import_optional_module(candidate)
        if module is None:
            continue
        redis_config = dict(getattr(module, "BIGQMT_REDIS_CONFIG", {}) or {})
        account_id = getattr(module, "BIGQMT_ACCOUNT_ID", None) or redis_config.get("account_id")
        timeout_seconds = getattr(module, "BIGQMT_RPC_TIMEOUT_SECONDS", None)
        if timeout_seconds is None:
            timeout_seconds = redis_config.get("rpc_timeout_seconds")
        download_wait_seconds = getattr(module, "BIGQMT_DOWNLOAD_WAIT_SECONDS", None)
        if download_wait_seconds is None:
            download_wait_seconds = redis_config.get("download_wait_seconds")
        download_poll_interval_seconds = getattr(module, "BIGQMT_DOWNLOAD_POLL_INTERVAL_SECONDS", None)
        if download_poll_interval_seconds is None:
            download_poll_interval_seconds = redis_config.get("download_poll_interval_seconds")
        full_tick_cache_config = dict(getattr(module, "BIGQMT_FULL_TICK_CACHE_CONFIG", {}) or {})
        for key in (
            "full_tick_cache_enabled",
            "full_tick_demand_ttl_seconds",
            "full_tick_cache_ttl_seconds",
            "full_tick_wait_seconds",
            "full_tick_poll_interval_seconds",
        ):
            if key in redis_config:
                full_tick_cache_config[key] = redis_config[key]
        local_cache_config = dict(getattr(module, "BIGQMT_LOCAL_CACHE_CONFIG", {}) or {})
        for key in ("local_cache_enabled", "local_cache_dir", "local_cache_fallback_rpc", "local_cache_format"):
            if key in redis_config:
                local_cache_config[key.replace("local_cache_", "")] = redis_config[key]
        formula_server_config = dict(getattr(module, "BIGQMT_FORMULA_SERVER_CONFIG", {}) or {})
        formula_server_config.update(dict(redis_config.get("formula_server") or {}))
        return {
            "module": candidate,
            "account_id": account_id,
            "redis_config": redis_config,
            "timeout_seconds": timeout_seconds,
            "download_wait_seconds": download_wait_seconds,
            "download_poll_interval_seconds": download_poll_interval_seconds,
            "full_tick_cache_config": full_tick_cache_config,
            "local_cache_config": local_cache_config,
            "formula_server_config": formula_server_config,
            "quote_client_id": getattr(module, "BIGQMT_QUOTE_CLIENT_ID", None),
        }
    return {}


def _account_id(account, fallback=""):
    if account is None:
        return str(fallback or "")
    if isinstance(account, str):
        return account
    for name in ("account_id", "m_strAccountID", "id"):
        value = getattr(account, name, None)
        if value:
            return str(value)
    if isinstance(account, dict):
        return str(account.get("account_id") or account.get("id") or fallback or "")
    return str(fallback or "")


def _action_to_order_type(action):
    text = str(action or "").upper()
    if text in ("BUY", str(STOCK_BUY)):
        return STOCK_BUY
    if text in ("SELL", str(STOCK_SELL)):
        return STOCK_SELL
    return 0


def _safe_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_unix_seconds(value, default=0):
    """Normalize a trade/order time into Unix seconds (MiniQMT semantics).

    Accepts numeric epochs, ``YYYY-MM-DD HH:MM:SS[.ffffff]`` and
    ``YYYYMMDDHHMMSS`` strings. Anything else falls back to ``default``.
    """
    if value is None or value == "":
        return default
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y%m%d%H%M%S"):
        try:
            return int(time.mktime(time.strptime(text, fmt)))
        except ValueError:
            continue
    return default


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, dict):
        return list(value.values())
    if isinstance(value, list):
        return value
    return [value]


def _account_type_name(value):
    """The NAME of an account type, whatever form it arrives in.

    StockAccount stores the numeric code, not the string it was constructed
    with: StockAccount(id, "CREDIT").account_type is 3. Comparing that against
    the server's "CREDIT" would report a mismatch on every credit account.
    """
    text = str("" if value is None else value).strip()
    if not text:
        return ""
    if text.isdigit():
        try:
            from xtquant.xtconstant import ACCOUNT_TYPE_DICT

            return str(ACCOUNT_TYPE_DICT.get(int(text), "")).strip().upper()
        except Exception:
            return ""
    return text.upper()


def _account_type_code(value):
    """The xtconstant NUMBER for an account type, whatever form it arrives in.

    The mirror of _account_type_name. XtOrder / XtTrade / XtPosition all carry
    account_type as an int (xttype sets it to SECURITY_ACCOUNT), so a name has
    to come back as a number before it reaches a caller. 0 means "nothing to
    go on" -- the caller decides what to fall back to.
    """
    text = str("" if value is None else value).strip()
    if not text:
        return 0
    try:
        return int(text)
    except (TypeError, ValueError):
        pass
    upper = text.upper()
    try:
        from xtquant import xtconstant
    except Exception:
        return 0
    # NOT ACCOUNT_TYPE_DICT alone: the xtquant that wins inside Big QMT is the
    # terminal's own bundled copy, which has 91 of this shim's 538 names and
    # does not include that dict. A client can land on it too -- appending the
    # QMT python directory to sys.path is a documented way to reach the config
    # modules. Fall back to the individual *_ACCOUNT constants, which both
    # copies have.
    table = getattr(xtconstant, "ACCOUNT_TYPE_DICT", None)
    if isinstance(table, dict):
        for code, name in table.items():
            if str(name).strip().upper() == upper:
                try:
                    return int(code)
                except (TypeError, ValueError):
                    break
    for attribute in ("%s_ACCOUNT" % upper,
                      "SECURITY_ACCOUNT" if upper == "STOCK" else ""):
        if not attribute:
            continue
        code = getattr(xtconstant, attribute, None)
        if isinstance(code, int) and not isinstance(code, bool):
            return int(code)
    return 0


def _restore_jsonable(value):
    if isinstance(value, dict):
        marker = value.get("__bigqmt_type__")
        if marker == "DataFrame":
            try:
                import pandas as pd

                return pd.DataFrame(value.get("records") or [], columns=value.get("columns") or None)
            except Exception:
                return value.get("records") or []
        if marker == "Panel":
            # pandas dropped Panel in 1.0, so a 3-D object cannot be rebuilt on
            # a modern client. It comes back as what a caller can actually use:
            # {item: DataFrame} (issue #115). The axis labels ride along for
            # anyone who needs to know how the cube was sliced.
            return {key: _restore_jsonable(item)
                    for key, item in (value.get("data") or {}).items()}
        if marker == "Series":
            try:
                import pandas as pd

                return pd.Series(value.get("data") or {})
            except Exception:
                return value.get("data") or {}
        return {key: _restore_jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_restore_jsonable(item) for item in value]
    return value


def _digits_only(value):
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _parse_qmt_stime(value):
    digits = _digits_only(value)
    if len(digits) >= 14:
        try:
            return _dt.datetime.strptime(digits[:14], "%Y%m%d%H%M%S")
        except ValueError:
            return None
    if len(digits) >= 8:
        try:
            return _dt.datetime.strptime(digits[:8], "%Y%m%d")
        except ValueError:
            return None
    return None


# FormulaServer 快照滞后检测：实测它会把 1m 数据冻结数小时（11:30 后不再
# 更新，收盘后还在发午间数据）。对直连回答的 intraday 数据做时间差检测：
# 滞后即告警 + 本次自动回落 RPC 桥拿实时数据，并在冷却期内跳过直连
# （冷却到期自动重新探测，自愈）。
_FORMULA_STALE_INTRADAY_PERIODS = ("tick", "1m", "3m", "5m", "15m", "30m", "1h")
_FORMULA_STALE_WARN_LAG_SECONDS = 30 * 60
_FORMULA_STALE_COOLDOWN_SECONDS = 120.0
_formula_stale_warned = {}
_formula_stale_until = {"ts": 0.0}


def _formula_bars_stale(data, params):
    """返回 (code, newest_dt, lag_seconds) 或 None。只检测，不告警。"""
    try:
        period = str((params or {}).get("period") or "").lower()
        if period not in _FORMULA_STALE_INTRADAY_PERIODS:
            return None
        now = _dt.datetime.now()
        today = now.date()
        for code, df in (data or {}).items():
            # 公式直连返回的帧时间轴在 stime 列（RangeIndex），
            # 兼容层归一化后的帧在索引上——两种形态都认。
            newest_value = None
            for col in ("stime", "time"):
                if col in list(getattr(df, "columns", [])):
                    newest_value = df[col].iloc[-1]
                    break
            if newest_value is None:
                index = getattr(df, "index", None)
                if index is not None and len(index):
                    newest_value = index[-1]
            newest = _parse_qmt_stime(newest_value)
            if newest is None:
                continue
            lag = (now - newest).total_seconds()
            if (newest.date() < today) or (lag > _FORMULA_STALE_WARN_LAG_SECONDS):
                return (str(code), newest, lag)
    except Exception:
        pass
    return None


def _warn_stale_formula_bars(data, params, hit=None):
    try:
        period = str((params or {}).get("period") or "").lower()
        today = _dt.datetime.now().date()
        if hit is None:
            hit = _formula_bars_stale(data, params)
        if hit is None:
            return
        code, newest, lag = hit
        key = (code, period, str(today))
        if _formula_stale_warned.get(key):
            return
        _formula_stale_warned[key] = True
        log.warning(
            "FormulaServer data looks stale for %s %s: newest bar %s lags now by %.0fs. "
            "Falling back to the RPC bridge for live reads (cooldown %.0fs).",
            code, period, newest, lag, _FORMULA_STALE_COOLDOWN_SECONDS,
        )
    except Exception:
        pass


def _formula_stale_active():
    """冷却期内跳过公式直连（检测到滞后之后的一段时间）。"""
    return time.time() < _formula_stale_until.get("ts", 0.0)


def _qmt_stime_index(value):
    digits = _digits_only(value)
    if len(digits) >= 14:
        return digits[:14]
    if len(digits) >= 8:
        return digits[:8]
    return str(value or "")


_EPOCH_PLACEHOLDER_FLOOR = "19900101"


def _is_epoch_placeholder_label(value):
    """True for a bar label that predates the A-share market itself (#228).

    Asked for a window it has no data for, big QMT does not answer with zero
    rows -- it answers with one row stamped at the epoch (`stime` `19700101`,
    OHLC and volume all zero). miniQMT returns an empty frame there, and that
    is the contract this bridge promises.

    The row is not harmless: a caller walking rows to derive period boundaries
    reads it as a real bar and asks the calendar for `1969-12-29~1970-01-04`.
    One downstream instance died on exactly that at 09:11 pre-open and idled
    until someone stopped it, having already persisted `bar_time=1970-01-01`
    rows that needed cleaning by hand.

    The floor is the market's own start, not a tuning knob: the Shanghai
    exchange opened in December 1990, so nothing earlier can be a real bar. A
    zero row on a *plausible* date is left alone -- a suspended day is
    legitimately zero-volume, and `fill_data=True` fills gaps on purpose.
    """
    digits = _digits_only(value)
    if len(digits) < 8:
        return False
    return digits[:8] < _EPOCH_PLACEHOLDER_FLOOR


def _iso_week_start_label(value):
    """The Monday of the ISO week a ``1w`` bar label belongs to, as YYYYMMDD.

    Big QMT labels a weekly bar with the **Sunday** of its ISO week. Measured
    against the live terminal, the weekly close equals the last daily close in
    Monday..Sunday for 19/19 weeks, two of them holiday-shortened -- so the
    label is the period end, not the period start (#166).

    Deriving Monday from the label, rather than from where the previous bar
    sat, is what lets the first bar of a window be filled at all. Returns None
    for anything unparseable, so the caller fills nothing instead of guessing.
    """
    parsed = _parse_qmt_stime(value)
    if parsed is None:
        return None
    day = parsed.date()
    return (day - _dt.timedelta(days=day.weekday())).strftime("%Y%m%d")


def _month_start_label(value):
    """The 1st of the month a bar label belongs to, as YYYYMMDD."""
    parsed = _parse_qmt_stime(value)
    if parsed is None:
        return None
    return parsed.strftime("%Y%m") + "01"


def _quarter_start_label(value):
    """The 1st of the quarter's first month a bar label belongs to."""
    parsed = _parse_qmt_stime(value)
    if parsed is None:
        return None
    month = ((parsed.month - 1) // 3) * 3 + 1
    return "%04d%02d01" % (parsed.year, month)


def _halfyear_start_label(value):
    """Jan 1 or Jul 1 of the half-year a bar label belongs to."""
    parsed = _parse_qmt_stime(value)
    if parsed is None:
        return None
    return "%04d0101" % parsed.year if parsed.month <= 6 else "%04d0701" % parsed.year


def _year_start_label(value):
    """Jan 1 of the year a bar label belongs to."""
    parsed = _parse_qmt_stime(value)
    if parsed is None:
        return None
    return "%04d0101" % parsed.year


def _period_end_label(period, value):
    """The last calendar day of the natural period a bar label belongs to.

    Used only to tell an in-progress bar (its period is not over yet) from a
    finalized one. Returns None for anything unparseable.
    """
    parsed = _parse_qmt_stime(value)
    if parsed is None:
        return None
    day = parsed.date()
    if period == "1w":
        return (day - _dt.timedelta(days=day.weekday()) + _dt.timedelta(days=6)).strftime("%Y%m%d")
    if period == "1mon":
        year, month = parsed.year, parsed.month
        end = _dt.date(year + month // 12, month % 12 + 1, 1) - _dt.timedelta(days=1)
        return end.strftime("%Y%m%d")
    if period == "1q":
        month = ((parsed.month - 1) // 3) * 3 + 3
        end = _dt.date(parsed.year + month // 12, month % 12 + 1, 1) - _dt.timedelta(days=1)
        return end.strftime("%Y%m%d")
    if period == "1hy":
        return "%04d0630" % parsed.year if parsed.month <= 6 else "%04d1231" % parsed.year
    if period == "1y":
        return "%04d1231" % parsed.year
    return None


def _bar_float(value):
    """A bar cell as a float, or None for anything that is not a number.

    NaN counts as "not a number" on purpose: an all-NaN preClose column means
    the same thing as an all-zero one -- the terminal did not answer.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return number


def _frame_bar_labels(frame):
    """Bar labels in row order, from the normalized index or a stime column.

    Both sources are checked for being date-shaped rather than trusted: a
    frame that never went through _normalize_market_data_frame still carries a
    positional index, and 0/1/2 would otherwise be read as bar labels.
    """
    for from_index in (True, False):
        try:
            values = list(frame.index) if from_index else list(frame["stime"])
        except Exception:
            continue
        labels = [_qmt_stime_index(value) for value in values]
        if labels and all(len(_digits_only(label)) >= 8 for label in labels):
            return labels
    return []


def _qmt_datetime_to_epoch_ms(dt_value):
    # QMT bar labels are China local time; MiniQMT's time column is epoch ms.
    china_tz = _dt.timezone(_dt.timedelta(hours=8))
    return int(dt_value.replace(tzinfo=china_tz).timestamp() * 1000)


def _normalize_market_data_frame(df, field_list=None):
    try:
        columns = list(df.columns)
    except Exception:
        return df
    if "stime" not in columns:
        return df

    requested = [str(field) for field in (field_list or [])]
    try:
        out = df.copy()
        stimes = list(out["stime"])
        out.index = [_qmt_stime_index(value) for value in stimes]
        if "time" in out.columns or "time" in requested:
            out["time"] = [
                _qmt_datetime_to_epoch_ms(parsed) if parsed is not None else None
                for parsed in (_parse_qmt_stime(value) for value in stimes)
            ]
        # Drop big QMT's "no data for this window" placeholders (#228). Done
        # after the derived columns are built, and by position, so the rows
        # that stay keep the values that belong to them.
        keep = [
            position for position, value in enumerate(stimes)
            if not _is_epoch_placeholder_label(value)
        ]
        if len(keep) != len(stimes):
            out = out.iloc[keep]
        if requested:
            keep = [field for field in requested if field in out.columns]
            if keep:
                return out[keep]
        if "stime" in out.columns:
            return out.drop(columns=["stime"])
        return out
    except Exception:
        return df


def _normalize_market_data_result(data, field_list=None):
    if not isinstance(data, dict):
        return data
    return {
        code: _normalize_market_data_frame(frame, field_list=field_list)
        for code, frame in data.items()
    }


def _normalize_code_for_filter(code):
    text = str(code or "").strip().upper()
    if "." not in text:
        return text
    return text.split(".", 1)[0]


def _is_hs_a_share(code):
    text = str(code or "").strip().upper()
    pure = _normalize_code_for_filter(text)
    if not (len(pure) == 6 and pure.isdigit()):
        return False
    if text.endswith(".SH"):
        return pure.startswith(("600", "601", "603", "605", "688", "689"))
    if text.endswith(".SZ"):
        return pure.startswith(("000", "001", "002", "003", "300", "301"))
    return pure.startswith(
        ("000", "001", "002", "003", "300", "301", "600", "601", "603", "605", "688", "689")
    )


def _full_a_share_code(code):
    """Ensure a callback stock_code carries its exchange suffix.

    Native MiniQMT XtOrder/XtTrade carry the full '600000.SH' form. Events from
    an older server (or when the callback object exposes no exchange info) may
    carry the bare 6-digit code; infer the suffix from the A-share code ranges
    so consumers can key on the full form. Non-6-digit or already-suffixed
    codes pass through unchanged.
    """
    text = str(code or "").strip().upper()
    if "." in text or not (len(text) == 6 and text.isdigit()):
        return text
    if text.startswith(("600", "601", "603", "605", "688", "689")):
        return text + ".SH"
    if text.startswith(("000", "001", "002", "003", "300", "301")):
        return text + ".SZ"
    return text


class BigQmtRpcClient:
    def __init__(
        self,
        account_id=None,
        redis_client=None,
        redis_config=None,
        timeout_seconds=None,
        transport=None,
    ):
        client_config = load_client_config()
        config_redis = dict(client_config.get("redis_config") or {})
        redis_config = dict(redis_config or {})
        merged_redis_config = dict(config_redis)
        merged_redis_config.update(redis_config)
        self.account_id = str(
            account_id
            or merged_redis_config.get("account_id")
            or client_config.get("account_id")
            or os.environ.get("BIGQMT_ACCOUNT_ID")
            or ""
        )
        self.redis_client = redis_client
        self.redis_config = {
            "host": merged_redis_config.get("host") or os.environ.get("BIGQMT_REDIS_HOST", "127.0.0.1"),
            "port": int(merged_redis_config.get("port") or _env_int("BIGQMT_REDIS_PORT", 6379)),
            "db": int(merged_redis_config.get("db") or _env_int("BIGQMT_REDIS_DB", 5)),
            "username": merged_redis_config.get("username", os.environ.get("BIGQMT_REDIS_USERNAME") or ""),
            "password": merged_redis_config.get("password", os.environ.get("BIGQMT_REDIS_PASSWORD") or ""),
            # redis-py 8.x 默认 RESP3，Redis 5.0 只支持 RESP2 -> 透传 protocol
            "protocol": merged_redis_config.get("protocol") or _env_int("BIGQMT_REDIS_PROTOCOL", 2),
        }
        config_timeout = client_config.get("timeout_seconds")
        self.timeout_seconds = float(
            timeout_seconds
            if timeout_seconds is not None
            else config_timeout
            if config_timeout is not None
            else _env_float("BIGQMT_RPC_TIMEOUT_SECONDS", DEFAULT_RPC_TIMEOUT_SECONDS)
        )
        config_download_wait = client_config.get("download_wait_seconds")
        self.download_wait_seconds = float(
            config_download_wait
            if config_download_wait is not None
            else _env_float("BIGQMT_DOWNLOAD_WAIT_SECONDS", 1800.0)
        )
        config_download_poll = client_config.get("download_poll_interval_seconds")
        self.download_poll_interval_seconds = float(
            config_download_poll
            if config_download_poll is not None
            else _env_float("BIGQMT_DOWNLOAD_POLL_INTERVAL_SECONDS", 0.5)
        )
        full_tick_cache_config = dict(client_config.get("full_tick_cache_config") or {})
        self.full_tick_cache_config = {
            "enabled": _bool_value(
                full_tick_cache_config.get("enabled", full_tick_cache_config.get("full_tick_cache_enabled")),
                _env_bool("BIGQMT_FULL_TICK_CACHE_ENABLED", False),
            ),
            "demand_ttl_seconds": float(
                full_tick_cache_config.get("demand_ttl_seconds")
                or full_tick_cache_config.get("full_tick_demand_ttl_seconds")
                or _env_float("BIGQMT_FULL_TICK_DEMAND_TTL_SECONDS", 10.0)
            ),
            "cache_ttl_seconds": float(
                full_tick_cache_config.get("cache_ttl_seconds")
                or full_tick_cache_config.get("full_tick_cache_ttl_seconds")
                or _env_float("BIGQMT_FULL_TICK_CACHE_TTL_SECONDS", 10.0)
            ),
            "wait_seconds": float(
                full_tick_cache_config.get("wait_seconds")
                or full_tick_cache_config.get("full_tick_wait_seconds")
                or _env_float("BIGQMT_FULL_TICK_WAIT_SECONDS", 3.5)
            ),
            "poll_interval_seconds": float(
                full_tick_cache_config.get("poll_interval_seconds")
                or full_tick_cache_config.get("full_tick_poll_interval_seconds")
                or _env_float("BIGQMT_FULL_TICK_POLL_INTERVAL_SECONDS", 0.2)
            ),
        }
        # Client-side local market-data cache. get_market_data_ex is cache-through;
        # get_local_data falls back to Big QMT by default so a MiniQMT-style
        # download of raw history can be followed by a read in another
        # adjustment mode. Set fallback_rpc=False only for an explicitly
        # offline, cache-only client.
        local_cache_config = dict(client_config.get("local_cache_config") or {})
        self.local_cache_config = {
            "enabled": _bool_value(
                local_cache_config.get("enabled", merged_redis_config.get("local_cache_enabled")),
                _env_bool("BIGQMT_LOCAL_CACHE_ENABLED", True),
            ),
            "dir": (
                local_cache_config.get("dir")
                or merged_redis_config.get("local_cache_dir")
                or os.environ.get("BIGQMT_LOCAL_CACHE_DIR")
                or None
            ),
            "fallback_rpc": _bool_value(
                local_cache_config.get("fallback_rpc", merged_redis_config.get("local_cache_fallback_rpc")),
                _env_bool("BIGQMT_LOCAL_CACHE_FALLBACK_RPC", True),
            ),
            "format": str(
                local_cache_config.get("format")
                or merged_redis_config.get("local_cache_format")
                or os.environ.get("BIGQMT_LOCAL_CACHE_FORMAT")
                or "auto"  # parquet if pyarrow is available, else pickle
            ),
        }
        # Transport selection. Default "redis" keeps the legacy call_redis_rpc
        # path (so existing client configs are unchanged). Setting transport to
        # "zmq"/"mysql"/"shm" (via config or constructor) routes calls through
        # the swappable transport layer instead.
        self.transport_name = str(
            transport
            or merged_redis_config.get("transport")
            or os.environ.get("BIGQMT_RPC_TRANSPORT")
            or "redis"
        ).lower()
        self.zmq_config = dict(merged_redis_config.get("zmq") or {})
        self.mysql_config = dict(merged_redis_config.get("mysql") or {})
        self._transport_instance = None  # lazily built by _transport()
        # FormulaServer read fast-path. QMT's C++ quote service (port 58600)
        # answers reference/history reads in ~0.07ms without touching the QMT
        # python thread. Enabled by default; every miss falls back to RPC, so a
        # client that cannot reach it just runs as before.
        formula_config = dict(
            client_config.get("formula_server_config")
            or merged_redis_config.get("formula_server")
            or {}
        )
        if "enabled" not in formula_config:
            formula_config["enabled"] = _env_bool("BIGQMT_FORMULA_ENABLED", True)
        self.formula_server_config = formula_config
        self._formula_router_instance = None  # lazily built by _formula_router()

    def _redis(self):
        if self.redis_client is None:
            import redis

            from .adapters.redis_common import redis_supports_protocol_kw

            cfg = dict(self.redis_config)
            if not cfg.get("username"):
                cfg.pop("username", None)
            if not cfg.get("password"):
                cfg.pop("password", None)
            if not redis_supports_protocol_kw():
                # QMT 自带 redis-py 3.5.3 不认 protocol（issue #71）
                cfg.pop("protocol", None)
            self.redis_client = redis.Redis(**cfg)
        return self.redis_client

    def _transport(self):
        if self._transport_instance is None:
            if self.transport_name in ("redis", "", "default"):
                # Legacy path: call_redis_rpc builds its own request envelope.
                return None
            from .transports.factory import build_transport

            client_config = load_client_config()
            config_redis = dict(client_config.get("redis_config") or {})
            zmq_config = dict(config_redis.get("zmq") or {})
            zmq_config.update(self.zmq_config)
            # ZMQ must work without Redis. Discovery is opt-in and unnecessary
            # when connect_address is explicitly configured.
            if (
                not zmq_config.get("connect_address")
                and bool(zmq_config.get("redis_discovery_enabled", False))
            ):
                zmq_config.setdefault("discovery_redis_client", self._redis())
            factory_config = {
                "zmq": zmq_config,
                "mysql": dict(config_redis.get("mysql") or {}, **self.mysql_config),
                # 管道名两侧必须一致，否则客户端连的是另一条线 —— 表现为
                # 「连不上」而不是「配错了」，最难查的那种。
                "pipe": dict(config_redis.get("pipe") or {}),
            }
            self._transport_instance = build_transport(
                self.transport_name,
                factory_config,
                account_id=self.account_id,
                print_prefix="[bigqmt_client]",
            )
        return self._transport_instance

    def _formula_router(self):
        """Lazily build the FormulaServer router. Never raises — a router that
        cannot be built simply means every read goes over RPC."""
        if self._formula_router_instance is None:
            try:
                from .formula_server import build_router

                self._formula_router_instance = build_router(
                    self.formula_server_config, print_prefix="[bigqmt_formula]"
                )
            except Exception as exc:
                print("[bigqmt_formula] disabled (%s: %s)" % (exc.__class__.__name__, exc))

                class _Disabled(object):
                    def supports(self, method):
                        return False

                self._formula_router_instance = _Disabled()
        return self._formula_router_instance

    def call(self, method, params=None, account_id=None, timeout_seconds=None, use_formula=True):
        target_account = str(account_id or self.account_id or "")
        if not target_account:
            raise ValueError(_missing_account_id_message())
        wait_seconds = self.timeout_seconds if timeout_seconds is None else timeout_seconds
        # Fast path: reference/history reads answered straight by QMT's
        # FormulaServer, bypassing the strategy process and its GIL. Anything it
        # declines (unmapped method, untranslatable params, server down) raises
        # Unroutable and drops through to the RPC bridge below.
        # use_formula=False 用于必须拿到最新数据的调用（如 subscribe_quote 的
        # 盘中形成 bar 轮询）——FormulaServer 的快照可能滞后数小时（实测盘中
        # 11:30 后冻结），形成 bar 只能走 RPC 桥读 QMT 实时数据。
        router = self._formula_router() if use_formula else None
        # 冷却期（公式数据刚被检出滞后）只对 get_market_data_ex 跳过直连——
        # 其他方法是静态参考数据，不受时间序列滞后影响，照常走快速路径。
        skip_formula = (
            router is not None
            and method == "get_market_data_ex"
            and _formula_stale_active()
        )
        if router is not None and router.supports(method) and not skip_formula:
            from .formula_server import Unroutable

            try:
                result = _restore_jsonable(router.call(method, params or {}))
                if method == "get_market_data_ex":
                    # 直连快照可能滞后（实测冻结数小时）——滞后即告警、
                    # 本次调用自动回落 RPC 桥拿实时数据，并进入冷却期
                    # 让后续调用直接跳过直连（到期重新探测，自愈）。
                    hit = _formula_bars_stale(result, params or {})
                    if hit is not None:
                        _warn_stale_formula_bars(result, params or {}, hit=hit)
                        _formula_stale_until["ts"] = time.time() + _FORMULA_STALE_COOLDOWN_SECONDS
                        return self.call(method, params, account_id=account_id,
                                         timeout_seconds=timeout_seconds, use_formula=False)
                return result
            except Unroutable:
                pass
        transport = self._transport()
        if transport is not None:
            # Swappable transport path (zmq/mysql/...). Build the request
            # envelope the same way call_redis_rpc does.
            request = {
                "schema_version": 1,
                "request_id": uuid.uuid4().hex,
                "account_id": target_account,
                "method": method,
                "params": params or {},
                "ttl_seconds": 60,
            }
            response = transport.send_request(request, wait_seconds)
        else:
            response = call_redis_rpc(
                self._redis(),
                account_id=target_account,
                method=method,
                params=params or {},
                timeout_seconds=wait_seconds,
            )
        if not response.get("ok"):
            raise RpcServerRepliedError(
                response.get("error") or "Big QMT RPC failed: %s" % method)
        # server_error 携带 QMT 端诊断（如 passorder 提交但委托没进系统）。
        # 只在交易类方法上设置（读取类恒为空），转成异常让调用方看到真实原因，
        # 而不是把「无委托号」误判为 -1 失败（issue #38）。
        server_error = str(response.get("server_error") or "")
        if server_error:
            raise RpcServerRepliedError(
                "Big QMT %s server_error: %s" % (method, server_error))
        # The transport already scanned the raw text for a typed envelope; when
        # it found none there is provably nothing to rebuild, and skipping the
        # walk turns 345.9ms into 3.7ms on a 51285-instrument snapshot. A None
        # flag means the text was never seen (in-process routing), so walk.
        if response.pop(TYPED_PAYLOAD_FLAG, None) is False:
            return response.get("data")
        return _restore_jsonable(response.get("data"))

    # ------------------------------------------------------------------
    # Async RPC (issue #63): call_async returns a Future immediately, so a
    # caller can have many independent requests in flight instead of one
    # blocking call at a time. The server still processes order RPCs on the
    # QMT main thread serially — client-side async overlaps the round-trip
    # latency, it does not parallelize the exchange leg.
    _ASYNC_RPC_MAX_IN_FLIGHT = 64

    def _async_rpc_pool(self):
        pool = getattr(self, "_rpc_async_pool", None)
        if pool is None:
            from concurrent.futures import ThreadPoolExecutor

            pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="bigqmt-rpc-async")
            self._rpc_async_pool = pool
            self._rpc_async_slots = threading.Semaphore(self._ASYNC_RPC_MAX_IN_FLIGHT)
            self._rpc_async_dispatcher = None
        return pool

    def call_async(self, method, params=None, account_id=None, timeout_seconds=None, callback=None):
        """Submit an RPC without blocking; returns concurrent.futures.Future.

        ``callback`` (optional) receives the result on a single dispatcher
        thread — callbacks fire serialized in completion order, never
        concurrently. In-flight requests are bounded; when the limit is hit
        the call raises instead of queueing unboundedly.
        """
        pool = self._async_rpc_pool()
        if not self._rpc_async_slots.acquire(timeout=30.0):
            raise RuntimeError(
                "too many RPCs in flight (max %d)" % self._ASYNC_RPC_MAX_IN_FLIGHT
            )

        def _run():
            try:
                return self.call(method, params, account_id=account_id,
                                 timeout_seconds=timeout_seconds)
            finally:
                self._rpc_async_slots.release()

        future = pool.submit(_run)
        if callback is not None:
            if self._rpc_async_dispatcher is None:
                from concurrent.futures import ThreadPoolExecutor

                self._rpc_async_dispatcher = ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix="bigqmt-rpc-dispatch"
                )
            dispatcher = self._rpc_async_dispatcher

            def _deliver(fut):
                try:
                    result = fut.result()
                except Exception as exc:
                    log.warning("call_async %s failed: %s", method, exc)
                    return
                try:
                    callback(result)
                except Exception:
                    log.exception("call_async callback failed: %s", method)

            future.add_done_callback(lambda fut: dispatcher.submit(_deliver, fut))
        return future

    def publish_event(self, event_type, payload, stream_template="bigqmt:quote_events:{account_id}"):
        account_id = str(self.account_id or "")
        event = {
            "event_type": str(event_type),
            "account_id": account_id,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "payload": payload or {},
        }
        # 显式地址且未启用 Redis discovery 的 ZMQ 是纯 ZMQ 模式，不应隐式连接 Redis。
        if self.transport_name == "zmq" and not bool(self.zmq_config.get("redis_discovery_enabled", False)):
            return event
        raw = json.dumps(event, ensure_ascii=False, default=str)
        stream_key = stream_template.format(account_id=account_id)
        redis_client = self._redis()
        try:
            redis_client.xadd(stream_key, {"payload": raw}, maxlen=1000, approximate=True)
            # 客户端侧写的流同样要能自己消失（#213）。
            from .adapters.redis_common import touch_stream_ttl

            touch_stream_ttl(redis_client, stream_key)
        except Exception:
            pass
        try:
            redis_client.publish(stream_key, raw)
        except Exception:
            pass
        return event

    def save_quote_subscription(self, seq, payload, active=True):
        # MySQL、SHM 和混合 ZMQ 继续保留既有 Redis subscription metadata 行为。
        if self.transport_name == "zmq" and not bool(self.zmq_config.get("redis_discovery_enabled", False)):
            return
        account_id = str(self.account_id or "")
        key = "bigqmt:quote_subscriptions:%s" % account_id
        redis_client = self._redis()
        if active:
            value = json.dumps(payload or {}, ensure_ascii=False, default=str)
            try:
                redis_client.hset(key, str(seq), value)
            except Exception:
                pass
        else:
            try:
                redis_client.hdel(key, str(seq))
            except Exception:
                pass


# IPO subscription codes are their own numbering, distinct from the listed
# share's code. Classification is FAIL-CLOSED: an unrecognised code returns None
# and the caller skips it. The version this replaced ended in `return True`
# ("默认放行"), i.e. it guessed in favour of placing an order -- on a path whose
# whole job was to keep BJ subscriptions, which freeze cash, out.
_IPO_SH_PREFIXES = ("730", "732", "780", "787", "789", "707")
_IPO_SZ_PREFIXES = ("00", "30")
_IPO_BJ_PREFIXES = ("920", "889", "8", "4")


def ipo_market_of(code):
    """Return "SH" / "SZ" / "BJ", or None when the code is not recognised."""
    text = str(code or "").strip().upper()
    if not text:
        return None
    for suffix, market in ((".SH", "SH"), (".SZ", "SZ"), (".BJ", "BJ")):
        if text.endswith(suffix):
            return market
    if not text.isdigit():
        return None
    # Order matters: BJ 920/889 would otherwise be caught by a looser rule.
    if text.startswith(_IPO_BJ_PREFIXES):
        return "BJ"
    if text.startswith(_IPO_SH_PREFIXES):
        return "SH"
    if text.startswith(_IPO_SZ_PREFIXES):
        return "SZ"
    return None


MARKET_TOKENS = frozenset({"SH", "SZ", "BJ", "HK"})
# Above this many explicit codes, one RPC's single timeout starts to matter more
# than the extra payload of reading the exchange and filtering (issue #104).
# Measured against a live bridge: query_orders 1.5s, get_asset 1.4s,
# get_financial_data 0.8s warm, a whole-market get_full_tick 7.7s. The old
# 6s default sat under the cost of ordinary QMT data calls, and timing out
# here is worse than waiting: the bridge keeps working on the abandoned
# request, so the next call queues behind it and one timeout breeds more.
# 30s is also what the whole-market snapshot path already used, so there is
# one number rather than two.
DEFAULT_RPC_TIMEOUT_SECONDS = 30.0

# Batch timeout scaling: the server runs batch items serially on the adjust
# thread. Per-item cost is milliseconds in market hours but ~300ms with the
# counter disconnected -- a 100-item batch then outlives even the 30s default
# (live 2026-09-05: the client gave up, fell back to singles, and the still
# running batch completed too, doubling 100 orders into 200). Scale the wait
# with N so the client outlives the server.
BATCH_TIMEOUT_FLOOR_SECONDS = 15.0
BATCH_TIMEOUT_PER_ITEM_SECONDS = 0.5


class RpcServerRepliedError(RuntimeError):
    """The server answered with an error -- as opposed to a timeout/transport
    failure, where the request's fate is unknown.

    The distinction matters for writes: a batch submit handler raises only
    before its per-item loop, so a replied error means no item ran and a retry
    is safe. A timeout means the batch may still be running and retrying
    doubles orders."""




LARGE_CODE_LIST = 1000
# What the fallback reads first. Stocks are 8.7% of an exchange listing, so
# starting narrow is 1.08s against 7.4s; it widens to "all" only if that misses.
DEFAULT_FALLBACK_TYPES = ("stock",)

# How many int -> 合同编号 pairs a trader keeps so a cancel still resolves after
# the caller round-tripped the id through JSON and lost the string (issue #113).
_ORDER_ID_MEMORY = 4096


def _markets_of(codes):
    """Market tokens the given suffixed codes live on, or empty if any code
    carries no recognised suffix -- filtering an exchange read cannot recover a
    code we cannot place."""
    markets = set()
    for code in codes or []:
        _, _, suffix = str(code).rpartition(".")
        suffix = suffix.upper()
        if suffix not in MARKET_TOKENS:
            return set()
        markets.add(suffix)
    return markets


def _full_tick_params(codes, types=None):
    """RPC params for get_full_tick. `types` narrows a whole-market token to one
    instrument kind at REQUEST time -- filtering the reply would still pay QMT's
    per-instrument cost for everything the exchange lists (issue #104)."""
    params = {"codes": codes}
    if types:
        params["types"] = [types] if isinstance(types, str) else list(types)
    return params


_FIELD_LIST_NOTICE = {"shown": False}
DIRECT_PATH_FIELDS = ("open", "high", "low", "close", "volume", "amount")

# Periods whose preClose big QMT answers as 0.0, and which we therefore fill
# from daily bars (#166). Measured read-only on 国金 2.1.19.0 / 0.3.19:
# 1d/1mon/1q/1hy/1y all carry a real preClose; only 1w is zero, for every code
# tried and under both fill_data settings. Adding a period here needs the same
# scan first -- the label semantics differ per period and are verified, not
# assumed.
PRE_CLOSE_BACKFILL_PERIODS = ("1w",)
_PRE_CLOSE_PERIOD_STARTS = {"1w": _iso_week_start_label}
_PRE_CLOSE_NOTICE = {"shown": False}

# Periods whose ongoing bar QMT synthesizes from the request window instead
# of from all local data (#226). Pinned as a set the way #172 pinned its
# period list -- a new period enters only with a measurement behind it.
RESYNTH_ONGOING_PERIODS = ("1w", "1mon", "1q", "1hy", "1y")
RESYNTH_PERIOD_STARTS = {
    "1w": _iso_week_start_label,
    "1mon": _month_start_label,
    "1q": _quarter_start_label,
    "1hy": _halfyear_start_label,
    "1y": _year_start_label,
}
_RESYNTH_NOTICE = {"shown": False}


def _in_trading_session(now):
    """Weekday inside the continuous-auction window (with a settle margin
    after close). After the close the ongoing multi-day bar is finalized and
    passes through untouched (#226's after-close control)."""
    if now.weekday() > 4:
        return False
    return _dt.time(9, 30) <= now.time() <= _dt.time(15, 5)


def _now():
    return _dt.datetime.now()


def _notice_field_list_cost(field_list):
    """Say once that naming fields is what enables the fast path.

    Not a warning about a mistake: an empty field_list correctly returns all 11
    columns and only RPC can do that. But the speedup is invisible unless
    someone tells you it exists (issue #104). Asking for a field the direct
    path lacks is safe -- it falls back to RPC rather than returning NaN."""
    if field_list or _FIELD_LIST_NOTICE["shown"]:
        return
    _FIELD_LIST_NOTICE["shown"] = True
    try:
        log.info(
            "get_market_data_ex with an empty field_list returns all 11 columns "
            "and must go over RPC. If the six OHLCV columns %s are enough, pass "
            "them as field_list -- that path is served by FormulaServer, 0.015s "
            "against 5.8s measured. Naming preClose / suspendFlag / "
            "settelementPrice / openInterest falls back to RPC, so a wider "
            "field_list is safe, just not faster.", ", ".join(DIRECT_PATH_FIELDS))
    except Exception:
        pass


# Bar subscriptions poll, because there is no server-side push for K-lines: the
# bridge only exposes ContextInfo.subscribe_whole_quote, which carries ticks.
# Interval is a floor on how stale a bar can be, not a promise of freshness.
DEFAULT_BAR_POLL_INTERVAL_SECONDS = 3.0


class _BarPoller(object):
    """Emit a K-line callback when the newest bar changes.

    MiniQMT's subscribe_quote pushes each bar update. We approximate it by
    re-reading the last bars and firing only when the newest one differs, so a
    caller written against MiniQMT keeps working. Every callback is wrapped:
    a raising subscriber must not kill the polling thread and silently end the
    subscription.
    """

    def __init__(self, fetch, callback, interval_seconds, on_error=None,
                 on_no_data=None):
        self._fetch = fetch
        self._callback = callback
        self._interval = max(0.2, float(interval_seconds))
        self._on_error = on_error
        self._on_no_data = on_no_data
        self._stop = threading.Event()
        self._last_signature = None
        self._reported_no_data = False
        self._thread = threading.Thread(target=self._loop)
        self._thread.daemon = True

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()

    @staticmethod
    def _signature(data):
        """Identify the newest bar cheaply, without assuming a container type."""
        try:
            for value in (data or {}).values():
                if value is None:
                    continue
                if hasattr(value, "empty"):          # pandas DataFrame
                    if getattr(value, "empty", True):
                        continue
                    return str(value.index[-1]), int(len(value))
                if isinstance(value, (list, tuple)) and value:
                    return repr(value[-1]), len(value)
                if isinstance(value, dict) and value:
                    key = sorted(value.keys())[-1]
                    return repr(key), len(value)
        except Exception:
            return None
        return None

    def _loop(self):
        while not self._stop.is_set():
            try:
                data = self._fetch()
                signature = self._signature(data)
                if signature is None:
                    # No bars for this period. The subscription is live and will
                    # start firing if data appears, but until then the caller
                    # gets nothing -- which is indistinguishable from a broken
                    # subscription unless we say so. Reported once, not per poll.
                    if not self._reported_no_data:
                        self._reported_no_data = True
                        if self._on_no_data is not None:
                            self._on_no_data()
                elif signature != self._last_signature:
                    self._reported_no_data = False
                    self._last_signature = signature
                    if self._callback is not None:
                        self._callback(data)
            except Exception as exc:
                if self._on_error is not None:
                    try:
                        self._on_error(exc)
                    except Exception:
                        pass
            self._stop.wait(self._interval)


_VERSION_WARNED = {"shown": False}


def warn_on_version_mismatch(ping_response):
    """Warn once when the QMT-side bridge is not the build this client is.

    Deploying into QMT is a file copy and QMT keeps modules across strategy
    re-runs, so a stale server behaves like a fix that "did not work". Say so on
    connect instead of leaving it to be discovered by debugging.

    Silent when the versions agree, when the server is too old to report one, or
    when it has already been said. Never raises: this is on the connect path.
    """
    if _VERSION_WARNED["shown"]:
        return None
    try:
        server = str((ping_response or {}).get("version") or "")
        if not server:
            return None      # server predates version reporting; nothing to compare
        from .version import __version__ as local

        if server == local:
            return None
        _VERSION_WARNED["shown"] = True
        log.warning(
            "version mismatch: this client is %s, the QMT-side bridge is %s. "
            "A copy alone does not take effect -- QMT keeps modules across "
            "strategy re-runs, so the strategy must be restarted too. Set "
            "BIGQMT_AUTO_SYNC=1 (or call xt_trader.sync_deployment()) to push "
            "this client's package into the QMT python directory.",
            local, server)
        return (local, server)
    except Exception:
        return None


def auto_sync_enabled():
    """Writing into a live trading terminal is opt-in, not a side effect of
    connecting."""
    return _bool_value(os.environ.get("BIGQMT_AUTO_SYNC"), False)


class BigQmtXtData:
    def __init__(self, client):
        self.client = client
        self._subscribe_seq = int(time.time() * 1000)
        self._cache_obj = None
        self._quote_session = None          # lazily built WholeQuoteClientSession
        self._quote_session_factory = None  # test hook: returns a session-like object
        self._bar_pollers = {}              # seq -> _BarPoller, for K-line periods
        self._bar_poller_lock = threading.Lock()

    def _next_seq(self):
        self._subscribe_seq += 1
        return self._subscribe_seq

    def _local_cache(self):
        cfg = dict(getattr(self.client, "local_cache_config", {}) or {})
        if not _bool_value(cfg.get("enabled"), True):
            return None
        if self._cache_obj is None:
            self._cache_obj = LocalMarketCache(cache_dir=cfg.get("dir"), fmt=cfg.get("format", "auto"))
        return self._cache_obj

    def _call(self, method, **params):
        return self.client.call(method, params)

    def get_full_tick(self, code_list, timeout_seconds=None, types=None):
        """Fetch full tick data for a list of codes.

        Args:
            code_list: stock codes to query.
            timeout_seconds: per-request RPC timeout. None = auto (30s for whole-market
                snapshots, else the client default, DEFAULT_RPC_TIMEOUT_SECONDS).
                Callers can pass a larger value when querying many codes (e.g. 1256
                ETF options may need 150-180s).
        """
        codes = list(code_list or [])
        if not codes:
            return {}
        cache_config = dict(getattr(self.client, "full_tick_cache_config", {}) or {})
        if _bool_value(cache_config.get("enabled"), False):
            redis_client = self.client._redis()
            request_full_tick_cache(
                redis_client,
                self.client.account_id,
                codes,
                demand_ttl_seconds=cache_config.get("demand_ttl_seconds", 10),
                cache_ttl_seconds=cache_config.get("cache_ttl_seconds", 10),
            )
            data = wait_full_tick_cache(
                redis_client,
                self.client.account_id,
                codes,
                max_age_seconds=cache_config.get("cache_ttl_seconds", 10),
                wait_seconds=cache_config.get("wait_seconds", 3.5),
                poll_interval_seconds=cache_config.get("poll_interval_seconds", 0.2),
            )
            if data is not None:
                return data
            upper_codes = {str(code).strip().upper() for code in codes}
            if upper_codes & {"SH", "SZ", "BJ", "HK"}:
                # Whole-market snapshots must stay on the demand cache. A live RPC
                # here would ship ~50k rows on every miss, so surface the timeout.
                raise TimeoutError("full tick redis cache timeout: %s" % ",".join(str(code) for code in codes))
            # Symbol-list miss (cold start / expired snapshot): fall back to a live
            # RPC so the first call is ~ms instead of a hard wait_seconds stall.
            rpc_timeout = timeout_seconds if timeout_seconds is not None else None
            return self.client.call("get_full_tick", _full_tick_params(codes, types), timeout_seconds=rpc_timeout) or {}
        upper_codes = {str(code).strip().upper() for code in codes}
        # Caller-provided timeout takes priority; otherwise auto-detect whole-market.
        if timeout_seconds is not None:
            rpc_timeout = timeout_seconds
        else:
            rpc_timeout = 30 if upper_codes & {"SH", "SZ", "BJ", "HK"} else None
        failure = None
        try:
            data = self.client.call(
                "get_full_tick", _full_tick_params(codes, types),
                timeout_seconds=rpc_timeout) or {}
        except Exception as exc:
            data = None
            failure = exc
            if not self._can_fall_back_to_markets(codes, upper_codes):
                raise
        if self._should_fall_back(codes, upper_codes, data):
            fallback_errors = []
            recovered = self._full_tick_via_markets(
                codes, rpc_timeout, types, errors=fallback_errors)
            if recovered is not None:
                return recovered
            if failure is not None:
                # A bare `raise` here has no active exception -- the except
                # block above has already exited -- so it produced
                # "RuntimeError: No active exception to reraise" and buried
                # the real timeout (reported on issue #104). Re-raise the
                # actual failure, and say why the recovery did not help.
                if fallback_errors:
                    log.warning(
                        "get_full_tick: %d codes failed directly (%s) and the "
                        "market re-read failed too (%s: %s); raising the "
                        "original failure.",
                        len(codes), failure,
                        fallback_errors[-1].__class__.__name__,
                        fallback_errors[-1])
                else:
                    log.warning(
                        "get_full_tick: %d codes failed directly (%s) and "
                        "could not be recovered from a market read.",
                        len(codes), failure)
                raise failure
        return data or {}

    def _can_fall_back_to_markets(self, codes, upper_codes):
        """Only an explicit list of suffixed codes can be recovered this way."""
        if upper_codes & MARKET_TOKENS:
            return False          # already a whole-market request
        if len(codes) <= LARGE_CODE_LIST:
            return False          # small list: a failure here is a real failure
        return bool(_markets_of(codes))

    def _should_fall_back(self, codes, upper_codes, data):
        if data is None:
            return True           # the request raised
        if not self._can_fall_back_to_markets(codes, upper_codes):
            return False
        # Short answer: the server dropped codes, or truncated. Anything missing
        # is worth one whole-market read rather than silently returning less
        # than was asked for (issue #104).
        return len(data) < len(set(str(c) for c in codes))

    def _full_tick_via_markets(self, codes, rpc_timeout, types=None, errors=None):
        """Read the exchange(s) these codes live on, then filter to them.

        A long explicit list is one RPC carrying one timeout, so it either fits
        or loses everything; a market token is a single cheap argument that
        cannot truncate.

        Narrowed first, "all" only if that came up short. An exchange listing is
        mostly bonds -- "SH" is 26744 instruments of which 2315 are stocks -- so
        reading all of it costs 7.4s against 1.08s for the stocks. No reason to
        pay that when the codes being recovered are stocks (issue #104).
        """
        markets = _markets_of(codes)
        if not markets:
            return None
        wanted = set(str(code) for code in codes)

        attempts = [list(types) if types else list(DEFAULT_FALLBACK_TYPES)]
        if not any(str(k).lower() == "all" for k in attempts[0]):
            attempts.append(["all"])

        merged = {}
        for attempt in attempts:
            merged = {}
            try:
                for market in sorted(markets):
                    snapshot = self.client.call(
                        "get_full_tick", _full_tick_params([market], attempt),
                        timeout_seconds=max(rpc_timeout or 0, 60)) or {}
                    for key, value in snapshot.items():
                        if str(key) in wanted:
                            merged[key] = value
            except Exception as exc:
                # Swallowing the reason here left the caller with nothing to
                # report; hand it back so the raise can name it (issue #104).
                if errors is not None:
                    errors.append(exc)
                return None
            if len(merged) >= len(wanted):
                break          # everything asked for; no need to widen

        log.warning(
            "get_full_tick: %d codes did not come back directly; re-read %s as "
            "%s and filtered to %d. A market token with types= is cheaper than "
            "a list this long.",
            len(wanted), "/".join(sorted(markets)), "/".join(attempts[-1]
                                                             if len(merged) < len(wanted)
                                                             else attempts[0]),
            len(merged))
        return merged

    def get_deployment_info(self):
        """Where the QMT-side bridge is running from, and which build it is.

        Returns version / package_dir / qmt_python_dir / strategy_dir /
        python_version. Use it to check a deploy landed before hunting for a
        fix that was never actually there -- QMT keeps modules across strategy
        re-runs, so a forgotten copy and an un-reloaded one look the same.
        """
        return self.client.call("get_deployment_info", {}) or {}

    def get_instrument_detail(self, stock_code):
        return self.client.call("get_instrument_detail", {"code": stock_code}) or {}

    def get_instrumentdetail(self, stock_code):
        return self.get_instrument_detail(stock_code)

    def get_instrument_type(self, stock_code, variety_list=None):
        return self._call("get_instrument_type", code=stock_code, variety_list=variety_list)

    def get_stock_type(self, stock_code, variety_list=None):
        """xtdata.get_stock_type 的同名封装 —— 大 QMT 上答不了，直接报错。

        服务端走的是 ContextInfo.get_stock_type(stock)。这个 stub 在大 QMT 上
        存在（缺失会抛 NotImplementedError），但**对任何代码都返回 0**：实测
        股票 600000.SH、ETF 589820.SH、沪市债券 186511.SH、期权
        10011096.SHO 全部是 0，换代码格式（600000 / SH600000 /
        600000.SSE）也一样。

        返回一个恒为 0 的"类型"比报 AttributeError 更糟：报错看得见，一个
        错的分类看不见。所以这里显式拒绝，并指向真正能用的那个：
        get_instrument_type()，实测能区分 stock / fund / etf / bond / index。
        """
        raise NotImplementedError(
            "get_stock_type is not usable on Big QMT: the server-side "
            "ContextInfo.get_stock_type stub returns 0 for every code "
            "(verified live against a stock, an ETF, a bond and an option, and "
            "against every code format). Use get_instrument_type(stock_code) "
            "instead -- it returns "
            "{'stock': ..., 'fund': ..., 'etf': ..., 'bond': ..., 'index': ...}."
        )

    def subscribe_l2thousand(self, stock_code, gear_num=None, callback=None):
        """千档盘口订阅。

        callback 在 RPC 模型下没有回调通道，服务端会忽略它 —— 想要推送请用
        subscribe_whole_quote。这里保留形参只为和 xtdata 签名一致。
        """
        return self._call(
            "subscribe_l2thousand",
            stock_code=stock_code,
            gear_num=0 if gear_num is None else gear_num,
        )

    def get_stock_list_in_sector(self, sector_name, real_timetag=-1):
        name = str(sector_name or "")
        try:
            return self._call("get_stock_list_in_sector", sector_name=sector_name, real_timetag=real_timetag) or []
        except Exception:
            pass
        if name in ("沪深A股", "沪深A股".encode("utf-8", errors="ignore").decode("utf-8", errors="ignore")):
            ticks = self.get_full_tick(["SH", "SZ"])
            return sorted(code for code in ticks.keys() if _is_hs_a_share(code))
        raise NotImplementedError("sector is not supported by BigQMT compat: %s" % sector_name)

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
        params = dict(
            field_list=list(field_list or []),
            stock_list=list(stock_list or []),
            period=period,
            start_time=start_time,
            end_time=end_time,
            count=count,
            dividend_type=dividend_type,
            fill_data=fill_data,
        )
        data = self._call("get_market_data", **params)
        # Self-heal adjusted reads (all-zero bars -> server raw download + retry).
        return self._heal_adjusted("get_market_data", params, data)

    def _get_market_data_ex_batch(self, params, timeout_seconds=None, use_formula=True):
        """One RPC's worth of bars, healed and normalized. No caching."""
        if use_formula:
            data = self.client.call("get_market_data_ex", params, timeout_seconds=timeout_seconds)
        else:
            # 只在绕路时显式传参——保持既有调用方/桩签名不变。
            data = self.client.call("get_market_data_ex", params, timeout_seconds=timeout_seconds,
                                    use_formula=False)
        # Self-heal adjusted reads (all-zero bars -> server raw download + retry).
        data = self._heal_adjusted("get_market_data_ex", params, data, timeout_seconds=timeout_seconds)
        # Normalize Big QMT's stime-indexed frame to MiniQMT shape (time-indexed).
        if isinstance(data, dict):
            data = _normalize_market_data_result(data, field_list=params.get("field_list"))
        return data

    @staticmethod
    def _pre_close_missing(frame):
        """True when a frame has a preClose column and no usable value in it.

        "Column present, says nothing" is the #166 symptom. A frame without
        the column was never asked for it, and one with a single real value
        came from a terminal that answers -- neither wants filling.
        """
        try:
            column = list(frame["preClose"])
        except Exception:
            return False
        for value in column:
            number = _bar_float(value)
            if number is not None and number != 0.0:
                return False
        return True

    def _backfill_pre_close(self, data, period, dividend_type,
                            timeout_seconds=None, use_formula=True):
        """Fill an all-zero preClose from daily bars (#166).

        Big QMT answers preClose = 0.0 on every ``1w`` bar. The right value,
        and the one MiniQMT gives, is the daily preClose of the period's FIRST
        trading day -- which on an ex-dividend day is the adjusted reference
        price, so it is NOT interchangeable with the previous bar's close even
        though the two agree in most weeks.

        Costs one extra daily request, and only when a frame actually came
        back empty-handed: a terminal that answers preClose pays nothing, and
        neither does a caller who never asked for the column (#104).
        """
        period_start = _PRE_CLOSE_PERIOD_STARTS.get(str(period or ""))
        if period_start is None or not isinstance(data, dict):
            return data

        targets = {}
        for code, frame in data.items():
            if not self._pre_close_missing(frame):
                continue
            spans = [(label, period_start(label)) for label in _frame_bar_labels(frame)]
            spans = [(label, start) for label, start in spans if start]
            if spans:
                targets[code] = spans
        if not targets:
            return data

        starts = [start for spans in targets.values() for _, start in spans]
        ends = [label for spans in targets.values() for label, _ in spans]
        try:
            daily = self.get_market_data_ex(
                field_list=["preClose", "stime"], stock_list=sorted(targets),
                period="1d", start_time=min(starts), end_time=max(ends),
                count=-1, dividend_type=dividend_type,
                # A filled row carries no real preClose and would be picked as
                # a period's "first trading day" if it were returned (#167).
                fill_data=False, timeout_seconds=timeout_seconds,
                use_formula=use_formula, backfill_pre_close=False,
            )
        except Exception as exc:
            # Degrade to the terminal's answer. The bars themselves are good;
            # taking the whole read down over the fill would be the worse bug.
            log.warning("preClose backfill for period %s skipped: %s", period, exc)
            return data

        for code, spans in targets.items():
            rows = self._daily_pre_close_rows(daily.get(code) if isinstance(daily, dict) else None)
            if not rows:
                continue
            frame = data[code]
            try:
                existing = list(frame["preClose"])
            except Exception:
                continue
            filled = list(existing)
            hits = 0
            for position, (label, start) in enumerate(spans):
                if position >= len(filled):
                    break
                for day, pre_close in rows:      # ascending -> first match wins
                    if start <= day <= label:
                        filled[position] = pre_close
                        hits += 1
                        break
            if not hits:
                continue
            try:
                frame["preClose"] = filled
            except Exception:
                continue
            self._notice_pre_close_backfill(period)
        return data

    @staticmethod
    def _daily_pre_close_rows(frame):
        """[(YYYYMMDD, preClose)] ascending, skipping cells with no number."""
        labels = _frame_bar_labels(frame) if frame is not None else []
        if not labels:
            return []
        try:
            values = list(frame["preClose"])
        except Exception:
            return []
        rows = []
        for label, value in zip(labels, values):
            number = _bar_float(value)
            if number is not None:
                rows.append((label, number))
        rows.sort()
        return rows

    def _resynth_ongoing_multiday_bars(self, data, period, start_time,
                                       end_time, dividend_type,
                                       timeout_seconds=None, use_formula=True):
        """Rebuild the in-progress multi-day bar from the period's daily bars (#226).

        Big QMT synthesizes an ongoing 1w/1mon/1q/1hy/1y bar from the bars in
        the REQUEST window, so a window covering only today answers the weekly
        bar with today's volume and the week's earlier days lost. MiniQMT's
        get_local_data synthesizes from ALL local base data and start/end only
        filters the returned rows -- this rebuild is what makes the bridge
        match that.

        Structural triggers only, never a value guess: the period is in
        RESYNTH_ONGOING_PERIODS, the last bar's natural period contains today,
        the request window cuts into the period (start_time past its start),
        and the clock is inside a weekday session -- after close the bar is
        finalized and passes through untouched.

        Degrades like the preClose backfill (#166): a failed or empty daily
        read keeps the terminal's answer and warns once; nothing raises.
        """
        if str(period or "") not in RESYNTH_ONGOING_PERIODS or not isinstance(data, dict):
            return data
        start_text = str(start_time or "")
        if not start_text:
            return data          # count-based reads never showed the truncation
        now = _now()
        if not _in_trading_session(now):
            return data
        today = now.strftime("%Y%m%d")

        targets = {}
        for code, frame in data.items():
            labels = _frame_bar_labels(frame)
            if not labels:
                continue
            label = labels[-1]
            period_start = RESYNTH_PERIOD_STARTS[period](label)
            period_end = _period_end_label(period, label)
            if period_start is None or period_end is None:
                continue
            if not (period_start <= today <= period_end):
                continue          # the last bar is finalized history
            if _digits_only(start_text)[:8] <= period_start:
                continue          # the window already covers the period
            targets[code] = period_start
        if not targets:
            return data

        try:
            daily = self.get_market_data_ex(
                field_list=["open", "high", "low", "close", "volume", "amount"],
                stock_list=sorted(targets), period="1d",
                start_time=min(targets.values()), end_time=today, count=-1,
                dividend_type=dividend_type, fill_data=False,
                timeout_seconds=timeout_seconds, use_formula=use_formula,
                backfill_pre_close=False, resynth_ongoing_multiday=False,
            )
        except Exception as exc:
            # The terminal's answer was good enough to serve; taking the whole
            # read down over the rebuild would be the worse bug.
            log.warning("ongoing %s bar rebuild skipped: %s", period, exc)
            return data

        rebuilt = 0
        for code, period_start in targets.items():
            frame = data.get(code)
            daily_frame = daily.get(code) if isinstance(daily, dict) else None
            summary = self._sum_daily_bars(daily_frame, period_start, today)
            if summary is None:
                continue
            if self._write_last_bar(frame, summary):
                rebuilt += 1
        if rebuilt:
            self._notice_resynth_ongoing(period, rebuilt)
        return data

    @staticmethod
    def _sum_daily_bars(frame, period_start, today):
        """open/high/low/close/volume/amount for one ongoing period from its
        daily bars, or None when there is nothing to sum."""
        labels = _frame_bar_labels(frame) if frame is not None else None
        if not labels:
            return None
        try:
            columns = {name: list(frame[name]) for name in
                       ("open", "high", "low", "close", "volume", "amount")}
        except Exception:
            return None
        rows = []
        for index, label in enumerate(labels):
            day = _digits_only(label)[:8]
            if not day or day < period_start or day > today:
                continue
            rows.append({name: _bar_float(columns[name][index])
                         for name in columns})
        rows = [row for row in rows if row["close"] is not None]
        if not rows:
            return None
        out = {
            "open": next((row["open"] for row in rows if row["open"] is not None), None),
            "high": max((row["high"] for row in rows if row["high"] is not None), default=None),
            "low": min((row["low"] for row in rows if row["low"] is not None), default=None),
            "close": next((row["close"] for row in reversed(rows) if row["close"] is not None), None),
        }
        for name in ("volume", "amount"):
            values = [row[name] for row in rows if row[name] is not None]
            out[name] = sum(values) if values else None
        return out

    @staticmethod
    def _write_last_bar(frame, summary):
        """Write the rebuilt values into the last bar, only into columns the
        caller actually asked for. True when anything was written."""
        wrote = False
        for name, value in summary.items():
            if value is None:
                continue
            try:
                values = list(frame[name])
            except Exception:
                continue          # the column was never requested
            if not values:
                continue
            values[-1] = value
            try:
                frame[name] = values
                wrote = True
            except Exception:
                continue
        return wrote

    @staticmethod
    def _notice_resynth_ongoing(period, count):
        """Say once that ongoing bars are rebuilt here, not answered by QMT."""
        if _RESYNTH_NOTICE["shown"]:
            return
        _RESYNTH_NOTICE["shown"] = True
        try:
            log.info(
                "the ongoing %s bar(s) of %d code(s) were rebuilt from the "
                "period's daily bars (issue #226): big QMT synthesizes them "
                "from the request window, miniQMT from all local data. Pass "
                "resynth_ongoing_multiday=False to serve the raw window "
                "answer.", period, count)
        except Exception:
            pass

    @staticmethod
    def _notice_pre_close_backfill(period):
        """Say once that these preClose values are derived, not the terminal's."""
        if _PRE_CLOSE_NOTICE["shown"]:
            return
        _PRE_CLOSE_NOTICE["shown"] = True
        try:
            log.info(
                "period %s came back with preClose = 0.0 for every bar, so it was "
                "filled from the daily preClose of each period's first trading day "
                "(issue #166). That is the same value MiniQMT reports, including on "
                "an ex-dividend day, but it is computed here rather than answered by "
                "the terminal -- pass backfill_pre_close=False to get the raw bars.",
                period)
        except Exception:
            pass

    def get_market_data_ex(
        self,
        field_list=None,
        stock_list=None,
        period="1d",
        start_time="",
        end_time="",
        count=-1,
        dividend_type="none",
        fill_data=True,
        chunk_size=None,
        timeout_seconds=None,
        use_formula=True,
        backfill_pre_close=True,
        resynth_ongoing_multiday=True,
    ):
        """Pull bars over RPC, in batches of ``chunk_size`` codes.

        Cache-through: whatever is fetched is written to the local cache (keyed
        by dividend_type), so it stays the latest -- important for 前复权 data,
        whose history re-scales on each dividend.

        Batching exists because one request carrying every code shares a single
        RPC timeout (6s by default), so a wide stock_list times out and loses
        the whole pull rather than degrading (issue #47). Splitting keeps each
        request small enough to answer, and a batch that still fails only costs
        its own codes -- the rest are returned.

        ``chunk_size=0`` restores the old single-request behaviour.

        An empty ``field_list`` means "every field", which only the RPC path can
        answer: FormulaServer has the six bar columns plus time, and not the
        four daily ones (settelementPrice, openInterest, preClose,
        suspendFlag). Naming the fields you actually want is what unlocks the
        direct path -- 0.015s against 5.8s, measured (issue #104).

        Asking it for a field it lacks is now refused at the router and served
        by RPC instead. It used to answer with a column of NaN, so naming all
        eleven columns looked like a free speedup and quietly cost four of them
        (see formula_server.SERVED_FIELDS).
        """
        _notice_field_list_cost(field_list)
        codes = list(stock_list or [])
        base = dict(
            field_list=list(field_list or []),
            period=period,
            start_time=start_time,
            end_time=end_time,
            count=count,
            dividend_type=dividend_type,
            fill_data=fill_data,
        )
        step = DEFAULT_MARKET_DATA_CHUNK if chunk_size is None else int(chunk_size)

        if step <= 0 or len(codes) <= step:
            data = self._get_market_data_ex_batch(
                dict(base, stock_list=codes), timeout_seconds=timeout_seconds,
                use_formula=use_formula,
            )
        else:
            data = {}
            failures = []
            for index in range(0, len(codes), step):
                batch = codes[index:index + step]
                try:
                    part = self._get_market_data_ex_batch(
                        dict(base, stock_list=batch), timeout_seconds=timeout_seconds,
                        use_formula=use_formula,
                    )
                except Exception as exc:
                    # Losing one batch must not lose the others: a partial
                    # result beats an exception when 500 codes were asked for.
                    failures.append((batch, exc))
                    continue
                if isinstance(part, dict):
                    data.update(part)
            if failures and not data:
                # Nothing came back at all -- surface the first cause rather
                # than returning a silent empty dict.
                raise failures[0][1]
            for batch, exc in failures:
                print("[bigqmt_client] get_market_data_ex batch failed (%d codes, first=%s): %s"
                      % (len(batch), batch[0] if batch else "", exc))

        if backfill_pre_close:
            # Before the cache write, so the cache keeps the corrected bars
            # rather than a copy of the zeros (#166).
            data = self._backfill_pre_close(
                data, period=period, dividend_type=dividend_type,
                timeout_seconds=timeout_seconds, use_formula=use_formula,
            )

        if resynth_ongoing_multiday:
            # Same rule as the backfill: the cache keeps the rebuilt bar, not
            # the window-truncated one (#226).
            data = self._resynth_ongoing_multiday_bars(
                data, period=period, start_time=start_time, end_time=end_time,
                dividend_type=dividend_type, timeout_seconds=timeout_seconds,
                use_formula=use_formula,
            )

        cache = self._local_cache()
        if cache is not None and isinstance(data, dict):
            for code, df in data.items():
                try:
                    cache.write(code, period, df, dividend_type=dividend_type)
                except Exception:
                    pass
        return data

    def get_local_data(
        self,
        field_list=None,
        stock_list=None,
        period="1d",
        start_time="",
        end_time="",
        count=-1,
        dividend_type="none",
        fill_data=True,
        data_dir=None,
    ):
        """Read bars from the CLIENT-side local cache — no RPC to Big QMT.

        Returns a dict {code: DataFrame}. A cache-missed code is omitted, unless
        local_cache_fallback_rpc is enabled (then it is fetched + cached over RPC).
        """
        codes = [str(c) for c in (stock_list or []) if str(c or "").strip()]
        cache = self._local_cache()
        if cache is None:
            # Cache disabled -> behave like a plain RPC local-data read.
            return self._call(
                "get_local_data",
                field_list=_as_list(field_list),
                stock_list=codes,
                period=period,
                start_time=start_time,
                end_time=end_time,
                count=count,
                dividend_type=dividend_type,
                fill_data=fill_data,
                data_dir=data_dir,
            )
        fields = list(field_list or [])
        result = {}
        missing = []
        for code in codes:
            df = cache.read(code, period, start_time, end_time, count, dividend_type=dividend_type)
            if df is not None and getattr(df, "shape", (0,))[0] > 0:
                result[code] = self._select_fields(
                    _normalize_market_data_frame(df, field_list=fields),
                    fields,
                )
            else:
                missing.append(code)
        if missing and _bool_value(self.client.local_cache_config.get("fallback_rpc"), False):
            fetched = self._pull_and_cache(missing, period, start_time, end_time, count, dividend_type)
            for code in missing:
                df = fetched.get(code)
                if df is not None and getattr(df, "shape", (0,))[0] > 0:
                    result[code] = self._select_fields(
                        _normalize_market_data_frame(df, field_list=fields),
                        fields,
                    )
        return result

    @staticmethod
    def _select_fields(df, fields):
        if not fields:
            return df
        try:
            keep = [c for c in df.columns if c in fields or (c in _TIME_COL_NAMES and c != "stime")]
            return df[keep] if keep else df
        except Exception:
            return df

    @staticmethod
    def _is_all_zero_any(data):
        """Detect the all-zero adjusted-bars symptom (server lacks raw data).

        Big QMT computes front/back-adjusted bars from raw bars + dividend
        factors; when those are missing server-side the price columns come
        back all 0.0 (only the last bar may hold the live price). Recursively
        handles DataFrame, {code: DataFrame} and {field: {code: [..]}} shapes.
        """
        try:
            if data is None:
                return False
            cols = getattr(data, "columns", None)
            if cols is not None:  # pandas DataFrame
                if "close" not in list(cols):
                    return False
                closes = data["close"]
                if len(closes) == 0:
                    return False
                head = closes.iloc[:-1] if len(closes) > 1 else closes
                return bool((head == 0).all())
            if isinstance(data, dict):
                return any(BigQmtXtData._is_all_zero_any(v) for v in data.values())
            if isinstance(data, (list, tuple)) and data and all(
                isinstance(x, (int, float)) for x in data
            ):
                head = data[:-1] if len(data) > 1 else data
                return bool(head) and all(x == 0 for x in head)
            return False
        except Exception:
            return False

    def _ensure_server_raw(self, codes, period, start_time, end_time):
        """Trigger a server-side raw download so adjusted bars can be computed."""
        try:
            self.client.call(
                "download_history_data2",
                {
                    "stock_list": list(codes),
                    "period": period,
                    "start_time": start_time,
                    "end_time": end_time,
                },
                timeout_seconds=60.0,
            )
        except Exception:
            pass

    @staticmethod
    def _served_codes(data):
        """Codes the server actually served, across both return shapes:
        get_market_data_ex is code-keyed ({code: DataFrame}), get_market_data
        is field-keyed ({field: {code: [..]}}) -- reading keys off the wrong
        level would make every code look missing."""
        if not isinstance(data, dict):
            return set()
        nested = {code for value in data.values() if isinstance(value, dict) for code in value}
        return nested if nested else set(data.keys())

    def _heal_adjusted(self, method, params, data, wait_seconds=2.0, timeout_seconds=None):
        """Self-heal reads served from an unready raw store: if the adjusted
        pull came back all-zero, or a none-adjusted pull came back missing
        most requested codes, trigger a server-side raw download, wait for
        async landing, retry once."""
        dividend_type = str(params.get("dividend_type") or "none").lower()
        codes = list(params.get("stock_list") or params.get("stock_code") or [])
        if not codes:
            return data
        if dividend_type in ("", "none"):
            # None-adjusted bars are never zero-filled, so the all-zero
            # detector does not apply -- a *missing* code means the server
            # has no raw bars for it at all. Big QMT's raw store is not
            # auto-populated market-wide (a --tick pipeline read 8 of 5225
            # codes on 2026-08-30 because nothing ever downloaded the raw
            # dailies), so heal that the same way. But a full-market read
            # always has a few codes the server can never serve (delisted,
            # suspended, no quote permission), and healing on *any* missing
            # code made those a per-call cost -- raw download + sleep + full
            # re-read, every time. Heal only when the majority came back
            # missing: that is the raw-store-not-populated signal.
            served = self._served_codes(data)
            missing = sum(1 for code in codes if code not in served)
            if missing < max(1, len(codes) // 2):
                return data
        elif not self._is_all_zero_any(data):
            return data
        self._ensure_server_raw(
            codes,
            params.get("period", "1d"),
            params.get("start_time", ""),
            params.get("end_time", ""),
        )
        time.sleep(wait_seconds)
        if timeout_seconds is not None:
            return self.client.call(method, params, timeout_seconds=timeout_seconds)
        return self._call(method, **params)

    def _pull_and_cache(self, codes, period, start_time, end_time, count, dividend_type="none"):
        """Fetch codes over RPC (get_market_data_ex already caches them)."""
        data = self.get_market_data_ex(
            field_list=DEFAULT_DOWNLOAD_FIELDS,
            stock_list=list(codes),
            period=period,
            start_time=start_time,
            end_time=end_time,
            count=count,
            dividend_type=dividend_type,
        )
        out = {}
        for code in codes:
            df = data.get(code) if isinstance(data, dict) else None
            if df is not None and getattr(df, "shape", (0,))[0] > 0:
                out[code] = df
        return out

    def subscribe_quote(self, stock_code, period="1d", start_time="", end_time="", count=0, callback=None):
        """Subscribe to one instrument, MiniQMT-style: the callback keeps firing.

        This used to invoke the callback exactly once and never again -- a
        one-shot fetch wearing a subscription's name, which is worse than not
        having it, because it looks like it works (issue #95).

        Ticks ride the whole-quote push channel; a single code is just a
        one-element code list. K-lines have no server-side push -- the bridge
        only exposes ContextInfo.subscribe_whole_quote, which carries ticks --
        so they are polled and emitted when the newest bar changes.
        """
        payload = {
            "stock_code": stock_code,
            "period": period,
            "start_time": start_time,
            "end_time": end_time,
            "count": count,
        }

        if str(period).lower() in ("tick", "full_tick"):
            session = self._whole_quote_session()
            session.start()
            seq = session.subscribe_whole_quote([stock_code], callback=callback)
            # The whole-quote callback is incremental; prime it with a snapshot
            # so a subscriber is not left with nothing until the first change.
            if callback is not None:
                try:
                    callback(self.get_full_tick([stock_code]))
                except Exception:
                    pass
            self._record_subscription(seq, payload)
            return seq

        seq = self._next_seq()

        def fetch():
            # 盘中形成 bar 必须读 QMT 实时数据——FormulaServer 快照可能滞后数
            # 小时（实测 11:30 后冻结，收盘后还停在午间数据），订阅推送如果走
            # 直连会把"刚完成的 bar"发成数小时前的旧快照（issue #104 实测）。
            return self.get_market_data_ex(
                stock_list=[stock_code], period=period,
                start_time=start_time, end_time=end_time, count=count or 1,
                use_formula=False)

        poller = _BarPoller(
            fetch, callback, self._bar_poll_interval_seconds(),
            on_error=lambda exc: log.debug(
                "subscribe_quote poll failed code=%s period=%s: %s",
                stock_code, period, exc),
            on_no_data=lambda: log.warning(
                "subscribe_quote: no %s bars for %s -- the subscription is live "
                "but stays silent until this terminal has data for that period. "
                "Check the period is downloaded (get_market_data_ex returns an "
                "empty frame for it).", period, stock_code))
        with self._bar_poller_lock:
            self._bar_pollers[seq] = poller
        poller.start()
        self._record_subscription(seq, payload)
        return seq

    def _bar_poll_interval_seconds(self):
        config = dict(getattr(self.client, "full_tick_cache_config", {}) or {})
        value = (config.get("bar_poll_interval_seconds")
                 or os.environ.get("BIGQMT_BAR_POLL_INTERVAL_SECONDS"))
        try:
            return float(value)
        except (TypeError, ValueError):
            return DEFAULT_BAR_POLL_INTERVAL_SECONDS

    def _record_subscription(self, seq, payload, active=True):
        """Bookkeeping only -- nothing on the server consumes it, and it needs a
        Redis client, which a zmq deployment does not have. Never let it break
        an otherwise working subscription."""
        try:
            self.client.save_quote_subscription(seq, dict(payload, seq=seq), active=active)
            self.client.publish_event(
                "subscribe_quote" if active else "unsubscribe_quote",
                dict(payload, seq=seq))
        except Exception:
            pass

    def subscribe_quote2(self, stock_code, period="1d", start_time="", end_time="", count=0, dividend_type=None, callback=None):
        return self.subscribe_quote(
            stock_code=stock_code,
            period=period,
            start_time=start_time,
            end_time=end_time,
            count=count,
            callback=callback,
        )

    def _whole_quote_session(self):
        if self._quote_session is None:
            if self._quote_session_factory is not None:
                self._quote_session = self._quote_session_factory()
            else:
                self._quote_session = self._build_quote_session()
        return self._quote_session

    def _build_quote_session(self):
        from .whole_quote_session import WholeQuoteClientSession

        client = self.client

        def rpc_call(method, params):
            return client.call(method, params)

        return WholeQuoteClientSession(
            rpc_call=rpc_call,
            push_channel=self._build_quote_push_channel(),
            client_id=_quote_client_id(),
            heartbeat_interval_seconds=_env_float("BIGQMT_QUOTE_HEARTBEAT_SECONDS", 3.0),
            sub_id_func=self._next_seq,
        )

    def _build_quote_push_channel(self):
        """Build the push-channel subscriber matching the RPC transport: redis
        deployments derive the channel locally; zmq deployments connect to the
        server PUB socket (host from zmq config, RPC port + 1)."""
        client = self.client
        from .quote_push_channel import RedisQuotePushChannel, ZmqQuotePushChannel

        transport_name = str(getattr(client, "transport_name", "redis") or "redis").lower()
        if transport_name in ("zmq",):
            address = _quote_push_zmq_address(client)
            return ZmqQuotePushChannel(connect_address=address)
        return RedisQuotePushChannel(client._redis(), account_id=client.account_id)

    def quote_subscription_status(self):
        """What whole-quote combos the bridge thinks are alive, and how stale.

        The diagnostic for "something keeps pushing and I lost the seq": a
        combo staying fresh has a LIVE keepalive feeding it (a leftover client
        process); one going silent past the heartbeat timeout is about to be
        reaped by the server (measured live: ~30s after the client dies).
        """
        return self.client.call("quote_subscription_status", {})

    def quote_unsubscribe_all(self):
        """Kill every whole-quote subscription on the bridge, no seq needed.

        keepalive is a no-op on unknown sub_ids, so a force-cleared combo
        stays down until someone subscribes again.
        """
        return self.client.call("quote_unsubscribe_all", {})

    _SUBSCRIBE_PRIME_MAX_CODES = 100

    def subscribe_whole_quote(self, code_list, callback=None):
        session = self._whole_quote_session()
        session.start()
        sub_id = session.subscribe_whole_quote(code_list, callback=callback)
        # The big-QMT whole-quote callback is incremental (changed symbols only),
        # so prime the callback once with a full get_full_tick snapshot.
        #
        # types=["all"] deliberately: the push side is ContextInfo's own
        # subscribe_whole_quote, which is not narrowed, so a narrowed snapshot
        # would hand the subscriber 2315 stocks and then start pushing all
        # 26744 instruments. The primer has to cover what the push covers.
        #
        # Large individual code lists (e.g. 3000 stocks) would block the server
        # adjust thread for seconds via get_full_tick(thousands_of_codes). Instead,
        # extract the exchange tokens (SH/SZ/...) from the codes and call
        # get_full_tick with those tokens -- QMT handles exchange tokens as
        # whole-exchange operations (fast). Then filter the result to only the
        # codes the caller actually asked for.
        if callback is not None:
            try:
                # Called unconditionally, including with an empty snapshot:
                # that is what this did before #247, and a subscriber that
                # waits for its first callback must not hang because the
                # primer happened to come back empty.
                callback(self._prime_snapshot(code_list))
            except Exception:
                pass
        return sub_id

    # Exchange tokens QMT actually answers a whole-market snapshot for.
    # Measured on the live terminal: SH 卡 SZ together return 5218 rows in
    # ~330ms, BJ returns 343. Every futures token -- SF DF ZF IF INE GF --
    # returns **0 rows**, so routing a futures list through the token path
    # yields an empty prime, and the subscriber silently never gets a first
    # frame. That is the #95 shape again: the subscription looks alive, the
    # snapshot is just missing.
    _WHOLE_MARKET_TOKENS = ("SH", "SZ", "BJ")

    def _prime_snapshot(self, code_list):
        """First-frame snapshot for subscribe_whole_quote.

        Per-code get_full_tick runs on QMT's adjust thread, and its cost grows
        with the list: measured medians on the live terminal were ~170ms at 100
        codes, ~500ms at 200, ~2.5s at 500, ~9.5s at 1000, and a hard
        TimeoutError at 3000 -- with the adjust thread blocked for that whole
        time, which stalls the drain and queues every other RPC behind it
        (#247). A whole-market token is ~330ms flat regardless of size, so
        above the threshold the token path wins.

        Codes are passed to QMT **verbatim**; only the exchange suffix is
        upper-cased for routing and only upper-cased copies are used for
        matching. Big QMT has cu2610.SF and not CU2610.SF, so upper-casing a
        code before sending it silently kills futures subscriptions (#58/#95).
        """
        codes = [str(c).strip() for c in (code_list or []) if str(c or "").strip()]
        if not codes:
            return {}
        if len(codes) <= self._SUBSCRIBE_PRIME_MAX_CODES:
            return self.get_full_tick(codes, types=["all"]) or {}

        # Split by whether this code's exchange can be primed wholesale.
        whole, direct = [], []
        for code in codes:
            suffix = code.rsplit(".", 1)[-1].upper() if "." in code else ""
            (whole if suffix in self._WHOLE_MARKET_TOKENS else direct).append(code)

        snapshot = {}
        if whole:
            wanted = set()
            tokens = set()
            for code in whole:
                wanted.add(code.upper())
                tokens.add(code.rsplit(".", 1)[-1].upper())
            full = self.get_full_tick(sorted(tokens)) or {}
            snapshot.update(
                (k, v) for k, v in full.items() if str(k).upper() in wanted)
        if direct:
            # No whole-market token for these (futures, and anything new).
            # Slow for a long list, but slow-and-correct beats fast-and-empty:
            # before #247 this was the only path, so it is not a regression.
            snapshot.update(self.get_full_tick(direct, types=["all"]) or {})
        return snapshot

    def unsubscribe_quote(self, seq):
        # Three kinds of handle now: whole-quote / tick subscriptions owned by
        # the push session, K-line pollers owned here, and legacy seqs that only
        # ever existed as redis bookkeeping.
        with self._bar_poller_lock:
            poller = self._bar_pollers.pop(seq, None)
        if poller is not None:
            poller.stop()
            self._record_subscription(seq, {}, active=False)
            return 0

        session = self._quote_session
        if session is not None and session.has_subscription(seq):
            session.unsubscribe_quote(seq)
            self._record_subscription(seq, {}, active=False)
            return 0

        self._record_subscription(seq, {}, active=False)
        return 0

    def stop_all_subscriptions(self):
        """Stop every K-line poller this object owns. Daemon threads die with
        the process anyway; this is for tests and for callers that recycle a
        client without exiting."""
        with self._bar_poller_lock:
            pollers = list(self._bar_pollers.values())
            self._bar_pollers.clear()
        for poller in pollers:
            poller.stop()
        return len(pollers)

    def run(self):
        while True:
            time.sleep(3600)

    def get_divid_factors(self, stock_code, start_time="", end_time=""):
        return self._call("get_divid_factors", stock_code=stock_code, start_time=start_time, end_time=end_time)

    def download_history_data2(self, stock_list, period, start_time="", end_time="", callback=None, incrementally=None, dividend_type="none", chunk_size=None, download_timeout_seconds=180.0, data_wait_seconds=60.0):
        """Pull bars from Big QMT over RPC and cache them locally, in batches.

        Mirrors xtdata.download_history_data2: after this, get_local_data(..., the
        same dividend_type) reads the data locally with no further RPC. Each batch
        re-pulls live, so re-running keeps the cache latest — needed for 前复权
        (front-adjusted) data. ``callback`` (optional) is invoked once per stock with
        {finished, total, stockcode} — xtdata-style. Returns {finished, total}.

        The server-side download runs for EVERY dividend_type, matching
        xtdata semantics ("populate the local QMT store"). It used to be skipped
        for unadjusted pulls, which made an unadjusted download a no-op that
        still reported progress (issue #47).

        Adjusted data (dividend_type != none) additionally depends on it: Big QMT
        computes adjusted bars from the RAW history + dividend factors, and
        without both, get_market_data_ex(dividend_type='front') returns all-zero
        closes (verified live).

        ``download_timeout_seconds`` covers the server-side download only; it is
        generous because a cold code with a wide window can take minutes.

        The server-side download is best-effort while the client pull can still
        save it (cache enabled), but with the local cache disabled it is the
        entire job -- its failure raises instead of reporting {finished: total},
        which would be the fake progress of issue #47.
        """
        codes = [str(c) for c in (stock_list or []) if str(c or "").strip()]
        if not codes:
            return {"finished": 0, "total": 0}

        # Server-side download first, for EVERY dividend_type.
        #
        # This used to run only when adjustment was requested, on the reasoning
        # that an unadjusted pull can be served straight from get_market_data_ex.
        # That reads whatever Big QMT already has -- it does not fetch anything.
        # So an unadjusted "download" left the QMT-side store untouched while
        # still reporting {finished: N} through the callback: a progress bar for
        # work that never happened (issue #47, and the real cause behind #39,
        # which was closed on an incomplete reading of this function).
        #
        # xtdata.download_history_data means "populate the local QMT store", and
        # callers depend on that: FormulaServer and get_local_data both read it,
        # and codes "downloaded" this way had zero bars there.
        #
        # Adjusted data additionally NEEDS this: QMT computes front/back-adjusted
        # bars from raw bars + dividend factors, and both must exist server-side
        # or the result is all zeros.
        server_download_error = None
        try:
            self.client.call(
                "download_history_data2",
                {
                    "stock_list": codes,
                    "period": period,
                    "start_time": start_time,
                    "end_time": end_time,
                },
                timeout_seconds=float(download_timeout_seconds),
            )
        except Exception as exc:
            # Best-effort only while the pull below can still save the
            # download (data already on the server). With the local cache
            # disabled there is no pull -- the server-side download is the
            # whole job, and reporting {finished: total} after a failed one
            # is the fake progress of issue #47.
            server_download_error = exc

        if self._local_cache() is None:
            # local cache disabled: server-side download only (step 1). The
            # data lands in the server-side DATs, so this is real work -- not
            # the fake progress download of issue #47. The client pull is
            # skipped uniformly for every period (tick and bars alike) and
            # progress is reported per code without any pull.
            if server_download_error is not None:
                raise server_download_error
            total = len(codes)
            for index, code in enumerate(codes, 1):
                if callback is not None:
                    try:
                        callback({"finished": index, "total": total, "stockcode": code})
                    except Exception:
                        pass
            return {"finished": total, "total": total}

        total = len(codes)
        step = int(chunk_size or 300)
        if step <= 0:
            step = 300
        finished = 0
        for i in range(0, total, step):
            batch = codes[i:i + step]
            # QMT 的下载全局是「提交任务即返回」，数据在服务端异步落地
            # （秒~分钟级）。下载后立刻读只能看到旧数据——issue #66 里
            #「tick 只能获得最近 1 天」的真正原因就是这个竞态：数据还没落地
            # 就已经被读走并缓存了空结果。这里分批轮询，直到批内每个代码都
            # 出现真实数据行或超时（超时容忍停牌/退市等确实无数据的代码）。
            deadline = time.time() + float(data_wait_seconds)
            while True:
                # get_market_data_ex 是 cache-through：每次轮询都会写入缓存，
                # 最后一次（数据齐或超时）的结果即最终缓存内容。
                data = self.get_market_data_ex(
                    field_list=DEFAULT_DOWNLOAD_FIELDS,
                    stock_list=batch,
                    period=period,
                    start_time=start_time,
                    end_time=end_time,
                    count=-1,
                    dividend_type=dividend_type,
                    fill_data=False,  # fill 会用全 0 占位行冒充数据，轮询判定必须关掉
                    timeout_seconds=float(data_wait_seconds),
                )
                ready = 0
                for code in batch:
                    df = (data or {}).get(code)
                    if df is not None and getattr(df, "shape", (0,))[0] > 0:
                        ready += 1
                if ready >= len(batch) or time.time() >= deadline:
                    break
                time.sleep(1.5)
            for code in batch:
                finished += 1
                if callback is not None:
                    try:
                        callback({"finished": finished, "total": total, "stockcode": code})
                    except Exception:
                        pass
        return {"finished": finished, "total": total}

    def download_history_data(self, stock_code, period, start_time="", end_time="", incrementally=None, dividend_type="none"):
        return self.download_history_data2([stock_code], period, start_time, end_time, dividend_type=dividend_type)

    def local_cache_stats(self):
        """Return (cached files, periods) for the client-side local cache."""
        cache = self._local_cache()
        return cache.stats() if cache is not None else (0, [])

    def get_trading_dates(self, market, start_time="", end_time="", count=-1):
        return self._call("get_trading_dates", market=market, start_time=start_time, end_time=end_time, count=count)

    def get_holidays(self):
        return self._call("get_holidays")

    def download_holiday_data(self, incrementally=True):
        return self._call("download_holiday_data", incrementally=incrementally)

    def download_his_st_data(self, incrementally=True):
        return self._call("download_his_st_data", incrementally=incrementally)

    def get_ipo_info(self, start_time="", end_time=""):
        return self._call("get_ipo_info", start_time=start_time, end_time=end_time)

    def get_etf_info(self):
        return self._call("get_etf_info")

    def download_etf_info(self):
        return self._call("download_etf_info")

    # 下面四个的服务端实现和 RPC 白名单一直都在（market_bigqmt 的
    # download_* 方法 + redis_rpc 的 MARKET_DATA_METHODS），只是客户端漏了这层
    # 包装，于是外部调用直接撞 AttributeError（issue #130）。
    def download_sector_data(self):
        return self._call("download_sector_data")

    def download_cb_data(self):
        return self._call("download_cb_data")

    def download_index_weight(self):
        return self._call("download_index_weight")

    def download_history_contracts(self, incrementally=True):
        # 形参保留是为了和 xtdata.download_history_contracts(incrementally=True)
        # 签名一致；大 QMT 那边这个调用没有增量参数，服务端按全量下载处理。
        return self._call("download_history_contracts")

    def get_option_list(self, undl_code, dedate, opttype="", isavailavle=False):
        return self._call("get_option_list", undl_code=undl_code, dedate=dedate, opttype=opttype, isavailavle=isavailavle)

    def get_his_option_list(self, undl_code, dedate):
        return self._call("get_his_option_list", undl_code=undl_code, dedate=dedate)

    def get_his_option_list_batch(self, undl_code, start_time="", end_time=""):
        return self._call("get_his_option_list_batch", undl_code=undl_code, start_time=start_time, end_time=end_time)

    def get_financial_data(self, stock_list, table_list=None, start_time="", end_time="", report_type="report_time"):
        return self._call(
            "get_financial_data",
            stock_list=list(stock_list or []),
            table_list=list(table_list or []),
            start_time=start_time,
            end_time=end_time,
            report_type=report_type,
        )

    def download_financial_data(self, stock_list, table_list=None, start_time="", end_time="", incrementally=None):
        return self._call(
            "download_financial_data",
            stock_list=list(stock_list or []),
            table_list=list(table_list or []),
            start_time=start_time,
            end_time=end_time,
            incrementally=incrementally,
        )

    def download_financial_data2(self, stock_list, table_list=None, start_time="", end_time="", callback=None):
        result = self._call(
            "download_financial_data2",
            stock_list=list(stock_list or []),
            table_list=list(table_list or []),
            start_time=start_time,
            end_time=end_time,
        )
        if callback is not None:
            callback(result)
        return result

    def get_sector_list(self, allow_fallback=False):
        """Sector names, or an error saying the terminal cannot list them.

        ``allow_fallback=True`` opts into the 13 curated well-known names,
        which still drive ``get_stock_list_in_sector``. Big QMT cannot
        enumerate real sectors at all, and handing back the curated list
        unasked made a fake answer indistinguishable from a real one (#143).
        """
        return self._call("get_sector_list", allow_fallback=bool(allow_fallback))

    def get_sector_info(self, sector_name=""):
        return self._call("get_sector_info", sector_name=sector_name)

    def get_markets(self):
        return self._call("get_markets")

    def get_market_last_trade_date(self, market):
        return self._call("get_market_last_trade_date", market=market)

    def call_formula(self, formula_name, stock_code, period, start_time="", end_time="", count=-1, dividend_type=None, extend_param=None):
        return self._call(
            "call_formula",
            formula_name=formula_name,
            stock_code=stock_code,
            period=period,
            start_time=start_time,
            end_time=end_time,
            count=count,
            dividend_type=dividend_type,
            extend_param=extend_param or {},
        )

    def subscribe_formula(self, formula_name, stock_code, period, start_time="", end_time="", count=-1, dividend_type=None, extend_param=None, callback=None):
        result = self._call(
            "subscribe_formula",
            formula_name=formula_name,
            stock_code=stock_code,
            period=period,
            start_time=start_time,
            end_time=end_time,
            count=count,
            dividend_type=dividend_type,
            extend_param=extend_param or {},
        )
        if callback is not None:
            callback(result)
        return result

    def unsubscribe_formula(self, request_id):
        return self._call("unsubscribe_formula", request_id=request_id)

    def get_formula_result(self, request_id, start_time="", end_time="", count=-1, timeout_second=-1):
        return self._call(
            "get_formula_result",
            request_id=request_id,
            start_time=start_time,
            end_time=end_time,
            count=count,
            timeout_second=timeout_second,
        )

    def gen_factor_index(self, data_name, formula_name, vars, sector_list, start_time="", end_time="", period="1d", dividend_type="none"):
        return self._call(
            "gen_factor_index",
            data_name=data_name,
            formula_name=formula_name,
            vars=vars,
            sector_list=list(sector_list or []),
            start_time=start_time,
            end_time=end_time,
            period=period,
            dividend_type=dividend_type,
        )

    # ------------------------------------------------------------------
    # 扩展行情/基本面方法（对应 ContextInfo 方法，走 RPC 白名单）。
    # 仅对最常用的显式声明签名；其余通过 __getattr__ 自动转发。
    # ------------------------------------------------------------------

    def get_longhubang(self, stock_list=None, start_time="", end_time="", count=-1):
        return self._call(
            "get_longhubang",
            stock_list=list(stock_list or []),
            start_time=start_time,
            end_time=end_time,
            count=count,
        )

    def get_top10_share_holder(self, stock_list, data_name, start_time, end_time, report_type="report_time"):
        return self._call(
            "get_top10_share_holder",
            stock_list=list(stock_list or []),
            data_name=data_name,
            start_time=start_time,
            end_time=end_time,
            report_type=report_type,
        )

    def get_holder_num(self, stock_list=None, start_time="", end_time="", report_type="report_time"):
        return self._call(
            "get_holder_num",
            stock_list=list(stock_list or []),
            start_time=start_time,
            end_time=end_time,
            report_type=report_type,
        )

    def get_turnover_rate(self, stock_code=None, start_time="19720101", end_time="22010101"):
        return self._call(
            "get_turnover_rate",
            stock_code=list(stock_code or []),
            start_time=start_time,
            end_time=end_time,
        )

    def get_industry(self, industry_name):
        return self._call("get_industry", industry_name=industry_name)

    def bsm_price(self, opt_type, target_price, strike_price, risk_free, sigma, days, dividend=0):
        return self._call(
            "bsm_price",
            opt_type=opt_type,
            target_price=target_price,
            strike_price=strike_price,
            risk_free=risk_free,
            sigma=sigma,
            days=days,
            dividend=dividend,
        )

    def bsm_iv(self, opt_type, target_price, strike_price, option_price, risk_free, days, dividend=0):
        return self._call(
            "bsm_iv",
            opt_type=opt_type,
            target_price=target_price,
            strike_price=strike_price,
            option_price=option_price,
            risk_free=risk_free,
            days=days,
            dividend=dividend,
        )

    def get_option_iv(self, opt_code):
        return self._call("get_option_iv", opt_code=opt_code)

    def get_option_analytics(
        self,
        opt_code,
        option_price=None,
        underlying_price=None,
        as_of=None,
        risk_free_rate=None,
        dividend_yield=None,
        price_period="1m",
        include_native_iv=False,
    ):
        """Return client-side IV and Greeks for one option contract."""
        from .option_analytics_client import get_option_analytics

        return get_option_analytics(
            self,
            opt_code,
            option_price=option_price,
            underlying_price=underlying_price,
            as_of=as_of,
            risk_free_rate=risk_free_rate,
            dividend_yield=dividend_yield,
            price_period=price_period,
            include_native_iv=include_native_iv,
        )

    def get_option_chain_analytics(
        self,
        undl_code,
        dedate,
        opttype="",
        isavailavle=False,
        underlying_price=None,
        as_of=None,
        risk_free_rate=None,
        dividend_yield=None,
        price_period="1m",
    ):
        """Return batched client-side IV and Greeks for one option expiry."""
        from .option_analytics_client import get_option_chain_analytics

        return get_option_chain_analytics(
            self,
            undl_code,
            dedate,
            opttype=opttype,
            isavailavle=isavailavle,
            underlying_price=underlying_price,
            as_of=as_of,
            risk_free_rate=risk_free_rate,
            dividend_yield=dividend_yield,
            price_period=price_period,
        )

    def get_option_detail_data(self, stockcode):
        return self._call("get_option_detail_data", stockcode=stockcode)

    def get_option_undl_data(self, undl_code_ref=""):
        return self._call("get_option_undl_data", undl_code_ref=undl_code_ref)

    def get_option_undl(self, opt_code):
        return self._call("get_option_undl", opt_code=opt_code)

    def get_raw_financial_data(self, field_list, stock_list, start_time, end_time, report_type="report_time", data_type="dict"):
        return self._call(
            "get_raw_financial_data",
            field_list=list(field_list or []),
            stock_list=list(stock_list or []),
            start_time=start_time,
            end_time=end_time,
            report_type=report_type,
            data_type=data_type,
        )

    def get_factor_data(self, field_list, stock_list, start_date, end_date):
        return self._call(
            "get_factor_data",
            field_list=list(field_list or []),
            stock_list=list(stock_list or []),
            start_date=start_date,
            end_date=end_date,
        )

    def get_north_finance_change(self, period):
        return self._call("get_north_finance_change", period=period)

    def get_hkt_statistics(self, stock_code):
        return self._call("get_hkt_statistics", stock_code=stock_code)

    def get_hkt_details(self, stock_code):
        return self._call("get_hkt_details", stock_code=stock_code)

    # 自定义板块写入（issue #143）。每一个都在服务端写入后回读校验，所以
    # 「没报错」现在真的代表写进去了 —— 以前 create_sector 是静默空操作。
    def create_sector(self, sector_name, stock_list):
        return self._call("create_sector", sector_name=sector_name, stock_list=list(stock_list or []))

    def create_sector_folder(self, parent_node, folder_name, overwrite=False):
        return self._call("create_sector_folder", parent_node=parent_node,
                          folder_name=folder_name, overwrite=overwrite)

    def reset_sector_stock_list(self, sector, stock_list):
        return self._call("reset_sector_stock_list", sector=sector,
                          stock_list=list(stock_list or []))

    def add_stock_to_sector(self, sector, stock_code):
        return self._call("add_stock_to_sector", sector=sector, stock_code=stock_code)

    def remove_stock_from_sector(self, sector, stock_code):
        return self._call("remove_stock_from_sector", sector=sector, stock_code=stock_code)

    def get_stock_name(self, stock):
        return self._call("get_stock_name", stock=stock)

    def get_close_price(self, market, stock_code, real_timetag, period=86400000, divid_type=0):
        return self._call(
            "get_close_price",
            market=market,
            stock_code=stock_code,
            real_timetag=real_timetag,
            period=period,
            divid_type=divid_type,
        )

    def get_main_contract(self, code_market):
        return self._call("get_main_contract", code_market=code_market)

    def get_his_contract_list(self, market):
        return self._call("get_his_contract_list", market=market)

    def get_date_location(self, date):
        return self._call("get_date_location", date=date)

    def get_his_st_data(self, stock_code):
        return self._call("get_his_st_data", stock_code=stock_code)

    def get_his_index_data(self, stock_code):
        return self._call("get_his_index_data", stock_code=stock_code)

    def call_method(self, method, **params):
        """Generic escape hatch: call any RPC market-data method by name.

        Use this for ContextInfo methods that don't have an explicit wrapper
        above (e.g. ``xtdata.call_method("get_last_close", stock="000001.SZ")``,
        ``xtdata.call_method("get_float_caps", stockcode="000001.SZ")``). The
        full list of callable methods is in ``MARKET_DATA_METHODS``.
        """
        return self._call(method, **params)

    # ------------------------------------------------------------------
    # L2 行情（需 L2 权限 + 原生 xtdata SDK 行情服务）
    # ------------------------------------------------------------------

    def get_l2_quote(self, field_list=None, stock_code="", start_time="", end_time="", count=-1):
        return self._call("get_l2_quote", field_list=list(field_list or []),
                          stock_code=stock_code, start_time=start_time, end_time=end_time, count=count)

    def get_l2_order(self, field_list=None, stock_code="", start_time="", end_time="", count=-1):
        return self._call("get_l2_order", field_list=list(field_list or []),
                          stock_code=stock_code, start_time=start_time, end_time=end_time, count=count)

    def get_l2_transaction(self, field_list=None, stock_code="", start_time="", end_time="", count=-1):
        return self._call("get_l2_transaction", field_list=list(field_list or []),
                          stock_code=stock_code, start_time=start_time, end_time=end_time, count=count)

    # ------------------------------------------------------------------
    # 指数权重 / 交易日历 / 交易时段 / 可转债 / 品种判断
    # ------------------------------------------------------------------

    def get_index_weight(self, index_code):
        return self._call("get_index_weight", index_code=index_code)

    def get_trading_calendar(self, market, start_time="", end_time="", tradetimes=False):
        return self._call("get_trading_calendar", market=market, start_time=start_time,
                          end_time=end_time, tradetimes=tradetimes)

    def get_trade_times(self, stockcode):
        return self._call("get_trade_times", stockcode=stockcode)

    def get_cb_info(self, stockcode):
        return self._call("get_cb_info", stockcode=stockcode)

    def is_stock_type(self, stock, tag):
        return self._call("is_stock_type", stock=stock, tag=tag)

    # ------------------------------------------------------------------
    # 板块增删
    # ------------------------------------------------------------------

    def add_sector(self, sector_name, stock_list):
        return self._call("add_sector", sector_name=sector_name, stock_list=list(stock_list or []))

    def remove_sector(self, sector_name):
        return self._call("remove_sector", sector_name=sector_name)

    # ------------------------------------------------------------------
    # 时间戳转换（纯计算）
    # ------------------------------------------------------------------

    @staticmethod
    def datetime_to_timetag(datetime_str, format="%Y%m%d%H%M%S"):
        import datetime as _dt
        try:
            return int(_dt.datetime.strptime(str(datetime_str), format).timestamp() * 1000)
        except Exception:
            return 0

    @staticmethod
    def timetag_to_datetime(timetag, format):
        import datetime as _dt
        try:
            return _dt.datetime.fromtimestamp(int(timetag) / 1000.0).strftime(format)
        except Exception:
            return ""

    @staticmethod
    def timetagToDateTime(timetag, format):
        return BigQmtXtData.timetag_to_datetime(timetag, format)


class BigQmtXtTrader:
    def __init__(
        self,
        path=None,
        session_id=None,
        account_id=None,
        redis_client=None,
        redis_config=None,
        timeout_seconds=None,
    ):
        self.path = path
        self.session_id = session_id
        self.client = BigQmtRpcClient(
            account_id=account_id,
            redis_client=redis_client,
            redis_config=redis_config,
            timeout_seconds=timeout_seconds,
        )
        self.callback = None
        self._event_thread = None
        self._event_running = False
        # Async order submission (issue #50). One worker, started on first use,
        # so a client that never calls order_stock_async pays nothing.
        self._async_order_queue = _queue.Queue()
        self._async_order_thread = None
        self._async_order_lock = threading.Lock()
        # Outcomes (response/error + #51 barrier release) fire on this second
        # thread, so a slow user callback -- or the bounded wait for a late
        # order id -- never holds up the next submit (issue #181).
        self._async_callback_queue = _queue.Queue()
        self._async_callback_thread = None
        # Async cancel submission — same worker-queue pattern as async orders.
        # cancel_order_stock_async used to call the blocking RPC inline, which
        # made 15 cancels take ~30s.  Now the cancel runs on a worker thread
        # and batches a backlog into one RPC.
        self._async_cancel_queue = _queue.Queue()
        self._async_cancel_thread = None
        # Reuses _async_order_lock for start-up serialisation.
        # int -> 合同编号 for ids handed out as OrderId (issue #113).
        self._order_sys_ids = _OrderedDict()
        # on_account_status used to report a hardcoded "STOCK" even for a
        # credit deployment (issue #103). The server is authoritative -- the
        # client's StockAccount(..., "CREDIT") never travels -- so prefer what
        # ping reports, fall back to what the caller declared.
        self._server_account_type = ""
        self._declared_account_type = ""
        # Set once the exec-event listener is really subscribed; start() waits
        # on it instead of sleeping blind. Never cleared on reconnect rounds --
        # start() only waits once, and a resubscribe does not un-start it.
        self._event_ready = threading.Event()
        try:
            self.event_listener_ready_timeout = float(
                os.environ.get("BIGQMT_EVENT_READY_TIMEOUT") or 1.0)
        except (TypeError, ValueError):
            self.event_listener_ready_timeout = 1.0
        # Account-query cache fallback (#243). OFF by default: a failed
        # POSITION/ASSET query must reach the caller, the way it already does
        # on every non-redis transport. Serving the last redis snapshot
        # instead turns "the query failed" into "here is what you own",
        # which is the one answer a strategy must never be given wrongly.
        #
        # Deliberately NOT tied to local_cache_enabled: that key is the
        # client-side *market data* cache. Two different caches, two
        # switches -- conflating them is what made local_cache_enabled=False
        # look like it should have disabled this and it did not.
        # Read the config as handed to us: BigQmtRpcClient normalises its own
        # copy and drops keys it does not know, so self.client.redis_config
        # cannot be the source here.
        cache_config = dict((load_client_config() or {}).get("redis_config") or {})
        cache_config.update(dict(redis_config or {}))
        self.account_cache_fallback = _bool_value(
            cache_config.get("account_cache_fallback"),
            _env_bool("BIGQMT_ACCOUNT_CACHE_FALLBACK", False),
        )
        try:
            self.account_cache_max_age_seconds = float(
                cache_config.get("account_cache_max_age_seconds")
                or os.environ.get("BIGQMT_ACCOUNT_CACHE_MAX_AGE") or 30.0)
        except (TypeError, ValueError):
            self.account_cache_max_age_seconds = 30.0

    def _snapshot_age_seconds(self, snapshot):
        """How old the cached snapshot is, or None when it does not say.

        A snapshot that carries no ``updated_at`` is treated as unusable
        rather than fresh: the whole point of the bound is refusing to answer
        with facts we cannot date.
        """
        stamp = (snapshot or {}).get("updated_at")
        if not stamp:
            return None
        text = str(stamp).strip()
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
            try:
                parsed = _dt.datetime.strptime(text.split("+")[0].strip(), fmt)
            except ValueError:
                continue
            return max(0.0, (_dt.datetime.now() - parsed).total_seconds())
        try:  # epoch seconds
            return max(0.0, time.time() - float(text))
        except (TypeError, ValueError):
            return None

    def _account_cache_usable(self, account_id, what):
        """May we answer `what` from the cached snapshot at all?

        Returns the snapshot when the fallback is switched on AND the data is
        dated AND it is inside the age bound. Anything else -> None, and the
        caller re-raises the original error.
        """
        if not self.account_cache_fallback:
            return None
        if not self._redis_cache_enabled():
            return None
        snapshot = self._cached_position_snapshot(account_id)
        if not snapshot:
            return None
        age = self._snapshot_age_seconds(snapshot)
        if age is None:
            log.warning(
                "%s: 拒绝用缓存回答 —— 快照没有 updated_at，无法判断新旧", what)
            return None
        if age > self.account_cache_max_age_seconds:
            log.warning(
                "%s: 拒绝用缓存回答 —— 快照已 %.1fs 前（上限 %.1fs）",
                what, age, self.account_cache_max_age_seconds)
            return None
        log.warning(
            "%s: 原生查询失败，改用 %.1fs 前的 redis 缓存作答（"
            "account_cache_fallback=True 打开的行为）", what, age)
        return snapshot

    def _cached_position_snapshot(self, account_id):
        key = "bigqmt:positions:%s" % str(account_id or self.client.account_id or "")
        try:
            raw = self.client._redis().get(key)
        except Exception:
            return {}
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw))
        except Exception:
            return {}

    def _cached_positions(self, account_id):
        snapshot = self._cached_position_snapshot(account_id)
        positions = snapshot.get("positions") if isinstance(snapshot, dict) else None
        if isinstance(positions, dict):
            return positions
        if isinstance(positions, list):
            return {str(item.get("stock_code") or idx): item for idx, item in enumerate(positions)}
        return {}

    def _cached_asset(self, account_id):
        snapshot = self._cached_position_snapshot(account_id)
        asset = snapshot.get("asset") if isinstance(snapshot, dict) else None
        return asset if isinstance(asset, dict) else {}

    def _redis_cache_enabled(self):
        return str(getattr(self.client, "transport_name", "redis") or "redis").lower() in (
            "redis",
            "",
            "default",
        )

    def register_callback(self, callback):
        self.callback = callback
        return 0

    def start(self):
        # Launch the real-time execution-event listener so a registered callback's
        # on_stock_order / on_stock_trade fire as soon as Big QMT pushes them.
        self._start_event_listener()
        return 0

    def connect(self):
        if self.client.account_id:
            pong = self.client.call("ping")
            self._note_server_account_type(pong)
            mismatch = warn_on_version_mismatch(pong)
            if mismatch and auto_sync_enabled():
                self.sync_deployment()
        self._fire_account_status()
        return 0

    def sync_deployment(self, dry_run=False):
        """Push this client's package into the QMT python directory.

        The copy runs here, not inside QMT: a trading process rewriting its own
        code mid-session would put whatever is in the source tree, half-finished
        edits included, straight onto the live terminal.

        Config files are never written. The strategy still has to be restarted
        afterwards -- QMT keeps modules in sys.modules across re-runs, so a copy
        on its own changes nothing.
        """
        from .sync import sync_deployment as _sync

        # get_deployment_info lives on BigQmtXtData; this class never had it,
        # so xt_trader.sync_deployment() died with AttributeError. Call the
        # RPC directly -- the trader's client is the same one.
        info = self.client.call("get_deployment_info", {}) or {}
        target = info.get("qmt_python_dir") or ""
        if not target:
            log.warning("sync_deployment: the bridge did not report a "
                        "qmt_python_dir (server too old?); nothing copied")
            return {"updated": [], "error": "no qmt_python_dir reported"}

        result = _sync(target, dry_run=dry_run)
        if result.get("error"):
            log.warning("sync_deployment: %s", result["error"])
        elif result["updated"]:
            log.warning(
                "sync_deployment: %d file(s) %s in %s. RESTART THE STRATEGY -- "
                "QMT keeps modules across re-runs, so this has no effect until "
                "you do. Config files untouched: %s",
                len(result["updated"]),
                "would be updated" if dry_run else "updated",
                target, ", ".join(result["skipped_config"]) or "none present")
        else:
            log.info("sync_deployment: already up to date (%d files identical)",
                     result["identical"])
        return result

    def subscribe(self, account):
        declared = _account_type_name(getattr(account, "account_type", None))
        if declared:
            self._declared_account_type = declared
            self._warn_on_account_type_mismatch()
        if not self.client.account_id:
            self.client.account_id = _account_id(account)
        # (Re)start the listener now that the account is known; the loop resubscribes
        # to the account's channels within ~1s if the account changed.
        self._start_event_listener()
        self._fire_account_status()
        # Wait for the exec-event listener to actually be subscribed, rather
        # than sleeping a fixed second and hoping (#247 shipped time.sleep(1)
        # here with "原因不明"). The race is real: _start_event_listener starts
        # a daemon thread and returns, so events published between subscribe()
        # and the pubsub.subscribe() inside that thread are lost -- a caller
        # that orders immediately after subscribe() can miss its own fill
        # callback. start() has the same shape but MiniQMT callers reach events
        # through subscribe(), which is where the sleep was.
        #
        # Bounded by the same 1s the sleep cost, so the worst case is no worse
        # than before, while the normal case (subscribed in a few ms) no longer
        # pays for it. BIGQMT_EVENT_READY_TIMEOUT=0 disables the wait.
        self._await_event_listener()
        return 0

    def _await_event_listener(self):
        timeout = self.event_listener_ready_timeout
        if timeout <= 0:
            return False
        return self._event_ready.wait(timeout)

    def stop(self):
        # Drain first: orders already queued must go out before teardown.
        # Costs nothing when the queue is empty (issue #156).
        self._drain_async_orders_on_exit()
        self._event_running = False
        thread = self._event_thread
        if thread is not None and thread.is_alive():
            thread.join(1.0)
        self._event_thread = None
        return 0

    def _start_event_listener(self):
        if self._event_thread is not None and self._event_thread.is_alive():
            return
        self._event_running = True
        self._event_thread = threading.Thread(
            target=self._event_loop, name="bigqmt-exec-events", daemon=True
        )
        self._event_thread.start()

    def _note_server_account_type(self, pong):
        """Remember what the deployment says it trades as."""
        try:
            reported = str((pong or {}).get("account_type") or "").strip().upper()
        except Exception:
            return
        if reported:
            self._server_account_type = reported
            self._warn_on_account_type_mismatch()

    def _warn_on_account_type_mismatch(self):
        """Say so when the caller and the deployment disagree.

        A client asking for CREDIT against a STOCK deployment gets an all-zero
        asset row and no error at all -- that was issue #92, and it cost the
        reporter a long time because nothing anywhere said the two disagreed.
        """
        server = self._server_account_type
        declared = self._declared_account_type
        if not server or not declared or server == declared:
            return
        log.warning(
            "account_type mismatch: this client asked for %s but the QMT "
            "deployment is configured as %s. The client's StockAccount type "
            "does NOT travel to the server -- set BIGQMT_ACCOUNT_TYPE = %r in "
            "the QMT-side local config and restart the strategy. Until then "
            "queries answer as %s (a credit account read as STOCK returns an "
            "all-zero asset row).", declared, server, declared, server)

    def _fire_account_status(self):
        """Fire on_account_status after connect/subscribe (MiniQMT parity).

        Big QMT has no per-strategy account-status push; we synthesize a
        CONNECTED status once the RPC link is up so client code that waits
        for on_account_status before trading keeps working.
        """
        callback = self.callback
        if callback is None:
            return
        try:
            callback.on_account_status(
                CompatObject(
                    account_id=str(self.client.account_id or ""),
                    account_type=(self._server_account_type
                                  or self._declared_account_type or "STOCK"),
                    status=1,  # ACCOUNT_STATUS_ONLINE (MiniQMT XtAccountStatus)
                )
            )
        except Exception:
            log.exception("user callback failed: on_account_status")

    def _event_loop_push_channel(self):
        """One push-channel round: zmq exec events arrive on the same PUB
        socket as whole-quote data.

        Reuses _build_quote_push_channel so the address derivation stays in one
        place. Single round: returns when the account changes or the channel
        dies, so the caller's per-round channel selection runs again (Redis may
        have come back, or gone away).
        """
        from .exec_events import EXEC_TOPICS

        topics = sorted(set(EXEC_TOPICS.values()))
        channel = None
        account_id = str(self.client.account_id or "")
        try:
            channel = self._build_quote_push_channel()
            channel.start_subscriber(topics, self._on_push_exec_event)
            self._event_ready.set()          # see the redis path (#247)
            while self._event_running:
                if str(self.client.account_id or "") != account_id:
                    return       # account changed -> rebuild against the new address
                time.sleep(0.5)
        except Exception:
            time.sleep(1.0)
        finally:
            if channel is not None:
                try:
                    channel.stop()
                except Exception:
                    pass

    def _on_push_exec_event(self, topic, data):
        """Push-channel callback. The payload is already a decoded dict, unlike
        the Redis path which hands over raw bytes."""
        try:
            self._dispatch_event(data)
        except Exception:
            pass

    def _event_loop(self):
        """Receive exec events, mirroring the server's sink choice.

        The server publishes to Redis FIRST whenever it can build a Redis
        client (its channels carry streams for short replay), even when the
        RPC transport is zmq, and only falls to the quote push channel after
        repeated Redis publish failures (strategy _exec_event_sink, issue
        #145).  This loop used to choose by transport instead -- zmq -> push
        channel only -- so a zmq deployment with a working Redis published
        every order/trade event to Redis while the client listened on the
        push channel: callbacks never fired (issue #144; reproduced
        2026-09-02, the day's events sat in the Redis stream while a
        zmq-transport listener saw nothing).

        Re-select per reconnect round, so the client follows a server that
        demotes Redis mid-session (its Redis publish failing usually means
        our Redis reads fail too).
        """
        while self._event_running:
            redis_client = self._exec_events_redis_or_none()
            if redis_client is not None:
                self._event_loop_redis(redis_client)
            elif self._exec_transport_is_zmq():
                self._event_loop_push_channel()
            else:
                # redis transport with redis down: nothing else carries
                # events; keep retrying as before.
                time.sleep(1.0)

    def _exec_transport_is_zmq(self):
        return str(getattr(self.client, "transport_name", "redis") or "redis").lower() == "zmq"

    def _exec_events_redis_or_none(self):
        """A REACHABLE Redis client for the exec-event channels, or None.

        _redis() only builds the client object; the connection is lazy, so
        an unreachable server would still return one. Ping it -- the channel
        choice must reflect reachability, not configuration.
        """
        try:
            client = self.client._redis()
            if client is None:
                return None
            client.ping()
            return client
        except Exception:
            return None

    def _event_loop_redis(self, redis_client):
        """One Redis round: subscribe the per-account channels until the
        account changes or the connection dies, then return for re-selection."""
        from .exec_events import (
            order_channel,
            trade_channel,
            order_error_channel,
            cancel_error_channel,
        )

        account_id = str(self.client.account_id or "")
        pubsub = None
        try:
            pubsub = redis_client.pubsub(ignore_subscribe_messages=True)
            pubsub.subscribe(
                order_channel(account_id),
                trade_channel(account_id),
                order_error_channel(account_id),
                cancel_error_channel(account_id),
            )
            # Subscribed for real -- release start()'s bounded wait (#247).
            self._event_ready.set()
            while self._event_running:
                if str(self.client.account_id or "") != account_id:
                    return  # account changed -> reconnect and resubscribe
                message = pubsub.get_message(timeout=1.0)
                if not message or message.get("type") != "message":
                    continue
                self._dispatch_event(message.get("data"))
        except Exception:
            time.sleep(1.0)
        finally:
            try:
                if pubsub is not None:
                    pubsub.close()
            except Exception:
                pass

    def _dispatch_event(self, raw):
        """Accepts raw bytes/str (Redis pub/sub) or an already-decoded dict.

        The push channel decodes msgpack/json itself, so it hands over a dict --
        str(dict) is not valid JSON and would be silently dropped here.
        """
        callback = self.callback
        if callback is None:
            return
        if isinstance(raw, dict):
            event = raw
        else:
            try:
                text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
                event = json.loads(text)
            except Exception:
                return
        if not isinstance(event, dict):
            return
        # 放行超时的屏障, 再决定这条事件是直通还是暂存 (issue #51)。
        try:
            self._sweep_order_barriers()
            if event.get("event_type") in ("order", "trade", "order_error", "cancel_error") and self._hold_if_pending(event):
                return
        except Exception:
            pass  # 屏障故障绝不能吞掉事件
        self._deliver_event(event)

    def _deliver_event(self, event):
        callback = self.callback
        if callback is None:
            return
        account_id = str(event.get("account_id") or self.client.account_id or "")
        try:
            event_type = event.get("event_type")
            if event_type == "trade":
                callback.on_stock_trade(self._trade_from_dict(account_id, event))
            elif event_type == "order":
                callback.on_stock_order(self._order_from_dict(account_id, event))
            elif event_type == "order_error":
                _sysid = str(event.get("order_sys_id") or "")
                callback.on_order_error(
                    CompatObject(
                        error_id=event.get("error_id"),
                        error_msg=event.get("error_msg") or "",
                        order_sysid=_sysid,       # MiniQMT 规范名 (issue #65)
                        order_sys_id=_sysid,      # 兼容别名
                        order_id=_sysid,
                        stock_code=event.get("stock_code") or "",
                        order_remark=str(
                            event.get("order_remark") or event.get("remark")
                            or event.get("user_order_id") or ""
                        ),
                        strategy_name=str(event.get("strategy_name") or ""),
                        status=_safe_int(event.get("status", event.get("order_status")), 0),
                    )
                )
            elif event_type == "cancel_error":
                _sysid = str(event.get("order_sys_id") or "")
                callback.on_cancel_error(
                    CompatObject(
                        error_id=event.get("error_id"),
                        error_msg=event.get("error_msg") or "",
                        order_sysid=_sysid,       # MiniQMT 规范名 (issue #65)
                        order_sys_id=_sysid,
                        order_id=_sysid,
                        stock_code=event.get("stock_code") or "",
                        order_remark=str(
                            event.get("order_remark") or event.get("remark")
                            or event.get("user_order_id") or ""
                        ),
                    )
                )
        except Exception:
            # 业务回调异常不能打崩事件线程，但必须留痕（issue: 静默吞错）。
            log.exception(
                "user callback failed: event_type=%s account=%s",
                event.get("event_type"),
                account_id,
            )

    def run_forever(self):
        while True:
            time.sleep(3600)

    def query_stock_asset(self, account):
        account_id = _account_id(account, self.client.account_id)
        try:
            data = self.client.call("query_stock_asset", {"account_id": account_id}, account_id=account_id) or {}
        except Exception:
            # #243: default is to let the failure through. Only an explicit
            # account_cache_fallback, with a dated and fresh snapshot, answers
            # from cache -- and it says so in the log when it does.
            if self._account_cache_usable(account_id, "query_stock_asset") is None:
                raise
            data = self._cached_asset(account_id)
            if not data:
                raise
        if (
            data.get("cash") is None
            and data.get("total_asset") is None
            and self._account_cache_usable(account_id, "query_stock_asset(empty)") is not None
        ):
            data = self._cached_asset(account_id) or data
        cash = data.get("cash")
        total_asset = data.get("total_asset")
        frozen_cash = data.get("frozen_cash")
        market_value = data.get("market_value")
        if market_value is None and cash is not None and total_asset is not None:
            # total_asset = cash(available) + frozen_cash + market_value. Older
            # servers send neither frozen_cash nor market_value; deriving without
            # frozen_cash overstates market value by the frozen amount, so
            # subtract it whenever the server did report it.
            market_value = _safe_float(total_asset) - _safe_float(cash)
            if frozen_cash is not None:
                market_value -= _safe_float(frozen_cash)
        return CompatObject(
            account_id=account_id,
            cash=_safe_float(cash, 0.0) if cash is not None else None,
            available_cash=_safe_float(cash, 0.0) if cash is not None else None,
            # MiniQMT's XtAsset always exposes frozen_cash, so default to 0.0
            # rather than None: callers do arithmetic on it.
            frozen_cash=_safe_float(frozen_cash, 0.0) if frozen_cash is not None else 0.0,
            total_asset=_safe_float(total_asset, 0.0) if total_asset is not None else None,
            market_value=_safe_float(market_value, 0.0) if market_value is not None else 0.0,
            # ===== 原生 xtquant 字段名别名（兼容 m_ 前缀访问）=====
            m_strAccountID=account_id,
            m_dCash=_safe_float(cash, 0.0) if cash is not None else None,
            m_dAvailableCash=_safe_float(cash, 0.0) if cash is not None else None,
            m_dFrozenCash=_safe_float(frozen_cash, 0.0) if frozen_cash is not None else 0.0,
            m_dTotalAsset=_safe_float(total_asset, 0.0) if total_asset is not None else None,
            m_dMarketValue=_safe_float(market_value, 0.0) if market_value is not None else 0.0,
        )

    def _position_object(self, account_id, item):
        volume = _safe_int(item.get("volume"))
        available = _safe_int(item.get("available", item.get("can_use_volume")))
        cost = _safe_float(item.get("cost", item.get("avg_price")))
        price = _safe_float(item.get("price", item.get("last_price")), cost)
        stock_code = str(item.get("stock_code") or "")
        stock_name = str(item.get("stock_name") or "")
        market_value = item.get("market_value")
        if market_value is None:
            market_value = price * volume
        return CompatObject(
            # 以前硬编码 2（SECURITY_ACCOUNT），信用账户上就是错的 —— 和 #103
            # 报的 on_account_status 同一类。现在跟服务端说的走。
            account_type=self._account_type_value(item),
            account_id=account_id,
            stock_code=stock_code,
            stock_name=stock_name,
            volume=volume,
            can_use_volume=available,
            enable_amount=available,
            available_amount=available,
            avg_price=cost,
            price=price,
            open_price=_safe_float(item.get("open_price"), cost),
            cost_price=cost,
            market_value=_safe_float(market_value, 0.0),
            frozen_volume=_safe_int(item.get("frozen_volume")),
            on_road_volume=_safe_int(item.get("on_road_volume")),
            yesterday_volume=_safe_int(item.get("yesterday_volume"), volume),
            direction=_safe_int(item.get("direction"), 48),
            # ===== 原生 xtquant 字段名别名（兼容 m_ 前缀访问）=====
            m_strAccountID=account_id,
            m_strStockCode=stock_code,
            m_strStockName=stock_name,
            m_nVolume=volume,
            m_nCanUseVolume=available,
            m_nCanUseVol=available,
            m_nEnableAmount=available,
            m_dOpenPrice=_safe_float(item.get("open_price"), cost),
            m_dAvgPrice=cost,
            m_dLastPrice=price,
            m_dMarketValue=_safe_float(market_value, 0.0),
            m_nFrozenVolume=_safe_int(item.get("frozen_volume")),
            m_nOnRoadVolume=_safe_int(item.get("on_road_volume")),
            m_nYesterdayVolume=_safe_int(item.get("yesterday_volume"), volume),
            m_nDirection=_safe_int(item.get("direction"), 48),
        )

    @staticmethod
    def _position_items(data):
        if isinstance(data, dict):
            return list(data.values())
        return _as_list(data)

    def query_stock_positions(self, account):
        account_id = _account_id(account, self.client.account_id)
        try:
            data = self.client.call("query_stock_positions", {"account_id": account_id}, account_id=account_id) or {}
        except Exception:
            if self._account_cache_usable(account_id, "query_stock_positions") is None:
                raise
            data = self._cached_positions(account_id)
            if not data:
                raise
        return [self._position_object(account_id, item) for item in self._position_items(data)]

    def query_position_statistics(self, account):
        """Intraday position statistics (futures), mirroring MiniQMT ``query_position_statistics``.

        Returns a list of :class:`XtPositionStatistics`-style :class:`CompatObject`.
        The server queries via ``get_trade_detail_data(..., "POSITION_STATISTICS")``.
        """
        account_id = _account_id(account, self.client.account_id)
        data = self.client.call(
            "query_position_statistics",
            {"account_id": account_id},
            account_id=account_id,
        ) or {}
        return [self._position_statistics_object(account_id, item) for item in _as_list(data)]

    def _position_statistics_object(self, account_id, item):
        return CompatObject(
            account_id=account_id,
            exchange_id=str(item.get("exchange_id") or ""),
            exchange_name=str(item.get("exchange_name") or ""),
            product_id=str(item.get("product_id") or ""),
            instrument_id=str(item.get("instrument_id") or ""),
            instrument_name=str(item.get("instrument_name") or ""),
            stock_code=str(item.get("stock_code") or ""),
            direction=_safe_int(item.get("direction"), 0),
            hedge_flag=_safe_int(item.get("hedge_flag"), 0),
            position=_safe_int(item.get("position"), 0),
            yesterday_position=_safe_int(item.get("yesterday_position"), 0),
            today_position=_safe_int(item.get("today_position"), 0),
            can_close_vol=_safe_int(item.get("can_close_vol"), 0),
            position_cost=_safe_float(item.get("position_cost"), None),
            avg_price=_safe_float(item.get("avg_price"), None),
            position_profit=_safe_float(item.get("position_profit"), None),
            float_profit=_safe_float(item.get("float_profit"), None),
            open_price=_safe_float(item.get("open_price"), None),
            used_margin=_safe_float(item.get("used_margin"), None),
            used_commission=_safe_float(item.get("used_commission"), None),
            frozen_margin=_safe_float(item.get("frozen_margin"), None),
            frozen_commission=_safe_float(item.get("frozen_commission"), None),
            instrument_value=_safe_float(item.get("instrument_value"), None),
            open_times=_safe_int(item.get("open_times"), 0),
            open_volume=_safe_int(item.get("open_volume"), 0),
            cancel_times=_safe_int(item.get("cancel_times"), 0),
            last_price=_safe_float(item.get("last_price"), None),
            rise_ratio=_safe_float(item.get("rise_ratio"), None),
            product_name=str(item.get("product_name") or ""),
            royalty=_safe_float(item.get("royalty"), None),
            expire_date=str(item.get("expire_date") or ""),
            assest_weight=_safe_float(item.get("assest_weight"), None),
            increase_by_settlement=_safe_float(item.get("increase_by_settlement"), None),
            margin_ratio=_safe_float(item.get("margin_ratio"), None),
            float_profit_divide_by_used_margin=_safe_float(
                item.get("float_profit_divide_by_used_margin"), None
            ),
            float_profit_divide_by_balance=_safe_float(
                item.get("float_profit_divide_by_balance"), None
            ),
            today_profit_loss=_safe_float(item.get("today_profit_loss"), None),
            yesterday_init_position=_safe_int(item.get("yesterday_init_position"), 0),
            frozen_royalty=_safe_float(item.get("frozen_royalty"), None),
            today_close_profit_loss=_safe_float(item.get("today_close_profit_loss"), None),
            close_profit=_safe_float(item.get("close_profit"), None),
            ft_product_name=str(item.get("ft_product_name") or ""),
            open_cost=_safe_float(item.get("open_cost"), None),
            # ===== native xtquant field-name aliases (m_-prefixed access) =====
            m_strAccountID=account_id,
            m_strStockCode=str(item.get("stock_code") or ""),
            m_strExchangeID=str(item.get("exchange_id") or ""),
            m_strExchangeName=str(item.get("exchange_name") or ""),
            m_strProductID=str(item.get("product_id") or ""),
            m_strInstrumentID=str(item.get("instrument_id") or ""),
            m_strInstrumentName=str(item.get("instrument_name") or ""),
            m_nDirection=_safe_int(item.get("direction"), 0),
            m_nHedgeFlag=_safe_int(item.get("hedge_flag"), 0),
            m_nPosition=_safe_int(item.get("position"), 0),
            m_nYestodayPosition=_safe_int(item.get("yesterday_position"), 0),
            m_nTodayPosition=_safe_int(item.get("today_position"), 0),
            m_nCanCloseVol=_safe_int(item.get("can_close_vol"), 0),
            m_dPositionCost=_safe_float(item.get("position_cost"), None),
            m_dAvgPrice=_safe_float(item.get("avg_price"), None),
            m_dPositionProfit=_safe_float(item.get("position_profit"), None),
            m_dFloatProfit=_safe_float(item.get("float_profit"), None),
            m_dOpenPrice=_safe_float(item.get("open_price"), None),
            m_dUsedMargin=_safe_float(item.get("used_margin"), None),
            m_dUsedCommission=_safe_float(item.get("used_commission"), None),
            m_dFrozenMargin=_safe_float(item.get("frozen_margin"), None),
            m_dFrozenCommission=_safe_float(item.get("frozen_commission"), None),
            m_dInstrumentValue=_safe_float(item.get("instrument_value"), None),
            m_nOpenTimes=_safe_int(item.get("open_times"), 0),
            m_nOpenVolume=_safe_int(item.get("open_volume"), 0),
            m_nCancelTimes=_safe_int(item.get("cancel_times"), 0),
            m_dLastPrice=_safe_float(item.get("last_price"), None),
            m_dRiseRatio=_safe_float(item.get("rise_ratio"), None),
            m_strProductName=str(item.get("product_name") or ""),
            m_dRoyalty=_safe_float(item.get("royalty"), None),
            m_strExpireDate=str(item.get("expire_date") or ""),
            m_dAssestWeight=_safe_float(item.get("assest_weight"), None),
            m_dIncreaseBySettlement=_safe_float(item.get("increase_by_settlement"), None),
            m_dMarginRatio=_safe_float(item.get("margin_ratio"), None),
            m_dFloatProfitDivideByUsedMargin=_safe_float(
                item.get("float_profit_divide_by_used_margin"), None
            ),
            m_dFloatProfitDivideByBalance=_safe_float(
                item.get("float_profit_divide_by_balance"), None
            ),
            m_dTodayProfitLoss=_safe_float(item.get("today_profit_loss"), None),
            m_nYestodayInitPosition=_safe_int(item.get("yesterday_init_position"), 0),
            m_dFrozenRoyalty=_safe_float(item.get("frozen_royalty"), None),
            m_dTodayCloseProfitLoss=_safe_float(item.get("today_close_profit_loss"), None),
            m_dCloseProfit=_safe_float(item.get("close_profit"), None),
            m_strFtProductName=str(item.get("ft_product_name") or ""),
            m_dOpenCost=_safe_float(item.get("open_cost"), None),
        )

    def query_stock_position(self, account, stock_code):
        account_id = _account_id(account, self.client.account_id)
        try:
            data = self.client.call(
                "query_stock_position",
                {"account_id": account_id, "stock_code": stock_code},
                account_id=account_id,
            )
        except Exception:
            if self._account_cache_usable(account_id, "query_stock_position") is None:
                raise
            normalized = str(stock_code or "").strip().upper()
            data = None
            for code, item in self._cached_positions(account_id).items():
                if str(code).upper() == normalized or str(code).split(".", 1)[0].upper() == normalized:
                    data = item
                    break
            if data is None:
                raise
        if not data:
            return None
        return [
            self._position_object(account_id, item)
            for item in [data]
        ][0]

    def query_stock_orders(self, account, cancelable_only=False, strategy_name=""):
        # strategy_name 默认 ""（返回全部）：与服务端一致，避免下单用的策略名
        # 与查询默认值不匹配导致委托查不到（strategy_name 陷阱）。
        account_id = _account_id(account, self.client.account_id)
        data = self.client.call(
            "query_stock_orders",
            {
                "account_id": account_id,
                "cancelable_only": bool(cancelable_only),
                "strategy_name": strategy_name,
            },
            account_id=account_id,
        ) or []
        return [self._order_from_dict(account_id, item) for item in _as_list(data)]

    def query_stock_order(self, account, order_id):
        order_id = str(order_id or "")
        for order in self.query_stock_orders(account, cancelable_only=False):
            if str(order.order_id) == order_id or str(order.order_sysid) == order_id:
                return order
        return None

    def query_stock_trades(self, account, strategy_name=""):
        # 默认 "" = 查询账户全部成交 (与服务端 _handle_query_trades 一致)。
        # 旧默认 "bigqmt_signal_trader" 会过滤掉其他策略名的成交;
        # 按策略过滤时由调用方显式传入。
        account_id = _account_id(account, self.client.account_id)
        data = self.client.call(
            "query_stock_trades",
            {"account_id": account_id, "strategy_name": strategy_name},
            account_id=account_id,
        ) or []
        return [self._trade_from_dict(account_id, item) for item in _as_list(data)]

    def describe_trade_detail_fields(self, account, detail_types=None):
        """Which attributes QMT's own ORDER / DEAL rows carry. Names only.

        A debugging aid, not part of MiniQMT: when a field comes back empty,
        this says whether the terminal is not providing it or the bridge is
        not forwarding it. Those two look identical from the client and have
        cost a deploy-and-restart each time (#113, #130, #133).

            xt_trader.describe_trade_detail_fields(account)
            -> {'ORDER': {'rows': 15, 'attributes': [...], 'error': ''}, ...}
        """
        account_id = _account_id(account, self.client.account_id)
        params = {"account_id": account_id}
        if detail_types:
            params["detail_types"] = list(detail_types)
        return self.client.call("describe_trade_detail_fields", params,
                                account_id=account_id) or {}

    def reload_deployment(self, reason="", account=None):
        """Re-import the deployed package and re-run init, without a restart.

        Returns as soon as the reload is SCHEDULED -- it runs on the next
        adjust tick, because performing it stops the RPC service answering the
        request. Poll reload_status() (or get_deployment_info()) for the
        outcome.

        Refreshes everything under bigqmt_signal_trader/. It cannot refresh
        bigqmt_signal_trader_strategy.py or the BIGQMT_REDIS_DRYRUN entry --
        QMT execs those, and a module cannot reload the one it is running in.
        Changes there still need a strategy restart.
        """
        account_id = _account_id(account, self.client.account_id)
        return self.client.call("reload_deployment", {"reason": str(reason or "")},
                                account_id=account_id) or {}

    def reload_status(self, account=None):
        """Outcome of the last reload_deployment, or what is still pending."""
        account_id = _account_id(account, self.client.account_id)
        return self.client.call("reload_status", {},
                                account_id=account_id) or {}

    def query_execution_snapshot(
        self,
        account,
        order_strategy_name="bigqmt_signal_trader",
        trade_strategy_name="",
    ):
        """Query orders and account-wide trades in one RPC round trip."""
        account_id = _account_id(account, self.client.account_id)
        data = self.client.call(
            "query_execution_snapshot",
            {
                "account_id": account_id,
                "order_strategy_name": order_strategy_name,
                "trade_strategy_name": trade_strategy_name,
            },
            account_id=account_id,
        ) or {}
        result = dict(data) if isinstance(data, dict) else {}
        result["orders"] = [
            self._order_from_dict(account_id, item)
            for item in _as_list(result.get("orders"))
        ]
        result["trades"] = [
            self._trade_from_dict(account_id, item)
            for item in _as_list(result.get("trades"))
        ]
        return result

    def order_stock(
        self,
        account,
        stock_code,
        order_type,
        order_volume,
        price_type,
        price,
        strategy_name,
        order_remark,
    ):
        data = self.order_stock_result(
            account, stock_code, order_type, order_volume, price_type,
            price, strategy_name, order_remark,
        )
        return self._order_id(data.get("order_sys_id"))

    def _order_id(self, order_sys_id):
        """MiniQMT's return contract: a positive int, or -1 on failure.

        Big QMT only has the broker's 合同编号 string, so an OrderId carries
        both (issue #113). "-1" arrives as a *string* from the server when the
        submit itself failed, and used to be returned as one -- truthy, and
        never equal to -1, so a rejected order read as success.
        """
        text = str(order_sys_id or "").strip()
        if not text or text == "-1":
            return -1
        order_id = OrderId(text)
        self._remember_order_id(order_id)
        return order_id

    def _order_object_id(self, order_sys_id):
        """``order_id`` for an XtOrder / XtTrade: int, empty stays empty.

        Unlike the order_stock return there is no -1 here -- a query result
        either has an id or does not.
        """
        text = str(order_sys_id or "").strip()
        if not text:
            return OrderId("")
        order_id = OrderId(text)
        self._remember_order_id(order_id)
        return order_id

    def _remember_order_id(self, order_id):
        """Keep int -> 合同编号 so a cancel still works after a round trip.

        A caller who stores the id in JSON or a database gets a plain int back,
        losing the string half. Bounded: this is a convenience, not a ledger.
        """
        sys_id = getattr(order_id, "order_sys_id", "")
        if not sys_id or str(int(order_id)) == sys_id:
            return                       # nothing to remember: they agree
        table = self._order_sys_ids
        table[int(order_id)] = sys_id
        while len(table) > _ORDER_ID_MEMORY:
            table.popitem(last=False)

    def _resolve_order_sys_id(self, value):
        """The broker string for whatever a caller passed to a cancel."""
        carried = getattr(value, "order_sys_id", None)
        if carried:
            return str(carried)
        if isinstance(value, int) and not isinstance(value, bool):
            remembered = self._order_sys_ids.get(int(value))
            if remembered:
                return remembered
        return order_sys_id_of(value)

    def order_stock_result(
        self, account, stock_code, order_type, order_volume, price_type,
        price, strategy_name, order_remark, wait_settlement=True,
    ):
        """Submit one order over RPC.

        ``wait_settlement=False`` tells the server to reply as soon as passorder
        returns instead of holding the reply until QMT assigns the order id.
        The async path uses it; the id then arrives through order_callback
        (issue #50).
        """
        account_id = _account_id(account, self.client.account_id)
        user_order_id = str(order_remark or "").strip()
        if not user_order_id:
            user_order_id = "bqrpc:%s:%s" % (int(time.time() * 1000), uuid.uuid4().hex[:10])
        payload = {
            "account_id": account_id,
            "stock_code": stock_code,
            "order_type": order_type,
            "order_volume": order_volume,
            "price_type": price_type,
            "price": price,
            "strategy_name": strategy_name,
            "order_remark": user_order_id,
        }
        if not wait_settlement:
            payload["wait_settlement"] = False
        try:
            return self.client.call("order_stock", payload, account_id=account_id) or {}
        except TimeoutError as exc:
            raise TimeoutError(
                "order_stock rpc timeout; user_order_id=%s. Query orders/trades before retrying to avoid duplicate orders. %s"
                % (user_order_id, exc)
            )

    def _async_order_worker(self):
        """Drain queued async orders; never fires user callbacks itself.

        A single worker rather than a pool: the server handles order RPCs on
        the QMT adjust thread serially anyway, so concurrency here buys little,
        while serializing keeps outcome units enqueued in submission order.
        Callbacks run on the callback worker, so a slow user callback -- or the
        bounded wait for a late order id -- never holds up the next submit
        (issue #181). For one order at a time that saves the callback's cost;
        for a backlog the batch path below saves the round trips too.
        """
        while True:
            job = self._async_order_queue.get()
            if job is None:          # shutdown sentinel
                self._async_order_queue.task_done()
                return
            jobs = [job]
            stopping = False
            # Take whatever else is already waiting. One RPC for N orders
            # instead of N: a backlog serializes on the round trip otherwise
            # (issue #181). Nothing is waited for -- a queue with one job in
            # it still goes down the single path.
            while len(jobs) < self.ASYNC_BATCH_MAX:
                try:
                    extra = self._async_order_queue.get_nowait()
                except _queue.Empty:
                    break
                if extra is None:
                    stopping = True      # honour it after this batch, not instead
                    self._async_order_queue.task_done()
                    break
                jobs.append(extra)
            try:
                self._submit_async_jobs(jobs)
            except Exception:
                # A worker that dies takes every later async order with it.
                log.exception("async order submit failed for %d job(s)", len(jobs))
            finally:
                for _ in jobs:
                    self._async_order_queue.task_done()
            if stopping:
                return

    def _async_callback_worker(self):
        """Fire outcome units in enqueue (= submission) order, serially.

        This thread, not the submit worker, is where user callbacks run. The
        #51 barrier release rides along inside the same unit, so the ordering
        contract -- a委托's async_response before its order/trade events --
        holds exactly as when everything ran on one thread.
        """
        while True:
            unit = self._async_callback_queue.get()
            try:
                if unit is None:      # shutdown sentinel
                    return
                self._fire_async_outcome(unit)
            except Exception:
                # The dispatcher dying would strand every later callback AND
                # every armed barrier, so it must not.
                log.exception("async callback dispatch failed seq=%s",
                              unit.get("seq") if isinstance(unit, dict) else "?")
            finally:
                self._async_callback_queue.task_done()

    def _ensure_async_order_worker(self):
        with self._async_order_lock:
            if self._async_callback_thread is None or not self._async_callback_thread.is_alive():
                self._async_callback_thread = threading.Thread(
                    target=self._async_callback_worker,
                    name="bigqmt-async-callback", daemon=True,
                )
                self._async_callback_thread.start()
            if self._async_order_thread is not None and self._async_order_thread.is_alive():
                return
            thread = threading.Thread(
                target=self._async_order_worker, name="bigqmt-async-order", daemon=True
            )
            self._async_order_thread = thread
            thread.start()

    def _register_exit_drain(self):
        """atexit hook, registered once: a fire-and-forget script that exits
        right after queueing must not drop the queued orders silently
        (issue #156)."""
        with self._async_order_lock:
            if getattr(self, "_exit_drain_registered", False):
                return
            self._exit_drain_registered = True
        import atexit
        atexit.register(self._drain_async_orders_on_exit)

    def _drain_async_orders_on_exit(self):
        """Best-effort flush of queued async orders at stop()/process exit.

        The async worker is a daemon thread: a script that exits right after
        queueing kills it mid-queue, and every order past the first is lost
        without a word (issue #156: "循环下单只有第一条成功，加 sleep 才正常"
        -- the sleep was keeping the process alive; reproduced live as 1/3 vs
        3/3 orders reaching QMT). Drain the queue, then give armed barriers a
        bounded moment so in-flight responses can fire their callbacks.
        Never raises: this runs at interpreter exit and inside stop().
        """
        try:
            self.wait_async_orders(timeout=self.ASYNC_EXIT_DRAIN_SECONDS)
        except Exception:
            pass
        barrier_lock = getattr(self, "_async_barrier_lock", None)
        if barrier_lock is None:
            return
        deadline = time.time() + self.ASYNC_EXIT_CALLBACK_GRACE_SECONDS
        try:
            while time.time() < deadline:
                with barrier_lock:
                    if not getattr(self, "_async_barrier", None):
                        return
                time.sleep(0.05)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Async cancel — worker-queue pattern mirroring async orders.
    # cancel_order_stock_async used to block on the RPC for each cancel;
    # now it enqueues and returns immediately.  The worker drains the
    # queue and batches a backlog into one cancel_orders_batch RPC.
    # ------------------------------------------------------------------

    def _async_cancel_worker(self):
        """Drain queued async cancels; batches when backlog allows."""
        while True:
            job = self._async_cancel_queue.get()
            if job is None:
                self._async_cancel_queue.task_done()
                return
            jobs = [job]
            stopping = False
            while len(jobs) < self.ASYNC_CANCEL_BATCH_MAX:
                try:
                    extra = self._async_cancel_queue.get_nowait()
                except _queue.Empty:
                    break
                if extra is None:
                    stopping = True
                    self._async_cancel_queue.task_done()
                    break
                jobs.append(extra)
            try:
                self._submit_async_cancel_jobs(jobs)
            except Exception:
                log.exception("async cancel submit failed for %d job(s)", len(jobs))
            finally:
                for _ in jobs:
                    self._async_cancel_queue.task_done()
            if stopping:
                return

    def _ensure_async_cancel_worker(self):
        with self._async_order_lock:
            if self._async_callback_thread is None or not self._async_callback_thread.is_alive():
                self._async_callback_thread = threading.Thread(
                    target=self._async_callback_worker,
                    name="bigqmt-async-callback", daemon=True,
                )
                self._async_callback_thread.start()
            if self._async_cancel_thread is not None and self._async_cancel_thread.is_alive():
                return
            thread = threading.Thread(
                target=self._async_cancel_worker,
                name="bigqmt-async-cancel", daemon=True,
            )
            self._async_cancel_thread = thread
            thread.start()

    def _submit_async_cancel_jobs(self, jobs):
        """Submit cancels one-at-a-time or batched by account."""
        if len(jobs) < self.ASYNC_CANCEL_BATCH_MIN:
            self._submit_async_cancel_single(jobs[0])
            return
        groups = {}
        for job in jobs:
            seq, args, kwargs = job
            account_id = _account_id(args[0], self.client.account_id)
            groups.setdefault(account_id, []).append(job)
        for account_id, group in groups.items():
            if len(group) < self.ASYNC_CANCEL_BATCH_MIN:
                for job in group:
                    self._submit_async_cancel_single(job)
            else:
                self._submit_async_cancel_batch(account_id, group)

    def _submit_async_cancel_single(self, job):
        """One cancel on the worker thread; enqueue its callback."""
        seq, args, kwargs = job
        account = args[0]
        order_id = args[1] if len(args) > 1 else kwargs.get("order_id", "")
        market = args[2] if len(args) > 2 else kwargs.get("market", "")
        account_id = _account_id(account, self.client.account_id)
        try:
            data = self.client.call(
                "cancel_order_stock_sysid",
                {
                    "account_id": account_id,
                    "market": market,
                    "order_sysid": self._resolve_order_sys_id(order_id),
                },
                account_id=account_id,
            ) or {}
            ok = bool(data.get("success", data))
        except Exception as exc:
            self._enqueue_async_outcome({
                "kind": "cancel_error", "seq": seq,
                "order_id": order_id, "market": market,
                "error_id": getattr(exc, "errno", 0),
                "error_msg": str(exc),
            })
            return
        self._enqueue_async_outcome({
            "kind": "cancel_response", "seq": seq,
            "order_id": order_id, "market": market,
            "success": ok,
        })

    def _submit_async_cancel_batch(self, account_id, group):
        """N cancels in one RPC; one callback per item.

        Failure taxonomy mirrors the order batch (#195): a batch the server
        REFUSED (answered with an error -- the handler raises before its
        per-item loop, so nothing ran) or answered empty is safe to retry as
        singles; a timeout/transport failure means the cancels may be running
        and retrying would double-cancel -- report unknown-outcome per item
        instead."""
        payload = []
        for seq, args, kwargs in group:
            order_id = args[1] if len(args) > 1 else kwargs.get("order_id", "")
            market = args[2] if len(args) > 2 else kwargs.get("market", "")
            payload.append({
                "account_id": account_id,
                "order_sysid": self._resolve_order_sys_id(order_id),
                "market": market,
            })
        try:
            results = self.client.call(
                "cancel_order_stock_batch",
                {"account_id": account_id, "items": payload},
                account_id=account_id,
            ) or []
        except RpcServerRepliedError as exc:
            log.warning("cancel batch of %d refused by the server (%s); "
                        "submitting one at a time", len(group), exc)
            for job in group:
                try:
                    self._submit_async_cancel_single(job)
                except Exception:
                    log.exception("async cancel failed after batch refusal")
            return
        except Exception as exc:
            log.exception("cancel batch of %d outcome unknown; NOT resubmitting",
                          len(group))
            for seq, args, kwargs in group:
                order_id = args[1] if len(args) > 1 else kwargs.get("order_id", "")
                market = args[2] if len(args) > 2 else kwargs.get("market", "")
                self._enqueue_async_outcome({
                    "kind": "cancel_error", "seq": seq,
                    "order_id": order_id, "market": market,
                    "error_id": -4,
                    "error_msg": ("cancel batch outcome unknown (%s: %s); the "
                                  "cancels MAY BE RUNNING -- check the order "
                                  "status before retrying"
                                  % (exc.__class__.__name__, exc)),
                })
            return
        if not results:
            # The server appends one result per item, so nothing back means the
            # batch never ran -- not that every cancel failed. Fall back to
            # singles, which is what would have happened anyway.
            log.warning("cancel batch of %d returned no results; submitting "
                        "one at a time", len(group))
            for job in group:
                try:
                    self._submit_async_cancel_single(job)
                except Exception:
                    log.exception("async cancel failed after empty batch")
            return
        by_index = {}
        for position, entry in enumerate(results):
            if isinstance(entry, dict):
                by_index[int(entry.get("index", position))] = entry
        for index, (seq, args, kwargs) in enumerate(group):
            order_id = args[1] if len(args) > 1 else kwargs.get("order_id", "")
            market = args[2] if len(args) > 2 else kwargs.get("market", "")
            entry = by_index.get(index) or {}
            if entry.get("success", False):
                self._enqueue_async_outcome({
                    "kind": "cancel_response", "seq": seq,
                    "order_id": order_id, "market": market,
                    "success": True,
                })
            else:
                self._enqueue_async_outcome({
                    "kind": "cancel_error", "seq": seq,
                    "order_id": order_id, "market": market,
                    "error_id": int(entry.get("code") or -1),
                    "error_msg": str(entry.get("error")
                                     or entry.get("message")
                                     or "cancel batch item failed"),
                })

    def cancel_order_stock_batch(self, account, cancels, timeout_seconds=None):
        """Cancel N orders in one RPC.

        Each item in *cancels* is a dict with ``order_sysid`` (or
        ``order_sys_id`` / ``order_id``) and optional ``market``.
        Returns a list of per-item result dicts.
        """
        account_id = _account_id(account, self.client.account_id)
        payload = []
        for item in cancels or []:
            entry = dict(item or {})
            entry.setdefault("account_id", account_id)
            payload.append(entry)
        params = {"account_id": account_id, "items": payload}
        if timeout_seconds is None:
            timeout_seconds = max(
                float(getattr(self.client, "timeout_seconds",
                              DEFAULT_RPC_TIMEOUT_SECONDS)),
                2.0 + 0.5 * len(payload),
            )
        return self.client.call(
            "cancel_order_stock_batch",
            params,
            account_id=account_id,
            timeout_seconds=timeout_seconds,
        ) or []

    # ------------------------------------------------------------------
    # issue #51 A: 同一笔委托的 async_response 必须先于它的 order/trade 到达。
    #
    # 两条回调走的是不同线程和不同通道: async_response 在异步下单的工作线程上
    # 触发, order/trade 来自 Redis pub/sub 监听线程。服务端在 order_callback
    # 里先推事件再回 RPC, 所以顺序颠倒是常态而非偶发。
    #
    # 做法是给「已提交但尚未收到 async_response」的委托设一道屏障: 它的
    # order/trade 事件先暂存, 等 response 触发后按到达顺序放行。延迟只加在
    # 异步下单这一条路径上——手工下单、同步下单、以及任何未登记的委托一律直通。
    # ------------------------------------------------------------------
    ASYNC_BARRIER_TIMEOUT_SECONDS = 10.0
    # response 触发前等屏障从暂存的委托事件里学到委托号的上限（issue #72）。
    # 委托号异步分配：推送通常比 RPC 应答快，几百毫秒内就能学到；超时则按
    # 原样发 response（order_id 回落 remark），不拖住回调。
    ASYNC_SYSID_WAIT_SECONDS = 2.0

    # Batch what is ALREADY queued when the worker wakes (issue #181). Never
    # waits to fill a batch: a lone order must not get slower so a busy one
    # can get faster. 2 is the smallest backlog where one round trip beats
    # two, and 500 is the server's own limit on submit_orders_batch.
    ASYNC_BATCH_MIN = 2
    ASYNC_BATCH_MAX = 500
    # stop()/进程退出时：等队列里已排队的 async 委托发完的上限，以及给
    # 在途 response 触发回调的宽限（issue #156）。
    ASYNC_EXIT_DRAIN_SECONDS = 5.0
    ASYNC_EXIT_CALLBACK_GRACE_SECONDS = 3.0
    # Async cancel: same queue-and-batch pattern as async orders (issue #50
    # applied to cancel_order_stock_async).  A batch of N cancels in one RPC
    # instead of N round trips: 15 cancels drop from ~30s to ~2s.
    ASYNC_CANCEL_BATCH_MIN = 2
    ASYNC_CANCEL_BATCH_MAX = 500

    def _order_barrier(self):
        barrier = getattr(self, "_async_barrier", None)
        if barrier is None:
            barrier = {}
            self._async_barrier = barrier
            self._async_barrier_lock = threading.Lock()
        return barrier

    @staticmethod
    def _async_remark(args, kwargs):
        """下单调用里的 order_remark —— 拿到 order_sys_id 之前唯一的关联键。"""
        return str(kwargs.get("order_remark") or (args[7] if len(args) > 7 else "") or "")

    def _arm_order_barrier(self, remark, seq):
        """登记一笔待响应的委托。remark 为空则不设屏障(无从关联)。"""
        if not remark:
            return
        self._order_barrier()
        with self._async_barrier_lock:
            # remark 不强制唯一(网格类策略常复用同一 remark)。同 remark 的上一笔
            # 可能还扣着暂存事件, 直接覆盖会把它们永久丢掉——丢事件比顺序错乱
            # 更糟, 所以接管旧 entry 并在锁外放行它的事件。
            superseded = self._async_barrier.pop(remark, None)
            self._async_barrier[remark] = {
                "seq": seq,
                "sys_ids": set(),
                "events": [],
                "deadline": time.time() + self.ASYNC_BARRIER_TIMEOUT_SECONDS,
            }
        for event in (superseded or {}).get("events", []):
            self._deliver_event(event)

    def _release_order_barrier(self, remark, seq=None):
        """response 已触发, 按到达顺序放行暂存的事件。"""
        if not remark:
            return
        self._order_barrier()
        with self._async_barrier_lock:
            entry = self._async_barrier.get(remark)
            if entry is None:
                return
            if seq is not None and entry["seq"] != seq:
                # 同 remark 的后一笔委托已接管屏障; 前一笔的 response 不该放它。
                return
            entry = self._async_barrier.pop(remark, None)
        for event in (entry or {}).get("events", []):
            self._deliver_event(event)

    def _sweep_order_barriers(self):
        """放行超时未收到 response 的委托。

        没有这一步, 一次失败的提交会把它的事件永久扣住——丢事件比顺序错乱更糟。
        """
        now = time.time()
        expired = []
        with self._async_barrier_lock:
            for remark, entry in list(self._async_barrier.items()):
                if now >= entry["deadline"]:
                    expired.append(self._async_barrier.pop(remark))
        for entry in expired:
            for event in entry.get("events", []):
                self._deliver_event(event)

    def _hold_if_pending(self, event):
        """属于待响应委托则暂存并返回 True, 否则返回 False 直通。"""
        barrier = self._order_barrier()
        if not barrier:
            return False
        remark = str(event.get("remark") or event.get("user_order_id") or "")
        sys_id = str(event.get("order_sys_id") or "")
        with self._async_barrier_lock:
            entry = barrier.get(remark) if remark else None
            if entry is None and sys_id:
                # 成交事件可能没有 remark; 用委托事件里学到的 order_sys_id 关联。
                for candidate in barrier.values():
                    if sys_id in candidate["sys_ids"]:
                        entry = candidate
                        break
            if entry is None:
                return False
            if sys_id:
                entry["sys_ids"].add(sys_id)
            entry["events"].append(event)
            return True

    @staticmethod
    def _async_job_fields(args, kwargs):
        """(account, stock_code, ...) from either calling convention.

        order_stock_async forwards *args/**kwargs untouched, so a job may carry
        either. The batch payload needs them named.
        """
        names = ("account", "stock_code", "order_type", "order_volume",
                 "price_type", "price", "strategy_name", "order_remark")
        out = {}
        for index, name in enumerate(names):
            if name in kwargs:
                out[name] = kwargs[name]
            elif len(args) > index:
                out[name] = args[index]
        return out

    def _submit_async_jobs(self, jobs):
        """Submit one job, or a queued backlog, keeping seq order per account."""
        if len(jobs) < self.ASYNC_BATCH_MIN:
            seq, args, kwargs = jobs[0]
            self._submit_async_single(seq, args, kwargs)
            return
        # order_stock_batch takes ONE account_id, so a mixed backlog splits by
        # account -- submitting the rest under the first job's account would
        # place orders on the wrong account. Within an account seq order is
        # kept; across accounts the order was never a contract.
        groups = {}
        for job in jobs:
            fields = self._async_job_fields(job[1], job[2])
            account_id = _account_id(fields.get("account"), self.client.account_id)
            groups.setdefault(account_id, []).append((job, fields))
        for account_id, group in groups.items():
            if len(group) < self.ASYNC_BATCH_MIN:
                for (seq, args, kwargs), _fields in group:
                    self._submit_async_single(seq, args, kwargs)
            else:
                self._submit_async_batch(account_id, group)

    def _submit_async_batch(self, account_id, group):
        """Submit a same-account backlog in one RPC, one outcome per job.

        wait_settlement=False on every item, deliberately. It is what the async
        contract already asks for (#69), and it also sidesteps a latent bug in
        the batch handler: _handle_submit_order defaults the flag to True and
        parks each item's settlement in the single _pending_settlement slot, so
        only the last item of a batch would ever get its order id backfilled.

        Fallbacks, and when they are allowed: a batch whose outcome is UNKNOWN
        (timeout, connection drop) must NOT be resubmitted -- the server may
        still be running it, and the resubmit doubles the orders (live: a
        100-item batch outlived the 30s timeout, the fallback resubmitted, and
        200 orders landed). Only two failure shapes may fall back to
        one-at-a-time: the server REPLIED with an error (the handler raises
        before its per-item loop, so nothing ran), and an empty result list
        (the handler appends one entry per item, so empty means it never ran).
        """
        payload = []
        for (seq, args, kwargs), fields in group:
            item = {name: value for name, value in fields.items()
                    if name != "account" and value is not None}
            item["wait_settlement"] = False
            # A unique per-item signal_id, for the same reason the single path
            # invents one (#190). It also makes the no-remark case safe against
            # a server that predates the idempotent flag: the batch tag falls
            # back to signal_id, so distinct ids cannot collide into a dedup.
            # An explicit order_remark still wins, and still reaches QMT as the
            # user wrote it.
            #
            # "rpc-<hex>", character for character what _handle_submit_order
            # invents, so the remark QMT shows for a no-remark async order is
            # the same "bqrpc:rpc-<hex>" it was on 0.3.20 -- that string is
            # visible in 备注 and is what the settlement lookup matches on
            # (#152), so its shape is not free to drift.
            item.setdefault("signal_id", "rpc-%s" % uuid.uuid4().hex)
            payload.append(item)
        try:
            results = self.order_stock_batch(account_id, payload,
                                             idempotent=False) or []
        except RpcServerRepliedError as exc:
            log.warning("async batch of %d refused by the server (%s); "
                        "submitting one at a time", len(group), exc)
            results = None
        except Exception as exc:
            # Timeout / transport failure: the batch's fate is unknown. Do NOT
            # resubmit -- report each item as unknown-outcome instead. The
            # message must say the orders may be live, because they may be:
            # a caller that retries on failure double-orders.
            log.exception("async batch of %d outcome unknown; NOT resubmitting",
                          len(group))
            for (seq, args, kwargs), fields in group:
                self._enqueue_batch_outcome(seq, fields, {
                    "success": False, "code": -4,
                    "error": ("batch outcome unknown (%s: %s); the orders MAY "
                              "BE LIVE -- query orders before retrying"
                              % (exc.__class__.__name__, exc)),
                })
            return
        if not results:
            if results is not None:
                # The server appends one result per item, so nothing back means
                # the batch never ran -- not that every order failed. Reporting
                # N failures here would be the dangerous reading: a caller that
                # retries on failure would place them all a second time.
                log.warning("async batch of %d returned no results; submitting "
                            "one at a time", len(group))
            for (seq, args, kwargs), _fields in group:
                try:
                    self._submit_async_single(seq, args, kwargs)
                except Exception:
                    log.exception("async order failed after batch fallback seq=%s", seq)
            return
        by_index = {}
        for position, entry in enumerate(results):
            if isinstance(entry, dict):
                by_index[int(entry.get("index", position))] = entry
        for index, ((seq, args, kwargs), fields) in enumerate(group):
            self._enqueue_batch_outcome(seq, fields, by_index.get(index))

    def _enqueue_batch_outcome(self, seq, fields, entry):
        """Translate one item of a batch result into that job's outcome unit.

        The per-order wait for a late order id is NOT done for batch items.
        Serially waiting up to ASYNC_SYSID_WAIT_SECONDS on each of N items
        would undo the batch: 300 orders would spend 600s waiting instead of
        0.6s submitting. Items whose id is not back yet answer with the remark,
        and the real id still arrives on the order_callback push like any other.
        """
        remark = str(fields.get("order_remark") or "")
        stock_code = str(fields.get("stock_code") or "")
        entry = entry if isinstance(entry, dict) else {}
        if not entry or not entry.get("success", False):
            self._enqueue_async_outcome({
                "kind": "error", "seq": seq, "remark": remark,
                "stock_code": stock_code,
                "order_remark": remark,
                "error_id": int(entry.get("code") or -1),
                "error_msg": str(entry.get("error")
                                 or "order missing from batch result"),
            })
            return
        order_sys_id = str(entry.get("order_sys_id") or "")
        user_order_id = str(entry.get("user_order_id") or remark or "")
        self._enqueue_async_outcome({
            "kind": "response", "seq": seq, "remark": remark,
            "stock_code": stock_code,
            "strategy_name": str(fields.get("strategy_name") or ""),
            "order_remark": remark,
            "order_sys_id": order_sys_id,
            "user_order_id": user_order_id,
            "wait_for_sysid": False,
        })

    def _submit_async_single(self, seq, args, kwargs):
        """Submit one job and enqueue its outcome. Runs on the order worker."""
        stock_code = str(kwargs.get("stock_code") or (args[1] if len(args) > 1 else ""))
        remark = self._async_remark(args, kwargs)
        order_remark = str(kwargs.get("order_remark") or (args[7] if len(args) > 7 else "") or "")
        try:
            # wait_settlement=False：passorder 一返回就应答，不在 worker 里等
            # 服务端结算（那是 #69 要的吞吐）。委托号从推送事件学——屏障暂存的
            # 委托事件里会带上（触发 response 前至多等 2s，学不到就回落 remark）。
            result = self.order_stock_result(*args, wait_settlement=False, **kwargs)
        except Exception as exc:
            self._enqueue_async_outcome({
                "kind": "error", "seq": seq, "remark": remark,
                "stock_code": stock_code, "order_remark": order_remark,
                "error_id": getattr(exc, "errno", 0), "error_msg": str(exc),
            })
            return

        order_sys_id = ""
        user_order_id = ""
        if isinstance(result, dict):
            order_sys_id = str(result.get("order_sys_id") or result.get("order_sysid") or "")
            user_order_id = str(result.get("user_order_id") or "")
        elif result is not None:
            order_sys_id = str(result)

        # order_stock returns -1 when the submit itself failed. The server also
        # pushes an order_error for a 废单; the two carry different information
        # (RPC submit failure vs QMT rejection detail), so both stay available.
        if order_sys_id == "-1" or result == -1:
            self._enqueue_async_outcome({
                "kind": "error", "seq": seq, "remark": remark,
                "stock_code": stock_code, "order_remark": order_remark,
                "error_id": -1,
                "error_msg": "order submit failed (order_stock returned -1)",
            })
            return

        self._enqueue_async_outcome({
            "kind": "response", "seq": seq, "remark": remark,
            "stock_code": stock_code,
            "strategy_name": str(kwargs.get("strategy_name") or (args[6] if len(args) > 6 else "")),
            "order_remark": order_remark,
            "order_sys_id": order_sys_id,
            "user_order_id": user_order_id,
            "wait_for_sysid": True,
        })

    def _enqueue_async_outcome(self, unit):
        self._async_callback_queue.put(unit)

    def _fire_async_outcome(self, unit):
        """One job's callback, on the dispatcher thread.

        The barrier release rides in the same unit AFTER the user callback, so
        the #51 contract -- a委托's async_response before its held order/trade
        events -- is unchanged from when everything ran on one thread.
        """
        kind = unit.get("kind", "")
        if kind in ("cancel_response", "cancel_error"):
            self._fire_async_cancel_outcome(unit)
            return
        callback = self.callback
        seq = unit["seq"]
        remark = unit["remark"]
        try:
            if unit["kind"] == "error":
                if callback is not None:
                    callback.on_order_error(
                        CompatObject(
                            error_id=unit["error_id"],
                            error_msg=unit["error_msg"],
                            order_sysid="",          # MiniQMT 规范名 (issue #65)
                            order_sys_id="",
                            order_id=self._order_object_id(""),   # int (#113)
                            stock_code=unit["stock_code"],
                            seq=seq,
                            order_remark=unit["order_remark"],
                        )
                    )
            elif callback is not None:
                # Native XtOrderResponse shape: one argument carrying
                # account_id/order_id/seq/error_msg.
                #
                # 委托号异步分配（#50）：服务端应答时通常还没有。触发 response 前
                # 先等屏障从暂存的委托事件里学到委托号（事件推送一般比 RPC 应答
                # 快，bounded 2s），否则 order_id 只能回落成 remark（issue #72）。
                # 这个等如今在回调线程上——它拖住的只是后续回调，不再是后续下单。
                order_sys_id = unit["order_sys_id"]
                if unit.get("wait_for_sysid") and not order_sys_id and remark:
                    wait_deadline = time.time() + self.ASYNC_SYSID_WAIT_SECONDS
                    while time.time() < wait_deadline:
                        learned = ""
                        with self._async_barrier_lock:
                            entry = self._async_barrier.get(remark)
                            if entry and entry["sys_ids"]:
                                learned = sorted(entry["sys_ids"])[0]
                        if learned:
                            order_sys_id = learned
                            break
                        time.sleep(0.05)
                callback.on_order_stock_async_response(
                    CompatObject(
                        account_id=self.client.account_id,
                        seq=seq,
                        order_id=self._order_object_id(order_sys_id or unit["user_order_id"]),
                        order_sysid=order_sys_id,    # MiniQMT 规范名 (issue #65)
                        order_sys_id=order_sys_id,
                        stock_code=unit["stock_code"],
                        strategy_name=unit["strategy_name"],
                        order_remark=unit["order_remark"],
                        error_msg="",
                    ),
                )
        except Exception:
            log.exception("user callback failed: async outcome seq=%s kind=%s",
                          seq, unit.get("kind"))
        finally:
            # response/error 已触发 -> 放行这笔委托暂存的 order/trade (issue #51)。
            self._release_order_barrier(remark, seq)

    def _fire_async_cancel_outcome(self, unit):
        """Fire on_cancel_order_stock_async_response / on_cancel_error."""
        callback = self.callback
        seq = unit["seq"]
        order_id = unit.get("order_id", "")
        try:
            if unit["kind"] == "cancel_error":
                if callback is not None:
                    callback.on_cancel_error(
                        CompatObject(
                            error_id=unit["error_id"],
                            error_msg=unit["error_msg"],
                            # seq was missing here while the response path had
                            # it -- an uncorrelatable error is how "which
                            # cancel failed?" goes unanswered.
                            seq=seq,
                            order_sysid=str(order_id or ""),
                            order_sys_id=str(order_id or ""),
                            order_id=self._order_object_id(order_id),
                            stock_code="",
                        )
                    )
            elif callback is not None:
                ok = unit.get("success", False)
                callback.on_cancel_order_stock_async_response(
                    CompatObject(
                        account_id=self.client.account_id,
                        seq=seq,
                        success=bool(ok),
                        cancel_result=0 if ok else -1,
                        # The native cancel return answers "the request went
                        # out", not "the order is cancelled" -- it has been
                        # false while the cancel landed (#148) and true for a
                        # nonexistent order (#151). Never word it as a
                        # rejection; the order-status push (54) or a query is
                        # the confirmation.
                        error_msg="" if ok else (
                            "cancel not confirmed by the counter; the order may "
                            "still get cancelled -- check the order-status push "
                            "or query before assuming either way"),
                        order_sysid=str(order_id or ""),
                        order_sys_id=str(order_id or ""),
                        order_id=self._order_object_id(order_id),
                    ),
                )
        except Exception:
            log.exception(
                "user callback failed: async cancel outcome seq=%s kind=%s",
                seq, unit.get("kind"),
            )

    def order_stock_async(self, *args, **kwargs):
        """Queue an order and return its seq immediately (MiniQMT semantics).

        This used to call order_stock inline, so it blocked for the full RPC
        round trip plus -- after the issue #44 change -- however long the server
        waited for QMT to assign an order id. That is 0.5-1s per order, which
        defeats the point of an async API (issue #50).

        Now the submit runs on a worker thread and the outcome arrives through
        on_order_stock_async_response / on_order_error, both carrying the seq so
        callers can correlate. Returns the seq without touching the network.

        The worker is a daemon thread: a script whose main thread exits right
        after queueing kills it mid-queue, losing every order not yet
        submitted (issue #156). stop() and an atexit hook both drain the queue
        (bounded); callbacks still require the process to be alive -- they
        cannot arrive after it is gone. Long-running strategies are unaffected.
        """
        seq = self._next_async_seq()
        # 屏障要在入队之前设好: 委托可能在本函数返回之前就被推送出来。
        self._arm_order_barrier(self._async_remark(args, kwargs), seq)
        self._ensure_async_order_worker()
        self._register_exit_drain()
        self._async_order_queue.put((seq, args, kwargs))
        return seq

    def wait_async_orders(self, timeout=10.0):
        """Block until every queued async order has been submitted AND its
        callback fired.

        For tests and for shutdown; the API itself is fire-and-forget. Returns
        False on timeout rather than hanging. Uses task_done bookkeeping on both
        queues, so it covers the in-flight submit and the in-flight callback,
        not merely the drained queues.
        """
        deadline = time.time() + float(timeout)
        for name in ("_async_order_queue", "_async_cancel_queue",
                      "_async_callback_queue"):
            queue_obj = getattr(self, name, None)
            if queue_obj is None:
                continue
            while queue_obj.unfinished_tasks:
                if time.time() > deadline:
                    return False
                time.sleep(0.005)
        return True

    def order_stock_batch(self, account, orders, batch_id="", idempotent=True,
                          timeout_seconds=None):
        """Submit N orders in one RPC.

        ``idempotent`` (default True, unchanged) is the batch contract: every
        item needs a tag, and a tag already submitted answers success without
        placing again, so a retried batch cannot double-order. Pass False for
        callers that never agreed to that -- order_stock_async routes its
        backlog through here (#181) and lost orders to it (#190).

        The wait scales with N when not given: the server runs items serially
        and per-item cost swings from ~ms (market hours) to ~300ms (counter
        disconnected), so a flat default turns a slow-but-alive batch into a
        client-side timeout -- and a retried batch doubles orders.
        """
        account_id = _account_id(account, self.client.account_id)
        payload = []
        for item in orders or []:
            entry = dict(item or {})
            entry.setdefault("account_id", account_id)
            payload.append(entry)
        params = {"account_id": account_id, "orders": payload}
        if not idempotent:
            params["idempotent"] = False
        if batch_id:
            params["batch_id"] = str(batch_id)
        if timeout_seconds is None:
            timeout_seconds = max(
                float(getattr(self.client, "timeout_seconds",
                              DEFAULT_RPC_TIMEOUT_SECONDS)),
                BATCH_TIMEOUT_FLOOR_SECONDS
                + BATCH_TIMEOUT_PER_ITEM_SECONDS * len(payload),
            )
        return self.client.call(
            "order_stock_batch",
            params,
            account_id=account_id,
            timeout_seconds=timeout_seconds,
        ) or []

    def passorder(self, op_type, order_type, account, order_code, price_type,
                  price, volume, strategy_name="", quick_trade=None,
                  user_order_id="", dry_run=False):
        """Call QMT's native passorder straight through (route 2).

        The argument order matches QMT's own signature -- opType, orderType,
        accountid, orderCode, prType, price, volume, strategyName, quickTrade,
        userOrderId -- with ContextInfo supplied server-side (it only exists
        inside QMT). Unlike order_stock this exposes orderType and quickTrade,
        and runs NONE of order_stock's safety nets: no opType validation, no
        code case-normalization (#95), no settlement read-back. You own the
        native contract.

        order_type / quick_trade default to the server config (1101 / 2) when
        left None, so the common case still reads like order_stock.

        passorder is async and returns no order id (QMT assigns the 合同编号
        later on the order_callback push), same as order_stock_async. This
        returns the server's ack dict.

        dry_run=True returns the exact 11-tuple the server WOULD pass to
        passorder without placing an order -- use it to check your mapping.
        """
        account_id = _account_id(account, self.client.account_id)
        params = {
            "account_id": account_id,
            "op_type": op_type,
            "order_code": order_code,
            "price_type": price_type,
            "price": price,
            "volume": volume,
            "strategy_name": strategy_name,
            "user_order_id": user_order_id,
        }
        if order_type is not None:
            params["order_type"] = order_type
        if quick_trade is not None:
            params["quick_trade"] = quick_trade
        if dry_run:
            params["dry_run"] = True
        return self.client.call("passorder", params, account_id=account_id)

    def cancel_order_stock_sysid(self, account, market, order_sysid):
        """MiniQMT contract: 0 on success, -1 on failure (issue #113).

        This returned a bool, which is worse than a type mismatch -- it inverts
        the meaning. ``if trader.cancel_order_stock(...) == 0`` is how MiniQMT
        code checks success, and ``False == 0`` is True, so a *failed* cancel
        read as a successful one while a successful one read as failed.
        """
        account_id = _account_id(account, self.client.account_id)
        data = self.client.call(
            "cancel_order_stock_sysid",
            {
                "account_id": account_id,
                "market": market,
                # Send the broker's own 合同编号, not the int we derived from it.
                "order_sysid": self._resolve_order_sys_id(order_sysid),
            },
            account_id=account_id,
        ) or {}
        return 0 if bool(data.get("success", data)) else -1

    def cancel_order_stock(self, account, order_id):
        return self.cancel_order_stock_sysid(account, "", order_id)

    def unsubscribe(self, account):
        # MiniQMT xttrader.unsubscribe(account) — 取消账户订阅。
        # Big QMT RPC 模式下账户是被动响应，unsubscribe 为 no-op。
        return 0

    # ------------------------------------------------------------------
    # 账户 / 融资融券扩展查询
    # 这些在 MiniQMT 走 XtQuantServer RPC；Big QMT 经
    # get_trade_detail_data 查询，需相应账户权限（两融账户等）。
    # 无权限/上下文未绑定时服务端降级为 []。
    # ------------------------------------------------------------------

    def _query_account_list(self, account, method):
        account_id = _account_id(account, self.client.account_id)
        try:
            return self.client.call(method, {"account_id": account_id}, account_id=account_id) or []
        except Exception:
            return []

    def query_account_infos(self, account=None):
        return self._query_account_list(account, "query_account_infos")

    def query_account_status(self, account=None):
        return self._query_account_list(account, "query_account_status")

    def query_credit_detail(self, account):
        """信用账户明细，读终端缓存的信用账号对象（同步）。

        大 QMT 走 get_trade_detail_data(accId, 'CREDIT', 'ACCOUNT')。这份是
        「非查柜台」的本地缓存（官方参考 3.14），券商没推就是空的 —— 空列表
        时用 query_credit_account() 问柜台那一份（#201 / #202）。
        """
        return self._query_account_list(account, "query_credit_detail")

    def query_credit_account(self, account=None, wait_seconds=None,
                             max_age_seconds=None):
        """信用账户明细，查柜台的那一份（官方参考 6.13）。

        大 QMT 那边是异步的：query_credit_account 立刻返回，结果从
        credit_account_callback 出来。桥这边替你发查询、等回调、缓存结果，
        所以这个调用本身是同步的。

        **返回的是最近一次柜台答案，不是这一次查询的结果。** 回调实测投递在
        MainThread 上（handler 也在那条线程），所以这一次的新答案只可能在本次
        调用返回之后才落进缓存 —— 想要它，隔一会儿再调一次。看 fresh /
        age_seconds 判断新旧，不要只看 rows（#202）。

        缓存**不会自己刷新**：没人调就永远不刷。所以默认超过 120 秒的数据
        直接不给（`dropped_stale`），免得有人拿几小时前的维持担保比例去做
        决策还毫无察觉。传 max_age_seconds=0 显式放弃这层保护。

        **要实时的维持担保比例 / 可用额度，请用 query_credit_detail** ——
        同步、读终端本地缓存、没有陈旧问题。这条查柜台的路是对账用的。

        Args:
            wait_seconds: 等回调的秒数。默认 0（不等）—— 回调若与 handler 同在
                一条线程，等待永远等不到，只会白占 adjust 主线程。
            max_age_seconds: 缓存超过这个年龄就不返回 rows。默认 120，0 = 不限。

        Returns:
            dict: rows / count / fresh / stale / age_seconds / max_age_seconds /
                dropped_stale / dropped_stale_reason / query_issued /
                not_issued_reason / callbacks_seen / callback_thread / seq /
                error / callback_bound
        """
        account_id = _account_id(account, self.client.account_id)
        params = {"account_id": account_id}
        if wait_seconds is not None:
            params["wait_seconds"] = float(wait_seconds)
        if max_age_seconds is not None:
            params["max_age_seconds"] = float(max_age_seconds)
        return self.client.call("query_credit_account", params, account_id=account_id)

    def query_stk_compacts(self, account):
        return self._query_account_list(account, "query_stk_compacts")

    def query_credit_subjects(self, account):
        return self._query_account_list(account, "query_credit_subjects")

    def query_credit_slo_code(self, account):
        return self._query_account_list(account, "query_credit_slo_code")

    def query_credit_assure(self, account):
        return self._query_account_list(account, "query_credit_assure")

    def query_appointment_info(self, account):
        return self._query_account_list(account, "query_appointment_info")

    def query_smt_secu_info(self, account):
        return self._query_account_list(account, "query_smt_secu_info")

    def query_smt_secu_rate(self, account, stock_code, max_term, fare_way, credit_type, trade_type):
        account_id = _account_id(account, self.client.account_id)
        try:
            return self.client.call(
                "query_smt_secu_rate",
                {"account_id": account_id, "stock_code": stock_code, "max_term": max_term,
                 "fare_way": fare_way, "credit_type": credit_type, "trade_type": trade_type},
                account_id=account_id,
            ) or []
        except Exception:
            return []

    def query_ipo_data(self, account=None, stock_type=""):
        """新股申购信息 (大 QMT get_ipo_data).
        stock_type: "" 全部, "STOCK" 新股, "BOND" 新债.
        8-28 修复: 直接调 get_ipo_data 带 type 参数 (原走 query_appointment_info
        传 account_id 导致返回空)."""
        account_id = _account_id(account, self.client.account_id)
        try:
            data = self.client.call(
                "get_ipo_data",
                {"type": stock_type},
                account_id=account_id,
            )
            # get_ipo_data answers with a dict keyed by subscription code.
            if isinstance(data, dict):
                return data
            if data:
                # Non-empty and not a dict: an older server is still routing
                # this through the detail-row normaliser, which iterates the
                # dict by key and discards every value -- real IPOs arrive as
                # [{}, {}]. Coercing that to {} silently reports "no IPOs
                # today", so say so instead of swallowing it.
                log.warning(
                    "query_ipo_data: server returned %s, not a mapping -- the "
                    "QMT-side bridge is too old to preserve get_ipo_data's "
                    "shape and the rows are empty. Update the server side.",
                    type(data).__name__)
            return {}
        except Exception:
            return {}

    def query_new_purchase_limit(self, account):
        """新股申购额度 (大 QMT get_new_purchase_limit).
        返回 {板块: 额度} 或 {} (失败/无权限)."""
        account_id = _account_id(account, self.client.account_id)
        try:
            data = self.client.call(
                "get_new_purchase_limit",
                {"account_id": account_id},
                account_id=account_id,
            ) or {}
            if isinstance(data, dict):
                return data
            return {}
        except Exception:
            return {}

    def ipo_subscribe_all(self, account=None, stock_type="STOCK",
                          markets=("SH", "SZ"), dry_run=False,
                          strategy_name="ipo"):
        """Subscribe to today's IPOs. Nothing here runs on a timer.

        This is deliberately a call you make, not a behaviour the bridge takes
        on: it places real orders, so it must be something the operator asked
        for on that day. It also goes through order_stock, which means it obeys
        rpc_allow_order_methods and inherits the gateway's passorder settings
        (orderType 1101, prType 11 指定价, quickTrade 2 -- 2 being the value the
        API reference requires for a non-bar context).

        markets: exchanges to subscribe on. SH/SZ subscriptions are backed by
            market value and freeze no cash; BJ freezes cash, so it is excluded
            by default. A code whose market cannot be identified is SKIPPED,
            never subscribed on a guess.
        dry_run: return the plan without placing anything.

        Returns one dict per candidate: stock_code, name, price, volume,
        action ("subscribed" / "skipped" / "failed") and reason.
        """
        allowed = set(str(m).upper() for m in (markets or ()))
        results = []
        for code, info in (self.query_ipo_data(account, stock_type=stock_type) or {}).items():
            info = info or {}
            entry = {
                "stock_code": code,
                "name": str(info.get("name") or ""),
                "price": _safe_float(info.get("issuePrice"), 0.0),
                "volume": _safe_int(info.get("maxPurchaseNum"), 0),
                "action": "skipped",
                "reason": "",
            }
            market = ipo_market_of(code)
            if market is None:
                entry["reason"] = "market not identified"
            elif market not in allowed:
                entry["reason"] = "%s not in %s" % (market, sorted(allowed))
            elif entry["price"] <= 0 or entry["volume"] <= 0:
                entry["reason"] = "issuePrice/maxPurchaseNum missing or non-positive"
            elif dry_run:
                entry["action"] = "planned"
            else:
                try:
                    entry["result"] = self.ipo_subscribe(
                        account, code, entry["volume"], entry["price"],
                        strategy_name=strategy_name,
                        order_remark="ipo:%s" % code)
                    entry["action"] = "subscribed"
                except Exception as exc:
                    entry["action"] = "failed"
                    entry["reason"] = "%s: %s" % (exc.__class__.__name__, exc)
            results.append(entry)
        return results

    def ipo_subscribe(self, account, stock_code, volume, price, strategy_name="ipo",
                      order_remark="ipo_sub"):
        """新股申购 (打新). 复用现有 order_stock RPC (passorder opType=23 指定价).
        stock_code 应为 get_ipo_data 返回的申购代码 (带后缀), price=发行价.
        返回 {order_sys_id} 或 {} (失败)."""
        return self.order_stock_result(
            account, stock_code, STOCK_BUY, int(volume),
            FIX_PRICE, float(price), strategy_name, order_remark,
        )

    # ------------------------------------------------------------------
    # async 变体：MiniQMT 的 *_async 方法返回 seq 后异步回调。
    # 在 RPC 模型里请求-响应本就是同步的，这里直接转发到同步实现并
    # 返回一个递增 seq，让旧代码 ``xt_trader.query_stock_positions_async(acc)``
    # 不报错（回调仍由 register_callback 注册的回调在事件来时触发）。
    # ------------------------------------------------------------------

    _async_seq = 0

    def _next_async_seq(self):
        BigQmtXtTrader._async_seq += 1
        return BigQmtXtTrader._async_seq

    def _async_query(self, sync_call, account, callback, *args, **kwargs):
        """Shared async query helper.

        MiniQMT's *_async query methods take a callback and hand the result to
        it (they return None). We accept an OPTIONAL callback for compat: when
        given, we call callback(result) synchronously (our RPC is already
        synchronous) and return None like MiniQMT; when omitted, we keep our
        seq-returning extension so existing callers don't break.
        """
        result = sync_call(account, *args, **kwargs)
        if callback is not None:
            try:
                callback(result)
            except Exception:
                log.exception(
                    "user callback failed: %s",
                    getattr(sync_call, "__name__", "async_query_callback"),
                )
            return None
        return self._next_async_seq()

    def query_stock_asset_async(self, account, callback=None):
        return self._async_query(self.query_stock_asset, account, callback)

    def query_stock_positions_async(self, account, callback=None):
        return self._async_query(self.query_stock_positions, account, callback)

    def query_stock_orders_async(self, account, cancelable_only=False, callback=None):
        if callback is not None:
            result = self.query_stock_orders(account, cancelable_only)
            try:
                callback(result)
            except Exception:
                pass
            return None
        return self._next_async_seq()

    def query_stock_trades_async(self, account, callback=None):
        return self._async_query(self.query_stock_trades, account, callback)

    def query_account_infos_async(self, account=None, callback=None):
        if callback is not None:
            result = self.query_account_infos(account)
            try:
                callback(result)
            except Exception:
                pass
            return None
        return self._next_async_seq()

    def query_account_status_async(self, account=None, callback=None):
        if callback is not None:
            result = self.query_account_status(account)
            try:
                callback(result)
            except Exception:
                pass
            return None
        return self._next_async_seq()

    def query_credit_detail_async(self, account, callback=None):
        return self._async_query(self.query_credit_detail, account, callback)

    def query_stk_compacts_async(self, account, callback=None):
        return self._async_query(self.query_stk_compacts, account, callback)

    def query_credit_subjects_async(self, account, callback=None):
        return self._async_query(self.query_credit_subjects, account, callback)

    def query_credit_slo_code_async(self, account, callback=None):
        return self._async_query(self.query_credit_slo_code, account, callback)

    def query_credit_assure_async(self, account, callback=None):
        return self._async_query(self.query_credit_assure, account, callback)

    def query_ipo_data_async(self, account=None, callback=None):
        if callback is not None:
            result = self.query_ipo_data(account)
            try:
                callback(result)
            except Exception:
                log.exception("user callback failed: query_ipo_data_async")
            return None
        return self._next_async_seq()

    def query_new_purchase_limit_async(self, account, callback=None):
        return self._async_query(self.query_new_purchase_limit, account, callback)

    def query_appointment_info_async(self, account, callback=None):
        return self._async_query(self.query_appointment_info, account, callback)

    def cancel_order_stock_async(self, account, order_id):
        """Queue a cancel and return its seq immediately (non-blocking).

        Used to call cancel_order_stock inline, blocking for the full RPC
        round trip per cancel.  15 cancels serialised at ~2s each = 30s.
        Now the cancel runs on a worker thread and a backlog is batched
        into one RPC (cancel_orders_batch), so 15 cancels take ~2s total.

        The outcome arrives through on_cancel_order_stock_async_response
        or on_cancel_error on the callback worker thread.
        """
        seq = self._next_async_seq()
        self._ensure_async_cancel_worker()
        self._register_exit_drain()
        self._async_cancel_queue.put((seq, (account, order_id), {}))
        return seq

    def cancel_order_stock_sysid_async(self, account, market, order_sysid):
        """Queue a cancel-by-sysid and return its seq immediately."""
        seq = self._next_async_seq()
        self._ensure_async_cancel_worker()
        self._register_exit_drain()
        self._async_cancel_queue.put(
            (seq, (account, order_sysid, market), {})
        )
        return seq

    def set_relaxed_response_order_enabled(self, enabled=True):
        # 内部行为开关，RPC 模式下无意义，no-op。
        return 0

    def smt_appointment_async(self, account, stock_code, apt_days, apt_volume,
                              fare_ratio, sub_rare_ratio, fine_ratio, begin_date):
        # SMB/预约打新走独立通道，RPC 桥不支持；返回 -1 表示失败（对齐 MiniQMT
        # 语义：seq 为 -1 表示委托失败）。
        return -1

    def _account_type_value(self, item=None):
        """account_type for an XtOrder / XtTrade / XtPosition, as an int.

        Server first (it is the one that knows what this deployment trades
        as -- #103), then what the caller declared, then SECURITY_ACCOUNT so
        the field is never absent. Positions used to hardcode 2 and orders and
        trades did not carry it at all (#133).
        """
        code = _account_type_code((item or {}).get("account_type"))
        if code:
            return code
        code = _account_type_code(self._server_account_type
                                 or self._declared_account_type)
        if code:
            return code
        try:
            from xtquant.xtconstant import SECURITY_ACCOUNT

            return int(SECURITY_ACCOUNT)
        except Exception:
            return 2

    def _order_from_dict(self, account_id, item):
        action = item.get("action")
        order_type = _action_to_order_type(action)
        order_sysid = str(item.get("order_sys_id") or item.get("order_sysid") or item.get("order_id") or "")
        return CompatObject(
            account_id=account_id,
            stock_code=_full_a_share_code(item.get("stock_code")),
            order_type=order_type,
            order_status=_safe_int(item.get("status", item.get("order_status")), ORDER_UNKNOWN),
            order_volume=_safe_int(item.get("volume", item.get("order_volume"))),
            traded_volume=_safe_int(item.get("traded_volume")),
            price=_safe_float(item.get("price")),
            traded_price=_safe_float(
                item.get("traded_price", item.get("avg_traded_price", item.get("m_dTradedPrice")))
            ),
            # 柜台的精确成交金额 (issue #173) -- ccxt 适配层的
            # order["cost"]。旧部署不发这个键，那就是 0.0；不在这里
            # 拿 traded_price × traded_volume 兑，因为那是估算值，让它
            # 冒充柜台金额正好是这个 issue 要避开的事。
            trade_amount=_safe_float(
                item.get("trade_amount", item.get("m_dTradeAmount"))
            ),
            order_sysid=order_sysid,
            # MiniQMT: order_id is the int 委托编号, order_sysid the string
            # 柜台编号. Both, from one 合同编号 (issue #113).
            order_id=self._order_object_id(
                order_sysid or str(item.get("user_order_id") or "")),
            strategy_name=str(item.get("strategy_name") or ""),
            order_remark=str(item.get("remark") or item.get("user_order_id") or ""),
            # MiniQMT XtOrder.order_time 是 Unix 秒。服务端订单事件只发
            # created_at_ts 不发 order_time，所以没有显式 order_time 时
            # 用 created_at_ts 兜底；两者都没有才落到 0（不要当成 1970 年）。
            order_time=_safe_int(item.get("order_time") or item.get("created_at_ts"), 0),
            # MiniQMT XtOrder.status_msg —— 废单时柜台给的原因 (issue #60)。
            status_msg=str(item.get("status_msg") or ""),
            price_type=item.get("price_type"),
            # xttype.XtOrder 契约里有、以前没发的字段 (issue #133)。旧部署不发
            # 这些键，所以每个都要能在缺失时给出 MiniQMT 语义的默认值，而不是
            # 让调用方撞 AttributeError —— 那正是这个 issue 报的现象。
            account_type=self._account_type_value(item),
            instrument_name=str(item.get("instrument_name") or ""),
            secu_account=str(item.get("secu_account") or ""),
            offset_flag=item.get("offset_flag"),
            direction=item.get("direction"),
        )

    def _trade_from_dict(self, account_id, item):
        action = item.get("action")
        order_type = _action_to_order_type(action)
        order_sysid = str(item.get("order_sys_id") or item.get("order_sysid") or "")
        trade_id = str(item.get("trade_id") or "")
        traded_volume = _safe_int(item.get("volume", item.get("traded_volume")))
        traded_price = _safe_float(item.get("price", item.get("traded_price")))
        amount = item.get("amount")
        if not amount:
            # 服务端未取到金额（缺失或 0）时按 价格 * 数量 估算，保证盈亏统计不为 0。
            amount = traded_price * traded_volume
        return CompatObject(
            account_id=account_id,
            stock_code=_full_a_share_code(item.get("stock_code")),
            order_type=order_type,
            order_sysid=order_sysid,
            order_id=self._order_object_id(order_sysid),
            trade_id=trade_id,
            # MiniQMT 字段契约: traded_id/traded_time 是业务代码读取的名字。
            traded_id=trade_id,
            traded_volume=traded_volume,
            traded_price=traded_price,
            # 优先级: 服务端真实成交时间(traded_time) -> 事件到达时间(created_at_ts)
            # -> traded_at 字符串解析。
            traded_time=_to_unix_seconds(
                item.get("traded_time") or item.get("created_at_ts") or item.get("traded_at")
            ),
            traded_amount=_safe_float(amount, 0.0),
            traded_at=str(item.get("traded_at") or ""),
            strategy_name=str(item.get("strategy_name") or ""),
            order_remark=str(item.get("user_order_id") or item.get("remark") or ""),
            # 同 _order_from_dict：xttype.XtTrade 契约里有而以前没发的 (issue #133)。
            account_type=self._account_type_value(item),
            instrument_name=str(item.get("instrument_name") or ""),
            secu_account=str(item.get("secu_account") or ""),
            commission=_safe_float(item.get("commission"), 0.0),
            offset_flag=item.get("offset_flag"),
            direction=item.get("direction"),
        )


XtQuantTrader = BigQmtXtTrader


_default_client = None
xt_trader = None
xtdata = None


def configure(account_id=None, redis_client=None, redis_config=None, timeout_seconds=None):
    global _default_client, xt_trader, xtdata
    _default_client = BigQmtRpcClient(
        account_id=account_id,
        redis_client=redis_client,
        redis_config=redis_config,
        timeout_seconds=timeout_seconds,
    )
    if xt_trader is None:
        xt_trader = BigQmtXtTrader(account_id=_default_client.account_id, redis_client=_default_client.redis_client)
    xt_trader.client = _default_client
    if xtdata is None:
        xtdata = BigQmtXtData(_default_client)
    else:
        xtdata.client = _default_client
    return xt_trader, xtdata


def get_default_client():
    global _default_client
    if _default_client is None:
        configure()
    return _default_client


configure()


__all__ = [
    "BigQmtRpcClient",
    "BigQmtXtData",
    "BigQmtXtTrader",
    "CompatObject",
    "StockAccount",
    "XtQuantTrader",
    "XtQuantTraderCallback",
    "configure",
    "get_default_client",
    "load_client_config",
    "xt_trader",
    "xtdata",
]
