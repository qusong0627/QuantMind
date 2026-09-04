"""把 Huntly 文章批量 enrich 到 news_article_enrichment。

调用入口：
- enrich_article(huntly_page_id, title, content) -> EnrichmentResult
- run_enrichment_batch(limit=200) -> int 处理多少篇

实现细节：
- 从 Huntly REST 拿 page/list（已经在 routers/news.py 里有代理，这里直接 httpx 同步）
- DB 写入用 psycopg2 同步连接（Celery worker 内同步即可）
- 每篇文章: matcher.match() + sentiment.score() -> upsert
- huntly_page_id 已存在则跳过，除非 model_version 不一致
"""
from __future__ import annotations

import dataclasses
import hashlib
import logging
import os
import sqlite3
import threading
import time
from typing import Iterable

import httpx
import psycopg2

from .matcher import get_matcher, MODEL_VERSION
from . import sentiment as sentiment_mod

logger = logging.getLogger("news.enricher")

HUNTLY_BASE_URL = os.getenv("HUNTLY_BASE_URL", "http://quantmind-huntly").rstrip("/")
HUNTLY_USERNAME = os.getenv("HUNTLY_USERNAME", "")
HUNTLY_PASSWORD = os.getenv("HUNTLY_PASSWORD", "")
HUNTLY_TIMEOUT = float(os.getenv("HUNTLY_TIMEOUT", "10"))
HUNTLY_SQLITE_PATH = os.getenv("HUNTLY_SQLITE_PATH", "/data/huntly/db.sqlite")


def _db_conn():
    return psycopg2.connect(
        host=os.getenv("POSTGRES_HOST", "quantmind-db"),
        port=int(os.getenv("POSTGRES_PORT", "5432")),
        user=os.getenv("POSTGRES_USER", "quantmind"),
        password=os.getenv("POSTGRES_PASSWORD", "quantmind2026"),
        dbname=os.getenv("POSTGRES_DB", "quantmind"),
    )


@dataclasses.dataclass
class EnrichmentResult:
    huntly_page_id: int
    tickers: list[str]
    industries: list[str]
    event_tags: list[str]
    sentiment_score: float | None
    sentiment_label: str | None
    sentiment_confidence: float | None
    model_version: str
    countries: list[str] = dataclasses.field(default_factory=list)
    regions: list[str] = dataclasses.field(default_factory=list)
    key_terms: list[str] = dataclasses.field(default_factory=list)
    date_entities: list[str] = dataclasses.field(default_factory=list)
    entity_sentiments: dict = dataclasses.field(default_factory=dict)
    provinces: list[str] = dataclasses.field(default_factory=list)
    cities: list[str] = dataclasses.field(default_factory=list)
    politicians: list[str] = dataclasses.field(default_factory=list)
    visits: list[str] = dataclasses.field(default_factory=list)
    departments: list[str] = dataclasses.field(default_factory=list)
    error: str | None = None


def _title_hash(title: str | None) -> int:
    if not title:
        return 0
    h = hashlib.md5(title.encode("utf-8", errors="ignore")).digest()
    return int.from_bytes(h[:8], "big", signed=True)


def _label_from_score(s: float) -> str:
    if s >= 0.25:
        return "bullish"
    if s <= -0.25:
        return "bearish"
    return "neutral"


