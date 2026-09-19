"""因子研究 —— 快照（artifact）读取层（支持多数据集）。

数据集：
- ``classic``（默认）：demo 复刻 82 因子，产物由 ``build_factor_research.py`` 写
  ``<quantdb>/factor_research/``（长表打分 + 经典目录 catalog.py）；
- ``private``：筛选最终保留 327 因子，产物由 ``build_factor_panel_private.py`` 写
  ``<quantdb>/factor_research_private/``（宽表打分 + factors.json 目录）。

本层只读 + 短 TTL 缓存，不触发计算。
"""

from __future__ import annotations

import json
import math
import threading
import time
from pathlib import Path

import pandas as pd

TTL_SECONDS = 600  # 快照读取缓存（构建脚本重跑后 10 分钟内自动失效）
_cache: dict[str, tuple[float, object]] = {}
_lock = threading.Lock()

# 数据集 → 产物目录名
DATASET_DIRS = {"classic": "factor_research", "private": "factor_research_private"}


def _sanitize(o):
    """NaN/Inf → None（快照 JSON 由 pandas 写出，可能含非有限值；FastAPI 序列化会 500）。"""
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, dict):
        return {k: _sanitize(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_sanitize(v) for v in o]
    return o


def artifact_dir(dataset: str = "classic") -> Path:
    from backend.shared.quantdb_paths import resolve_quantdb_dir

    return resolve_quantdb_dir() / DATASET_DIRS.get(dataset, dataset)


def _cached_obj(key: str, loader):
    """按 key 的 TTL 缓存读取（key 需含文件路径与过滤参数）。"""
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < TTL_SECONDS:
            return hit[1]
    obj = loader()
    with _lock:
        _cache[key] = (now, obj)
    return obj


def _file_key(p: Path, prefix: str, extra: str = "") -> str:
    """缓存 key = 路径 + mtime + 参数 —— 构建脚本重跑后旧缓存立即失效（不受 TTL 限制）。"""
    try:
        mtime = p.stat().st_mtime_ns
    except OSError:
        mtime = 0
    return f"{prefix}:{p}:{mtime}:{extra}"


def panel_stamp(dataset: str = "classic") -> str:
    """面板快照标识（路径 + mtime）。

    给上层当缓存键用：重建脚本一落盘，键就变，上层那份重算几十秒的上下文自动作废 ——
    不需要再靠 TTL 猜什么时候过期。
    """
    return _file_key(artifact_dir(dataset) / "factor_panel.parquet", "obj")


def load_json(name: str, dataset: str = "classic"):
    p = artifact_dir(dataset) / name

    def _rd():
        if not p.exists():
            return None
        return _sanitize(json.loads(p.read_text(encoding="utf-8")))

    return _cached_obj(_file_key(p, "json"), _rd)


def load_parquet(name: str, dataset: str = "classic", **kwargs) -> pd.DataFrame | None:
    p = artifact_dir(dataset) / name

    def _rd():
        if not p.exists():
            return None
        return pd.read_parquet(p, **kwargs)

    return _cached_obj(
        _file_key(p, "pq", repr(sorted(kwargs.items(), key=str))), _rd
    )


def metrics(dataset: str = "classic") -> dict:
    return load_json("metrics.json", dataset) or {
        "leaderboard": [],
        "metrics": {},
        "meta": {},
    }


def factors_meta(dataset: str = "private") -> dict | None:
    """私人因子库目录（factors.json：factors/l1_order/l2_order/meta）。"""
    return load_json("factors.json", dataset)


def holdings() -> dict:
    return load_json("holdings_latest.json") or {"date": None, "holdings": {}}


def ic_table(dataset: str = "classic") -> pd.DataFrame | None:
    return load_parquet("ic.parquet", dataset)


def corr_table() -> pd.DataFrame | None:
    return load_parquet("corr.parquet")


def benchmark_table(dataset: str = "classic") -> pd.DataFrame | None:
    """基准净值长表（trade_date, nav, index_code：沪深300/中证800/中证500）。"""
    return load_parquet("benchmarks.parquet", dataset)


def panel(dataset: str = "classic"):
    """月末名次面板（scorecard.Panel；进程内缓存）。"""
    from backend.services.engine.factor_research import scorecard

    p = artifact_dir(dataset) / "factor_panel.parquet"

    def _rd():
        if not p.exists():
            return None
        return scorecard.Panel(pd.read_parquet(p))

    return _cached_obj(_file_key(p, "obj"), _rd)


def stock_snapshot() -> pd.DataFrame | None:
    """最新截面个股元数据（名称/申万行业/市值/PE/PB/近一年日均成交额）。

    数据集无关（股票池元数据同一份；优先经典目录，私人库回退经典）。
    """
    df = load_parquet("stock_snapshot.parquet", "classic")
    if df is None:
        df = load_parquet("stock_snapshot.parquet", "private")
    return df


def scores_for(codes: list[str], dataset: str = "classic") -> pd.DataFrame | None:
    """按因子读取月末打分（长表：trade_date/symbol/factor_code/score）。

    classic：长表 parquet 行组过滤；private：宽表按列裁剪 + melt（不缓存，避免大对象驻留）。
    """
    codes = list(codes)
    if dataset != "private":
        return load_parquet(
            "monthly_scores.parquet",
            dataset,
            filters=[("factor_code", "in", codes)],
        )
    p = artifact_dir("private") / "monthly_scores.parquet"
    if not p.exists():
        return None
    import pyarrow.parquet as pq

    names = set(pq.ParquetFile(p).schema.names)
    cols = [c for c in codes if c in names]
    if not cols:
        return pd.DataFrame()
    df = pd.read_parquet(p, columns=["trade_date", "symbol", *cols])
    return df.melt(
        id_vars=["trade_date", "symbol"],
        value_vars=cols,
        var_name="factor_code",
        value_name="score",
    ).dropna(subset=["score"])


def fwd_returns(dataset: str = "classic") -> pd.DataFrame | None:
    return load_parquet("fwd_returns.parquet", dataset)


def screening() -> dict:
    """因子筛选结果（screen_factors.py 产物；缺失时返回空结构）。"""
    return load_json("screening/factor_selection.json") or {
        "counts": {
            "candidates": 0,
            "kept": 0,
            "gated_out": 0,
            "deduped": 0,
            "total_considered": 0,
        },
        "kept": [],
        "dropped_gated": [],
        "dropped_duplicate": [],
        "gates": {},
        "cross_corr": "",
        "generated_at": None,
    }


def clear_cache() -> None:
    with _lock:
        _cache.clear()
