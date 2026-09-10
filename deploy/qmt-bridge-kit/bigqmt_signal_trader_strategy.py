# coding: utf-8
"""ThinkTrader Big QMT strategy entry.

Keep this entry file ASCII-only because QMT's strategy editor may save the
generated strategy file with a local code page while preserving this coding
header. Business logic stays in the importable package.
"""

import datetime
import importlib as _importlib
import sys
import threading
import time
import traceback as _traceback

# The DRYRUN entry reloads strategy/runtime/redis_rpc/redis_common but NOT the
# other package submodules. Without this, the "from adapter_factory import build_app"
# below re-binds the STALE cached module on every strategy re-run, so edits to
# adapter_factory never take effect until a full terminal restart. Force-reload it
# (only it -- reloading the adapter classes would break isinstance elsewhere) so a
# plain strategy re-run picks up build_app fixes. build_app imports the adapter
# classes lazily, so their identity is preserved.
_af_mod = sys.modules.get("bigqmt_signal_trader.adapter_factory")
if _af_mod is not None:
    try:
        _importlib.reload(_af_mod)
    except Exception as _reload_err:
        print("[bigqmt_signal_trader] reload adapter_factory failed: %s" % _reload_err)

try:
    _load_bridge_module = __bigqmt_load_local_module
except NameError:
    _load_bridge_module = None

if _load_bridge_module is not None:
    _adapter_factory = _load_bridge_module("bigqmt_signal_trader.adapter_factory")
    _runner = _load_bridge_module("bigqmt_signal_trader.runner")
    _runtime_bigqmt = _load_bridge_module("bigqmt_signal_trader.runtime_bigqmt")
    _order_watch = _load_bridge_module("bigqmt_signal_trader.order_watch")
    _default_build_app = _adapter_factory.build_app
    forward_order_event = _runner.forward_order_event
    forward_trade_event = _runner.forward_trade_event
    init_app = _runner.init_app
    _reset_runner_app = _runner.reset_app
    sync_positions_app = _runner.sync_positions_app
    tick_app = _runner.tick_app
    BigQmtRuntimeAdapter = _runtime_bigqmt.BigQmtRuntimeAdapter
else:
    from bigqmt_signal_trader.adapter_factory import build_app as _default_build_app
    from bigqmt_signal_trader.runner import (
        forward_order_event,
        forward_trade_event,
        init_app,
        reset_app as _reset_runner_app,
        sync_positions_app,
        tick_app,
    )
    from bigqmt_signal_trader.runtime_bigqmt import BigQmtRuntimeAdapter
    from bigqmt_signal_trader import order_watch as _order_watch


# The order watch table (issue #164) lives at module level: created at module
# load, written from the C++ callback thread, read from the adjust thread.
_order_watch_table = _order_watch.OrderWatchTable()


def _note_order_watch(order_info):
    """Learn remark->sysid and sysid->status from a raw QMT orderInfo."""
    try:
        _order_watch_table.note(
            _exec_events.normalize_order_event(order_info, ""))
    except Exception:
        pass


# exec_events is loaded here, at module load, and never from inside the
# order/deal callback. QMT runs those callbacks on a C++ thread entered via
# PyGILState_Ensure; the first exec of a not-yet-imported module on such a
# thread fails down in the C layer WITHOUT setting a Python exception, which
# surfaces as SystemError "error return without exception set" (issue #76,
# live repro 2026-08-27). Modules already in sys.modules resolve fine there --
# which is why this only bites deployments where the init-time reload could not
# preload it, i.e. the single-file QMT sandbox build.
def _import_exec_events():
    # In the QMT sandbox a package-level "from bigqmt_signal_trader import x"
    # goes through the C-level __import__ and fails the same way; the local
    # loader that already serves the adapter modules does not.
    if _load_bridge_module is not None:
        return _load_bridge_module("bigqmt_signal_trader.exec_events")
    from bigqmt_signal_trader import exec_events

    return exec_events


def _load_exec_events():
    try:
        return _import_exec_events()
    except Exception:
        direct_error = _traceback.format_exc()
    # Only reached when the direct load failed. A plain threading.Thread always
    # has a full Python thread state, so the exec that just failed succeeds
    # there; the import lock makes handing the result back safe.
    holder = {}

    def _target():
        try:
            holder["module"] = _import_exec_events()
        except Exception:
            pass

    try:
        worker = threading.Thread(target=_target)
        worker.start()
        worker.join()
    except Exception:
        pass
    if holder.get("module") is not None:
        return holder["module"]
    print(
        "[bigqmt_signal_trader] exec_events load failed; exec-event push is "
        "disabled for this run:\n%s" % direct_error
    )
    return None


_exec_events = _load_exec_events()


_app_factory = None
_account_id = ""
_config = {}
_qmt_api = {}
_adjust_logged = False
# QMT 调了多少次 credit_account_callback。0 说明 QMT 压根没回调，
# 大于 0 但 handlers 里没数据说明是转交那一步断了（#202）。
_credit_callback_fired = 0
_rpc_service = None
_quote_subscription_service = None  # (QuoteSubscriptionManager, QuotePushChannel)
_exec_event_redis_client = None  # reused; building a new client per trade callback leaks
_scheduled_adjust = False
# Latency tuning / diagnostics (server side, in the Big QMT process).
#  - switch interval: hand the GIL to the background RPC thread ~5x more often
#    than the 5ms default so it is not starved as long during Python contention.
#  - GIL probe: a heartbeat thread that measures how long the interpreter was
#    unable to run it (i.e. the process was stalled), independent of any request.
_GIL_SWITCH_INTERVAL = 0.001
_LATENCY_PROBE_ENABLED = False
_LATENCY_PROBE_THRESHOLD_MS = 50.0
_latency_probe_started = False
_last_full_tick_refresh_at = 0.0
_last_full_tick_market_refresh_at = 0.0
# Observed adjust cadence, so a mis-scheduled run_time (e.g. clamped to bar
# cadence) is visible in the logs instead of silently costing latency.
_adjust_tick_stats = {"last_ts": 0.0, "count": 0, "window_start": 0.0, "sum": 0.0, "min": 0.0, "max": 0.0}


def set_app_factory(factory):
    global _app_factory
    _app_factory = factory


def set_account_id(account_id):
    global _account_id
    _account_id = str(account_id or "")


def configure(**kwargs):
    _config.update(kwargs)


def bind_qmt_api(passorder_func=None, cancel_func=None, get_trade_detail_data_func=None,
                 extra_funcs=None):
    if passorder_func is not None:
        _qmt_api["passorder"] = passorder_func
    if cancel_func is not None:
        _qmt_api["cancel"] = cancel_func
    if get_trade_detail_data_func is not None:
        _qmt_api["get_trade_detail_data"] = get_trade_detail_data_func
    # 捕获 QMT 运行时注入的额外全局函数（融资融券查询、IPO、期权持仓等）。
    # 这些函数和 passorder 一样由 Big QMT 进程在运行时注入到全局命名空间，
    # 不在 _PyContextInfo.py 桩里，需在 DRYRUN 入口捕获后传入。
    if extra_funcs:
        for name, func in extra_funcs.items():
            if func is not None:
                _qmt_api[name] = func


# A reload asked for over RPC. Deferred to the adjust tick rather than done in
# the handler, because reset_app() stops the very RPC service that is answering
# the request -- the reply has to be sent first.
# not_before: the reply to reload_deployment has to reach the client before the
# transport it would travel on is torn down. Longer than the ZMQ ROUTER's
# 1s RCVTIMEO, because that is how long its thread can sit in recv_multipart
# before it loops back to _drain_response_queue and actually sends the reply.
# _wait_for_responses_to_flush is the real guarantee; this is the floor.
_RELOAD_GRACE_SECONDS = 1.5
_RELOAD_FLUSH_TIMEOUT_SECONDS = 5.0
_reload_request = {"pending": False, "requested_at": 0.0, "not_before": 0.0,
                   "by": ""}
_reload_result = {}


def request_reload(reason=""):
    """Schedule a package reload for the next adjust tick.

    What it refreshes: everything under bigqmt_signal_trader/, by purging it
    from sys.modules and re-running init(). That covers the adapters, the RPC
    handlers, the models and the transports -- where nearly all changes land.

    What it CANNOT refresh, and no amount of importlib will: this file and the
    BIGQMT_REDIS_DRYRUN entry. QMT execs those itself, and a module cannot
    reload the module it is running in. Those still need a strategy restart.

    Deliberately explicit: reloading a live trading process is not free. QMT's
    order/deal callbacks run on a C++ thread, and the first exec of a
    not-yet-imported module there fails in the C layer without setting a Python
    exception (SystemError: error return without exception set). The reload runs
    on the adjust thread and the callback path holds its own reference to
    exec_events from module load, so a callback landing mid-reload keeps using
    the old module rather than importing anything -- but the window is real,
    which is why this never fires on its own.
    """
    _reload_request["pending"] = True
    _reload_request["requested_at"] = time.time()
    _reload_request["not_before"] = time.time() + _RELOAD_GRACE_SECONDS
    _reload_request["by"] = str(reason or "")
    return {
        "scheduled": True,
        "note": "reload runs on the next adjust tick; poll get_deployment_info "
                "or reload_status to see the result",
        "version_before": _package_version(),
    }


def reload_status():
    """The outcome of the last reload, or what is still pending."""
    status = dict(_reload_result)
    status["pending"] = bool(_reload_request["pending"])
    status["requested_at"] = _reload_request["requested_at"]
    return status


def _package_version():
    try:
        if _load_bridge_module is not None:
            module = _load_bridge_module("bigqmt_signal_trader.version")
        else:
            from bigqmt_signal_trader import version as module
        return str(getattr(module, "__version__", ""))
    except Exception:
        return ""


