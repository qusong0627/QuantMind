#!/usr/bin/env python3
"""WP7 数据审计 CLI：对训练 run 跑审计并可落库。

用法（应用容器内）：
    python backend/scripts/training_audit_run.py --run-id train_xxx [--model-id mdl_xxx] [--store]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.services.engine.training.pit_audit_service import build_audit  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="训练 run 数据审计")
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--model-id", default=None)
    ap.add_argument("--store", action="store_true")
    args = ap.parse_args()

    report = build_audit(args.run_id)
    print(json.dumps(report, ensure_ascii=False, indent=1))
    if args.store:
        import asyncio

        from backend.services.engine.training.pit_audit_service import upsert_audit

        asyncio.run(
            upsert_audit(run_id=args.run_id, model_id=args.model_id, report=report)
        )
        print(f"stored -> qm_training_audit_reports[{args.run_id}]")
    return 0 if not report.get("blocked") else 2


if __name__ == "__main__":
    raise SystemExit(main())
