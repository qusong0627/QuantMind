"""Reference-counted quote subscription manager (server side).

One big-QMT subscription group is shared by every client that asked for the same
(normalized) code combination. Normal instruments use
``ContextInfo.subscribe_whole_quote``. Explicit .SHO/.SZO option contracts use
``ContextInfo.subscribe_quote(result_type="list")`` because some full Big-QMT
versions do not push those contracts through the whole-quote API. The group is
only created for the first client and only torn down after the last client either
unsubscribes or goes silent (keepalive timeout).

The manager talks to big QMT exclusively through a :class:`QuoteSourceAdapter`;
it never touches ``ContextInfo`` directly so the real-environment wiring (method
names / handle shape) stays isolated to the adapter.

Threading: ``subscribe``/``unsubscribe``/``keepalive`` run on the RPC thread,
``reap_expired`` on the scheduler thread and ``on_push`` on big QMT's quote
thread. Shared state is guarded by one re-entrant lock; calls out to the quote
source and to the push publisher happen OUTSIDE the lock so a slow/blocking
publish never stalls quote-thread state, and no callback can deadlock.
"""

import threading

from .code_utils import normalize_stock_code


def normalize_subscription_code(code):
    """One code as big QMT wants it -- and futures keep their case (#95).

    This used to be a plain ``.upper()``, which is right for the exchange
    tokens (SH / SZ / IF ...) and wrong for every futures contract, because
    those symbols are lowercase: ``cu2610.SF`` went to QMT as ``CU2610.SF``,
    a contract it does not have, so the subscription produced no pushes at
    all. The reporter saw exactly that -- ``CF701.ZF`` ticking every 250ms
    while ``cu2610.SF`` delivered one frame and then nothing. The one frame
    was the initial snapshot, which takes the case-preserving get_full_tick
    path; the subscription behind it was already dead.

    normalize_stock_code keeps the caller's symbol verbatim for the
    case-sensitive suffixes (.SF .DF .IF .ZF .INE .GF -- issue #58) but
    rejects a bare exchange token, so tokens are handled here instead.
    """
    text = str(code or "").strip()
    if not text:
        return ""
    if "." not in text:
        return text.upper()          # exchange token: SH / SZ / BJ / IF / SF ...
    try:
        return normalize_stock_code(text)
    except Exception:
        return text.upper()
from .quote_utils import is_option_code, latest_quote_batch


def combo_key(code_list):
    """Normalize a code list into an order-independent combination key.

    Strips whitespace, drops empties and duplicates, sorts. Exchange tokens are
    uppercased, so ``["SH","SZ"]``, ``["sz","sh"]`` and ``["SH","SH","SZ"]``
    all map to ``"SH,SZ"`` and share one big-QMT subscription.

    Contract codes keep their case, because big QMT does: ``cu2610.SF`` and
    ``CU2610.SF`` are not the same subscription there -- only one of them
    exists -- so collapsing them into one key would hand the wrong string to
    the exchange (#95).
    """
    normalized = {normalize_subscription_code(code) for code in (code_list or [])
                  if str(code or "").strip()}
    return ",".join(sorted(normalized - {""}))


class QuoteSourceAdapter(object):
    """Big-QMT whole-quote source. ContextInfo-backed implementation lives in the
    server runtime; tests substitute a fake. ``subscribe`` must return a handle
    usable by ``unsubscribe``."""

    def subscribe(self, codes, on_push):
        raise NotImplementedError

    def unsubscribe(self, handle):
        raise NotImplementedError


class ContextInfoQuoteSource(QuoteSourceAdapter):
    """Real big-QMT source backed by the strategy's ``ContextInfo``.

    Verified against the real environment: ``ContextInfo.subscribe_whole_quote``
    returns an int subscription id and pushes ``{code: tick}`` batches. Explicit
    option contracts are subscribed one-by-one with ``subscribe_quote`` and the
    safe ``result_type='list'`` wrapper; its column arrays are collapsed to the
    newest tick before publishing. ``unsubscribe_quote`` cancels either handle.
    """

    def __init__(self, context_info):
        self._context = context_info

    def subscribe(self, codes, on_push):
        codes = list(codes)
        option_codes = [code for code in codes if is_option_code(code)]
        whole_codes = [code for code in codes if not is_option_code(code)]
        handles = []

        def checked_handle(sub_id, method, method_codes):
            try:
                value = int(sub_id)
            except (TypeError, ValueError):
                value = -1
            if value <= 0:
                raise RuntimeError(
                    "ContextInfo.%s failed for codes=%s" % (method, list(method_codes))
                )
            return value

        try:
            if whole_codes:
                sub_id = self._context.subscribe_whole_quote(
                    whole_codes, callback=on_push
                )
                handles.append(checked_handle(
                    sub_id, "subscribe_whole_quote", whole_codes
                ))

            def on_option_push(data):
                batch = latest_quote_batch(data)
                if batch:
                    on_push(batch)

            for code in option_codes:
                sub_id = self._context.subscribe_quote(
                    code, "tick", "none", "list", on_option_push
                )
                handles.append(checked_handle(
                    sub_id, "subscribe_quote", [code]
                ))
        except Exception:
            for handle in handles:
                try:
                    self._context.unsubscribe_quote(handle)
                except Exception:
                    pass
            raise

        if not handles:
            raise RuntimeError("no quote codes supplied")
        return handles[0] if len(handles) == 1 else tuple(handles)

    def unsubscribe(self, handle):
        handles = handle if isinstance(handle, (list, tuple, set)) else [handle]
        for sub_id in handles:
            try:
                self._context.unsubscribe_quote(sub_id)
            except Exception:
                pass


