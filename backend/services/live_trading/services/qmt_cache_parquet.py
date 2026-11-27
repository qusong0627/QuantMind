# -*- coding: utf-8 -*-
"""读取桥侧 `qmt_today_cache` 的 Parquet（后端 DuckDB/PyArrow 只读）。

路径：Windows `qmt-tools\\data\\cache\\qmt` 通过 `//192.168.31.250/PYPlugins`
共享挂载在容器 `/mnt/tdx-shared/bridge-windows/qmt-tools/data/cache/qmt`。
可用 ``QM_QMT_CACHE_DIR`` 覆盖。

只用于盘中宽度/复盘/研究读取；**订单路径不要直接读跨网 CIFS Parquet**。
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

CN_TZ = timezone(timedelta(hours=8))
DEFAULT_CACHE_DIR = "/mnt/tdx-shared/bridge-windows/qmt-tools/data/cache/qmt"
KINDS = {"bar1m", "snapshot3s", "tick_hot"}
DATE_RE = re.compile(r"^\d{8}$")
SYMBOL_RE = re.compile(r"^[0-9A-Za-z]{1,12}(?:\.[A-Za-z]{2})?$")


def cache_root() -> str:
    return os.environ.get("QM_QMT_CACHE_DIR") or DEFAULT_CACHE_DIR


def _pattern(kind: str, date: str) -> str:
    if kind not in KINDS or not DATE_RE.match(str(date or "")):
        raise ValueError("bad kind/date")
    root = os.path.abspath(cache_root())
    return os.path.join(root, "date=%s" % date, "type=%s" % kind, "hour=*", "part-*.parquet")


def _ts_column(kind: str) -> str:
    return "minute_ts" if kind == "bar1m" else "ts_ms"


def _clean_symbols(symbols) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for code in symbols or []:
        text = str(code or "").strip().upper()
        if not text or not SYMBOL_RE.match(text) or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def query(
    kind: str,
    date: str,
    *,
    symbols=None,
    start_ms: int | None = None,
    end_ms: int | None = None,
    limit: int = 200000,
) -> list[dict]:
    """查询当天 Parquet part；失败/无文件返回 []（调用方回落其他源）。"""
    try:
        pattern = _pattern(kind, date)
    except ValueError:
        return []
    root = os.path.abspath(cache_root())
    base = os.path.join(root, "date=%s" % date, "type=%s" % kind)
    if not os.path.isdir(base):
        return []
    ts_col = _ts_column(kind)
    where: list[str] = []
    params: list = []
    codes = _clean_symbols(symbols)
    if codes:
        where.append("symbol IN (%s)" % ",".join("?" for _ in codes))
        params.extend(codes)
    if start_ms is not None:
        where.append("%s >= ?" % ts_col)
        params.append(int(start_ms))
    if end_ms is not None:
        where.append("%s <= ?" % ts_col)
        params.append(int(end_ms))
    sql = "SELECT * FROM read_parquet('%s', hive_partitioning=false)" % pattern.replace("'", "''")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY %s LIMIT %d" % (ts_col, max(1, int(limit)))
    try:
        import duckdb

        con = duckdb.connect()
        try:
            rows = con.execute(sql, params).fetchdf().to_dict("records")
        finally:
            con.close()
        return rows
    except Exception as exc:  # noqa: BLE001 - DuckDB 不可用/文件忙只降级
        logger.warning("[QmtCacheParquet] DuckDB 读取失败 %s/%s: %s", kind, date, exc)
    try:
        import pyarrow.dataset as ds

        dataset = ds.dataset(base, format="parquet", partitioning=None)
        table = dataset.to_table(limit=max(1, int(limit)))
        return table.to_pylist()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[QmtCacheParquet] PyArrow 读取失败 %s/%s: %s", kind, date, exc)
    return []


def query_snapshot3s(date: str, **kwargs) -> list[dict]:
    return query("snapshot3s", date, **kwargs)


def query_bar1m(date: str, **kwargs) -> list[dict]:
    return query("bar1m", date, **kwargs)


def query_tick_hot(date: str, **kwargs) -> list[dict]:
    return query("tick_hot", date, **kwargs)


def today() -> str:
    return datetime.now(CN_TZ).strftime("%Y%m%d")
