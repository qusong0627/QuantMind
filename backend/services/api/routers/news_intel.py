"""资讯中心 2.0 — 决策级新闻流、个股档案、板块雷达、订阅置顶。

设计见 ``docs/资讯中心2.0_设计与实施_20261118.md``。纯计算在
``backend.shared.news_intel_query`` / ``news_intel_rank``，本模块只做取数与鉴权。

口径纪律：
- 发布时间唯一事实源 = ``huntly_publish_index.published_at``（不是 ``enriched_at``）；
- 个股方向只认 L1/L2，L3/L4 只做热度；
- ``pre5`` 缺失 → INFO，绝不猜；
- 展示面只输出位置/风险语，不输出买入/卖出/减仓。
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import TEXT, bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY as PG_ARRAY

from backend.services.api.user_app.middleware.auth import get_current_user
from backend.shared.database_manager_v2 import get_session
from backend.shared.news_event_rules import classify_event, is_speculative
from backend.shared.news_tagging import article_is_noise
from backend.shared.news_intel_query import (
    AMBIGUOUS_TICKERS,
    COMMODITY_THEMES,
    SectorIndex,
    aggregate_commodities,
    aggregate_sectors,
    aggregate_topics,
    article_payload,
    bare,
    collapse_duplicates,
    is_supported_symbol,
    market_of,
    matches_commodity,
    publisher_conflict,
    rank_rows,
    read_rule,
    stock_name_from_row,
    suffix,
    user_key,
)
from backend.shared.news_intel_rank import rank_sector_article
from backend.shared.news_pre5 import load_pre5_map, pre5_lookup
from backend.shared.stock_utils import StockCodeUtil

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/news-intel",
    tags=["News Intel"],
    dependencies=[Depends(get_current_user)],
)

CN_TZ = timezone(timedelta(hours=8))
_ARTICLE_SCAN_MAX = 4000
_SECTOR_SCAN_MAX = 40000
_SECTOR_CACHE_TTL = 300.0

_SECTOR_CACHE: dict[str, tuple[float, Any]] = {}
_PRE5_CACHE: dict[str, tuple[float, dict[str, float]]] = {}
_SECTOR_INDEX_CACHE: tuple[float, SectorIndex] | None = None
_SUB_TABLE_READY = False

# 读多写少：同一市场/窗口的聚合结果在 30 秒内复用，避免切页签时重复扫 4 万行。
_NEWS_CACHE_TTL = 30.0
_OVERVIEW_CACHE: dict[tuple[str, int], tuple[float, dict[str, Any]]] = {}
_TOPICS_CACHE: dict[tuple[str, int, int], tuple[float, dict[str, Any]]] = {}
_STOCKS_CACHE: dict[tuple[str, int, int], tuple[float, dict[str, Any]]] = {}
_HUNTLY_META_CACHE: dict[int, tuple[float, dict[str, str]]] = {}
_HUNTLY_META_TTL = 600.0


def _cached(cache: dict, key, ttl: float = _NEWS_CACHE_TTL):
    hit = cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    return None


def _store(cache: dict, key, payload):
    cache[key] = (time.time(), payload)
    return payload


# ---------------------------------------------------------------- data access


_MARKET_TICKER_SQL = {
    "CN": "(t LIKE '%.SH' OR t LIKE '%.SZ' OR t LIKE '%.BJ')",
    "HK": "(t LIKE '%.HK')",
    "US": "(t ~ '^[A-Z][A-Z0-9.\\-]{0,9}$')",
}


async def _fetch_market_articles(
    session,
    market: str,
    *,
    since: datetime,
    scan: int,
    with_title: bool = True,
) -> list[dict[str, Any]]:
    """按市场拉全市场新闻（用于 KPI/主题/商品聚合）。"""
    cols = (
        "e.huntly_page_id AS page_id, e.title, e.tickers, e.key_terms, e.event_tags, "
        "e.sentiment_label, e.sentiment_score"
        if with_title
        else "e.huntly_page_id AS page_id, e.tickers, e.key_terms, e.event_tags, "
        "e.sentiment_label, e.sentiment_score"
    )
    pattern = _MARKET_TICKER_SQL.get(market)
    where_market = (
        f"AND EXISTS (SELECT 1 FROM unnest(e.tickers) AS t WHERE {pattern})"
        if pattern
        else ""
    )
    stmt = text(
        f"""
        SELECT {cols}, p.published_at, p.source_domain
        FROM news_article_enrichment e
        JOIN huntly_publish_index p ON p.huntly_page_id = e.huntly_page_id
        WHERE p.published_at >= :since
          {where_market}
        ORDER BY p.published_at DESC
        LIMIT :scan
        """
    )
    rows = (
        await session.execute(stmt, {"since": since, "scan": int(scan)})
    ).mappings().all()
    return [dict(r) for r in rows]


async def _fetch_futures_articles(
    session,
    words: tuple[str, ...],
    *,
    since: datetime,
    scan: int = 2000,
) -> list[dict[str, Any]]:
    """期货品种：SQL 侧先按品种词过滤，避免扫全市场 4 万行。"""
    likes = [f"%{w}%" for w in words]
    stmt = text(
        """
        SELECT e.huntly_page_id AS page_id, e.title, e.tickers, e.key_terms,
               e.event_tags, e.sentiment_label, e.sentiment_score,
               p.published_at, p.source_domain
        FROM news_article_enrichment e
        JOIN huntly_publish_index p ON p.huntly_page_id = e.huntly_page_id
        WHERE p.published_at >= :since
          AND (e.title ILIKE ANY(:likes) OR e.key_terms && :terms)
        ORDER BY p.published_at DESC
        LIMIT :scan
        """
    ).bindparams(
        bindparam("likes", type_=PG_ARRAY(TEXT)),
        bindparam("terms", type_=PG_ARRAY(TEXT)),
    )
    rows = (
        await session.execute(
            stmt,
            {"since": since, "scan": int(scan), "likes": likes, "terms": list(words)},
        )
    ).mappings().all()
    return [dict(r) for r in rows]


async def _attach_ticker_names(
    session, items: list[dict[str, Any]], key: str = "top_tickers"
) -> None:
    codes = {
        str(t.get("code"))
        for item in items
        for t in (item.get(key) or [])
        if t.get("code")
    }
    if not codes:
        return
    names = await _stock_names(session, list(codes))
    for item in items:
        for ticker in item.get(key) or []:
            ticker["name"] = names.get(suffix(str(ticker.get("code"))), "")


async def _stock_names(session, codes: list[str]) -> dict[str, str]:
    """代码 → 名称。A 股走 ``stocks``；港股/美股走 ``stock_aliases`` 的名称别名。"""
    if not codes:
        return {}
    suffixes = [suffix(c) for c in codes]
    out: dict[str, str] = {}
    try:
        rows = (
            await session.execute(
                text("SELECT symbol, name FROM stocks WHERE symbol = ANY(:syms)").bindparams(
                    bindparam("syms", type_=PG_ARRAY(TEXT))
                ),
                {"syms": suffixes},
            )
        ).mappings().all()
        for row in rows:
            data = dict(row)
            if data.get("symbol"):
                out[suffix(str(data["symbol"]))] = stock_name_from_row(data)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[news-intel] a-share names failed: %s", exc)
    missing = [c for c in suffixes if c not in out]
    if missing:
        try:
            rows = (
                await session.execute(
                    text(
                        "SELECT ticker, alias FROM stock_aliases "
                        "WHERE ticker = ANY(:syms) AND alias_type = 'name' "
                        "ORDER BY priority ASC"
                    ).bindparams(bindparam("syms", type_=PG_ARRAY(TEXT))),
                    {"syms": missing},
                )
            ).mappings().all()
            for row in rows:
                code = suffix(str(row["ticker"]))
                if code not in out and row.get("alias"):
                    out[code] = str(row["alias"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("[news-intel] alias names failed: %s", exc)
    return out


async def _resolve_code(session, raw: str) -> str:
    """代码或名称 → 市场代码；A 股名称只认 stocks.name 的精确匹配。"""
    symbol = suffix(raw)
    if is_supported_symbol(symbol):
        return symbol
    name = str(raw or "").strip()
    if not name:
        return ""
    try:
        row = (
            await session.execute(
                text("SELECT symbol FROM stocks WHERE name = :n LIMIT 1"), {"n": name}
            )
        ).first()
    except Exception:  # noqa: BLE001
        return ""
    return suffix(str(row[0])) if row and row[0] else ""


async def _fetch_articles(
    session,
    symbols: list[str] | None,
    *,
    since: datetime,
    scan: int,
    with_title: bool = True,
    title_like: str | None = None,
) -> list[dict[str, Any]]:
    """文章 × 股票链接。``symbols=None`` = 不限票（板块聚合用）。"""
    if symbols is not None and not symbols:
        return []
    cols = (
        "e.huntly_page_id AS page_id, e.title, e.tickers, e.event_tags, "
        "e.sentiment_label, e.sentiment_score, e.entity_sentiments"
        if with_title
        else "e.huntly_page_id AS page_id, e.tickers, e.event_tags, "
        "e.sentiment_label, e.sentiment_score, e.entity_sentiments"
    )
    where_symbol = "AND e.tickers && :syms" if symbols is not None else ""
    where_title = "AND e.title ILIKE :title_like" if title_like else ""
    stmt = text(
        f"""
        SELECT {cols}, p.published_at, p.source_domain
        FROM news_article_enrichment e
        JOIN huntly_publish_index p ON p.huntly_page_id = e.huntly_page_id
        WHERE p.published_at >= :since
          {where_symbol}
          {where_title}
        ORDER BY p.published_at DESC
        LIMIT :scan
        """
    )
    params: dict[str, Any] = {"since": since, "scan": int(scan)}
    if symbols is not None:
        stmt = stmt.bindparams(bindparam("syms", type_=PG_ARRAY(TEXT)))
        params["syms"] = [suffix(s) for s in symbols]
    if title_like:
        params["title_like"] = title_like
    rows = (await session.execute(stmt, params)).mappings().all()
    return [dict(r) for r in rows]


def _huntly_meta(page_ids: list[int]) -> dict[int, dict[str, str]]:
    if not page_ids:
        return {}
    now = time.time()
    wanted = {int(i) for i in page_ids}
    out: dict[int, dict[str, str]] = {}
    missing: list[int] = []
    for pid in wanted:
        hit = _HUNTLY_META_CACHE.get(pid)
        if hit and now - hit[0] < _HUNTLY_META_TTL:
            out[pid] = hit[1]
        else:
            missing.append(pid)
    if not missing:
        return out
    path = os.getenv("HUNTLY_SQLITE_PATH", "/data/huntly/db.sqlite")
    if not Path(path).exists():
        return out
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            conn = sqlite3.connect(f"file:{path}?immutable=1", uri=True, timeout=3)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("CREATE TEMP TABLE _wanted(id INTEGER PRIMARY KEY)")
                conn.executemany("INSERT INTO _wanted VALUES(?)", [(int(i),) for i in missing])
                rows = conn.execute(
                    "SELECT p.id, p.url, c.name AS connector_name "
                    "FROM page p LEFT JOIN connector c ON c.id = p.connector_id "
                    "JOIN _wanted w ON w.id = p.id"
                ).fetchall()
            finally:
                conn.close()
            fetched = {
                int(r["id"]): {
                    "url": str(r["url"] or ""),
                    "connector_name": str(r["connector_name"] or ""),
                }
                for r in rows
            }
            if len(_HUNTLY_META_CACHE) > 200000:
                _HUNTLY_META_CACHE.clear()
            for pid, item in fetched.items():
                _HUNTLY_META_CACHE[pid] = (now, item)
            out.update(fetched)
            return out
        except Exception as exc:  # noqa: BLE001 - Huntly 并发写时 immutable 可能读到撕裂页
            last_exc = exc
            time.sleep(0.05 * (attempt + 1))
    logger.warning("[news-intel] huntly meta failed: %s", last_exc)
    return out


def _attach_meta(rows: list[dict[str, Any]]) -> None:
    meta = _huntly_meta([int(r["page_id"]) for r in rows])
    for row in rows:
        item = meta.get(int(row["page_id"]), {})
        row["url"] = item.get("url", "")
        row["source"] = item.get("connector_name") or str(row.get("source_domain") or "")


def _pre5_map(day: str) -> dict[str, float]:
    cached = _PRE5_CACHE.get(day)
    if cached and time.time() - cached[0] < 3600:
        return cached[1]
    try:
        values = load_pre5_map(day)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[news-intel] pre5 load failed: %s", exc)
        values = {}
    _PRE5_CACHE[day] = (time.time(), values)
    return values


def _quantdb_dir() -> Path:
    env = str(os.getenv("QM_QUANTDB_DATA_DIR") or "").strip()
    if env:
        return Path(env)
    for candidate in (
        Path("/data/quantdb"),
        Path(__file__).resolve().parents[4] / "data" / "quantdb",
    ):
        if candidate.is_dir():
            return candidate
    return Path("/data/quantdb")


def _sector_index() -> SectorIndex | None:
    global _SECTOR_INDEX_CACHE
    path = _quantdb_dir() / "2_base_sector" / "sector_concept" / "sector_members.parquet"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    if _SECTOR_INDEX_CACHE and _SECTOR_INDEX_CACHE[0] == mtime:
        return _SECTOR_INDEX_CACHE[1]
    try:
        import pandas as pd

        frame = pd.read_parquet(path, columns=["SectorCode", "SectorName", "SectorType", "Symbol"])
        index = SectorIndex(frame)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[news-intel] sector members failed: %s", exc)
        return None
    _SECTOR_INDEX_CACHE = (mtime, index)
    return index


async def _ensure_subscription_table() -> None:
    global _SUB_TABLE_READY
    if _SUB_TABLE_READY:
        return
    try:
        async with get_session() as session:
            await session.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS qm_news_subscription (
                        id BIGSERIAL PRIMARY KEY,
                        tenant_id TEXT NOT NULL DEFAULT 'default',
                        user_id TEXT NOT NULL,
                        symbol TEXT NOT NULL,
                        name TEXT,
                        pinned BOOLEAN NOT NULL DEFAULT TRUE,
                        sort_order INTEGER NOT NULL DEFAULT 0,
                        source TEXT NOT NULL DEFAULT 'manual',
                        last_synced_at TIMESTAMPTZ,
                        last_read_at TIMESTAMPTZ,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                        UNIQUE (tenant_id, user_id, symbol)
                    )
                    """
                )
            )
            await session.execute(
                text(
                    "ALTER TABLE qm_news_subscription "
                    "ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'manual'"
                )
            )
            await session.execute(
                text(
                    "ALTER TABLE qm_news_subscription "
                    "ADD COLUMN IF NOT EXISTS last_synced_at TIMESTAMPTZ"
                )
            )
            await session.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS idx_news_sub_user "
                    "ON qm_news_subscription "
                    "(tenant_id, user_id, pinned DESC, sort_order)"
                )
            )
            await session.commit()
        _SUB_TABLE_READY = True
    except Exception as exc:  # noqa: BLE001
        logger.warning("[news-intel] subscription table ensure failed: %s", exc)


