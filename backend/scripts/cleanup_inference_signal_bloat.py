#!/usr/bin/env python3
"""推理落库膨胀一次性清理（默认 dry-run，只报告不删除）。

用法（在项目根目录 / 容器内执行）：
  python backend/scripts/cleanup_inference_signal_bloat.py                 # 只统计
  python backend/scripts/cleanup_inference_signal_bloat.py --retention-days 30
  python backend/scripts/cleanup_inference_signal_bloat.py --purge-orphan-runs
  python backend/scripts/cleanup_inference_signal_bloat.py --apply --vacuum

背景（客户反馈「系统反复推理占用几十 GB 数据库」）：
- `engine_signal_scores` / `qm_research_candidate_snapshot` 是同一次推理的双写，
  唯一键都含 `run_id`，而 `run_id` 每次执行都是新 UUID → 重推同一天是「净新增」。
- 旧的保留期清理把条件绑在 `model_version='inference_script' + 本次 feature_version`
  上，且默认 730 天：换过一次模型，旧 feature_version 的整块历史就再也没有删除路径。
- `qm_research_candidate_snapshot` 此前完全没有时间维度清理。
本脚本负责把存量脏数据一次性收敛（增量防线见
`backend/services/engine/inference/{gap_backfill,script_runner}.py` 与
`backend/services/engine/services/signal_storage_janitor.py`）。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def _sync_url() -> str:
    url = str(os.getenv("DATABASE_URL", "")).strip()
    if "+asyncpg" in url:
        url = url.replace("+asyncpg", "+psycopg2")
    if url:
        return url
    return (
        f"postgresql+psycopg2://{os.getenv('DB_USER', 'quantmind')}"
        f":{os.getenv('DB_PASSWORD', 'quantmind2026')}"
        f"@{os.getenv('DB_HOST', 'localhost')}:{os.getenv('DB_PORT', '5432')}"
        f"/{os.getenv('DB_NAME', 'quantmind')}"
    )


def report_sizes(db) -> None:
    from sqlalchemy import text

    rows = db.execute(
        text(
            """
            SELECT relname,
                   pg_size_pretty(pg_total_relation_size(relid)) AS total,
                   pg_size_pretty(pg_relation_size(relid))       AS heap,
                   pg_size_pretty(pg_indexes_size(relid))        AS indexes,
                   n_live_tup
            FROM pg_stat_user_tables
            WHERE relname IN ('engine_signal_scores', 'qm_research_candidate_snapshot',
                              'engine_feature_runs', 'qm_model_inference_runs')
            ORDER BY pg_total_relation_size(relid) DESC
            """
        )
    ).fetchall()
    print("== 当前占用 ==")
    for r in rows:
        print(
            f"  {r[0]:<34} total={r[1]:>9}  heap={r[2]:>9}  idx={r[3]:>9}  rows={r[4]:>9}"
        )


def report_fv_blocks(db) -> None:
    """按 feature_version 列出 engine_signal_scores 的块（找出已经没人跑的孤儿块）。"""
    from sqlalchemy import text

    rows = db.execute(
        text(
            """
            SELECT feature_version,
                   model_version,
                   count(*) AS rows_,
                   min(trade_date) AS oldest,
                   max(trade_date) AS newest,
                   pg_size_pretty(pg_total_relation_size('engine_signal_scores')) AS tbl
            FROM engine_signal_scores
            GROUP BY 1, 2
            ORDER BY max(trade_date) DESC
            """
        )
    ).fetchall()
    print("== engine_signal_scores 按 feature_version 分块 ==")
    for r in rows:
        print(
            f"  {str(r[0])[:52]:<52} model_version={str(r[1])[:18]:<18} "
            f"rows={r[2]:>9}  {r[3]}~{r[4]}"
        )


def orphan_runs(db) -> list[tuple[str, int]]:
    """候选快照里有、信号明细里已不存在的 run（历史重推/跨 run 删除留下的孤儿）。"""
    from sqlalchemy import text

    return [
        (str(r[0]), int(r[1]))
        for r in db.execute(
            text(
                """
                SELECT s.run_id, count(*) AS rows_
                FROM qm_research_candidate_snapshot s
                WHERE NOT EXISTS (
                    SELECT 1 FROM engine_signal_scores e WHERE e.run_id = s.run_id
                )
                GROUP BY s.run_id
                ORDER BY rows_ DESC
                """
            )
        ).fetchall()
    ]


def main() -> int:
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    from backend.services.engine.services.signal_storage_janitor import (
        DEFAULT_RETENTION_DAYS,
        purge_signal_storage,
    )

    ap = argparse.ArgumentParser(description="推理落库膨胀一次性清理")
    ap.add_argument(
        "--retention-days",
        type=int,
        default=DEFAULT_RETENTION_DAYS,
        help=f"信号明细/候选快照保留天数（默认 {DEFAULT_RETENTION_DAYS}）",
    )
    ap.add_argument(
        "--apply",
        action="store_true",
        help="真正删除（默认只 dry-run 统计）",
    )
    ap.add_argument(
        "--purge-orphan-runs",
        action="store_true",
        help="额外删除「快照有、明细无」的孤儿 run 快照行（历史重推循环遗留）",
    )
    ap.add_argument(
        "--tenant",
        default="",
        help="只处理指定 tenant_id（默认全部）",
    )
    ap.add_argument(
        "--user",
        default="",
        help="只处理指定 user_id（默认全部）",
    )
    ap.add_argument(
        "--vacuum",
        action="store_true",
        help="删除后执行 VACUUM (ANALYZE)（不回收磁盘，只更新统计信息；"
        "要把磁盘还给 OS 需要 pg_repack 或 VACUUM FULL，会锁表）",
    )
    args = ap.parse_args()

    if args.retention_days < 1:
        print("retention-days 必须 >= 1", file=sys.stderr)
        return 2

    engine = create_engine(_sync_url(), pool_pre_ping=True)
    db = sessionmaker(bind=engine)()
    try:
        report_sizes(db)
        report_fv_blocks(db)

        orphans = orphan_runs(db)
        orphan_rows = sum(n for _, n in orphans)
        print(
            f"== 孤儿 run（候选快照有、信号明细无）== {len(orphans)} 个 run，"
            f"{orphan_rows} 行"
        )
        for run_id, n in orphans[:10]:
            print(f"  {run_id:<28} rows={n}")
        if len(orphans) > 10:
            print(f"  ...（其余 {len(orphans) - 10} 个略）")

        print("== 保留窗口清理 ==")
        res = purge_signal_storage(
            retention_days=args.retention_days,
            tenant_id=args.tenant or None,
            user_id=args.user or None,
            dry_run=not args.apply,
        )
        print(f"  status={res.get('status')} users={res.get('users')}")
        if res.get("indexes_created"):
            print(f"  补齐治理索引: {', '.join(res['indexes_created'])}")
        for k, v in (res.get("stats") or {}).items():
            print(f"  {k:<36} {v}")
        if res.get("note"):
            print(f"  note: {res['note']}")

        if args.purge_orphan_runs and orphans:
            run_ids = [r for r, _ in orphans]
            if args.apply:
                deleted = db.execute(
                    text(
                        """
                        DELETE FROM qm_research_candidate_snapshot
                        WHERE run_id = ANY(:run_ids)
                          AND NOT EXISTS (
                              SELECT 1 FROM engine_signal_scores e
                              WHERE e.run_id = qm_research_candidate_snapshot.run_id
                          )
                        """
                    ),
                    {"run_ids": run_ids},
                ).rowcount
                db.commit()
                print(f"== 孤儿 run 快照清理 == 已删除 {deleted} 行")
            else:
                print(
                    f"== 孤儿 run 快照清理 == dry-run：可删除 {orphan_rows} 行"
                    f"（加 --apply 生效）"
                )

        if args.vacuum and args.apply:
            for tbl in (
                "engine_signal_scores",
                "qm_research_candidate_snapshot",
                "engine_feature_runs",
            ):
                db.execute(text(f"VACUUM (ANALYZE) {tbl}"))
            db.commit()
            print("== VACUUM (ANALYZE) 完成 ==")
        elif args.apply:
            print("提示：未加 --vacuum，统计信息尚未更新。")
        return 0
    finally:
        db.close()
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
