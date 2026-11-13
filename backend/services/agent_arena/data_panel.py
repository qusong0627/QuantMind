"""arena 数据面 · 族 9 服务层：多市场 parquet 仓库浏览 / 预览（原生实现）。

上游 baymax ``backend/services/data_platform.py``（475 行）的迁移版。上游当年
就把 QM 数据管理台的目录统计 / 预览逻辑照抄了一份、数据根直接指到 QM 的
``/data/*``；本版**不再抄规格**，数据集清单 / 分组 / 数据根解析全部直连 QM
规范源——QM 加数据集、挪目录，这里自动跟随：

- quantdb → ``backend.shared.quantdb_datasets`` 的 ``DATASETS`` + ``GROUPS``
- quantus / quanthk / quantfutures → ``admin.global_market_console`` 的
  ``_default_datasets("US"/"HK"/"FUTURES")``（与数据管理台同一份可用集合）
- 数据根 → 用户自定义（状态文件）优先，否则 QM 原生解析链：quantdb 走
  ``quantdb_paths.resolve_quantdb_dir``，其余走 ``global_market_console._data_dir``
  同款 env → 默认目录 → hub resolver

对外形状与上游逐位相同（catalog / scan / preview 的字段名、日期格式、JSON
化规则），前端 ``arena/pages/DataPlatform.tsx`` 零改动。状态文件落
``arena_config.arena_dir()/data_platform.json``（本地树，不进公开仓；函数调用
而非模块常量——import 固化路径是既有教训）。
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MAX_PREVIEW_ROWS = 200
MAX_SYMBOL_CHOICES = 500

#: catalog 结果缓存秒数（上游同款：树大了 rglob 扫描不便宜，切 tab 防抖）。
#: 目录统计是只读展示面，30 秒陈旧可接受；POST root 改根时主动失效。
_CATALOG_TTL_S = 30.0
_catalog_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_catalog_lock = threading.Lock()

MARKETS: list[dict[str, Any]] = [
    {
        "id": "quantdb",
        "label": "A股市场",
        "code": "QuantDB",
        "flag": "🇨🇳",
        "beta": False,
        "default_root": "/data/quantdb",
    },
    {
        "id": "quanthk",
        "label": "港股市场",
        "code": "QuantHK",
        "flag": "🇭🇰",
        "beta": True,
        "default_root": "/data/quanthk",
    },
    {
        "id": "quantus",
        "label": "美股市场",
        "code": "QuantUS",
        "flag": "🇺🇸",
        "beta": True,
        "default_root": "/data/quantus",
    },
    {
        "id": "quantfutures",
        "label": "国内期货",
        "code": "QuantFutures",
        "flag": "⚡",
        "beta": True,
        "default_root": "/data/quantfutures",
    },
]

MARKET_IDS: frozenset[str] = frozenset(m["id"] for m in MARKETS)

#: 非 quantdb 市场的 env 变量 / 默认目录（与各自 console 路由逐字一致）。
_ENV_ROOTS: dict[str, tuple[str, str]] = {
    "quanthk": ("QM_QUANTHK_DATA_DIR", "/data/quanthk"),
    "quantus": ("QM_QUANTUS_DATA_DIR", "/data/quantus"),
    "quantfutures": ("QM_QUANTFUTURES_DATA_DIR", "/data/quantfutures"),
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# 规格源（直连 QM 规范，不复制）
# ---------------------------------------------------------------------------


def _specs(market: str) -> tuple[Any, ...]:
    """市场的数据集规格元组（DatasetSpec 形状；四种源字段同名同序）。"""
    if market == "quantdb":
        from backend.shared.quantdb_datasets import DATASETS

        return DATASETS
    from backend.services.api.routers.admin.global_market_console import (
        _default_datasets,
    )

    return _default_datasets(
        {"quanthk": "HK", "quantus": "US", "quantfutures": "FUTURES"}[market]
    )


def _groups(market: str) -> list[dict[str, str]]:
    if market == "quantdb":
        from backend.shared.quantdb_datasets import GROUPS

        return GROUPS
    from backend.services.api.routers.admin.global_market_console import _GROUPS

    return _GROUPS


# ---------------------------------------------------------------------------
# 数据根目录：用户自定义（状态文件）→ QM 原生解析链
# ---------------------------------------------------------------------------


def _state_file() -> Path:
    from backend.services.agent_arena.arena_config import arena_dir

    return arena_dir() / "data_platform.json"


def _load_state() -> dict[str, str]:
    try:
        return json.loads(_state_file().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(state: dict[str, str]) -> None:
    path = _state_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as exc:
        logger.warning("arena data_panel state 写入失败: %s", exc)


def _native_root(market: str) -> Path:
    """QM 原生数据根（数据管理台读的那一份）。"""
    if market == "quantdb":
        from backend.shared.quantdb_paths import resolve_quantdb_dir

        return resolve_quantdb_dir()
    from backend.services.api.routers.admin.global_market_console import _data_dir

    env_var, default_dir = _ENV_ROOTS[market]
    return _data_dir(env_var, default_dir)


def data_root(market: str) -> Path:
    """当前数据根：用户自定义（state 文件）优先 → QM 原生解析。"""
    custom = _load_state().get(market, "").strip()
    return Path(custom) if custom else _native_root(market)


def set_data_root(market: str, root: str) -> str:
    state = _load_state()
    state[market] = root.strip()
    _save_state(state)
    invalidate_catalog(market)
    return state[market]


def invalidate_catalog(market: str) -> None:
    with _catalog_lock:
        _catalog_cache.pop(market, None)


# ---------------------------------------------------------------------------
# 目录统计
# ---------------------------------------------------------------------------


def _partition_dates(root: Path) -> list[str]:
    out = []
    for p in root.iterdir():
        if p.is_dir() and p.name.startswith("dt="):
            out.append(p.name[3:])
        elif p.is_dir() and p.name.startswith("quarter="):
            out.append(p.name[8:])
    out.sort()
    return out


_DATE_IN_NAME = re.compile(r"(20\d{6})")


def _dataset_dates(d: Path) -> list[str]:
    """按日数据集的可用日期（分区目录 + 平铺文件名两种形态都要计入）。"""
    dates = set(_partition_dates(d))
    for f in d.glob("*.parquet"):
        m = _DATE_IN_NAME.search(f.stem)
        if m:
            dates.add(m.group(1))
    return sorted(dates)


def _dataset_stats(spec: Any, root: Path) -> dict[str, Any]:
    d = root / spec.rel_dir
    if not d.is_dir():
        return {"synced": False, "files": 0, "size_mb": 0.0}
    files = [f for f in d.rglob("*.parquet") if f.is_file()]
    size_mb = round(sum(f.stat().st_size for f in files) / 1024 / 1024, 1)
    stats: dict[str, Any] = {
        "synced": bool(files),
        "files": len(files),
        "size_mb": size_mb,
    }
    if spec.layout == "partition":
        dates = _dataset_dates(d)
        if dates:
            stats["start_date"] = dates[0]
            stats["end_date"] = dates[-1]
            stats["partitions"] = len(dates)
    if files:
        latest = max(f.stat().st_mtime for f in files)
        stats["updated_at"] = (
            datetime.fromtimestamp(latest, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
    return stats


def build_catalog(market: str) -> dict[str, Any]:
    now = time.monotonic()
    with _catalog_lock:
        hit = _catalog_cache.get(market)
        if hit and now - hit[0] < _CATALOG_TTL_S:
            return hit[1]
    data = _build_catalog_uncached(market)
    with _catalog_lock:
        _catalog_cache[market] = (time.monotonic(), data)
    return data


def _build_catalog_uncached(market: str) -> dict[str, Any]:
    root = data_root(market)
    items = []
    for spec in _specs(market):
        items.append(
            {
                "dataset": spec.dataset,
                "name": spec.name,
                "group": spec.group,
                "category_id": spec.category_id,
                "layout": spec.layout,
                "rel_dir": spec.rel_dir,
                "note": spec.note,
                **_dataset_stats(spec, root),
            }
        )
    groups = []
    for g in _groups(market):
        members = [it for it in items if it["group"] == g["id"]]
        if not members:
            continue
        groups.append(
            {
                **g,
                "dataset_count": len(members),
                "synced_count": sum(1 for it in members if it["synced"]),
                "files": sum(it["files"] for it in members),
                "size_mb": round(sum(it["size_mb"] for it in members), 1),
            }
        )
    return {
        "market": market,
        "data_dir": str(root),
        "exists": root.is_dir(),
        "groups": groups,
        "datasets": items,
        "timestamp": _now_iso(),
    }


# ---------------------------------------------------------------------------
# 文件夹选择预检（scan）：识别 root 下的数据集 + 文件数/大小
# ---------------------------------------------------------------------------


def scan_folder(market: str, root_str: str) -> dict[str, Any]:
    root = Path(root_str.strip() or str(data_root(market)))
    if not root.is_dir():
        return {
            "root": str(root),
            "exists": False,
            "total_files": 0,
            "total_bytes": 0,
            "datasets": [],
            "unknown": [],
            "timestamp": _now_iso(),
        }

    datasets = []
    for spec in _specs(market):
        d = root / spec.rel_dir
        if not d.is_dir():
            continue
        files = [f for f in d.rglob("*.parquet") if f.is_file()]
        datasets.append(
            {
                "dataset": spec.dataset,
                "name": spec.name,
                "layout": spec.layout,
                "rel_dir": spec.rel_dir,
                "files": len(files),
                "bytes": sum(f.stat().st_size for f in files),
            }
        )

    # 未登记的目录（根下非规格内的一级数据目录，便于发现新数据）
    known_prefixes = {spec.rel_dir.split("/")[0] for spec in _specs(market)}
    unknown = []
    for d in sorted(
        p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")
    ):
        if d.name in known_prefixes:
            continue
        files = [f for f in d.rglob("*.parquet") if f.is_file()]
        if files:
            unknown.append(
                {
                    "rel_dir": d.name,
                    "files": len(files),
                    "bytes": sum(f.stat().st_size for f in files),
                }
            )

    all_files = sum(ds["files"] for ds in datasets) + sum(u["files"] for u in unknown)
    all_bytes = sum(ds["bytes"] for ds in datasets) + sum(u["bytes"] for u in unknown)
    return {
        "root": str(root),
        "exists": True,
        "total_files": all_files,
        "total_bytes": all_bytes,
        "datasets": datasets,
        "unknown": unknown,
        "timestamp": _now_iso(),
    }


# ---------------------------------------------------------------------------
# 数据预览（本地 parquet；JSON 化规则与 QM 数据管理台一致）
# ---------------------------------------------------------------------------


def _pick_local_file(spec: Any, root: Path, symbol: str | None) -> Path | None:
    d = root / spec.rel_dir
    if not d.is_dir():
        return None
    if spec.layout == "partition":
        dates = _partition_dates(d)
        for dt in reversed(dates):
            files = sorted((d / f"dt={dt}").glob("*.parquet"))
            if files:
                return files[0]
        flat = sorted(d.glob("*.parquet"))
        return flat[-1] if flat else None
    files = sorted(f for f in d.glob("*.parquet") if f.is_file())
    if not files:
        return None
    if symbol:
        target = symbol.strip().upper()
        for f in files:
            if f.stem.upper() == target:
                return f
        raise LookupError(f"{spec.dataset} 无 {symbol} 的本地文件")
    return files[0]


def _symbol_choices(spec: Any, root: Path) -> dict[str, Any]:
    if spec.layout != "symbol":
        return {}
    d = root / spec.rel_dir
    if not d.is_dir():
        return {}
    stems = sorted(f.stem for f in d.glob("*.parquet") if f.is_file())
    return {"symbol_total": len(stems), "symbol_choices": stems[:MAX_SYMBOL_CHOICES]}


def _json_safe(value: Any) -> Any:
    """parquet 单元格 → JSON 可序列化（ndarray/list/NaN/Inf/Timestamp 逐值处理）。"""
    import numpy as np
    import pandas as pd

    if value is None:
        return None
    if isinstance(value, (np.ndarray, list, tuple, set)):
        return [_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (np.integer, int)) and not isinstance(value, bool):
        return int(value)
    if isinstance(value, (np.floating, float)):
        f = float(value)
        return None if (f != f or f in (float("inf"), float("-inf"))) else f
    if isinstance(value, (np.bool_, bool)):
        return bool(value)
    if value is pd.NaT:  # NaT 是 datetime 子类，必须排在 Timestamp 分支之前
        return None
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, (str, bytes)):
        return value.decode("utf-8", "replace") if isinstance(value, bytes) else value
    return str(value)


def _json_safe_records(df: Any) -> list[dict[str, Any]]:
    return [
        {str(k): _json_safe(v) for k, v in row.items()}
        for row in df.to_dict(orient="records")
    ]


def preview_dataset(
    market: str, dataset: str, symbol: str | None = None, limit: int = 50
) -> dict[str, Any]:
    import pandas as pd

    specs = {s.dataset: s for s in _specs(market)}
    spec = specs.get(dataset)
    if spec is None:
        raise LookupError(f"未知数据集: {dataset}")
    root = data_root(market)
    file_path = _pick_local_file(spec, root, symbol)
    if file_path is None:
        raise LookupError(
            f"{dataset} 本地无数据（目录 {root / spec.rel_dir} 无 parquet 文件）"
        )

    df = pd.read_parquet(file_path)
    if df is None or df.empty:
        return {
            "dataset": dataset,
            "name": spec.name,
            "source": "local",
            "file": str(file_path.relative_to(root)),
            "rows_total": 0,
            "column_count": 0,
            "columns": [],
            "data": [],
            **_symbol_choices(spec, root),
            "timestamp": _now_iso(),
        }

    columns = [{"name": str(c), "dtype": str(df[c].dtype)} for c in df.columns]
    records = _json_safe_records(df.head(min(limit, MAX_PREVIEW_ROWS)))
    return {
        "dataset": dataset,
        "name": spec.name,
        "source": "local",
        "file": str(file_path.relative_to(root)),
        "rows_total": int(len(df)),
        "column_count": len(columns),
        "columns": columns,
        "data": records,
        **_symbol_choices(spec, root),
        "timestamp": _now_iso(),
    }
