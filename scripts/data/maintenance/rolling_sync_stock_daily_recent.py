#!/usr/bin/env python3
"""手动滚动刷新 PG stock_daily_latest 最近 N 天（默认 30 天 ≈ 1 个月），QuantDB 源。

**日常无需手动执行**：已并入 QuantDB 同步脚本
（`quantdb_daily_sync.run_daily_sync` Phase 2 → `stock_daily_latest_refresh.
refresh_stock_daily_latest`），随 QuantDB 同步一起跑，QuantDB 没同步就不执行。

本脚本供手动补跑 / 强制回刷 / 清理窗口外历史。

用法：
    python scripts/data/maintenance/rolling_sync_stock_daily_recent.py
    python scripts/data/maintenance/rolling_sync_stock_daily_recent.py --days 22 --force
    python scripts/data/maintenance/rolling_sync_stock_daily_recent.py --prune
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# 以 `python /app/scripts/...` 直跑时 cwd 不在 sys.path，先补项目根
PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
LOGGER = logging.getLogger("rolling_sync_recent")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="手动滚动刷新 stock_daily_latest 最近 N 天（QuantDB 源）"
    )
    parser.add_argument("--days", type=int, default=30, help="回刷窗口（自然日，默认 30）")
    parser.add_argument("--batch-days", type=int, default=20, help="每批交易日数（默认 20）")
    parser.add_argument(
        "--prune",
        action="store_true",
        help="删除窗口外的历史行（真正滚动窗口；默认保留历史）",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="忽略「QuantDB 未推进则跳过」门控，强制回刷窗口",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    from backend.scripts.stock_daily_latest_refresh import (
        refresh_stock_daily_latest,
    )

    result = refresh_stock_daily_latest(
        days=args.days,
        batch_days=args.batch_days,
        prune=args.prune,
        force=args.force,
    )
    LOGGER.info("结果: %s", result)
    return 0 if result.get("status") in ("ok", "skipped") else 1


if __name__ == "__main__":
    raise SystemExit(main())
