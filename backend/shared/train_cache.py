"""训练数据缓存（D3）：同源、同特征、同窗口的重复训练不重复读盘/构标签。

缓存键必须包含所有会改变结果帧的输入：
  factor_source / 源指纹（schema_hash + max_date + 文件数 + 最新分区 size:mtime）
  / 日期窗 / horizon / target_mode / 排序后的特征列表 / 跨库映射
  / 股票池哈希 / industry_as_feature / cache_version / code_version。

任何一项缺失都可能造成脏读，所以宁可多 miss。缓存只在 ``load_data`` 的
直读因子源路径生效（旧 parquet 路径不缓存，避免行为变化）。写入原子
（tmp+rename），总量按 LRU 限制（默认 5GB，``QM_TRAIN_CACHE_MAX_GB``）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import pandas as pd

logger = logging.getLogger("quantmind.train")

CACHE_VERSION = "v1"
DEFAULT_MAX_GB = 5.0
META_KEYS = {"symbol", "trade_date", "label"}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def source_stamp(status: Any, source_path: str | Path | None) -> str:
    """源侧轻量指纹：schema/max_date/文件数 + 最新分区 size:mtime。

    不做 GB 级内容哈希；最新分区 mtime/size 变化足以发现「同日重写」。
    """
    schema = str(getattr(status, "schema_hash", "") or "")
    max_date = str(getattr(status, "max_date", "") or "")
    files = int(getattr(status, "files", 0) or 0)
    latest = ""
    if source_path:
        try:
            parts = [
                p
                for p in Path(source_path).glob("dt=*")
                if (p / "data.parquet").is_file()
            ]
            if parts:
                part = max(parts, key=lambda p: p.name)
                data = part / "data.parquet"
                stat = data.stat()
                latest = f"{part.name}:{stat.st_size}:{int(stat.st_mtime)}"
        except OSError:
            latest = ""
    return f"{schema}|{max_date}|{files}|{latest}"


def build_cache_key(
    *,
    market: str,
    factor_source: str,
    source_stamp_value: str,
    train_start: Any,
    train_end: Any,
    valid_end: Any = None,
    test_end: Any = None,
    horizon: int = 1,
    target_mode: str = "return",
    features: list[str] | None = None,
    factor_field_sources: dict[str, str] | None = None,
    pool_symbols: list[str] | None = None,
    industry_as_feature: bool = False,
    code_version: str = "",
) -> str:
    features = list(features or [])
    feat_hash = _sha("\n".join(sorted(features)))
    ffs = ";".join(f"{k}={v}" for k, v in sorted((factor_field_sources or {}).items()))
    pool_hash = _sha("\n".join(sorted(str(s) for s in (pool_symbols or []))))[:16]
    payload = "|".join(
        [
            CACHE_VERSION,
            str(code_version or os.getenv("QM_TRAIN_CODE_VERSION", "")),
            str(market).upper(),
            str(factor_source),
            str(source_stamp_value),
            str(train_start),
            str(train_end),
            str(valid_end or ""),
            str(test_end or ""),
            str(int(horizon)),
            str(target_mode or "return"),
            feat_hash,
            _sha(ffs),
            pool_hash,
            str(bool(industry_as_feature)),
        ]
    )
    return _sha(payload)


def _paths(cache_dir: str | Path, key: str) -> tuple[Path, Path]:
    base = Path(cache_dir)
    return base / f"train_{key}.parquet", base / f"train_{key}.json"


def read_cache(
    cache_dir: str | Path, key: str
) -> tuple[pd.DataFrame, list[str]] | None:
    """命中返回 (df, features)；任何不完整/不匹配都 miss（返回 None）。"""
    parquet_path, meta_path = _paths(cache_dir, key)
    if not parquet_path.is_file() or not meta_path.is_file():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(meta, dict):
        return None
    if meta.get("key") != key or meta.get("cache_version") != CACHE_VERSION:
        return None
    features = [str(f) for f in (meta.get("features") or [])]
    if not features:
        return None
    try:
        frame = pd.read_parquet(parquet_path)
    except Exception:  # noqa: BLE001 - 缓存损坏按 miss
        return None
    if not META_KEYS <= set(frame.columns):
        return None
    if not set(features) <= set(frame.columns):
        return None
    # parquet 往返会改变字符串列的 dtype（object ↔ StringDtype）以及日期列；
    # 按写侧记录的 dtypes 逐列还原，保证命中帧与正常加载帧逐列一致。
    dtypes = meta.get("dtypes")
    if isinstance(dtypes, dict):
        for column, dtype in dtypes.items():
            if column in frame.columns:
                try:
                    frame[column] = frame[column].astype(dtype)
                except (TypeError, ValueError):
                    pass
    return frame, features


def _prune(base: Path, limit_bytes: int) -> None:
    try:
        files = sorted(base.glob("train_*.parquet"), key=lambda p: p.stat().st_mtime)
    except OSError:
        return
    total = 0
    for path in files:
        try:
            total += path.stat().st_size
        except OSError:
            continue
    for path in files:
        if total <= limit_bytes:
            break
        try:
            size = path.stat().st_size
            path.unlink()
            path.with_suffix(".json").unlink(missing_ok=True)
            total -= size
        except OSError:
            continue


def write_cache(
    cache_dir: str | Path,
    key: str,
    frame: pd.DataFrame,
    features: list[str],
    *,
    meta_extra: dict[str, Any] | None = None,
    max_total_gb: float | None = None,
) -> Path | None:
    """原子写缓存；超过总量上限/写失败返回 None（best-effort，不影响训练）。"""
    try:
        base = Path(cache_dir)
        base.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    parquet_path, meta_path = _paths(base, key)
    tmp = parquet_path.with_suffix(".parquet.tmp")
    try:
        frame.to_parquet(tmp, index=False)
        os.replace(tmp, parquet_path)
    except Exception as exc:  # noqa: BLE001 - 缓存失败不炸训练
        tmp.unlink(missing_ok=True)
        logger.warning("训练缓存写入失败（忽略）: %s", exc)
        return None

    limit_gb = (
        float(max_total_gb)
        if max_total_gb is not None
        else float(
            os.getenv("QM_TRAIN_CACHE_MAX_GB", str(DEFAULT_MAX_GB)) or DEFAULT_MAX_GB
        )
    )
    limit_bytes = max(0, int(limit_gb * 1024**3))
    try:
        size = parquet_path.stat().st_size
    except OSError:
        size = 0
    if limit_bytes and size > limit_bytes:
        parquet_path.unlink(missing_ok=True)
        logger.warning(
            "训练缓存单文件 %.1f GB 超过上限 %.1f GB，不缓存", size / 1024**3, limit_gb
        )
        return None

    meta = {
        "key": key,
        "cache_version": CACHE_VERSION,
        "features": list(features),
        "rows": int(len(frame)),
        "bytes": int(size),
        "dtypes": {str(col): str(frame[col].dtype) for col in frame.columns},
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        **(meta_extra or {}),
    }
    try:
        meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    _prune(base, limit_bytes)
    return parquet_path
