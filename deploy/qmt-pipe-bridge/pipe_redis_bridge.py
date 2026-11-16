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
sys.path.insert(0, BASE)

# ---- 单实例锁（必须在 import redis / NamedPipeTransport 之前）----------
# 第二个进程若在 import/构造管道阶段才退出，会先卡在管道连接上；看门狗
# 再拉一个，就堆成多个 python.exe 抢同一条管道。TCP 17719 跨会话、不依赖
# SMB 文件锁、不需要特权；进程退出由 OS 释放。
_INSTANCE_SOCK = None
_INSTANCE_LOCK_PORT = 17719


def _try_bind_instance_lock():
    """尝试绑定单实例 TCP 锁；True=拿到，False=已有实例，异常=不拦启动。"""
    global _INSTANCE_SOCK
    try:
        import socket

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        exclusive = getattr(socket, 'SO_EXCLUSIVEADDRUSE', None)
        if exclusive is not None:
            sock.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        sock.bind(('127.0.0.1', _INSTANCE_LOCK_PORT))
        sock.listen(1)
        _INSTANCE_SOCK = sock
        return True
    except OSError:
        return False
    except Exception:  # noqa: BLE001 - 锁不可用时不拦启动
        return True


def _acquire_single_instance():
    global _INSTANCE_SOCK
    if os.name != 'nt':
        return True
    if _try_bind_instance_lock():
        return True
    # 自重启：旧进程先 spawn 新进程、随后 os._exit(0)。新进程启动时旧进程
    # 可能还占着 17719，若立即退出会被看成"重复实例"，留下无桥空窗
    # （2026-11-16 上线实测）。reload 子进程给旧进程最多 5s 释放锁。
    if os.environ.get('QM_RELOAD_CHILD') != '1':
        return False
    for _ in range(20):
        time.sleep(0.25)
        if _try_bind_instance_lock():
            return True
    return False


if not _acquire_single_instance():
    print('%s another pipe bridge is already running; exiting'
          % time.strftime('%Y-%m-%d %H:%M:%S'), file=sys.stderr, flush=True)
    os._exit(0)

import redis  # noqa: E402
from bigqmt_signal_trader.transports.pipe_transport import NamedPipeTransport  # noqa: E402
from bigqmt_signal_trader.redis_rpc import decode_rpc_request_payload  # noqa: E402
import bridge_gates  # noqa: E402
import bridge_account_state as account_state  # noqa: E402
import bridge_order_intent as order_intent  # noqa: E402

CONFIG_PATH = os.path.join(BASE, 'bridge_config.json')
LOG_PATH = os.path.join(BASE, '..', 'logs', 'pipe_bridge.log')
PID_PATH = os.path.join(BASE, '..', 'data', 'pipe_bridge.pid')

DEFAULT_CONFIG = {
    'redis': {'host': '127.0.0.1', 'port': 6379, 'db': 5},
    'account_id': '',  # set in bridge_config.json
    'pipe': {'name': 'bigqmt_rpc', 'connect_timeout_s': 3.0},
    'rpc': {'timeout_s': 15, 'queue_blpop_s': 0.5,
            # 订单意图：原子取件 + inflight 记录；TTL 覆盖一个交易日即可。
            # processing 列表是 BLMOVE 的落点，inflight hash 是"这笔单是谁、
            # 走到哪一步"的可读账本；两者任一非空，看门狗都不得重启桥。
            'inflight_ttl_s': 86400, 'processing_max': 5000,
            # 交易类 RPC 最小间隔：QMT 的 get_trade_detail_data 连续查询会阻塞，
            # 空载可自愈（2026-11-06 实测），把速率压下来是唯一可控变量。
            # 双桶：容器请求与桥内部轮询各自限速，互不饿死（内部抢光令牌会让
            # SLTP/委托轮询完全查不到柜台）。
            # 容量 3：手动「测试连接」要连发 ping+asset+positions，容量 1 会被
            # 后台轮询抢光令牌（2026-11-06 用户现场）。持续速率不变。
            'trade_min_interval_s': 3.0, 'trade_burst': 3,
            'internal_trade_min_interval_s': 2.0, 'internal_trade_burst': 6},
    # 启动错峰：2026-11-06 三次实测「桥一启动，策略 40-60s 内必挂」——五个
    # worker 零延迟同时开跑，瞬间并发打到 QMT 单线程交易 API。错峰后同一时刻
    # 只有一个 worker 热身。
    'startup': {'grace_s': 8.0, 'step_s': 3.0},
    'positions': {'enabled': True, 'interval_s': 10, 'ttl_s': 120,
                  # 资产刷新比持仓慢（asset_refresh_s=60），stale 阈值必须
                  # 独立且大于刷新周期；否则每轮正常刷新都会被误判陈旧。
                  'asset_refresh_s': 60, 'asset_stale_s': 120},
    'quotes': {'enabled': True, 'tier_a_interval_s': 2, 'tier_b_interval_s': 10,
               'chunk': 25, 'max_symbols': 250, 'stale_after_s': 10,
               'indices': ['000001.SH', '399001.SZ', '000300.SH'], 'watchlist': []},
    'orders': {'enabled': True, 'interval_s': 5, 'maxlen': 2000},
    'trades': {'enabled': True, 'interval_s': 15, 'maxlen': 2000},
    'budget': {'window_s': 10, 'market_ms': 2000, 'rpc_preempt_queue': 1,
               # 订单队列积压闸：待处理订单请求 >= N 时直接 BRIDGE_BUSY 快速失败，
               # 避免调用方在单线程管道里干等 25s（2026-10-30 实测头阻塞）。
               'order_preempt_queue': 3,
               # 行情类请求让路闸：队列积压 >= N 时直接 BRIDGE_BUSY，
               # 避免一次大批 get_full_tick 把单线程管道占住、订单排后面
               # （2026-10-30 实测：50 只 ≈200ms/次）。
               'market_preempt_queue': 2},
    'risk': {'enabled': True, 'max_order_amount': 200000, 'max_orders_per_min': 30,
             'max_deviation_pct': 5.0, 'blacklist_codes': [],
             'blacklist_name_keywords': ['ST', '退'],
             # 卖出是减风险动作：默认不做黑名单/金额/偏离/限频拦截（只拦买入）。
             # 2026-10-30 用户拍板：ST 黑名单把清仓也堵死，等于给卖出加风险。
             'apply_to_sells': False,
             # 但卖出保留「错价保护」：限价对昨收偏离超过该带宽即拒（市价单不受限）。
             # 20% 覆盖创业板/科创板涨跌停；主板 10%/ST 5% 由交易所涨跌停兜底。
             # 2026-10-30 实测 9.5% 会把「跌 10% 的票按买一价正常卖出」误杀。
             'sell_price_band_pct': 20.0,
             # 非持仓、非订阅热集的标的：下单前按需查一次合约详情补全名称/昨收，
             # 否则 ST 黑名单与 ±5% 偏离闸会 fail-open（2026-10-30 审计发现）。
             'enrich_unknown': True},
    'reload': {'enabled': True, 'key': 'qm:qmt:pipe:reload'},
    'health': {'heartbeat_key': 'qm:qmt:pipe:heartbeat', 'metrics_key': 'qm:qmt:pipe:metrics',
               'interval_s': 5, 'ttl_s': 30,
               # 交易查询通道探针（见 strategy_health_worker）：get_asset 走
               # adjust 主线程，连续失败即判 unresponsive 并开始退避。
               'account_probe_s': 30, 'account_probe_timeout_s': 6},
    'tap': {'enabled': True, 'interval_s': 0.5, 'subscribe_refresh_s': 60,
            'max_symbols': 250, 'respect_budget': False,
            # 全市场推送：指向 Redis 集合（成员为后缀式代码）即把订阅扩到全市场，
            # 置空=只订持仓+指数+watchlist 的 250 只热集。
            'universe_key': '',
            # 当日快照（quote:snap:{YYYYMMDD}，hash）：当天可查、次日 09:05 过期，
            # 隔日自动清空，不需要清理任务。
            'day_snapshot': True,
            'day_snapshot_key': 'quote:snap:%s',
            'day_snapshot_expire_hhmm': '09:05'},
    # 全市场轮询兜底：QMT 全推在本机只覆盖策略宇宙（实测 5,638 只代码订阅
    # 只有热集 61 只出 tick），全市场用分批 get_full_tick 轮询补齐。
    # 500 只 ≈0.6s，5,577 只整轮 ≈7-9s；每批前查队列，订单优先让路。
    'sweep': {'enabled': True, 'interval_s': 300, 'chunk': 500, 'ttl_s': 360,
              'universe_key': '', 'max_symbols': 6000,
              'start_hhmm': 930, 'end_hhmm': 1500},
    'source_http': {'enabled': True, 'host': '127.0.0.1', 'port': 17720,
                    'stale_after_s': 30, 'allow_orders': False},
}