def _purge_package_modules():
    """Drop every bigqmt_signal_trader module so the next import reads source.

    Purging beats importlib.reload here: reload has to run in dependency order
    (order_bigqmt does `from ..models import OrderSnapshot` at import time, so
    reloading models after it leaves the old class bound), and getting that
    order wrong fails silently. A purge has no order.
    """
    names = [name for name in list(sys.modules)
             if name == "bigqmt_signal_trader"
             or name.startswith("bigqmt_signal_trader.")]
    for name in names:
        sys.modules.pop(name, None)
    return sorted(names)


def _rebind_module_level_imports():
    """Re-point the names this module bound at import time.

    Purging sys.modules does nothing for references already held here --
    _default_build_app, the runner functions, BigQmtRuntimeAdapter and
    _exec_events would all keep pointing at the old objects, and the reload
    would look like it worked while changing nothing.
    """
    global _adapter_factory, _runner, _runtime_bigqmt, _default_build_app
    global forward_order_event, forward_trade_event, init_app, _reset_runner_app
    global sync_positions_app, tick_app, BigQmtRuntimeAdapter, _exec_events

    if _load_bridge_module is not None:
        _adapter_factory = _load_bridge_module("bigqmt_signal_trader.adapter_factory")
        _runner = _load_bridge_module("bigqmt_signal_trader.runner")
        _runtime_bigqmt = _load_bridge_module("bigqmt_signal_trader.runtime_bigqmt")
        _default_build_app = _adapter_factory.build_app
        forward_order_event = _runner.forward_order_event
        forward_trade_event = _runner.forward_trade_event
        init_app = _runner.init_app
        _reset_runner_app = _runner.reset_app
        sync_positions_app = _runner.sync_positions_app
        tick_app = _runner.tick_app
        BigQmtRuntimeAdapter = _runtime_bigqmt.BigQmtRuntimeAdapter
    else:
        from bigqmt_signal_trader.adapter_factory import build_app as _bp
        from bigqmt_signal_trader import runner as _rn
        from bigqmt_signal_trader.runtime_bigqmt import BigQmtRuntimeAdapter as _ra

        _default_build_app = _bp
        forward_order_event = _rn.forward_order_event
        forward_trade_event = _rn.forward_trade_event
        init_app = _rn.init_app
        _reset_runner_app = _rn.reset_app
        sync_positions_app = _rn.sync_positions_app
        tick_app = _rn.tick_app
        BigQmtRuntimeAdapter = _ra
    _exec_events = _load_exec_events()


def _pending_response_count():
    """How many RPC replies the transport has queued but not yet sent."""
    transport = getattr(_rpc_service, "_transport", None) if _rpc_service else None
    pending = getattr(transport, "_response_queue", None)
    if pending is None:
        return 0
    try:
        return pending.qsize()
    except Exception:
        return 0


def _wait_for_responses_to_flush(timeout_seconds=None):
    """Let the transport send what is queued before reset_app() tears it down.

    The reply to reload_deployment is put on the ZMQ transport's response queue
    by the handler, and the ROUTER thread sends it at the top of its next loop
    -- a loop that can be sitting in recv_multipart for up to RCVTIMEO (1s),
    and that needs the GIL this thread is holding. Sleeping yields both.

    Sending from here instead is not an option: the ROUTER socket belongs to
    that thread and ZMQ sockets are not thread-safe -- the same reason it closes
    its own socket in its finally block.

    Without this the reload SUCCEEDS and the caller still sees a timeout, which
    is indistinguishable from a reload that killed the bridge. That is what the
    first two live attempts did: "responded method=reload_deployment ok=True"
    and "[bigqmt_reload] ok purged=28" in the terminal, TransportTimeout at the
    client.
    """
    if timeout_seconds is None:
        timeout_seconds = _RELOAD_FLUSH_TIMEOUT_SECONDS
    deadline = time.time() + max(0.0, timeout_seconds)
    while time.time() < deadline:
        if _pending_response_count() == 0:
            # qsize() drops when the sender dequeues, which is just BEFORE the
            # send. One more yield so that send completes.
            time.sleep(0.2)
            return True
        time.sleep(0.05)
    return False


def _perform_reload(context_info):
    """Purge, re-import, re-init. Runs on the adjust thread."""
    global _reload_result
    _reload_request["pending"] = False
    started = time.time()
    before = _package_version()
    result = {"ok": False, "version_before": before, "version_after": "",
              "modules_purged": 0, "seconds": 0.0, "error": "",
              "replies_flushed": False, "by": _reload_request["by"]}
    try:
        result["replies_flushed"] = _wait_for_responses_to_flush()
        if not result["replies_flushed"]:
            print("[bigqmt_reload] WARNING: %d reply/replies still queued after "
                  "%.0fs; the caller may see a timeout even though the reload "
                  "runs" % (_pending_response_count(),
                            _RELOAD_FLUSH_TIMEOUT_SECONDS))
        reset_app()
        result["modules_purged"] = len(_purge_package_modules())
        _rebind_module_level_imports()
        init(context_info)
        result["version_after"] = _package_version()
        result["ok"] = True
    except Exception as exc:
        result["error"] = "%s: %s" % (type(exc).__name__, exc)
        _log_err("reload", "reload failed: %s\n%s"
                          % (exc, _traceback.format_exc()))
    result["seconds"] = round(time.time() - started, 3)
    _reload_result = result
    print("[bigqmt_reload] %s purged=%d %s -> %s in %.2fs%s"
          % ("ok" if result["ok"] else "FAILED", result["modules_purged"],
             before or "?", result["version_after"] or "?", result["seconds"],
             "" if result["ok"] else "  (" + result["error"] + ") -- RESTART THE STRATEGY"))
    return result


def reset_app():
    global _adjust_logged, _rpc_service, _scheduled_adjust, _last_full_tick_refresh_at, _last_full_tick_market_refresh_at
    global _quote_subscription_service, _exec_event_redis_client
    _adjust_logged = False
    _scheduled_adjust = False
    _last_full_tick_refresh_at = 0.0
    _last_full_tick_market_refresh_at = 0.0
    _adjust_tick_stats.update({"last_ts": 0.0, "count": 0, "window_start": 0.0, "sum": 0.0, "min": 0.0, "max": 0.0})
    if _rpc_service is not None:
        try:
            _rpc_service.stop()
        except Exception:
            pass
    _rpc_service = None
    # Stop the quote-push channel + unsubscribe big-QMT whole-quote subs. Without
    # this a strategy re-run leaks the PUB port (next run's start_publisher hits
    # EADDRINUSE, silently dropping quotes forever) and leaves stale QMT
    # subscriptions firing into dead manager objects.
    if _quote_subscription_service is not None:
        try:
            manager, channel = _quote_subscription_service
            for key in list(getattr(manager, "_combos", {}).keys()):
                combo = manager._combos.get(key)
                if combo is not None:
                    try:
                        manager._close_source(combo.handle)
                    except Exception:
                        pass
            manager._combos.clear()
            manager._sub_index.clear()
            channel.stop()
        except Exception:
            pass
    _quote_subscription_service = None
    # Drop the reused exec-event redis client so the next run rebuilds it fresh.
    _exec_event_redis_client = None
    _reset_runner_app()


def _resolve_runtime_name(name):
    if name in _qmt_api:
        return _qmt_api[name]
    if name in globals():
        return globals()[name]
    try:
        import builtins
        return getattr(builtins, name)
    except Exception:
        return None


def _detect_account_id(context_info=None):
    if _account_id:
        return _account_id
    try:
        import importlib
        import bigqmt_signal_trader_local_config as _local_config

        _local_config = importlib.reload(_local_config)
        value = str(
            getattr(_local_config, "BIGQMT_ACCOUNT_ID", "")
            or (getattr(_local_config, "BIGQMT_REDIS_CONFIG", {}) or {}).get("account_id")
            or ""
        )
        if value:
            return value
    except Exception:
        pass
    for name in ("account", "account_id", "accountID"):
        value = _resolve_runtime_name(name)
        if value:
            return str(value)
    if context_info is not None:
        for name in ("account", "account_id", "accountID", "m_strAccountID"):
            value = getattr(context_info, name, None)
            if value:
                return str(value)
        for name in ("get_account", "get_account_id", "getAccountID"):
            func = getattr(context_info, name, None)
            if callable(func):
                try:
                    value = func()
                except Exception:
                    value = None
                if value:
                    return str(value)
    return ""


# Official Big QMT runtime-injected global functions (like passorder) that we
# expose over RPC. These are not ContextInfo methods and not in the IDE stub;
# QMT injects them into the process global namespace at startup. We resolve
# them lazily so the module imports cleanly outside QMT (tests/dev).
_EXTRA_QMT_GLOBAL_FUNCS = (
    "get_history_trade_detail_data",  # 历史成交明细
    "get_value_by_order_id",          # 按 order_id 查委托详情
    "get_last_order_id",              # 最近委托号
    "get_ipo_data",                   # 新股数据
    "get_new_purchase_limit",         # 新股申购额度
    "get_assure_contract",            # 融资标的（担保品）合约
    "get_enable_short_contract",      # 融券标的合约
    "get_unclosed_compacts",          # 未平仓合约（负债）
    "get_closed_compacts",            # 已平仓合约
    "get_debt_contract",              # 负债合约
    # 信用账户明细，查柜台的那条路。异步：结果只从 credit_account_callback
    # 出来，见下面的 credit_account_callback 和 #202。
    "query_credit_account",
    "get_option_subject_position",    # 期权标的持仓
    "get_comb_option",                # 组合期权
    "get_hkt_exchange_rate",          # 港股通汇率
    # download_history_data / download_history_data2 are global functions
    # injected by QMT (not ContextInfo methods), same as passorder. They
    # must be captured here so the adapter can call them. Issue #32.
    "download_history_data",
    "download_history_data2",
    # Some QMT builds expose only down_history_data (single stock, same 4-arg
    # signature). Issue #54: without it the download RPC is a silent no-op and
    # reads only ever return the latest day.
    "down_history_data",
)

