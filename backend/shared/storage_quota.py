"""存储配额与卷巡检（Phase 4）。

per-user 隔离已有目录划分（stock_pool 的 u{uid}/t{tenant}/，见 materializer），
但零配额。本模块提供：

- user_usage_bytes：统计某用户在指定根目录下的占用（含新版子目录与旧版
  扁平 `u{uid}_*` 文件），防 symlink 逃逸、有文件数上限。
- check_user_quota：写入前调用，超 USER_STORAGE_QUOTA_GB（默认 20，<0 不限）
  抛 StorageQuotaExceeded（fail-closed，调用方转 429/507）。
- volume_report：卷顶层占用排行，供管理面板巡检/告警。
- sweep_old_files：bounded 按 mtime 清理，供留存任务复用。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_QUOTA_GB = 20.0
_MAX_WALK_FILES = 50000


class StorageQuotaExceeded(Exception):
    """用户存储超配额。调用方应转为 429（配额）或 507（卷满）。"""

    def __init__(self, user_id: str, used_gb: float, quota_gb: float):
        self.user_id = user_id
        self.used_gb = used_gb
        self.quota_gb = quota_gb
        super().__init__(
            f"用户 {user_id} 存储超限：已用 {used_gb:.2f}GB / 配额 {quota_gb:.2f}GB，"
            "请删除旧股票池/报告后重试"
        )


class StorageUsageIncomplete(Exception):
    """配额用量无法完整读取时拒绝放行，避免不完整统计造成穿透。"""


def quota_gb_default() -> float:
    try:
        return float(os.getenv("USER_STORAGE_QUOTA_GB", str(DEFAULT_QUOTA_GB)))
    except (TypeError, ValueError):
        return DEFAULT_QUOTA_GB


def _safe_name(text: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in str(text or ""))


def _iter_regular_files(root: Path, *, max_files: int = _MAX_WALK_FILES):
    """迭代常规文件（不跟随目录 symlink，防止逃逸出配额根）。"""
    count = 0
    stack = [root]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                entries = list(it)
        except OSError as exc:
            raise StorageUsageIncomplete(f"无法读取配额目录 {cur}") from exc
        for entry in entries:
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    if count >= max_files:
                        raise StorageUsageIncomplete(
                            f"配额目录文件数超过统计上限 {max_files}"
                        )
                    count += 1
                    yield Path(entry.path)
            except StorageUsageIncomplete:
                raise
            except OSError as exc:
                raise StorageUsageIncomplete(
                    f"无法检查配额目录条目 {entry.path}"
                ) from exc


def user_usage_bytes(
    root: str | Path,
    tenant_id: str | None,
    user_id: str | None,
    *,
    max_files: int = _MAX_WALK_FILES,
) -> int:
    """统计某用户/租户占用字节数。

    - 有 user_id：u{uid}/ 子目录 + 旧扁平 u{uid}_* 文件；
    - 无 user_id 但有 tenant_id：t{tenant}/ 子目录（租户池）。
    """
    base = Path(root)
    if not base.is_dir():
        return 0
    total = 0
    if tenant_id and user_id:
        from backend.shared.owner_path import user_scoped_dir

        owned_dir = user_scoped_dir(base, tenant_id, user_id)
        if owned_dir.is_dir() and not owned_dir.is_symlink():
            for fp in _iter_regular_files(owned_dir, max_files=max_files):
                try:
                    total += fp.stat().st_size
                except OSError as exc:
                    raise StorageUsageIncomplete(f"无法统计配额文件 {fp}") from exc
    if user_id:
        uid = _safe_name(user_id)
        subdir = base / f"u{uid}"
        if subdir.is_dir() and not subdir.is_symlink():
            for fp in _iter_regular_files(subdir, max_files=max_files):
                try:
                    total += fp.stat().st_size
                except OSError as exc:
                    raise StorageUsageIncomplete(f"无法统计配额文件 {fp}") from exc
        # 旧版扁平文件（读兼容期）：u{uid}_*.txt
        try:
            for fp in base.glob(f"u{uid}_*.txt"):
                try:
                    if fp.is_file() and not fp.is_symlink():
                        total += fp.stat().st_size
                except OSError as exc:
                    raise StorageUsageIncomplete(f"无法统计配额文件 {fp}") from exc
        except OSError as exc:
            raise StorageUsageIncomplete(f"无法枚举配额目录 {base}") from exc
    elif tenant_id:
        subdir = base / f"t{_safe_name(tenant_id)}"
        if subdir.is_dir() and not subdir.is_symlink():
            for fp in _iter_regular_files(subdir, max_files=max_files):
                try:
                    total += fp.stat().st_size
                except OSError as exc:
                    raise StorageUsageIncomplete(f"无法统计配额文件 {fp}") from exc
    return total


def check_user_quota(
    root: str | Path,
    tenant_id: str | None,
    user_id: str | None,
    additional_bytes: int = 0,
    *,
    quota_gb: float | None = None,
) -> float:
    """写入前检查。返回已用 GB；超限抛 StorageQuotaExceeded。

    user_id 为空且 tenant_id 为空（global/系统写入）不限制。quota_gb < 0 表示不限。
    """
    limit = quota_gb if quota_gb is not None else quota_gb_default()
    if (not user_id and not tenant_id) or limit < 0:
        return 0.0
    used_gb = user_usage_bytes(root, tenant_id, user_id) / (1024**3)
    if used_gb + max(0, additional_bytes) / (1024**3) > limit:
        raise StorageQuotaExceeded(str(user_id or f"tenant:{tenant_id}"), used_gb, limit)
    return used_gb


def volume_report(root: str | Path, top_n: int = 10) -> list[tuple[str, int]]:
    """卷顶层占用排行 [(name, bytes)]，供巡检/告警（不递归深层）。"""
    base = Path(root)
    rows: list[tuple[str, int]] = []
    if not base.is_dir():
        return rows
    try:
        children = [p for p in base.iterdir() if not p.is_symlink()]
    except OSError:
        return rows
    for child in children:
        try:
            if child.is_file():
                rows.append((child.name, child.stat().st_size))
            elif child.is_dir():
                subtotal = 0
                for fp in _iter_regular_files(child):
                    try:
                        subtotal += fp.stat().st_size
                    except OSError:
                        continue
                rows.append((child.name, subtotal))
        except OSError:
            continue
    rows.sort(key=lambda r: r[1], reverse=True)
    return rows[: max(1, top_n)]


def sweep_old_files(
    root: str | Path, max_age_days: float, limit: int = 200
) -> int:
    """删除超期文件（bounded），返回删除数。目录只删空目录。"""
    base = Path(root)
    if not base.is_dir() or max_age_days <= 0:
        return 0
    import time

    cutoff = time.time() - max_age_days * 86400
    deleted = 0
    for fp in _iter_regular_files(base, max_files=max(limit * 10, 1000)):
        if deleted >= limit:
            break
        try:
            if fp.stat().st_mtime < cutoff:
                fp.unlink()
                deleted += 1
        except OSError:
            continue
    # 顺手清理空目录（浅层一遍）
    try:
        for child in base.iterdir():
            try:
                if child.is_dir() and not child.is_symlink() and not any(child.iterdir()):
                    child.rmdir()
            except OSError:
                continue
    except OSError:
        pass
    return deleted
