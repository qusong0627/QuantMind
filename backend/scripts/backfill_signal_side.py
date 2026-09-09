#!/usr/bin/env python3
"""
回填历史 signal_side：用修复后的闸门重算 2026-08-11 ~ 2026-09-08 窗口。

背景：三道闸门（confidence 被解析层丢弃 / 共识阈值不可达 / 绝对阈能量纲不符）
自 2026-08-11 起把窗口内所有 run 的 signal_side 压成 HOLD，修复见 commit 742dcd9f。
本脚本用同一套 `_resolve_signal_sides` 重放历史截面，把 signal_side 及派生字段
（engine_signal_scores.quality.position、候选池 confidence_level）改回修复后口径。

口径（与生产一致）：
  - 分数：表内 fusion_score（按 run 截面）；
  - 共识：quality->'consensus'（缺失 → None → 闸门跳过）；
  - 置信度：历史未落库（曾被解析层丢弃）→ 传 None → 闸门跳过；
  - 仅重算 model_version='inference_script' 的行。

用法:
    python backend/scripts/backfill_signal_side.py --dry-run    # 只报表，不写库
    python backend/scripts/backfill_signal_side.py --apply      # 备份 + 写库（幂等）
    python backend/scripts/backfill_signal_side.py --rollback   # 从备份表还原
可选: --since 2026-08-11 --until 2026-09-08
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from typing import Any

from sqlalchemy import create_engine, text

PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
sys.path.insert(0, PROJECT_ROOT)

from backend.services.engine.inference.position_signal import (  # noqa: E402
    batch_update_quality,
    compute_position_scores,
)
from backend.services.engine.inference.script_runner import InferenceScriptRunner  # noqa: E402

DEFAULT_SINCE = "2026-08-11"
DEFAULT_UNTIL = "2026-09-08"
BACKUP_TAG = "20260909"
SS_BAK = f"engine_signal_scores_bak_{BACKUP_TAG}"
SNAP_BAK = f"qm_research_candidate_snapshot_bak_{BACKUP_TAG}"
# 与 script_runner._persist_locked 写快照时同口径
CONFIDENCE_LEVEL = {"BUY": "high", "SELL": "watch", "HOLD": "medium"}


def _get_engine():
    db_url = os.getenv(
        "DATABASE_URL",
        f"postgresql://{os.getenv('DB_USER', 'quantmind')}:{os.getenv('DB_PASSWORD', 'quantmind2026')}"
        f"@{os.getenv('DB_HOST', 'db')}:{os.getenv('DB_PORT', '5432')}/{os.getenv('DB_NAME', 'quantmind')}",
    )
    if "+asyncpg" in db_url:
        db_url = db_url.replace("+asyncpg", "+psycopg2")
    if not db_url.startswith("postgresql"):
        db_url = (
            f"postgresql+psycopg2://{os.getenv('DB_USER', 'quantmind')}:"
            f"{os.getenv('DB_PASSWORD', 'quantmind2026')}@{os.getenv('DB_HOST', 'db')}:5432/quantmind"
        )
    return create_engine(db_url, pool_pre_ping=True, future=True)


def _as_jsonb(val: Any) -> dict:
    """asyncpg/psycopg2 下 JSONB 可能是 str。"""
    if isinstance(val, dict):
        return val
    if isinstance(val, str):
        try:
            out = json.loads(val)
        except json.JSONDecodeError:
            return {}
        return out if isinstance(out, dict) else {}
    return {}


def _consensus_of(row) -> int | None:
    val = _as_jsonb(row[7]).get("consensus")
    return None if val is None else int(val)


def _load_signal_runs(conn, since: str, until: str) -> dict[tuple, list]:
    rows = conn.execute(
        text("""
            SELECT run_id, tenant_id, user_id, id, symbol, fusion_score, signal_side, quality
            FROM engine_signal_scores
            WHERE trade_date BETWEEN :since AND :until
              AND model_version = 'inference_script'
            ORDER BY run_id, symbol
        """),
        {"since": since, "until": until},
    ).fetchall()
    runs: dict[tuple, list] = defaultdict(list)
    for r in rows:
        runs[(r[0], r[1], r[2])].append(r)
    return runs


def _load_snapshots(conn, since: str, until: str) -> dict[tuple, list]:
    rows = conn.execute(
        text("""
            SELECT run_id, tenant_id, user_id, id, symbol, fusion_score, signal_side,
                   confidence_level
            FROM qm_research_candidate_snapshot
            WHERE prediction_trade_date BETWEEN :since AND :until
            ORDER BY run_id, symbol
        """),
        {"since": since, "until": until},
    ).fetchall()
    runs: dict[tuple, list] = defaultdict(list)
    for r in rows:
        runs[(r[0], r[1], r[2])].append(r)
    return runs


def _run_dates(conn, since: str, until: str) -> dict[str, str]:
    """run_id → trade_date（一个 run 只落一天）。"""
    return {
        r[0]: r[1]
        for r in conn.execute(
            text("""
                SELECT DISTINCT run_id, trade_date::text FROM engine_signal_scores
                WHERE trade_date BETWEEN :since AND :until
                  AND model_version = 'inference_script'
            """),
            {"since": since, "until": until},
        )
    }


def _recompute(scores: list[float], consensus: list[int | None]) -> list[str]:
    """与生产同口径重算；置信度历史缺失 → None（闸门跳过）。"""
    return InferenceScriptRunner._resolve_signal_sides(scores, consensus, None)


def _new_sides_of_run(
    rows: list, consensus: list[int | None] | None = None
) -> list[str]:
    scores = [float(r[5]) if r[5] is not None else float("nan") for r in rows]
    if consensus is None:
        consensus = [_consensus_of(r) for r in rows]
    return _recompute(scores, consensus)


def _report_engine(conn, runs: dict[tuple, list], since: str, until: str) -> dict:
    run_dates = _run_dates(conn, since, until)
    by_date: dict[str, dict] = {}
    per_run: list[dict] = []
    for (run_id, _tid, _uid), rows in runs.items():
        new_sides = _new_sides_of_run(rows)
        old_counts: defaultdict = defaultdict(int)
        new_counts: defaultdict = defaultdict(int)
        changed = 0
        for r, new_side in zip(rows, new_sides, strict=True):
            old_counts[r[6]] += 1
            new_counts[new_side] += 1
            changed += int(r[6] != new_side)
        date = run_dates.get(run_id, "?")
        bucket = by_date.setdefault(
            date,
            {"rows": 0, "old": defaultdict(int), "new": defaultdict(int), "changed": 0},
        )
        bucket["rows"] += len(rows)
        bucket["changed"] += changed
        for k, v in old_counts.items():
            bucket["old"][k] += v
        for k, v in new_counts.items():
            bucket["new"][k] += v
        per_run.append(
            {
                "run_id": run_id,
                "date": date,
                "n": len(rows),
                "changed": changed,
                "old": dict(old_counts),
                "new": dict(new_counts),
            }
        )
    return {"by_date": by_date, "per_run": per_run}


def _snapshot_updates(rows: list, consensus: list[int | None] | None) -> list[dict]:
    """快照行的目标值（side + confidence_level），只返回需要改的。"""
    new_sides = _new_sides_of_run(rows, consensus)
    return [
        {"id": r[3], "side": ns, "lvl": CONFIDENCE_LEVEL.get(ns, r[7] or "medium")}
        for r, ns in zip(rows, new_sides, strict=True)
        if r[6] != ns or (r[7] or "medium") != CONFIDENCE_LEVEL.get(ns, "medium")
    ]


def _iter_snapshot_plans(snapshots: dict[tuple, list], engine_runs: dict[tuple, list]):
    """产出 (key, rows, updates)。老 run 的引擎行已被后续覆盖删除 → 共识未知 → 闸门跳过。"""
    for key, rows in snapshots.items():
        eng = engine_runs.get(key)
        consensus = [_consensus_of(r) for r in eng] if eng else None
        yield key, rows, _snapshot_updates(rows, consensus)


def _report_snapshots(
    snapshots: dict[tuple, list], engine_runs: dict[tuple, list]
) -> dict:
    before: defaultdict = defaultdict(int)
    after: defaultdict = defaultdict(int)
    changed = 0
    for _key, rows, updates in _iter_snapshot_plans(snapshots, engine_runs):
        changed += len(updates)
        new_level = {u["id"]: u["lvl"] for u in updates}
        for r in rows:
            before[r[7] or "?"] += 1
            after[new_level.get(r[3], r[7] or "medium")] += 1
    return {
        "rows": sum(len(v) for v in snapshots.values()),
        "changed": changed,
        "level_before": dict(before),
        "level_after": dict(after),
    }


def _print_report(engine_rep: dict, snap_rep: dict, since: str, until: str) -> None:
    print(f"\n=== signal_side 回填 dry-run  {since} ~ {until} ===")
    total_rows = sum(p["n"] for p in engine_rep["per_run"])
    print(
        f"引擎行 {total_rows} / {len(engine_rep['per_run'])} runs；"
        f"快照行 {snap_rep['rows']}"
    )
    print(f"\n{'日期':<12}{'行数':>8}{'改前 B/S/H':>20}{'改后 B/S/H':>20}{'变更':>8}")
    for date in sorted(engine_rep["by_date"]):
        b = engine_rep["by_date"][date]
        o, n = b["old"], b["new"]
        print(
            f"{date:<12}{b['rows']:>8}"
            f"{o.get('BUY', 0):>7}/{o.get('SELL', 0):>5}/{o.get('HOLD', 0):>6}"
            f"{n.get('BUY', 0):>7}/{n.get('SELL', 0):>5}/{n.get('HOLD', 0):>6}"
            f"{b['changed']:>8}"
        )
    tot_old: defaultdict = defaultdict(int)
    tot_new: defaultdict = defaultdict(int)
    for p in engine_rep["per_run"]:
        for k, v in p["old"].items():
            tot_old[k] += v
        for k, v in p["new"].items():
            tot_new[k] += v
    print(
        f"\n合计: 改前 BUY/SELL/HOLD = {tot_old.get('BUY', 0)}/{tot_old.get('SELL', 0)}"
        f"/{tot_old.get('HOLD', 0)} → 改后 {tot_new.get('BUY', 0)}/{tot_new.get('SELL', 0)}"
        f"/{tot_new.get('HOLD', 0)}"
    )
    print(
        f"快照: 变更 {snap_rep['changed']} 行；confidence_level "
        f"{snap_rep['level_before']} → {snap_rep['level_after']}"
    )
    print("\n变更最多的 run（前 10）:")
    for p in sorted(engine_rep["per_run"], key=lambda x: -x["changed"])[:10]:
        print(
            f"  {p['date']} {p['run_id']:<28} n={p['n']:<6} 变更={p['changed']:<6}"
            f" 改后 B/S/H={p['new'].get('BUY', 0)}/{p['new'].get('SELL', 0)}"
            f"/{p['new'].get('HOLD', 0)}"
        )


def _create_backups(conn, since: str, until: str) -> None:
    conn.execute(
        text(f"""
            CREATE TABLE IF NOT EXISTS {SS_BAK} AS
            SELECT id, signal_side, quality FROM engine_signal_scores
            WHERE trade_date BETWEEN :since AND :until AND model_version='inference_script'
        """),
        {"since": since, "until": until},
    )
    conn.execute(
        text(f"""
            CREATE TABLE IF NOT EXISTS {SNAP_BAK} AS
            SELECT id, signal_side, confidence_level FROM qm_research_candidate_snapshot
            WHERE prediction_trade_date BETWEEN :since AND :until
        """),
        {"since": since, "until": until},
    )
    conn.commit()
    n1 = conn.execute(text(f"SELECT count(*) FROM {SS_BAK}")).scalar()
    n2 = conn.execute(text(f"SELECT count(*) FROM {SNAP_BAK}")).scalar()
    print(f"备份: {SS_BAK}={n1} 行, {SNAP_BAK}={n2} 行")


def _apply(engine, runs: dict[tuple, list], snapshots: dict[tuple, list]) -> dict:
    t0 = time.time()
    summary: dict[str, Any] = {
        "runs": 0,
        "engine_rows": 0,
        "snap_rows": 0,
        "orphan_snap_runs": 0,
        "position_runs": 0,
        "elapsed_s": 0.0,
    }
    snap_plans = {
        key: updates for key, _rows, updates in _iter_snapshot_plans(snapshots, runs)
    }
    for (run_id, tid, uid), rows in runs.items():
        consensus = [_consensus_of(r) for r in rows]
        new_sides = _new_sides_of_run(rows, consensus)
        updates = [
            {"id": r[3], "side": ns}
            for r, ns in zip(rows, new_sides, strict=True)
            if r[6] != ns
        ]
        s_updates = snap_plans.get((run_id, tid, uid), [])
        with engine.begin() as conn:
            if updates:
                conn.execute(
                    text(
                        "UPDATE engine_signal_scores SET signal_side=:side WHERE id=:id"
                    ),
                    updates,
                )
                summary["engine_rows"] += len(updates)
            if s_updates:
                conn.execute(
                    text("""
                        UPDATE qm_research_candidate_snapshot
                        SET signal_side=:side, confidence_level=:lvl WHERE id=:id
                    """),
                    s_updates,
                )
                summary["snap_rows"] += len(s_updates)
            # position 只随 signal_side 变（分位/基准只依赖 fusion_score）
            if updates:
                preds = compute_position_scores(
                    [r[4] for r in rows], [float(r[5]) for r in rows], new_sides
                )
                batch_update_quality(conn, run_id, tid, uid, preds)
                summary["position_runs"] += 1
        summary["runs"] += 1
        if summary["runs"] % 20 == 0:
            print(
                f"  ...已处理 {summary['runs']}/{len(runs)} runs"
                f"（{time.time() - t0:.0f}s）",
                flush=True,
            )
    # 仅存在于快照表的老 run：引擎行已被后续覆盖删除，只补快照
    engine_keys = set(runs)
    for key, s_updates in snap_plans.items():
        if key in engine_keys or not s_updates:
            continue
        with engine.begin() as conn:
            conn.execute(
                text("""
                    UPDATE qm_research_candidate_snapshot
                    SET signal_side=:side, confidence_level=:lvl WHERE id=:id
                """),
                s_updates,
            )
        summary["snap_rows"] += len(s_updates)
        summary["orphan_snap_runs"] += 1
    summary["elapsed_s"] = round(time.time() - t0, 1)
    return summary


def _rollback(conn, since: str, until: str) -> None:
    r1 = conn.execute(
        text(f"""
            UPDATE engine_signal_scores t SET signal_side=b.signal_side, quality=b.quality
            FROM {SS_BAK} b WHERE t.id=b.id
        """)
    )
    r2 = conn.execute(
        text(f"""
            UPDATE qm_research_candidate_snapshot t
            SET signal_side=b.signal_side, confidence_level=b.confidence_level
            FROM {SNAP_BAK} b WHERE t.id=b.id
        """)
    )
    conn.commit()
    print(
        f"已还原: engine_signal_scores={r1.rowcount} 行, "
        f"qm_research_candidate_snapshot={r2.rowcount} 行"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="回填历史 signal_side（修复后闸门口径）")
    ap.add_argument("--dry-run", action="store_true", help="只报表不写库")
    ap.add_argument("--apply", action="store_true", help="备份 + 写库（幂等）")
    ap.add_argument("--rollback", action="store_true", help="从备份表还原")
    ap.add_argument("--since", default=DEFAULT_SINCE)
    ap.add_argument("--until", default=DEFAULT_UNTIL)
    args = ap.parse_args()
    if not (args.dry_run or args.apply or args.rollback):
        ap.error("需要 --dry-run / --apply / --rollback 之一")

    engine = _get_engine()
    with engine.connect() as conn:
        if args.rollback:
            _rollback(conn, args.since, args.until)
            return 0
        runs = _load_signal_runs(conn, args.since, args.until)
        snapshots = _load_snapshots(conn, args.since, args.until)
        engine_rep = _report_engine(conn, runs, args.since, args.until)
        snap_rep = _report_snapshots(snapshots, runs)
        _print_report(engine_rep, snap_rep, args.since, args.until)

        if args.dry_run:
            print("\n[dry-run] 未写库。确认无误后执行 --apply。")
            return 0

        print("\n=== 写库 ===")
        _create_backups(conn, args.since, args.until)
        summary = _apply(engine, runs, snapshots)
        print("apply 完成:", json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
