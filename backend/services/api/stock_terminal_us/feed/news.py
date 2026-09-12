"""美股个股终端 —— 个股资讯（PG enrichment 主路径 + Huntly 兜底）。

**主路径**：`news_article_enrichment`（约 58.8 万行，FinBERT/LLM 结构化标签）按
`tickers` 数组精确匹配 symbol（GIN 索引，实测 AAPL 3532 条 / NVDA 4656 条），
并用**标题中文名 ILIKE** 兜召回（中文资讯常在标题里写「苹果」而不打 ticker）。
响应带 `sentiment_score` / `sentiment_label` / `event_tags` 等标签。
查询按 `huntly_page_id` 倒序取最近 `_PG_SCAN_MAX` 条候选（走主键索引后向扫描，
实测 0.03-0.3s），再按发布时间倒序切 limit —— 与 `routers/news.py` 的
`/articles` 同库同口径，不新造一套标签语义。

**匹配词规则**（沿用 A 股终端）：中文名 + 代码，**长度 ≤1 的代码不参与匹配**
（F/T/A 这类单字符 ticker 在标题里几乎满命中）；单字符标的只剩中文名走标题匹配。

**兜底**：Huntly SQLite 标题 LIKE（本模块原实现）—— PG 不可用 / enrichment
无命中 / 关键词为空时启用，再按 id 反查 PG 补情绪标签（best-effort）。

Huntly 连接必须用 `file:{path}?immutable=1`：Java 进程持写锁会让 `mode=ro`
阻塞（连 PRAGMA 都拿不到锁）。已知风险：`immutable=1` 与并发写可能抛
`database disk image is malformed`，失败重试一次后降级为 available=false + 空 items。
"""

from __future__ import annotations

import logging
import os
import sqlite3
from typing import Any
from collections.abc import Iterable

from backend.services.api.stock_terminal_us.feed.base import (
    _name_of,
    _symbol_exists,
    normalize_symbol,
)

logger = logging.getLogger(__name__)

HUNTLY_SQLITE_PATH = "/data/huntly/db.sqlite"
_MAX_KEYWORD_LEN = 1  # 长度 <= 1 的关键词命中一切，不参与匹配

# 代码关键词的 Huntly 扫描窗口（行数）；page 行内含正文大字段，全表扫一次冷启动 47s
_TICKER_SCAN_WINDOW = 200_000

# enrichment 候选窗口：按 huntly_page_id 倒序取多少条后在内存里按发布时间排序
_PG_SCAN_DEFAULT = 100
_PG_SCAN_MAX = 500
_PG_CONNECT_TIMEOUT = 5

_ENRICH_COLS = (
    "huntly_page_id, tickers, sentiment_score, sentiment_label, "
    "event_tags, industries, key_terms, countries, title"
)


def _db_path() -> str:
    return os.getenv("HUNTLY_SQLITE_PATH", HUNTLY_SQLITE_PATH)


def keywords_for(symbol: str) -> list[str]:
    """该标的的资讯匹配词（中文名 + 代码，去重且剔除单字符代码）。"""
    sym = normalize_symbol(symbol)
    if not sym:
        return []
    name = _name_of(sym)
    out: list[str] = []
    for kw in (name, sym):
        kw = str(kw or "").strip()
        if len(kw) > _MAX_KEYWORD_LEN and kw not in out:
            out.append(kw)
    return out


# ---- PG enrichment（主路径） ----


def _pg_conn():
    """同步 psycopg2 连接（与 `routers/news.py`、`news/matcher.py` 同款配置口径）。"""
    import psycopg2

    return psycopg2.connect(
        host=os.getenv("POSTGRES_HOST") or os.getenv("DB_HOST", "quantmind-db"),
        port=int(os.getenv("POSTGRES_PORT") or os.getenv("DB_PORT", "5432")),
        user=os.getenv("POSTGRES_USER") or os.getenv("DB_USER", "quantmind"),
        password=os.getenv("POSTGRES_PASSWORD") or os.getenv("DB_PASSWORD", ""),
        dbname=os.getenv("POSTGRES_DB") or os.getenv("DB_NAME", "quantmind"),
        connect_timeout=_PG_CONNECT_TIMEOUT,
    )


def _row_dict(row: tuple) -> dict[str, Any]:
    pid, tickers, score, label, events, inds, terms, countries, title = row
    return {
        "id": int(pid),
        "tickers": list(tickers or []),
        "sentiment_score": float(score) if score is not None else None,
        "sentiment_label": str(label) if label else None,
        "event_tags": list(events or []),
        "industries": list(inds or []),
        "key_terms": list(terms or []),
        "countries": list(countries or []),
        "title": str(title or ""),
    }


def _fetch_candidates(sym: str, keywords: list[str], scan: int) -> list[dict[str, Any]]:
    """PG 倒排候选：ticker 精确匹配（长度>1）OR 标题中文名匹配，按 page_id 倒序取窗口。

    `keywords` 已过 `_MAX_KEYWORD_LEN` 白名单；symbol 过 `_SYMBOL_RE` ——
    这里只做参数化查询，无字符串拼接注入面。
    """
    clauses: list[str] = []
    params: list[Any] = []
    for kw in keywords:
        if kw == sym:
            clauses.append("tickers @> ARRAY[%s]")
            params.append(kw)
        else:
            clauses.append("title ILIKE %s")
            params.append(f"%{kw}%")
    if not clauses:
        return []
    sql = (
        f"SELECT {_ENRICH_COLS} FROM news_article_enrichment "
        f"WHERE ({' OR '.join(clauses)}) "
        f"ORDER BY huntly_page_id DESC LIMIT %s"
    )
    params.append(int(scan))
    with _pg_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        return [_row_dict(r) for r in cur.fetchall()]


