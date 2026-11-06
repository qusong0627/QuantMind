"""Admission-time observations of the shared A-share mining data sources.

These facts are audit metadata, not a substitute for dataset checksums. The
scanner only visits fixed QuantDB paths and does not follow symlinks or accept
request-provided filesystem paths.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_KLINE_RELATIVE_PATH = "1_kline_data/daily_forward"
_SNAPSHOT_VERSION_FIELDS = (
    "market",
    "data_source",
    "provider_uri",
    "quantdb_root",
    "kline",
    "datasets",
    "errors",
    "reader",
    "storage",
)


class DataSnapshotDrift(ValueError):
    """The shared data source changed after a mining task was admitted."""


def data_snapshot_version(snapshot: dict[str, Any]) -> str:
    stable = {field: snapshot.get(field) for field in _SNAPSHOT_VERSION_FIELDS}
    return hashlib.sha256(
        json.dumps(stable, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def changed_snapshot_fields(
    expected: dict[str, Any], observed: dict[str, Any]
) -> list[str]:
    """Compare stable admission facts while ignoring capture timestamps."""
    if expected.get("status") != "observed" or observed.get("status") != "observed":
        raise DataSnapshotDrift("因子挖掘数据快照不完整，无法核验")
    return [
        field
        for field in _SNAPSHOT_VERSION_FIELDS
        if expected.get(field) != observed.get(field)
    ]


def verify_rd_agent_data_snapshot(
    expected: dict[str, Any],
    *,
    data_source: str | None,
    datasets: list[str] | tuple[str, ...] | None = None,
) -> None:
    """Fail closed if fixed QuantDB data metadata changed since admission."""
    observed = snapshot_rd_agent_data(
        data_source=data_source,
        provider_uri=str(expected.get("provider_uri") or ""),
        datasets=datasets,
    )
    changed = changed_snapshot_fields(expected, observed)
    if changed:
        raise DataSnapshotDrift(
            "A 股数据源在任务排队期间变化，请重新提交任务（变化字段："
            + ", ".join(changed)
            + ")"
        )


def _utc_mtime(timestamp: float) -> str:
    return (
        datetime.fromtimestamp(timestamp, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _partition_facts(path: Path) -> dict[str, Any] | None:
    """Return partition coverage and latest-file facts without scanning all files."""
    try:
        names = sorted(
            name[3:]
            for name in os.listdir(path)
            if name.startswith("dt=") and len(name) == 11 and name[3:].isdigit()
        )
        if not names:
            return None
        latest = path / f"dt={names[-1]}"
        latest_mtime = latest.stat().st_mtime
        manifest: list[tuple[str, int, int]] = []
        for entry in os.scandir(latest):
            if entry.name.endswith(".parquet") and entry.is_file(follow_symlinks=False):
                stat = entry.stat(follow_symlinks=False)
                manifest.append((entry.name, stat.st_size, stat.st_mtime_ns))
                latest_mtime = max(latest_mtime, stat.st_mtime)
        manifest.sort()
        latest_file_count = len(manifest)
        if latest_file_count == 0:
            return None
        manifest_json = json.dumps(
            manifest, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        return {
            "start": f"{names[0][:4]}-{names[0][4:6]}-{names[0][6:]}",
            "end": f"{names[-1][:4]}-{names[-1][4:6]}-{names[-1][6:]}",
            "latest_partition": names[-1],
            "latest_file_count": latest_file_count,
            "latest_mtime": _utc_mtime(latest_mtime),
            # Metadata manifest only; this is not a content checksum.
            "latest_manifest_sha256": hashlib.sha256(manifest_json).hexdigest(),
        }
    except OSError as exc:
        logger.warning("因子挖掘数据快照读取失败 %s: %s", path, exc)
        return None


def _file_inventory_facts(path: Path) -> dict[str, Any] | None:
    """Observe per-symbol layouts without 40,000 CFS stat calls at admission.

    This is explicitly an inventory, not a checksum of financial history. The
    reader enforces file/row budgets when actual requested fields are consumed.
    """
    try:
        if path.is_symlink():
            raise ValueError("Public dataset links are forbidden")
        names = sorted(name for name in os.listdir(path) if name.endswith(".parquet"))
        if not names:
            return None
        return {
            "layout": "per_symbol_or_flat",
            "status": "inventory_only",
            "file_count": len(names),
            "directory_mtime": _utc_mtime(path.stat().st_mtime),
            "names_sha256": hashlib.sha256("\n".join(names).encode()).hexdigest(),
            "content_version_verified": False,
        }
    except OSError:
        return None


def snapshot_rd_agent_data(
    *,
    data_source: str | None,
    provider_uri: str | None,
    datasets: list[str] | tuple[str, ...] | None = None,
    quantdb_dir: str | Path | None = None,
    storage_facts: dict | None = None,
) -> dict[str, Any]:
    """Capture best-effort data version facts at task admission.

    Missing storage is represented explicitly in ``errors``; it never invents
    a version and never prevents a task from being queued.
    """
    snapshot: dict[str, Any] = {
        "status": "observed",
        "captured_at": _utc_mtime(datetime.now(timezone.utc).timestamp()),
        "market": "a_share",
        "data_source": (data_source or "qlib_bin"),
        "provider_uri": provider_uri or "",
        "datasets": {},
        "errors": [],
    }
    from backend.shared.mining_cfs import shared_data_facts

    storage = storage_facts if storage_facts is not None else shared_data_facts()
    if storage is not None:
        snapshot["storage"] = storage
    reader_root = Path(__file__).parents[1] / "rd_agent"
    snapshot["reader"] = {
        name: hashlib.sha256((reader_root / name).read_bytes()).hexdigest()
        for name in ("quantdb_data.py", "mining_datasets.py", "rd_loop_wrapper.py", "panel_cache.py")
    }
    try:
        from backend.services.engine.rd_agent.rd_loop_wrapper import RDLoopWrapper

        quantdb_dir = quantdb_dir or RDLoopWrapper._resolve_quantdb_dir()
    except Exception as exc:  # noqa: BLE001 - an audit failure must not block admission
        logger.warning("QuantDB 数据路径解析失败: %s", exc)
        quantdb_dir = None
        snapshot["errors"].append("QuantDB 数据目录解析失败")
    if not quantdb_dir:
        snapshot["errors"].append("QuantDB 数据目录不可用")
        return snapshot

    root = Path(quantdb_dir)
    snapshot["quantdb_root"] = (
        "/data/quantdb" if storage and storage.get("kind") == "cfs" else str(root)
    )
    snapshot["kline"] = _partition_facts(root / _KLINE_RELATIVE_PATH)
    if snapshot["kline"] is None:
        snapshot["errors"].append("kline 无可用分区事实")
    from backend.services.engine.rd_agent.mining_datasets import (
        DATASET_REL_PATHS,
        resolve_datasets,
    )

    for key in resolve_datasets(datasets):
        relative = DATASET_REL_PATHS[key]
        facts = _partition_facts(root / relative) or _file_inventory_facts(
            root / relative
        )
        if facts is None:
            snapshot["errors"].append(f"{key} 无可用数据目录事实")
        else:
            snapshot["datasets"][key] = facts
    return snapshot
