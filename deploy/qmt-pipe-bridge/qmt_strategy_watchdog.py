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
import threading
import os
import sys
import time
from dataclasses import dataclass, field

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, 'bridge_config.json')
STATE_KEY = 'qm:qmt:strategy:watchdog'
METRICS_KEY = 'qm:qmt:pipe:metrics'
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
    # reload 也救不回来时，重启伴生桥（实测：断管道连接能解锁策略主线程，
    # 2026-11-06 两次现场 11:28:40 / 11:32:45）。
    'bridge_restart_after_s': 60,
    'bridge_restart_max_per_hour': 6,
    'bridge_start_cmd': r'C:\new_tdx_mock\PYPlugins\bridge-windows\qmt-tools\start_pipe_bridge.cmd',
    'bridge_restart_wait_s': 15.0,
    # 自愈顺序：首选重启伴生桥（断管道解锁主线程），reload_deployment 降为兜底。
    # 2026-11-10 实测：reload 只重载 deployment、不重跑 init，行情 Tap 注册随之丢失
    #（日志里 `[v2] quote tap installed` 不再出现），之后只能人工在 QMT 编辑器里
    # 停止/运行策略才能装回——"每次都要重启"就是这条自愈路径造成的。
    'prefer_bridge_restart': True,
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
        # 本次 unresponsive 的起始时间（桥重启判定用）：成功即刻清零。
        'unresponsive_since': float(prev.get('unresponsive_since') or 0.0),
        'bridge_restart_ts': str(prev.get('bridge_restart_ts') or ''),
    }

    if not probe.reachable:
        # ping 都不通：策略进程/管道整体不在，reload 无从谈起。
        # 但**必须记下 unresponsive_since**：管道 BUSY（err=231）时策略主线程卡死、
        # 后台线程仍活着，重连也一样失败——只有「重启伴生桥断开管道」能解锁。
        # 2026-11-06 现场：16:34 起 err=231，旧逻辑只报 alert、永不重启桥，
        # 两小时无人能恢复；main 里的 alert 分支正是靠这个时间戳才动手。
        # 交易时段外不记：QMT 关了就别拿桥当开关，免得整夜重启风暴。
        state['failures'] = 0
        if in_trading_window(now_ts) and not state['unresponsive_since']:
            state['unresponsive_since'] = now_ts
        return Decision('alert', 'strategy unreachable (ping timeout)', 0, state)

    if probe.account_ok:
        state['failures'] = 0
        state['unresponsive_since'] = 0.0
        return Decision('ok', 'account query ok', 0, state)

    state['failures'] += 1
    failures = state['failures']
    if failures >= int(cfg['fail_threshold']) and not state['unresponsive_since']:
        state['unresponsive_since'] = now_ts
    if failures < int(cfg['fail_threshold']):
        return Decision('observe', f'account timeout ({failures}/'
                         f'{int(cfg["fail_threshold"])})', failures, state)

    if not in_trading_window(now_ts):
        return Decision('observe', 'account timeout outside trading hours',
                        failures, state)
    if queue_has_orders:
        return Decision('observe', 'order rpc in flight; reload deferred',
                        failures, state)

    # 自愈首选：重启伴生桥（断管道解锁主线程，不碰策略运行态 → Tap 不丢）。
    # 2026-11-10 实测：reload_deployment 只重载 deployment、不重跑 init，策略的
    # 行情 Tap 注册丢失（`[v2] quote tap installed` 不再出现），之后只能人工在
    # QMT 编辑器里停止/运行策略——"每次都要重启"就是先 reload 造成的。
    if bool(cfg.get('prefer_bridge_restart', True)):
        allowed, why = should_restart_bridge(state, now_ts, cfg)
        if allowed:
            return Decision('restart_bridge', f'account hung; {why}',
                            failures, state)
        since = float(state.get('unresponsive_since') or 0.0)
        if since > 0 and now_ts - since < float(cfg['bridge_restart_after_s']):
            # 先等满 bridge_restart_after_s，别急着 reload（reload 会丢 Tap）。
            return Decision('observe', f'bridge restart window ({why})',
                            failures, state)
        if now_ts - state['last_reload_ts'] < float(cfg['reload_cooldown_s']):
            return Decision('observe', f'reload cooldown ({why})', failures, state)
        if reload_count >= int(cfg['reload_max_per_day']):
            return Decision('alert', 'reload budget exhausted; manual restart needed',
                            failures, state)
        state['reload_count'] = reload_count + 1
        state['last_reload_ts'] = now_ts
        return Decision('reload', 'account query hung; bridge restart unavailable; '
                        'scheduling reload_deployment', failures, state)

    # 旧顺序（先 reload）：仅当显式配置 prefer_bridge_restart=false 时使用。
    if now_ts - state['last_reload_ts'] < float(cfg['reload_cooldown_s']):
        # reload 冷却中也不该干等：桥重启（断管道）是更轻、更快的恢复手段。
        allowed, why = should_restart_bridge(state, now_ts, cfg)
        if allowed:
            return Decision('restart_bridge', f'account hung; {why}',
                            failures, state)
        return Decision('observe', f'reload cooldown ({why})', failures, state)
    if reload_count >= int(cfg['reload_max_per_day']):
        return Decision('alert', 'reload budget exhausted; manual restart needed',
                        failures, state)

    state['reload_count'] = reload_count + 1
    state['last_reload_ts'] = now_ts
    return Decision('reload', 'account query hung; scheduling reload_deployment',
                    failures, state)