# ---------------------------------------------------------------- 日志
_log_lock = threading.Lock()


def _as_text(value):
    """Redis 客户端 decode_responses=False（备源行情要 msgpack）→ 统一转 str。"""
    if isinstance(value, (bytes, bytearray)):
        return value.decode('utf-8', 'replace')
    return str(value)


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
        self.name_map = {}           # code -> name(来自持仓/按需补全)
        self.instrument = {}         # code -> {name, pre, ts}(按需补全缓存)
        self.sweep_written = 0
        self.sweep_last_ts = 0.0
        self.sweep_last_ms = 0.0
        self.quote_pre_close = {}    # code -> 昨收(风控用)
        self.quote_ts = {}           # code -> 写入时间
        self.last_rpc_ms = 0.0
        self.last_pos_age = None
        self.last_order_age = None
        self.last_trade_age = None
        self.errors = 0
        self.last_error = ''
        self.rpc_calls = 0
        self.purged_backlog = 0
        self.tap_ticks = 0
        self.tap_batches = 0
        self.tap_last_push = 0.0
        # 策略账户通道健康（判定见 bridge_gates）：unknown / ok / unresponsive。
        # 启动时**先按 unresponsive 处理**，等 health worker 的 get_asset 探针
        # 成功才放行交易类查询——桥刚重启的启动窗口里，策略可能还没热身完；
        # 此时把队列里的旧交易查询直接转进去，就是「重启后积压回灌」本身。
        self.strategy_state = bridge_gates.STATE_UNRESPONSIVE
        self.account_fail_count = 0
        self.account_probe_ok_ts = 0.0
        # 令牌桶：容器请求桶容量 1（超速即失败）；桥内部轮询桶容量 4
        # （positions 一轮要连发 get_asset + get_positions 两条，硬间隔会把
        # 第二条永远挡在门外 → pos_age 恒 -1）。
        self.queue_trade_tokens = 1.0
        self.queue_trade_ts = 0.0
        self.internal_trade_tokens = 4.0
        self.internal_trade_ts = 0.0

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


def market_call(method, params=None, timeout=None, est_ms=200.0,
                tokens=1.0, throttle=True, budget=True, backlog=True):
    """市场类调用：预算 + 让路 + 记账。返回 data 或抛异常。

    ``tokens``/``throttle`` 用于成对调用：一次预扣多个令牌，第二条调用传
    ``throttle=False``（不再单独取令牌）。分开取会被「只够一条」卡成永远
    只发第一条，配对永远完不成（2026-11-06 实测 pos_age 4028s）。
    """
    with STATE.lock:
        strategy_state = STATE.strategy_state
    if bridge_gates.should_drop_request(method, strategy_state):
        # 策略账户通道判死：本桥自己的轮询也停手，别往单线程管道里加柴。
        raise RuntimeError('strategy unhealthy, skipped')
    if (throttle and bridge_gates.is_trade_context(method)
            and not bridge_gates.is_order_like(method)):
        interval = float(CFG.get('rpc.internal_trade_min_interval_s') or 0)
        capacity = int(CFG.get('rpc.internal_trade_burst') or 6)
        with STATE.lock:
            ok, STATE.internal_trade_tokens, STATE.internal_trade_ts = (
                bridge_gates.take_token(
                    STATE.internal_trade_tokens, STATE.internal_trade_ts,
                    time.time(), interval, capacity, n=tokens))
        if not ok:
            raise RuntimeError('trade rpc throttled, skipped')
    preempt = int(CFG.get('budget.rpc_preempt_queue') or 1)
    if backlog and rpc_backlog() >= preempt:
        raise RuntimeError('rpc backlog, skipped')
    if budget and not BUDGET.allow():
        # 预算闸保护的是行情大调用；持仓快照是风控刚需，必须能绕过
        # （2026-11-06：tap 不可用时行情回退轮询吃光预算，pos_age 一路涨）。
        raise RuntimeError('market budget exhausted, skipped')
    t0 = time.perf_counter()
    try:
        return rpc(method, params, timeout)
    finally:
        BUDGET.add((time.perf_counter() - t0) * 1000.0)


# ---------------------------------------------------------------- A. RPC

def _startup_delay(step: int) -> None:
    """启动错峰：第 step 个 worker 等 grace_s + step*step_s 秒再开跑。"""
    base = float(CFG.get('startup.grace_s') or 0)
    step_s = float(CFG.get('startup.step_s') or 0)
    delay = base + max(0, int(step)) * step_s
    if delay > 0:
        time.sleep(delay)


# ---------------------------------------------------------------- A0. 订单意图
def processing_key() -> str:
    return 'bigqmt:rpc:processing:%s' % CFG.get('account_id')


def inflight_key() -> str:
    return 'qm:qmt:pipe:inflight:%s' % CFG.get('account_id')


def _intent_ttl_s() -> int:
    try:
        return max(60, int(CFG.get('rpc.inflight_ttl_s') or 86400))
    except (TypeError, ValueError):
        return 86400


def _load_intent(key):
    if not key:
        return None
    try:
        raw = R.get(key)
        if not raw:
            return None
        return json.loads(raw.decode('utf-8') if isinstance(raw, bytes) else raw)
    except Exception as exc:  # noqa: BLE001 - 读失败按"未知"处理，绝不重发
        STATE.note_error('intent.load', exc)
        return {'state': order_intent.STATE_UNKNOWN, 'error': repr(exc)}


def _save_intent(key, record) -> None:
    if not key:
        return
    try:
        R.set(key, json.dumps(record, ensure_ascii=False, default=str),
              ex=_intent_ttl_s())
    except Exception as exc:  # noqa: BLE001 - 持久化失败不能把响应弄丢
        STATE.note_error('intent.save', exc)


def _store_inflight(req_id, req, raw, state='inflight', error='') -> bool:
    if not req_id:
        return False
    payload = {
        'request_id': str(req_id),
        'method': str((req or {}).get('method') or ''),
        'state': str(state),
        'started_at': time.time(),
        'raw': str(raw or ''),
        'error': str(error or ''),
    }
    try:
        R.hset(inflight_key(), str(req_id),
               json.dumps(payload, ensure_ascii=False, default=str))
        R.expire(inflight_key(), _intent_ttl_s())
        return True
    except Exception as exc:  # noqa: BLE001
        STATE.note_error('inflight.store', exc)
        return False


def _clear_inflight(req_id) -> None:
    if not req_id:
        return
    try:
        R.hdel(inflight_key(), str(req_id))
    except Exception as exc:  # noqa: BLE001
        STATE.note_error('inflight.clear', exc)


def _remove_processing(raw) -> None:
    try:
        R.lrem(processing_key(), 1, raw)
    except Exception as exc:  # noqa: BLE001
        STATE.note_error('processing.lrem', exc)


