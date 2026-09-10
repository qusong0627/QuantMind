"""Shared-memory transport stub.

Reserved for a future low-latency same-host backend. Not implemented because
the QMT runtime ships Python 3.6, where ``multiprocessing.shared_memory`` is
unavailable (added in 3.8). A ``mmap``-plus-named-mutex implementation is
possible but non-trivial; until it lands, selecting this transport raises a
clear error so misconfiguration fails fast.
"""

from .base import RpcTransport, TransportError


class SharedMemoryTransport(RpcTransport):
    name = "shm"

    def __init__(self, account_id="", print_prefix="[bigqmt_rpc]", **kwargs):
        super(SharedMemoryTransport, self).__init__(
            account_id=account_id, print_prefix=print_prefix
        )

    def _unsupported(self):
        raise TransportError(
            "shared-memory transport is not implemented yet "
            "(requires Python 3.8+ shared_memory or a custom mmap ring buffer)"
        )

    def send_request(self, request, timeout_seconds):
        self._unsupported()

    def send_response(self, request, response):
        self._unsupported()

    def start_receiving(self, on_request, **kwargs):
        self._unsupported()
