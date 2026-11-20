#!/usr/bin/env python3
"""WP4 多种子顺序派发 + 离散度报告。

单飞锁（44G 宿主内存）决定了种子必须**串行**：本脚本逐个子任务提交、等终态，
再算每个种子的严格评估，最后输出离散度。不并发、不抢锁。

用法（应用容器内）：
    # 以某个已完成的 run 的提交载荷为模板，跑 3 个 seed
    python backend/scripts/multi_seed_dispatch.py \
        --base-run train_20261119114538_a466cd57 \
        --seeds 42,7,2024 [--dry-run] [--submit]

    # 只汇总已有 run（按 run_id 列表）的离散度
    python backend/scripts/multi_seed_dispatch.py --summarize r1,r2,r3
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.shared.database_manager_v2 import get_session  # noqa: E402

API_BASE = os.getenv("QM_API_BASE", "http://quantmind:8000/api/v1")
SUBMIT_URL = f"{API_BASE}/models/run-training"
USER_ID = os.getenv("QM_INTERNAL_USER_ID", "10000001")
TENANT = os.getenv("QM_INTERNAL_TENANT", "default")
TERMINAL = {"completed", "failed", "cancelled"}


async def _load_base_payload(base_run: str) -> dict:
    async with get_session() as session:
        row = (
            await session.execute(
                text("SELECT request_payload FROM admin_training_jobs WHERE id = :id"),
                {"id": base_run},
            )
        ).scalar_one_or_none()
    if not row:
        raise SystemExit(f"base run not found: {base_run}")
    return dict(row)


async def _run_status(run_id: str) -> str:
    async with get_session() as session:
        row = (
            await session.execute(
                text("SELECT status FROM admin_training_jobs WHERE id = :id"),
                {"id": run_id},
            )
        ).scalar_one_or_none()
    return str(row or "missing")


def _seed_payload(base: dict, seed: int, base_run: str) -> dict:
    payload = copy.deepcopy(base)
    for key in ("_valid_features", "_missing_features", "lineage", "missing_features"):
        payload.pop(key, None)
    params = dict(payload.get("lgb_params") or {})
    params["seed"] = seed
    params["bagging_seed"] = seed
    params["feature_fraction_seed"] = seed
    payload["lgb_params"] = params
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    payload["job_name"] = f"seed{seed}_{base_run}_{stamp}"
    payload["display_name"] = f"{base.get('display_name') or base_run} · seed{seed}"
    payload["deploy_to_production"] = False
    payload["node_id"] = payload.get("node_id") or "local"
    payload["generated_at"] = datetime.utcnow().isoformat() + "Z"
    return payload


def _submit(payload: dict) -> str:
    secret = os.getenv("INTERNAL_CALL_SECRET", "").strip()
    if not secret:
        raise SystemExit("缺少 env INTERNAL_CALL_SECRET（fail-closed）")
    headers = {
        "X-Internal-Call": secret,
        "X-User-Id": USER_ID,
        "X-Tenant-Id": TENANT,
    }
    with httpx.Client(timeout=180.0) as client:
        resp = client.post(SUBMIT_URL, json=payload, headers=headers)
    if resp.status_code >= 300:
        raise SystemExit(f"submit failed {resp.status_code}: {resp.text[:800]}")
    data = resp.json()
    return str(data.get("runId") or "")


async def _wait(run_id: str, poll_seconds: float = 30.0, timeout_hours: float = 12.0) -> str:
    deadline = time.time() + timeout_hours * 3600
    while time.time() < deadline:
        status = await _run_status(run_id)
        if status in TERMINAL:
            return status
        await asyncio.sleep(poll_seconds)
    return "timeout"


async def _summarize(run_ids: list[str]) -> None:
    from backend.services.engine.training.strict_eval_service import fetch_report
    from backend.services.engine.training.strict_eval import evaluate_run

    rows = []
    for run_id in run_ids:
        stored = await fetch_report(run_id)
        report = (stored or {}).get("report") if stored else None
        if not report:
            try:
                report = await asyncio.to_thread(evaluate_run, run_id=run_id)
            except Exception as exc:  # noqa: BLE001
                rows.append({"run_id": run_id, "error": str(exc)})
                continue
        test = (report.get("results") or {}).get("test") or {}
        top20 = test.get("top20") or {}
        c25 = top20.get("cost_25bp") or {}
        rows.append(
            {
                "run_id": run_id,
                "test_top20_total_25bp": c25.get("total"),
                "test_top20_mdd": c25.get("mdd"),
                "test_top20_hac_t": c25.get("hac_t"),
                "test_top20_psr": c25.get("psr"),
            }
        )
    values = [r["test_top20_total_25bp"] for r in rows if isinstance(r.get("test_top20_total_25bp"), (int, float))]
    summary = {"runs": rows, "dispersion": {}}
    if values:
        import statistics

        mean = statistics.fmean(values)
        std = statistics.pstdev(values)
        summary["dispersion"] = {
            "n": len(values),
            "mean": round(mean, 4),
            "std": round(std, 4),
            "min": round(min(values), 4),
            "max": round(max(values), 4),
            "std_over_mean": round(std / abs(mean), 4) if mean else None,
        }
    print(json.dumps(summary, ensure_ascii=False, indent=1))


def main() -> int:
    ap = argparse.ArgumentParser(description="多种子顺序派发 / 离散度")
    ap.add_argument("--base-run", default=None)
    ap.add_argument("--seeds", default="42,7,2024")
    ap.add_argument("--summarize", default=None, help="逗号分隔 run_id 列表")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--submit", action="store_true")
    ap.add_argument("--poll-seconds", type=float, default=30.0)
    args = ap.parse_args()

    if args.summarize:
        asyncio.run(_summarize([x.strip() for x in args.summarize.split(",") if x.strip()]))
        return 0
    if not args.base_run:
        raise SystemExit("需要 --base-run 或 --summarize")

    base = asyncio.run(_load_base_payload(args.base_run))
    seeds = [int(x) for x in args.seeds.split(",") if x.strip()]
    payloads = [_seed_payload(base, seed, args.base_run) for seed in seeds]
    if args.dry_run or not args.submit:
        print(
            json.dumps(
                {
                    "base_run": args.base_run,
                    "seeds": seeds,
                    "display_names": [p["display_name"] for p in payloads],
                    "seed_params": [p["lgb_params"] for p in payloads],
                    "note": "未提交；加 --submit 才逐个提交并等待终态",
                },
                ensure_ascii=False,
                indent=1,
            )
        )
        return 0

    finished: list[str] = []
    for seed, payload in zip(seeds, payloads):
        run_id = _submit(payload)
        print(f"[seed {seed}] submitted {run_id}", flush=True)
        status = asyncio.run(_wait(run_id, args.poll_seconds))
        print(f"[seed {seed}] {run_id} -> {status}", flush=True)
        if status == "completed":
            finished.append(run_id)
    if finished:
        asyncio.run(_summarize(finished))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
