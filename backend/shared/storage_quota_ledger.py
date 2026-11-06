"""Durable, atomic storage quota reservations for owner-scoped files."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import text

from backend.shared.storage_quota import (
    DEFAULT_QUOTA_GB,
    StorageQuotaExceeded,
    StorageUsageIncomplete,
)


class StorageQuotaUnavailable(RuntimeError):
    """Quota admission is closed or its durable ledger cannot be updated."""


HUB_IMPORT_LOCK_PREFIX = "hub_model_import_locks/"
HUB_IMPORT_LOCK_STALE_AFTER_SECONDS = 600


@asynccontextmanager
async def _quota_session(session=None):
    if session is not None:
        yield session
        return
    from backend.shared.database_manager_v2 import get_session

    async with get_session() as managed_session:
        yield managed_session


@contextmanager
def _local_quota_session(session=None):
    """Use a caller transaction or open one for synchronous ledger operations."""
    if session is not None:
        yield session
        return
    from backend.shared.database_pool import get_db

    with get_db() as managed_session, managed_session.begin():
        yield managed_session


def _quota_bytes(quota_gb: float | None = None) -> int:
    if quota_gb is None:
        try:
            quota_gb = float(os.getenv("USER_STORAGE_QUOTA_GB", str(DEFAULT_QUOTA_GB)))
        except (TypeError, ValueError):
            quota_gb = DEFAULT_QUOTA_GB
    if quota_gb < 0:
        return 2**63 - 1
    return int(quota_gb * (1024**3))


def _owner_hash(tenant_id: str, user_id: str) -> str:
    tenant = str(tenant_id or "").strip()
    user = str(user_id or "").strip()
    return hashlib.sha256(f"{tenant}\0{user}".encode()).hexdigest()


def _safe_path_component(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in value)


def tenant_quota_user_id(tenant_id: str) -> str:
    """Stable synthetic ledger key for tenant-shared storage without a user owner."""
    digest = hashlib.sha256(str(tenant_id or "").strip().encode()).hexdigest()[:40]
    return f"__tenant__{digest}"


def report_archive_resource_key(
    tenant_id: str, user_id: str, relative_path: str | Path
) -> str:
    """Stable quota key for an owner-scoped report archive file."""
    from backend.shared.owner_path import owner_segment

    tenant = owner_segment(tenant_id)
    user = owner_segment(user_id)
    relative = Path(relative_path)
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise StorageQuotaUnavailable("报告资源路径无效")
    if any("\\" in part or "\x00" in part for part in relative.parts):
        raise StorageQuotaUnavailable("报告资源路径无效")
    return f"report_archive/{tenant}/{user}/{relative.as_posix()}"


def report_archive_db_resource_key(tenant_id: str, user_id: str, report_id: str) -> str:
    """Stable quota key for a report stored as a PostgreSQL JSONB payload."""
    import re

    from backend.shared.owner_path import owner_segment

    report = str(report_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", report):
        raise StorageQuotaUnavailable("报告资源标识无效")
    return (
        f"report_archive_db/{owner_segment(tenant_id)}/"
        f"{owner_segment(user_id)}/{report}"
    )


def qwenpaw_attachment_resource_key(
    tenant_id: str, user_id: str, session_id: str, file_name: str
) -> str:
    """Stable quota key for a QwenPaw attachment owned by one session."""
    from backend.shared.owner_path import owner_segment

    session = owner_segment(session_id)
    name = str(file_name or "").strip()
    if (
        name in {"", ".", ".."}
        or Path(name).name != name
        or "\\" in name
        or "\x00" in name
    ):
        raise StorageQuotaUnavailable("QwenPaw 附件文件名无效")
    return (
        f"qwenpaw_attachments/{owner_segment(tenant_id)}/"
        f"{owner_segment(user_id)}/{session}/{name}"
    )


def _walk_qwenpaw_attachment_files(
    shared_root: str | Path, *, required: bool = False
) -> dict[tuple[str, str], dict[str, int]]:
    """Inventory QwenPaw attachment and index files without following links."""
    from backend.shared.owner_path import owner_segment

    base = Path(shared_root) / "quantmind_uploads"
    try:
        mode = os.lstat(base).st_mode
    except FileNotFoundError:
        if required:
            raise StorageUsageIncomplete(
                "QwenPaw 附件共享卷缺失，配额对账拒绝开放"
            ) from None
        return {}
    except OSError as exc:
        raise StorageUsageIncomplete("无法检查 QwenPaw 附件共享卷") from exc
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise StorageUsageIncomplete("QwenPaw 附件根目录不安全")

    found: dict[tuple[str, str], dict[str, int]] = {}

    def add_file(path: Path, tenant: str, user: str, namespace: str) -> None:
        try:
            file_mode = os.lstat(path).st_mode
        except OSError as exc:
            raise StorageUsageIncomplete("无法检查 QwenPaw 附件文件") from exc
        if not stat.S_ISREG(file_mode):
            raise StorageUsageIncomplete("QwenPaw 附件包含非普通文件")
        if "\\" in path.name or "\x00" in path.name:
            raise StorageUsageIncomplete("QwenPaw 附件文件名无效")
        relative = path.relative_to(base).as_posix()
        key = f"{namespace}/{relative}"
        found.setdefault((tenant, user), {})[key] = os.stat(
            path, follow_symlinks=False
        ).st_size

    def checked_dirs(path: Path) -> list[Path]:
        try:
            entries = list(os.scandir(path))
        except OSError as exc:
            raise StorageUsageIncomplete("无法完整枚举 QwenPaw 附件目录") from exc
        dirs = []
        for entry in entries:
            if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                raise StorageUsageIncomplete("QwenPaw 附件目录结构不安全")
            dirs.append(Path(entry.path))
        return dirs

    def scan_owner_tree(tree: Path, *, metadata: bool) -> None:
        for tenant_dir in checked_dirs(tree):
            if not metadata and tenant_dir.name == "_meta":
                continue
            tenant = owner_segment(tenant_dir.name)
            for user_dir in checked_dirs(tenant_dir):
                user = owner_segment(user_dir.name)
                for session_dir in checked_dirs(user_dir):
                    owner_segment(session_dir.name)
                    try:
                        entries = list(os.scandir(session_dir))
                    except OSError as exc:
                        raise StorageUsageIncomplete(
                            "无法完整枚举 QwenPaw 会话附件"
                        ) from exc
                    for entry in entries:
                        if entry.is_symlink() or not entry.is_file(
                            follow_symlinks=False
                        ):
                            raise StorageUsageIncomplete(
                                "QwenPaw 会话附件包含目录、链接或特殊文件"
                            )
                        name = entry.name
                        if metadata and name != "attachments.json":
                            raise StorageUsageIncomplete(
                                "QwenPaw 附件索引目录包含未知文件"
                            )
                        if not metadata:
                            add_file(
                                Path(entry.path), tenant, user, "qwenpaw_attachments"
                            )

    try:
        scan_owner_tree(base, metadata=False)
        metadata_root = base / "_meta"
        try:
            metadata_mode = os.lstat(metadata_root).st_mode
        except FileNotFoundError:
            metadata_mode = None
        if metadata_mode is not None:
            if stat.S_ISLNK(metadata_mode) or not stat.S_ISDIR(metadata_mode):
                raise StorageUsageIncomplete("QwenPaw 附件索引目录不安全")
            scan_owner_tree(metadata_root, metadata=True)
    except StorageUsageIncomplete:
        raise
    except (OSError, ValueError) as exc:
        raise StorageUsageIncomplete("无法完整重建 QwenPaw 附件用量") from exc
    return found


def user_model_resource_key(
    tenant_id: str, user_id: str, relative_path: str | Path
) -> str:
    """Stable quota key for an artifact stored in a user's model directory."""
    from backend.shared.owner_path import owner_segment

    relative = Path(relative_path)
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
        or any("\\" in part or "\x00" in part for part in relative.parts)
    ):
        raise StorageQuotaUnavailable("模型资源路径无效")
    return (
        f"user_models/{owner_segment(tenant_id)}/{owner_segment(user_id)}/"
        f"{relative.as_posix()}"
    )


def user_strategy_resource_key(tenant_id: str, user_id: str, strategy_id: str) -> str:
    """Stable quota key for the source saved by IDE, Lab, or strategy APIs."""
    import re

    from backend.shared.owner_path import owner_segment

    strategy = str(strategy_id or "").strip()
    if not re.fullmatch(r"[0-9]{1,20}", strategy):
        raise StorageQuotaUnavailable("策略资源标识无效")
    return (
        f"user_strategies/{owner_segment(tenant_id)}/"
        f"{owner_segment(user_id)}/{strategy}.py"
    )


def stock_pool_object_resource_key(
    tenant_id: str, user_id: str, object_key: str
) -> str:
    """Stable quota key for a stock-pool object stored outside local data roots."""
    tenant = str(tenant_id or "").strip()
    user = str(user_id or "").strip()
    key = str(object_key or "").strip()
    if not tenant or not user or not key or "\x00" in key or "\\" in key:
        raise StorageQuotaUnavailable("股票池对象属主或对象键无效")
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return f"stock_pool_objects/{_owner_hash(tenant, user)}/{digest}"


def _canonical_stock_pool_user_id(user_id: str) -> str:
    value = str(user_id or "").strip()
    return value.zfill(8) if value.isdigit() else value