def enrich_article(
    huntly_page_id: int,
    title: str | None,
    content: str | None,
    finbert_precomputed: tuple[str | None, float | None] | None = None,
) -> EnrichmentResult:
    matcher = get_matcher()
    blob = ((title or "") + "\n" + (content or "")).strip()
    ticker_hits, industry_hits, event_hits, dict_score, stats = matcher.match(blob)

    # 排序：按命中频次降序，截断
    tickers = sorted(ticker_hits, key=lambda k: -ticker_hits[k])[:20]
    industries = sorted(industry_hits, key=lambda k: -industry_hits[k])[:10]
    event_tags = sorted(event_hits, key=lambda k: -event_hits[k])[:10]

    countries_d = stats.get("countries") or {}
    regions_d = stats.get("regions") or {}
    key_terms_d = stats.get("key_terms") or {}
    provinces_d = stats.get("provinces") or {}
    cities_d = stats.get("cities") or {}
    politicians_d = stats.get("politicians") or {}
    visits_d = stats.get("visits") or {}
    departments_d = stats.get("departments") or {}
    countries = sorted(countries_d, key=lambda k: -countries_d[k])[:10]
    regions = sorted(regions_d, key=lambda k: -regions_d[k])[:10]
    key_terms = sorted(key_terms_d, key=lambda k: -key_terms_d[k])[:15]
    provinces = sorted(provinces_d, key=lambda k: -provinces_d[k])[:8]
    cities = sorted(cities_d, key=lambda k: -cities_d[k])[:10]
    politicians = sorted(politicians_d, key=lambda k: -politicians_d[k])[:8]
    visits = sorted(visits_d, key=lambda k: -visits_d[k])[:8]
    departments = sorted(departments_d, key=lambda k: -departments_d[k])[:10]
    date_entities = matcher.extract_dates(blob, limit=8)
    entity_sentiments = matcher.match_entity_sentiments(blob)

    # FinBERT 在标题上跑一次（标题信号最干净；全量重建时由外部批量预计算传入）
    finbert_label, finbert_conf = (
        finbert_precomputed
        if finbert_precomputed is not None
        else sentiment_mod.score(title or "")
    )
    if finbert_label and finbert_conf and finbert_conf >= 0.55:
        # 将 finbert label 折算成 [-1,1] 然后与字典法加权融合
        finbert_score = {"bullish": 1.0, "bearish": -1.0, "neutral": 0.0}.get(finbert_label, 0.0)
        # 0.6 字典 + 0.4 FinBERT
        final_score = 0.6 * dict_score + 0.4 * finbert_score * finbert_conf
        # label 由融合后的分数推导，而非直接取 FinBERT label——
        # 否则 FinBERT 高置信度判 neutral 时，字典法强信号(如"被重锤"→-0.567)会被压成中性
        final_label = _label_from_score(final_score)
        final_conf = max(finbert_conf, min(abs(dict_score) + 0.3, 1.0))
    else:
        final_score = dict_score
        final_label = _label_from_score(dict_score)
        final_conf = min(abs(dict_score) + 0.3, 1.0)

    return EnrichmentResult(
        huntly_page_id=huntly_page_id,
        tickers=tickers,
        industries=industries,
        event_tags=event_tags,
        sentiment_score=round(float(final_score), 4),
        sentiment_label=final_label,
        sentiment_confidence=round(float(final_conf), 4),
        model_version=MODEL_VERSION + ("+finbert" if sentiment_mod.is_available() else ""),
        countries=countries,
        regions=regions,
        key_terms=key_terms,
        date_entities=date_entities,
        entity_sentiments=entity_sentiments,
        provinces=provinces,
        cities=cities,
        politicians=politicians,
        visits=visits,
        departments=departments,
    )


def _upsert_enrichment(conn, r: EnrichmentResult, title_hash: int, title: str | None = None):
    import json as _json
    sql = """
        INSERT INTO news_article_enrichment (
            huntly_page_id, tickers, industries, event_tags,
            sentiment_score, sentiment_label, sentiment_confidence,
            enriched_at, model_version, title_hash, error,
            countries, regions, key_terms, date_entities, entity_sentiments,
            provinces, cities, politicians, visits, departments,
            title
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, NOW(), %s, %s, %s,
                %s, %s, %s, %s, %s::jsonb,
                %s, %s, %s, %s, %s, %s)
        ON CONFLICT (huntly_page_id) DO UPDATE
        SET tickers = EXCLUDED.tickers,
            industries = EXCLUDED.industries,
            event_tags = EXCLUDED.event_tags,
            sentiment_score = EXCLUDED.sentiment_score,
            sentiment_label = EXCLUDED.sentiment_label,
            sentiment_confidence = EXCLUDED.sentiment_confidence,
            enriched_at = NOW(),
            model_version = EXCLUDED.model_version,
            title_hash = EXCLUDED.title_hash,
            error = EXCLUDED.error,
            countries = EXCLUDED.countries,
            regions = EXCLUDED.regions,
            key_terms = EXCLUDED.key_terms,
            date_entities = EXCLUDED.date_entities,
            entity_sentiments = EXCLUDED.entity_sentiments,
            provinces = EXCLUDED.provinces,
            cities = EXCLUDED.cities,
            politicians = EXCLUDED.politicians,
            visits = EXCLUDED.visits,
            departments = EXCLUDED.departments,
            title = EXCLUDED.title;
    """
    with conn.cursor() as cur:
        cur.execute(sql, (
            r.huntly_page_id,
            r.tickers,
            r.industries,
            r.event_tags,
            r.sentiment_score,
            r.sentiment_label,
            r.sentiment_confidence,
            r.model_version,
            title_hash,
            r.error,
            r.countries,
            r.regions,
            r.key_terms,
            r.date_entities,
            _json.dumps(r.entity_sentiments or {}, ensure_ascii=False),
            r.provinces,
            r.cities,
            r.politicians,
            r.visits,
            r.departments,
            title,
        ))


