#!/usr/bin/env python3
"""回填 engine_signal_scores 的 Signal 契约列（T-P1-01）。

回填内容（幂等，可重跑）：
  rank_pct  — 截面分位 0..1，`percent_rank() OVER (PARTITION BY run_id ORDER BY fusion_score)`
              （与 backend/shared/signal_contract.compute_rank_pct 同口径：并列取最小名次，n=1 → 0.0）
  market    — 历史 NULL 补 'CN'（universe_tag 已知行不动）
  source    — 历史 NULL 补 'batch'

策略：按 trade_date 分窗循环（默认 30 天/批），每批单条 UPDATE + 逐批提交；
只更新 rank_pct IS NULL 的行（重跑安全）；窗口函数在整窗全量行上计算（部分回填的 run 分母仍正确）。

用法（容器内）:
    python backend/scripts/backfill_rank_pct.py                  # dry-run：只报表
    python backend/scripts/backfill_rank_pct.py --apply          # 执行（可重跑）
    python backend/scripts/backfill_rank_pct.py --apply --since 2026-09-01 --until 2026-09-15
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import date, timedelta

from sqlalchemy import create_engine, text

PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
sys.path.insert(0, PROJECT_ROOT)

DEFAULT_BATCH_DAYS = 30

_UPDATE_SQL = text("""
    UPDATE engine_signal_scores s
    SET rank_pct = sub.pct,
        market = COALESCE(s.market, 'CN'),
        source = COALESCE(s.source, 'batch')
    FROM (
        SELECT id,
               percent_rank() OVER (PARTITION BY run_id ORDER BY fusion_score) AS pct
        FROM engine_signal_scores
        WHERE trade_date >= :since AND trade_date <= :until
    ) sub
    WHERE s.id = sub.id
      AND s.rank_pct IS NULL
""")

_COUNT_SQL = text("""
    SELECT count(*) AS null_rows, count(DISTINCT run_id) AS runs
    FROM engine_signal_scores
    WHERE trade_date >= :since AND trade_date <= :until AND rank_pct IS NULL
""")

_RANGE_SQL = text("SELECT min(trade_date) AS lo, max(trade_date) AS hi FROM engine_signal_scores")


def _get_engine():
    db_url = os.getenv(
        "DATABASE_URL",
        f"postgresql://{os.getenv('DB_USER', 'quantmind')}:{os.getenv('DB_PASSWORD', '')}"
        f"@{os.getenv('DB_HOST', 'db')}:{os.getenv('DB_PORT', '5432')}/{os.getenv('DB_NAME', 'quantmind')}",
    )
    if "+asyncpg" in db_url:
        db_url = db_url.replace("+asyncpg", "+psycopg2")
    return create_engine(db_url, pool_pre_ping=True, future=True)


def _iter_windows(lo: date, hi: date, batch_days: int):
    cur = lo
    while cur <= hi:
        end = min(cur + timedelta(days=batch_days - 1), hi)
        yield cur, end
        cur = end + timedelta(days=1)


def main() -> int:
    parser = argparse.ArgumentParser(description="回填 Signal 契约列（rank_pct/market/source）")
    parser.add_argument("--apply", action="store_true", help="执行写入（默认 dry-run）")
    parser.add_argument("--since", default="", help="起止日期 YYYY-MM-DD（默认全表）")
    parser.add_argument("--until", default="", help="结束日期 YYYY-MM-DD（默认全表）")
    parser.add_argument("--batch-days", type=int, default=DEFAULT_BATCH_DAYS)
    args = parser.parse_args()

    engine = _get_engine()
    with engine.connect() as conn:
        row = conn.execute(_RANGE_SQL).one()
        if row.lo is None:
            print("表为空，无事可做")
            return 0
        lo = date.fromisoformat(args.since) if args.since else row.lo
        hi = date.fromisoformat(args.until) if args.until else row.hi

        total_null = 0
        windows = list(_iter_windows(lo, hi, max(1, args.batch_days)))
        print(f"范围 {lo} ~ {hi}，{len(windows)} 个窗口，模式={'APPLY' if args.apply else 'DRY-RUN'}")
        for win_lo, win_hi in windows:
            stats = conn.execute(_COUNT_SQL, {"since": win_lo, "until": win_hi}).one()
            total_null += int(stats.null_rows)
            if not args.apply:
                print(f"  [dry] {win_lo}~{win_hi}: 待回填 {stats.null_rows} 行 / {stats.runs} run")
                continue
            t0 = time.time()
            result = conn.execute(_UPDATE_SQL, {"since": win_lo, "until": win_hi})
            conn.commit()
            print(
                f"  [apply] {win_lo}~{win_hi}: 更新 {result.rowcount} 行"
                f"（待回填 {stats.null_rows}）耗时 {time.time() - t0:.1f}s"
            )
    if not args.apply:
        print(f"dry-run 合计待回填 {total_null} 行；加 --apply 执行（幂等，可重跑）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
