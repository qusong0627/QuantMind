"""美股个股终端 —— Huntly SQLite 只读访问（资讯主路径的元数据 + 标题兜底）。

职责边界：本模块只管 Huntly SQLite（标题/链接/发布时间/来源名 + 标题 LIKE 兜底），
匹配词规则与 PG enrichment 查询在 `news.py`。

连接必须用 `file:{path}?immutable=1`：Huntly 的 Java 进程持有写锁，
`mode=ro` 连接会被阻塞（连 PRAGMA 都拿不到读锁）。已知风险：`immutable=1`
声称文件不变，而 Huntly 在持续写入 —— 并发写时读到撕裂页会抛
`database disk image is malformed`（实测遇到过一次）。本模块的调用方
（`news.py`）对兜底路径失败重试一次后降级为 available=false + 空 items。

扫描窗口：`page` 行内含正文大字段，全表扫一次冷启动实测 47s（AAPL 仅 224 条
标题命中，`ORDER BY id DESC` 的提前终止帮不上忙）。中文名命中密集、扫几行即满页，
不限窗口；代码匹配限定在最近 `_TICKER_SCAN_WINDOW` 行，兼顾「最近资讯不丢」与成本。
"""

from __future__ import annotations

import logging
import os
import sqlite3
from typing import Any
from collections.abc import Iterable

logger = logging.getLogger(__name__)

HUNTLY_SQLITE_PATH = "/data/huntly/db.sqlite"

# 代码关键词的扫描窗口（行数）
_TICKER_SCAN_WINDOW = 200_000


def db_path() -> str:
    return os.getenv("HUNTLY_SQLITE_PATH", HUNTLY_SQLITE_PATH)


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path()}?immutable=1", uri=True, timeout=3)
    conn.row_factory = sqlite3.Row
    return conn


def huntly_meta(ids: Iterable[int]) -> dict[int, dict[str, Any]]:
    """按 id 批量取 Huntly 页面（标题/链接/发布时间/来源名）；失败返回空映射。

    主路径（PG enrichment）靠它把 huntly_page_id 还原成可展示的资讯条目；
    取不到时仍可用 enrichment 表自带的 title 兜底。
    """
    id_list = [int(i) for i in ids]
    if not id_list:
        return {}
    placeholders = ",".join(["?"] * len(id_list))
    sql = (
        "SELECT p.id, p.title, p.url, p.connected_at, p.created_at, c.name AS source_name "
        "FROM page p LEFT JOIN connector c ON c.id = p.connector_id "
        f"WHERE p.id IN ({placeholders})"
    )
    try:
        conn = _connect()
        try:
            rows = conn.execute(sql, id_list).fetchall()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 - 主路径仍可只靠 PG 标题
        logger.warning("[stock-terminal-us] Huntly 批量取页面失败: %s", exc)
        return {}
    return {
        int(r["id"]): {
            "title": str(r["title"] or ""),
            "link": str(r["url"] or ""),
            "published_at": str(r["connected_at"] or r["created_at"] or "")[:19],
            "source": str(r["source_name"] or ""),
        }
        for r in rows
    }


def _recent_floor(conn: sqlite3.Connection) -> int:
    """代码匹配的扫描下界（最近 _TICKER_SCAN_WINDOW 行的最小 id）；取不到返回 0。"""
    try:
        row = conn.execute("SELECT max(id) FROM page").fetchone()
        return max(0, int(row[0] or 0) - _TICKER_SCAN_WINDOW)
    except sqlite3.Error:  # 取不到就退回不限窗口（正确性优先）
        return 0


def fetch_by_title(keywords: list[str], sym: str, limit: int) -> list[dict[str, Any]]:
    """兜底路径：标题 LIKE 逐词匹配，去重后按 id 倒序取前 limit 条。

    条目形状与 `news._to_item` 对齐（情绪标签字段留空，由调用方按 id 反查 PG 补全）。
    """
    items: list[dict[str, Any]] = []
    seen: set[int] = set()
    conn = _connect()
    try:
        floor = _recent_floor(conn)
        for kw in keywords:
            sql = (
                "SELECT p.id, p.title, p.url, p.connected_at, p.created_at, "
                "c.name AS source_name FROM page p "
                "LEFT JOIN connector c ON c.id = p.connector_id WHERE p.title LIKE ?"
            )
            params: list[Any] = [f"%{kw}%"]
            if kw == sym and floor:
                sql += " AND p.id > ?"
                params.append(floor)
            rows = conn.execute(
                sql + " ORDER BY p.id DESC LIMIT ?", [*params, limit]
            ).fetchall()
            for r in rows:
                rid = int(r["id"])
                if rid in seen:
                    continue
                seen.add(rid)
                items.append(
                    {
                        "id": rid,
                        "title": str(r["title"] or ""),
                        "link": str(r["url"] or ""),
                        "published_at": str(r["connected_at"] or r["created_at"] or "")[
                            :19
                        ],
                        "source": str(r["source_name"] or ""),
                        "sentiment_score": None,
                        "sentiment_label": None,
                        "event_tags": [],
                        "industries": [],
                        "key_terms": [],
                        "countries": [],
                        "matched_by": "title",
                    }
                )
    finally:
        conn.close()
    items.sort(key=lambda it: int(it["id"]), reverse=True)
    return items[:limit]