# ---------- Huntly 拉取 ----------

_session_token: str | None = None


def _unwrap(body):
    if isinstance(body, dict) and "data" in body and "code" in body:
        return body["data"]
    return body


def _huntly_login(client: httpx.Client) -> str:
    """Huntly v0.6.x JWT 登录。返回 JWT token。"""
    global _session_token
    if _session_token:
        return _session_token
    r = client.post("/api/auth/signin", json={
        "username": HUNTLY_USERNAME,
        "password": HUNTLY_PASSWORD,
    })
    if r.status_code >= 300:
        raise RuntimeError(f"Huntly signin HTTP {r.status_code}: {r.text[:120]}")
    token = None
    try:
        token = _unwrap(r.json())
    except Exception:
        pass
    if not token:
        token = r.cookies.get("auth_token")
    if not token:
        set_cookie = r.headers.get("set-cookie", "")
        if "auth_token=" in set_cookie:
            token = set_cookie.split("auth_token=", 1)[1].split(";", 1)[0]
    if not token or not isinstance(token, str):
        raise RuntimeError("Huntly signin succeeded but no JWT returned")
    _session_token = token
    return token


def _huntly_request(client: httpx.Client, method: str, path: str, **kw) -> httpx.Response:
    global _session_token
    token = _huntly_login(client)
    headers = dict(kw.pop("headers", {}))
    headers["Authorization"] = f"Bearer {token}"
    headers["Cookie"] = f"auth_token={token}"
    r = client.request(method, path, headers=headers, **kw)
    if r.status_code in (401, 403):
        _session_token = None
        token = _huntly_login(client)
        headers["Authorization"] = f"Bearer {token}"
        headers["Cookie"] = f"auth_token={token}"
        r = client.request(method, path, headers=headers, **kw)
    return r


def fetch_recent_pages(limit: int = 200) -> list[dict]:
    """从 Huntly /api/page/list 拉最近的 page（id, title, content/description）。"""
    with httpx.Client(base_url=HUNTLY_BASE_URL, timeout=HUNTLY_TIMEOUT) as client:
        r = _huntly_request(client, "GET", "/api/page/list", params={
            "count": limit,
            "sort": "CONNECTED_AT",
            "isAsc": "false",
        })
        if r.status_code != 200:
            raise RuntimeError(f"Huntly /page/list HTTP {r.status_code}: {r.text[:200]}")
        body = _unwrap(r.json())
        if isinstance(body, list):
            return list(body)
        if isinstance(body, dict):
            pages = body.get("items") or body.get("content") or body.get("data") or []
            return list(pages)
        return []


def fetch_page_content(client: httpx.Client, page_id: int) -> str:
    try:
        r = _huntly_request(client, "GET", f"/api/page/{page_id}")
        if r.status_code != 200:
            return ""
        body = _unwrap(r.json()) or {}
        if isinstance(body, dict) and "page" in body:
            contents = body.get("contents") or []
            if contents and isinstance(contents[0], dict):
                return contents[0].get("content") or body["page"].get("content") or ""
            return body["page"].get("content") or ""
        if isinstance(body, dict):
            return body.get("content") or ""
        return ""
    except Exception:
        return ""


def _pending_page_ids(conn, candidate_ids: Iterable[int]) -> set[int]:
    """从候选 ID 中找出尚未 enrich 或 model 版本过旧的。"""
    ids = list(set(int(x) for x in candidate_ids if x))
    if not ids:
        return set()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT huntly_page_id, model_version FROM news_article_enrichment "
            "WHERE huntly_page_id = ANY(%s)",
            (ids,),
        )
        done = {row[0]: row[1] for row in cur.fetchall()}
    pending = set()
    for pid in ids:
        if pid not in done:
            pending.add(pid)
        elif done[pid] != MODEL_VERSION and (done[pid] or "").startswith("ac-v1+lex-v1") is False:
            # 旧版本，重跑
            pending.add(pid)
    return pending


