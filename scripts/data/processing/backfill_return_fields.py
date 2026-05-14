"""
回填/滚动更新 stock_daily_latest 的 return_1d 和 return_3d 字段。

计算逻辑:
  adj_close = close * adj_factor
  return_1d = adj_close[t+1] / adj_close[t] - 1  (次日收益率)
  return_3d = adj_close[t+3] / adj_close[t] - 1  (3日收益率)

用法:
  # 全量回填
  python scripts/data/processing/backfill_return_fields.py --full

  # 仅滚动更新最近 N 天 (默认5天, 用于每日定时任务)
  python scripts/data/processing/backfill_return_fields.py --recent-days 5

  # dry-run 模式
  python scripts/data/processing/backfill_return_fields.py --recent-days 5 --dry-run
"""

import argparse
import logging
import os
import sys
from pathlib import Path

import psycopg2

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="回填/滚动更新 return_1d, return_3d 字段")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--full", action="store_true", help="全量回填所有数据")
    g.add_argument("--recent-days", type=int, metavar="N", help="仅回填最近 N 天的数据")
    p.add_argument("--dry-run", action="store_true", help="仅预览不写入")
    p.add_argument("--database-url", default=None, help="PostgreSQL 连接 URL")
    return p.parse_args()


def get_conn(database_url: str = None):
    if database_url:
        url = database_url.replace("+asyncpg", "").replace("+psycopg2", "")
        return psycopg2.connect(url)

    url = os.getenv("DATABASE_URL")
    if url:
        url = url.replace("+asyncpg", "").replace("+psycopg2", "")
        return psycopg2.connect(url)

    host = os.getenv("DB_HOST", "localhost")
    port = int(os.getenv("DB_PORT", "5432"))
    name = os.getenv("DB_NAME", "quantmind")
    user = os.getenv("DB_USER", "quantmind")
    password = os.getenv("DB_PASSWORD", "")
    return psycopg2.connect(host=host, port=port, dbname=name, user=user, password=password)


def backfill_return_fields(conn, full: bool = False, recent_days: int = 5, dry_run: bool = False) -> int:
    """
    使用 SQL 窗口函数一次性计算并回填 return_1d 和 return_3d。

    对于全量回填: 计算所有行的 return_1d/3d
    对于滚动更新: 只计算最近 N 天的行（含前3天用于确保 return_3d 有值）
    收益率以复权价口径计算，避免除权除息导致的价格跳空污染。
    """
    with conn.cursor() as cur:
        # 构建日期过滤条件
        if full:
            date_filter = ""
            logger.info("全量回填模式: 计算所有行的 return_1d/3d")
        else:
            date_filter = f"WHERE s.trade_date >= CURRENT_DATE - INTERVAL '{recent_days + 5} days'"
            logger.info(f"滚动更新模式: 回填最近 {recent_days} 天数据")

        # 先统计本次需要重算的行数
        count_sql = f"""
            WITH priced AS (
                SELECT
                    ctid AS row_id,
                    CASE
                        WHEN volume > 0
                         AND NULLIF(close, 0) IS NOT NULL
                         AND COALESCE(adj_factor, 0) > 0
                        THEN close * adj_factor
                    END AS adj_close
                FROM stock_daily_latest
            )
            SELECT COUNT(*)
            FROM stock_daily_latest s
            INNER JOIN priced p ON p.row_id = s.ctid
            {date_filter}
              AND p.adj_close IS NOT NULL
        """
        cur.execute(count_sql)
        target_count = cur.fetchone()[0]
        logger.info(f"本次需要重算的行数: {target_count}")

        if target_count == 0:
            logger.info("无需重算，没有符合条件的记录")
            return 0

        if dry_run:
            logger.info(f"DRY-RUN: 将重算 {target_count} 行")
            return target_count

        # 使用 CTE + 窗口函数计算，然后 UPDATE
        # 注意: 对于滚动更新，需要看 "未来" 数据，所以 LEAD 窗口函数需要完整数据
        update_sql = f"""
            WITH priced AS (
                SELECT
                    ctid AS row_id,
                    symbol,
                    trade_date,
                    CASE
                        WHEN volume > 0
                         AND NULLIF(close, 0) IS NOT NULL
                         AND COALESCE(adj_factor, 0) > 0
                        THEN close * adj_factor
                    END AS adj_close
                FROM stock_daily_latest
            ),
            lead_calc AS (
                SELECT
                    row_id,
                    adj_close,
                    LEAD(adj_close, 1) OVER (PARTITION BY symbol ORDER BY trade_date) AS close_next_1d,
                    LEAD(adj_close, 3) OVER (PARTITION BY symbol ORDER BY trade_date) AS close_next_3d
                FROM priced
                WHERE adj_close IS NOT NULL
            ),
            targets AS (
                SELECT s.ctid AS row_id
                FROM stock_daily_latest s
                {date_filter}
            )
            UPDATE stock_daily_latest s
            SET
                return_1d = CASE
                    WHEN lc.close_next_1d IS NOT NULL AND NULLIF(lc.adj_close, 0) IS NOT NULL
                    THEN lc.close_next_1d / lc.adj_close - 1
                END,
                return_3d = CASE
                    WHEN lc.close_next_3d IS NOT NULL AND NULLIF(lc.adj_close, 0) IS NOT NULL
                    THEN lc.close_next_3d / lc.adj_close - 1
                END
            FROM lead_calc lc
            INNER JOIN targets t ON t.row_id = lc.row_id
            WHERE s.ctid = lc.row_id
        """
        cur.execute(update_sql)
        updated = cur.rowcount
        conn.commit()
        logger.info(f"回填完成: 更新了 {updated} 行")
        return updated


def main() -> None:
    args = parse_args()

    # 加载环境变量
    env_path = PROJECT_ROOT / ".env"
    if env_path.exists():
        from dotenv import load_dotenv
        load_dotenv(env_path, override=False)

    conn = get_conn(args.database_url)
    try:
        backfill_return_fields(
            conn,
            full=args.full,
            recent_days=args.recent_days or 5,
            dry_run=args.dry_run,
        )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
