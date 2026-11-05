#!/usr/bin/env python3
"""V5 宽表按需物化（D1）。

按一组特征从 ``factor_dictionary``（571 列/32GB）物化窄表到
``6_ml_datasets/factor_dictionary_sel_{set_id}/dt=.../data.parquet``：
- set_id = sha256(排序后的特征列表)[:12]，同特征复用同一派生库；
- manifest 记录源指纹（schema/max_date/分区数/最新分区 size:mtime）、特征、行数；
- 默认 **dry-run**，``--apply`` 才写；源指纹变化需 ``--force`` 重建；
- 写入逐分区原子（tmp+rename），manifest 最后更新；构建中出现 ``_INCOMPLETE``
  标记，异常中断后下次拒绝静默续写。

派生库可通过 ``QuantDBFactorReader`` 的动态源发现直接读取（目录名合规即可），
不需要改 FactorSource 枚举；是否设为默认训练源由 A/B 决定（D1 不直接改默认）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

PROJECT_ROOT = Path(__file__).resolve().parents[2]
log = logging.getLogger("materialize_v5_narrow")

SOURCE_REL = "6_ml_datasets/factor_dictionary"
OUT_PREFIX = "factor_dictionary_sel_"
FEATURE_RE = re.compile(r"^[A-Za-z0-9_]+$")
RESERVED = {"symbol", "date", "dt", "time"}


def normalize_features(raw: list[str]) -> list[str]:
    features = sorted({str(f).strip() for f in raw if str(f).strip()})
    if not features:
        raise ValueError("features 不能为空")
    bad = [f for f in features if not FEATURE_RE.match(f) or f in RESERVED]
    if bad:
        raise ValueError(f"非法/保留特征名: {bad[:10]}")
    return features


def set_id_for(features: list[str]) -> str:
    return hashlib.sha256("\n".join(features).encode()).hexdigest()[:12]


def _source_partitions(
    root: Path, start: str | None, end: str | None
) -> list[tuple[str, Path]]:
    base = root / SOURCE_REL
    if not base.is_dir():
        raise FileNotFoundError(f"V5 宽表目录不存在: {base}")
    start_key = (start or "00000000").replace("-", "")
    end_key = (end or "99999999").replace("-", "")
    out = []
    for part in base.glob("dt=*"):
        digits = part.name[3:]
        if len(digits) != 8 or not digits.isdigit():
            continue
        if not (start_key <= digits <= end_key):
            continue
        path = part / "data.parquet"
        if path.is_file():
            out.append((digits, path))
    return sorted(out)


def _source_stamp(partitions: list[tuple[str, Path]]) -> str:
    if not partitions:
        return ""
    latest_dt, latest_path = partitions[-1]
    schema = [str(c) for c in pq.ParquetFile(latest_path).schema_arrow.names]
    schema_hash = hashlib.sha256("\n".join(schema).encode()).hexdigest()[:16]
    stat = latest_path.stat()
    return f"{schema_hash}|{latest_dt}|{len(partitions)}|{stat.st_size}:{int(stat.st_mtime)}"


def _out_dir(root: Path, set_id: str) -> Path:
    return root / "6_ml_datasets" / f"{OUT_PREFIX}{set_id}"


def _manifest_path(out_dir: Path) -> Path:
    return out_dir / "manifest.json"


def _read_manifest(out_dir: Path) -> dict | None:
    try:
        return json.loads(_manifest_path(out_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def plan(
    root: str | Path,
    *,
    features: list[str],
    start: str | None = None,
    end: str | None = None,
) -> dict:
    root = Path(root)
    features = normalize_features(features)
    sid = set_id_for(features)
    partitions = _source_partitions(root, start, end)
    return {
        "root": str(root),
        "features": features,
        "set_id": sid,
        "out_dir": str(_out_dir(root, sid)),
        "source_partitions": len(partitions),
        "start": partitions[0][0] if partitions else None,
        "end": partitions[-1][0] if partitions else None,
        "source_stamp": _source_stamp(partitions),
    }


def materialize(
    root: str | Path,
    *,
    features: list[str],
    start: str | None = None,
    end: str | None = None,
    apply: bool = False,
    force: bool = False,
) -> dict:
    root = Path(root)
    info = plan(root, features=features, start=start, end=end)
    features = info["features"]
    sid = info["set_id"]
    out_dir = Path(info["out_dir"])
    if not apply:
        info["status"] = "dry_run"
        return info

    partitions = _source_partitions(root, start, end)
    if not partitions:
        raise RuntimeError("源分区为空（检查 --start/--end 与 V5 数据）")
    # 先验源 schema，避免写到一半才发现特征不存在
    latest_schema = {
        str(c) for c in pq.ParquetFile(partitions[-1][1]).schema_arrow.names
    }
    missing = [f for f in features if f not in latest_schema]
    if missing:
        raise RuntimeError(f"V5 缺少特征列: {missing[:10]}")

    old_manifest = _read_manifest(out_dir)
    incomplete = out_dir / "_INCOMPLETE"
    if incomplete.exists() and not force:
        raise RuntimeError(
            f"{out_dir} 存在 _INCOMPLETE（上次构建中断）；确认现场后加 --force 重建"
        )
    if (
        old_manifest
        and old_manifest.get("source_stamp") == info["source_stamp"]
        and old_manifest.get("features") == features
        and not force
    ):
        info["status"] = "up_to_date"
        info["rows"] = old_manifest.get("rows")
        return info
    if old_manifest and not force:
        raise RuntimeError(
            "源指纹或特征集已变化（manifest 与当前 V5 不一致）；"
            "确认要重建请加 --force（会逐分区覆盖派生库）"
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    incomplete.write_text(
        datetime.now().isoformat(timespec="seconds"), encoding="utf-8"
    )

    total_rows = 0
    written = 0
    for dt, src in partitions:
        target = out_dir / f"dt={dt}" / "data.parquet"
        target.parent.mkdir(parents=True, exist_ok=True)
        frame = pd.read_parquet(src, columns=["symbol", "date", *features])
        tmp = target.with_suffix(".parquet.tmp")
        frame.to_parquet(tmp, index=False)
        os.replace(tmp, target)
        total_rows += len(frame)
        written += 1

    manifest = {
        "set_id": sid,
        "features": features,
        "source": SOURCE_REL,
        "source_stamp": info["source_stamp"],
        "source_partitions": len(partitions),
        "partitions_written": written,
        "rows": int(total_rows),
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    _atomic_write_json(_manifest_path(out_dir), manifest)
    generation_id = f"g{info['source_stamp'][:12]}-{sid}"
    _atomic_write_json(
        out_dir / "generation.json",
        {
            "generation_id": generation_id,
            "status": "active",
            "source_stamp": info["source_stamp"],
            "features": features,
            "partitions": written,
            "rows": int(total_rows),
            "created_at": manifest["created_at"],
        },
    )
    _atomic_write_json(out_dir / "active.json", {"generation_id": generation_id})
    incomplete.unlink(missing_ok=True)

    info["status"] = "applied"
    info["rows"] = int(total_rows)
    info["partitions_written"] = written
    info["generation_id"] = generation_id
    return info


def _parse_features(raw: str | None, file: str | None) -> list[str]:
    if file:
        text = Path(file).read_text(encoding="utf-8")
        return normalize_features(
            [line.strip() for line in text.splitlines() if line.strip()]
        )
    return normalize_features((raw or "").split(","))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="V5 宽表按需物化（默认 dry-run）")
    parser.add_argument(
        "--root",
        default=os.getenv(
            "QM_QUANTDB_DATA_DIR", str(PROJECT_ROOT / "data" / "quantdb")
        ),
    )
    parser.add_argument("--features", default=None, help="逗号分隔的 V5 特征列")
    parser.add_argument("--features-file", default=None, help="每行一个特征")
    parser.add_argument("--start", default=None, help="YYYYMMDD 或 YYYY-MM-DD")
    parser.add_argument("--end", default=None)
    parser.add_argument("--apply", action="store_true", help="实际写盘（默认 dry-run）")
    parser.add_argument(
        "--force", action="store_true", help="源变化/中断标记时强制重建"
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
    )
    features = _parse_features(args.features, args.features_file)
    try:
        result = materialize(
            args.root,
            features=features,
            start=args.start,
            end=args.end,
            apply=args.apply,
            force=args.force,
        )
    except Exception as exc:  # noqa: BLE001
        log.error("物化失败: %s", exc)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
