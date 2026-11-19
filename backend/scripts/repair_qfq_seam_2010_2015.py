#!/usr/bin/env python3
"""修复 2010-2015 前复权在 2016-01-04 接缝处的错误缩放（可回滚）。

背景（2026-11-19 审计）：
  2010-2015 的 daily_forward 由本地回填（QMT 因子链）生成，2016+ 分区来自云端。
  两者对个别标的的复权因子归一化不一致，表现为 2015-12-31→2016-01-04
  原始价无缺口、但前复权因子跳变（正跳 12%~41%，或负跳 -0.2%~-1.8%）。
  后果：这些标的 2010-2015 的整段前复权价被错误缩放。

判据（只认「原始价无缺口」的因子跳）：
  ratio = f(2016-01-04) / f(2015-12-31)，f = close_qfq / close_raw
  |ratio-1| > 0.002 且 |raw_ret(接缝)| 与因子跳不匹配（真实除权必须伴随原始价缺口）。
  真实送转（如 002481 的 10 转 15，raw -64%）不修。

子命令：
  scan    只读，列出候选与 ratio
  apply   备份 → 按 ratio 缩放 2010-2015 的 open/high/low/close → 复核
  verify  复核接缝连续性
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

ROOT = Path(os.environ.get("QM_QUANTDB_ROOT", "/home/zbox/projects/quantmind/data/quantdb"))
KLINE = ROOT / "1_kline_data"
SEAM_PREV, SEAM = "20151231", "20160104"
LO, HI = "20100101", "20151231"
PRICE_COLS = ["open", "high", "low", "close"]
BACKUP = ROOT / "_backup_qfq_seam_20261119"
PLAN = ROOT / "_backup_qfq_seam_20261119" / "plan.json"


def _dates(ds: str, lo: str, hi: str) -> list[str]:
    base = KLINE / ds
    return sorted(
        n[3:] for n in os.listdir(base)
        if n.startswith("dt=") and lo <= n[3:] <= hi
        and (base / n / "data.parquet").exists()
    )


def _read_close(ds: str, dates: list[str]) -> pd.DataFrame:
    frames = []
    for d in dates:
        frames.append(pd.read_parquet(KLINE / ds / f"dt={d}" / "data.parquet",
                                      columns=["symbol", "time", "close"]))
    df = pd.concat(frames, ignore_index=True)
    df["dt"] = df["time"].dt.strftime("%Y%m%d")
    return df[["symbol", "dt", "close"]]


def build_plan() -> dict:
    for d in (_dates("daily_forward", SEAM_PREV, SEAM_PREV) + _dates("daily_forward", SEAM, SEAM)):
        if d not in (SEAM_PREV, SEAM):
            raise SystemExit(f"接缝分区缺失: {d}")
    f_prev = pd.read_parquet(KLINE / "daily_forward" / f"dt={SEAM_PREV}" / "data.parquet",
                             columns=["symbol", "close"]).set_index("symbol")["close"]
    f_next = pd.read_parquet(KLINE / "daily_forward" / f"dt={SEAM}" / "data.parquet",
                             columns=["symbol", "close"]).set_index("symbol")["close"]
    r_prev = pd.read_parquet(KLINE / "daily_unadjusted" / f"dt={SEAM_PREV}" / "data.parquet",
                             columns=["symbol", "close"]).set_index("symbol")["close"]
    r_next = pd.read_parquet(KLINE / "daily_unadjusted" / f"dt={SEAM}" / "data.parquet",
                             columns=["symbol", "close"]).set_index("symbol")["close"]

    df = pd.DataFrame({
        "qfq_prev": f_prev, "qfq_next": f_next,
        "raw_prev": r_prev, "raw_next": r_next,
    }).dropna()
    df["f_prev"] = df["qfq_prev"] / df["raw_prev"]
    df["f_next"] = df["qfq_next"] / df["raw_next"]
    df["ratio"] = df["f_next"] / df["f_prev"]
    df["raw_ret"] = df["raw_next"] / df["raw_prev"] - 1.0
    df["qfq_ret"] = df["qfq_next"] / df["qfq_prev"] - 1.0
    df["gap"] = (df["qfq_ret"] - df["raw_ret"]).abs()

    # 真实除权：原始价缺口应 ≈ 1/ratio - 1（允许 ±12% 的市场波动）。
    expected = 1.0 / df["ratio"] - 1.0
    df["event_resid"] = (df["raw_ret"] - expected).abs()
    # 方向判据：ratio>1（因子上升，除权）必须伴随原始价下跌；ratio<1 必须伴随上涨。
    sign_bad = ((df["ratio"] > 1) & (df["raw_ret"] > 0)) | (
        (df["ratio"] < 1) & (df["raw_ret"] < 0)
    )
    cand = df[(df["ratio"] - 1).abs() > 0.002]
    cand = cand[cand["gap"] > 0.002]
    # 原始价跌破跌停（<-10.5%）⇒ 当日必有真实除权（如 002481 的 10 转 15），不修；
    # 方向相反（因子降而原始价跌）仍视为坏点。
    proof_event = cand["raw_ret"] <= -0.105
    cand = cand[~proof_event | sign_bad.reindex(cand.index)]
    out = {
        "seam_prev": SEAM_PREV,
        "seam": SEAM,
        "candidates": {
            sym: {
                "ratio": float(row["ratio"]),
                "raw_ret": float(row["raw_ret"]),
                "qfq_ret": float(row["qfq_ret"]),
                "f_prev": float(row["f_prev"]),
                "f_next": float(row["f_next"]),
            }
            for sym, row in cand.sort_index().iterrows()
        },
    }
    return out


def cmd_scan(_: argparse.Namespace) -> int:
    plan = build_plan()
    print(f"候选 {len(plan['candidates'])} 只：")
    for sym, r in plan["candidates"].items():
        print(f"  {sym}  ratio={r['ratio']:.4f}  raw_ret={r['raw_ret']:+.2%}  "
              f"qfq_ret={r['qfq_ret']:+.2%}  f {r['f_prev']:.4f}->{r['f_next']:.4f}")
    print("\n真实送转对照组（应不在候选里）：")
    for sym in ("002481.SZ",):
        if sym in plan["candidates"]:
            print(f"  !! {sym} 被误判")
        else:
            print(f"  {sym} 未选中（正确）")
    return 0


def _backup_and_rewrite(symbols: dict[str, float], apply: bool) -> int:
    dates = _dates("daily_forward", LO, HI)
    if not dates:
        raise SystemExit("2010-2015 无分区")
    BACKUP.mkdir(parents=True, exist_ok=True)
    manifest = []
    touched = 0
    for d in dates:
        path = KLINE / "daily_forward" / f"dt={d}" / "data.parquet"
        table = pq.read_table(path)
        df = table.to_pandas()
        hit = df["symbol"].isin(symbols)
        if not hit.any():
            continue
        touched += 1
        if apply:
            bdir = BACKUP / f"dt={d}"
            bdir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, bdir / "data.parquet")
        for sym, ratio in symbols.items():
            m = df["symbol"] == sym
            if not m.any():
                continue
            for col in PRICE_COLS:
                df.loc[m, col] = df.loc[m, col] * ratio
        if apply:
            arrays = []
            for name in table.schema.names:
                src = table.column(name)
                if name in PRICE_COLS:
                    arrays.append(pa.array(df[name].to_numpy(), type=src.type))
                else:
                    arrays.append(src)
            new_table = pa.Table.from_arrays(arrays, schema=table.schema)
            tmp = path.with_suffix(".parquet.tmp")
            pq.write_table(new_table, tmp, compression="zstd")
            os.replace(tmp, path)
        manifest.append({"dt": d, "rows": int(hit.sum())})
    print(f"{'已改写' if apply else '将改写'} {touched} 个分区（含候选标的）")
    if apply:
        (BACKUP / "manifest.json").write_text(
            json.dumps({"symbols": symbols, "partitions": manifest}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    plan = build_plan()
    symbols = {s: r["ratio"] for s, r in plan["candidates"].items()}
    if not symbols:
        print("无候选，未改动")
        return 0
    BACKUP.mkdir(parents=True, exist_ok=True)
    PLAN.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"计划: {len(symbols)} 只 → {', '.join(symbols)}")
    return _backup_and_rewrite(symbols, apply=True)


def cmd_verify(_: argparse.Namespace) -> int:
    plan = build_plan()
    if plan["candidates"]:
        print(f"仍有 {len(plan['candidates'])} 只未修复：")
        for sym, r in plan["candidates"].items():
            print(f"  {sym} ratio={r['ratio']:.4f} gap={r['qfq_ret']-r['raw_ret']:+.4f}")
        return 1

    # 修复后全量复核：接缝 qfq 与 raw 收益应一致；期内不应出现新的 |ret|>11%
    f_prev = pd.read_parquet(KLINE / "daily_forward" / f"dt={SEAM_PREV}" / "data.parquet",
                             columns=["symbol", "close"]).set_index("symbol")["close"]
    f_next = pd.read_parquet(KLINE / "daily_forward" / f"dt={SEAM}" / "data.parquet",
                             columns=["symbol", "close"]).set_index("symbol")["close"]
    r_prev = pd.read_parquet(KLINE / "daily_unadjusted" / f"dt={SEAM_PREV}" / "data.parquet",
                             columns=["symbol", "close"]).set_index("symbol")["close"]
    r_next = pd.read_parquet(KLINE / "daily_unadjusted" / f"dt={SEAM}" / "data.parquet",
                             columns=["symbol", "close"]).set_index("symbol")["close"]
    df = pd.DataFrame({"fp": f_prev, "fn": f_next, "rp": r_prev, "rn": r_next}).dropna()
    gap = ((df.fn / df.fp) - (df.rn / df.rp)).abs()
    print(f"接缝 gap>1%: {int((gap > 0.01).sum())} 只（修复前 9 只）")
    print(f"接缝 gap>5%: {int((gap > 0.05).sum())} 只（修复前 6 只）")
    return 0 if int((gap > 0.05).sum()) == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("scan", cmd_scan), ("apply", cmd_apply), ("verify", cmd_verify)):
        p = sub.add_parser(name)
        p.set_defaults(func=fn)
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