def run_enrichment_batch(limit: int = 200) -> int:
    """一次扫描，返回新写入/更新条数。"""
    t0 = time.time()
    try:
        pages = fetch_recent_pages(limit=limit)
    except Exception as e:
        logger.warning("拉取 Huntly 文章失败: %s", e)
        return 0
    if not pages:
        return 0

    # 1. 找出 pending
    with _db_conn() as conn:
        candidates = [int(p.get("id")) for p in pages if p.get("id")]
        pending = _pending_page_ids(conn, candidates)
        if not pending:
            logger.debug("Huntly enrich: 无新文章")
            return 0

        # 2. 对 pending 拉正文（只拉短列表）
        n_ok = 0
        with httpx.Client(base_url=HUNTLY_BASE_URL, timeout=HUNTLY_TIMEOUT) as client:
            for p in pages:
                pid = int(p.get("id") or 0)
                if pid not in pending:
                    continue
                title = p.get("title") or p.get("siteName") or ""
                # description/content 通常在 list 里就有
                short_text = p.get("description") or p.get("content") or ""
                if len(short_text) < 200:
                    long_text = fetch_page_content(client, pid)
                    if long_text and len(long_text) > len(short_text):
                        short_text = long_text
                # 每篇独立事务：任何单篇 SQL 出错都不应把连接带进 aborted 状态
                # 从而让后续整批陪葬（'current transaction is aborted'）。
                # 失败后必须 rollback 清空残留事务，否则同连接后续语句全部失败。
                try:
                    conn.rollback()
                    result = enrich_article(pid, title, short_text)
                    _upsert_enrichment(conn, result, _title_hash(title), title)
                    conn.commit()
                    n_ok += 1
                except Exception as e:
                    conn.rollback()
                    logger.warning("enrich page=%d 失败: %s", pid, e)
                    # 回滚后再尝试写 error 记录，失败同样回滚
                    try:
                        _upsert_enrichment(conn, EnrichmentResult(
                            huntly_page_id=pid,
                            tickers=[],
                            industries=[],
                            event_tags=[],
                            sentiment_score=None,
                            sentiment_label=None,
                            sentiment_confidence=None,
                            model_version=MODEL_VERSION,
                            error=str(e)[:500],
                        ), _title_hash(title), title)
                        conn.commit()
                    except Exception:
                        conn.rollback()
        # 最外层不再统一 commit（已逐篇提交）；仅清理可能残留的事务
        conn.rollback()

    logger.info(
        "news enrich batch 完成: pending=%d, ok=%d, cost=%.2fs",
        len(pending), n_ok, time.time() - t0,
    )
    return n_ok


# ---------- 一键全量重建 (直接读 Huntly SQLite, 不走 REST) ----------

_REBUILD_LOCK = threading.Lock()
_REBUILD_STATE: dict = {
    "running": False,
    "total": 0,
    "processed": 0,
    "ok": 0,
    "failed": 0,
    "started_at": None,
    "finished_at": None,
    "error": None,
    "force": False,
}


def get_rebuild_progress() -> dict:
    """供 API 查询的进度快照."""
    with _REBUILD_LOCK:
        s = dict(_REBUILD_STATE)
    elapsed = 0.0
    eta = None
    if s.get("started_at"):
        end = s.get("finished_at") or time.time()
        elapsed = max(0.0, end - s["started_at"])
        if s["processed"] > 0 and s["running"]:
            rate = s["processed"] / max(0.001, elapsed)
            remaining = max(0, s["total"] - s["processed"])
            eta = remaining / rate if rate > 0 else None
    s["elapsed_seconds"] = round(elapsed, 1)
    s["eta_seconds"] = round(eta, 1) if eta is not None else None
    return s


def _huntly_sqlite_ro(timeout: float = 30.0) -> sqlite3.Connection:
    uri = f"file:{HUNTLY_SQLITE_PATH}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=timeout, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _snapshot_huntly_db(dest_dir: str = "/tmp") -> str:
    """用 sqlite backup API 把 Huntly 库复制成快照再全量扫描。

    滚动日志模式下长游标会顶住 huntly 写进程、写进程又会反过来
    锁死我们的读取（曾致 rebuild 卡死/静默失败），先备份成私有
    快照彻底消除两边互踩。backup API 分页拷贝可感知写锁，不会
    长时间持锁。
    """
    dest = os.path.join(dest_dir, f"huntly_snapshot_{int(time.time())}.sqlite")
    src_conn = _huntly_sqlite_ro()
    try:
        dst = sqlite3.connect(dest)
        src_conn.backup(dst, pages=4096)
        dst.close()
    finally:
        src_conn.close()
    logger.info("Huntly 快照完成: %s (%.1f MB)",
                dest, os.path.getsize(dest) / 1024 / 1024)
    return dest


