# -*- coding: utf-8 -*-
"""伴生桥体检（给 watchdog 用）。

退出码（watchdog 必须区分）：
  0 = 心跳新鲜且账户数据新鲜
  1 = 心跳缺失/过期（进程级故障，watchdog 可重启）
  2 = 心跳读不到（Redis 侧问题）
  3 = 心跳正常但账户快照停摆（QMT 主线程不 tick / 管道卡死）——
      **只告警、不自动重启**：交易时段重启伴生桥会打断在途 RPC（含下单回报）。

数据新鲜度阈值：
  交易日 09:15–11:35 / 12:55–15:10 -> 300s
  其余时段 -> 3600s（休市不刷是正常态）
重写用:
  QMT_DATA_STALE_TRADING_S / QMT_DATA_STALE_IDLE_S
"""
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, r'C:\xmt\venv\Lib\site-packages')
import redis  # noqa: E402

THRESHOLD_S = 60
METRICS_KEY = 'qm:qmt:pipe:metrics'
REDIS_HOST = os.getenv('QMT_REDIS_HOST', '127.0.0.1')
REDIS_PORT = int(os.getenv('QMT_REDIS_PORT', '6379'))
REDIS_DB = int(os.getenv('QMT_REDIS_DB', '5'))


def _env_int(name, default):
    try:
        return max(30, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def _in_trading_window(now):
    if now.weekday() >= 5:
        return False
    hm = now.hour * 60 + now.minute
    return (9 * 60 + 15 <= hm <= 11 * 60 + 35) or (12 * 60 + 55 <= hm <= 15 * 60 + 10)


def _data_stale_threshold():
    now = datetime.now()
    if _in_trading_window(now):
        return _env_int('QMT_DATA_STALE_TRADING_S', 300)
    return _env_int('QMT_DATA_STALE_IDLE_S', 3600)


def main():
    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB,
                    socket_timeout=3, socket_connect_timeout=3)
    try:
        raw = r.get('qm:qmt:pipe:heartbeat')
    except Exception as exc:
        print('HEARTBEAT_UNREADABLE %r' % (exc,))
        return 2
    if not raw:
        print('HEARTBEAT_MISSING')
        return 1
    try:
        age = time.time() - float(raw)
    except Exception:
        print('HEARTBEAT_BAD_VALUE %r' % (raw,))
        return 1
    if age >= THRESHOLD_S:
        print('HEARTBEAT_AGE %.0fs' % age)
        return 1

    # 心跳只证明进程活着：再查账户快照新鲜度（RPC 停摆时心跳仍新鲜）
    try:
        pos_raw = r.hget(METRICS_KEY, 'pos_age_s')
        pos_age = float(pos_raw) if pos_raw is not None else None
    except Exception as exc:
        print('METRICS_UNREADABLE %r' % (exc,))
        return 2
    threshold = _data_stale_threshold()
    if pos_age is not None and pos_age > threshold:
        print('POS_STALE %.0fs (threshold %ss)' % (pos_age, threshold))
        return 3
    pos_text = ('%.0fs' % pos_age) if pos_age is not None else 'unknown'
    print('HEARTBEAT_AGE %.0fs POS_AGE %s' % (age, pos_text))
    return 0


if __name__ == '__main__':
    sys.exit(main())
