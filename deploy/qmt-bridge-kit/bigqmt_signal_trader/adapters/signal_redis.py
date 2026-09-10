"""Redis Stream signal source.

Redis is only a transport here. It must never call passorder or inspect QMT.
"""

import datetime as _dt
import json

from ..models import TradeSignal
from .redis_common import decode_text, redis_mapping_to_text


DEFAULT_STREAM_KEY_TEMPLATE = "bigqmt:signals:{account_id}"
DEFAULT_GROUP = "bigqmt-signal-trader"


def _json_default(value):
    if isinstance(value, (_dt.datetime, _dt.date)):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return str(value)


def _coerce_scalar(value):
    text = decode_text(value).strip()
    lowered = text.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered in ("none", "null"):
        return None
    return text


def parse_stream_payload(fields):
    text_fields = redis_mapping_to_text(fields)
    payload_text = text_fields.get("payload") or text_fields.get("data")
    if payload_text:
        payload = json.loads(payload_text)
        if not isinstance(payload, dict):
            raise ValueError("Redis stream payload must be a JSON object")
        return payload
    return {decode_text(key): _coerce_scalar(value) for key, value in fields.items()}


class RedisStreamSignalSource:
    def __init__(
        self,
        redis_client,
        stream_key_template=DEFAULT_STREAM_KEY_TEMPLATE,
        group_name=DEFAULT_GROUP,
        consumer_name="bigqmt-consumer",
        block_ms=0,
    ):
        self.redis = redis_client
        self.stream_key_template = stream_key_template
        self.group_name = group_name
        self.consumer_name = consumer_name
        self.block_ms = int(block_ms or 0)
        self._stream_ids_by_signal_id = {}
        self._created_groups = set()

    def _stream_key(self, account_id):
        return self.stream_key_template.format(account_id=account_id)

    def _ensure_group(self, stream_key):
        if stream_key in self._created_groups:
            return
        try:
            self.redis.xgroup_create(stream_key, self.group_name, id="0-0", mkstream=True)
        except Exception as exc:
            if "BUSYGROUP" not in str(exc):
                raise
        self._created_groups.add(stream_key)

    def fetch(self, account_id, limit):
        stream_key = self._stream_key(account_id)
        self._ensure_group(stream_key)
        kwargs = {
            "groupname": self.group_name,
            "consumername": self.consumer_name,
            "streams": {stream_key: ">"},
            "count": int(limit),
        }
        if self.block_ms > 0:
            kwargs["block"] = self.block_ms
        rows = self.redis.xreadgroup(**kwargs) or []
        signals = []
        for _, entries in rows:
            for stream_id, fields in entries:
                payload = parse_stream_payload(fields)
                signal = TradeSignal.from_dict(payload)
                self._stream_ids_by_signal_id[signal.signal_id] = (stream_key, stream_id)
                signals.append(signal)
        return signals

    def ack(self, signal):
        ref = self._stream_ids_by_signal_id.pop(signal.signal_id, None)
        if not ref:
            return None
        stream_key, stream_id = ref
        return self.redis.xack(stream_key, self.group_name, stream_id)


def push_trade_signal(redis_client, payload, account_id=None, stream_key_template=DEFAULT_STREAM_KEY_TEMPLATE):
    if isinstance(payload, TradeSignal):
        account_id = account_id or payload.account_id
        raw_payload = dict(payload.raw_payload)
    else:
        raw_payload = dict(payload)
        account_id = account_id or raw_payload.get("account_id")
    if not account_id:
        raise ValueError("account_id is required")
    stream_key = stream_key_template.format(account_id=account_id)
    entry_id = redis_client.xadd(
        stream_key,
        {"payload": json.dumps(raw_payload, ensure_ascii=False, default=_json_default)},
    )
    # 信号流同样是永久键。续期一次，停用的账号自然消失（#213）。
    from .redis_common import touch_stream_ttl

    touch_stream_ttl(redis_client, stream_key)
    return entry_id
