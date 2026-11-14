#!/usr/bin/env python3
"""修复港股复权因子 + 重建 daily_backward（机构级数据收口）。

问题（2026-11-14 实测）：``adjust_factors`` 有 83.7 万行 ``adj_factor=inf``，
``daily_backward`` 有 817,621 行 ``close=inf``。根因是源侧 inf/异常 step
（如 0501.HK 2026-05-08 step=3400，但 canonical 收盘仅 89.8→87.6）被
cumprod 放大。修复规则见 ``backend/shared/adjust_factor_repair.py``。

本脚本默认 dry-run；``--apply`` 才写盘，写前全量备份因子目录与 backward
目录，支持 ``--restore``。重建 backward 复用 ``quanthk_adjust_factor`` 的
reindex+ffill 与逐日写盘实现，保证与生产链同口径。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from backend.scripts.quanthk_adjust_factor import (  # noqa: E402
    BACKWARD_DIR,
    FACTOR_DIR,
    FORWARD_DIR,
    _partition_dts,
    _write_backward,
)
from backend.shared.adjust_factor_repair import repair_factor_frame  # noqa: E402
from backend.shared.stock_utils import StockCodeUtil  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("hk_factor_repair")

FACTOR_BACKUP = FACTOR_DIR.parent / "adjust_factors_backup_20261114"
BACKWARD_BACKUP = BACKWARD_DIR.parent / "daily_backward_backup_20261114"
SUMMARY = FACTOR_DIR.parent / "adjust_factor_repair_summary.json"


def _atomic_write(df: pd.DataFrame, path: Path) -> None:
    tmp = path.with_name(f"{path.name}.tmp")
    df.to_parquet(tmp, index=False)
    tmp.replace(path)


def load_forward_closes() -> dict[str, pd.Series]:
    """symbol -> 交易日收盘 Series（canonical forward，已一个股票日一行）。"""
    pattern = str(FORWARD_DIR / "dt=*" / "data.parquet")
    log.info("读取 canonical forward：%s", pattern)
    df = (
        duckdb.connect()
        .execute(
            f"SELECT symbol, CAST(dt AS VARCHAR) AS dt, close "
            f"FROM read_parquet('{pattern}', hive_partitioning=1)"
        )
        .fetchdf()
    )
    df["dt"] = pd.to_datetime(df["dt"], format="%Y%m%d", errors="coerce")
    df = df.dropna(subset=["dt", "symbol"])
    out: dict[str, pd.Series] = {}
    for symbol, g in df.groupby("symbol", sort=False):
        s = pd.Series(
            pd.to_numeric(g["close"], errors="coerce").to_numpy(),
            index=pd.DatetimeIndex(g["dt"]),
        ).sort_index()
        out[str(symbol)] = s[~s.index.duplicated(keep="last")]
    log.info("forward 覆盖 %d 只标的", len(out))
    return out


def repair_all_factors(
    closes: dict[str, pd.Series],
    *,
    dry_run: bool,
    limit: int | None = None,
) -> dict[str, Any]:
    files = sorted(FACTOR_DIR.glob("*.parquet"))
    if limit:
        files = files[:limit]
    if not files:
        raise FileNotFoundError(f"无因子文件：{FACTOR_DIR}")

    if not dry_run and not FACTOR_BACKUP.exists():
        log.info("备份因子目录 → %s", FACTOR_BACKUP)
        shutil.copytree(FACTOR_DIR, FACTOR_BACKUP)

    stats_agg = Counter()
    changed: list[dict[str, Any]] = []
    t0 = time.monotonic()

    for i, path in enumerate(files, 1):
        symbol = path.stem.removesuffix(".HK")
        full_symbol = f"{symbol}.HK"
        df = pd.read_parquet(path)
        if df.empty:
            continue
        repaired, stats = repair_factor_frame(
            df, closes.get(full_symbol, pd.Series(dtype="float64"))
        )
        for k, v in stats.items():
            stats_agg[k] += v

        # 判断是否真的变化（step/factor 任一超过容差）
        old_step = pd.to_numeric(df["adj_step"], errors="coerce").to_numpy(
            dtype="float64"
        )
        new_step = pd.to_numeric(repaired["adj_step"], errors="coerce").to_numpy(
            dtype="float64"
        )
        old_factor = pd.to_numeric(df["adj_factor"], errors="coerce").to_numpy(
            dtype="float64"
        )
        new_factor = pd.to_numeric(repaired["adj_factor"], errors="coerce").to_numpy(
            dtype="float64"
        )
        changed_rows = int(
            np.sum(~np.isclose(old_step, new_step, equal_nan=True, rtol=0, atol=1e-12))
            + np.sum(
                ~np.isclose(old_factor, new_factor, equal_nan=True, rtol=0, atol=1e-9)
            )
        )
        # 任一侧有 inf/NaN 也算变化
        if changed_rows or not np.array_equal(
            np.isfinite(old_factor), np.isfinite(new_factor)
        ):
            stats_agg["files_changed"] += 1
            changed.append(
                {
                    "symbol": full_symbol,
                    "repaired": stats["repaired"],
                    "inf_before": stats["inf_before"],
                    "inf_after": stats["inf_after"],
                }
            )
            if not dry_run:
                _atomic_write(repaired, path)
        stats_agg["files_total"] += 1
        if i % 500 == 0 or i == len(files):
            log.info(
                "因子进度 %d/%d：变更 %d 只，修 step %d 行",
                i,
                len(files),
                stats_agg["files_changed"],
                stats_agg["repaired"],
            )

    return {
        "files_total": int(stats_agg["files_total"]),
        "files_changed": int(stats_agg["files_changed"]),
        "rows_total": int(stats_agg["rows"]),
        "steps_repaired": int(stats_agg["repaired"]),
        "inf_before": int(stats_agg["inf_before"]),
        "inf_after": int(stats_agg["inf_after"]),
        "changed_sample": changed[:20],
        "elapsed_s": round(time.monotonic() - t0, 1),
    }


def build_factor_panel(date_index: pd.DatetimeIndex) -> dict[str, dict[str, float]]:
    """与 quanthk_adjust_factor 同口径：因子按日 reindex + ffill。"""
    panel: dict[str, dict[str, float]] = {d.strftime("%Y%m%d"): {} for d in date_index}
    dt_keys = list(panel)
    for path in sorted(FACTOR_DIR.glob("*.parquet")):
        code = path.stem.removesuffix(".HK")
        try:
            f = pd.read_parquet(path, columns=["time", "adj_factor"])
        except Exception:  # noqa: BLE001
            continue
        f["time"] = pd.to_datetime(f["time"], errors="coerce")
        f = (
            f.dropna(subset=["time"])
            .drop_duplicates("time", keep="last")
            .sort_values("time")
        )
        if f.empty:
            continue
        series = f.set_index("time")["adj_factor"].astype("float64")
        vals = series.reindex(date_index, method="ffill")
        tk = StockCodeUtil.to_hk_suffix(code)
        for d, v in zip(dt_keys, vals, strict=False):
            if pd.notna(v) and np.isfinite(v):
                panel[d][tk] = float(v)
    return panel


def rebuild_backward(dry_run: bool) -> dict[str, Any]:
    if not dry_run and not BACKWARD_BACKUP.exists():
        log.info("备份 daily_backward → %s", BACKWARD_BACKUP)
        shutil.copytree(BACKWARD_DIR, BACKWARD_BACKUP)
    dates = _partition_dts(FORWARD_DIR)
    date_index = pd.DatetimeIndex([pd.Timestamp(d) for d in dates])
    panel = build_factor_panel(date_index)
    covered = sum(1 for d in panel if panel[d])
    log.info("因子面板：%d 日，其中 %d 日有因子", len(panel), covered)
    if dry_run:
        return {"dates": len(dates), "dates_with_factor": covered}
    written = _write_backward(dates, panel)
    return {"dates": len(dates), "dates_with_factor": covered, "written": written}


def restore() -> dict[str, Any]:
    if not FACTOR_BACKUP.is_dir():
        raise FileNotFoundError(FACTOR_BACKUP)
    for src in FACTOR_BACKUP.glob("*.parquet"):
        shutil.copy2(src, FACTOR_DIR / src.name)
    restored_backward = 0
    if BACKWARD_BACKUP.is_dir():
        for src in BACKWARD_BACKUP.glob("dt=*/data.parquet"):
            dst = BACKWARD_DIR / src.relative_to(BACKWARD_BACKUP)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            restored_backward += 1
    return {
        "factors_restored": len(list(FACTOR_BACKUP.glob("*.parquet"))),
        "backward_partitions_restored": restored_backward,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="港股复权因子修复 + backward 重建")
    parser.add_argument("--apply", action="store_true", help="写盘（默认 dry-run）")
    parser.add_argument("--dry-run", action="store_true", help="显式 dry-run")
    parser.add_argument("--restore", action="store_true", help="从备份恢复")
    parser.add_argument(
        "--skip-backward", action="store_true", help="只修因子，不重建 backward"
    )
    parser.add_argument("--limit", type=int, default=None, help="只处理前 N 个因子文件")
    args = parser.parse_args()
    if args.restore:
        if not args.apply:
            parser.error("--restore 必须同时给 --apply")
        print(json.dumps(restore(), ensure_ascii=False, indent=2))
        return 0

    dry_run = not args.apply
    t0 = time.monotonic()
    closes = load_forward_closes()
    summary: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dry_run": dry_run,
        "data_dir": str(FORWARD_DIR.parents[1]),
        "factors": repair_all_factors(closes, dry_run=dry_run, limit=args.limit),
    }
    if not args.limit and not args.skip_backward:
        summary["backward"] = rebuild_backward(dry_run)
    summary["elapsed_s"] = round(time.monotonic() - t0, 1)

    if not dry_run and not args.limit:
        tmp = SUMMARY.with_name(f"{SUMMARY.name}.tmp")
        tmp.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(SUMMARY)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
