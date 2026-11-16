# -*- coding: utf-8 -*-
"""伴生桥订单意图运维工具（人工核对柜台后使用）。

用法::

    python bridge_ops.py list
    python bridge_ops.py resolve --request-id <id> --outcome filled --note "柜台已成交"

``resolve`` 只做两件事：把 processing 里的原始条目移走、把 inflight 记录归档到
``qm:qmt:pipe:inflight:resolved:{account}``。**它不会重发订单、不会改订单账本**。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "bridge_config.json")

DEFAULT_CONFIG = {
    "redis": {"host": "127.0.0.1", "port": 6379, "db": 5},
    "account_id": "",
}
PROCESSING_KEY_TMPL = "bigqmt:rpc:processing:%s"
INFLIGHT_KEY_TMPL = "qm:qmt:pipe:inflight:%s"
RESOLVED_KEY_TMPL = "qm:qmt:pipe:inflight:resolved:%s"


def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            user = json.load(fh)
    except (OSError, ValueError):
        return cfg
    if isinstance(user, dict):
        redis_cfg = user.get("redis")
        if isinstance(redis_cfg, dict):
            cfg["redis"].update(redis_cfg)
        if user.get("account_id"):
            cfg["account_id"] = str(user["account_id"])
    return cfg


def connect(cfg):
    import redis

    rcfg = cfg.get("redis") or {}
    return redis.Redis(
        host=rcfg.get("host") or "127.0.0.1",
        port=int(rcfg.get("port") or 6379),
        db=int(rcfg.get("db") if rcfg.get("db") is not None else 5),
        socket_timeout=5,
        socket_connect_timeout=5,
    )


def _text(value):
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)


def _decode_request(raw):
    try:
        from bigqmt_signal_trader.redis_rpc import decode_rpc_request_payload
    except Exception:  # noqa: BLE001
        decode_rpc_request_payload = lambda payload: payload  # noqa: E731
    try:
        return json.loads(decode_rpc_request_payload(_text(raw)))
    except Exception:  # noqa: BLE001
        return None


def list_intents(cfg, client) -> int:
    account = str(cfg.get("account_id") or "")
    if not account:
        print("account_id missing in bridge_config.json", file=sys.stderr)
        return 2
    rows = []
    for raw in client.hvals(INFLIGHT_KEY_TMPL % account) or []:
        try:
            doc = json.loads(_text(raw))
        except Exception:  # noqa: BLE001
            rows.append({"state": "unparseable", "raw": _text(raw)[:200]})
            continue
        rows.append(
            {
                "request_id": doc.get("request_id"),
                "state": doc.get("state"),
                "method": doc.get("method"),
                "started_at": doc.get("started_at"),
                "error": doc.get("error"),
            }
        )
    processing = []
    for raw in client.lrange(PROCESSING_KEY_TMPL % account, 0, -1) or []:
        req = _decode_request(raw)
        processing.append(
            {
                "request_id": (req or {}).get("request_id"),
                "method": (req or {}).get("method"),
                "raw_len": len(_text(raw)),
            }
        )
    print(
        json.dumps(
            {
                "account_id": account,
                "inflight": rows,
                "processing": processing,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _find_processing_raw(client, account, request_id):
    for raw in client.lrange(PROCESSING_KEY_TMPL % account, 0, -1) or []:
        req = _decode_request(raw)
        if str((req or {}).get("request_id") or "") == str(request_id):
            return raw, req
    return None, None


def resolve_intent(cfg, client, *, request_id, outcome, note) -> int:
    account = str(cfg.get("account_id") or "")
    if not account:
        print("account_id missing in bridge_config.json", file=sys.stderr)
        return 2
    inflight_key = INFLIGHT_KEY_TMPL % account
    raw_value = client.hget(inflight_key, str(request_id))
    record = {}
    if raw_value:
        try:
            record = json.loads(_text(raw_value))
        except Exception:  # noqa: BLE001
            record = {"unparseable": _text(raw_value)[:500]}
    raw = None
    req = None
    # 先按 inflight 记录里的原始条目定位：recover_processing 可能用合成 id
    # 标 unknown，那个 id 不在 raw 里；按 request_id 搜会找不到。
    stored_raw = record.get("raw") if isinstance(record, dict) else None
    if stored_raw:
        for item in client.lrange(PROCESSING_KEY_TMPL % account, 0, -1) or []:
            if _text(item) == str(stored_raw):
                raw, req = item, _decode_request(item)
                break
    if raw is None:
        raw, req = _find_processing_raw(client, account, request_id)
    if not raw_value and raw is None:
        print("request_id not found in inflight or processing", file=sys.stderr)
        return 3
    if raw is not None:
        client.lrem(PROCESSING_KEY_TMPL % account, 1, raw)
    if raw_value:
        client.hdel(inflight_key, str(request_id))
    archive = {
        "request_id": str(request_id),
        "outcome": str(outcome),
        "note": str(note or ""),
        "resolved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "record": record,
        "request": req,
    }
    resolved_key = RESOLVED_KEY_TMPL % account
    client.hset(
        resolved_key,
        str(request_id),
        json.dumps(archive, ensure_ascii=False, default=str),
    )
    client.expire(resolved_key, 7 * 86400)
    print(json.dumps(archive, ensure_ascii=False, indent=2, default=str))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="QMT 伴生桥订单意图运维")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="列出 inflight / processing")
    resolve = sub.add_parser("resolve", help="人工核对后归档一条 unknown/inflight")
    resolve.add_argument("--request-id", required=True)
    resolve.add_argument(
        "--outcome",
        required=True,
        choices=["filled", "rejected", "cancelled", "unknown"],
    )
    resolve.add_argument("--note", default="")
    args = parser.parse_args(argv)

    cfg = load_config()
    client = connect(cfg)
    if args.cmd == "list":
        return list_intents(cfg, client)
    return resolve_intent(
        cfg, client, request_id=args.request_id, outcome=args.outcome, note=args.note
    )


if __name__ == "__main__":
    sys.exit(main())
