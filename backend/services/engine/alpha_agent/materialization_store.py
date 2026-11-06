"""Delete unpublished factor partitions and return their quota."""

from __future__ import annotations

from pathlib import Path


def release_partition(tenant_id: str, user_id: str, path: Path, relative: Path) -> None:
    from backend.shared.storage_quota_ledger import (
        delete_local_file_usage,
        user_factor_dataset_resource_key,
    )

    if path.is_symlink():
        raise RuntimeError("Materialized partition path became a symlink")
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    delete_local_file_usage(
        tenant_id,
        user_id,
        user_factor_dataset_resource_key(tenant_id, user_id, relative),
    )


def generation_partition_roots(
    tenant_id: str, user_id: str, generation_id: str
) -> list[Path]:
    from backend.shared.feature_source import quantcustom_root, user_factor_dataset_dir
    from backend.shared.owner_path import owner_segment, user_scoped_dir

    roots = [
        user_factor_dataset_dir(
            tenant_id, user_id, "rd_mined", generation_id=generation_id
        )
    ]
    roots.append(
        user_scoped_dir(quantcustom_root() / "users", tenant_id, user_id)
        / "factor_serving"
        / owner_segment(generation_id)
        / "6_ml_datasets"
        / "rd_mined"
    )
    return roots


def discard_generation_files(
    tenant_id: str, user_id: str, generation_ids: list[str]
) -> int:
    """Remove parquet partitions for generations that never published."""
    from backend.shared.feature_source import quantcustom_root
    from backend.shared.owner_path import user_scoped_dir

    owner_root = user_scoped_dir(quantcustom_root() / "users", tenant_id, user_id)
    removed = 0
    for generation_id in generation_ids:
        for root in generation_partition_roots(tenant_id, user_id, generation_id):
            if root.is_symlink() or not root.is_dir():
                continue
            resolved_owner = owner_root.resolve()
            for path in sorted(root.glob("dt=*/data.parquet")):
                if path.is_symlink() or not path.is_file():
                    continue
                if not path.resolve().is_relative_to(resolved_owner):
                    raise RuntimeError("Materialized partition escaped its owner")
                relative = path.resolve().relative_to(resolved_owner)
                release_partition(tenant_id, user_id, path, relative)
                removed += 1
                parent = path.parent
                try:
                    parent.rmdir()
                except OSError:
                    pass
    return removed
