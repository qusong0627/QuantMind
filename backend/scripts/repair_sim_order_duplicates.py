#!/usr/bin/env python3
"""sim_orders 幂等键重复修复（T-P2-08 配套）：唯一索引启用前的存量重复清理。

用法（容器内）:
    python backend/scripts/repair_sim_order_duplicates.py            # DRY-RUN（默认）
    python backend/scripts/repair_sim_order_duplicates.py --apply    # 实际写库

口径（机构级，保守）：
- 重复组 = (tenant_id, user_id, client_order_id) 相同且 cid 非空；
- 每组保留 **id 最小**（首次提交）的一行，其余为重复产物；
- 重复行 **不物理删除**（金融行留审计痕迹）：client_order_id 置 NULL +
  remarks 追加 ``[DUP-OF:{保留行 order_id}]``——置空后不再占用幂等键，
  唯一索引即可建立；
- **有成交的重复行跳过并点名**（双成交涉及资金，必须人工核对，脚本不自动处理）；
- 幂等：处理过的行 cid 已为空，二次运行为 0；``--apply`` 后建议重跑
  ``ensure_sim_order_unique_index_async``（写入路径下一次调用会自动启用）。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


async def _scan_duplicate_groups(session) -> list[dict]:
    from sqlalchemy import text as sa_text

    rows = (
        await session.execute(
            sa_text(
                "SELECT tenant_id, user_id, client_order_id, "
                "       array_agg(id ORDER BY id) AS ids, "
                "       min(order_id::text) AS first_order_id "
                "FROM sim_orders WHERE client_order_id IS NOT NULL "
                "GROUP BY tenant_id, user_id, client_order_id "
                "HAVING count(*) > 1 ORDER BY tenant_id, user_id, client_order_id"
            )
        )
    ).fetchall()
    return [
        {
            "tenant_id": str(r[0]),
            "user_id": str(r[1]),
            "client_order_id": str(r[2]),
            "ids": [int(x) for x in r[3]],
            "kept_order_id": str(r[4]),
        }
        for r in rows
    ]


async def _fill_counts(session, ids: list[int]) -> dict[int, int]:
    from sqlalchemy import text as sa_text

    rows = (
        await session.execute(
            sa_text(
                "SELECT o.id, count(f.*) FROM sim_orders o "
                "LEFT JOIN sim_trades f ON f.order_id = o.order_id "
                "WHERE o.id = ANY(:ids) GROUP BY o.id"
            ),
            {"ids": ids},
        )
    ).fetchall()
    return {int(r[0]): int(r[1]) for r in rows}


async def run(*, apply: bool) -> int:
    from backend.shared.database_manager_v2 import close_database, get_session

    groups = []
    async with get_session(read_only=not apply) as session:
        groups = await _scan_duplicate_groups(session)

    if not groups:
        print("[repair] 无重复组（幂等键口径健康）")
        await close_database()
        return 0

    total_rows = sum(len(g["ids"]) - 1 for g in groups)
    print(f"[repair] 重复组 {len(groups)}，冗余行 {total_rows}")
    auto_plan: list[tuple[dict, int]] = []
    manual: list[tuple[dict, int, int]] = []
    async with get_session(read_only=not apply) as session:
        for g in groups:
            dup_ids = g["ids"][1:]  # 保留 id 最小行
            fills = await _fill_counts(session, dup_ids)
            for dup_id in dup_ids:
                n = fills.get(dup_id, 0)
                if n > 0:
                    manual.append((g, dup_id, n))
                else:
                    auto_plan.append((g, dup_id))
    for g, dup_id in auto_plan:
        print(
            f"  可自动处理: {g['tenant_id']}/{g['user_id']}/{g['client_order_id']} "
            f"-> 置空 cid（保留行 order_id={g['kept_order_id']}）"
        )
    for g, dup_id, n in manual:
        print(
            f"  [需人工] {g['tenant_id']}/{g['user_id']}/{g['client_order_id']} "
            f"重复行 id={dup_id} 有 {n} 笔成交——双成交涉资金，不自动处理"
        )

    if not apply:
        print("[repair] DRY-RUN（未写库）。确认清单后加 --apply 执行。")
        await close_database()
        return 0

    from sqlalchemy import text as sa_text

    applied = 0
    async with get_session(read_only=False) as session:
        for g, dup_id in auto_plan:
            result = await session.execute(
                sa_text(
                    "UPDATE sim_orders SET client_order_id = NULL, "
                    "remarks = COALESCE(remarks, '') || :mark "
                    "WHERE id = :id AND client_order_id = :cid"
                ),
                {"id": dup_id, "cid": g["client_order_id"], "mark": f" [DUP-OF:{g['kept_order_id']}]"},
            )
            applied += int(result.rowcount or 0)
        await session.commit()
    print(f"[repair] 已处理 {applied} 行（cid 置空 + 标记保留行）；{len(manual)} 行留待人工")
    if manual:
        print("[repair] 存在需人工的重复行——唯一索引在人工处理后自动启用（写入路径重试）")
    await close_database()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="sim_orders 幂等键重复修复（T-P2-08）")
    parser.add_argument("--apply", action="store_true", help="实际写库（默认 DRY-RUN）")
    args = parser.parse_args()
    return asyncio.run(run(apply=args.apply))


if __name__ == "__main__":
    raise SystemExit(main())
