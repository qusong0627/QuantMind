#!/usr/bin/env python3
"""一次训练 → 一次成长：把选特征/训练 job/OOS/经验坑串成幂等收口命令。

典型用法（宿主机；OOS 已由容器算好）：
    python backend/scripts/stock_classifier/daily_growth.py \
        --selection /data/stock_classifier/feature_selection/daily_feature_selection_2016_2024.json \
        --top 100 \
        --jobs /data/stock_classifier/feature_selection/model_jobs.json \
        --oos /data/stock_classifier/feature_selection/oos_daily_2025_2026.json \
        --registry /data/stock_classifier/feature_selection/registry \
        --out-md docs/特征选择台账_20261110.md

容器内可直接 --compute-oos 现算（慢，约 5–10 分钟）。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from backend.scripts.stock_classifier.selection_registry import (
    PROTOCOL_FILE,
    SelectionRegistry,
    build_lessons,
    render_audit_md,
)


def _load(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser(description="训练后收口：台账 + OOS + 经验坑")
    ap.add_argument("--selection", required=True)
    ap.add_argument("--top", type=int, default=100)
    ap.add_argument("--protocol", default=str(PROTOCOL_FILE))
    ap.add_argument("--registry", required=True)
    ap.add_argument("--jobs", default="", help="submit 脚本产出的 model_jobs.json")
    ap.add_argument("--data-issues", default="")
    ap.add_argument("--oos", default="", help="oos_ic.py 产出的 JSON")
    ap.add_argument("--compute-oos", action="store_true")
    ap.add_argument("--force-oos", action="store_true")
    ap.add_argument("--start", default="2025-01-01")
    ap.add_argument("--end", default="2026-11-05")
    ap.add_argument("--step", type=int, default=5)
    ap.add_argument("--out-md", default="")
    args = ap.parse_args()

    selection = _load(args.selection)
    protocol = _load(args.protocol)
    data_issues = _load(args.data_issues) if args.data_issues else None
    jobs = _load(args.jobs) if args.jobs else None

    store = SelectionRegistry(args.registry)
    record = store.upsert(
        selection,
        top=args.top,
        protocol_snapshot=protocol,
        model_jobs=jobs,
        data_issues=data_issues,
    )
    run_id = record["run_id"]
    has_oos = any((f.get("oos") or {}).get("valid") for f in record["features"])

    if (args.oos or args.compute_oos) and (args.force_oos or not has_oos):
        if args.oos:
            oos = _load(args.oos)
        else:
            # 仅容器内可用：延迟导入，宿主机不加载重依赖
            from backend.scripts.stock_classifier.oos_ic import compute_oos

            oos = compute_oos(
                args.selection, args.top, args.start, args.end, args.step
            )
        record = store.attach_oos(run_id, oos)
        has_oos = True

    lessons = build_lessons(record, window="valid")
    if lessons:
        store.add_lessons(run_id, lessons)
    audit = record.get("audit") or {}
    valid = audit.get("valid") or {}
    test = audit.get("test") or {}

    if args.out_md:
        render_audit_md(record, Path(args.out_md))
    print(
        f"run={run_id} features={len(record['features'])} "
        f"jobs={sorted((record.get('model_jobs') or {}).keys())} "
        f"oos={'yes' if has_oos else 'pending'} "
        f"valid_n={valid.get('n', 0)} valid_sign_hit={valid.get('sign_hit_rate')} "
        f"test_n={test.get('n', 0)}"
    )
    print(
        f"lessons={len(lessons)} "
        f"open={sum(1 for x in lessons if x.get('status') == 'open')} "
        f"confirmed={sum(1 for x in lessons if x.get('status') == 'confirmed')}"
    )
    if args.out_md:
        print(f"wrote {args.out_md}")
    if not has_oos:
        print("OOS 未就绪：训练完成后用 --oos 或 --compute-oos 再跑一次（幂等）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
