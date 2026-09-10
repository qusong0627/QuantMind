"""根据配置装配 SignalTradingApp。

当前第一版只提供安全的空信号 + dry-run 默认实现，后续再接 Redis/MySQL/大 QMT
真实 adapter。这样大 QMT 运行文件可以先加载和响应调度，不会误发真实委托。
"""

from .adapters.order_dryrun import DryRunOrderGateway
from .app import SignalTradingApp
from .models import AssetSnapshot


class EmptySignalSource:
    def fetch(self, account_id, limit):
        return []

    def ack(self, signal):
        return None


class EmptyMarketDataProvider:
    def get_ticks(self, codes):
        return {}

    def get_instrument(self, code):
        return {}


class EmptyPositionProvider:
    def get_positions(self, account_id):
        return {}

    def get_asset(self, account_id):
        return AssetSnapshot(account_id=account_id, cash=None, total_asset=None)


class NoopPositionSyncSink:
    def __init__(self):
        self.snapshots = []

    def publish(self, snapshot):
        self.snapshots.append(snapshot)


class NoopStateStore:
    def claim(self, signal, consumer_id):
        return False

    def mark_submitted(self, signal_id, result):
        return None

    def mark_finished(self, signal_id, status, message=""):
        return None


def _config_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on")


def build_app(context_info=None, config=None):
    config = config or {}
    mode = str(config.get("mode") or "dryrun").lower()
    account_id = config.get("account_id", "default")
    source_type = str(config.get("signal_source_type") or config.get("source_type") or "").lower()
    state_type = str(config.get("state_store_type") or "").lower()
    position_sync_type = str(config.get("position_sync_type") or "").lower()

    signal_source = config.get("signal_source")
    market_data = config.get("market_data")
    position_provider = config.get("position_provider")
    order_gateway = config.get("order_gateway")
    position_sync_sink = config.get("position_sync_sink")
    state_store = config.get("state_store")
    redis_client = config.get("redis_client")

    if source_type == "redis" or state_type == "redis" or position_sync_type == "redis":
        from .adapters.redis_common import build_redis_client

        redis_client = redis_client or build_redis_client(config.get("redis") or {})

    if source_type == "redis":
        from .adapters.signal_redis import RedisStreamSignalSource

        redis_cfg = config.get("redis") or {}
        signal_source = signal_source or RedisStreamSignalSource(
            redis_client=redis_client,
            stream_key_template=redis_cfg.get("stream_key_template", "bigqmt:signals:{account_id}"),
            group_name=redis_cfg.get("group_name", "bigqmt-signal-trader"),
            consumer_name=redis_cfg.get("consumer_name", "bigqmt-consumer"),
            block_ms=int(redis_cfg.get("block_ms", 0)),
        )

    if state_type == "redis" or (source_type == "redis" and state_store is None):
        from .adapters.state_redis import RedisStateStore

        redis_cfg = config.get("redis") or {}
        state_store = state_store or RedisStateStore(
            redis_client=redis_client,
            account_id=account_id,
            claim_key_template=redis_cfg.get("claim_key_template", "bigqmt:signal_claim:{account_id}:{signal_id}"),
            status_key_template=redis_cfg.get("status_key_template", "bigqmt:signal_status:{account_id}:{signal_id}"),
            claim_ttl_seconds=int(redis_cfg.get("claim_ttl_seconds", 3600)),
            status_ttl_seconds=int(redis_cfg.get("status_ttl_seconds", 86400)),
        )

    if position_sync_type == "redis":
        from .adapters.position_sync_redis import RedisPositionSyncSink

        redis_cfg = config.get("redis") or {}
        position_sync_sink = position_sync_sink or RedisPositionSyncSink(
            redis_client=redis_client,
            key_template=redis_cfg.get("position_key_template", "bigqmt:positions:{account_id}"),
            event_stream_template=redis_cfg.get("position_event_stream_template", "bigqmt:position_events:{account_id}"),
            ttl_seconds=int(redis_cfg.get("position_ttl_seconds", 120)),
            publish_events=_config_bool(redis_cfg.get("position_publish_events"), True),
        )

    if mode == "bigqmt":
        from .adapters.market_bigqmt import BigQmtMarketDataProvider
        from .adapters.order_bigqmt import BigQmtOrderGateway
        from .adapters.position_bigqmt import BigQmtPositionProvider

        qmt_api = config.get("qmt_api") or {}
        get_trade_detail_data_func = qmt_api.get("get_trade_detail_data")
        market_data = market_data or BigQmtMarketDataProvider(context_info, qmt_api=qmt_api)
        position_provider = position_provider or BigQmtPositionProvider(
            get_trade_detail_data_func=get_trade_detail_data_func,
            account_type=config.get("account_type", "STOCK"),
        )
        # passorder / cancel need the RAW QMT ContextInfo as their last arg -- QMT's
        # injected passorder reads internals off it (e.g. .request_id). Our runtime
        # wrapper (BigQmtRuntimeAdapter) doesn't have those, so unwrap it here.
        raw_context_info = getattr(context_info, "context_info", context_info)
        order_gateway = order_gateway or BigQmtOrderGateway(
            context_info=raw_context_info,
            account_id=account_id,
            passorder_func=qmt_api.get("passorder"),
            cancel_func=qmt_api.get("cancel"),
            get_trade_detail_data_func=get_trade_detail_data_func,
            account_type=config.get("account_type", "STOCK"),
            combo_type=int(config.get("combo_type", 1101)),
            price_type=int(config.get("order_price_type", 11)),
            quick_trade=int(config.get("quick_trade", 2)),
        )

    return SignalTradingApp(
        account_id=account_id,
        signal_source=signal_source or EmptySignalSource(),
        market_data=market_data or EmptyMarketDataProvider(),
        position_provider=position_provider or EmptyPositionProvider(),
        order_gateway=order_gateway or DryRunOrderGateway(),
        position_sync_sink=position_sync_sink or NoopPositionSyncSink(),
        state_store=state_store or NoopStateStore(),
        consumer_id=config.get("consumer_id", "bigqmt-signal-trader"),
        fetch_limit=config.get("fetch_limit", 20),
    )