def _pop_request(queue):
    """原子取件：BLMOVE 把队首搬到 processing，再处理。

    旧实现先 BLPOP 再送管道：一旦进程在两步之间挂掉，这笔单既不在队列、
    也没有任何落点，看门狗只查队列就会把"结果未知"当成"没有在途单"。
    Redis 7 的 BLMOVE 没有这个窗口。老 Redis 降级 BLPOP + 立即 RPUSH，
    并明确记录仍有毫秒级窗口。
    """
    timeout = float(CFG.get('rpc.queue_blpop_s') or 0.5)
    try:
        return R.blmove(queue, processing_key(), timeout=timeout,
                        src='LEFT', dest='LEFT')
    except Exception as exc:  # noqa: BLE001
        text = str(exc).lower()
        if 'unknown command' not in text and 'not supported' not in text:
            raise
        log('BLMOVE unavailable, falling back to BLPOP+RPUSH: %r' % (exc,))
        item = R.blpop(queue, timeout=timeout)
        if not item:
            return None
        raw = item[1]
        R.rpush(processing_key(), raw)
        return raw


def _reply_for_decision(decision, record, req):
    req_id = (req or {}).get('request_id')
    if decision == order_intent.DECISION_REPLAY:
        if isinstance(record, dict):
            response = record.get('response')
            if isinstance(response, dict):
                return response
            # 旧版本缓存的是裸响应
            if any(k in record for k in ('ok', 'data', 'error')):
                return record
        return {'request_id': req_id, 'ok': True, 'data': record}
    if decision == order_intent.DECISION_CONFLICT:
        return {
            'request_id': req_id, 'ok': False,
            'code': 'IDEMPOTENCY_CONFLICT', 'retriable': False,
            'error': 'IDEMPOTENCY_CONFLICT: 同一业务键携带了不同订单参数，拒绝发送',
        }
    if decision == order_intent.DECISION_IN_PROGRESS:
        return {
            'request_id': req_id, 'ok': False,
            'code': 'IDEMPOTENCY_IN_PROGRESS', 'retriable': False,
            'error': 'IDEMPOTENCY_IN_PROGRESS: 同一业务键的订单仍在途，禁止重发',
        }
    return {
        'request_id': req_id, 'ok': False,
        'code': 'ORDER_RESULT_UNKNOWN', 'retriable': False,
        'error': 'ORDER_RESULT_UNKNOWN: 订单结果未知，禁止重发；请核对柜台',
    }


def _send_order_intent(req, raw=None, reply_fn=None):
    """订单写入的唯一出口：原子幂等 + inflight + 未知结果不可重试。

    返回 ``(response, remove_processing)``。``remove_processing=False`` 表示
    这是结果未知的单，processing 条目必须保留给看门狗/人工核对。
    """
    params = (req or {}).get('params') or {}
    method = str((req or {}).get('method') or '')
    account = str(CFG.get('account_id') or '')
    req_id = str((req or {}).get('request_id') or next_request_id())
    req['request_id'] = req_id
    key = order_intent.idempotency_key(account, method, params)
    fingerprint = order_intent.order_fingerprint(method, params)

    record = None
    claim_attempts = 0
    while True:
        existing = _load_intent(key) if key else None
        decision = order_intent.classify_intent(existing, fingerprint)
        if decision != order_intent.DECISION_NEW:
            response = _reply_for_decision(decision, existing, req)
            if reply_fn is not None:
                try:
                    reply_fn(req, response)
                except Exception as exc:  # noqa: BLE001
                    STATE.note_error('rpc.reply', exc)
            return response, True
        record = order_intent.make_intent_record(
            account_id=account, method=method, params=params,
            request_id=req_id, fingerprint=fingerprint, now=time.time(),
            raw=raw,
        )
        if not key:
            break
        try:
            claimed = R.set(key, json.dumps(record, ensure_ascii=False, default=str),
                            nx=True, ex=_intent_ttl_s())
        except Exception as exc:  # noqa: BLE001 - 幂等不可用就不发单
            STATE.note_error('intent.claim', exc)
            response = {
                'request_id': req_id, 'ok': False,
                'code': 'IDEMPOTENCY_UNAVAILABLE', 'retriable': False,
                'error': 'IDEMPOTENCY_UNAVAILABLE: 幂等存储不可用，拒绝发送订单',
            }
            if reply_fn is not None:
                try:
                    reply_fn(req, response)
                except Exception as reply_exc:  # noqa: BLE001
                    STATE.note_error('rpc.reply', reply_exc)
            return response, False
        if claimed:
            break
        # GET 与 SETNX 之间被别人抢占：重读一次走 replay/in_progress 分支。
        # 最多重试 3 次，避免存储异常导致自旋。
        claim_attempts += 1
        if claim_attempts >= 3:
            response = {
                'request_id': req_id, 'ok': False,
                'code': 'IDEMPOTENCY_UNAVAILABLE', 'retriable': False,
                'error': 'IDEMPOTENCY_UNAVAILABLE: 幂等键状态无法确认，拒绝发送订单',
            }
            if reply_fn is not None:
                try:
                    reply_fn(req, response)
                except Exception as reply_exc:  # noqa: BLE001
                    STATE.note_error('rpc.reply', reply_exc)
            return response, False

    if not _store_inflight(req_id, req, raw, state=order_intent.STATE_INFLIGHT):
        # 意图落不了盘就不发单：否则进程崩了没人知道这笔单存在。
        # 这笔没有真正发出，释放幂等占位，允许调用方在 Redis 恢复后重试。
        if key:
            try:
                R.delete(key)
            except Exception as exc:  # noqa: BLE001
                STATE.note_error('intent.release', exc)
        response = {
            'request_id': req_id, 'ok': False,
            'code': 'ORDER_INTENT_STORE_FAILED', 'retriable': False,
            'error': 'ORDER_INTENT_STORE_FAILED: 订单意图无法持久化，拒绝发送',
        }
        if reply_fn is not None:
            try:
                reply_fn(req, response)
            except Exception as reply_exc:  # noqa: BLE001
                STATE.note_error('rpc.reply', reply_exc)
        return response, False
    try:
        response = TR.send_request(
            req, timeout_seconds=float(CFG.get('rpc.timeout_s') or 15))
    except Exception as exc:  # noqa: BLE001 - 超时/断管 = 结果未知
        error = '%s: %s' % (type(exc).__name__, exc)
        if record is not None:
            record = order_intent.mark_intent(
                record, order_intent.STATE_UNKNOWN, now=time.time(), error=error)
            _save_intent(key, record)
        _store_inflight(req_id, req, raw,
                        state=order_intent.STATE_UNKNOWN, error=error)
        response = {
            'request_id': req_id, 'ok': False,
            'code': 'ORDER_RESULT_UNKNOWN', 'retriable': False,
            'error': 'ORDER_RESULT_UNKNOWN: %s（禁止重发，请核对柜台）' % error,
        }
        if reply_fn is not None:
            try:
                reply_fn(req, response)
            except Exception as reply_exc:  # noqa: BLE001
                STATE.note_error('rpc.reply', reply_exc)
        return response, False

    if not isinstance(response, dict):
        response = {'request_id': req_id, 'ok': False,
                    'error': 'bad response type: %r' % (type(response),)}
    if reply_fn is not None:
        try:
            reply_fn(req, response)
        except Exception as exc:  # noqa: BLE001 - 单可能已发出，回报失败=未知
            error = 'reply failed after send: %r' % (exc,)
            if record is not None:
                record = order_intent.mark_intent(
                    record, order_intent.STATE_UNKNOWN, now=time.time(),
                    error=error)
                _save_intent(key, record)
            _store_inflight(req_id, req, raw,
                            state=order_intent.STATE_UNKNOWN, error=error)
            STATE.note_error('rpc.reply', exc)
            return {
                'request_id': req_id, 'ok': False,
                'code': 'ORDER_RESULT_UNKNOWN', 'retriable': False,
                'error': 'ORDER_RESULT_UNKNOWN: 回报写入失败，禁止重发；请核对柜台',
            }, False

    state = (order_intent.STATE_DONE if response.get('ok')
             else order_intent.STATE_REJECTED)
    if record is not None:
        record = order_intent.mark_intent(
            record, state, now=time.time(), response=response)
        _save_intent(key, record)
    _clear_inflight(req_id)
    return response, True


