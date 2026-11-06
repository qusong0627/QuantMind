"""Restricted child process entry point for trusted statistical mining jobs.

The Celery consumer launches this fixed module with a JSON request. It does not
accept Python expressions, import paths, or shell fragments from task parameters.
"""

from __future__ import annotations

import json
import hashlib
import math
import os
import signal
import sys
import tempfile
import time
from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import Any


_STAGE_PROGRESS = {
    "data": 10,
    "filter": 25,
    "evaluate": 35,
    "select": 80,
}


def _sanitize(value: Any) -> Any:
    """Convert engine output to strict, bounded JSON-compatible values."""
    if is_dataclass(value) and not isinstance(value, type):
        return {
            item.name: _sanitize(getattr(value, item.name)) for item in fields(value)
        }
    if isinstance(value, dict):
        return {str(key): _sanitize(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_sanitize(item) for item in value]
    if hasattr(value, "item") and callable(value.item):
        try:
            return _sanitize(value.item())
        except (ValueError, TypeError):
            pass
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if hasattr(value, "isoformat") and callable(value.isoformat):
        return value.isoformat()
    raise TypeError(f"Unsupported statistical result value: {type(value).__name__}")


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    if len(payload.encode("utf-8")) > 4 * 1024 * 1024:
        raise ValueError("Statistical worker artifact exceeds 4 MiB")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _apply_resource_limits() -> None:
    if os.name != "posix":
        raise RuntimeError("Statistical subprocess requires POSIX resource limits")
    import resource

    cpu_seconds = int(os.environ.pop("QM_STAT_WORKER_CPU_SECONDS", "14400"))
    address_space = int(
        os.environ.pop("QM_STAT_WORKER_MEMORY_BYTES", str(24 * 1024**3))
    )
    if cpu_seconds < 60 or address_space < 512 * 1024**2:
        raise ValueError("Statistical worker resource limits are below safe minimums")
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 60))
    resource.setrlimit(resource.RLIMIT_AS, (address_space, address_space))
    resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
    parent_pid = os.getppid()
    try:
        import ctypes

        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        if libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "prctl(PR_SET_PDEATHSIG) failed")
    except (OSError, AttributeError) as exc:
        raise RuntimeError(
            "Statistical child cannot monitor its Celery parent"
        ) from exc
    if os.getppid() != parent_pid:
        raise RuntimeError("Statistical Celery parent exited during child startup")
    os.environ["OMP_NUM_THREADS"] = "1"
    os.environ["OPENBLAS_NUM_THREADS"] = "1"
    os.environ["MKL_NUM_THREADS"] = "1"
    os.environ["NUMEXPR_NUM_THREADS"] = "1"


