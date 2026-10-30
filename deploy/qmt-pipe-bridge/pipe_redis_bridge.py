# -*- coding: utf-8 -*-
"""QMT 管道伴生桥 v2

拓扑:
  quantmind --Redis--> 本进程 --命名管道--> QMT 策略(transport=pipe, 零网络)

职责(各自独立线程, 互不阻塞):
  A. RPC 收发     BLPOP 队列 -> 管道 -> 回写 respq/resp/publish
  B. 持仓快照     3s, bigqmt:positions + position_events (内容不变不写)
  C. 行情轮询     分 A/B 两级 + 分片, 写 market:snapshot:<code> (source=qmt_big)
  D. 委托/成交    轮询 -> order_events / trade_events (状态变化才写)
  E. 健康         心跳 + 指标 (qm:qmt:pipe:*)

策略侧时间是唯一稀缺资源:
  - 预算闸: window_s 内市场类调用累计超过 market_ms 就暂停
  - 让路: RPC 队列非空时市场类调用直接跳过
  - 所有失败退避, 绝不写旧值冒充实时

配置: bridge_config.json (热加载)
"""
import json
import os
import sys
import threading
import time
import traceback
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.environ.get('QMT_VENV_SITE_PACKAGES',
                                   r'C:\xmt\venv\Lib\site-packages'))

import redis  # noqa: E402
from bigqmt_signal_trader.transports.pipe_transport import NamedPipeTransport  # noqa: E402
from bigqmt_signal_trader.redis_rpc import decode_rpc_request_payload  # noqa: E402

CONFIG_PATH = os.path.join(BASE, 'bridge_config.json')
LOG_PATH = os.path.join(BASE, '..', 'logs', 'pipe_bridge.log')

DEFAULT_CONFIG = {
    'redis': {'host': '127.0.0.1', 'port': 6379, 'db': 5},
    'account_id': '',  # set in bridge_config.json
    'pipe': {'name': 'bigqmt_rpc', 'connect_timeout_s': 3.0},
    'rpc': {'timeout_s': 15, 'queue_blpop_s': 0.5},
    'positions': {'enabled': True, 'interval_s': 3, 'ttl_s': 120},
    'quotes': {'enabled': True, 'tier_a_interval_s': 2, 'tier_b_interval_s': 10,
               'chunk': 25, 'max_symbols': 250, 'stale_after_s': 10,
               'indices': ['000001.SH', '399001.SZ', '000300.SH'], 'watchlist': []},
    'orders': {'enabled': True, 'interval_s': 2, 'maxlen': 2000},
    'trades': {'enabled': True, 'interval_s': 5, 'maxlen': 2000},
    'budget': {'window_s': 10, 'market_ms': 2000, 'rpc_preempt_queue': 1,
               # 订单队列积压闸：待处理订单请求 >= N 时直接 BRIDGE_BUSY 快速失败，
               # 避免调用方在单线程管道里干等 25s（2026-10-30 实测头阻塞）。
               'order_preempt_queue': 3},
    'risk': {'enabled': True, 'max_order_amount': 200000, 'max_orders_per_min': 30,
             'max_deviation_pct': 5.0, 'blacklist_codes': [],
             'blacklist_name_keywords': ['ST', '退'],
             # 卖出是减风险动作：默认不做黑名单/金额/偏离/限频拦截（只拦买入）。
             # 2026-10-30 用户拍板：ST 黑名单把清仓也堵死，等于给卖出加风险。
             'apply_to_sells': False,
             # 但卖出保留「错价保护」：限价对昨收偏离超过该带宽即拒（市价单不受限）。
             # 20% 覆盖创业板/科创板涨跌停；主板 10%/ST 5% 由交易所涨跌停兜底。
             # 2026-10-30 实测 9.5% 会把「跌 10% 的票按买一价正常卖出」误杀。
             'sell_price_band_pct': 20.0},
    'reload': {'enabled': True, 'key': 'qm:qmt:pipe:reload'},
    'health': {'heartbeat_key': 'qm:qmt:pipe:heartbeat', 'metrics_key': 'qm:qmt:pipe:metrics',
               'interval_s': 5, 'ttl_s': 30},
    'tap': {'enabled': True, 'interval_s': 0.5, 'subscribe_refresh_s': 60,
            'max_symbols': 250, 'respect_budget': False},
    'source_http': {'enabled': True, 'host': '127.0.0.1', 'port': 17720,
                    'stale_after_s': 30, 'allow_orders': False},
}

# ---------------------------------------------------------------- 日志
_log_lock = threading.Lock()


def log(msg):
    line = '%s %s' % (time.strftime('%Y-%m-%d %H:%M:%S'), msg)
    with _log_lock:
        try:
            print(line, flush=True)
        except Exception:
            pass
        try:
            if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 20 * 1024 * 1024:
                os.replace(LOG_PATH, LOG_PATH + '.1')
            with open(LOG_PATH, 'a', encoding='utf-8') as f:
                f.write(line + '\n')
        except Exception:
            pass


# ---------------------------------------------------------------- 配置
class Config(object):
    def __init__(self):
        self._lock = threading.RLock()
        self._data = json.loads(json.dumps(DEFAULT_CONFIG))
        self._mtime = 0.0
        self.reload(force=True)

    def reload(self, force=False):
        try:
            mtime = os.path.getmtime(CONFIG_PATH)
        except OSError:
            return False
        with self._lock:
            if not force and mtime <= self._mtime:
                return False
            try:
                with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                    user = json.load(f)
            except Exception as exc:
                log('配置读取失败(保留旧配置): %r' % (exc,))
                return False
            merged = json.loads(json.dumps(DEFAULT_CONFIG))
            for section, values in (user or {}).items():
                if isinstance(values, dict) and isinstance(merged.get(section), dict):
                    merged[section].update(values)
                else:
                    merged[section] = values
            self._data = merged
            self._mtime = mtime
            log('配置已加载 mtime=%.0f' % mtime)
            return True

    def get(self, path, default=None):
        with self._lock:
            node = self._data
            for part in path.split('.'):
                if not isinstance(node, dict) or part not in node:
                    return default
                node = node[part]
            return node


