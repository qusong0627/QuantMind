"""模型 pred 一级证据的 sidecar 缓存（夜间批量的前提：产物没变就不重读 GB 级 parquet）。

`pred.parquet` 是百 MB 级（实测 54–127 MB），每个模型每晚重读一次不可接受。
命中判定是 **版本 + (mtime_ns, size) 双匹配**：产物一变就失效。

v2 起缓存里多存一份**长序列载荷**（`model_series.series_from_stats` 的输出）——
`/eval/series` 侧车的数据源就是它。缓存命中也就不必重算序列；v1 老缓存没有
这一项，由 `read_cache_series` 返回 None 让上层如实降级（不假装有序列）。

缓存损坏/写不进去**只降级为「未命中」**：正确性不受影响，但写失败必须留日志。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from backend.shared.eval_scoring import DimensionScore

CACHE_NAME = ".eval_cache.json"
# v2：缓存里多了长序列载荷（序列侧车的数据源）——v1 缓存没有它，必须重算一次
CACHE_VERSION = 2


# ── sidecar 缓存（夜间批量的前提：产物没变就不重读 GB 级 parquet） ──────

CACHE_NAME = ".eval_cache.json"
# v2：缓存里多了长序列载荷（序列侧车的数据源）——v1 缓存没有它，必须重算一次
CACHE_VERSION = 2


def _signature(path: Path) -> list[int]:
    st = path.stat()
    return [int(st.st_mtime_ns), int(st.st_size)]


def _dims_to_payload(dims: dict[str, DimensionScore]) -> dict[str, Any]:
    return {
        k: {
            "label": d.label,
            "weight": d.weight,
            "score": d.score,
            "red_line_failed": d.red_line_failed,
            "detail": d.detail,
        }
        for k, d in dims.items()
    }


def _dims_from_payload(payload: dict[str, Any]) -> dict[str, DimensionScore]:
    # 标签/权重缺失才回落（v2 缓存必带）。口径的唯一出处是 `model_realized`
    # 的 DIM_LABELS/DIM_WEIGHTS——延迟导入避免两模块成环。
    from backend.scripts.eval.model_realized import DIM_LABELS, DIM_WEIGHTS

    return {
        key: DimensionScore(
            key,
            str(raw.get("label") or DIM_LABELS.get(key, key)),
            float(raw.get("weight") or DIM_WEIGHTS.get(key, 0.0)),
            None if raw.get("score") is None else float(raw["score"]),
            bool(raw.get("red_line_failed")),
            dict(raw.get("detail") or {}),
        )
        for key, raw in payload.items()
    }


def _read_cache_entry(model_dir: Path, pred_path: Path) -> dict[str, Any] | None:
    """sidecar 命中判定：版本 + (mtime_ns, size) 双匹配才算数。"""
    cache_file = model_dir / CACHE_NAME
    if not cache_file.is_file():
        return None
    try:
        payload = json.loads(cache_file.read_text(encoding="utf-8"))
        entry = payload.get("pred") or {}
        if payload.get("version") != CACHE_VERSION or not entry.get("dims"):
            return None
        if entry.get("signature") != _signature(pred_path):
            return None
        return entry
    except (OSError, ValueError, KeyError, TypeError):
        return None  # 缓存损坏只降级为「未命中」，正确性不受影响


def read_cache(model_dir: Path, pred_path: Path) -> dict[str, DimensionScore] | None:
    """缓存里的四维（未命中 → None）。"""
    entry = _read_cache_entry(model_dir, pred_path)
    return None if entry is None else _dims_from_payload(entry["dims"])


def read_cache_series(model_dir: Path, pred_path: Path) -> dict[str, Any] | None:
    """缓存里的长序列载荷（v1 缓存无此项 → None，调用方决定重算或如实缺省）。"""
    entry = _read_cache_entry(model_dir, pred_path)
    if entry is None:
        return None
    series = entry.get("series")
    return series if isinstance(series, dict) else None


def write_cache(
    model_dir: Path,
    pred_path: Path,
    dims: dict[str, DimensionScore],
    series: dict[str, Any] | None = None,
) -> None:
    """原子写 sidecar；写不进去只影响性能，但必须留日志（不静默）。"""
    import logging

    body = {
        "version": CACHE_VERSION,
        "pred": {
            "signature": _signature(pred_path),
            "source": str(pred_path),
            "dims": _dims_to_payload(dims),
            "series": series or {},
        },
    }
    tmp = (model_dir / CACHE_NAME).with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(model_dir / CACHE_NAME)
    except OSError:
        logging.getLogger(__name__).warning(
            "eval 缓存写入失败: %s", model_dir / CACHE_NAME
        )