# The full set of QMT-injected global function names (the three trade entry
# points plus the extras above). Mount sites (the RPC runtime's direct mount)
# capture whatever is callable from their own exec namespace via
# capture_qmt_injected_funcs() -- this is the single source for that list; do
# not hand-copy the names elsewhere.
_QMT_INJECTED_GLOBAL_FUNCS = (
    "passorder", "cancel", "get_trade_detail_data",
) + _EXTRA_QMT_GLOBAL_FUNCS


def capture_qmt_injected_funcs(namespace):
    """Capture QMT-injected global funcs from a mounted entry's exec namespace.

    QMT mounts an entry file by exec and injects these functions into THAT
    namespace only -- the strategy module's globals/builtins lookups cannot
    see them -- so a mounted entry must pass its own globals() here and feed
    the result to bind_qmt_api(extra_funcs=...). Non-callables are skipped,
    so a plain import namespace binds nothing.
    """
    captured = {}
    for name in _QMT_INJECTED_GLOBAL_FUNCS:
        func = (namespace or {}).get(name)
        if callable(func):
            captured[name] = func
    return captured


def _build_config():
    config = dict(_config)
    if _account_id:
        config["account_id"] = _account_id
    qmt_api = dict(config.get("qmt_api") or {})
    for name in ("passorder", "cancel", "get_trade_detail_data"):
        if qmt_api.get(name) is None:
            qmt_api[name] = _resolve_runtime_name(name)
    # 解析其余官方全局函数（存在则注入，不存在保持 None）。
    for name in _EXTRA_QMT_GLOBAL_FUNCS:
        if qmt_api.get(name) is None:
            qmt_api[name] = _resolve_runtime_name(name)
    config["qmt_api"] = qmt_api
    return config


def _build_app(context_info):
    if _app_factory is not None:
        return _app_factory(context_info)
    return _default_build_app(context_info, _build_config())


def _config_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on")


_REDIS_TRANSPORT_NAMES = ("redis", "", "default")


def _is_redis_transport(transport_name):
    return str(transport_name or "redis").lower() in _REDIS_TRANSPORT_NAMES


def _resolve_background_threads(transport_name, configured):
    """Decide whether the RPC service runs its own background receive threads.

    ``configured`` is None when the local config never set
    ``rpc_background_threads`` (the runtime only forwards the key when it was
    given explicitly) -- that case keeps the historical default: background
    receiver threads on for every transport.

    An explicit False opts into the adjust-driven drain instead: the transport
    is polled with a non-blocking recv from drain_request_queue on each adjust
    tick, and the reply is sent on the adjust thread too. That removes every
    cross-thread handoff from the round trip -- measured on the live terminal,
    each time a background thread acquires the GIL costs ~1 adjust tick
    (~100ms), which is where the round trip actually goes (#104).

    Only transports that implement a *real* drain_request_queue may honor the
    override; anything else keeps the receiver thread it needs to be polled by.
    Redis honors either value (blocking brpop path AND an adjust lpop drain).

    The list used to be a hardcoded ("zmq", "mysql") and a new transport had to
    remember to add itself -- pipe did not, so an explicit False was silently
    forced back to True and the drain never ran (the log said
    ``background_threads=True`` while the config said False). Ask the transport
    class instead: it is the thing that knows.
    """
    normalized = str(transport_name or "redis").lower()
    if _is_redis_transport(normalized):
        return bool(configured)
    if configured is None:
        return True
    if _transport_can_drain(normalized):
        return bool(configured)
    return True


def _transport_can_drain(transport_name):
    """这个传输自己实现了 drain_request_queue 吗。

    问传输类本身，而不是维护一张名单 —— 名单会漏，pipe 就漏过一次。
    基类的实现返回 0（占位），所以只认子类自己覆盖过的。
    """
    try:
        from bigqmt_signal_trader.transports.factory import transport_supports_drain
    except Exception:
        # 导入不到就退回历史名单，别让一个 import 失败改变行为
        return str(transport_name or "").lower() in ("zmq", "mysql")
    return transport_supports_drain(transport_name)


def _build_quote_subscription_service(context_info, config, transport_name, account_id, redis_client):
    """Assemble the server-side whole-quote push service (manager + channel).

    Returns ``(manager, channel)`` or ``None`` when disabled. The channel publisher
    is started in ``_start_rpc_service`` once the RPC service is up; the manager's
    reaper is fed from ``_drain_rpc_service``.
    """
    quote_config = dict(config.get("quote_push") or {})
    enabled = _config_bool(quote_config.get("enabled"), True)
    if not enabled:
        return None
    if _load_bridge_module is not None:
        _qsm = _load_bridge_module("bigqmt_signal_trader.quote_subscription_manager")
    else:
        from bigqmt_signal_trader import quote_subscription_manager as _qsm
    import importlib

    _qsm = importlib.reload(_qsm)
    heartbeat_timeout = float(quote_config.get("heartbeat_timeout_seconds", 30.0))
    zmq_bind_address = quote_config.get("zmq_bind_address")
    return _qsm.build_quote_subscription_service(
        context_info,
        transport_name=transport_name,
        account_id=account_id,
        redis_client=redis_client,
        zmq_bind_address=zmq_bind_address,
        enabled=True,
        heartbeat_timeout_seconds=heartbeat_timeout,
    )