def recover_processing() -> int:
    """启动恢复：processing 里的订单只标 unknown，绝不重放。

    BLMOVE 保证"取出来的条目一定落在 processing"。进程崩溃后这些条目就是
    结果未知的订单：可能已到策略、也可能没有。重放会造第二笔；丢掉会让
    看门狗以为没有在途单。正确做法是保留并向运维暴露。
    """
    queue = 'bigqmt:rpc:queue:%s' % CFG.get('account_id')
    try:
        items = R.lrange(processing_key(), 0, -1) or []
    except Exception as exc:  # noqa: BLE001
        STATE.note_error('processing.recover', exc)
        return 0
    unknown = 0
    for index, raw in enumerate(items):
        text = raw.decode('utf-8', 'replace') if isinstance(raw, bytes) else str(raw)
        try:
            req = json.loads(decode_rpc_request_payload(text))
        except Exception:  # noqa: BLE001 - 解不开按订单处理
            req = None
        method = str((req or {}).get('method') or '')
        if req is None or order_intent.is_order_method(method):
            req_id = str((req or {}).get('request_id') or 'recovered-%d-%s' % (
                index, time.strftime('%H%M%S')))
            _store_inflight(
                req_id, req or {'method': method}, text,
                state=order_intent.STATE_UNKNOWN,
                error='bridge restarted with processing item; verify counter',
            )
            unknown += 1
            continue
        # 只读请求：重新入队，交给新进程重试；不写 inflight。
        try:
            R.lrem(processing_key(), 1, raw)
            R.rpush(queue, raw)
        except Exception as exc:  # noqa: BLE001
            STATE.note_error('processing.requeue', exc)
    if unknown:
        log('recovered %d unresolved order intent(s) as UNKNOWN; no replay' % unknown)
    return unknown


def rpc_worker():
    account = str(CFG.get('account_id'))
    queue = 'bigqmt:rpc:queue:%s' % account
    log('rpc worker start queue=%s' % queue)
    _startup_delay(0)
    while not STATE.stop.is_set():
        try:
            item = _pop_request(queue)
        except Exception as exc:
            STATE.note_error('rpc.pop', exc)
            time.sleep(1)
            continue
        if item is None:
            continue
        payload = item.decode('utf-8', 'replace') if isinstance(item, bytes) else item
        keep_processing = False
        try:
            try:
                req = json.loads(decode_rpc_request_payload(payload))
            except Exception as exc:
                # 解不开的条目有可能是订单：保留并标 unknown，绝不静默丢。
                STATE.note_error('rpc.decode', exc)
                _store_inflight(
                    'decode-%d' % int(time.time() * 1000),
                    {'method': ''}, payload,
                    state=order_intent.STATE_UNKNOWN,
                    error='decode failed: %r' % (exc,),
                )
                keep_processing = True
                continue
            method = str(req.get('method') or '')
            with STATE.lock:
                strategy_state = STATE.strategy_state
            if bridge_gates.should_drop_request(method, strategy_state):
                # 策略主线程已判定卡死：交易类只读请求立即失败，不转发。继续转发
                # 只会占住单线程管道，QMT 重启后还会把这些积压回灌给新策略
                # （2026-11-06：三次重启存活 12h → 6min → 3min，积压一轮比一轮多）。
                resp = {
                    'request_id': req.get('request_id'), 'ok': False,
                    'error': 'STRATEGY_UNHEALTHY: 交易查询通道卡死，请求未转发'
                             '（防止重启后积压回灌）',
                }
                STATE.note_error('rpc.drop_unhealthy', method)
                try:
                    write_reply(req, resp)
                except Exception as exc:
                    STATE.note_error('rpc.reply', exc)
                continue
            if is_order_method(method):
                busy = order_backlog_gate(
                    rpc_backlog(), CFG.get('budget.order_preempt_queue') or 0)
                if busy is not None:
                    resp = {'request_id': req.get('request_id'), 'ok': False,
                            'error': busy}
                    STATE.note_error('rpc.busy', busy)
                    try:
                        write_reply(req, resp)
                    except Exception as exc:
                        STATE.note_error('rpc.reply', exc)
                    continue
            if is_market_method(method):
                busy = order_backlog_gate(
                    rpc_backlog(), CFG.get('budget.market_preempt_queue') or 0,
                    label='market')
                if busy is not None:
                    resp = {'request_id': req.get('request_id'), 'ok': False,
                            'error': busy}
                    STATE.note_error('rpc.market_busy', busy)
                    try:
                        write_reply(req, resp)
                    except Exception as exc:
                        STATE.note_error('rpc.reply', exc)
                    continue
            # 交易类请求限速：只限交易上下文方法，订单/撤单/行情一律不受影响。
            # QMT 的 get_trade_detail_data 连续查询会长时间阻塞，令牌桶把持续速率
            # 压到 rpc.trade_min_interval_s 一条，容器请求桶容量 1。
            if (bridge_gates.is_trade_context(method)
                    and not bridge_gates.is_order_like(method)):
                interval = float(CFG.get('rpc.trade_min_interval_s') or 0)
                capacity = int(CFG.get('rpc.trade_burst') or 1)
                with STATE.lock:
                    ok, STATE.queue_trade_tokens, STATE.queue_trade_ts = (
                        bridge_gates.take_token(
                            STATE.queue_trade_tokens, STATE.queue_trade_ts,
                            time.time(), interval, capacity))
                if not ok:
                    resp = {'request_id': req.get('request_id'), 'ok': False,
                            'error': 'TRADE_RPC_THROTTLED: 交易查询限速，稍后重试'}
                    try:
                        write_reply(req, resp)
                    except Exception as exc:
                        STATE.note_error('rpc.reply', exc)
                    continue
            gate = risk_gate(method, req.get('params') or {})
            if gate is not None:
                resp = {'request_id': req.get('request_id'), 'ok': False,
                        'error': gate}
                try:
                    write_reply(req, resp)
                except Exception as exc:
                    STATE.note_error('rpc.reply', exc)
                continue
            if is_order_method(method):
                # 订单唯一出口：原子幂等 + inflight + 未知不可重试。
                try:
                    resp, remove = _send_order_intent(
                        req, raw=payload, reply_fn=write_reply)
                except Exception as exc:  # noqa: BLE001 - 未知异常也保留意图
                    STATE.note_error('rpc.order', exc)
                    keep_processing = True
                    resp = {
                        'request_id': req.get('request_id'), 'ok': False,
                        'code': 'ORDER_RESULT_UNKNOWN', 'retriable': False,
                        'error': 'ORDER_RESULT_UNKNOWN: %s（禁止重发，请核对柜台）'
                                 % exc,
                    }
                    try:
                        write_reply(req, resp)
                    except Exception as reply_exc:  # noqa: BLE001
                        STATE.note_error('rpc.reply', reply_exc)
                    continue
                keep_processing = not remove
                continue
            try:
                resp = TR.send_request(
                    req, timeout_seconds=float(CFG.get('rpc.timeout_s') or 15))
            except Exception as exc:
                resp = {'request_id': req.get('request_id'), 'ok': False,
                        'error': '%s: %s' % (type(exc).__name__, exc)}
                STATE.note_error('rpc.forward', exc)
            try:
                write_reply(req, resp)
            except Exception as exc:
                STATE.note_error('rpc.reply', exc)
        finally:
            if not keep_processing:
                _remove_processing(payload)


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


