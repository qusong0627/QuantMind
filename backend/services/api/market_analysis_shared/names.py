"""证券名称映射 — 由各市场的标的池 parquet 构建 symbol -> 中文名 dict。

约定：标的池 parquet 统一含 (symbol, cn_name[, en_name]) 列；
港股为 security_master/data.parquet，其他市场引入时遵守同一形状即可。
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@lru_cache(maxsize=16)
def build_name_map(
    parquet_path: str | Path, symbol_col: str = "symbol"
) -> dict[str, str]:
    """读取标的池 parquet，返回 symbol(suffix) -> 中文名。

    企业要求：优先 cn_name，缺省回退 en_name，最终回退 symbol 本身
    （保证展示层永远有可显示的名字，不产生空单元格）。
    """
    import duckdb

    con = duckdb.connect()
    try:
        cursor = con.execute(
            f"SELECT {symbol_col}, cn_name, en_name FROM read_parquet('{parquet_path}')"
        )
        mapping: dict[str, str] = {}
        for row in cursor.fetchall():
            sym = str(row[0])
            cn = row[1]
            en = row[2]
            name = (
                str(cn)
                if cn not in (None, "")
                else (str(en) if en not in (None, "") else sym)
            )
            mapping[sym] = name
        return mapping
    except Exception as exc:  # 标的池丢失不应让整个市场分析崩溃
        logger.warning("读取标的池失败 %s: %s", parquet_path, exc)
        return {}
    finally:
        con.close()


def map_names(symbols: list[str], name_map: dict[str, str]) -> list[str]:
    """批量映射，缺失回退 symbol 本身。"""
    return [name_map.get(s, s) for s in symbols]


def resolve_name(symbol: str, name_map: dict[str, str]) -> str:
    """单只映射，缺失回退 symbol 自身。"""
    return name_map.get(symbol, symbol)


def names_map_union(*mappings: dict[str, str]) -> dict[str, str]:
    """多来源合并（后者不覆盖前者）。"""
    merged: dict[str, str] = {}
    for m in mappings:
        for k, v in m.items():
            merged.setdefault(k, v)
    return merged