def _stock_pool_object_inventory(
    rows, known_owners: set[tuple[str, str]]
) -> dict[tuple[str, str], dict[str, int]]:
    """Build fail-closed quota inventory from durable COS/local pool metadata."""
    found: dict[tuple[str, str], dict[str, int]] = {}
    for row in rows:
        tenant = str(row["tenant_id"] or "").strip()
        raw_user = str(row["user_id"] or "").strip()
        user = _canonical_stock_pool_user_id(raw_user)
        object_key = str(row["file_key"] or "").strip()
        if not tenant or not user or not object_key:
            raise StorageUsageIncomplete(
                "股票池云对象缺少租户、用户或对象键，配额对账拒绝猜测归属"
            )
        owner = (tenant, user)
        if owner not in known_owners:
            raise StorageUsageIncomplete(
                "股票池云对象属主不在用户表中，配额对账拒绝猜测归属"
            )
        parts = object_key.split("/")
        if (
            len(parts) != 4
            or parts[0] != "user_pools"
            or _canonical_stock_pool_user_id(parts[1]) != user
            or not parts[2]
            or parts[3] not in {"stock_pool.txt", "stock_pool.csv", "stock_pool.json"}
            or any(part in {"", ".", ".."} for part in parts)
        ):
            raise StorageUsageIncomplete(
                "股票池云对象键与属主不匹配，配额对账拒绝猜测归属"
            )
        try:
            size = int(row["file_size"])
        except (TypeError, ValueError):
            raise StorageUsageIncomplete(
                "股票池云对象缺少有效字节数，配额对账拒绝低估"
            ) from None
        if size < 0:
            raise StorageUsageIncomplete("股票池云对象字节数无效")
        resource_key = stock_pool_object_resource_key(tenant, user, object_key)
        owner_files = found.setdefault(owner, {})
        if resource_key in owner_files and owner_files[resource_key] != size:
            raise StorageUsageIncomplete("同一股票池云对象记录了冲突的字节数")
        owner_files[resource_key] = size
    return found


def backtest_result_resource_key(
    tenant_id: str, user_id: str, relative_path: str | Path
) -> str:
    """Stable quota key for an owner-scoped persisted backtest result."""
    from backend.shared.owner_path import owner_segment

    relative = Path(relative_path)
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
        or any("\\" in part or "\x00" in part for part in relative.parts)
    ):
        raise StorageQuotaUnavailable("回测资源路径无效")
    return (
        f"backtest_results/{owner_segment(tenant_id)}/"
        f"{owner_segment(user_id)}/{relative.as_posix()}"
    )


def pipeline_artifact_resource_key(
    tenant_id: str, user_id: str, relative_path: str | Path
) -> str:
    """Stable quota key for user-owned AI IDE/Lab pipeline artifacts."""
    from backend.shared.owner_path import owner_segment

    relative = Path(relative_path)
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
        or any("\\" in part or "\x00" in part for part in relative.parts)
    ):
        raise StorageQuotaUnavailable("Pipeline 产物路径无效")
    return (
        f"pipeline_artifacts/{owner_segment(tenant_id)}/"
        f"{owner_segment(user_id)}/{relative.as_posix()}"
    )


def factor_pool_panel_resource_key(
    tenant_id: str, user_id: str, relative_path: str | Path
) -> str:
    """Stable quota key for owner-scoped factor research panels."""
    from backend.shared.owner_path import owner_segment

    relative = Path(relative_path)
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
        or any("\\" in part or "\x00" in part for part in relative.parts)
    ):
        raise StorageQuotaUnavailable("因子面板资源路径无效")
    return (
        f"factor_pool_panels/{owner_segment(tenant_id)}/"
        f"{owner_segment(user_id)}/{relative.as_posix()}"
    )


def user_factor_dataset_resource_key(
    tenant_id: str, user_id: str, relative_path: str | Path
) -> str:
    """Stable quota key for immutable user-owned factor dataset generations."""
    from backend.shared.owner_path import owner_segment

    relative = Path(relative_path)
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
        or any("\\" in part or "\x00" in part for part in relative.parts)
    ):
        raise StorageQuotaUnavailable("用户因子数据集路径无效")
    return (
        f"user_factor_datasets/{owner_segment(tenant_id)}/"
        f"{owner_segment(user_id)}/{relative.as_posix()}"
    )