def purge_trade_backlog(limit=200):
    """策略账户通道判死后，清掉队列里**已积压**的交易类只读请求。

    为什么：``rpc_worker`` 只拦「判死之后新到的」请求；判死之前已经入队的
    旧查询仍留在 Redis 队列里，QMT 一重启就会灌给新策略（2026-11-06 实测：
    三次重启存活 12h → 6min → 3min，积压一轮比一轮多）。

    只清 ``bridge_gates.should_drop_request`` 命中的方法；订单/撤单、行情类、
    解码失败的条目一律保留（fail-closed）。被清的请求会立刻收到同文案的
    ``STRATEGY_UNHEALTHY`` 失败回复，调用方不再干等 15–25s。
    """
    account = str(CFG.get('account_id') or '')
    if not account:
        return 0
    queue = 'bigqmt:rpc:queue:%s' % account
    try:
        items = R.lrange(queue, 0, max(1, int(limit)) - 1)
    except Exception as exc:  # noqa: BLE001 - 读队列失败只记日志
        STATE.note_error('backlog.lrange', exc)
        return 0
    if not items:
        return 0
    with STATE.lock:
        strategy_state = STATE.strategy_state
    purge, keep = bridge_gates.split_backlog(items, strategy_state)
    removed = 0
    for raw, req in purge:
        try:
            R.lrem(queue, 1, raw)
            write_reply(req, {
                'request_id': req.get('request_id'),
                'ok': False,
                'error': 'STRATEGY_UNHEALTHY: 交易查询通道卡死，请求已从队列清除'
                         '（防止重启后积压回灌）',
            })
            removed += 1
        except Exception as exc:  # noqa: BLE001 - 单条失败不拦其余
            STATE.note_error('backlog.lrem', exc)
    if removed:
        with STATE.lock:
            STATE.purged_backlog += removed
        log('purged %d trade-context requests (kept=%d) from %s'
            % (removed, len(keep), queue))
    return removed


# ---------------------------------------------------------------- 风控 + 幂等
_order_hits = deque()
_order_lock = threading.Lock()


def _order_params(params):
    symbol = (params.get('stock_code') or params.get('symbol')
              or params.get('code') or params.get('order_code') or '')
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
    """订单写方法集合（含策略侧别名，与 risk_gate / 幂等同源）。"""
    return bridge_gates.is_order_like(method)


_MARKET_METHODS = ('get_full_tick', 'get_instrument_detail', 'get_stock_list')


def is_market_method(method):
    """行情类只读方法（大批量拉取会长时间占用单线程管道）。"""
    return str(method or '').lower() in _MARKET_METHODS


def order_backlog_gate(backlog, limit, label='order'):
    """队列积压闸（纯函数）：待处理请求超限即快速失败，别让调用方干等 25s。"""
    if int(limit or 0) <= 0:
        return None
    if int(backlog) >= int(limit):
        return 'BRIDGE_BUSY: %s queue backlog=%d >= %d' % (label, int(backlog), int(limit))
    return None


_INSTRUMENT_TTL_S = 6 * 3600


def _instrument_info(symbol):
    """按需补全标的名称/昨收（持仓表与订阅热集都不含时）。

    不补全的话：ST 名称黑名单只对**持仓**生效、±5% 偏离闸只对**热集**生效，
    其余标的一律 fail-open —— 买入侧风控形同虚设（2026-10-30 审计）。
    结果缓存 6 小时（昨收当日不变）；查询失败保持 fail-open 并记账。
    """
    now = time.time()
    cached = STATE.instrument.get(symbol)
    if cached and now - float(cached.get('ts') or 0) < _INSTRUMENT_TTL_S:
        return cached
    if not bool(CFG.get('risk.enrich_unknown')):
        return None
    try:
        data = rpc('get_instrument_detail', {'code': symbol}, timeout=3.0)
    except Exception as exc:
        STATE.note_error('risk.enrich', exc)
        return None
    if not isinstance(data, dict):
        return None
    info = {
        'name': str(data.get('InstrumentName') or ''),
        'pre': float(data.get('PreClose') or 0),
        'ts': now,
    }
    with STATE.lock:
        STATE.instrument[symbol] = info
        if info['name']:
            STATE.name_map[symbol] = info['name']
        if info['pre'] > 0:
            STATE.quote_pre_close[symbol] = info['pre']
    return info


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


def _risk_rate_limit():
    with _order_lock:
        now = time.time()
        while _order_hits and now - _order_hits[0] > 60:
            _order_hits.popleft()
        rate_limit = int(CFG.get('risk.max_orders_per_min') or 0)
        if rate_limit > 0 and len(_order_hits) >= rate_limit:
            return 'risk: rate limit %d/min' % rate_limit
        _order_hits.append(now)
    return None


def _risk_gate_order(params):
    """单笔买入检查（不计数）。batch 逐笔调用它，再统一算一次频率。"""
    symbol, volume, price = _order_params(params)
    if not symbol:
        return 'risk: empty symbol'
    if not STATE.name_map.get(symbol) or not STATE.quote_pre_close.get(symbol):
        _instrument_info(symbol)
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
    return None


def risk_gate(method, params):
    """下单/撤单前置检查。返回 None=放行，字符串=拒绝原因。"""
    if not bool(CFG.get('risk.enabled')):
        return None
    # 先规范化别名：order_stock_async → submit_order，否则会绕过只认
    # order_stock 的闸门（2026-11-16 审计）。
    m = bridge_gates.canonical_method(method)
    if m in ('cancel_order', 'cancel_orders_batch', 'cancel'):
        return None
    if m == 'submit_orders_batch':
        # batch 是别名入口之一：逐笔做买入闸，再统一计一次频率。
        orders = (params or {}).get('orders') or []
        if not isinstance(orders, list) or not orders:
            return 'risk: empty batch'
        if len(orders) > 500:
            return 'risk: batch exceeds 500'
        has_buy = False
        for item in orders:
            if not isinstance(item, dict):
                return 'risk: invalid batch item'
            gate = _risk_gate_order(item)
            if gate:
                return gate
            if not (_order_side(item) == 'SELL'
                    and not bool(CFG.get('risk.apply_to_sells'))):
                has_buy = True
        return _risk_rate_limit() if has_buy else None
    if m not in ('submit_order', 'passorder'):
        return None
    gate = _risk_gate_order(params)
    if gate:
        return gate
    # 卖出是减风险动作：默认连频率闸也跳过（与旧行为一致）。
    if (_order_side(params) == 'SELL'
            and not bool(CFG.get('risk.apply_to_sells'))):
        return None
    return _risk_rate_limit()


def _idem_key(req):
    """业务幂等键（兼容旧测试/调用）；字段选择在 bridge_order_intent。"""
    return order_intent.idempotency_key(
        CFG.get('account_id'),
        (req or {}).get('method'),
        (req or {}).get('params') or {},
    )


# ---------------------------------------------------------------- B. 持仓
_last_pos_material = [None]
# asset 缓存：2026-11-06 实测「连发两条」比「单发一条」更容易触发 QMT 楔死，
# positions 每轮单发（风控刚需），asset 降频到 positions.asset_refresh_s 一次。
_asset_cache = [None]
_asset_cache_ts = [0.0]
_asset_error = [""]


def positions_worker():
    account = str(CFG.get('account_id'))
    key = 'bigqmt:positions:%s' % account
    stream = 'bigqmt:position_events:%s' % account
    log('positions worker start')
    _startup_delay(1)
    while not STATE.stop.is_set():
        interval = float(CFG.get('positions.interval_s') or 3)
        if not bool(CFG.get('positions.enabled')):
            time.sleep(max(1.0, interval))
            continue
        try:
            # 单发 positions（风控刚需）：配对调用在楔死状态下永远凑不齐两条。
            pos = market_call('get_positions', {}, est_ms=150,
                              budget=False, backlog=False)
            now = time.time()
            refresh = float(CFG.get('positions.asset_refresh_s') or 60)
            if _asset_cache[0] is None or now - _asset_cache_ts[0] >= refresh:
                try:
                    _asset_cache[0] = market_call(
                        'get_asset', {}, est_ms=100, budget=False, backlog=False)
                    _asset_cache_ts[0] = now
                    _asset_error[0] = ''
                except Exception as exc:  # noqa: BLE001 - 保留旧值，不拖垮本轮
                    # 关键：失败只记录错误，**不刷新** _asset_cache_ts。
                    # 旧实现复用缓存却把 payload.updated_at 写成当前时间，
                    # 后端/看门狗把陈旧资产当成新快照（2026-11-16 审计）。
                    _asset_error[0] = '%s: %s' % (type(exc).__name__, exc)
                    if 'skipped' not in str(exc):
                        STATE.note_error('positions.asset', exc)
            payload = build_position_payload(
                _asset_cache[0] or {}, pos,
                asset_observed_at=_asset_cache_ts[0],
                positions_observed_at=now,
                asset_error=_asset_error[0],
            )
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


