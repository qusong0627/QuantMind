#!/usr/bin/env python3
"""滚动刷新 stock_daily_latest 最近 N 天（默认 30 天 ≈ 1 个月），数据源 QuantDB。

复用 `backend.scripts.quantdb_daily_sync.fill_pg_from_parquet`——与市场定时同步
**完全同口径**：前复权 OHLCV（qdb_daily_forward）+ adj_factor=1.0 + 估值/波动/收益
特征（qdb_features_daily）+ 基于前复权 close 重算的价格派生指标（ma*/ma_gap_*/
vol_atr_14），symbol 统一前缀式内码。

与既有增量脚本（只补 PG 最大日之后的新日期）不同，本脚本每天**重刷最近一个月**：
QuantDB 分区落盘时间不固定（可晚至次日中午），且迟到补数/修订需要回灌；upsert
幂等，可重复运行。

用法：
    # 滚动刷新最近 30 天（默认）
    python scripts/data/maintenance/rolling_sync_stock_daily_recent.py

    # 自定义窗口 / 预览
    python scripts/data/maintenance/rolling_sync_stock_daily_recent.py --days 22
    python scripts/data/maintenance/rolling_sync_stock_daily_recent.py --dry-run

    # 顺带清理窗口外历史行（真正滚动窗口；默认保留历史）
    python scripts/data/maintenance/rolling_sync_stock_daily_recent.py --prune
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

# 以 `python /app/scripts/...` 直跑时 cwd 不在 sys.path，先补项目根
PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import create_engine, text  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
LOGGER = logging.getLogger("rolling_sync_recent")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="滚动刷新 stock_daily_latest 最近 N 天（QuantDB 源）"
    )
    parser.add_argument("--days", type=int, default=30, help="回刷窗口（自然日，默认 30）")
    parser.add_argument("--batch-days", type=int, default=20, help="每批交易日数（默认 20）")
    parser.add_argument(
        "--prune",
        action="store_true",
        help="删除窗口外的历史行（真正滚动窗口；默认保留历史）",
    )
    parser.add_argument("--dry-run", action="store_true", help="只枚举窗口内交易日，不写库")
    return parser.parse_args()


def _db_url() -> str:
    import os
    from urllib.parse import quote

    url = (os.getenv("DATABASE_URL") or "").strip()
    if url:
        return url.replace("+asyncpg", "").replace("+psycopg2", "")
    host = os.getenv("DB_HOST", "db")
    port = os.getenv("DB_PORT", "5432")
    user = os.getenv("DB_USER", "quantmind")
    password = quote(os.getenv("DB_PASSWORD", ""), safe="")
    dbname = os.getenv("DB_NAME", "quantmind")
    return f"postgresql://{user}:{password}@{host}:{port}/{dbname}"


def _table_range(db_url: str) -> tuple[int, object, object]:
    engine = create_engine(db_url)
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT COUNT(*)::bigint, MIN(trade_date), MAX(trade_date) "
                    "FROM public.stock_daily_latest"
                )
            ).fetchone()
        return int(row[0] or 0), row[1], row[2]
    finally:
        engine.dispose()


def _prune(db_url: str, before: date) -> int:
    engine = create_engine(db_url)
    try:
        with engine.begin() as conn:
            return conn.execute(
                text("DELETE FROM public.stock_daily_latest WHERE trade_date < :d"),
                {"d": before},
            ).rowcount
    finally:
        engine.dispose()


def _invalidate_cache() -> None:
    try:
        from backend.shared.redis_sentinel_client import get_redis_sentinel_client

        get_redis_sentinel_client().delete("qm:admin:data_status")
        LOGGER.info("已清除数据状态缓存 qm:admin:data_status")
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("清除数据状态缓存失败: %s", exc)


def _enrich_names_industry(db_url: str, start_date: date) -> int:
    """回填 stock_name / industry（fill_pg_from_parquet 不写这两列）。

    来源 QuantDB instrument_list（Symbol→Name、rs_hyname），统一转前缀式内码。
    仅更新窗口内行，避免整表写放大。
    """
    import psycopg2
    from psycopg2.extras import execute_values

    from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub
    from backend.shared.stock_utils import StockCodeUtil

    hub = QuantDBDataHub.get_instance()
    stock_list = hub.fetch_stock_list()
    industry = hub.fetch_instrument_industry()

    name_map: dict[str, str] = {}
    if not stock_list.empty and "symbol" in stock_list.columns:
        name_col = next(
            (c for c in ("Name", "name", "stock_name") if c in stock_list.columns), None
        )
        if name_col:
            name_map = {
                StockCodeUtil.to_prefix(str(s)): str(n)
                for s, n in zip(stock_list["symbol"], stock_list[name_col], strict=False)
                if n
            }
    ind_map: dict[str, str] = {}
    if not industry.empty and {"symbol", "ind_name_l1"}.issubset(industry.columns):
        ind_map = {
            StockCodeUtil.to_prefix(str(s)): str(n)
            for s, n in zip(industry["symbol"], industry["ind_name_l1"], strict=False)
            if n
        }
    if not name_map and not ind_map:
        LOGGER.warning("QuantDB 无名称/行业映射，跳过富化")
        return 0

    symbols = sorted(set(name_map) | set(ind_map))
    rows = [(s, name_map.get(s), ind_map.get(s)) for s in symbols]
    conn = psycopg2.connect(db_url)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TEMP TABLE _sdl_name_map "
                "(symbol text PRIMARY KEY, stock_name text, industry text) ON COMMIT DROP"
            )
            execute_values(
                cur,
                "INSERT INTO _sdl_name_map (symbol, stock_name, industry) VALUES %s",
                rows,
                page_size=2000,
            )
            cur.execute(
                "UPDATE stock_daily_latest s SET "
                "stock_name = COALESCE(m.stock_name, s.stock_name), "
                "industry = COALESCE(m.industry, s.industry) "
                "FROM _sdl_name_map m "
                "WHERE s.symbol = m.symbol AND s.trade_date >= %s",
                (start_date,),
            )
            updated = cur.rowcount
        conn.commit()
        LOGGER.info("富化 stock_name/industry: 更新 %s 行（映射 %s 只）", updated, len(rows))
        return int(updated or 0)
    finally:
        conn.close()


def main() -> int:
    args = parse_args()
    days = max(1, int(args.days))
    end_date = date.today()
    start_date = end_date - timedelta(days=days)
    db_url = _db_url()

    from backend.scripts.quantdb_daily_sync import (
        _trade_dates,
        fill_pg_from_parquet,
    )
    from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub

    hub = QuantDBDataHub.get_instance()
    if not hub.available:
        LOGGER.error("QuantDB 数据不可用，终止")
        return 1

    trade_days = _trade_dates(hub, start_date, end_date)
    if not trade_days:
        LOGGER.warning("窗口 [%s, %s] 内无 QuantDB 交易日", start_date, end_date)
        return 0

    rows_before, min_before, max_before = _table_range(db_url)
    LOGGER.info(
        "同步前: rows=%s range=[%s, %s]；窗口=[%s, %s] 交易日=%d（%s..%s）",
        rows_before,
        min_before,
        max_before,
        start_date,
        end_date,
        len(trade_days),
        trade_days[0],
        trade_days[-1],
    )

    if args.dry_run:
        LOGGER.info("dry-run：将刷新 %d 个交易日，未写库", len(trade_days))
        return 0

    result = fill_pg_from_parquet(
        start_date=start_date, end_date=end_date, batch_days=args.batch_days
    )
    LOGGER.info("fill_pg_from_parquet 结果: %s", result)

    try:
        _enrich_names_industry(db_url, start_date)
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("名称/行业富化失败（不阻断）: %s", exc)

    if args.prune:
        deleted = _prune(db_url, start_date)
        LOGGER.info("prune：删除窗口外行 %s 条（< %s）", deleted, start_date)

    _invalidate_cache()

    rows_after, min_after, max_after = _table_range(db_url)
    LOGGER.info("同步后: rows=%s range=[%s, %s]", rows_after, min_after, max_after)

    return 0 if result.get("status") in ("ok", "skipped") else 1


if __name__ == "__main__":
    raise SystemExit(main())