def _as_utc_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------- endpoints


@router.get("/stocks")
async def stock_list(
    market: str = Query("CN", pattern="^(CN|HK|US|FUTURES)$"),
    days: int = Query(7, ge=1, le=90),
    limit: int = Query(300, ge=1, le=6000),
    current_user: dict = Depends(get_current_user),
):
    """市场内按新闻活跃度排的股票列表（个股档案左侧）。

    路由先注册会遮住后面的同名 ``/stocks``；这里直接委托给唯一实现，
    避免两套排序/补全逻辑漂移。
    """
    return await stocks(market=market, days=days, limit=limit, current_user=current_user)


@router.get("/stocks/{code}/news")
async def stock_news(
    code: str,
    days: int = Query(90, ge=1, le=730),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0, le=2000),
    sort: str = Query("weight", pattern="^(weight|time)$"),
    bucket: str = Query("all", pattern="^(all|important|risk|spec)$"),
    direction: str = Query("", pattern="^(|利好|利空|中性)$"),
    decision: str = Query("", pattern="^(|P0|P1|P2|WATCH|INFO)$"),
    risk_type: str = Query("", max_length=32),
    subject_only: bool = Query(False),
    spec_only: bool = Query(False),
    current_user: dict = Depends(get_current_user),
):
    _ = current_user
    since = datetime.now(timezone.utc) - timedelta(days=days)
    day = datetime.now(CN_TZ).strftime("%Y%m%d")
    try:
        async with get_session(read_only=True) as session:
            symbol = await _resolve_code(session, code)
            if not is_supported_symbol(symbol):
                raise HTTPException(status_code=400, detail=f"非法代码或名称 {code}")
            rows = await _fetch_articles(session, [symbol], since=since, scan=_ARTICLE_SCAN_MAX)
            names = await _stock_names(session, [symbol])
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"资讯数据暂不可用: {exc}") from exc

    pre5 = pre5_lookup(symbol, _pre5_map(day))
    name = names.get(symbol) or names.get(bare(symbol)) or ""
    _attach_meta(rows)
    pairs = rank_rows(rows, code=symbol, name=name, pre5=pre5)

    if direction:
        pairs = [p for p in pairs if p[1].direction == direction]
    if decision:
        pairs = [p for p in pairs if p[1].decision == decision]
    if risk_type:
        pairs = [p for p in pairs if p[1].event_type == risk_type]
    if subject_only:
        pairs = [p for p in pairs if p[1].is_subject]
    if spec_only:
        pairs = [p for p in pairs if p[1].speculative]

    if bucket == "important":
        pairs = [p for p in pairs if p[1].weight >= 68]
    elif bucket == "risk":
        pairs = [p for p in pairs if p[1].risk_level != "none"]
    elif bucket == "spec":
        pairs = [p for p in pairs if p[1].speculative]

    if sort == "time":
        pairs.sort(
            key=lambda p: p[0].get("published_at")
            or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
    else:
        pairs.sort(
            key=lambda p: (
                p[1].weight,
                p[0].get("published_at") or datetime.min.replace(tzinfo=timezone.utc),
            ),
            reverse=True,
        )

    total = len(pairs)
    window = pairs[offset : offset + limit]
    return {
        "success": True,
        "data": {
            "code": symbol,
            "name": name,
            "market": market_of(symbol),
            "pre5": pre5,
            "pre5_bucket": pairs[0][1].pre5_bucket if pairs else "NA",
            "as_of": _as_utc_iso(datetime.now(timezone.utc)),
            "total": total,
            "offset": offset,
            "limit": limit,
            "has_more": offset + limit < total,
            "items": [article_payload(row, rank) for row, rank in window],
        },
    }


@router.get("/stocks/{code}/profile")
async def stock_profile(
    code: str,
    days: int = Query(90, ge=7, le=730),
    current_user: dict = Depends(get_current_user),
):
    _ = current_user
    since = datetime.now(timezone.utc) - timedelta(days=days)
    day = datetime.now(CN_TZ).strftime("%Y%m%d")
    try:
        async with get_session(read_only=True) as session:
            symbol = await _resolve_code(session, code)
            if not is_supported_symbol(symbol):
                raise HTTPException(status_code=400, detail=f"非法代码或名称 {code}")
            rows = await _fetch_articles(session, [symbol], since=since, scan=_ARTICLE_SCAN_MAX)
            names = await _stock_names(session, [symbol])
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"资讯数据暂不可用: {exc}") from exc

    pre5 = pre5_lookup(symbol, _pre5_map(day))
    name = names.get(symbol) or names.get(bare(symbol)) or ""
    _attach_meta(rows)
    pairs = rank_rows(rows, code=symbol, name=name, pre5=pre5)

    decisions = {"P0": 0, "P1": 0, "P2": 0, "WATCH": 0, "INFO": 0}
    directions = {"利好": 0, "利空": 0, "中性": 0}
    events = {
        "hard_negative": 0,
        "soft_negative": 0,
        "hard_positive": 0,
        "governance_positive": 0,
        "generic_positive": 0,
        "generic_negative": 0,
        "neutral": 0,
    }
    event_key = {
        "硬利空": "hard_negative",
        "软利空": "soft_negative",
        "硬利好": "hard_positive",
        "治理利好": "governance_positive",
        "泛利好": "generic_positive",
        "泛利空": "generic_negative",
    }
    risks: dict[str, int] = {}
    subjects = 0
    spec = 0
    sources: set[str] = set()
    heat: dict[str, int] = {}
    for row, rank in pairs:
        decisions[rank.decision] = decisions.get(rank.decision, 0) + 1
        directions[rank.direction] = directions.get(rank.direction, 0) + 1
        events[event_key.get(rank.category, "neutral")] += 1
        if rank.risk_level != "none":
            risks[rank.event_type] = risks.get(rank.event_type, 0) + 1
        subjects += 1 if rank.is_subject else 0
        spec += 1 if rank.speculative else 0
        sources.add(str(row.get("source") or row.get("source_domain") or ""))
        pub = row.get("published_at")
        if isinstance(pub, datetime):
            key = pub.astimezone(CN_TZ).strftime("%Y%m%d")
            heat[key] = heat.get(key, 0) + 1

    top = sorted(
        pairs,
        key=lambda p: (
            p[1].weight,
            p[0].get("published_at") or datetime.min.replace(tzinfo=timezone.utc),
        ),
        reverse=True,
    )[:5]

    return {
        "success": True,
        "data": {
            "code": symbol,
            "name": name,
            "market": market_of(symbol),
            "ambiguous": suffix(symbol) in AMBIGUOUS_TICKERS,
            "days": days,
            "n_articles": len(pairs),
            "n_subject": subjects,
            "n_sources": len(sources),
            "n_speculative": spec,
            "pre5": pre5,
            "decisions": decisions,
            "directions": directions,
            "events": events,
            "risks": risks,
            "heat": [{"date": k, "n": v} for k, v in sorted(heat.items())],
            "top_events": [article_payload(row, rank) for row, rank in top],
            "read_rule": read_rule([r for _, r in pairs], pre5),
            "as_of": _as_utc_iso(datetime.now(timezone.utc)),
        },
    }


