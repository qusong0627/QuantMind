#!/usr/bin/env python3
"""用库内 V5 因子字典播种 factor_dictionary 训练目录版本（零流量）。

背景：factor_dictionary 是 563 因子的动态因子库（34 分组），本地 parquet
尚未同步时无法走标准 refresh→seed 链路（seed 只收录已发现字段）。
本脚本直接以 V5 快照（backend/.../quantdb_factor_v5.py，随代码发布、
与线上库逐字节对齐）为唯一事实源，一次性建版并发布：

1. qm_quantdb_factor_field 补登记 571 列（is_present=FALSE，待数据同步后
   refresh 翻转为 TRUE；不影响标准链路幂等性）
2. 新建 draft 版本并写入 563 条映射（display_name/分组/排序取 V5，
   default_selected 沿用 CN 48 核心集规则）
3. 发布（仅归档同 source+market 的旧 published 版，不碰 l1_l2 版本）

幂等：重复执行只补缺失（ON CONFLICT DO NOTHING），已发布版本不重建；
如需重建请先归档/删除旧版本。

用法（容器内）：
  python backend/scripts/seed_factor_dictionary_version.py
  python backend/scripts/seed_factor_dictionary_version.py --dry-run
  python backend/scripts/seed_factor_dictionary_version.py --version-name "V5-571 全量"
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

SOURCE_DATASET = "factor_dictionary"
MARKET = "CN"

# 与 seed_draft_mappings 一致：排除键与标签列
KEY_COLUMNS = {"symbol", "date", "dt", "time", "release_id", "published_at"}
REQUIRED_COLUMNS = {"open", "high", "low", "close", "volume", "amount"}


def _load_v5() -> tuple[dict, list[dict]]:
    from backend.services.engine.data_platform.quantdb_factor_v5 import (
        GENERATED_AT,
        SCHEMA_VERSION,
        TOTAL_COLUMNS,
        V5_FIELDS,
    )

    meta = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": GENERATED_AT,
        "total_columns": TOTAL_COLUMNS,
    }
    cols = [
        {"field": f, **{k: v for k, v in info.items()}}
        for f, info in V5_FIELDS.items()
    ]
    return meta, cols


def _cn_defaults() -> frozenset:
    from backend.services.api.routers.admin.quantdb_factor_catalog import (
        DEFAULT_SELECTED_FACTORS,
    )

    return DEFAULT_SELECTED_FACTORS


async def _run(version_name: str, dry_run: bool) -> dict:
    import json

    from sqlalchemy import text

    from backend.services.api.routers.admin.quantdb_factor_catalog import (
        _ensure_schema,
    )
    from backend.shared.database_manager_v2 import get_session

    meta, cols = _load_v5()
    factors = [c for c in cols if c["field"] not in KEY_COLUMNS | REQUIRED_COLUMNS]
    defaults = _cn_defaults()

    result: dict = {
        "schema_version": meta["schema_version"],
        "total_columns": len(cols),
        "factor_columns": len(factors),
        "dry_run": dry_run,
    }

    async with get_session() as session:
        await _ensure_schema(session)

        # 已有同名 published 版本则直接复用（幂等）
        row = (
            await session.execute(
                text(
                    "SELECT version_id, status FROM qm_training_factor_catalog_version "
                    "WHERE market = :market AND source_dataset = :source "
                    "AND version_name = :name ORDER BY created_at DESC LIMIT 1"
                ),
                {"market": MARKET, "source": SOURCE_DATASET, "name": version_name},
            )
        ).mappings().first()
        if row and row["status"] == "published":
            result.update(
                {"version_id": row["version_id"], "status": "reused-published"}
            )
            return result

        version_id = (
            row["version_id"]
            if row
            else f"qdb-cn-{SOURCE_DATASET}-{uuid.uuid4().hex[:12]}"
        )
        if not row and not dry_run:
            await session.execute(
                text(
                    "INSERT INTO qm_training_factor_catalog_version "
                    "(version_id, market, version_name, status, source_dataset, created_by) "
                    "VALUES (:vid, :market, :name, 'draft', :source, 'seed-script')"
                ),
                {"vid": version_id, "market": MARKET, "name": version_name,
                 "source": SOURCE_DATASET},
            )
        result["version_id"] = version_id

        # 1) 字段登记（is_present=FALSE：字典已知、数据待同步）
        field_rows = [
            {
                "market": MARKET,
                "dataset": SOURCE_DATASET,
                "column": c["field"],
                "dtype": "",
                "schema": meta["schema_version"],
            }
            for c in cols
        ]
        if not dry_run:
            await session.execute(
                text(
                    "INSERT INTO qm_quantdb_factor_field "
                    "(market, dataset_id, column_name, data_type, schema_hash, "
                    "min_date, max_date, is_present, discovered_at) "
                    "VALUES (:market, :dataset, :column, :dtype, :schema, "
                    "NULL, NULL, FALSE, NOW()) "
                    "ON CONFLICT (market, dataset_id, column_name) DO NOTHING"
                ),
                field_rows,
            )
        result["fields_registered"] = len(field_rows)

        # 2) 映射播种（V5 分组/中文名/排序；默认勾选沿用 CN 48 规则）
        seeded = 0
        default_selected = 0
        for i, c in enumerate(factors):
            is_default = c["field"] in defaults
            params = {
                "mapping_id": uuid.uuid4().hex,
                "version_id": version_id,
                "source_dataset": SOURCE_DATASET,
                "source_column": c["field"],
                "feature_key": c["field"],
                "display_name": str(c.get("display_name") or c["field"]),
                "category_id": str(c.get("category_id") or "other"),
                "category_name": str(c.get("category_name") or "其他因子"),
                "default_selected": is_default,
                "sort_order": int(c.get("sort_order") or 0),
            }
            if not dry_run:
                await session.execute(
                    text(
                        "INSERT INTO qm_training_factor_mapping "
                        "(mapping_id, version_id, source_dataset, source_column, "
                        "feature_key, display_name, category_id, category_name, "
                        "enabled, default_selected, required, sort_order) "
                        "VALUES (:mapping_id, :version_id, :source_dataset, "
                        ":source_column, :feature_key, :display_name, "
                        ":category_id, :category_name, TRUE, :default_selected, "
                        "FALSE, :sort_order) "
                        "ON CONFLICT (version_id, source_dataset, source_column) "
                        "DO NOTHING"
                    ),
                    params,
                )
            seeded += 1
            default_selected += int(is_default)
        result["seeded_fields"] = seeded
        result["default_selected_fields"] = default_selected

        # 3) 发布（只归档同 source+market 的旧 published）
        if not dry_run:
            await session.execute(
                text(
                    "UPDATE qm_training_factor_catalog_version SET status = 'archived' "
                    "WHERE source_dataset = :source AND market = :market "
                    "AND status = 'published' AND version_id <> :vid"
                ),
                {"source": SOURCE_DATASET, "market": MARKET, "vid": version_id},
            )
            await session.execute(
                text(
                    "UPDATE qm_training_factor_catalog_version "
                    "SET status = 'published', published_at = NOW() "
                    "WHERE version_id = :vid"
                ),
                {"vid": version_id},
            )
        result["status"] = "published" if not dry_run else "dry-run"
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="只统计不写库")
    ap.add_argument("--version-name", default="V5-571 全量",
                    help="版本名（默认 V5-571 全量）")
    args = ap.parse_args()
    result = asyncio.run(_run(args.version_name, args.dry_run))
    import json as _json

    print(_json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