def _fmt_ts(ts):
    try:
        value = float(ts or 0)
    except (TypeError, ValueError):
        return ''
    if value <= 0:
        return ''
    return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(value))


def _parse_ts(value):
    if not value:
        return 0.0
    try:
        return time.mktime(time.strptime(str(value), '%Y-%m-%d %H:%M:%S'))
    except (TypeError, ValueError):
        return 0.0


def build_position_payload(asset, pos, *, asset_observed_at=None,
                           positions_observed_at=None, asset_error='',
                           positions_error=''):
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
    positions_ts = float(positions_observed_at or time.time())
    asset_ts = float(asset_observed_at or 0.0)
    meta = account_state.merge_observation(
        asset_observed_at=asset_ts or None,
        positions_observed_at=positions_ts,
        now=positions_ts,
        asset_stale_after_s=CFG.get('positions.asset_stale_s') or 120,
        positions_stale_after_s=CFG.get('source_http.stale_after_s') or 30,
        asset_error=asset_error,
        positions_error=positions_error,
    )
    return {
        'account_id': str(CFG.get('account_id')),
        'reason': 'bridge',
        # updated_at 代表「持仓轮次」；资产单独计时，见 asset_observed_at。
        'updated_at': _fmt_ts(positions_ts),
        'positions_observed_at': _fmt_ts(positions_ts),
        'asset_observed_at': _fmt_ts(asset_ts),
        **meta,
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


def _snapshot_mapping(code, tick, ts):
    """tick → snapshot hash 字段（纯函数，便于批量写与单测）。"""
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
    return mapping


def write_snapshot(code, tick):
    if not isinstance(tick, dict):
        return
    ts = time.time()
    mapping = _snapshot_mapping(code, tick, ts)
    pipe = R.pipeline()
    pipe.hset('market:snapshot:%s' % code, mapping=mapping)
    pipe.expire('market:snapshot:%s' % code, int(CFG.get('quotes.stale_after_s') or 10))
    pipe.execute()
    with STATE.lock:
        STATE.quote_ts[code] = ts
        if tick.get('lastClose'):
            STATE.quote_pre_close[code] = float(tick['lastClose'])


def _seconds_until_next(hhmm):
    """距下一个 HH:MM（本地墙钟）的秒数；跨日自动 +24h，最小 1 小时。"""
    try:
        hh, mm = [int(x) for x in str(hhmm or '09:05').split(':', 1)]
    except Exception:
        hh, mm = 9, 5
    now = time.time()
    lt = time.localtime(now)
    target = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hh, mm, 0, 0, 0, -1))
    if target <= now:
        target += 86400
    return max(3600, int(target - now))


def _day_snapshot_key():
    if not bool(CFG.get('tap.day_snapshot')):
        return None
    pattern = str(CFG.get('tap.day_snapshot_key') or 'quote:snap:%s')
    return pattern % time.strftime('%Y%m%d')


def write_snapshots_bulk(ticks, chunk=500, ttl_override=0):
    """批量写快照：**一个 pipeline 写一批**，不是每只一次往返。

    全市场（~5,400 只）0.5s 一轮时，逐只 pipeline 会变成 5,400 次往返
    （本地 ~0.2ms 也要 1s+，直接拖垮 tap 循环）。分批写把往返压到 ~11 次。
    """
    if not isinstance(ticks, dict) or not ticks:
        return 0
    ts = time.time()
    ttl = int(ttl_override or CFG.get('quotes.stale_after_s') or 10)
    day_key = _day_snapshot_key()
    day_ttl = _seconds_until_next(CFG.get('tap.day_snapshot_expire_hhmm')) if day_key else 0
    written = 0
    items = list(ticks.items())
    for start in range(0, len(items), max(1, int(chunk))):
        pipe = R.pipeline(transaction=False)
        batch = items[start : start + chunk]
        for code, tick in batch:
            if not isinstance(tick, dict):
                continue
            mapping = _snapshot_mapping(code, tick, ts)
            key = 'market:snapshot:%s' % code
            pipe.hset(key, mapping=mapping)
            pipe.expire(key, ttl)
            if day_key:
                # 当日快照：整条 mapping 落 JSON，消费方按需取字段。
                pipe.hset(day_key, code, json.dumps(mapping, ensure_ascii=False))
            written += 1
        if day_key:
            pipe.expire(day_key, day_ttl)
        pipe.execute()
        with STATE.lock:
            for code, tick in batch:
                if not isinstance(tick, dict):
                    continue
                STATE.quote_ts[code] = ts
                if tick.get('lastClose'):
                    STATE.quote_pre_close[code] = float(tick['lastClose'])
    return written


def session_active(now=None):
    """是否在 A 股连续竞价时段（纯函数，便于单测）。"""
    lt = time.localtime(now if now is not None else time.time())
    if lt.tm_wday >= 5:
        return False
    hhmm = lt.tm_hour * 100 + lt.tm_min
    return (930 <= hhmm <= 1130) or (1300 <= hhmm <= 1500)


def sweep_universe():
    """全市场轮询的标的域：sweep.universe_key 优先，否则用 tap 的域。"""
    key = str(CFG.get('sweep.universe_key') or '') or str(CFG.get('tap.universe_key') or '')
    codes = []
    if key:
        try:
            codes = [
                text
                for text in (_as_text(c) for c in (R.smembers(key) or set()))
                if '.' in text
            ]
        except Exception as exc:
            STATE.note_error('sweep.universe', exc)
    if not codes:
        with STATE.lock:
            codes = [c for c in STATE.symbols_a if '.' in str(c)]
    limit = int(CFG.get('sweep.max_symbols') or 6000)
    return sorted(set(codes))[:limit]


def sweep_worker():
    log('sweep worker start')
    _startup_delay(5)
    while not STATE.stop.is_set():
        interval = float(CFG.get('sweep.interval_s') or 300)
        if not bool(CFG.get('sweep.enabled')) or not session_active():
            time.sleep(min(30.0, max(5.0, interval)))
            continue
        codes = sweep_universe()
        chunk = max(50, int(CFG.get('sweep.chunk') or 500))
        ttl = int(CFG.get('sweep.ttl_s') or 360)
        written = 0
        started = time.perf_counter()
        for start in range(0, len(codes), chunk):
            waited = 0.0
            while rpc_backlog() >= 1 and waited < 5.0:
                time.sleep(0.2)
                waited += 0.2
            try:
                data = rpc('get_full_tick', {'codes': codes[start:start + chunk]},
                           timeout=15)
            except Exception as exc:
                STATE.note_error('sweep', exc)
                continue
            if isinstance(data, dict):
                written += write_snapshots_bulk(data, chunk=500, ttl_override=ttl)
            time.sleep(0.05)
        with STATE.lock:
            STATE.sweep_written = written
            STATE.sweep_last_ts = time.time()
            STATE.sweep_last_ms = (time.perf_counter() - started) * 1000.0
        time.sleep(max(30.0, interval))


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
    _startup_delay(1)
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
_SEEN_MAX = 5000


def _remember_seen(seen, key, raw_state):
    """有界记忆：先写流成功，才允许记 seen；超过上限丢最旧。"""
    seen[key] = raw_state
    while len(seen) > _SEEN_MAX:
        seen.pop(next(iter(seen)), None)


def _order_fingerprint(row):
    return str(row.get('order_id') or row.get('m_strOrderID') or row.get('order_sysid') or
               row.get('m_strOrderSysID') or row.get('m_nOrderRef') or '') + '|' + \
           str(row.get('status') or row.get('m_nOrderStatus') or row.get('order_status') or '')