@router.get("/stocks/summary")
async def stocks_summary(
    codes: str = Query(..., description="逗号分隔，如 600519.SH,300750.SZ"),
    days: int = Query(7, ge=1, le=90),
    current_user: dict = Depends(get_current_user),
):
    _ = current_user
    cache_key = (codes, days)
    cached = _cached(_STOCKS_CACHE, cache_key)
    if cached is not None:
        return cached
    wanted = [c.strip() for c in codes.split(",") if c.strip()][:200]
    if not wanted:
        return {"success": True, "data": {"items": {}}}
    since = datetime.now(timezone.utc) - timedelta(days=days)
    pre5_map = _pre5_map(datetime.now(CN_TZ).strftime("%Y%m%d"))
    try:
        async with get_session(read_only=True) as session:
            rows = await _fetch_articles(
                session, [suffix(c) for c in wanted], since=since, scan=8000
            )
            names = await _stock_names(session, wanted)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"资讯数据暂不可用: {exc}") from exc

    _attach_meta(rows)
    by_code: dict[str, list[dict[str, Any]]] = {suffix(c): [] for c in wanted}
    for row in rows:
        for ticker in row.get("tickers") or []:
            key = suffix(str(ticker))
            if key in by_code:
                by_code[key].append(row)

    items: dict[str, Any] = {}
    for code, code_rows in by_code.items():
        name = names.get(code) or names.get(bare(code)) or ""
        pairs = rank_rows(code_rows, code=code, name=name, pre5=pre5_lookup(code, pre5_map))
        ranked = [r for _, r in pairs]
        top = max(ranked, key=lambda r: r.weight, default=None)
        items[code] = {
            "n_articles": len(pairs),
            "n_risk": sum(1 for r in ranked if r.risk_level != "none"),
            "n_negative": sum(1 for r in ranked if r.direction == "利空"),
            "n_positive": sum(1 for r in ranked if r.direction == "利好"),
            "n_neutral": sum(1 for r in ranked if r.direction == "中性"),
            "n_hard_negative": sum(1 for r in ranked if r.category == "硬利空"),
            "n_hard_positive": sum(1 for r in ranked if r.category == "硬利好"),
            "n_soft_negative": sum(1 for r in ranked if r.category == "软利空"),
            "n_speculative": sum(1 for r in ranked if r.speculative),
            "max_weight": top.weight if top else 0,
            "top_label": top.primary_label if top else "",
            "decision": top.decision if top else "",
        }
    return _store(
        _STOCKS_CACHE,
        cache_key,
        {"success": True, "data": {"days": days, "items": items}},
    )


