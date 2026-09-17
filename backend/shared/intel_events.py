"""盘中情报总线契约（T-P6-11）：事件 schema 校验 + 生产者/消费者 SDK（唯一投递点）。

**事件 schema（设计 §6.3，评审红线——字段/枚举变更须同步前端与消费方）**：
    {ts: float(epoch 秒), type: news|regime|anomaly, market: CN|HK|US|CRYPTO|FUTURES,
     targets: [str ≤64], level: info|warn|critical, payload: dict(≤8KB JSON),
     actions_hint: [str ≤8], source: str(生产者标识, ≤64)}

**总线形态**：Redis Stream ``intel:events``（XADD MAXLEN≈见常量；消费者组 ``intel:ws``
由 WS 侧 IntelPusher 消费）。SDK 分两层：
- 纯函数（``validate_event``/``encode_event``/``topic_for_event``/``authorize_intel_topic``）——
  无 IO 可单测；
- IO 便捷层（``publish_event``/``read_events``/``ensure_group``/``ack_event``）——client 注入。

**Topic 约定**：``intel.{tenant}.market.{MARKET}``（市场级广播）与
``intel.{tenant}.user.{user_id}``（用户级定向）；越权订阅拒绝（见 ws_core.server）。
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

STREAM_KEY = "intel:events"
CONSUMER_GROUP = "intel:ws"
STREAM_MAXLEN = 20000

EVENT_TYPES = ("news", "regime", "anomaly")
LEVELS = ("info", "warn", "critical")
MARKETS = ("CN", "HK", "US", "CRYPTO", "FUTURES")
DEFAULT_MARKET = "CN"
DEFAULT_LEVEL = "info"

MAX_TARGETS = 64
MAX_TARGET_LEN = 24
MAX_ACTIONS = 8
MAX_ACTION_LEN = 48
MAX_SOURCE_LEN = 64
MAX_PAYLOAD_BYTES = 8192
_REQUIRED_KEYS = {"ts", "type", "market", "targets", "level", "payload", "actions_hint", "source"}


class IntelEventError(ValueError):
    """schema 校验失败（畸形事件一律拒绝，绝不静默降级）。"""


def validate_event(event: Any) -> dict[str, Any]:
    """规范化+校验事件；返回冻结 schema 的新 dict。畸形 → IntelEventError。"""
    if not isinstance(event, dict):
        raise IntelEventError(f"事件必须是 dict，收到 {type(event).__name__}")
    unknown = set(event) - _REQUIRED_KEYS
    if unknown:
        raise IntelEventError(f"未知字段: {sorted(unknown)}（schema 拒收扩展字段，payload 是扩展点）")

    etype = str(event.get("type") or "").strip().lower()
    if etype not in EVENT_TYPES:
        raise IntelEventError(f"type 非法: {etype!r}（允许 {EVENT_TYPES}）")

    try:
        ts = float(event.get("ts") if event.get("ts") is not None else time.time())
    except (TypeError, ValueError):
        raise IntelEventError(f"ts 非法: {event.get('ts')!r}") from None
    if not (ts > 0):
        raise IntelEventError(f"ts 必须为正 epoch 秒: {ts}")

    market = str(event.get("market") or DEFAULT_MARKET).strip().upper()
    if market not in MARKETS:
        raise IntelEventError(f"market 非法: {market!r}（允许 {MARKETS}）")

    level = str(event.get("level") or DEFAULT_LEVEL).strip().lower()
    if level not in LEVELS:
        raise IntelEventError(f"level 非法: {level!r}（允许 {LEVELS}）")

    raw_targets = event.get("targets") or []
    if not isinstance(raw_targets, (list, tuple)):
        raise IntelEventError("targets 必须是数组")
    targets = []
    for item in raw_targets:
        text = str(item or "").strip()
        if not text:
            continue
        if len(text) > MAX_TARGET_LEN:
            raise IntelEventError(f"target 超长: {text[:32]}…")
        targets.append(text)
    if len(targets) > MAX_TARGETS:
        raise IntelEventError(f"targets 数量超限: {len(targets)} > {MAX_TARGETS}")

    raw_actions = event.get("actions_hint") or []
    if not isinstance(raw_actions, (list, tuple)):
        raise IntelEventError("actions_hint 必须是数组")
    actions = []
    for item in raw_actions:
        text = str(item or "").strip()
        if not text:
            continue
        if len(text) > MAX_ACTION_LEN:
            raise IntelEventError(f"action 超长: {text[:48]}…")
        actions.append(text)
    if len(actions) > MAX_ACTIONS:
        raise IntelEventError(f"actions_hint 数量超限: {len(actions)} > {MAX_ACTIONS}")

    payload = event.get("payload") or {}
    if not isinstance(payload, dict):
        raise IntelEventError("payload 必须是 dict")
    try:
        encoded = json.dumps(payload, ensure_ascii=False, default=str)
    except Exception as exc:  # noqa: BLE001
        raise IntelEventError(f"payload 不可序列化: {exc}") from None
    if len(encoded.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise IntelEventError(f"payload 超限: >{MAX_PAYLOAD_BYTES}B")

    source = str(event.get("source") or "unknown").strip()[:MAX_SOURCE_LEN] or "unknown"
    return {
        "ts": ts, "type": etype, "market": market, "targets": targets,
        "level": level, "payload": payload, "actions_hint": actions, "source": source,
    }


def build_event(
    *,
    type: str,
    market: str = DEFAULT_MARKET,
    targets: Any = (),
    level: str = DEFAULT_LEVEL,
    payload: dict[str, Any] | None = None,
    actions_hint: Any = (),
    source: str = "unknown",
    ts: float | None = None,
) -> dict[str, Any]:
    """构造并校验事件（生产者统一入口）。"""
    return validate_event(
        {
            "ts": ts if ts is not None else time.time(),
            "type": type, "market": market, "targets": list(targets),
            "level": level, "payload": payload or {}, "actions_hint": list(actions_hint),
            "source": source,
        }
    )


def encode_event(event: dict[str, Any]) -> str:
    return json.dumps(validate_event(event), ensure_ascii=False, separators=(",", ":"))


def decode_event(raw: Any) -> dict[str, Any]:
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
    if not isinstance(raw, str):
        raise IntelEventError(f"原始事件必须是 str/bytes: {type(raw).__name__}")
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise IntelEventError(f"JSON 解析失败: {exc}") from None
    return validate_event(parsed)


def topic_for_event(event: dict[str, Any], *, tenant: str = "default") -> str:
    """事件 → 广播 topic（市场级；用户级定向由 payload.user_ids 扩展——v1 不做）。"""
    etype = str(event.get("market") or DEFAULT_MARKET).upper()
    return f"intel.{tenant}.market.{etype}"


def authorize_intel_topic(metadata: dict[str, Any], topic: str) -> bool:
    """intel topic 订阅鉴权（纯函数）：tenant 必须匹配；user 段必须本人；market 段任意已登录。"""
    parts = str(topic or "").split(".")
    if len(parts) != 4 or parts[0] != "intel":
        return False
    tenant, scope, subject = parts[1], parts[2], parts[3]
    if not metadata.get("authenticated"):
        return False
    if tenant != str(metadata.get("tenant_id") or "default"):
        return False
    if scope == "user":
        return subject == str(metadata.get("user_id") or "")
    if scope == "market":
        return subject.upper() in MARKETS
    return False


# ── IO 层（client 注入；Redis Stream 语义：投递即落总线，消费组保证至少一次）──


def publish_event(client: Any, event: dict[str, Any], *, key: str = STREAM_KEY, maxlen: int = STREAM_MAXLEN) -> str:
    """投递事件（唯一写入面）；返回 stream id。畸形事件在 encode 阶段即抛。"""
    fields = {"event_id": uuid.uuid4().hex, "data": encode_event(event)}
    return str(client.xadd(key, fields, maxlen=maxlen, approximate=True))


def ensure_group(
    client: Any, *, key: str = STREAM_KEY, group: str = CONSUMER_GROUP, start_id: str = "0"
) -> None:
    """建消费组（已存在忽略；mkstream 使总线不存在也可先建组）。

    start_id="0"（默认，兼容既有 intel:ws 订阅）从已有事件开始；新消费者若只关心
    增量（如哨兵留痕，避免回放历史事件风暴）传 start_id="$"。
    """
    try:
        client.xgroup_create(key, group, id=start_id, mkstream=True)
    except Exception as exc:  # noqa: BLE001 - BUSYGROUP 属正常
        if "BUSYGROUP" not in str(exc):
            raise


def read_events(
    client: Any,
    *,
    key: str = STREAM_KEY,
    group: str = CONSUMER_GROUP,
    consumer: str = "c1",
    count: int = 200,
    block_ms: int | None = 2000,
) -> list[tuple[str, dict[str, Any]]]:
    """读新事件（'>'）；畸形事件跳过并计数由调用方处理。返回 [(id, event)]。"""
    raw = client.xreadgroup(
        group, consumer, {key: ">"}, count=int(count),
        block=block_ms if block_ms is not None else None,
    ) or []
    out: list[tuple[str, dict[str, Any]]] = []
    for _stream, messages in raw:
        for msg_id, fields in messages:
            try:
                event = decode_event(fields.get("data") or "{}")
            except IntelEventError:
                out.append((str(msg_id), {"_malformed": str(fields.get("data"))[:200]}))
                continue
            out.append((str(msg_id), event))
    return out


def ack_event(client: Any, msg_id: str, *, key: str = STREAM_KEY, group: str = CONSUMER_GROUP) -> int:
    return int(client.xack(key, group, msg_id))
