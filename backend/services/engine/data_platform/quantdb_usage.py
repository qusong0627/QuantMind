"""QuantDB 下载用量账本（B1）。

按数据集记录每天实际下载的 bytes / files / errors / seconds，落
``data/quantdb/.usage/YYYYMMDD.json``，供管理台展示「今日流量 / 失败率 /
最慢数据集」。不依赖 Redis：OSS 单容器 / 便携包都能用，且跨进程（同步在
Celery，API 在 8000）通过同一数据卷读取。

写入侧是同步进程的 8 个下载线程：进程内 buffer + 锁，每 50 条或 5 秒
flush 一次，进程退出 atexit 兜底；flush 用 tmp+rename 原子替换，重复
flush 不会重复累加（buffer 清空后才写）。

口径：只统计**实际走 CDN 的下载**（`_download_object`），预览/query_manifest
等零流量调用不计。
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from backend.shared import quantdb_paths

logger = logging.getLogger(__name__)

USAGE_DIR = ".usage"
FLUSH_EVERY = 50
FLUSH_SECONDS = 5.0

_LOCK = threading.RLock()
#: (resolved root, YYYYMMDD) -> dataset -> stats
_BUFFER: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
_BUFFER_COUNT = 0
_LAST_FLUSH = time.monotonic()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_root(root: str | Path | None) -> str:
    base = Path(root) if root is not None else quantdb_paths.resolve_quantdb_dir()
    try:
        return str(base.resolve())
    except OSError:
        return str(base.absolute())


def _usage_path(resolved_root: str, day: str) -> Path:
    return Path(resolved_root) / USAGE_DIR / f"{day}.json"


def _read_usage(path: Path, day: str) -> dict[str, Any] | None:
    """读日账本；缺失/损坏返回 None（调用方决定跳过还是新建）。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("datasets"), dict):
        return None
    data.setdefault("date", day)
    return data


def _merge_stats(target: dict[str, Any], incoming: dict[str, Any]) -> None:
    for key in ("bytes", "files", "errors"):
        target[key] = int(target.get(key, 0)) + int(incoming.get(key, 0))
    target["seconds"] = round(
        float(target.get("seconds", 0.0)) + float(incoming.get("seconds", 0.0)), 3
    )
    last_error = str(incoming.get("last_error") or "").strip()
    if last_error:
        target["last_error"] = last_error[:200]


def _flush_locked() -> None:
    global _BUFFER_COUNT, _LAST_FLUSH
    if not _BUFFER:
        _BUFFER_COUNT = 0
        _LAST_FLUSH = time.monotonic()
        return
    for (resolved_root, day), datasets in list(_BUFFER.items()):
        path = _usage_path(resolved_root, day)
        payload = _read_usage(path, day) or {"date": day, "datasets": {}}
        merged = payload.setdefault("datasets", {})
        for dataset, stats in datasets.items():
            entry = merged.setdefault(
                dataset,
                {"bytes": 0, "files": 0, "errors": 0, "seconds": 0.0},
            )
            _merge_stats(entry, stats)
        payload["updated_at"] = _now_iso()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            logger.warning("写 QuantDB 用量账本失败(%s): %s", path, exc)
    _BUFFER.clear()
    _BUFFER_COUNT = 0
    _LAST_FLUSH = time.monotonic()


def record_download(
    dataset: str,
    *,
    nbytes: int = 0,
    seconds: float = 0.0,
    ok: bool = True,
    error: str = "",
    root: str | Path | None = None,
    day: str | None = None,
) -> None:
    """记录一次下载（线程安全）。失败只降级为日志，绝不影响同步。"""
    global _BUFFER_COUNT
    try:
        resolved_root = _resolve_root(root)
        day_key = day or date.today().strftime("%Y%m%d")
        with _LOCK:
            per_day = _BUFFER.setdefault((resolved_root, day_key), {})
            stats = per_day.setdefault(
                dataset,
                {"bytes": 0, "files": 0, "errors": 0, "seconds": 0.0},
            )
            stats["bytes"] += max(0, int(nbytes or 0))
            stats["files"] += 1
            stats["seconds"] = round(
                float(stats.get("seconds", 0.0)) + max(0.0, float(seconds or 0.0)), 3
            )
            if not ok:
                stats["errors"] += 1
                if error:
                    stats["last_error"] = str(error)[:200]
            _BUFFER_COUNT += 1
            if (
                _BUFFER_COUNT >= FLUSH_EVERY
                or time.monotonic() - _LAST_FLUSH >= FLUSH_SECONDS
            ):
                _flush_locked()
    except Exception as exc:  # noqa: BLE001 - 观测面绝不影响下载
        logger.warning("记录 QuantDB 用量失败: %s", exc)


def flush() -> None:
    """把当前 buffer 写盘（幂等）。"""
    try:
        with _LOCK:
            _flush_locked()
    except Exception as exc:  # noqa: BLE001
        logger.warning("flush QuantDB 用量失败: %s", exc)


def load_usage(
    root: str | Path | None = None,
    *,
    days: int = 7,
    today: date | None = None,
) -> dict[str, Any]:
    """聚合最近 N 天用量。文件缺失/损坏跳过，不抛异常。"""
    resolved_root = _resolve_root(root)
    today = today or date.today()
    days = max(1, int(days))

    rows: list[dict[str, Any]] = []
    totals: dict[str, Any] = {
        "bytes": 0,
        "files": 0,
        "errors": 0,
        "seconds": 0.0,
        "datasets": {},
    }
    for offset in range(days - 1, -1, -1):
        day = (today - timedelta(days=offset)).strftime("%Y%m%d")
        path = _usage_path(resolved_root, day)
        if not path.is_file():
            continue
        payload = _read_usage(path, day)
        if payload is None:
            continue
        datasets = payload.get("datasets") or {}
        row = {
            "date": day,
            "datasets": datasets,
            "total_bytes": sum(int(v.get("bytes", 0)) for v in datasets.values()),
            "total_files": sum(int(v.get("files", 0)) for v in datasets.values()),
            "errors": sum(int(v.get("errors", 0)) for v in datasets.values()),
            "updated_at": payload.get("updated_at"),
        }
        rows.append(row)
        totals["bytes"] += row["total_bytes"]
        totals["files"] += row["total_files"]
        totals["errors"] += row["errors"]
        totals["seconds"] = round(
            totals["seconds"]
            + sum(float(v.get("seconds", 0.0)) for v in datasets.values()),
            3,
        )
        for dataset, stats in datasets.items():
            entry = totals["datasets"].setdefault(
                dataset,
                {"bytes": 0, "files": 0, "errors": 0, "seconds": 0.0},
            )
            _merge_stats(entry, stats)

    return {
        "root": resolved_root,
        "days_requested": days,
        "days": rows,
        "totals": totals,
        "generated_at": _now_iso(),
    }


atexit.register(flush)
