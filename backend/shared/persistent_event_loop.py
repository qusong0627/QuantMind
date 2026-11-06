"""Long-lived event loop for sync callers that must touch asyncpg pools.

asyncpg's connection pool is bound to the event loop that first checked the
connection out. ``asyncio.run()`` creates andthen *closes* a fresh loop on
every call, so any later use of the pool from a different loop raises
``RuntimeError: ... got Future attached to a different loop``.

Celery worker parents call sync reconciliation code on a timer. Those code
paths talk to PG through ``get_session()``, so they must reuse one loop for
the whole process lifetime instead of opening a new one per call.
"""

from __future__ import annotations

import asyncio
import os
import threading
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Any, TypeVar
from collections.abc import Coroutine

T = TypeVar("T")

_LOCK = threading.Lock()
_LOOP: asyncio.AbstractEventLoop | None = None
_THREAD: threading.Thread | None = None


def _after_fork() -> None:
    # Only the forking thread survives. Neither the inherited pump thread nor
    # a lock held by another parent thread can be used in the child.
    global _LOCK, _LOOP, _THREAD
    _LOCK = threading.Lock()
    _LOOP = None
    _THREAD = None


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)


def _ensure_loop() -> asyncio.AbstractEventLoop:
    """Return the process-wide loop, starting its pump thread on first use."""
    global _LOOP, _THREAD
    with _LOCK:
        if (
            _LOOP is not None
            and not _LOOP.is_closed()
            and _THREAD is not None
            and _THREAD.is_alive()
            and _LOOP.is_running()
        ):
            return _LOOP
        loop = asyncio.new_event_loop()
        ready = threading.Event()

        def _run() -> None:
            asyncio.set_event_loop(loop)
            loop.call_soon(ready.set)
            loop.run_forever()

        thread = threading.Thread(target=_run, name="qm-persistent-loop", daemon=True)
        thread.start()
        ready.wait()
        _LOOP, _THREAD = loop, thread
        return loop


def run(coro: Coroutine[Any, Any, T], timeout: float | None = 30.0) -> T:
    """Run ``coro`` on the shared loop and return its result.

    Equivalent to ``asyncio.run`` for callers that must not create a new loop.
    ``timeout`` guards against a hung DB call blocking the worker timer thread.
    """
    loop = _ensure_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    try:
        return future.result(timeout)
    except FutureTimeoutError:
        future.cancel()
        raise


def shutdown() -> None:
    """Stop the shared loop. Test/teardown helper; production never calls it."""
    global _LOOP, _THREAD
    with _LOCK:
        loop, thread = _LOOP, _THREAD
        _LOOP, _THREAD = None, None
    if loop is None or loop.is_closed():
        return
    loop.call_soon_threadsafe(loop.stop)
    if thread is not None and thread.is_alive():
        thread.join(timeout=5)
    loop.close()
