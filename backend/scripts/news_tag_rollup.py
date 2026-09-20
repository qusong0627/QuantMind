#!/usr/bin/env python3
"""新闻 20 天标签汇总（通道 B）——把「近 N 天某只票出了什么新闻」落成可查表。

链路::

    Huntly SQLite connected_at ──┐
                                 ├─► huntly_publish_index ──┐
    news_article_enrichment ─────┘                          ├─► news_stock_tags
                                      窗口过滤 + 展开聚合 ───┘

**两个口径钉死在这条链上**：

1. **发布时间的唯一事实源是 Huntly**。`enriched_at` 是回填式产出的，实测中位滞后
   913.8 小时（约 38 天）；用它当「最近 20 天」会把 5 个月前的旧闻当新闻。
   `connected_at` 是**上海墙钟字符串**，换算见 `parse_huntly_time`。
2. **A 股必须精确过滤**。窗口内 tickers 实测 A 股 125,347 / 港股 1 / 美股 63,904，
   不过滤会把 `FDX`（联邦快递）挂到 A 股候选列表上。

发布时间索引是**增量**的：只补 `news_article_enrichment` 里出现而索引里没有的 id
（首次约 66 万个，之后每天约 1.3 万）。这一步要读 Huntly 的 `page` 表（库 2.3 GB、
带正文大字段），故用临时表 join 而不是 `IN (...)`，并只取 id + connected_at 两列。

用法::

    python backend/scripts/news_tag_rollup.py                 # 日常增量
    python backend/scripts/news_tag_rollup.py --days 20       # 指定窗口
    python backend/scripts/news_tag_rollup.py --dry-run       # 只算不落库
    python backend/scripts/news_tag_rollup.py --reindex       # 忽略索引重建（口径变更后）
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any
from collections.abc import Iterable, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.shared.news_tag_contract import (  # noqa: E402
    ensure_news_tag_tables_async,
    load_enrichment_page_ids,
    load_enrichment_rows,
    load_missing_publish_ids,
    load_stock_tags_meta,
    parse_huntly_time,
    replace_stock_tags,
    upsert_publish_index,
    window_start,
)
from backend.shared.news_tagging import (  # noqa: E402
    ALL_DIRECTIONS,
    EXCLUDING_DIRECTIONS,
    aggregate_articles,
)
from backend.services.api.stock_terminal_us.feed.huntly import db_path  # noqa: E402

logger = logging.getLogger("news_tag_rollup")

#: Huntly 单次查询的 id 批大小（走临时表 join，不受 SQL 参数个数限制；
#: 但要控制单条 SQL 的内存与 WAL 扫描量）
_HUNTLY_BATCH = 50_000
#: PG 写入批大小（单事务 66 万行会长时间持锁）
_PG_BATCH = 20_000
#: `immutable=1` 在 Huntly 并发写时可能读到撕裂页（实测遇到过一次），重试退避
_HUNTLY_RETRIES = 3

#: 单实例锁。`replace_stock_tags` 的清理是「本轮没写进去的行删掉」，
#: 两个实例交错跑会互相删对方的行（表里只剩最后收尾的那个实例的数据）。
#: TTL 取 900s：比调度间隔短，进程被 kill 后不会把后续几轮全挡掉；
#: 又远大于实测单轮耗时（首次自举 66 万条约 10s）。
_LOCK_KEY = "qm:news:tag:rollup:lock"
_LOCK_TTL = 900


def fetch_publish_times(
    ids: Sequence[int], *, db: str | None = None
) -> dict[int, datetime]:
    """批量取发布时间（aware UTC）。

    不复用 `huntly_meta()`：那个会 LEFT JOIN connector 并取 title/url 大字段，
    本处只要两列、且 doi 量级是 66 万行。连接方式与容错同源
    （`file:{path}?immutable=1`；Huntly 的 Java 进程持写锁，`mode=ro` 会被阻塞）。
    """
    wanted = [int(i) for i in ids]
    if not wanted:
        return {}
    path = db or db_path()
    last_exc: Exception | None = None
    for attempt in range(1, _HUNTLY_RETRIES + 1):
        try:
            conn = sqlite3.connect(f"file:{path}?immutable=1", uri=True, timeout=10)
            try:
                conn.execute("CREATE TEMP TABLE _wanted(id INTEGER PRIMARY KEY)")
                conn.executemany(
                    "INSERT INTO _wanted VALUES(?)", [(i,) for i in wanted]
                )
                rows = conn.execute(
                    "SELECT p.id, p.connected_at FROM page p "
                    "JOIN _wanted w ON w.id = p.id"
                ).fetchall()
            finally:
                conn.close()
            out: dict[int, datetime] = {}
            dropped = 0
            for pid, raw in rows:
                parsed = parse_huntly_time(raw)
                if parsed is None:
                    dropped += 1
                    continue
                out[int(pid)] = parsed
            if dropped:
                logger.warning(
                    "[Rollup] %d 行发布时间不可用（哨兵/格式异常），已丢弃", dropped
                )
            return out
        except sqlite3.DatabaseError as exc:  # 撕裂页 / 被写锁挡住
            last_exc = exc
            logger.warning(
                "[Rollup] Huntly 读取失败（第 %d/%d 次）: %s",
                attempt,
                _HUNTLY_RETRIES,
                exc,
            )
            if attempt < _HUNTLY_RETRIES:
                import time as _time

                _time.sleep(2 * attempt)
    logger.error("[Rollup] Huntly 读取连续失败，本次放弃索引同步: %s", last_exc)
    return {}


async def sync_publish_index(*, reindex: bool = False) -> int:
    """增量补齐发布时间索引。返回写入行数。"""
    if reindex:
        missing = sorted(await load_enrichment_page_ids())
        logger.info("[Rollup] 发布时间索引：全量重建 %d 条", len(missing))
    else:
        missing = await load_missing_publish_ids()
        logger.info("[Rollup] 发布时间索引：待补 %d 条（增量）", len(missing))
    written = 0
    for start in range(0, len(missing), _HUNTLY_BATCH):
        chunk = missing[start : start + _HUNTLY_BATCH]
        fetched = fetch_publish_times(chunk)
        rows = sorted(fetched.items())
        for i in range(0, len(rows), _PG_BATCH):
            written += await upsert_publish_index(rows[i : i + _PG_BATCH])
    logger.info("[Rollup] 发布时间索引：写入 %d 条", written)
    return written


def acquire_run_lock(token: str) -> bool:
    """抢单实例锁。**Redis 不可达时放行**（宿主上跑 CLI 没有 Redis，不该因此罢工），
    但会留下告警——静默降级和静默失败一样要看得见。"""
    from backend.shared.redis_sentinel_client import get_redis_sentinel_client

    try:
        client = get_redis_sentinel_client()
        return bool(client.set(_LOCK_KEY, token.encode("utf-8"), ex=_LOCK_TTL, nx=True))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Rollup] Redis 不可用，本轮无锁运行（并发风险自负）: %s", exc)
        return True


def release_run_lock(token: str) -> None:
    """释放锁（仅当仍归属本 token）。CAS 由 TTL 兜底——最坏情况是白等一轮。"""
    from backend.shared.redis_sentinel_client import get_redis_sentinel_client

    try:
        client = get_redis_sentinel_client()
        if client.get(_LOCK_KEY, use_slave=False) == token.encode("utf-8"):
            client.delete(_LOCK_KEY)
    except Exception as exc:  # noqa: BLE001 - 释放失败由 TTL 兜底
        logger.warning("[Rollup] 锁释放失败（TTL 会自动回收）: %s", exc)


def _summarize(hits: Iterable[Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    for hit in hits:
        out[hit.direction] = out.get(hit.direction, 0) + 1
    return out


async def run(
    days: int, *, dry_run: bool = False, reindex: bool = False, quiet: bool = False
) -> int:
    """带单实例锁的入口（锁只护写侧，故 dry-run 也走同一把锁）。

    ``quiet`` 关掉人眼报表（celery 每半小时一跑，报表会灌满 worker 日志）；
    摘要仍走 logger.info。
    """
    import uuid

    token = uuid.uuid4().hex
    if not acquire_run_lock(token):
        logger.warning("[Rollup] 已有实例在跑，本轮跳过（下一轮再来）")
        return 0
    try:
        return await _run(days, dry_run=dry_run, reindex=reindex, quiet=quiet)
    finally:
        release_run_lock(token)


async def _run(
    days: int, *, dry_run: bool = False, reindex: bool = False, quiet: bool = False
) -> int:
    if not await ensure_news_tag_tables_async():
        logger.error("[Rollup] 建表失败，退出")
        return 1

    await sync_publish_index(reindex=reindex)

    since = window_start(days)
    rows = await load_enrichment_rows(since=since)
    # `published_at` 从 PG 回来是 aware datetime；aggregate 只认 datetime 实例
    articles = [
        {
            "published_at": r["published_at"],
            "tickers": list(r["tickers"] or []),
            "event_tags": list(r["event_tags"] or []),
            "title": r["title"] or "",
        }
        for r in rows
    ]
    hits = aggregate_articles(articles)
    by_dir = _summarize(hits)
    symbols = {h.symbol for h in hits}

    def _say(line: str = "") -> None:
        if not quiet:
            print(line)

    _say(f"\n窗口：{since.date()} 起（近 {days} 天，按**发布时间**）")
    coverage = (
        f"参与聚合文章：{len(articles)} 篇 → 标签行 {len(hits)} 条 / "
        f"命中标的 {len(symbols)} 只"
    )
    _say(coverage)
    logger.info("[Rollup] %s", coverage)
    # 方向列表取自 ALL_DIRECTIONS（单点定义），且标出哪些方向参与默认排除
    for direction in ALL_DIRECTIONS:
        mark = "排除" if direction in EXCLUDING_DIRECTIONS else "标注"
        _say(f"  {direction:<11} {by_dir.get(direction, 0):>6} 条  [{mark}]")
        logger.info("[Rollup] 方向 %s=%d [%s]", direction, by_dir.get(direction, 0), mark)

    if dry_run:
        _say("\n[dry-run] 未写 news_stock_tags。")
        return 0

    written = await replace_stock_tags(hits, window_end=date.today())
    meta = await load_stock_tags_meta()
    _say(
        f"\n[已落库] {written} 行 → news_stock_tags；表内窗口止于 {meta.get('window_end')}"
        f"（标的 {meta.get('symbols')} 只 / 标签 {meta.get('tags')} 条）"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="新闻 20 天标签汇总（通道 B）")
    parser.add_argument("--days", type=int, default=20, help="窗口天数（用户指定 20）")
    parser.add_argument("--dry-run", action="store_true", help="只算不落库")
    parser.add_argument(
        "--reindex", action="store_true", help="忽略已有索引、全量重建发布时间索引"
    )
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--quiet", action="store_true", help="不打印人眼报表（只留日志）")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    return asyncio.run(
        run(
            args.days,
            dry_run=args.dry_run,
            reindex=args.reindex,
            quiet=args.quiet,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