def _build_rpc_service(context_info, app, config):
    rpc_config = dict(config.get("rpc") or {})
    enabled = _config_bool(config.get("enable_rpc"), False) or _config_bool(rpc_config.get("enabled"), False)
    if not enabled:
        return None
    transport_name = str(rpc_config.get("transport") or "redis").lower()
    redis_transport = transport_name in ("redis", "", "default")

    import importlib
    if _load_bridge_module is not None:
        _market_bigqmt = _load_bridge_module("bigqmt_signal_trader.adapters.market_bigqmt")
        _position_bigqmt = _load_bridge_module("bigqmt_signal_trader.adapters.position_bigqmt")
        _redis_rpc = _load_bridge_module("bigqmt_signal_trader.redis_rpc")
        BigQmtRpcHandlers = _redis_rpc.BigQmtRpcHandlers
        RedisPubSubRpcService = _redis_rpc.RedisPubSubRpcService
    else:
        from bigqmt_signal_trader.adapters import market_bigqmt as _market_bigqmt
        from bigqmt_signal_trader.adapters import position_bigqmt as _position_bigqmt
        from bigqmt_signal_trader.redis_rpc import BigQmtRpcHandlers, RedisPubSubRpcService

    # QMT keeps strategy modules in the same process between editor reruns.
    # Reload adapters here so synced local package fixes take effect immediately.
    _market_bigqmt = importlib.reload(_market_bigqmt)
    _position_bigqmt = importlib.reload(_position_bigqmt)
    # Reload the lazily-imported helper modules too, so edits to them take effect on
    # an editor rerun (QMT persists sys.modules across reruns; a plain lazy import
    # would otherwise keep the stale cached version).
    for _mod_name in (
        "bigqmt_signal_trader.full_tick_cache",
        "bigqmt_signal_trader.download_jobs",
        "bigqmt_signal_trader.exec_events",
    ):
        try:
            importlib.reload(importlib.import_module(_mod_name))
        except Exception as _reload_err:
            print("[bigqmt_rpc] reload %s failed: %s" % (_mod_name, _reload_err))
    BigQmtMarketDataProvider = _market_bigqmt.BigQmtMarketDataProvider
    BigQmtPositionProvider = _position_bigqmt.BigQmtPositionProvider

    qmt_api = dict(config.get("qmt_api") or {})
    redis_client = None
    response_redis_client = None
    if redis_transport:
        if _load_bridge_module is not None:
            _redis_common = _load_bridge_module("bigqmt_signal_trader.adapters.redis_common")
        else:
            from bigqmt_signal_trader.adapters import redis_common as _redis_common

        _redis_common = importlib.reload(_redis_common)
        redis_config = dict(config.get("redis") or {})
        redis_config.update(dict(rpc_config.get("redis") or {}))
        listen_redis_config = dict(redis_config)
        # Never use socket_timeout=None on the listen client: the same client also
        # serves the adjust-thread LPOP drain, and a None timeout makes a hung
        # (not refused) redis block the QMT main thread forever. brpop's own 1s
        # command timeout is unaffected by a bounded socket timeout.
        if listen_redis_config.get("socket_timeout") in (None, ""):
            listen_redis_config["socket_timeout"] = 10
        redis_client = rpc_config.get("redis_client") or config.get("redis_client") or _redis_common.build_redis_client(listen_redis_config)
        response_redis_client = (
            rpc_config.get("response_redis_client")
            or config.get("response_redis_client")
            or _redis_common.build_redis_client(redis_config)
        )
    account_id = str(rpc_config.get("account_id") or config.get("account_id") or _account_id or "")
    if not account_id:
        print("[bigqmt_rpc] disabled: account_id is empty")
        return None
    allow_order_methods = _config_bool(rpc_config.get("allow_order_methods"), False)
    global _quote_subscription_service
    _quote_subscription_service = _build_quote_subscription_service(
        context_info, config, transport_name, account_id, redis_client
    )
    quote_manager = (
        _quote_subscription_service[0] if _quote_subscription_service is not None else None
    )
    handlers = BigQmtRpcHandlers(
        account_id=account_id,
        market_data=BigQmtMarketDataProvider(context_info, qmt_api=qmt_api),
        position_provider=BigQmtPositionProvider(
            get_trade_detail_data_func=qmt_api.get("get_trade_detail_data"),
            account_type=config.get("account_type", "STOCK"),
        ),
        order_gateway=getattr(app, "order_gateway", None),
        position_sync_sink=getattr(app, "position_sync_sink", None),
        allow_order_methods=allow_order_methods,
        allowed_methods=rpc_config.get("allowed_methods"),
        qmt_api=qmt_api,
        # Async settlement keeps passorder off the adjust thread's critical
        # path; set rpc_settle_orders_inline=True only for a runtime with no
        # adjust drain to retry on.
        settle_orders_inline=_config_bool(rpc_config.get("settle_orders_inline"), False),
        order_settle_timeout_seconds=float(rpc_config.get("order_settle_timeout_seconds", 3.0)),
        quote_subscription_manager=quote_manager,
        # .get(key) not .get(key, default): "" is a real answer here (leave
        # 报单来源 blank), and a default would swallow it -- issue #154.
        default_strategy_name=rpc_config.get("default_strategy_name"),
    )
    # Neither of these may depend on the TRANSPORT. Only a redis transport
    # builds the two clients above, so on zmq both were None -- and each had a
    # working counterpart on the other side of that None:
    #
    #   download jobs   _pump_download_jobs advances queued jobs on every
    #                   adjust tick and takes its client from _exec_event_redis,
    #                   so on zmq the worker ran while submit / get_status /
    #                   wait answered "download jobs require a Redis client".
    #                   The worker was running and the door was locked.
    #   order identity  orders were never remembered at submit time, so a query
    #                   could never put the strategy name back (issue #133).
    #
    # _exec_event_redis is the helper that already covers this case ("only
    # builds a client when _rpc_service has none, which is the zmq-transport
    # case") and caches it -- its docstring says what building one per call
    # cost. No redis configured at all leaves both None, and both stores treat
    # that as "feature off", never as an error.
    _store_redis = response_redis_client or redis_client or _exec_event_redis(config)
    handlers.download_job_redis_client = _store_redis
    handlers.order_identity_redis_client = _store_redis
    # Settlement reads the callback-fed watch table first (issue #164).
    handlers.order_watch_table = _order_watch_table
    # Whether _pump_download_jobs will actually run queued jobs. The submit RPC
    # needs it: with the redis client now wired on every transport, a submit
    # would otherwise be accepted into a queue that nothing drains.
    # Lets reload_deployment re-import the package and re-run init without a
    # strategy restart. The handlers cannot reach this module's own functions
    # any other way.
    handlers.reload_hook = request_reload
    handlers.reload_status_hook = reload_status
    handlers.download_jobs_enabled = _config_bool(
        (config.get("download_jobs") or {}).get("enabled"), False)
    handlers.download_job_chunk_size = int((config.get("download_jobs") or {}).get("chunk_size") or 10)
    handlers.download_job_ttl_seconds = int((config.get("download_jobs") or {}).get("job_ttl_seconds") or 3600)
    process_in_listener = _config_bool(rpc_config.get("process_in_listener"), True)
    listener_methods = rpc_config.get("listener_methods") or ("*",)
    # None when the runtime did not forward the key (local config never set
    # rpc_background_threads): the resolver then keeps the historical default.
    configured_bg = rpc_config.get("background_threads")
    if configured_bg is not None:
        configured_bg = _config_bool(configured_bg, False)
    background_threads = _resolve_background_threads(transport_name, configured_bg)
    if background_threads and not configured_bg:
        print("[bigqmt_rpc] transport=%s -> background_threads auto-enabled" % transport_name)
    # Build the transport. Redis is the default and reuses the existing clients/
    # templates (zero behavior change). zmq/mysql/shm go through the factory and
    # bypass the Redis clients entirely.
    transport = None
    if transport_name not in ("redis", "", "default"):
        if _load_bridge_module is not None:
            build_transport = _load_bridge_module("bigqmt_signal_trader.transports.factory").build_transport
        else:
            from bigqmt_signal_trader.transports.factory import build_transport

        factory_config = dict(rpc_config)
        factory_config["account_id"] = account_id
        factory_config["print_prefix"] = "[bigqmt_rpc]"
        transport = build_transport(transport_name, factory_config, account_id=account_id, print_prefix="[bigqmt_rpc]")
    print(
        "[bigqmt_rpc] transport=%s mode process_in_listener=%s listener_methods=%s allow_order_methods=%s background_threads=%s"
        % (transport_name, process_in_listener, listener_methods, allow_order_methods, background_threads)
    )
    service = RedisPubSubRpcService(
        redis_client=redis_client,
        response_redis_client=response_redis_client,
        handlers=handlers,
        account_id=account_id,
        request_channel_template=rpc_config.get("request_channel_template", "bigqmt:rpc:req:{account_id}"),
        response_channel_template=rpc_config.get("response_channel_template", "bigqmt:rpc:resp:{account_id}:{request_id}"),
        response_key_template=rpc_config.get("response_key_template", "bigqmt:rpc:resp:{account_id}:{request_id}"),
        response_ttl_seconds=int(rpc_config.get("response_ttl_seconds", 60)),
        max_queue_size=int(rpc_config.get("max_queue_size", 200)),
        process_in_listener=process_in_listener,
        listener_methods=listener_methods,
        background_threads=background_threads,
        debug_log_limit=int(rpc_config.get("debug_log_limit", 5)),
        transport=transport,
    )
    # Multi-account: when BIGQMT_ACCOUNT_TYPE_MAP has multiple entries,
    # build one RPC service per account sharing the same handlers.
    try:
        from bigqmt_signal_trader.multi_account import build_multi_account_rpc_service
        multi_service = build_multi_account_rpc_service(
            context_info, app, config,
            # The single-service builder: reuse everything we just built
            # as the primary, and let build_multi_account_rpc_service
            # create secondary services if the map has more entries.
            lambda ctx, app, cfg: service if ctx is context_info else None,
        )
        if multi_service is not service:
            # Multi-account mode: wrapped in MultiAccountRpcServiceManager
            return multi_service
    except Exception as _ma_err:
        print("[bigqmt_rpc] multi_account check failed (single-account fallback): %s" % _ma_err)
    return service


def _start_rpc_service(context_info, app, config):
    global _rpc_service
    if _rpc_service is not None:
        return _rpc_service
    _rpc_service = _build_rpc_service(context_info, app, config)
    if _rpc_service is not None:
        _rpc_service.start()
        if _quote_subscription_service is not None:
            try:
                _quote_subscription_service[1].start_publisher()
                print("[bigqmt_quote_push] publisher started transport=%s"
                      % str(dict(config.get("rpc") or {}).get("transport") or "redis"))
            except Exception as exc:
                _log_err("quote_push", "publisher start failed: %s" % exc)
    return _rpc_service


def _drain_rpc_service(config):
    if _rpc_service is None:
        return 0
    rpc_config = dict(config.get("rpc") or {})
    max_items = int(rpc_config.get("drain_max_items", 20))
    processed = 0
    if hasattr(_rpc_service, "drain_request_queue"):
        processed += _rpc_service.drain_request_queue(max_items=max_items)
    processed += _rpc_service.drain_pending(max_items=max_items)
    if _quote_subscription_service is not None:
        try:
            _quote_subscription_service[0].reap_expired()
        except Exception as exc:
            _log_err("quote_push", "reap failed: %s" % exc)
    return processed


def _refresh_full_tick_cache(context_info, config):
    global _last_full_tick_refresh_at, _last_full_tick_market_refresh_at
    cache_config = dict(config.get("full_tick_cache") or {})
    if not _config_bool(cache_config.get("enabled"), True):
        return 0
    account_id = str(cache_config.get("account_id") or config.get("account_id") or _account_id or "")
    if not account_id:
        return 0
    # Symbol-list demands are cheap and refresh on the fast interval; whole-market
    # (SH/SZ/BJ/HK) demands are heavy and refresh on a slower cadence so a ~50k row
    # snapshot is not pulled every fast tick.
    symbol_interval = float(cache_config.get("refresh_interval_seconds") or 0.5)
    market_interval = float(cache_config.get("market_refresh_interval_seconds") or 3.0)
    max_wall = cache_config.get("refresh_max_wall_seconds")
    max_wall = float(max_wall) if max_wall else None
    now = time.time()
    do_symbol = now - _last_full_tick_refresh_at >= symbol_interval
    do_market = now - _last_full_tick_market_refresh_at >= market_interval
    if not do_symbol and not do_market:
        return 0
    redis_client = getattr(_rpc_service, "redis", None)
    if redis_client is None:
        redis_config = dict(config.get("redis") or {})
        if not redis_config:
            return 0
        from bigqmt_signal_trader.adapters.redis_common import build_redis_client

        redis_client = build_redis_client(redis_config)
    demand_ttl = float(cache_config.get("demand_ttl_seconds") or 10)
    cache_ttl = float(cache_config.get("cache_ttl_seconds") or 10)
    max_requests = int(cache_config.get("max_requests") or 8)
    from bigqmt_signal_trader.full_tick_cache import refresh_full_tick_cache

    refreshed = 0
    # Symbol and market refreshes are throttled independently, so each advances its
    # own timestamp and runs in its own try: a symbol-refresh error must not starve
    # the market refresh nor leave it retrying every fast tick (unthrottled).
    if do_symbol:
        _last_full_tick_refresh_at = now
        try:
            refreshed += refresh_full_tick_cache(
                redis_client,
                context_info,
                account_id,
                demand_ttl_seconds=demand_ttl,
                cache_ttl_seconds=cache_ttl,
                max_requests=max_requests,
                kind="symbol",
                max_wall_seconds=max_wall,
            )
        except Exception as exc:
            _log_err("full_tick_cache", "symbol refresh failed: %s" % exc)
    if do_market:
        _last_full_tick_market_refresh_at = now
        try:
            refreshed += refresh_full_tick_cache(
                redis_client,
                context_info,
                account_id,
                demand_ttl_seconds=demand_ttl,
                cache_ttl_seconds=cache_ttl,
                max_requests=max_requests,
                kind="market",
                max_wall_seconds=max_wall,
            )
        except Exception as exc:
            _log_err("full_tick_cache", "market refresh failed: %s" % exc)
    return refreshed


