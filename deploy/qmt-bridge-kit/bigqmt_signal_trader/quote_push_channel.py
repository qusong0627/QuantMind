"""Server→client whole-quote push channel.

The RPC transport is request/response only; whole-quote data needs the opposite
direction — the server pushes each incremental tick batch to every client
subscribed to that combination. This module provides one abstract channel with
two interchangeable implementations:

* :class:`ZmqQuotePushChannel` — a ``PUB`` socket on the server, a ``SUB`` socket
  per client. Native to no-redis deployments. Fire-and-forget: a client that is
  down simply misses frames (acceptable for incremental quote pushes).
* :class:`RedisQuotePushChannel` — redis ``publish``/``subscribe`` on a
  per-account, per-combination channel, for redis deployments.

Wire encoding is msgpack when available (smaller + faster for the
``{code: {field: number}}`` payload shape), falling back to stdlib json so the
channel stays usable without the optional dependency.
"""

import json
import threading
import time

try:
    import msgpack

    _HAS_MSGPACK = True
except Exception:  # pragma: no cover - depends on optional dependency
    msgpack = None
    _HAS_MSGPACK = False


def encode_push_payload(payload):
    """Encode a push payload dict to bytes (msgpack preferred, json fallback)."""
    if _HAS_MSGPACK:
        return msgpack.packb(payload, use_bin_type=True)
    return json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")


def decode_push_payload(blob):
    """Inverse of :func:`encode_push_payload`. Accepts bytes or str.

    Encoding is not symmetric across deployments: a server without msgpack
    falls back to json while a client with msgpack installed decodes with
    msgpack — ``msgpack.unpackb`` then raises ``ExtraData`` on the json text
    (its first byte ``{`` parses as an int, leaving trailing bytes). So try
    msgpack first, and fall back to json when the bytes are not a single
    valid msgpack object.
    """
    if blob is None:
        return None
    if isinstance(blob, str):
        blob = blob.encode("utf-8")
    if _HAS_MSGPACK:
        try:
            return msgpack.unpackb(blob, raw=False)
        except Exception:
            pass
    return json.loads(blob.decode("utf-8"))


class QuotePushChannel(object):
    """Abstract push channel. Server side: ``start_publisher`` + ``publish``.
    Client side: ``start_subscriber(topics, on_msg)``. A single instance may act
    as publisher or subscriber depending on which start method is called."""

    def start_publisher(self):
        raise NotImplementedError

    def start_subscriber(self, topics, on_msg):
        raise NotImplementedError

    def publish(self, topic, data):
        raise NotImplementedError

    def stop(self):
        raise NotImplementedError


class ZmqQuotePushChannel(QuotePushChannel):
    def __init__(self, bind_address=None, connect_address=None, context=None, print_prefix="[bigqmt_quote_push]"):
        self.bind_address = bind_address
        self.connect_address = connect_address
        self.print_prefix = print_prefix
        self._zmq = None
        self._context = context
        self._pub = None
        self._pub_lock = threading.Lock()
        self._sub = None
        self._sub_thread = None
        self._running = False

    def _ensure_context(self):
        if self._zmq is None:
            import zmq

            self._zmq = zmq
            if self._context is None:
                self._context = zmq.Context.instance()
        return self._zmq, self._context

    # -- server side ---------------------------------------------------------
    def start_publisher(self):
        zmq, ctx = self._ensure_context()
        if not self.bind_address:
            raise ValueError("bind_address is required to start a publisher")
        self._pub = ctx.socket(zmq.PUB)
        self._pub.bind(self.bind_address)
        self._running = True

    def publish(self, topic, data):
        payload = encode_push_payload({"combo_key": topic, "data": data})
        frame = [str(topic).encode("utf-8"), payload]
        # PUB socket is not thread-safe; serialize under the lock and read the
        # socket inside it so a concurrent stop() (which nulls _pub) can't hand
        # us a closed socket.
        with self._pub_lock:
            pub = self._pub
            if pub is None:
                return
            try:
                pub.send_multipart(frame)
            except Exception as exc:
                print("%s zmq publish failed: %s" % (self.print_prefix, exc))

    # -- client side ---------------------------------------------------------
    def start_subscriber(self, topics, on_msg):
        zmq, ctx = self._ensure_context()
        if not self.connect_address:
            raise ValueError("connect_address is required to start a subscriber")
        sub = ctx.socket(zmq.SUB)
        sub.connect(self.connect_address)
        for topic in topics or []:
            sub.setsockopt(zmq.SUBSCRIBE, str(topic).encode("utf-8"))
        self._sub = sub
        self._running = True
        self._sub_thread = threading.Thread(
            target=self._sub_loop, args=(sub, on_msg), name="bigqmt-quote-push-sub", daemon=True
        )
        self._sub_thread.start()

    def _sub_loop(self, sub, on_msg):
        # The SUB socket is owned by THIS thread; it must be closed HERE (in a
        # finally) and never from another thread. Closing a ZMQ socket cross-
        # thread trips a Windows signaler assertion and aborts the whole QMT
        # process (the "auto-exit" users hit).
        poller = self._zmq.Poller()
        poller.register(sub, self._zmq.POLLIN)
        try:
            while self._running:
                try:
                    events = dict(poller.poll(200))
                except Exception:
                    break
                if sub not in events:
                    continue
                try:
                    frames = sub.recv_multipart(self._zmq.NOBLOCK)
                except Exception:
                    continue
                if len(frames) < 2:
                    continue
                topic = frames[0].decode("utf-8", errors="ignore")
                data = decode_push_payload(frames[-1])
                payload_data = data.get("data") if isinstance(data, dict) else data
                try:
                    on_msg(topic, payload_data)
                except Exception as exc:
                    print("%s subscriber callback failed: %s" % (self.print_prefix, exc))
        finally:
            try:
                sub.close(linger=0)
            except Exception:
                pass

    def stop(self):
        # Signal the sub thread to exit and let IT close its own socket (see
        # _sub_loop). Closing the SUB socket from this (foreign) thread would
        # trip the Windows ZMQ signaler abort and crash QMT.
        self._running = False
        thread = self._sub_thread
        if thread is not None and thread.is_alive():
            thread.join(1.0)
        self._sub_thread = None
        self._sub = None
        # The PUB socket is only touched by publisher threads under _pub_lock;
        # null it first so a racing publish() sees None and bails, then close.
        with self._pub_lock:
            pub = self._pub
            self._pub = None
        if pub is not None:
            try:
                pub.close(linger=0)
            except Exception:
                pass


