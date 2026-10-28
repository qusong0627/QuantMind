#coding:gbk
"""QMT bridge v2 - plain imports, no file IO in strategy code.

Why this file exists
--------------------
The 2026-10-23+ QMT Python runtime (2.0.3.12) blocks file IO inside strategies:
    PermissionError: Forbidden fileIO: <path>
The old entry (BIGQMT_REDIS_DRYRUN.py) loads its modules through
importlib/compile/open, and the new build rejects it at load time with
"CFromulaExpandData::loadFile ... parse error".

Measured on this terminal (2026-10-26 probes):
  * direct open() read/write  -> BLOCKED (Forbidden fileIO)
  * import from disk          -> ALLOWED (redis 8.1.0 imports from the QMT venv)
  * socket / HTTP             -> ALLOWED (file bridge 51757, TQRelay 17710 ok)
So: plain `import` of the package (no custom loader) + redis from the venv.
File-based logging is disabled (it would hit the blocked open()).

Entry points are real `def`s (the loader appears to need them defined, not
aliased).
"""
import os
import sys

_VENV = 'C:/xmt/venv/Lib/site-packages'
_PYDIR = 'C:/xmt/python'
for _p in (_PYDIR, _VENV):
    if _p not in sys.path:
        sys.path.append(_p)

# The strategy sandbox rejects open(); the kit's file logger probes with open()
# and would fall back anyway - turn it off so nothing tries.
os.environ['BIGQMT_LOG_ENABLED'] = '0'

import bigqmt_signal_trader_redis_rpc_runtime as _runtime

ACCOUNT_ID = ''  # fill in at deploy time; never commit the account
# read-only first pass: flip ALLOW_ORDER_METHODS to True only after the bridge
# is verified online (it gates passorder/cancel).
ALLOW_ORDER_METHODS = False

BIGQMT_REDIS_CONFIG = {
    # 2026-10-26: 策略内任何非白名单 TCP（含回环）都会被《防护》判 illegal ip
    # 并中止。改用 kit 自带的 Windows 命名管道传输：策略只做本机 IPC，
    # 网络访问全部交给 qmt-external-bridge/pipe_redis_bridge.py。
    'transport': 'pipe',
    'redis_enabled': False,
    'rpc_allow_order_methods': ALLOW_ORDER_METHODS,
    'rpc_process_in_listener': True,
    'rpc_listener_methods': ('*',),
    'rpc_background_threads': True,
    'schedule_adjust': True,
    'schedule_adjust_interval': '100nMilliSecond',
    'quote_push': {'enabled': False},
    'exec_events_enabled': False,
    'download_jobs_enabled': False,
}

print('[v2] importing runtime ok; transport=%s orders=%s' % (
    BIGQMT_REDIS_CONFIG['transport'], ALLOW_ORDER_METHODS))

_runtime.configure_runtime_redis(BIGQMT_REDIS_CONFIG)
_runtime.configure_runtime_account(ACCOUNT_ID)

try:
    _extra = _runtime.capture_qmt_injected_funcs(globals())
    print('[v2] qmt injected globals: %s' % sorted(_extra.keys()))
except Exception as _e:
    _extra = {}
    print('[v2] capture_qmt_injected_funcs failed: %r' % (_e,))

try:
    _runtime.bind_runtime_api(
        passorder_func=globals().get('passorder'),
        cancel_func=globals().get('cancel'),
        get_trade_detail_data_func=globals().get('get_trade_detail_data'),
        extra_funcs=_extra or None,
    )
except Exception as _e:
    print('[v2] bind_runtime_api failed: %r' % (_e,))

if not any(_n in globals() for _n in
           ('passorder', 'get_trade_detail_data', 'download_history_data')):
    print('[v2] WARNING: QMT injected none of its API globals - this file is '
          'running as a plain script, not as a mounted strategy. init() will '
          'not be called. Load it under model trading / strategy run.')


# ---- 行情缓冲（Quote Tap）：策略内订阅全推，伴生桥高频 drain ----
_TAP_OK = [False]


def _tap_install(ContextInfo):
    try:
        import qmt_quote_tap as _tap
        ok = _tap.install(ContextInfo, sys.modules)
        if ok:
            print('[v2] quote tap installed')
        return ok
    except Exception as _e:
        print('[v2] quote tap install failed: %r' % (_e,))
        return False


def init(ContextInfo):
    rc = _runtime.init(ContextInfo)
    _TAP_OK[0] = _tap_install(ContextInfo)
    return rc


def handlebar(ContextInfo):
    if not _TAP_OK[0]:
        _TAP_OK[0] = _tap_install(ContextInfo)
    return _runtime.handlebar(ContextInfo)


adjust = _runtime.adjust
order_callback = _runtime.order_callback
deal_callback = _runtime.deal_callback
credit_account_callback = _runtime.credit_account_callback