@router.get("/stocks")
async def stocks(
    market: str = Query("CN", pattern="^(CN|HK|US|FUTURES)$"),
    days: int = Query(7, ge=1, le=90),
    limit: int = Query(300, ge=1, le=6000),
    current_user: dict = Depends(get_current_user),
):
    """全市场股票列表：按最近一条匹配新闻的时间倒序。

    个股档案的默认入口。只读 ``news_article_enrichment.tickers``，不额外回表
    查每只票的档案；名称沿用 ``_stock_names``（A 股 stocks / 港美 stock_aliases）。
    """
    _ = current_user
    cache_key = (market, days, limit)
    cached = _cached(_STOCKS_CACHE, cache_key)
    if cached is not None:
        return cached
    if market == "FUTURES":
        return {"success": True, "data": {"market": market, "items": []}}
    pattern = _MARKET_TICKER_SQL.get(market)
    if not pattern:
        return {"success": True, "data": {"market": market, "items": []}}
    since = datetime.now(timezone.utc) - timedelta(days=days)
    stmt = text(
        f"""
        SELECT t AS ticker, p.source_domain AS source_domain,
               count(*) AS n_articles, max(p.published_at) AS latest_at
        FROM news_article_enrichment e
        JOIN huntly_publish_index p ON p.huntly_page_id = e.huntly_page_id
        CROSS JOIN LATERAL unnest(e.tickers) AS t
        WHERE p.published_at >= :since
          AND {pattern}
        GROUP BY t, p.source_domain
        ORDER BY latest_at DESC, n_articles DESC
        LIMIT :scan
        """
    )
    try:
        async with get_session(read_only=True) as session:
            rows = (
                await session.execute(
                    stmt, {"since": since, "scan": max(limit * 6, 600)}
                )
            ).mappings().all()
            stats: dict[str, dict[str, Any]] = {}
            for row in rows:
                ticker = str(row.get("ticker") or "")
                if not ticker or market_of(ticker) != market:
                    continue
                domain = str(row.get("source_domain") or "")
                # 出版方自己的域名（10jqka/eastmoney/…）不是「被报道」；
                # 采购/招标源整篇丢弃。
                if suffix(ticker) in AMBIGUOUS_TICKERS:
                    continue
                if publisher_conflict(ticker, domain) or article_is_noise("", domain):
                    continue
                item = stats.setdefault(ticker, {"n_articles": 0, "latest_at": None})
                item["n_articles"] += int(row.get("n_articles") or 0)
                latest = row.get("latest_at")
                if latest is not None and (
                    item["latest_at"] is None or latest > item["latest_at"]
                ):
                    item["latest_at"] = latest
            names = await _stock_names(session, list(stats))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"资讯数据暂不可用: {exc}") from exc

    items: list[dict[str, Any]] = [
        {
            "code": ticker,
            "name": names.get(ticker) or names.get(bare(ticker)) or "",
            "market": market,
            "latest_at": _as_utc_iso(item["latest_at"]),
            "n_articles": item["n_articles"],
        }
        for ticker, item in stats.items()
    ]
    items.sort(key=lambda x: (x["latest_at"] or "", x["n_articles"]), reverse=True)
    items = items[:limit]
    # A 股补齐全市场：没有新闻的股票也留在列表底部，默认排序仍是新闻优先。
    if market == "CN" and len(items) < limit:
        seen = {item["code"] for item in items}
        try:
            async with get_session(read_only=True) as session:
                extra_rows = (
                    await session.execute(
                        text(
                            "SELECT symbol, name FROM stocks "
                            "WHERE symbol ~ '^[0-9]{6}[.](SH|SZ|BJ)$' "
                            "ORDER BY symbol"
                        )
                    )
                ).mappings().all()
        except Exception as exc:  # noqa: BLE001 - 补全失败不影响新闻列表
            logger.warning("[news-intel] stock universe fallback failed: %s", exc)
            extra_rows = []
        for row in extra_rows:
            code = suffix(str(row.get("symbol") or ""))
            if not code or code in seen:
                continue
            items.append(
                {
                    "code": code,
                    "name": str(row.get("name") or ""),
                    "market": market,
                    "latest_at": None,
                    "n_articles": 0,
                }
            )
            seen.add(code)
            if len(items) >= limit:
                break
    return _store(
        _STOCKS_CACHE,
        cache_key,
        {
            "success": True,
            "data": {
                "market": market,
                "days": days,
                "total": len(items),
                "items": items,
                "as_of": _as_utc_iso(datetime.now(timezone.utc)),
            },
        },
    )