CFG = Config()


# ---------------------------------------------------------------- 共享状态
class State(object):
    def __init__(self):
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.symbols_a = []          # 持仓 + 指数
        self.symbols_b = []          # 候选池
        self.name_map = {}           # code -> name(来自持仓)
        self.quote_pre_close = {}    # code -> 昨收(风控用)
        self.quote_ts = {}           # code -> 写入时间
        self.last_rpc_ms = 0.0
        self.last_pos_age = None
        self.last_order_age = None
        self.last_trade_age = None
        self.errors = 0
        self.last_error = ''
        self.rpc_calls = 0
        self.tap_ticks = 0
        self.tap_batches = 0
        self.tap_last_push = 0.0

    def note_error(self, where, exc):
        with self.lock:
            self.errors += 1
            self.last_error = '%s: %s: %s' % (time.strftime('%H:%M:%S'), where, repr(exc)[:200])


STATE = State()


def connect_redis():
    cfg = CFG.get('redis') or {}
    return redis.Redis(host=cfg.get('host', '127.0.0.1'), port=int(cfg.get('port', 6379)),
                       db=int(cfg.get('db', 5)), socket_timeout=5, socket_connect_timeout=5,
                       health_check_interval=30)


R = connect_redis()


def build_transport():
    pipe = CFG.get('pipe') or {}
    return NamedPipeTransport(account_id=str(CFG.get('account_id') or ''),
                              pipe_name=str(pipe.get('name') or 'bigqmt_rpc'),
                              connect_timeout_seconds=float(pipe.get('connect_timeout_s') or 3.0))


TR = build_transport()
_seq = [0]
_seq_lock = threading.Lock()


def next_request_id():
    with _seq_lock:
        _seq[0] += 1
        return 'b%d-%d' % (int(time.time() * 1000) % 100000000, _seq[0])


def rpc(method, params=None, timeout=None):
    """同步调用策略侧方法；返回 data 段，失败抛异常。"""
    req = {'request_id': next_request_id(), 'method': method, 'params': params or {}}
    t0 = time.perf_counter()
    try:
        resp = TR.send_request(req, timeout_seconds=float(timeout or CFG.get('rpc.timeout_s') or 15))
    finally:
        pass
    ms = (time.perf_counter() - t0) * 1000.0
    with STATE.lock:
        STATE.rpc_calls += 1
        STATE.last_rpc_ms = ms
    if not isinstance(resp, dict):
        raise RuntimeError('bad response type: %r' % (type(resp),))
    if not resp.get('ok', False):
        raise RuntimeError(str(resp.get('error') or resp.get('server_error') or 'rpc error'))
    return resp.get('data')


# ---------------------------------------------------------------- 预算闸
class Budget(object):
    def __init__(self):
        self.events = deque()
        self.lock = threading.Lock()

    def add(self, ms):
        now = time.time()
        with self.lock:
            self.events.append((now, ms))
            window = float(CFG.get('budget.window_s') or 10)
            while self.events and now - self.events[0][0] > window:
                self.events.popleft()

    def used_ms(self):
        now = time.time()
        window = float(CFG.get('budget.window_s') or 10)
        with self.lock:
            while self.events and now - self.events[0][0] > window:
                self.events.popleft()
            return sum(ms for _, ms in self.events)

    def allow(self):
        limit = float(CFG.get('budget.market_ms') or 2000)
        return self.used_ms() < limit


BUDGET = Budget()


def rpc_backlog():
    try:
        return int(R.llen('bigqmt:rpc:queue:%s' % CFG.get('account_id')))
    except Exception:
        return 0


def market_call(method, params=None, timeout=None, est_ms=200.0):
    """市场类调用：预算 + 让路 + 记账。返回 data 或抛异常。"""
    preempt = int(CFG.get('budget.rpc_preempt_queue') or 1)
    if rpc_backlog() >= preempt:
        raise RuntimeError('rpc backlog, skipped')
    if not BUDGET.allow():
        raise RuntimeError('market budget exhausted, skipped')
    t0 = time.perf_counter()
    try:
        return rpc(method, params, timeout)
    finally:
        BUDGET.add((time.perf_counter() - t0) * 1000.0)


# ---------------------------------------------------------------- A. RPC
def rpc_worker():
    account = str(CFG.get('account_id'))
    queue = 'bigqmt:rpc:queue:%s' % account
    blpop_s = float(CFG.get('rpc.queue_blpop_s') or 0.5)
    log('rpc worker start queue=%s' % queue)
    while not STATE.stop.is_set():
        try:
            item = R.blpop(queue, timeout=blpop_s)
        except Exception as exc:
            STATE.note_error('rpc.blpop', exc)
            time.sleep(1)
            continue
        if not item:
            continue
        payload = item[1].decode('utf-8', 'replace') if isinstance(item[1], bytes) else item[1]
        try:
            req = json.loads(decode_rpc_request_payload(payload))
        except Exception as exc:
            STATE.note_error('rpc.decode', exc)
            continue
        method = str(req.get('method') or '')
        if is_order_method(method):
            busy = order_backlog_gate(
                rpc_backlog(), CFG.get('budget.order_preempt_queue') or 0)
            if busy is not None:
                resp = {'request_id': req.get('request_id'), 'ok': False, 'error': busy}
                STATE.note_error('rpc.busy', busy)
                try:
                    write_reply(req, resp)
                except Exception as exc:
                    STATE.note_error('rpc.reply', exc)
                continue
        gate = risk_gate(method, req.get('params') or {})
        if gate is not None:
            resp = {'request_id': req.get('request_id'), 'ok': False, 'error': gate}
        else:
            idem = idempotent_lookup(req)
            if idem is not None:
                resp = idem
            else:
                try:
                    resp = TR.send_request(req, timeout_seconds=float(CFG.get('rpc.timeout_s') or 15))
                    idempotent_store(req, resp)
                except Exception as exc:
                    resp = {'request_id': req.get('request_id'), 'ok': False,
                            'error': '%s: %s' % (type(exc).__name__, exc)}
                    STATE.note_error('rpc.forward', exc)
        try:
            write_reply(req, resp)
        except Exception as exc:
            STATE.note_error('rpc.reply', exc)


