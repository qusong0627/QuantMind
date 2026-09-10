#coding:gbk
"""QMT bridge entry using the same file-loader pattern as qmt_realtime strategies.

Broker QMT strategy sandboxes may reject local package names through their
normal ``import`` allowlist.  The realtime QMT strategies in gupiao_ztfx load
their colocated helpers through ``importlib.util.spec_from_file_location``.
This entry applies path-based loading to the bridge package, including its
internal relative imports, while leaving all standard-library and QMT imports
untouched.  This terminal's spec loader ignores custom builtins for nested
package imports, so local bridge files are compiled explicitly after resolving
their path.
"""
import builtins as _builtins
import os
import sys
import types

try:
    import importlib as _importlib
except ImportError:
    # Some QMT python36.zip builds omit importlib. The local loader only needs
    # import_module and reload, so provide the smallest compatible fallback.
    _importlib = types.ModuleType("importlib")

    def _fallback_import_module(name, package=None):
        if package or str(name).startswith("."):
            raise ImportError("relative import requires the standard importlib package")
        return _builtins.__import__(name, globals(), locals(), ("*",), 0)

    def _fallback_reload(module):
        return module

    _importlib.import_module = _fallback_import_module
    _importlib.reload = _fallback_reload
    sys.modules["importlib"] = _importlib


_LOCAL_ROOTS = (
    "bigqmt_signal_trader",
    "bigqmt_signal_trader_strategy",
    "bigqmt_signal_trader_redis_rpc_runtime",
    "bigqmt_signal_trader_local_config",
)
_ORIGINAL_IMPORT = _builtins.__import__
_ORIGINAL_IMPORT_MODULE = _importlib.import_module
_ORIGINAL_RELOAD = _importlib.reload


def _known_qmt_python_dir():
    # Find the QMT python dir from sys.path instead of a hardcoded path, so
    # the bridge loads regardless of broker install location or launch mode
    # (editor / paste-run / exec). Falls back to empty when not found.
    for p in sys.path:
        if p and r"\python" in p and os.path.isdir(p):
            return p
    return ""


try:
    _SOURCE_ROOT = os.path.dirname(os.path.abspath(__file__))
except Exception:
    _SOURCE_ROOT = _known_qmt_python_dir()
if not _SOURCE_ROOT:
    _SOURCE_ROOT = _known_qmt_python_dir()


def _is_local_module(name):
    return any(name == root or name.startswith(root + ".") for root in _LOCAL_ROOTS)


def _resolve_name(name, module_globals, level):
    if not level:
        return name
    package = (module_globals or {}).get("__package__") or (module_globals or {}).get("__name__", "")
    if not package:
        raise ImportError("relative import without package")
    for unused in range(level - 1):
        if "." not in package:
            raise ImportError("relative import beyond top-level package")
        package = package.rsplit(".", 1)[0]
    return package + ("." + name if name else "")


def _find_local_source(name):
    relative = name.replace(".", os.sep)
    dirs = []
    if _SOURCE_ROOT:
        dirs.append(_SOURCE_ROOT)
    for p in sys.path:
        if p and os.path.isdir(p) and p not in dirs:
            dirs.append(p)
    for d in dirs:
        package_init = os.path.join(d, relative, "__init__.py")
        if os.path.isfile(package_init):
            return package_init, True
        module_file = os.path.join(d, relative + ".py")
        if os.path.isfile(module_file):
            return module_file, False
    raise ModuleNotFoundError("local source not found: %s" % name, name=name)


def _set_parent_attribute(name, module):
    if "." not in name:
        return
    parent_name, child_name = name.rsplit(".", 1)
    parent = _load_local_module(parent_name)
    setattr(parent, child_name, module)


