"""Pluggable transport layer for the BigQMT RPC bridge.

The :class:`~bigqmt_signal_trader.transports.base.RpcTransport` interface owns
the wire: how a request dict travels from the client to the QMT server and how
the response dict travels back. ``redis`` is the reference implementation; the
same business layer (handlers / ``process_request`` / ``to_jsonable``) runs
unchanged over any transport.

Select a transport with ``rpc.transport`` in the config (default ``"redis"``).
See :mod:`~bigqmt_signal_trader.transports.factory`.
"""

from .base import RpcTransport, TransportError, TransportTimeout
from .factory import build_transport

__all__ = ["RpcTransport", "TransportError", "TransportTimeout", "build_transport"]