def _schedule_adjust_if_needed(context_info, config):
    global _scheduled_adjust
    if _scheduled_adjust:
        return
    if not _config_bool(config.get("schedule_adjust"), False):
        return
    interval = str(config.get("schedule_adjust_interval") or "3000nMilliSecond")
    if not hasattr(context_info, "run_time"):
        print(
            "[bigqmt_signal_trader] WARNING: ContextInfo.run_time unavailable; RPC drain "
            "falls back to bar cadence (requested interval=%s not applied)" % interval
        )
        return
    start_time = (datetime.datetime.now() + datetime.timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        context_info.run_time("adjust", interval, start_time)
        _scheduled_adjust = True
        print(
            "[bigqmt_signal_trader] scheduled adjust interval=%s "
            "(verify observed cadence in the 'adjust cadence' log line)" % interval
        )
    except Exception as exc:
        print(
            "[bigqmt_signal_trader] WARNING: schedule adjust failed (%s); RPC drain falls back "
            "to bar cadence, requested interval=%s not applied" % (exc, interval)
        )


# Where each adjust() call came from, and what tick_app actually costs.
# The question this answers: handlebar is documented as tick-driven in live
# trading, but the observed cadence is a flat ~50/10s -- exactly the run_time
# timer and nothing else. Splitting the counters shows whether handlebar fires
# at all once the historical replay ends, and the tick_app histogram shows what
# it would cost to let it drive the strategy body rather than just the drain.
_adjust_source_stats = {"handlebar": 0, "timer": 0, "window_start": 0.0}
# Bucket upper bounds in ms; the last bucket is everything above.
_TICK_APP_BUCKETS = (1, 5, 20, 50, 100, 250, 500, 1000, 2000)
_tick_app_hist = [0] * (len(_TICK_APP_BUCKETS) + 1)
_tick_app_max_ms = [0.0]


def _record_adjust_source(source):
    """Count adjust() calls per trigger source, logged on the cadence window."""
    stats = _adjust_source_stats
    now = time.time()
    if stats["window_start"] <= 0:
        stats["window_start"] = now
    stats[source] = stats.get(source, 0) + 1
    if now - stats["window_start"] >= 10.0:
        print(
            "[adjust_source] handlebar=%d timer=%d over %.0fs"
            % (stats["handlebar"], stats["timer"], now - stats["window_start"])
        )
        stats.update({"handlebar": 0, "timer": 0, "window_start": now})


def _record_tick_app_ms(ms):
    """Histogram of tick_app cost.

    tick_app is the expensive half of adjust() and is skipped while
    is_last_bar() is False, so the replay-time cost (~0.2ms/call) says nothing
    about what it costs at the live edge. Before letting ticks drive it we need
    the real distribution, not just the >50ms outliers the phase logger prints.
    """
    for index, bound in enumerate(_TICK_APP_BUCKETS):
        if ms <= bound:
            _tick_app_hist[index] += 1
            break
    else:
        _tick_app_hist[-1] += 1
    if ms > _tick_app_max_ms[0]:
        _tick_app_max_ms[0] = ms


def _format_tick_app_hist():
    labels = []
    previous = 0
    for index, bound in enumerate(_TICK_APP_BUCKETS):
        labels.append("%d-%dms=%d" % (previous, bound, _tick_app_hist[index]))
        previous = bound
    labels.append(">%dms=%d" % (_TICK_APP_BUCKETS[-1], _tick_app_hist[-1]))
    return " ".join(labels)


def _record_adjust_tick():
    """Track and periodically log the real interval between adjust triggers."""
    stats = _adjust_tick_stats
    now = time.time()
    last = stats["last_ts"]
    stats["last_ts"] = now
    if last <= 0:
        stats["window_start"] = now
        return
    delta = now - last
    stats["count"] += 1
    stats["sum"] += delta
    stats["min"] = delta if stats["min"] <= 0 else min(stats["min"], delta)
    stats["max"] = max(stats["max"], delta)
    if now - stats["window_start"] >= 10.0 and stats["count"] > 0:
        avg = stats["sum"] / stats["count"]
        print(
            "[bigqmt_signal_trader] adjust cadence: ticks=%d avg=%.3fs min=%.3fs max=%.3fs over %.0fs"
            % (stats["count"], avg, stats["min"], stats["max"], now - stats["window_start"])
        )
        if sum(_tick_app_hist) > 0:
            print("[tick_app_hist] %s max=%.0fms"
                  % (_format_tick_app_hist(), _tick_app_max_ms[0]))
        stats.update({"count": 0, "sum": 0.0, "min": 0.0, "max": 0.0, "window_start": now})


def _gil_probe_loop():
    """Heartbeat: sleep 5ms in a loop and measure the ACTUAL elapsed time. sleep()
    releases the GIL; if returning from it takes much longer than 5ms, the thread
    was starved -- i.e. the interpreter (this whole process) was stalled holding
    the GIL elsewhere. Summarize gaps over a 10s window so we can see how often /
    how long the process freezes, independent of any RPC request."""
    step = 0.005
    threshold = _LATENCY_PROBE_THRESHOLD_MS / 1000.0
    window_start = time.time()
    gaps = []
    while True:
        t0 = time.time()
        time.sleep(step)
        gap = time.time() - t0 - step
        if gap > threshold:
            gaps.append(gap * 1000.0)
        now = time.time()
        if now - window_start >= 10.0:
            if gaps:
                gaps.sort()
                print(
                    "[gil_probe] over %.0fs: %d stalls>%.0fms  max=%.0fms p50=%.0fms total=%.0fms"
                    % (now - window_start, len(gaps), _LATENCY_PROBE_THRESHOLD_MS,
                       gaps[-1], gaps[len(gaps) // 2], sum(gaps))
                )
            else:
                print("[gil_probe] over %.0fs: 0 stalls>%.0fms (clean)" % (now - window_start, _LATENCY_PROBE_THRESHOLD_MS))
            window_start = now
            gaps = []


def _start_latency_probe():
    global _latency_probe_started
    if _latency_probe_started or not _LATENCY_PROBE_ENABLED:
        return
    _latency_probe_started = True
    t = threading.Thread(target=_gil_probe_loop, name="bigqmt-gil-probe", daemon=True)
    t.start()
    print("[gil_probe] started (threshold=%.0fms)" % _LATENCY_PROBE_THRESHOLD_MS)


def _apply_gil_tuning():
    try:
        sys.setswitchinterval(_GIL_SWITCH_INTERVAL)
        print("[bigqmt_signal_trader] gil switch interval set to %.4fs" % _GIL_SWITCH_INTERVAL)
    except Exception as exc:
        print("[bigqmt_signal_trader] setswitchinterval failed: %s" % exc)


def init(ContextInfo):
    detected_account_id = _detect_account_id(ContextInfo)
    if detected_account_id and not _account_id:
        set_account_id(detected_account_id)
    if _account_id and hasattr(ContextInfo, "set_account"):
        try:
            ContextInfo.set_account(_account_id)
        except Exception as exc:
            _log_startup_error("set_account failed: %s" % exc)
    _apply_gil_tuning()
    _start_latency_probe()
    config = _build_config()
    runtime = BigQmtRuntimeAdapter(ContextInfo)
    app = None
    try:
        app = init_app(runtime, _build_app)
    except Exception as exc:
        # A build failure (e.g. missing redis package) must not kill the strategy
        # before the RPC service even starts — log it and continue without app.
        _log_startup_error("init_app/build_app failed: %s" % exc)
    try:
        _start_rpc_service(ContextInfo, app, config)
    except Exception as exc:
        # e.g. zmq port conflict -> TransportError. Log it so the user sees why
        # the RPC service didn't start instead of QMT silently exiting.
        _log_startup_error("rpc service start failed: %s" % exc)
    try:
        _schedule_adjust_if_needed(ContextInfo, config)
    except Exception as exc:
        _log_startup_error("schedule adjust failed: %s" % exc)
    print("[bigqmt_signal_trader] init ok")

    # 启动时自动诊断：检测服务状态 + 关键函数绑定，方便发现问题
    _diag_startup(ContextInfo, config)
    try:
        _start_context_warmup(ContextInfo, config)
    except Exception as exc:
        _log_startup_error("context warmup failed to start: %s" % exc)
    return app


def _log_startup_error(message):
    """Log a startup error to file AND the QMT panel; never raises."""
    try:
        from bigqmt_signal_trader.logging_setup import get_logger
        get_logger("init").error("%s", message)
    except Exception:
        pass
    try:
        print("[bigqmt_signal_trader] INIT ERROR: %s" % message)
    except Exception:
        pass


def _log_err(tag, message):
    """Log a runtime error to the rotating file AND the QMT panel; never raises.

    Centralizes error visibility: the QMT output panel scrolls away, but the
    log file (logs/bigqmt.log, kept 7 days) survives restarts and crashes.
    """
    try:
        from bigqmt_signal_trader.logging_setup import get_logger
        get_logger(tag).error("%s", message)
    except Exception:
        pass
    try:
        print("[bigqmt_signal_trader] %s: %s" % (tag, message))
    except Exception:
        pass


def _diag_bar_driver(context_info):
    """Report what drives handlebar: the strategy's own symbol, period, and
    whether any quote subscription exists.

    handlebar is documented as firing per incoming tick in live trading, but it
    goes quiet here once the historical replay ends. This strategy never calls
    subscribe_quote / subscribe_whole_quote / set_universe, so the leading
    suspect is that nothing is feeding it ticks. Print what QMT actually has so
    the next live session settles it instead of us guessing.
    """
    fields = (
        ("stockcode", "品种"),
        ("stock_code", "品种(alt)"),
        ("period", "周期"),
        ("do_back_test", "回测模式"),
        ("start", "起始"),
        ("end", "结束"),
    )
    parts = []
    for name, label in fields:
        try:
            value = getattr(context_info, name, None)
            if callable(value):
                value = value()
            if value not in (None, ""):
                parts.append("%s=%s" % (label, value))
        except Exception:
            continue
    print("[bigqmt_diag] bar driver: %s" % (" ".join(parts) or "<无法读取>"))

    for name in ("subscribe_quote", "subscribe_whole_quote", "set_universe", "is_last_bar"):
        print("[bigqmt_diag]   %-22s %s"
              % (name, "可用" if callable(getattr(context_info, name, None)) else "不可用"))
    try:
        print("[bigqmt_diag]   is_last_bar() 当前值    %s" % context_info.is_last_bar())
    except Exception as exc:
        print("[bigqmt_diag]   is_last_bar() 调用失败  %s" % exc)
    print("[bigqmt_diag]   本策略未订阅任何行情 -> handlebar 预计仅由历史回放驱动")


def _diag_startup(ContextInfo, config):
    """Startup diagnostics: check service status and key function bindings.

    Prints a summary to the QMT log so users can quickly see if the RPC service
    is up, which transport is active, and whether key QMT functions (passorder,
    get_trade_detail_data) are bound. Helps diagnose "service won't start" issues.
    """
    print("=" * 60)
    print("[bigqmt_diag] startup diagnostics")
    print("=" * 60)

    _diag_bar_driver(ContextInfo)

    # 1. RPC service status
    rpc_config = dict(config.get("rpc") or {})
    transport = rpc_config.get("transport", "redis")
    print("[bigqmt_diag] transport=%s" % transport)
    if _rpc_service is not None:
        print("[bigqmt_diag] rpc_service=running (type=%s)" % type(_rpc_service).__name__)
    else:
        print("[bigqmt_diag] rpc_service=NOT STARTED (check enable_rpc / errors above)")

    # 2. Key QMT function bindings
    qmt_api = dict(config.get("qmt_api") or {})
    for name in ("passorder", "cancel", "get_trade_detail_data", "down_history_data"):
        bound = qmt_api.get(name) is not None
        print("[bigqmt_diag] %s bound=%s" % (name, bound))

    # 3. Quick connectivity test (get_full_tick)
    try:
        tick = ContextInfo.get_full_tick(["000001.SZ"])
        if tick:
            print("[bigqmt_diag] get_full_tick=OK (keys=%d)" % len(tick))
        else:
            print("[bigqmt_diag] get_full_tick=EMPTY (market may be closed)")
    except Exception as e:
        print("[bigqmt_diag] get_full_tick=FAIL: %s" % str(e)[:60])

    print("[bigqmt_diag] diagnostics complete")
    print("=" * 60)


# ContextInfo families whose FIRST call after a restart can cost minutes.
#
# get_full_tick is already exercised by _diag_startup, on the main thread, and
# comes back in milliseconds. get_financial_data is not, and it was measured at
# 346 SECONDS on its first call after a restart -- while QMT itself was healthy
# (whole-quote data flowing, threadpool alive) and the main strategy thread was
# idle. Every later call that day took under a second, including codes and
# tables never asked for before, so it is a one-time cost and not a per-code
# cache miss.
#
# That block lands on the RPC listener thread, which serves one request at a
# time, so it takes the whole bridge down with it: every queued request times
# out and the client sees a dead bridge.
#
# Warming does not make the cost cheaper. It moves it to a known moment, onto a
# thread nobody is waiting on, with a log line saying what is happening --
# instead of arriving as an unexplained freeze the first time a caller asks.
#
# Deliberately NOT on the main thread: _diag_startup runs there during init, and
# a 346-second call in init would freeze startup before the adjust timer is even
# scheduled -- worse than the problem.
def _warm_financial_data(context_info):
    """Fetch a real slice, not an empty one.

    An EMPTY date range is accepted and returns None instantly. The first
    version of this warmup passed "" for both and reported "warm in 0.00s"
    while exercising nothing at all -- a warmup that silently no-ops is worse
    than none, because the log says it worked. Measured against the live
    terminal, same code and stock:

        dotted field + real range   0.75s  DataFrame rows=159
        dotted field + empty range  0.17s  None
        whole table  + real range   0.41s  DataFrame rows=159
        whole table  + empty range  0.20s  Series rows=6
    """
    end = datetime.date.today()
    start = end - datetime.timedelta(days=365)
    return context_info.get_financial_data(
        ["CAPITALSTRUCTURE.total_capital"], ["000001.SZ"],
        start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), "report_time")


CONTEXT_WARMUP_PROBES = (
    ("get_financial_data", _warm_financial_data),
)


def _warmup_row_count(result):
    """How much a probe brought back, or -1 when that cannot be told."""
    if result is None:
        return 0
    try:
        return len(result)
    except Exception:
        return -1


def _context_warmup_loop(context_info):
    for name, probe in CONTEXT_WARMUP_PROBES:
        started = time.time()
        print("[bigqmt_warmup] %s: first call after a restart can take "
              "minutes; running it now so a caller does not have to wait" % name)
        try:
            result = probe(context_info)
            elapsed = time.time() - started
        except Exception as exc:
            print("[bigqmt_warmup] %s failed after %.1fs: %s"
                  % (name, time.time() - started, str(exc)[:120]))
            continue
        rows = _warmup_row_count(result)
        if rows == 0:
            # Warming nothing while reporting success is the failure mode this
            # check exists to catch; it already happened once.
            print("[bigqmt_warmup] %s returned NOTHING in %.2fs -- the probe "
                  "did not exercise the path it is meant to warm, so the "
                  "first real caller will still pay the wait" % (name, elapsed))
        elif elapsed > 10.0:
            print("[bigqmt_warmup] %s warm after %.1fs (%s rows) -- that wait "
                  "is now paid; callers should see sub-second responses"
                  % (name, elapsed, rows))
        else:
            print("[bigqmt_warmup] %s warm in %.2fs (%s rows)"
                  % (name, elapsed, rows))


def _start_context_warmup(context_info, config):
    """Kick the warmup onto a daemon thread. Never blocks init."""
    flag = dict(config.get("rpc") or {}).get("warm_context_data", True)
    if isinstance(flag, str):
        flag = flag.strip().lower() not in ("0", "false", "no", "off", "")
    if not flag:
        return
    thread = threading.Thread(
        target=_context_warmup_loop, args=(context_info,),
        name="bigqmt-context-warmup", daemon=True)
    thread.start()


def _pump_download_jobs(context_info, config):
    """Advance any queued async download job by a bounded slice on this thread."""
    job_config = dict(config.get("download_jobs") or {})
    if not _config_bool(job_config.get("enabled"), True):
        return None
    account_id = str(job_config.get("account_id") or config.get("account_id") or _account_id or "")
    if not account_id:
        return None
    # Reuse one client. This runs on every adjust tick, so building a client
    # here leaked one connection pool per tick -- at a 100ms interval that is
    # ten per second. The symptom is easy to miss: the pools are garbage
    # collected, and redis-py's __del__ then raises
    # "AttributeError: 'Redis' object has no attribute 'connection'", which
    # Python swallows as "Exception ignored in". It never reaches a log the
    # package writes; it only shows up in the QMT panel.
    #
    # _exec_event_redis already learned this lesson and caches; this path was
    # missed. Both only build a client when _rpc_service has none, which is the
    # zmq-transport case.
    redis_client = _exec_event_redis(config)
    if redis_client is None:
        return None
    market_data = getattr(getattr(_rpc_service, "handlers", None), "market_data", None)
    if market_data is None:
        from bigqmt_signal_trader.adapters.market_bigqmt import BigQmtMarketDataProvider

        market_data = BigQmtMarketDataProvider(context_info, qmt_api=dict(config.get("qmt_api") or {}))
    try:
        from bigqmt_signal_trader.download_jobs import pump_download_jobs

        return pump_download_jobs(
            redis_client,
            market_data,
            account_id,
            chunk_size=int(job_config.get("chunk_size") or 10),
            max_wall_seconds=float(job_config.get("max_wall_seconds") or 0.5),
            job_ttl_seconds=int(job_config.get("job_ttl_seconds") or 3600),
        )
    except Exception as exc:
        _log_err("download_jobs", "pump failed: %s" % exc)
        return None


def _adjust_phase(name, fn, *args):
    """Time one adjust phase; log only if it exceeds 50ms. Pinpoints which part of
    the 500ms adjust cycle holds the GIL (the gil_probe shows the stall exists;
    this shows WHERE). The finally-log never alters the call's result/exception.

    Guard with except: an exception here (e.g. redis outage inside the LPOP
    drain) must NOT propagate into adjust/handlebar — QMT stops the strategy on
    a callback raise, which is the 'auto-exit' users report. Log and continue.
    """
    t0 = time.perf_counter()
    try:
        return fn(*args)
    except Exception:
        import traceback as _tb
        try:
            from bigqmt_signal_trader.logging_setup import get_logger
            get_logger("adjust").error("adjust phase %s failed:\n%s", name, _tb.format_exc())
        except Exception:
            pass
        return None
    finally:
        ms = (time.perf_counter() - t0) * 1000.0
        if ms > 50.0:
            print("[adjust_phase] %s %.0fms" % (name, ms))


def adjust(ContextInfo, _source="timer"):
    global _adjust_logged
    _record_adjust_tick()
    _record_adjust_source(_source)
    config = _build_config()
    _adjust_phase("drain", _drain_rpc_service, config)
    _adjust_phase("exec_hold", _flush_held_presysid_orders, config)
    # AFTER the drain, and not on the tick that scheduled it. The drain is what
    # flushes queued RPC responses, and reset_app() tears the transport down
    # with any reply still in it -- reloading first killed the reply to
    # reload_deployment itself, which the server had already logged as
    # "responded ok=True" while the client sat there until it timed out.
    if _reload_request["pending"] and time.time() >= _reload_request["not_before"]:
        _adjust_phase("reload", _perform_reload, ContextInfo)
        return None
    _adjust_phase("full_tick", _refresh_full_tick_cache, ContextInfo, config)
    _adjust_phase("download", _pump_download_jobs, ContextInfo, config)
    try:
        if hasattr(ContextInfo, "is_last_bar") and not ContextInfo.is_last_bar():
            return None
    except Exception:
        pass
    if not _adjust_logged:
        print("[bigqmt_signal_trader] adjust ok")
        _adjust_logged = True
    _tick_app_t0 = time.perf_counter()
    try:
        return _adjust_phase("tick_app", tick_app, ContextInfo, datetime.datetime.now())
    finally:
        # Every call, not just the >50ms ones _adjust_phase prints: deciding
        # whether ticks may drive this needs the whole distribution.
        _record_tick_app_ms((time.perf_counter() - _tick_app_t0) * 1000.0)


def handlebar(ContextInfo):
    """Standard Big QMT bar callback.

    Documented as tick-driven during live trading ("再在每个tick数据来后驱动运行
    一次"), but the observed cadence is a flat ~50/10s -- the run_time timer
    alone. Tagging the source tells us whether this ever fires once the
    historical replay ends; the strategy subscribes to no quote, which is the
    leading suspect.
    """
    return adjust(ContextInfo, _source="handlebar")


# Redis is preferred for exec events, but only while it actually works.
# "Configured" is not "reachable": redis-py builds a client lazily and does not
# dial until the first command, so a stale redis block in the config yields a
# perfectly good-looking client that times out on every publish -- and the zmq
# push channel sitting right next to it never gets used (issue #145).
_EXEC_REDIS_FAILURE_LIMIT = 3
_exec_sink_state = {"redis_failures": 0, "reports": 0, "demoted": False}

# issue #161: QMT fires the order callback once when the order row appears and
# again when m_strOrderSysID is populated (#152's window) -- the client then
# logs two identical 已报 events, the first degenerate (no sysid, order_id=0).
# A sysid-less order event is held for a short window; if its sysid-bearing
# twin arrives the held one is dropped, otherwise the adjust tick publishes it.
_held_presysid_orders = {}      # key -> (event, held_at, raw_obj)
_HELD_PRESYSID_DEFAULT_SECONDS = 0.8
_instrument_name_cache = {}     # stock_code -> name (only non-empty cached)


def _presysid_key(event):
    """Identity for pairing a sysid-less event with its sysid-bearing twin."""
    remark = str(event.get("user_order_id") or event.get("remark") or "").strip()
    if remark:
        return ("remark", remark)
    stock = str(event.get("stock_code") or "")
    if not stock:
        return None  # cannot key safely -- publish immediately
    return ("fields", (
        stock, event.get("price"),
        event.get("volume") or event.get("order_volume"),
        event.get("direction"),
    ))


def _hold_presysid_order(event, event_config, raw_obj=None):
    """Hold a sysid-less order event instead of publishing it. True if held."""
    if str(event.get("order_sys_id") or ""):
        return False
    hold_s = float(event_config.get("hold_presysid_order_seconds",
                                    _HELD_PRESYSID_DEFAULT_SECONDS) or 0)
    if hold_s <= 0:
        return False
    key = _presysid_key(event)
    if key is None:
        return False
    _held_presysid_orders[key] = (event, time.time(), raw_obj)
    return True


def _drop_held_presysid_twin(event):
    """A sysid-bearing event supersedes its held sysid-less twin."""
    if not str(event.get("order_sys_id") or ""):
        return
    key = _presysid_key(event)
    if key is not None:
        _held_presysid_orders.pop(key, None)


def _flush_held_presysid_orders(config):
    """Publish held events whose window expired. Runs on the adjust tick."""
    if not _held_presysid_orders:
        return
    event_config = dict(config.get("exec_events") or {})
    hold_s = float(event_config.get("hold_presysid_order_seconds",
                                    _HELD_PRESYSID_DEFAULT_SECONDS) or 0)
    now = time.time()
    expired = [key for key, entry in _held_presysid_orders.items()
               if now - entry[1] >= hold_s]
    if not expired:
        return
    exec_events = _exec_events
    account_id = str(event_config.get("account_id") or config.get("account_id")
                     or _account_id or "")
    sink = _exec_event_sink(config)
    if exec_events is None or sink is None or not account_id:
        for key in expired:
            _held_presysid_orders.pop(key, None)
        return
    for key in expired:
        entry = _held_presysid_orders.pop(key, None)
        if entry is None:
            continue
        event, _held_at, raw_obj = entry
        _publish_one(exec_events, sink, account_id, event, "order", config)
        try:
            status = int(event.get("status") or 0)
        except (TypeError, ValueError):
            status = 0
        if status == 57:
            # the held event turns out to be a junk: it still owes the
            # order_error twin the non-held path would have published.
            _publish_one(exec_events, sink, account_id,
                         exec_events.normalize_order_error_event(raw_obj, account_id),
                         "order_error", config)


def _event_instrument_name(context_info, stock_code):
    """Resolve the instrument name for an event, cached; empty never cached."""
    code = str(stock_code or "")
    if not code:
        return ""
    cached = _instrument_name_cache.get(code)
    if cached:
        return cached
    name = ""
    getter = getattr(context_info, "get_stock_name", None) if context_info is not None else None
    if getter is not None:
        try:
            name = str(getter(code) or "")
        except Exception:
            name = ""
    if name:
        _instrument_name_cache[code] = name
    return name


def _push_channel_sink():
    if _quote_subscription_service is None:
        return None
    try:
        return _quote_subscription_service[1]          # (manager, channel)
    except Exception:
        return None


def _exec_event_sink(config):
    """Where exec events go: a Redis client, or the quote push channel.

    Exec events were Redis-only, so a zmq deployment with no Redis configured
    silently delivered no order/trade callbacks at all -- _publish_exec_event
    simply returned (issue #76). zmq deployments already run a push channel for
    whole-quote data, so reuse it rather than opening a second socket.

    Redis stays first *while it works*: its channels carry streams for short
    replay, which the push channel has no equivalent of. After
    _EXEC_REDIS_FAILURE_LIMIT consecutive publish failures it is demoted and
    the push channel takes over, because an unreachable redis was otherwise
    swallowing every callback while a working channel stood idle (issue #145).
    """
    if not _exec_sink_state["demoted"]:
        redis_client = _exec_event_redis(config)
        if redis_client is not None:
            return redis_client
    return _push_channel_sink()


def _note_exec_publish_failure(kind, exc):
    """Count a failure, demote redis once it is clearly not coming back, and
    keep the log readable.

    The full traceback is deliberate -- issue #76 took a day because str(exc)
    alone read "error return without exception set" with no origin. But a
    persistently unreachable redis prints that for EVERY order and deal, which
    buries the log it is meant to explain. Full detail the first few times,
    a one-liner after that.
    """
    state = _exec_sink_state
    state["redis_failures"] += 1
    state["reports"] += 1
    if state["reports"] <= 3:
        _log_err(
            "exec_events",
            "publish %s failed: %s (%s)\n%s"
            % (kind, exc, exc.__class__.__name__, _traceback.format_exc()),
        )
    elif state["reports"] % 50 == 0:
        _log_err(
            "exec_events",
            "publish %s still failing after %d attempts: %s (%s)"
            % (kind, state["reports"], exc, exc.__class__.__name__),
        )
    if not state["demoted"] and state["redis_failures"] >= _EXEC_REDIS_FAILURE_LIMIT:
        state["demoted"] = bool(_push_channel_sink())
        if state["demoted"]:
            print("[bigqmt_exec_events] redis failed %d times in a row; switching "
                  "to the quote push channel for order/trade callbacks. Remove the "
                  "redis block from the local config to skip this entirely."
                  % state["redis_failures"])


def _publish_one(exec_events, sink, account_id, event, kind, config):
    """Publish one event, falling back to the push channel if the sink fails.

    Without the fallback a failed publish simply lost the callback -- the
    client never learns the order happened. Trying the other channel costs one
    extra attempt and only on the failure path.
    """
    try:
        exec_events.publish_exec_event(sink, account_id, event)
        _exec_sink_state["redis_failures"] = 0
        return True
    except Exception as exc:
        _note_exec_publish_failure(kind, exc)
    fallback = _push_channel_sink()
    if fallback is None or fallback is sink:
        return False
    try:
        exec_events.publish_exec_event(fallback, account_id, event)
        return True
    except Exception as exc:
        _note_exec_publish_failure("%s (push fallback)" % kind, exc)
        return False


def _exec_event_redis(config):
    """Return a redis client for exec-event publishing, reusing one instance.

    Previously a new client was built per order/trade callback when the RPC
    service had none (the zmq-transport case), leaking a connection pool per
    event. Reuse one; build failure returns None so publishing just skips.

    A non-Redis transport may retain a Redis block for optional download jobs
    or exec-event replay. Skip that block only when both consumers are
    explicitly disabled; omitted flags retain the legacy enabled behavior.
    """
    global _exec_event_redis_client
    existing = getattr(_rpc_service, "redis", None) if _rpc_service is not None else None
    if existing is not None:
        return existing
    if _exec_event_redis_client is not None:
        return _exec_event_redis_client
    if not _config_bool((config.get("download_jobs") or {}).get("enabled"), True) and not _config_bool(
        (config.get("exec_events") or {}).get("enabled"), True
    ):
        return None
    redis_config = dict(config.get("redis") or {})
    if not redis_config:
        return None
    try:
        from bigqmt_signal_trader.adapters.redis_common import build_redis_client

        _exec_event_redis_client = build_redis_client(redis_config)
    except Exception as exc:
        # 静默 None 会让事件悄悄全丢（issue #71 的 protocol 崩溃就是这样没的），
        # 必须留痕。
        try:
            from bigqmt_signal_trader.logging_setup import get_logger

            get_logger("strategy").error("exec-event redis client build failed: %s", exc)
        except Exception:
            pass
        return None
    return _exec_event_redis_client


def _local_identity_strategy_name(account_id, event):
    """strategy_name from the in-process submit journal, or "".

    The journal (#156) is written by the RPC handlers at submit time and is
    all a deployment has when redis is absent -- or, the reported case in
    #174, configured but not reachable, which fails silently.
    """
    service = _rpc_service
    handlers = getattr(service, "handlers", None) if service is not None else None
    journal = getattr(handlers, "_order_identity_local", None)
    if not journal:
        return ""
    remark = str(event.get("user_order_id") or event.get("remark") or "").strip()
    if not remark:
        return ""
    try:
        entry = journal.get((str(account_id or ""), remark))
        if not entry:
            return ""
        stamped, name = entry
        ttl = getattr(handlers, "_ORDER_IDENTITY_LOCAL_TTL_SECONDS", 86400.0)
        if name and (time.time() - float(stamped)) <= float(ttl):
            return str(name)
    except Exception:
        return ""
    return ""


def _enrich_event_identity(exec_events, config, account_id, event):
    """Put the strategy name back on an event QMT could not name (#174).

    Most events no longer get here: normalize_*_event reads the name off
    报单来源 (m_strSource), which is passorder's own strategyName argument
    coming back, so an order this bridge placed names itself and returns at the
    first line below. m_strStrategyName really is absent (120 and 47 attributes
    on a live terminal, in neither) -- concluding from that alone that the name
    was absent too is what made this function the only way back.

    It stays as the fallback for what the row cannot answer: a terminal that
    blanks the source, and orders whose name only the bridge ever knew.

    The in-process journal is consulted FIRST, and deliberately so. This runs
    on QMT's C++ callback thread, and for an order this process submitted the
    journal already holds the same string that was written to redis -- so
    asking redis first would buy nothing and cost a round trip per event. It
    costs a lot when redis is configured and not reachable: redis-py does not
    dial until the first command, so every callback would pay the full
    timeout (#145's shape). That is precisely the deployment #174 was reported
    from.

    Redis is the fallback rather than the primary because its extra reach --
    naming an order some OTHER process submitted -- is the rarer case.

    Both branches call this. Enriching only orders is what left
    on_stock_trade's strategy_name permanently empty.

    The row's own strategy-name field is NOT authoritative (#216): QMT fills
    it with the QMT-side strategy's registered name (this bridge process),
    not the strategy_name the caller passed at order time. So no early return
    on a non-empty row value -- a submit-time identity with a name WINS.
    """
    name = _local_identity_strategy_name(account_id, event)
    if name:
        event["strategy_name"] = name
        return event
    redis_client = _exec_event_redis(config)
    if redis_client is not None:
        try:
            event = exec_events.enrich_order_identity(redis_client, account_id, event)
        except Exception:
            pass
    return event


def _publish_exec_event(kind, obj, context_info=None):
    """Push a normalized order/trade event to Redis for real-time client callbacks."""
    config = _build_config()
    event_config = dict(config.get("exec_events") or {})
    # Raw-field diagnostics run BEFORE every other check (and before the
    # enabled/account_id early returns), because the point is to observe the
    # object exactly as QMT handed it over — even when publishing is off.
    raw_fields = None
    exec_events = _exec_events
    if exec_events is None:
        # Already reported once at module load; a per-callback log would flood.
        return
    if _config_bool(event_config.get("debug_raw_fields"), False):
        try:
            print(exec_events.format_raw_snapshot(kind, obj))
            raw_fields = exec_events.raw_field_snapshot(obj)
        except Exception as exc:
            print("[bigqmt_exec_raw] snapshot %s failed: %s" % (kind, exc))
    if not _config_bool(event_config.get("enabled"), True):
        return
    account_id = str(event_config.get("account_id") or config.get("account_id") or _account_id or "")
    if not account_id:
        return
    sink = _exec_event_sink(config)
    if sink is None:
        return
    try:
        if kind == "trade":
            event = exec_events.normalize_trade_event(obj, account_id)
            event = _enrich_event_identity(exec_events, config, account_id, event)
            if not event.get("instrument_name"):
                event["instrument_name"] = _event_instrument_name(
                    context_info, event.get("stock_code"))
            if raw_fields:
                event["raw_fields"] = raw_fields
            _publish_one(exec_events, sink, account_id, event, kind, config)
        else:
            event = exec_events.normalize_order_event(obj, account_id)
            # Identity enrichment reads the remark->identity map that
            # remember_order_identity wrote. On a push channel the event goes
            # out un-enriched rather than not at all; order_sys_id and remark
            # are already on it.
            event = _enrich_event_identity(exec_events, config, account_id, event)
            if not event.get("instrument_name"):
                event["instrument_name"] = _event_instrument_name(
                    context_info, event.get("stock_code"))
            # QMT fires this callback once with the row pre-sysid and again
            # once the id lands (#152's window) -- two identical 已报 events,
            # the first degenerate (issue #161). Hold the sysid-less one; the
            # twin drops it, the adjust flush publishes it if no twin comes.
            if _hold_presysid_order(event, event_config, obj):
                return
            _drop_held_presysid_twin(event)
            if raw_fields:
                event["raw_fields"] = raw_fields
            _publish_one(exec_events, sink, account_id, event, kind, config)
            # 废单 (status=57 ENTRUST_STATUS_JUNK) 推送 order_error，让客户端
            # on_order_error 能感知下单被拒。
            try:
                status = int(event.get("status") or 0)
            except (TypeError, ValueError):
                status = 0
            if status == 57:
                err_event = exec_events.normalize_order_error_event(obj, account_id)
                if raw_fields:
                    err_event["raw_fields"] = raw_fields
                _publish_one(exec_events, sink, account_id, err_event,
                             "order_error", config)
    except Exception as exc:
        # Publishing itself is handled (and throttled) inside _publish_one, so
        # anything reaching here came from normalizing the QMT object. str(exc)
        # alone reads "error return without exception set" with no hint of where
        # it came from -- that is what made issue #76 take a day to pin down.
        _log_err(
            "exec_events",
            "building the %s event failed: %s (%s)\n%s"
            % (kind, exc, exc.__class__.__name__, _traceback.format_exc()),
        )


def order_callback(ContextInfo, orderInfo):
    """Standard Big QMT order callback."""
    # Settlement feeds on this even when client push is off (issue #164).
    _note_order_watch(orderInfo)
    _publish_exec_event("order", orderInfo, ContextInfo)
    return forward_order_event(BigQmtRuntimeAdapter.to_order_event(orderInfo))


def deal_callback(ContextInfo, dealInfo):
    """Standard Big QMT deal callback."""
    _publish_exec_event("trade", dealInfo, ContextInfo)
    return forward_trade_event(BigQmtRuntimeAdapter.to_trade_event(dealInfo))


def credit_account_callback(ContextInfo, seq, result):
    """Standard Big QMT credit-account callback (官方参考 6.13).

    信用账户明细只有这一条查柜台的路：query_credit_account 立刻返回，结果从这里
    出来。QMT 只往被挂载的那个文件的命名空间里回调，所以每个入口文件都要像
    order_callback / deal_callback 那样再导出一次这个名字（#202）。

    回调跑在 QMT 的 C++ 线程上，所以这里只把结果交给 handlers 存着，绝不做需要
    主线程上下文的事，也绝不让异常逃出去 —— 回调里抛异常在 QMT 那边表现为没有
    堆栈的 SystemError（issue #76）。
    """
    # 这里以前成功和失败都不出声，于是「QMT 根本没回调」和「回调来了但没送到
    # handlers」从外面看一模一样 —— 正是 CLAUDE.md 里那条「a failed operation
    # looks exactly like one that never ran」，栽在自己的新代码上。所以每一次
    # 触发都留痕：计数交给 handlers（probe_capabilities 报出来），送不到时
    # 一定要吼出来。
    global _credit_callback_fired
    _credit_callback_fired += 1
    try:
        handlers = getattr(_rpc_service, "handlers", None) if _rpc_service else None
        if handlers is None:
            _log_err("credit_account_callback",
                     "QMT delivered a credit account detail (seq=%s) but there is "
                     "no RPC service to hand it to -- the answer is being dropped."
                     % (seq,))
            return False
        if not hasattr(handlers, "note_credit_account"):
            _log_err("credit_account_callback",
                     "QMT delivered a credit account detail (seq=%s) but this "
                     "deployment's handlers have no note_credit_account -- the "
                     "bridge package is older than the entry file. Re-sync and "
                     "restart." % (seq,))
            return False
        return handlers.note_credit_account(seq, result)
    except Exception as exc:
        _log_err("credit_account_callback",
                 "recording the credit account detail failed: %s (%s)"
                 % (exc, exc.__class__.__name__))
        return False


def sync_positions(ContextInfo):
    return sync_positions_app("manual")