class _Combo(object):
    __slots__ = ("key", "codes", "handle", "topic", "clients")

    def __init__(self, key, codes, handle, topic):
        self.key = key
        self.codes = codes
        self.handle = handle
        self.topic = topic
        # (client_id, sub_id) -> last_seen. Sub-id granularity: one client may
        # hold several subscriptions to the same combination, and each one keeps
        # the shared big-QMT subscription alive independently.
        self.clients = {}  # (client_id, sub_id) -> last_seen timestamp


class QuoteSubscriptionManager(object):
    def __init__(self, source, heartbeat_timeout_seconds=30.0, time_func=None, on_push_publisher=None, push_endpoint=""):
        self._source = source
        self._heartbeat_timeout = float(heartbeat_timeout_seconds)
        self._now = time_func or _monotonic
        # Optional callable(topic, data) invoked when big QMT pushes a tick batch.
        # Wired to the QuotePushChannel in a later stage; None keeps dispatch inert.
        self._on_push_publisher = on_push_publisher
        # Advertised to clients in subscribe responses so a zmq subscriber knows
        # where to connect (redis subscribers derive the channel locally instead).
        self._push_endpoint = str(push_endpoint or "")
        self._lock = threading.RLock()
        self._combos = {}            # combo_key -> _Combo
        self._sub_index = {}         # (client_id, sub_id) -> combo_key

    # -- subscription lifecycle ---------------------------------------------
    def subscribe(self, client_id, sub_id, code_list):
        """Register (client_id, sub_id) against its combination; create the shared
        big-QMT subscription on first use. Idempotent for replayed subscribes."""
        client_id = str(client_id or "")
        sub_id = str(sub_id or "")
        key = combo_key(code_list)
        now = self._now()

        with self._lock:
            combo = self._combos.get(key)
            if combo is None:
                codes = sorted({normalize_subscription_code(c) for c in (code_list or [])
                                if str(c or "").strip()} - {""})
                # source.subscribe registers the on_push callback with big QMT; it
                # does not call back into the manager, so it is safe under the lock.
                handle = self._source.subscribe(codes, self._make_on_push(key))
                combo = _Combo(key, codes, handle, key)
                self._combos[key] = combo

            combo.clients[(client_id, sub_id)] = now
            self._sub_index[(client_id, sub_id)] = key
            return {"combo_key": key, "topic": combo.topic, "push_endpoint": self._push_endpoint}

    def unsubscribe(self, client_id, sub_id):
        """Drop (client_id, sub_id); tear the big-QMT subscription down when the
        last subscription of the combination leaves. Unknown sub_ids are a no-op."""
        client_id = str(client_id or "")
        sub_id = str(sub_id or "")
        with self._lock:
            key = self._sub_index.pop((client_id, sub_id), None)
            if key is None:
                return
            handle_to_close = self._remove_subscription_locked(key, client_id, sub_id)
        self._close_source(handle_to_close)

    def keepalive(self, client_id, sub_id):
        """Refresh last_seen for (client_id, sub_id). Unknown sub_ids are a no-op."""
        client_id = str(client_id or "")
        key = self._sub_index.get((client_id, str(sub_id or "")))
        if key is None:
            return
        with self._lock:
            combo = self._combos.get(key)
            if combo is None:
                return
            combo.clients[(client_id, str(sub_id or ""))] = self._now()

    # -- reaper ---------------------------------------------------------------
    def reap_expired(self, now=None):
        """Remove subscriptions silent for longer than the keepalive timeout;
        tear down combos that end up empty. Returns the number reaped."""
        now = self._now() if now is None else now
        reaped = 0
        handles_to_close = []
        with self._lock:
            for key in list(self._combos.keys()):
                combo = self._combos.get(key)
                if combo is None:
                    continue
                for (client_id, sub_id), last_seen in list(combo.clients.items()):
                    if now - last_seen > self._heartbeat_timeout:
                        self._sub_index.pop((client_id, sub_id), None)
                        handle = self._remove_subscription_locked(key, client_id, sub_id)
                        if handle is not None:
                            handles_to_close.append(handle)
                        reaped += 1
        for handle in handles_to_close:
            self._close_source(handle)
        return reaped

    # -- diagnostics / force-clear ---------------------------------------------
    def status(self):
        """Read-only snapshot of what is subscribed and how fresh it is.

        The report is the diagnostic for "I lost my seq and something is still
        pushing": which combos exist, how many clients each thinks it has, and
        how long since the freshest keepalive. A combo whose silence exceeds
        the heartbeat timeout but has not been reaped yet is about to die;
        one that stays fresh has a live keepalive feeding it (a leftover
        client process, not a leak).
        """
        with self._lock:
            now = self._now()
            combos = []
            for combo in self._combos.values():
                latest = max(combo.clients.values()) if combo.clients else None
                combos.append({
                    "topic": combo.topic,
                    "codes": list(combo.codes),
                    "clients": len(combo.clients),
                    "last_seen_seconds_ago": round(now - latest, 1) if latest is not None else None,
                })
            return {"heartbeat_timeout_seconds": self._heartbeat_timeout, "combos": combos}

    def unsubscribe_all(self):
        """Tear every combo down NOW, without needing any seq.

        The operator kill switch for a subscription whose seq is lost. Every
        source handle is closed, so big QMT stops pushing even for a combo
        whose clients are still sending keepalives -- they will recreate the
        combo on their next keepalive... no, keepalive is a no-op on unknown
        sub_ids, so a force-cleared combo stays down until someone subscribes
        again. Returns how many combos were closed.
        """
        with self._lock:
            combos = list(self._combos.values())
            count = len(combos)
            self._combos = {}
            self._sub_index = {}
        for combo in combos:
            self._close_source(combo.handle)
        return count

    # -- internals -------------------------------------------------------------
    def _make_on_push(self, key):
        def on_push(data):
            publisher = self._on_push_publisher
            if publisher is None:
                return
            with self._lock:
                combo = self._combos.get(key)
                topic = combo.topic if combo is not None else None
            if topic is None:
                return
            # Publish outside the lock: it is network IO and must not stall the
            # quote thread or block reaper/RPC threads waiting on the lock.
            publisher(topic, data)

        return on_push

    def _remove_subscription_locked(self, key, client_id, sub_id):
        """Remove one (client_id, sub_id) from a combo. If the combo has no
        subscriptions left, detach it and return its source handle for the
        caller to close OUTSIDE the lock; else return None. Caller must hold
        the lock."""
        combo = self._combos.get(key)
        if combo is None:
            return None
        combo.clients.pop((client_id, sub_id), None)
        if combo.clients:
            return None
        self._combos.pop(key, None)
        return combo.handle

    def _close_source(self, handle):
        if handle is None:
            return
        try:
            self._source.unsubscribe(handle)
        except Exception:
            pass


