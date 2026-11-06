"""把租户和用户拼进目录时用的路径分段。

拒绝分隔符、空段和 glob 元字符，避免一个用户的标识扫到或写到别人的目录。
"""

from __future__ import annotations

from pathlib import Path

_GLOB_META = "*?[]"


def owner_segment(value: str) -> str:
    raw = str(value or "").strip()
    if not raw or len(raw) > 64 or raw in {".", ".."}:
        raise ValueError("illegal owner segment")
    if any(ch in raw for ch in ("/", "\\", "\x00", "..")):
        raise ValueError("illegal owner segment")
    if any(ch in raw for ch in _GLOB_META):
        raise ValueError("illegal owner segment")
    return raw


def user_scoped_dir(root: Path, tenant_id: str, user_id: str) -> Path:
    return Path(root) / owner_segment(tenant_id) / owner_segment(user_id)
