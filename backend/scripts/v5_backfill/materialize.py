"""V5 回填物化：把对账通过（PASS）的列写入 2010-2017 分区。

纪律：
- 只写 2010-01-04 ~ 2017-12-29；已存在的分区一律跳过（绝不改 2018+ 官方分区）；
- 输出 schema = 官方 V5 571 列（列名/类型/顺序逐字段一致），非 PASS 列写 null；
- PASS 列 = 对账报告中 2018/2020 两期 status=PASS 的交集；
- 截面 1%/99% 缩尾（与官方 V5 同规则；主体逐值对账通过，缩尾边界差异见报告）；
- 逐分区原子写（tmp+rename），manifest 记录公式版本、PASS 列、行数、sha256。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from backend.scripts.v5_backfill.extra_formulas import compute_extra
from backend.scripts.v5_backfill.formulas import compute_panel, winsorize_cross_section
from backend.scripts.v5_backfill.panel import (QDB, load_bars, load_index_return_dates,
                                               trading_dates)

V5_DIR = QDB / "6_ml_datasets" / "factor_dictionary"
SCHEMA_PARTITION = V5_DIR / "dt=20180102" / "data.parquet"
START, END = "20100104", "20171229"
MANIFEST = V5_DIR / "_backfill_2010_2017_manifest.json"
OHLCV = ["open", "high", "low", "close", "volume", "amount"]


def pass_columns(report_paths: list[str]) -> list[str]:
    """多份对账报告：各自取各期 PASS 交集，再跨报告取并集。"""
    out: set[str] = set()
    for rp in report_paths:
        report = json.load(open(rp, encoding="utf-8"))
        sets = [{c["column"] for c in d["columns"] if c.get("status") == "PASS"} for d in report]
        if sets:
            out |= set.intersection(*sets)
    return sorted(out)


def load_ohlcv(dates: list[str]) -> pd.DataFrame:
    frames = []
    for ds in ("daily_backward", "daily_unadjusted"):
        fr = []
        for d in dates:
            f = QDB / "1_kline_data" / ds / f"dt={d}" / "data.parquet"
            x = pd.read_parquet(f, columns=["symbol", *OHLCV])
            x["dt"] = d
            fr.append(x)
        frames.append(pd.concat(fr, ignore_index=True))
    # daily_backward 已带量额；仅用其一份，保留 unadjusted 读取用于缺失兜底
    bwd = frames[0]
    raw = frames[1]
    out = bwd.merge(raw[["symbol", "dt", "volume", "amount"]], on=["symbol", "dt"],
                    how="left", suffixes=("", "_raw"))
    for c in ("volume", "amount"):
        out[c] = out[c].where(out[c].notna(), out[f"{c}_raw"])
    return out.drop(columns=[c for c in out.columns if c.endswith("_raw")])


def build_partition(schema, symbols, date_ts, bars_cross, cross, cols):
    n = len(symbols)
    arrays = []
    for field in schema:
        name = field.name
        typ = field.type
        if name == "symbol":
            arrays.append(pa.array(symbols, type=typ))
        elif name == "date":
            arrays.append(pa.array([date_ts] * n, type=typ))
        elif name in OHLCV:
            arr = bars_cross.reindex(symbols)[name].to_numpy(dtype="float64")
            arrays.append(pa.array(arr, type=typ))
        elif name in cols and name in cross.columns:
            arr = cross.reindex(symbols)[name].to_numpy(dtype="float64")
            arrays.append(pa.array(arr, type=typ))
        else:
            arrays.append(pa.nulls(n, type=typ))
    return pa.Table.from_arrays(arrays, schema=schema)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report",
                    default="data/tmp/qc_2010_2015/v5_reconcile.json,"
                            "data/tmp/qc_2010_2015/v5_reconcile_extra.json",
                    help="逗号分隔的多份对账报告（PASS 取并集）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--overwrite", action="store_true",
                    help="重写已存在的 2010-2017 分区（用于追加新 PASS 列；绝不碰 2018+）")
    ap.add_argument("--since", default=START)
    args = ap.parse_args()

    reports = [x.strip() for x in args.report.split(",") if x.strip()]
    cols = pass_columns(reports)
    if not cols:
        raise SystemExit("对账报告无 PASS 列")
    print(f"PASS 列 {len(cols)}: {cols}")

    schema = pq.read_schema(SCHEMA_PARTITION)
    dates = [d for d in trading_dates(end=END) if d >= args.since]
    if not dates:
        raise SystemExit("无 2010-2017 交易日")
    print(f"日期 {dates[0]} ~ {dates[-1]} 共 {len(dates)}")

    # 预热：从 2010-01-04 起算（更早无数据）
    warm_start = "20100104"
    bars = load_bars(dates[-1], window=len(trading_dates(end=dates[-1]) ))
    bars = bars[bars["dt"] >= warm_start]
    print(f"bars {len(bars):,} 行")
    panel = compute_panel(bars, None)
    mret = load_index_return_dates(sorted(bars["dt"].unique()))
    extra = compute_extra(bars, mret)
    panel = panel.merge(extra, on=["symbol", "dt"], how="left")
    print(f"panel {len(panel):,} 行 × {len(cols)} 列（含第二批）")
    panel = panel.sort_values(["dt", "symbol"], kind="stable")

    ohlcv = load_ohlcv(dates).set_index(["dt", "symbol"])

    written = skipped = 0
    manifest_rows = []
    for d in dates:
        out_dir = V5_DIR / f"dt={d}"
        out_path = out_dir / "data.parquet"
        if out_path.exists() and not args.overwrite:
            skipped += 1
            continue
        cross = panel[panel["dt"] == d].set_index("symbol")[cols]
        if cross.empty:
            skipped += 1
            continue
        cross = winsorize_cross_section(cross)
        symbols = sorted(cross.index.tolist())
        bars_cross = ohlcv.loc[d] if d in ohlcv.index.get_level_values(0) else pd.DataFrame()
        date_ts = pd.Timestamp(f"{d[:4]}-{d[4:6]}-{d[6:]}")
        table = build_partition(schema, symbols, date_ts, bars_cross, cross, cols)
        if args.dry_run:
            written += 1
            continue
        out_dir.mkdir(parents=True, exist_ok=True)
        tmp = out_dir / "data.parquet.tmp"
        pq.write_table(table, tmp, compression="zstd")
        os.replace(tmp, out_path)
        manifest_rows.append({
            "dt": d, "rows": len(symbols), "sha256": sha256_file(out_path),
            "bytes": out_path.stat().st_size,
        })
        written += 1
        if written % 100 == 0:
            print(f"  {d}: 已写 {written}")

    if not args.dry_run:
        prev = {}
        if MANIFEST.exists():
            try:
                prev = json.loads(MANIFEST.read_text(encoding="utf-8"))
            except ValueError:
                prev = {}
        all_rows = {r["dt"]: r for r in prev.get("partitions", [])}
        all_rows.update({r["dt"]: r for r in manifest_rows})
        MANIFEST.write_text(json.dumps({
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source": {
                "formula_doc": "https://www.quantdb.cn/docs/factors-fields.html",
                "bars": "daily_forward(qfq) for factors; daily_backward for OHLCV",
                "reconcile_reports": reports,
            },
            "pass_columns": cols,
            "non_pass_policy": "null（官方 schema 571 列保持不变）",
            "range": [dates[0], dates[-1]],
            "partitions": [all_rows[k] for k in sorted(all_rows)],
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nmanifest: {MANIFEST}")
    print(f"\n完成: written={written} skipped={skipped} dry_run={args.dry_run}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
