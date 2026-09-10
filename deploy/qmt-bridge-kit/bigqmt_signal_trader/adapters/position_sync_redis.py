"""Publish Big QMT position snapshots to Redis."""

import datetime as _dt
import json


class RedisPositionSyncSink:
    def __init__(
        self,
        redis_client,
        key_template="bigqmt:positions:{account_id}",
        event_stream_template="bigqmt:position_events:{account_id}",
        ttl_seconds=120,
        publish_events=True,
        stream_ttl_seconds=None,
    ):
        self.redis = redis_client
        self.key_template = key_template
        self.event_stream_template = event_stream_template
        self.ttl_seconds = int(ttl_seconds)
        self.publish_events = bool(publish_events)
        from .redis_common import EVENT_STREAM_TTL_SECONDS

        self.stream_ttl_seconds = int(
            EVENT_STREAM_TTL_SECONDS if stream_ttl_seconds is None else stream_ttl_seconds)
        # 上一次写出去的**实质内容**（不含 updated_at）。相同就不再写。
        self._last_material = None

    @staticmethod
    def _time_text(value):
        if isinstance(value, _dt.datetime):
            return value.strftime("%Y-%m-%d %H:%M:%S")
        return str(value)

    def _snapshot_to_dict(self, snapshot):
        return {
            "account_id": snapshot.account_id,
            "reason": snapshot.reason,
            "updated_at": self._time_text(snapshot.updated_at),
            "asset": {
                "cash": snapshot.asset.cash,
                "total_asset": snapshot.asset.total_asset,
                # Carried so the client's cached-asset fallback exposes the same
                # fields as a live query_stock_asset.
                "frozen_cash": getattr(snapshot.asset, "frozen_cash", None),
                "market_value": getattr(snapshot.asset, "market_value", None),
            },
            "positions": {
                code: {
                    "stock_code": position.stock_code,
                    "volume": position.volume,
                    "available": position.available,
                    "cost": position.cost,
                    "stock_name": position.stock_name,
                }
                for code, position in snapshot.positions.items()
            },
        }

    def publish(self, snapshot):
        # 账号 ID 为空时不能写：key_template.format(account_id="") 会拼出
        # "bigqmt:position_events:" 这种畸形键，而且它同样永不过期。线上真的
        # 扫出过一个（9 条记录）。静默写进畸形键比不写更难查（#213）。
        account_id = str(getattr(snapshot, "account_id", "") or "").strip()
        if not account_id:
            return None

        payload = json.dumps(self._snapshot_to_dict(snapshot), ensure_ascii=False)

        # 快照没变就不写。原来是无条件 SETEX + XADD，而 publish() 每个 adjust
        # tick 被调一次 —— 实测 10 条/秒，休市、持仓一动没动也照写，每天 86 万
        # 条。更糟的是 maxlen=2000 把可用历史压到了 200 秒：想回放持仓变动，
        # 三分半钟以前的已经被空转刷掉了（#213）。
        #
        # 注意 string 那份也要跟着跳过：它带 120 秒 TTL，不续期会过期。这是
        # 想要的 —— 快照没变时它本来就该由下一次真实变动来重建；但为了不让
        # 「持仓查询」在静默期落空，下面仍然给它续期。
        # 只比**实质内容**，不比时间戳。第一版拿整个 payload 做比较，结果
        # updated_at 是秒级的、每秒变一次，于是「没变」被伪装成「变了」——
        # 写入只从 10 条/秒降到 1 条/秒，而实测相邻两条之间唯一不同的字段
        # 就是它（account_id / reason / asset / positions 全部相同）。
        material = json.dumps(
            {k: v for k, v in self._snapshot_to_dict(snapshot).items()
             if k != "updated_at"},
            ensure_ascii=False, sort_keys=True)
        unchanged = material == self._last_material
        key = self.key_template.format(account_id=account_id)
        if unchanged:
            if self.ttl_seconds > 0:
                try:
                    self.redis.expire(key, self.ttl_seconds)
                except Exception:
                    pass
            return None
        self._last_material = material

        if self.ttl_seconds > 0:
            self.redis.setex(key, self.ttl_seconds, payload)
        else:
            self.redis.set(key, payload)
        if self.publish_events:
            from .redis_common import (
                note_stream_failure, streams_dead, touch_stream_ttl,
            )

            stream_key = self.event_stream_template.format(account_id=account_id)
            # Cap the stream to prevent unbounded memory growth. Order/trade
            # events already use maxlen=2000; position events were missing it,
            # causing 4.2GB+ streams in production (issue #21).
            # redis < 5.0 has no streams at all: learn it once and stop
            # raising every tick (issue #163).
            if not streams_dead():
                try:
                    self.redis.xadd(stream_key, {"payload": payload}, maxlen=2000, approximate=True)
                    # maxlen 挡的是单键膨胀，挡不住键永远不消失。续期一次，
                    # 停写的流自然过期（#213）。
                    touch_stream_ttl(self.redis, stream_key, self.stream_ttl_seconds)
                except Exception as exc:
                    note_stream_failure(exc)
