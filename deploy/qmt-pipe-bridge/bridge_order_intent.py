"""订单意图 / 幂等状态机（纯函数，可单测）。

为什么单独一层
--------------
伴生桥的订单路径有三处"看不见"的窗口：``BLPOP`` 之后进程崩、管道超时但单
可能已在柜台、HTTP 与 Redis 两条写入口缓存格式不一致。这里把"这笔单是什么、
是否已发过、结果是否已知"做成纯状态机，I/O 层只负责持久化与转发。

约定
----
* 幂等键优先取业务侧 remark / client_order_id；同键不同载荷 = 显式冲突。
* ``inflight`` / ``unknown`` 都不允许重发；前者可能还在路上，后者可能已成交。
* 旧版本 Redis 里存的是裸响应（没有 ``state``），升级后按 ``done`` 兼容。
"""

from __future__ import annotations

import hashlib
import json

import bridge_gates

#: 幂等记录状态
STATE_INFLIGHT = "inflight"
STATE_DONE = "done"
STATE_REJECTED = "rejected"
STATE_UNKNOWN = "unknown"

#: 判定结果
DECISION_NEW = "new"
DECISION_REPLAY = "replay"
DECISION_CONFLICT = "conflict"
DECISION_IN_PROGRESS = "in_progress"
DECISION_UNKNOWN = "unknown"

#: 业务幂等键字段（按优先级）。``order_id`` 是历史 HTTP 路由的位置；
#: ``client_order_id`` / ``signal_id`` 覆盖 QuantMind 两条下单链路。
IDEMPOTENCY_KEY_FIELDS = (
    "remark",
    "order_remark",
    "order_id",
    "order_sysid",
    "user_order_id",
    "client_order_id",
    "signal_id",
    "batch_id",
)

#: 请求级路由字段不参与订单载荷指纹：重试会换 request_id / reply key。
_FINGERPRINT_EXCLUDED = frozenset(
    {
        "request_id",
        "id",
        "reply_key",
        "reply_list",
        "reply_channel",
        "ttl_seconds",
        # 只影响是否等待成交号，不改变订单本身；重试可能切换。
        "wait_settlement",
    }
)


def canonical_method(method) -> str:
    """线名 → canonical 名（与策略侧 METHOD_ALIASES 同源，见 bridge_gates）。"""
    return bridge_gates.canonical_method(method)


def is_order_method(method) -> bool:
    return bridge_gates.is_order_like(method)


def order_fingerprint(method, params) -> str:
    """订单载荷指纹：canonical 方法 + 排序后的参数，排除请求级路由字段。"""
    clean = {}
    for key, value in dict(params or {}).items():
        if key in _FINGERPRINT_EXCLUDED:
            continue
        clean[str(key)] = value
    material = json.dumps(
        {"method": canonical_method(method), "params": clean},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def idempotency_key(account_id, method, params):
    """业务幂等键；非订单方法或没有业务键时返回 ``None``。"""
    if not is_order_method(method):
        return None
    for field in IDEMPOTENCY_KEY_FIELDS:
        value = (params or {}).get(field)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return "qm:qmt:pipe:idem:%s:%s" % (account_id, text)
    return None


def make_intent_record(
    *,
    account_id,
    method,
    params,
    request_id,
    fingerprint=None,
    now,
    raw="",
):
    """新建一条 inflight 记录（调用方负责 SET NX 持久化）。"""
    fp = fingerprint or order_fingerprint(method, params)
    return {
        "state": STATE_INFLIGHT,
        "account_id": str(account_id or ""),
        "method": str(method or ""),
        "canonical_method": canonical_method(method),
        "request_id": str(request_id or ""),
        "fingerprint": fp,
        "created_at": float(now),
        "started_at": float(now),
        "updated_at": float(now),
        "raw": str(raw or ""),
        "error": "",
        "response": None,
    }


def mark_intent(record, state, *, now=0.0, response=None, error="") -> dict:
    """返回更新后的记录（不原地修改，便于测试与回滚）。"""
    updated = dict(record or {})
    updated["state"] = str(state)
    updated["updated_at"] = float(now or 0.0)
    if response is not None:
        updated["response"] = response
    if error:
        updated["error"] = str(error)
    return updated


def _looks_like_legacy_response(record) -> bool:
    """旧版本缓存的是裸响应：没有 state，但有 ok/data/error 之一。"""
    if not isinstance(record, dict):
        return False
    if "state" in record:
        return False
    return any(key in record for key in ("ok", "data", "error", "request_id"))


def classify_intent(record, fingerprint) -> str:
    """已有记录 + 本次指纹 → 该走哪条路。"""
    if record is None:
        return DECISION_NEW
    if not isinstance(record, dict):
        # 有值但解不开：绝不能当成"没发过"再发一笔。
        return DECISION_UNKNOWN
    if _looks_like_legacy_response(record):
        return DECISION_REPLAY
    stored = str(record.get("fingerprint") or "")
    if stored and str(fingerprint or "") and stored != str(fingerprint):
        return DECISION_CONFLICT
    state = str(record.get("state") or STATE_DONE)
    if state in (STATE_DONE, STATE_REJECTED):
        return DECISION_REPLAY
    if state == STATE_UNKNOWN:
        return DECISION_UNKNOWN
    if state == STATE_INFLIGHT:
        return DECISION_IN_PROGRESS
    # 未知状态按 in_progress 处理：宁可让调用方等/人工核，也不重发。
    return DECISION_IN_PROGRESS


def has_unresolved(records) -> bool:
    """是否有在途/结果未知的订单意图。"""
    for record in records or ():
        if not isinstance(record, dict):
            continue
        if record.get("state") in (STATE_INFLIGHT, STATE_UNKNOWN):
            return True
    return False
