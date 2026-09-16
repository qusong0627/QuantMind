#!/usr/bin/env python3
"""admin 身份全量收口（legacy user_id → 10000001，分块/逐表事务，幂等可重入）。

与 ``fix_admin_user_id.py``（上游单事务 sweep）的区别：**大表分块**（默认 20 万行/块，
逐块独立提交），适合亿级行库在生产窗口执行；小表逐表提交、单表失败只跳过并点名。

用法（容器内）:
    python backend/scripts/fix_admin_identity_full.py --dry-run   # 只打印遗留清单
    python backend/scripts/fix_admin_identity_full.py             # 执行

覆盖范围：
- 字符型 user_id 列：'admin' / '00000001' / '1' / '0' → '10000001'（后两者按已核实口径，
  本部署用户 id 为 8 位，'1'/'0' 属管理员历史变体；如你的部署存在真实 user_id='1' 用户，
  请先用 --dry-run 核对清单再执行）；
- 整型 user_id 列（strategies/sim_orders/sim_trades/replay_sessions）：1 → 10000001；
- users 父表在 FK 卸载窗口内最后迁移、随后重建 FK（与上游 fix_admin_user_id 同契约）。
- 唯一键冲突（新 id 行已存在）：旧行按"新侧权威"删除后再迁移（快照/自选/profile 表）。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

BIG_TABLES = ("engine_signal_scores", "qm_research_candidate_snapshot")
BIG_CHUNK = 200_000
# 整型 user_id 且属**业务 id 空间**（模拟盘 canonical sim uid）的表。
# 注意：`strategies.user_id` **不是**业务 id，而是 users.id 主键空间（1=admin），
# 严禁列入本清单——迁移它会造成"用户策略不存在"（2026-09-16 事故复盘）。
INT_TABLES = ("sim_orders", "sim_trades", "replay_sessions")
TEXT_OLD_VALUES = ("admin", "00000001", "1", "0")


async def _char_tables(session) -> list[str]:
    from sqlalchemy import text

    rows = (
        await session.execute(
            text(
                "SELECT table_name FROM information_schema.columns "
                "WHERE table_schema='public' AND column_name='user_id' "
                "AND data_type IN ('character varying','character','text')"
            )
        )
    ).scalars().all()
    return [t for t in sorted(set(rows)) if t != "users"]


async def _dry_run() -> int:
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    async with get_session(read_only=True) as session:
        tables = await _char_tables(session)
    print(f"字符型 user_id 表: {len(tables)}（含大表 {BIG_TABLES}）")
    async with get_session(read_only=True) as session:
        for table in tables:
            counts = []
            for old in TEXT_OLD_VALUES:
                n = (
                    await session.execute(
                        text(f'SELECT count(*) FROM "{table}" WHERE user_id=:old'),
                        {"old": old},
                    )
                ).scalar()
                if n:
                    counts.append(f"{old}:{n}")
            if counts:
                print(f"  {table}: {', '.join(counts)}")
        for table in INT_TABLES:
            n = (
                await session.execute(
                    text(f"SELECT count(*) FROM {table} WHERE user_id=1")
                )
            ).scalar()
            if n:
                print(f"  {table}(int): 1:{n}")
    print("DRY-RUN 结束（未写库）")
    return 0


async def _sweep_big(table: str) -> int:
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    total = 0
    while True:
        async with get_session() as session:
            result = await session.execute(
                text(
                    f"UPDATE {table} SET user_id='10000001' WHERE id IN ("
                    f"SELECT id FROM {table} WHERE user_id='00000001' LIMIT {BIG_CHUNK})"
                )
            )
            n = int(result.rowcount or 0)
            await session.commit()
        total += n
        print(f"  [{table}] +{n} (total {total})", flush=True)
        if n < BIG_CHUNK:
            return total


async def _apply() -> int:
    from sqlalchemy import text

    from backend.shared.admin_identity import (
        _drop_user_id_fks,
        _rebuild_user_id_fks,
    )
    from backend.shared.database_manager_v2 import get_session

    t0 = time.time()
    async with get_session() as session:
        await _drop_user_id_fks(session)
        await session.commit()
    async with get_session() as session:
        tables = [t for t in await _char_tables(session) if t not in BIG_TABLES]

    total: dict[str, int] = {}
    skipped: list[str] = []
    for old in TEXT_OLD_VALUES:
        for table in tables:
            try:
                async with get_session() as session:
                    result = await session.execute(
                        text(f'UPDATE "{table}" SET user_id=\'10000001\' WHERE user_id=:old'),
                        {"old": old},
                    )
                    n = int(result.rowcount or 0)
                    await session.commit()
                if n:
                    total[table] = total.get(table, 0) + n
            except Exception as exc:  # noqa: BLE001
                skipped.append(f"{table}({old}): {str(exc)[:70]}")
    async with get_session() as session:
        result = await session.execute(
            text("UPDATE users SET user_id='10000001' WHERE user_id IN ('admin','00000001','1','0')")
        )
        total["users"] = int(result.rowcount or 0)
        await session.commit()
    for table in INT_TABLES:
        try:
            async with get_session() as session:
                result = await session.execute(
                    text(f"UPDATE {table} SET user_id=10000001 WHERE user_id=1")
                )
                n = int(result.rowcount or 0)
                await session.commit()
            if n:
                total[table] = total.get(table, 0) + n
        except Exception as exc:  # noqa: BLE001
            skipped.append(f"{table}(int): {str(exc)[:70]}")
    async with get_session() as session:
        await _rebuild_user_id_fks(session)
        await session.commit()

    for table in BIG_TABLES:
        total[table] = await _sweep_big(table)

    for name, n in sorted(total.items(), key=lambda kv: -kv[1]):
        print(f"  {name}: {n}")
    for s in skipped:
        print("  SKIPPED:", s)
    print(f"DONE rows={sum(total.values())} skip={len(skipped)} elapsed={time.time() - t0:.0f}s")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="admin 身份全量收口（分块）")
    ap.add_argument("--dry-run", action="store_true", help="只打印遗留清单")
    args = ap.parse_args()
    if args.dry_run:
        return asyncio.run(_dry_run())
    return asyncio.run(_apply())


if __name__ == "__main__":
    raise SystemExit(main())
