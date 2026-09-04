"""Huntly 资讯聚合代理 — /api/v1/news/*

后端代理 lcomplete/huntly:latest 的 REST API：
- 首次启动自动注册管理员账号 (POST /api/auth/signup)
- 已存在则登录 (POST /api/auth/signin) 拿 JSESSIONID
- 把 Huntly 的 Folder / Connector / Page 模型转译成 QuantMind 风格 JSON
- 前端无须知道 Huntly 存在，零 401 风险

环境变量：
- HUNTLY_BASE_URL    默认 http://quantmind-huntly
- HUNTLY_USERNAME    默认 admin
- HUNTLY_PASSWORD    默认 quantmind2026
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response

try:
    from zoneinfo import ZoneInfo
    _HUNTLY_TZ = ZoneInfo("Asia/Shanghai")
except Exception:
    _HUNTLY_TZ = timezone.utc

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/news", tags=["News"])

HUNTLY_BASE_URL = os.getenv("HUNTLY_BASE_URL", "http://quantmind-huntly").rstrip("/")
HUNTLY_USERNAME = os.getenv("HUNTLY_USERNAME", "")
HUNTLY_PASSWORD = os.getenv("HUNTLY_PASSWORD", "")
HUNTLY_TIMEOUT = float(os.getenv("HUNTLY_TIMEOUT_SECONDS", "20"))
RSSHUB_BASE_URL = os.getenv("RSSHUB_BASE_URL", "http://quantmind-rsshub:1200").rstrip("/")
# Huntly 把全部 page 写入此 SQLite (Asia/Shanghai 本地时间字符串).
# REST /api/page/list 上限 500, 拿不到几万条历史 — 我们直接读 SQLite 做真分页.
HUNTLY_SQLITE_PATH = os.getenv("HUNTLY_SQLITE_PATH", "/data/huntly/db.sqlite")


def _public_connector_icon_url(icon_url: Any, request: Request) -> Any:
    """将 Docker 内部 RSSHub 图标地址转换为浏览器可访问的 API 代理地址。"""
    if not isinstance(icon_url, str) or not icon_url:
        return icon_url
    parsed = urlsplit(icon_url)
    if parsed.hostname not in {"quantmind-rsshub", "rsshub"}:
        return icon_url
    path = quote(parsed.path.lstrip("/"), safe="/%")
    if parsed.query:
        path = f"{path}?{parsed.query}"
    return f"{str(request.base_url).rstrip('/')}/api/v1/news/rsshub/{path}"


# ---------- enrichment ----------

def _pg_conn():
    import psycopg2
    return psycopg2.connect(
        host=os.getenv("POSTGRES_HOST", "quantmind-db"),
        port=int(os.getenv("POSTGRES_PORT", "5432")),
        user=os.getenv("POSTGRES_USER", "quantmind"),
        password=os.getenv("POSTGRES_PASSWORD", "quantmind2026"),
        dbname=os.getenv("POSTGRES_DB", "quantmind"),
    )


def _load_enrichments(page_ids: list[int]) -> dict[int, dict]:
    if not page_ids:
        return {}
    try:
        with _pg_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT huntly_page_id, tickers, industries, event_tags, "
                "sentiment_score, sentiment_label, sentiment_confidence, "
                "countries, regions, key_terms, date_entities, entity_sentiments, "
                "provinces, cities, politicians, visits, departments "
                "FROM news_article_enrichment WHERE huntly_page_id = ANY(%s)",
                (page_ids,),
            )
            rows = cur.fetchall()
        out: dict[int, dict] = {}
        for (pid, tickers, industries, ev, score, label, conf,
             countries, regions, key_terms, date_entities, ent_sent,
             provinces, cities, politicians, visits, departments) in rows:
            out[int(pid)] = {
                "tickers": list(tickers or []),
                "industries": list(industries or []),
                "event_tags": list(ev or []),
                "sentiment_score": float(score) if score is not None else None,
                "sentiment_label": label,
                "sentiment_confidence": float(conf) if conf is not None else None,
                "countries": list(countries or []),
                "regions": list(regions or []),
                "key_terms": list(key_terms or []),
                "date_entities": list(date_entities or []),
                "entity_sentiments": dict(ent_sent) if ent_sent else {},
                "provinces": list(provinces or []),
                "cities": list(cities or []),
                "politicians": list(politicians or []),
                "visits": list(visits or []),
                "departments": list(departments or []),
            }
        return out
    except Exception as e:
        logger.warning("加载 enrichment 失败: %s", e)
        return {}


def _empty_enrichment() -> dict:
    return {
        "tickers": [],
        "industries": [],
        "event_tags": [],
        "sentiment_score": None,
        "sentiment_label": None,
        "sentiment_confidence": None,
        "countries": [],
        "regions": [],
        "key_terms": [],
        "date_entities": [],
        "entity_sentiments": {},
        "provinces": [],
        "cities": [],
        "politicians": [],
        "visits": [],
        "departments": [],
    }


def _build_enrichment_where(
    *,
    want_tickers: set[str] | None = None,
    want_industries: set[str] | None = None,
    want_event_tags: set[str] | None = None,
    want_sentiment: str | None = None,
    strong_only: bool = False,
    want_countries: set[str] | None = None,
    want_regions: set[str] | None = None,
    want_key_terms: set[str] | None = None,
    want_date_entities: set[str] | None = None,
    want_provinces: set[str] | None = None,
    want_cities: set[str] | None = None,
    want_politicians: set[str] | None = None,
    want_visits: set[str] | None = None,
    want_departments: set[str] | None = None,
    only_ids: set[int] | None = None,
) -> tuple[list[str], list]:
    where: list[str] = []
    params: list = []
    if want_tickers:
        where.append("tickers && %s"); params.append(list(want_tickers))
    if want_industries:
        where.append("industries && %s"); params.append(list(want_industries))
    if want_event_tags:
        where.append("event_tags && %s"); params.append(list(want_event_tags))
    if want_countries:
        where.append("countries && %s"); params.append(list(want_countries))
    if want_regions:
        where.append("regions && %s"); params.append(list(want_regions))
    if want_key_terms:
        where.append("key_terms && %s"); params.append(list(want_key_terms))
    if want_date_entities:
        where.append("date_entities && %s"); params.append(list(want_date_entities))
    if want_provinces:
        where.append("provinces && %s"); params.append(list(want_provinces))
    if want_cities:
        where.append("cities && %s"); params.append(list(want_cities))
    if want_politicians:
        where.append("politicians && %s"); params.append(list(want_politicians))
    if want_visits:
        where.append("visits && %s"); params.append(list(want_visits))
    if want_departments:
        where.append("departments && %s"); params.append(list(want_departments))
    if want_sentiment:
        where.append("sentiment_label = %s"); params.append(want_sentiment)
    if strong_only:
        where.append("abs(coalesce(sentiment_score, 0)) >= 0.5")
    if only_ids is not None:
        where.append("huntly_page_id = ANY(%s)"); params.append(list(only_ids))
    return where, params


def _query_enrichment_page_ids(
    want_tickers: set[str],
    want_industries: set[str],
    want_event_tags: set[str],
    want_sentiment: str | None,
    strong_only: bool,
    want_countries: set[str] | None = None,
    want_regions: set[str] | None = None,
    want_key_terms: set[str] | None = None,
    want_date_entities: set[str] | None = None,
    want_provinces: set[str] | None = None,
    want_cities: set[str] | None = None,
    want_politicians: set[str] | None = None,
    want_visits: set[str] | None = None,
    want_departments: set[str] | None = None,
    keyword: str | None = None,
    restrict_to_ids: list[int] | None = None,
    limit: int = 2000,
) -> list[int]:
    """根据 enrichment 表里的过滤条件，倒排查出所有命中文章的 huntly_page_id。

    keyword: 同时匹配 tickers / industries / event_tags / countries / regions /
             key_terms / provinces / cities / politicians / visits
             (用户输入的关键词可能是股票代码、行业名、地名、人名 — 在这里一并模糊匹配)。
             标题/内容的关键词搜索仍然走 Huntly 的 `q` 参数。
    """
    where, params = _build_enrichment_where(
        want_tickers=want_tickers,
        want_industries=want_industries,
        want_event_tags=want_event_tags,
        want_sentiment=want_sentiment,
        strong_only=strong_only,
        want_countries=want_countries,
        want_regions=want_regions,
        want_key_terms=want_key_terms,
        want_date_entities=want_date_entities,
        want_provinces=want_provinces,
        want_cities=want_cities,
        want_politicians=want_politicians,
        want_visits=want_visits,
        want_departments=want_departments,
    )
    if keyword:
        # 关键词匹配：PG title 列（trigram 索引，毫秒级）+ enrichment 标签数组。
        # title 优先——用户搜"茅台"应命中标题含茅台的新闻，而不是只在标签里碰运气。
        where.append(
            "(title ILIKE %s OR array_to_string(tickers || industries || event_tags "
            "|| countries || regions || key_terms || provinces || cities || "
            "politicians || visits || departments, ',') ILIKE %s)"
        )
        params.append(f"%{keyword}%")
        params.append(f"%{keyword}%")
    if restrict_to_ids is not None:
        if not restrict_to_ids:
            return []
        # 把候选 ID 集合作为额外过滤 — 避开 ORDER BY enriched_at LIMIT 5000 的近期偏置
        where.append("huntly_page_id = ANY(%s)")
        params.append(list(restrict_to_ids))
    if not where:
        return []
    sql = (
        "SELECT huntly_page_id FROM news_article_enrichment "
        "WHERE " + " AND ".join(where) + " "
        "ORDER BY enriched_at DESC LIMIT %s"
    )
    params.append(int(limit))
    try:
        with _pg_conn() as conn, conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            return [int(r[0]) for r in cur.fetchall()]
    except Exception as e:
        logger.warning("enrichment 倒排查询失败: %s", e)
        return []


# 财经事件关键词 (用于把普通文章打上 "financial_event" 标记)
_FINANCIAL_EVENT_KEYWORDS = (
    "减持", "增持", "回购", "公告", "业绩快报", "业绩预告", "重大事项",
    "股权激励", "分红", "送转", "停牌", "复牌", "ST", "退市",
    "IPO", "并购", "重组", "定增", "可转债", "中标",
)

# Huntly 鉴权：JWT (auth_token cookie + Bearer header 都能用)
# /api/auth/signin 返回 {"code":0, "data":"<jwt>"}, 同时 Set-Cookie: auth_token=<jwt>
_SESSION_LOCK = asyncio.Lock()
_SESSION_TOKEN: str | None = None
_SESSION_EXPIRES_AT: float = 0.0
_SESSION_TTL = 30 * 60  # 30 分钟内复用


# ── 全局 httpx 客户端（懒初始化，连接复用） ──
_http_client: httpx.AsyncClient | None = None


async def _get_http_client() -> httpx.AsyncClient:
    """Return a shared httpx.AsyncClient singleton for Huntly requests."""
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            base_url=HUNTLY_BASE_URL,
            timeout=HUNTLY_TIMEOUT,
        )
    return _http_client


def _unwrap(body: Any) -> Any:
    """Huntly 统一返回 {code, message, data} 包装；抽出 data。"""
    if isinstance(body, dict) and "data" in body and "code" in body:
        return body.get("data")
    return body


async def _ensure_session() -> str:
    """获取有效 JWT；若 Huntly 未设置用户则自动 signup 再 signin。"""
    global _SESSION_TOKEN, _SESSION_EXPIRES_AT

    if _SESSION_TOKEN and time.time() < _SESSION_EXPIRES_AT:
        return _SESSION_TOKEN

    async with _SESSION_LOCK:
        if _SESSION_TOKEN and time.time() < _SESSION_EXPIRES_AT:
            return _SESSION_TOKEN

        client = await _get_http_client()
        # 1. 探测是否已设置用户 — Huntly 返回 {"code":0, "data": <bool>}
        user_set = False
        try:
            r = await client.get("/api/auth/isUserSet")
            if r.status_code == 200:
                user_set = bool(_unwrap(r.json()))
        except Exception as exc:
            logger.warning("huntly isUserSet probe failed: %s", exc)

        # 2. 未设置则注册（已存在会返回 BusinessException 5101，忽略即可）
        if not user_set:
            try:
                r = await client.post(
                    "/api/auth/signup",
                    json={"username": HUNTLY_USERNAME, "password": HUNTLY_PASSWORD},
                )
                logger.info(
                    "huntly signup status=%s body=%s",
                    r.status_code, r.text[:200],
                )
            except Exception as exc:
                logger.warning("huntly signup failed: %s", exc)

        # 3. 登录拿 JWT
        r = await client.post(
            "/api/auth/signin",
            json={"username": HUNTLY_USERNAME, "password": HUNTLY_PASSWORD},
        )
        if r.status_code >= 300:
            raise HTTPException(
                status_code=502,
                detail=f"Huntly signin failed: HTTP {r.status_code} {r.text[:200]}",
            )

        token = _unwrap(r.json()) if r.headers.get("content-type", "").startswith("application/json") else None
        if not token:
            # 回退到 Set-Cookie: auth_token=...
            token = r.cookies.get("auth_token")
        if not token:
            set_cookie = r.headers.get("set-cookie", "")
            if "auth_token=" in set_cookie:
                token = set_cookie.split("auth_token=", 1)[1].split(";", 1)[0]

        if not token or not isinstance(token, str):
            raise HTTPException(
                status_code=502,
                detail="Huntly signin succeeded but no JWT returned",
            )

        _SESSION_TOKEN = token
        _SESSION_EXPIRES_AT = time.time() + _SESSION_TTL
        logger.info("huntly session established (jwt len=%d)", len(token))
        return token


async def _huntly_request(
    method: str,
    path: str,
    *,
    params: dict | None = None,
    json: Any = None,
    retry_on_401: bool = True,
) -> httpx.Response:
    """带 JWT 的 Huntly 调用，401 自动重登一次。"""
    token = await _ensure_session()
    headers = {
        "Authorization": f"Bearer {token}",
        "Cookie": f"auth_token={token}",
    }
    client = await _get_http_client()
    r = await client.request(method, path, params=params, json=json, headers=headers)
    if r.status_code in (401, 403) and retry_on_401:
        global _SESSION_TOKEN, _SESSION_EXPIRES_AT
        _SESSION_TOKEN = None
        _SESSION_EXPIRES_AT = 0
        return await _huntly_request(
            method, path, params=params, json=json, retry_on_401=False
        )
    return r


def _is_financial_event(title: str | None, summary: str | None) -> bool:
    haystack = (title or "") + " " + (summary or "")
    return any(kw in haystack for kw in _FINANCIAL_EVENT_KEYWORDS)


def _huntly_sqlite_available() -> bool:
    return os.path.exists(HUNTLY_SQLITE_PATH)


def _huntly_sqlite() -> sqlite3.Connection:
    # uri+immutable 让 sqlite 完全跳过锁协商（mode=ro 会抢共享锁，被 Huntly
    # 写锁阻塞导致搜索卡死十几秒；immutable 直接读快照，毫秒级返回）
    uri = f"file:{HUNTLY_SQLITE_PATH}?immutable=1"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _huntly_dt_to_iso(s: str | None) -> str | None:
    """Huntly 把时间存成 'YYYY-MM-DD HH:MM:SS.fff' 上海本地时间. 转 UTC ISO 给前端."""
    if not s or s.startswith("0001-"):
        return None
    try:
        # 既有 'YYYY-MM-DD HH:MM:SS.fff' 也可能 'YYYY-MM-DDTHH:MM:SSZ'
        if "T" in s:
            return s if s.endswith("Z") or "+" in s[10:] else s + "Z"
        s2 = s.replace(" ", "T")
        if "." in s2:
            head, _, frac = s2.partition(".")
            frac = (frac + "000")[:3]
            s2 = f"{head}.{frac}"
        d = datetime.fromisoformat(s2).replace(tzinfo=_HUNTLY_TZ)
        return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return None


def _sqlite_page_ids_in_range(
    *,
    source_id: int | None,
    folder_id: int | None,
    starred: bool | None,
    since_iso: str | None,
    until_iso: str | None,
    limit: int = 50000,
) -> list[int]:
    """从 SQLite 按 source/folder/starred/日期范围拿候选 page id (不做关键词过滤).

    用于「标签筛选 + 日期筛选」组合时, 先用日期裁出候选集再让 PG 在其中匹配标签,
    避免 PG ORDER BY enriched_at DESC LIMIT 5000 的近期偏置.
    """
    where: list[str] = []
    params: list = []
    if source_id is not None:
        where.append("connector_id = ?"); params.append(int(source_id))
    if folder_id is not None and folder_id > 0:
        where.append("folder_id = ?"); params.append(int(folder_id))
    if starred is True:
        where.append("is_starred = 1")

    def _iso_to_local_str(iso: str | None) -> str | None:
        if not iso:
            return None
        try:
            d = datetime.fromisoformat(iso.replace("Z", "+00:00"))
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            return d.astimezone(_HUNTLY_TZ).strftime("%Y-%m-%d %H:%M:%S.000")
        except Exception:
            return None

    s = _iso_to_local_str(since_iso)
    u = _iso_to_local_str(until_iso)
    if s:
        where.append("connected_at >= ?"); params.append(s)
    if u:
        where.append("connected_at <= ?"); params.append(u)

    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    sql = f"SELECT id FROM page{where_sql} ORDER BY connected_at DESC LIMIT ?"
    params.append(int(limit))
    try:
        with _huntly_sqlite() as conn:
            cur = conn.cursor()
            cur.execute(sql, params)
            return [int(r["id"]) for r in cur.fetchall()]
    except sqlite3.Error as e:
        logger.warning("sqlite 候选 ID 查询失败: %s", e)
        return []


def _sqlite_keyword_ids(
    *,
    keyword: str,
    source_id: int | None,
    folder_id: int | None,
    starred: bool | None,
    since_iso: str | None,
    until_iso: str | None,
    limit: int = 5000,
) -> list[int]:
    """从 SQLite 按关键词在标题/描述/正文中匹配, 返回 page id 列表.

    搜索范围: page.title + page.description + page_article_content.content (全文).
    用于 enrichment 标签搜索 + SQLite 全文搜索的 UNION 合并.
    """
    where: list[str] = []
    params: list = []
    if source_ids:
        placeholders = ",".join(["?"] * len(source_ids))
        where.append(f"p.connector_id IN ({placeholders})")
        params.extend(int(sid) for sid in source_ids)
    elif source_id is not None:
        where.append("p.connector_id = ?"); params.append(int(source_id))
    if folder_id is not None and folder_id > 0:
        where.append("p.folder_id = ?"); params.append(int(folder_id))
    if starred is True:
        where.append("p.is_starred = 1")

    def _iso_to_local_str(iso: str | None) -> str | None:
        if not iso:
            return None
        try:
            d = datetime.fromisoformat(iso.replace("Z", "+00:00"))
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            local = d.astimezone(_HUNTLY_TZ)
            return local.strftime("%Y-%m-%d %H:%M:%S.000")
        except Exception:
            return None

    s = _iso_to_local_str(since_iso)
    u = _iso_to_local_str(until_iso)
    if s:
        where.append("p.connected_at >= ?"); params.append(s)
    if u:
        where.append("p.connected_at <= ?"); params.append(u)

    kw = f"%{keyword}%"
    # 标题/描述/正文三路匹配, EXISTS 子查询避免 JOIN 产生重复
    where.append(
        "(p.title LIKE ? OR p.description LIKE ? OR EXISTS "
        "(SELECT 1 FROM page_article_content pac WHERE pac.page_id = p.id AND pac.content LIKE ?))"
    )
    params.extend([kw, kw, kw])

    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    sql = f"SELECT DISTINCT p.id FROM page p{where_sql} ORDER BY p.connected_at DESC LIMIT ?"
    params.append(int(limit))
    try:
        with _huntly_sqlite() as conn:
            cur = conn.cursor()
            cur.execute(sql, params)
            return [int(r["id"]) for r in cur.fetchall()]
    except sqlite3.Error as e:
        logger.warning("sqlite 关键词 ID 查询失败: %s", e)
        return []


def _list_articles_from_sqlite(
    *,
    source_id: int | None,
    folder_id: int | None,
    source_ids: list[int] | None,
    keyword: str | None,
    only_financial_event: bool,
    starred: bool | None,
    since_iso: str | None,
    until_iso: str | None,
    only_ids: list[int] | None,
    offset: int,
    limit: int,
    sort: str = "time_desc",
) -> tuple[list[dict], int, str | None]:
    """直读 Huntly SQLite, 返回 (page_articles, total, latest_published_at_iso).

    分页策略: SQL 真 offset/limit + 真 COUNT(*), 全库可分页, 不再受 REST count<=500 限制.
    """
    where: list[str] = []
    params: list = []

    if source_ids:
        placeholders = ",".join(["?"] * len(source_ids))
        where.append(f"p.connector_id IN ({placeholders})")
        params.extend(int(sid) for sid in source_ids)
    elif source_id is not None:
        where.append("p.connector_id = ?"); params.append(int(source_id))
    if folder_id is not None and folder_id > 0:
        where.append("p.folder_id = ?"); params.append(int(folder_id))
    if starred is True:
        where.append("p.is_starred = 1")

    # 时间过滤: 把 ISO UTC 转回上海本地, 直接走字符串比较 (Huntly 字段格式可比)
    def _iso_to_local_str(iso: str | None) -> str | None:
        if not iso:
            return None
        try:
            d = datetime.fromisoformat(iso.replace("Z", "+00:00"))
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            local = d.astimezone(_HUNTLY_TZ)
            return local.strftime("%Y-%m-%d %H:%M:%S.000")
        except Exception:
            return None

    since_local = _iso_to_local_str(since_iso)
    until_local = _iso_to_local_str(until_iso)
    if since_local:
        where.append("p.connected_at >= ?"); params.append(since_local)
    if until_local:
        where.append("p.connected_at <= ?"); params.append(until_local)

    if keyword and keyword.strip() and only_ids is None:
        # 仅在无 only_ids 限制时走 SQLite 关键词搜索;
        # 有 only_ids 时关键词匹配已在上游 enrichment + UNION 合并中完成.
        kw = f"%{keyword.strip()}%"
        # 标题/描述/正文三路匹配, EXISTS 子查询避免 JOIN 产生重复
        where.append(
            "(p.title LIKE ? OR p.description LIKE ? OR EXISTS "
            "(SELECT 1 FROM page_article_content pac WHERE pac.page_id = p.id AND pac.content LIKE ?))"
        )
        params.extend([kw, kw, kw])

    if only_financial_event:
        # SQL 内 OR (title LIKE %kw%) — 词表很短, 接受 N 次 OR
        ev_clauses = []
        for kw in _FINANCIAL_EVENT_KEYWORDS:
            ev_clauses.append("p.title LIKE ?")
            params.append(f"%{kw}%")
        where.append("(" + " OR ".join(ev_clauses) + ")")

    if only_ids is not None:
        if not only_ids:
            return [], 0, None
        # SQLite 不支持 ANY(array), 用 IN(?,?,...). 控制大小避免参数爆炸.
        ids = list(only_ids)[:5000]
        placeholders = ",".join(["?"] * len(ids))
        where.append(f"p.id IN ({placeholders})")
        params.extend(ids)

    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    sql_count = f"SELECT COUNT(*) FROM page p{where_sql}"

    # 排序: time_desc (默认) / time_asc / sentiment_bullish / sentiment_bearish
    order = "p.connected_at DESC"
    if sort == "time_asc":
        order = "p.connected_at ASC"

    if sort in ("sentiment_bullish", "sentiment_bearish") and only_ids:
        # 按情感排序时, 需要 only_ids (来自 enrichment 过滤) 且需要在 PG 端排序
        pass  # 排序在下方 Python 层处理

    sql_list = (
        "SELECT p.id, p.title, p.description, p.url, p.connector_id, p.source_id, "
        "       p.folder_id, p.connected_at, p.created_at, p.is_mark_read, p.is_starred, "
        "       p.thumb_url, c.name AS connector_name, c.icon_url AS connector_icon "
        "FROM page p LEFT JOIN connector c ON c.id = p.connector_id"
        f"{where_sql} "
        f"ORDER BY {order} "
        "LIMIT ? OFFSET ?"
    )

    try:
        with _huntly_sqlite() as conn:
            cur = conn.cursor()
            cur.execute(sql_count, params)
            total = int(cur.fetchone()[0])
            cur.execute(sql_list, params + [int(limit), int(offset)])
            rows = cur.fetchall()
    except sqlite3.Error as e:
        logger.error("huntly sqlite query failed: %s", e)
        raise HTTPException(status_code=502, detail=f"Huntly DB 查询失败: {e}")

    articles: list[dict] = []
    latest: str | None = None
    for row in rows:
        published = _huntly_dt_to_iso(row["connected_at"]) or _huntly_dt_to_iso(row["created_at"])
        if latest is None and published:
            latest = published
        title = row["title"] or "(无标题)"
        summary = row["description"] or ""
        if summary and len(summary) > 280:
            summary = summary[:280] + "..."
        articles.append({
            "id": int(row["id"]),
            "title": title,
            "summary": summary or None,
            "url": row["url"],
            "source_id": row["connector_id"] or row["source_id"],
            "source_name": row["connector_name"] or "未知来源",
            "folder_id": row["folder_id"],
            "published_at": published,
            "read": bool(row["is_mark_read"]),
            "starred": bool(row["is_starred"]),
            "is_financial_event": _is_financial_event(title, summary),
            "thumbnail": row["thumb_url"] or row["connector_icon"],
        })
    return articles, total, latest


def _normalize_page(page: dict) -> dict:
    """把 Huntly Page 转成 QuantMind News Article 标准结构。"""
    title = page.get("title") or page.get("siteName") or "(无标题)"
    summary = page.get("description") or page.get("content")
    if summary and len(summary) > 280:
        summary = summary[:280] + "..."

    # Huntly 的字段：connectedAt > recordAt > pubDate > createdAt
    published_at = (
        page.get("connectedAt")
        or page.get("recordAt")
        or page.get("pubDate")
        or page.get("createdAt")
    )

    return {
        "id": page.get("id"),
        "title": title,
        "summary": summary,
        "url": page.get("url"),
        "source_id": page.get("connectorId") or page.get("sourceId"),
        "source_name": page.get("siteName") or page.get("domain"),
        "folder_id": page.get("folderId"),
        "published_at": published_at,
        "read": bool(page.get("markRead")),
        "starred": bool(page.get("starred")),
        "is_financial_event": _is_financial_event(title, summary),
        "thumbnail": page.get("thumbUrl") or page.get("faviconUrl"),
    }


# ---------------------------------------------------------------------------
# 公开路由
# ---------------------------------------------------------------------------


@router.get("/health")
async def news_health():
    """检查 Huntly 上游连通性 (无须登录)"""
    try:
        client = await _get_http_client()
        r = await client.get("/api/health")
        return {
            "huntly_status": "up" if r.status_code == 200 else "down",
            "huntly_http_code": r.status_code,
            "huntly_base_url": HUNTLY_BASE_URL,
        }
    except Exception as exc:
        return {
            "huntly_status": "unreachable",
            "huntly_base_url": HUNTLY_BASE_URL,
            "error": str(exc),
        }


@router.get("/rsshub/{path:path}", include_in_schema=False)
async def proxy_rsshub_asset(path: str, request: Request):
    """代理 RSSHub 静态资源，避免把 Docker 服务名暴露给浏览器。"""
    target = f"{RSSHUB_BASE_URL}/{path.lstrip('/')}"
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            upstream = await client.get(target, params=request.query_params)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"RSSHub 资源不可用: {exc}") from exc

    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type"),
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.get("/sources")
async def list_sources(request: Request):
    """列出所有订阅源 (Huntly Folder + Connector)

    Huntly 真实端点是 GET /api/connector/folder-connectors，
    返回 {folderFeedConnectors: [{id, name, connectorItems: [...]}, ...]}
    folder.id=null 表示 "未分组"。
    """
    r = await _huntly_request("GET", "/api/connector/folder-connectors")
    if r.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"Huntly /connector/folder-connectors HTTP {r.status_code}",
        )

    body = _unwrap(r.json()) or {}
    folders_raw = body.get("folderFeedConnectors") if isinstance(body, dict) else body
    folders_raw = folders_raw or []

    sources: list[dict] = []
    folders_summary: list[dict] = []

    for folder in folders_raw:
        folder_id = folder.get("id")
        folder_name = folder.get("name") or "未分组"
        items = folder.get("connectorItems") or []
        folders_summary.append({
            "folder_id": folder_id if folder_id is not None else 0,
            "folder_name": folder_name,
            "source_count": len(items),
            "unread_count": sum(int(it.get("inboxCount") or 0) for it in items),
        })
        for conn in items:
            sources.append({
                "source_id": conn.get("id"),
                "source_name": conn.get("name") or "(未命名)",
                "subscribe_url": conn.get("subscribeUrl"),
                "type": conn.get("type"),
                "folder_id": folder_id if folder_id is not None else 0,
                "folder_name": folder_name,
                "site_avatar_url": _public_connector_icon_url(
                    conn.get("iconUrl"), request
                ),
                "unread_count": int(conn.get("inboxCount") or 0),
            })

    return {
        "sources": sources,
        "folders": folders_summary,
        "total": len(sources),
    }


@router.post("/sources/{source_id}/refresh")
async def refresh_source(source_id: int):
    """手动触发抓取（Huntly 上游 v0.5.x 未公开 fetchNow 端点，这里仅作占位返回 202）"""
    return {
        "ok": False,
        "source_id": source_id,
        "message": "当前 Huntly 版本未暴露手动抓取接口，请等待下一次定时抓取（每 1 小时）",
    }


# ---------------------------------------------------------------------------
# Admin: RSS 源 CRUD (代理 Huntly /api/setting/{feeds,folder}/*)
# ---------------------------------------------------------------------------

def _huntly_error(r: httpx.Response, action: str) -> HTTPException:
    detail = r.text[:300] if r.text else f"HTTP {r.status_code}"
    return HTTPException(
        status_code=502 if r.status_code >= 500 else r.status_code,
        detail=f"Huntly {action} 失败: {detail}",
    )


@router.get("/admin/folders")
async def admin_list_folders(request: Request):
    """列出所有文件夹（含其下的 connector 概览）

    Huntly 的 /setting/folder/all 只返回文件夹元信息，connector 列表是空的。
    需要再调一次 /connector/folder-connectors 把真实订阅源合并进来。
    """
    r1 = await _huntly_request("GET", "/api/setting/folder/all")
    if r1.status_code != 200:
        raise _huntly_error(r1, "folder/all")
    folders = list(_unwrap(r1.json()) or [])

    r2 = await _huntly_request("GET", "/api/connector/folder-connectors")
    if r2.status_code != 200:
        raise _huntly_error(r2, "connector/folder-connectors")
    body2 = _unwrap(r2.json()) or {}
    ffc = (
        body2.get("folderFeedConnectors")
        if isinstance(body2, dict)
        else body2
    ) or []

    # 索引：folder_id -> connectors（folder_id=None 视为未分组）
    conn_by_folder: dict[int | None, list[dict]] = {}
    seen_folder_ids: set[int | None] = set()
    for f in ffc:
        fid = f.get("id")  # None = 未分组
        conn_by_folder[fid] = [
            {
                **item,
                "iconUrl": _public_connector_icon_url(item.get("iconUrl"), request),
            }
            for item in (f.get("connectorItems") or [])
        ]
        seen_folder_ids.add(fid)

    merged: list[dict] = []
    have_ungrouped = False
    for fdr in folders:
        fid = fdr.get("id")
        items = conn_by_folder.get(fid, [])
        merged.append({**fdr, "connectors": items})
        if fid is None:
            have_ungrouped = True

    # folder/all 不一定返回未分组占位；ffc 里若有 id=None 的桶就补一条
    if not have_ungrouped and None in seen_folder_ids:
        merged.insert(
            0,
            {
                "id": None,
                "name": None,
                "displaySequence": None,
                "createdAt": None,
                "connectors": conn_by_folder[None],
            },
        )

    # folder/all 也可能漏掉某个 ffc 文件夹（极少见），兜底补齐
    known_ids = {f.get("id") for f in merged}
    for fid, items in conn_by_folder.items():
        if fid is None or fid in known_ids:
            continue
        merged.append(
            {
                "id": fid,
                "name": f"#{fid}",
                "displaySequence": None,
                "createdAt": None,
                "connectors": items,
            }
        )

    return {"folders": merged}


@router.post("/admin/folders")
async def admin_create_folder(payload: dict):
    """新建文件夹: body = {name}"""
    name = (payload or {}).get("name", "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name 不能为空")
    r = await _huntly_request("POST", "/api/setting/folder/save", json={"name": name})
    if r.status_code != 200:
        raise _huntly_error(r, "folder/save")
    return _unwrap(r.json())


@router.put("/admin/folders/{folder_id}")
async def admin_rename_folder(folder_id: int, payload: dict):
    """重命名文件夹: body = {name}"""
    name = (payload or {}).get("name", "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name 不能为空")
    r = await _huntly_request(
        "POST", "/api/setting/folder/save", json={"id": folder_id, "name": name}
    )
    if r.status_code != 200:
        raise _huntly_error(r, "folder/save")
    return _unwrap(r.json())


@router.delete("/admin/folders/{folder_id}")
async def admin_delete_folder(folder_id: int):
    """删除文件夹（其下 connector 会自动回到 未分组）"""
    r = await _huntly_request(
        "POST", "/api/setting/folder/delete", params={"folderId": folder_id}
    )
    if r.status_code != 200:
        raise _huntly_error(r, "folder/delete")
    return {"ok": True, "folder_id": folder_id}


@router.get("/admin/preview")
async def admin_preview_feed(subscribe_url: str = Query(..., alias="subscribe_url")):
    """添加前预览订阅源元信息 (title / siteLink / subscribed)"""
    r = await _huntly_request(
        "GET", "/api/setting/feeds/preview", params={"subscribeUrl": subscribe_url}
    )
    if r.status_code != 200:
        raise _huntly_error(r, "feeds/preview")
    return _unwrap(r.json())


@router.post("/admin/sources")
async def admin_create_source(payload: dict):
    """新增订阅源: body = {subscribe_url, folder_id?, name?}

    Huntly /feeds/follow 仅接受 subscribeUrl，folder/name 需追加一次 updateSetting。
    注意: Huntly 的 updateSetting 是全量覆盖更新（缺失字段会被写为 NULL），
    因此必须显式回传 subscribeUrl/enabled，否则订阅地址与启用状态会丢失。
    """
    subscribe_url = (payload or {}).get("subscribe_url", "").strip()
    if not subscribe_url:
        raise HTTPException(status_code=400, detail="subscribe_url 不能为空")

    # Huntly v0.6.6 的 follow 响应有时不返回 connector id。先记录已有
    # connector，随后从同一 SQLite 快照中找出新建项，以便完整写回设置。
    # 否则 updateSetting 不会执行，subscribe_url/is_enabled 会是 NULL，源就
    # 无法出现在 folder-connectors 列表中。
    existing_connector_ids: set[int] = set()
    if _huntly_sqlite_available():
        try:
            with _huntly_sqlite() as conn:
                existing_connector_ids = {
                    int(row["id"])
                    for row in conn.execute("SELECT id FROM connector")
                }
        except Exception as exc:
            logger.warning("RSS follow 前读取 connector 快照失败: %s", exc)

    r = await _huntly_request(
        "POST", "/api/setting/feeds/follow", params={"subscribeUrl": subscribe_url}
    )
    if r.status_code != 200:
        raise _huntly_error(r, "feeds/follow")
    follow_data = _unwrap(r.json()) or {}
    connector_id = follow_data.get("id") or follow_data.get("connectorId")

    if not connector_id and _huntly_sqlite_available():
        try:
            with _huntly_sqlite() as conn:
                # 已存在的源优先按 URL 找到；新建源则取本次请求后新增的 id。
                row = conn.execute(
                    "SELECT id FROM connector WHERE subscribe_url = ? ORDER BY id DESC LIMIT 1",
                    (subscribe_url,),
                ).fetchone()
                if row is None:
                    candidates = [
                        int(item["id"])
                        for item in conn.execute("SELECT id FROM connector")
                        if int(item["id"]) not in existing_connector_ids
                    ]
                    connector_id = max(candidates) if candidates else None
                else:
                    connector_id = int(row["id"])
        except Exception as exc:
            logger.warning("RSS follow 后解析 connector id 失败: %s", exc)

    folder_id = (payload or {}).get("folder_id")
    custom_name = (payload or {}).get("name")
    if connector_id:
        update_body: dict[str, Any] = {
            "connectorId": connector_id,
            "subscribeUrl": subscribe_url,
            "enabled": True,
        }
        if folder_id is not None:
            update_body["folderId"] = folder_id or None  # 0 视为未分组 → null
        if custom_name:
            update_body["name"] = custom_name
        u = await _huntly_request(
            "POST", "/api/setting/feeds/updateSetting", json=update_body
        )
        if u.status_code != 200:
            raise _huntly_error(u, "feeds/updateSetting")
    else:
        logger.warning("RSS follow 未返回且未解析到 connector id: %s", follow_data)

    return {"ok": True, "connector_id": connector_id, "follow": follow_data}


@router.put("/admin/sources/{connector_id}")
async def admin_update_source(connector_id: int, payload: dict):
    """编辑订阅源: body = {name?, folder_id?, fetch_interval_minutes?, enabled?, crawl_full_content?}

    Huntly updateSetting 是全量覆盖更新，先读当前设置合并成完整 body 再提交，
    保证部分字段更新不会把 subscribeUrl/enabled 等未提及字段抹成 NULL。
    """
    cur = await _huntly_request(
        "GET", "/api/setting/feeds/setting", params={"connectorId": connector_id}
    )
    current: dict[str, Any] = _unwrap(cur.json()) if cur.status_code == 200 else {}
    if not isinstance(current, dict):
        current = {}

    body: dict[str, Any] = {
        "connectorId": connector_id,
        "name": current.get("name"),
        "folderId": current.get("folderId"),
        "subscribeUrl": current.get("subscribeUrl"),
        "crawlFullContent": bool(current.get("crawlFullContent")),
        "enabled": bool(current.get("enabled")),
        "fetchIntervalMinutes": current.get("fetchIntervalMinutes")
        or current.get("defaultFetchIntervalMinutes"),
    }
    if "name" in payload:
        body["name"] = payload["name"]
    if "folder_id" in payload:
        fid = payload["folder_id"]
        body["folderId"] = None if fid in (0, None, "") else fid
    if "fetch_interval_minutes" in payload:
        body["fetchIntervalMinutes"] = payload["fetch_interval_minutes"]
    if "enabled" in payload:
        body["enabled"] = bool(payload["enabled"])
    if "crawl_full_content" in payload:
        body["crawlFullContent"] = bool(payload["crawl_full_content"])

    r = await _huntly_request("POST", "/api/setting/feeds/updateSetting", json=body)
    if r.status_code != 200:
        raise _huntly_error(r, "feeds/updateSetting")
    return {"ok": True, "connector_id": connector_id}


@router.delete("/admin/sources/{connector_id}")
async def admin_delete_source(connector_id: int):
    """删除订阅源"""
    r = await _huntly_request(
        "POST", "/api/setting/feeds/delete", params={"connectorId": connector_id}
    )
    if r.status_code != 200:
        raise _huntly_error(r, "feeds/delete")
    return {"ok": True, "connector_id": connector_id}


@router.get("/admin/sources/{connector_id}/setting")
async def admin_get_source_setting(connector_id: int):
    """查询单个订阅源的详细设置"""
    r = await _huntly_request(
        "GET", "/api/setting/feeds/setting", params={"connectorId": connector_id}
    )
    if r.status_code != 200:
        raise _huntly_error(r, "feeds/setting")
    return _unwrap(r.json())


@router.get("/articles")
async def list_articles(
    source_id: int | None = Query(None, description="按 connector(source) 过滤 (向后兼容, 优先使用 source_ids)"),
    source_ids: str | None = Query(None, description="按多个 connector ID 过滤, 逗号分隔 e.g. 1,3,5"),
    folder_id: int | None = Query(None, description="按 folder 过滤"),
    keyword: str | None = Query(None, description="标题关键词"),
    only_financial_event: bool = Query(False, description="仅返回财务事件"),
    tickers: str | None = Query(None, description="股票 ticker, 逗号分隔 e.g. 600519.SH,000858.SZ"),
    industries: str | None = Query(None, description="行业, 逗号分隔"),
    sentiment: str | None = Query(None, description="情感: bullish/bearish/neutral"),
    event_tags: str | None = Query(None, description="事件标签, 逗号分隔"),
    countries: str | None = Query(None, description="国家, 逗号分隔 e.g. 美国,中国"),
    regions: str | None = Query(None, description="地区, 逗号分隔 e.g. 欧盟,东南亚"),
    key_terms: str | None = Query(None, description="关键词, 逗号分隔 e.g. AI,半导体"),
    date_entities: str | None = Query(None, description="文章中提及的日期, 逗号分隔 e.g. 2026-05-25,2026-Q2"),
    provinces: str | None = Query(None, description="中国省份, 逗号分隔 e.g. 广东,江苏"),
    cities: str | None = Query(None, description="中国城市, 逗号分隔 e.g. 深圳,合肥"),
    politicians: str | None = Query(None, description="政治人物, 逗号分隔 e.g. 李强,潘功胜"),
    visits: str | None = Query(None, description="调研类动词, 逗号分隔 e.g. 调研,视察"),
    departments: str | None = Query(None, description="国家部门, 逗号分隔 e.g. 央行,证监会"),
    starred: bool | None = Query(None, description="仅返回收藏"),
    strong_only: bool = Query(False, description="仅返回强信号 |score|>=0.5"),
    sort: str = Query("time_desc", description="排序: time_desc (最新), time_asc (最早), sentiment_bullish (利好强度), sentiment_bearish (利空强度)"),
    since: str | None = Query(None, description="起始时间 ISO 8601, e.g. 2026-05-20T00:00:00Z"),
    until: str | None = Query(None, description="截止时间 ISO 8601"),
    page: int = Query(1, ge=1),
    page_size: int = Query(30, ge=1, le=500),
):
    """资讯文章列表 — 直接从 Huntly SQLite 读, 全库分页 (84k+ 历史可见).

    过滤策略:
      - 无 enrichment 标签过滤: SQL 真分页 (offset/limit + COUNT). 用户可以翻到任意一页.
      - 有 enrichment 过滤: 先在 PG enrichment 表里倒排查出 huntly_page_id 列表 (上限 5000),
        再用 SQLite IN(...) 求交集 + SQL 真分页. 仍然走 SQL, 不再受 REST 500 上限.
      - keyword 搜索: 同时走 enrichment 标签匹配 (key_terms 等) + SQLite 标题/描述匹配,
        两路 UNION 确保不遗漏. 无其他标签过滤时也走 enrichment 以提升搜索精度.
    """
    def _split(s: str | None) -> list[str]:
        return [x.strip() for x in (s or "").split(",") if x.strip()]

    want_tickers = set(_split(tickers))
    want_industries = set(_split(industries))
    want_event_tags = set(_split(event_tags))
    want_countries = set(_split(countries))
    want_regions = set(_split(regions))
    want_key_terms = set(_split(key_terms))
    want_date_entities = set(_split(date_entities))
    want_provinces = set(_split(provinces))
    want_cities = set(_split(cities))
    want_politicians = set(_split(politicians))
    want_visits = set(_split(visits))
    want_departments = set(_split(departments))
    want_sentiment = (sentiment or "").strip().lower() or None
    if want_sentiment and want_sentiment not in ("bullish", "bearish", "neutral"):
        want_sentiment = None

    # 多源过滤 (逗号分隔 source_ids)
    source_id_list: list[int] | None = None
    if source_ids and source_ids.strip():
        source_id_list = [int(x) for x in source_ids.split(",") if x.strip().isdigit()]
    elif source_id is not None:
        source_id_list = [source_id]

    # 排序
    sort_clean = sort.strip().lower()
    if sort_clean not in ("time_desc", "time_asc", "sentiment_bullish", "sentiment_bearish"):
        sort_clean = "time_desc"

    # 情感排序需要 enrichment 数据, 拉取时多拉一些再在内存中排序
    sort_limit = page_size * 3 if sort_clean.startswith("sentiment_") else page_size

    has_enrichment_filter = bool(
        want_tickers or want_industries or want_event_tags or want_sentiment
        or strong_only or want_countries or want_regions or want_key_terms
        or want_date_entities
        or want_provinces or want_cities or want_politicians or want_visits
        or want_departments
    )

    # keyword 同时走 enrichment 标签匹配 (key_terms 等) + SQLite 标题/描述匹配,
    # 两路 UNION 确保不遗漏. 无其他标签过滤时也走 enrichment 以提升搜索精度.
    keyword_only_enrichment = bool(keyword and keyword.strip() and not has_enrichment_filter)

    only_ids: list[int] | None = None
    matched_total: int | None = None
    if has_enrichment_filter or keyword_only_enrichment:
        # 当用户同时提供日期/source/folder 过滤时, 先用 SQLite 把候选 IDs 裁出来,
        # 再让 PG 在该子集内做标签匹配 — 避免 ORDER BY enriched_at LIMIT 5000 把
        # 早期时间窗的命中文章筛掉.
        restrict_ids: list[int] | None = None
        if (since or until or source_id is not None
                or (folder_id is not None and folder_id > 0)
                or starred is True) and _huntly_sqlite_available():
            restrict_ids = await asyncio.to_thread(
                _sqlite_page_ids_in_range,
                source_id=source_id_list[0] if source_id_list and len(source_id_list) == 1 else None,
                folder_id=folder_id,
                starred=starred,
                since_iso=since,
                until_iso=until,
                limit=50000,
            )
            if not restrict_ids:
                return {
                    "articles": [],
                    "page": page,
                    "page_size": page_size,
                    "total": 0,
                    "matched_total": 0,
                    "latest_published_at": None,
                    "server_time": datetime.utcnow().isoformat() + "Z",
                }

        # PG 倒排: 拿到全库命中的 huntly_page_id (5000 上限)
        only_ids = await asyncio.to_thread(
            _query_enrichment_page_ids,
            want_tickers, want_industries, want_event_tags, want_sentiment, strong_only,
            want_countries=want_countries,
            want_regions=want_regions,
            want_key_terms=want_key_terms,
            want_date_entities=want_date_entities,
            want_provinces=want_provinces,
            want_cities=want_cities,
            want_politicians=want_politicians,
            want_visits=want_visits,
            want_departments=want_departments,
            keyword=keyword,
            restrict_to_ids=restrict_ids,
            limit=5000,
        )
        matched_total = len(only_ids)

        # keyword-only: enrichment 可能为空 (标签里没命中), 回退到 SQLite 全文搜索
        if keyword_only_enrichment and not only_ids:
            only_ids = None  # 回退: 让 SQLite 用 title/description LIKE 搜索
            matched_total = None

        # 有 keyword + 其他标签过滤: enrichment 非空时, 同时取 SQLite 关键词匹配 ID
        # 做 UNION, 避免漏掉标题/描述含关键词但标签不含的文章
        if has_enrichment_filter and keyword and keyword.strip() and only_ids and _huntly_sqlite_available():
            sqlite_kw_ids = await asyncio.to_thread(
                _sqlite_keyword_ids,
                keyword=keyword.strip(),
                source_id=source_id,
                folder_id=folder_id,
                starred=starred,
                since_iso=since,
                until_iso=until,
                limit=5000,
            )
            if sqlite_kw_ids:
                merged = set(only_ids) | set(sqlite_kw_ids)
                only_ids = sorted(merged, reverse=True)
                matched_total = len(only_ids)

        if has_enrichment_filter and not only_ids:
            return {
                "articles": [],
                "page": page,
                "page_size": page_size,
                "total": 0,
                "matched_total": 0,
                "latest_published_at": None,
                "server_time": datetime.utcnow().isoformat() + "Z",
            }

    if not _huntly_sqlite_available():
        # 回退到旧的 REST 代理逻辑 (开发环境无 sqlite mount)
        return await _list_articles_via_rest(
            source_id=source_id, folder_id=folder_id, keyword=keyword,
            only_financial_event=only_financial_event, page=page, page_size=page_size,
            since=since, until=until,
        )

    # 情感排序需要多拉数据再排, time 排序直接分页
    fetch_limit = sort_limit if sort_clean.startswith("sentiment_") else page_size
    offset = (page - 1) * page_size
    articles, total, latest_at = await asyncio.to_thread(
        _list_articles_from_sqlite,
        source_id=source_id_list[0] if source_id_list and len(source_id_list) == 1 else None,
        folder_id=folder_id,
        source_ids=source_id_list if source_id_list and len(source_id_list) > 1 else None,
        keyword=keyword,
        only_financial_event=only_financial_event,
        starred=starred,
        since_iso=since,
        until_iso=until,
        only_ids=only_ids,
        offset=0 if sort_clean.startswith("sentiment_") else offset,
        limit=fetch_limit,
        sort=sort_clean,
    )

    # 合并 enrichment (同步 psycopg2 调用放到线程池避免阻塞事件循环)
    page_ids = [int(a["id"]) for a in articles if a.get("id")]
    enrich_map = await asyncio.to_thread(_load_enrichments, page_ids)
    for a in articles:
        a["enrichment"] = enrich_map.get(int(a["id"]), _empty_enrichment()) if a.get("id") else _empty_enrichment()

    # 情感排序: enrichment 合并后按 sentiment_score 排序
    if sort_clean == "sentiment_bullish":
        articles.sort(
            key=lambda a: (a.get("enrichment", {}).get("sentiment_score") or 0),
            reverse=True,
        )
    elif sort_clean == "sentiment_bearish":
        articles.sort(
            key=lambda a: (a.get("enrichment", {}).get("sentiment_score") or 0),
            reverse=False,
        )
    # 分页: 情感排序时在此切片
    if sort_clean.startswith("sentiment_"):
        articles = articles[offset:offset + page_size]

    return {
        "articles": articles,
        "page": page,
        "page_size": page_size,
        "total": total,
        "matched_total": matched_total if matched_total is not None else total,
        "latest_published_at": latest_at,
        "server_time": datetime.utcnow().isoformat() + "Z",
    }


async def _list_articles_via_rest(
    *,
    source_id: int | None,
    folder_id: int | None,
    keyword: str | None,
    only_financial_event: bool,
    page: int,
    page_size: int,
    since: str | None,
    until: str | None,
) -> dict:
    """开发回退: 没有 sqlite mount 时仍走旧的 Huntly REST 路径 (受 500 上限)."""
    params: dict = {
        "count": min(page_size * page, 500),
        "sort": "CONNECTED_AT",
        "isAsc": "false",
    }
    if source_id is not None:
        params["connectorId"] = source_id
    if folder_id is not None and folder_id > 0:
        params["folderId"] = folder_id
    if keyword:
        params["q"] = keyword
    r = await _huntly_request("GET", "/api/page/list", params=params)
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Huntly /page/list HTTP {r.status_code}")
    body = _unwrap(r.json())
    raw = body if isinstance(body, list) else (body.get("items") or body.get("content") or body.get("data") or [])
    arts = [_normalize_page(p) for p in raw]
    if only_financial_event:
        arts = [a for a in arts if a["is_financial_event"]]
    start = (page - 1) * page_size
    page_slice = arts[start:start + page_size]
    page_ids = [int(a["id"]) for a in page_slice if a.get("id")]
    enrich_map = await asyncio.to_thread(_load_enrichments, page_ids)
    for a in page_slice:
        a["enrichment"] = enrich_map.get(int(a["id"]), _empty_enrichment()) if a.get("id") else _empty_enrichment()
    return {
        "articles": page_slice,
        "page": page,
        "page_size": page_size,
        "total": len(arts),
        "matched_total": len(arts),
        "latest_published_at": arts[0].get("published_at") if arts else None,
        "server_time": datetime.utcnow().isoformat() + "Z",
    }


@router.get("/articles/{article_id}")
async def get_article(article_id: int):
    """获取单篇正文 (代理 Huntly /api/page/{id})"""
    r = await _huntly_request("GET", f"/api/page/{article_id}")
    if r.status_code == 404:
        raise HTTPException(status_code=404, detail=f"Article {article_id} not found")
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Huntly /page/{article_id} HTTP {r.status_code}")
    page = _unwrap(r.json()) or {}
    # Huntly /api/page/{id} 实际返回 {"page": {...}, "contents": [...]}
    if isinstance(page, dict) and "page" in page and isinstance(page["page"], dict):
        contents = page.get("contents") or []
        page_obj = page["page"]
        detail = _normalize_page(page_obj)
        # 优先取 contents[0].content，回退到 page.content
        if contents and isinstance(contents[0], dict):
            detail["content"] = contents[0].get("content") or page_obj.get("content") or ""
        else:
            detail["content"] = page_obj.get("content") or ""
        detail["content_html"] = page_obj.get("contentHtml") or detail["content"]
    else:
        detail = _normalize_page(page)
        detail["content"] = page.get("content") or ""
        detail["content_html"] = page.get("contentHtml") or ""

    # 合并 enrichment (同步 psycopg2 调用放到线程池)
    if detail.get("id"):
        em = await asyncio.to_thread(_load_enrichments, [int(detail["id"])])
        detail["enrichment"] = em.get(int(detail["id"]), _empty_enrichment())
    else:
        detail["enrichment"] = _empty_enrichment()
    return detail


@router.post("/articles/{article_id}/star")
async def star_article(article_id: int, starred: bool = True):
    """收藏 / 取消收藏"""
    path = f"/api/page/{'star' if starred else 'unStar'}/{article_id}"
    r = await _huntly_request("POST", path)
    if r.status_code >= 300:
        raise HTTPException(status_code=502, detail=f"Huntly star HTTP {r.status_code}")
    return {"ok": True, "starred": starred}


@router.post("/articles/{article_id}/read")
async def mark_read(article_id: int, read: bool = True):
    """标记已读 / 未读"""
    path = f"/api/page/{'markRead' if read else 'unMarkRead'}/{article_id}"
    r = await _huntly_request("POST", path)
    if r.status_code >= 300:
        raise HTTPException(status_code=502, detail=f"Huntly markRead HTTP {r.status_code}")
    return {"ok": True, "read": read}


@router.get("/events")
async def list_financial_events(
    page: int = Query(1, ge=1),
    page_size: int = Query(30, ge=1, le=200),
):
    """财务事件流 (=资讯流中含财务关键词的子集，方便量化业务消费)"""
    return await list_articles(
        source_id=None,
        folder_id=None,
        keyword=None,
        only_financial_event=True,
        tickers=None,
        industries=None,
        sentiment=None,
        event_tags=None,
        countries=None,
        regions=None,
        key_terms=None,
        date_entities=None,
        provinces=None,
        cities=None,
        politicians=None,
        visits=None,
        departments=None,
        starred=None,
        strong_only=False,
        since=None,
        until=None,
        page=page,
        page_size=page_size,
    )


@router.get("/enrichment/stats")
async def enrichment_stats(
    tickers: str | None = Query(None),
    industries: str | None = Query(None),
    sentiment: str | None = Query(None),
    event_tags: str | None = Query(None),
    countries: str | None = Query(None),
    regions: str | None = Query(None),
    key_terms: str | None = Query(None),
    date_entities: str | None = Query(None),
    provinces: str | None = Query(None),
    cities: str | None = Query(None),
    politicians: str | None = Query(None),
    visits: str | None = Query(None),
    departments: str | None = Query(None),
    strong_only: bool = Query(False),
    keyword: str | None = Query(None),
    since: str | None = Query(None, description="起始时间 ISO 8601"),
    until: str | None = Query(None, description="截止时间 ISO 8601"),
):
    """聚合统计：返回 enrichment 表里出现频次最高的标签，用于前端 Filter Bar 下拉选项。

    当传入任意 filter 参数时, 统计结果会限制在当前过滤后的子集内
    (例如 sentiment_counts 会变成"在当前筛选结果中 利好/利空/中性 的分布")。
    """
    def _split(s: str | None) -> set[str]:
        return {x.strip() for x in (s or "").split(",") if x.strip()}

    want_tickers = _split(tickers)
    want_industries = _split(industries)
    want_event_tags = _split(event_tags)
    want_countries = _split(countries)
    want_regions = _split(regions)
    want_key_terms = _split(key_terms)
    want_date_entities = _split(date_entities)
    want_provinces = _split(provinces)
    want_cities = _split(cities)
    want_politicians = _split(politicians)
    want_visits = _split(visits)
    want_departments = _split(departments)
    want_sentiment = (sentiment or "").strip().lower() or None
    if want_sentiment not in ("bullish", "bearish", "neutral"):
        want_sentiment = None
    has_filter = bool(
        want_tickers or want_industries or want_event_tags or want_sentiment
        or strong_only or want_countries or want_regions or want_key_terms
        or want_date_entities or want_provinces or want_cities or want_politicians
        or want_visits or want_departments or (keyword and keyword.strip())
    )
    has_time_filter = bool(since or until)

    try:
        with _pg_conn() as conn, conn.cursor() as cur:
            # 若有筛选, 先取符合条件的 huntly_page_id 集合 -> 后续所有 unnest 统计加 WHERE
            subset_ids: list[int] | None = None
            if has_filter or has_time_filter:
                # 时间预过滤: 先从 SQLite 拿时间范围内的候选 IDs
                time_restrict_ids: list[int] | None = None
                if has_time_filter and _huntly_sqlite_available():
                    time_restrict_ids = await asyncio.to_thread(
                        _sqlite_page_ids_in_range,
                        source_id=None,
                        folder_id=None,
                        starred=None,
                        since_iso=since,
                        until_iso=until,
                        limit=50000,
                    )
                    if not time_restrict_ids:
                        return {
                            "top_industries": [], "top_events": [], "top_tickers": [],
                            "top_countries": [], "top_regions": [], "top_key_terms": [],
                            "top_dates": [], "top_provinces": [], "top_cities": [],
                            "top_politicians": [], "top_visits": [], "top_departments": [],
                            "sentiment_counts": {},
                        }

                if has_filter:
                    subset_ids = _query_enrichment_page_ids(
                        want_tickers, want_industries, want_event_tags, want_sentiment, strong_only,
                        want_countries=want_countries,
                        want_regions=want_regions,
                        want_key_terms=want_key_terms,
                        want_date_entities=want_date_entities,
                        want_provinces=want_provinces,
                        want_cities=want_cities,
                        want_politicians=want_politicians,
                        want_visits=want_visits,
                        want_departments=want_departments,
                        keyword=keyword,
                        restrict_to_ids=time_restrict_ids,
                        limit=10000,
                    )
                elif time_restrict_ids is not None:
                    # 仅有时间筛选, 无 enrichment 标签筛选
                    subset_ids = time_restrict_ids

                if has_filter and not subset_ids:
                    return {
                        "top_industries": [], "top_events": [], "top_tickers": [],
                        "top_countries": [], "top_regions": [], "top_key_terms": [],
                        "top_dates": [], "top_provinces": [], "top_cities": [],
                        "top_politicians": [], "top_visits": [], "top_departments": [],
                        "sentiment_counts": {},
                    }

            id_where = " AND huntly_page_id = ANY(%s)" if subset_ids is not None else ""
            id_param: tuple = (subset_ids,) if subset_ids is not None else ()

            def _topn(col: str, limit: int) -> list[dict]:
                cur.execute(
                    f"SELECT unnest({col}) AS v, COUNT(*) c "
                    f"FROM news_article_enrichment "
                    f"WHERE array_length({col}, 1) > 0{id_where} "
                    f"GROUP BY v ORDER BY c DESC LIMIT %s;",
                    id_param + (limit,),
                )
                return [{"name": r[0], "count": int(r[1])} for r in cur.fetchall()]

            top_industries = _topn("industries", 50)
            top_events = _topn("event_tags", 30)
            top_countries = _topn("countries", 30)
            top_regions = _topn("regions", 30)
            top_key_terms = _topn("key_terms", 50)
            top_dates = _topn("date_entities", 30)
            top_provinces = _topn("provinces", 30)
            top_cities = _topn("cities", 50)
            top_politicians = _topn("politicians", 30)
            top_visits = _topn("visits", 20)
            top_departments = _topn("departments", 30)

            cur.execute(
                f"SELECT unnest(tickers) AS v, COUNT(*) c FROM news_article_enrichment "
                f"WHERE array_length(tickers, 1) > 0{id_where} "
                f"GROUP BY v ORDER BY c DESC LIMIT %s;",
                id_param + (50,),
            )
            top_tickers_raw = cur.fetchall()

            cur.execute(
                f"SELECT sentiment_label, COUNT(*) FROM news_article_enrichment "
                f"WHERE sentiment_label IS NOT NULL{id_where} "
                f"GROUP BY sentiment_label;",
                id_param,
            )
            sentiment_counts = {r[0]: int(r[1]) for r in cur.fetchall()}

            tickers_only = [r[0] for r in top_tickers_raw]
            name_map: dict[str, str] = {}
            if tickers_only:
                cur.execute("SELECT symbol, name FROM stocks WHERE symbol = ANY(%s)", (tickers_only,))
                name_map = {r[0]: r[1] for r in cur.fetchall()}
            top_tickers = [
                {"ticker": t, "name": name_map.get(t, ""), "count": int(c)}
                for t, c in top_tickers_raw
            ]

        return {
            "top_industries": top_industries,
            "top_events": top_events,
            "top_tickers": top_tickers,
            "top_countries": top_countries,
            "top_regions": top_regions,
            "top_key_terms": top_key_terms,
            "top_dates": top_dates,
            "top_provinces": top_provinces,
            "top_cities": top_cities,
            "top_politicians": top_politicians,
            "top_visits": top_visits,
            "top_departments": top_departments,
            "sentiment_counts": sentiment_counts,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"stats failed: {e}")


@router.post("/enrichment/run")
async def enrichment_run_now(limit: int = Query(200, ge=1, le=5000)):
    """手动触发一次 enrich（同步执行，便于调试 / 首次回填）。"""
    try:
        from backend.services.api.news import run_enrichment_batch
        n = await asyncio.to_thread(run_enrichment_batch, limit)
        return {"ok": True, "written": n}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"enrich failed: {e}")


@router.post("/enrichment/rebuild-all")
async def enrichment_rebuild_all(force: bool = Query(False, description="true=覆盖已 enrich 的文章")):
    """一键全量重建标签 — 直接读 Huntly SQLite, 后台线程跑, 立即返回."""
    try:
        from backend.services.api.news import start_full_rebuild_async
        return start_full_rebuild_async(force=force)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"rebuild failed: {e}")


@router.get("/enrichment/finbert-status")
async def enrichment_finbert_status():
    """FinBERT 中文金融情感模型健康状态（供管理后台/前端诊断面板）。

    返回：
      - available: 模型是否已就绪（加载成功且未失败）
      - use_finbert: 进程级是否启用了 FinBERT（CPU 镜像默认关闭，GPU 镜像默认开启）
      - model: 当前配置的模型名/路径
      - device: 推理设备（-1=CPU, 0=GPU0）
      - last_inference_label/conf: 最近一次推理样本（若无则 None）
      - recent_db: 近 24h enrich 表里 +finbert 后缀占比（真实"是否生效"指标）
      - tips: 根据状态给出可执行的下一步建议
    """
    try:
        from backend.services.api.news import sentiment as sentiment_mod
    except Exception as e:
        return {"available": False, "use_finbert": False, "error": f"import failed: {e}"}

    available = bool(sentiment_mod.is_available())
    use_finbert = bool(sentiment_mod.USE_FINBERT)

    # 最近一次推理样本（可选）
    sample = None
    try:
        label, conf = sentiment_mod.score("公司发布重大利好公告，净利润大幅增长")
        if label is not None and conf is not None:
            sample = {"label": label, "confidence": round(float(conf), 4)}
    except Exception:
        pass

    # DB 真实生效占比（近 24h 写入是否带 +finbert 后缀）
    db_ratio = None
    db_total_24h = 0
    try:
        import os
        import psycopg2
        with psycopg2.connect(
            host=os.getenv("POSTGRES_HOST", "quantmind-db"),
            port=int(os.getenv("POSTGRES_PORT", "5432")),
            user=os.getenv("POSTGRES_USER", "quantmind"),
            password=os.getenv("POSTGRES_PASSWORD", "quantmind2026"),
            dbname=os.getenv("POSTGRES_DB", "quantmind"),
        ) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*), "
                    "  COUNT(*) FILTER (WHERE model_version LIKE '%%+finbert') "
                    "FROM news_article_enrichment "
                    "WHERE enriched_at >= NOW() - INTERVAL '24 hours'"
                )
                total, with_finbert = cur.fetchone()
                db_total_24h = int(total or 0)
                if total and int(total) > 0:
                    db_ratio = round(int(with_finbert) / int(total), 4)
    except Exception:
        pass

    # 状态语义
    if not use_finbert:
        tip = "FinBERT 在当前环境被关闭（CPU 镜像默认）。如需启用：设 NEWS_USE_FINBERT=true 并部署 GPU 镜像或安装 CPU 版 torch。"
    elif not available:
        tip = "FinBERT 已启用但加载失败，请执行 python3 backend/scripts/download_finbert.py 下载权重；详见 docs/FinBERT 中文金融情感模型.md。"
    else:
        tip = "FinBERT 已就绪，可在新闻资讯/标签管理中观察带 +finbert 后缀的 model_version。"

    return {
        "available": available,
        "use_finbert": use_finbert,
        "model": sentiment_mod.DEFAULT_MODEL,
        "device": sentiment_mod.DEVICE,
        "sample_inference": sample,
        "db_total_24h": db_total_24h,
        "db_finbert_ratio_24h": db_ratio,
        "tip": tip,
    }


@router.get("/enrichment/rebuild-progress")
async def enrichment_rebuild_progress():
    """查询全量重建进度."""
    try:
        from backend.services.api.news import get_rebuild_progress
        return get_rebuild_progress()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"progress failed: {e}")


# ---------------------------------------------------------------------------
# Admin: 标签管理 (finance_lexicon CRUD)
# ---------------------------------------------------------------------------


@router.get("/admin/tags")
async def admin_list_tags(
    event_tag: str | None = Query(None, description="按 event_tag 分类筛选"),
    keyword: str | None = Query(None, description="按 term 模糊搜索"),
    kind: str | None = Query(None, description="按 kind 筛选"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
):
    """列出 finance_lexicon 词条（支持分页、筛选）"""
    where: list[str] = []
    params: list = []
    if event_tag:
        where.append("event_tag = %s"); params.append(event_tag)
    if kind:
        where.append("kind = %s"); params.append(kind)
    if keyword:
        where.append("term ILIKE %s"); params.append(f"%{keyword}%")
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    offset = (page - 1) * page_size
    try:
        with _pg_conn() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM finance_lexicon{where_sql}", tuple(params))
            total = int(cur.fetchone()[0])
            cur.execute(
                f"SELECT id, term, kind, event_tag, weight, note, enabled, created_at "
                f"FROM finance_lexicon{where_sql} "
                f"ORDER BY event_tag, term LIMIT %s OFFSET %s",
                tuple(params) + (page_size, offset),
            )
            rows = cur.fetchall()
        items = [
            {
                "id": r[0], "term": r[1], "kind": r[2], "event_tag": r[3],
                "weight": float(r[4]) if r[4] else 1.0,
                "note": r[5], "enabled": r[6],
                "created_at": r[7].isoformat() if r[7] else None,
            }
            for r in rows
        ]
        return {"items": items, "total": total, "page": page, "page_size": page_size}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"list tags failed: {e}")


@router.post("/admin/tags")
async def admin_create_tag(payload: dict):
    """新增词条: {term, kind, event_tag?, weight?, note?}"""
    term = (payload or {}).get("term", "").strip()
    kind = (payload or {}).get("kind", "").strip()
    if not term or not kind:
        raise HTTPException(status_code=400, detail="term 和 kind 不能为空")
    event_tag = (payload or {}).get("event_tag") or None
    weight = float((payload or {}).get("weight") or 1.0)
    note = (payload or {}).get("note") or None
    try:
        with _pg_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO finance_lexicon (term, kind, event_tag, weight, note, enabled) "
                "VALUES (%s, %s, %s, %s, %s, TRUE) RETURNING id",
                (term, kind, event_tag, weight, note),
            )
            new_id = cur.fetchone()[0]
            conn.commit()
        return {"ok": True, "id": new_id}
    except psycopg2.IntegrityError:
        raise HTTPException(status_code=409, detail=f"词条 '{term}' (kind={kind}) 已存在")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"create tag failed: {e}")


@router.put("/admin/tags/{tag_id}")
async def admin_update_tag(tag_id: int, payload: dict):
    """编辑词条: {term?, kind?, event_tag?, weight?, note?}"""
    fields: list[str] = []
    params: list = []
    for key in ("term", "kind", "event_tag", "note"):
        if key in payload:
            fields.append(f"{key} = %s"); params.append(payload[key])
    if "weight" in payload:
        fields.append("weight = %s"); params.append(float(payload["weight"]))
    if not fields:
        raise HTTPException(status_code=400, detail="无更新字段")
    params.append(tag_id)
    try:
        with _pg_conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"UPDATE finance_lexicon SET {', '.join(fields)} WHERE id = %s",
                tuple(params),
            )
            conn.commit()
        return {"ok": True, "id": tag_id}
    except psycopg2.IntegrityError:
        raise HTTPException(status_code=409, detail="词条冲突（term+kind 重复）")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"update tag failed: {e}")


@router.delete("/admin/tags/{tag_id}")
async def admin_delete_tag(tag_id: int):
    """删除词条"""
    try:
        with _pg_conn() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM finance_lexicon WHERE id = %s", (tag_id,))
            conn.commit()
        return {"ok": True, "id": tag_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"delete tag failed: {e}")


@router.patch("/admin/tags/{tag_id}/toggle")
async def admin_toggle_tag(tag_id: int):
    """启用/禁用词条"""
    try:
        with _pg_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE finance_lexicon SET enabled = NOT enabled WHERE id = %s RETURNING enabled",
                (tag_id,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="词条不存在")
            conn.commit()
        return {"ok": True, "id": tag_id, "enabled": row[0]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"toggle tag failed: {e}")


# ---------------------------------------------------------------------------
# Huntly 后台 UI 代理 — /api/v1/news/huntly-ui/*
# ---------------------------------------------------------------------------
# Huntly SPA 的静态资源和 API 全是根路径（/static/...、/api/...），
# 经反向代理挂子路径时需要：1) HTML 资源路径重写  2) JS 拦截脚本把 /api 调用
# 重写到本代理路径  3) 代理目标把 /api 前缀剥掉转发给 Huntly 后端。
# 这样 8089/6008 都不需要暴露，公网/局域网/本地统一走 QuantMind 后端。
from fastapi import Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response

HUNTLY_UI_STATIC_MIME = {
    ".html": "text/html",
    ".js": "application/javascript",
    ".css": "text/css",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".webmanifest": "application/manifest+json",
    ".txt": "text/plain",
}

_HUNTLY_UI_REWRITE_SCRIPT = r"""<script>
(function(){
  var P='/api/v1/news/huntly-ui/api/';
  function rw(u){
    if(typeof u!=='string')return u;
    if(u.charAt(0)==='/'&&u.indexOf('/api/')===0)return P+u.slice(5);
    return u;
  }
  var of=window.fetch;
  window.fetch=function(u,o){return of(rw(u),o);};
  var ox=XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open=function(m,u){return ox.call(this,m,rw(u));};
  var _ws=window.WebSocket;
  window.WebSocket=function(u,p){if(typeof u==='string'&&u.indexOf('/api/')===0)u=P+u.slice(5);return new _ws(u,p);};
  window.WebSocket.prototype=_ws.prototype;
})();
</script>"""


def _huntly_ui_unreachable(accept: str = "") -> Response:
    if "text/html" in (accept or ""):
        return HTMLResponse(
            "<h2>Huntly 未启动</h2><p>请检查 HUNTLY_BASE_URL 配置（默认 http://quantmind-huntly）。</p>",
            status_code=502,
        )
    return PlainTextResponse("Huntly 未部署或未启动", status_code=502)


def _huntly_ui_mime(path: str) -> str:
    from pathlib import PurePosixPath

    return HUNTLY_UI_STATIC_MIME.get(PurePosixPath(path).suffix.lower(), "application/octet-stream")


async def _huntly_ui_proxy_static(path: str, accept: str) -> Response:
    """代理 Huntly SPA 静态资源，重写 HTML 里的根路径。"""
    url = f"{HUNTLY_BASE_URL}/{path.lstrip('/')}"
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            resp = await client.get(url, headers={"Accept": accept})
    except httpx.HTTPError:
        return _huntly_ui_unreachable(accept)

    content_type = resp.headers.get("content-type", "")
    body = resp.content

    if "text/html" in content_type and body:
        html = body.decode("utf-8", errors="replace")
        html = html.replace('src="/static/', 'src="/api/v1/news/huntly-ui/static/')
        html = html.replace('href="/static/', 'href="/api/v1/news/huntly-ui/static/')
        html = html.replace('href="/favicon.ico"', 'href="/api/v1/news/huntly-ui/favicon.ico"')
        html = html.replace('href="/apple-touch-icon.png"', 'href="/api/v1/news/huntly-ui/apple-touch-icon.png"')
        html = html.replace('href="/site.webmanifest"', 'href="/api/v1/news/huntly-ui/site.webmanifest"')
        html = html.replace("<head>", "<head>" + _HUNTLY_UI_REWRITE_SCRIPT, 1)
        return HTMLResponse(content=html, status_code=resp.status_code)

    resp_headers = {}
    if "content-type" not in resp.headers:
        resp_headers["content-type"] = _huntly_ui_mime(path)
    else:
        resp_headers["content-type"] = content_type

    if any(
        path.endswith(ext)
        for ext in (".js", ".mjs", ".css", ".woff2", ".woff", ".ttf", ".svg", ".png", ".webp", ".ico")
    ):
        resp_headers["cache-control"] = "public, max-age=3600"

    return Response(content=body, status_code=resp.status_code, headers=resp_headers)


async def _huntly_ui_proxy_api(request: Request) -> Response:
    """代理 /api/v1/news/huntly-ui/api/* → Huntly /api/*（带 JWT 会话）。"""
    sub = request.url.path
    prefix = "/api/v1/news/huntly-ui/api/"
    idx = sub.find(prefix)
    target = "/api/" + sub[idx + len(prefix):] if idx >= 0 else sub
    if request.url.query:
        target += "?" + request.url.query

    headers = {k: v for k, v in request.headers.items() if k.lower() not in ("host", "content-length", "connection")}
    # 后端全局 JWT 会话兜底（覆盖转发任何客户端 token，保证 UI 请求总有有效鉴权）
    try:
        token = await _ensure_session()
    except HTTPException:
        # 会话没建立也不阻塞 UI 加载——带原样转发，Huntly 自会 401
        token = ""
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["Cookie"] = f"auth_token={token}"

    body = await request.body() if request.method in ("POST", "PUT", "PATCH", "DELETE") else None
    async with httpx.AsyncClient(timeout=HUNTLY_TIMEOUT, follow_redirects=False) as c:
        try:
            r = await c.request(request.method, f"{HUNTLY_BASE_URL}{target}", headers=headers, content=body)
        except httpx.HTTPError:
            return _huntly_ui_unreachable(request.headers.get("accept", ""))

    resp_headers = {"content-type": r.headers.get("content-type", "application/json")}
    # 透传 Set-Cookie（Huntly 用 HttpOnly cookie 会话；子路径下浏览器
    # 存的是 QuantMind origin 的 cookie，转发回 Huntly 时靠 Cookie 头携带）
    if r.headers.get("set-cookie"):
        resp_headers["set-cookie"] = r.headers.get("set-cookie")
    return Response(content=r.content, status_code=r.status_code, headers=resp_headers)


# ⚠️ API 路由必须先于静态路由声明：FastAPI 按声明顺序匹配，
# /huntly-ui/api/{path:path} 若排在 /huntly-ui/{path:path}（静态）之后，
# GET 会被静态路由吞掉（转发时不带鉴权会话），Huntly 返回 405/401。
@router.api_route("/huntly-ui/api/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
async def huntly_ui_api(path: str, request: Request):
    return await _huntly_ui_proxy_api(request)


@router.get("/huntly-ui/{path:path}")
async def huntly_ui_static(path: str, request: Request):
    accept = request.headers.get("accept", "*/*")
    return await _huntly_ui_proxy_static(path, accept)


@router.get("/huntly-ui")
async def huntly_ui_index(request: Request):
    accept = request.headers.get("accept", "*/*")
    return await _huntly_ui_proxy_static("", accept)