def write_quota_managed_local_file(
    *,
    root: str | Path,
    tenant_id: str,
    user_id: str,
    relative_path: str | Path,
    content: bytes | str | Path,
    resource_namespace: str = "pipeline_artifacts",
) -> Path:
    """Atomically write a user artifact after reserving its exact byte size.

    ``resource_namespace`` selects the durable ledger namespace while preserving
    the common ``root/{tenant}/{user}/{relative_path}`` layout.
    """
    import tempfile

    from backend.shared.owner_path import owner_segment

    relative = Path(relative_path)
    source_path: Path | None = None
    if isinstance(content, (str, Path)):
        source_path = Path(content)
        try:
            source_mode = os.lstat(source_path).st_mode
        except OSError as exc:
            raise StorageQuotaUnavailable("待写入本地文件不可用") from exc
        if not stat.S_ISREG(source_mode):
            raise StorageQuotaUnavailable("待写入本地文件不是普通文件")
        requested_bytes = source_path.stat().st_size
    else:
        requested_bytes = len(content)
    key_builders = {
        "pipeline_artifacts": pipeline_artifact_resource_key,
        "factor_pool_panels": factor_pool_panel_resource_key,
        "user_factor_datasets": user_factor_dataset_resource_key,
        "user_models": user_model_resource_key,
    }
    key_builder = key_builders.get(resource_namespace)
    if key_builder is None:
        raise StorageQuotaUnavailable("本地存储资源类型无效")
    resource_key = key_builder(tenant_id, user_id, relative)
    tenant = owner_segment(tenant_id)
    user = owner_segment(user_id)
    base = Path(root).absolute()

    try:
        if base.exists() and stat.S_ISLNK(os.lstat(base).st_mode):
            raise StorageQuotaUnavailable("Pipeline 产物根目录不能是符号链接")
        base.mkdir(parents=True, exist_ok=True)
        owner_dir = base / tenant / user
        run_dir = owner_dir.joinpath(*relative.parts[:-1])
        for directory in (base, base / tenant, owner_dir, *run_dir.parents):
            if not directory.exists():
                directory.mkdir(parents=True, exist_ok=True)
            mode = os.lstat(directory).st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                raise StorageQuotaUnavailable("Pipeline 产物路径包含非目录或符号链接")
        run_dir.mkdir(parents=True, exist_ok=True)
        resolved_base = base.resolve()
        if not run_dir.resolve().is_relative_to(resolved_base):
            raise StorageQuotaUnavailable("Pipeline 产物路径越出存储根目录")
        destination = run_dir / relative.name
        if destination.exists() and stat.S_ISLNK(os.lstat(destination).st_mode):
            raise StorageQuotaUnavailable("Pipeline 产物不能替换符号链接")
    except StorageQuotaUnavailable:
        raise
    except OSError as exc:
        raise StorageQuotaUnavailable("无法准备 Pipeline 产物目录") from exc

    from backend.shared.storage_quota_ledger import (
        reserve_local_file,
        release_local_file,
        settle_local_file,
    )

    reservation_id = reserve_local_file(tenant, user, resource_key, requested_bytes)
    temp_path: Path | None = None
    replaced = False
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=run_dir,
            prefix=f".{relative.name}.",
            suffix=".tmp",
            delete=False,
        ) as staged:
            temp_path = Path(staged.name)
            if source_path is None:
                staged.write(content)
            else:
                import shutil

                with source_path.open("rb") as source:
                    shutil.copyfileobj(source, staged, length=1024 * 1024)
            staged.flush()
            os.fsync(staged.fileno())
        if temp_path.stat().st_size != requested_bytes:
            raise StorageQuotaUnavailable("待写入本地文件在复制期间发生变化")
        os.replace(temp_path, destination)
        replaced = True
        try:
            directory_fd = os.open(run_dir, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
        settle_local_file(reservation_id, resource_key, destination.stat().st_size)
        return destination
    except Exception:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
        if not replaced:
            release_local_file(reservation_id)
        # Once the atomic replace happened, retain the reservation on ledger
        # failure. Startup reconciliation will settle the durable file.
        raise


def _walk_all_owner_storage_files(
    root: str | Path, namespace: str
) -> dict[tuple[str, str], dict[str, int]]:
    """Inventory an owner directory tree without following links."""
    from backend.shared.owner_path import owner_segment

    base = Path(root)
    try:
        mode = os.lstat(base).st_mode
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise StorageUsageIncomplete(f"无法检查 {namespace} 根目录 {base}") from exc
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise StorageUsageIncomplete(f"{namespace} 根目录不安全 {base}")

    found: dict[tuple[str, str], dict[str, int]] = {}
    try:
        with os.scandir(base) as tenants:
            for tenant_entry in tenants:
                if tenant_entry.is_symlink():
                    raise StorageUsageIncomplete(
                        f"{namespace} 目录包含符号链接 {tenant_entry.path}"
                    )
                if not tenant_entry.is_dir(follow_symlinks=False):
                    continue
                tenant = owner_segment(tenant_entry.name)
                with os.scandir(tenant_entry.path) as users:
                    for user_entry in users:
                        if user_entry.is_symlink():
                            raise StorageUsageIncomplete(
                                f"{namespace} 目录包含符号链接 {user_entry.path}"
                            )
                        if not user_entry.is_dir(follow_symlinks=False):
                            continue
                        user = owner_segment(user_entry.name)
                        owner_dir = Path(user_entry.path)
                        owner_files: dict[str, int] = {}
                        stack = [owner_dir]
                        while stack:
                            current = stack.pop()
                            with os.scandir(current) as entries:
                                for entry in entries:
                                    if entry.is_symlink():
                                        raise StorageUsageIncomplete(
                                            f"{namespace} 目录包含符号链接 {entry.path}"
                                        )
                                    if entry.is_dir(follow_symlinks=False):
                                        stack.append(Path(entry.path))
                                    elif entry.is_file(follow_symlinks=False):
                                        relative = Path(entry.path).relative_to(
                                            owner_dir
                                        )
                                        owner_files[
                                            f"{namespace}/{tenant}/{user}/"
                                            f"{relative.as_posix()}"
                                        ] = entry.stat(follow_symlinks=False).st_size
                        if owner_files:
                            found[(tenant, user)] = owner_files
    except StorageUsageIncomplete:
        raise
    except (OSError, ValueError) as exc:
        raise StorageUsageIncomplete(f"无法完整重建 {namespace} 目录 {base}") from exc
    return found


def _walk_all_user_model_files(
    root: str | Path,
) -> dict[tuple[str, str], dict[str, int]]:
    return _walk_all_owner_storage_files(root, "user_models")


def _walk_all_backtest_files(root: str | Path) -> dict[tuple[str, str], dict[str, int]]:
    return _walk_all_owner_storage_files(root, "backtest_results")


def _walk_all_pipeline_artifact_files(
    root: str | Path,
) -> dict[tuple[str, str], dict[str, int]]:
    return _walk_all_owner_storage_files(root, "pipeline_artifacts")


def _walk_all_factor_pool_panel_files(
    root: str | Path,
) -> dict[tuple[str, str], dict[str, int]]:
    return _walk_all_owner_storage_files(root, "factor_pool_panels")


def _walk_all_user_factor_dataset_files(
    root: str | Path,
) -> dict[tuple[str, str], dict[str, int]]:
    return _walk_all_owner_storage_files(root, "user_factor_datasets")


def _walk_training_job_files(
    root: str | Path,
    run_owners: dict[str, tuple[str, str]],
) -> dict[tuple[str, str], dict[str, int]]:
    """Inventory persisted training workspaces using their durable job owners."""
    import re

    from backend.shared.owner_path import owner_segment

    base = Path(root)
    try:
        mode = os.lstat(base).st_mode
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise StorageUsageIncomplete(f"无法检查训练工作目录 {base}") from exc
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise StorageUsageIncomplete(f"训练工作目录不安全 {base}")

    found: dict[tuple[str, str], dict[str, int]] = {}
    try:
        with os.scandir(base) as entries:
            for run_entry in entries:
                run_id = run_entry.name
                if run_entry.is_symlink() or not run_entry.is_dir(
                    follow_symlinks=False
                ):
                    raise StorageUsageIncomplete(
                        f"训练工作目录包含非属主目录 {run_entry.path}"
                    )
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", run_id):
                    raise StorageUsageIncomplete(
                        f"训练工作目录标识无效 {run_entry.path}"
                    )
                owner = run_owners.get(run_id)
                if owner is None:
                    raise StorageUsageIncomplete(
                        f"训练工作目录没有任务属主记录 {run_entry.path}"
                    )
                tenant_id, user_id = owner
                tenant = owner_segment(tenant_id)
                user = owner_segment(user_id)
                run_dir = Path(run_entry.path)
                owner_files = found.setdefault((tenant_id, user_id), {})
                stack = [run_dir]
                while stack:
                    current = stack.pop()
                    with os.scandir(current) as children:
                        for entry in children:
                            if entry.is_symlink():
                                raise StorageUsageIncomplete(
                                    f"训练产物包含符号链接 {entry.path}"
                                )
                            if entry.is_dir(follow_symlinks=False):
                                stack.append(Path(entry.path))
                            elif entry.is_file(follow_symlinks=False):
                                relative = Path(entry.path).relative_to(run_dir)
                                key = (
                                    f"training_jobs/{tenant}/{user}/{run_id}/"
                                    f"{relative.as_posix()}"
                                )
                                owner_files[key] = entry.stat(
                                    follow_symlinks=False
                                ).st_size
                            else:
                                raise StorageUsageIncomplete(
                                    f"训练目录包含不支持的文件类型 {entry.path}"
                                )
    except StorageUsageIncomplete:
        raise
    except (OSError, ValueError) as exc:
        raise StorageUsageIncomplete(f"无法完整重建训练工作目录 {base}") from exc
    return found


def _walk_training_workspace_files(
    root: str | Path,
    tenant_id: str,
    user_id: str,
    run_id: str,
) -> dict[str, int]:
    """Inventory one live workspace without following links or special files."""
    import re

    from backend.shared.owner_path import owner_segment

    run = str(run_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", run):
        raise StorageUsageIncomplete("训练工作区 run ID 无效")
    base = Path(root)
    try:
        mode = os.lstat(base).st_mode
    except FileNotFoundError as exc:
        raise StorageUsageIncomplete(f"训练工作区不存在 {base}") from exc
    except OSError as exc:
        raise StorageUsageIncomplete(f"无法检查训练工作区 {base}") from exc
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise StorageUsageIncomplete(f"训练工作区不安全 {base}")

    tenant = owner_segment(tenant_id)
    user = owner_segment(user_id)
    found: dict[str, int] = {}
    stack = [base]
    try:
        while stack:
            current = stack.pop()
            with os.scandir(current) as entries:
                for entry in entries:
                    if entry.is_symlink():
                        raise StorageUsageIncomplete(
                            f"训练工作区包含符号链接 {entry.path}"
                        )
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        relative = Path(entry.path).relative_to(base)
                        key = (
                            f"training_jobs/{tenant}/{user}/{run}/{relative.as_posix()}"
                        )
                        found[key] = entry.stat(follow_symlinks=False).st_size
                        if len(found) > 50_000:
                            raise StorageUsageIncomplete(
                                "训练工作区文件数超过对账上限 50000"
                            )
                    else:
                        raise StorageUsageIncomplete(
                            f"训练工作区包含不支持的文件类型 {entry.path}"
                        )
    except StorageUsageIncomplete:
        raise
    except (OSError, ValueError) as exc:
        raise StorageUsageIncomplete(f"无法完整扫描训练工作区 {base}") from exc
    return found


def sync_local_training_workspace_usage(
    root: str | Path,
    tenant_id: str,
    user_id: str,
    run_id: str,
    *,
    quota_gb: float | None = None,
) -> int:
    """Reconcile one live training workspace into the owner's quota ledger.

    The measured usage is committed even when it has crossed the quota so other
    writers are denied immediately. The caller must stop the training process
    after StorageQuotaExceeded is raised.
    """
    tenant = str(tenant_id or "").strip()
    user = str(user_id or "").strip()
    if not tenant or not user:
        raise StorageQuotaUnavailable("训练工作区缺少租户或用户属主")
    run = str(run_id or "").strip()
    from backend.shared.owner_path import owner_segment

    prefix = f"training_jobs/{owner_segment(tenant)}/{owner_segment(user)}/{run}/"
    limit = _quota_bytes(quota_gb)
    over_quota: tuple[int, int] | None = None
    try:
        with _local_quota_session() as session:
            ready = session.execute(
                text(
                    "SELECT ready FROM public.storage_quota_state "
                    "WHERE singleton=TRUE FOR SHARE"
                )
            ).scalar_one_or_none()
            if ready is not True:
                raise StorageQuotaUnavailable("存储配额对账尚未完成")
            # Serialize the filesystem snapshot and its ledger update. Without
            # this lock, a slower stale scan can overwrite a newer observer or
            # completion-callback snapshot and undercount the live workspace.
            session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"training-workspace:{tenant}:{user}:{run}"},
            )
            files = _walk_training_workspace_files(root, tenant, user, run)
            session.execute(
                text(
                    "INSERT INTO public.storage_quota_accounts "
                    "(tenant_id,user_id,used_bytes,reserved_bytes,quota_bytes) "
                    "VALUES (:tenant,:user,0,0,:quota) ON CONFLICT DO NOTHING"
                ),
                {"tenant": tenant, "user": user, "quota": limit},
            )
            account = (
                session.execute(
                    text(
                        "SELECT used_bytes,reserved_bytes FROM "
                        "public.storage_quota_accounts WHERE tenant_id=:tenant "
                        "AND user_id=:user FOR UPDATE"
                    ),
                    {"tenant": tenant, "user": user},
                )
                .mappings()
                .one()
            )
            existing = (
                session.execute(
                    text(
                        "SELECT resource_key,size_bytes FROM "
                        "public.storage_quota_objects WHERE tenant_id=:tenant "
                        "AND user_id=:user AND left(resource_key,:prefix_len)=:prefix "
                        "FOR UPDATE"
                    ),
                    {
                        "tenant": tenant,
                        "user": user,
                        "prefix": prefix,
                        "prefix_len": len(prefix),
                    },
                )
                .mappings()
                .all()
            )
            old = {str(row["resource_key"]): int(row["size_bytes"]) for row in existing}
            delta = sum(files.values()) - sum(old.values())
            used = max(0, int(account["used_bytes"]) + delta)
            reserved = int(account["reserved_bytes"])
            session.execute(
                text(
                    "UPDATE public.storage_quota_accounts SET used_bytes=:used, "
                    "quota_bytes=:quota,updated_at=NOW() WHERE tenant_id=:tenant "
                    "AND user_id=:user"
                ),
                {"used": used, "quota": limit, "tenant": tenant, "user": user},
            )
            for resource_key in old.keys() - files.keys():
                session.execute(
                    text(
                        "DELETE FROM public.storage_quota_objects "
                        "WHERE resource_key=:key AND tenant_id=:tenant "
                        "AND user_id=:user"
                    ),
                    {"key": resource_key, "tenant": tenant, "user": user},
                )
            for resource_key, size in files.items():
                upserted = session.execute(
                    text(
                        "INSERT INTO public.storage_quota_objects "
                        "(resource_key,tenant_id,user_id,size_bytes) "
                        "VALUES (:key,:tenant,:user,:size) ON CONFLICT "
                        "(resource_key) DO UPDATE SET size_bytes=EXCLUDED.size_bytes, "
                        "updated_at=NOW() WHERE storage_quota_objects.tenant_id="
                        "EXCLUDED.tenant_id AND storage_quota_objects.user_id="
                        "EXCLUDED.user_id"
                    ),
                    {"key": resource_key, "tenant": tenant, "user": user, "size": size},
                )
                if upserted.rowcount != 1:
                    raise StorageQuotaUnavailable("训练工作区配额资源已被其他属主占用")
            if used + reserved > limit:
                over_quota = (used + reserved, limit)
    except (StorageQuotaUnavailable, StorageUsageIncomplete):
        raise
    except Exception as exc:
        raise StorageQuotaUnavailable("无法同步训练工作区配额") from exc

    if over_quota is not None:
        used_bytes, limit_bytes = over_quota
        raise StorageQuotaExceeded(
            user,
            used_bytes / (1024**3),
            limit_bytes / (1024**3),
        )
    return sum(files.values())


