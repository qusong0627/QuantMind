"""Common Docker boundary for untrusted user code.

Only trusted orchestration constructs mounts. Client image and filesystem input
must never be passed straight through to Docker.
"""

from __future__ import annotations

import os
import json
import shutil
from pathlib import Path, PurePosixPath

DEFAULT_USER_CODE_MAX_SOURCE_BYTES = 256 * 1024
MAX_USER_CODE_SOURCE_BYTES = 1024 * 1024
DEFAULT_USER_CODE_MAX_REQUEST_BYTES = 2 * 1024 * 1024
MAX_USER_CODE_REQUEST_BYTES = 4 * 1024 * 1024
MAX_RDAGENT_SANDBOX_REQUEST_BYTES = 88 * 1024 * 1024


def user_code_source_limit_bytes() -> int:
    """Return a configurable source limit with a hard 1 MiB ceiling."""
    try:
        configured = int(
            os.getenv(
                "USER_CODE_MAX_SOURCE_BYTES",
                str(DEFAULT_USER_CODE_MAX_SOURCE_BYTES),
            )
        )
    except (TypeError, ValueError):
        configured = DEFAULT_USER_CODE_MAX_SOURCE_BYTES
    if configured <= 0:
        configured = DEFAULT_USER_CODE_MAX_SOURCE_BYTES
    return min(configured, MAX_USER_CODE_SOURCE_BYTES)


def validate_user_code_source(code: str) -> str:
    """Reject oversized user source before staging, queuing or parsing it."""
    if not isinstance(code, str):
        raise ValueError("User code must be text")
    limit = user_code_source_limit_bytes()
    if len(code.encode("utf-8")) > limit:
        raise ValueError(f"User code exceeds the {limit}-byte source limit")
    return code


def user_code_request_limit_bytes() -> int:
    """Bound the full JSON envelope before FastAPI parses user-code requests."""
    try:
        configured = int(
            os.getenv(
                "USER_CODE_MAX_REQUEST_BYTES",
                str(DEFAULT_USER_CODE_MAX_REQUEST_BYTES),
            )
        )
    except (TypeError, ValueError):
        configured = DEFAULT_USER_CODE_MAX_REQUEST_BYTES
    if configured <= 0:
        configured = DEFAULT_USER_CODE_MAX_REQUEST_BYTES
    return min(configured, MAX_USER_CODE_REQUEST_BYTES)


def user_code_request_body_limits() -> dict[tuple[str, str], int]:
    """Request caps for user-code and attachment routes on engine ASGI apps."""
    from backend.shared.upload_limits import ai_strategy_max_upload_bytes

    code_limit = user_code_request_limit_bytes()
    upload_limit = ai_strategy_max_upload_bytes() + 128 * 1024
    code_paths = (
        "/api/v1/ai-ide/execute/check-syntax",
        "/api/v1/ai-ide/execute/start",
        "/api/v1/ai-ide/execute/run-tmp",
        "/api/v1/ai-ide/strategy-lab/run",
        "/api/v1/ai-ide/strategy-lab/run/async",
    )
    limits = {("POST", path): code_limit for path in code_paths}
    limits[("POST", "/api/v1/files/upload")] = upload_limit
    limits[("POST", "/api/v1/internal/alpha-mining-sandbox/run")] = (
        MAX_RDAGENT_SANDBOX_REQUEST_BYTES
    )
    return limits


def legacy_engine_exec_enabled() -> bool:
    """Legacy host execution is permanently disabled pending sandbox migration."""
    return False


def allowed_image(image: str | None, default: str) -> str:
    selected = str(image or default).strip()
    allowed = {
        default,
        *filter(
            None,
            (
                part.strip()
                for part in os.getenv("USER_CODE_ALLOWED_IMAGES", "").split(",")
            ),
        ),
    }
    if selected not in allowed:
        raise ValueError("Execution image is not allowed")
    return selected


