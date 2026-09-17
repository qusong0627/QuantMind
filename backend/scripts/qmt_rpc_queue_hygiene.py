#!/usr/bin/env python3
"""大 QMT 桥 RPC 队列卫生（运维工具）：体检 / 清理积压请求。

**为什么需要**：桥（Windows 侧 big-convert RPC runtime）离线时，容器内各调用方
（账户同步/执行轮询/备源席订阅）仍按节拍入队 → 队列单调积压（2026-09-17 实测
09:52 离线 → 13:10 积压 580 条）。桥恢复瞬间会把积压一次性重放：老订阅请求会让
服务端重复建立订阅引用（引用计数永不释放）、老查询挤占带宽与新请求延迟。
因此**桥恢复前**应先行体检并清理。

**安全护栏（机构级，不可绕过）**：
- 默认 **dry-run**（只报告，不改动）；
- 清理**永不删除订单类方法**（``ORDER_METHODS``，含下单/撤单）——发现订单类条目
  即单独列出并跳过，无论传入什么参数；
- 只删显式指定的方法（``--drop-methods``）或"保留最新 N 条"之外的条目（``--keep-last``）。

用法（容器内）::

    python scripts/qmt_rpc_queue_hygiene.py --account 40327478            # 体检
    python scripts/qmt_rpc_queue_hygiene.py --account 40327478 \
        --trim --drop-methods query_stock_orders,query_stock_asset,subscribe_whole_quote
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))


def _decode_entry(raw: bytes) -> dict | None:
    """桥 RPC 载荷解码（b64s: base64 + 数字替换；用 kit 自带实现，避免第二份口径）。"""
    try:
        from bigqmt_signal_trader.redis_rpc import decode_rpc_request_payload
    except Exception:  # noqa: BLE001
        return None
    try:
        text = raw.decode("utf-8", "ignore") if isinstance(raw, bytes) else str(raw)
        return json.loads(decode_rpc_request_payload(text))
    except Exception:  # noqa: BLE001
        return None


def _order_methods() -> set[str]:
    try:
        from bigqmt_signal_trader.redis_rpc import ORDER_METHODS

        return set(ORDER_METHODS)
    except Exception:  # noqa: BLE001
        # 保守兜底：任何名字含 order 的方法都按订单类处理（宁可漏删不可误删）
        return set()


def _read_methods() -> set[str]:
    try:
        from bigqmt_signal_trader.redis_rpc import READ_METHODS

        return set(READ_METHODS)
    except Exception:  # noqa: BLE001
        return set()


_READ_PREFIXES = ("query_", "get_", "subscribe", "unsubscribe", "ping", "status", "describe_", "list_")


def _is_order_method(method: str) -> bool:
    """订单类判定（护栏唯一口径）：kit 常量 → kit 只读集 → 只读前缀 → 名字启发式。

    - 写入类（``passorder``/``order_stock``/``cancel_*``/``submit_*``）：kit 常量或
      名字启发式命中 → **永不删除**；
    - 明显只读类（``query_*``/``get_*``/``subscribe*`` 等前缀，且不在 kit 订单常量里）：
      放行，避免 ``query_stock_orders`` 这类含 "order" 字面的只读方法被误保护。
    """
    m = str(method or "")
    if m in _order_methods():
        return True
    if m in _read_methods():
        return False
    low = m.lower()
    if any(low.startswith(p) for p in _READ_PREFIXES):
        return False
    return any(tok in low for tok in ("order", "cancel", "passorder"))


def _bridge_redis_params() -> dict:
    """桥 Redis 解析：页面配置（broker:config:qmt_exec）> QMT_EXEC_* env > 通用 REDIS_*。"""
    page: dict = {}
    try:
        from backend.services.live_trading.services.qmt_exec_client import load_broker_settings

        page = load_broker_settings() or {}
    except Exception:  # noqa: BLE001
        page = {}
    host = str(page.get("redis_host") or os.getenv("QMT_EXEC_REDIS_HOST") or os.getenv("REDIS_HOST") or "redis")
    try:
        port = int(page.get("redis_port") or os.getenv("QMT_EXEC_REDIS_PORT") or os.getenv("REDIS_PORT") or 6379)
    except (TypeError, ValueError):
        port = 6379
    try:
        db = int(page.get("redis_db") or os.getenv("QMT_EXEC_REDIS_DB") or 0)
    except (TypeError, ValueError):
        db = 0
    password = page.get("redis_password") or os.getenv("QMT_EXEC_REDIS_PASSWORD") or os.getenv("REDIS_PASSWORD") or None
    return {"host": host, "port": port, "db": db, "password": password}


def _client(params: dict):
    import redis as _redis

    return _redis.Redis(
        host=params["host"],
        port=params["port"],
        db=params["db"],
        password=params["password"],
        decode_responses=False,
        socket_connect_timeout=5,
        socket_timeout=10,
    )


def survey(client, queue_key: str) -> tuple[Counter, list[dict]]:
    """体检：方法直方图 + 订单类条目清单（不解码的条目计入 UNDECODED）。"""
    items = client.lrange(queue_key, 0, -1)
    hist: Counter = Counter()
    order_rows: list[dict] = []
    for raw in items:
        payload = _decode_entry(raw)
        if payload is None:
            hist["UNDECODED"] += 1
            continue
        method = str(payload.get("method") or "?")
        hist[method] += 1
        if _is_order_method(method):
            order_rows.append(
                {
                    "method": method,
                    "request_id": payload.get("request_id"),
                    "params": json.dumps(payload.get("params") or {}, ensure_ascii=False)[:200],
                }
            )
    return hist, order_rows


def trim(client, queue_key: str, *, drop_methods: set[str], keep_last: int | None) -> dict:
    """按方法白名单删除（订单类方法永不删除）。返回统计。"""
    items = client.lrange(queue_key, 0, -1)
    keep_from = len(items) - int(keep_last) if keep_last else 0
    removed = 0
    kept_order = 0
    for idx, raw in enumerate(items):
        payload = _decode_entry(raw)
        if payload is None:
            continue
        method = str(payload.get("method") or "")
        if _is_order_method(method):
            kept_order += 1
            continue  # 硬护栏：订单类条目永不删除
        in_scope = method in drop_methods if drop_methods else False
        if keep_last is not None and idx < keep_from:
            in_scope = True
        if in_scope:
            client.lrem(queue_key, 1, raw)
            removed += 1
    return {"removed": removed, "kept_order_methods": kept_order}


def main() -> int:
    ap = argparse.ArgumentParser(description="大 QMT 桥 RPC 队列卫生")
    ap.add_argument("--account", required=True, help="资金账号（队列键后缀）")
    ap.add_argument("--trim", action="store_true", help="执行清理（默认 dry-run 只报告）")
    ap.add_argument("--drop-methods", default="", help="要删除的方法（逗号分隔）")
    ap.add_argument("--keep-last", type=int, default=None, help="仅保留最新 N 条，其余删除")
    args = ap.parse_args()

    params = _bridge_redis_params()
    client = _client(params)
    queue_key = f"bigqmt:rpc:queue:{args.account}"
    try:
        client.ping()
    except Exception as exc:  # noqa: BLE001
        print(f"[hygiene] 桥 Redis 不可达 {params['host']}:{params['port']} db{params['db']}: {exc}")
        return 2
    try:
        hist, order_rows = survey(client, queue_key)
        total = sum(hist.values())
        print(f"[hygiene] 队列 {queue_key} db={params['db']} 共 {total} 条")
        for method, n in hist.most_common():
            flag = "  <-- 订单类（永不删除）" if _is_order_method(method) else ""
            print(f"    {method:32s} {n}{flag}")
        if order_rows:
            print(f"[hygiene] ⚠ 发现 {len(order_rows)} 条订单类请求（人工核对后由桥消费，本工具不动）：")
            for row in order_rows[:10]:
                print("      ", row)
        if not args.trim:
            print("[hygiene] dry-run（未改动）。清理示例："
                  "--trim --drop-methods query_stock_orders,query_stock_asset,subscribe_whole_quote")
            return 0
        drop = {m.strip() for m in args.drop_methods.split(",") if m.strip()}
        if not drop and args.keep_last is None:
            print("[hygiene] --trim 需配合 --drop-methods 或 --keep-last（拒绝无差别清空）")
            return 2
        blocked = sorted(m for m in drop if _is_order_method(m))
        if blocked:
            print(f"[hygiene] 拒绝：方法含订单类 {blocked}（硬护栏）")
            return 2
        stats = trim(client, queue_key, drop_methods=drop, keep_last=args.keep_last)
        print(f"[hygiene] 已删除 {stats['removed']} 条；订单类保留 {stats['kept_order_methods']} 条")
        hist2, _ = survey(client, queue_key)
        print(f"[hygiene] 清理后队列 {sum(hist2.values())} 条: {dict(hist2)}")
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
