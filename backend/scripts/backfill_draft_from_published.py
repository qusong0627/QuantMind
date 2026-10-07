#!/usr/bin/env python3
"""给存量草稿补上「从线上抄一份」——修复之前建的草稿都缺这一步。

背景（2026-10-07 线上实证）：``publish_catalog_version`` 的语义是**替换**——
把同源同市场的旧版转 ``archived``、把草稿原样扶正，中间**没有合并**。而经
「因子研究 → 注册到训练目录」建出的草稿是**空底**的（只含本次注册的那几个），
于是「发布」不是把新因子加进线上，而是把线上口径砍成草稿里那几个。
``factor_defs`` 线上 1336 个启用特征，注册建的草稿里只有 2 个。

代码侧已在 ``_draft_version_id`` 建草稿时补种（``seed_draft_from_published``），
但**修复之前建的草稿补不到**——本脚本就是补它们。判据是「草稿的启用特征集
⊇ 线上那份的启用集」，不满足才写。

幂等：复制走 ``ON CONFLICT DO NOTHING``，**草稿里已有的行优先**，重跑无副作用。
只写 ``qm_training_factor_mapping``（草稿），**不碰已发布版本、不碰 parquet**。
草稿不参与训练（训练只读 ``published``），所以补种不影响任何线上训练口径。

用法（在 quantmind 容器内执行）：
    docker exec -w /app quantmind python backend/scripts/backfill_draft_from_published.py \
        [--market CN] [--apply]

不带 ``--apply`` 是演练：只打印每份草稿会补多少行。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sqlalchemy import text  # noqa: E402

from backend.services.api.routers.admin.quantdb_factor_catalog import (  # noqa: E402
    _ensure_schema,
    clone_version_mappings,
)
from backend.shared.database_manager_v2 import get_session  # noqa: E402

# 补种判据的两个方向：线上有而草稿没有 = 会缩（要补）；草稿有而线上没有 = 本次注册的
# 新因子（正常，不动）。
_COUNTS_SQL = text("""
    SELECT
      (SELECT count(*) FROM qm_training_factor_mapping
        WHERE version_id = :published AND enabled)                        AS online,
      (SELECT count(*) FROM qm_training_factor_mapping
        WHERE version_id = :draft AND enabled)                            AS draft,
      (SELECT count(*) FROM qm_training_factor_mapping m
        WHERE m.version_id = :published AND m.enabled
          AND NOT EXISTS (
            SELECT 1 FROM qm_training_factor_mapping d
            WHERE d.version_id = :draft AND d.source_dataset = m.source_dataset
              AND d.feature_key = m.feature_key))                         AS missing
""")


async def _published_id(session, *, market: str, source_dataset: str) -> str | None:
    found = (await session.execute(text("""
        SELECT version_id FROM qm_training_factor_catalog_version
        WHERE market = :market AND source_dataset = :source_dataset
          AND status = 'published'
        ORDER BY published_at DESC NULLS LAST, created_at DESC LIMIT 1
    """), {"market": market, "source_dataset": source_dataset})).scalars().first()
    return str(found) if found else None


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market", default="CN", help="市场（默认 CN）")
    parser.add_argument(
        "--apply", action="store_true", help="真正写库；不带则只演练"
    )
    args = parser.parse_args()

    async with get_session() as session:
        await _ensure_schema(session)
        rows = (await session.execute(text("""
            SELECT version_id, version_name, source_dataset
            FROM qm_training_factor_catalog_version
            WHERE market = :market AND status = 'draft'
            ORDER BY source_dataset
        """), {"market": args.market})).mappings().all()

    if not rows:
        print(f"{args.market} 没有草稿，无事可做")
        return 0

    print(f"{'来源库':<16}{'线上启用':>8}{'草稿启用':>9}{'缺口':>6}   动作")
    print("-" * 72)
    todo: list[tuple[str, str]] = []
    for row in rows:
        async with get_session() as session:
            published = await _published_id(
                session, market=args.market, source_dataset=row["source_dataset"]
            )
            if not published:
                print(
                    f"{row['source_dataset']:<16}{'—':>8}{'—':>9}{'—':>6}"
                    "   无已发布版本，跳过（草稿保持原样）"
                )
                continue
            counts = (await session.execute(_COUNTS_SQL, {
                "published": published, "draft": row["version_id"],
            })).mappings().first()
            online, draft_n, missing = (
                int(counts["online"]), int(counts["draft"]), int(counts["missing"])
            )
            if not missing:
                print(
                    f"{row['source_dataset']:<16}{online:>8}{draft_n:>9}{0:>6}"
                    "   已覆盖线上全部启用特征，无需补种"
                )
                continue
            todo.append((row["source_dataset"], row["version_id"]))
            print(
                f"{row['source_dataset']:<16}{online:>8}{draft_n:>9}{missing:>6}"
                f"   补种（{row['version_name'][:20]}）"
            )

    if not todo:
        print("\n没有需要补种的草稿。")
        return 0

    if not args.apply:
        print(f"\n演练模式：以上 {len(todo)} 份草稿待补种。加 --apply 执行。")
        return 0

    print()
    failed = 0
    for source, draft_id in todo:
        async with get_session() as session:
            published = await _published_id(
                session, market=args.market, source_dataset=source
            )
            if not published:  # 两次会话之间被发布/删除的极端情况
                print(f"  ! {source} 已无线上版本，跳过")
                continue
            added = await clone_version_mappings(
                session,
                source_version_id=published,
                target_version_id=draft_id,
            )
            after = (await session.execute(_COUNTS_SQL, {
                "published": published, "draft": draft_id,
            })).mappings().first()
        print(
            f"  ✓ {source:<16} 补入 {added:>5} 行 → 草稿启用 {int(after['draft'])}"
            f" / 线上 {int(after['online'])}，缺口 {int(after['missing'])}"
        )
        if int(after["missing"]):
            failed += 1
            print(f"      ! 仍有缺口，需人工看：{source}")

    print(f"\n完成：{len(todo)} 份草稿处理，{failed} 份仍有缺口。")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
