"""Owner-scoped, read-only RD-Agent workspace trajectory summaries.

Pickle payloads can contain objects produced by generated code. They are read
with the shared bounded unpickler, which rejects arbitrary globals and maps the
few required RD-Agent record types to inert shells.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.shared.owner_path import owner_segment, user_scoped_dir
from backend.shared.safe_pickle import load_pickle
from backend.shared.utc_datetime import to_utc_iso, utc_now

logger = logging.getLogger(__name__)
MAX_LOOPS = 100
MAX_FILES_PER_PHASE = 200
MAX_LOAD_FILES_PER_PHASE = 50
FLAG_TEXTS = {
    "no_selected": "本循环产出因子均未进入实现",
    "task_failed_here": "任务失败止步于本循环",
}
PHASES = (
    ("hypothesis", "假设生成", ("direct_exp_gen/hypothesis generation",)),
    ("experiment", "实验设计", ("direct_exp_gen/experiment generation",)),
    ("coding", "代码编写", ("coding/coder result", "coding")),
    ("running", "回测运行", ("running",)),
    ("feedback", "反馈总结", ("feedback/feedback", "feedback")),
)
PAYLOAD_GLOBS = {
    "experiment": ("direct_exp_gen/experiment generation/**/*.pkl",),
    "coding": ("coding/coder result/**/*.pkl",),
}


def _bounded_files(loop_dir: Path, patterns: tuple[str, ...]) -> tuple[list[Path], bool]:
    found: list[Path] = []
    overflow = False
    for pattern in patterns:
        try:
            for path in loop_dir.glob(pattern):
                if path.is_symlink() or not path.is_file():
                    continue
                found.append(path)
                if len(found) > MAX_LOAD_FILES_PER_PHASE:
                    overflow = True
                    return found[: MAX_LOAD_FILES_PER_PHASE + 1], overflow
        except OSError:
            continue
    return sorted(found), overflow


def _factor_name(item: Any) -> str | None:
    if isinstance(item, dict):
        name = item.get("factor_name") or item.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()[:256]
        target = item.get("target_task")
    else:
        name = getattr(item, "factor_name", None)
        if isinstance(name, str) and name.strip():
            return name.strip()[:256]
        target = getattr(item, "target_task", None)
    name = getattr(target, "factor_name", None) if target is not None else None
    if isinstance(name, str) and name.strip():
        return name.strip()[:256]
    return None


def extract_factor_names(value: Any) -> tuple[list[str], bool]:
    """Extract names only from known inert records or primitive dict/list data."""
    if value is None:
        return [], False
    if isinstance(value, (list, tuple)):
        items = value
    else:
        items = (value,)
    names: list[str] = []
    for item in items:
        name = _factor_name(item)
        if name and name not in names:
            names.append(name)
    if names:
        return names, True
    if isinstance(value, (list, tuple)) and not value:
        return [], True
    return [], False


def _phase_factor_names(loop_dir: Path, phase: str) -> tuple[list[str], bool]:
    files, overflow = _bounded_files(loop_dir, PAYLOAD_GLOBS.get(phase, ()))
    if overflow:
        return [], False
    names: list[str] = []
    ok = True
    for path in files:
        try:
            payload = load_pickle(path)
        except Exception as exc:  # noqa: BLE001 - attribution fails closed
            logger.warning("trajectory artifact attribution skipped (%s)", type(exc).__name__)
            ok = False
            continue
        extracted, recognized = extract_factor_names(payload)
        if not recognized:
            ok = False
        for name in extracted:
            if name not in names:
                names.append(name)
    return names, ok


def resolve_task_log_dir(
    *, root: Path | str, tenant_id: str, user_id: str, task_id: str
) -> Path:
    """Resolve only the current owner's canonical path; legacy flat paths are excluded."""
    task_name = owner_segment(task_id)
    root_path = Path(root)
    owner_dir = user_scoped_dir(root_path, tenant_id, user_id)
    candidate = owner_dir / task_name
    # A symlink in any owner/task segment could redirect this read into another
    # account's tree. The deployment root itself is configuration-owned.
    for path in (root_path / owner_segment(tenant_id), owner_dir, candidate):
        if path.is_symlink():
            raise ValueError("trajectory path contains a symlink")
    if not candidate.exists():
        raise FileNotFoundError("trajectory workspace does not exist")
    resolved_root = root_path.resolve()
    resolved = candidate.resolve(strict=True)
    if resolved != candidate.absolute() or not resolved.is_dir():
        raise ValueError("trajectory path is not a canonical directory")
    if not resolved.is_relative_to(resolved_root):
        raise ValueError("trajectory path escapes the configured root")
    return resolved