def should_restart_bridge(prev: dict, now_ts: float,
                          cfg: dict | None = None) -> tuple[bool, str]:
    """reload 救不回来时，是否该重启伴生桥（纯函数）。

    实测（2026-11-06 两次现场 11:28:40 / 11:32:45）：断开管道连接能让卡在
    QMT C++ 调用里的策略主线程立刻解锁；而空载 7 分钟不会自愈。所以「挂死后
    重启桥」是唯一不需要人工去 QMT 点重启的恢复手段。加冷却窗与小时上限，
    防止把桥当成重启风暴的开关。
    """
    cfg = {**DEFAULT_CONFIG, **(cfg or {})}
    since = float(prev.get('unresponsive_since') or 0.0)
    if since <= 0:
        return False, 'not unresponsive'
    waited = now_ts - since
    threshold = float(cfg['bridge_restart_after_s'])
    if waited < threshold:
        return False, f'unresponsive {waited:.0f}s < {int(threshold)}s'
    stamps = [float(x) for x in str(prev.get('bridge_restart_ts') or '').split(',')
              if x]
    recent = [s for s in stamps if now_ts - s < 3600]
    if len(recent) >= int(cfg['bridge_restart_max_per_hour']):
        return False, 'bridge restart hourly budget exhausted'
    return True, f'unresponsive {waited:.0f}s'


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


def _tap_state(redis_client) -> str:
    """读伴生桥的行情 Tap 状态（``qm:qmt:pipe:metrics.tap_state``）。

    reload_deployment 之后即使账户查询恢复，策略的 Tap 注册也可能已经丢了
    （2026-11-10 实测：`quote_tap_subscribe: rpc method is not allowed`）。
    这种情况不能判「已恢复」，否则下游看到的就是「账户正常但行情无推送」。
    """
    if redis_client is None:
        return ''
    try:
        raw = redis_client.hget(METRICS_KEY, 'tap_state')
    except Exception:  # noqa: BLE001 - 读不到不阻塞恢复判定
        return ''
    return raw.decode() if isinstance(raw, bytes) else str(raw or '')


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


