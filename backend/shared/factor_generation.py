"""因子库代次指针（D2）。

每个自有物化库在自己的根目录维护：
  ``generations/{generation_id}.json`` —— 一代不可变清单（源指纹/分区数/schema）
  ``generation.json``                  —— 最近一代（兼容旧读取方）
  ``active.json``                      —— 当前 active 指针

物化成功后才写；失败/部分完成不移动指针（训练侧可继续用上一代完整数据）。
训练配置可记录 ``factor_generation``；读取方用 ``validate_generation`` 校验
——默认只要求该代清单存在（可复现旧代），``require_active=True`` 才要求它
仍是当前 active。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

GENERATIONS_DIR = "generations"
ACTIVE_FILE = "active.json"
LATEST_FILE = "generation.json"


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def make_generation_id(prefix: str = "g") -> str:
    return f"{prefix}{time.strftime('%Y%m%dT%H%M%S')}-{os.getpid()}"


def record_generation(
    library_root: str | Path,
    *,
    generation_id: str | None = None,
    source_fingerprint: str = "",
    partitions: int = 0,
    schema_hash: str = "",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """写一代清单并原子切换 active 指针；返回该代 payload。"""
    root = Path(library_root)
    gid = generation_id or make_generation_id()
    payload = {
        "generation_id": gid,
        "status": "active",
        "source_fingerprint": str(source_fingerprint),
        "partitions": int(partitions),
        "schema_hash": str(schema_hash),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        **(extra or {}),
    }
    _atomic_json(root / GENERATIONS_DIR / f"{gid}.json", payload)
    _atomic_json(root / LATEST_FILE, payload)
    _atomic_json(root / ACTIVE_FILE, {"generation_id": gid, "updated_at": payload["created_at"]})
    return payload


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def read_active(library_root: str | Path) -> dict[str, Any] | None:
    root = Path(library_root)
    pointer = _read_json(root / ACTIVE_FILE)
    if not pointer:
        return None
    gid = str(pointer.get("generation_id") or "")
    if not gid:
        return None
    generation = _read_json(root / GENERATIONS_DIR / f"{gid}.json")
    if not generation:
        return None
    return generation


def validate_generation(
    library_root: str | Path,
    generation_id: str | None = None,
    *,
    require_active: bool = False,
) -> dict[str, Any]:
    """校验某个代次是否存在（以及是否仍为 active）。"""
    root = Path(library_root)
    active = read_active(root)
    if generation_id is None:
        return {
            "ok": active is not None,
            "active": active,
            "requested": None,
            "reasons": [] if active else ["no_active_generation"],
        }
    generation = _read_json(root / GENERATIONS_DIR / f"{generation_id}.json")
    reasons: list[str] = []
    if generation is None:
        reasons.append("generation_not_found")
    if require_active:
        active_id = str((active or {}).get("generation_id") or "")
        if active_id != generation_id:
            reasons.append(f"generation_not_active(active={active_id or 'none'})")
    return {
        "ok": not reasons,
        "active": active,
        "requested": generation,
        "reasons": reasons,
    }