def _monotonic():
    import time

    return time.monotonic()


def build_quote_subscription_service(
    context_info,
    transport_name="redis",
    account_id="",
    redis_client=None,
    zmq_bind_address=None,
    enabled=True,
    heartbeat_timeout_seconds=30.0,
    time_func=None,
):
    """Assemble the server-side whole-quote service: a ContextInfo-backed source,
    a push channel matching the RPC transport, and a QuoteSubscriptionManager
    wired so big-QMT pushes publish to the channel. Returns ``(manager, channel)``
    or ``None`` when disabled. The caller starts the channel publisher and feeds
    ``manager.reap_expired`` from the scheduler loop."""
    if not enabled:
        return None
    from .quote_push_channel import RedisQuotePushChannel, ZmqQuotePushChannel

    source = ContextInfoQuoteSource(context_info)
    transport_name = str(transport_name or "redis").lower()
    if transport_name == "zmq":
        bind_address = zmq_bind_address or _default_quote_push_zmq_bind(account_id)
        channel = ZmqQuotePushChannel(bind_address=bind_address)
        push_endpoint = bind_address
    else:
        channel = RedisQuotePushChannel(redis_client, account_id=account_id)
        push_endpoint = ""
    manager = QuoteSubscriptionManager(
        source,
        heartbeat_timeout_seconds=heartbeat_timeout_seconds,
        time_func=time_func,
        on_push_publisher=channel.publish,
        push_endpoint=push_endpoint,
    )
    return manager, channel


def _default_quote_push_zmq_bind(account_id):
    """Default server PUB bind address: loopback, RPC zmq port + 1 (client side
    derives the same host/port + 1 to connect)."""
    from .transports.zmq_transport import _default_zmq_port

    return "tcp://0.0.0.0:%d" % (_default_zmq_port(account_id) + 1)