@router.get("/stocks/search")
async def search_stocks(
    q: str = Query(..., min_length=1, max_length=40, description="代码 / 名称 / 新闻关键词"),
    market: str = Query("CN", pattern="^(CN|HK|US)$"),
    days: int = Query(30, ge=1, le=180),
    limit: int = Query(30, ge=1, le=100),
    current_user: dict = Depends(get_current_user),
):
    """关键词 → 股票候选。

    三路合并：① 代码直配；② 股票名/别名模糊匹配；③ 新闻标题命中该关键词的
    标的按命中篇数聚合。只回当前市场，个股档案搜索框用。
    """
    _ = current_user
    raw = str(q or "").strip()
    if not raw:
        return {"success": True, "data": {"items": [], "q": raw, "market": market}}
    pattern = _MARKET_TICKER_SQL.get(market)
    if not pattern:
        return {"success": True, "data": {"items": [], "q": raw, "market": market}}

    cache_key = ("search", market, raw.lower(), days, limit)
    cached = _cached(_STOCKS_CACHE, cache_key)
    if cached is not None:
        return cached

    since = datetime.now(timezone.utc) - timedelta(days=days)
    # 代码直配：输入本身就是一个合法代码且属于当前市场。
    direct = suffix(raw) if is_supported_symbol(suffix(raw)) else ""
    direct = direct if direct and market_of(direct) == market else ""

    stats: dict[str, dict[str, Any]] = {}
    if direct:
        stats[direct] = {"n_articles": 0, "latest_at": None}
    try:
        async with get_session(read_only=True) as session:
            # ① 名称/别名模糊匹配
            name_hits: dict[str, str] = {}
            if market == "CN":
                rows = (
                    await session.execute(
                        text(
                            "SELECT symbol, name FROM stocks "
                            "WHERE name ILIKE :like ORDER BY length(name) LIMIT 40"
                        ),
                        {"like": f"%{raw}%"},
                    )
                ).mappings().all()
                for row in rows:
                    code = suffix(str(row.get("symbol") or ""))
                    if code and market_of(code) == market:
                        name_hits.setdefault(code, str(row.get("name") or ""))
            rows = (
                await session.execute(
                    text(
                        "SELECT ticker, alias FROM stock_aliases "
                        "WHERE alias ILIKE :like AND alias_type = 'name' "
                        "ORDER BY priority ASC LIMIT 60"
                    ),
                    {"like": f"%{raw}%"},
                )
            ).mappings().all()
            for row in rows:
                code = suffix(str(row.get("ticker") or ""))
                if code and market_of(code) == market:
                    name_hits.setdefault(code, str(row.get("alias") or ""))
            for code in name_hits:
                stats.setdefault(code, {"n_articles": 0, "latest_at": None})

            # ② 新闻标题命中 → 标的聚合
            rows = (
                await session.execute(
                    text(
                        f"""
                        SELECT t AS ticker, count(*) AS n_articles,
                               max(p.published_at) AS latest_at
                        FROM news_article_enrichment e
                        JOIN huntly_publish_index p
                          ON p.huntly_page_id = e.huntly_page_id
                        CROSS JOIN LATERAL unnest(e.tickers) AS t
                        WHERE p.published_at >= :since
                          AND e.title ILIKE :like
                          AND {pattern}
                        GROUP BY t
                        ORDER BY n_articles DESC
                        LIMIT :scan
                        """
                    ),
                    {"since": since, "like": f"%{raw}%", "scan": max(limit * 8, 200)},
                )
            ).mappings().all()
            company_names: dict[str, str] = dict(name_hits)
            title_hits: list[tuple[str, int, Any]] = []
            for row in rows:
                ticker = str(row.get("ticker") or "")
                code = suffix(ticker)
                if not code or market_of(code) != market:
                    continue
                title_hits.append((code, int(row.get("n_articles") or 0), row.get("latest_at")))
            names = await _stock_names(session, list({c for c, _, _ in title_hits} | set(stats)))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"资讯数据暂不可用: {exc}") from exc

    for code, n, latest in title_hits:
        item = stats.setdefault(code, {"n_articles": 0, "latest_at": None})
        item["n_articles"] = int(item["n_articles"]) + n
        if latest is not None and (item["latest_at"] is None or latest > item["latest_at"]):
            item["latest_at"] = latest

    items: list[dict[str, Any]] = [
        {
            "code": code,
            "name": company_names.get(code) or names.get(code) or names.get(bare(code)) or "",
            "market": market,
            "n_articles": int(item["n_articles"]),
            "latest_at": _as_utc_iso(item["latest_at"]),
            "is_code": code == direct,
            "is_name": code in company_names,
        }
        for code, item in stats.items()
    ]
    # 代码直配置顶；其余命中篇数优先，其次最近时间（稳定排序，逐级收紧）。
    items.sort(key=lambda x: x["latest_at"] or "", reverse=True)
    items.sort(key=lambda x: x["n_articles"], reverse=True)
    items.sort(key=lambda x: not x["is_code"])
    items = items[:limit]
    return _store(
        _STOCKS_CACHE,
        cache_key,
        {
            "success": True,
            "data": {
                "q": raw,
                "market": market,
                "days": days,
                "total": len(items),
                "items": items,
                "as_of": _as_utc_iso(datetime.now(timezone.utc)),
            },
        },
    )


@router.get("/stock-score")
async def stock_score(
    code: str = Query(...),
    model_id: str = Query(...),
    current_user: dict = Depends(get_current_user),
):
    """某只票在指定模型最近一批推理里的分数/截面位置。"""
    _ = current_user
    symbol = suffix(code)
    variants = {symbol, bare(symbol), StockCodeUtil.to_prefix(symbol)}
    try:
        async with get_session(read_only=True) as session:
            row = (
                await session.execute(
                    text(
                        """
                        WITH latest AS (
                            SELECT run_id, prediction_trade_date, data_trade_date
                            FROM qm_model_inference_runs
                            WHERE model_id = :m AND status = 'completed'
                            ORDER BY prediction_trade_date DESC NULLS LAST, created_at DESC
                            LIMIT 1
                        )
                        SELECT s.symbol, s.fusion_score, s.rank_pct, s.score_rank,
                               l.prediction_trade_date, l.data_trade_date,
                               (SELECT count(*) FROM engine_signal_scores x
                                 WHERE x.run_id = l.run_id) AS total
                        FROM latest l
                        LEFT JOIN engine_signal_scores s
                               ON s.run_id = l.run_id AND s.symbol = ANY(:syms)
                        LIMIT 1
                        """
                    ).bindparams(bindparam("syms", type_=PG_ARRAY(TEXT))),
                    {"m": model_id, "syms": sorted(variants)},
                )
            ).mappings().first()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"推理分数读取失败: {exc}") from exc

    if not row:
        return {
            "success": True,
            "data": {"found": False, "model_id": model_id, "reason": "该模型暂无已完成批次"},
        }
    found = bool(row.get("symbol"))
    rank_pct = float(row["rank_pct"]) if row.get("rank_pct") is not None else None
    position = (
        "top" if rank_pct is not None and rank_pct >= 0.8
        else "bottom" if rank_pct is not None and rank_pct <= 0.2
        else "mid"
    ) if rank_pct is not None else None
    return {
        "success": True,
        "data": {
            "found": found,
            "model_id": model_id,
            "code": symbol,
            "trade_date": str(row.get("prediction_trade_date") or ""),
            "data_trade_date": str(row.get("data_trade_date") or ""),
            "score": float(row["fusion_score"]) if row.get("fusion_score") is not None else None,
            "rank_pct": rank_pct,
            "rank": int(row["score_rank"]) if row.get("score_rank") is not None else None,
            "total": int(row.get("total") or 0),
            "position": position,
            "reason": None if found else "该模型最近批次未覆盖该标的",
        },
    }