def write_reply(req, resp):
    raw = json.dumps(resp, ensure_ascii=False, default=str)
    ttl = int(req.get('ttl_seconds') or 60)
    if req.get('reply_key'):
        R.set(req['reply_key'], raw, ex=ttl)
    if req.get('reply_list'):
        R.rpush(req['reply_list'], raw)
        R.expire(req['reply_list'], ttl)
    if req.get('reply_channel'):
        R.publish(req['reply_channel'], raw)


# ---------------------------------------------------------------- 风控 + 幂等
_order_hits = deque()
_order_lock = threading.Lock()


def _order_params(params):
    symbol = params.get('stock_code') or params.get('symbol') or params.get('code') or ''
    volume = params.get('volume') or params.get('order_volume') or params.get('quantity') or 0
    price = params.get('price') or 0
    try:
        volume = float(volume)
        price = float(price)
    except Exception:
        volume, price = 0.0, 0.0
    return str(symbol), volume, price


_SIDE_FIELDS = ('op_type', 'optype', 'action', 'side', 'order_type', 'entrust_bs')
_SIDE_BUY = ('23', 'BUY', 'B', '0')
_SIDE_SELL = ('24', 'SELL', 'S', '1')


def _order_side(params):
    """推断买卖方向。xtquant ``order_stock``/``passorder`` 用 23=买 / 24=卖。"""
    for key in _SIDE_FIELDS:
        value = params.get(key)
        if value is None:
            continue
        text = str(value).strip().upper()
        if text in _SIDE_BUY:
            return 'BUY'
        if text in _SIDE_SELL:
            return 'SELL'
    return ''


def is_order_method(method):
    """订单写方法集合（与 risk_gate / 幂等同源）。"""
    return str(method or '').lower() in ('order_stock', 'passorder', 'submit_order')


def order_backlog_gate(backlog, limit):
    """订单队列积压闸（纯函数）：待处理请求超限即快速失败，别让调用方干等 25s。"""
    if int(limit or 0) <= 0:
        return None
    if int(backlog) >= int(limit):
        return 'BRIDGE_BUSY: order queue backlog=%d >= %d' % (int(backlog), int(limit))
    return None


def _sell_price_band_reason(symbol, price):
    """卖出错价保护：限价对昨收偏离超过带宽即拒；市价单（price<=0）不受限。"""
    band = float(CFG.get('risk.sell_price_band_pct') or 0)
    pre = STATE.quote_pre_close.get(symbol)
    if band <= 0 or price <= 0 or not pre:
        return None
    dev = abs(price / pre - 1) * 100.0
    if dev > band:
        return 'risk: sell price band %.2f%% > %.2f%%' % (dev, band)
    return None


def risk_gate(method, params):
    """下单/撤单前置检查。返回 None=放行，字符串=拒绝原因。"""
    if not bool(CFG.get('risk.enabled')):
        return None
    m = method.lower()
    is_order = m in ('order_stock', 'passorder', 'submit_order')
    is_cancel = m in ('cancel_order_stock', 'cancel_order', 'cancel')
    if not (is_order or is_cancel):
        return None
    if is_cancel:
        return None
    symbol, volume, price = _order_params(params)
    if not symbol:
        return 'risk: empty symbol'
    # 卖出是减风险动作：黑名单/金额/偏离/限频只对买入生效；卖出仅保留错价保护。
    if _order_side(params) == 'SELL' and not bool(CFG.get('risk.apply_to_sells')):
        return _sell_price_band_reason(symbol, price)
    blacklist = [str(x).upper() for x in (CFG.get('risk.blacklist_codes') or [])]
    if symbol.upper() in blacklist:
        return 'risk: blacklisted code %s' % symbol
    name = STATE.name_map.get(symbol, '')
    for kw in (CFG.get('risk.blacklist_name_keywords') or []):
        if kw and kw in name:
            return 'risk: blacklisted name %s(%s)' % (symbol, name)
    ref = STATE.quote_pre_close.get(symbol) or price
    amount = volume * (price or ref)
    limit = float(CFG.get('risk.max_order_amount') or 0)
    if limit > 0 and amount > limit:
        return 'risk: amount %.0f > %.0f' % (amount, limit)
    dev_limit = float(CFG.get('risk.max_deviation_pct') or 0)
    pre = STATE.quote_pre_close.get(symbol)
    if price > 0 and pre and dev_limit > 0:
        dev = abs(price / pre - 1) * 100.0
        if dev > dev_limit:
            return 'risk: price deviation %.2f%% > %.2f%%' % (dev, dev_limit)
    with _order_lock:
        now = time.time()
        while _order_hits and now - _order_hits[0] > 60:
            _order_hits.popleft()
        rate_limit = int(CFG.get('risk.max_orders_per_min') or 0)
        if rate_limit > 0 and len(_order_hits) >= rate_limit:
            return 'risk: rate limit %d/min' % rate_limit
        _order_hits.append(now)
    return None


