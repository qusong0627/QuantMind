"""Read-only Qlib daily arrays, loaded in the Celery parent before prefork."""
# OSS port: 与企业版逐字一致。唯一依赖 backend.shared.qlib_paths（OSS 侧存在）。
# 由 celery_config 经 worker_init 信号接入（见文末 install_worker_preload）。

import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)
DEFAULT_FIELDS = (
    "open",
    "close",
    "high",
    "low",
    "volume",
    "amount",
    "factor",
    "change",
    "vwap",
    "prop_net_asset_value",
)


class DailyFeatureCache:
    def __init__(self):
        self.root = None
        self.version = None
        self.arrays = {}
        self.instruments = set()
        self.fields = set()
        self.generation = 0
        self.bytes = 0
        self._lock = threading.Lock()

    @staticmethod
    def _version(root):
        return tuple(
            (p.stat().st_mtime_ns, p.stat().st_size)
            for p in (root / "calendars/day.txt", root / "instruments/all.txt")
        )

    def preload(
        self, root, *, fields=DEFAULT_FIELDS, max_bytes=768 * 1024**2, workers=8
    ):
        root = Path(root).expanduser().resolve()
        with self._lock:
            version = self._version(root)
            if self.root == root and self.version == version:
                return
            started = time.monotonic()
            instruments = {
                line.split()[0].lower()
                for line in (root / "instruments/all.txt").read_text().splitlines()
                if line.strip()
            }
            if not instruments or any(
                not re.fullmatch(r"(?:sh|sz|bj)\d{6}", code) for code in instruments
            ):
                raise ValueError("Invalid Qlib instrument list for memory preload")
            fields = set(fields)
            if any(not re.fullmatch(r"[a-z_][a-z_0-9]*", f) for f in fields):
                raise ValueError("Invalid Qlib field for memory preload")
            arrays = {}
            total = 0

            def read(pair):
                code, field = pair
                try:
                    raw = (root / "features" / code / f"{field}.day.bin").read_bytes()
                except FileNotFoundError:
                    return pair, None
                if len(raw) < 4 or len(raw) % 4:
                    raise ValueError(f"Invalid Qlib feature file: {code}/{field}")
                array = np.frombuffer(raw, dtype="<f4")
                if (
                    not np.isfinite(array[0])
                    or array[0] < 0
                    or array[0] != int(array[0])
                ):
                    raise ValueError(f"Invalid Qlib start index: {code}/{field}")
                return pair, array

            # Bounded batches: Executor.map on the whole universe can retain all
            # completed futures and exceed the budget before results are consumed.
            pairs = iter(
                (code, field)
                for code in sorted(instruments)
                for field in sorted(fields)
            )
            with ThreadPoolExecutor(max_workers=workers) as pool:
                while True:
                    from itertools import islice

                    batch = list(islice(pairs, workers * 2))
                    if not batch:
                        break
                    for pair, array in pool.map(read, batch):
                        if array is not None:
                            total += array.nbytes
                            if total > max_bytes:
                                raise MemoryError("Qlib preload exceeds memory budget")
                            arrays[pair] = array
            if version != self._version(root):
                raise RuntimeError("Qlib data changed during memory preload")
            self.root, self.version = root, version
            self.arrays, self.instruments, self.fields = arrays, instruments, fields
            self.bytes = total
            self.generation += 1
            logger.info(
                "Qlib memory preload ready: root=%s instruments=%d files=%d "
                "bytes=%d elapsed=%.2fs generation=%d",
                root,
                len(instruments),
                len(arrays),
                total,
                time.monotonic() - started,
                self.generation,
            )

    def feature(self, instrument, field, start_index, end_index, freq):
        code, field = str(instrument).lower(), str(field).lstrip("$").lower()
        if freq != "day" or code not in self.instruments or field not in self.fields:
            return None  # Unsupported fields/frequencies retain the disk provider.
        array = self.arrays.get((code, field))
        if array is None:
            return pd.Series(dtype=np.float32)
        offset = int(array[0])
        start = max(start_index, offset)
        stop = min(end_index + 1, offset + len(array) - 1)
        if start >= stop:
            return pd.Series(dtype=np.float32)
        return pd.Series(
            array[1 + start - offset : 1 + stop - offset],
            index=pd.RangeIndex(start, stop),
            copy=False,
        )


CACHE = DailyFeatureCache()


def _preload(root):
    CACHE.preload(
        root,
        max_bytes=int(os.getenv("QLIB_PRELOAD_MAX_MB", "768")) * 1024**2,
        workers=max(1, min(32, int(os.getenv("QLIB_PRELOAD_THREADS", "8")))),
    )


def provider_options(root):
    """Refresh at task boundaries; each running task uses one stable snapshot."""
    if CACHE.root != Path(root).expanduser().resolve():
        return {}, CACHE.generation
    try:
        _preload(root)
    except Exception:
        # Never retain stale data after a failed refresh.
        CACHE.arrays = {}
        CACHE.instruments = set()
        CACHE.root = None
        CACHE.generation += 1
        logger.exception("Qlib memory refresh failed; using disk provider")
        return {}, CACHE.generation
    return {
        "feature_provider": {
            "class": "MemoryFeatureProvider",
            "module_path": "backend.services.engine.qlib_app.memory_feature_provider",
        }
    }, CACHE.generation


def preload_backtest_worker(sender=None, **kwargs):
    if os.getenv("QLIB_BACKTEST_PRELOAD", "true").lower() not in {"true", "1", "yes"}:
        return
    queues = (
        getattr(getattr(sender.app, "amqp", None), "queues", None) if sender else None
    )
    if os.getenv("CELERY_QUEUE_BACKTEST", "qlib_backtest") not in getattr(
        queues, "consume_from", {}
    ):
        return
    try:
        from backend.shared.qlib_paths import resolve_qlib_provider_uri

        _preload(resolve_qlib_provider_uri("CN"))
    except Exception:
        logger.exception("Qlib startup preload failed; worker will use disk provider")


def install_worker_preload():
    from celery.signals import worker_init

    worker_init.connect(preload_backtest_worker, weak=False)
