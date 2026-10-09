#!/usr/bin/env python3
"""regime 日表回填 / 手动日更 CLI（P3 · 设计 §6.2）。

用法::

    python backend/scripts/regime_backfill.py --all                      # 日更形态（各市场尾部 10 个生效日）
    python backend/scripts/regime_backfill.py --market CN --from 2018-01-01
    python backend/scripts/regime_backfill.py --market CN --from 2018-01-01 --to 2026-09-30
    python backend/scripts/regime_backfill.py --market US --dry-run      # 只算不写

语义：写入按**生效日**（i→i+1），历史行冻结（冲突不覆盖）——所以本工具对同一区间
可以放心重跑（0 新行）；要改口径只能向前生效（换 ``thresholds_hash``），存量行
``computed_at`` 即审计留痕。表外市场（CRYPTO/FUTURES）诚实拒绝。

退出码：0 = 全部 reason=ok；1 = 有市场被拒绝 / 取数失败 / 写库失败。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="qm_regime_daily 回填 / 日更")
    parser.add_argument("--market", action="append", default=None,
                        help="市场（可重复；CN/HK/US）")
    parser.add_argument("--all", action="store_true",
                        help="全部可持久化市场（CN/HK/US）")
    parser.add_argument("--from", dest="from_date", default=None,
                        help="回填起始生效日 YYYY-MM-DD（给了即回填模式，--tail 失效）")
    parser.add_argument("--to", dest="to_date", default=None,
                        help="回填结束生效日 YYYY-MM-DD（可选）")
    parser.add_argument("--tail", type=int, default=10,
                        help="日更模式重算的尾部生效日数（默认 10）")
    parser.add_argument("--dry-run", action="store_true", help="只计算不写库")
    return parser


async def _run(markets: tuple[str, ...], args: argparse.Namespace) -> int:
    from backend.services.engine.regime_persist import persist_all
    from backend.shared.database_manager_v2 import close_database

    try:
        summaries = await persist_all(
            markets,
            backfill_from=args.from_date,
            backfill_to=args.to_date,
            tail=args.tail,
            dry_run=args.dry_run,
        )
    finally:
        await close_database()

    rc = 0
    for summary in summaries:
        print(json.dumps(summary, ensure_ascii=False, default=str))
        if summary.get("reason") != "ok":
            rc = 1
    return rc


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    from backend.services.engine.regime_persist import DEFAULT_MARKETS

    markets: tuple[str, ...]
    if args.all:
        markets = DEFAULT_MARKETS
    elif args.market:
        markets = tuple(args.market)
    else:
        print("需要 --all 或 --market（可重复）", file=sys.stderr)
        return 2
    return asyncio.run(_run(markets, args))


if __name__ == "__main__":
    raise SystemExit(main())
