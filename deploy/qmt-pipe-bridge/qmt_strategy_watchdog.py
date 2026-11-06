"""QMT 策略看门狗：交易查询通道卡死 → 尝试 reload_deployment → 告警。

背景（2026-11-06 两次实盘复现）
--------------------------------
策略把 **trade-context 方法**（``get_asset`` / ``get_positions`` /
``query_orders`` …）推迟到 adjust 主线程执行——``get_trade_detail_data`` 只能在
主线程拿到数据，后台线程返回空（见 ``LISTENER_DEFERRED_METHODS``）。主线程一旦
被某个 QMT C++ 调用挂住，adjust 不再 tick，这些查询全部超时；而 ``ping`` 与
行情方法跑在后台 listener 线程，仍然 25ms 正常应答——**"QMT 在线·账户滞后"
的假活**就是这么来的。

两次现场：09:37:46 与 10:14:23，策略日志（``XtClient_FormulaOutput``）在
``adjust cadence`` 之后同时断掉，pos_age 与 tap_age 冻结在同一秒。

看门狗做什么
------------
每 ``interval`` 探一次：

1. ``ping``（后台线程）—— 失败说明整条管道/策略进程没了，不是本工具的恢复范围；
2. ``get_asset``（主线程）—— 超时即判定"交易查询通道卡死"；
3. 连续 ``fail_threshold`` 次失败 → 发 ``reload_deployment``（同样走 adjust
   drain）：主线程还活着时能把运行态 reset 掉，是彻底挂死前最后的自愈机会；
4. reload 也超时 → 写 Redis 告警键 + CRITICAL 日志：**只有人工在 QMT 策略编辑器
   里重启策略能恢复**（GUI 是本机唯一手段，QMT 不暴露策略启停 API）。

保护
----
* 队列里有订单/撤单类 RPC 时不 reload——不能打断在途委托；
* 冷却窗内不重复 reload；每日次数上限，超限只告警；
* 只在交易时段自动 reload（其余时段只记录，不动手）。

部署
----
``register_strategy_watchdog.ps1`` 注册计划任务，每分钟跑一次本脚本。
全部依赖在 ``main()`` 里惰性导入，单测可在 Linux 上跑（见 tests/）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, 'bridge_config.json')
STATE_KEY = 'qm:qmt:strategy:watchdog'
QUEUE_KEY_TMPL = 'bigqmt:rpc:queue:%s'

#: 订单/撤单类方法：出现即禁止 reload（在途委托不能被 reset 打断）。
#: 写方法与 pipe_redis_bridge.is_order_method 同源，另加撤单。
ORDER_METHODS = frozenset({
    'order_stock', 'passorder', 'submit_order',
    'cancel_order_stock', 'cancel_order',
})

DEFAULT_CONFIG = {
    'enabled': True,
    'account_id': '',
    'pipe_name': 'bigqmt_rpc',
    'redis_host': '127.0.0.1',
    'redis_port': 6379,
    'redis_db': 5,
    'ping_timeout_s': 5.0,
    'asset_timeout_s': 10.0,
    'fail_threshold': 2,
    'reload_timeout_s': 25.0,
    'reload_cooldown_s': 600,
    'reload_max_per_day': 3,
    'state_ttl_s': 86400,
    'post_reload_wait_s': 8.0,
}


# ---------------------------------------------------------------------------
# 纯函数（可单测）
# ---------------------------------------------------------------------------
@dataclass
class Probe:
    """一次探测的结果。reachable=ping 通；account_ok=get_asset 通。"""

    reachable: bool
    account_ok: bool
    error: str = ''
    asset_ms: float = 0.0


@dataclass
class Decision:
    action: str          # ok | observe | reload | alert
    reason: str
    failures: int = 0
    state: dict = field(default_factory=dict)


def in_trading_window(now_ts: float) -> bool:
    """A 股交易时段（本地墙钟，北京时区）：09:15–11:35 / 12:55–15:10，工作日。"""
    lt = time.localtime(now_ts)
    if lt.tm_wday >= 5:
        return False
    hm = lt.tm_hour * 60 + lt.tm_min
    return (9 * 60 + 15 <= hm <= 11 * 60 + 35) or (12 * 60 + 55 <= hm <= 15 * 60 + 10)


def queue_has_order_requests(items, decode_payload=None) -> bool:
    """队列里是否有订单/撤单请求。

    ``items`` 是 Redis list 原始元素（bytes/str，``b64s:`` 编码）。解码失败按
    "有订单" 处理（fail-closed：宁可不 reload，也不冒打断委托的风险）。
    """
    if not items:
        return False
    if decode_payload is None:
        from bigqmt_signal_trader.redis_rpc import (  # noqa: PLC0415
            decode_rpc_request_payload,
        )

        decode_payload = decode_rpc_request_payload
    for item in items:
        try:
            raw = item.decode('utf-8', 'replace') if isinstance(item, bytes) else str(item)
            req = json.loads(decode_payload(raw))
            method = str((req or {}).get('method') or '').strip().lower()
        except Exception:  # noqa: BLE001 - 解不开就当有在途委托
            return True
        if method in ORDER_METHODS:
            return True
    return False


def evaluate(
    prev: dict,
    probe: Probe,
    *,
    now_ts: float,
    queue_has_orders: bool,
    cfg: dict | None = None,
) -> Decision:
    """根据上一次状态 + 本次探测 → 该做什么（纯函数，无 I/O）。

    state 里维护：``failures`` / ``last_reload_ts`` / ``reload_count`` / ``day``。
    """
    cfg = {**DEFAULT_CONFIG, **(cfg or {})}
    day = time.strftime('%Y%m%d', time.localtime(now_ts))
    reload_count = int(prev.get('reload_count') or 0)
    if str(prev.get('day') or '') != day:
        reload_count = 0
    state = {
        'day': day,
        'failures': int(prev.get('failures') or 0),
        'last_reload_ts': float(prev.get('last_reload_ts') or 0.0),
        'reload_count': reload_count,
    }

    if not probe.reachable:
        # ping 都不通：策略进程/管道整体不在，reload 无从谈起。
        state['failures'] = 0
        return Decision('alert', 'strategy unreachable (ping timeout)', 0, state)

    if probe.account_ok:
        state['failures'] = 0
        return Decision('ok', 'account query ok', 0, state)

    state['failures'] += 1
    failures = state['failures']
    if failures < int(cfg['fail_threshold']):
        return Decision('observe', f'account timeout ({failures}/'
                         f'{int(cfg["fail_threshold"])})', failures, state)

    if not in_trading_window(now_ts):
        return Decision('observe', 'account timeout outside trading hours',
                        failures, state)
    if queue_has_orders:
        return Decision('observe', 'order rpc in flight; reload deferred',
                        failures, state)
    if now_ts - state['last_reload_ts'] < float(cfg['reload_cooldown_s']):
        return Decision('observe', 'reload cooldown', failures, state)
    if reload_count >= int(cfg['reload_max_per_day']):
        return Decision('alert', 'reload budget exhausted; manual restart needed',
                        failures, state)

    state['reload_count'] = reload_count + 1
    state['last_reload_ts'] = now_ts
    return Decision('reload', 'account query hung; scheduling reload_deployment',
                    failures, state)


# ---------------------------------------------------------------------------
# I/O（Windows）
# ---------------------------------------------------------------------------
def load_config(path: str = CONFIG_PATH) -> dict:
    """读 bridge_config.json（与伴生桥同一份）：账户/管道名/Redis 地址单一来源。"""
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(path, encoding='utf-8') as fh:
            user = json.load(fh)
    except (OSError, ValueError):
        return cfg
    redis_cfg = user.get('redis') or {}
    if isinstance(redis_cfg, dict):
        cfg['redis_host'] = redis_cfg.get('host') or cfg['redis_host']
        cfg['redis_port'] = redis_cfg.get('port') or cfg['redis_port']
        db = redis_cfg.get('db')
        cfg['redis_db'] = cfg['redis_db'] if db is None else db
    pipe_cfg = user.get('pipe') or {}
    if isinstance(pipe_cfg, dict) and pipe_cfg.get('name'):
        cfg['pipe_name'] = str(pipe_cfg['name'])
    wd_cfg = user.get('watchdog') or {}
    if isinstance(wd_cfg, dict):
        cfg.update({k: v for k, v in wd_cfg.items() if k in DEFAULT_CONFIG})
    cfg['account_id'] = str(user.get('account_id') or cfg['account_id'])
    return cfg


def _connect_redis(cfg: dict):
    import redis  # noqa: PLC0415

    return redis.Redis(
        host=cfg.get('redis_host') or '127.0.0.1',
        port=int(cfg.get('redis_port') or 6379),
        db=int(cfg.get('redis_db') or 5),
        socket_timeout=5,
        socket_connect_timeout=5,
    )


def _build_transport(cfg: dict):
    sys.path.insert(0, os.environ.get(
        'QMT_VENV_SITE_PACKAGES', r'C:\xmt\venv\Lib\site-packages'))
    from bigqmt_signal_trader.transports.pipe_transport import (  # noqa: PLC0415
        NamedPipeTransport,
    )

    return NamedPipeTransport(
        account_id=str(cfg['account_id']),
        pipe_name=str(cfg.get('pipe_name') or 'bigqmt_rpc'),
        connect_timeout_seconds=3.0,
    )


def _call(transport, method: str, params: dict | None, timeout_s: float):
    """发一个 RPC；成功返回 data，失败返回 (None, 错误文本)。"""
    req_id = f'wd-{int(time.time() * 1000)}-{os.getpid()}'
    try:
        resp = transport.send_request(
            {'request_id': req_id, 'method': method, 'params': params or {}},
            timeout_seconds=float(timeout_s),
        )
    except Exception as exc:  # noqa: BLE001 - 超时/断管都按失败处理
        return None, f'{type(exc).__name__}: {exc}'
    if not isinstance(resp, dict) or not resp.get('ok', False):
        return None, str((resp or {}).get('error') or 'rpc_failed')
    return resp.get('data'), ''


def probe_once(transport, cfg: dict) -> Probe:
    ping, ping_err = _call(transport, 'ping', {}, cfg['ping_timeout_s'])
    if ping is None:
        return Probe(reachable=False, account_ok=False, error=ping_err)
    t0 = time.perf_counter()
    asset, asset_err = _call(transport, 'get_asset', {}, cfg['asset_timeout_s'])
    ms = (time.perf_counter() - t0) * 1000.0
    return Probe(reachable=True, account_ok=asset is not None,
                 error=asset_err, asset_ms=round(ms, 1))


def _load_state(redis_client) -> dict:
    try:
        raw = redis_client.hgetall(STATE_KEY) or {}
    except Exception:  # noqa: BLE001
        return {}
    out = {}
    for key, value in raw.items():
        k = key.decode() if isinstance(key, bytes) else key
        v = value.decode() if isinstance(value, bytes) else value
        out[k] = v
    for numeric in ('failures', 'last_reload_ts', 'reload_count'):
        try:
            out[numeric] = int(float(out.get(numeric) or 0))
        except (TypeError, ValueError):
            out[numeric] = 0
    out['last_reload_ts'] = float(out.get('last_reload_ts') or 0.0)
    return out


def _save_state(redis_client, decision: Decision, probe: Probe, cfg: dict) -> None:
    payload = {
        **{k: str(v) for k, v in decision.state.items()},
        'ts': str(int(time.time())),
        'action': decision.action,
        'reason': decision.reason,
        'reachable': '1' if probe.reachable else '0',
        'account_ok': '1' if probe.account_ok else '0',
        'asset_ms': str(probe.asset_ms),
        'last_error': probe.error[:200],
    }
    try:
        redis_client.hset(STATE_KEY, mapping=payload)
        redis_client.expire(STATE_KEY, int(cfg['state_ttl_s']))
    except Exception:  # noqa: BLE001
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='QMT 策略交易查询看门狗')
    parser.add_argument('--config', default=CONFIG_PATH)
    parser.add_argument('--dry-run', action='store_true', help='只探测不动作')
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    if not cfg.get('enabled', True):
        print('watchdog disabled')
        return 0
    if not cfg['account_id']:
        print('watchdog: account_id missing', file=sys.stderr)
        return 2

    transport = _build_transport(cfg)
    probe = probe_once(transport, cfg)

    queue_has_orders = False
    redis_client = None
    try:
        redis_client = _connect_redis(cfg)
        items = redis_client.lrange(QUEUE_KEY_TMPL % cfg['account_id'], 0, -1)
        queue_has_orders = queue_has_order_requests(items)
    except Exception as exc:  # noqa: BLE001 - 读不到队列按有单处理（fail-closed）
        queue_has_orders = True
        print(f'watchdog: queue check failed ({exc}); treating as order-in-flight',
              file=sys.stderr)

    prev = _load_state(redis_client) if redis_client is not None else {}
    decision = evaluate(prev, probe, now_ts=time.time(),
                        queue_has_orders=queue_has_orders, cfg=cfg)

    stamp = time.strftime('%Y-%m-%d %H:%M:%S')
    print(f'{stamp} action={decision.action} reason={decision.reason} '
          f'reachable={probe.reachable} account_ok={probe.account_ok} '
          f'asset_ms={probe.asset_ms} err={probe.error[:120]}')

    if decision.action == 'reload' and not args.dry_run:
        data, err = _call(transport, 'reload_deployment',
                          {}, cfg['reload_timeout_s'])
        if data is None:
            # 主线程已经连 reload 都处理不了：判定死锁，转人工。
            decision = Decision(
                'alert',
                f'reload_deployment failed ({err}); strategy main thread is '
                'deadlocked - restart the strategy in QMT strategy editor',
                decision.failures, decision.state)
            print(f'{stamp} ALERT {decision.reason}')
        else:
            time.sleep(float(cfg['post_reload_wait_s']))
            after = probe_once(transport, cfg)
            print(f'{stamp} reload scheduled={data} after_ok={after.account_ok} '
                  f'after_ms={after.asset_ms}')
            if not after.account_ok:
                decision = Decision(
                    'alert',
                    f'reload did not recover account queries ({after.error}); '
                    'restart the strategy in QMT strategy editor',
                    decision.failures, decision.state)

    if redis_client is not None:
        _save_state(redis_client, decision, probe, cfg)
    return 0 if decision.action in ('ok', 'observe') else 3


if __name__ == '__main__':
    sys.exit(main())
