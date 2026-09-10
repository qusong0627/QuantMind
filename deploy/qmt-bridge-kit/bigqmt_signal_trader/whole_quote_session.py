"""Client-side whole-quote subscription session.

Owns the per-process state for ``subscribe_whole_quote``: the local
subscription table, the shared push-channel subscriber thread, and the
keepalive heartbeat thread. One session is shared by every ``subscribe_whole_quote``
call in the process (``BigQmtXtData`` delegates here), so all subscriptions ride
a single push-channel connection and a single heartbeat loop.

The big-QMT whole-quote callback is INCREMENTAL (only changed symbols), so a
subscription does not by itself deliver an initial full snapshot — callers layer
a ``get_full_tick`` prime on top (done in ``BigQmtXtData.subscribe_whole_quote``).
"""

import threading


def _norm_topic(code_list):
    return ",".join(sorted({str(c).strip().upper() for c in (code_list or []) if str(c or "").strip()}))


class WholeQuoteClientSession(object):
    def __init__(self, rpc_call, push_channel, client_id, heartbeat_interval_seconds=3.0, sub_id_func=None,
                 push_silence_replay_heartbeats=10):
        """``rpc_call`` is ``client.call``-shaped: fn(method, params) -> dict.
        ``push_channel`` is a QuotePushChannel used purely as a subscriber.
        ``sub_id_func`` (optional) mints subscription ids; defaults to a counter.
        ``push_silence_replay_heartbeats``: after this many heartbeat rounds
        without any push, replay subscriptions (covers server restarts where
        keepalive keeps succeeding because the redis request queue buffers
        during the restart window but the subscription table was reset)."""
        self._rpc = rpc_call
        self._channel = push_channel
        self.client_id = str(client_id or "")
        self._heartbeat_interval = float(heartbeat_interval_seconds)
        self._push_silence_replay_heartbeats = int(push_silence_replay_heartbeats)
        self._sub_id_func = sub_id_func
        self._seq = 0
        self._lock = threading.RLock()
        self._subscriptions = {}  # sub_id -> {"topic": str, "callback": fn, "codes": [...]}
        self._started = False
        self._subscriber_active = False
        self._subscribed_topics = frozenset()  # topic set the subscriber covers now
        self._heartbeat_thread = None
        self._last_push_time = None  # monotonic time of last incoming push
        self._replay_pending = False  # a failed batch must finish despite other subscriptions pushing

    # -- subscription lifecycle ---------------------------------------------
    def subscribe_whole_quote(self, code_list, callback=None):
        codes = [str(c) for c in (code_list or []) if str(c or "").strip()]
        if not codes:
            raise ValueError("code_list is required")
        with self._lock:
            sub_id = self._next_sub_id()
        result = self._rpc(
            "subscribe_whole_quote",
            {"client_id": self.client_id, "sub_id": sub_id, "codes": codes},
        ) or {}
        topic = str(result.get("topic") or result.get("combo_key") or _norm_topic(codes))
        with self._lock:
            self._subscriptions[sub_id] = {"topic": topic, "callback": callback, "codes": codes}
            self._sync_subscriber_locked()
        return sub_id

    def unsubscribe_quote(self, sub_id):
        with self._lock:
            entry = self._subscriptions.pop(sub_id, None)
        if entry is None:
            return 0
        try:
            self._rpc("unsubscribe_whole_quote", {"client_id": self.client_id, "sub_id": sub_id})
        finally:
            with self._lock:
                self._sync_subscriber_locked()
        return 0

    def has_subscription(self, sub_id):
        with self._lock:
            return sub_id in self._subscriptions

    def replay_subscriptions(self):
        """Re-send subscribe for every active sub_id (server restart recovery).
        Idempotent on the server (keyed by client_id+combo), so replays are safe."""
        with self._lock:
            items = [(sid, dict(entry)) for sid, entry in self._subscriptions.items()]
            self._replay_pending = True
        error = None
        for sub_id, entry in items:
            try:
                self._rpc(
                    "subscribe_whole_quote",
                    {"client_id": self.client_id, "sub_id": sub_id, "codes": entry["codes"]},
                )
            except Exception as exc:
                if error is None:
                    error = exc
        if error is not None:
            raise error
        with self._lock:
            self._replay_pending = False

    # -- heartbeat -------------------------------------------------------------
    def start(self):
        with self._lock:
            thread = self._heartbeat_thread
            if self._started and thread is not None and thread.is_alive():
                return
            # A past replay exception may have killed the loop while _started
            # stayed True (#231) -- "started" must mean "a live thread",
            # otherwise start() never recovers it.
            self._started = True
            self._heartbeat_thread = threading.Thread(
                target=self._heartbeat_loop, name="bigqmt-quote-keepalive", daemon=True
            )
            self._heartbeat_thread.start()

    def stop(self):
        with self._lock:
            self._started = False
        thread = self._heartbeat_thread
        if thread is not None:
            thread.join(timeout=1.0)
        self._heartbeat_thread = None

    def _heartbeat_loop(self):
        import time

        consecutive_failures = 0
        silence_rounds = 0
        prev_last_push = None
        while True:
            with self._lock:
                if not self._started:
                    return
                sub_ids = list(self._subscriptions.keys())
                last_push = self._last_push_time
            if not sub_ids:
                time.sleep(self._heartbeat_interval)
                continue
            failures = 0
            for sub_id in sub_ids:
                try:
                    self._rpc("quote_keepalive", {"client_id": self.client_id, "sub_id": sub_id})
                except Exception:
                    failures += 1
            recovered = not failures and consecutive_failures >= 3
            if failures:
                consecutive_failures += 1
            else:
                consecutive_failures = 0
            # Push-silence detection: a server restart can survive with keepalive
            # succeeding (the redis request queue buffers during the restart
            # window) while the subscription table was reset, so pushes stop.
            # Replay when no push arrived for several heartbeat rounds (also
            # covers the case where the very first prime push never arrived).
            if last_push != prev_last_push:
                silence_rounds = 0  # a push arrived since the last round
            else:
                silence_rounds += 1
            prev_last_push = last_push
            with self._lock:
                replay_pending = self._replay_pending
            if recovered or replay_pending or silence_rounds >= self._push_silence_replay_heartbeats:
                # Retry the idempotent batch until every subscription succeeds.
                # A's pushes cannot erase B's unresolved replay (#231).
                try:
                    self.replay_subscriptions()
                except Exception:
                    pass
                silence_rounds = 0
            time.sleep(self._heartbeat_interval)

    # -- push routing ------------------------------------------------------------
    def _on_push(self, topic, data):
        import time

        now = time.monotonic()
        with self._lock:
            self._last_push_time = now
            callbacks = [
                entry["callback"]
                for entry in self._subscriptions.values()
                if entry["topic"] == topic and entry["callback"] is not None
            ]
        for callback in callbacks:
            try:
                callback(data)
            except Exception:
                pass

    def _sync_subscriber_locked(self):
        """(Re)start the push-channel subscriber to cover exactly the active
        topics. Reuses an existing subscriber when the topic set is unchanged;
        stops it before restarting when the set changed. No-op when nothing is
        subscribed (and stops the running subscriber in that case)."""
        topics = sorted({entry["topic"] for entry in self._subscriptions.values()})
        active = frozenset(topics)
        if active == self._subscribed_topics:
            return
        if not active:
            if self._subscriber_active:
                try:
                    self._channel.stop()
                except Exception:
                    pass
                self._subscriber_active = False
            self._subscribed_topics = active
            return
        if self._subscriber_active:
            try:
                self._channel.stop()
            except Exception:
                pass
        self._channel.start_subscriber(topics, self._on_push)
        self._subscriber_active = True
        self._subscribed_topics = active

    def _next_sub_id(self):
        if self._sub_id_func is not None:
            return self._sub_id_func()
        self._seq += 1
        return self._seq
