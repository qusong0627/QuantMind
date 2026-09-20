"""新闻标签契约（通道 B）：两张表的自愈 DDL + 读写。

- ``huntly_publish_index``：``huntly_page_id → published_at``。**这是发布时间的
  唯一事实源**——`news_article_enrichment.enriched_at` 是回填式产出的，实测中位
  滞后 913.8 小时（约 38 天），拿它当「最近 20 天」会把 5 个月前的旧闻当新闻。
  之所以要单独建索引表而不是每次现查 SQLite：Huntly 的 `page` 表带正文大字段
  （库 2.3 GB），全表扫一次冷启动 47s，且并发写时 `immutable=1` 会抛
  `database disk image is malformed`；落成 PG 表后按 id 增量补齐，日常只走 PG join。
- ``news_stock_tags``：按 ``(symbol, tag)`` 汇总的近 N 天标签（见 `news_tagging`）。

迁移纪律（同 eval/signal/order 契约）：to_regclass 预检 → 零 DDL 快路径；仅缺表才
CREATE（lock_timeout=3s）；异常不阻断业务。
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any
from collections.abc import Iterable, Sequence

from backend.shared.news_tagging import ALL_DIRECTIONS

logger = logging.getLogger(__name__)

PUBLISH_INDEX_TABLE = "huntly_publish_index"
STOCK_TAGS_TABLE = "news_stock_tags"

#: Huntly `connected_at` 是**上海墙钟**（实测锁定：当作 UTC 时 20.06% 的文章会
#: 变成「发布前 8 小时就被 enrich」，物理上不可能）。见记忆
#: `huntly-connected-at-shanghai-wallclock`。
HUNTLY_TZ = timezone(timedelta(hours=8))

#: 早于这个年份的 connected_at 视为哨兵值（实测 min = `0001-12-30 08:00:00.000`，
#: 1 行；`%Y` 能解析出公元 1 年，但 astimezone 跨公元前后的偏移不可信，直接丢弃）。
_MIN_VALID_YEAR = 1990

_CREATE_PUBLISH_INDEX_SQL = f"""
CREATE TABLE IF NOT EXISTS {PUBLISH_INDEX_TABLE} (
    huntly_page_id BIGINT PRIMARY KEY,
    published_at TIMESTAMPTZ NOT NULL,
    indexed_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

_CREATE_PUBLISH_INDEX_IX_SQL = (
    f"CREATE INDEX IF NOT EXISTS idx_huntly_publish_at "
    f"ON {PUBLISH_INDEX_TABLE} (published_at)"
)

_CREATE_STOCK_TAGS_SQL = f"""
CREATE TABLE IF NOT EXISTS {STOCK_TAGS_TABLE} (
    symbol VARCHAR(16) NOT NULL,
    tag VARCHAR(32) NOT NULL,
    direction VARCHAR(12) NOT NULL,
    n INTEGER NOT NULL,
    first_published TIMESTAMPTZ NOT NULL,
    last_published TIMESTAMPTZ NOT NULL,
    sample_titles JSONB NOT NULL DEFAULT '[]'::jsonb,
    window_end DATE NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, tag)
)
"""

_CREATE_STOCK_TAGS_IX_SQL = (
    f"CREATE INDEX IF NOT EXISTS idx_news_stock_tags_dir "
    f"ON {STOCK_TAGS_TABLE} (direction, last_published DESC)"
)


def parse_huntly_time(raw: object) -> datetime | None:
    """Huntly ``connected_at``（上海墙钟字符串）→ aware UTC。

    格式恒为 ``YYYY-MM-DD HH:MM:SS.mmm``（实测 681,450 行长度全 23、无 NULL）。
    哨兵值（年份 < 1990）与不可解析值返回 ``None`` —— 调用方必须丢弃该行，
    **不得**退回 enrich 时间兜底（那正是本契约要禁掉的口径）。
    """
    if not raw:
        return None
    text = str(raw).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            naive = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if naive.year < _MIN_VALID_YEAR:
            return None
        return naive.replace(tzinfo=HUNTLY_TZ).astimezone(timezone.utc)
    return None


async def ensure_news_tag_tables_async() -> bool:
    """自愈创建两张表（存在即零 DDL 快路径；失败仅告警不抛出）。"""
    from sqlalchemy import text as _text

    from backend.shared.database_manager_v2 import get_session

    try:
        async with get_session(read_only=True) as session:
            exists = (
                await session.execute(
                    _text(
                        "SELECT to_regclass('public.huntly_publish_index'), "
                        "to_regclass('public.news_stock_tags')"
                    )
                )
            ).first()
        if exists and exists[0] is not None and exists[1] is not None:
            return True
        async with get_session() as session:
            await session.execute(_text("SET LOCAL lock_timeout = '3s'"))
            for sql in (
                _CREATE_PUBLISH_INDEX_SQL,
                _CREATE_PUBLISH_INDEX_IX_SQL,
                _CREATE_STOCK_TAGS_SQL,
                _CREATE_STOCK_TAGS_IX_SQL,
            ):
                await session.execute(_text(sql))
            await session.commit()
        logger.info("[NewsTagContract] 新闻标签表已就绪")
        return True
    except Exception as exc:  # noqa: BLE001 - 不阻断业务
        logger.warning("[NewsTagContract] 自愈失败（不阻断）: %s", exc)
        return False


async def upsert_publish_index(rows: Sequence[tuple[int, datetime]]) -> int:
    """增量写发布时间索引（``published_at`` 变化时更新，否则不动）。返回写入行数。"""
    if not rows:
        return 0
    from sqlalchemy import text as _text

    from backend.shared.database_manager_v2 import get_session

    if not await ensure_news_tag_tables_async():
        return 0
    try:
        async with get_session() as session:
            await session.execute(
                _text(
                    f"INSERT INTO {PUBLISH_INDEX_TABLE} (huntly_page_id, published_at) "
                    "VALUES (:pid, :pub) "
                    "ON CONFLICT (huntly_page_id) DO UPDATE "
                    f"SET published_at = EXCLUDED.published_at, indexed_at = now() "
                    f"WHERE {PUBLISH_INDEX_TABLE}.published_at IS DISTINCT FROM "
                    "EXCLUDED.published_at"
                ),
                [{"pid": int(p), "pub": t} for p, t in rows],
            )
            await session.commit()
        return len(rows)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[NewsTagContract] 索引写入失败: %s", exc)
        return 0


async def replace_stock_tags(hits: Iterable[Any], *, window_end: date) -> int:
    """幂等 tick：upsert 本轮命中 + 清理未在本轮刷新的行（窗口外的自愈）。

    ``window_end`` 只作**新鲜度标记**（「这张表算到哪天」），不参与清理判据——
    清理用 ``updated_at < now()``（同一事务内 `now()` 恒定，故它会精确删掉
    「不是本轮写进来的」行）。用 ``window_end < 本轮`` 当天粒度判断的话，
    同一天重跑且窗口滑动时，上一轮多出来的行会滞留到第二天。
    """
    from sqlalchemy import text as _text

    from backend.shared.database_manager_v2 import get_session

    if not await ensure_news_tag_tables_async():
        return 0
    payload = [
        {
            "sym": h.symbol,
            "tag": h.tag,
            "dir": h.direction,
            "n": int(h.n),
            "first": h.first_published,
            "last": h.last_published,
            "samples": _json_dumps(list(h.sample_titles)),
            "we": window_end,
        }
        for h in hits
    ]
    try:
        async with get_session() as session:
            if payload:
                await session.execute(
                    _text(
                        f"INSERT INTO {STOCK_TAGS_TABLE} (symbol, tag, direction, n, "
                        "first_published, last_published, sample_titles, window_end) "
                        "VALUES (:sym, :tag, :dir, :n, :first, :last, "
                        "CAST(:samples AS jsonb), :we) "
                        "ON CONFLICT (symbol, tag) DO UPDATE SET "
                        "direction=EXCLUDED.direction, n=EXCLUDED.n, "
                        "first_published=EXCLUDED.first_published, "
                        "last_published=EXCLUDED.last_published, "
                        "sample_titles=EXCLUDED.sample_titles, "
                        "window_end=EXCLUDED.window_end, updated_at=now()"
                    ),
                    payload,
                )
            # 清理：同一事务内 now() 恒定，故「updated_at < now()」精确等于
            # 「不是本轮写进来的行」（本轮全部被 ON CONFLICT 刷成 now()）
            pruned = await session.execute(
                _text(f"DELETE FROM {STOCK_TAGS_TABLE} WHERE updated_at < now()")
            )
            await session.commit()
        logger.info(
            "[NewsTagContract] 标签落库 %d 行（清理窗口外 %s 行，窗口止于 %s）",
            len(payload),
            getattr(pruned, "rowcount", "?"),
            window_end,
        )
        return len(payload)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[NewsTagContract] 标签落库失败: %s", exc)
        return 0


def _json_dumps(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)


_SELECT_TAGS_SQL = (
    "SELECT symbol, tag, direction, n, first_published, "
    "last_published, sample_titles, window_end FROM " + STOCK_TAGS_TABLE
)


async def load_stock_tags(
    symbols: Sequence[str],
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """按标的取标签（按方向分桶）。**空桶保留**，前端按固定桶渲染。

    窗口由表内 ``window_end`` 自证（rollup 每轮清理窗口外的行），故这里不再按
    时间过滤——加一道会掩盖「rollup 没跑」这个真问题。
    """
    wanted = [str(s) for s in dict.fromkeys(symbols or []) if s]
    if not wanted:
        return {}
    return await _read_tags(
        f"{_SELECT_TAGS_SQL} WHERE symbol = ANY(:syms)", {"syms": wanted}
    )


async def load_all_stock_tags() -> dict[str, dict[str, list[dict[str, Any]]]]:
    """全表标签（约千行）按标的分桶。

    列表侧一次取全量而不是「筛选查一次集合、标注再查一次本页」：表本身只有
    20 天 × 约 757 只的量级，一次拉全比两次往返更省，而且两个用途天然同源——
    不会出现「界面标了利空却没被排除」这种自相矛盾。
    """
    return await _read_tags(_SELECT_TAGS_SQL, {})


async def _read_tags(
    sql: str, params: dict[str, Any]
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    from sqlalchemy import text as _text

    from backend.shared.database_manager_v2 import get_session

    try:
        async with get_session(read_only=True) as session:
            rows = (await session.execute(_text(sql), params)).mappings().all()
    except Exception as exc:  # noqa: BLE001 - 读不到就是「无标注」，不阻断列表
        logger.warning("[NewsTagContract] 标签读取失败（按无标注处理）: %s", exc)
        return {}

    out: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for row in rows:
        # 空桶取自 ALL_DIRECTIONS（单点定义）：前端按固定桶渲染，
        # 这里少一个键就会在 UI 上显示成 undefined。
        buckets = out.setdefault(row["symbol"], {d: [] for d in ALL_DIRECTIONS})
        buckets.setdefault(row["direction"], []).append(
            {
                "tag": row["tag"],
                "n": row["n"],
                "first": _iso(row["first_published"]),
                "last": _iso(row["last_published"]),
                "samples": row["sample_titles"] or [],
                "window_end": _iso(row["window_end"]),
            }
        )
    return out


async def load_stock_tags_meta() -> dict[str, Any]:
    """表级元信息：最新窗口日、标签数、标的数（运维要看「rollup 跑没跑」）。"""
    from sqlalchemy import text as _text

    from backend.shared.database_manager_v2 import get_session

    try:
        async with get_session(read_only=True) as session:
            row = (
                (
                    await session.execute(
                        _text(
                            f"SELECT max(window_end) AS window_end, count(*) AS tags, "
                            f"count(DISTINCT symbol) AS symbols FROM {STOCK_TAGS_TABLE}"
                        )
                    )
                )
                .mappings()
                .first()
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[NewsTagContract] 元信息读取失败: %s", exc)
        return {"available": False}
    if not row or not row["tags"]:
        return {"available": False, "reason": "标签表为空（rollup 尚未运行）"}
    window_end = row["window_end"]
    stale_days = None
    if isinstance(window_end, date):
        stale_days = (date.today() - window_end).days
    return {
        "available": True,
        "window_end": _iso(window_end),
        "stale_days": stale_days,
        "tags": int(row["tags"]),
        "symbols": int(row["symbols"]),
    }


def _iso(value: Any) -> str | None:
    """瞬时列输出带 Z 的 aware UTC（与平台口径一致）。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        aware = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return (
            aware.astimezone(timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )
    return str(value)


async def load_enrichment_rows(
    *, since: datetime | None = None
) -> list[dict[str, Any]]:
    """取待聚合的文章行（join 发布时间索引）。

    ``since`` 给了就按**发布时间**过滤——那是唯一正确的窗口口径。
    不给则返回全部（首次自举/回填用）。
    """
    from sqlalchemy import text as _text

    from backend.shared.database_manager_v2 import get_session

    sql = (
        "SELECT e.huntly_page_id, e.tickers, e.event_tags, e.title, p.published_at "
        "FROM news_article_enrichment e "
        f"JOIN {PUBLISH_INDEX_TABLE} p ON p.huntly_page_id = e.huntly_page_id "
    )
    params: dict[str, Any] = {}
    if since is not None:
        sql += "WHERE p.published_at >= :since "
        params["since"] = since
    sql += "ORDER BY p.published_at DESC"
    async with get_session(read_only=True) as session:
        rows = (await session.execute(_text(sql), params)).mappings().all()
    return [dict(r) for r in rows]


async def load_missing_publish_ids() -> list[int]:
    """还没进索引的 page id（**增量**补齐用，按 SQL 反连接算）。

    不写成「取全表 id − 取全索引 id」（那要往 Python 搬两遍 66 万个整数，
    按半小时一跑就是每天 96 遍）：反连接只回传真正缺的那几条，
    日常个位数，首次自举才回全量。
    """
    from sqlalchemy import text as _text

    from backend.shared.database_manager_v2 import get_session

    async with get_session(read_only=True) as session:
        rows = (
            (
                await session.execute(
                    _text(
                        "SELECT e.huntly_page_id FROM news_article_enrichment e "
                        f"LEFT JOIN {PUBLISH_INDEX_TABLE} p "
                        "ON p.huntly_page_id = e.huntly_page_id "
                        "WHERE p.huntly_page_id IS NULL ORDER BY e.huntly_page_id"
                    )
                )
            )
            .scalars()
            .all()
        )
    return [int(r) for r in rows]


async def load_enrichment_page_ids() -> set[int]:
    """``news_article_enrichment`` 的全部 page id（``--reindex`` 全量重建用）。"""
    from sqlalchemy import text as _text

    from backend.shared.database_manager_v2 import get_session

    async with get_session(read_only=True) as session:
        rows = (
            (
                await session.execute(
                    _text("SELECT huntly_page_id FROM news_article_enrichment")
                )
            )
            .scalars()
            .all()
        )
    return {int(r) for r in rows}


def window_start(days: int, *, now: datetime | None = None) -> datetime:
    """窗口起点（aware UTC）：``now - days``。"""
    ref = now or datetime.now(tz=timezone.utc)
    return ref - timedelta(days=days)