@router.get("/sectors")
async def sectors(
    type: str = Query("concept", pattern="^(concept|industry1|industry2)$"),
    days: int = Query(5, ge=1, le=30),
    limit: int = Query(60, ge=1, le=200),
    include_style: bool = Query(False, description="含 MSCI 成份/近期新低 等风格板块"),
    current_user: dict = Depends(get_current_user),
):
    _ = current_user
    index = _sector_index()
    if index is None:
        return {
            "success": True,
            "data": {"available": False, "reason": "板块成员表不可用", "items": []},
        }
    cache_key = (
        f"{type}:{days}:{limit}:{int(include_style)}:"
        f"{datetime.now(CN_TZ).strftime('%Y%m%d%H')}"
    )
    cached = _SECTOR_CACHE.get(cache_key)
    if cached and time.time() - cached[0] < _SECTOR_CACHE_TTL:
        return {"success": True, "data": cached[1]}

    since = datetime.now(timezone.utc) - timedelta(days=days)
    scan = min(_SECTOR_SCAN_MAX, max(6000, days * 2500))
    try:
        async with get_session(read_only=True) as session:
            rows = await _fetch_articles(
                session, None, since=since, scan=scan, with_title=True
            )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"资讯数据暂不可用: {exc}") from exc

    items = [
        x
        for x in aggregate_sectors(rows, index)
        if x["type"] == type and (include_style or not x.get("style"))
    ][:limit]
    payload = {
        "available": True,
        "type": type,
        "days": days,
        "include_style": include_style,
        "as_of": _as_utc_iso(datetime.now(timezone.utc)),
        "items": items,
    }
    _SECTOR_CACHE[cache_key] = (time.time(), payload)
    return {"success": True, "data": payload}


@router.get("/sectors/{sector_code}")
async def sector_detail(
    sector_code: str,
    days: int = Query(5, ge=1, le=30),
    limit: int = Query(40, ge=1, le=200),
    current_user: dict = Depends(get_current_user),
):
    _ = current_user
    index = _sector_index()
    if index is None:
        return {
            "success": True,
            "data": {"available": False, "reason": "板块成员表不可用", "items": []},
        }
    meta = index.by_code.get(sector_code)
    if meta is None:
        raise HTTPException(status_code=404, detail=f"未知板块 {sector_code}")
    symbols = list(meta["symbols"])
    keyword = str(meta.get("keyword") or meta["name"])
    since = datetime.now(timezone.utc) - timedelta(days=days)
    try:
        async with get_session(read_only=True) as session:
            rows = await _fetch_articles(
                session,
                None,
                since=since,
                scan=1200,
                title_like=f"%{keyword}%",
            )
            names = await _stock_names(session, symbols)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"资讯数据暂不可用: {exc}") from exc

    rows = rows[:300]
    _attach_meta(rows)
    rows = collapse_duplicates(rows)
    ranked = [
        (
            row,
            rank_sector_article(
                title=str(row.get("title") or ""),
                event_tags=row.get("event_tags") or (),
                sentiment_label=row.get("sentiment_label"),
                source=str(row.get("source") or ""),
                source_domain=str(row.get("source_domain") or ""),
                published_at=row.get("published_at"),
            ),
        )
        for row in rows
    ]
    ranked.sort(
        key=lambda p: (
            p[1].weight,
            p[0].get("published_at") or datetime.min.replace(tzinfo=timezone.utc),
        ),
        reverse=True,
    )

    counts: dict[str, int] = {}
    symbol_set = set(symbols)
    for row in rows:
        for ticker in row.get("tickers") or []:
            key = suffix(str(ticker))
            if key in symbol_set:
                counts[key] = counts.get(key, 0) + 1
    top_stocks = [
        {"code": code, "name": names.get(code, ""), "n": n}
        for code, n in sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:8]
    ]
    n_positive = sum(1 for _, rank in ranked if rank.direction == "利好")
    n_negative = sum(1 for _, rank in ranked if rank.direction == "利空")
    n_neutral = len(ranked) - n_positive - n_negative

    return {
        "success": True,
        "data": {
            "available": True,
            "sector": {"code": meta["code"], "name": meta["name"], "type": meta["type"]},
            "days": days,
            "n_articles": len(rows),
            "n_positive": n_positive,
            "n_negative": n_negative,
            "n_neutral": n_neutral,
            "top_stocks": top_stocks,            "read_rule": (
                "板块利好发布前 30 分钟已涨 18~21bp，发布后无延续 → 不追；"
                "概念板块利空 post5 -2.3bp / post30 -7.3bp → 坏消息发布后才发酵。"
            ),
            "items": [article_payload(row, rank) for row, rank in ranked[:limit]],
            "as_of": _as_utc_iso(datetime.now(timezone.utc)),
        },
    }


@router.get("/overview")
async def overview(
    market: str = Query("CN", pattern="^(CN|HK|US|FUTURES)$"),
    days: int = Query(1, ge=1, le=30),
    current_user: dict = Depends(get_current_user),
):
    """KPI 带：窗口内新闻量、方向、硬风险、投机、热门主题。"""
    _ = current_user
    cache_key = (market, days)
    cached = _cached(_OVERVIEW_CACHE, cache_key)
    if cached is not None:
        return cached
    since = datetime.now(timezone.utc) - timedelta(days=days)
    scan = min(40000, max(4000, days * 3000))
    try:
        async with get_session(read_only=True) as session:
            rows = await _fetch_market_articles(
                session, market, since=since, scan=scan, with_title=(market == "FUTURES")
            )
            if market == "FUTURES":
                rows = [r for r in rows if matches_commodity(r)]
                topics = aggregate_commodities(rows, limit=6)
            else:
                topics = aggregate_topics(rows, limit=6)
                await _attach_ticker_names(session, topics)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"资讯数据暂不可用: {exc}") from exc

    n_positive = n_negative = n_risk = n_spec = 0
    for row in rows:
        category, direction, _ = classify_event(
            "", row.get("event_tags") or (), str(row.get("sentiment_label") or "")
        )
        if direction == "利好":
            n_positive += 1
        elif direction == "利空":
            n_negative += 1
        if category == "硬利空":
            n_risk += 1
        if is_speculative("", row.get("event_tags") or ()):
            n_spec += 1
    return _store(
        _OVERVIEW_CACHE,
        cache_key,
        {
            "success": True,
            "data": {
                "market": market,
                "days": days,
                "n_articles": len(rows),
                "n_positive": n_positive,
                "n_negative": n_negative,
                "n_risk": n_risk,
                "n_speculative": n_spec,
                "topics": topics,
                "as_of": _as_utc_iso(datetime.now(timezone.utc)),
            },
        },
    )