# A strategy file that arrived corrupted fails with a bare NameError naming a
# 200-character token, which says nothing about the file being broken (issue
# #102). Real symptom, from a reporter's terminal:
#
#     File "...\bigqmt_signal_trader_strategy.py", line 1, in <module>
#         MiFBOecYoHXT4UUBBOIr3m5aTVbA5Rbt6OnG52cfBT5EAtPG9kA7kQnEsKuDUOORy...
#     NameError: name 'MiFBOecYoHXT4UUB...' is not defined
#
# Python read line 1 as a variable name because that is all it was: the file
# had been saved from something other than the source -- a download page, a
# proxy that served a token instead of the file. Every version behaves the
# same way, so "try a different version" sends people in the wrong direction.
_TOKEN_CHARS = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_=+/")


def _looks_like_a_token_not_python(source):
    """True when the first real line cannot be Python at all.

    Deliberately narrow. A Python line this long with no whitespace and
    nothing outside the base64 alphabet would have to be a bare identifier,
    which is already broken -- so a false positive costs a clearer message on
    code that was going to fail anyway.
    """
    try:
        if isinstance(source, bytes):
            source = source.decode("utf-8", "replace")
        for line in source.splitlines():
            line = line.strip()
            if not line:
                continue
            return (len(line) >= 40
                    and not any(char.isspace() for char in line)
                    and all(char in _TOKEN_CHARS for char in line))
    except Exception:
        pass
    return False


def _load_local_module(name):
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    source_path, is_package = _find_local_source(name)
    if "." in name:
        _load_local_module(name.rsplit(".", 1)[0])
    module = types.ModuleType(name)
    module.__file__ = source_path
    module.__package__ = name if is_package else name.rpartition(".")[0]
    if is_package:
        module.__path__ = [os.path.dirname(source_path)]
    module_builtins = dict(_builtins.__dict__)
    module_builtins["__import__"] = _local_import
    module.__dict__["__builtins__"] = module_builtins
    module.__dict__["__bigqmt_load_local_module"] = _load_local_module
    sys.modules[name] = module
    # QMT native allowlist rejects the root package eager exports.
    if name == "bigqmt_signal_trader":
        return module
    try:
        with open(source_path, "rb") as source_file:
            source = source_file.read()
        exec(compile(source, source_path, "exec"), module.__dict__)
    except Exception:
        sys.modules.pop(name, None)
        if _looks_like_a_token_not_python(source):
            raise RuntimeError(
                "%s is not Python source -- its first line is one long token, "
                "so the file was saved from something other than the code "
                "(a download page, a mirror that served a token). Fetch it "
                "again: pip install --upgrade xtquant-big-convert then "
                "xt_trader.sync_deployment(), or copy src/ from the release. "
                "A different version will not help; every version fails the "
                "same way on this file." % source_path)
        raise
    _set_parent_attribute(name, module)
    return module


# Names proven not to be modules. A miss costs a full sys.path
# walk, so it is worth remembering; a hit never changes back.
_MISSING_LOCAL_MODULES = set()


def _local_import(name, module_globals=None, module_locals=None, fromlist=(), level=0):
    absolute_name = _resolve_name(name, module_globals, level)
    if not _is_local_module(absolute_name):
        return _ORIGINAL_IMPORT(name, module_globals, module_locals, fromlist, level)
    module = _load_local_module(absolute_name)
    for child in fromlist or ():
        if child != "*":
            child_name = absolute_name + "." + child
            # A fromlist entry is usually an attribute, not a submodule.
            # Looking one up walks _SOURCE_ROOT plus every sys.path entry
            # doing os.path.isfile, then raises -- and nothing remembered the
            # answer, so it ran again on EVERY `from X import Y` in the
            # sandbox. Measured in the live terminal: a ping whose handler is
            # one such import took 1493ms in handle(); with the miss
            # remembered it takes 0ms, and the round trip went from ~1800ms to
            # ~95-200ms.
            if child_name in _MISSING_LOCAL_MODULES:
                continue
            try:
                _load_local_module(child_name)
            except ModuleNotFoundError:
                _MISSING_LOCAL_MODULES.add(child_name)
    if fromlist:
        return module
    return _load_local_module(absolute_name.split(".", 1)[0])


