#!/usr/bin/env python3
"""H1：冻结 v1 的 100 特征与已发布目录版本，只换标签为 size_excess_rank 重训。

不重新注册/发布目录（避免触发目录 clone 的 mapping_id 长度 bug），
直接复用 v1 model_jobs.json 里的 features + versions。

用法（容器内，API 8000 可达）：
    python backend/scripts/stock_classifier/submit_h1_size_neutral.py \
        --v1-jobs /data/stock_classifier/feature_selection/model_jobs.json \
        --out /data/stock_classifier/feature_selection/model_jobs_h1.json
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from backend.scripts.stock_classifier.submit_daily_model_jobs import (
    _session,
    _submit,
    _wait,
)

ANCHOR = "l1_factors"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--v1-jobs", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--label-transform", default="size_excess_rank")
    ap.add_argument("--run-tag", default="H1 size_neutral")
    args = ap.parse_args()
    secret = os.environ.get("INTERNAL_CALL_SECRET", "").strip()
    if not secret:
        raise SystemExit("缺少 INTERNAL_CALL_SECRET")
    v1 = json.loads(Path(args.v1_jobs).read_text(encoding="utf-8"))
    features = list(v1["features"])
    versions = {str(k): str(v) for k, v in (v1["versions"] or {}).items()}
    if ANCHOR not in versions:
        raise SystemExit(f"v1 jobs 缺少锚库版本 {ANCHOR}")
    print(f"[h1] features={len(features)} versions={len(versions)}")
    s = _session(secret)
    jobs: dict[str, dict] = {}
    for horizon in (3, 5):
        name = f"CN日频 H1 T+{horizon} · size_excess_rank · {args.run_tag}"
        run_id = _submit(
            s,
            horizon=horizon,
            features=features,
            versions=versions,
            anchor=ANCHOR,
            name=name,
            # API 侧校验只认 rank/zscore/raw/top_tail；用 label_formula 兼容通道
            # 把 size_excess_rank 传给训练容器（train.py 会覆盖 label config）。
            label_transform="rank",
            label_formula=f"label_transform={args.label_transform}",
        )
        jobs[f"t{horizon}"] = {"run_id": run_id}
        result = _wait(s, run_id)
        jobs[f"t{horizon}"]["status"] = result.get("status")
        jobs[f"t{horizon}"]["result"] = result
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(
            json.dumps(
                {
                    "features": features,
                    "versions": versions,
                    "label_transform": args.label_transform,
                    "jobs": jobs,
                },
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )
        print(f"[h1] t{horizon} status={result.get('status')}")
        time.sleep(2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