def _iter_all_huntly_pages(batch_size: int = 1000, db_path: str | None = None):
    """以游标方式分批从 Huntly SQLite 读 (id, title, description, content, page_article_content.content).

    使用 connected_at desc 顺序, 优先处理新文章.
    db_path 给定时读私有快照（rebuild 用），否则直连线上库。
    """
    if db_path:
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
    else:
        conn = _huntly_sqlite_ro()
    try:
        yield from _iter_pages_from_conn(conn, batch_size)
    finally:
        conn.close()


def _iter_pages_from_conn(conn: sqlite3.Connection, batch_size: int):
        cur = conn.cursor()
        cur.execute(
            "SELECT p.id, p.title, p.description, p.content, "
            "       (SELECT pac.content FROM page_article_content pac "
            "        WHERE pac.page_id = p.id ORDER BY pac.id DESC LIMIT 1) AS full_content "
            "FROM page p "
            "ORDER BY p.connected_at DESC"
        )
        while True:
            rows = cur.fetchmany(batch_size)
            if not rows:
                break
            for r in rows:
                yield {
                    "id": int(r["id"]),
                    "title": r["title"] or "",
                    "description": r["description"] or "",
                    "content": r["content"] or "",
                    "full_content": r["full_content"] or "",
                }


def _pick_text(row: dict) -> str:
    """优先 full_content -> description -> content -> title."""
    for k in ("full_content", "description", "content"):
        v = (row.get(k) or "").strip()
        if v and len(v) >= 30:
            return v
    return (row.get("description") or row.get("content") or "").strip()


