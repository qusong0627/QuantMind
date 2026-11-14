#!/usr/bin/env python3
"""把 ``daily_forward`` 收口为「每个 (symbol, time) 只有一行」。

背景与口径见 ``backend/shared/kline_canonical.py``。本脚本是**数据修复工具**：
默认 dry-run，只统计；``--apply`` 才写盘。写盘前先做全量目录备份，重复行另
存 ``daily_forward_release_backup``，并逐分区写 manifest，支持 ``--restore``。

用法::

    python backend/scripts/quanthk_canonicalize_kline.py --dry-run
    python backend/scripts/quanthk_canonicalize_kline.py --apply
    python backend/scripts/quanthk_canonicalize_kline.py --restore --apply
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

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from backend.shared.kline_canonical import canonicalize  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("quanthk_kline_canonical")

REL_DIR = Path("1_kline_data") / "daily_forward"
BACKUP_REL = Path("1_kline_data") / "daily_forward_backup_20261114"
REMOVED_REL = Path("1_kline_data") / "daily_forward_release_backup"
MANIFEST_REL = Path("1_kline_data") / "daily_forward_canonical_manifest.jsonl"
SUMMARY_REL = Path("1_kline_data") / "daily_forward_canonical_summary.json"
KEYS = ("symbol", "time")


def _data_dir() -> Path:
    env = os.getenv("QM_QUANTHK_DATA_DIR", "").strip()
    if env:
        return Path(env)
    if Path("/data/quanthk").is_dir():
        return Path("/data/quanthk")
    return PROJECT_ROOT / "data" / "quanthk"


def _partition_files(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(p for p in root.glob("dt=*/data.parquet") if p.is_file())


def _atomic_write(df: pd.DataFrame, path: Path) -> None:
    tmp = path.with_name(f"{path.name}.tmp")
    df.to_parquet(tmp, index=False)
    tmp.replace(path)


def _read(path: Path) -> pd.DataFrame:
    return pd.read_parquet(path)


def partition_stats(df: pd.DataFrame) -> dict[str, Any]:
    """单分区统计：行数、唯一键数、重复行数、release 分布。"""
    if df.empty:
        return {"rows": 0, "unique_keys": 0, "duplicate_rows": 0, "releases": {}}
    if not all(k in df.columns for k in KEYS):
        raise ValueError(f"缺少键列 {KEYS}")
    unique_keys = int(df.drop_duplicates(list(KEYS)).shape[0])
    releases = (
        df["release_id"].astype("string").fillna("<missing>").value_counts().to_dict()
        if "release_id" in df.columns
        else {}
    )
    return {
        "rows": int(len(df)),
        "unique_keys": unique_keys,
        "duplicate_rows": int(len(df) - unique_keys),
        "releases": {str(k): int(v) for k, v in releases.items()},
    }


def canonicalize_partition(
    df: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """返回 ``(canonical, removed)``；用临时行号精确识别保留下来的行。"""
    if df.empty:
        return df.copy(), df.copy()
    work = df.copy()
    work["_qm_row_id"] = range(len(work))
    kept = canonicalize(work, keys=KEYS)
    kept_ids = {int(x) for x in kept["_qm_row_id"].tolist()}
    removed = work.loc[~work["_qm_row_id"].isin(kept_ids)].copy()
    kept = kept.drop(columns=["_qm_row_id"])
    removed = removed.drop(columns=["_qm_row_id"])
    return kept.reset_index(drop=True), removed.reset_index(drop=True)


def _append_removed(root: Path, dt: str, removed: pd.DataFrame) -> None:
    if removed.empty:
        return
    target = root / REMOVED_REL / f"dt={dt}" / "data.parquet"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        old = pd.read_parquet(target)
        removed = pd.concat([old, removed], ignore_index=True)
    # 备份内也去重，防止重复 apply 把同一行记两次
    removed = removed.drop_duplicates()
    _atomic_write(removed, target)


def _full_backup(root: Path) -> Path:
    backup = root / BACKUP_REL
    if backup.exists():
        log.info("全量备份已存在，复用：%s", backup)
        return backup
    log.info("创建全量备份：%s", backup)
    shutil.copytree(root / REL_DIR, backup)
    return backup


def _restore(root: Path) -> dict[str, Any]:
    backup = root / BACKUP_REL
    if not backup.is_dir():
        raise FileNotFoundError(f"备份目录不存在：{backup}")
    restored = 0
    for src in _partition_files(backup):
        rel = src.relative_to(backup)
        dst = root / REL_DIR / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        restored += 1
    return {"restored_partitions": restored, "backup_dir": str(backup)}


def apply(
    root: Path,
    *,
    dry_run: bool,
    limit: int | None = None,
) -> dict[str, Any]:
    files = _partition_files(root / REL_DIR)
    if not files:
        raise FileNotFoundError(f"无 daily_forward 分区：{root / REL_DIR}")
    if limit:
        files = files[:limit]

    totals = Counter()
    release_removed: Counter[str] = Counter()
    details: list[dict[str, Any]] = []
    t0 = time.monotonic()

    if not dry_run:
        _full_backup(root)

    for i, path in enumerate(files, 1):
        dt = path.parent.name[3:]
        before = _read(path)
        stats = partition_stats(before)
        if stats["duplicate_rows"] == 0:
            totals["partitions_skipped"] += 1
            continue

        kept, removed = canonicalize_partition(before)
        if len(kept) != stats["unique_keys"]:
            raise AssertionError(
                f"{dt}: canonical 行数 {len(kept)} != 唯一键 {stats['unique_keys']}"
            )
        if len(kept) + len(removed) != len(before):
            raise AssertionError(f"{dt}: 行数不守恒")

        removed_releases = (
            removed["release_id"].astype("string").fillna("<missing>").value_counts()
            if "release_id" in removed.columns
            else {}
        )
        for k, v in removed_releases.items():
            release_removed[str(k)] += int(v)

        if not dry_run:
            _append_removed(root, dt, removed)
            _atomic_write(kept, path)
            # 写后回读断言：一个 (symbol,time) 只能一行
            check = pd.read_parquet(path)
            if len(check) != len(kept) or check.duplicated(list(KEYS)).any():
                raise AssertionError(f"{dt}: 写后校验失败")

        totals["partitions_total"] += 1
        totals["partitions_changed"] += 1
        totals["rows_before"] += len(before)
        totals["rows_after"] += len(kept)
        totals["rows_removed"] += len(removed)
        details.append(
            {
                "dt": dt,
                "rows_before": len(before),
                "rows_after": len(kept),
                "removed": len(removed),
                "removed_releases": {
                    str(k): int(v) for k, v in removed_releases.items()
                },
            }
        )
        if i % 50 == 0 or i == len(files):
            log.info(
                "进度 %d/%d（%s）：累计变更 %d 分区，移除 %d 行",
                i,
                len(files),
                dt,
                totals["partitions_changed"],
                totals["rows_removed"],
            )

    elapsed = round(time.monotonic() - t0, 1)
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dry_run": dry_run,
        "data_dir": str(root),
        "partitions_seen": len(files),
        "partitions_changed": int(totals["partitions_changed"]),
        "partitions_skipped": int(totals["partitions_skipped"]),
        "rows_before": int(totals["rows_before"]),
        "rows_after": int(totals["rows_after"]),
        "rows_removed": int(totals["rows_removed"]),
        "removed_by_release": dict(release_removed),
        "elapsed_s": elapsed,
        "changed_partitions": details,
    }

    if not dry_run:
        manifest = root / MANIFEST_REL
        manifest.parent.mkdir(parents=True, exist_ok=True)
        with manifest.open("a", encoding="utf-8") as fh:
            for d in details:
                fh.write(
                    json.dumps(
                        {"generated_at": summary["generated_at"], **d},
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        summary_path = root / SUMMARY_REL
        tmp = summary_path.with_name(f"{summary_path.name}.tmp")
        tmp.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(summary_path)

    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="daily_forward canonical 收口")
    parser.add_argument("--apply", action="store_true", help="写盘（默认 dry-run）")
    parser.add_argument(
        "--dry-run", action="store_true", help="显式 dry-run（默认行为，便于脚本自述）"
    )
    parser.add_argument("--restore", action="store_true", help="从全量备份恢复")
    parser.add_argument(
        "--limit", type=int, default=None, help="只处理前 N 个分区（调试）"
    )
    args = parser.parse_args()

    root = _data_dir()
    if args.restore:
        if not args.apply:
            parser.error("--restore 必须同时给 --apply")
        print(json.dumps(_restore(root), ensure_ascii=False, indent=2))
        return 0

    summary = apply(root, dry_run=not args.apply, limit=args.limit)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
