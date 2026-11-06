"""回填历史订单的执行券商（orders.broker）。

背景：``orders.broker`` 2026-11-06 才上线，之前的单没有这个字段。但历史委托的
``remarks`` 里已经写着通道来源（例：``通达信桥委托``、QMT 相关备注），可以据此
**确定性**回填，不靠猜：

* ``remarks LIKE '%通达信桥%'`` → ``tdx``
* ``remarks ILIKE '%qmt%'``     → ``qmt_exec``

只改 ``broker IS NULL`` 且 ``trading_mode='REAL'`` 的行，幂等；默认 dry-run。

    python backend/scripts/backfill_orders_broker.py            # 只统计
    python backend/scripts/backfill_orders_broker.py --apply    # 落库
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

_RULES = (
    ("tdx", "remarks LIKE '%通达信桥%'"),
    ("qmt_exec", "remarks ILIKE '%qmt%' AND remarks NOT LIKE '%通达信桥%'"),
)


async def main(apply: bool) -> int:
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    async with get_session(read_only=False) as session:
        total = 0
        for broker, predicate in _RULES:
            count_sql = text(
                f"SELECT COUNT(*) FROM orders WHERE broker IS NULL "
                f"AND trading_mode = 'REAL' AND {predicate}"
            )
            n = int((await session.execute(count_sql)).scalar() or 0)
            print(f"{broker}: {n} 行待回填（{predicate}）")
            total += n
            if apply and n:
                upd = text(
                    f"UPDATE orders SET broker = :b WHERE broker IS NULL "
                    f"AND trading_mode = 'REAL' AND {predicate}"
                )
                result = await session.execute(upd, {"b": broker})
                print(f"  已更新 {result.rowcount} 行")
        if apply:
            await session.commit()
            print(f"完成，共 {total} 行候选")
        else:
            print(f"dry-run：共 {total} 行候选；加 --apply 落库")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="实际写库（默认 dry-run）")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.apply)))