def _trade_fingerprint(row):
    return str(row.get('trade_id') or row.get('m_strTradeID') or row.get('traded_id') or
               row.get('m_strTradeId') or '') + '|' + str(row.get('traded_volume') or row.get('m_nVolume') or '')


def _emit_events_once(kind, rows, seen, fp, stream, account):
    """把一批回报写进 Redis Stream；写成功才记 seen。

    抽成独立函数是为了可测：第一次 XADD 失败时 seen 必须保持未变，
    下一次轮询要重投同一条回报（至少一次语义）。
    """
    wrote = 0
    for row in rows or ():
        if not isinstance(row, dict):
            continue
        key = fp(row)
        raw_state = json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
        if seen.get(key) == raw_state:
            continue
        payload = {'account_id': account, 'kind': kind,
                   'updated_at': time.strftime('%Y-%m-%d %H:%M:%S'), 'row': row}
        # 先 XADD 成功，再记 seen：写流失败时下一轮必须重投。
        R.xadd(stream, {'payload': json.dumps(payload, ensure_ascii=False, default=str)},
               maxlen=int(CFG.get('%ss.maxlen' % kind) or 2000), approximate=True)
        _remember_seen(seen, key, raw_state)
        wrote += 1
    return wrote


def events_worker(kind):
    account = str(CFG.get('account_id'))
    stream = 'bigqmt:%s_events:%s' % (kind, account)
    method = 'query_stock_orders' if kind == 'order' else 'query_stock_trades'
    seen = _order_seen if kind == 'order' else _trade_seen
    fp = _order_fingerprint if kind == 'order' else _trade_fingerprint
    log('%s events worker start' % kind)
    _startup_delay(3 if kind == 'order' else 4)
    while not STATE.stop.is_set():
        interval = float(CFG.get('%ss.interval_s' % kind) or (2 if kind == 'order' else 5))
        if not bool(CFG.get('%ss.enabled' % kind)):
            time.sleep(max(1.0, interval))
            continue
        try:
            rows = market_call(method, {}, est_ms=120)
            if isinstance(rows, list):
                _emit_events_once(kind, rows, seen, fp, stream, account)
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
    # 可选全市场来源：Redis 集合（如 qm:universe:cn，由数据管线每日重建）。
    # 置空则退回「持仓+指数+watchlist」，即当前 250 只热集口径。
    universe_key = str(CFG.get('tap.universe_key') or '')
    if universe_key:
        try:
            members = R.smembers(universe_key) or set()
            syms = list(syms) + sorted(_as_text(x) for x in members)
        except Exception as exc:
            STATE.note_error('tap.universe', exc)
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
    _startup_delay(0)
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
            if not universe:
                # 启动瞬间/宇宙集合未就绪：别把「空列表」当成错误去订阅。
                time.sleep(max(1.0, interval))
                continue
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
            write_snapshots_bulk(ticks)
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


def _inflight_counts():
    """(processing_len, inflight_orders, unknown_orders)；读失败返回 -1。"""
    try:
        processing_len = int(R.llen(processing_key()))
        records = R.hvals(inflight_key()) or []
    except Exception:  # noqa: BLE001
        return -1, -1, -1
    inflight = 0
    unknown = 0
    for raw in records:
        try:
            doc = json.loads(raw.decode('utf-8') if isinstance(raw, bytes) else raw)
        except Exception:  # noqa: BLE001
            continue
        state = str((doc or {}).get('state') or '')
        if state == order_intent.STATE_INFLIGHT:
            inflight += 1
        elif state == order_intent.STATE_UNKNOWN:
            unknown += 1
    return processing_len, inflight, unknown


def _metrics_snapshot():
    now = time.time()
    # Redis I/O 放在 STATE.lock 外面：Redis 卡住时不能连持仓线程一起堵。
    processing_len, inflight_orders, unknown_orders = _inflight_counts()
    with STATE.lock:
        positions_age = (round(now - STATE.last_pos_age, 1)
                         if STATE.last_pos_age else -1)
        asset_age = (round(now - _asset_cache_ts[0], 1)
                     if _asset_cache_ts[0] else -1)
        asset_status = account_state.component_status(
            _asset_cache_ts[0] or None, now,
            CFG.get('positions.asset_stale_s') or 120,
            error=_asset_error[0],
        )
        positions_status = account_state.component_status(
            STATE.last_pos_age, now,
            CFG.get('source_http.stale_after_s') or 30,
        )
        return {
            'state': 'ok',
            'account_id': str(CFG.get('account_id')),
            'last_rpc_ms': round(STATE.last_rpc_ms, 1),
            'pos_age_s': positions_age,
            'positions_age_s': positions_age,
            'asset_age_s': asset_age,
            'asset_status': asset_status,
            'positions_status': positions_status,
            'processing_len': processing_len,
            'inflight_orders': inflight_orders,
            'unknown_orders': unknown_orders,
            'order_age_s': round(now - STATE.last_order_age, 1) if STATE.last_order_age else -1,
            'trade_age_s': round(now - STATE.last_trade_age, 1) if STATE.last_trade_age else -1,
            'quote_symbols': len(STATE.quote_ts),
            'quote_age_max_s': round(now - max(STATE.quote_ts.values()), 1) if STATE.quote_ts else -1,
            'tap_state': _TAP_STATE['state'],
            'tap_ticks': STATE.tap_ticks,
            'tap_batches': STATE.tap_batches,
            'tap_age_s': round(now - STATE.tap_last_push, 1) if STATE.tap_last_push else -1,
            'tap_drain_ms': round(_TAP_STATE['last_drain_ms'], 1),
            'sweep_written': STATE.sweep_written,
            'sweep_last_age_s': round(now - STATE.sweep_last_ts, 1) if STATE.sweep_last_ts else -1,
            'sweep_ms': round(STATE.sweep_last_ms, 1),
            'tap_subscribed': len(_TAP_STATE['subscribed']),
            'rpc_calls': STATE.rpc_calls,
            'purged_backlog': STATE.purged_backlog,
            'errors': STATE.errors,
            'last_error': STATE.last_error or _TAP_STATE['last_error'],
            'duty_ms_10s': round(BUDGET.used_ms(), 0),
            'queue_len': rpc_backlog(),
            # 策略账户通道健康：ok/unresponsive。unresponsive 时交易类只读请求
            # 本地快速失败（不回灌），下单/撤单与行情不受影响。
            'strategy_state': STATE.strategy_state,
            'account_fail_count': STATE.account_fail_count,
            'account_probe_ok_age_s': (
                round(now - STATE.account_probe_ok_ts, 1)
                if STATE.account_probe_ok_ts else -1),
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
                   # 部署/能力诊断（与 8550 QMT_READONLY_METHODS 对齐）
                   'get_deployment_info', 'probe_capabilities', 'reload_status',
                   # 写入面：默认关（source_http.allow_orders=false），且策略
                   # ALLOW_ORDER_METHODS 必须同时打开；风控/幂等在本层执行
                   'order_stock', 'submit_order',
                   'order_stock_async', 'order_stock_batch',
                   'submit_orders_batch', 'passorder',
                   'cancel_order_stock', 'cancel_order',
                   'cancel_order_stock_sysid', 'cancel_order_stock_batch',
                   'cancel_orders_batch'}


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
    positions_ts = _parse_ts(payload.get('positions_observed_at') or updated_at)
    asset_ts = _parse_ts(payload.get('asset_observed_at'))
    now = time.time()
    meta = account_state.merge_observation(
        asset_observed_at=asset_ts or None,
        positions_observed_at=positions_ts or None,
        now=now,
        asset_stale_after_s=CFG.get('positions.asset_stale_s') or 120,
        positions_stale_after_s=stale_after,
        asset_error=str(payload.get('asset_error') or ''),
        positions_error=str(payload.get('positions_error') or ''),
    )
    age = meta['positions_age_s']
    stale = bool(meta['stale'])
    # 账户快照本身过期时**不合并当前行情**：旧量配新价会把浮盈做成假实时。
    # 资产陈旧不影响持仓价合并（两者独立），但整份仍标 stale，后端不落库。
    codes = list(positions.keys())
    if codes and meta['positions_status'] == account_state.STATUS_OK:
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
                if now - quote_ts > stale_after:
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
    payload.update(meta)
    payload['age_s'] = age
    payload['snapshot_source'] = source
    payload['stale'] = stale
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