def isolated_options() -> dict:
    """Bound each process even if it escapes AST restrictions."""
    return {
        "user": "65534:65534",
        "read_only": True,
        "network_mode": "none",
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "pids_limit": 128,
        "mem_limit": os.getenv("USER_CODE_MEMORY_LIMIT", "2g"),
        "cpu_period": 100000,
        "cpu_quota": 100000,
        "tmpfs": {
            "/tmp": "rw,noexec,nosuid,size=128m,mode=1777",
            "/output": "rw,noexec,nosuid,size=16m,mode=0700,uid=65534,gid=65534",
        },
        "working_dir": "/tmp",
    }


def validate_image(image) -> None:
    labels = (image.attrs.get("Config") or {}).get("Labels") or {}
    if labels.get("quantmind.user_code_sandbox") != "true":
        raise ValueError("Execution requires a sanitized sandbox image")
    for entry in (image.attrs.get("Config") or {}).get("Env") or []:
        key, _, value = entry.partition("=")
        if value and any(
            marker in key.upper()
            for marker in (
                "PASSWORD",
                "SECRET",
                "TOKEN",
                "API_KEY",
                "DATABASE_URL",
                "CREDENTIAL",
            )
        ):
            raise ValueError(
                "Execution image contains a sensitive environment variable"
            )


def checked_input(path: str | Path, root: str | Path) -> Path:
    """Require an existing input within its trusted root, without symlinks."""
    candidate, boundary = Path(path).absolute(), Path(root).absolute()
    if not candidate.is_relative_to(boundary):
        raise ValueError("Input is outside the authorized directory")
    for part in (candidate, *candidate.parents):
        if part.is_symlink():
            raise ValueError("Symlink inputs are forbidden")
        if part == boundary:
            break
    resolved = candidate.resolve(strict=True)
    if not resolved.is_relative_to(boundary.resolve(strict=True)):
        raise ValueError("Input escaped the authorized directory")
    return resolved


def host_bind(path: str | Path) -> str:
    """Translate approved container paths to the Docker host bind layout."""
    candidate = PurePosixPath(str(path))
    if not candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError("Input has no configured host bind mapping")
    host_root = PurePosixPath(os.getenv("HOST_PROJECT_PATH", "/opt/quantmind"))
    shared_root = os.getenv("HOST_SHARED_DATA_ROOT", "").strip()
    if shared_root:
        for public_root in (
            PurePosixPath("/data/quantdb"),
            PurePosixPath("/data/qlib"),
        ):
            if candidate.is_relative_to(public_root):
                return str(PurePosixPath(shared_root) / candidate.relative_to("/data"))
    for container_root, host_subdir in (
        (PurePosixPath("/app"), ""),
        (PurePosixPath("/data"), "data"),
    ):
        if candidate.is_relative_to(container_root):
            return str(host_root / host_subdir / candidate.relative_to(container_root))
    raise ValueError("Input has no configured host bind mapping")


def public_qlib_mounts() -> dict:
    """Share only public Qlib layout, excluding model predictions and users."""
    from backend.shared.qlib_paths import resolve_qlib_provider_uri

    root = Path(resolve_qlib_provider_uri("CN"))
    mounts = {}
    for name in ("calendars", "features"):
        source = checked_input(root / name, root)
        mounts[host_bind(source)] = {"bind": f"/sandbox/qlib/{name}", "mode": "ro"}
    from backend.shared.qlib_public import public_instrument_files

    for filename in public_instrument_files().values():
        path = root / "instruments" / filename
        if path.exists():
            source = checked_input(path, root)
            mounts[host_bind(source)] = {
                "bind": f"/sandbox/qlib/instruments/{filename}",
                "mode": "ro",
            }
    return mounts


