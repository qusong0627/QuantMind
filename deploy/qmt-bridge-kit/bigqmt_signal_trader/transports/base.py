"""Abstract transport interface for the BigQMT RPC bridge.

A transport owns the request/response wire. The business layer (handlers,
``process_request``, ``to_jsonable``, ``enqueue_payload``, ``drain_pending``)
is transport-agnostic; it only deals with request/response dicts.

Two roles, one interface
------------------------
* **Client side** — :meth:`RpcTransport.send_request`: send a request dict and
  block for the matching response dict (matched by ``request_id``).
* **Server side** — :meth:`RpcTransport.start_receiving` registers a callback
  ``on_request(request_dict)`` invoked per inbound request; the callback returns
  the response dict. :meth:`RpcTransport.send_response` delivers a response
  back to the client that sent ``request_dict`` (reply routing info is read
  from the request).

The request dict always carries the existing envelope (``schema_version``,
``request_id``, ``account_id``, ``method``, ``params``). It MAY carry reply
routing hints (``reply_key``/``reply_channel``/``reply_list``/``ttl_seconds``);
Redis uses them, other transports may ignore them and use native routing.
"""


class TransportError(RuntimeError):
    """A transport failed (connection lost, encode error, etc.)."""


class TransportTimeout(TimeoutError):
    """A request did not complete within the timeout window."""


class RpcTransport(object):
    """Abstract request/response transport. Concrete implementations own the wire.

    Subclasses MUST override :meth:`send_request`,
    :meth:`start_receiving`, :meth:`send_response`, and :meth:`stop`.
    """

    name = "abstract"

    def __init__(self, account_id="", print_prefix="[bigqmt_rpc]"):
        self.account_id = str(account_id or "")
        self.print_prefix = print_prefix
        self._on_request = None
        self._running = False

    # -- client side -------------------------------------------------------
    def send_request(self, request, timeout_seconds):
        """Send a request dict and block for the response dict.

        ``request`` is the full request envelope. Returns the response dict
        (with ``request_id`` matching). Raises :class:`TransportTimeout` if no
        response arrives within ``timeout_seconds``.
        """
        raise NotImplementedError

    # -- server side -------------------------------------------------------
    def start_receiving(self, on_request):
        """Begin accepting inbound requests on the server side.

        ``on_request(request_dict)`` is invoked per inbound request and MUST
        return the response dict. Implementations may spawn a background
        thread. Safe to call once per transport instance.
        """
        self._on_request = on_request
        self._running = True

    def send_response(self, request, response):
        """Deliver ``response`` back to the client that sent ``request``.

        Reply routing is read from ``request`` (e.g. ``reply_key`` /
        ``reply_channel`` / ``reply_list`` for Redis, or a native peer handle
        for ZMQ). Must be safe to call from the request-handling callback.
        """
        raise NotImplementedError

    def stop(self):
        """Stop receiving and release any sockets/connections/threads."""
        self._running = False
        self._on_request = None

    def deliver(self, request):
        """Internal: invoke the registered ``on_request`` callback.

        Concrete transports call this when an inbound request arrives. If the
        callback returns a non-None response dict, it is delivered back to the
        client via :meth:`send_response` automatically — so a callback only
        needs to ``return response``. Callbacks that send the response
        themselves (e.g. the Redis service path, which routes through
        ``_publish_response``) should return ``None`` to suppress the auto-send.

        Handler exceptions are turned into an ``ok=False`` response envelope so
        the receive loop keeps running.
        """
        callback = self._on_request
        if callback is None:
            return None
        try:
            response = callback(request)
        except Exception as exc:  # noqa: BLE001 - transport must survive
            import datetime as _dt

            response = {
                "schema_version": 1,
                "request_id": str((request or {}).get("request_id") or ""),
                "account_id": str((request or {}).get("account_id") or self.account_id or ""),
                "method": str((request or {}).get("method") or ""),
                "ok": False,
                "data": None,
                "error": "%s: %s" % (exc.__class__.__name__, exc),
                "handled_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
        if response is not None:
            try:
                self.send_response(request, response)
            except Exception:
                pass
        return response

    def __repr__(self):
        return "<%s account_id=%r>" % (self.__class__.__name__, self.account_id)