def run_full_rebuild(force: bool = False) -> int:
    """一次全量重建 — 同步函数, 调用方应放到后台线程.

    force=True 时不跳过已有 enrichment, 全部重跑.
    force=False 时只跑 pending (未 enrich 或 model_version 旧).
    """
    global _REBUILD_STATE
    t0 = time.time()
    with _REBUILD_LOCK:
        if _REBUILD_STATE.get("running"):
            return -1
        _REBUILD_STATE.update({
            "running": True,
            "total": 0,
            "processed": 0,
            "ok": 0,
            "failed": 0,
            "started_at": t0,
            "finished_at": None,
            "error": None,
            "force": bool(force),
        })

    try:
        with _huntly_sqlite_ro() as sconn:
            scur = sconn.cursor()
            scur.execute("SELECT COUNT(*) FROM page")
            total = int(scur.fetchone()[0])
            scan_db = None
        with _REBUILD_LOCK:
            _REBUILD_STATE["total"] = total
        logger.info("full rebuild start: total=%d force=%s", total, force)

        # 一次性把所有 enriched id 拉进内存做 set 比较, 比每篇 SELECT 快几个量级
        existing_versions: dict[int, str] = {}
        if not force:
            with _db_conn() as pg:
                with pg.cursor() as cur:
                    cur.execute(
                        "SELECT huntly_page_id, model_version FROM news_article_enrichment"
                    )
                    for pid, mv in cur.fetchall():
                        existing_versions[int(pid)] = mv or ""

        n_ok = 0
        n_fail = 0
        n_done = 0
        pg = _db_conn()
        try:
            # FinBERT 批量推理窗口：标题攒一批一次前向，比逐篇快数倍
            _BATCH = int(os.getenv("FINBERT_BATCH", "96"))
            # 预同步加载模型，避免首批 chunk 在懒加载窗口内整批降级 None
            if sentiment_mod.USE_FINBERT:
                t_load = time.time()
                sentiment_mod._try_load()
                logger.info("rebuild: FinBERT 预加载 is_available=%s (%.1fs)",
                            sentiment_mod.is_available(), time.time() - t_load)

            def _flush_pg_error(cur_conn, pid: int, e: Exception, title: str | None):
                logger.warning("rebuild page=%d 失败: %s", pid, e)
                # 失败后必须 rollback，否则连接进入 aborted 状态，后续语句全报
                # 'current transaction is aborted'
                try:
                    cur_conn.rollback()
                except Exception:
                    pass
                try:
                    _upsert_enrichment(cur_conn, EnrichmentResult(
                        huntly_page_id=pid,
                        tickers=[],
                        industries=[],
                        event_tags=[],
                        sentiment_score=None,
                        sentiment_label=None,
                        sentiment_confidence=None,
                        model_version=MODEL_VERSION,
                        error=str(e)[:500],
                    ), _title_hash(title), title)
                except Exception:
                    try:
                        cur_conn.rollback()
                    except Exception:
                        pass

            def _flush(chunk_items):
                nonlocal n_ok, n_fail
                t_fb = time.time()
                finberts = sentiment_mod.score_batch([it[1] or "" for it in chunk_items])
                logger.info("rebuild flush: %d items, finbert %.2fs", len(chunk_items), time.time() - t_fb)
                for (pid, title, text), finbert in zip(chunk_items, finberts, strict=True):
                    try:
                        result = enrich_article(pid, title, text, finbert_precomputed=finbert)
                        _upsert_enrichment(pg, result, _title_hash(title), title)
                        n_ok += 1
                    except Exception as e:
                        n_fail += 1
                        _flush_pg_error(pg, pid, e, title)

            buf: list[tuple[int, str | None, str]] = []
            try:
                scan_db = _snapshot_huntly_db()
            except Exception as e:
                # 快照失败（如磁盘紧张）降级为直读线上库，行为等同旧版
                logger.warning("Huntly 快照失败，降级直读: %s", e)
            target_version = MODEL_VERSION + (
                "+finbert" if sentiment_mod.is_available() else ""
            )
            for row in _iter_all_huntly_pages(batch_size=2000, db_path=scan_db):
                pid = row["id"]
                n_done += 1

                # 目标版本（含 finbert 后缀）已算清的行一律跳过：
                # 不论 force 与否都断点续跑，重复执行退化为幂等空扫描
                mv = existing_versions.get(pid)
                if mv == target_version:
                    with _REBUILD_LOCK:
                        _REBUILD_STATE["processed"] = n_done
                    continue

                buf.append((pid, row["title"], _pick_text(row)))
                if n_done % 5000 == 0:
                    logger.info("rebuild heartbeat: scanned=%d buffered=%d ok=%d fail=%d",
                                n_done, len(buf), n_ok, n_fail)
                if len(buf) < _BATCH:
                    continue

                _flush(buf)
                buf = []
                # 每 批 commit 一次, 既减少事务开销又能让前端看到中间结果
                try:
                    pg.commit()
                except Exception:
                    pass
                with _REBUILD_LOCK:
                    _REBUILD_STATE.update({
                        "processed": n_done,
                        "ok": n_ok,
                        "failed": n_fail,
                    })
                logger.info("rebuild progress: %d/%d ok=%d fail=%d", n_done, total, n_ok, n_fail)
            if buf:
                _flush(buf)
            try:
                pg.commit()
            except Exception:
                pass
        finally:
            try:
                pg.close()
            except Exception:
                pass

        if scan_db:
            try:
                os.remove(scan_db)
            except Exception:
                pass

        with _REBUILD_LOCK:
            _REBUILD_STATE.update({
                "running": False,
                "processed": n_done,
                "ok": n_ok,
                "failed": n_fail,
                "finished_at": time.time(),
            })
        logger.info(
            "full rebuild 完成: total=%d processed=%d ok=%d failed=%d cost=%.1fs",
            total, n_done, n_ok, n_fail, time.time() - t0,
        )
        return n_ok
    except Exception as e:
        logger.exception("full rebuild 异常: %s", e)
        with _REBUILD_LOCK:
            _REBUILD_STATE.update({
                "running": False,
                "error": str(e)[:500],
                "finished_at": time.time(),
            })
        return -1


def start_full_rebuild_async(force: bool = False) -> dict:
    """启动后台全量重建. 已在跑则直接返回当前进度."""
    with _REBUILD_LOCK:
        if _REBUILD_STATE.get("running"):
            return {"started": False, "reason": "already_running", **get_rebuild_progress()}

    t = threading.Thread(
        target=run_full_rebuild,
        kwargs={"force": force},
        name="news-full-rebuild",
        daemon=True,
    )
    t.start()
    # 给线程一点时间初始化 state
    time.sleep(0.05)
    return {"started": True, **get_rebuild_progress()}