def _local_import_module(name, package=None):
    if _is_local_module(name):
        return _load_local_module(name)
    return _ORIGINAL_IMPORT_MODULE(name, package)


def _local_reload(module):
    if _is_local_module(getattr(module, "__name__", "")):
        return _load_local_module(module.__name__)
    return _ORIGINAL_RELOAD(module)


def _clear_local_modules():
    for name in list(sys.modules):
        if _is_local_module(name):
            sys.modules.pop(name, None)


def _stop_previous_rpc_service():
    """Release the previous QMT strategy's socket before clearing its module.

    QMT can re-execute this entry in the same Python process.  The old strategy
    module owns the RPC service and its ZMQ ROUTER socket, so dropping that
    module from ``sys.modules`` first would make the service unreachable and
    leave its port bound for the next strategy start.
    """
    previous = sys.modules.get("bigqmt_signal_trader_strategy")
    reset = getattr(previous, "reset_app", None)
    if not callable(reset):
        return
    try:
        reset()
        print("[bigqmt_shell] previous rpc service stopped")
    except Exception as exc:
        # Continue the reload so a broken old instance does not prevent QMT
        # from reporting its normal startup error.
        print("[bigqmt_shell] previous rpc service stop failed: %s" % exc)


_stop_previous_rpc_service()
_clear_local_modules()
_importlib.import_module = _local_import_module
_importlib.reload = _local_reload
print("[bigqmt_shell] importlib entry source_root=%s" % _SOURCE_ROOT)


def _fallback_account_id():
    for name in ("BIGQMT_ACCOUNT_ID", "account", "account_id", "accountID"):
        value = globals().get(name)
        if value:
            return str(value)
    return ""


try:
    _local_import("bigqmt_signal_trader.adapters.redis_common", globals(), fromlist=("*",))
    _local_import("bigqmt_signal_trader.redis_rpc", globals(), fromlist=("*",))
    _strategy = _local_import("bigqmt_signal_trader_strategy", globals(), fromlist=("*",))
    _strategy.reset_app()
except Exception as bridge_preload_error:
    print("[bigqmt_shell] bridge preload failed: %s" % bridge_preload_error)

_runtime = _local_import("bigqmt_signal_trader_redis_rpc_runtime", globals(), fromlist=("*",))


def _load_local_config():
    return _local_import("bigqmt_signal_trader_local_config", globals(), fromlist=("*",))


try:
    _config = _load_local_config()
    BIGQMT_REDIS_CONFIG = getattr(_config, "BIGQMT_REDIS_CONFIG", {})
    forced_transport = globals().get("BIGQMT_FORCE_TRANSPORT")
    if forced_transport:
        BIGQMT_REDIS_CONFIG = dict(BIGQMT_REDIS_CONFIG or {})
        BIGQMT_REDIS_CONFIG["transport"] = str(forced_transport)
        if str(forced_transport).lower() == "zmq":
            # setdefault, not assignment: an explicit False in the local config
            # is the #183 drain opt-in, and overwriting it kept the pure-zmq
            # entries -- the deployments that most want low latency -- on the
            # slow path (#188). Measured on the live terminal, drain takes zmq
            # from ping 404ms / positions 607ms to 95ms for both.
            #
            # Only the historical default is filled in here. A transport that
            # cannot drain still gets its receiver thread: _resolve_background_
            # threads forces True for anything outside zmq/mysql/redis.
            BIGQMT_REDIS_CONFIG.setdefault("rpc_background_threads", True)
            BIGQMT_REDIS_CONFIG["download_jobs_enabled"] = False
            # Execution events have a native ZMQ PUB path.  Preserve the
            # configured/default switch so MiniQMT-compatible order and trade
            # callbacks keep working without Redis.
            BIGQMT_REDIS_CONFIG["full_tick_cache_enabled"] = False
    print("[bigqmt_shell] local rpc config loaded transport=%s keys=%s" % (
        (BIGQMT_REDIS_CONFIG or {}).get("transport", "redis"),
        sorted((BIGQMT_REDIS_CONFIG or {}).keys()),
    ))
    _runtime.configure_runtime_redis(BIGQMT_REDIS_CONFIG)
