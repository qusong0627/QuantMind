"""把 ``fused*`` 自定义数据集的缺口分区从 ``meta.json`` 的来源库补齐。

背景
----
``/data/quantcustom/6_ml_datasets/fused189`` 是训练时按特征选择物化的**快照**
数据集：meta.json 记录 ``features``（189 列）与 ``sources``（每个特征来自哪个
因子库目录）。推理端按 ``dt=YYYYMMDD`` 直读；快照只建到训练日时，模型在训练日
之后的补推会全部报「QuantDB fused189 最新可用 ...，请求 ... 超出数据范围」，
默认模型因此在模型管理里补推失败（2026-11-28 实测）。

本脚本按 meta.json 的来源映射逐日重建缺口分区：
- 股票池 = 来源库中特征数最多的库（通常是 ``quantcustom/.../l1_factors``），
  可用 ``--universe-source`` 覆盖；
- 其余来源库按 ``symbol`` 左连接；来源库缺该日/缺列 → 该特征写 NaN
  （推理端 DL 模板 ``np.nan_to_num`` 按 0 填充，树模型原生处理 NaN）；
- 已存在分区默认跳过，``--force`` 才覆盖；临时文件 + 原子替换，不改历史分区；
- 只补「股票池来源库已有分区」的日期，不凭单调来源造残缺历史。

用法::

    python backend/scripts/extend_fused_dataset.py --all
    python backend/scripts/extend_fused_dataset.py --dataset /data/quantcustom/6_ml_datasets/fused189
    python backend/scripts/extend_fused_dataset.py --dataset ... --start 20261124 --end 20261127
    python backend/scripts/extend_fused_dataset.py --all --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import pandas as pd

LOG = logging.getLogger("extend_fused_dataset")
DEFAULT_ROOT = Path(os.getenv("QM_QUANTCUSTOM_DATA_DIR") or "/data/quantcustom")
DATASET_SUBDIR = Path("6_ml_datasets")


def _partition_path(library: Path, day: str) -> Path:
    return library / f"dt={day}" / "data.parquet"


def _partition_days(library: Path) -> list[str]:
    if not library.is_dir():
        return []
    return sorted(
        entry.name.split("=", 1)[1]
        for entry in library.iterdir()
        if entry.is_dir()
        and entry.name.startswith("dt=")
        and entry.name.split("=", 1)[1].isdigit()
    )


def _quoted(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _read_columns(con: duckdb.DuckDBPyConnection, parquet: Path) -> list[str]:
    return [row[0] for row in con.execute(
        f"SELECT * FROM read_parquet('{parquet.as_posix()}') LIMIT 0"
    ).description]


def resolve_universe_source(sources: dict[str, str], features: list[str]) -> str:
    """选股票池来源库：覆盖特征数最多者；并列时取路径字典序最小，保证可复现。"""
    counts = Counter(sources[feature] for feature in features if feature in sources)
    if not counts:
        raise ValueError("meta.json 没有可用的 sources 映射")
    most = max(counts.values())
    candidates = sorted(path for path, count in counts.items() if count == most)
    return candidates[0]


def _load_meta(dataset_root: Path) -> tuple[list[str], dict[str, str]]:
    meta_path = dataset_root / "meta.json"
    if not meta_path.is_file():
        raise FileNotFoundError(f"meta.json 不存在: {meta_path}")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    features = list(meta.get("features") or [])
    sources = {str(k): str(v) for k, v in (meta.get("sources") or {}).items()}
    if not features or not sources:
        raise ValueError(f"meta.json 缺 features/sources: {meta_path}")
    missing = [feature for feature in features if feature not in sources]
    if missing:
        raise ValueError(f"features 未在 sources 中映射: {missing[:5]}（共 {len(missing)}）")
    return features, sources


def build_partition(
    dataset_root: Path,
    day: str,
    features: list[str],
    sources: dict[str, str],
    universe_source: str,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """重建单个分区；返回 {day, status, rows, missing_features}。"""
    out = _partition_path(dataset_root, day)
    if out.is_file() and not force:
        return {"day": day, "status": "skipped", "rows": 0, "missing_features": []}

    universe_path = _partition_path(Path(universe_source), day)
    if not universe_path.is_file():
        return {
            "day": day,
            "status": "no_universe",
            "rows": 0,
            "missing_features": [],
        }

    con = duckdb.connect()
    try:
        base = con.execute(
            f"SELECT DISTINCT symbol FROM read_parquet('{universe_path.as_posix()}')"
        ).fetchdf()
        by_library: dict[str, list[str]] = {}
        for feature in features:
            by_library.setdefault(sources[feature], []).append(feature)

        missing_features: list[str] = []
        for library, wanted in sorted(by_library.items()):
            parquet = _partition_path(Path(library), day)
            if not parquet.is_file():
                missing_features.extend(wanted)
                continue
            columns = set(_read_columns(con, parquet))
            usable = [
                feature
                for feature in wanted
                if feature in columns and feature not in base.columns
            ]
            missing_features.extend(
                feature for feature in wanted if feature not in columns
            )
            if not usable:
                continue
            select = ", ".join(_quoted(feature) for feature in usable)
            frame = con.execute(
                f"SELECT symbol, {select} FROM read_parquet('{parquet.as_posix()}')"
            ).fetchdf().drop_duplicates("symbol")
            base = base.merge(frame, on="symbol", how="left")

        for feature in features:
            if feature not in base.columns:
                base[feature] = np.nan
        base = base[["symbol", *features]]
        base["dt"] = int(day)

        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".parquet.tmp")
        base.to_parquet(tmp, index=False)
        tmp.replace(out)
        # 质量侧车：下游 coverage/推理预检据此知道「这一天有分区但缺列」，
        # 不用再读 189 列数据。缺列为空时删除侧车，避免旧标记粘住。
        sidecar = out.parent / "_missing_features.json"
        missing = sorted(set(missing_features))
        if missing:
            sidecar.write_text(
                json.dumps({"day": day, "missing_features": missing}, ensure_ascii=False),
                encoding="utf-8",
            )
        elif sidecar.is_file():
            sidecar.unlink()
        return {
            "day": day,
            "status": "written",
            "rows": len(base),
            "missing_features": missing,
        }
    finally:
        con.close()


def extend_dataset(
    dataset_root: Path,
    *,
    start: str | None = None,
    end: str | None = None,
    universe_source: str | None = None,
    force: bool = False,
    refresh_incomplete: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    """补齐 ``dataset_root`` 在 [start, end] 内的缺口分区。

    默认 start = 已有最新分区 + 1，end = 股票池来源库最新分区。
    只处理股票池来源库已有分区的交易日，绝不凭单一来源造残缺历史。
    ``refresh_incomplete``：已有分区若带 ``_missing_features.json`` 侧车，也重建
    一次——上游源库常在 T+1 才补齐（factor_dictionary / rd_mined），不刷新的话
    缺列会永久粘在分区上。
    """
    features, sources = _load_meta(dataset_root)
    universe = universe_source or resolve_universe_source(sources, features)
    universe_days = _partition_days(Path(universe))
    if not universe_days:
        return {
            "dataset": str(dataset_root),
            "status": "no_universe_data",
            "universe_source": universe,
            "written": 0,
            "skipped": 0,
        }

    existing_days = set(_partition_days(dataset_root))
    first = start or (
        f"{int(max(existing_days)) + 1:08d}" if existing_days else universe_days[0]
    )
    last = end or universe_days[-1]
    targets = [
        day for day in universe_days if first <= day <= last and day not in existing_days
    ]
    if force:
        targets = [
            day for day in universe_days if first <= day <= last
        ]
    elif refresh_incomplete:
        incomplete = [
            day
            for day in universe_days
            if day in existing_days
            and day <= last
            and (dataset_root / f"dt={day}" / "_missing_features.json").is_file()
        ]
        targets = sorted(set(targets) | set(incomplete))
    if dry_run:
        return {
            "dataset": str(dataset_root),
            "status": "dry_run",
            "universe_source": universe,
            "targets": targets,
            "written": 0,
            "skipped": len(existing_days),
        }

    results = [
        build_partition(
            dataset_root,
            day,
            features,
            sources,
            universe,
            force=force or day in existing_days,
        )
        for day in targets
    ]
    written = sum(1 for item in results if item["status"] == "written")
    skipped = sum(1 for item in results if item["status"] == "skipped")
    no_universe = [item["day"] for item in results if item["status"] == "no_universe"]
    missing = {
        item["day"]: item["missing_features"]
        for item in results
        if item.get("missing_features")
    }

    if written:
        meta_path = dataset_root / "meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["last_extended_dt"] = max(item["day"] for item in results if item["status"] == "written")
        meta["last_extended_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")

    return {
        "dataset": str(dataset_root),
        "status": "ok",
        "universe_source": universe,
        "start": first if first <= last else None,
        "end": last,
        "targets": len(targets),
        "written": written,
        "skipped": skipped,
        "no_universe": no_universe,
        "missing_features": missing,
        "rows": sorted({item["rows"] for item in results if item["status"] == "written"}),
    }


def extend_all(
    root: Path = DEFAULT_ROOT,
    *,
    start: str | None = None,
    end: str | None = None,
    force: bool = False,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    """补齐 ``<root>/6_ml_datasets/fused*`` 下所有带 meta.json 的数据集。"""
    base = root / DATASET_SUBDIR
    if not base.is_dir():
        return [{"dataset": str(base), "status": "missing_root"}]
    results = []
    for dataset_root in sorted(base.glob("fused*")):
        if not dataset_root.is_dir() or not (dataset_root / "meta.json").is_file():
            continue
        try:
            results.append(
                extend_dataset(
                    dataset_root,
                    start=start,
                    end=end,
                    force=force,
                    dry_run=dry_run,
                )
            )
        except Exception as exc:  # noqa: BLE001 - 单个数据集失败不拖垮其它
            LOG.warning("extend %s 失败: %s", dataset_root, exc)
            results.append(
                {"dataset": str(dataset_root), "status": "failed", "error": str(exc)}
            )
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", help="单个 fused 数据集目录（含 meta.json）")
    parser.add_argument(
        "--all", action="store_true", help="补齐 QM_QUANTCUSTOM_DATA_DIR 下所有 fused*"
    )
    parser.add_argument("--start", help="起始交易日 YYYYMMDD（含）")
    parser.add_argument("--end", help="结束交易日 YYYYMMDD（含）")
    parser.add_argument("--universe-source", help="股票池来源库目录（默认自动选）")
    parser.add_argument("--force", action="store_true", help="覆盖已有分区")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    if not args.dataset and not args.all:
        parser.error("必须指定 --dataset 或 --all")

    if args.dataset:
        result = extend_dataset(
            Path(args.dataset),
            start=args.start,
            end=args.end,
            universe_source=args.universe_source,
            force=args.force,
            dry_run=args.dry_run,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("status") in {"ok", "dry_run", "no_universe_data"} else 1

    results = extend_all(
        start=args.start,
        end=args.end,
        force=args.force,
        dry_run=args.dry_run,
    )
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0 if all(
        item.get("status") in {"ok", "dry_run", "no_universe_data", "skipped"}
        for item in results
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
