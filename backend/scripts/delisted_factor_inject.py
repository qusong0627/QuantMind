#!/usr/bin/env python3
"""退市股因子行级追加（A2，方案 1）。

`factor_materialize.merge` 的契约是「以正式库行集为准，只增/覆盖列，绝不改
标的不变集」——所以它不会把退市股（新 symbol）写进历史分区。本工具专门做
**行级追加**：把 staging 里正式库没有的 symbol 追加到官方分区，只增不减：

- 官方已有 symbol 的行 / 列值**原样保留**；
- 新 symbol 的列取 staging；staging 没有的官方列补 NaN；
- `--exclude-cols` 里的长窗列对新 symbol 强制 NaN（保留官方已有值，
  典型：`risk_fac_momentum` 长窗求值异常，不能把空值/快通道值写进去）；
- 写前 backup、tmp+rename 原子替换、manifest 记录 added symbols；
- 重复执行幂等（新 symbol 已全在官方 → up_to_date）。

默认 dry-run，`--apply` 才写。只对**已存在的官方分区**生效；新交易日仍由
materialize_library_tail 的整日落盘处理。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]
log = logging.getLogger("delisted_factor_inject")

BACKUP_DIR = "_delisted_backup"
MANIFEST_REL = "factor_research/delisted_factor_inject.jsonl"


def _canon_symbol(value: object) -> str:
    from backend.shared.stock_utils import StockCodeUtil

    text = str(value or "").strip().upper()
    if not text:
        return ""
    if "." in text:
        return text
    if len(text) == 8 and text[:2] in {"SH", "SZ", "BJ"}:
        return f"{text[2:]}.{text[:2]}"
    if len(text) == 6 and text.isdigit():
        return StockCodeUtil.to_suffix(text)
    return text


def _partition(root: Path, library: str, dt: str) -> Path:
    return root / "6_ml_datasets" / library / f"dt={dt}" / "data.parquet"


def _read(path: Path) -> pd.DataFrame:
    frame = pd.read_parquet(path)
    frame["_symbol"] = frame["symbol"].map(_canon_symbol)
    return frame


def _restore_dtypes(combined: pd.DataFrame, official: pd.DataFrame) -> pd.DataFrame:
    """尽量按官方 dtype 还原；整型列因 NaN 无法还原时退 float。"""
    for column in official.columns:
        if column not in combined.columns:
            continue
        target = official[column].dtype
        try:
            combined[column] = combined[column].astype(target)
        except (TypeError, ValueError):
            if pd.api.types.is_integer_dtype(target):
                combined[column] = combined[column].astype("float64")
    return combined


@dataclass
class InjectResult:
    dt: str
    status: str
    added: list[str]
    rows_before: int = 0
    rows_after: int = 0
    excluded: list[str] | None = None
    backup: str | None = None

    def to_dict(self) -> dict:
        return {
            "dt": self.dt,
            "status": self.status,
            "added": self.added,
            "added_count": len(self.added),
            "rows_before": self.rows_before,
            "rows_after": self.rows_after,
            "excluded": self.excluded or [],
            "backup": self.backup,
        }


def inject_date(
    root: str | Path,
    *,
    library: str,
    dt: str,
    staging_root: str | Path,
    exclude_cols: list[str] | None = None,
    apply: bool = False,
) -> InjectResult:
    root = Path(root)
    official_path = _partition(root, library, dt)
    staging_path = Path(staging_root) / f"dt={dt}" / "data.parquet"
    if not official_path.is_file():
        return InjectResult(dt, "official_missing", [])
    if not staging_path.is_file():
        return InjectResult(dt, "staging_missing", [])

    official = _read(official_path)
    staging = _read(staging_path)
    official_set = set(official["_symbol"])
    new_symbols = sorted(set(staging["_symbol"]) - official_set)
    result = InjectResult(
        dt=dt,
        status="dry_run" if new_symbols else "up_to_date",
        added=new_symbols,
        rows_before=len(official),
        excluded=exclude_cols or [],
    )
    if not new_symbols:
        result.rows_after = len(official)
        return result
    if not apply:
        result.rows_after = len(official) + len(new_symbols)
        return result

    columns = [c for c in official.columns if c != "_symbol"]
    add = staging[staging["_symbol"].isin(new_symbols)].copy()
    add["symbol"] = add["_symbol"]
    for column in columns:
        if column not in add.columns:
            add[column] = pd.NA
    add = add[columns]
    for column in exclude_cols or []:
        if column in add.columns:
            add[column] = pd.NA

    combined = pd.concat([official[columns], add], ignore_index=True)
    combined = _restore_dtypes(combined, official)

    backup = root / "6_ml_datasets" / BACKUP_DIR / library / f"dt={dt}" / "data.parquet"
    backup.parent.mkdir(parents=True, exist_ok=True)
    if not backup.is_file():
        shutil.copy2(official_path, backup)
    result.backup = str(backup)

    tmp = official_path.with_name(".data.parquet.tmp")
    try:
        combined.to_parquet(tmp, index=False)
        os.replace(tmp, official_path)
    finally:
        tmp.unlink(missing_ok=True)
    result.status = "applied"
    result.rows_after = len(combined)
    return result


def _append_manifest(root: Path, payload: dict) -> None:
    path = root / MANIFEST_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="退市股因子行级追加（默认 dry-run）")
    parser.add_argument(
        "--root",
        default=os.getenv(
            "QM_QUANTDB_DATA_DIR", str(PROJECT_ROOT / "data" / "quantdb")
        ),
    )
    parser.add_argument("--library", required=True)
    parser.add_argument("--dates", required=True, help="逗号分隔 YYYYMMDD")
    parser.add_argument("--staging-root", default=None)
    parser.add_argument(
        "--exclude-cols", default="", help="逗号分隔：新 symbol 强制 NaN 的列"
    )
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
    )
    root = Path(args.root)
    staging_root = (
        Path(args.staging_root)
        if args.staging_root
        else root / "6_ml_datasets" / f"_staging_{args.library}"
    )
    exclude = [c.strip() for c in args.exclude_cols.split(",") if c.strip()]
    dates = [d.strip() for d in args.dates.split(",") if d.strip()]
    results = []
    for dt in dates:
        result = inject_date(
            root,
            library=args.library,
            dt=dt,
            staging_root=staging_root,
            exclude_cols=exclude,
            apply=args.apply,
        )
        results.append(result.to_dict())
        log.info(
            "%s %s：%s +%d 行 %d→%d",
            args.library,
            dt,
            result.status,
            len(result.added),
            result.rows_before,
            result.rows_after,
        )
        if result.status == "applied":
            _append_manifest(
                root,
                {
                    "ts": datetime.now().isoformat(timespec="seconds"),
                    "library": args.library,
                    **result.to_dict(),
                },
            )
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
