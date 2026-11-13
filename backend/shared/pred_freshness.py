"""pred.parquet 新鲜度：路径解析 + 最后交易日（Parquet footer 统计优先）。

模型下拉、分数序列、健康检查都需要回答「这个模型最后一次产出分数是哪天」。
统一放在这里，避免各路由各写一份（一份用 DuckDB 全列 max、一份用 mtime 近似，
两处口径会漂移）。

性能：先读 Parquet footer 的列统计（每个 row group 的 max，几 KB），统计缺失
才回退 DuckDB `MAX(CAST(date AS DATE))`。结果按文件 mtime 缓存，文件不变不重读。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: 文件路径 → (mtime, last_date)
_LAST_DATE_CACHE: dict[str, tuple[float, str | None]] = {}
_CACHE_MAX = 512


def pred_parquet_path(storage_path: str | None) -> Path | None:
    """模型目录里实际存在的 pred.parquet（兼容根目录与 pred/ 子目录）。"""
    raw = str(storage_path or "").strip()
    if not raw:
        return None
    try:
        base = Path(raw)
    except (TypeError, ValueError):
        return None
    for candidate in (base / "pred.parquet", base / "pred" / "pred.parquet"):
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def pred_last_date(storage_path: str | None) -> str | None:
    """pred.parquet 的最新 trade_date（YYYY-MM-DD）；无产物/读取失败返回 None。"""
    parquet_file = pred_parquet_path(storage_path)
    if parquet_file is None:
        return None
    cache_key = str(parquet_file)
    try:
        mtime = parquet_file.stat().st_mtime
    except OSError:
        return None
    hit = _LAST_DATE_CACHE.get(cache_key)
    if hit and hit[0] == mtime:
        return hit[1]

    result: str | None = None
    # 快路径：Parquet footer 统计（不扫数据）
    try:
        import pyarrow.parquet as pq

        pf = pq.ParquetFile(str(parquet_file))
        names = list(pf.schema_arrow.names)
        date_col = next(
            (c for c in ("trade_date", "date", "datetime") if c in names), None
        )
        if date_col:
            col_idx = names.index(date_col)
            max_values: list[Any] = []
            for rg in range(pf.metadata.num_row_groups):
                stats = pf.metadata.row_group(rg).column(col_idx).statistics
                if stats is None or stats.max is None:
                    max_values = []
                    break
                max_values.append(stats.max)
            if max_values:
                result = str(max(max_values))[:10]
    except Exception:  # noqa: BLE001 - 统计不可用/老文件回退全列 max
        result = None

    # 慢路径：DuckDB max（只在 footer 统计缺失时走）
    if result is None:
        try:
            import duckdb

            escaped = str(parquet_file).replace("'", "''")
            con = duckdb.connect()
            try:
                cols = [
                    r[0]
                    for r in con.execute(
                        f"SELECT * FROM read_parquet('{escaped}') LIMIT 0"
                    ).description
                ]
                date_col = next(
                    (c for c in ("trade_date", "date", "datetime") if c in cols), None
                )
                if date_col:
                    row = con.execute(
                        f"SELECT MAX(CAST({date_col} AS DATE)) "
                        f"FROM read_parquet('{escaped}')"
                    ).fetchone()
                    if row and row[0] is not None:
                        result = str(row[0])[:10]
            finally:
                try:
                    con.close()
                except Exception:  # noqa: BLE001
                    pass
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取 pred.parquet 最后日期失败 %s: %s", parquet_file, exc)

    if len(_LAST_DATE_CACHE) >= _CACHE_MAX:
        now = time.time()
        stale = [
            k
            for k, (at, _v) in _LAST_DATE_CACHE.items()
            if now - at >= 3600
        ]
        for k in stale:
            _LAST_DATE_CACHE.pop(k, None)
        if len(_LAST_DATE_CACHE) >= _CACHE_MAX:
            _LAST_DATE_CACHE.clear()
    _LAST_DATE_CACHE[cache_key] = (mtime, result)
    return result
