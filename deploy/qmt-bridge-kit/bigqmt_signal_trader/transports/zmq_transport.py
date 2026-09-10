"""ZeroMQ transport for the BigQMT RPC bridge.

Designed for same-host low latency. Topology:

* **Server** binds a ``ROUTER`` socket. Each inbound message arrives as
  ``[identity, payload]``; the server remembers ``identity`` keyed by
  ``request_id`` and replies with ``[identity, payload]`` so ZMQ routes the
  response back to the originating client automatically.
* **Client** connects a ``DEALER`` socket (with a unique random identity), sends
  ``[payload]``, then ``poll``/``recv`` for the response. DEALER gives each
  client an asymmetric async path that pairs naturally with ROUTER.

Wire framing is a single JSON payload per message. The original b64 stock-code
obfuscation (``encode_rpc_request_payload``) is applied too, so payloads stay
opaque even though ZMQ does not need it — keeps the wire uniform with Redis.

Two threads on the server: the ROUTER recv loop, and a per-client is implicit
(ZMQ handles multiplexing). One thread on the client for recv is avoided by
using DEALER + ``poll`` (synchronous request/response fits the RPC model).
"""

import json
import queue
import threading
import traceback
import time
import uuid

from ..adapters.redis_common import decode_text
from ..redis_rpc import (
    TYPED_PAYLOAD_FLAG, TYPED_PAYLOAD_MARKER,
    decode_rpc_request_payload,
    encode_rpc_request_payload,
)
from .base import RpcTransport, TransportError, TransportTimeout


# ZMQ does not support ipc:// on Windows (it trips a signaler abort), so the
# default endpoint is tcp loopback. The port is derived from the account_id so
# distinct accounts don't collide on the same port; override via config when
# needed. Base 15560 keeps it clear of common dev ports.
DEFAULT_ZMQ_HOST = "127.0.0.1"
DEFAULT_ZMQ_BASE_PORT = 15560
DEFAULT_ZMQ_PORT_RANGE = 100  # derived port = base + (account_id_int mod range)


def _default_zmq_port(account_id):
    """Derive a stable port from account_id so each account gets its own socket."""
    text = str(account_id or "")
    digits = "".join(ch for ch in text if ch.isdigit())
    try:
        offset = int(digits) % DEFAULT_ZMQ_PORT_RANGE if digits else 0
    except ValueError:
        offset = 0
    return DEFAULT_ZMQ_BASE_PORT + offset


def _default_zmq_address(account_id, host=None):
    host = host or DEFAULT_ZMQ_HOST
    return "tcp://%s:%d" % (host, _default_zmq_port(account_id))


def _loads(raw):
    if isinstance(raw, dict):
        # Already an object (in-process routing): no text to scan, so the
        # client walks it as before rather than assuming there is nothing.
        return dict(raw)
    text = decode_text(raw)
    text = decode_rpc_request_payload(text)
    response = json.loads(text)
    if isinstance(response, dict):
        response[TYPED_PAYLOAD_FLAG] = TYPED_PAYLOAD_MARKER in text
    return response