def _restart_bridge(cfg: dict) -> bool:
    """杀掉伴生桥并脱离本会话重新拉起。成功返回 True。

    只动 pipe_redis_bridge 进程（按命令行匹配，绝不按进程名杀——容器里所有
    服务都叫 python 的教训同理）。拉起用 DETACHED_PROCESS，避免随本进程退出。
    """
    import subprocess  # noqa: PLC0415

    killed = False
    try:
        import psutil  # noqa: PLC0415

        for proc in psutil.process_iter(['pid', 'cmdline']):
            try:
                cmdline = ' '.join(proc.info.get('cmdline') or [])
            except Exception:  # noqa: BLE001
                continue
            if 'pipe_redis_bridge' in cmdline:
                proc.kill()
                killed = True
    except Exception:  # noqa: BLE001 - 没有 psutil 就退回 wmic/taskkill
        try:
            out = subprocess.run(
                ['wmic', 'process', 'where', "name='python.exe'", 'get',
                 'ProcessId,CommandLine', '/format:csv'],
                capture_output=True, text=True, timeout=15).stdout
            for line in out.splitlines():
                if 'pipe_redis_bridge' in line:
                    pid = line.rstrip().split(',')[-1].strip()
                    if pid.isdigit():
                        subprocess.run(['taskkill', '/F', '/PID', pid],
                                       capture_output=True, timeout=10)
                        killed = True
        except Exception:  # noqa: BLE001
            return False
    if not killed:
        # 桥本来就没在跑：等价于需要拉起，继续。
        pass
    time.sleep(2)
    flags = 0
    for name in ('DETACHED_PROCESS', 'CREATE_NEW_PROCESS_GROUP'):
        flags |= getattr(subprocess, name, 0)
    try:
        subprocess.Popen(
            ['cmd.exe', '/c', str(cfg['bridge_start_cmd'])],
            creationflags=flags, close_fds=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
    except Exception:  # noqa: BLE001
        return False
    return True


def _with_timeout(fn, seconds: float):
    """在守护线程里跑 fn，超时返回 (None, True)。

    管道的同步 ReadFile 在句柄楔死时，transport 自带的 watchdog 定时器可能
    取消不掉（2026-11-06 现场：看门狗进程卡在 reload 调用上 8 分钟，计划任务
    一直是 Running，后续每分钟触发全被 IgnoreNew 吞掉——自愈彻底失效）。
    线程超时后主流程继续走「重启桥」，卡住的线程随进程退出。
    """
    result: dict = {}

    def _run():
        try:
            result["value"] = fn()
        except Exception as exc:  # noqa: BLE001
            result["error"] = exc

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.join(float(seconds))
    if thread.is_alive():
        return None, True
    return result.get("value"), False


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
    probe, probe_hung = _with_timeout(lambda: probe_once(transport, cfg), 20.0)
    if probe_hung or probe is None:
        # 探测都卡住：进程多半还活着，是管道楔死——按「账户不可用」推进重启。
        probe = Probe(reachable=True, account_ok=False,
                      error='probe timeout (pipe wedged)')

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

    def _restart_or_alert(dec: Decision, why: str) -> Decision:
        """先尝试重启伴生桥（断管道解锁策略），失败才降级为人工告警。"""
        if args.dry_run:
            return Decision('alert', f'{why}; dry-run, no action',
                            dec.failures, dec.state)
        if queue_has_orders:
            return Decision('alert', f'{why}; order rpc in flight, bridge restart '
                            'deferred - check manually', dec.failures, dec.state)
        allowed, reason = should_restart_bridge(dec.state, time.time(), cfg)
        if not allowed:
            return Decision('alert', f'{why}; bridge restart skipped ({reason}) - '
                            'restart the strategy in QMT strategy editor',
                            dec.failures, dec.state)
        stamps = [s for s in str(dec.state.get('bridge_restart_ts') or '').split(',')
                  if s]
        stamps.append(str(int(time.time())))
        dec.state['bridge_restart_ts'] = ','.join(stamps)
        started = _restart_bridge(cfg)
        print(f'{stamp} BRIDGE RESTART started={started}')
        if not started:
            return Decision('alert', f'{why}; bridge restart failed to start',
                            dec.failures, dec.state)
        time.sleep(float(cfg['bridge_restart_wait_s']))
        after = probe_once(transport, cfg)
        print(f'{stamp} after bridge restart: account_ok={after.account_ok} '
              f'ms={after.asset_ms} err={after.error[:120]}')
        if after.account_ok:
            tap = _tap_state(redis_client)
            dec.state['tap_state'] = tap
            dec.state['unresponsive_since'] = 0.0
            dec.state['failures'] = 0
            if tap and tap != 'on':
                return Decision(
                    'alert', 'account recovered but quote tap is down '
                    f'(tap_state={tap}); restart the strategy in QMT strategy '
                    'editor to reinstall the tap', 0, dec.state)
            return Decision('ok', 'account query ok after bridge restart', 0,
                            dec.state)
        return Decision('alert', f'{why}; bridge restarted but account still hung '
                        f'({after.error}) - restart the strategy in QMT strategy '
                        'editor', dec.failures, dec.state)

    if decision.action == 'reload' and not args.dry_run:
        data, err = _with_timeout(
            lambda: _call(transport, 'reload_deployment', {},
                          cfg['reload_timeout_s']),
            float(cfg['reload_timeout_s']) + 10.0)
        if data is None and err is None:
            err = 'reload_deployment timed out (pipe wedged)'
        if data is None:
            # 主线程已经连 reload 都处理不了：先试重启桥，再降级人工。
            decision = _restart_or_alert(
                decision, f'reload_deployment failed ({err})')
            print(f'{stamp} {decision.action.upper()} {decision.reason}')
        else:
            time.sleep(float(cfg['post_reload_wait_s']))
            after = probe_once(transport, cfg)
            print(f'{stamp} reload scheduled={data} after_ok={after.account_ok} '
                  f'after_ms={after.asset_ms}')
            if not after.account_ok:
                decision = _restart_or_alert(
                    decision, f'reload did not recover account queries ({after.error})')
                print(f'{stamp} {decision.action.upper()} {decision.reason}')
            else:
                tap = _tap_state(redis_client)
                decision.state['tap_state'] = tap
                if tap and tap != 'on':
                    # reload 换掉了策略运行态但没重跑 init：账户回来了、Tap 没了。
                    # 这一步只有 QMT 编辑器里的「停止 → 运行」能补，继续自动
                    # reload 只会多丢一次，直接告警收口。
                    decision = Decision(
                        'alert', 'reload recovered account queries but lost the '
                        f'quote tap (tap_state={tap}); restart the strategy in '
                        'QMT strategy editor', decision.failures, decision.state)
                    print(f'{stamp} {decision.action.upper()} {decision.reason}')
    elif decision.action == 'restart_bridge' and not args.dry_run:
        decision = _restart_or_alert(decision, decision.reason)
        print(f'{stamp} {decision.action.upper()} {decision.reason}')
    elif decision.action == 'alert' and float(decision.state.get('unresponsive_since') or 0):
        # reload 预算耗尽等场景：桥重启仍是可用的恢复手段。
        decision = _restart_or_alert(decision, decision.reason)
        print(f'{stamp} {decision.action.upper()} {decision.reason}')

    if redis_client is not None:
        _save_state(redis_client, decision, probe, cfg)
    return 0 if decision.action in ('ok', 'observe') else 3


if __name__ == '__main__':
    sys.exit(main())