def _fetch_by_ids(ids: Iterable[int]) -> dict[int, dict[str, Any]]:
    """按 huntly_page_id 批量取 enrichment（兜底结果补情绪标签用，best-effort）。"""
    id_list = [int(i) for i in ids]
    if not id_list:
        return {}
    try:
        with _pg_conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {_ENRICH_COLS} FROM news_article_enrichment "
                f"WHERE huntly_page_id = ANY(%s)",
                (id_list,),
            )
            return {int(r[0]): _row_dict(r) for r in cur.fetchall()}
    except Exception as exc:  # noqa: BLE001 - 标签是增益，不是必得
        logger.warning("[stock-terminal-us] enrichment 批量补标签失败: %s", exc)
        return {}


# ---- Huntly SQLite（兜底 + 主路径的标题/链接/时间） ----


def _huntly_connect(db: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db}?immutable=1", uri=True, timeout=3)
    conn.row_factory = sqlite3.Row
    return conn


def _huntly_meta(ids: Iterable[int]) -> dict[int, dict[str, Any]]:
    """按 id 批量取 Huntly 页面（标题/链接/发布时间/来源名）；失败返回空映射。"""
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
        conn = _huntly_connect(_db_path())
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


def _fetch_huntly(
    db: str, keywords: list[str], sym: str, limit: int
) -> list[dict[str, Any]]:
    """兜底路径：Huntly 标题 LIKE，逐词匹配后去重、按 id 倒序取前 limit 条。"""
    items: list[dict[str, Any]] = []
    seen: set[int] = set()
    conn = sqlite3.connect(f"file:{db}?immutable=1", uri=True, timeout=3)
    conn.row_factory = sqlite3.Row
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


# ---- 组装 ----


def _to_item(row: dict[str, Any], meta: dict[int, dict[str, Any]], sym: str) -> dict:
    """PG enrichment 行 + Huntly 页面元数据 -> 前端资讯条目。"""
    page = meta.get(row["id"]) or {}
    return {
        "id": row["id"],
        "title": page.get("title") or row["title"],
        "link": page.get("link") or None,
        "published_at": page.get("published_at") or None,
        "source": page.get("source") or "",
        "sentiment_score": row["sentiment_score"],
        "sentiment_label": row["sentiment_label"],
        "event_tags": row["event_tags"],
        "industries": row["industries"],
        "key_terms": row["key_terms"],
        "countries": row["countries"],
        "matched_by": "ticker" if sym in row["tickers"] else "title",
    }


def _sort_key(item: dict[str, Any]) -> tuple[str, int]:
    """发布时间倒序；无发布时间的排到末尾（按 id 兜底）。"""
    return (item.get("published_at") or "", int(item["id"]))


def get_stock_news(symbol: str, limit: int = 20) -> dict[str, Any] | None:
    """个股资讯；标的不存在返回 None（路由转 404），资讯源不可用返回空 items。"""
    sym = normalize_symbol(symbol)
    if not sym or not _symbol_exists(sym):
        return None
    limit = max(1, min(int(limit), 100))
    keywords = keywords_for(sym)
    result: dict[str, Any] = {
        "symbol": sym,
        "name": _name_of(sym),
        "keywords": keywords,
        "provider": None,
        "available": False,
        "total": 0,
        "items": [],
        "note": (
            "主路径 news_article_enrichment（ticker 精确 + 标题中文名，含 FinBERT 情绪），"
            "Huntly 标题匹配仅兜底；长度 ≤1 的代码不参与匹配"
        ),
    }
    if not keywords:
        return result

    scan = min(max(limit * 5, _PG_SCAN_DEFAULT), _PG_SCAN_MAX)
    try:
        rows = _fetch_candidates(sym, keywords, scan)
    except Exception as exc:  # noqa: BLE001 - PG 不可用则走 Huntly 兜底
        logger.warning(
            "[stock-terminal-us] enrichment 查询失败，转 Huntly 兜底: %s", exc
        )
        rows = []
    if rows:
        meta = _huntly_meta([r["id"] for r in rows])
        items = sorted(
            (_to_item(r, meta, sym) for r in rows), key=_sort_key, reverse=True
        )[:limit]
        result.update(
            provider="enrichment", available=True, total=len(items), items=items
        )
        return result

    # 兜底：Huntly 标题 LIKE（PG 空/不可用时），再按 id 补情绪标签
    db = _db_path()
    if not os.path.exists(db):
        return result
    for attempt in (1, 2):
        try:
            items = _fetch_huntly(db, keywords, sym, limit)
            if items:
                labels = _fetch_by_ids(it["id"] for it in items)
                for it in items:
                    row = labels.get(it["id"])
                    if row:
                        it["sentiment_score"] = row["sentiment_score"]
                        it["sentiment_label"] = row["sentiment_label"]
                        it["event_tags"] = row["event_tags"]
                        it["industries"] = row["industries"]
                        it["key_terms"] = row["key_terms"]
                        it["countries"] = row["countries"]
                        if sym in row["tickers"]:
                            it["matched_by"] = "ticker"
            result.update(
                provider="huntly" if items else None,
                available=bool(items),
                total=len(items),
                items=items,
            )
            return result
        except Exception as exc:  # noqa: BLE001 - 资讯不可用不阻塞终端其他面板
            logger.warning(
                "[stock-terminal-us] 读取 Huntly 资讯失败 (%s/2) %s: %s",
                attempt,
                sym,
                exc,
            )
    return result