def _report_archive_root() -> Path:
    """Match the report router's configured and legacy archive directory order."""
    configured = (
        os.getenv("REPORT_ARCHIVE_DIR") or os.getenv("TRADING_AGENTS_RESULTS_DIR", "")
    ).strip()
    if configured and Path(configured).is_dir():
        return Path(configured)
    for candidate in (
        Path("/data/reports/trading_agents"),
        Path("/app/db/trading_agents_results"),
    ):
        if candidate.is_dir():
            return candidate
    return Path("/data/reports/trading_agents")


def _walk_report_archive_files(
    archive_root: str | Path, tenant_id: str, user_id: str
) -> dict[str, int]:
    """Inventory regular files under one report owner directory without following links."""
    from backend.shared.owner_path import owner_segment

    root = Path(archive_root)
    tenant = owner_segment(tenant_id)
    user = owner_segment(user_id)
    try:
        root_mode = os.lstat(root).st_mode
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise StorageUsageIncomplete(f"无法检查报告归档目录 {root}") from exc
    if stat.S_ISLNK(root_mode) or not stat.S_ISDIR(root_mode):
        raise StorageUsageIncomplete(f"报告归档目录不安全 {root}")

    owner_dir = root / tenant / user
    current = root
    for part in (tenant, user):
        current = current / part
        try:
            mode = os.lstat(current).st_mode
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise StorageUsageIncomplete(f"无法检查报告属主目录 {current}") from exc
        if stat.S_ISLNK(mode):
            raise StorageUsageIncomplete(f"报告属主目录包含符号链接 {current}")
        if not stat.S_ISDIR(mode):
            return {}

    found: dict[str, int] = {}
    stack = [owner_dir]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        file_stat = entry.stat(follow_symlinks=False)
                        relative = Path(entry.path).relative_to(owner_dir)
                        key = report_archive_resource_key(tenant, user, relative)
                        found[key] = file_stat.st_size
        except OSError as exc:
            raise StorageUsageIncomplete(f"无法重建报告归档目录 {current}") from exc
    return found


def _walk_owner_files(root: Path, tenant_id: str, user_id: str) -> dict[str, int]:
    """Inventory one identity's supported upload namespaces without following links."""
    owner = _owner_hash(tenant_id, user_id)
    folders = (
        root / "private_uploads" / owner,
        root / "uploads" / owner,
        root / "uploads" / "public" / owner,
    )
    found: dict[str, int] = {}
    for folder in folders:
        relative_parts = folder.relative_to(root).parts
        current = root
        missing = False
        for part in relative_parts:
            current = current / part
            try:
                mode = os.lstat(current).st_mode
            except FileNotFoundError:
                missing = True
                break
            except OSError as exc:
                raise StorageUsageIncomplete(f"无法检查配额目录 {current}") from exc
            if stat.S_ISLNK(mode):
                raise StorageUsageIncomplete(f"配额目录包含符号链接 {current}")
            if not stat.S_ISDIR(mode):
                missing = True
                break
        if missing:
            continue
        stack = [folder]
        while stack:
            current = stack.pop()
            try:
                with os.scandir(current) as entries:
                    for entry in entries:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            file_stat = entry.stat(follow_symlinks=False)
                            key = Path(entry.path).relative_to(root).as_posix()
                            found[key] = file_stat.st_size
            except OSError as exc:
                raise StorageUsageIncomplete(f"无法重建配额目录 {current}") from exc
    return found


AI_STRATEGY_UPLOAD_CATEGORIES = (
    "strategies",
    "templates",
    "backtests",
    "data",
    "documents",
    "images",
    "temp",
    "general",
)


def _walk_ai_strategy_files(upload_root: str | Path) -> dict[str, tuple[str, str, int]]:
    """Rebuild FileManager objects from validated owner sidecars."""
    base = Path(upload_root)
    try:
        base_mode = os.lstat(base).st_mode
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise StorageUsageIncomplete(f"无法检查 AI Strategy 上传目录 {base}") from exc
    if stat.S_ISLNK(base_mode) or not stat.S_ISDIR(base_mode):
        raise StorageUsageIncomplete(f"AI Strategy 上传目录不安全 {base}")

    base_resolved = base.resolve()
    found: dict[str, tuple[str, str, int]] = {}
    allowed_root_entries = set(AI_STRATEGY_UPLOAD_CATEGORIES) | {".qm-index-generation"}
    try:
        with os.scandir(base) as entries:
            for entry in entries:
                if entry.name not in allowed_root_entries:
                    raise StorageUsageIncomplete(
                        f"AI Strategy 根目录存在未归属文件 {entry.path}"
                    )
                mode = entry.stat(follow_symlinks=False).st_mode
                if entry.name == ".qm-index-generation":
                    if not stat.S_ISREG(mode):
                        raise StorageUsageIncomplete(
                            f"AI Strategy 索引文件不安全 {entry.path}"
                        )
                elif stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                    raise StorageUsageIncomplete(
                        f"AI Strategy 类别目录不安全 {entry.path}"
                    )
    except StorageUsageIncomplete:
        raise
    except OSError as exc:
        raise StorageUsageIncomplete(f"无法枚举 AI Strategy 上传目录 {base}") from exc

    for category in AI_STRATEGY_UPLOAD_CATEGORIES:
        category_dir = base / category
        try:
            mode = os.lstat(category_dir).st_mode
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise StorageUsageIncomplete(
                f"无法检查 AI Strategy 类别目录 {category_dir}"
            ) from exc
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise StorageUsageIncomplete(f"AI Strategy 类别目录不安全 {category_dir}")
        resolved_category = category_dir.resolve()
        if not resolved_category.is_relative_to(base_resolved):
            raise StorageUsageIncomplete(
                f"AI Strategy 类别目录越出上传根目录 {category_dir}"
            )

        try:
            with os.scandir(category_dir) as entries:
                entry_names = {entry.name for entry in entries}
            sidecars = [
                category_dir / name
                for name in entry_names
                if name.endswith(".qmmeta.json")
            ]
        except OSError as exc:
            raise StorageUsageIncomplete(
                f"无法枚举 AI Strategy 类别目录 {category_dir}"
            ) from exc

        for sidecar in sidecars:
            try:
                if sidecar.is_symlink() or not sidecar.is_file():
                    raise ValueError("sidecar is not a regular file")
                payload = json.loads(sidecar.read_text(encoding="utf-8"))
                file_id = str(payload["file_id"])
                UUID(file_id)
                storage_name = payload["storage_name"]
                if (
                    not isinstance(storage_name, str)
                    or Path(storage_name).name != storage_name
                    or not storage_name.startswith(f"{file_id}_")
                    or sidecar.name != storage_name + ".qmmeta.json"
                    or payload.get("category") != category
                ):
                    raise ValueError("sidecar identity does not match its path")
                tenant_id = str(payload.get("tenant_id") or "").strip()
                user_id = str(payload.get("user_id") or "").strip()
                if not tenant_id or not user_id:
                    raise ValueError("sidecar has no owner")
                filename = payload["filename"]
                content_type = payload["content_type"]
                description = payload.get("description", "")
                tags = payload.get("tags", [])
                uploaded_at = datetime.fromisoformat(payload["upload_time"])
                safe_filename = (
                    str(filename or "").replace("\\", "/").rsplit("/", 1)[-1]
                )
                if (
                    not all(
                        isinstance(value, str)
                        for value in (filename, content_type, description)
                    )
                    or filename != safe_filename.strip(" .")[:255]
                    or not filename
                    or any(not char.isprintable() for char in content_type)
                    or not isinstance(tags, list)
                    or not all(isinstance(tag, str) for tag in tags)
                    or uploaded_at.tzinfo is None
                ):
                    raise ValueError("sidecar fields are incomplete")
                file_path = category_dir / storage_name
                file_mode = os.lstat(file_path).st_mode
                if stat.S_ISLNK(file_mode) or not stat.S_ISREG(file_mode):
                    raise ValueError("file is not a regular file")
                resolved_file = file_path.resolve(strict=True)
                if not resolved_file.is_relative_to(resolved_category):
                    raise ValueError("file escaped its category directory")
                size = file_path.stat().st_size
                resource_key = f"ai_strategy_uploads/{category}/{storage_name}"
                found[resource_key] = (tenant_id, user_id, size)
                entry_names.discard(sidecar.name)
                entry_names.discard(storage_name)
            except (
                OSError,
                ValueError,
                TypeError,
                KeyError,
                json.JSONDecodeError,
            ) as exc:
                raise StorageUsageIncomplete(
                    f"AI Strategy 文件归属元数据不完整 {sidecar}"
                ) from exc
        # Data without a valid owner sidecar (including an interrupted write) must
        # not make the startup reconciliation silently undercount usage.
        if entry_names:
            raise StorageUsageIncomplete(
                f"AI Strategy 目录存在未归属或临时文件 {category_dir}"
            )
    return found