def _idem_key(req):
    params = req.get('params') or {}
    # xtquant ``order_stock`` 把幂等键放在 ``order_remark``；HTTP 路由用 ``remark``。
    # 只认 remark/order_id 时 order_stock 永远拿不到键 → 幂等等于没开（2026-10-30 实测）。
    key = (params.get('remark') or params.get('order_remark')
           or params.get('order_id') or params.get('user_order_id'))
    if not key:
        return None
    return 'qm:qmt:pipe:idem:%s:%s' % (CFG.get('account_id'), key)


def idempotent_lookup(req):
    if str(req.get('method') or '').lower() not in ('order_stock', 'passorder', 'submit_order'):
        return None
    key = _idem_key(req)
    if not key:
        return None
    try:
        raw = R.get(key)
        if raw:
            return json.loads(raw if isinstance(raw, str) else raw.decode('utf-8'))
    except Exception:
        return None
    return None


def idempotent_store(req, resp):
    key = _idem_key(req)
    if not key:
        return
    try:
        if not resp.get('ok'):
            return
        R.set(key, json.dumps(resp, ensure_ascii=False, default=str), ex=86400)
    except Exception:
        pass


# ---------------------------------------------------------------- B. 持仓
_last_pos_material = [None]


def positions_worker():
    account = str(CFG.get('account_id'))
    key = 'bigqmt:positions:%s' % account
    stream = 'bigqmt:position_events:%s' % account
    log('positions worker start')
    while not STATE.stop.is_set():
        interval = float(CFG.get('positions.interval_s') or 3)
        if not bool(CFG.get('positions.enabled')):
            time.sleep(max(1.0, interval))
            continue
        try:
            asset = market_call('get_asset', {}, est_ms=100)
            pos = market_call('get_positions', {}, est_ms=150)
            payload = build_position_payload(asset, pos)
            raw = json.dumps(payload, ensure_ascii=False)
            R.set(key, raw, ex=int(CFG.get('positions.ttl_s') or 120))
            material = json.dumps({k: v for k, v in payload.items() if k != 'updated_at'},
                                  ensure_ascii=False, sort_keys=True)
            if material != _last_pos_material[0]:
                _last_pos_material[0] = material
                R.xadd(stream, {'payload': raw}, maxlen=2000, approximate=True)
            with STATE.lock:
                STATE.last_pos_age = time.time()
                STATE.symbols_a = sorted(set(list(payload['positions'].keys()) +
                                             list(CFG.get('quotes.indices') or [])))[:int(CFG.get('quotes.max_symbols') or 250)]
                STATE.name_map = dict((c, v.get('stock_name') or '') for c, v in payload['positions'].items())
        except Exception as exc:
            if 'skipped' not in str(exc):
                STATE.note_error('positions', exc)
        time.sleep(max(1.0, interval))


def build_position_payload(asset, pos):
    a = asset or {}
    rows = pos or {}
    if isinstance(rows, list):
        rows = dict((r.get('stock_code'), r) for r in rows if isinstance(r, dict) and r.get('stock_code'))
    positions = {}
    for code, row in (rows or {}).items():
        if not isinstance(row, dict):
            continue
        positions[code] = {
            'stock_code': row.get('stock_code') or code,
            'volume': row.get('volume'),
            'available': row.get('available', row.get('can_use_volume')),
            'cost': row.get('cost', row.get('open_price')),
            'stock_name': row.get('stock_name') or row.get('instrument_name') or '',
        }
    return {
        'account_id': str(CFG.get('account_id')),
        'reason': 'bridge',
        'updated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'asset': {
            'cash': a.get('cash'),
            'total_asset': a.get('total_asset'),
            'frozen_cash': a.get('frozen_cash'),
            'market_value': a.get('market_value'),
        },
        'positions': positions,
    }


# ---------------------------------------------------------------- C. 行情
def _chunks(items, size):
    size = max(1, int(size))
    return [items[i:i + size] for i in range(0, len(items), size)]


def write_snapshot(code, tick):
    if not isinstance(tick, dict):
        return
    ts = time.time()
    key = 'market:snapshot:%s' % code
    bid_p = tick.get('bidPrice') or []
    bid_v = tick.get('bidVol') or []
    ask_p = tick.get('askPrice') or []
    ask_v = tick.get('askVol') or []
    mapping = {
        'price': tick.get('lastPrice'),
        'last': tick.get('lastPrice'),
        'open': tick.get('open'),
        'high': tick.get('high'),
        'low': tick.get('low'),
        'pre_close': tick.get('lastClose'),
        'volume': tick.get('volume'),
        'amount': tick.get('amount'),
        'ts': '%.3f' % ts,
        'source': 'qmt_big',
    }
    for i in range(5):
        mapping['bid%d_p' % (i + 1)] = bid_p[i] if i < len(bid_p) else None
        mapping['bid%d_v' % (i + 1)] = bid_v[i] if i < len(bid_v) else None
        mapping['ask%d_p' % (i + 1)] = ask_p[i] if i < len(ask_p) else None
        mapping['ask%d_v' % (i + 1)] = ask_v[i] if i < len(ask_v) else None
    pipe = R.pipeline()
    pipe.hset(key, mapping=mapping)
    pipe.expire(key, int(CFG.get('quotes.stale_after_s') or 10))
    pipe.execute()
    with STATE.lock:
        STATE.quote_ts[code] = ts
        if tick.get('lastClose'):
            STATE.quote_pre_close[code] = float(tick['lastClose'])


