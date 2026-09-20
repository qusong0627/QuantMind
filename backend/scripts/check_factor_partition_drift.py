#!/usr/bin/env python3
"""因子库 parquet 列序漂移巡检与修复。

QuantDB 因子库 parquet 的物理列序会随日期漂移（列名集合不变、位置变），
任何跨日按列**位置**索引的算法都会静默串列。本脚本是这件事的**唯一运维入口**：

    report      巡检全部市场全部库，有漂移即非零退出（可挂 CI / 定时体检）
    normalize   把漂移分区重排回本库主序，**默认 dry-run**，需 --apply 才落盘

修复规则（最小差量）：目标序 = 主序中该分区**拥有的**列（保持主序相对次序）
+ 该分区独有的列（排序后追加到末尾）。这样：

* 列集合相同的漂移分区 → 重排后与主序**逐位相同**
* 列集合是主序子集的（上游删了列）→ 共享列归位，仅少那几列
* 列集合有主序外新列的（上游加列）→ 新列追加末尾，不打断既有相对序

⚠ **与 QuantDB 同步校验的关系**：同步的免重下登记用**云端声明的 sha256** 比对本地
文件（``quantdb_daily_sync.py:358`` ``verify_content``）。就地重排会改变本地文件的
sha，因此**仅在「状态库丢失后的恢复路径」上**会触发该库全量重下（2026-08-17 曾发生过
一次）。日常同步走 ``objects`` 表命中即跳过、不哈希，不受影响。是否需要付这个代价，
由本脚本的 dry-run 报告交给运维决定。

用法::

    python3 backend/scripts/check_factor_partition_drift.py report
    python3 backend/scripts/check_factor_partition_drift.py normalize            # dry-run
    python3 backend/scripts/check_factor_partition_drift.py normalize --apply
    python3 backend/scripts/check_factor_partition_drift.py normalize --market CN --library l1_factors --apply
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.shared.factor_partition import (  # noqa: E402
    default_market_roots,
    iter_partitions,
    partition_signature,
)

log = logging.getLogger("factor_drift")
ML_DATASETS = "6_ml_datasets"


@dataclass(frozen=True)
class LibraryScan:
    market: str
    library: str
    root: Path
    partitions: int
    orders: int
    drifted: int
    schema_variants: int


def _libraries(market_root: Path) -> list[Path]:
    base = market_root / ML_DATASETS
    if not base.is_dir():
        return []
    return sorted(
        p
        for p in base.iterdir()
        if p.is_dir()
        and not p.name.startswith(("_", "."))
        and not p.name.endswith("_labels")
    )


def scan_library(market: str, lib_root: Path) -> tuple[LibraryScan, dict]:
    """扫一个库：按物理列序分组，返回统计 + 修复计划。"""
    buckets: dict[tuple[str, ...], list[Path]] = {}
    for f in iter_partitions(lib_root):
        buckets.setdefault(partition_signature(f), []).append(f)
    if not buckets:
        return LibraryScan(market, lib_root.name, lib_root, 0, 0, 0, 0), {}

    # 主序 = 覆盖分区最多的那个物理序
    ref_order = max(buckets, key=lambda o: len(buckets[o]))
    ref_set = set(ref_order)
    plan: list[tuple[Path, list[str]]] = []
    schema_variants = 0
    for order, files in buckets.items():
        if order == ref_order:
            continue
        if set(order) != ref_set:
            schema_variants += 1
        target = [c for c in ref_order if c in set(order)]
        target += sorted(set(order) - ref_set)
        if tuple(target) == order:
            continue  # 已归位（子集/超集但相对序正确）
        plan.extend((f, target) for f in files)

    stat = LibraryScan(
        market=market,
        library=lib_root.name,
        root=lib_root,
        partitions=sum(len(v) for v in buckets.values()),
        orders=len(buckets),
        drifted=len(plan),
        schema_variants=schema_variants,
    )
    return stat, {"ref_order": ref_order, "plan": plan}


def report(
    markets: dict[str, Path], only_market: str | None, only_lib: str | None
) -> int:
    rows: list[LibraryScan] = []
    details: dict[tuple[str, str], dict] = {}
    for market, root in markets.items():
        if only_market and market != only_market:
            continue
        if not root.is_dir():
            continue
        for lib_root in _libraries(root):
            if only_lib and lib_root.name != only_lib:
                continue
            stat, det = scan_library(market, lib_root)
            if stat.partitions == 0:
                continue
            rows.append(stat)
            details[(market, stat.library)] = det

    print(
        f"\n{'市场':<9}{'库':<18}{'分区':>7}{'列序种数':>9}{'漂移分区':>9}"
        f"{'列集合变体':>11}  状态"
    )
    print("-" * 78)
    bad = 0
    for r in sorted(rows, key=lambda x: (-x.drifted, x.market, x.library)):
        if r.drifted:
            bad += 1
            status = "✗ 漂移"
        elif r.schema_variants:
            status = "⚠ 仅列集合差异"
        else:
            status = "✓"
        print(
            f"{r.market:<9}{r.library:<18}{r.partitions:>7}{r.orders:>9}"
            f"{r.drifted:>9}{r.schema_variants:>11}  {status}"
        )

    if bad:
        print(
            f"\n发现 {bad} 个库存在列序漂移。修复："
            f"python3 {Path(__file__).name} normalize --apply"
        )
        print(
            "注意：漂移分区重排后，本地 sha 与云端声明不一致 —— "
            "仅影响「状态库丢失后的恢复路径」，日常同步不受影响。"
        )
    else:
        print("\n无列序漂移。")
    return 1 if bad else 0


def normalize(
    markets: dict[str, Path],
    only_market: str | None,
    only_lib: str | None,
    apply: bool,
) -> int:
    total_files = total_bytes = 0
    full_plan: list[tuple[Path, list[str]]] = []
    for market, root in markets.items():
        if only_market and market != only_market:
            continue
        if not root.is_dir():
            continue
        for lib_root in _libraries(root):
            if only_lib and lib_root.name != only_lib:
                continue
            stat, det = scan_library(market, lib_root)
            plan = det.get("plan") or []
            if not plan:
                continue
            nbytes = sum(f.stat().st_size for f, _ in plan)
            print(
                f"  {market}/{stat.library}: {len(plan)} 个分区待重排 "
                f"({nbytes / 1e6:.1f} MB)  [{stat.partitions} 分区 / {stat.orders} 种列序]"
            )
            total_files += len(plan)
            total_bytes += nbytes
            full_plan.extend(plan)
    if apply and full_plan:
        # 先落清单再改数据：任何中断都留得下精确回滚依据（KB 级，不占空间）。
        dest = _write_manifest(full_plan)
        print(f"\n回滚清单已写: {dest}")
        print(f"回滚命令: python3 {Path(__file__).name} restore {dest}")
        done = _apply_plan(full_plan)
        print(f"\n已重排 {done} / {total_files} 个分区，共 {total_bytes / 1e6:.1f} MB")
        return 0
    verb = "待重排（dry-run）"
    print(f"\n{verb} {total_files} 个分区，共 {total_bytes / 1e6:.1f} MB")
    if total_files:
        print("加 --apply 落盘（会先写回滚清单，再改数据）。")
    return 0


def _apply_plan(plan: list[tuple[Path, list[str]]]) -> int:
    """逐分区原子重排并**回读校验**列序。返回成功数。"""
    import pandas as pd

    done = 0
    for i, (path, target) in enumerate(plan, start=1):
        # 同目录临时文件 + replace：任何中断都不留半写入的有效目标。
        tmp = path.with_suffix(".reorder.tmp.parquet")
        try:
            df = pd.read_parquet(path)
            df[target].to_parquet(tmp, index=False)
            if partition_signature(tmp) != tuple(target):
                raise RuntimeError("回读列序与目标不符")
            os.replace(tmp, path)
            done += 1
        finally:
            tmp.unlink(missing_ok=True)
        if i % 50 == 0:
            log.info("normalize %d/%d", i, len(plan))
    return done


def _write_manifest(
    plan: list[tuple[Path, list[str]]], dest: Path | None = None
) -> Path:
    """把每个待改分区的**原始物理列序**记下来 —— 精确回滚用，体积是 KB 级。"""
    import json

    manifest = {str(path): list(partition_signature(path)) for path, _ in plan}
    if dest is None:
        dest = Path(
            f"factor_drift_manifest_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        )
    dest.write_text(json.dumps(manifest, ensure_ascii=False, indent=1))
    return dest


def restore(manifest_path: str) -> int:
    """按清单把分区**还原**到修复前的物理列序。"""
    import json

    import pandas as pd

    manifest = json.loads(Path(manifest_path).read_text())
    done = 0
    for path_s, original in manifest.items():
        path = Path(path_s)
        if not path.exists():
            log.warning("回滚跳过（文件已不在）: %s", path_s)
            continue
        current = partition_signature(path)
        if current == tuple(original) or set(current) != set(original):
            continue
        tmp = path.with_suffix(".restore.tmp.parquet")
        try:
            pd.read_parquet(path)[original].to_parquet(tmp, index=False)
            if partition_signature(tmp) != tuple(original):
                raise RuntimeError("回读列序与原始不符")
            os.replace(tmp, path)
            done += 1
        finally:
            tmp.unlink(missing_ok=True)
    print(f"已还原 {done} / {len(manifest)} 个分区")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="因子库 parquet 列序漂移巡检/修复")
    parser.add_argument(
        "action",
        choices=["report", "normalize", "restore"],
        nargs="?",
        default="report",
    )
    parser.add_argument("manifest", nargs="?", help="restore 时的回滚清单路径")
    parser.add_argument("--market", help="只处理该市场（CN/US/HK/BC/FUTURES/CUSTOM）")
    parser.add_argument("--library", help="只处理该库（如 l1_factors）")
    parser.add_argument(
        "--apply", action="store_true", help="normalize 时真正落盘（默认 dry-run）"
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )

    if args.action == "restore":
        if not args.manifest:
            parser.error("restore 需要给出回滚清单路径")
        return restore(args.manifest)

    markets = default_market_roots()
    if args.action == "report":
        return report(markets, args.market, args.library)
    return normalize(markets, args.market, args.library, args.apply)


if __name__ == "__main__":
    raise SystemExit(main())
