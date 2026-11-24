"""自动推理膨胀防线 - 线上只读验收脚本（不写库、不推理、不改文件）。

用法：
  docker cp scripts/verify_inference_leak_guard.py quantmind:/tmp/
  docker exec quantmind python3 /tmp/verify_inference_leak_guard.py

检查五件事：
1. 默认模型的「缺口」里有多少日期其实已经落库（防线 2 会复用而非重推）；
2. 每个模型 A/B 两套数据的背离程度：库里有分数、pred.parquet 却没有的日期
   —— 这正是修复前会被 Beat 每 30 分钟重推的那批日期；
3. 单日并存 run 数分布（防线 4 的上限是否会被正常业务触发）；
4. 超出保留窗口的行数（防线 3 / janitor 会清掉多少）；
5. 调度留痕 reason_code 分布（PRED_MERGE_FAILED 是否出现过）与治理索引就位情况。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, "/app")


def main() -> int:
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    url = os.getenv("DATABASE_URL", "").replace("+asyncpg", "+psycopg2")
    if not url:
        print("DATABASE_URL 未配置", file=sys.stderr)
        return 2
    db = sessionmaker(bind=create_engine(url, pool_pre_ping=True))()

    from backend.services.engine.inference.backfill_guard import covered_dates_from_db
    from backend.services.engine.inference.gap_backfill import (
        compute_coverage,
        resolve_pred_candidates,
    )

    print("== 1. 默认模型：缺口 vs 已落库分数 ==")
    models = db.execute(
        text(
            "SELECT tenant_id, user_id, model_id, storage_path FROM qm_user_models "
            "WHERE is_default = TRUE AND status IN ('ready','active')"
        )
    ).mappings().all()
    for m in models:
        cov = compute_coverage(model_id=m["model_id"], storage_path=str(m["storage_path"] or ""))
        gaps = list(cov.get("gap_dates") or [])
        covered = covered_dates_from_db(
            tenant_id=m["tenant_id"],
            user_id=m["user_id"],
            model_id=m["model_id"],
            dates=gaps,
        )
        print(
            f"  model={str(m['model_id'])[:52]}\n"
            f"    parquet_max={cov.get('max_date')} cutoff={cov.get('data_cutoff_date')} "
            f"is_up_to_date={cov.get('is_up_to_date')} gap={len(gaps)} "
            f"已落库可复用={len(covered)} 需真推理={len(gaps) - len(covered)}"
        )

    print("== 2. A/B 两套数据背离（库里有分数、parquet 没有的数据日） ==")
    rows = db.execute(
        text(
            """
            SELECT s.tenant_id, s.user_id, s.model_id,
                   array_agg(DISTINCT s.data_trade_date ORDER BY s.data_trade_date) AS db_days,
                   count(DISTINCT s.data_trade_date) AS db_day_count
            FROM qm_research_candidate_snapshot s
            GROUP BY 1,2,3 ORDER BY 5 DESC
            """
        )
    ).fetchall()
    total_divergent = 0
    for tid, uid, mid, db_days, _n in rows:
        sp = db.execute(
            text(
                "SELECT storage_path FROM qm_user_models "
                "WHERE tenant_id=:t AND user_id=:u AND model_id=:m LIMIT 1"
            ),
            {"t": tid, "u": uid, "m": mid},
        ).scalar()
        if not sp:
            print(f"  {str(mid)[:48]:<48} 无 storage_path（模型已删？）跳过")
            continue
        pfile = next((p for p in resolve_pred_candidates(str(sp)) if p.is_file()), None)
        if pfile is None:
            print(f"  {str(mid)[:48]:<48} pred.parquet 不存在 → 库里 {len(db_days)} 日全部背离")
            total_divergent += len(db_days)
            continue
        import duckdb

        con = duckdb.connect()
        try:
            pq = {
                str(r[0])[:10]
                for r in con.execute(
                    f"SELECT DISTINCT CAST(trade_date AS DATE) FROM read_parquet('{pfile}')"
                ).fetchall()
            }
        except Exception as exc:
            print(f"  {str(mid)[:48]:<48} 读 parquet 失败: {exc}")
            continue
        finally:
            con.close()
        missing = [str(d)[:10] for d in db_days if str(d)[:10] not in pq]
        total_divergent += len(missing)
        if missing:
            print(
                f"  {str(mid)[:48]:<48} parquet {len(pq)} 日 / 库 {len(db_days)} 日 "
                f"→ 背离 {len(missing)} 日（修复前会反复重推）: {missing[:6]}"
            )
        else:
            print(f"  {str(mid)[:48]:<48} parquet {len(pq)} 日 / 库 {len(db_days)} 日 → 无背离 ✓")
    print(f"  合计背离数据日: {total_divergent}")

    print("== 3. 单日并存 run 数分布（防线 4 上限=6） ==")
    rows = db.execute(
        text(
            """
            SELECT runs_per_day, count(*) AS buckets
            FROM (
                SELECT tenant_id, user_id, feature_version, trade_date,
                       count(DISTINCT run_id) AS runs_per_day
                FROM engine_signal_scores GROUP BY 1,2,3,4
            ) x GROUP BY 1 ORDER BY 1 DESC
            """
        )
    ).fetchall()
    for r in rows:
        print(f"  并存 {r[0]} 个 run/日 → {r[1]} 个日桶")
    over = [r for r in rows if int(r[0]) > 6]
    print(f"  超过上限的桶数: {len(over)}（新写入时才会触发淘汰，历史数据由清理脚本负责）")

    print("== 4. 超出保留窗口(365天)的行 ==")
    for tbl, col in (
        ("engine_signal_scores", "trade_date"),
        ("qm_research_candidate_snapshot", "prediction_trade_date"),
    ):
        n = db.execute(
            text(f"SELECT count(*) FROM {tbl} WHERE {col} < CURRENT_DATE - 365")
        ).scalar()
        print(f"  {tbl}.{col} 超窗 {n} 行")

    print("== 5. 调度留痕 reason_code 与治理索引 ==")
    for r in db.execute(
        text(
            "SELECT status, coalesce(reason_code,'-') rc, count(*) "
            "FROM qm_model_inference_dispatch_logs GROUP BY 1,2 ORDER BY 3 DESC"
        )
    ).fetchall():
        print(f"  {str(r[0]):<8} {str(r[1]):<20} {r[2]}")
    idx = db.execute(
        text(
            "SELECT indexname FROM pg_indexes WHERE indexname LIKE 'idx_qrcs%' ORDER BY 1"
        )
    ).fetchall()
    print(f"  治理索引: {', '.join(str(r[0]) for r in idx) or '（缺失，janitor 会自动补建）'}")
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
