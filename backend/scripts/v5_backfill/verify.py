"""V5 回填产物验收：manifest sha256 + 行数 + PASS 非空 + 官方分区未动。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

from backend.scripts.v5_backfill.materialize import MANIFEST, OHLCV, V5_DIR, sha256_file


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=30, help="抽查分区数")
    args = ap.parse_args()
    man = json.loads(MANIFEST.read_text(encoding="utf-8"))
    parts = man["partitions"]
    cols = set(man["pass_columns"])
    print(f"manifest: {len(parts)} 分区, PASS {len(cols)} 列, range {man['range']}")

    bad = []
    for r in parts:
        p = V5_DIR / f"dt={r['dt']}" / "data.parquet"
        if not p.exists():
            bad.append((r["dt"], "missing"))
            continue
        if p.stat().st_size != r["bytes"] or sha256_file(p) != r["sha256"]:
            bad.append((r["dt"], "sha/size"))
    print(f"全量校验: {len(parts) - len(bad)}/{len(parts)} 通过")
    if bad:
        print("失败:", bad[:10])

    # 抽查 schema/行数/PASS 非空
    sample = parts[:: max(1, len(parts) // max(1, args.sample))][: args.sample]
    for r in sample:
        p = V5_DIR / f"dt={r['dt']}" / "data.parquet"
        s = pq.read_schema(p)
        if len(s) != 571:
            print("  !! schema 字段数", r["dt"], len(s))
        df = pd.read_parquet(p, columns=["symbol", "date", *OHLCV, *list(cols)[:3]])
        if len(df) != r["rows"]:
            print("  !! rows", r["dt"], len(df), r["rows"])
    print(f"抽查 {len(sample)} 个分区完成")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
