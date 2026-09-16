#!/usr/bin/env python3
"""L0.5 热集快照留存维护（T-P6-04）：质检报告 / 容量统计 / 过期降冷。

定位：保留层的 EOD 运维入口。落盘与周期降冷由 worker 内嵌的 l05 归档器自动完成
（``SnapshotArchiver``，每日自动 prune），本脚本提供**可执行的日度质检报告**与运维面
（容量表 / 手动/dry-run 降冷）。

用法:
    # 当日质检（默认取最新分区日；报告 JSON 写 <base>/_reports/quality-<day>.json）
    python backend/scripts/l05_maintenance.py report
    python backend/scripts/l05_maintenance.py report --date 20260917
    python backend/scripts/l05_maintenance.py report --days 5        # 最近 5 个分区日

    # 容量统计（逐日行数/文件数/字节/行均字节 + 保留期投影）
    python backend/scripts/l05_maintenance.py capacity --keep-days 90

    # 过期降冷（默认 dry-run 只列清单；--apply 才真删）
    python backend/scripts/l05_maintenance.py prune --keep-days 90
    python backend/scripts/l05_maintenance.py prune --keep-days 90 --apply

可选参数:
    --dir        数据根目录（默认 env QM_L05_DIR 或 /data/l05_snapshots）
    --json-out   report 报告落点（默认 <base>/_reports/quality-<day>.json；多日时忽略）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from backend.shared.l05_store import (  # noqa: E402
    DEFAULT_BASE_DIR,
    capacity_report,
    list_days,
    prune_old,
    quality_report,
    read_day,
)

_CST = timezone(timedelta(hours=8))


def _base_dir(args: argparse.Namespace) -> str:
    return args.dir or os.getenv("QM_L05_DIR") or DEFAULT_BASE_DIR


def _parse_day(key: str) -> date:
    return date(int(key[:4]), int(key[4:6]), int(key[6:8]))


def _resolve_days(args: argparse.Namespace, base: str) -> list[str]:
    """report 目标日解析：--date 优先；否则 --days N 取最近 N 个分区日；再否则最新日。"""
    if getattr(args, "date", None):
        return [str(args.date).replace("-", "")]
    all_days = list_days(base)
    if not all_days:
        return []
    if getattr(args, "days", None):
        return all_days[-int(args.days):]
    return all_days[-1:]


def cmd_report(args: argparse.Namespace) -> int:
    base = _base_dir(args)
    days = _resolve_days(args, base)
    if not days:
        print(f"[l05] {base} 下无任何 date= 分区（尚未落盘？）")
        return 0

    reports_dir = Path(base) / "_reports"
    for key in days:
        try:
            day = _parse_day(key)
        except ValueError:
            print(f"[l05] --date 非法: {key}")
            return 2
        df = read_day(day, base_dir=base)
        report = quality_report(df)
        report["day"] = key
        report["generated_at"] = datetime.now(tz=_CST).isoformat()
        out_path = (
            Path(args.json_out)
            if args.json_out and len(days) == 1
            else reports_dir / f"quality-{key}.json"
        )
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )

        totals = report["totals"]
        flags = report["flags"]
        passed = "PASS" if totals["rows"] > 0 and not flags else "CHECK"
        print(
            f"[l05] {key} 质检 {passed}: rows={totals['rows']} symbols={totals['symbols']} "
            f"flags={len(flags)} -> {out_path}"
        )
        for flag in flags[:10]:
            print(f"    ⚠ {flag}")
        if len(flags) > 10:
            print(f"    … 其余 {len(flags) - 10} 条见报告 JSON")
    return 0


def cmd_capacity(args: argparse.Namespace) -> int:
    base = _base_dir(args)
    report = capacity_report(base)
    if not report["days"]:
        print(f"[l05] {base} 下无任何 date= 分区")
        return 0
    print(f"[l05] 容量统计 {base}")
    for key, stats in report["days"].items():
        print(
            f"    {key}: rows={stats['rows']:>9,} files={stats['files']:>4} "
            f"size={stats['bytes'] / 1e6:>8.1f} MB 行均={stats['bytes_per_row']} B"
        )
    totals = report["totals"]
    print(
        f"    合计: 天数={totals['days']} rows={totals['rows']:,} "
        f"size={totals['bytes'] / 1e9:.2f} GB 行均={totals['bytes_per_row']} B "
        f"日均={totals['bytes_per_day'] / 1e6:.1f} MB"
    )
    if getattr(args, "keep_days", None):
        projected = totals["bytes_per_day"] * int(args.keep_days)
        print(
            f"    保留期投影: keep_days={args.keep_days} × 日均 "
            f"≈ {projected / 1e9:.1f} GB（以已有日均线性外推）"
        )
    if report["unreadable_files"]:
        print(f"    ⚠ 无法读取元数据的文件 {len(report['unreadable_files'])} 个：")
        for path in report["unreadable_files"][:10]:
            print(f"        {path}")
    return 0


def cmd_prune(args: argparse.Namespace) -> int:
    base = _base_dir(args)
    dry_run = not args.apply
    removed = prune_old(base, keep_days=int(args.keep_days), dry_run=dry_run)
    if not removed:
        print(f"[l05] 无超过 {args.keep_days} 天的分区可降冷")
        return 0
    mode = "将删除（dry-run）" if dry_run else "已删除"
    print(f"[l05] {mode} {len(removed)} 个分区: {', '.join(removed)}")
    if dry_run:
        print("    复核无误后加 --apply 执行删除")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="L0.5 热集快照留存维护（T-P6-04）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_report = sub.add_parser("report", help="当日（或最近 N 日）质检报告")
    p_report.add_argument("--date", help="YYYYMMDD（缺省取最新分区日）")
    p_report.add_argument("--days", type=int, help="最近 N 个分区日逐个质检")
    p_report.add_argument("--json-out", help="报告 JSON 落点（多日时忽略，写入 <base>/_reports/）")
    p_report.set_defaults(func=cmd_report)

    p_cap = sub.add_parser("capacity", help="容量统计（逐日 + 保留期投影）")
    p_cap.add_argument("--keep-days", type=int, help="按现有日均投影该保留期的总占用")
    p_cap.set_defaults(func=cmd_capacity)

    p_prune = sub.add_parser("prune", help="过期降冷（默认 dry-run）")
    p_prune.add_argument("--keep-days", type=int, default=90)
    p_prune.add_argument("--apply", action="store_true", help="真删（缺省 dry-run 只列清单）")
    p_prune.set_defaults(func=cmd_prune)

    for p in (p_report, p_cap, p_prune):
        p.add_argument("--dir", help="数据根目录（默认 env QM_L05_DIR 或 /data/l05_snapshots）")

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
