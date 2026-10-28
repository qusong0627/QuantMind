# -*- coding: utf-8 -*-
"""QMT 策略侧行情缓冲（Quote Tap）

目的：命名管道是请求-响应模型，策略不能主动推。这里在策略内用
ContextInfo.subscribe_whole_quote 订阅全推行情，把 tick 缓存在内存，
再由伴生桥以高频（默认 0.5s）调用 quote_drain 抽干。

- 不联网、不写文件，仍满足《防护》
- 每个 code 只保留最新一笔；drain 返回"自上次以来变化过的"
- 通过挂载三个 RPC 方法暴露给伴生桥：
    quote_tap_subscribe {codes: [...]}   订阅/换订阅
    quote_drain {full: bool, limit: int} 抽干（full=true 取全量最新）
    quote_tap_status {}                  诊断
"""
import sys
import threading
import time
import types

_LOCK = threading.Lock()
_BUF = {}
_LATEST = {}
_CTX = [None]
_STATE = {
    'installed': False, 'sub_id': None, 'codes': [], 'batches': 0, 'ticks': 0,
    'last_push_ts': 0.0, 'installed_at': 0.0, 'errors': 0, 'last_error': '',
}


def _on_push(data):
    ts = time.time()
    with _LOCK:
        try:
            items = data.items() if hasattr(data, 'items') else []
            for code, tick in items:
                if isinstance(tick, dict):
                    _BUF[code] = tick
                    _LATEST[code] = tick
                    _STATE['ticks'] += 1
            _STATE['batches'] += 1
            _STATE['last_push_ts'] = ts
        except Exception as exc:
            _STATE['errors'] += 1
            _STATE['last_error'] = repr(exc)[:200]


def subscribe_codes(codes):
    ctx = _CTX[0]
    if ctx is None:
        raise RuntimeError('quote tap not installed (no ContextInfo)')
    codes = [str(c) for c in (codes or []) if c]
    if not codes:
        raise ValueError('codes is required')
    old = _STATE.get('sub_id')
    if old:
        try:
            ctx.unsubscribe_quote(old)
        except Exception:
            pass
    sub_id = ctx.subscribe_whole_quote(codes, callback=_on_push)
    with _LOCK:
        _STATE['sub_id'] = sub_id
        _STATE['codes'] = codes
    return {'sub_id': sub_id, 'count': len(codes)}


def drain(params=None):
    params = params or {}
    full = bool(params.get('full'))
    limit = int(params.get('limit') or 0)
    with _LOCK:
        ticks = dict(_LATEST) if full else dict(_BUF)
        if not full:
            _BUF.clear()
        batches = _STATE['batches']
        last_ts = _STATE['last_push_ts']
    if limit and len(ticks) > limit:
        ticks = dict(list(ticks.items())[:limit])
    return {'ticks': ticks, 'batches': batches, 'last_push_ts': last_ts}


def status():
    with _LOCK:
        out = dict(_STATE)
        out['buffered'] = len(_BUF)
        out['known'] = len(_LATEST)
        out['now'] = time.time()
    return out


def _attach(strategy_modules):
    strat = strategy_modules.get('bigqmt_signal_trader_strategy')
    svc = getattr(strat, '_rpc_service', None) if strat else None
    handlers = getattr(svc, 'handlers', None) if svc else None
    if handlers is None:
        return None
    allowed = set(getattr(handlers, 'allowed_methods', set()))
    allowed |= {'quote_drain', 'quote_tap_subscribe', 'quote_tap_status'}
    handlers.allowed_methods = allowed
    handlers._handle_quote_drain = types.MethodType(lambda self, p: drain(p), handlers)
    handlers._handle_quote_tap_subscribe = types.MethodType(
        lambda self, p: subscribe_codes(p.get('codes') if isinstance(p, dict) else None), handlers)
    handlers._handle_quote_tap_status = types.MethodType(lambda self, p: status(), handlers)
    return handlers


def install(context_info, strategy_modules=None):
    _CTX[0] = context_info
    if _STATE['installed']:
        return True
    handlers = _attach(strategy_modules or sys.modules)
    if handlers is None:
        return False
    _STATE['installed'] = True
    _STATE['installed_at'] = time.time()
    print('[quote_tap] installed: quote_tap_subscribe / quote_drain / quote_tap_status')
    return True