def poll_quotes(symbols):
    if not symbols:
        return
    data = market_call('get_full_tick', {'codes': list(symbols)},
                       est_ms=100 + 4 * len(symbols))
    if isinstance(data, dict):
        for code, tick in data.items():
            write_snapshot(code, tick)


def quotes_worker():
    log('quotes worker start')
    next_a = next_b = 0.0
    ia = ib = 0
    while not STATE.stop.is_set():
        if not bool(CFG.get('quotes.enabled')):
            time.sleep(1)
            continue
        now = time.time()
        with STATE.lock:
            syms_a = list(STATE.symbols_a)
            syms_b = [s for s in (CFG.get('quotes.watchlist') or []) if s]
        max_syms = int(CFG.get('quotes.max_symbols') or 250)
        syms_b = syms_b[:max(0, max_syms - len(syms_a))]
        chunk = int(CFG.get('quotes.chunk') or 25)
        chunks_a = _chunks(syms_a, chunk)
        chunks_b = _chunks(syms_b, chunk)
        slept = False
        tap_on = _TAP_STATE.get('state') == 'on' and (now - max(STATE.tap_last_push, 0)) < 5
        if tap_on:
            next_a = now + 0.5
        if chunks_a and not tap_on and now >= next_a:
            part = chunks_a[ia % len(chunks_a)]
            ia += 1
            try:
                poll_quotes(part)
            except Exception as exc:
                if 'skipped' not in str(exc):
                    STATE.note_error('quotes.A', exc)
                time.sleep(0.2)
                slept = True
            next_a = now + max(0.2, float(CFG.get('quotes.tier_a_interval_s') or 2) / len(chunks_a))
        if not slept and chunks_b and now >= next_b:
            part = chunks_b[ib % len(chunks_b)]
            ib += 1
            try:
                poll_quotes(part)
            except Exception as exc:
                if 'skipped' not in str(exc):
                    STATE.note_error('quotes.B', exc)
                time.sleep(0.2)
            next_b = now + max(0.5, float(CFG.get('quotes.tier_b_interval_s') or 10) / len(chunks_b))
        time.sleep(0.1)


# ---------------------------------------------------------------- D. 委托/成交
_order_seen = {}
_trade_seen = {}


def _order_fingerprint(row):
    return str(row.get('order_id') or row.get('m_strOrderID') or row.get('order_sysid') or
               row.get('m_strOrderSysID') or row.get('m_nOrderRef') or '') + '|' + \
           str(row.get('status') or row.get('m_nOrderStatus') or row.get('order_status') or '')


def _trade_fingerprint(row):
    return str(row.get('trade_id') or row.get('m_strTradeID') or row.get('traded_id') or
               row.get('m_strTradeId') or '') + '|' + str(row.get('traded_volume') or row.get('m_nVolume') or '')


def events_worker(kind):
    account = str(CFG.get('account_id'))
    stream = 'bigqmt:%s_events:%s' % (kind, account)
    method = 'query_stock_orders' if kind == 'order' else 'query_stock_trades'
    seen = _order_seen if kind == 'order' else _trade_seen
    fp = _order_fingerprint if kind == 'order' else _trade_fingerprint
    log('%s events worker start' % kind)
    while not STATE.stop.is_set():
        interval = float(CFG.get('%ss.interval_s' % kind) or (2 if kind == 'order' else 5))
        if not bool(CFG.get('%ss.enabled' % kind)):
            time.sleep(max(1.0, interval))
            continue
        try:
            rows = market_call(method, {}, est_ms=120)
            if isinstance(rows, list):
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    key = fp(row)
                    raw_state = json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
                    if seen.get(key) == raw_state:
                        continue
                    seen[key] = raw_state
                    payload = {'account_id': account, 'kind': kind,
                               'updated_at': time.strftime('%Y-%m-%d %H:%M:%S'), 'row': row}
                    R.xadd(stream, {'payload': json.dumps(payload, ensure_ascii=False, default=str)},
                           maxlen=int(CFG.get('%ss.maxlen' % kind) or 2000), approximate=True)
            with STATE.lock:
                if kind == 'order':
                    STATE.last_order_age = time.time()
                else:
                    STATE.last_trade_age = time.time()
        except Exception as exc:
            if 'skipped' not in str(exc):
                STATE.note_error('%ss' % kind, exc)
        time.sleep(max(1.0, interval))


# ---------------------------------------------------------------- F. Quote Tap
_TAP_STATE = {'state': 'init', 'subscribed': [], 'last_error': '', 'last_drain_ms': 0.0}


def _tap_universe():
    with STATE.lock:
        syms = list(STATE.symbols_a)
    watch = [str(x) for x in (CFG.get('quotes.watchlist') or []) if x]
    out, seen = [], set()
    for code in syms + watch:
        if code not in seen:
            seen.add(code)
            out.append(code)
    return out[:int(CFG.get('tap.max_symbols') or 250)]


