"""Bounded, inert pickle reader for untrusted RD-Agent workspace artifacts."""

from __future__ import annotations

import builtins
import datetime
import io
import os
import pathlib
import pickle
from pathlib import Path
from typing import Any

MAX_PICKLE_BYTES = 32 * 1024 * 1024
_SAFE_BUILTINS = frozenset(
    {
        "NoneType", "bool", "bytearray", "bytes", "complex", "dict", "float",
        "frozenset", "int", "list", "object", "range", "set", "slice", "str",
        "tuple",
    }
)
_INERT_RECORDS = {
    ("rdagent.components.coder.factor_coder.factor", "FactorTask"),
    ("rdagent.components.coder.factor_coder.factor", "FactorFBWorkspace"),
    ("rdagent.core.experiment", "RunningInfo"),
    ("rdagent.core.proposal", "HypothesisFeedback"),
    ("target_task", "FactorTask"),
}
_RECORD_TYPES: dict[tuple[str, str], type] = {}


class PayloadTooLargeError(ValueError):
    pass


def _safe_global(module: str, name: str) -> Any:
    if module == "builtins" and name in _SAFE_BUILTINS:
        return getattr(builtins, name)
    if (module, name) in _INERT_RECORDS:
        # Never import RD-Agent or task-local modules while reading an artifact.
        # These inert shells only receive pickle state; their methods never run.
        return _RECORD_TYPES.setdefault(
            (module, name), type(name, (), {"__module__": __name__})
        )
    if module == "datetime" and name in {"datetime", "date", "time", "timedelta", "timezone"}:
        return getattr(datetime, name)
    if module == "pathlib" and name == "PosixPath" and hasattr(pathlib, "PosixPath"):
        return pathlib.PosixPath
    raise pickle.UnpicklingError(f"pickle global is not allowed: {module}.{name}")


class RestrictedUnpickler(pickle.Unpickler):
    def find_class(self, module: str, name: str) -> Any:
        return _safe_global(module, name)


def load_pickle(path: str | Path) -> Any:
    """Read one bounded payload and allow only inert data classes and primitives."""
    source = Path(path)
    if source.is_symlink():
        raise pickle.UnpicklingError("refusing symlink pickle artifact")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(source, flags)
    try:
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(MAX_PICKLE_BYTES + 1)
    finally:
        os.close(fd)
    if len(raw) > MAX_PICKLE_BYTES:
        raise PayloadTooLargeError("pickle artifact exceeds the configured size limit")
    return RestrictedUnpickler(io.BytesIO(raw)).load()


__all__ = ["MAX_PICKLE_BYTES", "PayloadTooLargeError", "RestrictedUnpickler", "load_pickle"]