def _walk_stock_pool_files(
    root: Path, tenant_id: str, user_id: str, *, scope: str = "user"
) -> dict[str, int]:
    """Inventory isolated and legacy flat user pools during startup reconciliation."""
    owner = user_id if scope == "user" else tenant_id
    prefix = "u" if scope == "user" else "t"
    safe_owner = _safe_path_component(owner)
    folder = root / f"{prefix}{safe_owner}"
    found: dict[str, int] = {}

    def require_directory(path: Path, *, absent_ok: bool) -> bool:
        try:
            mode = os.lstat(path).st_mode
        except FileNotFoundError as exc:
            if absent_ok:
                return False
            raise StorageUsageIncomplete(f"股票池目录不存在 {path}") from exc
        except OSError as exc:
            raise StorageUsageIncomplete(f"无法检查股票池目录 {path}") from exc
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise StorageUsageIncomplete(f"股票池目录不安全 {path}")
        return True

    def add_tree(base: Path) -> None:
        stack = [base]
        while stack:
            current = stack.pop()
            try:
                with os.scandir(current) as entries:
                    for entry in entries:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            file_stat = entry.stat(follow_symlinks=False)
                            relative = Path(entry.path).relative_to(root).as_posix()
                            found[f"stock_pool/{relative}"] = file_stat.st_size
            except OSError as exc:
                raise StorageUsageIncomplete(
                    f"无法重建股票池配额目录 {current}"
                ) from exc

    root_exists = require_directory(root, absent_ok=True)
    if root_exists and require_directory(folder, absent_ok=True):
        add_tree(folder)

    tenant_folder = root / f"{prefix}{safe_owner}_t{_safe_path_component(tenant_id)}"
    if root_exists and require_directory(tenant_folder, absent_ok=True):
        add_tree(tenant_folder)

    if root_exists:
        try:
            with os.scandir(root) as entries:
                for entry in entries:
                    if (
                        entry.name.startswith(f"{prefix}{safe_owner}_")
                        and entry.name.lower().endswith(".txt")
                        and entry.is_file(follow_symlinks=False)
                        and not entry.is_symlink()
                    ):
                        file_stat = entry.stat(follow_symlinks=False)
                        found[f"stock_pool/{entry.name}"] = file_stat.st_size
        except OSError as exc:
            raise StorageUsageIncomplete(f"无法枚举股票池根目录 {root}") from exc
    return found