def _files_bounded(path: Path) -> list[Path]:
    files: list[Path] = []
    try:
        for item in path.rglob("*"):
            if item.is_symlink():
                continue
            if item.is_file():
                files.append(item)
                if len(files) >= MAX_FILES_PER_PHASE:
                    break
    except OSError:
        return []
    return files


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _time_info(folders: list[Path]) -> tuple[datetime | None, datetime | None, bool]:
    starts: list[datetime] = []
    ends: list[datetime] = []
    found = False
    for folder in folders:
        if folder.is_symlink() or not folder.is_dir():
            continue
        try:
            candidates = []
            for pattern in ("**/time_info/*.pkl", "**/time_info/**/*.pkl"):
                for path in folder.glob(pattern):
                    if path.is_symlink() or not path.is_file() or path in candidates:
                        continue
                    candidates.append(path)
                    if len(candidates) >= 20:
                        break
                if len(candidates) >= 20:
                    break
        except OSError:
            continue
        for path in candidates:
            try:
                value = load_pickle(path)
            except Exception as exc:  # noqa: BLE001 - timing is optional evidence
                logger.debug("trajectory time artifact skipped (%s)", type(exc).__name__)
                continue
            if not isinstance(value, dict):
                continue
            start = _parse_time(value.get("start_time"))
            end = _parse_time(value.get("end_time"))
            if start is not None or end is not None:
                found = True
            if start is not None:
                starts.append(start)
            if end is not None:
                ends.append(end)
    return (min(starts) if starts else None, max(ends) if ends else None, found)


def _node(loop_dir: Path, phase: str, label: str, paths: tuple[str, ...]) -> dict[str, Any]:
    folders = [loop_dir / path for path in paths]
    files: list[Path] = []
    for folder in folders:
        if folder.is_symlink() or not folder.is_dir():
            continue
        files.extend(_files_bounded(folder))
        if len(files) >= MAX_FILES_PER_PHASE:
            files = files[:MAX_FILES_PER_PHASE]
            break
    files = list(dict.fromkeys(files))
    mtimes: list[datetime] = []
    artifacts = [
        item
        for item in files
        if not (
            {"time_info", "token_cost"}
            & set(item.relative_to(loop_dir).parts[:-1])
        )
    ]
    for item in files:
        try:
            mtimes.append(datetime.fromtimestamp(item.stat().st_mtime, tz=timezone.utc))
        except OSError:
            continue
    start, end, has_time_info = _time_info(folders)
    if not has_time_info:
        start = min(mtimes) if mtimes else None
        end = max(mtimes) if mtimes else None
    duration = (
        max(0.0, (end - start).total_seconds())
        if start is not None and end is not None
        else None
    )
    return {
        "phase": phase,
        "label": label,
        "status": "observed" if files else "absent",
        "first_seen": to_utc_iso(min(mtimes)) if mtimes else None,
        "last_seen": to_utc_iso(max(mtimes)) if mtimes else None,
        "start_time": to_utc_iso(start) if start else None,
        "end_time": to_utc_iso(end) if end else None,
        "duration_seconds": round(duration, 3) if duration is not None else None,
        "time_source": "time_info" if has_time_info else "file_mtime",
        "artifact_count": len(artifacts),
        "count_truncated": len(artifacts) >= MAX_FILES_PER_PHASE,
    }