@router.get("/topics")
async def topics(
    market: str = Query("US", pattern="^(CN|HK|US|FUTURES)$"),
    days: int = Query(5, ge=1, le=30),
    limit: int = Query(40, ge=1, le=120),
    current_user: dict = Depends(get_current_user),
):
    """港股/美股按 key_terms 聚合主题；期货走商品品种词表。"""
    _ = current_user
    cache_key = (market, days, limit)
    cached = _cached(_TOPICS_CACHE, cache_key)
    if cached is not None:
        return cached
    since = datetime.now(timezone.utc) - timedelta(days=days)
    scan = min(40000, max(4000, days * 3000))
    try:
        async with get_session(read_only=True) as session:
            rows = await _fetch_market_articles(
                session, market, since=since, scan=scan, with_title=True
            )
            if market == "FUTURES":
                items = aggregate_commodities(rows, limit=limit)
            else:
                items = aggregate_topics(rows, limit=limit)
                await _attach_ticker_names(session, items)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"资讯数据暂不可用: {exc}") from exc
    return _store(
        _TOPICS_CACHE,
        cache_key,
        {
            "success": True,
            "data": {
                "market": market,
                "days": days,
                "items": items,
                "as_of": _as_utc_iso(datetime.now(timezone.utc)),
            },
        },
    )


@router.get("/topic-news")
async def topic_news(
    market: str = Query("US", pattern="^(CN|HK|US|FUTURES)$"),
    topic: str = Query(..., min_length=1),
    days: int = Query(5, ge=1, le=30),
    limit: int = Query(40, ge=1, le=200),
    current_user: dict = Depends(get_current_user),
):
    _ = current_user
    since = datetime.now(timezone.utc) - timedelta(days=days)
    scope = "商品" if market == "FUTURES" else "主题"
    words = COMMODITY_THEMES.get(topic, (topic,)) if market == "FUTURES" else ()
    try:
        async with get_session(read_only=True) as session:
            if market == "FUTURES":
                rows = await _fetch_futures_articles(session, words, since=since, scan=2000)
            else:
                rows = await _fetch_market_articles(
                    session, market, since=since, scan=40000, with_title=True
                )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"资讯数据暂不可用: {exc}") from exc

    if market == "FUTURES":
        rows = [
            r
            for r in rows
            if any(
                word
                in (
                    str(r.get("title") or "")
                    + " "
                    + " ".join(str(t) for t in (r.get("key_terms") or []))
                )
                for word in words
            )
            and matches_commodity(r)
        ]
    else:
        rows = [
            r
            for r in rows
            if topic in (r.get("key_terms") or [])
            or topic in str(r.get("title") or "")
        ]

    rows = rows[: max(limit * 5, 200)]
    _attach_meta(rows)
    ranked = [
        (
            row,
            rank_sector_article(
                title=str(row.get("title") or ""),
                event_tags=row.get("event_tags") or (),
                sentiment_label=row.get("sentiment_label"),
                source=str(row.get("source") or ""),
                source_domain=str(row.get("source_domain") or ""),
                published_at=row.get("published_at"),
                scope=scope,
            ),
        )
        for row in rows
    ]
    ranked.sort(
        key=lambda p: (
            p[1].weight,
            p[0].get("published_at") or datetime.min.replace(tzinfo=timezone.utc),
        ),
        reverse=True,
    )
    return {
        "success": True,
        "data": {
            "market": market,
            "topic": topic,
            "days": days,
            "items": [article_payload(row, rank) for row, rank in ranked[:limit]],
            "as_of": _as_utc_iso(datetime.now(timezone.utc)),
        },
    }


@router.get("/commodities")
async def commodities(
    days: int = Query(5, ge=1, le=30),
    limit: int = Query(30, ge=1, le=100),
    current_user: dict = Depends(get_current_user),
):
    _ = current_user
    since = datetime.now(timezone.utc) - timedelta(days=days)
    try:
        async with get_session(read_only=True) as session:
            rows = await _fetch_market_articles(
                session, "FUTURES", since=since, scan=40000, with_title=True
            )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"资讯数据暂不可用: {exc}") from exc
    return {
        "success": True,
        "data": {
            "days": days,
            "items": aggregate_commodities(rows, limit=limit),
            "as_of": _as_utc_iso(datetime.now(timezone.utc)),
        },
    }


@router.get("/subscriptions")
async def list_subscriptions(current_user: dict = Depends(get_current_user)):
    tenant, user_id = user_key(current_user)
    await _ensure_subscription_table()
    try:
        async with get_session(read_only=True) as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT symbol, name, pinned, sort_order, source, created_at "
                        "FROM qm_news_subscription WHERE tenant_id=:t AND user_id=:u "
                        "ORDER BY pinned DESC, sort_order ASC, created_at ASC"
                    ),
                    {"t": tenant, "u": user_id},
                )
            ).mappings().all()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"订阅读取失败: {exc}") from exc
    return {
        "success": True,
        "data": {
            "items": [
                {**dict(r), "created_at": _as_utc_iso(r.get("created_at"))} for r in rows
            ]
        },
    }


@router.post("/subscriptions")
async def add_subscription(
    payload: dict[str, Any],
    current_user: dict = Depends(get_current_user),
):
    tenant, user_id = user_key(current_user)
    symbol = suffix(str(payload.get("code") or payload.get("symbol") or ""))
    if "." not in symbol:
        raise HTTPException(status_code=400, detail="缺少有效的股票代码")
    await _ensure_subscription_table()
    try:
        async with get_session() as session:
            await session.execute(
                text(
                    """
                    INSERT INTO qm_news_subscription
                        (tenant_id, user_id, symbol, name, pinned, source)
                    VALUES (:t, :u, :s, :n, :p, 'manual')
                    ON CONFLICT (tenant_id, user_id, symbol)
                    DO UPDATE SET name = COALESCE(NULLIF(EXCLUDED.name, ''),
                                                  qm_news_subscription.name),
                                  pinned = EXCLUDED.pinned,
                                  source = CASE
                                      WHEN qm_news_subscription.source = 'live' THEN 'live'
                                      ELSE 'manual'
                                  END,
                                  updated_at = now()
                    """
                ),
                {
                    "t": tenant,
                    "u": user_id,
                    "s": symbol,
                    "n": str(payload.get("name") or ""),
                    "p": bool(payload.get("pinned", True)),
                },
            )
            await session.commit()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"订阅写入失败: {exc}") from exc
    return {"success": True, "data": {"code": symbol}}