async def reconcile_local_storage(root: str | Path | None = None) -> None:
    """Rebuild durable upload, artifact, and stock-pool usage at startup."""
    from backend.shared.database_manager_v2 import get_session

    base = Path(root or os.getenv("STORAGE_ROOT", "/data")).resolve()
    stock_root = Path(os.getenv("QM_STOCK_POOL_TXT_DIR", "/data/stock_pool")).resolve()
    model_root = Path(os.getenv("USER_MODELS_ROOT", "/app/models/users"))
    if not model_root.is_absolute():
        model_root = Path("/app") / model_root
    model_root = model_root.resolve()
    backtest_root = Path(
        os.getenv("QLIB_BACKTEST_RESULT_DIR", "/data/backtest_results")
    )
    if not backtest_root.is_absolute():
        backtest_root = Path("/app") / backtest_root
    backtest_root = backtest_root.resolve()
    pipeline_root = Path(
        os.getenv("PIPELINE_ARTIFACTS_DIR", "/data/pipeline_artifacts")
    )
    if not pipeline_root.is_absolute():
        pipeline_root = Path("/app") / pipeline_root
    pipeline_root = pipeline_root.resolve()
    factor_pool_panel_root = Path(
        os.getenv("QM_FACTOR_POOL_PANEL_DIR", "/data/factor_research/panels")
    )
    if not factor_pool_panel_root.is_absolute():
        factor_pool_panel_root = Path("/app") / factor_pool_panel_root
    factor_pool_panel_root = factor_pool_panel_root.resolve()
    user_factor_dataset_root = (
        Path(os.getenv("QM_QUANTCUSTOM_DATA_DIR", "/data/quantcustom")).resolve()
        / "users"
    )
    report_root = _report_archive_root()
    qwenpaw_shared_root = Path(os.getenv("QWENPAW_SHARED_FILES_DIR", "/qwenpaw-shared"))
    qwenpaw_required = os.getenv(
        "OPENCLAW_USER_SURFACE_ENABLED", "false"
    ).strip().lower() in {"1", "true", "yes", "on"}
    # Close admission first. If inventory or persistence fails, uploads stay denied.
    async with get_session() as session:
        closed = await session.execute(
            text(
                "UPDATE public.storage_quota_state SET ready=FALSE, updated_at=NOW() "
                "WHERE singleton=TRUE RETURNING singleton"
            )
        )
        if not closed.first():
            raise StorageQuotaUnavailable("存储配额对账状态表未初始化")

    async with get_session() as session:
        rows = (
            (
                await session.execute(
                    text(
                        "SELECT tenant_id, user_id FROM public.users ORDER BY tenant_id, user_id"
                    )
                )
            )
            .mappings()
            .all()
        )
        training_rows = (
            (
                await session.execute(
                    text(
                        "SELECT id, tenant_id, user_id "
                        "FROM public.admin_training_jobs ORDER BY id"
                    )
                )
            )
            .mappings()
            .all()
        )
        qwenpaw_ledger_refs = (
            await session.execute(
                text(
                    "SELECT EXISTS (SELECT 1 FROM public.storage_quota_objects "
                    "WHERE resource_key LIKE :prefix) OR EXISTS ("
                    "SELECT 1 FROM public.storage_quota_reservations "
                    "WHERE resource_key LIKE :prefix AND state='reserved')"
                ),
                {"prefix": "qwenpaw_attachments/%"},
            )
        ).scalar_one()
        qwenpaw_required = qwenpaw_required or bool(qwenpaw_ledger_refs)
        stock_pool_rows = (
            (
                await session.execute(
                    text(
                        "SELECT tenant_id, user_id, file_key, file_size "
                        "FROM public.stock_pool_files ORDER BY id"
                    )
                )
            )
            .mappings()
            .all()
        )
        tenant_rows = (
            (
                await session.execute(
                    text(
                        "SELECT DISTINCT tenant_id FROM public.qm_stock_pool "
                        "WHERE scope='tenant' AND tenant_id IS NOT NULL "
                        "ORDER BY tenant_id"
                    )
                )
            )
            .mappings()
            .all()
        )
        report_rows = (
            (
                await session.execute(
                    text(
                        "SELECT tenant_id, user_id, report_id, "
                        "octet_length(payload::text) AS size_bytes "
                        "FROM public.qm_user_report_archive "
                        "ORDER BY tenant_id, user_id, report_id"
                    )
                )
            )
            .mappings()
            .all()
        )
        orphan_strategy = (
            await session.execute(
                text(
                    "SELECT s.id FROM public.strategies s "
                    "LEFT JOIN public.users u ON u.id=s.user_id "
                    "WHERE s.user_id IS NOT NULL AND "
                    "COALESCE(NULLIF(octet_length(s.code), 0), s.file_size, 0) > 0 AND "
                    "(u.id IS NULL OR u.tenant_id IS NULL OR u.user_id IS NULL) "
                    "LIMIT 1"
                )
            )
        ).first()
        if orphan_strategy:
            raise StorageUsageIncomplete(
                "策略源码存在无法映射到有效用户/租户的记录，配额对账拒绝猜测归属"
            )
        strategy_rows = (
            (
                await session.execute(
                    text(
                        "SELECT u.tenant_id, u.user_id, s.id, "
                        "COALESCE(NULLIF(octet_length(s.code), 0), s.file_size, 0) "
                        "AS size_bytes "
                        "FROM public.strategies s "
                        "JOIN public.users u ON u.id=s.user_id "
                        "WHERE s.user_id IS NOT NULL AND "
                        "COALESCE(NULLIF(octet_length(s.code), 0), s.file_size, 0) > 0 "
                        "ORDER BY u.tenant_id, u.user_id, s.id"
                    )
                )
            )
            .mappings()
            .all()
        )
    identities = [(str(row["tenant_id"]), str(row["user_id"]), "user") for row in rows]
    identities.extend(
        (str(row["tenant_id"]), tenant_quota_user_id(str(row["tenant_id"])), "tenant")
        for row in tenant_rows
    )
    inventories = await asyncio.to_thread(
        lambda: [
            (
                tenant_id,
                user_id,
                {
                    **(
                        _walk_owner_files(base, tenant_id, user_id)
                        if scope == "user"
                        else {}
                    ),
                    **(
                        _walk_report_archive_files(report_root, tenant_id, user_id)
                        if scope == "user"
                        else {}
                    ),
                    **_walk_stock_pool_files(
                        stock_root,
                        tenant_id,
                        user_id if scope == "user" else tenant_id,
                        scope=scope,
                    ),
                },
            )
            for tenant_id, user_id, scope in identities
        ]
    )
    inventories_by_owner = {
        (tenant_id, user_id): files for tenant_id, user_id, files in inventories
    }
    known_owners = set(inventories_by_owner)
    stock_pool_object_files = _stock_pool_object_inventory(
        stock_pool_rows, known_owners
    )
    for owner, files in stock_pool_object_files.items():
        inventories_by_owner[owner].update(files)
    model_files = await asyncio.to_thread(_walk_all_user_model_files, model_root)
    backtest_files = await asyncio.to_thread(_walk_all_backtest_files, backtest_root)
    pipeline_files = await asyncio.to_thread(
        _walk_all_pipeline_artifact_files, pipeline_root
    )
    factor_pool_panel_files = await asyncio.to_thread(
        _walk_all_factor_pool_panel_files, factor_pool_panel_root
    )
    user_factor_dataset_files = await asyncio.to_thread(
        _walk_all_user_factor_dataset_files, user_factor_dataset_root
    )
    qwenpaw_files = await asyncio.to_thread(
        _walk_qwenpaw_attachment_files,
        qwenpaw_shared_root,
        required=qwenpaw_required,
    )
    training_root = base / "training_jobs"
    training_owners = {
        str(row["id"]): (str(row["tenant_id"] or ""), str(row["user_id"] or ""))
        for row in training_rows
    }
    training_files = await asyncio.to_thread(
        _walk_training_job_files, training_root, training_owners
    )
    evaluation_files = await asyncio.to_thread(
        _walk_all_owner_storage_files, base / "factor_evaluations", "training_jobs"
    )
    for namespace, owner_files in (
        ("模型", model_files),
        ("回测", backtest_files),
        ("AI IDE/Lab", pipeline_files),
        ("因子研究面板", factor_pool_panel_files),
        ("用户因子数据集", user_factor_dataset_files),
        ("训练", training_files),
        ("因子评估", evaluation_files),
        ("QwenPaw 附件", qwenpaw_files),
    ):
        for owner, files in owner_files.items():
            if owner not in known_owners:
                raise StorageUsageIncomplete(
                    f"{namespace}文件属主不在用户表中，配额对账拒绝猜测归属"
                )
            inventories_by_owner[owner].update(files)
    inventories = [
        (tenant_id, user_id, inventories_by_owner[(tenant_id, user_id)])
        for tenant_id, user_id, _ in inventories
    ]
    for row in report_rows:
        tenant_id, user_id = str(row["tenant_id"]), str(row["user_id"])
        if (tenant_id, user_id) not in known_owners:
            raise StorageUsageIncomplete(
                "数据库报告属主不在用户表中，配额对账拒绝猜测归属"
            )
        resource_key = report_archive_db_resource_key(
            tenant_id, user_id, str(row["report_id"])
        )
        inventories_by_owner[(tenant_id, user_id)][resource_key] = int(
            row["size_bytes"]
        )
    for row in strategy_rows:
        tenant_id, user_id = str(row["tenant_id"]), str(row["user_id"])
        if (tenant_id, user_id) not in known_owners:
            raise StorageUsageIncomplete(
                "策略源码属主不在用户表中，配额对账拒绝猜测归属"
            )
        resource_key = user_strategy_resource_key(tenant_id, user_id, str(row["id"]))
        inventories_by_owner[(tenant_id, user_id)][resource_key] = int(
            row["size_bytes"] or 0
        )
    inventories = [
        (tenant_id, user_id, inventories_by_owner[(tenant_id, user_id)])
        for tenant_id, user_id, _ in inventories
    ]
    configured_ai_root = os.getenv("AI_STRATEGY_UPLOAD_DIR")
    if configured_ai_root:
        ai_strategy_files = await asyncio.to_thread(
            _walk_ai_strategy_files, configured_ai_root
        )
        known_owners = {(tenant_id, user_id) for tenant_id, user_id, _ in identities}
        inventories_by_owner = {
            (tenant_id, user_id): files for tenant_id, user_id, files in inventories
        }
        for resource_key, (tenant_id, user_id, size) in ai_strategy_files.items():
            if (tenant_id, user_id) not in known_owners:
                raise StorageUsageIncomplete(
                    "AI Strategy 文件属主不在用户表中，配额对账拒绝猜测归属"
                )
            inventories_by_owner[(tenant_id, user_id)][resource_key] = size
        inventories = [
            (tenant_id, user_id, inventories_by_owner[(tenant_id, user_id)])
            for tenant_id, user_id, _ in identities
        ]

    async with get_session() as session:
        # An interrupted upload is settled if its exact key reached disk, otherwise
        # released. This recovers reservations across API process restarts.
        inventory_by_key = {
            key: (tenant_id, user_id, size)
            for tenant_id, user_id, files in inventories
            for key, size in files.items()
        }
        reservations = (
            (
                await session.execute(
                    text(
                        "SELECT reservation_id, tenant_id, user_id, resource_key, state, "
                        "(resource_key LIKE :hub_lock_prefix AND updated_at >= "
                        "NOW() - (:hub_lock_ttl * INTERVAL '1 second')) "
                        "AS active_hub_import_lock FROM "
                        "public.storage_quota_reservations WHERE state='reserved' FOR UPDATE"
                    ).bindparams(
                        hub_lock_prefix=f"{HUB_IMPORT_LOCK_PREFIX}%",
                        hub_lock_ttl=HUB_IMPORT_LOCK_STALE_AFTER_SECONDS,
                    )
                )
            )
            .mappings()
            .all()
        )
        for reservation in reservations:
            if reservation["active_hub_import_lock"]:
                # A rolling API startup must not remove a live import's unique
                # marker merely because it has no corresponding disk file.
                continue
            recovered = inventory_by_key.get(reservation["resource_key"])
            belongs_to_reservation = bool(
                recovered
                and recovered[0] == reservation["tenant_id"]
                and recovered[1] == reservation["user_id"]
            )
            if (
                reservation["resource_key"].startswith("stock_pool_objects/")
                and not belongs_to_reservation
            ):
                raise StorageUsageIncomplete(
                    "股票池对象预占没有对应的持久属主记录；需先核实远端对象，配额准入保持关闭"
                )
            await session.execute(
                text(
                    "UPDATE public.storage_quota_reservations SET state=:state, "
                    "actual_bytes=:actual, updated_at=NOW() WHERE reservation_id=:id"
                ),
                {
                    "state": "settled" if belongs_to_reservation else "released",
                    "actual": recovered[2] if belongs_to_reservation else None,
                    "id": reservation["reservation_id"],
                },
            )

        await session.execute(text("DELETE FROM public.storage_quota_objects"))
        for tenant_id, user_id, files in inventories:
            if files:
                await session.execute(
                    text(
                        "INSERT INTO public.storage_quota_objects "
                        "(resource_key, tenant_id, user_id, size_bytes) "
                        "VALUES (:key, :tenant, :user, :size)"
                    ),
                    [
                        {"key": key, "tenant": tenant_id, "user": user_id, "size": size}
                        for key, size in files.items()
                    ],
                )
            used = sum(files.values())
            await session.execute(
                text(
                    "INSERT INTO public.storage_quota_accounts "
                    "(tenant_id, user_id, used_bytes, reserved_bytes, quota_bytes) "
                    "VALUES (:tenant, :user, :used, 0, :quota) "
                    "ON CONFLICT (tenant_id, user_id) DO UPDATE SET "
                    "used_bytes=EXCLUDED.used_bytes, reserved_bytes=0, "
                    "quota_bytes=EXCLUDED.quota_bytes, updated_at=NOW()"
                ),
                {
                    "tenant": tenant_id,
                    "user": user_id,
                    "used": used,
                    "quota": _quota_bytes(),
                },
            )
        opened = await session.execute(
            text(
                "UPDATE public.storage_quota_state SET ready=TRUE, updated_at=NOW() "
                "WHERE singleton=TRUE RETURNING singleton"
            )
        )
        if not opened.first():
            raise StorageQuotaUnavailable("存储配额对账状态表未初始化")


async def reserve_upload(
    tenant_id: str,
    user_id: str,
    resource_key: str,
    requested_bytes: int,
    *,
    quota_gb: float | None = None,
    session=None,
) -> str:
    """Atomically reserve capacity in PostgreSQL before writing an upload."""
    tenant = str(tenant_id or "").strip()
    user = str(user_id or "").strip()
    amount = max(0, int(requested_bytes))
    if not tenant or not user or not resource_key:
        raise StorageQuotaUnavailable("租户、用户和资源标识不能为空")
    limit = _quota_bytes(quota_gb)
    reservation_id = uuid4().hex
    async with _quota_session(session) as session:
        ready = (
            await session.execute(
                text(
                    "SELECT ready FROM public.storage_quota_state "
                    "WHERE singleton=TRUE FOR SHARE"
                )
            )
        ).scalar_one_or_none()
        if ready is not True:
            raise StorageQuotaUnavailable("存储配额对账尚未完成")
        await session.execute(
            text(
                "INSERT INTO public.storage_quota_accounts "
                "(tenant_id, user_id, used_bytes, reserved_bytes, quota_bytes) "
                "VALUES (:tenant, :user, 0, 0, :quota) ON CONFLICT DO NOTHING"
            ),
            {"tenant": tenant, "user": user, "quota": limit},
        )
        existing = (
            await session.execute(
                text(
                    "SELECT size_bytes FROM public.storage_quota_objects "
                    "WHERE resource_key=:key AND tenant_id=:tenant AND user_id=:user "
                    "FOR UPDATE"
                ),
                {"key": resource_key, "tenant": tenant, "user": user},
            )
        ).scalar_one_or_none()
        replaced_bytes = int(existing or 0)
        # Reserve the full replacement while the temporary and old files coexist.
        reserve_bytes = amount
        accepted = (
            (
                await session.execute(
                    text(
                        "UPDATE public.storage_quota_accounts SET "
                        "reserved_bytes=reserved_bytes+:amount, quota_bytes=:quota, "
                        "updated_at=NOW() WHERE tenant_id=:tenant AND user_id=:user "
                        "AND used_bytes+reserved_bytes+:amount <= :quota "
                        "RETURNING used_bytes, reserved_bytes"
                    ),
                    {
                        "tenant": tenant,
                        "user": user,
                        "amount": reserve_bytes,
                        "quota": limit,
                    },
                )
            )
            .mappings()
            .first()
        )
        if not accepted:
            current = (
                (
                    await session.execute(
                        text(
                            "SELECT used_bytes, reserved_bytes FROM "
                            "public.storage_quota_accounts WHERE tenant_id=:tenant "
                            "AND user_id=:user"
                        ),
                        {"tenant": tenant, "user": user},
                    )
                )
                .mappings()
                .one()
            )
            used_gb = (current["used_bytes"] + current["reserved_bytes"]) / (1024**3)
            raise StorageQuotaExceeded(user, used_gb, limit / (1024**3))
        await session.execute(
            text(
                "INSERT INTO public.storage_quota_reservations "
                "(reservation_id, tenant_id, user_id, resource_key, reserved_bytes, "
                "replaced_bytes, state) "
                "VALUES (:id, :tenant, :user, :key, :amount, :replaced, 'reserved')"
            ),
            {
                "id": reservation_id,
                "tenant": tenant,
                "user": user,
                "key": resource_key,
                "amount": reserve_bytes,
                "replaced": replaced_bytes,
            },
        )
    return reservation_id