def tap_worker():
    last_sub = 0.0
    log('quote tap worker start')
    while not STATE.stop.is_set():
        interval = float(CFG.get('tap.interval_s') or 0.5)
        if not bool(CFG.get('tap.enabled')):
            if _TAP_STATE['state'] != 'disabled':
                _TAP_STATE['state'] = 'disabled'
                log('quote tap disabled by config')
            time.sleep(max(0.5, interval))
            continue
        try:
            now = time.time()
            universe = _tap_universe()
            refresh = float(CFG.get('tap.subscribe_refresh_s') or 60)
            if (not _TAP_STATE['subscribed'] or set(universe) != set(_TAP_STATE['subscribed'])
                    or now - last_sub > refresh):
                rpc('quote_tap_subscribe', {'codes': universe}, timeout=10)
                last_sub = now
                _TAP_STATE['subscribed'] = universe
                _TAP_STATE['state'] = 'on'
                log('quote tap subscribed %d codes' % len(universe))
            if bool(CFG.get('tap.respect_budget', False)) and rpc_backlog() >= int(CFG.get('budget.rpc_preempt_queue') or 1):
                time.sleep(interval)
                continue
            t0 = time.perf_counter()
            data = rpc('quote_drain', {}, timeout=10) or {}
            _TAP_STATE['last_drain_ms'] = (time.perf_counter() - t0) * 1000.0
            ticks = data.get('ticks') or {}
            for code, tick in ticks.items():
                write_snapshot(code, tick)
            with STATE.lock:
                STATE.tap_ticks += len(ticks)
                STATE.tap_batches = data.get('batches') or 0
                STATE.tap_last_push = data.get('last_push_ts') or 0.0
        except Exception as exc:
            msg = str(exc)
            if 'not allowed' in msg or 'not implemented' in msg:
                _TAP_STATE['state'] = 'unavailable'
                _TAP_STATE['last_error'] = msg[:160]
                log('quote tap unavailable, fallback to polling: %s' % msg[:120])
                time.sleep(10)
                continue
            STATE.note_error('tap', exc)
        time.sleep(max(0.1, interval))