class ZmqTransport(RpcTransport):
    """ZMQ ROUTER/DEALER transport.

    The same instance plays both roles depending on method called:
    ``send_request`` acts as a client (DEALER connect), ``start_receiving`` +
    ``send_response`` act as a server (ROUTER bind). A deployment normally uses
    one instance per role (the QMT process is the server; the external client
    is the client).
    """

    name = "zmq"

    def __init__(
        self,
        bind_address=None,
        connect_address=None,
        host=None,
        port=None,
        account_id="",
        print_prefix="[bigqmt_rpc]",
        io_threads=1,
        recv_timeout_seconds=1.0,
        server_hwm=10000,
        client_linger_ms=0,
        discovery_redis_client=None,
        discovery_key_template="bigqmt:zmq:addr:{account_id}",
        discovery_ttl_seconds=300,
        port_scan_range=50,
        stall_warn_seconds=20.0,
        stall_check_seconds=5.0,
    ):
        super(ZmqTransport, self).__init__(account_id=account_id, print_prefix=print_prefix)
        # Address resolution order: explicit bind_address/connect_address win;
        # otherwise build tcp://host:port from host/port (port defaults to a
        # value derived from account_id so distinct accounts don't collide).
        resolved_host = host or DEFAULT_ZMQ_HOST
        if port is not None:
            resolved_port = int(port)
        else:
            resolved_port = _default_zmq_port(account_id)
        default_addr = "tcp://%s:%d" % (resolved_host, resolved_port)
        self.bind_address = bind_address or default_addr
        self.connect_address = connect_address
        self.bind_host = resolved_host
        self.base_port = resolved_port
        # Handlers run one at a time, so anything past a few seconds is
        # holding up every queued request. 20s is comfortably longer than the
        # slowest healthy call measured here (a whole-market snapshot, 7.7s)
        # and far short of a client's default 30s timeout, so the log names
        # the culprit before the caller gives up. 0 disables the watchdog.
        self.stall_warn_seconds = float(stall_warn_seconds)
        self.stall_check_seconds = max(float(stall_check_seconds), 0.5)
        self.io_threads = int(io_threads)
        self.recv_timeout_seconds = float(recv_timeout_seconds)
        self.server_hwm = int(server_hwm)
        self.client_linger_ms = int(client_linger_ms)
        # Discovery remains available for clients, but a server must bind the
        # configured address exactly. ``port_scan_range`` is retained only for
        # backward-compatible config loading and is intentionally not used.
        self.discovery_redis_client = discovery_redis_client
        self.discovery_key_template = discovery_key_template
        self.discovery_ttl_seconds = int(discovery_ttl_seconds)
        self.port_scan_range = int(port_scan_range)

        self._zmq = None  # imported lazily
        self._ctx = None
        # server state
        self._router = None
        self._router_thread = None
        self._actual_bind_address = None  # set after start_receiving()
        self._reply_residency = {"count": 0, "sum_ms": 0.0, "max_ms": 0.0}
        # Wake pipe: a reply produced on the adjust thread is queued, and the
        # router thread only drains the queue at the top of its loop -- after a
        # recv that blocks up to RCVTIMEO. Measured residency was 1149ms avg on
        # a 1s RCVTIMEO, which is most of a 1500ms trade query (#104). Signalling
        # here lets the loop come round at once instead of polling, so idle costs
        # nothing -- the router thread shares the GIL with QMT's strategy thread
        # and must not spin.
        self._wake_endpoint = "inproc://bigqmt-wake-%d" % id(self)
        self._wake_recv = None
        self._wake_send = None
        self._wake_lock = threading.Lock()
        self._pending_identities = {}  # request_id -> client identity bytes
        self._identity_lock = threading.Lock()
        self._response_queue = queue.Queue()
        self._queued_response_count = 0
        self._sent_response_count = 0
        self._dropped_response_count = 0
        # 出站堆积让位队列的上限（满时丢最旧，防止背压变成内存膨胀）。
        self._max_queued_responses = 2000
        # (method, started_at, thread_name) while a handler is running.
        # Reported by the stall watchdog below; None when idle.
        self._in_flight = None
        self._stall_thread = None
        # client state. One DEALER per calling thread (#186): a zmq socket is
        # not thread-safe, and the single shared one had to be held for the
        # whole send/poll/recv cycle -- so N threads took turns and concurrency
        # bought nothing. Measured on the live terminal, 4 threads moved zmq
        # from 2.4 to 2.7 requests/sec while redis, which has no such lock,
        # went from 20.7 to 143.3.
        #
        # One socket per thread is zmq's own answer. Each gets its own random
        # IDENTITY, so the server's ROUTER routes every reply back to the
        # thread that asked. _client_lock now guards only the registry.
        self._dealer_local = threading.local()
        self._dealers = []          # [(thread, socket)] for shutdown + pruning
        self._client_lock = threading.Lock()

    # -- construction helper ----------------------------------------------
    @classmethod
    def from_config(cls, config, account_id="", print_prefix="[bigqmt_rpc]"):
        config = dict(config or {})
        return cls(
            bind_address=config.get("bind_address"),
            connect_address=config.get("connect_address"),
            host=config.get("host"),
            port=config.get("port"),
            account_id=config.get("account_id", account_id),
            print_prefix=print_prefix,
            io_threads=int(config.get("io_threads", 1)),
            recv_timeout_seconds=float(config.get("recv_timeout_seconds", 1.0)),
            server_hwm=int(config.get("server_hwm", 10000)),
            client_linger_ms=int(config.get("client_linger_ms", 0)),
            discovery_redis_client=config.get("discovery_redis_client"),
            discovery_key_template=config.get(
                "discovery_key_template", "bigqmt:zmq:addr:{account_id}"
            ),
            discovery_ttl_seconds=int(config.get("discovery_ttl_seconds", 300)),
            port_scan_range=int(config.get("port_scan_range", 50)),
            stall_warn_seconds=float(config.get("stall_warn_seconds", 20.0)),
            stall_check_seconds=float(config.get("stall_check_seconds", 5.0)),
        )

    # -- shared zmq context -----------------------------------------------
    def _ensure_zmq(self):
        if self._zmq is None:
            try:
                import zmq  # noqa: F401
            except ImportError as exc:  # pragma: no cover - depends on env
                raise TransportError(
                    "pyzmq is required for the zmq transport: %s" % exc
                )
            self._zmq = zmq
        if self._ctx is None:
            self._ctx = self._zmq.Context.instance(self.io_threads)
        return self._zmq, self._ctx

    # -- server side ------------------------------------------------------
    def _open_wake_pipe(self):
        """PULL end for the router loop, PUSH end for whoever queues a reply.

        inproc, so it never touches the network and needs no port. Failure is
        non-fatal: without it the loop still drains on its own timeout, just
        slowly -- which is exactly the behaviour this replaces.
        """
        try:
            zmq, ctx = self._ensure_zmq()
            recv = ctx.socket(zmq.PULL)
            recv.setsockopt(zmq.LINGER, 0)
            recv.bind(self._wake_endpoint)
            self._wake_recv = recv
        except Exception as exc:
            print("%s zmq wake pipe unavailable: %s" % (self.print_prefix, exc))
            self._wake_recv = None

    def _signal_wake(self):
        """Nudge the router loop. Called from the adjust thread.

        zmq sockets are not thread safe, so the PUSH end is created once here
        and used only under the lock; the PULL end belongs to the router thread.
        """
        if self._wake_recv is None:
            return
        try:
            with self._wake_lock:
                if self._wake_send is None:
                    zmq, ctx = self._ensure_zmq()
                    send = ctx.socket(zmq.PUSH)
                    send.setsockopt(zmq.LINGER, 0)
                    send.connect(self._wake_endpoint)
                    self._wake_send = send
                self._wake_send.send(b"1", self._zmq.DONTWAIT)
        except Exception:
            pass          # a missed nudge costs latency, never correctness

    def _bind_configured_address(self):
        """Bind exactly one configured address and reject duplicate servers."""
        zmq, ctx = self._ensure_zmq()
        sock = ctx.socket(zmq.ROUTER)
        sock.setsockopt(zmq.RCVHWM, self.server_hwm)
        sock.setsockopt(zmq.SNDHWM, self.server_hwm)
        sock.setsockopt(zmq.RCVTIMEO, int(self.recv_timeout_seconds * 1000))
        try:
            sock.bind(self.bind_address)
        except self._zmq.ZMQError as exc:
            try:
                sock.close(linger=0)
            except Exception:
                pass
            if getattr(exc, "errno", None) == zmq.EADDRINUSE:
                # 端口被占——通常是之前策略实例没正常停止。给出友好提示和解决步骤。
                print(
                    "%s ZMQ_BIND_CONFLICT: 端口 %s 被占用！"
                    % (self.print_prefix, self.bind_address)
                )
                print(
                    "%s   原因：之前的 QMT 策略实例没正常停止，仍占着这个端口。"
                    % self.print_prefix
                )
                print(
                    "%s   解决：1) 在 QMT 里停止旧策略再运行；2) 或等 60s 让系统释放端口；"
                    % self.print_prefix
                )
                print(
                    "%s   3) 或改配置用别的端口（BIGQMT_REDIS_CONFIG.zmq.port）"
                    % self.print_prefix
                )
                raise TransportError(
                    "ZMQ_BIND_CONFLICT address=%s; another bridge instance "
                    "already owns the configured endpoint" % self.bind_address
                )
            raise
        self._router = sock
        self._actual_bind_address = self.bind_address
        self._publish_discovery(self.bind_address)

    def _publish_discovery(self, address):
        if self.discovery_redis_client is None:
            return
        key = self.discovery_key_template.format(account_id=self.account_id)
        try:
            self.discovery_redis_client.setex(
                key, self.discovery_ttl_seconds, address
            )
        except Exception as exc:
            print("%s zmq discovery publish failed: %s" % (self.print_prefix, exc))

    def _clear_discovery(self):
        if self.discovery_redis_client is None:
            return
        key = self.discovery_key_template.format(account_id=self.account_id)
        try:
            self.discovery_redis_client.delete(key)
        except Exception:
            pass

    def start_receiving(self, on_request, background_threads=True):
        super(ZmqTransport, self).start_receiving(on_request)
        zmq, ctx = self._ensure_zmq()
        self._bind_configured_address()
        bound = self._actual_bind_address or self.bind_address
        if not background_threads:
            print(
                "%s zmq bound=%s background_threads=False"
                % (self.print_prefix, bound)
            )
            return
        self._open_wake_pipe()
        self._router_thread = threading.Thread(
            target=self._router_loop, name="bigqmt-zmq-rpc", daemon=True
        )
        self._router_thread.start()
        if self.stall_warn_seconds > 0:
            self._stall_thread = threading.Thread(
                target=self._stall_watchdog_loop, name="bigqmt-zmq-stall",
                daemon=True)
            self._stall_thread.start()
        print(
            "%s zmq started bound=%s" % (self.print_prefix, self.bind_address)
        )

    def _router_loop(self):
        """接收循环，出错自动重建 ROUTER 并继续（#240）。

        原来这里是一层 try/finally：任何逃出内层 while 的异常都会走到 finally
        关掉 socket、线程结束，**桥从此不再接收任何请求，也没有任何提示** ——
        从客户端看和「服务端死了」一模一样，而 QMT 里的策略还好好地跑着，
        adjust 照常打点。

        redis 那两条循环早就是「异常 -> 退避 -> 重建连接」的形状；zmq 缺这一层。
        现在补上：内层跑接收，外层负责重建。退避从 1 秒起、翻倍到 30 秒封顶 ——
        端口被别的进程占着时不至于每秒刷屏。
        """
        backoff = 1.0
        while self._running:
            try:
                self._router_session()
                backoff = 1.0          # 正常退出（stop），不重连
            except Exception:
                if not self._running:
                    break
                print("%s zmq router failed, rebuilding in %.0fs:\n%s"
                      % (self.print_prefix, backoff, traceback.format_exc()))
                time.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
                if not self._running:
                    break
                try:
                    self._bind_configured_address()
                except Exception:
                    # 重建失败（端口还被占着）——下一轮继续退避重试，
                    # 不能在这里放弃：放弃就回到了「线程静悄悄死掉」。
                    print("%s zmq rebind failed, will retry" % self.print_prefix)
                    continue
                print("%s zmq router rebuilt bound=%s"
                      % (self.print_prefix, self._actual_bind_address or self.bind_address))

    def _router_session(self):
        poller = None
        if self._wake_recv is not None:
            try:
                poller = self._zmq.Poller()
                poller.register(self._router, self._zmq.POLLIN)
                poller.register(self._wake_recv, self._zmq.POLLIN)
            except Exception:
                poller = None
        timeout_ms = int(self.recv_timeout_seconds * 1000)
        try:
            while self._running:
                self._drain_response_queue()
                if poller is None:
                    request = self._receive_request()
                    if request is not None:
                        self._deliver_request(request)
                    continue
                # Waiting on both ends means a reply queued by the adjust
                # thread wakes this loop immediately instead of after RCVTIMEO.
                try:
                    events = dict(poller.poll(timeout=timeout_ms))
                except Exception:
                    events = {}
                if self._wake_recv in events:
                    try:
                        while True:
                            self._wake_recv.recv(self._zmq.DONTWAIT)
                    except Exception:
                        pass
                if self._router in events:
                    request = self._receive_request(flags=self._zmq.NOBLOCK)
                    if request is not None:
                        self._deliver_request(request)
        finally:
            # Close the ROUTER socket on the thread that owns it. On Windows,
            # closing a ZMQ socket from a different thread trips a signaler
            # assertion (abort); closing it here is safe because this thread
            # created and exclusively used it.
            #
            # 只在真正停机时关。重连路径上 _bind_configured_address 会建一个新的，
            # 这里再关就把新 socket 关掉了 —— 那会让「重连成功」变成静默失效。
            if not self._running:
                try:
                    self._router.close(linger=0)
                except Exception:
                    pass
                self._router = None

    def _receive_request(self, flags=0):
        try:
            frames = self._router.recv_multipart(flags=flags)
        except self._zmq.Again:
            return None
        except Exception as exc:
            if self._running:
                print("%s zmq recv failed: %s" % (self.print_prefix, exc))
                if not flags:
                    time.sleep(0.5)
            return None
        if len(frames) < 2:
            return None
        identity, payload = frames[0], frames[-1]
        try:
            request = _loads(payload)
        except Exception as exc:
            print("%s zmq decode failed: %s" % (self.print_prefix, exc))
            return None
        request_id = str(request.get("request_id") or uuid.uuid4().hex)
        with self._identity_lock:
            self._pending_identities[request_id] = identity
        return request

    def _deliver_request(self, request):
        started = time.perf_counter()
        method = request.get("method")
        # Published for the watchdog. Handlers run one at a time on this
        # thread, so a single slot is enough.
        self._in_flight = (method, started, threading.current_thread().name)
        try:
            self.deliver(request)
        except Exception as exc:
            print("%s zmq deliver failed: %s" % (self.print_prefix, exc))
        finally:
            self._in_flight = None
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        if elapsed_ms > 50.0:
            print("%s zmq slow handler method=%s %.0fms"
                  % (self.print_prefix, method, elapsed_ms))

    def _stall_watchdog_loop(self):
        """Report a handler that is STILL running, while it still is.

        The slow-handler line above is measured after deliver() returns, so a
        handler that blocks says nothing until it finishes. One did, for 346
        seconds: get_financial_data on the first call after a restart, while
        QMT itself was healthy and the main strategy thread idle. Every
        request queued behind it timed out, and the only clue in the log
        arrived once it was already over.

        A stalled bridge and a dead one look identical from the client. This
        is what tells them apart, at the time it matters.
        """
        reported_at = 0.0
        while self._running:
            time.sleep(self.stall_check_seconds)
            snapshot = self._in_flight
            if snapshot is None:
                reported_at = 0.0
                continue
            method, started, thread_name = snapshot
            elapsed = time.perf_counter() - started
            if elapsed < self.stall_warn_seconds:
                continue
            # Back off: every interval at first, then less often, so a very
            # long stall does not bury the log it is meant to explain.
            interval = self.stall_warn_seconds * (2 ** min(reported_at, 6))
            if reported_at and elapsed < interval:
                continue
            reported_at += 1
            print("%s zmq handler STILL RUNNING method=%s %.0fs thread=%s "
                  "queued=%d -- the bridge is blocked, not dead"
                  % (self.print_prefix, method, elapsed, thread_name,
                     self._response_queue.qsize()))

    def _drain_response_queue(self):
        while True:
            try:
                identity, payload, queued_at = self._response_queue.get_nowait()
            except queue.Empty:
                return
            self._note_reply_residency((time.perf_counter() - queued_at) * 1000.0)
            try:
                self._router.send_multipart([identity, payload])
                self._sent_response_count += 1
                if self._sent_response_count <= 5:
                    print("%s zmq queued response sent" % self.print_prefix)
            except Exception as exc:
                print("%s zmq send failed: %s" % (self.print_prefix, exc))

    def _note_reply_residency(self, ms):
        """How long a finished reply waited for the router loop to come round.

        Measured because the round trip did not add up: ping handles in 0.1ms
        and the adjust tick is 96ms, yet a request takes ~300ms end to end and
        throughput sits at ~3.3/s no matter how many clients ask (#104).
        """
        stats = self._reply_residency
        stats["count"] += 1
        stats["sum_ms"] += ms
        if ms > stats["max_ms"]:
            stats["max_ms"] = ms

    def reply_residency_stats(self):
        stats = dict(self._reply_residency)
        count = stats.get("count") or 0
        stats["avg_ms"] = round(stats["sum_ms"] / count, 1) if count else 0.0
        stats["max_ms"] = round(stats["max_ms"], 1)
        stats["sum_ms"] = round(stats["sum_ms"], 1)
        return stats

    def _queue_response(self, identity, payload, reason=""):
        # 出站堆积时让位排队而不是阻塞 router 线程：队列有上限，满时丢最旧
        # 并记日志（读类请求超时可重试，比全管道停摆好）。
        self._queued_response_count += 1
        # Stamped so _drain_response_queue can report how long a reply sat
        # here. The handler runs on the adjust thread, so every reply takes
        # this path, and the queue is only drained at the top of the router
        # loop -- after a recv_multipart that blocks up to RCVTIMEO. That
        # residency, not the handler, is where the round trip goes (#104).
        self._response_queue.put((identity, payload, time.perf_counter()))
        self._signal_wake()
        if self._response_queue.qsize() > self._max_queued_responses:
            try:
                self._response_queue.get_nowait()
                self._dropped_response_count += 1
                if self._dropped_response_count <= 5 or self._dropped_response_count % 100 == 0:
                    print("%s zmq response queue full (%d), dropped oldest (%s)"
                          % (self.print_prefix, self._max_queued_responses, reason))
            except queue.Empty:
                pass

    def send_response(self, request, response):
        if self._router is None:
            raise TransportError("zmq server socket is not bound")
        request_id = str(
            response.get("request_id") or request.get("request_id") or ""
        )
        with self._identity_lock:
            identity = self._pending_identities.pop(request_id, None)
        if identity is None:
            # No matching peer — drop silently (client may have gone away).
            return
        payload = encode_rpc_request_payload(response).encode("utf-8")
        if self._router_thread is not None and threading.current_thread() is not self._router_thread:
            self._queue_response(identity, payload, reason="off-thread")
            return
        try:
            # 硬阻塞改非阻塞：peer 水位满（出站堆积）时不让位整个 router 线程
            # ~200ms——改道进队列由 drain 循环重试，线程永不停摆。
            self._router.send_multipart([identity, payload], self._zmq.DONTWAIT)
        except self._zmq.Again:
            self._queue_response(identity, payload, reason="peer-muted")
        except Exception as exc:
            print("%s zmq send failed: %s" % (self.print_prefix, exc))

    def drain_request_queue(self, max_items=20):
        """Drain requests from the scheduled QMT thread when no receiver thread exists."""
        if self._router_thread is not None or self._router is None:
            return 0
        processed = 0
        for _index in range(max(int(max_items), 0)):
            request = self._receive_request(flags=self._zmq.NOBLOCK)
            if request is None:
                break
            self._deliver_request(request)
            processed += 1
        return processed

    # -- client side ------------------------------------------------------
    def _resolve_connect_address(self):
        """Resolve the address to connect to.

        Order: explicit connect_address > discovery lookup > default derived.
        Discovery lets the client find a server that had to move off the
        default port because of a collision.
        """
        if self.connect_address:
            return self.connect_address
        discovered = self._lookup_discovery()
        if discovered:
            return discovered
        return _default_zmq_address(self.account_id)

    def _lookup_discovery(self):
        if self.discovery_redis_client is None:
            return None
        key = self.discovery_key_template.format(account_id=self.account_id)
        try:
            raw = self.discovery_redis_client.get(key)
        except Exception:
            return None
        if not raw:
            return None
        try:
            text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
        except Exception:
            return None
        return text or None

    def _reap_dead_dealers(self):
        """Close sockets whose owning thread has exited. Caller holds the lock.

        Without this a caller that spawns a thread per request would leak one
        socket -- and one TCP connection to the ROUTER -- per thread until
        stop(). The owner is gone by definition here, so nobody can be inside
        a send or recv on it; that is what makes closing it from this thread
        safe, unlike the router socket (see stop()).
        """
        alive = []
        for owner, sock in self._dealers:
            if owner.is_alive():
                alive.append((owner, sock))
                continue
            try:
                sock.close(linger=0)
            except Exception:
                pass
        self._dealers = alive

    def _ensure_dealer(self):
        """This thread's DEALER, created on first use."""
        sock = getattr(self._dealer_local, "sock", None)
        if sock is not None:
            return sock
        zmq, ctx = self._ensure_zmq()
        address = self._resolve_connect_address()
        sock = ctx.socket(zmq.DEALER)
        # Unique identity so ROUTER can route replies back to us.
        sock.setsockopt(zmq.IDENTITY, uuid.uuid4().hex.encode("utf-8")[:16])
        sock.setsockopt(zmq.LINGER, self.client_linger_ms)
        sock.connect(address)
        self._dealer_local.sock = sock
        with self._client_lock:
            self._reap_dead_dealers()
            self._dealers.append((threading.current_thread(), sock))
            self.connect_address = address
        return sock

    def send_request(self, request, timeout_seconds, **_kwargs):
        # No lock around the round trip: this socket belongs to this thread
        # and no other thread touches it (#186).
        zmq = self._zmq or self._ensure_zmq()[0]
        dealer = self._ensure_dealer()
        request = dict(request)
        request.setdefault("request_id", uuid.uuid4().hex)
        request_id = request["request_id"]
        payload = encode_rpc_request_payload(request)
        try:
            dealer.send(payload.encode("utf-8"))
        except Exception as exc:
            raise TransportError("zmq send failed: %s" % exc)
        deadline = time.time() + float(timeout_seconds)
        poller = zmq.Poller()
        poller.register(dealer, zmq.POLLIN)
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            events = dict(poller.poll(timeout=int(remaining * 1000)))
            if dealer in events:
                frames = dealer.recv_multipart()
                raw = frames[-1]
                response = _loads(raw)
                # A late reply to a request that already timed out lands here;
                # skipping it keeps this thread's socket usable afterwards.
                if response.get("request_id") == request_id:
                    return response
        raise TransportTimeout("zmq rpc timeout: %s" % request.get("method"))

    # -- lifecycle --------------------------------------------------------
    def stop(self):
        super(ZmqTransport, self).stop()
        # Clear _running so the router loop exits; the loop closes its own
        # socket (closing cross-thread trips a Windows signaler abort).
        thread = self._router_thread
        if thread is not None and thread.is_alive():
            thread.join(2.0)
        if thread is None and self._router is not None:
            try:
                self._router.close(linger=0)
            except Exception:
                pass
            self._router = None
        self._router_thread = None
        # If we were a server that published a discovery address, clear it so
        # clients don't keep hitting a dead endpoint.
        if self._actual_bind_address is not None:
            self._clear_discovery()
            self._actual_bind_address = None
        with self._client_lock:
            for _owner, sock in self._dealers:
                try:
                    sock.close(linger=self.client_linger_ms)
                except Exception:
                    pass
            self._dealers = []
        # The caller's own thread-local still points at a closed socket; drop
        # it so a send_request after stop() builds a fresh one rather than
        # raising on a dead handle. Other threads' locals are unreachable from
        # here -- their sockets are closed above, and _ensure_dealer would hand
        # back the closed one, so those threads must not reuse the transport
        # after stop(). That was true of the single shared dealer too.
        if getattr(self._dealer_local, "sock", None) is not None:
            self._dealer_local.sock = None
        # Do NOT terminate the shared context — other sockets/users may rely on it.