async def renew_hub_import_lock(reservation_id: str, resource_key: str) -> bool:
    """Refresh an active Hub import marker without changing quota accounting."""
    if not str(resource_key).startswith(HUB_IMPORT_LOCK_PREFIX):
        raise StorageQuotaUnavailable("Hub 导入锁资源键无效")
    async with _quota_session() as session:
        result = await session.execute(
            text(
                "UPDATE public.storage_quota_reservations SET updated_at=NOW() "
                "WHERE reservation_id=:id AND resource_key=:key "
                "AND state='reserved' RETURNING reservation_id"
            ),
            {"id": reservation_id, "key": resource_key},
        )
        return result.first() is not None


async def release_stale_hub_import_lock(resource_key: str) -> bool:
    """Release an abandoned Hub import marker after its heartbeat has expired."""
    if not str(resource_key).startswith(HUB_IMPORT_LOCK_PREFIX):
        raise StorageQuotaUnavailable("Hub 导入锁资源键无效")
    async with _quota_session() as session:
        reservation = (
            (
                await session.execute(
                    text(
                        "UPDATE public.storage_quota_reservations "
                        "SET state='released', updated_at=NOW() "
                        "WHERE resource_key=:key AND state='reserved' "
                        "AND updated_at < NOW() - (:ttl * INTERVAL '1 second') "
                        "RETURNING tenant_id, user_id, reserved_bytes"
                    ),
                    {
                        "key": resource_key,
                        "ttl": HUB_IMPORT_LOCK_STALE_AFTER_SECONDS,
                    },
                )
            )
            .mappings()
            .first()
        )
        if reservation is None:
            return False
        await session.execute(
            text(
                "UPDATE public.storage_quota_accounts SET "
                "reserved_bytes=GREATEST(0, reserved_bytes-:amount), "
                "updated_at=NOW() WHERE tenant_id=:tenant AND user_id=:user"
            ),
            {
                "amount": reservation["reserved_bytes"],
                "tenant": reservation["tenant_id"],
                "user": reservation["user_id"],
            },
        )
        return True


async def settle_upload(
    reservation_id: str, resource_key: str, actual_bytes: int, *, session=None
) -> None:
    """Convert a durable reservation into charged object bytes exactly once."""

    actual = max(0, int(actual_bytes))
    async with _quota_session(session) as session:
        reservation = (
            (
                await session.execute(
                    text(
                        "SELECT tenant_id, user_id, resource_key, reserved_bytes, "
                        "replaced_bytes, state "
                        "FROM public.storage_quota_reservations "
                        "WHERE reservation_id=:id FOR UPDATE"
                    ),
                    {"id": reservation_id},
                )
            )
            .mappings()
            .first()
        )
        if not reservation:
            raise StorageQuotaUnavailable("存储预占记录不存在")
        if reservation["resource_key"] != resource_key:
            raise StorageQuotaUnavailable("存储预占资源标识不匹配")
        if reservation["state"] == "settled":
            return
        if reservation["state"] != "reserved":
            raise StorageQuotaUnavailable("存储预占状态无效")
        if actual > reservation["reserved_bytes"]:
            raise StorageQuotaUnavailable("实际文件大小超过预占容量")
        await session.execute(
            text(
                "UPDATE public.storage_quota_accounts SET "
                "reserved_bytes=reserved_bytes-:reserved, "
                "used_bytes=GREATEST(0, used_bytes-:replaced+:actual), "
                "updated_at=NOW() WHERE tenant_id=:tenant AND user_id=:user"
            ),
            {
                "reserved": reservation["reserved_bytes"],
                "replaced": reservation["replaced_bytes"],
                "actual": actual,
                "tenant": reservation["tenant_id"],
                "user": reservation["user_id"],
            },
        )
        await session.execute(
            text(
                "UPDATE public.storage_quota_reservations SET state='settled', "
                "actual_bytes=:actual, updated_at=NOW() WHERE reservation_id=:id"
            ),
            {"actual": actual, "id": reservation_id},
        )
        inserted = await session.execute(
            text(
                "INSERT INTO public.storage_quota_objects "
                "(resource_key, tenant_id, user_id, size_bytes) "
                "VALUES (:key, :tenant, :user, :size) "
                "ON CONFLICT (resource_key) DO UPDATE SET "
                "tenant_id=EXCLUDED.tenant_id, user_id=EXCLUDED.user_id, "
                "size_bytes=EXCLUDED.size_bytes, updated_at=NOW() "
                "WHERE storage_quota_objects.tenant_id=EXCLUDED.tenant_id "
                "AND storage_quota_objects.user_id=EXCLUDED.user_id "
                "RETURNING resource_key"
            ),
            {
                "key": resource_key,
                "tenant": reservation["tenant_id"],
                "user": reservation["user_id"],
                "size": actual,
            },
        )
        if not inserted.first():
            raise StorageQuotaUnavailable("存储对象属主冲突")


async def release_upload(reservation_id: str) -> None:
    """Release a failed upload reservation idempotently."""
    from backend.shared.database_manager_v2 import get_session

    async with get_session() as session:
        reservation = (
            (
                await session.execute(
                    text(
                        "SELECT tenant_id, user_id, reserved_bytes, state "
                        "FROM public.storage_quota_reservations "
                        "WHERE reservation_id=:id FOR UPDATE"
                    ),
                    {"id": reservation_id},
                )
            )
            .mappings()
            .first()
        )
        if not reservation or reservation["state"] != "reserved":
            return
        await session.execute(
            text(
                "UPDATE public.storage_quota_accounts SET "
                "reserved_bytes=GREATEST(0, reserved_bytes-:amount), updated_at=NOW() "
                "WHERE tenant_id=:tenant AND user_id=:user"
            ),
            {
                "amount": reservation["reserved_bytes"],
                "tenant": reservation["tenant_id"],
                "user": reservation["user_id"],
            },
        )
        await session.execute(
            text(
                "UPDATE public.storage_quota_reservations SET state='released', "
                "updated_at=NOW() WHERE reservation_id=:id"
            ),
            {"id": reservation_id},
        )


async def delete_upload_usage(
    tenant_id: str, user_id: str, resource_key: str, *, session=None
) -> None:
    """Subtract an object only after the storage provider confirms deletion."""
    async with _quota_session(session) as session:
        row = (
            await session.execute(
                text(
                    "DELETE FROM public.storage_quota_objects WHERE resource_key=:key "
                    "AND tenant_id=:tenant AND user_id=:user RETURNING size_bytes"
                ),
                {"key": resource_key, "tenant": tenant_id, "user": user_id},
            )
        ).first()
        if row:
            await session.execute(
                text(
                    "UPDATE public.storage_quota_accounts SET "
                    "used_bytes=GREATEST(0, used_bytes-:amount), updated_at=NOW() "
                    "WHERE tenant_id=:tenant AND user_id=:user"
                ),
                {
                    "amount": row[0],
                    "tenant": tenant_id,
                    "user": user_id,
                },
            )


async def rename_upload_usage(
    tenant_id: str, user_id: str, old_resource_key: str, new_resource_key: str
) -> None:
    """Atomically move a charged local or remote object key without changing usage."""
    from backend.shared.database_manager_v2 import get_session

    if not old_resource_key or not new_resource_key:
        raise StorageQuotaUnavailable("存储资源标识不能为空")
    if old_resource_key == new_resource_key:
        return
    try:
        async with get_session() as session:
            moved = await session.execute(
                text(
                    "UPDATE public.storage_quota_objects SET resource_key=:new_key, "
                    "updated_at=NOW() WHERE resource_key=:old_key "
                    "AND tenant_id=:tenant AND user_id=:user "
                    "RETURNING resource_key"
                ),
                {
                    "new_key": new_resource_key,
                    "old_key": old_resource_key,
                    "tenant": tenant_id,
                    "user": user_id,
                },
            )
            if not moved.first():
                raise StorageQuotaUnavailable("报告文件配额记录不存在或属主不匹配")
    except StorageQuotaUnavailable:
        raise
    except Exception as exc:
        raise StorageQuotaUnavailable("无法更新报告文件配额标识") from exc