def _metrics_snapshot():
    now = time.time()
    with STATE.lock:
        return {
            'state': 'ok',
            'account_id': str(CFG.get('account_id')),
            'last_rpc_ms': round(STATE.last_rpc_ms, 1),
            'pos_age_s': round(now - STATE.last_pos_age, 1) if STATE.last_pos_age else -1,
            'order_age_s': round(now - STATE.last_order_age, 1) if STATE.last_order_age else -1,
            'trade_age_s': round(now - STATE.last_trade_age, 1) if STATE.last_trade_age else -1,
            'quote_symbols': len(STATE.quote_ts),
            'quote_age_max_s': round(now - max(STATE.quote_ts.values()), 1) if STATE.quote_ts else -1,
            'tap_state': _TAP_STATE['state'],
            'tap_ticks': STATE.tap_ticks,
            'tap_batches': STATE.tap_batches,
            'tap_age_s': round(now - STATE.tap_last_push, 1) if STATE.tap_last_push else -1,
            'tap_drain_ms': round(_TAP_STATE['last_drain_ms'], 1),
            'tap_subscribed': len(_TAP_STATE['subscribed']),
            'rpc_calls': STATE.rpc_calls,
            'errors': STATE.errors,
            'last_error': STATE.last_error or _TAP_STATE['last_error'],
            'duty_ms_10s': round(BUDGET.used_ms(), 0),
            'queue_len': rpc_backlog(),
            'updated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        }


# ---------------------------------------------------------------- G. QMT 数据源 HTTP
# 只读白名单：行情 + 账户。下单/撤单**不在此列**——伴生桥不开交易后门。
_SOURCE_METHODS = {'get_market_snapshot', 'snapshot', 'get_full_tick', 'get_market_data',
                   'get_market_data_ex', 'get_trading_dates', 'quote_tap_status',
                   'ping', 'get_asset', 'get_positions', 'account_snapshot',
                   'query_orders', 'query_trades', 'get_instrument_detail',
                   # 只读诊断：资产行全字段 / ORDER/DEAL 字段名（不返回订单值）
                   'query_account_infos', 'describe_trade_detail_fields',
                   # 写入面：默认关（source_http.allow_orders=false），且策略
                   # ALLOW_ORDER_METHODS 必须同时打开；风控/幂等在本层执行
                   'order_stock', 'submit_order',
                   'cancel_order_stock', 'cancel_order'}


def _redis_hash(code_key):
    raw = R.hgetall(code_key) or {}
    data = {}
    for k, v in raw.items():
        key = k.decode('utf-8') if isinstance(k, bytes) else k
        val = v.decode('utf-8') if isinstance(v, bytes) else v
        try:
            data[key] = float(val)
        except (TypeError, ValueError):
            data[key] = val
    return data


def _account_snapshot():
    """账户/持仓快照（positions 线程每 3s 写入，零 RPC）+ 最新价合并。

    休市/策略主线程不 tick 时 ``bigqmt:positions`` 会因 TTL 过期；
    这时回落到 ``position_events`` 的最后一条（QMT 自己写的），
    并把 ``stale`` 标出来——桥上有"最后一次可用"，但不冒充实时。

    持仓价只认 ``market:snapshot`` 里真实且**未过期**的 last；没有就保持 None，
    绝不用成本价冒充现价（会把浮盈做成 0）。
    """
    account = str(CFG.get('account_id'))
    stale_after = float(CFG.get('source_http.stale_after_s') or 30)
    source = 'snapshot'
    raw = R.get('bigqmt:positions:%s' % account)
    if not raw:
        # 快照键过期：回落到 event stream 最后一条
        source = 'stream'
        try:
            entries = R.xrevrange('bigqmt:position_events:%s' % account, count=1)
        except Exception:
            entries = []
        if entries:
            _entry_id, fields = entries[0]
            raw = fields.get(b'payload') if isinstance(fields, dict) else None
            if raw is None and isinstance(fields, dict):
                raw = fields.get('payload')
    if not raw:
        raise ValueError('account snapshot is empty (positions worker off / QMT offline)')
    try:
        payload = json.loads(raw.decode('utf-8') if isinstance(raw, bytes) else raw)
    except Exception as exc:
        raise ValueError('account snapshot is not valid json: %r' % (exc,))
    positions = payload.get('positions') or {}
    updated_at = payload.get('updated_at')
    age = None
    if updated_at:
        try:
            age = max(0.0, time.time() - time.mktime(
                time.strptime(updated_at, '%Y-%m-%d %H:%M:%S')))
        except (TypeError, ValueError):
            age = None
    # 账户快照本身过期时**不合并当前行情**：旧量配新价会把浮盈做成假实时。
    # 宁可整份都按快照时间展示，也不混两个时点的数据。
    stale = age is None or age > stale_after
    codes = list(positions.keys())
    if codes and not stale:
        pipe = R.pipeline()
        for code in codes:
            pipe.hgetall('market:snapshot:%s' % code)
        rows = pipe.execute()
        for code, row in zip(codes, rows):
            data = {}
            for k, v in (row or {}).items():
                key = k.decode('utf-8') if isinstance(k, bytes) else k
                val = v.decode('utf-8') if isinstance(v, bytes) else v
                try:
                    data[key] = float(val)
                except (TypeError, ValueError):
                    data[key] = val
            price = data.get('price', data.get('last'))
            if price is None:
                continue
            # 行情也要新鲜：休市时 market:snapshot 可能还是上午的价
            try:
                quote_ts = float(data.get('ts'))
                if time.time() - quote_ts > stale_after:
                    continue
            except (TypeError, ValueError):
                continue
            try:
                price = float(price)
                volume = float(positions[code].get('volume') or 0)
                cost = positions[code].get('cost')
                positions[code]['last_price'] = price
                positions[code]['market_value'] = round(price * volume, 2)
                if cost is not None:
                    positions[code]['pnl'] = round(
                        (price - float(cost)) * volume, 2)
            except (TypeError, ValueError):
                continue
    payload['age_s'] = round(age, 1) if age is not None else None
    payload['snapshot_source'] = source
    payload['stale'] = bool(stale)
    return payload


def _snapshot_from_tick(code):
    """快照键没命中时回落到 get_full_tick（桥通用透传的兼容语义）。"""
    result = rpc('get_full_tick', {'codes': [code]}, timeout=15) or {}
    tick = result.get(code) if isinstance(result, dict) else None
    if not isinstance(tick, dict) or not tick:
        return None
    bid_p = tick.get('bidPrice') or []
    bid_v = tick.get('bidVol') or []
    ask_p = tick.get('askPrice') or []
    ask_v = tick.get('askVol') or []
    amount = tick.get('amount')
    return {
        'Code': code,
        'Now': tick.get('lastPrice'),
        'LastClose': tick.get('lastClose'),
        'Open': tick.get('open'),
        'Max': tick.get('high'),
        'Min': tick.get('low'),
        # 与 TDX 快照同口径：Amount 单位万元（QMT tick 是元）
        'Amount': (float(amount) / 10000.0) if amount is not None else None,
        'Volume': tick.get('volume'),
        'Buyp': list(bid_p[:5]), 'Buyv': list(bid_v[:5]),
        'Sellp': list(ask_p[:5]), 'Sellv': list(ask_v[:5]),
        'source': 'qmt_tick',
    }


def _source_call(method, params):
    method = str(method or '').strip()
    params = dict(params or {})
    if method not in _SOURCE_METHODS:
        raise ValueError('method not allowed on qmt source: %s' % method)
    if method in ('get_market_snapshot', 'snapshot'):
        code = params.get('stock_code') or params.get('code') or params.get('symbol')
        if not code:
            raise ValueError('stock_code is required')
        data = _redis_hash('market:snapshot:%s' % code)
        if not data:
            data = _snapshot_from_tick(code)
        if not data:
            raise ValueError('no snapshot/tick for %s' % code)
        return data
    if method == 'account_snapshot':
        return _account_snapshot()
    if method == 'ping':
        return rpc('ping', {}, timeout=10)
    if method == 'get_asset':
        return rpc('get_asset', {}, timeout=15)
    if method == 'get_positions':
        return rpc('get_positions', {}, timeout=20)
    if method == 'query_orders':
        req = {'cancelable_only': bool(params.get('cancelable_only'))}
        if params.get('strategy_name') is not None:
            req['strategy_name'] = str(params.get('strategy_name'))
        return rpc('query_orders', req, timeout=25)
    if method == 'query_trades':
        req = {}
        if params.get('strategy_name') is not None:
            req['strategy_name'] = str(params.get('strategy_name'))
        return rpc('query_trades', req, timeout=25)
    if method in ('order_stock', 'submit_order'):
        if not bool(CFG.get('source_http.allow_orders')):
            raise ValueError('orders disabled on qmt source')
        gate = risk_gate(method, params)
        if gate:
            raise ValueError(gate)
        request = {'method': method, 'params': params}
        hit = idempotent_lookup(request)
        if hit is not None:
            return hit
        result = rpc('order_stock', params, timeout=30)
        key = _idem_key(request)
        status = (str((result or {}).get('status') or '').upper()
                  if isinstance(result, dict) else '')
        if key and status not in ('REJECTED', 'ERROR'):
            try:
                R.set(key, json.dumps(result, ensure_ascii=False, default=str),
                      ex=86400)
            except Exception:
                pass
        return result
    if method in ('cancel_order_stock', 'cancel_order'):
        if not bool(CFG.get('source_http.allow_orders')):
            raise ValueError('orders disabled on qmt source')
        return rpc('cancel_order_stock', params, timeout=30)
    if method == 'get_instrument_detail':
        code = params.get('code') or params.get('stock_code')
        if not code:
            raise ValueError('code is required')
        return rpc('get_instrument_detail', {'code': str(code)}, timeout=15)
    if method == 'quote_tap_status':
        return rpc('quote_tap_status', {}, timeout=10)
    if method == 'get_full_tick':
        codes = params.get('codes') or params.get('stock_list') or []
        if isinstance(codes, str):
            codes = [codes]
        return rpc('get_full_tick', {'codes': list(codes)}, timeout=15)
    if method in ('get_market_data', 'get_market_data_ex'):
        return rpc('get_market_data_ex', params, timeout=30)
    if method == 'get_trading_dates':
        return rpc('get_trading_dates', params, timeout=15)
    if method == 'query_account_infos':
        # 资产行全字段（诊断 total_asset 与 cash+mv 的差额）
        return rpc('query_account_infos', params, timeout=20)
    if method == 'describe_trade_detail_fields':
        # 只返回字段名/形状，不返回订单值
        return rpc('describe_trade_detail_fields', params, timeout=20)
    raise ValueError('unsupported method: %s' % method)


class _QmtSourceHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False, default=str).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def do_GET(self):
        if self.path.startswith('/health'):
            self._send(200, {'success': True, 'source': 'qmt', 'metrics': _metrics_snapshot()})
        else:
            self._send(404, {'success': False, 'error': 'not found'})

    def do_POST(self):
        if not self.path.startswith('/api/v1/qmt/call'):
            self._send(404, {'success': False, 'error': 'not found'})
            return
        try:
            length = int(self.headers.get('Content-Length') or 0)
            raw = self.rfile.read(length) if length else b'{}'
            req = json.loads(raw.decode('utf-8'))
        except Exception as exc:
            self._send(400, {'success': False, 'error': 'invalid json: %r' % (exc,)})
            return
        method = req.get('method')
        params = req.get('params') or {}
        try:
            result = _source_call(method, params)
            self._send(200, {'success': True, 'method': method, 'result': result, 'source': 'qmt'})
        except Exception as exc:
            self._send(400, {'success': False, 'method': method, 'error': str(exc), 'source': 'qmt'})