@router.post("/subscriptions/sync-positions")
async def sync_positions(current_user: dict = Depends(get_current_user)):
    """把实盘快照 + 模拟盘持仓自动订阅（source=live / sim，幂等）。

    同一标的在两个来源都持有时按实盘记（source=live）。
    """
    tenant, user_id = user_key(current_user)
    await _ensure_subscription_table()
    try:
        async with get_session(read_only=True) as session:
            rows = (
                await session.execute(
                    text(
                        """
                        WITH latest AS (
                            SELECT DISTINCT ON (account_id) account_id, payload_json
                            FROM real_account_snapshots
                            WHERE tenant_id = :t AND user_id = :u
                              AND snapshot_date >= current_date - 3
                            ORDER BY account_id, snapshot_at DESC
                        ),
                        live AS (
                            SELECT p->>'symbol' AS symbol,
                                   NULLIF(p->>'name', '') AS name,
                                   'live' AS source
                            FROM latest,
                                 jsonb_array_elements(latest.payload_json->'positions') AS p
                            WHERE COALESCE((p->>'volume')::numeric, 0) > 0
                        ),
                        sim AS (
                            SELECT symbol, NULL AS name, 'sim' AS source
                            FROM simulation_position_lots
                            WHERE tenant_id = :t AND user_id = :u
                              AND quantity_remaining > 0
                              AND status = 'open'
                        ),
                        merged AS (
                            SELECT symbol,
                                   max(name) AS name,
                                   CASE WHEN bool_or(source = 'live') THEN 'live' ELSE 'sim' END
                                       AS source
                            FROM (SELECT * FROM live UNION ALL SELECT * FROM sim) u
                            GROUP BY symbol
                        )
                        SELECT symbol, name, source FROM merged
                        """
                    ),
                    {"t": tenant, "u": user_id},
                )
            ).mappings().all()
            entries: dict[str, dict[str, str]] = {}
            for r in rows:
                raw_symbol = str(r["symbol"] or "").strip()
                if not raw_symbol:
                    continue
                code = suffix(raw_symbol)
                if not is_supported_symbol(code):
                    continue
                prev = entries.get(code)
                source = str(r.get("source") or "live")
                if prev is None:
                    entries[code] = {"source": source, "name": str(r.get("name") or "")}
                else:
                    if source == "live":
                        prev["source"] = "live"
                    if not prev["name"] and r.get("name"):
                        prev["name"] = str(r.get("name"))
            symbols = sorted(entries)
            names = await _stock_names(session, symbols)
    except Exception as exc:  # noqa: BLE001
        return {
            "success": True,
            "data": {
                "synced": 0,
                "live": 0,
                "sim": 0,
                "symbols": [],
                "reason": f"持仓快照/模拟盘不可用: {exc}",
            },
        }

    if not symbols:
        return {
            "success": True,
            "data": {
                "synced": 0,
                "live": 0,
                "sim": 0,
                "symbols": [],
                "reason": "近 3 日无实盘持仓快照，且模拟盘无持仓",
            },
        }

    try:
        async with get_session() as session:
            for code in symbols:
                entry = entries[code]
                await session.execute(
                    text(
                        """
                        INSERT INTO qm_news_subscription
                            (tenant_id, user_id, symbol, name, pinned, source, last_synced_at)
                        VALUES (:t, :u, :s, :n, TRUE, :src, now())
                        ON CONFLICT (tenant_id, user_id, symbol)
                        DO UPDATE SET
                            source = EXCLUDED.source,
                            name = COALESCE(NULLIF(EXCLUDED.name, ''),
                                            qm_news_subscription.name),
                            last_synced_at = now(),
                            updated_at = now()
                        """
                    ),
                    {
                        "t": tenant,
                        "u": user_id,
                        "s": code,
                        "n": entry["name"] or names.get(code) or "",
                        "src": entry["source"],
                    },
                )
            await session.commit()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"持仓订阅写入失败: {exc}") from exc
    live_n = sum(1 for e in entries.values() if e["source"] == "live")
    sim_n = len(entries) - live_n
    return {
        "success": True,
        "data": {
            "synced": len(symbols),
            "live": live_n,
            "sim": sim_n,
            "symbols": symbols,
        },
    }


@router.patch("/subscriptions/{code}")
async def update_subscription(
    code: str,
    payload: dict[str, Any],
    current_user: dict = Depends(get_current_user),
):
    tenant, user_id = user_key(current_user)
    symbol = suffix(code)
    await _ensure_subscription_table()
    fields: list[str] = []
    params: dict[str, Any] = {"t": tenant, "u": user_id, "s": symbol}
    if "pinned" in payload:
        fields.append("pinned = :p")
        params["p"] = bool(payload.get("pinned"))
    if "sort_order" in payload:
        fields.append("sort_order = :o")
        params["o"] = int(payload.get("sort_order") or 0)
    if not fields:
        raise HTTPException(status_code=400, detail="没有可更新字段")
    try:
        async with get_session() as session:
            result = await session.execute(
                text(
                    f"UPDATE qm_news_subscription SET {', '.join(fields)}, updated_at = now() "
                    "WHERE tenant_id=:t AND user_id=:u AND symbol=:s"
                ),
                params,
            )
            await session.commit()
            if result.rowcount == 0:
                raise HTTPException(status_code=404, detail="未订阅该股票")
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"订阅更新失败: {exc}") from exc
    return {"success": True, "data": {"code": symbol}}


@router.delete("/subscriptions/{code}")
async def remove_subscription(code: str, current_user: dict = Depends(get_current_user)):
    tenant, user_id = user_key(current_user)
    symbol = suffix(code)
    await _ensure_subscription_table()
    try:
        async with get_session() as session:
            await session.execute(
                text(
                    "DELETE FROM qm_news_subscription "
                    "WHERE tenant_id=:t AND user_id=:u AND symbol=:s"
                ),
                {"t": tenant, "u": user_id, "s": symbol},
            )
            await session.commit()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"订阅删除失败: {exc}") from exc
    return {"success": True, "data": {"code": symbol}}


@router.get("/feed")
async def subscription_feed(
    days: int = Query(14, ge=1, le=90),
    limit: int = Query(50, ge=1, le=200),
    current_user: dict = Depends(get_current_user),
):
    tenant, user_id = user_key(current_user)
    await _ensure_subscription_table()
    try:
        async with get_session(read_only=True) as session:
            subs = (
                await session.execute(
                    text(
                        "SELECT symbol, name, pinned, sort_order, source FROM qm_news_subscription "
                        "WHERE tenant_id=:t AND user_id=:u"
                    ),
                    {"t": tenant, "u": user_id},
                )
            ).mappings().all()
            symbols = [suffix(str(r["symbol"])) for r in subs]
            if not symbols:
                return {"success": True, "data": {"items": [], "subscribed": []}}
            rows = await _fetch_articles(
                session,
                symbols,
                since=datetime.now(timezone.utc) - timedelta(days=days),
                scan=_ARTICLE_SCAN_MAX,
            )
            names = await _stock_names(session, symbols)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"订阅动态读取失败: {exc}") from exc

    _attach_meta(rows)
    pinned = {suffix(str(r["symbol"])): bool(r["pinned"]) for r in subs}
    order = {suffix(str(r["symbol"])): int(r["sort_order"] or 0) for r in subs}
    pre5_map = _pre5_map(datetime.now(CN_TZ).strftime("%Y%m%d"))

    per_code: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        for ticker in row.get("tickers") or []:
            key = suffix(str(ticker))
            if key in pinned:
                per_code.setdefault(key, []).append(row)

    out: list[tuple[bool, int, int, dict[str, Any]]] = []
    for code, code_rows in per_code.items():
        name = names.get(code) or next(
            (str(r.get("name") or "") for r in subs if suffix(str(r["symbol"])) == code), ""
        )
        for row, rank in rank_rows(
            code_rows, code=code, name=name, pre5=pre5_lookup(code, pre5_map)
        ):
            item = article_payload(row, rank)
            item["code"] = code
            item["name"] = name
            out.append((pinned.get(code, False), order.get(code, 0), rank.weight, item))

    out.sort(key=lambda x: (x[0], x[1], x[2]), reverse=True)
    return {
        "success": True,
        "data": {
            "items": [x[3] for x in out[:limit]],
            "subscribed": [dict(r) for r in subs],
        },
    }