class SourceError(RuntimeError):
    """QMT source HTTP 的可映射错误（503/429），与裸 ValueError 区分。"""

    def __init__(self, message, code='SOURCE_ERROR', status=503):
        super().__init__(message)
        self.code = code
        self.status = int(status)


def _source_rpc(method, params=None, timeout=None):
    """HTTP 只读账户/委托查询也走健康门禁与交易令牌桶。

    旧实现直接 ``rpc()``：策略主线程已判死时仍往单线程管道灌查询，且
    容器侧可绕过限速把队列打满（审计 Secondary）。
    """
    with STATE.lock:
        strategy_state = STATE.strategy_state
    if bridge_gates.should_drop_request(method, strategy_state):
        raise SourceError(
            'STRATEGY_UNHEALTHY: 交易查询通道卡死，请求未转发',
            code='STRATEGY_UNHEALTHY', status=503)
    if (bridge_gates.is_trade_context(method)
            and not bridge_gates.is_order_like(method)):
        interval = float(CFG.get('rpc.trade_min_interval_s') or 0)
        capacity = int(CFG.get('rpc.trade_burst') or 1)
        with STATE.lock:
            ok, STATE.queue_trade_tokens, STATE.queue_trade_ts = (
                bridge_gates.take_token(
                    STATE.queue_trade_tokens, STATE.queue_trade_ts,
                    time.time(), interval, capacity))
        if not ok:
            raise SourceError(
                'TRADE_RPC_THROTTLED: 交易查询限速，稍后重试',
                code='TRADE_RPC_THROTTLED', status=429)
    return rpc(method, params or {}, timeout=timeout)


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
        return _source_rpc('get_asset', {}, timeout=15)
    if method == 'get_positions':
        return _source_rpc('get_positions', {}, timeout=20)
    if method == 'query_orders':
        req = {'cancelable_only': bool(params.get('cancelable_only'))}
        if params.get('strategy_name') is not None:
            req['strategy_name'] = str(params.get('strategy_name'))
        return _source_rpc('query_orders', req, timeout=25)
    if method == 'query_trades':
        req = {}
        if params.get('strategy_name') is not None:
            req['strategy_name'] = str(params.get('strategy_name'))
        return _source_rpc('query_trades', req, timeout=25)
    if bridge_gates.is_order_like(method):
        if not bool(CFG.get('source_http.allow_orders')):
            raise ValueError('orders disabled on qmt source')
        gate = risk_gate(method, params)
        if gate:
            raise ValueError(gate)
        request = {
            'request_id': next_request_id(),
            'method': method,
            'params': params,
        }
        # 与 Redis worker 共用同一幂等/inflight 状态机：
        # - 同 remark 同参数 -> 返回上次结果，不重发；
        # - 同 remark 不同参数 -> 冲突；
        # - 超时/断管 -> unknown，HTTP 侧也不得重试。
        response, _remove = _send_order_intent(
            request, raw=None, reply_fn=None)
        if not isinstance(response, dict) or not response.get('ok'):
            raise ValueError(str((response or {}).get('error') or 'order failed'))
        return response.get('data')
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
        return _source_rpc('query_account_infos', params, timeout=20)
    if method == 'describe_trade_detail_fields':
        # 只返回字段名/形状，不返回订单值
        return _source_rpc('describe_trade_detail_fields', params, timeout=20)
    if method in ('get_deployment_info', 'probe_capabilities', 'reload_status'):
        return _source_rpc(method, params, timeout=15)
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
        except SourceError as exc:
            self._send(exc.status, {
                'success': False, 'method': method, 'code': exc.code,
                'error': str(exc), 'source': 'qmt'})
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
def strategy_health_worker():
    """账户通道探针：``get_asset`` 走 adjust 主线程，是「假活」最灵敏的判据。

    ping 与行情方法跑在后台 listener 线程，主线程挂死时它们仍然 20ms 正常，
    不能拿来判健康。连续失败达阈值 → unresponsive：rpc_worker 对交易类只读
    请求快速失败（不回灌），本桥的轮询也自动让路；探针一成功立即回到 ok。
    """
    log('strategy health worker start')
    _startup_delay(2)
    while not STATE.stop.is_set():
        interval = float(CFG.get('health.account_probe_s') or 30)
        timeout = float(CFG.get('health.account_probe_timeout_s') or 6)
        ok = True
        try:
            rpc('get_asset', {}, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            ok = False
            STATE.note_error('health.account', exc)
        with STATE.lock:
            STATE.account_fail_count = 0 if ok else STATE.account_fail_count + 1
            prev = STATE.strategy_state
            STATE.strategy_state = bridge_gates.next_state(
                prev, ok, STATE.account_fail_count)
            if ok:
                STATE.account_probe_ok_ts = time.time()
            new = STATE.strategy_state
            fails = STATE.account_fail_count
        if new != prev:
            log('strategy account channel %s -> %s (fails=%d)' % (prev, new, fails))
        if new == bridge_gates.STATE_UNRESPONSIVE:
            # 判死后每轮清一次已积压的交易类只读请求；订单/行情不动。
            purge_trade_backlog()
        time.sleep(max(5.0, interval))


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
                            env=dict(os.environ, QM_RELOAD_CHILD='1'),
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


def _write_pid_file() -> None:
    """写精确 PID 文件：看门狗只杀这一 PID，不再按命令行子串扫杀。"""
    try:
        os.makedirs(os.path.dirname(PID_PATH), exist_ok=True)
        with open(PID_PATH, 'w', encoding='utf-8') as fh:
            json.dump({
                'pid': os.getpid(),
                'started_at': time.strftime('%Y-%m-%d %H:%M:%S'),
                'script': os.path.abspath(__file__),
            }, fh, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001 - PID 文件写不了不拦启动
        log('pid file write failed: %r' % (exc,))


def _remove_pid_file() -> None:
    try:
        with open(PID_PATH, encoding='utf-8') as fh:
            doc = json.load(fh)
        if int(doc.get('pid') or 0) == os.getpid():
            os.remove(PID_PATH)
    except Exception:  # noqa: BLE001
        pass


def main():
    log('pipe bridge v2 starting account=%s pipe=%s' %
        (CFG.get('account_id'), CFG.get('pipe.name')))
    _write_pid_file()
    # 先把上次崩溃留在 processing 的订单标 unknown；绝不重放。
    try:
        recover_processing()
    except Exception as exc:  # noqa: BLE001 - 恢复失败不能拦住桥启动
        log('recover_processing failed: %r' % (exc,))
    threads = [
        threading.Thread(target=watchdog_config_loop, daemon=True, name='cfg'),
        threading.Thread(target=rpc_worker, daemon=True, name='rpc'),
        threading.Thread(target=positions_worker, daemon=True, name='pos'),
        threading.Thread(target=quotes_worker, daemon=True, name='quotes'),
        threading.Thread(target=tap_worker, daemon=True, name='tap'),
        threading.Thread(target=sweep_worker, daemon=True, name='sweep'),
        threading.Thread(target=source_http_worker, daemon=True, name='source_http'),
        threading.Thread(target=events_worker, args=('order',), daemon=True, name='orders'),
        threading.Thread(target=events_worker, args=('trade',), daemon=True, name='trades'),
        threading.Thread(target=health_worker, daemon=True, name='health'),
        threading.Thread(target=strategy_health_worker, daemon=True,
                         name='strategy_health'),
    ]
    for t in threads:
        t.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        STATE.stop.set()
        _remove_pid_file()
        log('stopping by KeyboardInterrupt')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        log('FATAL\n' + traceback.format_exc())
        raise
