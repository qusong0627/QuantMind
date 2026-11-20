#!/usr/bin/env python3
"""严格评估 CLI（WP2）：对一个训练 run 产出 strict_report.json。

用法（应用容器内）：
    python backend/scripts/strict_eval_run.py --run-id train_20261119114538_a466cd57
    python backend/scripts/strict_eval_run.py --run-id ... --entry close --out /tmp/r.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.services.engine.training.strict_eval import (  # noqa: E402
    DATA_ROOT,
    evaluate_run,
    dump_report,
)

def _segments_from_config(run_id: str) -> dict[str, tuple[str, str]] | None:
    cfg_path = DATA_ROOT / "training_jobs" / run_id / "config.yaml"
    if not cfg_path.exists():
        return None
    try:
        import yaml

        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        split = cfg.get("split") or {}
        out: dict[str, tuple[str, str]] = {}
        for key in ("valid", "test"):
            pair = split.get(key)
            if isinstance(pair, list) and len(pair) == 2 and all(pair):
                out[key] = (str(pair[0]).replace("-", ""), str(pair[1]).replace("-", ""))
        return out or None
    except Exception:  # noqa: BLE001
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description="训练 run 严格评估")
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--pred-path", default=None)
    ap.add_argument("--market", default="CN")
    ap.add_argument("--horizon", type=int, default=10)
    ap.add_argument("--entry", choices=["open", "close"], default="open")
    ap.add_argument("--start", default=None, help="覆盖评估起点 YYYYMMDD")
    ap.add_argument("--end", default=None)
    ap.add_argument("--top-ns", default="20,50,100,200,500")
    ap.add_argument("--model-id", default=None)
    ap.add_argument("--store", action="store_true", help="写入 qm_training_strict_reports")
    ap.add_argument("--out", default=None)
    ap.add_argument("--write-standard", action="store_true", help="同时写 <run>/strict_report.json")
    args = ap.parse_args()

    segments = _segments_from_config(args.run_id)
    if args.start and args.end:
        segments = {"custom": (args.start, args.end)}
    if not segments:
        segments = None

    top_ns = tuple(int(x) for x in args.top_ns.split(",") if x.strip())
    report = evaluate_run(
        run_id=args.run_id,
        pred_path=args.pred_path,
        market=args.market,
        horizon=args.horizon,
        segments=segments,
        top_ns=top_ns,
        entry_price=args.entry,
    )

    out = Path(args.out) if args.out else (DATA_ROOT / "tmp" / "strict_reports" / f"{args.run_id}.json")
    dump_report(report, out)
    if args.write_standard:
        dump_report(
            report,
            DATA_ROOT / "training_jobs" / args.run_id / "strict_report.json",
        )
    if args.store:
        import asyncio

        from backend.services.engine.training.strict_eval_service import upsert_report

        asyncio.run(
            upsert_report(
                run_id=args.run_id,
                model_id=args.model_id,
                report=report,
            )
        )

    for seg, payload in report["results"].items():
        print(f"[{seg}]")
        for top, row in payload.items():
            if not row.get("batches"):
                print(f"  {top}: no batches")
                continue
            c25 = row.get("cost_25bp") or {}
            print(
                f"  {top}: batches={row['batches']} total25={c25.get('total'):+.4f} "
                f"mdd={c25.get('mdd'):+.4f} excess_hs300={c25.get('excess_hs300'):+.4f} "
                f"hac_t={c25.get('hac_t'):.2f} psr={c25.get('psr'):.2f}"
            )
    print(f"report -> {out}")
    print(json.dumps({k: report["segments"][k] for k in report["segments"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
