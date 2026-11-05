"""QuantDB SDK 版本与 V5 列集契约（B2）。

SDK 升级或上游宽表列集变化时，升级前就能发现：
- ``check_sdk_version``：安装版本必须落在 0.5.x（补丁可浮动）；
- ``check_factor_dictionary_schema``：本地最新 V5 分区列名与金样
  ``backend/shared/fixtures/quantdb_factor_dictionary_schema.json`` 对齐，
  多列/少列都报告 added/removed（不阻断读写，但管理页必须可见）。

本模块只读、绝不抛异常；数据未同步时返回 ``available=False``。
"""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

EXPECTED_SDK_VERSION = "0.5.0"
EXPECTED_SDK_MINOR = "0.5"
V5_REL_DIR = "6_ml_datasets/factor_dictionary"
GOLDEN_PATH = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "quantdb_factor_dictionary_schema.json"
)


def sdk_version() -> str:
    try:
        import quantdb_sdk  # type: ignore

        return str(getattr(quantdb_sdk, "__version__", "") or "")
    except Exception:  # noqa: BLE001 - 未安装/导入失败
        return ""


def _version_ok(installed: str) -> bool:
    """0.5.x 视为兼容（补丁浮动）；空/其他版本不通过。"""
    if not installed:
        return False
    parts = installed.strip().lstrip("v").split(".")
    if len(parts) < 2:
        return False
    return ".".join(parts[:2]) == EXPECTED_SDK_MINOR


def check_sdk_version() -> dict[str, Any]:
    installed = sdk_version()
    ok = _version_ok(installed)
    if not installed:
        message = "quantdb-sdk 未安装"
    elif ok:
        message = f"quantdb-sdk {installed}（期望 {EXPECTED_SDK_VERSION} 系列）"
    else:
        message = (
            f"quantdb-sdk 版本 {installed} 与期望 {EXPECTED_SDK_VERSION} 不匹配："
            "上游数据集/列集可能已变化，升级前先跑契约测试"
        )
    return {
        "ok": ok,
        "installed": installed,
        "expected": EXPECTED_SDK_VERSION,
        "message": message,
    }


@lru_cache(maxsize=1)
def golden_columns() -> tuple[str, ...]:
    """金样列名（首次读盘后进程内缓存）。缺失/损坏返回空元组。"""
    try:
        data = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
        columns = data.get("columns") if isinstance(data, dict) else data
        if isinstance(columns, list):
            return tuple(str(c) for c in columns)
    except (OSError, ValueError):
        pass
    return ()


def schema_hash(columns: list[str] | tuple[str, ...]) -> str:
    return hashlib.sha256("\n".join(str(c) for c in columns).encode()).hexdigest()


def latest_factor_dictionary_partition(root: str | Path | None = None) -> Path | None:
    """本地最新 V5 分区路径；未同步返回 None。"""
    if root is None:
        from backend.shared import quantdb_paths

        root = quantdb_paths.resolve_quantdb_dir()
    base = Path(root) / V5_REL_DIR
    if not base.is_dir():
        return None
    dates = sorted(
        p.name[3:]
        for p in base.glob("dt=*")
        if p.is_dir() and (p / "data.parquet").is_file()
    )
    if not dates:
        return None
    return base / f"dt={dates[-1]}" / "data.parquet"


def check_factor_dictionary_schema(
    path: str | Path | None = None,
    *,
    root: str | Path | None = None,
) -> dict[str, Any]:
    """比对本地最新 V5 分区列集与金样；不阻断读写，只报告。"""
    expected = list(golden_columns())
    target = (
        Path(path) if path is not None else latest_factor_dictionary_partition(root)
    )
    if target is None or not Path(target).is_file():
        return {
            "available": False,
            "ok": None,
            "expected_count": len(expected),
            "message": "本地尚未同步 V5 宽表，无法校验列集",
        }
    try:
        import pyarrow.parquet as pq

        actual = [str(c) for c in pq.ParquetFile(target).schema_arrow.names]
    except Exception as exc:  # noqa: BLE001
        return {
            "available": True,
            "ok": None,
            "expected_count": len(expected),
            "error": str(exc),
            "message": f"读取 V5 schema 失败: {exc}",
        }
    if not expected:
        return {
            "available": True,
            "ok": None,
            "actual_count": len(actual),
            "message": "金样缺失，无法校验 V5 列集",
        }
    added = sorted(set(actual) - set(expected))
    removed = sorted(set(expected) - set(actual))
    ok = not added and not removed
    return {
        "available": True,
        "ok": ok,
        "path": str(target),
        "expected_count": len(expected),
        "actual_count": len(actual),
        "added": added,
        "removed": removed,
        "schema_hash": schema_hash(actual),
        "expected_hash": schema_hash(expected),
        "message": (
            "V5 列集与金样一致"
            if ok
            else f"V5 列集漂移：+{len(added)} / -{len(removed)}（训练特征可能按名错位）"
        ),
    }


def contract_summary(root: str | Path | None = None) -> dict[str, Any]:
    """SDK 版本 + V5 列集契约的合并摘要（管理页/同步日志用）。"""
    return {
        "sdk": check_sdk_version(),
        "factor_dictionary": check_factor_dictionary_schema(root=root),
    }