def source_http_worker():
    if not bool(CFG.get('source_http.enabled')):
        return
    host = str(CFG.get('source_http.host') or '127.0.0.1')
    port = int(CFG.get('source_http.port') or 17720)
    try:
        srv = ThreadingHTTPServer((host, port), _QmtSourceHandler)
        srv.daemon_threads = True
        log('qmt source http listening %s:%d' % (host, port))
        srv.serve_forever()
    except Exception as exc:
        log('qmt source http failed: %r' % (exc,))


# ---------------------------------------------------------------- E. 健康
def health_worker():
    hb_key = str(CFG.get('health.heartbeat_key'))
    metrics_key = str(CFG.get('health.metrics_key'))
    ttl = int(CFG.get('health.ttl_s') or 30)
    log('health worker start')
    while not STATE.stop.is_set():
        interval = float(CFG.get('health.interval_s') or 5)
        try:
            now = time.time()
            metrics = _metrics_snapshot()
            pipe = R.pipeline()
            pipe.set(hb_key, str(int(now)), ex=ttl)
            pipe.hset(metrics_key, mapping=metrics)
            pipe.expire(metrics_key, 120)
            pipe.execute()
        except Exception as exc:
            log('health failed: %r' % (exc,))
        time.sleep(max(1.0, interval))


# ---------------------------------------------------------------- 主循环
def watchdog_config_loop():
    while not STATE.stop.is_set():
        CFG.reload()
        try:
            if bool(CFG.get('reload.enabled')):
                key = str(CFG.get('reload.key') or 'qm:qmt:pipe:reload')
                if R.get(key):
                    # 自重启：Windows 上 os.execv 不引用含空格的 sys.executable
                    # （实测 "C:\Program Files\..." 被拆成两个参数，进程直接死）。
                    # 用 subprocess 拉起新进程，成功才退出；失败保持原进程存活——
                    # 绝不能因一次热重载把桥弄下线（2026-10-30 实测看门狗兜不住）。
                    spawn = None
                    try:
                        import subprocess
                        spawn = subprocess.Popen(
                            [sys.executable, '-u', os.path.abspath(__file__)],
                            close_fds=True,
                            creationflags=(
                                getattr(subprocess, 'DETACHED_PROCESS', 0)
                                | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)
                            ),
                        )
                    except Exception as exc:
                        log('reload spawn failed: %r (keep running)' % (exc,))
                        R.delete(key)
                        time.sleep(10)
                        continue
                    R.delete(key)
                    log('reload requested via %s -> spawned pid=%s, exiting old' %
                        (key, getattr(spawn, 'pid', '?')))
                    time.sleep(0.2)
                    os._exit(0)
        except Exception as exc:
            STATE.note_error('reload.check', exc)
        time.sleep(10)


def main():
    log('pipe bridge v2 starting account=%s pipe=%s' %
        (CFG.get('account_id'), CFG.get('pipe.name')))
    threads = [
        threading.Thread(target=watchdog_config_loop, daemon=True, name='cfg'),
        threading.Thread(target=rpc_worker, daemon=True, name='rpc'),
        threading.Thread(target=positions_worker, daemon=True, name='pos'),
        threading.Thread(target=quotes_worker, daemon=True, name='quotes'),
        threading.Thread(target=tap_worker, daemon=True, name='tap'),
        threading.Thread(target=source_http_worker, daemon=True, name='source_http'),
        threading.Thread(target=events_worker, args=('order',), daemon=True, name='orders'),
        threading.Thread(target=events_worker, args=('trade',), daemon=True, name='trades'),
        threading.Thread(target=health_worker, daemon=True, name='health'),
    ]
    for t in threads:
        t.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        STATE.stop.set()
        log('stopping by KeyboardInterrupt')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        log('FATAL\n' + traceback.format_exc())
        raise
