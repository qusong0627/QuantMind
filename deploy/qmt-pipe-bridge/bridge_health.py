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

import json
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, r"C:\xmt\venv\Lib\site-packages")
import redis  # noqa: E402

try:  # 与伴生桥同目录；测试/独立运行时允许缺失
    import bridge_order_intent  # noqa: E402
    import bridge_gates  # noqa: E402
except Exception:  # noqa: BLE001 - 健康检查不能因诊断模块缺失而崩
    bridge_order_intent = None
    bridge_gates = None

THRESHOLD_S = 60
METRICS_KEY = "qm:qmt:pipe:metrics"
PROCESSING_KEY_TMPL = "bigqmt:rpc:processing:%s"
INFLIGHT_KEY_TMPL = "qm:qmt:pipe:inflight:%s"

#: 与伴生桥同一份配置：健康检查必须读桥实际写入的 Redis。
#: 旧实现只看 env/127.0.0.1，而桥配置指向 192.168.31.68 → watchdog 永远
#: 读不到心跳，误判「进程死了」反复重启健康桥（2026-11-16 现场）。
CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "bridge_config.json"
)


def _redis_config() -> dict:
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return {}
    redis_cfg = doc.get("redis") if isinstance(doc, dict) else None
    return redis_cfg if isinstance(redis_cfg, dict) else {}


def _bridge_config() -> dict:
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _resolve_db(env_db, cfg_db, default=5):
    """显式 0 是合法 DB；只有 None/空串才回退。"""
    if env_db not in (None, ""):
        try:
            return int(env_db)
        except (TypeError, ValueError):
            pass
    if cfg_db is not None:
        try:
            return int(cfg_db)
        except (TypeError, ValueError):
            pass
    return default


_CFG = _redis_config()
_BRIDGE_CFG = _bridge_config()
REDIS_HOST = os.getenv("QMT_REDIS_HOST") or _CFG.get("host") or "127.0.0.1"
REDIS_PORT = int(os.getenv("QMT_REDIS_PORT") or _CFG.get("port") or 6379)
# 显式 DB 0 必须保留：旧写法 `... or 5` 会把 0 吞成 5。
REDIS_DB = _resolve_db(os.getenv("QMT_REDIS_DB"), _CFG.get("db"))


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
        return _env_int("QMT_DATA_STALE_TRADING_S", 300)
    return _env_int("QMT_DATA_STALE_IDLE_S", 3600)


def _asset_stale_threshold():
    """资产独立阈值：默认 120s（2× asset_refresh_s），与持仓 300s 分开。"""
    positions = _BRIDGE_CFG.get("positions")
    if not isinstance(positions, dict):
        return 120
    try:
        value = positions.get("asset_stale_s")
        return max(30, int(value if value is not None else 120))
    except (TypeError, ValueError):
        return 120


def _account_id() -> str:
    return str(_BRIDGE_CFG.get("account_id") or "")


def _decode_request(text):
    try:
        from bigqmt_signal_trader.redis_rpc import (
            decode_rpc_request_payload,
        )  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        decode_rpc_request_payload = lambda raw: raw  # noqa: E731
    try:
        return json.loads(decode_rpc_request_payload(text))
    except Exception:  # noqa: BLE001
        return None


def _has_unresolved_orders(r) -> bool:
    """inflight hash 或 processing 列表里是否有订单/未知结果。

    只看队列会漏掉「BLMOVE 已取走、进程随后崩溃」的那一笔；这里同时看
    inflight hash（可读账本）与 processing 列表（原子落点）。
    """
    account = _account_id()
    if not account:
        return False
    try:
        records = r.hvals(INFLIGHT_KEY_TMPL % account) or []
    except Exception:  # noqa: BLE001
        records = []
    for raw in records:
        try:
            doc = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        except Exception:  # noqa: BLE001
            return True
        state = str((doc or {}).get("state") or "")
        if state in ("inflight", "unknown"):
            return True
    try:
        items = r.lrange(PROCESSING_KEY_TMPL % account, 0, -1) or []
    except Exception:  # noqa: BLE001
        return False
    for raw in items:
        text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
        req = _decode_request(text)
        if req is None:
            return True
        method = str((req or {}).get("method") or "")
        if bridge_order_intent is not None:
            if bridge_order_intent.is_order_method(method):
                return True
        elif bridge_gates is not None and bridge_gates.is_order_like(method):
            return True
    return False


def main():
    r = redis.Redis(
        host=REDIS_HOST,
        port=REDIS_PORT,
        db=REDIS_DB,
        socket_timeout=3,
        socket_connect_timeout=3,
    )
    try:
        raw = r.get("qm:qmt:pipe:heartbeat")
    except Exception as exc:
        print("HEARTBEAT_UNREADABLE %r" % (exc,))
        return 2
    if not raw:
        print("HEARTBEAT_MISSING")
        return 1
    try:
        age = time.time() - float(raw)
    except Exception:
        print("HEARTBEAT_BAD_VALUE %r" % (raw,))
        return 1
    if age >= THRESHOLD_S:
        print("HEARTBEAT_AGE %.0fs" % age)
        return 1

    # 心跳只证明进程活着：再查账户快照新鲜度（RPC 停摆时心跳仍新鲜）
    try:
        pos_raw = r.hget(METRICS_KEY, "pos_age_s")
        pos_age = float(pos_raw) if pos_raw is not None else None
    except Exception as exc:
        print("METRICS_UNREADABLE %r" % (exc,))
        return 2
    # 有在途/未知订单时，宁可让看门狗少重启一次，也不能打断回报。
    try:
        unresolved = _has_unresolved_orders(r)
    except Exception as exc:  # noqa: BLE001 - 诊断失败按"有单"处理
        print("INFLIGHT_CHECK_FAILED %r (no restart)" % (exc,))
        return 4
    if unresolved:
        print("INFLIGHT unresolved order intent present (no restart)")
        return 4

    threshold = _data_stale_threshold()
    in_trading = _in_trading_window(datetime.now())
    if pos_age is not None and pos_age < 0 and in_trading:
        print("POS_NEVER_OBSERVED (trading hours)")
        return 3
    if pos_age is not None and pos_age > threshold:
        print("POS_STALE %.0fs (threshold %ss)" % (pos_age, threshold))
        return 3
    try:
        asset_raw = r.hget(METRICS_KEY, "asset_age_s")
        asset_age = float(asset_raw) if asset_raw is not None else None
    except Exception:  # noqa: BLE001 - 旧版桥没有该指标，不阻塞
        asset_age = None
    if asset_age is not None and asset_age < 0 and in_trading:
        print("ASSET_NEVER_OBSERVED (trading hours)")
        return 3
    if asset_age is not None and asset_age > _asset_stale_threshold():
        print(
            "ASSET_STALE %.0fs (threshold %ss)" % (asset_age, _asset_stale_threshold())
        )
        return 3
    pos_text = ("%.0fs" % pos_age) if pos_age is not None else "unknown"
    asset_text = ("%.0fs" % asset_age) if asset_age is not None else "unknown"
    print("HEARTBEAT_AGE %.0fs POS_AGE %s ASSET_AGE %s" % (age, pos_text, asset_text))
    return 0


if __name__ == "__main__":
    sys.exit(main())