def scan_workspace(
    log_dir: Path | str,
    *,
    task: dict[str, Any],
    now: datetime | None = None,
    max_loops: int = MAX_LOOPS,
) -> dict[str, Any]:
    """Summarize bounded workspace evidence without opening generated payloads."""
    base = Path(log_dir)
    found: list[tuple[int, Path]] = []
    try:
        for item in base.iterdir():
            if item.is_symlink() or not item.is_dir() or not item.name.startswith("Loop_"):
                continue
            suffix = item.name[5:]
            if suffix.isdigit():
                found.append((int(suffix), item))
    except OSError:
        found = []
    found.sort(key=lambda entry: entry[0])
    kept = found[-max_loops:] if max_loops > 0 else []
    status = str(task.get("status") or "")
    loops = []
    attribution_ok = True
    for index, path in kept:
        proposed, proposed_ok = _phase_factor_names(path, "experiment")
        implemented, implemented_ok = _phase_factor_names(path, "coding")
        loop_attribution_ok = proposed_ok and implemented_ok
        attribution_ok = attribution_ok and loop_attribution_ok
        implemented_set = set(implemented)
        loops.append(
            {
                "loop": index,
                "phases": [
                    _node(path, phase, label, folders)
                    for phase, label, folders in PHASES
                ],
                "factors": (
                    [
                        {"name": name, "selected": name in implemented_set}
                        for name in proposed
                    ]
                    if loop_attribution_ok
                    else []
                ),
                "flags": (
                    ["no_selected"]
                    if proposed and implemented_ok and not implemented
                    else []
                ),
            }
        )
    if not attribution_ok:
        # Once any artifact is ambiguous, do not publish partial per-loop claims.
        for loop in loops:
            loop["factors"] = []
            loop["flags"] = []
    if status == "failed" and loops:
        loops[-1]["flags"].append("task_failed_here")
    return {
        "task_id": str(task.get("task_id") or ""),
        "direction": str(task.get("direction") or "") or None,
        "market": str(task.get("market") or "") or None,
        "status": status or None,
        "partial": status in {"pending", "queued", "running", "stopping"},
        "attribution": "loop" if attribution_ok else "task_level",
        "loops": loops,
        "flag_texts": FLAG_TEXTS,
        "truncated": len(found) > len(kept),
        "dropped_loops": max(0, len(found) - len(kept)),
        "generated_at": to_utc_iso(now or utc_now()),
    }


async def load_trajectory(
    *, task: dict[str, Any], root: Path | str, tenant_id: str, user_id: str
) -> dict[str, Any]:
    log_dir = resolve_task_log_dir(
        root=root,
        tenant_id=tenant_id,
        user_id=user_id,
        task_id=str(task.get("task_id") or ""),
    )
    summary = await asyncio.to_thread(scan_workspace, log_dir, task=task)
    from backend.services.engine.qlib_app.services.rd_agent_persistence import (
        RDAgentFactorPersistence,
    )

    rows = await RDAgentFactorPersistence().list_factors(
        tenant_id=tenant_id,
        user_id=user_id,
        task_id=str(task.get("task_id") or ""),
        limit=200,
    )
    by_name: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        name = str(row.get("factor_name") or "").strip()
        if name:
            by_name.setdefault(name, []).append(row)

    def outcome(name: str, selected: bool | None) -> dict[str, Any]:
        match = next(iter(by_name.get(name, ())), None)
        status = str(match.get("status") or "") if match else ""
        fate = {
            "completed": "factor_saved",
            "failed": "factor_failed",
            "pending": "pending",
        }.get(status)
        return {
            "name": name,
            "selected": selected,
            "outcome": fate,
            "reason": None,
        }

    if summary["attribution"] == "loop":
        for loop in summary["loops"]:
            loop["factors"] = [
                outcome(item["name"], item["selected"]) for item in loop["factors"]
            ]
    else:
        names: list[str] = []
        for loop in summary["loops"]:
            loop["factors"] = []
        for row in rows:
            name = str(row.get("factor_name") or "").strip()
            if name and name not in names:
                names.append(name)
        summary["factors"] = [outcome(name, None) for name in names]
    return summary


__all__ = ["load_trajectory", "resolve_task_log_dir", "scan_workspace"]