class RedisQuotePushChannel(QuotePushChannel):
    def __init__(self, redis_client, account_id="", channel_template="bigqmt:quote_push:{account_id}:{topic}", print_prefix="[bigqmt_quote_push]"):
        self.redis = redis_client
        self.account_id = str(account_id or "")
        self.channel_template = channel_template
        self.print_prefix = print_prefix
        self._running = False
        self._pubsub = None
        self._thread = None
        self._subscriber_stop = threading.Event()

    def _channel(self, topic):
        return self.channel_template.format(account_id=self.account_id, topic=topic)

    # -- server side ---------------------------------------------------------
    def start_publisher(self):
        # Redis publish needs no setup; present for interface symmetry.
        self._running = True

    def publish(self, topic, data):
        payload = encode_push_payload({"combo_key": topic, "data": data})
        try:
            self.redis.publish(self._channel(topic), payload)
        except Exception as exc:
            print("%s redis publish failed: %s" % (self.print_prefix, exc))

    # -- client side ---------------------------------------------------------
    def start_subscriber(self, topics, on_msg):
        self.stop()
        self._running = True
        self._subscriber_stop = threading.Event()
        self._thread = threading.Thread(
            target=self._sub_loop, args=(list(topics or []), on_msg, self._subscriber_stop),
            name="bigqmt-quote-push-sub", daemon=True
        )
        self._thread.start()

    def _sub_loop(self, topics, on_msg, stopped):
        # This receiver owns reconnect and closes its own connections. Capture
        # its stop event so a later start cannot revive an old blocked receiver.
        channels = [self._channel(topic) for topic in topics]
        failures = 0          # consecutive reconnect failures, for log throttling
        last_report = 0.0
        while not stopped.is_set():
            pubsub = None
            try:
                pubsub = self.redis.pubsub(ignore_subscribe_messages=True)
                self._pubsub = pubsub
                pubsub.subscribe(*channels)
                while not stopped.is_set():
                    message = pubsub.get_message(timeout=0.2)
                    if stopped.is_set():
                        break
                    if not message or message.get("type") != "message":
                        continue
                    channel = message.get("channel")
                    if isinstance(channel, bytes):
                        channel = channel.decode("utf-8", errors="ignore")
                    topic = str(channel).rsplit(":", 1)[-1]
                    data = decode_push_payload(message.get("data"))
                    payload_data = data.get("data") if isinstance(data, dict) else data
                    try:
                        on_msg(topic, payload_data)
                    except Exception as exc:
                        print("%s subscriber callback failed: %s" % (self.print_prefix, exc))
            except Exception as exc:
                if not stopped.is_set():
                    # Retry every second -- quotes should come back fast -- but
                    # do NOT print every second. Redis down over a weekend is
                    # 3600 lines/hour, and this repo has been bitten by log
                    # volume before (#139 wrote one line 16 times; #144 grew a
                    # log without bound because rotation could never run). The
                    # zmq ROUTER rebuild backs its reporting off for the same
                    # reason (#240). Report attempts 1, 2, 4, 8 ... then at most
                    # once a minute: the first failure stays loud and a long
                    # outage stays readable.
                    failures += 1
                    now = time.time()
                    if (failures & (failures - 1)) == 0 or now - last_report >= 60.0:
                        last_report = now
                        print("%s redis subscriber reconnecting (attempt %d): %s"
                              % (self.print_prefix, failures, exc))
            else:
                failures = 0
            finally:
                if pubsub is not None:
                    try:
                        pubsub.close()
                    except Exception:
                        pass
            stopped.wait(1.0)

    def stop(self):
        self._running = False
        self._subscriber_stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread() and thread.is_alive():
            thread.join(1.0)
        self._thread = None
        self._pubsub = None