def reserve_local_file(
    tenant_id: str,
    user_id: str,
    resource_key: str,
    requested_bytes: int,
    *,
    quota_gb: float | None = None,
    session=None,
) -> str:
    """Synchronous counterpart for atomic writers such as stock-pool materialization."""
    tenant = str(tenant_id or "").strip()
    user = str(user_id or "").strip()
    if not tenant or not user or not resource_key:
        raise StorageQuotaUnavailable("租户、用户和资源标识不能为空")
    amount = max(0, int(requested_bytes))
    limit = _quota_bytes(quota_gb)
    reservation_id = uuid4().hex
    try:
        with _local_quota_session(session) as session:
            ready = session.execute(
                text(
                    "SELECT ready FROM public.storage_quota_state "
                    "WHERE singleton=TRUE FOR SHARE"
                )
            ).scalar_one_or_none()
            if ready is not True:
                raise StorageQuotaUnavailable("存储配额对账尚未完成")
            session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": resource_key},
            )
            session.execute(
                text(
                    "INSERT INTO public.storage_quota_accounts "
                    "(tenant_id, user_id, used_bytes, reserved_bytes, quota_bytes) "
                    "VALUES (:tenant, :user, 0, 0, :quota) ON CONFLICT DO NOTHING"
                ),
                {"tenant": tenant, "user": user, "quota": limit},
            )
            replaced = session.execute(
                text(
                    "SELECT size_bytes FROM public.storage_quota_objects "
                    "WHERE resource_key=:key AND tenant_id=:tenant AND user_id=:user "
                    "FOR UPDATE"
                ),
                {"key": resource_key, "tenant": tenant, "user": user},
            ).scalar_one_or_none()
            replaced_bytes = int(replaced or 0)
            reserve_bytes = amount
            accepted = (
                session.execute(
                    text(
                        "UPDATE public.storage_quota_accounts SET "
                        "reserved_bytes=reserved_bytes+:amount, quota_bytes=:quota, "
                        "updated_at=NOW() WHERE tenant_id=:tenant AND user_id=:user "
                        "AND used_bytes+reserved_bytes+:amount <= :quota "
                        "RETURNING used_bytes, reserved_bytes"
                    ),
                    {
                        "tenant": tenant,
                        "user": user,
                        "amount": reserve_bytes,
                        "quota": limit,
                    },
                )
                .mappings()
                .first()
            )
            if not accepted:
                current = (
                    session.execute(
                        text(
                            "SELECT used_bytes, reserved_bytes FROM "
                            "public.storage_quota_accounts WHERE tenant_id=:tenant "
                            "AND user_id=:user"
                        ),
                        {"tenant": tenant, "user": user},
                    )
                    .mappings()
                    .one()
                )
                used_gb = (current["used_bytes"] + current["reserved_bytes"]) / (
                    1024**3
                )
                raise StorageQuotaExceeded(user, used_gb, limit / (1024**3))
            session.execute(
                text(
                    "INSERT INTO public.storage_quota_reservations "
                    "(reservation_id, tenant_id, user_id, resource_key, reserved_bytes, "
                    "replaced_bytes, state) VALUES (:id, :tenant, :user, :key, "
                    ":amount, :replaced, 'reserved')"
                ),
                {
                    "id": reservation_id,
                    "tenant": tenant,
                    "user": user,
                    "key": resource_key,
                    "amount": reserve_bytes,
                    "replaced": replaced_bytes,
                },
            )
    except (StorageQuotaExceeded, StorageQuotaUnavailable):
        raise
    except Exception as exc:
        raise StorageQuotaUnavailable("无法原子预占本地存储") from exc
    return reservation_id


def settle_local_file(
    reservation_id: str,
    resource_key: str,
    actual_bytes: int,
    *,
    session=None,
) -> None:
    """Commit a local file replacement into the durable usage counter."""
    actual = max(0, int(actual_bytes))
    try:
        with _local_quota_session(session) as session:
            reservation = (
                session.execute(
                    text(
                        "SELECT tenant_id, user_id, resource_key, reserved_bytes, "
                        "replaced_bytes, state FROM public.storage_quota_reservations "
                        "WHERE reservation_id=:id FOR UPDATE"
                    ),
                    {"id": reservation_id},
                )
                .mappings()
                .first()
            )
            if not reservation or reservation["resource_key"] != resource_key:
                raise StorageQuotaUnavailable("本地存储预占记录不匹配")
            if reservation["state"] == "settled":
                return
            if reservation["state"] != "reserved":
                raise StorageQuotaUnavailable("本地存储预占状态无效")
            if actual > reservation["reserved_bytes"]:
                raise StorageQuotaUnavailable("实际文件大小超过预占容量")
            session.execute(
                text(
                    "UPDATE public.storage_quota_accounts SET "
                    "reserved_bytes=reserved_bytes-:reserved, "
                    "used_bytes=GREATEST(0, used_bytes-:replaced+:actual), "
                    "updated_at=NOW() WHERE tenant_id=:tenant AND user_id=:user"
                ),
                {
                    "reserved": reservation["reserved_bytes"],
                    "replaced": reservation["replaced_bytes"],
                    "actual": actual,
                    "tenant": reservation["tenant_id"],
                    "user": reservation["user_id"],
                },
            )
            inserted = session.execute(
                text(
                    "INSERT INTO public.storage_quota_objects "
                    "(resource_key, tenant_id, user_id, size_bytes) "
                    "VALUES (:key, :tenant, :user, :size) "
                    "ON CONFLICT (resource_key) DO UPDATE SET "
                    "tenant_id=EXCLUDED.tenant_id, user_id=EXCLUDED.user_id, "
                    "size_bytes=EXCLUDED.size_bytes, updated_at=NOW() "
                    "WHERE storage_quota_objects.tenant_id=EXCLUDED.tenant_id "
                    "AND storage_quota_objects.user_id=EXCLUDED.user_id "
                    "RETURNING resource_key"
                ),
                {
                    "key": resource_key,
                    "tenant": reservation["tenant_id"],
                    "user": reservation["user_id"],
                    "size": actual,
                },
            ).first()
            if not inserted:
                raise StorageQuotaUnavailable("本地文件资源属主冲突")
            session.execute(
                text(
                    "UPDATE public.storage_quota_reservations SET state='settled', "
                    "actual_bytes=:actual, updated_at=NOW() WHERE reservation_id=:id"
                ),
                {"actual": actual, "id": reservation_id},
            )
    except StorageQuotaUnavailable:
        raise
    except Exception as exc:
        raise StorageQuotaUnavailable("无法结算本地存储用量") from exc


def release_local_file(reservation_id: str, *, session=None) -> None:
    """Release a local writer's reservation when its atomic replace fails."""
    try:
        with _local_quota_session(session) as session:
            reservation = (
                session.execute(
                    text(
                        "SELECT tenant_id, user_id, reserved_bytes, state "
                        "FROM public.storage_quota_reservations "
                        "WHERE reservation_id=:id FOR UPDATE"
                    ),
                    {"id": reservation_id},
                )
                .mappings()
                .first()
            )
            if not reservation or reservation["state"] != "reserved":
                return
            session.execute(
                text(
                    "UPDATE public.storage_quota_accounts SET "
                    "reserved_bytes=GREATEST(0, reserved_bytes-:amount), updated_at=NOW() "
                    "WHERE tenant_id=:tenant AND user_id=:user"
                ),
                {
                    "amount": reservation["reserved_bytes"],
                    "tenant": reservation["tenant_id"],
                    "user": reservation["user_id"],
                },
            )
            session.execute(
                text(
                    "UPDATE public.storage_quota_reservations SET state='released', "
                    "updated_at=NOW() WHERE reservation_id=:id"
                ),
                {"id": reservation_id},
            )
    except Exception as exc:
        raise StorageQuotaUnavailable("无法释放本地存储预占") from exc


def release_local_file_reservations(
    tenant_id: str, user_id: str, resource_key: str, *, session=None
) -> None:
    """Release pending reservations after their matching durable file is removed."""
    try:
        with _local_quota_session(session) as session:
            rows = (
                session.execute(
                    text(
                        "SELECT reservation_id, reserved_bytes FROM "
                        "public.storage_quota_reservations WHERE tenant_id=:tenant "
                        "AND user_id=:user AND resource_key=:key AND state='reserved' "
                        "FOR UPDATE"
                    ),
                    {"tenant": tenant_id, "user": user_id, "key": resource_key},
                )
                .mappings()
                .all()
            )
            if not rows:
                return
            reserved_bytes = sum(int(row["reserved_bytes"]) for row in rows)
            session.execute(
                text(
                    "UPDATE public.storage_quota_accounts SET "
                    "reserved_bytes=GREATEST(0, reserved_bytes-:amount), "
                    "updated_at=NOW() WHERE tenant_id=:tenant AND user_id=:user"
                ),
                {
                    "amount": reserved_bytes,
                    "tenant": tenant_id,
                    "user": user_id,
                },
            )
            for row in rows:
                session.execute(
                    text(
                        "UPDATE public.storage_quota_reservations SET state='released', "
                        "updated_at=NOW() WHERE reservation_id=:id"
                    ),
                    {"id": row["reservation_id"]},
                )
    except StorageQuotaUnavailable:
        raise
    except Exception as exc:
        raise StorageQuotaUnavailable("无法释放本地文件预占") from exc


def delete_local_file_usage(
    tenant_id: str, user_id: str, resource_key: str, *, session=None
) -> None:
    """Drop a charged DB-backed object in the same transaction as its row delete."""
    try:
        with _local_quota_session(session) as session:
            row = session.execute(
                text(
                    "DELETE FROM public.storage_quota_objects WHERE resource_key=:key "
                    "AND tenant_id=:tenant AND user_id=:user RETURNING size_bytes"
                ),
                {"key": resource_key, "tenant": tenant_id, "user": user_id},
            ).first()
            if row:
                session.execute(
                    text(
                        "UPDATE public.storage_quota_accounts SET "
                        "used_bytes=GREATEST(0, used_bytes-:amount), updated_at=NOW() "
                        "WHERE tenant_id=:tenant AND user_id=:user"
                    ),
                    {
                        "amount": row[0],
                        "tenant": tenant_id,
                        "user": user_id,
                    },
                )
    except StorageQuotaUnavailable:
        raise
    except Exception as exc:
        raise StorageQuotaUnavailable("无法扣减策略存储用量") from exc