except Exception as rpc_config_error:
    print("[bigqmt_shell] local rpc config load failed: %s" % rpc_config_error)
    if str(globals().get("BIGQMT_FORCE_TRANSPORT") or "").lower() == "zmq":
        raise RuntimeError("ZMQ bridge requires a valid local QMT config: %s" % rpc_config_error)

try:
    _config = _load_local_config()
    BIGQMT_ACCOUNT_ID = getattr(_config, "BIGQMT_ACCOUNT_ID", "")
    print("[bigqmt_shell] local account config loaded=%s" % bool(BIGQMT_ACCOUNT_ID))
    _runtime.configure_runtime_account(BIGQMT_ACCOUNT_ID)
except Exception as account_config_error:
    print("[bigqmt_shell] local account config load failed: %s" % account_config_error)
    account_id = _fallback_account_id()
    if account_id:
        _runtime.configure_runtime_account(account_id)

try:
    # QMT only injects its globals into the namespace of the file it mounts --
    # THIS one -- so the capture has to happen here, with this file's globals().
    #
    # It used to be a hand-copied tuple of names right here, and that is exactly
    # how query_credit_account went missing: it was added to the strategy
    # module's _QMT_INJECTED_GLOBAL_FUNCS (which the strategy's own comment
    # calls "the single source for that list; do not hand-copy the names
    # elsewhere") and this copy was not updated. The bridge then reported
    # "this terminal does not have query_credit_account" while the terminal had
    # it all along -- a bridge bug misreported as a broker capability (#202).
    #
    # So: read the single source instead of copying it.
    qmt_extra = _runtime.capture_qmt_injected_funcs(globals())
    print("[bigqmt_shell] download globals bound=%s" % sorted(k for k in qmt_extra if "download" in k or "down_history" in k))
    _runtime.bind_runtime_api(
        passorder_func=globals().get("passorder"),
        cancel_func=globals().get("cancel"),
        get_trade_detail_data_func=globals().get("get_trade_detail_data"),
        extra_funcs=qmt_extra or None,
    )
    # QMT injects passorder / get_trade_detail_data / download_history_data
    # into a file it runs AS A STRATEGY. When none of them are here the file is
    # being exec'd as a plain script: the module body runs, init() is never
    # called, no RPC service starts, and the run simply ends -- which from the
    # log looks like a successful start followed by "finished" (issue #123).
    # ASCII only: this file declares #coding:gbk and is stored as UTF-8, so it
    # holds together only while every byte is ASCII.
    if not any(name in globals() for name in
               ("passorder", "get_trade_detail_data", "download_history_data")):
        print("[bigqmt_shell] WARNING: QMT injected none of its API globals "
              "(passorder / get_trade_detail_data / download_history_data), so "
              "this file is running as a plain script rather than as a "
              "strategy. init() will never be called, the RPC service will not "
              "start, and the run ends here with nothing listening. Two known "
              "causes: running it from the strategy EDITOR window instead of "
              "adding it under model trading, and the 'standalone python "
              "process' option, which makes QMT exec the file as __main__ "
              "without calling init(). See issue #123.")
except NameError:
    pass


init = _runtime.init
handlebar = _runtime.handlebar
adjust = _runtime.adjust
order_callback = _runtime.order_callback
deal_callback = _runtime.deal_callback
# Credit-account counter query callback. QMT only calls back into the
# namespace of the file it mounted, so this has to be re-exported here
# the same way order_callback / deal_callback are (#202).
credit_account_callback = _runtime.credit_account_callback