def _prepare_snapshot(
    request: dict[str, Any], request_path: Path, progress_path: Path
) -> dict[str, Any]:
    import pandas as pd

    from backend.services.engine.alpha_agent.statistical import candidates, panel

    params = request["params"]
    snapshot = request["data_snapshot"]
    qlib_dir = snapshot.get("qlib_provider_uri")
    if not isinstance(qlib_dir, str) or not qlib_dir.startswith("/"):
        raise ValueError("Statistical snapshot preparation requires a public Qlib path")
    mining_start, mining_end = params.get("mining_start"), params.get("mining_end")
    valid_start, valid_end = params.get("valid_start"), params.get("valid_end")
    if not all(
        isinstance(value, str) and value for value in (mining_start, mining_end)
    ):
        raise ValueError("Statistical mining date range is required")
    anchor = min(pd.Timestamp(value) for value in (mining_start, valid_start) if value)
    fetch_end = max(pd.Timestamp(value) for value in (mining_end, valid_end) if value)
    universe = str(params.get("universe") or "all_a")
    symbols = snapshot.get("symbols")
    config = _statistical_config(params)
    warmup = candidates.MAX_MIN_HISTORY + (
        config.composite_z_window if config.composite_enabled else 0
    )
    _atomic_json(
        progress_path,
        {"stage": "snapshot", "detail": "冻结行情与元数据输入", "progress_pct": 5},
    )
    from backend.services.engine.alpha_agent.quantdb_price_panel import (
        load_factor_price_panel,
    )

    if snapshot.get("price_source") == "quantdb_daily_forward":
        frozen_symbols = symbols if symbols is not None else panel.qlib_instruments(
            universe, qlib_dir=qlib_dir
        )
        prices = load_factor_price_panel(
            frozen_symbols, anchor.strftime("%Y-%m-%d"),
            fetch_end.strftime("%Y-%m-%d"), warmup_bars=warmup,
        )
        price_panel = panel.normalize_panel(prices)
    else:
        calendar = panel.qlib_calendar("1900-01-01", fetch_end.strftime("%Y-%m-%d"), qlib_dir=qlib_dir)
        price_panel = panel.load_price_panel(
            anchor.strftime("%Y-%m-%d"), fetch_end.strftime("%Y-%m-%d"),
            market=universe, symbols=symbols, warmup_bars=warmup,
            qlib_dir=qlib_dir, calendar=calendar,
        )
    metadata = panel.load_symbol_metadata()
    task_dir = request_path.resolve().parent
    panel_path = task_dir / "statistical_panel.parquet"
    metadata_path = task_dir / "statistical_metadata.parquet"
    for target, frame in ((panel_path, price_panel), (metadata_path, metadata)):
        fd, temporary = tempfile.mkstemp(
            prefix=f".{target.stem}.", suffix=".parquet", dir=task_dir
        )
        os.close(fd)
        temp_path = Path(temporary)
        try:
            frame.to_parquet(
                temp_path, engine="pyarrow", compression="zstd", index=True
            )
            if temp_path.stat().st_size > (
                2 * 1024**3 if target == panel_path else 64 * 1024**2
            ):
                raise ValueError("Prepared statistical data exceeds its storage limit")
            os.replace(temp_path, target)
            if os.name == "posix":
                target.chmod(0o600)
        finally:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass
    symbols_used = sorted(set(price_panel.index.get_level_values("symbol").astype(str)))
    symbol_hash = hashlib.sha256(
        json.dumps(symbols_used, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    return {
        "status": "prepared",
        "qlib_provider_uri": qlib_dir,
        "price_source": snapshot.get("price_source", "qlib"),
        "panel_sha256": digest_file(panel_path, 2 * 1024**3),
        "metadata_sha256": digest_file(metadata_path, 64 * 1024**2),
        "symbols_sha256": symbol_hash,
        "n_symbols": len(symbols_used),
        "panel_rows": len(price_panel),
        "fetch_start": str(
            price_panel.index.get_level_values("trade_date").min().date()
        ),
        "fetch_end": fetch_end.strftime("%Y-%m-%d"),
        "mining_start": mining_start,
        "mining_end": mining_end,
        "valid_start": valid_start,
        "valid_end": valid_end,
    }


#: Task-envelope fields the launcher records alongside the kernel parameters.
#: They describe the shared ``alpha_mining_tasks`` row (queue/serialization and
#: display) and must not be mistaken for statistical kernel options.
_TASK_ENVELOPE_PARAMS = frozenset(
    {
        "algorithm",
        "market",
        "universe",
        "mining_start",
        "mining_end",
        "valid_start",
        "valid_end",
        "data_source",
        "loop_n",
        "direction",
        "auto_materialize",
    }
)


def validate_task_params(params: dict[str, Any]) -> None:
    """Reject unknown keys before any data is read.

    ``start_statistical_mining`` freezes both the kernel options and the task
    envelope into ``params_json``. The worker must accept that exact envelope;
    an unknown key is still a hard error (it would silently change the run).
    """
    from .configuration import STATISTICAL_PARAMETER_KEYS

    if params.keys() - STATISTICAL_PARAMETER_KEYS - _TASK_ENVELOPE_PARAMS:
        raise ValueError("Unsupported statistical task parameters")


def _statistical_config(params: dict[str, Any]):
    from .configuration import STATISTICAL_PARAMETER_KEYS, resolve_statistical_config

    return resolve_statistical_config(
        {
            key: value
            for key, value in params.items()
            if key in STATISTICAL_PARAMETER_KEYS
        }
    )


def digest_file(path: Path, max_bytes: int) -> str:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > max_bytes:
        raise ValueError("Statistical input artifact is missing or oversized")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def run(request_path: Path, result_path: Path, progress_path: Path) -> None:
    _apply_resource_limits()
    request = json.loads(request_path.read_text(encoding="utf-8"))
    if not isinstance(request, dict) or request.get("algorithm") != "statistical":
        raise ValueError("Invalid statistical worker request")
    params = request.get("params")
    snapshot = request.get("data_snapshot")
    panel_path = request.get("panel_path")
    metadata_path = request.get("metadata_path")
    if not isinstance(params, dict) or not isinstance(snapshot, dict):
        raise ValueError("Statistical task parameters and data snapshot are required")
    task_dir = request_path.resolve().parent
    prepared_path = task_dir / "statistical_prepared.json"
    sealed_path = task_dir / "statistical_sealed.json"
    if snapshot.get("status") == "preparing":
        manifest = _prepare_snapshot(request, request_path, progress_path)
        _atomic_json(prepared_path, manifest)
        deadline = time.monotonic() + 300
        while not sealed_path.is_file() and time.monotonic() < deadline:
            if os.getppid() == 1:
                raise RuntimeError(
                    "Statistical task controller exited before snapshot sealing"
                )
            time.sleep(0.5)
        if not sealed_path.is_file():
            raise TimeoutError(
                "Statistical data snapshot was not sealed by the controller"
            )
        sealed = json.loads(sealed_path.read_text(encoding="utf-8"))
        if (
            not isinstance(sealed, dict)
            or sealed.get("status") != "frozen"
            or sealed.get("panel_sha256") != manifest["panel_sha256"]
            or sealed.get("metadata_sha256") != manifest["metadata_sha256"]
        ):
            raise ValueError("Controller sealed a different statistical data snapshot")
        snapshot = sealed
    elif snapshot.get("status") != "frozen":
        raise ValueError(
            "Statistical worker requires a preparing or frozen data snapshot"
        )
    required = ("mining_start", "mining_end")
    if any(not isinstance(params.get(key), str) or not params[key] for key in required):
        raise ValueError("Statistical mining date range is required")

    from backend.services.engine.alpha_agent.statistical.engine import (
        run_statistical_mining,
    )
    import pandas as pd

    validate_task_params(params)
    config = _statistical_config(params)

    qlib_dir = snapshot.get("qlib_provider_uri")
    if not isinstance(qlib_dir, str) or not qlib_dir.startswith("/"):
        raise ValueError("Frozen Qlib provider path is required")
    panel_file = Path(panel_path).resolve() if isinstance(panel_path, str) else None
    metadata_file = (
        Path(metadata_path).resolve() if isinstance(metadata_path, str) else None
    )
    if (
        panel_file is None
        or metadata_file is None
        or panel_file.parent != task_dir
        or metadata_file.parent != task_dir
        or panel_file.name != "statistical_panel.parquet"
        or metadata_file.name != "statistical_metadata.parquet"
    ):
        raise ValueError("Statistical input artifacts are outside the task directory")

    if digest_file(panel_file, 2 * 1024**3) != snapshot.get("panel_sha256"):
        raise ValueError("Frozen statistical price panel hash does not match")
    if digest_file(metadata_file, 64 * 1024**2) != snapshot.get("metadata_sha256"):
        raise ValueError("Frozen statistical metadata hash does not match")
    universe = params.get("universe", "all_a")
    if not isinstance(universe, str) or not universe.strip():
        raise ValueError("A valid public universe is required")
    symbols = snapshot.get("symbols")
    if symbols is not None and (
        not isinstance(symbols, list)
        or len(symbols) > 20000
        or any(not isinstance(item, str) or not item for item in symbols)
    ):
        raise ValueError("Frozen instrument list is invalid")

    def report(stage: str, detail: str) -> None:
        _atomic_json(
            progress_path,
            {
                "stage": str(stage)[:64],
                "detail": str(detail)[:1000],
                "progress_pct": _STAGE_PROGRESS.get(str(stage), 5),
            },
        )

    from .variant_ledger import frozen_cooled_keys

    cooled_keys = frozen_cooled_keys(snapshot)
    result = run_statistical_mining(
        params["mining_start"],
        params["mining_end"],
        valid_start=params.get("valid_start"),
        valid_end=params.get("valid_end"),
        cfg=config,
        price_panel=pd.read_parquet(panel_file),
        metadata=pd.read_parquet(metadata_file),
        qlib_dir=qlib_dir,
        universe=universe,
        symbols=symbols,
        progress=report,
        cooled_keys=cooled_keys,
    )
    payload = _sanitize(result)
    if not isinstance(payload, dict):
        raise TypeError("Statistical engine returned an invalid result")
    if snapshot.get("variant_memory"):
        payload["variant_context_id"] = snapshot["variant_memory"]["context_id"]
    _atomic_json(result_path, payload)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 3:
        raise SystemExit("expected request, result, and progress file paths")
    try:
        run(*(Path(item) for item in argv))
    except Exception as exc:  # noqa: BLE001
        print(
            f"statistical worker failed: {type(exc).__name__}: {exc}", file=sys.stderr
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