def public_factor_panel_input(path: str | Path) -> Path:
    """Resolve one known public Alpha factor panel file without following links."""
    candidate = Path(path).absolute()
    roots = (
        Path("/data/quantdb/.panel_cache"),
        Path("/data/mining_cache/panels"),
        Path("/app/alphaagent/scenarios/qlib/experiment/factor_data_template"),
        Path("/app/db/cn_data"),
    )
    for root in roots:
        if candidate.is_relative_to(root):
            resolved = checked_input(candidate, root)
            if not resolved.is_file():
                raise ValueError("Factor panel input must be a regular file")
            if resolved.suffix != ".parquet":
                raise ValueError("Factor panel input must be a parquet file")
            if resolved.stat().st_size > 2 * 1024 * 1024 * 1024:
                raise ValueError("Factor panel input exceeds the 2 GiB limit")
            return resolved
    raise ValueError("Factor panel input is outside the public data allowlist")


def public_quantdb_mounts(datasets) -> dict:
    """Mount registered public leaves only, never the repository or user data."""
    from backend.services.engine.rd_agent.mining_datasets import (
        DATASET_REL_PATHS,
        resolve_datasets,
    )
    from backend.services.engine.rd_agent.rd_loop_wrapper import RDLoopWrapper

    source_root = RDLoopWrapper._resolve_quantdb_dir()
    if not source_root:
        raise ValueError("Public QuantDB root is unavailable")
    root = Path(source_root).absolute()
    if root not in {Path("/data/quantdb"), Path("/app/data/quantdb")}:
        raise ValueError("QuantDB root is outside the public sandbox allowlist")
    relatives = [DATASET_REL_PATHS[key] for key in resolve_datasets(datasets)]
    relatives.append("2_base_sector/index_weights")
    mounts = {}
    for relative in relatives:
        source = (
            checked_input(root / relative, root) if (root / relative).exists() else None
        )
        if source is not None:
            if not source.is_dir():
                raise ValueError("Public QuantDB dataset must be a directory")
            mounts[host_bind(source)] = {
                "bind": f"/sandbox/quantdb/{relative}",
                "mode": "ro",
            }
    return mounts


def _sandbox_manifest(backend_root: Path) -> list[str]:
    """新 SDK 清单 +（默认合并）旧链路兼容清单。

    `SANDBOX_MOUNT_LEGACY_QLIB=false` 时只挂新清单元数据；默认合并以保持旧行为。
    """
    import os

    manifest = json.loads(
        (backend_root / "shared" / "user_code_sources.json").read_text(encoding="utf-8")
    )
    legacy_path = backend_root / "shared" / "legacy_qlib_sources.json"
    disabled = os.getenv("SANDBOX_MOUNT_LEGACY_QLIB", "true").strip().lower() in {
        "false",
        "0",
        "no",
        "off",
    }
    if not disabled and legacy_path.exists():
        manifest = manifest + json.loads(legacy_path.read_text(encoding="utf-8"))
    return manifest


def prepare_source_bundle(destination: Path, *, include_legacy: bool = True) -> Path:
    """Copy reviewed code, excluding live files, credentials and runtime logs."""
    backend_root = Path(__file__).resolve().parents[1]
    manifest = (
        _sandbox_manifest(backend_root)
        if include_legacy
        else json.loads(
            (backend_root / "shared" / "user_code_sources.json").read_text(
                encoding="utf-8"
            )
        )
    )
    bundle = destination / "backend"
    bundle.mkdir(parents=True, exist_ok=False)
    for relative in manifest:
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts or path.suffix != ".py":
            raise ValueError("Invalid sandbox source manifest")
        source = checked_input(backend_root / path, backend_root)
        target = bundle / path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        target.chmod(0o644)
    return bundle


def remove_task_directory(task: Path, owner_root: Path, run_id: str) -> None:
    if not task.exists():
        return
    resolved = checked_input(task, owner_root)
    if resolved.parent != owner_root.resolve(strict=True) or resolved.name != run_id:
        raise ValueError("Cleanup target is not this owner's task directory")
    shutil.rmtree(resolved)
